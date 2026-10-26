"""Local image encoder adapters with explicit, reproducible provenance.

The audit package deliberately does not install a large neural network stack or
download checkpoints.  This module provides a small deterministic Pillow
baseline and opt-in adapters for locally available TorchScript and
Transformers checkpoints.  Every generated vector is accompanied by a
``model_id`` and a provenance record so that a retrieval result can be
reproduced and audited later.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import csv
import hashlib
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from PIL import Image, ImageOps

from .common import AuditError, sha256, write_csv, write_json


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"}
EMBEDDING_FIELDS = ["asset_id", "model_id", "vector"]


def _path_key(path: str | Path) -> str:
    """Return a case-normalized canonical key for local path matching."""
    return os.path.normcase(str(Path(path).resolve(strict=False)))


def load_asset_manifest_mapping(
    manifest_path: str | Path,
    input_root: str | Path,
    paths: Iterable[str | Path],
) -> dict[str, str]:
    """Map input image paths to IDs from an approved reference manifest.

    ``embedding-generate`` normally derives IDs from image bytes.  Reference
    galleries already have stable IDs in their manifest, however, and those
    IDs must be reused by ``reference-retrieve``.  This helper accepts either
    ``local_path`` or ``relative_path`` (or both), resolving relative values
    against the embedding input directory first and then the manifest's
    directory.  Every input image must be represented by exactly one manifest
    row; unrelated rows in a larger manifest are allowed.

    Paths are matched against the already-discovered input files rather than
    opening arbitrary paths from the manifest.  This keeps the command scoped
    to its input directory and prevents a manifest from widening the read
    boundary.  The returned keys are canonical absolute paths suitable for
    :func:`generate_embedding_rows`.
    """
    manifest = Path(manifest_path).resolve()
    root = Path(input_root).resolve()
    input_paths = [Path(path).resolve() for path in paths]
    if not input_paths:
        raise AuditError("Asset manifest mapping requires at least one input image.")
    by_key = {_path_key(path): path for path in input_paths}
    if len(by_key) != len(input_paths):
        raise AuditError("Embedding input contains duplicate canonical paths.")
    try:
        with manifest.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = reader.fieldnames or []
            if len(fields) != len(set(fields)):
                raise AuditError("Asset manifest contains duplicate column names.")
            if "asset_id" not in fields or not ({"local_path", "relative_path"} & set(fields)):
                raise AuditError(
                    "Asset manifest must contain asset_id and local_path or relative_path."
                )
            rows = list(reader)
    except FileNotFoundError:
        raise AuditError("Asset manifest does not exist.") from None
    except OSError as exc:
        raise AuditError(f"Could not read asset manifest: {exc}") from None
    if not rows:
        raise AuditError("Asset manifest is empty.")

    mapping: dict[str, str] = {}
    mapped_keys: set[str] = set()
    seen_ids: set[str] = set()
    manifest_ids: set[str] = set()
    seen_manifest_paths: set[str] = set()

    def candidates(value: str):
        raw = Path(value)
        if raw.is_absolute():
            return (raw.resolve(strict=False),)
        # Most street-view manifests store a basename relative to the download
        # directory.  Exporters using ``images/foo.jpg`` commonly store the
        # manifest next to that directory, hence both roots are intentional.
        return (
            (root / raw).resolve(strict=False),
            (manifest.parent / raw).resolve(strict=False),
        )

    for row_number, row in enumerate(rows, start=2):
        asset_id = str(row.get("asset_id") or "").strip()
        if not asset_id:
            raise AuditError(f"Asset manifest asset_id is blank (row {row_number}).")
        if asset_id in manifest_ids:
            raise AuditError("Asset manifest asset_id values must be unique.")
        manifest_ids.add(asset_id)
        values = []
        for field in ("local_path", "relative_path"):
            value = str(row.get(field) or "").strip()
            if value:
                values.append((field, value))
        if not values:
            # Rows for references that were not downloaded yet are harmless;
            # input files still require a concrete mapping below.
            continue
        matched: dict[str, Path] = {}
        for field, value in values:
            for candidate in candidates(value):
                key = _path_key(candidate)
                if key in by_key:
                    matched[key] = by_key[key]
        if not matched:
            # A manifest may cover a superset of the current image directory.
            # Ignore those rows and fail only if an actual input is unmapped.
            continue
        if len(matched) > 1:
            raise AuditError(
                f"Asset manifest local_path and relative_path disagree (row {row_number})."
            )
        key = next(iter(matched))
        if key in seen_manifest_paths:
            raise AuditError("Asset manifest maps one input image more than once.")
        if asset_id in seen_ids:
            raise AuditError("Asset manifest asset_id values must be unique for mapped images.")
        seen_manifest_paths.add(key)
        seen_ids.add(asset_id)
        mapped_keys.add(key)
        # Keep the original canonical spelling for the public mapping.  The
        # matcher above is case-insensitive on Windows, while
        # ``generate_embedding_rows`` also accepts ordinary ``Path.as_posix``
        # keys supplied by existing callers.
        mapping[matched[key].as_posix()] = asset_id

    missing = [path for path in input_paths if _path_key(path) not in mapped_keys]
    if missing:
        preview = ", ".join(path.name for path in missing[:3])
        suffix = "" if len(missing) <= 3 else f" (+{len(missing) - 3} more)"
        raise AuditError(f"Asset manifest does not map every input image: {preview}{suffix}")
    return mapping


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _safe_float_vector(value: Any) -> list[float]:
    """Convert an encoder output to a finite, L2-normalized list."""
    if hasattr(value, "detach"):
        value = value.detach().cpu()
    if hasattr(value, "numpy"):
        value = value.numpy()
    if hasattr(value, "tolist"):
        value = value.tolist()
    # Most image encoders return [1, D] for a single image.
    while isinstance(value, list) and len(value) == 1 and isinstance(value[0], list):
        value = value[0]
    if not isinstance(value, (list, tuple)) or not value:
        raise AuditError("Local encoder returned an empty or non-vector output.")
    try:
        vector = [float(item) for item in value]
    except (TypeError, ValueError):
        raise AuditError("Local encoder returned a non-numeric vector.") from None
    if not all(math.isfinite(item) for item in vector):
        raise AuditError("Local encoder returned a non-finite vector.")
    norm = math.sqrt(sum(item * item for item in vector))
    if norm == 0:
        raise AuditError("Local encoder returned a zero-norm vector.")
    return [item / norm for item in vector]


@dataclass(frozen=True)
class EncoderProvenance:
    """Machine-readable description of an embedding run."""

    backend: str
    model_id: str
    model_revision: str
    dimensions: int
    preprocess: str
    deterministic: bool
    device: str = "cpu"
    weights_sha256: str | None = None
    packages: Mapping[str, str] = field(default_factory=dict)
    network_mode: str = "offline"

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "dimensions": self.dimensions,
            "preprocess": self.preprocess,
            "deterministic": self.deterministic,
            "device": self.device,
            "weights_sha256": self.weights_sha256,
            "packages": dict(sorted(self.packages.items())),
            "network_mode": self.network_mode,
        }


class ImageEncoder(Protocol):
    """Minimal protocol implemented by every local image encoder."""

    @property
    def provenance(self) -> EncoderProvenance: ...

    def encode_image(self, path: Path) -> list[float]: ...


def validate_image_path(path: str | Path, max_file_bytes: int = 1_000_000_000) -> Path:
    """Validate a local image before passing it to an optional model."""
    raw = Path(path).expanduser()
    # Match the audit scanner's symlink policy.  A link can otherwise redirect
    # a seemingly local model input outside the declared photo directory.
    if raw.is_symlink() or any(parent.is_symlink() for parent in raw.parents):
        raise AuditError("Encoder input must not be a symbolic link.")
    resolved = raw.resolve()
    if not resolved.is_file() or resolved.suffix.lower() not in IMAGE_EXTENSIONS:
        raise AuditError("Encoder input must be an existing supported image file.")
    try:
        size = resolved.stat().st_size
    except OSError:
        raise AuditError("Could not inspect encoder input file.") from None
    if size <= 0 or size > max_file_bytes:
        raise AuditError("Encoder input file exceeds the configured size limit.")
    # verify() catches truncated files without decoding the full pixel buffer.
    try:
        with Image.open(resolved) as image:
            image.verify()
    except (OSError, ValueError):
        raise AuditError("Encoder input is not a valid image.") from None
    return resolved


class PillowGridEncoder:
    """Deterministic, dependency-light visual descriptor.

    This is a useful offline fallback and a test fixture, not a claim of
    geolocation capability.  It combines a 16x16 RGB thumbnail and per-channel
    histograms, then L2-normalizes the result.
    """

    def __init__(self, grid_size: int = 16, bins: int = 16) -> None:
        if type(grid_size) is not int or not 4 <= grid_size <= 64:
            raise AuditError("Pillow grid_size must be an integer between 4 and 64.")
        if type(bins) is not int or not 4 <= bins <= 64:
            raise AuditError("Pillow histogram bins must be an integer between 4 and 64.")
        self.grid_size = grid_size
        self.bins = bins
        dimensions = grid_size * grid_size * 3 + bins * 3
        self._provenance = EncoderProvenance(
            backend="pillow-grid",
            model_id=f"pillow-rgb-grid-v1-{grid_size}x{grid_size}-b{bins}",
            model_revision="builtin",
            dimensions=dimensions,
            preprocess=f"exif-transpose;RGB;thumbnail={grid_size}x{grid_size};histogram={bins}",
            deterministic=True,
            device="cpu",
            packages={"Pillow": _package_version("Pillow") or "unknown"},
        )

    @property
    def provenance(self) -> EncoderProvenance:
        return self._provenance

    def encode_image(self, path: str | Path) -> list[float]:
        path = validate_image_path(path)
        try:
            with Image.open(path) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
                thumb = image.resize((self.grid_size, self.grid_size), Image.Resampling.BILINEAR)
                pixels = list(thumb.getdata())
                values = [channel / 255.0 for pixel in pixels for channel in pixel]
                for channel in range(3):
                    hist = [0] * self.bins
                    for pixel in pixels:
                        index = min(self.bins - 1, int(pixel[channel] * self.bins / 256))
                        hist[index] += 1
                    values.extend(count / len(pixels) for count in hist)
        except (OSError, ValueError):
            raise AuditError("Could not decode encoder input image.") from None
        return _safe_float_vector(values)


class TorchScriptImageEncoder:
    """Adapter for a locally stored TorchScript image model.

    The checkpoint must already exist on disk.  No network access is attempted.
    The model receives a float tensor shaped ``[1, 3, image_size, image_size]``
    normalized with ImageNet statistics and must return a vector, tensor, tuple,
    or mapping containing a vector under ``embedding``/``pooler_output``.
    """

    def __init__(self, checkpoint: str | Path, image_size: int = 224, device: str = "cpu") -> None:
        self.checkpoint = validate_local_checkpoint(checkpoint)
        if type(image_size) is not int or not 32 <= image_size <= 1024:
            raise AuditError("TorchScript image_size must be an integer between 32 and 1024.")
        if device != "cpu":
            raise AuditError("Only CPU is supported by the default offline adapter.")
        try:
            torch = importlib.import_module("torch")
        except ImportError:
            raise AuditError("TorchScript backend requires the optional 'torch' package.") from None
        try:
            self._torch = torch
            self._model = torch.jit.load(str(self.checkpoint), map_location=device).eval()
        except Exception as exc:
            raise AuditError(f"Could not load local TorchScript checkpoint: {exc}") from None
        self.image_size = image_size
        weights_digest = sha256(self.checkpoint)
        self._provenance = EncoderProvenance(
            backend="torchscript",
            model_id=f"torchscript:{self.checkpoint.name}@sha256:{weights_digest[:16]}",
            model_revision="local-file",
            dimensions=0,  # filled after the first output; see provenance property
            preprocess=f"exif-transpose;RGB;resize={image_size};imagenet-normalize",
            deterministic=True,
            device=device,
            weights_sha256=weights_digest,
            packages={"torch": _package_version("torch") or "unknown", "Pillow": _package_version("Pillow") or "unknown"},
        )
        self._dimensions: int | None = None

    @property
    def provenance(self) -> EncoderProvenance:
        if self._dimensions is None:
            return self._provenance
        return EncoderProvenance(**{**self._provenance.as_dict(), "dimensions": self._dimensions})

    def encode_image(self, path: str | Path) -> list[float]:
        path = validate_image_path(path)
        try:
            with Image.open(path) as source:
                image = ImageOps.exif_transpose(source).convert("RGB").resize(
                    (self.image_size, self.image_size), Image.Resampling.BICUBIC
                )
            try:
                numpy = importlib.import_module("numpy")
            except ImportError:
                raise AuditError("TorchScript backend requires the optional 'numpy' package.") from None
            tensor = self._torch.from_numpy(numpy.asarray(image)).permute(2, 0, 1).float() / 255.0
            mean = self._torch.tensor([0.485, 0.456, 0.406]).view(3, 1, 1)
            std = self._torch.tensor([0.229, 0.224, 0.225]).view(3, 1, 1)
            with self._torch.inference_mode():
                output = self._model(((tensor - mean) / std).unsqueeze(0))
            if isinstance(output, Mapping):
                output = output.get("embedding", output.get("pooler_output", output.get("last_hidden_state")))
            elif isinstance(output, (tuple, list)):
                output = output[0]
            if output is None:
                raise AuditError("TorchScript model did not return an embedding.")
            if getattr(output, "ndim", 0) > 2:
                output = output.mean(dim=1)
            vector = _safe_float_vector(output)
            self._dimensions = len(vector)
            return vector
        except AuditError:
            raise
        except Exception as exc:
            raise AuditError(f"TorchScript inference failed: {exc}") from None


class TransformersLocalImageEncoder:
    """Adapter for a Transformers image model stored in a local directory."""

    def __init__(self, model_dir: str | Path, device: str = "cpu") -> None:
        self.model_dir = Path(model_dir).expanduser().resolve()
        if not self.model_dir.is_dir():
            raise AuditError("Transformers model_dir must be a local directory.")
        if device != "cpu":
            raise AuditError("Only CPU is supported by the default offline adapter.")
        try:
            transformers = importlib.import_module("transformers")
            torch = importlib.import_module("torch")
        except ImportError:
            raise AuditError("Transformers backend requires optional 'transformers' and 'torch' packages.") from None
        try:
            self._torch = torch
            self._processor = transformers.AutoImageProcessor.from_pretrained(str(self.model_dir), local_files_only=True)
            self._model = transformers.AutoModel.from_pretrained(str(self.model_dir), local_files_only=True).eval()
        except Exception as exc:
            raise AuditError(f"Could not load local Transformers checkpoint: {exc}") from None
        model_digest = _directory_sha256(self.model_dir)
        self._provenance = EncoderProvenance(
            backend="transformers",
            model_id=f"transformers:{self.model_dir.name}@sha256:{model_digest[:16]}",
            model_revision="local-directory",
            dimensions=0,
            preprocess="AutoImageProcessor(local_files_only=True);model.eval()",
            deterministic=True,
            device=device,
            weights_sha256=model_digest,
            packages={"transformers": _package_version("transformers") or "unknown", "torch": _package_version("torch") or "unknown", "Pillow": _package_version("Pillow") or "unknown"},
        )
        self._dimensions: int | None = None

    @property
    def provenance(self) -> EncoderProvenance:
        if self._dimensions is None:
            return self._provenance
        return EncoderProvenance(**{**self._provenance.as_dict(), "dimensions": self._dimensions})

    def encode_image(self, path: str | Path) -> list[float]:
        path = validate_image_path(path)
        try:
            with Image.open(path) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
            inputs = self._processor(images=image, return_tensors="pt")
            with self._torch.inference_mode():
                if hasattr(self._model, "get_image_features"):
                    output = self._model.get_image_features(pixel_values=inputs["pixel_values"])
                else:
                    outputs = self._model(**inputs)
                    output = getattr(outputs, "pooler_output", None)
                    if output is None:
                        output = getattr(outputs, "image_embeds", None)
                    if output is None:
                        output = getattr(outputs, "last_hidden_state", None)
            if output is None:
                raise AuditError("Transformers model did not return an embedding.")
            if getattr(output, "ndim", 0) > 2:
                output = output.mean(dim=1)
            vector = _safe_float_vector(output)
            self._dimensions = len(vector)
            return vector
        except AuditError:
            raise
        except Exception as exc:
            raise AuditError(f"Transformers inference failed: {exc}") from None


class OpenCLIPImageEncoder:
    """Adapter for a local OpenCLIP checkpoint.

    ``model_name`` selects the architecture already known by the installed
    open_clip package; ``checkpoint`` must be a local weight file.  Passing a
    local path as ``pretrained`` prevents the adapter from fetching weights.
    """

    def __init__(self, model_name: str, checkpoint: str | Path, device: str = "cpu") -> None:
        if not isinstance(model_name, str) or not model_name.strip():
            raise AuditError("OpenCLIP model_name must be a non-empty string.")
        self.checkpoint = validate_local_checkpoint(checkpoint)
        if device != "cpu":
            raise AuditError("Only CPU is supported by the default offline adapter.")
        try:
            open_clip = importlib.import_module("open_clip")
            torch = importlib.import_module("torch")
        except ImportError:
            raise AuditError("OpenCLIP backend requires optional 'open_clip_torch' and 'torch' packages.") from None
        try:
            self._torch = torch
            self._model, _, self._preprocess = open_clip.create_model_and_transforms(
                model_name, pretrained=str(self.checkpoint), device=device
            )
            self._model.eval()
        except Exception as exc:
            raise AuditError(f"Could not load local OpenCLIP checkpoint: {exc}") from None
        self.model_name = model_name
        weights_digest = sha256(self.checkpoint)
        self._provenance = EncoderProvenance(
            backend="open_clip",
            model_id=f"open_clip:{model_name}@sha256:{weights_digest[:16]}",
            model_revision="local-file",
            dimensions=0,
            preprocess="open_clip.create_model_and_transforms;model.eval()",
            deterministic=True,
            device=device,
            weights_sha256=weights_digest,
            packages={"open_clip_torch": _package_version("open_clip_torch") or "unknown", "torch": _package_version("torch") or "unknown", "Pillow": _package_version("Pillow") or "unknown"},
        )
        self._dimensions: int | None = None

    @property
    def provenance(self) -> EncoderProvenance:
        if self._dimensions is None:
            return self._provenance
        return EncoderProvenance(**{**self._provenance.as_dict(), "dimensions": self._dimensions})

    def encode_image(self, path: str | Path) -> list[float]:
        path = validate_image_path(path)
        try:
            with Image.open(path) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
            tensor = self._preprocess(image).unsqueeze(0)
            with self._torch.inference_mode():
                output = self._model.encode_image(tensor)
            vector = _safe_float_vector(output)
            self._dimensions = len(vector)
            return vector
        except AuditError:
            raise
        except Exception as exc:
            raise AuditError(f"OpenCLIP inference failed: {exc}") from None


def validate_local_checkpoint(path: str | Path) -> Path:
    path = Path(path).expanduser().resolve()
    if not path.is_file() or path.stat().st_size <= 0:
        raise AuditError("Model checkpoint must be a non-empty local file.")
    return path


def _directory_sha256(root: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    files = sorted(p for p in root.rglob("*") if p.is_file())
    if not files:
        raise AuditError("Local model directory contains no files.")
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def asset_id_for_path(path: str | Path) -> str:
    """Stable ID for an image when no audit manifest is supplied."""
    return sha256(validate_image_path(path))[:16]


def generate_embedding_rows(
    paths: Iterable[str | Path],
    encoder: ImageEncoder,
    asset_ids: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Encode paths in sorted order, returning rows accepted by embeddings.py."""
    rows = []
    seen: set[str] = set()
    for raw_path in sorted((Path(path) for path in paths), key=lambda p: p.as_posix()):
        path = validate_image_path(raw_path)
        key = path.as_posix()
        asset_id = (asset_ids or {}).get(key) or (asset_ids or {}).get(path.name) or asset_id_for_path(path)
        if not isinstance(asset_id, str) or not asset_id.strip():
            raise AuditError("Embedding asset_id values must be non-empty and unique.")
        if asset_id in seen:
            # Match the audit scanner's duplicate-content handling: preserve a
            # stable content prefix while adding a deterministic path suffix.
            suffix = hashlib.sha256(path.as_posix().encode("utf-8")).hexdigest()[:12]
            candidate = f"{asset_id}-{suffix}"
            offset = 12
            while candidate in seen:
                offset += 4
                candidate = f"{asset_id}-{suffix[:offset]}"
            asset_id = candidate
        seen.add(asset_id)
        rows.append({"asset_id": asset_id, "model_id": encoder.provenance.model_id,
                     "vector": encoder.encode_image(path)})
    if not rows:
        raise AuditError("No images were supplied to the local encoder.")
    dimensions = {len(row["vector"]) for row in rows}
    if len(dimensions) != 1:
        raise AuditError("Local encoder returned inconsistent vector dimensions.")
    return rows


def write_embedding_bundle(csv_path: str | Path, rows: Sequence[Mapping[str, Any]], encoder: ImageEncoder,
                           provenance_path: str | Path | None = None) -> None:
    """Write vectors and a sidecar provenance JSON without overwriting files."""
    csv_path = Path(csv_path)
    sidecar = Path(provenance_path) if provenance_path else csv_path.with_suffix(csv_path.suffix + ".provenance.json")
    if csv_path.exists() or sidecar.exists():
        raise AuditError("Embedding output or provenance sidecar already exists; choose new paths.")
    write_csv(csv_path, EMBEDDING_FIELDS, rows)
    write_json(sidecar, {"provenance": encoder.provenance.as_dict(), "row_count": len(rows),
                         "asset_ids": [row["asset_id"] for row in rows]})


def build_encoder(backend: str, model: str | Path | None = None) -> ImageEncoder:
    """Construct an encoder without downloading anything."""
    if backend == "pillow":
        return PillowGridEncoder()
    if backend == "torchscript":
        if model is None:
            raise AuditError("--model is required for the torchscript backend.")
        return TorchScriptImageEncoder(model)
    if backend == "transformers":
        if model is None:
            raise AuditError("--model is required for the transformers backend.")
        # A model directory is sufficient for Transformers because its config
        # is stored alongside the local weights.
        return TransformersLocalImageEncoder(model)
    if backend == "open_clip":
        if model is None or not isinstance(model, str) or "::" not in model:
            raise AuditError("OpenCLIP --model must be MODEL_NAME::CHECKPOINT_PATH.")
        model_name, checkpoint = model.split("::", 1)
        return OpenCLIPImageEncoder(model_name, checkpoint)
    raise AuditError("Unknown local encoder backend.")


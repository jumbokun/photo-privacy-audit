"""Create a new image copy without EXIF/ancillary metadata."""
from pathlib import Path

from PIL import Image, ImageOps

from .common import AuditError, sha256
from .metadata import EXTENSIONS


def strip_metadata(source_root, output_root, relative_path, max_pixels=60000000):
    source_root, output_root = Path(source_root).resolve(), Path(output_root).resolve()
    source = (source_root / relative_path).resolve()
    if not source.is_file() or not source.is_relative_to(source_root):
        raise AuditError("Sanitization input must be a file inside the source directory.")
    if output_root == source_root or output_root.is_relative_to(source_root):
        raise AuditError("Sanitized output must be outside the source directory.")
    destination = (output_root / relative_path).resolve()
    if not destination.is_relative_to(output_root):
        raise AuditError("Invalid output path.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise AuditError("Sanitized output already exists; nothing was overwritten.")
    original_hash = sha256(source)
    try:
        with Image.open(source) as image:
            if image.width * image.height > max_pixels:
                raise AuditError("Pixel limit exceeded.")
            clean = ImageOps.exif_transpose(image).copy()
            clean.info.clear()
            suffix = destination.suffix.lower()
            if suffix in {".jpg", ".jpeg"}:
                if clean.mode not in {"RGB", "L", "CMYK"}:
                    clean = clean.convert("RGB")
                clean.save(destination, format="JPEG", quality=95, optimize=True)
            elif suffix == ".png":
                clean.save(destination, format="PNG", optimize=True)
            elif suffix == ".webp":
                clean.save(destination, format="WEBP", quality=95, method=6)
            else:
                raise AuditError("Sanitization supports JPEG, PNG and WebP outputs.")
    except AuditError:
        raise
    except Exception:
        if destination.exists():
            destination.unlink()
        raise AuditError("Could not create sanitized copy.") from None
    # The source is user-controlled and may be modified by another process
    # while a large image is being decoded. Do not report a copy whose pixels
    # were produced from a different byte stream than the recorded hash.
    try:
        source_unchanged = sha256(source) == original_hash
    except OSError:
        source_unchanged = False
    if not source_unchanged:
        if destination.exists():
            destination.unlink()
        raise AuditError("A source file changed during sanitization. Retry with stable inputs.")
    return {"relative_path": str(Path(relative_path)), "source_sha256": original_hash,
            "sanitized_sha256": sha256(destination), "source_bytes": source.stat().st_size,
            "sanitized_bytes": destination.stat().st_size}

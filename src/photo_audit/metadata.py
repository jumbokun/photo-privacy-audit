"""Presence-only metadata inspection. Raw values never enter report records."""
import json
import shutil
import subprocess
import warnings
from pathlib import Path

from PIL import Image, ImageOps, ImageStat

from .common import AuditError, sha256

EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp", ".gif",
              ".heic", ".heif", ".avif", ".dng", ".cr2", ".cr3", ".nef", ".arw"}
FORMAT_NAMES = {"JPEG", "PNG", "WEBP", "TIFF", "BMP", "GIF", "AVIF", "HEIF"}
GPS_TAG = 34853
EXIF_IFD = 34665
TIME_TAGS = {306, 36867, 36868, 36880, 36881, 36882}
DEVICE_TAGS = {271, 272, 42036}
SERIAL_TAGS = {42033, 42037, 50735}
OPAQUE_TAGS = {270, 37500, 37510, 40091, 40092, 40093, 40094, 40095, 700, 33723, 34377}


def _dhash(gray, width=9, height=8):
    """Return a difference hash at the requested scale."""
    pixels = list(gray.resize((width, height), Image.Resampling.LANCZOS).getdata())
    bits = 0
    for y in range(height):
        for x in range(width - 1):
            bits = (bits << 1) | (pixels[y * width + x] > pixels[y * width + x + 1])
    return f"{bits:0{((width - 1) * height + 3) // 4}x}"


def _dct_2d(values, size):
    """Small dependency-free DCT-II used by pHash."""
    import math

    factor = math.pi / (2.0 * size)
    scale = (1.0 / math.sqrt(size), math.sqrt(2.0 / size))
    basis = [[(scale[0] if k == 0 else scale[1]) * math.cos((2 * x + 1) * k * factor)
              for x in range(size)] for k in range(size)]
    # DCT is separable: two matrix-vector passes avoid the O(n^4) nested sum.
    rows = [[sum(basis[u][x] * values[x][y] for x in range(size))
             for y in range(size)] for u in range(size)]
    return [[sum(rows[x][y] * basis[v][y] for y in range(size))
             for v in range(size)] for x in range(size)]


def _phash(gray, image_size=32, hash_size=8):
    """Return a 64-bit low-frequency perceptual hash."""
    pixels = list(gray.resize((image_size, image_size), Image.Resampling.LANCZOS).getdata())
    values = [pixels[row * image_size:(row + 1) * image_size] for row in range(image_size)]
    coefficients = _dct_2d(values, image_size)
    low = [coefficients[x][y] for x in range(hash_size) for y in range(hash_size)]
    ordered = sorted(low[1:])
    median = ordered[len(ordered) // 2]
    bits = 0
    for value in low:
        bits = (bits << 1) | (value > median)
    return f"{bits:016x}"


def fingerprints(image):
    """Return multi-scale hashes for four right-angle rotations.

    Correlation takes the minimum distance across variants as a review lead;
    hashes never prove that two images depict the same scene.
    """
    oriented = ImageOps.exif_transpose(image).convert("L")
    contrast = ImageStat.Stat(oriented.resize((32, 32))).stddev[0]
    rotations = []
    for angle in (0, 90, 180, 270):
        rotations.append(oriented if angle == 0 else oriented.rotate(angle, expand=True))
    return {
        "dhash": _dhash(rotations[0], 9, 8),
        "dhash_rotations": [_dhash(item, 9, 8) for item in rotations],
        "dhash_16": _dhash(rotations[0], 17, 16),
        "phash": _phash(rotations[0]),
        "phash_rotations": [_phash(item) for item in rotations],
        "hash_informative": contrast >= 4,
    }


def fingerprint(image):
    """Backward-compatible 64-bit dHash interface."""
    hashes = fingerprints(image)
    return hashes["dhash"], hashes["hash_informative"]


def exiftool_presence(path, executable):
    # ExifTool stays local. Capture all stdout/stderr; only booleans escape.
    result = subprocess.run([executable, "-j", "-G1", "-s", str(path.resolve())],
                            capture_output=True, timeout=30)
    if result.returncode != 0:
        raise AuditError("ExifTool could not inspect this file.")
    records = json.loads(result.stdout.decode("utf-8"))
    if not isinstance(records, list) or len(records) != 1 or "Error" in records[0]:
        raise AuditError("ExifTool returned an invalid record.")
    keys = [key.lower() for key in records[0]]
    flags = {
        "gps": any("gps" in k or any(t in k for t in ("latitude", "longitude", "locationiso6709")) for k in keys),
        "timestamp": any(any(t in k for t in ("datetimeoriginal", "createdate", "gpsdatetime", "datetimedigitized")) for k in keys),
        "serial": any("serial" in k or "ownername" in k for k in keys),
        "device": any(k.rsplit(":", 1)[-1] in {"make", "model", "lensmodel"} for k in keys),
        "opaque_metadata": any(any(t in k for t in ("xmp", "iptc", "makernote", "comment", "description")) for k in keys),
    }
    warning = any("warning" in k or "error" in k for k in keys)
    return flags, warning


def resolve_exiftool(option):
    if option == "off":
        return None
    executable = shutil.which("exiftool") if option == "auto" else shutil.which(option)
    if option != "auto" and not executable:
        raise AuditError("Requested ExifTool executable was not found.")
    return executable


def inspect_image(path, policy, executable=None):
    row = {"sha256": sha256(path), "bytes": path.stat().st_size, "format": "unknown",
           "width": None, "height": None, "status": "ok", "error_code": "",
           "metadata_coverage": "pillow_limited", "metadata_score": None,
           "dhash": None, "dhash_rotations": [], "dhash_16": None,
           "phash": None, "phash_rotations": [], "hash_informative": False,
           "frames": None,
           **{k: False for k in policy["metadata_scores"]}}
    if row["bytes"] > policy["max_file_mb"] * 1024 * 1024:
        row.update(status="unreadable", error_code="file_size_limit")
        return row
    with warnings.catch_warnings(record=True) as notices:
        warnings.simplefilter("always")
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            with Image.open(path) as image:
                if image.width * image.height > policy["max_pixels"]:
                    raise AuditError("Pixel limit exceeded.")
                row.update(format=image.format if image.format in FORMAT_NAMES else "other",
                           width=image.width, height=image.height, frames=getattr(image, "n_frames", 1))
                image.load()
                exif = image.getexif()
                tags = set(exif)
                if EXIF_IFD in exif:
                    tags.update(exif.get_ifd(EXIF_IFD))
                row.update(gps=GPS_TAG in tags, timestamp=bool(tags & TIME_TAGS),
                           serial=bool(tags & SERIAL_TAGS), device=bool(tags & DEVICE_TAGS),
                           opaque_metadata=bool(tags & OPAQUE_TAGS) or
                           any(k.lower() not in {"exif", "icc_profile", "dpi", "jfif", "jfif_version", "jfif_unit", "jfif_density", "transparency", "aspect"} for k in image.info))
                row.update(fingerprints(image))
                if row["frames"] > 1:
                    row.update(status="partial", error_code="multiple_frames_not_fully_audited")
            if notices:
                row.update(status="partial", error_code="metadata_parser_warning")
        except Exception:
            # Parser messages can contain embedded metadata or personal file paths.
            row.update(status="unreadable", error_code="unsupported_corrupt_or_oversize")
    if executable:
        try:
            extra, warning = exiftool_presence(path, executable)
            for key, value in extra.items():
                row[key] = row[key] or value
            row["metadata_coverage"] = "exiftool_extended"
            if warning:
                row.update(status="partial" if row["status"] == "ok" else row["status"],
                           error_code="extended_parser_warning")
        except Exception:
            row.update(status="partial" if row["status"] == "ok" else row["status"],
                       error_code="extended_parser_failed")
    if sha256(path) != row["sha256"]:
        raise AuditError("A source file changed during inspection. Retry with stable inputs.")
    row["metadata_score"] = max([policy["metadata_scores"][k] for k in policy["metadata_scores"] if row[k]] or [0])
    if row["status"] == "unreadable" and row["metadata_score"] == 0:
        row["metadata_score"] = None
    return row

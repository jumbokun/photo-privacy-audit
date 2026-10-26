"""Validated local I/O and reproducibility helpers."""
import csv
import hashlib
import importlib.metadata
import json
import platform
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from . import __version__

ROOT = Path(__file__).resolve().parents[2]
POLICY_FILE = ROOT / "config" / "audit-policy.yaml"


class AuditError(ValueError):
    """Only static, non-sensitive messages should reach the terminal."""


def local_timezone(name="Asia/Shanghai"):
    """Resolve an IANA timezone, with a fixed China fallback on bare Windows."""
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "Asia/Shanghai":
            return timezone(timedelta(hours=8), name="CST")
        raise AuditError(f"Timezone data is unavailable for {name}.") from None


def utc_now():
    return datetime.now(timezone.utc)


def sha256(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def read_json(path):
    with Path(path).open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def write_json(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def write_text(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        handle.write(value)


def write_csv(path, fields, rows):
    with Path(path).open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            safe = {}
            for key, value in row.items():
                if isinstance(value, (list, dict)):
                    value = json.dumps(value, ensure_ascii=False, sort_keys=True)
                # Protect spreadsheet users if future reports add free-text fields.
                if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                    value = "'" + value
                safe[key] = value
            writer.writerow(safe)


def new_directory(path, outside=None):
    path = Path(path).resolve()
    if outside is not None and path.is_relative_to(Path(outside).resolve()):
        raise AuditError("Output must be outside the input photo directory.")
    if path.exists():
        raise AuditError("Output already exists; choose a new directory. Nothing was overwritten.")
    path.mkdir(parents=True)
    return path


def contained_file(root, relative):
    if not isinstance(relative, str) or not relative:
        raise AuditError("A non-empty relative file path is required.")
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(root) or not path.is_file():
        raise AuditError("Input file must exist inside its declared photo directory.")
    return path


def load_policy(path=None):
    with Path(path or POLICY_FILE).open(encoding="utf-8") as handle:
        policy = yaml.safe_load(handle)
    if not isinstance(policy, dict) or policy.get("version") != 1:
        raise AuditError("Unsupported audit policy.")
    if policy.get("locale", "zh-CN") != "zh-CN":
        raise AuditError("This workflow currently supports locale zh-CN only.")
    timezone_name = policy.get("timezone", "Asia/Shanghai")
    if not isinstance(timezone_name, str) or not timezone_name:
        raise AuditError("A valid IANA timezone is required.")
    local_timezone(timezone_name)
    if policy.get("network_mode", "offline") not in {"offline", "local-only"}:
        raise AuditError("network_mode must be offline or local-only.")
    limits = {"near_duplicate_hamming": (0, 16), "max_images": (1, 10000),
              "max_pixels": (1, 100000000), "max_file_mb": (1, 1000),
              "post_time_window_hours": (1, 168)}
    for key, (low, high) in limits.items():
        if type(policy.get(key)) is not int or not low <= policy[key] <= high:
            raise AuditError("Invalid audit policy limit.")
    scores = policy.get("metadata_scores", {})
    for key in ("gps", "serial", "timestamp", "device", "opaque_metadata"):
        if type(scores.get(key)) is not int or not 0 <= scores[key] <= 4:
            raise AuditError("Metadata scores must be integers between 0 and 4.")
    return policy


def provenance(policy=None):
    code = {p.name: sha256(p) for p in sorted(Path(__file__).parent.glob("*.py"))}
    policy = policy or {}
    created_utc = utc_now()
    timezone_name = policy.get("timezone", "Asia/Shanghai")
    return {"tool_version": __version__, "python": platform.python_version(),
            "platform": platform.platform(), "locale": policy.get("locale", "zh-CN"),
            "timezone": timezone_name, "network_mode": policy.get("network_mode", "offline"),
            "created_at_utc": created_utc.isoformat(),
            "created_at_local": created_utc.astimezone(local_timezone(timezone_name)).isoformat(),
            "dependencies": {p: _installed_version(p) for p in ("Pillow", "PyYAML", "tzdata")
                             if _installed_version(p)},
            "code_sha256": digest(code), "policy_sha256": digest(policy)}


def _installed_version(package):
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None

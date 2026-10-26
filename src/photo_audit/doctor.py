"""Offline runtime diagnostics for mainland China deployments."""
import importlib.metadata
import json
import os
import platform
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from .common import AuditError, load_policy, local_timezone


def _dependency(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def run(policy_path=None):
    policy = load_policy(policy_path)
    checks = {
        "python": {"ok": sys.version_info >= (3, 11), "version": platform.python_version()},
        "locale": {"ok": policy.get("locale") == "zh-CN", "value": policy.get("locale")},
        "timezone": {"ok": False, "value": policy.get("timezone")},
        "network_mode": {"ok": policy.get("network_mode") in {"offline", "local-only"},
                         "value": policy.get("network_mode")},
        "dependencies": {name: _dependency(name) for name in ("Pillow", "PyYAML", "tzdata")},
        "exiftool": {"ok": bool(shutil.which("exiftool")), "optional": True},
        "unicode_paths": {"ok": False},
    }
    try:
        tz = local_timezone(policy.get("timezone", "Asia/Shanghai"))
        checks["timezone"]["ok"] = datetime.now(tz).utcoffset().total_seconds() == 8 * 3600
    except AuditError:
        checks["timezone"]["ok"] = False
    with tempfile.TemporaryDirectory(prefix="photo-audit-") as temp:
        path = Path(temp) / "中国照片-测试.txt"
        path.write_text("UTF-8\n", encoding="utf-8")
        checks["unicode_paths"]["ok"] = path.read_text(encoding="utf-8") == "UTF-8\n"
    checks["dependencies_ok"] = all(checks["dependencies"].values())
    checks["required_ok"] = all(value.get("ok", True) for key, value in checks.items()
                                 if key not in {"exiftool", "dependencies", "dependencies_ok", "required_ok"}) and checks["dependencies_ok"]
    checks["environment"] = {"python_executable": str(Path(sys.executable).resolve()),
                              "platform": platform.platform(),
                              "proxy_configured": bool(os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY"))}
    return checks


def main(policy_path=None):
    checks = run(policy_path)
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if checks["required_ok"] else 2

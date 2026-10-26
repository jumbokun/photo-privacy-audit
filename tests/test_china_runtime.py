from datetime import datetime
from pathlib import Path

from photo_audit.common import load_policy, local_timezone, provenance
from photo_audit.doctor import run


def test_china_policy_uses_east_eight_and_utf8_runtime():
    policy = load_policy(Path(__file__).parents[1] / "config" / "audit-policy.yaml")
    assert policy["locale"] == "zh-CN"
    assert policy["network_mode"] == "offline"
    tz = local_timezone(policy["timezone"])
    assert datetime.now(tz).utcoffset().total_seconds() == 8 * 3600
    record = provenance(policy)
    assert record["created_at_local"].endswith("+08:00")


def test_china_doctor_has_no_network_probe():
    checks = run(Path(__file__).parents[1] / "config" / "audit-policy.yaml")
    assert checks["required_ok"] is True
    assert checks["network_mode"]["value"] == "offline"
    assert checks["unicode_paths"]["ok"] is True

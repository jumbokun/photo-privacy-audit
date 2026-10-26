import pytest

from photo_audit.common import AuditError
from photo_audit.vision_evidence import validate_record


def test_vision_evidence_requires_visible_clues_only():
    rows = validate_record({
        "asset_id": "a", "model_id": "local-vlm@sha",
        "observations": [{"category": "text_or_logo", "fact": "A sign is visible", "confidence": "medium"}],
    })
    assert rows[0]["verified"] is False


def test_vision_evidence_rejects_geolocation_fields():
    with pytest.raises(AuditError):
        validate_record({"asset_id": "a", "model_id": "bad", "latitude": 1,
                         "observations": []})

import pytest

from photo_audit.common import AuditError
from photo_audit.ocr import _validate_score


def test_ocr_score_validation():
    assert _validate_score("0.75") == pytest.approx(0.75)
    with pytest.raises(AuditError):
        _validate_score("1.5")

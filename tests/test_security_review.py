from pathlib import Path

import pytest
from PIL import Image

from photo_audit import sanitize
from photo_audit.benchmark import evaluate
from photo_audit.cli import _assign_asset_ids, _image_files
from photo_audit.common import AuditError
from photo_audit.sanitize import strip_metadata
from photo_audit.visual import FIELDS, validate_checklist


def _write_checklist(path, rows):
    path.write_text(",".join(FIELDS) + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


def test_duplicate_content_gets_stable_distinct_asset_ids():
    scanned = [
        {"relative_path": "b.jpg", "scan": {"sha256": "a" * 64}},
        {"relative_path": "a.jpg", "scan": {"sha256": "a" * 64}},
    ]
    _assign_asset_ids(scanned)
    ids = [row["asset_id"] for row in scanned]
    assert len(set(ids)) == 2
    assert all(value.startswith("a" * 16 + "-") for value in ids)

    rerun = [
        {"relative_path": "a.jpg", "scan": {"sha256": "a" * 64}},
        {"relative_path": "b.jpg", "scan": {"sha256": "a" * 64}},
    ]
    _assign_asset_ids(rerun)
    assert {row["relative_path"]: row["asset_id"] for row in scanned} == {
        row["relative_path"]: row["asset_id"] for row in rerun
    }


def test_image_scan_rejects_symlink_outside_root(tmp_path):
    source = tmp_path / "source"
    outside = tmp_path / "outside.jpg"
    source.mkdir()
    Image.new("RGB", (2, 2)).save(outside)
    link = source / "link.jpg"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this platform")
    with pytest.raises(AuditError, match="symbolic links"):
        _image_files(source)


def test_summary_join_rejects_duplicate_and_unexpected_checklist_ids(tmp_path):
    row = [""] * len(FIELDS)
    row[0], row[3] = "a", "pending"
    path = tmp_path / "visual.csv"
    _write_checklist(path, [",".join(row), ",".join(row)])
    with pytest.raises(ValueError, match="unique"):
        validate_checklist(path, expected_asset_ids={"a"})

    _write_checklist(path, [",".join(row)])
    with pytest.raises(ValueError, match="match metadata_report"):
        validate_checklist(path, expected_asset_ids={"b"})


def test_sanitize_rechecks_source_hash(monkeypatch, tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    image = source / "photo.jpg"
    Image.new("RGB", (4, 4), (10, 20, 30)).save(image, format="JPEG")

    calls = iter(["original", "changed", "sanitized"])
    monkeypatch.setattr(sanitize, "sha256", lambda _path: next(calls))
    with pytest.raises(AuditError, match="changed during sanitization"):
        strip_metadata(source, output, "photo.jpg")
    assert not (output / "photo.jpg").exists()


def test_missing_predictions_count_as_hit_rate_misses():
    rows = [{"distance_km": None}, {"distance_km": 0.1}]
    summary = evaluate(rows, bootstrap_iterations=20)
    assert summary["failure_rate"] == pytest.approx(0.5)
    assert summary["hit_rate_0_2km"] == pytest.approx(0.5)

import csv
import json
from pathlib import Path

import pytest
from PIL import Image
from PIL.TiffImagePlugin import IFDRational

from photo_audit.benchmark import evaluate, haversine_km
from photo_audit.common import AuditError, load_policy
from photo_audit.correlation import correlate
from photo_audit.metadata import inspect_image
from photo_audit.sanitize import strip_metadata


def image_with_gps(path):
    image = Image.new("RGB", (32, 24), (40, 80, 120))
    exif = image.getexif()
    exif[34853] = {2: (IFDRational(50, 1), IFDRational(0, 1), IFDRational(0, 1)),
                   4: (IFDRational(8, 1), IFDRational(0, 1), IFDRational(0, 1))}
    exif[36867] = "2026:10:26 12:00:00"
    exif[272] = "Synthetic Camera"
    image.save(path, format="JPEG", exif=exif.tobytes())


def test_metadata_reports_presence_without_values(tmp_path):
    source = tmp_path / "input"
    source.mkdir()
    path = source / "private.jpg"
    image_with_gps(path)
    row = inspect_image(path, load_policy(Path(__file__).parents[1] / "config" / "audit-policy.yaml"))
    assert row["gps"] is True
    assert row["timestamp"] is True
    assert row["device"] is True
    assert "latitude" not in row
    assert "longitude" not in row


def test_correlation_finds_exact_and_near_matches():
    metadata = [
        {"asset_id": "a", "sha256": "same", "dhash": "0000000000000000"},
        {"asset_id": "b", "sha256": "same", "dhash": "0000000000000001"},
        {"asset_id": "c", "sha256": "different", "dhash": "0000000000000003"},
    ]
    manifest = [
        {"asset_id": "a", "post_id": "p1", "post_timestamp_parsed": None},
        {"asset_id": "b", "post_id": "p2", "post_timestamp_parsed": None},
        {"asset_id": "c", "post_id": "p3", "post_timestamp_parsed": None},
    ]
    rows = correlate(metadata, manifest, threshold=2, window_hours=24)
    first = next(row for row in rows if row["asset_id"] == "a")
    assert first["exact_match_asset_ids"] == ["a", "b"]
    assert first["near_match_asset_ids"] == ["b", "c"]
    assert first["cross_post_risk"] >= 3


def test_sanitize_writes_new_copy_without_exif(tmp_path):
    source = tmp_path / "input"
    output = tmp_path / "output"
    source.mkdir()
    path = source / "private.jpg"
    image_with_gps(path)
    record = strip_metadata(source, output, "private.jpg")
    assert path.exists()
    assert record["source_sha256"] != record["sanitized_sha256"]
    with Image.open(output / "private.jpg") as clean:
        assert not clean.getexif()


def test_benchmark_metrics_are_aggregate():
    assert haversine_km(0, 0, 0, 0) == 0
    rows = [{"distance_km": 0.1}, {"distance_km": 2.0}, {"distance_km": 30.0}]
    summary = evaluate(rows)
    assert summary["count"] == 3
    assert summary["hit_rate_0_2km"] == pytest.approx(1 / 3)
    assert summary["hit_rate_1_0km"] == pytest.approx(1 / 3)

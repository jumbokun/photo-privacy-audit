import csv
import json
from datetime import datetime, timezone

from photo_audit.correlation import correlate
from photo_audit.visual import FIELDS, aggregate_risk, validate_checklist


def test_rotation_aware_near_match_carries_time_evidence():
    metadata = [
        {"asset_id": "a", "sha256": "a", "dhash": "ffffffffffffffff",
         "dhash_rotations": json.dumps(["ffffffffffffffff"]),
         "phash": "0000000000000000", "phash_rotations": json.dumps(["0000000000000000"]),
         "hash_informative": "True"},
        {"asset_id": "b", "sha256": "b", "dhash": "0000000000000000",
         "dhash_rotations": json.dumps(["1111111111111111", "fffffffffffffffe"]),
         "phash": "0000000000000000", "phash_rotations": json.dumps(["0000000000000000"]),
         "hash_informative": "True"},
    ]
    manifest = [
        {"asset_id": "a", "post_id": "p1", "post_timestamp_parsed": datetime(2026, 1, 1, 12, tzinfo=timezone.utc)},
        {"asset_id": "b", "post_id": "p2", "post_timestamp_parsed": datetime(2026, 1, 1, 12, 30, tzinfo=timezone.utc)},
    ]
    row = correlate(metadata, manifest, threshold=2, window_hours=2)[0]
    assert row["near_match_asset_ids"] == ["b"]
    assert row["time_link"] is True
    assert row["cross_post_risk"] == 3
    assert "time window" in row["evidence"]


def test_pending_risk_is_not_zero_and_reviewed_risk_is_reproducible(tmp_path):
    pending = {"review_status": "pending"}
    assert aggregate_risk(pending)["overall_risk"] is None
    row = {**{field: "0" for field in FIELDS}, "review_status": "reviewed"}
    row.update({"evidence_level": "observed", "risk_basis": "No sensitive visual clue observed.",
                "reviewer_id": "reviewer-1", "reviewed_at": "2026-01-01T12:00:00+00:00",
                "metadata_risk": "4", "visual_risk": "1", "cross_post_risk": "0", "temporal_risk": "0"})
    aggregate = aggregate_risk(row)
    assert aggregate["overall_risk"] == 4
    assert aggregate["risk_weighted"] == 1.5
    path = tmp_path / "visual.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerow({**row, "asset_id": "asset-1", "overall_risk": aggregate["overall_risk"],
                         "risk_weighted": aggregate["risk_weighted"]})
    assert len(validate_checklist(path, expected_asset_ids={"asset-1"})) == 1

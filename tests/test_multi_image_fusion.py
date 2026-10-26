import json

import pytest

from photo_audit.common import AuditError
from photo_audit.cli import main
from photo_audit.multi_image_fusion import (
    cluster_candidates,
    fuse_multiview,
    haversine_km,
    load_candidates,
    normalise_candidates,
)


def test_haversine_handles_antimeridian_and_cluster_does_not_use_planar_lon():
    assert haversine_km(0, 179.9, 0, -179.9) < 25
    rows = normalise_candidates([
        {"image_id": "a", "candidate_id": "a1", "lat": 0, "lon": 179.9, "score": 1},
        {"image_id": "b", "candidate_id": "b1", "lat": 0, "lon": -179.9, "score": 1},
    ])
    assert len(cluster_candidates(rows, radius_km=25)) == 1


def test_fusion_normalises_each_image_and_returns_a_range(tmp_path):
    report = fuse_multiview([
        {"image_id": "street", "candidate_id": "s1", "lat": 48.2083, "lon": 16.3731,
         "score": 0.95, "source": "retrieval", "evidence": "bridge match"},
        {"image_id": "street", "candidate_id": "s2", "lat": 48.4, "lon": 16.9,
         "score": 0.5, "source": "vision"},
        {"image_id": "park", "candidate_id": "p1", "lat": 48.2085, "lon": 16.3734,
         "score": 0.9, "source": "retrieval", "uncertainty_km": 0.2},
    ], cluster_radius_km=1, top_k=3)
    assert report["status"] == "candidate"
    assert report["verified"] is False
    assert report["image_count"] == 2
    assert report["clusters"][0]["support_image_count"] == 2
    assert report["clusters"][0]["radius_km"] < 1
    assert report["clusters"][0]["center_lat"] == pytest.approx(48.2084, abs=0.001)
    assert "address_lookup" in report["method"]

    output = tmp_path / "fusion.json"
    output.write_text("{}", encoding="utf-8")
    with pytest.raises(FileExistsError):
        # The common write helper is intentionally create-only.
        from photo_audit.multi_image_fusion import write_fusion_report
        write_fusion_report(output, report)


def test_conflict_is_explicit_when_two_images_support_far_apart_clusters():
    report = fuse_multiview([
        {"image_id": "a", "candidate_id": "a1", "lat": 48.2, "lon": 16.37, "score": 0.95},
        {"image_id": "b", "candidate_id": "b1", "lat": 50.1, "lon": 8.68, "score": 0.94},
    ], cluster_radius_km=2, conflict_distance_km=10)
    assert report["status"] == "conflict"
    assert report["conflict"]["detected"] is True
    assert report["conflict"]["pairs"]
    assert len(report["clusters"]) == 2


def test_validation_rejects_bad_score_and_zero_weight_image():
    with pytest.raises(AuditError):
        normalise_candidates([{"image_id": "x", "lat": 0, "lon": 0, "score": 1.1}])
    with pytest.raises(AuditError):
        normalise_candidates([{"image_id": "x", "lat": 0, "lon": 0, "score": 0}])


def test_load_candidates_accepts_json_object_and_jsonl(tmp_path):
    rows = [{"image_id": "a", "lat": 1, "lon": 2, "score": 0.8}]
    json_path = tmp_path / "candidates.json"
    json_path.write_text(json.dumps({"candidates": rows}), encoding="utf-8")
    assert load_candidates(json_path) == rows
    jsonl_path = tmp_path / "candidates.jsonl"
    jsonl_path.write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
    assert load_candidates(jsonl_path) == rows


def test_fuse_candidates_cli_writes_candidate_only_report(tmp_path):
    source = tmp_path / "candidates.json"
    source.write_text(json.dumps({"candidates": [
        {"image_id": "one", "lat": 48.2083, "lon": 16.3731, "score": 0.9},
        {"image_id": "two", "lat": 48.2084, "lon": 16.3732, "score": 0.9},
    ]}), encoding="utf-8")
    output = tmp_path / "reports" / "fusion.json"
    assert main(["fuse-candidates", str(source), str(output), "--cluster-radius", "1"]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "candidate"
    assert report["verified"] is False
    assert report["clusters"][0]["support_image_count"] == 2


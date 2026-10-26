import csv

import pytest

from photo_audit.benchmark import (
    THRESHOLDS_KM,
    build_run_manifest,
    evaluate,
    leakage_report,
    load_predictions,
)
from photo_audit.common import AuditError


def _write(path, fields, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_loader_validates_truth_even_when_prediction_is_blank(tmp_path):
    path = tmp_path / "predictions.csv"
    _write(path, ["asset_id", "true_lat", "true_lon", "pred_lat", "pred_lon"], [
        {"asset_id": "q-1", "true_lat": "91", "true_lon": "0", "pred_lat": "", "pred_lon": ""},
    ])
    with pytest.raises(AuditError, match="true_lat"):
        load_predictions(path)


def test_failed_predictions_count_as_hit_rate_misses(tmp_path):
    path = tmp_path / "predictions.csv"
    _write(path, ["asset_id", "true_lat", "true_lon", "pred_lat", "pred_lon"], [
        {"asset_id": "q-1", "true_lat": "0", "true_lon": "0", "pred_lat": "0", "pred_lon": "0"},
        {"asset_id": "q-2", "true_lat": "0", "true_lon": "0", "pred_lat": "", "pred_lon": ""},
    ])
    summary = evaluate(load_predictions(path), bootstrap_iterations=25)
    assert summary["failure_rate"] == pytest.approx(0.5)
    assert summary["hit_rate_0_2km"] == pytest.approx(0.5)
    assert summary["predicted_count"] == 1
    assert len(summary["thresholds_km"]) >= 6
    assert summary["topk_metrics"] == {}


def test_split_leakage_and_group_metrics_are_reported(tmp_path):
    digest = "a" * 64
    path = tmp_path / "predictions.csv"
    fields = ["asset_id", "true_lat", "true_lon", "pred_lat", "pred_lon", "split", "source_sha256", "region"]
    _write(path, fields, [
        {"asset_id": "q-1", "true_lat": "0", "true_lon": "0", "pred_lat": "0", "pred_lon": "0", "split": "train", "source_sha256": digest, "region": "north"},
        {"asset_id": "q-2", "true_lat": "0", "true_lon": "0", "pred_lat": "1", "pred_lon": "1", "split": "test", "source_sha256": digest, "region": "south"},
    ])
    rows = load_predictions(path)
    summary = evaluate(rows, bootstrap_iterations=25)
    assert summary["group_key"] == "region"
    assert set(summary["group_metrics"]) == {"north", "south"}
    assert summary["leakage"]["ok"] is False
    assert summary["leakage"]["violations"][0]["field"] == "source_sha256"
    manifest = build_run_manifest(path, rows, "dataset-v1", "model-v1", "abc123", summary)
    assert manifest["coordinates_included"] is False
    assert manifest["prediction_file_sha256"]
    assert manifest["group_counts"] == {"north": 1, "south": 1}


def test_missing_split_is_explicitly_unavailable():
    rows = [{"distance_km": 1.0}]
    report = leakage_report(rows)
    assert report["available"] is False


def test_confidence_calibration_is_reported_when_supplied(tmp_path):
    path = tmp_path / "predictions.csv"
    _write(path, ["asset_id", "true_lat", "true_lon", "pred_lat", "pred_lon", "confidence"], [
        {"asset_id": "q-1", "true_lat": "0", "true_lon": "0", "pred_lat": "0", "pred_lon": "0", "confidence": "0.9"},
        {"asset_id": "q-2", "true_lat": "0", "true_lon": "0", "pred_lat": "", "pred_lon": "", "confidence": "0.1"},
    ])
    summary = evaluate(load_predictions(path), bootstrap_iterations=25)
    assert summary["calibration"]["available"] is True
    assert 0 <= summary["calibration"]["brier"] <= 1


def test_topk_candidates_produce_recall_at_k(tmp_path):
    path = tmp_path / "predictions.csv"
    _write(path, ["asset_id", "true_lat", "true_lon", "pred_lat", "pred_lon", "topk_json"], [
        {"asset_id": "q-1", "true_lat": "0", "true_lon": "0", "pred_lat": "0", "pred_lon": "0",
         "topk_json": "[[20, 20], [0, 0]]"},
    ])
    summary = evaluate(load_predictions(path), bootstrap_iterations=25)
    assert summary["topk_metrics"]["1"]["hit_rate_0_2km"] == 0
    assert summary["topk_metrics"]["5"]["hit_rate_0_2km"] == 1

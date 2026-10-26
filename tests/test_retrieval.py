import csv
import json

import pytest

from photo_audit.common import AuditError
from photo_audit.cli import main
from photo_audit.retrieval import (
    ReferenceIndex,
    grid_id,
    load_reference_manifest,
    retrieve_candidates,
    stratified_sample_manifest,
)


def _write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_local_reference_retrieval_is_ranked_and_explainable(tmp_path):
    manifest = tmp_path / "references.csv"
    references = tmp_path / "reference_embeddings.csv"
    queries = tmp_path / "query_embeddings.csv"
    _write_csv(manifest, ["asset_id", "lat", "lon", "source", "dataset"], [
        {"asset_id": "r-a", "lat": "48.2000", "lon": "16.3700", "source": "approved-set", "dataset": "v1"},
        {"asset_id": "r-b", "lat": "48.2100", "lon": "16.3800", "source": "approved-set", "dataset": "v1"},
        {"asset_id": "r-c", "lat": "50.1000", "lon": "8.6000", "source": "approved-set", "dataset": "v1"},
    ])
    fields = ["asset_id", "model_id", "vector"]
    _write_csv(references, fields, [
        {"asset_id": "r-a", "model_id": "encoder@sha", "vector": json.dumps([1.0, 0.0])},
        {"asset_id": "r-b", "model_id": "encoder@sha", "vector": json.dumps([0.8, 0.6])},
        {"asset_id": "r-c", "model_id": "encoder@sha", "vector": json.dumps([0.0, 1.0])},
    ])
    _write_csv(queries, fields, [
        {"asset_id": "q-1", "model_id": "encoder@sha", "vector": json.dumps([1.0, 0.0])},
    ])

    rows = retrieve_candidates(queries, manifest, references, top_k=2)
    assert [row["candidate_asset_id"] for row in rows] == ["r-a", "r-b"]
    assert rows[0]["candidate_rank"] == 1
    assert rows[0]["candidate_lat"] == "48.20000000"
    assert rows[0]["grid_id"] == grid_id(48.2, 16.37)
    assert "cosine=1.00000000" in rows[0]["evidence"]
    assert rows[0]["match_status"] == "candidate"


def test_radius_and_grid_filters_are_applied_before_ranking(tmp_path):
    manifest = tmp_path / "references.csv"
    refs = tmp_path / "refs.csv"
    queries = tmp_path / "queries.csv"
    _write_csv(manifest, ["asset_id", "lat", "lon"], [
        {"asset_id": "near", "lat": "48.2000", "lon": "16.3700"},
        {"asset_id": "far", "lat": "50.1000", "lon": "8.6000"},
    ])
    fields = ["asset_id", "model_id", "vector"]
    _write_csv(refs, fields, [
        {"asset_id": "near", "model_id": "m", "vector": "[0.9, 0.1]"},
        {"asset_id": "far", "model_id": "m", "vector": "[1.0, 0.0]"},
    ])
    _write_csv(queries, fields, [{"asset_id": "q", "model_id": "m", "vector": "[1.0, 0.0]"}])

    rows = retrieve_candidates(queries, manifest, refs, top_k=5,
                               center_lat=48.2, center_lon=16.37, radius_km=5)
    assert [row["candidate_asset_id"] for row in rows] == ["near"]
    assert float(rows[0]["distance_to_filter_center_km"]) < 0.01
    assert "radius:5km@48.200000,16.370000" in rows[0]["spatial_filter"]

    far_grid = grid_id(50.1, 8.6)
    rows = retrieve_candidates(queries, manifest, refs, top_k=5, grid_ids=[far_grid])
    assert [row["candidate_asset_id"] for row in rows] == ["far"]
    assert "grid:" + far_grid in rows[0]["spatial_filter"]


def test_no_candidate_result_is_explicit_and_model_mismatch_rejected(tmp_path):
    manifest = tmp_path / "references.csv"
    refs = tmp_path / "refs.csv"
    queries = tmp_path / "queries.csv"
    _write_csv(manifest, ["asset_id", "lat", "lon"], [{"asset_id": "r", "lat": "0", "lon": "0"}])
    fields = ["asset_id", "model_id", "vector"]
    _write_csv(refs, fields, [{"asset_id": "r", "model_id": "ref-model", "vector": "[1, 0]"}])
    _write_csv(queries, fields, [{"asset_id": "q", "model_id": "query-model", "vector": "[1, 0]"}])
    with pytest.raises(AuditError, match="same model"):
        retrieve_candidates(queries, manifest, refs)

    _write_csv(queries, fields, [{"asset_id": "q", "model_id": "ref-model", "vector": "[1, 0]"}])
    rows = retrieve_candidates(queries, manifest, refs, center_lat=0, center_lon=0, radius_km=0)
    assert rows[0]["match_status"] == "candidate"
    rows = retrieve_candidates(queries, manifest, refs, grid_ids=["g999:999"])
    assert rows[0]["match_status"] == "no_candidates"
    assert rows[0]["candidate_rank"] == 0


def test_manifest_rejects_invalid_authorization_and_coordinates(tmp_path):
    path = tmp_path / "manifest.csv"
    _write_csv(path, ["asset_id", "lat", "lon", "authorization_status"], [
        {"asset_id": "r", "lat": "1", "lon": "2", "authorization_status": "private"},
    ])
    with pytest.raises(AuditError, match="authorization_status"):
        load_reference_manifest(path)
    _write_csv(path, ["asset_id", "lat", "lon"], [{"asset_id": "r", "lat": "91", "lon": "2"}])
    with pytest.raises(AuditError, match="latitude"):
        load_reference_manifest(path)


def test_grid_rerank_can_override_one_embedding_outlier(tmp_path):
    manifest = tmp_path / "references.csv"
    refs = tmp_path / "refs.csv"
    queries = tmp_path / "queries.csv"
    _write_csv(manifest, ["asset_id", "lat", "lon"], [
        {"asset_id": "a-hi", "lat": "48.2000", "lon": "16.3700"},
        {"asset_id": "a-low", "lat": "48.2001", "lon": "16.3701"},
        {"asset_id": "b-1", "lat": "48.3000", "lon": "16.4700"},
        {"asset_id": "b-2", "lat": "48.3001", "lon": "16.4701"},
    ])
    fields = ["asset_id", "model_id", "vector"]
    _write_csv(refs, fields, [
        {"asset_id": "a-hi", "model_id": "m", "vector": "[1, 0]"},
        {"asset_id": "a-low", "model_id": "m", "vector": "[0.1, 0.995]"},
        {"asset_id": "b-1", "model_id": "m", "vector": "[0.95, 0.312]"},
        {"asset_id": "b-2", "model_id": "m", "vector": "[0.95, 0.312]"},
    ])
    _write_csv(queries, fields, [{"asset_id": "q", "model_id": "m", "vector": "[1, 0]"}])
    embedding = retrieve_candidates(queries, manifest, refs, top_k=1, rerank="embedding")
    grid = retrieve_candidates(queries, manifest, refs, top_k=1, rerank="grid")
    assert embedding[0]["candidate_asset_id"] == "a-hi"
    assert grid[0]["candidate_asset_id"] == "b-1"


def test_strict_research_manifest_requires_authorization_status(tmp_path):
    path = tmp_path / "manifest.csv"
    _write_csv(path, ["asset_id", "lat", "lon"], [{"asset_id": "r", "lat": "1", "lon": "2"}])
    with pytest.raises(AuditError, match="requires authorization_status"):
        load_reference_manifest(path, require_authorization=True)


def test_public_api_manifest_is_allowed_for_strict_research(tmp_path):
    path = tmp_path / "manifest.csv"
    _write_csv(path, ["asset_id", "lat", "lon", "authorization_status"],
               [{"asset_id": "r", "lat": "1", "lon": "2", "authorization_status": "public_api"}])
    assert load_reference_manifest(path, require_authorization=True)[0]["asset_id"] == "r"


def test_cli_writes_report_and_redaction_safe_provenance_sidecar(tmp_path):
    manifest = tmp_path / "references.csv"
    refs = tmp_path / "refs.csv"
    queries = tmp_path / "queries.csv"
    _write_csv(manifest, ["asset_id", "lat", "lon", "authorization_status"],
               [{"asset_id": "r", "lat": "1", "lon": "2", "authorization_status": "approved"}])
    fields = ["asset_id", "model_id", "vector"]
    _write_csv(refs, fields, [{"asset_id": "r", "model_id": "m", "vector": "[1, 0]"}])
    _write_csv(queries, fields, [{"asset_id": "q", "model_id": "m", "vector": "[1, 0]"}])
    output = tmp_path / "candidates.csv"
    assert main(["reference-retrieve", str(queries), str(manifest), str(refs), str(output),
                 "--require-authorization"]) == 0
    assert output.exists()
    sidecar = output.with_name(output.name + ".provenance.json")
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "reference-retrieval/v1"
    assert payload["coordinates_included"] is True
    assert "lat" not in payload and "lon" not in payload


def test_stratified_sampling_is_deterministic_and_balances_provider_grid():
    rows = [
        {"asset_id": f"a-{index}", "provider": "provider-a", "grid_id": "cell-a"}
        for index in range(6)
    ] + [
        {"asset_id": f"b-{index}", "provider": "provider-b", "grid_id": "cell-b"}
        for index in range(6)
    ]
    first = stratified_sample_manifest(rows, 4, seed=7)
    second = stratified_sample_manifest(rows, 4, seed=7)
    assert [row["asset_id"] for row in first] == [row["asset_id"] for row in second]
    assert {row["provider"] for row in first} == {"provider-a", "provider-b"}
    assert {row["grid_id"] for row in first} == {"cell-a", "cell-b"}
    with pytest.raises(AuditError, match="positive integer"):
        stratified_sample_manifest(rows, 0)


def test_reference_provider_and_license_provenance_are_retained(tmp_path):
    manifest = tmp_path / "references.csv"
    refs = tmp_path / "refs.csv"
    queries = tmp_path / "queries.csv"
    _write_csv(manifest, ["asset_id", "lat", "lon", "provider", "provider_image_id",
                          "capture_date", "source_url", "license_url", "license_provenance",
                          "authorization_status"], [{
        "asset_id": "r", "lat": "1", "lon": "2", "provider": "street-provider",
        "provider_image_id": "pano-1", "capture_date": "2025-01-02",
        "source_url": "https://example.invalid/r", "license_url": "https://example.invalid/license",
        "license_provenance": "provider-export:v1", "authorization_status": "authorized",
    }])
    fields = ["asset_id", "model_id", "vector"]
    _write_csv(refs, fields, [{"asset_id": "r", "model_id": "m", "vector": "[1, 0]"}])
    _write_csv(queries, fields, [{"asset_id": "q", "model_id": "m", "vector": "[1, 0]"}])
    row = retrieve_candidates(queries, manifest, refs)[0]
    assert row["reference_provider"] == "street-provider"
    assert row["reference_provider_image_id"] == "pano-1"
    assert row["reference_license_provenance"] == "provider-export:v1"


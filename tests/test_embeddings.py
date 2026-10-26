import json

from photo_audit.embeddings import correlate_embeddings


def test_embedding_correlation_is_deterministic_and_excludes_self():
    rows = [
        {"asset_id": "a", "model_id": "encoder@sha", "vector": [1.0, 0.0]},
        {"asset_id": "b", "model_id": "encoder@sha", "vector": [0.99, 0.01]},
        {"asset_id": "c", "model_id": "encoder@sha", "vector": [0.0, 1.0]},
    ]
    result = correlate_embeddings(rows, threshold=0.9, top_k=2)
    assert [(row["asset_id"], row["match_asset_id"]) for row in result] == [("a", "b"), ("b", "a")]
    assert all(row["asset_id"] != row["match_asset_id"] for row in result)

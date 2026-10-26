"""Local embedding correlation for high-recall cross-post review.

Embeddings are supplied by a local encoder and never generated or uploaded here.
This keeps model choice, checkpoint provenance, and privacy boundaries explicit.
"""
import csv
import json
import math
from pathlib import Path

from .common import AuditError, write_csv

REQUIRED = {"asset_id", "model_id", "vector"}
FIELDS = ["asset_id", "match_asset_id", "cosine_similarity", "model_id", "rank", "evidence"]


def _vector(value):
    try:
        data = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        raise AuditError("Embedding vector is not valid JSON.") from None
    if not isinstance(data, list) or not data or len(data) > 16384:
        raise AuditError("Embedding vector must be a non-empty list of at most 16384 values.")
    try:
        vector = [float(item) for item in data]
    except (TypeError, ValueError):
        raise AuditError("Embedding vector contains a non-numeric value.") from None
    if not all(math.isfinite(item) for item in vector):
        raise AuditError("Embedding vector contains a non-finite value.")
    norm = math.sqrt(sum(item * item for item in vector))
    if norm == 0:
        raise AuditError("Embedding vector has zero norm.")
    return [item / norm for item in vector]


def load_embeddings(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED - set(reader.fieldnames or [])
        if missing:
            raise AuditError("Embedding file is missing: " + ", ".join(sorted(missing)))
        rows = list(reader)
    if not rows:
        raise AuditError("Embedding file is empty.")
    result = []
    dimensions = {}
    seen = set()
    for row in rows:
        asset_id = row["asset_id"].strip()
        model_id = row["model_id"].strip()
        if not asset_id or not model_id or asset_id in seen:
            raise AuditError("asset_id must be non-empty and unique in embeddings.")
        seen.add(asset_id)
        vector = _vector(row["vector"])
        dimensions.setdefault(model_id, len(vector))
        if dimensions[model_id] != len(vector):
            raise AuditError("All vectors from one model_id must have the same dimension.")
        result.append({"asset_id": asset_id, "model_id": model_id, "vector": vector})
    if len(dimensions) != 1:
        raise AuditError("Embedding correlation requires one model_id per run.")
    return result


def correlate_embeddings(rows, threshold=0.90, top_k=5, block_size=512):
    if not 0.0 <= threshold <= 1.0:
        raise AuditError("Embedding similarity threshold must be between 0 and 1.")
    if not 1 <= top_k <= 100:
        raise AuditError("top_k must be between 1 and 100.")
    if not 1 <= block_size <= 4096:
        raise AuditError("block_size must be between 1 and 4096.")
    model_id = rows[0]["model_id"]
    result = []
    # Block the query side so memory stays bounded for large albums.
    for start in range(0, len(rows), block_size):
        block = rows[start:start + block_size]
        for left in block:
            scores = []
            for right in rows:
                if left["asset_id"] == right["asset_id"]:
                    continue
                score = sum(a * b for a, b in zip(left["vector"], right["vector"]))
                if score >= threshold:
                    scores.append((score, right["asset_id"]))
            scores.sort(key=lambda item: (-item[0], item[1]))
            for rank, (score, asset_id) in enumerate(scores[:top_k], start=1):
                result.append({"asset_id": left["asset_id"], "match_asset_id": asset_id,
                               "cosine_similarity": f"{score:.8f}", "model_id": model_id,
                               "rank": rank, "evidence": "Local embedding candidate; manually verify before treating as the same scene."})
    return result


def write_embedding_report(path, rows):
    write_csv(path, FIELDS, rows)

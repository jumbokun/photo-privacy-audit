"""Local GeoCLIP inference adapter for authorized research runs."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .common import AuditError, write_json


def predict(image_path, top_k=20, offline=True, model_id="VicenteVivanco/geoclip-geolocation"):
    if top_k < 1 or top_k > 1000:
        raise AuditError("GeoCLIP top_k must be between 1 and 1000.")
    if offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        import torch
        from geoclip import GeoCLIP
    except ImportError:
        raise AuditError("GeoCLIP requires the optional geoclip package and torch.") from None
    image_path = Path(image_path).resolve()
    if not image_path.is_file():
        raise AuditError("GeoCLIP image does not exist.")
    try:
        model = GeoCLIP.from_pretrained(model_id).eval()
        if torch.cuda.is_available():
            model = model.cuda()
        coordinates, scores = model.predict(str(image_path), top_k=top_k)
    except Exception as exc:
        raise AuditError(f"GeoCLIP inference failed: {exc}") from None
    return [{"rank": rank, "lat": float(coordinate[0]), "lon": float(coordinate[1]),
             "score": float(score), "source": "geoclip", "image_id": image_path.name,
             "candidate_id": f"{image_path.name}:geoclip:{rank}",
             "evidence": f"GeoCLIP gallery rank {rank}; score is relative, not a probability."}
            for rank, (coordinate, score) in enumerate(zip(coordinates.tolist(), scores.tolist()), 1)]


def write_prediction(path, image_path, rows, model_id="VicenteVivanco/geoclip-geolocation"):
    output = Path(path).resolve()
    if output.exists():
        raise AuditError("GeoCLIP output already exists; choose a new path.")
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, {"schema_version": "geoclip/v1", "model": model_id,
                        "image": str(Path(image_path).resolve()), "verified": False,
                        "coordinates_are_candidates": True, "candidates": rows})

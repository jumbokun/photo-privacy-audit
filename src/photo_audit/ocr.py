"""Local OCR adapter for authorized research or private audit evidence.

RapidOCR is optional and local-only. OCR text is evidence, not a verified
place; entity matching is a separate explicit step against an approved local
manifest.
"""
from __future__ import annotations

import importlib.metadata
import json
import math
from pathlib import Path
from typing import Any

from .common import AuditError, sha256, write_csv, write_json

OCR_FIELDS = ["asset_id", "text", "confidence", "box", "backend", "image_sha256"]


def _version(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _validate_score(value):
    try:
        score = float(value)
    except (TypeError, ValueError):
        raise AuditError("OCR confidence must be numeric") from None
    if not math.isfinite(score) or not 0 <= score <= 1:
        raise AuditError("OCR confidence must be between 0 and 1")
    return score


class RapidOCREngine:
    """Create a local RapidOCR engine without any network or model download."""

    backend = "rapidocr-onnxruntime"

    def __init__(self):
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            raise AuditError("OCR backend requires optional rapidocr_onnxruntime.") from None
        try:
            self._engine = RapidOCR()
        except Exception as exc:
            raise AuditError(f"Could not initialize local OCR backend: {exc}") from None

    def recognize(self, path):
        path = Path(path).resolve()
        if not path.is_file():
            raise AuditError("OCR input image does not exist.")
        try:
            result, _elapsed = self._engine(str(path))
        except Exception as exc:
            raise AuditError(f"Local OCR inference failed: {exc}") from None
        rows = []
        for item in result or []:
            if len(item) < 3:
                continue
            box, text, score = item[0], str(item[1]).strip(), _validate_score(item[2])
            if not text:
                continue
            rows.append({"text": text, "confidence": score, "box": box})
        return rows


def ocr_image(path, asset_id=None, engine=None):
    path = Path(path).resolve()
    engine = engine or RapidOCREngine()
    asset_id = asset_id or sha256(path)[:16]
    return [{"asset_id": asset_id, "text": row["text"], "confidence": row["confidence"],
             "box": row["box"], "backend": engine.backend, "image_sha256": sha256(path)}
            for row in engine.recognize(path)]


def write_ocr_report(path, rows, provenance=None):
    write_csv(path, OCR_FIELDS, rows)
    if provenance is not None:
        write_json(Path(path).with_suffix(Path(path).suffix + ".provenance.json"), provenance)


def ocr_provenance(engine):
    return {"backend": engine.backend, "package": _version("rapidocr_onnxruntime"),
            "network_mode": "offline", "model_source": "package-bundled-local-onnx"}

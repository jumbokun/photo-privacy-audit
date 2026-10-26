"""Validate structured observations from an explicitly local vision model.

This adapter is deliberately not a geolocation interface. It accepts visible
clue observations and leaves verification and risk scoring to the audit record.
"""
import json
from pathlib import Path

from .common import AuditError, write_csv

CATEGORIES = {"text_or_logo", "architecture", "window_reflection", "vehicle_or_person",
              "time_clue", "repeat_background", "other"}
CONFIDENCE = {"low", "medium", "high"}
FORBIDDEN_KEYS = {"lat", "lon", "latitude", "longitude", "address", "coordinates",
                  "location_candidate", "geocode", "reverse_geocode"}
FIELDS = ["asset_id", "model_id", "category", "fact", "confidence", "verified", "source", "notes"]


def _text(value, field, maximum=2000, required=True):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise AuditError(f"vision evidence {field} must be text")
    value = value.strip()
    if required and not value:
        raise AuditError(f"vision evidence {field} must not be blank")
    if len(value) > maximum:
        raise AuditError(f"vision evidence {field} is too long")
    return value


def validate_record(record):
    if not isinstance(record, dict):
        raise AuditError("vision evidence record must be an object")
    if FORBIDDEN_KEYS.intersection(record):
        raise AuditError("vision evidence cannot contain geolocation fields")
    asset_id = _text(record.get("asset_id"), "asset_id", 256)
    model_id = _text(record.get("model_id"), "model_id", 512)
    observations = record.get("observations")
    if not isinstance(observations, list) or len(observations) > 100:
        raise AuditError("vision evidence observations must be a list of at most 100 items")
    rows = []
    for observation in observations:
        if not isinstance(observation, dict) or FORBIDDEN_KEYS.intersection(observation):
            raise AuditError("vision evidence observation has an invalid field")
        category = _text(observation.get("category"), "category", 64)
        if category not in CATEGORIES:
            raise AuditError("vision evidence category is not in the allowed checklist")
        confidence = _text(observation.get("confidence"), "confidence", 16).lower()
        if confidence not in CONFIDENCE:
            raise AuditError("vision evidence confidence must be low, medium, or high")
        source = _text(observation.get("source", "model"), "source", 64)
        fact = _text(observation.get("fact"), "fact")
        notes = _text(observation.get("notes", ""), "notes", 2000, required=False)
        verified = observation.get("verified", False)
        if not isinstance(verified, bool):
            raise AuditError("vision evidence verified must be boolean")
        rows.append({"asset_id": asset_id, "model_id": model_id, "category": category,
                     "fact": fact, "confidence": confidence, "verified": verified,
                     "source": source, "notes": notes})
    return rows


def load_jsonl(path):
    rows = []
    with Path(path).open(encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                raise AuditError(f"vision evidence line {line_number} is not valid JSON") from None
            rows.extend(validate_record(record))
    if not rows:
        raise AuditError("vision evidence file is empty")
    return rows


def write_evidence_report(path, rows):
    write_csv(path, FIELDS, rows)

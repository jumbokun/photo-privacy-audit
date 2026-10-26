"""Structured human evidence for privacy review.

The checklist keeps three layers separate: facts visible in the image,
reviewer inference (the risk basis), and a conservative aggregate score. The
tool never guesses a place from a private photo.
"""
import csv
import math
from datetime import datetime
from pathlib import Path

FIELDS = [
    "asset_id", "post_id", "post_timestamp", "review_status",
    "text_or_logo_fact", "architecture_fact", "window_reflection_fact",
    "vehicle_or_person_fact", "time_clue_fact", "repeat_background_fact",
    "evidence_notes", "evidence_level", "evidence_refs", "risk_basis",
    "reviewer_id", "reviewed_at", "metadata_risk", "visual_risk",
    "cross_post_risk", "temporal_risk", "overall_risk", "risk_weighted",
    "risk_reasons", "recommended_action",
]
CATEGORY_FIELDS = FIELDS[4:10]
SCORE_FIELDS = ("metadata_risk", "visual_risk", "cross_post_risk", "temporal_risk")
EVIDENCE_LEVELS = {"observed", "inferred", "verified", "not_observable"}
RISK_WEIGHTS = {
    "metadata_risk": 0.30,
    "visual_risk": 0.30,
    "cross_post_risk": 0.20,
    "temporal_risk": 0.20,
}


def checklist_rows(metadata_rows):
    return [{"asset_id": row["asset_id"], "review_status": "pending",
             **{field: "" for field in FIELDS if field not in {"asset_id", "review_status"}}}
            for row in metadata_rows]


def _score(value, field):
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{field} must be an integer from 0 to 4")
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be an integer from 0 to 4") from None
    if not 0 <= parsed <= 4:
        raise ValueError(f"{field} must be an integer from 0 to 4")
    return parsed


def aggregate_risk(row):
    """Compute a transparent risk summary for a reviewed checklist row.

    ``overall_risk`` is the maximum component (a conservative guard against
    averaging away a severe exposure). ``risk_weighted`` is retained as a
    secondary, reproducible ordering signal. Pending rows deliberately return
    no score because missing evidence is not evidence of zero risk.
    """
    if str(row.get("review_status", "")).strip().lower() != "reviewed":
        return {"overall_risk": None, "risk_weighted": None, "risk_reasons": ["manual review pending"]}
    scores = {field: _score(row.get(field), field) for field in SCORE_FIELDS}
    weighted = sum(scores[field] * RISK_WEIGHTS[field] for field in SCORE_FIELDS)
    reasons = [f"{field.removesuffix('_risk')}={scores[field]}" for field in SCORE_FIELDS if scores[field] > 0]
    if not reasons:
        reasons = ["all reviewed risk components are 0"]
    return {
        "overall_risk": max(scores.values()),
        "risk_weighted": round(weighted, 3),
        "risk_reasons": reasons,
    }


def _validate_iso_datetime(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return False
    return parsed.tzinfo is not None


def validate_checklist(path, expected_asset_ids=None):
    """Validate a manually edited checklist and, when supplied, its identity set.

    A checklist is a join input for the aggregate summary. Duplicate, missing,
    or unexpected asset IDs would otherwise silently skew the pending count and
    risk totals, so the optional expected set is checked by the summary command.
    """
    path = Path(path)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = set(FIELDS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError("visual checklist is missing required columns")
        rows = list(reader)
    seen_asset_ids = set()
    for row in rows:
        asset_id = str(row.get("asset_id", "")).strip()
        if not asset_id:
            raise ValueError("asset_id must not be blank")
        if asset_id in seen_asset_ids:
            raise ValueError("visual checklist asset_id values must be unique")
        seen_asset_ids.add(asset_id)
        status = row["review_status"].strip().lower()
        if status not in {"pending", "reviewed"}:
            raise ValueError("review_status must be pending or reviewed")
        if status == "reviewed":
            evidence_level = str(row.get("evidence_level", "")).strip().lower()
            if evidence_level not in EVIDENCE_LEVELS:
                raise ValueError("evidence_level must be observed, inferred, verified, or not_observable")
            if not str(row.get("risk_basis", "")).strip():
                raise ValueError("risk_basis is required for reviewed rows")
            if not str(row.get("reviewer_id", "")).strip():
                raise ValueError("reviewer_id is required for reviewed rows")
            if not _validate_iso_datetime(str(row.get("reviewed_at", "")).strip()):
                raise ValueError("reviewed_at must be an ISO-8601 timestamp with timezone")
        for score in SCORE_FIELDS:
            raw = str(row.get(score, "")).strip()
            if not raw:
                if status == "reviewed":
                    raise ValueError(f"{score} must be an integer from 0 to 4")
                continue
            _score(raw, score)
        if status == "reviewed":
            aggregate = aggregate_risk(row)
            for field in ("overall_risk", "risk_weighted"):
                raw = str(row.get(field, "")).strip()
                if raw:
                    try:
                        parsed = float(raw)
                    except ValueError:
                        raise ValueError(f"{field} must be numeric when supplied") from None
                    expected = float(aggregate[field])
                    if not math.isclose(parsed, expected, abs_tol=0.01):
                        raise ValueError(f"{field} does not match the component risk scores")
    if expected_asset_ids is not None and seen_asset_ids != set(expected_asset_ids):
        raise ValueError("visual checklist asset_id values must match metadata_report exactly")
    return rows

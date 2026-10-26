"""Reproducible evaluator for an authorized benchmark prediction CSV.

The benchmark path is intentionally separate from the personal-photo audit. It
only accepts rows with public/approved ground truth and emits aggregate metrics;
coordinates are never copied into reports or manifests.
"""

from __future__ import annotations

import csv
import hashlib
import json
import importlib.metadata
import math
import platform
import random
import re
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .common import AuditError, sha256

REQUIRED = {"asset_id", "true_lat", "true_lon", "pred_lat", "pred_lon"}
# 200 m / 1 km / 25 km are the established workflow thresholds. The wider
# thresholds match the long-range geolocation reporting bands used by current
# research benchmarks and make coarse continent-level performance visible.
# The official scaling-geoloc evaluator reports these bands in addition to the
# wider 2,500 km continent-level band used by this workflow.
THRESHOLDS_KM = (0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 25.0,
                 50.0, 100.0, 200.0, 500.0, 750.0, 1000.0, 2500.0)
DEFAULT_BOOTSTRAP_ITERATIONS = 2000
_IDENTITY_KEYS = ("source_sha256", "image_sha256", "original_sha256", "capture_id", "scene_id")
_GROUP_KEYS = ("group", "region", "location", "country", "city_type", "category", "scene_type")
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
TOP_KS = (1, 5, 20, 100)


def haversine_km(lat1, lon1, lat2, lon2):
    values = (lat1, lon1, lat2, lon2)
    if not all(math.isfinite(v) for v in values):
        raise ValueError("coordinates must be finite")
    if not -90 <= lat1 <= 90 or not -90 <= lat2 <= 90 or not -180 <= lon1 <= 180 or not -180 <= lon2 <= 180:
        raise ValueError("coordinates outside valid ranges")
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


def _number(row, key):
    try:
        value = float(row[key])
    except (KeyError, TypeError, ValueError):
        raise AuditError(f"benchmark value {key} is missing or invalid") from None
    if not math.isfinite(value):
        raise AuditError(f"benchmark value {key} must be finite")
    return value


def _coordinate(row, key):
    value = _number(row, key)
    if key.endswith("lat") and not -90 <= value <= 90:
        raise AuditError(f"benchmark value {key} is outside its valid range")
    if key.endswith("lon") and not -180 <= value <= 180:
        raise AuditError(f"benchmark value {key} is outside its valid range")
    return value


def _text(row, key, *, required=False):
    value = row.get(key, "")
    if value is None:
        value = ""
    value = str(value).strip()
    if required and not value:
        raise AuditError(f"benchmark field {key} must not be blank")
    return value


def _normalise_optional_fields(row):
    """Normalise audit fields while preserving unknown model metadata columns."""
    output = dict(row)
    for key in ("asset_id", "split", *_IDENTITY_KEYS, *_GROUP_KEYS):
        if key in output:
            output[key] = _text(output, key)
    return output


def _confidence(row):
    raw = _text(row, "confidence")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        raise AuditError("benchmark confidence must be a finite number between 0 and 1") from None
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise AuditError("benchmark confidence must be a finite number between 0 and 1")
    return value


def _topk_distances(row, true):
    raw = _text(row, "topk_json")
    if not raw:
        return None
    try:
        candidates = json.loads(raw)
    except json.JSONDecodeError:
        raise AuditError("benchmark topk_json must be valid JSON") from None
    if not isinstance(candidates, list) or not candidates or len(candidates) > 1000:
        raise AuditError("benchmark topk_json must be a non-empty list of at most 1000 candidates")
    distances = []
    for candidate in candidates:
        if isinstance(candidate, dict):
            candidate_row = {"pred_lat": candidate.get("lat"), "pred_lon": candidate.get("lon")}
        elif isinstance(candidate, (list, tuple)) and len(candidate) == 2:
            candidate_row = {"pred_lat": candidate[0], "pred_lon": candidate[1]}
        else:
            raise AuditError("benchmark topk_json candidates must be [lat, lon] or {lat, lon}")
        predicted = (_coordinate(candidate_row, "pred_lat"), _coordinate(candidate_row, "pred_lon"))
        try:
            distances.append(haversine_km(*true, *predicted))
        except ValueError as exc:
            raise AuditError(str(exc)) from None
    return distances


def load_predictions(path):
    """Load and strictly validate a prediction CSV.

    ``split`` and identity columns are optional. When present they enable the
    leakage audit and grouped metrics; blank identity values are rejected so a
    caller cannot accidentally disable the check with an empty column.
    """
    path = Path(path)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        if len(fieldnames) != len(set(fieldnames)):
            raise AuditError("Prediction file contains duplicate column names.")
        missing = REQUIRED - set(fieldnames)
        if missing:
            raise AuditError("Prediction file is missing: " + ", ".join(sorted(missing)))
        rows = list(reader)
    if not rows:
        raise AuditError("Prediction file is empty.")

    enriched = []
    seen_assets = set()
    for row_number, original in enumerate(rows, start=2):
        row = _normalise_optional_fields(original)
        asset_id = _text(row, "asset_id", required=True)
        if asset_id in seen_assets:
            raise AuditError(f"asset_id must be unique in benchmark predictions (row {row_number}).")
        seen_assets.add(asset_id)
        row["asset_id"] = asset_id
        if "confidence" in row:
            row["confidence_value"] = _confidence(row)

        # Validate ground truth even for failed/blank predictions. A failed
        # prediction is a metric miss, not an escape hatch for malformed truth.
        true = (_coordinate(row, "true_lat"), _coordinate(row, "true_lon"))
        row["topk_distances_km"] = _topk_distances(row, true)
        pred_lat_blank = not _text(row, "pred_lat")
        pred_lon_blank = not _text(row, "pred_lon")
        if pred_lat_blank != pred_lon_blank:
            raise AuditError("pred_lat and pred_lon must both be present or both be blank")
        if pred_lat_blank:
            row["distance_km"] = None
        else:
            predicted = (_coordinate(row, "pred_lat"), _coordinate(row, "pred_lon"))
            try:
                row["distance_km"] = haversine_km(*true, *predicted)
            except ValueError as exc:
                raise AuditError(str(exc)) from None
        if row["topk_distances_km"] is None:
            row["topk_distances_km"] = [] if row["distance_km"] is None else [row["distance_km"]]

        # A SHA-256 identity is useful for leakage checks and safe to retain in
        # the input CSV. We only expose a digest of it in the run manifest.
        for key in _IDENTITY_KEYS:
            if key not in row or not row[key]:
                continue
            if key.endswith("sha256") and not _HEX64.fullmatch(row[key]):
                raise AuditError(f"benchmark field {key} must be a 64-character hexadecimal SHA-256")
        if "split" in row and not row["split"]:
            raise AuditError("benchmark field split must not be blank when supplied")
        enriched.append(row)
    return enriched


def calibration_report(rows, threshold_km=25.0, bins=10):
    """Calculate Brier score and equal-width ECE for a hit event.

    Confidence is interpreted as the model's probability that the prediction
    is within ``threshold_km``. Missing confidence makes calibration
    unavailable rather than silently dropping rows.
    """
    if not math.isfinite(threshold_km) or threshold_km <= 0:
        raise AuditError("calibration threshold must be a positive finite number")
    if not rows or not all("confidence_value" in row for row in rows):
        return {"available": False, "reason": "confidence column not supplied"}
    if any(row["confidence_value"] is None for row in rows):
        return {"available": False, "reason": "confidence is missing for one or more rows"}
    outcomes = [float(row["distance_km"] is not None and row["distance_km"] <= threshold_km) for row in rows]
    confidences = [float(row["confidence_value"]) for row in rows]
    brier = sum((confidence - outcome) ** 2 for confidence, outcome in zip(confidences, outcomes)) / len(rows)
    groups = []
    for index in range(bins):
        lower = index / bins
        upper = (index + 1) / bins
        members = [position for position, confidence in enumerate(confidences)
                   if (lower <= confidence < upper) or (index == bins - 1 and confidence == upper)]
        if not members:
            continue
        mean_confidence = sum(confidences[position] for position in members) / len(members)
        observed_rate = sum(outcomes[position] for position in members) / len(members)
        groups.append({"lower": lower, "upper": upper, "count": len(members),
                       "mean_confidence": mean_confidence, "observed_rate": observed_rate,
                       "gap": abs(mean_confidence - observed_rate)})
    ece = sum(group["count"] * group["gap"] for group in groups) / len(rows)
    return {"available": True, "threshold_km": threshold_km, "brier": brier,
            "ece": ece, "bins": groups}


def _bootstrap(values, statistic, seed=42, iterations=DEFAULT_BOOTSTRAP_ITERATIONS):
    """Deterministic percentile bootstrap interval (2.5th and 97.5th)."""
    if len(values) < 2 or iterations < 2:
        return None
    rng = random.Random(seed)
    results = [statistic([rng.choice(values) for _ in values]) for _ in range(iterations)]
    results.sort()
    low = max(0, math.floor(iterations * 0.025))
    high = min(iterations - 1, math.ceil(iterations * 0.975) - 1)
    return [results[low], results[high]]


def _median(values):
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _group_key(rows):
    for key in _GROUP_KEYS:
        if any(str(row.get(key, "")).strip() for row in rows):
            return key
    return None


def _evaluate_subset(rows, seed, iterations):
    distances = [row["distance_km"] for row in rows if row["distance_km"] is not None]
    total = len(rows)
    predicted_count = len(distances)
    summary = {
        "count": total,
        "predicted_count": predicted_count,
        "failure_rate": None if not total else (total - predicted_count) / total,
        "median_km": _median(distances),
        "mean_km": None if not distances else sum(distances) / len(distances),
        "p90_km": None if not distances else sorted(distances)[max(0, math.ceil(len(distances) * 0.90) - 1)],
        "p95_km": None if not distances else sorted(distances)[max(0, math.ceil(len(distances) * 0.95) - 1)],
        "mean_km_ci95": _bootstrap(distances, lambda values: sum(values) / len(values), seed, iterations),
        "median_km_ci95": _bootstrap(distances, _median, seed + 1, iterations),
    }
    # A missing prediction is a miss. The denominator is all queries, which
    # keeps hit rates comparable between systems with different failure rates.
    for threshold_index, threshold in enumerate(THRESHOLDS_KM):
        values = [
            0.0 if row["distance_km"] is None else float(row["distance_km"] <= threshold)
            for row in rows
        ]
        key = f"hit_rate_{str(threshold).replace('.', '_')}km"
        summary[key] = None if not total else sum(values) / total
        summary[key + "_ci95"] = _bootstrap(values, lambda sample: sum(sample) / len(sample), seed + threshold_index + 2, iterations)
    topk_metrics = {}
    if any(bool(str(row.get("topk_json", "")).strip()) for row in rows):
        for k in TOP_KS:
            distances_at_k = [
                min(row["topk_distances_km"][:k]) if row.get("topk_distances_km") else None
                for row in rows
            ]
            valid = [distance for distance in distances_at_k if distance is not None]
            topk = {"k": k, "predicted_count": len(valid),
                    "failure_rate": None if not total else (total - len(valid)) / total,
                    "distance_km": None if not valid else sum(valid) / len(valid)}
            for threshold in (0.2, 1.0, 25.0):
                values = [0.0 if distance is None else float(distance <= threshold) for distance in distances_at_k]
                topk[f"hit_rate_{str(threshold).replace('.', '_')}km"] = None if not total else sum(values) / total
            topk_metrics[str(k)] = topk
    summary["topk_metrics"] = topk_metrics
    return summary


def leakage_report(rows):
    """Return a split-aware identity leakage audit without exposing raw IDs.

    The check is intentionally conservative: it flags the same capture/image
    identity appearing in more than one split. A missing ``split`` column is
    reported as ``available=False`` so a reviewer cannot mistake an unavailable
    check for a clean split.
    """
    if not any("split" in row for row in rows):
        return {"available": False, "ok": None, "reason": "split column not supplied", "violations": []}
    violations = []
    checked_fields = []
    rows_with_identity = set()
    for key in _IDENTITY_KEYS:
        values = {}
        for row in rows:
            value = str(row.get(key, "")).strip()
            split = str(row.get("split", "")).strip()
            if not value or not split:
                continue
            checked_fields.append(key)
            rows_with_identity.add(id(row))
            values.setdefault(value, set()).add(split)
        for value, splits in values.items():
            if len(splits) > 1:
                # Hash the identity before writing the manifest/report.
                digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
                violations.append({"field": key, "identity_digest": digest, "splits": sorted(splits)})
    checked_fields = sorted(set(checked_fields))
    fully_identified = len(rows_with_identity) == len(rows)
    return {
        "available": True,
        "ok": None if not checked_fields else (not violations and fully_identified),
        "reason": (
            "identity appears in multiple splits" if violations else
            "one or more rows has no identity field" if not fully_identified else
            None
        ),
        "checked_fields": checked_fields,
        "rows_without_identity": len(rows) - len(rows_with_identity),
        "violations": violations,
    }


def evaluate(rows, seed=42, bootstrap_iterations=DEFAULT_BOOTSTRAP_ITERATIONS, group_key=None,
             calibration_threshold_km=25.0):
    """Compute aggregate and optional group-stratified geolocation metrics."""
    if not rows:
        raise AuditError("Cannot evaluate an empty benchmark.")
    if type(seed) is not int:
        raise AuditError("Benchmark seed must be an integer.")
    if type(bootstrap_iterations) is not int or not 2 <= bootstrap_iterations <= 100000:
        raise AuditError("bootstrap_iterations must be an integer between 2 and 100000.")
    if group_key and not any(group_key in row for row in rows):
        raise AuditError(f"group key {group_key!r} is not present in prediction rows.")
    summary = _evaluate_subset(rows, seed, bootstrap_iterations)
    selected_group_key = group_key or _group_key(rows)
    groups = {}
    if selected_group_key:
        for value in sorted({str(row.get(selected_group_key, "")).strip() for row in rows}):
            if not value:
                continue
            groups[value] = _evaluate_subset(
                [row for row in rows if str(row.get(selected_group_key, "")).strip() == value],
                seed,
                bootstrap_iterations,
            )
    summary.update({
        "thresholds_km": list(THRESHOLDS_KM),
        "bootstrap_seed": seed,
        "bootstrap_iterations": bootstrap_iterations,
        "group_key": selected_group_key,
        "group_metrics": groups,
        "leakage": leakage_report(rows),
        "calibration": calibration_report(rows, calibration_threshold_km),
    })
    return summary


def _installed_version(package):
    try:
        importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None
    return True


def build_run_manifest(prediction_path, rows, dataset, model, commit, summary):
    """Build a machine-readable, redaction-safe record of a benchmark run."""
    path = Path(prediction_path)
    group_counts = {
        value: metrics["count"] for value, metrics in summary.get("group_metrics", {}).items()
    }
    return {
        "schema_version": 1,
        "tool": "photo-privacy-audit",
        "tool_version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset": str(dataset),
        "model": str(model),
        "code_or_checkpoint": str(commit or ""),
        "prediction_file_sha256": sha256(path),
        "prediction_count": len(rows),
        "predicted_count": summary["predicted_count"],
        "seed": summary["bootstrap_seed"],
        "bootstrap_iterations": summary["bootstrap_iterations"],
        "thresholds_km": summary["thresholds_km"],
        "group_key": summary.get("group_key"),
        "group_counts": group_counts,
        "topk_input": any(bool(str(row.get("topk_json", "")).strip()) for row in rows),
        "topk_metrics": summary.get("topk_metrics"),
        "leakage": summary["leakage"],
        "calibration": summary.get("calibration"),
        "coordinates_included": False,
        "dependencies": {
            package: importlib.metadata.version(package)
            for package in ("Pillow", "PyYAML")
            if _installed_version(package)
        },
    }

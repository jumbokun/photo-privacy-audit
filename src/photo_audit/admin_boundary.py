"""Offline point-in-polygon mapping against a downloaded administrative GeoJSON."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .common import AuditError, write_csv
from .geo_city import _number

FIELDS = ["city", "admin_region", "admin_level", "candidate_count", "score_mass",
          "best_candidate_rank", "min_distance_to_candidate_km", "support_fraction", "evidence"]


def _load_shape(path):
    try:
        from shapely.geometry import Point, shape
    except ImportError:
        raise AuditError("Boundary matching requires the optional shapely package.") from None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("type") != "FeatureCollection":
        raise AuditError("Boundary file must be a GeoJSON FeatureCollection.")
    features = []
    for feature in data.get("features", []):
        geometry = feature.get("geometry")
        props = feature.get("properties") or {}
        if not geometry:
            continue
        polygon = shape(geometry)
        if polygon.is_empty:
            continue
        features.append((polygon, props))
    if not features:
        raise AuditError("Boundary GeoJSON contains no usable geometries.")
    return Point, features


def load_candidates(path):
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    rows = value.get("candidates") if isinstance(value, dict) else value
    if not isinstance(rows, list) or not rows:
        raise AuditError("Candidate JSON is empty.")
    return rows


def map_candidates(candidate_path, boundary_path, *, radius_km=0.0):
    from .geo_city import haversine_km

    Point, features = _load_shape(boundary_path)
    candidates = load_candidates(candidate_path)
    matched = {}
    for candidate in candidates:
        lat, lon = _number(candidate.get("lat"), "lat"), _number(candidate.get("lon"), "lon")
        score = max(0.0, _number(candidate.get("score", 0), "score"))
        rank = int(candidate.get("rank", 0) or 0)
        point = Point(lon, lat)
        containing = [(polygon, props) for polygon, props in features if polygon.contains(point) or polygon.touches(point)]
        for polygon, props in containing:
            city = str(props.get("NAME_2") or props.get("NAME_1") or props.get("shapeName") or props.get("NAME_0") or "").strip()
            region = str(props.get("NAME_3") or props.get("NAME_2") or props.get("shapeName") or "").strip()
            level = 3 if props.get("NAME_3") or props.get("shapeType") == "ADM3" else (2 if props.get("NAME_2") else 1)
            key = (city, region, level)
            item = matched.setdefault(key, {"count": 0, "mass": 0.0, "best_rank": 999999,
                                             "min_distance": float("inf")})
            item["count"] += 1
            item["mass"] += score
            item["best_rank"] = min(item["best_rank"], rank or 999999)
            item["min_distance"] = min(item["min_distance"], 0.0)
    total = sum(item["mass"] for item in matched.values()) or 1.0
    rows = []
    for (city, region, level), item in matched.items():
        rows.append({"city": city, "admin_region": region, "admin_level": level,
                     "candidate_count": item["count"], "score_mass": item["mass"],
                     "best_candidate_rank": item["best_rank"],
                     "min_distance_to_candidate_km": item["min_distance"],
                     "support_fraction": item["mass"] / total,
                     "evidence": "GeoCLIP candidate point falls inside downloaded administrative polygon."})
    return sorted(rows, key=lambda row: (-row["score_mass"], row["best_candidate_rank"], row["city"], row["admin_region"]))


def write_boundary_report(path, rows):
    write_csv(path, FIELDS, rows)

"""Aggregate coordinate candidates into broad city candidates using a local table."""
import csv
import json
import math
from pathlib import Path

from .common import AuditError, write_csv

FIELDS = ["city_id", "city", "province", "candidate_count", "score_mass",
          "best_candidate_rank", "min_distance_km", "support_fraction", "evidence"]


def _number(value, field):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise AuditError(f"{field} must be numeric") from None
    if not math.isfinite(result):
        raise AuditError(f"{field} must be finite")
    return result


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(a)))


def load_city_centers(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {"city_id", "city", "province", "lat", "lon"}
    if not rows or not required.issubset(rows[0]):
        raise AuditError("City centers must contain city_id, city, province, lat, and lon.")
    output = []
    for row in rows:
        output.append({**row, "lat_value": _number(row["lat"], "lat"), "lon_value": _number(row["lon"], "lon")})
    return output


def load_candidates(path):
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    rows = value.get("candidates") if isinstance(value, dict) else value
    if not isinstance(rows, list) or not rows:
        raise AuditError("GeoCLIP candidate JSON is empty.")
    return rows


def match_cities(candidates, cities, radius_km=100.0):
    radius_km = _number(radius_km, "radius_km")
    if not 1 <= radius_km <= 1000:
        raise AuditError("radius_km must be between 1 and 1000.")
    # GeoCLIP's coordinate gallery may contain repeated points. Deduplicate
    # before city aggregation so one coordinate cannot manufacture support.
    unique = {}
    for candidate in candidates:
        lat = _number(candidate.get("lat"), "lat")
        lon = _number(candidate.get("lon"), "lon")
        score = max(0.0, _number(candidate.get("score", 0), "score"))
        key = (round(lat, 4), round(lon, 4))
        current = unique.get(key)
        rank = int(candidate.get("rank", 0) or 0)
        if current is None or score > current["score"]:
            unique[key] = {**candidate, "lat": lat, "lon": lon, "score": score, "rank": rank}
    matched = {}
    total_score = sum(row["score"] for row in unique.values()) or 1.0
    for candidate in unique.values():
        lat, lon = _number(candidate.get("lat"), "lat"), _number(candidate.get("lon"), "lon")
        score = max(0.0, _number(candidate.get("score", 0), "score"))
        rank = int(candidate.get("rank", 0) or 0)
        for city in cities:
            distance = haversine_km(lat, lon, city["lat_value"], city["lon_value"])
            if distance > radius_km:
                continue
            record = matched.setdefault(city["city_id"], {"city": city, "count": 0, "mass": 0.0,
                                                              "best_rank": 999999, "min_distance": float("inf")})
            record["count"] += 1
            record["mass"] += score
            record["best_rank"] = min(record["best_rank"], rank or 999999)
            record["min_distance"] = min(record["min_distance"], distance)
    output = []
    for city_id, item in matched.items():
        city = item["city"]
        output.append({"city_id": city_id, "city": city["city"], "province": city["province"],
                       "candidate_count": item["count"], "score_mass": item["mass"],
                       "best_candidate_rank": item["best_rank"],
                       "min_distance_km": item["min_distance"],
                       "support_fraction": item["mass"] / total_score,
                       "evidence": f"{item['count']} GeoCLIP gallery candidate(s) within {radius_km:g} km of city center."})
    return sorted(output, key=lambda row: (-row["score_mass"], row["best_candidate_rank"], row["city_id"]))


def write_city_report(path, rows):
    write_csv(path, FIELDS, rows)

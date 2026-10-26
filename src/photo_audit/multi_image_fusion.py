"""Multi-image candidate fusion for approved geolocation research runs.

The module combines *candidate coordinates* emitted by an approved local
retrieval/vision adapter.  It deliberately does not reverse geocode, call a
map service, or claim that a candidate is verified.  Each output is a spatial
candidate with a centre, a radius, and the evidence that contributed to it.

Candidates are normalised per image so that an image with a large top-k list
cannot dominate the other images.  A latitude-band index keeps spatial
clustering bounded for the small top-k sets normally produced by retrieval
systems.  Cluster confidence is a transparent heuristic; it is not a
calibrated probability unless it has been calibrated on an authorised,
held-out benchmark.
"""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Mapping

from .common import AuditError, write_json

EARTH_RADIUS_KM = 6371.0088
MAX_CANDIDATES = 20_000
DEFAULT_CLUSTER_RADIUS_KM = 2.0
DEFAULT_CONFLICT_DISTANCE_KM = 4.0
DEFAULT_SOURCE_RELIABILITY = {
    # Reliability is a prior used only for transparent weighting.  Projects
    # should replace these values with validation-set estimates when available.
    "retrieval": 1.0,
    "landmark": 0.95,
    "ocr": 0.90,
    "metadata": 0.90,
    "vision": 0.60,
    "prior": 0.40,
    "unknown": 0.50,
}

REQUIRED_FIELDS = {"image_id", "lat", "lon", "score"}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return great-circle distance in kilometres with strict coordinates."""
    try:
        lat1, lon1, lat2, lon2 = (float(value) for value in (lat1, lon1, lat2, lon2))
    except (TypeError, ValueError):
        raise AuditError("candidate coordinates must be numeric") from None
    values = (lat1, lon1, lat2, lon2)
    if not all(math.isfinite(value) for value in values):
        raise AuditError("candidate coordinates must be finite")
    if not -90 <= lat1 <= 90 or not -90 <= lat2 <= 90:
        raise AuditError("candidate latitude is outside [-90, 90]")
    if not -180 <= lon1 <= 180 or not -180 <= lon2 <= 180:
        raise AuditError("candidate longitude is outside [-180, 180]")
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    value = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(value)))


def _number(value, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise AuditError(f"candidate field {field} must be numeric") from None
    if not math.isfinite(result):
        raise AuditError(f"candidate field {field} must be finite")
    return result


def _probability(value, field: str, default: float) -> float:
    if value in (None, ""):
        return default
    result = _number(value, field)
    if not 0 <= result <= 1:
        raise AuditError(f"candidate field {field} must be between 0 and 1")
    return result


def normalise_candidates(rows: Iterable[Mapping], *, source_reliability: Mapping[str, float] | None = None):
    """Validate and add deterministic weights to candidate rows.

    ``score`` and optional ``confidence`` are expected to be in ``[0, 1]``.
    Scores are normalised within each image.  The returned dictionaries are
    safe for JSON serialisation and contain no filesystem paths or addresses.
    """
    reliability = dict(DEFAULT_SOURCE_RELIABILITY)
    if source_reliability is not None:
        for source, value in source_reliability.items():
            value = _probability(value, f"source_reliability[{source}]", 0.5)
            reliability[str(source).strip() or "unknown"] = value

    parsed = []
    seen_ids = set()
    for index, original in enumerate(rows):
        if not isinstance(original, Mapping):
            raise AuditError("each candidate must be an object")
        missing = REQUIRED_FIELDS - set(original)
        if missing:
            raise AuditError("candidate is missing: " + ", ".join(sorted(missing)))
        image_id = str(original.get("image_id", "")).strip()
        if not image_id:
            raise AuditError("candidate image_id must not be blank")
        candidate_id = str(original.get("candidate_id", f"{image_id}:{index}")).strip()
        if not candidate_id or candidate_id in seen_ids:
            raise AuditError("candidate_id values must be non-empty and unique")
        seen_ids.add(candidate_id)
        lat = _number(original.get("lat"), "lat")
        lon = _number(original.get("lon"), "lon")
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise AuditError("candidate coordinates are outside valid ranges")
        score = _probability(original.get("score"), "score", 0.0)
        confidence = _probability(original.get("confidence"), "confidence", 1.0)
        source = str(original.get("source", "unknown")).strip() or "unknown"
        if source not in reliability:
            reliability[source] = reliability["unknown"]
        raw_uncertainty = original.get("uncertainty_km", 0.0)
        uncertainty = 0.0 if raw_uncertainty in (None, "") else _number(raw_uncertainty, "uncertainty_km")
        if uncertainty < 0 or uncertainty > 100_000:
            raise AuditError("candidate uncertainty_km must be between 0 and 100000")
        parsed.append({
            "candidate_id": candidate_id,
            "image_id": image_id,
            "lat": lat,
            "lon": lon,
            "score": score,
            "confidence": confidence,
            "source": source,
            "uncertainty_km": uncertainty,
            "evidence": str(original.get("evidence", "")).strip(),
        })
    if not parsed:
        raise AuditError("candidate input is empty")
    if len(parsed) > MAX_CANDIDATES:
        raise AuditError(f"candidate input exceeds {MAX_CANDIDATES} rows")

    by_image = defaultdict(list)
    for row in parsed:
        # Uncertainty reduces the contribution of broad or weak retrievals,
        # while keeping a candidate with zero score from contributing at all.
        raw = (row["score"] * row["confidence"] * reliability[row["source"]]
               / (1.0 + row["uncertainty_km"] / 25.0))
        row["raw_weight"] = raw
        by_image[row["image_id"]].append(row)
    for image_id, image_rows in by_image.items():
        total = sum(row["raw_weight"] for row in image_rows)
        if total <= 0:
            raise AuditError(f"all candidates for image {image_id} have zero weight")
        for row in image_rows:
            row["image_mass"] = row["raw_weight"] / total
    return parsed


def _union_find(size: int):
    parent = list(range(size))
    rank = [0] * size

    def find(item):
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left, right):
        left, right = find(left), find(right)
        if left == right:
            return
        if rank[left] < rank[right]:
            left, right = right, left
        parent[right] = left
        if rank[left] == rank[right]:
            rank[left] += 1

    return find, union


def cluster_candidates(rows: Iterable[Mapping], radius_km: float = DEFAULT_CLUSTER_RADIUS_KM):
    """Cluster candidates with a deterministic great-circle connected graph.

    ``radius_km`` is an evidence grouping tolerance, not an uncertainty claim.
    Connected components are retained even when a long chain makes the overall
    component wider; that spread is reported and lowers confidence.
    """
    radius_km = _number(radius_km, "cluster_radius_km")
    if not 0 < radius_km <= 1000:
        raise AuditError("cluster_radius_km must be between 0 and 1000")
    rows = list(rows)
    if not rows:
        raise AuditError("candidate input is empty")
    # Accept raw rows as a convenience, while avoiding a second normalisation
    # pass for fuse_multiview's already weighted rows.
    if any("image_mass" not in row for row in rows):
        rows = normalise_candidates(rows)
    find, union = _union_find(len(rows))
    # One latitude band is about ``band_degrees`` kilometres.  Only adjacent
    # bands can contain points inside radius, and longitude wrapping is handled
    # by haversine_km, so no planar-distance approximation leaks into output.
    band_degrees = max(radius_km / EARTH_RADIUS_KM * 180 / math.pi, 1e-6)
    bands = defaultdict(list)
    for index, row in enumerate(rows):
        band = math.floor((row["lat"] + 90.0) / band_degrees)
        for nearby in (band - 1, band, band + 1):
            for previous in bands.get(nearby, ()):
                if haversine_km(row["lat"], row["lon"], rows[previous]["lat"], rows[previous]["lon"]) <= radius_km:
                    union(index, previous)
        bands[band].append(index)
    components = defaultdict(list)
    for index in range(len(rows)):
        components[find(index)].append(rows[index])
    return sorted(components.values(), key=lambda component: min(row["candidate_id"] for row in component))


def _weighted_centroid(rows):
    x = y = z = total = 0.0
    for row in rows:
        weight = row["image_mass"]
        lat = math.radians(row["lat"])
        lon = math.radians(row["lon"])
        x += weight * math.cos(lat) * math.cos(lon)
        y += weight * math.cos(lat) * math.sin(lon)
        z += weight * math.sin(lat)
        total += weight
    norm = math.sqrt(x * x + y * y + z * z)
    if norm < 1e-12:
        # A cluster spanning antipodal points has no meaningful centroid.  A
        # highest-mass medoid is deterministic and makes the conflict visible.
        return max(rows, key=lambda row: (row["image_mass"], row["candidate_id"]))["lat"], max(
            rows, key=lambda row: (row["image_mass"], row["candidate_id"]))["lon"]
    return math.degrees(math.asin(z / norm)), math.degrees(math.atan2(y, x))


def _weighted_quantile(values, quantile):
    """Return a weighted quantile for ``(value, weight)`` pairs."""
    ordered = sorted(values, key=lambda item: item[0])
    total = sum(weight for _, weight in ordered)
    if not ordered or total <= 0:
        return 0.0
    threshold = max(0.0, min(1.0, quantile)) * total
    cumulative = 0.0
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= threshold:
            return value
    return ordered[-1][0]


def _cluster_record(rows, total_images: int, rank: int):
    center_lat, center_lon = _weighted_centroid(rows)
    image_masses = defaultdict(float)
    for row in rows:
        image_masses[row["image_id"]] += row["image_mass"]
    support_images = sorted(image_masses)
    weighted_distance = sum(row["image_mass"] * haversine_km(center_lat, center_lon, row["lat"], row["lon"])
                            for row in rows)
    mass = sum(row["image_mass"] for row in rows)
    mean_distance = weighted_distance / max(mass, 1e-12)
    uncertainty = sum(row["image_mass"] * row["uncertainty_km"] for row in rows) / max(mass, 1e-12)
    spread = max(haversine_km(center_lat, center_lon, row["lat"], row["lon"]) for row in rows)
    # The reported range is the weighted 90th-percentile distance around the
    # consensus centre.  It is more stable than max distance for a single bad
    # retrieval while remaining a useful range for downstream review.
    radius = max(0.05, _weighted_quantile([
        (haversine_km(center_lat, center_lon, row["lat"], row["lon"]), row["image_mass"])
        for row in rows
    ], 0.90), uncertainty)
    support_fraction = len(support_images) / max(total_images, 1)
    independent_support = 1.0
    for image_mass in image_masses.values():
        independent_support *= 1.0 - min(1.0, image_mass)
    independent_support = 1.0 - independent_support
    mean_score = sum(row["image_mass"] * row["score"] for row in rows) / max(mass, 1e-12)
    # Consistency is deliberately reported as a heuristic feature.  A
    # calibration run should fit the mapping to a real held-out benchmark.
    consistency = math.exp(-mean_distance / max(0.25, radius))
    source_diversity = min(1.0, len({row["source"] for row in rows}) / 3.0)
    multi_support = min(1.0, len(support_images) / 2.0)
    confidence = (0.30 * support_fraction + 0.20 * independent_support +
                  0.20 * consistency + 0.15 * mean_score + 0.10 * source_diversity +
                  0.05 * multi_support)
    if len(support_images) < 2:
        confidence = min(confidence, 0.65)
    confidence = max(0.0, min(1.0, confidence))
    ranking_score = (0.40 * independent_support + 0.25 * support_fraction +
                     0.20 * mean_score + 0.15 * consistency)
    lats = [row["lat"] for row in rows]
    lons = [row["lon"] for row in rows]
    return {
        "rank": rank,
        "status": "candidate",
        "verified": False,
        "center_lat": round(center_lat, 7),
        "center_lon": round(center_lon, 7),
        "radius_km": round(radius, 4),
        "bbox": {"min_lat": min(lats), "max_lat": max(lats), "min_lon": min(lons), "max_lon": max(lons)},
        "candidate_count": len(rows),
        "support_image_ids": support_images,
        "support_image_count": len(support_images),
        "support_fraction": round(support_fraction, 6),
        "cluster_mass": round(mass, 6),
        "independent_support": round(independent_support, 6),
        "mean_candidate_score": round(mean_score, 6),
        "mean_distance_km": round(mean_distance, 4),
        "spread_km": round(spread, 4),
        "radius_method": "weighted_90th_percentile_distance",
        "source_diversity": len({row["source"] for row in rows}),
        "confidence": round(confidence, 6),
        "ranking_score": round(ranking_score, 6),
        "evidence": sorted({row["evidence"] for row in rows if row["evidence"]}),
        "candidate_ids": sorted(row["candidate_id"] for row in rows),
    }


def fuse_multiview(rows: Iterable[Mapping], *, cluster_radius_km: float = DEFAULT_CLUSTER_RADIUS_KM,
                   conflict_distance_km: float | None = None, top_k: int = 5,
                   source_reliability: Mapping[str, float] | None = None):
    """Fuse candidate coordinates from multiple images into ranked ranges.

    The returned object has ``status='candidate'`` or ``'conflict'`` and never
    emits a place name or a verified address.  ``confidence`` is an
    uncalibrated score until a project calibrates it using held-out truth.
    """
    if type(top_k) is not int or not 1 <= top_k <= 100:
        raise AuditError("top_k must be between 1 and 100")
    cluster_radius_km = _number(cluster_radius_km, "cluster_radius_km")
    if conflict_distance_km is None:
        conflict_distance_km = max(DEFAULT_CONFLICT_DISTANCE_KM, cluster_radius_km * 2.0)
    conflict_distance_km = _number(conflict_distance_km, "conflict_distance_km")
    if conflict_distance_km <= 0:
        raise AuditError("conflict_distance_km must be positive")
    normalised = normalise_candidates(rows, source_reliability=source_reliability)
    image_count = len({row["image_id"] for row in normalised})
    components = cluster_candidates(normalised, cluster_radius_km)
    records = [_cluster_record(component, image_count, index + 1) for index, component in enumerate(components)]
    records.sort(key=lambda row: (-row["ranking_score"], -row["confidence"], row["center_lat"], row["center_lon"]))
    for rank, record in enumerate(records, 1):
        record["rank"] = rank
    records = records[:top_k]

    conflict = {"detected": False, "reason": "", "pairs": []}
    if len(records) >= 2:
        for left_index, left in enumerate(records):
            for right in records[left_index + 1:]:
                distance = haversine_km(left["center_lat"], left["center_lon"], right["center_lat"], right["center_lon"])
                relative = right["ranking_score"] / max(left["ranking_score"], 1e-12)
                shared_images = sorted(set(left["support_image_ids"]) & set(right["support_image_ids"]))
                if distance >= conflict_distance_km and relative >= 0.55:
                    conflict["detected"] = True
                    conflict["pairs"].append({"rank_a": left["rank"], "rank_b": right["rank"],
                                               "distance_km": round(distance, 4),
                                               "relative_score": round(relative, 6),
                                               "shared_image_ids": shared_images})
    if conflict["detected"]:
        conflict["reason"] = "multiple spatially separated candidate clusters have comparable support"
    return {
        "schema_version": "multi-image-fusion/v1",
        "status": "conflict" if conflict["detected"] else "candidate",
        "verified": False,
        "image_count": image_count,
        "candidate_count": len(normalised),
        "cluster_count": len(components),
        "cluster_radius_km": cluster_radius_km,
        "conflict_distance_km": conflict_distance_km,
        "method": {
            "normalisation": "per_image_mass",
            "centroid": "weighted_spherical_centroid",
            "clustering": "latitude_band_connected_components",
            "confidence": "uncalibrated_heuristic",
            "address_lookup": False,
        },
        "conflict": conflict,
        "clusters": records,
    }


def load_candidates(path):
    """Load candidate rows from CSV, JSON, or JSONL for a research run."""
    path = Path(path)
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        if rows and {"query_asset_id", "candidate_lat", "candidate_lon", "cosine_similarity"}.issubset(rows[0]):
            # Adapt the retrieval report contract without asking a researcher
            # to hand-edit coordinates or scores. Cosine [-1, 1] is mapped to
            # an ordering score [0, 1]; it is not called a probability.
            adapted = []
            for row in rows:
                if row.get("match_status") != "candidate":
                    continue
                try:
                    cosine = float(row["cosine_similarity"])
                    lat = float(row["candidate_lat"])
                    lon = float(row["candidate_lon"])
                except (TypeError, ValueError):
                    raise AuditError("retrieval report contains invalid candidate coordinates or similarity") from None
                adapted.append({
                    "image_id": row.get("query_asset_id", ""),
                    "candidate_id": f"{row.get('query_asset_id', '')}:{row.get('candidate_rank', len(adapted) + 1)}",
                    "lat": lat, "lon": lon,
                    "score": max(0.0, min(1.0, (cosine + 1.0) / 2.0)),
                    "confidence": "", "source": "retrieval",
                    "uncertainty_km": "", "evidence": row.get("evidence", ""),
                })
            return adapted
        return rows
    with path.open(encoding="utf-8-sig") as handle:
        if path.suffix.lower() in {".jsonl", ".ndjson"}:
            return [json.loads(line) for line in handle if line.strip()]
        value = json.load(handle)
    if isinstance(value, dict):
        value = value.get("candidates")
    if not isinstance(value, list):
        raise AuditError("candidate JSON must contain a list or a candidates list")
    return value


def write_fusion_report(path, report):
    """Write a new JSON report; never overwrite an existing run artifact."""
    write_json(path, report)


# US spelling is useful for callers while keeping the canonical API explicit.
normalize_candidates = normalise_candidates
fuse_multi_image = fuse_multiview


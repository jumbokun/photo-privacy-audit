"""Offline geotagged reference retrieval for authorized research runs.

The privacy-audit path never needs this module.  It is deliberately a small,
deterministic retrieval layer for a *local* reference set whose coordinates
are already licensed/approved for research.  A local encoder produces query
and reference embeddings; this module only validates them, applies optional
spatial filters, and emits ranked evidence for review.

No maps, reverse geocoders, network calls, or model downloads are used here.
"""

from __future__ import annotations

import csv
import hashlib
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .common import AuditError, write_csv
from .embeddings import load_embeddings
from .benchmark import haversine_km


MANIFEST_REQUIRED = {"asset_id", "lat", "lon"}
MANIFEST_OPTIONAL = {
    "relative_path", "source", "source_id", "license", "dataset", "split",
    "scene_id", "capture_id", "authorization_status", "grid_id", "provider",
    "provider_image_id", "capture_date", "heading", "pitch", "fov", "source_url",
    "license_url", "license_provenance", "stratum", "source_provider", "source_collection",
}
REPORT_FIELDS = [
    "query_asset_id", "candidate_rank", "candidate_asset_id", "candidate_lat",
    "candidate_lon", "grid_id", "grid_rank", "grid_count", "grid_mean_similarity",
    "cosine_similarity", "model_id", "reference_source", "reference_dataset",
    "reference_license", "reference_authorization_status", "reference_relative_path",
    "reference_provider", "reference_provider_image_id", "reference_capture_date",
    "reference_source_provider", "reference_source_collection",
    "reference_source_url", "reference_license_url", "reference_license_provenance",
    "reference_stratum",
    "distance_to_filter_center_km",
    "spatial_filter", "match_status", "evidence",
    # Fusion-compatible aliases. ``score`` is an affine mapping of cosine
    # from [-1, 1] to [0, 1], retained alongside the raw cosine value.
    "image_id", "candidate_id", "lat", "lon", "score", "source",
]


def stratified_sample_manifest(rows, limit, *, strata_fields=("provider", "grid_id"), seed=42):
    """Deterministically sample a manifest while preserving stratum coverage.

    Street-view providers often return many nearly identical headings or
    captures in one block. Sampling by provider/grid (and optionally dataset
    or capture metadata) prevents one provider or urban hotspot from filling
    the entire reference gallery. A hash-ranked round-robin gives stable
    output without relying on Python's process-randomized ``hash()``.
    """
    if type(limit) is not int or limit < 1:
        raise AuditError("reference sample limit must be a positive integer.")
    if not isinstance(seed, (str, int)):
        raise AuditError("reference sample seed must be text or an integer.")
    rows = list(rows)
    if len(rows) <= limit:
        return rows
    fields = tuple(str(field).strip() for field in strata_fields if str(field).strip())
    if not fields:
        raise AuditError("strata_fields must contain at least one field.")
    groups = defaultdict(list)
    for row in rows:
        key = tuple(_text(row.get(field)) or "<missing>" for field in fields)
        asset_id = _text(row.get("asset_id"))
        digest = hashlib.sha256(f"{seed}\0{asset_id}".encode("utf-8")).hexdigest()
        groups[key].append((digest, asset_id, row))
    for values in groups.values():
        values.sort(key=lambda item: (item[0], item[1]))
    selected = []
    group_keys = sorted(groups)
    offsets = {key: 0 for key in group_keys}
    # Round-robin is intentionally balanced: a dense provider/grid cell can't
    # crowd out less frequent strata until each has received a sample.
    while len(selected) < limit:
        progressed = False
        for key in group_keys:
            offset = offsets[key]
            values = groups[key]
            if offset >= len(values):
                continue
            selected.append(values[offset][2])
            offsets[key] = offset + 1
            progressed = True
            if len(selected) >= limit:
                break
        if not progressed:
            break
    return selected


def _text(value):
    return "" if value is None else str(value).strip()


def _coordinate(value, key):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise AuditError(f"Reference manifest {key} must be a finite number.") from None
    if not math.isfinite(number):
        raise AuditError(f"Reference manifest {key} must be a finite number.")
    if key == "lat" and not -90 <= number <= 90:
        raise AuditError("Reference manifest latitude is outside [-90, 90].")
    if key == "lon" and not -180 <= number <= 180:
        raise AuditError("Reference manifest longitude is outside [-180, 180].")
    return number


def grid_id(lat, lon, grid_size_deg=0.01):
    """Return a deterministic latitude/longitude grid cell identifier.

    ``grid_size_deg`` is intentionally explicit in the report and manifest.
    At the equator 0.01 degrees is approximately 1.1 km; use a smaller value
    for dense urban references.  The grid is a retrieval grouping aid, not a
    claim of coordinate precision.
    """
    try:
        size = float(grid_size_deg)
    except (TypeError, ValueError):
        raise AuditError("grid_size_deg must be a finite positive number.") from None
    if not math.isfinite(size) or not 1e-6 <= size <= 10:
        raise AuditError("grid_size_deg must be between 0.000001 and 10 degrees.")
    lat = _coordinate(lat, "lat")
    lon = _coordinate(lon, "lon")
    # Clamp the positive endpoint so 90/180 do not create a one-cell overflow.
    lat_index = math.floor(min(lat, 90.0 - 1e-12) / size)
    lon_index = math.floor(min(lon, 180.0 - 1e-12) / size)
    return f"g{lat_index}:{lon_index}"


def load_reference_manifest(path, *, grid_size_deg=0.01, require_authorization=False):
    """Load and validate a local geotagged reference manifest.

    Coordinates are accepted only for this explicitly research-scoped input;
    callers must keep personal-photo manifests out of this path.  If an
    ``authorization_status`` column is present, only approved/authorized or
    benchmark values are accepted.
    """
    path = Path(path)
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = reader.fieldnames or []
            if len(fields) != len(set(fields)):
                raise AuditError("Reference manifest contains duplicate column names.")
            missing = MANIFEST_REQUIRED - set(fields)
            if missing:
                raise AuditError("Reference manifest is missing: " + ", ".join(sorted(missing)))
            rows = list(reader)
    except FileNotFoundError:
        raise AuditError("Reference manifest does not exist.") from None
    if not rows:
        raise AuditError("Reference manifest is empty.")

    result = []
    seen = set()
    accepted_statuses = {
        "approved", "authorized", "benchmark", "research", "public", "licensed",
        "consented", "consent", "allowlisted", "public_api",
    }
    for row_number, row in enumerate(rows, start=2):
        asset_id = _text(row.get("asset_id"))
        if not asset_id:
            raise AuditError(f"Reference manifest asset_id is blank (row {row_number}).")
        if asset_id in seen:
            raise AuditError("Reference manifest asset_id values must be unique.")
        seen.add(asset_id)
        lat = _coordinate(row.get("lat"), "lat")
        lon = _coordinate(row.get("lon"), "lon")
        status = _text(row.get("authorization_status"))
        if require_authorization and not status:
            raise AuditError("Reference manifest requires authorization_status for research retrieval.")
        if status and status.lower() not in accepted_statuses:
            raise AuditError(
                "Reference manifest authorization_status must be approved, authorized, "
                "benchmark, research, public, public_api, licensed, consented, or allowlisted."
            )
        item = {"asset_id": asset_id, "lat": lat, "lon": lon}
        for key in MANIFEST_OPTIONAL:
            value = _text(row.get(key))
            if value:
                item[key] = value
        supplied_grid = item.get("grid_id")
        item["grid_id"] = supplied_grid or grid_id(lat, lon, grid_size_deg)
        item["grid_size_deg"] = float(grid_size_deg)
        result.append(item)
    return result


def _validate_embeddings(rows, label):
    if not rows:
        raise AuditError(f"{label} embedding file is empty.")
    model_ids = {row.get("model_id") for row in rows}
    if len(model_ids) != 1:
        raise AuditError(f"{label} embeddings must contain exactly one model_id.")
    dimensions = {len(row.get("vector", ())) for row in rows}
    if len(dimensions) != 1:
        raise AuditError(f"{label} embeddings must have one vector dimension.")
    return next(iter(model_ids)), next(iter(dimensions))


@dataclass(frozen=True)
class ReferenceIndex:
    """Validated in-memory reference index.

    The index remains small and dependency-free by using a spatial cell map
    plus a cosine scan over the selected cells.  A caller can replace the
    scan with an ANN index while keeping this manifest/report contract.
    """

    manifest: tuple
    embeddings: tuple
    grid_size_deg: float = 0.01

    def __post_init__(self):
        try:
            index_grid_size = float(self.grid_size_deg)
        except (TypeError, ValueError):
            raise AuditError("grid_size_deg must be a finite positive number.") from None
        if not math.isfinite(index_grid_size) or not 1e-6 <= index_grid_size <= 10:
            raise AuditError("grid_size_deg must be between 0.000001 and 10 degrees.")
        manifest = list(self.manifest)
        embeddings = list(self.embeddings)
        if not manifest:
            raise AuditError("Reference index requires at least one manifest row.")
        manifest_by_id = {row["asset_id"]: row for row in manifest}
        if len(manifest_by_id) != len(manifest):
            raise AuditError("Reference manifest asset_id values must be unique.")
        model_id, dimension = _validate_embeddings(embeddings, "Reference")
        missing = [row["asset_id"] for row in embeddings if row["asset_id"] not in manifest_by_id]
        if missing:
            raise AuditError("Reference embeddings contain IDs absent from the manifest.")
        embedding_by_id = {row["asset_id"]: row for row in embeddings}
        by_grid = defaultdict(list)
        for row in manifest:
            by_grid[row["grid_id"]].append(row)
        object.__setattr__(self, "manifest", tuple(manifest))
        object.__setattr__(self, "embeddings", tuple(embeddings))
        object.__setattr__(self, "_manifest_by_id", manifest_by_id)
        object.__setattr__(self, "_embedding_by_id", embedding_by_id)
        object.__setattr__(self, "_by_grid", {key: tuple(value) for key, value in by_grid.items()})
        object.__setattr__(self, "model_id", model_id)
        object.__setattr__(self, "dimension", dimension)
        object.__setattr__(self, "grid_size_deg", index_grid_size)

    @classmethod
    def from_files(cls, manifest_path, embedding_path, *, grid_size_deg=0.01,
                   require_authorization=False):
        manifest = load_reference_manifest(
            manifest_path,
            grid_size_deg=grid_size_deg,
            require_authorization=require_authorization,
        )
        embeddings = load_embeddings(embedding_path)
        return cls(tuple(manifest), tuple(embeddings), float(grid_size_deg))

    def _eligible(self, *, center_lat=None, center_lon=None, radius_km=None, grid_ids=None):
        if (center_lat is None) != (center_lon is None):
            raise AuditError("center_lat and center_lon must be supplied together.")
        if radius_km is not None:
            try:
                radius = float(radius_km)
            except (TypeError, ValueError):
                raise AuditError("radius_km must be a finite non-negative number.") from None
            if not math.isfinite(radius) or radius < 0 or radius > 20050:
                raise AuditError("radius_km must be between 0 and 20050 km.")
            if center_lat is None:
                raise AuditError("radius_km requires center_lat and center_lon.")
        else:
            radius = None
        if center_lat is not None:
            center_lat = _coordinate(center_lat, "lat")
            center_lon = _coordinate(center_lon, "lon")
        selected_grids = None if grid_ids is None else {_text(value) for value in grid_ids if _text(value)}
        candidate_manifest = self.manifest
        if selected_grids:
            candidate_manifest = tuple(
                row for key in sorted(selected_grids) for row in self._by_grid.get(key, ())
            )
        result = []
        for row in candidate_manifest:
            if selected_grids and row["grid_id"] not in selected_grids:
                continue
            distance = None
            if center_lat is not None:
                distance = haversine_km(center_lat, center_lon, row["lat"], row["lon"])
                if radius is not None and distance > radius:
                    continue
            result.append((row, distance))
        return result

    def search(self, query_rows, *, top_k=10, center_lat=None, center_lon=None,
               radius_km=None, grid_ids=None, rerank="grid", max_per_capture=None):
        """Return ranked candidate rows for each normalized query embedding."""
        if type(top_k) is not int or not 1 <= top_k <= 1000:
            raise AuditError("top_k must be an integer between 1 and 1000.")
        if rerank not in {"embedding", "grid"}:
            raise AuditError("rerank must be embedding or grid.")
        if max_per_capture is not None and (type(max_per_capture) is not int or not 1 <= max_per_capture <= 100):
            raise AuditError("max_per_capture must be an integer between 1 and 100.")
        query_model, query_dimension = _validate_embeddings(query_rows, "Query")
        if query_model != self.model_id or query_dimension != self.dimension:
            raise AuditError("Query and reference embeddings must use the same model and dimension.")
        eligible = self._eligible(center_lat=center_lat, center_lon=center_lon,
                                  radius_km=radius_km, grid_ids=grid_ids)
        spatial_filter = "none"
        if center_lat is not None:
            spatial_filter = f"radius:{float(radius_km):g}km@{float(center_lat):.6f},{float(center_lon):.6f}"
        if grid_ids:
            grid_filter = "grid:" + ",".join(sorted({_text(value) for value in grid_ids if _text(value)}))
            spatial_filter = grid_filter if spatial_filter == "none" else spatial_filter + ";" + grid_filter

        results = []
        for query in query_rows:
            ranked = []
            for manifest_row, distance in eligible:
                reference = self._embedding_by_id.get(manifest_row["asset_id"])
                if reference is None:
                    continue
                score = sum(a * b for a, b in zip(query["vector"], reference["vector"]))
                ranked.append((score, manifest_row, distance))
            ranked.sort(key=lambda item: (-item[0], item[1]["asset_id"]))
            if max_per_capture is not None:
                # A provider may export many headings/frames for one capture.
                # Cap those before grid voting so repeated views do not create
                # artificial consensus. Assets without capture_id remain
                # independent references.
                capture_counts = {}
                deduplicated = []
                for item in ranked:
                    capture = _text(item[1].get("capture_id"))
                    if not capture:
                        deduplicated.append(item)
                        continue
                    key = (_text(item[1].get("provider")), capture)
                    count = capture_counts.get(key, 0)
                    if count >= max_per_capture:
                        continue
                    capture_counts[key] = count + 1
                    deduplicated.append(item)
                ranked = deduplicated
            all_grid_stats = {}
            for score, row, _distance in ranked:
                stats = all_grid_stats.setdefault(row["grid_id"], {"count": 0, "scores": []})
                stats["count"] += 1
                stats["scores"].append(score)
            grid_order_all = sorted(
                all_grid_stats,
                key=lambda key: (
                    -sum(sorted(all_grid_stats[key]["scores"], reverse=True)[:5]) /
                    max(1, min(5, len(all_grid_stats[key]["scores"]))),
                    -all_grid_stats[key]["count"], key,
                ),
            )
            grid_ranks_all = {key: index for index, key in enumerate(grid_order_all, start=1)}
            if rerank == "grid":
                ranked.sort(key=lambda item: (grid_ranks_all[item[1]["grid_id"]], -item[0], item[1]["asset_id"]))
            chosen = ranked[:top_k]
            if not chosen:
                results.append({
                    "query_asset_id": query["asset_id"], "candidate_rank": 0,
                    "candidate_asset_id": "", "candidate_lat": "", "candidate_lon": "",
                    "grid_id": "", "grid_rank": "", "grid_count": 0,
                    "grid_mean_similarity": "", "cosine_similarity": "",
                    "model_id": self.model_id, "reference_source": "",
                    "reference_dataset": "", "reference_license": "",
                    "reference_authorization_status": "", "reference_relative_path": "",
                    "reference_provider": "", "reference_provider_image_id": "",
                    "reference_capture_date": "", "reference_source_url": "",
                    "reference_source_provider": "", "reference_source_collection": "",
                    "reference_license_url": "", "reference_license_provenance": "",
                    "reference_stratum": "",
                    "distance_to_filter_center_km": "",
                    "spatial_filter": spatial_filter, "match_status": "no_candidates",
                    "evidence": "No authorized reference passed the spatial filter.",
                    "image_id": query["asset_id"], "candidate_id": "", "lat": "", "lon": "",
                    "score": "", "source": "retrieval",
                })
                continue
            grid_stats = {}
            for score, row, _distance in chosen:
                stats = grid_stats.setdefault(row["grid_id"], {"count": 0, "scores": []})
                stats["count"] += 1
                stats["scores"].append(score)
            grid_order = sorted(
                grid_stats,
                key=lambda key: (-sum(grid_stats[key]["scores"]), -grid_stats[key]["count"], key),
            )
            grid_ranks = {key: index for index, key in enumerate(grid_order, start=1)}
            for rank, (score, row, distance) in enumerate(chosen, start=1):
                stats = grid_stats[row["grid_id"]]
                evidence = (
                    f"cosine={score:.8f}; model={self.model_id}; grid={row['grid_id']}; "
                    f"grid_rank={grid_ranks[row['grid_id']]}; "
                    f"reference={row.get('source', row.get('source_id', 'local'))}"
                )
                if distance is not None:
                    evidence += f"; filter_distance_km={distance:.3f}"
                results.append({
                    "query_asset_id": query["asset_id"], "candidate_rank": rank,
                    "candidate_asset_id": row["asset_id"], "candidate_lat": f"{row['lat']:.8f}",
                    "candidate_lon": f"{row['lon']:.8f}", "grid_id": row["grid_id"],
                    "grid_rank": grid_ranks[row["grid_id"]], "grid_count": stats["count"],
                    "grid_mean_similarity": f"{sum(stats['scores']) / len(stats['scores']):.8f}",
                    "cosine_similarity": f"{score:.8f}", "model_id": self.model_id,
                    "reference_source": row.get("source", row.get("source_id", "local")),
                    "reference_dataset": row.get("dataset", ""),
                    "reference_license": row.get("license", ""),
                    "reference_authorization_status": row.get("authorization_status", ""),
                    "reference_relative_path": row.get("relative_path", ""),
                    "reference_provider": row.get("provider", ""),
                    "reference_provider_image_id": row.get("provider_image_id", ""),
                    "reference_capture_date": row.get("capture_date", ""),
                    "reference_source_provider": row.get("source_provider", ""),
                    "reference_source_collection": row.get("source_collection", ""),
                    "reference_source_url": row.get("source_url", ""),
                    "reference_license_url": row.get("license_url", ""),
                    "reference_license_provenance": row.get("license_provenance", ""),
                    "reference_stratum": row.get("stratum", ""),
                    "distance_to_filter_center_km": "" if distance is None else f"{distance:.6f}",
                    "spatial_filter": spatial_filter, "match_status": "candidate",
                    "evidence": evidence,
                    "image_id": query["asset_id"], "candidate_id": row["asset_id"],
                    "lat": f"{row['lat']:.8f}", "lon": f"{row['lon']:.8f}",
                    "score": f"{(score + 1.0) / 2.0:.8f}", "source": "retrieval",
                })
        return results


def retrieve_candidates(query_embeddings, reference_manifest, reference_embeddings,
                        *, top_k=10, grid_size_deg=0.01, center_lat=None,
                        center_lon=None, radius_km=None, grid_ids=None,
                        require_authorization=False, rerank="grid", max_per_capture=None):
    """Convenience API accepting paths to the three research inputs."""
    index = ReferenceIndex.from_files(reference_manifest, reference_embeddings,
                                      grid_size_deg=grid_size_deg,
                                      require_authorization=require_authorization)
    queries = load_embeddings(query_embeddings)
    return index.search(queries, top_k=top_k, center_lat=center_lat, center_lon=center_lon,
                        radius_km=radius_km, grid_ids=grid_ids, rerank=rerank,
                        max_per_capture=max_per_capture)


def write_retrieval_report(path, rows):
    """Write a UTF-8 BOM CSV suitable for Chinese spreadsheet software."""
    write_csv(path, REPORT_FIELDS, rows)


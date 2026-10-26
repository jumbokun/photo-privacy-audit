"""Explainable, offline image correlation using a supplied post manifest.

The matcher emits review leads rather than scene or identity claims. It uses
exact SHA-256 equality plus rotation-aware dHash/pHash distances and records the
distance and time evidence that produced each lead.
"""
import csv
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .common import AuditError, contained_file

MANIFEST_FIELDS = ["asset_id", "relative_path", "post_id", "post_timestamp"]
_HEX = re.compile(r"^[0-9a-fA-F]+$")


def parse_timestamp(value):
    if not value:
        return None
    value = value.strip().replace("Z", "+00:00")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError:
        raise AuditError("Post timestamps must be ISO-8601 values.") from None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def hamming(left, right):
    if not left or not right:
        return None
    if not isinstance(left, str) or not isinstance(right, str) or not _HEX.fullmatch(left) or not _HEX.fullmatch(right):
        raise AuditError("Perceptual hashes must contain hexadecimal characters only.")
    if len(left) != len(right):
        return None
    return (int(left, 16) ^ int(right, 16)).bit_count()


def _hash_variants(value, fallback=None):
    """Decode a CSV JSON list while accepting pre-extension metadata rows."""
    if isinstance(value, (list, tuple)):
        values = list(value)
    elif isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            decoded = None
        values = decoded if isinstance(decoded, list) else [value.strip()]
    else:
        values = []
    if fallback and fallback not in values:
        values.insert(0, fallback)
    return [str(item) for item in values if item]


def _best_distance(left, right, field, fallback_field=None):
    left_values = _hash_variants(left.get(field), left.get(fallback_field) if fallback_field else None)
    right_values = _hash_variants(right.get(field), right.get(fallback_field) if fallback_field else None)
    distances = [hamming(a, b) for a in left_values for b in right_values]
    distances = [distance for distance in distances if distance is not None]
    return min(distances) if distances else None


def _match_metrics(left, right, threshold):
    def informative(row):
        value = row.get("hash_informative", True)
        if isinstance(value, str):
            return value.strip().lower() not in {"false", "0", "no", ""}
        return bool(value)

    # Uniform/near-uniform frames create meaningless all-zero dHashes. Exact
    # SHA-256 links are still handled separately, but such frames do not enter
    # the perceptual candidate graph.
    if not informative(left) or not informative(right):
        return None
    d_distance = _best_distance(left, right, "dhash_rotations", "dhash")
    p_distance = _best_distance(left, right, "phash_rotations", "phash")
    d16_distance = hamming(left.get("dhash_16"), right.get("dhash_16"))
    # pHash supplies an independent low-frequency check. When both are
    # present, require pHash to be close if dHash is only borderline; this
    # reduces false positives from flat or repetitive textures.
    if d_distance is None and p_distance is None:
        return None
    if d_distance is not None and d_distance <= threshold:
        matched = True
    elif p_distance is not None and p_distance <= threshold and (d_distance is None or d_distance <= threshold * 2):
        matched = True
    else:
        matched = False
    if not matched:
        return None
    return {"dhash": d_distance, "phash": p_distance, "dhash_16": d16_distance,
            "method": "rot4:dhash+pHash" if d_distance is not None and p_distance is not None
            else ("rot4:dhash" if d_distance is not None else "rot4:pHash")}


def read_manifest(path, image_root, asset_ids, expected_paths=None):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = set(MANIFEST_FIELDS) - set(reader.fieldnames or [])
        if missing:
            raise AuditError("Manifest is missing required columns: " + ", ".join(sorted(missing)))
        rows = list(reader)
    seen = set()
    result = []
    for row in rows:
        asset_id = str(row.get("asset_id", "")).strip()
        if asset_id not in asset_ids or asset_id in seen:
            raise AuditError("Manifest asset_id must match metadata_report exactly and be unique.")
        seen.add(asset_id)
        relative_path = str(row.get("relative_path", "")).strip()
        if expected_paths is not None:
            expected = expected_paths.get(asset_id)
            if expected is None or Path(relative_path).as_posix() != Path(str(expected)).as_posix():
                raise AuditError("Manifest relative_path must match metadata_report exactly.")
        contained_file(image_root, relative_path)
        result.append({**row, "relative_path": relative_path,
                       "post_timestamp_parsed": parse_timestamp(row.get("post_timestamp", "")),
                       "asset_id": asset_id})
    if seen != set(asset_ids):
        raise AuditError("Manifest must contain one row for every audited image.")
    return result


def _time_delta_hours(left, right):
    if not left or not right:
        return None
    return abs((left - right).total_seconds()) / 3600.0


def correlate(metadata_rows, manifest_rows, threshold, window_hours):
    """Return one explainable correlation row per audited asset.

    ``threshold`` applies to each 64-bit perceptual hash. Every match retains
    the minimum rotation distance and method so a reviewer can reproduce or
    reject the lead. A post timestamp only raises the score when it links
    different posts; missing timestamps never count as a zero-risk result.
    """
    by_id = {row["asset_id"]: row for row in metadata_rows}
    manifest_by_asset = {row["asset_id"]: row for row in manifest_rows}
    exact = defaultdict(list)
    for row in metadata_rows:
        exact[row["sha256"]].append(row["asset_id"])
    near = defaultdict(list)
    for index, left in enumerate(metadata_rows):
        for right in metadata_rows[index + 1:]:
            metrics = _match_metrics(left, right, threshold)
            if metrics is None:
                continue
            near[left["asset_id"]].append((right["asset_id"], metrics))
            near[right["asset_id"]].append((left["asset_id"], metrics))
    rows = []
    for asset_id, meta in by_id.items():
        post = manifest_by_asset[asset_id]
        own_post_id = post.get("post_id", "").strip()
        exact_group = sorted(exact[meta["sha256"]])
        near_matches = sorted(near[asset_id], key=lambda item: (
            item[1].get("dhash") if item[1].get("dhash") is not None else 999,
            item[1].get("phash") if item[1].get("phash") is not None else 999,
            item[0]))
        linked_ids = set(exact_group + [item[0] for item in near_matches]) - {asset_id}
        linked_posts = {manifest_by_asset[item].get("post_id", "") for item in linked_ids}
        linked_posts.discard("")
        cross_posts = {post_id for post_id in linked_posts if post_id != own_post_id}
        own_time = post.get("post_timestamp_parsed")
        deltas = []
        for item in linked_ids:
            delta = _time_delta_hours(own_time, manifest_by_asset[item].get("post_timestamp_parsed"))
            if delta is not None:
                deltas.append(delta)
        time_delta = min(deltas) if deltas else None
        time_link = any(delta <= window_hours for delta in deltas) and bool(cross_posts)
        exact_cross = any(manifest_by_asset[item].get("post_id", "") != own_post_id
                          for item in exact_group if item != asset_id)
        near_cross = any(manifest_by_asset[item[0]].get("post_id", "") != own_post_id
                         for item in near_matches)
        reasons = []
        if exact_cross:
            reasons.append("exact file hash repeats across posts")
        elif exact_group and len(exact_group) > 1:
            reasons.append("exact file hash repeats within one post")
        if near_cross:
            reasons.append("rotation-aware perceptual hash is within policy threshold across posts")
        if time_link:
            reasons.append(f"linked posts are within the {window_hours:g}-hour time window")
        elif linked_ids and own_time is None:
            reasons.append("linked post has no usable timestamp")
        score = 4 if exact_cross else (3 if near_cross and time_link else (2 if near_cross else 0))
        if score == 0 and time_link:
            score = 1
        rows.append({
            "asset_id": asset_id,
            "post_id": own_post_id,
            "exact_match_asset_ids": exact_group if len(exact_group) > 1 else [],
            "near_match_asset_ids": [item[0] for item in near_matches],
            "near_match_hamming": [item[1].get("dhash") for item in near_matches],
            "near_match_phash": [item[1].get("phash") for item in near_matches],
            "near_match_method": [item[1].get("method") for item in near_matches],
            "linked_post_count": len(linked_posts),
            "cross_post_count": len(cross_posts),
            "time_link": time_link,
            "time_delta_hours": round(time_delta, 3) if time_delta is not None else "",
            "cross_post_risk": score,
            "risk_reasons": reasons,
            "evidence": " ; ".join(reasons) if reasons else "No exact or policy-threshold perceptual cross-post match found.",
        })
    return rows

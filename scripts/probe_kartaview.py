#!/usr/bin/env python3
"""Probe public KartaView coverage without downloading imagery.

This is intentionally a metadata-only probe.  It requests a small number of
points and reports the API status, hit count and a few identifying fields.  It
does not follow ``fileurl`` links or save photographs, which keeps a coverage
check cheap and avoids creating a large unreviewed reference corpus.

The current KartaView API accepts radii up to 500 m.  A successful response is
identified by ``status.apiCode == 600``; HTTP 200 alone is not sufficient.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError as exc:  # pragma: no cover - exercised only without optional dep
    raise SystemExit("This probe requires requests (pip install requests).") from exc


API_URL = "https://api.kartaview.org/2.0/photo/"

# A small, reproducible Chongqing smoke-test set.  The points are deliberately
# spread across Yuzhong and adjacent central districts; this is not a city-wide
# coverage claim.
CHONGQING_POINTS: tuple[tuple[str, float, float, str], ...] = (
    ("yuzhong_center", 29.56301, 106.55156, "yuzhong"),
    ("jiefangbei", 29.55960, 106.57430, "yuzhong"),
    ("hongyadong", 29.56380, 106.58040, "yuzhong"),
    ("chongqing_station", 29.59000, 106.58000, "yuzhong"),
    ("shapingba", 29.54000, 106.46000, "main_urban_districts"),
    ("nanping", 29.53000, 106.56000, "main_urban_districts"),
    ("jiangbei", 29.59000, 106.53000, "main_urban_districts"),
    ("daxigou", 29.57000, 106.56000, "yuzhong"),
)


def _load_points(path: Path) -> list[tuple[str, float, float, str]]:
    """Load ``name,lat,lon`` rows from a CSV file.

    An optional ``stratum`` column is carried into the report so a regular
    grid can be compared with district-centre and Yuzhong samples without
    affecting the provider request itself.
    """

    points: list[tuple[str, float, float]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"name", "lat", "lon"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("points CSV must contain name,lat,lon columns")
        for row in reader:
            name = (row.get("name") or "").strip()
            if not name:
                continue
            points.append((name, float(row["lat"]), float(row["lon"]), (row.get("stratum") or "custom").strip()))
    if not points:
        raise ValueError("points CSV contains no usable rows")
    return points


def _compact_photo(photo: dict[str, Any]) -> dict[str, Any]:
    """Keep only useful metadata; never download or inline image bytes."""

    fields = (
        "id",
        "lat",
        "lng",
        "distance",
        "sequenceId",
        "shotDate",
        "heading",
        "fileurlProc",
        "fileurlTh",
    )
    return {key: photo.get(key) for key in fields if key in photo}


def probe_point(
    session: requests.Session,
    name: str,
    lat: float,
    lon: float,
    stratum: str,
    radius_m: int,
    timeout_s: float,
) -> dict[str, Any]:
    """Probe one point and return a JSON-serializable summary."""

    response = session.get(
        API_URL,
        params={
            "lat": f"{lat:.7f}",
            "lng": f"{lon:.7f}",
            "radius": radius_m,
            # The v2 endpoint currently rejects the legacy ``ipp`` option;
            # this query is metadata-only and we only retain three samples.
            "page": 1,
            "join": "sequence",
            "orderBy": "distance",
            "orderDirection": "asc",
        },
        timeout=timeout_s,
    )
    result: dict[str, Any] = {
        "name": name,
        "stratum": stratum,
        "query_lat": lat,
        "query_lon": lon,
        "radius_m": radius_m,
        "http_status": response.status_code,
        "api_url": response.url,
    }
    try:
        payload = response.json()
    except ValueError:
        result.update({"ok": False, "error": "non_json_response", "body_prefix": response.text[:200]})
        return result

    status = payload.get("status") if isinstance(payload, dict) else None
    status = status if isinstance(status, dict) else {}
    api_code = status.get("apiCode")
    data = ((payload.get("result") or {}).get("data") if isinstance(payload, dict) else None)
    data = data if isinstance(data, list) else []
    result.update(
        {
            "ok": response.status_code == 200 and api_code == 600,
            "api_code": api_code,
            "api_message": status.get("apiMessage"),
            "hit_count": len(data),
            "has_more": bool((payload.get("result") or {}).get("hasMoreData")) if isinstance(payload, dict) else False,
            "sample": [_compact_photo(item) for item in data[:3] if isinstance(item, dict)],
        }
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--points", type=Path, help="CSV with name,lat,lon[,stratum]; defaults to central Chongqing smoke-test points")
    parser.add_argument("--radius-m", type=int, default=500, help="Search radius in metres (1..500; default: 500)")
    parser.add_argument("--delay-s", type=float, default=0.4, help="Delay between requests (default: 0.4)")
    parser.add_argument("--timeout-s", type=float, default=30, help="Per-request timeout (default: 30)")
    parser.add_argument("--output", type=Path, help="Write JSON report to this path; stdout otherwise")
    parser.add_argument("--summary-csv", type=Path, help="Optional CSV summary (one row per queried point)")
    args = parser.parse_args(argv)
    if not 1 <= args.radius_m <= 500:
        parser.error("--radius-m must be between 1 and 500 (KartaView API limit)")
    points = _load_points(args.points) if args.points else list(CHONGQING_POINTS)

    session = requests.Session()
    session.headers.update({"User-Agent": "photo-privacy-audit/kartaview-probe (metadata-only)"})
    report = {
        "provider": "KartaView",
        "endpoint": API_URL,
        "metadata_only": True,
        "points_requested": len(points),
        "results": [],
    }
    for index, (name, lat, lon, stratum) in enumerate(points):
        try:
            report["results"].append(probe_point(session, name, lat, lon, stratum, args.radius_m, args.timeout_s))
        except requests.RequestException as exc:
            report["results"].append(
                {
                    "name": name,
                    "stratum": stratum,
                    "query_lat": lat,
                    "query_lon": lon,
                    "radius_m": args.radius_m,
                    "ok": False,
                    "error": type(exc).__name__,
                    "message": str(exc),
                }
            )
        if index + 1 < len(points):
            time.sleep(max(0.0, args.delay_s))

    report["successful_points"] = sum(1 for item in report["results"] if item.get("ok"))
    report["points_with_hits"] = sum(1 for item in report["results"] if item.get("hit_count", 0) > 0)
    report["error_points"] = sum(1 for item in report["results"] if item.get("error") or (item.get("ok") is False and item.get("api_code") != 600))
    report["empty_success_points"] = sum(1 for item in report["results"] if item.get("ok") and item.get("hit_count", 0) == 0)
    if args.summary_csv:
        args.summary_csv.parent.mkdir(parents=True, exist_ok=True)
        with args.summary_csv.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["name", "stratum", "query_lat", "query_lon", "radius_m", "http_status", "api_code", "ok", "hit_count", "error", "message"])
            writer.writeheader()
            for item in report["results"]:
                writer.writerow({key: item.get(key, "") for key in writer.fieldnames})
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    else:
        sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

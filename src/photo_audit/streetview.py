"""Auditable street-level imagery provider adapters.

The adapters in this module deliberately use documented APIs only.  They do
not crawl map tiles, replay private endpoints, or bypass provider access
controls.  KartaView is usable without a token (coverage is uneven); Mapillary
and Baidu require credentials supplied through the environment.  Every fetch
returns a manifest row with source, licence and request provenance so that a
download can be reproduced and reviewed locally.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .common import AuditError, write_csv, write_json


# The current public API is the 2.0 photo endpoint.  The legacy 1.0
# openstreetcam.org endpoint is retained only as an explicit override for
# archived deployments; it must not be the default because it can return 404.
KARTAVIEW_ENDPOINT = "https://api.kartaview.org/2.0/photo/"
MAPILLARY_ENDPOINT = "https://graph.mapillary.com/images"
BAIDU_STATIC_ENDPOINT = "https://api.map.baidu.com/panorama/v2"
PANORAMAX_ENDPOINT = "https://api.panoramax.xyz/api/search"
KARTAVIEW_LICENSE = "CC BY-SA"
KARTAVIEW_LICENSE_URL = "https://kartaview.org/terms"
MAPILLARY_LICENSE = "Mapillary license (provider metadata)"
MAPILLARY_LICENSE_URL = "https://www.mapillary.com/app/legal/terms-of-service"
BAIDU_LICENSE = "Baidu Maps API terms (credential owner must verify)"
BAIDU_LICENSE_URL = "https://lbsyun.baidu.com/index.php?title=open/agreements"
PANORAMAX_LICENSE = "Per-item STAC license"
PANORAMAX_LICENSE_URL = "https://panoramax.xyz/"

_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")
_MANIFEST_FIELDS = [
    "provider", "asset_id", "source_id", "sequence_id", "query_id", "lat", "lon",
    "heading", "captured_at", "image_url", "local_path", "source_url", "license",
    "license_url", "creator", "source_provider", "source_collection",
    "authorization_status", "dataset", "content_sha256",
]


@dataclass(frozen=True)
class Query:
    """A bounded geographic probe supplied by the user."""

    query_id: str
    lat: float
    lon: float
    radius_m: float = 100.0
    heading: float | None = None
    # Optional explicit WGS84 bbox (min_lon, min_lat, max_lon, max_lat).
    # Providers that use a radial search ignore it; Panoramax uses it directly.
    bbox: tuple[float, float, float, float] | None = None

    def validate(self) -> None:
        if not self.query_id or len(self.query_id) > 128:
            raise AuditError("streetview query_id must be non-empty and at most 128 characters.")
        if not -90 <= self.lat <= 90 or not -180 <= self.lon <= 180:
            raise AuditError("streetview query coordinates are outside valid bounds.")
        if not 1 <= self.radius_m <= 5000:
            raise AuditError("streetview radius_m must be between 1 and 5000.")
        if self.heading is not None and not 0 <= self.heading <= 360:
            raise AuditError("streetview heading must be between 0 and 360 degrees.")
        if self.bbox is not None:
            if len(self.bbox) != 4:
                raise AuditError("streetview bbox must contain four coordinates.")
            min_lon, min_lat, max_lon, max_lat = self.bbox
            if not (-180 <= min_lon < max_lon <= 180 and -90 <= min_lat < max_lat <= 90):
                raise AuditError("streetview bbox coordinates are outside valid bounds.")


@dataclass
class FetchResult:
    rows: list[dict]
    failures: list[dict]
    requests: int
    provider: str
    endpoint: str
    license: str
    license_url: str


def _float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_filename(value: str, suffix: str = ".jpg") -> str:
    value = _SAFE_ID.sub("_", str(value)).strip("._") or "image"
    return value[:160] + suffix


def _dedupe_rows(rows: list[dict]) -> list[dict]:
    """Drop repeated provider assets returned by overlapping spatial queries."""
    unique = []
    seen = set()
    for row in rows:
        key = str(row.get("asset_id") or row.get("source_id") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        unique.append(row)
    return unique


def _request_bytes(request: Request, *, timeout: float, max_bytes: int) -> tuple[bytes, str]:
    """Read a bounded response and return bytes plus content type."""
    try:
        with urlopen(request, timeout=timeout) as response:  # nosec B310: URL is provider allowlisted
            content_type = str(response.headers.get("Content-Type", ""))
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise AuditError("streetview response exceeds the configured byte limit.")
            data = response.read(max_bytes + 1)
    except HTTPError as exc:
        raise AuditError(f"streetview provider returned HTTP {exc.code}.") from None
    except URLError as exc:
        raise AuditError("streetview provider request failed.") from exc
    if len(data) > max_bytes:
        raise AuditError("streetview response exceeds the configured byte limit.")
    return data, content_type


def _request_json(url: str, *, form: dict | None = None, json_body: dict | None = None,
                 headers: dict | None = None,
                 timeout: float = 30.0, max_bytes: int = 25 * 1024 * 1024) -> dict:
    if form is not None and json_body is not None:
        raise AuditError("streetview request cannot contain both form and JSON payloads.")
    data = None
    request_headers = {"User-Agent": "photo-privacy-audit/0.1 (local research)"}
    if headers:
        request_headers.update(headers)
    if form is not None:
        data = urlencode(form).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = Request(url, data=data, headers=request_headers, method="POST" if data else "GET")
    payload, _ = _request_bytes(request, timeout=timeout, max_bytes=max_bytes)
    try:
        result = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuditError("streetview provider returned invalid JSON.") from exc
    if not isinstance(result, dict):
        raise AuditError("streetview provider returned an unexpected JSON shape.")
    return result


def _download_image(url: str, destination: Path, *, timeout: float, max_bytes: int) -> str:
    """Download one image from a provider allowlist and return its digest."""
    parsed = url.split("/", 3)
    if len(parsed) < 3 or parsed[0] != "https:":
        raise AuditError("streetview image URL must use HTTPS.")
    host = parsed[2].lower()
    def allowed(domain: str) -> bool:
        return host == domain or host.endswith("." + domain)

    # KartaView may return storage.openstreetcam.org or its CDN; Mapillary
    # thumbnails commonly live on an fbcdn.net host; Baidu serves from its
    # own API domain.  Use label-boundary checks to avoid suffix matches.
    if not (allowed("openstreetcam.org") or allowed("kartaview.org")
            or allowed("mapillary.com") or allowed("fbcdn.net")
            or allowed("baidu.com") or allowed("panoramax.openstreetmap.fr")
            or allowed("api.panoramax.xyz") or allowed("panoramax.xyz")
            or allowed("panoramax.mapcomplete.org") or allowed("panoramax.ign.fr")
            or allowed("images.panoramax.basi.re")):
        raise AuditError("streetview image host is not allowlisted.")
    data, content_type = _request_bytes(Request(url, headers={
        "User-Agent": "photo-privacy-audit/0.1 (local research)"
    }), timeout=timeout, max_bytes=max_bytes)
    if content_type and not content_type.lower().startswith("image/"):
        raise AuditError("streetview image response is not an image.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _redact_query_secret(url: str, names: set[str]) -> str:
    """Remove credential query parameters before a URL enters a manifest."""
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key.lower() not in {name.lower() for name in names}]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _kartaview_url(path: str) -> str:
    """Resolve the storage path returned by KartaView without trusting a host."""
    path = str(path or "").lstrip("/")
    if not path or not path.startswith("storage"):
        return ""
    host, _, rest = path.partition("/")
    if not re.fullmatch(r"storage[0-9]+", host):
        return ""
    return f"https://{host}.openstreetcam.org/{rest}"


class KartaViewProvider:
    name = "kartaview"
    endpoint = KARTAVIEW_ENDPOINT
    license = KARTAVIEW_LICENSE
    license_url = KARTAVIEW_LICENSE_URL

    def __init__(self, *, endpoint: str | None = None, timeout: float = 30.0,
                 max_response_bytes: int = 25 * 1024 * 1024):
        self.endpoint = endpoint or self.endpoint
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes

    def query(self, query: Query, *, limit: int = 100) -> list[dict]:
        query.validate()
        if not 1 <= limit <= 1000:
            raise AuditError("streetview limit must be between 1 and 1000.")
        if self.endpoint.rstrip("/").endswith("/2.0/photo"):
            # KartaView's current public endpoint is a GET and caps the search
            # radius at 500 m.  A successful HTTP 200 is not sufficient:
            # status.apiCode must be 600.
            if query.radius_m > 500:
                raise AuditError("KartaView 2.0 radius_m must be at most 500.")
            url = self.endpoint + "?" + urlencode({
                "lat": f"{query.lat:.7f}", "lng": f"{query.lon:.7f}",
                # The API parses radius with strconv.ParseInt; do not send
                # the otherwise harmless-looking ``250.0`` form.
                "radius": str(int(round(query.radius_m))), "page": 1,
                "join": "sequence", "orderBy": "distance", "orderDirection": "asc",
            })
            payload = _request_json(url, timeout=self.timeout, max_bytes=self.max_response_bytes)
            status = payload.get("status")
            if not isinstance(status, dict) or status.get("apiCode") != 600:
                raise AuditError("KartaView returned an API error.")
            items = ((payload.get("result") or {}).get("data")
                     if isinstance(payload.get("result"), dict) else [])
        else:
            # Compatibility path for an operator-supplied legacy endpoint.
            payload = _request_json(self.endpoint, form={
                "lat": f"{query.lat:.7f}", "lng": f"{query.lon:.7f}",
                "radius": f"{query.radius_m:.1f}",
            }, timeout=self.timeout, max_bytes=self.max_response_bytes)
            status = payload.get("status")
            if isinstance(status, dict) and int(status.get("httpCode", 200) or 200) >= 400:
                raise AuditError("KartaView returned an API error.")
            items = payload.get("currentPageItems", [])
        if not isinstance(items, list):
            raise AuditError("KartaView returned an unexpected item list.")
        rows = []
        for item in items[:limit]:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            # Prefer the provider's bounded thumbnail for gallery ingestion;
            # full processed frames can be tens of megabytes and add little
            # retrieval value at this stage.
            image_path = (item.get("fileurlTh") or item.get("fileurlLTh")
                          or item.get("fileurlProc") or item.get("fileurl")
                          or item.get("th_name")
                          or item.get("lth_name") or item.get("name"))
            image_url = image_path if str(image_path).startswith("https://") else _kartaview_url(image_path)
            lat = _float(item.get("lat"))
            lon = _float(item.get("lng"))
            if lat is None or lon is None or not image_url:
                continue
            sequence_id = str(item.get("sequenceId") or item.get("sequence_id", ""))
            user = item.get("user") if isinstance(item.get("user"), dict) else {}
            creator = item.get("username") or user.get("name") or user.get("username") or ""
            rows.append({
                "provider": self.name,
                "asset_id": f"kartaview-{item['id']}",
                "source_id": str(item["id"]),
                "sequence_id": sequence_id,
                "query_id": query.query_id,
                "lat": f"{lat:.8f}", "lon": f"{lon:.8f}",
                "heading": item.get("heading", ""),
                "captured_at": item.get("shotDate") or item.get("shot_date") or "",
                "image_url": image_url,
                "local_path": "",
                "source_url": f"https://kartaview.org/details/{sequence_id}/{item['id']}",
                "license": self.license, "license_url": self.license_url,
                "creator": creator,
                "authorization_status": "public_api",
                "dataset": "kartaview-public",
                "content_sha256": "",
            })
        return rows

    def fetch(self, queries: Iterable[Query], *, limit: int = 100, delay_s: float = 1.0,
              download_dir: Path | None = None, max_image_bytes: int = 20 * 1024 * 1024) -> FetchResult:
        rows, failures, requests = [], [], 0
        for query in queries:
            requests += 1
            try:
                found = self.query(query, limit=limit)
                for row in found:
                    if download_dir is not None:
                        try:
                            target = download_dir / _safe_filename(row["asset_id"])
                            row["content_sha256"] = _download_image(
                                row["image_url"], target, timeout=self.timeout, max_bytes=max_image_bytes
                            )
                            row["local_path"] = target.name
                        except (AuditError, OSError) as exc:
                            failures.append({"query_id": query.query_id, "source_id": row.get("source_id"),
                                              "stage": "download", "error": str(exc)})
                            continue
                    rows.append(row)
            except (AuditError, OSError) as exc:
                failures.append({"query_id": query.query_id, "stage": "query", "error": str(exc)})
            if delay_s:
                time.sleep(delay_s)
        rows = _dedupe_rows(rows)
        return FetchResult(rows, failures, requests, self.name, self.endpoint, self.license, self.license_url)


class MapillaryProvider:
    name = "mapillary"
    endpoint = MAPILLARY_ENDPOINT
    license = MAPILLARY_LICENSE
    license_url = MAPILLARY_LICENSE_URL

    def __init__(self, *, token: str | None = None, endpoint: str | None = None, timeout: float = 30.0):
        self.token = token or os.environ.get("MAPILLARY_ACCESS_TOKEN")
        if not self.token:
            raise AuditError("Mapillary requires MAPILLARY_ACCESS_TOKEN; no token was sent.")
        self.endpoint = endpoint or self.endpoint
        self.timeout = timeout

    def query(self, query: Query, *, limit: int = 100) -> list[dict]:
        query.validate()
        if not 1 <= limit <= 1000:
            raise AuditError("streetview limit must be between 1 and 1000.")
        # Approximate a circle with a small bounding box.  This is a documented
        # Graph API query, not an undocumented tile or web-app endpoint.
        dlat = query.radius_m / 111_320.0
        dlon = query.radius_m / (111_320.0 * max(0.1, abs(math.cos(math.radians(query.lat)))))
        bbox = f"{query.lon-dlon:.7f},{query.lat-dlat:.7f},{query.lon+dlon:.7f},{query.lat+dlat:.7f}"
        fields = "id,geometry,compass_angle,captured_at,thumb_1024_url,creator"
        url = self.endpoint + "?" + urlencode({"bbox": bbox,
                                                  "fields": fields, "limit": min(limit, 2000)})
        # Keep the access token out of URLs, logs and provenance.  Mapillary's
        # documented OAuth header is also safer for user-scoped tokens.
        payload = _request_json(url, headers={"Authorization": f"OAuth {self.token}"},
                                timeout=self.timeout)
        items = payload.get("data", [])
        if not isinstance(items, list):
            raise AuditError("Mapillary returned an unexpected item list.")
        rows = []
        for item in items[:limit]:
            geom = item.get("geometry", {}) if isinstance(item, dict) else {}
            coords = geom.get("coordinates", []) if isinstance(geom, dict) else []
            if not item.get("id") or len(coords) < 2:
                continue
            lon, lat = _float(coords[0]), _float(coords[1])
            if lat is None or lon is None:
                continue
            creator = item.get("creator") if isinstance(item.get("creator"), dict) else {}
            rows.append({
                "provider": self.name, "asset_id": f"mapillary-{item['id']}",
                "source_id": str(item["id"]), "sequence_id": "", "query_id": query.query_id,
                "lat": f"{lat:.8f}", "lon": f"{lon:.8f}",
                "heading": item.get("compass_angle", ""), "captured_at": item.get("captured_at", ""),
                "image_url": item.get("thumb_1024_url", ""), "local_path": "",
                "source_url": f"https://www.mapillary.com/app/?pKey={item['id']}",
                "license": self.license, "license_url": self.license_url,
                "creator": creator.get("username", ""), "authorization_status": "public_api",
                "dataset": "mapillary-public", "content_sha256": "",
            })
        return rows

    def fetch(self, queries: Iterable[Query], *, limit: int = 100, delay_s: float = 1.0,
              download_dir: Path | None = None, max_image_bytes: int = 20 * 1024 * 1024) -> FetchResult:
        rows, failures, requests = [], [], 0
        for query in queries:
            requests += 1
            try:
                found = self.query(query, limit=limit)
                for row in found:
                    if download_dir is not None and row.get("image_url"):
                        try:
                            target = download_dir / _safe_filename(row["asset_id"])
                            row["content_sha256"] = _download_image(row["image_url"], target,
                                                                       timeout=self.timeout,
                                                                       max_bytes=max_image_bytes)
                            row["local_path"] = target.name
                        except (AuditError, OSError) as exc:
                            failures.append({"query_id": query.query_id, "source_id": row.get("source_id"),
                                              "stage": "download", "error": str(exc)})
                            continue
                    rows.append(row)
            except (AuditError, OSError) as exc:
                failures.append({"query_id": query.query_id, "stage": "query", "error": str(exc)})
            if delay_s:
                time.sleep(delay_s)
        rows = _dedupe_rows(rows)
        return FetchResult(rows, failures, requests, self.name, self.endpoint, self.license, self.license_url)


class BaiduProvider:
    """Baidu panoramic static-image adapter for explicitly supplied points.

    Baidu's API is not a public search dump.  This adapter therefore emits one
    image request per user-provided coordinate and never crawls tiles or nearby
    points.  The API key is read from ``BAIDU_MAP_AK`` and omitted from reports.
    """

    name = "baidu"
    endpoint = BAIDU_STATIC_ENDPOINT
    license = BAIDU_LICENSE
    license_url = BAIDU_LICENSE_URL

    def __init__(self, *, ak: str | None = None, endpoint: str | None = None, timeout: float = 30.0):
        self.ak = ak or os.environ.get("BAIDU_MAP_AK")
        if not self.ak:
            raise AuditError("Baidu requires BAIDU_MAP_AK; no key was sent.")
        self.endpoint = endpoint or self.endpoint
        self.timeout = timeout

    def query(self, query: Query, *, limit: int = 1) -> list[dict]:
        query.validate()
        if limit != 1:
            raise AuditError("Baidu static panorama accepts one explicit point per query.")
        params = {"ak": self.ak, "width": 1024, "height": 768,
                  "location": f"{query.lon:.7f},{query.lat:.7f}"}
        if query.heading is not None:
            params["heading"] = f"{query.heading:.1f}"
        image_url = self.endpoint + "?" + urlencode(params)
        return [{
            "provider": self.name, "asset_id": f"baidu-{query.query_id}",
            "source_id": query.query_id, "sequence_id": "", "query_id": query.query_id,
            "lat": f"{query.lat:.8f}", "lon": f"{query.lon:.8f}",
            "heading": query.heading if query.heading is not None else "",
            # Keep the credential-bearing URL private to this in-process
            # result.  The public manifest URL is redacted; fetch() uses the
            # private value only for the actual image request.
            "captured_at": "", "image_url": _redact_query_secret(image_url, {"ak"}),
            "_request_url": image_url, "local_path": "",
            "source_url": "https://lbsyun.baidu.com/index.php?title=viewstatic/api",
            "license": self.license, "license_url": self.license_url, "creator": "",
            "authorization_status": "credentialed_api", "dataset": "baidu-pano-static",
            "content_sha256": "",
        }]

    def fetch(self, queries: Iterable[Query], *, limit: int = 1, delay_s: float = 1.0,
              download_dir: Path | None = None, max_image_bytes: int = 20 * 1024 * 1024) -> FetchResult:
        rows, failures, requests = [], [], 0
        for query in queries:
            requests += 1
            try:
                found = self.query(query, limit=limit)
                for row in found:
                    if download_dir is not None:
                        try:
                            target = download_dir / _safe_filename(row["asset_id"])
                            row["content_sha256"] = _download_image(row.get("_request_url", row["image_url"]), target,
                                                                       timeout=self.timeout,
                                                                       max_bytes=max_image_bytes)
                            row["local_path"] = target.name
                        except (AuditError, OSError) as exc:
                            failures.append({"query_id": query.query_id, "source_id": row.get("source_id"),
                                              "stage": "download", "error": str(exc)})
                            continue
                    # Never write the credential-bearing static-image URL to
                    # the manifest, even when running metadata-only mode.
                    row["image_url"] = _redact_query_secret(row["image_url"], {"ak"})
                    rows.append(row)
            except (AuditError, OSError) as exc:
                failures.append({"query_id": query.query_id, "stage": "query", "error": str(exc)})
            if delay_s:
                time.sleep(delay_s)
        rows = _dedupe_rows(rows)
        return FetchResult(rows, failures, requests, self.name, self.endpoint, self.license, self.license_url)


class PanoramaxProvider:
    """Panoramax STAC search adapter for public, geotagged imagery.

    Panoramax exposes a documented STAC search endpoint.  The adapter converts
    the existing bounded center/radius query into a bounding box, retains the
    per-item license and source collection metadata, and uses only the
    thumbnail asset when downloading a local gallery.  It does not crawl
    collection pages or infer a search area from a private image.
    """

    name = "panoramax"
    endpoint = PANORAMAX_ENDPOINT
    license = PANORAMAX_LICENSE
    license_url = PANORAMAX_LICENSE_URL

    def __init__(self, *, endpoint: str | None = None, timeout: float = 30.0,
                 max_response_bytes: int = 25 * 1024 * 1024):
        self.endpoint = endpoint or self.endpoint
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes

    @staticmethod
    def _bbox(query: Query) -> list[float]:
        if query.bbox is not None:
            return list(query.bbox)
        # Convert a bounded radius to a conservative WGS84 bbox.  Clamp the
        # longitude denominator near the poles to avoid division by zero.
        dlat = query.radius_m / 111_320.0
        dlon = query.radius_m / (111_320.0 * max(0.1, abs(math.cos(math.radians(query.lat)))))
        return [query.lon - dlon, query.lat - dlat, query.lon + dlon, query.lat + dlat]

    @staticmethod
    def _link(feature: dict, rel: str) -> str:
        links = feature.get("links", [])
        if not isinstance(links, list):
            return ""
        for link in links:
            if isinstance(link, dict) and link.get("rel") == rel and isinstance(link.get("href"), str):
                return link["href"]
        return ""

    def query(self, query: Query, *, limit: int = 100) -> list[dict]:
        query.validate()
        if not 1 <= limit <= 1000:
            raise AuditError("streetview limit must be between 1 and 1000.")
        payload = _request_json(
            self.endpoint,
            json_body={"bbox": self._bbox(query), "limit": limit},
            timeout=self.timeout,
            max_bytes=self.max_response_bytes,
        )
        features = payload.get("features", [])
        if not isinstance(features, list):
            raise AuditError("Panoramax returned an unexpected feature list.")
        rows = []
        for feature in features[:limit]:
            if not isinstance(feature, dict) or not feature.get("id"):
                continue
            geometry = feature.get("geometry")
            coords = geometry.get("coordinates", []) if isinstance(geometry, dict) else []
            if not isinstance(coords, list) or len(coords) < 2:
                continue
            lon, lat = _float(coords[0]), _float(coords[1])
            if lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180:
                continue
            properties = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
            assets = feature.get("assets") if isinstance(feature.get("assets"), dict) else {}
            asset = assets.get("thumb") if isinstance(assets.get("thumb"), dict) else assets.get("sd")
            if not isinstance(asset, dict) or not isinstance(asset.get("href"), str):
                continue
            image_url = asset["href"]
            if not image_url.startswith("https://"):
                continue
            providers = feature.get("providers") if isinstance(feature.get("providers"), list) else []
            source_provider = ""
            if providers and isinstance(providers[0], dict):
                source_provider = str(providers[0].get("name") or providers[0].get("id") or "")
            collection = str(feature.get("collection") or "")
            source_id = str(feature["id"])
            license_url = self._link(feature, "license")
            item_license = str(properties.get("license") or self.license)
            rows.append({
                "provider": self.name,
                "asset_id": f"panoramax-{source_id}",
                "source_id": source_id,
                "sequence_id": collection,
                "query_id": query.query_id,
                "lat": f"{lat:.8f}", "lon": f"{lon:.8f}",
                "heading": properties.get("view:azimuth", ""),
                "captured_at": properties.get("datetime") or properties.get("datetimetz") or "",
                "image_url": image_url,
                "local_path": "",
                "source_url": self._link(feature, "self") or self.endpoint,
                "license": item_license,
                "license_url": license_url or self.license_url,
                "creator": source_provider,
                "source_provider": source_provider,
                "source_collection": collection,
                "authorization_status": "public_api",
                "dataset": "panoramax-public",
                "content_sha256": "",
            })
        return rows

    def fetch(self, queries: Iterable[Query], *, limit: int = 100, delay_s: float = 1.0,
              download_dir: Path | None = None, max_image_bytes: int = 20 * 1024 * 1024) -> FetchResult:
        rows, failures, requests = [], [], 0
        for query in queries:
            requests += 1
            try:
                found = self.query(query, limit=limit)
                for row in found:
                    if download_dir is not None:
                        try:
                            target = download_dir / _safe_filename(row["asset_id"])
                            row["content_sha256"] = _download_image(
                                row["image_url"], target, timeout=self.timeout, max_bytes=max_image_bytes
                            )
                            row["local_path"] = target.name
                        except (AuditError, OSError) as exc:
                            failures.append({"query_id": query.query_id, "source_id": row.get("source_id"),
                                              "stage": "download", "error": str(exc)})
                            continue
                    rows.append(row)
            except (AuditError, OSError) as exc:
                failures.append({"query_id": query.query_id, "stage": "query", "error": str(exc)})
            if delay_s:
                time.sleep(delay_s)
        rows = _dedupe_rows(rows)
        return FetchResult(rows, failures, requests, self.name, self.endpoint, self.license, self.license_url)


def load_queries(path: str | Path) -> list[Query]:
    """Load and validate an explicit query CSV (never infer coordinates)."""
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise AuditError("streetview query CSV is empty.")
    if len(rows) > 10000:
        raise AuditError("streetview query CSV exceeds the 10000-row safety limit.")
    queries = []
    for index, row in enumerate(rows, 1):
        try:
            bbox_keys = ("min_lon", "min_lat", "max_lon", "max_lat")
            bbox_values = [str(row.get(key, "")).strip() for key in bbox_keys]
            bbox = None
            if any(bbox_values):
                if not all(bbox_values):
                    raise AuditError("streetview bbox requires min_lon,min_lat,max_lon,max_lat.")
                bbox = tuple(float(value) for value in bbox_values)
            query = Query(str(row.get("query_id") or f"q-{index}"), float(row["lat"]), float(row["lon"]),
                          float(row.get("radius_m") or 100),
                          None if not str(row.get("heading", "")).strip() else float(row["heading"]),
                          bbox=bbox)
            query.validate()
        except (KeyError, TypeError, ValueError, AuditError) as exc:
            raise AuditError(f"streetview query row {index} is invalid.") from exc
        queries.append(query)
    return queries


def provider_for(name: str, **kwargs):
    providers = {"kartaview": KartaViewProvider, "mapillary": MapillaryProvider,
                 "baidu": BaiduProvider, "panoramax": PanoramaxProvider}
    try:
        return providers[name.lower()](**kwargs)
    except KeyError:
        raise AuditError("Unknown streetview provider; choose kartaview, mapillary, baidu or panoramax.") from None


def write_fetch_result(result: FetchResult, output: str | Path, query_file: str | Path,
                       *, download_dir: str | Path | None = None) -> None:
    output = Path(output).resolve()
    if output.exists():
        raise AuditError("Streetview manifest already exists; choose a new path.")
    output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(output, _MANIFEST_FIELDS, result.rows)
    sidecar = output.with_name(output.name + ".provenance.json")
    if sidecar.exists():
        raise AuditError("Streetview provenance already exists; choose a new output path.")
    write_json(sidecar, {
        "schema_version": "streetview-fetch/v1", "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "provider": result.provider, "endpoint": result.endpoint,
        "query_csv_sha256": hashlib.sha256(Path(query_file).read_bytes()).hexdigest(),
        "download_dir": str(Path(download_dir).name) if download_dir else None,
        "license": result.license, "license_url": result.license_url,
        "request_count": result.requests, "row_count": len(result.rows),
        "failure_count": len(result.failures), "failures": result.failures,
        "coordinates_included": True,
    })


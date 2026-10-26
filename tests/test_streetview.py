import json
from pathlib import Path

import pytest

from photo_audit.common import AuditError
from photo_audit.streetview import (
    BaiduProvider,
    KartaViewProvider,
    MapillaryProvider,
    PanoramaxProvider,
    Query,
    load_queries,
    provider_for,
    write_fetch_result,
)


class _Response:
    def __init__(self, payload, content_type="application/json"):
        self.payload = payload
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, count=-1):
        return self.payload


def test_kartaview_query_normalizes_public_record(monkeypatch):
    payload = json.dumps({"status": {"apiCode": 600, "httpCode": 200}, "result": {"data": [{
        "id": "42", "sequenceId": "7", "lat": "29.563", "lng": "106.552",
        "fileurlTh": "https://storage13.openstreetcam.org/files/photo/2024/1/1/th/a.jpg",
        "fileurlProc": "https://storage13.openstreetcam.org/files/photo/2024/1/1/proc/a.jpg",
        "shotDate": "2024-01-01", "heading": "12", "user": {"name": "camera-owner"},
    }]}}).encode()
    monkeypatch.setattr("photo_audit.streetview.urlopen", lambda *args, **kwargs: _Response(payload))
    rows = KartaViewProvider().query(Query("cq", 29.563, 106.552, 100), limit=1)
    assert rows[0]["asset_id"] == "kartaview-42"
    assert rows[0]["lat"] == "29.56300000"
    assert rows[0]["image_url"] == "https://storage13.openstreetcam.org/files/photo/2024/1/1/th/a.jpg"
    assert rows[0]["source_url"].endswith("/7/42")
    assert rows[0]["creator"] == "camera-owner"
    assert rows[0]["authorization_status"] == "public_api"


def test_kartaview_legacy_shape_requires_explicit_endpoint(monkeypatch):
    payload = json.dumps({"status": {"httpCode": 200}, "currentPageItems": [{
        "id": "42", "sequence_id": "7", "lat": "29.563", "lng": "106.552",
        "th_name": "storage13/files/photo/2024/1/1/th/a.jpg", "shot_date": "2024-01-01",
        "heading": "12", "username": "camera-owner",
    }]}).encode()
    monkeypatch.setattr("photo_audit.streetview.urlopen", lambda *args, **kwargs: _Response(payload))
    rows = KartaViewProvider(endpoint="https://api.openstreetcam.org/1.0/list/nearby-photos/").query(
        Query("cq", 29.563, 106.552, 100), limit=1
    )
    assert rows[0]["source_id"] == "42"


def test_provider_credentials_are_required(monkeypatch):
    monkeypatch.delenv("MAPILLARY_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("BAIDU_MAP_AK", raising=False)
    with pytest.raises(AuditError, match="MAPILLARY_ACCESS_TOKEN"):
        MapillaryProvider()
    with pytest.raises(AuditError, match="BAIDU_MAP_AK"):
        BaiduProvider()
    assert provider_for("kartaview").name == "kartaview"


def test_mapillary_token_is_sent_in_header_not_url(monkeypatch):
    monkeypatch.setenv("MAPILLARY_ACCESS_TOKEN", "test-token")
    payload = json.dumps({"data": []}).encode()
    captured = {}

    def fake_urlopen(request, **kwargs):
        captured["url"] = request.full_url
        captured["authorization"] = request.headers.get("Authorization")
        return _Response(payload)

    monkeypatch.setattr("photo_audit.streetview.urlopen", fake_urlopen)
    MapillaryProvider().query(Query("cq", 29.563, 106.552, 100), limit=1)
    assert "test-token" not in captured["url"]
    assert captured["authorization"] == "OAuth test-token"


def test_panoramax_stac_search_normalizes_feature_and_bbox(monkeypatch):
    payload = json.dumps({"features": [{
        "id": "feature-1",
        "collection": "collection-7",
        "geometry": {"type": "Point", "coordinates": [106.5521, 29.5631]},
        "links": [
            {"rel": "self", "href": "https://api.panoramax.xyz/api/items/feature-1"},
            {"rel": "collection", "href": "https://api.panoramax.xyz/api/collections/collection-7"},
            {"rel": "license", "href": "https://creativecommons.org/licenses/by-sa/4.0/"},
        ],
        "assets": {"thumb": {"href": "https://panoramax.openstreetmap.fr/thumb/feature-1.jpg"},
                   "sd": {"href": "https://panoramax.openstreetmap.fr/sd/feature-1.jpg"}},
        "providers": [{"id": "provider-1", "name": "Kajak", "roles": ["producer"]}],
        "properties": {"license": "CC-BY-SA-4.0", "datetime": "2024-03-01T12:05:06Z",
                        "view:azimuth": 42},
    }]}).encode()
    captured = {}

    def fake_urlopen(request, **kwargs):
        captured["url"] = request.full_url
        captured["method"] = request.method
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _Response(payload)

    monkeypatch.setattr("photo_audit.streetview.urlopen", fake_urlopen)
    rows = PanoramaxProvider().query(
        Query("cq", 29.563, 106.552, 100, bbox=(106.50, 29.50, 106.60, 29.60)), limit=2
    )
    assert captured["method"] == "POST"
    assert captured["body"]["limit"] == 2
    assert captured["body"]["bbox"] == [106.50, 29.50, 106.60, 29.60]
    assert rows[0]["asset_id"] == "panoramax-feature-1"
    assert rows[0]["source_id"] == "feature-1"
    assert rows[0]["sequence_id"] == "collection-7"
    assert rows[0]["source_provider"] == "Kajak"
    assert rows[0]["source_collection"] == "collection-7"
    assert rows[0]["license"] == "CC-BY-SA-4.0"
    assert rows[0]["license_url"].startswith("https://creativecommons.org/")
    assert rows[0]["image_url"].endswith("thumb/feature-1.jpg")
    assert provider_for("panoramax").name == "panoramax"


def test_panoramax_overlapping_queries_are_deduplicated(monkeypatch):
    payload = json.dumps({"features": [{
        "id": "same-feature", "collection": "collection-1",
        "geometry": {"type": "Point", "coordinates": [106.552, 29.563]},
        "assets": {"thumb": {"href": "https://panoramax.openstreetmap.fr/thumb/same.jpg"}},
        "properties": {"license": "CC-BY-SA-4.0"},
    }]}).encode()
    monkeypatch.setattr("photo_audit.streetview.urlopen", lambda *args, **kwargs: _Response(payload))
    result = PanoramaxProvider().fetch([
        Query("q1", 29.563, 106.552, 100), Query("q2", 29.5631, 106.5521, 100)
    ], limit=10, delay_s=0)
    assert len(result.rows) == 1


def test_query_csv_and_limits_are_validated(tmp_path):
    path = tmp_path / "queries.csv"
    path.write_text("query_id,lat,lon,radius_m\nq1,29.56,106.55,100\n", encoding="utf-8")
    assert load_queries(path)[0].query_id == "q1"
    with pytest.raises(AuditError):
        Query("bad", 91, 0).validate()
    path.write_text("query_id,lat,lon,radius_m\nq1,29.56,106.55,0\n", encoding="utf-8")
    with pytest.raises(AuditError):
        load_queries(path)
    path.write_text(
        "query_id,lat,lon,radius_m,min_lon,min_lat,max_lon,max_lat\n"
        "q1,29.55,106.55,100,106.50,29.50,106.60,29.60\n",
        encoding="utf-8",
    )
    assert load_queries(path)[0].bbox == (106.5, 29.5, 106.6, 29.6)


def test_query_limit_is_bounded(tmp_path):
    path = tmp_path / "queries.csv"
    path.write_text("query_id,lat,lon\nq1,29.56,106.55\n", encoding="utf-8")
    with pytest.raises(AuditError, match="limit"):
        KartaViewProvider().query(Query("q", 29.56, 106.55), limit=0)
    with pytest.raises(AuditError, match="limit"):
        MapillaryProvider(token="test-token").query(Query("q", 29.56, 106.55), limit=0)


def test_manifest_provenance_and_secret_redaction(tmp_path, monkeypatch):
    query = tmp_path / "q.csv"
    query.write_text("query_id,lat,lon\nq,29.56,106.55\n", encoding="utf-8")
    out = tmp_path / "manifest.csv"
    payload = json.dumps({"status": {"apiCode": 600, "httpCode": 200}, "result": {"data": []}}).encode()
    monkeypatch.setattr("photo_audit.streetview.urlopen", lambda *args, **kwargs: _Response(payload))
    result = KartaViewProvider().fetch([Query("q", 29.56, 106.55)], delay_s=0)
    write_fetch_result(result, out, query)
    sidecar = json.loads((tmp_path / "manifest.csv.provenance.json").read_text(encoding="utf-8"))
    assert sidecar["provider"] == "kartaview"
    assert "token" not in json.dumps(sidecar).lower()
    assert out.exists()


def test_baidu_key_is_redacted_from_manifest(tmp_path):
    provider = BaiduProvider(ak="secret-ak")
    queried = provider.query(Query("cq", 29.56, 106.55))[0]
    assert "secret-ak" not in queried["image_url"]
    assert "secret-ak" in queried["_request_url"]
    result = provider.fetch([Query("cq", 29.56, 106.55)], delay_s=0)
    output = tmp_path / "baidu.csv"
    query_file = tmp_path / "queries.csv"
    query_file.write_text("query_id,lat,lon\ncq,29.56,106.55\n", encoding="utf-8")
    write_fetch_result(result, output, query_file)
    assert "secret-ak" not in output.read_text(encoding="utf-8-sig")
    assert "ak=" not in output.read_text(encoding="utf-8-sig")


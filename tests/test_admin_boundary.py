import json

from photo_audit.admin_boundary import map_candidates


def test_admin_boundary_maps_candidate_to_region(tmp_path):
    boundary = tmp_path / "bounds.json"
    boundary.write_text(json.dumps({"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {"NAME_0": "China", "NAME_1": "重庆市", "NAME_2": "渝中区", "NAME_3": "示例街道"},
        "geometry": {"type": "Polygon", "coordinates": [[[106.5, 29.5], [106.6, 29.5], [106.6, 29.6], [106.5, 29.6], [106.5, 29.5]]]}
    }]}), encoding="utf-8")
    candidates = tmp_path / "candidates.json"
    candidates.write_text(json.dumps({"candidates": [{"rank": 1, "lat": 29.55, "lon": 106.55, "score": 0.8}]}), encoding="utf-8")
    rows = map_candidates(candidates, boundary)
    assert rows[0]["admin_region"] == "示例街道"
    assert rows[0]["city"] == "渝中区"

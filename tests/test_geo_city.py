import csv
import json

from photo_audit.geo_city import load_city_centers, load_candidates, match_cities


def test_geoclip_candidates_aggregate_to_chongqing(tmp_path):
    centers = tmp_path / "cities.csv"
    with centers.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["city_id", "city", "province", "lat", "lon"])
        writer.writeheader()
        writer.writerow({"city_id": "cq", "city": "重庆市", "province": "重庆市", "lat": "29.563", "lon": "106.552"})
        writer.writerow({"city_id": "sz", "city": "深圳市", "province": "广东省", "lat": "22.543", "lon": "114.058"})
    candidates = tmp_path / "candidates.json"
    candidates.write_text(json.dumps({"candidates": [
        {"rank": 1, "lat": 29.558, "lon": 106.588, "score": 0.8},
        {"rank": 2, "lat": 29.556, "lon": 106.579, "score": 0.5},
        {"rank": 3, "lat": 22.545, "lon": 114.110, "score": 0.9},
    ]}), encoding="utf-8")
    rows = match_cities(load_candidates(candidates), load_city_centers(centers), radius_km=20)
    assert rows[0]["city"] == "重庆市"
    assert rows[0]["candidate_count"] == 2

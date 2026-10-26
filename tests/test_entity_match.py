import csv

from photo_audit.entity_match import load_entity_manifest, match_ocr_rows


def test_chinese_ocr_matches_authorized_region(tmp_path):
    path = tmp_path / "entities.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["entity_id", "aliases", "city", "admin_region", "authorization_status"])
        writer.writeheader()
        writer.writerow({"entity_id": "demo-hotel", "aliases": "轻居酒店|ATOUR LIGHT",
                         "city": "重庆市", "admin_region": "渝中区", "authorization_status": "approved"})
    entities = load_entity_manifest(path, require_authorization=True)
    rows = match_ocr_rows([{"text": "轻居酒店 ATOUR LIGHT", "confidence": "0.9"}], entities)
    assert rows[0]["admin_region"] == "渝中区"
    assert rows[0]["city"] == "重庆市"

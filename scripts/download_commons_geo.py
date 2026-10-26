"""Download a small, licensed Wikimedia Commons geosearch reference set."""
from __future__ import annotations

import argparse
import csv
import html
import re
import time
from pathlib import Path

import requests


API = "https://commons.wikimedia.org/w/api.php"
FIELDS = ["asset_id", "relative_path", "lat", "lon", "source", "dataset", "license",
          "authorization_status", "title", "source_url", "provider", "provider_image_id",
          "license_url", "license_provenance"]


def clean(value):
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def download(city, lat, lon, radius, limit, output, namespace=6):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    images = output / "images"
    images.mkdir(exist_ok=True)
    params = {
        "action": "query", "format": "json", "generator": "geosearch",
        "ggscoord": f"{lat}|{lon}", "ggsradius": radius, "ggsnamespace": namespace,
        "ggslimit": limit, "ggsprimary": "all",
    }
    if namespace == 6:
        params.update({"prop": "coordinates|imageinfo", "iiprop": "url|extmetadata", "iiurlwidth": 512})
    else:
        params.update({"prop": "coordinates|pageimages", "piprop": "thumbnail|original", "pithumbsize": 512})
    response = requests.get(API, params=params, headers={"User-Agent": "photo-privacy-audit/0.1 research"}, timeout=30)
    response.raise_for_status()
    pages = response.json().get("query", {}).get("pages", {})
    rows = []
    for index, page in enumerate(pages.values(), 1):
        info = (page.get("imageinfo") or [{}])[0]
        thumbnail = page.get("thumbnail") or {}
        image_url = info.get("thumburl") or info.get("url") or thumbnail.get("source")
        coordinates = (page.get("coordinates") or [{}])[0]
        if not image_url or "lat" not in coordinates or "lon" not in coordinates:
            continue
        title = clean(page.get("title"))
        asset_id = f"commons-{page.get('pageid', index)}"
        suffix = Path(image_url.split("?")[0]).suffix.lower() or ".jpg"
        if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            suffix = ".jpg"
        relative = f"images/{asset_id}{suffix}"
        destination = output / relative
        if not destination.exists():
            image_url = image_url.split("?", 1)[0]
            time.sleep(0.6)
            try:
                blob = requests.get(image_url, headers={"User-Agent": "photo-privacy-audit/0.1 research (contact: local)"}, timeout=60)
                if blob.status_code == 429:
                    time.sleep(5)
                    blob = requests.get(image_url, headers={"User-Agent": "photo-privacy-audit/0.1 research (contact: local)"}, timeout=60)
                blob.raise_for_status()
                destination.write_bytes(blob.content)
            except requests.RequestException:
                continue
        metadata = info.get("extmetadata") or {}
        license_name = clean((metadata.get("LicenseShortName") or {}).get("value")) or ("unknown-page-license" if namespace != 6 else "")
        license_url = clean((metadata.get("LicenseUrl") or {}).get("value"))
        rows.append({"asset_id": asset_id, "relative_path": relative,
                     "lat": coordinates["lat"], "lon": coordinates["lon"],
                     "source": "Wikimedia Commons", "dataset": f"commons-geosearch:{city}:namespace{namespace}",
                     "license": license_name, "authorization_status": "public",
                     "title": title, "source_url": info.get("descriptionurl", ""),
                     "provider": "wikimedia-commons", "provider_image_id": str(page.get("pageid", "")),
                     "license_url": license_url, "license_provenance": "commons-imageinfo:LicenseShortName"})
    with (output / "reference_manifest.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Downloaded {len(rows)} geotagged reference image(s) to {output}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", default="Chongqing")
    parser.add_argument("--lat", type=float, default=29.563)
    parser.add_argument("--lon", type=float, default=106.552)
    parser.add_argument("--radius", type=int, default=10000)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--output", required=True)
    parser.add_argument("--namespace", type=int, choices=(0, 6), default=6,
                        help="6=file geosearch, 0=geotagged encyclopedia pages")
    args = parser.parse_args()
    if not 10 <= args.radius <= 10000 or not 1 <= args.limit <= 500:
        raise SystemExit("radius must be 10..10000 and limit 1..500")
    download(args.city, args.lat, args.lon, args.radius, args.limit, args.output, namespace=args.namespace)


if __name__ == "__main__":
    main()

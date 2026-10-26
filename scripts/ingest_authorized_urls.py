"""Ingest explicitly authorized image URLs into a local reference gallery.

The input CSV must carry license and authorization metadata. This tool does
not discover URLs, crawl social platforms, bypass access controls, or infer
coordinates from the downloaded image.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import time
from pathlib import Path

import requests

REQUIRED = {"asset_id", "url", "lat", "lon", "license", "authorization_status"}
ACCEPTED = {"approved", "authorized", "benchmark", "research", "public", "licensed", "consented", "allowlisted"}


def download(input_csv, output_dir, delay=1.0, max_bytes=20_000_000):
    output_dir = Path(output_dir).resolve()
    image_dir = output_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    with Path(input_csv).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED - set(reader.fieldnames or [])
        if missing:
            raise ValueError("authorized URL CSV is missing: " + ", ".join(sorted(missing)))
        source_rows = list(reader)
    manifest = []
    seen = set()
    for row in source_rows:
        asset_id = str(row["asset_id"]).strip()
        status = str(row["authorization_status"]).strip().lower()
        if not asset_id or asset_id in seen or status not in ACCEPTED:
            raise ValueError("asset_id must be unique and authorization_status must be approved")
        seen.add(asset_id)
        response = requests.get(row["url"], headers={"User-Agent": "photo-privacy-audit/0.1 authorized-ingest"},
                                timeout=60, stream=True)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        suffix = ".jpg" if "jpeg" in content_type else ".png" if "png" in content_type else ".webp" if "webp" in content_type else ".bin"
        if suffix == ".bin":
            raise ValueError(f"URL did not return a supported image content type: {asset_id}")
        destination = image_dir / f"{asset_id}{suffix}"
        digest = hashlib.sha256()
        total = 0
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    destination.unlink(missing_ok=True)
                    raise ValueError(f"image exceeds max_bytes: {asset_id}")
                digest.update(chunk)
                handle.write(chunk)
        manifest.append({**row, "relative_path": str(Path("images") / destination.name),
                         "sha256": digest.hexdigest(), "bytes": total})
        time.sleep(max(0.0, delay))
    fields = list(dict.fromkeys(list(source_rows[0]) + ["relative_path", "sha256", "bytes"]))
    with (output_dir / "reference_manifest.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(manifest)
    print(f"Ingested {len(manifest)} authorized image(s) into {output_dir}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input_csv")
    parser.add_argument("output_dir")
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--max-bytes", type=int, default=20_000_000)
    args = parser.parse_args()
    download(args.input_csv, args.output_dir, args.delay, args.max_bytes)


if __name__ == "__main__":
    main()

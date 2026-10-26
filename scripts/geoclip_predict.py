"""Run GeoCLIP locally and write top-k coordinate candidates."""
import json
import sys
from pathlib import Path

import torch
from geoclip import GeoCLIP


def main(image_path: str, output_path: str, top_k: int = 20):
    image_path = Path(image_path).resolve()
    output_path = Path(output_path).resolve()
    if not image_path.is_file():
        raise SystemExit(f"image not found: {image_path}")
    model = GeoCLIP.from_pretrained("VicenteVivanco/geoclip-geolocation").eval()
    if torch.cuda.is_available():
        model = model.cuda()
    coordinates, scores = model.predict(str(image_path), top_k=top_k)
    rows = []
    for rank, (coordinate, score) in enumerate(zip(coordinates.tolist(), scores.tolist()), 1):
        rows.append({"rank": rank, "lat": coordinate[0], "lon": coordinate[1], "score": score})
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps({"model": "GeoCLIP@VicenteVivanco/geoclip-geolocation",
                                       "image": str(image_path), "device": "cuda" if torch.cuda.is_available() else "cpu",
                                       "top_k": top_k, "candidates": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if len(sys.argv) not in {3, 4}:
        raise SystemExit("usage: geoclip_predict.py IMAGE OUTPUT_JSON [TOP_K]")
    main(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) == 4 else 20)

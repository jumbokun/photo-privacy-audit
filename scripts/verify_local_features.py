#!/usr/bin/env python3
"""Verify local visual geometry against an authorized reference directory.

This is a research-mode companion to embedding retrieval.  It never searches
the network or turns a match into an address claim; it only reports local
descriptor matches and RANSAC homography inliers for already selected images.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from photo_audit.common import AuditError, sha256


def _verify_pair(query_path: Path, reference_path: Path, *, ratio: float, ransac_px: float,
                 max_features: int) -> dict:
    try:
        import cv2
        import numpy as np
    except ImportError as exc:  # pragma: no cover - optional research dependency
        raise AuditError("local feature verification requires opencv-python and numpy.") from exc

    query = cv2.imread(str(query_path), cv2.IMREAD_GRAYSCALE)
    reference = cv2.imread(str(reference_path), cv2.IMREAD_GRAYSCALE)
    if query is None or reference is None:
        raise AuditError("Could not decode an image for local feature verification.")
    detector = cv2.SIFT_create(nfeatures=max_features) if hasattr(cv2, "SIFT_create") else cv2.ORB_create(nfeatures=max_features)
    key_q, desc_q = detector.detectAndCompute(query, None)
    key_r, desc_r = detector.detectAndCompute(reference, None)
    good = []
    if desc_q is not None and desc_r is not None and len(desc_q) and len(desc_r):
        norm = cv2.NORM_L2 if desc_q.dtype == np.float32 else cv2.NORM_HAMMING
        matcher = cv2.BFMatcher(norm)
        for pair in matcher.knnMatch(desc_q, desc_r, k=2):
            if len(pair) == 2 and pair[0].distance < ratio * pair[1].distance:
                good.append(pair[0])
    inliers = 0
    query_coverage = 0.0
    reference_coverage = 0.0
    homography = False
    if len(good) >= 4:
        src = np.float32([key_q[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
        dst = np.float32([key_r[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
        _, mask = cv2.findHomography(src, dst, cv2.RANSAC, ransac_px)
        if mask is not None:
            inlier_mask = mask.ravel().astype(bool)
            inliers = int(inlier_mask.sum())
            if inliers:
                def coverage(points, width, height):
                    xy = points[inlier_mask].reshape(-1, 2)
                    span_x = float(xy[:, 0].max() - xy[:, 0].min()) / max(1.0, width)
                    span_y = float(xy[:, 1].max() - xy[:, 1].min()) / max(1.0, height)
                    return max(0.0, min(1.0, span_x * span_y))
                query_coverage = coverage(src, query.shape[1], query.shape[0])
                reference_coverage = coverage(dst, reference.shape[1], reference.shape[0])
            homography = inliers >= 4
    return {
        "reference_filename": reference_path.name,
        "reference_sha256": sha256(reference_path),
        "good_matches": len(good),
        "inliers": inliers,
        "inlier_ratio": round(inliers / len(good), 6) if good else 0.0,
        "query_inlier_coverage": round(query_coverage, 6),
        "reference_inlier_coverage": round(reference_coverage, 6),
        "geometric_score": round(inliers * min(query_coverage, reference_coverage), 6),
        "homography": homography,
        "query_keypoints": len(key_q),
        "reference_keypoints": len(key_r),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query")
    parser.add_argument("reference_dir")
    parser.add_argument("output")
    parser.add_argument("--ratio", type=float, default=0.72)
    parser.add_argument("--ransac-px", type=float, default=5.0)
    parser.add_argument("--max-features", type=int, default=3000)
    args = parser.parse_args(argv)
    if not 0.1 <= args.ratio < 1.0 or args.ransac_px <= 0 or args.max_features < 100:
        parser.error("invalid local feature verification parameters")
    query = Path(args.query).resolve()
    root = Path(args.reference_dir).resolve()
    output = Path(args.output).resolve()
    if not query.is_file() or not root.is_dir():
        raise SystemExit("query and reference_dir must exist")
    refs = sorted(p for p in root.iterdir() if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})
    if not refs:
        raise SystemExit("reference_dir contains no supported images")
    rows = [_verify_pair(query, path, ratio=args.ratio, ransac_px=args.ransac_px,
                         max_features=args.max_features) for path in refs]
    rows.sort(key=lambda row: (-row["geometric_score"], -row["inliers"],
                               -row["good_matches"], row["reference_filename"]))
    if output.exists():
        raise SystemExit("output already exists; choose a new path")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({
        "schema_version": "local-feature-verification/v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "query_sha256": sha256(query),
        "method": {"detector": "SIFT-or-ORB", "ratio": args.ratio,
                   "ransac_px": args.ransac_px, "max_features": args.max_features},
        "reference_count": len(rows), "results": rows,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Verified {len(rows)} local reference image(s): {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

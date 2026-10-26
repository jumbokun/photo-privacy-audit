"""Offline OCR-to-entity matching against an approved local manifest."""
from __future__ import annotations

import csv
import re
import unicodedata
from pathlib import Path

from .common import AuditError, write_csv

FIELDS = ["entity_id", "city", "admin_region", "match_score", "matched_aliases",
          "source", "dataset", "license", "authorization_status", "evidence"]
REQUIRED_MANIFEST = {"entity_id", "aliases", "city", "admin_region"}
ACCEPTED = {"approved", "authorized", "benchmark", "research", "public", "licensed", "consented", "allowlisted"}


def normalize_text(value):
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", value)


def load_entity_manifest(path, require_authorization=False):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_MANIFEST - set(reader.fieldnames or [])
        if missing:
            raise AuditError("Entity manifest is missing: " + ", ".join(sorted(missing)))
        rows = list(reader)
    if not rows:
        raise AuditError("Entity manifest is empty.")
    seen = set()
    output = []
    for row in rows:
        entity_id = str(row.get("entity_id", "")).strip()
        if not entity_id or entity_id in seen:
            raise AuditError("Entity manifest entity_id must be unique and non-empty.")
        seen.add(entity_id)
        aliases = [normalize_text(item) for item in re.split(r"[|;]", str(row.get("aliases", ""))) if normalize_text(item)]
        if not aliases:
            raise AuditError("Entity manifest aliases must contain at least one value.")
        status = str(row.get("authorization_status", "")).strip().lower()
        if require_authorization and status not in ACCEPTED:
            raise AuditError("Entity manifest row is not authorized.")
        output.append({**row, "entity_id": entity_id, "aliases_normalized": aliases,
                       "authorization_status": status})
    return output


def match_ocr_rows(ocr_rows, entity_rows, min_alias_chars=2):
    if type(min_alias_chars) is not int or min_alias_chars < 1:
        raise AuditError("min_alias_chars must be a positive integer.")
    results = []
    for ocr in ocr_rows:
        text = str(ocr.get("text", "")).strip()
        normalized = normalize_text(text)
        if not normalized:
            continue
        confidence = float(ocr.get("confidence", 0) or 0)
        for entity in entity_rows:
            matched = [alias for alias in entity["aliases_normalized"]
                       if len(alias) >= min_alias_chars and alias in normalized]
            if not matched:
                continue
            score = min(1.0, confidence * max(len(alias) for alias in matched) / max(len(normalized), 1))
            results.append({"entity_id": entity["entity_id"], "city": entity.get("city", ""),
                            "admin_region": entity.get("admin_region", ""),
                            "match_score": round(score, 6),
                            "matched_aliases": matched, "source": entity.get("source", ""),
                            "dataset": entity.get("dataset", ""), "license": entity.get("license", ""),
                            "authorization_status": entity.get("authorization_status", ""),
                            "evidence": f"OCR text={text!r}; confidence={confidence:.4f}; aliases={matched}"})
    results.sort(key=lambda row: (-row["match_score"], row["entity_id"]))
    return results


def load_ocr_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise AuditError("OCR report is empty.")
    return rows

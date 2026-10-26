"""Public-safe report writers."""
from datetime import datetime, timezone
from pathlib import Path

from .common import write_csv, write_json, write_text
from .visual import FIELDS as VISUAL_FIELDS

METADATA_FIELDS = [
    "asset_id", "relative_path", "sha256", "bytes", "format", "width", "height", "frames",
    "status", "error_code", "metadata_coverage", "has_gps", "has_timestamp", "has_serial",
    "has_device", "has_opaque_metadata", "metadata_score", "dhash", "hash_informative",
    "dhash_rotations", "dhash_16", "phash", "phash_rotations",
]
CORRELATION_FIELDS = ["asset_id", "post_id", "exact_match_asset_ids", "near_match_asset_ids",
                      "near_match_hamming", "near_match_phash", "near_match_method",
                      "linked_post_count", "cross_post_count", "time_link", "time_delta_hours",
                      "cross_post_risk", "risk_reasons", "evidence"]


def metadata_report_rows(scanned):
    rows = []
    for item in scanned:
        row = {"asset_id": item["asset_id"], "relative_path": item["relative_path"], **item["scan"]}
        for key in ("gps", "timestamp", "serial", "device", "opaque_metadata"):
            row[f"has_{key}"] = row.pop(key)
        rows.append(row)
    return rows


def metadata_reasons(row):
    def truth(value):
        return value is True or str(value).strip().lower() in {"true", "1", "yes"}

    reasons = []
    if truth(row["has_gps"]):
        reasons.append("精确位置元数据存在")
    if truth(row["has_serial"]):
        reasons.append("设备标识字段存在")
    if truth(row["has_timestamp"]):
        reasons.append("拍摄时间字段存在")
    if truth(row["has_device"]):
        reasons.append("设备型号字段存在")
    if truth(row["has_opaque_metadata"]):
        reasons.append("存在扩展或未逐项审计的元数据")
    return "；".join(reasons) if reasons else "未从支持的字段中发现元数据暴露"


def write_metadata_report(output, rows):
    output = Path(output)
    prepared = [{**row, "metadata_reason": metadata_reasons(row)} for row in rows]
    write_csv(output / "metadata_report.csv", METADATA_FIELDS + ["metadata_reason"], prepared)


def write_visual_template(output, rows):
    write_csv(Path(output) / "visual_audit.csv", VISUAL_FIELDS,
              [{"asset_id": row["asset_id"], "review_status": "pending"} for row in rows])


def write_correlation_report(output, rows):
    write_csv(Path(output) / "correlation_report.csv", CORRELATION_FIELDS, rows)


def write_privacy_summary(output, metadata_rows, visual_rows=None, correlation_rows=None, provenance=None):
    visual_rows = [] if visual_rows is None else visual_rows
    correlation_rows = correlation_rows or []
    def truth(value):
        return value is True or str(value).strip().lower() in {"true", "1", "yes"}

    readable = [row for row in metadata_rows if row["status"] in {"ok", "partial"}]
    gps_count = sum(1 for row in metadata_rows if truth(row["has_gps"]))
    high_metadata = sum(1 for row in metadata_rows if str(row.get("metadata_score", "")).strip() in {"3", "4"})
    pending = len(metadata_rows) if visual_rows == [] else sum(
        1 for row in visual_rows if row.get("review_status", "pending").lower() != "reviewed"
    )
    high_cross = sum(1 for row in correlation_rows if int(row.get("cross_post_risk", 0) or 0) >= 3)
    high_visual = sum(
        1 for row in visual_rows
        if row.get("review_status", "").lower() == "reviewed"
        and max(int(row.get(key, 0) or 0) for key in ("visual_risk", "temporal_risk")) >= 3
    )
    local_generated = (provenance or {}).get("created_at_local")
    utc_generated = (provenance or {}).get("created_at_utc", datetime.now(timezone.utc).isoformat())
    timezone_name = (provenance or {}).get("timezone", "Asia/Shanghai")
    lines = ["# Privacy audit summary", "", f"Generated ({timezone_name}): {local_generated or utc_generated}",
             f"Generated (UTC): {utc_generated}", "",
             "This report contains aggregate findings only. It does not include GPS coordinates, private addresses, or location candidates.", "",
             "## Coverage", "", f"- Files scanned: {len(metadata_rows)}", f"- Files readable: {len(readable)}", f"- Files requiring review: {len(metadata_rows) - len(readable)}", "",
             "## Metadata", "", f"- Files with exact location metadata: {gps_count}", f"- Files with metadata score 3 or 4: {high_metadata}", "- Coordinate values are intentionally omitted from ordinary reports.", "",
             "## Manual and cross-post review", "", f"- Visual checklist rows still pending: {pending}", f"- Reviewed visual/time rows at risk score 3 or 4: {high_visual}", f"- Cross-post rows at risk score 3 or 4: {high_cross}", "",
             "## Priority actions", "", "1. Keep originals in a restricted, read-only location and audit the social-platform copy separately.",
             "2. Review every pending visual row; a blank visual score is not a zero-risk result.",
             "3. Remove unnecessary metadata and visually redact sensitive content, then re-run the audit.",
             "4. Treat perceptual matches as review leads; they do not prove the same scene.", "",
             "## Scope", "", "No automatic geocoding, reverse geocoding, street-view search, or address candidate generation is performed."]
    if provenance:
        write_json(Path(output) / "provenance.json", provenance)
    write_text(Path(output) / "privacy_summary.md", "\n".join(lines) + "\n")


def write_benchmark_report(path, summary, dataset, model, commit, prediction_count):
    def pct(value):
        return "n/a" if value is None else f"{value * 100:.2f}%"
    def km(value):
        return "n/a" if value is None else f"{value:.3f} km"
    def threshold_key(threshold):
        return str(threshold).replace(".", "_")
    def threshold_label(threshold):
        if threshold < 1:
            return f"{threshold * 1000:g} m"
        return f"{threshold:g} km"
    lines = ["# Benchmark report", "", "This aggregate report is for an authorized benchmark dataset only.",
             "It must not be interpreted as a location probability for any individual private photo.", "",
             "## Run manifest", "", f"- Dataset: {dataset}", f"- Model: {model}", f"- Code/checkpoint reference: {commit or 'not supplied'}",
             f"- Prediction rows: {prediction_count}", "", "## Aggregate metrics", "",
             f"- Median error: {km(summary['median_km'])}", f"- Mean error: {km(summary['mean_km'])}",
             f"- P90 error: {km(summary.get('p90_km'))}", f"- P95 error: {km(summary.get('p95_km'))}",
             f"- Failure rate: {pct(summary['failure_rate'])}"]
    thresholds = summary.get("thresholds_km", (0.2, 1.0, 25.0))
    for threshold in thresholds:
        key = f"hit_rate_{threshold_key(threshold)}km"
        label = threshold_label(threshold)
        interval = summary[key + "_ci95"]
        ci = "n/a" if interval is None else f"[{pct(interval[0])}, {pct(interval[1])}]"
        lines.append(f"- Hit rate ≤ {label}: {pct(summary[key])} (bootstrap 95% CI {ci})")
    groups = summary.get("group_metrics", {})
    if groups:
        lines += ["", "## Group metrics", "", f"Grouping field: `{summary.get('group_key')}`"]
        for group, metrics in groups.items():
            key = f"hit_rate_{threshold_key(thresholds[0])}km"
            lines.append(f"- `{group}`: n={metrics['count']}, failure={pct(metrics['failure_rate'])}, "
                         f"hit ≤ {threshold_label(thresholds[0])}={pct(metrics[key])}")
    topk = summary.get("topk_metrics", {})
    if topk and any(int(key) > 1 for key in topk):
        lines += ["", "## Recall@K", ""]
        for key, metrics in topk.items():
            lines.append(f"- K={key}: failure={pct(metrics['failure_rate'])}, "
                         f"hit ≤ 200 m={pct(metrics['hit_rate_0_2km'])}, "
                         f"hit ≤ 1 km={pct(metrics['hit_rate_1_0km'])}, "
                         f"hit ≤ 25 km={pct(metrics['hit_rate_25_0km'])}")
    leakage = summary.get("leakage")
    if leakage:
        lines += ["", "## Split leakage audit", ""]
        if not leakage.get("available"):
            lines.append("- Not run: prediction file did not include a `split` column.")
        elif leakage.get("ok") is None:
            missing = leakage.get("rows_without_identity", "some")
            lines.append(f"- Identity coverage incomplete: {missing} row(s) have no capture/image identity.")
        elif leakage.get("ok"):
            lines.append("- No repeated capture/image identity was found across splits.")
        else:
            lines.append(f"- Potential leakage identities: {len(leakage.get('violations', []))}; review before comparing runs.")
    calibration = summary.get("calibration")
    if calibration:
        lines += ["", "## Confidence calibration", ""]
        if calibration.get("available"):
            lines.append(f"- Hit event: error ≤ {calibration['threshold_km']:g} km")
            lines.append(f"- Brier score: {calibration['brier']:.6f}")
            lines.append(f"- Expected calibration error: {calibration['ece']:.6f}")
        else:
            lines.append(f"- Not available: {calibration.get('reason', 'unknown')}")
    lines += ["", "## Interpretation", "", "The benchmark result depends on dataset coverage, licensing, split design, checkpoint, preprocessing, and evaluation code.",
              "Keep train and test locations disjoint where the dataset supports it. Record the exact dependency lock and checkpoint digest before comparing runs."]
    write_text(path, "\n".join(lines) + "\n")

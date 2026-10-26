"""Command line entry points for the local workflow."""
import argparse
import csv
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from .benchmark import build_run_manifest, evaluate, load_predictions
from .common import AuditError, load_policy, new_directory, provenance, sha256, write_csv, write_json
from .correlation import correlate, read_manifest
from .doctor import main as doctor_main
from .embeddings import correlate_embeddings, load_embeddings, write_embedding_report
from .metadata import EXTENSIONS, inspect_image, resolve_exiftool
from .ocr import RapidOCREngine, ocr_image, ocr_provenance, write_ocr_report
from .entity_match import load_entity_manifest, load_ocr_csv, match_ocr_rows
from .geo_city import load_candidates as load_geoclip_candidates, load_city_centers, match_cities, write_city_report
from .admin_boundary import map_candidates, write_boundary_report
from .geoclip_adapter import predict as geoclip_predict, write_prediction as write_geoclip_prediction
from .multi_image_fusion import fuse_multiview, load_candidates, write_fusion_report
from .reports import (METADATA_FIELDS, metadata_report_rows,
                       CORRELATION_FIELDS,
                       write_benchmark_report, write_correlation_report, write_metadata_report,
                       write_privacy_summary, write_visual_template)
from .sanitize import strip_metadata
from .visual import validate_checklist
from .vision_evidence import load_jsonl, write_evidence_report
from .retrieval import retrieve_candidates, write_retrieval_report
from .vision_encoder import (build_encoder, generate_embedding_rows,
                              load_asset_manifest_mapping, write_embedding_bundle)
from .streetview import load_queries, provider_for, write_fetch_result


def _image_files(root):
    root = Path(root).resolve()
    files = []
    try:
        for path in root.rglob("*"):
            # A symlink can point outside the declared root (or change its
            # target between discovery and hashing). Refuse it before any
            # decoder or metadata tool sees the path.
            if path.is_symlink():
                raise AuditError("Input directory must not contain symbolic links.")
            if not path.is_file() or path.suffix.lower() not in EXTENSIONS:
                continue
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise AuditError("Input file resolved outside the declared photo directory.")
            files.append(path)
    except OSError:
        raise AuditError("Could not safely inspect the input photo directory.") from None
    return sorted(files)


def _assign_asset_ids(scanned):
    """Assign deterministic IDs without collapsing same-content files."""
    groups = {}
    for item in scanned:
        base = item["scan"]["sha256"][:16]
        groups.setdefault(base, []).append(item)
    used = set()
    for base, items in groups.items():
        for item in items:
            if len(items) == 1:
                candidate = base
            else:
                material = f'{item["scan"]["sha256"]}\0{item["relative_path"]}'.encode("utf-8")
                suffix = hashlib.sha256(material).hexdigest()
                candidate = f"{base}-{suffix[:12]}"
                # Keep the collision path deterministic if an adversarial
                # fixture deliberately creates the same short suffix.
                offset = 12
                while candidate in used:
                    offset += 4
                    candidate = f"{base}-{suffix[:offset]}"
            if candidate in used:
                raise AuditError("Could not assign unique asset identifiers.")
            item["asset_id"] = candidate
            used.add(candidate)


def _audit(args):
    source = Path(args.input).resolve()
    if not source.is_dir():
        raise AuditError("Input must be a directory of photos.")
    policy = load_policy(args.policy)
    files = _image_files(source)
    if not files:
        raise AuditError("No supported image files were found.")
    if len(files) > policy["max_images"]:
        raise AuditError("Image count exceeds the policy limit.")
    output = new_directory(args.output, outside=source)
    executable = resolve_exiftool(args.exiftool)
    scanned = []
    for path in files:
        relative = path.relative_to(source).as_posix()
        scan = inspect_image(path, policy, executable)
        scanned.append({"relative_path": relative, "scan": scan})
    _assign_asset_ids(scanned)
    rows = metadata_report_rows(scanned)
    write_metadata_report(output, rows)
    write_visual_template(output, rows)
    write_privacy_summary(output, rows, provenance=provenance(policy))
    source_digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()
    write_json(output / "audit_manifest.json", {
        "input_root": "<redacted-local-path>",
        "input_root_sha256": source_digest,
        "image_count": len(rows),
        "policy": policy,
        "asset_ids": [row["asset_id"] for row in rows],
    })
    print(f"Audited {len(rows)} image(s). Reports: {output}")
    print("GPS coordinate values were not written to reports.")
    return 0


def _read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _validated_asset_ids(rows, label):
    ids = []
    for row in rows:
        asset_id = str(row.get("asset_id", "")).strip()
        if not asset_id:
            raise AuditError(f"{label} contains a blank asset_id.")
        ids.append(asset_id)
    if len(ids) != len(set(ids)):
        raise AuditError(f"{label} asset_id values must be unique.")
    return set(ids)


def _correlate(args):
    output = new_directory(args.output)
    metadata_rows = _read_csv(args.metadata)
    required = set(METADATA_FIELDS) - set(metadata_rows[0] if metadata_rows else [])
    if required:
        raise AuditError("metadata_report.csv is missing required columns.")
    image_root = Path(args.image_root).resolve()
    asset_ids = _validated_asset_ids(metadata_rows, "metadata_report.csv")
    expected_paths = {row["asset_id"].strip(): row.get("relative_path", "") for row in metadata_rows}
    manifest = read_manifest(args.manifest, image_root, asset_ids, expected_paths=expected_paths)
    policy = load_policy(args.policy)
    rows = correlate(metadata_rows, manifest, policy["near_duplicate_hamming"], policy["post_time_window_hours"])
    write_correlation_report(output, rows)
    write_privacy_summary(output, metadata_rows, correlation_rows=rows, provenance=provenance(policy))
    print(f"Correlated {len(rows)} asset(s). Reports: {output}")
    return 0


def _benchmark(args):
    rows = load_predictions(args.predictions)
    summary = evaluate(rows, seed=args.seed, bootstrap_iterations=args.bootstrap_iterations,
                       group_key=args.group_key, calibration_threshold_km=args.calibration_threshold)
    output = Path(args.output).resolve()
    if output.exists():
        raise AuditError("Benchmark report already exists; choose a new path.")
    manifest = output.with_name(output.name + ".manifest.json")
    if manifest.exists():
        raise AuditError("Benchmark run manifest already exists; choose a new report path.")
    output.parent.mkdir(parents=True, exist_ok=True)
    write_benchmark_report(output, summary, args.dataset, args.model, args.commit, len(rows))
    write_json(manifest, build_run_manifest(args.predictions, rows, args.dataset, args.model,
                                             args.commit, summary))
    print(f"Benchmark report written: {output}")
    print(f"Benchmark run manifest written: {manifest}")
    return 0


def _sanitize(args):
    source = Path(args.input).resolve()
    if not source.is_dir():
        raise AuditError("Input must be a directory of photos.")
    output = new_directory(args.output, outside=source)
    files = _image_files(source)
    if args.asset:
        wanted = {Path(value).as_posix() for value in args.asset}
        files = [path for path in files if path.relative_to(source).as_posix() in wanted]
    if not files:
        raise AuditError("No selected image files were found.")
    rows = []
    for path in files:
        rows.append(strip_metadata(source, output, path.relative_to(source).as_posix()))
    write_csv(output / "sanitization_log.csv", list(rows[0]), rows)
    print(f"Created {len(rows)} sanitized copy/copies: {output}")
    return 0


def _validate(args):
    rows = validate_checklist(args.checklist)
    print(f"Validated {len(rows)} checklist row(s).")
    return 0


def _summary(args):
    metadata_rows = _read_csv(args.metadata)
    if not metadata_rows:
        raise AuditError("metadata_report.csv is empty.")
    required = set(METADATA_FIELDS) - set(metadata_rows[0])
    if required:
        raise AuditError("metadata_report.csv is missing required columns.")
    asset_ids = _validated_asset_ids(metadata_rows, "metadata_report.csv")
    visual_rows = validate_checklist(args.visual, expected_asset_ids=asset_ids)
    correlation_rows = _read_csv(args.correlation) if args.correlation else []
    if correlation_rows:
        missing = set(CORRELATION_FIELDS) - set(correlation_rows[0])
        if missing:
            raise AuditError("correlation_report.csv is missing required columns.")
        correlation_ids = _validated_asset_ids(correlation_rows, "correlation_report.csv")
        if correlation_ids != asset_ids:
            raise AuditError("correlation_report.csv asset_id values must match metadata_report exactly.")
        for row in correlation_rows:
            try:
                risk = int(str(row.get("cross_post_risk", "")).strip())
            except (TypeError, ValueError):
                raise AuditError("correlation cross_post_risk must be an integer from 0 to 4.") from None
            if not 0 <= risk <= 4:
                raise AuditError("correlation cross_post_risk must be an integer from 0 to 4.")
    output = new_directory(args.output)
    policy = load_policy(args.policy)
    write_privacy_summary(output, metadata_rows, visual_rows, correlation_rows, provenance(policy))
    print(f"Summary written: {output / 'privacy_summary.md'}")
    return 0


def _embedding_correlate(args):
    rows = load_embeddings(args.embeddings)
    output = Path(args.output).resolve()
    if output.exists():
        raise AuditError("Embedding report already exists; choose a new path.")
    output.parent.mkdir(parents=True, exist_ok=True)
    matches = correlate_embeddings(rows, threshold=args.threshold, top_k=args.top_k, block_size=args.block_size)
    write_embedding_report(output, matches)
    print(f"Embedding candidates written: {output} ({len(matches)} pair(s))")
    return 0


def _vision_evidence(args):
    rows = load_jsonl(args.input)
    output = Path(args.output).resolve()
    if output.exists():
        raise AuditError("Vision evidence report already exists; choose a new path.")
    output.parent.mkdir(parents=True, exist_ok=True)
    write_evidence_report(output, rows)
    print(f"Validated {len(rows)} local vision observation(s): {output}")
    return 0


def _fuse_candidates(args):
    """Fuse approved per-image geolocation candidates into ranges."""
    rows = load_candidates(args.input)
    # ``reference-retrieve`` emits one explicit no_candidates row for a query
    # with no eligible reference.  It is useful in the retrieval audit but is
    # not a location candidate and must not enter spatial fusion.
    rows = [row for row in rows if str(row.get("match_status", "candidate")).strip().lower() == "candidate"]
    if not rows:
        raise AuditError("candidate input contains no rows with match_status=candidate")
    output = Path(args.output).resolve()
    if output.exists():
        raise AuditError("Fusion report already exists; choose a new path.")
    output.parent.mkdir(parents=True, exist_ok=True)
    report = fuse_multiview(rows, cluster_radius_km=args.cluster_radius,
                            conflict_distance_km=args.conflict_distance,
                            top_k=args.top_k)
    write_fusion_report(output, report)
    print(f"Multi-image fusion report written: {output}")
    print(f"Status: {report['status']}; candidate clusters: {len(report['clusters'])}")
    return 0


def _reference_retrieve(args):
    """Run an offline retrieval pass against an approved local reference set."""
    output = Path(args.output).resolve()
    if output.exists():
        raise AuditError("Retrieval report already exists; choose a new path.")
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = retrieve_candidates(
        args.query_embeddings,
        args.reference_manifest,
        args.reference_embeddings,
        top_k=args.top_k,
        grid_size_deg=args.grid_size_deg,
        center_lat=args.center_lat,
        center_lon=args.center_lon,
        radius_km=args.radius_km,
        grid_ids=getattr(args, "grid_id", None),
        require_authorization=getattr(args, "require_authorization", False),
        rerank=args.rerank,
        max_per_capture=getattr(args, "max_per_capture", None),
    )
    sidecar = output.with_name(output.name + ".provenance.json")
    if sidecar.exists():
        raise AuditError("Retrieval provenance already exists; choose a new output path.")
    write_retrieval_report(output, rows)
    write_json(sidecar, {
        "schema_version": "reference-retrieval/v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "query_embeddings_sha256": sha256(args.query_embeddings),
        "reference_manifest_sha256": sha256(args.reference_manifest),
        "reference_embeddings_sha256": sha256(args.reference_embeddings),
        "model_id": rows[0].get("model_id") if rows else None,
        "top_k": args.top_k,
        "grid_size_deg": args.grid_size_deg,
        "max_per_capture": getattr(args, "max_per_capture", None),
        "center_supplied": args.center_lat is not None,
        "radius_km": args.radius_km,
        "grid_ids": getattr(args, "grid_id", None) or [],
        "require_authorization": getattr(args, "require_authorization", False),
        "candidate_rows": sum(1 for row in rows if row.get("match_status") == "candidate"),
        "query_count": len({row.get("query_asset_id") for row in rows}),
        "coordinates_included": True,
    })
    candidates = sum(1 for row in rows if row.get("match_status") == "candidate")
    print(f"Reference retrieval wrote {len(rows)} row(s), {candidates} candidate(s): {output}")
    return 0


def _embedding_generate(args):
    source = Path(args.input).resolve()
    if not source.is_dir():
        raise AuditError("Embedding input must be a directory of photos.")
    files = _image_files(source)
    if not files:
        raise AuditError("No supported image files were found.")
    encoder = build_encoder(args.backend, args.model)
    output = Path(args.output).resolve()
    if output.exists():
        raise AuditError("Embedding output already exists; choose a new path.")
    output.parent.mkdir(parents=True, exist_ok=True)
    asset_ids = None
    if getattr(args, "asset_manifest", None):
        asset_ids = load_asset_manifest_mapping(args.asset_manifest, source, files)
    rows = generate_embedding_rows(files, encoder, asset_ids)
    write_embedding_bundle(output, rows, encoder, args.provenance)
    print(f"Generated {len(rows)} local embedding(s): {output}")
    print(f"Encoder: {encoder.provenance.model_id}; dimensions={len(rows[0]['vector'])}")
    if asset_ids is not None:
        print(f"Asset IDs mapped from approved manifest: {len(asset_ids)}")
    return 0


def _ocr(args):
    source = Path(args.input).resolve()
    if not source.is_dir():
        raise AuditError("OCR input must be a directory of photos.")
    files = _image_files(source)
    if not files:
        raise AuditError("No supported image files were found.")
    output = Path(args.output).resolve()
    if output.exists():
        raise AuditError("OCR output already exists; choose a new path.")
    output.parent.mkdir(parents=True, exist_ok=True)
    engine = RapidOCREngine()
    rows = []
    for path in files:
        rows.extend(ocr_image(path, asset_id=path.relative_to(source).as_posix(), engine=engine))
    write_ocr_report(output, rows, ocr_provenance(engine))
    print(f"OCR wrote {len(rows)} local text observation(s): {output}")
    return 0


def _entity_match(args):
    entities = load_entity_manifest(args.manifest, require_authorization=args.require_authorization)
    ocr_rows = load_ocr_csv(args.ocr_report)
    rows = match_ocr_rows(ocr_rows, entities, min_alias_chars=args.min_alias_chars)
    output = Path(args.output).resolve()
    if output.exists():
        raise AuditError("Entity match output already exists; choose a new path.")
    output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(output, ["entity_id", "city", "admin_region", "match_score", "matched_aliases",
                       "source", "dataset", "license", "authorization_status", "evidence"], rows)
    print(f"Entity matching wrote {len(rows)} candidate(s): {output}")
    return 0


def _geoclip(args):
    rows = geoclip_predict(args.input, top_k=args.top_k, offline=args.offline)
    write_geoclip_prediction(args.output, args.input, rows)
    print(f"GeoCLIP wrote {len(rows)} candidate coordinate(s): {args.output}")
    return 0


def _geo_city_match(args):
    candidates = load_geoclip_candidates(args.candidates)
    cities = load_city_centers(args.city_centers)
    rows = match_cities(candidates, cities, radius_km=args.radius_km)
    output = Path(args.output).resolve()
    if output.exists():
        raise AuditError("City candidate output already exists; choose a new path.")
    output.parent.mkdir(parents=True, exist_ok=True)
    write_city_report(output, rows)
    print(f"GeoCLIP city aggregation wrote {len(rows)} candidate city row(s): {output}")
    return 0


def _admin_match(args):
    rows = map_candidates(args.candidates, args.boundaries, radius_km=args.radius_km)
    output = Path(args.output).resolve()
    if output.exists():
        raise AuditError("Administrative match output already exists; choose a new path.")
    output.parent.mkdir(parents=True, exist_ok=True)
    write_boundary_report(output, rows)
    print(f"Administrative boundary matching wrote {len(rows)} row(s): {output}")
    return 0


def _streetview_fetch(args):
    """Fetch an explicitly bounded set of street-level imagery probes."""
    if args.delay < 0:
        raise AuditError("Streetview delay must be non-negative.")
    if args.max_image_mb < 1:
        raise AuditError("Streetview max-image-mb must be at least 1.")
    queries = load_queries(args.queries)
    output = Path(args.output).resolve()
    if output.exists():
        raise AuditError("Streetview manifest already exists; choose a new path.")
    download_dir = None
    if args.download_dir:
        download_dir = Path(args.download_dir).resolve()
        if download_dir.exists():
            raise AuditError("Streetview download directory already exists; choose a new directory.")
        download_dir.mkdir(parents=True)
    provider = provider_for(args.provider)
    result = provider.fetch(queries, limit=args.limit, delay_s=args.delay,
                           download_dir=download_dir, max_image_bytes=args.max_image_mb * 1024 * 1024)
    write_fetch_result(result, output, args.queries, download_dir=download_dir)
    print(f"Streetview provider={result.provider}: {len(result.rows)} row(s), "
          f"{len(result.failures)} failure(s). Manifest: {output}")
    if download_dir:
        print(f"Downloaded images: {download_dir}")
    return 0


def _doctor(args):
    return doctor_main(args.policy)


def _workflow(args):
    source = Path(args.input).resolve()
    root = new_directory(args.output, outside=source)
    audit_dir = root / "audit"
    _audit(argparse.Namespace(input=str(source), output=str(audit_dir), policy=args.policy,
                              exiftool=args.exiftool))
    correlation_dir = None
    if args.manifest:
        correlation_dir = root / "correlation"
        _correlate(argparse.Namespace(metadata=str(audit_dir / "metadata_report.csv"),
                                      manifest=args.manifest, image_root=str(source),
                                      output=str(correlation_dir), policy=args.policy))
    summary_dir = root / "summary"
    _summary(argparse.Namespace(metadata=str(audit_dir / "metadata_report.csv"),
                                visual=str(audit_dir / "visual_audit.csv"),
                                correlation=str(correlation_dir / "correlation_report.csv") if correlation_dir else None,
                                output=str(summary_dir), policy=args.policy))
    print(f"Workflow complete: {root}")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="photo-audit", description="Offline photo privacy audit")
    sub = parser.add_subparsers(dest="command", required=True)
    audit = sub.add_parser("audit", help="scan photos and create local reports")
    audit.add_argument("input")
    audit.add_argument("output")
    audit.add_argument("--policy", default=None)
    audit.add_argument("--exiftool", default="auto", help="auto, off, or an executable name")
    audit.set_defaults(func=_audit)
    corr = sub.add_parser("correlate", help="compare image hashes against a post manifest")
    corr.add_argument("metadata")
    corr.add_argument("manifest")
    corr.add_argument("image_root")
    corr.add_argument("output")
    corr.add_argument("--policy", default=None)
    corr.set_defaults(func=_correlate)
    bench = sub.add_parser("benchmark", help="evaluate authorized benchmark predictions")
    bench.add_argument("predictions")
    bench.add_argument("output")
    bench.add_argument("--dataset", required=True)
    bench.add_argument("--model", required=True)
    bench.add_argument("--commit", default="")
    bench.add_argument("--seed", type=int, default=42)
    bench.add_argument("--bootstrap-iterations", type=int, default=2000)
    bench.add_argument("--group-key", default=None,
                       help="optional grouping column (for example region or location)")
    bench.add_argument("--calibration-threshold", type=float, default=25.0,
                       help="distance threshold in km for confidence calibration")
    bench.set_defaults(func=_benchmark)
    sanitize = sub.add_parser("sanitize", help="write metadata-stripped copies")
    sanitize.add_argument("input")
    sanitize.add_argument("output")
    sanitize.add_argument("--asset", action="append", help="relative image path; repeatable")
    sanitize.set_defaults(func=_sanitize)
    check = sub.add_parser("validate-checklist", help="validate a manually completed visual checklist")
    check.add_argument("checklist")
    check.set_defaults(func=_validate)
    summary = sub.add_parser("summary", help="refresh the aggregate privacy summary after manual review")
    summary.add_argument("metadata")
    summary.add_argument("visual")
    summary.add_argument("output")
    summary.add_argument("--correlation", default=None)
    summary.add_argument("--policy", default=None)
    summary.set_defaults(func=_summary)
    embeddings = sub.add_parser("embedding-correlate", help="correlate locally computed image embeddings")
    embeddings.add_argument("embeddings", help="CSV with asset_id, model_id and JSON vector")
    embeddings.add_argument("output")
    embeddings.add_argument("--threshold", type=float, default=0.90)
    embeddings.add_argument("--top-k", type=int, default=5)
    embeddings.add_argument("--block-size", type=int, default=512)
    embeddings.set_defaults(func=_embedding_correlate)
    evidence = sub.add_parser("vision-evidence", help="validate structured local vision observations")
    evidence.add_argument("input", help="JSONL emitted by a local vision adapter")
    evidence.add_argument("output")
    evidence.set_defaults(func=_vision_evidence)
    fusion = sub.add_parser("fuse-candidates", help="fuse approved multi-image geolocation candidates")
    fusion.add_argument("input", help="CSV, JSON, or JSONL candidate rows")
    fusion.add_argument("output", help="new JSON report path")
    fusion.add_argument("--cluster-radius", type=float, default=2.0,
                        help="spatial grouping radius in kilometres")
    fusion.add_argument("--conflict-distance", type=float, default=None,
                        help="distance in kilometres at which comparable clusters conflict")
    fusion.add_argument("--top-k", type=int, default=5,
                        help="maximum candidate ranges to write")
    fusion.set_defaults(func=_fuse_candidates)
    retrieve = sub.add_parser(
        "reference-retrieve",
        help="retrieve top-k candidates from an authorized local geotagged reference set",
    )
    retrieve.add_argument("query_embeddings", help="CSV with query asset_id, model_id and JSON vector")
    retrieve.add_argument("reference_manifest", help="CSV with reference asset_id, lat and lon")
    retrieve.add_argument("reference_embeddings", help="CSV with reference asset_id, model_id and JSON vector")
    retrieve.add_argument("output", help="new candidate report CSV")
    retrieve.add_argument("--top-k", type=int, default=10)
    retrieve.add_argument("--grid-size-deg", type=float, default=0.01)
    retrieve.add_argument("--center-lat", type=float, default=None)
    retrieve.add_argument("--center-lon", type=float, default=None)
    retrieve.add_argument("--radius-km", type=float, default=None)
    retrieve.add_argument("--grid-id", action="append", default=None,
                          help="optional reference grid cell; repeatable")
    retrieve.add_argument("--require-authorization", action="store_true",
                          help="reject manifests without an approved authorization_status")
    retrieve.add_argument("--rerank", choices=("embedding", "grid"), default="grid",
                          help="rank by raw embedding or grid consensus")
    retrieve.add_argument("--max-per-capture", type=int, default=None,
                          help="optional cap per provider/capture_id before grid voting")
    retrieve.set_defaults(func=_reference_retrieve)
    generate = sub.add_parser("embedding-generate", help="generate embeddings with a local offline encoder")
    generate.add_argument("input", help="directory of images")
    generate.add_argument("output", help="embedding CSV output")
    generate.add_argument("--backend", choices=("pillow", "torchscript", "transformers", "open_clip"), default="pillow")
    generate.add_argument("--model", default=None, help="local checkpoint, Transformers directory, or open_clip MODEL_NAME::CHECKPOINT")
    generate.add_argument("--provenance", default=None, help="optional provenance JSON sidecar path")
    generate.add_argument("--asset-manifest", default=None,
                          help="approved CSV mapping asset_id to local_path or relative_path")
    generate.set_defaults(func=_embedding_generate)
    ocr = sub.add_parser("ocr", help="run local Chinese-capable OCR without network")
    ocr.add_argument("input", help="directory of images")
    ocr.add_argument("output", help="OCR CSV output")
    ocr.set_defaults(func=_ocr)
    entity = sub.add_parser("entity-match", help="match local OCR evidence to an approved entity manifest")
    entity.add_argument("ocr_report")
    entity.add_argument("manifest")
    entity.add_argument("output")
    entity.add_argument("--require-authorization", action="store_true")
    entity.add_argument("--min-alias-chars", type=int, default=2)
    entity.set_defaults(func=_entity_match)
    city = sub.add_parser("city-match", help="match OCR clues against a local China city alias table")
    city.add_argument("ocr_report")
    city.add_argument("manifest")
    city.add_argument("output")
    city.add_argument("--require-authorization", action="store_true")
    city.add_argument("--min-alias-chars", type=int, default=2)
    city.set_defaults(func=_entity_match)
    geoclip = sub.add_parser("geoclip-predict", help="run local GeoCLIP and write coordinate candidates")
    geoclip.add_argument("input")
    geoclip.add_argument("output")
    geoclip.add_argument("--top-k", type=int, default=20)
    geoclip.add_argument("--offline", action="store_true", default=True)
    geoclip.set_defaults(func=_geoclip)
    city_geo = sub.add_parser("geo-city-match", help="aggregate GeoCLIP candidates by local city centers")
    city_geo.add_argument("candidates")
    city_geo.add_argument("city_centers")
    city_geo.add_argument("output")
    city_geo.add_argument("--radius-km", type=float, default=100.0)
    city_geo.set_defaults(func=_geo_city_match)
    admin = sub.add_parser("admin-match", help="map coordinate candidates into local GeoJSON boundaries")
    admin.add_argument("candidates")
    admin.add_argument("boundaries")
    admin.add_argument("output")
    admin.add_argument("--radius-km", type=float, default=0.0)
    admin.set_defaults(func=_admin_match)
    streetview = sub.add_parser(
        "streetview-fetch",
        help="fetch bounded street-level imagery from an authorized provider API",
    )
    streetview.add_argument("provider", choices=("kartaview", "mapillary", "baidu", "panoramax"),
                            help="KartaView/Panoramax are public/open; Mapillary/Baidu require env credentials")
    streetview.add_argument("queries", help="CSV with query_id,lat,lon[,radius_m,heading,min_lon,min_lat,max_lon,max_lat]")
    streetview.add_argument("output", help="new streetview manifest CSV")
    streetview.add_argument("--download-dir", default=None,
                            help="optional new directory for image downloads")
    streetview.add_argument("--limit", type=int, default=100,
                            help="maximum results per query (Baidu must use 1)")
    streetview.add_argument("--delay", type=float, default=1.0,
                            help="seconds between provider queries (default: 1)")
    streetview.add_argument("--max-image-mb", type=int, default=20,
                            help="maximum bytes per downloaded image")
    streetview.set_defaults(func=_streetview_fetch)
    doctor = sub.add_parser("doctor", help="check China/offline runtime compatibility")
    doctor.add_argument("--policy", default=None)
    doctor.set_defaults(func=_doctor)
    workflow = sub.add_parser("workflow", help="run audit, optional cross-post correlation, and summary")
    workflow.add_argument("input")
    workflow.add_argument("output")
    workflow.add_argument("--manifest", default=None)
    workflow.add_argument("--policy", default=None)
    workflow.add_argument("--exiftool", default="auto")
    workflow.set_defaults(func=_workflow)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (AuditError, ValueError, OSError) as exc:
        print(f"error: {exc}")
        return 2

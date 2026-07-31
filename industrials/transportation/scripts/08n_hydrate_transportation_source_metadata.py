#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import (  # noqa: E402
    cfg_get,
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)
from industrials.transportation.source_exhaustion_hydration import (  # noqa: E402
    build_hydration_requests,
    hydrate_metadata,
    read_csv,
    validate_sealed_csv_artifact,
    write_hydration_results,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Resume-safe hydration of only the SEC submissions shards or "
            "archive index JSON files selected by DP6E, followed—only after "
            "the refreshed manifest gate passes—by its exact selected-document "
            "delta. No database rows, parser work, features, or calibration "
            "are allowed."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--phase",
        choices=("submissions", "indexes", "documents"),
        required=True,
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--priority-max", type=int, default=3)
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help=(
            "Concurrent metadata requests behind one process-wide SEC "
            "request-spacing throttle."
        ),
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=0,
        help="Zero processes the complete selected phase.",
    )
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    parser_cfg = family["dedicated_parser"]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "Metadata hydration requires parser_execution_authorized=false"
        )
    base_dir = config_path.parent
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else (
            resolve_path(parser_cfg["output_root"], base_dir=base_dir)
            / str(parser_cfg["source_census_asof_date"])
        )
    )
    source_manifest_path = (
        output_dir / "transportation_source_exhaustion_manifest.json"
    )
    gap_path = output_dir / "transportation_source_exhaustion_gaps.csv"
    delta_path = (
        output_dir / "transportation_source_exhaustion_delta_candidates.csv"
    )
    for path in (source_manifest_path, gap_path, delta_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    source_manifest = json.loads(
        source_manifest_path.read_text(encoding="utf-8")
    )
    if source_manifest.get("acceptance") not in {
        "PASS_WITH_REQUIRED_DELTA",
        "PASS_SOURCE_EXHAUSTED",
    }:
        raise ValueError("DP6E source-exhaustion manifest is not valid")
    if (
        args.phase == "documents"
        and (
            source_manifest.get("metadata_exhaustion_complete") is not True
            or source_manifest.get("delta_document_manifest_ready") is not True
        )
    ):
        raise ValueError(
            "Document hydration requires a refreshed DP6E manifest with "
            "metadata_exhaustion_complete=true and "
            "delta_document_manifest_ready=true"
        )
    cache_dir = resolve_path(
        cfg_get(config, "sec_fundamentals.cache_dir"),
        base_dir=base_dir,
    )
    gap_rows = read_csv(gap_path)
    delta_rows = read_csv(delta_path)
    seal_errors = [
        *validate_sealed_csv_artifact(
            source_manifest=source_manifest,
            artifact_name="source_gaps",
            path=gap_path,
            rows=gap_rows,
        ),
        *validate_sealed_csv_artifact(
            source_manifest=source_manifest,
            artifact_name="delta_candidates",
            path=delta_path,
            rows=delta_rows,
        ),
    ]
    if seal_errors:
        raise ValueError(
            "DP6E input artifact seal failed: " + "; ".join(seal_errors)
        )
    requests = build_hydration_requests(
        gap_rows=gap_rows,
        delta_rows=delta_rows,
        submissions_cache_dir=cache_dir / "sec_submissions",
        archive_cache_dir=cache_dir / "sec_archive_xbrl",
        phase=args.phase,
        priority_max=args.priority_max,
    )
    phase_name = f"transportation_source_metadata_{args.phase}"
    progress_path = output_dir / f"{phase_name}_progress.json"
    result_path = output_dir / f"{phase_name}_results.csv"
    manifest_path = output_dir / f"{phase_name}_manifest.json"
    user_agent = str(
        cfg_get(config, "sec_fundamentals.user_agent")
    )
    if "${" in user_agent:
        environment_name = user_agent.split("${", 1)[1].split(":", 1)[0]
        fallback = (
            user_agent.split(":-", 1)[1].rsplit("}", 1)[0]
            if ":-" in user_agent
            else ""
        )
        user_agent = os.environ.get(environment_name, fallback)
    result_rows, summary = hydrate_metadata(
        requests,
        execute=args.execute,
        user_agent=user_agent,
        timeout_sec=float(
            cfg_get(config, "sec_fundamentals.timeout_sec", 30.0)
        ),
        max_retries=int(
            cfg_get(config, "sec_fundamentals.max_retries", 3)
        ),
        request_spacing_sec=float(
            cfg_get(config, "sec_fundamentals.request_sleep_sec", 0.12)
        ),
        progress_path=progress_path,
        source_manifest_path=source_manifest_path,
        max_requests=args.max_requests,
        workers=args.workers,
    )
    payload = write_hydration_results(
        result_rows=result_rows,
        summary={
            **summary,
            "phase": args.phase,
            "priority_max": args.priority_max,
            "workers": max(1, args.workers),
            "available_request_count": len(requests),
            "progress_path": str(progress_path.resolve()),
        },
        result_path=result_path,
        manifest_path=manifest_path,
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 2 if payload["acceptance"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())

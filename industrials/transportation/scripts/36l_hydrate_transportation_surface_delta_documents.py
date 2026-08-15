#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, expand_env_vars, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.scripts._shared import DEFAULT_CONFIG  # noqa: E402


EXPECTED_MANIFEST_VERSION = "transportation_surface_delta_census_v1"
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "investable_v3"
    / "surface_delta"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Hydrate only exact document rows in the sealed 19-name "
            "surface-freight cache-gap manifest."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--gaps-csv", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument("--timeout-sec", type=float, default=45.0)
    parser.add_argument("--request-spacing-sec", type=float, default=0.20)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_retries < 1:
        raise ValueError("--max-retries must be at least 1")
    if args.timeout_sec <= 0 or args.request_spacing_sec < 0:
        raise ValueError("timeout must be positive and request spacing cannot be negative")

    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else DEFAULT_OUTPUT_ROOT / args.asof
    )
    gaps_path = (
        args.gaps_csv.expanduser().resolve()
        if args.gaps_csv
        else output_dir / "transportation_surface_delta_cache_gaps.csv"
    )

    helper = importlib.import_module(
        "industrials.transportation.scripts.36e_hydrate_transportation_tanker_delta_documents"
    )
    census_module = importlib.import_module(
        "industrials.transportation.scripts.36j_build_transportation_surface_delta_census"
    )
    profiles = census_module._rows(census_module.FILING_PROFILES)
    allowed_tickers = {row["ticker"].upper() for row in profiles}
    rows = helper.read_rows(gaps_path)
    seen_keys: set[tuple[str, str, str]] = set()
    for row in rows:
        if str(row.get("manifest_version") or "") != EXPECTED_MANIFEST_VERSION:
            raise ValueError("cache-gap row does not belong to the surface delta census")
        if str(row.get("gap_type") or "") != "SOURCE_DOCUMENT":
            raise ValueError("exact hydrator accepts SOURCE_DOCUMENT gaps only")
        if str(row.get("required_action") or "") != "HYDRATE_SEALED_DOCUMENT":
            raise ValueError("cache-gap row is not authorized for hydration")
        ticker = str(row.get("ticker") or "").strip().upper()
        if ticker not in allowed_tickers:
            raise ValueError(f"out-of-scope surface ticker in gap manifest: {ticker}")
        key = (
            ticker,
            str(row.get("accession_number") or "").strip(),
            str(row.get("document_name") or "").strip(),
        )
        if key in seen_keys:
            raise ValueError(f"duplicate exact document in gap manifest: {key}")
        seen_keys.add(key)

    cache_dir = resolve_path(cfg_get(config, "sec_fundamentals.cache_dir"), base_dir=base_dir)
    cache_root = (cache_dir / "sec_archive_xbrl").resolve()
    user_agent = expand_env_vars(str(cfg_get(config, "sec_fundamentals.user_agent")))
    results: list[dict[str, object]] = []
    for index, row in enumerate(rows, start=1):
        result = helper.fetch_document(
            row,
            cache_root=cache_root,
            user_agent=user_agent,
            max_retries=args.max_retries,
            timeout_sec=args.timeout_sec,
            request_spacing_sec=args.request_spacing_sec,
        )
        results.append(result)
        if index % 25 == 0 or index == len(rows):
            hydrated = sum(item["fetch_status"] == "HYDRATED" for item in results)
            failed = sum(item["fetch_status"] == "FAILED" for item in results)
            print(
                f"surface exact hydration progress={index}/{len(rows)} "
                f"hydrated={hydrated} failed={failed}",
                flush=True,
            )

    failures = [
        row
        for row in results
        if row["fetch_status"] not in {"HYDRATED", "ALREADY_CACHED"}
    ]
    summary: dict[str, Any] = {
        "acceptance": "PASS" if not failures else "NO_GO",
        "asof_date": args.asof,
        "manifest_version": EXPECTED_MANIFEST_VERSION,
        "requested_document_count": len(rows),
        "hydrated_document_count": sum(row["fetch_status"] == "HYDRATED" for row in results),
        "already_cached_document_count": sum(
            row["fetch_status"] == "ALREADY_CACHED" for row in results
        ),
        "failed_document_count": len(failures),
        "network_request_count": sum(int(row["network_requests"]) for row in results),
        "calibration_authorized": False,
        "historical_reconstruction_authorized": False,
        "production_promotion_authorized": False,
        "next_gate": "RERUN_SURFACE_DELTA_CENSUS",
    }
    write_csv_atomic(
        output_dir / "transportation_surface_delta_exact_hydration.csv",
        helper.RESULT_FIELDS,
        results,
    )
    write_text_atomic(
        output_dir / "transportation_surface_delta_exact_hydration.json",
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())

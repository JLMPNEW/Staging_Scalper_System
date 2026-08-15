#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation import source_census  # noqa: E402
from industrials.transportation.contracts import file_sha256  # noqa: E402
from industrials.transportation.discovery_contract import SCOPE_FIELDS, SUPPORTING_SCOPE_FIELDS  # noqa: E402
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)


DATA_ROOT = PROJECT_ROOT / "industrials" / "transportation" / "data"
SOURCE_MAP = DATA_ROOT / "transportation_surface_metric_source_map_v1.csv"
FILING_PROFILES = DATA_ROOT / "transportation_surface_filing_profiles_v1.csv"
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "investable_v3"
    / "surface_delta"
)
DERIVED_METRICS = ("surface_volume_growth",)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a read-only, versioned source census for the 19-name "
            "surface-freight cohort and every applicable direct surface KPI."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {str(key): str(value or "").strip() for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def _pipe(value: object) -> tuple[str, ...]:
    return tuple(item.strip() for item in str(value or "").split("|") if item.strip())


def _surface_contract(
    registry_path: Path,
) -> tuple[tuple[str, ...], tuple[str, ...], list[dict[str, object]]]:
    registry = {row["metric_id"]: row for row in _rows(registry_path)}
    profiles = _rows(FILING_PROFILES)
    tickers = tuple(row["ticker"].upper() for row in profiles)
    source_rows = _rows(SOURCE_MAP)
    direct_metrics = tuple(
        row["metric_id"]
        for row in source_rows
        if row["metric_id"] in registry
        and registry[row["metric_id"]]["source_lane"] == "DP"
    )
    missing = sorted(
        row["metric_id"]
        for row in source_rows
        if row["metric_id"] not in registry and row["metric_id"] not in DERIVED_METRICS
    )
    if missing:
        raise ValueError(f"surface metrics absent from parser registry={missing}")

    scope_rows: list[dict[str, object]] = []
    input_hash = file_sha256(SOURCE_MAP)
    for source in source_rows:
        metric_id = source["metric_id"]
        if metric_id not in direct_metrics:
            continue
        metric = registry[metric_id]
        for ticker in _pipe(source["applicable_tickers"]):
            if ticker not in tickers:
                raise ValueError(f"{metric_id}: unknown surface ticker={ticker}")
            scope_rows.append(
                {
                    "scope_version": "transportation_surface_delta_scope_v1",
                    "registry_version": "transportation_metrics_v3_discovery",
                    "policy_version": SOURCE_MAP.stem,
                    "input_contract_hash": input_hash,
                    "ticker": ticker,
                    "universe_role": "active",
                    "calibration_cohort": "surface_freight_core",
                    "industry": "Surface Freight",
                    "primary_archetype": "surface_freight_operator",
                    "applicability_tags": "surface_freight_core",
                    "development_overlay": "0",
                    "metric_id": metric_id,
                    "metric_pack": metric["metric_pack"],
                    "source_lane": metric["source_lane"],
                    "applicability_status": "APPLICABLE",
                    "applicability_reason": "v3_surface_metric_source_map",
                    "unit_contract": metric["unit_contract"],
                    "period_type": metric["period_type"],
                    "max_staleness_days": metric["max_staleness_days"],
                    "scoring_posture": metric["scoring_posture"],
                    "comparison_population": metric["comparison_population"],
                    "bounds_policy": metric["bounds_policy"],
                    "discovery_status": "coverage_pending",
                }
            )
    return tickers, direct_metrics, scope_rows


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    parser_cfg = family["dedicated_parser"]
    universe = family["universe"]
    foundation = resolve_foundation(config_path, args.db)
    base_dir = config_path.parent
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else DEFAULT_OUTPUT_ROOT / args.asof
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    scope_path = output_dir / "transportation_surface_delta_scope.csv"
    support_scope_path = output_dir / "transportation_surface_delta_support_scope.csv"
    dp0_path = output_dir / "transportation_surface_delta_dp0.json"
    gap_override_path = output_dir / "transportation_surface_delta_gap_overrides.csv"
    census_path = output_dir / "transportation_surface_delta_source_census.csv"
    decisions_path = output_dir / "transportation_surface_delta_source_decisions.csv"
    gaps_path = output_dir / "transportation_surface_delta_cache_gaps.csv"
    manifest_path = output_dir / "transportation_surface_delta_census_manifest.json"

    tickers, direct_metrics, scope_rows = _surface_contract(
        resolve_path(parser_cfg["discovery_registry_csv"], base_dir=base_dir)
    )
    write_csv_atomic(scope_path, SCOPE_FIELDS, scope_rows)
    write_csv_atomic(support_scope_path, SUPPORTING_SCOPE_FIELDS, [])
    write_csv_atomic(gap_override_path, source_census.GAP_OVERRIDE_FIELDS, [])
    write_text_atomic(
        dp0_path,
        json.dumps(
            {
                "model_family": MODEL_FAMILY,
                "contract_version": "transportation_surface_delta_dp0_v1",
                "identity_count": len(tickers),
                "direct_metric_count": len(direct_metrics),
                "hashes": {
                    "scope_sha256": file_sha256(scope_path),
                    "source_map_sha256": file_sha256(SOURCE_MAP),
                    "filing_profiles_sha256": file_sha256(FILING_PROFILES),
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )

    cache_dir = resolve_path(cfg_get(config, "sec_fundamentals.cache_dir"), base_dir=base_dir)
    original_members = source_census._members
    original_registry = source_census.get_registry
    full_registry = original_registry()
    direct_requests = tuple(
        request
        for request in full_registry.parser_metrics
        if request.metric_name in set(direct_metrics)
    )
    if len(direct_requests) != len(direct_metrics):
        found = {request.metric_name for request in direct_requests}
        raise ValueError(
            "adapter does not expose every direct surface metric: "
            f"missing={sorted(set(direct_metrics) - found)}"
        )

    def selected_members(*call_args: Any, **call_kwargs: Any) -> dict[str, dict[str, str]]:
        members = original_members(*call_args, **call_kwargs)
        missing = sorted(set(tickers) - set(members))
        if missing:
            raise ValueError(f"surface tickers missing from universe membership={missing}")
        return {ticker: members[ticker] for ticker in tickers}

    source_census._members = selected_members
    source_census.get_registry = lambda: SimpleNamespace(
        parser_metrics=direct_requests,
        document_keywords=full_registry.document_keywords,
        adapter_version=full_registry.adapter_version,
    )
    build_kwargs = {
        "cache_dir": cache_dir,
        "submissions_cache_dir": cache_dir / "sec_submissions",
        "final_scope_path": scope_path,
        "support_scope_path": support_scope_path,
        "listing_dates_path": foundation.listing_path,
        "continuity_path": resolve_path(
            universe["security_continuity_overrides_csv"], base_dir=base_dir
        ),
        "dp0_manifest_path": dp0_path,
        "gap_override_path": gap_override_path,
        "manifest_version": "transportation_surface_delta_census_v1",
        "source_id": str(cfg_get(config, "sec_fundamentals.submissions_source_id")),
        "active_source_id": foundation.seed_source_id,
        "historical_source_id": foundation.historical_source_id,
        "start_date": "2017-11-28",
        "legacy_inactive_start_date": "2000-01-01",
        "asof_date": args.asof,
        "expected_identity_count": len(tickers),
        "event_metric_anchor_accessions": frozenset(),
    }
    try:
        with source_census.read_only_connection(
            foundation.db_path,
            timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0)),
        ) as connection:
            first = source_census.build_source_census(
                connection,
                expected_base_accession_count=-1,
                **build_kwargs,
            )
            census_rows, decisions, gaps, summary = source_census.build_source_census(
                connection,
                expected_base_accession_count=int(first[3]["base_accession_count"]),
                **build_kwargs,
            )
    finally:
        source_census._members = original_members
        source_census.get_registry = original_registry

    payload = source_census.write_source_census(
        census_rows=census_rows,
        decisions=decisions,
        gaps=gaps,
        summary=summary,
        census_path=census_path,
        decisions_path=decisions_path,
        gaps_path=gaps_path,
        manifest_path=manifest_path,
    )
    payload["execution_scope"] = {
        "tickers": list(tickers),
        "direct_metric_ids": list(direct_metrics),
        "derived_metric_ids": list(DERIVED_METRICS),
        "source_map_sha256": file_sha256(SOURCE_MAP),
        "filing_profiles_sha256": file_sha256(FILING_PROFILES),
        "historical_reconstruction_authorized": False,
        "calibration_authorized": False,
        "production_promotion_authorized": False,
    }
    write_text_atomic(manifest_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["acceptance"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())

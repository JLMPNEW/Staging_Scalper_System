#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.software_infrastructure.dedicated_parser_baseline import (  # noqa: E402
    open_read_only_database,
    parse_iso_date,
)
from technology.software_infrastructure.software_nrr_discovery import (  # noqa: E402
    build_nrr_discovery,
    existing_nrr_coverage,
    limit_nrr_hydration_scope,
    load_likely_nrr_tickers,
    load_nrr_applicable_cohorts,
    load_nrr_filings,
    select_nrr_accessions,
)
from technology.software_infrastructure.software_parser_hydration import (  # noqa: E402
    atomic_csv,
    atomic_json,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_APPLICABILITY = (
    PACKAGE_ROOT
    / "software_infrastructure"
    / "data"
    / "software_infrastructure_metric_applicability.csv"
)
DEFAULT_CACHE = PROJECT_ROOT / "output" / "technology_cache" / "dedicated_parser"
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "output"
    / "technology_reports"
    / "software_infrastructure"
    / "nrr_discovery"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Plan targeted, PIT-scoped NRR discovery from cached software "
            "filings and emit exact accessions for resumable hydration."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--start-date", default="2010-01-01")
    parser.add_argument(
        "--event-window-days",
        type=int,
        default=21,
    )
    parser.add_argument("--max-target-tickers", type=int, default=30)
    parser.add_argument("--minimum-historical-tickers", type=int, default=8)
    parser.add_argument("--max-years-per-ticker", type=int, default=5)
    parser.add_argument(
        "--applicability",
        type=Path,
        default=DEFAULT_APPLICABILITY,
    )
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def _write_optional_csv(
    path: Path,
    rows: list[dict[str, object]],
) -> str:
    if not rows:
        return ""
    atomic_csv(path, rows)
    return str(path.resolve())


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(
            cfg_get(config, "paths.database_path"),
            base_dir=config_path.parent,
        )
    )
    timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))
    asof_date = parse_iso_date(args.asof, field_name="asof")
    start_date = parse_iso_date(
        args.start_date,
        field_name="start_date",
    )
    if start_date > asof_date:
        raise ValueError("start-date cannot be after asof")
    cohorts = load_nrr_applicable_cohorts(
        args.applicability.expanduser().resolve()
    )
    with open_read_only_database(db_path, timeout_sec=timeout_sec) as conn:
        filings = load_nrr_filings(
            conn,
            start_date=start_date,
            asof_date=asof_date,
            cohorts=cohorts,
        )
        selected = select_nrr_accessions(
            filings,
            event_window_days=max(0, args.event_window_days),
        )
        likely_tickers = load_likely_nrr_tickers(
            conn,
            max_tickers=max(1, args.max_target_tickers),
            minimum_historical=max(0, args.minimum_historical_tickers),
        )
        existing = existing_nrr_coverage(conn)
    cache_dir = args.cache_dir.expanduser().resolve()
    hits, hydration_population, source_manifest = build_nrr_discovery(
        selected_filings=selected,
        cache_dir=cache_dir,
    )
    hydration_scope = limit_nrr_hydration_scope(
        selected,
        likely_tickers=(str(row["ticker"]) for row in likely_tickers),
        max_years_per_ticker=max(1, args.max_years_per_ticker),
        event_window_days=max(0, args.event_window_days),
    )
    target_selected = [
        row
        for row in selected
        if str(row["accession_number"]) in hydration_scope
    ]
    priority = {
        str(row["ticker"]): row for row in likely_tickers
    }
    hydration = []
    for row in hydration_population:
        if str(row["accession_number"]) not in hydration_scope:
            continue
        score = priority.get(str(row["ticker"]), {})
        hydration.append(
            {
                **row,
                "disclosure_priority_score": score.get(
                    "disclosure_score", 0
                ),
                "priority_historical_member_flag": score.get(
                    "historical_member_flag", 0
                ),
            }
        )
    output_dir = args.output_dir.expanduser().resolve() / asof_date
    output_dir.mkdir(parents=True, exist_ok=True)
    selected_path = output_dir / "software_nrr_selected_accessions.csv"
    atomic_csv(selected_path, target_selected)
    likely_path = output_dir / "software_nrr_likely_tickers.csv"
    atomic_csv(likely_path, likely_tickers)
    hits_path = output_dir / "software_nrr_cached_document_hits.csv"
    hydration_path = output_dir / "software_nrr_hydration_accessions.csv"
    source_path = (
        output_dir / "software_nrr_cached_source_manifest.csv"
    )
    hit_output = _write_optional_csv(hits_path, hits)
    hydration_output = _write_optional_csv(hydration_path, hydration)
    source_output = _write_optional_csv(source_path, source_manifest)
    manifest = {
        "manifest_version": "software_nrr_discovery_plan_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "model_family": "software_infrastructure",
        "metric_name": "net_revenue_retention",
        "start_date": start_date,
        "asof_date": asof_date,
        "applicable_cohorts": list(cohorts),
        "discovery_population_filing_count": len(selected),
        "selected_filing_count": len(target_selected),
        "selected_ticker_count": len(
            {str(row["ticker"]) for row in target_selected}
        ),
        "target_historical_ticker_count": sum(
            int(row["historical_member_flag"]) for row in likely_tickers
        ),
        "likely_tickers_path": str(likely_path.resolve()),
        "likely_tickers_sha256": file_sha256(likely_path),
        "cached_document_hit_count": len(hits),
        "exact_parser_document_count": sum(
            row["match_type"] == "exact_parser_pattern"
            for row in hits
        ),
        "alias_review_document_count": sum(
            row["match_type"] == "alias_review_pattern"
            for row in hits
        ),
        "accession_requiring_hydration_count": len(hydration),
        "sealed_parser_source_document_count": len(source_manifest),
        "existing_parser_coverage": existing,
        "selected_accessions_path": str(selected_path.resolve()),
        "selected_accessions_sha256": file_sha256(selected_path),
        "cached_hits_path": hit_output,
        "hydration_accessions_path": hydration_output,
        "cached_source_manifest_path": source_output,
        "next_commands": {
            "hydrate": (
                "07c_hydrate_software_infrastructure_parser_documents.py "
                "--accession-file "
                f'"{hydration_path}" --start-date {start_date} '
                f"--asof {asof_date} --execute"
                if hydration
                else ""
            ),
            "shadow_parse_cached_hits": (
                "07d_run_software_infrastructure_parser_shadow.py "
                f'--asof {asof_date} --source-manifest "{source_path}"'
                if source_manifest
                else ""
            ),
        },
        "production_facts_modified_flag": 0,
        "production_scores_modified_flag": 0,
    }
    atomic_json(
        output_dir / "software_nrr_discovery_manifest.json",
        manifest,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

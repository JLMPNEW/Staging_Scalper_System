#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.software_infrastructure.dedicated_parser_baseline import (  # noqa: E402
    open_read_only_database,
)
from technology.software_infrastructure.software_disclosure_census import (  # noqa: E402
    build_cache_scope,
    select_recent_earnings_events,
)
from technology.software_infrastructure.software_nrr_discovery import (  # noqa: E402
    load_nrr_filings,
)
from technology.software_infrastructure.software_parser_hydration import (  # noqa: E402
    atomic_csv,
    atomic_json,
)
from technology.software_infrastructure.software_specialized_metrics import (  # noqa: E402
    load_policy,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_POLICY = (
    PACKAGE_ROOT
    / "software_infrastructure"
    / "review_policies"
    / "software_arr_policy_v1.json"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "output"
    / "technology_reports"
    / "software_infrastructure"
    / "arr_historical_research"
    / date.today().isoformat()
    / "hydration_scope"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build the complete earnings-event accession scope for approved "
            "ARR issuers without making network requests."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--start-date", default="2018-01-01")
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--event-window-days", type=int, default=21)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def _iso(value: str, *, field: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc


def main() -> int:
    args = parse_args()
    start_date = _iso(args.start_date, field="start-date")
    asof_date = _iso(args.asof, field="asof")
    if start_date > asof_date:
        raise ValueError("start-date cannot be after asof")
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
    cache_dir = PROJECT_ROOT / "output" / "technology_cache" / "dedicated_parser"
    policy_path = args.policy.expanduser().resolve()
    policy = load_policy(policy_path)
    tickers = {
        str(row["ticker"])
        for row in policy["decisions"]
        if str(row.get("ticker") or "")
    }
    timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))
    with open_read_only_database(db_path, timeout_sec=timeout_sec) as conn:
        cohorts = tuple(
            str(row[0])
            for row in conn.execute(
                """
                SELECT DISTINCT calibration_cohort_id
                FROM dim_technology_taxonomy
                WHERE model_family = 'software_infrastructure'
                  AND calibration_cohort_id <> ''
                ORDER BY calibration_cohort_id
                """
            ).fetchall()
        )
        filings = [
            row
            for row in load_nrr_filings(
                conn,
                start_date=start_date,
                asof_date=asof_date,
                cohorts=cohorts,
            )
            if str(row["ticker"]) in tickers
        ]
        membership_rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT ticker, start_date,
                       COALESCE(NULLIF(end_date, ''), '9999-12-31') AS end_date
                FROM dim_universe_membership
                WHERE model_family = 'software_infrastructure'
                  AND point_in_time_flag = 1
                ORDER BY ticker, start_date
                """
            ).fetchall()
            if str(row["ticker"]) in tickers
        ]
        trading_dates = [
            str(row[0])
            for row in conn.execute(
                """
                SELECT DISTINCT bar_date
                FROM fact_price_ohlcv
                WHERE ticker = 'QQQ' AND bar_date BETWEEN ? AND ?
                ORDER BY bar_date
                """,
                (start_date, asof_date),
            ).fetchall()
        ]
    max_contemporaneous_count = 0
    max_contemporaneous_date = ""
    for trading_date in trading_dates:
        count = len(
            {
                str(row["ticker"])
                for row in membership_rows
                if str(row["start_date"]) <= trading_date <= str(row["end_date"])
            }
        )
        if count > max_contemporaneous_count:
            max_contemporaneous_count = count
            max_contemporaneous_date = trading_date
    max_contemporaneous_count = 0
    max_contemporaneous_date = ""
    for trading_date in trading_dates:
        count = len(
            {
                str(row["ticker"])
                for row in membership_rows
                if str(row["start_date"]) <= trading_date <= str(row["end_date"])
            }
        )
        if count > max_contemporaneous_count:
            max_contemporaneous_count = count
            max_contemporaneous_date = trading_date
    selected = select_recent_earnings_events(
        filings,
        max_events_per_ticker=10000,
        event_window_days=max(0, args.event_window_days),
    )
    accession_rows, source_rows = build_cache_scope(
        selected,
        cache_dir=cache_dir,
        asof_date=asof_date,
    )
    missing_rows = [
        row for row in accession_rows if row["cache_status"] == "MISSING_CACHE"
    ]
    output_dir = args.output_dir.expanduser().resolve()
    all_path = output_dir / "software_arr_earnings_event_accessions.csv"
    missing_path = output_dir / "software_arr_missing_cache_accessions.csv"
    source_path = output_dir / "software_arr_cached_source_documents.csv"
    manifest_path = output_dir / "software_arr_hydration_scope_manifest.json"
    atomic_csv(all_path, accession_rows)
    atomic_csv(missing_path, missing_rows)
    atomic_csv(source_path, source_rows)
    manifest = {
        "manifest_version": "software_arr_hydration_scope_v1",
        "start_date": start_date,
        "asof_date": asof_date,
        "approved_policy_path": str(policy_path),
        "approved_policy_sha256": file_sha256(policy_path),
        "approved_issuer_count": len(tickers),
        "max_contemporaneous_approved_issuer_count": (
            max_contemporaneous_count
        ),
        "max_contemporaneous_approved_issuer_date": (
            max_contemporaneous_date
        ),
        "filing_metadata_count": len(filings),
        "selected_earnings_event_count": len(accession_rows),
        "selected_earnings_event_issuer_count": len(
            {str(row["ticker"]) for row in accession_rows}
        ),
        "cached_accession_count": len(accession_rows) - len(missing_rows),
        "missing_cache_accession_count": len(missing_rows),
        "cached_source_document_count": len(source_rows),
        "all_accessions_path": str(all_path),
        "all_accessions_sha256": file_sha256(all_path),
        "missing_accessions_path": str(missing_path),
        "missing_accessions_sha256": file_sha256(missing_path),
        "cached_sources_path": str(source_path),
        "cached_sources_sha256": file_sha256(source_path),
        "network_requests_made_flag": 0,
    }
    atomic_json(manifest_path, manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

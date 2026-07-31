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
from technology.software_infrastructure.software_disclosure_census import (  # noqa: E402
    build_cache_scope,
    build_metric_census,
    load_metric_evidence,
    load_parser_completion,
    load_recent_earnings_events,
    manifest_payload,
)
from technology.software_infrastructure.software_parser_hydration import (  # noqa: E402
    atomic_csv,
    atomic_json,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_CACHE = PROJECT_ROOT / "output" / "technology_cache" / "dedicated_parser"
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "output"
    / "technology_reports"
    / "software_infrastructure"
    / "disclosure_census"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a cache-first census of numeric specialized-metric "
            "disclosures in each issuer's latest PIT earnings events."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--start-date", default="2010-01-01")
    parser.add_argument("--max-events-per-ticker", type=int, default=4)
    parser.add_argument("--event-window-days", type=int, default=21)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def _optional_csv(path: Path, rows: list[dict[str, object]]) -> str:
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
    start_date = parse_iso_date(args.start_date, field_name="start_date")
    if start_date > asof_date:
        raise ValueError("start-date cannot be after asof")
    max_events = max(1, args.max_events_per_ticker)
    event_window = max(0, args.event_window_days)
    with open_read_only_database(db_path, timeout_sec=timeout_sec) as conn:
        universe, selected = load_recent_earnings_events(
            conn,
            start_date=start_date,
            asof_date=asof_date,
            max_events_per_ticker=max_events,
            event_window_days=event_window,
        )
    cache_dir = args.cache_dir.expanduser().resolve()
    accessions, source_rows = build_cache_scope(
        selected,
        cache_dir=cache_dir,
        asof_date=asof_date,
    )
    with open_read_only_database(db_path, timeout_sec=timeout_sec) as conn:
        completed = load_parser_completion(
            conn,
            accession_rows=accessions,
        )
        evidence = load_metric_evidence(
            conn,
            accession_rows=accessions,
        )
    detail, summary = build_metric_census(
        universe=universe,
        accession_rows=accessions,
        completed_accessions=completed,
        evidence_rows=evidence,
        max_events_per_ticker=max_events,
    )
    parser_scope_complete = len(completed) == len(accessions)

    output_dir = args.output_dir.expanduser().resolve() / asof_date
    output_dir.mkdir(parents=True, exist_ok=True)
    accession_path = output_dir / "software_disclosure_census_accessions.csv"
    source_path = output_dir / "software_disclosure_census_source_manifest.csv"
    hydration_path = output_dir / "software_disclosure_census_hydration_accessions.csv"
    detail_path = output_dir / "software_disclosure_census_by_ticker_metric.csv"
    summary_path = output_dir / "software_disclosure_census_summary.csv"
    atomic_csv(accession_path, accessions)
    source_output = _optional_csv(source_path, source_rows)
    missing = [
        {
            "accession_number": row["accession_number"],
            "ticker": row["ticker"],
            "cik": row["cik"],
            "form_type": row["form_type"],
            "filing_date": row["filing_date"],
            "selection_tier": row["selection_tier"],
            "cache_status": row["cache_status"],
        }
        for row in accessions
        if row["cache_status"] == "MISSING_CACHE"
    ]
    hydration_output = _optional_csv(hydration_path, missing)
    atomic_csv(detail_path, detail)
    atomic_csv(summary_path, summary)
    manifest = manifest_payload(
        asof_date=asof_date,
        start_date=start_date,
        universe=universe,
        accession_rows=accessions,
        source_rows=source_rows,
        detail_rows=detail,
        summary_rows=summary,
    )
    manifest.update(
        {
            "generated_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ),
            "event_window_days": event_window,
            "max_events_per_ticker": max_events,
            "parser_completed_accession_count": len(completed),
            "metric_evidence_row_count": len(evidence),
            "accessions_path": str(accession_path.resolve()),
            "accessions_sha256": file_sha256(accession_path),
            "source_manifest_path": source_output,
            "source_manifest_sha256": (
                file_sha256(source_path) if source_output else ""
            ),
            "hydration_accessions_path": hydration_output,
            "detail_path": str(detail_path.resolve()),
            "detail_sha256": file_sha256(detail_path),
            "summary_path": str(summary_path.resolve()),
            "summary_sha256": file_sha256(summary_path),
            "next_commands": {
                "parse_cached_scope": (
                    "07d_run_software_infrastructure_parser_shadow.py "
                    f'--asof {asof_date} --source-manifest "{source_path}"'
                    if source_output and not parser_scope_complete
                    else ""
                ),
                "hydrate_missing_scope": (
                    "07c_hydrate_software_infrastructure_parser_documents.py "
                    f'--accession-file "{hydration_path}" '
                    f"--start-date {start_date} --asof {asof_date} --execute"
                    if hydration_output
                    else ""
                ),
            },
        }
    )
    atomic_json(
        output_dir / "software_disclosure_census_manifest.json",
        manifest,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

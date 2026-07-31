#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.required_metric_repair import (  # noqa: E402
    ACCESSION_FIELDS,
    DEPENDENCY_FIELDS,
    PAIR_FIELDS,
    REPAIR_SCOPE_VERSION,
    build_accession_manifest,
    build_repair_contract,
    read_scope,
    summarize_contract,
)
from industrials.transportation.scripts._shared import DEFAULT_CONFIG  # noqa: E402


DEFAULT_SCOPE = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "review_policies"
    / "transportation_required_metric_repair_scope.csv"
)
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "required_metric_repair"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Freeze the bounded 19-ticker/32-pair transportation required-"
            "metric repair contract and one-pass SEC accession manifest."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--scope-csv", type=Path, default=DEFAULT_SCOPE)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--annual-filings", type=int, default=3)
    parser.add_argument("--interim-filings", type=int, default=8)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    asof_date = str(args.asof)[:10]
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = (
        args.db.expanduser().resolve()
        if args.db is not None
        else resolve_path(
            cfg_get(config, "paths.database_path"),
            base_dir=config_path.parent,
        )
    )
    scope_path = args.scope_csv.expanduser().resolve()
    output_dir = args.output_root.expanduser().resolve() / asof_date
    pair_path = output_dir / "transportation_required_metric_repair_pairs.csv"
    dependency_path = (
        output_dir / "transportation_required_metric_repair_dependencies.csv"
    )
    accession_path = (
        output_dir / "transportation_required_metric_repair_accessions.csv"
    )
    manifest_path = (
        output_dir / "transportation_required_metric_repair_plan.json"
    )
    scope_rows = read_scope(scope_path)
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        pair_rows, dependency_rows = build_repair_contract(
            connection,
            scope_rows=scope_rows,
            asof_date=asof_date,
        )
        accession_rows = build_accession_manifest(
            connection,
            pair_rows=pair_rows,
            asof_date=asof_date,
            annual_limit=args.annual_filings,
            interim_limit=args.interim_filings,
        )
    finally:
        connection.close()
    summary = summarize_contract(
        pair_rows=pair_rows,
        dependency_rows=dependency_rows,
        accession_rows=accession_rows,
    )
    errors: list[str] = []
    if summary["pair_count"] != 32:
        errors.append(f"pair_count={summary['pair_count']} expected=32")
    if summary["ticker_count"] != 19:
        errors.append(f"ticker_count={summary['ticker_count']} expected=19")
    if summary["financial_ticker_count"] != 18:
        errors.append(
            "financial_ticker_count="
            f"{summary['financial_ticker_count']} expected=18"
        )
    if summary["market_ticker_count"] != 1:
        errors.append(
            f"market_ticker_count={summary['market_ticker_count']} expected=1"
        )
    if summary["accession_ticker_count"] != 18:
        errors.append(
            "accession_ticker_count="
            f"{summary['accession_ticker_count']} expected=18"
        )
    write_csv_atomic(pair_path, PAIR_FIELDS, pair_rows)
    write_csv_atomic(dependency_path, DEPENDENCY_FIELDS, dependency_rows)
    write_csv_atomic(accession_path, ACCESSION_FIELDS, accession_rows)
    payload = {
        "acceptance": "PASS" if not errors else "FAIL",
        "gate": "TRANSPORTATION_REQUIRED_METRIC_REPAIR_PLAN",
        "scope_version": REPAIR_SCOPE_VERSION,
        "asof_date": asof_date,
        "database_path": str(db_path),
        "database_read_only": True,
        "network_requests": 0,
        "parser_invocations": 0,
        "feature_build_invocations": 0,
        "scope_sha256": file_sha256(scope_path),
        **summary,
        "artifacts": {
            "pair_contract": {
                "path": str(pair_path.resolve()),
                "sha256": file_sha256(pair_path),
                "row_count": len(pair_rows),
            },
            "dependency_contract": {
                "path": str(dependency_path.resolve()),
                "sha256": file_sha256(dependency_path),
                "row_count": len(dependency_rows),
            },
            "accession_manifest": {
                "path": str(accession_path.resolve()),
                "sha256": file_sha256(accession_path),
                "row_count": len(accession_rows),
            },
        },
        "rubi_market_history_policy": (
            "NO_FILING_RETRIEVAL; require 252 valid adjusted bars"
        ),
        "errors": errors,
        "next_gate": (
            "RUN_ONE_BOUNDED_ARCHIVE_PARSE"
            if not errors
            else "REVIEW_REQUIRED_METRIC_REPAIR_PLAN_ERRORS"
        ),
    }
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())

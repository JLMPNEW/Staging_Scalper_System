#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect  # noqa: E402
from industrials.machinery.scoring import (  # noqa: E402
    dated_path,
    parse_asof,
    read_rows,
    validate_scoring_feature_rows,
    write_json_atomic,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the machinery scoring feature contract.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--output-json", type=Path, default=None)
    return parser.parse_args()


def expected_members(conn: sqlite3.Connection, *, asof: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT UPPER(ticker) AS ticker
        FROM dim_universe_membership
        WHERE model_family = 'machinery'
          AND start_date <= ?
          AND (end_date IS NULL OR end_date = '' OR end_date >= ?)
        """,
        (asof, asof),
    ).fetchall()
    return {str(row["ticker"]) for row in rows}


def main() -> int:
    args = parse_args()
    asof = parse_asof(args.asof)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(
        cfg_get(config, "paths.database_path"),
        base_dir=base_dir,
    )
    feature_root = resolve_path(cfg_get(config, "machinery_scoring.feature_output_root"), base_dir=base_dir)
    input_path = args.input_csv.expanduser().resolve() if args.input_csv else dated_path(
        feature_root,
        asof,
        "machinery_scoring_feature_contract.csv",
    )
    output_path = args.output_json.expanduser().resolve() if args.output_json else dated_path(
        feature_root,
        asof,
        "machinery_scoring_feature_validation.json",
    )
    rows = read_rows(input_path)
    errors = validate_scoring_feature_rows(rows, asof=asof)
    actual_tickers = {str(row.get("ticker") or "").strip().upper() for row in rows if row.get("ticker")}
    timeout = float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))
    with connect(db_path, timeout_sec=timeout) as conn:
        expected_tickers = expected_members(conn, asof=asof)
    missing_tickers = sorted(expected_tickers - actual_tickers)
    unexpected_tickers = sorted(actual_tickers - expected_tickers)
    if missing_tickers:
        errors.append(f"missing point-in-time members={missing_tickers[:20]}")
    if unexpected_tickers:
        errors.append(f"unexpected point-in-time members={unexpected_tickers[:20]}")
    summary = {
        "acceptance": "PASS" if not errors else "FAIL",
        "model_family": "machinery",
        "asof_date": asof,
        "database_path": str(db_path),
        "input_csv": str(input_path),
        "row_count": len(rows),
        "expected_member_count": len(expected_tickers),
        "rank_ready_count": sum(str(row.get("rank_ready_flag") or "") == "1" for row in rows),
        "errors": errors,
    }
    write_json_atomic(output_path, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())

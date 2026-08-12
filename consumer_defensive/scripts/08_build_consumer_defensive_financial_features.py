#!/usr/bin/env python3
"""Normalize SEC facts and build an acceptance-dated financial feature snapshot."""

from __future__ import annotations

# ruff: noqa: E402

import argparse
import json
import sys
from datetime import date
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from consumer_defensive.core.config import cfg_get, load_config
from consumer_defensive.core.db import connect, finish_run, start_run
from consumer_defensive.core.market_data import write_json
from consumer_defensive.core.script_runtime import (
    assert_stage4_raw_facts_ready,
    iso_date,
    stage4_output_dir,
)
from consumer_defensive.core.stage3_runtime import database_path
from consumer_defensive.core.stage4 import bootstrap_stage4, build_financial_features

DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--as-of", type=iso_date, default=date.today().isoformat())
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bundle = load_config(args.config)
    db_path = database_path(bundle, args.db)
    output_dir = stage4_output_dir(bundle, as_of=args.as_of, override=args.output_dir)
    timeout = float(cfg_get(bundle.payload, "runtime.sqlite_timeout_sec", 30.0))

    with connect(db_path, timeout_sec=timeout) as conn:
        bootstrap_stage4(conn, bundle)
        run_id = start_run(
            conn,
            run_type="consumer_defensive_stage4_financial_features",
            input_path=bundle.path,
        )
        try:
            readiness = assert_stage4_raw_facts_ready(conn, bundle, as_of=args.as_of)
            result = build_financial_features(conn, bundle, as_of=args.as_of)
            fx_missing = int(
                conn.execute(
                    """SELECT COUNT(*) FROM fact_financial_statement_canonical
                       WHERE definition_version=? AND quality_status='fx_missing' AND accepted_at<=?""",
                    (result["definition_version"], f"{args.as_of}T23:59:59Z"),
                ).fetchone()[0]
            )
            expected = int(readiness["expected_taxonomy_rows"])
            if int(result["canonical_facts"]) == 0:
                raise RuntimeError("No SEC facts matched the configured canonical financial concept map.")
            if int(result["feature_rows"]) != expected:
                raise RuntimeError(
                    f"Financial snapshot is incomplete: expected {expected} rows, wrote {result['feature_rows']}."
                )
            if fx_missing:
                raise RuntimeError(
                    f"Canonical financial facts have {fx_missing} missing FX conversions; run Stage 4 FX sync first."
                )
            result["canonical_fx_missing"] = fx_missing
            finish_run(
                conn,
                run_id=run_id,
                status="success",
                row_count=int(result["feature_rows"]),
                message=json.dumps(result, sort_keys=True),
            )
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", message=f"{type(exc).__name__}: {exc}")
            raise

    payload = {"database": str(db_path), "readiness": readiness, **result}
    report_path = output_dir / "financial_features_build.json"
    write_json(report_path, payload)
    print(json.dumps({**payload, "report": str(report_path)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

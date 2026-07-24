#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect, init_db  # noqa: E402
from industrials.transportation.contracts import (  # noqa: E402
    FINAL_RANK_FIELDS,
    read_rows,
    validate_rank_rows,
    write_manifest,
)
from industrials.transportation.scripts._shared import DEFAULT_CONFIG, MODEL_FAMILY  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Independently validate a transportation shadow rank table.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--output-json", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    asof = datetime.strptime(args.asof[:10], "%Y-%m-%d").date().isoformat()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    scoring = family["scoring"]
    base_dir = config_path.parent
    dashboard_dir = resolve_path(scoring["dashboard_root"], base_dir=base_dir) / asof
    input_path = args.input_csv.expanduser().resolve() if args.input_csv else (
        dashboard_dir / "transportation_final_rank_table.csv"
    )
    output_path = args.output_json.expanduser().resolve() if args.output_json else (
        dashboard_dir / "transportation_final_rank_table_validation.json"
    )
    rows = read_rows(input_path)
    errors = validate_rank_rows(rows, asof=asof)
    output_map = load_yaml(resolve_path(scoring["output_column_map"], base_dir=base_dir))
    required_columns = {str(value) for value in output_map.get("required_columns", [])}
    if not required_columns.issubset(set(FINAL_RANK_FIELDS)):
        errors.append("output column map requires columns absent from the final-rank contract")
    invariants = output_map.get("shadow_invariants", {})
    for row in rows:
        ticker = str(row.get("ticker") or "<blank>")
        for field, expected_value in invariants.items():
            if str(row.get(field) or "") != str(expected_value):
                errors.append(f"{ticker}: {field} violates configured shadow invariant")
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(
        cfg_get(config, "paths.database_path"), base_dir=base_dir
    )
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))) as conn:
        init_db(conn)
        expected = {
            str(row[0])
            for row in conn.execute(
                """
                SELECT ticker FROM dim_universe_membership
                WHERE model_family=? AND membership_source_id=? AND membership_status='active'
                  AND start_date<=? AND COALESCE(end_date,'9999-12-31')>=?
                """,
                (MODEL_FAMILY, str(family["universe"]["seed_source_id"]), asof, asof),
            ).fetchall()
        }
    actual = {str(row.get("ticker") or "") for row in rows}
    if actual != expected:
        errors.append(
            f"rank universe mismatch missing={sorted(expected-actual)[:20]} extra={sorted(actual-expected)[:20]}"
        )
    cohort_counts: dict[str, int] = {}
    for row in rows:
        cohort = str(row.get("calibration_cohort") or "")
        cohort_counts[cohort] = cohort_counts.get(cohort, 0) + 1
    result = {
        "acceptance": "PASS" if not errors else "FAIL",
        "model_family": MODEL_FAMILY,
        "asof_date": asof,
        "row_count": len(rows),
        "expected_row_count": len(expected),
        "cohort_counts": cohort_counts,
        "rank_ready_count": sum(row.get("rank_ready_flag") == "1" for row in rows),
        "portfolio_candidate_count": sum(row.get("portfolio_candidate_gate") == "1" for row in rows),
        "oos_score_valid_count": sum(row.get("oos_score_valid_flag") == "1" for row in rows),
        "errors": errors,
    }
    write_manifest(output_path, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())

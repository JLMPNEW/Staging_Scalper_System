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
    read_rows,
    validate_scoring_rows,
    write_manifest,
)
from industrials.transportation.scripts._shared import DEFAULT_CONFIG, MODEL_FAMILY  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate transportation scoring feature contract.")
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
    base_dir = config_path.parent
    input_path = args.input_csv.expanduser().resolve() if args.input_csv else resolve_path(
        family["scoring"]["feature_output_csv"], base_dir=base_dir
    )
    output_path = args.output_json.expanduser().resolve() if args.output_json else resolve_path(
        family["scoring"]["validation_output_json"], base_dir=base_dir
    )
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(
        cfg_get(config, "paths.database_path"), base_dir=base_dir
    )
    rows = read_rows(input_path)
    errors = validate_scoring_rows(rows, asof=asof)
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
    if str(family["scoring"].get("score_construction_mode") or "").startswith(
        "surface_freight_"
    ):
        surface_policy = load_yaml(
            resolve_path(
                family["scoring"]["surface_freight_score_policy"],
                base_dir=base_dir,
            )
        )
        expected &= {str(value) for value in surface_policy["eligible_tickers"]}
    actual = {str(row.get("ticker") or "") for row in rows}
    if actual != expected:
        errors.append(
            f"active universe mismatch missing={sorted(expected-actual)[:20]} extra={sorted(actual-expected)[:20]}"
        )
    result = {
        "acceptance": "PASS" if not errors else "FAIL",
        "model_family": MODEL_FAMILY,
        "asof_date": asof,
        "row_count": len(rows),
        "expected_row_count": len(expected),
        "rank_ready_count": sum(row.get("rank_ready_flag") == "1" for row in rows),
        "blocked_count": sum(row.get("rank_ready_flag") == "0" for row in rows),
        "errors": errors,
    }
    write_manifest(output_path, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())

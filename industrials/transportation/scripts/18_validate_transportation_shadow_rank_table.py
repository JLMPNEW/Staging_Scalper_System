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
from industrials.core.score_history import (  # noqa: E402
    validate_shadow_survivorship_sidecar,
)
from industrials.transportation.contracts import (  # noqa: E402
    FINAL_RANK_FIELDS,
    file_sha256,
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
    parser.add_argument(
        "--membership-mode",
        choices=("current", "pit"),
        default="current",
    )
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
    sidecar_path = input_path.with_name(
        "transportation_stage11_survivorship_calibration_panel.csv"
    )
    sidecar_rows = read_rows(sidecar_path) if sidecar_path.is_file() else []
    if not sidecar_path.is_file():
        errors.append(f"missing Stage 11 sidecar={sidecar_path}")
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
        if args.membership_mode == "current":
            expected_rows = conn.execute(
                """
                SELECT ticker FROM dim_universe_membership
                WHERE model_family=? AND membership_source_id=?
                  AND membership_status='active' AND start_date<=?
                  AND COALESCE(end_date,'9999-12-31')>=?
                """,
                (
                    MODEL_FAMILY,
                    str(family["universe"]["seed_source_id"]),
                    asof,
                    asof,
                ),
            ).fetchall()
        else:
            expected_rows = conn.execute(
                """
                SELECT DISTINCT ticker FROM dim_universe_membership
                WHERE model_family=? AND start_date<=?
                  AND COALESCE(end_date,'9999-12-31')>=?
                """,
                (MODEL_FAMILY, asof, asof),
            ).fetchall()
        expected = {str(row[0]) for row in expected_rows}
    if str(scoring.get("score_construction_mode") or "").startswith(
        "surface_freight_"
    ):
        surface_policy = load_yaml(
            resolve_path(
                scoring["surface_freight_score_policy"],
                base_dir=base_dir,
            )
        )
        expected &= {str(value) for value in surface_policy["eligible_tickers"]}
    actual = {str(row.get("ticker") or "") for row in rows}
    errors.extend(
        validate_shadow_survivorship_sidecar(
            sidecar_rows,
            asof_date=asof,
            expected_tickers=expected,
        )
    )
    manifest_path = input_path.with_name(
        "transportation_final_rank_table_manifest.json"
    )
    manifest = {}
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("acceptance") != "PASS":
            errors.append("rank manifest acceptance is not PASS")
        if str(manifest.get("rank_table_sha256") or "") != file_sha256(input_path):
            errors.append("rank manifest hash mismatch")
        if sidecar_path.is_file() and str(
            manifest.get("stage11_survivorship_calibration_panel_sha256") or ""
        ) != file_sha256(sidecar_path):
            errors.append("Stage 11 sidecar manifest hash mismatch")
    else:
        errors.append(f"missing rank manifest={manifest_path}")
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
        "membership_mode": args.membership_mode,
        "row_count": len(rows),
        "stage11_sidecar_row_count": len(sidecar_rows),
        "stage11_calibration_input_eligible_count": sum(
            row.get("stage11_calibration_input_eligible_flag") == "1"
            for row in sidecar_rows
        ),
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

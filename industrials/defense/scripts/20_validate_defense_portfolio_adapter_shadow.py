#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.scores.adapters import run_adapter  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
MODEL_FAMILY = "defense"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate defense shadow rank-table ingestion through the portfolio tech_family adapter.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--sector-output-root", type=Path, default=PROJECT_ROOT / "output")
    return parser.parse_args()


def parse_asof(raw: str) -> str:
    return datetime.strptime(raw.strip(), "%Y-%m-%d").date().isoformat()


def active_defense_count(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(
            conn.execute(
                """
                SELECT COUNT(DISTINCT c.ticker)
                FROM dim_company c
                JOIN dim_industrials_taxonomy t ON t.company_id = c.company_id
                WHERE c.is_active = 1 AND t.model_family = 'defense'
                """
            ).fetchone()[0]
            or 0
        )


def main() -> int:
    args = parse_args()
    asof = parse_asof(args.asof)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    expected_rows = active_defense_count(db_path)
    snapshot_root = resolve_path(
        str(
            cfg_get(
                config,
                "oos_calibration_standards.families.defense.snapshot_history_root",
                "../output/industrials/defense/dashboard",
            )
        ),
        base_dir=base_dir,
    )
    sector_output_root = args.sector_output_root.expanduser().resolve()
    try:
        snapshot_rel = snapshot_root.resolve().relative_to(sector_output_root)
    except ValueError as exc:
        raise ValueError(
            f"Configured oos_calibration_standards.families.defense.snapshot_history_root "
            f"({snapshot_root}) is not under the sector output root ({sector_output_root}); "
            "the portfolio adapter resolves dated snapshots relative to that root."
        ) from exc
    adapter_cfg = {
        "model_family": MODEL_FAMILY,
        "adapter": "tech_family",
        "file_mode": "dated",
        "file_path": "/".join([*snapshot_rel.parts, "{yyyy-mm-dd}", "defense_final_rank_table.csv"]),
        "sector": "Industrials",
        "industry": "Aerospace & Defense",
        "industry_aggregate": "Aerospace & Defense",
        "require_oos_score_valid": True,
    }
    result = run_adapter(adapter_cfg, sector_output_root, asof)
    rows = result.rows
    errors: list[str] = []
    if result.source_asof_date != asof:
        errors.append(f"source_asof_date mismatch: expected={asof} actual={result.source_asof_date}")
    if len(rows) != expected_rows:
        errors.append(f"row count mismatch: expected active={expected_rows} actual={len(rows)}")
    investable = sum(1 for row in rows if row.investable_eligible)
    research = sum(1 for row in rows if row.calibration_research_eligible)
    if investable:
        errors.append(f"shadow adapter produced investable rows: {investable}")
    if research:
        errors.append(f"shadow adapter produced research-eligible rows: {research}")
    bad_oos = [row.ticker for row in rows if row.oos_score_valid_flag != 0]
    if bad_oos:
        errors.append(f"shadow adapter produced OOS-valid rows: {bad_oos[:10]}")
    bad_roles = [row.ticker for row in rows if row.stage1_sample_role != "excluded" or row.calibration_sample_role != "excluded"]
    if bad_roles:
        errors.append(f"shadow adapter produced non-excluded sample roles: {bad_roles[:10]}")
    bad_scores = [row.ticker for row in rows if row.native_score < 0.0 or row.native_score > 100.0]
    if bad_scores:
        errors.append(f"native scores outside 0..100: {bad_scores[:10]}")
    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    print(
        f"PASS: portfolio tech_family shadow adapter rows={len(rows)} "
        f"source_asof={result.source_asof_date} investable=0 research=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

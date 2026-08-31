#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.defense.research_artifacts import load_production_lock, lock_mode_for_asof  # noqa: E402
from portfolio_layer.scores.adapters import run_adapter  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
MODEL_FAMILY = "defense"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate defense rank-table ingestion through the portfolio tech_family adapter "
        "(shadow, pre_lock and production calibration modes)."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--sector-output-root", type=Path, default=PROJECT_ROOT / "output")
    return parser.parse_args()


def parse_asof(raw: str) -> str:
    return datetime.strptime(raw.strip(), "%Y-%m-%d").date().isoformat()


def expected_defense_count(db_path: Path, *, asof: str, membership_mode: str) -> int:
    with sqlite3.connect(db_path) as conn:
        if membership_mode == "pit":
            return int(
                conn.execute(
                    """
                    SELECT COUNT(DISTINCT m.ticker)
                    FROM dim_universe_membership m
                    JOIN dim_industrials_taxonomy t ON t.company_id = m.company_id AND t.model_family = m.model_family
                    WHERE m.model_family = 'defense'
                      AND m.point_in_time_flag = 1
                      AND m.start_date <= ?
                      AND COALESCE(m.end_date, '9999-12-31') >= ?
                    """,
                    (asof, asof),
                ).fetchone()[0]
                or 0
            )
        return int(
            conn.execute(
                """
                SELECT COUNT(DISTINCT m.ticker)
                FROM dim_universe_membership m
                JOIN dim_company c ON c.company_id = m.company_id
                JOIN dim_industrials_taxonomy t
                  ON t.company_id = m.company_id AND t.model_family = m.model_family
                WHERE m.model_family = 'defense'
                  AND m.membership_basis = 'current_source_of_truth'
                  AND m.is_current_member = 1
                  AND c.is_active = 1
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
    manifest_path = snapshot_root / asof / "defense_final_rank_table_manifest.json"
    membership_mode = "current"
    if manifest_path.exists():
        try:
            membership_mode = str(
                json.loads(manifest_path.read_text(encoding="utf-8")).get("membership_mode") or "current"
            )
        except json.JSONDecodeError:
            membership_mode = "current"
    lock = load_production_lock(config, base_dir=base_dir)
    mode = lock_mode_for_asof(lock, asof)
    expected_rows = expected_defense_count(db_path, asof=asof, membership_mode=membership_mode)
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
        errors.append(
            f"row count mismatch: expected {membership_mode}={expected_rows} actual={len(rows)}"
        )
    investable = sum(1 for row in rows if row.investable_eligible)
    research = sum(1 for row in rows if row.calibration_research_eligible)
    oos_valid = sum(1 for row in rows if row.oos_score_valid_flag == 1)
    if mode == "production":
        allowed_roles = {"strict_oos", "excluded"}
        if not oos_valid:
            errors.append("production adapter produced zero OOS-valid rows")
        if not investable:
            errors.append("production adapter produced zero investable rows")
    else:
        allowed_roles = {"pre_lock_research", "excluded"} if mode == "pre_lock" else {"excluded"}
        if investable:
            errors.append(f"{mode} adapter produced investable rows: {investable}")
        if oos_valid:
            errors.append(f"{mode} adapter produced OOS-valid rows: {oos_valid}")
        if mode == "shadow" and research:
            errors.append(f"shadow adapter produced research-eligible rows: {research}")
        if mode == "pre_lock" and membership_mode == "current" and research:
            errors.append(
                f"pre_lock current-universe replay produced research-eligible rows "
                f"(not survivorship corrected): {research}"
            )
        if mode == "pre_lock" and membership_mode == "pit" and not research:
            errors.append("pre_lock PIT snapshot produced zero research-eligible rows")
    bad_roles = [
        row.ticker
        for row in rows
        if row.stage1_sample_role not in allowed_roles or row.calibration_sample_role not in allowed_roles
    ]
    if bad_roles:
        errors.append(f"{mode} adapter produced sample roles outside {sorted(allowed_roles)}: {bad_roles[:10]}")
    bad_scores = [row.ticker for row in rows if row.native_score < 0.0 or row.native_score > 100.0]
    if bad_scores:
        errors.append(f"native scores outside 0..100: {bad_scores[:10]}")
    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    print(
        f"PASS: portfolio tech_family adapter mode={mode} membership={membership_mode} rows={len(rows)} "
        f"source_asof={result.source_asof_date} investable={investable} research={research} oos_valid={oos_valid}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

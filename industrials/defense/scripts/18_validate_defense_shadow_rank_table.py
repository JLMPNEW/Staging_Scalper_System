#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
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
from industrials.core.db import init_db  # noqa: E402
from industrials.core.rank_table_contracts import defense_final_rank_header  # noqa: E402
from industrials.defense.research_artifacts import load_production_lock, lock_mode_for_asof  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
MODEL_FAMILY = "defense"
PANEL_SOURCE_CURRENT_UNIVERSE_REPLAY = "dashboard_rank_snapshot_current_universe_replay"
PANEL_SOURCE_SURVIVORSHIP_CORRECTED = "survivorship_corrected_pit_membership_score_recompute"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the shadow defense final rank table.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--rank-table", type=Path, default=None)
    return parser.parse_args()


def parse_asof(raw: str) -> str:
    return datetime.strptime(raw.strip(), "%Y-%m-%d").date().isoformat()


def expected_header() -> list[str]:
    return defense_final_rank_header(PROJECT_ROOT)


def as_float(raw: object) -> float | None:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def expected_row_count(conn: sqlite3.Connection, *, asof: str, membership_mode: str) -> int:
    if membership_mode == "pit":
        return int(
            conn.execute(
                """
                SELECT COUNT(DISTINCT m.ticker)
                FROM dim_universe_membership m
                JOIN dim_industrials_taxonomy t ON t.company_id = m.company_id AND t.model_family = m.model_family
                WHERE m.model_family = ?
                  AND m.point_in_time_flag = 1
                  AND m.start_date <= ?
                  AND COALESCE(m.end_date, '9999-12-31') >= ?
                """,
                (MODEL_FAMILY, asof, asof),
            ).fetchone()[0]
            or 0
        )
    return int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT c.ticker)
            FROM dim_company c
            JOIN dim_industrials_taxonomy t ON t.company_id = c.company_id
            WHERE c.is_active = 1 AND t.model_family = ?
            """,
            (MODEL_FAMILY,),
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
    rank_table = (
        args.rank_table.expanduser().resolve()
        if args.rank_table
        else snapshot_root / asof / "defense_final_rank_table.csv"
    )
    manifest_path = rank_table.with_name("defense_final_rank_table_manifest.json")
    errors: list[str] = []

    if not rank_table.exists():
        raise FileNotFoundError(rank_table)
    with rank_table.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        got_header = list(reader.fieldnames or [])
        rows = list(reader)
    exp_header = expected_header()
    if got_header != exp_header:
        errors.append("rank table header does not match semiconductor contract with defense demand-pillar rename")

    manifest: dict[str, object] = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    membership_mode = str(manifest.get("membership_mode") or "current")
    if membership_mode not in {"current", "pit"}:
        errors.append(f"manifest membership_mode invalid: {membership_mode!r}")
        membership_mode = "current"

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        init_db(conn)
        expected_count = expected_row_count(conn, asof=asof, membership_mode=membership_mode)
    if len(rows) != expected_count:
        errors.append(f"row count mismatch: expected {membership_mode}={expected_count} actual={len(rows)}")
    tickers = [str(row.get("ticker") or "").strip().upper() for row in rows]
    if len(set(tickers)) != len(tickers):
        errors.append("duplicate tickers found")
    ranks = sorted(int(float(row.get("final_rank") or 0)) for row in rows)
    if ranks != list(range(1, len(rows) + 1)):
        errors.append("final_rank values are not a contiguous 1..N sequence")
    file_ranks = [int(float(row.get("final_rank") or 0)) for row in rows]
    if file_ranks != sorted(file_ranks):
        errors.append("rank table rows are not physically sorted by final_rank ascending")
    bad_scores = [
        row.get("ticker", "")
        for row in rows
        if (score := as_float(row.get("final_score"))) is None or score < 0.0 or score > 100.0
    ]
    if bad_scores:
        errors.append(f"final_score outside 0..100 or nonnumeric: {bad_scores[:10]}")
    lock = load_production_lock(config, base_dir=base_dir)
    mode = lock_mode_for_asof(lock, asof)
    if mode == "production":
        bad_role = [
            row.get("ticker", "")
            for row in rows
            if str(row.get("calibration_sample_role") or "") not in {"strict_oos", "excluded"}
        ]
        if bad_role:
            errors.append(f"production calibration_sample_role not strict_oos/excluded: {bad_role[:10]}")
        bad_gate = [
            row.get("ticker", "")
            for row in rows
            if str(row.get("portfolio_candidate_gate") or "") == "1"
            and (
                str(row.get("oos_score_valid_flag") or "") != "1"
                or str(row.get("calibration_eligible_flag") or "") != "1"
            )
        ]
        if bad_gate:
            errors.append(f"candidate gate open without oos/calibration eligibility: {bad_gate[:10]}")
        bad_lock_date = [
            row.get("ticker", "")
            for row in rows
            if lock is not None and str(row.get("calibration_lock_date") or "") != lock["lock_date"]
        ]
        if bad_lock_date:
            errors.append(f"production rows missing sealed calibration_lock_date: {bad_lock_date[:10]}")
    else:
        bad_shadow = [
            row.get("ticker", "")
            for row in rows
            if str(row.get("oos_score_valid_flag") or "") != "0"
            or str(row.get("portfolio_candidate_gate") or "") != "0"
            or str(row.get("calibration_eligible_flag") or "") != "0"
        ]
        if bad_shadow:
            errors.append(f"pre-production gates not disabled: {bad_shadow[:10]}")
        allowed_roles = {"pre_lock_research", "excluded"} if mode == "pre_lock" else {"excluded"}
        bad_role = [
            row.get("ticker", "")
            for row in rows
            if str(row.get("calibration_sample_role") or "") not in allowed_roles
        ]
        if bad_role:
            errors.append(f"{mode} calibration_sample_role outside {sorted(allowed_roles)}: {bad_role[:10]}")
        if mode == "pre_lock":
            bad_lock_date = [
                row.get("ticker", "")
                for row in rows
                if lock is not None and str(row.get("calibration_lock_date") or "") != lock["lock_date"]
            ]
            if bad_lock_date:
                errors.append(f"pre_lock rows missing sealed calibration_lock_date: {bad_lock_date[:10]}")
            bad_research_guard = [
                row.get("ticker", "")
                for row in rows
                if str(row.get("research_calibration_input_eligible_flag") or "") == "1"
                and str(row.get("survivorship_corrected_panel_flag") or "") != "1"
            ]
            if bad_research_guard:
                errors.append(
                    f"research-eligible pre_lock rows not survivorship corrected: {bad_research_guard[:10]}"
                )
        else:
            bad_lock_date = [
                row.get("ticker", "")
                for row in rows
                if str(row.get("calibration_lock_date") or "").strip()
            ]
            if bad_lock_date:
                errors.append(f"shadow rows carry a calibration_lock_date: {bad_lock_date[:10]}")
    bad_research_alias = [
        row.get("ticker", "")
        for row in rows
        if str(row.get("research_calibration_eligible_flag") or "")
        != str(row.get("research_calibration_input_eligible_flag") or "")
    ]
    if bad_research_alias:
        errors.append(f"research_calibration_eligible_flag does not mirror input flag: {bad_research_alias[:10]}")
    bad_oos_date = [
        row.get("ticker", "")
        for row in rows
        if str(row.get("oos_score_valid_flag") or "") == "1" and not str(row.get("oos_score_asof_date") or "").strip()
    ]
    if bad_oos_date:
        errors.append(f"OOS-valid rows missing oos_score_asof_date: {bad_oos_date[:10]}")
    expected_stage11_source = (
        PANEL_SOURCE_SURVIVORSHIP_CORRECTED if membership_mode == "pit" else PANEL_SOURCE_CURRENT_UNIVERSE_REPLAY
    )
    expected_survivorship_flag = "1" if membership_mode == "pit" else "0"
    bad_stage11_source = [
        row.get("ticker", "")
        for row in rows
        if str(row.get("stage11_calibration_panel_source") or "") != expected_stage11_source
    ]
    if bad_stage11_source:
        errors.append(f"stage11_calibration_panel_source not {expected_stage11_source}: {bad_stage11_source[:10]}")
    bad_survivorship_flag = [
        row.get("ticker", "")
        for row in rows
        if str(row.get("survivorship_corrected_panel_flag") or "") != expected_survivorship_flag
    ]
    if bad_survivorship_flag:
        errors.append(
            f"survivorship_corrected_panel_flag not {expected_survivorship_flag}: {bad_survivorship_flag[:10]}"
        )
    blank_market_cap = [row.get("ticker", "") for row in rows if not str(row.get("market_cap") or "").strip()]
    blank_market_cap_without_reason = [
        row.get("ticker", "")
        for row in rows
        if not str(row.get("market_cap") or "").strip()
        and "market_cap_unavailable" not in str(row.get("liquidity_capacity_reason") or "")
    ]
    if blank_market_cap and membership_mode == "current":
        errors.append(f"market_cap blank in published rank table: {blank_market_cap[:10]}")
    elif blank_market_cap_without_reason:
        errors.append(f"market_cap blank without clear liquidity_capacity_reason: {blank_market_cap_without_reason[:10]}")
    blank_adv60 = [row.get("ticker", "") for row in rows if not str(row.get("avg_dollar_volume_60d") or "").strip()]
    blank_adv60_without_reason = [
        row.get("ticker", "")
        for row in rows
        if not str(row.get("avg_dollar_volume_60d") or "").strip()
        and "avg_dollar_volume_60d_unavailable" not in str(row.get("liquidity_capacity_reason") or "")
    ]
    if blank_adv60 and membership_mode == "current":
        errors.append(f"avg_dollar_volume_60d blank in published rank table: {blank_adv60[:10]}")
    elif blank_adv60_without_reason:
        errors.append(f"avg_dollar_volume_60d blank without clear liquidity_capacity_reason: {blank_adv60_without_reason[:10]}")
    missing_capacity_reason = [
        row.get("ticker", "")
        for row in rows
        if (
            (not str(row.get("market_cap") or "").strip() and "market_cap_unavailable" not in str(row.get("liquidity_capacity_reason") or ""))
            or (
                not str(row.get("avg_dollar_volume_60d") or "").strip()
                and "avg_dollar_volume_60d_unavailable" not in str(row.get("liquidity_capacity_reason") or "")
            )
        )
    ]
    if missing_capacity_reason:
        errors.append(f"blank capacity fields missing clear liquidity_capacity_reason: {missing_capacity_reason[:10]}")
    bad_neutral = [
        row.get("ticker", "")
        for row in rows
        if str(row.get("sector_cycle_status") or "") != "neutralized_not_loaded"
        or str(row.get("defense_budget_backlog_status") or "") != "neutralized_not_loaded"
        or as_float(row.get("sector_cycle_score")) != 50.0
        or as_float(row.get("defense_budget_backlog_score")) != 50.0
    ]
    if bad_neutral:
        errors.append(f"neutral demand/sector pillars not pinned: {bad_neutral[:10]}")
    if not manifest_path.exists():
        errors.append("manifest file missing")
    else:
        digest = hashlib.sha256(rank_table.read_bytes()).hexdigest()
        if manifest.get("sha256") != digest:
            errors.append("manifest sha256 does not match rank table")
        if manifest.get("asof_date") != asof:
            errors.append("manifest asof_date mismatch")
        try:
            manifest_rows = int(str(manifest.get("rows") or "-1"))
        except ValueError:
            manifest_rows = -1
        if manifest_rows != len(rows):
            errors.append("manifest row count mismatch")

    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    print(f"PASS: {rank_table} rows={len(rows)} asof={asof}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

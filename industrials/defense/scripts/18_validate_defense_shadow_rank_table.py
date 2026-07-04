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


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


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

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        init_db(conn)
        active_count = int(
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
    if len(rows) != active_count:
        errors.append(f"row count mismatch: expected active={active_count} actual={len(rows)}")
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
    bad_shadow = [
        row.get("ticker", "")
        for row in rows
        if str(row.get("oos_score_valid_flag") or "") != "0"
        or str(row.get("portfolio_candidate_gate") or "") != "0"
        or str(row.get("calibration_eligible_flag") or "") != "0"
    ]
    if bad_shadow:
        errors.append(f"shadow-only gates not disabled: {bad_shadow[:10]}")
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
    bad_stage11_source = [
        row.get("ticker", "")
        for row in rows
        if str(row.get("stage11_calibration_panel_source") or "")
        != "dashboard_rank_snapshot_current_universe_replay"
    ]
    if bad_stage11_source:
        errors.append(f"stage11_calibration_panel_source not explicit current-universe replay: {bad_stage11_source[:10]}")
    blank_market_cap = [row.get("ticker", "") for row in rows if not str(row.get("market_cap") or "").strip()]
    if blank_market_cap:
        errors.append(f"market_cap blank in published rank table: {blank_market_cap[:10]}")
    blank_adv60 = [row.get("ticker", "") for row in rows if not str(row.get("avg_dollar_volume_60d") or "").strip()]
    if blank_adv60:
        errors.append(f"avg_dollar_volume_60d blank in published rank table: {blank_adv60[:10]}")
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
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        digest = hashlib.sha256(rank_table.read_bytes()).hexdigest()
        if manifest.get("sha256") != digest:
            errors.append("manifest sha256 does not match rank table")
        if manifest.get("asof_date") != asof:
            errors.append("manifest asof_date mismatch")
        if int(manifest.get("rows") or -1) != len(rows):
            errors.append("manifest row count mismatch")

    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    print(f"PASS: {rank_table} rows={len(rows)} asof={asof}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

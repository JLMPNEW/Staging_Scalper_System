#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sqlite3
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from macro_raw_config import cfg_get, configure_pipeline_logging, connect_sqlite, load_macro_raw_config, resolve_path
from macro_serving_common import resolve_serving_db_path

logger = logging.getLogger(__name__)

DEFAULT_OUT_DIR = "MacroLayer/out/foreign_sleeve_budget_checks"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Stage 12C foreign sleeve budget acceptance diagnostics.")
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--serving-db-path", type=Path, default=None, help="Optional serving SQLite path override.")
    parser.add_argument("--out-dir", type=Path, default=None, help="Output directory for diagnostic CSVs.")
    return parser.parse_args()


def _resolve_out_dir(config_path: Path, raw_out_dir: Path | None) -> Path:
    if raw_out_dir is not None:
        return raw_out_dir.expanduser().resolve()
    out_dir = resolve_path(config_path, DEFAULT_OUT_DIR)
    if out_dir is None:
        raise ValueError("Unable to resolve Stage 12C diagnostic output directory.")
    return out_dir


def _thresholds(cfg: dict) -> dict[str, float]:
    budget_cfg = dict(cfg_get(cfg, "foreign_sleeve_budget_layer", "budget", default={}) or {})
    weights_cfg = dict(cfg_get(cfg, "foreign_sleeve_budget_layer", "weights", default={}) or {})
    raw = dict(cfg_get(cfg, "foreign_sleeve_budget_layer", "acceptance", default={}) or {})
    defaults = {
        "max_budget": float(budget_cfg.get("max_budget", 0.20)),
        "min_active_selected_candidates": 1.0,
        "max_single_etf_sleeve_weight": float(weights_cfg.get("max_single_etf_sleeve_weight", 0.60)),
        "budget_sum_tolerance": 1e-6,
        "active_budget_min": float(budget_cfg.get("min_budget", 0.05)),
    }
    defaults.update({str(k): float(v) for k, v in raw.items()})
    return defaults


def _read_sql(conn: sqlite3.Connection, query: str, params: Iterable[object] = ()) -> pd.DataFrame:
    return pd.read_sql_query(query, conn, params=list(params))


def _table_summary(conn: sqlite3.Connection) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for table_name in ("foreign_sleeve_budget_daily", "foreign_sleeve_candidate_daily"):
        row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS row_count,
                MIN(as_of_date) AS min_date,
                MAX(as_of_date) AS max_date,
                COUNT(DISTINCT as_of_date) AS date_count
            FROM {table_name}
            """
        ).fetchone()
        row_count = int(row["row_count"] or 0)
        expected_nonempty = table_name == "foreign_sleeve_budget_daily"
        rows.append(
            {
                "check_name": f"{table_name}_{'nonempty' if expected_nonempty else 'present_optional'}",
                "value": row_count,
                "threshold": ">0" if expected_nonempty else ">=0",
                "passed": int(row_count > 0 if expected_nonempty else row_count >= 0),
                "details": f"min={row['min_date']} max={row['max_date']} dates={row['date_count']}",
            }
        )
    return pd.DataFrame(rows)


def _latest_frames(conn: sqlite3.Connection) -> tuple[str, pd.DataFrame, pd.DataFrame]:
    row = conn.execute("SELECT MAX(as_of_date) AS latest_date FROM foreign_sleeve_budget_daily").fetchone()
    latest_date = str(row["latest_date"] or "")
    if not latest_date:
        raise ValueError("foreign_sleeve_budget_daily is empty.")
    budget = _read_sql(
        conn,
        """
        SELECT *
        FROM foreign_sleeve_budget_daily
        WHERE as_of_date = ?
        """,
        [latest_date],
    )
    candidates = _read_sql(
        conn,
        """
        SELECT *
        FROM foreign_sleeve_candidate_daily
        WHERE as_of_date = ?
        ORDER BY selected_flag DESC, candidate_rank
        """,
        [latest_date],
    )
    return latest_date, budget, candidates


def _latest_checks(
    budget: pd.DataFrame,
    candidates: pd.DataFrame,
    latest_date: str,
    thresholds: dict[str, float],
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    if budget.empty:
        return pd.DataFrame(
            [
                {
                    "check_name": "latest_budget_row_present",
                    "value": 0,
                    "threshold": "1",
                    "passed": 0,
                    "details": latest_date,
                }
            ]
        )
    row = budget.iloc[0]
    active = int(row.get("active_flag", 0) or 0) == 1
    foreign_budget = float(row.get("foreign_budget", 0.0) or 0.0)
    selected = candidates.loc[pd.to_numeric(candidates["selected_flag"], errors="coerce").fillna(0).astype(int).eq(1)]
    selected_count = int(len(selected))
    sleeve_sum = float(pd.to_numeric(selected["sleeve_weight"], errors="coerce").fillna(0.0).sum()) if selected_count else 0.0
    portfolio_sum = (
        float(pd.to_numeric(selected["portfolio_weight_at_budget"], errors="coerce").fillna(0.0).sum())
        if selected_count
        else 0.0
    )
    max_single = float(pd.to_numeric(selected["sleeve_weight"], errors="coerce").max()) if selected_count else 0.0
    candidate_alpha = pd.to_numeric(candidates["foreign_fused_alpha"], errors="coerce") if not candidates.empty else pd.Series(dtype="float64")
    duplicate_count = int(
        candidates.groupby(["as_of_date", "ticker"]).size().reset_index(name="n").query("n > 1").shape[0]
        if not candidates.empty
        else 0
    )
    checks = [
        ("latest_budget_row_present", int(len(budget)), "1", len(budget) == 1),
        (
            "latest_budget_bounded",
            foreign_budget,
            f"[0, {thresholds['max_budget']}]",
            0.0 <= foreign_budget <= thresholds["max_budget"] + 1e-12,
        ),
        (
            "latest_active_selected_count",
            selected_count,
            f">={thresholds['min_active_selected_candidates']} when active",
            (not active) or selected_count >= thresholds["min_active_selected_candidates"],
        ),
        (
            "latest_active_budget_min",
            foreign_budget,
            f">={thresholds['active_budget_min']} when active",
            (not active) or foreign_budget >= thresholds["active_budget_min"] - 1e-12,
        ),
        (
            "latest_inactive_budget_zero",
            foreign_budget,
            "0 when inactive",
            active or abs(foreign_budget) <= thresholds["budget_sum_tolerance"],
        ),
        (
            "latest_selected_sleeve_weight_sum",
            sleeve_sum,
            "1 when active, 0 when inactive",
            (
                abs(sleeve_sum - 1.0) <= thresholds["budget_sum_tolerance"]
                if active
                else abs(sleeve_sum) <= thresholds["budget_sum_tolerance"]
            ),
        ),
        (
            "latest_selected_portfolio_weight_sum",
            portfolio_sum,
            "equals foreign_budget",
            abs(portfolio_sum - foreign_budget) <= thresholds["budget_sum_tolerance"],
        ),
        (
            "latest_max_single_etf_sleeve_weight",
            max_single,
            f"<={thresholds['max_single_etf_sleeve_weight']}",
            max_single <= thresholds["max_single_etf_sleeve_weight"] + 1e-12,
        ),
        (
            "latest_candidate_scores_finite",
            int(candidate_alpha.notna().sum()),
            "all latest candidates finite when present",
            candidates.empty or candidate_alpha.notna().all(),
        ),
        ("latest_no_duplicate_candidate_keys", duplicate_count, "0", duplicate_count == 0),
    ]
    for name, value, threshold, passed in checks:
        rows.append(
            {
                "check_name": name,
                "value": value,
                "threshold": threshold,
                "passed": int(bool(passed)),
                "details": latest_date,
            }
        )
    return pd.DataFrame(rows)


def _history_checks(conn: sqlite3.Connection, thresholds: dict[str, float]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    duplicates = conn.execute(
        """
        SELECT COUNT(*) AS duplicate_count
        FROM (
            SELECT as_of_date, ticker, COUNT(*) AS row_count
            FROM foreign_sleeve_candidate_daily
            GROUP BY as_of_date, ticker
            HAVING COUNT(*) > 1
        )
        """
    ).fetchone()
    bad_budgets = conn.execute(
        """
        SELECT COUNT(*) AS bad_count
        FROM foreign_sleeve_budget_daily
        WHERE foreign_budget IS NULL
           OR foreign_budget < -0.000000001
           OR foreign_budget > ? + 0.000000001
           OR (active_flag = 0 AND ABS(foreign_budget) > ?)
           OR (active_flag = 1 AND foreign_budget < ? - 0.000000001)
        """,
        [thresholds["max_budget"], thresholds["budget_sum_tolerance"], thresholds["active_budget_min"]],
    ).fetchone()
    bad_active_sums = conn.execute(
        """
        SELECT COUNT(*) AS bad_count
        FROM (
            SELECT
                b.as_of_date,
                b.active_flag,
                b.foreign_budget,
                COALESCE(SUM(c.sleeve_weight), 0.0) AS sleeve_sum,
                COALESCE(SUM(c.portfolio_weight_at_budget), 0.0) AS portfolio_sum,
                COALESCE(MAX(c.sleeve_weight), 0.0) AS max_single
            FROM foreign_sleeve_budget_daily b
            LEFT JOIN foreign_sleeve_candidate_daily c
              ON c.as_of_date = b.as_of_date
             AND c.selected_flag = 1
            GROUP BY b.as_of_date, b.active_flag, b.foreign_budget
        )
        WHERE (active_flag = 1 AND ABS(sleeve_sum - 1.0) > ?)
           OR ABS(portfolio_sum - foreign_budget) > ?
           OR max_single > ? + 0.000000001
        """,
        [
            thresholds["budget_sum_tolerance"],
            thresholds["budget_sum_tolerance"],
            thresholds["max_single_etf_sleeve_weight"],
        ],
    ).fetchone()
    rows.extend(
        [
            {
                "check_name": "history_no_candidate_duplicates",
                "value": int(duplicates["duplicate_count"] or 0),
                "threshold": "0",
                "passed": int(int(duplicates["duplicate_count"] or 0) == 0),
                "details": "",
            },
            {
                "check_name": "history_budgets_valid",
                "value": int(bad_budgets["bad_count"] or 0),
                "threshold": "0",
                "passed": int(int(bad_budgets["bad_count"] or 0) == 0),
                "details": "",
            },
            {
                "check_name": "history_candidate_weight_sums_valid",
                "value": int(bad_active_sums["bad_count"] or 0),
                "threshold": "0",
                "passed": int(int(bad_active_sums["bad_count"] or 0) == 0),
                "details": "",
            },
        ]
    )
    return pd.DataFrame(rows)


def _export_checks(budget: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    if budget.empty:
        return pd.DataFrame(
            [
                {
                    "check_name": "latest_candidate_export_present",
                    "value": 0,
                    "threshold": "budget row",
                    "passed": 0,
                    "details": "",
                }
            ]
        )
    candidate_path = Path(str(budget.iloc[0].get("output_csv", "") or ""))
    checks = [
        (
            "latest_candidate_output_csv_exists_and_schema",
            candidate_path,
            {"as_of_date", "ticker", "foreign_fused_alpha", "sleeve_weight", "portfolio_weight_at_budget"},
        )
    ]
    for name, path, required_cols in checks:
        exists = path.exists()
        cols_ok = False
        details = str(path)
        if exists:
            try:
                frame = pd.read_csv(path, nrows=5)
                cols_ok = required_cols.issubset(set(frame.columns))
                details = f"{path} columns_ok={cols_ok}"
            except Exception as exc:
                details = f"{path} read_error={exc}"
        rows.append(
            {
                "check_name": name,
                "value": int(exists and cols_ok),
                "threshold": f"exists with columns {sorted(required_cols)}",
                "passed": int(exists and cols_ok),
                "details": details,
            }
        )
    return pd.DataFrame(rows)


def _write_atomic_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    frame.to_csv(tmp_path, index=False)
    tmp_path.replace(path)


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, cfg = load_macro_raw_config(args.config)
    serving_db_path = resolve_serving_db_path(cfg, config_path, override=args.serving_db_path)
    out_dir = _resolve_out_dir(config_path, args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    thresholds = _thresholds(cfg)

    conn = connect_sqlite(serving_db_path, row_factory=sqlite3.Row)
    try:
        latest_date, latest_budget, latest_candidates = _latest_frames(conn)
        summary = pd.concat(
            [
                _table_summary(conn),
                _latest_checks(latest_budget, latest_candidates, latest_date, thresholds),
                _history_checks(conn, thresholds),
                _export_checks(latest_budget),
            ],
            ignore_index=True,
        )
        _write_atomic_csv(out_dir / "stage12c_foreign_sleeve_budget_acceptance_summary.csv", summary)
        _write_atomic_csv(out_dir / "stage12c_foreign_sleeve_budget_latest.csv", latest_budget)
        _write_atomic_csv(out_dir / "stage12c_foreign_sleeve_candidates_latest.csv", latest_candidates)
    finally:
        conn.close()

    passed = bool(summary["passed"].astype(int).all())
    logger.info("Stage 12C foreign sleeve budget acceptance: %s", "PASS" if passed else "FAIL")
    print(f"STAGE_12C_EXIT_GATE={'PASS' if passed else 'FAIL'}")
    if not passed:
        failed = summary.loc[summary["passed"].astype(int) == 0]
        print(failed.to_string(index=False))
        raise SystemExit(1)


if __name__ == "__main__":
    main()

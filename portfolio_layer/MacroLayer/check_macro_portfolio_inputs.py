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

DEFAULT_OUT_DIR = "MacroLayer/out/portfolio_input_checks"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Stage 12A portfolio-input acceptance diagnostics.")
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--serving-db-path", type=Path, default=None, help="Optional serving SQLite path override.")
    parser.add_argument("--out-dir", type=Path, default=None, help="Output directory for diagnostic CSVs.")
    return parser.parse_args()


def _resolve_out_dir(config_path: Path, raw_out_dir: Path | None) -> Path:
    if raw_out_dir is not None:
        return raw_out_dir.expanduser().resolve()
    out_dir = resolve_path(config_path, DEFAULT_OUT_DIR)
    if out_dir is None:
        raise ValueError("Unable to resolve Stage 12A diagnostic output directory.")
    return out_dir


def _thresholds(cfg: dict) -> dict[str, object]:
    raw = dict(cfg_get(cfg, "portfolio_input_layer", "acceptance", default={}) or {})
    defaults: dict[str, object] = {
        "min_latest_stock_rows": 1000.0,
        "min_latest_stock_eligible_rows": 100.0,
        "max_latest_null_score_share": 0.0,
        "min_latest_selection_score_std": 0.05,
        "min_latest_weight_score_std": 0.05,
        "max_foreign_required": False,
        "stock_eligible_state": str(cfg_get(cfg, "portfolio_input_layer", "stock", "state_when_eligible", default="Eligible")),
    }
    defaults.update(raw)
    return defaults


def _read_sql(conn: sqlite3.Connection, query: str, params: Iterable[object] = ()) -> pd.DataFrame:
    return pd.read_sql_query(query, conn, params=list(params))


def _table_summary(conn: sqlite3.Connection) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for table_name in ("portfolio_inputs_daily", "portfolio_allocation_summary"):
        summary = conn.execute(
            f"""
            SELECT
                COUNT(*) AS row_count,
                MIN(as_of_date) AS min_date,
                MAX(as_of_date) AS max_date,
                COUNT(DISTINCT as_of_date) AS date_count
            FROM {table_name}
            """
        ).fetchone()
        row_count = int(summary["row_count"] or 0)
        rows.append(
            {
                "check_name": f"{table_name}_nonempty",
                "value": row_count,
                "threshold": ">0",
                "passed": int(row_count > 0),
                "details": f"min={summary['min_date']} max={summary['max_date']} dates={summary['date_count']}",
            }
        )
    return pd.DataFrame(rows)


def _latest_frame(conn: sqlite3.Connection) -> tuple[str, pd.DataFrame, pd.DataFrame]:
    row = conn.execute("SELECT MAX(as_of_date) AS latest_date FROM portfolio_inputs_daily").fetchone()
    latest_date = str(row["latest_date"] or "")
    if not latest_date:
        raise ValueError("portfolio_inputs_daily is empty.")
    inputs = _read_sql(
        conn,
        """
        SELECT *
        FROM portfolio_inputs_daily
        WHERE as_of_date = ?
        ORDER BY asset_type, final_score DESC
        """,
        [latest_date],
    )
    summary = _read_sql(
        conn,
        """
        SELECT *
        FROM portfolio_allocation_summary
        WHERE as_of_date = ?
        """,
        [latest_date],
    )
    return latest_date, inputs, summary


def _latest_checks(inputs: pd.DataFrame, summary: pd.DataFrame, latest_date: str, thresholds: dict[str, object]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    stock = inputs.loc[inputs["asset_type"].eq("US_STOCK")].copy()
    foreign = inputs.loc[inputs["asset_type"].eq("FOREIGN_ETF")].copy()
    stock_count = int(len(stock))
    stock_eligible = int(stock["state"].astype(str).eq(str(thresholds["stock_eligible_state"])).sum())
    final_score = pd.to_numeric(stock["final_score"], errors="coerce")
    selection = pd.to_numeric(stock["selection_score"], errors="coerce")
    weight = pd.to_numeric(stock["weight_score"], errors="coerce")
    null_share = float((final_score.isna() | selection.isna() | weight.isna()).mean()) if stock_count else 1.0
    selection_std = float(selection.std(ddof=1)) if selection.notna().sum() > 1 else np.nan
    weight_std = float(weight.std(ddof=1)) if weight.notna().sum() > 1 else np.nan
    score_pct = pd.to_numeric(inputs["score_pct"], errors="coerce")
    duplicate_count = int(
        inputs.groupby(["as_of_date", "ticker", "asset_type"]).size().reset_index(name="n").query("n > 1").shape[0]
    )
    checks = [
        (
            "latest_stock_row_count",
            stock_count,
            f">={thresholds['min_latest_stock_rows']}",
            stock_count >= float(thresholds["min_latest_stock_rows"]),
        ),
        (
            "latest_stock_eligible_count",
            stock_eligible,
            f">={thresholds['min_latest_stock_eligible_rows']}",
            stock_eligible >= float(thresholds["min_latest_stock_eligible_rows"]),
        ),
        (
            "latest_null_score_share",
            null_share,
            f"<={thresholds['max_latest_null_score_share']}",
            null_share <= float(thresholds["max_latest_null_score_share"]),
        ),
        (
            "latest_selection_score_std",
            selection_std,
            f">={thresholds['min_latest_selection_score_std']}",
            np.isfinite(selection_std) and selection_std >= float(thresholds["min_latest_selection_score_std"]),
        ),
        (
            "latest_weight_score_std",
            weight_std,
            f">={thresholds['min_latest_weight_score_std']}",
            np.isfinite(weight_std) and weight_std >= float(thresholds["min_latest_weight_score_std"]),
        ),
        (
            "latest_score_pct_bounded",
            f"{score_pct.min():.6f}..{score_pct.max():.6f}",
            "[0, 1]",
            score_pct.notna().all() and bool(((score_pct >= 0.0) & (score_pct <= 1.0)).all()),
        ),
        ("latest_no_duplicate_keys", duplicate_count, "0", duplicate_count == 0),
        (
            "latest_summary_row_present",
            int(len(summary)),
            "1",
            len(summary) == 1,
        ),
    ]
    foreign_required = bool(thresholds.get("max_foreign_required", False))
    foreign_scores = pd.to_numeric(foreign["foreign_fused_alpha"], errors="coerce") if not foreign.empty else pd.Series(dtype="float64")
    checks.append(
        (
            "latest_foreign_rows_optional",
            int(len(foreign)),
            ">0 only when required",
            len(foreign) > 0 or not foreign_required,
        )
    )
    if not foreign.empty:
        checks.append(
            (
                "latest_foreign_scores_finite",
                int(foreign_scores.notna().sum()),
                "all foreign rows finite",
                foreign_scores.notna().all(),
            )
        )
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


def _history_checks(conn: sqlite3.Connection) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    duplicates = conn.execute(
        """
        SELECT COUNT(*) AS duplicate_count
        FROM (
            SELECT as_of_date, ticker, asset_type, COUNT(*) AS row_count
            FROM portfolio_inputs_daily
            GROUP BY as_of_date, ticker, asset_type
            HAVING COUNT(*) > 1
        )
        """
    ).fetchone()
    bad_scores = conn.execute(
        """
        SELECT COUNT(*) AS bad_count
        FROM portfolio_inputs_daily
        WHERE final_score IS NULL
           OR score_pct IS NULL
           OR score_pct < 0.0
           OR score_pct > 1.0
           OR state IS NULL
           OR state = ''
        """
    ).fetchone()
    rows.extend(
        [
            {
                "check_name": "history_no_portfolio_input_duplicates",
                "value": int(duplicates["duplicate_count"] or 0),
                "threshold": "0",
                "passed": int(int(duplicates["duplicate_count"] or 0) == 0),
                "details": "",
            },
            {
                "check_name": "history_scores_valid",
                "value": int(bad_scores["bad_count"] or 0),
                "threshold": "0",
                "passed": int(int(bad_scores["bad_count"] or 0) == 0),
                "details": "",
            },
        ]
    )
    return pd.DataFrame(rows)


def _export_checks(summary: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    if summary.empty:
        return pd.DataFrame(
            [
                {
                    "check_name": "latest_export_paths_present",
                    "value": 0,
                    "threshold": "summary row",
                    "passed": 0,
                    "details": "",
                }
            ]
        )
    row = summary.iloc[0]
    required_stock_cols = {"Ticker", "FinalScore", "sector", "industry", "industry_aggregate", "Rating"}
    for col_name, required_cols in (
        ("stock_output_csv", required_stock_cols),
        ("foreign_output_csv", {"Ticker", "MarketName", "Score", "ScorePct", "State"}),
        ("combined_output_csv", {"ticker", "asset_type", "final_score", "state"}),
    ):
        path = Path(str(row.get(col_name, "") or ""))
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
                "check_name": f"latest_{col_name}_exists_and_schema",
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
        latest_date, latest_inputs, latest_summary = _latest_frame(conn)
        summary = pd.concat(
            [
                _table_summary(conn),
                _latest_checks(latest_inputs, latest_summary, latest_date, thresholds),
                _history_checks(conn),
                _export_checks(latest_summary),
            ],
            ignore_index=True,
        )
        _write_atomic_csv(out_dir / "stage12a_portfolio_input_acceptance_summary.csv", summary)
        _write_atomic_csv(out_dir / "stage12a_portfolio_inputs_latest.csv", latest_inputs)
    finally:
        conn.close()

    passed = bool(summary["passed"].astype(int).all())
    logger.info("Stage 12A portfolio input acceptance: %s", "PASS" if passed else "FAIL")
    print(f"STAGE_12A_EXIT_GATE={'PASS' if passed else 'FAIL'}")
    if not passed:
        failed = summary.loc[summary["passed"].astype(int) == 0]
        print(failed.to_string(index=False))
        raise SystemExit(1)


if __name__ == "__main__":
    main()

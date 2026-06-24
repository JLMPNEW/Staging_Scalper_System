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

DEFAULT_OUT_DIR = "MacroLayer/out/stock_macro_overlay_checks"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Stage 11 stock macro overlay acceptance diagnostics.")
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--serving-db-path", type=Path, default=None, help="Optional serving SQLite path override.")
    parser.add_argument("--out-dir", type=Path, default=None, help="Output directory for diagnostic CSVs.")
    return parser.parse_args()


def _resolve_out_dir(config_path: Path, raw_out_dir: Path | None) -> Path:
    if raw_out_dir is not None:
        return raw_out_dir.expanduser().resolve()
    out_dir = resolve_path(config_path, DEFAULT_OUT_DIR)
    if out_dir is None:
        raise ValueError("Unable to resolve Stage 11 diagnostic output directory.")
    return out_dir


def _thresholds(cfg: dict) -> dict[str, object]:
    raw = dict(cfg_get(cfg, "stock_macro_overlay", "acceptance", default={}) or {})
    defaults: dict[str, object] = {
        "min_latest_rows": 1000.0,
        "min_latest_eligible_rows": 100.0,
        "max_null_score_share": 0.0,
        "min_selection_score_std": 0.05,
        "min_weight_score_std": 0.05,
        "min_macro_fit_std": 0.05,
        "min_rank_correlation_shift": 0.01,
        "favored_selection_lift_min": 0.05,
        "adverse_selection_lift_max": -0.05,
        "require_favored_adverse_lift": True,
    }
    for k, v in raw.items():
        key = str(k)
        if key == "require_favored_adverse_lift":
            defaults[key] = str(v).strip().lower() not in {"0", "false", "no", "n", "off"}
        else:
            defaults[key] = float(v)
    return defaults


def _read_sql(conn: sqlite3.Connection, query: str, params: Iterable[object] = ()) -> pd.DataFrame:
    return pd.read_sql_query(query, conn, params=list(params))


def _table_summary(conn: sqlite3.Connection) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for table_name in ("stock_macro_fit_daily", "stock_selection_score_daily", "stock_weight_score_daily"):
        row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS row_count,
                COUNT(DISTINCT as_of_date || '|' || ticker) AS natural_key_count,
                MIN(as_of_date) AS min_date,
                MAX(as_of_date) AS max_date,
                COUNT(DISTINCT as_of_date) AS date_count
            FROM {table_name}
            """
        ).fetchone()
        row_count = int(row["row_count"] or 0)
        key_count = int(row["natural_key_count"] or 0)
        rows.append(
            {
                "check_name": f"{table_name}_nonempty_unique",
                "value": row_count,
                "threshold": ">0 and no duplicate natural keys",
                "passed": int(row_count > 0 and row_count == key_count),
                "details": f"min={row['min_date']} max={row['max_date']} dates={row['date_count']} keys={key_count}",
            }
        )
    return pd.DataFrame(rows)


def _latest_frame(conn: sqlite3.Connection) -> tuple[str, pd.DataFrame]:
    row = conn.execute("SELECT MAX(as_of_date) AS latest_date FROM stock_selection_score_daily").fetchone()
    latest_date = str(row["latest_date"] or "")
    if not latest_date:
        raise ValueError("stock_selection_score_daily is empty.")
    frame = _read_sql(
        conn,
        """
        SELECT
            f.as_of_date,
            f.ticker,
            f.company,
            f.sector_name,
            f.industry_aggregate_name,
            f.industry_name,
            f.rating,
            f.base_score,
            f.base_stock_z,
            f.industry_macro_fit,
            f.industry_aggregate_macro_fit,
            f.sector_macro_fit,
            f.sector_tactical_lift,
            f.sector_tactical_lift_z,
            f.shock_fit,
            f.macro_stock_fit_raw,
            f.macro_stock_fit_z,
            s.selection_score,
            s.selection_rank,
            s.selection_percentile,
            w.weight_score,
            w.weight_rank,
            w.weight_percentile,
            f.macro_favored_flag,
            f.macro_adverse_flag,
            f.base_optimizer_eligible,
            f.coverage_flag
        FROM stock_macro_fit_daily f
        JOIN stock_selection_score_daily s
          ON s.as_of_date = f.as_of_date
         AND s.ticker = f.ticker
        JOIN stock_weight_score_daily w
          ON w.as_of_date = f.as_of_date
         AND w.ticker = f.ticker
        WHERE f.as_of_date = ?
        ORDER BY s.selection_rank
        """,
        [latest_date],
    )
    return latest_date, frame


def _latest_checks(frame: pd.DataFrame, latest_date: str, thresholds: dict[str, float]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    row_count = int(len(frame))
    eligible_count = int(pd.to_numeric(frame["base_optimizer_eligible"], errors="coerce").fillna(0).sum())
    covered_count = int(pd.to_numeric(frame["coverage_flag"], errors="coerce").fillna(0).sum())
    selection = pd.to_numeric(frame["selection_score"], errors="coerce")
    weight = pd.to_numeric(frame["weight_score"], errors="coerce")
    macro = pd.to_numeric(frame["macro_stock_fit_z"], errors="coerce")
    base = pd.to_numeric(frame["base_stock_z"], errors="coerce")
    null_share = float((selection.isna() | weight.isna() | macro.isna() | base.isna()).mean()) if row_count else 1.0
    selection_std = float(selection.std(ddof=1)) if selection.notna().sum() > 1 else np.nan
    weight_std = float(weight.std(ddof=1)) if weight.notna().sum() > 1 else np.nan
    macro_std = float(macro.std(ddof=1)) if macro.notna().sum() > 1 else np.nan
    corr = float(base.corr(selection, method="spearman")) if base.notna().sum() > 2 and selection.notna().sum() > 2 else np.nan
    rank_shift = 1.0 - abs(corr) if np.isfinite(corr) else np.nan
    favored = frame.loc[pd.to_numeric(frame["macro_favored_flag"], errors="coerce").fillna(0).astype(int).eq(1)]
    adverse = frame.loc[pd.to_numeric(frame["macro_adverse_flag"], errors="coerce").fillna(0).astype(int).eq(1)]
    neutral_mean = float(selection.mean()) if selection.notna().any() else np.nan
    favored_lift = float(pd.to_numeric(favored["selection_score"], errors="coerce").mean() - neutral_mean) if not favored.empty else np.nan
    adverse_lift = float(pd.to_numeric(adverse["selection_score"], errors="coerce").mean() - neutral_mean) if not adverse.empty else np.nan
    rank_count = int(frame["selection_rank"].nunique())

    checks: list[tuple[str, object, str, bool]] = [
        ("latest_row_count", row_count, f">={thresholds['min_latest_rows']}", row_count >= thresholds["min_latest_rows"]),
        (
            "latest_eligible_row_count",
            eligible_count,
            f">={thresholds['min_latest_eligible_rows']}",
            eligible_count >= thresholds["min_latest_eligible_rows"],
        ),
        ("latest_covered_row_count", covered_count, "equals row_count", covered_count == row_count),
        ("latest_null_score_share", null_share, f"<={thresholds['max_null_score_share']}", null_share <= thresholds["max_null_score_share"]),
        (
            "latest_selection_score_std",
            selection_std,
            f">={thresholds['min_selection_score_std']}",
            np.isfinite(selection_std) and selection_std >= thresholds["min_selection_score_std"],
        ),
        (
            "latest_weight_score_std",
            weight_std,
            f">={thresholds['min_weight_score_std']}",
            np.isfinite(weight_std) and weight_std >= thresholds["min_weight_score_std"],
        ),
        (
            "latest_macro_fit_std",
            macro_std,
            f">={thresholds['min_macro_fit_std']}",
            np.isfinite(macro_std) and macro_std >= thresholds["min_macro_fit_std"],
        ),
        (
            "latest_selection_not_base_clone",
            rank_shift,
            f">={thresholds['min_rank_correlation_shift']}",
            np.isfinite(rank_shift) and rank_shift >= thresholds["min_rank_correlation_shift"],
        ),
        ("latest_selection_rank_unique", rank_count, "one rank per row", rank_count == row_count),
    ]
    require_lift = bool(thresholds.get("require_favored_adverse_lift", True))
    checks.extend(
        [
            (
                "latest_favored_selection_lift",
                favored_lift,
                f">={thresholds['favored_selection_lift_min']}" if require_lift else "informational",
                bool(np.isfinite(favored_lift) and favored_lift >= thresholds["favored_selection_lift_min"]) if require_lift else True,
            ),
            (
                "latest_adverse_selection_lift",
                adverse_lift,
                f"<={thresholds['adverse_selection_lift_max']}" if require_lift else "informational",
                bool(np.isfinite(adverse_lift) and adverse_lift <= thresholds["adverse_selection_lift_max"]) if require_lift else True,
            ),
        ]
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
            SELECT as_of_date, ticker, COUNT(*) AS row_count
            FROM stock_selection_score_daily
            GROUP BY as_of_date, ticker
            HAVING COUNT(*) > 1
        )
        """
    ).fetchone()
    join_missing = conn.execute(
        """
        SELECT COUNT(*) AS missing_count
        FROM stock_selection_score_daily s
        LEFT JOIN stock_macro_fit_daily f
          ON f.as_of_date = s.as_of_date
         AND f.ticker = s.ticker
        LEFT JOIN stock_weight_score_daily w
          ON w.as_of_date = s.as_of_date
         AND w.ticker = s.ticker
        WHERE f.ticker IS NULL OR w.ticker IS NULL
        """
    ).fetchone()
    bad_scores = conn.execute(
        """
        SELECT COUNT(*) AS bad_count
        FROM stock_selection_score_daily
        WHERE coverage_flag = 1
          AND (
              selection_score IS NULL
              OR selection_percentile < 0.0
              OR selection_percentile > 1.0
              OR selection_rank IS NULL
          )
        """
    ).fetchone()
    rows.extend(
        [
            {
                "check_name": "history_no_selection_duplicates",
                "value": int(duplicates["duplicate_count"] or 0),
                "threshold": "0",
                "passed": int(int(duplicates["duplicate_count"] or 0) == 0),
                "details": "",
            },
            {
                "check_name": "history_selection_fit_weight_join_complete",
                "value": int(join_missing["missing_count"] or 0),
                "threshold": "0",
                "passed": int(int(join_missing["missing_count"] or 0) == 0),
                "details": "",
            },
            {
                "check_name": "history_covered_selection_scores_valid",
                "value": int(bad_scores["bad_count"] or 0),
                "threshold": "0",
                "passed": int(int(bad_scores["bad_count"] or 0) == 0),
                "details": "",
            },
        ]
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
        latest_date, latest = _latest_frame(conn)
        summary = pd.concat(
            [
                _table_summary(conn),
                _latest_checks(latest, latest_date, thresholds),
                _history_checks(conn),
            ],
            ignore_index=True,
        )
        _write_atomic_csv(out_dir / "stage11_stock_overlay_acceptance_summary.csv", summary)
        _write_atomic_csv(out_dir / "stage11_stock_overlay_latest_scores.csv", latest)
    finally:
        conn.close()

    passed = bool(summary["passed"].astype(int).all())
    logger.info("Stage 11 stock macro overlay acceptance: %s", "PASS" if passed else "FAIL")
    print(f"STAGE_11_EXIT_GATE={'PASS' if passed else 'FAIL'}")
    if not passed:
        failed = summary.loc[summary["passed"].astype(int) == 0]
        print(failed.to_string(index=False))
        raise SystemExit(1)


if __name__ == "__main__":
    main()

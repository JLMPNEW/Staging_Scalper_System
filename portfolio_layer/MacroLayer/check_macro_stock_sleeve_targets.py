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

DEFAULT_OUT_DIR = "MacroLayer/out/stock_sleeve_target_checks"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Stage 12B stock sleeve target acceptance diagnostics.")
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--serving-db-path", type=Path, default=None, help="Optional serving SQLite path override.")
    parser.add_argument("--out-dir", type=Path, default=None, help="Output directory for diagnostic CSVs.")
    return parser.parse_args()


def _resolve_out_dir(config_path: Path, raw_out_dir: Path | None) -> Path:
    if raw_out_dir is not None:
        return raw_out_dir.expanduser().resolve()
    out_dir = resolve_path(config_path, DEFAULT_OUT_DIR)
    if out_dir is None:
        raise ValueError("Unable to resolve Stage 12B diagnostic output directory.")
    return out_dir


def _thresholds(cfg: dict) -> dict[str, float]:
    target_cfg = dict(cfg_get(cfg, "stock_sleeve_target_layer", "target", default={}) or {})
    bands_cfg = dict(cfg_get(cfg, "stock_sleeve_target_layer", "bands", default={}) or {})
    raw = dict(cfg_get(cfg, "stock_sleeve_target_layer", "acceptance", default={}) or {})
    defaults = {
        "min_latest_industries": 25.0,
        "min_latest_targetable_industries": 20.0,
        "min_latest_sectors": 8.0,
        "min_effective_industry_count": 12.0,
        "target_sum_tolerance": 1e-6,
        "max_industry_weight": float(target_cfg.get("max_industry_weight", 0.08)),
        "max_sector_weight": float(bands_cfg.get("max_sector_weight", 0.35)),
    }
    defaults.update({str(k): float(v) for k, v in raw.items()})
    return defaults


def _read_sql(conn: sqlite3.Connection, query: str, params: Iterable[object] = ()) -> pd.DataFrame:
    return pd.read_sql_query(query, conn, params=list(params))


def _table_summary(conn: sqlite3.Connection) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for table_name in ("stock_industry_target_daily", "stock_sector_target_daily", "stock_sleeve_target_summary"):
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
        rows.append(
            {
                "check_name": f"{table_name}_nonempty",
                "value": row_count,
                "threshold": ">0",
                "passed": int(row_count > 0),
                "details": f"min={row['min_date']} max={row['max_date']} dates={row['date_count']}",
            }
        )
    return pd.DataFrame(rows)


def _latest_frames(conn: sqlite3.Connection) -> tuple[str, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    row = conn.execute("SELECT MAX(as_of_date) AS latest_date FROM stock_industry_target_daily").fetchone()
    latest_date = str(row["latest_date"] or "")
    if not latest_date:
        raise ValueError("stock_industry_target_daily is empty.")
    industry = _read_sql(
        conn,
        """
        SELECT *
        FROM stock_industry_target_daily
        WHERE as_of_date = ?
        ORDER BY target_rank
        """,
        [latest_date],
    )
    sector = _read_sql(
        conn,
        """
        SELECT *
        FROM stock_sector_target_daily
        WHERE as_of_date = ?
        ORDER BY target_rank
        """,
        [latest_date],
    )
    summary = _read_sql(
        conn,
        """
        SELECT *
        FROM stock_sleeve_target_summary
        WHERE as_of_date = ?
        """,
        [latest_date],
    )
    return latest_date, industry, sector, summary


def _latest_checks(
    industry: pd.DataFrame,
    sector: pd.DataFrame,
    summary: pd.DataFrame,
    latest_date: str,
    thresholds: dict[str, float],
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    industry_weight = pd.to_numeric(industry["target_weight"], errors="coerce").fillna(0.0)
    sector_weight = pd.to_numeric(sector["target_weight"], errors="coerce").fillna(0.0)
    industry_lower = pd.to_numeric(industry["min_weight"], errors="coerce").fillna(0.0)
    industry_upper = pd.to_numeric(industry["max_weight"], errors="coerce").fillna(0.0)
    sector_lower = pd.to_numeric(sector["min_weight"], errors="coerce").fillna(0.0)
    sector_upper = pd.to_numeric(sector["max_weight"], errors="coerce").fillna(0.0)
    targetable = int(pd.to_numeric(industry["coverage_flag"], errors="coerce").fillna(0).astype(int).sum())
    effective = 0.0
    if float((industry_weight ** 2).sum()) > 0.0:
        effective = 1.0 / float((industry_weight ** 2).sum())
    duplicate_industry = int(
        industry.groupby(["as_of_date", "sector_name", "industry_aggregate_name", "industry_name"])
        .size()
        .reset_index(name="n")
        .query("n > 1")
        .shape[0]
    )
    duplicate_sector = int(
        sector.groupby(["as_of_date", "sector_name"]).size().reset_index(name="n").query("n > 1").shape[0]
    )
    checks = [
        (
            "latest_industry_count",
            int(len(industry)),
            f">={thresholds['min_latest_industries']}",
            len(industry) >= thresholds["min_latest_industries"],
        ),
        (
            "latest_targetable_industry_count",
            targetable,
            f">={thresholds['min_latest_targetable_industries']}",
            targetable >= thresholds["min_latest_targetable_industries"],
        ),
        (
            "latest_sector_count",
            int(len(sector)),
            f">={thresholds['min_latest_sectors']}",
            len(sector) >= thresholds["min_latest_sectors"],
        ),
        (
            "latest_industry_target_sum",
            float(industry_weight.sum()),
            f"abs(sum-1)<={thresholds['target_sum_tolerance']}",
            abs(float(industry_weight.sum()) - 1.0) <= thresholds["target_sum_tolerance"],
        ),
        (
            "latest_sector_target_sum",
            float(sector_weight.sum()),
            f"abs(sum-1)<={thresholds['target_sum_tolerance']}",
            abs(float(sector_weight.sum()) - 1.0) <= thresholds["target_sum_tolerance"],
        ),
        (
            "latest_industry_targets_within_bands",
            int(((industry_weight >= industry_lower - 1e-12) & (industry_weight <= industry_upper + 1e-12)).sum()),
            "all industry target weights inside bands",
            bool(((industry_weight >= industry_lower - 1e-12) & (industry_weight <= industry_upper + 1e-12)).all()),
        ),
        (
            "latest_sector_targets_within_bands",
            int(((sector_weight >= sector_lower - 1e-12) & (sector_weight <= sector_upper + 1e-12)).sum()),
            "all sector target weights inside bands",
            bool(((sector_weight >= sector_lower - 1e-12) & (sector_weight <= sector_upper + 1e-12)).all()),
        ),
        (
            "latest_max_industry_weight",
            float(industry_weight.max()) if len(industry_weight) else np.nan,
            f"<={thresholds['max_industry_weight']}",
            len(industry_weight) > 0 and float(industry_weight.max()) <= thresholds["max_industry_weight"] + 1e-12,
        ),
        (
            "latest_max_sector_weight",
            float(sector_weight.max()) if len(sector_weight) else np.nan,
            f"<={thresholds['max_sector_weight']}",
            len(sector_weight) > 0 and float(sector_weight.max()) <= thresholds["max_sector_weight"] + 1e-12,
        ),
        (
            "latest_effective_industry_count",
            effective,
            f">={thresholds['min_effective_industry_count']}",
            effective >= thresholds["min_effective_industry_count"],
        ),
        ("latest_no_duplicate_industry_keys", duplicate_industry, "0", duplicate_industry == 0),
        ("latest_no_duplicate_sector_keys", duplicate_sector, "0", duplicate_sector == 0),
        ("latest_summary_row_present", int(len(summary)), "1", len(summary) == 1),
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
    duplicate_industry = conn.execute(
        """
        SELECT COUNT(*) AS duplicate_count
        FROM (
            SELECT as_of_date, sector_name, industry_aggregate_name, industry_name, COUNT(*) AS row_count
            FROM stock_industry_target_daily
            GROUP BY as_of_date, sector_name, industry_aggregate_name, industry_name
            HAVING COUNT(*) > 1
        )
        """
    ).fetchone()
    bad_industry = conn.execute(
        """
        SELECT COUNT(*) AS bad_count
        FROM stock_industry_target_daily
        WHERE target_weight IS NULL
           OR min_weight IS NULL
           OR max_weight IS NULL
           OR target_weight < -0.000000001
           OR min_weight < -0.000000001
           OR max_weight < target_weight - 0.000000001
           OR target_percentile < 0.0
           OR target_percentile > 1.0
        """
    ).fetchone()
    bad_sums = conn.execute(
        """
        SELECT COUNT(*) AS bad_count
        FROM (
            SELECT
                as_of_date,
                SUM(coverage_flag) AS targetable_count,
                ABS(SUM(target_weight) - 1.0) AS sum_error
            FROM stock_industry_target_daily
            GROUP BY as_of_date
        )
        WHERE targetable_count > 0
          AND sum_error > ?
        """,
        [thresholds["target_sum_tolerance"]],
    ).fetchone()
    rows.extend(
        [
            {
                "check_name": "history_no_industry_duplicates",
                "value": int(duplicate_industry["duplicate_count"] or 0),
                "threshold": "0",
                "passed": int(int(duplicate_industry["duplicate_count"] or 0) == 0),
                "details": "",
            },
            {
                "check_name": "history_industry_targets_valid",
                "value": int(bad_industry["bad_count"] or 0),
                "threshold": "0",
                "passed": int(int(bad_industry["bad_count"] or 0) == 0),
                "details": "",
            },
            {
                "check_name": "history_industry_target_sums_valid",
                "value": int(bad_sums["bad_count"] or 0),
                "threshold": "0",
                "passed": int(int(bad_sums["bad_count"] or 0) == 0),
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
    for col_name, required_cols in (
        (
            "industry_output_csv",
            {"as_of_date", "sector_name", "industry_name", "target_weight", "min_weight", "max_weight"},
        ),
        ("sector_output_csv", {"as_of_date", "sector_name", "target_weight", "min_weight", "max_weight"}),
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
        latest_date, industry, sector, latest_summary = _latest_frames(conn)
        summary = pd.concat(
            [
                _table_summary(conn),
                _latest_checks(industry, sector, latest_summary, latest_date, thresholds),
                _history_checks(conn, thresholds),
                _export_checks(latest_summary),
            ],
            ignore_index=True,
        )
        _write_atomic_csv(out_dir / "stage12b_stock_sleeve_target_acceptance_summary.csv", summary)
        _write_atomic_csv(out_dir / "stage12b_stock_industry_targets_latest.csv", industry)
        _write_atomic_csv(out_dir / "stage12b_stock_sector_targets_latest.csv", sector)
    finally:
        conn.close()

    passed = bool(summary["passed"].astype(int).all())
    logger.info("Stage 12B stock sleeve target acceptance: %s", "PASS" if passed else "FAIL")
    print(f"STAGE_12B_EXIT_GATE={'PASS' if passed else 'FAIL'}")
    if not passed:
        failed = summary.loc[summary["passed"].astype(int) == 0]
        print(failed.to_string(index=False))
        raise SystemExit(1)


if __name__ == "__main__":
    main()

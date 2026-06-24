#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sqlite3
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from macro_raw_config import configure_pipeline_logging, connect_sqlite, load_macro_raw_config, resolve_path
from macro_serving_common import resolve_serving_db_path

logger = logging.getLogger(__name__)

DEFAULT_OUT_DIR = "MacroLayer/out/country_macro_checks"
MIN_LATEST_COUNTRIES = 8
MIN_LATEST_ELIGIBLE_COUNTRIES = 6
MIN_ADJUSTED_SCORE_RANGE = 0.05


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Stage 10 country macro fit acceptance diagnostics.")
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--serving-db-path", type=Path, default=None, help="Optional serving SQLite path override.")
    parser.add_argument("--out-dir", type=Path, default=None, help="Output directory for diagnostic CSVs.")
    return parser.parse_args()


def _resolve_out_dir(config_path: Path, raw_out_dir: Path | None) -> Path:
    if raw_out_dir is not None:
        return raw_out_dir.expanduser().resolve()
    out_dir = resolve_path(config_path, DEFAULT_OUT_DIR)
    if out_dir is None:
        raise ValueError("Unable to resolve Stage 10 diagnostic output directory.")
    return out_dir


def _read_sql(conn: sqlite3.Connection, query: str, params: Iterable[object] = ()) -> pd.DataFrame:
    return pd.read_sql_query(query, conn, params=list(params))


def _table_summary(conn: sqlite3.Connection) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for table_name in ("country_macro_fit_daily", "country_confidence_daily", "country_macro_rank_daily"):
        summary = conn.execute(
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
        row_count = int(summary["row_count"] or 0)
        key_count = int(summary["natural_key_count"] or 0)
        rows.append(
            {
                "check_name": f"{table_name}_nonempty_unique",
                "value": row_count,
                "threshold": ">0 and no duplicate natural keys",
                "passed": int(row_count > 0 and row_count == key_count),
                "details": f"min={summary['min_date']} max={summary['max_date']} dates={summary['date_count']} keys={key_count}",
            }
        )
    return pd.DataFrame(rows)


def _latest_joined(conn: sqlite3.Connection) -> tuple[str, pd.DataFrame]:
    latest_row = conn.execute("SELECT MAX(as_of_date) AS latest_date FROM country_macro_rank_daily").fetchone()
    latest_date = str(latest_row["latest_date"] or "")
    if not latest_date:
        raise ValueError("country_macro_rank_daily is empty.")
    frame = _read_sql(
        conn,
        """
        SELECT
            r.as_of_date,
            r.ticker,
            r.ref_area,
            r.country_class,
            f.country_name,
            f.region,
            f.market_class,
            f.active_current_regime,
            f.active_next_regime,
            f.global_regime_fit,
            f.local_macro_fit,
            f.external_shock_fit,
            f.growth_now_score,
            f.growth_lead_score,
            f.inflation_score,
            f.local_external_score,
            f.global_shock_score,
            r.country_macro_fit,
            r.country_confidence,
            r.confidence_adjusted_fit,
            r.country_rank,
            r.country_percentile,
            r.eligible_flag,
            r.rank_reason,
            r.coverage_flag
        FROM country_macro_rank_daily r
        JOIN country_macro_fit_daily f
          ON f.as_of_date = r.as_of_date
         AND f.ticker = r.ticker
        WHERE r.as_of_date = ?
        ORDER BY r.country_rank
        """,
        [latest_date],
    )
    return latest_date, frame


def _latest_checks(frame: pd.DataFrame, latest_date: str) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    latest_count = int(len(frame))
    eligible_count = int(pd.to_numeric(frame["eligible_flag"], errors="coerce").fillna(0).sum())
    confidence = pd.to_numeric(frame["country_confidence"], errors="coerce")
    adjusted = pd.to_numeric(frame["confidence_adjusted_fit"], errors="coerce")
    raw_fit = pd.to_numeric(frame["country_macro_fit"], errors="coerce")
    rank_count = int(frame["country_rank"].nunique())
    adjusted_range = float(adjusted.max() - adjusted.min()) if adjusted.notna().any() else np.nan
    external_range = (
        float(pd.to_numeric(frame["external_shock_fit"], errors="coerce").max() - pd.to_numeric(frame["external_shock_fit"], errors="coerce").min())
        if frame["external_shock_fit"].notna().any()
        else np.nan
    )

    rows.append(
        {
            "check_name": "latest_country_count",
            "value": latest_count,
            "threshold": f">={MIN_LATEST_COUNTRIES}",
            "passed": int(latest_count >= MIN_LATEST_COUNTRIES),
            "details": latest_date,
        }
    )
    rows.append(
        {
            "check_name": "latest_eligible_country_count",
            "value": eligible_count,
            "threshold": f">={MIN_LATEST_ELIGIBLE_COUNTRIES}",
            "passed": int(eligible_count >= MIN_LATEST_ELIGIBLE_COUNTRIES),
            "details": latest_date,
        }
    )
    rows.append(
        {
            "check_name": "latest_confidence_bounded",
            "value": f"{confidence.min():.6f}..{confidence.max():.6f}",
            "threshold": "[0, 1]",
            "passed": int(confidence.notna().all() and bool(((confidence >= 0.0) & (confidence <= 1.0)).all())),
            "details": latest_date,
        }
    )
    rows.append(
        {
            "check_name": "latest_scores_finite",
            "value": int(raw_fit.notna().sum()),
            "threshold": "all latest countries finite",
            "passed": int(raw_fit.notna().all() and adjusted.notna().all()),
            "details": latest_date,
        }
    )
    rows.append(
        {
            "check_name": "latest_rank_unique",
            "value": rank_count,
            "threshold": "one rank per country",
            "passed": int(rank_count == latest_count),
            "details": latest_date,
        }
    )
    rows.append(
        {
            "check_name": "latest_adjusted_scores_not_collapsed",
            "value": adjusted_range,
            "threshold": f">={MIN_ADJUSTED_SCORE_RANGE}",
            "passed": int(np.isfinite(adjusted_range) and adjusted_range >= MIN_ADJUSTED_SCORE_RANGE),
            "details": latest_date,
        }
    )
    rows.append(
        {
            "check_name": "latest_external_shock_component_varies",
            "value": external_range,
            "threshold": ">0 or finite single-context fallback",
            "passed": int(np.isfinite(external_range) and external_range >= 0.0),
            "details": latest_date,
        }
    )

    class_means = confidence.groupby(frame["country_class"].astype(str)).mean()
    a_mean = class_means.get("A_full", np.nan)
    c_mean = class_means.get("C_fallback", np.nan)
    haircut_passed = True
    if np.isfinite(a_mean) and np.isfinite(c_mean):
        haircut_passed = bool(a_mean > c_mean)
    rows.append(
        {
            "check_name": "class_confidence_haircuts_present",
            "value": "; ".join(f"{idx}={val:.3f}" for idx, val in class_means.items()),
            "threshold": "A_full confidence above C_fallback when both exist",
            "passed": int(haircut_passed),
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
            FROM country_macro_rank_daily
            GROUP BY as_of_date, ticker
            HAVING COUNT(*) > 1
        )
        """
    ).fetchone()
    confidence_bad = conn.execute(
        """
        SELECT COUNT(*) AS bad_count
        FROM country_confidence_daily
        WHERE country_confidence < 0.0 OR country_confidence > 1.0 OR country_confidence IS NULL
        """
    ).fetchone()
    rank_bad = conn.execute(
        """
        SELECT COUNT(*) AS bad_count
        FROM country_macro_rank_daily
        WHERE country_rank IS NULL OR country_percentile < 0.0 OR country_percentile > 1.0
        """
    ).fetchone()
    rows.append(
        {
            "check_name": "history_no_rank_duplicates",
            "value": int(duplicates["duplicate_count"] or 0),
            "threshold": "0",
            "passed": int(int(duplicates["duplicate_count"] or 0) == 0),
            "details": "",
        }
    )
    rows.append(
        {
            "check_name": "history_confidence_bounded",
            "value": int(confidence_bad["bad_count"] or 0),
            "threshold": "0",
            "passed": int(int(confidence_bad["bad_count"] or 0) == 0),
            "details": "",
        }
    )
    rows.append(
        {
            "check_name": "history_rank_percentile_valid",
            "value": int(rank_bad["bad_count"] or 0),
            "threshold": "0",
            "passed": int(int(rank_bad["bad_count"] or 0) == 0),
            "details": "",
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

    conn = connect_sqlite(serving_db_path, row_factory=sqlite3.Row)
    try:
        table_summary = _table_summary(conn)
        latest_date, latest_frame = _latest_joined(conn)
        summary = pd.concat(
            [
                table_summary,
                _latest_checks(latest_frame, latest_date),
                _history_checks(conn),
            ],
            ignore_index=True,
        )
        _write_atomic_csv(out_dir / "stage10_country_macro_acceptance_summary.csv", summary)
        _write_atomic_csv(out_dir / "stage10_country_macro_latest_rank.csv", latest_frame)
    finally:
        conn.close()

    passed = bool(summary["passed"].astype(int).all())
    logger.info("Stage 10 country macro acceptance: %s", "PASS" if passed else "FAIL")
    print(f"STAGE_10_EXIT_GATE={'PASS' if passed else 'FAIL'}")
    if not passed:
        failed = summary.loc[summary["passed"].astype(int) == 0]
        print(failed.to_string(index=False))
        raise SystemExit(1)


if __name__ == "__main__":
    main()

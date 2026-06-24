#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from macro_raw_config import (
    configure_pipeline_logging,
    connect_sqlite,
    load_macro_raw_config,
    resolve_path,
)
from macro_serving_common import resolve_serving_db_path

logger = logging.getLogger(__name__)

DEFAULT_OUT_DIR = "MacroLayer/out/industry_macro_checks"
MAX_TARGET_DATE_GAP_DAYS = 10

MIN_2020_WEEKLY_DATES = 45
MIN_LATEST_SECTORS = 11
MIN_LATEST_AGGREGATES = 30
MIN_LATEST_INDUSTRIES = 130

MAX_MEDIAN_WEEKLY_CHANGE = 0.20
MAX_P95_WEEKLY_CHANGE = 0.75
MAX_P99_WEEKLY_CHANGE = 1.25
MAX_GT_1_WEEKLY_CHANGE_SHARE = 0.02

MIN_PEER_SCORE_RANGE = 0.05
MIN_PEER_SCORE_STD = 0.02
MIN_PEER_DISTINCT_SCORES = 3


@dataclass(frozen=True)
class WindowSpec:
    name: str
    target_date: date | None
    top_sector_any: tuple[str, ...] = ()
    bottom_sector_any: tuple[str, ...] = ()


WINDOW_SPECS = (
    WindowSpec(
        name="COVID_SHOCK_2020",
        target_date=date(2020, 3, 20),
        top_sector_any=("Consumer Defensive", "Healthcare", "Utilities"),
        bottom_sector_any=("Financial Services", "Real Estate", "Energy"),
    ),
    WindowSpec(name="COVID_REOPENING_2020", target_date=date(2020, 6, 26)),
    WindowSpec(
        name="INFLATION_SHOCK_2022",
        target_date=date(2022, 6, 17),
        top_sector_any=("Energy", "Basic Materials"),
        bottom_sector_any=("Real Estate", "Consumer Cyclical"),
    ),
    WindowSpec(name="DISINFLATION_GROWTH_2023", target_date=date(2023, 12, 29)),
    WindowSpec(name="DISINFLATION_GROWTH_2024", target_date=date(2024, 12, 27)),
    WindowSpec(name="DISINFLATION_GROWTH_2025", target_date=date(2025, 12, 26)),
    WindowSpec(name="LATEST", target_date=None),
)


@dataclass(frozen=True)
class PeerGroupSpec:
    name: str
    patterns: tuple[str, ...]
    min_members: int


PEER_GROUPS = (
    PeerGroupSpec(
        name="TECH_SEMIS_SOFTWARE_HARDWARE",
        patterns=("Semiconductor", "Software", "Computer Hardware"),
        min_members=5,
    ),
    PeerGroupSpec(
        name="FINANCIALS_BANKS_INSURERS_BROKERS",
        patterns=("Banks", "Insurance", "Broker"),
        min_members=6,
    ),
    PeerGroupSpec(
        name="ENERGY_SUBSECTORS",
        patterns=("Oil & Gas", "Coal"),
        min_members=5,
    ),
    PeerGroupSpec(
        name="REIT_SUBSECTORS",
        patterns=("REIT",),
        min_members=6,
    ),
)

EXTREME_COLUMNS = [
    "window_name",
    "selected_date",
    "as_of_date",
    "sector_name",
    "industry_aggregate_name",
    "industry_name",
    "active_current_regime",
    "active_next_regime",
    "final_score",
    "prior_score",
    "empirical_score",
    "empirical_weight",
    "member_count",
    "effective_history_weeks",
    "basket_return",
    "excess_return",
    "coverage_flag",
    "rank_side",
    "rank",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Stage 9 industry macro fit acceptance diagnostics.")
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--serving-db-path", type=Path, default=None, help="Optional serving SQLite path override.")
    parser.add_argument("--out-dir", type=Path, default=None, help="Output directory for diagnostic CSVs.")
    return parser.parse_args()


def _resolve_out_dir(config_path: Path, raw_out_dir: Path | None) -> Path:
    if raw_out_dir is not None:
        return raw_out_dir.expanduser().resolve()
    out_dir = resolve_path(config_path, DEFAULT_OUT_DIR)
    if out_dir is None:
        raise ValueError("Unable to resolve Stage 9 diagnostic output directory.")
    return out_dir


def _read_sql(conn: sqlite3.Connection, query: str, params: Iterable[object] = ()) -> pd.DataFrame:
    return pd.read_sql_query(query, conn, params=list(params))


def _available_industry_dates(conn: sqlite3.Connection) -> list[date]:
    rows = conn.execute(
        """
        SELECT DISTINCT as_of_date
        FROM industry_macro_fit_daily
        WHERE coverage_flag = 1
        ORDER BY as_of_date
        """
    ).fetchall()
    return [date.fromisoformat(str(row[0])) for row in rows]


def _select_window_dates(conn: sqlite3.Connection) -> pd.DataFrame:
    dates = _available_industry_dates(conn)
    if not dates:
        raise ValueError("industry_macro_fit_daily has no covered rows.")

    latest_date = max(dates)
    rows: list[dict[str, object]] = []
    for spec in WINDOW_SPECS:
        if spec.target_date is None:
            selected_date = latest_date
            gap_days = 0
        else:
            prior_dates = [item for item in dates if item <= spec.target_date]
            selected_date = max(prior_dates) if prior_dates else min(dates)
            gap_days = abs((spec.target_date - selected_date).days)
        rows.append(
            {
                "window_name": spec.name,
                "target_date": spec.target_date.isoformat() if spec.target_date else "LATEST",
                "selected_date": selected_date.isoformat(),
                "target_gap_days": gap_days,
                "passed": int(gap_days <= MAX_TARGET_DATE_GAP_DAYS),
            }
        )
    return pd.DataFrame(rows)


def _coverage_checks(conn: sqlite3.Connection, latest_date: str) -> pd.DataFrame:
    table_thresholds = {
        "sector_macro_fit_daily": MIN_LATEST_SECTORS,
        "industry_aggregate_macro_fit_daily": MIN_LATEST_AGGREGATES,
        "industry_macro_fit_daily": MIN_LATEST_INDUSTRIES,
    }
    rows: list[dict[str, object]] = []
    for table_name, latest_min_count in table_thresholds.items():
        coverage = conn.execute(
            f"""
            SELECT
                COUNT(*) AS row_count,
                MIN(as_of_date) AS min_date,
                MAX(as_of_date) AS max_date,
                COUNT(DISTINCT as_of_date) AS date_count
            FROM {table_name}
            WHERE coverage_flag = 1
            """
        ).fetchone()
        year_2020 = conn.execute(
            f"""
            SELECT COUNT(DISTINCT as_of_date) AS date_count
            FROM {table_name}
            WHERE coverage_flag = 1
              AND as_of_date BETWEEN '2020-01-01' AND '2020-12-31'
            """
        ).fetchone()
        latest = conn.execute(
            f"""
            SELECT COUNT(*) AS latest_count
            FROM {table_name}
            WHERE coverage_flag = 1
              AND as_of_date = ?
            """,
            [latest_date],
        ).fetchone()
        min_date = str(coverage["min_date"] or "")
        max_date = str(coverage["max_date"] or "")
        date_count_2020 = int(year_2020["date_count"] or 0)
        latest_count = int(latest["latest_count"] or 0)
        rows.append(
            {
                "check_name": f"{table_name}_coverage",
                "passed": int(
                    min_date <= "2020-01-31"
                    and max_date >= latest_date
                    and date_count_2020 >= MIN_2020_WEEKLY_DATES
                    and latest_count >= latest_min_count
                ),
                "table_name": table_name,
                "min_date": min_date,
                "max_date": max_date,
                "covered_date_count": int(coverage["date_count"] or 0),
                "covered_2020_date_count": date_count_2020,
                "latest_date": latest_date,
                "latest_count": latest_count,
                "threshold": (
                    f"min_date<=2020-01-31; 2020_dates>={MIN_2020_WEEKLY_DATES}; "
                    f"latest_count>={latest_min_count}"
                ),
            }
        )
    return pd.DataFrame(rows)


def _load_industry_frame(conn: sqlite3.Connection) -> pd.DataFrame:
    frame = _read_sql(
        conn,
        """
        SELECT
            as_of_date,
            sector_name,
            industry_aggregate_name,
            industry_name,
            active_current_regime,
            active_next_regime,
            final_score,
            prior_score,
            empirical_score,
            empirical_weight,
            member_count,
            effective_history_weeks,
            basket_return,
            excess_return,
            coverage_flag
        FROM industry_macro_fit_daily
        WHERE coverage_flag = 1
        ORDER BY as_of_date, sector_name, industry_aggregate_name, industry_name
        """,
    )
    if frame.empty:
        raise ValueError("industry_macro_fit_daily has no covered rows.")
    frame["as_of_date"] = frame["as_of_date"].astype(str)
    frame["final_score"] = pd.to_numeric(frame["final_score"], errors="coerce")
    return frame.dropna(subset=["final_score"]).copy()


def _window_extremes(
    industry_frame: pd.DataFrame,
    window_dates: pd.DataFrame,
    *,
    top_n: int = 15,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    extremes: list[pd.DataFrame] = []
    checks: list[dict[str, object]] = []
    spec_map = {spec.name: spec for spec in WINDOW_SPECS}

    for row in window_dates.itertuples(index=False):
        selected_date = str(row.selected_date)
        window_name = str(row.window_name)
        date_frame = industry_frame.loc[industry_frame["as_of_date"].eq(selected_date)].copy()
        if date_frame.empty:
            checks.append(
                {
                    "check_name": f"{window_name}_window_rows",
                    "passed": 0,
                    "observed": 0,
                    "threshold": "non-empty selected date",
                    "notes": selected_date,
                }
            )
            continue
        sorted_frame = date_frame.sort_values("final_score", ascending=False)
        top = sorted_frame.head(top_n).copy()
        bottom = sorted_frame.tail(top_n).sort_values("final_score", ascending=True).copy()
        top["rank_side"] = "TOP"
        top["rank"] = np.arange(1, len(top) + 1)
        bottom["rank_side"] = "BOTTOM"
        bottom["rank"] = np.arange(1, len(bottom) + 1)
        for part in (top, bottom):
            part.insert(0, "window_name", window_name)
            part.insert(1, "selected_date", selected_date)
            extremes.append(part)

        spec = spec_map[window_name]
        if spec.top_sector_any:
            top_sectors = set(top["sector_name"].dropna().astype(str).tolist())
            checks.append(
                {
                    "check_name": f"{window_name}_top_sector_plausibility",
                    "passed": int(bool(top_sectors.intersection(spec.top_sector_any))),
                    "observed": ", ".join(sorted(top_sectors)),
                    "threshold": "top basket contains one expected stress beneficiary sector",
                    "notes": ", ".join(spec.top_sector_any),
                }
            )
        if spec.bottom_sector_any:
            bottom_sectors = set(bottom["sector_name"].dropna().astype(str).tolist())
            checks.append(
                {
                    "check_name": f"{window_name}_bottom_sector_plausibility",
                    "passed": int(bool(bottom_sectors.intersection(spec.bottom_sector_any))),
                    "observed": ", ".join(sorted(bottom_sectors)),
                    "threshold": "bottom basket contains one expected stress-lagging sector",
                    "notes": ", ".join(spec.bottom_sector_any),
                }
            )

    extremes_frame = (
        pd.concat(extremes, ignore_index=True, sort=False)
        if extremes
        else pd.DataFrame(columns=EXTREME_COLUMNS)
    )
    return extremes_frame, pd.DataFrame(checks)


def _peer_group_checks(industry_frame: pd.DataFrame, window_dates: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for date_row in window_dates.itertuples(index=False):
        window_name = str(date_row.window_name)
        selected_date = str(date_row.selected_date)
        date_frame = industry_frame.loc[industry_frame["as_of_date"].eq(selected_date)].copy()
        names = date_frame["industry_name"].fillna("").astype(str)
        for spec in PEER_GROUPS:
            mask = pd.Series(False, index=date_frame.index)
            for pattern in spec.patterns:
                mask = mask | names.str.contains(pattern, case=False, regex=False)
            sub = date_frame.loc[mask].copy()
            member_count = int(len(sub))
            score_range = float(sub["final_score"].max() - sub["final_score"].min()) if member_count else np.nan
            score_std = float(sub["final_score"].std(ddof=1)) if member_count > 1 else np.nan
            distinct_scores = int(sub["final_score"].round(6).nunique()) if member_count else 0
            passed = (
                member_count >= spec.min_members
                and np.isfinite(score_range)
                and np.isfinite(score_std)
                and score_range >= MIN_PEER_SCORE_RANGE
                and score_std >= MIN_PEER_SCORE_STD
                and distinct_scores >= MIN_PEER_DISTINCT_SCORES
            )
            rows.append(
                {
                    "window_name": window_name,
                    "selected_date": selected_date,
                    "peer_group": spec.name,
                    "passed": int(passed),
                    "member_count": member_count,
                    "score_min": float(sub["final_score"].min()) if member_count else np.nan,
                    "score_max": float(sub["final_score"].max()) if member_count else np.nan,
                    "score_range": score_range,
                    "score_std": score_std,
                    "distinct_scores": distinct_scores,
                    "industries": " | ".join(sorted(sub["industry_name"].dropna().astype(str).unique().tolist())),
                    "threshold": (
                        f"members>={spec.min_members}; range>={MIN_PEER_SCORE_RANGE}; "
                        f"std>={MIN_PEER_SCORE_STD}; distinct>={MIN_PEER_DISTINCT_SCORES}"
                    ),
                }
            )
    return pd.DataFrame(rows)


def _stability_checks(industry_frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    group_cols = ["sector_name", "industry_aggregate_name", "industry_name"]
    ordered = industry_frame.sort_values(group_cols + ["as_of_date"]).copy()
    ordered["abs_weekly_score_change"] = ordered.groupby(group_cols, dropna=False)["final_score"].diff().abs()
    changes = ordered.dropna(subset=["abs_weekly_score_change"]).copy()

    def summarize(label: str, frame: pd.DataFrame) -> dict[str, object]:
        values = frame["abs_weekly_score_change"].dropna().to_numpy(dtype=float)
        if len(values) == 0:
            return {
                "sample": label,
                "passed": 0,
                "count": 0,
                "mean": np.nan,
                "median": np.nan,
                "p95": np.nan,
                "p99": np.nan,
                "max": np.nan,
                "gt_1_share": np.nan,
                "threshold": "non-empty weekly score-change sample",
            }
        median = float(np.percentile(values, 50))
        p95 = float(np.percentile(values, 95))
        p99 = float(np.percentile(values, 99))
        gt_1_share = float((values > 1.0).mean())
        passed = (
            median <= MAX_MEDIAN_WEEKLY_CHANGE
            and p95 <= MAX_P95_WEEKLY_CHANGE
            and p99 <= MAX_P99_WEEKLY_CHANGE
            and gt_1_share <= MAX_GT_1_WEEKLY_CHANGE_SHARE
        )
        return {
            "sample": label,
            "passed": int(passed),
            "count": int(len(values)),
            "mean": float(values.mean()),
            "median": median,
            "p95": p95,
            "p99": p99,
            "max": float(values.max()),
            "gt_1_share": gt_1_share,
            "threshold": (
                f"median<={MAX_MEDIAN_WEEKLY_CHANGE}; p95<={MAX_P95_WEEKLY_CHANGE}; "
                f"p99<={MAX_P99_WEEKLY_CHANGE}; gt_1_share<={MAX_GT_1_WEEKLY_CHANGE_SHARE}"
            ),
        }

    all_summary = summarize("ALL_HISTORY", changes)
    sample_2020 = changes.loc[changes["as_of_date"].between("2020-01-01", "2020-12-31")].copy()
    summary_2020 = summarize("YEAR_2020", sample_2020)
    return changes, pd.DataFrame([all_summary, summary_2020])


def main() -> int:
    configure_pipeline_logging()
    args = parse_args()
    config_path, cfg = load_macro_raw_config(args.config)
    serving_db_path = resolve_serving_db_path(cfg, config_path, override=args.serving_db_path)
    out_dir = _resolve_out_dir(config_path, args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = connect_sqlite(serving_db_path, row_factory=sqlite3.Row)
    try:
        window_dates = _select_window_dates(conn)
        latest_date = str(window_dates.loc[window_dates["window_name"].eq("LATEST"), "selected_date"].iloc[0])
        coverage_checks = _coverage_checks(conn, latest_date=latest_date)
        industry_frame = _load_industry_frame(conn)
        extremes, plausibility_checks = _window_extremes(industry_frame, window_dates)
        peer_checks = _peer_group_checks(industry_frame, window_dates)
        stability_details, stability_summary = _stability_checks(industry_frame)
    finally:
        conn.close()

    window_checks = window_dates.rename(columns={"window_name": "check_name"}).copy()
    window_checks["check_name"] = "window_date_available_" + window_checks["check_name"].astype(str)
    window_checks["threshold"] = f"selected date within {MAX_TARGET_DATE_GAP_DAYS} days of target"
    window_checks["observed"] = window_checks["selected_date"]

    peer_summary = peer_checks.rename(columns={"peer_group": "check_name"}).copy()
    peer_summary["check_name"] = "peer_noncollapse_" + peer_summary["window_name"].astype(str) + "_" + peer_summary["check_name"].astype(str)
    peer_summary["observed"] = (
        "members="
        + peer_summary["member_count"].astype(str)
        + "; range="
        + peer_summary["score_range"].round(4).astype(str)
        + "; std="
        + peer_summary["score_std"].round(4).astype(str)
    )

    stability_checks = stability_summary.rename(columns={"sample": "check_name"}).copy()
    stability_checks["check_name"] = "stability_" + stability_checks["check_name"].astype(str)
    stability_checks["observed"] = (
        "median="
        + stability_checks["median"].round(4).astype(str)
        + "; p95="
        + stability_checks["p95"].round(4).astype(str)
        + "; p99="
        + stability_checks["p99"].round(4).astype(str)
        + "; gt1="
        + stability_checks["gt_1_share"].round(4).astype(str)
    )

    summary_frames = [
        coverage_checks,
        window_checks[["check_name", "passed", "observed", "threshold", "target_date", "selected_date", "target_gap_days"]],
        plausibility_checks,
        peer_summary[["check_name", "passed", "observed", "threshold", "window_name", "selected_date", "industries"]],
        stability_checks[["check_name", "passed", "observed", "threshold", "count", "mean", "median", "p95", "p99", "max", "gt_1_share"]],
    ]
    summary = pd.concat(summary_frames, ignore_index=True, sort=False)
    exit_gate_passed = bool(summary["passed"].astype(int).eq(1).all())
    summary = pd.concat(
        [
            summary,
            pd.DataFrame(
                [
                    {
                        "check_name": "STAGE_9_EXIT_GATE",
                        "passed": int(exit_gate_passed),
                        "observed": "PASS" if exit_gate_passed else "FAIL",
                        "threshold": "all Stage 9 acceptance diagnostics pass",
                    }
                ]
            ),
        ],
        ignore_index=True,
        sort=False,
    )

    summary_path = out_dir / "stage9_industry_macro_acceptance_summary.csv"
    windows_path = out_dir / "stage9_industry_macro_window_dates.csv"
    extremes_path = out_dir / "stage9_industry_macro_window_extremes.csv"
    peers_path = out_dir / "stage9_industry_macro_peer_groups.csv"
    stability_path = out_dir / "stage9_industry_macro_stability.csv"

    summary.to_csv(summary_path, index=False)
    window_dates.to_csv(windows_path, index=False)
    extremes.to_csv(extremes_path, index=False)
    peer_checks.to_csv(peers_path, index=False)
    stability_summary.to_csv(stability_path, index=False)

    logger.info("Stage 9 acceptance summary written to %s", summary_path)
    logger.info("Stage 9 window extremes written to %s", extremes_path)
    logger.info("Stage 9 peer checks written to %s", peers_path)
    logger.info("Stage 9 stability summary written to %s", stability_path)

    failed = summary.loc[summary["passed"].astype(int).ne(1), "check_name"].astype(str).tolist()
    if failed:
        logger.error("Stage 9 acceptance failed: %s", failed)
        return 1
    logger.info("Stage 9 acceptance passed: stable, interpretable industry-first macro map.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

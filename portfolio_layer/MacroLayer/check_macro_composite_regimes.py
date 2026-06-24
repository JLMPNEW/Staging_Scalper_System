#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from macro_raw_config import configure_pipeline_logging, connect_sqlite, load_macro_raw_config, resolve_path
from macro_serving_common import resolve_serving_db_path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WindowSpec:
    name: str
    start_date: date
    end_date: date
    description: str


WINDOW_SPECS = [
    WindowSpec("GFC_2008_2009", date(2008, 7, 1), date(2009, 6, 30), "Global financial crisis and recession trough"),
    WindowSpec("COVID_2020", date(2020, 2, 1), date(2020, 9, 30), "Pandemic shock and lockdown recession"),
    WindowSpec("INFLATION_2022", date(2022, 1, 1), date(2022, 12, 31), "Inflation shock and policy tightening"),
    # Intentionally historical: this window captures the completed 2023-2025 disinflation transition regime.
    WindowSpec("DISINFLATION_2023_2025", date(2023, 1, 1), date(2025, 12, 31), "Disinflation and post-shock growth transitions"),
]

NEGATIVE_EXTREME_COMPOSITES = {"G_NOW", "G_LEAD"}
DEFAULT_OUT_DIR = "MacroLayer/out/composite_regime_checks"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check macro composites across major regime windows.")
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--serving-db-path", type=Path, default=None, help="Optional serving SQLite path override.")
    parser.add_argument("--out-dir", type=Path, default=None, help="Optional output directory override.")
    parser.add_argument("--windows", nargs="*", default=None, help="Optional window-name filter.")
    parser.add_argument("--top-components", type=int, default=5, help="Number of component contributions to retain per composite extreme date.")
    return parser.parse_args()


def _select_windows(window_names: list[str] | None) -> list[WindowSpec]:
    if not window_names:
        return list(WINDOW_SPECS)
    wanted = {str(item).strip().upper() for item in window_names if str(item).strip()}
    out = [item for item in WINDOW_SPECS if item.name.upper() in wanted]
    if not out:
        raise ValueError(f"No regime windows matched {sorted(wanted)!r}.")
    return out


def _resolve_out_dir(config_path: Path, override: Path | None) -> Path:
    if override is not None:
        return Path(override).expanduser().resolve()
    path = resolve_path(config_path, DEFAULT_OUT_DIR)
    if path is None:
        raise ValueError("Unable to resolve composite regime check output directory.")
    return path


def _load_composite_daily(
    conn: sqlite3.Connection,
    *,
    start_date_text: str,
    end_date_text: str,
) -> pd.DataFrame:
    df = pd.read_sql_query(
        """
        SELECT
            as_of_date,
            composite_key,
            composite_value_raw,
            composite_value_smoothed,
            coverage_flag,
            available_component_count,
            expected_component_count,
            available_required_count,
            required_component_count
        FROM macro_composite_daily
        WHERE as_of_date BETWEEN ? AND ?
        ORDER BY as_of_date, composite_key
        """,
        conn,
        params=[start_date_text, end_date_text],
    )
    if df.empty:
        raise ValueError("macro_composite_daily returned no rows for the requested regime-check range.")
    df["as_of_date"] = pd.to_datetime(df["as_of_date"], errors="coerce")
    df["analysis_value"] = df["composite_value_smoothed"]
    df.loc[df["analysis_value"].isna(), "analysis_value"] = df.loc[df["analysis_value"].isna(), "composite_value_raw"]
    return df


def _window_daily(df: pd.DataFrame, window: WindowSpec) -> pd.DataFrame:
    mask = (df["as_of_date"] >= pd.Timestamp(window.start_date)) & (df["as_of_date"] <= pd.Timestamp(window.end_date))
    out = df.loc[mask].copy()
    out.insert(0, "window_name", window.name)
    out.insert(1, "window_description", window.description)
    return out


def _summarize_window(df: pd.DataFrame, window: WindowSpec) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    window_df = _window_daily(df, window)
    summary_rows: list[dict[str, object]] = []
    extreme_rows: list[dict[str, object]] = []
    for composite_key, sub in window_df.groupby("composite_key", sort=True):
        sub = sub.sort_values("as_of_date").reset_index(drop=True)
        total_days = int(len(sub))
        covered_days = int(sub["coverage_flag"].fillna(0).astype(int).sum())
        covered = sub[sub["analysis_value"].notna()].copy()
        if covered.empty:
            summary_rows.append(
                {
                    "window_name": window.name,
                    "window_description": window.description,
                    "window_start_date": window.start_date.isoformat(),
                    "window_end_date": window.end_date.isoformat(),
                    "composite_key": composite_key,
                    "total_days": total_days,
                    "covered_days": covered_days,
                    "coverage_rate": covered_days / total_days if total_days > 0 else None,
                    "mean_value": None,
                    "median_value": None,
                    "start_value": None,
                    "end_value": None,
                    "delta_value": None,
                    "min_value": None,
                    "min_date": None,
                    "max_value": None,
                    "max_date": None,
                    "extreme_date": None,
                    "extreme_direction": None,
                }
            )
            continue

        mean_value = float(covered["analysis_value"].mean())
        median_value = float(covered["analysis_value"].median())
        start_row = covered.iloc[0]
        end_row = covered.iloc[-1]
        min_idx = covered["analysis_value"].idxmin()
        max_idx = covered["analysis_value"].idxmax()
        min_row = covered.loc[min_idx]
        max_row = covered.loc[max_idx]

        if composite_key in NEGATIVE_EXTREME_COMPOSITES:
            extreme_row = min_row
            extreme_direction = "trough"
        else:
            extreme_row = max_row
            extreme_direction = "peak"

        summary_rows.append(
            {
                "window_name": window.name,
                "window_description": window.description,
                "window_start_date": window.start_date.isoformat(),
                "window_end_date": window.end_date.isoformat(),
                "composite_key": composite_key,
                "total_days": total_days,
                "covered_days": covered_days,
                "coverage_rate": covered_days / total_days if total_days > 0 else None,
                "mean_value": mean_value,
                "median_value": median_value,
                "start_value": float(start_row["analysis_value"]),
                "end_value": float(end_row["analysis_value"]),
                "delta_value": float(end_row["analysis_value"] - start_row["analysis_value"]),
                "min_value": float(min_row["analysis_value"]),
                "min_date": pd.Timestamp(min_row["as_of_date"]).date().isoformat(),
                "max_value": float(max_row["analysis_value"]),
                "max_date": pd.Timestamp(max_row["as_of_date"]).date().isoformat(),
                "extreme_date": pd.Timestamp(extreme_row["as_of_date"]).date().isoformat(),
                "extreme_direction": extreme_direction,
            }
        )
        extreme_rows.append(
            {
                "window_name": window.name,
                "window_description": window.description,
                "composite_key": composite_key,
                "extreme_date": pd.Timestamp(extreme_row["as_of_date"]).date().isoformat(),
                "extreme_direction": extreme_direction,
            }
        )
    return summary_rows, extreme_rows


def _summary_lookup(summary_df: pd.DataFrame, *, window_name: str, composite_key: str, field_name: str) -> float | None:
    match = summary_df[
        (summary_df["window_name"] == window_name)
        & (summary_df["composite_key"] == composite_key)
    ]
    if match.empty:
        return None
    value = match.iloc[0][field_name]
    if pd.isna(value):
        return None
    return float(value)


def _comparison_checks(summary_df: pd.DataFrame) -> pd.DataFrame:
    checks: list[dict[str, object]] = []

    def add_check(name: str, description: str, observed: float | None, expected: str, passed: bool | None) -> None:
        checks.append(
            {
                "check_name": name,
                "description": description,
                "observed_value": observed,
                "expected_condition": expected,
                "status": "PASS" if passed is True else ("WARN" if passed is False else "INFO"),
            }
        )

    gfc_g_now = _summary_lookup(summary_df, window_name="GFC_2008_2009", composite_key="G_NOW", field_name="mean_value")
    add_check(
        "gfc_growth_now_negative",
        "G_NOW should be negative on average during the 2008-2009 crisis window.",
        gfc_g_now,
        "mean_value < 0",
        None if gfc_g_now is None else bool(gfc_g_now < 0.0),
    )

    gfc_g_lead = _summary_lookup(summary_df, window_name="GFC_2008_2009", composite_key="G_LEAD", field_name="mean_value")
    add_check(
        "gfc_growth_lead_negative",
        "G_LEAD should be negative on average during the 2008-2009 crisis window.",
        gfc_g_lead,
        "mean_value < 0",
        None if gfc_g_lead is None else bool(gfc_g_lead < 0.0),
    )

    gfc_shock = _summary_lookup(summary_df, window_name="GFC_2008_2009", composite_key="SHOCK", field_name="max_value")
    add_check(
        "gfc_shock_spike",
        "SHOCK should register a positive spike during the 2008-2009 crisis window.",
        gfc_shock,
        "max_value > 0.5",
        None if gfc_shock is None else bool(gfc_shock > 0.5),
    )

    covid_g_now = _summary_lookup(summary_df, window_name="COVID_2020", composite_key="G_NOW", field_name="mean_value")
    add_check(
        "covid_growth_now_negative",
        "G_NOW should be negative on average during the 2020 pandemic shock window.",
        covid_g_now,
        "mean_value < 0",
        None if covid_g_now is None else bool(covid_g_now < 0.0),
    )

    covid_g_lead = _summary_lookup(summary_df, window_name="COVID_2020", composite_key="G_LEAD", field_name="mean_value")
    add_check(
        "covid_growth_lead_negative",
        "G_LEAD should be negative on average during the 2020 pandemic shock window.",
        covid_g_lead,
        "mean_value < 0",
        None if covid_g_lead is None else bool(covid_g_lead < 0.0),
    )

    covid_shock = _summary_lookup(summary_df, window_name="COVID_2020", composite_key="SHOCK", field_name="max_value")
    add_check(
        "covid_shock_spike",
        "SHOCK should register a positive spike during the 2020 pandemic shock window.",
        covid_shock,
        "max_value > 0.5",
        None if covid_shock is None else bool(covid_shock > 0.5),
    )

    inf22_pi_now = _summary_lookup(summary_df, window_name="INFLATION_2022", composite_key="PI_NOW", field_name="mean_value")
    add_check(
        "inflation_2022_pi_now_positive",
        "PI_NOW should be positive on average during the 2022 inflation shock.",
        inf22_pi_now,
        "mean_value > 0",
        None if inf22_pi_now is None else bool(inf22_pi_now > 0.0),
    )

    inf22_pi_lead = _summary_lookup(summary_df, window_name="INFLATION_2022", composite_key="PI_LEAD", field_name="max_value")
    add_check(
        "inflation_2022_pi_lead_spike",
        "PI_LEAD should register a strong positive spike during the 2022 inflation shock.",
        inf22_pi_lead,
        "max_value > 0.5",
        None if inf22_pi_lead is None else bool(inf22_pi_lead > 0.5),
    )

    disinflation_pi_now = _summary_lookup(summary_df, window_name="DISINFLATION_2023_2025", composite_key="PI_NOW", field_name="mean_value")
    add_check(
        "disinflation_pi_now_below_2022",
        "PI_NOW should average below its 2022 level during 2023-2025 disinflation.",
        None if disinflation_pi_now is None or inf22_pi_now is None else float(disinflation_pi_now - inf22_pi_now),
        "PI_NOW(2023-2025) < PI_NOW(2022)",
        None if disinflation_pi_now is None or inf22_pi_now is None else bool(disinflation_pi_now < inf22_pi_now),
    )

    disinflation_pi_lead_peak = _summary_lookup(summary_df, window_name="DISINFLATION_2023_2025", composite_key="PI_LEAD", field_name="max_value")
    add_check(
        "disinflation_pi_lead_peak_below_2022",
        "PI_LEAD peak should sit below its 2022 peak during 2023-2025 disinflation.",
        None if disinflation_pi_lead_peak is None or inf22_pi_lead is None else float(disinflation_pi_lead_peak - inf22_pi_lead),
        "PI_LEAD max(2023-2025) < PI_LEAD max(2022)",
        None if disinflation_pi_lead_peak is None or inf22_pi_lead is None else bool(disinflation_pi_lead_peak < inf22_pi_lead),
    )

    disinflation_g_lead_start = _summary_lookup(summary_df, window_name="DISINFLATION_2023_2025", composite_key="G_LEAD", field_name="start_value")
    disinflation_g_lead_end = _summary_lookup(summary_df, window_name="DISINFLATION_2023_2025", composite_key="G_LEAD", field_name="end_value")
    add_check(
        "disinflation_growth_lead_improves",
        "G_LEAD should improve between the start and end of the 2023-2025 transition window.",
        None if disinflation_g_lead_start is None or disinflation_g_lead_end is None else float(disinflation_g_lead_end - disinflation_g_lead_start),
        "G_LEAD end_value > start_value",
        None if disinflation_g_lead_start is None or disinflation_g_lead_end is None else bool(disinflation_g_lead_end > disinflation_g_lead_start),
    )

    return pd.DataFrame(checks)


def _load_top_components(
    conn: sqlite3.Connection,
    *,
    extreme_df: pd.DataFrame,
    top_components: int,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for _, item in extreme_df.iterrows():
        q = conn.execute(
            """
            SELECT
                metric_key,
                feature_name,
                contribution_value,
                normalized_weight,
                standardized_value,
                included_flag
            FROM macro_composite_component_daily
            WHERE as_of_date = ?
              AND composite_key = ?
              AND included_flag = 1
            ORDER BY ABS(contribution_value) DESC, metric_key
            LIMIT ?
            """,
            (str(item["extreme_date"]), str(item["composite_key"]), int(top_components)),
        )
        for rank, row in enumerate(q.fetchall(), start=1):
            rows.append(
                {
                    "window_name": str(item["window_name"]),
                    "window_description": str(item["window_description"]),
                    "composite_key": str(item["composite_key"]),
                    "extreme_date": str(item["extreme_date"]),
                    "extreme_direction": str(item["extreme_direction"]),
                    "component_rank": rank,
                    "metric_key": str(row["metric_key"]),
                    "feature_name": str(row["feature_name"]),
                    "contribution_value": float(row["contribution_value"]) if row["contribution_value"] is not None else None,
                    "normalized_weight": float(row["normalized_weight"]) if row["normalized_weight"] is not None else None,
                    "standardized_value": float(row["standardized_value"]) if row["standardized_value"] is not None else None,
                }
            )
    return pd.DataFrame(rows)


def _write_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, cfg = load_macro_raw_config(args.config)
    serving_db_path = resolve_serving_db_path(cfg, config_path, override=args.serving_db_path)
    out_dir = _resolve_out_dir(config_path, args.out_dir)
    windows = _select_windows(args.windows)

    start_date_text = min(item.start_date for item in windows).isoformat()
    end_date_text = max(item.end_date for item in windows).isoformat()

    conn = connect_sqlite(serving_db_path, row_factory=sqlite3.Row)
    try:
        daily_df = _load_composite_daily(
            conn,
            start_date_text=start_date_text,
            end_date_text=end_date_text,
        )

        window_daily_frames: list[pd.DataFrame] = []
        summary_rows: list[dict[str, object]] = []
        extreme_rows: list[dict[str, object]] = []
        for window in windows:
            summary_part, extreme_part = _summarize_window(daily_df, window)
            summary_rows.extend(summary_part)
            extreme_rows.extend(extreme_part)
            window_daily_frames.append(_window_daily(daily_df, window))

        summary_df = pd.DataFrame(summary_rows).sort_values(["window_start_date", "composite_key"]).reset_index(drop=True)
        extremes_df = pd.DataFrame(extreme_rows).sort_values(["window_name", "composite_key"]).reset_index(drop=True)
        checks_df = _comparison_checks(summary_df)
        window_daily_df = pd.concat(window_daily_frames, ignore_index=True, sort=False)
        component_df = _load_top_components(
            conn,
            extreme_df=extremes_df,
            top_components=max(1, int(args.top_components)),
        )

        latest_date_row = conn.execute("SELECT MAX(as_of_date) AS max_as_of_date FROM macro_composite_daily").fetchone()
        latest_date = str(latest_date_row["max_as_of_date"] or "")

        summary_path = out_dir / "macro_composite_regime_window_summary.csv"
        checks_path = out_dir / "macro_composite_regime_checks.csv"
        daily_path = out_dir / "macro_composite_regime_window_daily.csv"
        component_path = out_dir / "macro_composite_regime_extreme_components.csv"
        _write_csv(summary_df, summary_path)
        _write_csv(checks_df, checks_path)
        _write_csv(window_daily_df, daily_path)
        _write_csv(component_df, component_path)

        logger.info("Composite regime check latest available as_of_date=%s", latest_date)
        for window in windows:
            logger.info("%s | %s | %s -> %s", window.name, window.description, window.start_date.isoformat(), window.end_date.isoformat())
            sub = summary_df[summary_df["window_name"] == window.name]
            for _, row in sub.iterrows():
                logger.info(
                    "  %s | mean=%.3f | start=%.3f | end=%.3f | min=%.3f (%s) | max=%.3f (%s) | coverage=%.2f",
                    str(row["composite_key"]),
                    float(row["mean_value"]) if pd.notna(row["mean_value"]) else float("nan"),
                    float(row["start_value"]) if pd.notna(row["start_value"]) else float("nan"),
                    float(row["end_value"]) if pd.notna(row["end_value"]) else float("nan"),
                    float(row["min_value"]) if pd.notna(row["min_value"]) else float("nan"),
                    str(row["min_date"] or ""),
                    float(row["max_value"]) if pd.notna(row["max_value"]) else float("nan"),
                    str(row["max_date"] or ""),
                    float(row["coverage_rate"]) if pd.notna(row["coverage_rate"]) else float("nan"),
                )

        for _, row in checks_df.iterrows():
            logger.info(
                "CHECK %s | status=%s | observed=%s | expected=%s",
                str(row["check_name"]),
                str(row["status"]),
                "" if pd.isna(row["observed_value"]) else f"{float(row['observed_value']):.3f}",
                str(row["expected_condition"]),
            )

        logger.info("Wrote regime summaries to %s", summary_path)
        logger.info("Wrote regime checks to %s", checks_path)
        logger.info("Wrote regime daily slices to %s", daily_path)
        logger.info("Wrote extreme component contributions to %s", component_path)
    finally:
        conn.close()


if __name__ == "__main__":
    main()

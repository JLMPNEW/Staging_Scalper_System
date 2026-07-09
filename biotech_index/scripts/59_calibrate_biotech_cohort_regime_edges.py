#!/usr/bin/env python3
# pyright: reportArgumentType=false, reportCallIssue=false, reportReturnType=false, reportAttributeAccessIssue=false
"""Diagnose biotech cohort/regime edge using existing clean calibration files.

This script intentionally does not rebuild features, scores, or historical
daily CSVs.  It consumes the clean Stage 11 calibration artifacts that already
exist and answers three questions:

1. Did selected tickers outperform their own cohort, not just XBI?
2. Which cohorts and XBI regimes carry or destroy the edge?
3. Would a cohort-specific top-k or train-learned no-trade gate have improved
   the holdout profile?
"""

from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SEQUENCE_DIR = PROJECT_ROOT / "output" / "biotech_index_reports" / "clean_historical_sequence_20190104_20260702"
DEFAULT_CALIBRATION_DIR = DEFAULT_SEQUENCE_DIR / "candidate_calibration"
DEFAULT_OBSERVATIONS = DEFAULT_CALIBRATION_DIR / "_progress" / "tier1_observations_with_forward_returns.csv"
DEFAULT_SELECTED = DEFAULT_CALIBRATION_DIR / "tier1_selected_ticker_diagnostics.csv"

HORIZONS = (20, 60, 120)
OBS_RETURN_COLUMNS = {
    "absolute": "fwd_{horizon}d_net_return",
    "xbi_alpha": "fwd_{horizon}d_net_benchmark_alpha_return",
    "equal_weight_alpha": "fwd_{horizon}d_net_equal_weight_alpha_return",
}
SELECTED_RETURN_COLUMNS = {
    "absolute": "net_forward_return_pct",
    "xbi_alpha": "net_benchmark_alpha_return_pct",
    "equal_weight_alpha": "net_equal_weight_alpha_return_pct",
}
SELECTED_BASE_COLUMNS = [
    "sample",
    "evaluation_split",
    "horizon_days",
    "top_n",
    "candidate_name",
    "selection_policy_name",
    "asof_date",
    "selected_rank_within_date",
    "ticker",
    "company_name",
    "profile_name",
    "candidate_selection_score",
    "benchmark_forward_return_pct",
    *SELECTED_RETURN_COLUMNS.values(),
]
KEY_COLUMNS = ["sample", "horizon_days", "top_n", "candidate_name", "selection_policy_name"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build biotech cohort/regime edge diagnostics from existing Stage 11 calibration artifacts."
    )
    parser.add_argument("--calibration-dir", type=Path, default=DEFAULT_CALIBRATION_DIR)
    parser.add_argument(
        "--observations",
        type=Path,
        default=None,
        help="Observation cache CSV. Defaults to <calibration-dir>/_progress/tier1_observations_with_forward_returns.csv.",
    )
    parser.add_argument(
        "--selected",
        type=Path,
        default=None,
        help="Selected ticker diagnostics CSV. Defaults to <calibration-dir>/tier1_selected_ticker_diagnostics.csv.",
    )
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--top-k", default="1,3,5,10")
    parser.add_argument("--lcb-z", type=float, default=1.0)
    parser.add_argument("--min-train-dates", type=int, default=25)
    parser.add_argument("--allow-lcb-threshold", type=float, default=0.0)
    parser.add_argument(
        "--forced-allowed-cohorts",
        default="",
        help=(
            "Optional comma/pipe/semicolon-separated cohort list to simulate as an explicit no-trade/cash gate. "
            "This is diagnostic-only and does not alter production scoring."
        ),
    )
    return parser.parse_args()


def filesystem_path(path: Path) -> str:
    text = str(path.resolve())
    if os.name == "nt" and not text.startswith("\\\\?\\"):
        return "\\\\?\\" + text
    return text


def parse_top_k(raw: str) -> list[int]:
    out: list[int] = []
    for item in str(raw or "").split(","):
        text = item.strip()
        if not text:
            continue
        value = int(text)
        if value <= 0:
            raise ValueError("--top-k values must be positive integers")
        out.append(value)
    if not out:
        raise ValueError("--top-k did not contain any positive integers")
    return sorted(set(out))


def parse_cohort_list(raw: str) -> list[str]:
    tokens = str(raw or "").replace("|", ",").replace(";", ",").split(",")
    out = sorted({token.strip() for token in tokens if token.strip()})
    return out


def write_df(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(filesystem_path(path), index=False, lineterminator="\n")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(filesystem_path(path), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def available_columns(path: Path) -> list[str]:
    return list(pd.read_csv(filesystem_path(path), nrows=0).columns)


def numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def as_percent_from_decimal(series: pd.Series) -> pd.Series:
    return numeric(series) * 100.0


def add_regime_columns(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    dates = pd.to_datetime(out["asof_date"], errors="coerce")
    out["asof_year"] = dates.dt.year.astype("Int64").astype(str).replace("<NA>", "")
    benchmark = numeric(out.get("benchmark_forward_return_pct", pd.Series(index=out.index, dtype="float64")))
    conditions = [
        benchmark <= -10.0,
        (benchmark > -10.0) & (benchmark < 0.0),
        (benchmark >= 0.0) & (benchmark < 10.0),
        benchmark >= 10.0,
    ]
    labels = ["xbi_sharp_down", "xbi_down", "xbi_up", "xbi_strong_up"]
    out["xbi_forward_regime"] = "unknown"
    for mask, label in zip(conditions, labels, strict=True):
        out.loc[mask.fillna(False), "xbi_forward_regime"] = label
    return out


def grouped_stats(frame: pd.DataFrame, group_cols: list[str], value_col: str, prefix: str, lcb_z: float) -> pd.DataFrame:
    if frame.empty or value_col not in frame.columns:
        return pd.DataFrame(columns=group_cols)
    working = frame[group_cols + [value_col]].copy()
    working[value_col] = numeric(working[value_col])
    working = working[working[value_col].notna()]
    if working.empty:
        return pd.DataFrame(columns=group_cols)
    working["_positive"] = (working[value_col] > 0.0).astype(float)
    working["_loss20"] = (working[value_col] <= -20.0).astype(float)
    working["_loss40"] = (working[value_col] <= -40.0).astype(float)
    grouped = working.groupby(group_cols, dropna=False)
    out = grouped.agg(
        **{
            f"{prefix}_n": (value_col, "count"),
            f"{prefix}_mean_pct": (value_col, "mean"),
            f"{prefix}_median_pct": (value_col, "median"),
            f"{prefix}_stdev_pct": (value_col, "std"),
            f"{prefix}_best_pct": (value_col, "max"),
            f"{prefix}_worst_pct": (value_col, "min"),
            f"{prefix}_hit_rate_pct": ("_positive", "mean"),
            f"{prefix}_loss20_rate_pct": ("_loss20", "mean"),
            f"{prefix}_loss40_rate_pct": ("_loss40", "mean"),
        }
    ).reset_index()
    n = numeric(out[f"{prefix}_n"])
    stdev = numeric(out[f"{prefix}_stdev_pct"]).fillna(0.0)
    mean = numeric(out[f"{prefix}_mean_pct"])
    out[f"{prefix}_lcb_pct"] = mean - max(0.0, lcb_z) * stdev / n.pow(0.5)
    for col in [f"{prefix}_hit_rate_pct", f"{prefix}_loss20_rate_pct", f"{prefix}_loss40_rate_pct"]:
        out[col] = numeric(out[col]) * 100.0
    numeric_cols = [col for col in out.columns if col not in group_cols]
    out[numeric_cols] = out[numeric_cols].round(6)
    return out


def merge_stats(
    frame: pd.DataFrame,
    *,
    group_cols: list[str],
    stats: list[tuple[str, str]],
    lcb_z: float,
) -> pd.DataFrame:
    out: pd.DataFrame | None = None
    for value_col, prefix in stats:
        piece = grouped_stats(frame, group_cols, value_col, prefix, lcb_z)
        out = piece if out is None else out.merge(piece, on=group_cols, how="outer")
    return pd.DataFrame(columns=group_cols) if out is None else out


def load_observation_returns(path: Path) -> pd.DataFrame:
    header = set(available_columns(path))
    required = {"asof_date", "ticker", "biotech_primary_cohort"}
    missing = sorted(required - header)
    if missing:
        raise ValueError(f"{path} is missing required observation columns: {missing}")
    usecols = sorted(required | {template.format(horizon=h) for template in OBS_RETURN_COLUMNS.values() for h in HORIZONS})
    usecols = [col for col in usecols if col in header]
    raw = pd.read_csv(filesystem_path(path), usecols=usecols, dtype="string")
    raw["asof_date"] = raw["asof_date"].astype("string").str.strip()
    raw["ticker"] = raw["ticker"].astype("string").str.upper().str.strip()
    raw["biotech_primary_cohort"] = raw["biotech_primary_cohort"].fillna("").astype("string").str.strip()
    parts: list[pd.DataFrame] = []
    for horizon in HORIZONS:
        part = raw[["asof_date", "ticker", "biotech_primary_cohort"]].copy()
        part["horizon_days"] = horizon
        for basis, template in OBS_RETURN_COLUMNS.items():
            column = template.format(horizon=horizon)
            part[f"universe_{basis}_return_pct"] = (
                as_percent_from_decimal(raw[column]) if column in raw.columns else pd.Series(math.nan, index=raw.index)
            )
        parts.append(part)
    out = pd.concat(parts, ignore_index=True)
    return out.dropna(subset=["universe_absolute_return_pct", "universe_xbi_alpha_return_pct"], how="all")


def build_cohort_baselines(observations: pd.DataFrame) -> pd.DataFrame:
    group_cols = ["asof_date", "horizon_days", "biotech_primary_cohort"]
    grouped = observations.groupby(group_cols, dropna=False)
    out = grouped.agg(
        same_cohort_n=("ticker", "count"),
        same_cohort_mean_return_pct=("universe_absolute_return_pct", "mean"),
        same_cohort_median_return_pct=("universe_absolute_return_pct", "median"),
        same_cohort_mean_xbi_alpha_pct=("universe_xbi_alpha_return_pct", "mean"),
        same_cohort_median_xbi_alpha_pct=("universe_xbi_alpha_return_pct", "median"),
        same_cohort_mean_equal_weight_alpha_pct=("universe_equal_weight_alpha_return_pct", "mean"),
    ).reset_index()
    return out.round(6)


def load_selected(path: Path, observations: pd.DataFrame, baselines: pd.DataFrame) -> pd.DataFrame:
    header = set(available_columns(path))
    usecols = [col for col in SELECTED_BASE_COLUMNS if col in header]
    missing = sorted({"sample", "evaluation_split", "horizon_days", "top_n", "candidate_name", "selection_policy_name", "asof_date", "ticker"} - set(usecols))
    if missing:
        raise ValueError(f"{path} is missing required selected-diagnostic columns: {missing}")
    raw = pd.read_csv(filesystem_path(path), usecols=usecols, dtype="string")
    raw["asof_date"] = raw["asof_date"].astype("string").str.strip()
    raw["ticker"] = raw["ticker"].astype("string").str.upper().str.strip()
    raw["horizon_days"] = numeric(raw["horizon_days"]).astype("Int64")
    raw["top_n"] = numeric(raw["top_n"]).astype("Int64")
    raw["selected_rank_within_date"] = numeric(raw.get("selected_rank_within_date", pd.Series(index=raw.index)))
    raw["candidate_selection_score"] = numeric(raw.get("candidate_selection_score", pd.Series(index=raw.index)))
    for column in SELECTED_RETURN_COLUMNS.values():
        if column in raw.columns:
            raw[column] = numeric(raw[column])
    if "benchmark_forward_return_pct" in raw.columns:
        raw["benchmark_forward_return_pct"] = numeric(raw["benchmark_forward_return_pct"])

    cohort_map = observations[["asof_date", "ticker", "horizon_days", "biotech_primary_cohort"]].drop_duplicates()
    selected = raw.merge(cohort_map, on=["asof_date", "ticker", "horizon_days"], how="left")
    selected["biotech_primary_cohort"] = selected["biotech_primary_cohort"].fillna("unknown")
    selected = selected.merge(baselines, on=["asof_date", "horizon_days", "biotech_primary_cohort"], how="left")
    selected["selected_absolute_return_pct"] = numeric(selected.get("net_forward_return_pct", pd.Series(index=selected.index)))
    selected["selected_xbi_alpha_pct"] = numeric(
        selected.get("net_benchmark_alpha_return_pct", pd.Series(index=selected.index))
    )
    selected["selected_equal_weight_alpha_pct"] = numeric(
        selected.get("net_equal_weight_alpha_return_pct", pd.Series(index=selected.index))
    )
    selected["same_cohort_alpha_mean_pct"] = (
        selected["selected_absolute_return_pct"] - numeric(selected["same_cohort_mean_return_pct"])
    )
    selected["same_cohort_alpha_median_pct"] = (
        selected["selected_absolute_return_pct"] - numeric(selected["same_cohort_median_return_pct"])
    )
    selected["same_cohort_xbi_alpha_mean_excess_pct"] = (
        selected["selected_xbi_alpha_pct"] - numeric(selected["same_cohort_mean_xbi_alpha_pct"])
    )
    selected = add_regime_columns(selected)
    rank_cols = [
        "sample",
        "evaluation_split",
        "horizon_days",
        "top_n",
        "candidate_name",
        "selection_policy_name",
        "asof_date",
        "biotech_primary_cohort",
    ]
    selected = selected.sort_values(
        rank_cols + ["candidate_selection_score", "selected_rank_within_date"],
        ascending=[True, True, True, True, True, True, True, True, False, True],
        na_position="last",
    )
    selected["cohort_rank_within_date"] = selected.groupby(rank_cols, dropna=False).cumcount() + 1
    return selected


def build_same_cohort_outputs(selected: pd.DataFrame, lcb_z: float) -> dict[str, pd.DataFrame]:
    candidate_group_cols = [
        "sample",
        "evaluation_split",
        "horizon_days",
        "top_n",
        "candidate_name",
        "selection_policy_name",
        "biotech_primary_cohort",
    ]
    regime_group_cols = candidate_group_cols + ["asof_year", "xbi_forward_regime"]
    stats = [
        ("selected_absolute_return_pct", "selected_abs"),
        ("selected_xbi_alpha_pct", "selected_xbi_alpha"),
        ("selected_equal_weight_alpha_pct", "selected_equal_weight_alpha"),
        ("same_cohort_alpha_mean_pct", "same_cohort_alpha_mean"),
        ("same_cohort_alpha_median_pct", "same_cohort_alpha_median"),
        ("same_cohort_xbi_alpha_mean_excess_pct", "same_cohort_xbi_alpha_excess"),
    ]
    return {
        "same_cohort_alpha_by_candidate.csv": merge_stats(
            selected, group_cols=candidate_group_cols, stats=stats, lcb_z=lcb_z
        ).sort_values(candidate_group_cols),
        "same_cohort_alpha_by_regime.csv": merge_stats(
            selected, group_cols=regime_group_cols, stats=stats, lcb_z=lcb_z
        ).sort_values(regime_group_cols),
    }


def topk_date_level(selected: pd.DataFrame, top_k_values: list[int]) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    date_group_cols = [
        "sample",
        "evaluation_split",
        "horizon_days",
        "top_n",
        "candidate_name",
        "selection_policy_name",
        "biotech_primary_cohort",
        "asof_date",
    ]
    for top_k in top_k_values:
        subset = selected[selected["cohort_rank_within_date"] <= top_k].copy()
        if subset.empty:
            continue
        grouped = subset.groupby(date_group_cols, dropna=False).agg(
            date_selected_count=("ticker", "count"),
            date_selected_abs_return_pct=("selected_absolute_return_pct", "mean"),
            date_selected_xbi_alpha_pct=("selected_xbi_alpha_pct", "mean"),
            date_same_cohort_alpha_mean_pct=("same_cohort_alpha_mean_pct", "mean"),
            date_same_cohort_alpha_median_pct=("same_cohort_alpha_median_pct", "mean"),
        ).reset_index()
        grouped["cohort_top_k"] = top_k
        parts.append(grouped)
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True)


def build_topk_outputs(selected: pd.DataFrame, top_k_values: list[int], lcb_z: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    date_level = topk_date_level(selected, top_k_values)
    if date_level.empty:
        return pd.DataFrame(), pd.DataFrame()
    group_cols = [
        "sample",
        "evaluation_split",
        "horizon_days",
        "top_n",
        "candidate_name",
        "selection_policy_name",
        "biotech_primary_cohort",
        "cohort_top_k",
    ]
    stats = [
        ("date_selected_abs_return_pct", "date_abs"),
        ("date_selected_xbi_alpha_pct", "date_xbi_alpha"),
        ("date_same_cohort_alpha_mean_pct", "date_same_cohort_alpha"),
        ("date_same_cohort_alpha_median_pct", "date_same_cohort_alpha_median"),
    ]
    topk_summary = merge_stats(date_level, group_cols=group_cols, stats=stats, lcb_z=lcb_z).sort_values(group_cols)
    return date_level, topk_summary


def stats_from_values(values: pd.Series, prefix: str, lcb_z: float) -> dict[str, Any]:
    clean = numeric(values).dropna()
    if clean.empty:
        return {
            f"{prefix}_n": 0,
            f"{prefix}_mean_pct": "",
            f"{prefix}_lcb_pct": "",
            f"{prefix}_hit_rate_pct": "",
            f"{prefix}_loss20_rate_pct": "",
        }
    mean = float(clean.mean())
    stdev = float(clean.std()) if len(clean) > 1 else 0.0
    lcb = mean - max(0.0, lcb_z) * stdev / math.sqrt(float(len(clean)))
    return {
        f"{prefix}_n": int(len(clean)),
        f"{prefix}_mean_pct": round(mean, 6),
        f"{prefix}_lcb_pct": round(lcb, 6),
        f"{prefix}_hit_rate_pct": round(float((clean > 0.0).mean() * 100.0), 6),
        f"{prefix}_loss20_rate_pct": round(float((clean <= -20.0).mean() * 100.0), 6),
    }


def build_cash_gate_outputs(
    selected: pd.DataFrame,
    date_level: pd.DataFrame,
    *,
    top_k_values: list[int],
    min_train_dates: int,
    allow_lcb_threshold: float,
    lcb_z: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if date_level.empty:
        return pd.DataFrame(), pd.DataFrame()
    gate_group_cols = KEY_COLUMNS + ["biotech_primary_cohort", "cohort_top_k"]
    train = date_level[date_level["evaluation_split"] == "train"].copy()
    train_stats = merge_stats(
        train,
        group_cols=gate_group_cols,
        stats=[("date_same_cohort_alpha_mean_pct", "train_same_cohort_alpha")],
        lcb_z=lcb_z,
    )
    if train_stats.empty:
        return pd.DataFrame(), pd.DataFrame()
    train_stats["cohort_allowed"] = (
        (numeric(train_stats["train_same_cohort_alpha_n"]) >= float(min_train_dates))
        & (numeric(train_stats["train_same_cohort_alpha_lcb_pct"]) > float(allow_lcb_threshold))
    )
    gate_rows = train_stats.copy()

    rows: list[dict[str, Any]] = []
    selected_dates = (
        selected[KEY_COLUMNS + ["evaluation_split", "asof_date"]]
        .drop_duplicates()
        .groupby(KEY_COLUMNS + ["evaluation_split"], dropna=False)["asof_date"]
        .apply(list)
        .reset_index()
    )
    date_lookup = {
        tuple(row[col] for col in KEY_COLUMNS + ["evaluation_split"]): list(row["asof_date"])
        for _, row in selected_dates.iterrows()
    }
    for top_k in top_k_values:
        allowed = gate_rows[(gate_rows["cohort_top_k"] == top_k) & (gate_rows["cohort_allowed"])].copy()
        allowed_map = (
            allowed.groupby(KEY_COLUMNS, dropna=False)["biotech_primary_cohort"].apply(lambda items: sorted(set(items))).to_dict()
        )
        for key_values, all_dates in date_lookup.items():
            key = key_values[:-1]
            split = key_values[-1]
            allowed_cohorts = allowed_map.get(key, [])
            mask = date_level["cohort_top_k"].eq(top_k)
            for col, value in zip(KEY_COLUMNS, key, strict=True):
                mask &= date_level[col].eq(value)
            mask &= date_level["evaluation_split"].eq(split)
            if allowed_cohorts:
                mask &= date_level["biotech_primary_cohort"].isin(allowed_cohorts)
            else:
                mask &= False
            included = date_level[mask]
            by_date = included.groupby("asof_date", dropna=False).agg(
                date_abs=("date_selected_abs_return_pct", "mean"),
                date_xbi_alpha=("date_selected_xbi_alpha_pct", "mean"),
                date_same_cohort_alpha=("date_same_cohort_alpha_mean_pct", "mean"),
            )
            all_date_index = pd.Index(sorted(set(all_dates)), name="asof_date")
            by_date = by_date.reindex(all_date_index).fillna(0.0)
            row: dict[str, Any] = {
                **dict(zip(KEY_COLUMNS, key, strict=True)),
                "evaluation_split": split,
                "cohort_top_k": top_k,
                "allowed_cohort_count": len(allowed_cohorts),
                "allowed_cohorts": "|".join(allowed_cohorts),
                "candidate_dates": len(all_date_index),
                "traded_dates": int((by_date.abs().sum(axis=1) != 0.0).sum()),
                "cash_dates": int((by_date.abs().sum(axis=1) == 0.0).sum()),
            }
            row.update(stats_from_values(by_date["date_abs"], "cash_gate_abs", lcb_z))
            row.update(stats_from_values(by_date["date_xbi_alpha"], "cash_gate_xbi_alpha", lcb_z))
            row.update(stats_from_values(by_date["date_same_cohort_alpha"], "cash_gate_same_cohort_alpha", lcb_z))
            rows.append(row)
    cash_gate = pd.DataFrame(rows)
    if not cash_gate.empty:
        cash_gate = cash_gate.sort_values(KEY_COLUMNS + ["cohort_top_k", "evaluation_split"])
    return gate_rows.sort_values(gate_group_cols), cash_gate


def build_forced_cohort_gate_outputs(
    selected: pd.DataFrame,
    date_level: pd.DataFrame,
    *,
    top_k_values: list[int],
    forced_allowed_cohorts: list[str],
    lcb_z: float,
) -> pd.DataFrame:
    """Simulate an explicit cohort-only cash gate without retraining scores.

    This intentionally complements the train-learned gate above. It answers:
    "what if we only traded this cohort and held cash on all other dates?"
    """
    if date_level.empty or not forced_allowed_cohorts:
        return pd.DataFrame()

    selected_dates = (
        selected[KEY_COLUMNS + ["evaluation_split", "asof_date"]]
        .drop_duplicates()
        .groupby(KEY_COLUMNS + ["evaluation_split"], dropna=False)["asof_date"]
        .apply(list)
        .reset_index()
    )
    date_lookup = {
        tuple(row[col] for col in KEY_COLUMNS + ["evaluation_split"]): list(row["asof_date"])
        for _, row in selected_dates.iterrows()
    }

    rows: list[dict[str, Any]] = []
    allowed_label = "|".join(forced_allowed_cohorts)
    for top_k in top_k_values:
        for key_values, all_dates in date_lookup.items():
            key = key_values[:-1]
            split = key_values[-1]
            mask = date_level["cohort_top_k"].eq(top_k)
            for col, value in zip(KEY_COLUMNS, key, strict=True):
                mask &= date_level[col].eq(value)
            mask &= date_level["evaluation_split"].eq(split)
            mask &= date_level["biotech_primary_cohort"].isin(forced_allowed_cohorts)

            included = date_level[mask]
            by_date = included.groupby("asof_date", dropna=False).agg(
                date_selected_count=("date_selected_count", "sum"),
                date_abs=("date_selected_abs_return_pct", "mean"),
                date_xbi_alpha=("date_selected_xbi_alpha_pct", "mean"),
                date_same_cohort_alpha=("date_same_cohort_alpha_mean_pct", "mean"),
            )
            all_date_index = pd.Index(sorted(set(all_dates)), name="asof_date")
            by_date = pd.DataFrame(by_date.reindex(all_date_index))
            by_date["date_selected_count"] = numeric(by_date.get("date_selected_count", pd.Series(dtype="float64"))).fillna(0.0)
            for column in ["date_abs", "date_xbi_alpha", "date_same_cohort_alpha"]:
                by_date[column] = numeric(by_date.get(column, pd.Series(dtype="float64"))).fillna(0.0)
            traded_dates = int((by_date["date_selected_count"] > 0.0).sum())
            row: dict[str, Any] = {
                **dict(zip(KEY_COLUMNS, key, strict=True)),
                "evaluation_split": split,
                "cohort_top_k": top_k,
                "gate_type": "forced_allowed_cohorts",
                "allowed_cohort_count": len(forced_allowed_cohorts),
                "allowed_cohorts": allowed_label,
                "candidate_dates": len(all_date_index),
                "traded_dates": traded_dates,
                "cash_dates": int(len(all_date_index) - traded_dates),
                "trade_rate_pct": round(100.0 * traded_dates / len(all_date_index), 6) if len(all_date_index) else 0.0,
            }
            row.update(stats_from_values(by_date["date_abs"], "cash_gate_abs", lcb_z))
            row.update(stats_from_values(by_date["date_xbi_alpha"], "cash_gate_xbi_alpha", lcb_z))
            row.update(stats_from_values(by_date["date_same_cohort_alpha"], "cash_gate_same_cohort_alpha", lcb_z))
            rows.append(row)

    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(KEY_COLUMNS + ["cohort_top_k", "evaluation_split"])
    return out


def build_recommendations(cash_gate: pd.DataFrame) -> pd.DataFrame:
    if cash_gate.empty:
        return pd.DataFrame()
    test = cash_gate[cash_gate["evaluation_split"] == "test"].copy()
    rows: list[dict[str, Any]] = []
    for _, row in test.iterrows():
        xbi_lcb = numeric(pd.Series([row.get("cash_gate_xbi_alpha_lcb_pct")])).iloc[0]
        cohort_lcb = numeric(pd.Series([row.get("cash_gate_same_cohort_alpha_lcb_pct")])).iloc[0]
        abs_lcb = numeric(pd.Series([row.get("cash_gate_abs_lcb_pct")])).iloc[0]
        allowed_count = int(row.get("allowed_cohort_count") or 0)
        traded_dates = int(row.get("traded_dates") or 0)
        candidate_dates = int(row.get("candidate_dates") or 0)
        trade_rate = 100.0 * traded_dates / candidate_dates if candidate_dates else 0.0
        if allowed_count == 0:
            status = "no_trade_candidate"
            reason = "train_gate_allowed_no_cohorts"
        elif xbi_lcb > 0.0 and cohort_lcb > 0.0 and abs_lcb > 0.0 and trade_rate >= 10.0:
            status = "targeted_policy_candidate"
            reason = "positive_test_lcb_vs_xbi_cohort_and_absolute"
        elif xbi_lcb > 0.0 and cohort_lcb > 0.0:
            status = "diagnostic_positive_alpha_low_trade_or_abs"
            reason = "positive_alpha_lcb_but_absolute_or_trade_rate_not_enough"
        else:
            status = "diagnostic_only"
            reason = "test_lcb_not_positive_after_train_learned_cohort_gate"
        out = {col: row.get(col) for col in KEY_COLUMNS}
        out.update(
            {
                "cohort_top_k": row.get("cohort_top_k"),
                "recommendation_status": status,
                "recommendation_reason": reason,
                "allowed_cohort_count": allowed_count,
                "allowed_cohorts": row.get("allowed_cohorts"),
                "test_trade_rate_pct": round(trade_rate, 6),
                "test_cash_gate_abs_lcb_pct": row.get("cash_gate_abs_lcb_pct"),
                "test_cash_gate_xbi_alpha_lcb_pct": row.get("cash_gate_xbi_alpha_lcb_pct"),
                "test_cash_gate_same_cohort_alpha_lcb_pct": row.get("cash_gate_same_cohort_alpha_lcb_pct"),
                "test_cash_gate_abs_mean_pct": row.get("cash_gate_abs_mean_pct"),
                "test_cash_gate_xbi_alpha_mean_pct": row.get("cash_gate_xbi_alpha_mean_pct"),
                "test_cash_gate_same_cohort_alpha_mean_pct": row.get("cash_gate_same_cohort_alpha_mean_pct"),
            }
        )
        rows.append(out)
    return pd.DataFrame(rows).sort_values(
        ["recommendation_status", "test_cash_gate_xbi_alpha_lcb_pct", "test_cash_gate_same_cohort_alpha_lcb_pct"],
        ascending=[True, False, False],
    )


def build_forced_cohort_recommendations(forced_gate: pd.DataFrame) -> pd.DataFrame:
    if forced_gate.empty:
        return pd.DataFrame()
    key_cols = KEY_COLUMNS + ["cohort_top_k", "allowed_cohorts"]
    train = forced_gate[forced_gate["evaluation_split"] == "train"].copy()
    test = forced_gate[forced_gate["evaluation_split"] == "test"].copy()
    if test.empty:
        return pd.DataFrame()
    train_lookup = {
        tuple(row[col] for col in key_cols): row
        for _, row in train.iterrows()
    }
    rows: list[dict[str, Any]] = []
    for _, row in test.iterrows():
        train_row = train_lookup.get(tuple(row[col] for col in key_cols))
        test_abs_lcb = numeric(pd.Series([row.get("cash_gate_abs_lcb_pct")])).iloc[0]
        test_xbi_lcb = numeric(pd.Series([row.get("cash_gate_xbi_alpha_lcb_pct")])).iloc[0]
        test_cohort_lcb = numeric(pd.Series([row.get("cash_gate_same_cohort_alpha_lcb_pct")])).iloc[0]
        test_trade_rate = numeric(pd.Series([row.get("trade_rate_pct")])).iloc[0]
        train_abs_lcb = numeric(pd.Series([train_row.get("cash_gate_abs_lcb_pct") if train_row is not None else math.nan])).iloc[0]
        train_xbi_lcb = numeric(pd.Series([train_row.get("cash_gate_xbi_alpha_lcb_pct") if train_row is not None else math.nan])).iloc[0]
        train_cohort_lcb = numeric(
            pd.Series([train_row.get("cash_gate_same_cohort_alpha_lcb_pct") if train_row is not None else math.nan])
        ).iloc[0]
        train_trade_rate = numeric(pd.Series([train_row.get("trade_rate_pct") if train_row is not None else math.nan])).iloc[0]
        train_positive = train_abs_lcb > 0.0 and train_xbi_lcb > 0.0 and train_cohort_lcb > 0.0
        test_positive = test_abs_lcb > 0.0 and test_xbi_lcb > 0.0 and test_cohort_lcb > 0.0
        if train_positive and test_positive and test_trade_rate >= 10.0:
            status = "forced_cohort_targeted_policy_candidate"
            reason = "positive_train_and_test_lcb_vs_absolute_xbi_and_same_cohort"
        elif test_positive and not train_positive:
            status = "diagnostic_test_only_regime_candidate"
            reason = "positive_test_lcb_but_train_split_did_not_validate"
        elif train_positive and not test_positive:
            status = "diagnostic_train_only_not_oos_stable"
            reason = "positive_train_lcb_but_test_split_failed"
        else:
            status = "diagnostic_only"
            reason = "forced_cohort_lcb_not_positive_in_train_and_test"
        out = {col: row.get(col) for col in KEY_COLUMNS}
        out.update(
            {
                "cohort_top_k": row.get("cohort_top_k"),
                "recommendation_status": status,
                "recommendation_reason": reason,
                "allowed_cohorts": row.get("allowed_cohorts"),
                "train_trade_rate_pct": round(float(train_trade_rate), 6) if pd.notna(train_trade_rate) else "",
                "test_trade_rate_pct": round(float(test_trade_rate), 6) if pd.notna(test_trade_rate) else "",
                "train_cash_gate_abs_lcb_pct": train_abs_lcb,
                "train_cash_gate_xbi_alpha_lcb_pct": train_xbi_lcb,
                "train_cash_gate_same_cohort_alpha_lcb_pct": train_cohort_lcb,
                "test_cash_gate_abs_lcb_pct": test_abs_lcb,
                "test_cash_gate_xbi_alpha_lcb_pct": test_xbi_lcb,
                "test_cash_gate_same_cohort_alpha_lcb_pct": test_cohort_lcb,
                "test_cash_gate_abs_mean_pct": row.get("cash_gate_abs_mean_pct"),
                "test_cash_gate_xbi_alpha_mean_pct": row.get("cash_gate_xbi_alpha_mean_pct"),
                "test_cash_gate_same_cohort_alpha_mean_pct": row.get("cash_gate_same_cohort_alpha_mean_pct"),
            }
        )
        rows.append(out)
    return pd.DataFrame(rows).sort_values(
        ["recommendation_status", "test_cash_gate_xbi_alpha_lcb_pct", "test_cash_gate_same_cohort_alpha_lcb_pct"],
        ascending=[True, False, False],
    )


def write_markdown_summary(path: Path, manifest: dict[str, Any], recommendations: pd.DataFrame) -> None:
    lines = [
        "# Biotech Cohort/Regime Edge Diagnostics",
        "",
        f"Generated: {manifest['written_at_utc']}",
        "",
        "This diagnostic uses existing calibration artifacts only. It does not regenerate historical files and does not change production scoring.",
        "",
        "## Inputs",
        "",
        f"- Observations: `{manifest['observations']}`",
        f"- Selected diagnostics: `{manifest['selected']}`",
        f"- Output directory: `{manifest['output_dir']}`",
        "",
        "## Coverage",
        "",
        f"- Observation return rows: {manifest['observation_return_rows']:,}",
        f"- Selected rows: {manifest['selected_rows']:,}",
        f"- Cohort/date baseline rows: {manifest['cohort_baseline_rows']:,}",
        "",
        "## Recommendation Snapshot",
        "",
    ]
    if recommendations.empty:
        lines.append("No recommendation rows were produced.")
    else:
        counts = recommendations["recommendation_status"].value_counts().to_dict()
        for status, count in sorted(counts.items()):
            lines.append(f"- {status}: {count}")
        candidates = recommendations[recommendations["recommendation_status"] == "targeted_policy_candidate"].head(10)
        if not candidates.empty:
            lines.extend(
                [
                    "",
                    "Top targeted policy candidates:",
                    "",
                    "| Horizon | Top N | Candidate | Policy | Top K | Allowed Cohorts | Test XBI LCB | Test Cohort LCB |",
                    "|---:|---:|---|---|---:|---|---:|---:|",
                ]
            )
            for _, row in candidates.iterrows():
                lines.append(
                    "| {horizon_days} | {top_n} | {candidate_name} | {selection_policy_name} | {cohort_top_k} | "
                    "{allowed_cohorts} | {test_cash_gate_xbi_alpha_lcb_pct} | "
                    "{test_cash_gate_same_cohort_alpha_lcb_pct} |".format(**row)
                )
    forced_rows = int(manifest.get("forced_cohort_recommendation_rows") or 0)
    if forced_rows:
        lines.extend(
            [
                "",
                "## Forced-Cohort Diagnostic",
                "",
                f"- Forced allowed cohorts: `{ '|'.join(manifest.get('forced_allowed_cohorts') or []) }`",
                f"- Recommendation rows: {forced_rows}",
                f"- Targeted candidates: {manifest.get('forced_cohort_targeted_policy_candidate_rows', 0)}",
                "",
                "See `forced_cohort_cash_gate_simulation.csv` and `forced_cohort_policy_recommendations.csv`.",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    calibration_dir = args.calibration_dir.resolve()
    observations_path = (
        args.observations.resolve()
        if args.observations is not None
        else calibration_dir / "_progress" / "tier1_observations_with_forward_returns.csv"
    )
    selected_path = (
        args.selected.resolve()
        if args.selected is not None
        else calibration_dir / "tier1_selected_ticker_diagnostics.csv"
    )
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else calibration_dir / "cohort_regime_edges"
    )
    top_k_values = parse_top_k(args.top_k)
    forced_allowed_cohorts = parse_cohort_list(args.forced_allowed_cohorts)
    if not observations_path.exists():
        raise FileNotFoundError(observations_path)
    if not selected_path.exists():
        raise FileNotFoundError(selected_path)

    observations = load_observation_returns(observations_path)
    baselines = build_cohort_baselines(observations)
    selected = load_selected(selected_path, observations, baselines)
    same_cohort_outputs = build_same_cohort_outputs(selected, float(args.lcb_z))
    date_level, topk_summary = build_topk_outputs(selected, top_k_values, float(args.lcb_z))
    gate_rows, cash_gate = build_cash_gate_outputs(
        selected,
        date_level,
        top_k_values=top_k_values,
        min_train_dates=int(args.min_train_dates),
        allow_lcb_threshold=float(args.allow_lcb_threshold),
        lcb_z=float(args.lcb_z),
    )
    recommendations = build_recommendations(cash_gate)
    forced_cohort_gate = build_forced_cohort_gate_outputs(
        selected,
        date_level,
        top_k_values=top_k_values,
        forced_allowed_cohorts=forced_allowed_cohorts,
        lcb_z=float(args.lcb_z),
    )
    forced_cohort_recommendations = build_forced_cohort_recommendations(forced_cohort_gate)

    output_dir.mkdir(parents=True, exist_ok=True)
    write_df(output_dir / "cohort_date_return_baselines.csv", baselines)
    for filename, frame in same_cohort_outputs.items():
        write_df(output_dir / filename, frame)
    write_df(output_dir / "cohort_specific_topk_date_level.csv", date_level)
    write_df(output_dir / "cohort_specific_topk_summary.csv", topk_summary)
    write_df(output_dir / "train_learned_cohort_gate.csv", gate_rows)
    write_df(output_dir / "no_trade_cash_gate_simulation.csv", cash_gate)
    write_df(output_dir / "cohort_policy_recommendations.csv", recommendations)
    if forced_allowed_cohorts:
        write_df(output_dir / "forced_cohort_cash_gate_simulation.csv", forced_cohort_gate)
        write_df(output_dir / "forced_cohort_policy_recommendations.csv", forced_cohort_recommendations)

    manifest = {
        "status": "success",
        "written_at_utc": datetime.now(timezone.utc).isoformat(),
        "calibration_dir": str(calibration_dir),
        "observations": str(observations_path),
        "selected": str(selected_path),
        "output_dir": str(output_dir),
        "top_k_values": top_k_values,
        "forced_allowed_cohorts": forced_allowed_cohorts,
        "lcb_z": float(args.lcb_z),
        "min_train_dates": int(args.min_train_dates),
        "allow_lcb_threshold": float(args.allow_lcb_threshold),
        "observation_return_rows": int(len(observations)),
        "cohort_baseline_rows": int(len(baselines)),
        "selected_rows": int(len(selected)),
        "topk_date_level_rows": int(len(date_level)),
        "cash_gate_rows": int(len(cash_gate)),
        "recommendation_rows": int(len(recommendations)),
        "forced_cohort_cash_gate_rows": int(len(forced_cohort_gate)),
        "forced_cohort_recommendation_rows": int(len(forced_cohort_recommendations)),
        "forced_cohort_targeted_policy_candidate_rows": int(
            (
                forced_cohort_recommendations.get("recommendation_status", pd.Series(dtype="string"))
                == "forced_cohort_targeted_policy_candidate"
            ).sum()
            if not forced_cohort_recommendations.empty
            else 0
        ),
        "targeted_policy_candidate_rows": int(
            (recommendations.get("recommendation_status", pd.Series(dtype="string")) == "targeted_policy_candidate").sum()
            if not recommendations.empty
            else 0
        ),
        "notes": [
            "Diagnostic-only run; no historical files were regenerated.",
            "Same-cohort alpha compares selected ticker return against the same asof-date, horizon, and cohort mean.",
            "No-trade/cash gate is trained on train split same-cohort alpha LCB and evaluated on test split with cash return of 0 for blocked dates.",
            "Forced-cohort cash gate is explicit diagnostic simulation only; it does not change calibration policy or production scoring.",
        ],
    }
    write_json(output_dir / "cohort_regime_edge_manifest.json", manifest)
    write_markdown_summary(output_dir / "cohort_regime_edge_summary.md", manifest, recommendations)
    print(
        "cohort_regime_edge_diagnostics_written="
        f"{output_dir} selected_rows={len(selected)} targeted_policy_candidates={manifest['targeted_policy_candidate_rows']}"
    )


if __name__ == "__main__":
    main()

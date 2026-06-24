#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from macro_raw_config import (
    cfg_get,
    configure_pipeline_logging,
    connect_sqlite,
    load_macro_raw_config,
    parse_iso_date,
    resolve_db_path,
    resolve_path,
    utc_now_iso,
)
from macro_serving_common import resolve_serving_db_path
from macro_serving_storage import (
    clear_country_macro_range,
    finish_serving_run,
    init_db,
    insert_many,
    start_serving_run,
)

logger = logging.getLogger(__name__)

REGIME_ORDER = (
    "EXPANSION_DISINFLATION",
    "HEATING_UP",
    "SLOW_GROWTH",
    "STAGFLATION",
)

GLOBAL_SHOCK_FEATURE_SPECS: dict[str, list[tuple[str, str, float]]] = {
    "oil": [
        ("us_brent_spot", "pct_21d", 1.0),
        ("us_wti_spot", "pct_21d", 1.0),
        ("us_henry_hub_natgas", "pct_21d", 1.0),
    ],
    "commodity": [
        ("global_copper", "ann_3m_pct", 1.0),
        ("global_wheat", "ann_3m_pct", 1.0),
    ],
    "dollar": [
        ("us_nominal_broad_dollar", "pct_21d", 1.0),
        ("us_real_broad_dollar", "pct_12m", 1.0),
    ],
    "real_yield": [
        ("us_10y_real_yield", "level", -1.0),
    ],
    "credit": [
        ("us_hy_oas", "level", -1.0),
        ("us_nfci", "level", -1.0),
    ],
}
GLOBAL_SHOCK_NAMES = tuple(GLOBAL_SHOCK_FEATURE_SPECS.keys())
LOCAL_MACRO_GROUPS = ("growth_now", "growth_lead", "inflation_now")
LOCAL_SCORE_COLUMNS = [
    "as_of_date",
    "ref_area",
    "growth_now_score",
    "growth_lead_score",
    "inflation_score",
    "local_external_score",
    "covered_feature_count",
]


@dataclass(frozen=True)
class CountryMacroConfig:
    output_dir: Path
    global_regime_weight: float
    local_macro_weight: float
    external_shock_weight: float
    current_regime_weight: float
    next_regime_weight: float
    confidence_adjustment_power: float
    min_rank_confidence: float
    class_confidence: dict[str, float]
    fallback_penalty: dict[str, float]
    feature_group_weights: dict[str, float]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the Stage 10 country macro fit and confidence layer.")
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--raw-db-path", type=Path, default=None, help="Optional raw SQLite path override.")
    parser.add_argument("--serving-db-path", type=Path, default=None, help="Optional serving SQLite path override.")
    parser.add_argument("--start-date", type=str, default=None, help="Optional Stage 10 start YYYY-MM-DD override.")
    parser.add_argument("--end-date", type=str, default=None, help="Optional Stage 10 end YYYY-MM-DD override.")
    return parser.parse_args()


def _clip(value: float, *, lo: float = -3.0, hi: float = 3.0) -> float:
    return float(min(max(float(value), lo), hi))


def _finite_or_none(value: object) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(out):
        return None
    return out


def _unit(value: object, *, default: float = 0.0) -> float:
    resolved = _finite_or_none(value)
    if resolved is None:
        return float(default)
    return float(min(max(resolved, 0.0), 1.0))


def _int_value(value: object, *, default: int = 0) -> int:
    try:
        if pd.isna(value):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _empty_dated_frame(columns: list[str]) -> pd.DataFrame:
    frame = pd.DataFrame(columns=columns)
    if "as_of_date" in frame.columns:
        frame["as_of_date"] = pd.to_datetime(frame["as_of_date"])
    return frame


def _normalize_weights(raw: dict[str, Any], keys: tuple[str, ...]) -> dict[str, float]:
    weights = {key: max(0.0, float(raw.get(key, 0.0))) for key in keys}
    total = sum(weights.values())
    if total <= 0.0:
        return {key: 1.0 / float(len(keys)) for key in keys}
    return {key: value / total for key, value in weights.items()}


def _resolve_layer_config(cfg: dict[str, Any], config_path: Path) -> CountryMacroConfig:
    raw_cfg = dict(cfg_get(cfg, "country_macro_layer", default={}) or {})
    output_dir = resolve_path(config_path, str(raw_cfg.get("output_dir", "MacroLayer/out/country_macro")))
    if output_dir is None:
        raise ValueError("country_macro_layer.output_dir could not be resolved.")
    fit_weights = [
        float(raw_cfg.get("global_regime_weight", 0.35)),
        float(raw_cfg.get("local_macro_weight", 0.40)),
        float(raw_cfg.get("external_shock_weight", 0.25)),
    ]
    if any(value < 0.0 for value in fit_weights) or abs(sum(fit_weights) - 1.0) > 1e-9:
        raise ValueError("country_macro_layer global/local/external weights must be non-negative and sum to 1.0.")
    regime_weights = [
        float(raw_cfg.get("current_regime_weight", 0.70)),
        float(raw_cfg.get("next_regime_weight", 0.30)),
    ]
    if any(value < 0.0 for value in regime_weights) or abs(sum(regime_weights) - 1.0) > 1e-9:
        raise ValueError("country_macro_layer current_regime_weight + next_regime_weight must sum to 1.0.")
    class_confidence = {str(k): float(v) for k, v in dict(raw_cfg.get("class_confidence", {}) or {}).items()}
    fallback_penalty = {str(k): float(v) for k, v in dict(raw_cfg.get("fallback_penalty", {}) or {}).items()}
    feature_group_weights = _normalize_weights(
        dict(raw_cfg.get("feature_group_weights", {}) or {}),
        LOCAL_MACRO_GROUPS,
    )
    return CountryMacroConfig(
        output_dir=output_dir,
        global_regime_weight=fit_weights[0],
        local_macro_weight=fit_weights[1],
        external_shock_weight=fit_weights[2],
        current_regime_weight=regime_weights[0],
        next_regime_weight=regime_weights[1],
        confidence_adjustment_power=max(0.0, float(raw_cfg.get("confidence_adjustment_power", 1.0))),
        min_rank_confidence=max(0.0, min(float(raw_cfg.get("min_rank_confidence", 0.35)), 1.0)),
        class_confidence=class_confidence or {"A_full": 1.0, "B_partial": 0.78, "C_fallback": 0.62, "MULTI": 0.55},
        fallback_penalty=fallback_penalty or {"A_full": 1.0, "B_partial": 0.90, "C_fallback": 0.80, "MULTI": 0.70},
        feature_group_weights=feature_group_weights,
    )


def _resolve_build_bounds(
    conn: sqlite3.Connection,
    *,
    start_override: str | None,
    end_override: str | None,
) -> tuple[date, date]:
    source_tables = (
        "macro_regime_decision_daily",
        "macro_country_coverage_daily",
        "macro_feature_daily",
    )
    min_dates: list[date] = []
    max_dates: list[date] = []
    for table_name in source_tables:
        row = conn.execute(
            f"SELECT MIN(as_of_date) AS min_date, MAX(as_of_date) AS max_date, COUNT(*) AS row_count FROM {table_name}"
        ).fetchone()
        if row is None or int(row["row_count"] or 0) <= 0:
            raise ValueError(f"{table_name} is empty. Build prerequisite serving layers before Stage 10.")
        min_date = parse_iso_date(row["min_date"])
        max_date = parse_iso_date(row["max_date"])
        if min_date is None or max_date is None:
            raise ValueError(f"Unable to resolve date bounds from {table_name}.")
        min_dates.append(min_date)
        max_dates.append(max_date)
    start_date = parse_iso_date(start_override) or max(min_dates)
    end_date = parse_iso_date(end_override) or min(max_dates)
    if end_date < start_date:
        raise ValueError(f"Stage 10 end date {end_date.isoformat()} is before start date {start_date.isoformat()}.")
    available_start = max(min_dates)
    available_end = min(max_dates)
    if start_date < available_start or end_date > available_end:
        raise ValueError(
            f"Stage 10 requested range {start_date.isoformat()}..{end_date.isoformat()} is outside "
            f"available prerequisite overlap {available_start.isoformat()}..{available_end.isoformat()}."
        )
    return start_date, end_date


def _latest_dependency_raw_ingest_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        """
        SELECT raw_ingest_run_id
        FROM macro_serving_run
        WHERE build_step IN ('regime_decision_layer', 'country_coverage_daily')
          AND status = 'completed'
          AND raw_ingest_run_id IS NOT NULL
        ORDER BY COALESCE(completed_at_utc, started_at_utc) DESC, rowid DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return str(row["raw_ingest_run_id"] or "") or None


def _load_country_metadata(raw_conn: sqlite3.Connection) -> pd.DataFrame:
    frame = pd.read_sql_query(
        """
        SELECT
            ticker,
            country_name,
            COALESCE(oecd_ref_area, ref_area) AS ref_area,
            country_class,
            region,
            market_class,
            commodity_profile,
            energy_profile,
            dollar_sensitivity,
            baseline_ticker,
            country_pack_scope
        FROM macro_country_metadata
        WHERE enabled = 1
          AND country_pack_enabled = 1
          AND country_pack_scope = 'single_country'
        ORDER BY ticker
        """,
        raw_conn,
    )
    if frame.empty:
        raise ValueError("No enabled single-country country-pack rows found in macro_country_metadata.")
    frame["ticker"] = frame["ticker"].astype(str).str.upper().str.strip()
    frame["ref_area"] = frame["ref_area"].astype(str).str.upper().str.strip()
    return frame


def _load_regime_decision_frame(conn: sqlite3.Connection, *, start_date: str, end_date: str) -> pd.DataFrame:
    frame = pd.read_sql_query(
        """
        SELECT
            as_of_date,
            active_current_regime,
            active_next_regime,
            current_confidence,
            next_confidence,
            coverage_flag AS decision_coverage_flag
        FROM macro_regime_decision_daily
        WHERE as_of_date BETWEEN ? AND ?
        ORDER BY as_of_date
        """,
        conn,
        params=[start_date, end_date],
        parse_dates=["as_of_date"],
    )
    if frame.empty:
        raise ValueError("macro_regime_decision_daily has no rows for the requested Stage 10 range.")
    return frame


def _load_country_coverage_frame(
    conn: sqlite3.Connection,
    *,
    countries: pd.DataFrame,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    tickers = countries["ticker"].dropna().astype(str).tolist()
    placeholders = ",".join("?" for _ in tickers)
    frame = pd.read_sql_query(
        f"""
        SELECT
            as_of_date,
            ticker,
            ref_area,
            country_class AS coverage_country_class,
            expected_metric_count,
            available_metric_count,
            required_metric_count,
            available_required_count,
            stale_metric_count,
            coverage_ratio,
            required_coverage_ratio,
            source_quality_score,
            coverage_flag AS country_coverage_flag
        FROM macro_country_coverage_daily
        WHERE as_of_date BETWEEN ? AND ?
          AND ticker IN ({placeholders})
        ORDER BY as_of_date, ticker
        """,
        conn,
        params=[start_date, end_date, *tickers],
        parse_dates=["as_of_date"],
    )
    if frame.empty:
        raise ValueError("macro_country_coverage_daily has no rows for the requested Stage 10 countries/range.")
    frame["ticker"] = frame["ticker"].astype(str).str.upper().str.strip()
    frame["ref_area"] = frame["ref_area"].astype(str).str.upper().str.strip()
    return frame


def _load_local_feature_scores(
    conn: sqlite3.Connection,
    *,
    ref_areas: list[str],
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    placeholders = ",".join("?" for _ in ref_areas)
    frame = pd.read_sql_query(
        f"""
        SELECT
            as_of_date,
            ref_area,
            regime_block,
            standardized_value,
            coverage_flag
        FROM macro_feature_daily
        WHERE as_of_date BETWEEN ? AND ?
          AND ref_area IN ({placeholders})
        ORDER BY as_of_date, ref_area, regime_block
        """,
        conn,
        params=[start_date, end_date, *ref_areas],
        parse_dates=["as_of_date"],
    )
    if frame.empty:
        return _empty_dated_frame(LOCAL_SCORE_COLUMNS)
    frame["ref_area"] = frame["ref_area"].astype(str).str.upper().str.strip()
    frame["regime_block"] = frame["regime_block"].astype(str).str.strip()
    frame["standardized_value"] = pd.to_numeric(frame["standardized_value"], errors="coerce")
    covered = frame.loc[
        (frame["coverage_flag"].fillna(0).astype(int) == 1)
        & frame["standardized_value"].replace([np.inf, -np.inf], np.nan).notna()
    ].copy()
    if covered.empty:
        return _empty_dated_frame(LOCAL_SCORE_COLUMNS)

    grouped = (
        covered.groupby(["as_of_date", "ref_area", "regime_block"], as_index=False)
        .agg(score=("standardized_value", "mean"), covered_feature_count=("standardized_value", "count"))
    )
    scores = grouped.pivot_table(index=["as_of_date", "ref_area"], columns="regime_block", values="score", aggfunc="last").reset_index()
    counts = grouped.groupby(["as_of_date", "ref_area"], as_index=False)["covered_feature_count"].sum()
    scores = scores.merge(counts, on=["as_of_date", "ref_area"], how="left")
    for column in ("growth_now", "growth_lead", "inflation_now", "external_shock"):
        if column not in scores.columns:
            scores[column] = np.nan
    scores = scores.rename(
        columns={
            "growth_now": "growth_now_score",
            "growth_lead": "growth_lead_score",
            "external_shock": "local_external_score",
        }
    )
    scores["inflation_score"] = -pd.to_numeric(scores["inflation_now"], errors="coerce")
    return scores[LOCAL_SCORE_COLUMNS]


def _load_global_shock_frame(conn: sqlite3.Connection, *, start_date: str, end_date: str) -> pd.DataFrame:
    metric_keys = sorted({spec[0] for specs in GLOBAL_SHOCK_FEATURE_SPECS.values() for spec in specs})
    placeholders = ",".join("?" for _ in metric_keys)
    frame = pd.read_sql_query(
        f"""
        SELECT as_of_date, metric_key, feature_name, standardized_value
        FROM macro_feature_daily
        WHERE as_of_date BETWEEN ? AND ?
          AND metric_key IN ({placeholders})
        ORDER BY as_of_date
        """,
        conn,
        params=[start_date, end_date, *metric_keys],
        parse_dates=["as_of_date"],
    )
    if frame.empty:
        return _empty_dated_frame(
            ["as_of_date", *[f"{name}_shock_value" for name in GLOBAL_SHOCK_NAMES], "global_shock_coverage_flag"]
        )
    frame["standardized_value"] = pd.to_numeric(frame["standardized_value"], errors="coerce")
    frame["metric_feature"] = frame["metric_key"].astype(str) + "::" + frame["feature_name"].astype(str)
    wanted = {
        f"{metric_key}::{feature_name}": sign
        for specs in GLOBAL_SHOCK_FEATURE_SPECS.values()
        for metric_key, feature_name, sign in specs
    }
    frame = frame.loc[frame["metric_feature"].isin(wanted)].copy()
    if frame.empty:
        return _empty_dated_frame(
            ["as_of_date", *[f"{name}_shock_value" for name in GLOBAL_SHOCK_NAMES], "global_shock_coverage_flag"]
        )
    wide = frame.pivot_table(index="as_of_date", columns="metric_feature", values="standardized_value", aggfunc="last").reset_index()
    for family, specs in GLOBAL_SHOCK_FEATURE_SPECS.items():
        cols: list[pd.Series] = []
        for metric_key, feature_name, sign in specs:
            col_name = f"{metric_key}::{feature_name}"
            col_data = wide.get(col_name)
            if col_data is None:
                cols.append(pd.Series(np.nan, index=wide.index, dtype="float64"))
            else:
                cols.append(pd.to_numeric(col_data, errors="coerce") * float(sign))
        wide[f"{family}_shock_value"] = pd.concat(cols, axis=1).mean(axis=1, skipna=True).clip(-3.0, 3.0)
    shock_cols = [f"{name}_shock_value" for name in GLOBAL_SHOCK_NAMES]
    wide["global_shock_coverage_flag"] = wide[shock_cols].notna().any(axis=1).astype(int)
    return wide[["as_of_date", *shock_cols, "global_shock_coverage_flag"]]


def _country_regime_prior(row: pd.Series, regime: str | None) -> float:
    values = {
        "EXPANSION_DISINFLATION": 0.65,
        "HEATING_UP": 0.05,
        "SLOW_GROWTH": -0.50,
        "STAGFLATION": -0.70,
    }
    regime_key = str(regime or "").strip()
    score = float(values.get(regime_key, 0.0))
    market_class = str(row.get("market_class", "") or "").upper()
    dollar_sensitivity = str(row.get("dollar_sensitivity", "") or "").lower()
    commodity_profile = str(row.get("commodity_profile", "") or "").lower()
    energy_profile = str(row.get("energy_profile", "") or "").lower()
    region = str(row.get("region", "") or "").lower()

    if market_class == "EM":
        if regime_key == "EXPANSION_DISINFLATION":
            score += 0.15
        elif regime_key == "HEATING_UP":
            score += 0.05
        elif regime_key == "SLOW_GROWTH":
            score -= 0.15
        elif regime_key == "STAGFLATION":
            score -= 0.25
    if dollar_sensitivity == "high":
        if regime_key == "EXPANSION_DISINFLATION":
            score += 0.05
        elif regime_key == "HEATING_UP":
            score -= 0.15
        elif regime_key == "SLOW_GROWTH":
            score -= 0.10
        elif regime_key == "STAGFLATION":
            score -= 0.25
    if "commodity exporter" in commodity_profile:
        if regime_key == "HEATING_UP":
            score += 0.30
        elif regime_key == "STAGFLATION":
            score += 0.20
        elif regime_key == "EXPANSION_DISINFLATION":
            score += 0.05
    if "energy exporter" in energy_profile:
        if regime_key == "HEATING_UP":
            score += 0.30
        elif regime_key == "STAGFLATION":
            score += 0.35
    if region == "europe" and regime_key == "STAGFLATION":
        score -= 0.05
    return _clip(score, lo=-2.0, hi=2.0)


def _country_shock_prior_map(row: pd.Series) -> dict[str, float]:
    out = {
        "oil": -0.10,
        "commodity": 0.00,
        "dollar": -0.20,
        "real_yield": -0.35,
        "credit": -0.50,
    }
    market_class = str(row.get("market_class", "") or "").upper()
    dollar_sensitivity = str(row.get("dollar_sensitivity", "") or "").lower()
    commodity_profile = str(row.get("commodity_profile", "") or "").lower()
    energy_profile = str(row.get("energy_profile", "") or "").lower()
    region = str(row.get("region", "") or "").lower()

    if market_class == "EM":
        out["dollar"] -= 0.25
        out["real_yield"] -= 0.10
        out["credit"] -= 0.15
    if dollar_sensitivity == "high":
        out["dollar"] -= 0.30
    elif dollar_sensitivity == "medium":
        out["dollar"] -= 0.10
    if "commodity exporter" in commodity_profile:
        out["commodity"] += 0.50
        out["oil"] += 0.15
    elif "broad mix" in commodity_profile:
        out["commodity"] += 0.10
    if "energy exporter" in energy_profile:
        out["oil"] += 0.65
    elif "broad mix" in energy_profile:
        out["oil"] += 0.10
    if region == "asia":
        out["dollar"] -= 0.10
    return {key: _clip(value, lo=-1.5, hi=1.5) for key, value in out.items()}


def _weighted_group_score(row: pd.Series, weights: dict[str, float]) -> float:
    column_by_group = {
        "growth_now": "growth_now_score",
        "growth_lead": "growth_lead_score",
        "inflation_now": "inflation_score",
    }
    total = 0.0
    weight_sum = 0.0
    for group, column_name in column_by_group.items():
        value = _finite_or_none(row.get(column_name))
        if value is None:
            continue
        weight = float(weights.get(group, 0.0))
        total += weight * value
        weight_sum += weight
    if weight_sum <= 0.0:
        return 0.0
    return _clip(total / weight_sum)


def _global_regime_fit(row: pd.Series, layer_cfg: CountryMacroConfig) -> float:
    raw_current = row.get("active_current_regime")
    raw_next = row.get("active_next_regime")
    current_regime = str(raw_current) if pd.notna(raw_current) else ""
    next_regime = str(raw_next) if pd.notna(raw_next) else ""
    current = _country_regime_prior(row, current_regime)
    next_value = _country_regime_prior(row, next_regime)
    return _clip(
        float(layer_cfg.current_regime_weight) * current
        + float(layer_cfg.next_regime_weight) * next_value,
        lo=-2.0,
        hi=2.0,
    )


def _global_shock_score(row: pd.Series) -> float:
    priors = _country_shock_prior_map(row)
    total = 0.0
    for family in GLOBAL_SHOCK_NAMES:
        value = _finite_or_none(row.get(f"{family}_shock_value"))
        if value is None:
            value = 0.0
        total += float(priors.get(family, 0.0)) * value
    return _clip(total, lo=-3.0, hi=3.0)


def _external_shock_fit(row: pd.Series) -> float:
    local_external = _finite_or_none(row.get("local_external_score"))
    global_shock = _finite_or_none(row.get("global_shock_score"))
    if local_external is None and global_shock is None:
        return 0.0
    if local_external is None:
        return _clip(float(global_shock))
    if global_shock is None:
        return _clip(float(local_external))
    return _clip(0.60 * float(local_external) + 0.40 * float(global_shock))


def _country_confidence(row: pd.Series, layer_cfg: CountryMacroConfig) -> tuple[float, str, dict[str, float]]:
    raw_country_class = row.get("country_class")
    fallback_country_class = row.get("coverage_country_class")
    if pd.notna(raw_country_class):
        country_class = str(raw_country_class)
    elif pd.notna(fallback_country_class):
        country_class = str(fallback_country_class)
    else:
        country_class = ""
    expected = _int_value(row.get("expected_metric_count"))
    stale = _int_value(row.get("stale_metric_count"))
    class_confidence = _unit(layer_cfg.class_confidence.get(country_class, 0.50), default=0.50)
    coverage_confidence = _unit(row.get("coverage_ratio"))
    required_confidence = _unit(row.get("required_coverage_ratio"), default=1.0)
    freshness_confidence = 1.0 if expected <= 0 else _unit(1.0 - float(stale) / float(expected))
    source_confidence = _unit(row.get("source_quality_score"))
    local_feature_confidence = _unit(row.get("local_feature_coverage_ratio"))
    fallback_penalty = _unit(layer_cfg.fallback_penalty.get(country_class, 0.75), default=0.75)
    coverage_flag = _int_value(row.get("coverage_flag"))
    confidence = (
        0.20 * class_confidence
        + 0.20 * coverage_confidence
        + 0.20 * required_confidence
        + 0.15 * freshness_confidence
        + 0.15 * source_confidence
        + 0.10 * local_feature_confidence
    )
    confidence *= fallback_penalty
    if coverage_flag == 0:
        confidence *= 0.50
    confidence = _unit(confidence)
    reason = (
        f"CLASS:{country_class or 'UNKNOWN'}|"
        f"COV:{coverage_confidence:.2f}|REQ:{required_confidence:.2f}|"
        f"STALE:{stale}|LOCAL_FEATURE:{local_feature_confidence:.2f}"
    )
    parts = {
        "class_confidence": class_confidence,
        "coverage_confidence": coverage_confidence,
        "required_confidence": required_confidence,
        "freshness_confidence": freshness_confidence,
        "source_confidence": source_confidence,
        "local_feature_confidence": local_feature_confidence,
        "fallback_penalty": fallback_penalty,
    }
    return confidence, reason, parts


def _build_stage10_frames(
    *,
    countries: pd.DataFrame,
    coverage: pd.DataFrame,
    regime: pd.DataFrame,
    local_scores: pd.DataFrame,
    global_shocks: pd.DataFrame,
    layer_cfg: CountryMacroConfig,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    base = coverage.merge(countries, on=["ticker", "ref_area"], how="left", suffixes=("", "_metadata"))
    base["country_class"] = base["country_class"].fillna(base["coverage_country_class"])
    base = base.merge(regime, on="as_of_date", how="left")
    base = base.merge(local_scores, on=["as_of_date", "ref_area"], how="left")
    base = base.merge(global_shocks, on="as_of_date", how="left")
    base["decision_coverage_flag"] = base["decision_coverage_flag"].fillna(0).astype(int)
    base["country_coverage_flag"] = base["country_coverage_flag"].fillna(0).astype(int)
    base["coverage_flag"] = (base["decision_coverage_flag"] & base["country_coverage_flag"]).astype(int)
    base["feature_count"] = base["expected_metric_count"].fillna(0).astype(int)
    base["available_feature_count"] = base["available_metric_count"].fillna(0).astype(int)
    base["local_feature_coverage_ratio"] = np.where(
        base["feature_count"] > 0,
        base["available_feature_count"] / base["feature_count"],
        np.nan,
    )
    for column in (
        "growth_now_score",
        "growth_lead_score",
        "inflation_score",
        "local_external_score",
        "oil_shock_value",
        "commodity_shock_value",
        "dollar_shock_value",
        "real_yield_shock_value",
        "credit_shock_value",
    ):
        if column not in base.columns:
            base[column] = np.nan
        base[column] = pd.to_numeric(base[column], errors="coerce").clip(-3.0, 3.0)

    base["global_regime_fit"] = base.apply(lambda row: _global_regime_fit(row, layer_cfg), axis=1)
    base["local_macro_fit"] = base.apply(lambda row: _weighted_group_score(row, layer_cfg.feature_group_weights), axis=1)
    base["global_shock_score"] = base.apply(_global_shock_score, axis=1)
    base["external_shock_fit"] = base.apply(_external_shock_fit, axis=1)
    base["country_macro_fit"] = (
        float(layer_cfg.global_regime_weight) * base["global_regime_fit"].astype(float)
        + float(layer_cfg.local_macro_weight) * base["local_macro_fit"].astype(float)
        + float(layer_cfg.external_shock_weight) * base["external_shock_fit"].astype(float)
    ).clip(-3.0, 3.0)

    confidence_rows = base.apply(lambda row: _country_confidence(row, layer_cfg), axis=1)
    base["country_confidence"] = [item[0] for item in confidence_rows]
    base["confidence_reason"] = [item[1] for item in confidence_rows]
    confidence_parts = pd.DataFrame([item[2] for item in confidence_rows], index=base.index)
    for column in confidence_parts.columns:
        base[column] = confidence_parts[column]
    base["confidence_adjusted_fit"] = base["country_macro_fit"].astype(float) * (
        base["country_confidence"].astype(float).clip(0.0, 1.0) ** float(layer_cfg.confidence_adjustment_power)
    )
    now_iso = utc_now_iso()
    base["updated_at_utc"] = now_iso

    fit_columns = [
        "as_of_date",
        "ticker",
        "ref_area",
        "country_name",
        "country_class",
        "region",
        "market_class",
        "active_current_regime",
        "active_next_regime",
        "global_regime_fit",
        "local_macro_fit",
        "external_shock_fit",
        "growth_now_score",
        "growth_lead_score",
        "inflation_score",
        "local_external_score",
        "global_shock_score",
        "country_macro_fit",
        "confidence_adjusted_fit",
        "feature_count",
        "available_feature_count",
        "local_feature_coverage_ratio",
        "coverage_flag",
        "updated_at_utc",
    ]
    confidence_columns = [
        "as_of_date",
        "ticker",
        "ref_area",
        "country_class",
        "expected_metric_count",
        "available_metric_count",
        "required_metric_count",
        "available_required_count",
        "stale_metric_count",
        "coverage_ratio",
        "required_coverage_ratio",
        "source_quality_score",
        "class_confidence",
        "coverage_confidence",
        "required_confidence",
        "freshness_confidence",
        "source_confidence",
        "local_feature_confidence",
        "fallback_penalty",
        "country_confidence",
        "confidence_reason",
        "coverage_flag",
        "updated_at_utc",
    ]

    ranked = base.copy()
    ranked["country_rank"] = (
        ranked.groupby("as_of_date")["confidence_adjusted_fit"]
        .rank(method="first", ascending=False, na_option="bottom")
        .astype(int)
    )
    group_size = ranked.groupby("as_of_date")["ticker"].transform("count")
    ranked["country_percentile"] = np.where(
        group_size > 1,
        1.0 - (ranked["country_rank"].astype(float) - 1.0) / (group_size.astype(float) - 1.0),
        1.0,
    )
    ranked["eligible_flag"] = (
        (ranked["coverage_flag"].astype(int) == 1)
        & (ranked["country_confidence"].astype(float) >= float(layer_cfg.min_rank_confidence))
        & ranked["confidence_adjusted_fit"].replace([np.inf, -np.inf], np.nan).notna()
    ).astype(int)
    ranked["rank_reason"] = np.where(
        ranked["coverage_flag"].astype(int) == 0,
        "coverage_failed",
        np.where(
            ranked["country_confidence"].astype(float) < float(layer_cfg.min_rank_confidence),
            "confidence_below_min",
            "eligible",
        ),
    )
    rank_columns = [
        "as_of_date",
        "ticker",
        "ref_area",
        "country_class",
        "country_macro_fit",
        "country_confidence",
        "confidence_adjusted_fit",
        "country_rank",
        "country_percentile",
        "eligible_flag",
        "rank_reason",
        "coverage_flag",
        "updated_at_utc",
    ]
    return base[fit_columns].copy(), base[confidence_columns].copy(), ranked[rank_columns].copy()


def _write_atomic_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    frame.to_csv(tmp_path, index=False)
    tmp_path.replace(path)


def _frame_rows(frame: pd.DataFrame, columns: list[str]) -> list[tuple[Any, ...]]:
    if frame.empty:
        return []
    prepared = frame.loc[:, columns].copy()
    if "as_of_date" in prepared.columns:
        prepared["as_of_date"] = pd.to_datetime(prepared["as_of_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    prepared = prepared.replace({np.nan: None})
    return [tuple(row) for row in prepared.itertuples(index=False, name=None)]


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, cfg = load_macro_raw_config(args.config)
    layer_cfg = _resolve_layer_config(cfg, config_path)
    layer_cfg.output_dir.mkdir(parents=True, exist_ok=True)
    raw_db_path = resolve_db_path(cfg, config_path, override=args.raw_db_path)
    serving_db_path = resolve_serving_db_path(cfg, config_path, override=args.serving_db_path)
    raw_conn: sqlite3.Connection | None = None
    conn: sqlite3.Connection | None = None
    serving_run_id = uuid.uuid4().hex
    run_started = False
    rows_written = 0
    try:
        raw_conn = connect_sqlite(raw_db_path, row_factory=sqlite3.Row)
        conn = connect_sqlite(serving_db_path, row_factory=sqlite3.Row)
        init_db(conn)
        start_date, end_date = _resolve_build_bounds(
            conn,
            start_override=args.start_date,
            end_override=args.end_date,
        )
        countries = _load_country_metadata(raw_conn)
        regime = _load_regime_decision_frame(conn, start_date=start_date.isoformat(), end_date=end_date.isoformat())
        coverage = _load_country_coverage_frame(
            conn,
            countries=countries,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
        )
        ref_areas = sorted(countries["ref_area"].dropna().astype(str).unique().tolist())
        local_scores = _load_local_feature_scores(
            conn,
            ref_areas=ref_areas,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
        )
        global_shocks = _load_global_shock_frame(
            conn,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
        )
        fit_frame, confidence_frame, rank_frame = _build_stage10_frames(
            countries=countries,
            coverage=coverage,
            regime=regime,
            local_scores=local_scores,
            global_shocks=global_shocks,
            layer_cfg=layer_cfg,
        )

        raw_ingest_run_id = _latest_dependency_raw_ingest_id(conn)
        start_serving_run(
            conn,
            serving_run_id=serving_run_id,
            build_step="country_macro_layer",
            raw_ingest_run_id=raw_ingest_run_id,
            as_of_start_date=start_date.isoformat(),
            as_of_end_date=end_date.isoformat(),
            metric_count=int(countries["ticker"].nunique()),
            notes="Building Stage 10 country macro fit, confidence, and rank layer.",
        )
        run_started = True
        for table_name in ("country_macro_fit_daily", "country_confidence_daily", "country_macro_rank_daily"):
            clear_country_macro_range(
                conn,
                table_name=table_name,
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
            )

        fit_columns = [
            "as_of_date",
            "ticker",
            "ref_area",
            "country_name",
            "country_class",
            "region",
            "market_class",
            "active_current_regime",
            "active_next_regime",
            "global_regime_fit",
            "local_macro_fit",
            "external_shock_fit",
            "growth_now_score",
            "growth_lead_score",
            "inflation_score",
            "local_external_score",
            "global_shock_score",
            "country_macro_fit",
            "confidence_adjusted_fit",
            "feature_count",
            "available_feature_count",
            "local_feature_coverage_ratio",
            "coverage_flag",
            "updated_at_utc",
        ]
        rows_written += insert_many(
            conn,
            """
            INSERT OR REPLACE INTO country_macro_fit_daily (
                as_of_date, ticker, ref_area, country_name, country_class, region, market_class,
                active_current_regime, active_next_regime, global_regime_fit, local_macro_fit,
                external_shock_fit, growth_now_score, growth_lead_score, inflation_score,
                local_external_score, global_shock_score, country_macro_fit, confidence_adjusted_fit,
                feature_count, available_feature_count, local_feature_coverage_ratio, coverage_flag,
                updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _frame_rows(fit_frame, fit_columns),
            chunk_size=50_000,
        )

        confidence_columns = [
            "as_of_date",
            "ticker",
            "ref_area",
            "country_class",
            "expected_metric_count",
            "available_metric_count",
            "required_metric_count",
            "available_required_count",
            "stale_metric_count",
            "coverage_ratio",
            "required_coverage_ratio",
            "source_quality_score",
            "class_confidence",
            "coverage_confidence",
            "required_confidence",
            "freshness_confidence",
            "source_confidence",
            "local_feature_confidence",
            "fallback_penalty",
            "country_confidence",
            "confidence_reason",
            "coverage_flag",
            "updated_at_utc",
        ]
        rows_written += insert_many(
            conn,
            """
            INSERT OR REPLACE INTO country_confidence_daily (
                as_of_date, ticker, ref_area, country_class, expected_metric_count,
                available_metric_count, required_metric_count, available_required_count,
                stale_metric_count, coverage_ratio, required_coverage_ratio, source_quality_score,
                class_confidence, coverage_confidence, required_confidence, freshness_confidence,
                source_confidence, local_feature_confidence, fallback_penalty, country_confidence,
                confidence_reason, coverage_flag, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _frame_rows(confidence_frame, confidence_columns),
            chunk_size=50_000,
        )

        rank_columns = [
            "as_of_date",
            "ticker",
            "ref_area",
            "country_class",
            "country_macro_fit",
            "country_confidence",
            "confidence_adjusted_fit",
            "country_rank",
            "country_percentile",
            "eligible_flag",
            "rank_reason",
            "coverage_flag",
            "updated_at_utc",
        ]
        rows_written += insert_many(
            conn,
            """
            INSERT OR REPLACE INTO country_macro_rank_daily (
                as_of_date, ticker, ref_area, country_class, country_macro_fit,
                country_confidence, confidence_adjusted_fit, country_rank, country_percentile,
                eligible_flag, rank_reason, coverage_flag, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _frame_rows(rank_frame, rank_columns),
            chunk_size=50_000,
        )

        latest_date = pd.to_datetime(rank_frame["as_of_date"], errors="coerce").max()
        if pd.notna(latest_date):
            latest_key = latest_date.strftime("%Y-%m-%d")
            _write_atomic_csv(
                layer_cfg.output_dir / "country_macro_fit_latest.csv",
                fit_frame.loc[pd.to_datetime(fit_frame["as_of_date"]).dt.strftime("%Y-%m-%d") == latest_key]
                .sort_values("confidence_adjusted_fit", ascending=False)
                .reset_index(drop=True),
            )
            _write_atomic_csv(
                layer_cfg.output_dir / "country_confidence_latest.csv",
                confidence_frame.loc[pd.to_datetime(confidence_frame["as_of_date"]).dt.strftime("%Y-%m-%d") == latest_key]
                .sort_values("country_confidence", ascending=False)
                .reset_index(drop=True),
            )
            _write_atomic_csv(
                layer_cfg.output_dir / "country_macro_rank_latest.csv",
                rank_frame.loc[pd.to_datetime(rank_frame["as_of_date"]).dt.strftime("%Y-%m-%d") == latest_key]
                .sort_values("country_rank")
                .reset_index(drop=True),
            )

        finish_serving_run(conn, serving_run_id=serving_run_id, status="completed", rows_written=rows_written)
    except BaseException as exc:
        if run_started and conn is not None:
            try:
                finish_serving_run(
                    conn,
                    serving_run_id=serving_run_id,
                    status="failed",
                    rows_written=rows_written,
                    notes=f"Country macro layer failed: {type(exc).__name__}: {exc}",
                )
            except Exception:
                logger.exception("Failed to record failed country macro run for serving_run_id=%s", serving_run_id)
        raise
    finally:
        if raw_conn is not None:
            raw_conn.close()
        if conn is not None:
            conn.close()

    logger.info(
        "Stage 10 country macro layer complete: countries=%d rows_written=%d range=%s..%s output_dir=%s",
        int(countries["ticker"].nunique()),
        rows_written,
        start_date.isoformat(),
        end_date.isoformat(),
        layer_cfg.output_dir,
    )


if __name__ == "__main__":
    main()

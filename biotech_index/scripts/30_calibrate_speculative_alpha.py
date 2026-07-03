#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import logging
import math
import os
import random
import sqlite3
import sys
import time
from collections import defaultdict
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable, TypeVar


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, normalize_string_list, resolve_path  # noqa: E402
from biotech_index.core.commercial_risk import commercial_risk_overlay_fields  # noqa: E402
from biotech_index.core.constants import (  # noqa: E402
    GOING_CONCERN_HARD_STATUSES,
    MILD_SOFT_WEAKNESS_REASONS,
    TOXIC_SOFT_WEAKNESS_REASONS,
)
from biotech_index.core.db import connect  # noqa: E402
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402
from biotech_index.core.market_policy import calibration_market_sources  # noqa: E402
from biotech_index.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("calibrate_speculative_alpha")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SQLITE_PARAM_CHUNK_SIZE = 800

DEFAULT_ROUND_TRIP_COST_BPS = 40.0
DEFAULT_LCB_Z = 1.0
DEFAULT_CVAR_Q = 0.05
DEFAULT_MIN_SELECTED_OBSERVATIONS = 30
T = TypeVar("T")
DEFAULT_MIN_ASOF_DATES = 8
DEFAULT_MIN_NET_LCB_RETURN_PCT = 0.0
DEFAULT_MIN_SORTINO = 0.0
DEFAULT_MIN_PROFIT_FACTOR = 1.15
DEFAULT_MAX_CORE_HARD_EXPOSURE_PCT = 0.0
DEFAULT_MAX_EVENT_HARD_EXPOSURE_PCT = 0.0
DEFAULT_MAX_SOFT_EXPOSURE_PCT = 35.0
DEFAULT_MAX_ILLIQUID_EXPOSURE_PCT = 0.0
DEFAULT_MAX_TOP3_GAIN_CONTRIBUTION_PCT = 60.0
DEFAULT_MAX_LARGE_LOSS_20_RATE_PCT = 35.0
DEFAULT_MAX_LARGE_LOSS_40_RATE_PCT = 15.0
DEFAULT_BOOTSTRAP_ITERATIONS = 100
DEFAULT_BOOTSTRAP_TOP_K = 8
DEFAULT_BOOTSTRAP_SEED = 3001
DEFAULT_SELECTED_TICKER_TOP_RANKS = 5
DEFAULT_HOLDOUT_TOP_K = 25
TRADING_BARS_PER_CALENDAR_YEAR = 252.0
CALENDAR_DAYS_PER_YEAR = 365.25
DEFAULT_EMBARGO_BUFFER_CALENDAR_DAYS = 10

SCORE_COLUMNS = [
    "tier1_score",
    "tier1_gate_score",
    "multibagger_score",
    "base_multibagger_score",
    "orthogonal_alpha_score",
    "distinctive_acceleration_score",
]

SPREAD_KEYS = [
    "lcb_return_pct",
    "sortino_like",
    "profit_factor",
    "mean_return_pct",
    "p10_return_pct",
    "cvar_5_return_pct",
    "large_loss_20pct_rate_pct",
    "large_loss_40pct_rate_pct",
    "core_hard_exposure_pct",
    "event_hard_exposure_pct",
    "soft_exposure_pct",
    "toxic_soft_exposure_pct",
    "commercial_risk_overlay_exposure_pct",
    "commercial_business_shock_exposure_pct",
    "evidence_json_missing_exposure_pct",
    "illiquid_exposure_pct",
    "top3_gain_contribution_pct",
]

BOOTSTRAP_METRIC_KEYS = [
    "lcb_return_pct",
    "sortino_like",
    "profit_factor",
    "large_loss_20pct_rate_pct",
    "core_hard_exposure_pct",
    "event_hard_exposure_pct",
    "soft_exposure_pct",
    "toxic_soft_exposure_pct",
    "commercial_business_shock_exposure_pct",
    "evidence_json_missing_exposure_pct",
]


@dataclass(frozen=True)
class Bar:
    day: date
    close: float


@dataclass(frozen=True)
class CalibrationParams:
    # Speculative-alpha calibration uses LCB/Sortino/profit-factor constraints only.
    # Omega is intentionally omitted here until the speculative stack has its own
    # hurdle policy; Tier-1 omega settings should not silently govern this grid.
    round_trip_cost_bps: float = DEFAULT_ROUND_TRIP_COST_BPS
    lcb_z: float = DEFAULT_LCB_Z
    cvar_q: float = DEFAULT_CVAR_Q
    min_selected_observations: int = DEFAULT_MIN_SELECTED_OBSERVATIONS
    min_asof_dates: int = DEFAULT_MIN_ASOF_DATES
    min_net_lcb_return_pct: float = DEFAULT_MIN_NET_LCB_RETURN_PCT
    min_sortino: float = DEFAULT_MIN_SORTINO
    min_profit_factor: float = DEFAULT_MIN_PROFIT_FACTOR
    max_core_hard_exposure_pct: float = DEFAULT_MAX_CORE_HARD_EXPOSURE_PCT
    max_event_hard_exposure_pct: float = DEFAULT_MAX_EVENT_HARD_EXPOSURE_PCT
    max_soft_exposure_pct: float = DEFAULT_MAX_SOFT_EXPOSURE_PCT
    max_illiquid_exposure_pct: float = DEFAULT_MAX_ILLIQUID_EXPOSURE_PCT
    max_top3_gain_contribution_pct: float = DEFAULT_MAX_TOP3_GAIN_CONTRIBUTION_PCT
    max_large_loss_20_rate_pct: float = DEFAULT_MAX_LARGE_LOSS_20_RATE_PCT
    max_large_loss_40_rate_pct: float = DEFAULT_MAX_LARGE_LOSS_40_RATE_PCT


@dataclass(frozen=True)
class SignalSpec:
    signal_name: str
    description: str
    weights: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class PoolSpec:
    pool_name: str
    description: str
    exclude_core_hard: bool = True
    require_liquidity: bool = False
    tier1_top_k: int = 0
    exclude_tier1_top_k: int = 0
    max_tier1_risk_score: float | None = None

    def __post_init__(self) -> None:
        if self.tier1_top_k > 0 and self.exclude_tier1_top_k > 0:
            raise ValueError(
                f"Pool '{self.pool_name}' cannot set both tier1_top_k and exclude_tier1_top_k."
            )


@dataclass(frozen=True)
class GridJob:
    index: int
    horizon: int
    top_n: int
    signal: SignalSpec
    pool: PoolSpec


@dataclass(frozen=True)
class BootstrapJob:
    index: int
    sample: str
    evaluation_split: str
    horizon: int
    top_n: int
    train_rank: int
    signal: SignalSpec
    pool: PoolSpec


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 2 calibration for speculative alpha and multibagger signals. "
            "The script tests whether multibagger/alpha signals add orthogonal value after Tier-1 investability."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--start-asof", type=str, default="")
    parser.add_argument("--end-asof", type=str, default="")
    parser.add_argument("--horizons", type=str, default="20,60,120", help="Comma-separated trading-bar horizons.")
    parser.add_argument("--top-n", type=str, default="10,20,30", help="Comma-separated Top-N cutoffs.")
    parser.add_argument("--market-sources", type=str, default="")
    parser.add_argument("--max-snapshots", type=int, default=0, help="Optional smoke-test limit; keeps latest dates.")
    parser.add_argument("--train-fraction", type=float, default=None)
    parser.add_argument("--max-workers", type=int, default=None)
    parser.add_argument("--bootstrap-iterations", type=int, default=None, help="Use 0 to disable bootstrap CI output.")
    parser.add_argument("--bootstrap-top-k", type=int, default=None)
    parser.add_argument("--holdout-top-k", type=int, default=None)
    parser.add_argument("--bootstrap-seed", type=int, default=None)
    parser.add_argument("--selected-ticker-top-ranks", type=int, default=None)
    parser.add_argument("--include-non-fridays", action="store_true", help="Include non-Friday snapshots.")
    parser.add_argument(
        "--next-bar-entry",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enter on the first market bar strictly after the score snapshot date.",
    )
    parser.add_argument(
        "--exclude-current-removals",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Exclude current inactive/remove/manual-exclude companies from historical calibration.",
    )
    parser.add_argument("--exclude-tickers", type=str, default="")
    return parser.parse_args()


def configure_logging() -> None:
    configure_utc_logging()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def to_float(raw: object, default: float | None = None) -> float | None:
    if raw is None:
        return default
    candidate: int | float | str
    if isinstance(raw, bool):
        candidate = int(raw)
    elif isinstance(raw, (int, float, str)):
        candidate = raw
    else:
        candidate = str(raw).strip()
    try:
        value = float(candidate)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def as_bool(raw: object, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"1", "true", "t", "yes", "y", "enabled", "on"}:
        return True
    if text in {"0", "false", "f", "no", "n", "disabled", "off"}:
        return False
    return default


def parse_json(raw: object) -> dict[str, Any]:
    try:
        payload = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def parse_int_list(raw: str, *, default: list[int]) -> list[int]:
    values: list[int] = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        parsed = float(part)
        if not parsed.is_integer():
            raise ValueError(f"Expected integer list value, got {part}")
        value = int(parsed)
        if value <= 0:
            raise ValueError(f"Expected positive integer value, got {value}")
        values.append(value)
    return values or list(default)


def parse_string_set(raw: object) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, str):
        parts = raw.replace(";", ",").replace("|", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        parts = [str(item) for item in raw]
    else:
        parts = [str(raw)]
    return {ticker for part in parts if (ticker := normalize_ticker(part))}


def chunked(values: list[Any], size: int = SQLITE_PARAM_CHUNK_SIZE) -> Iterable[list[Any]]:
    step = max(1, int(size))
    for start in range(0, len(values), step):
        yield values[start : start + step]


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def optional_select(columns: set[str], column: str, alias: str, *, table_alias: str) -> str:
    if column in columns:
        return f"{table_alias}.{column} AS {alias}"
    return f"NULL AS {alias}"


def load_calibration_params(config: dict[str, Any]) -> CalibrationParams:
    stack = cfg_get(config, "calibration.phase2.recommended_stack", {}) or {}
    costs = cfg_get(config, "calibration.phase2.costs", {}) or {}
    return CalibrationParams(
        round_trip_cost_bps=float(costs.get("long_round_trip_bps", DEFAULT_ROUND_TRIP_COST_BPS)),
        lcb_z=float(stack.get("lcb_z", DEFAULT_LCB_Z)),
        cvar_q=float(stack.get("cvar_q", DEFAULT_CVAR_Q)),
        min_selected_observations=int(stack.get("min_selected_observations", DEFAULT_MIN_SELECTED_OBSERVATIONS)),
        min_asof_dates=int(stack.get("min_asof_dates", DEFAULT_MIN_ASOF_DATES)),
        min_net_lcb_return_pct=float(stack.get("min_net_lcb_return_pct", DEFAULT_MIN_NET_LCB_RETURN_PCT)),
        min_sortino=float(stack.get("min_sortino", DEFAULT_MIN_SORTINO)),
        min_profit_factor=float(stack.get("min_profit_factor", DEFAULT_MIN_PROFIT_FACTOR)),
        max_core_hard_exposure_pct=float(
            stack.get("max_core_hard_exposure_pct", DEFAULT_MAX_CORE_HARD_EXPOSURE_PCT)
        ),
        max_event_hard_exposure_pct=float(
            stack.get("max_event_hard_exposure_pct", DEFAULT_MAX_EVENT_HARD_EXPOSURE_PCT)
        ),
        max_soft_exposure_pct=float(stack.get("max_soft_exposure_pct", DEFAULT_MAX_SOFT_EXPOSURE_PCT)),
        max_illiquid_exposure_pct=float(stack.get("max_illiquid_exposure_pct", DEFAULT_MAX_ILLIQUID_EXPOSURE_PCT)),
        max_top3_gain_contribution_pct=float(
            stack.get("max_top3_gain_contribution_pct", DEFAULT_MAX_TOP3_GAIN_CONTRIBUTION_PCT)
        ),
        max_large_loss_20_rate_pct=float(
            stack.get("max_large_loss_20_rate_pct", DEFAULT_MAX_LARGE_LOSS_20_RATE_PCT)
        ),
        max_large_loss_40_rate_pct=float(
            stack.get("max_large_loss_40_rate_pct", DEFAULT_MAX_LARGE_LOSS_40_RATE_PCT)
        ),
    )


def load_snapshot_dates(
    conn: sqlite3.Connection,
    *,
    start_asof: date | None,
    end_asof: date | None,
    fridays_only: bool,
    max_snapshots: int,
) -> list[str]:
    rows = conn.execute(
        """
        SELECT d.asof_date
        FROM daily_scores d
        INNER JOIN multibagger_scores_daily m ON m.asof_date = d.asof_date
        GROUP BY d.asof_date
        ORDER BY d.asof_date
        """
    ).fetchall()
    dates: list[str] = []
    for row in rows:
        parsed = parse_date(row["asof_date"])
        if parsed is None:
            continue
        if start_asof and parsed < start_asof:
            continue
        if end_asof and parsed > end_asof:
            continue
        if fridays_only and parsed.weekday() != 4:
            continue
        dates.append(parsed.isoformat())
    if max_snapshots > 0:
        dates = dates[-max_snapshots:]
    return dates


def load_excluded_tickers(conn: sqlite3.Connection, *, exclude_current_removals: bool, extra: set[str]) -> set[str]:
    out = set(extra)
    if exclude_current_removals:
        raise ValueError(
            "calibration.phase2.exclude_current_removals is disabled because current removal status "
            "retroactively excludes historical snapshots. Use calibration.exclude_tickers for explicit "
            "non-temporal exclusions until removal_date history is available."
        )
    return {ticker for ticker in out if ticker}


def load_score_rows(conn: sqlite3.Connection, dates: list[str], excluded_tickers: set[str]) -> list[dict[str, Any]]:
    if not dates:
        return []
    daily_cols = table_columns(conn, "daily_scores")
    multi_cols = table_columns(conn, "multibagger_scores_daily")
    multi_feature_cols = table_columns(conn, "multibagger_features_daily")
    join_multibagger_features = table_exists(conn, "multibagger_features_daily")
    select_columns = [
        "d.asof_date",
        "d.company_id",
        "c.ticker",
        "c.company_name",
        "d.rank AS tier1_rank",
        "d.bucket AS tier1_bucket",
        "d.opportunity_score AS tier1_score",
        optional_select(daily_cols, "tier1_selection_gate_score", "tier1_gate_score", table_alias="d"),
        "d.risk_score AS tier1_risk_score",
        "d.top_evidence_json AS tier1_top_evidence_json",
        "m.rank AS multibagger_rank",
        "m.bucket AS multibagger_bucket",
        "m.top_evidence_json AS multibagger_top_evidence_json",
        "m.multibagger_score AS multibagger_score",
        optional_select(multi_cols, "base_multibagger_score", "base_multibagger_score", table_alias="m"),
        optional_select(multi_cols, "orthogonal_alpha_score", "orthogonal_alpha_score", table_alias="m"),
        optional_select(multi_cols, "distinctive_acceleration_score", "distinctive_acceleration_score", table_alias="m"),
        optional_select(multi_cols, "tier1_available", "multibagger_tier1_available", table_alias="m"),
        optional_select(multi_cols, "tier1_gate_multiplier", "multibagger_tier1_gate_multiplier", table_alias="m"),
        (
            optional_select(
                multi_feature_cols,
                "commercial_fragility_risk_score",
                "commercial_fragility_risk_score",
                table_alias="f",
            )
            if join_multibagger_features
            else "NULL AS commercial_fragility_risk_score"
        ),
    ]
    out: list[dict[str, Any]] = []
    for date_chunk in chunked(dates):
        placeholders = ",".join("?" for _ in date_chunk)
        rows = conn.execute(
            f"""
            SELECT {", ".join(select_columns)}
            FROM daily_scores d
            INNER JOIN multibagger_scores_daily m
                ON m.asof_date = d.asof_date
               AND m.company_id = d.company_id
            INNER JOIN companies c ON c.company_id = d.company_id
            {"LEFT JOIN multibagger_features_daily f ON f.asof_date = d.asof_date AND f.company_id = d.company_id" if join_multibagger_features else ""}
            WHERE d.asof_date IN ({placeholders})
            ORDER BY d.asof_date, d.rank, c.ticker
            """,
            tuple(date_chunk),
        ).fetchall()
        for row in rows:
            record = dict(row)
            ticker = normalize_ticker(record.get("ticker"))
            if ticker in excluded_tickers:
                continue
            if not ticker:
                continue
            record["ticker"] = ticker
            out.append(record)
    return out


def nested_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def first_float(*values: object) -> float | None:
    for value in values:
        parsed = to_float(value)
        if parsed is not None:
            return parsed
    return None


def parse_reason_list(raw: object) -> list[str]:
    if isinstance(raw, list):
        return [str(value).strip() for value in raw if str(value).strip()]
    return [part.strip() for part in str(raw or "").replace(";", "|").split("|") if part.strip()]


def add_diagnostics(
    rows: list[dict[str, Any]],
    *,
    min_addv20: float,
    commercial_risk_settings: dict[str, Any] | None = None,
) -> None:
    commercial_risk_settings = commercial_risk_settings or {}
    for row in rows:
        tier1 = parse_json(row.get("tier1_top_evidence_json"))
        multi = parse_json(row.get("multibagger_top_evidence_json"))
        expected_tier1_keys = {
            "risk_flags",
            "ctgov_quality",
            "commercial_value",
            "core_structural_veto",
            "commercial_risk_overlay",
        }
        evidence_quality = 1.0 if expected_tier1_keys.issubset(set(tier1)) else 0.0
        risk_flags = nested_dict(tier1, "risk_flags")
        ctgov = nested_dict(tier1, "ctgov_quality")
        commercial = nested_dict(tier1, "commercial_value")
        core_veto = nested_dict(tier1, "core_structural_veto")
        embedded_commercial_risk = nested_dict(tier1, "commercial_risk_overlay")
        sec_events = nested_dict(tier1, "sec_events")
        multi_components = nested_dict(multi, "component_scores")
        multi_market = nested_dict(multi, "market")

        addv = first_float(risk_flags.get("median_addv20"), multi_market.get("avg_dollar_volume_20d"))
        verified_active = first_float(ctgov.get("verified_qualifying_active_trial_count"), 0.0) or 0.0
        cash_runway = first_float(risk_flags.get("cash_runway_months"))
        severe_runway = as_bool(core_veto.get("severe_runway_flag"), False)
        if "severe_runway_flag" in str(core_veto.get("reasons") or ""):
            severe_runway = True
        commercial_stage = as_bool(commercial.get("commercial_stage_flag"), False)
        profitable = as_bool(commercial.get("profitable_flag"), False)
        has_business_anchor = commercial_stage or profitable
        going_concern = str(risk_flags.get("going_concern_status") or "").strip().lower()
        reverse_splits = first_float(risk_flags.get("reverse_split_hits_2y"), 0.0) or 0.0
        dilution_events = first_float(sec_events.get("dilution_event_count"), risk_flags.get("sec_dilution_event_count"), 0.0) or 0.0
        negative_clinical = (
            first_float(sec_events.get("negative_clinical_event_count"), risk_flags.get("sec_negative_clinical_event_count"), 0.0)
            or 0.0
        )
        financial_quality = str(risk_flags.get("financial_data_quality") or "").strip().lower()
        commercial_fragility = (
            first_float(
                row.get("commercial_fragility_risk_score"),
                risk_flags.get("commercial_fragility_risk_score"),
                commercial.get("commercial_fragility_risk_score"),
                multi_components.get("commercial_fragility_risk_score"),
                0.0,
            )
            or 0.0
        )
        risk_score = first_float(row.get("tier1_risk_score"), 0.0) or 0.0
        computed_commercial_risk = commercial_risk_overlay_fields(
            commercial,
            {"commercial_fragility_risk_score": commercial_fragility},
            commercial_risk_settings,
        )
        commercial_risk_source = "evidence_json"
        if first_float(embedded_commercial_risk.get("commercial_risk_overlay_score")) is None:
            commercial_risk = computed_commercial_risk
            commercial_risk_source = "computed"
        else:
            commercial_risk = {**computed_commercial_risk, **embedded_commercial_risk}

        core_reasons: list[str] = []
        core_reasons = parse_reason_list(core_veto.get("reasons"))
        derived_core_reasons: list[str] = []
        if not core_reasons:
            if cash_runway is not None and cash_runway < 9.0:
                derived_core_reasons.append("cash_runway_lt_9m")
            if severe_runway:
                derived_core_reasons.append("severe_runway_flag")
            if going_concern in GOING_CONCERN_HARD_STATUSES:
                derived_core_reasons.append("going_concern_confirmed")
            if reverse_splits > 0.0:
                derived_core_reasons.append("reverse_split_history")
            if verified_active <= 0.0 and not has_business_anchor:
                derived_core_reasons.append("no_active_trial_no_business_anchor")
            if addv is not None and addv < min_addv20:
                derived_core_reasons.append("illiquid")
            core_reasons = derived_core_reasons
        else:
            if cash_runway is not None and cash_runway < 9.0:
                derived_core_reasons.append("cash_runway_lt_9m")
            if severe_runway:
                derived_core_reasons.append("severe_runway_flag")
            if going_concern in GOING_CONCERN_HARD_STATUSES:
                derived_core_reasons.append("going_concern_confirmed")
            if reverse_splits > 0.0:
                derived_core_reasons.append("reverse_split_history")
            if verified_active <= 0.0 and not has_business_anchor:
                derived_core_reasons.append("no_active_trial_no_business_anchor")
            if addv is not None and addv < min_addv20:
                derived_core_reasons.append("illiquid")
            if set(derived_core_reasons) and set(derived_core_reasons) != set(core_reasons):
                LOGGER.warning(
                    "Spec alpha core weakness divergence for %s %s: evidence=%s derived=%s",
                    row.get("asof_date", ""),
                    row.get("ticker", ""),
                    "|".join(sorted(core_reasons)),
                    "|".join(sorted(derived_core_reasons)),
                )

        event_reasons: list[str] = []
        if dilution_events >= 2.0:
            event_reasons.append("repeated_dilution")
        if negative_clinical > 0.0:
            event_reasons.append("negative_clinical_event")

        soft_reasons: list[str] = []
        if cash_runway is not None and 9.0 <= cash_runway < 12.0 and not has_business_anchor:
            soft_reasons.append("cash_runway_9_to_12m_clinical")
        if financial_quality in {"low", "poor", "stale"}:
            soft_reasons.append("low_financial_data_quality")
        if commercial_fragility >= 70.0:
            soft_reasons.append("high_commercial_fragility")
        if risk_score >= 75.0:
            soft_reasons.append("high_tier1_risk_score")
        toxic_soft_reasons = [reason for reason in soft_reasons if reason in TOXIC_SOFT_WEAKNESS_REASONS]
        mild_soft_reasons = [reason for reason in soft_reasons if reason in MILD_SOFT_WEAKNESS_REASONS]

        commercial_overlay_score = first_float(commercial_risk.get("commercial_risk_overlay_score"), 0.0) or 0.0
        commercial_business_shock_score = first_float(commercial_risk.get("commercial_business_shock_score"), 0.0) or 0.0
        row["diag_avg_dollar_volume_20d"] = addv if addv is not None else ""
        row["diag_liquidity_ok"] = 1.0 if addv is not None and addv >= min_addv20 else 0.0 if addv is not None else ""
        row["diag_cash_runway_months"] = cash_runway if cash_runway is not None else ""
        row["diag_verified_active_trial_count"] = verified_active
        row["diag_has_business_anchor"] = 1.0 if has_business_anchor else 0.0
        row["diag_core_hard_weakness_flag"] = 1.0 if core_reasons else 0.0
        row["diag_core_hard_weakness_reasons"] = "|".join(core_reasons)
        row["diag_event_hard_weakness_flag"] = 1.0 if event_reasons else 0.0
        row["diag_event_hard_weakness_reasons"] = "|".join(event_reasons)
        row["diag_soft_weakness_flag"] = 1.0 if soft_reasons else 0.0
        row["diag_soft_weakness_reasons"] = "|".join(soft_reasons)
        row["diag_toxic_soft_weakness_flag"] = 1.0 if toxic_soft_reasons else 0.0
        row["diag_toxic_soft_weakness_reasons"] = "|".join(toxic_soft_reasons)
        row["diag_mild_soft_weakness_flag"] = 1.0 if mild_soft_reasons else 0.0
        row["diag_mild_soft_weakness_reasons"] = "|".join(mild_soft_reasons)
        row["diag_core_hard_flag"] = row["diag_core_hard_weakness_flag"]
        row["diag_core_hard_reasons"] = row["diag_core_hard_weakness_reasons"]
        row["diag_event_hard_flag"] = row["diag_event_hard_weakness_flag"]
        row["diag_event_hard_reasons"] = row["diag_event_hard_weakness_reasons"]
        row["diag_soft_flag"] = row["diag_soft_weakness_flag"]
        row["diag_soft_reasons"] = row["diag_soft_weakness_reasons"]
        row["diag_illiquid_flag"] = 1.0 if "illiquid" in core_reasons else 0.0
        row["diag_multibagger_risk_penalty"] = first_float(multi_components.get("multibagger_risk_penalty"))
        row["diag_commercial_fragility_risk_score"] = commercial_fragility
        row["diag_commercial_risk_source"] = commercial_risk_source
        row["diag_commercial_risk_overlay_score"] = commercial_overlay_score
        row["diag_commercial_risk_overlay_flag"] = 1.0 if commercial_overlay_score > 0.0 else 0.0
        row["diag_commercial_risk_overlay_reasons"] = commercial_risk.get("commercial_risk_overlay_reasons", "")
        row["diag_commercial_deterioration_score"] = first_float(
            commercial_risk.get("commercial_deterioration_score"), 0.0
        ) or 0.0
        row["diag_commercial_deterioration_reasons"] = commercial_risk.get("commercial_deterioration_reasons", "")
        row["diag_valuation_growth_mismatch_score"] = first_float(
            commercial_risk.get("valuation_growth_mismatch_score"), 0.0
        ) or 0.0
        row["diag_valuation_growth_mismatch_reasons"] = commercial_risk.get("valuation_growth_mismatch_reasons", "")
        row["diag_transient_revenue_anchor_score"] = first_float(
            commercial_risk.get("transient_revenue_anchor_score"), 0.0
        ) or 0.0
        row["diag_transient_revenue_anchor_reasons"] = commercial_risk.get("transient_revenue_anchor_reasons", "")
        row["diag_commercial_business_shock_score"] = commercial_business_shock_score
        row["diag_commercial_business_shock_flag"] = 1.0 if commercial_business_shock_score > 0.0 else 0.0
        row["diag_commercial_business_shock_reasons"] = commercial_risk.get("commercial_business_shock_reasons", "")
        row["diag_evidence_json_quality"] = evidence_quality
        row["diag_evidence_json_missing_flag"] = 1.0 if evidence_quality <= 0.0 else 0.0

    for score_key in SCORE_COLUMNS:
        add_percentile_by_date(rows, score_key, f"pct_{score_key}")


def add_percentile_by_date(rows: list[dict[str, Any]], source_key: str, output_key: str) -> None:
    for row in rows:
        row[output_key] = ""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("asof_date") or "")].append(row)
    for date_rows in grouped.values():
        scored = [(to_float(row.get(source_key)), idx, row) for idx, row in enumerate(date_rows)]
        scored = [(score, idx, row) for score, idx, row in scored if score is not None]
        scored.sort(key=lambda item: (item[0], item[1]))
        n = len(scored)
        if n == 1:
            scored[0][2][output_key] = 50.0
            continue
        i = 0
        while i < n:
            j = i
            while j + 1 < n and scored[j + 1][0] == scored[i][0]:
                j += 1
            percentile = 100.0 * ((i + j) / 2.0) / float(max(1, n - 1))
            for k in range(i, j + 1):
                scored[k][2][output_key] = percentile
            i = j + 1


def load_bars(
    conn: sqlite3.Connection,
    *,
    tickers: set[str],
    min_date: date,
    market_sources: list[str],
) -> dict[str, list[Bar]]:
    if not tickers:
        return {}
    source_priority = {source: idx for idx, source in enumerate(market_sources)}
    ordered_sources = [source for source in market_sources if source]
    if not ordered_sources:
        return {}
    grouped: dict[tuple[str, str], list[Bar]] = defaultdict(list)
    ordered_tickers = sorted(tickers)
    ticker_chunk_size = max(1, SQLITE_PARAM_CHUNK_SIZE - len(ordered_sources) - 1)
    for ticker_chunk in chunked(ordered_tickers, ticker_chunk_size):
        ticker_placeholders = ",".join("?" for _ in ticker_chunk)
        source_placeholders = ",".join("?" for _ in ordered_sources)
        rows = conn.execute(
            f"""
            SELECT ticker, bar_date, source, close
            FROM market_bars_daily
            WHERE ticker IN ({ticker_placeholders})
              AND source IN ({source_placeholders})
              AND bar_date >= ?
              AND close IS NOT NULL
              AND close > 0
            ORDER BY ticker, source, bar_date
            """,
            (*ticker_chunk, *ordered_sources, min_date.isoformat()),
        ).fetchall()
        for row in rows:
            parsed = parse_date(row["bar_date"])
            close = to_float(row["close"])
            if parsed is None or close is None or close <= 0.0:
                continue
            ticker_key = normalize_ticker(row["ticker"])
            if not ticker_key:
                continue
            grouped[(ticker_key, str(row["source"] or ""))].append(Bar(day=parsed, close=close))
    by_ticker: dict[str, list[tuple[int, list[Bar]]]] = defaultdict(list)
    for (ticker, source), bars in grouped.items():
        if bars:
            by_ticker[ticker].append((source_priority.get(source, 9999), bars))
    out: dict[str, list[Bar]] = {}
    for ticker in ordered_tickers:
        candidates = by_ticker.get(ticker, [])
        if candidates:
            out[ticker] = sorted(min(candidates, key=lambda item: item[0])[1], key=lambda bar: bar.day)
    return out


def forward_return(bars: list[Bar], asof: date, horizon: int, *, next_bar_entry: bool) -> tuple[float | None, str, str]:
    if not bars:
        return None, "", ""
    days = [bar.day for bar in bars]
    entry_idx = bisect.bisect_right(days, asof) if next_bar_entry else bisect.bisect_left(days, asof)
    if entry_idx < 0 or entry_idx >= len(bars):
        return None, "", ""
    target_idx = entry_idx + horizon
    if target_idx >= len(bars):
        return None, bars[entry_idx].day.isoformat(), ""
    entry = bars[entry_idx]
    target = bars[target_idx]
    if entry.close <= 0.0:
        return None, entry.day.isoformat(), target.day.isoformat()
    return (target.close / entry.close) - 1.0, entry.day.isoformat(), target.day.isoformat()


def add_forward_returns(
    rows: list[dict[str, Any]],
    bars_by_ticker: dict[str, list[Bar]],
    horizons: list[int],
    *,
    round_trip_cost_bps: float,
    next_bar_entry: bool,
) -> None:
    cost = round_trip_cost_bps / 10000.0
    missing_return_counts: defaultdict[tuple[int, str], int] = defaultdict(int)
    for row in rows:
        ticker = normalize_ticker(row.get("ticker"))
        asof = parse_date(row.get("asof_date"))
        bars = bars_by_ticker.get(ticker, [])
        for horizon in horizons:
            prefix = f"fwd_{horizon}d"
            if asof is None:
                row[f"{prefix}_return"] = ""
                row[f"{prefix}_net_return"] = ""
                row[f"{prefix}_entry_date"] = ""
                row[f"{prefix}_target_date"] = ""
                missing_return_counts[(horizon, "invalid_asof_date")] += 1
                continue
            ret, entry_date, target_date = forward_return(bars, asof, horizon, next_bar_entry=next_bar_entry)
            row[f"{prefix}_return"] = ret if ret is not None else ""
            row[f"{prefix}_net_return"] = ret - cost if ret is not None else ""
            row[f"{prefix}_entry_date"] = entry_date
            row[f"{prefix}_target_date"] = target_date
            row[f"{prefix}_round_trip_cost_bps"] = round_trip_cost_bps
            if ret is None:
                if not bars:
                    reason = "no_market_bars"
                elif not entry_date:
                    reason = "no_entry_bar"
                elif not target_date:
                    reason = "insufficient_horizon_bars"
                else:
                    reason = "invalid_entry_close"
                missing_return_counts[(horizon, reason)] += 1
    if missing_return_counts:
        summary = ", ".join(
            f"{horizon}d:{reason}={count}"
            for (horizon, reason), count in sorted(missing_return_counts.items())
        )
        LOGGER.warning("Forward-return coverage gaps: %s", summary)


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def stdev(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    avg = sum(values) / len(values)
    return math.sqrt(sum((value - avg) ** 2 for value in values) / (len(values) - 1))


def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = max(0.0, min(1.0, q)) * (len(ordered) - 1)
    lower = int(math.floor(pos))
    upper = int(math.ceil(pos))
    if lower == upper:
        return ordered[lower]
    weight = pos - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def pct(value: float | None) -> float | str:
    return "" if value is None else round(100.0 * value, 6)


def rounded(value: float | None, digits: int = 6) -> float | str:
    return "" if value is None else round(value, digits)


def safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or abs(denominator) <= 1e-12:
        return None
    return numerator / denominator


def winsorized_mean(values: list[float], lower_q: float = 0.05, upper_q: float = 0.95) -> float | None:
    if not values:
        return None
    lower = quantile(values, lower_q)
    upper = quantile(values, upper_q)
    if lower is None or upper is None:
        return None
    return mean([min(max(value, lower), upper) for value in values])


def lower_confidence_bound(values: list[float], *, z: float) -> float | None:
    if not values:
        return None
    avg = mean(values)
    if avg is None:
        return None
    sigma = stdev(values)
    if sigma is None:
        return avg
    return avg - max(0.0, z) * sigma / math.sqrt(len(values))


def cvar_left_tail(values: list[float], *, q: float) -> float | None:
    if not values:
        return None
    threshold = quantile(values, q)
    if threshold is None:
        return None
    tail = [value for value in values if value <= threshold]
    if not tail:
        return None
    return mean(tail)


def profit_factor(values: list[float], *, hurdle: float = 0.0) -> float | None:
    gains = sum(max(value - hurdle, 0.0) for value in values)
    losses = sum(max(hurdle - value, 0.0) for value in values)
    if losses <= 1e-12:
        return None if gains <= 1e-12 else 999.0
    return gains / losses


def top_gain_contribution(values: list[float], *, top_n: int = 3) -> float | None:
    gains = sorted([value for value in values if value > 0.0], reverse=True)
    total_gain = sum(gains)
    if total_gain <= 1e-12:
        return None
    return sum(gains[:top_n]) / total_gain


def numeric_values(rows: Iterable[dict[str, Any]], key: str) -> list[float]:
    values: list[float] = []
    for row in rows:
        value = to_float(row.get(key))
        if value is not None:
            values.append(value)
    return values


def pct_flag(rows: list[dict[str, Any]], key: str) -> float | str:
    values = [1.0 if (to_float(row.get(key), 0.0) or 0.0) > 0.0 else 0.0 for row in rows]
    return pct(mean(values)) if values else ""


def mean_numeric(rows: Iterable[dict[str, Any]], key: str) -> float | str:
    return rounded(mean(numeric_values(rows, key)))


def summarize_return_risk(values: list[float], *, params: CalibrationParams) -> dict[str, Any]:
    if not values:
        return {
            "n": 0,
            "mean_return_pct": "",
            "median_return_pct": "",
            "hit_rate_pct": "",
            "loss_rate_pct": "",
            "winsorized_mean_return_pct": "",
            "stdev_return_pct": "",
            "downside_deviation_pct": "",
            "lcb_return_pct": "",
            "cvar_5_return_pct": "",
            "sharpe_like": "",
            "sortino_like": "",
            "profit_factor": "",
            "top3_gain_contribution_pct": "",
            "worst_return_pct": "",
            "best_return_pct": "",
            "p05_return_pct": "",
            "p10_return_pct": "",
            "large_loss_20pct_rate_pct": "",
            "large_loss_40pct_rate_pct": "",
            "large_gain_20pct_rate_pct": "",
        }
    positives = [value for value in values if value > 0.0]
    negatives = [value for value in values if value < 0.0]
    downside_terms = [min(0.0, value) for value in values]
    avg = mean(values)
    volatility = stdev(values)
    downside = math.sqrt(sum(value**2 for value in downside_terms) / len(values))
    return {
        "n": len(values),
        "mean_return_pct": pct(avg),
        "median_return_pct": pct(median(values)),
        "hit_rate_pct": round(100.0 * len(positives) / len(values), 6),
        "loss_rate_pct": round(100.0 * len(negatives) / len(values), 6),
        "winsorized_mean_return_pct": pct(winsorized_mean(values)),
        "stdev_return_pct": pct(volatility),
        "downside_deviation_pct": pct(downside),
        "lcb_return_pct": pct(lower_confidence_bound(values, z=params.lcb_z)),
        "cvar_5_return_pct": pct(cvar_left_tail(values, q=params.cvar_q)),
        "sharpe_like": rounded(safe_ratio(avg, volatility)),
        "sortino_like": rounded(safe_ratio(avg, downside)),
        "profit_factor": rounded(profit_factor(values)),
        "top3_gain_contribution_pct": pct(top_gain_contribution(values, top_n=3)),
        "worst_return_pct": pct(min(values)),
        "best_return_pct": pct(max(values)),
        "p05_return_pct": pct(quantile(values, 0.05)),
        "p10_return_pct": pct(quantile(values, 0.10)),
        "large_loss_20pct_rate_pct": round(100.0 * sum(1 for value in values if value <= -0.20) / len(values), 6),
        "large_loss_40pct_rate_pct": round(100.0 * sum(1 for value in values if value <= -0.40) / len(values), 6),
        "large_gain_20pct_rate_pct": round(100.0 * sum(1 for value in values if value >= 0.20) / len(values), 6),
    }


def selection_quality_summary(
    rows: list[dict[str, Any]],
    ret_key: str,
    *,
    params: CalibrationParams,
) -> dict[str, Any]:
    returns = numeric_values(rows, ret_key)
    summary = summarize_return_risk(returns, params=params)
    summary.update(
        {
            "core_hard_exposure_pct": pct_flag(rows, "diag_core_hard_weakness_flag"),
            "event_hard_exposure_pct": pct_flag(rows, "diag_event_hard_weakness_flag"),
            "soft_exposure_pct": pct_flag(rows, "diag_soft_weakness_flag"),
            "toxic_soft_exposure_pct": pct_flag(rows, "diag_toxic_soft_weakness_flag"),
            "mild_soft_exposure_pct": pct_flag(rows, "diag_mild_soft_weakness_flag"),
            "commercial_risk_overlay_exposure_pct": pct_flag(rows, "diag_commercial_risk_overlay_flag"),
            "commercial_business_shock_exposure_pct": pct_flag(rows, "diag_commercial_business_shock_flag"),
            "evidence_json_missing_exposure_pct": pct_flag(rows, "diag_evidence_json_missing_flag"),
            "mean_evidence_json_quality": mean_numeric(rows, "diag_evidence_json_quality"),
            "illiquid_exposure_pct": pct_flag(rows, "diag_illiquid_flag"),
            "liquidity_ok_pct": pct_flag(rows, "diag_liquidity_ok"),
            "mean_tier1_rank": mean_numeric(rows, "tier1_rank"),
            "mean_tier1_score": mean_numeric(rows, "tier1_score"),
            "mean_tier1_risk_score": mean_numeric(rows, "tier1_risk_score"),
            "mean_multibagger_score": mean_numeric(rows, "multibagger_score"),
            "mean_orthogonal_alpha_score": mean_numeric(rows, "orthogonal_alpha_score"),
            "mean_distinctive_acceleration_score": mean_numeric(rows, "distinctive_acceleration_score"),
            "mean_cash_runway_months": mean_numeric(rows, "diag_cash_runway_months"),
        }
    )
    return summary


def prefixed(prefix: str, values: dict[str, Any]) -> dict[str, Any]:
    return {f"{prefix}{key}": value for key, value in values.items()}


def summary_metric_spread(left: dict[str, Any], right: dict[str, Any], key: str) -> float | str:
    left_value = to_float(left.get(key))
    right_value = to_float(right.get(key))
    return rounded(left_value - right_value) if left_value is not None and right_value is not None else ""


def unprefix(row: dict[str, Any], prefix: str) -> dict[str, Any]:
    return {key[len(prefix) :]: value for key, value in row.items() if key.startswith(prefix)}


def calibration_constraint_fields(
    selected_summary: dict[str, Any],
    *,
    asof_dates: int,
    params: CalibrationParams,
) -> dict[str, Any]:
    reasons: list[str] = []
    n = int(to_float(selected_summary.get("n"), 0.0) or 0.0)
    lcb = to_float(selected_summary.get("lcb_return_pct"))
    sortino = to_float(selected_summary.get("sortino_like"))
    profit = to_float(selected_summary.get("profit_factor"))
    core = to_float(selected_summary.get("core_hard_exposure_pct"))
    event = to_float(selected_summary.get("event_hard_exposure_pct"))
    soft = to_float(selected_summary.get("soft_exposure_pct"))
    evidence_missing = to_float(selected_summary.get("evidence_json_missing_exposure_pct"))
    illiquid = to_float(selected_summary.get("illiquid_exposure_pct"))
    top3 = to_float(selected_summary.get("top3_gain_contribution_pct"))
    loss20 = to_float(selected_summary.get("large_loss_20pct_rate_pct"))
    loss40 = to_float(selected_summary.get("large_loss_40pct_rate_pct"))
    if n < params.min_selected_observations:
        reasons.append(f"n<{params.min_selected_observations}")
    if asof_dates < params.min_asof_dates:
        reasons.append(f"asof_dates<{params.min_asof_dates}")
    if lcb is None or lcb < params.min_net_lcb_return_pct:
        reasons.append(f"lcb<{params.min_net_lcb_return_pct}")
    if sortino is None or sortino < params.min_sortino:
        reasons.append(f"sortino<{params.min_sortino}")
    if profit is None or profit < params.min_profit_factor:
        reasons.append(f"profit_factor<{params.min_profit_factor}")
    if core is not None and core > params.max_core_hard_exposure_pct:
        reasons.append(f"core_hard>{params.max_core_hard_exposure_pct}")
    if event is not None and event > params.max_event_hard_exposure_pct:
        reasons.append(f"event_hard>{params.max_event_hard_exposure_pct}")
    if soft is not None and soft > params.max_soft_exposure_pct:
        reasons.append(f"soft>{params.max_soft_exposure_pct}")
    if evidence_missing is not None and evidence_missing > 0.0:
        reasons.append("evidence_json_missing>0")
    if illiquid is not None and illiquid > params.max_illiquid_exposure_pct:
        reasons.append(f"illiquid>{params.max_illiquid_exposure_pct}")
    if top3 is not None and top3 > params.max_top3_gain_contribution_pct:
        reasons.append(f"top3_concentration>{params.max_top3_gain_contribution_pct}")
    if loss20 is not None and loss20 > params.max_large_loss_20_rate_pct:
        reasons.append(f"loss20>{params.max_large_loss_20_rate_pct}")
    if loss40 is not None and loss40 > params.max_large_loss_40_rate_pct:
        reasons.append(f"loss40>{params.max_large_loss_40_rate_pct}")
    return {
        "calibration_pass": not reasons,
        "calibration_fail_reasons": "|".join(reasons),
    }


def robust_objective(selected: dict[str, Any], baseline: dict[str, Any]) -> float:
    lcb_spread = to_float(summary_metric_spread(selected, baseline, "lcb_return_pct"), 0.0) or 0.0
    sortino_spread = to_float(summary_metric_spread(selected, baseline, "sortino_like"), 0.0) or 0.0
    profit_spread = to_float(summary_metric_spread(selected, baseline, "profit_factor"), 0.0) or 0.0
    mean_spread = to_float(summary_metric_spread(selected, baseline, "mean_return_pct"), 0.0) or 0.0
    p10_spread = to_float(summary_metric_spread(selected, baseline, "p10_return_pct"), 0.0) or 0.0
    cvar_spread = to_float(summary_metric_spread(selected, baseline, "cvar_5_return_pct"), 0.0) or 0.0
    loss20_spread = to_float(summary_metric_spread(selected, baseline, "large_loss_20pct_rate_pct"), 0.0) or 0.0
    loss40_spread = to_float(summary_metric_spread(selected, baseline, "large_loss_40pct_rate_pct"), 0.0) or 0.0
    core_spread = to_float(summary_metric_spread(selected, baseline, "core_hard_exposure_pct"), 0.0) or 0.0
    event_spread = to_float(summary_metric_spread(selected, baseline, "event_hard_exposure_pct"), 0.0) or 0.0
    soft_spread = to_float(summary_metric_spread(selected, baseline, "soft_exposure_pct"), 0.0) or 0.0
    toxic_soft_spread = to_float(summary_metric_spread(selected, baseline, "toxic_soft_exposure_pct"), 0.0) or 0.0
    commercial_shock_spread = (
        to_float(summary_metric_spread(selected, baseline, "commercial_business_shock_exposure_pct"), 0.0) or 0.0
    )
    evidence_missing_spread = (
        to_float(summary_metric_spread(selected, baseline, "evidence_json_missing_exposure_pct"), 0.0) or 0.0
    )
    illiquid_spread = to_float(summary_metric_spread(selected, baseline, "illiquid_exposure_pct"), 0.0) or 0.0
    top3_spread = to_float(summary_metric_spread(selected, baseline, "top3_gain_contribution_pct"), 0.0) or 0.0
    return (
        0.12 * lcb_spread
        + 0.50 * sortino_spread
        + 0.20 * profit_spread
        + 0.01 * mean_spread
        + 0.01 * p10_spread
        + 0.01 * cvar_spread
        - 0.03 * max(0.0, loss20_spread)
        - 0.05 * max(0.0, loss40_spread)
        - 0.10 * max(0.0, core_spread)
        - 0.03 * max(0.0, event_spread)
        - 0.015 * max(0.0, soft_spread)
        - 0.04 * max(0.0, toxic_soft_spread)
        - 0.04 * max(0.0, commercial_shock_spread)
        - 0.10 * max(0.0, evidence_missing_spread)
        - 0.03 * max(0.0, illiquid_spread)
        - 0.01 * max(0.0, top3_spread)
    )


def signal_id(signal: SignalSpec, pool: PoolSpec) -> str:
    payload = json.dumps({"signal": signal.weights, "pool": pool.__dict__}, sort_keys=True)
    return hashlib.sha1(payload.encode("ascii")).hexdigest()[:16]


def signal_score(row: dict[str, Any], signal: SignalSpec) -> tuple[float | None, int, int, float]:
    numerator = 0.0
    denominator = 0.0
    present_count = 0
    missing_count = 0
    for key, weight in signal.weights:
        value = to_float(row.get(key))
        if value is None:
            missing_count += 1
            continue
        numerator += weight * value
        denominator += abs(weight)
        present_count += 1
    if denominator <= 0.0:
        return None, present_count, missing_count, 0.0
    coverage = present_count / max(1, present_count + missing_count)
    return numerator / denominator, present_count, missing_count, coverage


def ranked_by_signal(rows: list[dict[str, Any]], signal: SignalSpec) -> list[dict[str, Any]]:
    candidates: list[tuple[float, str, dict[str, Any]]] = []
    for row in rows:
        score, present_count, missing_count, coverage = signal_score(row, signal)
        if score is None:
            continue
        out = dict(row)
        out["phase2_selection_score"] = round(score, 6)
        out["phase2_score_component_count"] = present_count
        out["phase2_score_missing_component_count"] = missing_count
        out["phase2_score_component_coverage_pct"] = round(100.0 * coverage, 6)
        candidates.append((score, str(row.get("ticker") or ""), out))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [row for _, _, row in candidates]


def tier1_ranked_pool(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = [row for row in rows if to_float(row.get("tier1_score")) is not None]
    candidates.sort(key=lambda row: (numeric_or_default(row.get("tier1_score"), -1e9), str(row.get("ticker") or "")), reverse=True)
    return candidates


def apply_pool(rows: list[dict[str, Any]], pool: PoolSpec, ret_key: str) -> list[dict[str, Any]]:
    eligible = [row for row in rows if to_float(row.get(ret_key)) is not None]
    if pool.exclude_core_hard:
        eligible = [
            row
            for row in eligible
            if (to_float(row.get("diag_core_hard_weakness_flag"), to_float(row.get("diag_core_hard_flag"), 0.0)) or 0.0)
            <= 0.0
        ]
    if pool.require_liquidity:
        eligible = [row for row in eligible if (to_float(row.get("diag_liquidity_ok"), 0.0) or 0.0) >= 1.0]
    if pool.max_tier1_risk_score is not None:
        eligible = [
            row
            for row in eligible
            if numeric_or_default(row.get("tier1_risk_score"), 999.0) <= pool.max_tier1_risk_score
        ]
    ranked = tier1_ranked_pool(eligible)
    if pool.exclude_tier1_top_k > 0:
        ranked = ranked[pool.exclude_tier1_top_k :]
    if pool.tier1_top_k > 0:
        ranked = ranked[: pool.tier1_top_k]
    return ranked


def build_signal_specs(config: dict[str, Any]) -> list[SignalSpec]:
    raw_specs = cfg_get(config, "calibration.phase2.signal_specs", None)
    specs: list[SignalSpec] = []
    if isinstance(raw_specs, list):
        for idx, raw in enumerate(raw_specs, start=1):
            if not isinstance(raw, dict):
                continue
            weights_raw = raw.get("weights", {})
            if not isinstance(weights_raw, dict):
                continue
            weights = tuple((f"pct_{key}", float(value)) for key, value in sorted(weights_raw.items()))
            specs.append(
                SignalSpec(
                    signal_name=str(raw.get("signal_name") or raw.get("name") or f"custom_signal_{idx}"),
                    description=str(raw.get("description") or "Custom Phase 2 signal."),
                    weights=weights,
                )
            )
        if specs:
            return specs
    return [
        SignalSpec("tier1_baseline", "Tier-1 opportunity percentile baseline.", (("pct_tier1_score", 1.0),)),
        SignalSpec("multibagger_only", "Standalone multibagger percentile.", (("pct_multibagger_score", 1.0),)),
        SignalSpec("base_multibagger_only", "Standalone base multibagger percentile.", (("pct_base_multibagger_score", 1.0),)),
        SignalSpec("orthogonal_alpha_only", "Standalone orthogonal alpha percentile.", (("pct_orthogonal_alpha_score", 1.0),)),
        SignalSpec(
            "distinctive_acceleration_only",
            "Standalone distinctive acceleration percentile.",
            (("pct_distinctive_acceleration_score", 1.0),),
        ),
        SignalSpec(
            "multibagger_orthogonal_50_50",
            "Equal-weight multibagger and orthogonal alpha.",
            (("pct_multibagger_score", 0.50), ("pct_orthogonal_alpha_score", 0.50)),
        ),
        SignalSpec(
            "orthogonal_distinctive_50_50",
            "Equal-weight orthogonal alpha and distinctive acceleration.",
            (("pct_orthogonal_alpha_score", 0.50), ("pct_distinctive_acceleration_score", 0.50)),
        ),
        SignalSpec(
            "tier1_80_multibagger_20",
            "Controlled multibagger overlay on Tier-1.",
            (("pct_tier1_score", 0.80), ("pct_multibagger_score", 0.20)),
        ),
        SignalSpec(
            "tier1_80_orthogonal_20",
            "Controlled orthogonal alpha overlay on Tier-1.",
            (("pct_tier1_score", 0.80), ("pct_orthogonal_alpha_score", 0.20)),
        ),
        SignalSpec(
            "tier1_70_multibagger_orthogonal_30",
            "Tier-1 anchor with multibagger and orthogonal alpha overlay.",
            (("pct_tier1_score", 0.70), ("pct_multibagger_score", 0.15), ("pct_orthogonal_alpha_score", 0.15)),
        ),
    ]


def build_pool_specs(config: dict[str, Any]) -> list[PoolSpec]:
    raw_specs = cfg_get(config, "calibration.phase2.pool_specs", None)
    specs: list[PoolSpec] = []
    if isinstance(raw_specs, list):
        for idx, raw in enumerate(raw_specs, start=1):
            if not isinstance(raw, dict):
                continue
            max_risk_raw = raw.get("max_tier1_risk_score", None)
            max_risk = to_float(max_risk_raw) if max_risk_raw not in {None, ""} else None
            specs.append(
                PoolSpec(
                    pool_name=str(raw.get("pool_name") or raw.get("name") or f"custom_pool_{idx}"),
                    description=str(raw.get("description") or "Custom Phase 2 candidate pool."),
                    exclude_core_hard=as_bool(raw.get("exclude_core_hard", True), True),
                    require_liquidity=as_bool(raw.get("require_liquidity", False), False),
                    tier1_top_k=int(to_float(raw.get("tier1_top_k"), 0.0) or 0),
                    exclude_tier1_top_k=int(to_float(raw.get("exclude_tier1_top_k"), 0.0) or 0),
                    max_tier1_risk_score=max_risk,
                )
            )
        if specs:
            return specs
    return [
        PoolSpec(
            "tier1_top20_rerank_pool",
            "Names already in the Tier-1 Top 20 investable pool; useful for Top-10 reranking tests.",
            tier1_top_k=20,
        ),
        PoolSpec(
            "tier1_top50_overlay_pool",
            "Investable Tier-1 Top 50 pool; tests whether alpha promotes names into Top-N.",
            tier1_top_k=50,
        ),
        PoolSpec(
            "tier1_top100_overlay_pool",
            "Investable Tier-1 Top 100 pool; broad overlay promotion test.",
            tier1_top_k=100,
        ),
        PoolSpec(
            "speculative_outside_tier1_top20",
            "Non-core-veto names outside Tier-1 Top 20; separate speculative basket test.",
            exclude_tier1_top_k=20,
            require_liquidity=True,
        ),
        PoolSpec(
            "speculative_outside_tier1_top50",
            "Non-core-veto names outside Tier-1 Top 50; more speculative basket test.",
            exclude_tier1_top_k=50,
            require_liquidity=True,
        ),
    ]


def minimum_calendar_embargo_days_for_horizon(
    horizon_bars: int,
    *,
    buffer_days: int = DEFAULT_EMBARGO_BUFFER_CALENDAR_DAYS,
) -> int:
    """Convert a trading-bar horizon into the calendar-day embargo needed to avoid split leakage."""
    return int(
        math.ceil(max(0, int(horizon_bars)) * CALENDAR_DAYS_PER_YEAR / TRADING_BARS_PER_CALENDAR_YEAR)
        + max(0, int(buffer_days))
    )


def split_rows_by_completed_return_date(
    rows: list[dict[str, Any]],
    *,
    horizon: int,
    train_fraction: float,
    embargo_days: int = 0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], list[str]]:
    ret_key = f"fwd_{horizon}d_net_return"
    eligible_dates = sorted(
        {
            str(row.get("asof_date") or "")
            for row in rows
            if str(row.get("asof_date") or "") and to_float(row.get(ret_key)) is not None
        }
    )
    all_dates = sorted({str(row.get("asof_date") or "") for row in rows if str(row.get("asof_date") or "")})
    dropped_dates = sorted(set(all_dates) - set(eligible_dates))
    if dropped_dates:
        LOGGER.warning(
            "Horizon %sd: dropped %d/%d as-of dates with no completed forward returns; first=%s last=%s",
            horizon,
            len(dropped_dates),
            len(all_dates),
            dropped_dates[0],
            dropped_dates[-1],
        )
    if len(eligible_dates) < 2:
        LOGGER.warning(
            "Horizon %sd: fewer than two eligible dates with completed forward returns; test set will be empty.",
            horizon,
        )
        eligible = set(eligible_dates)
        return [row for row in rows if str(row.get("asof_date") or "") in eligible], [], eligible_dates, []
    bounded = max(0.10, min(0.90, float(train_fraction)))
    split_idx = int(math.floor(len(eligible_dates) * bounded))
    split_idx = max(1, min(len(eligible_dates) - 1, split_idx))
    train_dates = eligible_dates[:split_idx]
    test_dates = eligible_dates[split_idx:]
    if embargo_days > 0 and train_dates and test_dates:
        first_test_date = parse_date(test_dates[0])
        if first_test_date is not None:
            embargo_start = first_test_date - timedelta(days=int(embargo_days))
            kept_train_dates = [
                text for text in train_dates if (parsed := parse_date(text)) is not None and parsed < embargo_start
            ]
            dropped_count = len(train_dates) - len(kept_train_dates)
            if dropped_count:
                LOGGER.info(
                    "Horizon %sd: embargoed %d train as-of dates within %d calendar days before first test date %s.",
                    horizon,
                    dropped_count,
                    embargo_days,
                    test_dates[0],
                )
            train_dates = kept_train_dates
    train_set = set(train_dates)
    test_set = set(test_dates)
    return (
        [row for row in rows if str(row.get("asof_date") or "") in train_set],
        [row for row in rows if str(row.get("asof_date") or "") in test_set],
        train_dates,
        test_dates,
    )


def run_indexed_jobs(
    jobs: list[Any],
    worker: Callable[[Any], T],
    *,
    max_workers: int,
    job_label: str,
) -> list[T]:
    if max_workers <= 1 or len(jobs) <= 1:
        return [worker(job) for job in jobs]
    results: dict[int, T] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(worker, job): int(getattr(job, "index")) for job in jobs}
        done, pending = wait(futures, return_when=FIRST_EXCEPTION)
        first_exception: BaseException | None = None
        for future in done:
            idx = futures[future]
            try:
                results[idx] = future.result()
            except Exception as exc:
                LOGGER.exception("%s job failed: index=%s", job_label, idx)
                first_exception = exc
                break
        if first_exception is not None:
            for future in pending:
                future.cancel()
            raise first_exception
        for future in pending:
            idx = futures[future]
            results[idx] = future.result()
    return [results[idx] for idx in sorted(results)]


def build_grid_row(
    rows_by_date: dict[str, list[dict[str, Any]]],
    job: GridJob,
    *,
    sample: str,
    evaluation_split: str,
    params: CalibrationParams,
) -> dict[str, Any]:
    ret_key = f"fwd_{job.horizon}d_net_return"
    selected_rows: list[dict[str, Any]] = []
    baseline_rows: list[dict[str, Any]] = []
    selected_counts: list[int] = []
    baseline_counts: list[int] = []
    date_count = 0
    for asof_date in sorted(rows_by_date):
        pool_rows = apply_pool(rows_by_date[asof_date], job.pool, ret_key)
        if not pool_rows:
            continue
        selected = ranked_by_signal(pool_rows, job.signal)[: job.top_n]
        baseline = ranked_by_signal(pool_rows, SignalSpec("tier1_baseline", "", (("pct_tier1_score", 1.0),)))[: job.top_n]
        if not selected or not baseline:
            continue
        date_count += 1
        selected_counts.append(len(selected))
        baseline_counts.append(len(baseline))
        selected_rows.extend(selected)
        baseline_rows.extend(baseline)

    selected_summary = selection_quality_summary(selected_rows, ret_key, params=params)
    baseline_summary = selection_quality_summary(baseline_rows, ret_key, params=params)
    constraints = calibration_constraint_fields(selected_summary, asof_dates=date_count, params=params)
    selected_unprefixed = selected_summary
    baseline_unprefixed = baseline_summary
    objective = robust_objective(selected_unprefixed, baseline_unprefixed)
    candidate_id = f"phase2_{signal_id(job.signal, job.pool)}"
    return {
        "sample": sample,
        "evaluation_split": evaluation_split,
        "horizon_days": job.horizon,
        "horizon_unit": "trading_bars",
        "top_n": job.top_n,
        "return_basis": "net_after_round_trip_costs",
        "round_trip_cost_bps": params.round_trip_cost_bps,
        "candidate_id": candidate_id,
        "signal_name": job.signal.signal_name,
        "signal_description": job.signal.description,
        "signal_weights_json": json.dumps(dict(job.signal.weights), sort_keys=True),
        "pool_name": job.pool.pool_name,
        "pool_description": job.pool.description,
        "pool_exclude_core_hard": job.pool.exclude_core_hard,
        "pool_require_liquidity": job.pool.require_liquidity,
        "pool_tier1_top_k": job.pool.tier1_top_k,
        "pool_exclude_tier1_top_k": job.pool.exclude_tier1_top_k,
        "pool_max_tier1_risk_score": "" if job.pool.max_tier1_risk_score is None else job.pool.max_tier1_risk_score,
        "asof_dates": date_count,
        "avg_selected_names_per_date": rounded(mean([float(value) for value in selected_counts])),
        "avg_baseline_names_per_date": rounded(mean([float(value) for value in baseline_counts])),
        "calibration_objective_vs_tier1_baseline": rounded(objective),
        **constraints,
        **prefixed("selected_", selected_summary),
        **prefixed("tier1_baseline_", baseline_summary),
        **{
            f"selected_minus_tier1_baseline_{key}": summary_metric_spread(
                selected_summary,
                baseline_summary,
                key,
            )
            for key in SPREAD_KEYS
        },
    }


def build_grid_rows(
    rows: list[dict[str, Any]],
    signals: list[SignalSpec],
    pools: list[PoolSpec],
    horizons: list[int],
    top_ns: list[int],
    *,
    sample: str,
    evaluation_split: str,
    params: CalibrationParams,
    max_workers: int,
) -> list[dict[str, Any]]:
    rows_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_date[str(row.get("asof_date") or "")].append(row)
    jobs: list[GridJob] = []
    index = 0
    for horizon in horizons:
        for top_n in top_ns:
            for pool in pools:
                for signal in signals:
                    jobs.append(GridJob(index=index, horizon=horizon, top_n=top_n, signal=signal, pool=pool))
                    index += 1

    def worker(job: Any) -> dict[str, Any]:
        if not isinstance(job, GridJob):
            raise TypeError(f"Expected GridJob, got {type(job).__name__}")
        return build_grid_row(rows_by_date, job, sample=sample, evaluation_split=evaluation_split, params=params)

    return run_indexed_jobs(jobs, worker, max_workers=max_workers, job_label=f"phase2_grid:{sample}:{evaluation_split}")


def numeric_or_default(raw: object, default: float) -> float:
    value = to_float(raw)
    return value if value is not None else default


def calibration_sort_tuple(row: dict[str, Any]) -> tuple[float, float, float, float, float, float, float]:
    passed = 1.0 if as_bool(row.get("calibration_pass")) else 0.0
    objective = numeric_or_default(row.get("calibration_objective_vs_tier1_baseline"), -1e9)
    lcb = numeric_or_default(row.get("selected_lcb_return_pct"), -1e9)
    sortino = numeric_or_default(row.get("selected_sortino_like"), -1e9)
    profit = numeric_or_default(row.get("selected_profit_factor"), -1e9)
    core = numeric_or_default(row.get("selected_core_hard_exposure_pct"), 100.0)
    loss20 = numeric_or_default(row.get("selected_large_loss_20pct_rate_pct"), 100.0)
    return (passed, objective, lcb, sortino, profit, -core, -loss20)


def build_best_rows(grid_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    group_keys = ["sample", "evaluation_split", "horizon_days", "top_n", "pool_name"]
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in grid_rows:
        grouped[tuple(str(row.get(key) or "") for key in group_keys)].append(row)
    for key, rows_for_group in sorted(grouped.items()):
        ranked = sorted(rows_for_group, key=calibration_sort_tuple, reverse=True)
        for rank, row in enumerate(ranked[:15], start=1):
            out.append({"scope": "horizon", "rank": rank, **{group_keys[i]: key[i] for i in range(len(group_keys))}, **row})
    return out


def build_holdout_rows(train_rows: list[dict[str, Any]], test_rows: list[dict[str, Any]], *, top_k: int) -> list[dict[str, Any]]:
    group_keys = ["sample", "horizon_days", "top_n", "pool_name"]
    train_grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    test_by_candidate: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in train_rows:
        train_grouped[tuple(str(row.get(key) or "") for key in group_keys)].append(row)
    for row in test_rows:
        key = tuple(str(row.get(key) or "") for key in [*group_keys, "candidate_id"])
        test_by_candidate[key] = row
    out: list[dict[str, Any]] = []
    for key, group in sorted(train_grouped.items()):
        ranked = sorted(group, key=calibration_sort_tuple, reverse=True)
        for train_rank, train in enumerate(ranked[:top_k], start=1):
            test_key = (*key, str(train.get("candidate_id") or ""))
            test = test_by_candidate.get(test_key, {})
            out.append(
                {
                    "train_rank": train_rank,
                    "sample": key[0],
                    "horizon_days": key[1],
                    "top_n": key[2],
                    "pool_name": key[3],
                    "candidate_id": train.get("candidate_id", ""),
                    "signal_name": train.get("signal_name", ""),
                    "signal_description": train.get("signal_description", ""),
                    "pool_description": train.get("pool_description", ""),
                    "train_calibration_pass": train.get("calibration_pass", ""),
                    "test_calibration_pass": test.get("calibration_pass", ""),
                    "train_calibration_objective_vs_tier1_baseline": train.get(
                        "calibration_objective_vs_tier1_baseline", ""
                    ),
                    "test_calibration_objective_vs_tier1_baseline": test.get(
                        "calibration_objective_vs_tier1_baseline", ""
                    ),
                    **prefixed("train_", unprefix(train, "selected_")),
                    **prefixed("test_", unprefix(test, "selected_") if test else {}),
                    "signal_weights_json": train.get("signal_weights_json", ""),
                    "pool_tier1_top_k": train.get("pool_tier1_top_k", ""),
                    "pool_exclude_tier1_top_k": train.get("pool_exclude_tier1_top_k", ""),
                }
            )
    return out


def selected_rows_by_date(
    rows: list[dict[str, Any]],
    signal: SignalSpec,
    pool: PoolSpec,
    *,
    horizon: int,
    top_n: int,
) -> list[dict[str, Any]]:
    ret_key = f"fwd_{horizon}d_net_return"
    rows_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_date[str(row.get("asof_date") or "")].append(row)
    out: list[dict[str, Any]] = []
    for asof_date in sorted(rows_by_date):
        selected = ranked_by_signal(apply_pool(rows_by_date[asof_date], pool, ret_key), signal)[:top_n]
        for rank, row in enumerate(selected, start=1):
            out.append({"phase2_selected_rank_within_date": rank, **row})
    return out


def signal_from_row(row: dict[str, Any]) -> SignalSpec | None:
    try:
        payload = json.loads(str(row.get("signal_weights_json") or "{}"))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return SignalSpec(
        signal_name=str(row.get("signal_name") or ""),
        description=str(row.get("signal_description") or ""),
        weights=tuple((str(key), float(value)) for key, value in sorted(payload.items())),
    )


def pool_from_row(row: dict[str, Any]) -> PoolSpec:
    max_risk = to_float(row.get("pool_max_tier1_risk_score"))
    return PoolSpec(
        pool_name=str(row.get("pool_name") or ""),
        description=str(row.get("pool_description") or ""),
        exclude_core_hard=as_bool(row.get("pool_exclude_core_hard"), True),
        require_liquidity=as_bool(row.get("pool_require_liquidity"), False),
        tier1_top_k=int(to_float(row.get("pool_tier1_top_k"), 0.0) or 0),
        exclude_tier1_top_k=int(to_float(row.get("pool_exclude_tier1_top_k"), 0.0) or 0),
        max_tier1_risk_score=max_risk,
    )


def build_selected_ticker_rows(
    split_rows_by_key: dict[tuple[str, str, int], list[dict[str, Any]]],
    holdout_rows: list[dict[str, Any]],
    *,
    top_train_ranks: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int, str, str]] = set()
    for holdout in holdout_rows:
        train_rank = int(to_float(holdout.get("train_rank"), 0.0) or 0)
        if train_rank <= 0 or train_rank > top_train_ranks:
            continue
        signal = signal_from_row(holdout)
        if signal is None:
            continue
        pool = pool_from_row(holdout)
        sample = str(holdout.get("sample") or "")
        horizon = int(to_float(holdout.get("horizon_days"), 0.0) or 0)
        top_n = int(to_float(holdout.get("top_n"), 0.0) or 0)
        candidate_id = str(holdout.get("candidate_id") or "")
        if not sample or horizon <= 0 or top_n <= 0 or not candidate_id:
            continue
        for split in ["train", "test"]:
            key = (sample, split, horizon, pool.pool_name, candidate_id)
            if key in seen:
                continue
            seen.add(key)
            for row in selected_rows_by_date(
                split_rows_by_key.get((sample, split, horizon), []),
                signal,
                pool,
                horizon=horizon,
                top_n=top_n,
            ):
                ret_key = f"fwd_{horizon}d_net_return"
                out.append(
                    {
                        "sample": sample,
                        "evaluation_split": split,
                        "horizon_days": horizon,
                        "top_n": top_n,
                        "train_rank": train_rank,
                        "candidate_id": candidate_id,
                        "signal_name": signal.signal_name,
                        "pool_name": pool.pool_name,
                        "asof_date": row.get("asof_date", ""),
                        "phase2_selected_rank_within_date": row.get("phase2_selected_rank_within_date", ""),
                        "ticker": row.get("ticker", ""),
                        "company_name": row.get("company_name", ""),
                        "phase2_selection_score": row.get("phase2_selection_score", ""),
                        "phase2_score_component_count": row.get("phase2_score_component_count", ""),
                        "phase2_score_missing_component_count": row.get("phase2_score_missing_component_count", ""),
                        "phase2_score_component_coverage_pct": row.get("phase2_score_component_coverage_pct", ""),
                        "tier1_rank": row.get("tier1_rank", ""),
                        "tier1_score": row.get("tier1_score", ""),
                        "tier1_risk_score": row.get("tier1_risk_score", ""),
                        "multibagger_score": row.get("multibagger_score", ""),
                        "orthogonal_alpha_score": row.get("orthogonal_alpha_score", ""),
                        "distinctive_acceleration_score": row.get("distinctive_acceleration_score", ""),
                        "net_forward_return_pct": pct(to_float(row.get(ret_key))),
                        "core_hard_weakness_flag": row.get("diag_core_hard_weakness_flag", ""),
                        "core_hard_weakness_reasons": row.get("diag_core_hard_weakness_reasons", ""),
                        "event_hard_weakness_flag": row.get("diag_event_hard_weakness_flag", ""),
                        "event_hard_weakness_reasons": row.get("diag_event_hard_weakness_reasons", ""),
                        "soft_weakness_flag": row.get("diag_soft_weakness_flag", ""),
                        "soft_weakness_reasons": row.get("diag_soft_weakness_reasons", ""),
                        "toxic_soft_weakness_flag": row.get("diag_toxic_soft_weakness_flag", ""),
                        "toxic_soft_weakness_reasons": row.get("diag_toxic_soft_weakness_reasons", ""),
                        "mild_soft_weakness_flag": row.get("diag_mild_soft_weakness_flag", ""),
                        "mild_soft_weakness_reasons": row.get("diag_mild_soft_weakness_reasons", ""),
                        "commercial_risk_source": row.get("diag_commercial_risk_source", ""),
                        "commercial_risk_overlay_score": row.get("diag_commercial_risk_overlay_score", ""),
                        "commercial_risk_overlay_flag": row.get("diag_commercial_risk_overlay_flag", ""),
                        "commercial_risk_overlay_reasons": row.get("diag_commercial_risk_overlay_reasons", ""),
                        "commercial_deterioration_score": row.get("diag_commercial_deterioration_score", ""),
                        "commercial_deterioration_reasons": row.get("diag_commercial_deterioration_reasons", ""),
                        "valuation_growth_mismatch_score": row.get("diag_valuation_growth_mismatch_score", ""),
                        "valuation_growth_mismatch_reasons": row.get("diag_valuation_growth_mismatch_reasons", ""),
                        "transient_revenue_anchor_score": row.get("diag_transient_revenue_anchor_score", ""),
                        "transient_revenue_anchor_reasons": row.get("diag_transient_revenue_anchor_reasons", ""),
                        "commercial_business_shock_score": row.get("diag_commercial_business_shock_score", ""),
                        "commercial_business_shock_flag": row.get("diag_commercial_business_shock_flag", ""),
                        "commercial_business_shock_reasons": row.get("diag_commercial_business_shock_reasons", ""),
                        "evidence_json_quality": row.get("diag_evidence_json_quality", ""),
                        "liquidity_ok": row.get("diag_liquidity_ok", ""),
                        "avg_dollar_volume_20d": row.get("diag_avg_dollar_volume_20d", ""),
                    }
                )
    return out


def split_reason_tokens(raw: object) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    return [part.strip() for part in text.split("|") if part.strip()]


def build_phase2_weakness_component_rows(selected_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    group_keys = [
        "sample",
        "evaluation_split",
        "horizon_days",
        "top_n",
        "train_rank",
        "candidate_id",
        "signal_name",
        "pool_name",
    ]
    grouped: dict[tuple[str, ...], dict[str, Any]] = defaultdict(
        lambda: {"selected_n": 0, "reason_counts": defaultdict(int)}
    )
    for row in selected_rows:
        key = tuple(str(row.get(group_key) or "") for group_key in group_keys)
        grouped[key]["selected_n"] += 1
        for severity, reason_key in [
            ("core_hard", "core_hard_weakness_reasons"),
            ("event_hard", "event_hard_weakness_reasons"),
            ("soft", "soft_weakness_reasons"),
            ("toxic_soft", "toxic_soft_weakness_reasons"),
            ("mild_soft", "mild_soft_weakness_reasons"),
        ]:
            for reason in split_reason_tokens(row.get(reason_key)):
                grouped[key]["reason_counts"][(severity, reason)] += 1
    out: list[dict[str, Any]] = []
    for key, payload in sorted(grouped.items()):
        selected_n = int(payload["selected_n"])
        reason_counts = payload["reason_counts"]
        for (severity, reason), count in sorted(reason_counts.items(), key=lambda item: (-item[1], item[0])):
            record: dict[str, Any] = {group_keys[index]: key[index] for index in range(len(group_keys))}
            record.update(
                {
                    "weakness_severity": severity,
                    "weakness_reason": reason,
                    "reason_count": count,
                    "selected_n": selected_n,
                    "reason_exposure_pct": round(100.0 * count / selected_n, 6) if selected_n else "",
                }
            )
            out.append(record)
    return out


def selected_returns_by_date(
    rows: list[dict[str, Any]],
    signal: SignalSpec,
    pool: PoolSpec,
    *,
    horizon: int,
    top_n: int,
) -> dict[str, list[float]]:
    ret_key = f"fwd_{horizon}d_net_return"
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("asof_date") or "")].append(row)
    out: dict[str, list[float]] = {}
    for asof_date in sorted(grouped):
        selected = ranked_by_signal(apply_pool(grouped[asof_date], pool, ret_key), signal)[:top_n]
        returns = numeric_values(selected, ret_key)
        if returns:
            out[asof_date] = returns
    return out


def deterministic_bootstrap_seed(*, base_seed: int, parts: list[object]) -> int:
    text = "|".join([str(base_seed), *[str(part) for part in parts]])
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    return int(digest, 16)


def bootstrap_intervals(
    returns_by_date: dict[str, list[float]],
    *,
    params: CalibrationParams,
    iterations: int,
    seed: int,
) -> dict[str, Any]:
    dates = sorted(returns_by_date)
    if not dates or iterations <= 0:
        return {}
    rng = random.Random(seed)
    draws: dict[str, list[float]] = {key: [] for key in BOOTSTRAP_METRIC_KEYS}
    for _ in range(iterations):
        sampled_dates = [rng.choice(dates) for _ in dates]
        returns = [ret for day in sampled_dates for ret in returns_by_date.get(day, [])]
        summary = summarize_return_risk(returns, params=params)
        for key in BOOTSTRAP_METRIC_KEYS:
            value = to_float(summary.get(key))
            if value is not None:
                draws[key].append(value)
    out: dict[str, Any] = {"bootstrap_iterations": iterations}
    for key, values in draws.items():
        out[f"{key}_ci05"] = rounded(quantile(values, 0.05))
        out[f"{key}_ci95"] = rounded(quantile(values, 0.95))
    return out


def build_bootstrap_rows(
    split_rows_by_key: dict[tuple[str, str, int], list[dict[str, Any]]],
    holdout_rows: list[dict[str, Any]],
    *,
    params: CalibrationParams,
    top_k: int,
    iterations: int,
    seed: int,
) -> list[dict[str, Any]]:
    if iterations <= 0:
        return []
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int, str, str, str]] = set()
    for row in holdout_rows:
        train_rank = int(to_float(row.get("train_rank"), 0.0) or 0)
        if train_rank <= 0 or train_rank > top_k:
            continue
        signal = signal_from_row(row)
        if signal is None:
            continue
        pool = pool_from_row(row)
        sample = str(row.get("sample") or "")
        horizon = int(to_float(row.get("horizon_days"), 0.0) or 0)
        top_n = int(to_float(row.get("top_n"), 0.0) or 0)
        candidate_id = str(row.get("candidate_id") or "")
        for split in ["train", "test"]:
            key = (sample, split, horizon, str(top_n), pool.pool_name, candidate_id)
            if key in seen or not sample or horizon <= 0 or top_n <= 0:
                continue
            seen.add(key)
            rows = split_rows_by_key.get((sample, split, horizon), [])
            returns_by_date = selected_returns_by_date(rows, signal, pool, horizon=horizon, top_n=top_n)
            out.append(
                {
                    "sample": sample,
                    "evaluation_split": split,
                    "horizon_days": horizon,
                    "top_n": top_n,
                    "train_rank": train_rank,
                    "candidate_id": candidate_id,
                    "signal_name": signal.signal_name,
                    "pool_name": pool.pool_name,
                    "asof_dates": len(returns_by_date),
                    **bootstrap_intervals(
                        returns_by_date,
                        params=params,
                        iterations=iterations,
                        seed=deterministic_bootstrap_seed(
                            base_seed=seed,
                            parts=[sample, split, horizon, top_n, pool.pool_name, candidate_id],
                        ),
                    ),
                }
            )
    return out


def numeric_pairs(rows: Iterable[dict[str, Any]], x_key: str, y_key: str) -> list[tuple[float, float]]:
    pairs: list[tuple[float, float]] = []
    for row in rows:
        x = to_float(row.get(x_key))
        y = to_float(row.get(y_key))
        if x is not None and y is not None:
            pairs.append((x, y))
    return pairs


def pearson_from_pairs(pairs: list[tuple[float, float]]) -> float | None:
    n = len(pairs)
    if n < 3:
        return None
    xs = [pair[0] for pair in pairs]
    ys = [pair[1] for pair in pairs]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    denom = math.sqrt(var_x * var_y)
    return None if denom <= 0.0 else cov / denom


def ranks(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    out = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and indexed[j + 1][1] == indexed[i][1]:
            j += 1
        rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[indexed[k][0]] = rank
        i = j + 1
    return out


def spearman_from_pairs(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 3:
        return None
    xs = [pair[0] for pair in pairs]
    ys = [pair[1] for pair in pairs]
    return pearson_from_pairs(list(zip(ranks(xs), ranks(ys))))


def residual_pairs(rows: list[dict[str, Any]], predictor_key: str, y_key: str, score_key: str) -> list[tuple[float, float]]:
    base = numeric_pairs(rows, predictor_key, y_key)
    if len(base) < 3:
        return []
    xs = [pair[0] for pair in base]
    ys = [pair[1] for pair in base]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    var_x = sum((x - mean_x) ** 2 for x in xs)
    if var_x <= 0.0:
        return []
    slope = sum((x - mean_x) * (y - mean_y) for x, y in base) / var_x
    intercept = mean_y - slope * mean_x
    out: list[tuple[float, float]] = []
    for row in rows:
        x = to_float(row.get(predictor_key))
        y = to_float(row.get(y_key))
        score = to_float(row.get(score_key))
        if x is None or y is None or score is None:
            continue
        out.append((score, y - (intercept + slope * x)))
    return out


def build_orthogonality_rows(
    rows: list[dict[str, Any]],
    horizons: list[int],
    *,
    sample: str,
    evaluation_split: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    score_keys = [
        "multibagger_score",
        "base_multibagger_score",
        "orthogonal_alpha_score",
        "distinctive_acceleration_score",
    ]
    for score_key in score_keys:
        pairs = numeric_pairs(rows, "tier1_score", score_key)
        out.append(
            {
                "scope": "score_vs_tier1",
                "sample": sample,
                "evaluation_split": evaluation_split,
                "horizon_days": "",
                "score_column": score_key,
                "n": len(pairs),
                "pearson_vs_tier1": rounded(pearson_from_pairs(pairs)),
                "spearman_vs_tier1": rounded(spearman_from_pairs(pairs)),
            }
        )
    for horizon in horizons:
        ret_key = f"fwd_{horizon}d_net_return"
        for score_key in score_keys:
            pairs = numeric_pairs(rows, score_key, ret_key)
            residual = residual_pairs(rows, "tier1_score", ret_key, score_key)
            out.append(
                {
                    "scope": "score_vs_return",
                    "sample": sample,
                    "evaluation_split": evaluation_split,
                    "horizon_days": horizon,
                    "score_column": score_key,
                    "n": len(pairs),
                    "pearson_score_return": rounded(pearson_from_pairs(pairs)),
                    "spearman_score_return": rounded(spearman_from_pairs(pairs)),
                    "n_residual": len(residual),
                    "pearson_score_residual_return_after_tier1": rounded(pearson_from_pairs(residual)),
                    "spearman_score_residual_return_after_tier1": rounded(spearman_from_pairs(residual)),
                }
            )
    return out


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")


def liquidity_ok_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if (to_float(row.get("diag_liquidity_ok"), 0.0) or 0.0) >= 1.0]


def main() -> None:
    configure_logging()
    args = parse_args()
    start_time = time.perf_counter()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(
            cfg_get(config, "calibration.phase2.output_dir", "../output/biotech_index_reports/calibration_phase2_speculative_alpha"),
            base_dir=base_dir,
        )
    )
    start_asof = parse_date(args.start_asof)
    end_asof = parse_date(args.end_asof)
    if args.start_asof and start_asof is None:
        raise ValueError(f"Invalid --start-asof date: {args.start_asof}")
    if args.end_asof and end_asof is None:
        raise ValueError(f"Invalid --end-asof date: {args.end_asof}")
    horizons = parse_int_list(args.horizons, default=[20, 60, 120])
    top_ns = parse_int_list(args.top_n, default=[10, 20, 30])
    market_sources = [
        token.strip()
        for raw in normalize_string_list(args.market_sources, calibration_market_sources(config))
        for token in str(raw).split(",")
        if token.strip()
    ]
    params = load_calibration_params(config)
    train_fraction = float(
        args.train_fraction
        if args.train_fraction is not None
        else cfg_get(config, "calibration.phase2.train_fraction", 0.70)
    )
    max_workers = int(
        args.max_workers
        if args.max_workers is not None
        else cfg_get(config, "calibration.phase2.max_workers", max(1, min(8, os.cpu_count() or 1)))
    )
    bootstrap_iterations = int(
        args.bootstrap_iterations
        if args.bootstrap_iterations is not None
        else cfg_get(config, "calibration.phase2.bootstrap_iterations", DEFAULT_BOOTSTRAP_ITERATIONS)
    )
    bootstrap_top_k = int(
        args.bootstrap_top_k
        if args.bootstrap_top_k is not None
        else cfg_get(config, "calibration.phase2.bootstrap_top_k", DEFAULT_BOOTSTRAP_TOP_K)
    )
    holdout_top_k = int(
        args.holdout_top_k
        if args.holdout_top_k is not None
        else cfg_get(config, "calibration.phase2.holdout_top_k", DEFAULT_HOLDOUT_TOP_K)
    )
    holdout_top_k = max(1, holdout_top_k)
    bootstrap_seed = int(
        args.bootstrap_seed
        if args.bootstrap_seed is not None
        else cfg_get(config, "calibration.phase2.bootstrap_seed", DEFAULT_BOOTSTRAP_SEED)
    )
    selected_ticker_top_ranks = int(
        args.selected_ticker_top_ranks
        if args.selected_ticker_top_ranks is not None
        else cfg_get(config, "calibration.phase2.selected_ticker_top_ranks", DEFAULT_SELECTED_TICKER_TOP_RANKS)
    )
    next_bar_entry = (
        bool(args.next_bar_entry)
        if args.next_bar_entry is not None
        else as_bool(cfg_get(config, "calibration.phase2.next_bar_entry", True), True)
    )
    exclude_current_removals = (
        bool(args.exclude_current_removals)
        if args.exclude_current_removals is not None
        else as_bool(cfg_get(config, "calibration.phase2.exclude_current_removals", False), False)
    )
    extra_exclusions = parse_string_set(args.exclude_tickers) | parse_string_set(cfg_get(config, "calibration.exclude_tickers", []))
    min_addv20 = float(cfg_get(config, "multibagger.min_addv20", 1_000_000.0))
    commercial_risk_settings = dict(cfg_get(config, "biotech_scoring.commercial_risk_overlay", {}) or {})
    commercial_risk_settings.setdefault(
        "commercial_fragility_threshold",
        float(cfg_get(config, "biotech_scoring.production_policy.commercial_fragility_threshold", 70.0)),
    )
    commercial_risk_settings.setdefault(
        "commercial_stage_revenue_min",
        float(cfg_get(config, "commercial_value.commercial_stage_revenue_min", 50_000_000.0)),
    )
    signals = build_signal_specs(config)
    pools = build_pool_specs(config)

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        snapshot_dates = load_snapshot_dates(
            conn,
            start_asof=start_asof,
            end_asof=end_asof,
            fridays_only=not args.include_non_fridays,
            max_snapshots=max(0, int(args.max_snapshots)),
        )
        if not snapshot_dates:
            raise ValueError("No paired daily/multibagger score snapshot dates found for Phase 2 calibration.")
        excluded_tickers = load_excluded_tickers(
            conn,
            exclude_current_removals=exclude_current_removals,
            extra=extra_exclusions,
        )
        rows = load_score_rows(conn, snapshot_dates, excluded_tickers)
        if not rows:
            raise ValueError("No score rows remain after exclusions.")
        asof_dates = [parsed for row in rows if (parsed := parse_date(row.get("asof_date"))) is not None]
        if not asof_dates:
            raise ValueError("Score rows do not contain valid as-of dates.")
        tickers = {ticker for row in rows if (ticker := normalize_ticker(row.get("ticker")))}
        bars_by_ticker = load_bars(conn, tickers=tickers, min_date=min(asof_dates), market_sources=market_sources)

    add_diagnostics(rows, min_addv20=min_addv20, commercial_risk_settings=commercial_risk_settings)
    add_forward_returns(
        rows,
        bars_by_ticker,
        horizons,
        round_trip_cost_bps=params.round_trip_cost_bps,
        next_bar_entry=next_bar_entry,
    )
    liquid_rows = liquidity_ok_rows(rows)

    grid_rows: list[dict[str, Any]] = []
    split_rows_by_key: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    split_manifest: dict[str, Any] = {}
    configured_embargo_days = cfg_get(config, "calibration.phase2.embargo_days", None)
    for horizon in horizons:
        embargo_days = (
            int(configured_embargo_days)
            if configured_embargo_days is not None
            else minimum_calendar_embargo_days_for_horizon(horizon)
        )
        for sample, sample_rows in [("all", rows), ("liquidity_ok", liquid_rows)]:
            train_rows, test_rows, train_dates, test_dates = split_rows_by_completed_return_date(
                sample_rows,
                horizon=horizon,
                train_fraction=train_fraction,
                embargo_days=embargo_days,
            )
            split_rows_by_key[(sample, "train", horizon)] = train_rows
            split_rows_by_key[(sample, "test", horizon)] = test_rows
            split_manifest[f"{sample}_{horizon}"] = {
                "train_snapshot_dates": train_dates,
                "train_snapshot_date_count": len(train_dates),
                "test_snapshot_dates": test_dates,
                "test_snapshot_date_count": len(test_dates),
                "embargo_days": embargo_days,
            }
            grid_rows.extend(
                build_grid_rows(
                    train_rows,
                    signals,
                    pools,
                    [horizon],
                    top_ns,
                    sample=sample,
                    evaluation_split="train",
                    params=params,
                    max_workers=max_workers,
                )
            )
            grid_rows.extend(
                build_grid_rows(
                    test_rows,
                    signals,
                    pools,
                    [horizon],
                    top_ns,
                    sample=sample,
                    evaluation_split="test",
                    params=params,
                    max_workers=max_workers,
                )
            )

    train_grid_rows = [row for row in grid_rows if str(row.get("evaluation_split") or "") == "train"]
    test_grid_rows = [row for row in grid_rows if str(row.get("evaluation_split") or "") == "test"]
    best_rows = build_best_rows(grid_rows)
    holdout_rows = build_holdout_rows(train_grid_rows, test_grid_rows, top_k=holdout_top_k)
    selected_ticker_rows = build_selected_ticker_rows(
        split_rows_by_key,
        holdout_rows,
        top_train_ranks=selected_ticker_top_ranks,
    )
    weakness_component_rows = build_phase2_weakness_component_rows(selected_ticker_rows)
    bootstrap_rows = build_bootstrap_rows(
        split_rows_by_key,
        holdout_rows,
        params=params,
        top_k=bootstrap_top_k,
        iterations=bootstrap_iterations,
        seed=bootstrap_seed,
    )
    orthogonality_rows: list[dict[str, Any]] = []
    for (sample, split, horizon), split_rows in sorted(split_rows_by_key.items()):
        orthogonality_rows.extend(
            build_orthogonality_rows(
                split_rows,
                [horizon],
                sample=sample,
                evaluation_split=split,
            )
        )

    write_csv(output_dir / "phase2_speculative_alpha_grid.csv", grid_rows)
    write_csv(output_dir / "phase2_speculative_alpha_best.csv", best_rows)
    write_csv(output_dir / "phase2_speculative_alpha_holdout.csv", holdout_rows)
    write_csv(output_dir / "phase2_speculative_alpha_bootstrap_ci.csv", bootstrap_rows)
    write_csv(output_dir / "phase2_orthogonality.csv", orthogonality_rows)
    write_csv(output_dir / "phase2_selected_ticker_diagnostics.csv", selected_ticker_rows)
    write_csv(output_dir / "phase2_weakness_components.csv", weakness_component_rows)

    horizon_counts = {
        str(horizon): sum(1 for row in rows if to_float(row.get(f"fwd_{horizon}d_net_return")) is not None)
        for horizon in horizons
    }
    manifest = {
        "script": Path(__file__).name,
        "db_path": str(db_path),
        "output_dir": str(output_dir),
        "snapshot_dates": snapshot_dates,
        "snapshot_date_count": len(snapshot_dates),
        "score_row_count": len(rows),
        "liquidity_ok_score_row_count": len(liquid_rows),
        "ticker_count": len({row.get("ticker") for row in rows}),
        "excluded_ticker_count": len(excluded_tickers),
        "market_sources": market_sources,
        "horizons": horizons,
        "top_n": top_ns,
        "forward_return_observation_counts": horizon_counts,
        "train_fraction": train_fraction,
        "next_bar_entry": next_bar_entry,
        "exclude_current_removals": exclude_current_removals,
        "signal_count": len(signals),
        "pool_count": len(pools),
        "max_workers": max_workers,
        "bootstrap_iterations": bootstrap_iterations,
        "bootstrap_top_k": bootstrap_top_k,
        "holdout_top_k": holdout_top_k,
        "selected_ticker_top_ranks": selected_ticker_top_ranks,
        "split_manifest": split_manifest,
        "calibration_params": params.__dict__,
        "elapsed_sec": round(time.perf_counter() - start_time, 3),
        "notes": [
            "Phase 2 is diagnostic only and does not mutate production scoring or config weights.",
            "Tier-1 remains the investability engine; Phase 2 tests alpha/multibagger as an overlay or separate speculative basket.",
            "tier1_top*_overlay_pool tests promotion inside already investable Tier-1 pools.",
            "speculative_outside_tier1_top* pools test separate speculative baskets outside the current Tier-1 selection set.",
            "Forward returns use next-bar entry by default to reduce same-day-close look-ahead bias.",
            "Composite signals are scored over available components and report component coverage, instead of dropping rows with one missing score.",
            "Calibration constraints and objective penalize core hard, event hard, soft weakness, illiquidity, large losses, and top-winner concentration.",
            "phase2_weakness_components.csv aggregates selected-name weakness reasons by signal, pool, split, horizon, and Top-N cutoff.",
        ],
    }
    write_json(output_dir / "phase2_manifest.json", manifest)
    LOGGER.info(
        "Phase 2 speculative alpha calibration written: output_dir=%s rows=%d grid_rows=%d elapsed=%.3fs",
        output_dir,
        len(rows),
        len(grid_rows),
        time.perf_counter() - start_time,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except BaseException as exc:
        if not (isinstance(exc, SystemExit) and exc.code in (0, None)):
            LOGGER.exception("Unhandled exception in main()")
            sys.exit(1)
        raise

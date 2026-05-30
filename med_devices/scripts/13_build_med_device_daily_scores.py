#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import DEFAULT_NEUTRAL_SCORE, cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, finish_run, init_db, quote_identifier, start_run, utc_now  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("build_med_device_daily_scores")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_WEIGHTS = {
    "fundamental_quality": 0.25,
    "durable_growth": 0.15,
    "fda_product": 0.15,
    "reimbursement": 0.10,
    "valuation": 0.20,
    "technical_entry": 0.10,
    "sentiment_catalyst": 0.05,
}
ALLOWED_FEATURE_TABLES = {
    "feature_financial_valuation",
    "feature_fda_product_risk",
    "feature_reimbursement",
    "feature_technical_entry",
    "feature_durable_growth",
    "feature_sentiment_catalyst",
}
FIELDNAMES = [
    "asof_date",
    "scoring_model_version",
    "rank",
    "company_id",
    "ticker",
    "company_name",
    "subsector",
    "composite_score",
    "raw_composite_score",
    "composite_percentile",
    "fundamental_quality_score",
    "durable_growth_score",
    "fda_product_score",
    "fda_data_available",
    "reimbursement_score",
    "reimbursement_status",
    "direct_code_evidence",
    "payment_rate_evidence",
    "coverage_policy_evidence",
    "procedure_bundled_flag",
    "capital_equipment_flag",
    "diagnostics_lab_flag",
    "unknown_reimbursement_flag",
    "valuation_score",
    "technical_entry_score",
    "sentiment_catalyst_score",
    "value_trap_score",
    "data_completeness_score",
    "live_component_count",
    "composite_score_delta",
    "rank_delta",
    "classification_change",
    "hard_red_flag",
    "hard_red_flag_reasons",
    "classification",
    "decision_bucket",
    "entry_status",
    "gate_status",
    "review_reason",
    "failed_gates",
    "classification_reason",
    "fda_review_state",
    "market_cap",
    "current_shares_outstanding",
    "diluted_weighted_average_shares",
    "basic_weighted_average_shares",
    "shares_source_concept",
    "shares_source_form",
    "shares_source_period",
    "market_cap_validated_flag",
    "avg_dollar_volume_60d",
    "liquidity_score",
    "capacity_bucket",
    "min_position_size_feasible",
    "max_position_size_feasible",
    "passed_raw_score_gate",
    "passed_fundamental_gate",
    "passed_growth_gate",
    "passed_fda_gate",
    "passed_reimbursement_gate",
    "passed_valuation_gate",
    "passed_technical_gate",
    "passed_value_trap_gate",
    "passed_data_quality_gate",
    "passed_liquidity_gate",
    "passed_fda_manual_review_gate",
    "final_investability_gate",
    "top_positive_drivers",
    "top_negative_drivers",
]


@dataclass
class ScoreRow:
    asof_date: str
    scoring_model_version: str
    rank: int
    company_id: int
    ticker: str
    company_name: str
    subsector: str
    composite_score: float = 0.0
    raw_composite_score: float = 0.0
    composite_percentile: float = 0.0
    fundamental_quality_score: float = 0.0
    durable_growth_score: float = 50.0
    fda_product_score: float = 50.0
    fda_data_available: int = 0
    reimbursement_score: float = 50.0
    reimbursement_status: str = "unknown"
    direct_code_evidence: int = 0
    payment_rate_evidence: int = 0
    coverage_policy_evidence: int = 0
    procedure_bundled_flag: int = 0
    capital_equipment_flag: int = 0
    diagnostics_lab_flag: int = 0
    unknown_reimbursement_flag: int = 1
    valuation_score: float = 0.0
    technical_entry_score: float = 50.0
    sentiment_catalyst_score: float = 50.0
    value_trap_score: float = 0.0
    data_completeness_score: float = 0.0
    live_component_count: int = 0
    composite_score_delta: float | None = None
    rank_delta: int | None = None
    classification_change: str = ""
    hard_red_flag: int = 0
    hard_red_flag_reasons: str = ""
    classification: str = "unclassified"
    decision_bucket: str = "unclassified"
    entry_status: str = "unclassified"
    gate_status: str = "fail"
    review_reason: str = ""
    failed_gates: str = ""
    classification_reason: str = ""
    fda_review_state: str = ""
    market_cap: float | None = None
    current_shares_outstanding: float | None = None
    diluted_weighted_average_shares: float | None = None
    basic_weighted_average_shares: float | None = None
    shares_source_concept: str = ""
    shares_source_form: str = ""
    shares_source_period: str = ""
    market_cap_validated_flag: int = 0
    avg_dollar_volume_60d: float | None = None
    liquidity_score: float | None = None
    capacity_bucket: str = "unknown"
    min_position_size_feasible: float | None = None
    max_position_size_feasible: float | None = None
    passed_raw_score_gate: int = 0
    passed_fundamental_gate: int = 0
    passed_growth_gate: int = 0
    passed_fda_gate: int = 0
    passed_reimbursement_gate: int = 0
    passed_valuation_gate: int = 0
    passed_technical_gate: int = 0
    passed_value_trap_gate: int = 0
    passed_data_quality_gate: int = 0
    passed_liquidity_gate: int = 0
    passed_fda_manual_review_gate: int = 0
    final_investability_gate: int = 0
    top_positive_drivers: list[str] = field(default_factory=list)
    top_negative_drivers: list[str] = field(default_factory=list)
    durable_growth_proxy_available: bool = False
    sentiment_proxy_available: bool = False
    sentiment_proxy_source: str = ""
    sentiment_proxy_input: str = ""


@dataclass(frozen=True)
class SentimentProxy:
    score: float
    source: str
    input_name: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build med-device daily composite scores.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="")
    parser.add_argument("--tickers", type=str, default="")
    parser.add_argument("--max-tickers", type=int, default=0)
    return parser.parse_args()


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def cfg_float(config: dict[str, Any], dotted_key: str, default: float) -> float:
    value = to_float(cfg_get(config, dotted_key, default))
    if value is None:
        raise ValueError(f"Config value must be numeric: {dotted_key}")
    return value


def cfg_bool(config: dict[str, Any], dotted_key: str, default: bool) -> bool:
    raw = cfg_get(config, dotted_key, default)
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def component_neutral(config: dict[str, Any], component: str, legacy_key: str, default: float) -> float:
    nested_key = f"scoring.component_neutral_defaults.{component}"
    raw = cfg_get(config, nested_key, None)
    if raw is not None:
        value = to_float(raw)
        if value is None:
            raise ValueError(f"Config value must be numeric: {nested_key}")
        return value
    return cfg_float(config, legacy_key, default)


def score_or(raw: object, default: float) -> float:
    value = to_float(raw)
    return default if value is None else value


def parse_date(raw: object) -> datetime | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None


def latest_financial_asof(conn: Any) -> str:
    row = conn.execute("SELECT MAX(asof_date) AS asof_date FROM feature_financial_valuation").fetchone()
    asof = str(row["asof_date"] or "") if row is not None else ""
    if not asof:
        raise ValueError("No feature_financial_valuation rows found; run script 06 first.")
    return asof


def table_exists(conn: Any, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def load_weights(config: dict[str, Any]) -> dict[str, float]:
    raw = cfg_get(config, "scoring.composite_weights", DEFAULT_WEIGHTS)
    if not isinstance(raw, dict):
        return dict(DEFAULT_WEIGHTS)
    unknown = sorted(set(str(key) for key in raw) - set(DEFAULT_WEIGHTS))
    if unknown:
        LOGGER.warning("Ignoring unknown composite scoring weight key(s): %s", ", ".join(unknown))
    out = dict(DEFAULT_WEIGHTS)
    for key, raw_value in raw.items():
        if str(key) not in DEFAULT_WEIGHTS:
            continue
        value = to_float(raw_value)
        if value is None or value < 0:
            raise ValueError(f"Composite score weight must be non-negative numeric: {key}")
        out[str(key)] = value
    total = sum(out.values())
    if abs(total - 1.0) > 0.0001:
        raise ValueError(f"Composite score weights must sum to 1.0: {total:.6f}")
    return out


def load_financial_rows(conn: Any, *, asof: str, ticker_filter: set[str], max_tickers: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM feature_financial_valuation
        WHERE asof_date = (
            SELECT MAX(asof_date)
            FROM feature_financial_valuation
            WHERE asof_date <= ?
        )
        ORDER BY ticker
        """,
        (asof,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        ticker = normalize_ticker(item.get("ticker"))
        if ticker_filter and ticker not in ticker_filter:
            continue
        out.append(item)
        if max_tickers > 0 and len(out) >= max_tickers:
            break
    return out


def load_latest_feature(conn: Any, table: str, score_col: str, *, asof: str) -> dict[int, dict[str, Any]]:
    if table not in ALLOWED_FEATURE_TABLES:
        raise ValueError(f"Unknown feature table: {table}")
    if not table_exists(conn, table):
        return {}
    table_sql = quote_identifier(table)
    columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table_sql})").fetchall()}
    if not {"company_id", "asof_date", score_col}.issubset(columns):
        return {}
    rows = conn.execute(
        f"""
        SELECT t.*
        FROM {table_sql} t
        JOIN (
            SELECT company_id, MAX(asof_date) AS asof_date
            FROM {table_sql}
            WHERE asof_date <= ?
            GROUP BY company_id
        ) latest ON latest.company_id = t.company_id AND latest.asof_date = t.asof_date
        """,
        (asof,),
    ).fetchall()
    seen: set[int] = set()
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        company_id = int(row["company_id"])
        if company_id in seen:
            LOGGER.warning(
                "load_latest_feature: duplicate company_id=%d in %s at asof<=%s; keeping first occurrence",
                company_id,
                table,
                asof,
            )
            continue
        seen.add(company_id)
        out[company_id] = dict(row)
    return out


def feature_row_count(conn: Any, table: str, *, asof: str) -> int:
    if table not in ALLOWED_FEATURE_TABLES:
        raise ValueError(f"Unknown feature table: {table}")
    if not table_exists(conn, table):
        return 0
    table_sql = quote_identifier(table)
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table_sql} WHERE asof_date <= ?", (asof,)).fetchone()
    return int(row["n"] or 0) if row is not None else 0


def preflight_required_features(conn: Any, *, asof: str) -> None:
    required = {
        "feature_financial_valuation": "run script 06 first",
        "feature_fda_product_risk": "run script 10 first",
        "feature_reimbursement": "run scripts 14, 15, then 11 first",
        "feature_technical_entry": "run script 12 first",
    }
    missing = [f"{table} ({hint})" for table, hint in required.items() if feature_row_count(conn, table, asof=asof) <= 0]
    if missing:
        raise RuntimeError(f"Required upstream feature table(s) are empty as of {asof}: {', '.join(missing)}")


def feature_latest_asof(conn: Any, table: str, *, asof: str) -> str:
    if table not in ALLOWED_FEATURE_TABLES:
        raise ValueError(f"Unknown feature table: {table}")
    if not table_exists(conn, table):
        return ""
    table_sql = quote_identifier(table)
    row = conn.execute(f"SELECT MAX(asof_date) AS asof_date FROM {table_sql} WHERE asof_date <= ?", (asof,)).fetchone()
    return str(row["asof_date"] or "") if row is not None else ""


def preflight_feature_freshness(conn: Any, *, asof: str, max_staleness_days: int) -> None:
    asof_date = parse_date(asof)
    if asof_date is None:
        raise ValueError(f"Invalid scoring asof date: {asof}")
    stale: list[str] = []
    for table in ("feature_financial_valuation", "feature_fda_product_risk", "feature_reimbursement", "feature_technical_entry"):
        latest = feature_latest_asof(conn, table, asof=asof)
        latest_date = parse_date(latest)
        if latest_date is None:
            stale.append(f"{table}:missing")
            continue
        days_stale = (asof_date - latest_date).days
        if days_stale < 0 or days_stale > max_staleness_days:
            stale.append(f"{table}:{days_stale}d")
    if stale:
        raise RuntimeError(
            f"Required upstream feature table(s) are stale for {asof}; max_staleness_days={max_staleness_days}: "
            + ", ".join(stale)
        )


def percentile(
    values: list[tuple[int, float]],
    *,
    higher_is_better: bool,
    winsor_low_pct: float = 0.05,
    winsor_high_pct: float = 0.95,
) -> dict[int, float]:
    if not values:
        return {}
    ranked_values = list(values)
    if len(ranked_values) >= 4:
        if not 0.0 <= winsor_low_pct < winsor_high_pct <= 1.0:
            raise ValueError(
                f"winsor bounds must satisfy 0 <= low < high <= 1, got {winsor_low_pct}, {winsor_high_pct}"
            )
        sorted_values = sorted(value for _, value in ranked_values)
        low_bound = sorted_values[max(0, min(len(sorted_values) - 1, math.ceil(winsor_low_pct * len(sorted_values)) - 1))]
        high_bound = sorted_values[max(0, min(len(sorted_values) - 1, math.ceil(winsor_high_pct * len(sorted_values)) - 1))]
        if low_bound > high_bound:
            low_bound, high_bound = high_bound, low_bound
        ranked_values = [(company_id, max(low_bound, min(high_bound, value))) for company_id, value in ranked_values]
    ranked_values.sort(key=lambda item: item[1])
    if len(ranked_values) == 1:
        return {ranked_values[0][0]: 50.0}
    out: dict[int, float] = {}
    denominator = len(ranked_values) - 1
    for rank, (company_id, _) in enumerate(ranked_values):
        pct = 100.0 * rank / denominator
        out[company_id] = pct if higher_is_better else 100.0 - pct
    return out


def durable_growth_proxy(financial_rows: list[dict[str, Any]]) -> dict[int, float]:
    margin_trend_pairs: list[tuple[int, float]] = []
    rd_growth_pairs: list[tuple[int, float]] = []
    dilution_pairs: list[tuple[int, float]] = []
    leverage_pairs: list[tuple[int, float]] = []
    confidence_pairs: list[tuple[int, float]] = []
    for row in financial_rows:
        company_id = int(row["company_id"])
        margin_trend = to_float(row.get("gross_margin_trend_3y"))
        rd_growth = to_float(row.get("rd_growth_yoy"))
        dilution = to_float(row.get("shares_yoy_growth"))
        leverage = to_float(row.get("net_debt_to_revenue"))
        confidence = to_float(row.get("data_confidence_score"))
        if margin_trend is not None:
            margin_trend_pairs.append((company_id, margin_trend))
        if rd_growth is not None:
            rd_growth_pairs.append((company_id, rd_growth))
        if dilution is not None:
            dilution_pairs.append((company_id, dilution))
        if leverage is not None:
            leverage_pairs.append((company_id, leverage))
        if confidence is not None:
            confidence_pairs.append((company_id, confidence))
    margin_scores = percentile(margin_trend_pairs, higher_is_better=True)
    rd_scores = percentile(rd_growth_pairs, higher_is_better=True)
    dilution_scores = percentile(dilution_pairs, higher_is_better=False)
    leverage_scores = percentile(leverage_pairs, higher_is_better=False)
    confidence_scores = percentile(confidence_pairs, higher_is_better=True)
    out: dict[int, float] = {}
    for row in financial_rows:
        company_id = int(row["company_id"])
        available_scores = [
            (margin_scores.get(company_id), 0.30),
            (rd_scores.get(company_id), 0.20),
            (dilution_scores.get(company_id), 0.20),
            (leverage_scores.get(company_id), 0.15),
            (confidence_scores.get(company_id), 0.15),
        ]
        active = [(score, weight) for score, weight in available_scores if score is not None]
        if len(active) < 2:
            continue
        total_weight = sum(weight for _, weight in active)
        out[company_id] = round(sum(float(score) * weight for score, weight in active) / total_weight, 2)
    return out


def shrink_to_neutral(score: float, *, neutral: float, weight: float) -> float:
    return round(neutral + (score - neutral) * weight, 2)


def sentiment_catalyst_proxy(
    financial_rows: list[dict[str, Any]],
    *,
    config: dict[str, Any],
    neutral: float,
) -> dict[int, SentimentProxy]:
    annual_fallback_enabled = cfg_bool(
        config,
        "scoring.sentiment_catalyst_proxy.annual_revenue_growth_fallback_enabled",
        True,
    )
    rd_fallback_enabled = cfg_bool(
        config,
        "scoring.sentiment_catalyst_proxy.rd_scale_fallback_enabled",
        True,
    )
    annual_weight = cfg_float(config, "scoring.sentiment_catalyst_proxy.annual_revenue_growth_weight", 0.75)
    annual_min_revenue = cfg_float(
        config,
        "scoring.sentiment_catalyst_proxy.annual_revenue_growth_min_revenue_ttm",
        10_000_000.0,
    )
    rd_weight = cfg_float(config, "scoring.sentiment_catalyst_proxy.rd_scale_weight", 0.50)
    rd_max_revenue = cfg_float(
        config,
        "scoring.sentiment_catalyst_proxy.rd_scale_max_revenue_ttm",
        5_000_000.0,
    )
    runway_weight = cfg_float(config, "scoring.sentiment_catalyst_proxy.pre_revenue_runway_weight", 0.60)
    pre_revenue_rd_weight = cfg_float(config, "scoring.sentiment_catalyst_proxy.pre_revenue_rd_scale_weight", 0.40)
    rd_log_transform = cfg_bool(config, "scoring.sentiment_catalyst_proxy.rd_scale_log_transform", True)
    surprise_pairs: list[tuple[int, float]] = []
    annual_growth_pairs: list[tuple[int, float]] = []
    runway_pairs: list[tuple[int, float]] = []
    rd_scale_pairs: list[tuple[int, float]] = []
    for row in financial_rows:
        company_id = int(row["company_id"])
        surprise = to_float(row.get("quarterly_revenue_surprise_yoy"))
        if surprise is not None:
            surprise_pairs.append((company_id, surprise))
            continue
        revenue_ttm = to_float(row.get("revenue_ttm"))
        annual_growth = to_float(row.get("revenue_yoy_growth"))
        if annual_fallback_enabled and annual_growth is not None and revenue_ttm is not None and revenue_ttm >= annual_min_revenue:
            annual_growth_pairs.append((company_id, annual_growth))
            continue
        rd_ttm = to_float(row.get("annualized_research_and_development"))
        if rd_ttm is None:
            rd_ttm = to_float(row.get("research_and_development_ttm"))
        pre_revenue = revenue_ttm is None or revenue_ttm <= rd_max_revenue
        runway_years = to_float(row.get("financial_runway_years"))
        if rd_fallback_enabled and pre_revenue and runway_years is not None and runway_years > 0:
            runway_pairs.append((company_id, runway_years))
        if rd_fallback_enabled and pre_revenue and rd_ttm is not None and abs(rd_ttm) > 0:
            rd_value = math.log1p(abs(rd_ttm)) if rd_log_transform else abs(rd_ttm)
            rd_scale_pairs.append((company_id, rd_value))
    surprise_scores = percentile(surprise_pairs, higher_is_better=True)
    annual_growth_scores = percentile(annual_growth_pairs, higher_is_better=True)
    runway_scores = percentile(runway_pairs, higher_is_better=True)
    rd_scale_scores = percentile(rd_scale_pairs, higher_is_better=True)
    out: dict[int, SentimentProxy] = {
        company_id: SentimentProxy(
            score=round(score, 2),
            source="daily_score_quarterly_revenue_surprise_proxy",
            input_name="quarterly_revenue_surprise_yoy",
        )
        for company_id, score in surprise_scores.items()
    }
    for company_id, score in annual_growth_scores.items():
        if company_id in out:
            continue
        out[company_id] = SentimentProxy(
            score=shrink_to_neutral(score, neutral=neutral, weight=annual_weight),
            source="daily_score_annual_revenue_growth_fallback",
            input_name="revenue_yoy_growth",
        )
    pre_revenue_ids = sorted(set(runway_scores) | set(rd_scale_scores))
    for company_id in pre_revenue_ids:
        if company_id in out:
            continue
        components: list[tuple[float, float]] = []
        if company_id in runway_scores:
            components.append((float(runway_scores[company_id]), runway_weight))
        if company_id in rd_scale_scores:
            components.append((float(rd_scale_scores[company_id]), pre_revenue_rd_weight))
        if not components:
            continue
        total_weight = sum(weight for _, weight in components)
        blended = sum(score * weight for score, weight in components) / total_weight
        out[company_id] = SentimentProxy(
            score=shrink_to_neutral(blended, neutral=neutral, weight=rd_weight),
            source="daily_score_pre_revenue_runway_rd_fallback",
            input_name="financial_runway_years;annualized_research_and_development",
        )
    return out


def durable_proxy_available(financial_item: dict[str, Any]) -> bool:
    return (
        sum(
            1
            for key in ("gross_margin_trend_3y", "rd_growth_yoy", "shares_yoy_growth", "net_debt_to_revenue", "data_confidence_score")
            if to_float(financial_item.get(key)) is not None
        )
        >= 2
    )


def upsert_durable_growth_proxy_rows(conn: Any, rows: list[ScoreRow]) -> int:
    now = utc_now()
    payload_rows = [
        (
            row.asof_date,
            row.company_id,
            row.durable_growth_score,
            json.dumps(
                {
                    "source": "daily_score_durable_proxy",
                    "inputs": [
                        "gross_margin_trend_3y",
                        "rd_growth_yoy",
                        "shares_yoy_growth",
                        "net_debt_to_revenue",
                        "data_confidence_score",
                    ],
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
            now,
            now,
        )
        for row in rows
        if row.durable_growth_proxy_available
    ]
    if not payload_rows:
        return 0
    conn.executemany(
        """
        INSERT INTO feature_durable_growth(asof_date, company_id, score, payload_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(asof_date, company_id) DO UPDATE SET
            score = excluded.score,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        payload_rows,
    )
    return len(payload_rows)


def upsert_sentiment_proxy_rows(conn: Any, rows: list[ScoreRow]) -> int:
    now = utc_now()
    payload_rows = [
        (
            row.asof_date,
            row.company_id,
            row.sentiment_catalyst_score,
            row.sentiment_catalyst_score,
            50.0,
            json.dumps(
                {
                    "source": row.sentiment_proxy_source or "daily_score_sentiment_proxy",
                    "input": row.sentiment_proxy_input,
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
            now,
            now,
        )
        for row in rows
        if row.sentiment_proxy_available
    ]
    if not payload_rows:
        return 0
    conn.executemany(
        """
        INSERT INTO feature_sentiment_catalyst(
            asof_date, company_id, score, estimate_revision_proxy_score, event_risk_score,
            payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asof_date, company_id) DO UPDATE SET
            score = excluded.score,
            estimate_revision_proxy_score = excluded.estimate_revision_proxy_score,
            event_risk_score = excluded.event_risk_score,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        payload_rows,
    )
    return len(payload_rows)


def score_drivers(row: ScoreRow) -> tuple[list[str], list[str]]:
    items = [
        ("fundamental", row.fundamental_quality_score),
        ("durable_growth", row.durable_growth_score),
        ("fda_product", row.fda_product_score),
        ("reimbursement", row.reimbursement_score),
        ("valuation", row.valuation_score),
        ("technical_entry", row.technical_entry_score),
        ("sentiment_catalyst", row.sentiment_catalyst_score),
    ]
    positives = [f"{name}:{score:.1f}" for name, score in sorted(items, key=lambda item: item[1], reverse=True)[:3]]
    below_neutral = [(name, score) for name, score in items if score < 50.0]
    negatives = [f"{name}:{score:.1f}" for name, score in sorted(below_neutral, key=lambda item: item[1])[:3]]
    if row.unknown_reimbursement_flag and "reimbursement:unknown" not in negatives:
        negatives.append("reimbursement:unknown")
    return positives, negatives


def weighted_available_score(scores: dict[str, float], available: dict[str, bool], weights: dict[str, float]) -> float:
    active_keys = [key for key, is_available in available.items() if is_available and key in scores]
    total_weight = sum(weights[key] for key in active_keys)
    if total_weight <= 0:
        return DEFAULT_NEUTRAL_SCORE
    return sum(scores[key] * weights[key] for key in active_keys) / total_weight


def value_trap_discount(value_trap_score: float, *, start: float = 40.0) -> float:
    if value_trap_score <= start:
        return 1.0
    return max(0.50, 1.0 - ((value_trap_score - start) / (2.0 * (100.0 - start))))


def cross_sectional_percentile_rank(rows: list[ScoreRow]) -> None:
    pairs = [
        (idx, row.raw_composite_score)
        for idx, row in enumerate(rows)
        if row.raw_composite_score is not None and math.isfinite(row.raw_composite_score)
    ]
    if len(pairs) <= 1:
        for row in rows:
            row.composite_percentile = 50.0
        return
    pairs.sort(key=lambda item: item[1])
    denominator = len(pairs) - 1
    for rank, (idx, _) in enumerate(pairs):
        rows[idx].composite_percentile = round(100.0 * rank / denominator, 2)


def load_previous_scores(conn: Any, *, asof: str) -> dict[int, dict[str, Any]]:
    previous = conn.execute(
        """
        SELECT MAX(asof_date) AS asof_date
        FROM med_device_daily_scores
        WHERE asof_date < ?
        """,
        (asof,),
    ).fetchone()
    previous_asof = str(previous["asof_date"] or "") if previous is not None else ""
    if not previous_asof:
        return {}
    rows = conn.execute(
        """
        SELECT company_id, composite_score, rank, classification
        FROM med_device_daily_scores
        WHERE asof_date = ?
        """,
        (previous_asof,),
    ).fetchall()
    return {int(row["company_id"]): dict(row) for row in rows}


MANUAL_FDA_REVIEW_STATES = {"confirmed_hard_red", "regulatory_review_required", "mapping_review_required"}
NON_LIVE_REIMBURSEMENT_STATUSES = {"", "unknown", "cms_data_not_loaded"}


def int_flag(raw: object) -> int:
    return 1 if str(raw or "").strip().lower() in {"1", "true", "yes", "y", "on"} or raw == 1 else 0


def reimbursement_component_is_live(item: dict[str, Any], score: float | None) -> bool:
    if score is None:
        return False
    status = str(item.get("reimbursement_status") or "").strip().lower()
    if int_flag(item.get("unknown_reimbursement_flag")) or status in NON_LIVE_REIMBURSEMENT_STATUSES:
        return False
    return bool(
        int_flag(item.get("direct_code_evidence"))
        or int_flag(item.get("payment_rate_evidence"))
        or int_flag(item.get("coverage_policy_evidence"))
        or int_flag(item.get("procedure_bundled_flag"))
        or int_flag(item.get("capital_equipment_flag"))
        or int_flag(item.get("diagnostics_lab_flag"))
    )


def entry_status(technical_score: float) -> str:
    if technical_score < 35.0:
        return "avoid_technical_breakdown"
    if technical_score < 45.0:
        return "not_entry_ready"
    if technical_score < 55.0:
        return "watch_for_setup"
    return "entry_eligible"


def capacity_bucket(avg_dollar_volume_60d: float | None) -> str:
    if avg_dollar_volume_60d is None:
        return "unknown"
    if avg_dollar_volume_60d >= 50_000_000.0:
        return "institutional_liquid"
    if avg_dollar_volume_60d >= 10_000_000.0:
        return "liquid"
    if avg_dollar_volume_60d >= 2_000_000.0:
        return "moderate_capacity"
    if avg_dollar_volume_60d >= 1_000_000.0:
        return "minimum_capacity"
    return "illiquid"


def max_position_size(avg_dollar_volume_60d: float | None, *, participation_rate: float = 0.05) -> float | None:
    if avg_dollar_volume_60d is None or avg_dollar_volume_60d <= 0:
        return None
    return round(avg_dollar_volume_60d * participation_rate, 2)


def min_position_size(max_feasible: float | None, *, target_minimum: float = 25_000.0) -> float | None:
    if max_feasible is None:
        return None
    return round(min(target_minimum, max_feasible), 2)


def classify(row: ScoreRow, *, gates: dict[str, float]) -> None:
    reasons: list[str] = []
    row.entry_status = entry_status(row.technical_entry_score)
    row.capacity_bucket = capacity_bucket(row.avg_dollar_volume_60d)
    row.max_position_size_feasible = max_position_size(row.avg_dollar_volume_60d)
    row.min_position_size_feasible = min_position_size(row.max_position_size_feasible)
    row.passed_raw_score_gate = int(row.raw_composite_score >= gates["composite_min"])
    row.passed_fundamental_gate = int(row.fundamental_quality_score >= gates["fundamental_quality_min"])
    row.passed_growth_gate = int(row.durable_growth_score >= gates["durable_growth_min"])
    row.passed_fda_gate = int((not row.fda_data_available and not row.fda_review_state) or row.fda_product_score >= gates["fda_product_min"])
    reimbursement_live = row.unknown_reimbursement_flag == 0 and row.reimbursement_status not in NON_LIVE_REIMBURSEMENT_STATUSES
    row.passed_reimbursement_gate = int(reimbursement_live and row.reimbursement_score >= gates["reimbursement_min"])
    row.passed_valuation_gate = int(row.valuation_score >= gates["valuation_min"])
    row.passed_technical_gate = int(row.technical_entry_score >= gates["technical_entry_min"])
    row.passed_value_trap_gate = int(row.value_trap_score <= gates["value_trap_max"])
    row.passed_data_quality_gate = int(row.data_completeness_score >= gates["data_completeness_min"])
    row.passed_liquidity_gate = int(
        row.avg_dollar_volume_60d is not None and row.avg_dollar_volume_60d >= gates["min_avg_dollar_volume_60d"]
    )
    manual_regulatory_state = row.fda_review_state in MANUAL_FDA_REVIEW_STATES
    confirmed_hard_red = row.fda_review_state == "confirmed_hard_red"
    row.passed_fda_manual_review_gate = int(not manual_regulatory_state and not row.hard_red_flag)

    if not row.passed_raw_score_gate:
        reasons.append("composite_below_gate")
    if not row.passed_fundamental_gate:
        reasons.append("fundamental_below_gate")
    if not row.passed_growth_gate:
        reasons.append("growth_below_gate")
    if not row.passed_fda_gate:
        reasons.append("fda_below_gate")
    if not row.passed_reimbursement_gate and not reimbursement_live:
        reasons.append("reimbursement_missing_evidence")
    elif not row.passed_reimbursement_gate:
        reasons.append("reimbursement_below_gate")
    if not row.passed_valuation_gate:
        reasons.append("valuation_below_gate")
    if not row.passed_technical_gate:
        reasons.append("technical_below_gate")
    if not row.passed_data_quality_gate:
        reasons.append("data_quality_below_gate")
    if not row.passed_liquidity_gate:
        reasons.append("liquidity_below_gate")
    if row.hard_red_flag:
        reasons.append("hard_red_flag")
    elif manual_regulatory_state:
        reasons.append("fda_review_required")
    if row.value_trap_score >= gates["value_trap_hard_max"]:
        reasons.append("value_trap")
    elif not row.passed_value_trap_gate:
        reasons.append("value_trap_soft_gate")

    row.failed_gates = ";".join(reasons)
    row.review_reason = ";".join(reasons)
    row.final_investability_gate = int(
        row.passed_raw_score_gate
        and row.passed_fundamental_gate
        and row.passed_growth_gate
        and row.passed_fda_gate
        and row.passed_reimbursement_gate
        and row.passed_valuation_gate
        and row.passed_technical_gate
        and row.passed_value_trap_gate
        and row.passed_data_quality_gate
        and row.passed_liquidity_gate
        and row.passed_fda_manual_review_gate
    )
    row.gate_status = "pass" if row.final_investability_gate else "fail"
    if confirmed_hard_red:
        row.classification = "avoid_confirmed_regulatory_risk"
        row.classification_reason = "confirmed_hard_red"
    elif manual_regulatory_state or row.hard_red_flag:
        row.classification = "manual_review_regulatory_risk"
        row.classification_reason = "fda_manual_review_required"
    elif not row.passed_data_quality_gate:
        row.classification = "data_review_required"
        row.classification_reason = "data_completeness_below_gate"
    elif row.entry_status in {"avoid_technical_breakdown", "not_entry_ready"}:
        row.classification = "watchlist_wait_for_entry"
        row.classification_reason = row.entry_status
    elif row.fundamental_quality_score >= gates["fundamental_quality_min"] and row.valuation_score < gates["valuation_min"]:
        row.classification = "quality_watchlist_wait_for_price"
        row.classification_reason = "quality_but_valuation_below_gate"
    elif row.valuation_score >= 75.0 and row.fundamental_quality_score < gates["fundamental_quality_min"]:
        row.classification = "cheap_but_needs_proof"
        row.classification_reason = "cheap_but_fundamental_below_gate"
    elif not row.passed_value_trap_gate and row.raw_composite_score >= gates["watchlist_min"]:
        row.classification = "cheap_but_needs_proof"
        row.classification_reason = "value_trap_soft_gate"
    elif row.final_investability_gate:
        row.classification = "tier_1_long_candidate"
        row.classification_reason = "all_tier1_gates_passed"
    elif row.raw_composite_score >= gates["watchlist_min"]:
        row.classification = "watchlist"
        row.classification_reason = "raw_score_above_watchlist_floor"
    else:
        row.classification = "avoid"
        row.classification_reason = "raw_score_below_watchlist_floor"
    row.decision_bucket = row.classification


def build_rows(
    conn: Any,
    *,
    asof: str,
    weights: dict[str, float],
    config: dict[str, Any],
    ticker_filter: set[str],
    max_tickers: int,
) -> list[ScoreRow]:
    financial_rows = load_financial_rows(conn, asof=asof, ticker_filter=ticker_filter, max_tickers=max_tickers)
    fda_rows = load_latest_feature(conn, "feature_fda_product_risk", "fda_product_score", asof=asof)
    reimbursement_rows = load_latest_feature(conn, "feature_reimbursement", "score", asof=asof)
    technical_rows = load_latest_feature(conn, "feature_technical_entry", "technical_score", asof=asof)
    durable_rows = load_latest_feature(conn, "feature_durable_growth", "score", asof=asof)
    sentiment_rows = load_latest_feature(conn, "feature_sentiment_catalyst", "score", asof=asof)
    durable_proxy = durable_growth_proxy(financial_rows)
    neutral_fundamental = component_neutral(config, "fundamental_quality", "scoring.neutral_fundamental_quality_score", 50.0)
    neutral_durable = component_neutral(config, "durable_growth", "scoring.neutral_durable_growth_score", 50.0)
    neutral_reimbursement = component_neutral(config, "reimbursement", "scoring.neutral_reimbursement_score", 50.0)
    neutral_fda_no_data = component_neutral(config, "fda_product", "scoring.neutral_fda_no_data_score", 45.0)
    neutral_valuation = component_neutral(config, "valuation", "scoring.neutral_valuation_score", 50.0)
    neutral_technical = component_neutral(config, "technical_entry", "scoring.neutral_technical_entry_score", 50.0)
    neutral_sentiment = component_neutral(config, "sentiment_catalyst", "scoring.neutral_sentiment_catalyst_score", 50.0)
    sentiment_proxy = sentiment_catalyst_proxy(financial_rows, config=config, neutral=neutral_sentiment)
    gates = {
        "composite_min": cfg_float(config, "scoring.gates.composite_min", 75.0),
        "fundamental_quality_min": cfg_float(config, "scoring.gates.fundamental_quality_min", 70.0),
        "durable_growth_min": cfg_float(config, "scoring.gates.durable_growth_min", 60.0),
        "fda_product_min": cfg_float(config, "scoring.gates.fda_product_min", 60.0),
        "reimbursement_min": cfg_float(config, "scoring.gates.reimbursement_min", 45.0),
        "valuation_min": cfg_float(config, "scoring.gates.valuation_min", 60.0),
        "technical_entry_min": cfg_float(config, "scoring.gates.technical_entry_min", 55.0),
        "data_completeness_min": cfg_float(config, "scoring.gates.data_completeness_min", 90.0),
        "min_avg_dollar_volume_60d": cfg_float(config, "scoring.gates.min_avg_dollar_volume_60d", 1_000_000.0),
        "watchlist_min": cfg_float(config, "scoring.gates.watchlist_min", 60.0),
        "value_trap_max": cfg_float(config, "scoring.gates.value_trap_max", 20.0),
        "value_trap_hard_max": cfg_float(config, "scoring.gates.value_trap_hard_max", 85.0),
    }
    rank_composite = cfg_bool(config, "scoring.cross_sectional_composite_rank", True)
    model_version = str(cfg_get(config, "scoring.model_version", "med_device_score_v1") or "med_device_score_v1").strip()
    rows: list[ScoreRow] = []
    for item in financial_rows:
        company_id = int(item["company_id"])
        fda_item = fda_rows.get(company_id, {})
        reimbursement_item = reimbursement_rows.get(company_id, {})
        technical_item = technical_rows.get(company_id, {})
        durable_item = durable_rows.get(company_id, {})
        sentiment_item = sentiment_rows.get(company_id, {})
        has_durable_proxy = company_id in durable_proxy
        sentiment_proxy_item = sentiment_proxy.get(company_id)
        has_sentiment_proxy = sentiment_proxy_item is not None
        durable_table_score = to_float(durable_item.get("score")) if durable_item else None
        sentiment_table_score = to_float(sentiment_item.get("score")) if sentiment_item else None
        has_durable_live_score = durable_table_score is not None or has_durable_proxy
        has_sentiment_live_score = sentiment_table_score is not None or has_sentiment_proxy
        fda_hard_flag = int(fda_item.get("hard_red_flag") or 0) if fda_item else 0
        fda_data_available = int(fda_item.get("fda_data_available") or 0) if fda_item else 0
        reimbursement_hard_flag = int(reimbursement_item.get("hard_red_flag") or 0) if reimbursement_item else 0
        reimbursement_table_score = to_float(reimbursement_item.get("score")) if reimbursement_item else None
        reimbursement_status = str(reimbursement_item.get("reimbursement_status") or "unknown").strip().lower() if reimbursement_item else "unknown"
        direct_code_evidence = int_flag(reimbursement_item.get("direct_code_evidence")) if reimbursement_item else 0
        payment_rate_evidence = int_flag(reimbursement_item.get("payment_rate_evidence")) if reimbursement_item else 0
        coverage_policy_evidence = int_flag(reimbursement_item.get("coverage_policy_evidence")) if reimbursement_item else 0
        procedure_bundled_flag = int_flag(reimbursement_item.get("procedure_bundled_flag")) if reimbursement_item else 0
        capital_equipment_flag = int_flag(reimbursement_item.get("capital_equipment_flag")) if reimbursement_item else 0
        diagnostics_lab_flag = int_flag(reimbursement_item.get("diagnostics_lab_flag")) if reimbursement_item else 0
        unknown_reimbursement_flag = int_flag(reimbursement_item.get("unknown_reimbursement_flag")) if reimbursement_item else 1
        has_reimbursement_live_score = (
            bool(reimbursement_item)
            and reimbursement_component_is_live(reimbursement_item, reimbursement_table_score)
        )
        fda_review_state = str(fda_item.get("review_adjusted_fda_state") or "") if fda_item else ""
        fda_score = score_or(fda_item.get("fda_product_score"), neutral_fda_no_data) if fda_item else neutral_fda_no_data
        if fda_item and not fda_data_available and not fda_review_state.startswith("manual_fda_footprint_"):
            fda_score = neutral_fda_no_data
        durable_score = (
            score_or(durable_item.get("score"), durable_proxy.get(company_id, neutral_durable))
            if durable_item
            else durable_proxy.get(company_id, neutral_durable)
        )
        sentiment_score = (
            score_or(
                sentiment_item.get("score"),
                sentiment_proxy_item.score if sentiment_proxy_item is not None else neutral_sentiment,
            )
            if sentiment_item
            else sentiment_proxy_item.score if sentiment_proxy_item is not None else neutral_sentiment
        )
        technical_score = score_or(technical_item.get("technical_score"), neutral_technical) if technical_item else neutral_technical
        avg_dollar_volume_60d = to_float(technical_item.get("avg_dollar_volume_60d")) if technical_item else None
        liquidity_score = to_float(technical_item.get("liquidity_score")) if technical_item else None
        market_cap = to_float(item.get("market_cap"))
        current_shares_outstanding = to_float(item.get("current_shares_outstanding"))
        diluted_weighted_average_shares = to_float(item.get("diluted_weighted_average_shares"))
        basic_weighted_average_shares = to_float(item.get("basic_weighted_average_shares"))
        market_cap_validated_flag = int(item.get("market_cap_validated_flag") or 0)
        live_components = [
            to_float(item.get("fundamental_quality_score_v1")) is not None,
            has_durable_live_score,
            bool(fda_item) and to_float(fda_item.get("fda_product_score")) is not None,
            has_reimbursement_live_score,
            to_float(item.get("valuation_score_v1")) is not None,
            bool(technical_item) and to_float(technical_item.get("technical_score")) is not None,
            has_sentiment_live_score,
        ]
        row = ScoreRow(
            asof_date=asof,
            scoring_model_version=model_version,
            rank=0,
            company_id=company_id,
            ticker=normalize_ticker(item.get("ticker")),
            company_name=str(item.get("company_name") or ""),
            subsector=str(item.get("subsector") or ""),
            fundamental_quality_score=score_or(item.get("fundamental_quality_score_v1"), neutral_fundamental),
            durable_growth_score=durable_score,
            fda_product_score=fda_score,
            fda_data_available=fda_data_available,
            reimbursement_score=score_or(reimbursement_table_score, neutral_reimbursement) if reimbursement_item else neutral_reimbursement,
            reimbursement_status=reimbursement_status,
            direct_code_evidence=direct_code_evidence,
            payment_rate_evidence=payment_rate_evidence,
            coverage_policy_evidence=coverage_policy_evidence,
            procedure_bundled_flag=procedure_bundled_flag,
            capital_equipment_flag=capital_equipment_flag,
            diagnostics_lab_flag=diagnostics_lab_flag,
            unknown_reimbursement_flag=unknown_reimbursement_flag,
            valuation_score=score_or(item.get("valuation_score_v1"), neutral_valuation),
            technical_entry_score=technical_score,
            sentiment_catalyst_score=sentiment_score,
            value_trap_score=to_float(item.get("value_trap_score")) or 0.0,
            live_component_count=sum(1 for value in live_components if value),
            data_completeness_score=round(100.0 * sum(1 for value in live_components if value) / len(live_components), 2),
            hard_red_flag=1 if fda_hard_flag or reimbursement_hard_flag else 0,
            hard_red_flag_reasons=";".join(
                reason
                for reason in [
                    str(fda_item.get("hard_red_flag_reasons") or ""),
                    str(reimbursement_item.get("hard_red_flag_reasons") or ""),
                ]
                if reason
            ),
            durable_growth_proxy_available=has_durable_proxy,
            sentiment_proxy_available=has_sentiment_proxy,
            sentiment_proxy_source=sentiment_proxy_item.source if sentiment_proxy_item is not None else "",
            sentiment_proxy_input=sentiment_proxy_item.input_name if sentiment_proxy_item is not None else "",
            avg_dollar_volume_60d=avg_dollar_volume_60d,
            liquidity_score=liquidity_score,
            market_cap=market_cap,
            current_shares_outstanding=current_shares_outstanding,
            diluted_weighted_average_shares=diluted_weighted_average_shares,
            basic_weighted_average_shares=basic_weighted_average_shares,
            shares_source_concept=str(item.get("shares_source_concept") or ""),
            shares_source_form=str(item.get("shares_source_form") or ""),
            shares_source_period=str(item.get("shares_source_period") or ""),
            market_cap_validated_flag=market_cap_validated_flag,
            fda_review_state=fda_review_state,
        )
        row.fda_product_score = row.fda_product_score if row.fda_product_score is not None else 50.0
        row.durable_growth_score = row.durable_growth_score if row.durable_growth_score is not None else 50.0
        row.reimbursement_score = row.reimbursement_score if row.reimbursement_score is not None else neutral_reimbursement
        row.technical_entry_score = row.technical_entry_score if row.technical_entry_score is not None else neutral_technical
        row.sentiment_catalyst_score = row.sentiment_catalyst_score if row.sentiment_catalyst_score is not None else neutral_sentiment
        component_scores = {
            "fundamental_quality": row.fundamental_quality_score,
            "durable_growth": row.durable_growth_score,
            "fda_product": row.fda_product_score,
            "reimbursement": row.reimbursement_score,
            "valuation": row.valuation_score,
            "technical_entry": row.technical_entry_score,
            "sentiment_catalyst": row.sentiment_catalyst_score,
        }
        component_available = {
            "fundamental_quality": live_components[0],
            "durable_growth": live_components[1],
            "fda_product": live_components[2],
            "reimbursement": live_components[3],
            "valuation": live_components[4],
            "technical_entry": live_components[5],
            "sentiment_catalyst": live_components[6],
        }
        raw_composite = weighted_available_score(component_scores, component_available, weights)
        row.raw_composite_score = round(
            clamp(raw_composite * value_trap_discount(row.value_trap_score)),
            2,
        )
        row.composite_score = row.raw_composite_score
        rows.append(row)
    if rank_composite:
        cross_sectional_percentile_rank(rows)
    else:
        for row in rows:
            row.composite_percentile = row.raw_composite_score
    for row in rows:
        classify(row, gates=gates)
        row.top_positive_drivers, row.top_negative_drivers = score_drivers(row)
    rows.sort(
        key=lambda item: (
            -item.composite_percentile,
            -item.raw_composite_score,
            -item.fundamental_quality_score,
            -item.data_completeness_score,
            item.ticker,
        )
    )
    for rank, row in enumerate(rows, start=1):
        row.rank = rank
    previous_scores = load_previous_scores(conn, asof=asof)
    for row in rows:
        previous = previous_scores.get(row.company_id)
        if not previous:
            continue
        previous_score = to_float(previous.get("composite_score"))
        previous_rank = int(previous["rank"]) if previous.get("rank") is not None else None
        previous_classification = str(previous.get("classification") or "")
        row.composite_score_delta = round(row.composite_score - previous_score, 2) if previous_score is not None else None
        row.rank_delta = previous_rank - row.rank if previous_rank is not None else None
        if previous_classification and previous_classification != row.classification:
            row.classification_change = f"{previous_classification}->{row.classification}"
    return rows


def upsert_rows(conn: Any, rows: list[ScoreRow]) -> int:
    if not rows:
        return 0
    now = utc_now()
    columns = [
        "asof_date",
        "company_id",
        "scoring_model_version",
        "composite_score",
        "raw_composite_score",
        "composite_percentile",
        "fundamental_quality_score",
        "durable_growth_score",
        "fda_product_score",
        "reimbursement_score",
        "reimbursement_status",
        "direct_code_evidence",
        "payment_rate_evidence",
        "coverage_policy_evidence",
        "procedure_bundled_flag",
        "capital_equipment_flag",
        "diagnostics_lab_flag",
        "unknown_reimbursement_flag",
        "valuation_score",
        "technical_entry_score",
        "sentiment_catalyst_score",
        "value_trap_score",
        "rank",
        "data_completeness_score",
        "live_component_count",
        "composite_score_delta",
        "rank_delta",
        "classification_change",
        "classification",
        "decision_bucket",
        "entry_status",
        "gate_status",
        "review_reason",
        "failed_gates",
        "classification_reason",
        "fda_review_state",
        "market_cap",
        "current_shares_outstanding",
        "diluted_weighted_average_shares",
        "basic_weighted_average_shares",
        "shares_source_concept",
        "shares_source_form",
        "shares_source_period",
        "market_cap_validated_flag",
        "avg_dollar_volume_60d",
        "liquidity_score",
        "capacity_bucket",
        "min_position_size_feasible",
        "max_position_size_feasible",
        "passed_raw_score_gate",
        "passed_fundamental_gate",
        "passed_growth_gate",
        "passed_fda_gate",
        "passed_reimbursement_gate",
        "passed_valuation_gate",
        "passed_technical_gate",
        "passed_value_trap_gate",
        "passed_data_quality_gate",
        "passed_liquidity_gate",
        "passed_fda_manual_review_gate",
        "final_investability_gate",
        "hard_red_flag",
        "hard_red_flag_reasons",
        "top_positive_drivers_json",
        "top_negative_drivers_json",
        "created_at",
        "updated_at",
    ]
    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(quote_identifier(column) for column in columns)
    update_sql = ",\n            ".join(
        f"{quote_identifier(column)} = excluded.{quote_identifier(column)}"
        for column in columns
        if column not in {"asof_date", "company_id", "created_at"}
    )
    conn.executemany(
        f"""
        INSERT INTO med_device_daily_scores({column_sql})
        VALUES ({placeholders})
        ON CONFLICT(asof_date, company_id) DO UPDATE SET
            {update_sql}
        """,
        [
            (
                row.asof_date,
                row.company_id,
                row.scoring_model_version,
                row.composite_score,
                row.raw_composite_score,
                row.composite_percentile,
                row.fundamental_quality_score,
                row.durable_growth_score,
                row.fda_product_score,
                row.reimbursement_score,
                row.reimbursement_status,
                row.direct_code_evidence,
                row.payment_rate_evidence,
                row.coverage_policy_evidence,
                row.procedure_bundled_flag,
                row.capital_equipment_flag,
                row.diagnostics_lab_flag,
                row.unknown_reimbursement_flag,
                row.valuation_score,
                row.technical_entry_score,
                row.sentiment_catalyst_score,
                row.value_trap_score,
                row.rank,
                row.data_completeness_score,
                row.live_component_count,
                row.composite_score_delta,
                row.rank_delta,
                row.classification_change,
                row.classification,
                row.decision_bucket,
                row.entry_status,
                row.gate_status,
                row.review_reason,
                row.failed_gates,
                row.classification_reason,
                row.fda_review_state,
                row.market_cap,
                row.current_shares_outstanding,
                row.diluted_weighted_average_shares,
                row.basic_weighted_average_shares,
                row.shares_source_concept,
                row.shares_source_form,
                row.shares_source_period,
                row.market_cap_validated_flag,
                row.avg_dollar_volume_60d,
                row.liquidity_score,
                row.capacity_bucket,
                row.min_position_size_feasible,
                row.max_position_size_feasible,
                row.passed_raw_score_gate,
                row.passed_fundamental_gate,
                row.passed_growth_gate,
                row.passed_fda_gate,
                row.passed_reimbursement_gate,
                row.passed_valuation_gate,
                row.passed_technical_gate,
                row.passed_value_trap_gate,
                row.passed_data_quality_gate,
                row.passed_liquidity_gate,
                row.passed_fda_manual_review_gate,
                row.final_investability_gate,
                row.hard_red_flag,
                row.hard_red_flag_reasons,
                json.dumps(row.top_positive_drivers, ensure_ascii=True),
                json.dumps(row.top_negative_drivers, ensure_ascii=True),
                now,
                now,
            )
            for row in rows
        ],
    )
    return len(rows)


def row_to_dict(row: ScoreRow) -> dict[str, Any]:
    item = {field: getattr(row, field) for field in FIELDNAMES if hasattr(row, field)}
    item["top_positive_drivers"] = "; ".join(row.top_positive_drivers)
    item["top_negative_drivers"] = "; ".join(row.top_negative_drivers)
    return item


def write_csv(path: Path, rows: list[ScoreRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(row_to_dict(row) for row in rows)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(config, "scoring.output_csv", "../output/med_devices_reports/med_device_daily_composite_scores.csv"),
            base_dir=base_dir,
        )
    )
    ticker_filter = {normalize_ticker(value) for value in str(args.tickers or "").split(",") if normalize_ticker(value)}
    weights = load_weights(config)
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        run_id = start_run(conn, run_type="build_med_device_daily_scores", input_path=config_path)
        try:
            asof = args.asof.strip() or latest_financial_asof(conn)
            preflight_required_features(conn, asof=asof)
            preflight_feature_freshness(
                conn,
                asof=asof,
                max_staleness_days=int(cfg_get(config, "scoring.max_feature_staleness_days", 7)),
            )
            rows = build_rows(
                conn,
                asof=asof,
                weights=weights,
                config=config,
                ticker_filter=ticker_filter,
                max_tickers=int(args.max_tickers),
            )
            upserted = upsert_rows(conn, rows)
            upsert_durable_growth_proxy_rows(conn, rows)
            upsert_sentiment_proxy_rows(conn, rows)
            write_csv(output_csv, rows)
            message = f"asof={asof} rows={upserted} output={output_csv}"
            finish_run(conn, run_id=run_id, status="success", row_count=upserted, message=message)
            LOGGER.info("Daily composite scores complete: %s", message)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()

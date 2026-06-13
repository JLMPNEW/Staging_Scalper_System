from __future__ import annotations

import argparse
import csv
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from technology.core.config import cfg_get, load_yaml, resolve_path
from technology.core.db import connect, finish_run, init_db, start_run, utc_now
from technology.core.logging_utils import configure_utc_logging
from technology.core.source_registry import load_source_registry, upsert_source_registry
from technology.core.text_norm import normalize_ticker


LOGGER = logging.getLogger("technology_scoring_features")

CORE_COMPONENT_DEFS = [
    {
        "component_name": "quality",
        "component_group": "core",
        "display_name": "Quality",
        "description": "Margin quality, cash conversion, balance-sheet strength, SBC control, and dilution control.",
        "is_core_component": 1,
    },
    {
        "component_name": "growth",
        "component_group": "core",
        "display_name": "Growth",
        "description": "Revenue, gross-profit, operating-income, FCF, and acceleration signals.",
        "is_core_component": 1,
    },
    {
        "component_name": "valuation",
        "component_group": "core",
        "display_name": "Valuation",
        "description": "EV/gross profit, EV/operating income, and FCF yield signals.",
        "is_core_component": 1,
    },
    {
        "component_name": "market_behavior",
        "component_group": "core",
        "display_name": "Market Behavior",
        "description": "Momentum, relative strength, volatility, drawdown, liquidity, and trend proximity.",
        "is_core_component": 1,
    },
    {
        "component_name": "positioning",
        "component_group": "core",
        "display_name": "Positioning",
        "description": "Insider, 13F, short-interest, days-to-cover, and borrow-rate pressure.",
        "is_core_component": 1,
    },
    {
        "component_name": "risk_control",
        "component_group": "core",
        "display_name": "Risk Control",
        "description": "Balance-sheet, liquidity, volatility, inventory, short-pressure, borrow, and SBC controls.",
        "is_core_component": 1,
    },
]

DEFAULT_OVERLAY_COMPONENTS = [
    "sector_cycle",
    "equipment_cycle",
    "sector_inventory_cycle",
    "big_tech_capex",
    "memory_ai_proxy",
    "innovation",
    "geo_customer_risk",
]

COMPONENT_FIELD_MAP = {
    "quality": ("quality_score", "quality_component_quality"),
    "growth": ("growth_score", "growth_component_quality"),
    "valuation": ("valuation_score", "valuation_component_quality"),
    "market_behavior": ("market_behavior_score", "market_component_quality"),
    "positioning": ("positioning_score", "positioning_component_quality"),
    "risk_control": ("risk_control_score", "risk_component_quality"),
}

# Subfeature registry: (raw_field, score_field, higher_is_better, validity_filter).
# ev_gross_profit / ev_operating_income are emitted by Stage 4 only when the
# denominator is positive, so a negative value means negative enterprise value
# (cash > market cap). With higher_is_better=False those rank best, as they should.
SUBFEATURE_SPECS: list[tuple[str, str, bool, Any]] = [
    ("gross_margin", "gross_margin_score", True, None),
    ("operating_margin", "operating_margin_score", True, None),
    ("fcf_margin", "fcf_margin_score", True, None),
    ("fcf_to_net_income", "fcf_to_net_income_score", True, None),
    ("net_cash_to_assets", "net_cash_to_assets_score", True, None),
    ("sbc_pct_revenue", "sbc_pct_revenue_score", False, lambda value: value >= 0),
    ("share_count_yoy_growth", "share_count_yoy_growth_score", False, None),
    ("revenue_yoy_growth", "revenue_yoy_growth_score", True, None),
    ("gross_profit_yoy_growth", "gross_profit_yoy_growth_score", True, None),
    ("operating_income_yoy_growth", "operating_income_yoy_growth_score", True, None),
    ("free_cash_flow_yoy_growth", "free_cash_flow_yoy_growth_score", True, None),
    ("revenue_acceleration", "revenue_acceleration_score", True, None),
    ("ev_gross_profit", "ev_gross_profit_score", False, None),
    ("ev_operating_income", "ev_operating_income_score", False, None),
    ("fcf_yield", "fcf_yield_score", True, None),
    ("ret_12m_ex_1m", "ret_12m_ex_1m_score", True, None),
    ("ret_3m", "ret_3m_score", True, None),
    ("rel_strength_soxx_3m", "rel_strength_soxx_3m_score", True, None),
    ("realized_vol_60d", "realized_vol_60d_score", False, lambda value: value >= 0),
    ("max_drawdown_12m", "max_drawdown_12m_score", True, None),
    ("distance_from_52w_high", "distance_from_52w_high_score", True, None),
    ("avg_dollar_volume_60d", "avg_dollar_volume_60d_score", True, lambda value: value >= 0),
    ("insider_net_value_90d", "insider_net_value_90d_score", True, None),
    ("insider_cluster_buyers_90d", "insider_cluster_buyers_90d_score", True, lambda value: value >= 0),
    ("institutional_ownership_delta_pct", "institutional_ownership_delta_pct_score", True, None),
    ("latest_short_interest_pct_float", "latest_short_interest_pct_float_score", False, lambda value: value >= 0),
    ("short_interest_change_3m", "short_interest_change_3m_score", False, None),
    ("latest_days_to_cover", "latest_days_to_cover_score", False, lambda value: value >= 0),
    ("latest_borrow_fee_rate", "latest_borrow_fee_rate_score", False, lambda value: value >= 0),
    ("inventory_days", "inventory_days_score", False, lambda value: value >= 0),
    # Ticker-specific cycle exposure: cohort-shrunk beta to WSTS billings YoY
    # innovations multiplied by the current (publication-lagged) YoY state.
    # Measurement-only for now: computed in the diagnostics/Stage 8 panels and
    # IC-tested there; carries no production weight until validated on the
    # extended multi-cycle history.
    ("wsts_cycle_exposure", "wsts_cycle_exposure_score", True, None),
    # Inventory-cycle signals (measurement-only, same gating as above):
    # year-over-year change in inventory days, and inventory growth in excess
    # of TTM revenue growth — the classic semiconductor overbuild red flags.
    # Rising values are bad, hence higher_is_better=False.
    ("inventory_days_yoy_change", "inventory_days_yoy_change_score", False, None),
    ("inventory_to_revenue_growth_gap", "inventory_to_revenue_growth_gap_score", False, None),
]

COMPONENT_SPECS: dict[str, list[tuple[str, float]]] = {
    "quality": [
        ("gross_margin_score", 0.18),
        ("operating_margin_score", 0.18),
        ("fcf_margin_score", 0.16),
        ("fcf_to_net_income_score", 0.12),
        ("net_cash_to_assets_score", 0.12),
        ("sbc_pct_revenue_score", 0.10),
        ("share_count_yoy_growth_score", 0.14),
    ],
    "growth": [
        ("revenue_yoy_growth_score", 0.25),
        ("gross_profit_yoy_growth_score", 0.20),
        ("operating_income_yoy_growth_score", 0.20),
        ("free_cash_flow_yoy_growth_score", 0.15),
        ("revenue_acceleration_score", 0.20),
    ],
    "valuation": [
        ("ev_gross_profit_score", 0.35),
        ("ev_operating_income_score", 0.25),
        ("fcf_yield_score", 0.40),
    ],
    "market_behavior": [
        ("ret_12m_ex_1m_score", 0.25),
        ("ret_3m_score", 0.15),
        ("rel_strength_soxx_3m_score", 0.20),
        ("realized_vol_60d_score", 0.15),
        ("max_drawdown_12m_score", 0.10),
        ("distance_from_52w_high_score", 0.10),
        ("avg_dollar_volume_60d_score", 0.05),
    ],
    "positioning": [
        ("insider_net_value_90d_score", 0.20),
        ("insider_cluster_buyers_90d_score", 0.20),
        ("institutional_ownership_delta_pct_score", 0.20),
        ("short_interest_change_3m_score", 0.15),
        ("latest_short_interest_pct_float_score", 0.10),
        ("latest_days_to_cover_score", 0.05),
        ("latest_borrow_fee_rate_score", 0.10),
    ],
    "risk_control": [
        ("net_cash_to_assets_score", 0.20),
        ("realized_vol_60d_score", 0.15),
        ("max_drawdown_12m_score", 0.15),
        ("avg_dollar_volume_60d_score", 0.15),
        ("latest_short_interest_pct_float_score", 0.10),
        ("latest_borrow_fee_rate_score", 0.10),
        ("inventory_days_score", 0.10),
        ("sbc_pct_revenue_score", 0.05),
    ],
}

CSV_FIELDS = [
    "ticker",
    "asof_date",
    "feature_status",
    "rank_ready_flag",
    "calibration_eligible_flag",
    "calibration_cohort_id",
    "market_feature_asof_date",
    "financial_feature_asof_date",
    "positioning_feature_asof_date",
    "quality_score",
    "growth_score",
    "valuation_score",
    "market_behavior_score",
    "positioning_score",
    "risk_control_score",
    "sector_overlay_score",
    "core_data_quality_confidence",
    "full_data_quality_confidence",
    "core_available_component_count",
    "core_missing_component_count",
    "review_reason",
]


@dataclass(frozen=True)
class ScoringFeatureSettings:
    description: str
    default_config: Path
    config_key: str
    default_model_family: str
    default_source_id: str
    run_type: str
    validation_run_type: str


def parse_build_args(settings: ScoringFeatureSettings) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=settings.description)
    parser.add_argument("--config", type=Path, default=settings.default_config)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default="", help="Score-feature as-of date. Defaults to today.")
    parser.add_argument("--tickers", default="", help="Comma-separated ticker filter for development runs.")
    parser.add_argument("--output-csv", type=Path, default=None)
    return parser.parse_args()


def parse_validate_args(settings: ScoringFeatureSettings) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=settings.description)
    parser.add_argument("--config", type=Path, default=settings.default_config)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default="", help="Validation as-of date. Defaults to latest scoring feature date.")
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def safe_float(raw: object) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def safe_div(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0:
        return None
    value = num / den
    return value if math.isfinite(value) else None


def pct_change(value: float | None, prior: float | None) -> float | None:
    ratio = safe_div(value, prior)
    return ratio - 1.0 if ratio is not None else None


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def qmarks(values: list[str]) -> str:
    if not values:
        raise ValueError("values cannot be empty")
    return ",".join("?" for _ in values)


def cfg_bool(raw: Any, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "y"}


def cfg_ticker_set(raw: Any) -> set[str]:
    if raw is None:
        return set()
    values = raw.split(",") if isinstance(raw, str) else raw
    if not isinstance(values, (list, tuple, set)):
        return set()
    return {ticker for ticker in (normalize_ticker(value) for value in values) if ticker}


def overlay_component_defs(component_names: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name in component_names:
        out.append(
            {
                "component_name": name,
                "component_group": "sector_overlay",
                "display_name": name.replace("_", " ").title(),
                "description": f"Reserved sector-overlay component for {name.replace('_', ' ')}.",
                "is_core_component": 0,
            }
        )
    return out


def percentile_scores(
    rows: list[dict[str, Any]],
    key: str,
    *,
    higher_is_better: bool,
    valid: Callable[[float], bool] | None = None,
) -> dict[str, float]:
    values: list[tuple[str, float]] = []
    for row in rows:
        value = safe_float(row.get(key))
        if value is None:
            continue
        if valid is not None and not valid(value):
            continue
        values.append((str(row["ticker"]), value))
    if not values:
        return {}
    n = len(values)
    out: dict[str, float] = {}
    for ticker, value in values:
        less = sum(1 for _, candidate in values if candidate < value)
        equal = sum(1 for _, candidate in values if candidate == value)
        percentile = 100.0 * (less + 0.5 * equal) / n
        out[ticker] = clamp(percentile if higher_is_better else 100.0 - percentile)
    return out


def cohort_percentile_scores(
    rows: list[dict[str, Any]],
    key: str,
    *,
    higher_is_better: bool,
) -> dict[str, float]:
    out: dict[str, float] = {}
    by_cohort: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        cohort = str(row.get("calibration_cohort_id") or "")
        by_cohort.setdefault(cohort, []).append(row)
    for cohort_rows in by_cohort.values():
        out.update(percentile_scores(cohort_rows, key, higher_is_better=higher_is_better))
    return out


def weighted_available_score(
    row: dict[str, Any],
    specs: list[tuple[str, float]],
    *,
    neutral_score: float,
) -> tuple[float, float, int, int, str]:
    total_weight = sum(weight for _, weight in specs)
    available_weight = 0.0
    weighted_score = 0.0
    available_count = 0
    missing_keys: list[str] = []
    for score_key, weight in specs:
        score = safe_float(row.get(score_key))
        if score is None:
            missing_keys.append(score_key)
            continue
        available_weight += weight
        weighted_score += score * weight
        available_count += 1
    if available_weight <= 0:
        return neutral_score, 0.0, 0, len(specs), "missing"
    score = weighted_score / available_weight
    quality = available_weight / total_weight if total_weight > 0 else 0.0
    return clamp(score), max(0.0, min(1.0, quality)), available_count, len(missing_keys), ";".join(missing_keys)


def latest_row(
    conn: Any,
    table: str,
    ticker: str,
    source_id: str,
    model_family: str,
    asof: date,
    order_cols: str,
) -> Any | None:
    return conn.execute(
        f"""
        SELECT *
        FROM {table}
        WHERE ticker = ?
          AND source_id = ?
          AND model_family = ?
          AND asof_date <= ?
        ORDER BY {order_cols}
        LIMIT 1
        """,
        (ticker, source_id, model_family, asof.isoformat()),
    ).fetchone()


def load_universe(conn: Any, model_family: str, ticker_filter: set[str]) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            c.company_id,
            c.ticker,
            t.calibration_cohort_id,
            t.calibration_cohort
        FROM dim_company c
        JOIN dim_technology_taxonomy t
          ON t.ticker = c.ticker
         AND t.model_family = ?
        WHERE c.is_active = 1
        ORDER BY c.ticker
        """,
        (model_family,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        if not ticker or (ticker_filter and ticker not in ticker_filter):
            continue
        out.append(dict(row) | {"ticker": ticker})
    return out


def latest_share_growth(conn: Any, ticker: str, source_id: str, model_family: str, asof: date) -> float | None:
    rows = conn.execute(
        """
        SELECT fiscal_period_end, diluted_shares
        FROM feature_financial_statement
        WHERE ticker = ?
          AND source_id = ?
          AND model_family = ?
          AND asof_date <= ?
          AND diluted_shares IS NOT NULL
        ORDER BY fiscal_period_end DESC, asof_date DESC
        LIMIT 12
        """,
        (ticker, source_id, model_family, asof.isoformat()),
    ).fetchall()
    if len(rows) < 2:
        return None
    latest = safe_float(rows[0]["diluted_shares"])
    latest_end = parse_date(rows[0]["fiscal_period_end"])
    if latest is None or latest_end is None:
        return None
    for row in rows[1:]:
        prior = safe_float(row["diluted_shares"])
        prior_end = parse_date(row["fiscal_period_end"])
        if prior is None or prior_end is None:
            continue
        day_gap = (latest_end - prior_end).days
        if 300 <= day_gap <= 460:
            return pct_change(latest, prior)
    return None


def load_profile(conn: Any, ticker: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM dim_issuer_reporting_profile WHERE ticker = ?", (ticker,)).fetchone()
    return dict(row) if row is not None else {}


def adj_close_at(conn: Any, ticker: str, price_source: str, date_iso: str) -> float | None:
    row = conn.execute(
        """
        SELECT adj_close
        FROM fact_price_ohlcv
        WHERE ticker = ? AND source_id = ? AND bar_date <= ? AND adj_close IS NOT NULL
        ORDER BY bar_date DESC
        LIMIT 1
        """,
        (ticker, price_source, date_iso),
    ).fetchone()
    return safe_float(row["adj_close"]) if row is not None else None


def reprice_valuation_fields(row: dict[str, Any], financial: Any, price_ratio: float | None) -> None:
    """Move filing-date valuation ratios to the scoring date.

    Stage 4 stores market_cap/EV ratios priced at the filing availability date,
    which can be a quarter stale by the scoring asof. Using the adjusted-close
    ratio r = adj(asof)/adj(filing): mcap(t) = mcap_f*r, EV(t) = EV_f + mcap_f*(r-1)
    with net debt held constant between filings, fcf_yield(t) = fcf_yield_f / r
    (FCF yield is market-cap based upstream).
    """
    mcap_f = safe_float(row.get("market_cap"))
    if price_ratio is None or price_ratio <= 0 or mcap_f is None or mcap_f <= 0:
        return
    row["market_cap"] = mcap_f * price_ratio
    fcf_yield = safe_float(row.get("fcf_yield"))
    if fcf_yield is not None:
        row["fcf_yield"] = fcf_yield / price_ratio
    net_cash = safe_float(financial["net_cash"])
    balance_rate = safe_float(financial["fx_rate_balance_sheet"])
    if net_cash is None or balance_rate is None:
        return
    ev_f = mcap_f - net_cash * balance_rate
    if abs(ev_f) < 1e-9:
        return
    ev_t = ev_f + mcap_f * (price_ratio - 1.0)
    for field in ("ev_gross_profit", "ev_operating_income"):
        ratio = safe_float(row.get(field))
        if ratio is not None:
            row[field] = ratio * ev_t / ev_f


def build_raw_rows(
    conn: Any,
    universe: list[dict[str, Any]],
    *,
    asof: date,
    model_family: str,
    market_source: str,
    financial_source: str,
    positioning_source: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in universe:
        ticker = str(item["ticker"])
        market = latest_row(
            conn,
            "feature_market_technical",
            ticker,
            market_source,
            model_family,
            asof,
            "asof_date DESC",
        )
        financial = latest_row(
            conn,
            "feature_financial_statement",
            ticker,
            financial_source,
            model_family,
            asof,
            "asof_date DESC, fiscal_period_end DESC",
        )
        positioning = latest_row(
            conn,
            "feature_positioning",
            ticker,
            positioning_source,
            model_family,
            asof,
            "asof_date DESC",
        )
        profile = load_profile(conn, ticker)
        row: dict[str, Any] = {
            "ticker": ticker,
            "company_id": item.get("company_id"),
            "asof_date": asof.isoformat(),
            "calibration_cohort_id": item.get("calibration_cohort_id"),
            "calibration_cohort": item.get("calibration_cohort"),
            "market_feature_asof_date": market["asof_date"] if market is not None else "",
            "financial_feature_asof_date": financial["asof_date"] if financial is not None else "",
            "positioning_feature_asof_date": positioning["asof_date"] if positioning is not None else "",
            "market_quality": market["market_data_quality"] if market is not None else "missing",
            "financial_quality": financial["data_quality_status"] if financial is not None else "missing",
            "positioning_quality": positioning["positioning_quality"] if positioning is not None else "missing",
            "reporting_standard": financial["reporting_standard"] if financial is not None else "",
            "financial_frequency": financial["financial_frequency"] if financial is not None else "",
            "latest_price": safe_float(market["latest_adj_close"]) if market is not None else None,
            "market_cap": safe_float(financial["market_cap"]) if financial is not None else None,
            "calibration_eligible_flag": int(profile.get("calibration_fundamental_eligible") or 0),
        }
        if market is not None:
            for key in (
                "ret_3m",
                "ret_12m_ex_1m",
                "rel_strength_soxx_3m",
                "realized_vol_60d",
                "max_drawdown_12m",
                "distance_from_52w_high",
                "avg_dollar_volume_60d",
                "low_liquidity_flag",
            ):
                row[key] = market[key]
        if financial is not None:
            for key in (
                "revenue_yoy_growth",
                "gross_profit_yoy_growth",
                "operating_income_yoy_growth",
                "free_cash_flow_yoy_growth",
                "revenue_acceleration",
                "gross_margin",
                "operating_margin",
                "fcf_margin",
                "net_cash_to_assets",
                "sbc_pct_revenue",
                "r_and_d_pct_revenue",
                "inventory_days",
                "ev_gross_profit",
                "ev_operating_income",
                "fcf_yield",
            ):
                row[key] = financial[key]
            net_income_ttm = safe_float(financial["net_income_ttm"])
            row["fcf_to_net_income"] = safe_div(safe_float(financial["free_cash_flow_ttm"]), net_income_ttm if net_income_ttm and net_income_ttm > 0 else None)
            row["share_count_yoy_growth"] = latest_share_growth(conn, ticker, financial_source, model_family, asof)
            filing_adj = adj_close_at(conn, ticker, market_source, str(financial["asof_date"]))
            asof_adj = adj_close_at(conn, ticker, market_source, asof.isoformat())
            price_ratio = asof_adj / filing_adj if filing_adj and asof_adj and filing_adj > 0 else None
            reprice_valuation_fields(row, financial, price_ratio)
        if positioning is not None:
            for key in (
                "insider_net_value_90d",
                "insider_cluster_buyers_90d",
                "institutional_ownership_delta_pct",
                "latest_short_interest_pct_float",
                "short_interest_change_3m",
                "latest_days_to_cover",
                "latest_borrow_fee_rate",
            ):
                row[key] = positioning[key]
        rows.append(row)
    return rows


def apply_subfeature_scores(rows: list[dict[str, Any]]) -> None:
    for raw_key, score_key, higher_is_better, valid in SUBFEATURE_SPECS:
        scores = percentile_scores(rows, raw_key, higher_is_better=higher_is_better, valid=valid)
        for row in rows:
            row[score_key] = scores.get(str(row["ticker"]))


def apply_component_scores(rows: list[dict[str, Any]], *, neutral_score: float) -> None:
    for row in rows:
        component_meta: dict[str, dict[str, Any]] = {}
        for component, specs in COMPONENT_SPECS.items():
            score, quality, available_count, missing_count, missing_detail = weighted_available_score(
                row,
                specs,
                neutral_score=neutral_score,
            )
            score_field, quality_field = COMPONENT_FIELD_MAP[component]
            row[score_field] = score
            row[quality_field] = quality
            component_meta[component] = {
                "component_score": score,
                "component_quality": quality,
                "component_status": "complete" if quality >= 0.75 else "partial" if quality > 0 else "missing",
                "available_subfeature_count": available_count,
                "missing_subfeature_count": missing_count,
                "default_applied": int(quality == 0),
                "review_reason": missing_detail,
            }
        row["_component_meta"] = component_meta


def upsert_component_defs(
    conn: Any,
    *,
    model_family: str,
    component_defs: list[dict[str, Any]],
    neutral_score: float,
    overlay_default_quality: float,
) -> None:
    now = utc_now()
    for component in component_defs:
        default_quality = overlay_default_quality if component["component_group"] == "sector_overlay" else 0.0
        default_status = "not_loaded" if component["component_group"] == "sector_overlay" else "missing"
        conn.execute(
            """
            INSERT INTO dim_scoring_component(
                model_family, component_name, component_group, display_name,
                description, is_core_component, default_score, default_quality,
                default_status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(model_family, component_name) DO UPDATE SET
                component_group = excluded.component_group,
                display_name = excluded.display_name,
                description = excluded.description,
                is_core_component = excluded.is_core_component,
                default_score = excluded.default_score,
                default_quality = excluded.default_quality,
                default_status = excluded.default_status,
                updated_at = excluded.updated_at
            """,
            (
                model_family,
                component["component_name"],
                component["component_group"],
                component["display_name"],
                component["description"],
                int(component["is_core_component"]),
                neutral_score,
                default_quality,
                default_status,
                now,
                now,
            ),
        )


def add_issue(conn: Any, row: dict[str, Any], *, source_id: str, stage: str, detail: str) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO data_quality_issues(
            detected_at, severity, stage, ticker, company_id, source_id, issue_type,
            issue_detail, resolution_status, created_at, updated_at
        )
        VALUES (?, 'warning', ?, ?, ?, ?, 'scoring_feature_contract_review', ?, 'open', ?, ?)
        """,
        (now, stage, row["ticker"], row.get("company_id"), source_id, detail, now, now),
    )


OVERLAY_SCORE_FIELDS = [
    "sector_cycle_score",
    "equipment_cycle_score",
    "sector_inventory_cycle_score",
    "big_tech_capex_score",
    "memory_ai_proxy_score",
    "innovation_score",
    "geo_customer_risk_score",
    "sector_overlay_score",
    "sector_overlay_quality",
    "sector_overlay_status",
]


def load_preserved_overlays(
    conn: Any,
    *,
    source_id: str,
    model_family: str,
    asof: date,
) -> dict[str, dict[str, Any]]:
    """Stage 6B overlay state already applied for this asof; preserved across 6A rebuilds."""
    try:
        rows = conn.execute(
            f"""
            SELECT ticker, {", ".join(OVERLAY_SCORE_FIELDS)}
            FROM feature_scoring_input
            WHERE source_id = ? AND model_family = ? AND asof_date = ?
              AND sector_overlay_status <> 'not_loaded'
            """,
            (source_id, model_family, asof.isoformat()),
        ).fetchall()
    except Exception:  # noqa: BLE001 - table may not exist on first run
        return {}
    return {normalize_ticker(row["ticker"]): dict(row) for row in rows}


def finalize_rows(
    rows: list[dict[str, Any]],
    *,
    config: dict[str, Any],
    config_key: str,
    neutral_score: float,
    overlay_default_quality: float,
    preserved_overlays: dict[str, dict[str, Any]] | None = None,
) -> None:
    component_weights = {
        "quality": float(cfg_get(config, f"{config_key}.core_component_weights.quality", 0.22)),
        "growth": float(cfg_get(config, f"{config_key}.core_component_weights.growth", 0.18)),
        "valuation": float(cfg_get(config, f"{config_key}.core_component_weights.valuation", 0.18)),
        "market_behavior": float(cfg_get(config, f"{config_key}.core_component_weights.market_behavior", 0.17)),
        "positioning": float(cfg_get(config, f"{config_key}.core_component_weights.positioning", 0.13)),
        "risk_control": float(cfg_get(config, f"{config_key}.core_component_weights.risk_control", 0.12)),
    }
    min_components = int(cfg_get(config, f"{config_key}.min_core_components_for_rank_ready", 4))
    min_confidence = float(cfg_get(config, f"{config_key}.min_core_data_quality_confidence", 0.40))
    overlay_names = list(cfg_get(config, f"{config_key}.sector_overlay_components", DEFAULT_OVERLAY_COMPONENTS) or DEFAULT_OVERLAY_COMPONENTS)
    preserved_overlays = preserved_overlays or {}

    for row in rows:
        preserved = preserved_overlays.get(str(row["ticker"]))
        if preserved:
            for field in OVERLAY_SCORE_FIELDS:
                row[field] = preserved[field]
        else:
            row["sector_cycle_score"] = neutral_score
            row["equipment_cycle_score"] = neutral_score
            row["sector_inventory_cycle_score"] = neutral_score
            row["big_tech_capex_score"] = neutral_score
            row["memory_ai_proxy_score"] = neutral_score
            row["innovation_score"] = neutral_score
            row["geo_customer_risk_score"] = neutral_score
            row["sector_overlay_score"] = neutral_score
            row["sector_overlay_quality"] = overlay_default_quality
            row["sector_overlay_status"] = "not_loaded"

        component_meta: dict[str, dict[str, Any]] = row["_component_meta"]
        available_components = sum(1 for component in component_weights if component_meta[component]["component_quality"] > 0)
        missing_components = len(component_weights) - available_components
        weighted_quality = sum(component_meta[name]["component_quality"] * weight for name, weight in component_weights.items())
        total_weight = sum(component_weights.values())
        core_confidence = weighted_quality / total_weight if total_weight > 0 else 0.0
        overlay_quality = float(safe_float(row.get("sector_overlay_quality")) or 0.0)
        full_confidence = core_confidence * 0.75 + overlay_quality * 0.25
        row["core_available_component_count"] = available_components
        row["core_missing_component_count"] = missing_components
        row["core_data_quality_confidence"] = max(0.0, min(1.0, core_confidence))
        row["full_data_quality_confidence"] = max(0.0, min(1.0, full_confidence))

        reasons: list[str] = []
        blocking_reasons: list[str] = []
        if not row.get("market_feature_asof_date"):
            reasons.append("missing_market_features")
            blocking_reasons.append("missing_market_features")
        if not row.get("financial_feature_asof_date"):
            reasons.append("missing_financial_features")
            blocking_reasons.append("missing_financial_features")
        if not row.get("positioning_feature_asof_date"):
            reasons.append("missing_positioning_features")
            blocking_reasons.append("missing_positioning_features")
        if row.get("market_quality") not in {"complete"}:
            reasons.append(f"market_quality={row.get('market_quality')}")
        if row.get("financial_quality") not in {"complete"}:
            reasons.append(f"financial_quality={row.get('financial_quality')}")
        if row.get("positioning_quality") not in {"complete"}:
            reasons.append(f"positioning_quality={row.get('positioning_quality')}")
        if int(row.get("calibration_eligible_flag") or 0) != 1:
            reasons.append("not_fundamental_calibration_eligible")
            blocking_reasons.append("not_fundamental_calibration_eligible")
        if available_components < min_components:
            reasons.append(f"insufficient_core_components={available_components}")
            blocking_reasons.append(f"insufficient_core_components={available_components}")
        if row["core_data_quality_confidence"] < min_confidence:
            reasons.append(f"low_core_data_quality_confidence={row['core_data_quality_confidence']:.3f}")
            blocking_reasons.append(f"low_core_data_quality_confidence={row['core_data_quality_confidence']:.3f}")
        row["rank_ready_flag"] = int(
            not blocking_reasons
            and available_components >= min_components
            and row["core_data_quality_confidence"] >= min_confidence
        )
        row["feature_status"] = "complete" if not reasons else "review"
        row["review_reason"] = ";".join(reasons)

        for overlay_name in overlay_names:
            component_meta[overlay_name] = {
                "component_score": neutral_score,
                "component_quality": overlay_default_quality,
                "component_status": "not_loaded",
                "available_subfeature_count": 0,
                "missing_subfeature_count": 0,
                "default_applied": 1,
                "review_reason": "sector_overlay_not_loaded_stage6a_placeholder",
                "component_group": "sector_overlay",
            }


def upsert_scoring_input(
    conn: Any,
    row: dict[str, Any],
    *,
    source_id: str,
    model_family: str,
    contract_version: str,
) -> None:
    now = utc_now()
    fields = [
        "ticker",
        "asof_date",
        "source_id",
        "model_family",
        "scoring_contract_version",
        "calibration_cohort_id",
        "calibration_cohort",
        "market_feature_asof_date",
        "financial_feature_asof_date",
        "positioning_feature_asof_date",
        "reporting_standard",
        "financial_frequency",
        "latest_price",
        "market_cap",
        "revenue_yoy_growth",
        "gross_profit_yoy_growth",
        "operating_income_yoy_growth",
        "free_cash_flow_yoy_growth",
        "revenue_acceleration",
        "gross_margin",
        "operating_margin",
        "fcf_margin",
        "fcf_to_net_income",
        "net_cash_to_assets",
        "sbc_pct_revenue",
        "r_and_d_pct_revenue",
        "share_count_yoy_growth",
        "inventory_days",
        "ev_gross_profit",
        "ev_operating_income",
        "fcf_yield",
        "ret_3m",
        "ret_12m_ex_1m",
        "rel_strength_soxx_3m",
        "realized_vol_60d",
        "max_drawdown_12m",
        "distance_from_52w_high",
        "avg_dollar_volume_60d",
        "low_liquidity_flag",
        "insider_net_value_90d",
        "insider_cluster_buyers_90d",
        "institutional_ownership_delta_pct",
        "latest_short_interest_pct_float",
        "short_interest_change_3m",
        "latest_days_to_cover",
        "latest_borrow_fee_rate",
        "quality_score",
        "growth_score",
        "valuation_score",
        "market_behavior_score",
        "positioning_score",
        "risk_control_score",
        "sector_cycle_score",
        "equipment_cycle_score",
        "sector_inventory_cycle_score",
        "big_tech_capex_score",
        "memory_ai_proxy_score",
        "innovation_score",
        "geo_customer_risk_score",
        "sector_overlay_score",
        "quality_component_quality",
        "growth_component_quality",
        "valuation_component_quality",
        "market_component_quality",
        "positioning_component_quality",
        "risk_component_quality",
        "sector_overlay_quality",
        "sector_overlay_status",
        "core_available_component_count",
        "core_missing_component_count",
        "core_data_quality_confidence",
        "full_data_quality_confidence",
        "market_quality",
        "financial_quality",
        "positioning_quality",
        "rank_ready_flag",
        "calibration_eligible_flag",
        "feature_status",
        "review_reason",
    ]
    payload = dict(row)
    payload["source_id"] = source_id
    payload["model_family"] = model_family
    payload["scoring_contract_version"] = contract_version
    values = [payload.get(field) for field in fields] + [now, now]
    update_clause = ",\n                ".join(f"{field} = excluded.{field}" for field in fields[4:])
    conn.execute(
        f"""
        INSERT INTO feature_scoring_input(
            {", ".join(fields)}, created_at, updated_at
        )
        VALUES ({", ".join("?" for _ in values)})
        ON CONFLICT(ticker, asof_date, source_id, model_family) DO UPDATE SET
            {update_clause},
            updated_at = excluded.updated_at
        """,
        values,
    )


def upsert_component_rows(
    conn: Any,
    rows: list[dict[str, Any]],
    *,
    source_id: str,
    model_family: str,
    component_defs: list[dict[str, Any]],
    preserve_loaded_overlays: bool = True,
) -> None:
    now = utc_now()
    component_names = [component["component_name"] for component in component_defs]
    component_groups = {component["component_name"]: component["component_group"] for component in component_defs}
    universe_percentiles: dict[str, dict[str, float]] = {}
    cohort_percentiles: dict[str, dict[str, float]] = {}
    for component_name in component_names:
        score_key = f"_component_{component_name}_score"
        for row in rows:
            row[score_key] = row["_component_meta"][component_name]["component_score"]
        universe_percentiles[component_name] = percentile_scores(rows, score_key, higher_is_better=True)
        cohort_percentiles[component_name] = cohort_percentile_scores(rows, score_key, higher_is_better=True)
    loaded_overlay_keys: set[tuple[str, str]] = set()
    overlay_names = [name for name, group in component_groups.items() if group == "sector_overlay"]
    if preserve_loaded_overlays and overlay_names and rows:
        build_asof = str(rows[0]["asof_date"])
        try:
            existing = conn.execute(
                f"""
                SELECT ticker, component_name
                FROM feature_scoring_component
                WHERE source_id = ? AND model_family = ? AND asof_date = ?
                  AND component_name IN ({qmarks(overlay_names)})
                  AND component_status <> 'not_loaded'
                """,
                (source_id, model_family, build_asof, *overlay_names),
            ).fetchall()
            loaded_overlay_keys = {(normalize_ticker(row["ticker"]), str(row["component_name"])) for row in existing}
        except Exception:  # noqa: BLE001 - table may not exist on first run
            loaded_overlay_keys = set()
    for row in rows:
        for component_name in component_names:
            if (str(row["ticker"]), component_name) in loaded_overlay_keys:
                continue  # Stage 6B already populated this overlay row; do not reset it
            meta = row["_component_meta"][component_name]
            conn.execute(
                """
                INSERT INTO feature_scoring_component(
                    ticker, asof_date, source_id, model_family, component_name,
                    component_group, calibration_cohort_id, component_score,
                    universe_percentile, cohort_percentile, component_quality,
                    component_status, available_subfeature_count,
                    missing_subfeature_count, default_applied, review_reason,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, asof_date, source_id, model_family, component_name) DO UPDATE SET
                    component_group = excluded.component_group,
                    calibration_cohort_id = excluded.calibration_cohort_id,
                    component_score = excluded.component_score,
                    universe_percentile = excluded.universe_percentile,
                    cohort_percentile = excluded.cohort_percentile,
                    component_quality = excluded.component_quality,
                    component_status = excluded.component_status,
                    available_subfeature_count = excluded.available_subfeature_count,
                    missing_subfeature_count = excluded.missing_subfeature_count,
                    default_applied = excluded.default_applied,
                    review_reason = excluded.review_reason,
                    updated_at = excluded.updated_at
                """,
                (
                    row["ticker"],
                    row["asof_date"],
                    source_id,
                    model_family,
                    component_name,
                    component_groups[component_name],
                    row.get("calibration_cohort_id"),
                    meta["component_score"],
                    universe_percentiles[component_name].get(str(row["ticker"])),
                    cohort_percentiles[component_name].get(str(row["ticker"])),
                    meta["component_quality"],
                    meta["component_status"],
                    meta["available_subfeature_count"],
                    meta["missing_subfeature_count"],
                    meta["default_applied"],
                    meta["review_reason"],
                    now,
                    now,
                ),
            )


def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def load_registry_into_db(conn: Any, config: dict[str, Any], base_dir: Path) -> None:
    registry_path = resolve_path(cfg_get(config, "source_registry.path"), base_dir=base_dir)
    sources = load_source_registry(registry_path)
    upsert_source_registry(conn, sources)


def run_scoring_feature_build(settings: ScoringFeatureSettings) -> None:
    configure_utc_logging()
    args = parse_build_args(settings)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    source_id = str(cfg_get(config, f"{settings.config_key}.source_id", settings.default_source_id) or settings.default_source_id)
    contract_version = str(cfg_get(config, f"{settings.config_key}.contract_version", "stage6a_v1") or "stage6a_v1")
    model_family = str(cfg_get(config, "technology_universe.initial_subsector", settings.default_model_family) or settings.default_model_family)
    output_csv = args.output_csv.expanduser().resolve() if args.output_csv else resolve_path(cfg_get(config, f"{settings.config_key}.output_csv"), base_dir=base_dir)
    neutral_score = float(cfg_get(config, f"{settings.config_key}.neutral_score", 50.0))
    overlay_default_quality = float(cfg_get(config, f"{settings.config_key}.sector_overlay_default_quality", 0.0))
    market_source = str(cfg_get(config, f"{settings.config_key}.input_sources.market", cfg_get(config, "market_feature_build.source_id", "yahoo_finance_adjusted")))
    financial_source = str(cfg_get(config, f"{settings.config_key}.input_sources.financial", cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts")))
    positioning_source = str(cfg_get(config, f"{settings.config_key}.input_sources.positioning", cfg_get(config, "positioning_import.source_id", "technology_positioning_composite")))
    overlay_names = list(cfg_get(config, f"{settings.config_key}.sector_overlay_components", DEFAULT_OVERLAY_COMPONENTS) or DEFAULT_OVERLAY_COMPONENTS)
    ticker_filter = {ticker for ticker in (normalize_ticker(x) for x in args.tickers.split(",")) if ticker}
    effective_asof = parse_date(args.asof) or date.today()
    component_defs = CORE_COMPONENT_DEFS + overlay_component_defs(overlay_names)

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        load_registry_into_db(conn, config, base_dir)
        run_id = start_run(conn, run_type=settings.run_type, input_path=config_path)
        try:
            universe = load_universe(conn, model_family, ticker_filter)
            if not universe:
                raise ValueError(f"No active universe rows found for model_family={model_family}.")
            with conn:
                conn.execute("DELETE FROM data_quality_issues WHERE stage = ?", (settings.run_type,))
                upsert_component_defs(
                    conn,
                    model_family=model_family,
                    component_defs=component_defs,
                    neutral_score=neutral_score,
                    overlay_default_quality=overlay_default_quality,
                )
                rows = build_raw_rows(
                    conn,
                    universe,
                    asof=effective_asof,
                    model_family=model_family,
                    market_source=market_source,
                    financial_source=financial_source,
                    positioning_source=positioning_source,
                )
                apply_subfeature_scores(rows)
                apply_component_scores(rows, neutral_score=neutral_score)
                preserved_overlays = load_preserved_overlays(
                    conn,
                    source_id=source_id,
                    model_family=model_family,
                    asof=effective_asof,
                )
                if preserved_overlays:
                    LOGGER.info("Preserving Stage 6B overlay state for %d tickers at asof=%s", len(preserved_overlays), effective_asof)
                finalize_rows(
                    rows,
                    config=config,
                    config_key=settings.config_key,
                    neutral_score=neutral_score,
                    overlay_default_quality=overlay_default_quality,
                    preserved_overlays=preserved_overlays,
                )
                for row in rows:
                    upsert_scoring_input(
                        conn,
                        row,
                        source_id=source_id,
                        model_family=model_family,
                        contract_version=contract_version,
                    )
                    if str(row.get("feature_status")) != "complete":
                        add_issue(
                            conn,
                            row,
                            source_id=source_id,
                            stage=settings.run_type,
                            detail=str(row.get("review_reason") or "review"),
                        )
                upsert_component_rows(
                    conn,
                    rows,
                    source_id=source_id,
                    model_family=model_family,
                    component_defs=component_defs,
                )
            write_report(output_csv, rows)
            review_count = sum(1 for row in rows if str(row.get("feature_status")) != "complete")
            finish_run(
                conn,
                run_id=run_id,
                status="success",
                row_count=len(rows),
                message=f"asof={effective_asof.isoformat()} rows={len(rows)} review={review_count} output={output_csv}",
            )
            LOGGER.info("Wrote scoring feature contract report: %s", output_csv)
            LOGGER.info("Built scoring feature contract: asof=%s rows=%d review=%d", effective_asof, len(rows), review_count)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


def scalar(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


def value(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row is not None else None


def validate_scoring_feature_contract(settings: ScoringFeatureSettings) -> int:
    configure_utc_logging()
    args = parse_validate_args(settings)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    source_id = str(cfg_get(config, f"{settings.config_key}.source_id", settings.default_source_id) or settings.default_source_id)
    model_family = str(cfg_get(config, "technology_universe.initial_subsector", settings.default_model_family) or settings.default_model_family)
    neutral_score = float(cfg_get(config, f"{settings.config_key}.neutral_score", 50.0))
    overlay_default_quality = float(cfg_get(config, f"{settings.config_key}.sector_overlay_default_quality", 0.0))
    overlay_names = list(cfg_get(config, f"{settings.config_key}.sector_overlay_components", DEFAULT_OVERLAY_COMPONENTS) or DEFAULT_OVERLAY_COMPONENTS)
    expected_components = [component["component_name"] for component in CORE_COMPONENT_DEFS] + overlay_names
    rank_ready_exempt = cfg_ticker_set(cfg_get(config, f"{settings.config_key}.rank_ready_exempt_tickers", []))

    errors: list[str] = []
    warnings: list[str] = []
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        load_registry_into_db(conn, config, base_dir)
        source_status = value(conn, "SELECT status FROM source_registry WHERE source_id = ?", (source_id,))
        if source_status != "active":
            errors.append(f"Source {source_id} is not active in source_registry: {source_status!r}")
        universe_rows = conn.execute(
            """
            SELECT c.ticker
            FROM dim_company c
            JOIN dim_technology_taxonomy t
              ON t.ticker = c.ticker
             AND t.model_family = ?
            WHERE c.is_active = 1
            ORDER BY c.ticker
            """,
            (model_family,),
        ).fetchall()
        universe = [normalize_ticker(row["ticker"]) for row in universe_rows if normalize_ticker(row["ticker"])]
        if not universe:
            errors.append(f"No active universe rows found for model_family={model_family}.")
            universe = ["__none__"]
        asof = parse_date(args.asof)
        if asof is None:
            asof_text = value(
                conn,
                "SELECT MAX(asof_date) FROM feature_scoring_input WHERE source_id = ? AND model_family = ?",
                (source_id, model_family),
            )
            asof = parse_date(asof_text)
        if asof is None:
            errors.append(f"No feature_scoring_input rows found for source_id={source_id} model_family={model_family}.")
            asof = date.today()
        ph = qmarks(universe)
        feature_rows = conn.execute(
            f"""
            SELECT *
            FROM feature_scoring_input
            WHERE source_id = ?
              AND model_family = ?
              AND asof_date = ?
              AND ticker IN ({ph})
            ORDER BY ticker
            """,
            (source_id, model_family, asof.isoformat(), *universe),
        ).fetchall()
        feature_tickers = {normalize_ticker(row["ticker"]) for row in feature_rows}
        missing_features = [ticker for ticker in universe if ticker not in feature_tickers]
        if missing_features:
            errors.append(f"Missing scoring input rows: {missing_features}")
        if len(feature_rows) != len(universe):
            errors.append(f"Scoring input row count mismatch: expected={len(universe)} actual={len(feature_rows)}")

        component_count = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM feature_scoring_component
            WHERE source_id = ?
              AND model_family = ?
              AND asof_date = ?
            """,
            (source_id, model_family, asof.isoformat()),
        )
        expected_component_count = len(universe) * len(expected_components)
        if component_count != expected_component_count:
            errors.append(f"Scoring component row count mismatch: expected={expected_component_count} actual={component_count}")
        component_defs = scalar(
            conn,
            f"""
            SELECT COUNT(*)
            FROM dim_scoring_component
            WHERE model_family = ?
              AND component_name IN ({qmarks(expected_components)})
            """,
            (model_family, *expected_components),
        )
        if component_defs != len(expected_components):
            errors.append(f"Scoring component definition count mismatch: expected={len(expected_components)} actual={component_defs}")

        not_ready = [
            normalize_ticker(row["ticker"])
            for row in feature_rows
            if int(row["rank_ready_flag"] or 0) != 1 and normalize_ticker(row["ticker"]) not in rank_ready_exempt
        ]
        if not_ready:
            errors.append(f"Non-exempt tickers are not rank ready: {not_ready}")
        active_exemptions = sorted(
            normalize_ticker(row["ticker"])
            for row in feature_rows
            if int(row["rank_ready_flag"] or 0) != 1 and normalize_ticker(row["ticker"]) in rank_ready_exempt
        )
        stale_exemptions = sorted(
            ticker
            for ticker in rank_ready_exempt
            if ticker in feature_tickers
            and ticker not in active_exemptions
        )
        overlay_bad = conn.execute(
            f"""
            SELECT ticker, component_name, component_score, component_quality, component_status
            FROM feature_scoring_component
            WHERE source_id = ?
              AND model_family = ?
              AND asof_date = ?
              AND component_name IN ({qmarks(overlay_names)})
              AND (
                  (
                      component_status = 'not_loaded'
                      AND (
                          ABS(component_score - ?) > 0.0001
                          OR ABS(component_quality - ?) > 0.0001
                      )
                  )
                  OR (
                      component_status <> 'not_loaded'
                      AND component_quality <= 0
                  )
              )
            ORDER BY ticker, component_name
            """,
            (source_id, model_family, asof.isoformat(), *overlay_names, neutral_score, overlay_default_quality),
        ).fetchall()
        if overlay_bad:
            errors.append(f"Sector overlay component state is invalid: {[dict(row) for row in overlay_bad[:10]]}")

        # Universe-level component health gates: a core component that is dead
        # (quality=0) for a large share of the universe, or that has no
        # cross-sectional variance, indicates an upstream pipeline failure even
        # when per-ticker rank-ready gates pass.
        max_dead_pct = float(cfg_get(config, f"{settings.config_key}.max_dead_core_component_pct", 0.20))
        core_names = [component["component_name"] for component in CORE_COMPONENT_DEFS]
        for component_name in core_names:
            comp_rows = conn.execute(
                """
                SELECT component_score, component_quality
                FROM feature_scoring_component
                WHERE source_id = ? AND model_family = ? AND asof_date = ? AND component_name = ?
                """,
                (source_id, model_family, asof.isoformat(), component_name),
            ).fetchall()
            if not comp_rows:
                continue
            dead = sum(1 for row in comp_rows if float(row["component_quality"] or 0.0) <= 0)
            dead_pct = dead / len(comp_rows)
            if dead_pct > max_dead_pct:
                errors.append(
                    f"Core component '{component_name}' has zero quality for {dead}/{len(comp_rows)} tickers "
                    f"({dead_pct:.0%} > {max_dead_pct:.0%}); upstream feature layer is likely broken."
                )
            elif dead:
                warnings.append(f"Core component '{component_name}' has zero quality for {dead}/{len(comp_rows)} tickers.")
            live_scores = [float(row["component_score"]) for row in comp_rows if float(row["component_quality"] or 0.0) > 0]
            if len(live_scores) >= 10:
                mean = sum(live_scores) / len(live_scores)
                variance = sum((score - mean) ** 2 for score in live_scores) / len(live_scores)
                if variance < 1e-6:
                    errors.append(
                        f"Core component '{component_name}' has degenerate cross-sectional variance "
                        f"({len(live_scores)} live scores all equal to {mean:.2f})."
                    )

        review_count = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM feature_scoring_input
            WHERE source_id = ?
              AND model_family = ?
              AND asof_date = ?
              AND feature_status <> 'complete'
            """,
            (source_id, model_family, asof.isoformat()),
        )
        issue_count = scalar(
            conn,
            "SELECT COUNT(*) FROM data_quality_issues WHERE stage = ? AND issue_type = 'scoring_feature_contract_review'",
            (settings.run_type,),
        )
        if issue_count != review_count:
            errors.append(f"Scoring review issue mismatch: review_rows={review_count} issues={issue_count}")

        warnings.append(f"Universe tickers={len(universe)}")
        warnings.append(f"Scoring asof={asof.isoformat()} rows={len(feature_rows)} review={review_count}")
        warnings.append(f"Component rows={component_count} component_defs={component_defs}")
        warnings.append(f"Rank-ready exemptions active={active_exemptions}")
        if stale_exemptions:
            warnings.append(f"Rank-ready exemptions can be removed={stale_exemptions}")

    for message in warnings:
        LOGGER.info(message)
    if errors:
        for message in errors:
            LOGGER.error(message)
        return 1
    LOGGER.info("Scoring feature contract validation passed for model_family=%s", model_family)
    return 0

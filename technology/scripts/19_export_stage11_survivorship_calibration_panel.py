#!/usr/bin/env python3
"""Export survivorship-correct Stage 11 calibration panels.

This is intentionally separate from dashboard snapshots. Dashboards replay the
current investable universe for a historical date; this exporter starts from
point-in-time universe membership, includes delisted/historical members, and
recomputes scores in memory over that PIT cross-section without writing back to
the production technology database.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.calibrated_scoring import (  # noqa: E402
    component_weight_specs,
    compute_model_outputs,
    configured_overlay_names as calibrated_overlay_names,
    recalibrate_components,
    subfeature_weight_specs,
)
from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.oos_provenance import build_oos_provenance  # noqa: E402
from technology.core.portfolio_candidate_fields import add_portfolio_candidate_fields  # noqa: E402
from technology.core.scoring_features import (  # noqa: E402
    apply_component_scores,
    apply_subfeature_scores,
    build_raw_rows,
    cfg_ticker_set,
    finalize_rows,
    load_preserved_overlays,
    safe_float,
)
from technology.core.text_norm import normalize_ticker  # noqa: E402
from technology.semiconductors.calibrated_scoring import SETTINGS as SEMICONDUCTOR_CALIBRATED_SETTINGS  # noqa: E402
from technology.software_infrastructure.calibrated_scoring import (  # noqa: E402
    SETTINGS as SOFTWARE_INFRASTRUCTURE_CALIBRATED_SETTINGS,
)
from technology.technology_hardware.calibrated_scoring import SETTINGS as TECHNOLOGY_HARDWARE_CALIBRATED_SETTINGS  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_OUTPUT_SUBDIR = "stage11_calibration"
DEFAULT_CALENDAR_TICKER = "QQQ"
DEFAULT_HORIZONS = (21, 63, 126, 252)
MARKET_SOURCE_ID = "yahoo_finance_adjusted"
PANEL_SOURCE = "survivorship_corrected_pit_membership_score_recompute"


@dataclass(frozen=True)
class FamilySpec:
    model_family: str
    aliases: tuple[str, ...]
    scoring_config_key: str
    research_config_key: str
    diagnostics_config_key: str
    output_prefix: str
    calibrated_settings: Any


FAMILY_SPECS: dict[str, FamilySpec] = {}


def register_family(spec: FamilySpec) -> None:
    for alias in (spec.model_family, *spec.aliases):
        FAMILY_SPECS[alias.lower()] = spec


register_family(
    FamilySpec(
        model_family="semiconductors",
        aliases=("semi", "semis", "semiconductor"),
        scoring_config_key="semiconductor_scoring_features",
        research_config_key="semiconductor_research",
        diagnostics_config_key="semiconductor_signal_diagnostics",
        output_prefix="semiconductor",
        calibrated_settings=SEMICONDUCTOR_CALIBRATED_SETTINGS,
    )
)
register_family(
    FamilySpec(
        model_family="technology_hardware",
        aliases=("hardware", "tech_hardware"),
        scoring_config_key="technology_hardware_scoring_features",
        research_config_key="technology_hardware_research",
        diagnostics_config_key="technology_hardware_signal_diagnostics",
        output_prefix="technology_hardware",
        calibrated_settings=TECHNOLOGY_HARDWARE_CALIBRATED_SETTINGS,
    )
)
register_family(
    FamilySpec(
        model_family="software_infrastructure",
        aliases=("software", "software_infra", "software-infrastructure"),
        scoring_config_key="software_infrastructure_scoring_features",
        research_config_key="software_infrastructure_research",
        diagnostics_config_key="software_infrastructure_signal_diagnostics",
        output_prefix="software_infrastructure",
        calibrated_settings=SOFTWARE_INFRASTRUCTURE_CALIBRATED_SETTINGS,
    )
)


IDENTITY_FIELDS = [
    "model_family",
    "ticker",
    "asof_date",
    "company_id",
    "company_name",
    "cik",
    "sector",
    "industry",
    "subsector",
    "country",
    "currency",
    "universe_status",
    "is_active",
    "calibration_cohort_id",
    "calibration_cohort",
    "subindustry_role",
    "calibration_use",
]

MEMBERSHIP_FIELDS = [
    "survivorship_corrected_panel_flag",
    "stage11_calibration_panel_source",
    "membership_source_id",
    "membership_basis",
    "membership_status",
    "membership_start_date",
    "membership_end_date",
    "membership_confidence",
    "membership_reason",
    "is_current_member",
    "point_in_time_flag",
    "historical_universe_source",
    "terminal_date",
]

MODEL_FIELDS = [
    "score_model_version",
    "model_version",
    "scoring_contract_version",
    "scoring_source_id",
    "model_output_source_id",
    "baseline_source_id",
    "score_recomputed_pit_flag",
    "score_source_panel_basis",
    "final_rank",
    "final_percentile",
    "final_score",
    "core_score",
    "sector_overlay_score",
    "data_quality_confidence",
    "rank_ready_flag",
    "calibration_eligible_flag",
    "model_status",
    "review_reason",
]

FEATURE_FIELDS = [
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
    "rel_strength_bench_3m",
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
    "sector_overlay_quality",
    "sector_overlay_status",
    "quality_component_quality",
    "growth_component_quality",
    "valuation_component_quality",
    "market_component_quality",
    "positioning_component_quality",
    "risk_component_quality",
    "core_available_component_count",
    "core_missing_component_count",
    "core_data_quality_confidence",
    "full_data_quality_confidence",
    "market_quality",
    "financial_quality",
    "positioning_quality",
    "feature_status",
]

PRICE_FIELDS = [
    "price_available_on_asof_flag",
    "price_source_id",
    "price_asof_date",
    "price_stale_days",
    "price_close",
    "price_adj_close",
    "price_start_date",
    "price_end_date",
    "latest_price_date",
    "historical_price_ticker",
    "price_data_asof_date",
]

STAGE11_FIELDS = [
    "stage11_forward_return_join_ready_any_flag",
    "stage11_forward_return_join_ready_all_flag",
    "stage11_exclusion_reason",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export survivorship-correct Stage 11 technology calibration panels.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--date", action="append", default=[], help="Repeatable explicit as-of date.")
    parser.add_argument("--dates", default="", help="Comma-separated explicit as-of dates.")
    parser.add_argument("--frequency", choices=("daily", "panel21"), default="panel21")
    parser.add_argument(
        "--families",
        default="semiconductors,technology_hardware,software_infrastructure",
        help="Comma-separated family list.",
    )
    parser.add_argument("--family", action="append", default=[], help="Repeatable family selector.")
    parser.add_argument("--calendar-ticker", default=DEFAULT_CALENDAR_TICKER)
    parser.add_argument("--horizons", default=",".join(str(item) for item in DEFAULT_HORIZONS))
    parser.add_argument("--max-price-staleness-days", type=int, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--combined-only", action="store_true", help="Skip per-date snapshot CSV files.")
    return parser.parse_args()


def split_values(raw: str | list[str] | tuple[str, ...]) -> list[str]:
    chunks = list(raw) if isinstance(raw, (list, tuple)) else [str(raw or "")]
    return [item.strip() for chunk in chunks for item in str(chunk or "").split(",") if item.strip()]


def parse_iso_date(raw: Any) -> date:
    text = str(raw or "").strip()[:10]
    return datetime.strptime(text, "%Y-%m-%d").date()


def date_text(raw: Any) -> str:
    return parse_iso_date(raw).isoformat()


def sqlite_readonly(db_path: Path, timeout_sec: float) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout_sec)
    conn.row_factory = sqlite3.Row
    return conn


def resolve_families(raw_families: str, raw_family: list[str]) -> list[FamilySpec]:
    values = split_values(raw_family) if raw_family else split_values(raw_families)
    specs: list[FamilySpec] = []
    seen: set[str] = set()
    for value in values:
        spec = FAMILY_SPECS.get(value.lower())
        if spec is None:
            raise ValueError(f"Unknown family '{value}'. Valid aliases: {', '.join(sorted(FAMILY_SPECS))}")
        if spec.model_family not in seen:
            specs.append(spec)
            seen.add(spec.model_family)
    return specs


def resolve_horizons(raw: str) -> list[int]:
    horizons = [int(item) for item in split_values(raw)]
    if not horizons:
        raise ValueError("At least one forward-return horizon is required.")
    if any(item <= 0 for item in horizons):
        raise ValueError(f"Forward-return horizons must be positive trading-day counts: {horizons}")
    return sorted(set(horizons))


def resolve_dates(
    conn: sqlite3.Connection,
    *,
    explicit_dates: list[str],
    start_date: str,
    end_date: str,
    frequency: str,
    calendar_ticker: str,
) -> list[str]:
    if explicit_dates:
        return sorted({date_text(item) for item in explicit_dates})
    if not start_date:
        raise ValueError("--start-date is required when --date/--dates is not provided.")
    start = date_text(start_date)
    if end_date:
        end = date_text(end_date)
    else:
        row = conn.execute(
            """
            SELECT MAX(bar_date) AS bar_date
            FROM fact_price_ohlcv
            WHERE ticker = ? AND source_id = ?
            """,
            (normalize_ticker(calendar_ticker), MARKET_SOURCE_ID),
        ).fetchone()
        end = str(row["bar_date"] or "")
        if not end:
            raise ValueError(f"No calendar price rows found for {calendar_ticker}/{MARKET_SOURCE_ID}.")
    dates = [
        str(row["bar_date"])
        for row in conn.execute(
            """
            SELECT DISTINCT bar_date
            FROM fact_price_ohlcv
            WHERE ticker = ? AND source_id = ? AND bar_date BETWEEN ? AND ?
            ORDER BY bar_date
            """,
            (normalize_ticker(calendar_ticker), MARKET_SOURCE_ID, start, end),
        ).fetchall()
    ]
    if frequency == "panel21":
        dates = dates[::21]
    return dates


def source_ids_for_family(config: dict[str, Any], spec: FamilySpec) -> tuple[str, str, str, str]:
    scoring_source = str(
        cfg_get(config, f"{spec.scoring_config_key}.source_id", spec.calibrated_settings.default_baseline_source_id)
        or spec.calibrated_settings.default_baseline_source_id
    )
    model_source = str(
        cfg_get(config, f"{spec.calibrated_settings.config_key}.source_id", spec.calibrated_settings.default_source_id)
        or spec.calibrated_settings.default_source_id
    )
    model_version = str(
        cfg_get(config, f"{spec.calibrated_settings.config_key}.model_version", spec.calibrated_settings.default_model_version)
        or spec.calibrated_settings.default_model_version
    )
    contract_version = str(
        cfg_get(config, f"{spec.scoring_config_key}.contract_version", "")
        or cfg_get(config, f"{spec.calibrated_settings.config_key}.baseline_source_id", "")
        or scoring_source
    )
    return scoring_source, model_source, model_version, contract_version


def price_source_ids(config: dict[str, Any], spec: FamilySpec) -> list[str]:
    raw = cfg_get(config, f"{spec.research_config_key}.price_source_ids", None)
    values = [str(item) for item in raw] if isinstance(raw, list) else []
    if not values:
        values = [MARKET_SOURCE_ID, "norgate_us_equities_total_return"]
    return list(dict.fromkeys(values))


def benchmark_ticker(config: dict[str, Any], spec: FamilySpec) -> str:
    default = "SMH" if spec.model_family == "semiconductors" else "QQQ"
    return normalize_ticker(cfg_get(config, f"{spec.diagnostics_config_key}.benchmark_ticker", default) or default)


def membership_score(row: sqlite3.Row) -> tuple[int, int, float, str]:
    start = str(row["start_date"] or "")
    non_sentinel = 0 if start.startswith("1900-01-01") else 1
    current = int(row["is_current_member"] or 0)
    confidence = float(row["confidence"] or 0.0)
    return (non_sentinel, current, confidence, start)


def load_pit_members(conn: sqlite3.Connection, spec: FamilySpec, asof: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            m.company_id,
            m.ticker,
            m.model_family,
            m.membership_source_id,
            m.membership_basis,
            m.start_date,
            m.end_date,
            m.membership_status,
            m.is_current_member,
            m.point_in_time_flag,
            m.confidence,
            m.reason,
            c.cik,
            c.company_name,
            c.sector AS company_sector,
            c.industry,
            c.subsector AS company_subsector,
            c.country,
            c.currency,
            c.universe_status,
            c.is_active,
            t.sector AS taxonomy_sector,
            t.subsector AS taxonomy_subsector,
            t.calibration_cohort,
            t.calibration_cohort_id,
            t.subindustry_role,
            t.calibration_use
        FROM dim_universe_membership m
        LEFT JOIN dim_company c
          ON c.company_id = m.company_id
        LEFT JOIN dim_technology_taxonomy t
          ON t.company_id = m.company_id
         AND t.model_family = m.model_family
        WHERE m.model_family = ?
          AND m.point_in_time_flag = 1
          AND m.start_date <= ?
          AND (m.end_date IS NULL OR m.end_date = '' OR m.end_date >= ?)
        ORDER BY m.ticker, m.start_date
        """,
        (spec.model_family, asof, asof),
    ).fetchall()
    by_ticker: dict[str, sqlite3.Row] = {}
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        if not ticker:
            continue
        current = by_ticker.get(ticker)
        if current is None or membership_score(row) > membership_score(current):
            by_ticker[ticker] = row

    members: list[dict[str, Any]] = []
    for ticker in sorted(by_ticker):
        row = by_ticker[ticker]
        members.append(
            {
                "company_id": row["company_id"],
                "ticker": ticker,
                "model_family": spec.model_family,
                "company_name": row["company_name"] or "",
                "cik": row["cik"] or "",
                "sector": row["taxonomy_sector"] or row["company_sector"] or "",
                "industry": row["industry"] or "",
                "subsector": row["taxonomy_subsector"] or row["company_subsector"] or "",
                "country": row["country"] or "",
                "currency": row["currency"] or "",
                "universe_status": row["universe_status"] or row["membership_status"] or "",
                "is_active": int(row["is_active"] or 0),
                "calibration_cohort_id": row["calibration_cohort_id"] or "",
                "calibration_cohort": row["calibration_cohort"] or "",
                "subindustry_role": row["subindustry_role"] or "",
                "calibration_use": row["calibration_use"] or "",
                "membership_source_id": row["membership_source_id"] or "",
                "membership_basis": row["membership_basis"] or "",
                "membership_status": row["membership_status"] or "",
                "membership_start_date": row["start_date"] or "",
                "membership_end_date": row["end_date"] or "",
                "membership_confidence": row["confidence"] if row["confidence"] is not None else "",
                "membership_reason": row["reason"] or "",
                "is_current_member": int(row["is_current_member"] or 0),
                "point_in_time_flag": int(row["point_in_time_flag"] or 0),
                "historical_universe_source": row["membership_source_id"] or "",
                "terminal_date": row["end_date"] or "",
            }
        )
    return members


def source_date_after_asof(row: dict[str, Any], asof: str, field: str) -> bool:
    value = str(row.get(field) or "").strip()[:10]
    if not value:
        return False
    return value > asof


@dataclass
class PriceSeries:
    ticker: str
    dates: list[str]
    rows: dict[str, dict[str, Any]]

    def index_at_or_before(self, asof: str) -> int:
        return bisect.bisect_right(self.dates, asof) - 1

    def at_or_before(self, asof: str, max_staleness_days: int) -> dict[str, Any]:
        idx = self.index_at_or_before(asof)
        if idx < 0:
            return {
                "available": 0,
                "price_date": "",
                "stale_days": "",
                "source_id": "",
                "close": "",
                "adj_close": "",
            }
        price_date = self.dates[idx]
        stale_days = (parse_iso_date(asof) - parse_iso_date(price_date)).days
        row = self.rows[price_date]
        return {
            "available": int(stale_days <= max_staleness_days),
            "price_date": price_date,
            "stale_days": stale_days,
            "source_id": row["source_id"],
            "close": row["close"],
            "adj_close": row["adj_close"],
        }

    def forward_ready(self, asof: str, horizon: int, max_staleness_days: int) -> dict[str, Any]:
        idx = self.index_at_or_before(asof)
        if idx < 0:
            return {"ready": 0, "asof_price_date": "", "forward_price_date": "", "reason": "missing_asof_price"}
        asof_price_date = self.dates[idx]
        stale_days = (parse_iso_date(asof) - parse_iso_date(asof_price_date)).days
        if stale_days > max_staleness_days:
            return {
                "ready": 0,
                "asof_price_date": asof_price_date,
                "forward_price_date": "",
                "reason": f"stale_asof_price:{stale_days}d",
            }
        forward_idx = idx + horizon
        if forward_idx >= len(self.dates):
            return {
                "ready": 0,
                "asof_price_date": asof_price_date,
                "forward_price_date": "",
                "reason": f"missing_forward_price_{horizon}d",
            }
        return {
            "ready": 1,
            "asof_price_date": asof_price_date,
            "forward_price_date": self.dates[forward_idx],
            "reason": "ok",
        }

    def adj_close_at_index(self, idx: int) -> float | None:
        if idx < 0 or idx >= len(self.dates):
            return None
        return safe_float(self.rows[self.dates[idx]].get("adj_close"))

    def volume_at_index(self, idx: int) -> float | None:
        if idx < 0 or idx >= len(self.dates):
            return None
        return safe_float(self.rows[self.dates[idx]].get("volume"))

    def pct_return(self, idx: int, lookback: int, *, end_offset: int = 0) -> float | None:
        end_idx = idx - end_offset
        start_idx = end_idx - lookback
        end_price = self.adj_close_at_index(end_idx)
        start_price = self.adj_close_at_index(start_idx)
        if end_price is None or start_price is None or start_price <= 0:
            return None
        return end_price / start_price - 1.0

    def trailing_prices(self, idx: int, window: int) -> list[float]:
        start = max(0, idx - window + 1)
        values: list[float] = []
        for item_idx in range(start, idx + 1):
            value = self.adj_close_at_index(item_idx)
            if value is not None and value > 0:
                values.append(value)
        return values

    def trailing_dollar_volume(self, idx: int, window: int) -> float | None:
        start = max(0, idx - window + 1)
        values: list[float] = []
        for item_idx in range(start, idx + 1):
            price = self.adj_close_at_index(item_idx)
            volume = self.volume_at_index(item_idx)
            if price is not None and volume is not None and price > 0 and volume >= 0:
                values.append(price * volume)
        return sum(values) / len(values) if values else None

    @property
    def start_date(self) -> str:
        return self.dates[0] if self.dates else ""

    @property
    def end_date(self) -> str:
        return self.dates[-1] if self.dates else ""


def load_price_series(
    conn: sqlite3.Connection,
    *,
    tickers: set[str],
    source_ids: list[str],
    start_date: str,
) -> dict[str, PriceSeries]:
    if not tickers:
        return {}
    q_tickers = ",".join("?" for _ in tickers)
    q_sources = ",".join("?" for _ in source_ids)
    source_priority = {source_id: idx for idx, source_id in enumerate(source_ids)}
    by_ticker_date: dict[str, dict[str, dict[str, Any]]] = {}
    params = [*sorted(tickers), *source_ids, start_date]
    rows = conn.execute(
        f"""
        SELECT ticker, bar_date, source_id, close, adj_close, volume
        FROM fact_price_ohlcv
        WHERE ticker IN ({q_tickers})
          AND source_id IN ({q_sources})
          AND bar_date >= ?
          AND (adj_close IS NOT NULL OR close IS NOT NULL)
        ORDER BY ticker, bar_date, source_id
        """,
        params,
    ).fetchall()
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        bar_date = str(row["bar_date"])
        existing = by_ticker_date.setdefault(ticker, {}).get(bar_date)
        source_id = str(row["source_id"])
        if existing is None or source_priority.get(source_id, 999) < source_priority.get(str(existing["source_id"]), 999):
            adj_close = safe_float(row["adj_close"])
            close = safe_float(row["close"])
            by_ticker_date[ticker][bar_date] = {
                "source_id": source_id,
                "close": close if close is not None else adj_close,
                "adj_close": adj_close if adj_close is not None else close,
                "volume": safe_float(row["volume"]),
            }
    return {
        ticker: PriceSeries(ticker=ticker, dates=sorted(rows_by_date), rows=rows_by_date)
        for ticker, rows_by_date in by_ticker_date.items()
    }


def annotate_prices(
    row: dict[str, Any],
    *,
    series: PriceSeries | None,
    asof: str,
    horizons: list[int],
    max_staleness_days: int,
) -> None:
    if series is None:
        row.update(
            {
                "price_available_on_asof_flag": 0,
                "price_source_id": "",
                "price_asof_date": "",
                "price_stale_days": "",
                "price_close": "",
                "price_adj_close": "",
                "price_start_date": "",
                "price_end_date": "",
                "latest_price_date": "",
                "historical_price_ticker": row.get("ticker", ""),
                "price_data_asof_date": "",
            }
        )
        for horizon in horizons:
            row[f"forward_{horizon}d_join_ready_flag"] = 0
            row[f"forward_{horizon}d_price_date"] = ""
            row[f"forward_{horizon}d_join_reason"] = "missing_price_series"
        row["stage11_forward_return_join_ready_any_flag"] = 0
        row["stage11_forward_return_join_ready_all_flag"] = 0
        return

    price = series.at_or_before(asof, max_staleness_days)
    latest_price_date = price["price_date"] if price["available"] else ""
    row.update(
        {
            "price_available_on_asof_flag": int(price["available"]),
            "price_source_id": price["source_id"],
            "price_asof_date": price["price_date"],
            "price_stale_days": price["stale_days"],
            "price_close": price["close"],
            "price_adj_close": price["adj_close"],
            "price_start_date": series.start_date,
            "price_end_date": series.end_date,
            "latest_price_date": latest_price_date,
            "historical_price_ticker": row.get("ticker", ""),
            "price_data_asof_date": latest_price_date,
        }
    )
    ready_flags: list[int] = []
    for horizon in horizons:
        ready = series.forward_ready(asof, horizon, max_staleness_days)
        ready_flags.append(int(ready["ready"]))
        row[f"forward_{horizon}d_join_ready_flag"] = int(ready["ready"])
        row[f"forward_{horizon}d_price_date"] = ready["forward_price_date"]
        row[f"forward_{horizon}d_join_reason"] = ready["reason"]
    row["stage11_forward_return_join_ready_any_flag"] = int(any(ready_flags))
    row["stage11_forward_return_join_ready_all_flag"] = int(all(ready_flags)) if ready_flags else 0


def realized_volatility(prices: list[float]) -> float | None:
    if len(prices) < 20:
        return None
    returns: list[float] = []
    for prev, cur in zip(prices, prices[1:]):
        if prev > 0 and cur > 0:
            returns.append(math.log(cur / prev))
    if len(returns) < 20:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((item - mean) ** 2 for item in returns) / len(returns)
    return math.sqrt(variance) * math.sqrt(252.0)


def max_drawdown(prices: list[float]) -> float | None:
    if not prices:
        return None
    peak = prices[0]
    worst = 0.0
    for price in prices:
        peak = max(peak, price)
        if peak > 0:
            worst = min(worst, price / peak - 1.0)
    return worst


def apply_market_price_fallback(
    row: dict[str, Any],
    *,
    series: PriceSeries | None,
    benchmark: PriceSeries | None,
    asof: str,
    max_staleness_days: int,
    min_trading_days: int,
    liquidity_threshold: float,
) -> None:
    if series is None:
        return
    idx = series.index_at_or_before(asof)
    if idx < 0:
        return
    price = series.at_or_before(asof, max_staleness_days)
    if int(price["available"]) != 1:
        return
    adj_close = safe_float(price["adj_close"])
    if adj_close is None or adj_close <= 0:
        return

    trailing_60 = series.trailing_prices(idx, 60)
    trailing_252 = series.trailing_prices(idx, 252)
    ret_3m = series.pct_return(idx, 63)
    ret_12m_ex_1m = series.pct_return(idx, 231, end_offset=21)
    bench_ret_3m: float | None = None
    if benchmark is not None:
        bench_idx = benchmark.index_at_or_before(asof)
        if bench_idx >= 0:
            bench_ret_3m = benchmark.pct_return(bench_idx, 63)
    rel_strength = ret_3m - bench_ret_3m if ret_3m is not None and bench_ret_3m is not None else None
    avg_dollar_volume_60d = series.trailing_dollar_volume(idx, 60)
    high_52w = max(trailing_252) if trailing_252 else None

    row["market_feature_asof_date"] = str(price["price_date"])
    row["market_quality"] = "complete" if idx + 1 >= min_trading_days else "review"
    row["latest_price"] = adj_close
    row["ret_3m"] = ret_3m
    row["ret_12m_ex_1m"] = ret_12m_ex_1m
    row["rel_strength_bench_3m"] = rel_strength
    row["rel_strength_soxx_3m"] = rel_strength
    row["realized_vol_60d"] = realized_volatility(trailing_60)
    row["max_drawdown_12m"] = max_drawdown(trailing_252)
    row["distance_from_52w_high"] = adj_close / high_52w - 1.0 if high_52w and high_52w > 0 else None
    row["avg_dollar_volume_60d"] = avg_dollar_volume_60d
    row["low_liquidity_flag"] = int(avg_dollar_volume_60d is not None and avg_dollar_volume_60d < liquidity_threshold)


def build_pit_scores(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    spec: FamilySpec,
    members: list[dict[str, Any]],
    asof: str,
    price_series: dict[str, PriceSeries],
    benchmark_series: PriceSeries | None,
    max_price_staleness_days: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    scoring_source, model_source, model_version, contract_version = source_ids_for_family(config, spec)
    input_sources = cfg_get(config, f"{spec.scoring_config_key}.input_sources", {}) or {}
    market_source = str(input_sources.get("market") or MARKET_SOURCE_ID)
    financial_source = str(input_sources.get("financial") or "sec_companyfacts")
    positioning_source = str(input_sources.get("positioning") or "technology_positioning_composite")
    relative_strength_field = str(
        cfg_get(config, f"{spec.scoring_config_key}.relative_strength_market_field", "rel_strength_soxx_3m")
        or "rel_strength_soxx_3m"
    )
    neutral_score = float(cfg_get(config, f"{spec.scoring_config_key}.neutral_score", 50.0))
    overlay_default_quality = float(cfg_get(config, f"{spec.scoring_config_key}.sector_overlay_default_quality", 0.0))
    min_trading_days = int(cfg_get(config, "market_data_policy.min_trading_days_for_full_features", 252))
    liquidity_threshold = float(cfg_get(config, "market_data_policy.min_avg_dollar_volume_60d_for_full_features", 5000000.0))
    preserved_overlays = load_preserved_overlays(
        conn,
        source_id=scoring_source,
        model_family=spec.model_family,
        asof=parse_iso_date(asof),
    )

    raw_rows = build_raw_rows(
        conn,
        members,
        asof=parse_iso_date(asof),
        model_family=spec.model_family,
        market_source=market_source,
        financial_source=financial_source,
        positioning_source=positioning_source,
        relative_strength_market_field=relative_strength_field,
    )
    for row in raw_rows:
        apply_market_price_fallback(
            row,
            series=price_series.get(normalize_ticker(row["ticker"])),
            benchmark=benchmark_series,
            asof=asof,
            max_staleness_days=max_price_staleness_days,
            min_trading_days=min_trading_days,
            liquidity_threshold=liquidity_threshold,
        )
    for row in raw_rows:
        row["source_id"] = scoring_source
        row["model_family"] = spec.model_family
        row["scoring_contract_version"] = contract_version
    apply_subfeature_scores(raw_rows)
    apply_component_scores(raw_rows, neutral_score=neutral_score)
    finalize_rows(
        raw_rows,
        config=config,
        config_key=spec.scoring_config_key,
        neutral_score=neutral_score,
        overlay_default_quality=overlay_default_quality,
        preserved_overlays=preserved_overlays,
    )

    component_weights = component_weight_specs(config, spec.calibrated_settings)
    subfeature_specs = subfeature_weight_specs(config, spec.calibrated_settings)
    overlay_names = calibrated_overlay_names(config, spec.calibrated_settings)
    rank_ready_exempt = cfg_ticker_set(cfg_get(config, f"{spec.calibrated_settings.config_key}.rank_ready_exempt_tickers", []))
    calibrated_neutral = float(cfg_get(config, f"{spec.calibrated_settings.config_key}.neutral_score", 50.0))
    overlay_weight = float(cfg_get(config, f"{spec.calibrated_settings.config_key}.overlay_weight", 0.0))
    min_core_confidence = float(cfg_get(config, f"{spec.calibrated_settings.config_key}.min_core_data_quality_confidence", 0.50))
    max_missing_weight = float(
        cfg_get(config, f"{spec.calibrated_settings.config_key}.max_missing_positive_component_weight", 0.35)
    )
    recalibrate_components(
        raw_rows,
        component_weights=component_weights,
        subfeature_specs=subfeature_specs,
        overlay_names=overlay_names,
        neutral_score=calibrated_neutral,
    )
    outputs = compute_model_outputs(
        raw_rows,
        source_id=model_source,
        baseline_source_id=scoring_source,
        model_family=spec.model_family,
        model_version=model_version,
        component_weights=component_weights,
        overlay_weight=overlay_weight,
        min_core_confidence=min_core_confidence,
        max_missing_positive_component_weight=max_missing_weight,
        rank_ready_exempt=rank_ready_exempt,
        neutral_score=calibrated_neutral,
    )
    return raw_rows, outputs


def merge_member_score_rows(
    *,
    config: dict[str, Any],
    spec: FamilySpec,
    asof: str,
    members: list[dict[str, Any]],
    raw_rows: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    price_series: dict[str, PriceSeries],
    horizons: list[int],
    max_price_staleness_days: int,
) -> list[dict[str, Any]]:
    scoring_source, model_source, model_version, contract_version = source_ids_for_family(config, spec)
    raw_by_ticker = {normalize_ticker(row["ticker"]): row for row in raw_rows}
    output_by_ticker = {normalize_ticker(row["ticker"]): row for row in outputs}
    provenance = build_oos_provenance(config, model_family=spec.model_family, asof=asof, historical_mode=True)

    rows: list[dict[str, Any]] = []
    for member in members:
        ticker = normalize_ticker(member["ticker"])
        raw = raw_by_ticker.get(ticker, {})
        output = output_by_ticker.get(ticker, {})
        row: dict[str, Any] = {
            **member,
            "asof_date": asof,
            "survivorship_corrected_panel_flag": 1,
            "stage11_calibration_panel_source": PANEL_SOURCE,
            "score_model_version": model_version,
            "model_version": model_version,
            "scoring_contract_version": raw.get("scoring_contract_version") or contract_version,
            "scoring_source_id": scoring_source,
            "model_output_source_id": model_source,
            "baseline_source_id": scoring_source,
            "score_recomputed_pit_flag": 1 if output else 0,
            "score_source_panel_basis": "pit_membership_recomputed_in_memory" if output else "missing_score_row",
            **provenance.row_fields,
        }
        for field in FEATURE_FIELDS:
            if field in raw:
                row[field] = raw.get(field)
        for field in MODEL_FIELDS:
            if field in output:
                row[field] = output.get(field)
        row["score_model_version"] = row.get("score_model_version") or model_version
        row["model_version"] = row.get("model_version") or model_version
        row["scoring_contract_version"] = row.get("scoring_contract_version") or contract_version
        row["scoring_source_id"] = scoring_source
        row["model_output_source_id"] = model_source
        row["baseline_source_id"] = scoring_source
        row["score_recomputed_pit_flag"] = 1 if output else 0
        row["score_source_panel_basis"] = "pit_membership_recomputed_in_memory" if output else "missing_score_row"

        annotate_prices(
            row,
            series=price_series.get(ticker),
            asof=asof,
            horizons=horizons,
            max_staleness_days=max_price_staleness_days,
        )

        exclusion_reasons: list[str] = []
        if not output:
            exclusion_reasons.append("missing_pit_recomputed_score")
        if not row.get("final_score") and safe_float(row.get("final_score")) is None:
            exclusion_reasons.append("missing_final_score")
        for source_date_field in ("market_feature_asof_date", "financial_feature_asof_date", "positioning_feature_asof_date"):
            if source_date_after_asof(row, asof, source_date_field):
                exclusion_reasons.append(f"source_date_after_asof:{source_date_field}")
        if int(row.get("price_available_on_asof_flag") or 0) != 1:
            if row.get("price_asof_date"):
                exclusion_reasons.append(f"stale_or_invalid_asof_price:{row.get('price_stale_days')}d")
            else:
                exclusion_reasons.append("missing_asof_price")
        row["stage11_exclusion_reason"] = ";".join(dict.fromkeys(exclusion_reasons))
        rows.append(row)

    rows = add_portfolio_candidate_fields(rows)
    for row in rows:
        reasons = [item for item in str(row.get("stage11_exclusion_reason") or "").split(";") if item]
        if int(row.get("stage11_calibration_input_eligible_flag") or 0) != 1:
            reasons.append(str(row.get("stage11_calibration_input_reason") or "not_stage11_calibration_input_eligible"))
        if reasons:
            reason = ";".join(dict.fromkeys(reasons))
            row["stage11_exclusion_reason"] = reason
            if int(row.get("stage11_calibration_input_eligible_flag") or 0) == 1:
                row["stage11_calibration_input_eligible_flag"] = 0
                row["stage11_calibration_input_reason"] = reason
                row["research_calibration_input_eligible_flag"] = 0
                row["research_calibration_status"] = "excluded"
                row["research_calibration_reason"] = reason
                row["calibration_sample_role"] = "excluded"
                row["calibration_status"] = "excluded"
                row["calibration_status_reason"] = reason
            row["portfolio_candidate_gate"] = 0
            row["portfolio_candidate_status"] = "excluded"
            row["portfolio_candidate_reason"] = reason
        else:
            row["stage11_exclusion_reason"] = ""
    return rows


def ordered_fieldnames(rows: list[dict[str, Any]], horizons: list[int]) -> list[str]:
    forward_fields: list[str] = []
    for horizon in horizons:
        forward_fields.extend(
            [
                f"forward_{horizon}d_join_ready_flag",
                f"forward_{horizon}d_price_date",
                f"forward_{horizon}d_join_reason",
            ]
        )
    preferred = [
        *IDENTITY_FIELDS,
        *MEMBERSHIP_FIELDS,
        *MODEL_FIELDS,
        *FEATURE_FIELDS,
        *PRICE_FIELDS,
        *forward_fields,
        *STAGE11_FIELDS,
    ]
    seen: set[str] = set()
    fieldnames: list[str] = []
    for field in preferred:
        if field not in seen:
            fieldnames.append(field)
            seen.add(field)
    for row in rows:
        for field in row:
            if field not in seen and not field.startswith("_"):
                fieldnames.append(field)
                seen.add(field)
    return fieldnames


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in sorted(
            rows,
            key=lambda item: (
                str(item.get("asof_date") or ""),
                item.get("final_rank") in {"", None},
                item.get("final_rank") or 10**9,
                str(item.get("ticker") or ""),
            ),
        ):
            writer.writerow(row)


def summarize_date(rows: list[dict[str, Any]], asof: str, horizons: list[int]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "asof_date": asof,
        "membership_rows": len(rows),
        "score_recomputed_rows": sum(1 for row in rows if int(row.get("score_recomputed_pit_flag") or 0) == 1),
        "rank_ready_rows": sum(1 for row in rows if int(row.get("rank_ready_flag") or 0) == 1),
        "stage11_calibration_input_eligible_rows": sum(
            1 for row in rows if int(row.get("stage11_calibration_input_eligible_flag") or 0) == 1
        ),
        "portfolio_candidate_rows": sum(1 for row in rows if int(row.get("portfolio_candidate_gate") or 0) == 1),
        "missing_asof_price_rows": sum(1 for row in rows if int(row.get("price_available_on_asof_flag") or 0) != 1),
        "survivorship_corrected_panel_flag": 1,
    }
    for horizon in horizons:
        field = f"forward_{horizon}d_join_ready_flag"
        summary[f"forward_{horizon}d_join_ready_rows"] = sum(1 for row in rows if int(row.get(field) or 0) == 1)
    return summary


def validate_panel_rows(rows: list[dict[str, Any]], *, asof: str, spec: FamilySpec) -> None:
    errors: list[str] = []
    if not rows:
        errors.append(f"{spec.model_family} {asof}: no PIT membership rows exported")
    tickers = [str(row.get("ticker") or "") for row in rows]
    duplicates = sorted({ticker for ticker in tickers if ticker and tickers.count(ticker) > 1})
    if duplicates:
        errors.append(f"{spec.model_family} {asof}: duplicate PIT tickers: {duplicates[:10]}")
    bad_asof = sorted({str(row.get("asof_date") or "") for row in rows if str(row.get("asof_date") or "") != asof})
    if bad_asof:
        errors.append(f"{spec.model_family} {asof}: rows contain wrong asof_date values: {bad_asof[:5]}")
    bad_survivorship = [str(row.get("ticker") or "") for row in rows if int(row.get("survivorship_corrected_panel_flag") or 0) != 1]
    if bad_survivorship:
        errors.append(f"{spec.model_family} {asof}: rows not survivorship-correct stamped: {bad_survivorship[:10]}")
    bad_stage11 = [
        str(row.get("ticker") or "")
        for row in rows
        if int(row.get("stage11_calibration_input_eligible_flag") or 0) == 1
        and (
            int(row.get("survivorship_corrected_panel_flag") or 0) != 1
            or safe_float(row.get("final_score")) is None
            or int(row.get("price_available_on_asof_flag") or 0) != 1
            or str(row.get("stage11_exclusion_reason") or "")
        )
    ]
    if bad_stage11:
        errors.append(f"{spec.model_family} {asof}: invalid Stage 11 eligible rows: {bad_stage11[:10]}")
    bad_portfolio = [
        str(row.get("ticker") or "")
        for row in rows
        if int(row.get("portfolio_candidate_gate") or 0) == 1 and int(row.get("oos_score_valid_flag") or 0) != 1
    ]
    if bad_portfolio:
        errors.append(f"{spec.model_family} {asof}: portfolio candidates without strict OOS score: {bad_portfolio[:10]}")
    if errors:
        raise ValueError("; ".join(errors))


def manifest_payload(
    *,
    spec: FamilySpec,
    dates: list[str],
    rows: list[dict[str, Any]],
    coverage: list[dict[str, Any]],
    horizons: list[int],
    output_files: list[str],
) -> dict[str, Any]:
    eligible = sum(1 for row in rows if int(row.get("stage11_calibration_input_eligible_flag") or 0) == 1)
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "model_family": spec.model_family,
        "panel_source": PANEL_SOURCE,
        "survivorship_corrected_panel_flag": 1,
        "date_count": len(dates),
        "start_date": dates[0] if dates else "",
        "end_date": dates[-1] if dates else "",
        "row_count": len(rows),
        "stage11_calibration_input_eligible_rows": eligible,
        "horizons_trading_days": horizons,
        "coverage": coverage,
        "output_files": output_files,
        "usage_note": (
            "This export is the Stage 11 historical calibration input panel. It starts from PIT universe "
            "membership and recomputes scores in memory over each PIT cross-section. Dashboard snapshots "
            "remain current-universe portfolio/report artifacts and should not be relabeled as "
            "survivorship-correct calibration panels."
        ),
    }


def export_family(
    conn: sqlite3.Connection,
    *,
    config: dict[str, Any],
    spec: FamilySpec,
    dates: list[str],
    horizons: list[int],
    output_dir: Path,
    max_price_staleness_days: int,
    combined_only: bool,
) -> None:
    all_members: dict[str, list[dict[str, Any]]] = {asof: load_pit_members(conn, spec, asof) for asof in dates}
    tickers = {member["ticker"] for members in all_members.values() for member in members}
    benchmark = benchmark_ticker(config, spec)
    if benchmark:
        tickers.add(benchmark)
    source_ids = price_source_ids(config, spec)
    price_series = load_price_series(conn, tickers=tickers, source_ids=source_ids, start_date="2010-01-01")
    benchmark_series = price_series.get(benchmark)

    family_rows: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []
    written_files: list[str] = []
    family_dir = output_dir / spec.model_family
    for asof in dates:
        members = all_members[asof]
        raw_rows, outputs = build_pit_scores(
            conn,
            config,
            spec,
            members,
            asof,
            price_series,
            benchmark_series,
            max_price_staleness_days,
        )
        rows = merge_member_score_rows(
            config=config,
            spec=spec,
            asof=asof,
            members=members,
            raw_rows=raw_rows,
            outputs=outputs,
            price_series=price_series,
            horizons=horizons,
            max_price_staleness_days=max_price_staleness_days,
        )
        validate_panel_rows(rows, asof=asof, spec=spec)
        fieldnames = ordered_fieldnames(rows, horizons)
        if not combined_only:
            dated_path = family_dir / asof / f"{spec.output_prefix}_stage11_survivorship_calibration_panel.csv"
            write_csv(dated_path, rows, fieldnames)
            written_files.append(str(dated_path.resolve()))
        family_rows.extend(rows)
        coverage_rows.append(summarize_date(rows, asof, horizons))
        print(
            f"{spec.model_family} {asof}: membership={len(rows)} "
            f"score_rows={coverage_rows[-1]['score_recomputed_rows']} "
            f"stage11_eligible={coverage_rows[-1]['stage11_calibration_input_eligible_rows']}"
        )

    family_fieldnames = ordered_fieldnames(family_rows, horizons)
    combined_path = family_dir / f"{spec.output_prefix}_stage11_survivorship_calibration_panel.csv"
    coverage_path = family_dir / f"{spec.output_prefix}_stage11_survivorship_calibration_coverage.csv"
    manifest_path = family_dir / f"{spec.output_prefix}_stage11_survivorship_calibration_manifest.json"
    write_csv(combined_path, family_rows, family_fieldnames)
    coverage_fields = ordered_fieldnames(coverage_rows, horizons)
    write_csv(coverage_path, coverage_rows, coverage_fields)
    written_files.extend([str(combined_path.resolve()), str(coverage_path.resolve()), str(manifest_path.resolve())])
    manifest = manifest_payload(
        spec=spec,
        dates=dates,
        rows=family_rows,
        coverage=coverage_rows,
        horizons=horizons,
        output_files=written_files,
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Wrote {spec.model_family} Stage 11 panel: {combined_path}")


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else (resolve_path(cfg_get(config, "paths.output_dir"), base_dir=base_dir) / DEFAULT_OUTPUT_SUBDIR)
    )
    timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))
    max_staleness = int(
        args.max_price_staleness_days
        if args.max_price_staleness_days is not None
        else cfg_get(config, "market_data_policy.max_staleness_days", 7)
    )
    families = resolve_families(args.families, args.family)
    horizons = resolve_horizons(args.horizons)
    explicit_dates = split_values(args.date) + split_values(args.dates)

    with sqlite_readonly(db_path, timeout_sec=timeout_sec) as conn:
        dates = resolve_dates(
            conn,
            explicit_dates=explicit_dates,
            start_date=args.start_date,
            end_date=args.end_date,
            frequency=args.frequency,
            calendar_ticker=args.calendar_ticker,
        )
        if not dates:
            raise ValueError("No target dates resolved for Stage 11 export.")
        for spec in families:
            export_family(
                conn,
                config=config,
                spec=spec,
                dates=dates,
                horizons=horizons,
                output_dir=output_dir,
                max_price_staleness_days=max_staleness,
                combined_only=bool(args.combined_only),
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path
from biotech_index.core.db import connect, finish_run, init_db, quote_identifier, start_run, utc_now
from biotech_index.core.logging_utils import configure_utc_logging
from biotech_index.core.pipeline_guards import (
    read_final_scoring_tickers,
    subset_mode_enabled,
    subset_output_path,
    validate_full_universe_coverage,
    validate_layer_freshness,
    validate_nonempty_selection,
    validate_output_coverage,
    validate_requested_tickers,
)


LOGGER = logging.getLogger("build_multibagger_features")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SQLITE_PARAM_CHUNK_SIZE = 800


def chunked(values: list[Any] | tuple[Any, ...], size: int = SQLITE_PARAM_CHUNK_SIZE) -> list[list[Any]]:
    step = max(1, int(size))
    return [list(values[start : start + step]) for start in range(0, len(values), step)]


FEATURE_FIELDS = [
    "asof_date",
    "company_id",
    "ticker",
    "company_name",
    "commercial_acceleration_score",
    "upside_capacity_score",
    "cash_flow_acceleration_score",
    "survival_quality_score",
    "governance_event_score",
    "market_confirmation_score",
    "catalyst_quality_score",
    "commercial_fragility_risk_score",
    "multibagger_risk_penalty",
    "evidence_or_catalyst_flag",
    "data_quality",
    "missing_fields",
    "proxy_fields_used",
    "payload_json",
]

LATEST_SOURCE_TABLES = {
    "commercial_value_features_daily",
    "financial_survival_features",
    "market_features_daily",
    "governance_event_features_daily",
    "forward_guidance_features_daily",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build multibagger composite features from clinical, SEC, Form 4, financial, and market layers.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="Feature date in YYYY-MM-DD. Defaults to latest biotech feature date.")
    parser.add_argument("--tickers", type=str, default="", help="Optional comma-separated ticker subset.")
    parser.add_argument("--max-companies", type=int, default=0, help="Smoke-test limit. 0 means all.")
    parser.add_argument("--allow-missing-market", action="store_true", help="Build low-quality rows for companies without market features instead of failing historical snapshot validation.")
    return parser.parse_args()


def configure_logging() -> None:
    configure_utc_logging()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if len(text) < 10:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def to_float(raw: object, default: float = 0.0) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def to_optional_float(raw: object) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def to_int(raw: object, default: int = 0) -> int:
    return int(round(to_float(raw, float(default))))


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def parse_json(raw: object) -> dict[str, Any]:
    try:
        payload = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def normalize_ticker(raw: object) -> str:
    return str(raw or "").strip().upper().replace(".", "-")


def missing_layer_tickers(base_rows: list[dict[str, Any]], layer: dict[int, dict[str, Any]]) -> list[str]:
    return sorted(
        {
            normalize_ticker(row.get("ticker"))
            for row in base_rows
            if int(row["company_id"]) not in layer and normalize_ticker(row.get("ticker"))
        }
    )


def latest_feature_date(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT MAX(asof_date) AS asof_date FROM daily_features").fetchone()
    asof = str(row["asof_date"] or "") if row else ""
    if not asof:
        raise ValueError("No daily_features rows found. Run 10_build_biotech_features.py first.")
    return asof


def load_base_rows(conn: sqlite3.Connection, asof_date: str, ticker_filter: set[str], max_companies: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT f.asof_date, f.company_id, f.catalyst_score_raw, f.credibility_score_raw,
               f.risk_score_raw, f.feature_json, c.ticker, c.company_name
        FROM daily_features f
        JOIN companies c ON c.company_id = f.company_id
        WHERE f.asof_date = ?
        ORDER BY c.ticker
        """,
        (asof_date,),
    ).fetchall()
    out = []
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        if ticker_filter and ticker not in ticker_filter:
            continue
        out.append(dict(row))
        if max_companies > 0 and len(out) >= max_companies:
            break
    return out


def load_latest_table(
    conn: sqlite3.Connection,
    table: str,
    asof_date: str,
    *,
    preferred_source: str = "",
) -> dict[int, dict[str, Any]]:
    if table not in LATEST_SOURCE_TABLES:
        raise ValueError(f"Unsupported source table for latest-row load: {table}")
    table_sql = quote_identifier(table)
    rows = conn.execute(
        f"""
        SELECT t.*
        FROM {table_sql} t
        JOIN (
            SELECT company_id, MAX(asof_date) AS max_asof
            FROM {table_sql}
            WHERE asof_date <= ?
            GROUP BY company_id
        ) latest
          ON latest.company_id = t.company_id AND latest.max_asof = t.asof_date
        """,
        (asof_date,),
    ).fetchall()
    sorted_rows = sorted(
        rows,
        key=lambda row: (
            int(row["company_id"]),
            0
            if preferred_source and "source" in row.keys() and str(row["source"] or "") == preferred_source
            else 1,
            str(row["source"] or "") if "source" in row.keys() else "",
        ),
    )
    out: dict[int, dict[str, Any]] = {}
    for row in sorted_rows:
        company_id = int(row["company_id"])
        if company_id not in out:
            out[company_id] = dict(row)
    return out


def data_quality(critical_values: list[str], optional_values: list[str]) -> str:
    critical = [value.lower() for value in critical_values if value]
    optional = [value.lower() for value in optional_values if value]
    if any(value == "low" for value in critical):
        return "low"
    if len([value for value in critical if value != "high"]) >= 2:
        return "medium"
    if any(value == "low" for value in optional) or len([value for value in optional if value != "high"]) >= 2:
        return "medium"
    if any(value != "high" for value in critical):
        return "medium"
    return "high"


def score_commercial_acceleration(commercial: dict[str, Any], forward: dict[str, Any]) -> float:
    revenue_growth = clamp(to_float(commercial.get("revenue_growth_score"), 45.0))
    margin = clamp(to_float(commercial.get("margin_score"), 45.0))
    profitability = clamp(to_float(commercial.get("profitability_score"), 35.0))
    commercial_quality = clamp(to_float(commercial.get("commercial_quality_score"), 35.0))
    guidance = clamp(to_float(forward.get("guidance_score"), 45.0))
    forward_growth = clamp(to_float(forward.get("forward_growth_score"), 45.0))
    revenue = to_float(commercial.get("ttm_revenue"), 0.0)
    if revenue <= 0:
        return round(clamp(0.20 * revenue_growth + 0.20 * margin + 0.20 * profitability + 0.20 * commercial_quality + 0.20 * guidance), 4)
    return round(clamp(0.25 * revenue_growth + 0.15 * margin + 0.20 * profitability + 0.20 * commercial_quality + 0.10 * guidance + 0.10 * forward_growth), 4)


def score_cash_flow(commercial: dict[str, Any], survival: dict[str, Any]) -> float:
    score = 45.0
    ocf = to_optional_float(commercial.get("operating_cash_flow_ttm"))
    fcf = to_optional_float(commercial.get("free_cash_flow_ttm"))
    fcf_yield = to_optional_float(commercial.get("fcf_yield"))
    op_margin = to_optional_float(commercial.get("operating_margin_pct"))
    revenue_growth = to_optional_float(commercial.get("revenue_yoy_growth_pct"))
    cash_yoy = to_optional_float(survival.get("cash_yoy_change_pct"))
    burn_accel = to_int(survival.get("burn_acceleration_flag"))

    if ocf is not None and ocf > 0:
        score += 14.0
    elif ocf is not None and ocf < 0:
        score -= 8.0
    if fcf is not None and fcf > 0:
        score += 18.0
    elif fcf is not None and fcf < 0:
        score -= 8.0
    if fcf_yield is not None:
        if fcf_yield >= 0.08:
            score += 14.0
        elif fcf_yield >= 0.03:
            score += 8.0
        elif fcf_yield <= -0.15:
            score -= 12.0
    if op_margin is not None and op_margin > 0:
        score += min(10.0, op_margin * 50.0)
    if revenue_growth is not None and revenue_growth > 0.20:
        score += 6.0
    if cash_yoy is not None and cash_yoy < -0.30 and (fcf is None or fcf <= 0):
        score -= 12.0
    if burn_accel:
        score -= 10.0
    return round(clamp(score), 4)


def score_survival(commercial: dict[str, Any], survival: dict[str, Any]) -> float:
    if survival:
        base = clamp(to_float(survival.get("financial_survival_score"), 45.0))
        debt_to_cash = to_optional_float(survival.get("debt_to_cash"))
        runway = to_optional_float(survival.get("cash_runway_months"))
        if debt_to_cash is not None and debt_to_cash > 1.5:
            base -= 10.0
        if runway is not None and runway >= 24:
            base += 6.0
        return round(clamp(base), 4)
    balance = clamp(to_float(commercial.get("balance_sheet_score"), 45.0))
    dilution = clamp(to_float(commercial.get("dilution_score"), 45.0))
    return round(clamp(0.60 * balance + 0.40 * dilution), 4)


def score_market_confirmation(market: dict[str, Any]) -> float:
    if not market:
        return 35.0
    score = 45.0
    rs = to_optional_float(market.get("relative_strength_3m_vs_xbi"))
    price_200 = to_optional_float(market.get("price_vs_200d_pct"))
    return_3m = to_optional_float(market.get("return_3m_pct"))
    dist_52w = to_optional_float(market.get("distance_from_52w_high_pct"))
    liquidity = clamp(to_float(market.get("liquidity_score"), 35.0))
    if rs is not None:
        if rs > 0.20:
            score += 20.0
        elif rs > 0.05:
            score += 14.0
        elif rs > 0:
            score += 8.0
        elif rs < -0.20:
            score -= 18.0
        elif rs < -0.05:
            score -= 9.0
    if price_200 is not None:
        if price_200 > 0.20:
            score += 15.0
        elif price_200 > 0:
            score += 10.0
        elif price_200 < -0.25:
            score -= 18.0
        elif price_200 < -0.20:
            score -= 15.0
        elif price_200 < -0.10:
            score -= 8.0
        else:
            score -= 5.0
    if return_3m is not None:
        score += 6.0 if return_3m > 0 else -4.0
    if dist_52w is not None:
        if dist_52w >= -0.15:
            score += 8.0
        elif dist_52w <= -0.60:
            score -= 24.0
        elif dist_52w <= -0.45:
            score -= 18.0
        elif dist_52w <= -0.30:
            score -= 10.0
    if price_200 is not None and dist_52w is not None and price_200 < -0.15 and dist_52w < -0.35:
        score = min(score, 55.0)
    score += (liquidity - 50.0) * 0.12
    return round(clamp(score), 4)


def score_catalyst(base_payload: dict[str, Any], catalyst_raw: float, credibility_raw: float) -> float:
    ctgov = base_payload.get("ctgov", {}) if isinstance(base_payload, dict) else {}
    phase2_3 = to_int(ctgov.get("lead_phase2_3_active_trials")) + to_int(ctgov.get("program_phase2_3_active_trials"))
    pivotal = to_int(ctgov.get("active_pivotal_trials")) + to_int(ctgov.get("active_phase3_trials"))
    pipeline_quality = clamp(to_float(ctgov.get("core_pipeline_quality_score"), 0.0))
    score = 0.45 * clamp(catalyst_raw) + 0.25 * clamp(credibility_raw) + 0.30 * pipeline_quality
    if pivotal > 0:
        score += min(12.0, pivotal * 3.0)
    elif phase2_3 > 0:
        score += min(8.0, phase2_3 * 2.0)
    return round(clamp(score), 4)


def risk_penalty(
    base_payload: dict[str, Any],
    risk_raw: float,
    commercial: dict[str, Any],
    survival: dict[str, Any],
    governance: dict[str, Any],
    market: dict[str, Any],
    config: dict[str, Any],
) -> float:
    penalty = 0.55 * clamp(risk_raw)
    fragility = clamp(to_float(governance.get("commercial_fragility_risk_score"), 0.0))
    penalty += 0.15 * clamp(to_float(survival.get("dilution_pressure_score"), 0.0))
    penalty += 0.15 * clamp(to_float(governance.get("governance_risk_score"), 0.0))
    penalty += 0.35 * fragility
    penalty += 0.10 * (100.0 - clamp(to_float(market.get("liquidity_score"), 45.0)))
    market_cap = to_optional_float(commercial.get("market_cap")) or to_optional_float(market.get("market_cap"))
    soft_cap = float(cfg_get(config, "multibagger.market_cap_soft_cap", 25_000_000_000))
    hard_cap = float(cfg_get(config, "multibagger.market_cap_hard_cap", 75_000_000_000))
    if market_cap is not None:
        if market_cap >= hard_cap:
            penalty += 25.0
        elif market_cap >= soft_cap:
            penalty += 10.0
    sec_liq = base_payload.get("sec_and_liquidity", {}) if isinstance(base_payload, dict) else {}
    if str(sec_liq.get("going_concern_status") or "").lower() == "confirmed":
        penalty += 20.0
    if to_int(sec_liq.get("reverse_split_hits_2y")) > 0:
        penalty += 12.0
    price_200 = to_optional_float(market.get("price_vs_200d_pct"))
    dist_52w = to_optional_float(market.get("distance_from_52w_high_pct"))
    if price_200 is not None and price_200 < -0.20:
        penalty += 8.0
    if dist_52w is not None and dist_52w < -0.45:
        penalty += 10.0
    return round(clamp(penalty), 4)


def evidence_flag(
    base_payload: dict[str, Any],
    commercial: dict[str, Any],
    forward: dict[str, Any],
    governance: dict[str, Any],
    *,
    revenue_threshold: float,
) -> int:
    ctgov = base_payload.get("ctgov", {}) if isinstance(base_payload, dict) else {}
    has_advanced_clinical = (
        to_int(ctgov.get("lead_phase2_3_active_trials")) > 0
        or to_int(ctgov.get("program_phase2_3_active_trials")) > 0
        or to_int(ctgov.get("active_pivotal_trials")) > 0
        or to_int(ctgov.get("active_phase3_trials")) > 0
    )
    has_commercial = bool(
        to_int(commercial.get("commercial_stage_flag"))
        or to_int(commercial.get("profitable_flag"))
        or to_float(commercial.get("ttm_revenue"), 0.0) >= revenue_threshold
    )
    has_forward = to_float(forward.get("guidance_score"), 0.0) >= 60.0
    has_governance = to_float(governance.get("governance_event_score"), 0.0) >= 45.0
    return int(has_advanced_clinical or has_commercial or has_forward or has_governance)


def build_row(
    base: dict[str, Any],
    *,
    commercial: dict[str, Any],
    survival: dict[str, Any],
    market: dict[str, Any],
    governance: dict[str, Any],
    forward: dict[str, Any],
    config: dict[str, Any],
    asof_date: str,
) -> dict[str, Any]:
    payload = parse_json(base.get("feature_json"))
    catalyst_raw = clamp(to_float(base.get("catalyst_score_raw"), 0.0))
    credibility_raw = clamp(to_float(base.get("credibility_score_raw"), 0.0))
    risk_raw = clamp(to_float(base.get("risk_score_raw"), 0.0))
    revenue_threshold = float(cfg_get(config, "commercial_value.commercial_stage_revenue_min", 50_000_000))
    commercial_acceleration = score_commercial_acceleration(commercial, forward)
    upside = clamp(to_float(commercial.get("upside_capacity_score"), 50.0))
    cash_flow = score_cash_flow(commercial, survival)
    survival_score = score_survival(commercial, survival)
    governance_score = clamp(to_float(governance.get("governance_event_score"), 15.0))
    commercial_fragility = clamp(to_float(governance.get("commercial_fragility_risk_score"), 0.0))
    market_score = score_market_confirmation(market)
    catalyst_score = score_catalyst(payload, catalyst_raw, credibility_raw)
    risk = risk_penalty(payload, risk_raw, commercial, survival, governance, market, config)
    evidence = evidence_flag(payload, commercial, forward, governance, revenue_threshold=revenue_threshold)

    missing: list[str] = []
    proxies: list[str] = []
    for name, source in [
        ("commercial_value_features_daily", commercial),
        ("financial_survival_features", survival),
        ("market_features_daily", market),
        ("governance_event_features_daily", governance),
        ("forward_guidance_features_daily", forward),
    ]:
        if not source:
            missing.append(name)
        else:
            proxy = str(source.get("proxy_fields_used") or "")
            if proxy:
                proxies.append(proxy)
    quality = data_quality(
        [
            str(commercial.get("data_quality") or ""),
            str(survival.get("data_quality") or ""),
            str(market.get("market_data_quality") or ""),
        ],
        [
            str(governance.get("data_quality") or ""),
            str(forward.get("data_quality") or ""),
        ],
    )
    evidence_payload = {
        "source_dates": {
            "daily_features": base.get("asof_date"),
            "commercial": commercial.get("asof_date", ""),
            "financial_survival": survival.get("asof_date", ""),
            "market": market.get("asof_date", ""),
            "governance": governance.get("asof_date", ""),
            "forward_guidance": forward.get("asof_date", ""),
        },
        "commercial": {
            "ttm_revenue": commercial.get("ttm_revenue"),
            "revenue_yoy_growth_pct": commercial.get("revenue_yoy_growth_pct"),
            "gross_margin_pct": commercial.get("gross_margin_pct"),
            "operating_margin_pct": commercial.get("operating_margin_pct"),
            "free_cash_flow_ttm": commercial.get("free_cash_flow_ttm"),
            "fcf_yield": commercial.get("fcf_yield"),
            "market_cap": commercial.get("market_cap"),
            "ev_to_sales": commercial.get("ev_to_sales"),
            "pe_ratio": commercial.get("pe_ratio"),
            "commercial_stage_flag": commercial.get("commercial_stage_flag"),
            "profitable_flag": commercial.get("profitable_flag"),
        },
        "survival": {
            "cash_runway_months": survival.get("cash_runway_months"),
            "cash_yoy_change_pct": survival.get("cash_yoy_change_pct"),
            "debt_to_cash": survival.get("debt_to_cash"),
            "dilution_pressure_score": survival.get("dilution_pressure_score"),
            "financial_survival_score": survival.get("financial_survival_score"),
        },
        "market": {
            "return_3m_pct": market.get("return_3m_pct"),
            "relative_strength_3m_vs_xbi": market.get("relative_strength_3m_vs_xbi"),
            "price_vs_200d_pct": market.get("price_vs_200d_pct"),
            "distance_from_52w_high_pct": market.get("distance_from_52w_high_pct"),
            "avg_dollar_volume_20d": market.get("avg_dollar_volume_20d"),
            "liquidity_score": market.get("liquidity_score"),
        },
        "governance": {
            "insider_buy_count_90d": governance.get("insider_buy_count_90d"),
            "insider_buy_value_90d": governance.get("insider_buy_value_90d"),
            "insider_buy_cluster_count_90d": governance.get("insider_buy_cluster_count_90d"),
            "buyback_event_count_365d": governance.get("buyback_event_count_365d"),
            "asr_event_count_365d": governance.get("asr_event_count_365d"),
            "governance_event_score": governance.get("governance_event_score"),
            "governance_risk_score": governance.get("governance_risk_score"),
            "regulatory_setback_count_365d": governance.get("regulatory_setback_count_365d"),
            "adverse_legal_event_count_365d": governance.get("adverse_legal_event_count_365d"),
            "generic_competition_risk_count_365d": governance.get("generic_competition_risk_count_365d"),
            "product_concentration_risk_count_365d": governance.get("product_concentration_risk_count_365d"),
            "commercial_fragility_risk_score": governance.get("commercial_fragility_risk_score"),
        },
        "forward_guidance": {
            "forward_revenue_midpoint": forward.get("forward_revenue_midpoint"),
            "forward_revenue_growth_pct": forward.get("forward_revenue_growth_pct"),
            "forward_ebitda_midpoint": forward.get("forward_ebitda_midpoint"),
            "guidance_score": forward.get("guidance_score"),
        },
        "clinical": payload.get("ctgov", {}) if isinstance(payload, dict) else {},
        "risk": payload.get("sec_and_liquidity", {}) if isinstance(payload, dict) else {},
        "component_scores": {
            "commercial_acceleration_score": commercial_acceleration,
            "upside_capacity_score": upside,
            "cash_flow_acceleration_score": cash_flow,
            "survival_quality_score": survival_score,
            "governance_event_score": governance_score,
            "market_confirmation_score": market_score,
            "catalyst_quality_score": catalyst_score,
            "commercial_fragility_risk_score": commercial_fragility,
            "multibagger_risk_penalty": risk,
        },
    }
    return {
        "asof_date": asof_date,
        "company_id": int(base["company_id"]),
        "ticker": normalize_ticker(base.get("ticker")),
        "company_name": str(base.get("company_name") or ""),
        "commercial_acceleration_score": commercial_acceleration,
        "upside_capacity_score": round(upside, 4),
        "cash_flow_acceleration_score": cash_flow,
        "survival_quality_score": survival_score,
        "governance_event_score": round(governance_score, 4),
        "market_confirmation_score": market_score,
        "catalyst_quality_score": catalyst_score,
        "commercial_fragility_risk_score": round(commercial_fragility, 4),
        "multibagger_risk_penalty": risk,
        "evidence_or_catalyst_flag": evidence,
        "data_quality": quality,
        "missing_fields": ";".join(missing),
        "proxy_fields_used": ";".join(dict.fromkeys(filter(None, proxies))),
        "payload_json": json.dumps(evidence_payload, ensure_ascii=True, sort_keys=True),
    }


def upsert_rows(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    asof_date: str,
    *,
    target_company_ids: set[int] | None = None,
) -> None:
    now = utc_now()
    placeholders = ", ".join("?" for _ in FEATURE_FIELDS)
    with conn:
        if target_company_ids is None:
            conn.execute("DELETE FROM multibagger_features_daily WHERE asof_date = ?", (asof_date,))
        elif target_company_ids:
            for company_chunk in chunked(sorted(target_company_ids)):
                company_placeholders = ",".join("?" for _ in company_chunk)
                conn.execute(
                    f"DELETE FROM multibagger_features_daily WHERE asof_date = ? AND company_id IN ({company_placeholders})",
                    (asof_date, *company_chunk),
                )
        else:
            return
        conn.executemany(
            f"""
            INSERT INTO multibagger_features_daily({", ".join(FEATURE_FIELDS)}, created_at, updated_at)
            VALUES ({placeholders}, ?, ?)
            """,
            [tuple(row.get(field) for field in FEATURE_FIELDS) + (now, now) for row in rows],
        )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FEATURE_FIELDS, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    configure_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    universe_csv = resolve_path(
        cfg_get(
            config,
            "multibagger.final_scoring_universe_csv",
            cfg_get(config, "biotech_features.final_scoring_universe_csv", cfg_get(config, "governance_events.final_scoring_universe_csv")),
        ),
        base_dir=base_dir,
    )
    output_dir = resolve_path(cfg_get(config, "multibagger.output_dir"), base_dir=base_dir)
    output_csv = output_dir / str(cfg_get(config, "multibagger.features_csv", "multibagger_features.csv"))
    ticker_filter = {normalize_ticker(value) for value in args.tickers.split(",") if value.strip()}
    sqlite_timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))
    scoring_tickers = read_final_scoring_tickers(universe_csv)
    subset_mode = subset_mode_enabled(ticker_filter=ticker_filter, max_count=int(args.max_companies))
    output_csv = subset_output_path(output_csv, subset_mode=subset_mode)

    with connect(db_path, timeout_sec=sqlite_timeout_sec) as conn:
        run_id: int | None = None
        init_db(conn)
        asof_obj = parse_date(args.asof) if args.asof else None
        if args.asof and asof_obj is None:
            raise ValueError(f"Invalid --asof date: {args.asof}")
        asof_date = asof_obj.isoformat() if asof_obj else latest_feature_date(conn)
        run_id = start_run(conn, run_type="build_multibagger_features", input_path=db_path)
        try:
            base_rows = load_base_rows(conn, asof_date, ticker_filter, args.max_companies)
            validate_nonempty_selection(count=len(base_rows), context="multibagger feature build", subset_mode=subset_mode)
            loaded_tickers = [str(row["ticker"]) for row in base_rows]
            validate_requested_tickers(requested_tickers=ticker_filter, loaded_tickers=loaded_tickers, context="multibagger feature build")
            validate_full_universe_coverage(
                expected_tickers=scoring_tickers,
                observed_tickers=loaded_tickers,
                context="multibagger feature build",
                subset_mode=subset_mode,
            )
            commercial = load_latest_table(conn, "commercial_value_features_daily", asof_date)
            survival = load_latest_table(conn, "financial_survival_features", asof_date)
            market = load_latest_table(
                conn,
                "market_features_daily",
                asof_date,
                preferred_source=str(cfg_get(config, "multibagger.preferred_market_source", "interactive_brokers") or ""),
            )
            governance = load_latest_table(conn, "governance_event_features_daily", asof_date)
            forward = load_latest_table(conn, "forward_guidance_features_daily", asof_date)
            if not subset_mode:
                missing_market_tickers = missing_layer_tickers(base_rows, market)
                if args.allow_missing_market and missing_market_tickers:
                    LOGGER.warning(
                        "Multibagger feature build continuing without market rows for %d ticker(s): %s",
                        len(missing_market_tickers),
                        ",".join(missing_market_tickers[:25]) + (f"...(+{len(missing_market_tickers) - 25})" if len(missing_market_tickers) > 25 else ""),
                    )
                missing_layers = {
                    "commercial_value_features_daily": missing_layer_tickers(base_rows, commercial),
                    "financial_survival_features": missing_layer_tickers(base_rows, survival),
                    "governance_event_features_daily": missing_layer_tickers(base_rows, governance),
                    "forward_guidance_features_daily": missing_layer_tickers(base_rows, forward),
                }
                if not args.allow_missing_market:
                    missing_layers["market_features_daily"] = missing_market_tickers
                failures = [
                    f"{name} missing {len(tickers)} ticker(s): {','.join(tickers[:25])}{'...' if len(tickers) > 25 else ''}"
                    for name, tickers in missing_layers.items()
                    if tickers
                ]
                if failures:
                    raise RuntimeError("Multibagger feature build missing upstream layer rows: " + " | ".join(failures))
                max_upstream_staleness_days = int(cfg_get(config, "biotech_refresh.max_upstream_staleness_days", 0))
                for layer_name, layer_rows in (
                    ("commercial_value_features_daily", commercial),
                    ("financial_survival_features", survival),
                    ("market_features_daily", market),
                    ("governance_event_features_daily", governance),
                    ("forward_guidance_features_daily", forward),
                ):
                    freshness_base_rows = base_rows
                    if args.allow_missing_market and layer_name == "market_features_daily":
                        market_company_ids = set(market)
                        freshness_base_rows = [
                            row for row in base_rows if int(row["company_id"]) in market_company_ids
                        ]
                    validate_layer_freshness(
                        base_rows=freshness_base_rows,
                        layer_rows_by_company=layer_rows,
                        asof_date=asof_date,
                        context=f"multibagger feature build {layer_name}",
                        max_staleness_days=max_upstream_staleness_days,
                    )
            rows = [
                build_row(
                    row,
                    commercial=commercial.get(int(row["company_id"]), {}),
                    survival=survival.get(int(row["company_id"]), {}),
                    market=market.get(int(row["company_id"]), {}),
                    governance=governance.get(int(row["company_id"]), {}),
                    forward=forward.get(int(row["company_id"]), {}),
                    config=config,
                    asof_date=asof_date,
                )
                for row in base_rows
            ]
            partial_run = bool(ticker_filter) or int(args.max_companies) > 0
            validate_output_coverage(
                expected_tickers=scoring_tickers,
                output_tickers=[row["ticker"] for row in rows],
                context="multibagger feature build",
                subset_mode=subset_mode,
            )
            upsert_rows(
                conn,
                rows,
                asof_date,
                target_company_ids={int(row["company_id"]) for row in base_rows} if partial_run else None,
            )
            write_csv(output_csv, rows)
            LOGGER.info("Multibagger feature build complete: rows=%d output=%s", len(rows), output_csv)
            finish_run(conn, run_id=run_id, status="success", row_count=len(rows), message=f"asof={asof_date} output={output_csv}")
        except BaseException as exc:
            if run_id is not None and not (isinstance(exc, SystemExit) and exc.code in (0, None)):
                finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()

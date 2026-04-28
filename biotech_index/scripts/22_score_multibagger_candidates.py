#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sqlite3
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path
from biotech_index.core.db import connect, finish_run, init_db, start_run, utc_now


LOGGER = logging.getLogger("score_multibagger_candidates")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


SCORE_FIELDS = [
    "asof_date",
    "company_id",
    "ticker",
    "company_name",
    "multibagger_score",
    "rank",
    "bucket",
    "top_evidence_json",
]


CSV_FIELDS = [
    "asof_date",
    "rank",
    "ticker",
    "company_name",
    "bucket",
    "multibagger_score",
    "commercial_acceleration_score",
    "upside_capacity_score",
    "cash_flow_acceleration_score",
    "survival_quality_score",
    "governance_event_score",
    "market_confirmation_score",
    "catalyst_quality_score",
    "commercial_fragility_risk_score",
    "multibagger_risk_penalty",
    "ttm_revenue",
    "revenue_yoy_growth_pct",
    "free_cash_flow_ttm",
    "fcf_yield",
    "market_cap",
    "ev_to_sales",
    "cash_runway_months",
    "forward_revenue_midpoint",
    "forward_revenue_growth_pct",
    "insider_buy_count_90d",
    "insider_buy_value_90d",
    "buyback_event_count_365d",
    "asr_event_count_365d",
    "relative_strength_3m_vs_xbi",
    "price_vs_200d_pct",
    "distance_from_52w_high_pct",
    "avg_dollar_volume_20d",
    "primary_nct",
    "lead_phase2_3_active_trials",
    "program_phase2_3_active_trials",
    "active_pivotal_trials",
    "evidence_or_catalyst_flag",
    "data_quality",
    "missing_fields",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Score multibagger candidate composite.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="Score date in YYYY-MM-DD. Defaults to latest multibagger feature date.")
    return parser.parse_args()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    for handler in logging.getLogger().handlers:
        if handler.formatter is not None:
            handler.formatter.converter = time.gmtime


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def to_float(raw: object, default: float = 0.0) -> float:
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


def latest_feature_date(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT MAX(asof_date) AS asof_date FROM multibagger_features_daily").fetchone()
    asof = str(row["asof_date"] or "") if row else ""
    if not asof:
        raise ValueError("No multibagger_features_daily rows found. Run 21_build_multibagger_features.py first.")
    return asof


def load_feature_rows(conn: sqlite3.Connection, asof_date: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM multibagger_features_daily
        WHERE asof_date = ?
        ORDER BY ticker
        """,
        (asof_date,),
    ).fetchall()
    return [dict(row) for row in rows]


def bucket_for(score: float, risk: float, evidence: bool, liquidity_ok: bool, payload: dict[str, Any], config: dict[str, Any]) -> str:
    high_min = float(cfg_get(config, "multibagger.high_conviction_min", 80))
    watch_min = float(cfg_get(config, "multibagger.watchlist_min", 65))
    spec_min = float(cfg_get(config, "multibagger.speculative_min", 50))
    max_high_risk = float(cfg_get(config, "multibagger.max_high_conviction_risk", 35))
    max_watch_risk = float(cfg_get(config, "multibagger.max_watchlist_risk", 55))
    max_spec_risk = float(cfg_get(config, "multibagger.max_speculative_risk", 75))
    avoid_risk_min = float(cfg_get(config, "multibagger.avoid_risk_min", 75))
    avoid_fragility_min = float(cfg_get(config, "multibagger.avoid_fragility_min", 70))
    require_evidence = bool(cfg_get(config, "multibagger.require_event_or_catalyst", True))
    commercial = payload.get("commercial", {}) if isinstance(payload, dict) else {}
    components = payload.get("component_scores", {}) if isinstance(payload, dict) else {}
    market_cap = to_float(commercial.get("market_cap"), 0.0)
    fragility = to_float(components.get("commercial_fragility_risk_score"), 0.0)
    hard_cap = float(cfg_get(config, "multibagger.market_cap_hard_cap", 75_000_000_000))

    if not liquidity_ok:
        return "avoid_illiquid"
    if risk >= avoid_risk_min:
        return "avoid_high_risk"
    if fragility >= avoid_fragility_min:
        return "avoid_commercial_fragility"
    if require_evidence and not evidence:
        return "avoid_no_event_or_catalyst"
    if market_cap >= hard_cap and score >= watch_min:
        return "large_cap_quality"
    if score >= high_min and risk <= max_high_risk:
        return "high_conviction_multibagger"
    if score >= watch_min and risk <= max_watch_risk:
        return "multibagger_watchlist"
    if score >= spec_min and risk <= max_spec_risk:
        return "speculative_multibagger"
    return "avoid"


def score_one(row: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    weights = cfg_get(config, "multibagger.weights", {}) or {}
    commercial_w = float(weights.get("commercial_acceleration", 0.25))
    upside_w = float(weights.get("upside_capacity", 0.20))
    cash_flow_w = float(weights.get("cash_flow_acceleration", 0.15))
    survival_w = float(weights.get("survival_quality", 0.15))
    governance_w = float(weights.get("governance_event", 0.10))
    market_w = float(weights.get("market_confirmation", 0.10))
    catalyst_w = float(weights.get("catalyst_quality", 0.05))
    risk_w = float(weights.get("risk_penalty", 0.20))

    commercial = clamp(to_float(row.get("commercial_acceleration_score")))
    upside = clamp(to_float(row.get("upside_capacity_score")))
    cash_flow = clamp(to_float(row.get("cash_flow_acceleration_score")))
    survival = clamp(to_float(row.get("survival_quality_score")))
    governance = clamp(to_float(row.get("governance_event_score")))
    market = clamp(to_float(row.get("market_confirmation_score")))
    catalyst = clamp(to_float(row.get("catalyst_quality_score")))
    fragility = clamp(to_float(row.get("commercial_fragility_risk_score")))
    risk = clamp(to_float(row.get("multibagger_risk_penalty")))
    payload = parse_json(row.get("payload_json"))

    positive = (
        commercial_w * commercial
        + upside_w * upside
        + cash_flow_w * cash_flow
        + survival_w * survival
        + governance_w * governance
        + market_w * market
        + catalyst_w * catalyst
    )
    score = round(clamp(positive - risk_w * risk), 4)
    market_payload = payload.get("market", {}) if isinstance(payload, dict) else {}
    avg_dollar_volume = to_float(market_payload.get("avg_dollar_volume_20d"), 0.0)
    min_addv = float(cfg_get(config, "multibagger.min_addv20", 1_000_000))
    evidence = bool(to_int(row.get("evidence_or_catalyst_flag")))
    bucket = bucket_for(score, risk, evidence, avg_dollar_volume >= min_addv, payload, config)
    evidence_json = {
        "component_scores": {
            "commercial_acceleration_score": commercial,
            "upside_capacity_score": upside,
            "cash_flow_acceleration_score": cash_flow,
            "survival_quality_score": survival,
            "governance_event_score": governance,
            "market_confirmation_score": market,
            "catalyst_quality_score": catalyst,
            "commercial_fragility_risk_score": fragility,
            "multibagger_risk_penalty": risk,
        },
        "commercial": payload.get("commercial", {}),
        "survival": payload.get("survival", {}),
        "market": payload.get("market", {}),
        "governance": payload.get("governance", {}),
        "forward_guidance": payload.get("forward_guidance", {}),
        "clinical": {
            "primary_nct": payload.get("clinical", {}).get("primary_nct", "") if isinstance(payload.get("clinical", {}), dict) else "",
            "primary_trial_title": payload.get("clinical", {}).get("primary_trial_title", "") if isinstance(payload.get("clinical", {}), dict) else "",
            "lead_phase2_3_active_trials": payload.get("clinical", {}).get("lead_phase2_3_active_trials", 0) if isinstance(payload.get("clinical", {}), dict) else 0,
            "program_phase2_3_active_trials": payload.get("clinical", {}).get("program_phase2_3_active_trials", 0) if isinstance(payload.get("clinical", {}), dict) else 0,
            "active_pivotal_trials": payload.get("clinical", {}).get("active_pivotal_trials", 0) if isinstance(payload.get("clinical", {}), dict) else 0,
        },
        "risk": payload.get("risk", {}),
        "source_dates": payload.get("source_dates", {}),
        "data_quality": row.get("data_quality", ""),
        "missing_fields": row.get("missing_fields", ""),
    }
    return {
        "asof_date": row["asof_date"],
        "company_id": int(row["company_id"]),
        "ticker": str(row["ticker"] or ""),
        "company_name": str(row["company_name"] or ""),
        "multibagger_score": score,
        "rank": 0,
        "bucket": bucket,
        "top_evidence_json": json.dumps(evidence_json, ensure_ascii=True, sort_keys=True),
    }


def flatten_for_csv(score_row: dict[str, Any], feature_row: dict[str, Any]) -> dict[str, Any]:
    payload = parse_json(feature_row.get("payload_json"))
    commercial = payload.get("commercial", {}) if isinstance(payload, dict) else {}
    survival = payload.get("survival", {}) if isinstance(payload, dict) else {}
    market = payload.get("market", {}) if isinstance(payload, dict) else {}
    governance = payload.get("governance", {}) if isinstance(payload, dict) else {}
    forward = payload.get("forward_guidance", {}) if isinstance(payload, dict) else {}
    clinical = payload.get("clinical", {}) if isinstance(payload, dict) else {}
    return {
        **score_row,
        "commercial_acceleration_score": feature_row.get("commercial_acceleration_score"),
        "upside_capacity_score": feature_row.get("upside_capacity_score"),
        "cash_flow_acceleration_score": feature_row.get("cash_flow_acceleration_score"),
        "survival_quality_score": feature_row.get("survival_quality_score"),
        "governance_event_score": feature_row.get("governance_event_score"),
        "market_confirmation_score": feature_row.get("market_confirmation_score"),
        "catalyst_quality_score": feature_row.get("catalyst_quality_score"),
        "commercial_fragility_risk_score": feature_row.get("commercial_fragility_risk_score"),
        "multibagger_risk_penalty": feature_row.get("multibagger_risk_penalty"),
        "ttm_revenue": commercial.get("ttm_revenue"),
        "revenue_yoy_growth_pct": commercial.get("revenue_yoy_growth_pct"),
        "free_cash_flow_ttm": commercial.get("free_cash_flow_ttm"),
        "fcf_yield": commercial.get("fcf_yield"),
        "market_cap": commercial.get("market_cap"),
        "ev_to_sales": commercial.get("ev_to_sales"),
        "cash_runway_months": survival.get("cash_runway_months"),
        "forward_revenue_midpoint": forward.get("forward_revenue_midpoint"),
        "forward_revenue_growth_pct": forward.get("forward_revenue_growth_pct"),
        "insider_buy_count_90d": governance.get("insider_buy_count_90d"),
        "insider_buy_value_90d": governance.get("insider_buy_value_90d"),
        "buyback_event_count_365d": governance.get("buyback_event_count_365d"),
        "asr_event_count_365d": governance.get("asr_event_count_365d"),
        "relative_strength_3m_vs_xbi": market.get("relative_strength_3m_vs_xbi"),
        "price_vs_200d_pct": market.get("price_vs_200d_pct"),
        "distance_from_52w_high_pct": market.get("distance_from_52w_high_pct"),
        "avg_dollar_volume_20d": market.get("avg_dollar_volume_20d"),
        "primary_nct": clinical.get("primary_nct"),
        "lead_phase2_3_active_trials": clinical.get("lead_phase2_3_active_trials"),
        "program_phase2_3_active_trials": clinical.get("program_phase2_3_active_trials"),
        "active_pivotal_trials": clinical.get("active_pivotal_trials"),
        "evidence_or_catalyst_flag": feature_row.get("evidence_or_catalyst_flag"),
        "data_quality": feature_row.get("data_quality"),
        "missing_fields": feature_row.get("missing_fields"),
    }


def upsert_scores(conn: sqlite3.Connection, rows: list[dict[str, Any]], asof_date: str) -> None:
    now = utc_now()
    placeholders = ", ".join("?" for _ in SCORE_FIELDS)
    update_cols = [field for field in SCORE_FIELDS if field not in {"asof_date", "company_id"}]
    with conn:
        conn.execute("DELETE FROM multibagger_scores_daily WHERE asof_date = ?", (asof_date,))
        for row in rows:
            conn.execute(
                f"""
                INSERT INTO multibagger_scores_daily({", ".join(SCORE_FIELDS)}, created_at, updated_at)
                VALUES ({placeholders}, ?, ?)
                ON CONFLICT(asof_date, company_id) DO UPDATE SET
                    {", ".join(f"{field} = excluded.{field}" for field in update_cols)},
                    updated_at = excluded.updated_at
                """,
                tuple(row.get(field) for field in SCORE_FIELDS) + (now, now),
            )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    configure_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_dir = resolve_path(cfg_get(config, "multibagger.output_dir"), base_dir=base_dir)
    output_csv = output_dir / str(cfg_get(config, "multibagger.scores_csv", "biotech_multibagger_scores.csv"))
    sqlite_timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))

    with connect(db_path, timeout_sec=sqlite_timeout_sec) as conn:
        init_db(conn)
        asof_obj = parse_date(args.asof) if args.asof else None
        if args.asof and asof_obj is None:
            raise ValueError(f"Invalid --asof date: {args.asof}")
        asof_date = asof_obj.isoformat() if asof_obj else latest_feature_date(conn)
        run_id = start_run(conn, run_type="score_multibagger_candidates", input_path=db_path)
        try:
            feature_rows = load_feature_rows(conn, asof_date)
            if not feature_rows:
                raise ValueError(f"No multibagger_features_daily rows found for asof_date={asof_date}")
            feature_by_company = {int(row["company_id"]): row for row in feature_rows}
            scored = [score_one(row, config) for row in feature_rows]
            scored.sort(key=lambda item: (-to_float(item["multibagger_score"]), str(item["ticker"])))
            for idx, row in enumerate(scored, start=1):
                row["rank"] = idx
            upsert_scores(conn, scored, asof_date)
            csv_rows = [flatten_for_csv(row, feature_by_company[int(row["company_id"])]) for row in scored]
            write_csv(output_csv, csv_rows)
            finish_run(conn, run_id=run_id, status="success", row_count=len(scored), message=f"asof={asof_date} output={output_csv}")
        except Exception as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise
    LOGGER.info("Multibagger scoring complete: rows=%d output=%s", len(scored), output_csv)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from statistics import median as statistics_median
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path
from biotech_index.core.db import connect, finish_run, init_db, start_run
from biotech_index.core.logging_utils import configure_utc_logging


LOGGER = logging.getLogger("publish_multibagger_report")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


CANDIDATE_FIELDS = [
    "asof_date",
    "rank",
    "ticker",
    "company_name",
    "bucket",
    "multibagger_score",
    "base_multibagger_score",
    "orthogonal_alpha_score",
    "distinctive_acceleration_score",
    "tier1_opportunity_score",
    "tier1_risk_score",
    "tier1_bucket",
    "tier1_gate_score",
    "tier1_gate_multiplier",
    "tier1_available",
    "tier1_interaction_reason",
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
    "pe_ratio",
    "cash_runway_months",
    "financial_survival_score",
    "forward_revenue_midpoint",
    "forward_revenue_growth_pct",
    "forward_ebitda_midpoint",
    "guidance_score",
    "insider_buy_count_90d",
    "insider_buy_value_90d",
    "insider_buy_cluster_count_90d",
    "buyback_event_count_365d",
    "asr_event_count_365d",
    "governance_risk_score",
    "regulatory_setback_count_365d",
    "adverse_legal_event_count_365d",
    "generic_competition_risk_count_365d",
    "product_concentration_risk_count_365d",
    "relative_strength_3m_vs_xbi",
    "price_vs_200d_pct",
    "distance_from_52w_high_pct",
    "return_3m_pct",
    "avg_dollar_volume_20d",
    "primary_nct",
    "primary_trial_title",
    "lead_phase2_3_active_trials",
    "program_phase2_3_active_trials",
    "active_pivotal_trials",
    "going_concern_status",
    "reverse_split_hits_2y",
    "data_quality",
    "missing_fields",
]


SUMMARY_FIELDS = [
    "asof_date",
    "score_count",
    "candidate_count",
    "top_n",
    "top_n_avg_score",
    "median_score",
    "high_conviction_count",
    "watchlist_count",
    "speculative_count",
    "large_cap_quality_count",
    "avoid_count",
    "avoid_tier1_conflict_count",
    "avoid_tier1_risk_count",
    "tier1_context_count",
    "tier1_missing_count",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish multibagger composite candidate reports.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="Report date in YYYY-MM-DD. Defaults to latest multibagger score date.")
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


def parse_json(raw: object) -> dict[str, Any]:
    try:
        payload = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def latest_score_date(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT MAX(asof_date) AS asof_date FROM multibagger_scores_daily").fetchone()
    asof = str(row["asof_date"] or "") if row else ""
    if not asof:
        raise ValueError("No multibagger_scores_daily rows found. Run 22_score_multibagger_candidates.py first.")
    return asof


def load_rows(conn: sqlite3.Connection, asof_date: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            s.asof_date, s.company_id, s.ticker, s.company_name, s.multibagger_score, s.rank,
            s.base_multibagger_score, s.orthogonal_alpha_score, s.distinctive_acceleration_score,
            s.tier1_opportunity_score, s.tier1_risk_score, s.tier1_bucket, s.tier1_gate_score,
            s.tier1_gate_multiplier, s.tier1_available, s.tier1_interaction_reason,
            s.bucket, s.top_evidence_json,
            f.commercial_acceleration_score, f.upside_capacity_score, f.cash_flow_acceleration_score,
            f.survival_quality_score, f.governance_event_score, f.market_confirmation_score,
            f.catalyst_quality_score, f.commercial_fragility_risk_score, f.multibagger_risk_penalty,
            f.evidence_or_catalyst_flag,
            f.data_quality, f.missing_fields, f.payload_json
        FROM multibagger_scores_daily s
        JOIN multibagger_features_daily f
          ON f.asof_date = s.asof_date AND f.company_id = s.company_id
        WHERE s.asof_date = ?
        ORDER BY s.rank
        """,
        (asof_date,),
    ).fetchall()
    return [dict(row) for row in rows]


def flatten(row: dict[str, Any]) -> dict[str, Any]:
    payload = parse_json(row.get("payload_json"))
    commercial = payload.get("commercial", {}) if isinstance(payload, dict) else {}
    survival = payload.get("survival", {}) if isinstance(payload, dict) else {}
    market = payload.get("market", {}) if isinstance(payload, dict) else {}
    governance = payload.get("governance", {}) if isinstance(payload, dict) else {}
    forward = payload.get("forward_guidance", {}) if isinstance(payload, dict) else {}
    clinical = payload.get("clinical", {}) if isinstance(payload, dict) else {}
    risk = payload.get("risk", {}) if isinstance(payload, dict) else {}
    out = {
        "asof_date": row.get("asof_date"),
        "rank": row.get("rank"),
        "ticker": row.get("ticker"),
        "company_name": row.get("company_name"),
        "bucket": row.get("bucket"),
        "multibagger_score": row.get("multibagger_score"),
        "base_multibagger_score": row.get("base_multibagger_score"),
        "orthogonal_alpha_score": row.get("orthogonal_alpha_score"),
        "distinctive_acceleration_score": row.get("distinctive_acceleration_score"),
        "tier1_opportunity_score": row.get("tier1_opportunity_score"),
        "tier1_risk_score": row.get("tier1_risk_score"),
        "tier1_bucket": row.get("tier1_bucket"),
        "tier1_gate_score": row.get("tier1_gate_score"),
        "tier1_gate_multiplier": row.get("tier1_gate_multiplier"),
        "tier1_available": row.get("tier1_available"),
        "tier1_interaction_reason": row.get("tier1_interaction_reason"),
        "commercial_acceleration_score": row.get("commercial_acceleration_score"),
        "upside_capacity_score": row.get("upside_capacity_score"),
        "cash_flow_acceleration_score": row.get("cash_flow_acceleration_score"),
        "survival_quality_score": row.get("survival_quality_score"),
        "governance_event_score": row.get("governance_event_score"),
        "market_confirmation_score": row.get("market_confirmation_score"),
        "catalyst_quality_score": row.get("catalyst_quality_score"),
        "commercial_fragility_risk_score": row.get("commercial_fragility_risk_score"),
        "multibagger_risk_penalty": row.get("multibagger_risk_penalty"),
        "ttm_revenue": commercial.get("ttm_revenue"),
        "revenue_yoy_growth_pct": commercial.get("revenue_yoy_growth_pct"),
        "free_cash_flow_ttm": commercial.get("free_cash_flow_ttm"),
        "fcf_yield": commercial.get("fcf_yield"),
        "market_cap": commercial.get("market_cap"),
        "ev_to_sales": commercial.get("ev_to_sales"),
        "pe_ratio": commercial.get("pe_ratio"),
        "cash_runway_months": survival.get("cash_runway_months"),
        "financial_survival_score": survival.get("financial_survival_score"),
        "forward_revenue_midpoint": forward.get("forward_revenue_midpoint"),
        "forward_revenue_growth_pct": forward.get("forward_revenue_growth_pct"),
        "forward_ebitda_midpoint": forward.get("forward_ebitda_midpoint"),
        "guidance_score": forward.get("guidance_score"),
        "insider_buy_count_90d": governance.get("insider_buy_count_90d"),
        "insider_buy_value_90d": governance.get("insider_buy_value_90d"),
        "insider_buy_cluster_count_90d": governance.get("insider_buy_cluster_count_90d"),
        "buyback_event_count_365d": governance.get("buyback_event_count_365d"),
        "asr_event_count_365d": governance.get("asr_event_count_365d"),
        "governance_risk_score": governance.get("governance_risk_score"),
        "regulatory_setback_count_365d": governance.get("regulatory_setback_count_365d"),
        "adverse_legal_event_count_365d": governance.get("adverse_legal_event_count_365d"),
        "generic_competition_risk_count_365d": governance.get("generic_competition_risk_count_365d"),
        "product_concentration_risk_count_365d": governance.get("product_concentration_risk_count_365d"),
        "relative_strength_3m_vs_xbi": market.get("relative_strength_3m_vs_xbi"),
        "price_vs_200d_pct": market.get("price_vs_200d_pct"),
        "distance_from_52w_high_pct": market.get("distance_from_52w_high_pct"),
        "return_3m_pct": market.get("return_3m_pct"),
        "avg_dollar_volume_20d": market.get("avg_dollar_volume_20d"),
        "primary_nct": clinical.get("primary_nct"),
        "primary_trial_title": clinical.get("primary_trial_title"),
        "lead_phase2_3_active_trials": clinical.get("lead_phase2_3_active_trials"),
        "program_phase2_3_active_trials": clinical.get("program_phase2_3_active_trials"),
        "active_pivotal_trials": clinical.get("active_pivotal_trials"),
        "going_concern_status": risk.get("going_concern_status"),
        "reverse_split_hits_2y": risk.get("reverse_split_hits_2y"),
        "data_quality": row.get("data_quality"),
        "missing_fields": row.get("missing_fields"),
    }
    missing = [field for field in CANDIDATE_FIELDS if field not in out]
    if missing:
        raise RuntimeError(f"flatten() missing required multibagger report field(s): {', '.join(missing)}")
    return out


def build_summary(rows: list[dict[str, Any]], candidate_rows: list[dict[str, Any]], top_n: int, asof_date: str) -> dict[str, Any]:
    scores = [to_float(row.get("multibagger_score")) for row in rows]
    median_score = statistics_median(scores) if scores else 0.0
    top_scores = [to_float(row.get("multibagger_score")) for row in candidate_rows[:top_n]]
    buckets = [str(row.get("bucket") or "") for row in rows]
    tier1_available = [str(row.get("tier1_available") or "").strip().lower() for row in rows]
    return {
        "asof_date": asof_date,
        "score_count": len(rows),
        "candidate_count": len(candidate_rows),
        "top_n": top_n,
        "top_n_avg_score": round(sum(top_scores) / len(top_scores), 4) if top_scores else 0.0,
        "median_score": round(median_score, 4),
        "high_conviction_count": sum(1 for bucket in buckets if bucket == "high_conviction_multibagger"),
        "watchlist_count": sum(1 for bucket in buckets if bucket == "multibagger_watchlist"),
        "speculative_count": sum(1 for bucket in buckets if bucket == "speculative_multibagger"),
        "large_cap_quality_count": sum(1 for bucket in buckets if bucket == "large_cap_quality"),
        "avoid_count": sum(1 for bucket in buckets if bucket.startswith("avoid")),
        "avoid_tier1_conflict_count": sum(1 for bucket in buckets if bucket == "avoid_tier1_conflict"),
        "avoid_tier1_risk_count": sum(1 for bucket in buckets if bucket == "avoid_tier1_risk"),
        "tier1_context_count": sum(1 for value in tier1_available if value in {"1", "true", "yes"}),
        "tier1_missing_count": sum(1 for value in tier1_available if value not in {"1", "true", "yes"}),
    }


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")


def main() -> None:
    configure_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_dir = resolve_path(cfg_get(config, "multibagger.output_dir"), base_dir=base_dir)
    candidates_csv = output_dir / str(cfg_get(config, "multibagger.candidates_csv", "biotech_multibagger_candidates.csv"))
    summary_csv = output_dir / str(cfg_get(config, "multibagger.summary_csv", "biotech_multibagger_summary.csv"))
    evidence_json = output_dir / str(cfg_get(config, "multibagger.evidence_cards_json", "biotech_multibagger_evidence_cards.json"))
    top_n = int(cfg_get(config, "multibagger.top_n", 50))
    sqlite_timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))

    with connect(db_path, timeout_sec=sqlite_timeout_sec) as conn:
        run_id: int | None = None
        init_db(conn)
        asof_obj = parse_date(args.asof) if args.asof else None
        if args.asof and asof_obj is None:
            raise ValueError(f"Invalid --asof date: {args.asof}")
        asof_date = asof_obj.isoformat() if asof_obj else latest_score_date(conn)
        run_id = start_run(conn, run_type="publish_multibagger_report", input_path=db_path)
        try:
            rows = load_rows(conn, asof_date)
            if not rows:
                raise ValueError(f"No multibagger score rows found for asof_date={asof_date}")
            rows_with_bucket = [row for row in rows if row.get("bucket")]
            flattened = [flatten(row) for row in rows_with_bucket]
            all_candidate_rows = [
                row for row in flattened if row.get("bucket") and not str(row.get("bucket")).startswith("avoid")
            ]
            candidate_rows = all_candidate_rows[:top_n]
            evidence_by_ticker = {
                str(raw.get("ticker") or ""): parse_json(raw.get("top_evidence_json"))
                for raw in rows
            }
            evidence_cards = [
                {
                    "asof_date": row["asof_date"],
                    "rank": row["rank"],
                    "ticker": row["ticker"],
                    "company_name": row["company_name"],
                    "bucket": row["bucket"],
                    "multibagger_score": row["multibagger_score"],
                    "tier1_bucket": row.get("tier1_bucket", ""),
                    "tier1_gate_score": row.get("tier1_gate_score", ""),
                    "tier1_gate_multiplier": row.get("tier1_gate_multiplier", ""),
                    "tier1_interaction_reason": row.get("tier1_interaction_reason", ""),
                    "evidence": evidence_by_ticker.get(str(row["ticker"] or ""), {}),
                }
                for row in candidate_rows
            ]
            summary = build_summary(flattened, all_candidate_rows, top_n, asof_date)
            write_csv(candidates_csv, candidate_rows, CANDIDATE_FIELDS)
            write_csv(summary_csv, [summary], SUMMARY_FIELDS)
            write_json(evidence_json, evidence_cards)
            LOGGER.info("Multibagger report published: candidates=%d output=%s", len(candidate_rows), candidates_csv)
            finish_run(conn, run_id=run_id, status="success", row_count=len(candidate_rows), message=f"asof={asof_date} output={candidates_csv}")
        except BaseException as exc:
            if run_id is not None and not (isinstance(exc, SystemExit) and exc.code in (0, None)):
                finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()

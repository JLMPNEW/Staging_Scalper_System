#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_optional_path, resolve_path  # noqa: E402
from biotech_index.core.db import connect, finish_run, init_db, start_run  # noqa: E402
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402


LOGGER = logging.getLogger("publish_biotech_reports")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"

TOP_SCORE_FIELDS = [
    "asof_date",
    "rank",
    "ticker",
    "company_name",
    "bucket",
    "opportunity_score",
    "investment_score",
    "investment_profile",
    "clinical_opportunity_score",
    "tier1_selection_gate_score",
    "tier1_primary_horizon_trading_days",
    "tier1_production_score_model",
    "tier1_selection_policy",
    "alpha_multibagger_role",
    "core_structural_veto_flag",
    "core_structural_veto_reasons",
    "data_quality_confidence_multiplier",
    "clinical_risk_drag",
    "investment_risk_drag",
    "commercial_value_score",
    "forward_guidance_score",
    "valuation_score",
    "upside_capacity_score",
    "catalyst_score",
    "credibility_score",
    "financial_quality_score",
    "risk_score",
    "momentum_score",
    "primary_nct",
    "primary_trial_title",
    "lead_phase2_3_active_trials",
    "program_phase2_3_active_trials",
    "collaborator_phase2_3_active_trials",
    "effective_phase2_3_trials",
    "core_pipeline_quality_score",
    "collaborator_dependency_ratio",
    "collaborator_heavy_flag",
    "going_concern_status",
    "reverse_split_hits_2y",
    "median_addv20",
    "cash_runway_months",
    "financial_survival_score",
    "ttm_revenue",
    "revenue_yoy_growth_pct",
    "gross_margin_pct",
    "net_margin_pct",
    "market_cap",
    "ev_to_sales",
    "pe_ratio",
    "commercial_stage_flag",
    "profitable_flag",
    "latest_guidance_filing_date",
    "forward_revenue_midpoint",
    "forward_revenue_growth_pct",
    "forward_ebitda_midpoint",
    "forward_ebitda_margin_pct",
    "guidance_confidence",
    "forward_guidance_data_quality",
    "forward_guidance_source_type",
    "forward_guidance_source_name",
    "forward_guidance_source_url",
    "forward_guidance_override_reason",
    "financial_data_quality",
    "sec_regulatory_catalyst_count",
    "sec_dilution_event_count",
    "sec_negative_clinical_event_count",
    "industry",
    "industry_aggregate",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish Tier-1 biotech index reports.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="Report date in YYYY-MM-DD. Defaults to latest score date.")
    return parser.parse_args()


def configure_logging() -> None:
    configure_utc_logging()


def parse_date_text(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError(f"Invalid date: {text}") from exc


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


def latest_score_date(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT MAX(asof_date) AS asof_date FROM daily_scores").fetchone()
    asof = str(row["asof_date"] or "") if row else ""
    if not asof:
        raise ValueError("No daily_scores rows found. Run 11_score_biotech_index.py first.")
    return asof


def previous_score_date(conn: sqlite3.Connection, asof_date: str) -> str:
    row = conn.execute(
        "SELECT MAX(asof_date) AS asof_date FROM daily_scores WHERE asof_date < ?",
        (asof_date,),
    ).fetchone()
    return str(row["asof_date"] or "") if row else ""


def load_scores(conn: sqlite3.Connection, asof_date: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            s.asof_date, s.company_id, s.catalyst_score, s.credibility_score,
            s.financial_quality_score, s.risk_score, s.momentum_score,
            s.clinical_opportunity_score, s.commercial_value_score, s.valuation_score,
            s.forward_guidance_score, s.upside_capacity_score, s.investment_score, s.opportunity_score,
            s.tier1_selection_gate_score, s.data_quality_confidence_multiplier,
            s.clinical_risk_drag, s.investment_risk_drag,
            s.rank, s.bucket, s.top_evidence_json,
            c.ticker, c.company_name, c.exchange, c.industry, c.industry_aggregate
        FROM daily_scores s
        JOIN companies c ON c.company_id = s.company_id
        WHERE s.asof_date = ?
        ORDER BY s.rank
        """,
        (asof_date,),
    ).fetchall()
    return [dict(row) for row in rows]


def load_features(conn: sqlite3.Connection, asof_date: str) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT company_id, catalyst_score_raw, credibility_score_raw, risk_score_raw, feature_json
        FROM daily_features
        WHERE asof_date = ?
        """,
        (asof_date,),
    ).fetchall()
    return {int(row["company_id"]): dict(row) for row in rows}


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")


def read_csv_rows(path: Path, *, required: bool = False) -> list[dict[str, str]]:
    if not path.exists():
        if required:
            raise FileNotFoundError(f"Required CSV not found: {path}")
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [{str(k): str(v or "") for k, v in row.items()} for row in reader]


def apply_trial_status_overrides(rows: list[dict[str, str]], overrides: list[dict[str, str]]) -> list[dict[str, str]]:
    if not rows or not overrides:
        return rows
    out = [dict(row) for row in rows]
    for row in out:
        row.setdefault("outcome_override_applied", "")
        row.setdefault("outcome_override_status", "")
        row.setdefault("outcome_override_reason", "")
        row.setdefault("outcome_override_source_url", "")
        row.setdefault("outcome_override_manual_review", "")

    override_index = {
        (str(override.get("ticker") or "").strip().upper(), str(override.get("nct_id") or "").strip().upper()): override
        for override in overrides
        if bool_text(override.get("enabled", "true"))
        and str(override.get("ticker") or "").strip()
        and str(override.get("nct_id") or "").strip()
    }
    for row in out:
        override = override_index.get(
            (str(row.get("ticker") or "").strip().upper(), str(row.get("nct_id") or "").strip().upper())
        )
        if not override:
            continue
        status = str(override.get("override_status") or "").strip()
        reason = str(override.get("override_reason") or "").strip()
        source_url = str(override.get("source_url") or "").strip()
        row["outcome_override_applied"] = "True"
        row["outcome_override_status"] = status
        row["outcome_override_reason"] = reason
        row["outcome_override_source_url"] = source_url
        row["outcome_override_manual_review"] = "True" if bool_text(override.get("manual_review")) else "False"
        if bool_text(override.get("exclude_from_scoring")):
            row["is_active_status"] = "False"
            row["qualifying_trial"] = "False"
            row["trial_score"] = "0.0"
            suffix = f"outcome_override:{status}" if status else "outcome_override"
            row["exclusion_reasons"] = ";".join(part for part in [str(row.get("exclusion_reasons") or ""), suffix] if part)
    return out


def assert_output_paths_writable(paths: list[Path]) -> None:
    locked: list[str] = []
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("a", encoding="utf-8"):
                pass
        except PermissionError:
            locked.append(str(path))
    if locked:
        raise PermissionError("Report output file is not writable. Close the file and rerun: " + "; ".join(locked))


def build_index_summary(scores: list[dict[str, Any]], asof_date: str, top_n: int) -> dict[str, Any]:
    values = [to_float(row.get("opportunity_score")) for row in scores]
    top_values = values[:top_n]
    top_n_avg_score = round(sum(top_values) / len(top_values), 4) if top_values else 0.0
    median_score = round(median(values), 4) if values else 0.0
    full_universe_avg_score = round(sum(values) / len(values), 4) if values else 0.0
    # Blend leader strength with universe breadth so the index is not just an alias for top-N average.
    index_level = round((0.70 * top_n_avg_score) + (0.20 * full_universe_avg_score) + (0.10 * median_score), 4)
    bucket_counts: dict[str, int] = {}
    for row in scores:
        bucket = str(row.get("bucket") or "unknown")
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
    return {
        "asof_date": asof_date,
        "company_count": len(scores),
        "top_n": top_n,
        "biotech_opportunity_index_level": index_level,
        "top_n_avg_score": top_n_avg_score,
        "full_universe_avg_score": full_universe_avg_score,
        "median_score": median_score,
        "max_score": round(max(values), 4) if values else 0.0,
        "high_conviction_count": bucket_counts.get("high_conviction", 0),
        "watchlist_count": bucket_counts.get("watchlist", 0),
        "speculative_count": bucket_counts.get("speculative", 0),
        "avoid_count": bucket_counts.get("avoid", 0),
        "index_method": "0.70*top_n_avg_score+0.20*full_universe_avg_score+0.10*median_score",
    }


def parse_json_object(raw: object, *, context: str, ticker: object = "") -> dict[str, Any]:
    try:
        payload = json.loads(str(raw or "{}"))
    except json.JSONDecodeError as exc:
        LOGGER.error("Malformed JSON in %s for ticker=%s: %s", context, ticker, exc)
        return {}
    return payload if isinstance(payload, dict) else {}


def flatten_score_row(row: dict[str, Any]) -> dict[str, Any]:
    evidence = parse_json_object(row.get("top_evidence_json"), context="top_evidence_json", ticker=row.get("ticker"))
    risk_flags = evidence.get("risk_flags", {}) if isinstance(evidence, dict) else {}
    sec_events = evidence.get("sec_events", {}) if isinstance(evidence, dict) else {}
    ctgov_quality = evidence.get("ctgov_quality", {}) if isinstance(evidence, dict) else {}
    commercial_value = evidence.get("commercial_value", {}) if isinstance(evidence, dict) else {}
    forward_guidance = evidence.get("forward_guidance", {}) if isinstance(evidence, dict) else {}
    guidance_records = forward_guidance.get("guidance_records", []) if isinstance(forward_guidance, dict) else []
    if not isinstance(guidance_records, list):
        guidance_records = []
    primary_guidance = next((item for item in guidance_records if isinstance(item, dict) and item.get("metric") == "revenue"), None)
    if primary_guidance is None:
        primary_guidance = next((item for item in guidance_records if isinstance(item, dict)), {})
    score_components = evidence.get("score_components", {}) if isinstance(evidence, dict) else {}
    core_veto = evidence.get("core_structural_veto", {}) if isinstance(evidence, dict) else {}
    production_baseline = evidence.get("production_baseline", {}) if isinstance(evidence, dict) else {}
    core_veto_flag = core_veto.get("flag", risk_flags.get("core_structural_veto_flag", "")) if isinstance(core_veto, dict) else ""
    core_veto_reasons = core_veto.get("reasons", risk_flags.get("core_structural_veto_reasons", "")) if isinstance(core_veto, dict) else ""
    if isinstance(core_veto_reasons, list):
        core_veto_reasons = "|".join(str(reason) for reason in core_veto_reasons)
    return {
        "asof_date": row.get("asof_date", ""),
        "rank": row.get("rank", ""),
        "ticker": row.get("ticker", ""),
        "company_name": row.get("company_name", ""),
        "bucket": row.get("bucket", ""),
        "opportunity_score": row.get("opportunity_score", ""),
        "investment_score": row.get("investment_score", score_components.get("investment_score", "") if isinstance(score_components, dict) else ""),
        "investment_profile": score_components.get("investment_profile", "") if isinstance(score_components, dict) else "",
        "clinical_opportunity_score": row.get("clinical_opportunity_score", score_components.get("clinical_opportunity_score", "") if isinstance(score_components, dict) else ""),
        "tier1_selection_gate_score": row.get("tier1_selection_gate_score", score_components.get("tier1_selection_gate_score", "") if isinstance(score_components, dict) else ""),
        "tier1_primary_horizon_trading_days": production_baseline.get("primary_horizon_trading_days", score_components.get("primary_horizon_trading_days", "") if isinstance(score_components, dict) else "") if isinstance(production_baseline, dict) else "",
        "tier1_production_score_model": production_baseline.get("score_model", score_components.get("production_baseline_score_model", "") if isinstance(score_components, dict) else "") if isinstance(production_baseline, dict) else "",
        "tier1_selection_policy": production_baseline.get("selection_policy", score_components.get("selection_policy", "") if isinstance(score_components, dict) else "") if isinstance(production_baseline, dict) else "",
        "alpha_multibagger_role": production_baseline.get("alpha_multibagger_role", score_components.get("alpha_multibagger_role", "") if isinstance(score_components, dict) else "") if isinstance(production_baseline, dict) else "",
        "core_structural_veto_flag": core_veto_flag,
        "core_structural_veto_reasons": core_veto_reasons,
        "data_quality_confidence_multiplier": row.get("data_quality_confidence_multiplier", score_components.get("data_quality_confidence_multiplier", "") if isinstance(score_components, dict) else ""),
        "clinical_risk_drag": row.get("clinical_risk_drag", score_components.get("clinical_risk_drag", "") if isinstance(score_components, dict) else ""),
        "investment_risk_drag": row.get("investment_risk_drag", score_components.get("investment_risk_drag", "") if isinstance(score_components, dict) else ""),
        "commercial_value_score": row.get("commercial_value_score", commercial_value.get("commercial_value_score", "") if isinstance(commercial_value, dict) else ""),
        "forward_guidance_score": row.get("forward_guidance_score", score_components.get("forward_guidance_score", "") if isinstance(score_components, dict) else ""),
        "valuation_score": row.get("valuation_score", commercial_value.get("valuation_score", "") if isinstance(commercial_value, dict) else ""),
        "upside_capacity_score": row.get("upside_capacity_score", commercial_value.get("upside_capacity_score", "") if isinstance(commercial_value, dict) else ""),
        "catalyst_score": row.get("catalyst_score", ""),
        "credibility_score": row.get("credibility_score", ""),
        "financial_quality_score": row.get("financial_quality_score", ""),
        "risk_score": row.get("risk_score", ""),
        "momentum_score": row.get("momentum_score", ""),
        "primary_nct": evidence.get("primary_nct", "") if isinstance(evidence, dict) else "",
        "primary_trial_title": evidence.get("primary_trial_title", "") if isinstance(evidence, dict) else "",
        "lead_phase2_3_active_trials": ctgov_quality.get("lead_phase2_3_active_trials", "") if isinstance(ctgov_quality, dict) else "",
        "program_phase2_3_active_trials": ctgov_quality.get("program_phase2_3_active_trials", "") if isinstance(ctgov_quality, dict) else "",
        "collaborator_phase2_3_active_trials": ctgov_quality.get("collaborator_phase2_3_active_trials", "") if isinstance(ctgov_quality, dict) else "",
        "effective_phase2_3_trials": ctgov_quality.get("effective_phase2_3_trials", "") if isinstance(ctgov_quality, dict) else "",
        "core_pipeline_quality_score": ctgov_quality.get("core_pipeline_quality_score", "") if isinstance(ctgov_quality, dict) else "",
        "collaborator_dependency_ratio": ctgov_quality.get("collaborator_dependency_ratio", "") if isinstance(ctgov_quality, dict) else "",
        "collaborator_heavy_flag": ctgov_quality.get("collaborator_heavy_flag", "") if isinstance(ctgov_quality, dict) else "",
        "going_concern_status": risk_flags.get("going_concern_status", "") if isinstance(risk_flags, dict) else "",
        "reverse_split_hits_2y": risk_flags.get("reverse_split_hits_2y", "") if isinstance(risk_flags, dict) else "",
        "median_addv20": risk_flags.get("median_addv20", "") if isinstance(risk_flags, dict) else "",
        "cash_runway_months": risk_flags.get("cash_runway_months", "") if isinstance(risk_flags, dict) else "",
        "financial_survival_score": risk_flags.get("financial_survival_score", "") if isinstance(risk_flags, dict) else "",
        "ttm_revenue": commercial_value.get("ttm_revenue", "") if isinstance(commercial_value, dict) else "",
        "revenue_yoy_growth_pct": commercial_value.get("revenue_yoy_growth_pct", "") if isinstance(commercial_value, dict) else "",
        "gross_margin_pct": commercial_value.get("gross_margin_pct", "") if isinstance(commercial_value, dict) else "",
        "net_margin_pct": commercial_value.get("net_margin_pct", "") if isinstance(commercial_value, dict) else "",
        "market_cap": commercial_value.get("market_cap", "") if isinstance(commercial_value, dict) else "",
        "ev_to_sales": commercial_value.get("ev_to_sales", "") if isinstance(commercial_value, dict) else "",
        "pe_ratio": commercial_value.get("pe_ratio", "") if isinstance(commercial_value, dict) else "",
        "commercial_stage_flag": commercial_value.get("commercial_stage_flag", "") if isinstance(commercial_value, dict) else "",
        "profitable_flag": commercial_value.get("profitable_flag", "") if isinstance(commercial_value, dict) else "",
        "latest_guidance_filing_date": forward_guidance.get("latest_guidance_filing_date", "") if isinstance(forward_guidance, dict) else "",
        "forward_revenue_midpoint": forward_guidance.get("forward_revenue_midpoint", "") if isinstance(forward_guidance, dict) else "",
        "forward_revenue_growth_pct": forward_guidance.get("forward_revenue_growth_pct", "") if isinstance(forward_guidance, dict) else "",
        "forward_ebitda_midpoint": forward_guidance.get("forward_ebitda_midpoint", "") if isinstance(forward_guidance, dict) else "",
        "forward_ebitda_margin_pct": forward_guidance.get("forward_ebitda_margin_pct", "") if isinstance(forward_guidance, dict) else "",
        "guidance_confidence": forward_guidance.get("guidance_confidence", "") if isinstance(forward_guidance, dict) else "",
        "forward_guidance_data_quality": forward_guidance.get("data_quality", "") if isinstance(forward_guidance, dict) else "",
        "forward_guidance_source_type": primary_guidance.get("source_type", "") if isinstance(primary_guidance, dict) else "",
        "forward_guidance_source_name": primary_guidance.get("source_name", "") if isinstance(primary_guidance, dict) else "",
        "forward_guidance_source_url": primary_guidance.get("source_url", "") if isinstance(primary_guidance, dict) else "",
        "forward_guidance_override_reason": primary_guidance.get("override_reason", "") if isinstance(primary_guidance, dict) else "",
        "financial_data_quality": risk_flags.get("financial_data_quality", "") if isinstance(risk_flags, dict) else "",
        "sec_regulatory_catalyst_count": sec_events.get("regulatory_catalyst_count", "") if isinstance(sec_events, dict) else "",
        "sec_dilution_event_count": sec_events.get("dilution_event_count", "") if isinstance(sec_events, dict) else "",
        "sec_negative_clinical_event_count": sec_events.get("negative_clinical_event_count", "") if isinstance(sec_events, dict) else "",
        "industry": row.get("industry", ""),
        "industry_aggregate": row.get("industry_aggregate", ""),
    }


def load_previous_scores(conn: sqlite3.Connection, prev_asof: str) -> dict[int, dict[str, Any]]:
    if not prev_asof:
        return {}
    rows = conn.execute(
        """
        SELECT company_id, opportunity_score, rank, bucket
        FROM daily_scores
        WHERE asof_date = ?
        """,
        (prev_asof,),
    ).fetchall()
    return {int(row["company_id"]): dict(row) for row in rows}


def build_alerts(
    *,
    current_scores: list[dict[str, Any]],
    previous_scores: dict[int, dict[str, Any]],
    prev_asof: str,
    score_change_min: float,
    top_n: int,
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    strong_buckets = {"high_conviction", "watchlist"}
    if not previous_scores:
        for row in current_scores[:top_n]:
            alerts.append(
                {
                    "asof_date": row.get("asof_date", ""),
                    "ticker": row.get("ticker", ""),
                    "company_name": row.get("company_name", ""),
                    "alert_type": "initial_top_candidate",
                    "current_score": row.get("opportunity_score", ""),
                    "previous_score": "",
                    "score_change": "",
                    "current_rank": row.get("rank", ""),
                    "previous_rank": "",
                    "current_bucket": row.get("bucket", ""),
                    "previous_bucket": "",
                    "previous_asof_date": "",
                }
            )
        return alerts

    for row in current_scores:
        company_id = int(row["company_id"])
        prev = previous_scores.get(company_id)
        if not prev:
            if int(row.get("rank") or 999999) <= top_n:
                alert_type = "new_top_candidate"
            else:
                continue
            prev_score = ""
            score_change = ""
            prev_rank = ""
            prev_bucket = ""
        else:
            current_score = to_float(row.get("opportunity_score"))
            prev_score_value = to_float(prev.get("opportunity_score"))
            delta = current_score - prev_score_value
            current_bucket = str(row.get("bucket") or "")
            prev_bucket_value = str(prev.get("bucket") or "")
            if delta >= score_change_min:
                alert_type = "score_jump"
            elif current_bucket in strong_buckets and current_bucket != prev_bucket_value:
                alert_type = "bucket_upgrade"
            elif int(row.get("rank") or 999999) <= top_n and int(prev.get("rank") or 999999) > top_n:
                alert_type = "entered_top_n"
            else:
                continue
            prev_score = round(prev_score_value, 4)
            score_change = round(delta, 4)
            prev_rank = prev.get("rank", "")
            prev_bucket = prev_bucket_value
        alerts.append(
            {
                "asof_date": row.get("asof_date", ""),
                "ticker": row.get("ticker", ""),
                "company_name": row.get("company_name", ""),
                "alert_type": alert_type,
                "current_score": row.get("opportunity_score", ""),
                "previous_score": prev_score,
                "score_change": score_change,
                "current_rank": row.get("rank", ""),
                "previous_rank": prev_rank,
                "current_bucket": row.get("bucket", ""),
                "previous_bucket": prev_bucket,
                "previous_asof_date": prev_asof,
            }
        )
    return alerts


def build_trial_validation_rows(
    *,
    scores: list[dict[str, Any]],
    evidence_rows: list[dict[str, str]],
    top_n: int,
    extra_tickers: list[str],
    max_trials_per_ticker: int,
) -> list[dict[str, Any]]:
    score_by_ticker = {str(row.get("ticker") or "").upper(): row for row in scores}
    selected = {str(row.get("ticker") or "").upper() for row in scores[:top_n]}
    selected.update(str(ticker or "").strip().upper() for ticker in extra_tickers if str(ticker or "").strip())
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in evidence_rows:
        ticker = str(row.get("ticker") or "").upper()
        if ticker in selected:
            grouped.setdefault(ticker, []).append(row)

    out: list[dict[str, Any]] = []
    for ticker in sorted(grouped, key=lambda value: int(score_by_ticker.get(value, {}).get("rank") or 999999)):
        score_row = score_by_ticker.get(ticker, {})
        rows = grouped[ticker]
        rows.sort(
            key=lambda row: (
                1 if str(row.get("is_active_status") or "").lower() == "true" else 0,
                1 if str(row.get("qualifying_trial") or "").lower() == "true" else 0,
                1 if "lead" in str(row.get("match_roles") or "").split(";") else 0,
                to_float(row.get("phase_rank")),
                to_float(row.get("trial_score")),
                str(row.get("last_update_post_date") or ""),
            ),
            reverse=True,
        )
        for row in rows[:max(1, max_trials_per_ticker)]:
            out.append(
                {
                    "asof_date": score_row.get("asof_date", ""),
                    "rank": score_row.get("rank", ""),
                    "ticker": ticker,
                    "company_name": score_row.get("company_name", row.get("company_name", "")),
                    "opportunity_score": score_row.get("opportunity_score", ""),
                    "nct_id": row.get("nct_id", ""),
                    "brief_title": row.get("brief_title", ""),
                    "overall_status": row.get("overall_status", ""),
                    "phase_text": row.get("phase_text", ""),
                    "phase_rank": row.get("phase_rank", ""),
                    "primary_purpose": row.get("primary_purpose", ""),
                    "match_roles": row.get("match_roles", ""),
                    "match_methods": row.get("match_methods", ""),
                    "strong_company_link": row.get("strong_company_link", ""),
                    "max_confidence": row.get("max_confidence", ""),
                    "is_active_status": row.get("is_active_status", ""),
                    "is_pivotal": row.get("is_pivotal", ""),
                    "qualifying_trial": row.get("qualifying_trial", ""),
                    "trial_score": row.get("trial_score", ""),
                    "days_since_last_update": row.get("days_since_last_update", ""),
                    "last_update_post_date": row.get("last_update_post_date", ""),
                    "primary_completion_date": row.get("primary_completion_date", ""),
                    "intervention_types": row.get("intervention_types", ""),
                    "intervention_names": row.get("intervention_names", ""),
                    "exclusion_reasons": row.get("exclusion_reasons", ""),
                    "outcome_override_applied": row.get("outcome_override_applied", ""),
                    "outcome_override_status": row.get("outcome_override_status", ""),
                    "outcome_override_reason": row.get("outcome_override_reason", ""),
                    "outcome_override_source_url": row.get("outcome_override_source_url", ""),
                    "outcome_override_manual_review": row.get("outcome_override_manual_review", ""),
                    "sponsors": row.get("sponsors", ""),
                }
            )
    return out


def bool_text(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "y"}


def split_roles(raw: object) -> set[str]:
    return {part.strip().lower() for part in str(raw or "").split(";") if part.strip()}


def days_since_update(row: dict[str, Any]) -> int:
    return int(to_float(row.get("days_since_last_update"), 999999.0))


def is_terminal_status(row: dict[str, Any]) -> bool:
    return str(row.get("overall_status") or "").strip().upper() in {"COMPLETED", "TERMINATED", "WITHDRAWN", "SUSPENDED"}


def build_trial_validation_summary_rows(trial_rows: list[dict[str, Any]], validation_cap: int) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in trial_rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        if ticker:
            grouped.setdefault(ticker, []).append(row)

    out: list[dict[str, Any]] = []
    for ticker, rows in grouped.items():
        first = rows[0]
        active = [row for row in rows if bool_text(row.get("is_active_status"))]
        active_qualifying = [row for row in active if bool_text(row.get("qualifying_trial"))]
        active_phase2_3 = [row for row in active_qualifying if int(to_float(row.get("phase_rank"))) in {2, 3}]
        fresh_active_phase2_3 = [row for row in active_phase2_3 if days_since_update(row) <= 365]
        stale_active_phase2_3 = [row for row in active_phase2_3 if days_since_update(row) > 365]
        lead_active_qualifying = [row for row in active_qualifying if "lead" in split_roles(row.get("match_roles"))]
        lead_active_phase2_3 = [row for row in active_phase2_3 if "lead" in split_roles(row.get("match_roles"))]
        program_active_qualifying = [row for row in active_qualifying if "program" in split_roles(row.get("match_roles"))]
        program_active_phase2_3 = [row for row in active_phase2_3 if "program" in split_roles(row.get("match_roles"))]
        collab_only_active_qualifying = [
            row
            for row in active_qualifying
            if "collaborator" in split_roles(row.get("match_roles"))
            and "lead" not in split_roles(row.get("match_roles"))
            and "program" not in split_roles(row.get("match_roles"))
        ]
        collab_only_active_phase2_3 = [
            row
            for row in active_phase2_3
            if "collaborator" in split_roles(row.get("match_roles"))
            and "lead" not in split_roles(row.get("match_roles"))
            and "program" not in split_roles(row.get("match_roles"))
        ]
        pivotal_active_qualifying = [row for row in active_qualifying if bool_text(row.get("is_pivotal"))]
        lead_or_program_pivotal_active = [
            row
            for row in pivotal_active_qualifying
            if {"lead", "program"} & split_roles(row.get("match_roles"))
        ]
        weak_link_rows = [row for row in rows if not bool_text(row.get("strong_company_link"))]
        stale_active = [row for row in active if days_since_update(row) > 365]
        non_qualifying = [row for row in rows if not bool_text(row.get("qualifying_trial"))]
        terminal = [row for row in rows if is_terminal_status(row)]
        outcome_overridden = [row for row in rows if bool_text(row.get("outcome_override_applied"))]
        outcome_excluded = [
            row
            for row in outcome_overridden
            if any(
                part == "outcome_override" or part.startswith("outcome_override:")
                for part in str(row.get("exclusion_reasons") or "").split(";")
            )
        ]
        outcome_review = [row for row in outcome_overridden if bool_text(row.get("outcome_override_manual_review"))]

        review_flags: list[str] = []
        if outcome_excluded:
            review_flags.append("outcome_override_excluded")
        if outcome_review:
            review_flags.append("outcome_override_review")
        if weak_link_rows:
            review_flags.append("weak_links")
        if stale_active_phase2_3:
            review_flags.append("stale_active_phase2_3")
        if len(collab_only_active_phase2_3) > (len(lead_active_phase2_3) + len(program_active_phase2_3)):
            review_flags.append("collaborator_heavy_phase2_3")
        if non_qualifying:
            review_flags.append("non_qualifying_rows")
        if terminal:
            review_flags.append("terminal_rows")

        out.append(
            {
                "asof_date": first.get("asof_date", ""),
                "rank": first.get("rank", ""),
                "ticker": ticker,
                "company_name": first.get("company_name", ""),
                "opportunity_score": first.get("opportunity_score", ""),
                "rows_in_validation_csv": len(rows),
                "validation_cap_reached": len(rows) >= max(1, validation_cap),
                "needs_manual_review": bool(review_flags),
                "active_trials": len(active),
                "active_qualifying_trials": len(active_qualifying),
                "active_phase2_3_trials": len(active_phase2_3),
                "fresh_active_phase2_3_trials": len(fresh_active_phase2_3),
                "stale_active_phase2_3_trials": len(stale_active_phase2_3),
                "lead_active_qualifying_trials": len(lead_active_qualifying),
                "lead_active_phase2_3_trials": len(lead_active_phase2_3),
                "program_active_qualifying_trials": len(program_active_qualifying),
                "program_active_phase2_3_trials": len(program_active_phase2_3),
                "collab_only_active_qualifying_trials": len(collab_only_active_qualifying),
                "collab_only_active_phase2_3_trials": len(collab_only_active_phase2_3),
                "pivotal_active_qualifying_trials": len(pivotal_active_qualifying),
                "lead_or_program_pivotal_active_trials": len(lead_or_program_pivotal_active),
                "weak_link_rows": len(weak_link_rows),
                "stale_active_trials": len(stale_active),
                "non_qualifying_rows": len(non_qualifying),
                "terminal_rows": len(terminal),
                "outcome_override_rows": len(outcome_overridden),
                "outcome_override_excluded_rows": len(outcome_excluded),
                "outcome_override_review_rows": len(outcome_review),
                "review_flags": ";".join(review_flags),
            }
        )
    out.sort(key=lambda row: int(to_float(row.get("rank"), 999999.0)))
    return out


def build_evidence_cards(scores: list[dict[str, Any]], features: dict[int, dict[str, Any]], top_n: int) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for row in scores[:top_n]:
        company_id = int(row["company_id"])
        evidence = parse_json_object(row.get("top_evidence_json"), context="top_evidence_json", ticker=row.get("ticker"))
        feature_payload = parse_json_object(
            features.get(company_id, {}).get("feature_json"),
            context="feature_json",
            ticker=row.get("ticker"),
        )
        cards.append(
            {
                "asof_date": row.get("asof_date", ""),
                "rank": row.get("rank", ""),
                "ticker": row.get("ticker", ""),
                "company_name": row.get("company_name", ""),
                "bucket": row.get("bucket", ""),
                "opportunity_score": row.get("opportunity_score", ""),
                "scores": {
                    "catalyst": row.get("catalyst_score", ""),
                    "credibility": row.get("credibility_score", ""),
                    "financial_quality": row.get("financial_quality_score", ""),
                    "risk": row.get("risk_score", ""),
                    "momentum": row.get("momentum_score", ""),
                },
                "top_evidence": evidence,
                "feature_detail": feature_payload,
            }
        )
    return cards


def main() -> None:
    configure_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_dir = resolve_path(cfg_get(config, "biotech_reports.output_dir", "../output/biotech_index_reports"), base_dir=base_dir)
    top_n = int(cfg_get(config, "biotech_reports.top_n", 20))
    score_change_min = float(cfg_get(config, "biotech_reports.alerts_score_change_min", 12))
    index_csv = output_dir / str(cfg_get(config, "biotech_reports.index_latest_csv", "biotech_index_latest.csv"))
    top_csv = output_dir / str(cfg_get(config, "biotech_reports.top_candidates_csv", "biotech_top_candidates.csv"))
    alerts_csv = output_dir / str(cfg_get(config, "biotech_reports.alerts_csv", "biotech_alerts.csv"))
    evidence_json = output_dir / str(cfg_get(config, "biotech_reports.evidence_cards_json", "biotech_evidence_cards.json"))
    trial_validation_csv = output_dir / str(cfg_get(config, "biotech_reports.trial_validation_csv", "biotech_top_trial_validation.csv"))
    trial_validation_summary_csv = output_dir / str(cfg_get(config, "biotech_reports.trial_validation_summary_csv", "biotech_top_trial_validation_summary.csv"))
    ctgov_evidence_csv = resolve_path(cfg_get(config, "biotech_features.ctgov_evidence_csv", "../output/biotech_index_reports/ctgov_trial_evidence.csv"), base_dir=base_dir)
    trial_status_overrides_csv = resolve_optional_path(cfg_get(config, "ctgov_audit.trial_status_overrides_csv"), base_dir=base_dir)
    validation_extra_tickers = [str(x).upper() for x in (cfg_get(config, "biotech_reports.trial_validation_extra_tickers", []) or [])]
    validation_max_trials = int(cfg_get(config, "biotech_reports.trial_validation_max_trials_per_ticker", 25))
    sqlite_timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))

    with connect(db_path, timeout_sec=sqlite_timeout_sec) as conn:
        run_id: int | None = None
        init_db(conn)
        asof_date = parse_date_text(args.asof) if args.asof else latest_score_date(conn)
        run_id = start_run(conn, run_type="publish_biotech_reports", input_path=db_path)
        try:
            assert_output_paths_writable([index_csv, top_csv, alerts_csv, evidence_json, trial_validation_csv, trial_validation_summary_csv])
            scores = load_scores(conn, asof_date)
            if not scores:
                raise ValueError(f"No daily_scores rows found for asof_date={asof_date}")
            features = load_features(conn, asof_date)
            prev_asof = previous_score_date(conn, asof_date)
            previous = load_previous_scores(conn, prev_asof)

            summary = build_index_summary(scores, asof_date, top_n)
            top_rows = [flatten_score_row(row) for row in scores[:top_n]]
            alerts = build_alerts(
                current_scores=scores,
                previous_scores=previous,
                prev_asof=prev_asof,
                score_change_min=score_change_min,
                top_n=top_n,
            )
            cards = build_evidence_cards(scores, features, top_n)
            trial_validation_rows = build_trial_validation_rows(
                scores=scores,
                evidence_rows=apply_trial_status_overrides(
                    read_csv_rows(ctgov_evidence_csv, required=True),
                    read_csv_rows(trial_status_overrides_csv) if trial_status_overrides_csv else [],
                ),
                top_n=top_n,
                extra_tickers=validation_extra_tickers,
                max_trials_per_ticker=validation_max_trials,
            )
            trial_validation_summary_rows = build_trial_validation_summary_rows(trial_validation_rows, validation_max_trials)

            write_csv(index_csv, [summary], list(summary.keys()))
            write_csv(top_csv, top_rows, TOP_SCORE_FIELDS)
            write_csv(
                alerts_csv,
                alerts,
                [
                    "asof_date",
                    "ticker",
                    "company_name",
                    "alert_type",
                    "current_score",
                    "previous_score",
                    "score_change",
                    "current_rank",
                    "previous_rank",
                    "current_bucket",
                    "previous_bucket",
                    "previous_asof_date",
                ],
            )
            write_json(evidence_json, cards)
            write_csv(
                trial_validation_csv,
                trial_validation_rows,
                [
                    "asof_date", "rank", "ticker", "company_name", "opportunity_score", "nct_id", "brief_title",
                    "overall_status", "phase_text", "phase_rank", "primary_purpose", "match_roles",
                    "match_methods", "strong_company_link", "max_confidence", "is_active_status",
                    "is_pivotal", "qualifying_trial", "trial_score", "days_since_last_update",
                    "last_update_post_date", "primary_completion_date", "intervention_types",
                    "intervention_names", "exclusion_reasons", "outcome_override_applied",
                    "outcome_override_status", "outcome_override_reason", "outcome_override_source_url",
                    "outcome_override_manual_review", "sponsors",
                ],
            )
            write_csv(
                trial_validation_summary_csv,
                trial_validation_summary_rows,
                [
                    "asof_date", "rank", "ticker", "company_name", "opportunity_score",
                    "rows_in_validation_csv", "validation_cap_reached", "needs_manual_review",
                    "active_trials", "active_qualifying_trials",
                    "active_phase2_3_trials", "fresh_active_phase2_3_trials",
                    "stale_active_phase2_3_trials", "lead_active_qualifying_trials",
                    "lead_active_phase2_3_trials", "program_active_qualifying_trials",
                    "program_active_phase2_3_trials", "collab_only_active_qualifying_trials",
                    "collab_only_active_phase2_3_trials", "pivotal_active_qualifying_trials",
                    "lead_or_program_pivotal_active_trials", "weak_link_rows", "stale_active_trials",
                    "non_qualifying_rows", "terminal_rows", "outcome_override_rows",
                    "outcome_override_excluded_rows", "outcome_override_review_rows", "review_flags",
                ],
            )
            LOGGER.info("Published biotech reports: rows=%d output_dir=%s", len(scores), output_dir)
            finish_run(
                conn,
                run_id=run_id,
                status="success",
                row_count=len(scores),
                message=f"asof={asof_date} top_n={top_n} alerts={len(alerts)} output_dir={output_dir}",
            )
        except BaseException as exc:
            if run_id is not None and not (isinstance(exc, SystemExit) and exc.code in (0, None)):
                finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()




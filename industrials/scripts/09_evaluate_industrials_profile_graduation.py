#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import importlib
import logging
import sqlite3
import sys
from collections import Counter
from collections.abc import Iterable
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.core.sec_predecessor_bridge import (  # noqa: E402
    DESPAC_BRIDGE_PROFILE,
    load_certified_predecessor_rows,
)
from industrials.core.text_norm import normalize_ticker  # noqa: E402


FINANCIAL_FEATURES = importlib.import_module("industrials.scripts.08_build_industrials_financial_features")

LOGGER = logging.getLogger("evaluate_industrials_profile_graduation")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CANDIDATE_PROFILES = frozenset({"RECENT_IPO_DEVELOPMENT_STAGE", "RECENT_PUBLIC_STUB"})
PERIODIC_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"})
TARGET_PROFILE_BY_TAXONOMY = {
    "us-gaap": ("SEC_XBRL_US_GAAP", "US_GAAP", 0.90),
    "ifrs-full": ("SEC_XBRL_IFRS", "IFRS", 0.75),
}
DECISION_FIELDS = [
    "ticker",
    "handling_type",
    "parent_ticker",
    "skip_sec_network",
    "reporting_profile",
    "reporting_standard",
    "fallback_status",
    "financial_confidence",
    "usable_xbrl_flag",
    "review_reason",
    "notes",
    "valid_from",
    "reviewed_at",
]
AUDIT_FIELDS = [
    "ticker",
    "asof_date",
    "current_reporting_profile",
    "target_reporting_profile",
    "target_taxonomy",
    "graduation_eligible_flag",
    "application_status",
    "effective_date",
    "blocking_reasons",
    "development_stage",
    "trading_days_available",
    "market_data_quality",
    "financial_data_quality_status",
    "annual_form_type",
    "annual_filing_date",
    "annual_period_end",
    "annual_accession_number",
    "annual_age_days",
    "periodic_filing_count",
    "annual_metric_count",
    "annual_metrics",
    "missing_annual_metrics",
    "predecessor_bridge_used_flag",
    "predecessor_bridge_accession_number",
    "predecessor_bridge_metric_count",
    "revenue_mode",
    "revenue_ttm_status",
    "operating_cash_flow_ttm_status",
    "capex_ttm_status",
    "reporting_currency",
    "fx_coverage_status",
    "projected_financial_confidence",
    "projected_data_quality_status",
    "projected_fx_conversion_status",
    "projected_revenue_ttm_usd",
    "projected_free_cash_flow_ttm_usd",
    "projected_canonical_quality",
    "evidence_version",
]
EVIDENCE_VERSION = "controlled_profile_graduation_v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate recent-issuer reporting profiles for a controlled, PIT-safe graduation."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--model-family", default="")
    parser.add_argument("--asof", required=True, help="Evidence cutoff date, YYYY-MM-DD.")
    parser.add_argument("--tickers", default="", help="Optional comma-separated candidate filter.")
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--apply", action="store_true", help="Append eligible decisions to the dated graduation ledger.")
    parser.add_argument(
        "--effective-date",
        default="",
        help="Required with --apply and must be later than --asof, preserving sealed snapshots.",
    )
    return parser.parse_args()


def parse_required_date(raw: object, *, field: str) -> date:
    text = str(raw or "").strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Invalid {field}={raw!r}; expected YYYY-MM-DD") from exc


def parse_tickers(raw: object) -> list[str]:
    out: list[str] = []
    for value in str(raw or "").split(","):
        ticker = normalize_ticker(value)
        if ticker and ticker not in out:
            out.append(ticker)
    return out


def placeholders(values: Iterable[object]) -> str:
    return ",".join("?" for _ in values)


def currency_from_unit(raw: object) -> str | None:
    text = str(raw or "").strip().upper()
    if len(text) == 3 and text.isalpha() and text not in {"SHR"}:
        return text
    return None


def ttm_status(rows: list[dict[str, Any]], metric: str) -> tuple[str, Any]:
    result = FINANCIAL_FEATURES.ttm_metric_result(rows, metric)
    if result.value is not None:
        return "available", result
    return str(result.quality_flag or f"ttm_{metric}_unavailable"), result


def choose_target_taxonomy(subject: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    primary = str(subject.get("primary_taxonomy") or "").strip().lower()
    counts = Counter(str(row.get("taxonomy") or "").strip().lower() for row in rows)
    if primary in TARGET_PROFILE_BY_TAXONOMY and counts[primary] > 0:
        return primary
    candidates = [(count, taxonomy) for taxonomy, count in counts.items() if taxonomy in TARGET_PROFILE_BY_TAXONOMY]
    return max(candidates)[1] if candidates else ""


def latest_annual_evidence(rows: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, set[str]]:
    annual_rows = [row for row in rows if FINANCIAL_FEATURES.is_annual_fact(row)]
    if not annual_rows:
        return None, set()
    latest_period_end = max(str(row.get("period_end") or "") for row in annual_rows)
    period_rows = [row for row in annual_rows if str(row.get("period_end") or "") == latest_period_end]
    anchor = max(
        period_rows,
        key=lambda row: (
            str(row.get("filing_date") or ""),
            str(row.get("accession_number") or ""),
        ),
    )
    anchor_accession = str(anchor.get("accession_number") or "")
    metrics = {
        str(row.get("canonical_metric") or "")
        for row in period_rows
        if str(row.get("accession_number") or "") == anchor_accession
    }
    return anchor, metrics


def periodic_filing_count(rows: list[dict[str, Any]], *, annual_period_end: str) -> int:
    return len(
        {
            str(row.get("accession_number") or "")
            for row in rows
            if str(row.get("period_end") or "") >= annual_period_end
            and str(row.get("accession_number") or "")
        }
    )


def infer_reporting_currency(rows: list[dict[str, Any]]) -> str:
    counts = Counter(currency for row in rows if (currency := currency_from_unit(row.get("unit"))) is not None)
    if not counts:
        return ""
    top = max(counts.values())
    return sorted(currency for currency, count in counts.items() if count == top)[0]


def fx_coverage_status(
    conn: sqlite3.Connection,
    *,
    currency: str,
    asof: date,
    window_start: date | None,
    max_staleness_days: int,
) -> str:
    if not currency or currency == "USD":
        return "not_required"
    latest = conn.execute(
        """
        SELECT MAX(rate_date) AS max_rate_date
        FROM fact_fx_rate
        WHERE from_currency = ? AND to_currency = 'USD' AND rate_date <= ?
        """,
        (currency, asof.isoformat()),
    ).fetchone()
    latest_date_raw = str(latest["max_rate_date"] or "") if latest is not None else ""
    if not latest_date_raw:
        return f"missing_{currency}USD_rate"
    latest_date = parse_required_date(latest_date_raw, field="fx_rate_date")
    if (asof - latest_date).days > max_staleness_days:
        return f"stale_{currency}USD_rate_{latest_date.isoformat()}"
    if window_start is not None:
        count = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM fact_fx_rate
                WHERE from_currency = ? AND to_currency = 'USD'
                  AND rate_date >= ? AND rate_date <= ?
                """,
                (currency, window_start.isoformat(), asof.isoformat()),
            ).fetchone()[0]
            or 0
        )
        if count == 0:
            return f"missing_{currency}USD_ttm_window_rates"
    return "covered"


def evaluate_candidate(
    conn: sqlite3.Connection,
    *,
    subject: dict[str, Any],
    facts: list[dict[str, Any]],
    bridge_facts: list[dict[str, Any]],
    asof: date,
    min_trading_days: int,
    max_annual_age_days: int,
    min_periodic_filings: int,
    fx_max_staleness_days: int,
    source_id: str,
    model_family: str,
    market_source_ids: list[str],
) -> dict[str, Any]:
    ticker = str(subject.get("ticker") or "")
    current_profile = str(subject.get("reporting_profile") or "").strip().upper()
    reasons: list[str] = []
    trading_days = int(subject.get("trading_days_available") or 0)
    if current_profile not in CANDIDATE_PROFILES:
        reasons.append("profile_not_graduation_candidate")
    if trading_days < min_trading_days:
        reasons.append(f"insufficient_trading_days_{trading_days}_of_{min_trading_days}")
    if str(subject.get("market_data_quality") or "") != "complete":
        reasons.append("market_data_quality_not_complete")
    if str(subject.get("financial_data_quality_status") or "") != "complete":
        reasons.append("financial_data_quality_not_complete")

    periodic_facts = [
        row
        for row in facts
        if str(row.get("form_type") or "").upper() in PERIODIC_FORMS
        and str(row.get("taxonomy") or "").lower() in TARGET_PROFILE_BY_TAXONOMY
    ]
    target_taxonomy = choose_target_taxonomy(subject, periodic_facts)
    target_profile = ""
    target_standard = ""
    if target_taxonomy:
        target_profile, target_standard, _ = TARGET_PROFILE_BY_TAXONOMY[target_taxonomy]
    else:
        reasons.append("no_supported_periodic_xbrl_taxonomy")
    taxonomy_rows = [row for row in periodic_facts if str(row.get("taxonomy") or "").lower() == target_taxonomy]

    development_stage = str(subject.get("development_stage") or "").strip().lower()
    revenue_rows = [row for row in taxonomy_rows if str(row.get("canonical_metric") or "") == "revenue"]
    revenue_mode = "pre_revenue" if development_stage == "development_stage" and not revenue_rows else "revenue_reporting"
    required_annual_metrics = {
        "net_income",
        "operating_cash_flow",
        "capex",
    }
    if revenue_mode == "revenue_reporting":
        required_annual_metrics.add("revenue")
    else:
        required_annual_metrics.add("operating_income")

    periodic_annual_anchor, periodic_annual_metrics = latest_annual_evidence(taxonomy_rows)
    bridge_annual_anchor, bridge_annual_metrics = latest_annual_evidence(bridge_facts)
    periodic_missing = required_annual_metrics - periodic_annual_metrics
    bridge_missing = required_annual_metrics - bridge_annual_metrics
    bridge_used = bool(
        current_profile == "RECENT_PUBLIC_STUB"
        and bridge_annual_anchor is not None
        and len(bridge_missing) < len(periodic_missing)
    )
    if bridge_used:
        target_profile = DESPAC_BRIDGE_PROFILE
        target_standard = "US_GAAP_DESPAC_BRIDGE"
        target_profile_confidence = 0.75
        evidence_rows = [*taxonomy_rows, *bridge_facts]
        annual_anchor = bridge_annual_anchor
        annual_metrics = bridge_annual_metrics
    else:
        target_profile_confidence = TARGET_PROFILE_BY_TAXONOMY.get(target_taxonomy, ("", "", 0.0))[2]
        evidence_rows = taxonomy_rows
        annual_anchor = periodic_annual_anchor
        annual_metrics = periodic_annual_metrics
    annual_period_end = str(annual_anchor.get("period_end") or "") if annual_anchor is not None else ""
    annual_age_days: int | str = ""
    filing_count = 0
    if annual_anchor is None or not annual_period_end:
        reasons.append("no_periodic_xbrl_annual_baseline")
    else:
        annual_end_date = parse_required_date(annual_period_end, field=f"{ticker}.annual_period_end")
        annual_age_days = (asof - annual_end_date).days
        if annual_age_days < 0:
            reasons.append("annual_period_after_asof")
        elif annual_age_days > max_annual_age_days:
            reasons.append(f"annual_baseline_stale_{annual_age_days}_days")
        filing_count = periodic_filing_count(taxonomy_rows, annual_period_end=annual_period_end) + (1 if bridge_used else 0)
        if filing_count < min_periodic_filings:
            reasons.append(f"insufficient_periodic_filing_history_{filing_count}_of_{min_periodic_filings}")

    missing_annual = sorted(required_annual_metrics - annual_metrics)
    if missing_annual:
        reasons.append("missing_annual_metrics=" + ",".join(missing_annual))

    revenue_ttm_status, revenue_ttm = ttm_status(evidence_rows, "revenue")
    ocf_ttm_status, ocf_ttm = ttm_status(evidence_rows, "operating_cash_flow")
    capex_ttm_status, capex_ttm = ttm_status(evidence_rows, "capex")
    if revenue_mode == "revenue_reporting" and revenue_ttm.value is None:
        reasons.append(revenue_ttm_status)
    if ocf_ttm.value is None:
        reasons.append(ocf_ttm_status)
    if capex_ttm.value is None:
        reasons.append(capex_ttm_status)
    if revenue_mode == "pre_revenue" and (ocf_ttm.value is None or float(ocf_ttm.value) >= 0.0):
        reasons.append("pre_revenue_negative_operating_cash_flow_not_validated")

    reporting_currency = infer_reporting_currency(evidence_rows)
    ttm_starts = [
        result.window_start
        for result in (revenue_ttm, ocf_ttm, capex_ttm)
        if result.value is not None and result.window_start is not None
    ]
    fx_status = fx_coverage_status(
        conn,
        currency=reporting_currency,
        asof=asof,
        window_start=min(ttm_starts) if ttm_starts else None,
        max_staleness_days=fx_max_staleness_days,
    )
    if fx_status not in {"covered", "not_required"}:
        reasons.append(fx_status)

    projected: dict[str, Any] = {}
    if target_taxonomy:
        projected = FINANCIAL_FEATURES.build_feature_from_facts(
            conn,
            ticker=ticker,
            asof=asof,
            source_id=source_id,
            model_family=model_family,
            company=subject,
            profile={
                "reporting_profile": target_profile,
                "reporting_standard": target_standard,
                "primary_taxonomy": target_taxonomy,
                "financial_confidence": target_profile_confidence,
                "fallback_status": "none",
                "usable_xbrl_flag": 1,
            },
            rows=evidence_rows,
            market_source_ids=market_source_ids,
            fx_max_staleness_days=fx_max_staleness_days,
        )
        projected_status = str(projected.get("data_quality_status") or "")
        projected_confidence = float(projected.get("financial_confidence") or 0.0)
        minimum_confidence = 0.50 if development_stage == "development_stage" else (0.60 if target_taxonomy == "us-gaap" else 0.55)
        if projected_status != "complete":
            reasons.append("projected_financial_data_quality_not_complete")
        for required_current_metric in ("assets_usd", "cash_and_equivalents_usd"):
            if projected.get(required_current_metric) is None:
                reasons.append(f"projected_{required_current_metric}_missing")
        if projected_confidence < minimum_confidence:
            reasons.append(
                f"projected_financial_confidence_{projected_confidence:.4f}_below_{minimum_confidence:.2f}"
            )
        projected_fx_status = str(projected.get("fx_conversion_status") or "")
        if reporting_currency != "USD" and projected_fx_status != "converted_to_usd":
            reasons.append(f"projected_fx_conversion_not_complete_{projected_fx_status or 'missing'}")

    return {
        "ticker": ticker,
        "asof_date": asof.isoformat(),
        "current_reporting_profile": current_profile,
        "target_reporting_profile": target_profile,
        "target_reporting_standard": target_standard,
        "target_profile_confidence": target_profile_confidence,
        "target_taxonomy": target_taxonomy,
        "graduation_eligible_flag": 0 if reasons else 1,
        "application_status": "eligible_not_applied" if not reasons else "not_eligible",
        "effective_date": "",
        "blocking_reasons": ";".join(reasons),
        "development_stage": development_stage,
        "trading_days_available": trading_days,
        "market_data_quality": str(subject.get("market_data_quality") or ""),
        "financial_data_quality_status": str(subject.get("financial_data_quality_status") or ""),
        "annual_form_type": str(annual_anchor.get("form_type") or "") if annual_anchor is not None else "",
        "annual_filing_date": str(annual_anchor.get("filing_date") or "") if annual_anchor is not None else "",
        "annual_period_end": annual_period_end,
        "annual_accession_number": str(annual_anchor.get("accession_number") or "") if annual_anchor is not None else "",
        "annual_age_days": annual_age_days,
        "periodic_filing_count": filing_count,
        "annual_metric_count": len(annual_metrics),
        "annual_metrics": ",".join(sorted(annual_metrics)),
        "missing_annual_metrics": ",".join(missing_annual),
        "predecessor_bridge_used_flag": int(bridge_used),
        "predecessor_bridge_accession_number": (
            str(bridge_annual_anchor.get("accession_number") or "") if bridge_used and bridge_annual_anchor is not None else ""
        ),
        "predecessor_bridge_metric_count": len(bridge_annual_metrics) if bridge_used else 0,
        "revenue_mode": revenue_mode,
        "revenue_ttm_status": revenue_ttm_status,
        "operating_cash_flow_ttm_status": ocf_ttm_status,
        "capex_ttm_status": capex_ttm_status,
        "reporting_currency": reporting_currency,
        "fx_coverage_status": fx_status,
        "projected_financial_confidence": projected.get("financial_confidence", ""),
        "projected_data_quality_status": projected.get("data_quality_status", ""),
        "projected_fx_conversion_status": projected.get("fx_conversion_status", ""),
        "projected_revenue_ttm_usd": projected.get("revenue_ttm_usd", ""),
        "projected_free_cash_flow_ttm_usd": projected.get("free_cash_flow_ttm_usd", ""),
        "projected_canonical_quality": projected.get("canonical_quality", ""),
        "evidence_version": EVIDENCE_VERSION,
    }


def load_subjects(
    conn: sqlite3.Connection,
    *,
    model_family: str,
    asof: date,
    ticker_filter: list[str],
) -> list[dict[str, Any]]:
    filter_sql = ""
    if ticker_filter:
        filter_sql = f"AND c.ticker IN ({placeholders(ticker_filter)})"
    rows = conn.execute(
        f"""
        SELECT c.ticker, c.cik, c.company_name, c.currency,
               t.development_stage,
               COALESCE(NULLIF(f.reporting_profile, ''), p.reporting_profile) AS reporting_profile,
               COALESCE(NULLIF(f.reporting_standard, ''), p.reporting_standard) AS reporting_standard,
               p.primary_taxonomy,
               p.financial_confidence AS profile_financial_confidence,
               f.data_quality_status AS financial_data_quality_status,
               f.canonical_quality, f.revenue_usd,
               m.trading_days_available, m.market_data_quality
        FROM dim_company c
        JOIN dim_industrials_taxonomy t
          ON t.company_id = c.company_id AND t.model_family = ?
        JOIN dim_issuer_reporting_profile p
          ON p.ticker = c.ticker AND p.model_family = t.model_family
        LEFT JOIN feature_financial_statement f
          ON f.ticker = c.ticker AND f.model_family = t.model_family AND f.asof_date = ?
        LEFT JOIN feature_market_technical m
          ON m.ticker = c.ticker AND m.model_family = t.model_family AND m.asof_date = ?
        WHERE c.is_active = 1
          {filter_sql}
        ORDER BY c.ticker
        """,
        (model_family, asof.isoformat(), asof.isoformat(), *ticker_filter),
    ).fetchall()
    return [dict(row) for row in rows]


def load_periodic_facts(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    model_family: str,
    source_id: str,
    asof: date,
) -> list[dict[str, Any]]:
    forms = sorted(PERIODIC_FORMS)
    rows = conn.execute(
        f"""
        SELECT *
        FROM fact_financial_statement_canonical
        WHERE ticker = ? AND model_family = ? AND source_id = ?
          AND taxonomy IN ('us-gaap', 'ifrs-full')
          AND form_type IN ({placeholders(forms)})
          AND period_end <= ?
          AND COALESCE(substr(accepted_at, 1, 10), filing_date) <= ?
        ORDER BY period_end DESC, filing_date DESC, source_priority ASC, concept_name ASC
        """,
        (ticker, model_family, source_id, *forms, asof.isoformat(), asof.isoformat()),
    ).fetchall()
    return [dict(row) for row in rows]


def read_decisions(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if list(reader.fieldnames or []) != DECISION_FIELDS:
            raise ValueError(f"Graduation decision schema mismatch in {path}: {reader.fieldnames}")
        return [dict(row) for row in reader]


def append_decisions(
    path: Path,
    *,
    audit_rows: list[dict[str, Any]],
    effective_date: date,
    reviewed_at: date,
) -> set[str]:
    rows = read_decisions(path)
    by_key = {(normalize_ticker(row.get("ticker")), str(row.get("valid_from") or "")): row for row in rows}
    applied: set[str] = set()
    for audit in audit_rows:
        if int(audit["graduation_eligible_flag"]) != 1:
            continue
        ticker = str(audit["ticker"])
        target_profile = str(audit["target_reporting_profile"])
        target_standard = str(audit["target_reporting_standard"])
        target_confidence = float(audit["target_profile_confidence"])
        key = (ticker, effective_date.isoformat())
        decision = {
            "ticker": ticker,
            "handling_type": (
                "controlled_despac_profile_graduation"
                if int(audit.get("predecessor_bridge_used_flag") or 0) == 1
                else "controlled_profile_graduation"
            ),
            "parent_ticker": "",
            "skip_sec_network": "false",
            "reporting_profile": target_profile,
            "reporting_standard": target_standard,
            "fallback_status": "none",
            "financial_confidence": f"{target_confidence:.2f}",
            "usable_xbrl_flag": "1",
            "review_reason": "",
            "notes": (
                f"{EVIDENCE_VERSION}; evidence_asof={audit['asof_date']}; "
                f"annual={audit['annual_form_type']}:{audit['annual_period_end']}; "
                f"taxonomy={audit['target_taxonomy']}; revenue_mode={audit['revenue_mode']}; "
                f"predecessor_bridge={audit.get('predecessor_bridge_used_flag', 0)}"
            ),
            "valid_from": effective_date.isoformat(),
            "reviewed_at": reviewed_at.isoformat(),
        }
        existing = by_key.get(key)
        if existing is not None:
            semantic_fields = [field for field in DECISION_FIELDS if field != "reviewed_at"]
            if any(str(existing.get(field) or "") != str(decision.get(field) or "") for field in semantic_fields):
                raise ValueError(f"Conflicting graduation decision already exists for ticker={ticker} valid_from={key[1]}")
        if existing is None:
            later_dates = [
                str(row.get("valid_from") or "")
                for row in rows
                if normalize_ticker(row.get("ticker")) == ticker and str(row.get("valid_from") or "") > key[1]
            ]
            if later_dates:
                raise ValueError(f"Cannot append older graduation decision for {ticker}; later versions exist: {later_dates}")
            rows.append(decision)
            by_key[key] = decision
        applied.add(ticker)
    rows.sort(key=lambda row: (str(row.get("valid_from") or ""), normalize_ticker(row.get("ticker"))))
    write_csv_atomic(path, DECISION_FIELDS, rows)
    return applied


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    asof = parse_required_date(args.asof, field="asof")
    effective_date = parse_required_date(args.effective_date, field="effective_date") if args.effective_date else None
    if args.apply and effective_date is None:
        raise ValueError("--effective-date is required with --apply")
    if effective_date is not None and effective_date <= asof:
        raise ValueError("--effective-date must be later than --asof to preserve sealed PIT snapshots")

    model_family = str(args.model_family or cfg_get(config, "industrials_universe.initial_subsector", "defense") or "defense").strip()
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    family_key = f"scoring_policy.families.{model_family}"
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(cfg_get(config, f"{family_key}.reporting_profile_graduation_audit_csv"), base_dir=base_dir)
    )
    decisions_csv = resolve_path(
        cfg_get(config, f"{family_key}.reporting_profile_graduations_csv"),
        base_dir=base_dir,
    )
    min_trading_days = int(cfg_get(config, "market_data_policy.min_trading_days_for_full_features", 252))
    max_annual_age_days = int(cfg_get(config, f"{family_key}.profile_graduation.max_annual_age_days", 550))
    min_periodic_filings = int(cfg_get(config, f"{family_key}.profile_graduation.min_periodic_filings", 2))
    fx_max_staleness_days = int(cfg_get(config, "fx_rates.max_staleness_days", 7))
    source_id = str(cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts") or "sec_companyfacts")
    market_source_id = str(cfg_get(config, "market_data_policy.scoring_primary_source", "yahoo_finance_adjusted") or "yahoo_finance_adjusted")
    market_fallback_sources = [
        str(value).strip()
        for value in (cfg_get(config, "market_data_policy.scoring_fallback_sources", []) or [])
        if str(value).strip()
    ]
    market_source_ids = list(dict.fromkeys([market_source_id, *market_fallback_sources]))
    ticker_filter = parse_tickers(args.tickers)

    uri = f"file:{db_path.as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        subjects = load_subjects(conn, model_family=model_family, asof=asof, ticker_filter=ticker_filter)
        if ticker_filter:
            found = {str(subject["ticker"]) for subject in subjects}
            missing = sorted(set(ticker_filter) - found)
            if missing:
                raise ValueError(f"Requested tickers missing from active {model_family} universe: {missing}")
        candidates = [
            subject
            for subject in subjects
            if str(subject.get("reporting_profile") or "").strip().upper() in CANDIDATE_PROFILES
        ]
        audits = [
            evaluate_candidate(
                conn,
                subject=subject,
                facts=load_periodic_facts(
                    conn,
                    ticker=str(subject["ticker"]),
                    model_family=model_family,
                    source_id=source_id,
                    asof=asof,
                ),
                bridge_facts=load_certified_predecessor_rows(
                    conn,
                    ticker=str(subject["ticker"]),
                    source_id=source_id,
                    asof=asof,
                ),
                asof=asof,
                min_trading_days=min_trading_days,
                max_annual_age_days=max_annual_age_days,
                min_periodic_filings=min_periodic_filings,
                fx_max_staleness_days=fx_max_staleness_days,
                source_id=source_id,
                model_family=model_family,
                market_source_ids=market_source_ids,
            )
            for subject in candidates
        ]

    applied: set[str] = set()
    if args.apply and effective_date is not None:
        applied = append_decisions(
            decisions_csv,
            audit_rows=audits,
            effective_date=effective_date,
            reviewed_at=datetime.now(timezone.utc).date(),
        )
    for row in audits:
        ticker = str(row["ticker"])
        if ticker in applied:
            row["application_status"] = "applied"
            row["effective_date"] = effective_date.isoformat() if effective_date is not None else ""
    write_csv_atomic(output_csv, AUDIT_FIELDS, [{field: row.get(field, "") for field in AUDIT_FIELDS} for row in audits])

    eligible = [str(row["ticker"]) for row in audits if int(row["graduation_eligible_flag"]) == 1]
    blocked = [str(row["ticker"]) for row in audits if int(row["graduation_eligible_flag"]) == 0]
    LOGGER.info(
        "Profile graduation gate: asof=%s candidates=%d eligible=%s blocked=%s applied=%s output=%s",
        asof.isoformat(),
        len(audits),
        eligible,
        blocked,
        sorted(applied),
        output_csv,
    )


if __name__ == "__main__":
    main()

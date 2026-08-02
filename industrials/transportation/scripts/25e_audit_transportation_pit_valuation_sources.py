#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.oos_research import artifact_sha256  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.core.share_sources import load_reviewed_share_observations  # noqa: E402
from industrials.transportation.classification import (  # noqa: E402
    load_classification_overlays,
    resolve_classification,
)
from industrials.transportation.contracts import read_rows  # noqa: E402
from industrials.transportation.scripts._shared import DEFAULT_CONFIG  # noqa: E402
from industrials.transportation.valuation_source_audit import (  # noqa: E402
    companyfacts_path,
    inspect_companyfacts_share_sources,
    load_companyfacts,
    load_share_conversions,
    resolve_share_conversion,
    summarize_audit,
)


DEFAULT_ASOF = "2026-07-30"
REPORT_FIELDS = [
    "ticker",
    "company_name",
    "cik",
    "membership_status",
    "membership_start_date",
    "membership_end_date",
    "source_evaluation_date",
    "historical_membership_flag",
    "calibration_cohort",
    "industry",
    "calibration_pool",
    "risk_tier",
    "portfolio_role",
    "research_window_overlap_flag",
    "required_for_rebuild",
    "companyfacts_cache_flag",
    "companyfacts_cache_path",
    "share_source_kind",
    "share_namespace",
    "share_concept",
    "usable_fact_count",
    "first_period_end",
    "last_period_end",
    "first_filed_date",
    "last_filed_date",
    "reporting_forms",
    "foreign_reporting_flag",
    "conversion_review_status",
    "listing_instrument",
    "underlying_shares_per_traded_security",
    "conversion_source_url",
    "readiness_status",
    "disposition",
    "reason",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only audit of point-in-time outstanding-share and traded-security "
            "conversion sources required before transportation valuation history is built."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", default=DEFAULT_ASOF)
    return parser.parse_args()


def _universe_lookup(active_path: Path, delisted_path: Path) -> dict[str, dict[str, str]]:
    output: dict[str, dict[str, str]] = {}
    for row in read_rows(active_path):
        ticker = str(row.get("ticker") or "").strip().upper()
        output[ticker] = {
            "company_name": str(row.get("company_name") or "").strip(),
            "cik": str(row.get("cik") or "").strip(),
            "calibration_cohort_id": str(row.get("calibration_cohort") or "").strip(),
            "industry": str(row.get("industry") or "").strip(),
            "membership_status": "active_seed_only",
        }
    for row in read_rows(delisted_path):
        ticker = str(row.get("ticker") or "").strip().upper()
        if ticker in output:
            raise ValueError(f"Ticker appears in active and delisted seeds: {ticker}")
        output[ticker] = {
            "company_name": str(row.get("company") or "").strip(),
            "cik": str(row.get("cik") or "").strip(),
            "calibration_cohort_id": str(row.get("cohort") or "").strip(),
            "industry": str(row.get("industry") or "").strip(),
            "membership_status": "delisted_seed_only",
        }
    return output


def _research_overlap(row: dict[str, str], *, start: date, asof: date) -> bool:
    if not str(row.get("start_date") or "").strip():
        return False
    member_start = date.fromisoformat(str(row["start_date"])[:10])
    end_text = str(row.get("end_date") or "").strip()[:10]
    member_end = date.fromisoformat(end_text) if end_text else None
    return member_start <= asof and (member_end is None or member_end >= start)


def _source_disposition(
    *,
    cache_exists: bool,
    share_source_kind: str,
    foreign_reporting: bool,
    conversion_status: str,
) -> tuple[str, str, str]:
    if not cache_exists:
        if share_source_kind == "reviewed_override":
            return (
                "READY",
                "READY_REVIEWED_FILING_SHARES",
                "Reviewed point-in-time filing share observation is available",
            )
        return "BLOCKED", "MISSING_COMPANYFACTS", "CompanyFacts cache is absent"
    if share_source_kind == "reviewed_override":
        return (
            "READY",
            "READY_REVIEWED_FILING_SHARES",
            "Reviewed point-in-time filing share observation is available",
        )
    if share_source_kind == "none":
        return "BLOCKED", "MISSING_POINT_IN_TIME_SHARE_FACT", "No usable point-in-time share fact"
    if share_source_kind == "fallback":
        return (
            "BLOCKED",
            "REVIEW_WEIGHTED_AVERAGE_FALLBACK",
            "Only weighted-average shares are available; point-in-time use requires review",
        )
    if conversion_status == "PENDING_REVIEW":
        return (
            "BLOCKED",
            "REVIEW_SHARE_CONVERSION",
            "Traded-security conversion is pending source-backed review",
        )
    if foreign_reporting and conversion_status not in {"REVIEWED_ADR", "REVIEWED_DIRECT"}:
        return (
            "BLOCKED",
            "REVIEW_FOREIGN_LISTING_STRUCTURE",
            "Foreign-reporting issuer requires reviewed ADR/direct-share classification",
        )
    if conversion_status == "REVIEWED_ADR":
        return "READY", "READY_REVIEWED_ADR_CONVERSION", "Primary share facts and reviewed ADR ratio available"
    if conversion_status == "REVIEWED_DIRECT":
        return "READY", "READY_REVIEWED_DIRECT_SHARES", "Primary share facts and reviewed direct-share ratio available"
    return "READY", "READY_SEC_POINT_IN_TIME_SHARES", "Primary point-in-time SEC share facts available"


def main() -> int:
    args = parse_args()
    asof = date.fromisoformat(str(args.asof)[:10])
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    family = family_config(config, "transportation")
    universe = family["universe"]
    financial = family["financial"]
    research_start = date.fromisoformat(str(family["historical_load"]["start_date"])[:10])
    membership_path = resolve_path(universe["historical_membership_csv"], base_dir=base_dir)
    active_path = resolve_path(universe["seed_csv"], base_dir=base_dir)
    delisted_path = resolve_path(universe["delisted_seed_csv"], base_dir=base_dir)
    overlays_path = resolve_path(universe["classification_overlays_csv"], base_dir=base_dir)
    conversions_path = resolve_path(financial["share_conversion_overrides_csv"], base_dir=base_dir)
    share_policy = family.get("share_snapshot_ingestion", {})
    reviewed_path = resolve_path(share_policy["reviewed_share_observations_csv"], base_dir=base_dir)
    report_path = resolve_path(financial["valuation_source_audit_output_csv"], base_dir=base_dir)
    manifest_path = resolve_path(financial["valuation_source_audit_output_json"], base_dir=base_dir)
    cache_root = resolve_path(cfg_get(config, "sec_fundamentals.cache_dir"), base_dir=base_dir) / "sec_companyfacts"

    universe_rows = _universe_lookup(active_path, delisted_path)
    memberships = read_rows(membership_path)
    historical_tickers = {
        str(row.get("internal_ticker") or "").strip().upper() for row in memberships
    }
    seed_only_tickers = sorted(set(universe_rows) - historical_tickers)
    unknown_historical_tickers = sorted(historical_tickers - set(universe_rows))
    if unknown_historical_tickers:
        raise ValueError(
            "Historical membership contains tickers outside active/delisted seeds: "
            + ",".join(unknown_historical_tickers)
        )
    for ticker in seed_only_tickers:
        seed = universe_rows[ticker]
        memberships.append(
            {
                "internal_ticker": ticker,
                **seed,
                "start_date": "",
                "end_date": "",
            }
        )
    tickers = [str(row.get("internal_ticker") or "").strip().upper() for row in memberships]
    duplicates = sorted({ticker for ticker in tickers if tickers.count(ticker) > 1})
    if any(not ticker for ticker in tickers) or duplicates:
        raise ValueError(f"Historical membership ticker contract invalid; duplicates={duplicates}")
    overlays = load_classification_overlays(overlays_path)
    conversions = load_share_conversions(conversions_path)
    reviewed_observations = load_reviewed_share_observations(
        reviewed_path,
        model_family="transportation",
        history_start=research_start,
        asof=asof,
    )
    reviewed_by_ticker: dict[str, list[Any]] = {}
    for observation in reviewed_observations:
        reviewed_by_ticker.setdefault(observation.ticker, []).append(observation)
    report_rows: list[dict[str, Any]] = []
    parse_errors: list[str] = []
    for member in memberships:
        ticker = str(member["internal_ticker"]).strip().upper()
        cohort = str(member.get("calibration_cohort_id") or "").strip()
        industry = universe_rows[ticker]["industry"]
        development = cohort == "development_stage_and_speculative_transport"
        classification = resolve_classification(
            {
                "ticker": ticker,
                "industry": industry,
                "calibration_cohort": cohort,
                "calibration_use": "excluded" if development else "core",
                "development_stage": "development" if development else "operating",
            },
            asof=asof.isoformat(),
            overlays=overlays,
        )
        overlap = _research_overlap(member, start=research_start, asof=asof)
        required = overlap and classification.risk_tier == "operating" and classification.portfolio_role in {
            "core_candidate",
            "airline_satellite_research",
        }
        membership_end_text = str(member.get("end_date") or "").strip()[:10]
        membership_end = (
            date.fromisoformat(membership_end_text) if membership_end_text else None
        )
        source_evaluation_date = min(asof, membership_end) if membership_end else asof
        cache_path = companyfacts_path(cache_root, member.get("cik"))
        cache_exists = bool(cache_path and cache_path.is_file())
        source: dict[str, object] = {
            "share_source_kind": "none",
            "share_namespace": "",
            "share_concept": "",
            "usable_fact_count": 0,
            "first_period_end": "",
            "last_period_end": "",
            "first_filed_date": "",
            "last_filed_date": "",
            "reporting_forms": "",
            "foreign_reporting_flag": 0,
        }
        if cache_exists and cache_path is not None:
            try:
                source = inspect_companyfacts_share_sources(
                    load_companyfacts(cache_path),
                    asof=source_evaluation_date,
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                parse_errors.append(f"{ticker}:{exc}")
                cache_exists = False
        reviewed_rows = [
            observation
            for observation in reviewed_by_ticker.get(ticker, [])
            if observation.asof_date <= source_evaluation_date
            and (source_evaluation_date - observation.asof_date).days <= 550
        ]
        if reviewed_rows:
            source["share_source_kind"] = "reviewed_override"
            source["share_namespace"] = "reviewed_filing"
            source["share_concept"] = reviewed_rows[-1].outstanding_method
            source["usable_fact_count"] = len(reviewed_rows)
            source["first_filed_date"] = reviewed_rows[0].asof_date.isoformat()
            source["last_filed_date"] = reviewed_rows[-1].asof_date.isoformat()
        conversion = resolve_share_conversion(
            ticker,
            asof=source_evaluation_date,
            conversions=conversions,
        )
        conversion_status = conversion.review_status if conversion else ""
        readiness, disposition, reason = _source_disposition(
            cache_exists=cache_exists,
            share_source_kind=str(source["share_source_kind"]),
            foreign_reporting=str(source["foreign_reporting_flag"]) == "1",
            conversion_status=conversion_status,
        )
        report_rows.append(
            {
                "ticker": ticker,
                "company_name": member.get("company_name", ""),
                "cik": member.get("cik", ""),
                "membership_status": member.get("membership_status", ""),
                "membership_start_date": member.get("start_date", ""),
                "membership_end_date": member.get("end_date", ""),
                "source_evaluation_date": source_evaluation_date.isoformat(),
                "historical_membership_flag": int(ticker in historical_tickers),
                "calibration_cohort": cohort,
                "industry": industry,
                "calibration_pool": classification.calibration_pool,
                "risk_tier": classification.risk_tier,
                "portfolio_role": classification.portfolio_role,
                "research_window_overlap_flag": int(overlap),
                "required_for_rebuild": int(required),
                "companyfacts_cache_flag": int(cache_exists),
                "companyfacts_cache_path": str(cache_path or ""),
                **source,
                "conversion_review_status": conversion_status,
                "listing_instrument": conversion.listing_instrument if conversion else "",
                "underlying_shares_per_traded_security": (
                    conversion.underlying_shares_per_traded_security if conversion else ""
                ),
                "conversion_source_url": conversion.source_url if conversion else "",
                "readiness_status": readiness,
                "disposition": disposition,
                "reason": reason,
            }
        )
    report_rows.sort(key=lambda row: str(row["ticker"]))
    write_csv_atomic(report_path, REPORT_FIELDS, report_rows)
    summary = summarize_audit(report_rows)
    result = {
        "artifact_family": "transportation_pit_valuation_source_audit",
        "model_family": "transportation",
        "asof_date": asof.isoformat(),
        "research_start_date": research_start.isoformat(),
        "acceptance": "PASS" if not parse_errors else "FAIL",
        "parse_errors": parse_errors,
        "membership_path": str(membership_path),
        "membership_sha256": artifact_sha256(membership_path),
        "historical_membership_count": len(historical_tickers),
        "active_seed_path": str(active_path),
        "active_seed_sha256": artifact_sha256(active_path),
        "delisted_seed_path": str(delisted_path),
        "delisted_seed_sha256": artifact_sha256(delisted_path),
        "active_delisted_seed_union_count": len(universe_rows),
        "seed_only_tickers": seed_only_tickers,
        "classification_overlays_path": str(overlays_path),
        "classification_overlays_sha256": artifact_sha256(overlays_path),
        "share_conversion_overrides_path": str(conversions_path),
        "share_conversion_overrides_sha256": artifact_sha256(conversions_path),
        "reviewed_share_observations_path": str(reviewed_path),
        "reviewed_share_observations_sha256": artifact_sha256(reviewed_path),
        "report_path": str(report_path),
        **summary,
    }
    result["report_sha256"] = artifact_sha256(report_path)
    write_text_atomic(manifest_path, json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

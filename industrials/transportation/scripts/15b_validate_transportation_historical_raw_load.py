#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.transportation.contracts import file_sha256, write_manifest  # noqa: E402
from industrials.transportation.security_continuity import (  # noqa: E402
    load_security_continuity_policies,
)
from industrials.transportation.scripts._shared import DEFAULT_CONFIG, MODEL_FAMILY  # noqa: E402


FIELDS = [
    "ticker",
    "company_name",
    "calibration_cohort",
    "universe_role",
    "calibration_usable_flag",
    "membership_start_date",
    "membership_end_date",
    "continuity_policy",
    "history_treatment",
    "current_security_start_date",
    "related_price_symbols",
    "expected_price_source_id",
    "price_bar_count",
    "adjusted_price_bar_count",
    "first_price_date",
    "last_price_date",
    "price_status",
    "cik",
    "submissions_cache_flag",
    "companyfacts_cache_flag",
    "archive_filing_count",
    "sec_filing_count",
    "first_filing_date",
    "last_filing_date",
    "raw_xbrl_fact_count",
    "mapped_xbrl_fact_count",
    "reporting_profile",
    "reporting_standard",
    "profile_financial_confidence",
    "sec_status",
    "specialized_candidate_count",
    "specialized_status",
    "overall_status",
    "review_reasons",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only validation of loaded transportation historical raw data."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--strict-review", action="store_true")
    return parser.parse_args()


def parse_date(value: object) -> date | None:
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def normalize_cik(value: object) -> str:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    return digits.zfill(10) if digits else ""


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        return [
            {str(key): str(value or "") for key, value in row.items() if key is not None}
            for row in reader
        ]


def read_only_connection(path: Path, *, timeout_sec: float) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro",
        uri=True,
        timeout=timeout_sec,
    )
    connection.row_factory = sqlite3.Row
    return connection


def main() -> int:
    args = parse_args()
    asof_date = parse_date(args.asof)
    if asof_date is None:
        raise ValueError(f"Invalid --asof={args.asof!r}")
    asof = asof_date.isoformat()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    universe = family["universe"]
    historical = family["historical_load"]
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(
        cfg_get(config, "paths.database_path"),
        base_dir=base_dir,
    )
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(
        historical["output_dir"],
        base_dir=base_dir,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    coverage_path = output_dir / "transportation_historical_raw_load_coverage.csv"
    validation_path = output_dir / "transportation_historical_raw_load_validation.json"
    listing_rows = read_csv_rows(resolve_path(universe["listing_dates_csv"], base_dir=base_dir))
    listing_start = {
        str(row.get("ticker") or "").strip().upper(): str(
            row.get("first_eligible_date") or ""
        )
        for row in listing_rows
    }
    continuity_policies = load_security_continuity_policies(
        resolve_path(universe["security_continuity_overrides_csv"], base_dir=base_dir)
    )
    mapping_rows = read_csv_rows(resolve_path(universe["norgate_symbol_map_csv"], base_dir=base_dir))
    calibration_usable = {
        str(row.get("internal_ticker") or "").strip().upper(): str(
            row.get("calibration_usable_flag") or ""
        )
        for row in mapping_rows
    }
    approved_exclusions = {
        str(value).strip().upper()
        for value in historical.get("approved_price_exclusions", [])
        if str(value).strip()
    }
    active_source = str(historical["active_price_source_id"])
    delisted_source = str(historical["delisted_price_source_id"])
    benchmarks = [str(value).strip().upper() for value in historical["benchmark_tickers"]]
    configured_fx = {
        str(value).strip().upper()
        for value in historical["required_fx_pairs"]
        if str(value).strip()
    }
    research_start = parse_date(historical["start_date"])
    if research_start is None:
        raise ValueError("transportation historical_load.start_date is invalid")
    cache_root = resolve_path(cfg_get(config, "sec_fundamentals.cache_dir"), base_dir=base_dir)
    submissions_root = cache_root / "sec_submissions"
    companyfacts_root = cache_root / "sec_companyfacts"
    archive_root = cache_root / "sec_archive_xbrl"
    timeout = float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))
    with read_only_connection(db_path, timeout_sec=timeout) as connection:
        taxonomy = [
            dict(row)
            for row in connection.execute(
                """
                SELECT t.ticker, c.company_name, c.cik, t.calibration_cohort_id
                FROM dim_industrials_taxonomy AS t
                JOIN dim_company AS c ON c.company_id=t.company_id
                WHERE t.model_family=?
                ORDER BY t.ticker
                """,
                (MODEL_FAMILY,),
            ).fetchall()
        ]
        active_members = {
            str(row["ticker"]): dict(row)
            for row in connection.execute(
                """
                SELECT ticker, MIN(start_date) AS start_date, MAX(end_date) AS end_date
                FROM dim_universe_membership
                WHERE model_family=? AND membership_source_id=?
                  AND membership_status='active' AND is_current_member=1
                GROUP BY ticker
                """,
                (MODEL_FAMILY, str(universe["seed_source_id"])),
            ).fetchall()
        }
        historical_members = {
            str(row["ticker"]): dict(row)
            for row in connection.execute(
                """
                SELECT ticker, MIN(start_date) AS start_date, MAX(end_date) AS end_date,
                       MAX(membership_status) AS membership_status
                FROM dim_universe_membership
                WHERE model_family=? AND membership_source_id=?
                GROUP BY ticker
                """,
                (MODEL_FAMILY, str(universe["historical_membership_source_id"])),
            ).fetchall()
        }
        delisted_seed = {
            str(row["ticker"]): dict(row)
            for row in connection.execute(
                "SELECT * FROM dim_delisted_calibration_seed WHERE model_family=?",
                (MODEL_FAMILY,),
            ).fetchall()
        }
        price_rows = {
            (str(row["ticker"]), str(row["source_id"])): dict(row)
            for row in connection.execute(
                """
                SELECT ticker, source_id, COUNT(*) AS bar_count,
                       SUM(CASE WHEN is_adjusted=1 AND adj_close IS NOT NULL THEN 1 ELSE 0 END)
                           AS adjusted_bar_count,
                       MIN(bar_date) AS first_date, MAX(bar_date) AS last_date
                FROM fact_price_ohlcv
                WHERE source_id IN (?, ?)
                GROUP BY ticker, source_id
                """,
                (active_source, delisted_source),
            ).fetchall()
        }
        filings = {
            str(row["ticker"]): dict(row)
            for row in connection.execute(
                """
                SELECT ticker, COUNT(*) AS filing_count, MIN(filing_date) AS first_filing_date,
                       MAX(filing_date) AS last_filing_date
                FROM fact_sec_filing
                GROUP BY ticker
                """
            ).fetchall()
        }
        raw_facts = {
            str(row["ticker"]): int(row["fact_count"])
            for row in connection.execute(
                "SELECT ticker, COUNT(*) AS fact_count FROM fact_sec_xbrl_fact_raw GROUP BY ticker"
            ).fetchall()
        }
        mapped_facts = {
            str(row["ticker"]): int(row["fact_count"])
            for row in connection.execute(
                "SELECT ticker, COUNT(*) AS fact_count FROM fact_sec_xbrl_fact GROUP BY ticker"
            ).fetchall()
        }
        profiles = {
            str(row["ticker"]): dict(row)
            for row in connection.execute(
                """
                SELECT ticker, reporting_profile, reporting_standard, financial_confidence,
                       review_reason
                FROM dim_issuer_reporting_profile
                WHERE model_family=?
                """,
                (MODEL_FAMILY,),
            ).fetchall()
        }
        candidate_counts = {
            str(row["ticker"]): int(row["candidate_count"])
            for row in connection.execute(
                """
                SELECT ticker, COUNT(*) AS candidate_count
                FROM fact_sec_metric_disclosure_candidate
                WHERE model_family=?
                GROUP BY ticker
                """,
                (MODEL_FAMILY,),
            ).fetchall()
        }
        relevant_currencies = {
            str(row["currency"]).upper()
            for row in connection.execute(
                """
                SELECT DISTINCT UPPER(f.unit) AS currency
                FROM fact_sec_xbrl_fact AS f
                WHERE f.ticker IN (
                    SELECT ticker FROM dim_industrials_taxonomy WHERE model_family=?
                )
                  AND LENGTH(f.unit)=3
                  AND f.unit GLOB '[A-Za-z][A-Za-z][A-Za-z]'
                  AND UPPER(f.unit)<>'USD'
                """,
                (MODEL_FAMILY,),
            ).fetchall()
            if str(row["currency"] or "")
        }
        required_fx = configured_fx | {f"{currency}USD" for currency in relevant_currencies}
        fx_rows = {
            str(row["currency_pair"]): dict(row)
            for row in connection.execute(
                """
                SELECT currency_pair, COUNT(*) AS row_count, MIN(rate_date) AS first_date,
                       MAX(rate_date) AS last_date
                FROM fact_fx_rate
                WHERE source_id='yahoo_fx_rates'
                GROUP BY currency_pair
                """
            ).fetchall()
        }
        last_sec_run = connection.execute(
            """
            SELECT run_id, status, row_count, message, started_at, completed_at
            FROM runs
            WHERE run_type='sync_industrials_sec_fundamentals'
              AND message LIKE '%transportation%'
            ORDER BY run_id DESC LIMIT 1
            """
        ).fetchone()
        db_continuity = {
            str(row["ticker"]): dict(row)
            for row in connection.execute(
                """
                SELECT ticker, current_security_start_date, continuity_policy,
                       history_treatment, related_price_symbols, required_fx_pair,
                       evidence_label, review_status
                FROM dim_security_continuity_policy
                WHERE model_family=?
                """,
                (MODEL_FAMILY,),
            ).fetchall()
        }
    errors: list[str] = []
    warnings: list[str] = []
    if len(taxonomy) != 160:
        errors.append(f"transportation taxonomy rows={len(taxonomy)} expected=160")
    if len(active_members) != 112:
        errors.append(f"active memberships={len(active_members)} expected=112")
    if len(historical_members) != 159:
        errors.append(f"historical memberships={len(historical_members)} expected=159")
    if len(delisted_seed) != 48:
        errors.append(f"delisted seeds={len(delisted_seed)} expected=48")
    if set(db_continuity) != set(continuity_policies):
        errors.append(
            "security-continuity DB/file ticker mismatch: "
            f"db={sorted(db_continuity)} file={sorted(continuity_policies)}"
        )
    for ticker, policy in continuity_policies.items():
        stored = db_continuity.get(ticker, {})
        for field, expected in {
            "current_security_start_date": policy.current_security_start_date,
            "continuity_policy": policy.continuity_policy,
            "history_treatment": policy.history_treatment,
            "related_price_symbols": policy.related_price_symbols,
            "required_fx_pair": policy.required_fx_pair,
            "evidence_label": policy.evidence_label,
            "review_status": policy.review_status,
        }.items():
            if str(stored.get(field) or "") != str(expected or ""):
                errors.append(
                    f"security-continuity mismatch ticker={ticker} field={field} "
                    f"db={stored.get(field)!r} file={expected!r}"
                )
    rows: list[dict[str, Any]] = []
    for item in taxonomy:
        ticker = str(item["ticker"])
        continuity = continuity_policies.get(ticker)
        reasons: list[str] = []
        if ticker in active_members:
            role = "active"
            member = active_members[ticker]
            expected_source = active_source
            usable = "1"
            expected_start = max(
                research_start,
                parse_date(listing_start.get(ticker)) or research_start,
            )
        elif ticker in historical_members:
            role = "delisted_usable"
            member = historical_members[ticker]
            expected_source = delisted_source
            usable = calibration_usable.get(ticker, "0")
            expected_start = parse_date(member.get("start_date"))
        elif ticker in delisted_seed and ticker in approved_exclusions:
            role = "delisted_excluded"
            member = {"start_date": "", "end_date": ""}
            expected_source = ""
            usable = "0"
            expected_start = None
        else:
            role = "unresolved"
            member = {"start_date": "", "end_date": ""}
            expected_source = ""
            usable = "0"
            expected_start = None
            reasons.append("unresolved_universe_role")
        price = price_rows.get((ticker, expected_source), {}) if expected_source else {}
        price_count = int(price.get("bar_count") or 0)
        adjusted_count = int(price.get("adjusted_bar_count") or 0)
        first_price = str(price.get("first_date") or "")
        last_price = str(price.get("last_date") or "")
        price_status = "PASS"
        if role == "delisted_excluded":
            price_status = "EXCLUDED_APPROVED"
            reasons.append("approved_provider_identity_exclusion")
        elif price_count == 0:
            price_status = "FAIL"
            reasons.append("missing_required_price_history")
        else:
            first_day = parse_date(first_price)
            last_day = parse_date(last_price)
            if adjusted_count != price_count:
                price_status = "FAIL"
                reasons.append("unadjusted_or_null_adjusted_price_rows")
            if expected_start and (first_day is None or first_day > expected_start + timedelta(days=7)):
                price_status = "REVIEW" if price_status == "PASS" else price_status
                reasons.append(
                    f"price_starts_after_expected_window:{expected_start.isoformat()}:{first_price}"
                )
            if role == "active" and (last_day is None or last_day < asof_date):
                price_status = "FAIL"
                reasons.append(f"active_price_not_current_through_asof:{last_price}")
            if role == "delisted_usable":
                expected_end = parse_date(member.get("end_date"))
                if expected_end and last_day != expected_end:
                    price_status = "REVIEW" if price_status == "PASS" else price_status
                    reasons.append(
                        f"terminal_price_date_mismatch:{expected_end.isoformat()}:{last_price}"
                    )
        cik = normalize_cik(item.get("cik"))
        submissions_flag = int(bool(cik and (submissions_root / f"CIK{cik}.json").exists()))
        companyfacts_flag = int(bool(cik and (companyfacts_root / f"CIK{cik}.json").exists()))
        cik_archive_dir = archive_root / f"CIK{cik}" if cik else Path("")
        archive_count = (
            sum(1 for _ in cik_archive_dir.glob("*/index.json"))
            if cik and cik_archive_dir.exists()
            else 0
        )
        filing = filings.get(ticker, {})
        filing_count = int(filing.get("filing_count") or 0)
        raw_count = raw_facts.get(ticker, 0)
        mapped_count = mapped_facts.get(ticker, 0)
        profile = profiles.get(ticker, {})
        profile_name = str(profile.get("reporting_profile") or "")
        sec_status = "PASS"
        if not profile_name:
            sec_status = "FAIL"
            reasons.append("missing_transportation_reporting_profile")
        elif profile_name in {
            "NO_FINANCIALS_REVIEW",
            "SEC_20F_METADATA_ONLY",
            "SEC_XBRL_US_GAAP_PARTIAL",
            "SEC_XBRL_IFRS_PARTIAL",
        }:
            sec_status = "REVIEW"
            reasons.append(f"reporting_profile_requires_review:{profile_name}")
        if not submissions_flag:
            sec_status = "REVIEW" if sec_status == "PASS" else sec_status
            reasons.append("submissions_cache_missing")
        if filing_count == 0:
            sec_status = "REVIEW" if sec_status == "PASS" else sec_status
            reasons.append("no_sec_filing_metadata")
        if raw_count == 0:
            sec_status = "REVIEW" if sec_status == "PASS" else sec_status
            reasons.append("no_raw_xbrl_facts")
        candidate_count = candidate_counts.get(ticker, 0)
        specialized_status = (
            "CANDIDATE_AVAILABLE" if candidate_count else "SEPARATE_STAGE_08C_GATE"
        )
        overall_status = "PASS"
        if price_status == "FAIL" or sec_status == "FAIL" or role == "unresolved":
            overall_status = "FAIL"
        elif price_status == "REVIEW" or sec_status == "REVIEW":
            overall_status = "REVIEW"
        rows.append(
            {
                "ticker": ticker,
                "company_name": item["company_name"],
                "calibration_cohort": item["calibration_cohort_id"],
                "universe_role": role,
                "calibration_usable_flag": usable,
                "membership_start_date": member.get("start_date") or "",
                "membership_end_date": member.get("end_date") or "",
                "continuity_policy": continuity.continuity_policy if continuity else "",
                "history_treatment": continuity.history_treatment if continuity else "",
                "current_security_start_date": (
                    continuity.current_security_start_date if continuity else ""
                ),
                "related_price_symbols": (
                    continuity.related_price_symbols if continuity else ""
                ),
                "expected_price_source_id": expected_source,
                "price_bar_count": price_count,
                "adjusted_price_bar_count": adjusted_count,
                "first_price_date": first_price,
                "last_price_date": last_price,
                "price_status": price_status,
                "cik": cik,
                "submissions_cache_flag": submissions_flag,
                "companyfacts_cache_flag": companyfacts_flag,
                "archive_filing_count": archive_count,
                "sec_filing_count": filing_count,
                "first_filing_date": filing.get("first_filing_date") or "",
                "last_filing_date": filing.get("last_filing_date") or "",
                "raw_xbrl_fact_count": raw_count,
                "mapped_xbrl_fact_count": mapped_count,
                "reporting_profile": profile_name,
                "reporting_standard": profile.get("reporting_standard") or "",
                "profile_financial_confidence": profile.get("financial_confidence")
                if profile.get("financial_confidence") is not None
                else "",
                "sec_status": sec_status,
                "specialized_candidate_count": candidate_count,
                "specialized_status": specialized_status,
                "overall_status": overall_status,
                "review_reasons": ";".join(dict.fromkeys(reasons)),
            }
        )
    benchmark_summary: dict[str, Any] = {}
    for ticker in benchmarks:
        price = price_rows.get((ticker, active_source), {})
        count = int(price.get("bar_count") or 0)
        first = str(price.get("first_date") or "")
        last = str(price.get("last_date") or "")
        status = "PASS"
        if count == 0 or (parse_date(last) or date.min) < asof_date:
            status = "FAIL"
            errors.append(f"benchmark {ticker} missing/current-through-asof price history")
        benchmark_summary[ticker] = {
            "status": status,
            "bar_count": count,
            "first_date": first,
            "last_date": last,
        }
    fx_summary: dict[str, Any] = {}
    for pair in sorted(required_fx):
        payload = fx_rows.get(pair, {})
        count = int(payload.get("row_count") or 0)
        first = str(payload.get("first_date") or "")
        last = str(payload.get("last_date") or "")
        status = "PASS"
        if count == 0:
            status = "FAIL"
            errors.append(f"missing required FX pair={pair}")
        elif (parse_date(first) or date.max) > research_start + timedelta(days=7):
            status = "FAIL"
            errors.append(f"FX pair={pair} starts after research window: {first}")
        elif (parse_date(last) or date.min) < asof_date:
            status = "FAIL"
            errors.append(f"FX pair={pair} ends before asof: {last}")
        fx_summary[pair] = {
            "status": status,
            "row_count": count,
            "first_date": first,
            "last_date": last,
        }
    row_statuses = Counter(str(row["overall_status"]) for row in rows)
    role_counts = Counter(str(row["universe_role"]) for row in rows)
    price_statuses = Counter(str(row["price_status"]) for row in rows)
    sec_statuses = Counter(str(row["sec_status"]) for row in rows)
    if row_statuses["FAIL"]:
        errors.append(f"ticker raw-load failures={row_statuses['FAIL']}")
    if row_statuses["REVIEW"]:
        warnings.append(f"ticker raw-load reviews={row_statuses['REVIEW']}")
    specialized_tickers = sum(int(row["specialized_candidate_count"]) > 0 for row in rows)
    # Specialized disclosures are not universally reported. Requiring one
    # candidate for every issuer made legitimate NOT_DISCLOSED cases look like
    # raw-load failures. Stage 08c independently gates bounded document
    # recovery, candidate provenance, cohort signal, and whether coverage is
    # sufficient to authorize a full historical disclosure backfill.
    write_csv_atomic(coverage_path, FIELDS, rows)
    acceptance = "FAIL" if errors else ("PASS_WITH_REVIEW" if warnings else "PASS")
    result = {
        "acceptance": acceptance,
        "model_family": MODEL_FAMILY,
        "asof_date": asof,
        "research_start_date": research_start.isoformat(),
        "database_path": str(db_path),
        "ticker_count": len(rows),
        "role_counts": dict(sorted(role_counts.items())),
        "security_continuity_policy_count": len(continuity_policies),
        "security_continuity_policy_counts": dict(
            sorted(
                Counter(
                    policy.continuity_policy
                    for policy in continuity_policies.values()
                ).items()
            )
        ),
        "overall_status_counts": dict(sorted(row_statuses.items())),
        "price_status_counts": dict(sorted(price_statuses.items())),
        "active_price_bar_count": sum(
            int(row["price_bar_count"]) for row in rows if row["universe_role"] == "active"
        ),
        "delisted_price_bar_count": sum(
            int(row["price_bar_count"])
            for row in rows
            if row["universe_role"] == "delisted_usable"
        ),
        "sec_status_counts": dict(sorted(sec_statuses.items())),
        "benchmark_coverage": benchmark_summary,
        "required_fx_coverage": fx_summary,
        "reporting_profile_counts": dict(
            sorted(Counter(str(row["reporting_profile"]) for row in rows).items())
        ),
        "sec_filing_ticker_count": sum(int(row["sec_filing_count"]) > 0 for row in rows),
        "sec_filing_row_count": sum(int(row["sec_filing_count"]) for row in rows),
        "raw_xbrl_ticker_count": sum(int(row["raw_xbrl_fact_count"]) > 0 for row in rows),
        "raw_xbrl_fact_row_count": sum(int(row["raw_xbrl_fact_count"]) for row in rows),
        "mapped_xbrl_fact_row_count": sum(
            int(row["mapped_xbrl_fact_count"]) for row in rows
        ),
        "companyfacts_cache_ticker_count": sum(
            int(row["companyfacts_cache_flag"]) for row in rows
        ),
        "submissions_cache_ticker_count": sum(
            int(row["submissions_cache_flag"]) for row in rows
        ),
        "archive_cache_ticker_count": sum(int(row["archive_filing_count"]) > 0 for row in rows),
        "specialized_candidate_ticker_count": specialized_tickers,
        "specialized_parser_gate": "DELEGATED_TO_STAGE_08C",
        "approved_price_exclusions": sorted(approved_exclusions),
        "latest_sec_run": dict(last_sec_run) if last_sec_run is not None else {},
        "coverage_csv": str(coverage_path),
        "coverage_csv_sha256": file_sha256(coverage_path),
        "errors": errors,
        "warnings": warnings,
    }
    write_manifest(validation_path, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    if errors or (args.strict_review and warnings):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

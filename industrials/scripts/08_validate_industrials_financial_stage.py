#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sys
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect, init_db  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("validate_industrials_financial_stage")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
FEATURE_STAGE = "build_industrials_financial_features"
VALID_PROFILES = {
    "SEC_XBRL_US_GAAP",
    "SEC_XBRL_IFRS",
    "SEC_XBRL_US_GAAP_PARTIAL",
    "SEC_XBRL_IFRS_PARTIAL",
    "SEC_20F_METADATA_ONLY",
    "FOREIGN_VENDOR_FUNDAMENTALS",
    "FOREIGN_NEUTRAL_LOW_CONFIDENCE",
    "NO_FINANCIALS_REVIEW",
    "SEC_RAW_ARCHIVE_REQUIRED",
    "RECENT_IPO_DEVELOPMENT_STAGE",
    "PRIVATE_EXCLUDE",
    "PARENT_SEGMENT_NO_STANDALONE_SEC",
    "NON_FILING_OR_PENDING_REPORTING",
}
ACCEPTED_DATE_SQL = """
CASE
    WHEN COALESCE(accepted_at, '') GLOB '????-??-??*' THEN SUBSTR(accepted_at, 1, 10)
    WHEN COALESCE(accepted_at, '') GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]*'
        THEN SUBSTR(accepted_at, 1, 4) || '-' || SUBSTR(accepted_at, 5, 2) || '-' || SUBSTR(accepted_at, 7, 2)
    ELSE filing_date
END
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate Stage 4 industrials financial-feature gates.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--model-family", default="", help="Industrials model family to validate, e.g. defense.")
    parser.add_argument("--asof", default="", help="Validation as-of date. Defaults to latest financial feature date.")
    parser.add_argument("--strict-fallbacks", action="store_true", help="Fail neutral fallback rows instead of allowing review-only rows.")
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def scalar(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


def value(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row is not None else None


def placeholders(values: list[str]) -> str:
    if not values:
        raise ValueError("values cannot be empty")
    return ",".join("?" for _ in values)


def load_universe(conn: Any, model_family: str, *, asof: date | None) -> list[str]:
    if asof is not None:
        rows = conn.execute(
            """
            SELECT DISTINCT m.ticker
            FROM dim_universe_membership m
            JOIN dim_company c
              ON c.company_id = m.company_id
            WHERE m.model_family = ?
              AND m.start_date <= ?
              AND COALESCE(m.end_date, '9999-12-31') >= ?
            ORDER BY m.ticker
            """,
            (model_family, asof.isoformat(), asof.isoformat()),
        ).fetchall()
        return [normalize_ticker(row["ticker"]) for row in rows if normalize_ticker(row["ticker"])]
    rows = conn.execute(
        """
        SELECT c.ticker
        FROM dim_company c
        JOIN dim_industrials_taxonomy t
          ON t.company_id = c.company_id
         AND t.model_family = ?
        WHERE c.is_active = 1
        ORDER BY c.ticker
        """,
        (model_family,),
    ).fetchall()
    return [normalize_ticker(row["ticker"]) for row in rows if normalize_ticker(row["ticker"])]


def validate() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    model_family = str(args.model_family or cfg_get(config, "industrials_universe.initial_subsector", "defense") or "defense").strip()
    source_id = str(cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts") or "sec_companyfacts")
    submissions_source_id = str(cfg_get(config, "sec_fundamentals.submissions_source_id", "sec_submissions") or "sec_submissions")
    expected_count = int(cfg_get(config, "industrials_universe.expected_ticker_count", 0) or 0)
    requested_asof = parse_date(args.asof)

    errors: list[str] = []
    warnings: list[str] = []

    with closing(connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0)))) as conn:
        init_db(conn)
        for check_source in [submissions_source_id, source_id]:
            status = value(conn, "SELECT status FROM source_registry WHERE source_id = ?", (check_source,))
            if status != "active":
                errors.append(f"Source {check_source} is not active in source_registry: {status!r}")
        concept_count = scalar(conn, "SELECT COUNT(*) FROM dim_xbrl_concept_map WHERE active_flag = 1")
        if concept_count < 40:
            errors.append(f"XBRL concept map appears underseeded: active_concepts={concept_count}")

        universe = load_universe(conn, model_family, asof=requested_asof)
        if not universe:
            errors.append(f"No industrials universe tickers found for model_family={model_family}")
            universe = ["__NO_TICKERS__"]
        if requested_asof is None and expected_count and "__NO_TICKERS__" not in universe and len(universe) != expected_count:
            errors.append(f"Universe count mismatch: expected={expected_count} actual={len(universe)}")
        ph = placeholders(universe)

        feature_asof = args.asof.strip() or value(
            conn,
            f"""
            SELECT MAX(asof_date)
            FROM feature_financial_statement
            WHERE source_id = ?
              AND model_family = ?
              AND ticker IN ({ph})
            """,
            (source_id, model_family, *universe),
        )
        audit_asof = requested_asof or parse_date(feature_asof)
        if audit_asof is None:
            errors.append("No financial feature as-of date found.")
            audit_asof = date.today()
        else:
            LOGGER.info("Stage 4 financial validation asof=%s", audit_asof.isoformat())

        profile_rows = conn.execute(
            f"""
            SELECT ticker, reporting_profile, reporting_standard, usable_xbrl_flag,
                   financial_confidence, review_reason
            FROM dim_issuer_reporting_profile
            WHERE model_family = ?
              AND ticker IN ({ph})
            ORDER BY ticker
            """,
            (model_family, *universe),
        ).fetchall()
        profile_by_ticker = {str(row["ticker"]): row for row in profile_rows}
        missing_profiles = [ticker for ticker in universe if ticker not in profile_by_ticker]
        if missing_profiles:
            errors.append(f"Missing issuer reporting profiles: {missing_profiles}")
        invalid_profiles = [
            f"{row['ticker']}:{row['reporting_profile']}"
            for row in profile_rows
            if str(row["reporting_profile"] or "") not in VALID_PROFILES
        ]
        if invalid_profiles:
            errors.append(f"Invalid reporting profiles: {invalid_profiles}")
        confidence_bad = [
            f"{row['ticker']}:{row['financial_confidence']}"
            for row in profile_rows
            if row["financial_confidence"] is None or float(row["financial_confidence"]) < 0 or float(row["financial_confidence"]) > 1
        ]
        if confidence_bad:
            errors.append(f"Profile financial confidence outside [0,1]: {confidence_bad}")

        feature_rows = conn.execute(
            f"""
            SELECT *
            FROM feature_financial_statement
            WHERE source_id = ?
              AND model_family = ?
              AND asof_date = ?
              AND ticker IN ({ph})
            ORDER BY ticker
            """,
            (source_id, model_family, audit_asof.isoformat(), *universe),
        ).fetchall()
        feature_by_ticker = {str(row["ticker"]): row for row in feature_rows}
        missing_features = [ticker for ticker in universe if ticker not in feature_by_ticker]
        if missing_features:
            errors.append(f"Missing financial feature rows: {missing_features}")

        fallback_rows = []
        review_rows = []
        bad_feature_confidence = []
        future_periods = []
        non_usd_missing_fx = []
        complete_missing_core = []
        for row in feature_rows:
            ticker = str(row["ticker"])
            status = str(row["data_quality_status"] or "")
            if status != "complete":
                review_rows.append(f"{ticker}:{status}:{row['review_reason'] or ''}")
            if status == "neutral_low_confidence":
                fallback_rows.append(ticker)
            confidence = row["financial_confidence"]
            if confidence is None or float(confidence) < 0 or float(confidence) > 1:
                bad_feature_confidence.append(f"{ticker}:{confidence}")
            period_end = parse_date(row["fiscal_period_end"])
            if period_end is not None and period_end > audit_asof:
                future_periods.append(f"{ticker}:{period_end.isoformat()}")
            currency = str(row["reported_currency"] or "").upper()
            fx_status = str(row["fx_conversion_status"] or "")
            if currency and currency != "USD" and fx_status == "missing_fx_rate":
                non_usd_missing_fx.append(ticker)
            if status == "complete" and (row["revenue_usd"] is None or row["assets_usd"] is None):
                complete_missing_core.append(ticker)
        if bad_feature_confidence:
            errors.append(f"Feature financial confidence outside [0,1]: {bad_feature_confidence}")
        if future_periods:
            errors.append(f"Financial features have fiscal period ends after asof={audit_asof.isoformat()}: {future_periods}")
        if complete_missing_core:
            errors.append(f"Complete financial rows missing core USD fields: {complete_missing_core}")
        if non_usd_missing_fx:
            warnings.append(f"Non-USD rows missing FX and held in review: {non_usd_missing_fx}")
        if fallback_rows and args.strict_fallbacks:
            errors.append(f"Neutral-low-confidence fallback rows present under --strict-fallbacks: {fallback_rows}")

        usable_profiles = [
            str(row["ticker"])
            for row in profile_rows
            if int(row["usable_xbrl_flag"] or 0) == 1
        ]
        if usable_profiles:
            usable_ph = placeholders(usable_profiles)
            raw_count = scalar(
                conn,
                f"""
                SELECT COUNT(*)
                FROM fact_sec_xbrl_fact_raw
                WHERE source_id = ?
                  AND ticker IN ({usable_ph})
                """,
                (source_id, *usable_profiles),
            )
            mapped_count = scalar(
                conn,
                f"""
                SELECT COUNT(*)
                FROM fact_sec_xbrl_fact
                WHERE source_id = ?
                  AND ticker IN ({usable_ph})
                """,
                (source_id, *usable_profiles),
            )
            canonical_count = scalar(
                conn,
                f"""
                SELECT COUNT(*)
                FROM fact_financial_statement_canonical
                WHERE source_id = ?
                  AND model_family = ?
                  AND ticker IN ({usable_ph})
                """,
                (source_id, model_family, *usable_profiles),
            )
            if raw_count == 0:
                errors.append("Usable XBRL profiles exist but no raw SEC XBRL facts are stored.")
            if mapped_count == 0:
                errors.append("Usable XBRL profiles exist but no mapped SEC XBRL facts are stored.")
            if canonical_count == 0:
                errors.append("Usable XBRL profiles exist but no canonical financial facts are stored.")
            mapped_without_raw = scalar(
                conn,
                f"""
                SELECT COUNT(*)
                FROM fact_sec_xbrl_fact
                WHERE source_id = ?
                  AND ticker IN ({usable_ph})
                  AND raw_fact_id IS NULL
                """,
                (source_id, *usable_profiles),
            )
            if mapped_without_raw:
                errors.append(f"Mapped SEC facts missing raw_fact_id linkage: {mapped_without_raw}")
            priority_mismatches = conn.execute(
                f"""
                SELECT c.ticker, c.canonical_metric, c.period_end, c.accession_number,
                       c.source_priority AS canonical_priority,
                       best.best_priority AS expected_priority
                FROM fact_financial_statement_canonical c
                JOIN (
                    SELECT ticker, source_id, canonical_metric, period_end,
                           COALESCE(accession_number, '') AS accession_number,
                           COALESCE(unit, '') AS unit,
                           MIN(source_priority) AS best_priority
                    FROM fact_sec_xbrl_fact
                    WHERE source_id = ?
                      AND ticker IN ({usable_ph})
                      AND period_end IS NOT NULL
                      AND period_end <= ?
                      AND ({ACCEPTED_DATE_SQL}) <= ?
                    GROUP BY ticker, source_id, canonical_metric, period_end,
                             COALESCE(accession_number, ''), COALESCE(unit, '')
                ) best
                  ON best.ticker = c.ticker
                 AND best.source_id = c.source_id
                 AND best.canonical_metric = c.canonical_metric
                 AND best.period_end = c.period_end
                 AND best.accession_number = COALESCE(c.accession_number, '')
                 AND best.unit = COALESCE(c.unit, '')
                WHERE c.source_id = ?
                  AND c.model_family = ?
                  AND c.ticker IN ({usable_ph})
                  AND c.source_priority > best.best_priority
                ORDER BY c.ticker, c.canonical_metric, c.period_end
                LIMIT 20
                """,
                (
                    source_id,
                    *usable_profiles,
                    audit_asof.isoformat(),
                    audit_asof.isoformat(),
                    source_id,
                    model_family,
                    *usable_profiles,
                ),
            ).fetchall()
            if priority_mismatches:
                errors.append(f"Canonical fact priority mismatches: {[dict(row) for row in priority_mismatches]}")
            warnings.append(f"Usable XBRL profiles={len(usable_profiles)} raw_facts={raw_count} mapped_facts={mapped_count} canonical_facts={canonical_count}")
        else:
            warnings.append("No usable SEC XBRL profiles found; all financial rows should be explicit fallbacks until sync/vendor data is loaded.")

        future_canonical = scalar(
            conn,
            f"""
            SELECT COUNT(*)
            FROM fact_financial_statement_canonical
            WHERE source_id = ?
              AND model_family = ?
              AND ticker IN ({ph})
              AND (
                    ({ACCEPTED_DATE_SQL}) > ?
                 OR (period_end IS NOT NULL AND period_end > ?)
              )
            """,
            (source_id, model_family, *universe, audit_asof.isoformat(), audit_asof.isoformat()),
        )
        if future_canonical:
            warnings.append(f"Canonical table has {future_canonical} rows after validation asof; feature builder filters them out for PIT panels.")

        review_issue_count = scalar(
            conn,
            f"""
            SELECT COUNT(*)
            FROM data_quality_issues
            WHERE stage = ?
              AND issue_type = 'financial_feature_review'
              AND ticker IN ({ph})
            """,
            (FEATURE_STAGE, *universe),
        )
        if review_rows and review_issue_count < len(review_rows):
            errors.append(f"Financial review issue mismatch: review_features={len(review_rows)} issues={review_issue_count}")

        profile_counts = conn.execute(
            f"""
            SELECT reporting_profile, COUNT(*) AS n
            FROM dim_issuer_reporting_profile
            WHERE model_family = ?
              AND ticker IN ({ph})
            GROUP BY reporting_profile
            ORDER BY reporting_profile
            """,
            (model_family, *universe),
        ).fetchall()
        warnings.append(f"Universe tickers={len(universe)}")
        warnings.append(f"Financial feature asof={audit_asof.isoformat()} rows={len(feature_rows)} review={len(review_rows)} fallback={len(fallback_rows)}")
        warnings.append("Reporting profile counts=" + ", ".join(f"{row['reporting_profile']}:{row['n']}" for row in profile_counts))
        warnings.append(f"Active XBRL concept mappings={concept_count}")

    for message in warnings:
        LOGGER.info(message)
    if errors:
        for message in errors:
            LOGGER.error(message)
        return 1
    LOGGER.info("Industrials Stage 4 financial validation passed for model_family=%s", model_family)
    return 0


if __name__ == "__main__":
    raise SystemExit(validate())

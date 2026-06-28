#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.db import connect, init_db  # noqa: E402
from technology.core.logging_utils import configure_utc_logging  # noqa: E402
from technology.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("validate_technology_sec_positioning_stages")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SEC_SYNC_STAGE = "sync_technology_sec_fundamentals"
FIN_FEATURE_STAGE = "build_technology_financial_features"
POSITIONING_STAGE = "import_technology_positioning"
DIRECT_OWNERSHIP_STAGE = "sync_technology_sec_ownership"
EXPECTED_IFRS_RECOVERY = {"ASX", "GFS", "IMOS", "SQNS", "TSM", "UMC"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate technology Stage 4/5 SEC and positioning gates.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--model-family", default="", help="Technology model family to validate, e.g. semiconductors.")
    parser.add_argument("--allow-missing-borrow", action="store_true", help="Downgrade missing IBKR borrow coverage to warning.")
    parser.add_argument("--13f-exempt-tickers", default="", help="Comma-separated tickers with explicit 13F no-row exemptions.")
    parser.add_argument("--borrow-exempt-tickers", default="", help="Comma-separated tickers with explicit IBKR borrow no-row exemptions.")
    return parser.parse_args()


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


def cfg_ticker_set(raw: Any) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, str):
        values = raw.split(",")
    elif isinstance(raw, (list, tuple, set)):
        values = raw
    else:
        values = []
    return {ticker for ticker in (normalize_ticker(value) for value in values) if ticker}


def load_universe(conn: Any, model_family: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT c.ticker
        FROM dim_company c
        JOIN dim_technology_taxonomy t
          ON t.ticker = c.ticker
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
    model_family = str(
        args.model_family
        or cfg_get(config, "technology_universe.initial_subsector", "semiconductors")
        or "semiconductors"
    ).strip()
    if not model_family:
        raise ValueError("model_family cannot be empty")
    submissions_source = str(cfg_get(config, "sec_fundamentals.submissions_source_id", "sec_submissions"))
    companyfacts_source = str(cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts"))
    fx_source = str(cfg_get(config, "fx_rates.source_id", "yahoo_fx_rates"))
    form4_source = str(cfg_get(config, "positioning_import.form4_source_id", "sec_insider_upstream"))
    direct_ownership_source = str(cfg_get(config, "positioning_import.direct_ownership_source_id", "sec_ownership_direct"))
    mp_source = str(cfg_get(config, "positioning_import.market_positioning_source_id", "market_positioning_upstream"))
    positioning_source = str(cfg_get(config, "positioning_import.source_id", "technology_positioning_composite"))
    require_direct_ownership = str(cfg_get(config, "positioning_import.require_direct_ownership_for_gate", False)).lower() in {"1", "true", "yes", "y"}
    require_13f = str(cfg_get(config, "positioning_import.require_upstream_13f_for_gate", False)).lower() in {"1", "true", "yes", "y"}
    require_short = str(cfg_get(config, "positioning_import.require_upstream_short_for_gate", False)).lower() in {"1", "true", "yes", "y"}
    require_borrow = (
        str(cfg_get(config, "positioning_import.require_upstream_borrow_for_gate", False)).lower() in {"1", "true", "yes", "y"}
        and not bool(args.allow_missing_borrow)
    )
    exempt_13f_tickers = cfg_ticker_set(cfg_get(config, "positioning_import.upstream_13f_gate_exempt_tickers", []))
    exempt_13f_tickers.update(cfg_ticker_set(args.__dict__.get("13f_exempt_tickers", "")))
    exempt_borrow_tickers = cfg_ticker_set(cfg_get(config, "positioning_import.upstream_borrow_gate_exempt_tickers", []))
    exempt_borrow_tickers.update(cfg_ticker_set(args.borrow_exempt_tickers))

    errors: list[str] = []
    warnings: list[str] = []
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        tickers = load_universe(conn, model_family)
        if not tickers:
            errors.append(f"No active technology universe tickers found for model_family={model_family}")
            tickers = ["__NO_TICKERS__"]
        ph = placeholders(tickers)
        for source_id in (submissions_source, companyfacts_source, fx_source, form4_source, direct_ownership_source, mp_source, positioning_source):
            status = value(conn, "SELECT status FROM source_registry WHERE source_id = ?", (source_id,))
            if status != "active":
                errors.append(f"Source {source_id} is not active in source_registry: {status!r}")

        filing_tickers = scalar(
            conn,
            f"SELECT COUNT(DISTINCT ticker) FROM fact_sec_filing WHERE source_id = ? AND ticker IN ({ph})",
            (submissions_source, *tickers),
        )
        filing_rows = scalar(conn, "SELECT COUNT(*) FROM fact_sec_filing WHERE source_id = ?", (submissions_source,))
        accepted_rows = scalar(
            conn,
            "SELECT COUNT(*) FROM fact_sec_filing WHERE source_id = ? AND COALESCE(acceptance_datetime, '') <> ''",
            (submissions_source,),
        )
        fact_tickers = scalar(
            conn,
            f"SELECT COUNT(DISTINCT ticker) FROM fact_sec_xbrl_fact WHERE source_id = ? AND ticker IN ({ph})",
            (companyfacts_source, *tickers),
        )
        fact_rows = scalar(conn, "SELECT COUNT(*) FROM fact_sec_xbrl_fact WHERE source_id = ?", (companyfacts_source,))
        raw_fact_tickers = scalar(
            conn,
            f"SELECT COUNT(DISTINCT ticker) FROM fact_sec_xbrl_fact_raw WHERE source_id = ? AND ticker IN ({ph})",
            (companyfacts_source, *tickers),
        )
        raw_fact_rows = scalar(conn, "SELECT COUNT(*) FROM fact_sec_xbrl_fact_raw WHERE source_id = ?", (companyfacts_source,))
        canonical_tickers = scalar(
            conn,
            f"SELECT COUNT(DISTINCT ticker) FROM fact_financial_statement_canonical WHERE source_id = ? AND ticker IN ({ph})",
            (companyfacts_source, *tickers),
        )
        canonical_rows = scalar(conn, "SELECT COUNT(*) FROM fact_financial_statement_canonical WHERE source_id = ?", (companyfacts_source,))
        fin_feature_tickers = scalar(
            conn,
            f"SELECT COUNT(DISTINCT ticker) FROM feature_financial_statement WHERE source_id = ? AND model_family = ? AND ticker IN ({ph})",
            (companyfacts_source, model_family, *tickers),
        )
        fin_feature_rows = scalar(
            conn,
            "SELECT COUNT(*) FROM feature_financial_statement WHERE source_id = ? AND model_family = ?",
            (companyfacts_source, model_family),
        )
        missing_fin_issue_count = scalar(
            conn,
            f"SELECT COUNT(*) FROM data_quality_issues WHERE stage IN (?, ?) AND source_id = ? AND ticker IN ({ph})",
            (SEC_SYNC_STAGE, FIN_FEATURE_STAGE, companyfacts_source, *tickers),
        )
        if filing_tickers != len(tickers):
            missing = conn.execute(
                """
                SELECT c.ticker
                FROM dim_company c
                JOIN dim_technology_taxonomy t
                  ON t.ticker = c.ticker
                 AND t.model_family = ?
                WHERE c.is_active = 1
                  AND c.ticker NOT IN (
                    SELECT DISTINCT ticker FROM fact_sec_filing WHERE source_id = ?
                )
                ORDER BY c.ticker
                """,
                (model_family, submissions_source),
            ).fetchall()
            errors.append(f"SEC filing coverage missing tickers: {[row['ticker'] for row in missing]}")
        if filing_rows == 0:
            errors.append("No SEC filing rows loaded.")
        if accepted_rows == 0:
            errors.append("SEC filing rows do not include accepted timestamps.")
        if fact_rows == 0:
            errors.append("No SEC XBRL fact rows loaded.")
        if raw_fact_rows == 0:
            errors.append("No raw SEC XBRL fact rows loaded.")
        if canonical_rows == 0:
            errors.append("No canonical financial statement rows built.")
        if fin_feature_rows == 0:
            errors.append("No financial feature rows built.")
        if fact_tickers < fin_feature_tickers:
            errors.append(f"Financial feature ticker count exceeds XBRL fact ticker count: facts={fact_tickers} features={fin_feature_tickers}")
        if model_family == "semiconductors":
            ifrs_bad = conn.execute(
                f"""
                SELECT ticker, coverage_status
                FROM dim_issuer_reporting_profile
                WHERE ticker IN ({placeholders(sorted(EXPECTED_IFRS_RECOVERY))})
                  AND coverage_status <> 'SEC_OK_IFRS_FULL'
                ORDER BY ticker
                """,
                tuple(sorted(EXPECTED_IFRS_RECOVERY)),
            ).fetchall()
            if ifrs_bad:
                errors.append(f"Expected IFRS issuers not recovered: {[dict(row) for row in ifrs_bad]}")
            ifrs_missing_features = conn.execute(
                f"""
                SELECT ticker
                FROM dim_company
                WHERE ticker IN ({placeholders(sorted(EXPECTED_IFRS_RECOVERY))})
                  AND ticker NOT IN (
                      SELECT DISTINCT ticker
                      FROM feature_financial_statement
                      WHERE source_id = ? AND model_family = ?
                  )
                ORDER BY ticker
                """,
                (*tuple(sorted(EXPECTED_IFRS_RECOVERY)), companyfacts_source, model_family),
            ).fetchall()
            if ifrs_missing_features:
                errors.append(f"Expected IFRS issuers missing financial features: {[row['ticker'] for row in ifrs_missing_features]}")
            cbrs_status = value(conn, "SELECT coverage_status FROM dim_issuer_reporting_profile WHERE ticker = 'CBRS'")
            if cbrs_status == "SEC_NEW_ISSUER_INSUFFICIENT_FILINGS":
                cbrs_eligible = value(conn, "SELECT calibration_fundamental_eligible FROM dim_issuer_reporting_profile WHERE ticker = 'CBRS'")
                if int(cbrs_eligible or 0) != 0:
                    errors.append("CBRS should be excluded from fundamental calibration until regular financial statements exist.")
            elif cbrs_status not in {"SEC_OK_US_GAAP", "SEC_OK_IFRS_FULL"}:
                errors.append(f"CBRS expected new-issuer or recovered SEC coverage status, found {cbrs_status!r}")
        lagged_profiles = scalar(
            conn,
            "SELECT COUNT(*) FROM dim_issuer_reporting_profile WHERE companyfacts_lag_flag = 1",
        )
        calibration_excluded = scalar(
            conn,
            "SELECT COUNT(*) FROM dim_issuer_reporting_profile WHERE calibration_fundamental_eligible = 0",
        )
        inline_raw_rows = scalar(
            conn,
            "SELECT COUNT(*) FROM fact_sec_xbrl_fact_raw WHERE source_id = ? AND source_detail = ?",
            (companyfacts_source, str(cfg_get(config, "sec_fundamentals.inline_xbrl_source_detail", "inline_xbrl_fallback"))),
        )
        fx_rows = scalar(conn, "SELECT COUNT(*) FROM fact_fx_rate WHERE source_id = ?", (fx_source,))
        fx_currencies = scalar(conn, "SELECT COUNT(DISTINCT base_currency) FROM fact_fx_rate WHERE source_id = ?", (fx_source,))
        non_usd_feature_rows = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM feature_financial_statement
            WHERE source_id = ? AND model_family = ?
              AND COALESCE(reported_currency, '') NOT IN ('', 'USD')
            """,
            (companyfacts_source, model_family),
        )
        converted_non_usd_feature_rows = scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM feature_financial_statement
            WHERE source_id = ? AND model_family = ?
              AND COALESCE(reported_currency, '') NOT IN ('', 'USD')
              AND fx_conversion_status = 'converted'
            """,
            (companyfacts_source, model_family),
        )
        if non_usd_feature_rows and fx_rows == 0:
            warnings.append("Non-USD financial features exist but no FX rows are loaded; valuation ratios will remain null for those issuers.")

        direct_profile_tickers = scalar(
            conn,
            f"SELECT COUNT(*) FROM dim_insider_reporting_profile WHERE ticker IN ({ph})",
            tuple(tickers),
        )
        direct_filing_tickers = scalar(
            conn,
            f"SELECT COUNT(DISTINCT ticker) FROM fact_sec_ownership_filing WHERE source_id = ? AND parsed_successfully = 1 AND ticker IN ({ph})",
            (direct_ownership_source, *tickers),
        )
        direct_filing_rows = scalar(conn, "SELECT COUNT(*) FROM fact_sec_ownership_filing WHERE source_id = ?", (direct_ownership_source,))
        direct_nonderiv_rows = scalar(conn, "SELECT COUNT(*) FROM fact_sec_ownership_nonderivative_transaction WHERE source_id = ?", (direct_ownership_source,))
        direct_deriv_rows = scalar(conn, "SELECT COUNT(*) FROM fact_sec_ownership_derivative_transaction WHERE source_id = ?", (direct_ownership_source,))
        direct_holding_rows = scalar(conn, "SELECT COUNT(*) FROM fact_sec_ownership_holding WHERE source_id = ?", (direct_ownership_source,))
        direct_expected_missing = scalar(
            conn,
            f"""
            SELECT COUNT(*)
            FROM dim_insider_reporting_profile
            WHERE ticker IN ({ph})
              AND coverage_status IN (
                  'ownership_domestic_expected_missing_review',
                  'ownership_fpi_post_hfia_expected_direct_sec_not_found',
                  'ownership_sec_filings_found_parser_failed'
              )
            """,
            tuple(tickers),
        )
        upstream_form4_tickers = scalar(
            conn,
            f"SELECT COUNT(DISTINCT ticker) FROM fact_sec_form4_transaction WHERE source_id = ? AND ticker IN ({ph})",
            (form4_source, *tickers),
        )
        upstream_form4_rows = scalar(conn, "SELECT COUNT(*) FROM fact_sec_form4_transaction WHERE source_id = ?", (form4_source,))
        upstream_form4_rows = scalar(
            conn,
            f"SELECT COUNT(*) FROM fact_sec_form4_transaction WHERE source_id = ? AND ticker IN ({ph})",
            (form4_source, *tickers),
        )
        direct_form4_tickers = scalar(
            conn,
            f"SELECT COUNT(DISTINCT ticker) FROM fact_sec_form4_transaction WHERE source_id = ? AND ticker IN ({ph})",
            (direct_ownership_source, *tickers),
        )
        direct_form4_rows = scalar(
            conn,
            f"SELECT COUNT(*) FROM fact_sec_form4_transaction WHERE source_id = ? AND ticker IN ({ph})",
            (direct_ownership_source, *tickers),
        )
        form4_tickers = scalar(
            conn,
            f"SELECT COUNT(DISTINCT ticker) FROM fact_sec_form4_transaction WHERE source_id IN (?, ?) AND ticker IN ({ph})",
            (form4_source, direct_ownership_source, *tickers),
        )
        form4_rows = scalar(
            conn,
            f"SELECT COUNT(*) FROM fact_sec_form4_transaction WHERE source_id IN (?, ?) AND ticker IN ({ph})",
            (form4_source, direct_ownership_source, *tickers),
        )
        form4_buy_rows = scalar(
            conn,
            f"""
            SELECT COUNT(*)
            FROM fact_sec_form4_transaction
            WHERE source_id IN (?, ?)
              AND ticker IN ({ph})
              AND is_open_market_purchase = 1
            """,
            (form4_source, direct_ownership_source, *tickers),
        )
        institutional_tickers = scalar(
            conn,
            f"SELECT COUNT(DISTINCT ticker) FROM fact_13f_positioning WHERE source_id = ? AND ticker IN ({ph})",
            (mp_source, *tickers),
        )
        institutional_missing_rows = conn.execute(
            f"""
            SELECT ticker
            FROM dim_company
            WHERE is_active = 1
              AND ticker IN ({ph})
              AND ticker NOT IN (
                  SELECT DISTINCT ticker
                  FROM fact_13f_positioning
                  WHERE source_id = ?
              )
            ORDER BY ticker
            """,
            (*tickers, mp_source),
        ).fetchall()
        missing_13f_tickers = [row["ticker"] for row in institutional_missing_rows]
        unexpected_missing_13f = [ticker for ticker in missing_13f_tickers if ticker not in exempt_13f_tickers]
        active_13f_exemptions = sorted(ticker for ticker in exempt_13f_tickers if ticker in missing_13f_tickers)
        stale_13f_exemptions = sorted(ticker for ticker in exempt_13f_tickers if ticker in tickers and ticker not in missing_13f_tickers)
        short_tickers = scalar(
            conn,
            f"SELECT COUNT(DISTINCT ticker) FROM fact_short_interest WHERE source_id = ? AND ticker IN ({ph})",
            (mp_source, *tickers),
        )
        borrow_tickers = scalar(
            conn,
            f"SELECT COUNT(DISTINCT ticker) FROM fact_ibkr_borrow_snapshot WHERE source_id = ? AND ticker IN ({ph})",
            (mp_source, *tickers),
        )
        borrow_missing_rows = conn.execute(
            f"""
            SELECT ticker
            FROM dim_company
            WHERE is_active = 1
              AND ticker IN ({ph})
              AND ticker NOT IN (
                  SELECT DISTINCT ticker
                  FROM fact_ibkr_borrow_snapshot
                  WHERE source_id = ?
              )
            ORDER BY ticker
            """,
            (*tickers, mp_source),
        ).fetchall()
        missing_borrow_tickers = [row["ticker"] for row in borrow_missing_rows]
        unexpected_missing_borrow = [ticker for ticker in missing_borrow_tickers if ticker not in exempt_borrow_tickers]
        active_borrow_exemptions = sorted(ticker for ticker in exempt_borrow_tickers if ticker in missing_borrow_tickers)
        stale_borrow_exemptions = sorted(ticker for ticker in exempt_borrow_tickers if ticker in tickers and ticker not in missing_borrow_tickers)
        positioning_features = scalar(
            conn,
            f"SELECT COUNT(DISTINCT ticker) FROM feature_positioning WHERE source_id = ? AND model_family = ? AND ticker IN ({ph})",
            (positioning_source, model_family, *tickers),
        )
        missing_positioning_issue_count = scalar(
            conn,
            f"SELECT COUNT(*) FROM data_quality_issues WHERE stage = ? AND issue_type LIKE 'missing_%_upstream_rows' AND ticker IN ({ph})",
            (POSITIONING_STAGE, *tickers),
        )
        direct_ownership_issue_count = scalar(
            conn,
            f"SELECT COUNT(*) FROM data_quality_issues WHERE stage = ? AND ticker IN ({ph})",
            (DIRECT_OWNERSHIP_STAGE, *tickers),
        )
        if require_direct_ownership and direct_profile_tickers != len(tickers):
            errors.append(f"Direct ownership profile coverage mismatch: expected={len(tickers)} actual={direct_profile_tickers}")
        elif direct_profile_tickers != len(tickers):
            warnings.append(f"Direct ownership profiles are partial: {direct_profile_tickers}/{len(tickers)} tickers checked.")
        if require_direct_ownership and direct_filing_tickers == 0:
            errors.append("Direct SEC ownership coverage is required but no parsed Forms 3/4/5 filings were loaded.")
        if form4_rows == 0:
            errors.append("No Form 4 transactions imported from upstream or direct SEC ownership.")
        if form4_buy_rows == 0:
            warnings.append("No open-market Form 4 purchase rows imported for the current universe.")
        if require_13f and unexpected_missing_13f:
            errors.append(f"13F coverage required; missing non-exempt tickers: {unexpected_missing_13f}")
        if require_13f and active_13f_exemptions:
            warnings.append(f"13F required with active new-issuer exemptions: {active_13f_exemptions}")
        if require_13f and stale_13f_exemptions:
            warnings.append(f"13F exemption can be removed; rows now exist for: {stale_13f_exemptions}")
        if require_short and short_tickers != len(tickers):
            errors.append(f"Short-interest coverage required but only {short_tickers}/{len(tickers)} tickers have rows.")
        if require_borrow and unexpected_missing_borrow:
            errors.append(
                f"Borrow coverage required; missing non-exempt tickers: {unexpected_missing_borrow}"
            )
        if require_borrow and active_borrow_exemptions:
            warnings.append(f"Borrow required with active broker-contract exemptions: {active_borrow_exemptions}")
        if require_borrow and stale_borrow_exemptions:
            warnings.append(f"Borrow exemption can be removed; rows now exist for: {stale_borrow_exemptions}")
        if positioning_features != len(tickers):
            errors.append(f"Positioning feature coverage mismatch: expected={len(tickers)} actual={positioning_features}")

        warnings.append(f"Universe tickers={len(tickers)}")
        warnings.append(f"SEC filings rows={filing_rows} covered_tickers={filing_tickers} accepted_timestamp_rows={accepted_rows}")
        warnings.append(f"SEC raw XBRL facts rows={raw_fact_rows} covered_tickers={raw_fact_tickers}")
        warnings.append(f"SEC XBRL facts rows={fact_rows} covered_tickers={fact_tickers}")
        warnings.append(f"Canonical financial rows={canonical_rows} covered_tickers={canonical_tickers}")
        warnings.append(f"Financial feature rows={fin_feature_rows} covered_tickers={fin_feature_tickers} financial_issues={missing_fin_issue_count}")
        warnings.append(f"SEC companyfacts lagged_profiles={lagged_profiles} inline_fallback_raw_rows={inline_raw_rows} calibration_excluded={calibration_excluded}")
        warnings.append(f"FX rows={fx_rows} currencies={fx_currencies} non_usd_features_converted={converted_non_usd_feature_rows}/{non_usd_feature_rows}")
        warnings.append(f"Direct SEC ownership profiles={direct_profile_tickers} filing_rows={direct_filing_rows} filing_tickers={direct_filing_tickers} nonderiv_rows={direct_nonderiv_rows} deriv_rows={direct_deriv_rows} holding_rows={direct_holding_rows} expected_missing={direct_expected_missing} issues={direct_ownership_issue_count} required={require_direct_ownership}")
        warnings.append(f"Form 4 rows={form4_rows} covered_tickers={form4_tickers} open_market_purchase_rows={form4_buy_rows} upstream_rows={upstream_form4_rows}/{upstream_form4_tickers} direct_rows={direct_form4_rows}/{direct_form4_tickers}")
        warnings.append(f"13F covered_tickers={institutional_tickers} required={require_13f} missing={missing_13f_tickers} exempt_missing={active_13f_exemptions}")
        warnings.append(f"Short-interest covered_tickers={short_tickers} required={require_short}")
        warnings.append(f"Borrow covered_tickers={borrow_tickers} required={require_borrow} missing={missing_borrow_tickers} exempt_missing={active_borrow_exemptions}")
        warnings.append(f"Positioning feature covered_tickers={positioning_features} missing_upstream_issues={missing_positioning_issue_count}")

    for message in warnings:
        LOGGER.info(message)
    if errors:
        for message in errors:
            LOGGER.error(message)
        return 1
    LOGGER.info("Technology Stage 4/5 validation passed for model_family=%s", model_family)
    return 0


if __name__ == "__main__":
    raise SystemExit(validate())

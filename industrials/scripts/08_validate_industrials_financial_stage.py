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
from industrials.core.policy_loader import load_eligibility_policy  # noqa: E402
from industrials.core.profiles import FPI_HYBRID_PROFILES, VALID_REPORTING_PROFILES  # noqa: E402
from industrials.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("validate_industrials_financial_stage")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
FEATURE_STAGE = "build_industrials_financial_features"
DEFAULT_MAX_NEUTRAL_ROW_FRACTION = 0.5
# FN-14: unsuffixed TTM/net_cash columns hold local reported-currency values;
# the *_usd variants carry the USD conversion (TTM-window-average FX).
TTM_USD_COLUMN_PAIRS = [
    ("revenue_ttm", "revenue_ttm_usd"),
    ("gross_profit_ttm", "gross_profit_ttm_usd"),
    ("operating_income_ttm", "operating_income_ttm_usd"),
    ("net_income_ttm", "net_income_ttm_usd"),
    ("free_cash_flow_ttm", "free_cash_flow_ttm_usd"),
    ("depreciation_and_amortization_ttm", "depreciation_and_amortization_ttm_usd"),
        ("interest_expense_ttm", "interest_expense_ttm_usd"),
        ("equity_issuance_proceeds_ttm", "equity_issuance_proceeds_ttm_usd"),
        ("debt_issuance_proceeds_ttm", "debt_issuance_proceeds_ttm_usd"),
        ("orders_ttm", "orders_ttm_usd"),
    ("net_cash", "net_cash_usd"),
]
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


def resolve_eligibility_policy_path(config: dict[str, Any], *, base_dir: Path, model_family: str) -> Path:
    family_key = f"scoring_policy.families.{model_family}.eligibility_policy_csv"
    policy_path_raw = str(cfg_get(config, family_key, "") or "").strip()
    if not policy_path_raw and model_family == "defense":
        policy_path_raw = str(cfg_get(config, "scoring_policy.defense_eligibility_policy_csv", "") or "").strip()
    if not policy_path_raw:
        raise ValueError(f"Missing eligibility policy CSV config for model_family={model_family}: set {family_key}")
    policy_path = resolve_path(policy_path_raw, base_dir=base_dir)
    if not policy_path.exists():
        raise FileNotFoundError(f"Eligibility policy CSV configured at {family_key} does not exist: {policy_path}")
    return policy_path


def check_policy_profile_coverage(config: dict[str, Any], *, base_dir: Path, model_family: str, asof: date, errors: list[str]) -> None:
    """Cross-check the scoring eligibility policy CSV against the shared profile vocabulary.

    Every profile stage 07 can emit must have at least one policy row; unknown
    profile names in the CSV are typos or vocabulary drift. Both fail loudly.
    Coverage is evaluated at the validation asof (NEW-2): the loader selects
    the policy rows in force at that date (valid_from same-day inclusive), so
    versioned policy keys load deterministically instead of raising.
    """
    policy_path = resolve_eligibility_policy_path(config, base_dir=base_dir, model_family=model_family)
    policies = load_eligibility_policy(policy_path, asof=asof)
    policy_profiles = {str(key[0]) for key in policies}
    uncovered = sorted(VALID_REPORTING_PROFILES.difference(policy_profiles))
    if uncovered:
        errors.append(f"Eligibility policy CSV {policy_path} has no rows for reporting profiles: {uncovered}")
    unknown = sorted(policy_profiles.difference(VALID_REPORTING_PROFILES))
    if unknown:
        errors.append(f"Eligibility policy CSV {policy_path} contains unknown reporting profiles: {unknown}")


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
    asof_text = str(args.asof or "").strip()
    requested_asof = parse_date(asof_text)
    if asof_text and requested_asof is None:
        # Align with siblings 04/06/07/09/10: a malformed operator date must
        # raise, never silently validate at the latest feature asof.
        raise ValueError(f"Unparseable --asof value: {args.asof!r}; expected YYYY-MM-DD.")

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

        feature_asof = (requested_asof.isoformat() if requested_asof is not None else "") or value(
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
        latest_feature_asof = value(
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

        # NEW-2: pass the validation asof so versioned policy keys resolve to
        # the row in force at this asof instead of raising on load.
        check_policy_profile_coverage(config, base_dir=base_dir, model_family=model_family, asof=audit_asof, errors=errors)

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
            if str(row["reporting_profile"] or "") not in VALID_REPORTING_PROFILES
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
        recent_stub_missing_observation = []
        fpi_hybrid_gate_errors = []
        spinoff_bridge_gate_errors = []
        ttm_usd_gap_errors = []
        ttm_usd_gap_warnings = []
        for row in feature_rows:
            ticker = str(row["ticker"])
            status = str(row["data_quality_status"] or "")
            review_reason = str(row["review_reason"] or "")
            reporting_profile = str(row["reporting_profile"] or "").upper()
            if status != "complete":
                review_rows.append(f"{ticker}:{status}:{review_reason}")
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
            # FN-14: local and USD TTM/net_cash columns must move together for
            # USD-native rows (local == USD, so a gap is a writer bug). For
            # converted rows a missing TTM-window-average FX rate can
            # legitimately leave *_usd NULL, so that is visibility-only.
            for local_col, usd_col in TTM_USD_COLUMN_PAIRS:
                if row[local_col] is not None and row[usd_col] is None:
                    if fx_status == "usd_native":
                        ttm_usd_gap_errors.append(f"{ticker}:{usd_col}")
                    elif fx_status == "converted_to_usd":
                        ttm_usd_gap_warnings.append(f"{ticker}:{usd_col}")
            if reporting_profile == "RECENT_PUBLIC_STUB" and "revenue_not_annual" in review_reason:
                missing_stub_fields = [
                    field
                    for field in ["revenue_stub_annualized", "revenue_stub_period_days", "revenue_stub_quality"]
                    if row[field] is None or str(row[field]).strip() == ""
                ]
                if fx_status != "missing_fx_rate" and row["revenue_stub_annualized_usd"] is None:
                    missing_stub_fields.append("revenue_stub_annualized_usd")
                if missing_stub_fields:
                    recent_stub_missing_observation.append(f"{ticker}:{','.join(missing_stub_fields)}")
            if reporting_profile in FPI_HYBRID_PROFILES:
                if currency != "USD" and (
                    fx_status != "converted_to_usd"
                    or row["fx_rate_income_statement"] is None
                    or row["fx_rate_balance_sheet"] is None
                ):
                    fpi_hybrid_gate_errors.append(f"{ticker}:missing_pit_fx_{currency}_USD")
                if reporting_profile == "FPI_HYBRID_STUB_LOADED" and "revenue_not_annual" in review_reason:
                    missing_stub_fields = [
                        field
                        for field in ["revenue_stub_annualized", "revenue_stub_annualized_usd", "revenue_stub_period_days", "revenue_stub_quality"]
                        if row[field] is None or str(row[field]).strip() == ""
                    ]
                    if missing_stub_fields:
                        fpi_hybrid_gate_errors.append(f"{ticker}:missing_stub_fields={','.join(missing_stub_fields)}")
                    # FN-14: revenue_ttm is the LOCAL-currency TTM; it is the
                    # correct availability probe here (a full-year window
                    # exists or not, independent of FX; FX gaps are gated
                    # separately above).
                    if status == "complete" and row["revenue_ttm"] is None:
                        fpi_hybrid_gate_errors.append(f"{ticker}:stub_only_row_marked_complete")
                if reporting_profile == "FPI_HYBRID_LOADED":
                    if status != "complete":
                        fpi_hybrid_gate_errors.append(f"{ticker}:loaded_profile_not_complete status={status} reason={review_reason}")
                    if row["revenue_ttm"] is None and "revenue_not_annual" in review_reason:
                        fpi_hybrid_gate_errors.append(f"{ticker}:loaded_profile_still_stub_only")
            if reporting_profile == "SPINOFF_SEGMENT_BRIDGE_REVIEW" and status == "complete":
                spinoff_bridge_gate_errors.append(f"{ticker}:bridge_review_profile_marked_complete")
        if bad_feature_confidence:
            errors.append(f"Feature financial confidence outside [0,1]: {bad_feature_confidence}")
        if future_periods:
            errors.append(f"Financial features have fiscal period ends after asof={audit_asof.isoformat()}: {future_periods}")
        if complete_missing_core:
            errors.append(f"Complete financial rows missing core USD fields: {complete_missing_core}")
        if recent_stub_missing_observation:
            errors.append(f"Recent public stub rows with interim revenue lack explicit stub observation fields: {recent_stub_missing_observation}")
        if fpi_hybrid_gate_errors:
            errors.append(f"FPI hybrid profile gate failures: {fpi_hybrid_gate_errors}")
        if spinoff_bridge_gate_errors:
            errors.append(f"Spinoff segment bridge profile gate failures: {spinoff_bridge_gate_errors}")
        if ttm_usd_gap_errors:
            errors.append(f"USD-native rows missing *_usd TTM/net_cash values (FN-14 writer contract): {ttm_usd_gap_errors}")
        if ttm_usd_gap_warnings:
            warnings.append(f"Converted rows with local TTM/net_cash but no USD conversion (TTM-window FX unavailable): {ttm_usd_gap_warnings}")
        if non_usd_missing_fx:
            warnings.append(f"Non-USD rows missing FX and held in review: {non_usd_missing_fx}")
        if fallback_rows and args.strict_fallbacks:
            errors.append(f"Neutral-low-confidence fallback rows present under --strict-fallbacks: {fallback_rows}")
        max_neutral_fraction = float(
            cfg_get(config, "financial_validation.max_neutral_row_fraction", DEFAULT_MAX_NEUTRAL_ROW_FRACTION)
        )
        if feature_rows:
            neutral_fraction = len(fallback_rows) / len(feature_rows)
            if neutral_fraction > max_neutral_fraction:
                errors.append(
                    "Neutral-low-confidence fallback rows exceed the allowed panel fraction: "
                    f"{len(fallback_rows)}/{len(feature_rows)}={neutral_fraction:.3f} > "
                    f"max_neutral_row_fraction={max_neutral_fraction:.3f}"
                )

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
            archive_qc_rows = conn.execute(
                f"""
                SELECT DISTINCT c.ticker, c.canonical_metric, c.period_end,
                       c.accession_number, SUBSTR(r.payload_json, 1, 220) AS payload
                FROM fact_financial_statement_canonical c
                JOIN fact_sec_xbrl_fact f
                  ON f.ticker = c.ticker
                 AND f.source_id = c.source_id
                 AND f.canonical_metric = c.canonical_metric
                 AND f.period_end = c.period_end
                 AND COALESCE(f.accession_number, '') = COALESCE(c.accession_number, '')
                 AND COALESCE(f.unit, '') = COALESCE(c.unit, '')
                JOIN fact_sec_xbrl_fact_raw r
                  ON r.raw_fact_id = f.raw_fact_id
                WHERE c.source_id = ?
                  AND c.model_family = ?
                  AND c.ticker IN ({usable_ph})
                  AND c.period_end <= ?
                  AND (
                        CASE
                            WHEN COALESCE(c.accepted_at, '') GLOB '????-??-??*' THEN SUBSTR(c.accepted_at, 1, 10)
                            WHEN COALESCE(c.accepted_at, '') GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]*'
                                THEN SUBSTR(c.accepted_at, 1, 4) || '-' || SUBSTR(c.accepted_at, 5, 2) || '-' || SUBSTR(c.accepted_at, 7, 2)
                            ELSE c.filing_date
                        END
                  ) <= ?
                  AND f.source_detail = 'sec_archive_text_table_mapped'
                  AND (
                        r.payload_json LIKE '%"period_confidence":"fallback_filing_or_report_date"%'
                     OR r.payload_json LIKE '%"scale_confidence":"low"%'
                  )
                ORDER BY c.ticker, c.canonical_metric, c.period_end
                LIMIT 20
                """,
                (
                    source_id,
                    model_family,
                    *usable_profiles,
                    audit_asof.isoformat(),
                    audit_asof.isoformat(),
                ),
            ).fetchall()
            if archive_qc_rows:
                warnings.append(f"Archive text-table QC warnings: {[dict(row) for row in archive_qc_rows]}")
            warnings.append(f"Usable XBRL profiles={len(usable_profiles)} raw_facts={raw_count} mapped_facts={mapped_count} canonical_facts={canonical_count}")
        else:
            active_filer_rows = conn.execute(
                f"""
                SELECT ticker
                FROM dim_company
                WHERE is_active = 1
                  AND COALESCE(cik, '') <> ''
                  AND ticker IN ({ph})
                ORDER BY ticker
                """,
                (*universe,),
            ).fetchall()
            active_filers = [str(row["ticker"]) for row in active_filer_rows]
            if active_filers:
                errors.append(
                    "No usable SEC XBRL profiles found although the universe contains "
                    f"{len(active_filers)} active SEC filers with CIKs (stage 07 never ran or its facts were wiped): "
                    f"{active_filers}"
                )
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

        review_issue_rows = conn.execute(
            f"""
            SELECT DISTINCT ticker
            FROM data_quality_issues
            WHERE stage = ?
              AND issue_type = 'financial_feature_review'
              AND resolution_status = 'open'
              AND ticker IN ({ph})
            """,
            (FEATURE_STAGE, *universe),
        ).fetchall()
        review_issue_tickers = {str(row["ticker"]) for row in review_issue_rows}
        out_of_universe_issue_rows = conn.execute(
            f"""
            SELECT DISTINCT ticker
            FROM data_quality_issues
            WHERE stage = ?
              AND issue_type = 'financial_feature_review'
              AND resolution_status = 'open'
              AND ticker NOT IN ({ph})
            """,
            (FEATURE_STAGE, *universe),
        ).fetchall()
        out_of_universe_issue_tickers = sorted(str(row["ticker"]) for row in out_of_universe_issue_rows)
        # review_rows holds "ticker:status:reason" strings; recover the ticker key.
        review_feature_tickers = {entry.split(":", 1)[0] for entry in review_rows}
        at_latest_asof = audit_asof.isoformat() == str(latest_feature_asof or "").strip()
        if at_latest_asof:
            if review_feature_tickers != review_issue_tickers:
                missing_issues = sorted(review_feature_tickers.difference(review_issue_tickers))
                stale_issues = sorted(review_issue_tickers.difference(review_feature_tickers))
                errors.append(
                    "Financial review issue mismatch: "
                    f"review_features={len(review_feature_tickers)} issues={len(review_issue_tickers)} "
                    f"missing_issues={missing_issues} stale_issues={stale_issues}"
                )
            if out_of_universe_issue_tickers:
                errors.append(
                    "Open financial review issues exist for tickers outside the "
                    f"{model_family} universe (resolve or reassign them): {out_of_universe_issue_tickers}"
                )
        elif review_rows or review_issue_tickers or out_of_universe_issue_tickers:
            warnings.append(
                "Skipped financial review issue parity for historical asof="
                f"{audit_asof.isoformat()}; data_quality_issues stores the latest build state, not an immutable as-of ledger."
            )

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

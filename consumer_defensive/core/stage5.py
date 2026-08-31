"""Stage 5 ownership/positioning validation and foundation coverage audit."""

from __future__ import annotations

import bisect
import csv
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .atomic_io import atomic_text_writer
from .config import ConfigBundle, cfg_get, resolve_path
from .db import utc_now
from .source_registry import load_source_registry, upsert_source_registry
from .stage5_import import (
    POSITIONING_DEFINITION_VERSION,
    _ro_connect,
    _source_birthdate,
    _universe,
)
from .stage5_schema import STAGE5_MIGRATION_SHA256, STAGE5_SCHEMA_VERSION, ensure_stage5_schema
from .universe import normalize_ticker


SOURCE_KEYS = ("sec_form4", "institutional_13f", "short_interest", "borrow")


def _fraction(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _max_age(bundle: ConfigBundle, key: str) -> int | None:
    raw = cfg_get(bundle.payload, f"positioning.maximum_age_days.{key}")
    if raw in (None, ""):
        return None
    value = int(raw)
    if value < 0:
        raise ValueError(f"positioning.maximum_age_days.{key} cannot be negative")
    return value


def bootstrap_stage5(conn: sqlite3.Connection, bundle: ConfigBundle) -> None:
    """Ensure schema and install the explicit Stage 5 source-birthdate contract."""

    ensure_stage5_schema(conn)
    configured_source_ids = {
        str(cfg_get(bundle.payload, "positioning.ownership_source_id")),
        str(cfg_get(bundle.payload, "positioning.market_positioning_source_id")),
        str(cfg_get(bundle.payload, "positioning.feature_source_id")),
    }
    registry_path = resolve_path(
        cfg_get(bundle.payload, "source_registry.path"),
        base_dir=bundle.base_dir,
    )
    registry_rows = [
        row for row in load_source_registry(registry_path)
        if row.source_id in configured_source_ids
    ]
    missing_sources = configured_source_ids - {row.source_id for row in registry_rows}
    if missing_sources:
        raise RuntimeError(f"Stage 5 source IDs are absent from the registry: {sorted(missing_sources)}")
    upsert_source_registry(conn, registry_rows)
    source_ids = {
        "sec_form4": str(cfg_get(bundle.payload, "positioning.ownership_source_id")),
        "institutional_13f": str(cfg_get(bundle.payload, "positioning.market_positioning_source_id")),
        "short_interest": str(cfg_get(bundle.payload, "positioning.market_positioning_source_id")),
        "borrow": str(cfg_get(bundle.payload, "positioning.market_positioning_source_id")),
    }
    required = {
        "sec_form4": False,
        "institutional_13f": True,
        "short_interest": True,
        "borrow": False,
    }
    semantics = {
        "sec_form4": "visible_at_sec_acceptance_timestamp; no transaction is not numeric zero",
        "institutional_13f": "visible_at_latest_manager_filing_date for reporting-period aggregate",
        "short_interest": "visible_at_publication_date; settlement_date is not availability",
        "borrow": "visible_at_observed_asof_date; no pre-birthdate backfill",
    }
    now = utc_now()
    records = [
        (
            key,
            source_ids[key],
            _source_birthdate(bundle, key),
            int(required[key]),
            _max_age(bundle, key),
            semantics[key],
            "Missing-era observations remain NULL/unavailable, never zero.",
            now,
        )
        for key in SOURCE_KEYS
    ]
    with conn:
        conn.executemany(
            """
            INSERT INTO stage5_source_contract(
                source_key,source_id,source_birthdate,required_for_gate,
                maximum_age_days,availability_semantics,notes,updated_at
            ) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(source_key) DO UPDATE SET
                source_id=excluded.source_id,
                source_birthdate=excluded.source_birthdate,
                required_for_gate=excluded.required_for_gate,
                maximum_age_days=excluded.maximum_age_days,
                availability_semantics=excluded.availability_semantics,
                notes=excluded.notes,
                updated_at=excluded.updated_at
            """,
            records,
        )


def _reviewed_positioning_identifiers(
    bundle: ConfigBundle,
) -> dict[str, dict[str, Any]]:
    path = resolve_path(
        cfg_get(bundle.payload, "positioning.source_identifier_map"),
        base_dir=bundle.base_dir,
    )
    if not path.is_file():
        raise FileNotFoundError(f"Reviewed Stage 5 source-identifier map is missing: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "ticker",
            "cusip",
            "finra_symbol",
            "review_status",
            "review_reason",
        }
        if set(reader.fieldnames or ()) != required:
            raise ValueError(
                "Stage 5 source-identifier map must contain exactly "
                f"{sorted(required)}; got {sorted(reader.fieldnames or ())}."
            )
        records: dict[str, dict[str, Any]] = {}
        finra_owners: dict[str, str] = {}
        for position, row in enumerate(reader, start=2):
            ticker = normalize_ticker(row.get("ticker"))
            if not ticker or ticker in records:
                raise ValueError(
                    f"Missing or duplicate Stage 5 identifier ticker at CSV row {position}: {ticker!r}"
                )
            if str(row.get("review_status") or "").strip() != "reviewed":
                raise ValueError(f"Stage 5 identifier row is not reviewed: {ticker}")
            if not str(row.get("review_reason") or "").strip():
                raise ValueError(f"Stage 5 identifier row has no review reason: {ticker}")
            cusip = str(row.get("cusip") or "").strip().upper().replace(" ", "")
            if cusip and (len(cusip) != 9 or not cusip.isalnum()):
                raise ValueError(f"Invalid reviewed 13F CUSIP for {ticker}: {cusip!r}")
            finra_symbols = tuple(
                dict.fromkeys(
                    symbol
                    for symbol in (
                        normalize_ticker(value)
                        for value in str(row.get("finra_symbol") or "").split(";")
                    )
                    if symbol
                )
            )
            if not cusip and not finra_symbols:
                raise ValueError(f"Stage 5 identifier row has no source identifier: {ticker}")
            for finra_symbol in finra_symbols:
                prior_owner = finra_owners.get(finra_symbol)
                if prior_owner is not None and prior_owner != ticker:
                    raise ValueError(
                        f"Reviewed FINRA symbol {finra_symbol} maps to multiple "
                        f"canonical tickers: {prior_owner}, {ticker}"
                    )
                finra_owners[finra_symbol] = ticker
            records[ticker] = {"cusip": cusip, "finra_symbols": finra_symbols}
    return records


def build_positioning_universe_rows(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
) -> list[dict[str, Any]]:
    """Build the neutral-upstream universe without importing another sector package."""

    reviewed = _reviewed_positioning_identifiers(bundle)
    universe = _universe(conn)
    universe_tickers = {str(row["ticker"]) for row in universe}
    unknown = sorted(set(reviewed) - universe_tickers)
    if unknown:
        raise ValueError(
            f"Reviewed Stage 5 source identifiers reference out-of-scope tickers: {unknown}"
        )
    rows: list[dict[str, Any]] = []
    for row in universe:
        ticker = str(row["ticker"])
        source_identifiers = reviewed.get(ticker, {})
        identifiers = {
            str(item["identifier_type"]).lower(): str(item["identifier_value"])
            for item in conn.execute(
                "SELECT identifier_type,identifier_value FROM dim_identifier WHERE company_id=?",
                (row["company_id"],),
            )
        }
        finra_symbols = tuple(source_identifiers.get("finra_symbols") or (ticker,))
        for finra_symbol in finra_symbols:
            rows.append({
                "ticker": ticker,
                "internal_ticker": ticker,
                "exchange_ticker": ticker,
                "company_name": str(row["company_name"] or ""),
                "issuer_name": str(row["company_name"] or ""),
                "cik": str(row["cik"] or ""),
                "cusip": source_identifiers.get("cusip") or identifiers.get("cusip", ""),
                "finra_symbol": finra_symbol,
                "exchange": str(row["exchange"] or ""),
                "ibkr_ticker": str(row["provider_price_symbol"] or ticker).replace("-", " "),
                "membership_start_date": str(row["first_membership_date"] or ""),
                "membership_end_date": str(row["last_membership_end"] or ""),
                "listing_status": str(row["listing_status"] or ""),
                "source_membership": "consumer_defensive_point_in_time",
            })
    return rows


def write_positioning_universe_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "ticker", "internal_ticker", "exchange_ticker", "company_name", "issuer_name",
        "cik", "cusip", "finra_symbol", "exchange", "ibkr_ticker", "membership_start_date",
        "membership_end_date", "listing_status", "source_membership",
    ]
    with atomic_text_writer(path, encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="raise", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def audit_upstream_positioning(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    as_of: str,
) -> dict[str, Any]:
    """Read-only upstream coverage audit used before the owner pipeline is run."""

    tickers = {str(row["ticker"]) for row in _universe(conn)}
    path = resolve_path(cfg_get(bundle.payload, "positioning.market_positioning_upstream_db"), base_dir=bundle.base_dir)
    specs = {
        "institutional_13f": (
            "institutional_13f_ownership_snapshots",
            "asof_date",
            _source_birthdate(bundle, "institutional_13f"),
        ),
        "short_interest": (
            "short_interest_snapshots",
            "publication_date",
            _source_birthdate(bundle, "short_interest"),
        ),
        "borrow": (
            "ibkr_borrow_fee_rate_daily",
            "asof_date",
            _source_birthdate(bundle, "borrow"),
        ),
    }
    result: dict[str, Any] = {"database": str(path), "as_of": as_of, "universe_tickers": len(tickers)}
    with _ro_connect(path) as upstream:
        for key, (table, date_col, birthdate) in specs.items():
            upstream_source = str(cfg_get(bundle.payload, f"positioning.upstream_source_names.{key}"))
            by_ticker: dict[str, tuple[int, str, str]] = {}
            for row in upstream.execute(
                f"""SELECT ticker,COUNT(*) n,MIN({date_col}) min_date,MAX({date_col}) max_date
                     FROM {table} WHERE {date_col}>=? AND {date_col}<=? AND source=?
                     GROUP BY ticker""",
                (birthdate, as_of, upstream_source),
            ):
                ticker = normalize_ticker(row["ticker"])
                if ticker in tickers:
                    by_ticker[ticker] = (int(row["n"]), str(row["min_date"]), str(row["max_date"]))
            result[key] = {
                "covered_tickers": len(by_ticker),
                "rows": sum(item[0] for item in by_ticker.values()),
                "missing_tickers": sorted(tickers - set(by_ticker)),
                "source_birthdate": birthdate,
            }
        result["feed_state"] = [
            dict(row)
            for row in upstream.execute(
                "SELECT feed_name,last_success_at,history_start_date,row_count,message "
                "FROM market_positioning_feed_state ORDER BY feed_name"
            )
        ]
    return result


def _check(checks: list[dict[str, Any]], name: str, passed: bool, **details: Any) -> None:
    checks.append({"check": name, "passed": bool(passed), **details})


def _current_tickers(conn: sqlite3.Connection, as_of: str) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            """
            SELECT DISTINCT ticker FROM dim_universe_membership
            WHERE model_family='consumer_defensive'
              AND start_date<=? AND COALESCE(end_date,'9999-12-31')>=?
              AND (live_investable_flag=1 OR historical_calibration_eligible_flag=1)
            ORDER BY ticker
            """,
            (as_of, as_of),
        )
    ]


def _fresh_ticker_count(
    conn: sqlite3.Connection,
    *,
    table: str,
    available_column: str,
    source_id: str,
    tickers: list[str],
    as_of: str,
    max_age_days: int | None,
    value_column: str | None = None,
) -> tuple[int, list[str]]:
    if not tickers:
        return 0, []
    floor = "0001-01-01" if max_age_days is None else (date.fromisoformat(as_of) - timedelta(days=max_age_days)).isoformat()
    placeholders = ",".join("?" for _ in tickers)
    value_predicate = f" AND {value_column} IS NOT NULL" if value_column else ""
    covered = {
        str(row[0])
        for row in conn.execute(
            f"""SELECT DISTINCT ticker FROM {table}
                 WHERE source_id=? AND ticker IN ({placeholders})
                   AND {available_column}>=? AND {available_column}<=?
                   {value_predicate}""",
            (source_id, *tickers, floor, as_of),
        )
    }
    return len(covered), sorted(set(tickers) - covered)


def validate_stage5(conn: sqlite3.Connection, bundle: ConfigBundle, *, as_of: str) -> dict[str, Any]:
    """Validate Stage 5 PIT identity, birthdates, coverage, and feature completeness."""

    bootstrap_stage5(conn, bundle)
    checks: list[dict[str, Any]] = []
    source_id = str(cfg_get(bundle.payload, "positioning.market_positioning_source_id"))
    feature_source = str(cfg_get(bundle.payload, "positioning.feature_source_id"))
    current = _current_tickers(conn, as_of)
    expected_total = len(_universe(conn))
    _check(checks, "current_pit_universe_nonempty", bool(current), current_tickers=len(current))
    _check(checks, "stage5_migration_current", conn.execute("SELECT MAX(migration_version) FROM stage5_schema_migrations").fetchone()[0] == STAGE5_SCHEMA_VERSION, expected=STAGE5_SCHEMA_VERSION, sha256=STAGE5_MIGRATION_SHA256)
    contract_count = int(conn.execute("SELECT COUNT(*) FROM stage5_source_contract").fetchone()[0])
    _check(checks, "source_birthdates_explicit", contract_count == len(SOURCE_KEYS), rows=contract_count)

    future = {
        "ownership": int(conn.execute("SELECT COUNT(*) FROM fact_sec_ownership_transaction WHERE accepted_at>?", (f"{as_of}T23:59:59Z",)).fetchone()[0]),
        "13f": int(conn.execute("SELECT COUNT(*) FROM fact_13f_positioning WHERE publication_date>?", (as_of,)).fetchone()[0]),
        "short": int(conn.execute("SELECT COUNT(*) FROM fact_short_interest WHERE publication_date>?", (as_of,)).fetchone()[0]),
        "borrow": int(conn.execute("SELECT COUNT(*) FROM fact_borrow_snapshot WHERE asof_date>?", (as_of,)).fetchone()[0]),
    }
    _check(checks, "no_future_positioning_observations", sum(future.values()) == 0, counts=future)
    birth_violations = {
        "13f": int(conn.execute("SELECT COUNT(*) FROM fact_13f_positioning WHERE asof_date<source_birthdate").fetchone()[0]),
        "short": int(conn.execute("SELECT COUNT(*) FROM fact_short_interest WHERE publication_date<source_birthdate").fetchone()[0]),
        "borrow": int(conn.execute("SELECT COUNT(*) FROM fact_borrow_snapshot WHERE asof_date<source_birthdate").fetchone()[0]),
    }
    _check(checks, "source_birthdates_enforced", sum(birth_violations.values()) == 0, counts=birth_violations)
    identity_violations = {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE LENGTH(COALESCE(source_observation_id,''))<>64").fetchone()[0])
        for table in ("fact_sec_ownership_transaction", "fact_13f_positioning", "fact_short_interest", "fact_borrow_snapshot")
    }
    _check(checks, "source_observation_ids_complete", sum(identity_violations.values()) == 0, counts=identity_violations)
    ownership_source = str(cfg_get(bundle.payload, "positioning.ownership_source_id"))
    ownership_tickers = {
        str(row[0])
        for row in conn.execute(
            """SELECT DISTINCT ticker FROM fact_sec_ownership_transaction
               WHERE source_id=? AND is_current_truth=1 AND accepted_at<=?""",
            (ownership_source, f"{as_of}T23:59:59Z"),
        )
    }
    taxonomy_tickers = {str(row["ticker"]) for row in _universe(conn)}
    _check(
        checks,
        "sec_ownership_transaction_import_present",
        bool(ownership_tickers),
        transaction_tickers=len(ownership_tickers),
        tickers_without_eligible_purchase_or_sale=sorted(
            taxonomy_tickers - ownership_tickers
        ),
    )
    _check(
        checks,
        "sec_ownership_tickers_in_scope",
        ownership_tickers.issubset(taxonomy_tickers),
        unexpected_tickers=sorted(ownership_tickers - taxonomy_tickers),
    )
    profile_flags = {
        str(row["ticker"]): int(row["foreign_issuer_flag"])
        for row in conn.execute(
            "SELECT ticker,foreign_issuer_flag FROM dim_issuer_reporting_profile"
        )
        if str(row["ticker"]) in set(current)
    }
    missing_profiles = sorted(set(current) - set(profile_flags))
    _check(
        checks,
        "sec_form4_applicability_profiles_complete",
        not missing_profiles,
        missing_tickers=missing_profiles,
    )
    form4_applicable = sorted(
        ticker for ticker in current if profile_flags.get(ticker) == 0
    )
    form4_not_applicable = sorted(
        ticker for ticker in current if profile_flags.get(ticker) == 1
    )
    form4_coverage = {
        str(row[0])
        for row in conn.execute(
            """SELECT ticker FROM fact_sec_ownership_issuer_coverage
               WHERE source_id=? AND asof_date=? AND submission_count>0""",
            (ownership_source, as_of),
        )
    }
    covered_applicable = sorted(set(form4_applicable) & form4_coverage)
    missing_applicable = sorted(set(form4_applicable) - form4_coverage)
    _check(
        checks,
        "sec_form4_applicable_current_coverage",
        not missing_applicable and bool(form4_applicable),
        covered=len(covered_applicable),
        eligible=len(form4_applicable),
        fraction=_fraction(len(covered_applicable), len(form4_applicable)),
        missing=missing_applicable,
        foreign_private_issuer_not_applicable=form4_not_applicable,
        transaction_tickers=len(ownership_tickers & set(form4_applicable)),
    )

    coverage_specs = {
        "institutional_13f": (
            "fact_13f_positioning",
            "publication_date",
            "institutional_ownership_delta_pct",
        ),
        "short_interest": (
            "fact_short_interest",
            "publication_date",
            "COALESCE(short_interest,days_to_cover)",
        ),
        "borrow": ("fact_borrow_snapshot", "asof_date", "borrow_fee"),
    }
    coverage: dict[str, Any] = {}
    for key, (table, date_col, value_col) in coverage_specs.items():
        covered, missing = _fresh_ticker_count(
            conn,
            table=table,
            available_column=date_col,
            source_id=source_id,
            tickers=current,
            as_of=as_of,
            max_age_days=_max_age(bundle, key),
            value_column=value_col,
        )
        threshold = float(cfg_get(bundle.payload, f"positioning.minimum_current_coverage.{key}"))
        fraction = _fraction(covered, len(current))
        coverage[key] = {"covered": covered, "eligible": len(current), "fraction": fraction, "threshold": threshold, "missing": missing}
        _check(checks, f"{key}_current_coverage", fraction >= threshold, **coverage[key])
    short_float_covered, short_float_missing = _fresh_ticker_count(
        conn,
        table="fact_short_interest",
        available_column="publication_date",
        source_id=source_id,
        tickers=current,
        as_of=as_of,
        max_age_days=_max_age(bundle, "short_interest"),
        value_column="short_float_pct",
    )
    coverage["short_float_pct"] = {
        "covered": short_float_covered,
        "eligible": len(current),
        "fraction": _fraction(short_float_covered, len(current)),
        "missing": short_float_missing,
        "gating": False,
        "reason": "Days-to-cover remains a usable short signal when no safe PIT float proxy exists.",
    }

    feature_rows = int(conn.execute("SELECT COUNT(*) FROM feature_positioning WHERE model_family='consumer_defensive' AND asof_date=? AND source_id=?", (as_of, feature_source)).fetchone()[0])
    _check(checks, "positioning_feature_snapshot_complete", feature_rows == expected_total, rows=feature_rows, expected=expected_total)
    if current:
        placeholders = ",".join("?" for _ in current)
        complete_current = int(
            conn.execute(
                f"""SELECT COUNT(*) FROM feature_positioning
                     WHERE model_family='consumer_defensive' AND asof_date=?
                       AND source_id=? AND quality_status='complete'
                       AND ticker IN ({placeholders})""",
                (as_of, feature_source, *current),
            ).fetchone()[0]
        )
    else:
        complete_current = 0
    required_feature_fraction = max(
        float(cfg_get(bundle.payload, "positioning.minimum_current_coverage.institutional_13f")),
        float(cfg_get(bundle.payload, "positioning.minimum_current_coverage.short_interest")),
    )
    _check(
        checks,
        "positioning_feature_current_numeric_coverage",
        _fraction(complete_current, len(current)) >= required_feature_fraction,
        complete=complete_current,
        eligible=len(current),
        fraction=_fraction(complete_current, len(current)),
        threshold=required_feature_fraction,
    )
    feature_lineage_violations = int(
        conn.execute(
            """SELECT COUNT(*) FROM feature_positioning
               WHERE model_family='consumer_defensive' AND asof_date=? AND source_id=?
                 AND (definition_version<>? OR COALESCE(lineage_json,'{}')='{}')""",
            (as_of, feature_source, POSITIONING_DEFINITION_VERSION),
        ).fetchone()[0]
    )
    _check(
        checks,
        "positioning_feature_lineage_current",
        feature_lineage_violations == 0,
        violations=feature_lineage_violations,
    )
    disguised_zero = int(conn.execute("""
        SELECT COUNT(*) FROM feature_positioning f
        WHERE f.model_family='consumer_defensive' AND f.asof_date=? AND f.source_id=?
          AND f.quality_status IN ('missing','unavailable')
          AND (f.insider_net_buying=0 OR f.institutional_flow=0
               OR f.short_float_pct=0 OR f.short_days_to_cover=0 OR f.borrow_fee=0)
        """, (as_of, feature_source)).fetchone()[0])
    _check(checks, "missing_values_not_disguised_as_zero", disguised_zero == 0, violations=disguised_zero)
    fk = conn.execute("PRAGMA foreign_key_check").fetchmany(5)
    _check(checks, "foreign_keys_valid", not fk, sample=[tuple(row) for row in fk])
    status = "PASS" if all(bool(row["passed"]) for row in checks) else "FAIL"
    return {"status": status, "as_of": as_of, "checks": checks, "current_tickers": len(current), "coverage": coverage}


def _latest_available(dates: list[str], as_of: str, max_age: int | None) -> bool:
    index = bisect.bisect_right(dates, as_of) - 1
    if index < 0:
        return False
    if max_age is None:
        return True
    return (date.fromisoformat(as_of) - date.fromisoformat(dates[index])).days <= max_age


def audit_foundation_coverage(conn: sqlite3.Connection, bundle: ConfigBundle, *, as_of: str) -> dict[str, Any]:
    """Publish the Stage 5 foundation checkpoint without claiming Stage 6C readiness."""

    validation = validate_stage5(conn, bundle, as_of=as_of)
    start = str(cfg_get(bundle.payload, "historical_contract.requested_snapshot_start"))
    history_start = str(cfg_get(bundle.payload, "historical_contract.minimum_market_history_start"))
    trading_dates = [
        str(row[0])
        for row in conn.execute(
            """SELECT DISTINCT bar_date FROM fact_price_ohlcv
               WHERE ticker=? AND bar_date>=? AND bar_date<=? ORDER BY bar_date""",
            (str(cfg_get(bundle.payload, "historical_contract.trading_calendar_ticker")), start, as_of),
        )
    ]
    taxonomy = {
        str(row["ticker"]): str(row["calibration_cohort_id"])
        for row in conn.execute("SELECT ticker,calibration_cohort_id FROM dim_consumer_defensive_taxonomy WHERE model_family='consumer_defensive'")
    }
    intervals: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for row in conn.execute("""
        SELECT ticker,start_date,COALESCE(end_date,'9999-12-31') end_date
        FROM dim_universe_membership
        WHERE model_family='consumer_defensive'
          AND (live_investable_flag=1 OR historical_calibration_eligible_flag=1)
        """):
        intervals[str(row["ticker"])].append((str(row["start_date"]), str(row["end_date"])))
    terminal_intervals: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for row in conn.execute(
        """SELECT ticker,start_date,COALESCE(end_date,'9999-12-31') end_date
           FROM dim_universe_membership
           WHERE model_family='consumer_defensive'"""
    ):
        terminal_intervals[str(row["ticker"])].append(
            (str(row["start_date"]), str(row["end_date"]))
        )
    price_dates: dict[str, set[str]] = defaultdict(set)
    for row in conn.execute("SELECT ticker,bar_date FROM fact_price_ohlcv WHERE bar_date>=? AND bar_date<=? AND adjusted_close IS NOT NULL", (history_start, as_of)):
        price_dates[str(row["ticker"])].add(str(row["bar_date"]))
    financial_first = {
        str(row["ticker"]): str(row["first_date"])
        for row in conn.execute("""
            SELECT ticker,MIN(SUBSTR(accepted_at,1,10)) first_date
            FROM fact_financial_statement_canonical
            WHERE accepted_at<=? GROUP BY ticker
            """, (f"{as_of}T23:59:59Z",))
    }
    sorted_price_dates = {
        ticker: sorted(dates)
        for ticker, dates in price_dates.items()
    }
    fx_missing_dates: dict[str, list[str]] = defaultdict(list)
    for row in conn.execute(
        """SELECT ticker,SUBSTR(accepted_at,1,10) accepted_date
           FROM fact_financial_statement_canonical
           WHERE accepted_at<=? AND quality_status='fx_missing'
           ORDER BY ticker,accepted_at""",
        (f"{as_of}T23:59:59Z",),
    ):
        fx_missing_dates[str(row["ticker"])].append(str(row["accepted_date"]))
    ownership_dates: dict[str, list[str]] = defaultdict(list)
    ownership_source = str(cfg_get(bundle.payload, "positioning.ownership_source_id"))
    for row in conn.execute(
        """SELECT ticker,SUBSTR(accepted_at,1,10) available
           FROM fact_sec_ownership_transaction
           WHERE source_id=? AND is_current_truth=1 AND accepted_at<=?
           ORDER BY ticker,accepted_at""",
        (ownership_source, f"{as_of}T23:59:59Z"),
    ):
        ownership_dates[str(row["ticker"])].append(str(row["available"]))
    terminal_candidates = {
        str(row[0])
        for row in conn.execute(
            """SELECT ticker FROM dim_universe_membership
               WHERE model_family='consumer_defensive'
               GROUP BY ticker
               HAVING MAX(live_investable_flag)=0"""
        )
    }
    terminal_rows = {
        str(row["ticker"]): dict(row)
        for row in conn.execute(
            """SELECT ticker,economic_event_date,survivorship_complete,
                      reconciliation_status
               FROM fact_terminal_event_reconciliation"""
        )
    }

    def terminal_gaps(day: str) -> list[str]:
        gaps: list[str] = []
        for ticker in sorted(terminal_candidates):
            ended = any(
                end != "9999-12-31" and end <= day
                for _, end in terminal_intervals[ticker]
            )
            event = terminal_rows.get(ticker)
            if not ended:
                continue
            if (
                event is None
                or str(event["economic_event_date"]) > day
                or int(event["survivorship_complete"]) != 1
            ):
                gaps.append(ticker)
        return gaps
    positioning_dates: dict[str, dict[str, list[str]]] = {key: defaultdict(list) for key in ("institutional_13f", "short_interest", "borrow")}
    source = str(cfg_get(bundle.payload, "positioning.market_positioning_source_id"))
    for key, table, column, value_column in (
        (
            "institutional_13f",
            "fact_13f_positioning",
            "publication_date",
            "institutional_ownership_delta_pct",
        ),
        (
            "short_interest",
            "fact_short_interest",
            "publication_date",
            "COALESCE(short_float_pct,days_to_cover)",
        ),
        ("borrow", "fact_borrow_snapshot", "asof_date", "borrow_fee"),
    ):
        for row in conn.execute(
            f"""SELECT ticker,{column} available FROM {table}
                 WHERE source_id=? AND {column}<=? AND {value_column} IS NOT NULL
                 ORDER BY ticker,{column}""",
            (source, as_of),
        ):
            positioning_dates[key][str(row["ticker"])].append(str(row["available"]))

    rows: list[dict[str, Any]] = []
    daily_summary: list[dict[str, Any]] = []
    earliest: str | None = None
    thresholds = {
        key: float(cfg_get(bundle.payload, f"positioning.minimum_current_coverage.{key}"))
        for key in ("institutional_13f", "short_interest", "borrow")
    }
    for day in trading_dates:
        eligible = [ticker for ticker, ranges in intervals.items() if any(begin <= day <= end for begin, end in ranges)]
        by_cohort: dict[str, list[str]] = defaultdict(list)
        for ticker in eligible:
            by_cohort[taxonomy.get(ticker, "unknown")].append(ticker)
        for cohort, cohort_tickers in sorted(by_cohort.items()):
            market = sum(day in price_dates[ticker] for ticker in cohort_tickers)
            warmup = sum(
                bisect.bisect_right(sorted_price_dates.get(ticker, []), day) >= 252
                for ticker in cohort_tickers
            )
            financial = sum(financial_first.get(ticker, "9999-12-31") <= day for ticker in cohort_tickers)
            fx_ready = sum(
                financial_first.get(ticker, "9999-12-31") <= day
                and bisect.bisect_right(fx_missing_dates[ticker], day) == 0
                for ticker in cohort_tickers
            )
            ownership = sum(
                bisect.bisect_right(ownership_dates[ticker], day) > 0
                for ticker in cohort_tickers
            )
            source_counts = {
                key: sum(_latest_available(positioning_dates[key][ticker], day, _max_age(bundle, key)) for ticker in cohort_tickers)
                for key in positioning_dates
            }
            rows.append({
                "asof_date": day,
                "cohort_id": cohort,
                "eligible_tickers": len(cohort_tickers),
                "market_tickers": market,
                "market_warmup_252d_tickers": warmup,
                "financial_tickers": financial,
                "fx_ready_financial_tickers": fx_ready,
                "sec_ownership_tickers": ownership,
                "institutional_13f_tickers": source_counts["institutional_13f"],
                "short_interest_tickers": source_counts["short_interest"],
                "borrow_tickers": source_counts["borrow"],
            })
        if eligible:
            market_total = sum(day in price_dates[ticker] for ticker in eligible)
            warmup_total = sum(
                bisect.bisect_right(sorted_price_dates.get(ticker, []), day) >= 252
                for ticker in eligible
            )
            financial_total = sum(financial_first.get(ticker, "9999-12-31") <= day for ticker in eligible)
            fx_ready_total = sum(
                financial_first.get(ticker, "9999-12-31") <= day
                and bisect.bisect_right(fx_missing_dates[ticker], day) == 0
                for ticker in eligible
            )
            ownership_total = sum(
                bisect.bisect_right(ownership_dates[ticker], day) > 0
                for ticker in eligible
            )
            source_totals = {
                key: sum(_latest_available(positioning_dates[key][ticker], day, _max_age(bundle, key)) for ticker in eligible)
                for key in positioning_dates
            }
            required_ok = (
                market_total == len(eligible)
                and warmup_total == len(eligible)
                and financial_total == len(eligible)
                and fx_ready_total == financial_total
                and _fraction(source_totals["institutional_13f"], len(eligible)) >= thresholds["institutional_13f"]
                and _fraction(source_totals["short_interest"], len(eligible)) >= thresholds["short_interest"]
            )
            if required_ok and earliest is None:
                earliest = day
            gaps = terminal_gaps(day)
            daily_summary.append(
                {
                    "asof_date": day,
                    "eligible_tickers": len(eligible),
                    "market_tickers": market_total,
                    "market_warmup_252d_tickers": warmup_total,
                    "financial_tickers": financial_total,
                    "fx_ready_financial_tickers": fx_ready_total,
                    "sec_ownership_tickers": ownership_total,
                    "institutional_13f_tickers": source_totals["institutional_13f"],
                    "short_interest_tickers": source_totals["short_interest"],
                    "borrow_tickers": source_totals["borrow"],
                    "terminal_event_gap_count": len(gaps),
                    "terminal_event_gap_tickers": gaps,
                }
            )

    if validation["status"] == "PASS" and earliest:
        decision = "proceed_stage6a"
    elif any(row["institutional_13f_tickers"] or row["short_interest_tickers"] for row in rows):
        decision = "limited_shadow_candidate"
    else:
        decision = "defer_until_positioning_upstream_rematch"
    return {
        "status": "PASS" if validation["status"] == "PASS" and earliest else "FAIL",
        "as_of": as_of,
        "requested_start": start,
        "trading_dates": len(trading_dates),
        "earliest_potential_common_feature_date": earliest,
        "continuation_decision": decision,
        "stage5_validation_status": validation["status"],
        "terminal_event_gaps_as_of": terminal_gaps(as_of),
        "canonical_fx_missing_rows": sum(len(dates) for dates in fx_missing_dates.values()),
        "daily_summary": daily_summary,
        "rows": rows,
        "note": "Foundation checkpoint only; definitive historical feature readiness remains Stage 6C.",
    }

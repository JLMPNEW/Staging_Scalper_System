from __future__ import annotations

import csv
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from consumer_defensive.core.db import require_lastrowid, utc_now
from consumer_defensive.core.atomic_io import atomic_text_writer, atomic_write_text
from consumer_defensive.core.norgate_runtime import (
    NORGATE_MEMBERSHIP_DATABASES,
    norgate_database_fingerprint,
    require_norgate_snapshot,
)
from consumer_defensive.core.terminal_events import load_terminal_event_ledger, load_terminal_event_policy
from consumer_defensive.core.universe import (
    INTERNAL_SECTOR,
    MODEL_FAMILY,
    PIT_SOURCE_ID,
    PORTFOLIO_SECTOR,
    UniversePolicy,
    ensure_stage2_schema,
    normalize_security_type,
    normalize_ticker,
    read_csv,
)


@dataclass(frozen=True)
class Candidate:
    ticker: str
    company_name: str
    cohort_id: str
    cohort_name: str
    source_set: str
    exchange: str
    listing_country: str
    currency: str
    security_type: str
    cik: str = ""
    explicit_price_symbol: str = ""
    explicit_provider_asset_id: str = ""
    exit_year: str = ""
    calibration_eligible: int = 1


@dataclass(frozen=True)
class Resolution:
    symbol: str
    method: str
    alternatives: tuple[str, ...] = ()


@dataclass(frozen=True)
class _PendingMembershipLoad:
    candidate: Candidate
    symbol: str
    provider_asset_id: str
    first_date: str
    last_date: str
    effective_start: str
    effective_end: str
    price_dates: tuple[str, ...]
    listed_flags: tuple[int, ...]
    membership_values: tuple[tuple[str, tuple[int, ...]], ...]
    union_flags: tuple[int, ...]
    intervals: tuple[tuple[str, str], ...]
    extracted_at: str


def symbol_variants(symbol: str) -> list[str]:
    raw = normalize_ticker(symbol)
    variants = [raw]
    for value in (
        raw.replace("-", "."),
        raw.replace(".", "-"),
        raw.replace("/", "."),
        raw.replace("/", "-"),
    ):
        if value and value not in variants:
            variants.append(value)
    return variants


def _normalized_name(value: str) -> str:
    text = re.sub(r"[^A-Z0-9 ]+", " ", str(value or "").upper())
    stop = {
        "A", "AN", "AND", "THE", "INC", "INCORPORATED", "CORP", "CORPORATION",
        "CO", "COMPANY", "LTD", "LIMITED", "PLC", "SA", "NV", "LLC", "HOLDING",
        "HOLDINGS", "GROUP", "CLASS", "COMMON", "STOCK",
    }
    return " ".join(token for token in text.split() if token not in stop)


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, _normalized_name(left), _normalized_name(right)).ratio()


def _iso(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        import pandas as pd  # type: ignore

        return pd.Timestamp(value).date().isoformat()
    except Exception:
        return str(value)


def load_historical_ciks(policy: UniversePolicy) -> dict[str, str]:
    rows = read_csv(policy.resolve("historical_identifier_csv"))
    expected = int(policy.payload["expected_historical_identifier_rows"])
    if len(rows) != expected:
        raise ValueError(f"Historical identifier file must contain {expected} rows; found {len(rows)}.")
    mapping: dict[str, str] = {}
    for row in rows:
        ticker = normalize_ticker(row.get("ticker"))
        cik = str(row.get("cik") or "").strip().zfill(10)
        source_url = str(row.get("source_url") or "").strip()
        if not ticker or ticker in mapping:
            raise ValueError(f"Historical identifier ticker is blank or duplicated: {ticker!r}.")
        if len(cik) != 10 or not cik.isdigit():
            raise ValueError(f"Historical identifier CIK is invalid for {ticker}: {cik!r}.")
        if source_url != f"https://data.sec.gov/submissions/CIK{cik}.json":
            raise ValueError(f"Historical identifier SEC source URL is invalid for {ticker}.")
        if str(row.get("review_status") or "") != "reviewed":
            raise ValueError(f"Historical identifier is not reviewed for {ticker}.")
        mapping[ticker] = cik
    return mapping


def load_current_provider_symbols(
    policy: UniversePolicy,
) -> dict[str, tuple[str, str]]:
    """Load reviewed public-ticker to Norgate-symbol/asset overrides."""

    rows = read_csv(policy.resolve("current_provider_symbol_overrides_csv"))
    expected = int(policy.payload["expected_current_provider_symbol_override_rows"])
    if len(rows) != expected:
        raise ValueError(
            "Current provider-symbol override file must contain "
            f"{expected} rows; found {len(rows)}."
        )
    required = {
        "ticker",
        "norgate_symbol",
        "norgate_asset_id",
        "review_status",
        "review_reason",
    }
    if rows and set(rows[0]) != required:
        raise ValueError(
            "Current provider-symbol override columns must be exactly "
            f"{sorted(required)}; got {sorted(rows[0])}."
        )
    mapping: dict[str, tuple[str, str]] = {}
    for row in rows:
        ticker = normalize_ticker(row.get("ticker"))
        symbol = str(row.get("norgate_symbol") or "").strip().upper()
        asset_id = str(row.get("norgate_asset_id") or "").strip()
        if not ticker or ticker in mapping:
            raise ValueError(
                f"Current provider-symbol override ticker is blank or duplicated: {ticker!r}."
            )
        if not symbol or not asset_id:
            raise ValueError(f"Current provider-symbol override is incomplete for {ticker}.")
        if str(row.get("review_status") or "").strip() != "reviewed":
            raise ValueError(f"Current provider-symbol override is not reviewed for {ticker}.")
        if not str(row.get("review_reason") or "").strip():
            raise ValueError(f"Current provider-symbol override has no review reason for {ticker}.")
        mapping[ticker] = (symbol, asset_id)
    return mapping


def load_candidates(conn: sqlite3.Connection, policy: UniversePolicy) -> tuple[list[Candidate], list[dict[str, str]]]:
    current_overrides = load_current_provider_symbols(policy)
    current_rows = conn.execute(
        """
        SELECT s.ticker, c.company_name, c.cik, s.exchange, s.listing_country,
               s.currency, s.security_type, t.calibration_cohort_id,
               t.calibration_cohort
        FROM dim_security s
        JOIN dim_company c ON c.company_id = s.company_id
        JOIN dim_consumer_defensive_taxonomy t ON t.security_id = s.security_id
        WHERE t.model_family = 'consumer_defensive' AND s.listing_status = 'active' AND c.is_active = 1
        ORDER BY s.ticker
        """
    ).fetchall()
    current_tickers = {str(row[0]) for row in current_rows}
    unknown_overrides = sorted(set(current_overrides) - current_tickers)
    if unknown_overrides:
        raise ValueError(
            "Current provider-symbol overrides reference out-of-scope tickers: "
            f"{unknown_overrides}"
        )
    candidates = [
        Candidate(
            ticker=str(row[0]),
            company_name=str(row[1]),
            cik=str(row[2] or ""),
            exchange=str(row[3] or ""),
            listing_country=str(row[4] or ""),
            currency=str(row[5] or "USD"),
            security_type=str(row[6] or "Common Stock"),
            cohort_id=str(row[7]),
            cohort_name=str(row[8]),
            source_set="current",
            explicit_price_symbol=current_overrides.get(str(row[0]), ("", ""))[0],
            explicit_provider_asset_id=current_overrides.get(str(row[0]), ("", ""))[1],
            calibration_eligible=1,
        )
        for row in current_rows
    ]
    exclusions = {normalize_ticker(x) for x in policy.payload.get("delisted_security_exclusions", [])}
    excluded_rows: list[dict[str, str]] = []
    history_start = str(policy.payload["history_start"])
    terminal_policy = load_terminal_event_policy(policy.resolve("terminal_event_policy"))
    terminal_events = {event.ticker: event for event in load_terminal_event_ledger(terminal_policy)}
    historical_ciks = load_historical_ciks(policy)
    for row in read_csv(policy.resolve("delisted_seed_csv")):
        ticker = normalize_ticker(row.get("historical_ticker"))
        if str(row.get("include_in_historical_universe") or "0") != "1":
            continue
        if ticker in exclusions:
            excluded_rows.append(
                {
                    "ticker": ticker,
                    "status": "alias_lineage_excluded",
                    "reason": str(policy.payload.get("delisted_exclusion_reason") or ""),
                    "canonical_ticker": str(row.get("successor_ticker") or ""),
                }
            )
            continue
        event = terminal_events.get(ticker)
        if event is None:
            excluded_rows.append(
                {
                    "ticker": ticker,
                    "status": "outside_reconciled_terminal_scope",
                    "reason": "Ticker is not in the reviewed terminal-event ledger.",
                    "canonical_ticker": "",
                }
            )
            continue
        if event.last_trade_date < history_start:
            excluded_rows.append(
                {
                    "ticker": ticker,
                    "status": "pre_history_window",
                    "reason": f"last_trade_date={event.last_trade_date} before history_start={history_start}",
                    "canonical_ticker": "",
                }
            )
            continue
        exit_year = event.provider_last_quoted_date[:4]
        candidates.append(
            Candidate(
                ticker=ticker,
                company_name=str(row.get("company_name") or ""),
                cohort_id=str(row.get("cohort_id") or ""),
                cohort_name=str(row.get("cohort") or row.get("industry") or ""),
                source_set="delisted",
                exchange=str(row.get("exchange") or ""),
                listing_country=str(row.get("country") or ""),
                currency=str(row.get("currency") or "USD"),
                security_type=normalize_security_type(row.get("security_type")),
                explicit_price_symbol=str(row.get("price_source_symbol") or ""),
                exit_year=exit_year,
                cik=historical_ciks.get(ticker, ""),
                calibration_eligible=event.calibration_eligible,
            )
        )
    missing_ciks = sorted(candidate.ticker for candidate in candidates if candidate.source_set == "delisted" and not candidate.cik)
    if missing_ciks:
        raise ValueError(f"Historical candidates missing reviewed SEC CIKs: {missing_ciks}")
    loaded_delisted = {candidate.ticker for candidate in candidates if candidate.source_set == "delisted"}
    if loaded_delisted != set(terminal_events):
        raise ValueError(f"Historical candidate scope differs from terminal ledger: loaded={sorted(loaded_delisted)} expected={sorted(terminal_events)}")
    return candidates, excluded_rows


def resolve_candidate(
    provider: Any,
    candidate: Candidate,
    active_symbols: set[str],
    delisted_symbols: set[str],
) -> Resolution:
    catalog = active_symbols | delisted_symbols
    if candidate.explicit_price_symbol:
        matches = [value for value in symbol_variants(candidate.explicit_price_symbol) if value in catalog]
        if len(matches) == 1:
            return Resolution(matches[0], "explicit_price_source_symbol", tuple(matches))
        if len(matches) > 1:
            return Resolution("", "ambiguous_explicit_symbol", tuple(matches))
        return Resolution("", "explicit_price_source_symbol_not_found", tuple(symbol_variants(candidate.explicit_price_symbol)))
    preferred = active_symbols if candidate.source_set == "current" else delisted_symbols
    matches = [value for value in symbol_variants(candidate.ticker) if value in preferred]
    if len(matches) == 1:
        return Resolution(matches[0], f"{candidate.source_set}_exact_or_variant", tuple(matches))
    if len(matches) > 1:
        return Resolution("", "ambiguous_exact_variants", tuple(matches))
    if candidate.source_set == "current":
        return Resolution("", "current_symbol_not_in_active_database")

    suffix_matches = sorted(
        symbol
        for variant in symbol_variants(candidate.ticker)
        for symbol in delisted_symbols
        if symbol.startswith(f"{variant}-")
    )
    scored: list[tuple[float, str, str, str]] = []
    for symbol in sorted(set(suffix_matches)):
        try:
            provider_name = str(provider.security_name(symbol) or "")
            last_date = _iso(provider.last_quoted_date(symbol))
        except Exception:
            continue
        last_year = last_date[:4]
        score = _similarity(candidate.company_name, provider_name)
        year_matches = bool(candidate.exit_year and last_year == candidate.exit_year)
        if year_matches and score >= 0.55:
            scored.append((score, symbol, provider_name, last_date))
    scored.sort(reverse=True)
    if len(scored) == 1 or (len(scored) > 1 and scored[0][0] > scored[1][0] + 0.10):
        return Resolution(scored[0][1], "delisted_name_and_exit_year", tuple(suffix_matches))
    method = "unresolved_delisted_symbol" if not suffix_matches else "ambiguous_or_date_mismatched_delisted_symbol"
    return Resolution("", method, tuple(suffix_matches))


def _security_for_candidate(
    conn: sqlite3.Connection,
    candidate: Candidate,
    symbol: str,
    provider_asset_id: str,
    first_date: str,
    last_date: str,
) -> int:
    now = utc_now()
    if candidate.source_set == "current":
        row = conn.execute(
            "SELECT security_id, company_id FROM dim_security WHERE ticker=? AND listing_status='active'",
            (candidate.ticker,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Current security {candidate.ticker} was not loaded before Norgate membership.")
        security_id, company_id = int(row[0]), int(row[1])
        conn.execute(
            """
            UPDATE dim_security SET provider_price_symbol=?, listing_start_date=?,
                listing_end_date=NULL, updated_at=? WHERE security_id=?
            """,
            (symbol, first_date, now, security_id),
        )
    else:
        company = conn.execute(
            "SELECT company_id FROM dim_company WHERE primary_ticker=?", (candidate.ticker,)
        ).fetchone()
        if company is None:
            cursor = conn.execute(
                """
                INSERT INTO dim_company(
                    primary_ticker, cik, company_name, issuer_domicile, reporting_currency,
                    universe_status, is_active, data_quality_status, first_seen_at, updated_at
                ) VALUES (?, ?, ?, NULL, ?, 'historical', 0, 'terminal_reconciliation_pending', ?, ?)
                """,
                (candidate.ticker, candidate.cik or None, candidate.company_name, candidate.currency, now, now),
            )
            company_id = require_lastrowid(cursor, context=f"create historical company {candidate.ticker}")
        else:
            company_id = int(company[0])
            conn.execute(
                """
                UPDATE dim_company SET cik=COALESCE(NULLIF(?, ''), cik), company_name=?, reporting_currency=?,
                    universe_status='historical', is_active=0, data_quality_status='terminal_reconciliation_pending', updated_at=?
                WHERE company_id=?
                """,
                (candidate.cik, candidate.company_name, candidate.currency, now, company_id),
            )
        security = conn.execute(
            "SELECT security_id FROM dim_security WHERE ticker=? AND listing_status='delisted'",
            (candidate.ticker,),
        ).fetchone()
        if security is None:
            cursor = conn.execute(
                """
                INSERT INTO dim_security(
                    company_id, ticker, provider_price_symbol, exchange, listing_country,
                    security_type, adr_ads_flag, listing_status, is_primary_listing,
                    currency, listing_start_date, listing_end_date, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'delisted', 1, ?, ?, ?, ?, ?)
                """,
                (
                    company_id,
                    candidate.ticker,
                    symbol,
                    candidate.exchange,
                    candidate.listing_country,
                    candidate.security_type,
                    int(candidate.security_type == "ADR/ADS"),
                    candidate.currency,
                    first_date,
                    last_date,
                    now,
                    now,
                ),
            )
            security_id = require_lastrowid(cursor, context=f"create historical security {candidate.ticker}")
        else:
            security_id = int(security[0])
            conn.execute(
                """
                UPDATE dim_security SET company_id=?, provider_price_symbol=?, exchange=?,
                    listing_country=?, security_type=?, adr_ads_flag=?, currency=?,
                    listing_start_date=?, listing_end_date=?, updated_at=? WHERE security_id=?
                """,
                (
                    company_id,
                    symbol,
                    candidate.exchange,
                    candidate.listing_country,
                    candidate.security_type,
                    int(candidate.security_type == "ADR/ADS"),
                    candidate.currency,
                    first_date,
                    last_date,
                    now,
                    security_id,
                ),
            )
        conn.execute(
            """
            INSERT INTO dim_consumer_defensive_taxonomy(
                company_id, security_id, ticker, model_family, sector, portfolio_sector,
                calibration_cohort_id, calibration_cohort, applicability_subtype,
                taxonomy_confidence, taxonomy_source, business_cohort_override_flag,
                analyst_reviewed, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', 1.0, ?, 0, 1, ?)
            ON CONFLICT(ticker, model_family) DO UPDATE SET
                company_id=excluded.company_id, security_id=excluded.security_id,
                calibration_cohort_id=excluded.calibration_cohort_id,
                calibration_cohort=excluded.calibration_cohort,
                taxonomy_source=excluded.taxonomy_source, updated_at=excluded.updated_at
            """,
            (
                company_id,
                security_id,
                candidate.ticker,
                MODEL_FAMILY,
                INTERNAL_SECTOR,
                PORTFOLIO_SECTOR,
                candidate.cohort_id,
                candidate.cohort_name,
                PIT_SOURCE_ID,
                now,
            ),
        )
    existing_identifier = conn.execute(
        """
        SELECT identifier_id, company_id, security_id FROM dim_identifier
        WHERE identifier_type='norgate_assetid' AND identifier_value=?
        """,
        (provider_asset_id,),
    ).fetchall()
    mismatched_identifiers = [
        row for row in existing_identifier if int(row[2] or 0) != security_id
    ]
    if mismatched_identifiers:
        reviewed_reassignment = (
            candidate.source_set == "current"
            and candidate.explicit_provider_asset_id == provider_asset_id
        )
        for identifier in mismatched_identifiers:
            prior = conn.execute(
                """
                SELECT s.listing_status, c.is_active, COALESCE(c.cik, ''),
                       EXISTS(
                           SELECT 1 FROM dim_consumer_defensive_taxonomy t
                           WHERE t.security_id=s.security_id AND t.model_family=?
                       )
                FROM dim_security s
                JOIN dim_company c ON c.company_id=s.company_id
                WHERE s.security_id=?
                """,
                (MODEL_FAMILY, int(identifier[2])),
            ).fetchone()
            same_issuer = bool(
                prior
                and candidate.cik
                and str(prior[2]) == candidate.cik
            )
            safely_superseded = bool(
                prior
                and str(prior[0]) != "active"
                and int(prior[1]) == 0
                and int(prior[3]) == 0
            )
            if not (reviewed_reassignment and same_issuer and safely_superseded):
                raise ValueError(
                    f"Norgate asset {provider_asset_id} maps to multiple securities; "
                    f"candidate={candidate.ticker}."
                )
    if not existing_identifier:
        conn.execute(
            """
            INSERT INTO dim_identifier(
                company_id, security_id, identifier_type, identifier_value, source_id,
                valid_from, valid_to, confidence, created_at, updated_at
            ) VALUES (?, ?, 'norgate_assetid', ?, ?, ?, ?, 1.0, ?, ?)
            """,
            (company_id, security_id, provider_asset_id, PIT_SOURCE_ID, first_date, last_date, now, now),
        )
    else:
        conn.execute(
            '''
            UPDATE dim_identifier
            SET company_id=?, security_id=?, source_id=?, valid_from=?, valid_to=?,
                confidence=1.0, updated_at=?
            WHERE identifier_type='norgate_assetid' AND identifier_value=?
            ''',
            (
                company_id,
                security_id,
                PIT_SOURCE_ID,
                first_date,
                last_date,
                now,
                provider_asset_id,
            ),
        )
    return security_id


def _frame_values(frame: Any) -> tuple[list[str], list[int]]:
    import pandas as pd  # type: ignore

    if frame is None or frame.empty:
        return [], []
    dates = pd.DatetimeIndex(pd.to_datetime(frame.index)).tz_localize(None)
    if not dates.is_monotonic_increasing or dates.has_duplicates:
        raise ValueError("Provider series dates are not strictly ordered and unique.")
    numeric = pd.to_numeric(frame.iloc[:, 0], errors="coerce")
    distinct = set(numeric.dropna().astype(float).tolist())
    if numeric.isna().any() or not distinct.issubset({0.0, 1.0}):
        raise ValueError(
            "Provider binary series contains null, non-numeric, or non-binary values: "
            f"{sorted(distinct)}"
        )
    values = numeric.astype(int).tolist()
    return [value.date().isoformat() for value in dates], values


def _compress_intervals(dates: list[str], flags: list[int]) -> list[tuple[str, str]]:
    intervals: list[tuple[str, str]] = []
    start = ""
    previous = ""
    for current_date, flag in zip(dates, flags, strict=True):
        if flag and not start:
            start = current_date
        if not flag and start:
            intervals.append((start, previous))
            start = ""
        previous = current_date
    if start:
        intervals.append((start, previous))
    return intervals


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    if not columns:
        columns = ["status"]
    with atomic_text_writer(path, encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _persist_pending_membership(
    conn: sqlite3.Connection,
    pending: _PendingMembershipLoad,
    provider_updated_at: str,
) -> None:
    """Persist one already-validated provider snapshot without provider calls."""

    security_id = _security_for_candidate(
        conn,
        pending.candidate,
        pending.symbol,
        pending.provider_asset_id,
        pending.first_date,
        pending.last_date,
    )
    conn.execute(
        "DELETE FROM fact_major_exchange_listing_daily "
        "WHERE security_id=? AND source_id=?",
        (security_id, PIT_SOURCE_ID),
    )
    conn.executemany(
        """
        INSERT INTO fact_major_exchange_listing_daily(
            security_id, provider_asset_id, listing_date, major_exchange_listed_flag,
            source_id, provider_database_updated_at, extracted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                security_id,
                pending.provider_asset_id,
                membership_date,
                flag,
                PIT_SOURCE_ID,
                provider_updated_at,
                pending.extracted_at,
            )
            for membership_date, flag in zip(
                pending.price_dates,
                pending.listed_flags,
                strict=True,
            )
        ],
    )
    conn.execute(
        '''DELETE FROM fact_recognized_vehicle_membership_daily
           WHERE security_id=? AND source_id=?''',
        (security_id, PIT_SOURCE_ID),
    )
    for vehicle_id, flags in pending.membership_values:
        conn.executemany(
            """
            INSERT INTO fact_recognized_vehicle_membership_daily(
                security_id, provider_asset_id, vehicle_id, membership_date,
                member_flag, source_id, provider_database_updated_at, extracted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    security_id,
                    pending.provider_asset_id,
                    vehicle_id,
                    membership_date,
                    flag,
                    PIT_SOURCE_ID,
                    provider_updated_at,
                    pending.extracted_at,
                )
                for membership_date, flag in zip(pending.price_dates, flags, strict=True)
            ],
        )
    conn.execute(
        "DELETE FROM dim_universe_membership WHERE security_id=? AND membership_source_id=?",
        (security_id, PIT_SOURCE_ID),
    )
    for interval_start, interval_end in pending.intervals:
        is_current = int(
            pending.candidate.source_set == "current"
            and interval_end == pending.price_dates[-1]
            and pending.union_flags[-1] == 1
        )
        conn.execute(
            """
            INSERT INTO dim_universe_membership(
                company_id, security_id, ticker, model_family, membership_source_id,
                membership_basis, recognized_vehicle, start_date, end_date,
                membership_status, is_current_member, point_in_time_flag,
                live_investable_flag, historical_calibration_eligible_flag,
                confidence, reason, created_at, updated_at
            )
            SELECT company_id, security_id, ticker, ?, ?,
                   'recognized_index_union', 'approved_index_union', ?, ?, ?, ?, 1, ?, ?,
                   1.0, ?, ?, ?
            FROM dim_security WHERE security_id=?
            """,
            (
                MODEL_FAMILY,
                PIT_SOURCE_ID,
                interval_start,
                interval_end,
                "active" if is_current else "historical",
                is_current,
                is_current,
                pending.candidate.calibration_eligible,
                "Point-in-time union of the four approved Norgate index membership series "
                "and major-exchange listing.",
                pending.extracted_at,
                pending.extracted_at,
                security_id,
            ),
        )


def _delete_stale_source_membership(
    conn: sqlite3.Connection,
    candidate_tickers: set[str],
) -> int:
    placeholders = ",".join("?" for _ in candidate_tickers)
    stale_security_ids = [
        int(row[0])
        for row in conn.execute(
            f"""SELECT DISTINCT s.security_id
                FROM dim_security s
                WHERE s.ticker NOT IN ({placeholders})
                  AND (
                      EXISTS(
                          SELECT 1 FROM dim_universe_membership u
                          WHERE u.security_id=s.security_id
                            AND u.membership_source_id=?
                      )
                      OR EXISTS(
                          SELECT 1 FROM fact_major_exchange_listing_daily x
                          WHERE x.security_id=s.security_id AND x.source_id=?
                      )
                      OR EXISTS(
                          SELECT 1 FROM fact_recognized_vehicle_membership_daily r
                          WHERE r.security_id=s.security_id AND r.source_id=?
                      )
                  )""",
            [*sorted(candidate_tickers), PIT_SOURCE_ID, PIT_SOURCE_ID, PIT_SOURCE_ID],
        )
    ]
    for security_id in stale_security_ids:
        conn.execute(
            "DELETE FROM dim_universe_membership WHERE security_id=? AND membership_source_id=?",
            (security_id, PIT_SOURCE_ID),
        )
        conn.execute(
            "DELETE FROM fact_major_exchange_listing_daily WHERE security_id=? AND source_id=?",
            (security_id, PIT_SOURCE_ID),
        )
        conn.execute(
            "DELETE FROM fact_recognized_vehicle_membership_daily WHERE security_id=? AND source_id=?",
            (security_id, PIT_SOURCE_ID),
        )
    return len(stale_security_ids)


def load_norgate_membership(
    conn: sqlite3.Connection,
    policy: UniversePolicy,
    *,
    provider: Any,
    as_of: str | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    import pandas as pd  # type: ignore

    ensure_stage2_schema(conn)
    as_of = as_of or date.today().isoformat()
    start = str(policy.payload["history_start"])
    if date.fromisoformat(start) > date.fromisoformat(as_of):
        raise ValueError(f"Invalid membership date window: start {start} is after as-of {as_of}.")
    vehicles = {
        str(row["vehicle_id"]): str(row["norgate_index_name"])
        for row in policy.payload["approved_membership_vehicles"]
    }
    provider_fingerprint = norgate_database_fingerprint(
        provider,
        NORGATE_MEMBERSHIP_DATABASES,
    )
    active_symbols = set(provider.database_symbols("US Equities"))
    delisted_symbols = set(provider.database_symbols("US Equities Delisted"))
    provider_fingerprint_after_catalog = require_norgate_snapshot(
        provider,
        provider_fingerprint,
        context="while reading symbol catalogs",
    )
    candidates, excluded = load_candidates(conn, policy)
    report_rows: list[dict[str, Any]] = [dict(row) for row in excluded]
    loaded = 0
    current_loaded = 0
    current_latest_eligible = 0
    expected_historical = {
        candidate.ticker
        for candidate in candidates
        if candidate.source_set == "delisted"
    }
    daily_rows = 0
    intervals_loaded = 0
    pending_loads: list[_PendingMembershipLoad] = []

    for candidate in candidates:
        require_norgate_snapshot(
            provider,
            provider_fingerprint,
            context="during membership extraction",
        )
        resolution = resolve_candidate(provider, candidate, active_symbols, delisted_symbols)
        report: dict[str, Any] = {
            "ticker": candidate.ticker,
            "source_set": candidate.source_set,
            "status": "unresolved" if not resolution.symbol else "resolved",
            "resolution_method": resolution.method,
            "provider_symbol": resolution.symbol,
            "alternatives": ";".join(resolution.alternatives),
        }
        if not resolution.symbol:
            report_rows.append(report)
            continue
        symbol = resolution.symbol
        asset_id = str(provider.assetid(symbol))
        if (
            candidate.explicit_provider_asset_id
            and asset_id != candidate.explicit_provider_asset_id
        ):
            raise RuntimeError(
                f"{candidate.ticker}: reviewed Norgate asset mismatch for {symbol}; "
                f"expected={candidate.explicit_provider_asset_id} actual={asset_id}"
            )
        first_date = _iso(provider.first_quoted_date(symbol))
        last_date = _iso(provider.last_quoted_date(symbol))
        effective_start = max(start, first_date)
        effective_end = min(as_of, last_date or as_of)
        report.update(
            {
                "provider_asset_id": asset_id,
                "provider_security_name": str(provider.security_name(symbol) or ""),
                "first_quoted_date": first_date,
                "last_quoted_date": last_date,
                "effective_start": effective_start,
                "effective_end": effective_end,
            }
        )
        if effective_start > effective_end:
            report["status"] = "out_of_window"
            report_rows.append(report)
            continue
        raw = provider.price_timeseries(
            symbol,
            stock_price_adjustment_setting=provider.StockPriceAdjustmentType.NONE,
            start_date=effective_start,
            end_date=effective_end,
            timeseriesformat="pandas-dataframe",
        )
        price_dates = [value.date().isoformat() for value in pd.DatetimeIndex(pd.to_datetime(raw.index)).tz_localize(None)]
        if not price_dates:
            report["status"] = "no_price_data"
            report_rows.append(report)
            continue
        listed_frame = provider.major_exchange_listed_timeseries(
            symbol,
            start_date=effective_start,
            end_date=effective_end,
            timeseriesformat="pandas-dataframe",
        )
        listed_dates, listed_flags = _frame_values(listed_frame)
        if listed_dates != price_dates:
            raise ValueError(f"{candidate.ticker}: major-exchange dates do not align with prices.")
        membership_values: dict[str, list[int]] = {}
        for vehicle_id, index_name in vehicles.items():
            frame = provider.index_constituent_timeseries(
                symbol,
                index_name,
                start_date=effective_start,
                end_date=effective_end,
                timeseriesformat="pandas-dataframe",
            )
            dates, flags = _frame_values(frame)
            if dates != price_dates:
                raise ValueError(f"{candidate.ticker}: {index_name} membership dates do not align with prices.")
            membership_values[vehicle_id] = flags
        union_flags = [
            int(bool(listed_flags[position]) and any(flags[position] for flags in membership_values.values()))
            for position in range(len(price_dates))
        ]
        extracted_at = utc_now()
        intervals = tuple(_compress_intervals(price_dates, union_flags))
        pending_loads.append(
            _PendingMembershipLoad(
                candidate=candidate,
                symbol=symbol,
                provider_asset_id=asset_id,
                first_date=first_date,
                last_date=last_date,
                effective_start=effective_start,
                effective_end=effective_end,
                price_dates=tuple(price_dates),
                listed_flags=tuple(listed_flags),
                membership_values=tuple(
                    (vehicle_id, tuple(flags))
                    for vehicle_id, flags in membership_values.items()
                ),
                union_flags=tuple(union_flags),
                intervals=intervals,
                extracted_at=extracted_at,
            )
        )
        daily_rows += len(price_dates) * (len(vehicles) + 1)
        intervals_loaded += len(intervals)
        require_norgate_snapshot(
            provider,
            provider_fingerprint,
            context="during membership extraction",
        )
        latest_eligible = int(bool(union_flags[-1]))
        report.update(
            {
                "status": "loaded",
                "trading_days": len(price_dates),
                "eligible_days": sum(union_flags),
                "first_eligible_date": next((d for d, f in zip(price_dates, union_flags, strict=True) if f), ""),
                "last_eligible_date": next((d for d, f in reversed(list(zip(price_dates, union_flags, strict=True))) if f), ""),
                "latest_eligible": latest_eligible,
                "intervals": len(intervals),
            }
        )
        report_rows.append(report)
        loaded += 1
        if candidate.source_set == "current":
            current_loaded += 1
            current_latest_eligible += latest_eligible

    provider_fingerprint_end = require_norgate_snapshot(
        provider,
        provider_fingerprint,
        context="before membership publication",
    )
    expected_current = int(policy.payload["expected_current_rows"])
    unresolved_current = [
        row for row in report_rows
        if row.get("source_set") == "current" and row.get("status") != "loaded"
    ]
    if current_loaded != expected_current or current_latest_eligible != expected_current:
        if output_dir:
            _write_csv(output_dir / "norgate_membership_resolution.csv", report_rows)
        raise RuntimeError(
            "Current recognized-membership gate failed: "
            f"expected={expected_current} loaded={current_loaded} latest_eligible={current_latest_eligible} "
            f"unresolved={[row.get('ticker') for row in unresolved_current]}"
        )
    historical_rows = {
        str(row.get("ticker")): row
        for row in report_rows
        if row.get("source_set") == "delisted"
        and str(row.get("ticker") or "") in expected_historical
    }
    out_of_window_historical = {
        ticker
        for ticker, row in historical_rows.items()
        if row.get("status") == "out_of_window"
    }
    required_historical = expected_historical - out_of_window_historical
    loaded_historical = {
        ticker
        for ticker, row in historical_rows.items()
        if row.get("status") == "loaded"
    }
    eligible_historical = {
        ticker
        for ticker, row in historical_rows.items()
        if row.get("status") == "loaded"
        and int(row.get("eligible_days") or 0) > 0
    }
    historical_failures = sorted(required_historical - eligible_historical)
    if historical_failures:
        if output_dir:
            _write_csv(output_dir / "norgate_membership_resolution.csv", report_rows)
        failure_status = {
            ticker: str(historical_rows.get(ticker, {}).get("status") or "missing")
            for ticker in historical_failures
        }
        raise RuntimeError(
            "Historical recognized-membership gate failed: every in-window "
            "reviewed terminal-event candidate must resolve, load daily PIT "
            "membership, and have at least one eligible recognized-membership "
            f"day; failures={failure_status}"
        )

    # No database state is mutated until every provider call has completed under
    # one stable fingerprint.  The single transaction then publishes all
    # candidates together or rolls all of them back on any persistence failure.
    provider_fingerprint_json = json.dumps(
        provider_fingerprint,
        sort_keys=True,
        separators=(",", ":"),
    )
    with conn:
        stale_source_securities_removed = _delete_stale_source_membership(
            conn,
            {candidate.ticker for candidate in candidates},
        )
        for pending in pending_loads:
            _persist_pending_membership(
                conn,
                pending,
                provider_fingerprint_json,
            )

    breadth_rows = [
        dict(row)
        for row in conn.execute(
            """
            WITH union_membership AS (
                SELECT security_id, membership_date, MAX(member_flag) AS any_member
                FROM fact_recognized_vehicle_membership_daily
                GROUP BY security_id, membership_date
            )
            SELECT u.membership_date AS asof_date,
                   t.calibration_cohort_id,
                   t.calibration_cohort,
                   COUNT(*) AS eligible_security_count
            FROM union_membership u
            JOIN fact_major_exchange_listing_daily x
              ON x.security_id=u.security_id AND x.listing_date=u.membership_date
            JOIN dim_consumer_defensive_taxonomy t ON t.security_id=u.security_id
            WHERE u.any_member=1 AND x.major_exchange_listed_flag=1
            GROUP BY u.membership_date, t.calibration_cohort_id, t.calibration_cohort
            ORDER BY u.membership_date, t.calibration_cohort_id
            """
        ).fetchall()
    ]
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        _write_csv(output_dir / "norgate_membership_resolution.csv", report_rows)
        _write_csv(output_dir / "daily_cohort_breadth.csv", breadth_rows)
        summary = {
            "status": "PASS",
            "as_of": as_of,
            "history_start": start,
            "provider_database_updated_at": provider_fingerprint,
            "provider_database_updated_at_after_catalog": provider_fingerprint_after_catalog,
            "provider_database_updated_at_end": provider_fingerprint_end,
            "candidates": len(candidates),
            "loaded": loaded,
            "current_loaded": current_loaded,
            "current_latest_eligible": current_latest_eligible,
            "historical_expected": len(expected_historical),
            "historical_required_in_window": len(required_historical),
            "historical_loaded": len(loaded_historical),
            "historical_recognized_members": len(eligible_historical),
            "historical_out_of_window": len(out_of_window_historical),
            "daily_rows_written": daily_rows,
            "union_intervals_written": intervals_loaded,
            "stale_source_securities_removed": stale_source_securities_removed,
            "report_path": str(output_dir / "norgate_membership_resolution.csv"),
            "breadth_path": str(output_dir / "daily_cohort_breadth.csv"),
        }
        summary_path = output_dir / "summary.json"
        atomic_write_text(
            summary_path,
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    else:
        summary = {
            "status": "PASS",
            "as_of": as_of,
            "history_start": start,
            "provider_database_updated_at": provider_fingerprint,
            "provider_database_updated_at_after_catalog": provider_fingerprint_after_catalog,
            "provider_database_updated_at_end": provider_fingerprint_end,
            "candidates": len(candidates),
            "loaded": loaded,
            "current_loaded": current_loaded,
            "current_latest_eligible": current_latest_eligible,
            "historical_expected": len(expected_historical),
            "historical_required_in_window": len(required_historical),
            "historical_loaded": len(loaded_historical),
            "historical_recognized_members": len(eligible_historical),
            "historical_out_of_window": len(out_of_window_historical),
            "daily_rows_written": daily_rows,
            "union_intervals_written": intervals_loaded,
            "stale_source_securities_removed": stale_source_securities_removed,
        }
    return summary

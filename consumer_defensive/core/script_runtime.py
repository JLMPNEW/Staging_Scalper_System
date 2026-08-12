"""Fail-closed helpers shared by Consumer Defensive command-line scripts."""

from __future__ import annotations

import argparse
import os
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Iterator

from .config import ConfigBundle, cfg_get, resolve_path
from .terminal_events import load_terminal_event_ledger, load_terminal_event_policy
from .universe import MODEL_FAMILY, load_policy, normalize_ticker, read_csv


CACHE_ONLY_ENV = "CONSUMER_DEFENSIVE_CACHE_ONLY"


@contextmanager
def cache_only_environment(enabled: bool) -> Iterator[None]:
    """Scope the process-wide cache-only switch to one CLI invocation.

    Provider helpers also honor an externally supplied environment variable.
    A script-provided ``--cache-only`` flag, however, must not leak into a
    later command when ``main()`` is called in-process by an orchestrator or
    test runner.
    """
    previous = os.environ.get(CACHE_ONLY_ENV)
    if enabled:
        os.environ[CACHE_ONLY_ENV] = "1"
    try:
        yield
    finally:
        if enabled:
            if previous is None:
                os.environ.pop(CACHE_ONLY_ENV, None)
            else:
                os.environ[CACHE_ONLY_ENV] = previous


def iso_date(value: str) -> str:
    """Argparse type for an exact ISO calendar date (YYYY-MM-DD)."""
    text = str(value).strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid date {value!r}; expected YYYY-MM-DD.") from exc
    if parsed.isoformat() != text:
        raise argparse.ArgumentTypeError(f"Invalid date {value!r}; expected YYYY-MM-DD.")
    return text


def require_date_window(start: str, end: str) -> None:
    """Reject reversed date windows before a provider or database is touched."""
    if date.fromisoformat(start) > date.fromisoformat(end):
        raise ValueError(f"Invalid date window: start {start} is after end {end}.")


def parse_ticker_csv(value: str) -> list[str] | None:
    """Normalize and de-duplicate an optional comma-separated ticker scope."""
    tickers: list[str] = []
    seen: set[str] = set()
    for raw in str(value or "").split(","):
        ticker = normalize_ticker(raw)
        if ticker and ticker not in seen:
            seen.add(ticker)
            tickers.append(ticker)
    return tickers or None


def require_known_tickers(conn: sqlite3.Connection, tickers: list[str] | None) -> list[str] | None:
    """Fail on a misspelled or out-of-family targeted ticker instead of doing no work."""
    if not tickers:
        return None
    placeholders = ",".join("?" for _ in tickers)
    rows = conn.execute(
        f"""SELECT ticker FROM dim_consumer_defensive_taxonomy
            WHERE model_family=? AND ticker IN ({placeholders})""",
        [MODEL_FAMILY, *tickers],
    ).fetchall()
    known = {str(row[0]) for row in rows}
    unknown = sorted(set(tickers) - known)
    if unknown:
        raise ValueError(f"Unknown Consumer Defensive ticker scope: {unknown}")
    return tickers


def assert_stage4_universe_ready(conn: sqlite3.Connection, bundle: ConfigBundle) -> dict[str, int]:
    """Require the complete Stage 2 taxonomy before Stage 4 can silently no-op."""
    expected = int(cfg_get(bundle.payload, "specialized_disclosure_census.expected_applicability_rows"))
    universe_policy = load_policy(
        resolve_path(cfg_get(bundle.payload, 'universe.policy_path'), base_dir=bundle.base_dir)
    )
    current = {
        normalize_ticker(row['ticker'])
        for row in read_csv(universe_policy.resolve('authoritative_current_csv'))
    }
    terminal_policy = load_terminal_event_policy(
        universe_policy.resolve('terminal_event_policy')
    )
    historical = {
        normalize_ticker(event.ticker)
        for event in load_terminal_event_ledger(terminal_policy)
    }
    expected_tickers = current | historical
    if len(expected_tickers) != expected:
        raise RuntimeError(
            'Consumer Defensive Stage 4 authoritative scope/config mismatch; '
            f'expected_applicability_rows={expected} authoritative_tickers={len(expected_tickers)}.'
        )
    rows = conn.execute(
        '''SELECT t.ticker, s.ticker, c.primary_ticker, s.listing_status, c.is_active
           FROM dim_consumer_defensive_taxonomy t
           JOIN dim_security s ON s.security_id=t.security_id
           JOIN dim_company c ON c.company_id=t.company_id
           WHERE t.model_family=?''',
        (MODEL_FAMILY,),
    ).fetchall()
    actual_tickers = {normalize_ticker(str(row[0])) for row in rows}
    status_mismatches = sorted(
        str(row[0])
        for row in rows
        if str(row[0]) != str(row[1])
        or str(row[0]) != str(row[2])
        or (
            str(row[0]) in current
            and (str(row[3]) != 'active' or int(row[4]) != 1)
        )
        or (
            str(row[0]) in historical
            and (str(row[3]) != 'delisted' or int(row[4]) != 0)
        )
    )
    missing = sorted(expected_tickers - actual_tickers)
    extra = sorted(actual_tickers - expected_tickers)
    actual = len(actual_tickers)
    if missing or extra or status_mismatches:
        raise RuntimeError(
            "Consumer Defensive Stage 4 requires the complete current/historical taxonomy; "
            f'expected {expected} rows, found {actual}; missing={missing} extra={extra} '
            f'status_mismatches={status_mismatches}. Run Stage 2 first.'
        )
    return {"expected_taxonomy_rows": expected, "taxonomy_rows": actual}


def assert_stage4_raw_facts_ready(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    as_of: str | None = None,
) -> dict[str, int]:
    """Require SEC facts before FX normalization or feature construction."""
    readiness = assert_stage4_universe_ready(conn, bundle)
    if as_of is None:
        raw_facts = int(conn.execute("SELECT COUNT(*) FROM fact_sec_xbrl_fact_raw").fetchone()[0])
    else:
        cutoff = f"{as_of}T23:59:59Z"
        raw_facts = int(
            conn.execute(
                "SELECT COUNT(*) FROM fact_sec_xbrl_fact_raw WHERE accepted_at<=?",
                (cutoff,),
            ).fetchone()[0]
        )
    if raw_facts == 0:
        raise RuntimeError("No SEC XBRL facts are loaded. Run the Stage 4 SEC fundamentals sync first.")
    return {**readiness, "raw_xbrl_facts": raw_facts}


def assert_stage4_documents_ready(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    tickers: list[str] | None = None,
    *,
    as_of: str | None = None,
) -> dict[str, int]:
    """Require at least one hydrated filing for every requested census ticker."""
    readiness = assert_stage4_universe_ready(conn, bundle)
    scope = tickers or [
        str(row[0])
        for row in conn.execute(
            "SELECT ticker FROM dim_consumer_defensive_taxonomy WHERE model_family=? ORDER BY ticker",
            (MODEL_FAMILY,),
        )
    ]
    placeholders = ",".join("?" for _ in scope)
    date_clause = ' AND d.accepted_at<=?' if as_of is not None else ''
    association_clause = (
        ''' AND (
                b.association_status='active'
                OR (
                    b.association_status='retired'
                    AND b.retirement_effective_asof>?
                )
              )'''
        if as_of is not None
        else " AND b.association_status='active'"
    )
    query_params: list[str] = [*scope]
    if as_of is not None:
        cutoff = f"{as_of}T23:59:59Z"
        query_params.append(cutoff)
        query_params.append(cutoff)
    covered = {
        str(row[0])
        for row in conn.execute(
            f"""SELECT DISTINCT d.issuer_ticker
                FROM bridge_sec_filing_document_company d
                JOIN bridge_sec_filing_company b
                  ON b.accession_number=d.accession_number
                 AND b.issuer_company_id=d.issuer_company_id
                WHERE d.hydration_status='hydrated'
                  AND d.issuer_ticker IN ({placeholders})
                  {date_clause}{association_clause}""",
            query_params,
        )
    }
    missing = sorted(set(scope) - covered)
    if missing:
        raise RuntimeError(f"Hydrated SEC filing documents are missing for census tickers: {missing}")
    return {**readiness, "requested_tickers": len(scope), "document_tickers": len(covered)}


def stage4_output_dir(
    bundle: ConfigBundle,
    *,
    as_of: str,
    override: Path | None = None,
) -> Path:
    """Resolve the dated Stage 4 artifact directory."""
    if override is not None:
        return override.expanduser().resolve()
    return resolve_path(cfg_get(bundle.payload, "paths.output_dir"), base_dir=bundle.base_dir) / "stage4" / as_of


def compact_result(result: dict[str, Any], *large_keys: str) -> dict[str, Any]:
    """Remove row-level collections from run-log messages while retaining summaries."""
    omitted = set(large_keys)
    return {key: value for key, value in result.items() if key not in omitted}

from __future__ import annotations

import importlib
import sqlite3
from datetime import date
from typing import Any

from technology.core.db import init_db


ownership = importlib.import_module("technology.scripts.12_sync_technology_sec_ownership")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.execute(
        """
        INSERT INTO source_registry(
            source_id, stage, source_name, source_type, base_url,
            created_at, updated_at
        )
        VALUES ('sec_ownership_direct', 'stage_5', 'SEC ownership', 'api',
                'https://data.sec.gov', '2026-08-29T00:00:00Z',
                '2026-08-29T00:00:00Z')
        """
    )
    return conn


def _filing(accession: str) -> dict[str, Any]:
    return {
        "accession_number": accession,
        "form_type": "4",
        "filing_date": "2026-08-28",
        "report_date": "2026-08-27",
        "acceptance_datetime": "20260828160000",
        "primary_document": f"{accession}.xml",
    }


def test_daily_incremental_path_skips_successes_and_retries_failures() -> None:
    conn = _conn()
    parsed = {
        "issuer_cik": "0000000001",
        "issuer_name": "Example",
        "issuer_trading_symbol": "ABC",
        "document_type": "4",
        "period_of_report": "2026-08-27",
        "nonderiv_transactions": [],
        "deriv_transactions": [],
        "holdings": [],
    }
    ownership.upsert_filing(
        conn,
        ticker="ABC",
        cik="0000000001",
        source_id="sec_ownership_direct",
        filing=_filing("success"),
        parsed=parsed,
        owner={"reporting_owner_cik": "0000000002"},
        source_url="https://example.test/success.xml",
        raw_hash="a" * 64,
    )
    ownership.upsert_filing(
        conn,
        ticker="ABC",
        cik="0000000001",
        source_id="sec_ownership_direct",
        filing=_filing("retry"),
        parsed=None,
        owner={},
        source_url="https://example.test/retry.xml",
        raw_hash="b" * 64,
        parse_error="temporary failure",
    )

    pending, skipped = ownership.pending_ownership_filings(
        conn,
        ticker="ABC",
        source_id="sec_ownership_direct",
        filings=[_filing("success"), _filing("retry"), _filing("new")],
        reparse_history=False,
    )

    assert [row["accession_number"] for row in pending] == ["retry", "new"]
    assert skipped == 1


def test_explicit_history_reparse_keeps_every_discovered_accession() -> None:
    conn = _conn()
    filings = [_filing("old"), _filing("new")]

    pending, skipped = ownership.pending_ownership_filings(
        conn,
        ticker="ABC",
        source_id="sec_ownership_direct",
        filings=filings,
        reparse_history=True,
    )

    assert pending == filings
    assert skipped == 0


def test_reporting_profile_uses_persisted_totals_after_incremental_noop() -> None:
    conn = _conn()
    filing = _filing("success")
    owner = {
        "reporting_owner_cik": "0000000002",
        "reporting_owner_name": "Owner",
        "reporting_owner_relationship": "director",
        "reporting_owner_title": "",
        "is_director": 1,
        "is_officer": 0,
        "is_ten_percent_owner": 0,
    }
    transaction = {
        "transaction_seq": 1,
        "security_title": "Common Stock",
        "transaction_date": "2026-08-27",
        "deemed_execution_date": "",
        "transaction_code": "P",
        "equity_swap_involved": 0,
        "transaction_shares": 10.0,
        "transaction_price_per_share": 100.0,
        "transaction_value": 1000.0,
        "acquired_disposed_code": "A",
        "shares_owned_following_transaction": 20.0,
        "direct_or_indirect_ownership": "D",
        "nature_of_ownership": "",
        "footnotes": [],
    }
    parsed = {
        "issuer_cik": "0000000001",
        "issuer_name": "Example",
        "issuer_trading_symbol": "ABC",
        "document_type": "4",
        "period_of_report": "2026-08-27",
        "nonderiv_transactions": [transaction],
        "deriv_transactions": [],
        "holdings": [],
    }
    ownership.upsert_filing(
        conn,
        ticker="ABC",
        cik="0000000001",
        source_id="sec_ownership_direct",
        filing=filing,
        parsed=parsed,
        owner=owner,
        source_url="https://example.test/success.xml",
        raw_hash="a" * 64,
    )
    ownership.upsert_nonderiv_transaction(
        conn,
        ticker="ABC",
        source_id="sec_ownership_direct",
        accession="success",
        owner=owner,
        tx=transaction,
    )
    ownership.upsert_form4_compat(
        conn,
        ticker="ABC",
        source_id="sec_ownership_direct",
        filing=filing,
        owner=owner,
        tx=transaction,
        period_of_report="2026-08-27",
    )

    profile = ownership.upsert_reporting_profile(
        conn,
        {"ticker": "ABC", "cik": "0000000001", "country": "UNITED STATES", "is_fpi": 0},
        source_id="sec_ownership_direct",
        hfia_effective_date=date(2026, 3, 18),
    )

    assert profile["ownership_filing_count"] == 1
    assert profile["nonderivative_transactions"] == 1
    assert profile["derivative_transactions"] == 0
    assert profile["form4_compat_transactions"] == 1

from __future__ import annotations

from hashlib import sha256
import runpy
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from industrials.core.db import init_db, utc_now


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_mapped_xbrl_backfill_ignores_duplicate_logical_raw_facts() -> None:
    script = (
        PROJECT_ROOT
        / "industrials"
        / "scripts"
        / "08_build_industrials_financial_features.py"
    )
    backfill = runpy.run_path(str(script))["backfill_mapped_xbrl_facts"]
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    init_db(connection)
    now = utc_now()
    connection.execute(
        """
        INSERT INTO source_registry(
            source_id, stage, source_name, source_type, base_url,
            status, created_at, updated_at
        ) VALUES (
            'sec_companyfacts', 'financials', 'SEC test', 'test',
            'https://example.com', 'active', ?, ?
        )
        """,
        (now, now),
    )
    raw_common = {
        "ticker": "OSIS",
        "cik": "0001039065",
        "source_id": "sec_companyfacts",
        "accession_number": "0001104659-26-099752",
        "form_type": "10-Q",
        "filing_date": "2026-08-20",
        "accepted_at": "2026-08-20T12:00:00Z",
        "fiscal_year": 2026,
        "fiscal_period": "Q3",
        "period_start": "2026-04-01",
        "period_end": "2026-06-30",
        "frame": "CY2026Q2",
        "taxonomy": "us-gaap",
        "concept_name": "RevenueFromContractWithCustomerExcludingAssessedTax",
        "unit": "USD",
        "raw_value": 100.0,
        "decimals": "-3",
        "created_at": now,
        "updated_at": now,
    }
    for suffix in ("companyfacts", "cached_filing"):
        row = dict(
            raw_common,
            fact_key=f"osis-revenue-{suffix}",
            source_detail=suffix,
            payload_json="{}",
        )
        columns = ",".join(row)
        placeholders = ",".join("?" for _ in row)
        connection.execute(
            f"INSERT INTO fact_sec_xbrl_fact_raw({columns}) VALUES ({placeholders})",
            tuple(row.values()),
        )

    upgrade_module = runpy.run_path(
        str(
            PROJECT_ROOT
            / "industrials"
            / "machinery"
            / "stage12_contract_upgrade.py"
        )
    )
    assert upgrade_module["_mapped_fact_duplicate_group_count"](
        connection,
        tickers=["OSIS"],
        source_ids=["sec_companyfacts"],
        asof="2026-08-21",
    ) == 1

    assert backfill(
        connection,
        source_ids=("sec_companyfacts",),
        tickers=["OSIS"],
        asof=date(2026, 8, 21),
    ) == 1
    assert backfill(
        connection,
        source_ids=("sec_companyfacts",),
        tickers=["OSIS"],
        asof=date(2026, 8, 21),
    ) == 0
    mapped_count = connection.execute(
        "SELECT COUNT(*) FROM fact_sec_xbrl_fact "
        "WHERE ticker = 'OSIS' AND canonical_metric = 'revenue'"
    ).fetchone()[0]
    assert mapped_count == 1

def test_machinery_idempotency_amendment_requires_exact_sealed_patch(
    tmp_path: Path,
) -> None:
    module = runpy.run_path(
        str(
            PROJECT_ROOT
            / "industrials"
            / "machinery"
            / "stage12_contract_upgrade.py"
        )
    )
    source = (
        PROJECT_ROOT
        / "industrials"
        / "scripts"
        / "08_build_industrials_financial_features.py"
    ).read_text(encoding="utf-8")
    patch = module["MAPPED_FACT_IDEMPOTENCY_PATCH"]
    assert source.count(patch) == 1
    predecessor = source.replace(patch, "", 1)
    expected = sha256(predecessor.encode("utf-8")).hexdigest()
    candidate = tmp_path / "builder.py"
    candidate.write_text(source, encoding="utf-8")

    assert (
        module["_assert_exact_mapped_fact_idempotency_patch"](
            candidate, sealed_predecessor_sha256=expected
        )
        == expected
    )

    candidate.write_text(
        source.replace("             ) DO NOTHING\n", "             ) DO UPDATE\n", 1),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must occur exactly once"):
        module["_assert_exact_mapped_fact_idempotency_patch"](
            candidate, sealed_predecessor_sha256=expected
        )

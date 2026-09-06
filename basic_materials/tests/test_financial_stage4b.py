"""Focused regression tests for independent Basic Materials Stage 4B semantics."""

from __future__ import annotations

from pathlib import Path

from basic_materials.core.config import load_config
from basic_materials.core.db import (
    FINANCIAL_CONTRACT_SQL,
    FINANCIAL_INGESTION_SQL,
    FOUNDATION_SQL,
    HISTORICAL_RECONCILIATION_SQL,
    MARKET_DATA_SQL,
    connect,
    init_db,
    migration_checksum,
    utc_now,
)
from basic_materials.core.audited_html_financials import parse_ogc_audited_html
from basic_materials.core.financial_fx import parse_yahoo_rates
from basic_materials.core.financial_ingestion import (
    FilingRecord,
    _companyfacts_raw_rows,
    load_financial_ingestion_policy,
    parse_structured_filing_document,
)
from basic_materials.core.financial_normalization import (
    CanonicalCandidate,
    _apply_sign_policy,
    _canonicalize_candidates,
    _current_and_prior_flow,
    _current_and_prior_shares,
)
from basic_materials.core.security_ratios import (
    load_security_ratio_policy,
    load_security_ratio_rows,
)


def _policy():
    config = load_config()
    return load_financial_ingestion_policy(config.paths.financial_ingestion_policy)


def _filing(*, accepted_at: str = "2025-02-20T16:30:00Z") -> FilingRecord:
    return FilingRecord(
        filing_key="fixture-filing",
        company_id=1,
        security_id=1,
        ticker="FIX",
        cik="0001234567",
        accession_number="0001234567-25-000001",
        form_type="10-K",
        form_family="10-K",
        filing_date="2025-02-20",
        accepted_at=accepted_at,
        report_date="2024-12-31",
        primary_document="fixture.htm",
        source_id="sec_submissions",
        source_url="https://data.sec.gov/submissions/fixture.json",
        payload_sha256="a" * 64,
        fiscal_year="2024",
        fiscal_period="FY",
        is_amendment=0,
        filing_payload_kind="submissions",
        is_xbrl=True,
        is_inline_xbrl=True,
    )


def _concept_contract():
    return {
        ("ifrs-full", "Revenue"): (
            {
                "canonical_metric": "revenue",
                "period_type": "duration",
                "sign_policy": "preserve",
                "priority": 1,
            },
        ),
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"): (
            {
                "canonical_metric": "revenue",
                "period_type": "duration",
                "sign_policy": "preserve",
                "priority": 1,
            },
        ),
    }


def test_stage4b_policy_has_exact_exception_and_pilot_contract() -> None:
    policy = _policy()
    assert policy.version == "basic_materials_financial_ingestion_v2"
    assert policy.as_of_date == "2026-09-05"
    assert len(policy.checksum) == 64
    assert len(policy.payload["exception_resolutions"]) == 14
    assert policy.payload["snapshot"]["representative_pilot_tickers"] == [
        "NUE",
        "BHP",
        "AEM",
        "RMIX",
    ]
    assert policy.payload["exception_resolutions"]["OGC"]["resolution_status"] == (
        "resolved_audited_html"
    )
    assert policy.payload["exception_resolutions"]["OGC"] == {
        **policy.payload["exception_resolutions"]["OGC"],
        "route": "audited_filing_html",
        "canonical_eligible": True,
        "evidence_accession": "0001628280-26-029399",
        "evidence_accepted_at": "2026-05-01T20:53:37Z",
        "evidence_document": "exhibit991-oceanagoldfinan.htm",
    }
    assert policy.payload["exception_resolutions"]["AGU"]["taxonomy"] == "ifrs-full"
    assert policy.payload["exception_resolutions"]["POT"]["reporting_currency"] == "USD"
    assert policy.payload["exception_resolutions"]["ASM"] == {
        **policy.payload["exception_resolutions"]["ASM"],
        "route": "companyfacts_with_unstructured_latest_annual",
        "resolution_status": "resolved_metadata_unstructured",
        "canonical_eligible": True,
        "evidence_document": "avino_ex991.htm",
    }
    assert policy.payload["exception_resolutions"]["CGAU"] == {
        **policy.payload["exception_resolutions"]["CGAU"],
        "route": "companyfacts_with_unstructured_latest_annual",
        "resolution_status": "resolved_metadata_unstructured",
        "canonical_eligible": True,
        "evidence_document": "tm261243d1_ex99-3.htm",
    }
    assert policy.payload["exception_resolutions"]["TII"] == {
        **policy.payload["exception_resolutions"]["TII"],
        "route": "filing_package_xbrl_instance",
        "resolution_status": "resolved_xbrl_instance",
        "evidence_accession": "0001213900-26-031504",
        "evidence_document": "ea0281557-40f_titan_htm.xml",
    }


def test_schema_v5_migrates_to_v7_with_remediation_contract(tmp_path: Path) -> None:
    conn = connect(tmp_path / "basic_materials.sqlite")
    try:
        migrations = (
            (1, "basic_materials_foundation", FOUNDATION_SQL),
            (2, "basic_materials_historical_reconciliation", HISTORICAL_RECONCILIATION_SQL),
            (3, "basic_materials_adjusted_market_data", MARKET_DATA_SQL),
            (4, "basic_materials_financial_contract", FINANCIAL_CONTRACT_SQL),
            (5, "basic_materials_financial_ingestion", FINANCIAL_INGESTION_SQL),
        )
        for version, name, sql in migrations:
            conn.executescript(sql)
            conn.execute(
                """
                INSERT INTO schema_migrations(version, name, checksum, applied_at_utc)
                VALUES (?, ?, ?, ?)
                """,
                (version, name, migration_checksum(sql), utc_now()),
            )
        conn.execute(
            """
            INSERT INTO sector_database_identity(
                identity_id, model_family, sector, schema_owner, schema_version, created_at_utc
            ) VALUES (1, 'basic_materials', 'Basic Materials', 'basic_materials', 5, ?)
            """,
            (utc_now(),),
        )
        conn.commit()
        result = init_db(conn)
        assert result["schema_version"] == 7
        assert result["migrations_applied"] == [6, 7]
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='dim_financial_profile_resolution'"
        ).fetchone()[0] == 1
        raw_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(fact_sec_xbrl_fact_raw)").fetchall()
        }
        assert {"fiscal_year", "fiscal_period", "payload_sha256", "quality_status"} <= raw_columns
        index_names = {
            str(row["name"])
            for row in conn.execute(
                "PRAGMA index_list(fact_financial_statement_canonical)"
            ).fetchall()
        }
        assert "idx_canonical_financial_superseded_by" in index_names
        assert "idx_canonical_financial_snapshot_ticker" in index_names
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='dim_security_share_ratio'"
        ).fetchone()[0] == 1
        feature_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(feature_financial_statement)").fetchall()
        }
        assert {
            "security_ratio_key",
            "security_basis",
            "issuer_shares_per_traded_security",
        } <= feature_columns
    finally:
        conn.close()


def test_companyfacts_requires_exact_acceptance_and_excludes_future() -> None:
    filing = _filing()
    profile = {
        "ticker": "FIX",
        "cik": "0001234567",
        "company_id": 1,
        "security_id": 1,
        "source_cutoff_date": "2025-12-31",
    }
    payload = {
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {
                        "USD": [
                            {
                                "start": "2024-01-01",
                                "end": "2024-12-31",
                                "val": 100,
                                "accn": filing.accession_number,
                                "fy": 2024,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2025-02-20",
                            },
                            {
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "val": 200,
                                "accn": "0001234567-26-000002",
                                "fy": 2025,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2026-02-20",
                            },
                        ]
                    }
                }
            }
        }
    }
    rows, accessions, issues = _companyfacts_raw_rows(
        profile,
        payload,
        payload_sha256="b" * 64,
        evidence_url="https://data.sec.gov/api/xbrl/companyfacts/fixture.json",
        filing_lookup={("FIX", filing.accession_number): filing},
        concept_contract=_concept_contract(),
        history_start_date="2009-01-01",
    )
    assert len(rows) == 1
    assert rows[0].accepted_at == filing.accepted_at
    assert rows[0].quality_status == "usable"
    assert accessions == {filing.accession_number}
    assert any(item["issue_code"] == "FUTURE_FACTS_EXCLUDED" for item in issues)


def test_structured_instance_preserves_context_unit_scale_and_lineage() -> None:
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:ifrs-full="https://xbrl.ifrs.org/taxonomy/2024-03-27/ifrs-full"
      xmlns:iso4217="http://www.xbrl.org/2003/iso4217">
      <xbrli:context id="FY">
        <xbrli:entity><xbrli:identifier scheme="test">1</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:startDate>2024-01-01</xbrli:startDate>
          <xbrli:endDate>2024-12-31</xbrli:endDate></xbrli:period>
      </xbrli:context>
      <xbrli:unit id="USD"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
      <ifrs-full:Revenue contextRef="FY" unitRef="USD" decimals="-3">1,234</ifrs-full:Revenue>
    </xbrli:xbrl>"""
    filing = _filing()
    rows = parse_structured_filing_document(
        xml,
        profile={"company_id": 1, "security_id": 1},
        filing=filing,
        source_url="https://www.sec.gov/Archives/fixture.xml",
        payload_sha256="c" * 64,
        concept_contract=_concept_contract(),
        source_detail="filing_package_xbrl_instance",
    )
    assert len(rows) == 1
    assert rows[0].taxonomy == "ifrs-full"
    assert rows[0].concept == "Revenue"
    assert rows[0].numeric_value == 1234
    assert rows[0].unit == "USD"
    assert rows[0].period_start == "2024-01-01"
    assert rows[0].period_end == "2024-12-31"
    assert rows[0].accepted_at == filing.accepted_at
    assert rows[0].quality_status == "usable"
    assert rows[0].payload_sha256 == "c" * 64


def test_ogc_audited_html_parser_extracts_42_tied_observations() -> None:
    html = b"""
    <html><body>
    <table>
      <tr><td>Balance</td><td>2025</td><td>2024</td></tr>
      <tr><td>Cash and cash equivalents</td><td>100</td><td>90</td></tr>
      <tr><td>Trade and other receivables</td><td>50</td><td>45</td></tr>
      <tr><td>Inventories</td><td>70</td><td>60</td></tr>
      <tr><td>Trade and other receivables</td><td>10</td><td>8</td></tr>
      <tr><td>Inventories</td><td>5</td><td>4</td></tr>
      <tr><td>Debt</td><td>50</td><td>40</td></tr>
      <tr><td>Trade and other payables</td><td>80</td><td>70</td></tr>
      <tr><td>TOTAL ASSETS</td><td>1000</td><td>900</td></tr>
      <tr><td>TOTAL LIABILITIES</td><td>400</td><td>350</td></tr>
      <tr><td>TOTAL SHAREHOLDERS' EQUITY</td><td>600</td><td>550</td></tr>
    </table>
    <table>
      <tr><td>Income</td><td>2025</td><td>2024</td></tr>
      <tr><td>Revenue</td><td>1000</td><td>900</td></tr>
      <tr><td>Cost of sales, excluding depreciation and amortization</td><td>(600)</td><td>(550)</td></tr>
      <tr><td>Operating profit</td><td>200</td><td>180</td></tr>
      <tr><td>Depreciation and amortization</td><td>(50)</td><td>(45)</td></tr>
      <tr><td>Interest expense and finance costs</td><td>(10)</td><td>(9)</td></tr>
      <tr><td>Income tax expense</td><td>(40)</td><td>(35)</td></tr>
      <tr><td>Profit before income tax</td><td>160</td><td>145</td></tr>
      <tr><td>Net profit</td><td>120</td><td>110</td></tr>
    </table>
    <table>
      <tr><td>Cash flow</td><td>2025</td><td>2024</td></tr>
      <tr><td>Net profit</td><td>120</td><td>110</td></tr>
      <tr><td>Net cash provided by operating activities</td><td>250</td><td>220</td></tr>
      <tr><td>Payment for property, plant and equipment</td><td>(80)</td><td>(70)</td></tr>
      <tr><td>Payment for mining assets</td><td>(70)</td><td>(60)</td></tr>
      <tr><td>Dividends paid to equity holders of the Company</td><td>(20)</td><td>(18)</td></tr>
      <tr><td>Share buybacks</td><td>-</td><td>-</td></tr>
      <tr><td>Cash and cash equivalents at the end of the year</td><td>100</td><td>90</td></tr>
    </table>
    <table>
      <tr><td>Shares</td><td>2025</td><td>2024</td></tr>
      <tr><td>Basic weighted average number of shares (in millions)</td><td>200</td><td>190</td></tr>
      <tr><td>Diluted weighted average number of shares (in millions)</td><td>205</td><td>195</td></tr>
    </table>
    <table>
      <tr><td>Capital</td><td>2025</td><td>2024</td></tr>
      <tr><td>Total debt</td><td>200</td><td>180</td></tr>
      <tr><td>Total equity</td><td>600</td><td>550</td></tr>
      <tr><td>Net (cash) / debt</td><td>100</td><td>90</td></tr>
    </table>
    </body></html>
    """
    rows = parse_ogc_audited_html(html)
    assert len(rows) == 42
    assert len({row.canonical_metric for row in rows}) == 21
    assert {row.period_end for row in rows} == {"2024-12-31", "2025-12-31"}
    revenue = [row.numeric_value for row in rows if row.canonical_metric == "revenue"]
    capex = [
        row.numeric_value
        for row in rows
        if row.canonical_metric == "capital_expenditures"
    ]
    assert revenue == [900_000_000.0, 1_000_000_000.0]
    assert capex == [-130_000_000.0, -150_000_000.0]


def test_security_ratio_contract_has_exact_foreign_and_ads_census() -> None:
    config = load_config()
    policy = load_security_ratio_policy(config.paths.security_ratio_policy)
    rows = load_security_ratio_rows(
        config.paths.security_share_ratios_csv,
        policy=policy,
    )
    assert len(rows) == 47
    assert sum(row["security_basis"] == "direct_share" for row in rows) == 42
    assert {
        row["ticker"]: row["issuer_shares_per_traded_security"]
        for row in rows
        if row["security_basis"] == "adr_ads"
    } == {"BHP": 2.0, "ELVR": 10.0, "PKX": 0.25, "RIO": 1.0, "TX": 10.0}


def _candidate(value: float, *, source: str = "sec_companyfacts", observation: str = "one"):
    return CanonicalCandidate(
        source_observation_id=observation,
        filing_key="fixture-filing",
        company_id=1,
        security_id=1,
        ticker="FIX",
        canonical_metric="revenue",
        period_start="2024-01-01",
        period_end="2024-12-31",
        period_type="duration",
        accepted_at="2025-02-20T16:30:00Z",
        accession_number="0001234567-25-000001",
        form_type="10-K",
        taxonomy="us-gaap",
        concept="RevenueFromContractWithCustomerExcludingAssessedTax",
        reported_value=value,
        reported_currency="USD",
        source_id=source,
        source_priority=1 if source == "sec_companyfacts" else 2,
        concept_priority=1,
        fiscal_year="2024",
        fiscal_period="FY",
        context_id="",
        evidence_url="https://data.sec.gov/fixture",
        payload_sha256="d" * 64,
    )


def test_normalization_prefers_companyfacts_and_quarantines_equal_rank_conflicts(
    tmp_path: Path,
) -> None:
    policy = _policy()
    conn = connect(tmp_path / "basic_materials.sqlite")
    try:
        init_db(conn)
        preferred, issues = _canonicalize_candidates(
            conn,
            candidates=[
                _candidate(100, source="sec_companyfacts", observation="companyfacts"),
                _candidate(999, source="sec_inline_xbrl_fallback", observation="fallback"),
            ],
            snapshot_key="fixture",
            policy=policy,
        )
        assert len(preferred) == 1
        assert preferred[0].reported_value == 100
        assert preferred[0].quality_status == "usable"
        assert not issues
        conflicted, conflict_issues = _canonicalize_candidates(
            conn,
            candidates=[
                _candidate(100, observation="one"),
                _candidate(101, observation="two"),
            ],
            snapshot_key="fixture",
            policy=policy,
        )
        assert conflicted[0].quality_status == "conflicted"
        assert conflict_issues[0]["issue_code"] == "CANONICAL_VALUE_CONFLICT"
    finally:
        conn.close()


def _flow_row(start: str, end: str, value: float, fact_id: str):
    return {
        "period_start": start,
        "period_end": end,
        "accepted_at": f"{int(end[:4]) + 1}-02-01T12:00:00Z",
        "accession_number": fact_id,
        "canonical_fact_id": fact_id,
        "usd_value": value,
        "reported_value": value,
    }


def test_ttm_and_share_semantics_do_not_sum_weighted_average_shares() -> None:
    policy = _policy()
    rows = [
        _flow_row("2023-01-01", "2023-03-31", 1, "q1"),
        _flow_row("2023-04-01", "2023-06-30", 2, "q2"),
        _flow_row("2023-07-01", "2023-09-30", 3, "q3"),
        _flow_row("2023-10-01", "2023-12-31", 4, "q4"),
        _flow_row("2024-01-01", "2024-03-31", 10, "q5"),
        _flow_row("2024-04-01", "2024-06-30", 20, "q6"),
        _flow_row("2024-07-01", "2024-09-30", 30, "q7"),
        _flow_row("2024-10-01", "2024-12-31", 40, "q8"),
    ]
    current, prior = _current_and_prior_flow(rows, metric="revenue", policy=policy)
    assert current is not None and current.value == 100
    assert prior is not None and prior.value == 10
    share_rows = [replace_row | {"reported_value": 100 + index} for index, replace_row in enumerate(rows)]
    shares, prior_shares = _current_and_prior_shares(share_rows, policy=policy)
    assert shares is not None and shares.value == 107
    assert prior_shares is not None and prior_shares.value == 103


def test_capex_sign_and_yahoo_inverse_rate_semantics() -> None:
    assert _apply_sign_policy(-25.0, "positive_outflow") == 25.0
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [1735689600],
                    "meta": {"regularMarketTime": 1735689600},
                    "indicators": {"quote": [{"close": [2.0]}]},
                }
            ]
        }
    }
    rates = parse_yahoo_rates(
        payload,
        base_currency="CAD",
        invert=True,
        payload_sha256="e" * 64,
    )
    assert len(rates) == 1
    assert rates[0].rate == 0.5
    assert rates[0].quote_currency == "USD"

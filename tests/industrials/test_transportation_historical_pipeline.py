from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from industrials.core.db import connect, init_db, utc_now
from industrials.core.source_registry import load_source_registry, upsert_source_registry
from industrials.transportation.disclosure_candidates import (
    EXTRACTION_METHOD,
    extract_transportation_disclosure_candidates,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INDUSTRIALS_ROOT = PROJECT_ROOT / "industrials"


def load_script(name: str):
    path = INDUSTRIALS_ROOT / "transportation" / "scripts" / name
    spec = importlib.util.spec_from_file_location(
        f"transportation_historical_{name.replace('.', '_')}",
        path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_shared_script(name: str):
    path = INDUSTRIALS_ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(
        f"transportation_shared_historical_{name.replace('.', '_')}",
        path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def seed_source_registry(conn) -> None:
    init_db(conn)
    registry = load_source_registry(
        INDUSTRIALS_ROOT / "data" / "free_source_registry.yaml"
    )
    upsert_source_registry(conn, registry)


def insert_filing(
    conn,
    *,
    accession: str,
    form: str,
    filing_date: str,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO fact_sec_filing(
            ticker, cik, source_id, accession_number, form_type, filing_date,
            accepted_at, report_date, primary_document, created_at, updated_at
        )
        VALUES ('TEST', '0000000001', 'sec_submissions', ?, ?, ?, ?, ?,
                'test.htm', ?, ?)
        """,
        (
            accession,
            form,
            filing_date,
            f"{filing_date}T16:00:00Z",
            filing_date,
            now,
            now,
        ),
    )


def test_historical_filing_selection_is_form_date_and_cap_bounded(
    tmp_path: Path,
) -> None:
    sync = load_script("08c_sync_transportation_specialized_disclosures.py")
    db_path = tmp_path / "historical.sqlite"
    with connect(db_path) as conn:
        seed_source_registry(conn)
        insert_filing(
            conn,
            accession="0000000001-18-000001",
            form="10-K",
            filing_date="2018-02-01",
        )
        insert_filing(
            conn,
            accession="0000000001-19-000001",
            form="10-Q",
            filing_date="2019-05-01",
        )
        insert_filing(
            conn,
            accession="0000000001-20-000001",
            form="8-K",
            filing_date="2020-05-01",
        )
        insert_filing(
            conn,
            accession="0000000001-20-000002",
            form="6-K",
            filing_date="2020-08-01",
        )
        insert_filing(
            conn,
            accession="0000000001-21-000001",
            form="10-K",
            filing_date="2021-02-01",
        )
        selected = sync._selected_filings(
            conn,
            ticker="TEST",
            source_id="sec_submissions",
            asof="2021-12-31",
            annual_limit=0,
            interim_limit=0,
            start_date="2018-11-01",
            max_total=0,
            financial_6k_only=True,
        )
    assert [row["accession_number"] for row in selected] == [
        "0000000001-21-000001",
        "0000000001-19-000001",
    ]


def test_document_checkpoint_replaces_only_its_accession(
    tmp_path: Path,
) -> None:
    sync = load_script("08c_sync_transportation_specialized_disclosures.py")
    db_path = tmp_path / "checkpoint.sqlite"
    filing = {
        "accession_number": "0000000001-26-000001",
        "form_type": "10-Q",
        "filing_date": "2026-05-01",
        "accepted_at": "2026-05-01T16:00:00Z",
        "report_date": "2026-03-31",
    }
    candidates = extract_transportation_disclosure_candidates(
        "<p>Passenger load factor was 84.1%.</p>",
        filing=filing,
        cohort="air_transport_and_aviation_services",
        industry="Airlines",
    )
    with connect(db_path) as conn:
        seed_source_registry(conn)
        with conn:
            sync._replace_document_candidates(
                conn,
                ticker="TEST",
                cik="0000000001",
                source_id="sec_companyfacts",
                filing=filing,
                document_name="test.htm",
                source_url=(
                    "https://www.sec.gov/Archives/edgar/data/1/"
                    "000000000126000001/test.htm"
                ),
                content_sha256="a" * 64,
                candidates=candidates,
                now=utc_now(),
            )
        assert sync._scanned_accessions(
            conn,
            ticker="TEST",
            source_id="sec_companyfacts",
        ) == {"0000000001-26-000001"}
        assert conn.execute(
            """
            SELECT COUNT(*) FROM fact_sec_metric_disclosure_candidate
            WHERE extraction_method=?
            """,
            (EXTRACTION_METHOD,),
        ).fetchone()[0] == 1
        with conn:
            sync._replace_document_candidates(
                conn,
                ticker="TEST",
                cik="0000000001",
                source_id="sec_companyfacts",
                filing=filing,
                document_name="test.htm",
                source_url=(
                    "https://www.sec.gov/Archives/edgar/data/1/"
                    "000000000126000001/test.htm"
                ),
                content_sha256="b" * 64,
                candidates=[],
                now=utc_now(),
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM fact_sec_metric_disclosure_candidate"
        ).fetchone()[0] == 0
        scan = conn.execute(
            """
            SELECT candidate_count, content_sha256
            FROM fact_sec_metric_disclosure_document_scan
            """
        ).fetchone()
    assert scan["candidate_count"] == 0
    assert scan["content_sha256"] == "b" * 64


def test_specialized_candidate_lookup_rejects_stale_and_old_parser_rows(
    tmp_path: Path,
) -> None:
    builder = load_script("08a_build_transportation_specialized_metrics.py")
    db_path = tmp_path / "candidate_staleness.sqlite"
    now = utc_now()
    with connect(db_path) as conn:
        seed_source_registry(conn)
        for key, accepted_at, method, value in (
            ("stale", "2024-01-01T16:00:00Z", EXTRACTION_METHOD, 0.10),
            ("old-parser", "2026-01-01T16:00:00Z", "transportation_sec_filing_prose_v1", 0.20),
            ("current", "2026-01-02T16:00:00Z", EXTRACTION_METHOD, 0.30),
        ):
            conn.execute(
                """
                INSERT INTO fact_sec_metric_disclosure_candidate(
                    candidate_key, ticker, source_id, model_family,
                    accession_number, form_type, filing_date, accepted_at,
                    document_name, metric_name, concept_name, candidate_value,
                    unit, scope, extraction_method, confidence,
                    candidate_status, created_at, updated_at
                )
                VALUES (?, 'TEST', 'sec_companyfacts', 'transportation', ?,
                        '10-Q', SUBSTR(?, 1, 10), ?, 'test.htm',
                        'traffic_growth', 'traffic_growth', ?, 'ratio',
                        'issuer_reported', ?, 0.9, 'ACCEPTED', ?, ?)
                """,
                (
                    key,
                    key,
                    accepted_at,
                    accepted_at,
                    value,
                    method,
                    now,
                    now,
                ),
            )
        conn.execute(
            """
            INSERT INTO fact_sec_metric_disclosure_candidate(
                candidate_key, ticker, source_id, model_family,
                accession_number, form_type, filing_date, accepted_at,
                document_name, metric_name, concept_name, candidate_value,
                unit, scope, extraction_method, confidence,
                candidate_status, created_at, updated_at
            )
            VALUES ('after-close', 'TEST', 'sec_companyfacts',
                    'transportation', 'after-close', '10-Q', '2026-07-23',
                    '2026-07-22T22:00:00Z', 'test.htm', 'traffic_growth',
                    'traffic_growth', 0.40, 'ratio', 'issuer_reported',
                    ?, 0.99, 'ACCEPTED', ?, ?)
            """,
            (EXTRACTION_METHOD, now, now),
        )
        row = builder.candidate_row(
            conn,
            ticker="TEST",
            metric="traffic_growth",
            asof="2026-07-22",
            max_staleness_days=400,
        )
    assert row["candidate_key"] == "current"
    assert row["candidate_value"] == 0.30


def test_month_end_observation_dates_include_initial_loaded_bar(
    tmp_path: Path,
) -> None:
    history = load_script("19_build_transportation_pit_feature_history.py")
    db_path = tmp_path / "dates.sqlite"
    now = utc_now()
    with connect(db_path) as conn:
        seed_source_registry(conn)
        for bar_date in ("2019-01-02", "2019-01-31", "2019-02-15", "2019-02-28"):
            conn.execute(
                """
                INSERT INTO fact_price_ohlcv(
                    ticker, source_id, bar_date, open, high, low, close,
                    adj_close, volume, created_at, updated_at
                )
                VALUES ('IYT', 'yahoo_finance_adjusted', ?, 100, 100, 100,
                        100, 100, 1000, ?, ?)
                """,
                (bar_date, now, now),
            )
        dates = history.month_end_dates(
            conn,
            ticker="IYT",
            source_id="yahoo_finance_adjusted",
            start_date="2019-01-02",
            end_date="2019-02-28",
        )
    assert dates == ["2019-01-02", "2019-01-31", "2019-02-28"]


def test_history_resume_requires_exact_coverage_report_and_artifacts(
    tmp_path: Path,
) -> None:
    history = load_script("19_build_transportation_pit_feature_history.py")
    asof = "2020-01-31"
    counts = {
        "expected": 2,
        "market": 2,
        "financial": 2,
        "availability": 78,
        "expected_availability": 78,
        "profiles": 2,
    }
    report_row = {"status": "PASS"}
    assert not history.snapshot_is_complete(
        asof=asof,
        counts=counts,
        report_row=report_row,
        output_root=tmp_path,
    )

    output_dir = tmp_path / asof
    output_dir.mkdir()
    for filename in history.SNAPSHOT_ARTIFACTS:
        (output_dir / filename).write_text("header\n", encoding="utf-8")
    assert history.snapshot_is_complete(
        asof=asof,
        counts=counts,
        report_row=report_row,
        output_root=tmp_path,
    )
    assert not history.snapshot_is_complete(
        asof=asof,
        counts=counts,
        report_row={"status": "FAIL"},
        output_root=tmp_path,
    )


def test_transportation_financial_builder_uses_dated_reporting_profile(
    tmp_path: Path,
) -> None:
    builder = load_shared_script("08_build_industrials_financial_features.py")
    db_path = tmp_path / "profiles.sqlite"
    now = utc_now()
    with connect(db_path) as conn:
        seed_source_registry(conn)
        conn.execute(
            """
            INSERT INTO dim_issuer_reporting_profile_history(
                ticker, model_family, profile_asof_date, cik, country,
                reporting_profile, reporting_standard, primary_taxonomy,
                fallback_status, financial_confidence, usable_xbrl_flag,
                source_id, review_reason, created_at, updated_at
            )
            VALUES ('TEST', 'transportation', '2019-03-31', '0000000001',
                    'United States', 'SEC_XBRL_US_GAAP', 'US_GAAP', 'us-gaap',
                    'none', 0.9, 1, 'sec_companyfacts', '', ?, ?)
            """,
            (now, now),
        )
        profile = builder.load_profile(
            conn,
            ticker="TEST",
            model_family="transportation",
            company={
                "ticker": "TEST",
                "cik": "0000000001",
                "country": "United States",
            },
            source_id="sec_companyfacts",
            asof=builder.date(2019, 6, 30),
        )
    assert profile["profile_asof_date"] == "2019-03-31"
    assert profile["reporting_profile"] == "SEC_XBRL_US_GAAP"

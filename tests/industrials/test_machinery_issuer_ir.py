from __future__ import annotations

import csv
import runpy
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from industrials.core.db import connect, init_db, utc_now
from industrials.machinery.disclosure_candidates import (
    DisclosureCandidate,
    extract_machinery_prose_candidates,
    replace_document_candidates_and_facts,
)
from industrials.machinery.disclosure_documents import extract_document_text
from industrials.machinery.issuer_ir import (
    ISSUER_IR_SOURCE_DETAIL,
    IssuerIRDocument,
    apply_issuer_ir_policy,
    load_issuer_ir_manifest,
    validate_final_url,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _write_manifest(path: Path, **overrides: str) -> None:
    row = {
        "ticker": "CAT",
        "document_type": "EARNINGS_RELEASE",
        "published_at": "2026-04-20T12:30:00Z",
        "period_end": "2026-03-31",
        "title": "First-quarter results",
        "url": "https://ir.example.com/results/q1.html",
        "approved_domain": "example.com",
        "scope_override": "consolidated",
        "reviewed_by": "analyst",
        "reviewed_at": "2026-04-21",
        "expected_sha256": "",
        "enabled": "1",
        "notes": "reviewed issuer source",
    }
    row.update(overrides)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)


def test_issuer_ir_manifest_enforces_timestamp_domain_and_reviewed_scope(
    tmp_path: Path,
) -> None:
    path = tmp_path / "issuer_ir.csv"
    _write_manifest(path)
    documents = load_issuer_ir_manifest(path)
    assert len(documents) == 1
    document = documents[0]
    assert document.published_at == "2026-04-20T12:30:00Z"
    validate_final_url(document, "https://cdn.ir.example.com/results/q1.html")
    with pytest.raises(ValueError, match="outside approved_domain"):
        validate_final_url(document, "https://untrusted.example.net/q1.html")

    _write_manifest(path, published_at="2026-04-20", scope_override="")
    with pytest.raises(ValueError, match="explicit UTC offset"):
        load_issuer_ir_manifest(path)

    _write_manifest(path, reviewed_by="", reviewed_at="")
    with pytest.raises(ValueError, match="requires reviewed_by and reviewed_at"):
        load_issuer_ir_manifest(path)


def test_issuer_ir_policy_never_promotes_transcripts_or_unknown_scope() -> None:
    release = IssuerIRDocument(
        ticker="CAT",
        document_type="EARNINGS_RELEASE",
        published_at="2026-04-20T12:30:00Z",
        period_end="2026-03-31",
        title="Results",
        url="https://ir.example.com/q1.html",
        approved_domain="example.com",
        scope_override="",
        reviewed_by="",
        reviewed_at="",
        expected_sha256="",
        notes="",
    )
    candidate = DisclosureCandidate(
        concept_name="ReportedBacklog",
        metric_name="reported_backlog",
        value=1_200_000_000.0,
        unit="USD",
        period_start="",
        period_end="2026-03-31",
        scope="unknown",
        confidence=0.90,
        candidate_status="ACCEPTED",
        status_reason="explicit_consolidated_prose_value",
        evidence_text="Backlog was $1.2 billion as of March 31, 2026",
        block_index=0,
    )
    scoped = apply_issuer_ir_policy([candidate], document=release)
    assert scoped[0].candidate_status == "REVIEW_REQUIRED"
    assert scoped[0].status_reason == "issuer_ir_consolidated_scope_not_established"

    reviewed_release = replace(
        release,
        scope_override="consolidated",
        reviewed_by="analyst",
        reviewed_at="2026-04-21",
    )
    accepted = apply_issuer_ir_policy([candidate], document=reviewed_release)
    assert accepted[0].candidate_status == "ACCEPTED"
    assert accepted[0].scope == "consolidated"
    assert accepted[0].confidence == 0.80

    transcript = replace(release, document_type="EARNINGS_TRANSCRIPT")
    review_only = apply_issuer_ir_policy(
        [replace(candidate, scope="consolidated")],
        document=transcript,
    )
    assert review_only[0].candidate_status == "REVIEW_REQUIRED"
    assert "transcript" in review_only[0].status_reason


def test_firm_backlog_is_reported_not_funded_backlog() -> None:
    candidates = extract_machinery_prose_candidates(
        "<p>Our firm backlog stood at $1.2 billion.</p>",
        filing={"report_date": "2026-03-31", "filing_date": "2026-04-20"},
        company_currency="USD",
    )
    assert [(candidate.concept_name, candidate.metric_name) for candidate in candidates] == [
        ("ReportedBacklog", "reported_backlog")
    ]


def test_pdf_extraction_rejects_oversized_documents_before_parsing() -> None:
    result = extract_document_text(
        b"%PDF-1.4\n" + (b"x" * 32),
        document_name="oversized.pdf",
        max_pdf_bytes=16,
    )
    assert result.extraction_method == "pdf_size_limit"
    assert "max_pdf_bytes:16" in result.warning


def test_pdf_extraction_terminates_worker_at_deadline() -> None:
    result = extract_document_text(
        b"%PDF-1.4\n%%EOF",
        document_name="bounded.pdf",
        max_pdf_bytes=1_000,
        pdf_extraction_timeout_sec=0.001,
    )
    assert result.extraction_method == "pdf_pypdf_timeout"
    assert result.warning == "extraction_timeout_seconds:0.001"


def _insert_source(conn: object, source_id: str) -> None:
    now = utc_now()
    conn.execute(  # type: ignore[attr-defined]
        """
        INSERT INTO source_registry(
            source_id, stage, source_name, source_type, base_url,
            status, created_at, updated_at
        ) VALUES (?, 'financials', ?, 'test', 'https://example.com', 'active', ?, ?)
        """,
        (source_id, source_id, now, now),
    )


def test_issuer_ir_facts_merge_under_primary_financial_source_with_sec_precedence(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "08_build_industrials_financial_features.py")
    )
    refresh_canonical_facts = namespace["refresh_canonical_facts"]
    load_canonical_rows = namespace["load_canonical_rows"]
    select_fact = namespace["select_fact"]
    db_path = tmp_path / "issuer_ir.sqlite"
    now = utc_now()
    ir_candidate = DisclosureCandidate(
        concept_name="ReportedBacklog",
        metric_name="reported_backlog",
        value=1_200_000_000.0,
        unit="USD",
        period_start="",
        period_end="2026-03-31",
        scope="consolidated",
        confidence=0.80,
        candidate_status="ACCEPTED",
        status_reason="reviewed_manifest_consolidated_scope",
        evidence_text="Our backlog was $1.2 billion as of March 31, 2026",
        block_index=0,
        extraction_method="issuer_ir_earnings_release",
    )
    with connect(db_path) as conn, conn:
        init_db(conn)
        _insert_source(conn, "sec_companyfacts")
        _insert_source(conn, "machinery_issuer_ir")
        conn.execute(
            """
            INSERT INTO dim_company(
                ticker, cik, company_name, currency, first_seen_at, updated_at
            ) VALUES ('CAT', '0000018230', 'Caterpillar', 'USD', ?, ?)
            """,
            (now, now),
        )
        replace_document_candidates_and_facts(
            conn,
            ticker="CAT",
            cik="0000018230",
            source_id="machinery_issuer_ir",
            model_family="machinery",
            filing={
                "accession_number": "IR-ONE",
                "form_type": "IR-RELEASE",
                "filing_date": "2026-04-20",
                "accepted_at": "2026-04-20T12:30:00Z",
                "fiscal_year": 2026,
                "fiscal_period": "Q1",
            },
            document_name="q1.html",
            candidates=[ir_candidate],
            now=now,
            source_detail=ISSUER_IR_SOURCE_DETAIL,
            taxonomy="issuer-ir",
            source_priority_floor=240,
        )
        assert refresh_canonical_facts(
            conn,
            source_id="sec_companyfacts",
            model_family="machinery",
            tickers=["CAT"],
            asof=date(2026, 4, 30),
            supplemental_source_ids=("machinery_issuer_ir",),
        ) == 1
        rows = load_canonical_rows(
            conn,
            ticker="CAT",
            source_id="sec_companyfacts",
            model_family="machinery",
            asof=date(2026, 4, 30),
        )
        assert [(row["source_id"], row["taxonomy"], row["source_priority"]) for row in rows] == [
            ("sec_companyfacts", "issuer-ir", 240)
        ]

        replace_document_candidates_and_facts(
            conn,
            ticker="CAT",
            cik="0000018230",
            source_id="sec_companyfacts",
            model_family="machinery",
            filing={
                "accession_number": "SEC-ONE",
                "form_type": "10-Q",
                "filing_date": "2026-05-01",
                "accepted_at": "2026-05-01T16:00:00Z",
                "fiscal_year": 2026,
                "fiscal_period": "Q1",
            },
            document_name="q1-10q.htm",
            candidates=[replace(ir_candidate, value=1_250_000_000.0, confidence=0.85)],
            now=now,
        )
        refresh_canonical_facts(
            conn,
            source_id="sec_companyfacts",
            model_family="machinery",
            tickers=["CAT"],
            asof=date(2026, 5, 2),
            supplemental_source_ids=("machinery_issuer_ir",),
        )
        rows = load_canonical_rows(
            conn,
            ticker="CAT",
            source_id="sec_companyfacts",
            model_family="machinery",
            asof=date(2026, 5, 2),
        )
        selected = select_fact(rows, "reported_backlog", prefer_annual=False)
        assert selected is not None
        assert selected["value"] == 1_250_000_000.0
        assert selected["form_type"] == "10-Q"

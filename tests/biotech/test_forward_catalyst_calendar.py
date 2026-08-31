from __future__ import annotations

import csv
import importlib.util
import json
from datetime import date
from pathlib import Path

from biotech_index.core.db import connect, init_db


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "biotech_index" / "scripts" / "09_build_forward_catalyst_calendar.py"
SPEC = importlib.util.spec_from_file_location("build_forward_catalyst_calendar", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
calendar = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(calendar)


def test_calendar_publication_writes_hash_sealed_same_day_snapshot(tmp_path: Path) -> None:
    root_path = tmp_path / "forward_catalyst_calendar.csv"
    published = calendar.publish_calendar_outputs(
        root_path,
        [{"ticker": "TEST", "event_type": "pdufa_date", "event_date": "2026-08-29"}],
        {"asof_date": "2026-08-28", "event_count": 1},
        asof_date=date(2026, 8, 28),
        publish_dated_snapshot=True,
    )

    dated_path = tmp_path / "20260828" / root_path.name
    assert [item[0] for item in published] == [root_path, dated_path]
    assert dated_path.read_bytes() == root_path.read_bytes()
    for csv_path, manifest_path in published:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["schema_version"] == 2
        assert manifest["output_csv"] == str(csv_path)
        assert manifest["output_sha256"] == calendar.file_sha256(csv_path)


def test_calendar_output_override_does_not_publish_production_snapshot(tmp_path: Path) -> None:
    override_path = tmp_path / "smoke" / "calendar.csv"
    published = calendar.publish_calendar_outputs(
        override_path,
        [],
        {"asof_date": "2026-08-28", "event_count": 0},
        asof_date=date(2026, 8, 28),
        publish_dated_snapshot=False,
    )

    assert [item[0] for item in published] == [override_path]
    assert not (override_path.parent / "20260828").exists()


def insert_company(conn, ticker: str, company_name: str) -> int:
    now = "2026-06-01T00:00:00Z"
    cursor = conn.execute(
        """
        INSERT INTO companies(ticker, company_name, universe_status, first_seen_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (ticker, company_name, "active", now, now),
    )
    return int(cursor.lastrowid)


def insert_sec_event(
    conn,
    *,
    company_id: int,
    accession: str,
    filing_date: str,
    event_type: str,
    event_date: str = "",
    event_value: str = "",
    confidence: float = 0.0,
) -> None:
    now = "2026-06-01T00:00:00Z"
    conn.execute(
        """
        INSERT INTO sec_filings(accession_nodash, company_id, form, filing_date, archive_url, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (accession, company_id, "8-K", filing_date, f"https://example.test/{accession}", now, now),
    )
    conn.execute(
        """
        INSERT INTO sec_events(
            company_id, accession_nodash, filing_date, form, event_type, event_date,
            event_value, polarity, confidence, extracted_text, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            company_id,
            accession,
            filing_date,
            "8-K",
            event_type,
            event_date,
            event_value,
            "positive",
            confidence,
            "test event",
            now,
            now,
        ),
    )


def insert_trial_link(
    conn,
    *,
    company_id: int,
    nct_id: str,
    primary_completion_date: str,
    phase_text: str = "Phase 2",
    overall_status: str = "RECRUITING",
    match_role: str = "lead_sponsor",
    confidence: float = 0.90,
) -> None:
    now = "2026-06-01T00:00:00Z"
    conn.execute(
        """
        INSERT INTO trials(
            nct_id, brief_title, study_type, phase_text, overall_status,
            lead_sponsor, last_update_post_date, has_results, raw_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            nct_id,
            f"{nct_id} title",
            "INTERVENTIONAL",
            phase_text,
            overall_status,
            "Sponsor",
            "2026-05-01",
            0,
            "{}",
            now,
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO trial_snapshot_daily(
            asof_date, nct_id, overall_status, phase_text, has_results,
            primary_completion_date, enrollment_count, raw_hash, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("2026-06-01", nct_id, overall_status, phase_text, 0, primary_completion_date, 100, "hash", now),
    )
    conn.execute(
        """
        INSERT INTO trial_company_links(
            nct_id, company_id, match_role, match_method, confidence, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (nct_id, company_id, match_role, "unit", confidence, now, now),
    )


def test_forward_catalyst_calendar_is_point_in_time_and_forward_only(tmp_path) -> None:
    db_path = tmp_path / "biotech.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        aaa = insert_company(conn, "AAA", "Alpha Bio")
        bbb = insert_company(conn, "BBB", "Beta Bio")
        ccc = insert_company(conn, "CCC", "Gamma Bio")
        ddd = insert_company(conn, "DDD", "Delta Bio")
        eee = insert_company(conn, "EEE", "Epsilon Bio")
        insert_sec_event(
            conn,
            company_id=aaa,
            accession="AAA1",
            filing_date="2026-05-01",
            event_type="pdufa_date",
            event_date="2026-06-30",
            confidence=0.20,
        )
        insert_sec_event(
            conn,
            company_id=aaa,
            accession="AAA2",
            filing_date="2026-05-10",
            event_type="pdufa_date",
            event_date="2026-07-15",
            confidence=0.95,
        )
        insert_sec_event(
            conn,
            company_id=bbb,
            accession="BBB1",
            filing_date="2026-06-02",
            event_type="pdufa_date",
            event_date="2026-06-15",
        )
        insert_sec_event(
            conn,
            company_id=ccc,
            accession="CCC1",
            filing_date="2026-05-15",
            event_type="pdufa_date",
            event_date="2027-06-15",
        )
        insert_sec_event(
            conn,
            company_id=ddd,
            accession="DDD1",
            filing_date="2026-05-20",
            event_type="nda_bla_accepted",
            event_value="FDA action expected 2026-07-20",
        )
        insert_sec_event(
            conn,
            company_id=eee,
            accession="EEE1",
            filing_date="2026-05-25",
            event_type="pdufa_date",
            event_date="November 14, 2026",
        )

        rows = calendar.load_forward_events(
            conn,
            asof_date=calendar.parse_date("2026-06-01"),
            lookahead_days=180,
            ticker_filter={"AAA", "BBB", "CCC", "DDD", "EEE"},
        )

    keys = [(row["ticker"], row["accession_nodash"], row["event_date"]) for row in rows]
    assert keys == [
        ("AAA", "AAA1", "2026-06-30"),
        ("AAA", "AAA2", "2026-07-15"),
        ("DDD", "DDD1", "2026-07-20"),
        ("EEE", "EEE1", "2026-11-14"),
    ]
    assert rows[0]["days_until"] == 29
    assert rows[0]["confidence"] == 0.85
    assert rows[2]["confidence"] == 0.78
    assert rows[3]["days_until"] == 166


def test_manual_overrides_and_ctgov_sources_expand_forward_calendar(tmp_path) -> None:
    db_path = tmp_path / "biotech.sqlite"
    override_path = tmp_path / "forward_catalyst_overrides.csv"
    with override_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "ticker",
                "event_date",
                "event_type",
                "confidence",
                "source_name",
                "source_url",
                "notes",
                "active",
                "asof_start",
                "asof_end",
                "nct_id",
                "trial_phase",
                "overall_status",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "ticker": "MAN",
                "event_date": "2026-08-15",
                "event_type": "manual_phase2_topline",
                "confidence": "0.82",
                "source_name": "unit override",
                "source_url": "https://example.test/manual",
                "notes": "known readout",
                "active": "true",
                "asof_start": "2026-01-01",
                "asof_end": "",
                "nct_id": "NCTMANUAL",
                "trial_phase": "Phase 2",
                "overall_status": "RECRUITING",
            }
        )
        writer.writerow(
            {
                "ticker": "OLD",
                "event_date": "2026-08-15",
                "event_type": "manual_phase2_topline",
                "active": "false",
            }
        )

    with connect(db_path) as conn:
        init_db(conn)
        man = insert_company(conn, "MAN", "Manual Bio")
        ctg = insert_company(conn, "CTG", "CTGov Bio")
        insert_trial_link(
            conn,
            company_id=ctg,
            nct_id="NCT00000001",
            primary_completion_date="2026-09-30",
            phase_text="Phase 3",
            overall_status="RECRUITING",
        )
        companies = calendar.company_lookup(conn)
        manual_rows = calendar.load_manual_overrides(
            override_path,
            asof_date=calendar.parse_date("2026-06-01"),
            lookahead_days=365,
            ticker_filter={"MAN", "OLD", "CTG"},
            companies_by_ticker=companies,
            default_confidence=0.80,
        )
        ctgov_rows = calendar.load_ctgov_forward_events(
            conn,
            asof_date=calendar.parse_date("2026-06-01"),
            lookahead_days=365,
            ticker_filter={"MAN", "CTG"},
            settings={"enabled": True, "phase3_confidence": 0.55, "min_link_confidence": 0.60},
        )

    assert man > 0
    assert manual_rows == [
        {
            "ticker": "MAN",
            "company_name": "Manual Bio",
            "company_id": man,
            "accession_nodash": "",
            "filing_date": "",
            "form": "",
            "event_type": "manual_phase2_topline",
            "event_date": "2026-08-15",
            "days_until": 75,
            "event_value": "known readout",
            "polarity": "positive",
            "confidence": 0.82,
            "source": "manual_override",
            "source_name": "unit override",
            "source_url": "https://example.test/manual",
            "nct_id": "NCTMANUAL",
            "trial_phase": "Phase 2",
            "overall_status": "RECRUITING",
            "document_url": "https://example.test/manual",
            "extracted_text": "",
            "notes": "known readout",
        }
    ]
    assert ctgov_rows[0]["ticker"] == "CTG"
    assert ctgov_rows[0]["company_id"] == ctg
    assert ctgov_rows[0]["event_type"] == "ctgov_primary_completion_phase3"
    assert ctgov_rows[0]["source"] == "ctgov_primary_completion"
    assert ctgov_rows[0]["confidence"] == 0.495
    assert ctgov_rows[0]["source_url"] == "https://clinicaltrials.gov/study/NCT00000001"

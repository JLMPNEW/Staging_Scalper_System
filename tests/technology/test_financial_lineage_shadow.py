from __future__ import annotations

import csv
import hashlib
import sqlite3
from pathlib import Path

from technology.core.financial_lineage_shadow import build_financial_lineage_shadow


ASOF = "2026-08-14"


def _database(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE fact_sec_filing (
            ticker TEXT, cik TEXT, accession_number TEXT, form_type TEXT,
            filing_date TEXT, acceptance_datetime TEXT, report_date TEXT,
            primary_document TEXT
        );
        CREATE TABLE fact_financial_statement_canonical (
            ticker TEXT, canonical_metric TEXT, accession_number TEXT,
            filing_date TEXT
        );
        CREATE TABLE feature_financial_statement (
            ticker TEXT, model_family TEXT, asof_date TEXT
        );
        CREATE TABLE feature_scoring_input (
            ticker TEXT, model_family TEXT, asof_date TEXT,
            financial_feature_asof_date TEXT,
            financial_source_accession TEXT,
            financial_source_fiscal_period_end TEXT,
            financial_source_feature_updated_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE raw_api_responses (
            endpoint TEXT, query_params_json TEXT, request_time_utc TEXT,
            response_status INTEGER, asof_date TEXT
        );
        CREATE TABLE sec_parser_document_catalog (
            accession_number TEXT, source_path TEXT,
            is_full_submission INTEGER, is_primary INTEGER, file_size INTEGER
        );
        """
    )
    return conn


def _rank_table(path: Path, *, gap_is_candidate: bool) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "ticker",
                "asof_date",
                "portfolio_candidate_gate",
                "rank_ready_flag",
                "final_score",
            ),
        )
        writer.writeheader()
        writer.writerows(
            [
                {
                    "ticker": "SAFE",
                    "asof_date": ASOF,
                    "portfolio_candidate_gate": "1",
                    "rank_ready_flag": "1",
                    "final_score": "70",
                },
                {
                    "ticker": "GAP",
                    "asof_date": ASOF,
                    "portfolio_candidate_gate": "1" if gap_is_candidate else "0",
                    "rank_ready_flag": "1" if gap_is_candidate else "0",
                    "final_score": "40",
                },
            ]
        )


def _seed_safe_filing(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO fact_sec_filing VALUES (?,?,?,?,?,?,?,?)",
        (
            "SAFE",
            "1",
            "safe-2026",
            "10-Q",
            "2026-08-01",
            "2026-08-01T09:30:00",
            "2026-06-30",
            "safe.htm",
        ),
    )
    conn.executemany(
        "INSERT INTO fact_financial_statement_canonical VALUES (?,?,?,?)",
        [("SAFE", metric, "safe-2026", "2026-08-01") for metric in ("revenue", "assets", "operating_income")],
    )
    conn.executemany(
        "INSERT INTO feature_financial_statement VALUES (?,?,?)",
        [(ticker, "semiconductors", ASOF) for ticker in ("SAFE", "GAP")],
    )
    conn.execute(
        "INSERT INTO feature_scoring_input VALUES (?,?,?,?,?,?,?,?)",
        (
            "SAFE",
            "semiconductors",
            ASOF,
            "2026-08-01",
            "safe-2026",
            "2026-06-30",
            "2026-08-14T12:01:00Z",
            "2026-08-14T12:02:00Z",
        ),
    )
    conn.execute(
        "INSERT INTO raw_api_responses VALUES (?,?,?,?,?)",
        (
            "https://data.sec.gov/submissions/CIK0000000001.json",
            '{"payload_source":"live_network","response_kind":"root_submissions"}',
            "2026-08-14T12:00:00Z",
            200,
            ASOF,
        ),
    )
    conn.commit()


def test_candidate_only_shadow_passes_noncandidate_gap_without_mutating_rank(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "technology.sqlite"
    conn = _database(db_path)
    _seed_safe_filing(conn)
    conn.close()
    rank_path = tmp_path / "rank.csv"
    _rank_table(rank_path, gap_is_candidate=False)
    before = hashlib.sha256(rank_path.read_bytes()).hexdigest()

    manifest = build_financial_lineage_shadow(
        db_path=db_path,
        rank_table_path=rank_path,
        output_dir=tmp_path / "shadow",
        policy_context="production",
    )

    assert manifest["acceptance"] == "PASS"
    assert manifest["policy_mode"] == "candidate_only"
    assert manifest["candidate_count"] == 1
    assert manifest["candidate_incorporated_count"] == 1
    assert manifest["unresolved_count"] == 1
    assert manifest["production_rank_table_modified"] is False
    assert hashlib.sha256(rank_path.read_bytes()).hexdigest() == before


def test_candidate_only_shadow_fails_closed_for_unresolved_candidate(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "technology.sqlite"
    conn = _database(db_path)
    _seed_safe_filing(conn)
    conn.close()
    rank_path = tmp_path / "rank.csv"
    _rank_table(rank_path, gap_is_candidate=True)

    manifest = build_financial_lineage_shadow(
        db_path=db_path,
        rank_table_path=rank_path,
        output_dir=tmp_path / "shadow",
        policy_context="production",
    )

    assert manifest["acceptance"] == "FAIL"
    assert any("GAP:candidate_has_unresolved_financial_lineage" in issue for issue in manifest["blocking_issues"])

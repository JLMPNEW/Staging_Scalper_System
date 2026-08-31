from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from technology.core.financial_lineage_shadow import build_financial_lineage_shadow


ASOF = "2026-08-14"


def test_software_shadow_uses_family_policy_and_artifact_names(tmp_path: Path) -> None:
    db_path = tmp_path / "technology.sqlite"
    conn = sqlite3.connect(db_path)
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
    conn.execute(
        "INSERT INTO fact_sec_filing VALUES (?,?,?,?,?,?,?,?)",
        (
            "SAFE", "1", "safe-2026", "10-Q", "2026-08-03",
            "2026-08-03T09:30:00", "2026-06-30", "safe.htm",
        ),
    )
    conn.executemany(
        "INSERT INTO fact_financial_statement_canonical VALUES (?,?,?,?)",
        [
            ("SAFE", metric, "safe-2026", "2026-08-03")
            for metric in ("revenue", "assets", "operating_income")
        ],
    )
    conn.execute(
        "INSERT INTO feature_financial_statement VALUES (?,?,?)",
        ("SAFE", "software_infrastructure", ASOF),
    )
    conn.execute(
        "INSERT INTO feature_scoring_input VALUES (?,?,?,?,?,?,?,?)",
        (
            "SAFE",
            "software_infrastructure",
            ASOF,
            "2026-08-03",
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
    conn.close()

    rank_path = tmp_path / "software_rank.csv"
    with rank_path.open("w", encoding="utf-8", newline="") as handle:
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
        writer.writerow(
            {
                "ticker": "SAFE",
                "asof_date": ASOF,
                "portfolio_candidate_gate": "1",
                "rank_ready_flag": "1",
                "final_score": "70",
            }
        )
    output_dir = tmp_path / "shadow"

    manifest = build_financial_lineage_shadow(
        db_path=db_path,
        rank_table_path=rank_path,
        output_dir=output_dir,
        model_family="software_infrastructure",
        expected_asof=ASOF,
        policy_context="production",
    )

    assert manifest["acceptance"] == "PASS"
    assert manifest["policy_context"] == "production"
    assert manifest["policy_mode"] == "candidate_only"
    assert manifest["production_valid_from"] == ASOF
    assert manifest["model_family"] == "software_infrastructure"
    assert (output_dir / "software_infrastructure_financial_lineage_shadow.csv").is_file()
    assert (output_dir / "software_infrastructure_financial_lineage_shadow.json").is_file()

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE raw_api_responses
            SET asof_date = '2026-08-15',
                request_time_utc = '2026-08-15T12:00:00Z'
            """
        )

    strict_manifest = build_financial_lineage_shadow(
        db_path=db_path,
        rank_table_path=rank_path,
        output_dir=tmp_path / "strict_replay",
        model_family="software_infrastructure",
        expected_asof=ASOF,
        policy_context="production",
    )
    assert strict_manifest["acceptance"] == "FAIL"

    replay_dir = tmp_path / "bounded_replay"
    replay_manifest = build_financial_lineage_shadow(
        db_path=db_path,
        rank_table_path=rank_path,
        output_dir=replay_dir,
        model_family="software_infrastructure",
        expected_asof=ASOF,
        policy_context="production",
        retrospective_source_discovery_max_days=1,
    )
    assert replay_manifest["acceptance"] == "PASS"
    assert replay_manifest["retrospective_source_discovery_count"] == 1
    with (
        replay_dir / "software_infrastructure_financial_lineage_shadow.csv"
    ).open(encoding="utf-8", newline="") as handle:
        replay_row = next(csv.DictReader(handle))
    assert (
        "retrospective_sec_submissions_discovery_confirmed:capture_asof=2026-08-15"
        in replay_row["financial_lineage_reason"]
    )

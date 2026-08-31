from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "technology/scripts/08_build_technology_financial_features.py"
SPEC = importlib.util.spec_from_file_location("technology_financial_cleanup_test", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_ticker_rebuild_cleanup_is_scoped_to_family_and_source() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE feature_financial_statement(
            ticker TEXT, source_id TEXT, model_family TEXT, fiscal_period_end TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO feature_financial_statement VALUES (?,?,?,?)",
        [
            ("POET", "sec", "semiconductors", "2026-03-31"),
            ("POET", "sec", "technology_hardware", "2026-03-31"),
            ("OTHER", "sec", "semiconductors", "2026-03-31"),
        ],
    )

    MODULE.delete_ticker_feature_rows(
        conn,
        ticker="POET",
        source_id="sec",
        model_family="semiconductors",
    )

    assert conn.execute(
        "SELECT ticker, model_family FROM feature_financial_statement ORDER BY 1, 2"
    ).fetchall() == [
        ("OTHER", "semiconductors"),
        ("POET", "technology_hardware"),
    ]


def test_filing_acceptance_map_preserves_first_public_timestamp() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE fact_sec_filing(
            ticker TEXT, source_id TEXT, accession_number TEXT,
            filing_date TEXT, accepted_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO fact_sec_filing VALUES (?,?,?,?,?)",
        (
            "CRM",
            "sec_submissions",
            "0001108524-26-000190",
            "2026-08-27",
            "2026-08-26T22:54:46.000Z",
        ),
    )
    conn.execute(
        "INSERT INTO fact_sec_filing VALUES (?,?,?,?,?)",
        (
            "CRM",
            "sec_companyfacts",
            "0001108524-26-000190",
            "2026-08-27",
            "",
        ),
    )

    acceptance = MODULE.load_filing_acceptance_by_accession(
        conn,
        ticker="CRM",
    )

    assert acceptance == {
        "0001108524-26-000190": "2026-08-26T22:54:46.000Z"
    }

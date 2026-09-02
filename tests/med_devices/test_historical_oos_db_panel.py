from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "med_devices" / "scripts" / "75_validate_med_device_historical_snapshot_oos.py"


def load_validator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("med_historical_oos_db_panel_test", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def create_score_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE med_device_daily_scores(
            company_id INTEGER,
            asof_date TEXT,
            score_model_version TEXT,
            calibration_cohort TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE dim_company_model_taxonomy_history(
            asof_date TEXT,
            company_id INTEGER,
            calibration_cohort TEXT
        )
        """
    )


def test_db_panel_reconciliation_rejects_db_only_non_market_date() -> None:
    validator = load_validator()
    conn = sqlite3.connect(":memory:")
    create_score_tables(conn)
    conn.executemany(
        "INSERT INTO med_device_daily_scores VALUES (1, ?, 'v25', 'diagnostics_clinical_tests')",
        [("2024-06-21",), ("2024-06-23",), ("2024-06-24",)],
    )
    checks: list[dict[str, object]] = []

    validator.validate_db_panel_date_reconciliation(
        asofs=["2024-06-21", "2024-06-24"],
        db_conn=conn,
        db_path=None,
        checks=checks,
    )

    assert checks == [
        {
            "asof_date": "ALL",
            "artifact": "panel:med_device_daily_scores",
            "check_id": "db_score_asof_panel_reconciliation",
            "severity": "CRITICAL",
            "status": "FAIL",
            "observed": "file_dates=2 db_dates=3 unexpected=['2024-06-23'] missing=[]",
            "expected": "database score dates equal dated file-panel dates over the validated range",
            "details": (
                "DB-only dates can contaminate calibration even when every published file passes. "
                "Unexpected dates commonly indicate stale non-market snapshots or mixed model versions."
            ),
        }
    ]


def test_daily_db_reconciliation_rejects_score_model_version_mismatch() -> None:
    validator = load_validator()
    conn = sqlite3.connect(":memory:")
    create_score_tables(conn)
    conn.execute(
        "INSERT INTO med_device_daily_scores VALUES (1, '2024-06-21', 'v22', 'diagnostics_clinical_tests')"
    )
    conn.execute(
        "INSERT INTO dim_company_model_taxonomy_history "
        "VALUES ('2024-06-21', 1, 'diagnostics_clinical_tests')"
    )
    checks: list[dict[str, object]] = []

    validator.validate_daily_db_reconciliation(
        asof="2024-06-21",
        artifact="daily.csv",
        csv_row_count=1,
        csv_score_model_versions={"v25"},
        db_conn=conn,
        db_path=None,
        checks=checks,
    )

    version_check = next(
        row for row in checks if row["check_id"] == "daily_db_score_model_version_reconciliation"
    )
    assert version_check["status"] == "FAIL"
    assert version_check["observed"] == "csv=v25 db=v22"


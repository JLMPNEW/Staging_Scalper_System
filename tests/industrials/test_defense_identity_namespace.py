from __future__ import annotations

import importlib
import sqlite3


identity = importlib.import_module(
    "industrials.defense.scripts.02b_validate_defense_identity_reconciliation"
)


def test_delisted_validation_is_scoped_to_defense_model_family() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE dim_delisted_calibration_seed(
            ticker TEXT NOT NULL,
            model_family TEXT NOT NULL,
            company_name TEXT NOT NULL,
            calibration_cohort_id TEXT NOT NULL,
            cik TEXT,
            exit_year INTEGER,
            PRIMARY KEY(ticker, model_family)
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO dim_delisted_calibration_seed(
            ticker, model_family, company_name, calibration_cohort_id, cik, exit_year
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            ("EGL", "defense", "Engility", "primes_diversified_and_govtech_services", "0001544229", 2019),
            ("EGL", "transportation", "EGL Inc (Eagle Global Logistics)", "surface_freight_and_logistics", "0001001718", 2007),
        ],
    )

    errors, warnings = identity.validate_delisted_rows(
        conn,
        delisted_rows={
            "EGL": {
                "company": "Engility",
                "cohort": "primes_diversified_and_govtech_services",
                "cik": "0001544229",
                "exit_year": "2019",
            }
        },
        overrides=set(),
    )

    assert errors == []
    assert warnings == []

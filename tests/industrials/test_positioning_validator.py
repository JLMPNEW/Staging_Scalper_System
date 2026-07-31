from __future__ import annotations

import importlib
import sqlite3


validator = importlib.import_module("industrials.scripts.14_validate_industrials_sec_positioning_stages")
importer = importlib.import_module("industrials.scripts.09_import_industrials_positioning")


def test_universe_state_is_model_family_scoped_for_reused_ticker() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE dim_universe_membership(
            ticker TEXT,
            model_family TEXT,
            is_current_member INTEGER
        )
        """
    )
    conn.executemany(
        "INSERT INTO dim_universe_membership VALUES (?, ?, ?)",
        [
            ("FLY", "defense", 1),
            ("FLY", "transportation", 0),
            ("ACTIVE", "transportation", 1),
        ],
    )

    assert validator.load_active_universe(conn, "transportation") == [
        "ACTIVE"
    ]
    assert validator.load_inactive_universe(conn, "transportation") == [
        "FLY"
    ]
    assert importer.load_universe(
        conn,
        set(),
        model_family="transportation",
        include_historical=False,
    ) == ["ACTIVE"]
    assert importer.load_universe(
        conn,
        set(),
        model_family="transportation",
        include_historical=True,
    ) == ["ACTIVE", "FLY"]


def test_form4_status_distinguishes_coverage_nonapplicability_and_missing() -> None:
    common = {
        "ticker": "TEST",
        "form4_exempt_tickers": set(),
        "form4_exempt_reasons": {},
    }
    assert importer.form4_status_for_ticker(
        form4_rows=1,
        direct_rows=0,
        submission_rows=1,
        **common,
    ) == ("covered", "")
    assert importer.form4_status_for_ticker(
        form4_rows=0,
        direct_rows=0,
        submission_rows=2,
        **common,
    ) == (
        "covered_no_eligible_transactions",
        "FORM4_SUBMISSIONS_PRESENT_NO_ELIGIBLE_NONDERIVATIVE_TRANSACTIONS",
    )
    assert importer.form4_status_for_ticker(
        form4_rows=0,
        direct_rows=0,
        submission_rows=0,
        **common,
    ) == ("missing", "NO_FORM4_TRANSACTIONS")
    assert importer.form4_status_for_ticker(
        form4_rows=0,
        direct_rows=0,
        submission_rows=0,
        ticker="FPI",
        form4_exempt_tickers={"FPI"},
        form4_exempt_reasons={"FPI": "FOREIGN_PRIVATE_ISSUER"},
    ) == ("not_applicable", "FOREIGN_PRIVATE_ISSUER")
    assert validator.form4_source_is_covered(
        {"form4_status": "covered_no_eligible_transactions"}
    )
    assert not validator.form4_source_is_covered(
        {"form4_status": "not_applicable"}
    )


def test_required_feature_source_gate_detects_stale_values_and_honors_exemptions() -> None:
    features = {
        "FRESH": {"missing_fields": ""},
        "STALE": {"missing_fields": "borrow"},
        "MULTI": {"missing_fields": "13f;borrow"},
        "EXEMPT": {"missing_fields": "borrow"},
    }

    missing = validator.feature_tickers_missing_field(
        ["FRESH", "STALE", "MULTI", "EXEMPT", "NO_FEATURE"],
        features,
        field="borrow",
        exempt_tickers={"EXEMPT"},
    )

    assert missing == ["MULTI", "STALE"]


def test_resolve_feature_asof_honors_explicit_historical_replay_date() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE feature_positioning(asof_date TEXT, source_id TEXT, model_family TEXT)"
    )
    conn.executemany(
        "INSERT INTO feature_positioning VALUES (?, 'source', 'defense')",
        [("2026-07-13",), ("2026-07-17",)],
    )

    assert (
        validator.resolve_feature_asof(
            conn,
            requested_asof="2026-07-13",
            source_id="source",
            model_family="defense",
        )
        == "2026-07-13"
    )
    assert (
        validator.resolve_feature_asof(
            conn,
            requested_asof="",
            source_id="source",
            model_family="defense",
        )
        == "2026-07-17"
    )

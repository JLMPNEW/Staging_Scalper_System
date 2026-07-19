from __future__ import annotations

import importlib
import sqlite3


validator = importlib.import_module("industrials.scripts.14_validate_industrials_sec_positioning_stages")


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

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from types import ModuleType

from portfolio_layer.expectations_monitor.state_common import (
    append_raw_items,
    ensure_state_schema,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _guidance_db(path: Path, *, predecessor_midpoint: float) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE company_forward_guidance (
                guidance_id INTEGER PRIMARY KEY,
                guidance_unique_key TEXT NOT NULL,
                ticker TEXT NOT NULL,
                filing_date TEXT NOT NULL,
                metric TEXT NOT NULL,
                midpoint_value REAL,
                confidence REAL,
                accession_nodash TEXT,
                created_at TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO company_forward_guidance VALUES (?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    1,
                    "prior-key",
                    "TEST",
                    "2026-07-01",
                    "adjusted_eps",
                    predecessor_midpoint,
                    0.9,
                    "0001",
                    "2026-07-01T20:00:00+00:00",
                ),
                (
                    2,
                    "current-key",
                    "TEST",
                    "2026-08-01",
                    "adjusted_eps",
                    9.0,
                    0.9,
                    "0002",
                    "2026-08-01T20:00:00+00:00",
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_guidance_predecessor_correction_appends_a_revision(tmp_path: Path) -> None:
    sync = _load(
        PROJECT_ROOT
        / "portfolio_layer"
        / "expectations_monitor"
        / "53_sync_authoritative_events.py",
        "expectations_guidance_sync_test",
    )
    first_db = tmp_path / "first.sqlite"
    corrected_db = tmp_path / "corrected.sqlite"
    _guidance_db(first_db, predecessor_midpoint=10.0)
    _guidance_db(corrected_db, predecessor_midpoint=8.0)

    first = sync._guidance_items(
        first_db,
        tickers=["TEST"],
        as_of="2026-08-24",
        lookback_days=200,
        fetched_at="2026-08-24T22:00:00+00:00",
    )
    corrected = sync._guidance_items(
        corrected_db,
        tickers=["TEST"],
        as_of="2026-08-24",
        lookback_days=200,
        fetched_at="2026-08-29T22:00:00+00:00",
    )
    assert len(first) == len(corrected) == 1
    assert first[0]["source_uid"] != corrected[0]["source_uid"]
    assert corrected[0]["payload"]["predecessor_guidance_unique_key"] == "prior-key"

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        ensure_state_schema(conn)
        assert append_raw_items(conn, first) == (1, 0)
        assert append_raw_items(conn, corrected) == (1, 0)
        assert conn.execute("SELECT COUNT(*) FROM raw_items").fetchone()[0] == 2
    finally:
        conn.close()


def test_until_replaced_uses_latest_detected_revision() -> None:
    state = _load(
        PROJECT_ROOT
        / "portfolio_layer"
        / "expectations_monitor"
        / "56_build_expectations_state.py",
        "expectations_state_revision_test",
    )
    common = {
        "ticker": "TEST",
        "event_type": "guidance_cut",
        "event_date": "2026-08-01",
        "decay_mode": "until_replaced",
        "driver_tag": "adjusted_eps",
        "review_status": "auto",
    }
    old = {
        **common,
        "event_id": "zzzz-old-hash",
        "detected_at_utc": "2026-08-24T22:00:00+00:00",
    }
    corrected = {
        **common,
        "event_id": "aaaa-new-hash",
        "detected_at_utc": "2026-08-29T22:00:00+00:00",
    }

    assert state._active_events([corrected, old], as_of="2026-08-29") == [corrected]

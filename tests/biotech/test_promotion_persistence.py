from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from types import ModuleType

from biotech_index.core.db import DAILY_SCORES_OPTIONAL_COLUMNS, init_db


ROOT = Path(__file__).resolve().parents[2]
PROMOTION_FIELDS = {
    "biotech_selection_reliability_class",
    "biotech_active_sleeve_weight",
    "biotech_xbi_residual_weight",
    "biotech_promotion_contract_id",
    "biotech_promotion_contract_sha256",
}


def load_script(name: str) -> ModuleType:
    path = ROOT / "biotech_index" / "scripts" / name
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_promotion_fields_are_migrated_and_persisted() -> None:
    scorer = load_script("11_score_biotech_index.py")
    assert PROMOTION_FIELDS.issubset(DAILY_SCORES_OPTIONAL_COLUMNS)
    assert PROMOTION_FIELDS.issubset(scorer.PORTFOLIO_LAYER_CONTRACT_FIELDS)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    now = "2026-08-26T00:00:00+00:00"
    conn.execute(
        """
        INSERT INTO companies(
            ticker, company_name, universe_status, first_seen_at, updated_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        ("TEST", "Test Biotech", "active", now, now),
    )
    company_id = int(conn.execute("SELECT company_id FROM companies WHERE ticker = 'TEST'").fetchone()[0])
    row = {
        "asof_date": "2026-08-26",
        "company_id": company_id,
        "ticker": "TEST",
        "company_name": "Test Biotech",
        "opportunity_score": 55.0,
        "biotech_selection_reliability_class": "high",
        "biotech_active_sleeve_weight": 0.9,
        "biotech_xbi_residual_weight": 0.1,
        "biotech_promotion_contract_id": "candidate-v1",
        "biotech_promotion_contract_sha256": "a" * 64,
    }
    scorer.upsert_scores(conn, [row], "2026-08-26")
    stored = conn.execute(
        """
        SELECT biotech_selection_reliability_class,
               biotech_active_sleeve_weight,
               biotech_xbi_residual_weight,
               biotech_promotion_contract_id,
               biotech_promotion_contract_sha256
        FROM daily_scores
        WHERE asof_date = ? AND company_id = ?
        """,
        ("2026-08-26", company_id),
    ).fetchone()
    assert stored is not None
    assert stored["biotech_selection_reliability_class"] == "high"
    assert stored["biotech_active_sleeve_weight"] == 0.9
    assert stored["biotech_xbi_residual_weight"] == 0.1
    assert stored["biotech_promotion_contract_id"] == "candidate-v1"
    assert stored["biotech_promotion_contract_sha256"] == "a" * 64


def test_adaptive_selector_uses_native_score_not_zeroed_candidate_gate_score() -> None:
    scorer = load_script("11_score_biotech_index.py")
    row = {
        "portfolio_candidate_score": 0.0,
        "native_score_value": 55.0,
        "opportunity_score": 55.0,
    }
    assert scorer._candidate_policy_score(row) == 55.0


def test_reports_and_orchestrator_require_promotion_columns() -> None:
    publisher = load_script("12_publish_biotech_reports.py")
    orchestrator = load_script("24_run_biotech_refresh_pipeline.py")
    assert PROMOTION_FIELDS.issubset(publisher.TOP_SCORE_FIELDS)
    assert PROMOTION_FIELDS.issubset(orchestrator.BIOTECH_SCORE_CSV_PRESENT_COLUMNS)

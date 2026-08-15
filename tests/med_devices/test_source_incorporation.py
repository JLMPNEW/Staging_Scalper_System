from __future__ import annotations

import json
import runpy
import sqlite3
from pathlib import Path
from typing import Any

from med_devices.core.source_incorporation import (
    build_med_device_source_incorporation,
)
from orchestration_contracts.financial_lineage import (
    POLICY_CANDIDATE_ONLY,
    POLICY_DISABLED,
    policy_for_model_family,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ASOF = "2026-08-14"


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE dim_company (
            company_id INTEGER, ticker TEXT, cik TEXT
        );
        CREATE TABLE fact_sec_filing (
            accession_nodash TEXT, company_id INTEGER, form TEXT,
            filing_date TEXT, report_date TEXT
        );
        CREATE TABLE fact_financial_statement (
            accession_nodash TEXT, filed_date TEXT, period_end TEXT,
            revenue REAL, operating_income REAL, operating_cash_flow REAL,
            total_assets REAL, cash_and_investments REAL
        );
        CREATE TABLE raw_api_responses (
            source_id TEXT, endpoint TEXT, query_params_json TEXT,
            request_time_utc TEXT, response_status INTEGER, asof_date TEXT
        );
        CREATE TABLE ingestion_runs (
            ingestion_run_id INTEGER, status TEXT
        );
        CREATE TABLE ingestion_run_seals (
            ingestion_run_id INTEGER, source_id TEXT, asof_date TEXT,
            response_count INTEGER, sealed_at TEXT
        );
        CREATE TABLE feature_financial_valuation (
            company_id INTEGER, asof_date TEXT, payload_json TEXT, updated_at TEXT
        );
        CREATE TABLE feature_fda_product_risk (
            company_id INTEGER, asof_date TEXT, updated_at TEXT
        );
        CREATE TABLE feature_reimbursement (
            company_id INTEGER, asof_date TEXT, updated_at TEXT
        );
        CREATE TABLE feature_technical_entry (
            company_id INTEGER, asof_date TEXT, updated_at TEXT
        );
        CREATE TABLE med_device_daily_scores (
            company_id INTEGER, asof_date TEXT, updated_at TEXT
        );
        """
    )
    payload = json.dumps(
        {"selected_financial_accessions": ["000012345624000001"]}
    )
    conn.executescript(
        f"""
        INSERT INTO dim_company VALUES (1, 'MDX', '0000123456');
        INSERT INTO fact_sec_filing
        VALUES ('000012345624000001', 1, '10-Q', '2026-08-10', '2026-06-30');
        INSERT INTO fact_financial_statement
        VALUES ('000012345624000001', '2026-08-10', '2026-06-30',
                100.0, 10.0, 12.0, 400.0, 80.0);
        INSERT INTO raw_api_responses
        VALUES (
            'sec_submissions',
            'https://data.sec.gov/submissions/CIK0000123456.json',
            '{{"payload_source":"fetched","response_kind":"root_submissions"}}',
            '2026-08-14T10:00:00Z',
            200,
            '{ASOF}'
        );
        INSERT INTO ingestion_runs VALUES (7, 'success');
        INSERT INTO ingestion_run_seals
        VALUES (7, 'openfda_device', '{ASOF}', 12, '2026-08-14T10:00:30Z');
        INSERT INTO feature_financial_valuation
        VALUES (1, '{ASOF}', '{payload}', '2026-08-14T10:01:00Z');
        INSERT INTO feature_fda_product_risk
        VALUES (1, '{ASOF}', '2026-08-14T10:01:30Z');
        INSERT INTO feature_reimbursement
        VALUES (1, '{ASOF}', '2026-08-14T10:01:45Z');
        INSERT INTO feature_technical_entry
        VALUES (1, '{ASOF}', '2026-08-14T10:02:00Z');
        INSERT INTO med_device_daily_scores
        VALUES (1, '{ASOF}', '2026-08-14T10:03:00Z');
        """
    )
    return conn


def test_source_incorporation_proves_latest_inputs_and_fails_closed() -> None:
    conn = _connection()
    source_row = {
        "ticker": "MDX",
        "asof_date": ASOF,
        "portfolio_candidate_gate": "1",
        "portfolio_candidate_status": "eligible",
    }

    gated, evidence = build_med_device_source_incorporation(
        conn,
        asof=ASOF,
        score_rows=[source_row],
    )

    assert evidence[0]["financial_lineage_gate"] == "1"
    assert evidence[0]["incorporated_financial_accession"] == "000012345624000001"
    assert evidence[0]["fda_source_status"] == "SEALED_SUCCESS"
    assert gated[0]["portfolio_candidate_gate"] == "1"

    conn.execute(
        "UPDATE feature_financial_valuation SET payload_json = ?",
        (json.dumps({"selected_financial_accessions": []}),),
    )
    gated, evidence = build_med_device_source_incorporation(
        conn,
        asof=ASOF,
        score_rows=[source_row],
    )

    assert evidence[0]["financial_lineage_gate"] == "0"
    assert "latest_financial_accession_not_selected" in evidence[0]["financial_lineage_reason"]
    assert gated[0]["portfolio_candidate_gate"] == "0"
    assert gated[0]["portfolio_candidate_status"] == "data_review_required"


def test_daily_runner_refreshes_sec_live_then_enforces_source_gate() -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "med_devices" / "scripts" / "71_run_med_device_refresh_pipeline.py")
    )
    steps = namespace["build_steps"](
        asof=ASOF,
        force_refresh=False,
        skip_ibkr_borrow=True,
        skip_form4_runner=True,
        import_positioning_sources="",
        oos_score_valid=False,
    )
    by_id: dict[str, Any] = {step.step_id: step for step in steps}
    ids = [step.step_id for step in steps]

    assert by_id["05_sync_sec_fundamentals"].args[:3] == [
        "--asof",
        ASOF,
        "--refresh-submissions",
    ]
    assert ids.index("16_publish_review_pack") < ids.index(
        "81_build_source_incorporation"
    )
    assert ids.index("81_build_source_incorporation") < ids.index(
        "72_validate_production_outputs"
    )
    assert by_id["81_build_source_incorporation"].optional is False
    assert by_id["81_build_source_incorporation"].args == [
        "--asof",
        ASOF,
        "--policy-context",
        "production",
    ]


def test_med_device_lineage_policy_is_activation_bounded() -> None:
    policy = policy_for_model_family("med_devices")

    assert policy.enabled is True
    assert policy.mode_for("production") == POLICY_CANDIDATE_ONLY
    assert policy.production_valid_from == ASOF
    assert policy.mode_for_asof("production", "2026-08-13") == POLICY_DISABLED
    assert policy.mode_for_asof("production", ASOF) == POLICY_CANDIDATE_ONLY
    assert policy.require_score_incorporation is True
    assert policy.require_live_source_discovery is True

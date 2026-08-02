from __future__ import annotations

import csv
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from technology.core.db import init_db
from technology.core.dedicated_parser.db_contract import (
    ensure_technology_parser_schema,
)
from technology.core.measurement_diagnostics import (
    load_pit_measurement_features,
    validate_measurement_diagnostics,
    write_measurement_diagnostics,
)
from technology.software_infrastructure.dedicated_parser_adapter import (
    PROSE_ENABLED_METRICS,
    _candidate_status,
)
from technology.software_infrastructure.software_specialized_metrics import (
    PlausibilityThresholds,
    SpecializedFact,
    _latest,
    _visible_facts,
    adjudicated_facts,
    build_attrition_report,
    derive_signals,
    upsert_facts,
)


def connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def test_parser_channel_policy_and_intrinsic_gates_are_fail_closed() -> None:
    assert "selling_and_marketing_expense" not in PROSE_ENABLED_METRICS
    assert "customer_concentration_pct" not in PROSE_ENABLED_METRICS
    assert "remaining_performance_obligation" in PROSE_ENABLED_METRICS

    status, reason, _confidence = _candidate_status(
        metric_name="remaining_performance_obligation",
        context_before_value="RPO was $100 million",
        evidence_text="RPO was $100 million",
        period_end="2025-12-31",
        filing_date="2026-02-01",
        scope="consolidated",
        value=100_000_000,
    )
    assert status == "REVIEW_REQUIRED"
    assert reason == "prose_reconciliation_candidate_requires_xbrl_check"

    nrr_status, nrr_reason, _confidence = _candidate_status(
        metric_name="net_revenue_retention",
        context_before_value="NRR was 250 percent",
        evidence_text="NRR was 250 percent",
        period_end="2025-12-31",
        filing_date="2026-02-01",
        scope="consolidated",
        value=2.50,
    )
    assert nrr_status == "REJECTED_POLICY"
    assert nrr_reason == "nrr_outside_plausible_range"


def test_plausibility_gate_rejects_implausible_arr_to_revenue() -> None:
    with connection() as conn:
        conn.execute(
            """
            CREATE TABLE feature_financial_statement(
                model_family TEXT,
                ticker TEXT,
                asof_date TEXT,
                fiscal_period_end TEXT,
                financial_frequency TEXT,
                revenue REAL,
                revenue_ttm REAL,
                deferred_revenue REAL,
                remaining_performance_obligation REAL,
                accession_number TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO feature_financial_statement
            VALUES (
                'software_infrastructure', 'TEST', '2026-02-01',
                '2025-12-31', 'annual', 100.0, 100.0, 20.0, 80.0, 'a1'
            )
            """
        )
        decision = {
            "decision": "ACCEPTED",
            "effective_metric": "annual_recurring_revenue",
            "effective_value": 1_000.0,
            "ticker": "TEST",
            "cik": "0000000001",
            "effective_period_start": "",
            "effective_period_end": "2025-12-31",
            "accepted_at": "2026-02-01T17:00:00Z",
            "filing_date": "2026-02-01",
            "accession_number": "a1",
            "form_type": "10-K",
            "source_document": "test.htm",
            "source_document_sha256": "a" * 64,
            "source_evidence_key": "e1",
            "effective_unit": "USD",
            "decision_hash": "b" * 64,
            "period_kind": "annual",
            "definition_variant": "total_arr",
            "effective_scope": "consolidated",
            "calibration_eligible_flag": 1,
        }
        facts, reconciliation = adjudicated_facts(
            conn,
            policy={
                "release_id": "test",
                "policy_id": "test",
                "chain_root_sha256": "c" * 64,
                "decisions": [decision],
            },
            thresholds=PlausibilityThresholds(),
        )
    assert facts == []
    assert reconciliation[0]["gate_status"] == "REJECTED_PLAUSIBILITY"
    assert (
        reconciliation[0]["gate_reason"]
        == "arr_to_revenue_outside_plausible_band"
    )


def test_newest_visible_amendment_wins_without_definition_pooling() -> None:
    rows: list[dict[str, Any]] = [
        {
            "metric_name": "annual_recurring_revenue",
            "value": 100.0,
            "period_end": "2025-12-31",
            "availability_datetime": "2026-02-01T17:00:00Z",
            "provenance": {
                "calibration_eligible_flag": 1,
                "definition_variant": "total_arr",
                "period_kind": "annual",
            },
        },
        {
            "metric_name": "annual_recurring_revenue",
            "value": 120.0,
            "period_end": "2025-12-31",
            "availability_datetime": "2026-03-01T17:00:00Z",
            "provenance": {
                "calibration_eligible_flag": 1,
                "definition_variant": "total_arr",
                "period_kind": "annual",
            },
        },
        {
            "metric_name": "annual_recurring_revenue",
            "value": 90.0,
            "period_end": "2024-12-31",
            "availability_datetime": "2025-02-01T17:00:00Z",
            "provenance": {
                "calibration_eligible_flag": 1,
                "definition_variant": "segment_arr",
                "period_kind": "annual",
            },
        },
    ]
    visible = _visible_facts(rows, asof=date(2026, 3, 2))
    latest = _latest(
        visible,
        metric="annual_recurring_revenue",
        variant="total_arr",
    )
    assert latest is not None
    assert latest["value"] == 120.0
    assert len(
        [
            row
            for row in visible
            if row["period_end"] == "2025-12-31"
            and row["provenance"]["definition_variant"] == "total_arr"
        ]
    ) == 1


def test_measurement_loader_rejects_future_availability() -> None:
    with connection() as conn:
        conn.execute(
            """
            CREATE TABLE feature_technology_specialized_metric(
                model_family TEXT,
                ticker TEXT,
                asof_date TEXT,
                metric_name TEXT,
                metric_version TEXT,
                value REAL,
                availability_status TEXT,
                source_availability_datetime TEXT,
                review_required_flag INTEGER
            )
            """
        )
        conn.execute(
            """
            INSERT INTO feature_technology_specialized_metric
            VALUES (
                'software_infrastructure', 'TEST', '2024-01-02',
                'annual_recurring_revenue_yoy_growth',
                'software_specialized_measurement_v1', 0.20,
                'AVAILABLE_PIT', '2024-01-03T17:00:00Z', 0
            )
            """
        )
        with pytest.raises(RuntimeError, match="violates PIT availability"):
            load_pit_measurement_features(
                conn,
                model_family="software_infrastructure",
                metric_version="software_specialized_measurement_v1",
                metric_names={"annual_recurring_revenue_yoy_growth"},
                start_date=date(2024, 1, 1),
                end_date=date(2024, 1, 31),
            )


def test_measurement_diagnostics_remain_zero_weight(
    tmp_path: Path,
) -> None:
    panel: list[dict[str, Any]] = []
    dates = ("2024-01-02", "2024-02-01", "2024-03-01")
    for date_index, asof in enumerate(dates):
        for ticker_index in range(15):
            signal = ticker_index / 14 + date_index * 0.01
            panel.append(
                {
                    "asof_date": asof,
                    "ticker": f"T{ticker_index:02d}",
                    "gross_margin": (
                        0.30 + ((ticker_index * 7) % 15) / 100
                    ),
                    "annual_recurring_revenue_yoy_growth": signal,
                    "benchmark_trailing_252d": (
                        0.10 if date_index < 2 else -0.10
                    ),
                    "fwd_resid_21d": signal / 10,
                }
            )
    measurement_specs = [
        (
            "annual_recurring_revenue_yoy_growth",
            "annual_recurring_revenue_yoy_growth_score",
            True,
            None,
        )
    ]
    summary = write_measurement_diagnostics(
        output_dir=tmp_path,
        panel_rows=panel,
        measurement_specs=measurement_specs,
        all_specs=[
            ("gross_margin", "gross_margin_score", True, None),
            *measurement_specs,
        ],
        component_specs={"quality": [("gross_margin_score", 1.0)]},
        component_weights={"quality": 1.0},
        horizons=[21],
        step=21,
        min_cross_section=10,
        min_t_stat=1.5,
        metric_version="software_specialized_measurement_v1",
    )
    assert summary["production_weight"] == 0.0
    assert summary["production_scores_modified_flag"] == 0
    assert validate_measurement_diagnostics(
        tmp_path,
        expected_metric_version="software_specialized_measurement_v1",
    ) == []
    with (
        tmp_path / "measurement_incremental_ic.csv"
    ).open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows
    assert {row["production_weight"] for row in rows} == {"0.0"}


def _specialized_row(
    *,
    metric: str,
    value: float,
    period_end: str,
    availability: str,
    period_kind: str,
    variant: str,
) -> dict[str, Any]:
    return {
        "metric_name": metric,
        "value": value,
        "period_end": period_end,
        "availability_datetime": availability,
        "provenance": {
            "calibration_eligible_flag": 1,
            "definition_variant": variant,
            "period_kind": period_kind,
        },
    }


def test_subscription_mix_waits_for_financial_availability() -> None:
    visible = [
        _specialized_row(
            metric="subscription_revenue",
            value=50.0,
            period_end="2026-03-31",
            availability="2026-04-20T12:00:00Z",
            period_kind="quarterly",
            variant="total_subscription_revenue",
        )
    ]
    financial = [
        {
            "asof_date": "2026-05-01",
            "fiscal_period_end": "2026-03-31",
            "revenue": 100.0,
            "revenue_ttm": 400.0,
        }
    ]
    before = derive_signals(
        visible=visible,
        financial=financial,
        asof=date(2026, 4, 30),
    )["subscription_revenue_mix"]
    after = derive_signals(
        visible=visible,
        financial=financial,
        asof=date(2026, 5, 1),
    )["subscription_revenue_mix"]
    assert before.value is None
    assert before.status_reason.startswith("matching_period_revenue_missing")
    assert after.value == pytest.approx(0.5)
    assert after.availability_status == "AVAILABLE_PIT"
    assert after.source_availability_datetime == "2026-05-01T23:59:59Z"


def test_subscription_mix_never_pairs_quarterly_value_with_annual_revenue() -> None:
    visible = [
        _specialized_row(
            metric="subscription_revenue",
            value=90.0,
            period_end="2026-03-31",
            availability="2026-04-20T12:00:00Z",
            period_kind="quarterly",
            variant="total_subscription_revenue",
        ),
        _specialized_row(
            metric="subscription_revenue",
            value=320.0,
            period_end="2026-03-31",
            availability="2026-04-20T12:00:00Z",
            period_kind="annual",
            variant="total_subscription_revenue",
        ),
    ]
    financial = [
        {
            "asof_date": "2026-05-01",
            "fiscal_period_end": "2026-03-31",
            "revenue": 400.0,
            "revenue_ttm": 400.0,
        }
    ]
    result = derive_signals(
        visible=visible,
        financial=financial,
        asof=date(2026, 5, 1),
    )["subscription_revenue_mix"]
    assert result.value == pytest.approx(0.8)
    assert result.definition_version == "subscription_revenue_mix_v2"


def test_level_signal_becomes_stale_instead_of_carrying_forever() -> None:
    visible = [
        _specialized_row(
            metric="net_revenue_retention",
            value=1.20,
            period_end="2023-12-31",
            availability="2024-02-01T12:00:00Z",
            period_kind="annual",
            variant="dollar_based_net_retention",
        )
    ]
    result = derive_signals(
        visible=visible,
        financial=[],
        asof=date(2025, 6, 1),
    )["net_revenue_retention_level"]
    assert result.value is None
    assert result.availability_status == "STALE_PIT"
    assert "max_age_days=460" in result.status_reason


def test_attrition_report_names_censored_lower_bound_exclusion() -> None:
    decision = {
        "sequence": 1,
        "source_evidence_key": "crwd-nrr-lower-bound",
        "ticker": "CRWD",
        "source_metric": "net_revenue_retention",
        "effective_metric": "net_revenue_retention",
        "effective_period_end": "2022-01-31",
        "decision": "ACCEPTED",
        "definition_variant": "dollar_based_net_retention_lower_bound",
        "calibration_eligible_flag": 0,
    }
    rows = build_attrition_report(
        policy={"decisions": [decision]},
        reconciliation_rows=[
            {
                "source_evidence_key": "crwd-nrr-lower-bound",
                "materialized_flag": 0,
                "gate_status": "DIAGNOSTIC_SCOPE_ONLY",
                "gate_reason": "segment_or_noncomparable_definition",
            }
        ],
        panel_rows=[],
    )
    assert rows[0]["attrition_stage"] == "EXCLUDED_FROM_CALIBRATION"
    assert (
        rows[0]["attrition_reason"]
        == "censored_lower_bound_not_calibration_comparable"
    )

def test_arr_to_revenue_uses_matching_pit_ttm_revenue() -> None:
    visible = [
        _specialized_row(
            metric="annual_recurring_revenue",
            value=500.0,
            period_end="2026-03-31",
            availability="2026-04-20T12:00:00Z",
            period_kind="quarterly",
            variant="total_arr",
        )
    ]
    financial = [
        {
            "asof_date": "2026-05-01",
            "fiscal_period_end": "2026-03-31",
            "revenue": 125.0,
            "revenue_ttm": 400.0,
        }
    ]
    before = derive_signals(
        visible=visible,
        financial=financial,
        asof=date(2026, 4, 30),
    )["annual_recurring_revenue_to_revenue"]
    after = derive_signals(
        visible=visible,
        financial=financial,
        asof=date(2026, 5, 1),
    )["annual_recurring_revenue_to_revenue"]
    assert before.value is None
    assert before.status_reason == "matching_period_revenue_ttm_missing"
    assert after.value == pytest.approx(1.25)
    assert after.source_availability_datetime == "2026-05-01T23:59:59Z"
    assert after.availability_status == "AVAILABLE_PIT"

def test_fact_upsert_supersedes_prior_interpretation_of_same_evidence() -> None:
    def fact(period_end: str, value: float) -> SpecializedFact:
        return SpecializedFact(
            ticker="TEST",
            cik="0000000001",
            metric_name="annual_recurring_revenue",
            value=value,
            unit="USD",
            period_start="",
            period_end=period_end,
            availability_datetime="2026-05-01T20:00:00Z",
            filing_date="2026-05-01",
            accession_number="0000000001-26-000001",
            form_type="8-K",
            source_document="earnings.htm",
            source_document_sha256="a" * 64,
            evidence_key="same-evidence",
            confidence=1.0,
            status_reason="test",
            definition_version="arr_v1",
            provenance={
                "calibration_eligible_flag": 1,
                "definition_variant": "total_arr",
                "period_kind": "quarterly",
            },
        )

    with connection() as conn:
        init_db(conn)
        ensure_technology_parser_schema(conn)
        upsert_facts(conn, facts=[fact("2026-05-01", 500.0)])
        upsert_facts(conn, facts=[fact("2026-03-31", 500.0)])
        rows = conn.execute(
            """
            SELECT period_end, value
            FROM fact_technology_specialized_metric
            WHERE evidence_key = 'same-evidence'
            """
        ).fetchall()
    assert [(row["period_end"], row["value"]) for row in rows] == [
        ("2026-03-31", 500.0)
    ]

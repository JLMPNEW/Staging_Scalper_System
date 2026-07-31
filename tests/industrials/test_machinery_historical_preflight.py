from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from dedicated_parser.contracts import stable_hash
from industrials.machinery.dedicated_parser_adapter import get_registry
from industrials.machinery.historical_promotion_preflight import (
    HistoricalDepthThresholds,
    run_historical_promotion_preflight,
)
from industrials.machinery.scoring import AVAILABILITY_STATUS_FIELDS


def _create_preflight_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE sec_parser_production_promotion_run (
            promotion_id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            model_family TEXT NOT NULL,
            asof_date TEXT NOT NULL,
            source_id TEXT NOT NULL,
            status TEXT NOT NULL,
            candidate_count INTEGER NOT NULL,
            promoted_count INTEGER NOT NULL,
            blocked_count INTEGER NOT NULL,
            suppression_count INTEGER NOT NULL,
            metadata_json TEXT NOT NULL
        );
        CREATE TABLE sec_parser_production_evidence (
            promotion_id INTEGER NOT NULL,
            evidence_key TEXT NOT NULL,
            action TEXT NOT NULL
        );
        CREATE TABLE sec_parser_metric_evidence_shadow (
            evidence_key TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            period_start TEXT,
            period_end TEXT,
            accepted_at TEXT,
            filing_date TEXT
        );
        CREATE TABLE fact_sec_xbrl_fact_raw (
            raw_fact_id INTEGER PRIMARY KEY,
            fact_key TEXT NOT NULL
        );
        CREATE TABLE fact_sec_xbrl_fact (
            raw_fact_id INTEGER NOT NULL,
            source_id TEXT NOT NULL,
            canonical_metric TEXT NOT NULL
        );
        CREATE TABLE sec_parser_production_suppression (
            suppression_id INTEGER PRIMARY KEY,
            model_family TEXT NOT NULL,
            ticker TEXT NOT NULL,
            canonical_metric TEXT NOT NULL,
            period_start TEXT,
            period_end TEXT,
            valid_from TEXT,
            valid_to TEXT,
            evidence_key TEXT NOT NULL,
            policy_id TEXT,
            active INTEGER NOT NULL
        );
        CREATE TABLE sec_parser_production_metric_override (
            model_family TEXT NOT NULL,
            ticker TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            valid_from TEXT,
            valid_to TEXT,
            evidence_key TEXT NOT NULL,
            active INTEGER NOT NULL
        );
        """
    )


def _write_current_gate(path: Path) -> None:
    fields = [
        "metric",
        "category",
        "gate_mode",
        "minimum_count",
        "minimum_fraction",
        "status",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow(
            {
                "metric": "orders",
                "category": "orders_backlog_source",
                "gate_mode": "calibration",
                "minimum_count": 1,
                "minimum_fraction": 0.0,
                "status": "CALIBRATION_READY",
            }
        )


def _write_sidecars(
    dashboard_root: Path,
    *,
    statuses: dict[str, str],
) -> None:
    status_fields = sorted(AVAILABILITY_STATUS_FIELDS)
    fields = [
        "asof_date",
        "ticker",
        "calibration_cohort",
        "membership_status",
        "survivorship_corrected_panel_flag",
        *status_fields,
    ]
    for asof_date, orders_status in statuses.items():
        output_dir = dashboard_root / asof_date
        output_dir.mkdir(parents=True)
        row = {
            "asof_date": asof_date,
            "ticker": "TEST",
            "calibration_cohort": "test_cohort",
            "membership_status": "active",
            "survivorship_corrected_panel_flag": "1",
            **{
                field: "NOT_APPLICABLE"
                for field in status_fields
            },
            "orders_availability_status": orders_status,
        }
        path = (
            output_dir
            / "machinery_stage11_survivorship_calibration_panel.csv"
        )
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerow(row)


def _seed_promotion(
    conn: sqlite3.Connection,
    *,
    action: str,
    suppression: bool,
) -> None:
    evidence_key = "orders-evidence"
    conn.execute(
        """
        INSERT INTO sec_parser_production_promotion_run
        VALUES (
            1, 7, 'machinery', '2024-01-03',
            'dedicated_parser_production', 'COMPLETED',
            1, ?, ?, ?, '{"conflicting_evidence_count": 0}'
        )
        """,
        (
            int(action == "PROMOTED"),
            int(action != "PROMOTED"),
            int(suppression),
        ),
    )
    conn.execute(
        """
        INSERT INTO sec_parser_production_evidence
        VALUES (1, ?, ?)
        """,
        (evidence_key, action),
    )
    conn.execute(
        """
        INSERT INTO sec_parser_metric_evidence_shadow
        VALUES (
            ?, 'TEST', 'orders', '2023-10-01', '2023-12-31',
            '2024-01-03T12:00:00Z', '2024-01-03'
        )
        """,
        (evidence_key,),
    )
    if action == "PROMOTED":
        fact_key = stable_hash(
            {
                "source_id": "dedicated_parser_production",
                "evidence_key": evidence_key,
                "canonical_metric": "orders",
            }
        )
        conn.execute(
            "INSERT INTO fact_sec_xbrl_fact_raw VALUES (1, ?)",
            (fact_key,),
        )
        conn.execute(
            """
            INSERT INTO fact_sec_xbrl_fact
            VALUES (1, 'dedicated_parser_production', 'orders')
            """
        )
    if suppression:
        conn.execute(
            """
            INSERT INTO sec_parser_production_suppression
            VALUES (
                1, 'machinery', 'TEST', 'orders', '2023-10-01',
                '2023-12-31', '2024-01-03', NULL, ?,
                'test-policy', 1
            )
            """,
            (evidence_key,),
        )
    conn.commit()


def _run_preflight(
    tmp_path: Path,
    *,
    action: str,
    suppression: bool,
    statuses: dict[str, str],
    minimum_total_observations: int,
) -> dict[str, object]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _create_preflight_schema(conn)
    _seed_promotion(conn, action=action, suppression=suppression)
    current_coverage = tmp_path / "current.csv"
    _write_current_gate(current_coverage)
    dashboard_root = tmp_path / "dashboard"
    _write_sidecars(dashboard_root, statuses=statuses)
    summary_path = tmp_path / "historical.json"
    summary_path.write_text(
        json.dumps(
            {
                "acceptance": "PASS",
                "start_date": min(statuses),
                "end_date": max(statuses),
                "scheduled_date_count": len(statuses),
            }
        ),
        encoding="utf-8",
    )
    try:
        return run_historical_promotion_preflight(
            conn,
            promotion_ids=(1,),
            registry=get_registry(),
            source_id="dedicated_parser_production",
            current_coverage_csv=current_coverage,
            historical_summary_json=summary_path,
            dashboard_root=dashboard_root,
            thresholds=HistoricalDepthThresholds(
                minimum_total_observations=minimum_total_observations,
                minimum_qualified_dates=1,
                minimum_qualified_years=1,
                minimum_delisted_tickers=0,
            ),
        )
    finally:
        conn.close()


def test_preflight_limits_rebuild_to_affected_partitions(
    tmp_path: Path,
) -> None:
    result = _run_preflight(
        tmp_path,
        action="PROMOTED",
        suppression=False,
        statuses={
            "2024-01-02": "NOT_DISCLOSED",
            "2024-01-03": "REPORTED",
            "2024-01-04": "REPORTED",
        },
        minimum_total_observations=1,
    )
    summary = result["summary"]
    assert isinstance(summary, dict)
    assert summary["acceptance"] == "PASS"
    assert summary["decision"] == "GO_AFFECTED_PARTITIONS_ONLY"
    assert summary["affected_partition_count"] == 2
    assert summary["full_rebuild_required"] is False
    metric_rows = result["metric_rows"]
    assert isinstance(metric_rows, list)
    assert metric_rows[0]["historical_status"] == "PRODUCTION_CANDIDATE"


def test_preflight_suppression_worst_case_can_block_rebuild(
    tmp_path: Path,
) -> None:
    result = _run_preflight(
        tmp_path,
        action="BLOCKED",
        suppression=True,
        statuses={
            "2024-01-02": "REPORTED",
            "2024-01-03": "REPORTED",
            "2024-01-04": "REPORTED",
        },
        minimum_total_observations=2,
    )
    summary = result["summary"]
    assert isinstance(summary, dict)
    assert summary["acceptance"] == "FAIL"
    assert summary["decision"] == "BLOCK_REBUILD"
    metric_rows = result["metric_rows"]
    assert isinstance(metric_rows, list)
    assert metric_rows[0]["suppression_exposed_covered_count"] == 2
    assert metric_rows[0]["worst_case_covered_count"] == 1
    assert metric_rows[0]["historical_status"] == "DIAGNOSTIC_ONLY"

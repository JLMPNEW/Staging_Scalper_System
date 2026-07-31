from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from dedicated_parser.promotion import _conflicting_evidence_keys, promote_run
from dedicated_parser.storage import connect_database
from industrials.machinery.dedicated_parser_adapter import get_registry


def _create_source_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE fact_sec_filing (
            ticker TEXT, accession_number TEXT, form_type TEXT,
            filing_date TEXT, accepted_at TEXT, fiscal_year INTEGER,
            fiscal_period TEXT
        );
        CREATE TABLE fact_sec_xbrl_fact_raw (
            raw_fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
            fact_key TEXT NOT NULL UNIQUE,
            ticker TEXT NOT NULL, cik TEXT, source_id TEXT NOT NULL,
            accession_number TEXT, form_type TEXT, filing_date TEXT,
            accepted_at TEXT, fiscal_year INTEGER, fiscal_period TEXT,
            period_start TEXT, period_end TEXT, frame TEXT,
            taxonomy TEXT NOT NULL, concept_name TEXT NOT NULL, unit TEXT,
            raw_value REAL, decimals TEXT, source_detail TEXT,
            payload_json TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE fact_sec_xbrl_fact (
            fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
            raw_fact_id INTEGER, ticker TEXT NOT NULL, cik TEXT,
            source_id TEXT NOT NULL, accession_number TEXT, form_type TEXT,
            filing_date TEXT, accepted_at TEXT, fiscal_year INTEGER,
            fiscal_period TEXT, period_start TEXT, period_end TEXT,
            frame TEXT, taxonomy TEXT NOT NULL, concept_name TEXT NOT NULL,
            canonical_metric TEXT NOT NULL, financial_statement TEXT,
            period_type TEXT, unit TEXT, value REAL, sign_policy TEXT,
            source_priority INTEGER NOT NULL DEFAULT 100,
            source_detail TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(
                ticker, source_id, accession_number, taxonomy, concept_name,
                canonical_metric, unit, period_start, period_end, frame
            )
        );
        """
    )


def _seed_run(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        """
        INSERT INTO sec_parser_run(
            model_family, asof_date, parser_release, adapter_version,
            mode, worker_count, started_at, completed_at, status,
            planned_work_count, completed_work_count, failed_work_count
        )
        VALUES (
            'machinery', '2026-07-24', '0.4.0',
            'machinery_specialized_metrics_v3.0', 'shadow', 1,
            '2026-07-24T00:00:00Z', '2026-07-24T00:01:00Z',
            'COMPLETED', 1, 1, 0
        )
        """
    )
    if cursor.lastrowid is None:
        raise AssertionError("Failed to seed parser run")
    run_id = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO sec_parser_work_ledger(
            work_key, run_id, model_family, ticker, cik, accession_number,
            parser_release, adapter_version, requested_metrics_json,
            input_hashes_json, status
        )
        VALUES (
            'work', ?, 'machinery', 'TEST', '0000000001',
            '0000000001-26-000001', '0.4.0',
            'machinery_specialized_metrics_v3.0', '[]', '{}', 'COMPLETED'
        )
        """,
        (run_id,),
    )
    conn.execute(
        """
        INSERT INTO fact_sec_filing VALUES (
            'TEST', '0000000001-26-000001', '10-Q',
            '2026-04-30', '2026-04-30T12:00:00Z', 2026, 'Q1'
        )
        """
    )
    evidence_rows = (
        ("accepted", "consolidated", 100_000_000.0),
        ("segment", "segment", 200_000_000.0),
    )
    for evidence_key, scope, value in evidence_rows:
        conn.execute(
            """
            INSERT INTO sec_parser_metric_evidence_shadow(
                evidence_key, run_id, work_key, model_family,
                adapter_version, ticker, cik, accession_number, form_type,
                filing_date, accepted_at, report_date, metric_name,
                concept_name, candidate_value, unit, period_start,
                period_end, scope, confidence, candidate_status,
                status_reason, evidence_text, source_document,
                extraction_method, provenance_json, parser_release,
                created_at
            )
            VALUES (
                ?, ?, 'work', 'machinery',
                'machinery_specialized_metrics_v3.0', 'TEST',
                '0000000001', '0000000001-26-000001', '10-Q',
                '2026-04-30', '2026-04-30T12:00:00Z', '2026-03-31',
                'reported_backlog', 'ReportedBacklog', ?, 'USD', '',
                '2026-03-31', ?, 0.95, 'ACCEPTED', 'test', 'test',
                'filing.htm', 'test', '{}', '0.4.0',
                '2026-07-24T00:00:00Z'
            )
            """,
            (evidence_key, run_id, value, scope),
        )
        conn.execute(
            """
            INSERT INTO sec_parser_run_metric_evidence(
                run_id, evidence_key
            ) VALUES (?, ?)
            """,
            (run_id, evidence_key),
        )
    conn.commit()
    return run_id


def test_production_promotion_is_gated_and_idempotent(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "promotion.sqlite"
    with connect_database(db_path) as conn:
        _create_source_tables(conn)
        run_id = _seed_run(conn)
        summary = promote_run(
            conn,
            run_id=run_id,
            registry=get_registry(),
            source_id="dedicated_parser_production",
            min_confidence=0.90,
        )
        assert summary["promoted_count"] == 1
        assert summary["blocked_count"] == 1
        fact = conn.execute(
            """
            SELECT canonical_metric, value, source_priority, source_detail
            FROM fact_sec_xbrl_fact
            WHERE source_id = 'dedicated_parser_production'
            """
        ).fetchone()
        assert dict(fact) == {
            "canonical_metric": "reported_backlog",
            "value": 100_000_000.0,
            "source_priority": 175,
            "source_detail": (
                "reported_backlog:"
                "dedicated_parser_production_mapped"
            ),
        }
        payload = json.loads(
            conn.execute(
                """
                SELECT payload_json
                FROM fact_sec_xbrl_fact_raw
                WHERE source_id = 'dedicated_parser_production'
                """
            ).fetchone()["payload_json"]
        )
        assert payload["evidence_key"] == "accepted"

        promote_run(
            conn,
            run_id=run_id,
            registry=get_registry(),
            source_id="dedicated_parser_production",
            min_confidence=0.90,
        )
        count = conn.execute(
            """
            SELECT COUNT(*)
            FROM fact_sec_xbrl_fact
            WHERE source_id = 'dedicated_parser_production'
            """
        ).fetchone()[0]
        assert count == 1


def _conflict_row(
    *,
    evidence_key: str,
    accession_number: str,
    value: float,
    accepted_at: str,
) -> sqlite3.Row:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT
            ? AS evidence_key, 'TEST' AS ticker,
            ? AS accession_number, 'remaining_performance_obligation'
                AS metric_name,
            ? AS candidate_value, 0.98 AS confidence,
            'ACCEPTED' AS candidate_status, 'consolidated' AS scope,
            '' AS period_start, '2026-03-31' AS period_end,
            'USD' AS unit, ? AS accepted_at, '2026-04-30' AS filing_date
        """,
        (evidence_key, accession_number, value, accepted_at),
    ).fetchone()


def test_conflicts_are_scoped_to_one_filing_accession() -> None:
    same_accession = [
        _conflict_row(
            evidence_key="tagged",
            accession_number="0000000001-26-000001",
            value=147_016_000.0,
            accepted_at="2026-04-30T12:00:00Z",
        ),
        _conflict_row(
            evidence_key="timing",
            accession_number="0000000001-26-000001",
            value=138_685_000.0,
            accepted_at="2026-04-30T12:00:00Z",
        ),
    ]
    conflicts = _conflicting_evidence_keys(
        same_accession,
        registry=get_registry(),
        asof_date="2026-07-24",
        min_confidence=0.90,
    )
    assert conflicts == {"tagged", "timing"}

    amendment_revisions = [
        _conflict_row(
            evidence_key="original",
            accession_number="0000000001-24-000001",
            value=22_800_000_000.0,
            accepted_at="2024-05-07T12:00:00Z",
        ),
        _conflict_row(
            evidence_key="amended",
            accession_number="0000000001-24-000002",
            value=22_700_000_000.0,
            accepted_at="2024-12-04T12:00:00Z",
        ),
    ]
    assert not _conflicting_evidence_keys(
        amendment_revisions,
        registry=get_registry(),
        asof_date="2026-07-24",
        min_confidence=0.90,
    )


def test_accepted_review_policy_deactivates_prior_suppression(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "suppression.sqlite"
    with connect_database(db_path) as conn:
        _create_source_tables(conn)
        run_id = _seed_run(conn)
        conn.execute(
            """
            UPDATE sec_parser_metric_evidence_shadow
            SET provenance_json = ?
            WHERE evidence_key = 'accepted'
            """,
            (json.dumps({"review_policy": {"policy_id": "overturned"}}),),
        )
        conn.execute(
            """
            INSERT INTO sec_parser_production_suppression(
                model_family, ticker, canonical_metric, period_start,
                period_end, candidate_value, value_tolerance, unit,
                accession_number, evidence_key, policy_id, valid_from,
                active, created_at
            )
            VALUES (
                'machinery', 'TEST', 'reported_backlog', '',
                '2026-03-31', 100000000, 1, 'USD',
                '0000000001-26-000001', 'old-key', 'overturned',
                '2026-04-30', 1, '2026-07-24T00:00:00Z'
            )
            """
        )
        conn.commit()
        promote_run(
            conn,
            run_id=run_id,
            registry=get_registry(),
            source_id="dedicated_parser_production",
            min_confidence=0.90,
        )
        active = conn.execute(
            """
            SELECT active
            FROM sec_parser_production_suppression
            WHERE policy_id = 'overturned'
            """
        ).fetchone()[0]
        assert active == 0


@pytest.mark.parametrize(
    ("metric_name", "status_reason", "expected_metric"),
    (
        (
            "rpo_current",
            "timing_dimension_current_fraction_outside_valid_range",
            "rpo_current",
        ),
        (
            "orders",
            "revenue_contract_narrative_is_not_orders",
            "orders",
        ),
    ),
)
def test_deterministic_rejection_creates_cross_source_suppression(
    tmp_path: Path,
    metric_name: str,
    status_reason: str,
    expected_metric: str,
) -> None:
    db_path = tmp_path / "automatic-suppression.sqlite"
    with connect_database(db_path) as conn:
        _create_source_tables(conn)
        run_id = _seed_run(conn)
        conn.execute(
            """
            UPDATE sec_parser_metric_evidence_shadow
            SET metric_name = ?,
                concept_name = 'RemainingPerformanceObligationCurrent',
                scope = 'consolidated',
                candidate_status = 'REJECTED_POLICY',
                status_reason = ?
            WHERE evidence_key = 'segment'
            """,
            (metric_name, status_reason),
        )
        conn.commit()
        summary = promote_run(
            conn,
            run_id=run_id,
            registry=get_registry(),
            source_id="dedicated_parser_production",
            min_confidence=0.90,
        )
        assert summary["suppression_count"] == 1
        suppression = conn.execute(
            """
            SELECT canonical_metric, candidate_value, policy_id, active
            FROM sec_parser_production_suppression
            """
        ).fetchone()
        assert dict(suppression) == {
            "canonical_metric": expected_metric,
            "candidate_value": 200_000_000.0,
            "policy_id": f"automatic:{status_reason}",
            "active": 1,
        }


def test_non_twelve_month_timing_rejection_creates_suppression(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "timing-window-suppression.sqlite"
    with connect_database(db_path) as conn:
        _create_source_tables(conn)
        run_id = _seed_run(conn)
        conn.execute(
            """
            UPDATE sec_parser_metric_evidence_shadow
            SET metric_name = 'rpo_current',
                concept_name = 'RemainingPerformanceObligationCurrent',
                scope = 'consolidated',
                candidate_status = 'REJECTED_POLICY',
                status_reason =
                    'timing_dimension_current_bucket_not_twelve_months'
            WHERE evidence_key = 'segment'
            """
        )
        conn.commit()
        summary = promote_run(
            conn,
            run_id=run_id,
            registry=get_registry(),
            source_id="dedicated_parser_production",
            min_confidence=0.90,
        )
        assert summary["suppression_count"] == 1
        suppression = conn.execute(
            """
            SELECT policy_id, active
            FROM sec_parser_production_suppression
            """
        ).fetchone()
        assert dict(suppression) == {
            "policy_id": (
                "automatic:"
                "timing_dimension_current_bucket_not_twelve_months"
            ),
            "active": 1,
        }

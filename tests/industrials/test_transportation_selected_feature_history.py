from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from industrials.transportation.selected_feature_history import (
    Evidence,
    build_preflight_rows,
    choose_point_in_time_evidence,
    evidence_lineage,
    normalized_accepted_evidence,
    sha256,
    stable_json_sha256,
    verify_v2_snapshots,
)


def test_point_in_time_evidence_excludes_future_and_stale_values() -> None:
    evidence = [
        Evidence(
            ticker="ARCB",
            metric_id="operating_ratio",
            value=0.95,
            unit="ratio",
            period_start="",
            period_end="2020-12-31",
            availability_date="2021-02-26",
            source_record_id="old",
            accession_number="a",
            scope="consolidated",
            confidence=1.0,
        ),
        Evidence(
            ticker="ARCB",
            metric_id="operating_ratio",
            value=0.90,
            unit="ratio",
            period_start="",
            period_end="2021-12-31",
            availability_date="2022-02-25",
            source_record_id="new",
            accession_number="b",
            scope="consolidated",
            confidence=1.0,
        ),
    ]

    old = choose_point_in_time_evidence(
        evidence, asof_date="2022-01-31", max_staleness_days=550
    )
    new = choose_point_in_time_evidence(
        evidence, asof_date="2022-02-28", max_staleness_days=550
    )
    assert old is not None and old.source_record_id == "old"
    assert new is not None and new.source_record_id == "new"
    assert (
        choose_point_in_time_evidence(
            evidence, asof_date="2024-01-31", max_staleness_days=550
        )
        is None
    )


def test_accepted_evidence_deduplicates_scope_variants() -> None:
    rows = [
        {
            "ticker": "ARCB",
            "metric_name": "operating_ratio",
            "candidate_status": "ACCEPTED",
            "candidate_value": 0.95,
            "unit": "ratio",
            "period_start": "",
            "period_end": "2020-12-31",
            "accepted_at": "2021-02-26T21:45:13Z",
            "filing_date": "2021-02-26",
            "accession_number": "a",
            "evidence_key": "unknown",
            "scope": "unknown",
            "confidence": 1.0,
        },
        {
            "ticker": "ARCB",
            "metric_name": "operating_ratio",
            "candidate_status": "ACCEPTED",
            "candidate_value": 0.95,
            "unit": "ratio",
            "period_start": "",
            "period_end": "2020-12-31",
            "accepted_at": "2021-02-26T21:45:13Z",
            "filing_date": "2021-02-26",
            "accession_number": "a",
            "evidence_key": "consolidated",
            "scope": "consolidated",
            "confidence": 1.0,
        },
    ]

    accepted = normalized_accepted_evidence(rows)

    assert len(accepted[("ARCB", "operating_ratio")]) == 1
    assert (
        accepted[("ARCB", "operating_ratio")][0].source_record_id
        == "consolidated"
    )


def test_preflight_marks_all_dates_for_explicit_new_registry_state() -> None:
    scope = [
        {
            "ticker": "ARCB",
            "universe_role": "active",
            "calibration_cohort": "surface",
            "industry": "Trucking",
            "primary_archetype": "surface_trucking",
            "metric_id": "operating_ratio",
            "source_lane": "DP",
            "applicability_status": "APPLICABLE",
        }
    ]
    coverage = [
        {
            "ticker": "ARCB",
            "metric_id": "operating_ratio",
            "coverage_status": "COVERED_ACCEPTED",
        }
    ]
    disposition = [
        {
            "metric_id": "operating_ratio",
            "metric_disposition": "CALIBRATION_CANDIDATE",
            "calibration_candidate": "1",
        }
    ]

    rows = build_preflight_rows(
        scope_rows=scope,
        coverage_rows=coverage,
        disposition_rows=disposition,
        dates=["2020-01-31", "2020-02-28", "2020-03-31"],
        first_dates={
            ("ARCB", "operating_ratio"): {
                "first_evidence": "2020-02-15",
                "first_accepted": "2020-02-15",
            }
        },
    )

    assert rows[0]["first_affected_snapshot_date"] == "2020-02-28"
    assert rows[0]["affected_snapshot_count"] == 2


def test_frozen_snapshot_prefix_allows_later_snapshots_but_detects_prefix_changes(
    tmp_path: Path,
) -> None:
    first = tmp_path / "2026-07-22" / "financial_features.csv"
    later = tmp_path / "2026-07-30" / "financial_features.csv"
    first.parent.mkdir(parents=True)
    later.parent.mkdir(parents=True)
    first.write_text("ticker,value\nAAA,1\n", encoding="utf-8")
    later.write_text("ticker,value\nAAA,2\n", encoding="utf-8")
    validation = {
        "acceptance": "PASS",
        "panel_status": "FROZEN",
        "snapshot_sha256": {
            "2026-07-22": {"financial_features.csv": sha256(first)},
            "2026-07-30": {"financial_features.csv": sha256(later)},
        },
    }

    frozen = verify_v2_snapshots(
        historical_root=tmp_path,
        validation_manifest=validation,
        through_date="2026-07-22",
    )
    frozen_digest = stable_json_sha256(frozen)
    later.write_text("ticker,value\nAAA,3\n", encoding="utf-8")

    assert stable_json_sha256(
        verify_v2_snapshots(
            historical_root=tmp_path,
            validation_manifest=validation,
            through_date="2026-07-22",
        )
    ) == frozen_digest
    with pytest.raises(ValueError, match="frozen v2 hash changed"):
        verify_v2_snapshots(
            historical_root=tmp_path,
            validation_manifest=validation,
        )

    first.write_text("ticker,value\nAAA,9\n", encoding="utf-8")
    with pytest.raises(ValueError, match="frozen v2 hash changed"):
        verify_v2_snapshots(
            historical_root=tmp_path,
            validation_manifest=validation,
            through_date="2026-07-22",
        )


def _lineage_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE sec_parser_review_evaluation (
            evaluation_id INTEGER PRIMARY KEY,
            base_run_id INTEGER NOT NULL,
            model_family TEXT NOT NULL,
            status TEXT NOT NULL,
            evaluated_evidence_count INTEGER NOT NULL
        );
        CREATE TABLE sec_parser_review_evidence (
            evaluation_id INTEGER NOT NULL,
            evaluated_evidence_key TEXT NOT NULL,
            ticker TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            candidate_value REAL,
            unit TEXT,
            period_start TEXT,
            period_end TEXT,
            accepted_at TEXT,
            filing_date TEXT,
            accession_number TEXT,
            scope TEXT,
            confidence REAL,
            candidate_status TEXT
        );
        CREATE TABLE sec_parser_run_metric_evidence (
            run_id INTEGER NOT NULL,
            evidence_key TEXT NOT NULL
        );
        CREATE TABLE sec_parser_metric_evidence_shadow (
            evidence_key TEXT PRIMARY KEY,
            model_family TEXT NOT NULL,
            ticker TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            candidate_value REAL,
            unit TEXT,
            period_start TEXT,
            period_end TEXT,
            accepted_at TEXT,
            filing_date TEXT,
            accession_number TEXT,
            scope TEXT,
            confidence REAL,
            candidate_status TEXT
        );
        """
    )
    connection.executemany(
        """
        INSERT INTO sec_parser_review_evaluation
        VALUES (?, ?, 'transportation', 'COMPLETED', 1)
        """,
        [(3, 58), (4, 59)],
    )
    connection.executemany(
        """
        INSERT INTO sec_parser_review_evidence
        VALUES (?, ?, ?, ?, ?, 'ratio', '', '2024-12-31',
                '2025-02-01', '2025-01-31', ?, 'consolidated', 1.0, ?)
        """,
        [
            (3, "reviewed-58", "ARCB", "operating_ratio", 0.91, "a", "ACCEPTED"),
            (
                4,
                "reviewed-59",
                "AAL",
                "passenger_load_factor",
                0.84,
                "b",
                "REJECTED_POLICY",
            ),
        ],
    )
    return connection


def test_evidence_lineage_uses_every_review_evaluation_without_raw_fallback() -> None:
    connection = _lineage_connection()
    connection.execute(
        """
        INSERT INTO sec_parser_metric_evidence_shadow
        VALUES ('raw-59', 'transportation', 'AAL', 'passenger_load_factor',
                0.99, 'ratio', '', '2024-12-31', '2025-02-01',
                '2025-01-31', 'raw', 'consolidated', 1.0, 'ACCEPTED')
        """
    )
    connection.execute(
        "INSERT INTO sec_parser_run_metric_evidence VALUES (59, 'raw-59')"
    )

    rows = evidence_lineage(
        connection=connection,
        evaluation_ids=[3, 4],
        supplemental_run_ids=[],
    )

    assert [row["evidence_key"] for row in rows] == [
        "reviewed-58",
        "reviewed-59",
    ]
    assert [row["candidate_status"] for row in rows] == [
        "ACCEPTED",
        "REJECTED_POLICY",
    ]


def test_evidence_lineage_rejects_reviewed_run_as_raw_supplement() -> None:
    connection = _lineage_connection()

    with pytest.raises(ValueError, match="cannot be loaded again"):
        evidence_lineage(
            connection=connection,
            evaluation_ids=[3, 4],
            supplemental_run_ids=[59],
        )

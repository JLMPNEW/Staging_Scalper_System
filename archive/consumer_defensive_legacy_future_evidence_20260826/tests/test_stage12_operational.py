from __future__ import annotations

from copy import deepcopy
import csv
import hashlib
import json

import pytest

from consumer_defensive.core.stage10_publishing import FINAL_RANK_REQUIRED_FIELDS
from consumer_defensive.core.stage12_operational import (
    LOCK_FIELDS,
    build_operational_rows,
    publish_operational_snapshot,
    validate_operational_snapshot,
)


def _row(ticker: str, cohort: str, rank: int) -> dict[str, object]:
    row: dict[str, object] = {field: "" for field in FINAL_RANK_REQUIRED_FIELDS}
    row.update(
        {
            "asof_date": "2026-09-30",
            "ticker": ticker,
            "company_name": ticker,
            "sector": "Consumer Staples",
            "industry": cohort,
            "industry_aggregate": "Consumer Staples",
            "calibration_cohort": cohort,
            "final_score": 60.0 + rank,
            "final_rank": rank,
            "rank_ready_flag": 1,
            "model_status": "complete",
            "promotion_state": "shadow_monitor",
            "score_confidence": 0.8,
            "score_model_version": "consumer_model_v1",
            "model_version": "consumer_model_v1",
            "scoring_contract_version": "score_contract_v1",
            "portfolio_candidate_gate": 0,
            "portfolio_candidate_status": "shadow_only",
            "calibration_eligible_flag": 1,
            "research_calibration_input_eligible_flag": 0,
            "calibration_sample_role": "excluded",
            "stage11_calibration_input_eligible_flag": 0,
            "survivorship_corrected_panel_flag": 0,
            "oos_score_valid_flag": 0,
        }
    )
    return row


def _lock(scope: str) -> dict[str, object]:
    return {
        "scope_id": scope,
        "effective_from": "2026-09-01",
        "effective_to": "",
        "lock_id": f"future_lock_{scope}",
        "payload_sha256": "a" * 64,
        "model_contract_sha256": "b" * 64,
        "score_model_version": "consumer_model_v1",
        "scoring_contract_version": "score_contract_v1",
        "expected_alpha_at_full": 0.05,
        "optimizer_cap": 0.02,
    }


def test_shadow_snapshot_cannot_assert_production_authority() -> None:
    rows, locks = build_operational_rows(
        [_row("KO", "beverages", 1)],
        asof_date="2026-09-30",
    )
    assert locks == {}
    assert rows[0]["promotion_state"] == "shadow_monitor"
    assert rows[0]["portfolio_candidate_gate"] == 0
    assert rows[0]["oos_score_valid_flag"] == 0
    assert all(rows[0][field] == "" for field in LOCK_FIELDS)


def test_only_effectively_locked_cohort_is_promoted() -> None:
    registry = {"locks": [_lock("beverages")]}
    rows, locks = build_operational_rows(
        [
            _row("KO", "beverages", 1),
            _row("WMT", "consumer_staples_distribution_retail", 2),
        ],
        asof_date="2026-09-30",
        activation_registry=registry,
    )
    by_ticker = {str(row["ticker"]): row for row in rows}
    assert set(locks) == {"beverages"}
    assert by_ticker["KO"]["promotion_state"] == "promoted"
    assert by_ticker["KO"]["portfolio_candidate_gate"] == 1
    assert by_ticker["KO"]["oos_score_valid_flag"] == 1
    assert all(by_ticker["KO"][field] for field in LOCK_FIELDS)
    assert by_ticker["WMT"]["promotion_state"] == "shadow_monitor"
    assert by_ticker["WMT"]["portfolio_candidate_gate"] == 0


def test_locked_cohort_rejects_changed_model_version() -> None:
    row = _row("KO", "beverages", 1)
    row["score_model_version"] = "unreviewed_model_v2"
    with pytest.raises(ValueError, match="outside the active lock"):
        build_operational_rows(
            [row],
            asof_date="2026-09-30",
            activation_registry={"locks": [deepcopy(_lock("beverages"))]},
        )


def test_shadow_publish_is_byte_bound_and_independently_validated(tmp_path) -> None:
    stage10 = tmp_path / "stage10"
    stage10.mkdir()
    row = _row("KO", "beverages", 1)
    fields = list(row)
    rank_path = stage10 / "consumer_defensive_final_rank_table.csv"
    with rank_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerow(row)
    rank_hash = hashlib.sha256(rank_path.read_bytes()).hexdigest()
    (stage10 / "consumer_defensive_dashboard_manifest.json").write_text(
        json.dumps(
            {
                "asof_date": "2026-09-30",
                "file_sha256s": {
                    "consumer_defensive_final_rank_table.csv": rank_hash
                },
            }
        ),
        encoding="utf-8",
    )
    (stage10 / "consumer_defensive_stage10_validation.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "asof_date": "2026-09-30",
                "check_count": 2,
                "passed_check_count": 2,
            }
        ),
        encoding="utf-8",
    )
    output_root = tmp_path / "dashboard"
    manifest = publish_operational_snapshot(
        stage10_dir=stage10,
        output_root=output_root,
        asof_date="2026-09-30",
    )
    assert manifest["mode"] == "shadow"
    assert manifest["portfolio_candidate_count"] == 0
    assert validate_operational_snapshot(output_root / "2026-09-30") == manifest
    assert publish_operational_snapshot(
        stage10_dir=stage10,
        output_root=output_root,
        asof_date="2026-09-30",
    ) == manifest

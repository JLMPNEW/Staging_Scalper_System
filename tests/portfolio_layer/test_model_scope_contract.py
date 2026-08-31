from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

from portfolio_layer.core.contracts import upsert_stocks_scores, write_csv


def _calibration_module():
    path = Path("portfolio_layer/scores/02_calibrate_cross_sector_scores.py")
    spec = importlib.util.spec_from_file_location("scope_calibration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validation_module():
    path = Path("portfolio_layer/scores/03_validate_score_contract.py")
    spec = importlib.util.spec_from_file_location("scope_validation", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_percentiles_are_independent_by_model_scope() -> None:
    module = _calibration_module()
    rows = [
        {"source_pipeline": "consumer_defensive", "model_scope_id": "beverages", "native_score": 10, "missing_score_flag": 0},
        {"source_pipeline": "consumer_defensive", "model_scope_id": "beverages", "native_score": 20, "missing_score_flag": 0},
        {"source_pipeline": "consumer_defensive", "model_scope_id": "consumer_staples_distribution_retail", "native_score": 90, "missing_score_flag": 0},
    ]
    module.assign_percentiles_and_ratings(rows, module.DEFAULT_RATING_BANDS)
    assert [rows[0]["within_sector_percentile"], rows[1]["within_sector_percentile"]] == [25.0, 75.0]
    assert rows[2]["within_sector_percentile"] == 50.0


def test_validator_recomputes_calibration_within_model_scope() -> None:
    module = _validation_module()
    rows = [
        {
            "ticker": "A",
            "source_pipeline": "consumer_defensive",
            "model_scope_id": "beverages",
            "native_score": "10",
            "final_score": "-0.1",
            "within_sector_percentile": "25.0",
            "rating": "reduce",
            "missing_score_flag": "0",
        },
        {
            "ticker": "B",
            "source_pipeline": "consumer_defensive",
            "model_scope_id": "beverages",
            "native_score": "20",
            "final_score": "0.1",
            "within_sector_percentile": "75.0",
            "rating": "buy",
            "missing_score_flag": "0",
        },
        {
            "ticker": "C",
            "source_pipeline": "consumer_defensive",
            "model_scope_id": "food_products",
            "native_score": "90",
            "final_score": "-0.2",
            "within_sector_percentile": "50.0",
            "rating": "hold",
            "missing_score_flag": "0",
        },
    ]
    non_monotone, rank_errors = module.calibration_population_errors(
        rows, module.DEFAULT_RATING_BANDS
    )
    assert non_monotone == []
    assert rank_errors == []


def test_scope_and_policy_lineage_persist_to_contract_database() -> None:
    conn = sqlite3.connect(":memory:")
    row = {
        "as_of_date": "2026-09-30",
        "ticker": "KO",
        "source_pipeline": "consumer_defensive",
        "sector": "Consumer Staples",
        "industry": "Beverages",
        "industry_aggregate": "Consumer Staples",
        "model_scope_id": "beverages",
        "production_policy_id": "future_lock_beverages",
        "production_policy_sha256": "a" * 64,
        "selection_reliability_class": "high",
        "active_sleeve_weight": 0.9,
        "benchmark_residual_weight": 0.1,
        "benchmark_residual_ticker": "XBI",
        "final_score": 0.02,
        "rating": "buy",
        "within_sector_percentile": 75.0,
        "score_confidence": 0.8,
        "investable_eligible": 1,
        "eligibility_reason": "ok",
        "native_score": 65.0,
        "calibration_research_eligible": 1,
        "calibration_research_reason": "ok",
        "calibration_sample_role": "strict_oos",
        "stage1_sample_role": "strict_oos",
        "oos_score_valid_flag": 1,
        "missing_score_flag": 0,
        "survivorship_corrected_panel_flag": 0,
        "source_asof_date": "2026-09-30",
        "staleness_days": 0,
        "score_version": "stocks_scores_v1",
    }
    assert upsert_stocks_scores(conn, "2026-09-30", [row]) == 1
    persisted = conn.execute(
        "SELECT model_scope_id,production_policy_id,production_policy_sha256,"
        "selection_reliability_class,active_sleeve_weight,benchmark_residual_weight,"
        "benchmark_residual_ticker FROM stocks_scores"
    ).fetchone()
    assert persisted == (
        "beverages", "future_lock_beverages", "a" * 64, "high", 0.9, 0.1, "XBI"
    )


def test_empty_med_device_upstream_gate_is_visible_degradation(tmp_path: Path) -> None:
    module = _validation_module()
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    write_csv(
        raw_dir / "med_devices_scores.csv",
        [
            "ticker",
            "portfolio_candidate_gate",
            "portfolio_candidate_score",
            "analyst_review_decision",
        ],
        [
            {
                "ticker": "MDX",
                "portfolio_candidate_gate": "0",
                "portfolio_candidate_score": "80",
                "analyst_review_decision": "approve",
            }
        ],
    )

    errors, warnings = module.validate_med_devices_handoff(
        run_dir=tmp_path,
        score_rows=[
            {
                "ticker": "MDX",
                "source_pipeline": "med_devices",
                "investable_eligible": "0",
                "eligibility_reason": "failed_portfolio_candidate_gate",
            }
        ],
    )

    assert errors == []
    assert warnings == ["upstream_portfolio_candidate_gate_is_empty"]

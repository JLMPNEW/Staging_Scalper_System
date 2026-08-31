from __future__ import annotations

from portfolio_layer.scores.adapters import _adapt_biotech


def _config() -> dict[str, object]:
    return {
        "model_family": "biotech",
        "sector": "Health Care",
        "industry": "Biotechnology",
        "industry_aggregate": "Health Care",
    }


def _row(*, missing_flag: str) -> dict[str, str]:
    return {
        "ticker": "ZERO",
        "asof_date": "2026-08-21",
        "production_rank_score_field": "opportunity_score",
        "native_score_field": "opportunity_score",
        "native_score_value": "0.0",
        "opportunity_score": "0.0",
        "production_rank_score": "0.0",
        "portfolio_candidate_gate": "0",
        "portfolio_candidate_status": "excluded",
        "portfolio_candidate_reason": "allocation_bucket_avoid",
        "eligibility_reason": "allocation_bucket_avoid",
        "score_zero_is_missing_flag": missing_flag,
        "calibration_eligible_flag": "1",
        "price_data_asof_date": "2026-08-21",
        "research_calibration_input_eligible_flag": "1",
        "stage11_calibration_input_eligible_flag": "1",
        "stage11_calibration_input_reason": "ok",
        "calibration_sample_role": "pre_lock_research",
        "oos_score_valid_flag": "0",
        "survivorship_corrected_panel_flag": "1",
        "score_confidence": "1.0",
        "biotech_selection_reliability_class": "medium",
        "biotech_active_sleeve_weight": "0.55",
        "biotech_xbi_residual_weight": "0.45",
        "biotech_promotion_contract_id": "contract-v1",
        "biotech_promotion_contract_sha256": "a" * 64,
    }


def test_explicit_computed_zero_is_not_reinterpreted_as_missing() -> None:
    scores = _adapt_biotech(_config(), [_row(missing_flag="0")])

    assert len(scores) == 1
    assert scores[0].native_score == 0.0
    assert scores[0].missing_score_flag == 0
    assert scores[0].investable_eligible == 0
    assert scores[0].calibration_research_eligible == 1
    assert scores[0].selection_reliability_class == "medium"
    assert scores[0].active_sleeve_weight == 0.55
    assert scores[0].benchmark_residual_weight == 0.45
    assert scores[0].benchmark_residual_ticker == "XBI"


def test_explicit_missing_zero_remains_missing() -> None:
    scores = _adapt_biotech(_config(), [_row(missing_flag="1")])

    assert len(scores) == 1
    assert scores[0].missing_score_flag == 1
    assert scores[0].investable_eligible == 0
    assert scores[0].calibration_research_eligible == 0

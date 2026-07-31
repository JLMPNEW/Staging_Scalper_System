from __future__ import annotations

from industrials.machinery.scoring import FINAL_RANK_FIELDS, validate_rank_rows
from industrials.machinery.stage8_calibration import COMPONENT_FIELDS
from industrials.machinery.stage12_governance import production_preview_rows
from industrials.machinery.stage9_backtest import StrategySpec


def _row(
    ticker: str,
    score: float,
    *,
    eligible: bool,
    development_stage: str = "operating",
) -> dict[str, str]:
    row = {field: "" for field in FINAL_RANK_FIELDS}
    row.update(
        {
            "ticker": ticker,
            "asof_date": "2026-07-24",
            "rank_ready_flag": "1" if eligible else "0",
            "rank_ready_reason": "ok" if eligible else "low_score_confidence",
            "model_status": "complete" if eligible else "incomplete",
            "development_stage": development_stage,
        }
    )
    row.update({field: str(score) for field in COMPONENT_FIELDS})
    return row


def test_production_preview_isolated_flags_and_rank_order() -> None:
    weights = {
        field: 1.0 / len(COMPONENT_FIELDS) for field in COMPONENT_FIELDS
    }

    rows = production_preview_rows(
        [
            _row("INELIGIBLE", 99.0, eligible=False),
            _row("SELECTED", 80.0, eligible=True),
            _row("ELIGIBLE_NOT_SELECTED", 60.0, eligible=True),
        ],
        weights=weights,
        asof="2026-07-24",
        lock_date="2026-01-01",
        score_model_version="test_oos",
        model_version="test_model",
        scoring_contract_version="test_contract",
        selection_spec=StrategySpec(
            name="long_only_q20_equal",
            portfolio_type="long_only",
            weighting="equal",
            quantile=0.20,
        ),
        minimum_positions=1,
        universe_policy="operating_only",
    )

    assert [row["ticker"] for row in rows] == [
        "SELECTED",
        "ELIGIBLE_NOT_SELECTED",
        "INELIGIBLE",
    ]
    assert rows[0]["portfolio_universe_eligible_flag"] == "1"
    assert rows[0]["portfolio_sleeve_selected_flag"] == "1"
    assert rows[0]["portfolio_sleeve_target_weight"] == "1"
    assert rows[0]["portfolio_candidate_gate"] == "1"
    assert rows[0]["portfolio_candidate_status"] == "eligible"
    assert rows[0]["oos_score_valid_flag"] == "1"
    assert rows[0]["calibration_sample_role"] == "strict_oos"
    assert rows[1]["portfolio_candidate_gate"] == "0"
    assert rows[1]["portfolio_candidate_status"] == "not_selected"
    assert rows[1]["portfolio_sleeve_selected_flag"] == "0"
    assert rows[1]["oos_score_valid_flag"] == "1"
    assert rows[2]["portfolio_candidate_gate"] == "0"
    assert rows[2]["oos_score_valid_flag"] == "0"


def test_development_stage_remains_research_valid_but_not_investable() -> None:
    weights = {
        field: 1.0 / len(COMPONENT_FIELDS) for field in COMPONENT_FIELDS
    }
    rows = production_preview_rows(
        [
            _row(
                "DEV",
                99.0,
                eligible=True,
                development_stage="development_stage",
            ),
            _row("OPERATING", 70.0, eligible=True),
        ],
        weights=weights,
        asof="2026-07-24",
        lock_date="2026-01-01",
        score_model_version="test_oos",
        model_version="test_model",
        scoring_contract_version="test_contract",
        selection_spec=StrategySpec(
            name="long_only_q20_equal",
            portfolio_type="long_only",
            weighting="equal",
            quantile=0.20,
        ),
        minimum_positions=1,
        universe_policy="operating_only",
    )

    development = next(row for row in rows if row["ticker"] == "DEV")
    operating = next(row for row in rows if row["ticker"] == "OPERATING")
    assert development["portfolio_universe_eligible_flag"] == "0"
    assert development["portfolio_candidate_gate"] == "0"
    assert development["portfolio_candidate_reason"] == (
        "development_stage_core_sleeve_excluded"
    )
    assert development["research_calibration_input_eligible_flag"] == "1"
    assert development["oos_score_valid_flag"] == "1"
    assert operating["portfolio_candidate_gate"] == "1"
    validation_errors = validate_rank_rows(
        rows,
        asof="2026-07-24",
        allow_production=True,
    )
    assert not any(
        "OOS-valid core-sleeve exclusion" in error
        or "production universe eligibility requires OOS validity" in error
        for error in validation_errors
    )

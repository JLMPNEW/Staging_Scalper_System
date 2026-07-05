from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from industrials.defense.research_artifacts import PILLAR_SCORE_FIELDS


def load_promotion_module() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[2]
        / "industrials"
        / "defense"
        / "scripts"
        / "27_promote_defense_oos_production.py"
    )
    spec = importlib.util.spec_from_file_location("defense_production_promotion", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def row(ticker: str, *, rank_ready: str, model_status: str, score: str) -> dict[str, str]:
    payload = {
        "ticker": ticker,
        "final_score": score,
        "rank_ready_flag": rank_ready,
        "model_status": model_status,
        "review_reason": "" if rank_ready == "1" else "not_rank_ready",
        "eligibility_reason": "",
    }
    for field in PILLAR_SCORE_FIELDS:
        payload[field] = score
    return payload


def test_promote_rows_sets_production_gates_and_reorders_by_weighted_score() -> None:
    module = load_promotion_module()
    rows = [
        row("AAA", rank_ready="1", model_status="complete", score="60"),
        row("BBB", rank_ready="0", model_status="review", score="90"),
    ]
    weights = {field: 1.0 for field in PILLAR_SCORE_FIELDS}

    promoted = module.promote_rows(
        rows,
        weights=weights,
        asof="2026-07-02",
        train_start="2019-01-04",
        train_end="2024-01-05",
        provenance_version="production_oos_validated",
    )

    assert [item["ticker"] for item in promoted] == ["BBB", "AAA"]
    assert promoted[0]["final_rank"] == "1"
    assert promoted[0]["portfolio_candidate_gate"] == "0"
    assert promoted[0]["portfolio_candidate_reason"] == "not_rank_ready"
    assert promoted[1]["portfolio_candidate_gate"] == "1"
    assert promoted[1]["portfolio_candidate_reason"] == "ok"
    assert promoted[1]["calibration_eligible_flag"] == "1"
    assert promoted[1]["oos_score_valid_flag"] == "1"
    assert promoted[1]["oos_score_asof_date"] == "2026-07-02"
    assert promoted[1]["research_calibration_eligible_flag"] == promoted[1]["research_calibration_input_eligible_flag"]
    assert promoted[1]["calibration_sample_role"] == "strict_oos"


def test_promotion_issues_require_explicit_overlap_acceptance() -> None:
    module = load_promotion_module()

    issues = module.promotion_issues(
        asof="2026-07-02",
        panel_check={"status": "pass", "promotable": "1"},
        snapshot_readiness_rows=[{"asof_date": "2026-07-02", "status": "pass"}],
        calibration_summary={
            "selection_metric": "validation_ic",
            "validation_ic": "0.10",
            "holdout_ic": "0.05",
        },
        backtest_summary={
            "holdout_mean_excess_vs_benchmark": "0.01",
            "selected_mean_excess_vs_benchmark": "0.02",
            "overlapping_forward_windows_flag": "1",
        },
        accept_overlap_warning=False,
        min_validation_ic=0.0,
        min_holdout_ic=0.0,
        min_holdout_excess=0.0,
    )

    assert issues == [
        "weekly backtest windows overlap; rerun with --accept-overlap-warning to promote with audit waiver"
    ]
    assert (
        module.promotion_issues(
            asof="2026-07-02",
            panel_check={"status": "pass", "promotable": "1"},
            snapshot_readiness_rows=[{"asof_date": "2026-07-02", "status": "pass"}],
            calibration_summary={
                "selection_metric": "validation_ic",
                "validation_ic": "0.10",
                "holdout_ic": "0.05",
            },
            backtest_summary={
                "holdout_mean_excess_vs_benchmark": "0.01",
                "selected_mean_excess_vs_benchmark": "0.02",
                "overlapping_forward_windows_flag": "1",
            },
            accept_overlap_warning=True,
            min_validation_ic=0.0,
            min_holdout_ic=0.0,
            min_holdout_excess=0.0,
        )
        == []
    )

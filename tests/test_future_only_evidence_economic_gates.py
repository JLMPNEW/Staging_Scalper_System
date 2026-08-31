from __future__ import annotations

from datetime import date

from future_only_evidence.prospective_contracts import ProspectiveContract
from future_only_evidence.prospective_evaluator import (
    apply_costs,
    deterministic_nonoverlap,
    scope_verdict,
    weighted_verdict_periods,
)


def _contract() -> ProspectiveContract:
    return ProspectiveContract(
        family="test",
        policy_id="test",
        effective_from=date(2026, 1, 1),
        first_signal_date=date(2026, 1, 31),
        horizons=(21,),
        minimum_counts={21: 1},
        benchmark_ticker="SPY",
        cadence_id="monthly_true_month_end_v1",
        minimum_ic=0.0,
        minimum_efficacy=0.0,
        minimum_top_minus_bottom=0.0,
        minimum_hit_rate=0.55,
        transaction_cost_bps=100.0,
        top_minus_bottom_basis="net",
    )


def _period(capture_id: str, *, ic=0.5, top=0.10, bottom=0.0, cohort=0.05):
    return {
        "capture_id": capture_id,
        "asof_date": "2026-01-31",
        "entry_date": "2026-02-02",
        "exit_date": "2026-03-03",
        "entry_session_index": 1,
        "exit_session_index": 22,
        "cross_section": 4,
        "ic": ic,
        "top_gross": top,
        "bottom_gross": bottom,
        "cohort_gross": cohort,
        "top_tickers": ["AAA"],
        "bottom_tickers": ["BBB"],
        "cohort_tickers": ["AAA", "BBB", "CCC", "DDD"],
    }


def test_long_short_costs_are_charged_on_both_legs() -> None:
    row = apply_costs([_period("a")], transaction_cost_bps=100.0)[0]
    assert abs(row["top_minus_cohort_net"] - 0.01) < 1e-12
    assert abs(row["top_minus_bottom_net"] - 0.06) < 1e-12


def test_undefined_ic_in_any_counted_period_fails_gate() -> None:
    row = apply_costs([_period("a", ic=None)], transaction_cost_bps=100.0)
    verdict = scope_verdict(
        row,
        contract=_contract(),
        horizon=21,
        minimum_cross_section=2,
        efficacy_field="top_net",
        hit_field="top_net",
    )
    assert verdict["gates"]["all_counted_ic_defined_pass"] is False
    assert verdict["pass"] is False


def test_partial_predictive_group_weights_are_normalized_for_gates() -> None:
    first = apply_costs([_period("a", top=0.10)], transaction_cost_bps=0.0)
    second = apply_costs([_period("a", top=0.20)], transaction_cost_bps=0.0)
    aggregate = weighted_verdict_periods(
        {"g1": first, "g2": second},
        group_weights={"g1": 0.25, "g2": 0.25},
    )[0]
    assert abs(aggregate["top_gross"] - 0.15) < 1e-12
    assert aggregate["group_weight_total"] == 0.5


def test_positive_mean_and_hit_rate_cannot_bypass_ic_sign_test() -> None:
    contract = ProspectiveContract(
        **{
            **_contract().__dict__,
            "minimum_counts": {21: 12},
            "transaction_cost_bps": 0.0,
            "maximum_ic_sign_pvalue": 0.10,
        }
    )
    periods = []
    for index in range(12):
        row = _period(str(index), ic=0.10 if index < 7 else -0.01)
        row["entry_session_index"] = index * 21
        row["exit_session_index"] = (index + 1) * 21
        periods.append(row)
    verdict = scope_verdict(
        apply_costs(periods, transaction_cost_bps=0.0),
        contract=contract,
        horizon=21,
        minimum_cross_section=2,
        efficacy_field="top_net",
        hit_field="top_net",
    )
    assert verdict["mean_ic"] > 0
    assert verdict["hit_rate"] >= 0.55
    assert verdict["one_sided_ic_sign_pvalue"] > 0.10
    assert verdict["gates"]["ic_sign_test_pass"] is False
    assert verdict["pass"] is False


def test_later_monitor_periods_cannot_change_first_n_decision_inputs() -> None:
    periods = []
    for index, top in enumerate((0.02, -0.01, 1.00, -1.00)):
        row = _period(str(index), top=top)
        row["entry_session_index"] = index * 22
        row["exit_session_index"] = index * 22 + 21
        periods.append(row)
    early = apply_costs(
        deterministic_nonoverlap(periods[:2])[:2],
        transaction_cost_bps=20.0,
    )
    after_extreme_monitors = apply_costs(
        deterministic_nonoverlap(periods)[:2],
        transaction_cost_bps=20.0,
    )
    assert after_extreme_monitors == early

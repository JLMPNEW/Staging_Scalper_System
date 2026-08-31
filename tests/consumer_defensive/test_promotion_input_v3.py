from __future__ import annotations

import copy

import pytest

from consumer_defensive.core.promotion_engine_v3 import (
    build_capital_allocation_context,
    canonical_sha256,
    validate_capital_allocation_context,
    value_sha256,
)
from consumer_defensive.core.promotion_input_v3 import (
    build_matched_benchmark_paths,
    validate_benchmark_attestation,
)


COHORTS = (
    "beverages",
    "consumer_staples_distribution_retail",
    "household_personal_tobacco",
    "packaged_foods_agricultural_products",
)
HORIZONS = (21, 63, 126)
CALENDAR = ("2026-01-02", "2026-01-05", "2026-01-06")


def _capital_context() -> dict:
    return build_capital_allocation_context(
        asof_date="2026-08-28",
        account_aum_usd=500_000.0,
        active_sector_count=8,
        sector_max_fraction=0.125,
        calibration_reference_notional_usd=1_000_000.0,
    )


def _inputs() -> tuple[dict, list[dict], dict]:
    strategy: dict[str, dict[str, list[dict[str, object]]]] = {}
    membership: list[dict[str, object]] = []
    prices: dict[str, dict[str, float]] = {
        "XLP": {CALENDAR[0]: 100.0, CALENDAR[1]: 101.0, CALENDAR[2]: 102.01},
        "SPY": {CALENDAR[0]: 200.0, CALENDAR[1]: 202.0, CALENDAR[2]: 204.02},
    }
    for cohort_index, cohort in enumerate(COHORTS, start=1):
        tickers = (f"C{cohort_index}A", f"C{cohort_index}B")
        for ticker, multiplier in zip(tickers, (1.01, 1.03)):
            prices[ticker] = {
                CALENDAR[0]: 100.0,
                CALENDAR[1]: 100.0 * multiplier,
                CALENDAR[2]: 100.0 * multiplier * multiplier,
            }
            membership.append(
                {
                    "asof_date": CALENDAR[0],
                    "ticker": ticker,
                    "cohort_id": cohort,
                    "membership_eligible_flag": 1,
                    "investable_flag": 1,
                }
            )
        strategy[cohort] = {
            str(horizon): [
                {
                    "signal_date": CALENDAR[0],
                    "return_date": CALENDAR[1],
                    "observation_id": f"{cohort}-{horizon}-1",
                    "net_return": 0.025,
                },
                {
                    "signal_date": CALENDAR[0],
                    "return_date": CALENDAR[2],
                    "observation_id": f"{cohort}-{horizon}-2",
                    "net_return": -0.005,
                },
            ]
            for horizon in HORIZONS
        }
    return strategy, membership, prices


def test_builds_exact_pit_peer_xlp_and_spy_paths() -> None:
    strategy, membership, prices = _inputs()
    paths, attestation = build_matched_benchmark_paths(
        strategy_path_rows_by_cohort=strategy,
        membership_rows=membership,
        prices=prices,
        calendar=CALENDAR,
    )

    first = paths[COHORTS[0]]["63"][0]
    assert first["primary_benchmark_return"] == pytest.approx(0.02)
    assert first["xlp_return"] == pytest.approx(0.01)
    assert first["spy_return"] == pytest.approx(0.01)
    assert attestation["cohorts"][COHORTS[0]]["63"][0]["peer_count"] == 2
    assert len(attestation["payload_sha256"]) == 64


def test_missing_peer_mark_fails_closed_instead_of_changing_census() -> None:
    strategy, membership, prices = _inputs()
    del prices["C1B"][CALENDAR[1]]
    with pytest.raises(ValueError, match="price C1B/2026-01-05"):
        build_matched_benchmark_paths(
            strategy_path_rows_by_cohort=strategy,
            membership_rows=membership,
            prices=prices,
            calendar=CALENDAR,
        )


def test_future_signal_membership_is_rejected() -> None:
    strategy, membership, prices = _inputs()
    tampered = copy.deepcopy(strategy)
    for horizon in HORIZONS:
        tampered[COHORTS[0]][str(horizon)][0]["signal_date"] = CALENDAR[2]
    membership.extend(
        {
            "asof_date": CALENDAR[2],
            "ticker": ticker,
            "cohort_id": COHORTS[0],
            "membership_eligible_flag": 1,
            "investable_flag": 1,
        }
        for ticker in ("C1A", "C1B")
    )
    with pytest.raises(ValueError, match="signal date must precede return date"):
        build_matched_benchmark_paths(
            strategy_path_rows_by_cohort=tampered,
            membership_rows=membership,
            prices=prices,
            calendar=CALENDAR,
        )


def test_duplicate_membership_identity_is_rejected() -> None:
    strategy, membership, prices = _inputs()
    membership.append(dict(membership[0]))
    with pytest.raises(ValueError, match="membership identity is duplicated"):
        build_matched_benchmark_paths(
            strategy_path_rows_by_cohort=strategy,
            membership_rows=membership,
            prices=prices,
            calendar=CALENDAR,
        )


def test_rehashed_benchmark_with_false_peer_arithmetic_is_rejected() -> None:
    strategy, membership, prices = _inputs()
    paths, attestation = build_matched_benchmark_paths(
        strategy_path_rows_by_cohort=strategy,
        membership_rows=membership,
        prices=prices,
        calendar=CALENDAR,
    )
    tampered = copy.deepcopy(attestation)
    tampered["cohorts"][COHORTS[0]]["63"][0]["peer_rows"][0]["return"] += 0.01
    tampered["payload_sha256"] = value_sha256(
        {key: value for key, value in tampered.items() if key != "payload_sha256"}
    )
    with pytest.raises(ValueError, match="peer return does not reconcile"):
        validate_benchmark_attestation(
            tampered, matched_paths_by_cohort=paths
        )


def test_capital_context_is_self_hashed_and_may_postdate_predictive_evidence() -> None:
    context = _capital_context()

    assert validate_capital_allocation_context(
        context,
        evidence_asof_date="2026-08-27",
    ) == context
    assert context["sector_max_notional_usd"] == pytest.approx(62_500.0)
    assert context["calibration_reference_notional_usd"] == pytest.approx(
        1_000_000.0
    )


def test_capital_context_cannot_be_backdated_before_predictive_evidence() -> None:
    context = _capital_context()
    context["asof_date"] = "2026-08-26"
    context["payload_sha256"] = canonical_sha256(context)

    with pytest.raises(ValueError, match="cannot predate promotion evidence"):
        validate_capital_allocation_context(
            context,
            evidence_asof_date="2026-08-27",
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("account_aum_usd", 0.0, "account AUM must be positive"),
        ("active_sector_count", True, "must be an integer"),
        ("sector_max_fraction", 0.20, "equal active-sector allocation"),
        ("sector_max_notional_usd", 62_499.0, "does not reconcile"),
        (
            "calibration_reference_notional_usd",
            0.0,
            "reference notional must be positive",
        ),
    ],
)
def test_rehashed_invalid_capital_context_fails_closed(
    field: str,
    value: object,
    message: str,
) -> None:
    context = _capital_context()
    context[field] = value
    context["payload_sha256"] = canonical_sha256(context)

    with pytest.raises(ValueError, match=message):
        validate_capital_allocation_context(context)


def test_capital_context_tamper_without_rehash_is_rejected() -> None:
    context = _capital_context()
    context["account_aum_usd"] = 600_000.0

    with pytest.raises(ValueError):
        validate_capital_allocation_context(context)

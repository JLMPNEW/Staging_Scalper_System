from __future__ import annotations

import copy
from datetime import date
from pathlib import Path

import pytest

from industrials.transportation.subgroup_scoring import (
    build_fact_history,
    derive_feature,
    load_subgroup_score_policy,
    ticker_location,
)


ROOT = Path(__file__).resolve().parents[2]
POLICY = (
    ROOT
    / "industrials"
    / "transportation"
    / "data"
    / "transportation_subgroup_score_policy_v8.yaml"
)


def accepted_row(
    *,
    metric: str,
    value: float,
    period_end: str,
    filing_date: str,
    definition: str,
    unit: str = "ratio",
) -> dict[str, object]:
    return {
        "ticker": "AAA",
        "metric_id": metric,
        "value": value,
        "unit": unit,
        "period_end": period_end,
        "filing_date": filing_date,
        "accepted_at": filing_date,
        "definition_basis": definition,
        "comparability_class": "issuer_stable",
        "candidate_key": f"{metric}-{period_end}",
        "replay_status": "ACCEPTED",
    }


def test_v8_policy_partitions_35_active_tickers_and_preserves_history() -> None:
    policy = load_subgroup_score_policy(POLICY)
    current = {
        ticker
        for cohort in policy["cohorts"].values()
        for group in cohort["groups"].values()
        for ticker in group["tickers"]
    }
    assert len(current) == 35
    assert ticker_location("ARCB", "2026-07-30", policy) == (
        "surface_freight_core",
        "ltl_carriers",
    )
    assert ticker_location("YELL", "2023-06-30", policy) == (
        "surface_freight_core",
        "ltl_carriers",
    )
    assert ticker_location("YELL", "2024-01-31", policy) is None


def test_change_feature_requires_stable_definition_and_filing_date() -> None:
    rows = [
        accepted_row(
            metric="operating_ratio",
            value=0.90,
            period_end="2024-12-31",
            filing_date="2025-02-15",
            definition="segment_operating_expense_over_revenue",
        ),
        accepted_row(
            metric="operating_ratio",
            value=0.85,
            period_end="2025-12-31",
            filing_date="2026-02-15",
            definition="segment_operating_expense_over_revenue",
        ),
    ]
    history = build_fact_history(rows)
    spec = {
        "source_metric": "operating_ratio",
        "transform": "yoy_improvement",
    }
    value, sources = derive_feature(
        ticker="AAA",
        asof=date(2026, 3, 31),
        spec=spec,
        history=history,
        staleness_days={"operating_ratio": 550},
    )
    assert value == pytest.approx((0.90 - 0.85) / 0.90)
    assert len(sources) == 2
    unavailable, _ = derive_feature(
        ticker="AAA",
        asof=date(2026, 1, 31),
        spec=spec,
        history=history,
        staleness_days={"operating_ratio": 550},
    )
    assert unavailable is None


def test_policy_excludes_operating_ratio_level_and_has_meaningful_tanker_pack() -> None:
    policy = load_subgroup_score_policy(POLICY)
    group = policy["cohorts"]["surface_freight_core"]["groups"]["rail_networks"]
    assert "operating_ratio_level" not in group["specialized_pack"]
    assert (
        group["specialized_pack"]["operating_ratio_yoy_improvement"]["transform"]
        == "yoy_improvement"
    )
    tanker = policy["cohorts"]["oil_tanker_operators"]["groups"]["oil_tankers"]
    assert tanker["component_weights_active"]["specialized"] >= 0.25


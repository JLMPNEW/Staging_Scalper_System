from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from industrials.core.db import XBRL_CONCEPT_MAP_SEED
from industrials.core.oos_price_lineage import (
    audit_panel_return_lineage,
    price_slice_rows,
)
from industrials.core.oos_research import (
    ExecutionPricePoint,
    finite_or_default,
)
from industrials.transportation.calibration_preflight import (
    audit_candidate_component_coverage,
    candidate_registry,
)
from industrials.transportation.classification import (
    load_classification_overlays,
    resolve_classification,
)
from industrials.transportation.contracts import COMPONENT_FIELDS
from industrials.transportation.financial_contract import load_metric_registry
from industrials.transportation.scoring import positioning_component_scores
from industrials.transportation.surface_freight_research import (
    add_positioning_research_scores,
    build_directional_metric_scores,
    load_surface_freight_policy,
    mean_reversion_score_field,
    metric_score_field,
    surface_freight_cohort_eligible,
    train_derived_candidate_registry,
)
from industrials.transportation.surface_freight_score_engine import (
    build_surface_component_scores,
    cohort_score_eligible,
    candidate_registry_from_policy,
    load_cohort_score_policy,
    load_surface_freight_score_policy,
    metric_comparison_group,
    metric_comparison_group_for_metric,
    score_surface_metric_percentiles,
)
from industrials.transportation.valuation_source_audit import (
    companyfacts_path,
    inspect_companyfacts_share_sources,
    load_share_conversions,
    resolve_share_conversion,
    summarize_audit,
)


def test_positioning_scores_respect_foreign_form4_applicability() -> None:
    members = [
        {"ticker": ticker, "calibration_cohort_id": "surface_freight_and_logistics"}
        for ticker in ("CNI", "CP", "UNP", "CSX", "MISSING")
    ]
    rows = {
        "CNI": {
            "form4_status": "not_applicable",
            "institutional_ownership_delta_pct": 0.03,
            "short_interest_change_3m": -0.02,
        },
        "CP": {
            "form4_status": "not_applicable",
            "institutional_ownership_delta_pct": 0.01,
            "short_interest_change_3m": 0.01,
        },
        "UNP": {
            "form4_status": "covered",
            "insider_net_value_90d": 0.0,
            "insider_cluster_buyers_90d": 0,
            "institutional_ownership_delta_pct": -0.01,
            "short_interest_change_3m": 0.03,
        },
        "CSX": {
            "form4_status": "covered",
            "insider_net_value_90d": 100000.0,
            "insider_cluster_buyers_90d": 2,
            "institutional_ownership_delta_pct": 0.02,
            "short_interest_change_3m": -0.01,
        },
    }
    scores, coverage = positioning_component_scores(members, rows)
    assert set(scores) == {"CNI", "CP", "UNP", "CSX"}
    assert coverage["CNI"] == {"observed": 2, "applicable": 2}
    assert coverage["CP"] == {"observed": 2, "applicable": 2}
    assert coverage["UNP"] == {"observed": 4, "applicable": 4}
    assert coverage["MISSING"] == {"observed": 0, "applicable": 4}


def test_positioning_scores_support_frozen_peer_group_normalization() -> None:
    members = [
        {"ticker": ticker, "calibration_cohort_id": "surface_freight_and_logistics"}
        for ticker in ("LIGHT1", "LIGHT2", "ASSET1", "ASSET2")
    ]
    rows = {
        "LIGHT1": {"form4_status": "not_applicable", "institutional_ownership_delta_pct": 1.0},
        "LIGHT2": {"form4_status": "not_applicable", "institutional_ownership_delta_pct": 2.0},
        "ASSET1": {"form4_status": "not_applicable", "institutional_ownership_delta_pct": 100.0},
        "ASSET2": {"form4_status": "not_applicable", "institutional_ownership_delta_pct": 200.0},
    }
    scores, _ = positioning_component_scores(
        members,
        rows,
        comparison_group_by_ticker={
            "LIGHT1": "asset_light_logistics",
            "LIGHT2": "asset_light_logistics",
            "ASSET1": "asset_based_freight",
            "ASSET2": "asset_based_freight",
        },
    )
    assert scores == {
        "LIGHT1": 0.0,
        "LIGHT2": 100.0,
        "ASSET1": 0.0,
        "ASSET2": 100.0,
    }


def test_shared_debt_map_includes_loaded_capital_lease_concepts() -> None:
    mapping = {
        (str(row["taxonomy"]), str(row["concept_name"])): str(
            row["canonical_metric"]
        )
        for row in XBRL_CONCEPT_MAP_SEED
    }
    assert mapping[("us-gaap", "LongTermDebtAndCapitalLeaseObligations")] == (
        "debt_noncurrent"
    )
    assert mapping[
        ("us-gaap", "LongTermDebtAndCapitalLeaseObligationsCurrent")
    ] == "debt_current"
    assert mapping[
        (
            "us-gaap",
            "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
        )
    ] == "debt_total"
    assert mapping[("us-gaap", "DebtAndCapitalLeaseObligations")] == "debt_total"
    assert mapping[("us-gaap", "LongTermDebt")] == "debt_total"
    assert mapping[("us-gaap", "InterestAndDebtExpense")] == "interest_expense"
    assert mapping[
        ("us-gaap", "InterestIncomeExpenseNonoperatingNet")
    ] == "interest_expense"
    assert mapping[("ifrs-full", "FinanceCosts")] == "interest_expense"


def test_shared_map_includes_verified_eligible_freight_edge_concepts() -> None:
    mapping = {
        (str(row["taxonomy"]), str(row["concept_name"])): (
            str(row["canonical_metric"]),
            str(row["period_type"]),
            str(row["sign_policy"]),
        )
        for row in XBRL_CONCEPT_MAP_SEED
    }
    assert mapping[("us-gaap", "OtherDepreciationAndAmortization")] == (
        "depreciation_and_amortization",
        "duration",
        "positive_abs",
    )
    assert mapping[("us-gaap", "ShortTermBankLoansAndNotesPayable")] == (
        "debt_current",
        "instant",
        "as_reported",
    )


def test_positioning_component_is_exposed_only_when_observed() -> None:
    rows = add_positioning_research_scores(
        [
            {"ticker": "UNP", "positioning_score": "62.5"},
            {"ticker": "CSX", "positioning_score": ""},
        ]
    )
    field = metric_score_field("positioning_composite")
    assert rows[0][field] == 62.5
    assert field not in rows[1]


def test_classification_preserves_economic_pool_and_blocks_role_leakage() -> None:
    speculative_marine = resolve_classification(
        {
            "ticker": "CISS",
            "industry": "Marine Shipping",
            "calibration_cohort": "development_stage_and_speculative_transport",
            "calibration_use": "excluded",
            "development_stage": "development",
        },
        asof="2026-07-30",
    )
    assert speculative_marine.calibration_pool == "marine_shipping_and_maritime"
    assert speculative_marine.risk_tier == "development_speculative"
    assert speculative_marine.portfolio_role == "speculative_research"

    airline = resolve_classification(
        {
            "ticker": "DAL",
            "industry": "Airlines",
            "calibration_cohort": "air_transport_and_aviation_services",
            "calibration_use": "core",
            "development_stage": "operating",
        },
        asof="2026-07-30",
    )
    assert airline.risk_tier == "operating"
    assert airline.portfolio_role == "airline_satellite_research"
    assert not airline.production_portfolio_authorized


def test_classification_overlays_are_effective_dated_and_nonoverlapping(
    tmp_path: Path,
) -> None:
    path = tmp_path / "overlays.csv"
    path.write_text(
        "ticker,effective_from,effective_to,economic_peer_group,portfolio_role,review_status,source,notes\n"
        "PBI,2019-01-02,2024-12-31,mail_presort,universe_review,reviewed,test,old\n"
        "PBI,2025-01-01,,mail_presort_review,universe_review,reviewed,test,current\n",
        encoding="utf-8",
    )
    overlays = load_classification_overlays(path)
    result = resolve_classification(
        {
            "ticker": "PBI",
            "industry": "Integrated Freight & Logistics",
            "calibration_cohort": "surface_freight_and_logistics",
            "calibration_use": "core",
            "development_stage": "operating",
        },
        asof="2026-07-30",
        overlays=overlays,
    )
    assert result.economic_peer_group == "mail_presort_review"
    assert result.portfolio_role == "universe_review"

    path.write_text(
        "ticker,effective_from,effective_to,economic_peer_group,portfolio_role,review_status,source,notes\n"
        "PBI,2019-01-02,2025-12-31,one,universe_review,reviewed,test,one\n"
        "PBI,2025-01-01,,two,universe_review,reviewed,test,two\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="overlapping overlays"):
        load_classification_overlays(path)


def _panel_row(*, valuation: str) -> dict[str, object]:
    row: dict[str, object] = {
        "split": "train",
        "calibration_eligible_flag": "1",
        "outcome_available_flag": "1",
        "horizon_sessions": "63",
    }
    row.update({field: "50" for field in COMPONENT_FIELDS})
    row["valuation_score"] = valuation
    row["development_stage_risk_score"] = ""
    row["positioning_score"] = "50"
    return row


def test_operating_core_candidate_registry_masks_structural_components() -> None:
    baseline = {field: 1.0 for field in COMPONENT_FIELDS}
    candidates = candidate_registry(baseline)
    assert len(candidates) == 7
    for weights in candidates.values():
        assert weights["development_stage_risk_score"] == 0.0
        assert sum(weights.values()) == pytest.approx(1.0)

    failed = audit_candidate_component_coverage(
        [_panel_row(valuation="")],
        candidates=candidates,
        horizon_sessions=63,
        production_universe_policy="operating_core_only",
        minimum_complete_row_coverage=0.90,
    )
    assert failed["acceptance"] == "FAIL"
    assert all(
        "development_stage_risk_score" not in issue
        for issue in failed["issues"]
    )

    passed = audit_candidate_component_coverage(
        [_panel_row(valuation="50")],
        candidates=candidates,
        horizon_sessions=63,
        production_universe_policy="operating_core_only",
        minimum_complete_row_coverage=0.90,
    )
    assert passed["acceptance"] == "PASS"
    assert passed["structurally_excluded_components"] == [
        "development_stage_risk_score"
    ]


def test_candidate_sort_default_preserves_exact_zero() -> None:
    assert finite_or_default(0.0, default=-999.0) == 0.0
    assert finite_or_default("0", default=-999.0) == 0.0
    assert finite_or_default("", default=-999.0) == -999.0
    assert finite_or_default(None, default=-999.0) == -999.0


def test_frozen_price_slice_independently_reconstructs_panel_returns() -> None:
    def point(day: int, open_value: float) -> ExecutionPricePoint:
        return ExecutionPricePoint(
            bar_date=date(2024, 1, day),
            adjusted_open=open_value,
            adjusted_close=open_value,
            source_id="test_prices",
            price_basis="split_dividend_adjusted_open",
        )

    prices = {
        "AAA": {
            "test_prices": [
                point(2, 9.0),
                point(3, 10.0),
                point(4, 11.0),
                point(5, 12.0),
            ]
        },
        "IYT": {
            "test_prices": [
                point(2, 19.0),
                point(3, 20.0),
                point(4, 21.0),
                point(5, 22.0),
            ]
        },
    }
    frozen = price_slice_rows(
        prices,
        start_date="2024-01-01",
        end_date="2024-01-05",
    )
    panel = [
        {
            "asof_date": "2024-01-02",
            "ticker": "AAA",
            "horizon_sessions": "2",
            "outcome_available_flag": "1",
            "physical_price_ticker": "AAA",
            "security_price_source_id": "test_prices",
            "entry_date": "2024-01-03",
            "entry_adjusted_open": "10",
            "exit_date": "2024-01-05",
            "exit_execution_value": "12",
            "outcome_method": "scheduled_d1_open_to_open",
            "terminal_type": "",
            "security_forward_return": "0.2",
            "benchmark_ticker": "IYT",
            "benchmark_price_source_id": "test_prices",
            "benchmark_entry_date": "2024-01-03",
            "benchmark_exit_date": "2024-01-05",
            "benchmark_forward_return": "0.1",
            "forward_excess_return": "0.1",
        }
    ]
    passed = audit_panel_return_lineage(panel, frozen)
    assert passed["acceptance"] == "PASS"
    assert passed["recomputed_row_count"] == 1
    assert passed["maximum_absolute_error"] == pytest.approx(0.0)

    panel[0]["forward_excess_return"] = "-0.1"
    failed = audit_panel_return_lineage(panel, frozen)
    assert failed["acceptance"] == "FAIL"
    assert any(
        "forward_excess_return mismatch" in issue
        for issue in failed["issues"]
    )


def test_surface_freight_policy_is_outcome_blind_and_peer_normalized() -> None:
    root = Path(__file__).resolve().parents[2]
    policy = load_surface_freight_policy(
        root
        / "industrials"
        / "transportation"
        / "data"
        / "transportation_surface_freight_research_policy.yaml"
    )
    _, definitions = load_metric_registry(
        root
        / "industrials"
        / "transportation"
        / "data"
        / "transportation_metric_registry.yaml"
    )
    ret_3m = [item for item in definitions if item.metric_id == "ret_3m"]

    def row(ticker: str, peer: str, value: float) -> dict[str, object]:
        return {
            "asof_date": "2024-01-05",
            "ticker": ticker,
            "industry": (
                "Trucking" if peer == "trucking" else "Integrated Freight & Logistics"
            ),
            "calibration_cohort": "surface_freight_and_logistics",
            "calibration_pool": "surface_freight_and_logistics",
            "economic_peer_group": peer,
            "risk_tier": "operating",
            "portfolio_role": "core_candidate",
            "metric_values_json": json.dumps({"ret_3m": value}),
            "metric_status_json": json.dumps({"ret_3m": "REPORTED"}),
        }

    rows = [
        row("AAA", "integrated_freight_and_logistics", 1.0),
        row("BBB", "integrated_freight_and_logistics", 2.0),
        row("CCC", "trucking", 100.0),
        row("DDD", "trucking", 200.0),
    ]
    scored = build_directional_metric_scores(
        rows,
        definitions=ret_3m,
        policy=policy,
    )
    by_ticker = {item["ticker"]: item for item in scored}
    assert by_ticker["AAA"]["metric_score__ret_3m"] == 0.0
    assert by_ticker["BBB"]["metric_score__ret_3m"] == 100.0
    assert by_ticker["CCC"]["metric_score__ret_3m"] == 0.0
    assert by_ticker["DDD"]["metric_score__ret_3m"] == 100.0
    assert mean_reversion_score_field("ret_3m") not in by_ticker["AAA"]

    assert surface_freight_cohort_eligible(rows[0], policy)
    excluded = dict(rows[0], ticker="ZTO")
    assert not surface_freight_cohort_eligible(excluded, policy)
    assert policy["governance"]["membership_selection_uses_outcomes"] is False


def test_surface_freight_candidate_weights_are_bounded_and_normalized() -> None:
    root = Path(__file__).resolve().parents[2]
    policy = load_surface_freight_policy(
        root
        / "industrials"
        / "transportation"
        / "data"
        / "transportation_surface_freight_research_policy.yaml"
    )
    selected = [
        {"metric_id": "fcf_yield", "component": "valuation", "selection_strength": 0.08},
        {"metric_id": "capex_to_revenue", "component": "capital_risk", "selection_strength": 0.07},
        {"metric_id": "asset_turnover", "component": "operating_efficiency", "selection_strength": 0.06},
    ]
    reversion = [
        {"metric_id": "relative_strength_3m", "selection_strength": 0.05}
    ]
    candidates = train_derived_candidate_registry(
        selected,
        policy=policy,
        mean_reversion_metrics=reversion,
    )
    assert set(candidates) == {
        "train_ic_equal",
        "train_ic_proportional",
        "train_ic_component_balanced",
        "fundamental_plus_mean_reversion_equal",
        "fundamental_plus_mean_reversion_bounded",
    }
    for weights in candidates.values():
        assert sum(weights.values()) == pytest.approx(1.0)
        assert max(weights.values()) <= 0.35 + 1e-12
    bounded = candidates["fundamental_plus_mean_reversion_bounded"]
    assert bounded[mean_reversion_score_field("relative_strength_3m")] == pytest.approx(0.30)


def test_v2_score_policy_freezes_24_names_and_three_component_candidates() -> None:
    root = Path(__file__).resolve().parents[2]
    policy = load_surface_freight_score_policy(
        root
        / "industrials"
        / "transportation"
        / "data"
        / "transportation_surface_freight_score_policy_v2.yaml"
    )
    assert len(policy["eligible_tickers"]) == 24
    assert set(policy["score_construction"]["retained_specialized_metrics"]) == {
        "operating_ratio"
    }
    all_candidates = candidate_registry_from_policy(
        policy, positioning_enabled=True
    )
    no_positioning = candidate_registry_from_policy(
        policy, positioning_enabled=False
    )
    assert set(all_candidates) == {
        "surface_balanced",
        "surface_quality_efficiency",
        "surface_balanced_positioning",
    }
    assert set(no_positioning) == {
        "surface_balanced",
        "surface_quality_efficiency",
    }
    assert all(sum(weights.values()) == pytest.approx(1.0) for weights in all_candidates.values())


def test_v2_fixed_denominator_neutralizes_missing_optional_metric() -> None:
    root = Path(__file__).resolve().parents[2]
    policy = load_surface_freight_score_policy(
        root
        / "industrials"
        / "transportation"
        / "data"
        / "transportation_surface_freight_score_policy_v2.yaml"
    )
    row = {
        "ticker": "ARCB",
        "economic_peer_group": "trucking",
        "metric_score__operating_margin": 80.0,
        "metric_score__fcf_margin": 60.0,
    }
    components, coverage = build_surface_component_scores(row, policy=policy)
    assert components["quality_score"] == pytest.approx(
        0.40 * 80.0 + 0.40 * 60.0 + 0.20 * 50.0
    )
    assert coverage["quality"] == {"observed": 2, "applicable": 3}
    assert components["operating_efficiency_score"] == pytest.approx(50.0)
    assert coverage["operating_efficiency"] == {"observed": 0, "applicable": 2}


def test_v5_cohort_policies_are_disjoint_and_metric_domain_isolated() -> None:
    root = Path(__file__).resolve().parents[2]
    surface = load_cohort_score_policy(
        root
        / "industrials"
        / "transportation"
        / "data"
        / "transportation_surface_freight_score_policy_v3.yaml"
    )
    tanker = load_cohort_score_policy(
        root
        / "industrials"
        / "transportation"
        / "data"
        / "transportation_tanker_score_policy_v1.yaml"
    )
    surface_tickers = set(surface["eligible_tickers"])
    tanker_tickers = set(tanker["eligible_tickers"])
    assert len(surface_tickers) == 24
    assert len(tanker_tickers) == 11
    assert not surface_tickers & tanker_tickers
    assert set(surface["score_construction"]["retained_specialized_metrics"]) == {
        "operating_ratio",
        "purchased_transportation_ratio",
        "freight_weight_per_shipment",
        "shipment_or_load_growth",
        "pricing_or_yield_growth",
    }
    arcb = {
        "ticker": "ARCB",
        "calibration_pool": "surface_freight_and_logistics",
        "risk_tier": "operating",
        "portfolio_role": "core_candidate",
        "economic_peer_group": "trucking",
    }
    assert cohort_score_eligible(arcb, surface)
    assert not cohort_score_eligible(arcb, tanker)
    assert metric_comparison_group_for_metric(
        arcb, surface, "operating_ratio"
    ) == "ltl_carriers"
    assert metric_comparison_group_for_metric(
        arcb, surface, "purchased_transportation_ratio"
    ) is None


def test_v5_historical_calibration_membership_is_pit_only() -> None:
    root = Path(__file__).resolve().parents[2]
    surface = load_cohort_score_policy(
        root
        / "industrials"
        / "transportation"
        / "data"
        / "transportation_surface_freight_score_policy_v3.yaml"
    )
    tanker = load_cohort_score_policy(
        root
        / "industrials"
        / "transportation"
        / "data"
        / "transportation_tanker_score_policy_v1.yaml"
    )
    ksu = {
        "ticker": "KSU",
        "asof_date": "2021-06-30",
        "calibration_cohort": "surface_freight_and_logistics",
        "calibration_use": "historical_research",
        "_score_membership_mode": "pit",
    }
    assert cohort_score_eligible(ksu, surface)
    assert metric_comparison_group(ksu, surface) == "rail_networks"
    assert not cohort_score_eligible({**ksu, "_score_membership_mode": "current"}, surface)
    assert not cohort_score_eligible({**ksu, "asof_date": "2022-01-31"}, surface)

    osg = {
        "ticker": "OSG",
        "asof_date": "2023-06-30",
        "calibration_cohort": "marine_shipping_and_maritime",
        "calibration_use": "historical_research",
        "_score_membership_mode": "pit",
    }
    assert cohort_score_eligible(osg, tanker)
    assert metric_comparison_group(osg, tanker) == "oil_tankers"
    assert "NNA" not in tanker["historical_calibration_only"]
    assert tanker["excluded_tickers"]["NNA"].startswith(
        "insufficient_required_metric_history"
    )


def test_scoring_context_propagates_asof_to_historical_policy_gate() -> None:
    from industrials.transportation.scoring import _member_scoring_context

    root = Path(__file__).resolve().parents[2]
    surface = load_cohort_score_policy(
        root
        / "industrials"
        / "transportation"
        / "data"
        / "transportation_surface_freight_score_policy_v3.yaml"
    )
    member = {
        "ticker": "KSU",
        "calibration_cohort": "surface_freight_and_logistics",
        "calibration_use": "historical_research",
    }
    context = _member_scoring_context(
        member, asof="2021-06-30", membership_mode="pit"
    )
    assert context["asof_date"] == "2021-06-30"
    assert cohort_score_eligible(context, surface)


def test_v2_research_and_production_metric_entrypoints_are_identical() -> None:
    root = Path(__file__).resolve().parents[2]
    policy = load_surface_freight_score_policy(
        root
        / "industrials"
        / "transportation"
        / "data"
        / "transportation_surface_freight_score_policy_v2.yaml"
    )
    _, definitions = load_metric_registry(
        root
        / "industrials"
        / "transportation"
        / "data"
        / "transportation_metric_registry.yaml"
    )
    definitions = [item for item in definitions if item.metric_id == "ret_3m"]

    def row(ticker: str, peer: str, value: float) -> dict[str, object]:
        return {
            "asof_date": "2026-07-30",
            "ticker": ticker,
            "industry": "Trucking" if peer == "trucking" else "Integrated Freight & Logistics",
            "calibration_cohort": "surface_freight_and_logistics",
            "calibration_pool": "surface_freight_and_logistics",
            "economic_peer_group": peer,
            "risk_tier": "operating",
            "portfolio_role": "core_candidate",
            "metric_values_json": json.dumps({"ret_3m": value}),
            "metric_status_json": json.dumps({"ret_3m": "REPORTED"}),
        }

    rows = [
        row("ARCB", "trucking", 1.0),
        row("CVLG", "trucking", 2.0),
        row("CHRW", "integrated_freight_and_logistics", 100.0),
        row("EXPD", "integrated_freight_and_logistics", 200.0),
    ]
    production = score_surface_metric_percentiles(
        rows, definitions=definitions, policy=policy
    )
    research = build_directional_metric_scores(
        rows, definitions=definitions, policy=policy
    )
    assert {
        row["ticker"]: row["metric_score__ret_3m"] for row in production
    } == {
        row["ticker"]: row["metric_score__ret_3m"] for row in research
    }


def test_pit_share_source_audit_excludes_future_facts() -> None:
    payload = {
        "facts": {
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {
                        "shares": [
                            {
                                "end": "2025-12-31",
                                "filed": "2026-02-20",
                                "form": "10-K",
                                "accn": "one",
                                "val": 100,
                            },
                            {
                                "end": "2026-06-30",
                                "filed": "2026-08-01",
                                "form": "10-Q",
                                "accn": "future",
                                "val": 110,
                            },
                        ]
                    }
                }
            }
        }
    }
    result = inspect_companyfacts_share_sources(payload, asof=date(2026, 7, 30))
    assert result["share_source_kind"] == "primary"
    assert result["usable_fact_count"] == 1
    assert result["last_filed_date"] == "2026-02-20"
    assert result["foreign_reporting_flag"] == 0


def test_share_conversion_registry_is_effective_dated_and_fail_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "conversions.csv"
    path.write_text(
        "ticker,effective_from,effective_to,listing_instrument,underlying_shares_per_traded_security,review_status,source_url,notes\n"
        "XYZ,2019-01-02,2023-12-31,ADR,2,REVIEWED_ADR,https://example.test/old,old\n"
        "XYZ,2024-01-01,,ADR_OR_ADS_REVIEW,,PENDING_REVIEW,,pending\n",
        encoding="utf-8",
    )
    conversions = load_share_conversions(path)
    old = resolve_share_conversion("XYZ", asof=date(2020, 1, 1), conversions=conversions)
    current = resolve_share_conversion("XYZ", asof=date(2026, 7, 30), conversions=conversions)
    assert old is not None and old.underlying_shares_per_traded_security == 2.0
    assert current is not None and current.review_status == "PENDING_REVIEW"

    malformed = tmp_path / "malformed.csv"
    malformed.write_text(
        "ticker,effective_from,effective_to,listing_instrument,underlying_shares_per_traded_security,review_status,source_url,notes\n"
        "XYZ,2019-01-02,,ADR,2,REVIEWED_ADR,,missing source\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="requires ratio and source_url"):
        load_share_conversions(malformed)


def test_valuation_summary_separates_audit_completion_from_rebuild_readiness(
    tmp_path: Path,
) -> None:
    rows = [
        {
            "ticker": "AAA",
            "required_for_rebuild": "1",
            "readiness_status": "READY",
            "disposition": "READY_SEC_POINT_IN_TIME_SHARES",
        },
        {
            "ticker": "BBB",
            "required_for_rebuild": "1",
            "readiness_status": "BLOCKED",
            "disposition": "REVIEW_SHARE_CONVERSION",
        },
        {
            "ticker": "OLD",
            "required_for_rebuild": "0",
            "readiness_status": "BLOCKED",
            "disposition": "MISSING_COMPANYFACTS",
        },
    ]
    summary = summarize_audit(rows)
    assert summary["valuation_rebuild_readiness"] == "FAIL"
    assert summary["blocked_required_tickers"] == ["BBB"]
    assert summary["blocked_required_ticker_count"] == 1
    assert companyfacts_path(tmp_path, "0000012345") == tmp_path / "CIK0000012345.json"


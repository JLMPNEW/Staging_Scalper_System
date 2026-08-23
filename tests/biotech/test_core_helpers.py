from __future__ import annotations

from datetime import date
from pathlib import Path

from biotech_index.core.biotech_taxonomy import classify_biotech_cohort
from biotech_index.core.config import normalize_string_list
from biotech_index.core.http_cache import CachedHttpClient
from biotech_index.core.market_policy import select_latest_rows_by_source_priority
from biotech_index.core.report_inputs import (
    resolve_dated_report_input_csv,
    resolve_market_snapshot_universe_csv,
)
from biotech_index.core.scoring_math import (
    score_commercial_entry_quality,
    score_commercial_expected_return_overlay,
    score_commercial_overextension,
)


def test_normalize_string_list_splits_cli_delimiters_and_drops_empty_values() -> None:
    assert normalize_string_list(" yahoo_adjusted, interactive_brokers | manual ; ", ["default"]) == [
        "yahoo_adjusted",
        "interactive_brokers",
        "manual",
    ]
    assert normalize_string_list([" keep ", None, "", "review"], ["default"]) == ["keep", "review"]
    assert normalize_string_list(None, ["keep", "review"]) == ["keep", "review"]


def test_market_snapshot_universe_uses_requested_date_when_prices_roll_back(tmp_path: Path) -> None:
    report_root = tmp_path / "reports"
    prior = report_root / "20260821" / "ctgov_final_scoring_universe.csv"
    requested = report_root / "20260822" / "ctgov_final_scoring_universe.csv"
    prior.parent.mkdir(parents=True)
    requested.parent.mkdir(parents=True)
    prior.write_text("ticker\nOLD\n", encoding="utf-8")
    requested.write_text("ticker\nNEW\n", encoding="utf-8")

    resolved = resolve_market_snapshot_universe_csv(
        report_root / "ctgov_final_scoring_universe.csv",
        base_output_dir=report_root,
        requested_asof_date="2026-08-22",
        effective_market_asof_date="2026-08-21",
    )

    assert resolved == requested


def test_dated_report_input_prevents_newer_root_catalyst_lookahead(tmp_path: Path) -> None:
    report_root = tmp_path / "reports"
    root = report_root / "forward_catalyst_calendar.csv"
    dated = report_root / "20260821" / root.name
    dated.parent.mkdir(parents=True)
    root.write_text("ticker,filing_date\nAAA,2026-08-22\n", encoding="utf-8")
    dated.write_text("ticker,filing_date\nAAA,2026-08-21\n", encoding="utf-8")

    resolved = resolve_dated_report_input_csv(
        root,
        base_output_dir=report_root,
        asof_date="2026-08-21",
    )

    assert resolved == dated


def test_market_source_selection_ignores_future_rows_and_normalizes_sources() -> None:
    rows = [
        {"company_id": 1, "asof_date": "2026-05-09", "source": "yahoo_adjusted", "value": 99},
        {"company_id": 1, "asof_date": "2026-05-08", "source": " interactive_brokers ", "value": 10},
        {"company_id": 2, "asof_date": "2026-05-08", "source": "YAHOO_ADJUSTED", "value": 20},
        {"company_id": 2, "asof_date": "2026-05-08", "source": "interactive_brokers", "value": 11},
    ]

    selected = select_latest_rows_by_source_priority(
        rows,
        asof_date=date(2026, 5, 8),
        source_priority=["Yahoo_Adjusted", "interactive_brokers"],
        max_staleness_days=0,
    )

    assert selected[1]["value"] == 10
    assert selected[2]["value"] == 20


def test_taxonomy_going_concern_overlay_uses_shared_status_sets() -> None:
    for status in ("going_concern_confirmed", "substantial_doubt", "hard", "warning"):
        classification = classify_biotech_cohort(
            payload={
                "ctgov": {"verified_qualifying_active_trial_count": 1},
                "financial_survival": {"data_quality": "high", "going_concern_status": status},
                "sec_and_liquidity": {},
            },
            commercial={},
            forward_guidance={},
            diagnostics={},
        )

        assert "going_concern" in classification.overlays


def test_taxonomy_commercial_anchor_wins_primary_over_pipeline_overlay() -> None:
    classification = classify_biotech_cohort(
        payload={
            "ctgov": {
                "verified_qualifying_active_trial_count": 4,
                "active_phase3_trials": 1,
                "active_pivotal_trials": 1,
                "lead_phase2_3_active_trials": 1,
            },
            "sec_events": {"counts": {"pdufa_date": 1}},
            "financial_survival": {"data_quality": "high"},
            "sec_and_liquidity": {},
        },
        commercial={
            "commercial_stage_flag": 1,
            "profitable_flag": 1,
            "ttm_revenue": 1_000_000_000.0,
            "revenue_yoy_growth_pct": 0.20,
            "value_trap_score": 5.0,
        },
        forward_guidance={"latest_guidance_filing_date": "2026-05-08"},
        diagnostics={},
    )

    assert classification.primary_cohort == "commercial_profitable_quality_or_mature"
    assert classification.secondary_cohort == "late_clinical_pivotal_or_registrational"
    assert "commercial_with_major_pipeline_catalyst" in classification.overlays
    assert "late_clinical_overlay" in classification.overlays
    assert classification.confidence <= 88.0


def test_taxonomy_forward_profitable_commercial_anchor_beats_late_pipeline() -> None:
    classification = classify_biotech_cohort(
        payload={
            "ctgov": {
                "verified_qualifying_active_trial_count": 4,
                "active_phase3_trials": 2,
                "active_pivotal_trials": 1,
                "lead_phase2_3_active_trials": 1,
            },
            "sec_events": {"counts": {"pdufa_date": 1, "regulatory_submission": 1}},
            "financial_survival": {"data_quality": "high"},
            "sec_and_liquidity": {},
        },
        commercial={
            "commercial_stage_flag": 1,
            "profitable_flag": 0,
            "ttm_revenue": 125_000_000.0,
            "revenue_yoy_growth_pct": 0.28,
            "value_trap_score": 5.0,
        },
        forward_guidance={"forward_profitability_flag": 1, "latest_guidance_filing_date": "2026-05-08"},
        diagnostics={},
    )

    assert classification.primary_cohort == "commercial_turnaround_or_unprofitable_growth"
    assert classification.secondary_cohort == "late_clinical_pivotal_or_registrational"
    assert "late_clinical_overlay" in classification.overlays
    assert classification.evidence["pipeline_clearly_dominates"] is False


def test_taxonomy_medtech_device_is_investible_primary_with_pipeline_overlay_cap() -> None:
    classification = classify_biotech_cohort(
        payload={
            "company_strategy_category": "diabetes_device",
            "ctgov": {
                "verified_qualifying_active_trial_count": 2,
                "active_phase3_trials": 1,
                "active_pivotal_trials": 1,
            },
            "financial_survival": {"data_quality": "high"},
            "sec_and_liquidity": {},
        },
        commercial={},
        forward_guidance={},
        diagnostics={},
    )

    assert classification.primary_cohort == "commercial_profitable_quality_or_mature"
    assert "medtech_device_strategy_category" in classification.reason_codes
    assert classification.confidence <= 86.0


def test_cached_json_null_cache_is_refetched(tmp_path, monkeypatch) -> None:
    client = CachedHttpClient(cache_dir=tmp_path, sleep_sec=0.0, timeout_sec=1.0, max_retries=1)
    url = "https://example.test/data"
    path = client.cache_path("json", url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("null", encoding="utf-8")
    calls = 0

    def fake_get_text(*, url: str, params: object, headers: dict[str, str]) -> str:
        nonlocal calls
        calls += 1
        return '{"ok": true}'

    try:
        monkeypatch.setattr(client, "_get_text", fake_get_text)

        assert client.fetch_json(namespace="json", url=url, ttl_hours=24.0) == {"ok": True}
        assert calls == 1
        assert path.read_text(encoding="utf-8") == '{"ok": true}'
    finally:
        client.close()


def test_commercial_entry_quality_rewards_investable_pullback() -> None:
    pullback = score_commercial_entry_quality(
        distance_from_52w_high_pct=-0.25,
        price_vs_200d_pct=0.04,
        return_3m_pct=0.10,
        relative_strength_3m_vs_xbi=0.18,
    )
    chased_high = score_commercial_entry_quality(
        distance_from_52w_high_pct=-0.01,
        price_vs_200d_pct=0.48,
        return_3m_pct=0.60,
        relative_strength_3m_vs_xbi=0.75,
    )

    assert pullback > 75.0
    assert chased_high < pullback


def test_commercial_overextension_flags_stretched_mature_names() -> None:
    stretched = score_commercial_overextension(
        distance_from_52w_high_pct=-0.01,
        price_vs_200d_pct=0.50,
        return_3m_pct=0.60,
        valuation_growth_mismatch_score=75.0,
        mature_defensive_score=80.0,
    )
    normal = score_commercial_overextension(
        distance_from_52w_high_pct=-0.25,
        price_vs_200d_pct=0.05,
        return_3m_pct=0.10,
        valuation_growth_mismatch_score=10.0,
        mature_defensive_score=15.0,
    )

    assert stretched > 70.0
    assert normal < stretched


def test_commercial_expected_return_overlay_penalizes_value_trap_setup() -> None:
    attractive = score_commercial_expected_return_overlay(
        commercial={
            "distance_from_52w_high_pct": -0.25,
            "price_vs_200d_pct": 0.04,
            "return_3m_pct": 0.10,
            "relative_strength_3m_vs_xbi": 0.18,
            "quality_adjusted_valuation_score": 75.0,
            "revenue_yoy_growth_pct": 0.18,
            "institutional_upside_capacity_score": 80.0,
            "commercial_value_score": 75.0,
            "value_trap_score": 5.0,
            "leverage_score": 80.0,
        },
        forward_guidance={
            "forward_revenue_growth_pct": 0.20,
            "forward_ebitda_margin_pct": 0.20,
            "quality_adjusted_guidance_score": 75.0,
        },
        momentum_score=65.0,
        risk_score=35.0,
        mature_defensive_score=10.0,
    )
    value_trap = score_commercial_expected_return_overlay(
        commercial={
            "distance_from_52w_high_pct": -0.02,
            "price_vs_200d_pct": 0.45,
            "return_3m_pct": 0.55,
            "relative_strength_3m_vs_xbi": 0.75,
            "quality_adjusted_valuation_score": 35.0,
            "revenue_yoy_growth_pct": -0.05,
            "institutional_upside_capacity_score": 35.0,
            "commercial_value_score": 35.0,
            "value_trap_score": 80.0,
            "leverage_score": 35.0,
        },
        forward_guidance={
            "forward_revenue_growth_pct": -0.02,
            "forward_ebitda_margin_pct": 0.03,
            "quality_adjusted_guidance_score": 35.0,
        },
        momentum_score=45.0,
        risk_score=55.0,
        mature_defensive_score=80.0,
    )

    assert attractive["commercial_expected_return_overlay_score"] > 65.0
    assert value_trap["commercial_expected_return_overlay_score"] < attractive[
        "commercial_expected_return_overlay_score"
    ]

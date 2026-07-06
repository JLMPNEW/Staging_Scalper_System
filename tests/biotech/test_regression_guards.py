from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from tests.biotech.conftest import load_script_module


def test_sec_event_worker_exception_does_not_write_parse_state(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_script_module("07_parse_sec_biotech_events.py", "sec_events_regression")
    replace_called = False

    def fail_detect(*_args: object, **_kwargs: object) -> list[object]:
        raise RuntimeError("worker smoke failure")

    def replace_events(*_args: object, **_kwargs: object) -> None:
        nonlocal replace_called
        replace_called = True

    monkeypatch.setattr(module, "detect_events", fail_detect)
    monkeypatch.setattr(module, "replace_events", replace_events)
    filing = module.FilingText(1, "TST", "Test Co", "0001", "2026-05-08", "8-K", "", "hash", "text")

    with pytest.raises(RuntimeError, match="worker smoke failure"):
        module.parse_filing_batch(
            sqlite3.connect(":memory:"),
            [filing],
            min_confidence=0.0,
            max_per_type=1,
            max_workers=2,
            parser_signature="smoke",
        )

    assert replace_called is False


def test_sec_event_incremental_clears_events_when_latest_document_text_is_missing() -> None:
    module = load_script_module("07_parse_sec_biotech_events.py", "sec_events_missing_text_cleanup")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE companies(company_id INTEGER PRIMARY KEY, ticker TEXT NOT NULL, company_name TEXT);
        CREATE TABLE sec_filings(
            company_id INTEGER NOT NULL,
            accession_nodash TEXT PRIMARY KEY,
            filing_date TEXT NOT NULL,
            form TEXT NOT NULL,
            text_hash TEXT
        );
        CREATE TABLE sec_filing_documents(
            document_id INTEGER PRIMARY KEY,
            accession_nodash TEXT NOT NULL,
            document_url TEXT NOT NULL,
            document_type TEXT NOT NULL,
            text_content TEXT,
            text_hash TEXT
        );
        CREATE TABLE sec_filing_latest_document(
            accession_nodash TEXT PRIMARY KEY,
            document_id INTEGER NOT NULL,
            document_url TEXT NOT NULL,
            document_type TEXT NOT NULL,
            text_hash TEXT NOT NULL,
            text_length INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE sec_events(
            event_id INTEGER PRIMARY KEY,
            company_id INTEGER NOT NULL,
            accession_nodash TEXT NOT NULL,
            filing_date TEXT NOT NULL,
            form TEXT NOT NULL,
            event_type TEXT NOT NULL
        );
        CREATE TABLE sec_event_parse_state(
            accession_nodash TEXT PRIMARY KEY,
            text_hash TEXT,
            parser_signature TEXT,
            parsed_at TEXT NOT NULL,
            event_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO companies(company_id, ticker, company_name) VALUES (1, 'TST', 'Test Therapeutics');
        INSERT INTO sec_filings(company_id, accession_nodash, filing_date, form, text_hash)
            VALUES (1, '0001', '2026-05-08', '10-Q', 'old-hash');
        INSERT INTO sec_filing_documents(document_id, accession_nodash, document_url, document_type, text_content, text_hash)
            VALUES (10, '0001', 'https://example.test/0001.txt', 'complete_submission_text', '', '');
        INSERT INTO sec_filing_latest_document(accession_nodash, document_id, document_url, document_type, text_hash, text_length)
            VALUES ('0001', 10, 'https://example.test/0001.txt', 'complete_submission_text', '', 0);
        INSERT INTO sec_events(event_id, company_id, accession_nodash, filing_date, form, event_type)
            VALUES (1, 1, '0001', '2026-05-08', '10-Q', 'pdufa_date');
        INSERT INTO sec_event_parse_state(accession_nodash, text_hash, parser_signature, parsed_at, event_count, created_at, updated_at)
            VALUES ('0001', 'old-hash', 'old-signature', '2026-05-08T00:00:00Z', 1, '2026-05-08T00:00:00Z', '2026-05-08T00:00:00Z');
        """
    )

    cleared = module.clear_stale_events_for_missing_document_text(
        conn,
        cutoff="2025-05-08",
        asof="2026-05-08",
        ticker_filter={"TST"},
        parser_signature="new-signature",
    )

    assert cleared == 1
    assert conn.execute("SELECT COUNT(*) FROM sec_events").fetchone()[0] == 0
    state = conn.execute("SELECT text_hash, parser_signature, event_count FROM sec_event_parse_state").fetchone()
    assert dict(state) == {"text_hash": "", "parser_signature": "new-signature", "event_count": 0}


def test_sec_event_incremental_can_skip_parser_signature_only_reparse() -> None:
    module = load_script_module("07_parse_sec_biotech_events.py", "sec_events_skip_signature_reparse")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE companies(company_id INTEGER PRIMARY KEY, ticker TEXT NOT NULL, company_name TEXT);
        CREATE TABLE sec_filings(
            company_id INTEGER NOT NULL,
            accession_nodash TEXT PRIMARY KEY,
            filing_date TEXT NOT NULL,
            form TEXT NOT NULL,
            text_hash TEXT
        );
        CREATE TABLE sec_filing_documents(
            document_id INTEGER PRIMARY KEY,
            accession_nodash TEXT NOT NULL,
            document_url TEXT NOT NULL,
            document_type TEXT NOT NULL,
            text_content TEXT,
            text_hash TEXT
        );
        CREATE TABLE sec_filing_latest_document(
            accession_nodash TEXT PRIMARY KEY,
            document_id INTEGER NOT NULL,
            document_url TEXT NOT NULL,
            document_type TEXT NOT NULL,
            text_hash TEXT NOT NULL,
            text_length INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE sec_event_parse_state(
            accession_nodash TEXT PRIMARY KEY,
            text_hash TEXT,
            parser_signature TEXT,
            parsed_at TEXT NOT NULL,
            event_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO companies(company_id, ticker, company_name) VALUES (1, 'TST', 'Test Therapeutics');
        INSERT INTO sec_filings(company_id, accession_nodash, filing_date, form, text_hash)
            VALUES (1, '0001', '2026-05-08', '10-Q', 'same-hash');
        INSERT INTO sec_filing_documents(document_id, accession_nodash, document_url, document_type, text_content, text_hash)
            VALUES (10, '0001', 'https://example.test/0001.txt', 'complete_submission_text', 'valid filing text', 'same-hash');
        INSERT INTO sec_filing_latest_document(accession_nodash, document_id, document_url, document_type, text_hash, text_length)
            VALUES ('0001', 10, 'https://example.test/0001.txt', 'complete_submission_text', 'same-hash', 17);
        INSERT INTO sec_event_parse_state(accession_nodash, text_hash, parser_signature, parsed_at, event_count, created_at, updated_at)
            VALUES ('0001', 'same-hash', 'old-signature', '2026-05-08T00:00:00Z', 1, '2026-05-08T00:00:00Z', '2026-05-08T00:00:00Z');
        """
    )

    skipped = module.load_filing_texts_to_parse(
        conn,
        cutoff="2025-05-08",
        asof="2026-05-08",
        ticker_filter={"TST"},
        max_filings=0,
        offset=0,
        reparse_signature_mismatch=False,
        parser_signature="new-signature",
    )
    strict = module.load_filing_texts_to_parse(
        conn,
        cutoff="2025-05-08",
        asof="2026-05-08",
        ticker_filter={"TST"},
        max_filings=0,
        offset=0,
        reparse_signature_mismatch=True,
        parser_signature="new-signature",
    )

    assert skipped == []
    assert [filing.accession_nodash for filing in strict] == ["0001"]


def test_forward_guidance_worker_exception_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_script_module("19_parse_forward_guidance.py", "forward_guidance_regression")

    def fail_detect(*_args: object, **_kwargs: object) -> list[object]:
        raise RuntimeError("guidance smoke failure")

    monkeypatch.setattr(module, "detect_guidance", fail_detect)
    filing = module.FilingText(
        company_id=1,
        ticker="TST",
        company_name="Test Co",
        accession_nodash="0001",
        filing_date="2026-05-08",
        form="10-Q",
        archive_url="",
        document_type="complete_submission_text",
        text_content="text",
        text_hash="hash",
    )

    with pytest.raises(RuntimeError, match="guidance smoke failure"):
        module.parse_guidance_records(
            [filing],
            asof_date=date(2026, 5, 8),
            min_confidence=0.0,
            max_windows_per_filing=1,
            max_workers=2,
        )


def test_score_rows_missing_risk_score_raw_uses_default() -> None:
    module = load_script_module("11_score_biotech_index.py", "score_biotech_regression")
    rows = [
        {
            "asof_date": "2026-05-08",
            "company_id": 1,
            "ticker": "TST",
            "company_name": "Test Co",
            "catalyst_score_raw": 50.0,
            "credibility_score_raw": 50.0,
            "feature_json": "{}",
        }
    ]
    config = {
        "biotech_scoring": {
            "use_investment_score": False,
            "data_quality_adjustment": {"enabled": False},
            "weights": {
                "catalyst": 0.45,
                "credibility": 0.30,
                "financial_quality": 0.15,
                "momentum": 0.10,
                "risk_penalty": 0.35,
            },
        }
    }

    scored = module.score_rows(rows, config, commercial_by_company={}, forward_by_company={})

    assert scored[0]["risk_score"] == 0.0


def test_companyfacts_fetch_reuses_supplied_http_client(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_script_module("15_sync_sec_companyfacts_history.py", "companyfacts_regression")

    class FakeHttp:
        def __init__(self) -> None:
            self.calls = 0

        def fetch_json(self, **_kwargs: object) -> dict[str, object]:
            self.calls += 1
            return {"facts": {"ok": True}}

    fake_http = FakeHttp()
    monkeypatch.setattr(
        module,
        "parse_observations",
        lambda _payload, *, company, cutoff, asof=None: [{"company_id": company.company_id, "concept": f"x:{cutoff.isoformat()}"}],
    )
    monkeypatch.setattr(
        module,
        "normalize_rows",
        lambda _observations, company_id: [{"company_id": company_id, "period_end": "2026-03-31"}],
    )
    company = module.Company(1, "TST", "1234567890", "Test Co")

    result = module.fetch_companyfacts_result(
        company,
        url_template="https://example.test/CIK{cik}.json",
        headers={},
        cache_dir=Path("unused"),
        ttl_hours=1.0,
        sleep_sec=0.0,
        timeout_sec=1.0,
        max_retries=1,
        throttle=module.HostThrottle(),
        cutoff=date(2025, 1, 1),
        latest_source_filing_date="2026-05-08",
        http=fake_http,
    )

    assert fake_http.calls == 1
    assert result.error == ""
    assert result.normalized[0]["company_id"] == 1


def test_governance_invalid_date_returns_none() -> None:
    module = load_script_module("20_build_governance_event_features.py", "governance_regression")

    assert module.parse_date("not-a-date") is None


def test_multibagger_missing_layer_helper_identifies_missing_tickers() -> None:
    module = load_script_module("21_build_multibagger_features.py", "multibagger_features_regression")
    base_rows = [{"company_id": 1, "ticker": "AAA"}, {"company_id": 2, "ticker": "BBB"}]

    assert module.missing_layer_tickers(base_rows, {1: {"asof_date": "2026-05-08"}}) == ["BBB"]


def test_biotech_report_allocation_rows_do_not_admit_avoid_discovery_names() -> None:
    module = load_script_module("12_publish_biotech_reports.py", "publish_biotech_reports_regression")
    settings = {
        "high_confidence_score_min": 55.0,
        "allocation_rank_max": 10.0,
        "allocation_score_min": 50.0,
        "research_rank_max": 20.0,
        "research_score_min": 45.0,
    }
    rows = [
        {
            "rank": 1,
            "ticker": "IMVT",
            "allocation_bucket": "avoid",
            "allocation_opportunity_score": 32.0,
            "allocation_risk_score": 80.0,
            "discovery_opportunity_score": 75.0,
            "biotech_cohort_investible_flag": 1.0,
            "rank_quality_cap_vetoed": 0.0,
        },
        {
            "rank": 7,
            "ticker": "HALO",
            "allocation_bucket": "watchlist",
            "allocation_opportunity_score": 72.0,
            "allocation_risk_score": 25.0,
            "discovery_opportunity_score": 72.0,
            "biotech_cohort_investible_flag": 1.0,
            "rank_quality_cap_vetoed": 0.0,
        },
    ]

    allocation_rows = module.build_allocation_ranked_rows(rows, settings)

    assert [row["ticker"] for row in allocation_rows] == ["HALO"]
    assert allocation_rows[0]["rank"] == 1
    assert allocation_rows[0]["production_rank"] == 7
    assert allocation_rows[0]["rank_purpose"] == "allocation"
    assert allocation_rows[0]["rank_source"] == "allocation_opportunity_score"


def test_biotech_discovery_avoid_rows_are_research_only() -> None:
    module = load_script_module("12_publish_biotech_reports.py", "publish_biotech_discovery_action_regression")
    settings = {
        "high_confidence_score_min": 55.0,
        "allocation_rank_max": 10.0,
        "allocation_score_min": 50.0,
        "research_rank_max": 20.0,
        "research_score_min": 45.0,
    }
    row = {
        "ticker": "IMVT",
        "allocation_rank": "",
        "discovery_rank": 3,
        "allocation_bucket": "avoid",
        "allocation_opportunity_score": 32.0,
        "discovery_opportunity_score": 76.0,
        "rank_quality_cap_vetoed": 0.0,
    }

    labeled = module.apply_discovery_action_framework(row, settings)

    assert labeled["discovery_action_tier"] == "research_only_allocation_avoid"
    assert labeled["dual_consensus_tier"] == "research_only_allocation_avoid"
    assert labeled["allocation_candidate_flag"] == 0
    assert labeled["discovery_candidate_flag"] == 1
    assert labeled["research_watchlist_flag"] == 1


def test_biotech_ranking_validation_accepts_clean_split_outputs() -> None:
    module = load_script_module("12_publish_biotech_reports.py", "publish_biotech_validation_pass_regression")
    allocation_rows = [
        {
            "ticker": "HALO",
            "allocation_bucket": "watchlist",
            "rank_source": "allocation_opportunity_score",
            "rank_quality_cap_vetoed": 0.0,
            "biotech_cohort_investible_flag": 1.0,
            "biotech_primary_cohort": "commercial_profitable_quality_or_mature",
            "ttm_revenue": 1_000_000_000.0,
            "profitable_flag": 1.0,
        }
    ]
    discovery_rows = [
        {
            "ticker": "IMVT",
            "allocation_bucket": "avoid",
            "rank_source": "discovery_opportunity_score",
            "discovery_action_tier": "research_only_allocation_avoid",
            "dual_consensus_tier": "research_only_allocation_avoid",
        }
    ]

    assert module.validate_ranked_outputs(
        allocation_rows=allocation_rows,
        discovery_rows=discovery_rows,
        config={},
    ) == []


def test_biotech_ranking_validation_flags_allocation_and_discovery_leakage() -> None:
    module = load_script_module("12_publish_biotech_reports.py", "publish_biotech_validation_fail_regression")
    allocation_rows = [
        {
            "ticker": "BAD1",
            "allocation_bucket": "avoid",
            "rank_source": "discovery_opportunity_score",
            "rank_quality_cap_vetoed": 1.0,
            "biotech_cohort_investible_flag": 0.0,
        },
        {
            "ticker": "BAD2",
            "allocation_bucket": "watchlist",
            "rank_source": "allocation_opportunity_score",
            "rank_quality_cap_vetoed": 0.0,
            "biotech_cohort_investible_flag": 1.0,
            "biotech_primary_cohort": "late_clinical_pivotal_or_registrational",
            "ttm_revenue": 500_000_000.0,
            "profitable_flag": 1.0,
        },
    ]
    discovery_rows = [
        {
            "ticker": "BAD3",
            "allocation_bucket": "avoid",
            "rank_source": "production_rank_score",
            "discovery_action_tier": "discovery_candidate",
            "dual_consensus_tier": "discovery_candidate",
        }
    ]
    config = {
        "biotech_scoring": {
            "ranking_validation": {
                "max_commercial_late_clinical_top20": 0,
            }
        }
    }

    errors = module.validate_ranked_outputs(
        allocation_rows=allocation_rows,
        discovery_rows=discovery_rows,
        config=config,
    )

    assert "allocation_contains_avoid_bucket:BAD1" in errors
    assert "allocation_contains_rank_cap_vetoed:BAD1" in errors
    assert "allocation_contains_non_investible_cohort:BAD1" in errors
    assert "allocation_rank_source_not_allocation_score:BAD1" in errors
    assert "discovery_rank_source_not_discovery_score:BAD3" in errors
    assert "discovery_avoid_not_research_only:BAD3" in errors
    assert "discovery_avoid_dual_tier_not_research_only:BAD3" in errors
    assert any(error.startswith("too_many_commercial_names_primary_late_clinical:1:BAD2") for error in errors)


def test_production_rank_source_requires_explicit_discovery_opt_in() -> None:
    module = load_script_module("11_score_biotech_index.py", "score_biotech_rank_source_regression")
    config: dict[str, Any] = {
        "biotech_scoring": {
            "risk_mode_routing": {
                "production_score_source": "routed_discovery",
            }
        }
    }

    with pytest.raises(ValueError, match="expected opportunity_score"):
        module.production_rank_score_field(config)

    config["biotech_scoring"]["risk_mode_routing"]["allow_discovery_as_production_rank"] = True

    assert module.production_rank_score_field(config) == "discovery_opportunity_score"


def test_production_rank_blocked_filters_avoid_rankcap_and_noninvestible() -> None:
    module = load_script_module("11_score_biotech_index.py", "score_biotech_rank_block_regression")

    assert module.production_rank_blocked({"allocation_bucket": "avoid"}, apply_core_veto_to_rank=False)
    assert module.production_rank_blocked({"rank_quality_cap_vetoed": 1.0}, apply_core_veto_to_rank=False)
    assert module.production_rank_blocked(
        {"biotech_cohort_investible_flag": 0.0},
        apply_core_veto_to_rank=False,
    )
    assert module.production_rank_blocked(
        {"core_structural_veto_flag": 1.0},
        apply_core_veto_to_rank=True,
    )
    assert not module.production_rank_blocked(
        {"allocation_bucket": "watchlist", "allocation_opportunity_score": 70.0},
        apply_core_veto_to_rank=False,
    )


def test_ctgov_shared_study_merge_is_deterministic_on_conflict() -> None:
    module = load_script_module("03_sync_ctgov_trials.py", "ctgov_sync_merge_regression")
    first = module.SyncResult(
        company_id=1,
        ticker="AAA",
        alias_count=1,
        search_count=1,
        study_count=1,
        studies={"NCT00000001": {"protocolSection": {"identificationModule": {"nctId": "NCT00000001"}}, "value": 1}},
    )
    second = module.SyncResult(
        company_id=2,
        ticker="BBB",
        alias_count=1,
        search_count=1,
        study_count=1,
        studies={"NCT00000001": {"protocolSection": {"identificationModule": {"nctId": "NCT00000001"}}, "value": 2}},
    )

    merged = module.merge_unique_studies([first, second])

    assert merged["NCT00000001"]["value"] == 1


def test_company_master_preserves_first_source_screen_decision_on_update_and_deactivate() -> None:
    module = load_script_module("02_build_company_master.py", "company_master_source_decision_regression")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    module.init_db(conn)

    def company(decision: str) -> object:
        return module.ScreenCompany(
            ticker="AAA",
            cik="0000000001",
            company_name="AAA Therapeutics",
            exchange="NASDAQ",
            sector="Healthcare",
            industry="Biotechnology",
            industry_aggregate="Healthcare",
            security_type="Common Stock",
            is_primary_listing="true",
            listing_status="active",
            country="US",
            currency="USD",
            manual_include="",
            manual_exclude="",
            manual_review="",
            notes="",
            decision=decision,
            reason_codes=decision,
            match_type="screen",
            source="screen",
        )

    company_id = module.upsert_company(conn, company("keep"), active_decisions={"keep", "review"})
    module.upsert_company(conn, company("review"), active_decisions={"keep", "review"})
    module.deactivate_absent_companies(
        conn,
        present_tickers={"ZZZ"},
        protected_tickers=set(),
        source_file=Path("screen.csv"),
        run_id=1,
    )
    row = conn.execute(
        "SELECT universe_status, source_screen_decision, is_active FROM companies WHERE company_id = ?",
        (company_id,),
    ).fetchone()

    assert row["universe_status"] == "remove"
    assert row["source_screen_decision"] == "keep"
    assert row["is_active"] == 0


def test_missing_trial_update_age_does_not_apply_stale_trial_penalty() -> None:
    module = load_script_module("10_build_biotech_features.py", "biotech_features_stale_update_regression")

    def compute(days_since_last_update: object) -> dict[str, object]:
        return module.compute_feature_row(
            universe_row={
                "ticker": "AAA",
                "company_name": "AAA Therapeutics",
                "verified_qualifying_active_trial_count": 1,
                "active_lead_sponsor_trials": 1,
                "phase2_3_active_trials": 1,
                "days_since_last_update": days_since_last_update,
            },
            screen_row=None,
            evidence={
                "active_lead_phase2_3_trials": 1,
                "effective_phase2_3_trials": 1,
                "core_pipeline_quality_score": 10.0,
            },
            company_id=1,
            asof_date=date(2026, 5, 8),
            min_liquidity_addv20=1_000_000.0,
            low_liquidity_addv20=2_000_000.0,
            strong_liquidity_addv20=10_000_000.0,
            category_overrides={},
            going_concern_source_priority=["db", "csv"],
            survival_score_blend_weight=0.55,
            core_pipeline_quality_multiplier=0.18,
            sec_catalyst_event_weights={
                "pdufa_date": 18.0,
                "nda_bla_accepted": 16.0,
                "regulatory_submission": 7.0,
                "endpoint_met": 10.0,
                "clinical_update_positive": 5.0,
            },
            risk_decomposition_settings=module.load_risk_decomposition_settings({}),
            sec_catalyst_recency_decay_enabled=True,
            sec_catalyst_half_life_days=90.0,
            market=None,
            survival=None,
            sec_events=None,
        )

    missing = compute("")
    stale = compute("400")

    assert float(str(missing["catalyst_score_raw"])) > float(str(stale["catalyst_score_raw"]))
    payload = json.loads(str(missing["feature_json"]))
    assert payload["ctgov"]["has_trial_update_age"] is False


def test_feature_builder_emits_item5_to_7_shadow_signals() -> None:
    module = load_script_module("10_build_biotech_features.py", "biotech_features_shadow_signal_regression")

    row = module.compute_feature_row(
        universe_row={
            "ticker": "CNSX",
            "company_name": "CNSX Therapeutics",
            "verified_qualifying_active_trial_count": 2,
            "active_lead_sponsor_trials": 1,
            "phase2_3_active_trials": 2,
            "primary_trial_title": "Phase 2 study in Alzheimer disease",
            "days_since_last_update": "45",
        },
        screen_row=None,
        evidence={
            "active_lead_phase2_3_trials": 1,
            "active_phase2_trials": 1,
            "effective_phase2_3_trials": 2,
            "core_pipeline_quality_score": 12.0,
            "top_ncts": [{"title": "Alzheimer disease proof of concept"}],
        },
        company_id=1,
        asof_date=date(2026, 6, 1),
        min_liquidity_addv20=1_000_000.0,
        low_liquidity_addv20=2_000_000.0,
        strong_liquidity_addv20=10_000_000.0,
        category_overrides={},
        going_concern_source_priority=["db", "csv"],
        survival_score_blend_weight=0.55,
        core_pipeline_quality_multiplier=0.18,
        sec_catalyst_event_weights={
            "pdufa_date": 18.0,
            "nda_bla_accepted": 16.0,
            "regulatory_submission": 7.0,
            "endpoint_met": 10.0,
            "clinical_update_positive": 5.0,
        },
        risk_decomposition_settings=module.load_risk_decomposition_settings({}),
        sec_catalyst_recency_decay_enabled=True,
        sec_catalyst_half_life_days=90.0,
        market=None,
        survival=None,
        sec_events=None,
        indication_success_settings=module.load_indication_success_settings(
            {
                "biotech_features": {
                    "indication_success_weighting": {
                        "enabled": True,
                        "apply_to_catalyst": False,
                    }
                }
            }
        ),
        forward_catalyst={"event_date": "2026-07-01", "event_type": "phase2_topline", "days_until": 30, "confidence": 0.80},
        short_interest={"short_interest_pct_float": 0.18, "days_to_cover": 6},
        institutional_ownership={"institutional_ownership_delta_pct": 0.08},
    )

    assert row["indication_success_area"] == "cns"
    assert float(row["indication_success_multiplier"]) < 1.0
    assert float(row["indication_weighted_phase2_3_component"]) > 0.0
    assert float(row["forward_catalyst_score"]) > 50.0
    assert float(row["short_interest_signal_score"]) > 60.0
    assert float(row["institutional_accumulation_score"]) > 70.0
    payload = json.loads(str(row["feature_json"]))
    assert payload["shadow_signals"]["indication_success"]["applied_to_catalyst"] is False


def test_calibration_forward_returns_include_alpha_adjusted_objectives() -> None:
    module = load_script_module("28_calibrate_biotech_opportunity.py", "calibration_alpha_return_regression")
    rows = [
        {"ticker": "AAA", "asof_date": "2026-01-01"},
        {"ticker": "BBB", "asof_date": "2026-01-01"},
    ]
    bars_by_ticker = {
        "AAA": [module.Bar(date(2026, 1, 2), 100.0), module.Bar(date(2026, 1, 5), 110.0)],
        "BBB": [module.Bar(date(2026, 1, 2), 100.0), module.Bar(date(2026, 1, 5), 90.0)],
    }
    benchmark_bars = [module.Bar(date(2026, 1, 2), 100.0), module.Bar(date(2026, 1, 5), 105.0)]

    module.add_forward_returns(
        rows,
        bars_by_ticker,
        [1],
        round_trip_cost_bps=0.0,
        next_bar_entry=True,
        benchmark_ticker="XBI",
        benchmark_bars=benchmark_bars,
    )

    params = module.CalibrationParams(alpha_adjustment_enabled=True, benchmark_ticker="XBI", return_objective="benchmark_alpha")
    assert module.objective_return_key(1, params) == "fwd_1d_net_benchmark_alpha_return"
    assert rows[0]["fwd_1d_net_return"] == pytest.approx(0.10)
    assert rows[0]["fwd_1d_net_benchmark_alpha_return"] == pytest.approx(0.05)
    assert rows[0]["fwd_1d_equal_weight_net_return"] == pytest.approx(0.0)
    assert rows[0]["fwd_1d_net_equal_weight_alpha_return"] == pytest.approx(0.10)


def test_delisted_price_overlay_protects_reused_canonical_ticker() -> None:
    module = load_script_module("28_calibrate_biotech_opportunity.py", "calibration_delisted_overlay_regression")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE market_bars_daily(
            ticker TEXT NOT NULL,
            bar_date TEXT NOT NULL,
            source TEXT NOT NULL,
            close REAL NOT NULL
        );
        """
    )
    conn.executemany(
        "INSERT INTO market_bars_daily(ticker, bar_date, source, close) VALUES (?, ?, ?, ?)",
        [
            ("BOLD", "2020-01-13", "norgate_us_equities_total_return", 40.0),
            ("BOLD", "2020-01-14", "norgate_us_equities_total_return", 60.0),
            ("BOLD", "2024-04-01", "yahoo_adjusted", 100.0),
            ("BOLD", "2024-04-02", "yahoo_adjusted", 20.0),
        ],
    )
    live_reuser_bars = {
        "BOLD": [module.Bar(date(2024, 4, 1), 100.0), module.Bar(date(2024, 4, 2), 20.0)]
    }

    applied = module.apply_delisted_price_series_overlay(
        conn,
        live_reuser_bars,
        price_ticker_alias={"BOLD-202001": "BOLD", "BOLD": "BOLD"},
        min_date=date(2020, 1, 1),
        config={"delisted_calibration": {"source_rules": {"price_source": "norgate_us_equities_total_return"}}},
    )

    assert applied == 1
    assert [bar.day for bar in live_reuser_bars["BOLD-202001"]] == [date(2020, 1, 13), date(2020, 1, 14)]
    assert [bar.close for bar in live_reuser_bars["BOLD-202001"]] == [40.0, 60.0]
    assert [bar.close for bar in live_reuser_bars["BOLD"]] == [100.0, 20.0]

    rows = [{"ticker": "BOLD-202001", "asof_date": "2020-01-10"}]
    module.add_forward_returns(
        rows,
        live_reuser_bars,
        [1],
        round_trip_cost_bps=0.0,
        next_bar_entry=True,
        price_ticker_alias={"BOLD-202001": "BOLD"},
    )

    assert rows[0]["fwd_1d_entry_date"] == "2020-01-13"
    assert rows[0]["fwd_1d_target_date"] == "2020-01-14"
    assert rows[0]["fwd_1d_return"] == pytest.approx(0.5)


def test_phase1_score_sort_value_preserves_zero_scores() -> None:
    module = load_script_module("27_calibration_phase1_backtest.py", "phase1_score_sort_regression")
    rows = [
        {"ticker": "MISSING", "score": ""},
        {"ticker": "ZERO", "score": "0.0"},
        {"ticker": "POSITIVE", "score": "5.0"},
    ]

    assert module.score_sort_value(rows[0], "score") == pytest.approx(-1e9)
    assert module.score_sort_value(rows[1], "score") == pytest.approx(0.0)
    assert module.score_sort_value(rows[2], "score") == pytest.approx(5.0)

    ranked = sorted(rows, key=lambda row: module.score_sort_value(row, "score"), reverse=True)
    assert [row["ticker"] for row in ranked] == ["POSITIVE", "ZERO", "MISSING"]

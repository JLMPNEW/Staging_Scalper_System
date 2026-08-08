from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from tests.biotech.conftest import load_script_module


def test_legacy_13f_date_parser_preserves_eleven_character_sec_dates() -> None:
    module = load_script_module("54_backfill_legacy_13f_text_filings.py", "legacy_13f_date_regression")

    assert module.parse_date("31-JAN-2025") == date(2025, 1, 31)
    assert module.parse_date("2025-01-31T12:00:00Z") == date(2025, 1, 31)


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
            VALUES (10, '0001', 'https://example.test/0001.txt', 'complete_submission_text', '', 'old-hash');
        INSERT INTO sec_filing_latest_document(accession_nodash, document_id, document_url, document_type, text_hash, text_length)
            VALUES ('0001', 10, 'https://example.test/0001.txt', 'complete_submission_text', 'old-hash', 0);
        INSERT INTO sec_events(event_id, company_id, accession_nodash, filing_date, form, event_type)
            VALUES (1, 1, '0001', '2026-05-08', '10-Q', 'pdufa_date');
        INSERT INTO sec_event_parse_state(accession_nodash, text_hash, parser_signature, parsed_at, event_count, created_at, updated_at)
            VALUES ('0001', 'old-hash', 'old-signature', '2026-05-08T00:00:00Z', 1, '2026-05-08T00:00:00Z', '2026-05-08T00:00:00Z');
        """
    )

    # The metadata-only stale scan no longer reads text_content, so a blank body
    # behind an intact manifest row is not cleared here...
    cleared = module.clear_stale_events_for_missing_document_text(
        conn,
        cutoff="2025-05-08",
        asof="2026-05-08",
        ticker_filter={"TST"},
        parser_signature="new-signature",
    )
    assert cleared == 0

    # ...it is detected at fetch time instead: the filing is eligible (signature
    # mismatch), comes back as a phantom, and reset gives the same end state.
    filings, phantoms = module.load_filing_texts_to_parse(
        conn,
        cutoff="2025-05-08",
        asof="2026-05-08",
        ticker_filter={"TST"},
        max_filings=0,
        offset=0,
        reparse_signature_mismatch=True,
        parser_signature="new-signature",
    )
    assert filings == []
    assert [candidate.accession_nodash for candidate in phantoms] == ["0001"]
    module.reset_blank_text_parse_state(conn, ["0001"], "new-signature")

    assert conn.execute("SELECT COUNT(*) FROM sec_events").fetchone()[0] == 0
    state = conn.execute("SELECT text_hash, parser_signature, event_count FROM sec_event_parse_state").fetchone()
    assert dict(state) == {"text_hash": "", "parser_signature": "new-signature", "event_count": 0}

    # Metadata breakage (manifest row gone) is still cleared by the cheap scan.
    conn.execute(
        "INSERT INTO sec_events(event_id, company_id, accession_nodash, filing_date, form, event_type)"
        " VALUES (2, 1, '0001', '2026-05-08', '10-Q', 'pdufa_date')"
    )
    conn.execute("DELETE FROM sec_filing_latest_document WHERE accession_nodash = '0001'")
    cleared = module.clear_stale_events_for_missing_document_text(
        conn,
        cutoff="2025-05-08",
        asof="2026-05-08",
        ticker_filter={"TST"},
        parser_signature="new-signature",
    )
    assert cleared == 1
    assert conn.execute("SELECT COUNT(*) FROM sec_events").fetchone()[0] == 0


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

    skipped, skipped_phantoms = module.load_filing_texts_to_parse(
        conn,
        cutoff="2025-05-08",
        asof="2026-05-08",
        ticker_filter={"TST"},
        max_filings=0,
        offset=0,
        reparse_signature_mismatch=False,
        parser_signature="new-signature",
    )
    strict, strict_phantoms = module.load_filing_texts_to_parse(
        conn,
        cutoff="2025-05-08",
        asof="2026-05-08",
        ticker_filter={"TST"},
        max_filings=0,
        offset=0,
        reparse_signature_mismatch=True,
        parser_signature="new-signature",
    )

    assert skipped == [] and skipped_phantoms == []
    assert strict_phantoms == []
    assert [filing.accession_nodash for filing in strict] == ["0001"]


def test_sec_event_zero_work_scan_reads_no_text_content() -> None:
    # Runs the script's embedded selftest: zero-eligible scans must issue no
    # text_content reads (sqlite trace assertion) within a timing sanity bound,
    # eligible filings must load byte-identical to the full-rescan path, and
    # blank-text phantoms must be cleared and skipped.
    module = load_script_module("07_parse_sec_biotech_events.py", "sec_events_zero_work_selftest")

    module._selftest()


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


def test_promoted_portfolio_candidate_policy_filters_global_top_by_cohort_caps() -> None:
    module = load_script_module("11_score_biotech_index.py", "score_biotech_promoted_gate_regression")
    config = {
        "biotech_scoring": {
            "portfolio_candidate_policy": {
                "enabled": True,
                "name": "post_adaptive_top4_late_platform_max8_raw",
                "rank_top_n": 20,
                "allowed_primary_cohorts": [
                    "late_clinical_pivotal_or_registrational",
                    "platform_partnered_modality_pipeline",
                ],
                "cohort_top_k_per_cohort": 4,
                "total_max": 8,
                "selected_reason": "promoted_policy",
                "excluded_reason": "excluded_by_policy",
            }
        }
    }
    rows: list[dict[str, Any]] = []
    cohorts = {
        "L": "late_clinical_pivotal_or_registrational",
        "P": "platform_partnered_modality_pipeline",
        "C": "commercial_profitable_quality_or_mature",
    }
    for idx, (ticker, cohort_key, score) in enumerate(
        [
            ("L1", "L", 100),
            ("L2", "L", 99),
            ("L3", "L", 98),
            ("L4", "L", 97),
            ("L5", "L", 96),
            ("P1", "P", 95),
            ("P2", "P", 94),
            ("P3", "P", 93),
            ("P4", "P", 92),
            ("P5", "P", 91),
            ("C1", "C", 90),
            ("C2", "C", 89),
        ],
        start=1,
    ):
        rows.append(
            {
                "ticker": ticker,
                "biotech_primary_cohort": cohorts[cohort_key],
                "portfolio_candidate_gate": 0.0,
                "portfolio_candidate_status": "excluded",
                "portfolio_candidate_reason": "allocation_bucket_avoid",
                "eligibility_reason": "allocation_bucket_avoid",
                "portfolio_candidate_score": float(score),
                "native_score_value": float(score),
                "opportunity_score": float(score),
                "score_zero_is_missing_flag": 0.0,
                "price_data_asof_date": "2026-07-07",
                "rank": idx,
            }
        )
    rows.append(
        {
            "ticker": "MISS",
            "biotech_primary_cohort": cohorts["L"],
            "portfolio_candidate_gate": 0.0,
            "portfolio_candidate_status": "excluded",
            "portfolio_candidate_reason": "missing_score",
            "eligibility_reason": "missing_score",
            "portfolio_candidate_score": 0.0,
            "native_score_value": "",
            "opportunity_score": 0.0,
            "score_zero_is_missing_flag": 1.0,
            "price_data_asof_date": "2026-07-07",
        }
    )

    module.apply_promoted_portfolio_candidate_policy(rows, config)

    selected = [row["ticker"] for row in rows if row["portfolio_candidate_gate"] == 1.0]
    assert selected == ["L1", "L2", "L3", "L4", "P1", "P2", "P3", "P4"]
    assert next(row for row in rows if row["ticker"] == "L5")["portfolio_candidate_reason"] == "excluded_by_policy"
    assert next(row for row in rows if row["ticker"] == "C1")["portfolio_candidate_reason"] == "excluded_by_policy"
    missing = next(row for row in rows if row["ticker"] == "MISS")
    assert missing["portfolio_candidate_gate"] == 0.0
    assert missing["portfolio_candidate_reason"] == "missing_score"


def test_promoted_portfolio_candidate_policy_supports_cohort_first_and_cash_floor() -> None:
    module = load_script_module("11_score_biotech_index.py", "score_biotech_cohort_first_gate_regression")
    config = {
        "biotech_scoring": {
            "portfolio_candidate_policy": {
                "enabled": True,
                "name": "cohort_first_top4_late_platform_max8_cash2_raw",
                "selection_order": "cohort_then_rank",
                "rank_top_n": 20,
                "allowed_primary_cohorts": [
                    "late_clinical_pivotal_or_registrational",
                    "platform_partnered_modality_pipeline",
                ],
                "cohort_top_k_per_cohort": 4,
                "total_max": 8,
                "min_selected_names": 2,
                "below_min_selected_action": "cash",
                "selected_reason": "cohort_first_selected",
                "excluded_reason": "cohort_first_excluded",
            }
        }
    }
    rows: list[dict[str, Any]] = []
    for idx in range(25):
        rows.append(
            {
                "ticker": f"C{idx:02d}",
                "biotech_primary_cohort": "commercial_profitable_quality_or_mature",
                "portfolio_candidate_score": float(100 - idx),
                "score_zero_is_missing_flag": 0.0,
                "price_data_asof_date": "2026-07-24",
            }
        )
    for idx in range(5):
        for prefix, cohort in (
            ("L", "late_clinical_pivotal_or_registrational"),
            ("P", "platform_partnered_modality_pipeline"),
        ):
            rows.append(
                {
                    "ticker": f"{prefix}{idx + 1}",
                    "biotech_primary_cohort": cohort,
                    "portfolio_candidate_score": float(70 - idx * 2 - (1 if prefix == "P" else 0)),
                    "score_zero_is_missing_flag": 0.0,
                    "price_data_asof_date": "2026-07-24",
                }
            )

    module.apply_promoted_portfolio_candidate_policy(rows, config)

    selected = [row["ticker"] for row in rows if row.get("portfolio_candidate_gate") == 1.0]
    assert selected == ["L1", "P1", "L2", "P2", "L3", "P3", "L4", "P4"]
    assert not any(ticker.startswith("C") for ticker in selected)

    one_name_rows = [dict(rows[-1], ticker="ONLY")]
    module.apply_promoted_portfolio_candidate_policy(one_name_rows, config)
    assert one_name_rows[0]["portfolio_candidate_gate"] == 0.0
    assert one_name_rows[0]["portfolio_candidate_reason"] == (
        "cash_fallback_insufficient_promoted_policy_breadth"
    )


def test_promoted_portfolio_candidate_policy_rejects_evidence_drift() -> None:
    module = load_script_module("11_score_biotech_index.py", "score_biotech_gate_evidence_regression")
    config = {
        "biotech_scoring": {
            "portfolio_candidate_policy": {
                "enabled": True,
                "name": "validated_policy",
                "rank_top_n": 10,
                "evidence": {
                    "validated_policy_name": "validated_policy",
                    "validated_rank_top_n": 20,
                },
            }
        }
    }

    with pytest.raises(ValueError, match="rank_top_n differs"):
        module.apply_promoted_portfolio_candidate_policy([], config)


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

    merged = module.merge_unique_studies([second, first])

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
            ("OLD", "2018-01-12", "norgate_us_equities_total_return", 30.0),
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
    assert module.count_applicable_delisted_price_overlays(
        conn,
        price_ticker_alias={"BOLD-202001": "BOLD", "OLD-201801": "OLD"},
        min_date=date(2020, 1, 1),
        config={"delisted_calibration": {"source_rules": {"price_source": "norgate_us_equities_total_return"}}},
    ) == 1

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


def test_calibration_missing_raw_risk_defaults_to_worst_case() -> None:
    module = load_script_module("28_calibrate_biotech_opportunity.py", "calibration_missing_risk_regression")

    risk_value, risk_missing = module.raw_score_value({}, {}, "risk_score_raw")
    catalyst_value, catalyst_missing = module.raw_score_value({}, {}, "catalyst_score_raw")

    assert risk_missing is True
    assert risk_value == pytest.approx(100.0)
    assert catalyst_missing is True
    assert catalyst_value == pytest.approx(0.0)


def test_calibration_custom_selection_policies_keep_raw_baseline() -> None:
    module = load_script_module("28_calibrate_biotech_opportunity.py", "calibration_policy_baseline_regression")
    config = {
        "calibration": {
            "tier1": {
                "selection_policies": [
                    {"policy_name": "custom_hard_veto", "hard_veto": True, "hard_veto_reasons": ["*"]}
                ]
            }
        }
    }

    policies = module.generate_selection_policies(config)

    assert [policy.policy_name for policy in policies][:2] == ["raw_legacy_score", "custom_hard_veto"]


def test_calibration_load_bars_rejects_empty_market_sources() -> None:
    module = load_script_module("28_calibrate_biotech_opportunity.py", "calibration_load_bars_sources_regression")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    with pytest.raises(ValueError, match="market data source"):
        module.load_bars(conn, tickers={"AAA"}, min_date=date(2026, 1, 1), market_sources=[])


def test_calibration_commercial_risk_diag_drops_nested_sub_scores() -> None:
    module = load_script_module("28_calibrate_biotech_opportunity.py", "calibration_commercial_diag_regression")

    fields = module.diagnostic_commercial_risk_fields(
        {
            "commercial_risk_overlay_score": 42.0,
            "commercial_risk_sub_scores": {"deterioration": 12.0},
        }
    )

    assert fields == {"diag_commercial_risk_overlay_score": 42.0}


def test_calibration_selection_policy_allowed_primary_cohorts_filters_rows() -> None:
    module = load_script_module("28_calibrate_biotech_opportunity.py", "calibration_allowed_cohorts_regression")
    policy = module.SelectionPolicy(
        policy_name="late_platform_only",
        description="test policy",
        allowed_primary_cohorts=("late_clinical_pivotal_or_registrational", "platform_partnered_modality_pipeline"),
    )
    spec = module.WeightSpec(
        candidate_name="current_config",
        description="test",
        clinical_catalyst=0.55,
        clinical_credibility=0.25,
        clinical_financial_quality=0.15,
        clinical_momentum=0.05,
        clinical_risk_penalty=0.15,
        clinical_stage_profile={
            "clinical_opportunity": 0.45,
            "commercial_value": 0.03,
            "forward_guidance": 0.04,
            "valuation": 0.05,
            "upside_capacity": 0.15,
            "institutional_upside": 0.0,
            "financial_quality": 0.15,
            "momentum": 0.05,
            "borrow_signal": 0.05,
            "short_interest_signal": 0.03,
            "institutional_crowding": 0.0,
            "risk_penalty": 0.40,
        },
        commercial_stage_profile={
            "clinical_opportunity": 0.04,
            "commercial_value": 0.27,
            "forward_guidance": 0.2,
            "valuation": 0.07,
            "upside_capacity": 0.04,
            "institutional_upside": 0.06,
            "financial_quality": 0.14,
            "momentum": 0.06,
            "borrow_signal": 0.04,
            "short_interest_signal": 0.03,
            "institutional_crowding": 0.05,
            "risk_penalty": 0.24,
        },
    )
    params = module.CalibrationParams()
    base_row = {
        "profile_name": "clinical_stage",
        "catalyst_score_raw": 50.0,
        "credibility_score_raw": 50.0,
        "financial_quality_score_raw": 50.0,
        "momentum_score_raw": 50.0,
        "risk_score_raw": 50.0,
        "commercial_value_score": 50.0,
        "forward_guidance_score": 50.0,
        "valuation_score_raw": 50.0,
        "valuation_score": 50.0,
        "upside_capacity_score": 50.0,
        "institutional_upside_capacity_score": 50.0,
        "borrow_pressure_score": 0.0,
        "short_interest_days_to_cover_score": 0.0,
        "institutional_accumulation_score": 50.0,
    }
    allowed_row = {
        **base_row,
        "biotech_primary_cohort": "platform_partnered_modality_pipeline",
    }
    blocked_row = {
        **base_row,
        "biotech_primary_cohort": "commercial_profitable_quality_or_mature",
    }

    allowed_score, _allowed_diag = module.policy_adjusted_score(allowed_row, spec, policy, params)
    blocked_score, _blocked_diag = module.policy_adjusted_score(blocked_row, spec, policy, params)

    assert allowed_score is not None
    assert blocked_score is None


def test_calibration_selection_policy_cohort_top_k_limits_each_cohort() -> None:
    module = load_script_module("28_calibrate_biotech_opportunity.py", "calibration_cohort_top_k_regression")
    policy = module.SelectionPolicy(
        policy_name="top2_per_cohort",
        description="test policy",
        cohort_top_k_per_cohort=2,
    )
    ranked_rows = [
        {"ticker": "A1", "biotech_primary_cohort": "alpha"},
        {"ticker": "A2", "biotech_primary_cohort": "alpha"},
        {"ticker": "A3", "biotech_primary_cohort": "alpha"},
        {"ticker": "B1", "biotech_primary_cohort": "beta"},
        {"ticker": "B2", "biotech_primary_cohort": "beta"},
        {"ticker": "B3", "biotech_primary_cohort": "beta"},
    ]

    limited = module.apply_policy_cohort_top_k_limit(ranked_rows, policy)

    assert [row["ticker"] for row in limited] == ["A1", "A2", "B1", "B2"]


def test_calibration_selection_policy_minimum_floor_returns_cash() -> None:
    module = load_script_module("28_calibrate_biotech_opportunity.py", "calibration_minimum_floor_regression")
    policy = module.SelectionPolicy(
        policy_name="cash_below_two",
        description="test policy",
        min_selected_names=2,
        below_min_selected_action="cash",
    )

    assert module.apply_policy_minimum_selection_floor([{"ticker": "A"}], policy) == []
    selected = module.apply_policy_minimum_selection_floor(
        [{"ticker": "A"}, {"ticker": "B"}],
        policy,
    )
    assert [row["ticker"] for row in selected] == ["A", "B"]


def test_calibration_constraints_enforce_selection_breadth() -> None:
    module = load_script_module("28_calibrate_biotech_opportunity.py", "calibration_breadth_gate_regression")
    params = module.CalibrationParams(
        min_selected_observations=1,
        min_asof_dates=1,
        min_selection_date_coverage_pct=50.0,
        min_avg_selected_names_per_active_date=3.0,
        min_selected_names_per_active_date=2,
    )
    selected_summary = {
        "n": 10,
        "lcb_return_pct": 1.0,
        "sortino_like": 1.0,
        "profit_factor": 2.0,
        "omega_configured": 2.0,
        "core_hard_weakness_exposure_pct": 0.0,
        "illiquid_weakness_exposure_pct": 0.0,
        "top3_gain_contribution_pct": 10.0,
        "large_loss_20pct_rate_pct": 0.0,
        "large_loss_40pct_rate_pct": 0.0,
    }

    result = module.calibration_constraint_fields(
        selected_summary,
        asof_dates=4,
        eligible_asof_dates=10,
        avg_selected_names_per_active_date=2.5,
        min_selected_names_per_active_date=1,
        params=params,
    )

    assert result["calibration_pass"] is False
    assert result["calibration_fail_reasons"].split("|") == [
        "selection_date_coverage<50.0",
        "avg_selected_names_per_active_date<3.0",
        "min_selected_names_per_active_date<2",
    ]


def test_calibration_selection_policy_post_selection_gate_filters_global_top_n() -> None:
    module = load_script_module("28_calibrate_biotech_opportunity.py", "calibration_post_selection_gate_regression")
    policy = module.SelectionPolicy(
        policy_name="post_top1_beta",
        description="test policy",
        post_selection_allowed_primary_cohorts=("beta",),
        post_selection_cohort_top_k_per_cohort=1,
    )
    ranked_rows = [
        {"ticker": "A1", "biotech_primary_cohort": "alpha"},
        {"ticker": "B1", "biotech_primary_cohort": "beta"},
        {"ticker": "B2", "biotech_primary_cohort": "beta"},
        {"ticker": "B3", "biotech_primary_cohort": "beta"},
    ]

    selected = module.apply_policy_post_selection_filter(ranked_rows, policy, top_n=3)

    assert [row["ticker"] for row in selected] == ["B1"]


def test_calibration_selection_policy_post_selection_total_max_is_adaptive_cap() -> None:
    module = load_script_module("28_calibrate_biotech_opportunity.py", "calibration_post_selection_total_cap")
    policy = module.SelectionPolicy(
        policy_name="post_max3",
        description="test policy",
        post_selection_allowed_primary_cohorts=("alpha", "beta"),
        post_selection_total_max=3,
    )
    ranked_rows = [
        {"ticker": "A1", "biotech_primary_cohort": "alpha"},
        {"ticker": "B1", "biotech_primary_cohort": "beta"},
        {"ticker": "A2", "biotech_primary_cohort": "alpha"},
        {"ticker": "B2", "biotech_primary_cohort": "beta"},
        {"ticker": "A3", "biotech_primary_cohort": "alpha"},
    ]

    selected = module.apply_policy_post_selection_filter(ranked_rows, policy, top_n=5)

    assert [row["ticker"] for row in selected] == ["A1", "B1", "A2"]


def test_calibration_selection_policy_post_selection_score_floor_keeps_close_scores() -> None:
    module = load_script_module("28_calibrate_biotech_opportunity.py", "calibration_post_selection_score_floor")
    policy = module.SelectionPolicy(
        policy_name="post_score90",
        description="test policy",
        post_selection_allowed_primary_cohorts=("alpha", "beta"),
        post_selection_min_score_pct_of_top=90.0,
        post_selection_total_max=10,
    )
    ranked_rows = [
        {"ticker": "A1", "biotech_primary_cohort": "alpha", "candidate_selection_score": 100.0},
        {"ticker": "B1", "biotech_primary_cohort": "beta", "candidate_selection_score": 92.0},
        {"ticker": "A2", "biotech_primary_cohort": "alpha", "candidate_selection_score": 89.9},
        {"ticker": "B2", "biotech_primary_cohort": "beta", "candidate_selection_score": 88.0},
    ]

    selected = module.apply_policy_post_selection_filter(ranked_rows, policy, top_n=4)

    assert [row["ticker"] for row in selected] == ["A1", "B1"]


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


def test_borrow_rank_lift_preserves_zero_score_and_zero_risk() -> None:
    module = load_script_module("44_validate_biotech_borrow_rank_lift.py", "borrow_rank_zero_regression")
    rows = [
        {"ticker": "NEG", "score": -1.0, "risk_score": 0.0},
        {"ticker": "ZHI", "score": 0.0, "risk_score": 100.0},
        {"ticker": "ZLO", "score": 0.0, "risk_score": 0.0},
    ]

    ranked = module.rank_rows(rows, score_field="score")

    assert [row["ticker"] for row in ranked] == ["ZLO", "ZHI", "NEG"]


def test_borrow_diagnostics_treats_zero_financial_quality_as_distress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_script_module("40_validate_biotech_borrow_availability.py", "borrow_quality_zero_regression")
    monkeypatch.setattr(
        module,
        "point_in_time_borrow_features",
        lambda **_kwargs: {
            "borrow_pressure_score": 40.0,
            "borrow_rate_current": 0.20,
            "borrow_data_available_flag": 1.0,
        },
    )
    rows = [
        {
            "ticker": "AAA",
            "asof_date": "2026-07-07",
            "short_interest_pct_float": 0.20,
            "forward_catalyst_score": 80.0,
            "financial_quality_score_raw": 0.0,
        }
    ]

    module.enrich_borrow_diagnostics(
        rows,
        history_by_ticker={},
        snapshots_by_ticker={},
        high_borrow_pressure_min=30.0,
        elevated_borrow_pressure_min=20.0,
        high_borrow_rate_min=0.15,
        squeeze_short_interest_min=50.0,
        squeeze_catalyst_min=60.0,
        hard_to_borrow_shares=50_000.0,
        max_fee_staleness_days=7,
        max_snapshot_staleness_days=7,
    )

    assert rows[0]["borrow_distress_flag"] == 1.0
    assert rows[0]["borrow_squeeze_setup_flag"] == 0.0


def test_oos_role_honors_configured_lock_date() -> None:
    module = load_script_module("11_score_biotech_index.py", "score_oos_lock_regression")
    config = {"biotech_historical_sequence": {"strict_oos_start_date": "2026-07-07"}}

    def row(asof: str) -> dict[str, Any]:
        return {
            "ticker": "AAA",
            "asof_date": asof,
            "source_snapshot_asof_date": asof,
            "price_data_asof_date": asof,
            "opportunity_score": 55.0,
            "production_rank_score": 55.0,
            "production_rank_score_field": "opportunity_score",
            "biotech_cohort_investible_flag": 1.0,
            "biotech_cohort_calibration_eligible_flag": 1.0,
            "core_structural_veto_flag": 0.0,
            "rank_quality_cap_vetoed": 0.0,
            "allocation_bucket": "watch",
            "calibration_only": 0.0,
        }

    rows = [row("2026-07-06"), row("2026-07-07")]
    module.enrich_portfolio_layer_contract_rows(rows, config)

    assert rows[0]["calibration_sample_role"] == "pre_lock_research"
    assert rows[0]["oos_score_valid_flag"] == 0.0
    assert rows[1]["calibration_sample_role"] == "strict_oos"
    assert rows[1]["oos_score_valid_flag"] == 1.0


def test_historical_export_forwards_config_to_contract_enrichment() -> None:
    module = load_script_module(
        "56_generate_historical_biotech_score_csvs.py",
        "historical_export_contract_config_regression",
    )
    config = {"biotech_historical_sequence": {"strict_oos_start_date": "2026-07-07"}}
    observed: dict[str, object] = {}

    class ExportModule:
        @staticmethod
        def enrich_portfolio_layer_contract_rows(
            rows: list[dict[str, Any]],
            received_config: dict[str, Any],
        ) -> None:
            observed["rows"] = rows
            observed["config"] = received_config

    result = module.prepare_score_rows_for_export(
        [],
        ExportModule(),
        config=config,
        model_metadata={},
    )

    assert result == []
    assert observed == {"rows": [], "config": config}


def test_adcom_unknown_or_future_announcement_is_not_pit_visible() -> None:
    module = load_script_module("10_build_biotech_features.py", "adcom_announcement_pit_regression")
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE fda_adcom_events(
            company_id INTEGER,
            ticker TEXT,
            meeting_date TEXT,
            committee TEXT,
            drug_name TEXT,
            indication TEXT,
            vote_result TEXT,
            source_url TEXT,
            announced_date TEXT
        );
        INSERT INTO fda_adcom_events VALUES
            (1, 'KNOWN', '2026-08-01', '', '', '', '', '', '2026-07-01'),
            (2, 'UNKNOWN', '2026-08-01', '', '', '', '', '', NULL),
            (3, 'FUTURE', '2026-08-01', '', '', '', '', '', '2026-07-08');
        """
    )

    rows = module.load_fda_adcom_events(conn, date(2026, 7, 7), lookahead_days=120)

    assert set(rows) == {1}


def test_trial_status_override_is_not_applied_before_verification_date() -> None:
    module = load_script_module("10_build_biotech_features.py", "trial_override_pit_regression")
    evidence = module.pd.DataFrame(
        [
            {
                "ticker": "AAA",
                "nct_id": "NCT00000001",
                "overall_status": "RECRUITING",
                "is_active_status": "True",
                "is_therapeutic": "True",
                "qualifying_trial": "True",
                "trial_score": "8.0",
            }
        ]
    )
    overrides = module.pd.DataFrame(
        [
            {
                "enabled": "true",
                "ticker": "AAA",
                "nct_id": "NCT00000001",
                "verified_date": "2026-04-22",
                "override_status": "failed_parent_program",
                "exclude_from_scoring": "true",
            }
        ]
    )

    before = module.apply_trial_status_overrides(evidence, overrides, asof_date=date(2026, 4, 21))
    after = module.apply_trial_status_overrides(evidence, overrides, asof_date=date(2026, 4, 22))

    assert str(before.iloc[0].get("outcome_override_applied") or "") == ""
    assert after.iloc[0]["outcome_override_applied"] == "True"
    assert after.iloc[0]["qualifying_trial"] == "False"


def test_ctgov_manual_decision_requires_verification_by_asof() -> None:
    module = load_script_module("05_audit_ctgov_trial_links.py", "ctgov_manual_decision_pit_regression")
    row = {"manual_verified_date": "2026-05-27"}

    assert not module.row_verified_asof(row, asof_date=date(2026, 5, 26), field="manual_verified_date")
    assert module.row_verified_asof(row, asof_date=date(2026, 5, 27), field="manual_verified_date")
    assert not module.row_verified_asof({}, asof_date=date(2026, 5, 27), field="manual_verified_date")


def test_oos_diagnostic_zero_lcb_beats_negative_lcb() -> None:
    module = load_script_module("58_diagnose_biotech_oos_calibration.py", "oos_zero_lcb_regression")
    rows = [
        {
            "horizon_days": "120",
            "top_n": "10",
            "sample": "all",
            "candidate_name": "zero_lcb",
            "test_selected_lcb_return_pct": "0.0",
        },
        {
            "horizon_days": "120",
            "top_n": "10",
            "sample": "all",
            "candidate_name": "negative_lcb",
            "test_selected_lcb_return_pct": "-1.0",
        },
    ]

    best = module.best_by_scope_rows(rows)

    assert best[0]["candidate_name"] == "zero_lcb"


def test_optuna_help_formats_percent_text(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    module = load_script_module("46_optuna_biotech_candidate_optimizer.py", "optuna_help_regression")
    monkeypatch.setattr(sys, "argv", ["46_optuna_biotech_candidate_optimizer.py", "--help"])

    with pytest.raises(SystemExit) as exc_info:
        module.parse_args()

    assert exc_info.value.code == 0
    assert "Maximum 20% loss rate" in capsys.readouterr().out


def test_ranked_report_surfaces_portfolio_contract_fields() -> None:
    module = load_script_module("12_publish_biotech_reports.py", "report_portfolio_contract_regression")
    row = {
        "ticker": "AAA",
        "top_evidence_json": "{}",
        "portfolio_candidate_gate": 1.0,
        "portfolio_candidate_score": 55.0,
        "portfolio_candidate_status": "eligible",
        "portfolio_candidate_reason": "promoted_policy",
        "calibration_sample_role": "strict_oos",
        "oos_score_valid_flag": 1.0,
    }

    flattened = module.flatten_score_row(row)

    assert flattened["portfolio_candidate_gate"] == 1.0
    assert flattened["portfolio_candidate_reason"] == "promoted_policy"
    assert flattened["calibration_sample_role"] == "strict_oos"
    assert {"portfolio_candidate_gate", "oos_score_valid_flag"}.issubset(module.TOP_SCORE_FIELDS)

from __future__ import annotations

import argparse
import csv
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.biotech.conftest import load_script_module


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BIOTECH_ROOT = PROJECT_ROOT / "biotech_index"


def load_screen_module() -> Any:
    module_name = "biotech_screen_listing_policy_regression"
    spec = importlib.util.spec_from_file_location(
        module_name,
        BIOTECH_ROOT / "screen_biotech_universe.py",
    )
    if spec is None or spec.loader is None:
        raise ImportError("Could not load biotech universe screener")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    sys.path.insert(0, str(BIOTECH_ROOT))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(BIOTECH_ROOT))
    return module


def load_enrichment_module() -> Any:
    module_name = "biotech_ticker_enrichment_listing_policy_regression"
    module_path = PROJECT_ROOT / "ticker_mapping" / "enrich_all_tickers_biotech.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError("Could not load biotech ticker enrichment")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    sys.path.insert(0, str(module_path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(module_path.parent))
    return module


def test_nasdaq_listing_status_refreshes_without_identity_overwrite() -> None:
    module = load_enrichment_module()
    frame = module.pd.DataFrame(
        [
            {
                "Ticker": "CUE",
                "ListingStatus": "active_financial_status_D",
                "SecurityType": "Common Stock",
                "Exchange": "Nasdaq",
                "CompanyName": "Cue Biopharma, Inc.",
                "IdentityDataSources": "nasdaqtrader:nasdaqlisted",
            }
        ]
    )
    directory = {
        "CUE": module.DirectoryEntry(
            ticker="CUE",
            security_name="Cue Biopharma, Inc. Common Stock",
            exchange="Nasdaq",
            etf="N",
            test_issue="N",
            financial_status="N",
            source="nasdaqtrader:nasdaqlisted",
        )
    }

    refreshed = module.apply_nasdaq_fields(frame, directory, overwrite_existing=False)

    assert refreshed.loc[0, "ListingStatus"] == "active"
    assert refreshed.loc[0, "CompanyName"] == "Cue Biopharma, Inc."


def test_stale_nasdaq_directory_cache_is_refreshed_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_enrichment_module()
    cache_path = tmp_path / "nasdaqlisted.txt"
    cache_path.write_text("stale", encoding="utf-8")
    cached_mtime = cache_path.stat().st_mtime

    class Response:
        text = "fresh"

        @staticmethod
        def raise_for_status() -> None:
            return None

    monkeypatch.setattr(module.time, "time", lambda: cached_mtime + 7200.0)
    monkeypatch.setattr(module.requests, "get", lambda *args, **kwargs: Response())

    result = module.cache_get_text(tmp_path, cache_path.name, "https://example.test", 1.0, 1.0)

    assert result == "fresh"
    assert cache_path.read_text(encoding="utf-8") == "fresh"
    assert not cache_path.with_suffix(".txt.tmp").exists()


def test_identity_fallback_recovers_verified_rows_and_unknowns_fail_closed() -> None:
    module = load_enrichment_module()
    current = module.pd.DataFrame(
        [
            {
                "Ticker": "NUVL",
                "CompanyName": "Nuvalent, Inc.",
                "Exchange": "Nasdaq",
                "SecurityType": "",
                "IsPrimaryListing": "",
                "ListingStatus": "",
                "Country": "",
                "Currency": "",
                "IdentityDataSources": "",
            },
            {
                "Ticker": "UNRES",
                "CompanyName": "Unresolved Co",
                "Exchange": "Nasdaq",
                "SecurityType": "",
                "IsPrimaryListing": "True",
                "ListingStatus": "",
                "Country": "",
                "Currency": "",
                "IdentityDataSources": "",
            },
        ]
    )
    fallback = module.pd.DataFrame(
        [
            {
                "ticker": "NUVL",
                "security_type": "Common Stock",
                "is_primary_listing": "True",
                "listing_status": "active",
                "country": "United States",
                "currency": "USD",
            }
        ]
    )

    recovered = module.apply_identity_fallback_fields(
        current, fallback, source_label="test_fallback"
    )
    finalized = module.finalize_unresolved_identity_fields(recovered)

    nuvl = finalized.loc[finalized["Ticker"] == "NUVL"].iloc[0]
    unresolved = finalized.loc[finalized["Ticker"] == "UNRES"].iloc[0]
    assert nuvl["ListingStatus"] == "active"
    assert "test_fallback" in nuvl["IdentityDataSources"]
    assert unresolved["SecurityType"] == "Unknown"
    assert unresolved["ListingStatus"] == "unknown"
    assert unresolved["Country"] == "United States"
    assert unresolved["Currency"] == "USD"


@pytest.mark.parametrize(
    "listing_status",
    ["active_financial_status_D", "active_financial_status_E"],
)
def test_nonclean_active_listing_remains_scoreable_but_flagged_for_review(
    listing_status: str,
) -> None:
    module = load_screen_module()
    args = argparse.Namespace(
        disable_identity_gate=False,
        allow_missing_identity_fields=False,
        target_security_types=module.DEFAULT_TARGET_SECURITY_TYPES,
        require_primary_listing=True,
        allowed_listing_statuses=module.DEFAULT_ALLOWED_LISTING_STATUSES,
    )
    record = {
        "SecurityType": "Common Stock",
        "IsPrimaryListing": "true",
        "ListingStatus": listing_status,
        "ManualExclude": "false",
    }

    assert module.pre_screen_remove_reasons(record, args) == []

    decision, reasons = module.decide_row(
        row={
            "listing_status": listing_status,
            "has_interventional_trial_match": True,
            "has_recent_sec_filing_2y": True,
            "has_recent_rnd_disclosure": True,
            "median_addv20": 5_000_000.0,
        },
        min_median_addv20=1_000_000.0,
        allow_missing_liquidity=False,
        manual_include_demotes_remove_to_review=True,
        review_on_soft_liquidity_warning=False,
    )

    assert decision == "keep"
    assert "review:listing_financial_status_not_clean" in reasons


def test_nonclean_listing_forces_published_portfolio_gate_off() -> None:
    module = load_script_module(
        "12_publish_biotech_reports.py",
        "biotech_report_listing_gate_regression",
    )

    flattened = module.flatten_score_row(
        {
            "ticker": "KYNB",
            "listing_status": "active_financial_status_D",
            "top_evidence_json": "{}",
            "portfolio_candidate_gate": 1.0,
            "portfolio_candidate_status": "eligible",
            "portfolio_candidate_reason": "promoted_policy",
            "eligibility_reason": "promoted_policy",
        }
    )

    assert flattened["portfolio_candidate_gate"] == 0.0
    assert flattened["portfolio_candidate_status"] == "excluded"
    assert flattened["portfolio_candidate_reason"] == "listing_status_not_clean"
    assert flattened["eligibility_reason"] == "listing_status_not_clean"


def test_wve_operating_sponsor_alias_is_an_exact_link() -> None:
    module = load_script_module(
        "04_link_trials_to_companies.py",
        "biotech_wve_alias_link_regression",
    )
    alias = module.normalize_org_name("Wave Life Sciences USA, Inc.")
    company = module.CompanyAliases(
        company_id=1,
        ticker="WVE",
        company_name="Wave Life Sciences Ltd",
        alias_norms=frozenset({alias}),
        alias_tokens=tuple(frozenset(value) for value in module.alias_token_sets({alias})),
    )
    sponsor = module.SponsorRow(
        nct_id="NCT06842186",
        sponsor_name="Wave Life Sciences USA, Inc.",
        sponsor_name_norm=alias,
        sponsor_role="lead",
    )

    links = module.build_links(
        [sponsor],
        [company],
        min_confidence=0.7,
        allow_single_token_match=False,
        allow_single_token_prefix_match=False,
        single_token_prefix_min_length=7,
    )

    assert len(links) == 1
    assert links[0].match_method == "exact_norm"
    assert links[0].confidence == pytest.approx(1.0)


def read_rows(name: str) -> list[dict[str, str]]:
    with (BIOTECH_ROOT / "data" / name).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_five_ticker_curated_repairs_are_complete() -> None:
    cohorts = {
        row["ticker"]: row["biotech_calibration_cohort"]
        for row in read_rows("biotech_calibration_cohorts.csv")
    }
    assert cohorts["TECH"] == "commercial_profitable_quality_or_mature"
    assert cohorts["WVE"] == "platform_partnered_modality_pipeline"
    assert cohorts["KRRO"] == "platform_partnered_modality_pipeline"
    assert cohorts["CUE"] == "platform_partnered_modality_pipeline"
    assert cohorts["KYNB"] == "early_clinical_speculative_or_single_asset_pipeline"

    status_tickers = {row["ticker"] for row in read_rows("company_status_overrides.csv")}
    assert "KRRO" not in status_tickers
    statuses = {
        row["ticker"]: row for row in read_rows("company_status_overrides.csv")
    }
    assert statuses["CUE"]["decision"] == "review"
    assert statuses["KYNB"]["listing_status"] == "active_financial_status_D"

    manual = {
        row["ticker"]: row for row in read_rows("ctgov_manual_activation_overrides.csv")
    }
    assert manual["TECH"]["manual_verdict"] == "manual_keep"
    assert manual["CUE"]["manual_verdict"] == "manual_keep"
    assert manual["KRRO"]["manual_verified_active_study"] == "false"
    assert manual["KYNB"]["manual_verdict"] == "manual_keep"

    aliases = {
        (row["ticker"], row["alias"])
        for row in read_rows("company_alias_overrides.csv")
    }
    assert ("WVE", "Wave Life Sciences USA, Inc.") in aliases


def test_august_2026_scoring_additions_have_governed_cohorts() -> None:
    cohorts = {
        row["ticker"]: row["biotech_calibration_cohort"]
        for row in read_rows("biotech_calibration_cohorts.csv")
    }
    assert cohorts["BDTX"] == "platform_partnered_modality_pipeline"
    assert cohorts["TNYA"] == "platform_partnered_modality_pipeline"


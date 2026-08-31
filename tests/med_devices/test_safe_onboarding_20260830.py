from __future__ import annotations

import csv
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
TICKERS = {"MDXG", "ORGO", "CAI", "LNTH", "ANIK", "TECH"}
EXPECTED = {
    "MDXG": ("0001376339", "602496101", "2008-04-02", "hospital_supplies_surgical_consumables_oem"),
    "ORGO": ("0001661181", "68621F102", "2018-12-11", "hospital_supplies_surgical_consumables_oem"),
    "CAI": ("0002019410", "142152107", "2025-06-18", "diagnostics_clinical_tests"),
    "LNTH": ("0001521036", "516544103", "2015-06-25", "diagnostics_clinical_tests"),
    "ANIK": ("0000898437", "035255108", "1993-05-03", "orthopedics_spine_sports_implants"),
    "TECH": ("0000842023", "09073M104", "1989-02-09", "life_science_tools_research_instruments"),
}


def rows(relative_path: str) -> list[dict[str, str]]:
    with (ROOT / relative_path).open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def one_by_ticker(relative_path: str) -> dict[str, dict[str, str]]:
    selected = [row for row in rows(relative_path) if row.get("ticker") in TICKERS]
    assert len(selected) == len(TICKERS)
    assert len({row["ticker"] for row in selected}) == len(TICKERS)
    return {row["ticker"]: row for row in selected}


def test_seed_identity_and_listing_boundaries_are_complete() -> None:
    selected = one_by_ticker("ticker_mapping/med_dev_tickers_clean_keep.csv")
    for ticker, (cik, cusip, start, _) in EXPECTED.items():
        row = selected[ticker]
        assert row["cik"] == cik
        assert row["cusip"] == cusip
        assert row["listing_start_date"] == start
        assert row["investability_status"] == "investable"
        assert row["listing_status"] == "active"
        assert row["is_primary_listing"] == "1"


def test_security_identity_overrides_match_seed_and_are_reviewed() -> None:
    config = yaml.safe_load((ROOT / "med_devices/config.yaml").read_text(encoding="utf-8"))
    overrides = config["universe_validation"]["security_identity_overrides"]
    for ticker, (cik, cusip, start, _) in EXPECTED.items():
        assert overrides[ticker]["cik"] == cik
        assert overrides[ticker]["cusip"] == cusip
        assert overrides[ticker]["listing_start_date"] == start
        assert overrides[ticker]["reviewed_at"] == "2026-08-30"
        assert overrides[ticker]["reason"]
    assert "ticker_history_guard" in overrides["MDXG"]["reason"]
    assert "ticker_transition_guard" in overrides["ORGO"]["reason"]


def test_cohorts_are_exact_and_effective_from_security_start() -> None:
    selected = one_by_ticker("med_devices/data/calibration_cohort_overrides.csv")
    for ticker, (_, _, start, cohort) in EXPECTED.items():
        row = selected[ticker]
        assert row["calibration_cohort"] == cohort
        assert row["include_in_universe"].lower() == "true"
        assert row["valid_from"] == start
        assert row["reviewed_at"] == "2026-08-30"


def test_fda_and_reimbursement_controls_are_effective_dated() -> None:
    footprints = one_by_ticker("med_devices/data/fda_company_footprints.csv")
    classifications = one_by_ticker("med_devices/data/reimbursement_company_classifications.csv")
    aliases = [row for row in rows("med_devices/data/fda_company_aliases.csv") if row.get("ticker") in TICKERS]
    assert {row["ticker"] for row in aliases} == TICKERS
    for ticker, (_, _, start, _) in EXPECTED.items():
        for row in (footprints[ticker], classifications[ticker]):
            assert row["valid_from"] == start
            assert row["reviewed_at"] == "2026-08-30"
        assert footprints[ticker]["review_adjusted_fda_state"]
        assert classifications[ticker]["payment_rate_status"]
    assert footprints["ORGO"]["premarket_numbers"] == "K220317;K212579"
    assert footprints["CAI"]["premarket_numbers"] == "P240010"
    assert "K250997" in footprints["ANIK"]["premarket_numbers"]
    assert footprints["TECH"]["expected_cdrh_records"] == "no"


def test_exact_fda_manufacturers_have_manual_overrides_and_regressions() -> None:
    expected = {"1821": "ANIK", "5238": "ORGO", "8917": "CAI"}
    overrides = {
        row["fda_manufacturer_id"]: row
        for row in rows("med_devices/data/fda_manufacturer_overrides.csv")
        if row.get("fda_manufacturer_id") in expected
    }
    assert set(overrides) == set(expected)
    for manufacturer_id, ticker in expected.items():
        row = overrides[manufacturer_id]
        assert row["ticker"] == ticker
        assert row["mapping_method"] == "manual_override"
        assert float(row["confidence"]) >= 99.0
        assert row["valid_from"] == EXPECTED[ticker][2]
        assert row["reviewed_at"] == "2026-08-30"

    regressions = {
        row["fda_manufacturer_id"]: row
        for row in rows("med_devices/data/fda_mapping_regression_cases.csv")
        if row.get("fda_manufacturer_id") in expected
    }
    assert {key: row["expected_ticker"] for key, row in regressions.items()} == expected
    assert all(row["expected_mapping_method"] == "manual_override" for row in regressions.values())


def test_clinical_reviews_block_tech_token_collision() -> None:
    reviews = [row for row in rows("med_devices/data/clinical_trial_mapping_reviews.csv") if row["ticker"] in TICKERS]
    assert reviews
    assert all(row["valid_from"] and row["reviewed_at"] == "2026-08-30" for row in reviews)
    tech = {(row["nct_id"], row["decision"]): row for row in reviews if row["ticker"] == "TECH"}
    assert ("NCT06966089", "include") in tech
    assert ("NCT06349772", "exclude") in tech
    assert tech[("NCT06349772", "exclude")]["relationship_type"] == "ticker_token_collision"


def test_portfolio_layer_canonical_owner_is_med_devices() -> None:
    selected = one_by_ticker("portfolio_layer/data/canonical_sector_overrides.csv")
    assert all(row["canonical_pipeline"] == "med_devices" for row in selected.values())

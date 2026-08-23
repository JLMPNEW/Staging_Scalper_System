from __future__ import annotations

import csv
import runpy
from datetime import date
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
ONBOARDED = {
    "BBNX": ("0001674632", "08659B102", "2025-01-30", "home_chronic_care_devices_dme_drug_delivery"),
    "BLLN": ("0002070849", "090168105", "2025-11-06", "diagnostics_clinical_tests"),
    "FLGT": ("0001674930", "359664109", "2016-09-29", "diagnostics_clinical_tests"),
    "FRNM": ("0002017526", "35661P100", "2026-07-21", "diagnostics_clinical_tests"),
    "LNSR": ("0001320350", "52634L108", "2020-10-01", "elective_vision_dental_aesthetic_devices"),
    "PRPO": ("0001043961", "74019L602", "2017-06-30", "diagnostics_clinical_tests"),
}


def rows_by_ticker(path: Path) -> dict[str, list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("ticker") or "").strip().upper(), []).append(row)
    return grouped


def test_new_issuers_have_governed_security_identities_and_cohorts() -> None:
    universe = rows_by_ticker(ROOT / "ticker_mapping" / "med_dev_tickers_clean_keep.csv")
    cohorts = rows_by_ticker(ROOT / "med_devices" / "data" / "calibration_cohort_overrides.csv")
    config = yaml.safe_load((ROOT / "med_devices" / "config.yaml").read_text(encoding="utf-8"))
    identities = config["universe_validation"]["security_identity_overrides"]

    for ticker, (cik, cusip, listing_start, cohort) in ONBOARDED.items():
        assert len(universe[ticker]) == 1
        assert universe[ticker][0]["cik"] == cik
        assert universe[ticker][0]["cusip"] == cusip
        assert universe[ticker][0]["listing_start_date"] == listing_start
        assert identities[ticker]["cik"] == cik
        assert identities[ticker]["cusip"] == cusip
        assert identities[ticker]["listing_start_date"] == listing_start
        assert identities[ticker]["reviewed_at"] == "2026-08-22"
        assert cohorts[ticker][-1]["calibration_cohort"] == cohort
        assert cohorts[ticker][-1]["valid_from"] == listing_start
        assert cohorts[ticker][-1]["reviewed_at"] == "2026-08-22"


def test_new_issuers_have_effective_dated_fda_and_reimbursement_governance() -> None:
    aliases = rows_by_ticker(ROOT / "med_devices" / "data" / "fda_company_aliases.csv")
    footprints = rows_by_ticker(ROOT / "med_devices" / "data" / "fda_company_footprints.csv")
    reimbursement = rows_by_ticker(
        ROOT / "med_devices" / "data" / "reimbursement_company_classifications.csv"
    )
    manufacturer_overrides = rows_by_ticker(
        ROOT / "med_devices" / "data" / "fda_manufacturer_overrides.csv"
    )

    for ticker in ONBOARDED:
        assert aliases[ticker]
        assert aliases[ticker][-1]["reviewed_at"] == "2026-08-22"
        assert len(footprints[ticker]) == 1
        assert footprints[ticker][0]["reviewed_at"] == "2026-08-22"
        assert len(reimbursement[ticker]) == 1
        assert reimbursement[ticker][0]["reviewed_at"] == "2026-08-22"

    assert manufacturer_overrides["BBNX"][0]["fda_manufacturer_id"] == "146"
    assert manufacturer_overrides["BBNX"][0]["company_id"] == ""
    assert manufacturer_overrides["BBNX"][0]["confidence"] == "100"
    assert footprints["BBNX"][0]["product_codes"] == "QFG;QJI"
    assert footprints["LNSR"][0]["premarket_numbers"].endswith("K220259")
    assert footprints["FRNM"][0]["review_adjusted_fda_state"] == "manual_fda_footprint_ivd_lab"

def test_13f_aggregation_rejects_prelisting_ticker_history() -> None:
    module = runpy.run_path(
        str(ROOT / "med_devices" / "scripts" / "63_rebuild_med_device_sec_13f_common_share_facts.py")
    )
    holding_type = module["Holding"]
    aggregate_holdings = module["aggregate_holdings"]
    before_listing = holding_type(
        ticker="BBNX",
        period=date(2024, 12, 31),
        manager_key="before",
        cusip="08659B102",
        filing_key="before",
        filing_date=date(2025, 2, 1),
        accepted_at="2025-02-01",
        shares=100.0,
        market_value=1000.0,
        title_of_class="COM",
        share_type="SH",
    )
    after_listing = holding_type(
        ticker="BBNX",
        period=date(2025, 3, 31),
        manager_key="after",
        cusip="08659B102",
        filing_key="after",
        filing_date=date(2025, 5, 1),
        accepted_at="2025-05-01",
        shares=200.0,
        market_value=2000.0,
        title_of_class="COM",
        share_type="SH",
    )

    rows = aggregate_holdings(
        {
            ("BBNX", before_listing.period, before_listing.manager_key, before_listing.cusip): before_listing,
            ("BBNX", after_listing.period, after_listing.manager_key, after_listing.cusip): after_listing,
        },
        company_by_ticker={
            "BBNX": {
                "company_id": 1,
                "listing_start_date": "2025-01-30",
            }
        },
    )

    assert [row["report_date"] for row in rows] == ["2025-03-31"]

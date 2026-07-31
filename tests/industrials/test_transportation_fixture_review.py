from __future__ import annotations

from industrials.transportation.fixture_review import (
    review_fixture_evidence,
)


def _row(
    *,
    metric_id: str,
    source_metric_id: str,
    value: str,
    unit: str,
    period_end: str,
    text: str,
) -> dict[str, str]:
    return {
        "metric_id": metric_id,
        "source_metric_id": source_metric_id,
        "candidate_value": value,
        "unit": unit,
        "period_end": period_end,
        "evidence_text": text,
        "form_type": "",
        "filing_date": "",
        "accession_number": "",
    }


def test_top_metric_positive_fixtures() -> None:
    decision = review_fixture_evidence(
        _row(
            metric_id="revenue_days",
            source_metric_id="revenue_days",
            value="2416",
            unit="days",
            period_end="2018-03-31",
            text=(
                "Total revenue days increased to 2,416 for the three months "
                "ended March 31, 2018."
            ),
        ),
        aliases=("revenue days",),
    )
    assert decision[0] == "ACCEPT"

    decision = review_fixture_evidence(
        _row(
            metric_id="vessel_count",
            source_metric_id="vessel_count",
            value="28",
            unit="count",
            period_end="2018-09-30",
            text=(
                "As of September 30, 2018, our fleet consisted of 28 vessels."
            ),
        ),
        aliases=("vessel count", "fleet consisted of"),
    )
    assert decision[0] == "ACCEPT"

    decision = review_fixture_evidence(
        _row(
            metric_id="tce_day_rate",
            source_metric_id="tce_day_rate",
            value="34060",
            unit="USD_per_day",
            period_end="2023-12-31",
            text=(
                "For the quarter ended December 31, 2023, we achieved a "
                "fleetwide time charter equivalent rate of $34,060 per day."
            ),
        ),
        aliases=("time charter equivalent rate", "TCE rate"),
    )
    assert decision[0] == "ACCEPT"


def test_top_metric_prohibited_fixtures() -> None:
    decision = review_fixture_evidence(
        _row(
            metric_id="average_length_of_haul",
            source_metric_id="average_length_of_haul",
            value="3",
            unit="distance",
            period_end="2025-12-31",
            text=(
                "For the year ended December 31, 2025, a 3.0% decline "
                "in profit per shipment was driven by a "
                "shorter average length of haul."
            ),
        ),
        aliases=("average length of haul",),
    )
    assert decision[0] == "REJECT"

    decision = review_fixture_evidence(
        _row(
            metric_id="tce_day_rate",
            source_metric_id="tce_day_rate",
            value="2024",
            unit="USD_per_day",
            period_end="2024-12-31",
            text=(
                "Our time charter equivalent rate in 2024 increased "
                "compared with 2023."
            ),
        ),
        aliases=("time charter equivalent rate", "TCE rate"),
    )
    assert decision[0] == "REJECT"

    decision = review_fixture_evidence(
        _row(
            metric_id="revenue_days",
            source_metric_id="revenue_days",
            value="30",
            unit="days",
            period_end="2014-12-31",
            text=(
                "For the year ended December 31, 2014, revenue "
                "calculations assume 365 revenue days per ship per annum, "
                "with 30 off-hire days."
            ),
        ),
        aliases=("revenue days",),
    )
    assert decision[0] == "REJECT"


def test_period_mismatch_and_derived_source_dispatch_are_fail_closed() -> None:
    decision = review_fixture_evidence(
        _row(
            metric_id="vessel_count",
            source_metric_id="vessel_count",
            value="44",
            unit="count",
            period_end="2023-07-20",
            text=(
                "As of February 24, 2023, our fleet consisted of "
                "44 vessels."
            ),
        ),
        aliases=("vessel count", "fleet consisted of"),
    )
    assert decision[0] == "DEFER"

    decision = review_fixture_evidence(
        _row(
            metric_id="tce_day_rate",
            source_metric_id="tce_day_rate",
            value="28944",
            unit="USD_per_day",
            period_end="2008-03-17",
            text=(
                "Vessels earned a blended average time charter equivalent "
                "rate of $28,944 per day."
            ),
        ),
        aliases=("time charter equivalent rate", "TCE rate"),
    )
    assert decision[0] == "DEFER"

    decision = review_fixture_evidence(
        _row(
            metric_id="fleet_capacity_growth",
            source_metric_id="fleet_capacity",
            value="157000",
            unit="segment_native_capacity",
            period_end="2025-12-31",
            text=(
                "We entered into an agreement to acquire an additional "
                "newbuild vessel of approximately 157,000 dwt."
            ),
        ),
        aliases=("fleet capacity", "deadweight tonnage"),
    )
    assert decision[0] == "REJECT"


def test_explicit_single_value_metric_rules() -> None:
    fixtures = (
        (
            "completion_factor",
            "0.99",
            "ratio",
            "2017-12-31",
            "For 2017, the system completion factor was 99.0%.",
        ),
        (
            "fleet_age",
            "9",
            "years",
            "2020-12-31",
            (
                "For the year ended December 31, 2020, our average LPG "
                "fleet age is 9 years."
            ),
        ),
        (
            "charter_coverage_next_12m",
            "0.822",
            "ratio",
            "2020-06-30",
            (
                "As of June 30, 2020, contracted revenues represented "
                "82.2% charter coverage for the next 12 months."
            ),
        ),
        (
            "rail_intermodal_volume_growth",
            "0.06",
            "ratio",
            "2006-03-31",
            (
                "For the first quarter of 2006, volume growth was led by "
                "intermodal volume growth of 6 percent."
            ),
        ),
    )
    for metric_id, value, unit, period_end, text in fixtures:
        decision = review_fixture_evidence(
            _row(
                metric_id=metric_id,
                source_metric_id=metric_id,
                value=value,
                unit=unit,
                period_end=period_end,
                text=text,
            ),
                aliases=(
                    metric_id.replace("_", " "),
                    "charter coverage",
                    "intermodal volume growth",
                ),
        )
        assert decision[0] == "ACCEPT", (metric_id, decision)

    ambiguous = review_fixture_evidence(
        _row(
            metric_id="rail_intermodal_volume_growth",
            source_metric_id="rail_intermodal_volume_growth",
            value="0.08",
            unit="ratio",
            period_end="2018-03-31",
            text=(
                "For the first quarter of 2018, LTL and intermodal volume "
                "growth was 8 percent and 3 percent, respectively."
            ),
        ),
        aliases=("intermodal volume growth",),
    )
    assert ambiguous[0] == "DEFER"

    decision = review_fixture_evidence(
        _row(
            metric_id="fuel_surcharge_revenue_ratio",
            source_metric_id="fuel_surcharge_revenue_ratio",
            value="0.18",
            unit="ratio",
            period_end="2022-12-31",
            text=(
                "For the year ended December 31, 2022, freight revenue "
                "per RTM increased by 18%, mainly driven by "
                "higher fuel surcharge revenue."
            ),
        ),
        aliases=("fuel surcharge revenue",),
    )
    assert decision[0] == "REJECT"


def test_unlock_metric_positive_fixtures() -> None:
    fixtures = (
        (
            "passenger_load_factor",
            "0.812",
            "For the first quarter ended March 31, 2024, average load "
            "factor was 81.2 percent.",
        ),
        (
            "capacity_growth",
            "0.157",
            "For the six months ended June 30, 2025, revenue increased "
            "on capacity growth of 15.7 percent year-over-year.",
        ),
        (
            "equipment_utilization",
            "0.973",
            "At March 31, 2024, equipment utilization was 97.3 percent.",
        ),
        (
            "fleet_utilization",
            "0.968",
            "For the year ended December 31, 2024, fleet utilization "
            "decreased from 97.3 percent in 2023 to 96.8 percent in 2024.",
        ),
    )
    periods = {
        "passenger_load_factor": "2024-03-31",
        "capacity_growth": "2025-06-30",
        "equipment_utilization": "2024-03-31",
        "fleet_utilization": "2024-12-31",
    }
    for metric_id, value, text in fixtures:
        decision = review_fixture_evidence(
            _row(
                metric_id=metric_id,
                source_metric_id=metric_id,
                value=value,
                unit="ratio",
                period_end=periods[metric_id],
                text=text,
            ),
            aliases=(
                metric_id.replace("_", " "),
                "load factor",
                "capacity growth",
                "equipment utilization",
                "fleet utilization",
            ),
        )
        assert decision[0] == "ACCEPT", (metric_id, decision)


def test_unlock_metric_comparators_and_false_positives_fail_closed() -> None:
    prior_fleet = review_fixture_evidence(
        _row(
            metric_id="fleet_utilization",
            source_metric_id="fleet_utilization",
            value="0.973",
            unit="ratio",
            period_end="2024-12-31",
            text=(
                "For the year ended December 31, 2024, fleet utilization "
                "decreased from 97.3 percent in 2023 to 96.8 percent in 2024."
            ),
        ),
        aliases=("fleet utilization",),
    )
    assert prior_fleet[0] == "DEFER"

    per_asm = review_fixture_evidence(
        _row(
            metric_id="capacity_growth",
            source_metric_id="capacity_growth",
            value="0.085",
            unit="ratio",
            period_end="2019-06-30",
            text=(
                "For the quarter ended June 30, 2019, aircraft rent per "
                "ASM increased 8.5 percent."
            ),
        ),
        aliases=("capacity growth", "ASM"),
    )
    assert per_asm[0] != "ACCEPT"

    monthly_segment = review_fixture_evidence(
        _row(
            metric_id="passenger_load_factor",
            source_metric_id="passenger_load_factor",
            value="0.84",
            unit="ratio",
            period_end="2021-06-30",
            text=(
                "For the quarter ended June 30, 2021, domestic load factor "
                "for the month of May was approximately 84 percent."
            ),
        ),
        aliases=("load factor",),
    )
    assert monthly_segment[0] == "DEFER"

    release_date = review_fixture_evidence(
        _row(
            metric_id="passenger_load_factor",
            source_metric_id="passenger_load_factor",
            value="0.80",
            unit="ratio",
            period_end="2023-04-27",
            text=(
                "In the first quarter of 2023, average load factor "
                "was 80 percent."
            ),
        ),
        aliases=("load factor",),
    )
    assert release_date[0] == "DEFER"

    prior_respectively = review_fixture_evidence(
        _row(
            metric_id="fleet_utilization",
            source_metric_id="fleet_utilization",
            value="0.955",
            unit="ratio",
            period_end="2022-12-31",
            text=(
                "During 2020, 2021, and 2022 we had fleet utilization of "
                "95.5%, 98.5% and 98.3%, respectively."
            ),
        ),
        aliases=("fleet utilization",),
    )
    assert prior_respectively[0] == "DEFER"

    current_respectively = review_fixture_evidence(
        _row(
            metric_id="fleet_utilization",
            source_metric_id="fleet_utilization",
            value="0.983",
            unit="ratio",
            period_end="2022-12-31",
            text=(
                "During 2020, 2021, and 2022 we had fleet utilization of "
                "95.5%, 98.5% and 98.3%, respectively."
            ),
        ),
        aliases=("fleet utilization",),
    )
    assert current_respectively[0] == "ACCEPT"

    trusted_filing_row = _row(
        metric_id="passenger_load_factor",
        source_metric_id="passenger_load_factor",
        value="0.869",
        unit="ratio",
        period_end="2018-09-30",
        text=(
            "Passenger revenue increased compared to the September 2017 "
            "quarter. Load factor was flat to the prior year quarter "
            "at 86.9 percent."
        ),
    )
    trusted_filing_row.update(
        {
            "form_type": "10-Q",
            "filing_date": "2018-10-12",
            "accession_number": "0000000000-18-000001",
        }
    )
    trusted = review_fixture_evidence(
        trusted_filing_row,
        aliases=("load factor",),
    )
    assert trusted[0] == "ACCEPT"

    decision = review_fixture_evidence(
        _row(
            metric_id="tce_day_rate",
            source_metric_id="tce_day_rate",
            value="14226",
            unit="USD_per_day",
                period_end="2017-12-31",
                text=(
                    "For the year ended December 31, 2017, the estimated "
                    "daily time charter equivalent rate used in our "
                    "impairment analysis was $14,226."
                ),
        ),
        aliases=("time charter equivalent rate",),
    )
    assert decision[0] == "REJECT"

from __future__ import annotations

from industrials.transportation.contemporaneous_metric_coverage import (
    DomainRule,
    audit_contemporaneous_coverage,
)


def _row(
    ticker: str,
    *,
    filing_date: str,
    period_end: str,
    definition: str = "exact_ltl",
) -> dict[str, object]:
    return {
        "ticker": ticker,
        "metric_id": "operating_ratio",
        "filing_date": filing_date,
        "accepted_at": filing_date + "T20:00:00Z",
        "period_end": period_end,
        "unit": "ratio",
        "concept_name": "ReportedOperatingRatio",
        "formula": "",
        "comparability_class": definition,
        "definition_basis": "operating_expenses_divided_by_operating_revenue",
        "replay_status": "ACCEPTED",
    }


def test_future_filing_cannot_satisfy_an_earlier_score_date() -> None:
    rule = DomainRule(
        cohort="surface",
        metric_id="operating_ratio",
        domain_id="ltl",
        tickers=("A", "B", "C"),
        minimum_breadth=2,
    )
    rows = [
        _row("A", filing_date="2020-02-01", period_end="2019-12-31"),
        _row("B", filing_date="2020-04-01", period_end="2019-12-31"),
    ]

    detail, summary, manifest = audit_contemporaneous_coverage(
        score_dates=("2020-03-01",),
        rules=(rule,),
        accepted_rows=rows,
        max_staleness_days={"operating_ratio": 500},
    )

    assert detail[0]["accepted_compatible_breadth"] == 1
    assert detail[0]["date_gate"] == "FAIL"
    assert detail[0]["future_only_tickers"] == "B"
    assert summary[0]["calibration_gate"] == "FAIL"
    assert manifest["point_in_time_availability_enforced"]


def test_incompatible_definitions_do_not_combine_to_clear_breadth() -> None:
    rule = DomainRule(
        cohort="surface",
        metric_id="operating_ratio",
        domain_id="ltl",
        tickers=("A", "B", "C"),
        minimum_breadth=3,
    )
    rows = [
        _row("A", filing_date="2020-02-01", period_end="2019-12-31"),
        _row("B", filing_date="2020-02-01", period_end="2019-12-31"),
        _row(
            "C",
            filing_date="2020-02-01",
            period_end="2019-12-31",
            definition="broad_proxy",
        ),
    ]

    detail, _, _ = audit_contemporaneous_coverage(
        score_dates=("2020-03-01",),
        rules=(rule,),
        accepted_rows=rows,
        max_staleness_days={"operating_ratio": 500},
    )

    assert detail[0]["accepted_compatible_breadth"] == 2
    assert detail[0]["incompatible_definition_tickers"] == "C"
    assert detail[0]["date_gate"] == "FAIL"


def test_metric_requires_date_fraction_and_latest_date_to_pass() -> None:
    rule = DomainRule(
        cohort="surface",
        metric_id="operating_ratio",
        domain_id="ltl",
        tickers=("A", "B"),
        minimum_breadth=2,
    )
    rows = [
        _row("A", filing_date="2020-02-01", period_end="2019-12-31"),
        _row("B", filing_date="2020-02-01", period_end="2019-12-31"),
    ]

    detail, summary, _ = audit_contemporaneous_coverage(
        score_dates=("2020-03-01", "2021-12-31"),
        rules=(rule,),
        accepted_rows=rows,
        max_staleness_days={"operating_ratio": 500},
        minimum_date_pass_fraction=0.5,
    )

    assert [row["date_gate"] for row in detail] == ["PASS", "FAIL"]
    assert summary[0]["passing_score_date_fraction"] == 0.5
    assert summary[0]["latest_date_gate"] == "FAIL"
    assert summary[0]["calibration_gate"] == "FAIL"

def test_impossible_breadth_is_allowed_only_for_diagnostic_domains() -> None:
    diagnostic = DomainRule(
        cohort="surface",
        metric_id="operating_ratio",
        domain_id="integrated_parcel",
        tickers=("FDX", "UPS"),
        minimum_breadth=3,
        calibration_eligibility="DIAGNOSTIC_ONLY",
    )

    detail, summary, manifest = audit_contemporaneous_coverage(
        score_dates=("2020-03-01",),
        rules=(diagnostic,),
        accepted_rows=(),
        max_staleness_days={"operating_ratio": 500},
    )

    assert detail[0]["date_gate"] == "FAIL"
    assert summary[0]["calibration_gate"] == "FAIL"
    assert manifest["calibration_accepted_metric_count"] == 0


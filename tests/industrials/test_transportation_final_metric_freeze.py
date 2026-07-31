from __future__ import annotations

from industrials.transportation.final_metric_freeze import (
    build_final_metric_dispositions,
)


def _row(
    *,
    ticker: str,
    metric_id: str,
    status: str,
    source_lane: str = "DP",
    universe_role: str = "active",
    periods: int = 4,
    first: str = "2020-12-31",
    last: str = "2024-12-31",
) -> dict[str, object]:
    return {
        "ticker": ticker,
        "metric_id": metric_id,
        "metric_pack": "surface",
        "source_lane": source_lane,
        "universe_role": universe_role,
        "primary_archetype": "surface_trucking",
        "applicability_status": "APPLICABLE",
        "coverage_status": status,
        "distinct_period_count": periods,
        "first_period_end": first,
        "last_period_end": last,
    }


def test_final_metric_freeze_selects_only_all_gate_passes() -> None:
    coverage: list[dict[str, object]] = [
        _row(
            ticker=f"A{index}",
            metric_id="operating_ratio",
            status="COVERED_ACCEPTED",
        )
        for index in range(5)
    ]
    coverage.append(
        _row(
            ticker="OLD",
            metric_id="operating_ratio",
            status="COVERED_ACCEPTED",
            universe_role="delisted_usable",
        )
    )
    coverage.extend(
        _row(
            ticker=f"R{index}",
            metric_id="fleet_age",
            status="COVERED_REVIEW_REQUIRED",
        )
        for index in range(5)
    )
    coverage.extend(
        _row(
            ticker=f"F{index}",
            metric_id="cash_runway_years",
            status="COVERED_FINANCIAL_DERIVED",
            source_lane="FIN-D",
        )
        for index in range(5)
    )

    rows = build_final_metric_dispositions(
        coverage_rows=coverage,
        policy_golden_validated=True,
        accepted_periods={
            (str(row["ticker"]), str(row["metric_id"])): {
                "2020-12-31",
                "2021-12-31",
                "2022-12-31",
                "2024-12-31",
            }
            for row in coverage
        },
    )
    by_metric = {str(row["metric_id"]): row for row in rows}

    assert by_metric["operating_ratio"]["calibration_candidate"] == 1
    assert (
        by_metric["operating_ratio"]["metric_disposition"]
        == "CALIBRATION_CANDIDATE"
    )
    assert by_metric["fleet_age"]["calibration_candidate"] == 0
    assert by_metric["fleet_age"]["metric_disposition"] == "DEFERRED_REVIEW"
    assert by_metric["cash_runway_years"]["calibration_candidate"] == 0
    assert by_metric["cash_runway_years"]["metric_disposition"] == "DIAGNOSTIC_ONLY"


def test_final_metric_freeze_requires_policy_golden_validation() -> None:
    coverage = [
        _row(
            ticker=f"A{index}",
            metric_id="operating_ratio",
            status="COVERED_ACCEPTED",
        )
        for index in range(5)
    ]
    rows = build_final_metric_dispositions(
        coverage_rows=coverage,
        policy_golden_validated=False,
        accepted_periods={
            (str(row["ticker"]), str(row["metric_id"])): {
                "2020-12-31",
                "2021-12-31",
                "2022-12-31",
                "2024-12-31",
            }
            for row in coverage
        },
    )

    assert rows[0]["accepted_breadth_gate_pass"] == 1
    assert rows[0]["evidence_precision_gate_pass"] == 0
    assert rows[0]["calibration_candidate"] == 0

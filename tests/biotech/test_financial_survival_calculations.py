from __future__ import annotations

from datetime import date

import pytest

from biotech_index.core.financial_survival import cash_runway_is_reliable
from tests.biotech.conftest import load_script_module


def ocf_row(
    *,
    period_end: str,
    fiscal_year: int,
    fiscal_period: str,
    value: float,
    duration_days: int,
    form: str,
    cash_and_investments: float | None = None,
) -> dict[str, object]:
    return {
        "period_end": period_end,
        "filed_date": period_end,
        "fiscal_year": fiscal_year,
        "fiscal_period": fiscal_period,
        "form": form,
        "operating_cash_flow": value,
        "operating_cash_flow_duration_days": duration_days,
        "cash_and_investments": cash_and_investments,
        "proxy_fields_used": "",
    }


def insm_cash_flow_rows() -> list[dict[str, object]]:
    return [
        ocf_row(
            period_end="2026-06-30",
            fiscal_year=2026,
            fiscal_period="Q2",
            value=-311_566_000.0,
            duration_days=180,
            form="10-Q",
            cash_and_investments=1_160_265_000.0,
        ),
        ocf_row(
            period_end="2026-03-31",
            fiscal_year=2026,
            fiscal_period="Q1",
            value=-222_738_000.0,
            duration_days=89,
            form="10-Q",
        ),
        ocf_row(
            period_end="2025-12-31",
            fiscal_year=2025,
            fiscal_period="FY",
            value=-935_014_000.0,
            duration_days=364,
            form="10-K",
        ),
        ocf_row(
            period_end="2025-09-30",
            fiscal_year=2025,
            fiscal_period="Q3",
            value=-687_418_000.0,
            duration_days=272,
            form="10-Q",
        ),
        ocf_row(
            period_end="2025-06-30",
            fiscal_year=2025,
            fiscal_period="Q2",
            value=-467_657_000.0,
            duration_days=180,
            form="10-Q",
        ),
    ]


def test_companyfacts_prefers_explicit_current_investments_over_overlapping_total() -> None:
    module = load_script_module(
        "15_sync_sec_companyfacts_history.py",
        "companyfacts_cash_overlap_regression",
    )

    def observation(concept: str, value: float) -> dict[str, object]:
        return {
            "concept": concept,
            "value": value,
            "period_end": "2026-06-30",
            "fiscal_year": 2026,
            "fiscal_period": "Q2",
            "form": "10-Q",
            "filed_date": "2026-08-05",
            "accession_nodash": "000000000026000001",
        }

    rows = module.normalize_rows(
        [
            observation("CashAndCashEquivalentsAtCarryingValue", 815_435_000.0),
            observation("MarketableSecuritiesCurrent", 3_122_534_000.0),
            observation("AvailableForSaleSecuritiesDebtSecurities", 3_882_973_000.0),
        ],
        company_id=1,
    )

    assert rows[0]["marketable_investments_total"] == pytest.approx(3_122_534_000.0)
    assert rows[0]["cash_and_investments"] == pytest.approx(3_937_969_000.0)
    assert "ignored_reported_investments_total_overlap_risk" in rows[0]["proxy_fields_used"]


def test_companyfacts_preserves_operating_cash_flow_duration_metadata() -> None:
    module = load_script_module(
        "15_sync_sec_companyfacts_history.py",
        "companyfacts_ocf_duration_regression",
    )
    rows = module.normalize_rows(
        [
            {
                "concept": "NetCashProvidedByUsedInOperatingActivities",
                "value": -311_566_000.0,
                "period_start": "2026-01-01",
                "period_end": "2026-06-30",
                "duration_days": 180,
                "fiscal_year": 2026,
                "fiscal_period": "Q2",
                "form": "10-Q",
                "filed_date": "2026-08-06",
                "accession_nodash": "000000000026000002",
            }
        ],
        company_id=1,
    )

    assert rows[0]["operating_cash_flow_period_start"] == "2026-01-01"
    assert rows[0]["operating_cash_flow_duration_days"] == 180


def test_ttm_cash_burn_uses_discrete_quarters_instead_of_summing_ytd_values() -> None:
    module = load_script_module(
        "16_build_financial_survival_features.py",
        "financial_survival_discrete_ocf_regression",
    )
    proxies: list[str] = []
    missing: list[str] = []

    quarterly_burn, ttm_burn, ocf_ttm = module.burn_metrics(
        insm_cash_flow_rows(),
        proxies,
        missing,
        asof_date=date(2026, 8, 20),
    )

    assert quarterly_burn == pytest.approx(88_828_000.0)
    assert ocf_ttm == pytest.approx(-778_923_000.0)
    assert ttm_burn == pytest.approx(778_923_000.0)
    assert "partial_quarter_annualized_operating_cash_flow" not in proxies
    assert "annualized_ytd_operating_cash_flow" not in proxies


def test_unreconciled_ytd_cash_flow_cannot_support_a_hard_runway_veto() -> None:
    module = load_script_module(
        "16_build_financial_survival_features.py",
        "financial_survival_unreconciled_ytd_regression",
    )
    proxies: list[str] = []
    missing: list[str] = []
    rows = [
        ocf_row(
            period_end="2026-06-30",
            fiscal_year=2026,
            fiscal_period="Q2",
            value=-311_566_000.0,
            duration_days=180,
            form="10-Q",
        )
    ]

    quarterly_burn, ttm_burn, _ = module.burn_metrics(
        rows,
        proxies,
        missing,
        asof_date=date(2026, 8, 20),
    )

    assert quarterly_burn == pytest.approx(155_783_000.0)
    assert ttm_burn == pytest.approx(623_132_000.0)
    assert "annualized_ytd_operating_cash_flow" in proxies
    assert cash_runway_is_reliable({"proxy_fields_used": proxies}) is False


def test_ttm_cash_flow_does_not_bridge_missing_fiscal_quarters() -> None:
    module = load_script_module(
        "16_build_financial_survival_features.py",
        "financial_survival_cash_flow_gap_regression",
    )
    rows = [
        ocf_row(
            period_end="2026-06-30",
            fiscal_year=2026,
            fiscal_period="Q2",
            value=-300.0,
            duration_days=180,
            form="10-Q",
        ),
        ocf_row(
            period_end="2026-03-31",
            fiscal_year=2026,
            fiscal_period="Q1",
            value=-200.0,
            duration_days=89,
            form="10-Q",
        ),
        ocf_row(
            period_end="2024-09-30",
            fiscal_year=2024,
            fiscal_period="Q3",
            value=-450.0,
            duration_days=272,
            form="10-Q",
        ),
        ocf_row(
            period_end="2024-06-30",
            fiscal_year=2024,
            fiscal_period="Q2",
            value=-300.0,
            duration_days=180,
            form="10-Q",
        ),
    ]
    proxies: list[str] = []

    ocf_ttm = module.ttm_amount(
        rows,
        "operating_cash_flow",
        proxies,
        asof_date=date(2026, 8, 20),
    )

    assert ocf_ttm == pytest.approx(-600.0)
    assert "partial_quarter_annualized_operating_cash_flow" in proxies



def test_historical_survival_ignores_undated_current_screen_rows(tmp_path) -> None:
    module = load_script_module(
        "16_build_financial_survival_features.py",
        "financial_survival_screen_pit_regression",
    )
    screen_path = tmp_path / "screen.csv"
    screen_path.write_text("ticker,going_concern_status\nTEST,confirmed\n", encoding="utf-8")

    rows = module.screen_rows_for_asof(
        screen_path,
        asof_date=date(2024, 1, 2),
        current_date=date(2026, 8, 23),
    )

    assert rows == {}


def test_going_concern_hard_status_requires_periodic_filing() -> None:
    module = load_script_module(
        "16_build_financial_survival_features.py",
        "financial_survival_going_concern_form_regression",
    )

    assert module.going_concern_status_for_form("S-1") == "possible"
    assert module.going_concern_status_for_form("424B4") == "possible"
    assert module.going_concern_status_for_form("10-Q/A") == "confirmed"
    assert module.going_concern_status_for_form("10-K") == "confirmed"


def test_reliable_long_runway_resolves_stale_going_concern_warning() -> None:
    module = load_script_module(
        "16_build_financial_survival_features.py",
        "financial_survival_going_concern_runway_regression",
    )
    rows = insm_cash_flow_rows()
    rows[0]["cash_and_investments"] = 2_320_530_000.0

    survival = module.compute_survival_row(
        company={"company_id": 1, "ticker": "TEST", "company_name": "Test Biotech"},
        rows=rows,
        screen_row={},
        asof_date=date(2026, 8, 20),
        dilution_events={},
        going_concern_status="confirmed",
        config={},
    )

    assert survival["cash_runway_reliable_flag"] == 1
    assert survival["cash_runway_months"] > 18.0
    assert survival["going_concern_status"] == "resolved"

def test_corrected_insm_runway_removes_false_structural_veto() -> None:
    survival_module = load_script_module(
        "16_build_financial_survival_features.py",
        "financial_survival_downstream_regression",
    )
    score_module = load_script_module(
        "11_score_biotech_index.py",
        "financial_survival_veto_downstream_regression",
    )
    survival = survival_module.compute_survival_row(
        company={"company_id": 1, "ticker": "INSM", "company_name": "Insmed"},
        rows=insm_cash_flow_rows(),
        screen_row={},
        asof_date=date(2026, 8, 20),
        dilution_events={},
        going_concern_status="",
        config={},
    )

    assert survival["cash_runway_months"] == pytest.approx(17.8749119)
    assert survival["cash_runway_reliable_flag"] == 1
    reasons = score_module.core_structural_veto_reasons(
        {"financial_survival": survival},
        {"commercial_stage_flag": 1.0, "ttm_revenue": 981_207_000.0},
        {
            "enabled": True,
            "reasons": {"cash_runway_lt_9m", "severe_runway_flag"},
            "min_addv20": 0.0,
            "commercial_stage_revenue_min": 50_000_000.0,
        },
    )
    assert reasons == []

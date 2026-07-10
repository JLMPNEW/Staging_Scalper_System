from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from technology.core.oos_provenance import build_oos_provenance
from technology.core.portfolio_candidate_fields import (
    add_portfolio_candidate_fields,
    validate_portfolio_candidate_rows,
)
from technology.core.refresh_orchestration import asof_governance_conflict
from technology.core.signal_diagnostics import (
    financial_subfeatures as shared_financial_subfeatures,
)
from technology.core.signal_diagnostics import load_prices


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_script(relative_path: str, module_name: str) -> ModuleType:
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def semiconductor_diagnostics() -> ModuleType:
    return load_script(
        "technology/semiconductors/scripts/07_run_semiconductor_signal_diagnostics.py",
        "technology_semiconductor_diagnostics_regression",
    )


@pytest.fixture(scope="module")
def yahoo_prices() -> ModuleType:
    return load_script(
        "technology/scripts/03_sync_technology_yahoo_adjusted_prices.py",
        "technology_yahoo_prices_regression",
    )


def oos_config() -> dict[str, Any]:
    return {
        "oos_calibration_standards": {
            "allow_replay_oos_within_days": 5,
            "families": {
                "semiconductors": {
                    "calibration_train_start_date": "2011-01-01",
                    "calibration_train_end_date": "2020-12-31",
                    "calibration_lock_date": "2021-01-01",
                    "calibration_production_start_date": "2021-01-01",
                    "calibration_validation_method": "test",
                }
            },
        }
    }


def candidate_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ticker": "TEST",
        "asof_date": "2024-01-02",
        "final_score": 61.0,
        "rank_ready_flag": 1,
        "calibration_eligible_flag": 1,
        "model_status": "complete",
        "oos_score_valid_flag": 0,
        "oos_invalid_reason": "pre_lock",
        "feature_point_in_time_flag": 1,
        "future_return_excluded_flag": 1,
        "calibration_usage": "calibration_input_only",
        "calibration_input_valid_flag": 1,
        "market_feature_asof_date": "2024-01-02",
        "financial_feature_asof_date": "2023-11-01",
        "positioning_feature_asof_date": "2023-12-20",
        "data_quality_confidence": 0.8,
        "survivorship_corrected_panel_flag": 1,
        "score_recomputed_pit_flag": 1,
    }
    row.update(overrides)
    return row


def test_future_current_snapshot_is_never_oos_valid() -> None:
    future = (date.today() + timedelta(days=1)).isoformat()
    provenance = build_oos_provenance(
        oos_config(),
        model_family="semiconductors",
        asof=future,
        historical_mode=False,
    )
    assert provenance.row_fields["oos_score_valid_flag"] == 0
    assert "asof_date_in_future" in provenance.row_fields["oos_invalid_reason"]


def test_candidate_metadata_never_invents_missing_price_provenance() -> None:
    row = candidate_row(market_feature_asof_date="", latest_price_date="")
    result = add_portfolio_candidate_fields([row])[0]
    assert result["research_calibration_input_eligible_flag"] == 0
    assert result["stage11_calibration_input_eligible_flag"] == 0
    assert "missing_price_data_asof" in result["research_calibration_reason"]


def test_survivorship_panel_requires_pit_score_recompute() -> None:
    invalid = add_portfolio_candidate_fields([candidate_row(score_recomputed_pit_flag=0)])[0]
    valid = add_portfolio_candidate_fields([candidate_row(score_recomputed_pit_flag=1)])[0]
    assert invalid["stage11_calibration_input_eligible_flag"] == 0
    assert invalid["stage11_calibration_input_reason"] == "survivorship_panel_score_not_recomputed_pit"
    assert valid["stage11_calibration_input_eligible_flag"] == 1
    assert valid["stage11_calibration_input_reason"] == "ok"


def test_portfolio_validator_detects_gate_and_source_date_inconsistency() -> None:
    row = add_portfolio_candidate_fields([candidate_row()])[0]
    row["portfolio_candidate_gate"] = 1
    row["financial_data_asof_date"] = "2024-01-03"
    errors = validate_portfolio_candidate_rows([row])
    assert any("portfolio_candidate_gate" in error for error in errors)
    assert any("financial_data_asof_date" in error for error in errors)


def test_portfolio_validator_rejects_malformed_provenance_dates() -> None:
    row = add_portfolio_candidate_fields([candidate_row()])[0]
    row["price_data_asof_date"] = "not-a-date"
    errors = validate_portfolio_candidate_rows([row])
    assert any("invalid price_data_asof_date" in error for error in errors)


def test_diagnostics_price_loader_selects_one_continuous_source() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE fact_price_ohlcv(
            ticker TEXT, bar_date TEXT, source_id TEXT,
            adj_close REAL, close REAL, volume REAL
        )
        """
    )
    conn.executemany(
        "INSERT INTO fact_price_ohlcv VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("TEST", "2024-01-02", "source_a", 10.0, 10.0, 100.0),
            ("TEST", "2024-01-03", "source_a", 11.0, 11.0, 100.0),
            ("TEST", "2024-01-04", "source_b", 100.0, 100.0, 100.0),
            ("TEST", "2024-01-05", "source_b", 101.0, 101.0, 100.0),
        ],
    )
    series = load_prices(conn, ["source_a", "source_b"], ["TEST"])["TEST"]
    assert series.source_id == "source_b"
    assert series.available_source_ids == ["source_a", "source_b"]
    assert series.dates == [date(2024, 1, 4), date(2024, 1, 5)]
    assert series.adj == [100.0, 101.0]


@pytest.mark.parametrize("implementation", ["shared", "semiconductor"])
def test_financial_amendment_cannot_shadow_newer_period(
    implementation: str,
    semiconductor_diagnostics: ModuleType,
) -> None:
    rows = [
        {
            "asof_date": "2024-05-01",
            "fiscal_period_end": "2024-03-31",
            "gross_margin": 0.40,
        },
        {
            "asof_date": "2024-06-01",
            "fiscal_period_end": "2023-12-31",
            "gross_margin": 0.20,
        },
    ]
    function = (
        shared_financial_subfeatures
        if implementation == "shared"
        else semiconductor_diagnostics.financial_subfeatures
    )
    result = function(rows, "2024-06-15")
    assert result["gross_margin"] == pytest.approx(0.40)
    assert result["_val_asof"] == "2024-05-01"


def test_semiconductor_form4_joint_filing_is_counted_once(
    semiconductor_diagnostics: ModuleType,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE fact_sec_form4_transaction(
            ticker TEXT, source_id TEXT, accession_number TEXT,
            nonderiv_trans_sk TEXT, filing_date TEXT, transaction_date TEXT,
            transaction_value REAL, is_open_market_purchase INTEGER,
            is_open_market_sale INTEGER, rptowner_cik TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO fact_sec_form4_transaction VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("TEST", "direct", "A1", "T1", "2024-01-05", "2024-01-03", 100.0, 1, 0, "1"),
            ("TEST", "direct", "A1", "T1", "2024-01-05", "2024-01-03", 100.0, 1, 0, "2"),
        ],
    )
    events = semiconductor_diagnostics.load_form4(conn, "direct", "upstream")["TEST"]
    assert len(events) == 1
    assert events[0][3] == ("1", "2")
    features = semiconductor_diagnostics.insider_subfeatures(events, "2024-01-10", 90)
    assert features["insider_net_value_90d"] == pytest.approx(100.0)
    assert features["insider_cluster_buyers_90d"] == pytest.approx(2.0)


def test_semiconductor_short_interest_missing_publication_uses_lag(
    semiconductor_diagnostics: ModuleType,
) -> None:
    short = {
        "TEST": [
            {
                "settlement_date": "2024-01-02",
                "publication_date": "",
                "short_interest_pct_float": 0.1,
                "days_to_cover": 2.0,
            }
        ]
    }
    before = semiconductor_diagnostics.positioning_subfeatures(
        "TEST", "2024-01-10", form4={}, inst={}, short=short, borrow={}
    )
    after = semiconductor_diagnostics.positioning_subfeatures(
        "TEST", "2024-01-16", form4={}, inst={}, short=short, borrow={}
    )
    assert "latest_short_interest_pct_float" not in before
    assert after["latest_short_interest_pct_float"] == pytest.approx(0.1)


def test_yahoo_parser_skips_malformed_corporate_action_dates(yahoo_prices: ModuleType) -> None:
    payload = {
        "chart": {
            "error": None,
            "result": [
                {
                    "meta": {},
                    "timestamp": [1704153600],
                    "indicators": {
                        "quote": [{"close": [10.0], "open": [9.0], "high": [11.0], "low": [8.0], "volume": [100]}],
                        "adjclose": [{"adjclose": [10.0]}],
                    },
                    "events": {
                        "dividends": {
                            "bad_missing": {"amount": 0.2},
                            "bad_overflow": {"date": 10**100, "amount": 0.3},
                        },
                        "splits": {"bad": {"date": None, "numerator": 2, "denominator": 1}},
                    },
                }
            ],
        }
    }
    bars, actions, _meta, error = yahoo_prices.parse_chart_result(
        yahoo_prices.PriceJob("TEST", "Test"),
        json.dumps(payload),
        "yahoo_finance_adjusted",
    )
    assert error == ""
    assert len(bars) == 1
    assert actions == []


def test_financial_batch_runner_rejects_invalid_batch_size() -> None:
    runner = load_script(
        "technology/scripts/08_build_technology_financial_features_batched.py",
        "technology_financial_batch_runner_regression",
    )
    assert runner.chunks(["A", "B", "C"], 2) == [["A", "B"], ["C"]]
    with pytest.raises(ValueError, match="batch-size"):
        runner.chunks(["A"], 0)


@pytest.mark.parametrize(
    ("script_path", "module_name", "model_family"),
    [
        (
            "technology/semiconductors/scripts/17_run_semiconductor_refresh_pipeline.py",
            "technology_semiconductor_refresh_regression",
            "semiconductors",
        ),
        (
            "technology/software_infrastructure/scripts/17_run_software_infrastructure_refresh_pipeline.py",
            "technology_software_refresh_regression",
            "software_infrastructure",
        ),
        (
            "technology/technology_hardware/scripts/17_run_technology_hardware_refresh_pipeline.py",
            "technology_hardware_refresh_regression",
            "technology_hardware",
        ),
    ],
)
def test_refresh_orchestrators_use_recoverable_financial_batches(
    script_path: str,
    module_name: str,
    model_family: str,
) -> None:
    module = load_script(script_path, module_name)
    kwargs: dict[str, Any] = {
        "asof": "2026-07-08",
        "skip_ibkr_borrow": False,
        "force_refresh": False,
        "financial_batch_size": 7,
        "financial_batch_timeout_sec": 900.0,
    }
    if model_family == "semiconductors":
        kwargs["manual_wsts_xlsx"] = None
    steps = module.build_steps(**kwargs)
    step = next(item for item in steps if item.step_id == "08_build_financial_features")
    assert step.script.name == "08_build_technology_financial_features_batched.py"
    assert step.args == [
        "--model-family",
        model_family,
        "--batch-size",
        "7",
        "--batch-timeout-sec",
        "900.0",
    ]


def test_historical_refresh_rejects_latest_only_governance_steps() -> None:
    class TestStep:
        def __init__(self, step_id: str) -> None:
            self.step_id = step_id

    assert asof_governance_conflict(
        "2024-01-02",
        [TestStep("10b_publish_dashboard")],
        publisher_script="publisher.py",
    ) == ""
    message = asof_governance_conflict(
        "2024-01-02",
        [TestStep("16_publish_governance")],
        publisher_script="publisher.py",
    )
    assert "cannot be combined" in message
    assert "publisher.py" in message

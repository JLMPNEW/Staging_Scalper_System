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

from market_positioning.api_collectors import (
    bounded_ibkr_end_date,
    prune_backdated_ibkr_shortable_rows,
)
from technology.core import https as technology_https
from technology.core.db import init_db
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
from technology.core.short_interest_float import (
    enrich_short_interest_float,
    load_issuer_flags,
    validate_float_enrichment,
)
from technology.core.universe_loader import prune_removed_current_universe_rows


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_verified_https_context_uses_explicit_ca_bundle(monkeypatch) -> None:
    sentinel = object()
    captured: list[str] = []

    def fake_create_default_context(*, cafile: str):
        captured.append(cafile)
        return sentinel

    monkeypatch.setenv("SSL_CERT_FILE", "C:/trusted/technology-ca.pem")
    monkeypatch.setattr(technology_https.ssl, "create_default_context", fake_create_default_context)

    assert technology_https.verified_https_context() is sentinel
    assert captured == ["C:/trusted/technology-ca.pem"]


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


def test_current_universe_prune_retires_company_unless_another_family_owns_it() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    now = "2026-07-20T00:00:00+00:00"
    conn.executemany(
        """
        INSERT INTO dim_company(
            ticker, company_name, universe_status, is_active,
            data_quality_status, first_seen_at, updated_at
        )
        VALUES (?, ?, 'keep', 1, 'complete', ?, ?)
        """,
        [
            ("RETIRED", "Retired Hardware", now, now),
            ("SHARED", "Shared Company", now, now),
        ],
    )
    company_ids = {
        str(row["ticker"]): int(row["company_id"])
        for row in conn.execute("SELECT company_id, ticker FROM dim_company")
    }
    conn.executemany(
        """
        INSERT INTO dim_universe_membership(
            company_id, ticker, model_family, membership_basis, start_date,
            membership_status, is_current_member, point_in_time_flag,
            created_at, updated_at
        )
        VALUES (?, ?, ?, 'current_source_of_truth', '2010-01-01',
                'active', 1, 0, ?, ?)
        """,
        [
            (company_ids["RETIRED"], "RETIRED", "technology_hardware", now, now),
            (company_ids["SHARED"], "SHARED", "technology_hardware", now, now),
            (company_ids["SHARED"], "SHARED", "software_infrastructure", now, now),
        ],
    )

    prune_removed_current_universe_rows(
        conn,
        model_family="technology_hardware",
        keep_tickers={"KEPT"},
        cohort_source_id="technology_hardware_cohort_policy",
    )

    retired = conn.execute(
        "SELECT is_active, universe_status, data_quality_status FROM dim_company WHERE ticker = 'RETIRED'"
    ).fetchone()
    shared = conn.execute(
        "SELECT is_active, universe_status FROM dim_company WHERE ticker = 'SHARED'"
    ).fetchone()
    assert retired is not None
    assert tuple(retired) == (0, "historical", "retired_from_current_universe")
    assert shared is not None
    assert tuple(shared) == (1, "keep")
    assert conn.execute(
        """
        SELECT COUNT(*)
        FROM dim_universe_membership
        WHERE ticker = 'RETIRED' AND is_current_member = 1
        """
    ).fetchone()[0] == 0
    assert conn.execute(
        """
        SELECT COUNT(*)
        FROM dim_universe_membership
        WHERE ticker = 'SHARED'
          AND model_family = 'software_infrastructure'
          AND is_current_member = 1
        """
    ).fetchone()[0] == 1


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


def test_short_interest_float_uses_filing_availability_and_split_adjustment() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    now = "2024-04-15T00:00:00+00:00"
    conn.executemany(
        """
        INSERT INTO source_registry(
            source_id, stage, source_name, source_type, base_url,
            status, created_at, updated_at
        )
        VALUES (?, 'test', ?, 'test', 'https://example.test', 'active', ?, ?)
        """,
        [
            (source_id, source_id, now, now)
            for source_id in (
                "sec_submissions",
                "sec_companyfacts",
                "yahoo_finance_adjusted",
                "market_positioning_upstream",
            )
        ],
    )
    conn.execute(
        """
        INSERT INTO dim_company(
            ticker, cik, company_name, universe_status, is_active,
            data_quality_status, first_seen_at, updated_at
        )
        VALUES ('TEST', '0000000001', 'Test Company', 'keep', 1, 'complete', ?, ?)
        """,
        (now, now),
    )
    conn.execute(
        """
        INSERT INTO fact_sec_filing(
            ticker, cik, accession_number, source_id, form_type, filing_date,
            created_at, updated_at
        )
        VALUES ('TEST', '0000000001', 'A1', 'sec_submissions', '10-K',
                '2024-02-20', ?, ?)
        """,
        (now, now),
    )
    conn.execute(
        """
        INSERT INTO fact_sec_xbrl_fact_raw(
            fact_key, ticker, cik, source_id, taxonomy, concept, unit, value,
            end_date, accession_number, created_at, updated_at
        )
        VALUES ('F1', 'TEST', '0000000001', 'sec_companyfacts', 'dei',
                'EntityPublicFloat', 'USD', 1000000000.0, '2024-01-15',
                'A1', ?, ?)
        """,
        (now, now),
    )
    conn.execute(
        """
        INSERT INTO fact_price_ohlcv(
            ticker, bar_date, source_id, close, adj_close, is_adjusted,
            created_at, updated_at
        )
        VALUES ('TEST', '2024-01-15', 'yahoo_finance_adjusted',
                10.0, 10.0, 1, ?, ?)
        """,
        (now, now),
    )
    conn.execute(
        """
        INSERT INTO fact_corporate_action(
            ticker, action_date, source_id, action_type, split_factor,
            created_at, updated_at
        )
        VALUES ('TEST', '2024-03-01', 'yahoo_finance_adjusted',
                'split', 2.0, ?, ?)
        """,
        (now, now),
    )
    conn.executemany(
        """
        INSERT INTO fact_short_interest(
            ticker, settlement_date, source_id, asof_date, publication_date,
            short_interest_shares, days_to_cover, created_at, updated_at
        )
        VALUES ('TEST', ?, 'market_positioning_upstream', ?, ?, 10000000.0, 2.0, ?, ?)
        """,
        [
            ("2024-01-31", "2024-02-14", "2024-02-14", now, now),
            ("2024-03-31", "2024-04-14", "2024-04-14", now, now),
        ],
    )

    stats = enrich_short_interest_float(
        conn,
        ["TEST"],
        source_id="market_positioning_upstream",
    )

    assert stats.rows_examined == 2
    assert stats.rows_enriched == 1
    before = conn.execute(
        "SELECT * FROM fact_short_interest WHERE settlement_date = '2024-01-31'"
    ).fetchone()
    after = conn.execute(
        "SELECT * FROM fact_short_interest WHERE settlement_date = '2024-03-31'"
    ).fetchone()
    assert before["short_interest_pct_float"] is None
    assert before["float_selection_reason"] == "no_pit_float_candidate"
    assert after["source_id"] == "market_positioning_upstream"
    assert after["float_source"] == "sec_entity_public_float_price_proxy"
    assert after["float_source_asof_date"] == "2024-02-20"
    assert after["float_measurement_date"] == "2024-01-15"
    assert after["float_split_adjustment_factor"] == pytest.approx(2.0)
    assert after["float_shares"] == pytest.approx(200_000_000.0)
    assert after["short_interest_pct_float"] == pytest.approx(0.05)
    assert validate_float_enrichment(
        conn,
        ["TEST"],
        source_id="market_positioning_upstream",
    ) == []


def test_ibkr_current_availability_cannot_be_backdated_or_future_dated() -> None:
    assert bounded_ibkr_end_date(
        date(2026, 7, 30),
        capture_date=date(2026, 7, 25),
    ) == date(2026, 7, 25)
    assert bounded_ibkr_end_date(
        date(2026, 7, 20),
        capture_date=date(2026, 7, 25),
    ) == date(2026, 7, 20)

    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE ibkr_shortable_shares_snapshots(
            ticker TEXT,
            asof_date TEXT,
            asof_datetime TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO ibkr_shortable_shares_snapshots VALUES (?, ?, ?)",
        [
            ("KEEP", "2026-07-24", "2026-07-25T02:00:00+00:00"),
            ("DROP", "2026-07-20", "2026-07-23T02:00:00+00:00"),
        ],
    )
    assert prune_backdated_ibkr_shortable_rows(conn) == 1
    assert conn.execute(
        "SELECT GROUP_CONCAT(ticker) FROM ibkr_shortable_shares_snapshots"
    ).fetchone()[0] == "KEEP"


def test_ticker_change_is_not_misclassified_as_multiple_share_classes() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    now = "2026-07-25T00:00:00+00:00"
    conn.executemany(
        """
        INSERT INTO dim_company(
            ticker, cik, company_name, universe_status, is_active,
            data_quality_status, first_seen_at, updated_at
        )
        VALUES (?, '0000004321', ?, ?, ?, 'complete', ?, ?)
        """,
        [
            ("OLD", "Old Name", "historical", 0, now, now),
            ("NEW", "New Name", "keep", 1, now, now),
        ],
    )

    flags = load_issuer_flags(conn, ["OLD", "NEW"])

    assert flags["OLD"] == (False, False)
    assert flags["NEW"] == (False, False)


def test_positioning_stale_fallback_only_accepts_availability_errors() -> None:
    importer = load_script(
        "technology/scripts/09_import_technology_positioning.py",
        "technology_positioning_import_error_classification",
    )
    upstream = load_script(
        "technology/scripts/13_sync_technology_positioning_upstream.py",
        "technology_positioning_upstream_error_classification",
    )

    assert importer.is_upstream_availability_error(
        sqlite3.OperationalError("database is locked")
    )
    assert importer.is_upstream_availability_error(OSError("share unavailable"))
    assert not importer.is_upstream_availability_error(
        sqlite3.OperationalError("no such table: short_interest_snapshots")
    )
    assert upstream.is_shared_db_availability_error(
        sqlite3.OperationalError("unable to open database file")
    )
    assert not upstream.is_shared_db_availability_error(
        sqlite3.OperationalError("no such column: settlement_date")
    )


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
        "--current-members-only",
        "--model-family",
        model_family,
        "--batch-size",
        "7",
        "--batch-timeout-sec",
        "900.0",
    ]
    positioning = next(item for item in steps if item.step_id == "13_sync_positioning_upstream")
    assert "--allow-stale-ibkr-borrow-on-error" not in positioning.args

    kwargs["allow_stale_ibkr_borrow_on_error"] = True
    fallback_steps = module.build_steps(**kwargs)
    fallback_positioning = next(
        item for item in fallback_steps if item.step_id == "13_sync_positioning_upstream"
    )
    assert "--allow-stale-ibkr-borrow-on-error" in fallback_positioning.args


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

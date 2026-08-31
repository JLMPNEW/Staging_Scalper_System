from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
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
from technology.core.positioning_window import (
    resolve_positioning_window,
    resolve_positioning_windows,
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
from technology.core.universe_loader import (
    apply_lifecycle_overrides,
    prune_removed_current_universe_rows,
)
from technology.core.universe_validator import expected_current_tickers


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
        str(row["ticker"]): int(row["company_id"]) for row in conn.execute("SELECT company_id, ticker FROM dim_company")
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
    shared = conn.execute("SELECT is_active, universe_status FROM dim_company WHERE ticker = 'SHARED'").fetchone()
    assert retired is not None
    assert tuple(retired) == (0, "historical", "retired_from_current_universe")
    assert shared is not None
    assert tuple(shared) == (1, "keep")
    assert (
        conn.execute(
            """
        SELECT COUNT(*)
        FROM dim_universe_membership
        WHERE ticker = 'RETIRED' AND is_current_member = 1
        """
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            """
        SELECT COUNT(*)
        FROM dim_universe_membership
        WHERE ticker = 'SHARED'
          AND model_family = 'software_infrastructure'
          AND is_current_member = 1
        """
        ).fetchone()[0]
        == 1
    )


def test_market_validator_expected_count_uses_active_contract(tmp_path: Path) -> None:
    module = load_script(
        "technology/scripts/06_validate_technology_market_stage.py",
        "technology_market_validator_active_count_test",
    )
    seed = tmp_path / "universe.csv"
    seed.write_text(
        "ticker,listing_status,security_type\n"
        "ACTIVE,active,common stock\n"
        "INACTIVE,inactive,common stock\n"
        "RETIRED,active,common stock\n",
        encoding="utf-8",
    )
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        json.dumps(
            {
                "non_investable_listing_statuses": ["inactive"],
                "investable_security_types": ["common stock"],
                "lifecycle_overrides": [{"ticker": "RETIRED", "last_tradable_date": "2026-07-30"}],
            }
        ),
        encoding="utf-8",
    )
    config = {"technology_universe": {"seed_csv": str(seed)}}

    assert (
        module.expected_current_ticker_count(
            config,
            base_dir=tmp_path,
            policy_path=policy,
            universe_csv_path=seed,
            effective_date=date(2026, 8, 17),
        )
        == 1
    )


def test_market_validator_expected_count_uses_explicit_family_seed(
    tmp_path: Path,
) -> None:
    module = load_script(
        "technology/scripts/06_validate_technology_market_stage.py",
        "technology_market_validator_family_seed_test",
    )
    default_seed = tmp_path / "default.csv"
    default_seed.write_text(
        "ticker,listing_status,security_type\nSEMI,active,common stock\n",
        encoding="utf-8",
    )
    family_seed = tmp_path / "family.csv"
    family_seed.write_text(
        "ticker,listing_status,security_type\nSOFT1,active,common stock\nSOFT2,active,common stock\n",
        encoding="utf-8",
    )
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        json.dumps(
            {
                "non_investable_listing_statuses": ["inactive"],
                "investable_security_types": ["common stock"],
            }
        ),
        encoding="utf-8",
    )
    config = {"technology_universe": {"seed_csv": str(default_seed)}}

    assert (
        module.expected_current_ticker_count(
            config,
            base_dir=tmp_path,
            policy_path=policy,
            universe_csv_path=family_seed,
            effective_date=date(2026, 8, 17),
        )
        == 2
    )


def test_expected_current_tickers_honors_status_type_and_lifecycle() -> None:
    rows = [
        {"ticker": "ACTIVE", "listing_status": "active", "security_type": "common stock"},
        {"ticker": "INACTIVE", "listing_status": "inactive", "security_type": "common stock"},
        {"ticker": "FUND", "listing_status": "active", "security_type": "fund"},
        {"ticker": "RETIRED", "listing_status": "active", "security_type": "common stock"},
    ]
    policy = {
        "non_investable_listing_statuses": ["inactive"],
        "investable_security_types": ["common stock"],
        "lifecycle_overrides": [{"ticker": "RETIRED", "last_tradable_date": "2026-07-30"}],
    }

    assert expected_current_tickers(
        rows,
        policy=policy,
        effective_date=date(2026, 8, 19),
    ) == {"ACTIVE"}


def test_lifecycle_override_retires_ticker_and_prunes_post_end_market_rows() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    now = "2026-08-19T00:00:00+00:00"
    conn.executemany(
        """
        INSERT INTO source_registry(
            source_id, stage, source_name, source_type, base_url,
            status, created_at, updated_at
        )
        VALUES (?, 'test', ?, 'test', 'https://example.test', 'active', ?, ?)
        """,
        [
            ("semiconductor_current_universe_csv", "universe", now, now),
            ("yahoo_finance_adjusted", "prices", now, now),
        ],
    )
    conn.execute(
        """
        INSERT INTO dim_company(
            ticker, company_name, universe_status, is_active,
            data_quality_status, first_seen_at, updated_at
        )
        VALUES ('SKYT', 'SkyWater Technology', 'keep', 1, 'complete', ?, ?)
        """,
        (now, now),
    )
    company_id = int(conn.execute("SELECT company_id FROM dim_company WHERE ticker = 'SKYT'").fetchone()[0])
    conn.execute(
        """
        INSERT INTO dim_security(
            company_id, ticker, exchange, security_type, listing_status,
            is_primary_listing, currency, created_at, updated_at
        )
        VALUES (?, 'SKYT', 'NASDAQ', 'common stock', 'active', 1, 'USD', ?, ?)
        """,
        (company_id, now, now),
    )
    conn.execute(
        """
        INSERT INTO dim_universe_membership(
            company_id, ticker, model_family, membership_source_id,
            membership_basis, start_date, membership_status,
            is_current_member, point_in_time_flag, created_at, updated_at
        )
        VALUES (?, 'SKYT', 'semiconductors', 'semiconductor_current_universe_csv',
                'current_source_of_truth', '2021-04-21', 'active', 1, 0, ?, ?)
        """,
        (company_id, now, now),
    )
    conn.executemany(
        """
        INSERT INTO fact_price_ohlcv(
            ticker, bar_date, source_id, close, adj_close, volume,
            is_adjusted, created_at, updated_at
        )
        VALUES ('SKYT', ?, 'yahoo_finance_adjusted', 32.46, 32.46, ?, 1, ?, ?)
        """,
        [
            ("2026-07-30", 2_241_100.0, now, now),
            ("2026-08-07", 0.0, now, now),
        ],
    )
    conn.executemany(
        """
        INSERT INTO fact_market_snapshot(
            ticker, asof_date, source_id, regular_market_price,
            source_timestamp, created_at, updated_at
        )
        VALUES ('SKYT', ?, 'yahoo_finance_adjusted', 32.46, ?, ?, ?)
        """,
        [
            ("2026-07-30", "2026-07-30T20:00:00Z", now, now),
            ("2026-08-07", "2026-07-30T20:00:00Z", now, now),
        ],
    )
    policy = {
        "lifecycle_overrides": [
            {
                "ticker": "SKYT",
                "start_date": "2021-04-21",
                "last_tradable_date": "2026-07-30",
                "event_type": "acquired_and_delisted",
                "successor_ticker": "IONQ",
                "confidence": 1.0,
                "source_url": "https://www.sec.gov/example",
                "reason": "Acquisition completed before the 2026-07-31 market open.",
            }
        ]
    }

    counts = apply_lifecycle_overrides(
        conn,
        policy=policy,
        model_family="semiconductors",
        membership_source_id="semiconductor_current_universe_csv",
        price_source_id="yahoo_finance_adjusted",
        effective_date=date(2026, 8, 19),
    )

    assert counts == {"applied": 1, "prices_deleted": 1, "snapshots_deleted": 1}
    assert tuple(
        conn.execute(
            "SELECT universe_status, is_active, data_quality_status FROM dim_company WHERE ticker = 'SKYT'"
        ).fetchone()
    ) == ("historical", 0, "retired_by_governed_lifecycle")
    assert (
        conn.execute("SELECT listing_status FROM dim_security WHERE ticker = 'SKYT'").fetchone()[0]
        == "historical_delisted"
    )
    membership = conn.execute(
        """
        SELECT end_date, membership_status, is_current_member, point_in_time_flag
        FROM dim_universe_membership
        WHERE ticker = 'SKYT' AND membership_basis = 'governed_lifecycle_override'
        """
    ).fetchone()
    assert tuple(membership) == ("2026-07-30", "historical", 0, 1)
    assert (
        conn.execute(
            """
        SELECT COUNT(*) FROM dim_universe_membership
        WHERE ticker = 'SKYT' AND end_date IS NULL
        """
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute("SELECT MAX(bar_date) FROM fact_price_ohlcv WHERE ticker = 'SKYT'").fetchone()[0] == "2026-07-30"
    )
    assert (
        conn.execute("SELECT MAX(asof_date) FROM fact_market_snapshot WHERE ticker = 'SKYT'").fetchone()[0]
        == "2026-07-30"
    )


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
        shared_financial_subfeatures if implementation == "shared" else semiconductor_diagnostics.financial_subfeatures
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
    before = conn.execute("SELECT * FROM fact_short_interest WHERE settlement_date = '2024-01-31'").fetchone()
    after = conn.execute("SELECT * FROM fact_short_interest WHERE settlement_date = '2024-03-31'").fetchone()
    assert before["short_interest_pct_float"] is None
    assert before["float_selection_reason"] == "no_pit_float_candidate"
    assert after["source_id"] == "market_positioning_upstream"
    assert after["float_source"] == "sec_entity_public_float_price_proxy"
    assert after["float_source_asof_date"] == "2024-02-20"
    assert after["float_measurement_date"] == "2024-01-15"
    assert after["float_split_adjustment_factor"] == pytest.approx(2.0)
    assert after["float_shares"] == pytest.approx(200_000_000.0)
    assert after["short_interest_pct_float"] == pytest.approx(0.05)
    assert (
        validate_float_enrichment(
            conn,
            ["TEST"],
            source_id="market_positioning_upstream",
        )
        == []
    )


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
    assert conn.execute("SELECT GROUP_CONCAT(ticker) FROM ibkr_shortable_shares_snapshots").fetchone()[0] == "KEEP"


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

    assert importer.is_upstream_availability_error(sqlite3.OperationalError("database is locked"))
    assert importer.is_upstream_availability_error(OSError("share unavailable"))
    assert not importer.is_upstream_availability_error(
        sqlite3.OperationalError("no such table: short_interest_snapshots")
    )
    assert upstream.is_shared_db_availability_error(sqlite3.OperationalError("unable to open database file"))
    assert not upstream.is_shared_db_availability_error(sqlite3.OperationalError("no such column: settlement_date"))


def test_positioning_incremental_window_is_bounded_and_respects_floor() -> None:
    start, end = resolve_positioning_window(
        asof="2026-08-14",
        configured_start="2013-01-01",
        lookback_days=550,
    )
    assert end == date(2026, 8, 14)
    assert (end - start).days == 550

    floor_start, _ = resolve_positioning_window(
        asof="2013-03-01",
        configured_start="2013-01-01",
        lookback_days=550,
    )
    assert floor_start == date(2013, 1, 1)


def test_positioning_source_windows_use_approved_horizons() -> None:
    windows = resolve_positioning_windows(
        asof="2026-08-14",
        configured_start="2013-01-01",
    )
    assert (windows.end - windows.form4_start).days == 120
    assert (windows.end - windows.short_interest_start).days == 120
    assert (windows.end - windows.institutional_13f_start).days == 550
    assert (windows.end - windows.borrow_start).days == 45
    assert (windows.end - windows.float_denominator_start).days == 550

    overridden = resolve_positioning_windows(
        asof="2026-08-14",
        configured_start="2013-01-01",
        requested_start="2025-01-01",
        borrow_requested_start="2026-08-01",
    )
    assert overridden.form4_start == date(2025, 1, 1)
    assert overridden.institutional_13f_start == date(2025, 1, 1)
    assert overridden.borrow_start == date(2026, 8, 1)

    full = resolve_positioning_windows(
        asof="2026-08-14",
        configured_start="2013-01-01",
        full_history=True,
    )
    assert {
        full.form4_start,
        full.short_interest_start,
        full.institutional_13f_start,
        full.borrow_start,
        full.float_denominator_start,
    } == {date(2013, 1, 1)}


def test_upstream_sync_propagates_source_windows_to_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream = load_script(
        "technology/scripts/13_sync_technology_positioning_upstream.py",
        "technology_positioning_window_propagation",
    )
    windows = resolve_positioning_windows(
        asof="2026-08-14",
        configured_start="2013-01-01",
    )
    captured: dict[str, list[str]] = {}

    def fake_run(command: list[str], **_kwargs: Any) -> None:
        captured["command"] = command

    monkeypatch.setattr(upstream.subprocess, "run", fake_run)
    upstream.run_technology_import(Path("technology/config.yaml"), windows=windows)
    command = captured["command"]

    expected = {
        "--form4-history-start": windows.form4_start,
        "--short-interest-history-start": windows.short_interest_start,
        "--sec-13f-history-start": windows.institutional_13f_start,
        "--ibkr-history-start": windows.borrow_start,
        "--float-denominator-history-start": windows.float_denominator_start,
        "--asof": windows.end,
    }
    for flag, expected_date in expected.items():
        assert command[command.index(flag) + 1] == expected_date.isoformat()


def test_historical_positioning_universe_is_scoped_to_asof_membership() -> None:
    importer = load_script(
        "technology/scripts/09_import_technology_positioning.py",
        "technology_positioning_pit_universe",
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.executemany(
        """
        INSERT INTO dim_universe_membership(
            ticker, model_family, membership_basis, start_date, end_date,
            membership_status, is_current_member, point_in_time_flag,
            confidence, created_at, updated_at
        )
        VALUES (?, 'semiconductors', 'historical_point_in_time',
                ?, ?, 'historical', 0, 1, 1.0, '', '')
        """,
        [
            ("OLD", "2019-01-01", "2020-12-31"),
            ("ACTIVE", "2020-01-01", "2025-12-31"),
            ("FUTURE", "2026-01-01", None),
        ],
    )

    tickers = importer.load_universe(
        conn,
        set(),
        model_family="semiconductors",
        include_historical=True,
        asof=date(2024, 6, 30),
    )

    assert tickers == ["ACTIVE"]


def test_incremental_13f_import_preserves_rows_outside_window() -> None:
    importer = load_script(
        "technology/scripts/09_import_technology_positioning.py",
        "technology_positioning_incremental_13f",
    )
    dest = sqlite3.connect(":memory:")
    dest.row_factory = sqlite3.Row
    dest.execute(
        """
        CREATE TABLE fact_13f_positioning(
            ticker TEXT NOT NULL,
            asof_date TEXT NOT NULL,
            period_of_report TEXT,
            source_id TEXT NOT NULL,
            institutional_shares REAL,
            institutional_value REAL,
            manager_count INTEGER,
            institutional_ownership_delta_pct REAL,
            new_buyer_count INTEGER,
            exiting_holder_count INTEGER,
            net_buyer_count INTEGER,
            created_at TEXT,
            updated_at TEXT,
            UNIQUE(ticker, asof_date, source_id)
        )
        """
    )
    dest.executemany(
        """
        INSERT INTO fact_13f_positioning(
            ticker, asof_date, source_id, institutional_shares
        ) VALUES ('TEST', ?, 'market_positioning_upstream', ?)
        """,
        [("2023-12-31", 10.0), ("2024-06-30", 20.0), ("2026-01-31", 30.0)],
    )
    source = sqlite3.connect(":memory:")
    source.row_factory = sqlite3.Row
    source.execute(
        """
        CREATE TABLE institutional_13f_ownership_snapshots(
            ticker TEXT, asof_date TEXT, period_of_report TEXT,
            institutional_shares REAL, institutional_value REAL,
            manager_count INTEGER, institutional_ownership_delta_pct REAL,
            new_buyer_count INTEGER, exiting_holder_count INTEGER,
            net_buyer_count INTEGER
        )
        """
    )
    source.executemany(
        """
        INSERT INTO institutional_13f_ownership_snapshots
        VALUES ('TEST', ?, ?, ?, 100.0, 2, 0.1, 1, 0, 1)
        """,
        [
            ("2023-12-31", "2023-09-30", 11.0),
            ("2024-06-30", "2024-03-31", 25.0),
            ("2026-01-31", "2025-12-31", 35.0),
        ],
    )

    stats = importer.import_13f(
        dest,
        source,
        ["TEST"],
        query_tickers=["TEST"],
        source_to_internal={"TEST": "TEST"},
        source_id="market_positioning_upstream",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
    )

    rows = dest.execute(
        "SELECT asof_date, institutional_shares FROM fact_13f_positioning ORDER BY asof_date"
    ).fetchall()
    assert stats == {"TEST": 1}
    assert [(row[0], row[1]) for row in rows] == [
        ("2023-12-31", 10.0),
        ("2024-06-30", 25.0),
        ("2026-01-31", 30.0),
    ]


def test_incremental_short_interest_excludes_unpublished_observations() -> None:
    importer = load_script(
        "technology/scripts/09_import_technology_positioning.py",
        "technology_positioning_incremental_short_interest",
    )
    dest = sqlite3.connect(":memory:")
    dest.row_factory = sqlite3.Row
    dest.execute(
        """
        CREATE TABLE fact_short_interest(
            ticker TEXT NOT NULL, settlement_date TEXT NOT NULL,
            source_id TEXT NOT NULL, asof_date TEXT, publication_date TEXT,
            short_interest_shares REAL, float_shares REAL,
            short_interest_pct_float REAL, days_to_cover REAL,
            created_at TEXT, updated_at TEXT,
            UNIQUE(ticker, settlement_date, source_id)
        )
        """
    )
    source = sqlite3.connect(":memory:")
    source.row_factory = sqlite3.Row
    source.execute(
        """
        CREATE TABLE short_interest_snapshots(
            ticker TEXT, settlement_date TEXT, asof_date TEXT,
            publication_date TEXT, short_interest_shares REAL,
            float_shares REAL, short_interest_pct_float REAL,
            days_to_cover REAL
        )
        """
    )
    source.executemany(
        """
        INSERT INTO short_interest_snapshots
        VALUES ('TEST', ?, ?, ?, 100.0, 1000.0, 0.1, 2.0)
        """,
        [
            ("2024-11-30", "2024-12-10", "2024-12-10"),
            ("2024-12-31", "2025-01-12", "2025-01-12"),
        ],
    )

    stats = importer.import_short_interest(
        dest,
        source,
        ["TEST"],
        query_tickers=["TEST"],
        source_to_internal={"TEST": "TEST"},
        source_id="market_positioning_upstream",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
    )

    assert stats == {"TEST": 1}
    assert dest.execute("SELECT settlement_date FROM fact_short_interest").fetchone()[0] == "2024-11-30"


def test_incremental_13f_aggregation_preserves_outer_history_and_prior_delta() -> None:
    upstream = load_script(
        "technology/scripts/13_sync_technology_positioning_upstream.py",
        "technology_positioning_incremental_13f_aggregation",
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    upstream.init_market_positioning_db(conn)
    conn.executemany(
        """
        INSERT INTO institutional_13f_holdings(
            filing_key, manager_cik, manager_name, ticker, period_of_report,
            filing_date, shares, market_value, share_type, put_call, source,
            created_at, updated_at
        )
        VALUES (
            ?, 'M1', 'Manager', 'TEST', ?, ?, ?, 1000.0, 'SH', '',
            'sec_13f_data_sets', '', ''
        )
        """,
        [
            ("A1", "2023-09-30", "2023-11-14", 100.0),
            ("A2", "2023-12-31", "2024-02-14", 110.0),
            ("A3", "2024-03-31", "2024-05-14", 121.0),
        ],
    )
    conn.executemany(
        """
        INSERT INTO institutional_13f_ownership_snapshots(
            ticker, asof_date, period_of_report, institutional_shares,
            source, created_at, updated_at
        )
        VALUES ('TEST', ?, ?, ?, 'sec_13f_data_sets', '', '')
        """,
        [
            ("2023-11-14", "2023-09-30", 100.0),
            ("2026-02-14", "2025-12-31", 999.0),
        ],
    )

    count = upstream.aggregate_13f_ownership_for_tickers(
        conn,
        ["TEST"],
        history_start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
    )

    rows = conn.execute(
        """
        SELECT asof_date, institutional_shares, institutional_ownership_delta_pct
        FROM institutional_13f_ownership_snapshots
        WHERE ticker = 'TEST'
        ORDER BY asof_date
        """
    ).fetchall()
    assert count == 2
    assert [row["asof_date"] for row in rows] == [
        "2023-11-14",
        "2024-02-14",
        "2024-05-14",
        "2026-02-14",
    ]
    assert rows[1]["institutional_ownership_delta_pct"] == pytest.approx(0.1)
    assert rows[2]["institutional_ownership_delta_pct"] == pytest.approx(0.1)


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


def test_yahoo_parser_drops_zero_volume_carry_forward_after_authoritative_market_time(
    yahoo_prices: ModuleType,
) -> None:
    market_time = int(datetime(2026, 7, 30, 20, tzinfo=timezone.utc).timestamp())
    timestamps = [
        market_time,
        int(datetime(2026, 7, 31, 20, tzinfo=timezone.utc).timestamp()),
        int(datetime(2026, 8, 3, 20, tzinfo=timezone.utc).timestamp()),
    ]
    payload = {
        "chart": {
            "error": None,
            "result": [
                {
                    "meta": {"regularMarketTime": market_time},
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [
                            {
                                "close": [32.46, 32.46, 32.46],
                                "open": [31.8, 32.46, 32.46],
                                "high": [33.0, 32.46, 32.46],
                                "low": [31.5, 32.46, 32.46],
                                "volume": [2_241_100, 0, 0],
                            }
                        ],
                        "adjclose": [{"adjclose": [32.46, 32.46, 32.46]}],
                    },
                }
            ],
        }
    }

    bars, _actions, meta, error = yahoo_prices.parse_chart_result(
        yahoo_prices.PriceJob("SKYT", "SkyWater Technology"),
        json.dumps(payload),
        "yahoo_finance_adjusted",
    )

    assert error == ""
    assert [bar.bar_date for bar in bars] == ["2026-07-30"]
    assert meta["droppedSyntheticPostMarketBarDates"] == [
        "2026-07-31",
        "2026-08-03",
    ]


def test_semiconductor_membership_cleanup_removes_post_end_market_rows() -> None:
    membership = load_script(
        "technology/semiconductors/scripts/01b_load_semiconductor_historical_membership.py",
        "technology_semiconductor_membership_cleanup_regression",
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    now = "2026-08-19T00:00:00+00:00"
    conn.execute(
        """
        INSERT INTO source_registry(
            source_id, stage, source_name, source_type, base_url,
            subsector_scope, created_at, updated_at
        )
        VALUES ('yahoo_finance_adjusted', 'stage_3', 'Yahoo', 'api',
                'https://finance.yahoo.com', 'technology', ?, ?)
        """,
        (now, now),
    )
    conn.executemany(
        """
        INSERT INTO fact_price_ohlcv(
            ticker, bar_date, source_id, close, adj_close,
            price_adjustment, is_adjusted, created_at, updated_at
        )
        VALUES ('SKYT', ?, 'yahoo_finance_adjusted', 32.46, 32.46,
                'adjusted_close', 1, ?, ?)
        """,
        [
            ("2026-07-30", now, now),
            ("2026-07-31", now, now),
            ("2026-08-03", now, now),
        ],
    )
    conn.executemany(
        """
        INSERT INTO fact_market_snapshot(
            ticker, asof_date, source_id, regular_market_price,
            created_at, updated_at
        )
        VALUES ('SKYT', ?, 'yahoo_finance_adjusted', 32.46, ?, ?)
        """,
        [
            ("2026-07-30", now, now),
            ("2026-08-03", now, now),
        ],
    )

    deleted_prices, deleted_snapshots = membership.truncate_prices_after_membership_end(
        conn,
        [{"ticker": "SKYT", "end_date": "2026-07-30"}],
        price_source="yahoo_finance_adjusted",
    )

    assert deleted_prices == 2
    assert deleted_snapshots == 1
    assert (
        conn.execute("SELECT MAX(bar_date) FROM fact_price_ohlcv WHERE ticker = 'SKYT'").fetchone()[0] == "2026-07-30"
    )
    assert (
        conn.execute("SELECT MAX(asof_date) FROM fact_market_snapshot WHERE ticker = 'SKYT'").fetchone()[0]
        == "2026-07-30"
    )


def test_skyt_is_governed_as_historical_after_ionq_acquisition() -> None:
    seed_path = PROJECT_ROOT / "ticker_mapping" / "semiconductor_tickers.csv"
    history_path = PROJECT_ROOT / "technology" / "semiconductors" / "data" / "semiconductor_historical_membership.csv"
    seed_rows = {row["ticker"]: row for row in __import__("csv").DictReader(seed_path.open(encoding="utf-8-sig"))}
    history_rows = {
        row["internal_ticker"]: row for row in __import__("csv").DictReader(history_path.open(encoding="utf-8-sig"))
    }

    assert seed_rows["SKYT"]["listing_status"] == "inactive_or_not_investable"
    assert history_rows["SKYT"]["end_date"] == "2026-07-30"
    assert history_rows["SKYT"]["successor_ticker"] == "IONQ"


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

    def start_for(args: list[str], flag: str) -> date:
        return date.fromisoformat(args[args.index(flag) + 1])

    asof_date = date(2026, 7, 8)
    assert (asof_date - start_for(positioning.args, "--finra-history-start")).days == 120
    assert (asof_date - start_for(positioning.args, "--sec-13f-history-start")).days == 550
    assert (asof_date - start_for(positioning.args, "--ibkr-history-start")).days == 45

    positioning_import = next(item for item in steps if item.step_id == "09_import_positioning")
    expected_import_windows = {
        "--form4-history-start": 120,
        "--short-interest-history-start": 120,
        "--sec-13f-history-start": 550,
        "--ibkr-history-start": 45,
        "--float-denominator-history-start": 550,
    }
    for flag, expected_days in expected_import_windows.items():
        assert (asof_date - start_for(positioning_import.args, flag)).days == expected_days

    kwargs["allow_stale_ibkr_borrow_on_error"] = True
    fallback_steps = module.build_steps(**kwargs)
    fallback_positioning = next(item for item in fallback_steps if item.step_id == "13_sync_positioning_upstream")
    assert "--allow-stale-ibkr-borrow-on-error" in fallback_positioning.args


def test_historical_positioning_rebuild_is_features_only() -> None:
    module = load_script(
        "technology/scripts/18_backfill_technology_historical_dashboard_reports.py",
        "technology_historical_positioning_features_only",
    )
    families = {
        module.FAMILIES["semiconductors"].family: module.FAMILIES["semiconductors"],
        module.FAMILIES["technology_hardware"].family: module.FAMILIES["technology_hardware"],
        module.FAMILIES["software_infrastructure"].family: module.FAMILIES["software_infrastructure"],
    }
    for spec in families.values():
        step = next(item for item in spec.steps if item.step_id == "09_import_positioning")
        assert "--features-only" in step.extra_args


def test_historical_dashboard_builds_financial_lineage_before_validation() -> None:
    module = load_script(
        "technology/scripts/18_backfill_technology_historical_dashboard_reports.py",
        "technology_historical_financial_lineage",
    )
    families = {
        module.FAMILIES["semiconductors"].family: module.FAMILIES["semiconductors"],
        module.FAMILIES["technology_hardware"].family: module.FAMILIES["technology_hardware"],
        module.FAMILIES["software_infrastructure"].family: module.FAMILIES["software_infrastructure"],
    }
    for family, spec in families.items():
        assert len(spec.pre_steps) == 1
        financial_rebuild = spec.pre_steps[0]
        assert financial_rebuild.step_id == "08_rebuild_financial_features"
        assert financial_rebuild.pass_asof is False
        assert financial_rebuild.extra_args[:2] == ("--model-family", family)
        step_ids = [item.step_id for item in spec.steps]
        lineage = next(
            item for item in spec.steps if item.step_id == "10c_financial_lineage_shadow"
        )
        assert step_ids.index("10b_publish_dashboard") < step_ids.index(
            "10c_financial_lineage_shadow"
        )
        assert step_ids.index("10c_financial_lineage_shadow") < step_ids.index(
            "10b_validate_dashboard_snapshot"
        )
        assert lineage.extra_args == (
            "--family",
            family,
            "--policy-context",
            "production",
            "--retrospective-source-discovery-max-days",
            "7",
        )


def test_historical_refresh_rejects_latest_only_governance_steps() -> None:
    class TestStep:
        def __init__(self, step_id: str) -> None:
            self.step_id = step_id

    assert (
        asof_governance_conflict(
            "2024-01-02",
            [TestStep("10b_publish_dashboard")],
            publisher_script="publisher.py",
        )
        == ""
    )
    message = asof_governance_conflict(
        "2024-01-02",
        [TestStep("16_publish_governance")],
        publisher_script="publisher.py",
    )
    assert "cannot be combined" in message
    assert "publisher.py" in message

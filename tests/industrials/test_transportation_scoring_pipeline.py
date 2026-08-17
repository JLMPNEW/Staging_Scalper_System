from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

from industrials.core.config import family_config, load_yaml
from industrials.core.db import connect, utc_now
from industrials.core.production_lock import ProductionLock
from industrials.transportation.contracts import COMPONENT_FIELDS, read_rows
from industrials.transportation.scoring import finalize_rank_rows
from industrials.transportation.scripts import _shared
from portfolio_layer.scores.adapters import run_adapter
from tests.industrials.test_transportation_market_foundation import load_scratch_foundation


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INDUSTRIALS_ROOT = PROJECT_ROOT / "industrials"
CONFIG_PATH = INDUSTRIALS_ROOT / "config.yaml"
ASOF = "2026-07-17"


def load_script(name: str):
    path = INDUSTRIALS_ROOT / "transportation" / "scripts" / name
    spec = importlib.util.spec_from_file_location(f"transportation_scoring_{name.replace('.', '_')}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def seed_complete_inputs(db_path: Path) -> int:
    load_scratch_foundation(db_path)
    now = utc_now()
    with connect(db_path) as conn:
        members = conn.execute(
            """
            SELECT t.ticker, t.calibration_cohort_id
            FROM dim_industrials_taxonomy AS t
            JOIN dim_universe_membership AS m
              ON m.ticker=t.ticker AND m.model_family=t.model_family
            WHERE t.model_family='transportation'
              AND m.membership_source_id='transportation_ticker_seed'
              AND m.membership_status='active'
            ORDER BY t.ticker
            """
        ).fetchall()
        assert members
        member_count = len(members)
        for index, member in enumerate(members, start=1):
            ticker = str(member["ticker"])
            cohort = str(member["calibration_cohort_id"])
            scale = index / 1000.0
            conn.execute(
                """
                INSERT INTO feature_market_technical(
                    ticker, asof_date, source_id, model_family, latest_close,
                    latest_adj_close, latest_volume, trading_days_available,
                    latest_bar_date, stale_days, stale_flag, low_history_flag,
                    low_liquidity_flag, ret_3m, ret_6m, ret_12m_ex_1m,
                    rel_strength_bench_3m, avg_dollar_volume_60d,
                    realized_vol_60d, max_drawdown_12m, market_data_quality,
                    created_at, updated_at
                ) VALUES (?, ?, 'yahoo_finance_adjusted', 'transportation', 25.0,
                          25.0, 1000000.0, 300, ?, 0, 0, 0, 0, ?, ?, ?, ?,
                          10000000.0, ?, ?, 'complete', ?, ?)
                """,
                (
                    ticker,
                    ASOF,
                    ASOF,
                    0.05 + scale,
                    0.10 + scale,
                    0.15 + scale,
                    0.02 + scale,
                    0.20 + scale,
                    -0.30 + scale,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO feature_financial_statement(
                    ticker, asof_date, source_id, model_family, accession_number,
                    form_type, fiscal_period_end, reporting_standard,
                    reporting_profile, financial_frequency, reported_currency,
                    fx_conversion_status, revenue_ttm_usd, capex_ttm_usd,
                    operating_margin, fcf_margin, asset_turnover,
                    revenue_yoy_growth, operating_income_yoy_growth, fcf_yield,
                    ev_operating_income, net_debt_to_ebitda, interest_coverage,
                    fcf_to_net_income, cash_runway_years, capital_raise_dependence,
                    diluted_shares_yoy_growth, sbc_pct_revenue,
                    development_stage, financial_confidence,
                    financial_fallback_status, canonical_quality,
                    data_quality_status, created_at, updated_at
                ) VALUES (?, ?, 'sec_companyfacts', 'transportation', ?, '10-K',
                          '2025-12-31', 'US_GAAP', 'SEC_XBRL_US_GAAP', 'TTM',
                          'USD', 'not_required_usd', 1000000000.0, -100000000.0,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.90,
                          'none', 'complete', 'complete', ?, ?)
                """,
                (
                    ticker,
                    ASOF,
                    f"synthetic-{ticker}",
                    0.10 + scale,
                    0.08 + scale,
                    0.50 + scale,
                    0.06 + scale,
                    0.07 + scale,
                    0.04 + scale,
                    10.0 - scale,
                    2.0 - scale,
                    8.0 + scale,
                    1.0 + scale,
                    3.0 + scale,
                    0.20 - scale,
                    0.03 - scale,
                    0.02 - scale,
                    "development_stage" if cohort == "development_stage_and_speculative_transport" else "operating",
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO dim_issuer_reporting_profile(
                    ticker, model_family, reporting_profile, reporting_standard,
                    primary_taxonomy, latest_filing_date, latest_form_type,
                    latest_accession_number, fallback_status,
                    financial_confidence, usable_xbrl_flag, source_id,
                    profile_asof_date, created_at, updated_at
                ) VALUES (?, 'transportation', 'SEC_XBRL_US_GAAP', 'US_GAAP',
                          'us-gaap', ?, '10-K', ?, 'none', 0.90, 1,
                          'sec_companyfacts', ?, ?, ?)
                """,
                (ticker, ASOF, f"synthetic-{ticker}", ASOF, now, now),
            )
            specialized_metric = {
                "surface_freight_and_logistics": "transport_volume_growth",
                "air_transport_and_aviation_services": "traffic_growth",
                "marine_shipping_and_maritime": "tce_or_day_rate",
                "development_stage_and_speculative_transport": "commercialization_progress",
            }[cohort]
            candidate_value = 10000.0 + index if specialized_metric == "tce_or_day_rate" else 0.05 + scale
            conn.execute(
                """
                INSERT INTO fact_sec_metric_disclosure_candidate(
                    candidate_key, ticker, source_id, model_family,
                    accession_number, form_type, filing_date, accepted_at,
                    document_name, metric_name, concept_name, candidate_value,
                    unit, period_end, scope, extraction_method, confidence,
                    candidate_status, status_reason, created_at, updated_at
                ) VALUES (?, ?, 'sec_companyfacts', 'transportation', ?, '10-K',
                          ?, ?, 'synthetic.htm', ?, ?, ?, 'ratio', ?,
                          'issuer', 'transportation_sec_filing_prose_v2', 0.90,
                          'ACCEPTED', 'reviewed_fixture', ?, ?)
                """,
                (
                    f"candidate-{ticker}-{specialized_metric}",
                    ticker,
                    f"synthetic-{ticker}",
                    ASOF,
                    f"{ASOF}T16:00:00Z",
                    specialized_metric,
                    specialized_metric,
                    candidate_value,
                    ASOF,
                    now,
                    now,
                ),
            )

    return member_count


def run_feature_pipeline(
    db_path: Path,
    output_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    metric_csv = output_root / "stage4" / "metric_availability.csv"
    scoring_csv = output_root / "stage6" / "scoring.csv"
    metric_builder = load_script("08a_build_transportation_specialized_metrics.py")
    monkeypatch.setattr(
        sys,
        "argv",
        ["metric_builder.py", "--db", str(db_path), "--asof", ASOF, "--output-csv", str(metric_csv)],
    )
    assert metric_builder.main() == 0
    score_builder = load_script("06a_build_transportation_scoring_features.py")
    monkeypatch.setattr(
        sys,
        "argv",
        ["score_builder.py", "--db", str(db_path), "--asof", ASOF, "--output-csv", str(scoring_csv)],
    )
    assert score_builder.main() == 0
    return metric_csv, scoring_csv


def test_specialized_metric_and_scoring_contract_is_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "transportation.sqlite"
    member_count = seed_complete_inputs(db_path)
    metric_csv, scoring_csv = run_feature_pipeline(db_path, tmp_path, monkeypatch)
    metric_rows = read_rows(metric_csv)
    score_rows = read_rows(scoring_csv)
    config = load_yaml(CONFIG_PATH)
    family = family_config(config, "transportation")
    registry_path = INDUSTRIALS_ROOT / family["financial"]["metric_registry"]
    registry = load_yaml(registry_path)
    surface_policy = load_yaml(
        INDUSTRIALS_ROOT / family["scoring"]["surface_freight_score_policy"]
    )
    expected_scored = set(surface_policy["eligible_tickers"])
    assert len(metric_rows) == member_count * len(registry["metrics"])
    assert {row["ticker"] for row in score_rows} == expected_scored
    assert len(score_rows) == 24
    assert sum(row["rank_ready_flag"] == "1" for row in score_rows) == 24
    assert {row["calibration_cohort"] for row in score_rows} == {
        "surface_freight_and_logistics",
    }
    for row in score_rows:
        specialized_count = int(row["specialized_metric_count"])
        specialized_observed = int(row["specialized_metric_observed_count"])
        assert specialized_count > 0
        assert specialized_observed > 0
        assert float(row["specialized_coverage"]) == pytest.approx(
            specialized_observed / specialized_count
        )
    valuation_rows = {
        row["metric_name"]: row
        for row in metric_rows
        if row["ticker"] == "UNP"
        and row["metric_name"] in {"fcf_yield", "ev_operating_income"}
    }
    assert set(valuation_rows) == {"fcf_yield", "ev_operating_income"}
    assert {row["availability_status"] for row in valuation_rows.values()} == {
        "MISSING_MARKET_DENOMINATOR"
    }
    metric_validator = load_script("08a_validate_transportation_specialized_metrics.py")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "validate_metrics.py",
            "--db",
            str(db_path),
            "--asof",
            ASOF,
            "--output-json",
            str(tmp_path / "metric_validation.json"),
        ],
    )
    assert metric_validator.main() == 0
    metric_validation = json.loads((tmp_path / "metric_validation.json").read_text(encoding="utf-8"))
    assert metric_validation["required_coverage"]["coverage_bps"] == 10000
    assert metric_validation["required_coverage"]["observed"] == (
        metric_validation["required_coverage"]["applicable"]
    )
    assert Path(metric_validation["coverage_csv"]).exists()
    score_validator = load_script("06a_validate_transportation_scoring_features.py")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "validate_scores.py",
            "--db",
            str(db_path),
            "--asof",
            ASOF,
            "--input-csv",
            str(scoring_csv),
            "--output-json",
            str(tmp_path / "scoring_validation.json"),
        ],
    )
    assert score_validator.main() == 0


def test_zero_overlay_scoring_is_invariant_to_optional_specialized_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "transportation.sqlite"
    seed_complete_inputs(db_path)
    _, baseline_csv = run_feature_pipeline(db_path, tmp_path / "baseline", monkeypatch)
    baseline = {row["ticker"]: row for row in read_rows(baseline_csv)}
    config = load_yaml(CONFIG_PATH)
    registry_path = (
        INDUSTRIALS_ROOT
        / family_config(config, "transportation")["financial"]["metric_registry"]
    )
    registry = load_yaml(registry_path)
    optional_specialized = sorted(
        str(metric["metric_id"])
        for metric in registry["metrics"]
        if metric.get("specialized") and not metric.get("required_for_rank")
    )
    assert optional_specialized
    placeholders = ",".join("?" for _ in optional_specialized)
    with connect(db_path) as conn:
        conn.execute(
            f"""
            UPDATE feature_financial_metric_availability
            SET availability_status='NOT_DISCLOSED', metric_value=NULL,
                status_reason='zero_overlay_invariance_test'
            WHERE model_family='transportation' AND asof_date=?
              AND metric_name IN ({placeholders})
            """,
            (ASOF, *optional_specialized),
        )
    rescored_csv = tmp_path / "rescored" / "scoring.csv"
    score_builder = load_script("06a_build_transportation_scoring_features.py")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "score_builder.py",
            "--db",
            str(db_path),
            "--asof",
            ASOF,
            "--output-csv",
            str(rescored_csv),
        ],
    )
    assert score_builder.main() == 0
    rescored = {row["ticker"]: row for row in read_rows(rescored_csv)}
    invariant_fields = (
        "final_score",
        "market_trend_score",
        "quality_score",
        "growth_score",
        "valuation_score",
        "operating_efficiency_score",
        "capital_risk_score",
        "score_confidence",
        "rank_ready_flag",
        "rank_ready_reason",
    )
    assert baseline.keys() == rescored.keys()
    for ticker in baseline:
        assert {field: baseline[ticker][field] for field in invariant_fields} == {
            field: rescored[ticker][field] for field in invariant_fields
        }
        assert "specialized_coverage_below" not in rescored[ticker]["rank_ready_reason"]
    manifest = json.loads(
        rescored_csv.with_suffix(".manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["score_construction_mode"] == (
        "surface_freight_fixed_denominator_v2"
    )
    assert manifest["specialized_overlay_active"] is False
    assert all(
        float(weight) == 0.0
        for weight in manifest["specialized_overlay_weights"].values()
    )

def test_missing_required_metric_blocks_instead_of_neutral_fill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "transportation.sqlite"
    seed_complete_inputs(db_path)
    run_feature_pipeline(db_path, tmp_path / "initial", monkeypatch)
    with connect(db_path) as conn:
        tickers = [
            str(row[0])
            for row in conn.execute(
                """
                SELECT t.ticker
                FROM dim_industrials_taxonomy AS t
                JOIN dim_universe_membership AS m
                  ON m.ticker=t.ticker AND m.model_family=t.model_family
                WHERE t.model_family='transportation'
                  AND t.calibration_cohort_id='surface_freight_and_logistics'
                  AND m.membership_source_id='transportation_ticker_seed'
                  AND m.membership_status='active'
                ORDER BY t.ticker LIMIT 2
                """
            ).fetchall()
        ]
        conn.execute(
            """
            UPDATE feature_financial_metric_availability
            SET availability_status='NOT_DISCLOSED', metric_value=NULL,
                status_reason='synthetic_missingness_gate'
            WHERE ticker=? AND model_family='transportation' AND asof_date=?
              AND metric_name='operating_margin'
            """,
            (tickers[0], ASOF),
        )
    score_csv = tmp_path / "missing" / "scoring.csv"
    score_builder = load_script("06a_build_transportation_scoring_features.py")
    monkeypatch.setattr(
        sys,
        "argv",
        ["score_builder.py", "--db", str(db_path), "--asof", ASOF, "--output-csv", str(score_csv)],
    )
    assert score_builder.main() == 0
    rows_by_ticker = {row["ticker"]: row for row in read_rows(score_csv)}
    blocked = rows_by_ticker[tickers[0]]
    complete = rows_by_ticker[tickers[1]]
    assert blocked["rank_ready_flag"] == "0"
    assert "missing_required_metrics:operating_margin" in blocked["rank_ready_reason"]
    assert float(blocked["score_confidence"]) < float(complete["score_confidence"])


def test_cash_generative_runway_is_conditionally_not_applicable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "transportation.sqlite"
    seed_complete_inputs(db_path)
    with connect(db_path) as conn:
        ticker = str(
            conn.execute(
                """
                SELECT t.ticker
                FROM dim_industrials_taxonomy AS t
                JOIN dim_universe_membership AS m
                  ON m.ticker=t.ticker AND m.model_family=t.model_family
                WHERE t.model_family='transportation'
                  AND t.calibration_cohort_id='development_stage_and_speculative_transport'
                  AND m.membership_source_id='transportation_ticker_seed'
                  AND m.membership_status='active'
                ORDER BY t.ticker LIMIT 1
                """
            ).fetchone()[0]
        )
        conn.execute(
            """
            UPDATE feature_financial_statement
            SET cash_burn_ttm_usd=0.0, cash_runway_years=NULL,
                capital_raise_dependence=0.0
            WHERE ticker=? AND model_family='transportation' AND asof_date=?
            """,
            (ticker, ASOF),
        )
    metric_csv, scoring_csv = run_feature_pipeline(db_path, tmp_path, monkeypatch)
    metric_by_name = {
        row["metric_name"]: row
        for row in read_rows(metric_csv)
        if row["ticker"] == ticker
    }
    assert metric_by_name["cash_runway_years"]["availability_status"] == "NOT_APPLICABLE"
    assert metric_by_name["cash_runway_years"]["status_reason"] == (
        "issuer_cash_generative_runway_not_meaningful"
    )
    assert ticker not in {row["ticker"] for row in read_rows(scoring_csv)}
    metric_validator = load_script("08a_validate_transportation_specialized_metrics.py")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "validate_metrics.py",
            "--db",
            str(db_path),
            "--asof",
            ASOF,
            "--output-json",
            str(tmp_path / "metric_validation.json"),
        ],
    )
    assert metric_validator.main() == 0


def test_nonpositive_operating_income_has_explicit_valuation_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "transportation.sqlite"
    seed_complete_inputs(db_path)
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE feature_financial_statement
            SET market_cap=1000000000.0,
                operating_income_ttm_usd=-100000000.0,
                ev_operating_income=-10.0
            WHERE ticker='UNP' AND model_family='transportation'
              AND asof_date=?
            """,
            (ASOF,),
        )
    metric_csv, _ = run_feature_pipeline(db_path, tmp_path, monkeypatch)
    metrics = {
        row["metric_name"]: row
        for row in read_rows(metric_csv)
        if row["ticker"] == "UNP"
        and row["metric_name"] in {"fcf_yield", "ev_operating_income"}
    }
    assert metrics["fcf_yield"]["availability_status"] == "REPORTED"
    assert metrics["ev_operating_income"]["availability_status"] == (
        "NEGATIVE_PROFIT_NOT_MEANINGFUL"
    )


def test_nonpositive_net_income_makes_fcf_conversion_not_applicable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "transportation.sqlite"
    seed_complete_inputs(db_path)
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE feature_financial_statement
            SET net_income_ttm_usd=-1000000.0,
                fcf_to_net_income=NULL
            WHERE ticker='UNP' AND model_family='transportation'
              AND asof_date=?
            """,
            (ASOF,),
        )
    metric_csv, _ = run_feature_pipeline(db_path, tmp_path, monkeypatch)
    metric = next(
        row
        for row in read_rows(metric_csv)
        if row["ticker"] == "UNP" and row["metric_name"] == "fcf_conversion"
    )
    assert metric["availability_status"] == "NOT_APPLICABLE"
    assert metric["status_reason"] == (
        "fcf_conversion_not_meaningful_nonpositive_net_income"
    )
    metric_validator = load_script("08a_validate_transportation_specialized_metrics.py")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "validate_metrics.py",
            "--db",
            str(db_path),
            "--asof",
            ASOF,
            "--output-json",
            str(tmp_path / "metric_validation.json"),
        ],
    )
    assert metric_validator.main() == 0


def test_shadow_publish_is_deterministic_and_portfolio_adapter_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "transportation.sqlite"
    seed_complete_inputs(db_path)
    _, scoring_csv = run_feature_pipeline(db_path, tmp_path / "features", monkeypatch)
    publisher = load_script("17_publish_transportation_shadow_rank_table.py")
    rank_paths: list[Path] = []
    for label in ("one", "two"):
        output_dir = tmp_path / label / "industrials" / "transportation" / "dashboard" / ASOF
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "publish.py",
                "--asof",
                ASOF,
                "--input-csv",
                str(scoring_csv),
                "--output-dir",
                str(output_dir),
            ],
        )
        assert publisher.main() == 0
        rank_paths.append(output_dir / "transportation_final_rank_table.csv")
    assert rank_paths[0].read_bytes() == rank_paths[1].read_bytes()
    ranks = read_rows(rank_paths[0])
    assert [int(row["final_rank"]) for row in ranks] == list(range(1, 25))
    assert {row["portfolio_candidate_gate"] for row in ranks} == {"0"}
    assert {row["oos_score_valid_flag"] for row in ranks} == {"0"}
    portfolio_config = load_yaml(PROJECT_ROOT / "portfolio_layer" / "config.yaml")
    source = next(
        item for item in portfolio_config["score_contract"]["sectors"]
        if item["model_family"] == "transportation"
    )
    result = run_adapter(source, tmp_path / "one", ASOF)
    assert result.source_pipeline == "transportation"
    assert result.adapter == "industrial_family"
    assert result.source_asof_date == ASOF
    assert len(result.rows) == 24
    assert not any(row.investable_eligible for row in result.rows)
    assert not any(row.oos_score_valid_flag for row in result.rows)
    assert not any(row.calibration_research_eligible for row in result.rows)
    rank_validator = load_script("18_validate_transportation_shadow_rank_table.py")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "validate_rank.py",
            "--db",
            str(db_path),
            "--asof",
            ASOF,
            "--input-csv",
            str(rank_paths[0]),
            "--output-json",
            str(tmp_path / "rank_validation.json"),
        ],
    )
    assert rank_validator.main() == 0
    adapter_validator = load_script("20_validate_transportation_portfolio_adapter_shadow.py")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "validate_adapter.py",
            "--asof",
            ASOF,
            "--sector-output-root",
            str(tmp_path / "one"),
            "--output-json",
            str(tmp_path / "adapter_validation.json"),
        ],
    )
    assert adapter_validator.main() == 0

    versioned_validation = tmp_path / "versioned_adapter_validation.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "validate_adapter.py",
            "--asof",
            ASOF,
            "--input-csv",
            str(rank_paths[0]),
            "--output-json",
            str(versioned_validation),
        ],
    )
    assert adapter_validator.main() == 0
    assert json.loads(versioned_validation.read_text(encoding="utf-8"))["acceptance"] == "PASS"


def test_production_lock_keeps_research_roles_out_of_portfolio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "transportation.sqlite"
    seed_complete_inputs(db_path)
    _, scoring_csv = run_feature_pipeline(db_path, tmp_path / "features", monkeypatch)
    weights = {field: 0.0 for field in COMPONENT_FIELDS}
    weights["market_trend_score"] = 1.0
    lock = ProductionLock(
        model_family="transportation",
        lock_id="test",
        effective_from=date.fromisoformat(ASOF),
        effective_to=None,
        lock_date=date.fromisoformat(ASOF),
        train_start_date=date(2019, 1, 2),
        train_end_date=date(2025, 12, 31),
        scoring_mode="generic_oos",
        score_model_version="test_transportation_oos",
        validation_method="test",
        decision_manifest_path=tmp_path / "decision.json",
        decision_manifest_sha256="test",
        weights=weights,
    )
    rows = finalize_rank_rows(
        read_rows(scoring_csv),
        score_model_version="shadow",
        model_version="shadow",
        scoring_contract_version="shadow",
        production_lock=lock,
    )
    by_ticker = {row["ticker"]: row for row in rows}
    assert {"CISS", "DAL", "PBI"}.isdisjoint(by_ticker)
    assert len(by_ticker) == 24
    assert by_ticker["UNP"]["oos_score_valid_flag"] == "1"
    assert by_ticker["UNP"]["portfolio_candidate_gate"] == "1"



def test_financial_and_fx_wrappers_are_family_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []

    def fake_run(path: str, *, run_name: str) -> None:
        assert path
        assert run_name == "__main__"
        captured.append(list(sys.argv))

    monkeypatch.setattr(_shared.runpy, "run_path", fake_run)
    monkeypatch.setattr(sys, "argv", ["wrapper.py", "--asof", ASOF])
    _shared.run_financial_shared("08_build_industrials_financial_features.py")
    assert captured[-1][captured[-1].index("--model-family") + 1] == "transportation"
    monkeypatch.setattr(sys, "argv", ["wrapper.py", "--asof", ASOF])
    _shared.run_fx_shared()
    assert captured[-1][captured[-1].index("--pairs") + 1] == (
        "BRLUSD,CADUSD,CLPUSD,CNYUSD,COPUSD,EURUSD,GBPUSD,MXNUSD,NOKUSD"
    )
    monkeypatch.setattr(sys, "argv", ["wrapper.py", "--model-family=defense"])
    with pytest.raises(ValueError, match="pinned"):
        _shared.run_financial_shared("08_build_industrials_financial_features.py")


def test_xbrl_candidate_audit_keeps_components_and_receivables_out_of_revenue() -> None:
    audit = load_script("08b_audit_transportation_xbrl_tag_candidates.py")
    approved = {
        (
            "ifrs-full",
            "RevenueFromRenderingOfTransportServices",
            "revenue",
        )
    }
    assert audit.candidate_decision(
        concept_name="RevenueFromRenderingOfTransportServices",
        existing_metrics=set(),
        approved_aliases=approved,
        taxonomy="ifrs-full",
    ) == ("APPROVED_ALIAS", "revenue", "remap_companyfacts")
    assert audit.candidate_decision(
        concept_name="RevenueFromRenderingOfPassengerTransportServices",
        existing_metrics=set(),
        approved_aliases=approved,
        taxonomy="ifrs-full",
    ) == ("COMPONENT_ONLY", "", "retain_as_component_not_total_revenue")
    assert audit.candidate_decision(
        concept_name="AccruedFeesAndOtherRevenueReceivable",
        existing_metrics=set(),
        approved_aliases=approved,
        taxonomy="us-gaap",
    ) == ("NOT_TOTAL_REVENUE", "", "do_not_alias")


def test_required_metric_gap_audit_only_routes_reusable_core_concepts() -> None:
    audit = load_script("08d_audit_transportation_required_metric_gaps.py")
    assert (
        audit.candidate_dependency("PaymentsToAcquireEquipmentOnLease")
        == "capex"
    )
    assert (
        audit.candidate_dependency("PaymentsToAcquireMachineryAndEquipment")
        == "capex"
    )
    assert (
        audit.candidate_dependency("ProceedsFromCurrentBorrowings")
        == "debt_issuance_proceeds"
    )
    assert audit.candidate_dependency("OperatingIncomeLoss") == "operating_income"
    assert audit.candidate_dependency("ProceedsFromCollectionOfLoansReceivable") == ""
    assert audit.candidate_dependency("PaymentsToAcquireLoansReceivable") == ""

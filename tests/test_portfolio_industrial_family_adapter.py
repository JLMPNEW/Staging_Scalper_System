from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

import pytest

from portfolio_layer.core.contracts import read_csv
from portfolio_layer.core.db import connect, init_db
from portfolio_layer.scores.adapters import (
    INDUSTRIAL_FAMILY_ONE_OF_COLUMNS,
    INDUSTRIAL_FAMILY_REQUIRED_COLUMNS,
    run_adapter,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASOF = "2026-07-09"
RANK_REL = Path("industrials") / "machinery" / "dashboard" / ASOF / "machinery_final_rank_table.csv"


def machinery_row(ticker: str, rank: int, score: float, company_name: str) -> dict[str, str]:
    row = {field: "" for field in INDUSTRIAL_FAMILY_REQUIRED_COLUMNS}
    for group in INDUSTRIAL_FAMILY_ONE_OF_COLUMNS:
        row[group[-1]] = ""
    row.update(
        {
            "asof_date": ASOF,
            "ticker": ticker,
            "company_name": company_name,
            "sector": "Industrials",
            "industry": "Machinery",
            "subsector": "Machinery",
            "calibration_cohort": "diversified_machinery_automation_and_tools",
            "final_score": str(score),
            "final_rank": str(rank),
            "rank_ready_flag": "1",
            "model_status": "complete",
            "score_confidence": "0.9",
            "score_model_version": "machinery_shadow_v0.1.0",
            "model_version": "machinery_shadow_2026_07",
            "scoring_contract_version": "industrial_family_final_rank_table_v1_shadow",
            "portfolio_candidate_gate": "0",
            "portfolio_candidate_score": str(score),
            "portfolio_candidate_status": "shadow_only",
            "portfolio_candidate_reason": "shadow_only",
            "calibration_eligible_flag": "0",
            "research_calibration_input_eligible_flag": "0",
            "research_calibration_reason": "shadow_only",
            "calibration_sample_role": "excluded",
            "stage11_calibration_panel_source": "dashboard_rank_snapshot_current_universe_replay",
            "stage11_calibration_input_eligible_flag": "0",
            "stage11_calibration_input_reason": "shadow_only",
            "survivorship_corrected_panel_flag": "0",
            "oos_score_valid_flag": "0",
            "oos_invalid_reason": "shadow_pre_oos",
            "market_cap": "10000000000",
            "market_cap_source": "synthetic_fixture",
            "avg_dollar_volume_60d": "50000000",
            "liquidity_capacity_reason": "synthetic_fixture",
            "valuation_score": "55",
            "quality_score": "60",
            "risk_control_score": "58",
            "positioning_score": "52",
            "market_behavior_score": "61",
            "growth_score": "57",
            "sector_cycle_score": "50",
            "industrial_cycle_score": "50",
            "orders_backlog_score": "50",
            "capex_cycle_score": "50",
            "development_stage_risk_score": "50",
            "financial_lineage_checked_asof_date": ASOF,
            "financial_lineage_status": "INCORPORATED",
            "financial_lineage_gate": "1",
            "financial_lineage_classification": "INCORPORATED",
            "latest_material_financial_filing_date": "2026-05-01",
            "latest_material_financial_form": "10-Q",
            "latest_material_financial_accession": f"{ticker.lower()}-latest",
            "latest_material_financial_report_date": "2026-03-31",
            "incorporated_financial_filing_date": "2026-05-01",
            "incorporated_financial_accession": f"{ticker.lower()}-latest",
            "incorporated_financial_report_date": "2026-03-31",
            "incorporated_financial_core_metric_count": "3",
            "financial_lineage_reason": "synthetic_fixture_incorporated",
        }
    )
    return row


def production_machinery_row(ticker: str, rank: int, score: float, company_name: str) -> dict[str, str]:
    row = machinery_row(ticker, rank, score, company_name)
    row.update(
        {
            "portfolio_candidate_gate": "1",
            "portfolio_candidate_status": "eligible",
            "portfolio_candidate_reason": "ok",
            "calibration_eligible_flag": "1",
            "research_calibration_input_eligible_flag": "1",
            "research_calibration_reason": "ok",
            "calibration_sample_role": "strict_oos",
            "stage11_calibration_input_eligible_flag": "1",
            "stage11_calibration_input_reason": "ok",
            "oos_score_valid_flag": "1",
            "oos_score_asof_date": ASOF,
            "oos_invalid_reason": "",
            "calibration_lock_date": ASOF,
        }
    )
    return row


def sidecar_machinery_row(ticker: str, rank: int, score: float, company_name: str) -> dict[str, str]:
    row = machinery_row(ticker, rank, score, company_name)
    row.update(
        {
            "portfolio_candidate_reason": "sidecar_calibration_only",
            "research_calibration_input_eligible_flag": "1",
            "research_calibration_reason": "ok",
            "calibration_sample_role": "pre_lock_research",
            "stage11_calibration_panel_source": "survivorship_corrected_pit_membership_score_recompute",
            "stage11_calibration_input_eligible_flag": "1",
            "stage11_calibration_input_reason": "ok",
            "survivorship_corrected_panel_flag": "1",
        }
    )
    return row


def write_rows(path: Path, rows: list[dict[str, str]], *, omit_field: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({field for row in rows for field in row if field != omit_field})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_machinery_rank_table(path: Path, *, omit_field: str = "") -> None:
    write_rows(
        path,
        [
            machinery_row("CAT", 1, 72.0, "Caterpillar Inc."),
            machinery_row("PH", 2, 61.0, "Parker-Hannifin Corporation"),
            machinery_row("CMI", 3, 48.0, "Cummins Inc."),
        ],
        omit_field=omit_field,
    )


def adapter_cfg(*, require_oos_score_valid: bool = True) -> dict[str, object]:
    rank_pattern = str(RANK_REL).replace("\\", "/").replace(ASOF, "{yyyy-mm-dd}")
    return {
        "model_family": "machinery",
        "adapter": "industrial_family",
        "file_mode": "dated",
        "file_path": rank_pattern,
        "sector": "Industrials",
        "industry": "Machinery",
        "industry_aggregate": "Machinery",
        "require_oos_score_valid": require_oos_score_valid,
    }


def write_portfolio_config(path: Path) -> None:
    rank_pattern = str(RANK_REL).replace("\\", "/").replace(ASOF, "{yyyy-mm-dd}")
    med_devices = """
    - model_family: med_devices
      adapter: med_devices
      enabled: true
      required: true
      staleness_tolerance_days: 3
      sector: "Health Care"
      industry: "Health Care Equipment"
      industry_aggregate: "Health Care Equipment & Services"
      file_mode: dated
      file_path: "med_devices/{yyyy-mm-dd}/med_device_daily_composite_scores.csv"
      require_oos_score_valid: true
      calibration:
        neutral: 50.0
        scale: 50.0
        expected_alpha_at_full: 0.15
"""
    path.write_text(
        f"""
paths:
  database_path: "db/portfolio_layer.sqlite"
  output_dir: "output"
  cache_dir: "output/cache"
  macro_serving_db_path: "macro.sqlite"
runtime:
  sqlite_timeout_sec: 30.0
score_contract:
  contract_version: "stocks_scores_v1"
  sector_output_root: "sector_output"
  staleness_tolerance_days: 10
  min_successful_sectors: 2
  native_score_range:
    min: 0.0
    max: 100.0
  max_abs_expected_alpha: 1.0
  rating_bands:
    strong_buy: 90.0
    buy: 70.0
    hold: 40.0
    reduce: 20.0
    avoid: 0.0
  sectors:
    - model_family: machinery
      adapter: industrial_family
      enabled: true
      required: true
      staleness_tolerance_days: 3
      sector: "Industrials"
      industry: "Machinery"
      industry_aggregate: "Machinery"
      file_mode: dated
      file_path: "{rank_pattern}"
      require_oos_score_valid: true
      calibration:
        neutral: 50.0
        scale: 50.0
        expected_alpha_at_full: 0.15
{med_devices}
""".lstrip(),
        encoding="utf-8",
    )


def write_med_devices_file(path: Path) -> None:
    write_rows(
        path,
        [
            {
                "asof_date": ASOF,
                "ticker": "MDTEST",
                "portfolio_candidate_gate": "1",
                "portfolio_candidate_score": "75",
                "portfolio_candidate_reason": "ok",
                "analyst_review_decision": "approve",
                "rank": "1",
                "score_confidence": "0.9",
                "calibration_eligible_flag": "1",
                "research_calibration_input_eligible_flag": "1",
                "calibration_sample_role": "strict_oos",
                "oos_score_valid_flag": "1",
                "oos_score_asof_date": ASOF,
                "survivorship_corrected_panel_flag": "0",
            }
        ],
    )


def run_stage(script: str, config_path: Path) -> None:
    subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / script),
            "--config",
            str(config_path),
            "--as-of",
            ASOF,
            "--force",
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_industrial_family_adapter_reads_shadow_machinery_rank_table(tmp_path: Path) -> None:
    sector_root = tmp_path / "sector_output"
    write_machinery_rank_table(sector_root / RANK_REL)

    result = run_adapter(adapter_cfg(), sector_root, ASOF)

    assert result.adapter == "industrial_family"
    assert result.source_pipeline == "machinery"
    assert result.source_asof_date == ASOF
    assert [row.ticker for row in result.rows] == ["CAT", "PH", "CMI"]
    assert {row.investable_eligible for row in result.rows} == {0}
    assert {row.oos_score_valid_flag for row in result.rows} == {0}
    assert {row.calibration_research_eligible for row in result.rows} == {0}
    assert {row.stage1_sample_role for row in result.rows} == {"excluded"}


def test_industrial_family_adapter_fails_when_calibration_field_missing(tmp_path: Path) -> None:
    sector_root = tmp_path / "sector_output"
    write_machinery_rank_table(sector_root / RANK_REL, omit_field="stage11_calibration_input_eligible_flag")

    with pytest.raises(ValueError, match="stage11_calibration_input_eligible_flag"):
        run_adapter(adapter_cfg(), sector_root, ASOF)


def test_industrial_family_adapter_respects_candidate_status(tmp_path: Path) -> None:
    sector_root = tmp_path / "sector_output"
    denied = production_machinery_row("BAD", 1, 80.0, "Bad Status Corp.")
    denied["portfolio_candidate_status"] = "shadow_only"
    denied["portfolio_candidate_reason"] = "ok"
    write_rows(sector_root / RANK_REL, [denied])

    result = run_adapter(adapter_cfg(), sector_root, ASOF)

    assert len(result.rows) == 1
    assert result.rows[0].investable_eligible == 0
    assert result.rows[0].eligibility_reason == "portfolio_candidate_status:shadow_only"
    assert result.rows[0].calibration_research_eligible == 1
    assert result.rows[0].stage1_sample_role == "strict_oos"


def test_tech_family_keeps_original_gate_semantics_when_status_is_noneligible(tmp_path: Path) -> None:
    sector_root = tmp_path / "sector_output"
    row = production_machinery_row("DEF", 1, 80.0, "Defense Contract Fixture")
    row["portfolio_candidate_status"] = "shadow_only"
    row["portfolio_candidate_reason"] = "ok"
    path = sector_root / RANK_REL
    write_rows(path, [row])
    cfg = adapter_cfg()
    cfg["model_family"] = "defense"
    cfg["adapter"] = "tech_family"

    result = run_adapter(cfg, sector_root, ASOF)

    assert len(result.rows) == 1
    assert result.rows[0].investable_eligible == 1
    assert result.rows[0].eligibility_reason == "ok"


def test_industrial_family_adapter_accepts_production_oos_rows(tmp_path: Path) -> None:
    sector_root = tmp_path / "sector_output"
    write_rows(sector_root / RANK_REL, [production_machinery_row("CAT", 1, 72.0, "Caterpillar Inc.")])

    result = run_adapter(adapter_cfg(), sector_root, ASOF)

    assert len(result.rows) == 1
    assert result.rows[0].investable_eligible == 1
    assert result.rows[0].eligibility_reason == "ok"
    assert result.rows[0].calibration_research_eligible == 1
    assert result.rows[0].oos_score_valid_flag == 1
    assert result.rows[0].stage1_sample_role == "strict_oos"


def test_industrial_family_adapter_merges_sidecar_only_calibration_rows(tmp_path: Path) -> None:
    sector_root = tmp_path / "sector_output"
    rank_path = sector_root / RANK_REL
    write_rows(rank_path, [machinery_row("CAT", 1, 72.0, "Caterpillar Inc.")])
    write_rows(
        rank_path.with_name("machinery_stage11_survivorship_calibration_panel.csv"),
        [sidecar_machinery_row("OLD", 99, 42.0, "Old Machinery Corp.")],
    )

    result = run_adapter(adapter_cfg(), sector_root, ASOF)
    by_ticker = {row.ticker: row for row in result.rows}

    assert sorted(by_ticker) == ["CAT", "OLD"]
    assert by_ticker["OLD"].investable_eligible == 0
    assert by_ticker["OLD"].calibration_research_eligible == 1
    assert by_ticker["OLD"].survivorship_corrected_panel_flag == 1
    assert by_ticker["OLD"].stage1_sample_role == "pre_lock_research"


def test_industrial_family_adapter_fails_invalid_sidecar_schema(tmp_path: Path) -> None:
    sector_root = tmp_path / "sector_output"
    rank_path = sector_root / RANK_REL
    write_rows(rank_path, [machinery_row("CAT", 1, 72.0, "Caterpillar Inc.")])
    write_rows(
        rank_path.with_name("machinery_stage11_survivorship_calibration_panel.csv"),
        [sidecar_machinery_row("OLD", 99, 42.0, "Old Machinery Corp.")],
        omit_field="final_score",
    )

    with pytest.raises(ValueError, match="OLD.*final_score"):
        run_adapter(adapter_cfg(), sector_root, ASOF)


def test_machinery_file_smokes_through_portfolio_score_stages(tmp_path: Path) -> None:
    config_path = tmp_path / "portfolio_layer_smoke.yaml"
    write_portfolio_config(config_path)
    write_machinery_rank_table(tmp_path / "sector_output" / RANK_REL)
    write_med_devices_file(
        tmp_path / "sector_output" / "med_devices" / ASOF / "med_device_daily_composite_scores.csv"
    )
    with connect(tmp_path / "db" / "portfolio_layer.sqlite") as conn:
        init_db(conn)

    run_stage("portfolio_layer/scores/01_collect_sector_scores.py", config_path)
    run_stage("portfolio_layer/scores/02_calibrate_cross_sector_scores.py", config_path)
    run_stage("portfolio_layer/scores/03_validate_score_contract.py", config_path)

    run_dir = tmp_path / "output" / "runs" / ASOF
    collected = read_csv(run_dir / "collected_scores.csv")
    stocks = read_csv(run_dir / "stocks_scores.csv")
    validation = read_csv(run_dir / "validation" / "score_contract_validation.csv")

    machinery_collected = [row for row in collected if row["source_pipeline"] == "machinery"]
    machinery_stocks = [row for row in stocks if row["source_pipeline"] == "machinery"]
    assert len(machinery_collected) == 3
    assert len(machinery_stocks) == 3
    assert {row["investable_eligible"] for row in machinery_stocks} == {"0"}
    assert {row["calibration_research_eligible"] for row in machinery_stocks} == {"0"}
    assert {row["oos_score_valid_flag"] for row in machinery_stocks} == {"0"}
    assert {row["stage1_sample_role"] for row in machinery_stocks} == {"excluded"}
    assert all(row["source_asof_date"] == ASOF for row in machinery_stocks)
    hard_failures = [row for row in validation if row["status"] not in {"PASS", "WARN", "DEFERRED"}]
    assert hard_failures == []

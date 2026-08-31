from __future__ import annotations

import csv
from pathlib import Path

import pytest

from orchestration_contracts.financial_lineage import LINEAGE_FIELDS
from portfolio_layer.scores.adapters import run_adapter


def _config() -> dict[str, object]:
    return {
        "model_family": "semiconductors",
        "adapter": "tech_family",
        "file_mode": "dated",
        "file_path": "scores/{yyyy-mm-dd}/semiconductor_final_rank_table.csv",
        "financial_lineage_file_path": (
            "lineage/{yyyy-mm-dd}/semiconductor_financial_lineage_shadow.csv"
        ),
        "sector": "Information Technology",
        "industry": "Semiconductors",
        "industry_aggregate": "Semiconductors & Semiconductor Equipment",
        "require_oos_score_valid": True,
    }


def _rank_row(asof: str) -> dict[str, str]:
    return {
        "ticker": "SAFE",
        "asof_date": asof,
        "final_score": "72.5",
        "rank_ready_flag": "1",
        "calibration_eligible_flag": "1",
        "model_status": "complete",
        "oos_score_valid_flag": "1",
        "portfolio_candidate_gate": "1",
        "portfolio_candidate_status": "eligible",
        "portfolio_candidate_reason": "ok",
    }


def _lineage_row(asof: str) -> dict[str, str]:
    row = {
        **_rank_row(asof),
        "financial_lineage_checked_asof_date": asof,
        "financial_lineage_status": "INCORPORATED",
        "financial_lineage_gate": "1",
        "financial_lineage_classification": "INCORPORATED",
        "latest_material_financial_filing_date": asof,
        "latest_material_financial_form": "6-K",
        "latest_material_financial_accession": "latest-accession",
        "latest_material_financial_report_date": "2026-06-30",
        "incorporated_financial_filing_date": asof,
        "incorporated_financial_accession": "latest-accession",
        "incorporated_financial_report_date": "2026-06-30",
        "incorporated_financial_core_metric_count": "4",
        "financial_lineage_reason": "latest_material_filing_incorporated",
    }
    assert not set(LINEAGE_FIELDS).difference(row)
    return row


def _write_csv(path: Path, row: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)


def _rank_path(root: Path, asof: str) -> Path:
    return root / "scores" / asof / "semiconductor_final_rank_table.csv"


def _lineage_path(root: Path, asof: str) -> Path:
    return (
        root
        / "lineage"
        / asof
        / "semiconductor_financial_lineage_shadow.csv"
    )


def _stage11_sidecar_path(root: Path, asof: str) -> Path:
    return (
        root
        / "scores"
        / asof
        / "semiconductor_stage11_survivorship_calibration_panel.csv"
    )


def test_current_production_joins_exact_dated_lineage_sidecar(
    tmp_path: Path,
) -> None:
    asof = "2026-08-14"
    rank_path = _rank_path(tmp_path, asof)
    lineage_path = _lineage_path(tmp_path, asof)
    _write_csv(rank_path, _rank_row(asof))
    _write_csv(lineage_path, _lineage_row(asof))

    result = run_adapter(_config(), tmp_path, asof)

    assert len(result.rows) == 1
    assert result.rows[0].investable_eligible == 1
    assert result.rows[0].financial_lineage_gate == 1
    assert result.rows[0].incorporated_financial_accession == "latest-accession"
    assert result.source_files == (rank_path.resolve(), lineage_path.resolve())


def test_current_production_fails_when_lineage_sidecar_is_missing(
    tmp_path: Path,
) -> None:
    asof = "2026-08-14"
    _write_csv(_rank_path(tmp_path, asof), _rank_row(asof))

    with pytest.raises(FileNotFoundError, match="Missing production financial lineage sidecar"):
        run_adapter(_config(), tmp_path, asof)


def test_pre_activation_history_does_not_require_lineage_sidecar(
    tmp_path: Path,
) -> None:
    asof = "2026-08-13"
    rank_path = _rank_path(tmp_path, asof)
    _write_csv(rank_path, _rank_row(asof))

    result = run_adapter(_config(), tmp_path, asof)

    assert len(result.rows) == 1
    assert result.rows[0].investable_eligible == 1
    assert result.rows[0].financial_lineage_status == ""
    assert result.source_files == (rank_path.resolve(),)


def test_lineage_sidecar_must_match_rank_candidate_contract(
    tmp_path: Path,
) -> None:
    asof = "2026-08-14"
    _write_csv(_rank_path(tmp_path, asof), _rank_row(asof))
    lineage = _lineage_row(asof)
    lineage["rank_ready_flag"] = "0"
    _write_csv(_lineage_path(tmp_path, asof), lineage)

    with pytest.raises(ValueError, match="lineage/rank mismatch"):
        run_adapter(_config(), tmp_path, asof)


def test_stage11_sidecar_only_row_is_research_only_not_production(
    tmp_path: Path,
) -> None:
    asof = "2026-08-14"
    rank_path = _rank_path(tmp_path, asof)
    lineage_path = _lineage_path(tmp_path, asof)
    sidecar_path = _stage11_sidecar_path(tmp_path, asof)
    _write_csv(rank_path, _rank_row(asof))
    _write_csv(lineage_path, _lineage_row(asof))
    sidecar_row = {
        **_rank_row(asof),
        "ticker": "RESEARCH",
        "stage11_calibration_input_eligible_flag": "1",
        "stage11_calibration_input_reason": "ok",
        "survivorship_corrected_panel_flag": "1",
        "calibration_sample_role": "strict_oos",
    }
    _write_csv(sidecar_path, sidecar_row)

    result = run_adapter(_config(), tmp_path, asof)

    by_ticker = {row.ticker: row for row in result.rows}
    assert by_ticker["SAFE"].investable_eligible == 1
    assert by_ticker["RESEARCH"].investable_eligible == 0
    assert by_ticker["RESEARCH"].eligibility_reason == "stage11_sidecar_calibration_only"
    assert by_ticker["RESEARCH"].calibration_research_eligible == 1
    assert by_ticker["RESEARCH"].calibration_sample_role == "strict_oos"
    assert by_ticker["RESEARCH"].financial_lineage_status == ""
    assert result.source_files == (
        rank_path.resolve(),
        lineage_path.resolve(),
        sidecar_path.resolve(),
    )

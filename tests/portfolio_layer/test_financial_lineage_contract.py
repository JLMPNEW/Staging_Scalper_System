from __future__ import annotations

import sqlite3

import pytest

from portfolio_layer.core.contracts import FINANCIAL_LINEAGE_FIELDS, upsert_stocks_scores
from portfolio_layer.scores.adapters import _adapt_final_rank_family


def _native_row() -> dict[str, str]:
    row = {
        "ticker": "CAE",
        "asof_date": "2026-08-13",
        "final_score": "71.5",
        "rank_ready_flag": "1",
        "calibration_eligible_flag": "1",
        "model_status": "complete",
        "oos_score_valid_flag": "1",
        "portfolio_candidate_gate": "1",
        "portfolio_candidate_status": "eligible",
        "portfolio_candidate_reason": "ok",
        "financial_lineage_checked_asof_date": "2026-08-13",
        "financial_lineage_status": "INCORPORATED",
        "financial_lineage_gate": "1",
        "financial_lineage_classification": "INCORPORATED",
        "latest_material_financial_filing_date": "2026-08-12",
        "latest_material_financial_form": "6-K",
        "latest_material_financial_accession": "cover",
        "latest_material_financial_report_date": "2026-06-30",
        "incorporated_financial_filing_date": "2026-08-12",
        "incorporated_financial_accession": "data",
        "incorporated_financial_report_date": "2026-06-30",
        "incorporated_financial_core_metric_count": "3",
        "financial_lineage_reason": "latest_material_filing_incorporated",
    }
    assert not set(FINANCIAL_LINEAGE_FIELDS).difference(row)
    return row


def _config() -> dict[str, object]:
    return {
        "model_family": "machinery",
        "sector": "Industrials",
        "industry": "Machinery",
        "industry_aggregate": "Machinery",
        "require_oos_score_valid": True,
        "require_financial_lineage": True,
    }


def test_adapter_requires_complete_lineage_columns() -> None:
    row = _native_row()
    del row["incorporated_financial_accession"]

    with pytest.raises(ValueError, match="missing financial lineage fields"):
        _adapt_final_rank_family(_config(), [row], enforce_candidate_status=True)


def test_adapter_demotes_unresolved_lineage_from_investable_and_research() -> None:
    row = _native_row()
    row.update(
        {
            "financial_lineage_status": "REVIEW_REQUIRED",
            "financial_lineage_gate": "0",
            "stage11_calibration_input_eligible_flag": "1",
            "stage11_calibration_input_reason": "ok",
            "calibration_sample_role": "strict_oos",
        }
    )

    adapted = _adapt_final_rank_family(
        _config(),
        [row],
        enforce_candidate_status=True,
    )[0]

    assert adapted.investable_eligible == 0
    assert adapted.calibration_research_eligible == 0
    assert adapted.eligibility_reason == "financial_lineage:REVIEW_REQUIRED"
    assert adapted.stage1_sample_role == "excluded"


def test_lineage_columns_persist_in_portfolio_database() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row = {
        "as_of_date": "2026-08-13",
        "ticker": "CAE",
        "source_pipeline": "machinery",
        "sector": "Industrials",
        "industry": "Machinery",
        "industry_aggregate": "Machinery",
        "final_score": 0.06,
        "rating": "buy",
        "within_sector_percentile": 75.0,
        "score_confidence": 0.9,
        "investable_eligible": 1,
        "eligibility_reason": "ok",
        "native_score": 71.5,
        "calibration_research_eligible": 1,
        "calibration_research_reason": "ok",
        "calibration_sample_role": "strict_oos",
        "stage1_sample_role": "strict_oos",
        "oos_score_valid_flag": 1,
        "missing_score_flag": 0,
        "survivorship_corrected_panel_flag": 1,
        "source_asof_date": "2026-08-13",
        "staleness_days": 0,
        "score_version": "test",
        **{field: _native_row()[field] for field in FINANCIAL_LINEAGE_FIELDS},
    }

    assert upsert_stocks_scores(conn, "2026-08-13", [row]) == 1
    stored = conn.execute(
        "SELECT financial_lineage_status, financial_lineage_gate, "
        "incorporated_financial_accession FROM stocks_scores WHERE ticker='CAE'"
    ).fetchone()

    assert dict(stored) == {
        "financial_lineage_status": "INCORPORATED",
        "financial_lineage_gate": 1,
        "incorporated_financial_accession": "data",
    }

from __future__ import annotations

import csv
import json
import runpy
from pathlib import Path

import pytest

from dedicated_parser.contracts import file_sha256


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "industrials" / "defense" / "scripts" / "08h_build_defense_pair_adjudication_queue.py"
REVIEW_SCRIPT_PATH = (
    PROJECT_ROOT / "industrials" / "defense" / "scripts" / "08g_build_defense_evidence_review_package.py"
)
METRICS = (
    "orders",
    "funded_backlog",
    "reported_backlog",
    "remaining_performance_obligation",
    "rpo_current",
)


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _comparison_row(
    metric_name: str,
    *,
    recovery_class: str,
    covered: int,
    value: object = "",
) -> dict[str, object]:
    return {
        "ticker": "TEST",
        "company_name": "Test Defense",
        "cik": "0000000001",
        "calibration_cohort": "defense_primes_and_services",
        "membership_status": "active",
        "metric_name": metric_name,
        "recovery_class": recovery_class,
        "baseline_status": "NOT_DISCLOSED",
        "baseline_value": "",
        "baseline_covered_flag": 0,
        "shadow_predicted_status": ("REPORTED_SHADOW" if covered else "NOT_DISCLOSED"),
        "shadow_value": value,
        "shadow_period_end": "2026-03-31" if covered else "",
        "shadow_covered_flag": covered,
        "current_match_mode": "exact_anchor" if covered else "none",
        "current_evidence_period_end": ("2026-03-31" if covered else ""),
        "current_evidence_age_days": "115" if covered else "",
        "accepted_current_count": covered,
        "accepted_historical_count": 0,
        "review_required_count": (1 if recovery_class == "FOUND_AMBIGUOUS" else 0),
        "rejected_count": 0,
        "parser_failure_count": 0,
        "searched_filing_count": 10,
        "searched_document_count": 20,
        "failed_filing_count": 0,
        "missing_cache_filing_count": 0,
    }


def _evidence_row(
    metric_name: str,
    *,
    recovery_class: str,
    candidate_status: str = "ACCEPTED",
    evidence_key: str = "",
    value: object = "",
) -> dict[str, object]:
    evidence = bool(evidence_key)
    return {
        "record_type": ("evidence" if evidence else "assessment_no_evidence"),
        "ticker": "TEST",
        "metric_name": metric_name,
        "recovery_class": recovery_class,
        "candidate_status": candidate_status if evidence else "",
        "candidate_value": value if evidence else "",
        "unit": "USD" if evidence else "",
        "period_start": "",
        "period_end": "2026-03-31" if evidence else "",
        "scope": "consolidated" if evidence else "",
        "confidence": "0.95" if evidence else "",
        "concept_name": "Backlog" if evidence else "",
        "accession_number": ("0000000001-26-000001" if evidence else ""),
        "form_type": "8-K" if evidence else "",
        "filing_date": "2026-04-15" if evidence else "",
        "accepted_at": "2026-04-15T12:00:00Z" if evidence else "",
        "source_document": "ex99-1.htm" if evidence else "",
        "source_path": "cache/ex99-1.htm" if evidence else "",
        "source_content_sha256": "a" * 64 if evidence else "",
        "evidence_key": evidence_key,
        "extraction_method": "test" if evidence else "",
        "status_reason": "test_candidate" if evidence else "",
        "evidence_text": "Backlog was $100 million." if evidence else "",
        "review_decision": "",
        "review_notes": "",
        "reviewed_by": "",
        "reviewed_at": "",
    }


def test_evidence_review_contract_includes_evidence_key() -> None:
    namespace = runpy.run_path(str(REVIEW_SCRIPT_PATH))
    assert "evidence_key" in namespace["OUTPUT_FIELDS"]


def test_pair_adjudication_main_builds_complete_blank_queue(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(SCRIPT_PATH))
    current_rows = [
        _comparison_row(
            "reported_backlog",
            recovery_class="RECOVERED_REPORTED",
            covered=1,
            value=100_000_000,
        ),
        _comparison_row(
            "orders",
            recovery_class="FOUND_AMBIGUOUS",
            covered=0,
        ),
        _comparison_row(
            "funded_backlog",
            recovery_class="NOT_FOUND_IN_SEARCHED_DOCUMENTS",
            covered=0,
        ),
        _comparison_row(
            "remaining_performance_obligation",
            recovery_class="CONFIRMED_REPORTED",
            covered=1,
            value=90_000_000,
        ),
        _comparison_row(
            "rpo_current",
            recovery_class="HISTORICAL_RECOVERY_ONLY",
            covered=0,
        ),
    ]
    prior_rows = [
        {
            **row,
            "shadow_predicted_status": "NOT_DISCLOSED",
            "shadow_value": "",
            "shadow_period_end": "",
            "shadow_covered_flag": 0,
        }
        for row in current_rows
    ]
    evidence_rows = [
        _evidence_row(
            "reported_backlog",
            recovery_class="RECOVERED_REPORTED",
            evidence_key="reported-key",
            value=100_000_000,
        ),
        _evidence_row(
            "orders",
            recovery_class="FOUND_AMBIGUOUS",
            candidate_status="REVIEW_REQUIRED",
            evidence_key="orders-key",
            value=50_000_000,
        ),
        _evidence_row(
            "funded_backlog",
            recovery_class="NOT_FOUND_IN_SEARCHED_DOCUMENTS",
        ),
        _evidence_row(
            "remaining_performance_obligation",
            recovery_class="CONFIRMED_REPORTED",
            evidence_key="rpo-key",
            value=90_000_000,
        ),
        _evidence_row(
            "rpo_current",
            recovery_class="HISTORICAL_RECOVERY_ONLY",
            evidence_key="rpo-current-key",
            value=40_000_000,
        ),
    ]
    comparison_csv = tmp_path / "current.csv"
    prior_csv = tmp_path / "prior.csv"
    evidence_csv = tmp_path / "evidence.csv"
    rank_csv = tmp_path / "rank.csv"
    _write_csv(comparison_csv, current_rows)
    _write_csv(prior_csv, prior_rows)
    _write_csv(evidence_csv, evidence_rows)
    rank_csv.write_text("ticker,final_score\nTEST,50\n", encoding="utf-8")

    current_summary = {
        "acceptance": "PASS",
        "asof_date": "2026-07-24",
        "run_id": 50,
        "shadow_only": True,
        "expected_comparison_rows": len(METRICS),
        "comparison_csv_sha256": file_sha256(comparison_csv),
        "production_rank_csv": str(rank_csv),
        "production_rank_sha256": file_sha256(rank_csv),
    }
    prior_summary = {
        "acceptance": "PASS",
        "asof_date": "2026-07-24",
        "run_id": 47,
        "comparison_csv_sha256": file_sha256(prior_csv),
        "production_rank_sha256": file_sha256(rank_csv),
    }
    evidence_summary = {
        "acceptance": "PASS",
        "asof_date": "2026-07-24",
        "run_id": 50,
        "hydration_status": "CACHE_COMPLETE",
        "remaining_source_gap_count": 0,
        "event_catalog_audit": {"status": "PASS"},
        "work_units": {"event_filing_work_items": 1},
        "review_row_count": len(evidence_rows),
        "output_csv_sha256": file_sha256(evidence_csv),
    }
    current_summary_path = tmp_path / "current.json"
    prior_summary_path = tmp_path / "prior.json"
    evidence_summary_path = tmp_path / "evidence.json"
    current_summary_path.write_text(
        json.dumps(current_summary),
        encoding="utf-8",
    )
    prior_summary_path.write_text(
        json.dumps(prior_summary),
        encoding="utf-8",
    )
    evidence_summary_path.write_text(
        json.dumps(evidence_summary),
        encoding="utf-8",
    )
    output_csv = tmp_path / "queue.csv"
    output_summary = tmp_path / "queue.json"

    result = namespace["main"](
        [
            "--asof",
            "2026-07-24",
            "--comparison-csv",
            str(comparison_csv),
            "--comparison-summary",
            str(current_summary_path),
            "--prior-comparison-csv",
            str(prior_csv),
            "--prior-comparison-summary",
            str(prior_summary_path),
            "--evidence-review-csv",
            str(evidence_csv),
            "--evidence-review-summary",
            str(evidence_summary_path),
            "--output-csv",
            str(output_csv),
            "--summary-json",
            str(output_summary),
        ]
    )

    assert result == 0
    with output_csv.open(
        "r",
        encoding="utf-8",
        newline="",
    ) as handle:
        queue = list(csv.DictReader(handle))
    summary = json.loads(output_summary.read_text(encoding="utf-8"))
    assert len(queue) == len(METRICS)
    assert summary["new_coverage_pair_count"] == 2
    assert summary["populated_review_decision_count"] == 0
    assert summary["populated_selected_evidence_key_count"] == 0
    reported = next(row for row in queue if row["metric_name"] == "reported_backlog")
    assert reported["review_tier"] == "1_new_coverage_validation"
    assert reported["representative_evidence_key"] == "reported-key"
    assert reported["review_decision"] == ""
    assert reported["selected_evidence_key"] == ""


def test_pair_adjudication_rejects_missing_evidence_key() -> None:
    namespace = runpy.run_path(str(SCRIPT_PATH))
    current = _comparison_row(
        "orders",
        recovery_class="FOUND_AMBIGUOUS",
        covered=0,
    )
    evidence = _evidence_row(
        "orders",
        recovery_class="FOUND_AMBIGUOUS",
        candidate_status="REVIEW_REQUIRED",
        evidence_key="missing-key-placeholder",
        value=1,
    )
    evidence["evidence_key"] = ""

    with pytest.raises(ValueError, match="missing evidence_key"):
        namespace["build_pair_queue"](
            run_id=50,
            prior_run_id=47,
            asof_date="2026-07-24",
            comparison_rows=[current],
            prior_rows=[current],
            evidence_rows=[evidence],
        )


def test_pair_adjudication_prioritizes_removed_coverage() -> None:
    namespace = runpy.run_path(str(SCRIPT_PATH))
    current = _comparison_row(
        "reported_backlog",
        recovery_class="FOUND_AMBIGUOUS",
        covered=0,
    )
    prior = {
        **current,
        "shadow_predicted_status": "REPORTED_SHADOW",
        "shadow_value": 685_000_000,
        "shadow_period_end": "2025-09-30",
        "shadow_covered_flag": 1,
    }
    evidence = _evidence_row(
        "reported_backlog",
        recovery_class="FOUND_AMBIGUOUS",
        candidate_status="REJECTED_POLICY",
        evidence_key="removed-key",
        value=685_000_000,
    )

    queue, summary = namespace["build_pair_queue"](
        run_id=51,
        prior_run_id=50,
        asof_date="2026-07-24",
        comparison_rows=[current],
        prior_rows=[prior],
        evidence_rows=[evidence],
    )

    assert queue[0]["review_tier"] == "1_removed_coverage_validation"
    assert queue[0]["removed_coverage_vs_prior_flag"] == 1
    assert summary["removed_coverage_pair_count"] == 1
    assert summary["removed_coverage_pairs"][0]["ticker"] == "TEST"


def test_source_validation_rejects_missing_gap_count(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(SCRIPT_PATH))
    source_csv = tmp_path / "source.csv"
    rank_csv = tmp_path / "rank.csv"
    source_csv.write_text("ticker\nTEST\n", encoding="utf-8")
    rank_csv.write_text("ticker,final_score\nTEST,50\n", encoding="utf-8")
    source_hash = file_sha256(source_csv)
    rank_hash = file_sha256(rank_csv)
    current_summary = {
        "acceptance": "PASS",
        "asof_date": "2026-07-24",
        "run_id": 50,
        "shadow_only": True,
        "comparison_csv_sha256": source_hash,
        "production_rank_csv": str(rank_csv),
        "production_rank_sha256": rank_hash,
    }
    prior_summary = {
        "acceptance": "PASS",
        "asof_date": "2026-07-24",
        "run_id": 47,
        "comparison_csv_sha256": source_hash,
        "production_rank_sha256": rank_hash,
    }
    evidence_summary = {
        "acceptance": "PASS",
        "asof_date": "2026-07-24",
        "run_id": 50,
        "hydration_status": "CACHE_COMPLETE",
        "event_catalog_audit": {"status": "PASS"},
        "work_units": {"event_filing_work_items": 1},
        "output_csv_sha256": source_hash,
    }

    with pytest.raises(
        ValueError,
        match="does not report remaining source gaps",
    ):
        namespace["_validate_source_artifacts"](
            asof_date="2026-07-24",
            run_id=50,
            prior_run_id=47,
            comparison_csv=source_csv,
            comparison_summary=current_summary,
            prior_comparison_csv=source_csv,
            prior_summary=prior_summary,
            evidence_csv=source_csv,
            evidence_summary=evidence_summary,
        )

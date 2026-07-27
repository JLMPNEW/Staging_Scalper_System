#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from industrials.core.reports import (  # noqa: E402
    write_csv_atomic,
    write_text_atomic,
)


PAIR_FIELDS = [
    "queue_rank",
    "review_tier",
    "manual_review_required_flag",
    "suggested_review_action",
    "allowed_review_decisions",
    "run_id",
    "prior_run_id",
    "asof_date",
    "ticker",
    "company_name",
    "cik",
    "calibration_cohort",
    "membership_status",
    "metric_name",
    "recovery_class",
    "baseline_status",
    "baseline_value",
    "baseline_covered_flag",
    "prior_shadow_predicted_status",
    "prior_shadow_value",
    "prior_shadow_period_end",
    "prior_shadow_covered_flag",
    "shadow_predicted_status",
    "shadow_value",
    "shadow_period_end",
    "shadow_covered_flag",
    "new_coverage_vs_prior_flag",
    "removed_coverage_vs_prior_flag",
    "current_match_mode",
    "current_evidence_period_end",
    "current_evidence_age_days",
    "accepted_current_count",
    "accepted_historical_count",
    "review_required_count",
    "rejected_count",
    "parser_failure_count",
    "searched_filing_count",
    "searched_document_count",
    "failed_filing_count",
    "missing_cache_filing_count",
    "evidence_row_count",
    "accepted_evidence_count",
    "review_required_evidence_count",
    "rejected_evidence_count",
    "distinct_candidate_value_count",
    "representative_evidence_key",
    "representative_candidate_status",
    "representative_candidate_value",
    "representative_unit",
    "representative_period_start",
    "representative_period_end",
    "representative_scope",
    "representative_confidence",
    "representative_concept_name",
    "representative_accession_number",
    "representative_form_type",
    "representative_filing_date",
    "representative_accepted_at",
    "representative_source_document",
    "representative_source_path",
    "representative_source_content_sha256",
    "representative_extraction_method",
    "representative_status_reason",
    "representative_evidence_text",
    "candidate_preview_json",
    "review_decision",
    "selected_evidence_key",
    "decision_reason",
    "review_notes",
    "reviewed_by",
    "reviewed_at",
]

MANUAL_REVIEW_CLASSES = frozenset(
    {
        "BASELINE_POLICY_CORRECTION",
        "BASELINE_REPORTED_HISTORICAL_ONLY",
        "BASELINE_REPORTED_UNCONFIRMED",
        "DISCLOSURE_REJECTED_POLICY",
        "FOUND_AMBIGUOUS",
        "HISTORICAL_RECOVERY_ONLY",
        "PARSER_FAILURE",
        "RECOVERED_REPORTED",
        "SOURCE_DOCUMENT_INCOMPLETE",
        "SOURCE_DOCUMENT_MISSING",
    }
)
KNOWN_NO_REVIEW_CLASSES = frozenset(
    {
        "CONFIRMED_REPORTED",
        "NOT_FOUND_IN_SEARCHED_DOCUMENTS",
    }
)
POLICY_REVIEW_CLASSES = frozenset(
    {
        "BASELINE_POLICY_CORRECTION",
        "DISCLOSURE_REJECTED_POLICY",
    }
)
METRIC_ORDER = {
    "reported_backlog": 0,
    "funded_backlog": 1,
    "remaining_performance_obligation": 2,
    "rpo_current": 3,
    "orders": 4,
}
TIER_ORDER = {
    "1_removed_coverage_validation": 0,
    "1_new_coverage_validation": 1,
    "1_baseline_unconfirmed": 2,
    "1_active_ambiguous": 3,
    "2_active_recovered_validation": 4,
    "2_active_policy_validation": 5,
    "3_active_historical_research": 6,
    "3_historical_research_validation": 7,
    "3_other_manual_review": 8,
    "4_confirmed_sample_check": 9,
    "5_no_evidence": 10,
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build one deterministic defense adjudication row per "
            "ticker/metric pair from the complete shadow evidence package."
        )
    )
    parser.add_argument("--asof", required=True)
    parser.add_argument("--run-id", type=int, default=0)
    parser.add_argument("--prior-run-id", type=int, default=0)
    parser.add_argument("--comparison-csv", type=Path, default=None)
    parser.add_argument("--comparison-summary", type=Path, default=None)
    parser.add_argument("--prior-comparison-csv", type=Path, default=None)
    parser.add_argument(
        "--prior-comparison-summary",
        type=Path,
        default=None,
    )
    parser.add_argument("--evidence-review-csv", type=Path, default=None)
    parser.add_argument("--evidence-review-summary", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--summary-json", type=Path, default=None)
    return parser.parse_args(argv)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return payload


def _load_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _require_hash(
    path: Path,
    expected: object,
    *,
    label: str,
) -> str:
    actual = file_sha256(path)
    expected_text = str(expected or "").strip().lower()
    if not expected_text:
        raise ValueError(f"{label} summary does not seal its source CSV")
    if actual.lower() != expected_text:
        raise ValueError(f"{label} hash mismatch: expected={expected_text} actual={actual}")
    return actual


def _as_int(value: object) -> int:
    try:
        return int(str(value or "0"))
    except ValueError:
        return 0


def _as_float(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _date_ordinal(value: object) -> int:
    raw = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(raw).toordinal()
    except ValueError:
        return 0


def _candidate_value_matches(
    evidence: dict[str, str],
    comparison: dict[str, str],
) -> bool:
    candidate = _as_float(evidence.get("candidate_value"))
    shadow = _as_float(comparison.get("shadow_value"))
    if candidate is None or shadow is None:
        return False
    return abs(candidate - shadow) <= max(
        1e-6,
        abs(shadow) * 1e-9,
    )


def _evidence_sort_key(
    evidence: dict[str, str],
    comparison: dict[str, str],
) -> tuple[object, ...]:
    status = str(evidence.get("candidate_status") or "")
    status_order = 0 if status == "ACCEPTED" else 1 if status == "REVIEW_REQUIRED" else 2
    current_period = str(comparison.get("current_evidence_period_end") or "")
    return (
        0 if current_period and str(evidence.get("period_end") or "") == current_period else 1,
        0 if _candidate_value_matches(evidence, comparison) else 1,
        status_order,
        -_date_ordinal(evidence.get("accepted_at")),
        -_date_ordinal(evidence.get("period_end")),
        -(_as_float(evidence.get("confidence")) or 0.0),
        str(evidence.get("accession_number") or ""),
        str(evidence.get("evidence_key") or ""),
    )


def _review_tier(
    *,
    membership_status: str,
    recovery_class: str,
    new_coverage: bool,
    removed_coverage: bool,
) -> str:
    active = membership_status == "active"
    if removed_coverage:
        return "1_removed_coverage_validation"
    if new_coverage:
        return "1_new_coverage_validation"
    if recovery_class == "BASELINE_REPORTED_UNCONFIRMED":
        return "1_baseline_unconfirmed"
    if active and recovery_class == "FOUND_AMBIGUOUS":
        return "1_active_ambiguous"
    if active and recovery_class == "RECOVERED_REPORTED":
        return "2_active_recovered_validation"
    if active and recovery_class in POLICY_REVIEW_CLASSES:
        return "2_active_policy_validation"
    if active and recovery_class in {
        "BASELINE_REPORTED_HISTORICAL_ONLY",
        "HISTORICAL_RECOVERY_ONLY",
    }:
        return "3_active_historical_research"
    if not active and recovery_class in MANUAL_REVIEW_CLASSES:
        return "3_historical_research_validation"
    if recovery_class in MANUAL_REVIEW_CLASSES:
        return "3_other_manual_review"
    if recovery_class == "CONFIRMED_REPORTED":
        return "4_confirmed_sample_check"
    return "5_no_evidence"


def _suggested_action(
    *,
    review_tier: str,
    recovery_class: str,
) -> str:
    if review_tier == "1_removed_coverage_validation":
        return "verify_policy_driven_coverage_removal_against_source_filing"
    if review_tier == "1_new_coverage_validation":
        return "verify_new_value_period_unit_scope_and_select_evidence"
    if recovery_class == "BASELINE_REPORTED_UNCONFIRMED":
        return "resolve_baseline_vs_shadow_disagreement"
    if recovery_class == "FOUND_AMBIGUOUS":
        return "select_valid_evidence_or_reject_all_candidates"
    if recovery_class == "RECOVERED_REPORTED":
        return "validate_recovered_value_against_source_filing"
    if recovery_class in POLICY_REVIEW_CLASSES:
        return "validate_policy_rejection_or_document_override"
    if recovery_class in {
        "BASELINE_REPORTED_HISTORICAL_ONLY",
        "HISTORICAL_RECOVERY_ONLY",
    }:
        return "validate_for_survivorship_corrected_research_only"
    if recovery_class == "CONFIRMED_REPORTED":
        return "sample_check_confirmed_mapping"
    if recovery_class == "NOT_FOUND_IN_SEARCHED_DOCUMENTS":
        return "no_action_unless_external_source_is_added"
    return "manual_classification_required"


def _candidate_preview(
    rows: list[dict[str, str]],
    comparison: dict[str, str],
) -> str:
    preview = []
    for row in sorted(
        rows,
        key=lambda item: _evidence_sort_key(item, comparison),
    )[:5]:
        preview.append(
            {
                "accession_number": row.get("accession_number", ""),
                "candidate_status": row.get("candidate_status", ""),
                "candidate_value": row.get("candidate_value", ""),
                "confidence": row.get("confidence", ""),
                "evidence_key": row.get("evidence_key", ""),
                "form_type": row.get("form_type", ""),
                "period_end": row.get("period_end", ""),
                "source_document": row.get("source_document", ""),
                "status_reason": row.get("status_reason", ""),
                "unit": row.get("unit", ""),
            }
        )
    return json.dumps(
        preview,
        sort_keys=True,
        separators=(",", ":"),
    )


def _pair_map(
    rows: list[dict[str, str]],
    *,
    label: str,
) -> dict[tuple[str, str], dict[str, str]]:
    output: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        key = (
            str(row.get("ticker") or "").strip().upper(),
            str(row.get("metric_name") or "").strip(),
        )
        if not all(key):
            raise ValueError(f"{label} contains an empty pair key: {row}")
        if key in output:
            raise ValueError(f"{label} contains duplicate pair: {key}")
        output[key] = row
    return output


def _validate_source_artifacts(
    *,
    asof_date: str,
    run_id: int,
    prior_run_id: int,
    comparison_csv: Path,
    comparison_summary: dict[str, Any],
    prior_comparison_csv: Path,
    prior_summary: dict[str, Any],
    evidence_csv: Path,
    evidence_summary: dict[str, Any],
) -> dict[str, str]:
    for label, summary, expected_run in (
        ("comparison", comparison_summary, run_id),
        ("prior comparison", prior_summary, prior_run_id),
        ("evidence review", evidence_summary, run_id),
    ):
        if summary.get("acceptance") != "PASS":
            raise ValueError(f"{label} acceptance is not PASS")
        if str(summary.get("asof_date") or "") != asof_date:
            raise ValueError(f"{label} asof_date mismatch")
        if int(summary.get("run_id") or 0) != expected_run:
            raise ValueError(f"{label} run_id mismatch")
    if comparison_summary.get("shadow_only") is not True:
        raise ValueError("Current comparison must remain shadow-only")
    if evidence_summary.get("hydration_status") != "CACHE_COMPLETE":
        raise ValueError("Evidence review does not use a complete cache")
    if "remaining_source_gap_count" not in evidence_summary:
        raise ValueError("Evidence review does not report remaining source gaps")
    if _as_int(evidence_summary["remaining_source_gap_count"]) != 0:
        raise ValueError("Evidence review contains remaining source gaps")
    event_audit = evidence_summary.get("event_catalog_audit") or {}
    if not isinstance(event_audit, dict) or event_audit.get("status") != "PASS":
        raise ValueError("Event filing catalog audit is not PASS")
    if (
        int(
            (evidence_summary.get("work_units") or {}).get(
                "event_filing_work_items",
                0,
            )
        )
        <= 0
    ):
        raise ValueError("Evidence review has no event filing work items")

    hashes = {
        "comparison_csv_sha256": _require_hash(
            comparison_csv,
            comparison_summary.get("comparison_csv_sha256"),
            label="comparison",
        ),
        "prior_comparison_csv_sha256": _require_hash(
            prior_comparison_csv,
            prior_summary.get("comparison_csv_sha256"),
            label="prior comparison",
        ),
        "evidence_review_csv_sha256": _require_hash(
            evidence_csv,
            evidence_summary.get("output_csv_sha256"),
            label="evidence review",
        ),
    }
    rank_path = Path(str(comparison_summary.get("production_rank_csv") or ""))
    if not rank_path.is_file():
        raise FileNotFoundError(rank_path)
    rank_hash = _require_hash(
        rank_path,
        comparison_summary.get("production_rank_sha256"),
        label="production rank",
    )
    if str(prior_summary.get("production_rank_sha256") or "").lower() != rank_hash.lower():
        raise ValueError("Production rank hash changed between prior and current shadow runs")
    hashes["production_rank_sha256"] = rank_hash
    hashes["production_rank_csv"] = str(rank_path)
    return hashes


def build_pair_queue(
    *,
    run_id: int,
    prior_run_id: int,
    asof_date: str,
    comparison_rows: list[dict[str, str]],
    prior_rows: list[dict[str, str]],
    evidence_rows: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    current_by_pair = _pair_map(
        comparison_rows,
        label="current comparison",
    )
    prior_by_pair = _pair_map(
        prior_rows,
        label="prior comparison",
    )
    if set(current_by_pair) != set(prior_by_pair):
        missing = sorted(set(current_by_pair) - set(prior_by_pair))
        unexpected = sorted(set(prior_by_pair) - set(current_by_pair))
        raise ValueError(f"Prior/current pair scope mismatch: missing={missing[:10]} unexpected={unexpected[:10]}")

    evidence_by_pair: dict[
        tuple[str, str],
        list[dict[str, str]],
    ] = {}
    evidence_keys: set[str] = set()
    for row in evidence_rows:
        key = (
            str(row.get("ticker") or "").strip().upper(),
            str(row.get("metric_name") or "").strip(),
        )
        if key not in current_by_pair:
            raise ValueError(f"Evidence contains unexpected pair: {key}")
        if any(
            str(row.get(field) or "").strip()
            for field in (
                "review_decision",
                "review_notes",
                "reviewed_by",
                "reviewed_at",
            )
        ):
            raise ValueError(
                "Evidence package already contains review decisions; "
                "pair queue generation must start from an unadjudicated run"
            )
        if str(row.get("record_type") or "") == "evidence":
            evidence_key = str(row.get("evidence_key") or "").strip()
            if not evidence_key:
                raise ValueError(f"Evidence row missing evidence_key: {key}")
            if evidence_key in evidence_keys:
                raise ValueError(f"Duplicate evidence_key in review package: {evidence_key}")
            evidence_keys.add(evidence_key)
        evidence_by_pair.setdefault(key, []).append(row)
    if set(evidence_by_pair) != set(current_by_pair):
        missing = sorted(set(current_by_pair) - set(evidence_by_pair))
        raise ValueError(f"Evidence review is missing ticker/metric pairs: {missing[:10]}")

    queue: list[dict[str, Any]] = []
    unknown_recovery_classes: set[str] = set()
    for key in sorted(current_by_pair):
        current = current_by_pair[key]
        prior = prior_by_pair[key]
        pair_evidence = [row for row in evidence_by_pair[key] if str(row.get("record_type") or "") == "evidence"]
        recovery_class = str(current.get("recovery_class") or "")
        if recovery_class not in MANUAL_REVIEW_CLASSES and recovery_class not in KNOWN_NO_REVIEW_CLASSES:
            unknown_recovery_classes.add(recovery_class)
        new_coverage = (
            _as_int(prior.get("shadow_covered_flag")) == 0 and _as_int(current.get("shadow_covered_flag")) == 1
        )
        removed_coverage = (
            _as_int(prior.get("shadow_covered_flag")) == 1 and _as_int(current.get("shadow_covered_flag")) == 0
        )
        membership_status = str(current.get("membership_status") or "")
        tier = _review_tier(
            membership_status=membership_status,
            recovery_class=recovery_class,
            new_coverage=new_coverage,
            removed_coverage=removed_coverage,
        )
        representative = (
            min(
                pair_evidence,
                key=lambda row: _evidence_sort_key(row, current),
            )
            if pair_evidence
            else {}
        )
        statuses = Counter(str(row.get("candidate_status") or "") for row in pair_evidence)
        distinct_values = {
            (
                str(row.get("candidate_value") or ""),
                str(row.get("unit") or ""),
                str(row.get("period_end") or ""),
            )
            for row in pair_evidence
            if str(row.get("candidate_value") or "").strip()
        }
        manual_review = (
            removed_coverage or recovery_class in MANUAL_REVIEW_CLASSES or recovery_class not in KNOWN_NO_REVIEW_CLASSES
        )
        queue.append(
            {
                "queue_rank": 0,
                "review_tier": tier,
                "manual_review_required_flag": int(manual_review),
                "suggested_review_action": _suggested_action(
                    review_tier=tier,
                    recovery_class=recovery_class,
                ),
                "allowed_review_decisions": (
                    "ACCEPT|REJECT|STRUCTURAL_NA|DEFER"
                    if pair_evidence
                    else "NO_ACTION|STRUCTURAL_NA|EXTERNAL_SOURCE_REQUIRED"
                ),
                "run_id": run_id,
                "prior_run_id": prior_run_id,
                "asof_date": asof_date,
                "ticker": key[0],
                "company_name": current.get("company_name", ""),
                "cik": current.get("cik", ""),
                "calibration_cohort": current.get(
                    "calibration_cohort",
                    "",
                ),
                "membership_status": membership_status,
                "metric_name": key[1],
                "recovery_class": recovery_class,
                "baseline_status": current.get("baseline_status", ""),
                "baseline_value": current.get("baseline_value", ""),
                "baseline_covered_flag": current.get(
                    "baseline_covered_flag",
                    "",
                ),
                "prior_shadow_predicted_status": prior.get(
                    "shadow_predicted_status",
                    "",
                ),
                "prior_shadow_value": prior.get("shadow_value", ""),
                "prior_shadow_period_end": prior.get(
                    "shadow_period_end",
                    "",
                ),
                "prior_shadow_covered_flag": prior.get(
                    "shadow_covered_flag",
                    "",
                ),
                "shadow_predicted_status": current.get(
                    "shadow_predicted_status",
                    "",
                ),
                "shadow_value": current.get("shadow_value", ""),
                "shadow_period_end": current.get(
                    "shadow_period_end",
                    "",
                ),
                "shadow_covered_flag": current.get(
                    "shadow_covered_flag",
                    "",
                ),
                "new_coverage_vs_prior_flag": int(new_coverage),
                "removed_coverage_vs_prior_flag": int(removed_coverage),
                "current_match_mode": current.get(
                    "current_match_mode",
                    "",
                ),
                "current_evidence_period_end": current.get(
                    "current_evidence_period_end",
                    "",
                ),
                "current_evidence_age_days": current.get(
                    "current_evidence_age_days",
                    "",
                ),
                "accepted_current_count": current.get(
                    "accepted_current_count",
                    "",
                ),
                "accepted_historical_count": current.get(
                    "accepted_historical_count",
                    "",
                ),
                "review_required_count": current.get(
                    "review_required_count",
                    "",
                ),
                "rejected_count": current.get("rejected_count", ""),
                "parser_failure_count": current.get(
                    "parser_failure_count",
                    "",
                ),
                "searched_filing_count": current.get(
                    "searched_filing_count",
                    "",
                ),
                "searched_document_count": current.get(
                    "searched_document_count",
                    "",
                ),
                "failed_filing_count": current.get(
                    "failed_filing_count",
                    "",
                ),
                "missing_cache_filing_count": current.get(
                    "missing_cache_filing_count",
                    "",
                ),
                "evidence_row_count": len(pair_evidence),
                "accepted_evidence_count": statuses["ACCEPTED"],
                "review_required_evidence_count": statuses["REVIEW_REQUIRED"],
                "rejected_evidence_count": sum(
                    count for status, count in statuses.items() if status.startswith(("REJECTED", "SUPPRESSED"))
                ),
                "distinct_candidate_value_count": len(distinct_values),
                "representative_evidence_key": representative.get(
                    "evidence_key",
                    "",
                ),
                "representative_candidate_status": representative.get(
                    "candidate_status",
                    "",
                ),
                "representative_candidate_value": representative.get(
                    "candidate_value",
                    "",
                ),
                "representative_unit": representative.get("unit", ""),
                "representative_period_start": representative.get(
                    "period_start",
                    "",
                ),
                "representative_period_end": representative.get(
                    "period_end",
                    "",
                ),
                "representative_scope": representative.get("scope", ""),
                "representative_confidence": representative.get(
                    "confidence",
                    "",
                ),
                "representative_concept_name": representative.get(
                    "concept_name",
                    "",
                ),
                "representative_accession_number": representative.get(
                    "accession_number",
                    "",
                ),
                "representative_form_type": representative.get(
                    "form_type",
                    "",
                ),
                "representative_filing_date": representative.get(
                    "filing_date",
                    "",
                ),
                "representative_accepted_at": representative.get(
                    "accepted_at",
                    "",
                ),
                "representative_source_document": representative.get(
                    "source_document",
                    "",
                ),
                "representative_source_path": representative.get(
                    "source_path",
                    "",
                ),
                "representative_source_content_sha256": representative.get(
                    "source_content_sha256",
                    "",
                ),
                "representative_extraction_method": representative.get(
                    "extraction_method",
                    "",
                ),
                "representative_status_reason": representative.get(
                    "status_reason",
                    "",
                ),
                "representative_evidence_text": representative.get(
                    "evidence_text",
                    "",
                ),
                "candidate_preview_json": _candidate_preview(
                    pair_evidence,
                    current,
                ),
                "review_decision": "",
                "selected_evidence_key": "",
                "decision_reason": "",
                "review_notes": "",
                "reviewed_by": "",
                "reviewed_at": "",
            }
        )
    if unknown_recovery_classes:
        raise ValueError(
            f"Unclassified recovery classes require an explicit review tier: {sorted(unknown_recovery_classes)}"
        )

    queue.sort(
        key=lambda row: (
            TIER_ORDER[str(row["review_tier"])],
            0 if row["membership_status"] == "active" else 1,
            METRIC_ORDER.get(str(row["metric_name"]), 99),
            str(row["ticker"]),
        )
    )
    for rank, row in enumerate(queue, start=1):
        row["queue_rank"] = rank

    summary = {
        "acceptance": "PASS",
        "asof_date": asof_date,
        "run_id": run_id,
        "prior_run_id": prior_run_id,
        "pair_row_count": len(queue),
        "unique_pair_count": len({(row["ticker"], row["metric_name"]) for row in queue}),
        "ticker_count": len({str(row["ticker"]) for row in queue}),
        "metric_count": len({str(row["metric_name"]) for row in queue}),
        "active_pair_count": sum(row["membership_status"] == "active" for row in queue),
        "historical_pair_count": sum(row["membership_status"] == "historical" for row in queue),
        "manual_review_pair_count": sum(int(row["manual_review_required_flag"]) for row in queue),
        "new_coverage_pair_count": sum(int(row["new_coverage_vs_prior_flag"]) for row in queue),
        "new_coverage_pairs": [
            {
                "ticker": row["ticker"],
                "metric_name": row["metric_name"],
                "shadow_value": row["shadow_value"],
                "shadow_period_end": row["shadow_period_end"],
            }
            for row in queue
            if int(row["new_coverage_vs_prior_flag"])
        ],
        "removed_coverage_pair_count": sum(int(row["removed_coverage_vs_prior_flag"]) for row in queue),
        "removed_coverage_pairs": [
            {
                "ticker": row["ticker"],
                "metric_name": row["metric_name"],
                "prior_shadow_value": row["prior_shadow_value"],
                "prior_shadow_period_end": row["prior_shadow_period_end"],
                "recovery_class": row["recovery_class"],
            }
            for row in queue
            if int(row["removed_coverage_vs_prior_flag"])
        ],
        "review_tier_counts": dict(
            sorted(
                Counter(str(row["review_tier"]) for row in queue).items(),
                key=lambda item: TIER_ORDER[item[0]],
            )
        ),
        "recovery_class_counts": dict(sorted(Counter(str(row["recovery_class"]) for row in queue).items())),
        "metric_pair_counts": dict(sorted(Counter(str(row["metric_name"]) for row in queue).items())),
        "populated_review_decision_count": sum(bool(str(row["review_decision"]).strip()) for row in queue),
        "populated_selected_evidence_key_count": sum(bool(str(row["selected_evidence_key"]).strip()) for row in queue),
        "shadow_only": True,
        "promotion_blockers": [
            "pair_level_manual_adjudication_not_completed",
            "reviewed_policy_and_golden_corpus_not_sealed",
            "specialized_metric_pit_oos_recalibration_not_completed",
        ],
    }
    return queue, summary


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_dir = PROJECT_ROOT / "output" / "industrials" / "defense" / "dedicated_parser" / args.asof
    comparison_csv = (
        (args.comparison_csv or output_dir / "defense_specialized_metrics_before_after.csv").expanduser().resolve()
    )
    comparison_summary_path = (
        (args.comparison_summary or output_dir / "defense_specialized_metrics_before_after_summary.json")
        .expanduser()
        .resolve()
    )
    prior_comparison_csv = (
        (args.prior_comparison_csv or output_dir / "defense_specialized_metrics_run47_before_after.csv")
        .expanduser()
        .resolve()
    )
    prior_summary_path = (
        (args.prior_comparison_summary or output_dir / "defense_specialized_metrics_run47_before_after_summary.json")
        .expanduser()
        .resolve()
    )
    evidence_csv = (
        (args.evidence_review_csv or output_dir / "defense_specialized_metric_evidence_review.csv")
        .expanduser()
        .resolve()
    )
    evidence_summary_path = (
        (args.evidence_review_summary or output_dir / "defense_specialized_metric_evidence_review_summary.json")
        .expanduser()
        .resolve()
    )
    output_csv = (
        (args.output_csv or output_dir / "defense_specialized_metric_pair_adjudication_queue.csv")
        .expanduser()
        .resolve()
    )
    summary_path = (
        (args.summary_json or output_dir / "defense_specialized_metric_pair_adjudication_summary.json")
        .expanduser()
        .resolve()
    )

    comparison_summary = _load_json(comparison_summary_path)
    prior_summary = _load_json(prior_summary_path)
    evidence_summary = _load_json(evidence_summary_path)
    run_id = args.run_id or int(comparison_summary.get("run_id") or 0)
    prior_run_id = args.prior_run_id or int(prior_summary.get("run_id") or 0)
    if run_id <= 0 or prior_run_id <= 0:
        raise ValueError("Current and prior run IDs must be positive")
    if prior_run_id >= run_id:
        raise ValueError("Prior run ID must precede the current run ID")

    source_hashes = _validate_source_artifacts(
        asof_date=args.asof,
        run_id=run_id,
        prior_run_id=prior_run_id,
        comparison_csv=comparison_csv,
        comparison_summary=comparison_summary,
        prior_comparison_csv=prior_comparison_csv,
        prior_summary=prior_summary,
        evidence_csv=evidence_csv,
        evidence_summary=evidence_summary,
    )
    comparison_rows = _load_csv(comparison_csv)
    prior_rows = _load_csv(prior_comparison_csv)
    evidence_rows = _load_csv(evidence_csv)
    expected_pairs = int(comparison_summary.get("expected_comparison_rows") or 0)
    if len(comparison_rows) != expected_pairs:
        raise ValueError(
            f"Current comparison row count mismatch: expected={expected_pairs} actual={len(comparison_rows)}"
        )
    if len(evidence_rows) != int(evidence_summary.get("review_row_count") or 0):
        raise ValueError("Evidence review row count does not match summary")

    queue, summary = build_pair_queue(
        run_id=run_id,
        prior_run_id=prior_run_id,
        asof_date=args.asof,
        comparison_rows=comparison_rows,
        prior_rows=prior_rows,
        evidence_rows=evidence_rows,
    )
    if len(queue) != expected_pairs:
        raise ValueError(f"Pair queue is incomplete: expected={expected_pairs} actual={len(queue)}")
    if summary["populated_review_decision_count"]:
        raise ValueError("Pair queue generation populated review decisions")
    if summary["populated_selected_evidence_key_count"]:
        raise ValueError("Pair queue generation populated selected evidence keys")

    write_csv_atomic(output_csv, PAIR_FIELDS, queue)
    summary.update(
        {
            "output_csv": str(output_csv),
            "output_csv_sha256": file_sha256(output_csv),
            "source_artifacts": {
                "comparison_csv": str(comparison_csv),
                "comparison_summary": str(comparison_summary_path),
                "prior_comparison_csv": str(prior_comparison_csv),
                "prior_comparison_summary": str(prior_summary_path),
                "evidence_review_csv": str(evidence_csv),
                "evidence_review_summary": str(evidence_summary_path),
                **source_hashes,
            },
            "complete_cache_required": True,
            "event_catalog_audit_required": True,
        }
    )
    write_text_atomic(
        summary_path,
        json.dumps(
            summary,
            indent=2,
            sort_keys=True,
            default=str,
        )
        + "\n",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

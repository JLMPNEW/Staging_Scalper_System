from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

from industrials.core.reports import write_csv_atomic, write_text_atomic


COMPONENT_FIELDS = [
    "market_trend_score",
    "quality_score",
    "growth_score",
    "valuation_score",
    "operating_efficiency_score",
    "capital_risk_score",
    "development_stage_risk_score",
    "positioning_score",
]

SCORING_FEATURE_FIELDS = [
    "asof_date",
    "ticker",
    "company_name",
    "sector",
    "industry",
    "industry_aggregate",
    "subsector",
    "calibration_cohort",
    "calibration_cohort_name",
    "calibration_use",
    "development_stage",
    "membership_source_id",
    "membership_basis",
    "membership_start_date",
    "membership_end_date",
    "membership_status",
    "membership_confidence",
    "market_feature_asof_date",
    "market_feature_source_id",
    "latest_bar_date",
    "market_data_quality",
    "avg_dollar_volume_60d",
    "financial_feature_asof_date",
    "financial_feature_source_id",
    "reporting_profile",
    "reporting_standard",
    "financial_confidence",
    "financial_data_quality_status",
    "financial_fallback_status",
    "metric_registry_version",
    "metric_values_json",
    "metric_status_json",
    "component_coverage_json",
    "applicable_metric_count",
    "observed_metric_count",
    "required_metric_count",
    "required_metric_observed_count",
    "specialized_metric_count",
    "specialized_metric_observed_count",
    "specialized_coverage",
    "rank_ready_policy",
    "minimum_financial_confidence",
    "policy_valid_from",
    "policy_gate_status",
    *COMPONENT_FIELDS,
    "score_input_available_count",
    "score_input_total_count",
    "score_confidence",
    "final_score",
    "rank_ready_flag",
    "rank_ready_reason",
    "model_status",
]

FINAL_RANK_FIELDS = [
    *SCORING_FEATURE_FIELDS,
    "final_rank",
    "cohort_rank",
    "score_model_version",
    "model_version",
    "scoring_contract_version",
    "portfolio_candidate_gate",
    "portfolio_candidate_score",
    "portfolio_candidate_status",
    "portfolio_candidate_reason",
    "calibration_eligible_flag",
    "research_calibration_input_eligible_flag",
    "research_calibration_reason",
    "calibration_sample_role",
    "stage11_calibration_panel_source",
    "stage11_calibration_input_eligible_flag",
    "stage11_calibration_input_reason",
    "survivorship_corrected_panel_flag",
    "oos_score_valid_flag",
    "oos_score_asof_date",
    "oos_invalid_reason",
    "calibration_lock_date",
]


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        return [
            {str(key): str(value or "") for key, value in row.items() if key is not None}
            for row in reader
        ]


def _float(value: object) -> float | None:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def validate_scoring_rows(rows: list[dict[str, str]], *, asof: str) -> list[str]:
    if not rows:
        return ["scoring feature contract is empty"]
    errors: list[str] = []
    missing = sorted(set(SCORING_FEATURE_FIELDS) - set(rows[0]))
    if missing:
        errors.append(f"missing scoring columns={missing}")
    tickers = [str(row.get("ticker") or "").strip().upper() for row in rows]
    if not all(tickers):
        errors.append("blank ticker")
    if len(set(tickers)) != len(tickers):
        errors.append("duplicate ticker")
    if {str(row.get("asof_date") or "") for row in rows} != {asof}:
        errors.append("scoring rows must contain exactly the requested asof_date")
    for row in rows:
        ticker = str(row.get("ticker") or "<blank>")
        score = _float(row.get("final_score"))
        confidence = _float(row.get("score_confidence"))
        if score is None or not 0.0 <= score <= 100.0:
            errors.append(f"{ticker}: invalid final_score={row.get('final_score')!r}")
        if confidence is None or not 0.0 <= confidence <= 1.0:
            errors.append(f"{ticker}: invalid score_confidence={row.get('score_confidence')!r}")
        for field in COMPONENT_FIELDS:
            value = str(row.get(field) or "").strip()
            if value and (_float(value) is None or not 0.0 <= float(value) <= 100.0):
                errors.append(f"{ticker}: invalid {field}={value!r}")
        integer_fields = [
            "applicable_metric_count",
            "observed_metric_count",
            "required_metric_count",
            "required_metric_observed_count",
            "specialized_metric_count",
            "specialized_metric_observed_count",
            "score_input_available_count",
            "score_input_total_count",
        ]
        parsed_counts: dict[str, int] = {}
        for field in integer_fields:
            try:
                parsed_counts[field] = int(str(row.get(field) or ""))
            except ValueError:
                errors.append(f"{ticker}: invalid integer {field}={row.get(field)!r}")
        if parsed_counts:
            if parsed_counts.get("observed_metric_count", 0) > parsed_counts.get("applicable_metric_count", 0):
                errors.append(f"{ticker}: observed metrics exceed applicable metrics")
            if parsed_counts.get("required_metric_observed_count", 0) > parsed_counts.get("required_metric_count", 0):
                errors.append(f"{ticker}: observed required metrics exceed required metrics")
        for field in ("metric_values_json", "metric_status_json", "component_coverage_json"):
            try:
                decoded = json.loads(str(row.get(field) or "{}"))
                if not isinstance(decoded, dict):
                    raise TypeError
            except (json.JSONDecodeError, TypeError):
                errors.append(f"{ticker}: {field} must be a JSON object")
        rank_ready = str(row.get("rank_ready_flag") or "")
        reason = str(row.get("rank_ready_reason") or "")
        status = str(row.get("model_status") or "")
        if rank_ready not in {"0", "1"}:
            errors.append(f"{ticker}: rank_ready_flag must be 0 or 1")
        elif rank_ready == "1" and (reason != "ok" or status != "complete"):
            errors.append(f"{ticker}: rank-ready row must be complete with reason=ok")
        elif rank_ready == "0" and (not reason or status != "incomplete"):
            errors.append(f"{ticker}: blocked row requires reason and incomplete status")
        if str(row.get("policy_gate_status") or "") not in {"pass", "blocked"}:
            errors.append(f"{ticker}: invalid policy_gate_status")
    return errors


def validate_rank_rows(rows: list[dict[str, str]], *, asof: str) -> list[str]:
    errors = validate_scoring_rows(rows, asof=asof)
    if not rows:
        return errors
    missing = sorted(set(FINAL_RANK_FIELDS) - set(rows[0]))
    if missing:
        errors.append(f"missing final-rank columns={missing}")
    ranks: list[int] = []
    cohort_ranks: dict[str, list[int]] = {}
    for row in rows:
        ticker = str(row.get("ticker") or "<blank>")
        try:
            ranks.append(int(str(row.get("final_rank") or "")))
            cohort_ranks.setdefault(str(row.get("calibration_cohort") or ""), []).append(
                int(str(row.get("cohort_rank") or ""))
            )
        except ValueError:
            errors.append(f"{ticker}: invalid global/cohort rank")
        oos_valid = str(row.get("oos_score_valid_flag") or "")
        if oos_valid == "0":
            if str(row.get("portfolio_candidate_gate") or "") != "0":
                errors.append(f"{ticker}: shadow candidate gate must be 0")
            if str(row.get("portfolio_candidate_status") or "") != "shadow_only":
                errors.append(f"{ticker}: shadow status must be shadow_only")
        elif oos_valid == "1":
            ready = str(row.get("rank_ready_flag") or "") == "1"
            expected = ("1", "eligible") if ready else ("0", "not_eligible")
            if (
                str(row.get("portfolio_candidate_gate") or "") != expected[0]
                or str(row.get("portfolio_candidate_status") or "") != expected[1]
            ):
                errors.append(f"{ticker}: invalid locked-production candidate state")
            if not row.get("oos_score_asof_date") or not row.get("calibration_lock_date"):
                errors.append(f"{ticker}: locked production row lacks OOS/lock dates")
        else:
            errors.append(f"{ticker}: oos_score_valid_flag must be 0 or 1")
        if str(row.get("survivorship_corrected_panel_flag") or "") != "0":
            errors.append(f"{ticker}: current dashboard cannot claim survivorship correction")
    if sorted(ranks) != list(range(1, len(rows) + 1)):
        errors.append("final_rank must be contiguous")
    for cohort, values in cohort_ranks.items():
        if sorted(values) != list(range(1, len(values) + 1)):
            errors.append(f"{cohort}: cohort_rank must be contiguous")
    return errors


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_scoring_rows(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    write_csv_atomic(path, SCORING_FEATURE_FIELDS, rows)


def write_rank_rows(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    write_csv_atomic(path, FINAL_RANK_FIELDS, rows)


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")

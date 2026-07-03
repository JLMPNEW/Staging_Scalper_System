#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.point_in_time import (  # noqa: E402
    REVIEW_DATE_COLUMNS,
    START_DATE_COLUMNS,
    PIT_METADATA_COLUMNS,
    parse_iso_date,
    pit_date_parse_errors,
    row_has_pit_metadata,
    row_is_effective_asof,
    row_value,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CORE_REQUIRED_DAILY_COLUMNS = {
    "asof_date",
    "ticker",
    "composite_score",
    "calibration_cohort",
    "calibration_eligible_flag",
    "portfolio_candidate_gate",
    "analyst_review_decision",
    "analyst_reviewed_at",
    "analyst_review_expires_at",
}
POST_MIGRATION_DAILY_COLUMNS = {
    "score_confidence",
    "eligibility_reason",
    "native_score_field",
    "native_score_value",
    "score_zero_is_missing_flag",
    "universe_status",
    "historical_universe_source",
    "price_start_date",
    "price_end_date",
    "terminal_date",
    "historical_price_ticker",
    "calibration_only",
    "latest_price_date",
    "source_snapshot_asof_date",
    "price_data_asof_date",
    "feature_data_asof_date",
    "score_scale_min",
    "score_scale_max",
    "score_neutral_value",
    "oos_score_valid_flag",
    "research_calibration_input_eligible_flag",
    "research_calibration_status",
    "research_calibration_reason",
    "calibration_sample_role",
    "stage11_calibration_input_eligible_flag",
    "stage11_calibration_input_reason",
    "stage11_calibration_panel_source",
    "survivorship_corrected_panel_flag",
    "recovery_type",
    "equity_recovery",
    "drop_otc_tape",
    "financial_data_asof_date",
    "short_interest_asof_date",
    "institutional_data_asof_date",
    "insider_data_asof_date",
    "borrow_data_asof_date",
    "forward_catalyst_event_date",
    "forward_catalyst_event_type",
    "forward_catalyst_nearest_days",
    "forward_catalyst_source",
    "forward_catalyst_confidence",
    "forward_catalyst_asof_date",
    "avg_dollar_volume_60d",
    "avg_dollar_volume_60d_available_flag",
}
REQUIRED_DAILY_COLUMNS = CORE_REQUIRED_DAILY_COLUMNS | POST_MIGRATION_DAILY_COLUMNS
RESEARCH_ELIGIBLE_SAMPLE_ROLES = {"research_calibration_input", "strict_oos"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate med-devices dated snapshots for point-in-time/OOS readiness.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", type=str, default="")
    parser.add_argument("--start-asof", type=str, default="")
    parser.add_argument("--end-asof", type=str, default="")
    parser.add_argument("--reports-root", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--diagnostic-output-csv", type=Path, default=None)
    parser.add_argument(
        "--new-daily-columns-required-from",
        default="",
        help=(
            "Promote missing post-migration daily columns to CRITICAL for snapshots on/after this as-of date. "
            "Before this date, missing post-migration columns are WARNINGs so legacy snapshots do not block calibration."
        ),
    )
    parser.add_argument(
        "--allow-missing-static-pit-metadata",
        action="store_true",
        help=(
            "Allow the command to exit successfully when the only strict failures are missing PIT metadata "
            "in static source CSVs. The strict output CSV remains strict; the diagnostic CSV marks those rows PASS."
        ),
    )
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    return parse_iso_date(raw)


def parse_float(raw: object) -> float | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = [str(field) for field in (reader.fieldnames or [])]
        return fieldnames, [{str(key): str(value or "") for key, value in row.items()} for row in reader]


def add_check(
    checks: list[dict[str, Any]],
    *,
    asof: str,
    artifact: str,
    check_id: str,
    severity: str,
    passed: bool,
    observed: object,
    expected: object,
    details: str,
) -> None:
    checks.append(
        {
            "asof_date": asof,
            "artifact": artifact,
            "check_id": check_id,
            "severity": severity,
            "status": "PASS" if passed else "FAIL",
            "observed": observed,
            "expected": expected,
            "details": details,
        }
    )


def discover_asofs(root: Path, *, explicit: str, start: str, end: str) -> list[str]:
    if explicit.strip():
        dates = [item.strip() for item in explicit.split(",") if item.strip()]
    else:
        dates = [path.name for path in root.iterdir() if path.is_dir() and DATE_RE.match(path.name)] if root.exists() else []
    start_date = parse_date(start)
    end_date = parse_date(end)
    out: list[str] = []
    for item in dates:
        parsed = parse_date(item)
        if parsed is None:
            continue
        if start_date is not None and parsed < start_date:
            continue
        if end_date is not None and parsed > end_date:
            continue
        out.append(parsed.isoformat())
    return sorted(set(out))


def default_diagnostic_path(output_csv: Path) -> Path:
    if output_csv.name.endswith("_diagnostic.csv"):
        return output_csv
    return output_csv.with_name(f"{output_csv.stem}_diagnostic{output_csv.suffix}")


def validate_daily_csv(
    path: Path,
    *,
    asof: str,
    checks: list[dict[str, Any]],
    new_daily_columns_required_from: date | None,
) -> None:
    artifact = str(path)
    if not path.exists():
        add_check(
            checks,
            asof=asof,
            artifact=artifact,
            check_id="daily_csv_exists",
            severity="CRITICAL",
            passed=False,
            observed="missing",
            expected="exists",
            details="Dated daily composite CSV is required for OOS snapshot validation.",
        )
        return
    fields, rows = read_csv_rows(path)
    field_set = set(fields)
    missing = sorted(CORE_REQUIRED_DAILY_COLUMNS - field_set)
    add_check(
        checks,
        asof=asof,
        artifact=artifact,
        check_id="daily_required_columns",
        severity="CRITICAL",
        passed=not missing,
        observed=",".join(missing),
        expected="all core required columns",
        details="Dated daily CSV must include core portfolio and analyst-review audit fields.",
    )
    post_migration_missing = sorted(POST_MIGRATION_DAILY_COLUMNS - field_set)
    snapshot_date = parse_date(asof)
    enforce_post_migration = (
        new_daily_columns_required_from is not None
        and snapshot_date is not None
        and snapshot_date >= new_daily_columns_required_from
    )
    add_check(
        checks,
        asof=asof,
        artifact=artifact,
        check_id="daily_post_migration_columns",
        severity="CRITICAL" if enforce_post_migration else "WARNING",
        passed=not post_migration_missing,
        observed=",".join(post_migration_missing),
        expected="all post-migration provenance and liquidity columns",
        details=(
            "Post-migration columns are required only on/after "
            f"{new_daily_columns_required_from.isoformat() if new_daily_columns_required_from else 'an unset cutover date'}; "
            "before that cutover, missing columns are diagnostic warnings for legacy snapshots."
        ),
    )
    post_migration_severity = "CRITICAL" if enforce_post_migration else "WARNING"
    research_fields = {
        "score_scale_min",
        "score_scale_max",
        "score_neutral_value",
        "oos_score_valid_flag",
        "research_calibration_input_eligible_flag",
        "research_calibration_status",
        "research_calibration_reason",
        "calibration_sample_role",
        "stage11_calibration_input_eligible_flag",
        "stage11_calibration_input_reason",
        "stage11_calibration_panel_source",
        "survivorship_corrected_panel_flag",
        "native_score_value",
        "composite_score",
    }
    if not post_migration_missing and research_fields <= field_set:
        invalid_scale_rows = sum(
            1
            for row in rows
            if parse_float(row.get("score_scale_min")) != 0.0
            or parse_float(row.get("score_scale_max")) != 100.0
            or parse_float(row.get("score_neutral_value")) != 50.0
        )
        add_check(
            checks,
            asof=asof,
            artifact=artifact,
            check_id="daily_research_score_scale_values",
            severity=post_migration_severity,
            passed=invalid_scale_rows == 0,
            observed=invalid_scale_rows,
            expected=0,
            details="Research calibration score scale must be explicit 0..100 with neutral value 50.",
        )
        invalid_flag_rows = sum(
            1
            for row in rows
            if str(row.get("research_calibration_input_eligible_flag") or "").strip() not in {"0", "1"}
        )
        invalid_oos_flag_rows = sum(
            1
            for row in rows
            if str(row.get("oos_score_valid_flag") or "").strip() not in {"0", "1"}
        )
        unexpected_historical_oos_rows = sum(
            1
            for row in rows
            if str(row.get("oos_score_valid_flag") or "").strip() == "1"
            and str(row.get("calibration_sample_role") or "").strip() != "strict_oos"
        )
        add_check(
            checks,
            asof=asof,
            artifact=artifact,
            check_id="daily_research_eligible_flag_values",
            severity=post_migration_severity,
            passed=invalid_flag_rows == 0,
            observed=invalid_flag_rows,
            expected=0,
            details="Research calibration eligibility flag must be numeric 0 or 1.",
        )
        add_check(
            checks,
            asof=asof,
            artifact=artifact,
            check_id="daily_oos_score_valid_flag_values",
            severity=post_migration_severity,
            passed=invalid_oos_flag_rows == 0,
            observed=invalid_oos_flag_rows,
            expected=0,
            details="OOS score-valid flag must be numeric 0 or 1.",
        )
        add_check(
            checks,
            asof=asof,
            artifact=artifact,
            check_id="daily_oos_score_valid_role_consistency",
            severity=post_migration_severity,
            passed=unexpected_historical_oos_rows == 0,
            observed=unexpected_historical_oos_rows,
            expected=0,
            details="Rows marked OOS score-valid must carry strict_oos calibration sample role.",
        )
        invalid_status_rows = sum(
            1
            for row in rows
            if str(row.get("research_calibration_status") or "").strip() not in {"eligible", "excluded"}
        )
        add_check(
            checks,
            asof=asof,
            artifact=artifact,
            check_id="daily_research_status_values",
            severity=post_migration_severity,
            passed=invalid_status_rows == 0,
            observed=invalid_status_rows,
            expected=0,
            details="Research calibration status must be eligible or excluded.",
        )
        inconsistent_research_rows = 0
        inconsistent_stage11_rows = 0
        invalid_stage11_flag_rows = 0
        invalid_panel_source_rows = 0
        invalid_survivorship_flag_rows = 0
        zero_score_eligible_rows = 0
        missing_score_eligible_rows = 0
        missing_exclusion_reason_rows = 0
        wrong_eligible_reason_rows = 0
        for row in rows:
            flag = str(row.get("research_calibration_input_eligible_flag") or "").strip()
            status = str(row.get("research_calibration_status") or "").strip()
            role = str(row.get("calibration_sample_role") or "").strip()
            reason = str(row.get("research_calibration_reason") or "").strip()
            stage11_flag = str(row.get("stage11_calibration_input_eligible_flag") or "").strip()
            stage11_reason = str(row.get("stage11_calibration_input_reason") or "").strip()
            panel_source = str(row.get("stage11_calibration_panel_source") or "").strip()
            survivorship_flag = str(row.get("survivorship_corrected_panel_flag") or "").strip()
            native_score = parse_float(row.get("native_score_value"))
            composite_score = parse_float(row.get("composite_score"))
            if stage11_flag not in {"0", "1"}:
                invalid_stage11_flag_rows += 1
            if panel_source != "med_devices_survivorship_corrected_score_review_pack":
                invalid_panel_source_rows += 1
            if stage11_flag == "1" and survivorship_flag != "1":
                invalid_survivorship_flag_rows += 1
            if flag == "1":
                if status != "eligible" or role not in RESEARCH_ELIGIBLE_SAMPLE_ROLES:
                    inconsistent_research_rows += 1
                if stage11_flag != "1" or stage11_reason != "ok":
                    inconsistent_stage11_rows += 1
                if reason != "valid_research_calibration_input":
                    wrong_eligible_reason_rows += 1
                if native_score is None or composite_score is None:
                    missing_score_eligible_rows += 1
                elif native_score <= 0.0 or composite_score <= 0.0:
                    zero_score_eligible_rows += 1
            elif flag == "0":
                if status != "excluded" or role != "excluded_from_research_calibration":
                    inconsistent_research_rows += 1
                if stage11_flag != "0" or not stage11_reason or stage11_reason != reason:
                    inconsistent_stage11_rows += 1
                if not reason:
                    missing_exclusion_reason_rows += 1
            else:
                inconsistent_research_rows += 1
                inconsistent_stage11_rows += 1
        add_check(
            checks,
            asof=asof,
            artifact=artifact,
            check_id="daily_research_flag_status_role_consistency",
            severity=post_migration_severity,
            passed=inconsistent_research_rows == 0,
            observed=inconsistent_research_rows,
            expected=0,
            details="Research eligibility flag, status, and sample role must agree.",
        )
        add_check(
            checks,
            asof=asof,
            artifact=artifact,
            check_id="daily_stage11_eligible_flag_values",
            severity=post_migration_severity,
            passed=invalid_stage11_flag_rows == 0,
            observed=invalid_stage11_flag_rows,
            expected=0,
            details="Stage 11 calibration eligibility flag must be numeric 0 or 1.",
        )
        add_check(
            checks,
            asof=asof,
            artifact=artifact,
            check_id="daily_stage11_panel_source_values",
            severity=post_migration_severity,
            passed=invalid_panel_source_rows == 0,
            observed=invalid_panel_source_rows,
            expected=0,
            details="Stage 11 panel source must identify the med-device survivorship-corrected score review pack.",
        )
        add_check(
            checks,
            asof=asof,
            artifact=artifact,
            check_id="daily_stage11_survivorship_flag_values",
            severity=post_migration_severity,
            passed=invalid_survivorship_flag_rows == 0,
            observed=invalid_survivorship_flag_rows,
            expected=0,
            details="Stage 11 snapshots must explicitly certify survivorship-corrected panel membership.",
        )
        add_check(
            checks,
            asof=asof,
            artifact=artifact,
            check_id="daily_stage11_research_consistency",
            severity=post_migration_severity,
            passed=inconsistent_stage11_rows == 0,
            observed=inconsistent_stage11_rows,
            expected=0,
            details=(
                "Stage 11 eligibility must mirror research calibration eligibility; eligible rows use reason 'ok', "
                "excluded rows reuse the research exclusion reason."
            ),
        )
        add_check(
            checks,
            asof=asof,
            artifact=artifact,
            check_id="daily_no_zero_score_research_inputs",
            severity=post_migration_severity,
            passed=zero_score_eligible_rows == 0,
            observed=zero_score_eligible_rows,
            expected=0,
            details="Rows with zero or negative native/composite score cannot be Stage 11 research inputs.",
        )
        add_check(
            checks,
            asof=asof,
            artifact=artifact,
            check_id="daily_no_missing_score_research_inputs",
            severity=post_migration_severity,
            passed=missing_score_eligible_rows == 0,
            observed=missing_score_eligible_rows,
            expected=0,
            details="Rows with missing or non-finite native/composite score cannot be Stage 11 research inputs.",
        )
        add_check(
            checks,
            asof=asof,
            artifact=artifact,
            check_id="daily_research_exclusions_have_reason",
            severity=post_migration_severity,
            passed=missing_exclusion_reason_rows == 0,
            observed=missing_exclusion_reason_rows,
            expected=0,
            details="Excluded research calibration rows must publish an exclusion reason.",
        )
        add_check(
            checks,
            asof=asof,
            artifact=artifact,
            check_id="daily_research_eligible_reason_value",
            severity=post_migration_severity,
            passed=wrong_eligible_reason_rows == 0,
            observed=wrong_eligible_reason_rows,
            expected=0,
            details="Eligible research calibration rows must carry reason 'valid_research_calibration_input'.",
        )
    else:
        add_check(
            checks,
            asof=asof,
            artifact=artifact,
            check_id="daily_research_metadata_checks_executed",
            severity=post_migration_severity,
            passed=False,
            observed="missing_fields=" + ",".join(sorted(research_fields - field_set)),
            expected="all research calibration fields present",
            details="Research and Stage 11 metadata checks must execute for post-migration dated snapshots.",
        )
    wrong_asof = sum(1 for row in rows if str(row.get("asof_date") or "") != asof)
    add_check(
        checks,
        asof=asof,
        artifact=artifact,
        check_id="daily_asof_consistency",
        severity="CRITICAL",
        passed=wrong_asof == 0,
        observed=wrong_asof,
        expected=0,
        details="Every row in the dated CSV must match the folder as-of date.",
    )
    target = parse_iso_date(asof)
    future_decisions = sum(
        1
        for row in rows
        if str(row.get("analyst_review_decision") or "").strip()
        and (reviewed_at := parse_iso_date(row.get("analyst_reviewed_at")))
        and target is not None
        and reviewed_at >= target
    )
    invalid_review_dates = sum(
        1
        for row in rows
        if str(row.get("analyst_review_decision") or "").strip()
        and str(row.get("analyst_reviewed_at") or "").strip()
        and parse_iso_date(row.get("analyst_reviewed_at")) is None
    )
    missing_review_dates = sum(
        1
        for row in rows
        if str(row.get("analyst_review_decision") or "").strip()
        and not str(row.get("analyst_reviewed_at") or "").strip()
    )
    expired_decisions = sum(
        1
        for row in rows
        if str(row.get("analyst_review_decision") or "").strip()
        and (expires_at := parse_iso_date(row.get("analyst_review_expires_at")))
        and target is not None
        and expires_at < target
    )
    add_check(
        checks,
        asof=asof,
        artifact=artifact,
        check_id="no_future_analyst_decisions",
        severity="CRITICAL",
        passed=future_decisions == 0,
        observed=future_decisions,
        expected=0,
        details="Analyst decisions cannot be applied before their reviewed_at date.",
    )
    add_check(
        checks,
        asof=asof,
        artifact=artifact,
        check_id="analyst_review_dates_parse",
        severity="CRITICAL",
        passed=invalid_review_dates == 0 and missing_review_dates == 0,
        observed=f"invalid={invalid_review_dates} missing={missing_review_dates}",
        expected=0,
        details="Applied analyst decisions must use nonblank, parseable YYYY-MM-DD reviewed_at values.",
    )
    add_check(
        checks,
        asof=asof,
        artifact=artifact,
        check_id="no_expired_analyst_decisions",
        severity="CRITICAL",
        passed=expired_decisions == 0,
        observed=expired_decisions,
        expected=0,
        details="Expired analyst decisions cannot be applied to dated snapshots.",
    )


def static_source_paths(config: dict[str, Any], *, base_dir: Path) -> list[tuple[str, Path]]:
    specs = [
        ("taxonomy_override", "calibration.taxonomy_override_csv"),
        ("fda_entity_manual_overrides", "fda_entity_linking.manual_overrides_csv"),
        ("fda_company_aliases", "fda_entity_linking.extra_alias_csv"),
        ("fda_entity_product_line_overrides", "fda_entity_linking.product_line_overrides_csv"),
        ("fda_review_overrides", "fda_features.review_override_csv"),
        ("fda_footprints", "fda_features.footprint_csv"),
        ("fda_manual_footprint_evidence", "fda_features.manual_footprint_evidence_csv"),
        ("reimbursement_company_classifications", "reimbursement_features.company_classification_csv"),
        ("reimbursement_mapping_overrides", "reimbursement_entity_linking.override_csv"),
        ("reimbursement_resolved_classifications", "reimbursement_entity_linking.resolved_classification_csv"),
        ("reimbursement_manual_payment_rates", "reimbursement_entity_linking.manual_rate_csv"),
        ("analyst_review_decisions", "med_devices_analyst_review.decisions_csv"),
        ("historical_membership", "med_devices_universe.historical_membership_csv"),
        ("share_count_overrides", "financial_features.share_count_override_csv"),
    ]
    by_path: dict[Path, list[str]] = {}
    order: list[Path] = []
    for label, key in specs:
        raw = str(cfg_get(config, key, "") or "").strip()
        if not raw:
            continue
        path = resolve_path(raw, base_dir=base_dir)
        if path not in by_path:
            by_path[path] = []
            order.append(path)
        by_path[path].append(label)
    return [("+".join(by_path[path]), path) for path in order]


def validate_static_sources(
    paths: list[tuple[str, Path]],
    *,
    asof: str,
    start_asof: str,
    checks: list[dict[str, Any]],
) -> None:
    start_date = parse_date(start_asof)
    for label, path in paths:
        artifact = f"{label}:{path}"
        if not path.exists():
            add_check(
                checks,
                asof=asof,
                artifact=artifact,
                check_id="static_source_exists",
                severity="CRITICAL",
                passed=False,
                observed="missing",
                expected="exists",
                details="Configured static source CSV must exist.",
            )
            continue
        fields, rows = read_csv_rows(path)
        missing_metadata = sum(1 for row in rows if not row_has_pit_metadata(row))
        invalid_metadata_dates = sum(1 for row in rows if pit_date_parse_errors(row))
        valid_metadata_rows = [row for row in rows if row_has_pit_metadata(row) and not pit_date_parse_errors(row)]
        rows_backdated_to_start = 0
        if start_date is not None:
            rows_backdated_to_start = sum(
                1
                for row in valid_metadata_rows
                if parse_date(row_value(row, *START_DATE_COLUMNS)) == start_date
                and (reviewed_at := parse_date(row_value(row, *REVIEW_DATE_COLUMNS))) is not None
                and reviewed_at < start_date
            )
        backdated_share = rows_backdated_to_start / len(rows) if rows else 0.0
        not_effective_asof = sum(
            1
            for row in rows
            if row_has_pit_metadata(row)
            and not pit_date_parse_errors(row)
            and not row_is_effective_asof(row, asof, include_missing=False)
        )
        has_metadata_column = any(field.strip().lower() in PIT_METADATA_COLUMNS for field in fields)
        add_check(
            checks,
            asof=asof,
            artifact=artifact,
            check_id="static_source_has_pit_metadata_columns",
            severity="CRITICAL",
            passed=has_metadata_column,
            observed=int(has_metadata_column),
            expected=1,
            details="Static source CSV headers must include PIT metadata columns, even when the file has zero rows.",
        )
        add_check(
            checks,
            asof=asof,
            artifact=artifact,
            check_id="static_source_rows_have_pit_metadata_values",
            severity="CRITICAL",
            passed=missing_metadata == 0,
            observed=missing_metadata,
            expected=0,
            details=(
                "Every static override row needs valid_from/effective_date/reviewed_at/valid_to metadata "
                "for strict OOS calibration."
            ),
        )
        add_check(
            checks,
            asof=asof,
            artifact=artifact,
            check_id="static_source_pit_dates_parse",
            severity="CRITICAL",
            passed=invalid_metadata_dates == 0,
            observed=invalid_metadata_dates,
            expected=0,
            details="Static override PIT metadata dates must be parseable YYYY-MM-DD values.",
        )
        add_check(
            checks,
            asof=asof,
            artifact=artifact,
            check_id="static_source_rows_not_yet_effective",
            severity="INFO",
            passed=True,
            observed=not_effective_asof,
            expected="PIT-filtered by consumers",
            details=(
                "Versioned static source files may contain future or expired rows; downstream loaders must "
                "apply row_is_effective_asof() before using them in dated snapshots."
            ),
        )
        add_check(
            checks,
            asof=asof,
            artifact=artifact,
            check_id="static_source_backdated_metadata_detector",
            severity="CRITICAL",
            passed=backdated_share < 0.50,
            observed=f"{rows_backdated_to_start}/{len(rows)}",
            expected="<50% rows with valid_from equal to the backfill start and reviewed_at before valid_from",
            details=(
                "Mass restamping static override files to the first historical as-of can embed future-known "
                "taxonomy/FDA/reimbursement decisions into old snapshots. Restamp rows to true reviewed/effective dates."
            ),
        )
        add_check(
            checks,
            asof=asof,
            artifact=artifact,
            check_id="static_source_readable",
            severity="INFO",
            passed=bool(fields),
            observed=f"rows={len(rows)} columns={len(fields)}",
            expected="readable CSV",
            details=(
                "Static source CSV was parsed for OOS metadata checks. "
                f"pit_metadata_columns_present={int(has_metadata_column)}"
            ),
        )


def write_checks(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["asof_date", "artifact", "check_id", "severity", "status", "observed", "expected", "details"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def diagnostic_checks_from_strict(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    diagnostic_rows: list[dict[str, Any]] = []
    for row in rows:
        out = dict(row)
        if out.get("check_id") == "static_source_rows_have_pit_metadata_values":
            out["status"] = "PASS"
            out["details"] = f"{out.get('details', '')} Diagnostic mode permits missing PIT metadata."
        diagnostic_rows.append(out)
    return diagnostic_rows


def build_checks(
    *,
    config: dict[str, Any],
    base_dir: Path,
    reports_root: Path,
    asofs: list[str],
    new_daily_columns_required_from: date | None,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    static_paths = static_source_paths(config, base_dir=base_dir)
    for asof in asofs:
        validate_daily_csv(
            reports_root / asof / "med_device_daily_composite_scores.csv",
            asof=asof,
            checks=checks,
            new_daily_columns_required_from=new_daily_columns_required_from,
        )
        validate_static_sources(
            static_paths,
            asof=asof,
            start_asof=asofs[0],
            checks=checks,
        )
    return checks


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    reports_root = (
        args.reports_root.expanduser().resolve()
        if args.reports_root
        else resolve_path("../output/med_devices_reports/score_review_pack", base_dir=base_dir)
    )
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            "../output/med_devices_reports/oos_validation/med_device_historical_snapshot_oos_validation.csv",
            base_dir=base_dir,
        )
    )
    diagnostic_output_csv = (
        args.diagnostic_output_csv.expanduser().resolve()
        if args.diagnostic_output_csv
        else default_diagnostic_path(output_csv)
    )
    asofs = discover_asofs(reports_root, explicit=args.asof, start=args.start_asof, end=args.end_asof)
    if not asofs:
        raise RuntimeError(
            "OOS validation found zero dated snapshots. "
            f"reports_root={reports_root} explicit_asof={args.asof!r} start={args.start_asof!r} end={args.end_asof!r}"
        )
    new_daily_columns_raw = str(
        args.new_daily_columns_required_from
        or cfg_get(config, "historical_backfill.new_daily_columns_required_from", "")
        or ""
    ).strip()
    new_daily_columns_required_from = parse_date(new_daily_columns_raw)
    if new_daily_columns_raw and new_daily_columns_required_from is None:
        raise RuntimeError(
            "Invalid historical_backfill.new_daily_columns_required_from date: "
            f"{new_daily_columns_raw!r}. Use YYYY-MM-DD or leave blank."
        )
    checks = build_checks(
        config=config,
        base_dir=base_dir,
        reports_root=reports_root,
        asofs=asofs,
        new_daily_columns_required_from=new_daily_columns_required_from,
    )
    write_checks(output_csv, checks)
    diagnostic_checks = diagnostic_checks_from_strict(checks)
    diagnostic_written = False
    if diagnostic_output_csv != output_csv:
        write_checks(diagnostic_output_csv, diagnostic_checks)
        diagnostic_written = True
    critical_failures = sum(1 for row in checks if row["severity"] == "CRITICAL" and row["status"] == "FAIL")
    diagnostic_critical_failures = sum(
        1 for row in diagnostic_checks if row["severity"] == "CRITICAL" and row["status"] == "FAIL"
    )
    diagnostic_note = f" diagnostic_output={diagnostic_output_csv}" if diagnostic_written else ""
    print(
        f"oos_validation_output={output_csv}{diagnostic_note} "
        f"asofs={len(asofs)} checks={len(checks)} critical_failures={critical_failures}"
    )
    return 1 if (diagnostic_critical_failures if args.allow_missing_static_pit_metadata else critical_failures) else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import os
import re
import sqlite3
import sys
from datetime import date, timedelta
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
# FDA product-family shadow columns (scripts 78 -> 13 -> 16 DAILY_COMPOSITE_CONTRACT).
# Gated by their own cutover anchor (historical_backfill.product_family_shadow_columns_required_from)
# rather than new_daily_columns_required_from: the 2019-01-04 post-migration anchor predates the
# shadow feature, so promoting these under that anchor would fail every pre-shadow snapshot.
PRODUCT_FAMILY_SHADOW_DAILY_COLUMNS = {
    "fda_event_risk_product_family_adjusted_score",
    "fda_safety_product_family_adjusted_score",
    "fda_product_family_shadow_available_flag",
    "fda_product_family_shadow_oos_valid_flag",
    "fda_product_family_adjustment_applied_flag",
    "fda_product_family_exposure_available_count",
    "fda_product_family_exposure_waived_count",
    "fda_product_family_exposure_missing_count",
    "fda_product_family_shadow_status",
    "fda_product_family_shadow_reason",
}
RESEARCH_ELIGIBLE_SAMPLE_ROLES = {"research_calibration_input", "strict_oos"}
SCORE_PROVENANCE_DAILY_COLUMNS = {
    "ic_tilted_composite_mode",
    "production_score_source",
    "ic_tilt_applied_to_production_flag",
    "production_score_regime_version",
}
IC_TILT_PRODUCTION_SOURCES = {"ic_tilted_composite", "ic_tilted_composite_score"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate med-devices dated snapshots for point-in-time/OOS readiness."
    )
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
        "--product-family-shadow-columns-required-from",
        default="",
        help=(
            "Promote missing FDA product-family shadow daily columns to CRITICAL for snapshots on/after this "
            "as-of date. Before this date, missing shadow columns are WARNINGs so pre-shadow snapshots do not "
            "block calibration. Defaults to historical_backfill.product_family_shadow_columns_required_from."
        ),
    )
    parser.add_argument(
        "--score-provenance-columns-required-from",
        default="",
        help=(
            "Require explicit production-score provenance columns for snapshots on/after this date. "
            "Earlier clean shadow snapshots may use the legacy mode field as transitional evidence."
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
        dates = (
            [path.name for path in root.iterdir() if path.is_dir() and DATE_RE.match(path.name)]
            if root.exists()
            else []
        )
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
    product_family_shadow_columns_required_from: date | None,
    score_provenance_columns_required_from: date | None = None,
) -> dict[str, Any]:
    artifact = str(path)
    summary: dict[str, Any] = {"row_count": None, "score_model_versions": set()}
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
        return summary
    fields, rows = read_csv_rows(path)
    field_set = set(fields)
    summary["row_count"] = len(rows)
    if "score_model_version" in field_set:
        summary["score_model_versions"] = {
            str(row.get("score_model_version") or "").strip()
            for row in rows
            if str(row.get("score_model_version") or "").strip()
        }
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
    provenance_missing = sorted(SCORE_PROVENANCE_DAILY_COLUMNS - field_set)
    enforce_score_provenance = (
        score_provenance_columns_required_from is not None
        and snapshot_date is not None
        and snapshot_date >= score_provenance_columns_required_from
    )
    provenance_severity = "CRITICAL" if enforce_score_provenance else "WARNING"
    add_check(
        checks,
        asof=asof,
        artifact=artifact,
        check_id="daily_score_provenance_columns",
        severity=provenance_severity,
        passed=not provenance_missing,
        observed=",".join(provenance_missing),
        expected="all production-score provenance columns",
        details=(
            "Explicit production score source, IC-tilt application flag, mode, and regime version "
            "are required on/after "
            f"{score_provenance_columns_required_from.isoformat() if score_provenance_columns_required_from else 'an unset cutover date'}."
        ),
    )

    unsafe_replacement_rows = 0
    inconsistent_provenance_rows = 0
    allowed_safe_modes = {"shadow", "disabled", "fallback_no_valid_ic"}
    for row in rows:
        mode = str(row.get("ic_tilted_composite_mode") or "").strip().lower()
        source = str(row.get("production_score_source") or "").strip().lower()
        flag_text = str(row.get("ic_tilt_applied_to_production_flag") or "").strip().lower()
        regime = str(row.get("production_score_regime_version") or "").strip()
        flag_true = flag_text in {"1", "1.0", "true", "yes"}
        unsafe = mode == "replace_raw" or source in IC_TILT_PRODUCTION_SOURCES or flag_true
        if unsafe:
            unsafe_replacement_rows += 1

        if provenance_missing:
            continue
        if flag_text not in {"0", "1"}:
            inconsistent_provenance_rows += 1
            continue
        if unsafe:
            if not (
                mode == "replace_raw"
                and source == "ic_tilted_composite_score"
                and flag_text == "1"
                and regime == "med_devices_ic_tilt_replace_legacy_v1"
            ):
                inconsistent_provenance_rows += 1
        elif (
            mode not in allowed_safe_modes
            or source != "baseline_composite_score"
            or flag_text != "0"
            or not regime
        ):
            inconsistent_provenance_rows += 1

    add_check(
        checks,
        asof=asof,
        artifact=artifact,
        check_id="daily_no_ic_tilt_production_replacement",
        severity="CRITICAL",
        passed=unsafe_replacement_rows == 0,
        observed=unsafe_replacement_rows,
        expected=0,
        details=(
            "IC-tilted scores are shadow diagnostics only. replace_raw mode, an IC production source, "
            "or an asserted production-application flag makes the snapshot unsafe for OOS/research use."
        ),
    )
    add_check(
        checks,
        asof=asof,
        artifact=artifact,
        check_id="daily_score_provenance_consistency",
        severity=provenance_severity,
        passed=not provenance_missing and inconsistent_provenance_rows == 0,
        observed=(
            f"missing_fields={','.join(provenance_missing)}"
            if provenance_missing
            else inconsistent_provenance_rows
        ),
        expected=0,
        details=(
            "Safe rows must identify baseline_composite_score with flag 0 and a non-empty regime; "
            "legacy IC replacements must be fully and consistently labeled even though they remain prohibited."
        ),
    )

    shadow_missing = sorted(PRODUCT_FAMILY_SHADOW_DAILY_COLUMNS - field_set)
    enforce_shadow_columns = (
        product_family_shadow_columns_required_from is not None
        and snapshot_date is not None
        and snapshot_date >= product_family_shadow_columns_required_from
    )
    add_check(
        checks,
        asof=asof,
        artifact=artifact,
        check_id="daily_product_family_shadow_columns",
        severity="CRITICAL" if enforce_shadow_columns else "WARNING",
        passed=not shadow_missing,
        observed=",".join(shadow_missing),
        expected="all FDA product-family shadow columns",
        details=(
            "FDA product-family shadow columns are required only on/after "
            f"{product_family_shadow_columns_required_from.isoformat() if product_family_shadow_columns_required_from else 'an unset shadow cutover date'}; "
            "before that cutover, missing shadow columns are diagnostic warnings for pre-shadow snapshots."
        ),
    )
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
            1 for row in rows if str(row.get("oos_score_valid_flag") or "").strip() not in {"0", "1"}
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
        publisher_defaulted_stage11_rows = sum(
            1
            for row in rows
            if not str(row.get("stage11_calibration_panel_source") or "").strip()
            or not str(row.get("research_calibration_status") or "").strip()
        )
        add_check(
            checks,
            asof=asof,
            artifact=artifact,
            check_id="daily_stage11_metadata_not_publisher_defaulted",
            severity=post_migration_severity,
            passed=publisher_defaulted_stage11_rows == 0,
            observed=publisher_defaulted_stage11_rows,
            expected=0,
            details=(
                "Rows carrying Stage 11 columns must publish real Stage 11 metadata; empty "
                "stage11_calibration_panel_source or research_calibration_status indicates publisher defaults."
            ),
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
    ticker_counts: dict[str, int] = {}
    for row in rows:
        ticker = str(row.get("ticker") or "").strip()
        if ticker:
            ticker_counts[ticker] = ticker_counts.get(ticker, 0) + 1
    duplicate_ticker_rows = sum(count - 1 for count in ticker_counts.values() if count > 1)
    add_check(
        checks,
        asof=asof,
        artifact=artifact,
        check_id="daily_no_duplicate_tickers",
        severity="CRITICAL",
        passed=duplicate_ticker_rows == 0,
        observed=duplicate_ticker_rows,
        expected=0,
        details="Each ticker must appear at most once per dated daily snapshot.",
    )
    out_of_range_composite_rows = sum(
        1
        for row in rows
        if (composite := parse_float(row.get("composite_score"))) is not None and (composite < 0.0 or composite > 100.0)
    )
    add_check(
        checks,
        asof=asof,
        artifact=artifact,
        check_id="daily_composite_score_range",
        severity="CRITICAL",
        passed=out_of_range_composite_rows == 0,
        observed=out_of_range_composite_rows,
        expected=0,
        details="Composite scores must stay within the published 0..100 scale.",
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
    return summary


def static_source_paths(config: dict[str, Any], *, base_dir: Path) -> list[tuple[str, Path]]:
    specs = [
        ("taxonomy_override", "calibration.taxonomy_override_csv"),
        ("fda_entity_manual_overrides", "fda_entity_linking.manual_overrides_csv"),
        ("fda_company_aliases", "fda_entity_linking.extra_alias_csv"),
        ("fda_entity_product_line_overrides", "fda_entity_linking.product_line_overrides_csv"),
        ("fda_review_overrides", "fda_features.review_override_csv"),
        ("fda_footprints", "fda_features.footprint_csv"),
        ("fda_manual_footprint_evidence", "fda_features.manual_footprint_evidence_csv"),
        (
            "fda_adverse_event_adjudications",
            "fda_features.adverse_event_adjudication_csv",
        ),
        ("reimbursement_company_classifications", "reimbursement_features.company_classification_csv"),
        ("reimbursement_mapping_overrides", "reimbursement_entity_linking.override_csv"),
        ("reimbursement_policy_overrides", "reimbursement_entity_linking.policy_override_csv"),
        ("reimbursement_resolved_classifications", "reimbursement_entity_linking.resolved_classification_csv"),
        ("reimbursement_manual_payment_rates", "reimbursement_entity_linking.manual_rate_csv"),
        ("company_risk_events", "company_risk_events.input_csv"),
        ("analyst_review_decisions", "med_devices_analyst_review.decisions_csv"),
        ("historical_membership", "med_devices_universe.historical_membership_csv"),
        ("share_count_overrides", "financial_features.share_count_override_csv"),
        ("fda_product_family_mapping", "fda_product_family_review.product_family_mapping_csv"),
        ("fda_product_family_exposure", "fda_product_family_review.product_family_exposure_csv"),
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
        uniform_restamped_valid_from = False
        if start_date is not None and rows:
            valid_from_values = {parse_date(row_value(row, *START_DATE_COLUMNS)) for row in rows}
            if len(valid_from_values) == 1:
                only_valid_from = next(iter(valid_from_values))
                uniform_restamped_valid_from = only_valid_from is not None and only_valid_from in {
                    start_date,
                    start_date - timedelta(days=1),
                }
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
        # PIT policy: "Event-dated rows apply historically." Neither ordering of
        # reviewed_at versus valid_from is a violation (reviewed_at > valid_from is
        # the expected honest pattern for event-dated rows; reviewed_at < valid_from
        # is ordinary review-before-effectiveness provenance), so no per-row
        # reviewed_at/valid_from ordering check exists here. Mass-restamping is
        # still caught by static_source_backdated_metadata_detector below, which
        # anchors on the backfill start_asof.
        add_check(
            checks,
            asof=asof,
            artifact=artifact,
            check_id="static_source_backdated_metadata_detector",
            severity="CRITICAL",
            passed=backdated_share < 0.50 and not uniform_restamped_valid_from,
            observed=f"{rows_backdated_to_start}/{len(rows)} uniform_restamp={int(uniform_restamped_valid_from)}",
            expected=(
                "<50% rows with valid_from equal to the backfill start and reviewed_at before valid_from; "
                "no file where 100% of rows share one valid_from equal to the backfill start (or start minus one day)"
            ),
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


COMPONENT_IC_PROVENANCE_COLUMNS = ("generated_asof", "valid_from")


def validate_component_ic_provenance(
    path: Path,
    *,
    asof: str,
    checks: list[dict[str, Any]],
) -> None:
    artifact = f"component_ic:{path}"
    if not path.exists():
        add_check(
            checks,
            asof=asof,
            artifact=artifact,
            check_id="component_ic_provenance",
            severity="CRITICAL",
            passed=False,
            observed="missing",
            expected="exists with generated_asof/valid_from provenance",
            details=(
                "calibration.component_ic_csv shapes IC-tilted composites; it must exist and carry "
                "point-in-time provenance for strict OOS validation."
            ),
        )
        return
    fields, rows = read_csv_rows(path)
    lowered_fields = {field.strip().lower() for field in fields}
    provenance_columns = [column for column in COMPONENT_IC_PROVENANCE_COLUMNS if column in lowered_fields]
    if not provenance_columns:
        add_check(
            checks,
            asof=asof,
            artifact=artifact,
            check_id="component_ic_provenance",
            severity="CRITICAL",
            passed=False,
            observed="missing_columns=" + ",".join(COMPONENT_IC_PROVENANCE_COLUMNS),
            expected="generated_asof or valid_from column",
            details=(
                "calibration.component_ic_csv shapes IC-tilted composites; without a generated_asof/valid_from "
                "column the IC panel can embed look-ahead information into historical snapshots."
            ),
        )
        return
    invalid_provenance_rows = sum(1 for row in rows if parse_date(row_value(row, *provenance_columns)) is None)
    add_check(
        checks,
        asof=asof,
        artifact=artifact,
        check_id="component_ic_provenance",
        severity="CRITICAL",
        passed=invalid_provenance_rows == 0,
        observed=invalid_provenance_rows,
        expected=0,
        details=(
            "Every component IC row must carry a parseable generated_asof/valid_from provenance value. "
            "The check PASSES whenever provenance columns are present and parseable; a recent generated_asof "
            "does not fail historical as-of dates."
        ),
    )
    if invalid_provenance_rows:
        return
    target = parse_date(asof)
    generated_dates = [
        generated for row in rows if (generated := parse_date(row_value(row, *provenance_columns))) is not None
    ]
    max_generated = max(generated_dates) if generated_dates else None
    ic_pit_available = target is None or max_generated is None or max_generated <= target
    add_check(
        checks,
        asof=asof,
        artifact=artifact,
        check_id="component_ic_pit_availability",
        severity="WARNING",
        passed=ic_pit_available,
        observed=f"generated_asof={max_generated.isoformat() if max_generated else ''} asof={asof}",
        expected="asof on/after generated_asof for PIT-available IC tilt",
        details=(
            "IC tilt is LIVE-ONLY by policy decision: the component IC panel carries generated_asof/valid_from "
            "provenance, and as-of dates earlier than generated_asof cannot use the IC tilt point-in-time. "
            "This is a WARNING (not CRITICAL) until a PIT-clean IC history exists; consumers must not apply "
            "the IC tilt to historical snapshots dated before generated_asof."
        ),
    )


def validate_daily_db_reconciliation(
    *,
    asof: str,
    artifact: str,
    csv_row_count: int,
    db_conn: sqlite3.Connection | None,
    db_path: Path | None,
    checks: list[dict[str, Any]],
) -> None:
    if db_conn is None:
        add_check(
            checks,
            asof=asof,
            artifact=artifact,
            check_id="daily_db_row_count_reconciliation",
            severity="WARNING",
            passed=False,
            observed="db_unavailable",
            expected="med_device_daily_scores row count matches daily CSV",
            details=f"med_devices database not available for reconciliation: {db_path}",
        )
        return
    try:
        row = db_conn.execute(
            "SELECT COUNT(*) FROM med_device_daily_scores WHERE asof_date = ?",
            (asof,),
        ).fetchone()
        db_count = int(row[0]) if row else 0
    except sqlite3.Error as exc:
        add_check(
            checks,
            asof=asof,
            artifact=artifact,
            check_id="daily_db_row_count_reconciliation",
            severity="WARNING",
            passed=False,
            observed=f"db_error={exc}",
            expected="med_device_daily_scores row count matches daily CSV",
            details=f"med_devices database query failed during reconciliation: {db_path}",
        )
        return
    add_check(
        checks,
        asof=asof,
        artifact=artifact,
        check_id="daily_db_row_count_reconciliation",
        severity="CRITICAL",
        passed=db_count == csv_row_count,
        observed=f"csv={csv_row_count} db={db_count}",
        expected="csv row count equals med_device_daily_scores count for the as-of date",
        details="Published dated daily CSVs must reconcile with the med_device_daily_scores table.",
    )


def write_checks(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["asof_date", "artifact", "check_id", "severity", "status", "observed", "expected", "details"]
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp_path, path)


def diagnostic_checks_from_strict(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    diagnostic_rows: list[dict[str, Any]] = []
    for row in rows:
        out = dict(row)
        if out.get("check_id") == "static_source_rows_have_pit_metadata_values" and out.get("status") == "FAIL":
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
    start_asof: str,
    new_daily_columns_required_from: date | None,
    product_family_shadow_columns_required_from: date | None,
    score_provenance_columns_required_from: date | None,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    static_paths = static_source_paths(config, base_dir=base_dir)
    component_ic_raw = str(cfg_get(config, "calibration.component_ic_csv", "") or "").strip()
    component_ic_path = resolve_path(component_ic_raw, base_dir=base_dir) if component_ic_raw else None
    db_raw = str(cfg_get(config, "paths.database_path", "") or "").strip()
    db_path = resolve_path(db_raw, base_dir=base_dir) if db_raw else None
    db_conn: sqlite3.Connection | None = None
    if db_path is not None and db_path.exists():
        db_conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    score_model_versions: set[str] = set()
    try:
        for asof in asofs:
            daily_path = reports_root / asof / "med_device_daily_composite_scores.csv"
            daily_summary = validate_daily_csv(
                daily_path,
                asof=asof,
                checks=checks,
                new_daily_columns_required_from=new_daily_columns_required_from,
                product_family_shadow_columns_required_from=product_family_shadow_columns_required_from,
                score_provenance_columns_required_from=score_provenance_columns_required_from,
            )
            score_model_versions |= daily_summary["score_model_versions"]
            if daily_summary["row_count"] is not None:
                validate_daily_db_reconciliation(
                    asof=asof,
                    artifact=str(daily_path),
                    csv_row_count=daily_summary["row_count"],
                    db_conn=db_conn,
                    db_path=db_path,
                    checks=checks,
                )
            validate_static_sources(
                static_paths,
                asof=asof,
                start_asof=start_asof,
                checks=checks,
            )
            if component_ic_path is not None:
                validate_component_ic_provenance(
                    component_ic_path,
                    asof=asof,
                    checks=checks,
                )
    finally:
        if db_conn is not None:
            db_conn.close()
    distinct_versions = sorted(score_model_versions)
    add_check(
        checks,
        asof="ALL",
        artifact="panel:daily_score_model_version",
        check_id="panel_single_score_model_version",
        severity="CRITICAL",
        passed=len(distinct_versions) <= 1,
        observed=",".join(distinct_versions),
        expected="at most one distinct non-empty score_model_version across validated asofs",
        details="Mixed score model versions inside one validated panel break calibration comparability.",
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
        else resolve_path(
            cfg_get(config, "scoring.review_pack_dir", "../output/med_devices_reports/score_review_pack"),
            base_dir=base_dir,
        )
    )
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(
                config,
                "historical_backfill.oos_validation_csv",
                "../output/med_devices_reports/oos_validation/med_device_historical_snapshot_oos_validation.csv",
            ),
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
    score_provenance_raw = str(
        args.score_provenance_columns_required_from
        or cfg_get(config, "historical_backfill.score_provenance_columns_required_from", "")
        or ""
    ).strip()
    score_provenance_columns_required_from = parse_date(score_provenance_raw)
    if score_provenance_raw and score_provenance_columns_required_from is None:
        raise RuntimeError(
            "Invalid historical_backfill.score_provenance_columns_required_from date: "
            f"{score_provenance_raw!r}. Use YYYY-MM-DD or leave blank."
        )
    shadow_columns_raw = str(
        args.product_family_shadow_columns_required_from
        or cfg_get(config, "historical_backfill.product_family_shadow_columns_required_from", "")
        or ""
    ).strip()
    product_family_shadow_columns_required_from = parse_date(shadow_columns_raw)
    if shadow_columns_raw and product_family_shadow_columns_required_from is None:
        raise RuntimeError(
            "Invalid historical_backfill.product_family_shadow_columns_required_from date: "
            f"{shadow_columns_raw!r}. Use YYYY-MM-DD or leave blank."
        )
    config_start_asof = str(cfg_get(config, "historical_backfill.start_asof", "") or "").strip()
    if config_start_asof and parse_date(config_start_asof) is None:
        raise RuntimeError(
            f"Invalid historical_backfill.start_asof date: {config_start_asof!r}. Use YYYY-MM-DD or leave blank."
        )
    detector_start_asof = config_start_asof or asofs[0]
    checks = build_checks(
        config=config,
        base_dir=base_dir,
        reports_root=reports_root,
        asofs=asofs,
        start_asof=detector_start_asof,
        new_daily_columns_required_from=new_daily_columns_required_from,
        product_family_shadow_columns_required_from=product_family_shadow_columns_required_from,
        score_provenance_columns_required_from=score_provenance_columns_required_from,
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
    if args.allow_missing_static_pit_metadata:
        downgraded_failures = sum(
            1
            for row in checks
            if row["check_id"] == "static_source_rows_have_pit_metadata_values" and row["status"] == "FAIL"
        )
        if downgraded_failures:
            print(
                "WARNING: --allow-missing-static-pit-metadata downgraded "
                f"{downgraded_failures} failing static PIT metadata check(s) in the diagnostic CSV; "
                "the strict output CSV remains authoritative.",
                file=sys.stderr,
            )
    diagnostic_note = f" diagnostic_output={diagnostic_output_csv}" if diagnostic_written else ""
    print(
        f"oos_validation_output={output_csv}{diagnostic_note} "
        f"asofs={len(asofs)} checks={len(checks)} critical_failures={critical_failures}"
    )
    return 1 if (diagnostic_critical_failures if args.allow_missing_static_pit_metadata else critical_failures) else 0


if __name__ == "__main__":
    raise SystemExit(main())

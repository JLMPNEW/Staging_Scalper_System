#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
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
    PIT_METADATA_COLUMNS,
    parse_iso_date,
    pit_date_parse_errors,
    row_has_pit_metadata,
    row_is_effective_asof,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CORE_REQUIRED_DAILY_COLUMNS = {
    "asof_date",
    "ticker",
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
        passed=invalid_review_dates == 0,
        observed=invalid_review_dates,
        expected=0,
        details="Applied analyst decisions must use parseable YYYY-MM-DD reviewed_at values.",
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
        ("fda_entity_product_line_overrides", "fda_entity_linking.product_line_overrides_csv"),
        ("fda_review_overrides", "fda_features.review_override_csv"),
        ("fda_footprints", "fda_features.footprint_csv"),
        ("fda_manual_footprint_evidence", "fda_features.manual_footprint_evidence_csv"),
        ("reimbursement_company_classifications", "reimbursement_features.company_classification_csv"),
        ("reimbursement_mapping_overrides", "reimbursement_entity_linking.override_csv"),
        ("reimbursement_resolved_classifications", "reimbursement_entity_linking.resolved_classification_csv"),
        ("reimbursement_manual_payment_rates", "reimbursement_entity_linking.manual_rate_csv"),
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
    checks: list[dict[str, Any]],
) -> None:
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
    new_daily_columns_required_from = parse_date(
        args.new_daily_columns_required_from
        or cfg_get(config, "historical_backfill.new_daily_columns_required_from", "")
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

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import os
import tempfile
from pathlib import Path
from typing import Iterable


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COHORT_REGISTRY = PACKAGE_ROOT / "data" / "biotech_calibration_cohorts.csv"
DEFAULT_STATUS_OVERRIDES = PACKAGE_ROOT / "data" / "company_status_overrides.csv"
DEFAULT_COHORT_MIGRATION = PACKAGE_ROOT / "data" / "biotech_cohort_migration_20260831.csv"
DEFAULT_ACTIVE_REMOVALS = PACKAGE_ROOT / "data" / "biotech_active_universe_removals_20260831.csv"

ALLOWED_COHORTS = frozenset(
    {
        "commercial_profitable_quality_or_mature",
        "commercial_turnaround_or_unprofitable_growth",
        "late_clinical_pivotal_or_registrational",
        "platform_partnered_modality_pipeline",
        "early_clinical_speculative_or_single_asset_pipeline",
    }
)
COHORT_FIELDS = ("ticker", "biotech_calibration_cohort", "reason")
STATUS_FIELDS = (
    "ticker",
    "decision",
    "listing_status",
    "manual_include",
    "manual_exclude",
    "manual_review",
    "reason_codes",
    "notes",
    "effective_date",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate and atomically apply the 2026-08-31 biotech cohort reorganization "
            "and effective-dated active-universe removals. The command is check-only unless --apply is set."
        )
    )
    parser.add_argument("--cohort-registry", type=Path, default=DEFAULT_COHORT_REGISTRY)
    parser.add_argument("--status-overrides", type=Path, default=DEFAULT_STATUS_OVERRIDES)
    parser.add_argument("--cohort-migration", type=Path, default=DEFAULT_COHORT_MIGRATION)
    parser.add_argument("--active-removals", type=Path, default=DEFAULT_ACTIVE_REMOVALS)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def read_csv(path: Path, required_fields: Iterable[str]) -> tuple[list[str], list[dict[str, str]]]:
    with path.expanduser().resolve().open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = [str(field or "").strip() for field in (reader.fieldnames or [])]
        missing = sorted(set(required_fields) - set(fields))
        if missing:
            raise ValueError(f"{path} is missing required field(s): {missing}")
        rows = [{field: str(row.get(field) or "").strip() for field in fields} for row in reader]
    return fields, rows


def index_unique(rows: Iterable[dict[str, str]], *, path: Path) -> dict[str, dict[str, str]]:
    indexed: dict[str, dict[str, str]] = {}
    for line_no, row in enumerate(rows, start=2):
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker:
            raise ValueError(f"{path}:{line_no} has a blank ticker")
        if ticker in indexed:
            raise ValueError(f"{path}:{line_no} duplicates ticker {ticker}")
        row["ticker"] = ticker
        indexed[ticker] = row
    return indexed


def append_reason_codes(*codes: str) -> str:
    output: list[str] = []
    for raw in codes:
        for code in str(raw or "").replace("|", ";").split(";"):
            clean = code.strip()
            if clean and clean not in output:
                output.append(clean)
    return ";".join(output)


def apply_cohort_moves(
    cohort_rows: list[dict[str, str]],
    migration_rows: list[dict[str, str]],
    *,
    cohort_path: Path,
    migration_path: Path,
) -> tuple[int, int]:
    cohorts = index_unique(cohort_rows, path=cohort_path)
    migrations = index_unique(migration_rows, path=migration_path)
    changed = 0
    already_applied = 0
    for ticker, migration in migrations.items():
        expected = migration["expected_current_cohort"]
        new_cohort = migration["new_cohort"]
        effective_date = migration["effective_date"]
        reason = migration["reason"]
        if expected not in ALLOWED_COHORTS or new_cohort not in ALLOWED_COHORTS:
            raise ValueError(f"Invalid cohort migration for {ticker}: {expected!r} -> {new_cohort!r}")
        if not effective_date:
            raise ValueError(f"Cohort migration for {ticker} has no effective_date")
        current = cohorts.get(ticker)
        if current is None:
            raise ValueError(f"Cohort migration ticker is absent from registry: {ticker}")
        current_cohort = current["biotech_calibration_cohort"]
        marker = f"source={reason}"
        if current_cohort == new_cohort and marker in current["reason"]:
            already_applied += 1
            continue
        if current_cohort != expected:
            raise ValueError(
                f"Cohort precondition failed for {ticker}: expected {expected!r}, found {current_cohort!r}"
            )
        current["biotech_calibration_cohort"] = new_cohort
        current["reason"] = (
            f"user-approved cohort reorganization effective {effective_date}; "
            f"prior_cohort={expected}; source={reason}"
        )
        changed += 1
    return changed, already_applied


def apply_active_removals(
    status_rows: list[dict[str, str]],
    removal_rows: list[dict[str, str]],
    *,
    status_path: Path,
    removal_path: Path,
) -> tuple[int, int]:
    statuses = index_unique(status_rows, path=status_path)
    removals = index_unique(removal_rows, path=removal_path)
    changed = 0
    already_applied = 0
    for ticker, removal in removals.items():
        effective_date = removal["effective_date"]
        reason = removal["reason"]
        if not effective_date:
            raise ValueError(f"Active-universe removal for {ticker} has no effective_date")
        row = statuses.get(ticker)
        if row is None:
            row = {field: "" for field in STATUS_FIELDS}
            row["ticker"] = ticker
            status_rows.append(row)
            statuses[ticker] = row
        expected_codes = append_reason_codes(
            "manual_exclude",
            "removed_from_biotech_active_universe",
            reason,
        )
        expected_note = (
            f"User approved removal from the active biotech universe effective {effective_date}. "
            "Historical PIT membership, prices, features, and calibration observations remain preserved."
        )
        already = (
            row.get("decision") == "remove"
            and row.get("manual_exclude") == "true"
            and row.get("effective_date") == effective_date
            and "removed_from_biotech_active_universe" in row.get("reason_codes", "")
        )
        if already:
            already_applied += 1
            continue
        row.update(
            {
                "decision": "remove",
                "manual_include": "false",
                "manual_exclude": "true",
                "manual_review": "false",
                "reason_codes": expected_codes,
                "notes": expected_note,
                "effective_date": effective_date,
            }
        )
        changed += 1
    return changed, already_applied


def write_csv_atomic(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    args = parse_args()
    cohort_path = args.cohort_registry.expanduser().resolve()
    status_path = args.status_overrides.expanduser().resolve()
    migration_path = args.cohort_migration.expanduser().resolve()
    removal_path = args.active_removals.expanduser().resolve()

    cohort_fields, cohort_rows = read_csv(cohort_path, COHORT_FIELDS)
    status_fields, status_rows = read_csv(status_path, STATUS_FIELDS)
    _, migration_rows = read_csv(
        migration_path,
        ("ticker", "expected_current_cohort", "new_cohort", "effective_date", "reason"),
    )
    _, removal_rows = read_csv(removal_path, ("ticker", "effective_date", "reason"))
    cohort_changed, cohort_existing = apply_cohort_moves(
        cohort_rows,
        migration_rows,
        cohort_path=cohort_path,
        migration_path=migration_path,
    )
    status_changed, status_existing = apply_active_removals(
        status_rows,
        removal_rows,
        status_path=status_path,
        removal_path=removal_path,
    )

    if args.apply:
        write_csv_atomic(cohort_path, cohort_fields, cohort_rows)
        write_csv_atomic(status_path, status_fields, status_rows)
    mode = "applied" if args.apply else "validated"
    print(
        f"{mode}: cohort_changed={cohort_changed} cohort_already_applied={cohort_existing} "
        f"status_changed={status_changed} status_already_applied={status_existing}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

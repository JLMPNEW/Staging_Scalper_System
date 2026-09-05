"""Stage 2 database validation and report publication."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from basic_materials import MODEL_FAMILY, SECTOR
from basic_materials.core.atomic_io import atomic_write_csv, atomic_write_json
from basic_materials.core.db import assert_database_identity, database_counts, utc_now
from basic_materials.core.input_manifest import ManifestValidation, file_sha256
from basic_materials.core.universe import UniversePolicy, read_and_validate_universe


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    issue_code: str
    message: str
    ticker: str | None = None
    details: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["details"] = dict(self.details or {})
        return payload


@dataclass(frozen=True)
class UniverseValidationReport:
    passed: bool
    validated_at_utc: str
    policy_version: str
    source_snapshot_date: str
    input_sha256: str
    expected_rows: int
    actual_rows: int
    database_counts: Mapping[str, int]
    expected_cohort_counts: Mapping[str, int]
    actual_cohort_counts: Mapping[str, int]
    issues: tuple[ValidationIssue, ...]
    universe_rows: tuple[Mapping[str, Any], ...]

    def summary_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "validated_at_utc": self.validated_at_utc,
            "policy_version": self.policy_version,
            "source_snapshot_date": self.source_snapshot_date,
            "input_sha256": self.input_sha256,
            "expected_rows": self.expected_rows,
            "actual_rows": self.actual_rows,
            "database_counts": dict(self.database_counts),
            "expected_cohort_counts": dict(self.expected_cohort_counts),
            "actual_cohort_counts": dict(self.actual_cohort_counts),
            "error_count": sum(issue.severity == "error" for issue in self.issues),
            "warning_count": sum(issue.severity == "warning" for issue in self.issues),
            "issues": [issue.as_dict() for issue in self.issues],
        }


def _count(conn: sqlite3.Connection, sql: str, parameters: tuple[Any, ...] = ()) -> int:
    return int(conn.execute(sql, parameters).fetchone()[0])


def validate_universe_database(
    conn: sqlite3.Connection,
    *,
    policy: UniversePolicy,
    manifest: ManifestValidation,
) -> UniverseValidationReport:
    assert_database_identity(conn)
    expected_rows = read_and_validate_universe(manifest.path, policy)
    expected_by_ticker = {row.ticker: row for row in expected_rows}
    issues: list[ValidationIssue] = []

    control_rows = conn.execute("SELECT * FROM model_control_state").fetchall()
    if len(control_rows) != 1:
        issues.append(
            ValidationIssue("error", "CONTROL_ROW_COUNT", f"Expected one model control row, found {len(control_rows)}")
        )
    else:
        control = control_rows[0]
        control_values = (
            control["promotion_state"],
            int(control["portfolio_candidate_gate"]),
            int(control["oos_score_valid_flag"]),
            int(control["current_universe_is_survivorship_corrected"]),
            int(control["current_universe_calibration_eligible"]),
        )
        if control_values != ("shadow_monitor", 0, 0, 0, 0):
            issues.append(
                ValidationIssue("error", "PROMOTION_GATE_OPEN", f"Invalid model control state: {control_values}")
            )

    source = conn.execute(
        "SELECT active FROM source_registry WHERE source_id = ?", (policy.source_id,)
    ).fetchone()
    if source is None or int(source["active"]) != 1:
        issues.append(
            ValidationIssue("error", "SOURCE_NOT_REGISTERED", f"Active source is missing: {policy.source_id}")
        )

    payload = conn.execute(
        "SELECT * FROM raw_source_payloads WHERE source_id = ? AND sha256 = ?",
        (policy.source_id, manifest.sha256),
    ).fetchone()
    if payload is None:
        issues.append(
            ValidationIssue("error", "RAW_INPUT_MISSING", "Manifested universe payload is absent from the raw layer")
        )
    else:
        stored_hash = hashlib.sha256(bytes(payload["payload"])).hexdigest()
        if stored_hash != manifest.sha256 or int(payload["row_count"]) != manifest.row_count:
            issues.append(
                ValidationIssue(
                    "error",
                    "RAW_INPUT_MISMATCH",
                    "Stored raw universe payload does not match the authoritative manifest",
                )
            )

    rows = conn.execute(
        """
        SELECT
            c.company_id,
            c.cik,
            c.legal_name AS company_name,
            c.domicile_country AS country,
            c.universe_status AS investability_status,
            c.is_active,
            s.security_id,
            s.ticker,
            s.exchange,
            s.trading_currency AS currency,
            s.security_type,
            s.listing_status,
            s.is_primary_listing,
            t.sector,
            t.industry,
            t.cohort_id,
            t.calibration_group,
            t.calibration_parent,
            t.lifecycle_state,
            t.policy_version AS taxonomy_policy_version,
            t.input_sha256 AS taxonomy_input_sha256,
            m.membership_start_date,
            m.membership_end_date,
            m.membership_status,
            m.membership_basis,
            m.current_source_only,
            m.survivorship_corrected,
            m.calibration_eligible,
            m.source_snapshot_date,
            m.policy_version AS membership_policy_version,
            m.input_sha256 AS membership_input_sha256
        FROM dim_universe_membership AS m
        JOIN dim_company AS c ON c.company_id = m.company_id
        JOIN dim_security AS s ON s.security_id = m.security_id
        JOIN dim_basic_materials_taxonomy AS t ON t.security_id = s.security_id
        WHERE m.model_family = ? AND m.membership_status = 'current' AND m.membership_end_date IS NULL
        ORDER BY s.ticker
        """,
        (MODEL_FAMILY,),
    ).fetchall()
    actual_by_ticker = {str(row["ticker"]).upper(): row for row in rows}
    expected_tickers = set(expected_by_ticker)
    actual_tickers = set(actual_by_ticker)
    missing = sorted(expected_tickers - actual_tickers)
    extra = sorted(actual_tickers - expected_tickers)
    if missing:
        issues.append(
            ValidationIssue("error", "MISSING_TICKERS", f"Missing {len(missing)} expected tickers", details={"tickers": missing})
        )
    if extra:
        issues.append(
            ValidationIssue("error", "UNEXPECTED_TICKERS", f"Found {len(extra)} unexpected tickers", details={"tickers": extra})
        )

    comparison_fields = (
        "cik",
        "company_name",
        "country",
        "investability_status",
        "exchange",
        "currency",
        "security_type",
        "listing_status",
        "sector",
        "industry",
    )
    output_rows: list[dict[str, Any]] = []
    for ticker in sorted(expected_tickers & actual_tickers):
        expected = expected_by_ticker[ticker]
        actual = actual_by_ticker[ticker]
        expected_values = expected.as_dict()
        for field in comparison_fields:
            if str(actual[field]) != str(expected_values[field]):
                issues.append(
                    ValidationIssue(
                        "error",
                        "FIELD_MISMATCH",
                        f"{field} expected {expected_values[field]!r}, got {actual[field]!r}",
                        ticker=ticker,
                    )
                )
        exact_expectations = {
            "is_active": 1,
            "is_primary_listing": int(expected.is_primary_listing),
            "cohort_id": expected.subsector,
            "calibration_group": expected.subsector,
            "calibration_parent": expected.calibration_parent,
            "lifecycle_state": policy.default_lifecycle_state,
            "taxonomy_policy_version": policy.policy_version,
            "taxonomy_input_sha256": manifest.sha256,
            "membership_start_date": policy.source_snapshot_date,
            "membership_status": "current",
            "membership_basis": policy.membership_basis,
            "current_source_only": 1,
            "survivorship_corrected": 0,
            "calibration_eligible": 0,
            "source_snapshot_date": policy.source_snapshot_date,
            "membership_policy_version": policy.policy_version,
            "membership_input_sha256": manifest.sha256,
        }
        for field, expected_value in exact_expectations.items():
            actual_value = actual[field]
            if actual_value != expected_value:
                issues.append(
                    ValidationIssue(
                        "error",
                        "CONTRACT_MISMATCH",
                        f"{field} expected {expected_value!r}, got {actual_value!r}",
                        ticker=ticker,
                    )
                )
        output_rows.append(
            {
                "ticker": ticker,
                "company_name": actual["company_name"],
                "cik": actual["cik"],
                "exchange": actual["exchange"],
                "country": actual["country"],
                "security_type": actual["security_type"],
                "cohort": actual["cohort_id"],
                "calibration_group": actual["calibration_group"],
                "calibration_parent": actual["calibration_parent"],
                "lifecycle_state": actual["lifecycle_state"],
                "current_source_only": actual["current_source_only"],
                "survivorship_corrected": actual["survivorship_corrected"],
                "calibration_eligible": actual["calibration_eligible"],
            }
        )

    actual_cohort_counts = dict(sorted(Counter(str(row["cohort_id"]) for row in rows).items()))
    expected_cohort_counts = dict(sorted(policy.cohort_counts().items()))
    if actual_cohort_counts != expected_cohort_counts:
        issues.append(
            ValidationIssue(
                "error",
                "COHORT_COUNT_MISMATCH",
                "Current cohort counts differ from policy",
                details={"expected": expected_cohort_counts, "actual": actual_cohort_counts},
            )
        )

    active_company_count = _count(conn, "SELECT COUNT(*) FROM dim_company WHERE is_active = 1")
    active_security_count = _count(
        conn,
        "SELECT COUNT(*) FROM dim_security WHERE listing_status = 'active' AND valid_to_date IS NULL",
    )
    taxonomy_count = _count(
        conn,
        """
        SELECT COUNT(*)
        FROM dim_basic_materials_taxonomy AS t
        JOIN dim_universe_membership AS m ON m.security_id = t.security_id
        WHERE m.membership_source_id = ?
          AND m.membership_status = 'current'
          AND m.membership_end_date IS NULL
        """,
        (policy.source_id,),
    )
    identifier_count = _count(
        conn,
        """
        SELECT COUNT(*)
        FROM dim_identifier
        WHERE source_id = ?
          AND identifier_type IN ('cik', 'ticker')
        """,
        (policy.source_id,),
    )
    expected_counts = {
        "active companies": active_company_count,
        "active securities": active_security_count,
        "taxonomy rows": taxonomy_count,
        "current memberships": len(rows),
    }
    for label, actual_count in expected_counts.items():
        if actual_count != policy.expected_current_rows:
            issues.append(
                ValidationIssue(
                    "error",
                    "FOUNDATION_COUNT_MISMATCH",
                    f"Expected {policy.expected_current_rows} {label}, found {actual_count}",
                )
            )
    if identifier_count != policy.expected_current_rows * 2:
        issues.append(
            ValidationIssue(
                "error",
                "IDENTIFIER_COUNT_MISMATCH",
                f"Expected {policy.expected_current_rows * 2} CIK/ticker identifiers, found {identifier_count}",
            )
        )

    duplicate_checks = {
        "DUPLICATE_ACTIVE_TICKER": """
            SELECT COUNT(*) FROM (
                SELECT ticker FROM dim_security GROUP BY upper(ticker) HAVING COUNT(*) > 1
            )
        """,
        "DUPLICATE_CIK": """
            SELECT COUNT(*) FROM (
                SELECT cik FROM dim_company GROUP BY cik HAVING COUNT(*) > 1
            )
        """,
    }
    for code, sql in duplicate_checks.items():
        duplicates = _count(conn, sql)
        if duplicates:
            issues.append(ValidationIssue("error", code, f"Found {duplicates} duplicate groups"))

    foreign_count = sum(str(row["country"]) != "United States" for row in rows)
    if foreign_count != policy.expected_foreign_rows:
        issues.append(
            ValidationIssue(
                "error",
                "FOREIGN_COUNT_MISMATCH",
                f"Expected {policy.expected_foreign_rows} foreign rows, found {foreign_count}",
            )
        )
    foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        issues.append(
            ValidationIssue(
                "error",
                "FOREIGN_KEY_FAILURE",
                f"Found {len(foreign_key_errors)} foreign-key violations",
            )
        )

    issues.append(
        ValidationIssue(
            "warning",
            "CURRENT_UNIVERSE_NOT_PIT",
            "The current authoritative seed is not survivorship-correct historical membership; calibration remains blocked.",
        )
    )
    passed = not any(issue.severity == "error" for issue in issues)
    return UniverseValidationReport(
        passed=passed,
        validated_at_utc=utc_now(),
        policy_version=policy.policy_version,
        source_snapshot_date=policy.source_snapshot_date,
        input_sha256=manifest.sha256,
        expected_rows=policy.expected_current_rows,
        actual_rows=len(rows),
        database_counts=database_counts(conn),
        expected_cohort_counts=expected_cohort_counts,
        actual_cohort_counts=actual_cohort_counts,
        issues=tuple(issues),
        universe_rows=tuple(output_rows),
    )


def write_validation_reports(
    report: UniverseValidationReport,
    *,
    policy: UniversePolicy,
    report_dir: str | Path,
) -> dict[str, str]:
    target = Path(report_dir).resolve(strict=False)
    target.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    written["summary"] = atomic_write_json(target / "validation_summary.json", report.summary_dict())

    issue_rows = [issue.as_dict() for issue in report.issues]
    flattened_issues = [
        {
            "severity": issue["severity"],
            "issue_code": issue["issue_code"],
            "ticker": issue["ticker"] or "",
            "message": issue["message"],
            "details": str(issue["details"]),
        }
        for issue in issue_rows
    ]
    written["issues"] = atomic_write_csv(
        target / "validation_issues.csv",
        flattened_issues,
        ("severity", "issue_code", "ticker", "message", "details"),
    )
    written["universe"] = atomic_write_csv(
        target / "universe_snapshot.csv",
        report.universe_rows,
        (
            "ticker",
            "company_name",
            "cik",
            "exchange",
            "country",
            "security_type",
            "cohort",
            "calibration_group",
            "calibration_parent",
            "lifecycle_state",
            "current_source_only",
            "survivorship_corrected",
            "calibration_eligible",
        ),
    )
    census_rows = [
        {
            "cohort": cohort_id,
            "expected_count": policy.cohorts[cohort_id].expected_count,
            "actual_count": report.actual_cohort_counts.get(cohort_id, 0),
            "calibration_parent": policy.cohorts[cohort_id].calibration_parent,
            "status": (
                "pass"
                if policy.cohorts[cohort_id].expected_count == report.actual_cohort_counts.get(cohort_id, 0)
                else "fail"
            ),
        }
        for cohort_id in sorted(policy.cohorts)
    ]
    written["cohort_census"] = atomic_write_csv(
        target / "cohort_census.csv",
        census_rows,
        ("cohort", "expected_count", "actual_count", "calibration_parent", "status"),
    )

    artifact_rows = {
        name: {
            "path": str(path),
            "sha256": file_sha256(path),
            "byte_size": path.stat().st_size,
        }
        for name, path in written.items()
    }
    manifest_path = atomic_write_json(
        target / "artifact_manifest.json",
        {
            "model_family": MODEL_FAMILY,
            "sector": SECTOR,
            "source_snapshot_date": report.source_snapshot_date,
            "generated_at_utc": report.validated_at_utc,
            "artifacts": artifact_rows,
        },
    )
    written["artifact_manifest"] = manifest_path
    return {name: str(path) for name, path in written.items()}

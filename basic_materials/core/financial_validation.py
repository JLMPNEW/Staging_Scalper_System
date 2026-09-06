"""Read-only Stage 4B financial ingestion, normalization, FX, and feature validation."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from basic_materials.core.atomic_io import atomic_write_csv, atomic_write_json
from basic_materials.core.db import assert_database_identity, database_counts, utc_now
from basic_materials.core.financial_ingestion import FinancialIngestionPolicy


@dataclass(frozen=True)
class FinancialStageIssue:
    severity: str
    issue_code: str
    message: str
    ticker: str = ""
    details: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "issue_code": self.issue_code,
            "ticker": self.ticker,
            "message": self.message,
            "details": dict(self.details or {}),
        }


@dataclass(frozen=True)
class FinancialStageValidationReport:
    passed: bool
    validated_at_utc: str
    as_of_date: str
    snapshot_key: str
    policy_version: str
    policy_sha256: str
    counts: Mapping[str, int]
    resolution_status_counts: Mapping[str, int]
    feature_quality_counts: Mapping[str, int]
    rank_ready_count: int
    valuation_ready_count: int
    issues: tuple[FinancialStageIssue, ...]

    def summary_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "counts": dict(self.counts),
            "resolution_status_counts": dict(self.resolution_status_counts),
            "feature_quality_counts": dict(self.feature_quality_counts),
            "issues": [item.as_dict() for item in self.issues],
            "error_count": sum(item.severity == "error" for item in self.issues),
            "warning_count": sum(item.severity == "warning" for item in self.issues),
        }


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _count(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def validate_financial_stage(
    conn: sqlite3.Connection,
    *,
    policy: FinancialIngestionPolicy,
    snapshot_key: str | None = None,
) -> FinancialStageValidationReport:
    """Validate Stage 4B without changing database or external state."""

    assert_database_identity(conn)
    snapshot = (
        conn.execute(
            "SELECT * FROM fact_financial_ingestion_snapshot WHERE snapshot_key = ?",
            (snapshot_key,),
        ).fetchone()
        if snapshot_key
        else conn.execute(
            """
            SELECT * FROM fact_financial_ingestion_snapshot
            WHERE extraction_asof_date <= ?
            ORDER BY extraction_asof_date DESC, created_at_utc DESC
            LIMIT 1
            """,
            (policy.as_of_date,),
        ).fetchone()
    )
    issues: list[FinancialStageIssue] = []
    if snapshot is None:
        return FinancialStageValidationReport(
            passed=False,
            validated_at_utc=utc_now(),
            as_of_date=policy.as_of_date,
            snapshot_key="",
            policy_version=policy.version,
            policy_sha256=policy.checksum,
            counts=database_counts(conn),
            resolution_status_counts={},
            feature_quality_counts={},
            rank_ready_count=0,
            valuation_ready_count=0,
            issues=(
                FinancialStageIssue(
                    severity="error",
                    issue_code="FINANCIAL_SNAPSHOT_MISSING",
                    message="No Stage 4B financial snapshot exists.",
                ),
            ),
        )
    snapshot_key = str(snapshot["snapshot_key"])
    if str(snapshot["policy_sha256"]) != policy.checksum:
        issues.append(
            FinancialStageIssue(
                "error",
                "POLICY_SHA_MISMATCH",
                "Financial snapshot does not match the active Stage 4B policy.",
            )
        )
    if str(snapshot["extraction_asof_date"]) != policy.as_of_date:
        issues.append(
            FinancialStageIssue(
                "error",
                "SNAPSHOT_ASOF_MISMATCH",
                "Financial snapshot as-of date differs from the governed policy.",
            )
        )
    counts = database_counts(conn)
    expected = {
        "dim_financial_profile_resolution": 154,
        "feature_financial_statement": 134,
        "fact_financial_data_coverage": 134,
    }
    for table, expected_count in expected.items():
        actual = counts[table]
        if actual != expected_count:
            issues.append(
                FinancialStageIssue(
                    "error",
                    "TABLE_CENSUS_MISMATCH",
                    f"{table} expected {expected_count} rows, found {actual}.",
                    details={"table": table, "expected": expected_count, "actual": actual},
                )
            )
    resolution_counts = Counter(
        str(row["resolution_status"])
        for row in conn.execute(
            "SELECT resolution_status FROM dim_financial_profile_resolution"
        ).fetchall()
    )
    if resolution_counts["resolved_standard"] != 140:
        issues.append(
            FinancialStageIssue(
                "error",
                "STANDARD_ROUTE_CENSUS_MISMATCH",
                f"Expected 140 standard profiles, found {resolution_counts['resolved_standard']}.",
            )
        )
    exception_count = _count(
        conn,
        "SELECT COUNT(*) FROM dim_financial_profile_resolution WHERE resolution_status <> 'resolved_standard'",
    )
    if exception_count != 14:
        issues.append(
            FinancialStageIssue(
                "error",
                "EXCEPTION_ROUTE_CENSUS_MISMATCH",
                f"Expected 14 exception routes, found {exception_count}.",
            )
        )
    expected_exception_state = {
        ticker: str(resolution["resolution_status"])
        for ticker, resolution in policy.payload["exception_resolutions"].items()
    }
    exception_tickers = tuple(sorted(expected_exception_state))
    placeholders = ",".join("?" for _ in exception_tickers)
    actual_exception_state = {
        str(row["ticker"]): str(row["resolution_status"])
        for row in conn.execute(
            f"""
            SELECT ticker, resolution_status
            FROM dim_financial_profile_resolution
            WHERE ticker IN ({placeholders})
            """,
            exception_tickers,
        ).fetchall()
    }
    for ticker, expected_status in expected_exception_state.items():
        if actual_exception_state.get(ticker) != expected_status:
            issues.append(
                FinancialStageIssue(
                    "error",
                    "GOVERNED_EXCEPTION_STATE_MISMATCH",
                    f"{ticker} expected {expected_status}, found {actual_exception_state.get(ticker, 'missing')}.",
                    ticker=ticker,
                )
            )
    structured_route_without_facts = [
        str(row["ticker"])
        for row in conn.execute(
            """
            SELECT r.ticker
            FROM dim_financial_profile_resolution AS r
            WHERE r.resolution_status IN ('resolved_inline_xbrl', 'resolved_xbrl_instance')
              AND NOT EXISTS (
                  SELECT 1
                  FROM fact_sec_xbrl_fact_raw AS x
                  WHERE x.security_id = r.security_id
                    AND x.snapshot_key = ?
                    AND x.source_id = 'sec_inline_xbrl_fallback'
                    AND x.accession_number = r.evidence_accession
                    AND x.quality_status = 'usable'
              )
            ORDER BY r.ticker
            """,
            (snapshot_key,),
        ).fetchall()
    ]
    if structured_route_without_facts:
        issues.append(
            FinancialStageIssue(
                "error",
                "STRUCTURED_FALLBACK_EMPTY",
                "A resolved structured fallback has no usable mapped facts.",
                details={"tickers": structured_route_without_facts},
            )
        )
    stored_error_issues = [
        dict(row)
        for row in conn.execute(
            """
            SELECT ticker, stage, issue_code, message
            FROM fact_financial_normalization_issue
            WHERE snapshot_key = ? AND severity = 'error'
            ORDER BY stage, ticker, issue_code
            """,
            (snapshot_key,),
        ).fetchall()
    ]
    if stored_error_issues:
        issues.append(
            FinancialStageIssue(
                "error",
                "STORED_FINANCIAL_PIPELINE_ERRORS",
                "The ingestion or normalization audit table contains error-severity rows.",
                details={"issues": stored_error_issues},
            )
        )
    future_filings = _count(
        conn,
        """
        SELECT COUNT(*)
        FROM fact_sec_filing AS f
        JOIN dim_issuer_reporting_profile AS p ON p.security_id = f.security_id
        WHERE f.snapshot_key = ? AND substr(f.accepted_at, 1, 10) > p.source_cutoff_date
        """,
        (snapshot_key,),
    )
    future_raw = _count(
        conn,
        """
        SELECT COUNT(*)
        FROM fact_sec_xbrl_fact_raw AS x
        JOIN dim_issuer_reporting_profile AS p ON p.security_id = x.security_id
        WHERE x.snapshot_key = ? AND x.accepted_at <> ''
          AND substr(x.accepted_at, 1, 10) > p.source_cutoff_date
        """,
        (snapshot_key,),
    )
    future_canonical = _count(
        conn,
        """
        SELECT COUNT(*)
        FROM fact_financial_statement_canonical AS c
        JOIN dim_issuer_reporting_profile AS p ON p.security_id = c.security_id
        WHERE c.snapshot_key = ? AND substr(c.accepted_at, 1, 10) > p.source_cutoff_date
        """,
        (snapshot_key,),
    )
    if future_filings or future_raw or future_canonical:
        issues.append(
            FinancialStageIssue(
                "error",
                "NO_LOOKAHEAD_VIOLATION",
                "One or more filings/facts appear after the issuer-specific cutoff.",
                details={
                    "future_filings": future_filings,
                    "future_raw": future_raw,
                    "future_canonical": future_canonical,
                },
            )
        )
    canonical_without_acceptance = _count(
        conn,
        """
        SELECT COUNT(*)
        FROM fact_financial_statement_canonical
        WHERE snapshot_key = ? AND (accepted_at = '' OR accepted_at < period_end)
        """,
        (snapshot_key,),
    )
    canonical_from_quarantine = _count(
        conn,
        """
        SELECT COUNT(*)
        FROM fact_financial_statement_canonical AS c
        JOIN fact_sec_xbrl_fact_raw AS x
          ON x.source_observation_id = c.source_observation_id
        WHERE c.snapshot_key = ? AND x.quality_status <> 'usable'
        """,
        (snapshot_key,),
    )
    if canonical_without_acceptance or canonical_from_quarantine:
        issues.append(
            FinancialStageIssue(
                "error",
                "CANONICAL_LINEAGE_INVALID",
                "Canonical facts violate acceptance or raw-quality requirements.",
                details={
                    "without_valid_acceptance": canonical_without_acceptance,
                    "from_quarantined_raw": canonical_from_quarantine,
                },
            )
        )
    broken_payload_lineage = _count(
        conn,
        """
        SELECT COUNT(*)
        FROM fact_sec_xbrl_fact_raw
        WHERE snapshot_key = ? AND (
            length(payload_sha256) <> 64 OR evidence_url = '' OR source_observation_id = ''
        )
        """,
        (snapshot_key,),
    )
    if broken_payload_lineage:
        issues.append(
            FinancialStageIssue(
                "error",
                "RAW_LINEAGE_INCOMPLETE",
                f"{broken_payload_lineage} raw facts lack payload or evidence lineage.",
            )
        )
    invalid_currency = _count(
        conn,
        """
        SELECT COUNT(*)
        FROM fact_financial_statement_canonical AS c
        JOIN dim_financial_profile_resolution AS r ON r.security_id = c.security_id
        WHERE c.snapshot_key = ?
          AND c.reported_currency <> 'SHARES'
          AND c.reported_currency <> r.effective_reporting_currency
        """,
        (snapshot_key,),
    )
    if invalid_currency:
        issues.append(
            FinancialStageIssue(
                "error",
                "REPORTING_CURRENCY_MISMATCH",
                f"{invalid_currency} canonical monetary facts use a non-governed reporting currency.",
            )
        )
    missing_usd_conversion = _count(
        conn,
        """
        SELECT COUNT(*)
        FROM fact_financial_statement_canonical
        WHERE snapshot_key = ? AND quality_status = 'usable'
          AND reported_currency <> 'SHARES' AND usd_value IS NULL
        """,
        (snapshot_key,),
    )
    if missing_usd_conversion:
        issues.append(
            FinancialStageIssue(
                "error",
                "USABLE_FACT_WITHOUT_USD_VALUE",
                f"{missing_usd_conversion} usable monetary facts have no USD value.",
            )
        )
    feature_quality = Counter(
        str(row["quality_status"])
        for row in conn.execute(
            "SELECT quality_status FROM feature_financial_statement WHERE asof_date = ?",
            (policy.as_of_date,),
        ).fetchall()
    )
    missing_current_visibility = _count(
        conn,
        """
        SELECT COUNT(*)
        FROM dim_issuer_reporting_profile AS p
        LEFT JOIN feature_financial_statement AS f
          ON f.security_id = p.security_id AND f.asof_date = ?
        LEFT JOIN fact_financial_data_coverage AS c
          ON c.security_id = p.security_id AND c.audit_asof_date = ?
        WHERE p.role_type = 'current_universe'
          AND (f.security_id IS NULL OR c.security_id IS NULL)
        """,
        (policy.as_of_date, policy.as_of_date),
    )
    if missing_current_visibility:
        issues.append(
            FinancialStageIssue(
                "error",
                "CURRENT_PROFILE_NOT_VISIBLE",
                f"{missing_current_visibility} current profiles lack a feature or coverage row.",
            )
        )
    unsafe_foreign_valuations = _count(
        conn,
        """
        SELECT COUNT(*)
        FROM feature_financial_statement AS f
        JOIN dim_issuer_reporting_profile AS p ON p.security_id = f.security_id
        JOIN dim_financial_profile_resolution AS r ON r.security_id = f.security_id
        WHERE f.asof_date = ? AND f.market_cap_usd IS NOT NULL
          AND p.filing_regime <> 'domestic_sec'
          AND r.ingestion_route <> 'domestic_interim_companyfacts'
        """,
        (policy.as_of_date,),
    )
    if unsafe_foreign_valuations:
        issues.append(
            FinancialStageIssue(
                "error",
                "UNSAFE_FOREIGN_SHARE_BASIS",
                f"{unsafe_foreign_valuations} foreign listings received valuation without a ratio contract.",
            )
        )
    invalid_valuation_denominators = _count(
        conn,
        """
        SELECT COUNT(*)
        FROM feature_financial_statement
        WHERE asof_date = ? AND (
            (enterprise_value_to_ebitda IS NOT NULL AND (ebitda_ttm_usd IS NULL OR ebitda_ttm_usd <= 0))
            OR (enterprise_value_to_ebit IS NOT NULL
                AND (operating_income_ttm_usd IS NULL OR operating_income_ttm_usd <= 0))
            OR (enterprise_value_to_gross_profit IS NOT NULL
                AND (gross_profit_ttm_usd IS NULL OR gross_profit_ttm_usd <= 0))
            OR (free_cash_flow_yield IS NOT NULL AND (market_cap_usd IS NULL OR market_cap_usd <= 0))
        )
        """,
        (policy.as_of_date,),
    )
    if invalid_valuation_denominators:
        issues.append(
            FinancialStageIssue(
                "error",
                "INVALID_VALUATION_DENOMINATOR",
                f"{invalid_valuation_denominators} valuation ratios violate positive-denominator policy.",
            )
        )
    foreign_features = _count(
        conn,
        """
        SELECT COUNT(*)
        FROM feature_financial_statement AS f
        JOIN dim_issuer_reporting_profile AS p ON p.security_id = f.security_id
        WHERE f.asof_date = ? AND p.filing_regime <> 'domestic_sec'
        """,
        (policy.as_of_date,),
    )
    if foreign_features:
        issues.append(
            FinancialStageIssue(
                "warning",
                "FOREIGN_VALUATION_RATIO_CONTRACT_PENDING",
                f"{foreign_features} foreign/current profiles keep valuation null until security ratios are governed.",
            )
        )
    missing_source_profiles = [
        str(row["ticker"])
        for row in conn.execute(
            """
            SELECT ticker
            FROM dim_financial_profile_resolution
            WHERE resolution_status = 'missing_required_source'
            ORDER BY ticker
            """
        ).fetchall()
    ]
    if missing_source_profiles:
        issues.append(
            FinancialStageIssue(
                "warning",
                "MISSING_REQUIRED_SOURCE_EXPLICIT",
                "Profiles without cutoff-valid structured facts remain explicitly blocked.",
                details={"tickers": missing_source_profiles},
            )
        )
    rank_ready = _count(
        conn,
        "SELECT COUNT(*) FROM fact_financial_data_coverage WHERE audit_asof_date = ? AND rank_ready = 1",
        (policy.as_of_date,),
    )
    valuation_ready = _count(
        conn,
        "SELECT COUNT(*) FROM feature_financial_statement WHERE asof_date = ? AND market_cap_usd IS NOT NULL",
        (policy.as_of_date,),
    )
    control = conn.execute("SELECT * FROM model_control_state WHERE identity_id = 1").fetchone()
    unsafe_flags = (
        control is None
        or int(control["portfolio_candidate_gate"]) != 0
        or int(control["oos_score_valid_flag"]) != 0
        or int(control["current_universe_calibration_eligible"]) != 0
        or _count(
            conn,
            "SELECT COUNT(*) FROM dim_financial_profile_resolution WHERE calibration_eligible <> 0",
        )
        != 0
    )
    if unsafe_flags:
        issues.append(
            FinancialStageIssue(
                "error",
                "PROMOTION_OR_CALIBRATION_GATE_OPEN",
                "Stage 4B changed a promotion or calibration gate.",
            )
        )
    foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        issues.append(
            FinancialStageIssue(
                "error",
                "FOREIGN_KEY_VIOLATION",
                f"SQLite reports {len(foreign_keys)} foreign-key violations.",
            )
        )
    return FinancialStageValidationReport(
        passed=not any(item.severity == "error" for item in issues),
        validated_at_utc=utc_now(),
        as_of_date=policy.as_of_date,
        snapshot_key=snapshot_key,
        policy_version=policy.version,
        policy_sha256=policy.checksum,
        counts=counts,
        resolution_status_counts=dict(sorted(resolution_counts.items())),
        feature_quality_counts=dict(sorted(feature_quality.items())),
        rank_ready_count=rank_ready,
        valuation_ready_count=valuation_ready,
        issues=tuple(issues),
    )


def write_financial_validation_reports(
    report: FinancialStageValidationReport,
    *,
    conn: sqlite3.Connection,
    report_dir: str | Path,
) -> dict[str, str]:
    target = Path(report_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    summary = atomic_write_json(target / "financial_stage_validation_summary.json", report.summary_dict())
    issue_rows = [item.as_dict() for item in report.issues]
    normalized_issues = [
        {
            "severity": item["severity"],
            "issue_code": item["issue_code"],
            "ticker": item["ticker"],
            "message": item["message"],
            "details_json": json.dumps(item["details"], sort_keys=True),
        }
        for item in issue_rows
    ]
    issues = atomic_write_csv(
        target / "financial_stage_validation_issues.csv",
        normalized_issues,
        ("severity", "issue_code", "ticker", "message", "details_json"),
    )
    exceptions = [
        dict(row)
        for row in conn.execute(
            """
            SELECT ticker, role_type, original_profile_status, ingestion_route,
                   resolution_status, canonical_eligible, effective_annual_form,
                   effective_accounting_basis, effective_taxonomy,
                   effective_reporting_currency, evidence_accession,
                   evidence_accepted_at, evidence_url, evidence_sha256,
                   resolution_reason
            FROM dim_financial_profile_resolution
            WHERE original_profile_status <> 'ready_for_ingestion'
            ORDER BY ticker
            """
        ).fetchall()
    ]
    exception_path = atomic_write_csv(
        target / "resolved_exception_queue.csv",
        exceptions,
        tuple(exceptions[0]) if exceptions else ("ticker",),
    )
    artifacts = {
        "summary": str(summary),
        "issues": str(issues),
        "exception_queue": str(exception_path),
    }
    manifest = atomic_write_json(
        target / "artifact_manifest.json",
        {
            "stage": "stage4b_validation",
            "snapshot_key": report.snapshot_key,
            "artifacts": [
                {
                    "artifact": name,
                    "path": path,
                    "sha256": _sha256_path(Path(path)),
                    "byte_size": Path(path).stat().st_size,
                }
                for name, path in sorted(artifacts.items())
            ],
        },
    )
    artifacts["artifact_manifest"] = str(manifest)
    return artifacts

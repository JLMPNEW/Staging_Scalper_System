#!/usr/bin/env python3
"""Validate the final med-devices production output surface.

This is the blocking QA gate for routine refreshes. It checks that the scored
as-of date is internally consistent across the database, top-level CSV, dated
review pack, required portfolio columns, and FDA mapping governance output.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core import analyst_review as analyst_review_core  # noqa: E402
from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CONFIG_KEY = "med_devices_production_qa"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate med-devices production outputs.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default="")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--fail-on-warnings", action="store_true")
    return parser.parse_args()


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    # atomic publish (tmp + os.replace): a crash mid-write must never leave a
    # truncated artifact at the final name (same pattern as scripts 16/76)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent, delete=False) as handle:
        tmp_name = handle.name
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp_name, path)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        tmp_name = handle.name
        handle.write(json.dumps(payload, indent=2, sort_keys=True, default=str))
    os.replace(tmp_name, path)


def read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        return [], []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,)).fetchone())


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not table_exists(conn, table):
        return set()
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def count_rows(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(query, params).fetchone()
    return int(row[0] or 0) if row else 0


def scalar(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> str:
    row = conn.execute(query, params).fetchone()
    return str(row[0] or "") if row else ""


def parse_iso_date(raw: str) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:10]).date()
    except ValueError:
        return None


def truthy_flag(raw: Any) -> bool:
    text = str(raw or "").strip().lower()
    if text in {"1", "1.0", "true", "yes", "y", "t"}:
        return True
    if text in {"", "0", "0.0", "false", "no", "n", "f", "none", "null", "nan"}:
        return False
    try:
        return float(text) != 0.0
    except ValueError:
        return False


def values_equivalent(left: Any, right: Any) -> bool:
    """Compare two CSV cell values, tolerating float formatting differences only."""
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    if left_text == right_text:
        return True
    try:
        left_num = float(left_text)
        right_num = float(right_text)
    except (TypeError, ValueError):
        return False
    return abs(left_num - right_num) <= 1e-9 * max(1.0, abs(left_num), abs(right_num))


def files_identical(left: Path, right: Path) -> bool:
    return left.exists() and right.exists() and left.read_bytes() == right.read_bytes()


def latest_score_asof(conn: sqlite3.Connection) -> str:
    value = scalar(conn, "SELECT MAX(asof_date) FROM med_device_daily_scores")
    if not value:
        raise RuntimeError("No med_device_daily_scores rows available.")
    return value


def add_check(
    checks: list[dict[str, Any]],
    *,
    check_id: str,
    severity: str,
    passed: bool,
    details: str,
    observed: Any = "",
    expected: Any = "",
) -> None:
    checks.append(
        {
            "check_id": check_id,
            "severity": severity,
            "status": "PASS" if passed else "FAIL",
            "observed": observed,
            "expected": expected,
            "details": details,
        }
    )


def freshness_date(conn: sqlite3.Connection, table: str, column: str, asof: str) -> str:
    if not table_exists(conn, table):
        return ""
    # bounded at the validated asof: future-dated rows (a look-ahead symptom) must
    # not satisfy freshness, and historical re-validation stays meaningful
    return scalar(conn, f"SELECT MAX({column}) FROM {table} WHERE {column} <= ?", (asof,))


def count_future_rows(conn: sqlite3.Connection, table: str, column: str, asof: str) -> int | None:
    if not table_exists(conn, table):
        return None
    return count_rows(conn, f"SELECT COUNT(*) FROM {table} WHERE {column} > ?", (asof,))


def count_fda_mapping_critical(path: Path) -> int:
    _, rows = read_csv_rows(path)
    count = 0
    for row in rows:
        severity = str(row.get("severity") or row.get("issue_severity") or "").strip().lower()
        if severity == "critical":
            count += 1
    return count


def qa_artifact_age_days(path: Path, *, reference: date) -> tuple[int | None, str]:
    """Return (age_days, basis) for a QA publisher artifact, or (None, "missing").

    Prefers the artifact's own max asof_date column (publish-time as-of; script 79's
    validation CSV names it generated_asof) and falls back to the file's UTC mtime when
    no asof is parseable. Age is measured against the validated asof so historical
    re-validation stays meaningful; an artifact newer than the validated asof yields a
    negative age and passes any non-negative threshold.
    """
    if not path.exists():
        return None, "missing"
    _, rows = read_csv_rows(path)
    asof_dates: list[date] = []
    for row in rows:
        parsed = parse_iso_date(str(row.get("asof_date") or row.get("generated_asof") or ""))
        if parsed is not None:
            asof_dates.append(parsed)
    if asof_dates:
        return (reference - max(asof_dates)).days, "asof"
    mtime_date = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).date()
    return (reference - mtime_date).days, "mtime"


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(cfg_get(config, f"{CONFIG_KEY}.output_dir", "../output/med_devices_reports/production_qa"), base_dir=base_dir)
    )
    report_dir = resolve_path(cfg_get(config, "paths.output_dir", "../output/med_devices_reports"), base_dir=base_dir)
    review_base_dir = resolve_path(cfg_get(config, "scoring.review_pack_dir", "../output/med_devices_reports/score_review_pack"), base_dir=base_dir)
    analyst_review_dir = resolve_path(
        cfg_get(config, "med_devices_analyst_review.output_dir", "../output/med_devices_reports/analyst_review"),
        base_dir=base_dir,
    )
    required_columns = [str(item) for item in cfg_get(config, "med_devices_refresh_pipeline.required_output_columns", []) or []]
    min_score_pct = float(cfg_get(config, f"{CONFIG_KEY}.min_score_rows_pct_of_active", 0.95) or 0.95)
    min_portfolio_candidates = int(cfg_get(config, f"{CONFIG_KEY}.min_portfolio_candidates", 1) or 0)
    max_staleness_days = int(cfg_get(config, f"{CONFIG_KEY}.max_feature_staleness_days", 7) or 7)
    fail_on_warnings = bool(args.fail_on_warnings or cfg_get(config, f"{CONFIG_KEY}.fail_on_warnings", False))
    expiration_warning_days = int(cfg_get(config, "med_devices_analyst_review.expiration_warning_days", 14) or 14)
    decision_path = resolve_path(
        cfg_get(config, "med_devices_analyst_review.decisions_csv", "data/analyst_review_decisions.csv"),
        base_dir=base_dir,
    )
    decision_log_path = resolve_path(
        cfg_get(config, "med_devices_analyst_review.decision_change_log_csv", "data/analyst_review_decision_log.csv"),
        base_dir=base_dir,
    )
    analyst_review_core.ensure_decision_file(decision_path)
    allowed_decisions = analyst_review_core.parse_allowed_decisions(
        cfg_get(config, "med_devices_analyst_review.allowed_decisions", None)
    )
    analyst_decisions, decision_issues = analyst_review_core.load_analyst_review_decisions(
        decision_path,
        allowed_decisions=allowed_decisions,
    )

    if not db_path.exists():
        # connecting would CREATE an empty DB file at the configured path and then fail confusingly
        raise FileNotFoundError(f"med_devices database not found: {db_path}")
    with sqlite3.connect(db_path, timeout=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0) or 30.0)) as conn:
        conn.row_factory = sqlite3.Row
        asof = str(args.asof or "").strip() or latest_score_asof(conn)
        asof_date = parse_iso_date(asof)
        if asof_date is None:
            raise ValueError(f"Invalid asof date: {asof!r}")

        checks: list[dict[str, Any]] = []
        lifecycle_rows = analyst_review_core.decision_lifecycle_rows(
            analyst_decisions,
            asof=asof_date,
            warning_days=expiration_warning_days,
        )
        expiring_decision_count = sum(1 for row in lifecycle_rows if row.get("expiration_status") == "expires_soon")
        expired_decision_count = sum(1 for row in lifecycle_rows if row.get("expiration_status") == "expired")
        score_rows = count_rows(conn, "SELECT COUNT(*) FROM med_device_daily_scores WHERE asof_date = ?", (asof,))
        active_rows = count_rows(conn, "SELECT COUNT(*) FROM dim_company WHERE is_active = 1")
        min_historical_members = int(cfg_get(config, "med_devices_universe.min_historical_membership_tickers", 0) or 0)
        historical_member_rows = count_rows(
            conn,
            """
            SELECT COUNT(DISTINCT m.ticker)
            FROM dim_universe_membership m
            JOIN dim_company c ON c.company_id = m.company_id
            WHERE m.model_family = 'med_devices'
              AND m.membership_basis = 'calibration_only_historical_delisted'
              AND m.membership_status = 'historical'
              AND COALESCE(m.point_in_time_flag, 0) = 1
              AND COALESCE(c.is_active, 0) = 0
            """,
        )
        add_check(
            checks,
            check_id="historical_membership_min_loaded",
            severity="WARNING",
            passed=historical_member_rows >= min_historical_members,
            observed=historical_member_rows,
            expected=f">={min_historical_members}",
            details="Calibration-only historical/delisted med-device members should be loaded before survivorship-bias calibration.",
        )
        active_historical_members = count_rows(
            conn,
            """
            SELECT COUNT(DISTINCT m.ticker)
            FROM dim_universe_membership m
            JOIN dim_company c ON c.company_id = m.company_id
            WHERE m.model_family = 'med_devices'
              AND m.membership_basis = 'calibration_only_historical_delisted'
              AND m.membership_status = 'historical'
              AND COALESCE(m.point_in_time_flag, 0) = 1
              AND COALESCE(c.is_active, 0) = 1
            """,
        )
        add_check(
            checks,
            check_id="historical_membership_not_active",
            severity="CRITICAL",
            passed=active_historical_members == 0,
            observed=active_historical_members,
            expected=0,
            details="Calibration-only historical/delisted members must not remain active in dim_company.",
        )
        min_score_rows = int(active_rows * min_score_pct)
        add_check(
            checks,
            check_id="score_rows_active_coverage",
            severity="CRITICAL",
            passed=score_rows >= min_score_rows and score_rows > 0,
            observed=f"{score_rows}/{active_rows}",
            expected=f">={min_score_rows}",
            details="Scored row count must cover the active investable universe.",
        )
        portfolio_candidates = count_rows(
            conn,
            "SELECT COUNT(*) FROM med_device_daily_scores WHERE asof_date = ? AND portfolio_candidate_gate = 1",
            (asof,),
        )
        add_check(
            checks,
            check_id="portfolio_candidate_count",
            severity="CRITICAL",
            passed=portfolio_candidates >= min_portfolio_candidates,
            observed=portfolio_candidates,
            expected=f">={min_portfolio_candidates}",
            details="Portfolio optimizer must receive at least the configured minimum candidate set.",
        )
        inactive_outputs = count_rows(
            conn,
            """
            SELECT COUNT(*)
            FROM med_device_daily_scores s
            JOIN dim_company c ON c.company_id = s.company_id
            WHERE s.asof_date = ? AND COALESCE(c.is_active, 0) = 0
            """,
            (asof,),
        )
        add_check(
            checks,
            check_id="no_inactive_tickers_scored",
            severity="CRITICAL",
            passed=inactive_outputs == 0,
            observed=inactive_outputs,
            expected=0,
            details="Inactive/delisted tickers cannot remain in the production score surface.",
        )
        explicit_asof = str(args.asof or "").strip()
        max_score_asof = scalar(conn, "SELECT MAX(asof_date) FROM med_device_daily_scores")
        add_check(
            checks,
            check_id="explicit_asof_matches_latest_score_asof",
            severity="WARNING",
            passed=not explicit_asof or explicit_asof == max_score_asof,
            observed=f"asof={asof};max_asof_date={max_score_asof}",
            expected="explicit --asof equals MAX(asof_date)",
            details="An explicit --asof behind the latest scored date validates a stale surface; confirm this is intentional.",
        )
        # Historical re-validation must not rewrite the *_latest QA surface or demand
        # that *_latest artifacts (which track the newest asof) match the old date.
        validating_latest = not explicit_asof or explicit_asof == max_score_asof
        composite_score_min = float(cfg_get(config, f"{CONFIG_KEY}.composite_score_min", 0.0) or 0.0)
        composite_score_max = float(cfg_get(config, f"{CONFIG_KEY}.composite_score_max", 100.0) or 100.0)
        if "composite_score" in table_columns(conn, "med_device_daily_scores"):
            out_of_range_scores = count_rows(
                conn,
                """
                SELECT COUNT(*)
                FROM med_device_daily_scores
                WHERE asof_date = ?
                  AND composite_score IS NOT NULL
                  AND (composite_score < ? OR composite_score > ?)
                """,
                (asof, composite_score_min, composite_score_max),
            )
            add_check(
                checks,
                check_id="composite_score_within_scale",
                severity="WARNING",
                passed=out_of_range_scores == 0,
                observed=out_of_range_scores,
                expected=0,
                details=f"composite_score must stay within the published score scale [{composite_score_min}, {composite_score_max}].",
            )
        overrides_enabled = bool(cfg_get(config, "med_devices_analyst_review.enable_portfolio_overrides", False))
        # While overrides are disabled, analyst_portfolio_override_applied is 0 for every
        # row, so override-conditioned checks are structurally vacuous. Publish them as
        # INFO so QA consumers do not credit them as live CRITICAL coverage; they flip to
        # CRITICAL automatically if the config flag is ever turned on.
        override_check_severity = "CRITICAL" if overrides_enabled else "INFO"
        add_check(
            checks,
            check_id="analyst_portfolio_overrides_not_enabled",
            severity="CRITICAL",
            passed=not overrides_enabled,
            observed=overrides_enabled,
            expected=False,
            details=(
                "med_devices_analyst_review.enable_portfolio_overrides must stay false: the override pathway is "
                "shadow-only and scoring never applies overrides, so enabling it would be silently ignored."
            ),
        )
        critical_decision_issues = [
            issue for issue in decision_issues if str(issue.get("severity") or "").upper() == "CRITICAL"
        ]
        add_check(
            checks,
            check_id="analyst_review_decision_file_valid",
            severity="CRITICAL",
            passed=not critical_decision_issues,
            observed=len(critical_decision_issues),
            expected=0,
            details=f"Analyst review decisions must use the governed schema and allowed decisions: {decision_path}",
        )
        lifecycle_latest_csv = analyst_review_dir / "med_device_analyst_review_decision_status_latest.csv"
        add_check(
            checks,
            check_id="analyst_review_lifecycle_status_exists",
            severity="CRITICAL",
            passed=lifecycle_latest_csv.exists(),
            observed=lifecycle_latest_csv if lifecycle_latest_csv.exists() else "",
            expected="exists",
            details="Analyst workflow Phase 2 requires the latest decision lifecycle status artifact.",
        )
        add_check(
            checks,
            check_id="analyst_review_decision_change_log_exists",
            severity="CRITICAL",
            passed=decision_log_path.exists(),
            observed=decision_log_path if decision_log_path.exists() else "",
            expected="exists",
            details="Analyst workflow Phase 2 requires the persistent decision change log.",
        )
        add_check(
            checks,
            check_id="analyst_review_expiring_decisions",
            severity="WARNING",
            passed=expiring_decision_count == 0,
            observed=expiring_decision_count,
            expected=0,
            details=f"Active analyst decisions expiring within {expiration_warning_days} days should be reviewed.",
        )
        add_check(
            checks,
            check_id="analyst_review_expired_decisions",
            severity="WARNING",
            passed=expired_decision_count == 0,
            observed=expired_decision_count,
            expected=0,
            details="Expired active analyst decisions remain in the source decision file and should be renewed or deactivated.",
        )
        score_columns = table_columns(conn, "med_device_daily_scores")
        analyst_columns = {
            "analyst_review_decision",
            "analyst_review_reason",
            "analyst_review_owner",
            "analyst_reviewed_at",
            "analyst_review_expires_at",
            "analyst_portfolio_override_applied",
        }
        add_check(
            checks,
            check_id="analyst_review_score_columns_present",
            severity="CRITICAL",
            passed=analyst_columns.issubset(score_columns),
            observed=",".join(sorted(analyst_columns.difference(score_columns))),
            expected="all analyst review audit columns",
            details="Score table must persist analyst review decision audit columns.",
        )
        if analyst_columns.issubset(score_columns):
            expired_approval_overrides = count_rows(
                conn,
                """
                SELECT COUNT(*)
                FROM med_device_daily_scores
                WHERE asof_date = ?
                  AND COALESCE(analyst_portfolio_override_applied, 0) = 1
                  AND LOWER(COALESCE(analyst_review_decision, '')) = 'approve'
                  AND COALESCE(analyst_review_expires_at, '') <> ''
                  AND SUBSTR(analyst_review_expires_at, 1, 10) < ?
                """,
                (asof, asof),
            )
            add_check(
                checks,
                check_id="no_expired_analyst_approval_applied",
                severity=override_check_severity,
                passed=expired_approval_overrides == 0,
                observed=expired_approval_overrides,
                expected=0,
                details=(
                    "Expired analyst approvals cannot affect portfolio eligibility. "
                    "Override pathway shadow-only: analyst_portfolio_override_applied is always 0 while "
                    "enable_portfolio_overrides is false, so this check is vacuous until overrides are implemented."
                ),
            )
            expired_applied_decisions = count_rows(
                conn,
                """
                SELECT COUNT(*)
                FROM med_device_daily_scores
                WHERE asof_date = ?
                  AND COALESCE(TRIM(analyst_review_decision), '') <> ''
                  AND COALESCE(analyst_review_expires_at, '') <> ''
                  AND SUBSTR(analyst_review_expires_at, 1, 10) < ?
                """,
                (asof, asof),
            )
            add_check(
                checks,
                check_id="no_expired_analyst_decision_applied",
                severity="CRITICAL",
                passed=expired_applied_decisions == 0,
                observed=expired_applied_decisions,
                expected=0,
                details="Expired analyst decisions cannot be applied to the production score surface.",
            )
            future_applied_decisions = count_rows(
                conn,
                # date-truncate: reviewed_at may carry a timestamp. Intentional same-day semantics:
                # scoring applies a decision only when reviewed_at < asof (is_reviewed_before_asof),
                # i.e. reviewed_at >= asof means a decision never applies on its own review date.
                # The validator tolerates the same-day boundary (only strictly-future dates fail),
                # so it is strictly looser than scoring and can never false-fail.
                """
                SELECT COUNT(*)
                FROM med_device_daily_scores
                WHERE asof_date = ?
                  AND COALESCE(TRIM(analyst_review_decision), '') <> ''
                  AND COALESCE(analyst_reviewed_at, '') <> ''
                  AND SUBSTR(analyst_reviewed_at, 1, 10) > ?
                """,
                (asof, asof),
            )
            add_check(
                checks,
                check_id="no_future_analyst_decision_applied",
                severity="CRITICAL",
                passed=future_applied_decisions == 0,
                observed=future_applied_decisions,
                expected=0,
                details="Analyst decisions cannot be applied before their reviewed_at date.",
            )
            hard_gate_bypass_overrides = count_rows(
                conn,
                """
                SELECT COUNT(*)
                FROM med_device_daily_scores s
                JOIN dim_company c ON c.company_id = s.company_id
                WHERE s.asof_date = ?
                  AND COALESCE(s.analyst_portfolio_override_applied, 0) = 1
                  AND (
                    COALESCE(c.is_active, 0) = 0
                    OR COALESCE(s.hard_red_flag, 0) = 1
                    OR s.classification IN ('manual_review_regulatory_risk', 'avoid_confirmed_regulatory_risk')
                  )
                """,
                (asof,),
            )
            add_check(
                checks,
                check_id="no_analyst_override_hard_gate_bypass",
                severity=override_check_severity,
                passed=hard_gate_bypass_overrides == 0,
                observed=hard_gate_bypass_overrides,
                expected=0,
                details=(
                    "Analyst approvals cannot bypass inactive, hard-red, or confirmed regulatory-risk blocks. "
                    "Override pathway shadow-only: analyst_portfolio_override_applied is always 0 while "
                    "enable_portfolio_overrides is false, so this check is vacuous until overrides are implemented."
                ),
            )
            undocumented_overrides = count_rows(
                conn,
                """
                SELECT COUNT(*)
                FROM med_device_daily_scores
                WHERE asof_date = ?
                  AND COALESCE(analyst_portfolio_override_applied, 0) = 1
                  AND (
                    COALESCE(TRIM(analyst_review_reason), '') = ''
                    OR COALESCE(TRIM(analyst_review_owner), '') = ''
                  )
                """,
                (asof,),
            )
            add_check(
                checks,
                check_id="analyst_override_has_owner_and_reason",
                severity=override_check_severity,
                passed=undocumented_overrides == 0,
                observed=undocumented_overrides,
                expected=0,
                details=(
                    "Any applied analyst portfolio override must have a reason and owner. "
                    "Override pathway shadow-only: analyst_portfolio_override_applied is always 0 while "
                    "enable_portfolio_overrides is false, so this check is vacuous until overrides are implemented."
                ),
            )
            analyst_negative_decisions_in_candidates = count_rows(
                conn,
                """
                SELECT COUNT(*)
                FROM med_device_daily_scores
                WHERE asof_date = ?
                  AND COALESCE(portfolio_candidate_gate, 0) = 1
                  AND LOWER(COALESCE(analyst_review_decision, '')) IN ('reject', 'data_fix_needed')
                """,
                (asof,),
            )
            add_check(
                checks,
                check_id="analyst_negative_decisions_excluded_from_portfolio_candidates",
                severity="CRITICAL",
                passed=analyst_negative_decisions_in_candidates == 0,
                observed=analyst_negative_decisions_in_candidates,
                expected=0,
                details="Active analyst reject/data_fix_needed decisions must force portfolio_candidate_gate=0.",
            )

        feature_tables = {
            "prices": ("fact_price_ohlcv", "bar_date"),
            "market_snapshots": ("fact_market_snapshot", "asof_date"),
            "financial": ("feature_financial_valuation", "asof_date"),
            "fda": ("feature_fda_product_risk", "asof_date"),
            "reimbursement": ("feature_reimbursement", "asof_date"),
            "technical": ("feature_technical_entry", "asof_date"),
            "borrow": ("feature_borrow_risk", "asof_date"),
            "short_interest": ("feature_short_interest", "asof_date"),
            "institutional_flow": ("feature_institutional_flow", "asof_date"),
            "insider_activity": ("feature_insider_activity", "asof_date"),
        }
        for name, (table, column) in feature_tables.items():
            latest = freshness_date(conn, table, column, asof)
            latest_date = parse_iso_date(latest)
            age = (asof_date - latest_date).days if latest_date else None
            add_check(
                checks,
                check_id=f"feature_freshness_{name}",
                severity="CRITICAL",
                passed=latest_date is not None and age is not None and 0 <= age <= max_staleness_days,
                observed=latest,
                expected=f"within {max_staleness_days} days of {asof}",
                details=f"Latest {name} feature/fact date at or before {asof} must be current enough for production scoring.",
            )
            if validating_latest:
                # rows dated after the latest scored asof are a look-ahead/PIT-integrity
                # symptom; skipped on historical re-validation where newer rows are expected
                future_rows = count_future_rows(conn, table, column, asof)
                add_check(
                    checks,
                    check_id=f"feature_no_future_rows_{name}",
                    severity="WARNING",
                    passed=future_rows is not None and future_rows == 0,
                    observed="missing_table" if future_rows is None else future_rows,
                    expected=0,
                    details=f"No {name} rows may be dated after the validated asof (point-in-time integrity).",
                )

    # --- analyst review published artifacts (queue + lifecycle status, dated and latest) ---
    queue_dated_csv = analyst_review_dir / f"med_device_analyst_review_queue_{asof}.csv"
    queue_latest_csv = analyst_review_dir / "med_device_analyst_review_queue_latest.csv"
    queue_dated_md = analyst_review_dir / f"med_device_analyst_review_queue_{asof}.md"
    queue_latest_md = analyst_review_dir / "med_device_analyst_review_queue_latest.md"
    lifecycle_dated_csv = analyst_review_dir / f"med_device_analyst_review_decision_status_{asof}.csv"
    lifecycle_latest_csv = analyst_review_dir / "med_device_analyst_review_decision_status_latest.csv"
    for check_id, path in {
        "analyst_review_queue_dated_exists": queue_dated_csv,
        "analyst_review_queue_latest_exists": queue_latest_csv,
        "analyst_review_lifecycle_status_dated_exists": lifecycle_dated_csv,
    }.items():
        add_check(
            checks,
            check_id=check_id,
            severity="CRITICAL",
            passed=path.exists(),
            observed=str(path),
            expected="exists",
            details="Analyst workflow Phase 2 requires the published review queue and decision status artifacts.",
        )
    for check_id, path in {
        "analyst_review_queue_dated_md_exists": queue_dated_md,
        "analyst_review_queue_latest_md_exists": queue_latest_md,
    }.items():
        add_check(
            checks,
            check_id=check_id,
            severity="WARNING",
            passed=path.exists(),
            observed=str(path),
            expected="exists",
            details="Analyst review queue markdown companion should be published with the CSV.",
        )
    if validating_latest:
        # *_latest artifacts track the newest asof; only reconcile them when this run
        # validates that asof (historical re-validation checks the dated pair only)
        add_check(
            checks,
            check_id="analyst_review_queue_latest_matches_dated",
            severity="CRITICAL",
            passed=files_identical(queue_latest_csv, queue_dated_csv),
            observed="identical" if files_identical(queue_latest_csv, queue_dated_csv) else "differs_or_missing",
            expected="identical",
            details="med_device_analyst_review_queue_latest.csv must be content-identical to the dated queue for the validated asof.",
        )
        add_check(
            checks,
            check_id="analyst_review_lifecycle_status_latest_matches_dated",
            severity="CRITICAL",
            passed=files_identical(lifecycle_latest_csv, lifecycle_dated_csv),
            observed="identical" if files_identical(lifecycle_latest_csv, lifecycle_dated_csv) else "differs_or_missing",
            expected="identical",
            details="Decision status *_latest must be content-identical to the dated status for the validated asof.",
        )
    queue_fields, queue_rows = read_csv_rows(queue_dated_csv)
    add_check(
        checks,
        check_id="analyst_review_queue_parseable",
        severity="CRITICAL",
        passed=bool(queue_fields),
        observed=str(queue_dated_csv),
        expected="header row present",
        details="Published analyst review queue must exist and contain a parseable CSV header.",
    )
    if queue_fields:
        queue_bad_asof = [
            str(row.get("ticker") or "") for row in queue_rows if str(row.get("asof_date") or "") != asof
        ]
        add_check(
            checks,
            check_id="analyst_review_queue_asof_consistency",
            severity="CRITICAL",
            passed=not queue_bad_asof,
            observed=len(queue_bad_asof),
            expected=0,
            details="Every analyst review queue row must carry the validated as-of date (stale queue detection).",
        )
        # recompute each queue row's decision state from the governed decision file so a
        # queue that disagrees with the decisions on disk (stale or wiped) fails loudly
        queue_decision_mismatches: list[str] = []
        for row in queue_rows:
            ticker = str(row.get("ticker") or "").strip()
            cohort = str(row.get("calibration_cohort") or "").strip()
            categories = {
                analyst_review_core.normalize_key(part)
                for part in str(row.get("review_categories") or "").split(";")
                if str(part or "").strip()
            }
            active = analyst_review_core.effective_decision(
                analyst_decisions,
                ticker=ticker,
                cohort=cohort,
                review_categories=categories,
                asof=asof_date,
            )
            expired = analyst_review_core.latest_expired_decision(
                analyst_decisions,
                ticker=ticker,
                cohort=cohort,
                review_categories=categories,
                asof=asof_date,
            )
            status = str(row.get("review_status") or "").strip()
            recorded = analyst_review_core.normalize_key(row.get("analyst_decision"))
            if status in {"decided", "decision_expires_soon"}:
                consistent = active is not None and active.decision == recorded
            elif status == "expired_decision_needs_review":
                consistent = active is None and expired is not None and expired.decision == recorded
            else:
                consistent = active is None and expired is None
            if not consistent:
                queue_decision_mismatches.append(f"{ticker}:{status or 'open'}")
        add_check(
            checks,
            check_id="analyst_review_queue_decisions_match_decision_file",
            severity="CRITICAL",
            passed=not queue_decision_mismatches,
            observed=",".join(queue_decision_mismatches[:25]),
            expected=0,
            details="Published queue decision states must match decisions recomputed from the governed decision file.",
        )
    lifecycle_pub_fields, lifecycle_pub_rows = read_csv_rows(lifecycle_dated_csv)
    published_fingerprints = {str(row.get("decision_fingerprint") or "") for row in lifecycle_pub_rows}
    expected_fingerprints = {str(row.get("decision_fingerprint") or "") for row in lifecycle_rows}
    add_check(
        checks,
        check_id="analyst_review_lifecycle_status_matches_decision_file",
        severity="CRITICAL",
        passed=(
            bool(lifecycle_pub_fields)
            and len(lifecycle_pub_rows) == len(lifecycle_rows)
            and published_fingerprints == expected_fingerprints
        ),
        observed=f"published={len(lifecycle_pub_rows)};expected={len(lifecycle_rows)}",
        expected="published status rows equal decisions recomputed from the decision file",
        details="Published decision lifecycle status must parse and reconcile with the governed decision file.",
    )
    # guard against a wiped/recreated decision file: ensure_decision_file() silently
    # rebuilds an empty file, which would otherwise pass every decision check vacuously
    _, decision_log_rows = read_csv_rows(decision_log_path)
    logged_created_events = sum(
        1 for row in decision_log_rows if str(row.get("event_type") or "").strip() == "decision_created"
    )
    add_check(
        checks,
        check_id="analyst_review_decision_file_not_collapsed",
        severity="CRITICAL",
        passed=bool(analyst_decisions) or logged_created_events == 0,
        observed=f"decisions={len(analyst_decisions)};logged_created_events={logged_created_events}",
        expected="decision file non-empty whenever the change log records created decisions",
        details=(
            "analyst_review_decisions.csv has zero rows while the decision change log records prior "
            "decision_created events: the decision file was likely deleted and silently recreated."
        ),
    )

    top_level_csv = report_dir / "med_device_daily_composite_scores.csv"
    review_dir = review_base_dir / asof
    dated_daily_csv = review_dir / "med_device_daily_composite_scores.csv"
    dated_portfolio_candidate_csv = review_dir / "med_device_score_review_portfolio_candidates.csv"
    for check_id, path in {
        "top_level_daily_csv_exists": top_level_csv,
        "dated_daily_csv_exists": dated_daily_csv,
        "dated_portfolio_candidate_csv_exists": dated_portfolio_candidate_csv,
    }.items():
        add_check(
            checks,
            check_id=check_id,
            severity="CRITICAL",
            passed=path.exists(),
            observed=str(path),
            expected="exists",
            details="Required production score CSV must exist.",
        )
    parsed_csvs: dict[str, tuple[list[str], list[dict[str, str]]]] = {}
    for label, path in {
        "top_level": top_level_csv,
        "dated_review": dated_daily_csv,
        "dated_portfolio_candidates": dated_portfolio_candidate_csv,
    }.items():
        fields, rows = read_csv_rows(path)
        parsed_csvs[label] = (fields, rows)
        # an existing-but-empty/headerless CSV must fail the gate explicitly instead of
        # silently skipping the content checks below (missing files fail *_exists above)
        add_check(
            checks,
            check_id=f"{label}_csv_parseable",
            severity="CRITICAL",
            passed=bool(fields),
            observed=str(path),
            expected="header row present",
            details="Production CSV must exist and contain a parseable header.",
        )
        if not fields:
            continue
        missing_columns = [column for column in required_columns if column not in fields]
        add_check(
            checks,
            check_id=f"{label}_required_columns",
            severity="CRITICAL",
            passed=not missing_columns,
            observed=",".join(missing_columns),
            expected="all configured required columns present",
            details="Production CSV contract must stay stable for portfolio integration.",
        )
        bad_asof = [row.get("ticker", "") for row in rows if str(row.get("asof_date") or "") != asof]
        add_check(
            checks,
            check_id=f"{label}_asof_consistency",
            severity="CRITICAL",
            passed=not bad_asof and bool(rows),
            observed=len(bad_asof),
            expected=0,
            details="Every row in the output CSV must match the requested as-of date.",
        )
        tickers = [str(row.get("ticker") or "").strip().upper() for row in rows if str(row.get("ticker") or "").strip()]
        duplicate_count = len(tickers) - len(set(tickers))
        add_check(
            checks,
            check_id=f"{label}_duplicate_tickers",
            severity="CRITICAL",
            passed=duplicate_count == 0,
            observed=duplicate_count,
            expected=0,
            details="Production CSV cannot contain duplicate ticker rows.",
        )

    top_level_fields, top_level_rows = parsed_csvs["top_level"]
    dated_daily_fields, dated_daily_rows = parsed_csvs["dated_review"]
    _, dated_candidate_rows = parsed_csvs["dated_portfolio_candidates"]
    # keyed on file existence, not row counts: a truncated/empty CSV must fail the
    # reconciliation below instead of silently skipping it (missing files already
    # fail the *_exists checks above)
    if dated_daily_csv.exists() and dated_portfolio_candidate_csv.exists():
        daily_candidate_tickers = {
            str(row.get("ticker") or "").strip().upper()
            for row in dated_daily_rows
            if str(row.get("ticker") or "").strip() and truthy_flag(row.get("portfolio_candidate_gate"))
        }
        candidate_file_tickers = {
            str(row.get("ticker") or "").strip().upper()
            for row in dated_candidate_rows
            if str(row.get("ticker") or "").strip()
        }
        candidate_mismatch = sorted(
            (daily_candidate_tickers - candidate_file_tickers)
            | (candidate_file_tickers - daily_candidate_tickers)
        )
        add_check(
            checks,
            check_id="portfolio_candidate_csv_matches_daily_gate",
            severity="CRITICAL",
            # bool(dated_daily_rows): an empty daily CSV must fail rather than
            # reconciling two empty ticker sets as equal
            passed=not candidate_mismatch and bool(dated_daily_rows),
            observed=",".join(candidate_mismatch[:25]) or f"daily_rows={len(dated_daily_rows)}",
            expected="dated portfolio-candidate CSV tickers exactly equal daily portfolio_candidate_gate=1 tickers",
            details="Portfolio-layer handoff CSV must be an exact materialization of the production candidate gate.",
        )
        candidate_bad_gate = sorted(
            str(row.get("ticker") or "").strip().upper()
            for row in dated_candidate_rows
            if not truthy_flag(row.get("portfolio_candidate_gate"))
        )
        add_check(
            checks,
            check_id="portfolio_candidate_csv_all_rows_gate_true",
            severity="CRITICAL",
            passed=not candidate_bad_gate,
            observed=",".join(candidate_bad_gate[:25]),
            expected=0,
            details="Every row in med_device_score_review_portfolio_candidates.csv must have portfolio_candidate_gate=1.",
        )
        candidate_negative_decisions = sorted(
            str(row.get("ticker") or "").strip().upper()
            for row in dated_candidate_rows
            if str(row.get("analyst_review_decision") or "").strip().lower() in {"reject", "data_fix_needed"}
        )
        add_check(
            checks,
            check_id="portfolio_candidate_csv_excludes_negative_analyst_decisions",
            severity="CRITICAL",
            passed=not candidate_negative_decisions,
            observed=",".join(candidate_negative_decisions[:25]),
            expected=0,
            details="Portfolio candidate CSV must not include active analyst reject/data_fix_needed decisions.",
        )

    # --- rolling top-level composite vs dated pack copy reconciliation (same asof) ---
    # both files are written by the same refresh (scripts 13 and 16); schema or value
    # drift between them means one side was rerun/edited out of band. Skipped on
    # historical re-validation: the rolling CSV legitimately tracks the newest asof.
    if validating_latest and top_level_fields and dated_daily_fields:
        dated_field_set = set(dated_daily_fields)
        # expected relationship: dated copy = rolling columns minus company_id plus
        # pack-only enrichment fields appended by script 16
        missing_from_dated = [
            column for column in top_level_fields if column != "company_id" and column not in dated_field_set
        ]
        add_check(
            checks,
            check_id="daily_composite_header_relationship",
            severity="CRITICAL",
            passed=not missing_from_dated,
            observed=",".join(missing_from_dated[:25]),
            expected="every rolling composite column except company_id present in the dated pack copy",
            details="Dated review-pack composite must carry all rolling composite columns (pack-only enrichment columns may be added).",
        )
        top_by_ticker = {
            str(row.get("ticker") or "").strip().upper(): row
            for row in top_level_rows
            if str(row.get("ticker") or "").strip()
        }
        dated_by_ticker = {
            str(row.get("ticker") or "").strip().upper(): row
            for row in dated_daily_rows
            if str(row.get("ticker") or "").strip()
        }
        composite_ticker_mismatch = sorted(set(top_by_ticker).symmetric_difference(dated_by_ticker))
        add_check(
            checks,
            check_id="daily_composite_rolling_matches_dated_tickers",
            severity="CRITICAL",
            passed=(
                not composite_ticker_mismatch
                and len(top_level_rows) == len(dated_daily_rows)
                and bool(top_level_rows)
            ),
            observed=",".join(composite_ticker_mismatch[:25])
            or f"rolling_rows={len(top_level_rows)};dated_rows={len(dated_daily_rows)}",
            expected="identical non-empty ticker sets and row counts",
            details="Rolling composite CSV and the dated pack copy must cover the same tickers for the validated asof.",
        )
        shared_required_columns = [
            column for column in required_columns if column in top_level_fields and column in dated_field_set
        ]
        composite_value_mismatches: list[str] = []
        for ticker in sorted(set(top_by_ticker) & set(dated_by_ticker)):
            top_row = top_by_ticker[ticker]
            dated_row = dated_by_ticker[ticker]
            for column in shared_required_columns:
                if not values_equivalent(top_row.get(column), dated_row.get(column)):
                    composite_value_mismatches.append(f"{ticker}:{column}")
        add_check(
            checks,
            check_id="daily_composite_rolling_matches_dated_values",
            severity="CRITICAL",
            passed=not composite_value_mismatches,
            observed=",".join(composite_value_mismatches[:25]),
            expected=0,
            details="Rolling composite CSV and the dated pack copy must agree on every required output column per ticker.",
        )

    required_review_files = [str(item) for item in cfg_get(config, f"{CONFIG_KEY}.review_pack_required_files", []) or []]
    for name in required_review_files:
        path = review_dir / name
        add_check(
            checks,
            check_id=f"review_pack_file_{Path(name).stem}",
            severity="CRITICAL",
            passed=path.exists(),
            observed=str(path),
            expected="exists",
            details="Dated review pack file must be present.",
        )

    # --- QA publisher artifact freshness (scripts 07 and 19; default-on pipeline steps) ---
    # WARNING severity: a missing/stale artifact means the QA publisher step silently
    # stopped running with the refresh cadence, not that the score surface is wrong.
    max_qa_artifact_age_days = int(cfg_get(config, f"{CONFIG_KEY}.max_qa_artifact_age_days", 7) or 7)
    financial_baseline_qa_csv = resolve_path(
        cfg_get(
            config,
            "financial_baseline_qa.summary_csv",
            "../output/med_devices_reports/med_device_financial_baseline_qa_summary.csv",
        ),
        base_dir=base_dir,
    )
    share_count_qa_csv = review_dir / "med_device_share_count_qa.csv"
    for check_id, qa_path in {
        "financial_baseline_qa_artifact_fresh": financial_baseline_qa_csv,
        "share_count_qa_artifact_fresh": share_count_qa_csv,
    }.items():
        age_days, age_basis = qa_artifact_age_days(qa_path, reference=asof_date)
        add_check(
            checks,
            check_id=check_id,
            severity="WARNING",
            passed=age_days is not None and age_days <= max_qa_artifact_age_days,
            observed="missing" if age_days is None else f"{age_basis}_age_days={age_days}",
            expected=f"exists and within {max_qa_artifact_age_days} days of {asof}",
            details=f"QA publisher artifact must exist and stay current with the refresh cadence: {qa_path}",
        )

    # --- FDA product-family shadow artifacts (scripts 78 and 79) ---
    # WARNING severity, same rationale as the QA publisher block above: a missing/stale
    # artifact means the shadow-feature publisher silently fell off the refresh cadence,
    # not that the score surface is wrong. Script 78 is a PROTECTED_CRITICAL stage_5
    # step whose dated review pack must exist for the validated asof; script 79 is an
    # optional stage_9 step (it runs AFTER this gate), and its rolling validation CSV is
    # the sole artifact gating any future promotion (promotion_min_oos_observations), so
    # a silently failing/stale 79 would otherwise go unnoticed indefinitely. Because 79
    # runs after 72 in the same refresh, the freshness threshold is satisfied by the
    # previous refresh's output; only a 79 that stopped running for more than
    # max_qa_artifact_age_days trips this check.
    product_family_review_dir = resolve_path(
        cfg_get(
            config,
            "fda_product_family_review.output_dir",
            "../output/med_devices_reports/fda_product_family_review",
        ),
        base_dir=base_dir,
    )
    product_family_dated_dir = product_family_review_dir / asof
    product_family_summary_csv = product_family_dated_dir / "med_device_fda_product_family_review_summary.csv"
    product_family_validation_csv = resolve_path(
        cfg_get(
            config,
            "fda_product_family_review.shadow_score.validation_output_csv",
            "../output/med_devices_reports/calibration/med_device_abt_fda_product_family_oos_validation.csv",
        ),
        base_dir=base_dir,
    )
    for check_id, artifact_path in {
        "fda_product_family_review_summary_fresh": product_family_summary_csv,
        "fda_product_family_oos_validation_fresh": product_family_validation_csv,
    }.items():
        age_days, age_basis = qa_artifact_age_days(artifact_path, reference=asof_date)
        add_check(
            checks,
            check_id=check_id,
            severity="WARNING",
            passed=age_days is not None and age_days <= max_qa_artifact_age_days,
            observed="missing" if age_days is None else f"{age_basis}_age_days={age_days}",
            expected=f"exists and within {max_qa_artifact_age_days} days of {asof}",
            details=f"FDA product-family shadow artifact must exist and stay current with the refresh cadence: {artifact_path}",
        )
    # the dated review pack is published file-by-file (atomic per file, summary is not
    # last), so a fresh summary alone does not prove the pack completed; the expected
    # filename set mirrors what script 78 writes
    product_family_expected_files = [
        "med_device_fda_mdr_review.csv",
        "med_device_fda_class_i_recall_review.csv",
        "med_device_fda_product_family_qa.csv",
        "med_device_fda_product_family_exceptions.csv",
        "med_device_fda_product_family_review_summary.csv",
        "med_device_fda_product_family_governance_issues.csv",
    ]
    product_family_missing_files = [
        name for name in product_family_expected_files if not (product_family_dated_dir / name).exists()
    ]
    add_check(
        checks,
        check_id="fda_product_family_review_pack_complete",
        severity="WARNING",
        passed=not product_family_missing_files,
        observed=",".join(product_family_missing_files),
        expected="all script-78 review pack files present",
        details=f"Dated FDA product-family review pack must contain every published file: {product_family_dated_dir}",
    )

    fda_queue = report_dir / "fda_mapping_review_queue.csv"
    critical_fda = count_fda_mapping_critical(fda_queue) if fda_queue.exists() else 0
    add_check(
        checks,
        check_id="fda_mapping_governance_critical",
        severity="CRITICAL",
        passed=fda_queue.exists() and critical_fda == 0,
        observed=critical_fda if fda_queue.exists() else "missing_queue",
        expected=0,
        details="FDA mapping governance queue must have no critical issues.",
    )

    critical_failures = [row for row in checks if row["severity"] == "CRITICAL" and row["status"] == "FAIL"]
    warning_failures = [row for row in checks if row["severity"] == "WARNING" and row["status"] == "FAIL"]
    status = "FAIL" if critical_failures or (fail_on_warnings and warning_failures) else "PASS"
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"med_device_production_qa_{asof}.csv"
    json_path = output_dir / f"med_device_production_qa_{asof}.json"
    latest_csv = output_dir / "med_device_production_qa_latest.csv"
    latest_json = output_dir / "med_device_production_qa_latest.json"
    fields = ["check_id", "severity", "status", "observed", "expected", "details"]
    summary = {
        "asof": asof,
        "status": status,
        "critical_failure_count": len(critical_failures),
        "warning_failure_count": len(warning_failures),
        "check_count": len(checks),
        "output_csv": str(csv_path),
        "output_json": str(json_path),
        "latest_written": validating_latest,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "checks": checks,
    }
    # dated pair first, then the *_latest pair, so a partial run can never publish a
    # latest artifact that is newer than its dated counterpart
    write_csv(csv_path, checks, fields)
    write_json(json_path, summary)
    if validating_latest:
        write_csv(latest_csv, checks, fields)
        write_json(latest_json, summary)
    else:
        print(
            f"production_qa_latest_skipped=1 asof={asof} max_asof_date={max_score_asof} "
            "reason=historical_revalidation_must_not_rewrite_latest"
        )
    print(
        f"production_qa_status={status} asof={asof} checks={len(checks)} "
        f"critical_failures={len(critical_failures)} output={csv_path}"
    )
    return 1 if status == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())

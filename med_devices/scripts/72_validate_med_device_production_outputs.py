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
import sqlite3
import sys
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
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


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


def freshness_date(conn: sqlite3.Connection, table: str, column: str) -> str:
    if not table_exists(conn, table):
        return ""
    return scalar(conn, f"SELECT MAX({column}) FROM {table}")


def count_fda_mapping_critical(path: Path) -> int:
    _, rows = read_csv_rows(path)
    count = 0
    for row in rows:
        severity = str(row.get("severity") or row.get("issue_severity") or "").strip().lower()
        if severity == "critical":
            count += 1
    return count


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
                  AND analyst_review_expires_at < ?
                """,
                (asof, asof),
            )
            add_check(
                checks,
                check_id="no_expired_analyst_approval_applied",
                severity="CRITICAL",
                passed=expired_approval_overrides == 0,
                observed=expired_approval_overrides,
                expected=0,
                details="Expired analyst approvals cannot affect portfolio eligibility.",
            )
            expired_applied_decisions = count_rows(
                conn,
                """
                SELECT COUNT(*)
                FROM med_device_daily_scores
                WHERE asof_date = ?
                  AND COALESCE(TRIM(analyst_review_decision), '') <> ''
                  AND COALESCE(analyst_review_expires_at, '') <> ''
                  AND analyst_review_expires_at < ?
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
                severity="CRITICAL",
                passed=hard_gate_bypass_overrides == 0,
                observed=hard_gate_bypass_overrides,
                expected=0,
                details="Analyst approvals cannot bypass inactive, hard-red, or confirmed regulatory-risk blocks.",
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
                severity="CRITICAL",
                passed=undocumented_overrides == 0,
                observed=undocumented_overrides,
                expected=0,
                details="Any applied analyst portfolio override must have a reason and owner.",
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
            latest = freshness_date(conn, table, column)
            latest_date = parse_iso_date(latest)
            age = (asof_date - latest_date).days if latest_date else None
            add_check(
                checks,
                check_id=f"feature_freshness_{name}",
                severity="CRITICAL",
                passed=latest_date is not None and age is not None and age <= max_staleness_days,
                observed=latest,
                expected=f"within {max_staleness_days} days of {asof}",
                details=f"Latest {name} feature/fact date must be current enough for production scoring.",
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
    for label, path in {
        "top_level": top_level_csv,
        "dated_review": dated_daily_csv,
        "dated_portfolio_candidates": dated_portfolio_candidate_csv,
    }.items():
        fields, rows = read_csv_rows(path)
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
    write_csv(csv_path, checks, fields)
    write_csv(latest_csv, checks, fields)
    summary = {
        "asof": asof,
        "status": status,
        "critical_failure_count": len(critical_failures),
        "warning_failure_count": len(warning_failures),
        "check_count": len(checks),
        "output_csv": str(csv_path),
        "output_json": str(json_path),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "checks": checks,
    }
    json_path.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str), encoding="utf-8")
    latest_json.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(
        f"production_qa_status={status} asof={asof} checks={len(checks)} "
        f"critical_failures={len(critical_failures)} output={csv_path}"
    )
    return 1 if status == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())

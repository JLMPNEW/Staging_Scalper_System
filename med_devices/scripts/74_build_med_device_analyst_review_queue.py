#!/usr/bin/env python3
"""Build an analyst review queue from the latest med-devices score surface."""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core import analyst_review as analyst_review_core  # noqa: E402
from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CONFIG_KEY = "med_devices_analyst_review"

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build med-devices analyst review queue.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default="")
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def float_or_zero(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def latest_score_asof(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT MAX(asof_date) FROM med_device_daily_scores").fetchone()
    value = str(row[0] or "") if row else ""
    if not value:
        raise RuntimeError("No med_device_daily_scores rows available.")
    return value


def load_rows(conn: sqlite3.Connection, asof: str) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT s.*, c.ticker AS ticker, c.company_name AS company_name
        FROM med_device_daily_scores s
        JOIN dim_company c ON c.company_id = s.company_id
        WHERE s.asof_date = ?
        ORDER BY s.portfolio_candidate_score DESC, s.composite_score DESC, c.ticker
        """,
        (asof,),
    ).fetchall()
    return [dict(row) for row in rows]


def review_categories(row: dict[str, Any], *, high_score_threshold: float, include_portfolio_candidates: bool) -> list[str]:
    return analyst_review_core.review_categories_for_item(
        row,
        high_score_threshold=high_score_threshold,
        include_portfolio_candidates=include_portfolio_candidates,
    )


def priority_for(row: dict[str, Any], categories: list[str], *, priority_score_threshold: float) -> str:
    portfolio_candidate_score = row.get("portfolio_candidate_score")
    score = float_or_zero(
        portfolio_candidate_score
        if portfolio_candidate_score is not None and str(portfolio_candidate_score).strip() != ""
        else row.get("composite_score")
    )
    if "hard_red_flag" in categories or "avoid_confirmed_regulatory_risk" in categories:
        return "P1"
    if "manual_review_regulatory_risk" in categories:
        return "P1"
    if "high_score_blocked" in categories and score >= priority_score_threshold:
        return "P1"
    if {"high_score_blocked", "tier1_safety_failed", "unknown_reimbursement"}.intersection(categories):
        return "P2"
    return "P3"


def reason_for(row: dict[str, Any]) -> str:
    parts = [
        str(row.get("portfolio_candidate_reason") or ""),
        str(row.get("classification_reason") or ""),
        str(row.get("tier1_safety_reason") or ""),
        str(row.get("safe_core_reason") or ""),
        str(row.get("hard_red_flag_reasons") or ""),
    ]
    return ";".join(part for part in parts if part)


def write_markdown(path: Path, rows: list[dict[str, Any]], *, asof: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Med-Devices Analyst Review Queue - {asof}",
        "",
        f"Generated at UTC: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "| Priority | Ticker | Cohort | Score | Status | Decision | Categories | Reason |",
        "|---|---:|---|---:|---|---|---|---|",
    ]
    if not rows:
        lines.append("| - | - | - | - | - | - | - | No open review items |")
    for row in rows[:100]:
        reason = str(row.get("review_reason") or "").replace("|", "/")
        categories = str(row.get("review_categories") or "").replace("|", "/")
        status = str(row.get("review_status") or "").replace("|", "/")
        decision = str(row.get("analyst_decision") or "").replace("|", "/")
        lines.append(
            f"| {row.get('priority')} | {row.get('ticker')} | {row.get('calibration_cohort')} | "
            f"{float_or_zero(row.get('portfolio_candidate_score')):.2f} | {status} | {decision} | "
            f"{categories} | {reason} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(cfg_get(config, f"{CONFIG_KEY}.output_dir", "../output/med_devices_reports/analyst_review"), base_dir=base_dir)
    )
    high_score_threshold = float(cfg_get(config, f"{CONFIG_KEY}.high_score_threshold", 70.0) or 70.0)
    priority_score_threshold = float(cfg_get(config, f"{CONFIG_KEY}.priority_score_threshold", 75.0) or 75.0)
    include_portfolio_candidates = bool(cfg_get(config, f"{CONFIG_KEY}.include_portfolio_candidates", False))
    expiration_warning_days = int(cfg_get(config, f"{CONFIG_KEY}.expiration_warning_days", 14) or 14)
    decision_path = resolve_path(
        cfg_get(config, f"{CONFIG_KEY}.decisions_csv", "data/analyst_review_decisions.csv"),
        base_dir=base_dir,
    )
    decision_log_path = resolve_path(
        cfg_get(config, f"{CONFIG_KEY}.decision_change_log_csv", "data/analyst_review_decision_log.csv"),
        base_dir=base_dir,
    )
    analyst_review_core.ensure_decision_file(decision_path)
    allowed_decisions = analyst_review_core.parse_allowed_decisions(
        cfg_get(config, f"{CONFIG_KEY}.allowed_decisions", None)
    )
    analyst_decisions, decision_issues = analyst_review_core.load_analyst_review_decisions(
        decision_path,
        allowed_decisions=allowed_decisions,
    )
    critical_decision_issues = [
        issue for issue in decision_issues if str(issue.get("severity") or "").upper() == "CRITICAL"
    ]
    if critical_decision_issues:
        details = "; ".join(
            f"row={issue.get('row_number')} ticker={issue.get('ticker')} issue={issue.get('issue_type')}"
            for issue in critical_decision_issues[:10]
        )
        raise ValueError(f"Invalid analyst review decision file {decision_path}: {details}")

    with sqlite3.connect(db_path, timeout=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0) or 30.0)) as conn:
        asof = str(args.asof or "").strip() or latest_score_asof(conn)
        score_rows = load_rows(conn, asof)
    asof_date = analyst_review_core.parse_date(asof) or analyst_review_core.utc_today()
    lifecycle_rows = analyst_review_core.decision_lifecycle_rows(
        analyst_decisions,
        asof=asof_date,
        warning_days=expiration_warning_days,
    )
    logged_change_count = analyst_review_core.append_decision_change_log(
        decision_log_path,
        analyst_decisions,
        asof=asof_date,
    )
    review_rows: list[dict[str, Any]] = []
    for row in score_rows:
        categories = review_categories(
            row,
            high_score_threshold=high_score_threshold,
            include_portfolio_candidates=include_portfolio_candidates,
        )
        if not categories:
            continue
        category_set = set(categories)
        active_decision = analyst_review_core.effective_decision(
            analyst_decisions,
            ticker=str(row.get("ticker") or ""),
            cohort=str(row.get("calibration_cohort") or ""),
            review_categories=category_set,
            asof=asof_date,
        )
        expired_decision = analyst_review_core.latest_expired_decision(
            analyst_decisions,
            ticker=str(row.get("ticker") or ""),
            cohort=str(row.get("calibration_cohort") or ""),
            review_categories=category_set,
            asof=asof_date,
        )
        active_expiration_status = ""
        active_days_to_expiration: int | None = None
        active_needs_review = 0
        if active_decision is not None:
            active_expiration_status, active_days_to_expiration, active_needs_review = (
                analyst_review_core.decision_expiration_status(
                    active_decision,
                    asof=asof_date,
                    warning_days=expiration_warning_days,
                )
            )
        queue_status = (
            "decision_expires_soon"
            if active_decision and active_expiration_status == "expires_soon"
            else "decided"
            if active_decision
            else "expired_decision_needs_review"
            if expired_decision
            else "open"
        )
        decision_for_display = active_decision or expired_decision
        review_rows.append(
            {
                "asof_date": asof,
                "priority": priority_for(row, categories, priority_score_threshold=priority_score_threshold),
                "review_status": queue_status,
                "review_owner": decision_for_display.review_owner if decision_for_display else "",
                "analyst_decision": decision_for_display.decision if decision_for_display else "",
                "analyst_decision_reason": decision_for_display.decision_reason if decision_for_display else "",
                "analyst_reviewed_at": decision_for_display.reviewed_at if decision_for_display else "",
                "analyst_review_expires_at": decision_for_display.expires_at if decision_for_display else "",
                "analyst_expiration_status": active_expiration_status if active_decision else "expired" if expired_decision else "",
                "analyst_days_to_expiration": (
                    "" if active_days_to_expiration is None else active_days_to_expiration
                ),
                "analyst_review_due": active_needs_review if active_decision else int(bool(expired_decision)),
                "analyst_override_allowed": (
                    int(decision_for_display.allow_portfolio_candidate_override) if decision_for_display else 0
                ),
                "analyst_source_reference": decision_for_display.source_reference if decision_for_display else "",
                "analyst_notes": "",
                "ticker": row.get("ticker", ""),
                "company_name": row.get("company_name", ""),
                "calibration_cohort": row.get("calibration_cohort", ""),
                "calibration_eligible_flag": row.get("calibration_eligible_flag", ""),
                "rank": row.get("rank", ""),
                "portfolio_candidate_gate": row.get("portfolio_candidate_gate", ""),
                "portfolio_candidate_score": row.get("portfolio_candidate_score", ""),
                "composite_score": row.get("composite_score", ""),
                "safe_core_score": row.get("safe_core_score", ""),
                "ic_tilted_composite_score": row.get("ic_tilted_composite_score", ""),
                "classification": row.get("classification", ""),
                "portfolio_candidate_reason": row.get("portfolio_candidate_reason", ""),
                "classification_reason": row.get("classification_reason", ""),
                "tier1_safety_status": row.get("tier1_safety_status", ""),
                "tier1_safety_reason": row.get("tier1_safety_reason", ""),
                "fda_review_state": row.get("fda_review_state", ""),
                "hard_red_flag": row.get("hard_red_flag", ""),
                "hard_red_flag_reasons": row.get("hard_red_flag_reasons", ""),
                "unknown_reimbursement_flag": row.get("unknown_reimbursement_flag", ""),
                "single_product_risk_flag": row.get("single_product_risk_flag", ""),
                "binary_event_risk_flag": row.get("binary_event_risk_flag", ""),
                "review_categories": ";".join(categories),
                "review_reason": reason_for(row),
            }
        )
    priority_order = {"P1": 0, "P2": 1, "P3": 2}
    review_rows.sort(
        key=lambda item: (
            priority_order.get(str(item.get("priority")), 9),
            -float_or_zero(item.get("portfolio_candidate_score")),
            str(item.get("ticker") or ""),
        )
    )
    fields = [
        "asof_date",
        "priority",
        "review_status",
        "review_owner",
        "analyst_decision",
        "analyst_decision_reason",
        "analyst_reviewed_at",
        "analyst_review_expires_at",
        "analyst_expiration_status",
        "analyst_days_to_expiration",
        "analyst_review_due",
        "analyst_override_allowed",
        "analyst_source_reference",
        "analyst_notes",
        "ticker",
        "company_name",
        "calibration_cohort",
        "calibration_eligible_flag",
        "rank",
        "portfolio_candidate_gate",
        "portfolio_candidate_score",
        "composite_score",
        "safe_core_score",
        "ic_tilted_composite_score",
        "classification",
        "portfolio_candidate_reason",
        "classification_reason",
        "tier1_safety_status",
        "tier1_safety_reason",
        "fda_review_state",
        "hard_red_flag",
        "hard_red_flag_reasons",
        "unknown_reimbursement_flag",
        "single_product_risk_flag",
        "binary_event_risk_flag",
        "review_categories",
        "review_reason",
    ]
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"med_device_analyst_review_queue_{asof}.csv"
    md_path = output_dir / f"med_device_analyst_review_queue_{asof}.md"
    latest_csv = output_dir / "med_device_analyst_review_queue_latest.csv"
    latest_md = output_dir / "med_device_analyst_review_queue_latest.md"
    lifecycle_csv = output_dir / f"med_device_analyst_review_decision_status_{asof}.csv"
    lifecycle_latest_csv = output_dir / "med_device_analyst_review_decision_status_latest.csv"
    write_csv(csv_path, review_rows, fields)
    write_csv(latest_csv, review_rows, fields)
    write_csv(lifecycle_csv, lifecycle_rows, analyst_review_core.DECISION_STATUS_FIELDNAMES)
    write_csv(lifecycle_latest_csv, lifecycle_rows, analyst_review_core.DECISION_STATUS_FIELDNAMES)
    write_markdown(md_path, review_rows, asof=asof)
    write_markdown(latest_md, review_rows, asof=asof)
    p1_count = sum(1 for row in review_rows if row["priority"] == "P1")
    review_due_count = sum(1 for row in review_rows if int(row.get("analyst_review_due") or 0) == 1)
    print(
        f"analyst_review_queue={csv_path} asof={asof} rows={len(review_rows)} p1={p1_count} "
        f"review_due={review_due_count} decision_status={lifecycle_csv} "
        f"decision_log_appended={logged_change_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

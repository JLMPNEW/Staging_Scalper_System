#!/usr/bin/env python3
"""Build an analyst review queue from the latest med-devices score surface."""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from collections import Counter
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
# Artifact names this script's directory used to contain but that no current
# script revision writes; removed on every run so stale *_latest files cannot
# pose as current state.
RETIRED_ARTIFACT_NAMES = ("med_device_p1_review_summary_latest.csv",)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build med-devices analyst review queue.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default="")
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp_path, path)


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


def priority_for(
    row: dict[str, Any],
    categories: list[str],
    *,
    priority_score_threshold: float,
    saturated_categories: set[str] | None = None,
) -> str:
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
    # Near-universal categories carry no per-ticker escalation signal, so they
    # are excluded from the P2 mapping (queue membership is unaffected).
    escalation_categories = {"high_score_blocked", "unknown_reimbursement"}
    if saturated_categories:
        escalation_categories -= saturated_categories
    if escalation_categories.intersection(categories):
        return "P2"
    return "P3"


def saturated_category_set(
    categorized_rows: list[tuple[dict[str, Any], list[str]]],
    *,
    scored_count: int,
    saturation_threshold: float,
) -> tuple[set[str], list[str]]:
    """Return categories whose incidence exceeds the saturation threshold plus warnings."""
    if scored_count <= 0:
        return set(), []
    counts: Counter[str] = Counter()
    for _, categories in categorized_rows:
        counts.update(set(categories))
    saturated: set[str] = set()
    warnings: list[str] = []
    for category, count in sorted(counts.items()):
        incidence = count / scored_count
        if incidence > saturation_threshold:
            saturated.add(category)
            warnings.append(
                f"WARNING: category '{category}' is near-universal "
                f"({count}/{scored_count} scored tickers, {incidence:.0%} > {saturation_threshold:.0%}); "
                "excluded from P2 escalation - investigate the upstream gate."
            )
    return saturated, warnings


def reason_for(row: dict[str, Any]) -> str:
    parts = [
        str(row.get("portfolio_candidate_reason") or ""),
        str(row.get("classification_reason") or ""),
        str(row.get("tier1_safety_reason") or ""),
        str(row.get("safe_core_reason") or ""),
        str(row.get("hard_red_flag_reasons") or ""),
    ]
    return ";".join(part for part in parts if part)


def write_markdown(
    path: Path,
    rows: list[dict[str, Any]],
    *,
    asof: str,
    scored_count: int,
    max_rows: int,
    warnings: list[str] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    priority_counts = Counter(str(row.get("priority") or "") for row in rows)
    status_counts = Counter(str(row.get("review_status") or "") for row in rows)
    status_summary = " ".join(f"{status}={count}" for status, count in sorted(status_counts.items())) or "none"
    lines = [
        f"# Med-Devices Analyst Review Queue - {asof}",
        "",
        f"Generated at UTC: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        f"Total queue rows: {len(rows)} of {scored_count} scored tickers | "
        f"P1={priority_counts.get('P1', 0)} P2={priority_counts.get('P2', 0)} P3={priority_counts.get('P3', 0)} | "
        f"Status: {status_summary}",
        "",
    ]
    for warning in warnings or []:
        lines.extend([warning, ""])
    lines.extend(
        [
            "| Priority | Ticker | Cohort | Score | Status | Decision | Categories | Reason |",
            "|---|---:|---|---:|---|---|---|---|",
        ]
    )
    if not rows:
        lines.append(
            f"| - | - | - | - | - | - | - | No open review items across {scored_count} scored tickers |"
        )
    for row in rows[:max_rows]:
        reason = str(row.get("review_reason") or "").replace("|", "/")
        categories = str(row.get("review_categories") or "").replace("|", "/")
        status = str(row.get("review_status") or "").replace("|", "/")
        decision = str(row.get("analyst_decision") or "").replace("|", "/")
        lines.append(
            f"| {row.get('priority')} | {row.get('ticker')} | {row.get('calibration_cohort')} | "
            f"{float_or_zero(row.get('portfolio_candidate_score')):.2f} | {status} | {decision} | "
            f"{categories} | {reason} |"
        )
    if len(rows) > max_rows:
        lines.extend(
            [
                "",
                f"Showing top {max_rows} of {len(rows)} rows (priority-sorted); "
                f"the remaining {len(rows) - max_rows} rows are in the CSV artifact.",
            ]
        )
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="") as handle:
        handle.write("\n".join(lines) + "\n")
    os.replace(tmp_path, path)


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
    markdown_max_rows = int(cfg_get(config, f"{CONFIG_KEY}.markdown_max_rows", 100) or 100)
    saturation_threshold = float(cfg_get(config, f"{CONFIG_KEY}.category_saturation_threshold", 0.5) or 0.5)
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
        max_score_asof = latest_score_asof(conn)
        asof = str(args.asof or "").strip() or max_score_asof
        score_rows = load_rows(conn, asof)
    if not score_rows:
        # Fail loud: an empty score surface means the asof is wrong or scores
        # have not been built yet. Writing artifacts here would clobber a real
        # queue with an empty one that is indistinguishable from a clean day.
        raise RuntimeError(
            f"No med_device_daily_scores rows at asof={asof} (latest scored asof={max_score_asof}); "
            "refusing to publish review-queue artifacts."
        )
    is_latest_asof = asof == max_score_asof
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
    categorized_rows: list[tuple[dict[str, Any], list[str]]] = []
    for row in score_rows:
        categories = review_categories(
            row,
            high_score_threshold=high_score_threshold,
            include_portfolio_candidates=include_portfolio_candidates,
        )
        if not categories:
            continue
        categorized_rows.append((row, categories))
    saturated_categories, saturation_warnings = saturated_category_set(
        categorized_rows,
        scored_count=len(score_rows),
        saturation_threshold=saturation_threshold,
    )
    for warning in saturation_warnings:
        print(warning)
    review_rows: list[dict[str, Any]] = []
    for row, categories in categorized_rows:
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
        active_review_cadence_status = ""
        active_days_to_review: int | None = None
        active_needs_review = 0
        if active_decision is not None:
            active_expiration_status, active_days_to_expiration, expiration_needs_review = (
                analyst_review_core.decision_expiration_status(
                    active_decision,
                    asof=asof_date,
                    warning_days=expiration_warning_days,
                )
            )
            active_review_cadence_status, active_days_to_review, cadence_needs_review = (
                analyst_review_core.decision_review_cadence_status(
                    active_decision,
                    asof=asof_date,
                    warning_days=expiration_warning_days,
                )
            )
            active_needs_review = max(expiration_needs_review, cadence_needs_review)
        queue_status = (
            "decision_expires_soon"
            if active_decision and active_expiration_status == "expires_soon"
            else "decision_review_overdue"
            if active_decision and active_review_cadence_status == "review_overdue"
            else "decision_review_due_soon"
            if active_decision and active_review_cadence_status == "review_due_soon"
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
                "priority": priority_for(
                    row,
                    categories,
                    priority_score_threshold=priority_score_threshold,
                    saturated_categories=saturated_categories,
                ),
                "review_status": queue_status,
                "review_owner": decision_for_display.review_owner if decision_for_display else "",
                "analyst_decision": decision_for_display.decision if decision_for_display else "",
                "analyst_decision_reason": decision_for_display.decision_reason if decision_for_display else "",
                "analyst_reviewed_at": decision_for_display.reviewed_at if decision_for_display else "",
                "analyst_review_expires_at": decision_for_display.expires_at if decision_for_display else "",
                "analyst_next_review_at": decision_for_display.next_review_at if decision_for_display else "",
                "analyst_expiration_status": active_expiration_status if active_decision else "expired" if expired_decision else "",
                "analyst_days_to_expiration": (
                    "" if active_days_to_expiration is None else active_days_to_expiration
                ),
                "analyst_review_cadence_status": (
                    active_review_cadence_status if active_decision else ""
                ),
                "analyst_days_to_review": (
                    "" if active_days_to_review is None else active_days_to_review
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
                "tier1_safety_policy_version": row.get("tier1_safety_policy_version", ""),
                "tier1_safety_strict_pass_flag": row.get("tier1_safety_strict_pass_flag", ""),
                "tier1_safety_balanced_pass_flag": row.get("tier1_safety_balanced_pass_flag", ""),
                "tier1_safety_tolerated_reason": row.get("tier1_safety_tolerated_reason", ""),
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
        "analyst_next_review_at",
        "analyst_expiration_status",
        "analyst_days_to_expiration",
        "analyst_review_cadence_status",
        "analyst_days_to_review",
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
    # Flag decisions whose ticker no longer maps to the score surface / queue so
    # stale decisions cannot report 'current' indefinitely after delistings.
    scored_tickers = {str(row.get("ticker") or "") for row in score_rows}
    queued_tickers = {str(row.get("ticker") or "") for row in review_rows}
    lifecycle_fields = list(analyst_review_core.DECISION_STATUS_FIELDNAMES) + [
        "in_score_surface",
        "in_current_queue",
    ]
    orphaned_decision_count = 0
    for lifecycle_row in lifecycle_rows:
        ticker = str(lifecycle_row.get("ticker") or "")
        in_score_surface = int(ticker in scored_tickers)
        lifecycle_row["in_score_surface"] = in_score_surface
        lifecycle_row["in_current_queue"] = int(ticker in queued_tickers)
        if not in_score_surface and str(lifecycle_row.get("expiration_status") or "") in (
            "current",
            "expires_soon",
            "active_no_expiration",
        ):
            lifecycle_row["expiration_status"] = "orphaned_ticker"
            lifecycle_row["needs_review"] = 1
            orphaned_decision_count += 1
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"med_device_analyst_review_queue_{asof}.csv"
    md_path = output_dir / f"med_device_analyst_review_queue_{asof}.md"
    latest_csv = output_dir / "med_device_analyst_review_queue_latest.csv"
    latest_md = output_dir / "med_device_analyst_review_queue_latest.md"
    lifecycle_csv = output_dir / f"med_device_analyst_review_decision_status_{asof}.csv"
    lifecycle_latest_csv = output_dir / "med_device_analyst_review_decision_status_latest.csv"
    # Dated artifacts first, then *_latest, so an interrupted run can never
    # leave latest newer than its dated twin.
    write_csv(csv_path, review_rows, fields)
    write_csv(lifecycle_csv, lifecycle_rows, lifecycle_fields)
    write_markdown(
        md_path,
        review_rows,
        asof=asof,
        scored_count=len(score_rows),
        max_rows=markdown_max_rows,
        warnings=saturation_warnings,
    )
    if is_latest_asof:
        write_csv(latest_csv, review_rows, fields)
        write_csv(lifecycle_latest_csv, lifecycle_rows, lifecycle_fields)
        write_markdown(
            latest_md,
            review_rows,
            asof=asof,
            scored_count=len(score_rows),
            max_rows=markdown_max_rows,
            warnings=saturation_warnings,
        )
    else:
        print(
            f"skipped_latest_artifacts=1 asof={asof} latest_scored_asof={max_score_asof} "
            "(backfill run; *_latest left untouched)"
        )
    for retired_name in RETIRED_ARTIFACT_NAMES:
        retired_path = output_dir / retired_name
        if retired_path.exists():
            retired_path.unlink()
            print(f"removed_retired_artifact={retired_path}")
    p1_count = sum(1 for row in review_rows if row["priority"] == "P1")
    review_due_count = sum(1 for row in review_rows if int(row.get("analyst_review_due") or 0) == 1)
    print(
        f"analyst_review_queue={csv_path} asof={asof} scored={len(score_rows)} rows={len(review_rows)} "
        f"p1={p1_count} review_due={review_due_count} latest_written={int(is_latest_asof)} "
        f"orphaned_decisions={orphaned_decision_count} decision_status={lifecycle_csv} "
        f"decision_log_appended={logged_change_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

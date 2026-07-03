from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from med_devices.core.text_norm import normalize_ticker


DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"
DECISION_WATCHLIST = "watchlist"
DECISION_DATA_FIX_NEEDED = "data_fix_needed"
DECISION_DEFER = "defer"
ALLOWED_ANALYST_DECISIONS = {
    DECISION_APPROVE,
    DECISION_REJECT,
    DECISION_WATCHLIST,
    DECISION_DATA_FIX_NEEDED,
    DECISION_DEFER,
}
DECISION_FIELDNAMES = [
    "ticker",
    "calibration_cohort",
    "review_category",
    "decision",
    "decision_reason",
    "review_owner",
    "reviewed_at",
    "expires_at",
    "active",
    "allow_portfolio_candidate_override",
    "max_position_weight_override",
    "source_reference",
]
DECISION_STATUS_FIELDNAMES = [
    "asof_date",
    "row_number",
    "ticker",
    "calibration_cohort",
    "review_category",
    "decision",
    "decision_reason",
    "review_owner",
    "reviewed_at",
    "expires_at",
    "active",
    "allow_portfolio_candidate_override",
    "source_reference",
    "expiration_status",
    "days_to_expiration",
    "needs_review",
    "decision_key",
    "decision_fingerprint",
]
DECISION_LOG_FIELDNAMES = [
    "logged_at_utc",
    "asof_date",
    "event_type",
    "decision_key",
    "decision_fingerprint",
    "row_number",
    "ticker",
    "calibration_cohort",
    "review_category",
    "decision",
    "decision_reason",
    "review_owner",
    "reviewed_at",
    "expires_at",
    "active",
    "allow_portfolio_candidate_override",
    "max_position_weight_override",
    "source_reference",
]
MANUAL_REVIEW_CLASSES = {
    "manual_review_regulatory_risk",
    "avoid_confirmed_regulatory_risk",
    "data_review_required",
}
DECISION_PRIORITY = {
    DECISION_REJECT: 0,
    DECISION_DATA_FIX_NEEDED: 1,
    DECISION_APPROVE: 2,
    DECISION_WATCHLIST: 3,
    DECISION_DEFER: 4,
}
# shared wildcard set for both calibration_cohort and review_category matching
GLOBAL_MATCH_VALUES = {"", "*", "all", "any"}
GLOBAL_CATEGORY_VALUES = GLOBAL_MATCH_VALUES


@dataclass(frozen=True)
class AnalystReviewDecision:
    ticker: str
    calibration_cohort: str
    review_category: str
    decision: str
    decision_reason: str
    review_owner: str
    reviewed_at: str
    expires_at: str
    active: bool
    allow_portfolio_candidate_override: bool
    max_position_weight_override: float | None
    source_reference: str
    row_number: int

    @property
    def key_ticker(self) -> str:
        return normalize_ticker(self.ticker)

    @property
    def key_cohort(self) -> str:
        return normalize_key(self.calibration_cohort)

    @property
    def key_category(self) -> str:
        return normalize_key(self.review_category)


def normalize_key(raw: object) -> str:
    return str(raw or "").strip().lower()


def parse_allowed_decisions(raw: object) -> set[str]:
    if raw is None:
        return set(ALLOWED_ANALYST_DECISIONS)
    if isinstance(raw, str):
        values = {normalize_key(item) for item in raw.split(",") if normalize_key(item)}
    elif isinstance(raw, Iterable):
        values = {normalize_key(item) for item in raw if normalize_key(item)}
    else:
        values = set()
    return values or set(ALLOWED_ANALYST_DECISIONS)


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def parse_bool(raw: object, default: bool = False) -> bool:
    text = str(raw or "").strip().lower()
    if not text:
        return default
    if text in {"1", "true", "t", "yes", "y", "active"}:
        return True
    if text in {"0", "false", "f", "no", "n", "inactive"}:
        return False
    return default


def parse_float(raw: object) -> float | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def ensure_decision_file(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=DECISION_FIELDNAMES, lineterminator="\n")
        writer.writeheader()


def read_decision_payload(path: Path, *, create_if_missing: bool = False) -> tuple[list[str], list[dict[str, str]]]:
    if create_if_missing:
        ensure_decision_file(path)
    if not path.exists():
        raise FileNotFoundError(f"Analyst review decision file not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"Analyst review decision file has no header: {path}")
        fieldnames = [str(field or "") for field in reader.fieldnames]
        rows = [{str(key): str(value or "") for key, value in row.items()} for row in reader]
        return fieldnames, rows


def read_decision_rows(path: Path, *, create_if_missing: bool = False) -> list[dict[str, str]]:
    _, rows = read_decision_payload(path, create_if_missing=create_if_missing)
    return rows


def load_analyst_review_decisions(
    path: Path,
    *,
    create_if_missing: bool = False,
    allowed_decisions: set[str] | None = None,
) -> tuple[list[AnalystReviewDecision], list[dict[str, str]]]:
    fieldnames, rows = read_decision_payload(path, create_if_missing=create_if_missing)
    allowed = set(allowed_decisions or ALLOWED_ANALYST_DECISIONS)
    decisions: list[AnalystReviewDecision] = []
    issues: list[dict[str, str]] = []
    unsupported_allowed = sorted(allowed.difference(ALLOWED_ANALYST_DECISIONS))
    if unsupported_allowed:
        issues.append(
            {
                "severity": "CRITICAL",
                "issue_type": "invalid_allowed_decisions_config",
                "row_number": "0",
                "ticker": "",
                "decision": "",
                "details": (
                    "Configured analyst-review decisions are not supported by the current workflow: "
                    + ",".join(unsupported_allowed)
                ),
            }
        )
    effective_allowed = allowed.intersection(ALLOWED_ANALYST_DECISIONS)
    missing_columns = [column for column in DECISION_FIELDNAMES if column not in fieldnames]
    if missing_columns:
        issues.append(
            {
                "severity": "CRITICAL",
                "issue_type": "missing_decision_columns",
                "row_number": "0",
                "ticker": "",
                "decision": "",
                "details": ",".join(missing_columns),
            }
        )
    for row_number, row in enumerate(rows, start=2):
        ticker = normalize_ticker(row.get("ticker"))
        decision = normalize_key(row.get("decision"))
        active = parse_bool(row.get("active"), False)
        allow_override = parse_bool(row.get("allow_portfolio_candidate_override"), False)
        if active and not decision:
            issues.append(
                {
                    "severity": "CRITICAL",
                    "issue_type": "missing_decision",
                    "row_number": str(row_number),
                    "ticker": ticker,
                    "decision": "",
                    "details": "Active analyst-review rows must include a decision.",
                }
            )
        if not ticker:
            issues.append(
                {
                    "severity": "CRITICAL",
                    "issue_type": "missing_ticker",
                    "row_number": str(row_number),
                    "ticker": "",
                    "decision": decision,
                    "details": "Decision rows must include a ticker.",
                }
            )
        if decision and decision not in effective_allowed:
            issues.append(
                {
                    "severity": "CRITICAL",
                    "issue_type": "invalid_decision",
                    "row_number": str(row_number),
                    "ticker": ticker,
                    "decision": decision,
                    "details": f"Allowed decisions: {','.join(sorted(effective_allowed))}",
                }
            )
        if active and decision in {DECISION_APPROVE, DECISION_REJECT, DECISION_DATA_FIX_NEEDED}:
            if not str(row.get("decision_reason") or "").strip():
                issues.append(
                    {
                        "severity": "CRITICAL",
                        "issue_type": "missing_decision_reason",
                        "row_number": str(row_number),
                        "ticker": ticker,
                        "decision": decision,
                        "details": "Active approve/reject/data_fix_needed decisions require decision_reason.",
                    }
                )
            if not str(row.get("review_owner") or "").strip():
                issues.append(
                    {
                        "severity": "CRITICAL",
                        "issue_type": "missing_review_owner",
                        "row_number": str(row_number),
                        "ticker": ticker,
                        "decision": decision,
                        "details": "Active approve/reject/data_fix_needed decisions require review_owner.",
                    }
                )
        if active and decision:
            reviewed_at_raw = str(row.get("reviewed_at") or "").strip()
            if not reviewed_at_raw or parse_date(reviewed_at_raw) is None:
                issues.append(
                    {
                        "severity": "CRITICAL",
                        "issue_type": "missing_or_invalid_reviewed_at",
                        "row_number": str(row_number),
                        "ticker": ticker,
                        "decision": decision,
                        "details": "Active analyst-review decisions require a parseable reviewed_at date.",
                    }
                )
        if allow_override and decision != DECISION_APPROVE:
            issues.append(
                {
                    "severity": "CRITICAL",
                    "issue_type": "invalid_override_decision",
                    "row_number": str(row_number),
                    "ticker": ticker,
                    "decision": decision,
                    "details": "Portfolio candidate overrides are only valid for approve decisions.",
                }
            )
        decisions.append(
            AnalystReviewDecision(
                ticker=ticker,
                calibration_cohort=str(row.get("calibration_cohort") or "").strip(),
                review_category=str(row.get("review_category") or "").strip(),
                decision=decision,
                decision_reason=str(row.get("decision_reason") or "").strip(),
                review_owner=str(row.get("review_owner") or "").strip(),
                reviewed_at=str(row.get("reviewed_at") or "").strip(),
                expires_at=str(row.get("expires_at") or "").strip(),
                active=active,
                allow_portfolio_candidate_override=allow_override,
                max_position_weight_override=parse_float(row.get("max_position_weight_override")),
                source_reference=str(row.get("source_reference") or "").strip(),
                row_number=row_number,
            )
        )
    return decisions, issues


def is_expired(decision: AnalystReviewDecision, *, asof: date) -> bool:
    expires_at = parse_date(decision.expires_at)
    return expires_at is not None and expires_at < asof


def is_reviewed_after_asof(decision: AnalystReviewDecision, *, asof: date) -> bool:
    reviewed_at = parse_date(decision.reviewed_at)
    return reviewed_at is not None and reviewed_at >= asof


def is_reviewed_before_asof(decision: AnalystReviewDecision, *, asof: date) -> bool:
    reviewed_at = parse_date(decision.reviewed_at)
    return reviewed_at is not None and reviewed_at < asof


def decision_matches(
    decision: AnalystReviewDecision,
    *,
    ticker: str,
    cohort: str,
    review_categories: set[str] | None = None,
) -> bool:
    if decision.key_ticker != normalize_ticker(ticker):
        return False
    decision_cohort = decision.key_cohort
    if decision_cohort not in GLOBAL_MATCH_VALUES and decision_cohort != normalize_key(cohort):
        return False
    if review_categories is None:
        return True
    decision_category = decision.key_category
    return decision_category in GLOBAL_MATCH_VALUES or decision_category in review_categories


def matching_decisions(
    decisions: list[AnalystReviewDecision],
    *,
    ticker: str,
    cohort: str,
    review_categories: set[str] | None = None,
) -> list[AnalystReviewDecision]:
    return [
        decision
        for decision in decisions
        if decision_matches(decision, ticker=ticker, cohort=cohort, review_categories=review_categories)
    ]


def effective_decision(
    decisions: list[AnalystReviewDecision],
    *,
    ticker: str,
    cohort: str,
    review_categories: set[str] | None = None,
    asof: date | None = None,
) -> AnalystReviewDecision | None:
    target_asof = asof or utc_today()
    candidates = [
        decision
        for decision in matching_decisions(
            decisions,
            ticker=ticker,
            cohort=cohort,
            review_categories=review_categories,
        )
        if decision.active
        and decision.decision
        and not is_expired(decision, asof=target_asof)
        and is_reviewed_before_asof(decision, asof=target_asof)
    ]
    candidates.sort(
        key=lambda item: (
            DECISION_PRIORITY.get(item.decision, 99),
            -(parse_date(item.reviewed_at) or date.min).toordinal(),
            -item.row_number,
        )
    )
    return candidates[0] if candidates else None


def latest_expired_decision(
    decisions: list[AnalystReviewDecision],
    *,
    ticker: str,
    cohort: str,
    review_categories: set[str] | None = None,
    asof: date | None = None,
) -> AnalystReviewDecision | None:
    target_asof = asof or utc_today()
    candidates = [
        decision
        for decision in matching_decisions(
            decisions,
            ticker=ticker,
            cohort=cohort,
            review_categories=review_categories,
        )
        if decision.active
        and decision.decision
        and is_expired(decision, asof=target_asof)
        and is_reviewed_before_asof(decision, asof=target_asof)
    ]
    candidates.sort(key=lambda item: (parse_date(item.expires_at) or date.min, -item.row_number), reverse=True)
    return candidates[0] if candidates else None


def decision_key(decision: AnalystReviewDecision) -> str:
    return "|".join(
        [
            decision.key_ticker,
            decision.key_cohort or "*",
            decision.key_category or "*",
        ]
    )


def decision_fingerprint(decision: AnalystReviewDecision) -> str:
    payload = {
        "ticker": decision.key_ticker,
        "calibration_cohort": decision.key_cohort,
        "review_category": decision.key_category,
        "decision": decision.decision,
        "decision_reason": decision.decision_reason,
        "review_owner": decision.review_owner,
        "reviewed_at": decision.reviewed_at,
        "expires_at": decision.expires_at,
        "active": decision.active,
        "allow_portfolio_candidate_override": decision.allow_portfolio_candidate_override,
        "max_position_weight_override": decision.max_position_weight_override,
        "source_reference": decision.source_reference,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def decision_expiration_status(
    decision: AnalystReviewDecision,
    *,
    asof: date,
    warning_days: int,
) -> tuple[str, int | None, int]:
    if not decision.active or not decision.decision:
        return "inactive", None, 0
    expires_at = parse_date(decision.expires_at)
    if expires_at is None:
        return "active_no_expiration", None, 0
    days_to_expiration = (expires_at - asof).days
    if days_to_expiration < 0:
        return "expired", days_to_expiration, 1
    if days_to_expiration <= warning_days:
        return "expires_soon", days_to_expiration, 1
    return "current", days_to_expiration, 0


def decision_lifecycle_rows(
    decisions: list[AnalystReviewDecision],
    *,
    asof: date,
    warning_days: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for decision in decisions:
        expiration_status, days_to_expiration, needs_review = decision_expiration_status(
            decision,
            asof=asof,
            warning_days=warning_days,
        )
        rows.append(
            {
                "asof_date": asof.isoformat(),
                "row_number": decision.row_number,
                "ticker": decision.ticker,
                "calibration_cohort": decision.calibration_cohort,
                "review_category": decision.review_category,
                "decision": decision.decision,
                "decision_reason": decision.decision_reason,
                "review_owner": decision.review_owner,
                "reviewed_at": decision.reviewed_at,
                "expires_at": decision.expires_at,
                "active": int(decision.active),
                "allow_portfolio_candidate_override": int(decision.allow_portfolio_candidate_override),
                "source_reference": decision.source_reference,
                "expiration_status": expiration_status,
                "days_to_expiration": "" if days_to_expiration is None else days_to_expiration,
                "needs_review": needs_review,
                "decision_key": decision_key(decision),
                "decision_fingerprint": decision_fingerprint(decision),
            }
        )
    return rows


def ensure_decision_log_file(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=DECISION_LOG_FIELDNAMES, lineterminator="\n")
        writer.writeheader()


def append_decision_change_log(
    path: Path,
    decisions: list[AnalystReviewDecision],
    *,
    asof: date,
    logged_at_utc: datetime | None = None,
) -> int:
    ensure_decision_log_file(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        existing_rows = list(csv.DictReader(handle))
    seen_fingerprints = {str(row.get("decision_fingerprint") or "") for row in existing_rows}
    seen_keys = {str(row.get("decision_key") or "") for row in existing_rows}
    logged_at = (logged_at_utc or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    new_rows: list[dict[str, Any]] = []
    for decision in decisions:
        fingerprint = decision_fingerprint(decision)
        if fingerprint in seen_fingerprints:
            continue
        key = decision_key(decision)
        new_rows.append(
            {
                "logged_at_utc": logged_at,
                "asof_date": asof.isoformat(),
                "event_type": "decision_changed" if key in seen_keys else "decision_created",
                "decision_key": key,
                "decision_fingerprint": fingerprint,
                "row_number": decision.row_number,
                "ticker": decision.ticker,
                "calibration_cohort": decision.calibration_cohort,
                "review_category": decision.review_category,
                "decision": decision.decision,
                "decision_reason": decision.decision_reason,
                "review_owner": decision.review_owner,
                "reviewed_at": decision.reviewed_at,
                "expires_at": decision.expires_at,
                "active": int(decision.active),
                "allow_portfolio_candidate_override": int(decision.allow_portfolio_candidate_override),
                "max_position_weight_override": (
                    "" if decision.max_position_weight_override is None else decision.max_position_weight_override
                ),
                "source_reference": decision.source_reference,
            }
        )
    if not new_rows:
        return 0
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=DECISION_LOG_FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writerows(new_rows)
    return len(new_rows)


def value_from(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def int_flag(value: Any) -> int:
    try:
        return 1 if int(float(value or 0)) else 0
    except (TypeError, ValueError):
        return 0


def float_or_zero(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def is_blank(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def review_categories_for_item(
    item: Any,
    *,
    high_score_threshold: float,
    include_portfolio_candidates: bool = False,
) -> list[str]:
    categories: list[str] = []
    classification = str(value_from(item, "classification", "") or "").strip()
    score_value = value_from(item, "portfolio_candidate_score")
    if is_blank(score_value):
        score_value = value_from(item, "composite_score", 0.0)
    score = float_or_zero(score_value)
    if classification in MANUAL_REVIEW_CLASSES:
        categories.append(classification)
    if classification == "special_situation_or_binary_risk_watchlist":
        categories.append("special_situation_or_binary_risk")
    if score >= high_score_threshold and not int_flag(value_from(item, "portfolio_candidate_gate", 0)):
        categories.append("high_score_blocked")
    tier1_safety_gate = value_from(item, "passed_tier1_safety_gate")
    if tier1_safety_gate is not None and int_flag(tier1_safety_gate) == 0:
        categories.append("tier1_safety_failed")
    if int_flag(value_from(item, "hard_red_flag", 0)):
        categories.append("hard_red_flag")
    if int_flag(value_from(item, "unknown_reimbursement_flag", 0)):
        categories.append("unknown_reimbursement")
    if int_flag(value_from(item, "single_product_risk_flag", 0)):
        categories.append("single_product_risk")
    if int_flag(value_from(item, "binary_event_risk_flag", 0)):
        categories.append("binary_event_risk")
    if str(value_from(item, "safe_core_status", "") or "").strip().lower() == "watchlist":
        categories.append("safe_core_watchlist")
    if include_portfolio_candidates and int_flag(value_from(item, "portfolio_candidate_gate", 0)):
        categories.append("portfolio_candidate")
    return sorted(set(categories))

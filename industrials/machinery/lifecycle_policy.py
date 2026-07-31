from __future__ import annotations

import csv
import hashlib
import importlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from industrials.core.config import cfg_get, resolve_path


MODEL_FAMILY = "machinery"
POLICY_VERSION = "machinery_lifecycle_v1"
FINANCIAL_FEATURES = importlib.import_module(
    "industrials.scripts.08_build_industrials_financial_features"
)

PRE_COMMERCIAL = "pre_commercial"
COMMERCIAL_EMERGING = "commercial_emerging"
ESTABLISHED_OPERATING = "established_operating"
LIFECYCLE_CLASSES = frozenset(
    {
        PRE_COMMERCIAL,
        COMMERCIAL_EMERGING,
        ESTABLISHED_OPERATING,
    }
)

ACCEPTED = "ACCEPTED"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
REJECTED = "REJECTED"
DECISION_STATUSES = frozenset({ACCEPTED, REVIEW_REQUIRED, REJECTED})
FINAL_DECISION_STATUSES = frozenset({ACCEPTED, REJECTED})

VALIDATED_CUSTOMER_REVENUE = "validated_customer_revenue"
VALIDATED_NONCOMMERCIAL_REVENUE = "validated_noncommercial_revenue"
REVENUE_REVIEW_REQUIRED = "review_required"
REVENUE_CLASSIFICATIONS = frozenset(
    {
        VALIDATED_CUSTOMER_REVENUE,
        VALIDATED_NONCOMMERCIAL_REVENUE,
        REVENUE_REVIEW_REQUIRED,
    }
)

HARD_EVENT_TYPES = frozenset(
    {
        "bankruptcy",
        "definitive_acquisition",
        "delisting_notice",
        "filing_deficiency",
        "going_concern",
    }
)

TRANSITION_FIELDS = (
    "transition_id",
    "ticker",
    "from_class",
    "to_class",
    "valid_from",
    "evidence_asof",
    "evidence_artifact",
    "evidence_sha256",
    "decision_status",
    "decision_reason",
    "reviewer",
    "reviewed_at",
    "policy_version",
    "record_sha256",
)
TRANSITION_HASH_FIELDS = tuple(
    field for field in TRANSITION_FIELDS if field != "record_sha256"
)
REVENUE_POLICY_FIELDS = (
    "ticker",
    "revenue_classification",
    "valid_from",
    "evidence_artifact",
    "evidence_sha256",
    "decision_status",
    "decision_reason",
    "reviewer",
    "reviewed_at",
    "policy_version",
    "record_sha256",
)
REVENUE_HASH_FIELDS = tuple(
    field for field in REVENUE_POLICY_FIELDS if field != "record_sha256"
)
HARD_EVENT_FIELDS = (
    "event_id",
    "ticker",
    "event_type",
    "valid_from",
    "valid_to",
    "evidence_artifact",
    "evidence_sha256",
    "decision_status",
    "decision_reason",
    "reviewer",
    "reviewed_at",
    "policy_version",
    "record_sha256",
)
HARD_EVENT_HASH_FIELDS = tuple(
    field for field in HARD_EVENT_FIELDS if field != "record_sha256"
)

LIFECYCLE_STATE_FIELDS = (
    "lifecycle_class",
    "lifecycle_policy_version",
    "lifecycle_state_source",
    "lifecycle_transition_id",
    "lifecycle_valid_from",
    "lifecycle_investability_eligible_flag",
    "lifecycle_investability_reason",
    "lifecycle_weight_cap",
    "lifecycle_hard_event_veto_flag",
    "lifecycle_hard_event_types",
)

CANDIDATE_FIELDS = (
    "asof_date",
    "ticker",
    "company_name",
    "calibration_cohort",
    "development_stage",
    "current_lifecycle_class",
    "suggested_lifecycle_class",
    "candidate_status",
    "candidate_reasons",
    "revenue_classification",
    "commercial_revenue_quarter_streak",
    "established_revenue_quarter_streak",
    "demotion_revenue_quarter_streak",
    "latest_fiscal_period_end",
    "latest_revenue_ttm_usd",
    "latest_financial_confidence",
    "latest_data_quality_status",
    "listing_start_date",
    "listed_days",
    "avg_dollar_volume_60d",
    "capital_raise_dependence",
    "cash_runway_years",
    "diluted_shares_yoy_growth",
    "hard_event_vetoes",
    "evidence_periods_json",
    "evidence_artifact",
    "evidence_sha256",
    "policy_version",
)

SHADOW_FIELDS = (
    "asof_date",
    "ticker",
    "final_rank",
    "final_score",
    "calibration_cohort",
    "development_stage",
    "rank_ready_flag",
    "operating_only_eligible_flag",
    *LIFECYCLE_STATE_FIELDS,
    "lifecycle_universe_eligible_flag",
    "eligibility_changed_flag",
)


@dataclass(frozen=True)
class LifecycleThresholds:
    emerging_revenue_usd: float = 10_000_000.0
    emerging_quarters: int = 4
    emerging_financial_confidence: float = 0.55
    established_revenue_usd: float = 50_000_000.0
    established_quarters: int = 8
    established_financial_confidence: float = 0.70
    established_min_listed_days: int = 730
    established_demotion_revenue_usd: float = 25_000_000.0
    emerging_demotion_revenue_usd: float = 5_000_000.0
    demotion_quarters: int = 4
    maximum_capital_raise_dependence: float = 0.75
    minimum_cash_runway_years: float = 2.0
    maximum_diluted_shares_yoy_growth: float = 0.15
    minimum_avg_dollar_volume_60d: float = 5_000_000.0
    emerging_weight_cap: float = 0.025
    minimum_precommercial_calibration_members: int = 15
    maximum_period_gap_days: int = 150


@dataclass(frozen=True)
class LifecyclePolicy:
    policy_version: str
    thresholds: LifecycleThresholds
    transitions: tuple[dict[str, str], ...]
    revenue_decisions: tuple[dict[str, str], ...]
    hard_events: tuple[dict[str, str], ...]
    transitions_path: Path
    revenue_policy_path: Path
    hard_events_path: Path


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _ticker(value: object) -> str:
    return _text(value).upper()


def _float(value: object) -> float | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = float(text)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_date(value: object, *, field: str) -> date:
    text = _text(value)
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(
            f"Invalid {field}={value!r}; expected YYYY-MM-DD"
        ) from exc


def _parse_review_date(value: object, *, field: str) -> date:
    text = _text(value)
    if not text:
        raise ValueError(f"Missing {field}")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return _parse_date(text, field=field)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record_sha256(
    row: Mapping[str, object],
    *,
    fields: Sequence[str],
) -> str:
    payload = {field: _text(row.get(field)) for field in fields}
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_csv(
    path: Path,
    *,
    expected_fields: Sequence[str],
) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        actual = tuple(reader.fieldnames or ())
        if actual != tuple(expected_fields):
            raise ValueError(
                f"{path}: expected fields={list(expected_fields)} "
                f"found={list(actual)}"
            )
        return [
            {field: _text(row.get(field)) for field in expected_fields}
            for row in reader
            if any(_text(row.get(field)) for field in expected_fields)
        ]


def _thresholds(config: Mapping[str, Any]) -> LifecycleThresholds:
    prefix = "machinery_lifecycle.thresholds"
    return LifecycleThresholds(
        emerging_revenue_usd=float(
            cfg_get(dict(config), f"{prefix}.emerging_revenue_usd", 10_000_000)
        ),
        emerging_quarters=int(
            cfg_get(dict(config), f"{prefix}.emerging_quarters", 4)
        ),
        emerging_financial_confidence=float(
            cfg_get(
                dict(config),
                f"{prefix}.emerging_financial_confidence",
                0.55,
            )
        ),
        established_revenue_usd=float(
            cfg_get(
                dict(config),
                f"{prefix}.established_revenue_usd",
                50_000_000,
            )
        ),
        established_quarters=int(
            cfg_get(dict(config), f"{prefix}.established_quarters", 8)
        ),
        established_financial_confidence=float(
            cfg_get(
                dict(config),
                f"{prefix}.established_financial_confidence",
                0.70,
            )
        ),
        established_min_listed_days=int(
            cfg_get(
                dict(config),
                f"{prefix}.established_min_listed_days",
                730,
            )
        ),
        established_demotion_revenue_usd=float(
            cfg_get(
                dict(config),
                f"{prefix}.established_demotion_revenue_usd",
                25_000_000,
            )
        ),
        emerging_demotion_revenue_usd=float(
            cfg_get(
                dict(config),
                f"{prefix}.emerging_demotion_revenue_usd",
                5_000_000,
            )
        ),
        demotion_quarters=int(
            cfg_get(dict(config), f"{prefix}.demotion_quarters", 4)
        ),
        maximum_capital_raise_dependence=float(
            cfg_get(
                dict(config),
                f"{prefix}.maximum_capital_raise_dependence",
                0.75,
            )
        ),
        minimum_cash_runway_years=float(
            cfg_get(
                dict(config),
                f"{prefix}.minimum_cash_runway_years",
                2.0,
            )
        ),
        maximum_diluted_shares_yoy_growth=float(
            cfg_get(
                dict(config),
                f"{prefix}.maximum_diluted_shares_yoy_growth",
                0.15,
            )
        ),
        minimum_avg_dollar_volume_60d=float(
            cfg_get(
                dict(config),
                f"{prefix}.minimum_avg_dollar_volume_60d",
                5_000_000,
            )
        ),
        emerging_weight_cap=float(
            cfg_get(dict(config), f"{prefix}.emerging_weight_cap", 0.025)
        ),
        minimum_precommercial_calibration_members=int(
            cfg_get(
                dict(config),
                f"{prefix}.minimum_precommercial_calibration_members",
                15,
            )
        ),
        maximum_period_gap_days=int(
            cfg_get(
                dict(config),
                f"{prefix}.maximum_period_gap_days",
                150,
            )
        ),
    )


def load_lifecycle_policy(
    config: Mapping[str, Any],
    *,
    config_path: Path,
) -> LifecyclePolicy:
    base_dir = config_path.parent
    version = _text(
        cfg_get(dict(config), "machinery_lifecycle.policy_version", POLICY_VERSION)
    )
    if version != POLICY_VERSION:
        raise ValueError(
            f"Unsupported machinery lifecycle policy version={version!r}"
        )
    transitions_path = resolve_path(
        cfg_get(
            dict(config),
            "machinery_lifecycle.transitions_csv",
            "system_csvs/machinery_lifecycle_transitions.csv",
        ),
        base_dir=base_dir,
    )
    revenue_path = resolve_path(
        cfg_get(
            dict(config),
            "machinery_lifecycle.revenue_policy_csv",
            "system_csvs/machinery_lifecycle_revenue_policy.csv",
        ),
        base_dir=base_dir,
    )
    hard_events_path = resolve_path(
        cfg_get(
            dict(config),
            "machinery_lifecycle.hard_events_csv",
            "system_csvs/machinery_lifecycle_hard_events.csv",
        ),
        base_dir=base_dir,
    )
    return LifecyclePolicy(
        policy_version=version,
        thresholds=_thresholds(config),
        transitions=tuple(
            _read_csv(transitions_path, expected_fields=TRANSITION_FIELDS)
        ),
        revenue_decisions=tuple(
            _read_csv(revenue_path, expected_fields=REVENUE_POLICY_FIELDS)
        ),
        hard_events=tuple(
            _read_csv(hard_events_path, expected_fields=HARD_EVENT_FIELDS)
        ),
        transitions_path=transitions_path,
        revenue_policy_path=revenue_path,
        hard_events_path=hard_events_path,
    )


def _validate_record(
    row: Mapping[str, str],
    *,
    path: Path,
    row_id: str,
    hash_fields: Sequence[str],
    require_evidence: bool,
) -> list[str]:
    issues: list[str] = []
    if _text(row.get("policy_version")) != POLICY_VERSION:
        issues.append(f"{row_id}:invalid_policy_version")
    status = _text(row.get("decision_status")).upper()
    if status not in DECISION_STATUSES:
        issues.append(f"{row_id}:invalid_decision_status")
    elif status not in FINAL_DECISION_STATUSES:
        issues.append(f"{row_id}:pending_decision_in_policy_ledger")
    expected_hash = record_sha256(row, fields=hash_fields)
    if _text(row.get("record_sha256")) != expected_hash:
        issues.append(f"{row_id}:record_sha256_mismatch")
    try:
        valid_from = _parse_date(row.get("valid_from"), field="valid_from")
        reviewed_at = _parse_review_date(
            row.get("reviewed_at"),
            field="reviewed_at",
        )
        if valid_from < reviewed_at:
            issues.append(f"{row_id}:valid_from_before_review")
    except ValueError as exc:
        issues.append(f"{row_id}:{exc}")
    evidence_raw = _text(row.get("evidence_artifact"))
    evidence_hash = _text(row.get("evidence_sha256"))
    if require_evidence and status in FINAL_DECISION_STATUSES:
        if not evidence_raw or not evidence_hash:
            issues.append(f"{row_id}:accepted_record_missing_evidence")
        else:
            evidence_path = Path(evidence_raw)
            if not evidence_path.is_absolute():
                evidence_path = path.parent / evidence_path
            if not evidence_path.is_file():
                issues.append(f"{row_id}:evidence_artifact_missing")
            elif file_sha256(evidence_path) != evidence_hash:
                issues.append(f"{row_id}:evidence_sha256_mismatch")
    return issues


def _validate_evidence_binding(
    row: Mapping[str, str],
    *,
    path: Path,
    row_id: str,
    kind: str,
) -> list[str]:
    evidence_raw = _text(row.get("evidence_artifact"))
    if not evidence_raw:
        return []
    evidence_path = Path(evidence_raw)
    if not evidence_path.is_absolute():
        evidence_path = path.parent / evidence_path
    if not evidence_path.is_file():
        return []
    try:
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [f"{row_id}:invalid_evidence_json"]
    issues: list[str] = []
    if _text(payload.get("policy_version")) != POLICY_VERSION:
        issues.append(f"{row_id}:evidence_policy_version_mismatch")
    ticker = _ticker(row.get("ticker"))
    if kind in {"transition", "revenue"}:
        if (
            _text(payload.get("artifact_family"))
            != "machinery_lifecycle_candidate_evidence"
        ):
            issues.append(f"{row_id}:invalid_candidate_evidence_family")
            return issues
        candidate_raw = payload.get("candidate")
        if not isinstance(candidate_raw, Mapping):
            issues.append(f"{row_id}:candidate_evidence_missing")
            return issues
        candidate = candidate_raw
        if _ticker(candidate.get("ticker")) != ticker:
            issues.append(f"{row_id}:evidence_ticker_mismatch")
        if kind == "transition":
            expected = {
                "from_class": "current_lifecycle_class",
                "to_class": "suggested_lifecycle_class",
                "evidence_asof": "asof_date",
            }
            for row_field, evidence_field in expected.items():
                if _text(row.get(row_field)) != _text(
                    candidate.get(evidence_field)
                ):
                    issues.append(
                        f"{row_id}:evidence_{row_field}_mismatch"
                    )
            if _text(candidate.get("candidate_status")) == "NO_CHANGE":
                issues.append(f"{row_id}:evidence_has_no_transition")
    elif kind == "hard_event":
        if (
            _text(payload.get("artifact_family"))
            != "machinery_lifecycle_hard_event_evidence"
        ):
            issues.append(f"{row_id}:invalid_hard_event_evidence_family")
            return issues
        parser_raw = payload.get("parser_evidence")
        if not isinstance(parser_raw, Mapping):
            issues.append(f"{row_id}:parser_evidence_missing")
            return issues
        if _ticker(parser_raw.get("ticker")) != ticker:
            issues.append(f"{row_id}:evidence_ticker_mismatch")
        if _text(payload.get("event_type")) != _text(row.get("event_type")):
            issues.append(f"{row_id}:evidence_event_type_mismatch")
    else:
        issues.append(f"{row_id}:unknown_evidence_kind")
    return issues


def validate_lifecycle_policy(
    policy: LifecyclePolicy,
) -> dict[str, Any]:
    issues: list[str] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()
    accepted_by_ticker: dict[str, list[dict[str, str]]] = {}
    for row in policy.transitions:
        transition_id = _text(row.get("transition_id"))
        row_id = transition_id or f"transition_row_{len(seen_ids) + 1}"
        if not transition_id:
            issues.append(f"{row_id}:missing_transition_id")
        elif transition_id in seen_ids:
            issues.append(f"{row_id}:duplicate_transition_id")
        seen_ids.add(transition_id)
        ticker = _ticker(row.get("ticker"))
        if not ticker:
            issues.append(f"{row_id}:missing_ticker")
        from_class = _text(row.get("from_class"))
        to_class = _text(row.get("to_class"))
        if from_class not in LIFECYCLE_CLASSES:
            issues.append(f"{row_id}:invalid_from_class")
        if to_class not in LIFECYCLE_CLASSES:
            issues.append(f"{row_id}:invalid_to_class")
        if from_class == to_class:
            issues.append(f"{row_id}:no_op_transition")
        try:
            evidence_asof = _parse_date(
                row.get("evidence_asof"),
                field="evidence_asof",
            )
            valid_from = _parse_date(
                row.get("valid_from"),
                field="valid_from",
            )
            if valid_from < evidence_asof:
                issues.append(f"{row_id}:valid_from_before_evidence")
        except ValueError as exc:
            issues.append(f"{row_id}:{exc}")
        issues.extend(
            _validate_record(
                row,
                path=policy.transitions_path,
                row_id=row_id,
                hash_fields=TRANSITION_HASH_FIELDS,
                require_evidence=True,
            )
        )
        issues.extend(
            _validate_evidence_binding(
                row,
                path=policy.transitions_path,
                row_id=row_id,
                kind="transition",
            )
        )
        if _text(row.get("decision_status")).upper() == ACCEPTED and ticker:
            accepted_by_ticker.setdefault(ticker, []).append(dict(row))

    for ticker, rows in accepted_by_ticker.items():
        ordered = sorted(
            rows,
            key=lambda item: (
                _text(item.get("valid_from")),
                _text(item.get("transition_id")),
            ),
        )
        previous_to = ""
        previous_date: date | None = None
        for row in ordered:
            current_date = _parse_date(
                row.get("valid_from"),
                field="valid_from",
            )
            if previous_date is not None and current_date <= previous_date:
                issues.append(f"{ticker}:accepted_transition_dates_not_strict")
            if previous_to and _text(row.get("from_class")) != previous_to:
                issues.append(f"{ticker}:accepted_transition_chain_broken")
            previous_to = _text(row.get("to_class"))
            previous_date = current_date

    for index, row in enumerate(policy.revenue_decisions, start=1):
        ticker = _ticker(row.get("ticker"))
        row_id = f"revenue:{ticker or index}"
        if not ticker:
            issues.append(f"{row_id}:missing_ticker")
        if _text(row.get("revenue_classification")) not in REVENUE_CLASSIFICATIONS:
            issues.append(f"{row_id}:invalid_revenue_classification")
        issues.extend(
            _validate_record(
                row,
                path=policy.revenue_policy_path,
                row_id=row_id,
                hash_fields=REVENUE_HASH_FIELDS,
                require_evidence=True,
            )
        )
        issues.extend(
            _validate_evidence_binding(
                row,
                path=policy.revenue_policy_path,
                row_id=row_id,
                kind="revenue",
            )
        )

    seen_events: set[str] = set()
    for index, row in enumerate(policy.hard_events, start=1):
        event_id = _text(row.get("event_id"))
        row_id = event_id or f"hard_event_row_{index}"
        if not event_id:
            issues.append(f"{row_id}:missing_event_id")
        elif event_id in seen_events:
            issues.append(f"{row_id}:duplicate_event_id")
        seen_events.add(event_id)
        if _ticker(row.get("ticker")) == "":
            issues.append(f"{row_id}:missing_ticker")
        if _text(row.get("event_type")) not in HARD_EVENT_TYPES:
            issues.append(f"{row_id}:invalid_event_type")
        valid_to_raw = _text(row.get("valid_to"))
        if valid_to_raw:
            try:
                if _parse_date(
                    valid_to_raw,
                    field="valid_to",
                ) < _parse_date(row.get("valid_from"), field="valid_from"):
                    issues.append(f"{row_id}:valid_to_before_valid_from")
            except ValueError as exc:
                issues.append(f"{row_id}:{exc}")
        issues.extend(
            _validate_record(
                row,
                path=policy.hard_events_path,
                row_id=row_id,
                hash_fields=HARD_EVENT_HASH_FIELDS,
                require_evidence=True,
            )
        )
        issues.extend(
            _validate_evidence_binding(
                row,
                path=policy.hard_events_path,
                row_id=row_id,
                kind="hard_event",
            )
        )

    thresholds = policy.thresholds
    if thresholds.emerging_revenue_usd <= 0:
        issues.append("threshold:emerging_revenue_usd_must_be_positive")
    if thresholds.established_revenue_usd <= thresholds.emerging_revenue_usd:
        issues.append("threshold:established_revenue_must_exceed_emerging")
    if (
        thresholds.established_demotion_revenue_usd
        >= thresholds.established_revenue_usd
    ):
        issues.append("threshold:established_hysteresis_is_not_asymmetric")
    if (
        thresholds.emerging_demotion_revenue_usd
        >= thresholds.emerging_revenue_usd
    ):
        issues.append("threshold:emerging_hysteresis_is_not_asymmetric")
    if not 0 < thresholds.emerging_weight_cap <= 1:
        issues.append("threshold:emerging_weight_cap_out_of_range")
    if thresholds.minimum_precommercial_calibration_members < 5:
        warnings.append("calibration_floor_below_scoring_fallback_floor")
    return {
        "acceptance": "PASS" if not issues else "FAIL",
        "policy_version": policy.policy_version,
        "transition_row_count": len(policy.transitions),
        "accepted_transition_count": sum(
            _text(row.get("decision_status")).upper() == ACCEPTED
            for row in policy.transitions
        ),
        "revenue_policy_row_count": len(policy.revenue_decisions),
        "hard_event_row_count": len(policy.hard_events),
        "issues": issues,
        "warnings": warnings,
        "source_sha256": {
            "transitions_csv": file_sha256(policy.transitions_path),
            "revenue_policy_csv": file_sha256(policy.revenue_policy_path),
            "hard_events_csv": file_sha256(policy.hard_events_path),
        },
    }


def _default_class(row: Mapping[str, object]) -> str:
    stage = _text(row.get("development_stage")).lower()
    cohort = _text(row.get("calibration_cohort"))
    if (
        stage in {"development", "development_stage"}
        or cohort == "development_stage_emerging_machinery"
    ):
        return PRE_COMMERCIAL
    return ESTABLISHED_OPERATING


def _effective_rows(
    rows: Iterable[dict[str, str]],
    *,
    ticker: str,
    asof: date,
) -> list[dict[str, str]]:
    return sorted(
        (
            row
            for row in rows
            if _ticker(row.get("ticker")) == ticker
            and _text(row.get("decision_status")).upper() == ACCEPTED
            and _parse_date(row.get("valid_from"), field="valid_from") <= asof
        ),
        key=lambda row: (
            _text(row.get("valid_from")),
            _text(row.get("transition_id")),
            _text(row.get("event_id")),
        ),
    )


def effective_revenue_classification(
    policy: LifecyclePolicy,
    *,
    ticker: str,
    asof: str,
) -> str:
    target = _parse_date(asof, field="asof")
    rows = _effective_rows(
        policy.revenue_decisions,
        ticker=_ticker(ticker),
        asof=target,
    )
    return (
        _text(rows[-1].get("revenue_classification"))
        if rows
        else REVENUE_REVIEW_REQUIRED
    )


def _active_hard_events(
    policy: LifecyclePolicy,
    *,
    ticker: str,
    asof: date,
) -> list[dict[str, str]]:
    active: list[dict[str, str]] = []
    for row in _effective_rows(
        policy.hard_events,
        ticker=ticker,
        asof=asof,
    ):
        valid_to = _text(row.get("valid_to"))
        if not valid_to or asof <= _parse_date(valid_to, field="valid_to"):
            active.append(row)
    return active


def emerging_risk_gate_reasons(
    row: Mapping[str, object],
    *,
    thresholds: LifecycleThresholds,
) -> list[str]:
    reasons: list[str] = []
    dependence = _float(row.get("capital_raise_dependence"))
    if dependence is None:
        reasons.append("missing_capital_raise_dependence")
    elif dependence > thresholds.maximum_capital_raise_dependence:
        reasons.append("capital_raise_dependence_above_limit")
    runway = _float(row.get("cash_runway_years"))
    if runway is None:
        reasons.append("missing_cash_runway_years")
    elif runway < thresholds.minimum_cash_runway_years:
        reasons.append("cash_runway_below_minimum")
    dilution = _float(row.get("diluted_shares_yoy_growth"))
    if dilution is None:
        reasons.append("missing_diluted_shares_yoy_growth")
    elif dilution > thresholds.maximum_diluted_shares_yoy_growth:
        reasons.append("diluted_shares_growth_above_limit")
    liquidity = _float(row.get("avg_dollar_volume_60d"))
    if liquidity is None:
        reasons.append("missing_avg_dollar_volume_60d")
    elif liquidity < thresholds.minimum_avg_dollar_volume_60d:
        reasons.append("avg_dollar_volume_below_minimum")
    return reasons


def resolve_lifecycle_state(
    row: Mapping[str, object],
    *,
    asof: str,
    policy: LifecyclePolicy,
) -> dict[str, str]:
    target = _parse_date(asof, field="asof")
    ticker = _ticker(row.get("ticker"))
    lifecycle_class = _default_class(row)
    state_source = "static_taxonomy_bootstrap"
    transition_id = ""
    valid_from = ""
    transitions = _effective_rows(
        policy.transitions,
        ticker=ticker,
        asof=target,
    )
    for transition in transitions:
        from_class = _text(transition.get("from_class"))
        if from_class != lifecycle_class:
            raise ValueError(
                f"{ticker}: lifecycle transition chain expected "
                f"from_class={lifecycle_class!r}, found={from_class!r}"
            )
        lifecycle_class = _text(transition.get("to_class"))
        state_source = "ratified_transition"
        transition_id = _text(transition.get("transition_id"))
        valid_from = _text(transition.get("valid_from"))

    hard_events = _active_hard_events(
        policy,
        ticker=ticker,
        asof=target,
    )
    event_types = sorted(
        {_text(event.get("event_type")) for event in hard_events}
    )
    reasons: list[str] = []
    eligible = False
    if event_types:
        reasons.extend(f"hard_event_{event_type}" for event_type in event_types)
    elif lifecycle_class == PRE_COMMERCIAL:
        reasons.append("pre_commercial_research_only")
    elif lifecycle_class == COMMERCIAL_EMERGING:
        reasons.extend(
            emerging_risk_gate_reasons(
                row,
                thresholds=policy.thresholds,
            )
        )
        eligible = not reasons
    elif lifecycle_class == ESTABLISHED_OPERATING:
        eligible = True
    else:
        raise ValueError(
            f"{ticker}: unsupported lifecycle_class={lifecycle_class!r}"
        )
    return {
        "lifecycle_class": lifecycle_class,
        "lifecycle_policy_version": policy.policy_version,
        "lifecycle_state_source": state_source,
        "lifecycle_transition_id": transition_id,
        "lifecycle_valid_from": valid_from,
        "lifecycle_investability_eligible_flag": "1" if eligible else "0",
        "lifecycle_investability_reason": (
            "eligible" if eligible else ";".join(reasons)
        ),
        "lifecycle_weight_cap": (
            _format_float(policy.thresholds.emerging_weight_cap)
            if lifecycle_class == COMMERCIAL_EMERGING
            else "1"
        ),
        "lifecycle_hard_event_veto_flag": "1" if event_types else "0",
        "lifecycle_hard_event_types": ";".join(event_types),
    }


def _format_float(value: object) -> str:
    parsed = _float(value)
    if parsed is None:
        return ""
    return f"{parsed:.12g}"


def _current_memberships(
    conn: sqlite3.Connection,
    *,
    asof: str,
) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT *
        FROM (
            SELECT
                m.ticker,
                c.company_name,
                t.calibration_cohort_id AS calibration_cohort,
                t.development_stage,
                m.start_date AS membership_start_date,
                ROW_NUMBER() OVER (
                    PARTITION BY m.ticker
                    ORDER BY m.confidence DESC, m.start_date DESC
                ) AS row_number
            FROM dim_universe_membership m
            JOIN dim_company c ON c.company_id = m.company_id
            JOIN dim_industrials_taxonomy t
              ON t.ticker = m.ticker
             AND t.model_family = m.model_family
            WHERE m.model_family = ?
              AND m.start_date <= ?
              AND (
                    m.end_date IS NULL
                 OR m.end_date = ''
                 OR m.end_date >= ?
              )
        )
        WHERE row_number = 1
        ORDER BY ticker
        """,
        (MODEL_FAMILY, asof, asof),
    ).fetchall()
    return [
        {key: row[key] for key in row.keys() if key != "row_number"}
        for row in rows
    ]


def _latest_market_rows(
    conn: sqlite3.Connection,
    *,
    asof: str,
) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT ticker, asof_date, avg_dollar_volume_60d
        FROM feature_market_technical
        WHERE model_family = ? AND asof_date <= ?
        ORDER BY ticker, asof_date DESC
        """,
        (MODEL_FAMILY, asof),
    ).fetchall()
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        ticker = _ticker(row["ticker"])
        if ticker not in output:
            output[ticker] = dict(row)
    return output


def parser_hard_event_candidates(
    conn: sqlite3.Connection,
    *,
    asof: str,
    minimum_confidence: float = 0.90,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            evidence_key,
            run_id,
            ticker,
            accession_number,
            filing_date,
            accepted_at,
            period_end,
            candidate_value,
            confidence,
            candidate_status,
            status_reason,
            evidence_text,
            source_document,
            extraction_method,
            provenance_json,
            created_at
        FROM sec_parser_metric_evidence_shadow
        WHERE model_family = ?
          AND metric_name = 'going_concern_flag'
          AND candidate_status = 'ACCEPTED'
          AND confidence >= ?
        ORDER BY ticker, COALESCE(accepted_at, filing_date) DESC, run_id DESC
        """,
        (MODEL_FAMILY, minimum_confidence),
    ).fetchall()
    latest: dict[str, tuple[tuple[str, int], dict[str, Any]]] = {}
    for source in rows:
        row = dict(source)
        evidence_date = _accepted_date(row)
        ticker = _ticker(row.get("ticker"))
        if not evidence_date or evidence_date > asof:
            continue
        key = (evidence_date, int(row.get("run_id") or 0))
        current = latest.get(ticker)
        if current is None or key > current[0]:
            latest[ticker] = (key, row)
    return [
        item[1]
        for ticker, item in sorted(latest.items())
        if _float(item[1].get("candidate_value")) == 1.0
    ]


def _latest_financial_rows(
    conn: sqlite3.Connection,
    *,
    asof: str,
) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            ticker,
            asof_date,
            fiscal_period_end,
            revenue_ttm_usd,
            financial_confidence,
            data_quality_status,
            capital_raise_dependence,
            cash_runway_years,
            diluted_shares_yoy_growth,
            canonical_quality,
            financial_fallback_status,
            operating_cash_flow_ttm_usd,
            accession_number,
            form_type
        FROM feature_financial_statement
        WHERE model_family = ?
          AND asof_date <= ?
        ORDER BY ticker, asof_date DESC
        """,
        (MODEL_FAMILY, asof),
    ).fetchall()
    output: dict[str, dict[str, Any]] = {}
    for source in rows:
        row = dict(source)
        ticker = _ticker(row["ticker"])
        if ticker not in output:
            output[ticker] = row
    return output


def _validated_zero_revenue_evidence(
    row: Mapping[str, object],
) -> dict[str, Any] | None:
    quality = _text(row.get("canonical_quality"))
    revenue = _float(row.get("revenue_ttm_usd"))
    operating_cash_flow = _float(
        row.get("operating_cash_flow_ttm_usd")
    )
    required_flag = (
        "development_stage_zero_revenue_validated_by_negative_"
        "operating_cash_flow"
    )
    if (
        revenue != 0.0
        or operating_cash_flow is None
        or operating_cash_flow >= 0.0
        or required_flag not in quality
    ):
        return None
    return {
        "ticker": _ticker(row.get("ticker")),
        "asof_date": _text(row.get("asof_date")),
        "fiscal_period_end": _text(row.get("fiscal_period_end")),
        "revenue_ttm_usd": 0.0,
        "financial_confidence": _float(row.get("financial_confidence")),
        "data_quality_status": _text(row.get("data_quality_status")),
        "accession_number": _text(row.get("accession_number")),
        "form_type": _text(row.get("form_type")),
        "evidence_basis": (
            "validated_zero_revenue_negative_operating_cash_flow"
        ),
    }


def _accepted_date(row: Mapping[str, object]) -> str:
    accepted = _text(row.get("accepted_at"))
    if len(accepted) >= 10 and accepted[4:5] == "-":
        return accepted[:10]
    digits = "".join(
        character for character in accepted if character.isdigit()
    )
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return _text(row.get("filing_date"))


def _canonical_revenue_period_rows(
    conn: sqlite3.Connection,
    *,
    asof: str,
    current_financial: Mapping[str, Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    source_rows = conn.execute(
        """
        SELECT
            ticker,
            canonical_metric,
            period_start,
            period_end,
            filing_date,
            accepted_at,
            accession_number,
            form_type,
            fiscal_period,
            taxonomy,
            concept_name,
            unit,
            value,
            value_usd,
            source_priority,
            canonical_quality,
            source_id
        FROM fact_financial_statement_canonical
        WHERE model_family = ?
          AND canonical_metric = 'revenue'
          AND period_end <= ?
        ORDER BY ticker, period_end DESC, filing_date DESC, source_priority ASC
        """,
        (MODEL_FAMILY, asof),
    ).fetchall()
    by_ticker: dict[str, list[dict[str, Any]]] = {}
    for source in source_rows:
        row = dict(source)
        if _accepted_date(row) > asof:
            continue
        value_usd = _float(row.get("value_usd"))
        value_local = _float(row.get("value"))
        row["value"] = value_usd if value_usd is not None else value_local
        by_ticker.setdefault(_ticker(row.get("ticker")), []).append(row)

    output: dict[str, list[dict[str, Any]]] = {}
    for ticker, rows in by_ticker.items():
        target_ends = sorted(
            {
                _text(row.get("period_end"))[:10]
                for row in rows
                if _text(row.get("period_end"))
            },
            reverse=True,
        )
        current = current_financial.get(ticker, {})
        current_confidence = _float(current.get("financial_confidence"))
        period_rows: list[dict[str, Any]] = []
        for period_end_raw in target_ends[:16]:
            period_end = _parse_date(period_end_raw, field="period_end")
            scoped = FINANCIAL_FEATURES.ttm_rows_ending_near(
                rows,
                target_end=period_end,
            )
            result = FINANCIAL_FEATURES.ttm_metric_result(scoped, "revenue")
            if (
                result.value is None
                or result.window_end is None
                or abs((result.window_end - period_end).days) > 20
            ):
                continue
            result_rows = [
                row
                for row in scoped
                if _text(row.get("period_end"))[:10] <= period_end_raw
            ]
            source_confidence = max(
                (
                    0.90
                    if _text(row.get("taxonomy"))
                    in {"us-gaap", "ifrs-full"}
                    and _text(row.get("canonical_quality")).startswith(
                        "mapped"
                    )
                    else 0.70
                    if _text(row.get("taxonomy")) == "dedicated-parser"
                    else 0.45
                )
                for row in result_rows
            )
            confidence = (
                min(current_confidence, source_confidence)
                if current_confidence is not None
                else source_confidence
            )
            filing_dates = [
                _accepted_date(row)
                for row in result_rows
                if _accepted_date(row)
            ]
            period_rows.append(
                {
                    "ticker": ticker,
                    "asof_date": max(filing_dates, default=asof),
                    "fiscal_period_end": result.window_end.isoformat(),
                    "revenue_ttm_usd": result.value,
                    "financial_confidence": confidence,
                    "data_quality_status": (
                        "complete" if not result.quality_flag else "review"
                    ),
                    "accession_number": "",
                    "form_type": "canonical_ttm_reconstruction",
                    "ttm_quality_flag": result.quality_flag,
                }
            )
        unique: dict[str, dict[str, Any]] = {}
        for row in period_rows:
            key = _text(row.get("fiscal_period_end"))
            if key and key not in unique:
                unique[key] = row
        output[ticker] = sorted(
            unique.values(),
            key=lambda row: _text(row.get("fiscal_period_end")),
            reverse=True,
        )
    return output


def _period_streak(
    rows: Sequence[Mapping[str, object]],
    *,
    threshold: float,
    confidence: float,
    comparison: str,
    maximum_gap_days: int,
) -> int:
    streak = 0
    previous: date | None = None
    for row in rows:
        try:
            period_end = _parse_date(
                row.get("fiscal_period_end"),
                field="fiscal_period_end",
            )
        except ValueError:
            break
        if (
            previous is not None
            and (previous - period_end).days > maximum_gap_days
        ):
            break
        revenue = _float(row.get("revenue_ttm_usd"))
        row_confidence = _float(row.get("financial_confidence"))
        complete = _text(row.get("data_quality_status")) == "complete"
        if (
            revenue is None
            or row_confidence is None
            or row_confidence < confidence
            or not complete
        ):
            break
        passes = (
            revenue >= threshold
            if comparison == "minimum"
            else revenue < threshold
        )
        if not passes:
            break
        streak += 1
        previous = period_end
    return streak


def _evidence_periods(
    rows: Sequence[Mapping[str, object]],
    *,
    limit: int,
) -> str:
    payload = [
        {
            "asof_date": _text(row.get("asof_date")),
            "fiscal_period_end": _text(row.get("fiscal_period_end")),
            "revenue_ttm_usd": _float(row.get("revenue_ttm_usd")),
            "financial_confidence": _float(
                row.get("financial_confidence")
            ),
            "data_quality_status": _text(row.get("data_quality_status")),
            "accession_number": _text(row.get("accession_number")),
            "form_type": _text(row.get("form_type")),
            "evidence_basis": _text(row.get("evidence_basis")),
        }
        for row in rows[:limit]
    ]
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def generate_lifecycle_candidates(
    conn: sqlite3.Connection,
    *,
    asof: str,
    policy: LifecyclePolicy,
) -> list[dict[str, str]]:
    target = _parse_date(asof, field="asof")
    memberships = _current_memberships(conn, asof=asof)
    market = _latest_market_rows(conn, asof=asof)
    current_financial = _latest_financial_rows(conn, asof=asof)
    financial = _canonical_revenue_period_rows(
        conn,
        asof=asof,
        current_financial=current_financial,
    )
    thresholds = policy.thresholds
    output: list[dict[str, str]] = []
    for membership in memberships:
        ticker = _ticker(membership.get("ticker"))
        periods = financial.get(ticker, [])
        latest_financial = current_financial.get(ticker, {})
        validated_zero_evidence = _validated_zero_revenue_evidence(
            latest_financial
        )
        display_periods = (
            periods
            if periods
            else [validated_zero_evidence]
            if validated_zero_evidence is not None
            else []
        )
        current_market = market.get(ticker, {})
        row: dict[str, object] = {
            **membership,
            **latest_financial,
            **current_market,
            "ticker": ticker,
        }
        state = resolve_lifecycle_state(row, asof=asof, policy=policy)
        current_class = state["lifecycle_class"]
        revenue_classification = effective_revenue_classification(
            policy,
            ticker=ticker,
            asof=asof,
        )
        emerging_streak = _period_streak(
            periods,
            threshold=thresholds.emerging_revenue_usd,
            confidence=thresholds.emerging_financial_confidence,
            comparison="minimum",
            maximum_gap_days=thresholds.maximum_period_gap_days,
        )
        established_streak = _period_streak(
            periods,
            threshold=thresholds.established_revenue_usd,
            confidence=thresholds.established_financial_confidence,
            comparison="minimum",
            maximum_gap_days=thresholds.maximum_period_gap_days,
        )
        demotion_threshold = (
            thresholds.established_demotion_revenue_usd
            if current_class == ESTABLISHED_OPERATING
            else thresholds.emerging_demotion_revenue_usd
        )
        demotion_streak = _period_streak(
            periods,
            threshold=demotion_threshold,
            confidence=thresholds.emerging_financial_confidence,
            comparison="maximum",
            maximum_gap_days=thresholds.maximum_period_gap_days,
        )
        listing_start = _text(membership.get("membership_start_date"))
        listed_days = (
            (target - _parse_date(listing_start, field="membership_start_date")).days
            if listing_start
            else -1
        )
        suggested = current_class
        status = "NO_CHANGE"
        reasons: list[str] = []
        if state["lifecycle_hard_event_veto_flag"] == "1":
            status = "HARD_EVENT_VETO_ACTIVE"
            reasons.append(state["lifecycle_investability_reason"])
        elif current_class == PRE_COMMERCIAL:
            if (
                established_streak >= thresholds.established_quarters
                and listed_days >= thresholds.established_min_listed_days
            ):
                suggested = ESTABLISHED_OPERATING
            elif emerging_streak >= thresholds.emerging_quarters:
                suggested = COMMERCIAL_EMERGING
            if suggested != current_class:
                status = REVIEW_REQUIRED
                if revenue_classification != VALIDATED_CUSTOMER_REVENUE:
                    reasons.append("commercial_revenue_not_ratified")
                if suggested == COMMERCIAL_EMERGING:
                    reasons.extend(
                        emerging_risk_gate_reasons(
                            row,
                            thresholds=thresholds,
                        )
                    )
                dilution = _float(row.get("diluted_shares_yoy_growth"))
                if dilution is not None and abs(dilution) > 5.0:
                    reasons.append("diluted_shares_growth_data_anomaly")
                if reasons:
                    status = "BLOCKED_PENDING_REVIEW"
        elif current_class == COMMERCIAL_EMERGING:
            if (
                established_streak >= thresholds.established_quarters
                and listed_days >= thresholds.established_min_listed_days
            ):
                suggested = ESTABLISHED_OPERATING
                status = REVIEW_REQUIRED
                if revenue_classification != VALIDATED_CUSTOMER_REVENUE:
                    reasons.append("commercial_revenue_not_ratified")
                    status = "BLOCKED_PENDING_REVIEW"
            elif demotion_streak >= thresholds.demotion_quarters:
                suggested = PRE_COMMERCIAL
                status = REVIEW_REQUIRED
                reasons.append("emerging_revenue_below_demotion_threshold")
        elif (
            current_class == ESTABLISHED_OPERATING
            and demotion_streak >= thresholds.demotion_quarters
        ):
            suggested = COMMERCIAL_EMERGING
            status = REVIEW_REQUIRED
            reasons.append("established_revenue_below_demotion_threshold")

        if (
            status == "NO_CHANGE"
            and current_class == PRE_COMMERCIAL
            and validated_zero_evidence is not None
        ):
            reasons.append("validated_precommercial_zero_revenue")

        latest = display_periods[0] if display_periods else {}
        output.append(
            {
                "asof_date": asof,
                "ticker": ticker,
                "company_name": _text(membership.get("company_name")),
                "calibration_cohort": _text(
                    membership.get("calibration_cohort")
                ),
                "development_stage": _text(
                    membership.get("development_stage")
                ),
                "current_lifecycle_class": current_class,
                "suggested_lifecycle_class": suggested,
                "candidate_status": status,
                "candidate_reasons": (
                    ";".join(dict.fromkeys(reasons))
                    if reasons
                    else "criteria_not_met"
                    if status == "NO_CHANGE"
                    else "mechanical_criteria_met_human_ratification_required"
                ),
                "revenue_classification": revenue_classification,
                "commercial_revenue_quarter_streak": str(emerging_streak),
                "established_revenue_quarter_streak": str(
                    established_streak
                ),
                "demotion_revenue_quarter_streak": str(demotion_streak),
                "latest_fiscal_period_end": _text(
                    latest.get("fiscal_period_end")
                ),
                "latest_revenue_ttm_usd": _format_float(
                    latest.get("revenue_ttm_usd")
                ),
                "latest_financial_confidence": _format_float(
                    latest.get("financial_confidence")
                ),
                "latest_data_quality_status": _text(
                    latest.get("data_quality_status")
                ),
                "listing_start_date": listing_start,
                "listed_days": str(listed_days) if listed_days >= 0 else "",
                "avg_dollar_volume_60d": _format_float(
                    row.get("avg_dollar_volume_60d")
                ),
                "capital_raise_dependence": _format_float(
                    row.get("capital_raise_dependence")
                ),
                "cash_runway_years": _format_float(
                    row.get("cash_runway_years")
                ),
                "diluted_shares_yoy_growth": _format_float(
                    row.get("diluted_shares_yoy_growth")
                ),
                "hard_event_vetoes": state["lifecycle_hard_event_types"],
                "evidence_periods_json": _evidence_periods(
                    display_periods,
                    limit=max(
                        thresholds.established_quarters,
                        thresholds.emerging_quarters,
                        thresholds.demotion_quarters,
                    ),
                ),
                "evidence_artifact": "",
                "evidence_sha256": "",
                "policy_version": policy.policy_version,
            }
        )
    return output


def evaluate_lifecycle_shadow(
    rows: Sequence[Mapping[str, object]],
    *,
    asof: str,
    policy: LifecyclePolicy,
) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for source in rows:
        state = resolve_lifecycle_state(source, asof=asof, policy=policy)
        stage = _text(source.get("development_stage")).lower()
        cohort = _text(source.get("calibration_cohort"))
        operating_only = (
            stage not in {"development", "development_stage"}
            and cohort != "development_stage_emerging_machinery"
        )
        rank_ready = (
            _text(source.get("rank_ready_flag")) == "1"
            and _text(source.get("model_status")) == "complete"
            and _text(source.get("rank_ready_reason")) == "ok"
        )
        lifecycle_eligible = (
            rank_ready
            and state["lifecycle_investability_eligible_flag"] == "1"
        )
        record = {
            "asof_date": asof,
            "ticker": _ticker(source.get("ticker")),
            "final_rank": _text(source.get("final_rank")),
            "final_score": _text(source.get("final_score")),
            "calibration_cohort": cohort,
            "development_stage": _text(source.get("development_stage")),
            "rank_ready_flag": "1" if rank_ready else "0",
            "operating_only_eligible_flag": (
                "1" if rank_ready and operating_only else "0"
            ),
            **state,
            "lifecycle_universe_eligible_flag": (
                "1" if lifecycle_eligible else "0"
            ),
            "eligibility_changed_flag": (
                "1"
                if lifecycle_eligible != (rank_ready and operating_only)
                else "0"
            ),
        }
        output.append({field: record.get(field, "") for field in SHADOW_FIELDS})
    return sorted(
        output,
        key=lambda row: (
            int(row.get("final_rank") or "999999"),
            row["ticker"],
        ),
    )

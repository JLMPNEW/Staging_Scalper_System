from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from dedicated_parser.contracts import stable_hash
from technology.core.db import utc_now
from technology.software_infrastructure.software_metric_governance import (
    EVENT_METRICS,
    MODEL_FAMILY,
    RECONCILIATION_METRICS,
)


METRIC_VERSION = "software_specialized_measurement_v1"
SOURCE_ID = "software_adjudication_v1"
MONETARY_METRICS = frozenset(
    {
        "remaining_performance_obligation",
        "current_remaining_performance_obligation",
        "deferred_revenue_current",
        "deferred_revenue_noncurrent",
        "deferred_revenue_total",
        "annual_recurring_revenue",
        "subscription_revenue",
        "disclosed_billings",
    }
)
DERIVED_SIGNAL_SPECS = {
    "annual_recurring_revenue_yoy_growth": "ratio",
    "annual_recurring_revenue_to_revenue": "ratio",
    "net_revenue_retention_level": "ratio",
    "net_revenue_retention_yoy_change": "ratio",
    "disclosed_billings_yoy_growth": "ratio",
    "subscription_revenue_yoy_growth": "ratio",
    "subscription_revenue_mix": "ratio",
    "customer_threshold_disclosure_change": "event",
}
METRIC_DERIVED_SIGNALS = {
    "annual_recurring_revenue": (
        "annual_recurring_revenue_yoy_growth",
        "annual_recurring_revenue_to_revenue",
    ),
    "net_revenue_retention": (
        "net_revenue_retention_level",
        "net_revenue_retention_yoy_change",
    ),
    "disclosed_billings": ("disclosed_billings_yoy_growth",),
    "subscription_revenue": (
        "subscription_revenue_yoy_growth",
        "subscription_revenue_mix",
    ),
    "customer_count_threshold": ("customer_threshold_disclosure_change",),
}
PERIOD_MAX_AGE_DAYS = {
    "quarterly": 200,
    "annual": 460,
    "instant": 460,
}
DEFINITION_VERSION = {
    "annual_recurring_revenue": "arr_v1",
    "net_revenue_retention": "nrr_v1",
    "subscription_revenue": "subscription_revenue_v1",
    "disclosed_billings": "disclosed_billings_v1",
    "remaining_performance_obligation": "rpo_v1",
    "current_remaining_performance_obligation": "crpo_v1",
    "deferred_revenue_current": "deferred_revenue_current_v1",
    "deferred_revenue_noncurrent": "deferred_revenue_noncurrent_v1",
    "deferred_revenue_total": "deferred_revenue_total_v1",
    "customer_count_threshold": "customer_count_threshold_event_v1",
}


@dataclass(frozen=True)
class PlausibilityThresholds:
    arr_to_revenue_min: float = 0.05
    arr_to_revenue_max: float = 5.0
    rpo_to_revenue_min: float = 0.0
    rpo_to_revenue_max: float = 10.0
    nrr_min: float = 0.50
    nrr_max: float = 2.00
    subscription_revenue_mix_max: float = 1.20
    billings_identity_relative_tolerance: float = 0.30
    structured_reconciliation_relative_tolerance: float = 0.15
    structured_reconciliation_absolute_tolerance: float = 1_000_000.0


@dataclass(frozen=True)
class SpecializedFact:
    ticker: str
    cik: str
    metric_name: str
    value: float
    unit: str
    period_start: str
    period_end: str
    availability_datetime: str
    filing_date: str
    accession_number: str
    form_type: str
    source_document: str
    source_document_sha256: str
    evidence_key: str
    confidence: float
    status_reason: str
    definition_version: str
    provenance: dict[str, Any]


@dataclass(frozen=True)
class DerivedSignal:
    value: float | None
    source_availability_datetime: str
    definition_version: str
    availability_status: str
    status_reason: str


def validate_policy_payload(
    payload: dict[str, Any],
    *,
    source: str = "<memory>",
) -> dict[str, Any]:
    if not str(payload.get("policy_id") or "").strip():
        raise ValueError(f"Policy policy_id is required: {source}")
    if not str(payload.get("release_id") or "").strip():
        raise ValueError(f"Policy release_id is required: {source}")
    decisions = payload.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise ValueError(f"Invalid software metric policy: {source}")
    try:
        declared_count = int(payload.get("decision_count") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("Policy decision_count must be an integer") from exc
    if declared_count != len(decisions):
        raise ValueError(
            "Policy decision_count does not match the decisions array"
        )
    previous = "0" * 64
    evidence_keys: set[str] = set()
    for sequence, decision in enumerate(decisions, start=1):
        if int(decision.get("sequence") or 0) != sequence:
            raise ValueError(f"Policy sequence mismatch at row {sequence}")
        if decision.get("previous_decision_hash") != previous:
            raise ValueError(f"Policy chain predecessor mismatch at row {sequence}")
        candidate = dict(decision)
        expected_hash = str(candidate.pop("decision_hash", ""))
        actual_hash = stable_hash(candidate)
        if actual_hash != expected_hash:
            raise ValueError(f"Policy decision hash mismatch at row {sequence}")
        evidence_key = str(decision.get("source_evidence_key") or "")
        if not evidence_key or evidence_key in evidence_keys:
            raise ValueError(
                f"Policy evidence key missing or duplicated at row {sequence}"
            )
        evidence_keys.add(evidence_key)
        previous = expected_hash
    if payload.get("chain_root_sha256") != previous:
        raise ValueError("Policy chain root mismatch")
    declared_counts = payload.get("decision_counts")
    if declared_counts is not None:
        actual_counts = dict(
            Counter(str(row.get("decision") or "") for row in decisions)
        )
        if declared_counts != actual_counts:
            raise ValueError("Policy decision_counts do not match decisions")
    return payload


def load_policy(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return validate_policy_payload(payload, source=str(path))


def _financial_rows(
    conn: sqlite3.Connection,
    *,
    tickers: Iterable[str],
) -> dict[str, list[dict[str, Any]]]:
    names = sorted(set(tickers))
    if not names:
        return {}
    placeholders = ",".join("?" for _ in names)
    rows = conn.execute(
        f"""
        SELECT ticker, asof_date, fiscal_period_end, financial_frequency,
               revenue, revenue_ttm, deferred_revenue,
               remaining_performance_obligation, accession_number
        FROM feature_financial_statement
        WHERE model_family = ?
          AND ticker IN ({placeholders})
        ORDER BY ticker, fiscal_period_end, asof_date
        """,
        (MODEL_FAMILY, *names),
    ).fetchall()
    output: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        output[str(row["ticker"])].append(dict(row))
    return output


def _matching_financial(
    rows: list[dict[str, Any]],
    *,
    period_end: str,
    availability_datetime: str,
) -> dict[str, Any] | None:
    visible = [
        row
        for row in rows
        if str(row.get("asof_date") or "") <= availability_datetime[:10]
        and str(row.get("fiscal_period_end") or "") == period_end
    ]
    return max(
        visible,
        key=lambda row: (
            str(row.get("fiscal_period_end") or ""),
            str(row.get("asof_date") or ""),
        ),
        default=None,
    )


def _prior_financial(
    rows: list[dict[str, Any]],
    *,
    current: dict[str, Any],
    period_kind: str,
    availability_datetime: str,
) -> dict[str, Any] | None:
    current_end = _date(current.get("fiscal_period_end"))
    if current_end is None:
        return None
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("asof_date") or "") > availability_datetime[:10]:
            continue
        prior_end = _date(row.get("fiscal_period_end"))
        if prior_end is None or prior_end >= current_end:
            continue
        gap = (current_end - prior_end).days
        valid_gap = (
            300 <= gap <= 460
            if period_kind == "annual"
            else 60 <= gap <= 130
        )
        if valid_gap:
            candidates.append(row)
    return max(
        candidates,
        key=lambda row: (
            str(row.get("fiscal_period_end") or ""),
            str(row.get("asof_date") or ""),
        ),
        default=None,
    )


def _date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _close(
    left: float,
    right: float,
    *,
    relative_tolerance: float,
    absolute_tolerance: float,
) -> bool:
    return abs(left - right) <= max(
        absolute_tolerance,
        max(abs(left), abs(right)) * relative_tolerance,
    )


def adjudicated_facts(
    conn: sqlite3.Connection,
    *,
    policy: dict[str, Any],
    thresholds: PlausibilityThresholds,
) -> tuple[list[SpecializedFact], list[dict[str, Any]]]:
    accepted = [
        row
        for row in policy["decisions"]
        if row["decision"] in {"ACCEPTED", "CORRECTED"}
    ]
    financial = _financial_rows(
        conn,
        tickers=(str(row["ticker"]) for row in accepted),
    )
    facts: list[SpecializedFact] = []
    reconciliation: list[dict[str, Any]] = []
    for decision in accepted:
        metric = str(decision["effective_metric"])
        value = float(decision["effective_value"])
        unit = str(decision["effective_unit"] or "").upper()
        ticker = str(decision["ticker"])
        period_end = str(decision["effective_period_end"])
        availability = str(decision["accepted_at"])
        fin = _matching_financial(
            financial.get(ticker, []),
            period_end=period_end,
            availability_datetime=availability,
        )
        revenue = _number(fin.get("revenue")) if fin else None
        revenue_ttm = _number(fin.get("revenue_ttm")) if fin else None
        structured_value = None
        if fin and metric == "remaining_performance_obligation":
            structured_value = _number(
                fin.get("remaining_performance_obligation")
            )
        elif fin and metric == "deferred_revenue_total":
            structured_value = _number(fin.get("deferred_revenue"))

        gate_status = "PROSE_PRIMARY_PLAUSIBLE"
        gate_reason = "manual_adjudication_and_plausibility_passed"
        source_role = "prose_primary"
        materialize = True
        ratio = None
        identity_value = None
        if metric in RECONCILIATION_METRICS:
            source_role = "structured_reconciliation"
            if structured_value is None:
                gate_status = "PROSE_GAP_FILL"
                gate_reason = "structured_metric_missing_for_same_period"
            elif _close(
                value,
                structured_value,
                relative_tolerance=(
                    thresholds.structured_reconciliation_relative_tolerance
                ),
                absolute_tolerance=(
                    thresholds.structured_reconciliation_absolute_tolerance
                ),
            ):
                gate_status = "PROSE_MATCHES_STRUCTURED"
                gate_reason = "prose_value_reconciles_to_structured_primary"
            else:
                gate_status = "PROSE_CONFLICTS_STRUCTURED"
                gate_reason = "prose_value_conflicts_with_structured_primary"
                materialize = False
        if metric in MONETARY_METRICS and unit not in {"USD"}:
            gate_status = "REVIEW_FX_NORMALIZATION_REQUIRED"
            gate_reason = "non_usd_metric_requires_pit_fx_normalization"
            materialize = False
        elif metric == "annual_recurring_revenue" and revenue_ttm:
            ratio = value / revenue_ttm
            if not (
                thresholds.arr_to_revenue_min
                <= ratio
                <= thresholds.arr_to_revenue_max
            ):
                gate_status = "REJECTED_PLAUSIBILITY"
                gate_reason = "arr_to_revenue_outside_plausible_band"
                materialize = False
        elif metric == "remaining_performance_obligation" and revenue_ttm:
            ratio = value / revenue_ttm
            if not (
                thresholds.rpo_to_revenue_min
                <= ratio
                <= thresholds.rpo_to_revenue_max
            ):
                gate_status = "REJECTED_PLAUSIBILITY"
                gate_reason = "rpo_to_revenue_outside_plausible_band"
                materialize = False
        elif metric == "net_revenue_retention":
            if not thresholds.nrr_min <= value <= thresholds.nrr_max:
                gate_status = "REJECTED_PLAUSIBILITY"
                gate_reason = "nrr_outside_plausible_band"
                materialize = False
        elif metric == "subscription_revenue" and revenue:
            ratio = value / revenue
            if not 0 <= ratio <= thresholds.subscription_revenue_mix_max:
                gate_status = "REJECTED_PLAUSIBILITY"
                gate_reason = "subscription_revenue_mix_outside_band"
                materialize = False
        elif metric == "disclosed_billings" and fin:
            prior = _prior_financial(
                financial.get(ticker, []),
                current=fin,
                period_kind=str(decision["period_kind"]),
                availability_datetime=availability,
            )
            current_deferred = _number(fin.get("deferred_revenue"))
            prior_deferred = (
                _number(prior.get("deferred_revenue")) if prior else None
            )
            if (
                revenue is not None
                and current_deferred is not None
                and prior_deferred is not None
            ):
                identity_value = revenue + current_deferred - prior_deferred
                if identity_value > 0:
                    relative_gap = abs(value - identity_value) / identity_value
                    if (
                        relative_gap
                        > thresholds.billings_identity_relative_tolerance
                    ):
                        gate_status = "REJECTED_PLAUSIBILITY"
                        gate_reason = (
                            "billings_conflicts_with_revenue_plus_deferred_change"
                        )
                        materialize = False
            else:
                gate_status = "REVIEW_MISSING_ACCOUNTING_ANCHOR"
                gate_reason = "billings_identity_inputs_unavailable"
                materialize = False
        elif metric in EVENT_METRICS:
            source_role = "censored_disclosure_event"
            gate_status = "EVENT_ONLY"
            gate_reason = "censored_threshold_not_numeric_cross_section"

        calibration_eligible = int(
            decision["calibration_eligible_flag"]
        )
        if not calibration_eligible and metric not in EVENT_METRICS:
            materialize = False
            gate_status = "DIAGNOSTIC_SCOPE_ONLY"
            gate_reason = "segment_or_noncomparable_definition"
        row = {
            "source_evidence_key": decision["source_evidence_key"],
            "decision_sequence": decision.get("sequence", 0),
            "ticker": ticker,
            "metric_name": metric,
            "period_end": period_end,
            "availability_datetime": availability,
            "decision": decision["decision"],
            "source_role": source_role,
            "prose_value": value,
            "structured_value": structured_value,
            "plausibility_ratio": ratio,
            "billings_identity_value": identity_value,
            "gate_status": gate_status,
            "gate_reason": gate_reason,
            "materialized_flag": int(materialize),
            "calibration_eligible_flag": calibration_eligible,
            "definition_variant": decision["definition_variant"],
            "source_unit": unit,
        }
        reconciliation.append(row)
        if not materialize:
            continue
        provenance = {
            "release_id": policy["release_id"],
            "policy_id": policy["policy_id"],
            "policy_chain_root_sha256": policy["chain_root_sha256"],
            "decision_hash": decision["decision_hash"],
            "source_role": source_role,
            "gate_status": gate_status,
            "gate_reason": gate_reason,
            "structured_value": structured_value,
            "plausibility_ratio": ratio,
            "billings_identity_value": identity_value,
            "period_kind": decision["period_kind"],
            "definition_variant": decision["definition_variant"],
            "effective_scope": decision["effective_scope"],
            "calibration_eligible_flag": calibration_eligible,
            "governance_status": str(
                decision.get("governance_status") or "HUMAN_APPROVED"
            ),
            "production_use_prohibited_flag": int(
                decision.get("production_use_prohibited_flag") or 0
            ),
            "censored_flag": int(metric in EVENT_METRICS),
        }
        facts.append(
            SpecializedFact(
                ticker=ticker,
                cik=str(decision["cik"]),
                metric_name=metric,
                value=value,
                unit=str(decision["effective_unit"]),
                period_start=str(decision["effective_period_start"]),
                period_end=period_end,
                availability_datetime=availability,
                filing_date=str(decision["filing_date"]),
                accession_number=str(decision["accession_number"]),
                form_type=str(decision["form_type"]),
                source_document=str(decision["source_document"]),
                source_document_sha256=str(
                    decision["source_document_sha256"]
                ),
                evidence_key=str(decision["source_evidence_key"]),
                confidence=1.0 if decision["decision"] == "ACCEPTED" else 0.95,
                status_reason=gate_reason,
                definition_version=DEFINITION_VERSION[metric],
                provenance=provenance,
            )
        )
    return facts, reconciliation


def upsert_facts(
    conn: sqlite3.Connection,
    *,
    facts: list[SpecializedFact],
) -> int:
    now = utc_now()
    rows = []
    for fact in facts:
        if len(fact.source_document_sha256) != 64:
            raise ValueError(
                "Specialized fact source document is not SHA-256 sealed: "
                f"{fact.ticker} {fact.metric_name} {fact.source_document}"
            )
        payload = {
            "model_family": MODEL_FAMILY,
            "ticker": fact.ticker,
            "metric_name": fact.metric_name,
            "metric_version": METRIC_VERSION,
            "period_start": fact.period_start,
            "period_end": fact.period_end,
            "source_id": SOURCE_ID,
            "evidence_key": fact.evidence_key,
        }
        rows.append(
            (
                stable_hash(payload),
                MODEL_FAMILY,
                fact.ticker,
                fact.cik,
                fact.metric_name,
                METRIC_VERSION,
                fact.value,
                "",
                fact.unit,
                fact.period_start,
                fact.period_end,
                fact.availability_datetime,
                fact.filing_date,
                fact.accession_number,
                fact.form_type,
                fact.source_document,
                fact.source_document_sha256,
                SOURCE_ID,
                fact.evidence_key,
                "software_policy_adjudication",
                fact.confidence,
                0,
                fact.status_reason,
                fact.definition_version,
                json.dumps(fact.provenance, sort_keys=True),
                now,
                now,
            )
        )
    conn.executemany(
        """
        DELETE FROM fact_technology_specialized_metric
        WHERE model_family = ?
          AND metric_version = ?
          AND source_id = ?
          AND evidence_key = ?
          AND specialized_fact_key <> ?
        """,
        [
            (
                MODEL_FAMILY,
                METRIC_VERSION,
                SOURCE_ID,
                fact.evidence_key,
                row[0],
            )
            for fact, row in zip(facts, rows, strict=True)
        ],
    )
    conn.executemany(
        """
        INSERT INTO fact_technology_specialized_metric(
            specialized_fact_key, model_family, ticker, cik, metric_name,
            metric_version, value, value_text, unit, period_start, period_end,
            availability_datetime, filing_date, accession_number, form_type,
            source_document, source_document_sha256, source_id, evidence_key,
            extraction_method, confidence, review_required_flag,
            status_reason, definition_version, provenance_json,
            created_at, updated_at
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(specialized_fact_key) DO UPDATE SET
            value=excluded.value,
            unit=excluded.unit,
            availability_datetime=excluded.availability_datetime,
            status_reason=excluded.status_reason,
            provenance_json=excluded.provenance_json,
            updated_at=excluded.updated_at
        """,
        rows,
    )
    return len(rows)


def _membership(
    conn: sqlite3.Connection,
) -> tuple[
    dict[str, list[tuple[date, date | None, str]]],
    dict[str, str],
]:
    intervals: dict[str, list[tuple[date, date | None, str]]] = defaultdict(
        list
    )
    cohorts: dict[str, str] = {}
    rows = conn.execute(
        """
        SELECT m.ticker, m.start_date, m.end_date, m.membership_status,
               COALESCE(t.calibration_cohort_id, '') AS cohort_id
        FROM dim_universe_membership AS m
        LEFT JOIN dim_technology_taxonomy AS t
          ON t.model_family = m.model_family
         AND t.ticker = m.ticker
        WHERE m.model_family = ?
          AND m.point_in_time_flag = 1
          AND m.membership_status IN ('active', 'historical', 'inactive', 'review')
        ORDER BY m.ticker, m.start_date
        """,
        (MODEL_FAMILY,),
    ).fetchall()
    for row in rows:
        start = _date(row["start_date"])
        if start is None:
            continue
        ticker = str(row["ticker"])
        intervals[ticker].append(
            (start, _date(row["end_date"]), str(row["membership_status"]))
        )
        cohorts[ticker] = str(row["cohort_id"])
    return dict(intervals), cohorts


def _member_status(
    intervals: list[tuple[date, date | None, str]],
    asof: date,
) -> str:
    for start, end, status in intervals:
        if start <= asof and (end is None or asof <= end):
            return status
    return ""


def _price_dates(
    conn: sqlite3.Connection,
    *,
    start_date: str,
    end_date: str,
) -> list[date]:
    rows = conn.execute(
        """
        SELECT DISTINCT bar_date
        FROM fact_price_ohlcv
        WHERE ticker = 'QQQ'
          AND bar_date BETWEEN ? AND ?
        ORDER BY bar_date
        """,
        (start_date, end_date),
    ).fetchall()
    return [parsed for row in rows if (parsed := _date(row["bar_date"]))]


def _fact_history(
    conn: sqlite3.Connection,
) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rows = conn.execute(
        """
        SELECT *
        FROM fact_technology_specialized_metric
        WHERE model_family = ?
          AND metric_version = ?
          AND source_id = ?
          AND review_required_flag = 0
        ORDER BY ticker, availability_datetime, period_end
        """,
        (MODEL_FAMILY, METRIC_VERSION, SOURCE_ID),
    ).fetchall()
    for row in rows:
        item = dict(row)
        item["provenance"] = json.loads(
            str(item.get("provenance_json") or "{}")
        )
        output[str(row["ticker"])].append(item)
    return dict(output)


def _visible_facts(
    rows: list[dict[str, Any]],
    *,
    asof: date,
) -> list[dict[str, Any]]:
    latest_by_period: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    asof_iso = asof.isoformat()
    for row in rows:
        if str(row["availability_datetime"])[:10] > asof_iso:
            continue
        provenance = row["provenance"]
        if not int(provenance.get("calibration_eligible_flag") or 0):
            continue
        key = (
            str(row["metric_name"]),
            str(provenance.get("definition_variant") or ""),
            str(provenance.get("period_kind") or ""),
            str(row["period_end"]),
        )
        previous = latest_by_period.get(key)
        if previous is None or str(row["availability_datetime"]) > str(
            previous["availability_datetime"]
        ):
            latest_by_period[key] = row
    return sorted(
        latest_by_period.values(),
        key=lambda row: (
            str(row["metric_name"]),
            str(row["period_end"]),
            str(row["availability_datetime"]),
        ),
    )


def _latest(
    rows: list[dict[str, Any]],
    *,
    metric: str,
    variant: str = "",
    period_kind: str = "",
) -> dict[str, Any] | None:
    candidates = [
        row
        for row in rows
        if row["metric_name"] == metric
        and (
            not variant
            or row["provenance"].get("definition_variant") == variant
        )
        and (
            not period_kind
            or row["provenance"].get("period_kind") == period_kind
        )
    ]
    return max(
        candidates,
        key=lambda row: (
            str(row["period_end"]),
            str(row["availability_datetime"]),
        ),
        default=None,
    )


def _year_ago(
    rows: list[dict[str, Any]],
    *,
    latest: dict[str, Any],
) -> dict[str, Any] | None:
    latest_end = _date(latest["period_end"])
    if latest_end is None:
        return None
    provenance = latest["provenance"]
    candidates = []
    for row in rows:
        if row["metric_name"] != latest["metric_name"]:
            continue
        other = row["provenance"]
        if other.get("definition_variant") != provenance.get(
            "definition_variant"
        ):
            continue
        if other.get("period_kind") != provenance.get("period_kind"):
            continue
        period_end = _date(row["period_end"])
        if period_end is None:
            continue
        if 300 <= (latest_end - period_end).days <= 460:
            candidates.append(row)
    return max(
        candidates,
        key=lambda row: (
            str(row["period_end"]),
            str(row["availability_datetime"]),
        ),
        default=None,
    )


def _growth(
    rows: list[dict[str, Any]],
    *,
    metric: str,
    variant: str = "",
) -> tuple[float | None, str]:
    latest = _latest(rows, metric=metric, variant=variant)
    if latest is None:
        return None, ""
    prior = _year_ago(rows, latest=latest)
    current_value = _number(latest["value"])
    prior_value = _number(prior["value"]) if prior else None
    if (
        current_value is None
        or prior_value is None
        or prior_value <= 0
    ):
        return None, str(latest["availability_datetime"])
    return current_value / prior_value - 1.0, str(
        latest["availability_datetime"]
    )


def _matching_revenue(
    financial: list[dict[str, Any]],
    *,
    period_end: str,
    availability_datetime: str,
    period_kind: str,
) -> tuple[float | None, str]:
    row = _matching_financial(
        financial,
        period_end=period_end,
        availability_datetime=availability_datetime,
    )
    if row is None:
        return None, ""
    revenue = _number(row.get("revenue"))
    revenue_ttm = _number(row.get("revenue_ttm"))
    if period_kind == "annual":
        value = revenue_ttm
    elif period_kind == "quarterly":
        # Some annual filings are represented as quarterly_sec upstream.
        # Fail closed unless period revenue is demonstrably distinct from TTM.
        value = (
            revenue
            if revenue is not None
            and (revenue_ttm is None or revenue_ttm > revenue * 1.25)
            else None
        )
    else:
        value = None
    return value, str(row.get("asof_date") or "")


def _stale_reason(
    row: dict[str, Any],
    *,
    asof: date,
) -> str:
    period_end = _date(row.get("period_end"))
    if period_end is None:
        return "metric_period_end_missing"
    period_kind = str(
        row.get("provenance", {}).get("period_kind") or ""
    )
    max_age = PERIOD_MAX_AGE_DAYS.get(period_kind)
    if max_age is None:
        return "metric_period_kind_unsupported"
    age_days = (asof - period_end).days
    if age_days > max_age:
        return (
            f"metric_stale:{period_kind}:age_days={age_days}:"
            f"max_age_days={max_age}"
        )
    return ""


def _missing_signal(
    definition_version: str,
    *,
    source_asof: str = "",
    reason: str,
) -> DerivedSignal:
    return DerivedSignal(
        value=None,
        source_availability_datetime=source_asof,
        definition_version=definition_version,
        availability_status="MISSING_INPUT",
        status_reason=reason,
    )


def _stale_signal(
    row: dict[str, Any],
    *,
    definition_version: str,
    reason: str,
) -> DerivedSignal:
    return DerivedSignal(
        value=None,
        source_availability_datetime=str(row["availability_datetime"]),
        definition_version=definition_version,
        availability_status="STALE_PIT",
        status_reason=reason,
    )


def _available_signal(
    value: float,
    *,
    source_asof: str,
    definition_version: str,
) -> DerivedSignal:
    return DerivedSignal(
        value=value,
        source_availability_datetime=source_asof,
        definition_version=definition_version,
        availability_status="AVAILABLE_PIT",
        status_reason="sealed_adjudication_pit_feature",
    )


def derive_signals(
    *,
    visible: list[dict[str, Any]],
    financial: list[dict[str, Any]],
    asof: date,
) -> dict[str, DerivedSignal]:
    output: dict[str, DerivedSignal] = {}

    def growth_signal(
        *,
        metric: str,
        variant: str,
        definition_version: str,
    ) -> DerivedSignal:
        latest = _latest(visible, metric=metric, variant=variant)
        if latest is None:
            return _missing_signal(
                definition_version,
                reason="current_metric_observation_missing",
            )
        source_asof = str(latest["availability_datetime"])
        stale = _stale_reason(latest, asof=asof)
        if stale:
            return _stale_signal(
                latest,
                definition_version=definition_version,
                reason=stale,
            )
        prior = _year_ago(visible, latest=latest)
        current_value = _number(latest["value"])
        prior_value = _number(prior["value"]) if prior else None
        if (
            current_value is None
            or prior_value is None
            or prior_value <= 0
        ):
            return _missing_signal(
                definition_version,
                source_asof=source_asof,
                reason="same_definition_year_ago_pair_missing",
            )
        return _available_signal(
            current_value / prior_value - 1.0,
            source_asof=source_asof,
            definition_version=definition_version,
        )

    output["annual_recurring_revenue_yoy_growth"] = growth_signal(
        metric="annual_recurring_revenue",
        variant="total_arr",
        definition_version="arr_growth_v1",
    )
    arr = _latest(
        visible,
        metric="annual_recurring_revenue",
        variant="total_arr",
    )
    arr_ratio = _missing_signal(
        "arr_to_revenue_v1",
        reason="current_metric_observation_missing",
    )
    if arr is not None:
        stale = _stale_reason(arr, asof=asof)
        if stale:
            arr_ratio = _stale_signal(
                arr,
                definition_version="arr_to_revenue_v1",
                reason=stale,
            )
        else:
            fin = _matching_financial(
                financial,
                period_end=str(arr["period_end"]),
                availability_datetime=asof.isoformat(),
            )
            revenue_ttm = _number(fin.get("revenue_ttm")) if fin else None
            arr_value = _number(arr.get("value"))
            if (
                fin is not None
                and arr_value is not None
                and revenue_ttm is not None
                and revenue_ttm > 0
            ):
                financial_asof = str(fin.get("asof_date") or "")
                source_asof = max(
                    str(arr["availability_datetime"]),
                    (
                        f"{financial_asof}T23:59:59Z"
                        if financial_asof
                        else ""
                    ),
                )
                arr_ratio = _available_signal(
                    arr_value / revenue_ttm,
                    source_asof=source_asof,
                    definition_version="arr_to_revenue_v1",
                )
            else:
                arr_ratio = _missing_signal(
                    "arr_to_revenue_v1",
                    source_asof=str(arr["availability_datetime"]),
                    reason="matching_period_revenue_ttm_missing",
                )
    output["annual_recurring_revenue_to_revenue"] = arr_ratio
    nrr = _latest(
        visible,
        metric="net_revenue_retention",
        variant="dollar_based_net_retention",
    )
    nrr_value = _number(nrr["value"]) if nrr else None
    nrr_asof = str(nrr["availability_datetime"]) if nrr else ""
    nrr_stale = _stale_reason(nrr, asof=asof) if nrr else ""
    if nrr is None or nrr_value is None:
        output["net_revenue_retention_level"] = _missing_signal(
            "nrr_level_v1",
            reason="current_metric_observation_missing",
        )
    elif nrr_stale:
        output["net_revenue_retention_level"] = _stale_signal(
            nrr,
            definition_version="nrr_level_v1",
            reason=nrr_stale,
        )
    else:
        output["net_revenue_retention_level"] = _available_signal(
            nrr_value,
            source_asof=nrr_asof,
            definition_version="nrr_level_v1",
        )
    nrr_change = None
    if nrr is not None and not nrr_stale:
        prior_nrr = _year_ago(visible, latest=nrr)
        prior_value = _number(prior_nrr["value"]) if prior_nrr else None
        if nrr_value is not None and prior_value is not None:
            nrr_change = nrr_value - prior_value
    if nrr is not None and nrr_stale:
        output["net_revenue_retention_yoy_change"] = _stale_signal(
            nrr,
            definition_version="nrr_change_v1",
            reason=nrr_stale,
        )
    elif nrr_change is None:
        output["net_revenue_retention_yoy_change"] = _missing_signal(
            "nrr_change_v1",
            source_asof=nrr_asof,
            reason="same_definition_year_ago_pair_missing",
        )
    else:
        output["net_revenue_retention_yoy_change"] = _available_signal(
            nrr_change,
            source_asof=nrr_asof,
            definition_version="nrr_change_v1",
        )
    output["disclosed_billings_yoy_growth"] = growth_signal(
        metric="disclosed_billings",
        variant="reported_billings",
        definition_version="disclosed_billings_growth_v1",
    )
    output["subscription_revenue_yoy_growth"] = growth_signal(
        metric="subscription_revenue",
        variant="total_subscription_revenue",
        definition_version="subscription_revenue_growth_v1",
    )
    subscription_candidates = sorted(
        (
            row
            for row in visible
            if row["metric_name"] == "subscription_revenue"
            and row["provenance"].get("definition_variant")
            == "total_subscription_revenue"
        ),
        key=lambda row: (
            str(row["period_end"]),
            str(row["availability_datetime"]),
            int(row["provenance"].get("period_kind") == "quarterly"),
        ),
        reverse=True,
    )
    mix_signal = _missing_signal(
        "subscription_revenue_mix_v2",
        reason="matching_period_revenue_missing",
    )
    for subscription in subscription_candidates:
        stale = _stale_reason(subscription, asof=asof)
        if stale:
            mix_signal = _stale_signal(
                subscription,
                definition_version="subscription_revenue_mix_v2",
                reason=stale,
            )
            continue
        period_kind = str(
            subscription["provenance"].get("period_kind") or ""
        )
        revenue, financial_asof = _matching_revenue(
            financial,
            period_end=str(subscription["period_end"]),
            availability_datetime=asof.isoformat(),
            period_kind=period_kind,
        )
        subscription_value = _number(subscription["value"])
        if (
            revenue is not None
            and revenue > 0
            and subscription_value is not None
        ):
            source_asof = max(
                str(subscription["availability_datetime"]),
                f"{financial_asof}T23:59:59Z" if financial_asof else "",
            )
            mix_signal = _available_signal(
                subscription_value / revenue,
                source_asof=source_asof,
                definition_version="subscription_revenue_mix_v2",
            )
            break
        mix_signal = _missing_signal(
            "subscription_revenue_mix_v2",
            source_asof=str(subscription["availability_datetime"]),
            reason=(
                "matching_period_revenue_missing:"
                f"period_kind={period_kind or 'unknown'}"
            ),
        )
    output["subscription_revenue_mix"] = mix_signal
    threshold_rows = [
        row
        for row in visible
        if row["metric_name"] == "customer_count_threshold"
    ]
    threshold_event = None
    threshold_asof = ""
    latest_threshold = None
    if len(threshold_rows) >= 2:
        latest_threshold = max(
            threshold_rows,
            key=lambda row: (
                str(row["period_end"]),
                str(row["availability_datetime"]),
            ),
        )
        prior_threshold = _year_ago(
            threshold_rows,
            latest=latest_threshold,
        )
        if prior_threshold is not None:
            latest_variant = latest_threshold["provenance"].get(
                "definition_variant"
            )
            prior_variant = prior_threshold["provenance"].get(
                "definition_variant"
            )
            threshold_event = float(latest_variant != prior_variant)
            threshold_asof = str(latest_threshold["availability_datetime"])
    threshold_stale = (
        _stale_reason(latest_threshold, asof=asof)
        if latest_threshold is not None
        else ""
    )
    if latest_threshold is not None and threshold_stale:
        output["customer_threshold_disclosure_change"] = _stale_signal(
            latest_threshold,
            definition_version="customer_threshold_change_event_v1",
            reason=threshold_stale,
        )
    elif threshold_event is None:
        output["customer_threshold_disclosure_change"] = _missing_signal(
            "customer_threshold_change_event_v1",
            source_asof=threshold_asof,
            reason="same_definition_year_ago_pair_missing",
        )
    else:
        output["customer_threshold_disclosure_change"] = _available_signal(
            threshold_event,
            source_asof=threshold_asof,
            definition_version="customer_threshold_change_event_v1",
        )
    return output

def build_pit_features(
    conn: sqlite3.Connection,
    *,
    start_date: str,
    end_date: str,
    write_database: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    dates = _price_dates(
        conn,
        start_date=start_date,
        end_date=end_date,
    )
    if not dates:
        raise RuntimeError("No QQQ trading dates found for PIT feature build")
    membership, cohorts = _membership(conn)
    facts = _fact_history(conn)
    financial = _financial_rows(conn, tickers=membership)
    panel_rows: list[dict[str, Any]] = []
    feature_rows: list[tuple[Any, ...]] = []
    coverage_counter: Counter[str] = Counter()
    stale_counter: Counter[str] = Counter()
    now = utc_now()
    for asof in dates:
        for ticker, intervals in membership.items():
            status = _member_status(intervals, asof)
            if not status:
                continue
            visible = _visible_facts(facts.get(ticker, []), asof=asof)
            signals = derive_signals(
                visible=visible,
                financial=financial.get(ticker, []),
                asof=asof,
            )
            reportable = {
                signal: payload
                for signal, payload in signals.items()
                if payload.availability_status
                in {"AVAILABLE_PIT", "STALE_PIT"}
                and payload.source_availability_datetime
            }
            if not reportable:
                continue
            panel_row: dict[str, Any] = {
                "asof_date": asof.isoformat(),
                "ticker": ticker,
                "membership_status": status,
                "historical_member_flag": int(status != "active"),
                "point_in_time_flag": 1,
                "calibration_cohort_id": cohorts.get(ticker, ""),
                "source_acceptance_datetime_max": max(
                    payload.source_availability_datetime
                    for payload in reportable.values()
                ),
            }
            for signal, payload in signals.items():
                value = payload.value
                source_asof = payload.source_availability_datetime
                panel_row[signal] = value
                panel_row[f"{signal}_source_availability_datetime"] = (
                    source_asof
                )
                panel_row[f"{signal}_availability_status"] = (
                    payload.availability_status
                )
                panel_row[f"{signal}_status_reason"] = payload.status_reason
                if payload.availability_status == "MISSING_INPUT":
                    continue
                if payload.availability_status == "AVAILABLE_PIT":
                    coverage_counter[signal] += 1
                else:
                    stale_counter[signal] += 1
                feature_rows.append(
                    (
                        MODEL_FAMILY,
                        ticker,
                        asof.isoformat(),
                        signal,
                        METRIC_VERSION,
                        value,
                        DERIVED_SIGNAL_SPECS[signal],
                        payload.availability_status,
                        "",
                        source_asof,
                        1.0,
                        0,
                        payload.status_reason,
                        payload.definition_version,
                        now,
                        now,
                    )
                )
            panel_rows.append(panel_row)
    if write_database:
        conn.execute(
            """
            DELETE FROM feature_technology_specialized_metric
            WHERE model_family = ? AND metric_version = ?
              AND asof_date BETWEEN ? AND ?
            """,
            (MODEL_FAMILY, METRIC_VERSION, start_date, end_date),
        )
        conn.executemany(
            """
            INSERT INTO feature_technology_specialized_metric(
                model_family, ticker, asof_date, metric_name, metric_version,
                value, unit, availability_status, source_accession_number,
                source_availability_datetime, confidence,
                review_required_flag, status_reason, definition_version,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                model_family, ticker, asof_date, metric_name, metric_version
            ) DO UPDATE SET
                value=excluded.value,
                unit=excluded.unit,
                availability_status=excluded.availability_status,
                source_availability_datetime=(
                    excluded.source_availability_datetime
                ),
                confidence=excluded.confidence,
                status_reason=excluded.status_reason,
                definition_version=excluded.definition_version,
                updated_at=excluded.updated_at
            """,
            feature_rows,
        )
    coverage_rows = [
        {
            "metric_name": signal,
            "populated_pit_row_count": coverage_counter[signal],
            "stale_pit_row_count": stale_counter[signal],
            "persisted_pit_row_count": (
                coverage_counter[signal] + stale_counter[signal]
            ),
            "distinct_ticker_count": len(
                {
                    row["ticker"]
                    for row in panel_rows
                    if row.get(signal) is not None
                }
            ),
            "first_asof_date": min(
                (
                    row["asof_date"]
                    for row in panel_rows
                    if row.get(signal) is not None
                ),
                default="",
            ),
            "latest_asof_date": max(
                (
                    row["asof_date"]
                    for row in panel_rows
                    if row.get(signal) is not None
                ),
                default="",
            ),
            "measurement_only_flag": 1,
            "production_weight": 0.0,
        }
        for signal in DERIVED_SIGNAL_SPECS
    ]
    return panel_rows, coverage_rows, sum(coverage_counter.values())



def build_attrition_report(
    *,
    policy: dict[str, Any],
    reconciliation_rows: list[dict[str, Any]],
    panel_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    reconciliation_by_evidence = {
        str(row["source_evidence_key"]): row
        for row in reconciliation_rows
    }
    available_by_ticker: dict[str, set[str]] = defaultdict(set)
    stale_by_ticker: dict[str, set[str]] = defaultdict(set)
    for row in panel_rows:
        ticker = str(row["ticker"])
        for signal in DERIVED_SIGNAL_SPECS:
            status = str(row.get(f"{signal}_availability_status") or "")
            if status == "AVAILABLE_PIT" and row.get(signal) is not None:
                available_by_ticker[ticker].add(signal)
            elif status == "STALE_PIT":
                stale_by_ticker[ticker].add(signal)

    output: list[dict[str, Any]] = []
    structured_core_metrics = {
        "remaining_performance_obligation",
        "deferred_revenue_current",
        "deferred_revenue_noncurrent",
        "deferred_revenue_total",
    }
    for decision in policy["decisions"]:
        evidence_key = str(decision["source_evidence_key"])
        decision_name = str(decision["decision"])
        ticker = str(decision["ticker"])
        metric = str(
            decision.get("effective_metric")
            or decision.get("source_metric")
            or ""
        )
        signals = set(METRIC_DERIVED_SIGNALS.get(metric, ()))
        observed = sorted(signals & available_by_ticker[ticker])
        stale = sorted(signals & stale_by_ticker[ticker])
        reconciliation = reconciliation_by_evidence.get(evidence_key, {})
        calibration_eligible = int(
            decision.get("calibration_eligible_flag") or 0
        )
        materialized = int(reconciliation.get("materialized_flag") or 0)
        variant = str(decision.get("definition_variant") or "")
        if decision_name == "REJECTED_POLICY":
            stage = "REJECTED_AT_ADJUDICATION"
            reason = str(decision.get("decision_reason") or "policy_rejected")
        elif not calibration_eligible:
            stage = "EXCLUDED_FROM_CALIBRATION"
            reason = (
                "censored_lower_bound_not_calibration_comparable"
                if "lower_bound" in variant
                else "policy_marked_not_calibration_comparable"
            )
        elif not materialized:
            stage = "REJECTED_AT_MATERIALIZATION"
            reason = str(
                reconciliation.get("gate_reason")
                or "materialization_gate_failed"
            )
        elif not signals:
            stage = "STORED_FACT_NO_SPECIALIZED_DERIVATION"
            reason = (
                "evaluated_in_structured_financial_diagnostics"
                if metric in structured_core_metrics
                else "no_specialized_derived_signal_registered"
            )
        elif observed:
            stage = "DERIVED_FEATURE_AVAILABLE"
            reason = "derived_feature_observed"
        elif stale:
            stage = "DERIVED_FEATURE_STALE"
            reason = "latest_derived_feature_exceeded_staleness_limit"
        elif metric == "subscription_revenue":
            stage = "DERIVATION_INPUT_INSUFFICIENT"
            reason = "matching_period_revenue_or_longitudinal_pair_missing"
        else:
            stage = "DERIVATION_INPUT_INSUFFICIENT"
            reason = "same_definition_longitudinal_pair_missing"
        output.append(
            {
                "sequence": decision["sequence"],
                "source_evidence_key": evidence_key,
                "ticker": ticker,
                "source_metric": decision.get("source_metric", ""),
                "effective_metric": decision.get("effective_metric", ""),
                "effective_period_end": decision.get(
                    "effective_period_end", ""
                ),
                "decision": decision_name,
                "definition_variant": variant,
                "calibration_eligible_flag": calibration_eligible,
                "materialized_fact_flag": materialized,
                "materialization_gate_status": reconciliation.get(
                    "gate_status", ""
                ),
                "materialization_gate_reason": reconciliation.get(
                    "gate_reason", ""
                ),
                "registered_derived_signals": "|".join(sorted(signals)),
                "observed_derived_signals": "|".join(observed),
                "stale_derived_signals": "|".join(stale),
                "derived_feature_observed_flag": int(bool(observed)),
                "derived_feature_stale_flag": int(bool(stale)),
                "attrition_stage": stage,
                "attrition_reason": reason,
                "measurement_only_flag": 1,
                "production_weight": 0.0,
            }
        )
    return output

def validate_pit_panel(
    panel_rows: list[dict[str, Any]],
) -> list[str]:
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for row in panel_rows:
        key = (str(row["asof_date"]), str(row["ticker"]))
        if key in seen:
            errors.append(f"duplicate PIT panel row: {key}")
        seen.add(key)
        asof = str(row["asof_date"])
        source_asof = str(row["source_acceptance_datetime_max"])[:10]
        if source_asof > asof:
            errors.append(
                f"future source availability: {key} source={source_asof}"
            )
        if int(row.get("point_in_time_flag") or 0) != 1:
            errors.append(f"missing PIT flag: {key}")
    return errors


def manifest_payload(
    *,
    start_date: str,
    end_date: str,
    policy_path: Path,
    fact_count: int,
    replaced_fact_count: int,
    feature_count: int,
    panel_rows: list[dict[str, Any]],
    coverage_rows: list[dict[str, Any]],
    reconciliation_rows: list[dict[str, Any]],
    attrition_rows: list[dict[str, Any]],
    errors: list[str],
    write_database: bool,
) -> dict[str, Any]:
    return {
        "manifest_version": "software_specialized_metric_pit_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "model_family": MODEL_FAMILY,
        "metric_version": METRIC_VERSION,
        "start_date": start_date,
        "end_date": end_date,
        "policy_path": str(policy_path.resolve()),
        "policy_chain_root_sha256": load_policy(policy_path)[
            "chain_root_sha256"
        ],
        "materialized_fact_count": fact_count,
        "replaced_fact_count": replaced_fact_count,
        "materialized_feature_count": feature_count,
        "persisted_feature_row_count": sum(
            int(row["persisted_pit_row_count"])
            for row in coverage_rows
        ),
        "stale_feature_row_count": sum(
            int(row["stale_pit_row_count"])
            for row in coverage_rows
        ),
        "pit_panel_row_count": len(panel_rows),
        "pit_distinct_ticker_count": len(
            {str(row["ticker"]) for row in panel_rows}
        ),
        "historical_ticker_count": len(
            {
                str(row["ticker"])
                for row in panel_rows
                if int(row["historical_member_flag"]) == 1
            }
        ),
        "reconciliation_status_counts": dict(
            Counter(
                str(row["gate_status"])
                for row in reconciliation_rows
            )
        ),
        "attrition_stage_counts": dict(
            Counter(str(row["attrition_stage"]) for row in attrition_rows)
        ),
        "attrition_row_count": len(attrition_rows),
        "write_database_flag": int(write_database),
        "production_score_weight": 0.0,
        "production_scores_modified_flag": 0,
        "validation_status": "FAIL" if errors else "PASS",
        "validation_errors": errors,
    }

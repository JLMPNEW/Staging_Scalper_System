from __future__ import annotations

import csv
import math
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from dedicated_parser.contracts import stable_hash
from technology.software_infrastructure.software_metric_governance import (
    EXPANSION_FAMILIES,
    _row_payload,
    _source_document_sha256,
)


REVIEW_SCHEMA_VERSION = "software_metric_expansion_review_v1"
ALLOWED_DECISIONS = frozenset(
    {"ACCEPTED", "CORRECTED", "REJECTED_POLICY"}
)
SOURCE_FIELDS = (
    "metric_family",
    "hard_negative_candidate_flag",
    "historical_member_flag",
    "membership_status_at_filing",
    "ticker",
    "accession_number",
    "form_type",
    "accepted_at",
    "source_document",
    "source_metric",
    "candidate_value",
    "unit",
    "period_end",
    "parser_status",
    "parser_reason",
    "source_evidence_key",
    "source_row_sha256",
    "evidence_text",
)
DECISION_FIELDS = (
    "reviewer",
    "reviewed_at_utc",
    "decision",
    "decision_reason",
    "effective_metric",
    "effective_value",
    "effective_unit",
    "effective_period_start",
    "effective_period_end",
    "effective_scope",
    "period_kind",
    "definition_variant",
    "calibration_eligible_flag",
    "review_notes",
)
EFFECTIVE_METRICS = {
    "remaining_performance_obligation": frozenset(
        {
            "remaining_performance_obligation",
            "current_remaining_performance_obligation",
        }
    ),
    "deferred_revenue_total": frozenset(
        {
            "deferred_revenue_current",
            "deferred_revenue_noncurrent",
            "deferred_revenue_total",
        }
    ),
    "annual_recurring_revenue": frozenset(
        {"annual_recurring_revenue"}
    ),
    "net_revenue_retention": frozenset({"net_revenue_retention"}),
    "disclosed_billings": frozenset({"disclosed_billings"}),
    "subscription_revenue": frozenset({"subscription_revenue"}),
    "customer_count_threshold": frozenset(
        {"customer_count_threshold"}
    ),
}
CROSS_FAMILY_EFFECTIVE_METRIC_OVERRIDES = {
    # Informatica's prose candidate was classified as cRPO, but the cited
    # balance-sheet line is current contract liabilities.
    "d1f0b97e8da7194ee54718648a52ccf0254b4918171aeff3d1c5ffdb37dd4656": (
        frozenset({"deferred_revenue_current"})
    ),
}
CALIBRATION_VARIANTS = {
    "remaining_performance_obligation": frozenset({"total_rpo"}),
    "current_remaining_performance_obligation": frozenset(
        {"current_rpo", "current_12m_rpo"}
    ),
    "deferred_revenue_current": frozenset(
        {"current_deferred_revenue"}
    ),
    "deferred_revenue_noncurrent": frozenset(
        {"noncurrent_deferred_revenue"}
    ),
    "deferred_revenue_total": frozenset({"total_deferred_revenue"}),
    "annual_recurring_revenue": frozenset({"total_arr"}),
    "net_revenue_retention": frozenset(
        {"dollar_based_net_retention"}
    ),
    "disclosed_billings": frozenset({"reported_billings"}),
    "subscription_revenue": frozenset(
        {"total_subscription_revenue"}
    ),
    "customer_count_threshold": frozenset(),
}


def load_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [
            {str(key): str(value or "").strip() for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]
    if not rows:
        raise ValueError(f"CSV is empty: {path}")
    return rows


def load_source_evidence(
    conn: sqlite3.Connection,
    evidence_keys: Iterable[str],
) -> dict[str, dict[str, Any]]:
    keys = tuple(sorted(set(evidence_keys)))
    if not keys:
        raise ValueError("At least one evidence key is required")
    placeholders = ",".join("?" for _ in keys)
    rows = conn.execute(
        f"""
        SELECT *
        FROM sec_parser_metric_evidence_shadow
        WHERE evidence_key IN ({placeholders})
          AND model_family = 'software_infrastructure'
        """,
        keys,
    ).fetchall()
    return {
        str(row["evidence_key"]): _row_payload(row)
        for row in rows
    }


def _source_seal(row: dict[str, Any]) -> str:
    payload = {
        field: str(row.get(field) or "").strip()
        for field in SOURCE_FIELDS
    }
    payload["source_document_sha256"] = str(
        row.get("source_document_sha256") or ""
    )
    return stable_hash(payload)


def _existing_by_key(
    rows: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("source_evidence_key") or "")
        if not key:
            continue
        if key in output:
            raise ValueError(f"Duplicate review evidence key: {key}")
        output[key] = dict(row)
    return output


def build_review_rows(
    queue_rows: list[dict[str, Any]],
    *,
    source_evidence: dict[str, dict[str, Any]],
    existing_rows: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    existing = _existing_by_key(existing_rows)
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for queue in queue_rows:
        key = str(queue.get("source_evidence_key") or "")
        if not key or key in seen:
            raise ValueError(
                f"Queue has missing or duplicate evidence key: {key!r}"
            )
        seen.add(key)
        source = source_evidence.get(key)
        if source is None:
            raise ValueError(f"Source evidence is missing from database: {key}")
        actual_source_hash = stable_hash(_row_payload(source))
        expected_source_hash = str(queue.get("source_row_sha256") or "")
        if actual_source_hash != expected_source_hash:
            raise ValueError(
                f"Queue source hash mismatch for evidence {key}"
            )
        document_hash = _source_document_sha256(source)
        if len(document_hash) != 64:
            raise ValueError(
                f"Source document is not SHA-256 sealed for evidence {key}"
            )
        row: dict[str, Any] = {
            "review_schema_version": REVIEW_SCHEMA_VERSION,
            **{
                field: str(queue.get(field) or "")
                for field in SOURCE_FIELDS
            },
            "source_document_sha256": document_hash,
        }
        row["review_source_sha256"] = _source_seal(row)
        prior = existing.get(key)
        if prior is not None:
            if str(prior.get("review_source_sha256") or "") != row[
                "review_source_sha256"
            ]:
                raise ValueError(
                    f"Refusing to carry review across changed source: {key}"
                )
            row.update(
                {
                    field: str(prior.get(field) or "")
                    for field in DECISION_FIELDS
                }
            )
        else:
            row.update({field: "" for field in DECISION_FIELDS})
        output.append(row)
    stale = sorted(set(existing) - seen)
    if stale:
        raise ValueError(
            "Existing review contains rows no longer present in queue: "
            + ", ".join(stale[:10])
        )
    return output


def _flag(value: object) -> int | None:
    text = str(value or "").strip()
    if text not in {"0", "1"}:
        return None
    return int(text)


def _finite(value: object) -> float | None:
    try:
        result = float(str(value))
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _valid_review_timestamp(value: str) -> bool:
    text = value.strip()
    if not text.endswith("Z"):
        return False
    try:
        datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError:
        return False
    return True


def validate_review_rows(
    rows: list[dict[str, Any]],
    *,
    queue_rows: list[dict[str, Any]],
    source_evidence: dict[str, dict[str, Any]],
) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    queue_by_key = _existing_by_key(queue_rows)
    rows_by_key = _existing_by_key(rows)
    missing = sorted(set(queue_by_key) - set(rows_by_key))
    extra = sorted(set(rows_by_key) - set(queue_by_key))
    if missing:
        errors.append(f"missing review rows: {missing[:10]}")
    if extra:
        errors.append(f"unexpected review rows: {extra[:10]}")
    decision_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    pending = 0
    for key in sorted(set(queue_by_key) & set(rows_by_key)):
        row = rows_by_key[key]
        queue = queue_by_key[key]
        if str(row.get("review_schema_version") or "") != (
            REVIEW_SCHEMA_VERSION
        ):
            errors.append(f"{key}: invalid review_schema_version")
        source = source_evidence.get(key)
        if source is None:
            errors.append(f"{key}: source evidence missing from database")
            continue
        if stable_hash(_row_payload(source)) != str(
            queue.get("source_row_sha256") or ""
        ):
            errors.append(f"{key}: database source hash changed")
        for field in SOURCE_FIELDS:
            if str(row.get(field) or "").strip() != str(
                queue.get(field) or ""
            ).strip():
                errors.append(f"{key}: immutable field changed: {field}")
        if _source_document_sha256(source) != str(
            row.get("source_document_sha256") or ""
        ):
            errors.append(f"{key}: source document hash changed")
        if _source_seal(row) != str(
            row.get("review_source_sha256") or ""
        ):
            errors.append(f"{key}: review source seal mismatch")
        decision = str(row.get("decision") or "").strip().upper()
        if not decision:
            pending += 1
            continue
        decision_counts[decision] += 1
        family = str(row.get("metric_family") or "")
        family_counts[family] += 1
        if decision not in ALLOWED_DECISIONS:
            errors.append(f"{key}: invalid decision={decision!r}")
            continue
        if not str(row.get("reviewer") or "").strip():
            errors.append(f"{key}: reviewer is required")
        if not _valid_review_timestamp(
            str(row.get("reviewed_at_utc") or "")
        ):
            errors.append(f"{key}: reviewed_at_utc must be ISO UTC with Z")
        if not str(row.get("decision_reason") or "").strip():
            errors.append(f"{key}: decision_reason is required")
        calibration_flag = _flag(row.get("calibration_eligible_flag"))
        if calibration_flag is None:
            errors.append(
                f"{key}: calibration_eligible_flag must be 0 or 1"
            )
            continue
        if decision == "REJECTED_POLICY":
            if calibration_flag != 0:
                errors.append(
                    f"{key}: rejected rows cannot be calibration eligible"
                )
            continue
        effective_metric = str(row.get("effective_metric") or "").strip()
        allowed_effective_metrics = EFFECTIVE_METRICS.get(
            family,
            frozenset(),
        ) | CROSS_FAMILY_EFFECTIVE_METRIC_OVERRIDES.get(
            key,
            frozenset(),
        )
        if effective_metric not in allowed_effective_metrics:
            errors.append(
                f"{key}: invalid effective metric for {family}: "
                f"{effective_metric!r}"
            )
        effective_value = _finite(row.get("effective_value"))
        if effective_value is None:
            errors.append(f"{key}: effective_value must be finite")
        elif effective_metric == "net_revenue_retention" and not (
            0.50 <= effective_value <= 2.00
        ):
            errors.append(f"{key}: NRR must be between 0.50 and 2.00")
        elif effective_metric != "net_revenue_retention" and (
            effective_value <= 0
        ):
            errors.append(f"{key}: effective_value must be positive")
        period_end = str(row.get("effective_period_end") or "").strip()
        accepted_date = str(row.get("accepted_at") or "")[:10]
        if not period_end or period_end > accepted_date:
            errors.append(f"{key}: invalid or forward effective_period_end")
        if str(row.get("effective_scope") or "") not in {
            "consolidated",
            "segment",
            "subset",
            "unknown",
        }:
            errors.append(f"{key}: invalid effective_scope")
        if str(row.get("period_kind") or "") not in {
            "instant",
            "quarterly",
            "annual",
        }:
            errors.append(f"{key}: invalid period_kind")
        variant = str(row.get("definition_variant") or "").strip()
        if not variant:
            errors.append(f"{key}: definition_variant is required")
        if calibration_flag == 1 and variant not in (
            CALIBRATION_VARIANTS.get(effective_metric, frozenset())
        ):
            errors.append(
                f"{key}: definition variant is not calibration-comparable"
            )
        if decision == "CORRECTED":
            unchanged = (
                effective_metric == str(row.get("source_metric") or "")
                and effective_value
                == _finite(row.get("candidate_value"))
                and period_end == str(row.get("period_end") or "")
            )
            if unchanged:
                errors.append(
                    f"{key}: CORRECTED must change metric, value, or period"
                )
    unknown_families = sorted(set(family_counts) - set(EXPANSION_FAMILIES))
    if unknown_families:
        errors.append(f"unknown metric families: {unknown_families}")
    summary = {
        "review_schema_version": REVIEW_SCHEMA_VERSION,
        "queue_row_count": len(queue_rows),
        "review_row_count": len(rows),
        "completed_review_count": len(rows) - pending,
        "pending_review_count": pending,
        "decision_counts": dict(sorted(decision_counts.items())),
        "reviewed_family_counts": dict(sorted(family_counts.items())),
        "source_integrity_pass_flag": int(
            not any(
                "source" in error
                or "immutable" in error
                or "seal" in error
                for error in errors
            )
        ),
        "ready_for_release_flag": int(not errors and pending == 0),
        "validation_status": (
            "FAIL" if errors else "PENDING" if pending else "PASS"
        ),
        "validation_errors": errors,
    }
    return errors, summary

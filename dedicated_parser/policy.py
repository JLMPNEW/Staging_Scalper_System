from __future__ import annotations

import csv
import json
import math
import os
from dataclasses import dataclass, replace
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from dedicated_parser.contracts import (
    MetricEvidence,
    WorkItem,
    file_sha256,
)


ALLOWED_DECISIONS = frozenset(
    {
        "ACCEPTED",
        "REJECTED_POLICY",
        "REVIEW_REQUIRED",
        "SUPPRESSED_SEMANTIC_DUPLICATE",
        "STRUCTURAL_NA",
    }
)
POLICY_FIELDS = (
    "policy_id",
    "policy_version",
    "enabled",
    "model_family",
    "ticker",
    "accession_number",
    "source_document",
    "metric_name",
    "concept_name",
    "candidate_value",
    "value_tolerance",
    "unit",
    "period_start",
    "period_end",
    "decision",
    "status_reason",
    "scope_override",
    "confidence_override",
    "reviewed_by",
    "reviewed_at",
    "period_start_override",
    "period_end_override",
    "value_override",
)


@dataclass(frozen=True)
class ReviewPolicy:
    policy_id: str
    policy_version: str
    model_family: str
    ticker: str
    accession_number: str
    source_document: str
    metric_name: str
    concept_name: str
    candidate_value: float | None
    value_tolerance: float
    unit: str
    period_start: str
    period_end: str
    decision: str
    status_reason: str
    scope_override: str
    confidence_override: float | None
    reviewed_by: str
    reviewed_at: str
    period_start_override: str
    period_end_override: str
    value_override: float | None


def _enabled(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "y"}


def _required(row: dict[str, str], field: str, *, path: Path) -> str:
    value = str(row.get(field) or "").strip()
    if not value:
        raise ValueError(f"{path}: review policy field {field!r} is required")
    return value


def load_review_policies(
    path: Path,
    *,
    expected_sha256: str = "",
) -> tuple[ReviewPolicy, ...]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Review policy registry does not exist: {resolved}")
    content_sha256 = expected_sha256 or file_sha256(resolved)
    return _load_review_policies_cached(str(resolved), content_sha256)


@lru_cache(maxsize=32)
def _load_review_policies_cached(
    resolved_path: str,
    expected_sha256: str,
) -> tuple[ReviewPolicy, ...]:
    resolved = Path(resolved_path)
    actual_sha256 = file_sha256(resolved)
    if actual_sha256 != expected_sha256:
        raise RuntimeError(
            "Review policy registry changed after planning: "
            f"{resolved} expected={expected_sha256} actual={actual_sha256}"
        )
    with resolved.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = set(POLICY_FIELDS) - set(reader.fieldnames or ())
        if missing:
            raise ValueError(
                f"{resolved}: missing review policy columns {sorted(missing)}"
            )
        rows = list(reader)

    policies: list[ReviewPolicy] = []
    policy_ids: set[str] = set()
    exact_keys: set[tuple[object, ...]] = set()
    for row_number, row in enumerate(rows, start=2):
        # policy_id uniqueness is validated for DISABLED rows too: a disabled
        # row silently sharing an enabled row's id would corrupt provenance the
        # moment it is re-enabled.
        policy_id = _required(row, "policy_id", path=resolved)
        if policy_id in policy_ids:
            raise ValueError(f"{resolved}:{row_number}: duplicate policy_id {policy_id}")
        policy_ids.add(policy_id)
        if not _enabled(row.get("enabled")):
            continue
        decision = _required(row, "decision", path=resolved).upper()
        if decision not in ALLOWED_DECISIONS:
            raise ValueError(
                f"{resolved}:{row_number}: unsupported decision {decision!r}"
            )
        try:
            candidate_value_text = str(
                row.get("candidate_value") or ""
            ).strip()
            candidate_value = (
                float(candidate_value_text)
                if candidate_value_text
                else None
            )
            value_tolerance = float(
                str(row.get("value_tolerance") or "0.000001")
            )
            confidence_override = (
                float(str(row["confidence_override"]).strip())
                if str(row.get("confidence_override") or "").strip()
                else None
            )
            value_override = (
                float(str(row["value_override"]).strip())
                if str(row.get("value_override") or "").strip()
                else None
            )
        except ValueError as exc:
            raise ValueError(
                f"{resolved}:{row_number}: invalid numeric policy field"
            ) from exc
        if candidate_value is None and decision != "STRUCTURAL_NA":
            raise ValueError(
                f"{resolved}:{row_number}: candidate_value is required "
                f"for decision {decision}"
            )
        if value_tolerance < 0:
            raise ValueError(
                f"{resolved}:{row_number}: value_tolerance cannot be negative"
            )
        if confidence_override is not None and not 0.0 <= confidence_override <= 1.0:
            raise ValueError(
                f"{resolved}:{row_number}: confidence_override must be in [0, 1]"
            )
        if (
            candidate_value is not None
            and not math.isfinite(candidate_value)
        ) or not math.isfinite(value_tolerance):
            raise ValueError(
                f"{resolved}:{row_number}: policy numeric fields must be finite"
            )
        if confidence_override is not None and not math.isfinite(
            confidence_override
        ):
            raise ValueError(
                f"{resolved}:{row_number}: confidence_override must be finite"
            )
        if value_override is not None and not math.isfinite(value_override):
            raise ValueError(
                f"{resolved}:{row_number}: value_override must be finite"
            )
        reviewed_at = _required(row, "reviewed_at", path=resolved)
        try:
            reviewed_timestamp = datetime.fromisoformat(
                reviewed_at.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ValueError(
                f"{resolved}:{row_number}: invalid reviewed_at timestamp"
            ) from exc
        if reviewed_timestamp.tzinfo is None:
            raise ValueError(
                f"{resolved}:{row_number}: reviewed_at must include a timezone"
            )
        period_start_override = str(
            row.get("period_start_override") or ""
        ).strip()
        period_end_override = str(
            row.get("period_end_override") or ""
        ).strip()
        for field, value in (
            ("period_start_override", period_start_override),
            ("period_end_override", period_end_override),
        ):
            if not value:
                continue
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError as exc:
                raise ValueError(
                    f"{resolved}:{row_number}: {field} must be YYYY-MM-DD"
                ) from exc
        policy = ReviewPolicy(
            policy_id=policy_id,
            policy_version=_required(row, "policy_version", path=resolved),
            model_family=_required(row, "model_family", path=resolved),
            ticker=_required(row, "ticker", path=resolved).upper(),
            accession_number=_required(
                row, "accession_number", path=resolved
            ),
            source_document=_required(row, "source_document", path=resolved),
            metric_name=_required(row, "metric_name", path=resolved),
            concept_name=str(row.get("concept_name") or "").strip(),
            candidate_value=candidate_value,
            value_tolerance=value_tolerance,
            unit=(
                str(row.get("unit") or "").strip()
                if decision == "STRUCTURAL_NA"
                else _required(row, "unit", path=resolved)
            ),
            period_start=str(row.get("period_start") or "").strip(),
            period_end=_required(row, "period_end", path=resolved),
            decision=decision,
            status_reason=_required(row, "status_reason", path=resolved),
            scope_override=str(row.get("scope_override") or "").strip(),
            confidence_override=confidence_override,
            reviewed_by=_required(row, "reviewed_by", path=resolved),
            reviewed_at=reviewed_at,
            period_start_override=period_start_override,
            period_end_override=period_end_override,
            value_override=value_override,
        )
        effective_period_start = (
            policy.period_start_override or policy.period_start
        )
        effective_period_end = (
            policy.period_end_override or policy.period_end
        )
        if (
            effective_period_start
            and effective_period_start > effective_period_end
        ):
            raise ValueError(
                f"{resolved}:{row_number}: effective period start is after end"
            )
        exact_key = (
            policy.model_family,
            policy.ticker,
            policy.accession_number,
            policy.source_document,
            policy.metric_name,
            policy.concept_name,
            policy.candidate_value,
            policy.unit,
            policy.period_start,
            policy.period_end,
        )
        if exact_key in exact_keys:
            raise ValueError(
                f"{resolved}:{row_number}: duplicate exact review policy match"
            )
        exact_keys.add(exact_key)
        policies.append(policy)
    # Tolerance-aware overlap detection at LOAD time, matching runtime
    # semantics (blank concept is a wildcard; values match within combined
    # tolerances). Exact-key dedup alone lets an overlapping pair ship and
    # only fail per-evidence-row at parse time.
    for first_index in range(len(policies)):
        for second_index in range(first_index + 1, len(policies)):
            first = policies[first_index]
            second = policies[second_index]
            if (
                first.model_family == second.model_family
                and first.ticker == second.ticker
                and first.accession_number == second.accession_number
                and first.source_document == second.source_document
                and first.metric_name == second.metric_name
                and first.unit == second.unit
                and first.period_start == second.period_start
                and first.period_end == second.period_end
                and (
                    not first.concept_name
                    or not second.concept_name
                    or first.concept_name == second.concept_name
                )
                and (
                    (
                        first.candidate_value is None
                        and second.candidate_value is None
                    )
                    or (
                        first.candidate_value is not None
                        and second.candidate_value is not None
                        and abs(
                            first.candidate_value
                            - second.candidate_value
                        )
                        <= first.value_tolerance + second.value_tolerance
                    )
                )
            ):
                raise ValueError(
                    f"{resolved}: overlapping review policies "
                    f"{first.policy_id} and {second.policy_id} can match the "
                    "same evidence row"
                )
    return tuple(policies)


def _matches(
    policy: ReviewPolicy,
    *,
    item: WorkItem,
    evidence: MetricEvidence,
) -> bool:
    candidate_period = (evidence.period_start, evidence.period_end)
    policy_period = (policy.period_start, policy.period_end)
    effective_policy_period = (
        policy.period_start_override or policy.period_start,
        policy.period_end_override or policy.period_end,
    )
    return (
        policy.model_family == item.model_family
        and policy.ticker == item.filing.ticker
        and policy.accession_number == item.filing.accession_number
        and policy.source_document == evidence.source_document
        and policy.metric_name == evidence.metric_name
        and (
            not policy.concept_name
            or policy.concept_name == evidence.concept_name
        )
        and (
            (
                evidence.value is None
                and policy.candidate_value is None
            )
            or (
                evidence.value is not None
                and policy.candidate_value is not None
                and abs(float(evidence.value) - policy.candidate_value)
                <= max(
                    policy.value_tolerance,
                    abs(policy.candidate_value) * 1e-12,
                )
            )
        )
        and policy.unit == evidence.unit
        # Parser upgrades can natively correct a date that an older reviewed
        # policy repaired with an override. Keep that policy applicable to
        # both the original observation and its reviewed effective period.
        and candidate_period in {policy_period, effective_policy_period}
    )


def apply_review_policies(
    item: WorkItem,
    evidence_rows: Iterable[MetricEvidence],
) -> tuple[MetricEvidence, ...]:
    rows = tuple(evidence_rows)
    if not item.review_policy_path:
        return rows
    registry_path = Path(item.review_policy_path)
    policies = load_review_policies(
        registry_path,
        expected_sha256=item.review_policy_sha256,
    )
    # Provenance must record the sha of the registry actually loaded, not a
    # possibly-blank value from a hand-built WorkItem.
    applied_registry_sha256 = item.review_policy_sha256 or file_sha256(
        registry_path
    )
    output: list[MetricEvidence] = []
    applied_policy_ids: set[str] = set()
    for evidence in rows:
        matches = [
            policy
            for policy in policies
            if _matches(policy, item=item, evidence=evidence)
        ]
        if len(matches) > 1:
            ids = ", ".join(policy.policy_id for policy in matches)
            raise RuntimeError(
                "Conflicting review policies matched one evidence row: "
                f"{item.filing.ticker} {item.filing.accession_number} "
                f"{evidence.metric_name} ({ids})"
            )
        if not matches:
            output.append(evidence)
            continue
        policy = matches[0]
        applied_policy_ids.add(policy.policy_id)
        provenance = dict(evidence.provenance)
        provenance["review_policy"] = {
            "policy_id": policy.policy_id,
            "policy_version": policy.policy_version,
            "reviewed_by": policy.reviewed_by,
            "reviewed_at": policy.reviewed_at,
            "registry_sha256": applied_registry_sha256,
            "matched_period_start": evidence.period_start,
            "matched_period_end": evidence.period_end,
            "matched_value": evidence.value,
            "matched_reason": evidence.reason,
        }
        output.append(
            replace(
                evidence,
                period_start=(
                    policy.period_start_override or evidence.period_start
                ),
                period_end=(
                    policy.period_end_override or evidence.period_end
                ),
                value=(
                    policy.value_override
                    if policy.value_override is not None
                    else evidence.value
                ),
                scope=policy.scope_override or evidence.scope,
                confidence=(
                    policy.confidence_override
                    if policy.confidence_override is not None
                    else evidence.confidence
                ),
                status=policy.decision,
                reason=policy.status_reason,
                provenance=provenance,
            )
        )
    requested_metrics = {
        request.metric_name for request in item.requested_metrics
    }
    documents = {document.name: document for document in item.documents}
    for policy in policies:
        if (
            policy.policy_id in applied_policy_ids
            or policy.model_family != item.model_family
            or policy.ticker != item.filing.ticker
            or policy.accession_number != item.filing.accession_number
            or policy.metric_name not in requested_metrics
            or policy.source_document not in documents
        ):
            continue
        document = documents[policy.source_document]
        output.append(
            MetricEvidence(
                metric_name=policy.metric_name,
                concept_name=policy.concept_name or policy.metric_name,
                value=(
                    policy.value_override
                    if policy.value_override is not None
                    else policy.candidate_value
                ),
                unit=policy.unit,
                period_start=(
                    policy.period_start_override or policy.period_start
                ),
                period_end=(
                    policy.period_end_override or policy.period_end
                ),
                scope=policy.scope_override or "unknown",
                confidence=(
                    policy.confidence_override
                    if policy.confidence_override is not None
                    else 1.0
                ),
                status=policy.decision,
                reason=policy.status_reason,
                evidence_text=(
                    "Materialized from an enabled reviewed policy for "
                    f"{policy.source_document}."
                ),
                source_document=policy.source_document,
                extraction_method="dedicated_parser:review_policy_registry",
                provenance={
                    "review_policy": {
                        "policy_id": policy.policy_id,
                        "policy_version": policy.policy_version,
                        "reviewed_by": policy.reviewed_by,
                        "reviewed_at": policy.reviewed_at,
                        "registry_sha256": applied_registry_sha256,
                        "materialized": True,
                    },
                    "source_document": {
                        "name": document.name,
                        "content_sha256": document.content_sha256,
                        "source_kind": document.source_kind,
                    },
                },
            )
        )
    return tuple(output)


def export_policy_golden_corpus(
    policies: Iterable[ReviewPolicy],
    *,
    output_path: Path,
    corpus_id: str,
) -> dict[str, Any]:
    expectations: list[dict[str, Any]] = []
    for policy in sorted(policies, key=lambda item: item.policy_id):
        base = {
            "ticker": policy.ticker,
            "accession_number": policy.accession_number,
            "document_name": policy.source_document,
            "metric_name": policy.metric_name,
            "candidate_value": (
                policy.value_override
                if policy.value_override is not None
                else policy.candidate_value
            ),
            "unit": policy.unit,
            "period_start": (
                policy.period_start_override or policy.period_start
            ),
            "period_end": policy.period_end_override or policy.period_end,
        }
        expectations.append(
            {
                "id": f"{policy.policy_id}_decision",
                **base,
                "candidate_status": policy.decision,
                "reason_contains": policy.status_reason,
                # The policy matched evidence within THIS tolerance; the
                # validator must use the same band or a legitimately matched
                # value can fail the expectation (and a prohibited acceptance
                # can slip past it).
                "value_tolerance": policy.value_tolerance,
            }
        )
        if policy.decision.startswith(("REJECTED", "SUPPRESSED")):
            expectations.append(
                {
                    "id": f"{policy.policy_id}_not_accepted",
                    **base,
                    "candidate_status": "ACCEPTED",
                    "reason_contains": "",
                    "expect_absent": True,
                    "value_tolerance": policy.value_tolerance,
                }
            )
    payload = {
        "corpus_id": corpus_id,
        "description": (
            "Generated exact-match expectations from the reviewed policy registry."
        ),
        "expectations": expectations,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, output_path)
    return payload


def export_registry_golden_corpus(
    *,
    registry_path: Path,
    output_path: Path,
    corpus_id: str,
) -> dict[str, Any]:
    return export_policy_golden_corpus(
        load_review_policies(registry_path),
        output_path=output_path,
        corpus_id=corpus_id,
    )

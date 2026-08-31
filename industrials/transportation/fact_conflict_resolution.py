from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

from industrials.transportation.contemporaneous_metric_coverage import (
    availability_date,
)


POLICY_VERSION = "transportation_accepted_fact_conflict_resolution_v3"

SCOPE_DIMENSIONS = (
    "segment_id",
    "denominator_basis",
    "weighting_basis",
    "capacity_basis",
)

RATIO_METRICS = frozenset(
    {
        "operating_ratio",
        "pricing_or_yield_growth",
        "purchased_transportation_ratio",
        "shipment_or_load_growth",
    }
)
GROWTH_METRICS = frozenset(
    {"pricing_or_yield_growth", "shipment_or_load_growth"}
)
ROUNDING_ABSOLUTE_TOLERANCE = 0.0005

# A metric-specific parser is allowed to suppress only the corresponding
# broad-discovery concept.  This is a source hierarchy, not a fuzzy match.
CONCEPT_PRIORITY: Mapping[str, tuple[frozenset[str], frozenset[str]]] = {
    "pricing_or_yield_growth": (
        frozenset({"ReportedLtlYieldGrowth"}),
        frozenset({"ReportedSurfaceOperatingKpi"}),
    ),
    "purchased_transportation_ratio": (
        frozenset(
            {
                "DerivedPurchasedTransportationRatioFromReportedTable",
                "PurchasedTransportation",
                "PurchasedTransportationCosts",
            }
        ),
        frozenset({"ReportedSurfaceOperatingKpi"}),
    ),
    "revenue_days": (
        frozenset({"ReportedRevenueDays"}),
        frozenset({"TransportationDiscoveryRevenueDays"}),
    ),
    "shipment_or_load_growth": (
        frozenset({"ReportedLtlShipmentGrowth"}),
        frozenset({"ReportedSurfaceOperatingKpi"}),
    ),
    "tce_day_rate": (
        frozenset({"ReportedTceDayRate"}),
        frozenset({"TransportationDiscoveryTceDayRate"}),
    ),
}

ALLOWED_CONCEPT_DEFINITIONS: Mapping[str, frozenset[str]] = {
    "pricing_or_yield_growth": frozenset(
        {"reportedltlyieldgrowth", "reportedsurfaceoperatingkpi"}
    ),
    "purchased_transportation_ratio": frozenset(
        {"purchased_transportation/revenue", "reportedsurfaceoperatingkpi"}
    ),
    "revenue_days": frozenset(
        {
            "issuer_reported_revenue_or_earning_days",
            "issuer_reported_or_available_less_offhire",
        }
    ),
    "shipment_or_load_growth": frozenset(
        {"reportedltlshipmentgrowth", "reportedsurfaceoperatingkpi"}
    ),
    "tce_day_rate": frozenset(
        {"issuer_reported_exact_label", "issuer_reported_tce_per_defined_day"}
    ),
}

ROUNDING_DEFINITION_FAMILIES: Mapping[str, frozenset[str]] = {
    "operating_ratio": frozenset(
        {
            "reportedsurfaceoperatingkpi",
            "operating_expense/revenue",
            "1-operating_income/revenue",
            "named_segment_operating_ratio",
        }
    ),
    "purchased_transportation_ratio": frozenset(
        {"purchased_transportation/revenue", "reportedsurfaceoperatingkpi"}
    ),
    "pricing_or_yield_growth": frozenset(
        {"reportedltlyieldgrowth", "reportedsurfaceoperatingkpi"}
    ),
    "shipment_or_load_growth": frozenset(
        {"reportedltlshipmentgrowth", "reportedsurfaceoperatingkpi"}
    ),
}

DIRECT_REPORTED_CONCEPTS = frozenset(
    {
        "ReportedLtlShipmentGrowth",
        "ReportedLtlYieldGrowth",
        "ReportedRevenueDays",
        "ReportedSurfaceOperatingKpi",
        "ReportedTceDayRate",
    }
)

_NEGATIVE_DIRECTION = re.compile(
    r"\b(?:decrease(?:d)?|declin(?:e|ed)|down|lower)\b",
    re.IGNORECASE,
)
_POSITIVE_DIRECTION = re.compile(
    r"\b(?:increase(?:d)?|grew|growth|higher|up)\b",
    re.IGNORECASE,
)
_ADJUSTED_TABLE_LABEL = re.compile(
    r"\|\s*adjusted operating ratio\b",
    re.IGNORECASE,
)
_REPORTED_TABLE_LABEL = re.compile(
    r"\|\s*operating ratio(?:\s*\([^)]*\))?\s*\|",
    re.IGNORECASE,
)
_ADJUSTED_PROSE_LABEL = re.compile(
    r"\badjusted operating ratio\s+(?:was|of|is|improved|deteriorated)",
    re.IGNORECASE,
)
_REPORTED_PROSE_LABEL = re.compile(
    r"\boperating ratio\s+(?:was|of|is|improved|deteriorated)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Candidate:
    row: Mapping[str, object]
    evidence: Mapping[str, object]
    value: float
    normalized_value: float

    @property
    def source_key(self) -> str:
        return str(
            self.row.get("candidate_key")
            or self.row.get("evidence_key")
            or ""
        )

    def with_value(self, value: float) -> Candidate:
        return Candidate(
            row=self.row,
            evidence=self.evidence,
            value=self.value,
            normalized_value=value,
        )


@dataclass(frozen=True)
class GroupResolution:
    conflict_id: str
    identity: tuple[str, str, str, str]
    original: tuple[Candidate, ...]
    retained: tuple[Candidate, ...]
    status: str
    resolution_rule: str
    applied_rules: tuple[str, ...]
    residual_classification: str
    confirmed_true_contradiction: bool


@dataclass(frozen=True)
class ConflictResolutionResult:
    groups: tuple[GroupResolution, ...]
    normalized_rows: tuple[dict[str, object], ...]
    group_audit_rows: tuple[dict[str, object], ...]
    evidence_audit_rows: tuple[dict[str, object], ...]
    manifest: Mapping[str, object]


def _text(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _iso(value: object) -> str:
    raw = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        return ""


def _finite(value: object) -> float | None:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        payload = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _canonical_unit(candidate: Candidate) -> str:
    unit = _text(candidate.row.get("unit")).replace(" ", "_")
    if unit in {
        "usd_per_day",
        "currency_per_day",
        "dollars_per_day",
        "$_per_day",
    }:
        return "currency_per_day"
    return unit


def _known_text(value: object) -> str:
    normalized = _text(value)
    return "" if normalized in {"unknown", "unspecified", "n/a", "na"} else normalized


def _scope_dimensions(candidate: Candidate) -> tuple[str, ...]:
    evidence_scope = _known_text(candidate.evidence.get("scope"))
    if not evidence_scope and str(candidate.row.get("source_lane") or "") != (
        "parser_run_evidence"
    ):
        evidence_scope = "not_applicable"
    return tuple(
        _known_text(candidate.row.get(field)) for field in SCOPE_DIMENSIONS
    ) + (evidence_scope,)


def _scope_payload(candidate: Candidate) -> dict[str, str]:
    values = _scope_dimensions(candidate)
    return {
        **dict(zip(SCOPE_DIMENSIONS, values[:-1])),
        "evidence_scope": values[-1],
    }


def _semantic_identity(candidate: Candidate) -> tuple[str, ...] | None:
    """Return an exact same-document evidence locator when one is available."""
    provenance = _json_object(candidate.evidence.get("provenance_json"))
    accession = _known_text(candidate.row.get("accession_number"))
    document = _known_text(candidate.row.get("source_document"))
    document_hash = _known_text(
        candidate.row.get("source_content_sha256")
        or provenance.get("document_sha256")
    )
    table = _known_text(provenance.get("semantic_table_id"))
    block = _known_text(provenance.get("semantic_block_index"))
    row = _known_text(provenance.get("semantic_row_index"))
    evidence_key = _known_text(candidate.evidence.get("evidence_key"))
    if not accession or not document or not document_hash:
        return None
    if any((table, block, row)):
        return accession, document, document_hash, "semantic", table, block, row
    if evidence_key:
        return accession, document, document_hash, "evidence_key", evidence_key
    return None


def _same_exact_evidence_identity(candidates: Sequence[Candidate]) -> bool:
    identities = [_semantic_identity(candidate) for candidate in candidates]
    return (
        bool(identities)
        and all(identity is not None for identity in identities)
        and len(set(identities)) == 1
    )


def _strict_dimension_compatible(
    candidates: Sequence[Candidate],
    values: Iterable[str],
) -> bool:
    normalized = list(values)
    if len(normalized) != len(candidates) or not normalized:
        return False
    present = [bool(value) for value in normalized]
    if any(present) and not all(present):
        return False
    if all(present):
        return len(set(normalized)) == 1
    return _same_exact_evidence_identity(candidates)


def _period_starts_compatible(candidates: Sequence[Candidate]) -> bool:
    return _strict_dimension_compatible(
        candidates,
        (_iso(candidate.row.get("period_start")) for candidate in candidates),
    )


def _scopes_compatible(candidates: Sequence[Candidate]) -> bool:
    dimensions = [_scope_dimensions(candidate) for candidate in candidates]
    return all(
        _strict_dimension_compatible(
            candidates,
            (values[index] for values in dimensions),
        )
        for index in range(len(SCOPE_DIMENSIONS) + 1)
    )


def _units_compatible(candidates: Sequence[Candidate]) -> bool:
    units = [_canonical_unit(item) for item in candidates]
    return bool(units) and all(units) and len(set(units)) == 1


def _definition(candidate: Candidate) -> str:
    return _text(candidate.row.get("definition_basis"))


def _adjustment_basis(candidate: Candidate) -> str:
    explicit = _text(candidate.row.get("adjustment_basis"))
    if explicit:
        return explicit
    text = " ".join(str(candidate.evidence.get("evidence_text") or "").split())
    if _ADJUSTED_TABLE_LABEL.search(text) or _ADJUSTED_PROSE_LABEL.search(text):
        return "adjusted"
    if _REPORTED_TABLE_LABEL.search(text) or _REPORTED_PROSE_LABEL.search(text):
        return "reported"
    return "unknown"


def _duration_boundary(candidate: Candidate) -> tuple[str, ...]:
    return (
        _canonical_unit(candidate),
        _definition(candidate),
        _text(candidate.row.get("comparability_class")),
        *_scope_dimensions(candidate),
        _adjustment_basis(candidate),
    )


def _distinct_values(candidates: Sequence[Candidate]) -> set[float]:
    return {candidate.normalized_value for candidate in candidates}


def _conflict_id(identity: tuple[str, str, str, str]) -> str:
    payload = json.dumps(identity, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def conflict_identity(row: Mapping[str, object]) -> tuple[str, str, str, str] | None:
    ticker = str(row.get("ticker") or "").upper()
    metric = str(row.get("metric_id") or "")
    period_end = _iso(row.get("period_end"))
    available = availability_date(row)
    if not ticker or not metric or not period_end or available is None:
        return None
    return ticker, metric, period_end, available.isoformat()


def _candidate(
    row: Mapping[str, object],
    evidence_by_key: Mapping[str, Mapping[str, object]],
) -> Candidate | None:
    value = _finite(row.get("value"))
    if value is None:
        return None
    evidence_key = str(row.get("evidence_key") or "")
    evidence = evidence_by_key.get(evidence_key, {})
    return Candidate(row=dict(row), evidence=dict(evidence), value=value, normalized_value=value)


def _select_shortest_duration(
    candidates: Sequence[Candidate],
) -> tuple[list[Candidate], bool]:
    if (
        not _period_starts_compatible(candidates)
        or not _scopes_compatible(candidates)
        or len({_duration_boundary(item) for item in candidates}) != 1
    ):
        return list(candidates), False
    return list(candidates), False


def _select_reported_over_adjusted(
    candidates: Sequence[Candidate],
) -> tuple[list[Candidate], bool]:
    if not candidates or str(candidates[0].row.get("metric_id")) != "operating_ratio":
        return list(candidates), False
    if not _units_compatible(candidates):
        return list(candidates), False
    if not _period_starts_compatible(candidates) or not _scopes_compatible(candidates):
        return list(candidates), False
    definitions = {_definition(item) for item in candidates}
    if len(definitions) != 1:
        return list(candidates), False
    bases = [_adjustment_basis(item) for item in candidates]
    if set(bases) != {"reported", "adjusted"}:
        return list(candidates), False
    selected = [
        item for item, basis in zip(candidates, bases) if basis == "reported"
    ]
    return selected, len(selected) != len(candidates)


def _select_metric_specific_concept(
    candidates: Sequence[Candidate],
) -> tuple[list[Candidate], bool]:
    if not candidates:
        return [], False
    metric = str(candidates[0].row.get("metric_id") or "")
    policy = CONCEPT_PRIORITY.get(metric)
    if policy is None:
        return list(candidates), False
    preferred, broad = policy
    concepts = {str(item.row.get("concept_name") or "") for item in candidates}
    if not concepts & preferred or not concepts <= preferred | broad:
        return list(candidates), False
    if (
        not _units_compatible(candidates)
        or not _period_starts_compatible(candidates)
        or not _scopes_compatible(candidates)
    ):
        return list(candidates), False
    allowed_definitions = ALLOWED_CONCEPT_DEFINITIONS[metric]
    if any(_definition(item) not in allowed_definitions for item in candidates):
        return list(candidates), False
    selected = [
        item
        for item in candidates
        if str(item.row.get("concept_name") or "") in preferred
    ]
    return selected, len(selected) != len(candidates)


def _raw_value_token(candidate: Candidate) -> str:
    provenance = _json_object(candidate.evidence.get("provenance_json"))
    token = str(provenance.get("raw_value_text") or "").strip()
    if token:
        return token
    magnitude = abs(candidate.value) * 100.0
    return f"{magnitude:g}"


def _direction_from_evidence(candidate: Candidate) -> int | None:
    text = " ".join(str(candidate.evidence.get("evidence_text") or "").split())
    if not text:
        return None
    raw = _raw_value_token(candidate)
    numbers = re.findall(r"\d+(?:\.\d+)?", raw.replace(",", ""))
    if not numbers:
        return None
    number = re.escape(numbers[0])
    normalized = text.replace(",", "")
    before = re.compile(
        rf"(.{{0,90}})\b{number}\s*%?",
        re.IGNORECASE,
    ).search(normalized)
    after = re.compile(
        rf"\b{number}\s*%?(.{{0,60}})",
        re.IGNORECASE,
    ).search(normalized)
    context = " ".join(
        part
        for part in (
            before.group(1) if before else "",
            after.group(1) if after else "",
        )
        if part
    )
    negative = bool(_NEGATIVE_DIRECTION.search(context))
    positive = bool(_POSITIVE_DIRECTION.search(context))
    if negative == positive:
        return None
    return -1 if negative else 1


def _normalize_growth_sign(
    candidates: Sequence[Candidate],
) -> tuple[list[Candidate], bool]:
    if not candidates:
        return [], False
    metric = str(candidates[0].row.get("metric_id") or "")
    if metric not in GROWTH_METRICS:
        return list(candidates), False
    if not _units_compatible(candidates):
        return list(candidates), False
    if not _period_starts_compatible(candidates) or not _scopes_compatible(candidates):
        return list(candidates), False
    if len({_definition(item) for item in candidates}) != 1:
        return list(candidates), False
    if len({abs(item.normalized_value) for item in candidates}) != 1:
        return list(candidates), False
    normalized: list[Candidate] = []
    changed = False
    for item in candidates:
        direction = _direction_from_evidence(item)
        if direction is None:
            return list(candidates), False
        value = abs(item.normalized_value) * direction
        normalized.append(item.with_value(value))
        changed = changed or value != item.normalized_value
    return normalized, changed


def _rounding_source_priority(candidate: Candidate) -> int:
    concept = str(candidate.row.get("concept_name") or "")
    lane = str(candidate.row.get("source_lane") or "")
    if lane == "parser_run_evidence" and concept in DIRECT_REPORTED_CONCEPTS:
        return 0
    if lane == "fact_store_ratio":
        return 1
    return 2


def _select_rounding_tie(
    candidates: Sequence[Candidate],
) -> tuple[list[Candidate], bool]:
    if not candidates:
        return [], False
    metric = str(candidates[0].row.get("metric_id") or "")
    if metric not in RATIO_METRICS:
        return list(candidates), False
    if not _units_compatible(candidates):
        return list(candidates), False
    if not _period_starts_compatible(candidates) or not _scopes_compatible(candidates):
        return list(candidates), False
    allowed = ROUNDING_DEFINITION_FAMILIES.get(metric, frozenset())
    if not allowed or any(_definition(item) not in allowed for item in candidates):
        return list(candidates), False
    values = [item.normalized_value for item in candidates]
    if max(values) - min(values) > ROUNDING_ABSOLUTE_TOLERANCE:
        return list(candidates), False

    counts = Counter(values)
    highest_count = max(counts.values())
    modal_values = {value for value, count in counts.items() if count == highest_count}
    if highest_count > 1 and len(modal_values) == 1:
        chosen = next(iter(modal_values))
        return [item for item in candidates if item.normalized_value == chosen], True

    best_priority = min(_rounding_source_priority(item) for item in candidates)
    preferred = [
        item
        for item in candidates
        if _rounding_source_priority(item) == best_priority
    ]
    if len(_distinct_values(preferred)) != 1:
        return list(candidates), False
    return preferred, len(preferred) != len(candidates)


def _confirmed_source_contradiction(candidates: Sequence[Candidate]) -> bool:
    if len(_distinct_values(candidates)) < 2:
        return False
    adjustments = {_adjustment_basis(item) for item in candidates}
    documents = {str(item.row.get("source_document") or "") for item in candidates}
    if (
        not _period_starts_compatible(candidates)
        or not _scopes_compatible(candidates)
        or "unknown" in adjustments
        or "" in documents
    ):
        return False
    return (
        len(adjustments) == 1
        and len({_canonical_unit(item) for item in candidates}) == 1
        and len({_definition(item) for item in candidates}) == 1
        and len(documents) > 1
    )


def _residual_classification(candidates: Sequence[Candidate]) -> str:
    if _confirmed_source_contradiction(candidates):
        return "confirmed_source_contradiction"
    period_starts = [_iso(item.row.get("period_start")) for item in candidates]
    if any(not value for value in period_starts):
        return "period_start_missing_or_mixed"
    if len(set(period_starts)) > 1:
        return "period_start_collision"
    scope_values = [_scope_dimensions(item) for item in candidates]
    for index in range(len(SCOPE_DIMENSIONS) + 1):
        values = [item[index] for item in scope_values]
        if any(values) and not all(values):
            return "scope_dimension_missing_or_mixed"
        if len(set(values)) > 1:
            return "explicit_scope_collision"
        if not any(values) and not _same_exact_evidence_identity(candidates):
            return "incomplete_scope_identity"
    if not _scopes_compatible(candidates):
        return "explicit_scope_collision"
    if len({_definition(item) for item in candidates}) > 1:
        return "definition_scope_collision"
    if len({str(item.row.get("source_lane") or "") for item in candidates}) > 1:
        return "cross_lane_source_tieout_required"
    return "unresolved_parser_identity_ambiguity"


def resolve_conflict_group(
    identity: tuple[str, str, str, str],
    rows: Sequence[Mapping[str, object]],
    evidence_by_key: Mapping[str, Mapping[str, object]],
) -> GroupResolution:
    original = tuple(
        candidate
        for row in rows
        if (candidate := _candidate(row, evidence_by_key)) is not None
    )
    if len(_distinct_values(original)) < 2:
        raise ValueError(f"{identity}: group is not a numeric conflict")
    retained = list(original)
    applied: list[str] = []

    retained, changed = _select_shortest_duration(retained)
    if changed:
        applied.append("shortest_explicit_duration")
    if len(_distinct_values(retained)) == 1:
        rule = applied[-1]
        return GroupResolution(
            _conflict_id(identity), identity, original, tuple(retained),
            "RESOLVED_DETERMINISTIC", rule, tuple(applied), "", False,
        )

    retained, changed = _select_reported_over_adjusted(retained)
    if changed:
        applied.append("reported_gaap_over_adjusted_operating_ratio")
    if len(_distinct_values(retained)) == 1:
        rule = applied[-1]
        return GroupResolution(
            _conflict_id(identity), identity, original, tuple(retained),
            "RESOLVED_DETERMINISTIC", rule, tuple(applied), "", False,
        )

    retained, changed = _select_metric_specific_concept(retained)
    if changed:
        applied.append("exact_metric_parser_over_broad_discovery")
    if len(_distinct_values(retained)) == 1:
        rule = applied[-1]
        return GroupResolution(
            _conflict_id(identity), identity, original, tuple(retained),
            "RESOLVED_DETERMINISTIC", rule, tuple(applied), "", False,
        )

    # A broad row with an unknown duration may have prevented the first pass.
    # Once that lower-priority source is removed, duration becomes usable.
    retained, changed = _select_shortest_duration(retained)
    if changed:
        applied.append("shortest_duration_after_provenance_priority")
    if len(_distinct_values(retained)) == 1:
        rule = applied[-1]
        return GroupResolution(
            _conflict_id(identity), identity, original, tuple(retained),
            "RESOLVED_DETERMINISTIC", rule, tuple(applied), "", False,
        )

    retained, changed = _normalize_growth_sign(retained)
    if changed:
        applied.append("explicit_growth_direction_sign_normalization")
    if len(_distinct_values(retained)) == 1:
        rule = applied[-1]
        return GroupResolution(
            _conflict_id(identity), identity, original, tuple(retained),
            "RESOLVED_DETERMINISTIC", rule, tuple(applied), "", False,
        )

    retained, changed = _select_rounding_tie(retained)
    if changed:
        applied.append("reported_or_modal_value_within_disclosed_rounding")
    if len(_distinct_values(retained)) == 1:
        rule = applied[-1]
        return GroupResolution(
            _conflict_id(identity), identity, original, tuple(retained),
            "RESOLVED_DETERMINISTIC", rule, tuple(applied), "", False,
        )

    contradiction = _confirmed_source_contradiction(retained)
    residual = _residual_classification(retained)
    return GroupResolution(
        conflict_id=_conflict_id(identity),
        identity=identity,
        original=original,
        retained=tuple(retained),
        status="FAIL_CLOSED_REVIEW_REQUIRED",
        resolution_rule="",
        applied_rules=tuple(applied),
        residual_classification=residual,
        confirmed_true_contradiction=contradiction,
    )


def _candidate_payload(candidate: Candidate) -> dict[str, object]:
    provenance = _json_object(candidate.evidence.get("provenance_json"))
    scope = _scope_payload(candidate)
    semantic_identity = _semantic_identity(candidate)
    return {
        "candidate_key": candidate.source_key,
        "original_value": candidate.value,
        "normalized_value": candidate.normalized_value,
        "unit": str(candidate.row.get("unit") or ""),
        "canonical_unit": _canonical_unit(candidate),
        "period_start": str(candidate.row.get("period_start") or ""),
        "definition_basis": str(candidate.row.get("definition_basis") or ""),
        "concept_name": str(candidate.row.get("concept_name") or ""),
        "source_lane": str(candidate.row.get("source_lane") or ""),
        "accession_number": str(candidate.row.get("accession_number") or ""),
        "source_document": str(candidate.row.get("source_document") or ""),
        **scope,
        "scope_signature_json": json.dumps(
            list(_scope_dimensions(candidate)), separators=(",", ":")
        ),
        "scope_complete_flag": int(all(_scope_dimensions(candidate))),
        "semantic_evidence_identity_json": json.dumps(
            list(semantic_identity) if semantic_identity else [],
            separators=(",", ":"),
        ),
        "adjustment_basis": _adjustment_basis(candidate),
        "extraction_method": str(candidate.evidence.get("extraction_method") or ""),
        "semantic_table_id": provenance.get("semantic_table_id"),
        "semantic_block_index": provenance.get("semantic_block_index"),
        "semantic_row_index": provenance.get("semantic_row_index"),
    }


def _group_audit_row(group: GroupResolution) -> dict[str, object]:
    ticker, metric, period_end, available_on = group.identity
    retained_keys = {candidate.source_key for candidate in group.retained}
    return {
        "conflict_id": group.conflict_id,
        "ticker": ticker,
        "metric_id": metric,
        "period_end": period_end,
        "available_on": available_on,
        "original_candidate_count": len(group.original),
        "original_distinct_value_count": len(_distinct_values(group.original)),
        "original_values_json": json.dumps(
            sorted(_distinct_values(group.original)), separators=(",", ":")
        ),
        "retained_candidate_count": len(group.retained),
        "retained_distinct_value_count": len(_distinct_values(group.retained)),
        "retained_values_json": json.dumps(
            sorted(_distinct_values(group.retained)), separators=(",", ":")
        ),
        "resolution_status": group.status,
        "resolution_rule": group.resolution_rule,
        "applied_rules": "|".join(group.applied_rules),
        "residual_classification": group.residual_classification,
        "confirmed_true_contradiction_flag": int(
            group.confirmed_true_contradiction
        ),
        "retained_candidate_keys": "|".join(sorted(retained_keys)),
        "suppressed_candidate_keys": "|".join(
            sorted(
                candidate.source_key
                for candidate in group.original
                if candidate.source_key not in retained_keys
            )
        ),
        "candidate_summary_json": json.dumps(
            [_candidate_payload(candidate) for candidate in group.original],
            sort_keys=True,
            separators=(",", ":"),
        ),
    }


def _evidence_audit_rows(group: GroupResolution) -> list[dict[str, object]]:
    retained_keys = {candidate.source_key for candidate in group.retained}
    output: list[dict[str, object]] = []
    for candidate in group.original:
        evidence = candidate.evidence
        payload = _candidate_payload(candidate)
        output.append(
            {
                "conflict_id": group.conflict_id,
                "ticker": group.identity[0],
                "metric_id": group.identity[1],
                "period_end": group.identity[2],
                "available_on": group.identity[3],
                **payload,
                "candidate_disposition": (
                    "RETAINED" if candidate.source_key in retained_keys else "SUPPRESSED"
                ),
                "resolution_status": group.status,
                "resolution_rule": group.resolution_rule,
                "status_reason": str(evidence.get("status_reason") or ""),
                "evidence_text": str(evidence.get("evidence_text") or ""),
                "provenance_json": json.dumps(
                    _json_object(evidence.get("provenance_json")),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "evidence_label": (
                    "derived_calculation"
                    if str(candidate.row.get("formula") or "")
                    else "fact_source_reported"
                ),
            }
        )
    return output


def resolve_accepted_fact_conflicts(
    *,
    rows: Iterable[Mapping[str, object]],
    evidence_by_key: Mapping[str, Mapping[str, object]],
    metric_ids: set[str],
) -> ConflictResolutionResult:
    source_rows = [dict(row) for row in rows]
    grouped: defaultdict[
        tuple[str, str, str, str], list[dict[str, object]]
    ] = defaultdict(list)
    passthrough: list[dict[str, object]] = []
    for row in source_rows:
        identity = conflict_identity(row)
        value = _finite(row.get("value"))
        if identity is None or value is None or identity[1] not in metric_ids:
            passthrough.append(row)
            continue
        grouped[identity].append(row)

    conflicts: dict[
        tuple[str, str, str, str], list[dict[str, object]]
    ] = {}
    nonconflicted: list[dict[str, object]] = []
    for identity, candidates in grouped.items():
        values = {
            value
            for row in candidates
            if (value := _finite(row.get("value"))) is not None
        }
        if len(values) > 1:
            conflicts[identity] = candidates
        else:
            nonconflicted.extend(candidates)

    resolutions = tuple(
        resolve_conflict_group(identity, conflicts[identity], evidence_by_key)
        for identity in sorted(conflicts)
    )
    resolution_by_identity = {group.identity: group for group in resolutions}

    normalized: list[dict[str, object]] = []
    for row in passthrough + nonconflicted:
        item = dict(row)
        item.update(
            conflict_group_id="",
            conflict_resolution_status="NOT_CONFLICTED",
            conflict_resolution_rule="",
            conflict_applied_rules="",
            original_value=str(row.get("value") or ""),
            value_normalization="none",
        )
        normalized.append(item)

    for identity in sorted(resolution_by_identity):
        group = resolution_by_identity[identity]
        for candidate in group.retained:
            item = dict(candidate.row)
            item["value"] = repr(candidate.normalized_value)
            item.update(
                conflict_group_id=group.conflict_id,
                conflict_resolution_status=group.status,
                conflict_resolution_rule=group.resolution_rule,
                conflict_applied_rules="|".join(group.applied_rules),
                original_value=repr(candidate.value),
                value_normalization=(
                    "explicit_direction"
                    if candidate.normalized_value != candidate.value
                    else "none"
                ),
            )
            normalized.append(item)

    normalized.sort(
        key=lambda row: (
            str(row.get("ticker") or ""),
            str(row.get("metric_id") or ""),
            str(row.get("period_end") or ""),
            str(row.get("filing_date") or ""),
            str(row.get("candidate_key") or row.get("evidence_key") or ""),
        )
    )
    group_rows = tuple(_group_audit_row(group) for group in resolutions)
    evidence_rows = tuple(
        row
        for group in resolutions
        for row in _evidence_audit_rows(group)
    )
    rule_counts = Counter(
        group.resolution_rule
        for group in resolutions
        if group.status == "RESOLVED_DETERMINISTIC"
    )
    residual_counts = Counter(
        group.residual_classification
        for group in resolutions
        if group.status == "FAIL_CLOSED_REVIEW_REQUIRED"
    )
    metric_before = Counter(group.identity[1] for group in resolutions)
    metric_residual = Counter(
        group.identity[1]
        for group in resolutions
        if group.status == "FAIL_CLOSED_REVIEW_REQUIRED"
    )
    resolved_count = sum(
        group.status == "RESOLVED_DETERMINISTIC" for group in resolutions
    )
    residual_count = len(resolutions) - resolved_count
    contradiction_count = sum(
        group.confirmed_true_contradiction for group in resolutions
    )
    manifest: dict[str, object] = {
        "policy_version": POLICY_VERSION,
        "input_row_count": len(source_rows),
        "target_metric_count": len(metric_ids),
        "resolver_conflict_count_before": len(resolutions),
        "deterministic_false_conflict_count": resolved_count,
        "resolver_conflict_count_after": residual_count,
        "confirmed_true_contradiction_count": contradiction_count,
        "unresolved_fail_closed_count": residual_count,
        "resolution_count_by_rule": dict(sorted(rule_counts.items())),
        "residual_count_by_classification": dict(sorted(residual_counts.items())),
        "conflict_count_before_by_metric": dict(sorted(metric_before.items())),
        "conflict_count_after_by_metric": dict(sorted(metric_residual.items())),
        "normalized_row_count": len(normalized),
        "evidence_audit_row_count": len(evidence_rows),
        "source_conflicts_are_never_averaged": True,
        "unresolved_conflicts_fail_closed": True,
        "period_start_boundary_policy": (
            "complete_and_equal_for_every_deterministic_resolution_rule"
        ),
        "scope_boundary_dimensions": [
            *SCOPE_DIMENSIONS,
            "evidence_scope",
        ],
        "scope_boundary_policy": (
            "each_dimension_complete_and_equal_or_all_missing_with_exact_"
            "document_and_semantic_evidence_identity;known_missing_never_compatible"
        ),
        "production_activation_authorized": False,
    }
    return ConflictResolutionResult(
        groups=resolutions,
        normalized_rows=tuple(normalized),
        group_audit_rows=group_rows,
        evidence_audit_rows=evidence_rows,
        manifest=manifest,
    )

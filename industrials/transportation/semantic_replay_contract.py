from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping


REPLAY_IDENTITY_FIELDS = (
    "ticker",
    "metric_id",
    "period_end",
    "filing_date",
    "accession_number",
)


@dataclass(frozen=True)
class ReplayResolution:
    conflict_free_rows: tuple[dict[str, str], ...]
    conflict_rows: tuple[dict[str, str], ...]
    accepted_input_count: int
    observation_group_count: int
    conflict_group_count: int


def _normalized_value(raw: object) -> float | None:
    try:
        value = float(str(raw or "").strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _identity(row: Mapping[str, object]) -> tuple[str, ...]:
    return tuple(str(row.get(field) or "").strip() for field in REPLAY_IDENTITY_FIELDS)


def resolve_semantic_replay_rows(
    rows: Iterable[Mapping[str, object]],
) -> ReplayResolution:
    """Return one deterministic row only for unambiguous PIT observations.

    Definition-level approval does not make multiple different values from the
    same filing, period, and metric interchangeable. Such groups normally
    represent segment rows, table columns, or incomplete labels. They must be
    reviewed further and cannot enter a canonical feature or coverage gate.
    """
    accepted = [
        {str(key): str(value or "").strip() for key, value in row.items()}
        for row in rows
        if str(row.get("replay_status") or row.get("candidate_status") or "").upper()
        == "ACCEPTED"
    ]
    grouped: defaultdict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
    invalid: list[dict[str, str]] = []
    for row in accepted:
        identity = _identity(row)
        value = _normalized_value(row.get("value") or row.get("candidate_value"))
        if not all(identity) or value is None:
            invalid.append(
                {
                    **row,
                    "conflict_reason": "invalid_or_incomplete_observation_identity",
                    "observation_candidate_count": "1",
                    "observation_distinct_value_count": "0",
                }
            )
            continue
        grouped[identity].append(row)

    conflict_free: list[dict[str, str]] = []
    conflicts = list(invalid)
    conflict_group_count = len(invalid)
    for identity, candidates in sorted(grouped.items()):
        values = {
            round(
                float(
                    _normalized_value(
                        row.get("value") or row.get("candidate_value")
                    )
                ),
                10,
            )
            for row in candidates
        }
        units = {str(row.get("unit") or "").strip().lower() for row in candidates}
        if len(values) != 1 or len(units) != 1:
            conflict_group_count += 1
            reason = (
                "same_filing_period_metric_has_multiple_values"
                if len(values) != 1
                else "same_filing_period_metric_has_multiple_units"
            )
            for row in candidates:
                conflicts.append(
                    {
                        **row,
                        "conflict_reason": reason,
                        "observation_candidate_count": str(len(candidates)),
                        "observation_distinct_value_count": str(len(values)),
                    }
                )
            continue
        selected = min(
            candidates,
            key=lambda row: (
                str(row.get("candidate_key") or row.get("evidence_key") or ""),
                str(row.get("concept_name") or ""),
            ),
        )
        conflict_free.append(selected)

    return ReplayResolution(
        conflict_free_rows=tuple(conflict_free),
        conflict_rows=tuple(conflicts),
        accepted_input_count=len(accepted),
        observation_group_count=len(grouped) + len(invalid),
        conflict_group_count=conflict_group_count,
    )

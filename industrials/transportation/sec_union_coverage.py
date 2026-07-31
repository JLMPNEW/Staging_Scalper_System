from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence


_COUNT_FIELDS = (
    "text_hit_count",
    "value_candidate_count",
    "accepted_value_count",
    "review_value_count",
    "rejected_value_count",
    "parser_failure_count",
)
_SET_FIELDS = (
    "periods",
    "accepted_periods",
    "usable_periods",
)


def merge_evidence_stats(
    *sources: Mapping[tuple[str, str], Mapping[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    output: dict[tuple[str, str], dict[str, Any]] = {}
    for source in sources:
        for key, stats in source.items():
            merged = output.setdefault(
                key,
                {
                    **{field: 0 for field in _COUNT_FIELDS},
                    **{field: set() for field in _SET_FIELDS},
                },
            )
            for field in _COUNT_FIELDS:
                merged[field] += int(stats.get(field) or 0)
            for field in _SET_FIELDS:
                merged[field].update(stats.get(field) or ())
    return output


def merge_work_stats(
    *sources: Mapping[str, Mapping[str, int]],
) -> dict[str, dict[str, int]]:
    output: dict[str, dict[str, int]] = {}
    for source in sources:
        for ticker, stats in source.items():
            merged = output.setdefault(
                ticker,
                {"searched": 0, "completed": 0, "failed": 0},
            )
            for field in ("searched", "completed", "failed"):
                merged[field] += int(stats.get(field) or 0)
    return output


def coverage_rates_from_counts(
    counts: Mapping[str, int],
) -> dict[str, float]:
    total = sum(int(value) for value in counts.values())
    if total <= 0:
        return {"accepted": 0.0, "usable": 0.0, "discovery": 0.0}
    accepted = int(counts.get("COVERED_ACCEPTED") or 0) + int(
        counts.get("COVERED_FINANCIAL_DERIVED") or 0
    )
    usable = accepted + int(
        counts.get("COVERED_REVIEW_REQUIRED") or 0
    )
    discovery = (
        usable
        + int(counts.get("DISCOVERED_REJECTED") or 0)
        + int(counts.get("TEXT_HIT_NO_VALUE") or 0)
    )
    return {
        "accepted": accepted / total,
        "usable": usable / total,
        "discovery": discovery / total,
    }


def coverage_counts(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, int]:
    return dict(
        sorted(
            Counter(
                str(row["coverage_status"])
                for row in rows
                if row["applicability_status"] == "APPLICABLE"
            ).items()
        )
    )

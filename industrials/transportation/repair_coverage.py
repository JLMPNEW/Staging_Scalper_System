from __future__ import annotations

from typing import Any, Mapping, Sequence


def repaired_document_keys(
    rows: Sequence[Mapping[str, object]],
) -> set[tuple[str, str, str]]:
    return {
        (
            str(row.get("ticker") or "").upper(),
            str(row.get("accession_number") or ""),
            str(row.get("document_name") or ""),
        )
        for row in rows
        if str(row.get("ticker") or "")
        and str(row.get("accession_number") or "")
        and str(row.get("document_name") or "")
    }


def suppress_repaired_failure_counts(
    *,
    evidence: Mapping[tuple[str, str], Mapping[str, Any]],
    failure_rows: Sequence[Mapping[str, object]],
    repaired_keys: set[tuple[str, str, str]],
) -> tuple[
    dict[tuple[str, str], dict[str, Any]],
    int,
    list[str],
]:
    output: dict[tuple[str, str], dict[str, Any]] = {
        key: {
            field: (
                set(value)
                if field
                in {"periods", "accepted_periods", "usable_periods"}
                else value
            )
            for field, value in stats.items()
        }
        for key, stats in evidence.items()
    }
    suppressed = 0
    errors: list[str] = []
    for row in failure_rows:
        if str(row.get("candidate_status") or "") != "PARSER_FAILURE":
            continue
        document_key = (
            str(row.get("ticker") or "").upper(),
            str(row.get("accession_number") or ""),
            str(row.get("source_document") or ""),
        )
        if document_key not in repaired_keys:
            continue
        pair = (
            document_key[0],
            str(row.get("metric_name") or ""),
        )
        stats = output.get(pair)
        if stats is None:
            errors.append(
                "superseded failure pair missing aggregate="
                + "|".join(pair)
            )
            continue
        current = int(stats.get("parser_failure_count") or 0)
        if current <= 0:
            errors.append(
                "superseded failure count already zero="
                + "|".join(pair)
            )
            continue
        stats["parser_failure_count"] = current - 1
        suppressed += 1
    return output, suppressed, errors

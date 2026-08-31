from __future__ import annotations

import json
import re
from datetime import date
from typing import Mapping, Sequence


EXACT_PERIOD_START_KEYS = (
    "period_start",
    "context_period_start",
    "xbrl_period_start",
    "duration_start",
)
EXACT_XBRL_LINK_KEYS = (
    "raw_fact_id",
    "fact_fingerprint",
    "xbrl_context_id",
    "context_id",
)
_DURATION_PHRASE = re.compile(
    r"\b(?:three|six|nine|twelve)\s+months?\s+ended\b"
    r"|\b(?:quarter|year)\s+ended\b",
    re.IGNORECASE,
)
_MONTH = (
    r"(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)"
)
_EXPLICIT_FULL_DATE_RANGE = re.compile(
    rf"\b(?:from|between)\s+{_MONTH}\s+\d{{1,2}},\s+\d{{4}}\s+"
    rf"(?:to|through|and)\s+{_MONTH}\s+\d{{1,2}},\s+\d{{4}}\b",
    re.IGNORECASE,
)


def iso_date(value: object) -> str:
    raw = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        return ""


def json_object(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        payload = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def classify_candidate_period_start(
    row: Mapping[str, object],
    *,
    bound_evidence_period_start: object = "",
) -> dict[str, object]:
    """Classify exact recovery using only already-bound identity fields.

    Duration language and table locators are recorded as feasibility signals,
    but never converted into a start date. Calendar subtraction, fiscal-calendar
    assumptions, and numeric/source similarity are inference and remain barred.
    """
    candidate_start = iso_date(row.get("period_start"))
    evidence_start = iso_date(bound_evidence_period_start)
    provenance = json_object(row.get("provenance_json"))
    provenance_starts = sorted(
        {
            parsed
            for key in EXACT_PERIOD_START_KEYS
            if (parsed := iso_date(provenance.get(key)))
        }
    )
    xbrl_links = {
        key: str(provenance.get(key) or "").strip()
        for key in EXACT_XBRL_LINK_KEYS
        if str(provenance.get(key) or "").strip()
    }
    evidence_text = str(row.get("evidence_text") or "")
    duration_phrase = bool(_DURATION_PHRASE.search(evidence_text))
    explicit_range = bool(_EXPLICIT_FULL_DATE_RANGE.search(evidence_text))
    table_locator = any(
        str(row.get(field) or "").strip()
        for field in (
            "semantic_table_id",
            "semantic_block_index",
            "semantic_row_index",
        )
    )
    table_context_hash = bool(
        str(provenance.get("table_context_sha256") or "").strip()
    )

    recovered = ""
    exact = False
    if candidate_start:
        recovered = candidate_start
        reason = "PERIOD_START_ALREADY_PRESENT"
    elif evidence_start:
        recovered = evidence_start
        exact = True
        reason = "EXACT_BOUND_EVIDENCE_PERIOD_START"
    elif len(provenance_starts) == 1:
        recovered = provenance_starts[0]
        exact = True
        reason = "EXACT_BOUND_PROVENANCE_PERIOD_START"
    elif len(provenance_starts) > 1:
        reason = "CONFLICTING_BOUND_PROVENANCE_PERIOD_STARTS"
    elif xbrl_links:
        reason = "BOUND_XBRL_IDENTIFIER_REQUIRES_EXACT_FACT_LOOKUP"
    elif explicit_range:
        reason = "EXPLICIT_DATE_RANGE_REQUIRES_METRIC_LINK_ADJUDICATION"
    elif duration_phrase:
        reason = "DURATION_ONLY_CONTEXT_REQUIRES_CALENDAR_INFERENCE"
    elif table_locator or table_context_hash:
        reason = "TABLE_CONTEXT_WITHOUT_BOUND_PERIOD_START"
    else:
        reason = "NO_BOUND_PERIOD_START_CONTEXT"

    return {
        "candidate_period_start": candidate_start,
        "bound_evidence_period_start": evidence_start,
        "bound_provenance_period_starts_json": json.dumps(
            provenance_starts,
            separators=(",", ":"),
        ),
        "bound_xbrl_links_json": json.dumps(
            xbrl_links,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "duration_phrase_flag": int(duration_phrase),
        "explicit_full_date_range_flag": int(explicit_range),
        "semantic_table_locator_flag": int(bool(table_locator)),
        "table_context_hash_flag": int(table_context_hash),
        "exact_recoverable_flag": int(exact),
        "effective_period_start": recovered,
        "recovery_reason": reason,
    }


def classify_conflict_group(
    candidates: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    if not candidates:
        raise ValueError("period-start feasibility group cannot be empty")
    effective = [
        iso_date(candidate.get("effective_period_start"))
        for candidate in candidates
    ]
    known = {value for value in effective if value}
    missing_count = sum(not value for value in effective)
    recovered_count = sum(
        int(candidate.get("exact_recoverable_flag") or 0)
        for candidate in candidates
    )
    if missing_count == 0 and len(known) == 1:
        category = "EXACT_RECOVERABLE_ALL_CANDIDATES_SAME_BOUND_START"
        group_exact = 1
        recovered_start = next(iter(known))
    elif missing_count == 0:
        category = "COMPLETE_CONFLICTING_PERIOD_STARTS"
        group_exact = 0
        recovered_start = ""
    elif not known:
        category = "ALL_MISSING_NO_EXACT_BOUND_START"
        group_exact = 0
        recovered_start = ""
    elif len(known) == 1:
        category = "MISSING_WITH_ONE_KNOWN_ANCHOR_NO_EXACT_LINK"
        group_exact = 0
        recovered_start = ""
    else:
        category = "MISSING_WITH_MULTIPLE_KNOWN_ANCHORS"
        group_exact = 0
        recovered_start = ""
    return {
        "candidate_count": len(candidates),
        "missing_candidate_count": missing_count,
        "exact_candidate_recovery_count": recovered_count,
        "known_period_starts_json": json.dumps(
            sorted(known),
            separators=(",", ":"),
        ),
        "group_exact_recoverable_flag": group_exact,
        "exact_recovered_period_start": recovered_start,
        "feasibility_category": category,
    }

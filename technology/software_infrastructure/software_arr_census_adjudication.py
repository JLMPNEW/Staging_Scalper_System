from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from typing import Any


MODEL_FAMILY = "software_infrastructure"
METRIC_NAME = "annual_recurring_revenue"
ELIGIBLE_DECISIONS = {"ACCEPTED", "CORRECTED"}

_SPACE_RE = re.compile(r"\s+")
_MONEY_RE = re.compile(
    r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*"
    r"(billion|million|thousand|bn|mm|m|k)?",
    re.IGNORECASE,
)
_MONEY_RANGE_RE = re.compile(
    r"\$\s*[0-9][0-9,.]*\s*"
    r"(?:billion|million|thousand|bn|mm|mn|m|k)?\s*"
    r"(?:[-\u2013\u2014]|to)\s*\$",
    re.IGNORECASE,
)
_ARR_RE = re.compile(
    r"\b(?:arr|annual(?:ized)? recurring revenue|annual run-rate revenue)\b",
    re.IGNORECASE,
)
_TOTAL_RE = re.compile(
    r"\b(?:total arr|total annual(?:ized)? recurring revenue)\b",
    re.IGNORECASE,
)
_ACTUAL_RE = re.compile(
    r"\b(?:was|were|of|at|to|reached|ended|came in|grew|increased|"
    r"remained|stood|exceeded|reaches|surpasses?)\b",
    re.IGNORECASE,
)
_GUIDANCE_RE = re.compile(
    r"\b(?:expect|expects|expected|guidance|outlook|forecast|"
    r"between|range of|target)\b",
    re.IGNORECASE,
)
_NONLEVEL_RE = re.compile(
    r"\b(?:net new arr|booked annual recurring revenue|barr|"
    r"arr contribution|arr per customer|revenue retention rate|"
    r"(?:number of )?customers? (?:with|generating|over|greater than|"
    r"equal or greater than)|"
    r"customer count|threshold)\b",
    re.IGNORECASE,
)
_SUBSET_CONTEXT_RE = re.compile(
    r"\b(?:secaas|secure communications|cloud|subscription|saas|"
    r"maintenance|product|next-generation security|service collection|"
    r"computer backup|b2 cloud storage|ai customer|large customer|"
    r"high-value)\b",
    re.IGNORECASE,
)
_SUBSET_LABEL_RE = re.compile(
    r"^(?:secure communications|subscription|saas|cloud|maintenance|"
    r"product|computer backup|b2 cloud storage)\s+"
    r"(?:annual(?:ized)? recurring revenue|arr)\b",
    re.IGNORECASE,
)
_NAMED_SUBSET_RE = re.compile(
    r"\b(?:high\s*-\s*value|next-generation security|service collection)"
    r"[^.;]{0,80}\b(?:annual(?:ized)? recurring revenue|arr)\b",
    re.IGNORECASE,
)
_OTHER_METRIC_RE = re.compile(
    r"\b(?:free cash flow|bookings|share repurchase)\b",
    re.IGNORECASE,
)


def _clean(value: object) -> str:
    return _SPACE_RE.sub(" ", str(value or "")).strip()


def _money_values(text: str) -> list[tuple[float, int, int]]:
    multipliers = {
        "": 1.0,
        "billion": 1_000_000_000.0,
        "bn": 1_000_000_000.0,
        "million": 1_000_000.0,
        "mm": 1_000_000.0,
        "m": 1_000_000.0,
        "thousand": 1_000.0,
        "k": 1_000.0,
    }
    values: list[tuple[float, int, int]] = []
    for match in _MONEY_RE.finditer(text):
        number = float(match.group(1).replace(",", ""))
        scale = multipliers[str(match.group(2) or "").lower()]
        values.append((number * scale, match.start(), match.end()))
    return values


def _approximately_equal(left: float, right: float) -> bool:
    tolerance = max(1.0, abs(right) * 0.002)
    return abs(left - right) <= tolerance


def _total_arr_amount(text: str) -> float | None:
    total_matches = list(_TOTAL_RE.finditer(text))
    if not total_matches:
        return None
    best: tuple[int, float] | None = None
    for amount, start, end in _money_values(text):
        distance = min(
            min(abs(start - match.end()), abs(match.start() - end))
            + (0 if start >= match.end() else 20)
            for match in total_matches
        )
        if distance <= 80 and (best is None or distance < best[0]):
            best = (distance, amount)
    return None if best is None else best[1]


def propose_arr_candidate(row: dict[str, Any]) -> dict[str, Any]:
    text = _clean(row.get("evidence_text"))
    value = float(row.get("candidate_value") or 0.0)
    base = {
        **row,
        "proposal_decision": "REVIEW_REQUIRED",
        "proposal_reason": "candidate_value_not_unambiguously_linked_to_arr",
        "effective_metric": METRIC_NAME,
        "effective_value": value,
        "effective_unit": str(row.get("unit") or ""),
        "effective_period_end": str(row.get("period_end") or ""),
        "effective_scope": str(row.get("scope") or "unknown"),
        "calibration_eligible_flag": 0,
        "proposal_confidence": 0.0,
        "canonical_candidate_flag": 0,
    }
    if value <= 0:
        return {
            **base,
            "proposal_decision": "REJECTED_POLICY",
            "proposal_reason": "nonpositive_candidate_value",
            "proposal_confidence": 1.0,
        }

    total_arr_amount = _total_arr_amount(text)
    if str(row.get("scope") or "") == "segment":
        return {
            **base,
            "proposal_decision": "REJECTED_POLICY",
            "proposal_reason": "segment_arr_not_company_total",
            "proposal_confidence": 0.99,
        }
    if _NONLEVEL_RE.search(text):
        return {
            **base,
            "proposal_decision": "REJECTED_POLICY",
            "proposal_reason": "nonlevel_arr_flow_threshold_or_customer_metric",
            "proposal_confidence": 0.99,
        }
    if total_arr_amount is not None and not _approximately_equal(
        total_arr_amount, value
    ):
        return {
            **base,
            "proposal_decision": "CORRECTED",
            "proposal_reason": "corrected_subset_candidate_to_explicit_total_arr",
            "effective_value": total_arr_amount,
            "effective_scope": "consolidated",
            "calibration_eligible_flag": 1,
            "proposal_confidence": 0.99,
        }
    if _SUBSET_LABEL_RE.search(text) or _NAMED_SUBSET_RE.search(text):
        return {
            **base,
            "proposal_decision": "REJECTED_POLICY",
            "proposal_reason": "noncomparable_product_or_segment_arr",
            "effective_scope": "subset",
            "proposal_confidence": 0.99,
        }
    if _MONEY_RANGE_RE.search(text):
        return {
            **base,
            "proposal_decision": "REJECTED_POLICY",
            "proposal_reason": "forward_guidance_not_actual_arr",
            "proposal_confidence": 0.99,
        }

    matching_mentions = [
        (start, end)
        for amount, start, end in _money_values(text)
        if _approximately_equal(amount, value)
    ]
    if not matching_mentions:
        return base

    best_window = ""
    best_relation_window = ""
    best_distance = 10_000
    for start, end in matching_mentions:
        for arr_match in _ARR_RE.finditer(text):
            distance = min(
                abs(start - arr_match.end()),
                abs(arr_match.start() - end),
            )
            if distance < best_distance:
                best_distance = distance
                window_start = max(0, min(start, arr_match.start()) - 100)
                window_end = min(len(text), max(end, arr_match.end()) + 100)
                best_window = text[window_start:window_end]
                relation_start = max(
                    0, min(start, arr_match.start()) - 30
                )
                relation_end = min(
                    len(text), max(end, arr_match.end()) + 15
                )
                best_relation_window = text[relation_start:relation_end]
    if not best_window or best_distance > 120:
        return base
    if _OTHER_METRIC_RE.search(best_window):
        return {
            **base,
            "proposal_decision": "REJECTED_POLICY",
            "proposal_reason": "candidate_value_belongs_to_adjacent_non_arr_metric",
            "proposal_confidence": 0.98,
        }
    if _GUIDANCE_RE.search(best_window):
        return {
            **base,
            "proposal_decision": "REJECTED_POLICY",
            "proposal_reason": "forward_guidance_not_actual_arr",
            "proposal_confidence": 0.99,
        }
    if _NONLEVEL_RE.search(best_window):
        return {
            **base,
            "proposal_decision": "REJECTED_POLICY",
            "proposal_reason": "nonlevel_arr_flow_threshold_or_customer_metric",
            "proposal_confidence": 0.99,
        }
    total_match = _TOTAL_RE.search(best_window)
    if (
        _SUBSET_CONTEXT_RE.search(best_relation_window)
        and total_match is None
    ):
        return {
            **base,
            "proposal_decision": "REJECTED_POLICY",
            "proposal_reason": "noncomparable_product_or_segment_arr",
            "effective_scope": "subset",
            "proposal_confidence": 0.98,
        }
    if total_match is None and (
        _ARR_RE.search(best_window) is None
        or _ACTUAL_RE.search(best_window) is None
    ):
        return base

    source_scope = str(row.get("scope") or "unknown")
    decision = "ACCEPTED" if source_scope == "consolidated" else "CORRECTED"
    confidence = 0.99 if total_match is not None else 0.92
    return {
        **base,
        "proposal_decision": decision,
        "proposal_reason": (
            "explicit_total_arr_level"
            if total_match is not None
            else "direct_company_arr_level_scope_corrected"
        ),
        "effective_scope": "consolidated",
        "calibration_eligible_flag": 1,
        "proposal_confidence": confidence,
    }


def load_census_arr_evidence(
    conn: sqlite3.Connection,
    *,
    accessions: set[str],
) -> list[dict[str, Any]]:
    if not accessions:
        return []
    placeholders = ",".join("?" for _ in accessions)
    rows = conn.execute(
        f"""
        SELECT *
        FROM sec_parser_metric_evidence_shadow
        WHERE model_family = ?
          AND metric_name = ?
          AND candidate_value IS NOT NULL
          AND candidate_status IN ('REVIEW_REQUIRED', 'REJECTED_POLICY')
          AND accession_number IN ({placeholders})
        ORDER BY ticker, accepted_at, accession_number, evidence_key
        """,
        (MODEL_FAMILY, METRIC_NAME, *sorted(accessions)),
    ).fetchall()
    return [dict(row) for row in rows]


def build_arr_proposals(
    evidence_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    deduplicated: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in evidence_rows:
        key = (
            str(row["ticker"]),
            str(row["accession_number"]),
            round(float(row["candidate_value"]), 4),
            str(row.get("unit") or ""),
            str(row.get("period_end") or ""),
            _clean(row.get("evidence_text")).lower(),
        )
        existing = deduplicated.get(key)
        if existing is None or str(row["source_document"]) < str(
            existing["source_document"]
        ):
            deduplicated[key] = row

    proposals = [propose_arr_candidate(row) for row in deduplicated.values()]
    by_accession: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for proposal in proposals:
        by_accession[
            (str(proposal["ticker"]), str(proposal["accession_number"]))
        ].append(proposal)

    output: list[dict[str, Any]] = []
    for candidates in by_accession.values():
        eligible = [
            row
            for row in candidates
            if row["proposal_decision"] in ELIGIBLE_DECISIONS
        ]
        if eligible:
            selected = max(
                eligible,
                key=lambda row: (
                    float(row["proposal_confidence"]),
                    int(str(row.get("scope") or "") == "consolidated"),
                    float(row["effective_value"]),
                    str(row["source_evidence_key"])
                    if "source_evidence_key" in row
                    else str(row["evidence_key"]),
                ),
            )
            selected["canonical_candidate_flag"] = 1
            selected_key = str(selected["evidence_key"])
            for row in candidates:
                if str(row["evidence_key"]) == selected_key:
                    continue
                if row["proposal_decision"] == "REJECTED_POLICY":
                    continue
                row["proposal_decision"] = "REJECTED_POLICY"
                row["proposal_reason"] = (
                    "noncanonical_duplicate_or_comparative_in_accession"
                )
                row["calibration_eligible_flag"] = 0
                row["proposal_confidence"] = 0.98
        output.extend(candidates)
    return sorted(
        output,
        key=lambda row: (
            str(row["ticker"]),
            str(row["accepted_at"]),
            str(row["accession_number"]),
            -int(row["canonical_candidate_flag"]),
            str(row["evidence_key"]),
        ),
    )


def summarize_arr_proposals(
    rows: list[dict[str, Any]],
    *,
    minimum_cross_section: int = 30,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_ticker[str(row["ticker"])].append(row)
    ticker_rows: list[dict[str, Any]] = []
    for ticker, candidates in sorted(by_ticker.items()):
        accepted_accessions = {
            str(row["accession_number"])
            for row in candidates
            if int(row["canonical_candidate_flag"]) == 1
            and row["proposal_decision"] in ELIGIBLE_DECISIONS
        }
        review_accessions = {
            str(row["accession_number"])
            for row in candidates
            if row["proposal_decision"] == "REVIEW_REQUIRED"
        }
        ticker_rows.append(
            {
                "ticker": ticker,
                "proposed_accepted_event_count": len(accepted_accessions),
                "unresolved_review_event_count": len(review_accessions),
                "strict_level_candidate_flag": int(bool(accepted_accessions)),
                "strict_longitudinal_candidate_flag": int(
                    len(accepted_accessions) >= 2
                ),
                "upper_bound_level_candidate_flag": int(
                    bool(accepted_accessions | review_accessions)
                ),
                "upper_bound_longitudinal_candidate_flag": int(
                    len(accepted_accessions | review_accessions) >= 2
                ),
            }
        )
    strict_level = sum(row["strict_level_candidate_flag"] for row in ticker_rows)
    strict_longitudinal = sum(
        row["strict_longitudinal_candidate_flag"] for row in ticker_rows
    )
    upper_level = sum(
        row["upper_bound_level_candidate_flag"] for row in ticker_rows
    )
    upper_longitudinal = sum(
        row["upper_bound_longitudinal_candidate_flag"] for row in ticker_rows
    )
    unresolved = sum(
        int(row["proposal_decision"] == "REVIEW_REQUIRED") for row in rows
    )
    summary = {
        "minimum_cross_section_required": minimum_cross_section,
        "canonical_proposed_fact_count": sum(
            int(row["canonical_candidate_flag"]) for row in rows
        ),
        "strict_level_ticker_count": strict_level,
        "strict_longitudinal_ticker_count": strict_longitudinal,
        "upper_bound_level_ticker_count": upper_level,
        "upper_bound_longitudinal_ticker_count": upper_longitudinal,
        "unresolved_review_row_count": unresolved,
        "unresolved_review_required_flag": int(unresolved > 0),
        "human_approval_required_flag": int(bool(rows)),
        "historical_hydration_authorized_flag": 0,
        "branch_recommendation": (
            "CLOSE_AFTER_PROPOSAL_UPPER_BOUND_BELOW_GATE"
            if upper_level < minimum_cross_section
            and upper_longitudinal < minimum_cross_section
            else "RESOLVE_EXCEPTIONS_AND_HUMAN_APPROVE"
            if unresolved
            else "HUMAN_APPROVAL_REQUIRED_BEFORE_HISTORICAL_HYDRATION"
        ),
        "proposal_only_flag": 1,
        "production_facts_modified_flag": 0,
        "production_scores_modified_flag": 0,
    }
    return ticker_rows, summary

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from industrials.transportation.adjudication import policy_match_key


CONFLICT_RESOLUTION_VERSION = (
    "transportation_accepted_conflict_resolution_v1"
)


@dataclass(frozen=True)
class AcceptedConflictResolution:
    ticker: str
    metric_name: str
    period_end: str
    winner_base_evidence_key: str
    loser_base_evidence_key: str
    winner_value: float
    loser_value: float
    winner_scope: str
    loser_reason: str
    source_document: str
    document_sha256: str


ACCEPTED_CONFLICT_RESOLUTIONS = (
    AcceptedConflictResolution(
        ticker="ALK",
        metric_name="passenger_load_factor",
        period_end="2023-12-31",
        winner_base_evidence_key=(
            "37cbfb4c88eb279b68b622107df06b8f"
            "44224345ec76472b75df654ab85c0a9c"
        ),
        loser_base_evidence_key=(
            "3b5f6262ee37e9f6a599eef9cf296cce"
            "05bdb5cc940c172ece4cb4a8d6c18ffb"
        ),
        winner_value=0.837,
        loser_value=0.838,
        winner_scope="consolidated",
        loser_reason=(
            "regional_operating_statistic_suppressed_in_favor_of_"
            "explicit_consolidated_operating_statistic"
        ),
        source_document="alk-20231231.htm",
        document_sha256=(
            "19764b69abdb61ea9963d75fd8a38ba5"
            "8f3564a59b6d38811c8d8fb02ddf43ea"
        ),
    ),
    AcceptedConflictResolution(
        ticker="UAL",
        metric_name="passenger_load_factor",
        period_end="2017-12-31",
        winner_base_evidence_key=(
            "692bcbe74b97a86eae6d07003d725ecc"
            "12183085e510516b1e8ba2c550fa15cb"
        ),
        loser_base_evidence_key=(
            "4297ca72ac116c2d14b5c69d44e75713"
            "74d2c9b1342e6bd0fefb06e203664804"
        ),
        winner_value=0.824,
        loser_value=0.825,
        winner_scope="consolidated",
        loser_reason=(
            "unlabeled_operating_statistic_suppressed_in_favor_of_"
            "explicit_consolidated_operating_statistic"
        ),
        source_document="d471340d10k.htm",
        document_sha256=(
            "ee0511458c1957379e7f470d97e62c71"
            "e530b8f714604f47c3d99c3532596d37"
        ),
    ),
)


def numeric_equal(left: object, right: object) -> bool:
    try:
        first = float(str(left))
        second = float(str(right))
    except (TypeError, ValueError):
        return False
    if not math.isfinite(first) or not math.isfinite(second):
        return False
    return abs(first - second) <= max(1e-6, abs(second) * 1e-9)


def replace_exact_policy(
    rows: Sequence[Mapping[str, str]],
    *,
    policy_id: str,
    replacement: Mapping[str, str],
) -> list[dict[str, str]]:
    matched = [row for row in rows if row["policy_id"] == policy_id]
    if len(matched) != 1:
        raise ValueError(
            f"policy_id={policy_id}: expected one active row, "
            f"found {len(matched)}"
        )
    prior = matched[0]
    if prior["decision"] != "ACCEPTED":
        raise ValueError(
            f"policy_id={policy_id}: expected ACCEPTED, "
            f"found {prior['decision']}"
        )
    if policy_match_key(prior) != policy_match_key(replacement):
        raise ValueError(
            f"policy_id={policy_id}: replacement changed exact match key"
        )
    if replacement["decision"] not in {
        "REJECTED_POLICY",
        "SUPPRESSED_SEMANTIC_DUPLICATE",
    }:
        raise ValueError(
            f"policy_id={policy_id}: replacement must fail closed"
        )
    return [
        (
            dict(replacement)
            if row["policy_id"] == policy_id
            else dict(row)
        )
        for row in rows
    ]

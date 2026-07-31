from __future__ import annotations

import pytest

from industrials.transportation.accepted_conflicts import (
    numeric_equal,
    replace_exact_policy,
)


def _policy(
    policy_id: str,
    *,
    decision: str = "ACCEPTED",
    candidate_value: str = "0.838",
) -> dict[str, str]:
    return {
        "policy_id": policy_id,
        "model_family": "transportation",
        "ticker": "ALK",
        "accession_number": "accession",
        "source_document": "filing.htm",
        "metric_name": "passenger_load_factor",
        "concept_name": "TransportationDiscoveryPassengerLoadFactor",
        "candidate_value": candidate_value,
        "unit": "ratio",
        "period_start": "",
        "period_end": "2023-12-31",
        "decision": decision,
    }


def test_replace_exact_policy_preserves_match_and_row_count() -> None:
    rows = [_policy("winner", candidate_value="0.837"), _policy("loser")]
    replacement = _policy(
        "replacement",
        decision="SUPPRESSED_SEMANTIC_DUPLICATE",
    )

    output = replace_exact_policy(
        rows,
        policy_id="loser",
        replacement=replacement,
    )

    assert len(output) == len(rows)
    assert {row["policy_id"] for row in output} == {
        "winner",
        "replacement",
    }
    assert [row["policy_id"] for row in output] == [
        "winner",
        "replacement",
    ]


def test_replace_exact_policy_rejects_changed_match_key() -> None:
    rows = [_policy("loser")]
    replacement = _policy(
        "replacement",
        decision="SUPPRESSED_SEMANTIC_DUPLICATE",
    )
    replacement["period_end"] = "2022-12-31"

    with pytest.raises(ValueError, match="changed exact match key"):
        replace_exact_policy(
            rows,
            policy_id="loser",
            replacement=replacement,
        )


def test_numeric_equal_allows_database_float_representation() -> None:
    assert numeric_equal(0.8370000000000001, 0.837)
    assert not numeric_equal(0.838, 0.837)

from __future__ import annotations

from industrials.transportation.adjudication import (
    accepted_final_metric,
    build_legacy_index,
    confirmation_basis,
    legacy_metric_ids,
    lockable_rejection,
    value_matches,
)


def _evidence(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "ticker": "TEST",
        "metric_name": "operating_ratio",
        "accession_number": "0000000001-26-000001",
        "source_document": "test-20251231.htm",
        "candidate_value": 0.72,
        "unit": "ratio",
        "period_start": "",
        "period_end": "2025-12-31",
        "scope": "consolidated",
        "candidate_status": "REVIEW_REQUIRED",
        "status_reason": (
            "broad_discovery_candidate_requires_metric_fixture_review"
        ),
    }
    row.update(overrides)
    return row


def _legacy(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "ticker": "TEST",
        "metric_name": "operating_ratio",
        "accession_number": "0000000001-26-000001",
        "document_name": "test-20251231.htm",
        "candidate_value": 0.72,
        "unit": "ratio",
        "period_end": "2025-12-31",
        "candidate_status": "ACCEPTED",
    }
    row.update(overrides)
    return row


def test_exact_prior_accepted_disclosure_confirms_review_evidence() -> None:
    legacy = build_legacy_index([_legacy()])

    basis = confirmation_basis(
        _evidence(),
        final_metric_id="operating_ratio",
        legacy_index=legacy,
    )

    assert basis == "EXACT_ACCEPTED_LEGACY_MATCH"


def test_reviewed_composite_mapping_is_explicit_and_exact() -> None:
    legacy = build_legacy_index(
        [
            _legacy(
                metric_name="load_factor_or_utilization",
                candidate_value=0.81,
            )
        ]
    )
    evidence = _evidence(
        metric_name="passenger_load_factor",
        candidate_value=0.81,
    )

    assert legacy_metric_ids(
        final_metric_id="passenger_load_factor",
        evidence_metric_id="passenger_load_factor",
    ) == ("passenger_load_factor", "load_factor_or_utilization")
    assert confirmation_basis(
        evidence,
        final_metric_id="passenger_load_factor",
        legacy_index=legacy,
    ) == "EXACT_REVIEWED_MAPPING:load_factor_or_utilization"


def test_nonissuer_or_changed_value_cannot_be_confirmed() -> None:
    legacy = build_legacy_index([_legacy()])

    assert not confirmation_basis(
        _evidence(scope="nonissuer"),
        final_metric_id="operating_ratio",
        legacy_index=legacy,
    )
    assert not confirmation_basis(
        _evidence(candidate_value=0.73),
        final_metric_id="operating_ratio",
        legacy_index=legacy,
    )
    assert value_matches(0.72, 0.72)
    assert not value_matches(0.72, 0.73)


def test_series_derivation_requires_two_confirmed_periods() -> None:
    one = [_evidence(metric_name="fleet_capacity")]
    two = [
        *one,
        _evidence(
            metric_name="fleet_capacity",
            period_end="2024-12-31",
        ),
    ]

    assert not accepted_final_metric(
        final_metric_id="fleet_capacity_growth",
        source_lane="DP-D",
        confirmed_evidence=one,
    )
    assert accepted_final_metric(
        final_metric_id="fleet_capacity_growth",
        source_lane="DP-D",
        confirmed_evidence=two,
    )


def test_only_frozen_contract_rejections_with_exact_values_are_locked() -> None:
    assert lockable_rejection(
        _evidence(
            candidate_status="REJECTED_POLICY",
            status_reason="ratio_value_out_of_bounds",
        )
    )
    assert not lockable_rejection(
        _evidence(
            candidate_status="REJECTED_POLICY",
            status_reason="ratio_value_out_of_bounds",
            candidate_value=None,
        )
    )
    assert not lockable_rejection(
        _evidence(
            candidate_status="REJECTED_POLICY",
            status_reason="broad_discovery_candidate_requires_metric_fixture_review",
        )
    )

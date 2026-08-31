"""Observed-only PIT census accounting for loaded identities.

This additive version removes the ambiguous ``0`` overlap value assigned by
V2 to loaded identities that were absent from candidate discovery.  A zero is
evidence of a completed query with no overlap; an absent query is represented
as ``None`` and counted separately.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence

from .historical_census_reconciliation_v3 import (
    reconcile_historical_candidates_v3,
)


def reconcile_historical_candidates_v4(
    conn: Any,
    rows: Sequence[Mapping[str, Any]],
    *,
    reviewed_pit_overrides: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Reconcile the full identity union without inventing negative queries."""

    reconciled, summary = reconcile_historical_candidates_v3(
        conn,
        rows,
        reviewed_pit_overrides=reviewed_pit_overrides,
    )
    for row in reconciled:
        if int(row.get('candidate_discovery_present_flag') or 0) == 1:
            row['pit_index_membership_query_status'] = (
                'observed_in_candidate_discovery'
            )
            continue
        if int(row.get('reviewed_pit_membership_override_flag') or 0) == 1:
            row['pit_index_membership_query_status'] = (
                'verified_by_reviewed_pit_override'
            )
            continue
        row['pit_index_membership_overlap_flag'] = None
        row['pit_index_membership_query_status'] = (
            'not_queried_for_loaded_identity_absent_discovery'
        )

    observed_rows = [
        row for row in reconciled
        if row.get('pit_index_membership_overlap_flag') in (0, 1)
    ]
    unqueried_loaded = [
        row for row in reconciled
        if row.get('pit_index_membership_query_status')
        == 'not_queried_for_loaded_identity_absent_discovery'
    ]
    status_counts = Counter(
        str(row['reconciliation_status']) for row in reconciled
    )
    query_status_counts = Counter(
        str(row.get('pit_index_membership_query_status') or 'missing')
        for row in reconciled
    )
    summary = {
        **summary,
        'status': 'HISTORICAL_CENSUS_RECONCILIATION_REVIEW_ONLY_V4',
        'pit_membership_observed_identity_count': len(observed_rows),
        'pit_membership_overlap_count': sum(
            int(row['pit_index_membership_overlap_flag'])
            for row in observed_rows
        ),
        'loaded_pit_overlap_identity_count': sum(
            int(row.get('loaded_identity_match_flag') or 0)
            * int(row['pit_index_membership_overlap_flag'])
            for row in observed_rows
        ),
        'unqueried_loaded_identity_count': len(unqueried_loaded),
        'unqueried_loaded_asset_ids': sorted(
            str(row['provider_asset_id']) for row in unqueried_loaded
        ),
        'pit_membership_query_status_counts': dict(
            sorted(query_status_counts.items())
        ),
        'point_in_time_taxonomy_verified': False,
        'survivorship_corrected_panel_ready': False,
        'production_or_calibration_use_allowed': False,
        'status_counts': dict(sorted(status_counts.items())),
    }
    return reconciled, summary


__all__ = ['reconcile_historical_candidates_v4']

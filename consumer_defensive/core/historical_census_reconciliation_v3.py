"""Reviewed PIT-overlap overlays for the fail-closed census union."""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence

from .historical_census_reconciliation_v2 import (
    reconcile_historical_candidates_v2,
)


def _validated_overrides(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    required = {
        'provider_asset_id',
        'provider_symbol',
        'pit_overlap_start',
        'pit_overlap_end',
        'pit_session_count',
        'pit_index_memberships',
        'current_or_final_sector',
        'review_source',
        'reviewed_flag',
    }
    for source in rows:
        missing = sorted(required - set(source))
        if missing:
            raise ValueError(f'Reviewed PIT override missing fields: {missing}')
        row = dict(source)
        asset_id = str(row['provider_asset_id'] or '')
        if not asset_id or asset_id in output:
            raise ValueError(
                f'Reviewed PIT override asset ID is blank or duplicate: {asset_id}'
            )
        memberships = row['pit_index_memberships']
        if (
            row['reviewed_flag'] is not True
            or not str(row['review_source']).strip()
            or not isinstance(memberships, list)
            or not memberships
            or int(row['pit_session_count']) <= 0
            or str(row['pit_overlap_start']) > str(row['pit_overlap_end'])
        ):
            raise ValueError(f'Invalid reviewed PIT override for {asset_id}.')
        output[asset_id] = row
    return output


def reconcile_historical_candidates_v3(
    conn: Any,
    rows: Sequence[Mapping[str, Any]],
    *,
    reviewed_pit_overrides: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    reconciled, summary = reconcile_historical_candidates_v2(conn, rows)
    overrides = _validated_overrides(reviewed_pit_overrides)
    matched_overrides: list[dict[str, Any]] = []
    for row in reconciled:
        if int(row.get('candidate_discovery_present_flag') or 0) == 1:
            continue
        row['external_pit_membership_review_required'] = 1
        asset_id = str(row.get('provider_asset_id') or '')
        override = overrides.get(asset_id)
        if override is None:
            continue
        row.update({
            'provider_symbol': str(override['provider_symbol']),
            'pit_index_membership_overlap_flag': 1,
            'pit_overlap_start': str(override['pit_overlap_start']),
            'pit_overlap_end': str(override['pit_overlap_end']),
            'pit_session_count': int(override['pit_session_count']),
            'pit_index_memberships': list(override['pit_index_memberships']),
            'current_or_final_sector': str(
                override['current_or_final_sector']
            ),
            'pit_membership_review_source': str(override['review_source']),
            'reviewed_pit_membership_override_flag': 1,
            'external_pit_membership_review_required': 0,
            # Membership overlap is verified; PIT Consumer Defensive taxonomy
            # is deliberately not inferred from current/final taxonomy.
            'point_in_time_taxonomy_verified': 0,
            'taxonomy_review_required': 1,
            'production_or_calibration_use_allowed': 0,
            'reconciliation_status': (
                'loaded_identity_with_reviewed_pit_overlap_taxonomy_review_required'
            ),
        })
        matched_overrides.append({
            key: row[key]
            for key in (
                'provider_asset_id', 'provider_symbol', 'loaded_ticker',
                'pit_overlap_start', 'pit_overlap_end', 'pit_session_count',
                'pit_index_memberships', 'current_or_final_sector',
                'pit_membership_review_source',
            )
        })
    unmatched = sorted(set(overrides) - {
        str(row['provider_asset_id']) for row in matched_overrides
    })
    if unmatched:
        raise ValueError(
            'Reviewed PIT overrides do not match loaded-only identities: '
            f'{unmatched}'
        )
    status_counts = Counter(
        str(row['reconciliation_status']) for row in reconciled
    )
    summary = {
        **summary,
        'status': 'HISTORICAL_CENSUS_RECONCILIATION_REVIEW_ONLY_V3',
        'pit_membership_overlap_count': sum(
            int(row.get('pit_index_membership_overlap_flag') or 0)
            for row in reconciled
        ),
        'loaded_pit_overlap_identity_count': sum(
            int(row.get('loaded_identity_match_flag') or 0)
            * int(row.get('pit_index_membership_overlap_flag') or 0)
            for row in reconciled
        ),
        'reviewed_pit_override_count': len(matched_overrides),
        'reviewed_pit_override_identities': matched_overrides,
        'external_pit_membership_review_required_count': sum(
            int(row.get('external_pit_membership_review_required') or 0)
            for row in reconciled
        ),
        'point_in_time_taxonomy_verified': False,
        'survivorship_corrected_panel_ready': False,
        'production_or_calibration_use_allowed': False,
        'status_counts': dict(sorted(status_counts.items())),
    }
    return reconciled, summary


__all__ = ['reconcile_historical_candidates_v3']

"""Native strict reconciliation with PIT exit at or before delisting."""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any, Mapping, Sequence

from .historical_census_reconciliation_v3 import (
    reconcile_historical_candidates_v3,
)
from .historical_census_reconciliation_v5 import (
    REVIEWED_PIT_OVERRIDE_SCHEMA_V1,
    reviewed_pit_override_sha256,
)


_OVERRIDE_FIELDS = {
    'schema_version', 'provider_asset_id', 'provider_symbol',
    'loaded_ticker', 'loaded_company_id', 'loaded_security_id',
    'delisted_date', 'pit_overlap_verified_flag', 'pit_overlap_start',
    'pit_overlap_end', 'pit_session_count', 'pit_index_memberships',
    'current_or_final_sector', 'current_or_final_industry',
    'local_norgate_snapshot_asof_date', 'reviewed_at_date',
    'review_source', 'review_rationale', 'reviewed_flag',
    'point_in_time_taxonomy_verified', 'taxonomy_review_required',
    'production_or_calibration_use_allowed', 'record_sha256',
}


def _strict_overrides_native(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in rows:
        row = dict(source)
        if set(row) != _OVERRIDE_FIELDS:
            raise ValueError(
                'Reviewed PIT override fields do not match the strict schema: '
                f'missing={sorted(_OVERRIDE_FIELDS - set(row))}, '
                f'unknown={sorted(set(row) - _OVERRIDE_FIELDS)}'
            )
        if row['schema_version'] != REVIEWED_PIT_OVERRIDE_SCHEMA_V1:
            raise ValueError('Unsupported reviewed PIT override schema.')
        for field in (
            'delisted_date', 'pit_overlap_start', 'pit_overlap_end',
            'local_norgate_snapshot_asof_date', 'reviewed_at_date',
        ):
            date.fromisoformat(str(row[field]))
        asset_id = str(row['provider_asset_id'] or '')
        if not asset_id or asset_id in seen:
            raise ValueError(
                f'Reviewed PIT override asset ID is blank or duplicate: {asset_id}'
            )
        seen.add(asset_id)
        memberships = row['pit_index_memberships']
        if (
            not isinstance(memberships, list)
            or memberships != sorted(set(str(value) for value in memberships))
            or not memberships
        ):
            raise ValueError(
                'Reviewed PIT memberships must be nonempty, sorted, and unique.'
            )
        if (
            row['reviewed_flag'] is not True
            or row['pit_overlap_verified_flag'] is not True
            or row['point_in_time_taxonomy_verified'] is not False
            or row['taxonomy_review_required'] is not True
            or row['production_or_calibration_use_allowed'] is not False
            or int(row['pit_session_count']) <= 0
            or str(row['pit_overlap_start']) > str(row['pit_overlap_end'])
            or str(row['pit_overlap_end']) > str(row['delisted_date'])
            or not str(row['review_source']).strip()
            or not str(row['review_rationale']).strip()
        ):
            raise ValueError('Reviewed PIT override fails fail-closed invariants.')
        if (
            str(row['record_sha256']).lower()
            != reviewed_pit_override_sha256(row)
        ):
            raise ValueError('Reviewed PIT override record SHA-256 mismatch.')
        output.append(row)
    return output


def _normalize_discovered_overlap(row: dict[str, Any]) -> int | None:
    error = str(row.get('pit_index_membership_query_error') or '').strip()
    if error:
        row['pit_index_membership_overlap_flag'] = None
        row['pit_index_membership_query_status'] = 'query_error'
        row['reconciliation_status'] = (
            'pit_membership_query_error_review_required'
        )
        return None
    value = row.get('pit_index_membership_overlap_flag')
    if isinstance(value, bool):
        normalized = int(value)
    elif isinstance(value, int) and value in (0, 1):
        normalized = value
    elif isinstance(value, str) and value.strip() in {'0', '1'}:
        normalized = int(value.strip())
    else:
        raise ValueError(
            'Discovered PIT query requires an explicit 0/1 overlap result; '
            f'asset={row.get("provider_asset_id")!r}, value={value!r}'
        )
    row['pit_index_membership_overlap_flag'] = normalized
    row['pit_index_membership_query_status'] = (
        'query_completed_overlap'
        if normalized == 1
        else 'query_completed_no_overlap'
    )
    return normalized


def reconcile_historical_candidates_v7(
    conn: Any,
    rows: Sequence[Mapping[str, Any]],
    *,
    reviewed_pit_overrides: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Preserve reviewed records byte-semantically through reconciliation."""

    overrides = _strict_overrides_native(reviewed_pit_overrides)
    reconciled, summary = reconcile_historical_candidates_v3(
        conn,
        rows,
        reviewed_pit_overrides=overrides,
    )
    overrides_by_id = {
        str(row['provider_asset_id']): row for row in overrides
    }
    for row in reconciled:
        if int(row.get('candidate_discovery_present_flag') or 0) == 1:
            _normalize_discovered_overlap(row)
            continue
        if int(row.get('reviewed_pit_membership_override_flag') or 0) == 1:
            override = overrides_by_id[str(row['provider_asset_id'])]
            if (
                str(row['loaded_ticker']) != str(override['loaded_ticker'])
                or int(row['loaded_company_id'])
                != int(override['loaded_company_id'])
                or int(row['loaded_security_id'])
                != int(override['loaded_security_id'])
            ):
                raise ValueError(
                    'Reviewed PIT override loaded identity does not match DB.'
                )
            row.update({
                key: value for key, value in override.items()
                if key not in {
                    'provider_asset_id', 'provider_symbol', 'loaded_ticker',
                    'loaded_company_id', 'loaded_security_id',
                }
            })
            row['pit_index_membership_overlap_flag'] = 1
            row['pit_index_membership_query_status'] = (
                'verified_by_reviewed_pit_override'
            )
            row['pit_exit_before_or_at_delisting_verified_flag'] = 1
            continue
        row['pit_index_membership_overlap_flag'] = None
        row['pit_index_membership_query_status'] = (
            'not_queried_for_loaded_identity_absent_discovery'
        )

    observed_rows = [
        row for row in reconciled
        if isinstance(row.get('pit_index_membership_overlap_flag'), int)
    ]
    unqueried_loaded = [
        row for row in reconciled
        if row.get('pit_index_membership_query_status')
        == 'not_queried_for_loaded_identity_absent_discovery'
    ]
    query_error_rows = [
        row for row in reconciled
        if row.get('pit_index_membership_query_status') == 'query_error'
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
        'status': 'HISTORICAL_CENSUS_RECONCILIATION_REVIEW_ONLY_V7',
        'pit_exit_delisting_policy': 'pit_overlap_end_lte_delisted_date',
        'reviewed_record_hash_preserved_end_to_end_flag': 1,
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
        'pit_membership_query_error_count': len(query_error_rows),
        'pit_membership_query_error_asset_ids': sorted(
            str(row.get('provider_asset_id') or '')
            for row in query_error_rows
        ),
        'pit_membership_query_status_counts': dict(
            sorted(query_status_counts.items())
        ),
        'reviewed_override_record_sha256s': sorted(
            str(row['record_sha256']) for row in overrides
        ),
        'point_in_time_taxonomy_verified': False,
        'survivorship_corrected_panel_ready': False,
        'production_or_calibration_use_allowed': False,
        'status_counts': dict(sorted(status_counts.items())),
    }
    return reconciled, summary


__all__ = ['reconcile_historical_candidates_v7']

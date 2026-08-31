"""Fail-closed union reconciliation of discovered and already-loaded assets."""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence


def _loaded_norgate_identities(conn: Any) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """SELECT i.identifier_value,s.ticker,s.company_id,s.security_id
           FROM dim_identifier AS i
           JOIN dim_security AS s ON s.security_id=i.security_id
           WHERE i.identifier_type='norgate_assetid'
           ORDER BY i.identifier_value,s.ticker"""
    ).fetchall()
    output: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    for raw in rows:
        asset_id = str(raw[0] or '')
        value = {
            'loaded_ticker': str(raw[1] or ''),
            'loaded_company_id': int(raw[2]),
            'loaded_security_id': int(raw[3]),
        }
        if not asset_id:
            raise RuntimeError('Loaded Norgate identity has a blank asset ID.')
        if asset_id in output and output[asset_id] != value:
            duplicates.add(asset_id)
        output[asset_id] = value
    if duplicates:
        raise RuntimeError(
            'Norgate asset IDs map to multiple loaded securities: '
            f'{sorted(duplicates)}'
        )
    return output


def reconcile_historical_candidates_v2(
    conn: Any,
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return the union of discovered candidates and loaded provider assets.

    Loaded Norgate identities missing from discovery are appended as explicit
    review-only rows. No appended or discovered row is made eligible for
    calibration or production by reconciliation alone.
    """

    loaded = _loaded_norgate_identities(conn)
    seen_assets: set[str] = set()
    duplicates: set[str] = set()
    output: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        row.update({
            'candidate_discovery_present_flag': 1,
            'loaded_identity_missing_from_candidate_input_flag': 0,
            'taxonomy_review_required': 1,
            'point_in_time_taxonomy_verified': 0,
            'production_or_calibration_use_allowed': 0,
        })
        asset_id = str(row.get('provider_asset_id') or '')
        if not asset_id:
            row.update({
                'reconciliation_status': 'missing_provider_asset_id',
                'loaded_identity_match_flag': 0,
                'loaded_ticker': '',
                'loaded_company_id': '',
                'loaded_security_id': '',
                'unloaded_candidate_review_flag': 0,
                'identity_review_required': 1,
            })
            output.append(row)
            continue
        if asset_id in seen_assets:
            duplicates.add(asset_id)
        seen_assets.add(asset_id)
        match = loaded.get(asset_id)
        overlap = int(row.get('pit_index_membership_overlap_flag') or 0) == 1
        row.update({
            'loaded_identity_match_flag': int(match is not None),
            'loaded_ticker': match['loaded_ticker'] if match else '',
            'loaded_company_id': match['loaded_company_id'] if match else '',
            'loaded_security_id': match['loaded_security_id'] if match else '',
            'unloaded_candidate_review_flag': int(overlap and match is None),
            'identity_review_required': int(match is None),
        })
        if not overlap:
            row['reconciliation_status'] = 'outside_pit_membership_window'
        elif match is not None:
            row['reconciliation_status'] = 'already_loaded_identity'
        else:
            row['reconciliation_status'] = (
                'unloaded_identity_and_taxonomy_review_required'
            )
        output.append(row)
    if duplicates:
        raise RuntimeError(
            'Candidate census contains duplicate Norgate asset IDs: '
            f'{sorted(duplicates)}'
        )

    missing_loaded_asset_ids = sorted(set(loaded) - seen_assets)
    missing_loaded_identities: list[dict[str, Any]] = []
    for asset_id in missing_loaded_asset_ids:
        match = loaded[asset_id]
        identity = {
            'provider_asset_id': asset_id,
            'provider_symbol': match['loaded_ticker'],
            'loaded_ticker': match['loaded_ticker'],
            'loaded_company_id': match['loaded_company_id'],
            'loaded_security_id': match['loaded_security_id'],
        }
        missing_loaded_identities.append(identity)
        output.append({
            **identity,
            'candidate_discovery_present_flag': 0,
            'loaded_identity_missing_from_candidate_input_flag': 1,
            'pit_index_membership_overlap_flag': 0,
            'loaded_identity_match_flag': 1,
            'unloaded_candidate_review_flag': 0,
            'point_in_time_taxonomy_verified': 0,
            'identity_review_required': 0,
            'taxonomy_review_required': 1,
            'production_or_calibration_use_allowed': 0,
            'reconciliation_status': (
                'loaded_identity_absent_from_candidate_discovery_review_required'
            ),
        })

    counts = Counter(str(row['reconciliation_status']) for row in output)
    unloaded = [
        row for row in output
        if int(row['unloaded_candidate_review_flag']) == 1
    ]
    unloaded_by_industry = Counter(
        str(row.get('gics_industry_current_or_final') or 'UNCLASSIFIED')
        for row in unloaded
    )
    unloaded_by_catalog_status = Counter(
        str(row.get('catalog_status') or 'UNKNOWN') for row in unloaded
    )
    summary = {
        'status': 'HISTORICAL_CENSUS_RECONCILIATION_REVIEW_ONLY_V2',
        'candidate_input_count': len(rows),
        'candidate_input_asset_id_count': len(seen_assets),
        'reconciled_union_count': len(output),
        'candidate_count': len(output),
        'loaded_identity_count': len(loaded),
        'missing_loaded_identity_count': len(missing_loaded_identities),
        'missing_loaded_asset_ids': missing_loaded_asset_ids,
        'missing_loaded_identities': missing_loaded_identities,
        'pit_membership_overlap_count': sum(
            int(row.get('pit_index_membership_overlap_flag') or 0)
            for row in output
        ),
        'already_loaded_identity_count': sum(
            int(row['loaded_identity_match_flag']) for row in output
        ),
        'loaded_pit_overlap_identity_count': sum(
            int(row['loaded_identity_match_flag'])
            * int(row.get('pit_index_membership_overlap_flag') or 0)
            for row in output
        ),
        'unloaded_candidate_review_count': sum(
            int(row['unloaded_candidate_review_flag']) for row in output
        ),
        'unloaded_candidate_count_by_industry': dict(
            sorted(unloaded_by_industry.items())
        ),
        'unloaded_candidate_count_by_catalog_status': dict(
            sorted(unloaded_by_catalog_status.items())
        ),
        'point_in_time_taxonomy_verified': False,
        'taxonomy_review_required_count': sum(
            int(row.get('taxonomy_review_required') or 0) for row in output
        ),
        'survivorship_corrected_panel_ready': False,
        'database_write_count': 0,
        'production_or_calibration_use_allowed': False,
        'status_counts': dict(sorted(counts.items())),
    }
    return output, summary


__all__ = ['reconcile_historical_candidates_v2']

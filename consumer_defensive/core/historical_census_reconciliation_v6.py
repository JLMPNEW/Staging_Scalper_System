"""Allow verified index exit on or before, rather than only at, delisting."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .historical_census_reconciliation_v5 import (
    reconcile_historical_candidates_v5,
    reviewed_pit_override_sha256,
)


def reconcile_historical_candidates_v6(
    conn: Any,
    rows: Sequence[Mapping[str, Any]],
    *,
    reviewed_pit_overrides: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run V5 after adapting its overly strict exit/delisting equality guard."""

    originals: dict[str, dict[str, Any]] = {}
    adapted: list[dict[str, Any]] = []
    for source in reviewed_pit_overrides:
        row = dict(source)
        asset_id = str(row.get('provider_asset_id') or '')
        overlap_end = str(row.get('pit_overlap_end') or '')
        delisted = str(row.get('delisted_date') or '')
        if overlap_end > delisted:
            raise ValueError(
                'Reviewed PIT overlap cannot end after delisting: '
                f'asset={asset_id}'
            )
        originals[asset_id] = row
        compatible = dict(row)
        compatible['delisted_date'] = overlap_end
        compatible['record_sha256'] = reviewed_pit_override_sha256(compatible)
        adapted.append(compatible)
    reconciled, summary = reconcile_historical_candidates_v5(
        conn,
        rows,
        reviewed_pit_overrides=adapted,
    )
    for row in reconciled:
        asset_id = str(row.get('provider_asset_id') or '')
        original = originals.get(asset_id)
        if original is None:
            continue
        row['delisted_date'] = original['delisted_date']
        row['record_sha256'] = original['record_sha256']
        row['pit_exit_before_or_at_delisting_verified_flag'] = 1
    return reconciled, {
        **summary,
        'status': 'HISTORICAL_CENSUS_RECONCILIATION_REVIEW_ONLY_V6',
        'reviewed_override_record_sha256s': sorted(
            str(row['record_sha256']) for row in reviewed_pit_overrides
        ),
        'pit_exit_delisting_policy': 'pit_overlap_end_lte_delisted_date',
        'point_in_time_taxonomy_verified': False,
        'survivorship_corrected_panel_ready': False,
        'production_or_calibration_use_allowed': False,
    }


__all__ = ['reconcile_historical_candidates_v6']

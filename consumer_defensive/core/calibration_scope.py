"""Hash-bound reviewed ticker scope for calibration and live production.

Raw universe and source history remain intact. Derived calibration panels and
live allocation inputs apply the same reviewed exclusions before any
cross-sectional normalization so production cannot drift from calibration.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any, Mapping, Sequence

from .config import ConfigBundle, cfg_get


CALIBRATION_COHORTS = frozenset({
    'beverages',
    'consumer_staples_distribution_retail',
    'household_personal_tobacco',
    'packaged_foods_agricultural_products',
})
CALIBRATION_SCOPE_CONTRACT_KEYS = frozenset({
    'mode',
    'enforcement_stage',
    'selection_basis',
    'evidence_classification',
    'strict_oos_eligible',
    'preserve_source_history',
    'production_promotion_requires_fresh_post_scope_evidence',
    'reviewed_as_of',
    'excluded_tickers_by_cohort',
    'excluded_tickers',
    'excluded_ticker_count',
    'expected_remaining_current_ticker_count',
    'expected_remaining_current_tickers_sha256',
    'expected_remaining_current_by_cohort',
    'payload_sha256',
})


def _sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(',', ':'), ensure_ascii=True
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def calibration_scope_contract(bundle: ConfigBundle) -> dict[str, Any]:
    config = bundle.payload
    raw_excluded = cfg_get(
        config, 'calibration_scope.excluded_tickers_by_cohort'
    )
    excluded_by_cohort = {
        str(cohort).strip(): sorted(
            str(ticker).strip().upper() for ticker in tickers
        )
        for cohort, tickers in sorted(raw_excluded.items())
    }
    excluded = sorted({
        ticker for tickers in excluded_by_cohort.values()
        for ticker in tickers
    })
    raw_remaining = cfg_get(
        config, 'calibration_scope.expected_remaining_current_by_cohort'
    )
    payload: dict[str, Any] = {
        key: cfg_get(config, f'calibration_scope.{key}')
        for key in (
            'mode', 'enforcement_stage', 'selection_basis',
            'evidence_classification', 'strict_oos_eligible',
            'preserve_source_history',
            'production_promotion_requires_fresh_post_scope_evidence',
            'reviewed_as_of',
        )
    }
    payload.update({
        'excluded_tickers_by_cohort': excluded_by_cohort,
        'excluded_tickers': excluded,
        'excluded_ticker_count': len(excluded),
        'expected_remaining_current_ticker_count': int(cfg_get(
            config,
            'calibration_scope.expected_remaining_current_ticker_count',
        )),
        'expected_remaining_current_tickers_sha256': str(cfg_get(
            config,
            'calibration_scope.expected_remaining_current_tickers_sha256',
        )).strip().lower(),
        'expected_remaining_current_by_cohort': {
            str(cohort): int(count)
            for cohort, count in sorted(raw_remaining.items())
        },
    })
    payload['payload_sha256'] = _sha256(payload)
    return validate_calibration_scope_contract(payload)


def validate_calibration_scope_contract(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a serialized scope without trusting the producer config."""

    if set(payload) != CALIBRATION_SCOPE_CONTRACT_KEYS:
        raise ValueError('Calibration scope contract has the wrong schema.')
    contract = dict(payload)
    if (
        contract['mode'] != 'explicit_ticker_exclusions'
        or contract['enforcement_stage']
        != 'before_cross_section_normalization'
    ):
        raise ValueError('Calibration scope enforcement policy changed.')
    for field in (
        'strict_oos_eligible',
        'preserve_source_history',
        'production_promotion_requires_fresh_post_scope_evidence',
    ):
        if not isinstance(contract[field], bool):
            raise ValueError(f'Calibration scope {field} must be boolean.')
    excluded_by_cohort = contract['excluded_tickers_by_cohort']
    remaining_by_cohort = contract['expected_remaining_current_by_cohort']
    if (
        not isinstance(excluded_by_cohort, dict)
        or not isinstance(remaining_by_cohort, dict)
        or set(excluded_by_cohort) != CALIBRATION_COHORTS
        or set(remaining_by_cohort) != CALIBRATION_COHORTS
    ):
        raise ValueError('Calibration scope cohort census changed.')
    excluded: list[str] = []
    for cohort in sorted(CALIBRATION_COHORTS):
        values = excluded_by_cohort[cohort]
        if (
            not isinstance(values, list)
            or any(
                not isinstance(value, str)
                or not value
                or value != value.strip().upper()
                for value in values
            )
            or values != sorted(set(values))
        ):
            raise ValueError(
                f'Calibration exclusions for {cohort} are not canonical.'
            )
        excluded.extend(values)
        count = remaining_by_cohort[cohort]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(
                f'Calibration remaining count for {cohort} is invalid.'
            )
    if len(excluded) != len(set(excluded)):
        raise ValueError('Calibration exclusions overlap across cohorts.')
    excluded_count = contract['excluded_ticker_count']
    remaining_count = contract['expected_remaining_current_ticker_count']
    remaining_sha = str(
        contract['expected_remaining_current_tickers_sha256'] or ''
    )
    if (
        isinstance(excluded_count, bool)
        or not isinstance(excluded_count, int)
        or contract['excluded_tickers'] != sorted(excluded)
        or excluded_count != len(excluded)
    ):
        raise ValueError('Calibration exclusion census does not tie.')
    if (
        isinstance(remaining_count, bool)
        or not isinstance(remaining_count, int)
        or remaining_count != sum(remaining_by_cohort.values())
    ):
        raise ValueError('Calibration remaining census does not tie.')
    if (
        len(remaining_sha) != 64
        or any(char not in '0123456789abcdef' for char in remaining_sha)
    ):
        raise ValueError('Calibration remaining ticker hash is invalid.')
    body = dict(contract)
    declared_sha = str(body.pop('payload_sha256') or '')
    if declared_sha != _sha256(body):
        raise ValueError('Calibration scope contract self-hash mismatch.')
    return contract


def apply_calibration_scope(
    membership_rows: Sequence[Mapping[str, Any]],
    bundle: ConfigBundle,
    *,
    require_all_exclusions: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Remove configured tickers before they can become normalization peers."""

    contract = calibration_scope_contract(bundle)
    excluded_by_cohort = contract['excluded_tickers_by_cohort']
    excluded = set(contract['excluded_tickers'])
    observed: dict[str, set[str]] = defaultdict(set)
    for row in membership_rows:
        observed[str(row['ticker'])].add(str(row['cohort_id']))
    missing = sorted(excluded.difference(observed))
    if require_all_exclusions and missing:
        raise ValueError(
            'Configured calibration exclusions are absent from the source '
            f'membership panel: {", ".join(missing)}'
        )
    for cohort, tickers in excluded_by_cohort.items():
        for ticker in tickers:
            actual = observed.get(ticker, set())
            if actual and actual != {cohort}:
                raise ValueError(
                    f'Calibration exclusion {ticker} is configured under '
                    f'{cohort}, but source membership has {sorted(actual)}.'
                )
    filtered = [
        dict(row) for row in membership_rows
        if str(row['ticker']) not in excluded
    ]
    if not filtered:
        raise ValueError('Calibration scope removed every membership row.')

    source_by_cohort: dict[str, set[str]] = defaultdict(set)
    remaining_by_cohort: dict[str, set[str]] = defaultdict(set)
    for row in membership_rows:
        source_by_cohort[str(row['cohort_id'])].add(str(row['ticker']))
    for row in filtered:
        remaining_by_cohort[str(row['cohort_id'])].add(str(row['ticker']))
    source_tickers = set(observed)
    remaining_tickers = {str(row['ticker']) for row in filtered}
    summary: dict[str, Any] = {
        'contract': contract,
        'source_membership_row_count': len(membership_rows),
        'remaining_membership_row_count': len(filtered),
        'excluded_membership_row_count': len(membership_rows) - len(filtered),
        'source_panel_ticker_count': len(source_tickers),
        'remaining_panel_ticker_count': len(remaining_tickers),
        'observed_excluded_tickers': sorted(excluded & source_tickers),
        'source_panel_tickers_by_cohort': {
            key: len(value) for key, value in sorted(source_by_cohort.items())
        },
        'remaining_panel_tickers_by_cohort': {
            key: len(value)
            for key, value in sorted(remaining_by_cohort.items())
        },
    }
    summary['payload_sha256'] = _sha256(summary)
    return filtered, summary


def apply_current_production_scope(
    membership_rows: Sequence[Mapping[str, Any]],
    bundle: ConfigBundle,
    *,
    ticker_key: str = 'ticker',
    cohort_key: str = 'calibration_cohort_id',
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply the reviewed scope to one current live-universe census.

    This production path requires exactly one row per live ticker, observes
    every configured exclusion under its reviewed cohort, and ties the
    remaining total and cohort counts to the configuration.
    """

    contract = calibration_scope_contract(bundle)
    normalized: list[dict[str, Any]] = []
    observed: dict[str, str] = {}
    for position, raw in enumerate(membership_rows, start=1):
        row = dict(raw)
        ticker = str(row.get(ticker_key) or '').strip().upper()
        cohort = str(row.get(cohort_key) or '').strip()
        if not ticker or not cohort:
            raise ValueError(
                'Current production scope requires nonblank ticker/cohort '
                f'identities; row={position} ticker={ticker!r} cohort={cohort!r}.'
            )
        if ticker in observed:
            raise ValueError(
                f'Current production scope contains duplicate ticker {ticker}.'
            )
        observed[ticker] = cohort
        row[ticker_key] = ticker
        normalized.append(row)

    excluded_by_cohort = contract['excluded_tickers_by_cohort']
    excluded = set(contract['excluded_tickers'])
    missing = sorted(excluded.difference(observed))
    if missing:
        raise ValueError(
            'Configured production exclusions are absent from the current '
            f'live universe: {", ".join(missing)}'
        )
    wrong_cohort = sorted(
        (ticker, cohort, observed.get(ticker, ''))
        for cohort, tickers in excluded_by_cohort.items()
        for ticker in tickers
        if observed.get(ticker) != cohort
    )
    if wrong_cohort:
        raise ValueError(
            'Configured production exclusions changed cohort: '
            + ', '.join(
                f'{ticker}:{actual or "missing"}!={expected}'
                for ticker, expected, actual in wrong_cohort
            )
        )

    expected_source_count = (
        int(contract['expected_remaining_current_ticker_count'])
        + int(contract['excluded_ticker_count'])
    )
    if len(observed) != expected_source_count:
        raise ValueError(
            'Current production source-universe count drifted: '
            f'observed={len(observed)} expected={expected_source_count}.'
        )

    filtered = [
        row for row in normalized if str(row[ticker_key]) not in excluded
    ]
    remaining_by_cohort: dict[str, int] = defaultdict(int)
    for row in filtered:
        remaining_by_cohort[str(row[cohort_key])] += 1
    expected_by_cohort = {
        str(cohort): int(count)
        for cohort, count in contract[
            'expected_remaining_current_by_cohort'
        ].items()
    }
    observed_by_cohort = {
        cohort: int(remaining_by_cohort.get(cohort, 0))
        for cohort in expected_by_cohort
    }
    unexpected_cohorts = sorted(set(remaining_by_cohort) - set(expected_by_cohort))
    if unexpected_cohorts or observed_by_cohort != expected_by_cohort:
        raise ValueError(
            'Current production cohort census drifted: '
            f'observed={observed_by_cohort} expected={expected_by_cohort} '
            f'unexpected={unexpected_cohorts}.'
        )
    if len(filtered) != int(contract['expected_remaining_current_ticker_count']):
        raise ValueError(
            'Current production investable-scope count drifted: '
            f'observed={len(filtered)} '
            f'expected={contract["expected_remaining_current_ticker_count"]}.'
        )

    source_tickers = sorted(observed)
    remaining_tickers = sorted(str(row[ticker_key]) for row in filtered)
    remaining_tickers_sha256 = _sha256(remaining_tickers)
    if remaining_tickers_sha256 != str(
        contract['expected_remaining_current_tickers_sha256']
    ):
        raise ValueError(
            'Current production ticker identity drifted: '
            f'observed_sha256={remaining_tickers_sha256} '
            'expected_sha256='
            f'{contract["expected_remaining_current_tickers_sha256"]}.'
        )
    summary: dict[str, Any] = {
        'contract': contract,
        'source_ticker_count': len(source_tickers),
        'source_tickers_sha256': _sha256(source_tickers),
        'remaining_ticker_count': len(remaining_tickers),
        'remaining_tickers_sha256': remaining_tickers_sha256,
        'remaining_tickers_by_cohort': observed_by_cohort,
        'observed_excluded_tickers': sorted(excluded.intersection(observed)),
        'observed_excluded_ticker_count': len(excluded.intersection(observed)),
    }
    summary['payload_sha256'] = _sha256(summary)
    return filtered, summary


def filter_label_mapping(
    labels: Mapping[tuple[str, str], Mapping[str, Any]],
    *,
    excluded_tickers: Sequence[str],
) -> dict[tuple[str, str], dict[str, Any]]:
    excluded = set(str(ticker) for ticker in excluded_tickers)
    return {
        (str(key[0]), str(key[1])): dict(row)
        for key, row in labels.items() if str(key[1]) not in excluded
    }


__all__ = [
    'CALIBRATION_COHORTS', 'CALIBRATION_SCOPE_CONTRACT_KEYS',
    'apply_calibration_scope', 'apply_current_production_scope',
    'calibration_scope_contract',
    'filter_label_mapping',
    'validate_calibration_scope_contract',
]

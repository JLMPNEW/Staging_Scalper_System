from __future__ import annotations

import copy
import csv
import hashlib
import json
from pathlib import Path

import pytest

from consumer_defensive.core.calibration_scope import (
    apply_calibration_scope,
    calibration_scope_contract,
    filter_label_mapping,
)
from consumer_defensive.core.config import load_config, validate_config


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / 'consumer_defensive' / 'config.yaml'
BEVERAGE_EXCLUDED = {
    'BF-B', 'CCU', 'DEO', 'FIZZ', 'KDP',
    'MGPI', 'PEP', 'SAM', 'STZ', 'WEST',
}
HOUSEHOLD_EXCLUDED = {'CHD', 'CLX', 'ENR', 'KMB', 'ODD', 'WDFC'}
PACKAGED_EXCLUDED = {
    'AVO', 'CAG', 'CENTA', 'DOLE', 'FRPT', 'GIS', 'HLF', 'HSY',
    'INGR', 'JBSS', 'MDLZ', 'MKC', 'NOMD', 'SMPL', 'VITL',
}
EXPECTED_EXCLUDED = (
    BEVERAGE_EXCLUDED | HOUSEHOLD_EXCLUDED | PACKAGED_EXCLUDED
)


def test_requested_exclusions_are_hash_bound_and_pre_normalization() -> None:
    bundle = load_config(CONFIG)
    contract = calibration_scope_contract(bundle)

    assert set(contract['excluded_tickers']) == EXPECTED_EXCLUDED
    assert contract['excluded_tickers_by_cohort']['beverages'] == sorted(
        BEVERAGE_EXCLUDED
    )
    assert contract['excluded_tickers_by_cohort'][
        'household_personal_tobacco'
    ] == sorted(
        HOUSEHOLD_EXCLUDED
    )
    assert contract['excluded_tickers_by_cohort'][
        'packaged_foods_agricultural_products'
    ] == sorted(PACKAGED_EXCLUDED)
    assert contract['enforcement_stage'] == (
        'before_cross_section_normalization'
    )
    assert contract['strict_oos_eligible'] is False
    assert contract['preserve_source_history'] is True
    assert len(contract['payload_sha256']) == 64


def test_scope_removes_every_requested_ticker_and_filters_labels_identically() -> None:
    bundle = load_config(CONFIG)
    retained = {
        'ABEV': 'beverages',
        'WMT': 'consumer_staples_distribution_retail',
        'PG': 'household_personal_tobacco',
        'ADM': 'packaged_foods_agricultural_products',
    }
    membership = [
        {
            'asof_date': '2026-08-14',
            'ticker': ticker,
            'cohort_id': cohort,
        }
        for cohort, tickers in bundle.payload['calibration_scope'][
            'excluded_tickers_by_cohort'
        ].items()
        for ticker in tickers
    ] + [
        {
            'asof_date': '2026-08-14',
            'ticker': ticker,
            'cohort_id': cohort,
        }
        for ticker, cohort in retained.items()
    ]

    filtered, summary = apply_calibration_scope(membership, bundle)
    assert {row['ticker'] for row in filtered} == set(retained)
    assert summary['excluded_membership_row_count'] == 31
    assert summary['observed_excluded_tickers'] == sorted(EXPECTED_EXCLUDED)
    assert summary['remaining_panel_ticker_count'] == 4

    labels = {
        (row['asof_date'], row['ticker']): row for row in membership
    }
    scoped_labels = filter_label_mapping(
        labels, excluded_tickers=sorted(EXPECTED_EXCLUDED)
    )
    assert {ticker for _, ticker in scoped_labels} == set(retained)


def test_exclusion_cohort_mismatch_fails_closed() -> None:
    bundle = load_config(CONFIG)
    membership = []
    for cohort, tickers in bundle.payload['calibration_scope'][
        'excluded_tickers_by_cohort'
    ].items():
        membership.extend({
            'asof_date': '2026-08-14',
            'ticker': ticker,
            'cohort_id': (
                'household_personal_tobacco'
                if ticker == 'BF-B' else cohort
            ),
        } for ticker in tickers)
    membership.append({
        'asof_date': '2026-08-14',
        'ticker': 'ABEV',
        'cohort_id': 'beverages',
    })

    with pytest.raises(ValueError, match='BF-B.*configured under'):
        apply_calibration_scope(membership, bundle)


def test_current_universe_census_ties_to_governed_remaining_counts() -> None:
    bundle = load_config(CONFIG)
    with (ROOT / 'ticker_mapping' / 'consumer_defensive.csv').open(
        encoding='utf-8-sig', newline=''
    ) as handle:
        current = list(csv.DictReader(handle))
    with (
        ROOT
        / 'consumer_defensive'
        / 'data'
        / 'consumer_defensive_metric_applicability.csv'
    ).open(encoding='utf-8-sig', newline='') as handle:
        applicability = {
            row['ticker']: row['calibration_cohort_id']
            for row in csv.DictReader(handle)
        }

    remaining = [
        row for row in current if row['ticker'] not in EXPECTED_EXCLUDED
    ]
    counts: dict[str, int] = {}
    for row in remaining:
        cohort = applicability[row['ticker']]
        counts[cohort] = counts.get(cohort, 0) + 1
    expected = bundle.payload['calibration_scope']
    assert len(current) == 110
    assert len(remaining) == expected['expected_remaining_current_ticker_count']
    assert counts == expected['expected_remaining_current_by_cohort']
    remaining_sha = hashlib.sha256(
        json.dumps(
            sorted(row['ticker'] for row in remaining),
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True,
        ).encode('utf-8')
    ).hexdigest()
    assert remaining_sha == expected[
        'expected_remaining_current_tickers_sha256'
    ]


def test_scope_cannot_be_mislabeled_strict_oos() -> None:
    bundle = load_config(CONFIG)
    changed = copy.deepcopy(bundle.payload)
    changed['calibration_scope']['strict_oos_eligible'] = True

    with pytest.raises(ValueError, match='strict_oos_eligible'):
        validate_config(changed)

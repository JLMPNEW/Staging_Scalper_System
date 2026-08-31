from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from consumer_defensive.core.config import load_config
from consumer_defensive.core.stage10_publishing import (
    CONTRACT_FILE,
    MANIFEST_FILE,
    _row_hash,
    _specialized_coverage_rows,
    render_dashboard,
    stage10_policy,
    validate_stage10_policy,
    write_stage10_validation,
)


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / 'consumer_defensive' / 'config.yaml'


def test_stage10_policy_fails_closed_on_any_investable_state() -> None:
    bundle = load_config(CONFIG)
    policy = stage10_policy(bundle)
    assert policy['mode'] == 'research_only_static_publish'
    for key, value in (
        ('production_promotion_enabled', True),
        ('portfolio_write_enabled', True),
        ('portfolio_candidate_gate', 1),
        ('oos_score_valid_flag', 1),
    ):
        mutated = copy.deepcopy(policy)
        mutated[key] = value
        with pytest.raises(ValueError, match=key):
            validate_stage10_policy(mutated)


def test_stage10_row_hash_is_order_independent_and_tamper_evident() -> None:
    first = _row_hash({'ticker': 'KO', 'value': 1.0})
    second = _row_hash({'value': 1.0, 'ticker': 'KO'})
    tampered = _row_hash({'ticker': 'KO', 'value': 2.0})
    assert first['row_sha256'] == second['row_sha256']
    assert first['row_sha256'] != tampered['row_sha256']


def test_specialized_coverage_separates_measurement_and_weight_verdicts() -> None:
    ranks = [
        {
            'asof_date': '2026-08-14', 'ticker': ticker,
            'company_name': ticker, 'calibration_cohort': 'beverages',
            'applicability_subtype': 'non_alcohol',
        }
        for ticker in ('KO', 'PEP')
    ]
    scorecards = [
        {
            'asof_date': '2026-08-14', 'ticker': ticker,
            'calibration_cohort': 'beverages',
            'component_group': 'specialized',
            'metric_id': 'organic_revenue_growth_pct',
            'metric_label': 'Organic revenue growth (%)',
            'availability_status': status,
            'measurement_qualified_flag': qualified,
        }
        for ticker, status, qualified in (
            ('KO', 'measurement_only', 1),
            ('PEP', 'not_loaded', 0),
        )
    ]
    coverage, ticker_coverage = _specialized_coverage_rows(
        scorecards, ranks, factor_verdict='zero_directionally_accepted',
    )
    sector = next(row for row in coverage if row['scope_type'] == 'sector')
    assert sector['measurement_qualified_ticker_count'] == 1
    assert sector['measurement_coverage_pct'] == 50.0
    assert sector['model_weight_qualified_flag'] == 0
    assert {row['ticker'] for row in ticker_coverage} == {'KO', 'PEP'}


def test_dashboard_renders_readiness_citations_and_mobile_layout() -> None:
    payload = {
        'title': 'Test Dashboard', 'subtitle': 'Evidence monitor',
        'asof_date': '2026-08-14',
        'generation_timestamp': '2026-08-14T00:00:00Z',
        'readiness': {
            'label': 'Research-only — not investable',
            'citation_ids': ['S1'],
        },
        'first_read': {
            'what_changed': 'Frozen.', 'what_looks_attractive': 'Research only.',
            'what_can_break': 'No OOS.', 'decision': 'Wait.',
            'citation_ids': ['S1'],
        },
        'cards': [
            {
                'label': 'Names', 'value': 1, 'detail': 'shadow',
                'citation_ids': ['S1'],
            }
        ],
        'top_ranks': [], 'cohorts': [], 'specialized_sector_coverage': [],
        'stage9_baseline': [], 'risks': [], 'review_queue': [],
        'sources': [
            {
                'citation_id': 'S1', 'source_name': 'test',
                'source_status': 'accepted', 'source_sha256': 'a' * 64,
            }
        ],
        'downloads': [], 'stage10_run_id': 'cds10_test',
    }
    rendered = render_dashboard(payload)
    assert 'not investable' in rendered
    assert 'source-S1' in rendered
    assert '@media(max-width:680px)' in rendered
    assert 'No portfolio write' in rendered


def test_latest_is_updated_only_by_a_passing_validation(tmp_path: Path) -> None:
    dated = tmp_path / '2026-08-14'
    dated.mkdir()
    contract = {'asof_date': '2026-08-14'}
    (dated / CONTRACT_FILE).write_text(json.dumps(contract), encoding='utf-8')
    manifest = {'file_sha256s': {CONTRACT_FILE: 'not_used_by_sync'}}
    (dated / MANIFEST_FILE).write_text(json.dumps(manifest), encoding='utf-8')
    result = write_stage10_validation(
        dated, tmp_path, {'status': 'FAIL', 'asof_date': '2026-08-14'}
    )
    assert result['latest_status'] == 'not_updated_validation_failed'
    assert not (tmp_path / 'latest').exists()

    result = write_stage10_validation(
        dated, tmp_path, {'status': 'PASS', 'asof_date': '2026-08-14'}
    )
    assert result['latest_status'] == 'updated_after_passing_validation'
    assert (tmp_path / 'latest' / CONTRACT_FILE).is_file()

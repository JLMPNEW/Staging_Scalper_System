from __future__ import annotations

import csv
from pathlib import Path

import pytest

from consumer_defensive.core.calibration_scope import calibration_scope_contract
from consumer_defensive.core.config import load_config as load_consumer_config
from industrials.core.config import load_yaml
from portfolio_layer.scores.adapters import (
    CONSUMER_DEFENSIVE_REQUIRED_COLUMNS,
    _adapt_consumer_defensive,
    run_adapter,
)


def _config() -> dict[str, object]:
    return {
        'model_family': 'consumer_defensive',
        'sector': 'Consumer Staples',
        'industry': 'Consumer Defensive',
        'industry_aggregate': 'Consumer Staples',
        'require_oos_score_valid': True,
        'calibration': {
            'neutral': 'median',
            'scale': 50.0,
            'expected_alpha_at_full': 0.0,
        },
    }


def _row(*, promoted: bool = False) -> dict[str, str]:
    row = {field: '' for field in CONSUMER_DEFENSIVE_REQUIRED_COLUMNS}
    row.update(
        {
            'asof_date': '2026-08-25',
            'ticker': 'KO',
            'company_name': 'The Coca-Cola Company',
            'sector': 'Consumer Staples',
            'industry': 'Consumer Defensive',
            'industry_aggregate': 'Consumer Staples',
            'calibration_cohort': 'beverages',
            'final_score': '72.5',
            'final_rank': '1',
            'rank_ready_flag': '1',
            'model_status': 'complete',
            'score_confidence': '0.9',
            'score_model_version': 'consumer_defensive_test_v1',
            'model_version': 'consumer_defensive_test_v1',
            'scoring_contract_version': 'consumer_defensive_test_v1',
            'portfolio_candidate_gate': '1' if promoted else '0',
            'portfolio_candidate_score': '72.5',
            'portfolio_candidate_status': (
                'eligible' if promoted else 'not_eligible'
            ),
            'portfolio_candidate_reason': (
                'ok' if promoted else 'governance_shadow'
            ),
            'calibration_eligible_flag': '1',
            'research_calibration_input_eligible_flag': '0',
            'research_calibration_reason': 'not_survivorship_corrected',
            'calibration_sample_role': (
                'strict_oos' if promoted else 'excluded'
            ),
            'stage11_calibration_panel_source': 'dated_rank_table',
            'stage11_calibration_input_eligible_flag': '0',
            'stage11_calibration_input_reason': 'not_survivorship_corrected',
            'survivorship_corrected_panel_flag': '0',
            'oos_score_valid_flag': '1' if promoted else '0',
            'oos_score_asof_date': '2026-08-25' if promoted else '',
            'oos_invalid_reason': '' if promoted else 'governance_shadow',
            'calibration_lock_date': '2026-08-24' if promoted else '',
            'promotion_state': 'promoted' if promoted else 'shadow_monitor',
        }
    )
    return row


def test_shadow_row_is_readable_but_never_investable() -> None:
    rows = _adapt_consumer_defensive(_config(), [_row()])

    assert len(rows) == 1
    assert rows[0].investable_eligible == 0
    assert rows[0].oos_score_valid_flag == 0


def test_manifest_free_shadow_diagnostic_file_remains_readable(
    tmp_path: Path,
) -> None:
    source = tmp_path / "consumer_shadow.csv"
    row = _row()
    with source.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=tuple(row))
        writer.writeheader()
        writer.writerow(row)
    config = {
        **_config(),
        "adapter": "consumer_defensive",
        "file_mode": "flat",
        "file_path": source.name,
    }

    adapted = run_adapter(config, tmp_path, None)

    assert len(adapted.rows) == 1
    assert adapted.rows[0].ticker == "KO"
    assert adapted.rows[0].investable_eligible == 0
    assert adapted.source_files == (source.resolve(),)


def test_syntactically_promoted_row_is_rejected_without_hash_bound_decision() -> None:
    with pytest.raises(ValueError, match='cryptographically verified'):
        _adapt_consumer_defensive(_config(), [_row(promoted=True)])


@pytest.mark.parametrize('field', ['promotion_state', 'industry_aggregate'])
def test_required_governance_fields_fail_closed(field: str) -> None:
    row = _row()
    del row[field]

    with pytest.raises(ValueError, match='missing required columns'):
        _adapt_consumer_defensive(_config(), [row])


def test_shadow_cannot_assert_candidate_or_oos_gate() -> None:
    row = _row()
    row['portfolio_candidate_gate'] = '1'
    row['portfolio_candidate_status'] = 'eligible'

    with pytest.raises(ValueError, match='candidate gate'):
        _adapt_consumer_defensive(_config(), [row])

    row = _row()
    row['oos_score_valid_flag'] = '1'
    with pytest.raises(ValueError, match='OOS validity'):
        _adapt_consumer_defensive(_config(), [row])


def test_internal_sector_label_is_rejected_at_portfolio_boundary() -> None:
    row = _row()
    row['sector'] = 'Consumer Defensive'

    with pytest.raises(ValueError, match='canonical sector'):
        _adapt_consumer_defensive(_config(), [row])


def test_reviewed_excluded_ticker_is_rejected_even_when_not_investable() -> None:
    root = Path(__file__).resolve().parents[2]
    scope = calibration_scope_contract(
        load_consumer_config(root / 'consumer_defensive' / 'config.yaml')
    )
    config = _config()
    config['_consumer_defensive_calibration_scope_contract'] = scope
    row = _row()
    row['ticker'] = 'MKC'
    row['consumer_defensive_calibration_scope_sha256'] = scope['payload_sha256']

    with pytest.raises(ValueError, match='reviewed excluded tickers'):
        _adapt_consumer_defensive(config, [row])


def test_shadow_rejects_latent_nonzero_expected_alpha() -> None:
    config = _config()
    config['calibration'] = {
        'neutral': 'median',
        'scale': 50.0,
        'expected_alpha_at_full': 0.15,
    }

    with pytest.raises(ValueError, match='expected_alpha_at_full=0'):
        _adapt_consumer_defensive(config, [_row()])


@pytest.mark.parametrize(
    'field',
    [
        'research_calibration_input_eligible_flag',
        'stage11_calibration_input_eligible_flag',
        'survivorship_corrected_panel_flag',
    ],
)
def test_shadow_rejects_research_or_survivorship_lineage(field: str) -> None:
    row = _row()
    row[field] = '1'

    with pytest.raises(ValueError, match='asserts research lineage'):
        _adapt_consumer_defensive(_config(), [row])


def test_consumer_defensive_portfolio_configuration_has_standard_allocation() -> None:
    root = Path(__file__).resolve().parents[2]
    config = load_yaml(root / 'portfolio_layer' / 'config.yaml')
    source = next(
        item
        for item in config['score_contract']['sectors']
        if item['model_family'] == 'consumer_defensive'
    )
    assert source['adapter'] == 'consumer_defensive'
    assert source['enabled'] is True
    assert source['enabled_from'] == '2026-08-28'
    assert source['required'] is True
    assert config['score_contract']['min_successful_sectors'] == 8
    assert source['calibration']['expected_alpha_at_full'] == 0.0
    assert {
        cohort: calibration['expected_alpha_at_full']
        for cohort, calibration in source['calibration_by_scope'].items()
    } == pytest.approx(
        {
            'beverages': 0.002775685508133919,
            'consumer_staples_distribution_retail': 0.030029042651632126,
            'household_personal_tobacco': 0.06172254768156904,
            'packaged_foods_agricultural_products': 0.10806271277233473,
        }
    )
    assert source['optimizer_sector_cap'] == pytest.approx(0.125)
    assert set(source['optimizer_cap_by_scope'].values()) == {0.03125}
    assert source['production_activation_registry_file_path'] == (
        'consumer_defensive/framework_v3/production/2026-08-28/'
        '475fe462877c7522/consumer_defensive_activation_registry_v3.json'
    )
    assert source['production_activation_registry_sha256'] == (
        '1722d00239df6625045197f3e95752fa6911f0d80910c3cd40ecabf07073e0e1'
    )
    assert source['production_score_manifest_filename'] == (
        'consumer_defensive_production_score_manifest_v3.json'
    )
    assert source['production_terminal_manifest_file_path'] == (
        'consumer_defensive/orchestration/{yyyy-mm-dd}/'
        'consumer_defensive_production_refresh_manifest_v3.json'
    )
    assert source['production_calibration_scope_sha256'] == (
        'b9993085e910504b386484f3642db7c11e4ccc0ad82f170da19ab06981c03c68'
    )
    assert config['optimizer']['sector_weight_caps']['consumer_defensive'] == pytest.approx(0.125)
    assert config['macro']['sleeve_taxonomy']['consumer_defensive'] == {
        'macro_sector_fallback': 'Consumer Defensive',
        'industries': [
            'Beverages',
            'Consumer Staples Distribution & Retail',
            'Food Products',
            'Household Products',
            'Personal Care Products',
            'Tobacco',
        ],
        'industry_aggregates': ['Consumer Staples'],
    }
    assert set(
        config['optimizer']['scope_weight_caps']['consumer_defensive'].values()
    ) == {0.03125}
    assert (
        config['black_litterman_fusion']['strategic_sector_weights'][
            'consumer_defensive'
        ]
        == pytest.approx(0.125)
    )
    assert sum(
        config['black_litterman_fusion']['strategic_sector_weights'].values()
    ) == pytest.approx(1.0)
    assert config['transaction_costs']['aum_usd'] == 500000


def test_consumer_defensive_cannot_bypass_governance_via_generic_adapter(
    tmp_path: Path,
) -> None:
    config = _config()
    config['adapter'] = 'industrial_family'

    with pytest.raises(ValueError, match='bypass promotion governance'):
        run_adapter(config, tmp_path, None)

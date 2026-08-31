from __future__ import annotations

import copy
import csv
import hashlib
import json
from pathlib import Path

import pytest

from consumer_defensive.core.config import load_config
from consumer_defensive.core.stage10b_governance import (
    MANIFEST_FILE,
    REGISTRY_FILE,
    validate_stage10b_governance,
    validate_stage10b_policy,
    publish_stage10b_governance,
)


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / 'consumer_defensive' / 'config.yaml'


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def _contract(prefix: str, extra: dict[str, object]) -> dict[str, object]:
    value = dict(extra)
    value['contract_sha256'] = _hash(value)
    run_prefix = {'stage8': 'cds8', 'stage9': 'cds9', 'stage10': 'cds10'}[prefix]
    value[f'{prefix}_run_id'] = f"{run_prefix}_{value['contract_sha256'][:24]}"
    return value


def _manifest(root: Path, name: str, contract: dict[str, object]) -> dict[str, object]:
    value: dict[str, object] = {
        'contract_sha256': contract['contract_sha256'],
        'file_sha256s': {name: _file_hash(root / name)},
    }
    value['manifest_sha256'] = _hash(value)
    return value


def _fixture_roots(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    factor = tmp_path / 'factor'
    _write_json(factor / 'campaign' / 'consumer_defensive_factor_validation_report.json', {
        'schema_version': 'consumer_defensive_factor_validation_report_v2',
        'mode': 'shadow', 'shared_gate_active': False,
        'statistical_acceptance_only': True,
        'production_promotion_enabled': False, 'portfolio_write_enabled': False,
        'campaign_id': 'campaign', 'registry_sha256': 'registry',
    })
    stage8 = tmp_path / 'stage8'
    c8 = _contract('stage8', {'candidate_count': 320, 'decision': 'retain_stage7_core_baseline', 'factor_validation_campaign_id': 'campaign', 'factor_validation_registry_sha256': 'registry'})
    _write_json(stage8 / 'stage8_contract.json', c8)
    stage8_manifest: dict[str, object] = {'contract_sha256': c8['contract_sha256'], 'artifacts': {'stage8_contract.json': {'sha256': _file_hash(stage8 / 'stage8_contract.json'), 'bytes': (stage8 / 'stage8_contract.json').stat().st_size}}}
    stage8_manifest['manifest_sha256'] = _hash(stage8_manifest)
    _write_json(stage8 / 'stage8_artifact_manifest.json', stage8_manifest)
    stage9 = tmp_path / 'stage9'
    c9 = _contract('stage9', {'stage8_contract_sha256': c8['contract_sha256']})
    _write_json(stage9 / 'stage9_contract.json', c9)
    _write_json(stage9 / 'stage9_artifact_manifest.json', _manifest(stage9, 'stage9_contract.json', c9))
    _write_json(stage9 / 'stage9_validation.json', {'status': 'PASS'})
    stage10 = tmp_path / 'stage10'
    c10 = _contract('stage10', {
        'asof_date': '2026-08-14', 'stage9_contract_sha256': c9['contract_sha256'],
        'stage9_manifest_sha256': _read(stage9 / 'stage9_artifact_manifest.json')['manifest_sha256'],
        'production_promotion_enabled': False, 'portfolio_write_enabled': False,
        'portfolio_candidate_gate': 0, 'oos_score_valid_flag': 0, 'database_write_count': 0,
        'campaign_id': 'campaign', 'registry_sha256': 'registry',
    })
    _write_json(stage10 / 'consumer_defensive_stage10_contract.json', c10)
    with (stage10 / 'consumer_defensive_company_scorecards.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=['component_name', 'component_group', 'metric_id', 'stage7_component_weight'])
        writer.writeheader()
        writer.writerows([
            {'component_name': 'return_252d', 'component_group': 'market', 'metric_id': 'return_252d', 'stage7_component_weight': '1.0'},
            {'component_name': 'specialized:organic_revenue_growth_pct', 'component_group': 'specialized', 'metric_id': 'organic_revenue_growth_pct', 'stage7_component_weight': '0.0'},
        ])
    with (stage10 / 'consumer_defensive_final_rank_table.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=['ticker', 'oos_score_valid_flag', 'survivorship_corrected_panel_flag', 'portfolio_candidate_gate'])
        writer.writeheader()
        writer.writerow({'ticker': 'KO', 'oos_score_valid_flag': '0', 'survivorship_corrected_panel_flag': '0', 'portfolio_candidate_gate': '0'})
    files = ['consumer_defensive_stage10_contract.json', 'consumer_defensive_company_scorecards.csv', 'consumer_defensive_final_rank_table.csv']
    manifest: dict[str, object] = {'file_sha256s': {name: _file_hash(stage10 / name) for name in files}}
    manifest['manifest_sha256'] = _hash(manifest)
    _write_json(stage10 / 'consumer_defensive_dashboard_manifest.json', manifest)
    _write_json(stage10 / 'consumer_defensive_stage10_validation.json', {'status': 'PASS', 'check_count': 17, 'passed_check_count': 17})
    return stage10, stage9, stage8, factor


def _read(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding='utf-8'))


def test_stage10b_policy_rejects_any_automatic_or_production_promotion() -> None:
    policy = load_config(CONFIG)
    from consumer_defensive.core.stage10b_governance import stage10b_policy
    accepted = stage10b_policy(policy)
    for key, value in (('automatic_promotion_enabled', True), ('production_promotion_enabled', True), ('portfolio_cap', 1.0)):
        changed = copy.deepcopy(accepted)
        changed[key] = value
        with pytest.raises(ValueError, match=key):
            validate_stage10b_policy(changed)


def test_stage10b_lockbox_preserves_shadow_state_and_reports_exact_blockers(tmp_path: Path) -> None:
    stage10, stage9, stage8, factor = _fixture_roots(tmp_path)
    output = tmp_path / 'governance'
    result = publish_stage10b_governance(load_config(CONFIG), stage10_root=stage10, stage9_root=stage9, stage8_root=stage8, factor_root=factor, output_dir=output)
    assert result['promotion_state'] == 'shadow_monitor'
    assert result['portfolio_cap'] == 0.0
    assert {row['requirement'] for row in result['active_blockers']} >= {
        'strict_contemporaneous_oos', 'survivorship_corrected_evidence',
        'portfolio_gate', 'absolute_baseline_gate', 'approved_nonzero_portfolio_cap',
        'stage11_stage12_operational_acceptance', 'independent_reviewer',
        'independent_reference', 'explicit_authorization',
    }
    registry = _read(output / REGISTRY_FILE)
    assert registry['core_baseline_signal_count'] == 1
    assert registry['specialized_zero_weight_signal_count'] == 1
    assert {row['signal_class'] for row in registry['signals']} == {
        'core_baseline_locked', 'specialized_measurement_only',
    }
    validation = validate_stage10b_governance(load_config(CONFIG), stage10_root=stage10, stage9_root=stage9, stage8_root=stage8, factor_root=factor, output_dir=output)
    assert validation['status'] == 'PASS'
    assert (output / MANIFEST_FILE).is_file()


def test_stage10b_fails_closed_when_specialized_weight_is_nonzero(tmp_path: Path) -> None:
    stage10, stage9, stage8, factor = _fixture_roots(tmp_path)
    scorecard = stage10 / 'consumer_defensive_company_scorecards.csv'
    text = scorecard.read_text(encoding='utf-8').replace(',0.0\n', ',0.1\n')
    scorecard.write_text(text, encoding='utf-8')
    manifest = _read(stage10 / 'consumer_defensive_dashboard_manifest.json')
    manifest['file_sha256s']['consumer_defensive_company_scorecards.csv'] = _file_hash(scorecard)
    manifest['manifest_sha256'] = _hash({key: value for key, value in manifest.items() if key != 'manifest_sha256'})
    _write_json(stage10 / 'consumer_defensive_dashboard_manifest.json', manifest)
    with pytest.raises(RuntimeError, match='zero-weight lock'):
        publish_stage10b_governance(load_config(CONFIG), stage10_root=stage10, stage9_root=stage9, stage8_root=stage8, factor_root=factor, output_dir=tmp_path / 'out')

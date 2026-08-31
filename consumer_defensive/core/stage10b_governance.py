'''Immutable, fail-closed Stage 10B governance lockbox.

This module consumes accepted factor-validation, Stage 8, Stage 9, and Stage 10
artifacts only.  It never opens the sector database and cannot change scores,
weights, OOS flags, portfolio gates, or any production setting.
'''

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .atomic_io import atomic_text_writer
from .config import ConfigBundle, load_yaml


POLICY_FILE = 'consumer_defensive_stage10b_governance.yaml'
REGISTRY_FILE = 'consumer_defensive_stage10b_signal_registry.json'
EVIDENCE_FILE = 'consumer_defensive_stage10b_evidence_ledger.json'
LOCK_FILE = 'consumer_defensive_stage10b_governance_lock.json'
DECISION_FILE = 'consumer_defensive_stage10b_decision_record.json'
MANIFEST_FILE = 'consumer_defensive_stage10b_manifest.json'
VALIDATION_FILE = 'consumer_defensive_stage10b_validation.json'

STAGE10_CONTRACT = 'consumer_defensive_stage10_contract.json'
STAGE10_MANIFEST = 'consumer_defensive_dashboard_manifest.json'
STAGE10_VALIDATION = 'consumer_defensive_stage10_validation.json'
STAGE10_SCORECARD = 'consumer_defensive_company_scorecards.csv'
STAGE10_RANKS = 'consumer_defensive_final_rank_table.csv'

_POLICY_KEYS = {
    'mode', 'model_family', 'default_promotion_state',
    'automatic_promotion_enabled', 'production_promotion_enabled',
    'portfolio_write_enabled', 'portfolio_cap', 'required_upstream_stages',
    'promotion_requirements',
}
_REQUIREMENT_KEYS = {
    'strict_contemporaneous_oos', 'survivorship_corrected_evidence',
    'absolute_baseline_gate', 'portfolio_gate', 'approved_nonzero_portfolio_cap',
    'stage11_stage12_operational_acceptance', 'independent_reviewer',
    'independent_reference', 'explicit_authorization',
    'upstream_validations_pass',
}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'Unreadable governance artifact: {path}') from exc
    if not isinstance(value, dict):
        raise RuntimeError(f'Expected JSON object: {path}')
    return value


def _read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open('r', encoding='utf-8', newline='') as handle:
            return list(csv.DictReader(handle))
    except OSError as exc:
        raise RuntimeError(f'Unreadable governance CSV: {path}') from exc


def _json_text(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n'


def _immutable_text(path: Path, content: str) -> None:
    if path.is_symlink():
        raise RuntimeError(f'Refusing symlinked Stage 10B artifact: {path}')
    if path.exists():
        if path.read_bytes() != content.encode('utf-8'):
            raise FileExistsError(f'Immutable Stage 10B artifact changed: {path}')
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with atomic_text_writer(path, encoding='utf-8', newline='') as handle:
        handle.write(content)


def _self_hash(payload: Mapping[str, Any], field: str) -> str:
    return _sha256({key: value for key, value in payload.items() if key != field})


def validate_stage10b_policy(section: Mapping[str, Any]) -> None:
    unknown = sorted(set(section) - _POLICY_KEYS)
    missing = sorted(_POLICY_KEYS - set(section))
    if unknown or missing:
        raise ValueError(f'Stage 10B policy key mismatch: missing={missing} unknown={unknown}.')
    exact = {
        'mode': 'immutable_evidence_governance',
        'model_family': 'consumer_defensive',
        'default_promotion_state': 'shadow_monitor',
        'automatic_promotion_enabled': False,
        'production_promotion_enabled': False,
        'portfolio_write_enabled': False,
        'portfolio_cap': 0.0,
        'required_upstream_stages': ['factor_validation', 'stage8', 'stage9', 'stage10'],
    }
    for key, expected in exact.items():
        if section.get(key) != expected:
            raise ValueError(f'stage10b_governance.{key} must be {expected!r}.')
    requirements = section.get('promotion_requirements')
    if not isinstance(requirements, dict) or set(requirements) != _REQUIREMENT_KEYS:
        raise ValueError('Stage 10B promotion requirements must be complete and exact.')
    if any(value is not True for value in requirements.values()):
        raise ValueError('Every Stage 10B promotion requirement must be true.')


def stage10b_policy(bundle: ConfigBundle) -> dict[str, Any]:
    payload = load_yaml(bundle.base_dir / 'data' / POLICY_FILE)
    if set(payload) != {'schema_version', 'stage10b_governance'}:
        raise ValueError('Stage 10B policy root is invalid.')
    if payload['schema_version'] != 'consumer_defensive_stage10b_governance_policy_v1':
        raise ValueError('Unknown Stage 10B policy version.')
    section = payload['stage10b_governance']
    if not isinstance(section, dict):
        raise ValueError('Stage 10B policy must be a mapping.')
    validate_stage10b_policy(section)
    return json.loads(_canonical(section))


def _require_files(root: Path, names: Sequence[str], label: str) -> None:
    missing = [name for name in names if not (root / name).is_file()]
    if missing:
        raise RuntimeError(f'Missing {label} artifacts: {missing}')


def _verify_manifest(root: Path, manifest_name: str, *, contract_name: str) -> dict[str, Any]:
    manifest = _read_json(root / manifest_name)
    if manifest.get('manifest_sha256') != _self_hash(manifest, 'manifest_sha256'):
        raise RuntimeError(f'{manifest_name} self-hash mismatch.')
    file_hashes = manifest.get('file_sha256s')
    if not isinstance(file_hashes, dict):
        artifacts = manifest.get('artifacts')
        if not isinstance(artifacts, dict):
            raise RuntimeError(f'{manifest_name} lacks artifact hashes.')
        file_hashes = {
            str(name): str(value.get('sha256', ''))
            for name, value in artifacts.items()
            if isinstance(value, dict)
        }
        if len(file_hashes) != len(artifacts) or any(not digest for digest in file_hashes.values()):
            raise RuntimeError(f'{manifest_name} has malformed artifact hashes.')
    for name, digest in file_hashes.items():
        path = root / str(name)
        if not path.is_file() or _file_sha256(path) != str(digest):
            raise RuntimeError(f'{manifest_name} hash mismatch: {name}')
    contract = _read_json(root / contract_name)
    core = dict(contract)
    observed = str(core.pop('contract_sha256', ''))
    run_id = core.pop('stage8_run_id', core.pop('stage9_run_id', ''))
    if observed != _sha256(core):
        raise RuntimeError(f'{contract_name} self-hash mismatch.')
    if run_id and not str(run_id).endswith(observed[:24]):
        raise RuntimeError(f'{contract_name} run ID mismatch.')
    if manifest.get('contract_sha256') not in (None, observed):
        raise RuntimeError(f'{manifest_name} contract binding mismatch.')
    return {'manifest': manifest, 'contract': contract}


def _factor_report(root: Path) -> tuple[Path, dict[str, Any]]:
    reports = sorted(root.rglob('consumer_defensive_factor_validation_report.json'))
    if len(reports) != 1:
        raise RuntimeError('Expected exactly one accepted factor-validation report.')
    path = reports[0]
    report = _read_json(path)
    safety = {
        'mode': 'shadow', 'shared_gate_active': False,
        'statistical_acceptance_only': True,
        'production_promotion_enabled': False, 'portfolio_write_enabled': False,
    }
    if report.get('schema_version') != 'consumer_defensive_factor_validation_report_v2':
        raise RuntimeError('Factor-validation report version is not accepted.')
    if any(report.get(key) != value for key, value in safety.items()):
        raise RuntimeError('Factor-validation safety lock mismatch.')
    return path, report


def _upstream(
    *, stage10_root: Path, stage9_root: Path, stage8_root: Path, factor_root: Path,
) -> dict[str, Any]:
    for root in (stage10_root, stage9_root, stage8_root, factor_root):
        if root.is_symlink() or not root.is_dir():
            raise RuntimeError(f'Invalid or symlinked upstream root: {root}')
    _require_files(stage10_root, (STAGE10_CONTRACT, STAGE10_MANIFEST, STAGE10_VALIDATION, STAGE10_SCORECARD, STAGE10_RANKS), 'Stage 10')
    stage10_contract = _read_json(stage10_root / STAGE10_CONTRACT)
    contract_core = dict(stage10_contract)
    contract_hash = str(contract_core.pop('contract_sha256', ''))
    run_id = str(contract_core.pop('stage10_run_id', ''))
    if contract_hash != _sha256(contract_core) or run_id != 'cds10_' + contract_hash[:24]:
        raise RuntimeError('Stage 10 contract self-hash or run ID mismatch.')
    stage10_safety = {
        'production_promotion_enabled': False, 'portfolio_write_enabled': False,
        'portfolio_candidate_gate': 0, 'oos_score_valid_flag': 0,
        'database_write_count': 0,
    }
    if any(stage10_contract.get(key) != value for key, value in stage10_safety.items()):
        raise RuntimeError('Stage 10 report-only safety lock mismatch.')
    stage10_manifest = _read_json(stage10_root / STAGE10_MANIFEST)
    if stage10_manifest.get('manifest_sha256') != _self_hash(stage10_manifest, 'manifest_sha256'):
        raise RuntimeError('Stage 10 manifest self-hash mismatch.')
    for name, digest in dict(stage10_manifest.get('file_sha256s') or {}).items():
        if not (stage10_root / str(name)).is_file() or _file_sha256(stage10_root / str(name)) != str(digest):
            raise RuntimeError(f'Stage 10 artifact hash mismatch: {name}')
    stage10_validation = _read_json(stage10_root / STAGE10_VALIDATION)
    if stage10_validation.get('status') != 'PASS' or stage10_validation.get('passed_check_count') != stage10_validation.get('check_count'):
        raise RuntimeError('Stage 10 upstream validation is not a complete PASS.')
    stage9 = _verify_manifest(stage9_root, 'stage9_artifact_manifest.json', contract_name='stage9_contract.json')
    if _read_json(stage9_root / 'stage9_validation.json').get('status') != 'PASS':
        raise RuntimeError('Stage 9 upstream validation is not PASS.')
    stage8 = _verify_manifest(stage8_root, 'stage8_artifact_manifest.json', contract_name='stage8_contract.json')
    factor_path, factor = _factor_report(factor_root)
    if (
        stage8['contract'].get('factor_validation_campaign_id') != factor.get('campaign_id')
        or stage8['contract'].get('factor_validation_registry_sha256')
        != factor.get('registry_sha256')
    ):
        raise RuntimeError('Stage 8 / factor-validation campaign lineage mismatch.')
    if stage10_contract.get('stage9_contract_sha256') != stage9['contract'].get('contract_sha256'):
        raise RuntimeError('Stage 10 / Stage 9 contract lineage mismatch.')
    if stage10_contract.get('stage9_manifest_sha256') != stage9['manifest'].get('manifest_sha256'):
        raise RuntimeError('Stage 10 / Stage 9 manifest lineage mismatch.')
    if stage9['contract'].get('stage8_contract_sha256') != stage8['contract'].get('contract_sha256'):
        raise RuntimeError('Stage 9 / Stage 8 contract lineage mismatch.')
    return {
        'stage10': {'root': stage10_root, 'contract': stage10_contract, 'manifest': stage10_manifest, 'validation': stage10_validation},
        'stage9': {**stage9, 'root': stage9_root}, 'stage8': {**stage8, 'root': stage8_root},
        'factor': {'root': factor_root, 'path': factor_path, 'report': factor},
    }


def _signal_registry(upstream: Mapping[str, Any]) -> dict[str, Any]:
    root = upstream['stage10']['root']
    scores = _read_csv(root / STAGE10_SCORECARD)
    core = sorted({row['component_name'] for row in scores if row.get('component_group') != 'specialized'})
    specialized = sorted({row['metric_id'] for row in scores if row.get('component_group') == 'specialized'})
    if not core or not specialized:
        raise RuntimeError('Stage 10 scorecard does not contain core and specialized signals.')
    core_rows = [row for row in scores if row.get('component_group') != 'specialized']
    if any(float(row.get('stage7_component_weight') or 0.0) <= 0.0 for row in core_rows):
        raise RuntimeError('A core baseline signal lacks a nonzero frozen weight.')
    specialized_rows = [row for row in scores if row.get('component_group') == 'specialized']
    if any(float(row.get('stage7_component_weight') or 0.0) != 0.0 for row in specialized_rows):
        raise RuntimeError('A specialized signal escaped its zero-weight lock.')
    signals = [
        {'signal_id': name, 'signal_class': 'core_baseline_locked', 'weight_state': 'nonzero_frozen_stage7_baseline', 'promotion_state': 'shadow_monitor', 'evidence_ref': 'stage7_contract_via_stage10'}
        for name in core
    ] + [
        {'signal_id': name, 'signal_class': 'specialized_measurement_only', 'weight_state': 'zero_locked', 'promotion_state': 'shadow_monitor', 'evidence_ref': 'factor_validation_zero_accepted_cells'}
        for name in specialized
    ]
    candidates = {
        'candidate_count': int(upstream['stage8']['contract'].get('candidate_count', 0)),
        'decision': str(upstream['stage8']['contract'].get('decision', 'retain_stage7_core_baseline')),
        'status': 'research_candidate_rejected_or_deferred',
    }
    return {
        'schema_version': 'consumer_defensive_stage10b_signal_registry_v1',
        'stage10_contract_sha256': upstream['stage10']['contract']['contract_sha256'],
        'core_baseline_signal_count': len(core), 'specialized_zero_weight_signal_count': len(specialized),
        'research_candidates': candidates, 'signals': signals,
    }


def _promotion_blockers(upstream: Mapping[str, Any]) -> list[dict[str, str]]:
    ranks = _read_csv(upstream['stage10']['root'] / STAGE10_RANKS)
    blockers: list[dict[str, str]] = []
    if not ranks or any(row.get('oos_score_valid_flag') != '1' for row in ranks):
        blockers.append({'requirement': 'strict_contemporaneous_oos', 'reason': 'accepted Stage 10 rows have oos_score_valid_flag=0'})
    if any(row.get('survivorship_corrected_panel_flag') != '1' for row in ranks):
        blockers.append({'requirement': 'survivorship_corrected_evidence', 'reason': 'accepted Stage 10 rows have survivorship_corrected_panel_flag=0'})
    if any(row.get('portfolio_candidate_gate') != '1' for row in ranks):
        blockers.append({'requirement': 'portfolio_gate', 'reason': 'accepted Stage 10 rows have portfolio_candidate_gate=0'})
    blockers.extend([
        {'requirement': 'absolute_baseline_gate', 'reason': 'Stage 8 retained the frozen core baseline; no candidate passed final promotion evidence'},
        {'requirement': 'approved_nonzero_portfolio_cap', 'reason': 'no approved nonzero portfolio cap is present; Stage 10B cap is 0.0'},
        {'requirement': 'stage11_stage12_operational_acceptance', 'reason': 'Stage 11, Stage 12, clean-room acceptance, and production migration remain downstream'},
        {'requirement': 'independent_reviewer', 'reason': 'no independent reviewer approval is present in accepted artifacts'},
        {'requirement': 'independent_reference', 'reason': 'no independent reference decision is present in accepted artifacts'},
        {'requirement': 'explicit_authorization', 'reason': 'Stage 10B policy permanently disables automatic and production promotion'},
    ])
    return blockers


def _artifact_payloads(
    bundle: ConfigBundle, *, stage10_root: Path, stage9_root: Path,
    stage8_root: Path, factor_root: Path,
) -> dict[str, str]:
    policy = stage10b_policy(bundle)
    upstream = _upstream(stage10_root=stage10_root, stage9_root=stage9_root, stage8_root=stage8_root, factor_root=factor_root)
    registry = _signal_registry(upstream)
    blockers = _promotion_blockers(upstream)
    evidence = {
        'schema_version': 'consumer_defensive_stage10b_evidence_ledger_v1',
        'entries': [
            {'stage': 'factor_validation', 'path': str(upstream['factor']['path']), 'sha256': _file_sha256(upstream['factor']['path']), 'status': 'PASS', 'purpose': 'specialized signal evidence'},
            {'stage': 'stage8', 'path': str(stage8_root / 'stage8_artifact_manifest.json'), 'sha256': upstream['stage8']['manifest']['manifest_sha256'], 'status': 'PASS', 'purpose': 'research candidate governance'},
            {'stage': 'stage9', 'path': str(stage9_root / 'stage9_artifact_manifest.json'), 'sha256': upstream['stage9']['manifest']['manifest_sha256'], 'status': 'PASS', 'purpose': 'portfolio backtest evidence'},
            {'stage': 'stage10', 'path': str(stage10_root / STAGE10_MANIFEST), 'sha256': upstream['stage10']['manifest']['manifest_sha256'], 'status': 'PASS', 'purpose': 'frozen score and rank publication'},
        ],
    }
    decision = {
        'schema_version': 'consumer_defensive_stage10b_decision_record_v1',
        'promotion_state': 'shadow_monitor', 'qualification_gaps': blockers,
        'last_evaluated_date': upstream['stage10']['contract']['asof_date'],
        'next_review_date': None, 'oos_evidence_status': 'not_valid_contemporaneous_oos',
        'eligible_cross_section_summary': {'ticker_count': len(_read_csv(stage10_root / STAGE10_RANKS)), 'portfolio_candidate_gate': 0},
        'specialized_metric_coverage': {'signal_count': registry['specialized_zero_weight_signal_count'], 'weight_state': 'all_zero_locked'},
        'portfolio_cap': 0.0, 'promotion_eligible': False,
    }
    lock = {
        'schema_version': 'consumer_defensive_stage10b_governance_lock_v1',
        'promotion_state': 'shadow_monitor', 'automatic_promotion_enabled': False,
        'production_promotion_enabled': False, 'portfolio_write_enabled': False,
        'portfolio_cap': 0.0, 'oos_score_valid_flag_required': 1,
        'portfolio_candidate_gate_required': 1, 'promotion_requirements': policy['promotion_requirements'],
        'active_blocker_count': len(blockers), 'active_blockers': blockers,
    }
    registry['registry_sha256'] = _self_hash(registry, 'registry_sha256')
    evidence['ledger_sha256'] = _self_hash(evidence, 'ledger_sha256')
    decision['decision_sha256'] = _self_hash(decision, 'decision_sha256')
    lock['lock_sha256'] = _self_hash(lock, 'lock_sha256')
    bodies = {REGISTRY_FILE: registry, EVIDENCE_FILE: evidence, DECISION_FILE: decision, LOCK_FILE: lock}
    hashes = {name: _sha256(payload) for name, payload in bodies.items()}
    manifest = {
        'schema_version': 'consumer_defensive_stage10b_manifest_v1',
        'promotion_state': 'shadow_monitor', 'stage10_contract_sha256': upstream['stage10']['contract']['contract_sha256'],
        'file_sha256s': hashes, 'upstream_manifest_sha256s': {
            'stage8': upstream['stage8']['manifest']['manifest_sha256'], 'stage9': upstream['stage9']['manifest']['manifest_sha256'], 'stage10': upstream['stage10']['manifest']['manifest_sha256'],
        },
    }
    manifest['manifest_sha256'] = _self_hash(manifest, 'manifest_sha256')
    bodies[MANIFEST_FILE] = manifest
    return {name: _json_text(value) for name, value in bodies.items()}


def publish_stage10b_governance(
    bundle: ConfigBundle, *, stage10_root: Path, stage9_root: Path,
    stage8_root: Path, factor_root: Path, output_dir: Path,
) -> dict[str, Any]:
    output = output_dir.expanduser().resolve()
    texts = _artifact_payloads(bundle, stage10_root=stage10_root.expanduser().resolve(), stage9_root=stage9_root.expanduser().resolve(), stage8_root=stage8_root.expanduser().resolve(), factor_root=factor_root.expanduser().resolve())
    for name, content in texts.items():
        _immutable_text(output / name, content)
    lock = _read_json(output / LOCK_FILE)
    return {'status': 'PASS', 'stage': 'stage10b_governance', 'output_dir': str(output), 'promotion_state': 'shadow_monitor', 'promotion_eligible': False, 'portfolio_cap': 0.0, 'active_blockers': lock['active_blockers'], 'database_write_count': 0}


def validate_stage10b_governance(
    bundle: ConfigBundle, *, stage10_root: Path, stage9_root: Path,
    stage8_root: Path, factor_root: Path, output_dir: Path,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        expected = _artifact_payloads(bundle, stage10_root=stage10_root.expanduser().resolve(), stage9_root=stage9_root.expanduser().resolve(), stage8_root=stage8_root.expanduser().resolve(), factor_root=factor_root.expanduser().resolve())
        observed = output_dir.expanduser().resolve()
        required = set(expected)
        present = {path.name for path in observed.iterdir() if path.is_file() and path.name != VALIDATION_FILE} if observed.is_dir() else set()
        checks.append({'check': 'artifact_census_exact', 'status': 'PASS' if present == required else 'FAIL', 'missing': sorted(required - present), 'unexpected': sorted(present - required)})
        mismatch = [name for name, text in expected.items() if not (observed / name).is_file() or (observed / name).read_text(encoding='utf-8') != text]
        checks.append({'check': 'artifact_bytes_recompute_exactly', 'status': 'PASS' if not mismatch else 'FAIL', 'mismatched': mismatch})
        lock = _read_json(observed / LOCK_FILE) if (observed / LOCK_FILE).is_file() else {}
        locked = lock.get('promotion_state') == 'shadow_monitor' and lock.get('production_promotion_enabled') is False and lock.get('portfolio_cap') == 0.0 and bool(lock.get('active_blockers'))
        checks.append({'check': 'promotion_is_fail_closed', 'status': 'PASS' if locked else 'FAIL'})
        registry = _read_json(observed / REGISTRY_FILE) if (observed / REGISTRY_FILE).is_file() else {}
        classes = {row.get('signal_class') for row in registry.get('signals', [])}
        checks.append({'check': 'signal_classes_separated', 'status': 'PASS' if {'core_baseline_locked', 'specialized_measurement_only'} <= classes and registry.get('research_candidates', {}).get('status') == 'research_candidate_rejected_or_deferred' else 'FAIL'})
    except Exception as exc:
        errors.append(str(exc))
    failed = [row['check'] for row in checks if row['status'] != 'PASS']
    return {'schema_version': 'consumer_defensive_stage10b_validation_v1', 'status': 'PASS' if not errors and not failed else 'FAIL', 'checks': checks, 'errors': errors, 'failed_checks': failed, 'check_count': len(checks), 'passed_check_count': sum(row['status'] == 'PASS' for row in checks), 'promotion_state': 'shadow_monitor', 'production_promotion_enabled': False, 'portfolio_write_enabled': False, 'database_write_count': 0}


def write_stage10b_validation(output_dir: Path, payload: Mapping[str, Any]) -> None:
    content = _json_text(dict(payload))
    path = output_dir.expanduser().resolve() / VALIDATION_FILE
    if path.is_symlink():
        raise RuntimeError(f'Refusing symlinked Stage 10B validation: {path}')
    with atomic_text_writer(path, encoding='utf-8', newline='') as handle:
        handle.write(content)

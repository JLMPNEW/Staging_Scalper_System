from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    ROOT / 'consumer_defensive' / 'scripts'
    / '25_persist_consumer_defensive_validation_v6.py'
)


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        'test_consumer_defensive_validation_persistence_v6', SCRIPT
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f'Unable to load persistence script: {SCRIPT}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_failing_symmetric_turnover_is_persisted_as_blocker(
    tmp_path, monkeypatch
) -> None:
    module = _module()
    output = tmp_path / 'evidence'
    stage8 = tmp_path / 'stage8'
    stage9 = tmp_path / 'stage9'
    stage8.mkdir()
    stage9.mkdir()
    database = tmp_path / 'database.sqlite'
    database.write_bytes(b'sealed-test-database')
    config = tmp_path / 'config.yaml'
    config.write_text('test: true\n', encoding='utf-8')

    def _base_run(_args):
        output.mkdir()
        report = output / 'validation_audit_v5.json'
        plan = output / 'future_monthly_target_plan_draft.json'
        manifest = output / 'artifact_manifest.json'
        report.write_text(json.dumps({
            'calendar_independent_baseline_evidence_v5': {
                'average_top_turnover': 0.1,
                'average_bottom_turnover': 0.9,
                'average_trade_notional_turnover': 1.0,
                'candidate_quality_constraint_pass': 1,
                'cohort_concentration_cap_pass': 1,
            },
            'limited_production_gate_v5': {'blockers': []},
        }), encoding='utf-8')
        plan.write_text('{}', encoding='utf-8')
        manifest.write_text('{}', encoding='utf-8')
        return {
            'report_path': str(report),
            'future_plan_path': str(plan),
            'manifest_path': str(manifest),
        }

    monkeypatch.setattr(
        module, '_script24',
        lambda: SimpleNamespace(run=_base_run),
    )
    monkeypatch.setattr(module, 'load_config', lambda _path: object())
    monkeypatch.setattr(
        module,
        'apply_symmetric_turnover_gate_v6',
        lambda evidence, _bundle: {
            **evidence,
            'turnover_cap_pass': 0,
        },
    )
    monkeypatch.setattr(
        module,
        '_environment_lineage',
        lambda: {
            'schema_version': 'test_environment_lineage',
            'working_tree_clean_for_scoped_paths': False,
        },
    )
    monkeypatch.setattr(module, '_code_and_policy_paths', lambda: [SCRIPT])
    args = argparse.Namespace(
        stage8_root=stage8,
        stage9_root=stage9,
        database=database,
        config=config,
        output_dir=output,
    )
    result = module.run(args)
    report = json.loads(
        Path(result['report_v6_path']).read_text(encoding='utf-8')
    )
    assert report['limited_production_gate_v6'][
        'limited_production_ready_flag'
    ] == 0
    assert 'symmetric_turnover_constraint_failed' in report[
        'limited_production_gate_v6'
    ]['blockers']
    manifest = json.loads(
        Path(result['manifest_v2_path']).read_text(encoding='utf-8')
    )
    assert manifest['lineage_complete_flag'] == 1
    environment = json.loads(
        Path(result['environment_lineage_path']).read_text(encoding='utf-8')
    )
    assert environment['working_tree_clean_for_scoped_paths'] is False

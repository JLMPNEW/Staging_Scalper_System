from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from consumer_defensive.core.atomic_io import atomic_text_writer  # noqa: E402
from consumer_defensive.core.config import load_config  # noqa: E402
from consumer_defensive.core.stage8_turnover_gate_v6 import (  # noqa: E402
    apply_symmetric_turnover_gate_v6,
)


SCHEMA_VERSION = 'consumer_defensive_validation_evidence_v6'


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            'Persist full-lineage V6 Consumer Defensive validation evidence.'
        )
    )
    parser.add_argument('--stage8-root', type=Path, required=True)
    parser.add_argument('--stage9-root', type=Path, required=True)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument(
        '--config', type=Path, default=PACKAGE_ROOT / 'config.yaml'
    )
    parser.add_argument('--output-dir', type=Path, required=True)
    return parser.parse_args()


def _script24() -> ModuleType:
    path = Path(__file__).with_name(
        '24_persist_consumer_defensive_validation_v5.py'
    )
    spec = importlib.util.spec_from_file_location(
        'consumer_defensive_validation_persistence_script24', path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f'Unable to load prerequisite persistence: {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _identity(path: Path, *, role: str) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    return {
        'role': role,
        'path': str(resolved),
        'bytes': resolved.stat().st_size,
        'sha256': _file_sha256(resolved),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    content = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + '\n'
    with atomic_text_writer(path, encoding='utf-8', newline='') as handle:
        handle.write(content)


def _git(command: list[str]) -> str:
    completed = subprocess.run(
        ['git', *command],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding='utf-8',
    )
    return completed.stdout


def _environment_lineage() -> dict[str, Any]:
    distributions = sorted({
        (
            str(distribution.metadata.get('Name') or '').lower(),
            str(distribution.version),
        )
        for distribution in importlib.metadata.distributions()
        if str(distribution.metadata.get('Name') or '').strip()
    })
    package_rows = [
        {'name': name, 'version': version}
        for name, version in distributions
    ]
    status = _git([
        'status', '--porcelain=v1', '--untracked-files=all', '--',
        'consumer_defensive', 'tests/consumer_defensive',
    ])
    return {
        'schema_version': 'consumer_defensive_environment_lineage_v1',
        'python_executable': str(Path(sys.executable).resolve()),
        'python_version': sys.version,
        'platform': platform.platform(),
        'package_count': len(package_rows),
        'packages': package_rows,
        'packages_sha256': _canonical_sha256(package_rows),
        'git_head': _git(['rev-parse', 'HEAD']).strip(),
        'git_status_porcelain': status.splitlines(),
        'git_status_sha256': hashlib.sha256(
            status.encode('utf-8')
        ).hexdigest(),
        'working_tree_clean_for_scoped_paths': not bool(status.strip()),
    }


def _code_and_policy_paths() -> list[Path]:
    paths = list((PACKAGE_ROOT / 'core').glob('*.py'))
    for pattern in ('20*.py', '21*.py', '22*.py', '23*.py', '24*.py', '25*.py'):
        paths.extend((PACKAGE_ROOT / 'scripts').glob(pattern))
    paths.extend(
        path for path in (PACKAGE_ROOT / 'data').rglob('*') if path.is_file()
    )
    paths.append(PACKAGE_ROOT / 'config.yaml')
    return sorted(set(path.resolve() for path in paths))


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = args.output_dir.expanduser().resolve()
    base_result = _script24().run(args)
    report_v5_path = Path(base_result['report_path'])
    report_v5 = json.loads(report_v5_path.read_text(encoding='utf-8'))
    bundle = load_config(args.config.expanduser().resolve())
    symmetric = apply_symmetric_turnover_gate_v6(
        report_v5['calendar_independent_baseline_evidence_v5'], bundle
    )
    symmetric['schema_version'] = (
        'consumer_defensive_stage8_independent_evidence_v6'
    )
    blockers = set(report_v5['limited_production_gate_v5']['blockers'])
    if int(symmetric['turnover_cap_pass']) != 1:
        blockers.add('symmetric_turnover_constraint_failed')
    report_v6 = {
        **report_v5,
        'schema_version': SCHEMA_VERSION,
        'calendar_independent_baseline_evidence_v6': symmetric,
        'legacy_lineage_manifest_status': (
            'superseded_by_artifact_manifest_v2'
        ),
        'limited_production_gate_v6': {
            'limited_production_ready_flag': 0,
            'action': 'remain_shadow_fail_closed',
            'blockers': sorted(blockers),
            'portfolio_write_enabled': False,
            'production_promotion_enabled': False,
        },
    }
    plan_v1_path = Path(base_result['future_plan_path'])
    plan_v2 = json.loads(plan_v1_path.read_text(encoding='utf-8'))
    plan_v2.update({
        'schema_version': 'consumer_defensive_monthly_target_plan_v2_draft',
        'required_validator': (
            'consumer_defensive_stage8_monthly_preregistration_v7'
        ),
        'trusted_registration_anchor_required': True,
        'trusted_target_access_ledger_required': True,
        'target_bytes_binding_required': True,
        'registration_chronology_pass_flag': 0,
        'status': 'draft_unregistered_fail_closed',
    })
    report_v6_path = output_dir / 'validation_audit_v6.json'
    plan_v2_path = output_dir / 'future_monthly_target_plan_draft_v2.json'
    environment_path = output_dir / 'environment_lineage.json'
    _write_json(report_v6_path, report_v6)
    _write_json(plan_v2_path, plan_v2)
    environment = _environment_lineage()
    _write_json(environment_path, environment)

    external_inputs = [
        *sorted(args.stage8_root.expanduser().resolve().iterdir()),
        *sorted(args.stage9_root.expanduser().resolve().iterdir()),
        args.database.expanduser().resolve(),
    ]
    code_policy_paths = _code_and_policy_paths()
    inputs = [
        _identity(path, role='sealed_stage_input')
        for path in external_inputs if path.is_file()
    ] + [
        _identity(path, role='executable_or_policy_dependency')
        for path in code_policy_paths
    ]
    outputs = [
        _identity(report_v5_path, role='legacy_validation_report_v5'),
        _identity(plan_v1_path, role='legacy_future_plan_draft_v1'),
        _identity(
            Path(base_result['manifest_path']),
            role='superseded_partial_manifest_v1',
        ),
        _identity(report_v6_path, role='validation_report_v6'),
        _identity(plan_v2_path, role='future_plan_draft_v2'),
        _identity(environment_path, role='environment_lineage'),
    ]
    manifest: dict[str, Any] = {
        'schema_version': (
            'consumer_defensive_validation_artifact_manifest_v2'
        ),
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'output_dir': str(output_dir),
        'inputs': inputs,
        'outputs': outputs,
        'directly_executed_script': str(Path(__file__).resolve()),
        'transitive_code_policy_dependency_count': len(code_policy_paths),
        'environment_lineage_sha256': _file_sha256(environment_path),
        'lineage_complete_flag': 1,
        'legacy_evidence_classification': 'retrospective_diagnostic_only',
        'promotion_action': 'remain_shadow_fail_closed',
    }
    manifest['manifest_payload_sha256'] = _canonical_sha256(manifest)
    manifest_path = output_dir / 'artifact_manifest_v2.json'
    _write_json(manifest_path, manifest)
    return {
        'output_dir': str(output_dir),
        'report_v6_path': str(report_v6_path),
        'report_v6_sha256': _file_sha256(report_v6_path),
        'future_plan_v2_path': str(plan_v2_path),
        'future_plan_v2_sha256': _file_sha256(plan_v2_path),
        'environment_lineage_path': str(environment_path),
        'environment_lineage_sha256': _file_sha256(environment_path),
        'manifest_v2_path': str(manifest_path),
        'manifest_v2_file_sha256': _file_sha256(manifest_path),
        'manifest_v2_payload_sha256': manifest['manifest_payload_sha256'],
        'lineage_complete_flag': 1,
        'promotion_action': 'remain_shadow_fail_closed',
    }


def main() -> int:
    print(json.dumps(run(_arguments()), indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

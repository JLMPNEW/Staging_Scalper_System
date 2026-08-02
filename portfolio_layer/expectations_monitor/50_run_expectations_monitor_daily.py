#!/usr/bin/env python3
'''Run and seal the daily provider-independent expectations-monitor chain.'''

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    read_manifest,
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.expectations_monitor.monitor_common import (  # noqa: E402
    connect_monitor_db,
)


DEFAULT_CONFIG = PACKAGE_ROOT / 'config.yaml'
CAPTURE_SCRIPT = Path(__file__).with_name('43_run_provider_capture_schedule.py')
SEMANTICS_SCRIPT = Path(__file__).with_name(
    '41_validate_provider_estimate_semantics.py'
)
RECONCILE_SCRIPT = Path(__file__).with_name('42_reconcile_provider_estimates.py')
EVENT_SCRIPT = Path(__file__).with_name('48_run_provider_earnings_event_cycle.py')
PENDING_SCRIPT = Path(__file__).with_name('49_snapshot_ib_pending_orders.py')
DIAGNOSTICS_SCRIPT = Path(__file__).with_name('49a_build_provider_diagnostics.py')
MARKET_BUILD_SCRIPT = Path(__file__).with_name('51_build_monitor_ohlcv.py')
MARKET_VALIDATE_SCRIPT = Path(__file__).with_name('52_validate_monitor_ohlcv.py')
STATE_PIPELINE_SCRIPT = Path(__file__).with_name(
    '59_run_expectations_state_pipeline.py'
)
STEP_FIELDS = [
    'step',
    'cycle',
    'status',
    'return_code',
    'manifest_path',
    'manifest_sha256',
    'detail',
]
READINESS_FIELDS = ['dependency', 'status', 'detail']
EXPECTED_STATES = (
    'buy_candidate',
    'add_candidate',
    'hold',
    'watch',
    'deteriorating',
    'suspend_adds',
    'exit_review',
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--as-of', type=date.fromisoformat)
    parser.add_argument('--universe-as-of', type=date.fromisoformat)
    parser.add_argument('--tiers', nargs='+', choices=('tier0', 'tier1', 'tier2'))
    parser.add_argument('--capture-pending-orders', action='store_true')
    parser.add_argument(
        '--skip-provider-capture',
        action='store_true',
        help=(
            'Do not call estimate providers. Intended for deterministic historical report '
            'rebuilds; sealed PIT diagnostics still run from the local snapshot store.'
        ),
    )
    parser.add_argument('--skip-pending-orders', action='store_true')
    parser.add_argument('--skip-event-cycle', action='store_true')
    parser.add_argument('--skip-market-data', action='store_true')
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--selftest', action='store_true')
    return parser.parse_args()


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _broker_execution_surface_errors(root: Path) -> list[str]:
    prohibited = (
        'place' + 'Order',
        'cancel' + 'Order',
        'reqGlobal' + 'Cancel',
        'exercise' + 'Options',
    )
    errors: list[str] = []
    for path in sorted(root.glob('*.py')):
        try:
            source = path.read_text(encoding='utf-8')
        except (OSError, UnicodeError) as exc:
            errors.append(f'{path.name}:unreadable:{exc}')
            continue
        for token in prohibited:
            if token in source:
                errors.append(f'{path.name}:prohibited_ib_method:{token}')
    return errors


def _state_contract_errors(monitor_cfg: dict[str, Any]) -> list[str]:
    raw = monitor_cfg.get('state_contract', {})
    if not isinstance(raw, dict):
        return ['state_contract_not_mapping']
    allowed = raw.get('allowed_states', [])
    states = tuple(str(value).strip() for value in allowed) if isinstance(allowed, list) else ()
    errors: list[str] = []
    if states != EXPECTED_STATES:
        errors.append(f'allowed_states={states!r}; expected={EXPECTED_STATES!r}')
    if raw.get('exit_review_is_human_only') is not True:
        errors.append('exit_review_is_human_only_not_true')
    return errors


def _outputs_valid(manifest_path: Path, manifest: dict[str, Any]) -> bool:
    outputs = manifest.get('outputs_sha256')
    if not isinstance(outputs, dict) or not outputs:
        return False
    return all(
        (manifest_path.parent / str(name)).is_file()
        and sha256_file(manifest_path.parent / str(name)) == str(expected)
        for name, expected in outputs.items()
    )


def _manifest_valid(
    path: Path,
    *,
    accepted: set[str],
    identity: tuple[str, str] | None = None,
) -> bool:
    if not path.is_file():
        return False
    try:
        manifest = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return False
    if str(manifest.get('acceptance', '')) not in accepted:
        return False
    if identity is not None and str(manifest.get(identity[0], '')) != identity[1]:
        return False
    return _outputs_valid(path, manifest)


def _provider_diagnostics_result(
    manifest_path: Path,
    *,
    return_code: int,
    as_of_date: str,
) -> tuple[str, dict[str, Any], str]:
    """Classify sealed provider diagnostics without blocking provider-independent monitoring."""
    integrity_valid = _manifest_valid(
        manifest_path,
        accepted={'PASS', 'PASS_WITH_WARNINGS', 'FAIL'},
        identity=('as_of_date', as_of_date),
    )
    if not integrity_valid:
        return 'FAIL', {}, 'provider diagnostics manifest is missing, stale, or hash-invalid'
    payload = read_manifest(manifest_path)
    acceptance = str(payload.get('acceptance', ''))
    if acceptance in {'PASS', 'PASS_WITH_WARNINGS'} and return_code == 0:
        return (
            'PASS',
            payload,
            f'acceptance={acceptance}; provider coverage gates passed; '
            'economic signals remain diagnostic',
        )
    failure_reasons = payload.get('failure_reasons', [])
    diagnostic_coverage_failure = (
        acceptance == 'FAIL'
        and return_code != 0
        and failure_reasons == ['tier0_1_provider_coverage']
        and payload.get('dependency_lineage_verified') is True
        and payload.get('shadow_only') is True
        and payload.get('les_effect_authorized') is False
        and payload.get('levels_effect_authorized') is False
    )
    if diagnostic_coverage_failure:
        return (
            'DEFERRED',
            payload,
            'sealed Tier 0/1 provider coverage failure; alert required, but provider data is '
            'diagnostic-only and provider-independent monitoring continues',
        )
    return (
        'FAIL',
        payload,
        f'unsupported provider diagnostics result: acceptance={acceptance}; '
        f'return_code={return_code}; failure_reasons={failure_reasons!r}',
    )


def _latest_universe_date(conn: Any, as_of: date) -> str:
    row = conn.execute(
        'SELECT MAX(run_as_of) AS run_as_of FROM monitor_universe WHERE run_as_of<=?',
        (as_of.isoformat(),),
    ).fetchone()
    value = str(row['run_as_of'] or '') if row is not None else ''
    if not value:
        raise ValueError(f'No sealed monitor universe exists on or before {as_of}')
    return value


def _universe_valid(
    *,
    db_path: Path,
    timeout_sec: float,
    run_as_of: str,
    output_dir: Path,
) -> tuple[bool, list[dict[str, Any]]]:
    manifest_path = output_dir / 'monitor_universe_manifest.json'
    if not _manifest_valid(
        manifest_path,
        accepted={'PASS'},
        identity=('run_as_of', run_as_of),
    ):
        return False, []
    conn = connect_monitor_db(db_path, timeout_sec=timeout_sec)
    try:
        row_count = int(
            conn.execute(
                'SELECT COUNT(*) FROM monitor_universe WHERE run_as_of=?',
                (run_as_of,),
            ).fetchone()[0]
        )
        sources = [
            dict(row)
            for row in conn.execute(
                'SELECT * FROM monitor_source_artifacts WHERE run_as_of=? '
                'ORDER BY source_role',
                (run_as_of,),
            ).fetchall()
        ]
    finally:
        conn.close()
    if row_count < 1 or not sources:
        return False, sources
    for source in sources:
        artifact = Path(str(source['artifact_path']))
        source_manifest = Path(str(source['manifest_path']))
        if (
            not artifact.is_file()
            or not source_manifest.is_file()
            or sha256_file(artifact) != str(source['artifact_sha256'])
            or sha256_file(source_manifest) != str(source['manifest_sha256'])
        ):
            return False, sources
    return True, sources


def _run(command: list[str], *, dry_run: bool) -> tuple[int, str]:
    if dry_run:
        return 0, subprocess.list2cmdline(command)
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    output = '\n'.join(
        value.strip() for value in (result.stdout, result.stderr) if value.strip()
    )
    if output:
        print(output)
    return int(result.returncode), output


def _cycles_from_capture_steps(path: Path) -> list[str]:
    with path.open('r', encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle))
    by_cycle: dict[str, set[str]] = {}
    for row in rows:
        cycle = str(row.get('retrieval_cycle', '')).strip()
        provider = str(row.get('provider', '')).strip()
        if cycle and provider:
            by_cycle.setdefault(cycle, set()).add(provider)
    return sorted(
        cycle
        for cycle, providers in by_cycle.items()
        if providers == {'alpha_vantage', 'fmp'}
    )


def _path_from_output(text: str, *, key: str) -> Path | None:
    prefix = f'{key}='
    for line in reversed(text.splitlines()):
        for field in reversed(line.strip().split(';')):
            value = field.strip()
            if value.startswith(prefix):
                path_text = value[len(prefix) :].strip()
                return Path(path_text) if path_text else None
    return None


def _semantics_verified(monitor_cfg: dict[str, Any]) -> bool:
    basis = monitor_cfg.get('estimate_basis', {})
    if not isinstance(basis, dict):
        return False
    providers = basis.get('provider_semantics', {})
    if not isinstance(providers, dict):
        return False
    for provider in ('alpha_vantage', 'fmp'):
        provider_cfg = providers.get(provider, {})
        if not isinstance(provider_cfg, dict):
            return False
        for metric in ('eps', 'revenue'):
            metric_cfg = provider_cfg.get(metric, {})
            if not isinstance(metric_cfg, dict):
                return False
            if metric_cfg.get('currency_semantics_status') != 'verified':
                return False
            if metric_cfg.get('definition_semantics_status') != 'verified':
                return False
    return True


def _prior_parent_valid(path: Path, *, plan_digest: str) -> bool:
    if not path.is_file():
        return False
    try:
        manifest = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return False
    if manifest.get('acceptance') not in {'PASS', 'PASS_WITH_DEFERRED'}:
        return False
    if manifest.get('plan_digest') != plan_digest or not _outputs_valid(path, manifest):
        return False
    children = manifest.get('child_manifests', [])
    if not isinstance(children, list):
        return False
    if any(not isinstance(child, dict) for child in children):
        return False
    return all(
        Path(str(child.get('manifest_path', ''))).is_file()
        and sha256_file(Path(str(child['manifest_path'])))
        == str(child.get('manifest_sha256', ''))
        for child in children
    )


def _publish_stable_manifest(
    *,
    stable_path: Path,
    parent_path: Path,
    force: bool,
) -> None:
    """Publish the run-local pointer consumed by the portfolio orchestrator."""
    parent = read_manifest(parent_path)
    parent_sha = sha256_file(parent_path)
    payload = {
        'schema_version': 'expectations_monitor_stable_manifest_v2',
        'acceptance': str(parent.get('acceptance', '')),
        'run_as_of': str(parent.get('as_of_date', '')),
        'as_of_date': str(parent.get('as_of_date', '')),
        'universe_as_of': str(parent.get('universe_as_of', '')),
        'shadow_only': bool(parent.get('shadow_only', True)),
        'state_publication_authorized': bool(
            parent.get('state_publication_authorized', False)
        ),
        'plan_digest': str(parent.get('plan_digest', '')),
        'parent_manifest_path': str(parent_path.resolve()),
        'parent_manifest_sha256': parent_sha,
    }
    if stable_path.is_file() and not force:
        existing = read_manifest(stable_path)
        if existing == payload:
            return
        existing_acceptance = str(existing.get('acceptance', ''))
        new_acceptance = str(payload.get('acceptance', ''))
        if new_acceptance != 'FAIL' and existing_acceptance != 'FAIL':
            raise FileExistsError(
                'Refusing to replace a different passing stable monitor manifest '
                f'without --force: {stable_path}'
            )
    write_manifest(stable_path, payload)


def run_selftest() -> None:
    assert _path_from_output(
        'events=55; batches=2; output=C:\\sealed output\\event-001',
        key='output',
    ) == Path(r'C:\sealed output\event-001')
    assert _path_from_output('prior_manifest=C:\\sealed\\manifest.json', key='prior_manifest') == Path(
        r'C:\sealed\manifest.json'
    )
    assert _path_from_output('status=PASS', key='output') is None
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / 'safe.py').write_text('def read_only():\n    return True\n', encoding='utf-8')
        assert not _broker_execution_surface_errors(root)
        method = 'place' + 'Order'
        (root / 'unsafe.py').write_text(
            f'ib.{method}(contract, order)\n', encoding='utf-8'
        )
        assert _broker_execution_surface_errors(root) == [
            f'unsafe.py:prohibited_ib_method:{method}'
        ]
    assert not _semantics_verified(
        {
            'estimate_basis': {
                'provider_semantics': {
                    provider: {
                        metric: {
                            'currency_semantics_status': 'unverified',
                            'definition_semantics_status': 'verified',
                        }
                        for metric in ('eps', 'revenue')
                    }
                    for provider in ('alpha_vantage', 'fmp')
                }
            }
        }
    )
    verified = {
        'estimate_basis': {
            'provider_semantics': {
                provider: {
                    metric: {
                        'currency_semantics_status': 'verified',
                        'definition_semantics_status': 'verified',
                    }
                    for metric in ('eps', 'revenue')
                }
                for provider in ('alpha_vantage', 'fmp')
            }
        }
    }
    assert _semantics_verified(verified)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        output = root / 'output.csv'
        manifest = root / 'manifest.json'
        write_csv(output, ['status'], [{'status': 'PASS'}])
        write_manifest(
            manifest,
            {
                'acceptance': 'PASS',
                'retrieval_cycle': 'cycle',
                'outputs_sha256': {output.name: sha256_file(output)},
            },
        )
        assert _manifest_valid(
            manifest,
            accepted={'PASS'},
            identity=('retrieval_cycle', 'cycle'),
        )
        output.write_text('status\nFAIL\n', encoding='utf-8')
        assert not _manifest_valid(manifest, accepted={'PASS'})
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        output = root / 'provider_coverage_readiness.csv'
        manifest = root / 'provider_diagnostics_manifest.json'
        write_csv(output, ['status'], [{'status': 'FAIL'}])
        base_payload = {
            'acceptance': 'FAIL',
            'as_of_date': '2026-07-31',
            'shadow_only': True,
            'les_effect_authorized': False,
            'levels_effect_authorized': False,
            'failure_reasons': ['tier0_1_provider_coverage'],
            'dependency_lineage_verified': True,
            'outputs_sha256': {output.name: sha256_file(output)},
        }
        write_manifest(manifest, base_payload)
        status, _, _ = _provider_diagnostics_result(
            manifest,
            return_code=1,
            as_of_date='2026-07-31',
        )
        assert status == 'DEFERRED'
        write_manifest(
            manifest,
            {**base_payload, 'failure_reasons': ['runtime_error']},
        )
        status, _, _ = _provider_diagnostics_result(
            manifest,
            return_code=1,
            as_of_date='2026-07-31',
        )
        assert status == 'FAIL'
        write_manifest(manifest, base_payload)
        output.write_text('status\nPASS\n', encoding='utf-8')
        status, _, _ = _provider_diagnostics_result(
            manifest,
            return_code=1,
            as_of_date='2026-07-31',
        )
        assert status == 'FAIL'
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        parent = root / 'session' / 'daily_monitor_manifest.json'
        stable = root / 'run' / 'daily_monitor_manifest.json'
        write_manifest(
            parent,
            {
                'acceptance': 'PASS_WITH_DEFERRED',
                'as_of_date': '2026-07-31',
                'universe_as_of': '2026-07-31',
                'shadow_only': True,
                'state_publication_authorized': False,
                'plan_digest': 'digest',
            },
        )
        _publish_stable_manifest(
            stable_path=stable,
            parent_path=parent,
            force=False,
        )
        changed_parent = root / 'session-2' / 'daily_monitor_manifest.json'
        write_manifest(
            changed_parent,
            {
                'acceptance': 'PASS',
                'as_of_date': '2026-07-31',
                'universe_as_of': '2026-07-31',
                'shadow_only': True,
                'state_publication_authorized': False,
                'plan_digest': 'different',
            },
        )
        try:
            _publish_stable_manifest(
                stable_path=stable,
                parent_path=changed_parent,
                force=False,
            )
        except FileExistsError:
            pass
        else:
            raise AssertionError('Different PASS pointer replaced without --force')
        failed_parent = root / 'failed' / 'daily_monitor_manifest.json'
        write_manifest(
            failed_parent,
            {
                'acceptance': 'FAIL',
                'as_of_date': '2026-07-31',
                'universe_as_of': '2026-07-31',
                'shadow_only': True,
                'state_publication_authorized': False,
                'plan_digest': 'failed',
            },
        )
        _publish_stable_manifest(
            stable_path=stable,
            parent_path=failed_parent,
            force=False,
        )
        assert read_manifest(stable)['acceptance'] == 'FAIL'
        _publish_stable_manifest(
            stable_path=stable,
            parent_path=parent,
            force=False,
        )
        assert read_manifest(stable)['acceptance'] == 'PASS_WITH_DEFERRED'
        sealed = read_manifest(stable)
        assert sealed['run_as_of'] == '2026-07-31'
        assert sealed['parent_manifest_sha256'] == sha256_file(parent)
        _publish_stable_manifest(
            stable_path=stable,
            parent_path=parent,
            force=False,
        )
    print('expectations-monitor daily orchestrator selftest: PASS')


def main() -> int:
    args = parse_args()
    if args.selftest:
        run_selftest()
        return 0
    if args.as_of is None:
        raise ValueError('--as-of is required')
    if args.capture_pending_orders and args.skip_pending_orders:
        raise ValueError('Pending-order capture and skip flags are mutually exclusive')
    config_path = args.config.resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    monitor_cfg = cfg_get(config, 'expectations_monitor', {})
    if not isinstance(monitor_cfg, dict):
        raise ValueError('expectations_monitor config must be a mapping')
    ingestion_cfg = cfg_get(config, 'provider_ingestion', {})
    if not isinstance(ingestion_cfg, dict):
        raise ValueError('provider_ingestion config must be a mapping')
    independent_provider_owner = (
        ingestion_cfg.get('enabled') is True
        and ingestion_cfg.get('network_owner') == 'independent_service'
        and 'estimates' in ingestion_cfg.get('managed_capabilities', [])
    )
    skip_provider_capture = bool(args.skip_provider_capture or independent_provider_owner)
    if monitor_cfg.get('broker_execution_prohibited') is not True:
        raise ValueError('expectations_monitor.broker_execution_prohibited must be true')
    if monitor_cfg.get('enabled_in_production') is not False:
        raise ValueError('Expectations monitor must remain shadow-only')
    state_contract = monitor_cfg.get('state_contract', {})
    if not isinstance(state_contract, dict) or state_contract.get('schema_version') != 'expectations_state_v1':
        raise ValueError('expectations_state_v1 contract is required')
    retention = monitor_cfg.get('retention', {})
    if not isinstance(retention, dict):
        raise ValueError('expectations_monitor.retention must be a mapping')
    if retention.get('derived_signals_shadow_only') is not True:
        raise ValueError('Derived monitor signals must remain shadow-only')
    allowed_providers = {
        str(value) for value in retention.get('allowed_providers', [])
    }
    configured_providers = {
        str(value)
        for value in dict(monitor_cfg.get('provider_capture', {})).get(
            'providers', []
        )
    }
    if not configured_providers or not configured_providers <= allowed_providers:
        raise ValueError('Provider capture exceeds the retention allowlist')
    event_policy = monitor_cfg.get('events', {})
    if (
        not isinstance(event_policy, dict)
        or event_policy.get('rules_only') is not True
        or event_policy.get('implementation_or_policy_data_sent') is not False
    ):
        raise ValueError('Events must remain rules-only and confidential')
    execution_errors = _broker_execution_surface_errors(Path(__file__).resolve().parent)
    if execution_errors:
        raise RuntimeError(f'Broker execution surface detected: {execution_errors}')
    state_errors = _state_contract_errors(monitor_cfg)
    if state_errors:
        raise ValueError(f'Invalid expectations-monitor state contract: {state_errors}')
    daily_cfg = monitor_cfg.get('daily_orchestration', {})
    if not isinstance(daily_cfg, dict) or daily_cfg.get('policy_version') != 'expectations_monitor_daily_v1':
        raise ValueError('expectations_monitor_daily_v1 config is required')
    if daily_cfg.get('allow_shadow_with_deferred_dependencies') is not True:
        raise ValueError('Daily monitor must explicitly allow shadow deferred dependencies')
    db_path = ensure_not_prod_path(
        resolve_path(
            monitor_cfg.get('database_path', 'db/expectations_monitor.sqlite'),
            base_dir=config_path.parent,
        ),
        label='expectations monitor database',
    )
    timeout_sec = float(monitor_cfg.get('writer_lock_timeout_sec', 30.0))
    conn = connect_monitor_db(db_path, timeout_sec=timeout_sec)
    try:
        universe_as_of = (
            args.universe_as_of.isoformat()
            if args.universe_as_of is not None
            else _latest_universe_date(conn, args.as_of)
        )
    finally:
        conn.close()
    universe_dir = (
        paths.output_dir
        / 'runs'
        / universe_as_of
        / str(monitor_cfg.get('output_subdir', 'expectations_monitor'))
    )
    universe_ok, universe_sources = _universe_valid(
        db_path=db_path,
        timeout_sec=timeout_sec,
        run_as_of=universe_as_of,
        output_dir=universe_dir,
    )
    if not universe_ok:
        raise RuntimeError(
            f'Monitor universe {universe_as_of} is missing, stale, or hash-invalid; rerun script 39'
        )

    input_paths = [
        config_path,
        Path(__file__).resolve(),
        CAPTURE_SCRIPT.resolve(),
        SEMANTICS_SCRIPT.resolve(),
        RECONCILE_SCRIPT.resolve(),
        EVENT_SCRIPT.resolve(),
        PENDING_SCRIPT.resolve(),
        DIAGNOSTICS_SCRIPT.resolve(),
        MARKET_BUILD_SCRIPT.resolve(),
        MARKET_VALIDATE_SCRIPT.resolve(),
        STATE_PIPELINE_SCRIPT.resolve(),
        Path(__file__).with_name('market_data_common.py').resolve(),
        Path(__file__).with_name('state_common.py').resolve(),
        PACKAGE_ROOT / 'levels' / 'levels_common.py',
        PACKAGE_ROOT / 'levels' / '64_run_levels_daily.py',
        PACKAGE_ROOT / 'risk' / 'ohlcv_sources.py',
        PACKAGE_ROOT / 'risk' / 'yahoo.py',
        Path(__file__).with_name('monitor_common.py').resolve(),
        universe_dir / 'monitor_universe_manifest.json',
    ]
    plan_identity = {
        'policy_version': 'expectations_monitor_daily_v1',
        'as_of_date': args.as_of.isoformat(),
        'universe_as_of': universe_as_of,
        'tiers': sorted(args.tiers or ['auto']),
        'capture_pending_orders': bool(args.capture_pending_orders),
        'skip_provider_capture': skip_provider_capture,
        'provider_network_owner': (
            'independent_service' if independent_provider_owner else 'daily_monitor'
        ),
        'skip_pending_orders': bool(args.skip_pending_orders),
        'skip_event_cycle': bool(args.skip_event_cycle),
        'skip_market_data': bool(args.skip_market_data),
        'universe_sources': universe_sources,
        'inputs_sha256': {str(path): sha256_file(path) for path in input_paths},
    }
    plan_digest = _digest(plan_identity)
    root = paths.output_dir / 'expectations_monitor_daily' / args.as_of.isoformat()
    output_subdir = str(
        monitor_cfg.get('output_subdir', 'expectations_monitor')
    ).strip()
    if (
        not output_subdir
        or Path(output_subdir).is_absolute()
        or '..' in Path(output_subdir).parts
    ):
        raise ValueError('expectations_monitor.output_subdir must be a safe relative path')
    stable_manifest_path = (
        paths.output_dir
        / 'runs'
        / args.as_of.isoformat()
        / output_subdir
        / 'daily_monitor_manifest.json'
    )
    stable_failed = False
    if stable_manifest_path.is_file():
        stable_failed = read_manifest(stable_manifest_path).get('acceptance') == 'FAIL'
    if args.output_dir is None and not args.force and not stable_failed:
        for prior in sorted(root.glob('*/daily_monitor_manifest.json')):
            if _prior_parent_valid(prior, plan_digest=plan_digest):
                try:
                    _publish_stable_manifest(
                        stable_path=stable_manifest_path,
                        parent_path=prior,
                        force=False,
                    )
                except FileExistsError:
                    continue
                print('EXPECTATIONS MONITOR DAILY: SKIPPED_ALREADY_PASS')
                print(f'prior_manifest={prior}')
                return 0
    invocation = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    session = f'daily-{args.as_of.strftime("%Y%m%d")}-{plan_digest[:10]}-{invocation}'
    output_dir = args.output_dir.resolve() if args.output_dir else root / session
    steps_path = output_dir / 'daily_monitor_steps.csv'
    readiness_path = output_dir / 'daily_monitor_readiness.csv'
    plan_path = output_dir / 'daily_monitor_plan.json'
    manifest_path = output_dir / 'daily_monitor_manifest.json'
    fail_if_exists([steps_path, readiness_path, plan_path, manifest_path])
    write_manifest(plan_path, {**plan_identity, 'plan_digest': plan_digest})
    steps: list[dict[str, Any]] = []
    children: list[dict[str, str]] = []
    failed = False

    pending_cfg = monitor_cfg.get('pending_orders', {})
    if not isinstance(pending_cfg, dict):
        raise ValueError('expectations_monitor.pending_orders must be a mapping')
    pending_mode = str(pending_cfg.get('source_mode', '')).strip().casefold()
    if pending_mode not in {'static', 'live'}:
        raise ValueError('pending_orders.source_mode must be static or live')
    pending_requested = (
        not args.skip_pending_orders
        and (
            args.capture_pending_orders
            or bool(pending_cfg.get("capture_enabled_by_default", True))
        )
    )
    pending_status = 'DEFERRED'
    pending_detail = 'pending-order capture disabled or explicitly skipped'
    if pending_requested:
        pending_output = output_dir / 'pending_orders'
        command = [
            sys.executable,
            str(PENDING_SCRIPT),
            '--config',
            str(config_path),
            '--as-of',
            args.as_of.isoformat(),
            '--source-mode',
            pending_mode,
            '--output-dir',
            str(pending_output),
        ]
        return_code, detail = _run(command, dry_run=args.dry_run)
        pending_manifest = pending_output / 'ib_pending_orders_manifest.json'
        valid = args.dry_run or (
            return_code == 0
            and _manifest_valid(
                pending_manifest,
                accepted={'PASS', 'PASS_WITH_DEFERRED'},
                identity=('as_of_date', args.as_of.isoformat()),
            )
        )
        pending_status = 'DRY_RUN' if args.dry_run else 'FAIL'
        pending_detail = detail
        if valid and not args.dry_run:
            pending_payload = read_manifest(pending_manifest)
            if pending_payload.get('broker_execution_prohibited') is not True:
                valid = False
                pending_detail = 'pending-order child lacks broker-execution prohibition'
            elif pending_payload.get('pending_order_coverage') is True:
                pending_status = 'PASS'
                pending_detail = 'current pending orders captured from read-only live IB'
            else:
                pending_status = 'DEFERRED'
                pending_detail = str(
                    pending_payload.get(
                        'coverage_reason',
                        'configured broker source does not provide current pending orders',
                    )
                )
        if not valid:
            failed = True
        pending_hash = (
            sha256_file(pending_manifest)
            if pending_manifest.is_file()
            else ''
        )
        if pending_hash:
            children.append(
                {
                    'step': 'pending_orders',
                    'manifest_path': str(pending_manifest.resolve()),
                    'manifest_sha256': pending_hash,
                }
            )
        steps.append(
            {
                'step': 'pending_orders',
                'cycle': args.as_of.isoformat(),
                'status': pending_status,
                'return_code': return_code,
                'manifest_path': str(pending_manifest.resolve()),
                'manifest_sha256': pending_hash,
                'detail': detail,
            }
        )

    market_cfg = monitor_cfg.get('market_data', {})
    if not isinstance(market_cfg, dict):
        raise ValueError('expectations_monitor.market_data must be a mapping')
    market_status = 'DEFERRED'
    market_detail = 'monitor OHLCV build disabled or explicitly skipped'
    market_output: Path | None = None
    market_requested = bool(market_cfg.get('build_enabled', False)) and not args.skip_market_data
    if market_requested:
        market_dir = output_dir / 'market_data'
        market_output = market_dir
        build_command = [
            sys.executable,
            str(MARKET_BUILD_SCRIPT),
            '--config',
            str(config_path),
            '--as-of',
            args.as_of.isoformat(),
            '--universe-as-of',
            universe_as_of,
            '--output-dir',
            str(market_dir),
        ]
        build_rc, build_detail = _run(build_command, dry_run=args.dry_run)
        build_manifest = market_dir / 'monitor_ohlcv_manifest.json'
        build_valid = args.dry_run or (
            build_rc == 0
            and _manifest_valid(
                build_manifest,
                accepted={'PASS', 'PASS_WITH_WARNINGS'},
                identity=('as_of_date', args.as_of.isoformat()),
            )
        )
        build_hash = sha256_file(build_manifest) if build_manifest.is_file() else ''
        steps.append(
            {
                'step': 'monitor_ohlcv_build',
                'cycle': args.as_of.isoformat(),
                'status': 'DRY_RUN' if args.dry_run else 'PASS' if build_valid else 'FAIL',
                'return_code': build_rc,
                'manifest_path': str(build_manifest.resolve()),
                'manifest_sha256': build_hash,
                'detail': build_detail,
            }
        )
        if build_hash:
            children.append(
                {
                    'step': 'monitor_ohlcv_build',
                    'manifest_path': str(build_manifest.resolve()),
                    'manifest_sha256': build_hash,
                }
            )
        validate_valid = args.dry_run
        validate_detail = 'not run because producer build failed'
        validate_rc = 0 if args.dry_run else 1
        validation_manifest = market_dir / 'monitor_ohlcv_validation_manifest.json'
        if build_valid:
            validate_command = [
                sys.executable,
                str(MARKET_VALIDATE_SCRIPT),
                '--config',
                str(config_path),
                '--as-of',
                args.as_of.isoformat(),
                '--universe-as-of',
                universe_as_of,
                '--input-dir',
                str(market_dir),
            ]
            validate_rc, validate_detail = _run(
                validate_command,
                dry_run=args.dry_run,
            )
            validate_valid = args.dry_run or (
                validate_rc == 0
                and _manifest_valid(
                    validation_manifest,
                    accepted={'PASS', 'PASS_WITH_WARNINGS'},
                    identity=('as_of_date', args.as_of.isoformat()),
                )
            )
        validation_hash = (
            sha256_file(validation_manifest) if validation_manifest.is_file() else ''
        )
        steps.append(
            {
                'step': 'monitor_ohlcv_validation',
                'cycle': args.as_of.isoformat(),
                'status': 'DRY_RUN' if args.dry_run else 'PASS' if validate_valid else 'FAIL',
                'return_code': validate_rc,
                'manifest_path': str(validation_manifest.resolve()),
                'manifest_sha256': validation_hash,
                'detail': validate_detail,
            }
        )
        if validation_hash:
            children.append(
                {
                    'step': 'monitor_ohlcv_validation',
                    'manifest_path': str(validation_manifest.resolve()),
                    'manifest_sha256': validation_hash,
                }
            )
        market_status = (
            'DRY_RUN'
            if args.dry_run
            else 'PASS'
            if build_valid and validate_valid
            else 'FAIL'
        )
        market_detail = (
            'sealed adjusted OHLCV independently validated under Yahoo/IB/Tiingo policy'
            if market_status == 'PASS'
            else validate_detail if build_valid else build_detail
        )
        if not build_valid or not validate_valid:
            failed = True

    capture_output = output_dir / 'capture'
    capture_manifest = capture_output / 'capture_session_manifest.json'
    if skip_provider_capture:
        capture_rc = 0
        capture_detail = (
            'provider capture owned by the independent current-time service; sealed PIT '
            'diagnostics read the observation store and no observation is backdated'
        )
        capture_valid = True
    else:
        capture_command = [
            sys.executable,
            str(CAPTURE_SCRIPT),
            '--config',
            str(config_path),
            '--db',
            str(db_path),
            '--as-of',
            args.as_of.isoformat(),
            '--universe-as-of',
            universe_as_of,
            '--output-dir',
            str(capture_output),
        ]
        if args.tiers:
            capture_command.extend(['--tiers', *args.tiers])
        if args.dry_run:
            capture_command.append('--dry-run')
        capture_rc, capture_detail = _run(capture_command, dry_run=False)
        capture_valid = capture_rc == 0 and _manifest_valid(
            capture_manifest,
            accepted={'DRY_RUN'} if args.dry_run else {'PASS', 'PASS_NOOP'},
            identity=('universe_as_of', universe_as_of),
        )
    capture_hash = sha256_file(capture_manifest) if capture_manifest.is_file() else ''
    steps.append(
        {
            'step': 'provider_capture',
            'cycle': args.as_of.isoformat(),
            'status': (
                'DEFERRED'
                if skip_provider_capture
                else 'PASS'
                if capture_valid
                else 'FAIL'
            ),
            'return_code': capture_rc,
            'manifest_path': str(capture_manifest.resolve()),
            'manifest_sha256': capture_hash,
            'detail': capture_detail,
        }
    )
    if capture_hash:
        children.append(
            {
                'step': 'provider_capture',
                'manifest_path': str(capture_manifest.resolve()),
                'manifest_sha256': capture_hash,
            }
        )
    if not capture_valid:
        failed = True

    cycles: list[str] = []
    capture_steps = capture_output / 'capture_steps.csv'
    if capture_valid and capture_steps.is_file():
        cycles = _cycles_from_capture_steps(capture_steps)
    if not args.dry_run:
        for index, cycle in enumerate(cycles, start=1):
            cycle_root = output_dir / 'validation' / f'c{index:03d}'
            validations = [
                (
                    'semantic_validation',
                    SEMANTICS_SCRIPT,
                    cycle_root / 'semantics',
                    'provider_estimate_semantic_validation_manifest.json',
                    [],
                ),
                (
                    'provider_reconciliation',
                    RECONCILE_SCRIPT,
                    cycle_root / 'reconciliation',
                    'provider_estimate_reconciliation_manifest.json',
                    ['--universe-as-of', universe_as_of],
                ),
            ]
            for step, script, child_output, child_name, extra in validations:
                command = [
                    sys.executable,
                    str(script),
                    '--config',
                    str(config_path),
                    '--db',
                    str(db_path),
                    '--retrieval-cycle',
                    cycle,
                    '--as-of',
                    args.as_of.isoformat(),
                    '--output-dir',
                    str(child_output),
                    *extra,
                ]
                return_code, detail = _run(command, dry_run=False)
                child_manifest = child_output / child_name
                valid = return_code == 0 and _manifest_valid(
                    child_manifest,
                    accepted={'PASS'},
                    identity=('retrieval_cycle', cycle),
                )
                child_hash = (
                    sha256_file(child_manifest) if child_manifest.is_file() else ''
                )
                if child_hash:
                    children.append(
                        {
                            'step': step,
                            'manifest_path': str(child_manifest.resolve()),
                            'manifest_sha256': child_hash,
                        }
                    )
                steps.append(
                    {
                        'step': step,
                        'cycle': cycle,
                        'status': 'PASS' if valid else 'FAIL',
                        'return_code': return_code,
                        'manifest_path': str(child_manifest.resolve()),
                        'manifest_sha256': child_hash,
                        'detail': detail,
                    }
                )
                if not valid:
                    failed = True

    if not args.skip_event_cycle:
        event_command = [
            sys.executable,
            str(EVENT_SCRIPT),
            '--config',
            str(config_path),
            '--db',
            str(db_path),
            '--as-of',
            args.as_of.isoformat(),
            '--universe-as-of',
            universe_as_of,
        ]
        if args.dry_run:
            event_command.append('--dry-run')
        event_rc, event_detail = _run(event_command, dry_run=False)
        event_manifest = _path_from_output(event_detail, key='prior_manifest')
        if event_manifest is None:
            event_output = _path_from_output(event_detail, key='output')
            event_manifest = (
                event_output / 'provider_event_cycle_manifest.json'
                if event_output is not None
                else None
            )
        event_valid = event_rc == 0 and event_manifest is not None and _manifest_valid(
            event_manifest,
            accepted=(
                {'DRY_RUN', 'PASS', 'PASS_NO_EVENTS'}
                if args.dry_run
                else {'PASS', 'PASS_NO_EVENTS'}
            ),
            identity=('universe_as_of', universe_as_of),
        )
        event_hash = (
            sha256_file(event_manifest)
            if event_manifest is not None and event_manifest.is_file()
            else ''
        )
        if event_hash and event_manifest is not None:
            children.append(
                {
                    'step': 'earnings_event_cycle',
                    'manifest_path': str(event_manifest.resolve()),
                    'manifest_sha256': event_hash,
                }
            )
        steps.append(
            {
                'step': 'earnings_event_cycle',
                'cycle': args.as_of.isoformat(),
                'status': 'PASS' if event_valid else 'FAIL',
                'return_code': event_rc,
                'manifest_path': '' if event_manifest is None else str(event_manifest.resolve()),
                'manifest_sha256': event_hash,
                'detail': event_detail,
            }
        )
        if not event_valid:
            failed = True

    diagnostics_output = output_dir / 'provider_diagnostics'
    diagnostics_command = [
        sys.executable,
        str(DIAGNOSTICS_SCRIPT),
        '--config',
        str(config_path),
        '--db',
        str(db_path),
        '--as-of',
        args.as_of.isoformat(),
        '--universe-as-of',
        universe_as_of,
        '--output-dir',
        str(diagnostics_output),
    ]
    if args.force:
        diagnostics_command.append('--force')
    diagnostics_rc, diagnostics_detail = _run(
        diagnostics_command,
        dry_run=args.dry_run,
    )
    diagnostics_manifest = diagnostics_output / 'provider_diagnostics_manifest.json'
    if args.dry_run:
        diagnostics_status = 'DRY_RUN'
        diagnostics_payload: dict[str, Any] = {}
    else:
        diagnostics_status, diagnostics_payload, diagnostics_detail = (
            _provider_diagnostics_result(
                diagnostics_manifest,
                return_code=diagnostics_rc,
                as_of_date=args.as_of.isoformat(),
            )
        )
    diagnostics_hash = (
        sha256_file(diagnostics_manifest) if diagnostics_manifest.is_file() else ''
    )
    steps.append(
        {
            'step': 'provider_diagnostics',
            'cycle': args.as_of.isoformat(),
            'status': diagnostics_status,
            'return_code': diagnostics_rc,
            'manifest_path': str(diagnostics_manifest.resolve()),
            'manifest_sha256': diagnostics_hash,
            'detail': diagnostics_detail,
        }
    )
    if diagnostics_hash:
        children.append(
            {
                'step': 'provider_diagnostics',
                'manifest_path': str(diagnostics_manifest.resolve()),
                'manifest_sha256': diagnostics_hash,
            }
        )
    if diagnostics_status == 'FAIL':
        failed = True

    state_status = 'DEFERRED'
    state_detail = 'state/levels chain not requested or validated market data unavailable'
    run_state_chain = bool(daily_cfg.get('run_state_and_levels_chain', False))
    if run_state_chain and market_status == 'PASS' and market_output is not None:
        resolved_market_output: Path = market_output
        state_command = [
            sys.executable,
            str(STATE_PIPELINE_SCRIPT),
            '--config',
            str(config_path),
            '--as-of',
            args.as_of.isoformat(),
            '--universe-as-of',
            universe_as_of,
            '--market-data-dir',
            str(resolved_market_output),
        ]
        if args.force:
            state_command.append('--force')
        if args.dry_run:
            state_command.append('--dry-run')
        state_rc, state_detail = _run(state_command, dry_run=False)
        state_manifest_path = (
            paths.output_dir
            / 'runs'
            / args.as_of.isoformat()
            / str(monitor_cfg.get('output_subdir', 'expectations_monitor'))
            / 'expectations_pipeline_manifest.json'
        )
        state_valid = state_rc == 0 and _manifest_valid(
            state_manifest_path,
            accepted={'DRY_RUN'} if args.dry_run else {'PASS', 'PASS_WITH_DEFERRED'},
            identity=('as_of_date', args.as_of.isoformat()),
        )
        state_payload = (
            read_manifest(state_manifest_path)
            if state_valid and state_manifest_path.is_file()
            else {}
        )
        state_status = (
            'DRY_RUN'
            if args.dry_run
            else 'DEFERRED'
            if state_valid and state_payload.get('acceptance') == 'PASS_WITH_DEFERRED'
            else 'PASS'
            if state_valid
            else 'FAIL'
        )
        state_hash = sha256_file(state_manifest_path) if state_manifest_path.is_file() else ''
        steps.append(
            {
                'step': 'expectations_state_and_levels',
                'cycle': args.as_of.isoformat(),
                'status': state_status,
                'return_code': state_rc,
                'manifest_path': str(state_manifest_path.resolve()),
                'manifest_sha256': state_hash,
                'detail': state_detail,
            }
        )
        if state_hash:
            children.append(
                {
                    'step': 'expectations_state_and_levels',
                    'manifest_path': str(state_manifest_path.resolve()),
                    'manifest_sha256': state_hash,
                }
            )
        if not state_valid:
            failed = True
    elif run_state_chain and market_status == 'FAIL':
        state_status = 'FAIL'
        state_detail = 'validated OHLCV is a load-bearing prerequisite for state/levels'
        failed = True

    readiness = [
        {
            'dependency': 'broker_execution_prohibited',
            'status': 'PASS',
            'detail': 'monitor package contains no executable IB order API methods',
        },
        {
            'dependency': 'provider_semantics',
            'status': 'PASS' if _semantics_verified(monitor_cfg) else 'DEFERRED',
            'detail': (
                'all estimate currency and definition semantics verified'
                if _semantics_verified(monitor_cfg)
                else 'provider currency/definition semantics remain unverified'
            ),
        },
        {
            'dependency': 'provider_diagnostics',
            'status': diagnostics_status,
            'detail': diagnostics_detail,
        },
        {
            'dependency': 'pending_orders',
            'status': pending_status,
            'detail': pending_detail,
        },
        {
            'dependency': 'adjusted_ohlcv',
            'status': market_status,
            'detail': market_detail,
        },
        {
            'dependency': 'expectations_state_and_levels',
            'status': state_status,
            'detail': state_detail,
        },
    ]
    state_publication_authorized = (
        bool(monitor_cfg.get('state_publication_enabled', False))
        and all(row['status'] == 'PASS' for row in readiness)
        and not failed
    )
    write_csv(steps_path, STEP_FIELDS, steps)
    write_csv(readiness_path, READINESS_FIELDS, readiness)
    if args.dry_run:
        acceptance = 'DRY_RUN'
    elif failed:
        acceptance = 'FAIL'
    elif all(row['status'] == 'PASS' for row in readiness):
        acceptance = 'PASS'
    else:
        acceptance = 'PASS_WITH_DEFERRED'
    write_manifest(
        manifest_path,
        {
            'schema_version': 'expectations_monitor_daily_manifest_v2',
            'acceptance': acceptance,
            'as_of_date': args.as_of.isoformat(),
            'universe_as_of': universe_as_of,
            'plan_digest': plan_digest,
            'shadow_only': True,
            'state_publication_authorized': state_publication_authorized,
            'broker_execution_prohibited': True,
            'provider_request_fields': ['endpoint', 'ticker', 'authentication'],
            'implementation_or_policy_data_sent': False,
            'capture_cycle_count': len(cycles),
            'child_manifests': children,
            'readiness': readiness,
            'inputs_sha256': plan_identity['inputs_sha256'],
            'outputs_sha256': {
                steps_path.name: sha256_file(steps_path),
                readiness_path.name: sha256_file(readiness_path),
                plan_path.name: sha256_file(plan_path),
            },
        },
    )
    if acceptance in {"PASS", "PASS_WITH_DEFERRED", "FAIL"}:
        try:
            _publish_stable_manifest(
                stable_path=stable_manifest_path,
                parent_path=manifest_path,
                force=args.force,
            )
        except FileExistsError as exc:
            print(f'EXPECTATIONS MONITOR DAILY: FAIL_STABLE_CONFLICT: {exc}')
            return 1
    print(f'EXPECTATIONS MONITOR DAILY: {acceptance}')
    print(
        f'cycles={len(cycles)}; state_publication_authorized='
        f'{state_publication_authorized}; '
        f'output={output_dir}'
    )
    return 1 if acceptance == 'FAIL' else 0


if __name__ == '__main__':
    raise SystemExit(main())

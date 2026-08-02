#!/usr/bin/env python3
'''Run resumable tiered estimate capture without weakening per-call symbol caps.'''

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
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.paths import ensure_not_prod_path  # noqa: E402
from portfolio_layer.expectations_monitor.monitor_common import (  # noqa: E402
    connect_monitor_db,
)
from portfolio_layer.expectations_monitor.provider_common import (  # noqa: E402
    load_entitlements,
)


DEFAULT_CONFIG = PACKAGE_ROOT / 'config.yaml'
DEFAULT_ENTITLEMENTS = Path(__file__).with_name('provider_entitlements.yaml')
SNAPSHOT_SCRIPT = Path(__file__).with_name('40_snapshot_provider_estimates.py')
STEP_FIELDS = [
    'group',
    'batch_number',
    'symbol_count',
    'symbols_sha256',
    'retrieval_cycle',
    'provider',
    'status',
    'return_code',
    'detail',
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--entitlements', type=Path, default=DEFAULT_ENTITLEMENTS)
    parser.add_argument('--db', type=Path)
    parser.add_argument('--as-of', type=date.fromisoformat)
    parser.add_argument('--universe-as-of', type=date.fromisoformat)
    parser.add_argument('--tiers', nargs='+', choices=('tier0', 'tier1', 'tier2'))
    parser.add_argument('--event-symbols-file', type=Path)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--selftest', action='store_true')
    return parser.parse_args()


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _latest_universe_date(conn: Any, as_of: date) -> str:
    row = conn.execute(
        'SELECT MAX(run_as_of) AS run_as_of FROM monitor_universe WHERE run_as_of<=?',
        (as_of.isoformat(),),
    ).fetchone()
    value = str(row['run_as_of'] or '') if row is not None else ''
    if not value:
        raise ValueError(f'No monitor universe exists on or before {as_of}')
    return value


def _event_symbols(path: Path | None) -> list[str]:
    if path is None:
        return []
    with path.resolve().open('r', encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        fields = {str(value).casefold(): str(value) for value in (reader.fieldnames or [])}
        key = fields.get('ticker') or fields.get('symbol')
        if key is None:
            raise ValueError('Event-symbol file requires ticker or symbol')
        return sorted(
            {
                str(row.get(key, '')).strip().upper()
                for row in reader
                if str(row.get(key, '')).strip()
            }
        )


def _scheduled_tiers(as_of: date, requested: list[str] | None, weekly_weekday: int) -> list[str]:
    if requested:
        return [tier for tier in ('tier0', 'tier1', 'tier2') if tier in requested]
    if as_of.weekday() >= 5:
        return []
    tiers = ['tier0', 'tier1']
    if as_of.weekday() == weekly_weekday:
        tiers.append('tier2')
    return tiers


def build_batches(
    universe_rows: list[dict[str, Any]],
    *,
    tiers: list[str],
    event_symbols: list[str],
    batch_size: int,
) -> list[dict[str, Any]]:
    if batch_size < 1:
        raise ValueError('batch_size must be positive')
    by_ticker = {str(row['ticker']): row for row in universe_rows}
    unknown_events = sorted(set(event_symbols) - set(by_ticker))
    if unknown_events:
        raise ValueError(f'Event symbols are outside the sealed universe: {unknown_events}')
    groups: list[tuple[str, list[str]]] = []
    event_set = set(event_symbols)
    if event_symbols:
        groups.append(('event', event_symbols))
    for tier in tiers:
        symbols = sorted(
            str(row['ticker'])
            for row in universe_rows
            if row['tier'] == tier and str(row['ticker']) not in event_set
        )
        groups.append((tier, symbols))
    batches: list[dict[str, Any]] = []
    for group, symbols in groups:
        for start in range(0, len(symbols), batch_size):
            batch_symbols = symbols[start : start + batch_size]
            batches.append(
                {
                    'group': group,
                    'batch_number': start // batch_size + 1,
                    'symbols': batch_symbols,
                    'symbols_sha256': _digest(batch_symbols),
                }
            )
    return batches


def run_selftest() -> None:
    rows = [
        {'ticker': 'AAA', 'tier': 'tier0'},
        {'ticker': 'BBB', 'tier': 'tier1'},
        {'ticker': 'CCC', 'tier': 'tier1'},
        {'ticker': 'DDD', 'tier': 'tier2'},
    ]
    batches = build_batches(
        rows,
        tiers=['tier0', 'tier1'],
        event_symbols=['CCC'],
        batch_size=1,
    )
    assert [(row['group'], row['symbols']) for row in batches] == [
        ('event', ['CCC']),
        ('tier0', ['AAA']),
        ('tier1', ['BBB']),
    ]
    assert _scheduled_tiers(date(2026, 7, 31), None, 4) == [
        'tier0',
        'tier1',
        'tier2',
    ]
    assert _scheduled_tiers(date(2026, 8, 1), None, 4) == []
    with tempfile.TemporaryDirectory() as tmp:
        output = _provider_output_dir(
            Path(tmp),
            group='tier0',
            batch_number=1,
            provider='alpha_vantage',
            cycle='capture-test-tier0-b001-a02',
        )
        report = output / 'provider_snapshot_results.csv'
        manifest = output / 'provider_snapshot_manifest.json'
        write_csv(
            report,
            ['provider', 'status'],
            [{'provider': 'alpha_vantage', 'status': 'AVAILABLE'}],
        )
        write_manifest(
            manifest,
            {
                'acceptance': 'PASS',
                'retrieval_cycle': 'capture-test-tier0-b001-a02',
                'providers': ['alpha_vantage'],
                'outputs_sha256': {report.name: sha256_file(report)},
            },
        )
        assert _provider_output_complete(
            output,
            provider='alpha_vantage',
            cycle='capture-test-tier0-b001-a02',
        )
        report.write_text('status\nFAIL\n', encoding='utf-8')
        assert not _provider_output_complete(
            output,
            provider='alpha_vantage',
            cycle='capture-test-tier0-b001-a02',
        )
        conn = connect_monitor_db(Path(tmp) / 'monitor.sqlite', timeout_sec=1.0)
        try:
            cycle_a01 = 'capture-test-tier0-b001-a01'
            with conn:
                for provider in ('alpha_vantage', 'fmp'):
                    conn.execute(
                        'INSERT INTO provider_snapshot_runs('
                        'snapshot_run_id,provider,endpoint_id,retrieval_cycle,'
                        'started_at_utc,completed_at_utc,status,'
                        'entitlement_sha256,source_sha256) '
                        'VALUES (?,?,?,?,?,?,\'PASS\',?,?)',
                        (
                            f'run-{provider}',
                            provider,
                            'test',
                            cycle_a01,
                            '2026-07-31T20:00:00+00:00',
                            '2026-07-31T20:01:00+00:00',
                            'a' * 64,
                            'b' * 64,
                        ),
                    )
            next_cycle, complete = _resolve_attempt(
                conn,
                cycle_prefix='capture-test-tier0-b001',
                providers=['alpha_vantage', 'fmp'],
                max_attempts=2,
                provider_run_root=Path(tmp) / 'runs',
                group='tier0',
                batch_number=1,
            )
            assert not complete and next_cycle.endswith('a02')
            assert set(_cycle_statuses(conn, cycle_a01).values()) == {
                'FAIL_OUTPUT_INVALID'
            }

            coverage_cycle = 'capture-test-tier1-b001-a01'
            coverage_output = _provider_output_dir(
                Path(tmp) / 'coverage-runs',
                group='tier1',
                batch_number=1,
                provider='fmp',
                cycle=coverage_cycle,
            )
            coverage_report = coverage_output / 'provider_snapshot_results.csv'
            coverage_manifest = coverage_output / 'provider_snapshot_manifest.json'
            write_csv(
                coverage_report,
                ['provider', 'status'],
                [{'provider': 'fmp', 'status': 'EMPTY'}],
            )
            write_manifest(
                coverage_manifest,
                {
                    'acceptance': 'PASS',
                    'retrieval_cycle': coverage_cycle,
                    'providers': ['fmp'],
                    'outputs_sha256': {
                        coverage_report.name: sha256_file(coverage_report)
                    },
                },
            )
            with conn:
                conn.execute(
                    'INSERT INTO provider_snapshot_runs('
                    'snapshot_run_id,provider,endpoint_id,retrieval_cycle,'
                    'started_at_utc,completed_at_utc,status,missing_count,error_count,'
                    'entitlement_sha256,source_sha256) '
                    'VALUES (?,?,?,?,?,?,\'FAIL\',1,0,?,?)',
                    (
                        'coverage-fmp',
                        'fmp',
                        'test',
                        coverage_cycle,
                        '2026-07-31T20:00:00+00:00',
                        '2026-07-31T20:01:00+00:00',
                        'a' * 64,
                        'b' * 64,
                    ),
                )
            resolved, complete = _resolve_attempt(
                conn,
                cycle_prefix='capture-test-tier1-b001',
                providers=['fmp'],
                max_attempts=2,
                provider_run_root=Path(tmp) / 'coverage-runs',
                group='tier1',
                batch_number=1,
            )
            assert complete and resolved == coverage_cycle
            assert _cycle_statuses(conn, coverage_cycle) == {'fmp': 'PASS'}
        finally:
            conn.close()
    print('provider capture scheduler selftest: PASS')


def _cycle_statuses(conn: Any, cycle: str) -> dict[str, str]:
    rows = conn.execute(
        'SELECT provider,status FROM provider_snapshot_runs WHERE retrieval_cycle=?',
        (cycle,),
    ).fetchall()
    return {str(row['provider']): str(row['status']) for row in rows}


def _resolve_attempt(
    conn: Any,
    *,
    cycle_prefix: str,
    providers: list[str],
    max_attempts: int,
    provider_run_root: Path,
    group: str,
    batch_number: int,
) -> tuple[str, bool]:
    for attempt in range(1, max_attempts + 1):
        cycle = f'{cycle_prefix}-a{attempt:02d}'
        statuses = _cycle_statuses(conn, cycle)
        if statuses:
            outputs_pass = all(
                _provider_output_complete(
                    _provider_output_dir(
                        provider_run_root,
                        group=group,
                        batch_number=batch_number,
                        provider=provider,
                        cycle=cycle,
                    ),
                    provider=provider,
                    cycle=cycle,
                )
                for provider in providers
            )
            if outputs_pass and all(provider in statuses for provider in providers):
                with conn:
                    conn.execute(
                        'UPDATE provider_snapshot_runs '
                        'SET status=\'PASS\', '
                        'message=COALESCE(message,\'\') || '
                        '\'; scheduler revalidated: EMPTY is a coverage gap\' '
                        'WHERE retrieval_cycle=? AND status=\'FAIL\' AND error_count=0',
                        (cycle,),
                    )
                statuses = _cycle_statuses(conn, cycle)
            if (
                all(statuses.get(provider) == 'PASS' for provider in providers)
                and outputs_pass
            ):
                return cycle, True
            if all(statuses.get(provider) == 'PASS' for provider in providers):
                with conn:
                    conn.execute(
                        'UPDATE provider_snapshot_runs '
                        'SET status=\'FAIL_OUTPUT_INVALID\', '
                        'message=\'scheduler invalidated: child output missing or hash mismatch\' '
                        'WHERE retrieval_cycle=? AND status=\'PASS\'',
                        (cycle,),
                    )
            continue
        if not statuses:
            return cycle, False
    raise RuntimeError(f'Capture attempts exhausted for {cycle_prefix}')


def _provider_output_dir(
    root: Path,
    *,
    group: str,
    batch_number: int,
    provider: str,
    cycle: str,
) -> Path:
    provider_slug = {'alpha_vantage': 'av', 'fmp': 'fm'}[provider]
    attempt_slug = cycle.rsplit('-', 1)[-1]
    group_slug = {'event': 'ev', 'tier0': 't0', 'tier1': 't1', 'tier2': 't2'}[group]
    return root / f'{group_slug}{batch_number:03d}-{provider_slug}-{attempt_slug}'


def _provider_output_complete(
    output_dir: Path, *, provider: str, cycle: str
) -> bool:
    manifest_path = output_dir / 'provider_snapshot_manifest.json'
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return False
    if manifest.get('acceptance') != 'PASS':
        return False
    if manifest.get('retrieval_cycle') != cycle:
        return False
    if manifest.get('providers') != [provider]:
        return False
    outputs = manifest.get('outputs_sha256')
    if not isinstance(outputs, dict) or not outputs:
        return False
    for name, expected in outputs.items():
        path = output_dir / str(name)
        if not path.is_file() or sha256_file(path) != str(expected):
            return False
    report_path = output_dir / 'provider_snapshot_results.csv'
    try:
        with report_path.open('r', encoding='utf-8-sig', newline='') as handle:
            report_rows = list(csv.DictReader(handle))
    except OSError:
        return False
    if not report_rows:
        return False
    if any(str(row.get('provider', '')) != provider for row in report_rows):
        return False
    if any(
        str(row.get('status', '')) not in {'AVAILABLE', 'EMPTY'}
        for row in report_rows
    ):
        return False
    return True


def _snapshot_command(
    *,
    config_path: Path,
    entitlements_path: Path,
    db_path: Path,
    provider: str,
    symbols_path: Path,
    as_of: date,
    cycle: str,
    output_dir: Path,
) -> list[str]:
    return [
        sys.executable,
        str(SNAPSHOT_SCRIPT),
        '--config',
        str(config_path),
        '--entitlements',
        str(entitlements_path),
        '--db',
        str(db_path),
        '--provider',
        provider,
        '--symbols-file',
        str(symbols_path),
        '--as-of',
        as_of.isoformat(),
        '--retrieval-cycle',
        cycle,
        '--output-dir',
        str(output_dir),
    ]


def _command_text(command: list[str]) -> str:
    return subprocess.list2cmdline(command)


def main() -> int:
    args = parse_args()
    if args.selftest:
        run_selftest()
        return 0
    if args.as_of is None:
        raise ValueError('--as-of is required')

    config_path = args.config.resolve()
    entitlements_path = args.entitlements.resolve()
    config = load_yaml(config_path)
    monitor_cfg = cfg_get(config, 'expectations_monitor', {})
    if not isinstance(monitor_cfg, dict):
        raise ValueError('expectations_monitor config must be a mapping')
    capture_cfg = monitor_cfg.get('provider_capture', {})
    if not isinstance(capture_cfg, dict):
        raise ValueError('expectations_monitor.provider_capture must be a mapping')
    if capture_cfg.get('policy_version') != 'provider_capture_v1':
        raise ValueError('provider_capture_v1 config is required')
    providers = [str(value) for value in capture_cfg.get('providers', [])]
    if len(providers) != 2 or set(providers) != {'alpha_vantage', 'fmp'}:
        raise ValueError('Capture providers must contain Alpha and FMP exactly once')
    expected_cadences = {
        'tier0_cadence': 'daily',
        'tier1_cadence': 'daily',
        'tier2_cadence': 'weekly',
    }
    for field, expected in expected_cadences.items():
        if capture_cfg.get(field) != expected:
            raise ValueError(f'{field} must be {expected!r}')
    event_refresh_enabled = capture_cfg.get('event_refresh_enabled') is True
    if args.event_symbols_file is not None and not event_refresh_enabled:
        raise RuntimeError('Event refresh is disabled by provider_capture config')
    entitlements = load_entitlements(entitlements_path)
    cap_values = entitlements.get('probe', {}).get('max_symbols_by_provider', {})
    configured_batch_size = int(capture_cfg.get('batch_size', 50))
    provider_cap = min(int(cap_values.get(provider, 0)) for provider in providers)
    batch_size = min(configured_batch_size, provider_cap)
    if batch_size < 1:
        raise ValueError('No positive provider batch cap is configured')
    max_attempts = int(capture_cfg.get('max_attempts_per_batch', 3))
    if max_attempts < 1:
        raise ValueError('max_attempts_per_batch must be positive')
    weekly_weekday = int(capture_cfg.get('tier2_weekly_weekday', 4))
    if weekly_weekday not in range(5):
        raise ValueError('tier2_weekly_weekday must be Monday=0 through Friday=4')

    db_path = ensure_not_prod_path(
        args.db.resolve()
        if args.db
        else resolve_path(
            monitor_cfg.get('database_path', 'db/expectations_monitor.sqlite'),
            base_dir=config_path.parent,
        ),
        label='expectations monitor database',
    )
    timeout = float(monitor_cfg.get('writer_lock_timeout_sec', 30.0))
    conn = connect_monitor_db(db_path, timeout_sec=timeout)
    try:
        universe_as_of = (
            args.universe_as_of.isoformat()
            if args.universe_as_of is not None
            else _latest_universe_date(conn, args.as_of)
        )
        universe_rows = [
            dict(row)
            for row in conn.execute(
                'SELECT * FROM monitor_universe WHERE run_as_of=? ORDER BY ticker',
                (universe_as_of,),
            ).fetchall()
        ]
        source_rows = [
            dict(row)
            for row in conn.execute(
                'SELECT * FROM monitor_source_artifacts WHERE run_as_of=? '
                'ORDER BY source_role',
                (universe_as_of,),
            ).fetchall()
        ]
    finally:
        conn.close()
    if not universe_rows:
        raise ValueError(f'No sealed monitor universe for {universe_as_of}')

    tiers = _scheduled_tiers(args.as_of, args.tiers, weekly_weekday)
    events = _event_symbols(args.event_symbols_file)
    batches = build_batches(
        universe_rows,
        tiers=tiers,
        event_symbols=events,
        batch_size=batch_size,
    )
    plan_identity = {
        'policy_version': 'provider_capture_v1',
        'as_of': args.as_of.isoformat(),
        'universe_as_of': universe_as_of,
        'providers': providers,
        'tiers': tiers,
        'events': events,
        'batches': [
            {
                'group': row['group'],
                'batch_number': row['batch_number'],
                'symbols_sha256': row['symbols_sha256'],
            }
            for row in batches
        ],
    }
    plan_digest = _digest(plan_identity)
    date_slug = args.as_of.strftime('%Y%m%d')
    universe_slug = universe_as_of.replace('-', '')
    session_base = (
        f'capture-{date_slug}-u'
        f'{universe_slug}-{plan_digest[:10]}'
    )
    invocation = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else PACKAGE_ROOT
        / 'output'
        / 'provider_capture_sessions'
        / args.as_of.isoformat()
        / f'{session_base}-{invocation}'
    )
    provider_run_root = (
        PACKAGE_ROOT
        / 'output'
        / 'provider_capture_runs'
        / date_slug
        / plan_digest[:12]
    )
    steps_path = output_dir / 'capture_steps.csv'
    plan_path = output_dir / 'capture_plan.json'
    manifest_path = output_dir / 'capture_session_manifest.json'
    fail_if_exists([steps_path, plan_path, manifest_path])
    write_manifest(plan_path, {**plan_identity, 'plan_digest': plan_digest})

    step_rows: list[dict[str, Any]] = []
    failed_batches = 0
    completed_batches = 0
    for batch in batches:
        group = str(batch['group'])
        batch_number = int(batch['batch_number'])
        batch_dir = output_dir / group / f'batch_{batch_number:03d}'
        symbols_path = batch_dir / 'symbols.csv'
        write_csv(
            symbols_path,
            ['ticker'],
            [{'ticker': ticker} for ticker in batch['symbols']],
        )
        cycle_prefix = f'{session_base}-{group}-b{batch_number:03d}'
        conn = connect_monitor_db(db_path, timeout_sec=timeout)
        try:
            cycle, already_complete = _resolve_attempt(
                conn,
                cycle_prefix=cycle_prefix,
                providers=providers,
                max_attempts=max_attempts,
                provider_run_root=provider_run_root,
                group=group,
                batch_number=batch_number,
            )
        finally:
            conn.close()
        if already_complete:
            completed_batches += 1
            for provider in providers:
                step_rows.append(
                    {
                        'group': group,
                        'batch_number': batch_number,
                        'symbol_count': len(batch['symbols']),
                        'symbols_sha256': batch['symbols_sha256'],
                        'retrieval_cycle': cycle,
                        'provider': provider,
                        'status': 'SKIPPED_ALREADY_PASS',
                        'return_code': 0,
                        'detail': 'immutable provider cycle already passed',
                    }
                )
            continue

        batch_return_codes: dict[str, int] = {}
        for provider in providers:
            provider_output = _provider_output_dir(
                provider_run_root,
                group=group,
                batch_number=batch_number,
                provider=provider,
                cycle=cycle,
            )
            command = _snapshot_command(
                config_path=config_path,
                entitlements_path=entitlements_path,
                db_path=db_path,
                provider=provider,
                symbols_path=symbols_path,
                as_of=args.as_of,
                cycle=cycle,
                output_dir=provider_output,
            )
            if args.dry_run:
                return_code = 0
                status = 'DRY_RUN'
            else:
                result = subprocess.run(command, check=False)
                return_code = int(result.returncode)
                status = 'COMMAND_PASS' if return_code == 0 else 'COMMAND_FAIL'
            batch_return_codes[provider] = return_code
            step_rows.append(
                {
                    'group': group,
                    'batch_number': batch_number,
                    'symbol_count': len(batch['symbols']),
                    'symbols_sha256': batch['symbols_sha256'],
                    'retrieval_cycle': cycle,
                    'provider': provider,
                    'status': status,
                    'return_code': return_code,
                    'detail': _command_text(command),
                }
            )
        if args.dry_run:
            continue
        conn = connect_monitor_db(db_path, timeout_sec=timeout)
        try:
            statuses = _cycle_statuses(conn, cycle)
        finally:
            conn.close()
        outputs_pass = all(
            _provider_output_complete(
                _provider_output_dir(
                    provider_run_root,
                    group=group,
                    batch_number=batch_number,
                    provider=provider,
                    cycle=cycle,
                ),
                provider=provider,
                cycle=cycle,
            )
            for provider in providers
        )
        commands_pass = all(batch_return_codes.get(provider) == 0 for provider in providers)
        if (
            commands_pass
            and outputs_pass
            and all(statuses.get(provider) == 'PASS' for provider in providers)
        ):
            completed_batches += 1
        else:
            failed_batches += 1

    write_csv(steps_path, STEP_FIELDS, step_rows)
    if args.dry_run:
        acceptance = 'DRY_RUN'
    elif failed_batches:
        acceptance = 'FAIL'
    elif batches:
        acceptance = 'PASS'
    else:
        acceptance = 'PASS_NOOP'
    input_paths = [
        config_path,
        entitlements_path,
        Path(__file__).resolve(),
        SNAPSHOT_SCRIPT.resolve(),
        Path(__file__).with_name('monitor_common.py').resolve(),
        Path(__file__).with_name('provider_common.py').resolve(),
    ]
    if args.event_symbols_file is not None:
        input_paths.append(args.event_symbols_file.resolve())
    child_manifests: list[dict[str, str]] = []
    seen_child_manifests: set[tuple[str, str]] = set()
    for row in step_rows:
        if row['status'] == 'DRY_RUN':
            continue
        provider = str(row['provider'])
        cycle = str(row['retrieval_cycle'])
        identity = (provider, cycle)
        if identity in seen_child_manifests:
            continue
        seen_child_manifests.add(identity)
        child_dir = _provider_output_dir(
            provider_run_root,
            group=str(row['group']),
            batch_number=int(row['batch_number']),
            provider=provider,
            cycle=cycle,
        )
        child_manifest = child_dir / 'provider_snapshot_manifest.json'
        if child_manifest.is_file():
            child_manifests.append(
                {
                    'provider': provider,
                    'retrieval_cycle': cycle,
                    'manifest_path': str(child_manifest.resolve()),
                    'manifest_sha256': sha256_file(child_manifest),
                }
            )
    write_manifest(
        manifest_path,
        {
            'schema_version': 'provider_capture_session_manifest_v1',
            'acceptance': acceptance,
            'shadow_only': True,
            'as_of_date': args.as_of.isoformat(),
            'universe_as_of': universe_as_of,
            'session_base': session_base,
            'plan_digest': plan_digest,
            'providers': providers,
            'scheduled_tiers': tiers,
            'event_symbol_count': len(events),
            'batch_size': batch_size,
            'batch_count': len(batches),
            'completed_batch_count': completed_batches,
            'failed_batch_count': failed_batches,
            'raw_payloads_retained': False,
            'implementation_or_policy_data_sent': False,
            'provider_request_fields': ['endpoint', 'ticker', 'authentication'],
            'provider_run_root': str(provider_run_root.resolve()),
            'child_manifests': child_manifests,
            'universe_source_artifacts': source_rows,
            'inputs_sha256': {str(path): sha256_file(path) for path in input_paths},
            'outputs_sha256': {
                plan_path.name: sha256_file(plan_path),
                steps_path.name: sha256_file(steps_path),
            },
        },
    )
    print(f'PROVIDER CAPTURE SCHEDULE: {acceptance}')
    print(
        f'batches={len(batches)}; completed={completed_batches}; '
        f'failed={failed_batches}; output={output_dir}'
    )
    return 1 if acceptance == 'FAIL' else 0


if __name__ == '__main__':
    raise SystemExit(main())

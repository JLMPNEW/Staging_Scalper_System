#!/usr/bin/env python3
'''Resolve and evaluate provider forecasts around sealed earnings events.'''

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
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
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.expectations_monitor.monitor_common import (  # noqa: E402
    connect_monitor_db,
)
from portfolio_layer.expectations_monitor.provider_common import (  # noqa: E402
    load_entitlements,
)


DEFAULT_CONFIG = PACKAGE_ROOT / 'config.yaml'
DEFAULT_ENTITLEMENTS = Path(__file__).with_name('provider_entitlements.yaml')
RESOLUTION_SCRIPT = Path(__file__).with_name('46_sync_fiscal_period_resolutions.py')
ACTUALS_SCRIPT = Path(__file__).with_name('45_snapshot_provider_actual_outcomes.py')
LINKER_SCRIPT = Path(__file__).with_name('47_link_provider_forecasts_to_outcomes.py')
EVENT_FIELDS = [
    'ticker',
    'universe_tier',
    'source_pipeline',
    'earnings_date',
    'days_from_as_of',
    'calendar_run_as_of',
    'calendar_fetched_at_utc',
    'calendar_source',
    'fiscal_period_end',
]
STEP_FIELDS = [
    'step',
    'batch_number',
    'symbol_count',
    'cycle',
    'status',
    'return_code',
    'manifest_path',
    'manifest_sha256',
    'detail',
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--entitlements', type=Path, default=DEFAULT_ENTITLEMENTS)
    parser.add_argument('--db', type=Path)
    parser.add_argument('--earnings-history', type=Path)
    parser.add_argument('--as-of', type=date.fromisoformat)
    parser.add_argument('--universe-as-of', type=date.fromisoformat)
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


def _parse_iso_date(value: Any, *, field: str) -> date:
    text = str(value or '').strip()
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f'Invalid {field}: {text!r}') from exc


def select_events(
    calendar_path: Path,
    *,
    as_of: date,
    universe_by_ticker: dict[str, dict[str, Any]],
    included_tiers: set[str],
    calendar_source: str,
    lookback_days: int,
    lookahead_days: int,
    maximum_staleness_days: int,
) -> tuple[list[dict[str, Any]], date]:
    required = {
        'run_as_of_date',
        'fetched_at_utc',
        'ticker',
        'next_earnings_date',
        'fiscal_date_ending',
        'source',
    }
    latest_by_ticker: dict[str, tuple[tuple[date, str], dict[str, str]]] = {}
    latest_calendar_date: date | None = None
    with calendar_path.resolve().open(
        'r', encoding='utf-8-sig', newline=''
    ) as handle:
        reader = csv.DictReader(handle)
        missing = sorted(required - set(reader.fieldnames or []))
        if missing:
            raise ValueError(f'Earnings calendar missing columns: {missing}')
        for row in reader:
            if str(row.get('source', '')).strip() != calendar_source:
                continue
            try:
                run_as_of = _parse_iso_date(
                    row.get('run_as_of_date'), field='calendar run_as_of_date'
                )
                _parse_iso_date(
                    row.get('next_earnings_date'), field='next_earnings_date'
                )
            except ValueError:
                continue
            if run_as_of > as_of:
                continue
            latest_calendar_date = max(latest_calendar_date or run_as_of, run_as_of)
            ticker = str(row.get('ticker', '')).strip().upper()
            if ticker not in universe_by_ticker:
                continue
            key = (run_as_of, str(row.get('fetched_at_utc', '')).strip())
            prior = latest_by_ticker.get(ticker)
            if prior is None or key > prior[0]:
                latest_by_ticker[ticker] = (key, row)
    if latest_calendar_date is None:
        raise ValueError(
            f'No {calendar_source!r} calendar rows exist on or before {as_of}'
        )
    staleness = (as_of - latest_calendar_date).days
    if staleness > maximum_staleness_days:
        raise ValueError(
            f'Earnings calendar is {staleness} days stale; maximum is '
            f'{maximum_staleness_days}'
        )

    lower = as_of - timedelta(days=lookback_days)
    upper = as_of + timedelta(days=lookahead_days)
    selected: list[dict[str, Any]] = []
    for ticker, (_, row) in latest_by_ticker.items():
        universe_row = universe_by_ticker[ticker]
        tier = str(universe_row.get('tier', '')).strip()
        if tier not in included_tiers:
            continue
        earnings_date = _parse_iso_date(
            row.get('next_earnings_date'), field='next_earnings_date'
        )
        if not lower <= earnings_date <= upper:
            continue
        selected.append(
            {
                'ticker': ticker,
                'universe_tier': tier,
                'source_pipeline': str(
                    universe_row.get('source_pipeline', '')
                ).strip(),
                'earnings_date': earnings_date.isoformat(),
                'days_from_as_of': (earnings_date - as_of).days,
                'calendar_run_as_of': str(row.get('run_as_of_date', '')).strip(),
                'calendar_fetched_at_utc': str(
                    row.get('fetched_at_utc', '')
                ).strip(),
                'calendar_source': calendar_source,
                'fiscal_period_end': str(
                    row.get('fiscal_date_ending', '')
                ).strip(),
            }
        )
    return sorted(selected, key=lambda row: (row['earnings_date'], row['ticker'])), latest_calendar_date


def _output_hashes_valid(manifest_path: Path, manifest: dict[str, Any]) -> bool:
    outputs = manifest.get('outputs_sha256')
    if not isinstance(outputs, dict) or not outputs:
        return False
    for name, expected in outputs.items():
        path = manifest_path.parent / str(name)
        if not path.is_file() or sha256_file(path) != str(expected):
            return False
    return True


def _child_manifest_valid(
    manifest_path: Path,
    *,
    accepted: set[str],
    identity_field: str,
    identity: str,
    symbol_count: int | None = None,
) -> bool:
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return False
    if str(manifest.get('acceptance', '')) not in accepted:
        return False
    if str(manifest.get(identity_field, '')) != identity:
        return False
    if symbol_count is not None and int(manifest.get('symbol_count', -1)) != symbol_count:
        return False
    return _output_hashes_valid(manifest_path, manifest)


def _prior_parent_valid(path: Path, *, plan_digest: str) -> bool:
    try:
        manifest = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return False
    if manifest.get('acceptance') not in {'PASS', 'PASS_NO_EVENTS'}:
        return False
    if manifest.get('plan_digest') != plan_digest:
        return False
    if not _output_hashes_valid(path, manifest):
        return False
    children = manifest.get('child_manifests')
    if not isinstance(children, list):
        return False
    for child in children:
        if not isinstance(child, dict):
            return False
        child_path = Path(str(child.get('manifest_path', '')))
        if not child_path.is_file():
            return False
        if sha256_file(child_path) != str(child.get('manifest_sha256', '')):
            return False
    return True


def _latest_failed_resume(
    root: Path,
    *,
    plan_digest: str,
    economic_identity: dict[str, Any],
) -> tuple[Path | None, dict[tuple[int, str], tuple[Path, dict[str, Any]]]]:
    for path in sorted(
        root.glob('*/provider_event_cycle_manifest.json'), reverse=True
    ):
        try:
            manifest = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        if manifest.get('acceptance') != 'FAIL':
            continue
        if manifest.get('plan_digest') != plan_digest:
            continue
        if not _output_hashes_valid(path, manifest):
            continue
        children = manifest.get('child_manifests')
        if not isinstance(children, list):
            continue
        state: dict[tuple[int, str], tuple[Path, dict[str, Any]]] = {}
        valid = True
        for child in children:
            if not isinstance(child, dict):
                valid = False
                break
            child_path = Path(str(child.get('manifest_path', '')))
            if (
                not child_path.is_file()
                or sha256_file(child_path) != str(child.get('manifest_sha256', ''))
            ):
                valid = False
                break
            try:
                child_manifest = json.loads(child_path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError):
                valid = False
                break
            if not _output_hashes_valid(child_path, child_manifest):
                valid = False
                break
            match = re.search(r'-b(\d{3})-', str(child.get('cycle', '')))
            step = str(child.get('step', ''))
            if match is not None and step:
                state[(int(match.group(1)), step)] = (child_path, child_manifest)
        if valid:
            return path, state
    return None, {}


def _command_text(command: list[str]) -> str:
    return subprocess.list2cmdline(command)


def _run_child(
    command: list[str],
    *,
    dry_run: bool,
) -> int:
    if dry_run:
        return 0
    return int(subprocess.run(command, check=False).returncode)


def run_selftest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        calendar = root / 'calendar.csv'
        fields = [
            'run_as_of_date',
            'fetched_at_utc',
            'ticker',
            'next_earnings_date',
            'fiscal_date_ending',
            'source',
        ]
        write_csv(
            calendar,
            fields,
            [
                {
                    'run_as_of_date': '2026-07-29',
                    'fetched_at_utc': '2026-07-29T22:00:00Z',
                    'ticker': 'AAA',
                    'next_earnings_date': '2026-08-20',
                    'fiscal_date_ending': '2026-06-30',
                    'source': 'alpha_vantage_bulk',
                },
                {
                    'run_as_of_date': '2026-07-31',
                    'fetched_at_utc': '2026-07-31T22:00:00Z',
                    'ticker': 'AAA',
                    'next_earnings_date': '2026-08-01',
                    'fiscal_date_ending': '2026-06-30',
                    'source': 'alpha_vantage_bulk',
                },
                {
                    'run_as_of_date': '2026-07-31',
                    'fetched_at_utc': '2026-07-31T22:00:00Z',
                    'ticker': 'BBB',
                    'next_earnings_date': '2026-07-31',
                    'fiscal_date_ending': '2026-06-30',
                    'source': 'alpha_vantage_bulk',
                },
                {
                    'run_as_of_date': '2026-07-31',
                    'fetched_at_utc': '2026-07-31T22:00:00Z',
                    'ticker': 'CCC',
                    'next_earnings_date': '2026-07-31',
                    'fiscal_date_ending': '2026-06-30',
                    'source': 'other',
                },
            ],
        )
        rows, latest = select_events(
            calendar,
            as_of=date(2026, 7, 31),
            universe_by_ticker={
                'AAA': {'tier': 'tier0', 'source_pipeline': 'test'},
                'BBB': {'tier': 'tier2', 'source_pipeline': 'test'},
                'CCC': {'tier': 'tier1', 'source_pipeline': 'test'},
            },
            included_tiers={'tier0', 'tier1'},
            calendar_source='alpha_vantage_bulk',
            lookback_days=2,
            lookahead_days=2,
            maximum_staleness_days=3,
        )
        assert latest == date(2026, 7, 31)
        assert [row['ticker'] for row in rows] == ['AAA']

        output = root / 'child'
        report = output / 'report.csv'
        manifest_path = output / 'manifest.json'
        write_csv(report, ['status'], [{'status': 'PASS'}])
        write_manifest(
            manifest_path,
            {
                'acceptance': 'PASS',
                'retrieval_cycle': 'test-cycle',
                'symbol_count': 1,
                'outputs_sha256': {report.name: sha256_file(report)},
            },
        )
        assert _child_manifest_valid(
            manifest_path,
            accepted={'PASS'},
            identity_field='retrieval_cycle',
            identity='test-cycle',
            symbol_count=1,
        )
        report.write_text('status\nFAIL\n', encoding='utf-8')
        assert not _child_manifest_valid(
            manifest_path,
            accepted={'PASS'},
            identity_field='retrieval_cycle',
            identity='test-cycle',
            symbol_count=1,
        )

        resume_root = root / 'resume'
        parent_dir = resume_root / 'failed-run'
        prior_plan_path = parent_dir / 'provider_event_plan.json'
        prior_steps_path = parent_dir / 'provider_event_steps.csv'
        prior_events_path = parent_dir / 'selected_earnings_events.csv'
        prior_child_dir = parent_dir / 'child'
        prior_child_report = prior_child_dir / 'fiscal_period_resolutions.csv'
        prior_child_manifest = (
            prior_child_dir / 'fiscal_period_resolution_manifest.json'
        )
        write_csv(prior_child_report, ['ticker'], [{'ticker': 'AAA'}])
        write_manifest(
            prior_child_manifest,
            {
                'acceptance': 'PASS',
                'retrieval_cycle': 'event-test-b001-period-r01-old',
                'symbol_count': 1,
                'outputs_sha256': {
                    prior_child_report.name: sha256_file(prior_child_report)
                },
            },
        )
        economic_identity = {
            'policy_version': 'provider_event_cycle_v1',
            'as_of_date': '2026-07-31',
        }
        write_manifest(
            prior_plan_path,
            {**economic_identity, 'plan_digest': 'old-digest'},
        )
        write_csv(prior_steps_path, ['status'], [{'status': 'FAIL'}])
        write_csv(prior_events_path, ['ticker'], [{'ticker': 'AAA'}])
        prior_parent = parent_dir / 'provider_event_cycle_manifest.json'
        write_manifest(
            prior_parent,
            {
                'acceptance': 'FAIL',
                'plan_digest': 'old-digest',
                'child_manifests': [
                    {
                        'step': 'fiscal_period_resolution',
                        'cycle': 'event-test-b001-period-r01-old',
                        'manifest_path': str(prior_child_manifest.resolve()),
                        'manifest_sha256': sha256_file(prior_child_manifest),
                    }
                ],
                'outputs_sha256': {
                    prior_plan_path.name: sha256_file(prior_plan_path),
                    prior_steps_path.name: sha256_file(prior_steps_path),
                    prior_events_path.name: sha256_file(prior_events_path),
                },
            },
        )
        resumed_path, resumed = _latest_failed_resume(
            resume_root,
            plan_digest='new-digest',
            economic_identity=economic_identity,
        )
        assert resumed_path is None and not resumed
        resumed_path, resumed = _latest_failed_resume(
            resume_root,
            plan_digest='old-digest',
            economic_identity=economic_identity,
        )
        assert resumed_path == prior_parent
        assert (1, 'fiscal_period_resolution') in resumed
    print('provider earnings-event cycle selftest: PASS')


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
    paths = resolve_runtime_paths(config, config_path)
    earnings_history = (
        args.earnings_history.resolve()
        if args.earnings_history is not None
        else paths.output_dir / 'earnings_dates' / 'earnings_calendar_history.csv'
    )
    monitor_cfg = cfg_get(config, 'expectations_monitor', {})
    if not isinstance(monitor_cfg, dict):
        raise ValueError('expectations_monitor config must be a mapping')
    capture_cfg = monitor_cfg.get('provider_capture', {})
    if not isinstance(capture_cfg, dict):
        raise ValueError('expectations_monitor.provider_capture must be a mapping')
    event_cfg = capture_cfg.get('event_cycle', {})
    if not isinstance(event_cfg, dict):
        raise ValueError('provider_capture.event_cycle must be a mapping')
    if event_cfg.get('policy_version') != 'provider_event_cycle_v1':
        raise ValueError('provider_event_cycle_v1 config is required')
    included_tiers = {str(value) for value in event_cfg.get('included_tiers', [])}
    if not included_tiers or not included_tiers <= {'tier0', 'tier1', 'tier2'}:
        raise ValueError('event_cycle.included_tiers is invalid')
    lookback_days = int(event_cfg.get('lookback_calendar_days', -1))
    lookahead_days = int(event_cfg.get('lookahead_calendar_days', -1))
    maximum_staleness = int(event_cfg.get('maximum_calendar_staleness_days', -1))
    maximum_symbols = int(event_cfg.get('maximum_event_symbols', 0))
    max_attempts = int(capture_cfg.get('max_attempts_per_batch', 0))
    if min(lookback_days, lookahead_days, maximum_staleness) < 0:
        raise ValueError('Event-cycle day windows must be non-negative')
    if maximum_symbols < 1:
        raise ValueError('maximum_event_symbols must be positive')
    if max_attempts < 1:
        raise ValueError('max_attempts_per_batch must be positive')
    calendar_source = str(event_cfg.get('calendar_source', '')).strip()
    if not calendar_source:
        raise ValueError('event_cycle.calendar_source is required')

    entitlements = load_entitlements(entitlements_path)
    provider_caps = entitlements.get('probe', {}).get('max_symbols_by_provider', {})
    batch_size = min(
        int(capture_cfg.get('batch_size', 50)),
        int(provider_caps.get('alpha_vantage', 0)),
        int(provider_caps.get('fmp', 0)),
    )
    if batch_size < 1:
        raise ValueError('No positive provider batch cap is configured')

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
    universe_by_ticker = {str(row['ticker']): row for row in universe_rows}
    events, latest_calendar_date = select_events(
        earnings_history,
        as_of=args.as_of,
        universe_by_ticker=universe_by_ticker,
        included_tiers=included_tiers,
        calendar_source=calendar_source,
        lookback_days=lookback_days,
        lookahead_days=lookahead_days,
        maximum_staleness_days=maximum_staleness,
    )
    if len(events) > maximum_symbols:
        raise RuntimeError(
            f'Event symbol count {len(events)} exceeds fail-closed cap {maximum_symbols}'
        )

    input_paths = [
        config_path,
        entitlements_path,
        earnings_history,
        Path(__file__).resolve(),
        RESOLUTION_SCRIPT.resolve(),
        ACTUALS_SCRIPT.resolve(),
        LINKER_SCRIPT.resolve(),
        Path(__file__).with_name('monitor_common.py').resolve(),
        Path(__file__).with_name('provider_common.py').resolve(),
    ]
    plan_identity = {
        'policy_version': 'provider_event_cycle_v1',
        'as_of_date': args.as_of.isoformat(),
        'universe_as_of': universe_as_of,
        'included_tiers': sorted(included_tiers),
        'calendar_source': calendar_source,
        'latest_calendar_date': latest_calendar_date.isoformat(),
        'lookback_calendar_days': lookback_days,
        'lookahead_calendar_days': lookahead_days,
        'batch_size': batch_size,
        'events_digest': _digest(events),
        'universe_digest': _digest(universe_rows),
        'inputs_sha256': {str(path): sha256_file(path) for path in input_paths},
    }
    plan_digest = _digest(plan_identity)
    root = paths.output_dir / 'provider_event_cycles' / args.as_of.isoformat()
    if args.output_dir is None:
        for prior in sorted(root.glob('*/provider_event_cycle_manifest.json')):
            if _prior_parent_valid(prior, plan_digest=plan_digest):
                print('PROVIDER EARNINGS EVENT CYCLE: SKIPPED_ALREADY_PASS')
                print(f'prior_manifest={prior}')
                return 0
    economic_identity = {
        field: plan_identity[field]
        for field in (
            'policy_version',
            'as_of_date',
            'universe_as_of',
            'included_tiers',
            'calendar_source',
            'latest_calendar_date',
            'lookback_calendar_days',
            'lookahead_calendar_days',
            'batch_size',
            'events_digest',
            'universe_digest',
        )
    }
    resume_path, resume_children = _latest_failed_resume(
        root,
        plan_digest=plan_digest,
        economic_identity=economic_identity,
    )
    invocation = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    cycle_base = (
        f'event-{args.as_of.strftime("%Y%m%d")}-u'
        f'{universe_as_of.replace("-", "")}-{plan_digest[:10]}'
    )
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else root / f'{cycle_base}-{invocation}'
    )
    events_path = output_dir / 'selected_earnings_events.csv'
    steps_path = output_dir / 'provider_event_steps.csv'
    plan_path = output_dir / 'provider_event_plan.json'
    manifest_path = output_dir / 'provider_event_cycle_manifest.json'
    fail_if_exists([events_path, steps_path, plan_path, manifest_path])
    write_csv(events_path, EVENT_FIELDS, events)
    write_manifest(plan_path, {**plan_identity, 'plan_digest': plan_digest})

    step_rows: list[dict[str, Any]] = []
    child_manifests: list[dict[str, str]] = []
    failed = False
    symbols = [str(row['ticker']) for row in events]
    for start in range(0, len(symbols), batch_size):
        batch_number = start // batch_size + 1
        batch_symbols = symbols[start : start + batch_size]
        batch_dir = output_dir / 'children' / f'b{batch_number:03d}'
        children = [
            (
                'fiscal_period_resolution',
                RESOLUTION_SCRIPT,
                batch_dir / 'periods',
                'fiscal_period_resolution_manifest.json',
                {'PASS'},
            ),
            (
                'fmp_actual_outcomes',
                ACTUALS_SCRIPT,
                batch_dir / 'actuals',
                'provider_actual_outcomes_manifest.json',
                {'PASS', 'PASS_NO_NEW_OUTCOMES'},
            ),
        ]
        for step, script, child_output, child_manifest_name, accepted in children:
            pending_symbols = list(batch_symbols)
            prior_child = resume_children.get((batch_number, step))
            if prior_child is not None:
                prior_path, prior_manifest = prior_child
                prior_acceptance = str(prior_manifest.get('acceptance', ''))
                prior_symbol_count = int(prior_manifest.get('symbol_count', -1))
                prior_cycle = str(prior_manifest.get('retrieval_cycle', ''))
                prior_hash = sha256_file(prior_path)
                if prior_acceptance in accepted and prior_symbol_count == len(batch_symbols):
                    pending_symbols = []
                    child_manifests.append(
                        {
                            'step': step,
                            'cycle': prior_cycle,
                            'manifest_path': str(prior_path.resolve()),
                            'manifest_sha256': prior_hash,
                        }
                    )
                    step_rows.append(
                        {
                            'step': step,
                            'batch_number': batch_number,
                            'symbol_count': len(batch_symbols),
                            'cycle': prior_cycle,
                            'status': 'REUSED_PRIOR_PASS',
                            'return_code': 0,
                            'manifest_path': str(prior_path.resolve()),
                            'manifest_sha256': prior_hash,
                            'detail': f'resumed from {resume_path}',
                        }
                    )
                elif step == 'fiscal_period_resolution' and prior_acceptance == 'FAIL':
                    failed_symbols = sorted(
                        {
                            str(value).strip().upper()
                            for value in prior_manifest.get('failed_symbols', [])
                            if str(value).strip().upper() in set(batch_symbols)
                        }
                    )
                    if failed_symbols and len(failed_symbols) < len(batch_symbols):
                        pending_symbols = failed_symbols
                        child_manifests.append(
                            {
                                'step': step,
                                'cycle': prior_cycle,
                                'manifest_path': str(prior_path.resolve()),
                                'manifest_sha256': prior_hash,
                            }
                        )
                        step_rows.append(
                            {
                                'step': step,
                                'batch_number': batch_number,
                                'symbol_count': len(batch_symbols) - len(failed_symbols),
                                'cycle': prior_cycle,
                                'status': 'REUSED_PRIOR_PARTIAL',
                                'return_code': 1,
                                'manifest_path': str(prior_path.resolve()),
                                'manifest_sha256': prior_hash,
                                'detail': (
                                    f'resumed {len(batch_symbols) - len(failed_symbols)} '
                                    f'successes from {resume_path}'
                                ),
                            }
                        )

            step_valid = not pending_symbols
            for attempt in range(1, max_attempts + 1):
                if not pending_symbols:
                    break
                attempt_symbols = list(pending_symbols)
                attempt_slug = f'r{attempt:02d}'
                symbols_path = batch_dir / f'{step}-{attempt_slug}-symbols.csv'
                write_csv(
                    symbols_path,
                    ['ticker'],
                    [{'ticker': value} for value in attempt_symbols],
                )
                retrieval_cycle = (
                    f'{cycle_base}-b{batch_number:03d}-'
                    f'{step}-{attempt_slug}-{invocation}'
                )
                attempt_output = child_output.with_name(
                    f'{child_output.name}-{attempt_slug}'
                )
                command = [
                    sys.executable,
                    str(script),
                    '--config',
                    str(config_path),
                    '--entitlements',
                    str(entitlements_path),
                    '--db',
                    str(db_path),
                    '--earnings-history',
                    str(earnings_history),
                    '--symbols-file',
                    str(symbols_path),
                    '--as-of',
                    args.as_of.isoformat(),
                    '--retrieval-cycle',
                    retrieval_cycle,
                    '--output-dir',
                    str(attempt_output),
                ]
                return_code = _run_child(command, dry_run=args.dry_run)
                child_manifest = attempt_output / child_manifest_name
                valid = args.dry_run or (
                    return_code == 0
                    and _child_manifest_valid(
                        child_manifest,
                        accepted=accepted,
                        identity_field='retrieval_cycle',
                        identity=retrieval_cycle,
                        symbol_count=len(attempt_symbols),
                    )
                )
                status = 'DRY_RUN' if args.dry_run else ('PASS' if valid else 'FAIL')
                manifest_hash = (
                    sha256_file(child_manifest) if child_manifest.is_file() else ''
                )
                if manifest_hash:
                    child_manifests.append(
                        {
                            'step': step,
                            'cycle': retrieval_cycle,
                            'manifest_path': str(child_manifest.resolve()),
                            'manifest_sha256': manifest_hash,
                        }
                    )
                step_rows.append(
                    {
                        'step': step,
                        'batch_number': batch_number,
                        'symbol_count': len(attempt_symbols),
                        'cycle': retrieval_cycle,
                        'status': status,
                        'return_code': return_code,
                        'manifest_path': str(child_manifest.resolve()),
                        'manifest_sha256': manifest_hash,
                        'detail': _command_text(command),
                    }
                )
                if valid:
                    pending_symbols = []
                    step_valid = True
                    break
                if step == 'fiscal_period_resolution' and child_manifest.is_file():
                    try:
                        attempt_manifest = json.loads(
                            child_manifest.read_text(encoding='utf-8')
                        )
                    except (OSError, json.JSONDecodeError):
                        attempt_manifest = {}
                    retry_symbols = sorted(
                        {
                            str(value).strip().upper()
                            for value in attempt_manifest.get('failed_symbols', [])
                            if str(value).strip().upper() in set(attempt_symbols)
                        }
                    )
                    pending_symbols = retry_symbols or attempt_symbols
                if attempt < max_attempts and not args.dry_run:
                    time.sleep(min(30.0, 5.0 * attempt))
            if not step_valid:
                failed = True

    if events and (args.dry_run or not failed):
        evaluation_cycle = f'{cycle_base}-evaluation-{invocation}'
        linker_output = output_dir / 'children' / 'evaluation'
        command = [
            sys.executable,
            str(LINKER_SCRIPT),
            '--config',
            str(config_path),
            '--db',
            str(db_path),
            '--as-of',
            args.as_of.isoformat(),
            '--evaluation-cycle',
            evaluation_cycle,
            '--output-dir',
            str(linker_output),
        ]
        return_code = _run_child(command, dry_run=args.dry_run)
        child_manifest = linker_output / 'forecast_evaluation_manifest.json'
        valid = args.dry_run or (
            return_code == 0
            and _child_manifest_valid(
                child_manifest,
                accepted={'PASS', 'PASS_NO_CANDIDATES'},
                identity_field='evaluation_cycle',
                identity=evaluation_cycle,
            )
        )
        status = 'DRY_RUN' if args.dry_run else ('PASS' if valid else 'FAIL')
        if not valid:
            failed = True
        manifest_hash = sha256_file(child_manifest) if child_manifest.is_file() else ''
        if manifest_hash:
            child_manifests.append(
                {
                    'step': 'forecast_outcome_linker',
                    'cycle': evaluation_cycle,
                    'manifest_path': str(child_manifest.resolve()),
                    'manifest_sha256': manifest_hash,
                }
            )
        step_rows.append(
            {
                'step': 'forecast_outcome_linker',
                'batch_number': 0,
                'symbol_count': len(symbols),
                'cycle': evaluation_cycle,
                'status': status,
                'return_code': return_code,
                'manifest_path': str(child_manifest.resolve()),
                'manifest_sha256': manifest_hash,
                'detail': _command_text(command),
            }
        )

    write_csv(steps_path, STEP_FIELDS, step_rows)
    if args.dry_run:
        acceptance = 'DRY_RUN'
    elif failed:
        acceptance = 'FAIL'
    elif events:
        acceptance = 'PASS'
    else:
        acceptance = 'PASS_NO_EVENTS'
    write_manifest(
        manifest_path,
        {
            'schema_version': 'provider_event_cycle_manifest_v1',
            'acceptance': acceptance,
            'shadow_only': True,
            'as_of_date': args.as_of.isoformat(),
            'universe_as_of': universe_as_of,
            'plan_digest': plan_digest,
            'event_count': len(events),
            'batch_count': (len(events) + batch_size - 1) // batch_size,
            'latest_calendar_date': latest_calendar_date.isoformat(),
            'included_tiers': sorted(included_tiers),
            'provider_request_fields': ['endpoint', 'ticker', 'authentication'],
            'implementation_or_policy_data_sent': False,
            'raw_payloads_retained': False,
            'child_manifests': child_manifests,
            'resumed_from_manifest': (
                '' if resume_path is None else str(resume_path.resolve())
            ),
            'universe_source_artifacts': source_rows,
            'inputs_sha256': plan_identity['inputs_sha256'],
            'outputs_sha256': {
                events_path.name: sha256_file(events_path),
                steps_path.name: sha256_file(steps_path),
                plan_path.name: sha256_file(plan_path),
            },
        },
    )
    print(f'PROVIDER EARNINGS EVENT CYCLE: {acceptance}')
    print(
        f'events={len(events)}; batches='
        f'{(len(events) + batch_size - 1) // batch_size}; output={output_dir}'
    )
    return 1 if acceptance == 'FAIL' else 0


if __name__ == '__main__':
    raise SystemExit(main())

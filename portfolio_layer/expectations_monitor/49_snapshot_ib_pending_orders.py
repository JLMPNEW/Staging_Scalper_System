#!/usr/bin/env python3
'''Capture a sealed, read-only IB open-order snapshot for monitor coordination.'''

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


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
    monitor_output_subdir,
)
from portfolio_layer.ledger.ledger_common import peek_statement_period_end  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / 'config.yaml'
FIELDS = [
    'as_of_date',
    'captured_at_utc',
    'account_id_sha256',
    'ticker',
    'security_type',
    'local_symbol',
    'currency',
    'exchange',
    'action',
    'order_type',
    'total_quantity',
    'filled_quantity',
    'remaining_quantity',
    'limit_price',
    'aux_price',
    'time_in_force',
    'outside_rth',
    'status',
    'permanent_id',
    'order_id',
    'client_id',
    'order_ref',
    'source',
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--as-of', type=date.fromisoformat)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--client-id', type=int)
    parser.add_argument('--source-mode', choices=('static', 'live'))
    parser.add_argument('--replay-csv', type=Path)
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--selftest', action='store_true')
    return parser.parse_args()


def _text(value: Any) -> str:
    return str(value or '').strip()


def _number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if result == result else 0.0


def _account_hash(account: str) -> str:
    return hashlib.sha256(account.encode('utf-8')).hexdigest() if account else ''


def normalize_orders(
    rows: Iterable[dict[str, Any]],
    *,
    as_of: date,
    captured_at_utc: str,
    account: str,
    source: str = 'ibkr_read_only_open_orders',
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in rows:
        row_account = _text(raw.get('account'))
        if account and row_account and row_account != account:
            continue
        remaining = _number(raw.get('remaining_quantity'))
        status = _text(raw.get('status'))
        if remaining <= 1e-12 or status.casefold() in {
            'cancelled',
            'apicancelled',
            'filled',
            'inactive',
        }:
            continue
        ticker = _text(raw.get('ticker') or raw.get('symbol')).upper()
        if not ticker or ticker == 'CASH' or any(char.isspace() for char in ticker):
            raise ValueError(f'Invalid pending-order ticker: {ticker!r}')
        key = (
            _text(raw.get('permanent_id')),
            _text(raw.get('client_id')),
            _text(raw.get('order_id')),
        )
        if key in seen:
            raise ValueError(f'Duplicate pending-order identity: {key}')
        seen.add(key)
        output.append(
            {
                'as_of_date': as_of.isoformat(),
                'captured_at_utc': captured_at_utc,
                'account_id_sha256': _account_hash(account or row_account),
                'ticker': ticker,
                'security_type': _text(raw.get('security_type')).upper(),
                'local_symbol': _text(raw.get('local_symbol')),
                'currency': _text(raw.get('currency')).upper(),
                'exchange': _text(raw.get('exchange')).upper(),
                'action': _text(raw.get('action')).upper(),
                'order_type': _text(raw.get('order_type')).upper(),
                'total_quantity': _number(raw.get('total_quantity')),
                'filled_quantity': _number(raw.get('filled_quantity')),
                'remaining_quantity': remaining,
                'limit_price': _number(raw.get('limit_price')),
                'aux_price': _number(raw.get('aux_price')),
                'time_in_force': _text(raw.get('time_in_force')).upper(),
                'outside_rth': int(bool(raw.get('outside_rth'))),
                'status': status,
                'permanent_id': _text(raw.get('permanent_id')),
                'order_id': _text(raw.get('order_id')),
                'client_id': _text(raw.get('client_id')),
                'order_ref': _text(raw.get('order_ref')),
                'source': source,
            }
        )
    return sorted(
        output,
        key=lambda row: (
            row['ticker'],
            row['security_type'],
            row['permanent_id'],
            row['client_id'],
            row['order_id'],
        ),
    )


def _replay_rows(path: Path) -> list[dict[str, Any]]:
    with path.resolve().open('r', encoding='utf-8-sig', newline='') as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _select_static_statement(
    *,
    source_dir: Path,
    statement_glob: str,
    as_of: date,
    max_stale_days: int,
) -> tuple[Path, date]:
    if max_stale_days < 0:
        raise ValueError('pending_orders.static.max_stale_days must be non-negative')
    if not source_dir.is_dir():
        raise FileNotFoundError(f'IB static source directory is missing: {source_dir}')
    candidates: list[tuple[date, int, float, Path]] = []
    for candidate in source_dir.glob(statement_glob):
        path = ensure_not_prod_path(candidate.resolve(), label='IB static statement')
        period_end_text = peek_statement_period_end(path)
        if not period_end_text:
            continue
        try:
            period_end = date.fromisoformat(period_end_text)
        except ValueError:
            continue
        if period_end <= as_of:
            stat = path.stat()
            candidates.append((period_end, stat.st_size, stat.st_mtime, path))
    if not candidates:
        raise FileNotFoundError(
            f'No dated IB statement on or before {as_of} under {source_dir} '
            f'with glob {statement_glob!r}'
        )
    period_end, _, _, selected = max(candidates)
    stale_days = (as_of - period_end).days
    if stale_days > max_stale_days:
        raise RuntimeError(
            f'Latest IB static statement is stale: period_end={period_end}; '
            f'as_of={as_of}; stale_days={stale_days}; max={max_stale_days}'
        )
    return selected, period_end


def _live_rows(
    *,
    host: str,
    port: int,
    client_id: int,
    timeout_sec: float,
    configured_account: str,
    require_single_account: bool,
) -> tuple[list[dict[str, Any]], str]:
    try:
        from ib_insync import IB  # type: ignore
    except ImportError as exc:
        raise RuntimeError('ib_insync is required for live pending-order capture') from exc
    ib = IB()
    try:
        ib.connect(
            host,
            port,
            clientId=client_id,
            timeout=timeout_sec,
            readonly=True,
        )
        accounts = sorted(set(ib.managedAccounts()))
        if configured_account:
            if configured_account not in accounts:
                raise RuntimeError('Configured IB monitor account is not managed by this session')
            account = configured_account
        elif require_single_account:
            if len(accounts) != 1:
                raise RuntimeError(
                    f'Expected exactly one managed IB account; observed {len(accounts)}'
                )
            account = accounts[0]
        else:
            raise RuntimeError('An explicit IB monitor account is required')
        trades = ib.reqAllOpenOrders()
        ib.sleep(0.5)
        rows: list[dict[str, Any]] = []
        for trade in trades:
            order = trade.order
            order_status = trade.orderStatus
            contract = trade.contract
            rows.append(
                {
                    'account': _text(getattr(order, 'account', '')),
                    'ticker': _text(getattr(contract, 'symbol', '')),
                    'security_type': _text(getattr(contract, 'secType', '')),
                    'local_symbol': _text(getattr(contract, 'localSymbol', '')),
                    'currency': _text(getattr(contract, 'currency', '')),
                    'exchange': _text(
                        getattr(contract, 'primaryExchange', '')
                        or getattr(contract, 'exchange', '')
                    ),
                    'action': _text(getattr(order, 'action', '')),
                    'order_type': _text(getattr(order, 'orderType', '')),
                    'total_quantity': getattr(order, 'totalQuantity', 0.0),
                    'filled_quantity': getattr(order_status, 'filled', 0.0),
                    'remaining_quantity': getattr(order_status, 'remaining', 0.0),
                    'limit_price': getattr(order, 'lmtPrice', 0.0),
                    'aux_price': getattr(order, 'auxPrice', 0.0),
                    'time_in_force': _text(getattr(order, 'tif', '')),
                    'outside_rth': bool(getattr(order, 'outsideRth', False)),
                    'status': _text(getattr(order_status, 'status', '')),
                    'permanent_id': getattr(order, 'permId', 0),
                    'order_id': getattr(order, 'orderId', 0),
                    'client_id': getattr(order, 'clientId', 0),
                    'order_ref': _text(getattr(order, 'orderRef', '')),
                }
            )
        return rows, account
    finally:
        if ib.isConnected():
            ib.disconnect()


def run_selftest() -> None:
    rows = normalize_orders(
        [
            {
                'account': 'DU123',
                'ticker': 'AAA',
                'security_type': 'STK',
                'action': 'BUY',
                'order_type': 'LMT',
                'total_quantity': 10,
                'filled_quantity': 2,
                'remaining_quantity': 8,
                'status': 'Submitted',
                'permanent_id': 100,
                'order_id': 2,
                'client_id': 1,
            },
            {
                'account': 'DU123',
                'ticker': 'BBB',
                'remaining_quantity': 0,
                'status': 'Filled',
                'permanent_id': 101,
                'order_id': 3,
                'client_id': 1,
            },
        ],
        as_of=date(2026, 7, 31),
        captured_at_utc='2026-08-01T00:00:00+00:00',
        account='DU123',
    )
    assert len(rows) == 1
    assert rows[0]['ticker'] == 'AAA'
    assert rows[0]['remaining_quantity'] == 8.0
    assert rows[0]['account_id_sha256'] == _account_hash('DU123')
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        older = root / 'U1_20260723.csv'
        latest = root / 'U1_20260724.csv'
        older.write_text(
            'Statement,Header,Field Name,Field Value\n'
            'Statement,Data,Period,"July 23, 2026"\n',
            encoding='utf-8',
        )
        latest.write_text(
            'Statement,Header,Field Name,Field Value\n'
            'Statement,Data,Period,"July 24, 2026"\n',
            encoding='utf-8',
        )
        selected, period_end = _select_static_statement(
            source_dir=root,
            statement_glob='U*.csv',
            as_of=date(2026, 7, 31),
            max_stale_days=10,
        )
        assert selected == latest
        assert period_end == date(2026, 7, 24)
        try:
            _select_static_statement(
                source_dir=root,
                statement_glob='U*.csv',
                as_of=date(2026, 7, 31),
                max_stale_days=6,
            )
        except RuntimeError as exc:
            assert 'stale_days=7' in str(exc)
        else:
            raise AssertionError('stale static statement must fail closed')
    print('IB pending-order snapshot selftest: PASS')


def main() -> int:
    args = parse_args()
    if args.selftest:
        run_selftest()
        return 0
    if args.as_of is None:
        raise ValueError('--as-of is required')
    config_path = args.config.resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    monitor_cfg = cfg_get(config, 'expectations_monitor', {})
    if not isinstance(monitor_cfg, dict):
        raise ValueError('expectations_monitor config must be a mapping')
    policy = monitor_cfg.get('pending_orders', {})
    if not isinstance(policy, dict) or policy.get('policy_version') != 'ib_pending_orders_v1':
        raise ValueError('ib_pending_orders_v1 config is required')
    source_mode = _text(args.source_mode or policy.get('source_mode')).casefold()
    if source_mode not in {'static', 'live'}:
        raise ValueError('pending_orders.source_mode must be static or live')
    if args.replay_csv is None and source_mode == 'live':
        local_date = datetime.now(ZoneInfo('America/Chicago')).date()
        if args.as_of != local_date:
            raise RuntimeError(
                f'Live pending orders cannot be backfilled: as_of={args.as_of}; today={local_date}'
            )
    captured_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    inputs = [config_path, Path(__file__).resolve()]
    source_as_of = args.as_of
    pending_order_coverage = True
    coverage_reason = 'current_open_orders_captured'
    account = ''
    if args.replay_csv is not None:
        replay_path = ensure_not_prod_path(
            args.replay_csv.resolve(), label='IB pending-order replay CSV'
        )
        raw_rows = _replay_rows(replay_path)
        accounts = sorted({_text(row.get('account')) for row in raw_rows if _text(row.get('account'))})
        if len(accounts) != 1:
            raise ValueError('Replay pending orders require exactly one account')
        account = accounts[0]
        capture_mode = 'sealed_replay'
        order_source = 'ibkr_sealed_open_orders_replay'
        inputs.append(replay_path)
    elif source_mode == 'static':
        static_cfg = policy.get('static', {})
        if not isinstance(static_cfg, dict):
            raise ValueError('pending_orders.static must be a mapping')
        source_dir = ensure_not_prod_path(
            resolve_path(
                static_cfg.get('source_reports_dir', '../IB_reports'),
                base_dir=config_path.parent,
            ),
            label='IB static report directory',
        )
        statement_path, source_as_of = _select_static_statement(
            source_dir=source_dir,
            statement_glob=_text(static_cfg.get('statement_glob')) or 'U*.csv',
            as_of=args.as_of,
            max_stale_days=int(static_cfg.get('max_stale_days', 10)),
        )
        raw_rows = []
        capture_mode = 'static_activity_statement'
        order_source = 'ibkr_static_activity_statement_no_order_coverage'
        pending_order_coverage = False
        coverage_reason = (
            'IB Activity Statements provide holdings and completed trades but do not provide '
            'a current open-order contract'
        )
        inputs.append(statement_path)
    else:
        live_cfg = policy.get('live', {})
        if not isinstance(live_cfg, dict):
            raise ValueError('pending_orders.live must be a mapping')
        configured_account = _text(
            os.environ.get('IBKR_MONITOR_ACCOUNT') or live_cfg.get('account')
        )
        raw_rows, account = _live_rows(
            host=_text(live_cfg.get('host')) or '127.0.0.1',
            port=int(live_cfg.get('port', 7496)),
            client_id=int(
                args.client_id
                if args.client_id is not None
                else live_cfg.get('client_id', 52)
            ),
            timeout_sec=float(live_cfg.get('timeout_sec', 20.0)),
            configured_account=configured_account,
            require_single_account=bool(
                live_cfg.get('require_single_managed_account', True)
            ),
        )
        capture_mode = 'live_read_only'
        order_source = 'ibkr_read_only_open_orders'
    rows = normalize_orders(
        raw_rows,
        as_of=args.as_of,
        captured_at_utc=captured_at,
        account=account,
        source=order_source,
    )
    output_dir = ensure_not_prod_path(
        args.output_dir.resolve()
        if args.output_dir is not None
        else paths.output_dir
        / 'runs'
        / args.as_of.isoformat()
        / monitor_output_subdir(config)
        / 'pending_orders',
        label='pending-order output directory',
    )
    csv_path = output_dir / 'ib_pending_orders.csv'
    manifest_path = output_dir / 'ib_pending_orders_manifest.json'
    fail_if_exists([csv_path, manifest_path], force=args.force)
    write_csv(csv_path, FIELDS, rows)
    acceptance = 'PASS' if pending_order_coverage else 'PASS_WITH_DEFERRED'
    write_manifest(
        manifest_path,
        {
            'schema_version': 'ib_pending_orders_manifest_v2',
            'acceptance': acceptance,
            'as_of_date': args.as_of.isoformat(),
            'source_as_of_date': source_as_of.isoformat(),
            'captured_at_utc': captured_at,
            'source_mode': 'replay' if args.replay_csv is not None else source_mode,
            'capture_mode': capture_mode,
            'ib_connection_used': capture_mode == 'live_read_only',
            'read_only_connection': capture_mode == 'live_read_only',
            'broker_execution_prohibited': True,
            'pending_order_coverage': pending_order_coverage,
            'coverage_reason': coverage_reason,
            'order_count': len(rows),
            'ticker_count': len({str(row['ticker']) for row in rows}),
            'account_id_sha256': _account_hash(account),
            'inputs_sha256': {str(path): sha256_file(path) for path in inputs},
            'outputs_sha256': {csv_path.name: sha256_file(csv_path)},
        },
    )
    print(f'IB PENDING ORDERS: {acceptance}')
    print(
        f'mode={capture_mode}; coverage={pending_order_coverage}; orders={len(rows)}; '
        f'tickers={len({str(row["ticker"]) for row in rows})}'
    )
    print(f'output={csv_path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

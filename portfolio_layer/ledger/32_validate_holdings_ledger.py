#!/usr/bin/env python3
"""Stage 8.5 - validate and seal the holdings ledger."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.db import connect, table_exists  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_database_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.ledger.ledger_common import RECONCILIATION_FIELDS, csv_trade_key, parse_number  # noqa: E402
from portfolio_layer.ledger.storage import count_for_run, count_for_source, init_ledger_tables  # noqa: E402
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("validate_holdings_ledger")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SOURCE_FILES = [
    "ledger_common.py",
    "storage.py",
    "30_import_ib_activity_statement.py",
    "31_build_holdings_ledger.py",
    "32_validate_holdings_ledger.py",
]


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Validate and seal the holdings ledger.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, default=None)
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def _f(raw: Any) -> float:
    value = parse_number(raw)
    return 0.0 if value is None else float(value)


def _manual_override_symbols(config: dict[str, Any]) -> set[str]:
    raw = cfg_get(config, "holdings_ledger.manual_lot_overrides", {}) or {}
    return {str(symbol).strip().upper() for symbol in raw} if isinstance(raw, dict) else set()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:  # noqa: C901
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    db_path = resolve_database_path(paths, args.db)
    runs_root = paths.output_dir / "runs"
    run_as_of = args.as_of or latest_run_with(runs_root, "ledger/ledger_build_meta.json")
    if not run_as_of:
        LOGGER.error("No built holdings-ledger run found under %s", runs_root)
        return 1
    ledger_dir = runs_root / run_as_of / "ledger"
    art = {
        "ib_statement_meta": ledger_dir / "ib_statement_meta.json",
        "broker_statement_sources": ledger_dir / "broker_statement_sources.csv",
        "broker_open_positions": ledger_dir / "broker_open_positions.csv",
        "broker_net_stock_positions": ledger_dir / "broker_net_stock_positions.csv",
        "broker_trades": ledger_dir / "broker_trades.csv",
        "broker_instruments": ledger_dir / "broker_instruments.csv",
        "broker_cash_report": ledger_dir / "broker_cash_report.csv",
        "broker_dividends": ledger_dir / "broker_dividends.csv",
        "broker_cash_transactions": ledger_dir / "broker_cash_transactions.csv",
        "broker_fees": ledger_dir / "broker_fees.csv",
        "broker_securities_lending": ledger_dir / "broker_securities_lending.csv",
        "holding_lots": ledger_dir / "holding_lots.csv",
        "holding_state": ledger_dir / "holding_state.csv",
        "ledger_reconciliation": ledger_dir / "ledger_reconciliation.csv",
        "ledger_build_meta": ledger_dir / "ledger_build_meta.json",
    }
    missing = [name for name, path in art.items() if not path.exists()]
    if missing:
        LOGGER.error("Run 30 + 31 first; missing %s", missing)
        return 1

    validation_path = ledger_dir / "validation" / "ledger_validation.csv"
    manifest_path = ledger_dir / "ledger_manifest.json"
    if args.force:
        for path in (validation_path, manifest_path):
            if path.exists():
                path.unlink()
    try:
        fail_if_exists([validation_path, manifest_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    checks: list[dict[str, str]] = []

    def rec(check: str, status: str, detail: str) -> None:
        checks.append({"run_as_of": run_as_of, "check": check, "status": status, "detail": detail})

    meta30 = _load_json(art["ib_statement_meta"])
    meta31 = _load_json(art["ledger_build_meta"])
    source = meta30.get("raw_source") or {}
    source_sha = str(source.get("sha256", ""))
    source_path = Path(str(source.get("path", "")))

    if meta30.get("acceptance") != "PASS":
        rec("stage30_import_passed", "FAIL", f"acceptance={meta30.get('acceptance')}")
    else:
        rec("stage30_import_passed", "PASS", "IB import accepted")
    if meta31.get("acceptance") != "PASS":
        rec("stage31_build_passed", "FAIL", f"acceptance={meta31.get('acceptance')}")
    else:
        rec("stage31_build_passed", "PASS", "ledger build accepted")

    if source_path.exists():
        current_hash = sha256_file(source_path)
        rec("raw_ib_csv_hash_current", "PASS" if current_hash == source_sha else "FAIL",
            "raw IB CSV hash matches sealed import" if current_hash == source_sha else f"{current_hash}!={source_sha}")
    else:
        rec("raw_ib_csv_hash_current", "FAIL", f"raw IB CSV missing: {source_path}")

    stale_inputs: list[str] = []
    for name, recorded in (meta31.get("inputs_sha256") or {}).items():
        path_text = (meta31.get("input_paths") or {}).get(name)
        if not path_text:
            stale_inputs.append(f"{name}:missing_path")
            continue
        path = Path(path_text)
        if not path.exists():
            stale_inputs.append(f"{name}:missing")
            continue
        if sha256_file(path) != recorded:
            stale_inputs.append(f"{name}:hash")
    rec("build_inputs_current", "PASS" if not stale_inputs else "FAIL",
        "Stage 31 input hashes current" if not stale_inputs else "; ".join(stale_inputs[:8]))

    stale_outputs: list[str] = []
    for filename, recorded in (meta31.get("outputs_sha256") or {}).items():
        path = ledger_dir / filename
        if not path.exists():
            stale_outputs.append(f"{filename}:missing")
            continue
        if sha256_file(path) != recorded:
            stale_outputs.append(f"{filename}:hash")
    rec("build_outputs_current", "PASS" if not stale_outputs else "FAIL",
        "Stage 31 output hashes current" if not stale_outputs else "; ".join(stale_outputs[:8]))

    reconciliation = read_csv(art["ledger_reconciliation"])
    hard_fail_recs = [r for r in reconciliation if r.get("status") == "FAIL"]
    rec("reconciliation_checks_pass", "PASS" if not hard_fail_recs else "FAIL",
        f"{len(reconciliation)} reconciliation checks, no hard fails" if not hard_fail_recs else "; ".join(
            f"{r.get('check')}={r.get('detail')}" for r in hard_fail_recs[:6]
        ))

    lots = read_csv(art["holding_lots"])
    state = read_csv(art["holding_state"])
    open_positions = read_csv(art["broker_open_positions"])
    net_stock = read_csv(art["broker_net_stock_positions"])
    trades = read_csv(art["broker_trades"])
    instruments = read_csv(art["broker_instruments"])

    stock_state = {r["symbol"].upper(): r for r in state if r.get("asset_category") == "Stocks"}
    lots_by_symbol: dict[str, list[dict[str, str]]] = {}
    for lot in lots:
        if lot.get("asset_category") == "Stocks":
            lots_by_symbol.setdefault(lot.get("symbol", "").upper(), []).append(lot)

    lot_bad: list[str] = []
    for symbol, holding in sorted(stock_state.items()):
        lot_qty = sum(_f(lot.get("quantity")) for lot in lots_by_symbol.get(symbol, []))
        lot_basis = sum(_f(lot.get("cost_basis")) for lot in lots_by_symbol.get(symbol, []))
        hold_qty = _f(holding.get("quantity"))
        hold_basis = _f(holding.get("cost_basis"))
        if abs(lot_qty - hold_qty) > 1e-6:
            lot_bad.append(f"{symbol}:qty {lot_qty:.6g}!={hold_qty:.6g}")
        if abs(lot_basis - hold_basis) > 0.05:
            lot_bad.append(f"{symbol}:basis {lot_basis:.2f}!={hold_basis:.2f}")
    rec("holding_lots_match_current_stock_state", "PASS" if not lot_bad else "FAIL",
        f"{len(stock_state)} stock positions reconcile to lots" if not lot_bad else "; ".join(lot_bad[:8]))

    override_symbols = _manual_override_symbols(config)
    override_bad: list[str] = []
    for symbol in override_symbols:
        matched = [
            lot for lot in lots_by_symbol.get(symbol, [])
            if "manual_entry_date" in lot.get("provenance", "") and lot.get("entry_date")
        ]
        if not matched:
            override_bad.append(symbol)
    rec("manual_lot_overrides_materialized", "PASS" if not override_bad else "FAIL",
        f"manual overrides materialized: {sorted(override_symbols)}" if not override_bad else f"missing={override_bad}")

    stock_trade_keys = [r.get("trade_key", "") for r in trades if r.get("trade_key")]
    duplicate_trade_keys = len(stock_trade_keys) - len(set(stock_trade_keys))
    rec("trade_keys_unique", "PASS" if duplicate_trade_keys == 0 else "FAIL",
        f"trade_rows={len(trades)} unique_keys={len(set(stock_trade_keys))}")

    key_guard_bad: list[str] = []
    if trades:
        sample = dict(trades[0])
        recomputed = csv_trade_key(source_sha, sample)
        if recomputed != sample.get("trade_key", ""):
            key_guard_bad.append("stored_key_not_recomputed")
        shifted = dict(sample)
        shifted_source_row = str(int(_f(sample.get("source_row"))) + 1_000_000)
        shifted["source_row"] = shifted_source_row
        if csv_trade_key(source_sha, shifted) == recomputed:
            key_guard_bad.append("source_row_not_in_key")
    else:
        key_guard_bad.append("no_trades")
    rec("trade_key_source_row_collision_guard", "PASS" if not key_guard_bad else "FAIL",
        "trade_key recomputes and changes when source_row changes" if not key_guard_bad else "; ".join(key_guard_bad))

    instrument_assets = {str(r.get("asset_category") or "") for r in instruments}
    rec("instrument_metadata_loaded", "PASS" if {"Stocks", "Equity and Index Options"}.issubset(instrument_assets) else "FAIL",
        f"instrument_rows={len(instruments)} asset_categories={sorted(instrument_assets)}")

    lending_bad: list[str] = []
    for row in net_stock:
        symbol = row.get("symbol", "").upper()
        lent = _f(row.get("shares_lent"))
        if abs(lent) <= 1e-9:
            continue
        shares_at_ib = _f(row.get("shares_at_ib"))
        state_qty = _f(stock_state.get(symbol, {}).get("quantity"))
        if abs(state_qty - shares_at_ib) > 1e-6:
            lending_bad.append(f"{symbol}:state={state_qty:.6g},shares_at_ib={shares_at_ib:.6g},lent={lent:.6g}")
    rec("lent_shares_do_not_reduce_exposure", "PASS" if not lending_bad else "FAIL",
        "lending stored separately; holding exposure uses owned shares" if not lending_bad else "; ".join(lending_bad[:8]))

    timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))
    required_tables = [
        "broker_statement_sources", "broker_open_positions", "broker_net_stock_positions", "broker_trades",
        "broker_instruments", "broker_cash_report", "broker_dividends", "broker_cash_transactions",
        "broker_fees", "broker_securities_lending", "holdings_lots", "holding_state", "broker_reconciliations",
    ]
    with connect(db_path, timeout_sec=timeout_sec) as conn:
        init_ledger_tables(conn)
        missing_tables = [table for table in required_tables if not table_exists(conn, table)]
        db_bad: list[str] = []
        if missing_tables:
            db_bad.extend(f"missing_table:{table}" for table in missing_tables)
        expected_source_counts = {
            "broker_open_positions": len(open_positions),
            "broker_net_stock_positions": len(net_stock),
            "broker_trades": len(trades),
            "broker_instruments": len(instruments),
            "broker_cash_report": len(read_csv(art["broker_cash_report"])),
            "broker_dividends": len(read_csv(art["broker_dividends"])),
            "broker_cash_transactions": len(read_csv(art["broker_cash_transactions"])),
            "broker_fees": len(read_csv(art["broker_fees"])),
            "broker_securities_lending": len(read_csv(art["broker_securities_lending"])),
        }
        for table, expected in expected_source_counts.items():
            actual = count_for_source(conn, table, source_sha)
            if actual != expected:
                db_bad.append(f"{table}:{actual}!={expected}")
        for table, expected in {"holdings_lots": len(lots), "holding_state": len(state)}.items():
            actual = count_for_run(conn, table, run_as_of)
            if actual != expected:
                db_bad.append(f"{table}:{actual}!={expected}")
    rec("sqlite_loaded_and_idempotent_counts", "PASS" if not db_bad else "FAIL",
        "SQLite row counts match sealed CSV artifacts" if not db_bad else "; ".join(db_bad[:8]))

    validation_path.parent.mkdir(parents=True, exist_ok=True)
    write_csv(validation_path, RECONCILIATION_FIELDS, checks)
    passed = all(row["status"] in {"PASS", "WARN"} for row in checks)

    provenance = {name: path for name, path in art.items()}
    provenance["validation/ledger_validation.csv"] = validation_path
    provenance["config.yaml"] = config_path
    manifest = {
        "stage": "stage8_5_holdings_ledger",
        "run_as_of": run_as_of,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "acceptance": "PASS" if passed else "FAIL",
        "shadow_only": True,
        "enabled_in_production": bool(cfg_get(config, "holdings_ledger.enabled_in_production", False)),
        "db_path": str(db_path),
        "raw_source": source,
        "row_counts": {
            "open_positions": len(open_positions),
            "trades": len(trades),
            "instruments": len(instruments),
            "holding_lots": len(lots),
            "holding_state": len(state),
            "reconciliation_checks": len(checks),
        },
        "checks": checks,
        "provenance_sha256": {name: sha256_file(path) for name, path in provenance.items() if path.exists()},
        "source_sha256": {
            name: sha256_file(PACKAGE_ROOT / "ledger" / name)
            for name in SOURCE_FILES
            if (PACKAGE_ROOT / "ledger" / name).exists()
        },
    }
    write_manifest(manifest_path, manifest)

    for row in checks:
        LOGGER.info("[%s] %s -- %s", row["status"], row["check"], row["detail"])
    if passed:
        LOGGER.info("STAGE 8.5 LEDGER ACCEPTANCE: PASS (as_of=%s, holdings=%d, lots=%d)", run_as_of, len(state), len(lots))
        return 0
    LOGGER.error("STAGE 8.5 LEDGER ACCEPTANCE: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

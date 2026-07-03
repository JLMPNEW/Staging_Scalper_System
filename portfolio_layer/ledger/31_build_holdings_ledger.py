#!/usr/bin/env python3
"""Stage 8.5 - build/load the portfolio-owned holdings ledger from normalized IB artifacts."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.db import connect, init_db  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_database_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.ledger.ledger_common import (  # noqa: E402
    HOLDING_LOT_FIELDS,
    HOLDING_STATE_FIELDS,
    RECONCILIATION_FIELDS,
    fmt_number,
    parse_number,
)
from portfolio_layer.ledger.storage import (  # noqa: E402
    init_ledger_tables,
    replace_reconciliations,
    replace_run_rows,
    replace_source_rows,
    replace_statement_source,
)
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("build_holdings_ledger")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SOURCE_FILES = ["ledger_common.py", "storage.py", "30_import_ib_activity_statement.py", "31_build_holdings_ledger.py"]


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build/load the holdings ledger from normalized IB artifacts.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, default=None)
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def _f(raw: Any) -> float:
    value = parse_number(raw)
    return 0.0 if value is None else float(value)


def _manual_overrides(config: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    raw = cfg_get(config, "holdings_ledger.manual_lot_overrides", {}) or {}
    out: dict[str, list[dict[str, Any]]] = {}
    if isinstance(raw, dict):
        for symbol, payload in raw.items():
            rows = payload if isinstance(payload, list) else [payload]
            out[str(symbol).strip().upper()] = [dict(row) for row in rows if isinstance(row, dict)]
    return out


def _open_stock_positions(open_positions: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {
        str(row.get("symbol", "")).strip().upper(): row
        for row in open_positions
        if row.get("asset_category") == "Stocks" and str(row.get("symbol", "")).strip()
    }


def _open_option_positions(open_positions: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {
        str(row.get("symbol", "")).strip().upper(): row
        for row in open_positions
        if row.get("asset_category") == "Equity and Index Options" and str(row.get("symbol", "")).strip()
    }


def _trade_sort_key(row: dict[str, str]) -> tuple[str, int]:
    return (row.get("date_time", ""), int(_f(row.get("source_row"))))


def _previous_ledger_inputs(runs_root: Path, run_as_of: str) -> tuple[str | None, Path | None, Path | None]:
    cutoff = date.fromisoformat(run_as_of)
    candidates: list[tuple[date, Path, Path]] = []
    children = runs_root.iterdir() if runs_root.exists() else []
    for child in children:
        if not child.is_dir():
            continue
        try:
            child_date = date.fromisoformat(child.name)
        except ValueError:
            continue
        if child_date >= cutoff:
            continue
        ledger_dir = child / "ledger"
        manifest_path = ledger_dir / "ledger_manifest.json"
        lots_path = ledger_dir / "holding_lots.csv"
        if not manifest_path.exists() or not lots_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if manifest.get("acceptance") != "PASS":
            continue
        candidates.append((child_date, manifest_path, lots_path))
    if not candidates:
        return None, None, None
    previous_date, manifest_path, lots_path = max(candidates, key=lambda item: item[0])
    return previous_date.isoformat(), manifest_path, lots_path


def _seed_lots_from_previous(
    prior_lots: list[dict[str, str]],
    *,
    asset_category: str,
    tracked_symbols: set[str],
    prior_as_of: str | None,
) -> dict[str, list[dict[str, Any]]]:
    seeded: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in prior_lots:
        if row.get("asset_category") != asset_category:
            continue
        symbol = str(row.get("symbol", "")).strip().upper()
        if not symbol or symbol not in tracked_symbols:
            continue
        qty = _f(row.get("quantity"))
        if abs(qty) <= 1e-12:
            continue
        basis = _f(row.get("cost_basis"))
        provenance = str(row.get("provenance", ""))
        carry_provenance = (
            f"carried_forward_from_ledger={prior_as_of};"
            f"prior_lot_id={row.get('lot_id', '')};"
            f"prior_source={row.get('source', '')};"
            f"prior_source_sha256={row.get('source_sha256', '')}"
        )
        if provenance:
            carry_provenance += f";prior_provenance={provenance}"
        seeded[symbol].append({
            "lot_id": str(row.get("lot_id", "")),
            "quantity": qty,
            "entry_date": str(row.get("entry_date", "")),
            "cost_basis": basis,
            "entry_date_unknown": int(_f(row.get("entry_date_unknown"))),
            "source": str(row.get("source", "")) or "carried_forward_lot",
            "provenance": carry_provenance,
        })
    return seeded


def _stock_trade_lots(
    trades: list[dict[str, str]],
    *,
    tracked_symbols: set[str],
    seed_lots: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], list[str], list[str]]:
    lots: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for symbol, seed_rows in (seed_lots or {}).items():
        lots[symbol].extend(dict(row) for row in seed_rows)
    issues: list[str] = []
    ignored_closed: list[str] = []
    closed_statement_qty: dict[str, float] = defaultdict(float)
    stock_trades = sorted((r for r in trades if r.get("asset_category") == "Stocks"), key=_trade_sort_key)
    for row in stock_trades:
        symbol = str(row.get("symbol", "")).strip().upper()
        qty = _f(row.get("quantity"))
        if not symbol or abs(qty) < 1e-12:
            continue
        if symbol not in tracked_symbols:
            if qty > 0:
                closed_statement_qty[symbol] += qty
                continue
            close_qty = -qty
            available = closed_statement_qty.get(symbol, 0.0)
            used = min(available, close_qty)
            closed_statement_qty[symbol] = available - used
            close_qty -= used
            if close_qty > 1e-6:
                ignored_closed.append(f"{symbol}:{close_qty:.6g}")
            continue
        if qty > 0:
            basis = _f(row.get("basis"))
            entry_date = str(row.get("trade_date", "")).strip()
            lots[symbol].append({
                "lot_id": f"{symbol}-{entry_date}-{row.get('source_row')}",
                "quantity": qty,
                "entry_date": entry_date,
                "cost_basis": basis,
                "entry_date_unknown": 0,
                "source": "ib_trade",
                "provenance": f"trade_key={row.get('trade_key', '')};source_row={row.get('source_row', '')}",
            })
            continue
        close_qty = -qty
        for lot in lots[symbol]:
            if close_qty <= 1e-9:
                break
            lot_qty = float(lot["quantity"])
            if lot_qty <= 1e-9:
                continue
            used = min(lot_qty, close_qty)
            ratio = used / lot_qty if lot_qty else 0.0
            lot["quantity"] = lot_qty - used
            lot["cost_basis"] = float(lot["cost_basis"]) * (1.0 - ratio)
            close_qty -= used
        if close_qty > 1e-6:
            issues.append(f"{symbol}:sell_exceeds_report_lots:{close_qty:.6f}")
    return lots, issues, ignored_closed


def _find_manual_override(
    overrides: dict[str, list[dict[str, Any]]],
    *,
    symbol: str,
    quantity: float,
    asset_category: str = "Stocks",
) -> dict[str, Any] | None:
    candidates = overrides.get(symbol.upper(), [])
    for row in candidates:
        row_asset = str(row.get("asset_category", asset_category)).strip() or asset_category
        row_qty = parse_number(row.get("quantity"))
        if row_asset == asset_category and row_qty is not None and abs(float(row_qty) - quantity) <= 1e-6:
            return row
    return None


def _build_stock_holding_lots(
    *,
    run_as_of: str,
    source_sha: str,
    open_stocks: dict[str, dict[str, str]],
    trades: list[dict[str, str]],
    overrides: dict[str, list[dict[str, Any]]],
    prior_lots: list[dict[str, str]],
    prior_as_of: str | None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    seed_lots = _seed_lots_from_previous(
        prior_lots,
        asset_category="Stocks",
        tracked_symbols=set(open_stocks),
        prior_as_of=prior_as_of,
    )
    lots_by_symbol, trade_issues, ignored_closed = _stock_trade_lots(
        trades,
        tracked_symbols=set(open_stocks),
        seed_lots=seed_lots,
    )
    lots_out: list[dict[str, str]] = []
    recs: list[dict[str, str]] = []
    pre_report: list[str] = []
    basis_bad: list[str] = []
    qty_bad: list[str] = []
    manual_used: list[str] = []

    for symbol, pos in sorted(open_stocks.items()):
        current_qty = _f(pos.get("quantity"))
        target_basis = _f(pos.get("cost_basis"))
        live_lots = [lot for lot in lots_by_symbol.get(symbol, []) if float(lot["quantity"]) > 1e-8]
        lot_qty = sum(float(lot["quantity"]) for lot in live_lots)
        lot_basis = sum(float(lot["cost_basis"]) for lot in live_lots)
        missing_qty = current_qty - lot_qty
        if missing_qty > 1e-6:
            inferred_basis = target_basis - lot_basis
            override = _find_manual_override(overrides, symbol=symbol, quantity=missing_qty)
            entry_date = str((override or {}).get("entry_date", "")).strip()
            entry_unknown = 0 if entry_date else 1
            provenance = "pre_report_lot_inferred_from_ib_snapshot"
            if override:
                provenance += f";manual_entry_date={entry_date};reason={override.get('reason', '')}"
                manual_used.append(f"{symbol}:{missing_qty:.6g}@{entry_date}")
            else:
                pre_report.append(f"{symbol}:{missing_qty:.6g}:entry_unknown")
            live_lots.insert(0, {
                "lot_id": f"{symbol}-PRE-REPORT-1",
                "quantity": missing_qty,
                "entry_date": entry_date,
                "cost_basis": inferred_basis,
                "entry_date_unknown": entry_unknown,
                "source": "ib_snapshot_inferred_pre_report",
                "provenance": provenance,
            })
        elif missing_qty < -1e-6:
            qty_bad.append(f"{symbol}:lots>{current_qty:.6g} by {-missing_qty:.6g}")

        # Reconcile basis exactly to IB's current aggregate where possible.
        live_lots = [lot for lot in live_lots if float(lot["quantity"]) > 1e-8]
        lot_qty = sum(float(lot["quantity"]) for lot in live_lots)
        lot_basis = sum(float(lot["cost_basis"]) for lot in live_lots)
        basis_delta = target_basis - lot_basis
        if live_lots and abs(basis_delta) > 1e-6:
            # Preserve IB as source of truth for aggregate basis; attach any rounding/inference delta to the last lot.
            live_lots[-1]["cost_basis"] = float(live_lots[-1]["cost_basis"]) + basis_delta
            lot_basis += basis_delta
        if abs(lot_qty - current_qty) > 1e-6:
            qty_bad.append(f"{symbol}:lot_qty={lot_qty:.6g},open={current_qty:.6g}")
        if abs(lot_basis - target_basis) > 0.05:
            basis_bad.append(f"{symbol}:lot_basis={lot_basis:.2f},open_basis={target_basis:.2f}")

        for idx, lot in enumerate(live_lots, start=1):
            qty = float(lot["quantity"])
            basis = float(lot["cost_basis"])
            cost_price = basis / qty if abs(qty) > 1e-12 else 0.0
            lots_out.append({
                "run_as_of": run_as_of,
                "asset_category": "Stocks",
                "symbol": symbol,
                "lot_id": str(lot.get("lot_id") or f"{symbol}-LOT-{idx}"),
                "quantity": fmt_number(qty),
                "entry_date": str(lot.get("entry_date", "")),
                "cost_basis": fmt_number(basis),
                "cost_price": fmt_number(cost_price),
                "entry_date_unknown": str(int(lot.get("entry_date_unknown", 0))),
                "source": str(lot.get("source", "")),
                "provenance": str(lot.get("provenance", "")),
                "source_sha256": source_sha,
            })

    def rec(check: str, status: str, detail: str) -> None:
        recs.append({"run_as_of": run_as_of, "check": check, "status": status, "detail": detail})

    rec("stock_trade_lot_reconstruction", "PASS" if not trade_issues else "FAIL",
        "stock trades reconstruct FIFO lots" if not trade_issues else "; ".join(trade_issues[:8]))
    rec("closed_position_pre_report_history", "PASS" if not ignored_closed else "WARN",
        "no closed-position pre-report lot history needed" if not ignored_closed else "; ".join(ignored_closed[:8]))
    rec("stock_lot_quantity_reconciles", "PASS" if not qty_bad else "FAIL",
        "stock lot quantities equal IB open quantities" if not qty_bad else "; ".join(qty_bad[:8]))
    rec("stock_lot_basis_reconciles", "PASS" if not basis_bad else "FAIL",
        "stock lot basis equals IB aggregate cost basis" if not basis_bad else "; ".join(basis_bad[:8]))
    rec("pre_report_lots", "PASS" if not pre_report else "WARN",
        "no unknown pre-report stock lots" if not pre_report else "; ".join(pre_report[:8]))
    rec("manual_lot_overrides_applied", "PASS" if manual_used else "WARN",
        "; ".join(manual_used) if manual_used else "no manual lot overrides were required/applied")
    carried = sum(len(rows) for rows in seed_lots.values())
    rec("prior_stock_lots_carried_forward", "PASS" if carried else "WARN",
        f"carried_lots={carried} from prior_as_of={prior_as_of}" if carried else "no prior stock lots carried forward")
    return lots_out, recs


def _build_option_lots(
    run_as_of: str,
    source_sha: str,
    open_options: dict[str, dict[str, str]],
    trades: list[dict[str, str]],
    prior_lots: list[dict[str, str]],
    prior_as_of: str | None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    qty_by_symbol: dict[str, float] = defaultdict(float)
    first_date: dict[str, str] = {}
    for row in sorted((r for r in trades if r.get("asset_category") == "Equity and Index Options"), key=_trade_sort_key):
        symbol = str(row.get("symbol", "")).strip().upper()
        qty = _f(row.get("quantity"))
        if not symbol:
            continue
        qty_by_symbol[symbol] += qty
        first_date.setdefault(symbol, str(row.get("trade_date", "")))
    prior_options = _seed_lots_from_previous(
        prior_lots,
        asset_category="Equity and Index Options",
        tracked_symbols=set(open_options),
        prior_as_of=prior_as_of,
    )

    lots: list[dict[str, str]] = []
    mismatches: list[str] = []
    for symbol, pos in sorted(open_options.items()):
        qty = _f(pos.get("quantity"))
        prior_rows = prior_options.get(symbol, [])
        prior_qty = sum(float(row["quantity"]) for row in prior_rows)
        expected_qty = prior_qty + qty_by_symbol.get(symbol, 0.0)
        if prior_rows:
            entry_date = str(prior_rows[0].get("entry_date", ""))
            entry_unknown = int(prior_rows[0].get("entry_date_unknown", 0))
            provenance = f"option lot carried forward from prior_as_of={prior_as_of}; current aggregate from IB open position"
            if abs(expected_qty - qty) > 1e-6:
                mismatches.append(f"{symbol}:prior_plus_trades={expected_qty:.6g},open={qty:.6g}")
        else:
            entry_date = first_date.get(symbol, "")
            entry_unknown = 0 if entry_date else 1
            provenance = "option aggregate lot from IB open position; trade history used for quantity check"
        if not prior_rows and abs(qty_by_symbol.get(symbol, 0.0) - qty) > 1e-6:
            mismatches.append(f"{symbol}:trades={qty_by_symbol.get(symbol, 0.0):.6g},open={qty:.6g}")
        basis = _f(pos.get("cost_basis"))
        cost_price = basis / qty if abs(qty) > 1e-12 else 0.0
        lots.append({
            "run_as_of": run_as_of,
            "asset_category": "Equity and Index Options",
            "symbol": symbol,
            "lot_id": f"{symbol}-OPEN-OPTION",
            "quantity": fmt_number(qty),
            "entry_date": entry_date,
            "cost_basis": fmt_number(basis),
            "cost_price": fmt_number(cost_price),
            "entry_date_unknown": str(entry_unknown),
            "source": "ib_option_open_position",
            "provenance": provenance,
            "source_sha256": source_sha,
        })
    rec = {
        "run_as_of": run_as_of,
        "check": "option_quantity_reconciles",
        "status": "PASS" if not mismatches else "FAIL",
        "detail": "open options match prior ledger plus period option trades" if not mismatches else "; ".join(mismatches[:8]),
    }
    return lots, [rec]


def _holding_state(run_as_of: str, source_sha: str, open_positions: list[dict[str, str]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for row in open_positions:
        if row.get("asset_category") not in {"Stocks", "Equity and Index Options"}:
            continue
        rows.append({
            "run_as_of": run_as_of,
            "asset_category": row.get("asset_category", ""),
            "currency": row.get("currency", ""),
            "symbol": str(row.get("symbol", "")).strip().upper(),
            "quantity": row.get("quantity", ""),
            "multiplier": row.get("multiplier", ""),
            "cost_price": row.get("cost_price", ""),
            "cost_basis": row.get("cost_basis", ""),
            "close_price": row.get("close_price", ""),
            "market_value": row.get("market_value", ""),
            "unrealized_pl": row.get("unrealized_pl", ""),
            "source_sha256": source_sha,
        })
    return rows


def _additional_reconciliations(
    *,
    run_as_of: str,
    open_positions: list[dict[str, str]],
    net_stock_positions: list[dict[str, str]],
    trades: list[dict[str, str]],
    instruments: list[dict[str, str]],
    cash_report: list[dict[str, str]],
    securities_lending: list[dict[str, str]],
) -> list[dict[str, str]]:
    recs: list[dict[str, str]] = []

    def rec(check: str, status: str, detail: str) -> None:
        recs.append({"run_as_of": run_as_of, "check": check, "status": status, "detail": detail})

    stock_pos = [r for r in open_positions if r.get("asset_category") == "Stocks"]
    option_pos = [r for r in open_positions if r.get("asset_category") == "Equity and Index Options"]
    rec("open_positions_present", "PASS" if stock_pos else "FAIL", f"stocks={len(stock_pos)} options={len(option_pos)}")
    rec("trade_history_present", "PASS" if trades else "FAIL", f"trade_rows={len(trades)}")
    rec("instrument_metadata_present", "PASS" if instruments else "FAIL", f"instrument_rows={len(instruments)}")
    rec("cash_report_present", "PASS" if cash_report else "FAIL", f"cash_report_rows={len(cash_report)}")

    open_by_symbol = {str(r.get("symbol", "")).strip().upper(): r for r in stock_pos}
    lending_details = [r for r in securities_lending if str(r.get("symbol", "")).strip()]
    lending_bad: list[str] = []
    for row in net_stock_positions:
        symbol = str(row.get("symbol", "")).strip().upper()
        lent = _f(row.get("shares_lent"))
        if abs(lent) <= 1e-9:
            continue
        shares_at_ib = _f(row.get("shares_at_ib"))
        open_qty = _f(open_by_symbol.get(symbol, {}).get("quantity"))
        if abs(open_qty - shares_at_ib) > 1e-6:
            lending_bad.append(f"{symbol}:open={open_qty:.6g},shares_at_ib={shares_at_ib:.6g},lent={lent:.6g}")
    if net_stock_positions:
        rec("securities_lending_uses_owned_shares", "PASS" if not lending_bad else "FAIL",
            "open exposure uses Shares at IB; lending tracked separately" if not lending_bad else "; ".join(lending_bad[:8]))
    else:
        rec("securities_lending_uses_owned_shares", "WARN", "Net Stock Position Summary absent; cannot check lent share exposure")
    rec("securities_lending_detail_present", "PASS" if lending_details else "WARN",
        f"lending_detail_rows={len(lending_details)}")
    return recs


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    db_path = resolve_database_path(paths, args.db)
    runs_root = paths.output_dir / "runs"
    run_as_of = args.as_of or latest_run_with(runs_root, "ledger/ib_statement_meta.json")
    if not run_as_of:
        LOGGER.error("No imported IB ledger run found under %s", runs_root)
        return 1
    ledger_dir = runs_root / run_as_of / "ledger"
    input_paths = {
        "ib_statement_meta.json": ledger_dir / "ib_statement_meta.json",
        "broker_statement_sources.csv": ledger_dir / "broker_statement_sources.csv",
        "broker_open_positions.csv": ledger_dir / "broker_open_positions.csv",
        "broker_net_stock_positions.csv": ledger_dir / "broker_net_stock_positions.csv",
        "broker_trades.csv": ledger_dir / "broker_trades.csv",
        "broker_instruments.csv": ledger_dir / "broker_instruments.csv",
        "broker_cash_report.csv": ledger_dir / "broker_cash_report.csv",
        "broker_dividends.csv": ledger_dir / "broker_dividends.csv",
        "broker_cash_transactions.csv": ledger_dir / "broker_cash_transactions.csv",
        "broker_fees.csv": ledger_dir / "broker_fees.csv",
        "broker_securities_lending.csv": ledger_dir / "broker_securities_lending.csv",
    }
    missing = [name for name, path in input_paths.items() if not path.exists()]
    if missing:
        LOGGER.error("Run 30 first; missing %s", missing)
        return 1

    out_paths = {
        "holding_lots.csv": ledger_dir / "holding_lots.csv",
        "holding_state.csv": ledger_dir / "holding_state.csv",
        "ledger_reconciliation.csv": ledger_dir / "ledger_reconciliation.csv",
        "ledger_build_meta.json": ledger_dir / "ledger_build_meta.json",
    }
    if args.force:
        for path in out_paths.values():
            if path.exists():
                path.unlink()
    try:
        fail_if_exists(out_paths.values(), force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    meta30 = json.loads(input_paths["ib_statement_meta.json"].read_text(encoding="utf-8"))
    if meta30.get("acceptance") != "PASS":
        LOGGER.error("Stage 30 import acceptance is not PASS: %s", meta30.get("acceptance"))
        return 1
    source_sha = (meta30.get("raw_source") or {}).get("sha256", "")
    statement_sources = read_csv(input_paths["broker_statement_sources.csv"])
    open_positions = read_csv(input_paths["broker_open_positions.csv"])
    net_stock_positions = read_csv(input_paths["broker_net_stock_positions.csv"])
    trades = read_csv(input_paths["broker_trades.csv"])
    instruments = read_csv(input_paths["broker_instruments.csv"])
    cash_report = read_csv(input_paths["broker_cash_report.csv"])
    dividends = read_csv(input_paths["broker_dividends.csv"])
    cash_transactions = read_csv(input_paths["broker_cash_transactions.csv"])
    fees = read_csv(input_paths["broker_fees.csv"])
    securities_lending = read_csv(input_paths["broker_securities_lending.csv"])

    prior_as_of, prior_manifest_path, prior_lots_path = _previous_ledger_inputs(runs_root, run_as_of)
    prior_lots = read_csv(prior_lots_path) if prior_lots_path is not None else []
    if prior_as_of:
        LOGGER.info("Using prior sealed ledger %s as lot carry-forward seed", prior_as_of)

    overrides = _manual_overrides(config)
    open_stocks = _open_stock_positions(open_positions)
    open_options = _open_option_positions(open_positions)
    stock_lots, recs = _build_stock_holding_lots(
        run_as_of=run_as_of,
        source_sha=source_sha,
        open_stocks=open_stocks,
        trades=trades,
        overrides=overrides,
        prior_lots=prior_lots,
        prior_as_of=prior_as_of,
    )
    option_lots, option_recs = _build_option_lots(run_as_of, source_sha, open_options, trades, prior_lots, prior_as_of)
    recs.extend(option_recs)
    recs.extend(_additional_reconciliations(
        run_as_of=run_as_of,
        open_positions=open_positions,
        net_stock_positions=net_stock_positions,
        trades=trades,
        instruments=instruments,
        cash_report=cash_report,
        securities_lending=securities_lending,
    ))
    holding_lots = stock_lots + option_lots
    holding_state = _holding_state(run_as_of, source_sha, open_positions)

    write_csv(out_paths["holding_lots.csv"], HOLDING_LOT_FIELDS, holding_lots)
    write_csv(out_paths["holding_state.csv"], HOLDING_STATE_FIELDS, holding_state)
    write_csv(out_paths["ledger_reconciliation.csv"], RECONCILIATION_FIELDS, recs)

    timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))
    db_counts: dict[str, int] = {}
    with connect(db_path, timeout_sec=timeout_sec) as conn:
        init_db(conn)
        init_ledger_tables(conn)
        replace_statement_source(conn, statement_sources[0])
        db_counts["broker_open_positions"] = replace_source_rows(conn, "broker_open_positions", open_positions, source_sha)
        db_counts["broker_net_stock_positions"] = replace_source_rows(conn, "broker_net_stock_positions", net_stock_positions, source_sha)
        db_counts["broker_trades"] = replace_source_rows(conn, "broker_trades", trades, source_sha)
        db_counts["broker_instruments"] = replace_source_rows(conn, "broker_instruments", instruments, source_sha)
        db_counts["broker_cash_report"] = replace_source_rows(conn, "broker_cash_report", cash_report, source_sha)
        db_counts["broker_dividends"] = replace_source_rows(conn, "broker_dividends", dividends, source_sha)
        db_counts["broker_cash_transactions"] = replace_source_rows(conn, "broker_cash_transactions", cash_transactions, source_sha)
        db_counts["broker_fees"] = replace_source_rows(conn, "broker_fees", fees, source_sha)
        db_counts["broker_securities_lending"] = replace_source_rows(conn, "broker_securities_lending", securities_lending, source_sha)
        db_counts["holdings_lots"] = replace_run_rows(conn, "holdings_lots", holding_lots, run_as_of)
        db_counts["holding_state"] = replace_run_rows(conn, "holding_state", holding_state, run_as_of)
        db_counts["broker_reconciliations"] = replace_reconciliations(conn, recs, run_as_of=run_as_of, source_sha256=source_sha)

    passed = all(row["status"] in {"PASS", "WARN"} for row in recs)
    build_meta = {
        "stage": "stage8_5_build_holdings_ledger",
        "run_as_of": run_as_of,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "acceptance": "PASS" if passed else "FAIL",
        "db_path": str(db_path),
        "source_sha256": source_sha,
        "input_paths": {
            **{name: str(path) for name, path in input_paths.items()},
            **({"prior_ledger_manifest.json": str(prior_manifest_path)} if prior_manifest_path is not None else {}),
            **({"prior_holding_lots.csv": str(prior_lots_path)} if prior_lots_path is not None else {}),
        },
        "inputs_sha256": {
            **{name: sha256_file(path) for name, path in input_paths.items()},
            **({"prior_ledger_manifest.json": sha256_file(prior_manifest_path)} if prior_manifest_path is not None else {}),
            **({"prior_holding_lots.csv": sha256_file(prior_lots_path)} if prior_lots_path is not None else {}),
        },
        "prior_ledger_as_of": prior_as_of,
        "outputs_sha256": {name: sha256_file(path) for name, path in out_paths.items() if name != "ledger_build_meta.json"},
        "db_row_counts": db_counts,
        "row_counts": {
            "holding_lots": len(holding_lots),
            "holding_state": len(holding_state),
            "reconciliation_checks": len(recs),
        },
        "source_files_sha256": {
            name: sha256_file(PACKAGE_ROOT / "ledger" / name)
            for name in SOURCE_FILES
            if (PACKAGE_ROOT / "ledger" / name).exists()
        },
    }
    write_manifest(out_paths["ledger_build_meta.json"], build_meta)

    for row in recs:
        LOGGER.info("[%s] %s -- %s", row["status"], row["check"], row["detail"])
    if passed:
        LOGGER.info(
            "STAGE 8.5 LEDGER BUILD: PASS (as_of=%s, lots=%d, holdings=%d, db=%s)",
            run_as_of,
            len(holding_lots),
            len(holding_state),
            db_path,
        )
        return 0
    LOGGER.error("STAGE 8.5 LEDGER BUILD: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Stage 9 Phase 2 - exit-adjusted book, lot-FIFO realized P&L estimate, and price-stop probe.

Consumes the Phase 1 exit actions (exits/34), the broker lot ledger (Stage 8.5 holding_lots), and
the sealed Stage 2 price panel to produce three DIAGNOSTIC artifacts (nothing trades):

  exit_adjusted_book.csv    the actual holdings book with exit fractions applied; exited weight
                            flows to CASH; weight conservation is a hard gate
  realized_pnl_fifo.csv     per-lot FIFO realized P&L estimate for the exited fractions (oldest
                            lots exit first, marked at the panel's last as-of close) plus
                            unrealized P&L on the remainder
  price_stop_probe.csv      per-holding probe: drawdown from FIFO cost basis and from the trailing
                            high vs configured stop levels — flags candidates a price-stop rule
                            WOULD have exited (rule evaluation only; exits stay score-driven)

PIT: marks come from the sealed panel's last bar at/before the run as-of. Lots come from the
ledger's reconstructed FIFO lot table (entry_date_unknown lots are marked and their realized P&L
is flagged estimate_basis_incomplete rather than dropped).
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

from portfolio_layer.core.artifacts import invalidate_dependents  # noqa: E402
from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.contracts import read_manifest, sealed_artifact_errors  # noqa: E402
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("build_exit_adjusted_book")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"

BOOK_FIELDS = ["ticker", "pre_exit_weight", "exit_fraction", "post_exit_weight", "exit_signal",
               "action", "market_value", "post_exit_market_value"]
PNL_FIELDS = ["ticker", "lot_id", "entry_date", "lot_quantity", "exited_quantity", "cost_basis_total",
              "exited_cost_basis", "mark_price", "mark_date", "realized_pnl", "unrealized_pnl",
              "basis_quality"]
PROBE_FIELDS = ["ticker", "weight", "mark_price", "fifo_avg_cost", "pnl_pct_vs_cost",
                "trailing_high", "drawdown_from_high", "stop_vs_cost_would_exit",
                "stop_vs_high_would_exit", "already_exiting"]
CASH_TICKER = "CASH"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stage 9 Phase 2 exit-adjusted book + FIFO P&L + stop probe.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", default=None)
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--force", action="store_true")
    return p.parse_args()


# ---------------------------------------------------------------------------
# pure FIFO math (self-tested)
# ---------------------------------------------------------------------------
def fifo_exit_allocation(lots: list[dict[str, Any]], exit_quantity: float) -> list[dict[str, Any]]:
    """Allocate an exit quantity across lots oldest-first; returns per-lot exited quantities."""
    ordered = sorted(lots, key=lambda x: (str(x.get("entry_date", "")), str(x.get("lot_id", ""))))
    remaining = max(0.0, float(exit_quantity))
    out = []
    for lot in ordered:
        qty = float(lot.get("quantity", 0.0) or 0.0)
        take = min(qty, remaining)
        out.append({**lot, "exited_quantity": take})
        remaining -= take
    return out


def realized_pnl_row(lot: dict[str, Any], *, mark_price: float, mark_date: str) -> dict[str, Any]:
    qty = float(lot.get("quantity", 0.0) or 0.0)
    exited = float(lot.get("exited_quantity", 0.0) or 0.0)
    basis_total = float(lot.get("cost_basis", 0.0) or 0.0)
    per_share_basis = basis_total / qty if qty > 0 else 0.0
    exited_basis = per_share_basis * exited
    realized = exited * mark_price - exited_basis
    unrealized = (qty - exited) * mark_price - (basis_total - exited_basis)
    quality = "ok" if not int(lot.get("entry_date_unknown", 0) or 0) else "estimate_basis_incomplete"
    return {
        "ticker": str(lot.get("symbol", lot.get("ticker", ""))).upper(),
        "lot_id": str(lot.get("lot_id", "")),
        "entry_date": str(lot.get("entry_date", "")),
        "lot_quantity": round(qty, 6),
        "exited_quantity": round(exited, 6),
        "cost_basis_total": round(basis_total, 4),
        "exited_cost_basis": round(exited_basis, 4),
        "mark_price": round(mark_price, 6),
        "mark_date": mark_date,
        "realized_pnl": round(realized, 4),
        "unrealized_pnl": round(unrealized, 4),
        "basis_quality": quality,
    }


def exit_fraction_from_action(action: dict[str, Any]) -> float:
    """Parse the Stage 34 action contract without silently turning malformed exits into keeps."""
    raw = action.get("proposed_exit_fraction")
    if raw is None:
        raise ValueError("missing proposed_exit_fraction")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid proposed_exit_fraction={raw!r}") from exc
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"proposed_exit_fraction outside [0,1]: {raw!r}")
    return value


def _selftest() -> None:
    lots = [
        {"lot_id": "B", "entry_date": "2024-02-01", "quantity": 100.0, "cost_basis": 1000.0,
         "entry_date_unknown": 0, "symbol": "XYZ"},
        {"lot_id": "A", "entry_date": "2024-01-01", "quantity": 50.0, "cost_basis": 400.0,
         "entry_date_unknown": 0, "symbol": "XYZ"},
    ]
    alloc = fifo_exit_allocation(lots, 70.0)
    by_id = {a["lot_id"]: a for a in alloc}
    assert by_id["A"]["exited_quantity"] == 50.0 and by_id["B"]["exited_quantity"] == 20.0, alloc
    row = realized_pnl_row(by_id["A"], mark_price=12.0, mark_date="2024-06-01")
    # lot A: 50 sh at basis 8.0 -> exit all 50 at 12 => realized 200, unrealized 0
    assert abs(row["realized_pnl"] - 200.0) < 1e-9 and abs(row["unrealized_pnl"]) < 1e-9, row
    row_b = realized_pnl_row(by_id["B"], mark_price=12.0, mark_date="2024-06-01")
    # lot B: 100 sh at basis 10 -> exit 20 => realized 40; remaining 80 sh unrealized 160
    assert abs(row_b["realized_pnl"] - 40.0) < 1e-9 and abs(row_b["unrealized_pnl"] - 160.0) < 1e-9, row_b
    over = fifo_exit_allocation(lots, 500.0)
    assert sum(a["exited_quantity"] for a in over) == 150.0  # capped at available
    unknown = realized_pnl_row({**lots[0], "exited_quantity": 10.0, "entry_date_unknown": 1},
                               mark_price=12.0, mark_date="2024-06-01")
    assert unknown["basis_quality"] == "estimate_basis_incomplete"
    assert exit_fraction_from_action({"proposed_exit_fraction": "0.5"}) == 0.5
    try:
        exit_fraction_from_action({"exit_fraction": "0.5"})
    except ValueError:
        pass
    else:
        raise AssertionError("legacy/missing Stage 34 action field must fail closed")
    print("exit-adjusted-book self-test: PASS")


def main() -> int:  # noqa: C901
    configure_utc_logging()
    args = parse_args()
    if args.selftest:
        _selftest()
        return 0
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    runs_root = paths.output_dir / "runs"
    run_as_of = args.as_of or latest_run_with(runs_root, "exits/exit_actions.csv")
    if not run_as_of:
        LOGGER.error("No run with exits/exit_actions.csv under %s (run 33/34 first)", runs_root)
        return 1
    run_dir = runs_root / run_as_of
    art = {
        "actions": run_dir / "exits" / "exit_actions.csv",
        "lots": run_dir / "ledger" / "holding_lots.csv",
        "holding_state": run_dir / "ledger" / "holding_state.csv",
        "broker_open_positions": run_dir / "ledger" / "broker_open_positions.csv",
        "prices": run_dir / "risk" / "prices_adjclose.csv",
    }
    action_meta_path = run_dir / "exits" / "exit_actions_meta.json"
    exit_manifest_path = run_dir / "exits" / "exit_manifest.json"
    ledger_manifest_path = run_dir / "ledger" / "ledger_manifest.json"
    risk_manifest_path = run_dir / "risk" / "risk_manifest.json"
    required_meta = {
        "exit_actions_meta": action_meta_path,
        "exit_manifest": exit_manifest_path,
        "ledger_manifest": ledger_manifest_path,
        "risk_manifest": risk_manifest_path,
    }
    missing = [k for k, p in (art | required_meta).items() if not p.exists()]
    if missing:
        LOGGER.error("Missing inputs %s (need exits 33/34, ledger 31, risk 05)", missing)
        return 1
    action_meta = read_manifest(action_meta_path)
    exit_manifest = read_manifest(exit_manifest_path)
    ledger_manifest = read_manifest(ledger_manifest_path)
    risk_manifest = read_manifest(risk_manifest_path)
    seal_errors = sealed_artifact_errors(
        action_meta, art["actions"], "exit_actions.csv", run_as_of=run_as_of,
    )
    seal_errors.extend(
        sealed_artifact_errors(
            exit_manifest, art["actions"], "exit_actions.csv", run_as_of=run_as_of,
        )
    )
    if str(exit_manifest.get("ledger_as_of", "")).strip() != run_as_of:
        seal_errors.append(
            f"exit_manifest_ledger_as_of={exit_manifest.get('ledger_as_of')} expected={run_as_of}"
        )
    seal_errors.extend(
        sealed_artifact_errors(
            ledger_manifest, art["lots"], "holding_lots", run_as_of=run_as_of,
        )
    )
    seal_errors.extend(
        sealed_artifact_errors(
            ledger_manifest, art["holding_state"], "holding_state", run_as_of=run_as_of,
        )
    )
    seal_errors.extend(
        sealed_artifact_errors(
            ledger_manifest,
            art["broker_open_positions"],
            "broker_open_positions",
            run_as_of=run_as_of,
        )
    )
    seal_errors.extend(
        sealed_artifact_errors(
            risk_manifest, art["prices"], "prices_adjclose.csv", run_as_of=run_as_of,
        )
    )
    if seal_errors:
        LOGGER.error("Stage 9 Phase 2 inputs are unsealed/stale: %s", seal_errors)
        return 1
    out = {
        "book": run_dir / "exits" / "exit_adjusted_book.csv",
        "pnl": run_dir / "exits" / "realized_pnl_fifo.csv",
        "probe": run_dir / "exits" / "price_stop_probe.csv",
        "meta": run_dir / "exits" / "exit_adjusted_book_meta.json",
    }
    try:
        fail_if_exists(list(out.values()), force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    stop_vs_cost = float(cfg_get(config, "exit_engine.price_stop_probe.loss_vs_cost_pct", 0.25))
    stop_vs_high = float(cfg_get(config, "exit_engine.price_stop_probe.drawdown_vs_high_pct", 0.20))
    high_lookback = int(cfg_get(config, "exit_engine.price_stop_probe.trailing_high_lookback_days", 126))

    prices = pd.read_csv(art["prices"], index_col=0)
    prices.columns = [str(c).strip().upper() for c in prices.columns]
    mark_date = str(prices.index[-1])[:10]
    if mark_date > run_as_of:
        LOGGER.error("Panel right edge %s is after run as-of %s (lookahead)", mark_date, run_as_of)
        return 1
    marks = prices.iloc[-1].apply(pd.to_numeric, errors="coerce")

    broker_marks: dict[str, tuple[float, str]] = {}
    broker_mark_errors: list[str] = []
    for row_number, row in enumerate(read_csv(art["broker_open_positions"]), start=2):
        if str(row.get("asset_category") or "").strip() != "Stocks":
            continue
        ticker = str(row.get("symbol") or "").strip().upper()
        broker_date = str(row.get("statement_end_date") or "").strip()
        try:
            broker_mark = float(row.get("close_price") or "nan")
        except (TypeError, ValueError):
            broker_mark = float("nan")
        if not ticker:
            broker_mark_errors.append(f"row={row_number}:blank_ticker")
        elif ticker in broker_marks:
            broker_mark_errors.append(f"row={row_number}:{ticker}:duplicate_broker_mark")
        elif broker_date != run_as_of:
            broker_mark_errors.append(
                f"row={row_number}:{ticker}:broker_mark_date={broker_date!r} expected={run_as_of}"
            )
        elif not math.isfinite(broker_mark) or broker_mark <= 0.0:
            broker_mark_errors.append(f"row={row_number}:{ticker}:broker_mark={broker_mark!r}")
        else:
            broker_marks[ticker] = (broker_mark, broker_date)
    if broker_mark_errors:
        LOGGER.error("Sealed broker marks are malformed: %s", broker_mark_errors[:12])
        return 1

    lots_by_ticker: dict[str, list[dict[str, Any]]] = {}
    for r in read_csv(art["lots"]):
        if str(r.get("asset_category", "")) != "Stocks":
            continue
        t = str(r.get("symbol", "")).strip().upper()
        if t:
            lots_by_ticker.setdefault(t, []).append(dict(r))

    holdings: dict[str, dict[str, float]] = {}
    holding_errors: list[str] = []
    for row_number, r in enumerate(read_csv(art["holding_state"]), start=2):
        if str(r.get("asset_category", "")).strip() != "Stocks":
            continue
        t = str(r.get("symbol", r.get("ticker", ""))).strip().upper()
        try:
            qty = float(r.get("quantity") or r.get("net_quantity") or 0.0)
        except (TypeError, ValueError):
            holding_errors.append(f"row={row_number}:{t or '<blank>'}:invalid_quantity")
            continue
        if not t or not math.isfinite(qty):
            holding_errors.append(f"row={row_number}:ticker={t!r}:quantity={qty!r}")
            continue
        if qty < -1e-9:
            holding_errors.append(f"row={row_number}:{t}:short_quantity={qty}")
            continue
        if qty > 1e-9:
            if t in holdings:
                holding_errors.append(f"row={row_number}:{t}:duplicate_holding")
                continue
            holdings[t] = {"quantity": qty}
    if holding_errors or not holdings:
        LOGGER.error("Stock holding state is malformed/empty: %s", holding_errors[:12])
        return 1

    actions: dict[str, dict[str, str]] = {}
    action_errors: list[str] = []
    for row_number, row in enumerate(read_csv(art["actions"]), start=2):
        ticker = str(row.get("ticker", "")).strip().upper()
        if not ticker or ticker in actions:
            action_errors.append(f"row={row_number}:ticker={ticker!r}:duplicate={ticker in actions}")
            continue
        try:
            fraction = exit_fraction_from_action(row)
        except ValueError as exc:
            action_errors.append(f"row={row_number}:{ticker}:{exc}")
            continue
        action = str(row.get("action", "")).strip()
        if action not in {"keep", "review", "soft_exit", "hard_exit"}:
            action_errors.append(f"row={row_number}:{ticker}:invalid_action={action!r}")
            continue
        if (action in {"keep", "review"} and fraction != 0.0) or (
            action in {"soft_exit", "hard_exit"} and fraction <= 0.0
        ):
            action_errors.append(f"row={row_number}:{ticker}:action_fraction_mismatch={action}/{fraction}")
            continue
        actions[ticker] = row
    missing_actions = sorted(set(holdings) - set(actions))
    extra_actions = sorted(set(actions) - set(holdings))
    if missing_actions:
        action_errors.append(f"missing_actions={missing_actions[:12]}")
    if extra_actions:
        action_errors.append(f"actions_without_stock_holdings={extra_actions[:12]}")
    if action_errors:
        LOGGER.error("Exit-action contract is malformed/misaligned: %s", action_errors[:12])
        return 1

    book_rows: list[dict[str, Any]] = []
    pnl_rows: list[dict[str, Any]] = []
    probe_rows: list[dict[str, Any]] = []
    total_mv = 0.0
    values: dict[str, float] = {}
    mark_by_ticker: dict[str, float] = {}
    mark_date_by_ticker: dict[str, str] = {}
    mark_source_by_ticker: dict[str, str] = {}
    mark_errors: list[str] = []
    for t, h in holdings.items():
        mark = float(marks.get(t)) if pd.notna(marks.get(t)) else float("nan")
        ticker_mark_date = mark_date
        mark_source = "stage2_adjusted_close"
        if not math.isfinite(mark) or mark <= 0.0:
            broker = broker_marks.get(t)
            if broker is None:
                mark_errors.append(f"{t}:panel_mark={marks.get(t)!r}:broker_mark=missing")
                continue
            mark, ticker_mark_date = broker
            mark_source = "sealed_ib_statement_close"
        mv = h["quantity"] * mark
        mark_by_ticker[t] = mark
        mark_date_by_ticker[t] = ticker_mark_date
        mark_source_by_ticker[t] = mark_source
        values[t] = mv
        total_mv += mv
    if mark_errors or total_mv <= 0.0:
        LOGGER.error("Cannot value every stock holding at a positive sealed mark: %s", mark_errors[:12])
        return 1
    conservation_errors: list[str] = []
    for t, h in sorted(holdings.items()):
        mark = mark_by_ticker[t]
        ticker_mark_date = mark_date_by_ticker[t]
        action = actions[t]
        exit_fraction = exit_fraction_from_action(action)
        pre_w = values[t] / total_mv if total_mv > 0 else 0.0
        post_w = pre_w * (1.0 - exit_fraction)
        book_rows.append({
            "ticker": t, "pre_exit_weight": round(pre_w, 10),
            "exit_fraction": round(exit_fraction, 6),
            "post_exit_weight": round(post_w, 10),
            "exit_signal": str(action.get("exit_signal", "")),
            "action": str(action.get("action", "")),
            "market_value": round(values[t], 2),
            "post_exit_market_value": round(values[t] * (1.0 - exit_fraction), 2),
        })
        # FIFO realized P&L for the exited fraction
        lots = lots_by_ticker.get(t, [])
        if exit_fraction > 0 and lots and mark > 0:
            exit_qty = h["quantity"] * exit_fraction
            for lot in fifo_exit_allocation(lots, exit_qty):
                if float(lot.get("exited_quantity", 0.0) or 0.0) > 0 or float(lot.get("quantity", 0) or 0) > 0:
                    pnl_rows.append(realized_pnl_row(lot, mark_price=mark, mark_date=ticker_mark_date))
        # price-stop probe (diagnostic only)
        basis_qty = sum(float(x.get("quantity", 0) or 0) for x in lots)
        basis_total = sum(float(x.get("cost_basis", 0) or 0) for x in lots)
        avg_cost = basis_total / basis_qty if basis_qty > 0 else 0.0
        col = prices[t].apply(pd.to_numeric, errors="coerce") if t in prices.columns else None
        trailing_high = float(col.tail(high_lookback).max()) if col is not None else 0.0
        pnl_pct = (mark / avg_cost - 1.0) if avg_cost > 0 else 0.0
        dd_high = (mark / trailing_high - 1.0) if trailing_high > 0 else 0.0
        probe_rows.append({
            "ticker": t, "weight": round(pre_w, 8), "mark_price": round(mark, 4),
            "fifo_avg_cost": round(avg_cost, 4), "pnl_pct_vs_cost": round(pnl_pct, 6),
            "trailing_high": round(trailing_high, 4), "drawdown_from_high": round(dd_high, 6),
            "stop_vs_cost_would_exit": int(avg_cost > 0 and pnl_pct <= -stop_vs_cost),
            "stop_vs_high_would_exit": int(trailing_high > 0 and dd_high <= -stop_vs_high),
            "already_exiting": int(exit_fraction > 0),
        })
    pre_asset_sum = sum(float(r["pre_exit_weight"]) for r in book_rows)
    post_asset_sum = sum(float(r["post_exit_weight"]) for r in book_rows)
    exited_to_cash = pre_asset_sum - post_asset_sum
    book_rows.append({
        "ticker": CASH_TICKER,
        "pre_exit_weight": 0.0,
        "exit_fraction": 0.0,
        "post_exit_weight": round(exited_to_cash, 10),
        "exit_signal": "",
        "action": "cash_from_exits",
        "market_value": 0.0,
        "post_exit_market_value": round(exited_to_cash * total_mv, 2),
    })
    pre_sum = sum(float(r["pre_exit_weight"]) for r in book_rows)
    post_sum = sum(float(r["post_exit_weight"]) for r in book_rows)
    if abs(pre_sum - 1.0) > 1e-6:
        conservation_errors.append(f"pre_exit_weights_sum={pre_sum:.10f}")
    if abs(post_sum - 1.0) > 1e-6:
        conservation_errors.append(f"post_exit_weights_sum={post_sum:.10f}")
    intended_exit_names = {t for t, a in actions.items() if exit_fraction_from_action(a) > 0.0}
    applied_exit_names = {str(r["ticker"]) for r in book_rows if float(r["exit_fraction"]) > 0.0}
    if intended_exit_names != applied_exit_names:
        conservation_errors.append(
            f"exit_contract_mismatch intended={sorted(intended_exit_names)} applied={sorted(applied_exit_names)}"
        )

    broker_mark_count = sum(source == "sealed_ib_statement_close" for source in mark_source_by_ticker.values())
    checks = [
        {"check": "weight_conservation", "status": "PASS" if not conservation_errors else "FAIL",
         "detail": f"pre={pre_sum:.8f} post={post_sum:.8f} exited_to_cash={exited_to_cash:.8f}"
         if not conservation_errors else ";".join(conservation_errors)},
        {"check": "stage34_action_contract_applied", "status": "PASS" if not conservation_errors else "FAIL",
         "detail": f"intended_exit_names={len(intended_exit_names)} applied={len(applied_exit_names)}"},
        {
            "check": "marks_pit",
            "status": "PASS",
            "detail": (
                f"panel_mark_date={mark_date}<=asof={run_as_of}; "
                f"stage2_marks={len(holdings) - broker_mark_count}; "
                f"same_day_sealed_broker_marks={broker_mark_count}"
            ),
        },
        {"check": "diagnostic_only", "status": "PASS",
         "detail": "price-stop probe and realized P&L are diagnostics; exits remain score-driven"},
    ]
    # Invalidate accepted consumers only after all inputs and replacement rows are valid. A failed
    # build must not erase the prior diagnostic book before a replacement can be sealed.
    if args.force:
        invalidate_dependents(run_dir, "exits")
    write_csv(out["book"], BOOK_FIELDS, book_rows)
    write_csv(out["pnl"], PNL_FIELDS, pnl_rows)
    write_csv(out["probe"], PROBE_FIELDS, probe_rows)
    passed = all(c["status"] in ("PASS", "WARN") for c in checks)
    write_manifest(out["meta"], {
        "stage": "stage9_phase2_exit_adjusted_book",
        "run_as_of": run_as_of,
        "generated_at": utc_now(),
        "acceptance": "PASS" if passed else "FAIL",
        # Source-book lineage: this diagnostic book adjusts the ACTUAL broker holdings universe
        # (ledger/holding_state.csv), not any composed target book. Consumers that would promote it
        # in place of a composed book must compare inputs_sha256.book against the book they
        # composed and fail closed on mismatch.
        "book_source": "ledger/holding_state.csv",
        "inputs_sha256": {
            "book": sha256_file(art["holding_state"]),
        },
        "mark_date": mark_date,
        "mark_source_counts": {
            "stage2_adjusted_close": len(holdings) - broker_mark_count,
            "sealed_ib_statement_close": broker_mark_count,
        },
        "holdings": len(holdings),
        "exiting_names": len(applied_exit_names),
        "exited_weight_to_cash": round(exited_to_cash, 8),
        "realized_pnl_total": round(sum(r["realized_pnl"] for r in pnl_rows), 2),
        "stop_probe_flags": {
            "vs_cost": sum(r["stop_vs_cost_would_exit"] for r in probe_rows),
            "vs_high": sum(r["stop_vs_high_would_exit"] for r in probe_rows),
        },
        "checks": checks,
        "provenance_sha256": {
            **{name: sha256_file(p) for name, p in art.items()},
            "exit_actions_meta.json": sha256_file(action_meta_path),
            "exit_manifest.json": sha256_file(exit_manifest_path),
            "ledger_manifest.json": sha256_file(ledger_manifest_path),
            "risk_manifest.json": sha256_file(risk_manifest_path),
        },
        "outputs_sha256": {
            "exit_adjusted_book.csv": sha256_file(out["book"]),
            "realized_pnl_fifo.csv": sha256_file(out["pnl"]),
            "price_stop_probe.csv": sha256_file(out["probe"]),
        },
    })
    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    LOGGER.info("EXIT-ADJUSTED BOOK: %s (holdings=%d, exiting=%d, exited_weight=%.4f, realized_pnl=%.2f)",
                "PASS" if passed else "FAIL", len(holdings),
                len(applied_exit_names), exited_to_cash,
                sum(r["realized_pnl"] for r in pnl_rows))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

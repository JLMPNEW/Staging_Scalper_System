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
import sys
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("build_exit_adjusted_book")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"

BOOK_FIELDS = ["ticker", "pre_exit_weight", "exit_fraction", "post_exit_weight", "exit_signal",
               "action_hint", "market_value", "post_exit_market_value"]
PNL_FIELDS = ["ticker", "lot_id", "entry_date", "lot_quantity", "exited_quantity", "cost_basis_total",
              "exited_cost_basis", "mark_price", "mark_date", "realized_pnl", "unrealized_pnl",
              "basis_quality"]
PROBE_FIELDS = ["ticker", "weight", "mark_price", "fifo_avg_cost", "pnl_pct_vs_cost",
                "trailing_high", "drawdown_from_high", "stop_vs_cost_would_exit",
                "stop_vs_high_would_exit", "already_exiting"]


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
        "prices": run_dir / "risk" / "prices_adjclose.csv",
    }
    missing = [k for k, p in art.items() if not p.exists()]
    if missing:
        LOGGER.error("Missing inputs %s (need exits 33/34, ledger 31, risk 05)", missing)
        return 1
    out = {
        "book": run_dir / "exits" / "exit_adjusted_book.csv",
        "pnl": run_dir / "exits" / "realized_pnl_fifo.csv",
        "probe": run_dir / "exits" / "price_stop_probe.csv",
        "meta": run_dir / "exits" / "exit_adjusted_book_meta.json",
    }
    if args.force:
        for p in out.values():
            if p.exists():
                p.unlink()
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

    lots_by_ticker: dict[str, list[dict[str, Any]]] = {}
    for r in read_csv(art["lots"]):
        if str(r.get("asset_category", "")) != "Stocks":
            continue
        t = str(r.get("symbol", "")).strip().upper()
        if t:
            lots_by_ticker.setdefault(t, []).append(dict(r))

    holdings: dict[str, dict[str, float]] = {}
    for r in read_csv(art["holding_state"]):
        t = str(r.get("symbol", r.get("ticker", ""))).strip().upper()
        try:
            qty = float(r.get("quantity") or r.get("net_quantity") or 0.0)
        except (TypeError, ValueError):
            continue
        if t and abs(qty) > 1e-9:
            holdings[t] = {"quantity": qty}

    actions = {str(r.get("ticker", "")).strip().upper(): r for r in read_csv(art["actions"])}

    book_rows: list[dict[str, Any]] = []
    pnl_rows: list[dict[str, Any]] = []
    probe_rows: list[dict[str, Any]] = []
    total_mv = 0.0
    values: dict[str, float] = {}
    for t, h in holdings.items():
        mark = float(marks.get(t)) if pd.notna(marks.get(t)) else 0.0
        mv = h["quantity"] * mark
        values[t] = mv
        total_mv += mv
    conservation_errors: list[str] = []
    for t, h in sorted(holdings.items()):
        mark = float(marks.get(t)) if pd.notna(marks.get(t)) else 0.0
        action = actions.get(t, {})
        try:
            exit_fraction = float(action.get("exit_fraction") or 0.0)
        except (TypeError, ValueError):
            exit_fraction = 0.0
        exit_fraction = min(1.0, max(0.0, exit_fraction))
        pre_w = values[t] / total_mv if total_mv > 0 else 0.0
        post_w = pre_w * (1.0 - exit_fraction)
        book_rows.append({
            "ticker": t, "pre_exit_weight": round(pre_w, 10),
            "exit_fraction": round(exit_fraction, 6),
            "post_exit_weight": round(post_w, 10),
            "exit_signal": str(action.get("exit_signal", "")),
            "action_hint": str(action.get("action_hint", "")),
            "market_value": round(values[t], 2),
            "post_exit_market_value": round(values[t] * (1.0 - exit_fraction), 2),
        })
        # FIFO realized P&L for the exited fraction
        lots = lots_by_ticker.get(t, [])
        if exit_fraction > 0 and lots and mark > 0:
            exit_qty = h["quantity"] * exit_fraction
            for lot in fifo_exit_allocation(lots, exit_qty):
                if float(lot.get("exited_quantity", 0.0) or 0.0) > 0 or float(lot.get("quantity", 0) or 0) > 0:
                    pnl_rows.append(realized_pnl_row(lot, mark_price=mark, mark_date=mark_date))
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
    pre_sum = sum(r["pre_exit_weight"] for r in book_rows)
    post_sum = sum(r["post_exit_weight"] for r in book_rows)
    exited_to_cash = pre_sum - post_sum
    if holdings and abs(pre_sum - 1.0) > 1e-6:
        conservation_errors.append(f"pre_exit_weights_sum={pre_sum:.10f}")
    if post_sum > pre_sum + 1e-9:
        conservation_errors.append("post_exit_exceeds_pre_exit")

    checks = [
        {"check": "weight_conservation", "status": "PASS" if not conservation_errors else "FAIL",
         "detail": f"pre={pre_sum:.8f} post={post_sum:.8f} exited_to_cash={exited_to_cash:.8f}"
         if not conservation_errors else ";".join(conservation_errors)},
        {"check": "marks_pit", "status": "PASS", "detail": f"mark_date={mark_date}<=as_of={run_as_of}"},
        {"check": "diagnostic_only", "status": "PASS",
         "detail": "price-stop probe and realized P&L are diagnostics; exits remain score-driven"},
    ]
    write_csv(out["book"], BOOK_FIELDS, book_rows)
    write_csv(out["pnl"], PNL_FIELDS, pnl_rows)
    write_csv(out["probe"], PROBE_FIELDS, probe_rows)
    passed = all(c["status"] in ("PASS", "WARN") for c in checks)
    write_manifest(out["meta"], {
        "stage": "stage9_phase2_exit_adjusted_book",
        "run_as_of": run_as_of,
        "generated_at": utc_now(),
        "acceptance": "PASS" if passed else "FAIL",
        "mark_date": mark_date,
        "holdings": len(holdings),
        "exiting_names": sum(1 for r in book_rows if r["exit_fraction"] > 0),
        "exited_weight_to_cash": round(exited_to_cash, 8),
        "realized_pnl_total": round(sum(r["realized_pnl"] for r in pnl_rows), 2),
        "stop_probe_flags": {
            "vs_cost": sum(r["stop_vs_cost_would_exit"] for r in probe_rows),
            "vs_high": sum(r["stop_vs_high_would_exit"] for r in probe_rows),
        },
        "checks": checks,
        "provenance_sha256": {name: sha256_file(p) for name, p in art.items()},
    })
    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    LOGGER.info("EXIT-ADJUSTED BOOK: %s (holdings=%d, exiting=%d, exited_weight=%.4f, realized_pnl=%.2f)",
                "PASS" if passed else "FAIL", len(holdings),
                sum(1 for r in book_rows if r["exit_fraction"] > 0), exited_to_cash,
                sum(r["realized_pnl"] for r in pnl_rows))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

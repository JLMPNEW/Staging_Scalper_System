#!/usr/bin/env python3
"""Stage 7 - apply the Stage 4 transaction-cost overlay to the fused BL book.

This writes under `runs/<as_of>/blacklitterman/costs/` and never mutates the Stage 4 baseline `costs/`
directory. The economics are intentionally the same as Stage 4: flat commission, row-level spread from the
liquidity panel when enabled, AUM-aware minimum economic position, and residual routed to CASH.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.costs.cost_common import (  # noqa: E402
    commission,
    decision_commission,
    finite_float,
    half_spread_for_ticker,
    load_spread_inputs,
    prior_fingerprint,
    resolve_aum,
)
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("apply_bl_cost_overlay")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SOURCE_FILES = ["25_apply_bl_cost_overlay.py"]
TRADE_FIELDS = ["ticker", "prior_weight", "target_weight", "delta_weight", "side", "trade_notional", "n_orders"]
COST_FIELDS = [
    "ticker", "side", "trade_notional", "commission_base", "commission_worst",
    "half_spread_bps_used", "spread_source", "spread_status", "spread_reason",
    "spread_cost", "impact_cost", "total_cost_base", "total_cost_worst", "cost_bps_of_position",
]
DECISION_FIELDS = [
    "ticker", "prior_weight", "target_weight", "decision", "reason", "position_notional",
    "commission_fraction", "utility_gain", "cost_drag",
]
EPS = 1e-12


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Apply Stage 4 cost overlay to Stage 7 BL target weights.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, default=None)
    p.add_argument("--aum", type=float, default=None)
    p.add_argument("--prior-weights", type=Path, default=None)
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def _f(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed == parsed and abs(parsed) != float("inf") else default


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_weight_map(path: Path | None) -> dict[str, float]:
    if path is None:
        return {}
    out: dict[str, float] = {}
    for row in read_csv(path):
        ticker = str(row.get("ticker") or row.get("Ticker") or "").strip().upper()
        if not ticker or ticker == "CASH":
            continue
        weight = finite_float(row.get("weight") or row.get("Weight") or 0.0, name=f"{path}:{ticker}.weight")
        if weight < 0:
            raise ValueError(f"Prior weight for {ticker} must be non-negative, got {weight}")
        if ticker in out:
            raise ValueError(f"Duplicate prior-weight ticker: {ticker}")
        out[ticker] = weight
    return out


def _load_bl_target(path: Path) -> tuple[dict[str, float], float]:
    target: dict[str, float] = {}
    cash_weight = 0.0
    for row in read_csv(path):
        ticker = str(row.get("Ticker") or row.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        weight = finite_float(row.get("Weight") or row.get("weight"), name=f"{path}:{ticker}.Weight")
        if weight < -1e-10:
            raise ValueError(f"BL target weight for {ticker} is negative: {weight}")
        if ticker == "CASH":
            cash_weight += max(0.0, weight)
            continue
        if weight > 0:
            if ticker in target:
                raise ValueError(f"Duplicate BL target ticker: {ticker}")
            target[ticker] = weight
    return target, cash_weight


def _verify_stage24_current(meta: dict[str, Any], bl_dir: Path) -> list[str]:
    bad: list[str] = []
    if meta.get("acceptance") != "PASS":
        bad.append(f"acceptance={meta.get('acceptance')}")
    outputs = meta.get("outputs_sha256") or {}
    for name in ("bl_target_weights.csv", "bl_optimizer_summary.csv", "optimizer/weights_long_only.csv"):
        path = bl_dir / name
        if not path.exists():
            bad.append(f"{name}:missing")
        elif outputs.get(name) != sha256_file(path):
            bad.append(f"{name}:hash_mismatch")
    return bad


def _unlink_existing(paths: list[Path]) -> None:
    for path in paths:
        if path.exists():
            path.unlink()


def main() -> int:  # noqa: C901
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    runs_root = paths.output_dir / "runs"
    run_as_of = args.as_of or latest_run_with(runs_root, "manifest.json")
    if not run_as_of:
        LOGGER.error("No sealed Stage 1 run found under %s", runs_root)
        return 1
    try:
        aum = resolve_aum(config, args.aum)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1
    run_dir = runs_root / run_as_of
    bl_dir = run_dir / "blacklitterman"
    costs_dir = bl_dir / "costs"
    meta24_path = bl_dir / "bl_optimizer_meta.json"
    target_path = bl_dir / "bl_target_weights.csv"
    trade_path = costs_dir / "bl_trade_list.csv"
    trade_meta_path = costs_dir / "bl_trade_list_meta.json"
    report_path = costs_dir / "bl_cost_report.csv"
    summary_path = costs_dir / "bl_cost_summary.json"
    adjusted_path = costs_dir / "bl_cost_adjusted_target_weights.csv"
    decisions_path = costs_dir / "bl_no_trade_decisions.csv"
    meta_path = costs_dir / "bl_cost_meta.json"
    downstream = [
        bl_dir / "bl_manifest.json",
        bl_dir / "validation" / "bl_fusion_validation.csv",
    ]
    outputs = [trade_path, trade_meta_path, report_path, summary_path, adjusted_path, decisions_path, meta_path, *downstream]
    if args.force:
        _unlink_existing(outputs)
    try:
        fail_if_exists(outputs, force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1
    for required in (meta24_path, target_path):
        if not required.exists():
            LOGGER.error("Run 24 first; missing %s", required)
            return 1

    checks: list[dict[str, str]] = []

    def rec(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    meta24 = _load_json(meta24_path)
    stage24_bad = _verify_stage24_current(meta24, bl_dir)
    rec("stage24_bl_optimizer_current", "PASS" if not stage24_bad else "FAIL",
        "24 accepted and output hashes match" if not stage24_bad else f"{stage24_bad[:8]}")
    if stage24_bad:
        write_csv(costs_dir / "validation" / "bl_cost_overlay_validation.csv", ["check", "status", "detail"], checks)
        return 1

    try:
        target, target_cash = _load_bl_target(target_path)
        prior_path = args.prior_weights.expanduser().resolve() if args.prior_weights else None
        prior = _load_weight_map(prior_path)
        prior_id = prior_fingerprint(prior_path, sha256_file)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1

    costs_dir.mkdir(parents=True, exist_ok=True)
    target_hash = sha256_file(target_path)
    trade_rows = []
    for ticker in sorted(set(target) | set(prior)):
        tw = target.get(ticker, 0.0)
        pw = prior.get(ticker, 0.0)
        delta = tw - pw
        if abs(delta) <= EPS:
            continue
        trade_rows.append({
            "ticker": ticker,
            "prior_weight": round(pw, 10),
            "target_weight": round(tw, 10),
            "delta_weight": round(delta, 10),
            "side": "buy" if delta > 0 else "sell",
            "trade_notional": round(abs(delta) * aum, 4),
            "n_orders": 1,
        })
    write_csv(trade_path, TRADE_FIELDS, trade_rows)
    trade_meta = {
        "run_as_of": run_as_of,
        "stage": "stage7_bl_trade_list",
        "aum_usd": aum,
        "prior_source": prior_id["prior_source"],
        "prior_weights_sha256": prior_id["prior_weights_sha256"],
        "target_weights_path": str(target_path),
        "target_weights_sha256": target_hash,
        "bl_optimizer_meta_sha256": sha256_file(meta24_path),
        "n_trades": len(trade_rows),
        "n_orders": sum(int(r["n_orders"]) for r in trade_rows),
        "n_buys": sum(1 for r in trade_rows if r["side"] == "buy"),
        "n_sells": sum(1 for r in trade_rows if r["side"] == "sell"),
        "gross_traded_notional": round(sum(float(r["trade_notional"]) for r in trade_rows), 2),
    }
    trade_meta_path.write_text(json.dumps(trade_meta, indent=2, sort_keys=True), encoding="utf-8")

    try:
        comm_base = commission(config, "base")
        comm_worst = commission(config, "worst_case")
        half_spread_bps = finite_float(
            cfg_get(config, "transaction_costs.half_spread_bps_default", 5.0),
            name="transaction_costs.half_spread_bps_default",
        )
        if half_spread_bps < 0:
            raise ValueError(f"transaction_costs.half_spread_bps_default must be non-negative, got {half_spread_bps}")
        spread_inputs = load_spread_inputs(config, run_dir, half_spread_bps)
        impact_model = str(cfg_get(config, "transaction_costs.impact_model", "none"))
        if impact_model != "none":
            raise ValueError(f"Unsupported transaction_costs.impact_model={impact_model!r}; ADV/volume impact is deferred")
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1

    cost_rows = []
    for trade in trade_rows:
        ticker = str(trade["ticker"])
        notional = float(trade["trade_notional"])
        n_orders = int(trade["n_orders"])
        try:
            spread_info = half_spread_for_ticker(ticker, spread_inputs)
        except ValueError as exc:
            LOGGER.error("%s", exc)
            return 1
        row_half_spread_bps = float(spread_info["half_spread_bps"])
        spread = (row_half_spread_bps / 1e4) * notional
        impact = 0.0
        comm_b = comm_base * n_orders
        comm_w = comm_worst * n_orders
        total_b = comm_b + spread + impact
        total_w = comm_w + spread + impact
        cost_rows.append({
            "ticker": ticker,
            "side": trade["side"],
            "trade_notional": round(notional, 4),
            "commission_base": round(comm_b, 4),
            "commission_worst": round(comm_w, 4),
            "half_spread_bps_used": round(row_half_spread_bps, 6),
            "spread_source": str(spread_info["source"]),
            "spread_status": str(spread_info["status"]),
            "spread_reason": str(spread_info["reason"]),
            "spread_cost": round(spread, 4),
            "impact_cost": round(impact, 4),
            "total_cost_base": round(total_b, 4),
            "total_cost_worst": round(total_w, 4),
            "cost_bps_of_position": round(total_b / notional * 1e4, 4) if notional > 0 else 0.0,
        })
    write_csv(report_path, COST_FIELDS, cost_rows)

    one_way_base = sum(float(r["total_cost_base"]) for r in cost_rows)
    one_way_worst = sum(float(r["total_cost_worst"]) for r in cost_rows)
    spread_total = sum(float(r["spread_cost"]) for r in cost_rows)
    gross_traded = sum(float(r["trade_notional"]) for r in cost_rows)
    spread_status_counts = Counter(str(r["spread_status"]) for r in cost_rows)
    spread_source_counts = Counter(str(r["spread_source"]) for r in cost_rows)
    fallback_count = sum(1 for r in cost_rows if str(r["spread_status"]) == "fallback")
    n_orders = sum(int(r["n_orders"]) for r in trade_rows)
    weighted_half_spread_bps = (spread_total / gross_traded * 1e4) if gross_traded > 0 else 0.0
    summary = {
        "run_as_of": run_as_of,
        "stage": "stage7_bl_cost_model",
        "aum_usd": aum,
        "trade_list_meta_sha256": sha256_file(trade_meta_path),
        "commission_base_per_order": comm_base,
        "commission_worst_per_order": comm_worst,
        "half_spread_bps_default": half_spread_bps,
        "fallback_half_spread_bps": spread_inputs["fallback_half_spread_bps"],
        "max_half_spread_bps": spread_inputs["max_half_spread_bps"],
        "spread_mode": spread_inputs["mode"],
        "spread_snapshot_sha256": spread_inputs["snapshot_sha256"],
        "spread_status_counts": dict(spread_status_counts),
        "spread_source_counts": dict(spread_source_counts),
        "spread_fallback_count": fallback_count,
        "spread_fallback_fraction": round(fallback_count / len(cost_rows), 6) if cost_rows else 0.0,
        "weighted_half_spread_bps": round(weighted_half_spread_bps, 6),
        "spread_cost_total": round(spread_total, 4),
        "impact_model": impact_model,
        "n_orders": n_orders,
        "commission_total_base": round(comm_base * n_orders, 4),
        "one_way_cost_base_usd": round(one_way_base, 4),
        "one_way_cost_worst_usd": round(one_way_worst, 4),
        "one_way_cost_bps_of_aum": round(one_way_base / aum * 1e4, 4),
        "round_trip_cost_base_usd_DIAGNOSTIC": round(2 * one_way_base, 4),
        "round_trip_cost_bps_of_aum_DIAGNOSTIC": round(2 * one_way_base / aum * 1e4, 4),
        "cost_components_note": (
            "commission=exact; spread=per-row liquidity snapshot when enabled else config default; "
            "impact=deferred (no ADV/volume impact model)"
        ),
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")

    try:
        min_frac = finite_float(
            cfg_get(config, "transaction_costs.min_position_commission_fraction", 0.005),
            name="transaction_costs.min_position_commission_fraction",
        )
        enable_mu_gate = bool(cfg_get(config, "transaction_costs.enable_provisional_mu_no_trade", False))
        if enable_mu_gate:
            raise ValueError("BL cost overlay refuses provisional mu no-trade gating; enable only after Stage 11 calibration")
        comm_dec = decision_commission(config)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1
    final: dict[str, float] = dict(target)
    cost_by_ticker = {str(r["ticker"]): float(r["total_cost_worst"]) for r in cost_rows}
    is_first_build = not prior
    decision_rows = []
    for ticker in sorted(set(target) | set(prior)):
        tw = target.get(ticker, 0.0)
        pw = prior.get(ticker, 0.0)
        delta = tw - pw
        if abs(delta) <= EPS:
            final[ticker] = tw
            continue
        notional = abs(delta) * aum
        if ticker not in cost_by_ticker:
            LOGGER.error("Missing BL cost_report row for trade ticker %s", ticker)
            return 1
        cost_drag = cost_by_ticker[ticker] / aum
        if pw <= 0.0 and tw > 0.0:
            commission_fraction = comm_dec / notional if notional > 0 else float("inf")
            if commission_fraction > min_frac:
                final[ticker] = 0.0
                decision_rows.append({
                    "ticker": ticker, "prior_weight": 0.0, "target_weight": round(tw, 10),
                    "decision": "drop_to_cash", "reason": "below_min_economic_position",
                    "position_notional": round(notional, 2), "commission_fraction": round(commission_fraction, 6),
                    "utility_gain": "", "cost_drag": "",
                })
                continue
            if is_first_build:
                final[ticker] = tw
                decision_rows.append({
                    "ticker": ticker, "prior_weight": 0.0, "target_weight": round(tw, 10),
                    "decision": "open", "reason": "economic_position",
                    "position_notional": round(notional, 2), "commission_fraction": round(commission_fraction, 6),
                    "utility_gain": "", "cost_drag": "",
                })
                continue
        final[ticker] = tw
        decision_rows.append({
            "ticker": ticker,
            "prior_weight": round(pw, 10),
            "target_weight": round(tw, 10),
            "decision": "execute",
            "reason": "rebalance_mu_gate_deferred_stage11",
            "position_notional": round(notional, 2),
            "commission_fraction": "",
            "utility_gain": "",
            "cost_drag": round(cost_drag, 8),
        })

    gross = 1.0
    asset_sum = sum(max(0.0, w) for w in final.values())
    cash_weight = gross - asset_sum
    if cash_weight < -1e-8:
        LOGGER.error("BL cost-adjusted book is over-invested: assets=%.10f gross=%.10f cash=%.10f", asset_sum, gross, cash_weight)
        return 1
    if abs(cash_weight) <= 1e-10:
        cash_weight = 0.0
    adjusted_rows = [{"ticker": t, "weight": round(w, 10)} for t, w in sorted(final.items()) if w > 0]
    adjusted_rows.append({"ticker": "CASH", "weight": round(cash_weight, 10)})
    write_csv(adjusted_path, ["ticker", "weight"], adjusted_rows)
    write_csv(decisions_path, DECISION_FIELDS, sorted(decision_rows, key=lambda r: r["ticker"]))

    n_dropped = sum(1 for d in decision_rows if d["decision"] == "drop_to_cash")
    rec("bl_cost_overlay_built", "PASS", f"trades={len(trade_rows)} orders={n_orders} dropped={n_dropped} cash={cash_weight:.6f}")
    rec("bl_cost_uses_stage4_policy", "PASS", f"spread_mode={summary['spread_mode']} one_way_bps={summary['one_way_cost_bps_of_aum']}")
    passed = all(c["status"] == "PASS" for c in checks)
    meta = {
        "run_as_of": run_as_of,
        "stage": "stage7_bl_cost_overlay",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "acceptance": "PASS" if passed else "FAIL",
        "aum_usd": aum,
        "target_cash_weight": round(target_cash, 10),
        "cash_weight": round(cash_weight, 10),
        "n_asset_positions": sum(1 for r in adjusted_rows if str(r["ticker"]).upper() != "CASH"),
        "n_dropped_to_cash": n_dropped,
        "checks": checks,
        "cost_summary": summary,
        "inputs_sha256": {
            "bl_optimizer_meta.json": sha256_file(meta24_path),
            "bl_target_weights.csv": target_hash,
            "config.yaml": sha256_file(config_path),
            "spread_snapshot.csv": spread_inputs["snapshot_sha256"],
        },
        "outputs_sha256": {
            "costs/bl_trade_list.csv": sha256_file(trade_path),
            "costs/bl_trade_list_meta.json": sha256_file(trade_meta_path),
            "costs/bl_cost_report.csv": sha256_file(report_path),
            "costs/bl_cost_summary.json": sha256_file(summary_path),
            "costs/bl_cost_adjusted_target_weights.csv": sha256_file(adjusted_path),
            "costs/bl_no_trade_decisions.csv": sha256_file(decisions_path),
        },
        "source_sha256": {
            name: sha256_file(PACKAGE_ROOT / "blacklitterman" / name)
            for name in SOURCE_FILES
            if (PACKAGE_ROOT / "blacklitterman" / name).exists()
        },
    }
    write_manifest(meta_path, meta)
    LOGGER.info(
        "BL cost overlay: trades=%d one-way=$%.2f (%.2f bps), cash=%.4f -> %s",
        len(trade_rows), one_way_base, summary["one_way_cost_bps_of_aum"], cash_weight, adjusted_path,
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

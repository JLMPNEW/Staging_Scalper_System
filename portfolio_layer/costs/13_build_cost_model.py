#!/usr/bin/env python3
"""Stage 4 - cost the trade list. Commission = exact flat $/order; spread = provisional; impact = deferred.

One-way cost is the default (current execution). Round-trip cost is reported as a diagnostic only.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import date
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.costs.cost_common import (  # noqa: E402
    commission, finite_float, half_spread_for_ticker, invalidate_after_cost_model,
    load_spread_inputs, require_same_aum, resolve_aum,
)
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("build_cost_model")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
COST_FIELDS = [
    "ticker", "side", "trade_notional", "commission_base", "commission_worst",
    "half_spread_bps_used", "spread_source", "spread_status", "spread_reason",
    "spread_cost", "impact_cost", "total_cost_base", "total_cost_worst", "cost_bps_of_position",
]


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Cost the Stage 4 trade list.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, default=None)
    p.add_argument("--aum", type=float, default=None)
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    runs_root = paths.output_dir / "runs"
    run_as_of = args.as_of or latest_run_with(runs_root, "manifest.json")
    if not run_as_of:
        LOGGER.error("No run found under %s", runs_root)
        return 1
    try:
        aum = resolve_aum(config, args.aum)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1
    run_dir = runs_root / run_as_of
    costs_dir = run_dir / "costs"
    trade_path = costs_dir / "trade_list.csv"
    trade_meta_path = costs_dir / "trade_list_meta.json"
    if not (trade_path.exists() and trade_meta_path.exists()):
        LOGGER.error("Run 12 first; missing %s or %s", trade_path, trade_meta_path)
        return 1
    trade_meta = json.loads(trade_meta_path.read_text(encoding="utf-8"))
    try:
        require_same_aum(aum, trade_meta.get("aum_usd"), source="trade_list_meta.json")
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1
    report_path = costs_dir / "cost_report.csv"
    summary_path = costs_dir / "cost_summary.json"
    if args.force:
        invalidate_after_cost_model(costs_dir)
    try:
        fail_if_exists([report_path, summary_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    try:
        comm_base = commission(config, "base")
        comm_worst = commission(config, "worst_case")
        half_spread_bps = finite_float(
            cfg_get(config, "transaction_costs.half_spread_bps_default", 5.0),
            name="transaction_costs.half_spread_bps_default",
        )
        if half_spread_bps < 0:
            raise ValueError(
                f"transaction_costs.half_spread_bps_default must be non-negative, got {half_spread_bps}"
            )
        spread_inputs = load_spread_inputs(config, run_dir, half_spread_bps)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1
    impact_model = str(cfg_get(config, "transaction_costs.impact_model", "none"))
    if impact_model != "none":
        LOGGER.error("Unsupported transaction_costs.impact_model=%r; ADV/volume impact is deferred", impact_model)
        return 1

    rows = []
    for t in read_csv(trade_path):
        try:
            notional = finite_float(t["trade_notional"], name=f"{t.get('ticker')}.trade_notional")
            n_orders = int(finite_float(t["n_orders"], name=f"{t.get('ticker')}.n_orders"))
        except ValueError as exc:
            LOGGER.error("%s", exc)
            return 1
        if notional < 0 or n_orders <= 0:
            LOGGER.error("Invalid trade row for %s: notional=%s n_orders=%s", t.get("ticker"), notional, n_orders)
            return 1
        try:
            spread_info = half_spread_for_ticker(t["ticker"], spread_inputs)
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
        rows.append({
            "ticker": t["ticker"], "side": t["side"], "trade_notional": round(notional, 4),
            "commission_base": round(comm_b, 4), "commission_worst": round(comm_w, 4),
            "half_spread_bps_used": round(row_half_spread_bps, 6),
            "spread_source": str(spread_info["source"]),
            "spread_status": str(spread_info["status"]),
            "spread_reason": str(spread_info["reason"]),
            "spread_cost": round(spread, 4), "impact_cost": round(impact, 4),
            "total_cost_base": round(total_b, 4), "total_cost_worst": round(total_w, 4),
            "cost_bps_of_position": round(total_b / notional * 1e4, 4) if notional > 0 else 0.0,
        })
    write_csv(report_path, COST_FIELDS, rows)

    one_way_base = sum(r["total_cost_base"] for r in rows)
    one_way_worst = sum(r["total_cost_worst"] for r in rows)
    spread_total = sum(r["spread_cost"] for r in rows)
    n_orders = sum(int(float(t["n_orders"])) for t in read_csv(trade_path))
    spread_status_counts = Counter(str(r["spread_status"]) for r in rows)
    spread_source_counts = Counter(str(r["spread_source"]) for r in rows)
    fallback_count = sum(1 for r in rows if str(r["spread_status"]) == "fallback")
    gross_traded = sum(float(r["trade_notional"]) for r in rows)
    weighted_half_spread_bps = (spread_total / gross_traded * 1e4) if gross_traded > 0 else 0.0
    summary = {
        "run_as_of": run_as_of, "aum_usd": aum,
        "trade_list_meta_sha256": sha256_file(trade_meta_path),
        "commission_base_per_order": comm_base, "commission_worst_per_order": comm_worst,
        "half_spread_bps_default": half_spread_bps,
        "fallback_half_spread_bps": spread_inputs["fallback_half_spread_bps"],
        "max_half_spread_bps": spread_inputs["max_half_spread_bps"],
        "spread_mode": spread_inputs["mode"],
        "spread_snapshot_sha256": spread_inputs["snapshot_sha256"],
        "spread_status_counts": dict(spread_status_counts),
        "spread_source_counts": dict(spread_source_counts),
        "spread_fallback_count": fallback_count,
        "spread_fallback_fraction": round(fallback_count / len(rows), 6) if rows else 0.0,
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
    write_manifest(summary_path, summary)
    LOGGER.info("Cost: %d orders, one-way $%.2f (%.2f bps of AUM) [commission $%.2f]; round-trip $%.2f (diag) -> %s",
                n_orders, one_way_base, summary["one_way_cost_bps_of_aum"], summary["commission_total_base"],
                summary["round_trip_cost_base_usd_DIAGNOSTIC"], report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

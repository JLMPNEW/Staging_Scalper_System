#!/usr/bin/env python3
"""Stage 10 - payout / liability planner. SHADOW-ONLY: emits a distribution PROPOSAL per run and
never modifies the deployable book. Per docs/LOCKBOX_PROTOCOL.md (2026-06-27 + 2026-07-05 entries)
the `+payout` walk-forward arm is UNREGISTERED for the first Open Event; production application
requires validation against a subsequent lockbox cycle.

Funding hierarchy (spec: "payout met from cash buffer + natural rotation; no position with an
intact thesis is liquidated solely to fund a distribution"):
  1. the book's CASH line (buffer),
  2. bounded HARVEST TRIMS of the weakest-conviction holdings (ascending mu_used), each capped at
     `harvest_max_per_name_fraction` of the position — full liquidation is impossible by
     construction and a forced-sale detector re-verifies it.
The plan raises target + buffer floor so the post-payout CASH keeps `min_cash_buffer_fraction`.
Already-planned Stage 4 sells are REPORTED as observed natural rotation but not double-counted as
funding: the cost-adjusted book's weights are post-trade targets, so sell proceeds are already
allocated inside them. True gains-staggering (lot-aware) starts when the broker ledger supplies
cost basis; until then trim priority is weakest-thesis-first.

Book source follows the SAME promotion rule as Stage 12a (20_compose_final_target_book):
exits/exit_adjusted_book.csv only when exit_engine.apply_in_final is true (sealed meta required,
fail closed), else costs/cost_adjusted_target_weights.csv — a merely-present sealed exits artifact
stays shadow so the payout book_sha256 handshake keys to the book Stage 12a actually composes.
Outputs runs/<as_of>/payout/{payout_plan.csv,
payout_adjusted_book.csv, payout_manifest.json}; the adjusted book moves the distribution to an
explicit PAYOUT_RESERVED line so conservation is provable.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.artifacts import invalidate_dependents  # noqa: E402
from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    read_csv,
    read_manifest,
    sealed_artifact_errors,
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("build_payout_liability")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CASH_TICKER = "CASH"
RESERVED_TICKER = "PAYOUT_RESERVED"
PLAN_FIELDS = ["funding_source", "ticker", "rating", "mu_used", "position_weight",
               "trim_weight", "notional_usd", "reason"]
BOOK_FIELDS = ["ticker", "weight"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stage 10 payout/liability planner (shadow-only).")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", default=None)
    p.add_argument("--aum", type=float, default=None, help="Override AUM (default: trade_list_meta.json).")
    p.add_argument("--force", action="store_true")
    p.add_argument("--selftest", action="store_true")
    return p.parse_args()


def _f(value: object, default: float = 0.0) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return out if out == out and out not in (float("inf"), float("-inf")) else default


def plan_payout(
    book: list[dict[str, str]],
    conviction: dict[str, dict[str, str]],
    *,
    aum: float,
    target_usd: float,
    range_fraction: float,
    min_buffer_fraction: float,
    harvest_cap: float,
    intact_ratings: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Pure planner: returns (plan_rows, payout_adjusted_book_rows, summary)."""
    weights = {str(r["ticker"]).strip(): _f(r.get("weight")) for r in book if str(r.get("ticker", "")).strip()}
    cash_weight = weights.get(CASH_TICKER, 0.0)
    gross = sum(weights.values())
    min_buffer_usd = min_buffer_fraction * aum
    cash_usd = cash_weight * aum

    plan: list[dict[str, Any]] = []
    if cash_usd > 0:
        plan.append({
            "funding_source": "cash_buffer", "ticker": CASH_TICKER, "rating": "", "mu_used": "",
            "position_weight": round(cash_weight, 10), "trim_weight": 0.0,
            "notional_usd": round(cash_usd, 2), "reason": "existing_cash_buffer",
        })

    # raise enough for the distribution AND the post-payout buffer floor
    raise_needed = max(0.0, target_usd + min_buffer_usd - cash_usd)
    holdings = sorted(
        (t for t, w in weights.items() if t not in (CASH_TICKER, RESERVED_TICKER) and w > 0),
        key=lambda t: (_f((conviction.get(t) or {}).get("mu_used"), 0.0), t),
    )
    trims: dict[str, float] = {}
    remaining = raise_needed
    for ticker in holdings:
        if remaining <= 1e-9:
            break
        position_usd = weights[ticker] * aum
        trim_usd = min(remaining, harvest_cap * position_usd)
        if trim_usd <= 0:
            continue
        trims[ticker] = trim_usd
        remaining -= trim_usd
        conv = conviction.get(ticker) or {}
        plan.append({
            "funding_source": "harvest_trim", "ticker": ticker,
            "rating": str(conv.get("rating", "")), "mu_used": _f(conv.get("mu_used"), 0.0),
            "position_weight": round(weights[ticker], 10),
            "trim_weight": round(trim_usd / aum, 10),
            "notional_usd": round(trim_usd, 2),
            "reason": f"weakest_conviction_harvest_capped_{harvest_cap:g}",
        })

    harvested_usd = sum(trims.values())
    available_usd = cash_usd + harvested_usd
    payout_usd = min(target_usd, max(0.0, available_usd - min_buffer_usd))
    post_cash_usd = available_usd - payout_usd

    adjusted: list[dict[str, Any]] = []
    for ticker in sorted(weights):
        if ticker in (CASH_TICKER, RESERVED_TICKER):
            continue
        w = weights[ticker] - trims.get(ticker, 0.0) / aum
        adjusted.append({"ticker": ticker, "weight": round(w, 10)})
    adjusted.append({"ticker": CASH_TICKER, "weight": round(post_cash_usd / aum, 10)})
    adjusted.append({"ticker": RESERVED_TICKER, "weight": round(payout_usd / aum, 10)})

    band_low = target_usd * (1.0 - range_fraction)
    band_high = target_usd * (1.0 + range_fraction)
    summary = {
        "aum_usd": aum,
        "gross_before": round(gross, 10),
        "gross_after": round(sum(_f(r["weight"]) for r in adjusted), 10),
        "cash_buffer_usd": round(cash_usd, 2),
        "harvested_usd": round(harvested_usd, 2),
        "payout_usd": round(payout_usd, 2),
        "payout_band_usd": [round(band_low, 2), round(band_high, 2)],
        "post_payout_cash_usd": round(post_cash_usd, 2),
        "min_buffer_usd": round(min_buffer_usd, 2),
        "n_harvested_names": len(trims),
        "natural_rotation_note": "stage4 planned sells reported separately; proceeds already inside target weights",
    }
    return plan, adjusted, summary


def forced_sale_check(
    plan: list[dict[str, Any]],
    base_weights: dict[str, float],
    adjusted_weights: dict[str, float],
    *,
    harvest_cap: float,
    intact_ratings: set[str],
) -> list[str]:
    """Detector (independent of the planner): no over-cap trim; no intact-thesis full liquidation."""
    violations: list[str] = []
    for row in plan:
        if row["funding_source"] != "harvest_trim":
            continue
        ticker = str(row["ticker"])
        pre = base_weights.get(ticker, 0.0)
        trim = _f(row.get("trim_weight"))
        if trim > harvest_cap * pre + 1e-9:
            violations.append(f"{ticker}:trim_{trim:.6f}_exceeds_cap_{harvest_cap * pre:.6f}")
        post = adjusted_weights.get(ticker, 0.0)
        rating = str(row.get("rating", "")).strip().lower()
        if rating in intact_ratings and post <= 1e-12:
            violations.append(f"{ticker}:intact_thesis_fully_liquidated_for_distribution")
    return violations


def selftest() -> int:
    aum = 300_000.0
    book = [
        {"ticker": "AAA", "weight": "0.30"}, {"ticker": "BBB", "weight": "0.30"},
        {"ticker": "CCC", "weight": "0.20"}, {"ticker": "DDD", "weight": "0.15"},
        {"ticker": CASH_TICKER, "weight": "0.05"},
    ]
    conviction = {
        "AAA": {"mu_used": "0.09", "rating": "strong_buy"},
        "BBB": {"mu_used": "0.06", "rating": "buy"},
        "CCC": {"mu_used": "0.02", "rating": "hold"},
        "DDD": {"mu_used": "0.005", "rating": "hold"},
    }
    intact = {"strong_buy", "buy", "hold"}
    plan, adjusted, summary = plan_payout(
        book, conviction, aum=aum, target_usd=15_000.0, range_fraction=0.2,
        min_buffer_fraction=0.02, harvest_cap=0.25, intact_ratings=intact,
    )
    base_w = {r["ticker"]: _f(r["weight"]) for r in book}
    adj_w = {r["ticker"]: _f(r["weight"]) for r in adjusted}
    checks = {
        # cash 15k covers target+floor partially; harvest tops up to keep the 6k floor
        "payout_hits_target": abs(summary["payout_usd"] - 15_000.0) < 1e-6,
        "buffer_floor_kept": summary["post_payout_cash_usd"] >= summary["min_buffer_usd"] - 1e-6,
        "weakest_trimmed_first": plan[1]["ticker"] == "DDD" if len(plan) > 1 else False,
        "conservation": abs(summary["gross_after"] - summary["gross_before"]) < 1e-9,
        "no_violations": not forced_sale_check(plan, base_w, adj_w, harvest_cap=0.25, intact_ratings=intact),
        "detector_trips_on_overcap": bool(forced_sale_check(
            [{"funding_source": "harvest_trim", "ticker": "DDD", "trim_weight": 0.10, "rating": "hold"}],
            base_w, adj_w, harvest_cap=0.25, intact_ratings=intact,
        )),
        "detector_trips_on_liquidation": bool(forced_sale_check(
            [{"funding_source": "harvest_trim", "ticker": "DDD", "trim_weight": 0.0375, "rating": "hold"}],
            base_w, {**adj_w, "DDD": 0.0}, harvest_cap=0.25, intact_ratings=intact,
        )),
    }
    for name, ok in checks.items():
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    return 0 if all(checks.values()) else 1


def main() -> int:  # noqa: C901
    configure_utc_logging()
    args = parse_args()
    if args.selftest:
        return selftest()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    runs_root = paths.output_dir / "runs"
    run_as_of = args.as_of or latest_run_with(runs_root, "stocks_scores.csv") or date.today().isoformat()
    run_dir = runs_root / run_as_of

    pc = cfg_get(config, "payout", {}) or {}
    enabled_in_production = bool(pc.get("enabled_in_production", False))
    target_usd = float(pc.get("target_payout_usd", 15_000.0))
    range_fraction = float(pc.get("payout_range_fraction", 0.20))
    min_buffer_fraction = float(pc.get("min_cash_buffer_fraction", 0.02))
    harvest_cap = float(pc.get("harvest_max_per_name_fraction", 0.25))
    intact_ratings = {str(r).strip().lower() for r in (pc.get("thesis_intact_ratings")
                      or ["strong_buy", "buy", "hold"])}

    cost_manifest_path = run_dir / "costs" / "cost_manifest.json"
    cost_book_path = run_dir / "costs" / "cost_adjusted_target_weights.csv"
    trade_meta_path = run_dir / "costs" / "trade_list_meta.json"
    trade_list_path = run_dir / "costs" / "trade_list.csv"
    weights_path = run_dir / "optimizer" / "target_weights.csv"
    for required in (cost_manifest_path, cost_book_path, trade_meta_path, weights_path):
        if not required.exists():
            LOGGER.error("Required input missing (run Stage 3+4 first): %s", required)
            return 1
    cost_manifest = read_manifest(cost_manifest_path)
    cost_seal_errors = sealed_artifact_errors(
        cost_manifest,
        cost_book_path,
        "cost_adjusted_target_weights.csv",
        run_as_of=run_as_of,
    )
    if cost_seal_errors:
        LOGGER.error("Stage 4 cost book is unsealed/stale: %s", cost_seal_errors)
        return 1

    # Book source must follow the SAME promotion rule as Stage 12a (20_compose): the exit-adjusted
    # book feeds payout only when exit_engine.apply_in_final=true. A sealed exits artifact that is
    # merely PRESENT stays shadow — otherwise payout keys its book_sha256 handshake to a book
    # Stage 12a never composes and promotion deadlocks.
    exits_in_final = bool(cfg_get(config, "exit_engine.apply_in_final", False))
    book_source = "costs/cost_adjusted_target_weights.csv"
    exits_book_path = run_dir / "exits" / "exit_adjusted_book.csv"
    exits_meta_path = run_dir / "exits" / "exit_adjusted_book_meta.json"
    if exits_in_final:
        if not exits_book_path.exists() or not exits_meta_path.exists():
            LOGGER.error(
                "exit_engine.apply_in_final=true but exits/exit_adjusted_book.csv or its meta is "
                "missing in this run; refusing")
            return 1
        exits_meta = read_manifest(exits_meta_path)
        exit_seal_errors = sealed_artifact_errors(
            exits_meta,
            exits_book_path,
            "exit_adjusted_book.csv",
            run_as_of=run_as_of,
        )
        if exit_seal_errors:
            LOGGER.error(
                "exit_engine.apply_in_final=true but the exit-adjusted book is unsealed/stale: %s",
                exit_seal_errors)
            return 1
        book_source = "exits/exit_adjusted_book.csv"
    if book_source.startswith("exits"):
        book = [{"ticker": r["ticker"], "weight": r.get("post_exit_weight", "0")}
                for r in read_csv(exits_book_path)]
    else:
        book = read_csv(cost_book_path)

    trade_meta = json.loads(trade_meta_path.read_text(encoding="utf-8"))
    aum = float(args.aum) if args.aum else _f(trade_meta.get("aum_usd"), 0.0)
    if aum <= 0:
        LOGGER.error("AUM unavailable (trade_list_meta aum_usd missing and no --aum)")
        return 1
    conviction = {r["ticker"]: r for r in read_csv(weights_path)}
    planned_sells_usd = sum(_f(r.get("trade_notional")) for r in read_csv(trade_list_path)
                            if str(r.get("side", "")).strip().lower() == "sell") if trade_list_path.exists() else 0.0

    out_dir = run_dir / "payout"
    plan_path = out_dir / "payout_plan.csv"
    book_path = out_dir / "payout_adjusted_book.csv"
    manifest_path = out_dir / "payout_manifest.json"
    if args.force:
        invalidate_dependents(run_dir, "payout")
    try:
        fail_if_exists([plan_path, book_path, manifest_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    plan, adjusted, summary = plan_payout(
        book, conviction, aum=aum, target_usd=target_usd, range_fraction=range_fraction,
        min_buffer_fraction=min_buffer_fraction, harvest_cap=harvest_cap, intact_ratings=intact_ratings,
    )
    base_w = {str(r["ticker"]).strip(): _f(r.get("weight")) for r in book}
    adj_w = {str(r["ticker"]).strip(): _f(r.get("weight")) for r in adjusted}
    violations = forced_sale_check(plan, base_w, adj_w, harvest_cap=harvest_cap, intact_ratings=intact_ratings)
    band_low, band_high = summary["payout_band_usd"]

    checks = [
        {"check": "payout_funded_within_range",
         "status": "PASS" if band_low - 1e-6 <= summary["payout_usd"] <= band_high + 1e-6 else "FAIL",
         "detail": f"payout={summary['payout_usd']} band=[{band_low}, {band_high}]"},
        {"check": "buffer_floor_after_payout",
         "status": "PASS" if summary["post_payout_cash_usd"] >= summary["min_buffer_usd"] - 1e-6 else "FAIL",
         "detail": f"post_cash={summary['post_payout_cash_usd']} floor={summary['min_buffer_usd']}"},
        {"check": "forced_sale_detector",
         "status": "PASS" if not violations else "FAIL",
         "detail": "no over-cap trims, no intact-thesis liquidations" if not violations else str(violations[:5])},
        {"check": "conservation_weights_sum",
         "status": "PASS" if abs(summary["gross_after"] - summary["gross_before"]) < 1e-8 else "FAIL",
         "detail": f"gross {summary['gross_before']} -> {summary['gross_after']} (incl {RESERVED_TICKER})"},
        {"check": "shadow_only_unregistered_arm",
         "status": "PASS" if not enabled_in_production else "FAIL",
         "detail": ("proposal only; +payout arm unregistered for the first Open Event"
                    if not enabled_in_production else
                    "payout.enabled_in_production=true without a protocol amendment + lockbox cycle")},
    ]
    acceptance = "PASS" if all(c["status"] == "PASS" for c in checks) else "FAIL"

    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(plan_path, PLAN_FIELDS, plan)
    write_csv(book_path, BOOK_FIELDS, adjusted)
    write_manifest(manifest_path, {
        "stage": "stage10_payout_liability",
        "generated_at": utc_now(),
        "run_as_of": run_as_of,
        "acceptance": acceptance,
        "shadow_only": True,
        "enabled_in_production": enabled_in_production,
        "cadence": str(pc.get("cadence", "quarterly")),
        "book_source": book_source,
        "cost_manifest_sha256": sha256_file(cost_manifest_path),
        "book_sha256": sha256_file(exits_book_path if book_source.startswith("exits") else cost_book_path),
        "planned_sells_observed_usd": round(planned_sells_usd, 2),
        "summary": summary,
        "checks": checks,
        "inputs_sha256": {
            "cost_manifest.json": sha256_file(cost_manifest_path),
            "book_source": sha256_file(exits_book_path if book_source.startswith("exits") else cost_book_path),
            "target_weights.csv": sha256_file(weights_path),
            "trade_list_meta.json": sha256_file(trade_meta_path),
            **({"exit_adjusted_book_meta.json": sha256_file(exits_meta_path)}
               if book_source.startswith("exits") else {}),
        },
        "outputs_sha256": {
            "payout_plan.csv": sha256_file(plan_path),
            "payout_adjusted_book.csv": sha256_file(book_path),
        },
    })
    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    LOGGER.info("PAYOUT PLAN (%s): payout=%.2f from cash=%.2f + harvest=%.2f across %d names -> %s",
                acceptance, summary["payout_usd"], summary["cash_buffer_usd"],
                summary["harvested_usd"], summary["n_harvested_names"], out_dir)
    return 0 if acceptance == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

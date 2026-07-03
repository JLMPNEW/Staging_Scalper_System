#!/usr/bin/env python3
"""Stage 4 - build the trade list from prior (cash default) -> Stage 3 target weights.

First build: prior = cash, so every target position is a one-way BUY. Rebalance: pass --prior-weights.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.costs.cost_common import (  # noqa: E402
    finite_float, invalidate_after_trade_list, prior_fingerprint, resolve_aum,
)
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("build_trade_list")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
TRADE_FIELDS = ["ticker", "prior_weight", "target_weight", "delta_weight", "side", "trade_notional", "n_orders"]
EPS = 1e-12


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build the Stage 4 trade list.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, default=None)
    p.add_argument("--aum", type=float, default=None)
    p.add_argument("--prior-weights", type=Path, default=None, help="CSV ticker,weight (default: cash = all 0)")
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def load_weight_map(path: Path) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in read_csv(path):
        t = str(r.get("ticker", "")).strip()
        if t and t.upper() != "CASH":
            raw_weight = r.get("weight")
            if raw_weight in (None, ""):
                raw_weight = 0.0  # blank cell = no prior position (finite_float raises on blank)
            weight = finite_float(raw_weight, name=f"{path}:{t}.weight")
            if weight < 0:
                raise ValueError(f"Prior weight for {t} must be non-negative, got {weight}")
            if t in out:
                raise ValueError(f"Duplicate prior-weight ticker: {t}")
            out[t] = weight
    return out


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
    target_path = run_dir / "optimizer" / "target_weights.csv"
    opt_manifest = run_dir / "optimizer" / "optimizer_manifest.json"
    if not (target_path.exists() and opt_manifest.exists()):
        LOGGER.error("Need a validated Stage 3 book (target_weights.csv + optimizer_manifest.json)")
        return 1
    opt = json.loads(opt_manifest.read_text(encoding="utf-8"))
    target_hash = sha256_file(target_path)
    manifest_target_hash = (opt.get("provenance_sha256") or {}).get("target_weights.csv")
    if opt.get("acceptance") != "PASS" or target_hash != manifest_target_hash:
        LOGGER.error(
            "Stage 3 book is not sealed/current: acceptance=%s target_hash_match=%s",
            opt.get("acceptance"), target_hash == manifest_target_hash,
        )
        return 1
    costs_dir = run_dir / "costs"
    trade_path = costs_dir / "trade_list.csv"
    meta_path = costs_dir / "trade_list_meta.json"
    if args.force:
        invalidate_after_trade_list(costs_dir)
    try:
        fail_if_exists([trade_path, meta_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    target: dict[str, float] = {}
    for r in read_csv(target_path):
        ticker = str(r.get("ticker", "")).strip()
        weight = finite_float(r.get("weight"), name=f"target_weights:{ticker}.weight")
        if weight < 0:
            LOGGER.error("Target weight for %s is negative: %s", ticker, weight)
            return 1
        if weight > 0:
            if ticker in target:
                LOGGER.error("Duplicate target ticker in Stage 3 book: %s", ticker)
                return 1
            target[ticker] = weight
    if args.prior_weights:
        prior_path = args.prior_weights.expanduser().resolve()
        try:
            prior = load_weight_map(prior_path)
        except ValueError as exc:
            LOGGER.error("%s", exc)
            return 1
        prior_id = prior_fingerprint(prior_path, sha256_file)
    else:
        prior = {}
        prior_id = prior_fingerprint(None, sha256_file)
    prior_source = str(prior_id["prior_source"])

    rows = []
    for ticker in sorted(set(target) | set(prior)):
        tw = target.get(ticker, 0.0)
        pw = prior.get(ticker, 0.0)
        delta = tw - pw
        if abs(delta) <= EPS:
            continue
        rows.append({
            "ticker": ticker, "prior_weight": round(pw, 10), "target_weight": round(tw, 10),
            "delta_weight": round(delta, 10), "side": "buy" if delta > 0 else "sell",
            "trade_notional": round(abs(delta) * aum, 4), "n_orders": 1,
        })
    n = write_csv(trade_path, TRADE_FIELDS, rows)
    meta = {
        "run_as_of": run_as_of, "aum_usd": aum, "prior_source": prior_source,
        "prior_weights_sha256": prior_id["prior_weights_sha256"],
        "target_weights_sha256": target_hash,
        "optimizer_manifest_sha256": sha256_file(opt_manifest),
        "n_trades": n, "n_orders": sum(r["n_orders"] for r in rows),
        "n_buys": sum(1 for r in rows if r["side"] == "buy"),
        "n_sells": sum(1 for r in rows if r["side"] == "sell"),
        "gross_traded_notional": round(sum(r["trade_notional"] for r in rows), 2),
    }
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    write_manifest(meta_path, meta)
    LOGGER.info("Trade list: %d trades (%d buys / %d sells) vs prior=%s, AUM=$%.0f -> %s",
                n, meta["n_buys"], meta["n_sells"], prior_source, aum, trade_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

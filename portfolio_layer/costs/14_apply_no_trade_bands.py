#!/usr/bin/env python3
"""Stage 4 - apply economic-position and no-trade filters, routing residual to CASH.

First build: new positions below the AUM-aware minimum economic size are dropped to CASH.
Rebalance: cost-only execution is the default until Stage 11 calibrates score snapshots to realized
forward returns. A provisional mu-based utility gate exists behind an explicit config flag only.
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

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.costs.cost_common import (  # noqa: E402
    decision_commission,
    finite_float,
    invalidate_after_overlay,
    prior_fingerprint,
    require_same_aum,
    require_same_prior,
    resolve_aum,
)
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("apply_no_trade_bands")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DECISION_FIELDS = [
    "ticker", "prior_weight", "target_weight", "decision", "reason", "position_notional",
    "commission_fraction", "utility_gain", "cost_drag", "applied_weight", "budget_scale",
]


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Apply Stage 4 no-trade bands + CASH residual.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, default=None)
    p.add_argument("--aum", type=float, default=None)
    p.add_argument("--prior-weights", type=Path, default=None)
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def load_prior(path: Path | None) -> dict[str, float]:
    if not path:
        return {}
    out: dict[str, float] = {}
    for r in read_csv(path):
        ticker = str(r.get("ticker", "")).strip()
        if not ticker or ticker.upper() == "CASH":
            continue
        raw_weight = r.get("weight")
        if raw_weight in (None, ""):
            raw_weight = 0.0  # blank cell = no prior position (finite_float raises on blank)
        weight = finite_float(raw_weight, name=f"{path}:{ticker}.weight")
        if weight < 0:
            raise ValueError(f"Prior weight for {ticker} must be non-negative, got {weight}")
        if ticker in out:
            raise ValueError(f"Duplicate prior-weight ticker: {ticker}")
        out[ticker] = weight
    return out


def period_utility(weights: np.ndarray, mu: np.ndarray, cov: np.ndarray, *, gamma: float, k: float) -> float:
    return float(k * (mu @ weights - 0.5 * gamma * weights @ cov @ weights))


def utility_gain_for_delta(
    *,
    ticker: str,
    target_weight: float,
    prior_weight: float,
    current_final: dict[str, float],
    names: list[str],
    mu_vec: np.ndarray,
    cov: np.ndarray,
    gamma: float,
    k: float,
) -> float | None:
    if ticker not in names:
        return None
    w_execute = np.array([current_final.get(t, 0.0) for t in names], dtype=float)
    w_suppress = w_execute.copy()
    idx = names.index(ticker)
    w_execute[idx] = target_weight
    w_suppress[idx] = prior_weight
    return period_utility(w_execute, mu_vec, cov, gamma=gamma, k=k) - \
        period_utility(w_suppress, mu_vec, cov, gamma=gamma, k=k)


def main() -> int:  # noqa: C901
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
    opt_dir = run_dir / "optimizer"
    costs_dir = run_dir / "costs"
    target_path = opt_dir / "target_weights.csv"
    cost_report_path = costs_dir / "cost_report.csv"
    trade_meta_path = costs_dir / "trade_list_meta.json"
    summary_path = costs_dir / "cost_summary.json"
    if not (target_path.exists() and cost_report_path.exists() and trade_meta_path.exists() and summary_path.exists()):
        LOGGER.error("Run 09/12/13 first (need target weights, cost report, trade meta, and cost summary)")
        return 1

    trade_meta = json.loads(trade_meta_path.read_text(encoding="utf-8"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    prior_path = args.prior_weights.expanduser().resolve() if args.prior_weights else None
    try:
        require_same_aum(aum, trade_meta.get("aum_usd"), source="trade_list_meta.json")
        require_same_aum(aum, summary.get("aum_usd"), source="cost_summary.json")
        require_same_prior(prior_fingerprint(prior_path, sha256_file), trade_meta)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1

    adjusted_path = costs_dir / "cost_adjusted_target_weights.csv"
    decisions_path = costs_dir / "no_trade_decisions.csv"
    if args.force:
        invalidate_after_overlay(costs_dir)
    try:
        fail_if_exists([adjusted_path, decisions_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    try:
        gross = finite_float(cfg_get(config, "optimizer.gross_exposure", 1.0), name="optimizer.gross_exposure")
        gamma = finite_float(cfg_get(config, "optimizer.risk_aversion", 5.0), name="optimizer.risk_aversion")
        horizon = int(finite_float(cfg_get(config, "transaction_costs.rebalance_horizon_days", 21),
                                   name="transaction_costs.rebalance_horizon_days"))
        min_frac = finite_float(cfg_get(config, "transaction_costs.min_position_commission_fraction", 0.005),
                                name="transaction_costs.min_position_commission_fraction")
        buffer = finite_float(cfg_get(config, "transaction_costs.no_trade_buffer_drag", 0.0),
                              name="transaction_costs.no_trade_buffer_drag")
        cash_target_fraction = finite_float(cfg_get(config, "transaction_costs.cash_target_fraction", 0.0),
                                            name="transaction_costs.cash_target_fraction")
        enable_mu_gate = bool(cfg_get(config, "transaction_costs.enable_provisional_mu_no_trade", False))
        comm_dec = decision_commission(config)
        prior = load_prior(prior_path)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1
    if gross <= 0 or horizon <= 0 or min_frac < 0:
        LOGGER.error("Invalid no-trade config: gross=%s horizon=%s min_frac=%s", gross, horizon, min_frac)
        return 1
    if not 0.0 <= cash_target_fraction < 1.0:
        LOGGER.error("transaction_costs.cash_target_fraction must be in [0,1), got %s", cash_target_fraction)
        return 1
    k = horizon / 252.0
    is_first_build = not prior

    target: dict[str, float] = {}
    mu_used: dict[str, float] = {}
    for r in read_csv(target_path):
        ticker = str(r.get("ticker", "")).strip()
        if not ticker:
            continue
        weight = finite_float(r.get("weight"), name=f"target_weights:{ticker}.weight")
        mu = finite_float(r.get("mu_used"), name=f"target_weights:{ticker}.mu_used")
        if weight < 0:
            LOGGER.error("Negative target weight for %s: %s", ticker, weight)
            return 1
        mu_used[ticker] = mu
        if weight > 0:
            target[ticker] = weight

    cost_by_ticker: dict[str, float] = {}
    for r in read_csv(cost_report_path):
        ticker = str(r.get("ticker", "")).strip()
        cost_by_ticker[ticker] = finite_float(r.get("total_cost_worst"), name=f"cost_report:{ticker}.total_cost_worst")

    covariance = pd.read_csv(opt_dir.parent / "risk" / "covariance.csv", index_col=0)
    covariance.index = [str(i) for i in covariance.index]
    covariance.columns = [str(c) for c in covariance.columns]
    utility_names = sorted((set(target) | set(prior)).intersection(set(covariance.index)))
    mu_vec = np.array([mu_used.get(t, 0.0) for t in utility_names], dtype=float)
    cov = covariance.loc[utility_names, utility_names].to_numpy(dtype=float) if utility_names else np.zeros((0, 0))

    final: dict[str, float] = dict(target)
    decisions: list[dict] = []
    for ticker in sorted(set(target) | set(prior)):
        tw = target.get(ticker, 0.0)
        pw = prior.get(ticker, 0.0)
        delta = tw - pw
        if abs(delta) <= 1e-12:
            final[ticker] = tw
            continue
        notional = abs(delta) * aum
        if ticker not in cost_by_ticker:
            LOGGER.error("Missing cost_report row for trade ticker %s", ticker)
            return 1
        cost_drag = cost_by_ticker[ticker] / aum

        if pw <= 0.0 and tw > 0.0:
            commission_fraction = comm_dec / notional if notional > 0 else float("inf")
            if commission_fraction > min_frac:
                final[ticker] = 0.0
                decisions.append({
                    "ticker": ticker, "prior_weight": 0.0, "target_weight": round(tw, 10),
                    "decision": "drop_to_cash", "reason": "below_min_economic_position",
                    "position_notional": round(notional, 2), "commission_fraction": round(commission_fraction, 6),
                })
                continue
            if is_first_build:
                final[ticker] = tw
                decisions.append({
                    "ticker": ticker, "prior_weight": 0.0, "target_weight": round(tw, 10),
                    "decision": "open", "reason": "economic_position",
                    "position_notional": round(notional, 2), "commission_fraction": round(commission_fraction, 6),
                })
                continue

        if not enable_mu_gate:
            final[ticker] = tw
            decisions.append({
                "ticker": ticker,
                "prior_weight": round(pw, 10),
                "target_weight": round(tw, 10),
                "decision": "execute",
                "reason": "rebalance_mu_gate_deferred_stage11",
                "position_notional": round(notional, 2),
                "cost_drag": round(cost_drag, 8),
            })
            continue

        gain = utility_gain_for_delta(
            ticker=ticker,
            target_weight=tw,
            prior_weight=pw,
            current_final=final,
            names=utility_names,
            mu_vec=mu_vec,
            cov=cov,
            gamma=gamma,
            k=k,
        )
        if gain is None:
            # A prior-only name absent from the sealed covariance cannot be evaluated. Never let
            # that uncertainty suppress a de-risking sale or authorize a risk increase.
            if tw <= pw:
                final[ticker] = tw
                decision = "execute"
                reason = "risk_unknown_forces_de_risk"
            else:
                final[ticker] = pw
                decision = "suppress_keep_prior"
                reason = "risk_unknown_blocks_increase"
            decisions.append({
                "ticker": ticker, "prior_weight": round(pw, 10), "target_weight": round(tw, 10),
                "decision": decision, "reason": reason,
                "position_notional": round(notional, 2), "utility_gain": "",
                "cost_drag": round(cost_drag, 8),
            })
            continue
        if gain > cost_drag + buffer:
            final[ticker] = tw
            decisions.append({
                "ticker": ticker, "prior_weight": round(pw, 10), "target_weight": round(tw, 10),
                "decision": "execute", "reason": "utility_gain_beats_cost",
                "position_notional": round(notional, 2), "utility_gain": round(gain, 8),
                "cost_drag": round(cost_drag, 8),
            })
        else:
            final[ticker] = pw
            decisions.append({
                "ticker": ticker, "prior_weight": round(pw, 10), "target_weight": round(tw, 10),
                "decision": "suppress_keep_prior", "reason": "utility_gain_below_cost",
                "position_notional": round(notional, 2), "utility_gain": round(gain, 8),
                "cost_drag": round(cost_drag, 8),
            })

    asset_sum = sum(final.values())
    cash_weight = gross - asset_sum
    budget_scale = 1.0
    if cash_weight < -1e-8:
        # Suppressed sells kept prior weights above the gross budget (possible on a rebalance whose
        # target de-grosses the book). Scale the whole asset block back to the budget — a pure
        # proportional de-risking that keeps every no-trade decision, recorded per name.
        scale = gross / asset_sum
        budget_scale = scale
        LOGGER.warning(
            "Suppressed sells over-invest the book (assets=%.10f > gross=%.10f); scaling all "
            "positions by %.8f to restore the budget", asset_sum, gross, scale,
        )
        final = {t: w * scale for t, w in final.items()}
        for d in decisions:
            if d["ticker"] in final:
                d["reason"] = f"{d['reason']};budget_rescale={scale:.8f}"
        asset_sum = sum(final.values())
        cash_weight = gross - asset_sum

    # deployable-book cash policy 2026-07-20: hold a fixed CASH buffer. Scale the whole asset block by
    # (1 - cash_target_fraction) and route the freed weight (the target buffer plus rounding dust) to
    # CASH. The scale is folded into budget_scale so the Stage-4 decision gate
    # (applied_weight == pre_scale * budget_scale) and the assets+cash == gross conservation gate both
    # still hold exactly. Optimizer gross stays 1.0; only the deployable book carries the buffer.
    if cash_target_fraction > 0.0:
        keep = 1.0 - cash_target_fraction
        final = {t: w * keep for t, w in final.items()}
        budget_scale *= keep
        for d in decisions:
            if final.get(str(d["ticker"]), 0.0) > 0.0:
                d["reason"] = f"{d['reason']};cash_buffer={cash_target_fraction:.8f}"
        asset_sum = sum(final.values())
        cash_weight = gross - asset_sum
    if abs(cash_weight) <= 1e-10:
        cash_weight = 0.0

    for decision in decisions:
        decision["applied_weight"] = round(final.get(str(decision["ticker"]), 0.0), 10)
        decision["budget_scale"] = round(budget_scale, 10)

    # CASH closes the book to EXACTLY gross against the PUBLISHED (rounded) asset weights, so the
    # downstream conservation gates (costs/15, orchestration/20) see assets+cash == gross with no dust.
    asset_rows = [{"ticker": t, "weight": round(w, 10)} for t, w in sorted(final.items()) if w > 0]
    cash_weight = round(gross - sum(r["weight"] for r in asset_rows), 10)
    rows = asset_rows + [{"ticker": "CASH", "weight": cash_weight}]
    write_csv(adjusted_path, ["ticker", "weight"], rows)
    write_csv(decisions_path, DECISION_FIELDS, sorted(decisions, key=lambda r: r["ticker"]))

    n_dropped = sum(1 for d in decisions if d["decision"] == "drop_to_cash")
    n_suppressed = sum(1 for d in decisions if d["decision"] == "suppress_keep_prior")
    LOGGER.info("No-trade overlay (%s): %d positions, %d dropped-to-cash, %d suppressed; cash_weight=%.4f -> %s",
                "first_build" if is_first_build else "rebalance",
                len([r for r in rows if r["ticker"] != "CASH"]), n_dropped, n_suppressed, cash_weight, adjusted_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

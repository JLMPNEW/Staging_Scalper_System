"""Shared Stage 4 helpers: AUM/commission resolution, lineage checks, and artifact cleanup."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from portfolio_layer.core.config import cfg_get
from portfolio_layer.core.artifacts import unlink_if_exists
from portfolio_layer.core.contracts import sha256_file
from portfolio_layer.risk.liquidity import (
    configured_fallback_half_spread_bps,
    effective_spread_uses_panel,
    liquidity_half_spread_fail_bps,
    load_spread_snapshot,
)


def finite_float(value: Any, *, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric, got {value!r}") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return parsed


def resolve_aum(config: dict[str, Any], cli_aum: float | None) -> float:
    """AUM is required; CLI overrides config. No silent default (flat commission is AUM-dependent)."""
    aum = cli_aum if cli_aum is not None else cfg_get(config, "transaction_costs.aum_usd", None)
    if aum is None:
        raise ValueError("AUM is required: pass --aum or set transaction_costs.aum_usd in config")
    aum = finite_float(aum, name="AUM")
    if aum <= 0:
        raise ValueError(f"AUM must be positive, got {aum}")
    return aum


def commission(config: dict[str, Any], which: str = "base") -> float:
    block = cfg_get(config, "transaction_costs.commission_per_order", {}) or {}
    value = block.get(which)
    if value is None:
        raise ValueError(f"transaction_costs.commission_per_order.{which} is required")
    parsed = finite_float(value, name=f"transaction_costs.commission_per_order.{which}")
    if parsed < 0:
        raise ValueError(f"transaction_costs.commission_per_order.{which} must be non-negative, got {parsed}")
    return parsed


def decision_commission(config: dict[str, Any]) -> float:
    which = str(cfg_get(config, "transaction_costs.decision_commission", "worst_case"))
    return commission(config, which)


def load_spread_inputs(config: dict[str, Any], run_dir: Path, default_half_spread_bps: float) -> dict[str, Any]:
    """Resolve the active spread source shared by Stage 4 and BL cost overlays."""
    use_panel = effective_spread_uses_panel(config, run_dir)
    allow_fallback = bool(cfg_get(config, "liquidity_panel.allow_fallback_to_default", True))
    fallback_half_spread_bps = (
        configured_fallback_half_spread_bps(config) if use_panel else default_half_spread_bps
    )
    max_half_spread_bps = liquidity_half_spread_fail_bps(config)
    if not use_panel:
        return {
            "mode": "config_default",
            "snapshot_path": None,
            "snapshot_sha256": "",
            "snapshot": {},
            "allow_fallback": True,
            "default_half_spread_bps": default_half_spread_bps,
            "fallback_half_spread_bps": fallback_half_spread_bps,
            "max_half_spread_bps": max_half_spread_bps,
        }
    snapshot_path = run_dir / "risk" / "spread_snapshot.csv"
    if not snapshot_path.exists():
        raise ValueError(
            f"Enhanced liquidity spread source requested but missing {snapshot_path}. "
            "Run risk/05c_collect_ib_historical_spread_samples.py first or set transaction_costs.spread_source=config_default."
        )
    snapshot = load_spread_snapshot(snapshot_path)
    if not snapshot:
        raise ValueError(f"Enhanced liquidity spread source requested but {snapshot_path} is empty")
    return {
        "mode": "liquidity_panel",
        "snapshot_path": snapshot_path,
        "snapshot_sha256": sha256_file(snapshot_path),
        "snapshot": snapshot,
        "allow_fallback": allow_fallback,
        "default_half_spread_bps": default_half_spread_bps,
        "fallback_half_spread_bps": fallback_half_spread_bps,
        "max_half_spread_bps": max_half_spread_bps,
    }


def half_spread_for_ticker(ticker: str, spread_inputs: dict[str, Any]) -> dict[str, Any]:
    default_bps = float(spread_inputs["default_half_spread_bps"])
    fallback_bps = float(spread_inputs["fallback_half_spread_bps"])
    max_bps = float(spread_inputs["max_half_spread_bps"])
    if spread_inputs["mode"] != "liquidity_panel":
        return {
            "half_spread_bps": default_bps,
            "source": "config_default",
            "status": "config_default",
            "reason": "liquidity_panel_disabled",
        }
    row = spread_inputs["snapshot"].get(str(ticker).strip().upper())
    if row is None:
        if spread_inputs["allow_fallback"]:
            return {
                "half_spread_bps": fallback_bps,
                "source": "config_default",
                "status": "fallback",
                "reason": "missing_spread_snapshot_row",
            }
        raise ValueError(f"Missing spread snapshot row for {ticker}")
    status = str(row.get("spread_status", "")).strip().lower()
    source = str(row.get("spread_source", "")).strip() or "unknown"
    reason = str(row.get("spread_reason", "")).strip()
    if status == "failed" and not spread_inputs["allow_fallback"]:
        raise ValueError(f"Spread snapshot failed for {ticker}: {reason}")
    if status == "failed":
        return {
            "half_spread_bps": fallback_bps,
            "source": "config_default",
            "status": "fallback",
            "reason": reason or "spread_snapshot_failed",
        }
    try:
        bps = finite_float(row.get("median_half_spread_bps"), name=f"spread_snapshot:{ticker}.median_half_spread_bps")
    except ValueError:
        if spread_inputs["allow_fallback"]:
            return {
                "half_spread_bps": fallback_bps,
                "source": "config_default",
                "status": "fallback",
                "reason": "invalid_spread_snapshot_value",
            }
        raise
    if bps < 0:
        raise ValueError(f"spread_snapshot:{ticker}.median_half_spread_bps must be non-negative, got {bps}")
    if bps >= max_bps:
        raise ValueError(
            f"spread_snapshot:{ticker}.median_half_spread_bps={bps} meets/exceeds hard limit {max_bps}"
        )
    return {
        "half_spread_bps": bps,
        "source": source,
        "status": status or "ok",
        "reason": reason,
    }


def same_money(a: float, b: float, *, tol: float = 0.005) -> bool:
    """Compare dollar values to the nearest cent tolerance."""
    return abs(float(a) - float(b)) <= tol


def require_same_aum(actual: float, expected: Any, *, source: str) -> None:
    expected_f = finite_float(expected, name=f"{source}.aum_usd")
    if not same_money(actual, expected_f):
        raise ValueError(f"AUM mismatch: current={actual} vs {source}={expected_f}. Re-run upstream Stage 4 steps.")


def prior_fingerprint(path: Path | None, sha256_file) -> dict[str, str | None]:
    """Return a stable prior-book identity for cross-step consistency checks."""
    if path is None:
        return {"prior_source": "cash", "prior_weights_sha256": None}
    resolved = path.expanduser().resolve()
    return {"prior_source": str(resolved), "prior_weights_sha256": sha256_file(resolved)}


def require_same_prior(current: dict[str, str | None], recorded: dict[str, Any]) -> None:
    recorded_source = recorded.get("prior_source")
    recorded_hash = recorded.get("prior_weights_sha256")
    if current["prior_source"] == "cash":
        if recorded_source != "cash" or recorded_hash not in (None, ""):
            raise ValueError("Prior mismatch: current prior=cash but trade list was built from a prior-weights file")
        return
    if recorded_source == "cash":
        raise ValueError("Prior mismatch: current prior-weights file provided but trade list was built from cash")
    if current["prior_weights_sha256"] != recorded_hash:
        raise ValueError("Prior mismatch: prior-weights sha256 differs from trade_list_meta.json")


def clear_validation_dir(costs_dir: Path) -> None:
    validation_dir = costs_dir / "validation"
    if validation_dir.exists():
        for path in validation_dir.iterdir():
            if path.is_file():
                path.unlink()


def invalidate_bl_final_after_baseline_cost_change(costs_dir: Path) -> None:
    """Clear final Stage 7 seal artifacts that depend on the Stage 4 baseline cost seal."""
    bl_dir = costs_dir.parent / "blacklitterman"
    unlink_if_exists([
        bl_dir / "bl_manifest.json",
        bl_dir / "bl_net_static_replay_metrics.json",
        bl_dir / "validation" / "bl_fusion_validation.csv",
    ])


def invalidate_after_trade_list(costs_dir: Path) -> None:
    unlink_if_exists([
        costs_dir / "cost_report.csv",
        costs_dir / "cost_summary.json",
        costs_dir / "cost_adjusted_target_weights.csv",
        costs_dir / "no_trade_decisions.csv",
        costs_dir / "cost_manifest.json",
        costs_dir / "net_static_replay_metrics.json",
    ])
    clear_validation_dir(costs_dir)
    invalidate_bl_final_after_baseline_cost_change(costs_dir)


def invalidate_after_cost_model(costs_dir: Path) -> None:
    unlink_if_exists([
        costs_dir / "cost_adjusted_target_weights.csv",
        costs_dir / "no_trade_decisions.csv",
        costs_dir / "cost_manifest.json",
        costs_dir / "net_static_replay_metrics.json",
    ])
    clear_validation_dir(costs_dir)
    invalidate_bl_final_after_baseline_cost_change(costs_dir)


def invalidate_after_overlay(costs_dir: Path) -> None:
    unlink_if_exists([
        costs_dir / "cost_manifest.json",
        costs_dir / "net_static_replay_metrics.json",
    ])
    clear_validation_dir(costs_dir)
    invalidate_bl_final_after_baseline_cost_change(costs_dir)


def invalidate_after_validation(costs_dir: Path) -> None:
    unlink_if_exists([costs_dir / "net_static_replay_metrics.json"])
    invalidate_bl_final_after_baseline_cost_change(costs_dir)

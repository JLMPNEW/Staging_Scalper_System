"""Shared Stage 4 helpers: AUM/commission resolution, lineage checks, and artifact cleanup."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from portfolio_layer.core.config import cfg_get
from portfolio_layer.core.artifacts import unlink_if_exists


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


def invalidate_after_cost_model(costs_dir: Path) -> None:
    unlink_if_exists([
        costs_dir / "cost_adjusted_target_weights.csv",
        costs_dir / "no_trade_decisions.csv",
        costs_dir / "cost_manifest.json",
        costs_dir / "net_static_replay_metrics.json",
    ])
    clear_validation_dir(costs_dir)


def invalidate_after_overlay(costs_dir: Path) -> None:
    unlink_if_exists([
        costs_dir / "cost_manifest.json",
        costs_dir / "net_static_replay_metrics.json",
    ])
    clear_validation_dir(costs_dir)


def invalidate_after_validation(costs_dir: Path) -> None:
    unlink_if_exists([costs_dir / "net_static_replay_metrics.json"])

"""Shared artifact cleanup helpers across portfolio-layer stages."""
from __future__ import annotations

from pathlib import Path


def unlink_if_exists(paths: list[Path]) -> None:
    for path in paths:
        if path.exists() and path.is_file():
            path.unlink()


def clear_dir_files(path: Path) -> None:
    if path.exists():
        for child in path.iterdir():
            if child.is_file():
                child.unlink()


def invalidate_risk_outputs_after_spread_change(risk_dir: Path) -> None:
    """Clear Stage 2 risk validation artifacts that depend on the spread snapshot."""
    unlink_if_exists([
        risk_dir / "liquidity_audit.csv",
        risk_dir / "liquidity_audit_by_sector.csv",
        risk_dir / "liquidity_audit_summary.json",
        risk_dir / "risk_manifest.json",
    ])
    clear_dir_files(risk_dir / "validation")


def invalidate_cost_outputs_after_spread_change(run_dir: Path) -> None:
    """Clear Stage 4 artifacts that depend on the liquidity spread snapshot."""
    costs_dir = run_dir / "costs"
    unlink_if_exists([
        costs_dir / "cost_report.csv",
        costs_dir / "cost_summary.json",
        costs_dir / "cost_adjusted_target_weights.csv",
        costs_dir / "no_trade_decisions.csv",
        costs_dir / "cost_manifest.json",
        costs_dir / "net_static_replay_metrics.json",
    ])
    clear_dir_files(costs_dir / "validation")


def invalidate_rotation_outputs_after_signal_change(rotation_dir: Path) -> None:
    """Clear Stage 5 validation/diagnostic artifacts that depend on rotation signals."""
    unlink_if_exists([
        rotation_dir / "rotation_manifest.json",
        rotation_dir / "rotation_ablation_metrics.json",
        rotation_dir / "rotation_ablation_weights.csv",
    ])
    clear_dir_files(rotation_dir / "validation")


def invalidate_rotation_outputs_after_validation(rotation_dir: Path) -> None:
    """Clear Stage 5 diagnostics that depend on the sealed rotation manifest."""
    unlink_if_exists([
        rotation_dir / "rotation_ablation_metrics.json",
        rotation_dir / "rotation_ablation_weights.csv",
    ])

"""Shared artifact cleanup helpers across portfolio-layer stages."""
from __future__ import annotations

from pathlib import Path


DEPENDENCIES: dict[str, set[str]] = {
    "scores": {"risk", "optimizer", "costs", "rotation", "macro", "blacklitterman", "sleeves", "exits", "payout", "governor", "final"},
    "risk": {"optimizer", "costs", "rotation", "macro", "blacklitterman", "sleeves", "exits", "payout", "governor", "final"},
    "liquidity": {"risk", "costs", "blacklitterman", "sleeves", "exits", "payout", "governor", "final"},
    "optimizer": {"costs", "rotation", "macro", "blacklitterman", "sleeves", "exits", "payout", "governor", "final"},
    "costs": {"blacklitterman", "sleeves", "exits", "payout", "governor", "final"},
    "rotation": {"blacklitterman", "sleeves", "exits", "final"},
    "macro": {"blacklitterman", "sleeves", "exits", "final"},
    "blacklitterman": {"sleeves", "exits", "final"},
    "sleeves": {"exits", "final"},
    "ledger": {"exits", "payout", "final"},
    "exits": {"payout", "final"},
    "payout": {"final"},
    "governor": {"final"},
    "earnings": {"final_report"},
    "monitor": {"final_report"},
    "levels": {"final_report"},
}

CONSUMER_FILES: dict[str, tuple[str, ...]] = {
    "risk": ("risk/risk_manifest.json", "risk/validation/risk_panel_validation.csv"),
    "optimizer": ("optimizer/optimizer_manifest.json", "optimizer/validation/optimizer_validation.csv"),
    "costs": (
        "costs/cost_manifest.json", "costs/net_static_replay_metrics.json",
        "costs/validation/cost_validation.csv",
    ),
    "rotation": (
        "rotation/rotation_manifest.json", "rotation/rotation_ablation_metrics.json",
        "rotation/rotation_ablation_weights.csv", "rotation/validation/rotation_validation.csv",
    ),
    "macro": ("macro/macro_manifest.json", "macro/validation/macro_contract_validation.csv"),
    "blacklitterman": (
        "blacklitterman/bl_manifest.json", "blacklitterman/bl_net_static_replay_metrics.json",
        "blacklitterman/validation/bl_fusion_validation.csv",
    ),
    "sleeves": (
        "sleeves/sleeve_manifest.json", "sleeves/validation/sleeve_validation.csv",
        "sleeves/validation/sleeve_framework_validation.csv",
        "sleeves/validation/risk_budget_validation.csv",
    ),
    "exits": (
        "exits/exit_manifest.json", "exits/exit_adjusted_book_meta.json",
        "exits/exit_adjusted_book.csv", "exits/validation/exit_validation.csv",
    ),
    "payout": (
        "payout/payout_manifest.json", "payout/payout_adjusted_book.csv",
        "payout/payout_plan.csv",
    ),
    "governor": ("governor/governor_manifest.json", "governor/gross_exposure_directive.json"),
    "final": (
        "final/final_weights_manifest.json",
        "final/final_target_weights.csv",
        "final/final_manifest.json",
        "final/final_target_book.csv",
        # The monitor bootstrap book (39_sync_monitor_universe prefers it when
        # present) must be invalidated with the deployable book: a forced
        # upstream rerun would otherwise leave a stale sealed bootstrap book.
        "final/bootstrap_target_weights.csv",
        "final/bootstrap_final_weights_manifest.json",
    ),
    "final_report": ("final/final_manifest.json", "final/final_target_book.csv"),
}


def invalidate_dependents(run_dir: Path, producer: str) -> list[Path]:
    """Invalidate every accepted artifact that transitively consumes `producer` outputs."""
    if producer not in DEPENDENCIES:
        raise ValueError(f"unknown artifact producer {producer!r}")
    removed: list[Path] = []
    for consumer in sorted(DEPENDENCIES[producer]):
        for relative in CONSUMER_FILES.get(consumer, ()):
            path = run_dir / relative
            if path.is_file():
                path.unlink()
                removed.append(path)
    # orchestration_meta.json is deliberately NOT touched here: the meta is owned
    # exclusively by 18_run_portfolio_pipeline (persisted RUNNING before each step
    # and re-persisted after), and deleting it from artifact invalidation destroyed
    # the original full-run provenance during recovery runs.
    return removed


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
    invalidate_dependents(risk_dir.parent, "liquidity")


def invalidate_cost_outputs_after_spread_change(run_dir: Path) -> None:
    """Clear Stage 4 artifacts that depend on the liquidity spread snapshot."""
    costs_dir = run_dir / "costs"
    bl_dir = run_dir / "blacklitterman"
    bl_costs_dir = bl_dir / "costs"
    unlink_if_exists([
        costs_dir / "cost_report.csv",
        costs_dir / "cost_summary.json",
        costs_dir / "cost_adjusted_target_weights.csv",
        costs_dir / "no_trade_decisions.csv",
        costs_dir / "cost_manifest.json",
        costs_dir / "net_static_replay_metrics.json",
        bl_costs_dir / "bl_trade_list.csv",
        bl_costs_dir / "bl_trade_list_meta.json",
        bl_costs_dir / "bl_cost_report.csv",
        bl_costs_dir / "bl_cost_summary.json",
        bl_costs_dir / "bl_cost_adjusted_target_weights.csv",
        bl_costs_dir / "bl_no_trade_decisions.csv",
        bl_costs_dir / "bl_cost_meta.json",
        bl_dir / "bl_manifest.json",
        bl_dir / "bl_net_static_replay_metrics.json",
        bl_dir / "validation" / "bl_fusion_validation.csv",
    ])
    clear_dir_files(costs_dir / "validation")
    invalidate_dependents(run_dir, "liquidity")


def invalidate_rotation_outputs_after_signal_change(rotation_dir: Path) -> None:
    """Clear Stage 5 validation/diagnostic artifacts that depend on rotation signals."""
    unlink_if_exists([
        rotation_dir / "rotation_manifest.json",
        rotation_dir / "rotation_ablation_metrics.json",
        rotation_dir / "rotation_ablation_weights.csv",
    ])
    clear_dir_files(rotation_dir / "validation")
    invalidate_dependents(rotation_dir.parent, "rotation")


def invalidate_rotation_outputs_after_validation(rotation_dir: Path) -> None:
    """Clear Stage 5 diagnostics that depend on the sealed rotation manifest."""
    unlink_if_exists([
        rotation_dir / "rotation_ablation_metrics.json",
        rotation_dir / "rotation_ablation_weights.csv",
    ])
    invalidate_dependents(rotation_dir.parent, "rotation")


def invalidate_macro_outputs_after_contract_change(macro_dir: Path) -> None:
    """Clear Stage 6 validation artifacts that depend on macro contract CSVs."""
    unlink_if_exists([
        macro_dir / "macro_manifest.json",
    ])
    clear_dir_files(macro_dir / "validation")
    invalidate_dependents(macro_dir.parent, "macro")

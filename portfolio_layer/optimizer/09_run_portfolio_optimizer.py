#!/usr/bin/env python3
"""Stage 3 - AQR-only baseline book: long-only mean-variance on the injected Stage 2 covariance.

Universe (LOCKED): Stage 1 investable, Stage 2 risk eligible, monitor-entry eligible,
role=scored, and ticker in covariance.csv.
Risk-ineligible eligible names are NOT sized (never zero-weighted inside the solver) and are surfaced in
risk_excluded_candidates.csv. mu_used = final_score * score_confidence (annualized expected alpha).
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

from portfolio_layer.core.artifacts import invalidate_dependents  # noqa: E402
from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    manifest_acceptance_value,
    read_csv,
    read_manifest,
    sealed_artifact_errors,
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.db import connect, finish_run, start_run  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_database_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.optimizer.optimizer_core import (  # noqa: E402
    constraint_aware_invested_gross,
    finalize_long_only_weights,
    finalize_with_group_caps,
    maximum_investable_gross,
    rescale_group_caps_for_invested_gross,
    snap_rounded_weights,
    solve_long_only_mv,
    weight_sensitivity_band,
)
from portfolio_layer.risk.liquidity import load_spread_snapshot  # noqa: E402
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("run_portfolio_optimizer")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
WEIGHT_FIELDS = [
    "ticker", "sector", "industry", "source_pipeline", "rating", "final_score", "score_confidence",
    "mu_raw", "mu_used", "weight", "weight_band_low", "weight_band_high",
]
EXCLUDED_FIELDS = [
    "ticker", "source_pipeline", "investable_eligible", "risk_eligible", "role",
    "risk_status", "monitor_entry_eligible", "monitor_internal_state",
    "monitor_action_state", "monitor_policy_reason", "exclusion_reason",
]


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build the Stage 3 AQR-only baseline book.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, default=None)
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--force", action="store_true")
    p.add_argument(
        "--monitor-overlay-mode",
        choices=("ignore", "required"),
        default="required",
        help=(
            "ignore is allowed only for the orchestrator's preliminary bootstrap solve; "
            "required is the deployable fail-closed default"
        ),
    )
    return p.parse_args()


def _f(value: object, default: float = 0.0) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
        return out if np.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def biotech_benchmark_overlay(
    scores: dict[str, dict[str, str]],
    universe: list[str],
    covariance: pd.DataFrame,
    mu_used: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, dict[str, object] | None]:
    """Map each biotech sleeve dollar to active stocks plus the frozen benchmark residual."""
    contract_rows = [
        row
        for ticker, row in scores.items()
        if ticker in universe
        and str(row.get("source_pipeline") or "").strip() == "biotech"
        and str(row.get("active_sleeve_weight") or "").strip()
    ]
    if not contract_rows:
        return mu_used, covariance.loc[universe, universe].to_numpy(dtype=float), None
    lineages = {
        (
            str(row.get("production_policy_id") or "").strip(),
            str(row.get("production_policy_sha256") or "").strip().lower(),
        )
        for row in contract_rows
    }
    if len(lineages) != 1:
        raise ValueError("Biotech adaptive sleeve contract has inconsistent policy lineage")
    policy_id, policy_sha256 = lineages.pop()
    if not policy_id or len(policy_sha256) != 64:
        raise ValueError("Biotech adaptive sleeve contract lacks immutable policy lineage")
    parsed_weights: dict[str, tuple[float, float]] = {}
    benchmark_tickers: set[str] = set()
    for ticker in universe:
        row = scores[ticker]
        if (
            str(row.get("source_pipeline") or "").strip() != "biotech"
            or not str(row.get("active_sleeve_weight") or "").strip()
        ):
            continue
        active_weight = round(_f(row.get("active_sleeve_weight"), -1.0), 10)
        name_weight_cap = _f(row.get("active_name_weight_cap"), -1.0)
        selected_name_count = _f(row.get("active_selected_name_count"), -1.0)
        residual_weight = round(_f(row.get("benchmark_residual_weight"), -1.0), 10)
        if not 0.0 <= active_weight <= 1.0 or not 0.0 <= residual_weight <= 1.0:
            raise ValueError("Biotech adaptive sleeve weights must be within [0, 1]")
        if abs(active_weight + residual_weight - 1.0) > 1e-9:
            raise ValueError("Biotech adaptive sleeve contract has inconsistent active and residual weights")
        if not 0.0 < name_weight_cap <= 1.0 or selected_name_count < 0.0:
            raise ValueError("Biotech adaptive sleeve contract has invalid breadth/name-cap fields")
        if active_weight - min(1.0, selected_name_count * name_weight_cap) > 1e-9:
            raise ValueError("Biotech active sleeve exceeds its selected-name capacity")
        benchmark_ticker = str(row.get("benchmark_residual_ticker") or "").strip().upper()
        if not benchmark_ticker:
            raise ValueError("Biotech adaptive sleeve contract lacks benchmark_residual_ticker")
        benchmark_tickers.add(benchmark_ticker)
        parsed_weights[ticker] = (active_weight, residual_weight)
    if len(benchmark_tickers) != 1:
        raise ValueError("Biotech adaptive sleeve contract has inconsistent benchmark tickers")
    benchmark_ticker = next(iter(benchmark_tickers))
    if benchmark_ticker in universe:
        raise ValueError(f"Biotech benchmark residual ticker duplicates the scored universe: {benchmark_ticker}")
    if benchmark_ticker not in covariance.index or benchmark_ticker not in covariance.columns:
        raise ValueError(f"Biotech benchmark residual lacks covariance support: {benchmark_ticker}")
    biotech_indices = [
        index
        for index, ticker in enumerate(universe)
        if str(scores[ticker].get("source_pipeline") or "").strip() == "biotech"
    ]
    if not biotech_indices:
        raise ValueError("Biotech adaptive sleeve contract is active but no biotech names reached the optimizer universe")
    underlying = [*universe, benchmark_ticker]
    transform = np.zeros((len(underlying), len(universe)), dtype=float)
    active_weights_by_index: dict[str, float] = {}
    residual_weights_by_index: dict[str, float] = {}
    for index, ticker in enumerate(universe):
        if index in biotech_indices:
            active_weight, residual_weight = parsed_weights.get(ticker, (1.0, 0.0))
            transform[index, index] = active_weight
            transform[-1, index] = residual_weight
            active_weights_by_index[str(index)] = active_weight
            residual_weights_by_index[str(index)] = residual_weight
        else:
            transform[index, index] = 1.0
    underlying_covariance = covariance.loc[underlying, underlying].to_numpy(dtype=float)
    transformed_covariance = transform.T @ underlying_covariance @ transform
    transformed_mu = np.asarray(mu_used, dtype=float).copy()
    for index in biotech_indices:
        transformed_mu[index] *= active_weights_by_index[str(index)]
    distinct_weights = set(parsed_weights.values())
    common_weights = next(iter(distinct_weights)) if len(distinct_weights) == 1 else None
    return transformed_mu, transformed_covariance, {
        "active_weight": common_weights[0] if common_weights is not None else None,
        "residual_weight": common_weights[1] if common_weights is not None else None,
        "benchmark_ticker": benchmark_ticker,
        "policy_id": policy_id,
        "policy_sha256": policy_sha256,
        "biotech_indices": biotech_indices,
        "active_weights_by_index": active_weights_by_index,
        "residual_weights_by_index": residual_weights_by_index,
        "active_name_weight_caps": sorted(
            {_f(row.get("active_name_weight_cap"), -1.0) for row in contract_rows}
        ),
        "active_selected_name_counts": sorted(
            {_f(row.get("active_selected_name_count"), -1.0) for row in contract_rows}
        ),
    }


def realize_biotech_benchmark_weights(
    universe: list[str],
    virtual_weights: np.ndarray,
    overlay: dict[str, object] | None,
) -> dict[str, float]:
    """Convert optimizer sleeve budgets into stock and benchmark holdings without changing gross."""
    realized = {ticker: float(virtual_weights[index]) for index, ticker in enumerate(universe)}
    if overlay is None:
        return realized
    benchmark_ticker = str(overlay["benchmark_ticker"])
    biotech_indices = [int(index) for index in overlay["biotech_indices"]]  # type: ignore[union-attr]
    active_by_index = overlay.get("active_weights_by_index")
    residual_by_index = overlay.get("residual_weights_by_index")
    if not isinstance(active_by_index, dict) or not isinstance(residual_by_index, dict):
        active_by_index = {str(index): float(str(overlay["active_weight"])) for index in biotech_indices}
        residual_by_index = {str(index): float(str(overlay["residual_weight"])) for index in biotech_indices}
    benchmark_weight = 0.0
    for index in biotech_indices:
        active_weight = float(str(active_by_index[str(index)]))
        residual_weight = float(str(residual_by_index[str(index)]))
        realized[universe[index]] = float(virtual_weights[index]) * active_weight
        benchmark_weight += float(virtual_weights[index]) * residual_weight
    realized[benchmark_ticker] = benchmark_weight
    if abs(sum(realized.values()) - float(np.sum(virtual_weights))) > 1e-9:
        raise ValueError("Biotech benchmark residual transformation changed portfolio gross")
    return realized


def load_json(path: Path) -> dict:    return json.loads(path.read_text(encoding="utf-8"))


def median_half_spread_bps(row: dict[str, str] | None) -> float | None:
    """Parse median_half_spread_bps from a spread_snapshot row; None when absent/blank/non-finite.

    A None result means the liquidity floor cannot judge the name and must fail open (never exclude).
    """
    if not row:
        return None
    raw = row.get("median_half_spread_bps")
    if raw in (None, ""):
        return None
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if np.isfinite(value) else None


def load_monitor_overlay(
    run_dir: Path,
    *,
    run_as_of: str,
    enabled: bool,
    mode: str,
) -> tuple[dict[str, dict[str, str]], dict[str, object]]:
    if mode == "ignore":
        return {}, {
            "status": "bootstrap_ignored",
            "enabled_in_production": enabled,
            "deployable": False,
            "n_excluded": 0,
        }
    if not enabled:
        raise ValueError(
            "--monitor-overlay-mode required but optimizer.monitor_entry_policy is disabled"
        )
    overlay_path = run_dir / "optimizer" / "monitor_eligibility_overlay.csv"
    manifest_path = run_dir / "optimizer" / "monitor_eligibility_manifest.json"
    if not overlay_path.is_file() or not manifest_path.is_file():
        raise FileNotFoundError(
            "Required monitor eligibility overlay is missing; run optimizer/08 first"
        )
    manifest = read_manifest(manifest_path)
    if (
        manifest_acceptance_value(manifest) != "PASS"
        or str(manifest.get("run_as_of", "")) != run_as_of
        or manifest.get("production_entry_gate") is not True
    ):
        raise ValueError("Monitor eligibility manifest is not same-date production PASS")
    errors = sealed_artifact_errors(
        manifest,
        overlay_path,
        overlay_path.name,
        run_as_of=run_as_of,
    )
    if errors:
        raise ValueError(f"Monitor eligibility overlay is not sealed/current: {errors}")
    overlay: dict[str, dict[str, str]] = {}
    for row in read_csv(overlay_path):
        ticker = str(row.get("ticker", "")).strip().upper()
        if not ticker or ticker in overlay:
            raise ValueError(
                f"Monitor eligibility overlay has blank/duplicate ticker: {ticker!r}"
            )
        if str(row.get("run_as_of", "")).strip() != run_as_of:
            raise ValueError(f"Monitor eligibility row is not same-date: {ticker}")
        overlay[ticker] = row
    return overlay, {
        "status": "applied",
        "enabled_in_production": True,
        "deployable": True,
        "overlay_path": str(overlay_path),
        "overlay_sha256": sha256_file(overlay_path),
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
        "policy": manifest.get("policy", {}),
        "n_excluded": 0,
    }


def stage1_config_binding(
    stage1_manifest: dict, config_path: Path
) -> tuple[bool, str]:
    expected = str(
        (((stage1_manifest.get("provenance") or {}).get("config_yaml") or {}).get("sha256"))
        or ""
    ).strip()
    actual = sha256_file(config_path) if config_path.is_file() else ""
    valid = bool(expected) and actual == expected
    return valid, f"expected={expected or '<missing>'}; actual={actual or '<missing>'}"


def stage3_readiness(
    run_dir: Path, risk_dir: Path, config_path: Path
) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []

    def rec(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    stage1_manifest_path = run_dir / "manifest.json"
    risk_manifest_path = risk_dir / "risk_manifest.json"
    scores_path = run_dir / "stocks_scores.csv"
    coverage_path = risk_dir / "risk_coverage.csv"
    cov_path = risk_dir / "covariance.csv"
    if not stage1_manifest_path.exists():
        rec("stage1_manifest_present", "FAIL", f"missing {stage1_manifest_path}")
        return checks
    if not risk_manifest_path.exists():
        rec("stage2_risk_manifest_present", "FAIL", f"missing {risk_manifest_path}")
        return checks
    stage1 = load_json(stage1_manifest_path)
    risk_manifest = load_json(risk_manifest_path)
    config_bound, config_detail = stage1_config_binding(stage1, config_path)
    rec(
        "config_hash_matches_stage1_manifest",
        "PASS" if config_bound else "FAIL",
        config_detail,
    )
    rec(
        "stage1_hard_gates_passed",
        "PASS" if stage1.get("hard_gate_acceptance") == "PASS" else "FAIL",
        f"hard_gate_acceptance={stage1.get('hard_gate_acceptance')}",
    )
    rec(
        "stage2_acceptance_passed",
        "PASS" if risk_manifest.get("acceptance") == "PASS" else "FAIL",
        f"acceptance={risk_manifest.get('acceptance')}",
    )
    expected_scores_hash = ((stage1.get("files") or {}).get("stocks_scores.csv") or {}).get("sha256")
    rec(
        "stocks_scores_hash_matches_stage1_manifest",
        "PASS" if scores_path.exists() and sha256_file(scores_path) == expected_scores_hash else "FAIL",
        "stocks_scores.csv hash matches Stage 1 manifest",
    )
    risk_files = risk_manifest.get("files") or {}
    for name, path in (("risk_coverage.csv", coverage_path), ("covariance.csv", cov_path)):
        expected = (risk_files.get(name) or {}).get("sha256")
        rec(
            f"{name}_hash_matches_stage2_manifest",
            "PASS" if path.exists() and sha256_file(path) == expected else "FAIL",
            f"{name} hash {'matches' if path.exists() and sha256_file(path) == expected else 'mismatch'}",
        )
    return checks


def readiness_passed(checks: list[dict[str, str]]) -> bool:
    return bool(checks) and all(c["status"] == "PASS" for c in checks)


def invalidate_downstream_artifacts(opt_dir: Path) -> None:
    for rel in (
        "optimizer_manifest.json",
        "static_replay_metrics.json",
        "static_replay_equity_curve.csv",
        "static_replay_manifest.json",
    ):
        path = opt_dir / rel
        if path.exists():
            path.unlink()
    validation_dir = opt_dir / "validation"
    if validation_dir.exists():
        for path in validation_dir.iterdir():
            if path.is_file():
                path.unlink()
    invalidate_dependents(opt_dir.parent, "optimizer")


def main() -> int:  # noqa: C901
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    try:
        db_path = resolve_database_path(paths, args.db)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1
    runs_root = paths.output_dir / "runs"
    run_as_of = args.as_of or latest_run_with(runs_root, "manifest.json")
    if not run_as_of:
        LOGGER.error("No sealed run found under %s", runs_root)
        return 1
    run_dir = runs_root / run_as_of
    risk_dir = run_dir / "risk"
    cov_path = risk_dir / "covariance.csv"
    coverage_path = risk_dir / "risk_coverage.csv"
    scores_path = run_dir / "stocks_scores.csv"
    for required in (cov_path, coverage_path, scores_path, risk_dir / "risk_manifest.json"):
        if not required.exists():
            LOGGER.error("Required input missing (run Stage 1+2 first): %s", required)
            return 1
    readiness = stage3_readiness(run_dir, risk_dir, config_path)
    for c in readiness:
        LOGGER.info("readiness [%s] %s -- %s", c["status"], c["check"], c["detail"])
    if not readiness_passed(readiness):
        LOGGER.error("Stage 3 readiness FAILED; refusing to optimize for %s", run_as_of)
        return 1

    opt_dir = run_dir / "optimizer"
    weights_path = opt_dir / "target_weights.csv"
    excluded_path = opt_dir / "risk_excluded_candidates.csv"
    meta_path = opt_dir / "optimizer_meta.json"
    if args.force:
        invalidate_downstream_artifacts(opt_dir)
    try:
        fail_if_exists([weights_path, excluded_path, meta_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    oc = cfg_get(config, "optimizer", {})
    monitor_policy = oc.get("monitor_entry_policy", {})
    if not isinstance(monitor_policy, dict):
        LOGGER.error("optimizer.monitor_entry_policy must be a mapping")
        return 1
    monitor_policy_enabled = monitor_policy.get("enabled_in_production") is True
    try:
        monitor_overlay, monitor_meta = load_monitor_overlay(
            run_dir,
            run_as_of=run_as_of,
            enabled=monitor_policy_enabled,
            mode=args.monitor_overlay_mode,
        )
    except (FileNotFoundError, ValueError) as exc:
        LOGGER.error("%s", exc)
        return 1
    use_conf = bool(oc.get("use_confidence_adjusted_mu", True))
    risk_aversion = float(oc.get("risk_aversion", 5.0))
    gross = float(oc.get("gross_exposure", 1.0))
    allow_constraint_cash = oc.get("allow_constraint_cash") is True
    max_weight = float(oc.get("max_weight_per_name", 0.05))
    min_hold = float(oc.get("min_weight_to_hold", 0.0005))
    solver = str(oc.get("solver", "ECOS"))
    band_gammas = [float(g) for g in (oc.get("sensitivity_band_gammas") or [risk_aversion])]
    if not any(abs(g - risk_aversion) < 1e-12 for g in band_gammas):
        band_gammas.append(risk_aversion)
    band_gammas = sorted(set(band_gammas))
    # deployable-book liquidity floor: exclude names whose median half-spread exceeds this ceiling
    # (<=0 or missing disables the floor). Fail-open on a missing snapshot / missing ticker row.
    max_half_spread = _f(oc.get("max_half_spread_bps"), 0.0)
    spread_rows = load_spread_snapshot(risk_dir / "spread_snapshot.csv") if max_half_spread > 0 else {}

    scores = {r["ticker"]: r for r in read_csv(scores_path)}
    coverage = {r["ticker"]: r for r in read_csv(coverage_path)}
    covariance = pd.read_csv(cov_path, index_col=0)
    covariance.index = [str(i) for i in covariance.index]
    covariance.columns = [str(c) for c in covariance.columns]
    cov_tickers = set(covariance.index)

    # Locked universe: scored equities only, eligible by both gates, with a covariance row.
    # A pre-solve liquidity floor then drops names whose median half-spread breaches the config ceiling
    # (fail-open: a missing snapshot / missing row / blank median never excludes â€” coverage wins).
    universe: list[str] = []
    excluded: list[dict] = []
    n_liquidity_excluded = 0
    n_liquidity_no_spread = 0
    n_monitor_excluded = 0

    def exclusion_row(
        ticker: str,
        srow: dict[str, str],
        crow: dict[str, str],
        *,
        risk_eligible: bool,
        role: str,
        reason: str,
    ) -> dict[str, object]:
        monitor = monitor_overlay.get(ticker, {})
        return {
            "ticker": ticker,
            "source_pipeline": srow.get("source_pipeline", ""),
            "investable_eligible": 1,
            "risk_eligible": int(risk_eligible),
            "role": role,
            "risk_status": crow.get("risk_status", "missing"),
            "monitor_entry_eligible": monitor.get(
                "optimizer_entry_eligible", ""
            ),
            "monitor_internal_state": monitor.get("internal_state", ""),
            "monitor_action_state": monitor.get("action_state", ""),
            "monitor_policy_reason": monitor.get("policy_reason", ""),
            "exclusion_reason": reason,
        }

    for ticker, srow in scores.items():
        if str(srow.get("investable_eligible", "")).strip() != "1":
            continue
        crow = coverage.get(ticker, {})
        risk_eligible = str(crow.get("risk_eligible", "")).strip() == "1"
        role = str(crow.get("role", "")).strip()
        in_cov = ticker in cov_tickers
        if args.monitor_overlay_mode == "required":
            monitor = monitor_overlay.get(ticker)
            if monitor is None:
                LOGGER.error("Investable ticker missing from monitor overlay: %s", ticker)
                return 1
            if str(monitor.get("stage1_investable_eligible", "")).strip() != "1":
                LOGGER.error("Monitor overlay Stage 1 eligibility mismatch: %s", ticker)
                return 1
            if str(monitor.get("optimizer_entry_eligible", "")).strip() != "1":
                excluded.append(
                    exclusion_row(
                        ticker,
                        srow,
                        crow,
                        risk_eligible=risk_eligible,
                        role=role,
                        reason="monitor_entry_policy",
                    )
                )
                n_monitor_excluded += 1
                continue
        if risk_eligible and role == "scored" and in_cov:
            spread_bps = median_half_spread_bps(spread_rows.get(ticker)) if max_half_spread > 0 else None
            if max_half_spread > 0 and spread_bps is not None and spread_bps > max_half_spread:
                excluded.append(
                    exclusion_row(
                        ticker,
                        srow,
                        crow,
                        risk_eligible=True,
                        role=role,
                        reason="liquidity_spread",
                    )
                )
                n_liquidity_excluded += 1
                continue
            if max_half_spread > 0 and spread_bps is None:
                n_liquidity_no_spread += 1  # fail-open: sized normally, only counted for the log
            universe.append(ticker)
        else:
            reason = (
                "not_risk_eligible" if not risk_eligible
                else "role_not_scored" if role != "scored"
                else "missing_covariance_row"
            )
            excluded.append(
                exclusion_row(
                    ticker,
                    srow,
                    crow,
                    risk_eligible=risk_eligible,
                    role=role,
                    reason=reason,
                )
            )
    if max_half_spread > 0:
        LOGGER.info(
            "Liquidity floor (max_half_spread_bps=%.4g): excluded=%d, fail_open_no_spread=%d, snapshot_rows=%d",
            max_half_spread, n_liquidity_excluded, n_liquidity_no_spread, len(spread_rows),
        )
    universe = sorted(universe)
    if not universe:
        LOGGER.error("Optimizer universe is empty after the locked eligibility filter")
        return 1

    mu_raw = np.array([_f(scores[t].get("final_score")) for t in universe])
    # neutral 0.5 default matches the adapters' missing-confidence convention (and Stage 8's);
    # the sealed contract always carries score_confidence, so this only guards a malformed row
    conf = np.array([_f(scores[t].get("score_confidence"), 0.5) for t in universe])
    mu_used = mu_raw * conf if use_conf else mu_raw
    try:
        mu_optimized, sigma, biotech_overlay = biotech_benchmark_overlay(
            scores,
            universe,
            covariance,
            mu_used,
        )
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1

    # Explicit per-sleeve budget caps (LIVE book only): sum(w of pipeline) <= cap * gross.
    # A cap of 0.0 excludes the sleeve from sizing entirely. Research/shadow books stay uncapped
    # by design â€” a sleeve earns a larger live budget through Stage 7/BL + Stage 11 evidence,
    # never by inflating its alpha anchor.
    sector_caps_cfg = {str(k): float(v) for k, v in (oc.get("sector_weight_caps") or {}).items()}
    group_caps: list[tuple[list[int], float]] = []
    sector_cap_summary: dict[str, dict[str, float]] = {}
    for pipeline, cap in sorted(sector_caps_cfg.items()):
        if not np.isfinite(cap) or cap < 0:
            LOGGER.error("optimizer.sector_weight_caps.%s=%s must be finite and non-negative", pipeline, cap)
            return 1
        indices = [i for i, t in enumerate(universe)
                   if str(scores[t].get("source_pipeline", "")).strip() == pipeline]
        if indices:
            group_caps.append((indices, cap))
        sector_cap_summary[pipeline] = {"cap": cap, "n_universe": len(indices)}
    scope_caps_cfg: dict[str, dict[str, float]] = {
        str(pipeline): {
            str(scope): float(cap)
            for scope, cap in dict(raw_scopes or {}).items()
        }
        for pipeline, raw_scopes in dict(oc.get("scope_weight_caps") or {}).items()
    }
    scope_cap_summary: dict[str, dict[str, float]] = {}
    for pipeline, scopes in sorted(scope_caps_cfg.items()):
        for scope, cap in sorted(scopes.items()):
            key = f"{pipeline}::{scope}"
            if not np.isfinite(cap) or cap < 0:
                LOGGER.error("optimizer.scope_weight_caps.%s=%s must be finite and non-negative", key, cap)
                return 1
            indices = [
                i
                for i, ticker in enumerate(universe)
                if str(scores[ticker].get("source_pipeline", "")).strip() == pipeline
                and str(scores[ticker].get("model_scope_id", "")).strip() == scope
            ]
            if indices:
                group_caps.append((indices, cap))
            scope_cap_summary[key] = {"cap": cap, "n_universe": len(indices)}
        configured_scopes = set(scopes)
        uncapped_scope_tickers = [
            ticker
            for ticker in universe
            if str(scores[ticker].get("source_pipeline", "")).strip() == pipeline
            and str(scores[ticker].get("model_scope_id", "")).strip() not in configured_scopes
        ]
        if uncapped_scope_tickers:
            LOGGER.error(
                "optimizer.scope_weight_caps.%s does not cover optimizer scope(s) for %s",
                pipeline,
                [
                    (
                        ticker,
                        str(scores[ticker].get("model_scope_id", "")).strip(),
                    )
                    for ticker in uncapped_scope_tickers[:10]
                ],
            )
            return 1
    fixed_equal_sleeves = {
        str(value).strip()
        for value in (oc.get("fixed_equal_weight_sleeves") or [])
        if str(value).strip()
    }
    equal_weight_groups: list[list[int]] = []
    for pipeline in sorted(fixed_equal_sleeves):
        if pipeline not in sector_caps_cfg:
            LOGGER.error(
                "optimizer.fixed_equal_weight_sleeves=%s requires a "
                "matching sector_weight_caps entry",
                pipeline,
            )
            return 1
        indices = [
            i
            for i, ticker in enumerate(universe)
            if str(scores[ticker].get("source_pipeline", "")).strip()
            == pipeline
        ]
        if indices:
            equal_weight_groups.append(indices)

    try:
        maximum_gross, capacity_attempts = maximum_investable_gross(
            len(universe),
            group_caps=group_caps or None,
            cap_base_gross=gross,
            max_weight=max_weight,
            equal_weight_groups=equal_weight_groups or None,
        )
        invested_gross, constraint_cash_triggered = constraint_aware_invested_gross(
            requested_gross=gross,
            capacity=maximum_gross,
            allow_constraint_cash=allow_constraint_cash,
        )
        solve_group_caps = (
            rescale_group_caps_for_invested_gross(
                group_caps,
                cap_base_gross=gross,
                invested_gross=invested_gross,
            )
            if group_caps
            else []
        )
        weights, info = solve_long_only_mv(
            mu_optimized,
            sigma,
            risk_aversion=risk_aversion,
            max_weight=max_weight,
            gross=invested_gross,
            solver=solver,
            group_caps=solve_group_caps or None,
            equal_weight_groups=equal_weight_groups or None,
        )
    except ValueError as exc:
        LOGGER.error("Stage 3 hard-cap feasibility failed: %s", exc)
        return 1
    if info["status"] not in ("optimal", "optimal_inaccurate"):
        LOGGER.error("Solver did not converge: %s", info)
        return 1
    try:
        # Drop dust then re-project to the feasible invested gross without breaching
        # per-name or absolute sleeve-budget caps. Any configured-gross shortfall is
        # explicit constraint cash, closed to NAV by Stage 4.
        if solve_group_caps:
            weights = finalize_with_group_caps(
                weights,
                group_caps=solve_group_caps,
                min_weight=min_hold,
                max_weight=max_weight,
                gross=invested_gross,
            )
        else:
            weights = finalize_long_only_weights(
                weights,
                min_weight=min_hold,
                max_weight=max_weight,
                gross=invested_gross,
            )
        # Publish rounded weights that sum to EXACTLY invested_gross. Stage 4 computes
        # CASH against the configured gross, so conservation remains exact.
        weights = snap_rounded_weights(
            weights,
            gross=invested_gross,
            max_weight=max_weight,
            group_caps=solve_group_caps or None,
        )
        band_low, band_high = weight_sensitivity_band(
            mu_optimized,
            sigma,
            gammas=band_gammas,
            min_weight=min_hold,
            max_weight=max_weight,
            gross=invested_gross,
            solver=solver,
            group_caps=solve_group_caps or None,
            equal_weight_groups=equal_weight_groups or None,
        )
    except ValueError as exc:
        LOGGER.error("Stage 3 constrained post-processing failed: %s", exc)
        return 1
    realized_weights = realize_biotech_benchmark_weights(universe, weights, biotech_overlay)
    realized_band_low = realize_biotech_benchmark_weights(universe, band_low, biotech_overlay)
    realized_band_high = realize_biotech_benchmark_weights(universe, band_high, biotech_overlay)
    for pipeline, summary in sector_cap_summary.items():
        realized = float(sum(
            weights[i] for i, t in enumerate(universe)
            if str(scores[t].get("source_pipeline", "")).strip() == pipeline
        ))
        summary["realized_weight"] = round(realized, 8)
    for key, summary in scope_cap_summary.items():
        pipeline, scope = key.split("::", 1)
        realized = float(sum(
            weights[i] for i, ticker in enumerate(universe)
            if str(scores[ticker].get("source_pipeline", "")).strip() == pipeline
            and str(scores[ticker].get("model_scope_id", "")).strip() == scope
        ))
        summary["realized_weight"] = round(realized, 8)

    rows = []
    for i, t in enumerate(universe):
        srow = scores[t]
        rows.append({
            "ticker": t, "sector": srow.get("sector", ""), "industry": srow.get("industry", ""),
            "source_pipeline": srow.get("source_pipeline", ""), "rating": srow.get("rating", ""),
            "final_score": round(mu_raw[i], 6), "score_confidence": round(float(conf[i]), 4),
            "mu_raw": round(mu_raw[i], 6), "mu_used": round(float(mu_used[i]), 6),
            "weight": round(realized_weights[t], 10),
            "weight_band_low": round(realized_band_low[t], 10),
            "weight_band_high": round(realized_band_high[t], 10),
        })
    if biotech_overlay is not None:
        benchmark_ticker = str(biotech_overlay["benchmark_ticker"])
        rows.append({
            "ticker": benchmark_ticker,
            "sector": "Health Care",
            "industry": "Biotech Benchmark Residual",
            "source_pipeline": "biotech",
            "rating": "benchmark_residual",
            "final_score": 0.0,
            "score_confidence": 1.0,
            "mu_raw": 0.0,
            "mu_used": 0.0,
            "weight": round(realized_weights[benchmark_ticker], 10),
            "weight_band_low": round(realized_band_low[benchmark_ticker], 10),
            "weight_band_high": round(realized_band_high[benchmark_ticker], 10),
        })
    rows.sort(key=lambda r: -r["weight"])
    write_csv(weights_path, WEIGHT_FIELDS, rows)
    write_csv(excluded_path, EXCLUDED_FIELDS, sorted(excluded, key=lambda r: r["ticker"]))

    held = [r for r in rows if r["weight"] > 0]
    meta = {
        "run_as_of": run_as_of,
        "stage": "stage3_aqr_baseline",
        "objective": "long_only_mean_variance",
        "mu_transform": "final_score*score_confidence" if use_conf else "final_score",
        "risk_aversion": risk_aversion,
        "gross_exposure": gross,
        "invested_gross": round(invested_gross, 10),
        "constraint_cash_weight": round(gross - invested_gross, 10),
        "constraint_cash_policy": {
            "enabled": allow_constraint_cash,
            "triggered": constraint_cash_triggered,
            "requested_gross": gross,
            "maximum_investable_gross": round(maximum_gross, 10),
            "invested_gross": round(invested_gross, 10),
            "cash_weight_before_cost_overlay": round(gross - invested_gross, 10),
            "capacity_solver_attempts": capacity_attempts,
            "cap_reference": "configured_gross_nav_fraction",
        },
        "max_weight_per_name": max_weight,
        "min_weight_to_hold": min_hold,
        "sector_weight_caps": sector_cap_summary,
        "scope_weight_caps": scope_cap_summary,
        "fixed_equal_weight_sleeves": sorted(fixed_equal_sleeves),
        "biotech_adaptive_sleeve": (
            {
                key: value
                for key, value in biotech_overlay.items()
                if key != "biotech_indices"
            }
            if biotech_overlay is not None
            else {"status": "inactive_no_promotion_contract"}
        ),
        "liquidity_floor": {
            "max_half_spread_bps": max_half_spread,
            "snapshot_present": bool(spread_rows),
            "n_excluded": n_liquidity_excluded,
            "n_fail_open_no_spread": n_liquidity_no_spread,
        },
        "monitor_entry_policy": {
            **monitor_meta,
            "n_excluded": n_monitor_excluded,
        },
        "sensitivity_band_gammas": band_gammas,
        "covariance_source": "stage2_risk_covariance_csv",
        "covariance_sha256": sha256_file(cov_path),
        "universe_size": len(universe),
        "n_held": len(held),
        "n_excluded_candidates": len(excluded),
        "sum_weights": round(float(sum(r["weight"] for r in rows)), 6),
        "max_weight_actual": round(max((r["weight"] for r in rows), default=0.0), 6),
        "solver": {k: info[k] for k in ("status", "solver_requested", "solver_used",
                                        "objective_value", "solver_attempts")},
    }
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    write_manifest(meta_path, meta)

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        run_id = start_run(conn, run_type="run_portfolio_optimizer", input_path=scores_path)
        finish_run(conn, run_id=run_id, status="success", row_count=len(held),
                   message=f"as_of={run_as_of} held={len(held)} universe={len(universe)} excluded={len(excluded)}")

    LOGGER.info(
        "AQR-only baseline: %d held / %d universe "
        "(requested_gross=%.3f, invested=%.3f, constraint_cash=%.3f, max_wt=%.3f), "
        "%d excluded -> %s",
        len(held),
        len(universe),
        gross,
        meta["sum_weights"],
        meta["constraint_cash_weight"],
        meta["max_weight_actual"],
        len(excluded),
        opt_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

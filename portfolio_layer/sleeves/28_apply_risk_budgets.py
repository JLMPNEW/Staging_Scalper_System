#!/usr/bin/env python3
"""Stage 8 - re-allocate the fused book's RISK into a SHADOW proposal (improvement-relative).

Targets regime-conditional sleeve risk budgets with IR-tilted risk parity within each sleeve, capped
per-name risk contribution, long-only + per-name caps, and gross/cash held at the Stage 7 level (no added
risk). Emits a proposal only; never mutates the Stage 7 book. Gross is NOT re-scaled upward (Stage 6/7 own it).
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.artifacts import invalidate_dependents  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    read_csv,
    read_manifest,
    sealed_artifact_errors,
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402
from portfolio_layer.sleeves.risk_model import (  # noqa: E402
    effective_number_of_bets,
    enforce_rc_cap_to_cash,
    factor_decomposition,
    information_ratios,
    risk_contributions,
    solve_risk_budget,
    sleeve_risk_bounds,
    target_risk_budget,
    throttle_scale,
    trailing_book_drawdown,
)


LOGGER = logging.getLogger("apply_risk_budgets")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SOURCE_FILES = ["risk_model.py", "27_build_sleeve_framework.py", "28_apply_risk_budgets.py"]
WEIGHT_FIELDS = ["ticker", "weight", "sleeve", "source_pipeline", "prior_weight",
                 "target_risk_budget", "realized_risk_contribution"]
BUDGET_FIELDS = [
    "sleeve", "n_names", "target_risk_share", "feasible_target_risk_share",
    "feasible_min_risk_share", "feasible_max_risk_share", "before_risk_share", "after_risk_share",
    "before_capital_share", "after_capital_share",
]
FACTOR_FIELDS = ["scope", "idiosyncratic_share", "systematic_share", "market_share", "max_sector_share"]


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Apply Stage 8 risk budgets (shadow proposal).")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, default=None)
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def _f(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def _f_default(value: Any, default: float) -> float:
    parsed = _f(value)
    return default if parsed is None else parsed


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _verify_recorded_hashes(meta: dict[str, Any], *, package_root: Path) -> list[str]:
    """Verify a prior stage's sealed input/source hashes against current files."""
    bad: list[str] = []
    input_paths = meta.get("input_paths") or {}
    for key, recorded in (meta.get("inputs_sha256") or {}).items():
        path_text = input_paths.get(key)
        if not path_text:
            bad.append(f"input_path_missing:{key}")
            continue
        path = Path(path_text)
        if not path.exists():
            bad.append(f"input_missing:{key}")
            continue
        if sha256_file(path) != recorded:
            bad.append(f"input_hash:{key}")
    for name, recorded in (meta.get("source_sha256") or {}).items():
        path = package_root / "sleeves" / name
        if not path.exists():
            bad.append(f"source_missing:{name}")
            continue
        if sha256_file(path) != recorded:
            bad.append(f"source_hash:{name}")
    return bad


def main() -> int:  # noqa: C901
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    runs_root = paths.output_dir / "runs"
    run_as_of = args.as_of or latest_run_with(runs_root, "sleeves/risk_model_meta.json")
    if not run_as_of:
        LOGGER.error("No sealed Stage 1 run found under %s", runs_root)
        return 1
    run_dir = runs_root / run_as_of
    sleeves_dir = run_dir / "sleeves"
    art = {
        "assignments": sleeves_dir / "sleeve_assignments.csv",
        "risk_model_meta": sleeves_dir / "risk_model_meta.json",
        "covariance": run_dir / "risk" / "covariance.csv",
        "returns_panel": run_dir / "risk" / "returns_panel.csv",
        "risk_manifest": run_dir / "risk" / "risk_manifest.json",
        "config": config_path,
    }
    missing = [k for k, p in art.items() if not p.exists()]
    if missing:
        LOGGER.error("Run 27 first; missing %s", missing)
        return 1

    out = {
        "sleeve_adjusted_target_weights.csv": sleeves_dir / "sleeve_adjusted_target_weights.csv",
        "sleeve_risk_budget.csv": sleeves_dir / "sleeve_risk_budget.csv",
        "factor_risk_decomposition.csv": sleeves_dir / "factor_risk_decomposition.csv",
        "effective_bets.json": sleeves_dir / "effective_bets.json",
        "drawdown_throttle_simulation.json": sleeves_dir / "drawdown_throttle_simulation.json",
        "risk_budget_meta.json": sleeves_dir / "risk_budget_meta.json",
    }
    validation_path = sleeves_dir / "validation" / "risk_budget_validation.csv"
    if args.force:
        invalidate_dependents(run_dir, "sleeves")
        for p in (*out.values(), validation_path):
            if p.exists():
                p.unlink()
    try:
        fail_if_exists(list(out.values()), force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    checks: list[dict[str, str]] = []

    def rec(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    meta27 = _load_json(art["risk_model_meta"])
    bad27 = []
    if meta27.get("acceptance") != "PASS":
        bad27.append(f"framework_acceptance={meta27.get('acceptance')}")
    if (meta27.get("outputs_sha256") or {}).get("sleeve_assignments.csv") != sha256_file(art["assignments"]):
        bad27.append("assignments_hash_mismatch")
    bad27.extend(_verify_recorded_hashes(meta27, package_root=PACKAGE_ROOT))
    rec("sleeve_framework_current", "PASS" if not bad27 else "FAIL",
        "27 accepted and assignments hash matches" if not bad27 else f"{bad27}")
    if bad27:
        LOGGER.error("Stage 27 inputs stale (source/hash mismatch); re-run 27: %s", bad27)
        validation_path.parent.mkdir(parents=True, exist_ok=True)
        write_csv(validation_path, ["check", "status", "detail"], checks)
        return 1

    assignments = read_csv(art["assignments"])
    prior: dict[str, float] = {}
    sleeve_of: dict[str, str] = {}
    pipe_of: dict[str, str] = {}
    alpha: dict[str, float] = {}
    for r in assignments:
        t = str(r.get("ticker", "")).strip().upper()
        w = _f(r.get("weight"))
        if not t or w is None or w <= 0:
            continue
        prior[t] = w
        sleeve_of[t] = str(r.get("sleeve", "")).strip()
        pipe_of[t] = str(r.get("source_pipeline", "")).strip()
        alpha[t] = _f_default(r.get("final_score"), 0.0)
    cash_weight = _f_default(meta27.get("cash_weight"), 0.0)
    invested_gross = sum(prior.values())

    cov = pd.read_csv(art["covariance"], index_col=0)
    cov.index = [str(i).strip().upper() for i in cov.index]
    cov.columns = [str(c).strip().upper() for c in cov.columns]
    market_etf = str(cfg_get(config, "sleeves.market_factor_etf", "SPY")).strip().upper()
    sector_etfs = {k: str(v).strip().upper() for k, v in (cfg_get(config, "sleeves.sector_factor_etfs", {}) or {}).items()}

    # before diagnostics
    rc_before = risk_contributions(prior, cov)
    ir = information_ratios(alpha, rc_before.sigma)
    factor_before = factor_decomposition(prior, cov, market_etf=market_etf, sector_etfs=sector_etfs)
    enb_before = effective_number_of_bets(prior, cov)

    members: dict[str, list[str]] = {}
    for t, s in sleeve_of.items():
        members.setdefault(s, []).append(t)
    sleeve_budgets = dict((meta27.get("regime") or {}).get("sleeve_risk_budgets") or {})
    tilt = _f_default(cfg_get(config, "sleeves.within_sleeve_ir_tilt", 0.5), 0.5)
    rc_cap = _f_default(cfg_get(config, "sleeves.per_name_risk_contribution_cap", 0.08), 0.08)
    max_weight = _f_default(cfg_get(config, "sleeves.max_weight_per_name", 0.05), 0.05)
    max_iter = int(_f_default(cfg_get(config, "sleeves.projection.max_iterations", 50), 50.0))

    target_b = target_risk_budget(
        members=members,
        sleeve_budgets=sleeve_budgets,
        information_ratio=ir,
        tilt=tilt,
        rc_cap=rc_cap,
    )
    weights = solve_risk_budget(cov, target_b, gross=invested_gross, max_weight=max_weight, max_iter=max_iter)
    rc_enforcement = enforce_rc_cap_to_cash(weights, cov, rc_cap=rc_cap, max_iter=max_iter)
    weights = rc_enforcement.weights

    # ---- Phase 2 drawdown throttle: measured on the proposal, state-chained, applied on opt-in ----
    # Uniform de-risking after the RC trim preserves RC shares (scale-invariant) and only frees
    # weight to cash. Recovery is ratcheted: the applied scale may rise at most recovery_step_per_run
    # per run so re-risking is deliberate, never a single-day flicker.
    throttle_apply = bool(cfg_get(config, "sleeves.drawdown_throttle.apply", False))
    dd_limit = _f_default(cfg_get(config, "sleeves.drawdown_throttle.dd_limit", 0.15), 0.15)
    throttle_window = int(_f_default(cfg_get(config, "sleeves.drawdown_throttle.window_trading_days", 63), 63.0))
    recovery_step = _f_default(cfg_get(config, "sleeves.drawdown_throttle.recovery_step_per_run", 0.25), 0.25)
    min_complete = _f_default(
        cfg_get(config, "sleeves.drawdown_throttle.min_complete_fraction", 0.80), 0.80,
    )
    risk_manifest = read_manifest(art["risk_manifest"])
    risk_seal_bad = sealed_artifact_errors(
        risk_manifest, art["returns_panel"], "returns_panel.csv", run_as_of=run_as_of,
    )
    rec(
        "drawdown_returns_sealed",
        "PASS" if not risk_seal_bad else "FAIL",
        "returns panel matches accepted Stage 2 manifest"
        if not risk_seal_bad else f"{risk_seal_bad}",
    )
    if risk_seal_bad:
        validation_path.parent.mkdir(parents=True, exist_ok=True)
        write_csv(validation_path, ["check", "status", "detail"], checks)
        return 1
    returns_panel = pd.read_csv(art["returns_panel"], index_col=0)
    returns_panel.columns = [str(c).strip().upper() for c in returns_panel.columns]
    try:
        measured_dd = trailing_book_drawdown(
            weights,
            returns_panel,
            window=throttle_window,
            min_complete_fraction=min_complete,
        )
    except ValueError as exc:
        rec("drawdown_data_sufficient", "FAIL", str(exc))
        validation_path.parent.mkdir(parents=True, exist_ok=True)
        write_csv(validation_path, ["check", "status", "detail"], checks)
        LOGGER.error("Cannot compute Stage 8 drawdown throttle safely: %s", exc)
        return 1
    rec(
        "drawdown_data_sufficient",
        "PASS",
        f"window={throttle_window}; min_complete_fraction={min_complete:.3f}",
    )
    computed_scale = throttle_scale(abs(measured_dd), dd_limit)
    prev_scale = 1.0
    prior_runs = sorted(
        p for p in runs_root.iterdir()
        if p.is_dir() and p.name < run_as_of and (p / "sleeves" / "drawdown_throttle_state.json").exists()
    )
    if prior_runs:
        try:
            prev_state = json.loads(
                (prior_runs[-1] / "sleeves" / "drawdown_throttle_state.json").read_text(encoding="utf-8"))
            prev_scale = float(prev_state.get("applied_scale", 1.0))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            prev_scale = 1.0
    ratchet_ceiling = min(1.0, prev_scale + recovery_step)
    applied_scale = min(computed_scale, ratchet_ceiling) if throttle_apply else 1.0
    if throttle_apply and applied_scale < 1.0 - 1e-12:
        weights = {t: w * applied_scale for t, w in weights.items()}
        LOGGER.info("Drawdown throttle APPLIED: dd=%.4f scale=%.4f (computed=%.4f, ratchet<=%.4f)",
                    measured_dd, applied_scale, computed_scale, ratchet_ceiling)

    # any name pruned/trimmed keeps zero or lower weight; cash absorbs the residual
    realized_invested = sum(weights.values())
    final_cash = round(1.0 - realized_invested, 10)

    rc_after = risk_contributions(weights, cov)
    factor_after = factor_decomposition(weights, cov, market_etf=market_etf, sector_etfs=sector_etfs)
    enb_after = effective_number_of_bets(weights, cov)

    # per-sleeve risk + capital shares (before/after)
    def _sleeve_share(rc_map: dict[str, float], wmap: dict[str, float]) -> tuple[dict[str, float], dict[str, float]]:
        risk: dict[str, float] = {}
        cap: dict[str, float] = {}
        wsum = sum(wmap.values()) or 1.0
        for t, s in sleeve_of.items():
            risk[s] = risk.get(s, 0.0) + rc_map.get(t, 0.0)
            cap[s] = cap.get(s, 0.0) + wmap.get(t, 0.0) / wsum
        return risk, cap

    risk_before_s, capital_before_s = _sleeve_share(rc_before.rc, prior)
    risk_after_s, capital_after_s = _sleeve_share(rc_after.rc, weights)
    feasible_bounds = sleeve_risk_bounds(
        cov,
        sleeve_of,
        gross=invested_gross,
        max_weight=max_weight,
        rc_cap=rc_cap,
        max_iter=max_iter,
    )

    # ---- write artifacts ----
    sleeves_dir.mkdir(parents=True, exist_ok=True)
    weight_rows = [{
        "ticker": t, "weight": round(weights[t], 10), "sleeve": sleeve_of.get(t, ""),
        "source_pipeline": pipe_of.get(t, ""), "prior_weight": round(prior.get(t, 0.0), 10),
        "target_risk_budget": round(target_b.get(t, 0.0), 10),
        "realized_risk_contribution": round(rc_after.rc.get(t, 0.0), 10),
    } for t in sorted(weights)]
    weight_rows.append({"ticker": "CASH", "weight": final_cash, "sleeve": "CASH", "source_pipeline": "",
                        "prior_weight": round(cash_weight, 10), "target_risk_budget": 0.0,
                        "realized_risk_contribution": 0.0})
    write_csv(out["sleeve_adjusted_target_weights.csv"], WEIGHT_FIELDS, weight_rows)

    budget_rows = [{
        "sleeve": s, "n_names": len(members.get(s, [])),
        "target_risk_share": round(sleeve_budgets.get(s, 0.0), 6),
        "feasible_target_risk_share": round(
            min(max(sleeve_budgets.get(s, 0.0), feasible_bounds.get(s, {}).get("min", 0.0)),
                feasible_bounds.get(s, {}).get("max", 1.0)),
            6,
        ),
        "feasible_min_risk_share": round(feasible_bounds.get(s, {}).get("min", 0.0), 6),
        "feasible_max_risk_share": round(feasible_bounds.get(s, {}).get("max", 1.0), 6),
        "before_risk_share": round(risk_before_s.get(s, 0.0), 6),
        "after_risk_share": round(risk_after_s.get(s, 0.0), 6),
        "before_capital_share": round(capital_before_s.get(s, 0.0), 6),
        "after_capital_share": round(capital_after_s.get(s, 0.0), 6),
    } for s in sorted(members)]
    write_csv(out["sleeve_risk_budget.csv"], BUDGET_FIELDS, budget_rows)

    write_csv(out["factor_risk_decomposition.csv"], FACTOR_FIELDS, [
        {"scope": "before", "idiosyncratic_share": round(factor_before["idiosyncratic_share"], 6),
         "systematic_share": round(factor_before["systematic_share"], 6),
         "market_share": round(factor_before["market_share"], 6),
         "max_sector_share": round(factor_before["max_sector_share"], 6)},
        {"scope": "after", "idiosyncratic_share": round(factor_after["idiosyncratic_share"], 6),
         "systematic_share": round(factor_after["systematic_share"], 6),
         "market_share": round(factor_after["market_share"], 6),
         "max_sector_share": round(factor_after["max_sector_share"], 6)},
    ])
    write_manifest(out["effective_bets.json"],
                   {"before": enb_before, "after": enb_after,
                    "improvement": round(enb_after["enb"] - enb_before["enb"], 4)})
    simulated_throttle = {
        "applied_to_weights": bool(throttle_apply and applied_scale < 1.0 - 1e-12),
        "apply_enabled": throttle_apply,
        "dd_limit": dd_limit,
        "formula": "clip(1 - drawdown / dd_limit, 0, 1), recovery ratcheted per run",
        "measured_trailing_drawdown": round(measured_dd, 6),
        "window_trading_days": throttle_window,
        "computed_scale": round(computed_scale, 8),
        "previous_applied_scale": round(prev_scale, 8),
        "recovery_step_per_run": recovery_step,
        "applied_scale": round(applied_scale, 8),
        "cases": [
            {
                "case": "no_breach",
                "drawdown": round(0.5 * dd_limit, 8),
                "scale": round(throttle_scale(0.5 * dd_limit, dd_limit), 8),
            },
            {
                "case": "breach",
                "drawdown": round(1.25 * dd_limit, 8),
                "scale": round(throttle_scale(1.25 * dd_limit, dd_limit), 8),
            },
        ],
    }
    write_manifest(out["drawdown_throttle_simulation.json"], simulated_throttle)
    write_manifest(sleeves_dir / "drawdown_throttle_state.json", {
        "run_as_of": run_as_of,
        "applied_scale": round(applied_scale, 8),
        "computed_scale": round(computed_scale, 8),
        "measured_trailing_drawdown": round(measured_dd, 6),
        "apply_enabled": throttle_apply,
    })

    # ---- proposal-level sanity (improvement-relative; hard gates live in 29) ----
    serialized_rows = read_csv(out["sleeve_adjusted_target_weights.csv"])
    serialized_bad: list[str] = []
    serialized_tickers: set[str] = set()
    serialized_sum = 0.0
    serialized_cash_rows = 0
    for row_number, row in enumerate(serialized_rows, start=2):
        ticker = str(row.get("ticker", "")).strip().upper()
        raw_weight = row.get("weight")
        if raw_weight is None:
            serialized_bad.append(f"row={row_number}:missing_weight")
            continue
        try:
            weight = float(raw_weight)
        except (TypeError, ValueError):
            serialized_bad.append(f"row={row_number}:non_numeric_weight")
            continue
        if not ticker or ticker in serialized_tickers:
            serialized_bad.append(f"row={row_number}:blank_or_duplicate_ticker={ticker!r}")
        if not math.isfinite(weight) or weight < -1e-12:
            serialized_bad.append(f"row={row_number}:{ticker}:invalid_weight={weight}")
        serialized_tickers.add(ticker)
        serialized_cash_rows += int(ticker == "CASH")
        serialized_sum += weight
    sum_ok = abs(serialized_sum - 1.0) <= 1e-6
    serialized_ok = not serialized_bad and serialized_cash_rows == 1 and sum_ok
    rec(
        "serialized_weights_valid",
        "PASS" if serialized_ok else "FAIL",
        f"rows={len(serialized_rows)} sum={serialized_sum:.10f} cash_rows={serialized_cash_rows}"
        if serialized_ok else f"sum={serialized_sum:.10f}; cash_rows={serialized_cash_rows}; bad={serialized_bad[:8]}",
    )
    no_add_risk = realized_invested <= invested_gross + 1e-6
    rec("no_added_gross", "PASS" if no_add_risk else "FAIL", f"after_gross={realized_invested:.6f}<=stage7={invested_gross:.6f}")
    cash_ok = final_cash + 1e-8 >= cash_weight
    rec("cash_preserved", "PASS" if cash_ok else "FAIL", f"cash={final_cash:.6f}>=stage7={cash_weight:.6f}")
    rec(
        "gross_not_increased_after_rc_cap",
        "PASS" if no_add_risk and cash_ok and sum_ok else "FAIL",
        f"after_gross={realized_invested:.10f}; stage7_gross={invested_gross:.10f}; "
        f"cash_added_by_rc_trim={rc_enforcement.cash_added:.10f}; throttle_apply={throttle_apply}",
    )
    rec(
        "realized_rc_cap_enforced",
        # Keep this tolerance aligned with enforce_rc_cap_to_cash() and the Stage 29 per_name_rc_cap gate.
        "PASS" if rc_enforcement.converged and rc_enforcement.max_rc <= rc_cap + 1e-6 else "FAIL",
        f"max_rc={rc_enforcement.max_rc:.6f}<=cap={rc_cap}; "
        f"trimmed_names={len({str(r.get('ticker')) for r in rc_enforcement.trimmed})}; "
        f"iterations={rc_enforcement.iterations}; cash_added={rc_enforcement.cash_added:.6f}",
    )
    no_new = set(weights).issubset(set(prior))
    rec("no_new_tickers", "PASS" if no_new else "FAIL", f"new={sorted(set(weights)-set(prior))[:8]}")

    passed = all(c["status"] == "PASS" for c in checks)
    validation_path.parent.mkdir(parents=True, exist_ok=True)
    write_csv(validation_path, ["check", "status", "detail"], checks)
    meta = {
        "run_as_of": run_as_of,
        "stage": "stage8_risk_budget",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "shadow_only": True,
        "acceptance": "PASS" if passed else "FAIL",
        "invested_gross": round(invested_gross, 10),
        "after_invested_gross": round(realized_invested, 10),
        "cash_before": round(cash_weight, 10),
        "cash_after": final_cash,
        "drawdown_throttle_simulation": simulated_throttle,
        "within_sleeve_ir_tilt": tilt,
        "per_name_risk_contribution_cap": rc_cap,
        "rc_cap_enforcement": {
            "converged": rc_enforcement.converged,
            "iterations": rc_enforcement.iterations,
            "cash_added": round(rc_enforcement.cash_added, 10),
            "max_rc": round(rc_enforcement.max_rc, 10),
            "trimmed": list(rc_enforcement.trimmed),
        },
        "sleeve_feasibility_bounds": feasible_bounds,
        "diagnostics": {
            "enb_before": enb_before["enb"], "enb_after": enb_after["enb"],
            "idio_before": round(factor_before["idiosyncratic_share"], 6),
            "idio_after": round(factor_after["idiosyncratic_share"], 6),
            "rc_max_before": round(max(rc_before.rc.values()), 6),
            "rc_max_after": round(max(rc_after.rc.values()), 6),
        },
        "input_paths": {k: str(p) for k, p in art.items()},
        "inputs_sha256": {k: sha256_file(p) for k, p in art.items()},
        "outputs_sha256": {name: sha256_file(p) for name, p in out.items() if p.exists() and name != "risk_budget_meta.json"},
        "source_sha256": {n: sha256_file(PACKAGE_ROOT / "sleeves" / n)
                          for n in SOURCE_FILES if (PACKAGE_ROOT / "sleeves" / n).exists()},
        "checks": checks,
    }
    write_manifest(out["risk_budget_meta.json"], meta)

    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    LOGGER.info(
        "STAGE 8 RISK BUDGET: %s (ENB %.2f->%.2f, idio %.3f->%.3f, rc_max %.3f->%.3f, names %d->%d) -> %s",
        "PASS" if passed else "FAIL", enb_before["enb"], enb_after["enb"],
        factor_before["idiosyncratic_share"], factor_after["idiosyncratic_share"],
        max(rc_before.rc.values()), max(rc_after.rc.values()), len(prior), len(weights),
        out["sleeve_adjusted_target_weights.csv"],
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

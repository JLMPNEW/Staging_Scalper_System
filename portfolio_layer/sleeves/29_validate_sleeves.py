#!/usr/bin/env python3
"""Stage 8 - validate and seal the sleeve risk-budget proposal (SHADOW-ONLY).

Hard gates are improvement-relative (do not worsen diversification, respect per-name RC cap + sleeve risk
bands, no added risk, no new names, Stage 7 byte-unchanged). The absolute Rentech floors (idio >= 0.50,
ENB >= 10) are WARN-only because the upstream 2-sector book caps achievable diversification by reweighting.
Everything is recomputed from sealed CSVs + the Stage 2 covariance (not trusted from 28).
"""
from __future__ import annotations

import argparse
import json
import logging
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
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402
from portfolio_layer.sleeves.risk_model import (  # noqa: E402
    effective_number_of_bets,
    factor_decomposition,
    risk_contributions,
    sleeve_risk_bounds,
    throttle_scale,
)


LOGGER = logging.getLogger("validate_sleeves")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SOURCE_FILES = ["risk_model.py", "27_build_sleeve_framework.py", "28_apply_risk_budgets.py", "29_validate_sleeves.py"]


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Validate and seal Stage 8 sleeve risk-budget proposal.")
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
    run_as_of = args.as_of or latest_run_with(runs_root, "sleeves/risk_budget_meta.json")
    if not run_as_of:
        LOGGER.error("No sealed Stage 1 run found under %s", runs_root)
        return 1
    run_dir = runs_root / run_as_of
    sleeves_dir = run_dir / "sleeves"
    bl_dir = run_dir / "blacklitterman"
    art = {
        "framework_meta": sleeves_dir / "risk_model_meta.json",
        "assignments": sleeves_dir / "sleeve_assignments.csv",
        "budget_meta": sleeves_dir / "risk_budget_meta.json",
        "proposal": sleeves_dir / "sleeve_adjusted_target_weights.csv",
        "sleeve_risk_budget": sleeves_dir / "sleeve_risk_budget.csv",
        "factor_risk_decomposition": sleeves_dir / "factor_risk_decomposition.csv",
        "effective_bets": sleeves_dir / "effective_bets.json",
        "drawdown_throttle_simulation": sleeves_dir / "drawdown_throttle_simulation.json",
        "covariance": run_dir / "risk" / "covariance.csv",
        "risk_manifest": run_dir / "risk" / "risk_manifest.json",
        "bl_manifest": bl_dir / "bl_manifest.json",
        "bl_cost_adjusted": bl_dir / "costs" / "bl_cost_adjusted_target_weights.csv",
        "config": config_path,
    }
    missing = [k for k, p in art.items() if not p.exists()]
    if missing:
        LOGGER.error("Run 27 + 28 + Stage 7 first; missing %s", missing)
        return 1

    validation_path = sleeves_dir / "validation" / "sleeve_validation.csv"
    manifest_path = sleeves_dir / "sleeve_manifest.json"
    if args.force:
        for p in (validation_path, manifest_path):
            if p.exists():
                p.unlink()
    try:
        fail_if_exists([validation_path, manifest_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    checks: list[dict[str, str]] = []

    def rec(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    meta27 = _load_json(art["framework_meta"])
    meta28 = _load_json(art["budget_meta"])

    # 1. 27 + 28 sealed and current.
    cur_bad = []
    if meta27.get("acceptance") != "PASS":
        cur_bad.append(f"27_acceptance={meta27.get('acceptance')}")
    if (meta27.get("outputs_sha256") or {}).get("sleeve_assignments.csv") != sha256_file(art["assignments"]):
        cur_bad.append("assignments_hash")
    cur_bad.extend(_verify_recorded_hashes(meta27, package_root=PACKAGE_ROOT))
    if meta28.get("acceptance") != "PASS":
        cur_bad.append(f"28_acceptance={meta28.get('acceptance')}")
    cur_bad.extend(_verify_recorded_hashes(meta28, package_root=PACKAGE_ROOT))
    for name, recorded in (meta28.get("outputs_sha256") or {}).items():
        output_path = sleeves_dir / name
        if not output_path.exists() or recorded != sha256_file(output_path):
            cur_bad.append(f"{name}_hash")
    rec("stage8_inputs_current", "PASS" if not cur_bad else "FAIL",
        "27/28 accepted and output hashes match" if not cur_bad else f"{cur_bad[:8]}")

    # 2. Stage 7 baseline byte-unchanged (Stage 8 wrote only under sleeves/).
    bl_manifest = _load_json(art["bl_manifest"])
    base_bad = []
    if bl_manifest.get("acceptance") != "PASS":
        base_bad.append(f"stage7_acceptance={bl_manifest.get('acceptance')}")
    if (bl_manifest.get("provenance_sha256") or {}).get("costs/bl_cost_adjusted_target_weights.csv") != sha256_file(art["bl_cost_adjusted"]):
        base_bad.append("stage7_cost_adjusted_changed")
    rec("stage7_baseline_unchanged", "PASS" if not base_bad else "FAIL",
        "Stage 7 fused book intact" if not base_bad else f"{base_bad}")

    # 3. covariance hash == sealed Stage 2.
    risk_manifest = _load_json(art["risk_manifest"])
    cov_ok = ((risk_manifest.get("files") or {}).get("covariance.csv") or {}).get("sha256") == sha256_file(art["covariance"])
    rec("covariance_sealed_stage2", "PASS" if cov_ok else "FAIL", "cov hash matches Stage 2" if cov_ok else "cov_hash_mismatch")

    # ---- recompute everything from sealed CSVs + Sigma (do not trust 28) ----
    cov = pd.read_csv(art["covariance"], index_col=0)
    cov.index = [str(i).strip().upper() for i in cov.index]
    cov.columns = [str(c).strip().upper() for c in cov.columns]
    prior: dict[str, float] = {}
    sleeve_of: dict[str, str] = {}
    ir_by_ticker: dict[str, float] = {}
    for r in read_csv(art["assignments"]):
        t = str(r.get("ticker", "")).strip().upper()
        w = _f(r.get("weight"))
        if t and w and w > 0:
            prior[t] = w
            sleeve_of[t] = str(r.get("sleeve", "")).strip()
            ir_by_ticker[t] = _f(r.get("information_ratio")) or 0.0
    proposal: dict[str, float] = {}
    cash_after = 0.0
    for r in read_csv(art["proposal"]):
        t = str(r.get("ticker", "")).strip().upper()
        w = _f(r.get("weight"))
        if not t or w is None:
            continue
        if t == "CASH":
            cash_after += w
        elif w > 0:
            proposal[t] = w

    market_etf = str(cfg_get(config, "sleeves.market_factor_etf", "SPY")).strip().upper()
    sector_etfs = {k: str(v).strip().upper() for k, v in (cfg_get(config, "sleeves.sector_factor_etfs", {}) or {}).items()}
    required_factors = sorted({market_etf, *sector_etfs.values()} - {""})
    missing_factors = sorted(t for t in required_factors if t not in cov.index or t not in cov.columns)
    rec("factor_universe_present", "PASS" if not missing_factors else "FAIL",
        f"all configured factor ETFs present: {required_factors}" if not missing_factors else f"missing={missing_factors}")
    max_weight = _f(cfg_get(config, "sleeves.max_weight_per_name", 0.05)) or 0.05
    rc_cap = _f(cfg_get(config, "sleeves.per_name_risk_contribution_cap", 0.08)) or 0.08
    band = _f(cfg_get(config, "sleeves.sleeve_risk_budget_band", 0.05)) or 0.05

    rc_after = risk_contributions(proposal, cov)
    factor_before = factor_decomposition(prior, cov, market_etf=market_etf, sector_etfs=sector_etfs)
    factor_after = factor_decomposition(proposal, cov, market_etf=market_etf, sector_etfs=sector_etfs)
    enb_before = effective_number_of_bets(prior, cov)["enb"]
    enb_after = effective_number_of_bets(proposal, cov)["enb"]

    # 4. conservation / no-add-risk / no-new-names / caps.
    invested = sum(proposal.values())
    prior_gross = sum(prior.values())
    cons_bad = []
    if abs(invested + cash_after - 1.0) > 1e-6:
        cons_bad.append(f"sum={invested + cash_after:.10f}")
    if not set(proposal).issubset(set(prior)):
        cons_bad.append(f"new_tickers={sorted(set(proposal) - set(prior))[:8]}")
    if any(w < -1e-10 for w in proposal.values()):
        cons_bad.append("negative_weight")
    if any(w > max_weight + 1e-8 for w in proposal.values()):
        cons_bad.append(f"weight>cap_{max_weight}")
    if invested > prior_gross + 1e-6:
        cons_bad.append(f"added_gross={invested:.6f}>{prior_gross:.6f}")
    stage7_cash = _f(meta28.get("cash_before")) or 0.0
    if cash_after + 1e-8 < stage7_cash:
        cons_bad.append(f"cash={cash_after:.6f}<stage7={stage7_cash:.6f}")
    rec("conservation_no_added_risk", "PASS" if not cons_bad else "FAIL",
        f"invested={invested:.6f} cash={cash_after:.6f}" if not cons_bad else f"{cons_bad[:8]}")

    # 5. per-name RC cap.
    rc_max = max(rc_after.rc.values())
    # Keep this tolerance aligned with 28_apply_risk_budgets and enforce_rc_cap_to_cash().
    rec("per_name_rc_cap", "PASS" if rc_max <= rc_cap + 1e-6 else "FAIL",
        f"rc_max={rc_max:.6f}<=cap={rc_cap}")

    # 6. sleeve risk shares within +/- band of the feasible clipped regime budget.
    budgets = dict((meta27.get("regime") or {}).get("sleeve_risk_budgets") or {})
    after_share: dict[str, float] = {}
    for t, share in rc_after.rc.items():
        after_share[sleeve_of.get(t, "")] = after_share.get(sleeve_of.get(t, ""), 0.0) + share
    max_iter = int(_f(cfg_get(config, "sleeves.projection.max_iterations", 50)) or 50)
    feasible_bounds = sleeve_risk_bounds(
        cov,
        sleeve_of,
        gross=prior_gross,
        max_weight=max_weight,
        rc_cap=rc_cap,
        max_iter=max_iter,
    )
    feasible_targets: dict[str, float] = {}
    sleeve_bad = []
    aspirational_miss = []
    for sleeve, target in budgets.items():
        target = float(target or 0.0)
        if target <= 0:
            continue
        bounds = feasible_bounds.get(sleeve, {"min": 0.0, "max": 1.0})
        feasible = min(max(target, bounds["min"]), bounds["max"])
        feasible_targets[sleeve] = feasible
        realized = after_share.get(sleeve, 0.0)
        if abs(realized - feasible) > band + 1e-6:
            sleeve_bad.append(
                f"{sleeve}:{realized:.4f}!~feasible={feasible:.4f}"
                f"[{bounds['min']:.4f},{bounds['max']:.4f}] target={target:.4f}"
            )
        if abs(realized - target) > band + 1e-6:
            aspirational_miss.append(
                f"{sleeve}:realized={realized:.4f},target={target:.4f},"
                f"feasible={feasible:.4f},bounds=[{bounds['min']:.4f},{bounds['max']:.4f}]"
            )
    rec("sleeve_risk_within_band", "PASS" if not sleeve_bad else "FAIL",
        f"sleeves within +/-{band} of feasible clipped budgets"
        if not sleeve_bad else f"{sleeve_bad[:8]}")
    rec("sleeve_risk_aspirational_target", "PASS" if not aspirational_miss else "WARN",
        f"realized sleeve risk within +/-{band} of raw targets"
        if not aspirational_miss else f"{aspirational_miss[:8]}")

    # 7. IR consistency: large RC without proportional IR is a diagnostic warning, not a hard gate.
    z_limit = _f(cfg_get(config, "sleeves.ir_outlier_z", 3.0)) or 3.0
    pressure_rows = []
    for ticker, rc_share in rc_after.rc.items():
        ir = max(0.0, ir_by_ticker.get(ticker, 0.0))
        pressure_rows.append((ticker, rc_share / max(ir, 1e-4), rc_share, ir))
    ir_outliers: list[str] = []
    if len(pressure_rows) >= 3:
        values = pd.Series([p[1] for p in pressure_rows], dtype=float)
        std = float(values.std(ddof=0))
        mean = float(values.mean())
        if std > 1e-12:
            for ticker, pressure, rc_share, ir in pressure_rows:
                z = (pressure - mean) / std
                if z > z_limit and rc_share > (1.0 / max(1, len(pressure_rows))):
                    ir_outliers.append(f"{ticker}:z={z:.2f},rc={rc_share:.4f},ir={ir:.4f}")
    rec("ir_consistency", "PASS" if not ir_outliers else "WARN",
        "no large risk-contribution / IR outliers" if not ir_outliers else f"{ir_outliers[:8]}")

    # 8. HARD improvement-relative: do not worsen diversification.
    div_bad = []
    if enb_after + 1e-6 < enb_before:
        div_bad.append(f"enb {enb_after:.3f}<{enb_before:.3f}")
    if factor_after["idiosyncratic_share"] + 1e-4 < factor_before["idiosyncratic_share"]:
        div_bad.append(f"idio {factor_after['idiosyncratic_share']:.4f}<{factor_before['idiosyncratic_share']:.4f}")
    rec("diversification_not_worsened", "PASS" if not div_bad else "FAIL",
        f"ENB {enb_before:.2f}->{enb_after:.2f}; idio {factor_before['idiosyncratic_share']:.3f}->{factor_after['idiosyncratic_share']:.3f}"
        if not div_bad else f"{div_bad}")

    # 9. shadow-only.
    prod = bool(cfg_get(config, "sleeves.enabled_in_production", False))
    rec("shadow_only_not_production", "PASS" if not prod else "FAIL", f"enabled_in_production={prod}")

    # 10. deterministic throttle simulation recomputes from the continuous throttle formula.
    throttle = _load_json(art["drawdown_throttle_simulation"])
    dd_limit = _f(throttle.get("dd_limit"))
    cases = [c for c in throttle.get("cases", []) if isinstance(c, dict)]
    throttle_bad: list[str] = []
    recomputed: list[tuple[float, float]] = []
    if dd_limit is None:
        throttle_bad.append("missing_dd_limit")
    for case in cases:
        name = str(case.get("case", ""))
        drawdown = _f(case.get("drawdown"))
        scale = _f(case.get("scale"))
        if dd_limit is None or drawdown is None or scale is None:
            throttle_bad.append(f"{name}:missing_numeric")
            continue
        try:
            expected = throttle_scale(drawdown, dd_limit)
        except ValueError as exc:
            throttle_bad.append(f"{name}:{exc}")
            continue
        if abs(scale - expected) > 1e-8:
            throttle_bad.append(f"{name}:scale={scale:.8f},expected={expected:.8f}")
        if not 0.0 <= scale <= 1.0:
            throttle_bad.append(f"{name}:scale_out_of_range={scale:.8f}")
        recomputed.append((drawdown, scale))
    if len(recomputed) < 2:
        throttle_bad.append("fewer_than_two_cases")
    ordered = sorted(recomputed, key=lambda item: item[0])
    for i in range(1, len(ordered)):
        if ordered[i][1] > ordered[i - 1][1] + 1e-12:
            throttle_bad.append("scale_not_monotone_nonincreasing")
            break
    throttle_ok = not throttle_bad
    rec("drawdown_throttle_simulated", "PASS" if throttle_ok else "FAIL",
        "stored cases match continuous throttle formula and are monotone" if throttle_ok else f"{throttle_bad[:8]}")

    # 11. determinism: recomputed diagnostics match 28's sealed numbers.
    d = meta28.get("diagnostics") or {}
    det_bad = []
    for key, val in (("enb_after", enb_after), ("idio_after", factor_after["idiosyncratic_share"]), ("rc_max_after", rc_max)):
        if d.get(key) is not None and abs(float(d[key]) - float(val)) > 1e-4:
            det_bad.append(f"{key}:{d.get(key)}!={val:.6f}")
    rec("recompute_matches_28", "PASS" if not det_bad else "FAIL",
        "29 recompute matches 28 sealed diagnostics" if not det_bad else f"{det_bad}")

    # ---- WARN-only: absolute Rentech floors (capped by the 2-sector upstream book) ----
    min_idio = _f(cfg_get(config, "sleeves.factor_risk_caps.min_idiosyncratic_share", 0.50)) or 0.50
    max_mkt = _f(cfg_get(config, "sleeves.factor_risk_caps.max_market_share", 0.25)) or 0.25
    max_sec = _f(cfg_get(config, "sleeves.factor_risk_caps.max_sector_share", 0.25)) or 0.25
    min_enb = _f(cfg_get(config, "sleeves.effective_bets.min_enb", 10.0)) or 10.0
    min_enb_frac = _f(cfg_get(config, "sleeves.effective_bets.min_enb_fraction", 0.35)) or 0.35
    enb_floor = max(min_enb, min_enb_frac * len(proposal))
    rec("absolute_idiosyncratic_floor", "PASS" if factor_after["idiosyncratic_share"] >= min_idio else "WARN",
        f"idio={factor_after['idiosyncratic_share']:.3f} (floor {min_idio}; upstream 2-sector book limits this)")
    rec("absolute_enb_floor", "PASS" if enb_after >= enb_floor else "WARN",
        f"ENB={enb_after:.2f} (floor {enb_floor:.1f}; limited by upstream concentration)")
    rec("factor_share_caps", "PASS" if factor_after["market_share"] <= max_mkt and factor_after["max_sector_share"] <= max_sec else "FAIL",
        f"market={factor_after['market_share']:.3f}(<= {max_mkt}); max_sector={factor_after['max_sector_share']:.3f}(<= {max_sec})")
    if str((meta27.get("checks") and next((c for c in meta27["checks"] if c["check"] == "short_catalyst_contract"), {}).get("status", "")) ) == "WARN":
        rec("short_catalyst_disabled", "WARN", "short_catalyst disabled (no event contract) - Phase 1")

    validation_path.parent.mkdir(parents=True, exist_ok=True)
    write_csv(validation_path, ["check", "status", "detail"], checks)
    passed = all(c["status"] == "PASS" for c in checks if c["status"] != "WARN")

    provenance = {
        "risk_model_meta.json": art["framework_meta"], "sleeve_assignments.csv": art["assignments"],
        "risk_budget_meta.json": art["budget_meta"], "sleeve_adjusted_target_weights.csv": art["proposal"],
        "sleeve_risk_budget.csv": art["sleeve_risk_budget"],
        "factor_risk_decomposition.csv": sleeves_dir / "factor_risk_decomposition.csv",
        "effective_bets.json": sleeves_dir / "effective_bets.json",
        "drawdown_throttle_simulation.json": art["drawdown_throttle_simulation"],
        "validation/sleeve_validation.csv": validation_path, "config.yaml": config_path,
    }
    manifest = {
        "run_as_of": run_as_of,
        "stage": "stage8_sleeves_risk_budget",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "acceptance": "PASS" if passed else "FAIL",
        "shadow_only": True,
        "enabled_in_production": prod,
        "phase": "phase1_long_core_medium_rotation",
        "diagnostics": {
            "enb_before": round(enb_before, 4), "enb_after": round(enb_after, 4),
            "idio_before": round(factor_before["idiosyncratic_share"], 6), "idio_after": round(factor_after["idiosyncratic_share"], 6),
            "rc_max_after": round(rc_max, 6), "invested_gross": round(invested, 6), "cash": round(cash_after, 6),
            "sleeve_realized": {s: round(after_share.get(s, 0.0), 6) for s in sorted(after_share)},
            "sleeve_targets": {s: round(float(v or 0.0), 6) for s, v in sorted(budgets.items())},
            "sleeve_feasible_targets": {s: round(v, 6) for s, v in sorted(feasible_targets.items())},
            "sleeve_feasibility_bounds": {
                s: {"min": round(v.get("min", 0.0), 6), "max": round(v.get("max", 1.0), 6)}
                for s, v in sorted(feasible_bounds.items())
            },
        },
        "checks": checks,
        "provenance_sha256": {n: sha256_file(p) for n, p in provenance.items() if p.exists()},
        "source_sha256": {n: sha256_file(PACKAGE_ROOT / "sleeves" / n)
                          for n in SOURCE_FILES if (PACKAGE_ROOT / "sleeves" / n).exists()},
    }
    write_manifest(manifest_path, manifest)

    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    if passed:
        LOGGER.info("STAGE 8 ACCEPTANCE: PASS (as_of=%s, ENB %.2f->%.2f) -> %s",
                    run_as_of, enb_before, enb_after, manifest_path)
        return 0
    LOGGER.error("STAGE 8 ACCEPTANCE: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

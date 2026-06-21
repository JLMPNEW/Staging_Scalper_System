#!/usr/bin/env python3
"""Stage 3 - AQR-only baseline book: long-only mean-variance on the injected Stage 2 covariance.

Universe (LOCKED): investable_eligible=1 AND risk_eligible=1 AND role=scored AND ticker in covariance.csv.
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

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv  # noqa: E402
from portfolio_layer.core.db import connect, finish_run, start_run  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_database_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.optimizer.optimizer_core import (  # noqa: E402
    finalize_long_only_weights, solve_long_only_mv, weight_sensitivity_band,
)
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("run_portfolio_optimizer")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
WEIGHT_FIELDS = [
    "ticker", "sector", "industry", "source_pipeline", "rating", "final_score", "score_confidence",
    "mu_raw", "mu_used", "weight", "weight_band_low", "weight_band_high",
]
EXCLUDED_FIELDS = ["ticker", "source_pipeline", "investable_eligible", "risk_eligible", "role",
                   "risk_status", "exclusion_reason"]


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
    return p.parse_args()


def _f(value: object, default: float = 0.0) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
        return out if np.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def stage3_readiness(run_dir: Path, risk_dir: Path) -> list[dict[str, str]]:
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
    readiness = stage3_readiness(run_dir, risk_dir)
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
    use_conf = bool(oc.get("use_confidence_adjusted_mu", True))
    risk_aversion = float(oc.get("risk_aversion", 5.0))
    gross = float(oc.get("gross_exposure", 1.0))
    max_weight = float(oc.get("max_weight_per_name", 0.05))
    min_hold = float(oc.get("min_weight_to_hold", 0.0005))
    solver = str(oc.get("solver", "ECOS"))
    band_gammas = [float(g) for g in (oc.get("sensitivity_band_gammas") or [risk_aversion])]
    if not any(abs(g - risk_aversion) < 1e-12 for g in band_gammas):
        band_gammas.append(risk_aversion)
    band_gammas = sorted(set(band_gammas))

    scores = {r["ticker"]: r for r in read_csv(scores_path)}
    coverage = {r["ticker"]: r for r in read_csv(coverage_path)}
    covariance = pd.read_csv(cov_path, index_col=0)
    covariance.index = [str(i) for i in covariance.index]
    covariance.columns = [str(c) for c in covariance.columns]
    cov_tickers = set(covariance.index)

    # Locked universe: scored equities only, eligible by both gates, with a covariance row.
    universe: list[str] = []
    excluded: list[dict] = []
    for ticker, srow in scores.items():
        if str(srow.get("investable_eligible", "")).strip() != "1":
            continue
        crow = coverage.get(ticker, {})
        risk_eligible = str(crow.get("risk_eligible", "")).strip() == "1"
        role = str(crow.get("role", "")).strip()
        in_cov = ticker in cov_tickers
        if risk_eligible and role == "scored" and in_cov:
            universe.append(ticker)
        else:
            reason = (
                "not_risk_eligible" if not risk_eligible
                else "role_not_scored" if role != "scored"
                else "missing_covariance_row"
            )
            excluded.append({
                "ticker": ticker, "source_pipeline": srow.get("source_pipeline", ""),
                "investable_eligible": 1, "risk_eligible": int(risk_eligible),
                "role": role, "risk_status": crow.get("risk_status", "missing"),
                "exclusion_reason": reason,
            })
    universe = sorted(universe)
    if not universe:
        LOGGER.error("Optimizer universe is empty after the locked eligibility filter")
        return 1

    mu_raw = np.array([_f(scores[t].get("final_score")) for t in universe])
    conf = np.array([_f(scores[t].get("score_confidence"), 1.0) for t in universe])
    mu_used = mu_raw * conf if use_conf else mu_raw
    sigma = covariance.loc[universe, universe].to_numpy(dtype=float)

    weights, info = solve_long_only_mv(
        mu_used, sigma, risk_aversion=risk_aversion, max_weight=max_weight, gross=gross, solver=solver,
    )
    if info["status"] not in ("optimal", "optimal_inaccurate"):
        LOGGER.error("Solver did not converge: %s", info)
        return 1
    # Drop dust then re-project to exact gross without breaching per-name caps.
    weights = finalize_long_only_weights(weights, min_weight=min_hold, max_weight=max_weight, gross=gross)
    band_low, band_high = weight_sensitivity_band(
        mu_used, sigma, gammas=band_gammas, min_weight=min_hold, max_weight=max_weight, gross=gross,
        solver=solver,
    )

    rows = []
    for i, t in enumerate(universe):
        srow = scores[t]
        rows.append({
            "ticker": t, "sector": srow.get("sector", ""), "industry": srow.get("industry", ""),
            "source_pipeline": srow.get("source_pipeline", ""), "rating": srow.get("rating", ""),
            "final_score": round(mu_raw[i], 6), "score_confidence": round(float(conf[i]), 4),
            "mu_raw": round(mu_raw[i], 6), "mu_used": round(float(mu_used[i]), 6),
            "weight": round(float(weights[i]), 10),
            "weight_band_low": round(float(band_low[i]), 10),
            "weight_band_high": round(float(band_high[i]), 10),
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
        "max_weight_per_name": max_weight,
        "min_weight_to_hold": min_hold,
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
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")

    with connect(db_path) as conn:
        run_id = start_run(conn, run_type="run_portfolio_optimizer", input_path=scores_path)
        finish_run(conn, run_id=run_id, status="success", row_count=len(held),
                   message=f"as_of={run_as_of} held={len(held)} universe={len(universe)} excluded={len(excluded)}")

    LOGGER.info("AQR-only baseline: %d held / %d universe (gross=%.3f, max_wt=%.3f), %d excluded -> %s",
                len(held), len(universe), meta["sum_weights"], meta["max_weight_actual"], len(excluded), opt_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

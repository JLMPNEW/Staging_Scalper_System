#!/usr/bin/env python3
"""Stage 3 - validate the AQR-only baseline book and seal a provenance-hashed manifest."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, cast


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402
from scipy.stats import spearmanr  # noqa: E402

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("validate_optimizer_outputs")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Validate the Stage 3 baseline book.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, default=None)
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def finite_float(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if np.isfinite(parsed) else None


def manifest_hash(manifest: dict, rel_path: str) -> str | None:
    return ((manifest.get("files") or {}).get(rel_path) or {}).get("sha256")


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
    run_dir = runs_root / run_as_of
    risk_dir = run_dir / "risk"
    opt_dir = run_dir / "optimizer"
    weights_path = opt_dir / "target_weights.csv"
    excluded_path = opt_dir / "risk_excluded_candidates.csv"
    meta_path = opt_dir / "optimizer_meta.json"
    validation_path = opt_dir / "validation" / "optimizer_validation.csv"
    manifest_path = opt_dir / "optimizer_manifest.json"
    scores_path = run_dir / "stocks_scores.csv"
    coverage_path = risk_dir / "risk_coverage.csv"
    covariance_path = risk_dir / "covariance.csv"
    stage1_manifest_path = run_dir / "manifest.json"
    risk_manifest_path = risk_dir / "risk_manifest.json"

    for required in (
        weights_path, excluded_path, meta_path, scores_path, coverage_path, covariance_path,
        stage1_manifest_path, risk_manifest_path,
    ):
        if not required.exists():
            LOGGER.error("Missing required Stage 3 validation input: %s", required)
            return 1
    try:
        fail_if_exists([validation_path, manifest_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    oc = cfg_get(config, "optimizer", {})
    gross = float(oc.get("gross_exposure", 1.0))
    max_weight = float(oc.get("max_weight_per_name", 0.05))
    min_hold = float(oc.get("min_weight_to_hold", 0.0005))
    weight_tol = 1e-6
    cap_tol = 1e-8
    band_tol = 1e-8

    rows = read_csv(weights_path)
    excluded_rows = read_csv(excluded_path)
    excluded = {str(r.get("ticker", "")).strip() for r in excluded_rows if str(r.get("ticker", "")).strip()}
    meta = load_json(meta_path)
    stage1_manifest = load_json(stage1_manifest_path)
    risk_manifest = load_json(risk_manifest_path)
    scores = {r["ticker"]: r for r in read_csv(scores_path)}
    coverage = {r["ticker"]: r for r in read_csv(coverage_path)}
    cov_first_col = read_csv(covariance_path)
    cov_tickers = {str(r.get("ticker") or r.get("") or "").strip() for r in cov_first_col}
    cov_tickers.discard("")

    checks: list[dict] = []

    def rec(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    universe = [str(r.get("ticker", "")).strip() for r in rows]
    universe_counts = Counter(universe)
    duplicate_universe = sorted(t for t, count in universe_counts.items() if t and count > 1)
    book_set = {t for t in universe if t}
    held = [r for r in rows if (finite_float(r.get("weight")) or 0.0) > 0]

    # 1. exact optimizer universe: every eligible scored/risk-covered/cov-backed name is present once,
    # and no other name is in the optimizer book.
    expected_universe = {
        t for t, s in scores.items()
        if str(s.get("investable_eligible", "")).strip() == "1"
        and str(coverage.get(t, {}).get("risk_eligible", "")).strip() == "1"
        and str(coverage.get(t, {}).get("role", "")).strip() == "scored"
        and t in cov_tickers
    }
    missing_book = sorted(expected_universe - book_set)
    extra_book = sorted(book_set - expected_universe)
    rec(
        "optimizer_universe_exact",
        "PASS" if not (missing_book or extra_book or duplicate_universe) else "FAIL",
        (
            f"universe={len(book_set)} exactly matches expected scored/risk-eligible/cov-backed names"
            if not (missing_book or extra_book or duplicate_universe)
            else f"missing={missing_book[:10]} extra={extra_book[:10]} duplicates={duplicate_universe[:10]}"
        ),
    )

    # 2. exact exclusion report: every investable name outside the optimizer universe is named, and no
    # optimizer-eligible name is wrongly reported as excluded.
    investable = {t for t, r in scores.items() if str(r.get("investable_eligible", "")).strip() == "1"}
    expected_excluded = investable - expected_universe
    missing_exclusions = sorted(expected_excluded - excluded)
    extra_exclusions = sorted(excluded - expected_excluded)
    rec(
        "risk_exclusions_exact",
        "PASS" if not (missing_exclusions or extra_exclusions) else "FAIL",
        (
            f"excluded={len(excluded)} exactly matches eligible names outside optimizer universe"
            if not (missing_exclusions or extra_exclusions)
            else f"missing={missing_exclusions[:10]} extra={extra_exclusions[:10]}"
        ),
    )

    # 3. numeric fields are finite. NaN/Inf here would corrupt optimizer handoff.
    numeric_fields = ["final_score", "score_confidence", "mu_raw", "mu_used",
                      "weight", "weight_band_low", "weight_band_high"]
    bad_numeric = []
    for r in rows:
        ticker = str(r.get("ticker", ""))
        for field in numeric_fields:
            if finite_float(r.get(field)) is None:
                bad_numeric.append(f"{ticker}:{field}")
    rec(
        "numeric_fields_finite",
        "PASS" if not bad_numeric else "FAIL",
        "all optimizer numeric fields are finite" if not bad_numeric else f"non-finite: {bad_numeric[:10]}",
    )

    # 4. weights valid: long-only, capped, no dust holdings, and sum to gross with strict tolerance.
    w = np.array([finite_float(r.get("weight")) if finite_float(r.get("weight")) is not None else np.nan for r in rows])
    bad_w = []
    if len(w) == 0:
        bad_w.append("empty_weights")
    if not np.isfinite(w).all():
        bad_w.append("nonfinite_weights")
    else:
        if (w < -cap_tol).any():
            bad_w.append("negative_weights")
        if (w > max_weight + cap_tol).any():
            bad_w.append(f"cap_breach>{max_weight}")
        if min_hold > 0 and ((w > cap_tol) & (w < min_hold - cap_tol)).any():
            bad_w.append(f"dust_weight_below_min_hold>{min_hold}")
        if abs(float(w.sum()) - gross) > weight_tol:
            bad_w.append(f"sum={float(w.sum()):.10f}!={gross}")
    rec(
        "weights_valid",
        "PASS" if not bad_w else "FAIL",
        (
            f"long-only, <= {max_weight}, no dust, sum={float(np.nansum(w)):.10f}"
            if not bad_w else f"{bad_w}"
        ),
    )

    # 5. sensitivity bands are post-finalization bands, so the published weight must be contained.
    band_bad = []
    for r in rows:
        t = str(r.get("ticker", ""))
        wt = finite_float(r.get("weight"))
        low = finite_float(r.get("weight_band_low"))
        high = finite_float(r.get("weight_band_high"))
        if wt is None or low is None or high is None:
            band_bad.append(f"{t}:nonfinite")
        elif low > high + band_tol:
            band_bad.append(f"{t}:low_gt_high")
        elif wt < low - band_tol or wt > high + band_tol:
            band_bad.append(f"{t}:weight_outside_band")
    rec(
        "weight_bands_contain_weights",
        "PASS" if not band_bad else "FAIL",
        "all weights fall inside finalized sensitivity bands" if not band_bad else f"violations: {band_bad[:10]}",
    )

    # 6. positive mu->weight relationship among held; negative-mu holdings allowed but reported.
    held_pairs = [
        (finite_float(r.get("mu_used")), finite_float(r.get("weight")), r["ticker"])
        for r in held
    ]
    valid_pairs = [(mu, wt, ticker) for mu, wt, ticker in held_pairs if mu is not None and wt is not None]
    mu = np.array([p[0] for p in valid_pairs], dtype=float)
    wh = np.array([p[1] for p in valid_pairs], dtype=float)
    rho = float(cast(Any, spearmanr(mu, wh)).correlation) if len(valid_pairs) > 2 else float("nan")
    neg_mu_held = [ticker for mu_val, _, ticker in valid_pairs if float(mu_val) < 0]
    ok_rel = (np.isnan(rho) or rho > 0)
    rec(
        "mu_weight_relationship",
        "PASS" if ok_rel else "FAIL",
        f"spearman(mu_used,weight)={round(rho, 3)}; held_with_negative_mu={len(neg_mu_held)} "
        f"(diversifiers; allowed)",
    )

    # 7. optimizer meta matches current output files.
    meta_bad = []
    if int(meta.get("universe_size", -1)) != len(book_set):
        meta_bad.append(f"universe_size={meta.get('universe_size')}!={len(book_set)}")
    if int(meta.get("n_held", -1)) != len(held):
        meta_bad.append(f"n_held={meta.get('n_held')}!={len(held)}")
    if int(meta.get("n_excluded_candidates", -1)) != len(excluded):
        meta_bad.append(f"n_excluded_candidates={meta.get('n_excluded_candidates')}!={len(excluded)}")
    if np.isfinite(w).all() and abs(float(meta.get("sum_weights", np.nan)) - float(w.sum())) > 5e-6:
        meta_bad.append(f"sum_weights_meta={meta.get('sum_weights')} csv={float(w.sum()):.10f}")
    if np.isfinite(w).all() and abs(float(meta.get("max_weight_actual", np.nan)) - float(w.max(initial=0.0))) > 5e-6:
        meta_bad.append(f"max_weight_meta={meta.get('max_weight_actual')} csv={float(w.max(initial=0.0)):.10f}")
    rec(
        "optimizer_meta_matches_outputs",
        "PASS" if not meta_bad else "FAIL",
        "optimizer_meta agrees with target/exclusion outputs" if not meta_bad else f"{meta_bad}",
    )

    # 8. upstream sealed inputs are still exactly the artifacts Stage 1/2 accepted.
    stocks_hash = sha256_file(scores_path)
    coverage_hash = sha256_file(coverage_path)
    covariance_hash = sha256_file(covariance_path)
    upstream_bad = []
    if stage1_manifest.get("hard_gate_acceptance") != "PASS":
        upstream_bad.append(f"stage1_hard_gate_acceptance={stage1_manifest.get('hard_gate_acceptance')}")
    if stocks_hash != manifest_hash(stage1_manifest, "stocks_scores.csv"):
        upstream_bad.append("stocks_scores_hash_mismatch_stage1_manifest")
    if risk_manifest.get("acceptance") != "PASS":
        upstream_bad.append(f"stage2_acceptance={risk_manifest.get('acceptance')}")
    if coverage_hash != manifest_hash(risk_manifest, "risk_coverage.csv"):
        upstream_bad.append("risk_coverage_hash_mismatch_stage2_manifest")
    if covariance_hash != manifest_hash(risk_manifest, "covariance.csv"):
        upstream_bad.append("covariance_hash_mismatch_stage2_manifest")
    rec(
        "upstream_manifests_valid",
        "PASS" if not upstream_bad else "FAIL",
        "Stage 1/2 manifests pass and hashes match consumed artifacts" if not upstream_bad else f"{upstream_bad}",
    )

    # 9. covariance injected from the accepted Stage 2 artifact, never rebuilt inside Stage 3.
    cov_match_current = meta.get("covariance_sha256") == covariance_hash
    cov_match_stage2 = meta.get("covariance_sha256") == manifest_hash(risk_manifest, "covariance.csv")
    rec(
        "covariance_injected_from_stage2",
        "PASS" if cov_match_current and cov_match_stage2 and meta.get("covariance_source") == "stage2_risk_covariance_csv" else "FAIL",
        f"covariance_source={meta.get('covariance_source')} current_hash_match={cov_match_current} "
        f"stage2_hash_match={cov_match_stage2}",
    )

    # 10. solver converged.
    solver = meta.get("solver", {})
    rec(
        "solver_optimal",
        "PASS" if solver.get("status") in ("optimal", "optimal_inaccurate") else "FAIL",
        f"solver={solver.get('solver_used')} status={solver.get('status')} obj={solver.get('objective_value')}",
    )

    validation_path.parent.mkdir(parents=True, exist_ok=True)
    write_csv(validation_path, ["check", "status", "detail"], checks)

    passed = all(c["status"] == "PASS" for c in checks)
    # Provenance: hash every input that determines the book, incl. upstream manifests + vendored source.
    provenance_paths = {
        "stocks_scores.csv": scores_path,
        "risk_coverage.csv": coverage_path,
        "covariance.csv": covariance_path,
        "stage1_manifest.json": stage1_manifest_path,
        "stage2_risk_manifest.json": risk_manifest_path,
        "config.yaml": config_path,
        "target_weights.csv": weights_path,
        "risk_excluded_candidates.csv": excluded_path,
        "optimizer_meta.json": meta_path,
        "validation/optimizer_validation.csv": validation_path,
        "vendored/tier1_portfolio_optimizer.py": PACKAGE_ROOT / "optimizer" / "tier1_portfolio_optimizer.py",
        "vendored/tier1_common.py": PACKAGE_ROOT / "optimizer" / "tier1_common.py",
        "optimizer_core.py": PACKAGE_ROOT / "optimizer" / "optimizer_core.py",
    }
    provenance = {name: sha256_file(p) for name, p in provenance_paths.items() if p.exists()}
    manifest = {
        "run_as_of": run_as_of,
        "stage": "stage3_aqr_baseline",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "acceptance": "PASS" if passed else "FAIL",
        "universe_size": len(book_set),
        "n_held": len(held),
        "n_excluded_candidates": len(excluded),
        "optimizer_config": oc,
        "provenance_sha256": provenance,
        "checks": checks,
    }
    write_manifest(manifest_path, manifest)

    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    if passed:
        LOGGER.info("STAGE 3 ACCEPTANCE: PASS (as_of=%s, %d held) -> %s", run_as_of, len(held), manifest_path)
        return 0
    LOGGER.error("STAGE 3 ACCEPTANCE: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

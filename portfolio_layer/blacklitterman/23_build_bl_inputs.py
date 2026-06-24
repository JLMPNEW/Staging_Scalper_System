#!/usr/bin/env python3
"""Stage 7 - build sealed Black-Litterman optimizer inputs from sealed upstream contracts (SHADOW-ONLY).

This is the adapter/probe step. It converts sealed Stage 1/2/3/5/6 artifacts into optimizer-ready inputs
and a generated, sealed tier1 config that references ONLY run-local sealed files. It runs contract-probe +
pre-solve feasibility checks and stops before any optimizer solve (that is Stage 7 step 24).

Units = annualized (option B): only Stage 1 final_score becomes an expected-return view. Rating, rotation
State, and macro regime adjust confidence / gross / sector budgets / alpha multipliers - never returns.
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

import yaml


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("build_bl_inputs")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SOURCE_FILES = ["23_build_bl_inputs.py"]

VIEW_FIELDS = ["ticker", "source_pipeline", "expected_alpha_annual", "score_confidence", "rating",
               "tier1_rating", "sector_state", "alpha_units"]
OPT_STOCK_FIELDS = ["Ticker", "Company", "sector", "industry", "industry_aggregate", "Rating",
                    "FinalScore", "ExpectedAlphaAnnual", "ScoreConfidence", "SourcePipeline",
                    "BaseOptimizerEligible"]
SECTOR_TARGET_FIELDS = ["sector_name", "target_weight", "baseline_weight", "baseline_source", "macro_fit_score",
                        "macro_fit_z", "raw_shift", "clipped_shift", "realized_shift",
                        "rotation_state"]
BENCHMARK_FIELDS = ["Ticker", "Weight", "source_pipeline", "within_sector_source", "sector_target_weight",
                    "within_sector_weight"]
FOREIGN_BUDGET_FIELDS = ["region", "min_budget", "max_budget", "active_flag", "macro_foreign_budget",
                         "activation_policy"]
TIER1_RATING = {
    "strong_buy": "Strong Buy",
    "buy": "Buy",
    "hold": "Hold",
    "reduce": "Sell",
    "avoid": "Strong Sell",
}
TIER1_RATING_ORDER = ["Strong Buy", "Buy", "Hold", "Sell", "Strong Sell"]

# (manifest path, acceptance keys to accept as PASS, {artifact_name: path-relative-to-run})
UPSTREAM = "upstream"


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build Stage 7 Black-Litterman optimizer inputs (shadow-only).")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, default=None)
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def _f(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _recorded_hash(manifest: dict, name: str) -> str | None:
    prov = manifest.get("provenance_sha256") or {}
    if name in prov:
        return prov[name]
    files = manifest.get("files") or {}
    info = files.get(name)
    if isinstance(info, dict):
        return info.get("sha256")
    return None


def _acceptance_ok(manifest: dict) -> bool:
    for key in ("acceptance", "hard_gate_acceptance"):
        val = manifest.get(key)
        if isinstance(val, str):
            return val.upper().startswith("PASS")
    # Stage 1 manifest has no explicit acceptance string; treat presence as ok (hash check still applies).
    return True


def _zscores(values: list[float]) -> list[float]:
    n = len(values)
    if n == 0:
        return []
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    sd = math.sqrt(var)
    if sd <= 1e-12:
        return [0.0 for _ in values]
    return [(v - mean) / sd for v in values]


def _renormalize(weights: dict[str, float]) -> dict[str, float]:
    total = sum(max(0.0, w) for w in weights.values())
    if total <= 0:
        n = len(weights)
        return {k: 1.0 / n for k in weights} if n else {}
    return {k: max(0.0, w) / total for k, w in weights.items()}


def _renormalize_with_floors(weights: dict[str, float], floors: dict[str, float]) -> dict[str, float]:
    keys = list(weights)
    floor_vals = {k: max(0.0, min(1.0, floors.get(k, 0.0))) for k in keys}
    floor_sum = sum(floor_vals.values())
    if floor_sum >= 1.0:
        return _renormalize(floor_vals)
    excess = {k: max(0.0, weights.get(k, 0.0) - floor_vals.get(k, 0.0)) for k in keys}
    excess_sum = sum(excess.values())
    if excess_sum <= 0.0:
        return dict(floor_vals)
    scale = (1.0 - floor_sum) / excess_sum
    return {k: floor_vals[k] + excess[k] * scale for k in keys}


def _tier1_rating(label: Any) -> str:
    key = str(label or "").strip().lower().replace(" ", "_")
    return TIER1_RATING.get(key, "Hold")


def _price_panel_date_range(path: Path) -> tuple[str, str]:
    first = ""
    last = ""
    with path.open("r", encoding="utf-8") as fh:
        _ = fh.readline()
        for line in fh:
            if not line.strip():
                continue
            day = line.split(",", 1)[0].strip()
            if not day:
                continue
            if not first:
                first = day
            last = day
    if not first or not last:
        raise ValueError(f"price panel has no dated rows: {path}")
    return first, last


def _tier1_periods_per_year(freq: str) -> int:
    f = (freq or "").upper()
    if f.startswith("D"):
        return 252
    if f.startswith("W"):
        return 52
    if f.startswith("M"):
        return 12
    if f.startswith("Q"):
        return 4
    return 252


def _returns_frequency_from_cov_meta(annualization_factor: float | None, stage2_frequency: str) -> str:
    if annualization_factor is not None and math.isfinite(annualization_factor):
        rounded = int(round(annualization_factor))
        if abs(annualization_factor - rounded) <= 1e-9:
            if rounded == 252:
                return "D"
            if rounded == 52:
                return "W-FRI"
            if rounded == 12:
                return "M"
            if rounded == 4:
                return "Q"
    freq = stage2_frequency.strip().lower()
    if freq.startswith("week"):
        return "W-FRI"
    if freq.startswith("month"):
        return "M"
    if freq.startswith("quarter"):
        return "Q"
    return "D"


def main() -> int:  # noqa: C901
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    runs_root = paths.output_dir / "runs"
    run_as_of = args.as_of or latest_run_with(runs_root, "manifest.json")
    if not run_as_of:
        LOGGER.error("No Stage 1 run found under %s", runs_root)
        return 1
    run_dir = runs_root / run_as_of
    bl = cfg_get(config, "black_litterman_fusion", {}) or {}

    # ---- sealed upstream artifacts ----
    art = {
        "scores": run_dir / "stocks_scores.csv",
        "stage1_manifest": run_dir / "manifest.json",
        "covariance": run_dir / "risk" / "covariance.csv",
        "cov_meta": run_dir / "risk" / "covariance_meta.json",
        "prices": run_dir / "risk" / "prices_adjclose.csv",
        "risk_coverage": run_dir / "risk" / "risk_coverage.csv",
        "risk_manifest": run_dir / "risk" / "risk_manifest.json",
        "target_weights": run_dir / "optimizer" / "target_weights.csv",
        "optimizer_manifest": run_dir / "optimizer" / "optimizer_manifest.json",
        "sector_rotation": run_dir / "rotation" / "sector_rotation.csv",
        "sector_rotation_optimizer": run_dir / "rotation" / "sector_rotation_optimizer.csv",
        "foreign_etfs_optimizer": run_dir / "rotation" / "foreign_etfs_optimizer.csv",
        "rotation_manifest": run_dir / "rotation" / "rotation_manifest.json",
        "macro_sector_fit": run_dir / "macro" / "macro_sector_fit.csv",
        "macro_regime": run_dir / "macro" / "macro_regime.csv",
        "macro_foreign_budget": run_dir / "macro" / "macro_foreign_budget.csv",
        "macro_manifest": run_dir / "macro" / "macro_manifest.json",
    }
    missing = [k for k, p in art.items() if not p.exists()]
    if missing:
        LOGGER.error("Missing sealed upstream artifacts: %s", missing)
        return 1

    out_dir = run_dir / "blacklitterman"
    out = {
        "bl_views.csv": out_dir / "bl_views.csv",
        "bl_stocks_scores_optimizer.csv": out_dir / "bl_stocks_scores_optimizer.csv",
        "bl_sector_targets_optimizer.csv": out_dir / "bl_sector_targets_optimizer.csv",
        "bl_benchmark_weights.csv": out_dir / "bl_benchmark_weights.csv",
        "bl_foreign_budget_optimizer.csv": out_dir / "bl_foreign_budget_optimizer.csv",
        "bl_optimizer_config.yaml": out_dir / "bl_optimizer_config.yaml",
        "bl_inputs_meta.json": out_dir / "bl_inputs_meta.json",
    }
    probe_path = out_dir / "validation" / "bl_inputs_probe.csv"
    if args.force:
        downstream = [
            out_dir / "bl_target_weights.csv",
            out_dir / "bl_optimizer_summary.csv",
            out_dir / "bl_optimizer_meta.json",
            out_dir / "validation" / "bl_optimizer_validation.csv",
            out_dir / "optimizer" / "weights_long_only.csv",
            out_dir / "optimizer" / "weights_long_short.csv",
            out_dir / "optimizer" / "weights_user_portfolio.csv",
            out_dir / "optimizer" / "optimization_results.csv",
        ]
        for p in list(out.values()) + [probe_path, *downstream]:
            if p.exists():
                p.unlink()
    try:
        fail_if_exists(list(out.values()), force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    checks: list[dict] = []

    def rec(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    # ---- load sealed manifests ----
    manifests = {
        "stage1": (json.loads(art["stage1_manifest"].read_text(encoding="utf-8")), [("stocks_scores.csv", art["scores"])]),
        "stage2": (json.loads(art["risk_manifest"].read_text(encoding="utf-8")),
                   [("covariance.csv", art["covariance"]), ("prices_adjclose.csv", art["prices"]),
                    ("risk_coverage.csv", art["risk_coverage"])]),
        "stage3": (json.loads(art["optimizer_manifest"].read_text(encoding="utf-8")), [("target_weights.csv", art["target_weights"])]),
        "stage5": (json.loads(art["rotation_manifest"].read_text(encoding="utf-8")),
                   [("sector_rotation.csv", art["sector_rotation"]),
                    ("sector_rotation_optimizer.csv", art["sector_rotation_optimizer"]),
                    ("foreign_etfs_optimizer.csv", art["foreign_etfs_optimizer"])]),
        "stage6": (json.loads(art["macro_manifest"].read_text(encoding="utf-8")),
                   [("macro_sector_fit.csv", art["macro_sector_fit"]), ("macro_regime.csv", art["macro_regime"]),
                    ("macro_foreign_budget.csv", art["macro_foreign_budget"])]),
    }
    upstream_bad: list[str] = []
    for stage, (manifest, artifacts) in manifests.items():
        if not _acceptance_ok(manifest):
            upstream_bad.append(f"{stage}:acceptance={manifest.get('acceptance') or manifest.get('hard_gate_acceptance')}")
        for name, path in artifacts:
            recorded = _recorded_hash(manifest, name)
            if recorded is None:
                upstream_bad.append(f"{stage}:{name}:no_recorded_hash")
            elif recorded != sha256_file(path):
                upstream_bad.append(f"{stage}:{name}:hash_mismatch")
    rec("upstream_sealed_and_current", "PASS" if not upstream_bad else "FAIL",
        "Stages 1/2/3/5/6 manifests accept + hashes match" if not upstream_bad else f"{upstream_bad[:8]}")

    # ---- covariance universe + eligibility ----
    cov_header = art["covariance"].read_text(encoding="utf-8").splitlines()[0].split(",")
    cov_tickers = {c.strip().upper() for c in cov_header[1:] if c.strip()}
    cov_meta = json.loads(art["cov_meta"].read_text(encoding="utf-8"))
    cov_units = cov_meta.get("covariance_units")
    cov_annualization_factor = _f(cov_meta.get("annualization_factor"))
    cov_frequency = str(cov_meta.get("frequency", "") or "")
    returns_frequency = _returns_frequency_from_cov_meta(cov_annualization_factor, cov_frequency)

    score_rows = read_csv(art["scores"])
    coverage_rows = read_csv(art["risk_coverage"])
    risk_eligible = {str(r.get("ticker", "")).strip().upper() for r in coverage_rows
                     if str(r.get("risk_eligible", "")).strip() == "1" and str(r.get("role", "")).strip() == "scored"}
    scores_by_ticker = {str(r.get("ticker", "")).strip().upper(): r for r in score_rows}

    # rotation State per sleeve, macro fit per sleeve
    rotation_state = {str(r.get("source_pipeline", "")).strip(): str(r.get("state", "")).strip()
                      for r in read_csv(art["sector_rotation"])}
    macro_sector = {str(r.get("source_pipeline", "")).strip(): r for r in read_csv(art["macro_sector_fit"])}
    pipelines = sorted({str(r.get("source_pipeline", "")).strip() for r in score_rows
                        if str(r.get("source_pipeline", "")).strip()})

    # ---- BL optimization universe = investable_eligible AND risk_eligible AND in covariance ----
    universe = sorted(
        t for t, r in scores_by_ticker.items()
        if str(r.get("investable_eligible", "")).strip() == "1" and t in risk_eligible and t in cov_tickers
    )

    # ---- views (units B: final_score is the annual alpha) ----
    view_rows = []
    opt_stock_rows = []
    alpha_bad = []
    for t in universe:
        r = scores_by_ticker[t]
        pipe = str(r.get("source_pipeline", "")).strip()
        alpha = _f(r.get("final_score"))
        if alpha is None:
            alpha_bad.append(t)
            continue
        tier1_rating = _tier1_rating(r.get("rating"))
        score_conf = _f(r.get("score_confidence")) or 0.0
        view_rows.append({
            "ticker": t,
            "source_pipeline": pipe,
            "expected_alpha_annual": round(alpha, 10),
            "score_confidence": round(score_conf, 8),
            "rating": str(r.get("rating", "")).strip(),
            "tier1_rating": tier1_rating,
            "sector_state": rotation_state.get(pipe, "Neutral"),
            "alpha_units": "annual_return_decimal",
        })
        opt_stock_rows.append({
            "Ticker": t,
            "Company": str(r.get("company", "") or ""),
            "sector": pipe,
            "industry": str(r.get("industry", "") or ""),
            "industry_aggregate": str(r.get("industry_aggregate", "") or ""),
            "Rating": tier1_rating,
            "FinalScore": round(alpha, 10),
            "ExpectedAlphaAnnual": round(alpha, 10),
            "ScoreConfidence": round(score_conf, 8),
            "SourcePipeline": pipe,
            "BaseOptimizerEligible": 1,
        })

    # ---- sector budgets: strategic/stage3/count baseline shifted by bounded macro_fit ----
    stage3_by_pipe: dict[str, float] = {p: 0.0 for p in pipelines}
    pipe_of = {t: str(scores_by_ticker[t].get("source_pipeline", "")).strip() for t in scores_by_ticker}
    stage3_weights: dict[str, float] = {}
    for tr in read_csv(art["target_weights"]):
        t = str(tr.get("ticker", "")).strip().upper()
        w = _f(tr.get("weight")) or 0.0
        stage3_weights[t] = w
        p = pipe_of.get(t)
        if p and w > 0:
            stage3_by_pipe[p] = stage3_by_pipe.get(p, 0.0) + w
    if sum(stage3_by_pipe.values()) <= 0:  # pre-Stage-3 fallback: eligible-count share
        for t in universe:
            stage3_by_pipe[pipe_of.get(t, "")] = stage3_by_pipe.get(pipe_of.get(t, ""), 0.0) + 1.0
    stage3_by_pipe = _renormalize({p: stage3_by_pipe.get(p, 0.0) for p in pipelines})

    eligible_count_by_pipe: dict[str, float] = {p: 0.0 for p in pipelines}
    for t in universe:
        p = pipe_of.get(t, "")
        if p:
            eligible_count_by_pipe[p] = eligible_count_by_pipe.get(p, 0.0) + 1.0
    eligible_count_by_pipe = _renormalize(eligible_count_by_pipe)

    baseline_source = str(bl.get("macro_sector_baseline_source", "strategic_weights")).strip().lower()
    baseline_bad = []
    if baseline_source in {"strategic", "strategic_weights", "policy", "policy_weights"}:
        raw_strategic = cfg_get(config, "black_litterman_fusion.strategic_sector_weights", {}) or {}
        missing = [p for p in pipelines if _f(raw_strategic.get(p)) is None]
        negative = [p for p in pipelines if (_f(raw_strategic.get(p)) or 0.0) < 0.0]
        if missing:
            baseline_bad.append(f"missing_strategic_weights={missing}")
        if negative:
            baseline_bad.append(f"negative_strategic_weights={negative}")
        base_by_pipe = {p: max(0.0, _f(raw_strategic.get(p)) or 0.0) for p in pipelines}
        if sum(base_by_pipe.values()) <= 0.0:
            baseline_bad.append("strategic_weight_sum<=0")
            base_by_pipe = dict(stage3_by_pipe)
        else:
            base_by_pipe = _renormalize(base_by_pipe)
        baseline_source_label = "strategic_weights"
    elif baseline_source in {"stage3", "stage3_weights", "stage3_realized", "optimizer"}:
        base_by_pipe = dict(stage3_by_pipe)
        baseline_source_label = "stage3_weights"
    elif baseline_source in {"eligible", "eligible_count", "eligible_counts"}:
        base_by_pipe = dict(eligible_count_by_pipe)
        baseline_source_label = "eligible_count"
    else:
        baseline_bad.append(f"unknown_baseline_source={baseline_source}")
        base_by_pipe = dict(stage3_by_pipe)
        baseline_source_label = "stage3_weights_fallback"
    rec("sector_baseline_valid", "PASS" if not baseline_bad else "FAIL",
        f"source={baseline_source_label}; weights={{{', '.join(f'{p}:{base_by_pipe.get(p, 0.0):.4f}' for p in pipelines)}}}"
        if not baseline_bad else f"{baseline_bad[:8]}")

    fit_scores = [_f(macro_sector.get(p, {}).get("macro_fit_score")) or 0.0 for p in pipelines]
    fit_z = dict(zip(pipelines, _zscores(fit_scores)))
    shift_scale = _f(bl.get("macro_sector_shift_scale")) or 0.05
    max_shift = _f(bl.get("macro_sector_max_shift")) or 0.10
    max_rel_shift = _f(bl.get("macro_sector_max_relative_shift")) or 0.50
    min_floor = _f(bl.get("macro_sector_min_weight_floor")) or 0.0
    shift_mode = str(bl.get("macro_sector_shift_mode", "relative_with_floor")).strip().lower()
    shifted = {}
    raw_shift_by_pipe = {}
    clipped_shift = {}
    floors = {}
    for p in pipelines:
        raw_shift = shift_scale * fit_z.get(p, 0.0)
        base_w = base_by_pipe.get(p, 0.0)
        abs_cap = max_shift
        if shift_mode in {"relative", "relative_with_floor", "relative_floor"}:
            abs_cap = min(abs_cap, max_rel_shift * base_w)
        s = max(-abs_cap, min(abs_cap, raw_shift))
        raw_shift_by_pipe[p] = raw_shift
        clipped_shift[p] = s
        floors[p] = min(min_floor, base_w) if base_w > 0.0 else 0.0
        shifted[p] = max(0.0, base_by_pipe.get(p, 0.0) + s)
    if shift_mode in {"relative_with_floor", "relative_floor", "floor"}:
        sector_target = _renormalize_with_floors(shifted, floors)
    else:
        sector_target = _renormalize(shifted)
    sector_target_rows = [{
        "sector_name": p,
        "target_weight": round(sector_target.get(p, 0.0), 10),
        "baseline_weight": round(base_by_pipe.get(p, 0.0), 10),
        "baseline_source": baseline_source_label,
        "macro_fit_score": round(_f(macro_sector.get(p, {}).get("macro_fit_score")) or 0.0, 8),
        "macro_fit_z": round(fit_z.get(p, 0.0), 8),
        "raw_shift": round(raw_shift_by_pipe.get(p, 0.0), 10),
        "clipped_shift": round(clipped_shift.get(p, 0.0), 10),
        "realized_shift": round(sector_target.get(p, 0.0) - base_by_pipe.get(p, 0.0), 10),
        "rotation_state": rotation_state.get(p, "Neutral"),
    } for p in pipelines]

    # Ticker-level BL benchmark: preserve Stage-3 weights inside each sleeve when available, otherwise
    # equal-weight the sleeve. This is the tier1-supported way to express macro-shifted sector budgets.
    universe_by_pipe: dict[str, list[str]] = {p: [] for p in pipelines}
    for t in universe:
        p = pipe_of.get(t, "")
        if p:
            universe_by_pipe.setdefault(p, []).append(t)
    benchmark_rows = []
    within_source = str(bl.get("benchmark_within_sector_source", "equal")).strip().lower()
    benchmark_bad = []
    if within_source not in {"equal", "stage3", "stage3_weights", "stage3_or_equal"}:
        benchmark_bad.append(f"unknown_benchmark_within_sector_source={within_source}")
        within_source = "equal"
    for p in pipelines:
        tickers = sorted(universe_by_pipe.get(p, []))
        if not tickers:
            continue
        if within_source == "equal":
            within = {t: 1.0 / len(tickers) for t in tickers}
        else:
            positive = {t: max(0.0, stage3_weights.get(t, 0.0)) for t in tickers}
            denom = sum(positive.values())
            if denom <= 0.0:
                within = {t: 1.0 / len(tickers) for t in tickers}
            else:
                within = {t: positive[t] / denom for t in tickers}
        target = sector_target.get(p, 0.0)
        for t in tickers:
            benchmark_rows.append({
                "Ticker": t,
                "Weight": round(target * within[t], 12),
                "source_pipeline": p,
                "within_sector_source": "stage3_weights" if within_source in {"stage3", "stage3_weights", "stage3_or_equal"} else "equal",
                "sector_target_weight": round(target, 10),
                "within_sector_weight": round(within[t], 12),
            })
    rec("benchmark_within_sector_valid", "PASS" if not benchmark_bad else "FAIL",
        f"source={within_source}; rows={len(benchmark_rows)}" if not benchmark_bad else f"{benchmark_bad[:8]}")

    # ---- regime -> gross scalar ----
    regime_rows = read_csv(art["macro_regime"])
    regime_label = str(regime_rows[0].get("active_current_regime", "")).strip() if regime_rows else ""
    gross_map = cfg_get(config, "black_litterman_fusion.regime_to_gross_scalar", {}) or {}
    base_gross = _f(bl.get("base_gross_exposure")) or 1.0
    regime_scalar = _f(gross_map.get(regime_label, gross_map.get("default", 0.85))) or 0.85
    gross_exposure = round(base_gross * regime_scalar, 8)

    # ---- foreign budget (respect active_flag) ----
    fb = read_csv(art["macro_foreign_budget"])
    fb_row = fb[0] if fb else {}
    policy = str(bl.get("foreign_activation_policy", "respect_active_flag")).strip()
    active = str(fb_row.get("active_flag", "0")).strip() == "1"
    macro_budget = _f(fb_row.get("foreign_budget")) or 0.0
    if policy == "respect_active_flag" and not active:
        fmin, fmax = 0.0, 0.0
    else:
        fmin = _f(fb_row.get("min_budget")) or 0.0
        fmax = _f(fb_row.get("max_budget")) or 0.0
    foreign_rows = [{
        "region": "FOREIGN", "min_budget": round(fmin, 8), "max_budget": round(fmax, 8),
        "active_flag": 1 if active else 0, "macro_foreign_budget": round(macro_budget, 8),
        "activation_policy": policy,
    }]

    # ---- probe / feasibility checks ----
    rec("alpha_views_finite", "PASS" if not alpha_bad and view_rows else "FAIL",
        f"{len(view_rows)} annual-alpha views" if not alpha_bad and view_rows else f"non_finite={alpha_bad[:8]}")

    rng_bad = [r["ticker"] for r in view_rows if abs(_f(r["expected_alpha_annual"]) or 0.0) > 2.0]
    rec("alpha_units_annualized", "PASS" if cov_units == "annualized" and not rng_bad else "FAIL",
        f"covariance_units={cov_units}; |alpha|<=200%/yr" if cov_units == "annualized" and not rng_bad
        else f"cov_units={cov_units} out_of_range={rng_bad[:8]}")

    sector_pipe_set = {r["sector_name"] for r in sector_target_rows}
    view_pipe_set = {r["source_pipeline"] for r in view_rows}
    join_bad = []
    if sector_pipe_set != set(pipelines):
        join_bad.append(f"sector_targets!=pipelines diff={sorted(sector_pipe_set ^ set(pipelines))}")
    if not view_pipe_set.issubset(set(pipelines)):
        join_bad.append(f"view_pipelines_not_in_sector={sorted(view_pipe_set - set(pipelines))}")
    rec("sleeve_join_key_consistent", "PASS" if not join_bad else "FAIL",
        f"sector_name==source_pipeline ({len(pipelines)} sleeves)" if not join_bad else f"{join_bad}")

    not_in_cov = [r["ticker"] for r in view_rows if r["ticker"] not in cov_tickers]
    rec("covariance_universe_alignment", "PASS" if not not_in_cov else "FAIL",
        f"all {len(view_rows)} view tickers in covariance ({len(cov_tickers)} cols)"
        if not not_in_cov else f"missing_from_cov={not_in_cov[:8]}")

    # feasibility: sector budgets sum, foreign within gross, per-name caps satisfy budgets
    max_w = _f(bl.get("max_weight_per_name")) or 0.05
    n_by_pipe: dict[str, int] = {p: 0 for p in pipelines}
    for r in view_rows:
        n_by_pipe[r["source_pipeline"]] = n_by_pipe.get(r["source_pipeline"], 0) + 1
    feas_bad = []
    if abs(sum(r["target_weight"] for r in sector_target_rows) - 1.0) > 1e-6:
        feas_bad.append(f"sector_sum={sum(r['target_weight'] for r in sector_target_rows):.10f}")
    if fmax > gross_exposure + 1e-9:
        feas_bad.append(f"foreign_max={fmax}>gross={gross_exposure}")
    for r in sector_target_rows:
        cap = n_by_pipe.get(r["sector_name"], 0) * max_w
        if r["target_weight"] * gross_exposure > cap + 1e-9:
            feas_bad.append(f"{r['sector_name']}:budget*gross={r['target_weight']*gross_exposure:.4f}>cap={cap:.4f}(n={n_by_pipe.get(r['sector_name'],0)})")
    rec("budget_feasibility", "PASS" if not feas_bad else "FAIL",
        f"budgets sum=1, foreign<=gross, caps satisfy budgets (gross={gross_exposure})" if not feas_bad else f"{feas_bad[:8]}")

    # ---- generated, sealed optimizer config (run-local sealed paths only) ----
    price_start, price_end = _price_panel_date_range(art["prices"])
    tier1_conf_by_rating = {
        "Strong Buy": _f((cfg_get(config, "black_litterman_fusion.confidence_by_rating", {}) or {}).get("strong_buy")) or 0.90,
        "Buy": _f((cfg_get(config, "black_litterman_fusion.confidence_by_rating", {}) or {}).get("buy")) or 0.70,
        "Hold": _f((cfg_get(config, "black_litterman_fusion.confidence_by_rating", {}) or {}).get("hold")) or 0.50,
        "Sell": _f((cfg_get(config, "black_litterman_fusion.confidence_by_rating", {}) or {}).get("reduce")) or 0.35,
        "Strong Sell": _f((cfg_get(config, "black_litterman_fusion.confidence_by_rating", {}) or {}).get("avoid")) or 0.20,
        "FOREIGN": _f((cfg_get(config, "black_litterman_fusion.confidence_by_rating", {}) or {}).get("foreign")) or 0.50,
    }
    cash_weight = round(max(0.0, 1.0 - gross_exposure), 8)
    us_min = round(max(0.0, gross_exposure - fmax), 8)
    us_max = round(max(0.0, gross_exposure - fmin), 8)
    sector_cap_band = _f(bl.get("macro_sector_cap_band")) or 0.03
    foreign_candidates = read_csv(art["foreign_etfs_optimizer"])
    max_foreign_etfs = len(foreign_candidates) if fmax > 0.0 else 0
    gen_config = {
        "_generated_by": "blacklitterman/23_build_bl_inputs.py",
        "_run_as_of": run_as_of,
        "_shadow_only": True,
        "output": {
            "out_dir": str(out_dir / "optimizer"),
            "write_weights_csvs": True,
            "bands_quantiles": [0.05, 0.95],
        },
        "paths": {
            "stocks_scores_csv": str(out["bl_stocks_scores_optimizer.csv"]),
            "sector_rotation_csv": str(art["sector_rotation_optimizer"]),
            "foreign_etfs_csv": str(art["foreign_etfs_optimizer"]),
            "covariance_csv": str(art["covariance"]),
        },
        "returns": {
            "source": "csv",
            "prices_csv": str(art["prices"]),
            "start": price_start,
            "end": min(price_end, run_as_of),
            "frequency": returns_frequency,
            "log_returns": True,
            "min_history_rows": 60,
            "max_nan_frac": 1.0,
            "drop_insufficient_history": False,
            "ffill_max_periods": 0,
            "winsorize": {"enabled": False},
        },
        "cash": {"annual_yield": 0.0},
        "universe": {
            "cash_symbol": "CASH",
            "include_all_stocks_long_only": True,
            "max_us_stocks_long_only": len(opt_stock_rows),
            "max_foreign_etfs": max_foreign_etfs,
            "allowed_foreign_states": ["Eligible"],
        },
        "optimization": {
            "solver": "ECOS",
            "risk_aversion": _f(bl.get("risk_aversion")) or 5.0,
            "hhi_penalty": 0.0,
            "turnover_penalty": 0.0,
            "long_only": {
                "max_weight_per_stock": max_w,
                "max_weight_per_foreign_etf": min(0.20, max(0.0, fmax)),
                "min_expected_return_mode": "none",
            },
            "long_short": {"enabled": False},
            "prune_reoptimize": {"enabled": False},
        },
        "risk": {
            "covariance_source": "stage2_covariance_csv",
            "covariance_csv": str(art["covariance"]),
            "covariance_units": "annualized",
            "robust_mode": "average",
            "shrinkage": "manual",
            "manual_shrink_delta": 0.20,
            "kendall_manual_shrink_delta": 0.20,
            "psd_eigen_floor": 1e-8,
            "max_cov_condition": _f(cfg_get(config, "risk_panel.max_condition_number", None)) or 1e8,
            "scenarios": {"shock": {"enabled": False}, "bootstrap": {"enabled": False}},
        },
        "diversification": {"use_cluster_caps": False},
        "black_litterman": {
            "tau": _f(bl.get("tau")) or 0.05,
            "delta": _f(bl.get("delta")) or 2.5,
            "return_space": str(bl.get("return_space", "excess")),
            "alpha_units_policy": str(bl.get("alpha_units_policy", "B_annualized_calibrated")),
            "alpha_input_mode": "absolute_annual",
            "alpha_column": "ExpectedAlphaAnnual",
            "confidence_by_rating": tier1_conf_by_rating,
            "min_confidence": _f(bl.get("min_confidence")) or 0.15,
            "max_confidence": _f(bl.get("max_confidence")) or 0.95,
            "score_confidence_boost": _f(bl.get("score_confidence_boost")) or 0.10,
            "use_score_confidence_in_omega": True,
            "use_sector_state_alpha_multiplier": True,
            "sector_state_alpha_multipliers": dict(cfg_get(config, "black_litterman_fusion.sector_state_alpha_multipliers", {}) or {}),
            "sector_alpha_scale_annual": _f(bl.get("sector_alpha_scale_annual")) or 0.03,
            "include_sector_in_alpha": True,
            "include_foreign_in_alpha": True,
            "benchmark_weight_source": "csv",
            "benchmark_weights_csv": str(out["bl_benchmark_weights.csv"]),
        },
        "regime": {"label": regime_label, "gross_scalar": regime_scalar, "base_gross_exposure": base_gross},
        "allocation": {
            "region_budgets": {
                "US": {"min": us_min, "max": us_max},
                "FOREIGN": {"min": fmin, "max": fmax},
                "CASH": {"min": cash_weight, "max": cash_weight},
            }
        },
        "sector": {"benchmark_sector_weights": {r["sector_name"]: r["target_weight"] for r in sector_target_rows},
                   "sector_cap_band": sector_cap_band,
                   "stock_to_sectorname": {p: p for p in pipelines}},
        "macro_optimizer_integration": {"enabled": False},
    }

    # ---- write artifacts ----
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out["bl_views.csv"], VIEW_FIELDS, view_rows)
    write_csv(out["bl_stocks_scores_optimizer.csv"], OPT_STOCK_FIELDS, opt_stock_rows)
    write_csv(out["bl_sector_targets_optimizer.csv"], SECTOR_TARGET_FIELDS, sector_target_rows)
    write_csv(out["bl_benchmark_weights.csv"], BENCHMARK_FIELDS, benchmark_rows)
    write_csv(out["bl_foreign_budget_optimizer.csv"], FOREIGN_BUDGET_FIELDS, foreign_rows)
    out["bl_optimizer_config.yaml"].write_text(yaml.safe_dump(gen_config, sort_keys=True), encoding="utf-8")

    # generated config must reference only run-local sealed files (no PROD, no MacroLayer DB, run-local)
    cfg_text = out["bl_optimizer_config.yaml"].read_text(encoding="utf-8")
    cfg_bad = []
    for bad_token in ("PROD_" + "Scalper_System", "macro_serving.sqlite", "MacroLayer"):
        if bad_token in cfg_text:
            cfg_bad.append(bad_token)
    for p in (str(art["covariance"]), str(art["prices"]), str(art["sector_rotation_optimizer"]), str(art["foreign_etfs_optimizer"]),
              str(out["bl_stocks_scores_optimizer.csv"]), str(out["bl_benchmark_weights.csv"]),
              str(out["bl_views.csv"]), str(out["bl_sector_targets_optimizer.csv"]),
              str(out["bl_foreign_budget_optimizer.csv"])):
        try:
            Path(p).resolve().relative_to(run_dir.resolve())
        except ValueError:
            cfg_bad.append(f"non_run_local:{p}")
        ensure_not_prod_path(Path(p), label="bl config path")
    rec("generated_config_run_local_sealed", "PASS" if not cfg_bad else "FAIL",
        "generated config references only run-local sealed files" if not cfg_bad else f"{cfg_bad[:8]}")

    # Tier1-native schema probe: this should fail before Stage 24 if the generated config cannot be read.
    required_paths = {"stocks_scores_csv", "sector_rotation_csv", "foreign_etfs_csv", "covariance_csv"}
    path_bad = sorted(required_paths - set((gen_config.get("paths") or {}).keys()))
    returns_bad = []
    if (gen_config.get("returns") or {}).get("source") != "csv":
        returns_bad.append("returns.source!=csv")
    if not (gen_config.get("returns") or {}).get("prices_csv"):
        returns_bad.append("returns.prices_csv_missing")
    opt_stock_required = {"Ticker", "sector", "Rating", "FinalScore"}
    opt_stock_bad = sorted(opt_stock_required - set(OPT_STOCK_FIELDS))
    bad_ratings = sorted({str(r["Rating"]) for r in opt_stock_rows} - set(TIER1_RATING_ORDER))
    bench_sum = sum(_f(r.get("Weight")) or 0.0 for r in benchmark_rows)
    contract_bad = path_bad + returns_bad + opt_stock_bad
    if bad_ratings:
        contract_bad.append(f"bad_ratings={bad_ratings}")
    if abs(bench_sum - 1.0) > 1e-6:
        contract_bad.append(f"benchmark_sum={bench_sum:.10f}")
    rec("tier1_optimizer_contract", "PASS" if not contract_bad else "FAIL",
        "tier1 paths/schema/ratings/benchmark weights are optimizer-native" if not contract_bad else f"{contract_bad[:8]}")

    risk_cfg = gen_config.get("risk") or {}
    cov_contract_bad = []
    if risk_cfg.get("covariance_source") != "stage2_covariance_csv":
        cov_contract_bad.append(f"covariance_source={risk_cfg.get('covariance_source')}")
    if str(risk_cfg.get("covariance_csv")) != str(art["covariance"]):
        cov_contract_bad.append("covariance_csv_not_stage2_artifact")
    if str(risk_cfg.get("covariance_units", "")).lower() != "annualized":
        cov_contract_bad.append(f"covariance_units={risk_cfg.get('covariance_units')}")
    cov_cond = _f(cov_meta.get("condition_number")) or float("inf")
    max_cond = _f(risk_cfg.get("max_cov_condition")) or 0.0
    if max_cond <= 0.0 or cov_cond > max_cond:
        cov_contract_bad.append(f"condition_number={cov_cond:.3e}>max={max_cond:.3e}")
    tier1_ppy = _tier1_periods_per_year(str((gen_config.get("returns") or {}).get("frequency", "")))
    if cov_annualization_factor is None:
        cov_contract_bad.append("covariance_meta.annualization_factor_missing_or_nonfinite")
    elif abs(cov_annualization_factor - float(tier1_ppy)) > 1e-9:
        cov_contract_bad.append(
            f"annualization_factor={cov_annualization_factor:g}!=tier1_ppy={tier1_ppy}"
        )
    if (gen_config.get("returns") or {}).get("drop_insufficient_history") is not False:
        cov_contract_bad.append("returns.drop_insufficient_history_not_false")
    if (gen_config.get("universe") or {}).get("include_all_stocks_long_only") is not True:
        cov_contract_bad.append("include_all_stocks_long_only_not_true")
    rec("stage2_covariance_injection_contract", "PASS" if not cov_contract_bad else "FAIL",
        f"uses sealed annualized Stage 2 covariance; freq={returns_frequency}; ppy={tier1_ppy}; "
        f"cond={cov_cond:.3e}; optimizer universe={len(opt_stock_rows)}"
        if not cov_contract_bad else f"{cov_contract_bad[:8]}")

    budgets = ((gen_config.get("allocation") or {}).get("region_budgets") or {})
    budget_bad = []
    for sleeve, expected in (("US", (us_min, us_max)), ("FOREIGN", (fmin, fmax)), ("CASH", (cash_weight, cash_weight))):
        band = budgets.get(sleeve) or {}
        mn = _f(band.get("min"))
        mx = _f(band.get("max"))
        if mn is None or mx is None or abs(mn - expected[0]) > 1e-9 or abs(mx - expected[1]) > 1e-9:
            budget_bad.append(f"{sleeve}:got=({mn},{mx}) expected={expected}")
    if abs((us_max + fmax + cash_weight) - 1.0) > 1e-8:
        budget_bad.append(f"budget_max_sum={us_max + fmax + cash_weight:.10f}")
    rec("regime_budget_contract", "PASS" if not budget_bad else "FAIL",
        f"gross={gross_exposure:.4f}; cash={cash_weight:.4f}; foreign=[{fmin:.4f},{fmax:.4f}]"
        if not budget_bad else f"{budget_bad[:8]}")

    probe_path.parent.mkdir(parents=True, exist_ok=True)
    write_csv(probe_path, ["check", "status", "detail"], checks)
    passed = all(c["status"] == "PASS" for c in checks)

    meta = {
        "run_as_of": run_as_of,
        "stage": "stage7_bl_inputs",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "shadow_only": True,
        "enabled_in_production": bool(cfg_get(config, "black_litterman_fusion.enabled_in_production", False)),
        "acceptance": "PASS" if passed else "FAIL",
        "universe_size": len(view_rows),
        "regime": {"label": regime_label, "gross_scalar": regime_scalar, "gross_exposure": gross_exposure},
        "foreign": {"active": active, "min_budget": fmin, "max_budget": fmax},
        "sector_baseline": {"source": baseline_source_label, "weights": base_by_pipe},
        "benchmark_within_sector_source": "stage3_weights" if within_source in {"stage3", "stage3_weights", "stage3_or_equal"} else "equal",
        "sector_targets": {r["sector_name"]: r["target_weight"] for r in sector_target_rows},
        "inputs_sha256": {"config.yaml": sha256_file(config_path)} | {k: sha256_file(p) for k, p in art.items()},
        "outputs_sha256": {name: sha256_file(p) for name, p in out.items()
                           if p.exists() and name != "bl_inputs_meta.json"},
        "source_sha256": {n: sha256_file(PACKAGE_ROOT / "blacklitterman" / n)
                          for n in SOURCE_FILES if (PACKAGE_ROOT / "blacklitterman" / n).exists()},
        "checks": checks,
    }
    write_manifest(out["bl_inputs_meta.json"], meta)

    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    if passed:
        LOGGER.info("STAGE 7 BL INPUTS: PASS (as_of=%s, universe=%d, gross=%.3f, regime=%s) -> %s",
                    run_as_of, len(view_rows), gross_exposure, regime_label, out["bl_inputs_meta.json"])
        return 0
    LOGGER.error("STAGE 7 BL INPUTS: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

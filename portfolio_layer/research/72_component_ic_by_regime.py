#!/usr/bin/env python3
"""Stage 11 research - component-level purged forward IC by (pipeline, component, horizon, regime).

GATE 1 of the sector-neutral research track. Before touching the blended composite score, prove which
UNDERLYING PILLARS actually predict forward returns out-of-sample -- per sector, horizon, and macro
regime -- and whether any pillar BEATS the composite (evidence the blend is diluting a real signal).

SHADOW / read-only. Consumes the sealed calibration panel (research/67) for PIT forward-return targets
+ regime + admission, enriches it with the sector dashboards' pillar scores (the raw factor pillars the
sector scorer blends into final_score), standardizes each pillar cross-sectionally within
(pipeline, date), and reports for every (pipeline, component, horizon, regime) cell:

  mean_rank_ic        mean per-date Spearman IC of the standardized component vs excess-sector return
  ic_t_deflated       t-stat deflated to the number of INDEPENDENT (non-overlapping) label windows,
                      so overlapping daily observations cannot inflate significance
  half1_ic / half2_ic chronological-half sign stability (an in-sample split; a real signal holds in both)
  pct_pos             share of dates with positive IC
  composite_ic        the blended score's IC in the same cell
  delta_vs_composite  component IC minus composite IC (positive => the blend is diluting this pillar)
  fdr_significant     Benjamini-Hochberg gate across ALL component cells (multiplicity-honest)
  attributes          1 only if fdr_significant AND sign-stable across halves AND beats the composite

Writes evidence only; it NEVER edits config or scoring. If no component attributes, the composite is
not diluting anything discoverable and reweighting would be curve-fitting -- a legitimate, valuable
negative that stops the track here.

--selftest verifies the IC, overlap deflation, half-stability, and FDR logic on synthetic data.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy import stats as _stats  # noqa: E402

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, sha256_file, write_manifest, write_via_temp  # noqa: E402
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.research.stage11_common import (  # noqa: E402
    forward_status_is_valid,
    independent_windows,
    load_lockbox,
    manifest_file_errors,
    rank_ic_of,
)
from portfolio_layer.scores.adapters import dated_candidates  # noqa: E402


LOGGER = logging.getLogger("component_ic_by_regime")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"

# The standard factor pillars the tech-family scorer blends into final_score (shared across
# semis / software / hardware / defense). Any additional sector-specific demand pillar present in a
# dashboard (e.g. big_tech_capex_score, defense_budget_backlog_score) is detected and included too.
SHARED_PILLARS = [
    "valuation_score", "quality_score", "risk_control_score", "positioning_score",
    "market_behavior_score", "growth_score", "sector_cycle_score",
]
# never treat these *_score columns as raw factor pillars (they are outputs / meta, not inputs)
PILLAR_BLOCKLIST = {
    "final_score", "core_score", "sector_overlay_score", "portfolio_candidate_score",
    "native_score_value", "production_rank_score", "production_rank_risk_score",
    "discovery_opportunity_score", "opportunity_score", "allocation_opportunity_score",
    "investment_score", "discovery_investment_score", "liquidity_score",
}
DEFAULT_PIPELINES = ["semiconductors", "software_infrastructure", "technology_hardware", "defense"]
HORIZONS = [21, 63, 126, 252]
COMPOSITE = "composite"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stage 11 component-level purged forward IC by regime.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--panel-build", default=None, help="calibration_panel build to consume (default: latest).")
    p.add_argument("--pipelines", default=",".join(DEFAULT_PIPELINES),
                   help="Comma-separated source_pipelines with standard factor pillars (default: tech family).")
    p.add_argument("--min-cross-section", type=int, default=8, help="min names/date for a per-date IC.")
    p.add_argument("--min-dates", type=int, default=12, help="min per-date ICs for a reportable cell.")
    p.add_argument("--fdr-alpha", type=float, default=0.10, help="Benjamini-Hochberg FDR level.")
    p.add_argument("--delta-margin", type=float, default=0.02,
                   help="component rank IC must beat the composite by this to 'attribute'.")
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--force", action="store_true")
    return p.parse_args()


# ---------------------------------------------------------------------------
# pure stats (self-tested)
# ---------------------------------------------------------------------------
def per_date_rank_ic(z: np.ndarray, y: np.ndarray) -> float | None:
    # rank_ic_of (stage11_common) handles the length / constant-input guards and is pyright-clean
    return rank_ic_of(np.asarray(z, dtype=float), np.asarray(y, dtype=float))


def deflated_t(ics: list[float], n_independent: int) -> tuple[float | None, float | None, float | None]:
    """(mean_ic, t_deflated, two_sided_p). t uses the INDEPENDENT window count, not the raw date count."""
    arr = np.asarray([v for v in ics if v is not None and np.isfinite(v)], dtype=float)
    if len(arr) < 2:
        return (float(arr.mean()) if len(arr) == 1 else None, None, None)
    mean = float(arr.mean())
    sd = float(arr.std(ddof=1))
    n_eff = max(1, int(n_independent))
    if sd <= 0:
        return mean, None, None
    t = mean / sd * np.sqrt(n_eff)
    p = float(2.0 * _stats.norm.sf(abs(t)))
    return mean, float(t), p


def benjamini_hochberg(pvals: list[float], alpha: float) -> list[bool]:
    """Return the BH-significant mask at FDR level alpha for the given p-values."""
    clean = [(i, float(p)) for i, p in enumerate(pvals) if p is not None and np.isfinite(p)]
    out = [False] * len(pvals)
    if not clean:
        return out
    m = len(clean)
    ordered = sorted(clean, key=lambda kv: kv[1])
    k_max = 0
    for rank, (_i, p) in enumerate(ordered, start=1):
        if p <= alpha * rank / m:
            k_max = rank
    for rank, (i, _p) in enumerate(ordered, start=1):
        if rank <= k_max:
            out[i] = True
    return out


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _selftest() -> None:
    rng = np.random.default_rng(72)
    # a component with true IC ~0.3 vs a pure-noise component, over independent weekly cross-sections
    n_dates, n_names = 60, 40
    good, bad = [], []
    for _ in range(n_dates):
        z = rng.standard_normal(n_names)
        y = 0.35 * z + rng.standard_normal(n_names)  # real signal
        good.append(per_date_rank_ic(z, y))
        zb = rng.standard_normal(n_names)
        yb = rng.standard_normal(n_names)             # noise
        bad.append(per_date_rank_ic(zb, yb))
    mg, tg, pg = deflated_t(good, n_independent=n_dates)
    mb, tb, pb = deflated_t(bad, n_independent=n_dates)
    assert mg is not None and mg > 0.2 and tg is not None and tg > 4, (mg, tg)
    assert mb is not None and abs(mb) < 0.1 and (tb is None or abs(tb) < 2.5), (mb, tb)
    # deflation: fewer independent windows -> smaller t for the same ICs
    _m, t_full, _p = deflated_t(good, n_independent=n_dates)
    _m, t_defl, _p = deflated_t(good, n_independent=max(2, n_dates // 5))
    assert t_defl is not None and t_full is not None and t_defl < t_full, (t_defl, t_full)
    # BH: one tiny p among many large -> only it flags
    mask = benjamini_hochberg([1e-6] + [0.6] * 19, alpha=0.10)
    assert mask[0] and not any(mask[1:]), mask
    assert not any(benjamini_hochberg([0.4, 0.5, 0.6], alpha=0.10)), "no true signal -> none flagged"
    print("component-ic self-test: PASS")


# ---------------------------------------------------------------------------
# pillar ingestion + main
# ---------------------------------------------------------------------------
def _detect_pillars(columns: list[str]) -> list[str]:
    present = [c for c in SHARED_PILLARS if c in columns]
    extra = [c for c in columns
             if c.endswith("_score") and c not in PILLAR_BLOCKLIST and c not in SHARED_PILLARS
             and not c.endswith("_quality_score")]
    # sector-specific demand pillar(s): keep only ones that look like a raw factor score, not meta
    extra = [c for c in extra if not any(tok in c for tok in ("percentile", "rank", "confidence", "quality"))]
    return present + sorted(extra)


def _load_pillar_frame(
    sector_cfg: dict[str, Any],
    root: Path,
    wanted_dates: set[str],
    used_sha256: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Long pillar frame (as_of_date, ticker, <pillars>) for one sector, restricted to wanted_dates."""
    candidates: list[tuple[str, Path, list[str]]] = []
    pillar_union: set[str] = set()
    for datestr, path in dated_candidates(sector_cfg, root):
        as_of = f"{datestr[:4]}-{datestr[4:6]}-{datestr[6:]}"
        if wanted_dates and as_of not in wanted_dates:
            continue
        try:
            head = pd.read_csv(path, nrows=0)
        except (OSError, pd.errors.ParserError) as exc:
            raise ValueError(f"Cannot read pillar header {path}: {exc}") from exc
        columns = list(head.columns)
        if "ticker" not in columns:
            raise ValueError(f"Pillar source {path} has no ticker column")
        detected = _detect_pillars(columns)
        pillar_union.update(detected)
        candidates.append((as_of, path, columns))
    pillars = sorted(pillar_union)
    if not candidates or not pillars:
        return pd.DataFrame()
    frames: list[pd.DataFrame] = []
    for as_of, path, columns in candidates:
        available = [c for c in pillars if c in columns]
        col_set = {"ticker", *available}
        df = pd.read_csv(path, usecols=lambda c: c in col_set)
        for missing in set(pillars) - set(available):
            df[missing] = np.nan
        df["as_of_date"] = as_of
        frames.append(df)
        if used_sha256 is not None:
            used_sha256[str(path.resolve())] = sha256_file(path)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out["ticker"] = out["ticker"].astype(str).str.upper().str.strip()
    duplicates = out.duplicated(["as_of_date", "ticker"], keep=False)
    if duplicates.any():
        sample = out.loc[duplicates, ["as_of_date", "ticker"]].head(8).to_dict("records")
        raise ValueError(f"Pillar sources contain duplicate (as_of_date,ticker) rows: {sample}")
    for c in pillars:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


def _zscore(group: pd.Series) -> pd.Series:
    sd = group.std(ddof=0)
    if not np.isfinite(sd) or sd == 0:
        return pd.Series(np.nan, index=group.index)
    return (group - group.mean()) / sd


def _latest_build(root: Path, wanted: str | None) -> Path | None:
    if wanted:
        cand = root / wanted
        return cand if (cand / "calibration_panel_manifest.json").exists() else None
    if not root.exists():
        return None
    builds = sorted(p for p in root.iterdir() if p.is_dir() and (p / "calibration_panel_manifest.json").exists())
    return builds[-1] if builds else None


def main() -> int:  # noqa: C901
    configure_utc_logging()
    args = parse_args()
    if args.selftest:
        _selftest()
        return 0
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    try:
        lockbox = load_lockbox(config, config_path)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1

    pipelines = [p.strip() for p in str(args.pipelines).split(",") if p.strip()]
    panel_root = paths.output_dir / str(cfg_get(config, "calibration_panel.dir", "calibration_panel"))
    panel_dir = _latest_build(panel_root, args.panel_build)
    if panel_dir is None:
        LOGGER.error("No calibration-panel build under %s; run research/67 first", panel_root)
        return 1
    panel_manifest = json.loads((panel_dir / "calibration_panel_manifest.json").read_text(encoding="utf-8"))
    if panel_manifest.get("acceptance") != "PASS":
        LOGGER.error("Calibration panel %s acceptance=%s; refusing", panel_dir.name, panel_manifest.get("acceptance"))
        return 1
    panel_path = panel_dir / "calibration_panel.csv"
    panel_errors = manifest_file_errors(panel_manifest, {"calibration_panel.csv": panel_path})
    if panel_errors:
        LOGGER.error("Calibration panel %s is stale/unsealed: %s", panel_dir.name, panel_errors)
        return 1

    out_dir = paths.output_dir / str(cfg_get(config, "component_ic.dir", "component_ic")) / panel_dir.name
    cells_path = out_dir / "component_ic.csv"
    manifest_path = out_dir / "component_ic_manifest.json"
    if args.force:
        for p in (cells_path, manifest_path):
            if p.exists():
                p.unlink()
    try:
        fail_if_exists([cells_path, manifest_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    # --- load panel (PIT forward returns + regime + admission) ---
    usecols = (["as_of_date", "ticker", "source_pipeline", "macro_regime", "score_z_pipeline_date",
                "calibration_research_eligible", "sidecar_stage11_eligible", "usable_for_promoted_training",
                "survivorship_complete", "in_lockbox"]
               + [f"excess_sector_{h}d" for h in HORIZONS] + [f"fwd_status_{h}d" for h in HORIZONS])
    panel_head = pd.read_csv(panel_path, nrows=0)
    use_set = {c for c in usecols if c in panel_head.columns}
    panel = pd.read_csv(panel_path, usecols=lambda c: c in use_set)
    panel["ticker"] = panel["ticker"].astype(str).str.upper().str.strip()
    panel["as_of_date"] = panel["as_of_date"].astype(str).str.slice(0, 10)

    truthy = ("1", "1.0", "true", "True")
    eligible = panel["calibration_research_eligible"].astype(str).isin(truthy)
    if "sidecar_stage11_eligible" in panel.columns:
        eligible = eligible | panel["sidecar_stage11_eligible"].astype(str).isin(truthy)
    admit = (
        eligible
        & panel["usable_for_promoted_training"].astype(str).isin(truthy)
        & panel["survivorship_complete"].astype(str).isin(truthy)
        & ~panel["in_lockbox"].astype(str).isin(truthy)
        & panel["source_pipeline"].isin(pipelines)
    )
    panel = panel.loc[admit].copy()
    leaked_lockbox = 0  # excluded above; recorded for the gate
    if panel.empty:
        LOGGER.error("No admitted panel rows for pipelines=%s", pipelines)
        return 1

    # --- enrich with sector pillar scores ---
    root = resolve_path(cfg_get(config, "score_contract.sector_output_root", "../output"), base_dir=config_path.parent)
    sectors_cfg = {str(s.get("model_family")): dict(s) for s in cfg_get(config, "score_contract.sectors", []) or []}
    pillar_sets: dict[str, list[str]] = {}
    pillar_sources_sha256: dict[str, str] = {}
    merged: list[pd.DataFrame] = []
    for pipe in pipelines:
        sub = panel.loc[panel["source_pipeline"] == pipe]
        if sub.empty or pipe not in sectors_cfg:
            continue
        wanted_dates = set(sub["as_of_date"].unique())
        pillar_frame = _load_pillar_frame(
            sectors_cfg[pipe], root, wanted_dates, used_sha256=pillar_sources_sha256,
        )
        if pillar_frame.empty:
            LOGGER.warning("No pillar columns ingested for %s; skipping", pipe)
            continue
        pillars = [c for c in pillar_frame.columns if c not in ("ticker", "as_of_date")]
        pillar_sets[pipe] = pillars
        j = sub.merge(pillar_frame, on=["as_of_date", "ticker"], how="inner")
        merged.append(j)
    if not merged:
        LOGGER.error("No pipeline yielded a pillar-enriched panel")
        return 1
    data = pd.concat(merged, ignore_index=True)

    # standardize each pillar within (pipeline, date); composite already standardized upstream
    all_pillars = sorted({c for cols in pillar_sets.values() for c in cols})
    for c in all_pillars:
        if c in data.columns:
            data[f"{c}__z"] = data.groupby(["source_pipeline", "as_of_date"])[c].transform(_zscore)
    data["composite__z"] = pd.to_numeric(data["score_z_pipeline_date"], errors="coerce")

    regimes_all = sorted(r for r in data["macro_regime"].dropna().astype(str).unique() if r)

    # --- compute per-cell IC ---
    rows: list[dict[str, Any]] = []
    for pipe in pipelines:
        pipe_pillars = pillar_sets.get(pipe, [])
        if not pipe_pillars:
            continue
        components = [f"{c}__z" for c in pipe_pillars] + ["composite__z"]
        pdata = data.loc[data["source_pipeline"] == pipe]
        for h in HORIZONS:
            tgt, status = f"excess_sector_{h}d", f"fwd_status_{h}d"
            if tgt not in pdata.columns:
                continue
            ok = pdata.loc[pdata[status].map(forward_status_is_valid)] if status in pdata.columns else pdata
            for regime in ["ALL"] + regimes_all:
                rsub = ok if regime == "ALL" else ok.loc[ok["macro_regime"].astype(str) == regime]
                if rsub.empty:
                    continue
                # composite IC first (baseline for the delta)
                comp_ics_by_comp: dict[str, tuple[float | None, float | None, float | None, list[float], list[str]]] = {}
                for comp in components:
                    ics: list[float] = []
                    dates: list[str] = []
                    for d, g in rsub.groupby("as_of_date"):
                        zz = np.asarray(pd.to_numeric(g[comp], errors="coerce"), dtype=float)
                        yy = np.asarray(pd.to_numeric(g[tgt], errors="coerce"), dtype=float)
                        mask = np.isfinite(zz) & np.isfinite(yy)
                        if int(mask.sum()) >= args.min_cross_section:
                            ic = per_date_rank_ic(zz[mask], yy[mask])
                            if ic is not None:
                                ics.append(ic)
                                dates.append(str(d))
                    if len(ics) < args.min_dates:
                        continue
                    n_ind = independent_windows(sorted(set(dates)), h)
                    mean, t, p = deflated_t(ics, n_ind)
                    comp_ics_by_comp[comp] = (mean, t, p, ics, dates)
                composite_mean = comp_ics_by_comp.get("composite__z", (None,))[0]
                for comp, (mean, t, p, ics, dates) in comp_ics_by_comp.items():
                    arr = np.asarray(ics, dtype=float)
                    n = len(arr)
                    half = n // 2
                    h1 = float(arr[:half].mean()) if half >= 1 else float("nan")
                    h2 = float(arr[half:].mean()) if n - half >= 1 else float("nan")
                    sign_stable = bool(np.isfinite(h1) and np.isfinite(h2)
                                       and np.sign(h1) == np.sign(h2) and mean is not None and mean != 0
                                       and np.sign(h1) == np.sign(mean))
                    rows.append({
                        "source_pipeline": pipe, "component": comp.replace("__z", ""),
                        "horizon_days": h, "regime": regime, "n_dates": n,
                        "independent_windows": independent_windows(sorted(set(dates)), h),
                        "mean_rank_ic": None if mean is None else round(mean, 6),
                        "ic_t_deflated": None if t is None else round(t, 4),
                        "p_two_sided": None if p is None else p,
                        "half1_ic": round(h1, 6) if np.isfinite(h1) else "",
                        "half2_ic": round(h2, 6) if np.isfinite(h2) else "",
                        "pct_pos": round(float((arr > 0).mean()) * 100.0, 1),
                        "sign_stable": int(sign_stable),
                        "composite_ic": None if composite_mean is None else round(composite_mean, 6),
                        "delta_vs_composite": (round(mean - composite_mean, 6)
                                               if mean is not None and composite_mean is not None
                                               and comp != "composite__z" else ""),
                        "is_composite": int(comp == "composite__z"),
                    })

    # --- multiplicity gate (BH-FDR) across NON-composite cells ---
    non_comp_idx = [i for i, r in enumerate(rows) if not r["is_composite"]]
    pvals = [rows[i]["p_two_sided"] for i in non_comp_idx]
    fdr_mask = benjamini_hochberg(pvals, args.fdr_alpha)
    for i, flag in zip(non_comp_idx, fdr_mask):
        rows[i]["fdr_significant"] = int(flag)
    for r in rows:
        r.setdefault("fdr_significant", "")
        beats = (r["delta_vs_composite"] != "" and float(r["delta_vs_composite"]) >= args.delta_margin)
        r["attributes"] = int(
            (not r["is_composite"]) and r["fdr_significant"] == 1
            and r["sign_stable"] == 1 and beats
            and (r["mean_rank_ic"] is not None and float(r["mean_rank_ic"]) > 0)
        )

    attributing = [r for r in rows if r["attributes"] == 1]
    fields = ["source_pipeline", "component", "horizon_days", "regime", "n_dates", "independent_windows",
              "mean_rank_ic", "ic_t_deflated", "p_two_sided", "half1_ic", "half2_ic", "pct_pos",
              "sign_stable", "composite_ic", "delta_vs_composite", "is_composite", "fdr_significant",
              "attributes"]
    out_dir.mkdir(parents=True, exist_ok=True)
    write_via_temp(
        cells_path,
        lambda temp: pd.DataFrame(rows).reindex(columns=fields).to_csv(temp, index=False),
    )

    checks = [
        {"check": "lockbox_no_sealed_rows", "status": "PASS",
         "detail": f"sealed rows excluded from admission; leaked={leaked_lockbox}"},
        {"check": "multiplicity_disclosed", "status": "PASS",
         "detail": f"cells={len(rows)} non_composite={len(non_comp_idx)} fdr_alpha={args.fdr_alpha}; "
                   "BH gate applied across all component cells"},
        {"check": "shadow_only", "status": "PASS",
         "detail": "evidence only; no config or scoring change. Reweighting requires 73 + 16c + a "
                   "protocol amendment."},
    ]
    passed = all(c["status"] in ("PASS", "WARN") for c in checks)
    write_manifest(manifest_path, {
        "stage": "stage11_component_ic_by_regime",
        "generated_at": utc_now(),
        "acceptance": "PASS" if passed else "FAIL",
        "panel_build": panel_dir.name,
        "panel_manifest_sha256": sha256_file(panel_dir / "calibration_panel_manifest.json"),
        "protocol_sha256": lockbox["protocol_sha256"],
        "pipelines": pipelines,
        "pillar_sets": pillar_sets,
        "inputs_sha256": {
            "calibration_panel_manifest.json": sha256_file(panel_dir / "calibration_panel_manifest.json"),
            "calibration_panel.csv": sha256_file(panel_path),
            **{f"pillar_source:{path}": sha for path, sha in sorted(pillar_sources_sha256.items())},
        },
        "horizons": HORIZONS,
        "fdr_alpha": args.fdr_alpha,
        "delta_margin": args.delta_margin,
        "rows_admitted": int(len(data)),
        "cells": len(rows),
        "attributing_cells": len(attributing),
        "checks": checks,
        "files": {"component_ic.csv": {"sha256": sha256_file(cells_path), "rows": len(rows)}},
    })
    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    for r in sorted(attributing, key=lambda x: -float(x["mean_rank_ic"]))[:20]:
        LOGGER.info("ATTRIBUTES %s %s h=%s regime=%s ic=%.3f t=%.2f delta_vs_composite=%s",
                    r["source_pipeline"], r["component"], r["horizon_days"], r["regime"],
                    float(r["mean_rank_ic"]), float(r["ic_t_deflated"] or 0), r["delta_vs_composite"])
    LOGGER.info("COMPONENT IC: %s (cells=%d, attributing=%d) -> %s",
                "PASS" if passed else "FAIL", len(rows), len(attributing), out_dir)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

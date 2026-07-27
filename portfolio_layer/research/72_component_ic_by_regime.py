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
import re
import sys
from collections.abc import Sequence
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
    calibration_admission_mask,
    forward_status_is_valid,
    independent_windows,
    load_lockbox,
    manifest_file_errors,
    manifest_input_errors,
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
DEFAULT_PIPELINES = [
    "semiconductors",
    "software_infrastructure",
    "technology_hardware",
    "biotech",
    "med_devices",
    "defense",
]
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
    n_eff = int(n_independent)
    if sd <= 0 or n_eff < 2:
        return mean, None, None
    t = mean / sd * np.sqrt(n_eff)
    # The effective sample is the non-overlapping-window count, often small.
    # Normal tails materially overstate significance in that setting.
    p = float(2.0 * _stats.t.sf(abs(t), df=n_eff - 1))
    return mean, float(t), p


def benjamini_hochberg(pvals: Sequence[float | None], alpha: float) -> list[bool]:
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
    requested_pillars: list[str] | None = None,
) -> pd.DataFrame:
    """Long pillar frame, using a frozen list when supplied or heuristic discovery otherwise."""
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
        detected = (
            [pillar for pillar in requested_pillars if pillar in columns]
            if requested_pillars
            else _detect_pillars(columns)
        )
        pillar_union.update(detected)
        candidates.append((as_of, path, columns))
    consolidated = _load_consolidated_pillar_frame(
        candidates=candidates,
        wanted_dates=wanted_dates,
        requested_pillars=requested_pillars,
        used_sha256=used_sha256,
    )
    if not consolidated.empty:
        return consolidated
    pillars = sorted(pillar_union)
    if not candidates or not pillars:
        return pd.DataFrame()
    if requested_pillars:
        missing_requested = sorted(set(requested_pillars) - set(pillars))
        if missing_requested:
            raise ValueError(
                f"Configured pillars are absent from every source for "
                f"{sector_cfg.get('model_family')}: {missing_requested}"
            )
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


def _load_consolidated_pillar_frame(
    *,
    candidates: list[tuple[str, Path, list[str]]],
    wanted_dates: set[str],
    requested_pillars: list[str] | None,
    used_sha256: dict[str, str] | None,
) -> pd.DataFrame:
    """Load technology Stage 11 sidecars, including consolidated range chunks.

    Source precedence matches research/67: root panel, ascending range chunks,
    then per-date sidecars. Later rows replace earlier rows for the same
    (as_of_date, ticker).
    """
    if not candidates or not requested_pillars:
        return pd.DataFrame()
    rank_path = candidates[0][1]
    suffix = "_final_rank_table.csv"
    if not rank_path.name.endswith(suffix):
        return pd.DataFrame()
    prefix = rank_path.name.removesuffix(suffix)
    dashboard_root = rank_path.parent.parent
    sidecar_name = f"{prefix}_stage11_survivorship_calibration_panel.csv"
    source_paths: list[Path] = []
    root_panel = dashboard_root / sidecar_name
    if root_panel.exists():
        source_paths.append(root_panel)
    chunk_dir = dashboard_root / "stage11_combined"
    if chunk_dir.exists():
        range_re = re.compile(
            r"_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.csv$"
        )
        chunks: list[tuple[tuple[str, str], Path]] = []
        for path in chunk_dir.glob(
            f"{prefix}_stage11_survivorship_calibration_panel_*.csv"
        ):
            match = range_re.search(path.name)
            if match:
                chunks.append(((match.group(2), match.group(1)), path))
        source_paths.extend(path for _key, path in sorted(chunks))
    source_paths.extend(
        path.parent / sidecar_name
        for _as_of, path, _columns in candidates
        if (path.parent / sidecar_name).exists()
    )
    if not source_paths:
        return pd.DataFrame()

    frames: list[pd.DataFrame] = []
    for order, path in enumerate(dict.fromkeys(source_paths)):
        header = pd.read_csv(path, nrows=0)
        date_column = (
            "asof_date"
            if "asof_date" in header.columns
            else "as_of_date"
            if "as_of_date" in header.columns
            else ""
        )
        if "ticker" not in header.columns or not date_column:
            raise ValueError(
                f"Stage 11 pillar source lacks ticker/as-of columns: {path}"
            )
        available = [
            pillar for pillar in requested_pillars if pillar in header.columns
        ]
        if not available:
            continue
        selected = {"ticker", date_column, *available}
        frame = pd.read_csv(
            path,
            usecols=lambda column: column in selected,
        )
        frame.rename(columns={date_column: "as_of_date"}, inplace=True)
        frame["as_of_date"] = (
            frame["as_of_date"].astype(str).str.slice(0, 10)
        )
        frame = frame.loc[
            frame["as_of_date"].isin(sorted(wanted_dates))
        ].copy()
        if frame.empty:
            continue
        if frame.duplicated(["as_of_date", "ticker"]).any():
            raise ValueError(
                f"Duplicate (as_of_date,ticker) rows within {path}"
            )
        for missing in set(requested_pillars) - set(available):
            frame[missing] = np.nan
        frame["_source_order"] = order
        frames.append(
            frame[["ticker", "as_of_date", *requested_pillars, "_source_order"]]
        )
        if used_sha256 is not None:
            used_sha256[str(path.resolve())] = sha256_file(path)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out["ticker"] = out["ticker"].astype(str).str.upper().str.strip()
    out.sort_values("_source_order", inplace=True)
    out.drop_duplicates(["as_of_date", "ticker"], keep="last", inplace=True)
    out.drop(columns=["_source_order"], inplace=True)
    for pillar in requested_pillars:
        out[pillar] = pd.to_numeric(out[pillar], errors="coerce")
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
    panel_input_errors = manifest_input_errors(
        panel_manifest,
        {
            "config.yaml": config_path,
            "research/67_join_calibration_panel.py": (
                PACKAGE_ROOT / "research" / "67_join_calibration_panel.py"
            ),
            "research/stage11_common.py": (
                PACKAGE_ROOT / "research" / "stage11_common.py"
            ),
        },
    )
    if panel_errors or panel_input_errors:
        LOGGER.error(
            "Calibration panel %s is stale/unsealed: files=%s inputs=%s",
            panel_dir.name,
            panel_errors,
            panel_input_errors,
        )
        return 1

    out_dir = paths.output_dir / str(cfg_get(config, "component_ic.dir", "component_ic")) / panel_dir.name
    cells_path = out_dir / "component_ic.csv"
    coverage_path = out_dir / "component_coverage.csv"
    usable_coverage_path = out_dir / "component_usable_coverage.csv"
    manifest_path = out_dir / "component_ic_manifest.json"
    if args.force:
        for p in (cells_path, coverage_path, usable_coverage_path, manifest_path):
            if p.exists():
                p.unlink()
    try:
        fail_if_exists(
            [cells_path, coverage_path, usable_coverage_path, manifest_path],
            force=args.force,
        )
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

    lockbox_series = (
        panel["in_lockbox"]
        if "in_lockbox" in panel.columns
        else pd.Series("", index=panel.index)
    )
    lockbox_rows = lockbox_series.astype(str).str.strip().str.lower().isin(
        ("1", "1.0", "true", "yes")
    )
    admit = calibration_admission_mask(panel) & panel["source_pipeline"].isin(pipelines)
    admitted_lockbox_rows = int((admit & lockbox_rows).sum())
    panel = panel.loc[admit].copy()
    if panel.empty:
        LOGGER.error("No admitted panel rows for pipelines=%s", pipelines)
        return 1

    # --- enrich with sector pillar scores ---
    root = resolve_path(cfg_get(config, "score_contract.sector_output_root", "../output"), base_dir=config_path.parent)
    sectors_cfg = {str(s.get("model_family")): dict(s) for s in cfg_get(config, "score_contract.sectors", []) or []}
    pillar_sets: dict[str, list[str]] = {}
    pillar_sources_sha256: dict[str, str] = {}
    coverage_rows: list[dict[str, Any]] = []
    usable_coverage_rows: list[dict[str, Any]] = []
    join_fractions: dict[str, float] = {}
    min_component_coverage = float(
        cfg_get(config, "component_ic.min_component_coverage_fraction", 0.50)
    )
    merged: list[pd.DataFrame] = []
    for pipe in pipelines:
        sub = panel.loc[panel["source_pipeline"] == pipe]
        if sub.empty or pipe not in sectors_cfg:
            continue
        wanted_dates = set(sub["as_of_date"].unique())
        configured = cfg_get(config, f"component_ic.pillars_by_pipeline.{pipe}", []) or []
        pillar_frame = _load_pillar_frame(
            sectors_cfg[pipe],
            root,
            wanted_dates,
            used_sha256=pillar_sources_sha256,
            requested_pillars=[str(value) for value in configured] if configured else None,
        )
        if pillar_frame.empty:
            LOGGER.warning("No pillar columns ingested for %s; skipping", pipe)
            continue
        pillars = [c for c in pillar_frame.columns if c not in ("ticker", "as_of_date")]
        pillar_sets[pipe] = pillars
        j = sub.merge(
            pillar_frame,
            on=["as_of_date", "ticker"],
            how="left",
            validate="one_to_one",
        )
        joined = j[pillars].notna().any(axis=1)
        join_fractions[pipe] = float(joined.mean()) if len(j) else 0.0
        for pillar in pillars:
            present = j[pillar].notna()
            coverage_rows.append(
                {
                    "source_pipeline": pipe,
                    "component": pillar,
                    "panel_rows": len(j),
                    "nonmissing_rows": int(present.sum()),
                    "nonmissing_fraction": round(float(present.mean()), 6),
                    "dates_with_values": int(j.loc[present, "as_of_date"].nunique()),
                    "coverage_eligible": int(float(present.mean()) >= min_component_coverage),
                }
            )
        for (as_of, regime), cross_section in j.groupby(
            ["as_of_date", "macro_regime"],
            dropna=False,
            sort=True,
        ):
            numeric = cross_section[pillars].apply(pd.to_numeric, errors="coerce")
            finite = np.isfinite(numeric.to_numpy(dtype=float))
            complete_case = finite.all(axis=1) if len(pillars) else np.zeros(len(numeric), dtype=bool)
            nonconstant = 0
            for pillar in pillars:
                values = numeric[pillar].to_numpy(dtype=float)
                values = values[np.isfinite(values)]
                if len(values) >= 2 and float(np.std(values, ddof=0)) > 0.0:
                    nonconstant += 1
            usable_coverage_rows.append(
                {
                    "source_pipeline": pipe,
                    "as_of_date": str(as_of),
                    "macro_regime": "" if pd.isna(regime) else str(regime),
                    "panel_rows": len(cross_section),
                    "configured_pillars": len(pillars),
                    "nonconstant_pillars": nonconstant,
                    "complete_case_rows": int(complete_case.sum()),
                    "complete_case_fraction": round(
                        float(complete_case.mean()) if len(complete_case) else 0.0,
                        6,
                    ),
                    "all_pillars_nonconstant": int(nonconstant == len(pillars)),
                }
            )
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
    min_independent_windows = int(
        cfg_get(config, "component_ic.min_independent_windows", 3)
    )
    max_entry_lag = int(
        cfg_get(config, "calibration_targets.max_entry_lag_trading_days", 5)
    )
    component_coverage = {
        (str(row["source_pipeline"]), str(row["component"])): bool(
            row["coverage_eligible"]
        )
        for row in coverage_rows
    }

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
                comp_ics_by_comp: dict[
                    str,
                    tuple[
                        float | None,
                        float | None,
                        float | None,
                        list[float],
                        list[str],
                        dict[str, float],
                    ],
                ] = {}
                for comp in components:
                    ics: list[float] = []
                    dates: list[str] = []
                    ic_by_date: dict[str, float] = {}
                    for d, g in rsub.groupby("as_of_date"):
                        zz = np.asarray(pd.to_numeric(g[comp], errors="coerce"), dtype=float)
                        yy = np.asarray(pd.to_numeric(g[tgt], errors="coerce"), dtype=float)
                        mask = np.isfinite(zz) & np.isfinite(yy)
                        if int(mask.sum()) >= args.min_cross_section:
                            ic = per_date_rank_ic(zz[mask], yy[mask])
                            if ic is not None:
                                ics.append(ic)
                                dates.append(str(d))
                                ic_by_date[str(d)] = ic
                    if len(ics) < args.min_dates:
                        continue
                    n_ind = independent_windows(
                        sorted(set(dates)),
                        h,
                        entry_lag_trading_days=max_entry_lag,
                    )
                    mean, t, p = deflated_t(ics, n_ind)
                    comp_ics_by_comp[comp] = (mean, t, p, ics, dates, ic_by_date)
                composite_mean = comp_ics_by_comp.get("composite__z", (None,))[0]
                composite_by_date = (
                    comp_ics_by_comp.get("composite__z", (None, None, None, [], [], {}))[5]
                )
                for comp, (mean, t, p, ics, dates, ic_by_date) in comp_ics_by_comp.items():
                    arr = np.asarray(ics, dtype=float)
                    n = len(arr)
                    half = n // 2
                    h1 = float(arr[:half].mean()) if half >= 1 else float("nan")
                    h2 = float(arr[half:].mean()) if n - half >= 1 else float("nan")
                    sign_stable = bool(np.isfinite(h1) and np.isfinite(h2)
                                       and np.sign(h1) == np.sign(h2) and mean is not None and mean != 0
                                       and np.sign(h1) == np.sign(mean))
                    paired_dates = sorted(set(ic_by_date) & set(composite_by_date))
                    deltas = [
                        ic_by_date[d] - composite_by_date[d]
                        for d in paired_dates
                    ]
                    delta_n_ind = independent_windows(
                        paired_dates,
                        h,
                        entry_lag_trading_days=max_entry_lag,
                    )
                    delta_mean, delta_t, delta_p_two_sided = deflated_t(
                        deltas,
                        delta_n_ind,
                    )
                    if delta_p_two_sided is None or delta_t is None:
                        delta_p_one_sided = None
                    elif delta_t > 0:
                        delta_p_one_sided = delta_p_two_sided / 2.0
                    else:
                        delta_p_one_sided = 1.0 - delta_p_two_sided / 2.0
                    rows.append({
                        "source_pipeline": pipe, "component": comp.replace("__z", ""),
                        "horizon_days": h, "regime": regime, "n_dates": n,
                        "independent_windows": independent_windows(
                            sorted(set(dates)),
                            h,
                            entry_lag_trading_days=max_entry_lag,
                        ),
                        "enough_independent_windows": int(
                            independent_windows(
                                sorted(set(dates)),
                                h,
                                entry_lag_trading_days=max_entry_lag,
                            )
                            >= min_independent_windows
                        ),
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
                        "delta_paired_dates": len(paired_dates) if comp != "composite__z" else "",
                        "delta_independent_windows": (
                            delta_n_ind if comp != "composite__z" else ""
                        ),
                        "delta_t_deflated": (
                            round(delta_t, 4)
                            if comp != "composite__z" and delta_t is not None
                            else ""
                        ),
                        "delta_p_one_sided": (
                            delta_p_one_sided
                            if comp != "composite__z" and delta_p_one_sided is not None
                            else ""
                        ),
                        "is_composite": int(comp == "composite__z"),
                    })

    # --- multiplicity gate (BH-FDR) across NON-composite cells ---
    non_comp_idx = [i for i, r in enumerate(rows) if not r["is_composite"]]
    pvals = [rows[i]["p_two_sided"] for i in non_comp_idx]
    fdr_mask = benjamini_hochberg(pvals, args.fdr_alpha)
    for i, flag in zip(non_comp_idx, fdr_mask):
        rows[i]["fdr_significant"] = int(flag)
    delta_pvals = [
        rows[i]["delta_p_one_sided"]
        if rows[i]["delta_p_one_sided"] != ""
        else None
        for i in non_comp_idx
    ]
    delta_fdr_mask = benjamini_hochberg(delta_pvals, args.fdr_alpha)
    for i, flag in zip(non_comp_idx, delta_fdr_mask):
        rows[i]["delta_fdr_significant"] = int(flag)
    for r in rows:
        r.setdefault("fdr_significant", "")
        r.setdefault("delta_fdr_significant", "")
        beats = (r["delta_vs_composite"] != "" and float(r["delta_vs_composite"]) >= args.delta_margin)
        r["attributes"] = int(
            (not r["is_composite"]) and r["fdr_significant"] == 1
            and r["delta_fdr_significant"] == 1
            and r["sign_stable"] == 1 and beats
            and (r["mean_rank_ic"] is not None and float(r["mean_rank_ic"]) > 0)
            and r["enough_independent_windows"] == 1
            and component_coverage.get(
                (str(r["source_pipeline"]), str(r["component"])), False
            )
        )

    attributing = [r for r in rows if r["attributes"] == 1]
    fields = ["source_pipeline", "component", "horizon_days", "regime", "n_dates", "independent_windows",
              "enough_independent_windows",
              "mean_rank_ic", "ic_t_deflated", "p_two_sided", "half1_ic", "half2_ic", "pct_pos",
              "sign_stable", "composite_ic", "delta_vs_composite", "delta_paired_dates",
              "delta_independent_windows", "delta_t_deflated", "delta_p_one_sided",
              "is_composite", "fdr_significant", "delta_fdr_significant",
              "attributes"]
    out_dir.mkdir(parents=True, exist_ok=True)
    write_via_temp(
        cells_path,
        lambda temp: pd.DataFrame(rows).reindex(columns=fields).to_csv(temp, index=False),
    )
    coverage_fields = [
        "source_pipeline",
        "component",
        "panel_rows",
        "nonmissing_rows",
        "nonmissing_fraction",
        "dates_with_values",
        "coverage_eligible",
    ]
    write_via_temp(
        coverage_path,
        lambda temp: pd.DataFrame(coverage_rows)
        .reindex(columns=coverage_fields)
        .to_csv(temp, index=False),
    )
    usable_coverage_fields = [
        "source_pipeline",
        "as_of_date",
        "macro_regime",
        "panel_rows",
        "configured_pillars",
        "nonconstant_pillars",
        "complete_case_rows",
        "complete_case_fraction",
        "all_pillars_nonconstant",
    ]
    write_via_temp(
        usable_coverage_path,
        lambda temp: pd.DataFrame(usable_coverage_rows)
        .reindex(columns=usable_coverage_fields)
        .to_csv(temp, index=False),
    )

    min_join_fraction = float(
        cfg_get(config, "component_ic.min_pillar_join_fraction", 0.90)
    )
    bad_joins = {
        pipe: fraction
        for pipe, fraction in join_fractions.items()
        if fraction < min_join_fraction
    }
    min_complete_case_fraction = float(
        cfg_get(config, "component_ic.min_complete_case_fraction", 0.90)
    )
    usable_by_pipeline: dict[str, dict[str, float]] = {}
    for pipe in pipelines:
        rows_for_pipe = [
            row for row in usable_coverage_rows if row["source_pipeline"] == pipe
        ]
        if not rows_for_pipe:
            usable_by_pipeline[pipe] = {
                "dates": 0,
                "complete_case_date_fraction": 0.0,
                "all_nonconstant_date_fraction": 0.0,
            }
            continue
        usable_by_pipeline[pipe] = {
            "dates": len(rows_for_pipe),
            "complete_case_date_fraction": round(
                sum(
                    float(row["complete_case_fraction"]) >= min_complete_case_fraction
                    for row in rows_for_pipe
                )
                / len(rows_for_pipe),
                6,
            ),
            "all_nonconstant_date_fraction": round(
                sum(int(row["all_pillars_nonconstant"]) == 1 for row in rows_for_pipe)
                / len(rows_for_pipe),
                6,
            ),
        }
    degraded_usable = {
        pipe: values
        for pipe, values in usable_by_pipeline.items()
        if values["complete_case_date_fraction"] < min_complete_case_fraction
        or values["all_nonconstant_date_fraction"] < min_complete_case_fraction
    }
    checks = [
        {
            "check": "lockbox_no_sealed_rows",
            "status": "PASS" if admitted_lockbox_rows == 0 else "FAIL",
            "detail": (
                f"admitted_lockbox_rows={admitted_lockbox_rows}; "
                f"panel_lockbox_rows={int(lockbox_rows.sum())}"
            ),
        },
        {"check": "multiplicity_disclosed", "status": "PASS",
         "detail": f"cells={len(rows)} non_composite={len(non_comp_idx)} fdr_alpha={args.fdr_alpha}; "
                   "BH gates applied to component IC and paired component-minus-composite deltas"},
        {
            "check": "pillar_join_coverage",
            "status": "PASS" if not bad_joins else "FAIL",
            "detail": (
                f"minimum={min_join_fraction:.3f}; fractions={join_fractions}"
                if not bad_joins
                else f"below minimum={min_join_fraction:.3f}: {bad_joins}"
            ),
        },
        {
            "check": "usable_pillar_coverage",
            "status": "PASS" if not degraded_usable else "WARN",
            "detail": (
                f"minimum_date_fraction={min_complete_case_fraction:.3f}; "
                f"by_pipeline={usable_by_pipeline}"
            ),
        },
        {
            "check": "independent_window_floor",
            "status": "PASS",
            "detail": (
                f"attribution requires >= {min_independent_windows} "
                "non-overlapping forward windows"
            ),
        },
        {"check": "shadow_only", "status": "PASS",
         "detail": "evidence only; no config or scoring change. Reweighting requires the "
                   "pre-registered 74/75 campaign, economic conversion, and a protocol amendment."},
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
            "config.yaml": sha256_file(config_path),
            "research/72_component_ic_by_regime.py": sha256_file(Path(__file__).resolve()),
            "research/stage11_common.py": sha256_file(
                Path(__file__).with_name("stage11_common.py")
            ),
            "calibration_panel_manifest.json": sha256_file(panel_dir / "calibration_panel_manifest.json"),
            "calibration_panel.csv": sha256_file(panel_path),
            **{f"pillar_source:{path}": sha for path, sha in sorted(pillar_sources_sha256.items())},
        },
        "horizons": HORIZONS,
        "fdr_alpha": args.fdr_alpha,
        "delta_margin": args.delta_margin,
        "min_independent_windows": min_independent_windows,
        "max_entry_lag_trading_days": max_entry_lag,
        "min_component_coverage_fraction": min_component_coverage,
        "min_pillar_join_fraction": min_join_fraction,
        "min_complete_case_fraction": min_complete_case_fraction,
        "pillar_join_fractions": join_fractions,
        "usable_coverage_by_pipeline": usable_by_pipeline,
        "rows_admitted": int(len(data)),
        "cells": len(rows),
        "attributing_cells": len(attributing),
        "checks": checks,
        "files": {
            "component_ic.csv": {
                "sha256": sha256_file(cells_path),
                "rows": len(rows),
            },
            "component_coverage.csv": {
                "sha256": sha256_file(coverage_path),
                "rows": len(coverage_rows),
            },
            "component_usable_coverage.csv": {
                "sha256": sha256_file(usable_coverage_path),
                "rows": len(usable_coverage_rows),
            },
        },
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

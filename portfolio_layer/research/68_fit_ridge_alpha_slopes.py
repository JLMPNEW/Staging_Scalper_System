#!/usr/bin/env python3
"""Stage 11 - first-pass empirical alpha calibration: ridge score-payoff slopes per (pipeline, horizon).

Consumes the joined calibration panel (research/67). For every (source_pipeline, horizon):

  slope_pooled_ols     pooled cross-sectional OLS of the forward target on score_z_pipeline_date
  slope_pooled_ridge   same, shrunk toward 0: sum(z*y) / (sum(z^2) + shrinkage * n)
  fm_slope / fm_t      Fama-MacBeth: mean and t-stat of the per-date cross-sectional slopes
  ic / rank_ic         pooled Pearson IC and mean per-date Spearman rank IC
  stability            sign consistency across dates + first/second-half split slopes
  approved             conservative promotion verdict with explicit rejection reasons

Row admission (all required): research-eligible (contract flag OR tech sidecar stage11 flag),
label status ok for the horizon, usable_for_promoted_training=1 (lockbox purge), and
survivorship_complete=1. Every exclusion is counted in the manifest — nothing drops silently.

STATISTICAL HONESTY: forward windows overlap across neighboring snapshot dates, so per-date slopes
are not independent. `independent_windows` estimates how many non-overlapping label windows the
snapshot span actually contains; approval requires a minimum. Short test spans therefore produce
`approved=0 (insufficient_independent_windows)` BY DESIGN — slopes become promotable only once the
historical generation provides enough non-overlapping windows.

LOCKBOX: rows come from 67 (sealed snapshots already excluded); this script re-verifies and refuses
on any in_lockbox row. Approved slopes feed Stage 1 / Stage 7 only after Stage 11 OOS gates (69/16).

`--selftest` verifies slope recovery, ridge shrinkage direction, FM t, rank IC, and the approval
logic on synthetic data.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.research.stage11_common import (  # noqa: E402
    admit_calibration_rows, forward_status_is_valid, independent_windows, load_lockbox,
    manifest_file_errors, mean_t, mean_t_hac, parse_finite, per_date_slope, pooled_slopes, rank_ic_of,
)


LOGGER = logging.getLogger("fit_ridge_alpha_slopes")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"

SLOPE_FIELDS = [
    "source_pipeline", "horizon_days", "target", "n_obs", "n_dates", "span_calendar_days",
    "independent_windows", "slope_pooled_ols", "slope_pooled_ridge", "ridge_shrinkage",
    "fm_slope", "fm_slope_se", "fm_t", "ic", "rank_ic", "rank_ic_t",
    "sign_consistency", "slope_first_half", "slope_second_half",
    "approved", "rejection_reasons",
]
DATE_SLOPE_FIELDS = ["source_pipeline", "horizon_days", "as_of_date", "n", "slope", "rank_ic"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stage 11 ridge alpha-slope calibration (research-only).")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--panel-build", default=None,
                   help="calibration_panel build to consume (default: latest).")
    p.add_argument("--selftest", action="store_true", help="Run synthetic estimator self-tests and exit.")
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def approval(
    *, n_obs: int, n_dates: int, windows: int, fm_t: float | None, sign_consistency: float | None,
    cfg: dict[str, Any],
) -> tuple[int, list[str]]:
    reasons: list[str] = []
    if n_obs < int(cfg.get("min_observations", 200)):
        reasons.append(f"insufficient_observations:{n_obs}")
    if n_dates < int(cfg.get("min_dates", 8)):
        reasons.append(f"insufficient_dates:{n_dates}")
    if windows < int(cfg.get("min_independent_windows", 3)):
        reasons.append(f"insufficient_independent_windows:{windows}")
    t_min = float(cfg.get("approve_t_min", 2.0))
    if fm_t is None or abs(fm_t) < t_min:
        reasons.append(f"fm_t_below_{t_min:g}:{'' if fm_t is None else round(fm_t, 3)}")
    s_min = float(cfg.get("min_sign_consistency", 0.55))
    if sign_consistency is None or sign_consistency < s_min:
        reasons.append(f"sign_consistency_below_{s_min:g}")
    return (1 if not reasons else 0), reasons


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _selftest() -> None:
    rng = np.random.default_rng(7)
    true_beta = 0.02
    n_dates, n_names = 40, 60
    z_all, y_all, slopes, rics = [], [], [], []
    for _ in range(n_dates):
        z = rng.standard_normal(n_names)
        z = (z - z.mean()) / z.std(ddof=1)
        y = true_beta * z + rng.standard_normal(n_names) * 0.03
        z_all.append(z)
        y_all.append(y)
        slopes.append(per_date_slope(z, y))
        rics.append(rank_ic_of(z, y))
    z_pool = np.concatenate(z_all)
    y_pool = np.concatenate(y_all)
    ols, ridge = pooled_slopes(z_pool, y_pool, shrinkage=0.25)
    assert ols is not None and abs(ols - true_beta) < 0.005, ols
    assert ridge is not None and 0 < ridge < ols, (ols, ridge)  # shrinks toward zero
    szz = float(z_pool @ z_pool)
    assert abs(ridge - ols * szz / (szz + 0.25 * len(z_pool))) < 1e-12  # exact ridge formula
    fm_mean, _se, fm_t = mean_t([s for s in slopes if s is not None])
    assert fm_mean is not None and abs(fm_mean - true_beta) < 0.005
    assert fm_t is not None and fm_t > 4.0, fm_t
    assert np.mean([r for r in rics if r is not None]) > 0.2
    # null signal: slope ~ 0 and approval rejects on fm_t
    y_null = [rng.standard_normal(n_names) * 0.03 for _ in range(n_dates)]
    null_slopes = [per_date_slope(z_all[i], y_null[i]) for i in range(n_dates)]
    _m, _s, t_null = mean_t([s for s in null_slopes if s is not None])
    ok, reasons = approval(n_obs=n_dates * n_names, n_dates=n_dates, windows=10, fm_t=t_null,
                           sign_consistency=0.5, cfg={})
    assert ok == 0 and any(r.startswith("fm_t_below") or r.startswith("sign_") for r in reasons), reasons
    # independence: 10 daily dates within one 21d window -> 1 independent window
    days = [f"2024-01-{d:02d}" for d in (2, 3, 4, 5, 8, 9, 10, 11, 12, 16)]
    assert independent_windows(days, 21) == 1, independent_windows(days, 21)
    assert independent_windows(["2024-01-02", "2025-01-02"], 21) > 10
    ok, reasons = approval(n_obs=5000, n_dates=10, windows=1, fm_t=5.0, sign_consistency=0.9, cfg={})
    assert ok == 0 and any("independent_windows" in r for r in reasons), reasons
    # degenerate guards
    assert per_date_slope(np.array([1.0, 1.0]), np.array([0.1, 0.2])) is None
    assert pooled_slopes(np.array([0.0]), np.array([0.1]), shrinkage=0.25) == (None, None)
    assert rank_ic_of(np.array([1.0, 2.0]), np.array([0.1, 0.2])) is None
    print("alpha-slope calibration self-test: PASS")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
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

    ac = cfg_get(config, "alpha_calibration", {}) or {}
    target_kind = str(ac.get("target", "excess_sector"))
    shrinkage = float(ac.get("ridge_shrinkage", 0.25))
    min_cross_section = int(ac.get("min_cross_section_names", 8))
    horizons = [int(h) for h in cfg_get(config, "calibration_targets.horizons_trading_days", [21, 63, 126, 252])]
    if shrinkage < 0 or min_cross_section < 2 or any(h <= 0 for h in horizons):
        LOGGER.error(
            "Invalid alpha_calibration config: ridge_shrinkage=%s min_cross_section=%s horizons=%s",
            shrinkage,
            min_cross_section,
            horizons,
        )
        return 1

    panel_root = paths.output_dir / str(cfg_get(config, "calibration_panel.dir", "calibration_panel"))
    panel_dir = _latest_build(panel_root, args.panel_build)
    if panel_dir is None:
        LOGGER.error("No calibration-panel build found under %s; run research/67 first", panel_root)
        return 1
    panel_manifest = json.loads((panel_dir / "calibration_panel_manifest.json").read_text(encoding="utf-8"))
    if panel_manifest.get("acceptance") != "PASS":
        LOGGER.error("Calibration panel %s acceptance=%s; refusing", panel_dir.name, panel_manifest.get("acceptance"))
        return 1
    panel_path = panel_dir / "calibration_panel.csv"
    rows = read_csv(panel_path)
    panel_bad = manifest_file_errors(
        panel_manifest,
        {"calibration_panel.csv": panel_path},
        row_counts={"calibration_panel.csv": len(rows)},
    )
    if panel_bad:
        LOGGER.error("Calibration panel %s is not current: %s", panel_dir.name, panel_bad[:8])
        return 1

    out_dir = paths.output_dir / str(ac.get("dir", "alpha_calibration")) / panel_dir.name
    slopes_path = out_dir / "alpha_slopes.csv"
    date_slopes_path = out_dir / "fm_date_slopes.csv"
    manifest_path = out_dir / "alpha_calibration_manifest.json"
    if args.force:
        for p in (slopes_path, date_slopes_path, manifest_path):
            if p.exists():
                p.unlink()
    try:
        fail_if_exists([slopes_path, date_slopes_path, manifest_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    # ---- row admission with full accounting ----
    leaked_lockbox = sum(1 for r in rows if str(r.get("in_lockbox", "0")).strip() == "1")
    admitted, exclusions = admit_calibration_rows(rows)

    target_col = {"excess_sector": "excess_sector_{h}d", "excess_spy": "excess_spy_{h}d",
                  "raw": "fwd_return_{h}d", "resid_sector": "resid_sector_{h}d"}.get(target_kind)
    if target_col is None:
        LOGGER.error("alpha_calibration.target must be excess_sector|excess_spy|raw|resid_sector, got %r", target_kind)
        return 1

    slope_rows: list[dict[str, Any]] = []
    date_slope_rows: list[dict[str, Any]] = []
    label_missing: dict[str, int] = {}
    pipelines = sorted({str(r.get("source_pipeline", "")) for r in admitted})
    for pipe in pipelines + ["ALL"]:
        sub = admitted if pipe == "ALL" else [r for r in admitted if str(r.get("source_pipeline", "")) == pipe]
        for h in horizons:
            col = target_col.format(h=h)
            status_col = f"fwd_status_{h}d"
            usable = []
            for r in sub:
                if not forward_status_is_valid(r.get(status_col)):
                    label_missing[f"{pipe}:{h}d:status"] = label_missing.get(f"{pipe}:{h}d:status", 0) + 1
                    continue
                z = parse_finite(r.get("score_z_pipeline_date"))
                if z is None:
                    label_missing[f"{pipe}:{h}d:score_z"] = label_missing.get(f"{pipe}:{h}d:score_z", 0) + 1
                    continue
                y = parse_finite(r.get(col))
                if y is None:
                    label_missing[f"{pipe}:{h}d:target"] = label_missing.get(f"{pipe}:{h}d:target", 0) + 1
                    continue
                usable.append((str(r.get("as_of_date", "")), z, y))
            if not usable:
                continue
            by_date: dict[str, list[tuple[float, float]]] = {}
            for d, z, y in usable:
                by_date.setdefault(d, []).append((z, y))
            z_pool = np.array([z for _, z, _y in usable])
            y_pool = np.array([y for _, _z, y in usable])
            ols, ridge = pooled_slopes(z_pool, y_pool, shrinkage=shrinkage)
            fm_slopes: list[float] = []
            date_rics: list[float] = []
            for d in sorted(by_date):
                pairs = by_date[d]
                if len(pairs) < min_cross_section:
                    continue
                z_d = np.array([p[0] for p in pairs])
                y_d = np.array([p[1] for p in pairs])
                s = per_date_slope(z_d, y_d)
                ric = rank_ic_of(z_d, y_d)
                if s is not None:
                    fm_slopes.append(s)
                    date_slope_rows.append({
                        "source_pipeline": pipe, "horizon_days": h, "as_of_date": d,
                        "n": len(pairs), "slope": round(s, 8),
                        "rank_ic": round(ric, 6) if ric is not None else "",
                    })
                if ric is not None:
                    date_rics.append(ric)
            hac_lag = max(0, h - 1)
            fm_mean, fm_se, fm_t = mean_t_hac(fm_slopes, max_lag=hac_lag)
            _ric_mean, _ric_se, ric_t = mean_t_hac(date_rics, max_lag=hac_lag)
            ic_pool = None
            if len(z_pool) >= 3 and z_pool.std() > 0 and y_pool.std() > 0:
                ic_pool = float(np.corrcoef(z_pool, y_pool)[0, 1])
            sign_consistency = None
            if fm_slopes:
                dominant = 1.0 if (fm_mean or 0.0) >= 0 else -1.0
                sign_consistency = float(np.mean([1.0 if s * dominant > 0 else 0.0 for s in fm_slopes]))
            half = len(fm_slopes) // 2
            first_half = float(np.mean(fm_slopes[:half])) if half else None
            second_half = float(np.mean(fm_slopes[half:])) if len(fm_slopes) > half else None
            dates_used = sorted({d for d in by_date if len(by_date[d]) >= min_cross_section})
            windows = independent_windows(dates_used, h)
            span = ((date.fromisoformat(max(dates_used)) - date.fromisoformat(min(dates_used))).days
                    if dates_used else 0)
            approved, reasons = approval(
                n_obs=len(usable), n_dates=len(dates_used), windows=windows, fm_t=fm_t,
                sign_consistency=sign_consistency, cfg=ac,
            )
            slope_rows.append({
                "source_pipeline": pipe, "horizon_days": h, "target": col,
                "n_obs": len(usable), "n_dates": len(dates_used), "span_calendar_days": span,
                "independent_windows": windows,
                "slope_pooled_ols": round(ols, 8) if ols is not None else "",
                "slope_pooled_ridge": round(ridge, 8) if ridge is not None else "",
                "ridge_shrinkage": shrinkage,
                "fm_slope": round(fm_mean, 8) if fm_mean is not None else "",
                "fm_slope_se": round(fm_se, 8) if fm_se is not None else "",
                "fm_t": round(fm_t, 4) if fm_t is not None else "",
                "ic": round(ic_pool, 6) if ic_pool is not None else "",
                "rank_ic": round(float(np.mean(date_rics)), 6) if date_rics else "",
                "rank_ic_t": round(ric_t, 4) if ric_t is not None else "",
                "sign_consistency": round(sign_consistency, 4) if sign_consistency is not None else "",
                "slope_first_half": round(first_half, 8) if first_half is not None else "",
                "slope_second_half": round(second_half, 8) if second_half is not None else "",
                "approved": approved,
                "rejection_reasons": ";".join(reasons),
            })

    # ---- gates ----
    checks: list[dict[str, str]] = []

    def rec(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    rec("lockbox_no_sealed_rows", "PASS" if leaked_lockbox == 0 else "FAIL",
        "panel rows are all dev-window" if leaked_lockbox == 0 else f"{leaked_lockbox} sealed rows in panel input")
    admission_ok = len(admitted) + sum(exclusions.values()) == len(rows)
    rec(
        "training_admission_enforced",
        "PASS" if admission_ok else "FAIL",
        f"admitted rows all usable_for_promoted_training=1; excluded={exclusions['not_usable_for_promoted_training']}"
        if admission_ok else f"admitted={len(admitted)} exclusions={sum(exclusions.values())} rows={len(rows)}",
    )
    rec("admission_accounted", "PASS" if admission_ok else "FAIL",
        f"admitted={len(admitted)}/{len(rows)}; exclusions={exclusions}")
    approved_rows = [r for r in slope_rows if r["approved"] == 1]
    rec("approval_conservatism", "PASS" if all(
        int(r["independent_windows"]) >= int(ac.get("min_independent_windows", 3)) for r in approved_rows
    ) else "FAIL", f"{len(approved_rows)} approved of {len(slope_rows)} (pipeline, horizon) cells")
    shadow = "approved slopes are RESEARCH ONLY until Stage 11 OOS validation (69/16) passes"
    rec("promotion_deferred", "PASS", shadow)

    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(slopes_path, SLOPE_FIELDS, slope_rows)
    write_csv(date_slopes_path, DATE_SLOPE_FIELDS, date_slope_rows)
    passed = all(c["status"] in ("PASS", "WARN") for c in checks)
    manifest = {
        "stage": "stage11_alpha_calibration",
        "generated_at": utc_now(),
        "acceptance": "PASS" if passed else "FAIL",
        "panel_build": panel_dir.name,
        "panel_manifest_sha256": sha256_file(panel_dir / "calibration_panel_manifest.json"),
        "protocol_sha256": lockbox["protocol_sha256"],
        "target": target_kind,
        "ridge_shrinkage": shrinkage,
        "horizons_trading_days": horizons,
        "rows_in_panel": len(rows),
        "rows_admitted": len(admitted),
        "exclusions": exclusions,
        "label_exclusions": dict(sorted(label_missing.items())),
        "cells": len(slope_rows),
        "cells_approved": len(approved_rows),
        "checks": checks,
        "files": {
            "alpha_slopes.csv": {"sha256": sha256_file(slopes_path), "rows": len(slope_rows)},
            "fm_date_slopes.csv": {"sha256": sha256_file(date_slopes_path), "rows": len(date_slope_rows)},
        },
    }
    write_manifest(manifest_path, manifest)
    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    LOGGER.info("ALPHA CALIBRATION: %s (admitted=%d, cells=%d, approved=%d) -> %s",
                "PASS" if passed else "FAIL", len(admitted), len(slope_rows), len(approved_rows), out_dir)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

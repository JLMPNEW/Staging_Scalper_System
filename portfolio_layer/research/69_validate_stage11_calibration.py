#!/usr/bin/env python3
"""Stage 11 - purged walk-forward validation of the alpha calibration (research/68's evidence).

For every (source_pipeline, horizon) cell, splits the admitted calibration rows into K
chronologically contiguous test folds. For each fold the TRAIN set contains only snapshot dates
whose forward-label windows cannot overlap any test date's window:

    train date d is admitted iff  d < test_start - W,
    W = horizon trading days * 7/5 calendar + embargo_extra_calendar_days

(expanding-window purged walk-forward: overlapping label windows share outcomes and future dates
are never available to an earlier fold). Per fold: fit the pooled ridge slope on train, then
score the test dates OUT OF FOLD:

  oof_rank_ic          per-test-date Spearman of sign(trained slope) * z vs the realized target
                       (did training pick the RIGHT DIRECTION out of sample?)
  static_rank_ic       per-test-date Spearman of +z (the provisional score->alpha assumption)
  oos_r2_vs_zero       pooled 1 - MSE(slope*z) / MSE(0) on test rows (magnitude skill vs no-skill)
  calibration_slope    pooled OLS of realized y on predicted slope*z (1.0 = magnitudes calibrated)

Verdict per cell: `oos_validated` requires enough valid folds, enough INDEPENDENT (non-overlapping)
test windows, an out-of-fold rank-IC t-stat over threshold, and positive magnitude skill. Short
test spans therefore report `insufficient_purged_folds` / `insufficient_independent_test_windows`
BY DESIGN — the same statistical honesty as 68's approval gate.

LOCKBOX: rows come from 67 (sealed snapshots excluded); re-verified here. Validated slopes still
feed Stage 1 / Stage 7 only after the Stage 16 walk-forward ablation confirms net-of-cost value.

`--selftest` verifies fold purging, direction recovery, null rejection, and regime-flip rejection
on synthetic data.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, timedelta
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
    manifest_file_errors, mean_t_hac, parse_finite, pooled_slopes, rank_ic_of,
)


LOGGER = logging.getLogger("validate_stage11_calibration")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"

CELL_FIELDS = [
    "source_pipeline", "horizon_days", "target", "n_obs", "n_dates", "n_folds", "folds_valid",
    "test_dates_scored", "independent_test_windows", "oof_rank_ic", "oof_rank_ic_t",
    "static_rank_ic", "trained_minus_static", "oos_r2_vs_zero", "calibration_slope",
    "oos_validated", "rejection_reasons",
]
FOLD_FIELDS = [
    "source_pipeline", "horizon_days", "fold", "train_dates", "test_dates", "purged_dates",
    "train_rows", "trained_slope_ridge", "fold_oof_rank_ic",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stage 11 purged walk-forward calibration validation.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--panel-build", default=None, help="calibration_panel build to consume (default: latest).")
    p.add_argument("--selftest", action="store_true", help="Run synthetic purged-CV self-tests and exit.")
    p.add_argument("--force", action="store_true")
    return p.parse_args()


# ---------------------------------------------------------------------------
# purged fold construction (pure, self-tested)
# ---------------------------------------------------------------------------
def purge_window_days(horizon_trading_days: int, embargo_extra_calendar_days: int) -> int:
    return int(np.ceil(horizon_trading_days * 7.0 / 5.0)) + int(embargo_extra_calendar_days)


def purged_folds(
    dates: list[str], *, n_folds: int, horizon_trading_days: int, embargo_extra_calendar_days: int = 0,
) -> list[tuple[list[str], list[str], list[str]]]:
    """(train_dates, test_dates, purged_dates) per chronologically contiguous test block.

    A deployable walk-forward fold can train only on dates strictly before the test block. The
    additional W-day purge ensures every training label has ended before the first test decision.
    All remaining non-test dates, including future dates, are reported as excluded/purged.
    """
    ordered = sorted(set(dates))
    if not ordered or n_folds < 1:
        return []
    window = purge_window_days(horizon_trading_days, embargo_extra_calendar_days)
    blocks = [list(b) for b in np.array_split(np.array(ordered), min(n_folds, len(ordered))) if len(b)]
    folds: list[tuple[list[str], list[str], list[str]]] = []
    for block in blocks:
        test = [str(d) for d in block]
        lo = date.fromisoformat(test[0]) - timedelta(days=window)
        train, purged = [], []
        for d in ordered:
            if d in test:
                continue
            day = date.fromisoformat(d)
            if day < lo:
                train.append(d)
            else:
                purged.append(d)
        folds.append((train, test, purged))
    return folds


def verify_purge(folds: list[tuple[list[str], list[str], list[str]]], *, window: int) -> list[str]:
    """Re-check no overlap and no future training dates (gate input)."""
    violations: list[str] = []
    for i, (train, test, _purged) in enumerate(folds):
        if train and test and max(train) >= min(test):
            violations.append(f"fold{i}:non_chronological_train={max(train)} test_start={min(test)}")
        for d in train:
            for t in test:
                delta = (date.fromisoformat(t) - date.fromisoformat(d)).days
                if delta <= window:
                    violations.append(f"fold{i}:{d}~{t}")
    return violations


def oos_metrics(pred: np.ndarray, y: np.ndarray) -> tuple[float | None, float | None]:
    """(r2_vs_zero, calibration_slope) pooled over out-of-fold rows."""
    if len(y) < 3:
        return None, None
    ss_zero = float(y @ y)
    if ss_zero <= 0:
        return None, None
    resid = y - pred
    r2 = 1.0 - float(resid @ resid) / ss_zero
    spp = float(pred @ pred)
    calib = float(pred @ y) / spp if spp > 0 else None
    return r2, calib


def validation_verdict(
    *, folds_valid: int, test_windows: int, oof_t: float | None, oos_r2: float | None,
    cfg: dict[str, Any],
) -> tuple[int, list[str]]:
    reasons: list[str] = []
    if folds_valid < int(cfg.get("min_folds_valid", 3)):
        reasons.append(f"insufficient_purged_folds:{folds_valid}")
    if test_windows < int(cfg.get("min_independent_test_windows", 3)):
        reasons.append(f"insufficient_independent_test_windows:{test_windows}")
    t_min = float(cfg.get("validate_t_min", 2.0))
    if oof_t is None or oof_t < t_min:
        reasons.append(f"oof_rank_ic_t_below_{t_min:g}:{'' if oof_t is None else round(oof_t, 3)}")
    if oos_r2 is None or oos_r2 <= 0.0:
        reasons.append("no_out_of_sample_magnitude_skill")
    return (1 if not reasons else 0), reasons


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _synthetic_cell(rng, *, n_dates: int, step_days: int, n_names: int, beta, noise: float):
    start = date(2000, 1, 3)
    dates = [(start + timedelta(days=step_days * i)).isoformat() for i in range(n_dates)]
    data: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for i, d in enumerate(dates):
        z = rng.standard_normal(n_names)
        z = (z - z.mean()) / z.std(ddof=1)
        b = beta(i) if callable(beta) else beta
        data[d] = (z, b * z + rng.standard_normal(n_names) * noise)
    return dates, data


def _run_cell(dates, data, *, horizon: int, n_folds: int, shrinkage: float = 0.25):
    folds = purged_folds(dates, n_folds=n_folds, horizon_trading_days=horizon)
    window = purge_window_days(horizon, 0)
    assert not verify_purge(folds, window=window)
    oof_rics, preds, ys = [], [], []
    folds_valid = 0
    for train, test, _purged in folds:
        if len(train) < 2:
            continue
        z_tr = np.concatenate([data[d][0] for d in train])
        y_tr = np.concatenate([data[d][1] for d in train])
        _ols, ridge = pooled_slopes(z_tr, y_tr, shrinkage=shrinkage)
        if ridge is None:
            continue
        folds_valid += 1
        sign = 1.0 if ridge >= 0 else -1.0
        for d in test:
            z_te, y_te = data[d]
            ric = rank_ic_of(sign * z_te, y_te)
            if ric is not None:
                oof_rics.append(ric)
            preds.append(ridge * z_te)
            ys.append(y_te)
    _m, _se, t = mean_t_hac(oof_rics, max_lag=max(0, horizon - 1))
    r2, calib = oos_metrics(np.concatenate(preds), np.concatenate(ys)) if preds else (None, None)
    return folds_valid, oof_rics, t, r2, calib


def _selftest() -> None:
    rng = np.random.default_rng(11)
    # purge mechanics: 10 daily dates, 21d horizon -> every non-test date sits inside the purge
    daily = [f"2024-01-{d:02d}" for d in (2, 3, 4, 5, 8, 9, 10, 11, 12, 16)]
    folds = purged_folds(daily, n_folds=5, horizon_trading_days=21)
    assert folds and all(not train for train, _t, _p in folds), "expected fully purged train sets"
    # weekly dates over ~14 months: real folds exist and purge holds
    dates, data = _synthetic_cell(rng, n_dates=60, step_days=7, n_names=40, beta=0.02, noise=0.03)
    folds = purged_folds(dates, n_folds=5, horizon_trading_days=21)
    window = purge_window_days(21, 0)
    assert not verify_purge(folds, window=window)
    assert any(train for train, _t, _p in folds[1:]), "later walk-forward folds need non-empty train sets"
    assert all(not train or max(train) < min(test) for train, test, _p in folds)
    # true signal validates
    folds_valid, rics, t, r2, calib = _run_cell(dates, data, horizon=21, n_folds=5)
    ok, reasons = validation_verdict(folds_valid=folds_valid, test_windows=independent_windows(dates, 21),
                                     oof_t=t, oos_r2=r2, cfg={})
    assert ok == 1, (folds_valid, t, r2, reasons)
    assert float(np.mean(rics)) > 0.15 and r2 is not None and r2 > 0 and calib is not None and calib > 0.5
    # null signal rejected on t and r2
    dates_n, data_n = _synthetic_cell(rng, n_dates=60, step_days=7, n_names=40, beta=0.0, noise=0.03)
    fv, _rics, t_n, r2_n, _c = _run_cell(dates_n, data_n, horizon=21, n_folds=5)
    ok_n, reasons_n = validation_verdict(folds_valid=fv, test_windows=independent_windows(dates_n, 21),
                                         oof_t=t_n, oos_r2=r2_n, cfg={})
    assert ok_n == 0 and any("oof_rank_ic_t" in r or "magnitude" in r for r in reasons_n), reasons_n
    # sign flip mid-sample: direction learned in-fold fails out-of-fold -> rejected
    dates_f, data_f = _synthetic_cell(rng, n_dates=60, step_days=7, n_names=40,
                                      beta=lambda i: 0.03 if i < 30 else -0.03, noise=0.01)
    fv_f, rics_f, t_f, r2_f, _c = _run_cell(dates_f, data_f, horizon=21, n_folds=5)
    ok_f, _reasons_f = validation_verdict(folds_valid=fv_f, test_windows=independent_windows(dates_f, 21),
                                          oof_t=t_f, oos_r2=r2_f, cfg={})
    assert ok_f == 0, (t_f, r2_f, float(np.mean(rics_f)))
    print("stage11 purged-validation self-test: PASS")


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
    cv = cfg_get(config, "calibration_validation", {}) or {}
    target_kind = str(ac.get("target", "excess_sector"))
    shrinkage = float(ac.get("ridge_shrinkage", 0.25))
    min_cross_section = int(ac.get("min_cross_section_names", 8))
    n_folds = int(cv.get("n_folds", 5))
    embargo_days = int(cv.get("embargo_extra_calendar_days", 0))
    min_train_dates = int(cv.get("min_train_dates", 8))
    horizons = [int(h) for h in cfg_get(config, "calibration_targets.horizons_trading_days", [21, 63, 126, 252])]
    if shrinkage < 0 or min_cross_section < 2 or n_folds < 2 or embargo_days < 0 or min_train_dates < 1 \
            or any(h <= 0 for h in horizons):
        LOGGER.error(
            "Invalid calibration validation config: shrinkage=%s min_cross_section=%s n_folds=%s "
            "embargo_days=%s min_train_dates=%s horizons=%s",
            shrinkage,
            min_cross_section,
            n_folds,
            embargo_days,
            min_train_dates,
            horizons,
        )
        return 1
    target_col = {"excess_sector": "excess_sector_{h}d", "excess_spy": "excess_spy_{h}d",
                  "raw": "fwd_return_{h}d", "resid_sector": "resid_sector_{h}d"}.get(target_kind)
    if target_col is None:
        LOGGER.error("alpha_calibration.target must be excess_sector|excess_spy|raw|resid_sector, got %r", target_kind)
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

    out_dir = paths.output_dir / str(cv.get("dir", "calibration_validation")) / panel_dir.name
    cells_path = out_dir / "oos_validation.csv"
    folds_path = out_dir / "fold_details.csv"
    manifest_path = out_dir / "validation_manifest.json"
    if args.force:
        for p in (cells_path, folds_path, manifest_path):
            if p.exists():
                p.unlink()
    try:
        fail_if_exists([cells_path, folds_path, manifest_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    leaked_lockbox = sum(1 for r in rows if str(r.get("in_lockbox", "0")).strip() == "1")
    admitted, exclusions = admit_calibration_rows(rows)
    alpha_root = paths.output_dir / str(ac.get("dir", "alpha_calibration"))
    alpha_dir = alpha_root / panel_dir.name
    alpha_manifest_path = alpha_dir / "alpha_calibration_manifest.json"
    alpha_bad: list[str] = []
    alpha_manifest: dict[str, Any] = {}
    if not alpha_manifest_path.exists():
        alpha_bad.append("alpha_calibration_manifest_missing")
    else:
        alpha_manifest = json.loads(alpha_manifest_path.read_text(encoding="utf-8"))
        if alpha_manifest.get("acceptance") != "PASS":
            alpha_bad.append(f"alpha_acceptance={alpha_manifest.get('acceptance')}")
        if str(alpha_manifest.get("panel_build", "")) != panel_dir.name:
            alpha_bad.append(f"alpha_panel_build={alpha_manifest.get('panel_build')}!={panel_dir.name}")
        current_panel_manifest_sha = sha256_file(panel_dir / "calibration_panel_manifest.json")
        if alpha_manifest.get("panel_manifest_sha256") != current_panel_manifest_sha:
            alpha_bad.append("alpha_panel_manifest_sha_mismatch")
        if str(alpha_manifest.get("target", "")) != target_kind:
            alpha_bad.append(f"alpha_target={alpha_manifest.get('target')}!={target_kind}")
        if abs(float(alpha_manifest.get("ridge_shrinkage", float("nan"))) - shrinkage) > 1e-12:
            alpha_bad.append("alpha_ridge_shrinkage_mismatch")
        if [int(h) for h in alpha_manifest.get("horizons_trading_days", [])] != horizons:
            alpha_bad.append("alpha_horizons_mismatch")
        if int(alpha_manifest.get("rows_admitted", -1)) != len(admitted):
            alpha_bad.append(f"alpha_rows_admitted={alpha_manifest.get('rows_admitted')}!={len(admitted)}")
        alpha_bad.extend(manifest_file_errors(
            alpha_manifest,
            {
                "alpha_slopes.csv": alpha_dir / "alpha_slopes.csv",
                "fm_date_slopes.csv": alpha_dir / "fm_date_slopes.csv",
            },
        ))

    cell_rows: list[dict[str, Any]] = []
    fold_rows: list[dict[str, Any]] = []
    purge_violations: list[str] = []
    label_exclusions: dict[str, int] = {}
    pipelines = sorted({str(r.get("source_pipeline", "")) for r in admitted})
    for pipe in pipelines + ["ALL"]:
        sub = admitted if pipe == "ALL" else [r for r in admitted if str(r.get("source_pipeline", "")) == pipe]
        for h in horizons:
            col = target_col.format(h=h)
            status_col = f"fwd_status_{h}d"
            by_date: dict[str, list[tuple[float, float]]] = {}
            for r in sub:
                if not forward_status_is_valid(r.get(status_col)):
                    label_exclusions[f"{pipe}:{h}d:status"] = label_exclusions.get(f"{pipe}:{h}d:status", 0) + 1
                    continue
                if str(r.get(col, "")).strip() == "":
                    label_exclusions[f"{pipe}:{h}d:target"] = label_exclusions.get(f"{pipe}:{h}d:target", 0) + 1
                    continue
                z = parse_finite(r.get("score_z_pipeline_date"))
                y = parse_finite(r.get(col))
                if z is None:
                    label_exclusions[f"{pipe}:{h}d:score_z"] = label_exclusions.get(f"{pipe}:{h}d:score_z", 0) + 1
                    continue
                if y is None:
                    label_exclusions[f"{pipe}:{h}d:target"] = label_exclusions.get(f"{pipe}:{h}d:target", 0) + 1
                    continue
                by_date.setdefault(str(r.get("as_of_date", "")), []).append(
                    (z, y)
                )
            dates = sorted(d for d, pairs in by_date.items() if len(pairs) >= min_cross_section)
            if not dates:
                continue
            n_obs = sum(len(by_date[d]) for d in dates)
            folds = purged_folds(dates, n_folds=n_folds, horizon_trading_days=h,
                                 embargo_extra_calendar_days=embargo_days)
            window = purge_window_days(h, embargo_days)
            purge_violations.extend(f"{pipe}:{h}d:{v}" for v in verify_purge(folds, window=window))
            oof_rics: list[float] = []
            static_rics: list[float] = []
            preds: list[np.ndarray] = []
            ys: list[np.ndarray] = []
            folds_valid = 0
            test_dates_scored: set[str] = set()
            for i, (train, test, purged) in enumerate(folds):
                trained_ridge = None
                fold_rics: list[float] = []
                if len(train) >= min_train_dates:
                    z_tr = np.concatenate([np.array([p[0] for p in by_date[d]]) for d in train])
                    y_tr = np.concatenate([np.array([p[1] for p in by_date[d]]) for d in train])
                    _ols, trained_ridge = pooled_slopes(z_tr, y_tr, shrinkage=shrinkage)
                if trained_ridge is not None:
                    folds_valid += 1
                    sign = 1.0 if trained_ridge >= 0 else -1.0
                    for d in test:
                        z_te = np.array([p[0] for p in by_date[d]])
                        y_te = np.array([p[1] for p in by_date[d]])
                        ric = rank_ic_of(sign * z_te, y_te)
                        s_ric = rank_ic_of(z_te, y_te)
                        if ric is not None:
                            oof_rics.append(ric)
                            fold_rics.append(ric)
                            test_dates_scored.add(d)
                        if s_ric is not None:
                            static_rics.append(s_ric)
                        preds.append(trained_ridge * z_te)
                        ys.append(y_te)
                fold_rows.append({
                    "source_pipeline": pipe, "horizon_days": h, "fold": i,
                    "train_dates": len(train), "test_dates": len(test), "purged_dates": len(purged),
                    "train_rows": sum(len(by_date[d]) for d in train),
                    "trained_slope_ridge": round(trained_ridge, 8) if trained_ridge is not None else "",
                    "fold_oof_rank_ic": round(float(np.mean(fold_rics)), 6) if fold_rics else "",
                })
            oof_mean, _se, oof_t = mean_t_hac(oof_rics, max_lag=max(0, h - 1))
            static_mean = float(np.mean(static_rics)) if static_rics else None
            r2, calib = oos_metrics(np.concatenate(preds), np.concatenate(ys)) if preds else (None, None)
            test_windows = independent_windows(sorted(test_dates_scored), h)
            validated, reasons = validation_verdict(
                folds_valid=folds_valid, test_windows=test_windows, oof_t=oof_t, oos_r2=r2, cfg=cv,
            )
            cell_rows.append({
                "source_pipeline": pipe, "horizon_days": h, "target": col,
                "n_obs": n_obs, "n_dates": len(dates), "n_folds": len(folds), "folds_valid": folds_valid,
                "test_dates_scored": len(test_dates_scored), "independent_test_windows": test_windows,
                "oof_rank_ic": round(oof_mean, 6) if oof_mean is not None else "",
                "oof_rank_ic_t": round(oof_t, 4) if oof_t is not None else "",
                "static_rank_ic": round(static_mean, 6) if static_mean is not None else "",
                "trained_minus_static": round(oof_mean - static_mean, 6)
                if oof_mean is not None and static_mean is not None else "",
                "oos_r2_vs_zero": round(r2, 6) if r2 is not None else "",
                "calibration_slope": round(calib, 6) if calib is not None else "",
                "oos_validated": validated,
                "rejection_reasons": ";".join(reasons),
            })

    # ---- gates ----
    checks: list[dict[str, str]] = []

    def rec(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    rec("lockbox_no_sealed_rows", "PASS" if leaked_lockbox == 0 else "FAIL",
        "panel rows are all dev-window" if leaked_lockbox == 0 else f"{leaked_lockbox} sealed rows in panel input")
    rec("purge_verified", "PASS" if not purge_violations else "FAIL",
        "no train/test label-window overlap in any constructed fold"
        if not purge_violations else f"{purge_violations[:8]}")
    admission_ok = len(admitted) + sum(exclusions.values()) == len(rows)
    rec(
        "admission_accounted",
        "PASS" if admission_ok else "FAIL",
        f"admitted={len(admitted)}/{len(rows)}; exclusions={exclusions}"
        if admission_ok else f"admitted+excluded={len(admitted) + sum(exclusions.values())}!={len(rows)}",
    )
    rec("alpha_calibration_current", "PASS" if not alpha_bad else "FAIL",
        "68 alpha calibration manifest/files match this panel and validation config"
        if not alpha_bad else f"{alpha_bad[:8]}")
    validated_cells = [r for r in cell_rows if r["oos_validated"] == 1]
    degenerate = [r for r in cell_rows if r["folds_valid"] == 0]
    rec("degenerate_cells_flagged", "PASS" if all(r["oos_validated"] == 0 for r in degenerate) else "FAIL",
        f"{len(degenerate)} cells with zero valid purged folds, all unvalidated")
    rec("promotion_deferred", "PASS",
        "validated cells remain research-only until the Stage 16 walk-forward ablation confirms net-of-cost value")

    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(cells_path, CELL_FIELDS, cell_rows)
    write_csv(folds_path, FOLD_FIELDS, fold_rows)
    passed = all(c["status"] in ("PASS", "WARN") for c in checks)
    manifest = {
        "stage": "stage11_calibration_validation",
        "generated_at": utc_now(),
        "acceptance": "PASS" if passed else "FAIL",
        "panel_build": panel_dir.name,
        "panel_manifest_sha256": sha256_file(panel_dir / "calibration_panel_manifest.json"),
        "protocol_sha256": lockbox["protocol_sha256"],
        "target": target_kind,
        "ridge_shrinkage": shrinkage,
        "n_folds": n_folds,
        "embargo_extra_calendar_days": embargo_days,
        "rows_in_panel": len(rows),
        "rows_admitted": len(admitted),
        "exclusions": exclusions,
        "label_exclusions": dict(sorted(label_exclusions.items())),
        "alpha_calibration_manifest_sha256": sha256_file(alpha_manifest_path) if alpha_manifest_path.exists() else "",
        "cells": len(cell_rows),
        "cells_validated": len(validated_cells),
        "checks": checks,
        "files": {
            "oos_validation.csv": {"sha256": sha256_file(cells_path), "rows": len(cell_rows)},
            "fold_details.csv": {"sha256": sha256_file(folds_path), "rows": len(fold_rows)},
        },
    }
    write_manifest(manifest_path, manifest)
    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    LOGGER.info("CALIBRATION VALIDATION: %s (cells=%d, validated=%d) -> %s",
                "PASS" if passed else "FAIL", len(cell_rows), len(validated_cells), out_dir)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

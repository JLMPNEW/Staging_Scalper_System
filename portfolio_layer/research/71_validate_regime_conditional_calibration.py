#!/usr/bin/env python3
"""Stage 11 - REGIME-CONDITIONAL purged validation of the alpha calibration (68/69 extension).

Motivation (2026-07-06 diagnostic): unconditional purged OOS validation (research/69) rejects
nearly every (pipeline, horizon) cell, yet per-date rank ICs are strongly positive inside the
HEATING_UP / STAGFLATION macro regimes across ALL sectors and flat-to-negative in SLOW_GROWTH and
crisis-2020. This script tests that hypothesis at the same evidentiary standard as 69: for every
(pipeline, horizon, regime) cell, purged expanding-window validation run entirely WITHIN the regime's dates
(folds chronologically contiguous in the regime's own timeline; the calendar purge window around a
test block is therefore conservative across regime gaps).

Per cell it reports the 69 metrics (out-of-fold sign-adjusted rank IC + t, magnitude skill vs zero,
calibration slope) plus two regime-specific columns:

  mean_trained_ridge     the average in-regime trained slope: a VALIDATED cell with a negative
                         slope means the score inverts in that regime, which is tradable
                         information but very different from "score works"
  delta_static_ic / _t   in-regime minus out-of-regime mean per-date rank IC (Welch t):
                         evidence that the CONDITIONING itself carries information, not merely
                         that a subsample is positive

Statistical honesty: this grid is (pipelines+ALL) x horizons x regimes — several dozen cells — so
`validate_t_min` defaults ABOVE 69's threshold, small regimes fail closed with
`insufficient_regime_dates`, and the manifest records the number of cells tested so any reader can
apply their own multiplicity correction. Validated cells are RESEARCH EVIDENCE ONLY: acting on them
requires a regime-conditional sizing design through Stage 6/7/8, a Stage 16 walk-forward arm, and a
dated protocol amendment. This script changes no config and promotes nothing.

`--selftest` proves on synthetic data: a regime-dependent signal validates inside its regime while
the unconditional test rejects it; an inverted-regime cell validates with a NEGATIVE trained slope;
an undersized regime fails closed.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import sys
from collections import Counter
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

# reuse 69's self-tested purged-fold machinery verbatim (module name starts with a digit,
# so a file-location import is the only way to share it without duplicating tested code)
_spec = importlib.util.spec_from_file_location(
    "stage11_purged_validation", Path(__file__).with_name("69_validate_stage11_calibration.py"))
assert _spec is not None and _spec.loader is not None
_v69 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_v69)
purged_folds = _v69.purged_folds
purge_window_days = _v69.purge_window_days
verify_purge = _v69.verify_purge
oos_metrics = _v69.oos_metrics
validation_verdict = _v69.validation_verdict


LOGGER = logging.getLogger("validate_regime_conditional_calibration")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"

CELL_FIELDS = [
    "source_pipeline", "horizon_days", "regime", "target", "n_obs", "n_dates", "regime_share_of_dates",
    "n_folds", "folds_valid", "test_dates_scored", "independent_test_windows",
    "mean_trained_ridge", "oof_rank_ic", "oof_rank_ic_t", "static_rank_ic",
    "complement_static_ic", "delta_static_ic", "delta_static_t",
    "oos_r2_vs_zero", "calibration_slope", "oos_validated", "rejection_reasons",
]
FOLD_FIELDS = [
    "source_pipeline", "horizon_days", "regime", "fold", "train_dates", "test_dates", "purged_dates",
    "train_rows", "trained_slope_ridge", "fold_oof_rank_ic",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stage 11 regime-conditional purged calibration validation.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--panel-build", default=None, help="calibration_panel build to consume (default: latest).")
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def welch_t(a: list[float], b: list[float], *, max_lag: int) -> float | None:
    if len(a) < 3 or len(b) < 3:
        return None
    a_mean, a_se, _ = mean_t_hac(a, max_lag=max_lag)
    b_mean, b_se, _ = mean_t_hac(b, max_lag=max_lag)
    if a_mean is None or b_mean is None or a_se is None or b_se is None:
        return None
    denom = float(np.sqrt(a_se * a_se + b_se * b_se))
    if denom <= 0:
        return None
    return float((a_mean - b_mean) / denom)


def regime_of_dates(rows: list[dict[str, str]], regime_col: str) -> tuple[dict[str, str], int]:
    """Per-date regime label (modal across the date's rows) + count of rows disagreeing with it."""
    by_date: dict[str, Counter] = {}
    for r in rows:
        regime = str(r.get(regime_col, "")).strip()
        if regime:
            by_date.setdefault(str(r.get("as_of_date", "")), Counter())[regime] += 1
    labels = {d: c.most_common(1)[0][0] for d, c in by_date.items()}
    inconsistent = sum(
        count for d, c in by_date.items() for regime, count in c.items() if regime != labels[d]
    )
    return labels, inconsistent


def validate_regime_cell(
    by_date: dict[str, list[tuple[float, float]]],
    regime_dates: list[str],
    complement_dates: list[str],
    *,
    horizon: int,
    n_folds: int,
    embargo_days: int,
    min_train_dates: int,
    shrinkage: float,
    min_regime_dates: int,
    verdict_cfg: dict[str, Any],
    max_entry_lag_trading_days: int = 1,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """69-equivalent purged validation restricted to one regime's dates, plus the complement contrast."""
    static_in = [ric for d in regime_dates
                 if (ric := rank_ic_of(np.array([p[0] for p in by_date[d]]),
                                       np.array([p[1] for p in by_date[d]]))) is not None]
    static_out = [ric for d in complement_dates
                  if (ric := rank_ic_of(np.array([p[0] for p in by_date[d]]),
                                        np.array([p[1] for p in by_date[d]]))) is not None]
    delta_t = welch_t(static_in, static_out, max_lag=max(0, horizon - 1))

    folds = purged_folds(regime_dates, n_folds=n_folds, horizon_trading_days=horizon,
                         embargo_extra_calendar_days=embargo_days,
                         max_entry_lag_trading_days=max_entry_lag_trading_days)
    window = purge_window_days(
        horizon,
        embargo_days,
        max_entry_lag_trading_days,
    )
    violations = verify_purge(folds, window=window)
    oof_rics: list[float] = []
    ridges: list[float] = []
    preds: list[np.ndarray] = []
    ys: list[np.ndarray] = []
    folds_valid = 0
    test_dates_scored: set[str] = set()
    fold_rows: list[dict[str, Any]] = []
    for i, (train, test, purged) in enumerate(folds):
        trained_ridge = None
        fold_rics: list[float] = []
        if len(train) >= min_train_dates:
            z_tr = np.concatenate([np.array([p[0] for p in by_date[d]]) for d in train])
            y_tr = np.concatenate([np.array([p[1] for p in by_date[d]]) for d in train])
            _ols, trained_ridge = pooled_slopes(z_tr, y_tr, shrinkage=shrinkage)
        if trained_ridge is not None:
            folds_valid += 1
            ridges.append(trained_ridge)
            sign = 1.0 if trained_ridge >= 0 else -1.0
            for d in test:
                z_te = np.array([p[0] for p in by_date[d]])
                y_te = np.array([p[1] for p in by_date[d]])
                ric = rank_ic_of(sign * z_te, y_te)
                if ric is not None:
                    oof_rics.append(ric)
                    fold_rics.append(ric)
                    test_dates_scored.add(d)
                preds.append(trained_ridge * z_te)
                ys.append(y_te)
        fold_rows.append({
            "fold": i, "train_dates": len(train), "test_dates": len(test), "purged_dates": len(purged),
            "train_rows": sum(len(by_date[d]) for d in train),
            "trained_slope_ridge": round(trained_ridge, 8) if trained_ridge is not None else "",
            "fold_oof_rank_ic": round(float(np.mean(fold_rics)), 6) if fold_rics else "",
        })
    oof_mean, _se, oof_t = mean_t_hac(oof_rics, max_lag=max(0, horizon - 1))
    r2, calib = oos_metrics(np.concatenate(preds), np.concatenate(ys)) if preds else (None, None)
    test_windows = independent_windows(
        sorted(test_dates_scored),
        horizon,
        entry_lag_trading_days=max_entry_lag_trading_days,
    )
    validated, reasons = validation_verdict(
        folds_valid=folds_valid, test_windows=test_windows, oof_t=oof_t, oos_r2=r2, cfg=verdict_cfg,
    )
    if len(regime_dates) < min_regime_dates:
        reasons.append(f"insufficient_regime_dates:{len(regime_dates)}<{min_regime_dates}")
        validated = 0
    cell = {
        "n_obs": sum(len(by_date[d]) for d in regime_dates),
        "n_dates": len(regime_dates),
        "n_folds": len(folds), "folds_valid": folds_valid,
        "test_dates_scored": len(test_dates_scored), "independent_test_windows": test_windows,
        "mean_trained_ridge": round(float(np.mean(ridges)), 8) if ridges else "",
        "oof_rank_ic": round(oof_mean, 6) if oof_mean is not None else "",
        "oof_rank_ic_t": round(oof_t, 4) if oof_t is not None else "",
        "static_rank_ic": round(float(np.mean(static_in)), 6) if static_in else "",
        "complement_static_ic": round(float(np.mean(static_out)), 6) if static_out else "",
        "delta_static_ic": round(float(np.mean(static_in)) - float(np.mean(static_out)), 6)
        if static_in and static_out else "",
        "delta_static_t": round(delta_t, 4) if delta_t is not None else "",
        "oos_r2_vs_zero": round(r2, 6) if r2 is not None else "",
        "calibration_slope": round(calib, 6) if calib is not None else "",
        "oos_validated": validated,
        "rejection_reasons": ";".join(reasons),
    }
    return cell, fold_rows, violations


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _selftest() -> None:
    rng = np.random.default_rng(23)
    start = date(2000, 1, 3)
    # weekly dates over ~6 years, regime alternates in ~26-week blocks: A(+), B(-)
    n_dates, n_names, horizon = 300, 40, 21
    dates, regimes, data = [], {}, {}
    for i in range(n_dates):
        d = (start + timedelta(days=7 * i)).isoformat()
        regime = "A" if (i // 26) % 2 == 0 else "B"
        z = rng.standard_normal(n_names)
        z = (z - z.mean()) / z.std(ddof=1)
        beta = 0.03 if regime == "A" else -0.03
        dates.append(d)
        regimes[d] = regime
        data[d] = [(float(zi), float(beta * zi + rng.standard_normal() * 0.02)) for zi in z]
    cfg = {"validate_t_min": 2.5}
    a_dates = [d for d in dates if regimes[d] == "A"]
    b_dates = [d for d in dates if regimes[d] == "B"]

    cell_a, _f, viol_a = validate_regime_cell(
        data, a_dates, b_dates, horizon=horizon, n_folds=5, embargo_days=0,
        min_train_dates=8, shrinkage=0.25, min_regime_dates=100, verdict_cfg=cfg)
    assert not viol_a and cell_a["oos_validated"] == 1, cell_a
    assert float(cell_a["mean_trained_ridge"]) > 0 and float(cell_a["oof_rank_ic"]) > 0.2
    assert float(cell_a["delta_static_t"]) > 5, cell_a["delta_static_t"]

    # inverted regime validates too, with a NEGATIVE trained slope (sign information)
    cell_b, _f, _v = validate_regime_cell(
        data, b_dates, a_dates, horizon=horizon, n_folds=5, embargo_days=0,
        min_train_dates=8, shrinkage=0.25, min_regime_dates=100, verdict_cfg=cfg)
    assert cell_b["oos_validated"] == 1 and float(cell_b["mean_trained_ridge"]) < 0, cell_b

    # the UNCONDITIONAL cell on the same data must fail (this is 69's regime-flip rejection)
    cell_all, _f, _v = validate_regime_cell(
        data, dates, [], horizon=horizon, n_folds=5, embargo_days=0,
        min_train_dates=8, shrinkage=0.25, min_regime_dates=100, verdict_cfg=cfg)
    assert cell_all["oos_validated"] == 0, cell_all

    # an undersized regime fails closed even with a perfect signal
    cell_small, _f, _v = validate_regime_cell(
        data, a_dates[:30], b_dates, horizon=horizon, n_folds=5, embargo_days=0,
        min_train_dates=8, shrinkage=0.25, min_regime_dates=100, verdict_cfg=cfg)
    assert cell_small["oos_validated"] == 0 and "insufficient_regime_dates" in cell_small["rejection_reasons"]
    print("regime-conditional validation self-test: PASS")


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
    rc = cfg_get(config, "regime_calibration", {}) or {}
    target_kind = str(ac.get("target", "excess_sector"))
    shrinkage = float(ac.get("ridge_shrinkage", 0.25))
    min_cross_section = int(ac.get("min_cross_section_names", 8))
    n_folds = int(cv.get("n_folds", 5))
    embargo_days = int(cv.get("embargo_extra_calendar_days", 0))
    min_train_dates = int(cv.get("min_train_dates", 8))
    regime_col = str(rc.get("regime_column", "macro_regime"))
    min_regime_dates = int(rc.get("min_regime_dates", 120))
    verdict_cfg = dict(cv)
    # stricter than 69: this grid multiplies the comparisons by the number of regimes
    verdict_cfg["validate_t_min"] = float(rc.get("validate_t_min", 2.5))
    horizons = [int(h) for h in cfg_get(config, "calibration_targets.horizons_trading_days", [21, 63, 126, 252])]
    max_entry_lag = int(cfg_get(config, "calibration_targets.max_entry_lag_trading_days", 5))
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
        panel_manifest, {"calibration_panel.csv": panel_path},
        row_counts={"calibration_panel.csv": len(rows)},
    )
    if panel_bad:
        LOGGER.error("Calibration panel %s is not current: %s", panel_dir.name, panel_bad[:8])
        return 1

    out_dir = paths.output_dir / str(rc.get("dir", "regime_calibration")) / panel_dir.name
    cells_path = out_dir / "regime_oos_validation.csv"
    folds_path = out_dir / "regime_fold_details.csv"
    manifest_path = out_dir / "regime_calibration_manifest.json"
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
    date_regime, inconsistent_rows = regime_of_dates(admitted, regime_col)
    regimes = sorted({v for v in date_regime.values()})
    no_regime_dates = sorted({str(r.get("as_of_date", "")) for r in admitted}
                             - set(date_regime))

    cell_rows: list[dict[str, Any]] = []
    fold_rows: list[dict[str, Any]] = []
    purge_violations: list[str] = []
    pipelines = sorted({str(r.get("source_pipeline", "")) for r in admitted})
    for pipe in pipelines + ["ALL"]:
        sub = admitted if pipe == "ALL" else [r for r in admitted if str(r.get("source_pipeline", "")) == pipe]
        for h in horizons:
            col = target_col.format(h=h)
            status_col = f"fwd_status_{h}d"
            by_date: dict[str, list[tuple[float, float]]] = {}
            for r in sub:
                if not forward_status_is_valid(r.get(status_col)):
                    continue
                z = parse_finite(r.get("score_z_pipeline_date"))
                y = parse_finite(r.get(col))
                if z is None or y is None:
                    continue
                by_date.setdefault(str(r.get("as_of_date", "")), []).append((z, y))
            usable_dates = sorted(d for d, pairs in by_date.items()
                                  if len(pairs) >= min_cross_section and d in date_regime)
            if not usable_dates:
                continue
            for regime in regimes:
                regime_dates = [d for d in usable_dates if date_regime[d] == regime]
                complement = [d for d in usable_dates if date_regime[d] != regime]
                if not regime_dates:
                    continue
                cell, cell_folds, violations = validate_regime_cell(
                    by_date, regime_dates, complement, horizon=h, n_folds=n_folds,
                    embargo_days=embargo_days, min_train_dates=min_train_dates, shrinkage=shrinkage,
                    min_regime_dates=min_regime_dates, verdict_cfg=verdict_cfg,
                    max_entry_lag_trading_days=max_entry_lag,
                )
                purge_violations.extend(f"{pipe}:{h}d:{regime}:{v}" for v in violations)
                cell_rows.append({
                    "source_pipeline": pipe, "horizon_days": h, "regime": regime, "target": col,
                    "regime_share_of_dates": round(len(regime_dates) / len(usable_dates), 4),
                    **cell,
                })
                fold_rows.extend({
                    "source_pipeline": pipe, "horizon_days": h, "regime": regime, **fr,
                } for fr in cell_folds)

    checks: list[dict[str, str]] = []

    def rec(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    rec("lockbox_no_sealed_rows", "PASS" if leaked_lockbox == 0 else "FAIL",
        "panel rows are all dev-window" if leaked_lockbox == 0 else f"{leaked_lockbox} sealed rows")
    rec("purge_verified", "PASS" if not purge_violations else "FAIL",
        "no train/test label-window overlap in any regime fold"
        if not purge_violations else f"{purge_violations[:8]}")
    admission_ok = len(admitted) + sum(exclusions.values()) == len(rows)
    rec(
        "admission_accounted",
        "PASS" if admission_ok else "FAIL",
        f"admitted={len(admitted)}/{len(rows)}; exclusions={exclusions}"
        if admission_ok else f"admitted+excluded={len(admitted) + sum(exclusions.values())}!={len(rows)}",
    )
    frac_inconsistent = inconsistent_rows / max(1, len(admitted))
    rec("regime_labels_consistent", "PASS" if frac_inconsistent < 0.01 else "FAIL",
        f"rows disagreeing with their date's modal regime: {inconsistent_rows} "
        f"({frac_inconsistent:.4%}); dates without a regime label: {len(no_regime_dates)}")
    validated_cells = [r for r in cell_rows if r["oos_validated"] == 1]
    negative_validated = [r for r in validated_cells
                          if r["mean_trained_ridge"] != "" and float(r["mean_trained_ridge"]) < 0]
    rec("multiplicity_disclosed", "PASS",
        f"cells_tested={len(cell_rows)} at validate_t_min={verdict_cfg['validate_t_min']}; "
        "apply your own correction before believing any single cell")
    rec("promotion_deferred", "PASS",
        "regime-conditional usage additionally requires a Stage 6/7/8 sizing design, a Stage 16 "
        "walk-forward arm, and a dated protocol amendment; this artifact changes nothing")

    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(cells_path, CELL_FIELDS, cell_rows)
    write_csv(folds_path, FOLD_FIELDS, fold_rows)
    passed = all(c["status"] in ("PASS", "WARN") for c in checks)
    write_manifest(manifest_path, {
        "stage": "stage11_regime_conditional_calibration",
        "generated_at": utc_now(),
        "acceptance": "PASS" if passed else "FAIL",
        "panel_build": panel_dir.name,
        "panel_manifest_sha256": sha256_file(panel_dir / "calibration_panel_manifest.json"),
        "protocol_sha256": lockbox["protocol_sha256"],
        "target": target_kind,
        "regime_column": regime_col,
        "regimes": regimes,
        "min_regime_dates": min_regime_dates,
        "validate_t_min": verdict_cfg["validate_t_min"],
        "max_entry_lag_trading_days": max_entry_lag,
        "rows_in_panel": len(rows),
        "rows_admitted": len(admitted),
        "cells": len(cell_rows),
        "cells_validated": len(validated_cells),
        "cells_validated_negative_slope": len(negative_validated),
        "checks": checks,
        "inputs_sha256": {
            "config.yaml": sha256_file(config_path),
            "research/71_validate_regime_conditional_calibration.py": sha256_file(
                Path(__file__).resolve()
            ),
            "research/69_validate_stage11_calibration.py": sha256_file(
                Path(__file__).with_name("69_validate_stage11_calibration.py")
            ),
            "research/stage11_common.py": sha256_file(
                Path(__file__).with_name("stage11_common.py")
            ),
            "calibration_panel_manifest.json": sha256_file(
                panel_dir / "calibration_panel_manifest.json"
            ),
            "calibration_panel.csv": sha256_file(panel_path),
        },
        "files": {
            "regime_oos_validation.csv": {"sha256": sha256_file(cells_path), "rows": len(cell_rows)},
            "regime_fold_details.csv": {"sha256": sha256_file(folds_path), "rows": len(fold_rows)},
        },
    })
    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    for r in validated_cells:
        LOGGER.info("VALIDATED %s h=%s regime=%s oof_ic=%s t=%s slope=%s delta_t=%s",
                    r["source_pipeline"], r["horizon_days"], r["regime"], r["oof_rank_ic"],
                    r["oof_rank_ic_t"], r["mean_trained_ridge"], r["delta_static_t"])
    LOGGER.info("REGIME CALIBRATION: %s (cells=%d, validated=%d, negative-slope validated=%d) -> %s",
                "PASS" if passed else "FAIL", len(cell_rows), len(validated_cells),
                len(negative_validated), out_dir)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

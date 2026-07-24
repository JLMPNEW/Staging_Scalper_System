#!/usr/bin/env python3
"""Stage 7 - final validation and seal for Black-Litterman fusion.

This is the Stage 7 completion gate. It validates the sealed 23 inputs, 24 solve, and 25 cost overlay;
checks that Stage 3/4 baseline artifacts were not mutated; and emits a WARN-only in-sample net diagnostic.
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

import numpy as np
import pandas as pd
import yaml


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.artifacts import invalidate_dependents  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.optimizer.tier1_portfolio_optimizer import black_litterman_posterior  # noqa: E402
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("validate_bl_fusion")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SOURCE_FILES = ["23_build_bl_inputs.py", "24_run_bl_optimizer.py", "25_apply_bl_cost_overlay.py", "26_validate_bl_fusion.py"]


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Validate and seal Stage 7 Black-Litterman fusion.")
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


def _f_default(value: Any, default: float) -> float:
    parsed = _f(value)
    return default if parsed is None else parsed


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _hashes_current(meta: dict[str, Any], root: Path, files_key: str = "outputs_sha256") -> list[str]:
    bad: list[str] = []
    outputs = meta.get(files_key) or {}
    for rel, expected in outputs.items():
        path = root / str(rel)
        if not path.exists():
            bad.append(f"{rel}:missing")
        elif expected != sha256_file(path):
            bad.append(f"{rel}:hash_mismatch")
    return bad


def _perf_stats(returns: pd.Series, ppy: int = 252) -> dict[str, float | int]:
    r = returns.dropna()
    if r.empty:
        return {"observations": 0}
    ann_ret = float((1.0 + r).prod() ** (ppy / len(r)) - 1.0)
    ann_vol = float(r.std(ddof=1) * np.sqrt(ppy))
    curve = (1.0 + r).cumprod()
    return {
        "observations": int(len(r)),
        "cumulative_return": round(float((1.0 + r).prod() - 1.0), 6),
        "annualized_return": round(ann_ret, 6),
        "annualized_vol": round(ann_vol, 6),
        "sharpe_ratio": round(float(ann_ret / ann_vol) if ann_vol > 0 else float("nan"), 4),
        "max_drawdown": round(float((curve / curve.cummax() - 1.0).min()), 6),
    }


def _terminal_net_stats(gross_returns: pd.Series, *, cost_drag: float, ppy: int = 252) -> dict[str, float | int]:
    r = gross_returns.dropna()
    if r.empty:
        return {"observations": 0}
    gross_curve = (1.0 + r).cumprod()
    net_curve = (1.0 - cost_drag) * gross_curve
    net_cum = float(net_curve.iloc[-1] - 1.0)
    ann_ret = float((1.0 + net_cum) ** (ppy / len(r)) - 1.0)
    ann_vol = float(r.std(ddof=1) * np.sqrt(ppy))
    equity = np.concatenate(([1.0], net_curve.to_numpy(dtype=float)))
    running_max = np.maximum.accumulate(equity)
    max_dd = float((equity / running_max - 1.0).min())
    return {
        "observations": int(len(r)),
        "cumulative_return": round(net_cum, 6),
        "annualized_return": round(ann_ret, 6),
        "annualized_vol": round(ann_vol, 6),
        "sharpe_ratio": round(float(ann_ret / ann_vol) if ann_vol > 0 else float("nan"), 4),
        "max_drawdown": round(max_dd, 6),
        "one_way_cost_drag_bps": round(cost_drag * 1e4, 4),
    }


def _adjusted_weights(path: Path) -> tuple[dict[str, float], float]:
    weights: dict[str, float] = {}
    cash_values: list[float] = []
    for row in read_csv(path):
        ticker = str(row.get("ticker") or row.get("Ticker") or "").strip().upper()
        if not ticker:
            continue
        raw_weight = row.get("weight")
        if raw_weight in (None, ""):
            raw_weight = row.get("Weight")
        weight = _f(raw_weight)
        if weight is None:
            raise ValueError(f"{path}:{ticker}.weight must be finite")
        if ticker == "CASH":
            cash_values.append(weight)
        elif weight > 0:
            weights[ticker] = weight
    if len(cash_values) != 1:
        raise ValueError(f"{path} must contain exactly one CASH row, found {len(cash_values)}")
    return weights, float(sum(cash_values))


def _raw_bl_weights(path: Path) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, float], float]:
    rows = read_csv(path)
    sector_by_ticker: dict[str, str] = {}
    weights: dict[str, float] = {}
    cash = 0.0
    for row in rows:
        ticker = str(row.get("Ticker") or row.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        raw_weight = row.get("Weight")
        if raw_weight in (None, ""):
            raw_weight = row.get("weight")
        weight = _f(raw_weight)
        if weight is None:
            raise ValueError(f"{path}:{ticker}.Weight must be finite")
        if ticker == "CASH":
            cash += weight
            continue
        sector_by_ticker[ticker] = str(row.get("SectorName", "")).strip()
        if weight > 0:
            weights[ticker] = weight
    return rows, sector_by_ticker, weights, cash


def _replay_series(adjusted_path: Path, summary_path: Path, returns: pd.DataFrame) -> dict[str, Any]:
    weights, cash = _adjusted_weights(adjusted_path)
    summary = _load_json(summary_path)
    cost_drag = float(summary.get("one_way_cost_base_usd", 0.0)) / float(summary.get("aum_usd", 1.0))
    held = [ticker for ticker in weights if ticker in returns.columns]
    missing = sorted(ticker for ticker in weights if ticker not in returns.columns)
    if missing:
        raise ValueError(f"held names missing from returns panel: {missing[:20]}")
    if not held:
        raise ValueError("no adjusted held names in returns panel")
    series_w = pd.Series({ticker: weights[ticker] for ticker in held})
    if abs(float(series_w.sum()) + cash - 1.0) > 1e-6:
        raise ValueError(f"adjusted weights do not close to 1: invested={float(series_w.sum())} cash={cash}")
    panel = returns[held].dropna(how="any")
    if panel.empty:
        raise ValueError("no complete-case replay window")
    gross_ret = pd.Series(panel.to_numpy() @ series_w.reindex(panel.columns).to_numpy(), index=panel.index)
    return {
        "invested_weight": round(float(series_w.sum()), 6),
        "cash_weight": round(cash, 6),
        "gross_returns": gross_ret,
        "one_way_cost_drag": cost_drag,
        "one_way_cost_bps_of_aum": summary.get("one_way_cost_bps_of_aum"),
        "round_trip_cost_bps_of_aum_DIAGNOSTIC": summary.get("round_trip_cost_bps_of_aum_DIAGNOSTIC"),
    }


def _replay_payload(series_info: dict[str, Any], common_index: pd.Index) -> dict[str, Any]:
    gross_ret = series_info["gross_returns"].loc[common_index]
    return {
        "invested_weight": series_info["invested_weight"],
        "cash_weight": series_info["cash_weight"],
        "window": {"start": str(common_index[0]), "end": str(common_index[-1]), "rows": int(len(common_index))},
        "gross": _perf_stats(gross_ret),
        "net_of_one_way_cost": _terminal_net_stats(gross_ret, cost_drag=float(series_info["one_way_cost_drag"])),
        "one_way_cost_bps_of_aum": series_info["one_way_cost_bps_of_aum"],
        "round_trip_cost_bps_of_aum_DIAGNOSTIC": series_info["round_trip_cost_bps_of_aum_DIAGNOSTIC"],
    }


def _bl_sanity_errors(generated_config: dict[str, Any]) -> list[str]:
    bl = generated_config.get("black_litterman") or {}
    errors: list[str] = []
    tau = _f(bl.get("tau"))
    delta = _f(bl.get("delta"))
    min_conf = _f(bl.get("min_confidence"))
    max_conf = _f(bl.get("max_confidence"))
    if tau is None or tau <= 0:
        errors.append(f"tau={bl.get('tau')}")
    if delta is None or delta <= 0:
        errors.append(f"delta={bl.get('delta')}")
    if min_conf is None or max_conf is None or min_conf <= 0 or max_conf < min_conf or max_conf > 1.0:
        errors.append(f"confidence_bounds=({bl.get('min_confidence')},{bl.get('max_confidence')})")
    if str(bl.get("alpha_input_mode", "")).strip().lower() != "absolute_annual":
        errors.append(f"alpha_input_mode={bl.get('alpha_input_mode')}")
    if str(bl.get("alpha_column", "")).strip() != "ExpectedAlphaAnnual":
        errors.append(f"alpha_column={bl.get('alpha_column')}")
    conf_by_rating = bl.get("confidence_by_rating") or {}
    for label in ("Strong Buy", "Buy", "Hold", "Sell", "Strong Sell", "FOREIGN"):
        value = _f(conf_by_rating.get(label))
        if value is None or value <= 0 or value > 1.0:
            errors.append(f"confidence_by_rating.{label}={conf_by_rating.get(label)}")
    try:
        pi = np.array([0.01, -0.002, 0.006], dtype=float)
        sigma = np.array([[0.05, 0.01, 0.0], [0.01, 0.04, 0.005], [0.0, 0.005, 0.03]], dtype=float)
        p_empty = np.zeros((0, 3), dtype=float)
        q_empty = np.zeros(0, dtype=float)
        omega_empty = np.zeros((0, 0), dtype=float)
        tau_value = _f_default(tau, 0.05)
        posterior = black_litterman_posterior(pi, sigma, p_empty, q_empty, omega_empty, tau_value)
        if not np.allclose(posterior, pi, atol=1e-12):
            errors.append("no_views_identity_failed")
        omega_diag = np.maximum(tau_value * np.diag(sigma) * ((1.0 / 0.5) - 1.0), 1e-12)
        if not np.all(omega_diag > 0):
            errors.append("omega_diag_not_positive")
    except Exception as exc:  # noqa: BLE001 - validation should report, not crash, on smoke-test failure.
        errors.append(f"bl_identity_smoke_error={type(exc).__name__}:{exc}")
    return errors


def main() -> int:  # noqa: C901
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    runs_root = paths.output_dir / "runs"
    run_as_of = args.as_of or latest_run_with(runs_root, "manifest.json")
    if not run_as_of:
        LOGGER.error("No sealed Stage 1 run found under %s", runs_root)
        return 1
    run_dir = runs_root / run_as_of
    bl_dir = run_dir / "blacklitterman"
    bl_costs = bl_dir / "costs"
    risk_dir = run_dir / "risk"
    base_costs = run_dir / "costs"
    opt_dir = run_dir / "optimizer"

    paths_required = {
        "bl_inputs_meta.json": bl_dir / "bl_inputs_meta.json",
        "bl_optimizer_meta.json": bl_dir / "bl_optimizer_meta.json",
        "bl_cost_meta.json": bl_costs / "bl_cost_meta.json",
        "bl_target_weights.csv": bl_dir / "bl_target_weights.csv",
        "bl_cost_adjusted_target_weights.csv": bl_costs / "bl_cost_adjusted_target_weights.csv",
        "bl_cost_summary.json": bl_costs / "bl_cost_summary.json",
        "bl_optimizer_config.yaml": bl_dir / "bl_optimizer_config.yaml",
        "bl_sector_targets_optimizer.csv": bl_dir / "bl_sector_targets_optimizer.csv",
        "returns_panel.csv": risk_dir / "returns_panel.csv",
        "optimizer_manifest.json": opt_dir / "optimizer_manifest.json",
        "target_weights.csv": opt_dir / "target_weights.csv",
        "cost_manifest.json": base_costs / "cost_manifest.json",
        "cost_adjusted_target_weights.csv": base_costs / "cost_adjusted_target_weights.csv",
        "cost_summary.json": base_costs / "cost_summary.json",
    }
    missing = [name for name, path in paths_required.items() if not path.exists()]
    if missing:
        LOGGER.error("Missing Stage 7 validation inputs: %s", missing)
        return 1

    validation_path = bl_dir / "validation" / "bl_fusion_validation.csv"
    manifest_path = bl_dir / "bl_manifest.json"
    replay_path = bl_dir / "bl_net_static_replay_metrics.json"
    if args.force:
        invalidate_dependents(run_dir, "blacklitterman")
        for path in (validation_path, manifest_path, replay_path):
            if path.exists():
                path.unlink()
    try:
        fail_if_exists([validation_path, manifest_path, replay_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    meta23 = _load_json(paths_required["bl_inputs_meta.json"])
    meta24 = _load_json(paths_required["bl_optimizer_meta.json"])
    meta25 = _load_json(paths_required["bl_cost_meta.json"])
    checks: list[dict[str, str]] = []

    def rec(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    bad23 = []
    if meta23.get("acceptance") != "PASS":
        bad23.append(f"acceptance={meta23.get('acceptance')}")
    bad23.extend(_hashes_current(meta23, bl_dir, "outputs_sha256"))
    rec("stage7_inputs_current", "PASS" if not bad23 else "FAIL",
        "23 accepted and output hashes match" if not bad23 else f"{bad23[:8]}")

    bad24 = []
    if meta24.get("acceptance") != "PASS":
        bad24.append(f"acceptance={meta24.get('acceptance')}")
    bad24.extend(_hashes_current(meta24, bl_dir, "outputs_sha256"))
    rec("stage7_optimizer_current", "PASS" if not bad24 else "FAIL",
        "24 accepted and output hashes match" if not bad24 else f"{bad24[:8]}")

    bad25 = []
    if meta25.get("acceptance") != "PASS":
        bad25.append(f"acceptance={meta25.get('acceptance')}")
    bad25.extend(_hashes_current(meta25, bl_dir, "outputs_sha256"))
    rec("stage7_cost_overlay_current", "PASS" if not bad25 else "FAIL",
        "25 accepted and output hashes match" if not bad25 else f"{bad25[:8]}")

    prod_enabled = bool(cfg_get(config, "black_litterman_fusion.enabled_in_production", False))
    rec("shadow_only_not_production", "PASS" if not prod_enabled else "FAIL",
        f"enabled_in_production={prod_enabled}")

    base_bad = []
    opt_manifest = _load_json(paths_required["optimizer_manifest.json"])
    cost_manifest = _load_json(paths_required["cost_manifest.json"])
    if opt_manifest.get("acceptance") != "PASS":
        base_bad.append(f"stage3_acceptance={opt_manifest.get('acceptance')}")
    if (opt_manifest.get("provenance_sha256") or {}).get("target_weights.csv") != sha256_file(paths_required["target_weights.csv"]):
        base_bad.append("stage3_target_hash_mismatch")
    if cost_manifest.get("acceptance") != "PASS":
        base_bad.append(f"stage4_acceptance={cost_manifest.get('acceptance')}")
    cost_prov = cost_manifest.get("provenance_sha256") or {}
    for name, path in (
        ("cost_adjusted_target_weights.csv", paths_required["cost_adjusted_target_weights.csv"]),
        ("cost_summary.json", paths_required["cost_summary.json"]),
    ):
        if cost_prov.get(name) != sha256_file(path):
            base_bad.append(f"stage4_{name}_hash_mismatch")
    rec("baseline_stage3_stage4_byte_unchanged", "PASS" if not base_bad else "FAIL",
        "Stage 3/4 baseline seals still match current artifacts" if not base_bad else f"{base_bad[:8]}")

    try:
        raw_rows, sector_by_ticker, raw_weights, raw_cash = _raw_bl_weights(paths_required["bl_target_weights.csv"])
        adjusted_weights, adjusted_cash = _adjusted_weights(paths_required["bl_cost_adjusted_target_weights.csv"])
    except ValueError as exc:
        raw_rows, sector_by_ticker, raw_weights, raw_cash = [], {}, {}, 0.0
        adjusted_weights, adjusted_cash = {}, 0.0
        rec("bl_weight_files_parse", "FAIL", str(exc))
    else:
        rec("bl_weight_files_parse", "PASS", f"raw_rows={len(raw_rows)} adjusted_assets={len(adjusted_weights)}")

    expected_cash = _f((meta24.get("regime") or {}).get("gross_exposure"))
    expected_cash = None if expected_cash is None else 1.0 - expected_cash
    raw_sum = sum(raw_weights.values()) + raw_cash
    raw_bad = []
    if abs(raw_sum - 1.0) > 1e-6:
        raw_bad.append(f"raw_sum={raw_sum:.10f}")
    if expected_cash is None:
        raw_bad.append("expected_cash_missing")
    elif abs(raw_cash - expected_cash) > 1e-6:
        raw_bad.append(f"raw_cash={raw_cash:.10f}!={expected_cash:.10f}")
    if any(w < -1e-10 for w in raw_weights.values()):
        raw_bad.append("negative_raw_weight")
    rec("bl_target_weights_valid", "PASS" if not raw_bad else "FAIL",
        f"sum={raw_sum:.10f}; cash={raw_cash:.6f}" if not raw_bad else f"{raw_bad[:8]}")

    adjusted_sum = sum(adjusted_weights.values()) + adjusted_cash
    adj_bad = []
    if abs(adjusted_sum - 1.0) > 1e-6:
        adj_bad.append(f"adjusted_sum={adjusted_sum:.10f}")
    if expected_cash is not None and adjusted_cash + 1e-8 < expected_cash:
        adj_bad.append(f"adjusted_cash={adjusted_cash:.10f}<raw_regime_cash={expected_cash:.10f}")
    if any(w < -1e-10 for w in adjusted_weights.values()):
        adj_bad.append("negative_adjusted_weight")
    rec("bl_cost_adjusted_weights_valid", "PASS" if not adj_bad else "FAIL",
        f"sum={adjusted_sum:.10f}; cash={adjusted_cash:.6f}" if not adj_bad else f"{adj_bad[:8]}")

    generated_config = yaml.safe_load(paths_required["bl_optimizer_config.yaml"].read_text(encoding="utf-8")) or {}
    sanity_bad = _bl_sanity_errors(generated_config)
    rec("bl_sanity_contract", "PASS" if not sanity_bad else "FAIL",
        "Omega confidence inputs positive; no-view posterior recovers prior; absolute annual alpha mode active"
        if not sanity_bad else f"{sanity_bad[:8]}")

    sector_band = _f_default(((generated_config.get("sector") or {}).get("sector_cap_band")), 0.03)
    sector_targets = {
        str(r.get("sector_name", "")).strip(): _f_default(r.get("target_weight"), 0.0)
        for r in read_csv(paths_required["bl_sector_targets_optimizer.csv"])
    }
    target_sectors = set(sector_targets)
    sector_bad = []
    raw_us_total = sum(w for t, w in raw_weights.items() if sector_by_ticker.get(t) in target_sectors)
    adjusted_us_total = sum(w for t, w in adjusted_weights.items() if sector_by_ticker.get(t) in target_sectors)
    # Stage 25 only removes uneconomic positions and routes their weight to cash. Relative sector
    # shares can therefore move beyond the raw optimizer band even though no sector risk was added.
    # Bound that extra movement by the total US risky weight actually removed; the strategic band
    # itself remains hard on the raw book.
    cost_deleveraging_drift = (
        max(0.0, raw_us_total - adjusted_us_total) / adjusted_us_total
        if adjusted_us_total > 0.0
        else 0.0
    )
    for label, weights in (("raw", raw_weights), ("cost_adjusted", adjusted_weights)):
        us_total = raw_us_total if label == "raw" else adjusted_us_total
        if us_total <= 0:
            sector_bad.append(f"{label}:us_total<=0")
            continue
        allowed_band = sector_band + (cost_deleveraging_drift if label == "cost_adjusted" else 0.0)
        exposure: dict[str, float] = {}
        for ticker, weight in weights.items():
            sector = sector_by_ticker.get(ticker, "")
            if sector not in target_sectors:
                continue
            exposure[sector] = exposure.get(sector, 0.0) + weight / us_total
        for sector, target in sector_targets.items():
            actual = exposure.get(sector, 0.0)
            if actual < target - allowed_band - 1e-6 or actual > target + allowed_band + 1e-6:
                sector_bad.append(
                    f"{label}:{sector} actual={actual:.4f} target={target:.4f} "
                    f"band={allowed_band:.4f}"
                )
    rec("sector_exposures_within_macro_bands", "PASS" if not sector_bad else "FAIL",
        (
            f"{len(sector_targets)} sectors within raw +/-{sector_band:.4f}; "
            f"cost_deleveraging_drift={cost_deleveraging_drift:.4f}"
        )
        if not sector_bad else f"{sector_bad[:8]}")

    foreign_cfg = (meta24.get("foreign") or {})
    fmin = _f_default(foreign_cfg.get("min_budget"), 0.0)
    fmax = _f_default(foreign_cfg.get("max_budget"), 0.0)
    foreign_weight = sum(
        _f_default(r.get("Weight"), 0.0)
        for r in raw_rows
        if str(r.get("Sleeve", "")).strip().upper() == "FOREIGN"
        or str(r.get("RegionGroup", "")).strip().upper() == "FOREIGN"
    )
    rec("foreign_budget_respected", "PASS" if fmin - 1e-6 <= foreign_weight <= fmax + 1e-6 else "FAIL",
        f"foreign_weight={foreign_weight:.6f}; allowed=[{fmin:.6f},{fmax:.6f}]")

    risk_cfg = generated_config.get("risk") or {}
    cov_path = Path(str(risk_cfg.get("covariance_csv", "")))
    cov_hash_expected = (meta23.get("inputs_sha256") or {}).get("covariance")
    cov_bad = []
    if risk_cfg.get("covariance_source") != "stage2_covariance_csv":
        cov_bad.append(f"covariance_source={risk_cfg.get('covariance_source')}")
    if str(risk_cfg.get("covariance_units", "")).lower() != "annualized":
        cov_bad.append(f"covariance_units={risk_cfg.get('covariance_units')}")
    if not cov_path.exists():
        cov_bad.append("covariance_csv_missing")
    elif cov_hash_expected and cov_hash_expected != sha256_file(cov_path):
        cov_bad.append("covariance_hash_mismatch")
    rec("stage2_covariance_injection_current", "PASS" if not cov_bad else "FAIL",
        "generated config still points at sealed Stage 2 covariance" if not cov_bad else f"{cov_bad[:8]}")

    # WARN-only diagnostic: in-sample/static, never promotion evidence.
    try:
        returns_panel = pd.read_csv(paths_required["returns_panel.csv"], index_col=0)
        bl_series = _replay_series(
            paths_required["bl_cost_adjusted_target_weights.csv"],
            paths_required["bl_cost_summary.json"],
            returns_panel,
        )
        aqr_series = _replay_series(
            paths_required["cost_adjusted_target_weights.csv"],
            paths_required["cost_summary.json"],
            returns_panel,
        )
        common_index = bl_series["gross_returns"].index.intersection(aqr_series["gross_returns"].index)
        if common_index.empty:
            raise ValueError("BL/AQR replay series have no common dates")
        bl_replay = _replay_payload(bl_series, common_index)
        aqr_replay = _replay_payload(aqr_series, common_index)
        bl_sharpe = float((bl_replay.get("net_of_one_way_cost") or {}).get("sharpe_ratio", float("nan")))
        aqr_sharpe = float((aqr_replay.get("net_of_one_way_cost") or {}).get("sharpe_ratio", float("nan")))
        delta = bl_sharpe - aqr_sharpe
        replay_payload = {
            "run_as_of": run_as_of,
            "artifact_type": "stage7_bl_vs_aqr_static_replay_diagnostic",
            "WARNING": "lookahead/in-sample diagnostic; not promotion evidence. OOS decision waits for Stage 11.",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "aqr_stage4_cost_adjusted": aqr_replay,
            "bl_stage7_cost_adjusted": bl_replay,
            "comparison_window": {
                "method": "common_complete_case_dates",
                "start": str(common_index[0]),
                "end": str(common_index[-1]),
                "rows": int(len(common_index)),
            },
            "delta": {
                "net_sharpe": round(delta, 4),
                "one_way_cost_bps": round(float(bl_replay["one_way_cost_bps_of_aum"]) - float(aqr_replay["one_way_cost_bps_of_aum"]), 4),
            },
            "inputs_sha256": {
                "bl_cost_adjusted_target_weights.csv": sha256_file(paths_required["bl_cost_adjusted_target_weights.csv"]),
                "bl_cost_summary.json": sha256_file(paths_required["bl_cost_summary.json"]),
                "cost_adjusted_target_weights.csv": sha256_file(paths_required["cost_adjusted_target_weights.csv"]),
                "cost_summary.json": sha256_file(paths_required["cost_summary.json"]),
                "returns_panel.csv": sha256_file(paths_required["returns_panel.csv"]),
            },
        }
        write_manifest(replay_path, replay_payload)
        rec("bl_vs_aqr_net_static_replay_diagnostic", "WARN",
            f"in-sample only; common_rows={len(common_index)}; BL net Sharpe={bl_sharpe:.4f}, "
            f"AQR net Sharpe={aqr_sharpe:.4f}, delta={delta:.4f}")
    except Exception as exc:  # noqa: BLE001 - diagnostic failure should be visible but non-blocking.
        replay_payload = {"error": f"{type(exc).__name__}: {exc}"}
        write_manifest(replay_path, replay_payload)
        rec("bl_vs_aqr_net_static_replay_diagnostic", "WARN", f"diagnostic unavailable: {type(exc).__name__}: {exc}")

    validation_path.parent.mkdir(parents=True, exist_ok=True)
    write_csv(validation_path, ["check", "status", "detail"], checks)
    hard = [check for check in checks if check["status"] != "WARN"]
    passed = all(check["status"] == "PASS" for check in hard)
    provenance_paths = {
        "bl_inputs_meta.json": paths_required["bl_inputs_meta.json"],
        "bl_optimizer_meta.json": paths_required["bl_optimizer_meta.json"],
        "costs/bl_cost_meta.json": paths_required["bl_cost_meta.json"],
        "bl_target_weights.csv": paths_required["bl_target_weights.csv"],
        "costs/bl_cost_adjusted_target_weights.csv": paths_required["bl_cost_adjusted_target_weights.csv"],
        "costs/bl_cost_summary.json": paths_required["bl_cost_summary.json"],
        "bl_net_static_replay_metrics.json": replay_path,
        "validation/bl_fusion_validation.csv": validation_path,
        "config.yaml": config_path,
    }
    manifest = {
        "run_as_of": run_as_of,
        "stage": "stage7_black_litterman_fusion",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "acceptance": "PASS" if passed else "FAIL",
        "shadow_only": True,
        "enabled_in_production": prod_enabled,
        "checks": checks,
        "cost_summary": _load_json(paths_required["bl_cost_summary.json"]),
        "bl_vs_aqr_diagnostic": replay_payload,
        "provenance_sha256": {name: sha256_file(path) for name, path in provenance_paths.items() if path.exists()},
        "source_sha256": {
            name: sha256_file(PACKAGE_ROOT / "blacklitterman" / name)
            for name in SOURCE_FILES
            if (PACKAGE_ROOT / "blacklitterman" / name).exists()
        },
    }
    write_manifest(manifest_path, manifest)

    for check in checks:
        LOGGER.info("[%s] %s -- %s", check["status"], check["check"], check["detail"])
    if passed:
        LOGGER.info("STAGE 7 ACCEPTANCE: PASS (as_of=%s) -> %s", run_as_of, manifest_path)
        return 0
    LOGGER.error("STAGE 7 ACCEPTANCE: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

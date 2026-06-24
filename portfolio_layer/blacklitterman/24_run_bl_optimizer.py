#!/usr/bin/env python3
"""Stage 7 - run tier1 on sealed Black-Litterman inputs and gate realized regime budgets.

This is the first solve step in Stage 7. It consumes only the run-local config and artifacts produced by
`23_build_bl_inputs.py`, runs the vendored tier1 optimizer, publishes a portfolio-layer target book, and
turns the regime gross/cash check into a hard acceptance gate.
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

import pandas as pd
import yaml


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.optimizer.tier1_portfolio_optimizer import run_end_to_end  # noqa: E402
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("run_bl_optimizer")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SOURCE_FILES = ["23_build_bl_inputs.py", "24_run_bl_optimizer.py"]
TARGET_FIELDS = [
    "Ticker", "Company", "Sleeve", "AssetType", "Rating", "LS_Book", "SectorName",
    "IndustryAggregateName", "IndustryName", "RegionGroup", "SignalScore",
    "NextEarningsDate", "EarningsDaysAhead", "EarningsDaysAheadAsOf", "EarningsFilterNote",
    "Weight", "Low", "High",
]
SUMMARY_FIELDS = [
    "Portfolio", "exp_return_ann", "vol_ann", "sharpe_ann", "objective_value",
    "net_exposure_us", "net_exposure_foreign", "cash_weight", "risky_gross_exposure",
    "portfolio_sum_weight",
]


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run Stage 7 Black-Litterman optimizer on sealed inputs.")
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


def _metric(metrics: dict[str, Any], key: str, default: float = float("nan")) -> float:
    val = _f(metrics.get(key))
    return val if val is not None else default


def _record(status_rows: list[dict[str, str]], name: str, status: str, detail: str) -> None:
    status_rows.append({"check": name, "status": status, "detail": detail})


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _verify_bl_inputs_meta(meta: dict[str, Any], out: dict[str, Path]) -> list[str]:
    bad: list[str] = []
    if meta.get("acceptance") != "PASS":
        bad.append(f"acceptance={meta.get('acceptance')}")
    expected = meta.get("outputs_sha256") or {}
    for name in (
        "bl_views.csv",
        "bl_stocks_scores_optimizer.csv",
        "bl_sector_targets_optimizer.csv",
        "bl_benchmark_weights.csv",
        "bl_foreign_budget_optimizer.csv",
        "bl_optimizer_config.yaml",
    ):
        path = out.get(name)
        if path is None or not path.exists():
            bad.append(f"{name}:missing")
            continue
        if expected.get(name) != sha256_file(path):
            bad.append(f"{name}:hash_mismatch")
    return bad


def _weights_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    for col in TARGET_FIELDS:
        if col not in df.columns:
            df[col] = ""
    frame = df[TARGET_FIELDS].fillna("")
    return [{col: row.get(col, "") for col in TARGET_FIELDS} for _, row in frame.iterrows()]


def _summary_records(metrics: dict[str, Any], portfolio: str = "LONG_ONLY") -> list[dict[str, Any]]:
    row: dict[str, Any] = {"Portfolio": portfolio}
    for field in SUMMARY_FIELDS:
        if field == "Portfolio":
            continue
        val = _f(metrics.get(field))
        row[field] = "" if val is None else val
    return [row]


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
    native_dir = bl_dir / "optimizer"
    validation_path = bl_dir / "validation" / "bl_optimizer_validation.csv"
    meta_path = bl_dir / "bl_optimizer_meta.json"
    target_path = bl_dir / "bl_target_weights.csv"
    summary_path = bl_dir / "bl_optimizer_summary.csv"
    input_meta_path = bl_dir / "bl_inputs_meta.json"
    generated_config_path = bl_dir / "bl_optimizer_config.yaml"
    out = {
        "bl_views.csv": bl_dir / "bl_views.csv",
        "bl_stocks_scores_optimizer.csv": bl_dir / "bl_stocks_scores_optimizer.csv",
        "bl_sector_targets_optimizer.csv": bl_dir / "bl_sector_targets_optimizer.csv",
        "bl_benchmark_weights.csv": bl_dir / "bl_benchmark_weights.csv",
        "bl_foreign_budget_optimizer.csv": bl_dir / "bl_foreign_budget_optimizer.csv",
        "bl_optimizer_config.yaml": generated_config_path,
    }
    native_outputs = [
        native_dir / "weights_long_only.csv",
        native_dir / "weights_long_short.csv",
        native_dir / "weights_user_portfolio.csv",
        native_dir / "optimization_results.csv",
    ]
    output_paths = [target_path, summary_path, validation_path, meta_path, *native_outputs]
    if args.force:
        for path in output_paths:
            if path.exists():
                path.unlink()
    try:
        fail_if_exists(output_paths, force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1
    for required in (input_meta_path, generated_config_path, *out.values()):
        if not required.exists():
            LOGGER.error("Missing Stage 7 input; run 23_build_bl_inputs.py first: %s", required)
            return 1
        ensure_not_prod_path(required, label="stage7 bl optimizer input")

    checks: list[dict[str, str]] = []
    input_meta = _load_json(input_meta_path)
    input_bad = _verify_bl_inputs_meta(input_meta, out)
    _record(
        checks,
        "bl_inputs_sealed_and_current",
        "PASS" if not input_bad else "FAIL",
        "23 outputs are sealed and current" if not input_bad else f"{input_bad[:8]}",
    )
    if input_bad:
        write_csv(validation_path, ["check", "status", "detail"], checks)
        LOGGER.error("Stage 7 BL optimizer refused stale/unsealed inputs")
        return 1

    generated_config = yaml.safe_load(generated_config_path.read_text(encoding="utf-8")) or {}
    cfg_text = generated_config_path.read_text(encoding="utf-8")
    cfg_bad = []
    for bad_token in ("PROD_" + "Scalper_System", "macro_serving.sqlite", "MacroLayer"):
        if bad_token in cfg_text:
            cfg_bad.append(bad_token)
    try:
        Path(str((generated_config.get("output") or {}).get("out_dir", ""))).resolve().relative_to(run_dir.resolve())
    except ValueError:
        cfg_bad.append("output.out_dir_not_run_local")
    _record(
        checks,
        "generated_config_still_run_local",
        "PASS" if not cfg_bad else "FAIL",
        "generated optimizer config is run-local and independent" if not cfg_bad else f"{cfg_bad[:8]}",
    )
    if cfg_bad:
        write_csv(validation_path, ["check", "status", "detail"], checks)
        LOGGER.error("Stage 7 BL optimizer refused unsafe generated config")
        return 1

    try:
        results = run_end_to_end(str(generated_config_path))
    except Exception as exc:  # noqa: BLE001 - turn optimizer exceptions into a sealed failed gate.
        _record(checks, "tier1_run_completed", "FAIL", f"{type(exc).__name__}: {exc}")
        write_csv(validation_path, ["check", "status", "detail"], checks)
        LOGGER.exception("Tier1 Black-Litterman solve failed")
        return 1
    long_only = results.get("LONG_ONLY")
    if long_only is None:
        _record(checks, "tier1_run_completed", "FAIL", f"result_keys={sorted(results)}")
        write_csv(validation_path, ["check", "status", "detail"], checks)
        LOGGER.error("Tier1 did not return LONG_ONLY")
        return 1
    _record(checks, "tier1_run_completed", "PASS", "tier1 returned LONG_ONLY result")

    weights_df = long_only.weights.copy()
    metrics = dict(long_only.metrics)
    write_csv(target_path, TARGET_FIELDS, _weights_records(weights_df))
    write_csv(summary_path, SUMMARY_FIELDS, _summary_records(metrics))

    missing_native = [str(path.name) for path in (native_dir / "weights_long_only.csv", native_dir / "optimization_results.csv") if not path.exists()]
    _record(
        checks,
        "tier1_native_outputs_present",
        "PASS" if not missing_native else "FAIL",
        "weights_long_only.csv and optimization_results.csv present" if not missing_native else f"missing={missing_native}",
    )

    # Hard runtime realization of the Stage 6 regime governor.
    regime = input_meta.get("regime") or {}
    foreign = input_meta.get("foreign") or {}
    expected_gross = _f(regime.get("gross_exposure"))
    expected_cash = None if expected_gross is None else max(0.0, 1.0 - expected_gross)
    expected_fmin = _f(foreign.get("min_budget")) or 0.0
    expected_fmax = _f(foreign.get("max_budget")) or 0.0
    budgets = ((generated_config.get("allocation") or {}).get("region_budgets") or {})
    us_band = budgets.get("US") or {}
    cash_band = budgets.get("CASH") or {}
    foreign_band = budgets.get("FOREIGN") or {}
    budget_bad = []
    tol = 1e-6
    cash_metric = _metric(metrics, "cash_weight")
    risky_metric = _metric(metrics, "risky_gross_exposure")
    us_metric = _metric(metrics, "net_exposure_us")
    foreign_metric = _metric(metrics, "net_exposure_foreign")
    total_metric = _metric(metrics, "portfolio_sum_weight")
    if expected_gross is None:
        budget_bad.append("expected_gross_missing")
    elif abs(risky_metric - expected_gross) > tol:
        budget_bad.append(f"risky_gross={risky_metric:.10f}!={expected_gross:.10f}")
    if expected_cash is None:
        budget_bad.append("expected_cash_missing")
    elif abs(cash_metric - expected_cash) > tol:
        budget_bad.append(f"cash={cash_metric:.10f}!={expected_cash:.10f}")
    if abs(total_metric - 1.0) > tol:
        budget_bad.append(f"portfolio_sum={total_metric:.10f}!=1")
    if not ((_f(us_band.get("min")) or 0.0) - tol <= us_metric <= (_f(us_band.get("max")) or 0.0) + tol):
        budget_bad.append(f"us={us_metric:.10f} outside [{us_band.get('min')},{us_band.get('max')}]")
    if not ((_f(foreign_band.get("min")) or 0.0) - tol <= foreign_metric <= (_f(foreign_band.get("max")) or 0.0) + tol):
        budget_bad.append(f"foreign={foreign_metric:.10f} outside [{foreign_band.get('min')},{foreign_band.get('max')}]")
    if expected_fmax < expected_fmin - tol:
        budget_bad.append(f"foreign_budget_invalid=[{expected_fmin},{expected_fmax}]")
    if not ((_f(cash_band.get("min")) or 0.0) - tol <= cash_metric <= (_f(cash_band.get("max")) or 0.0) + tol):
        budget_bad.append(f"cash={cash_metric:.10f} outside [{cash_band.get('min')},{cash_band.get('max')}]")
    if bool(metrics.get("cash_budget_relaxation_used", False)):
        budget_bad.append(f"cash_budget_relaxation_used slack={metrics.get('cash_max_slack')}")
    _record(
        checks,
        "realized_regime_cash_gross_budget",
        "PASS" if not budget_bad else "FAIL",
        (
            f"risky={risky_metric:.6f}; cash={cash_metric:.6f}; us={us_metric:.6f}; "
            f"foreign={foreign_metric:.6f}; regime_gross={expected_gross:.6f}"
            if not budget_bad and expected_gross is not None else f"{budget_bad[:8]}"
        ),
    )

    # CSV-level conservation catches any future reporting drift between metrics and emitted weights.
    weight_bad = []
    for col in ("Ticker", "Weight", "Low", "High"):
        if col not in weights_df.columns:
            weight_bad.append(f"missing_col={col}")
    if not weight_bad:
        weight_values = [_f(v) for v in weights_df["Weight"].tolist()]
        low_values = [_f(v) for v in weights_df["Low"].tolist()]
        high_values = [_f(v) for v in weights_df["High"].tolist()]
        if any(v is None for v in weight_values):
            weight_bad.append("nonfinite_weight")
        if any(v is None for v in low_values) or any(v is None for v in high_values):
            weight_bad.append("nonfinite_band")
        if not weight_bad:
            w = [float(v) for v in weight_values if v is not None]
            low = [float(v) for v in low_values if v is not None]
            high = [float(v) for v in high_values if v is not None]
            if any(v < -1e-8 for v in w):
                weight_bad.append("negative_weight")
            if any(lo > hi + 1e-8 for lo, hi in zip(low, high, strict=False)):
                weight_bad.append("low_gt_high")
            if any(wt < lo - 1e-8 or wt > hi + 1e-8 for wt, lo, hi in zip(w, low, high, strict=False)):
                weight_bad.append("weight_outside_band")
            csv_sum = sum(w)
            if abs(csv_sum - 1.0) > tol:
                weight_bad.append(f"csv_sum={csv_sum:.10f}!=1")
            csv_cash = 0.0
            for _, row in weights_df.iterrows():
                if str(row.get("Ticker", "")).strip().upper() == "CASH":
                    csv_cash += _f(row.get("Weight")) or 0.0
            if expected_cash is not None and abs(csv_cash - expected_cash) > tol:
                weight_bad.append(f"csv_cash={csv_cash:.10f}!={expected_cash:.10f}")
    _record(
        checks,
        "target_weights_conservation",
        "PASS" if not weight_bad else "FAIL",
        "long-only weights finite; bands contain weights; CSV sum/cash match regime"
        if not weight_bad else f"{weight_bad[:8]}",
    )

    # Stage 2 covariance injection must still be the runtime risk path.
    risk_cfg = generated_config.get("risk") or {}
    cov_path = Path(str(risk_cfg.get("covariance_csv", "")))
    cov_bad = []
    if risk_cfg.get("covariance_source") != "stage2_covariance_csv":
        cov_bad.append(f"covariance_source={risk_cfg.get('covariance_source')}")
    if str(risk_cfg.get("covariance_units", "")).lower() != "annualized":
        cov_bad.append(f"covariance_units={risk_cfg.get('covariance_units')}")
    if not cov_path.exists():
        cov_bad.append("covariance_csv_missing")
    else:
        expected_cov_hash = (input_meta.get("inputs_sha256") or {}).get("covariance")
        if expected_cov_hash and expected_cov_hash != sha256_file(cov_path):
            cov_bad.append("covariance_hash_mismatch")
    _record(
        checks,
        "stage2_covariance_runtime_path",
        "PASS" if not cov_bad else "FAIL",
        "tier1 config points at sealed annualized Stage 2 covariance" if not cov_bad else f"{cov_bad[:8]}",
    )

    # Ensure tier1 did not silently drop the Stage 7 stock universe.
    input_stocks = {str(r.get("Ticker", "")).strip().upper() for r in read_csv(out["bl_stocks_scores_optimizer.csv"])}
    ticker_values = weights_df["Ticker"].tolist() if "Ticker" in weights_df.columns else []
    weight_tickers = {str(t).strip().upper() for t in ticker_values}
    missing_stocks = sorted(input_stocks - weight_tickers)
    _record(
        checks,
        "stock_universe_preserved",
        "PASS" if not missing_stocks else "FAIL",
        f"all {len(input_stocks)} Stage 7 stock rows appear in target weights"
        if not missing_stocks else f"missing={missing_stocks[:10]}",
    )

    finite_bad = []
    for key in ("exp_return_ann", "vol_ann", "sharpe_ann", "objective_value", "cash_weight", "risky_gross_exposure"):
        if _f(metrics.get(key)) is None:
            finite_bad.append(key)
    _record(
        checks,
        "diagnostic_metrics_finite",
        "PASS" if not finite_bad else "FAIL",
        "tier1 summary metrics are finite" if not finite_bad else f"nonfinite={finite_bad}",
    )

    passed = all(c["status"] == "PASS" for c in checks)
    write_csv(validation_path, ["check", "status", "detail"], checks)
    meta = {
        "run_as_of": run_as_of,
        "stage": "stage7_bl_optimizer",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "shadow_only": True,
        "acceptance": "PASS" if passed else "FAIL",
        "input_meta_sha256": sha256_file(input_meta_path),
        "generated_config_sha256": sha256_file(generated_config_path),
        "regime": regime,
        "foreign": foreign,
        "metrics": metrics,
        "checks": checks,
        "outputs_sha256": {
            "bl_target_weights.csv": sha256_file(target_path),
            "bl_optimizer_summary.csv": sha256_file(summary_path),
            "validation/bl_optimizer_validation.csv": sha256_file(validation_path),
            "optimizer/weights_long_only.csv": sha256_file(native_dir / "weights_long_only.csv"),
            "optimizer/optimization_results.csv": sha256_file(native_dir / "optimization_results.csv"),
        },
        "source_sha256": {
            name: sha256_file(PACKAGE_ROOT / "blacklitterman" / name)
            for name in SOURCE_FILES
            if (PACKAGE_ROOT / "blacklitterman" / name).exists()
        },
    }
    write_manifest(meta_path, meta)

    for check in checks:
        LOGGER.info("[%s] %s -- %s", check["status"], check["check"], check["detail"])
    if passed:
        LOGGER.info(
            "STAGE 7 BL OPTIMIZER: PASS (as_of=%s, risky=%.3f, cash=%.3f) -> %s",
            run_as_of, risky_metric, cash_metric, target_path,
        )
        return 0
    LOGGER.error("STAGE 7 BL OPTIMIZER: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Nested, purged calibration for the Stage 11 tactical short engine.

Parameter selection occurs only inside each outer training window. The selected
candidate is then evaluated on the untouched next chronological block by
calling 16e in calibration-only mode. This deliberately reuses the production
research engine instead of maintaining a second simulator.
"""
from __future__ import annotations

import argparse
import itertools
import json
import logging
import math
import statistics
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.research.stage11_common import mean_t_hac  # noqa: E402


LOGGER = logging.getLogger("calibrate_tactical_short")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
REPLAY_SCRIPT = PACKAGE_ROOT / "backtest" / "16e_tactical_short_replay.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Calibrate the tactical short policy.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--panel-build", default=None)
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Run one real candidate over the last 30 signal dates; publish no calibration.",
    )
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def candidate_grid(config: dict[str, Any]) -> list[dict[str, Any]]:
    cfg = cfg_get(config, "tactical_short_calibration", {}) or {}
    holds = sorted({int(value) for value in cfg.get("max_holding_days", [1, 2, 3, 5, 7])})
    targets = sorted(
        {float(value) for value in cfg.get("net_profit_targets", [0.005, 0.01, 0.02, 0.03])}
    )
    stops = sorted({float(value) for value in cfg.get("stop_losses", [0.03, 0.05])})
    if (
        not holds
        or not targets
        or not stops
        or min(holds) < 1
        or min(targets) <= 0
        or max(targets) >= 1
        or min(stops) <= 0
        or max(stops) >= 1
    ):
        raise ValueError("Invalid tactical_short_calibration candidate grid")
    return [
        {
            "max_holding_days": hold,
            "net_profit_target": target,
            "stop_loss": stop,
        }
        for hold, target, stop in itertools.product(holds, targets, stops)
    ]


def expanding_blocks(
    dates: list[str],
    *,
    folds: int,
    initial_fraction: float,
    minimum_train_dates: int,
    purge_dates: int,
) -> list[dict[str, Any]]:
    if folds < 1 or not 0 < initial_fraction < 1 or purge_dates < 0:
        raise ValueError("Invalid fold settings")
    initial = max(minimum_train_dates, int(len(dates) * initial_fraction))
    if initial >= len(dates) - folds:
        return []
    remaining = len(dates) - initial
    block_size = max(1, remaining // folds)
    output: list[dict[str, Any]] = []
    for fold in range(folds):
        test_start = initial + fold * block_size
        test_end = len(dates) if fold == folds - 1 else min(
            len(dates), test_start + block_size
        )
        train_end = max(0, test_start - purge_dates)
        if train_end < minimum_train_dates or test_start >= test_end:
            continue
        output.append(
            {
                "fold": fold + 1,
                "train_dates": dates[:train_end],
                "test_dates": dates[test_start:test_end],
                "purged_dates": dates[train_end:test_start],
            }
        )
    return output


def _objective(results: list[dict[str, Any]], stability_penalty: float) -> float:
    values = [
        float(result["summary"]["selection_alpha_ann"])
        for result in results
        if result.get("acceptance") == "PASS"
    ]
    if len(values) != len(results) or not values:
        return -math.inf
    dispersion = statistics.pstdev(values) if len(values) > 1 else 0.0
    return statistics.fmean(values) - stability_penalty * dispersion


def _selftest() -> None:
    dates = [f"2020-01-{day:02d}" for day in range(1, 31)]
    folds = expanding_blocks(
        dates,
        folds=3,
        initial_fraction=0.5,
        minimum_train_dates=10,
        purge_dates=2,
    )
    assert len(folds) == 3
    for fold in folds:
        assert set(fold["train_dates"]).isdisjoint(fold["test_dates"])
        assert len(fold["purged_dates"]) == 2
        assert max(fold["train_dates"]) < min(fold["test_dates"])
    strong = [{"acceptance": "PASS", "summary": {"selection_alpha_ann": value}} for value in (0.1, 0.08)]
    unstable = [{"acceptance": "PASS", "summary": {"selection_alpha_ann": value}} for value in (0.2, -0.1)]
    assert _objective(strong, 1.0) > _objective(unstable, 1.0)
    print("tactical-short calibration self-test: PASS")


def _run_replay(
    *,
    config_path: Path,
    panel_build: str,
    parameters: dict[str, Any],
    signal_from: str,
    signal_to: str,
    work_dir: Path,
    label: str,
) -> dict[str, Any]:
    parameter_path = work_dir / f"{label}_parameters.json"
    result_path = work_dir / f"{label}_result.json"
    write_manifest(
        parameter_path,
        {
            "acceptance": "PASS",
            "purpose": "nested_calibration_candidate",
            "parameters": parameters,
        },
    )
    command = [
        sys.executable,
        str(REPLAY_SCRIPT),
        "--config",
        str(config_path),
        "--panel-build",
        panel_build,
        "--parameter-file",
        str(parameter_path),
        "--signal-from",
        signal_from,
        "--signal-to",
        signal_to,
        "--evaluation-json",
        str(result_path),
    ]
    completed = subprocess.run(  # noqa: S603 - fixed local script and explicit arguments
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0 or not result_path.exists():
        raise RuntimeError(
            f"16e candidate failed ({label}, rc={completed.returncode}): "
            f"{completed.stderr[-1000:]}"
        )
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    if payload.get("acceptance") != "PASS":
        raise RuntimeError(f"16e candidate did not pass ({label})")
    return payload


def _choose_candidate(
    *,
    candidates: list[dict[str, Any]],
    dates: list[str],
    config_path: Path,
    panel_build: str,
    calibration_cfg: dict[str, Any],
    work_dir: Path,
    label_prefix: str,
    grid_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    inner_folds = expanding_blocks(
        dates,
        folds=int(calibration_cfg.get("inner_folds", 3)),
        initial_fraction=float(calibration_cfg.get("inner_initial_fraction", 0.50)),
        minimum_train_dates=int(calibration_cfg.get("minimum_inner_train_dates", 126)),
        purge_dates=max(candidate["max_holding_days"] for candidate in candidates)
        + int(calibration_cfg.get("embargo_trading_days", 2)),
    )
    if not inner_folds:
        raise RuntimeError(f"No valid inner folds for {label_prefix}")
    best: dict[str, Any] | None = None
    best_objective = -math.inf
    for candidate_index, candidate in enumerate(candidates, start=1):
        results: list[dict[str, Any]] = []
        for fold in inner_folds:
            test_dates = fold["test_dates"]
            result = _run_replay(
                config_path=config_path,
                panel_build=panel_build,
                parameters=candidate,
                signal_from=test_dates[0],
                signal_to=test_dates[-1],
                work_dir=work_dir,
                label=f"{label_prefix}_c{candidate_index}_i{fold['fold']}",
            )
            results.append(result)
        objective = _objective(
            results, float(calibration_cfg.get("stability_penalty", 0.50))
        )
        grid_rows.append(
            {
                "selection_stage": label_prefix,
                "candidate": candidate_index,
                **candidate,
                "inner_folds": len(results),
                "objective": round(objective, 10),
                "mean_selection_alpha_ann": round(
                    statistics.fmean(
                        float(result["summary"]["selection_alpha_ann"])
                        for result in results
                    ),
                    10,
                ),
                "selected": 0,
            }
        )
        candidate_key = (
            objective,
            -int(candidate["max_holding_days"]),
            -float(candidate["net_profit_target"]),
            -float(candidate["stop_loss"]),
        )
        best_key = (
            best_objective,
            -int(best["max_holding_days"]) if best else -10**9,
            -float(best["net_profit_target"]) if best else -10**9,
            -float(best["stop_loss"]) if best else -10**9,
        )
        if best is None or candidate_key > best_key:
            best = dict(candidate)
            best_objective = objective
    assert best is not None
    for row in reversed(grid_rows):
        if row["selection_stage"] == label_prefix and all(
            row[key] == best[key]
            for key in ("max_holding_days", "net_profit_target", "stop_loss")
        ):
            row["selected"] = 1
            break
    return best


def main() -> int:  # noqa: C901, PLR0912, PLR0915
    configure_utc_logging()
    args = parse_args()
    if args.selftest:
        _selftest()
        return 0
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    panel_root = paths.output_dir / str(
        cfg_get(config, "calibration_panel.dir", "calibration_panel")
    )
    if args.panel_build:
        panel_dir = panel_root / args.panel_build
    else:
        builds = sorted(
            path
            for path in panel_root.iterdir()
            if path.is_dir() and (path / "calibration_panel_manifest.json").exists()
        )
        panel_dir = builds[-1] if builds else panel_root / "__missing__"
    panel_path = panel_dir / "calibration_panel.csv"
    panel_manifest_path = panel_dir / "calibration_panel_manifest.json"
    execution_manifest_path = (
        paths.output_dir
        / str(cfg_get(config, "execution_ohlcv_panel.dir", "execution_ohlcv_panel"))
        / panel_dir.name
        / "execution_ohlcv_manifest.json"
    )
    if not panel_path.exists() or not panel_manifest_path.exists() or not execution_manifest_path.exists():
        LOGGER.error("Calibration panel or matching execution OHLCV panel is missing")
        return 1
    panel_manifest = json.loads(panel_manifest_path.read_text(encoding="utf-8"))
    execution_manifest = json.loads(execution_manifest_path.read_text(encoding="utf-8"))
    if panel_manifest.get("acceptance") != "PASS" or execution_manifest.get("acceptance") != "PASS":
        LOGGER.error("Calibration or execution panel is not accepted")
        return 1

    dates = sorted(
        pd.read_csv(panel_path, usecols=lambda column: column == "as_of_date")["as_of_date"]
        .astype(str)
        .str.slice(0, 10)
        .unique()
    )
    signal_every = max(
        1, int(cfg_get(config, "tactical_short.signal_every_n_snapshots", 5))
    )
    signal_dates = dates[::signal_every]
    calibration_cfg = cfg_get(config, "tactical_short_calibration", {}) or {}
    candidates = candidate_grid(config)
    if args.smoke:
        smoke_dates = signal_dates[-30:]
        if len(smoke_dates) < 10:
            LOGGER.error("Insufficient signal dates for calibration smoke test")
            return 1
        work_root = paths.output_dir / ".tactical_short_calibration_work"
        work_root.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(dir=work_root) as temp_name:
                result = _run_replay(
                    config_path=config_path,
                    panel_build=panel_dir.name,
                    parameters=candidates[0],
                    signal_from=smoke_dates[0],
                    signal_to=smoke_dates[-1],
                    work_dir=Path(temp_name),
                    label="smoke",
                )
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            LOGGER.error("Calibration smoke failed: %s", exc)
            return 1
        LOGGER.info(
            "TACTICAL SHORT CALIBRATION SMOKE: PASS trades=%s alpha=%s parameters=%s",
            result["summary"]["trades"],
            result["summary"]["selection_alpha_ann"],
            candidates[0],
        )
        return 0
    purge_dates = max(candidate["max_holding_days"] for candidate in candidates) + int(
        calibration_cfg.get("embargo_trading_days", 2)
    )
    outer_folds = expanding_blocks(
        signal_dates,
        folds=int(calibration_cfg.get("outer_folds", 4)),
        initial_fraction=float(calibration_cfg.get("outer_initial_fraction", 0.50)),
        minimum_train_dates=int(calibration_cfg.get("minimum_outer_train_dates", 252)),
        purge_dates=purge_dates,
    )
    if len(outer_folds) < int(calibration_cfg.get("minimum_valid_outer_folds", 3)):
        LOGGER.error("Insufficient outer folds: %d", len(outer_folds))
        return 1

    out_dir = (
        paths.output_dir
        / str(calibration_cfg.get("dir", "tactical_short_calibration"))
        / panel_dir.name
    )
    grid_path = out_dir / "tactical_short_calibration_grid.csv"
    folds_path = out_dir / "tactical_short_outer_folds.csv"
    parameters_path = out_dir / "tactical_short_parameters.json"
    manifest_path = out_dir / "tactical_short_calibration_manifest.json"
    artifacts = [grid_path, folds_path, parameters_path, manifest_path]
    if args.force:
        for path in artifacts:
            if path.exists():
                path.unlink()
    try:
        fail_if_exists(artifacts, force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)

    grid_rows: list[dict[str, Any]] = []
    fold_rows: list[dict[str, Any]] = []
    outer_results: list[dict[str, Any]] = []
    selected_candidates: list[dict[str, Any]] = []
    work_root = paths.output_dir / ".tactical_short_calibration_work"
    work_root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(dir=work_root) as temp_name:
            work_dir = Path(temp_name)
            for outer in outer_folds:
                selected = _choose_candidate(
                    candidates=candidates,
                    dates=outer["train_dates"],
                    config_path=config_path,
                    panel_build=panel_dir.name,
                    calibration_cfg=calibration_cfg,
                    work_dir=work_dir,
                    label_prefix=f"outer{outer['fold']}",
                    grid_rows=grid_rows,
                )
                selected_candidates.append(selected)
                test_dates = outer["test_dates"]
                result = _run_replay(
                    config_path=config_path,
                    panel_build=panel_dir.name,
                    parameters=selected,
                    signal_from=test_dates[0],
                    signal_to=test_dates[-1],
                    work_dir=work_dir,
                    label=f"outer{outer['fold']}_test",
                )
                outer_results.append(result)
                fold_rows.append(
                    {
                        "outer_fold": outer["fold"],
                        "train_start": outer["train_dates"][0],
                        "train_end": outer["train_dates"][-1],
                        "purged_dates": len(outer["purged_dates"]),
                        "test_start": test_dates[0],
                        "test_end": test_dates[-1],
                        **selected,
                        "test_trades": result["summary"]["trades"],
                        "test_selection_alpha_ann": result["summary"][
                            "selection_alpha_ann"
                        ],
                        "test_profit_factor": result["summary"]["profit_factor"],
                    }
                )
            final_parameters = _choose_candidate(
                candidates=candidates,
                dates=signal_dates,
                config_path=config_path,
                panel_build=panel_dir.name,
                calibration_cfg=calibration_cfg,
                work_dir=work_dir,
                label_prefix="full_development",
                grid_rows=grid_rows,
            )
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        LOGGER.error("Calibration failed: %s", exc)
        return 1

    daily_selection = np.asarray(
        [
            float(value)
            for result in outer_results
            for value in result["daily_selection_returns"]
        ],
        dtype=float,
    )
    trade_returns = np.asarray(
        [
            float(value)
            for result in outer_results
            for value in result["trade_net_returns"]
        ],
        dtype=float,
    )
    daily_stress = np.asarray(
        [
            float(value)
            for result in outer_results
            for value in result["daily_stress_returns"]
        ],
        dtype=float,
    )
    years = max(len(daily_selection) / 252.0, 1e-9)
    oos_selection_ann = float(daily_selection.sum() / years)
    oos_stress_ann = float(daily_stress.sum() / years)
    _mean, _se, active_t = mean_t_hac(list(daily_selection), max_lag=5)
    gains = float(trade_returns[trade_returns > 0].sum())
    losses = abs(float(trade_returns[trade_returns < 0].sum()))
    profit_factor = gains / losses if losses > 0 else None
    sector_totals: Counter[str] = Counter()
    for result in outer_results:
        sector_totals.update(
            {
                str(key): float(value)
                for key, value in result["sector_selection_alpha"].items()
            }
        )
    positive_sectors = sum(value > 0 for value in sector_totals.values())
    promotion = cfg_get(config, "tactical_short.promotion", {}) or {}
    rejection_reasons: list[str] = []
    if len(trade_returns) < int(promotion.get("min_trades", 500)):
        rejection_reasons.append("insufficient_oos_trades")
    if oos_selection_ann <= float(promotion.get("min_selection_alpha_ann", 0.0)):
        rejection_reasons.append("oos_selection_alpha_not_positive")
    if active_t is None or active_t < float(promotion.get("min_active_t", 2.0)):
        rejection_reasons.append("oos_active_t_below_threshold")
    if profit_factor is None or profit_factor < float(
        promotion.get("min_profit_factor", 1.10)
    ):
        rejection_reasons.append("oos_profit_factor_below_threshold")
    if positive_sectors < int(promotion.get("min_positive_sectors", 4)):
        rejection_reasons.append("oos_sector_breadth_below_threshold")
    if oos_stress_ann <= float(promotion.get("min_stress_net_ann", 0.0)):
        rejection_reasons.append("oos_stress_return_not_positive")
    minimum_borrow_coverage = min(
        float(result["summary"]["borrow_actual_weight_fraction"])
        for result in outer_results
    )
    minimum_availability_coverage = min(
        float(result["summary"]["availability_covered_weight_fraction"])
        for result in outer_results
    )
    minimum_ohlcv_coverage = min(
        float(result["summary"]["candidate_execution_ohlcv_fraction"])
        for result in outer_results
    )
    if minimum_borrow_coverage < float(
        promotion.get("min_actual_borrow_weight_fraction", 0.90)
    ):
        rejection_reasons.append("oos_borrow_coverage_below_threshold")
    if minimum_availability_coverage < float(
        promotion.get("min_observed_or_fee_proxy_availability_weight_fraction", 0.90)
    ):
        rejection_reasons.append("oos_availability_coverage_below_threshold")
    if minimum_ohlcv_coverage < float(
        promotion.get("min_candidate_execution_ohlcv_fraction", 0.95)
    ):
        rejection_reasons.append("oos_execution_ohlcv_coverage_below_threshold")
    parameter_counts = Counter(
        (
            item["max_holding_days"],
            item["net_profit_target"],
            item["stop_loss"],
        )
        for item in selected_candidates
    )
    dominant_fraction = max(parameter_counts.values()) / len(selected_candidates)
    if dominant_fraction < float(calibration_cfg.get("min_parameter_fold_consistency", 0.50)):
        rejection_reasons.append("selected_parameters_unstable_across_folds")
    promotable = not rejection_reasons

    write_csv(grid_path, list(grid_rows[0]), grid_rows)
    write_csv(folds_path, list(fold_rows[0]), fold_rows)
    write_manifest(
        parameters_path,
        {
            "acceptance": "PASS",
            "promotion_status": "PROMOTABLE" if promotable else "NOT_PROMOTABLE",
            "panel_build": panel_dir.name,
            "parameters": final_parameters,
            "oos_summary": {
                "outer_folds": len(outer_results),
                "trades": len(trade_returns),
                "selection_alpha_ann": round(oos_selection_ann, 8),
                "stress_net_ann": round(oos_stress_ann, 8),
                "active_t": round(float(active_t), 6) if active_t is not None else None,
                "profit_factor": round(profit_factor, 6)
                if profit_factor is not None
                else None,
                "positive_sectors": positive_sectors,
                "parameter_fold_consistency": round(dominant_fraction, 6),
                "minimum_borrow_coverage": round(minimum_borrow_coverage, 6),
                "minimum_availability_coverage": round(
                    minimum_availability_coverage, 6
                ),
                "minimum_execution_ohlcv_coverage": round(
                    minimum_ohlcv_coverage, 6
                ),
                "rejection_reasons": rejection_reasons,
            },
        },
    )
    write_manifest(
        manifest_path,
        {
            "stage": "stage11_tactical_short_calibration",
            "generated_at": utc_now(),
            "acceptance": "PASS",
            "promotion_status": "PROMOTABLE" if promotable else "NOT_PROMOTABLE",
            "panel_build": panel_dir.name,
            "candidate_count": len(candidates),
            "outer_folds": len(outer_results),
            "purge_trading_dates": purge_dates,
            "inputs_sha256": {
                "config.yaml": sha256_file(config_path),
                "backtest/16e_tactical_short_replay.py": sha256_file(REPLAY_SCRIPT),
                "backtest/16f_calibrate_tactical_short.py": sha256_file(
                    Path(__file__).resolve()
                ),
                "calibration_panel_manifest.json": sha256_file(panel_manifest_path),
                "execution_ohlcv_manifest.json": sha256_file(execution_manifest_path),
            },
            "files": {
                grid_path.name: {"sha256": sha256_file(grid_path), "rows": len(grid_rows)},
                folds_path.name: {"sha256": sha256_file(folds_path), "rows": len(fold_rows)},
                parameters_path.name: {"sha256": sha256_file(parameters_path)},
            },
        },
    )
    LOGGER.info(
        "TACTICAL SHORT CALIBRATION: PASS / %s folds=%d candidates=%d "
        "oos_alpha=%.4f active_t=%s parameters=%s -> %s",
        "PROMOTABLE" if promotable else "NOT_PROMOTABLE",
        len(outer_results),
        len(candidates),
        oos_selection_ann,
        f"{active_t:.3f}" if active_t is not None else "NA",
        final_parameters,
        out_dir,
    )
    if rejection_reasons:
        LOGGER.info("Promotion rejections: %s", ";".join(rejection_reasons))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Nested comparison of rank-reconstitution long policies.

Holding horizons are selected separately for top-10% and top-20% entries
inside each outer training window. The untouched outer block then compares
unconditional, V1-gated, and H1-gated arms with identical structural
parameters and signal dates.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import statistics
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

from portfolio_layer.backtest.rank_reconstitution import (  # noqa: E402
    REGIME_MODES,
    circular_block_mean_ci,
    effective_sample_size,
)
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


LOGGER = logging.getLogger("calibrate_rank_reconstitution")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
REPLAY_SCRIPT = PACKAGE_ROOT / "backtest" / "16i_rank_reconstitution_long.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Calibrate rank-reconstitution arms.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--panel-build", default=None)
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


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
        test_end = (
            len(dates)
            if fold == folds - 1
            else min(len(dates), test_start + block_size)
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


def common_nonoverlap_window(
    window: list[str],
    *,
    next_window_start: str | None,
    all_dates: list[str],
    maximum_holding_days: int,
    signal_every: int,
) -> list[str]:
    """Keep signals whose longest label remains before the supplied boundary."""
    next_index = (
        all_dates.index(next_window_start)
        if next_window_start is not None
        else len(all_dates)
    )
    intervals = int(math.ceil(maximum_holding_days / signal_every))
    safe_index = next_index - intervals - 1
    if safe_index < 0:
        return []
    safe_end = all_dates[safe_index]
    return [day for day in window if day <= safe_end]


def _run(
    *,
    config_path: Path,
    panel_build: str,
    parameters: dict[str, Any],
    signal_dates: list[str],
    work_dir: Path,
    label: str,
) -> dict[str, Any]:
    if not signal_dates:
        raise ValueError(f"{label} has no signal dates")
    parameter_path = work_dir / f"{label}_parameters.json"
    output_path = work_dir / f"{label}_result.json"
    write_manifest(
        parameter_path,
        {
            "acceptance": "PASS",
            "purpose": "rank_reconstitution_nested_candidate",
            "parameters": parameters,
        },
    )
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(REPLAY_SCRIPT),
            "--config",
            str(config_path),
            "--panel-build",
            panel_build,
            "--parameter-file",
            str(parameter_path),
            "--signal-from",
            signal_dates[0],
            "--signal-to",
            signal_dates[-1],
            "--evaluation-json",
            str(output_path),
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0 or not output_path.exists():
        raise RuntimeError(
            f"16i failed ({label}, rc={completed.returncode}): "
            f"{completed.stderr[-1200:]}"
        )
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    if payload.get("acceptance") != "PASS":
        raise RuntimeError(f"16i rejected {label}")
    return payload


def _objective(results: list[dict[str, Any]], stability_penalty: float) -> float:
    if not results:
        return -math.inf
    values = [float(result["summary"]["selection_alpha_ann"]) for result in results]
    return statistics.fmean(values) - stability_penalty * (
        statistics.pstdev(values) if len(values) > 1 else 0.0
    )


def choose_horizon(
    *,
    entry_fraction: float,
    horizons: list[int],
    dates: list[str],
    config_path: Path,
    panel_build: str,
    calibration: dict[str, Any],
    purge_dates: int,
    signal_every: int,
    work_dir: Path,
    prefix: str,
    grid_rows: list[dict[str, Any]],
) -> int:
    inner = expanding_blocks(
        dates,
        folds=int(calibration.get("inner_folds", 3)),
        initial_fraction=float(calibration.get("inner_initial_fraction", 0.50)),
        minimum_train_dates=int(calibration.get("minimum_inner_train_dates", 126)),
        purge_dates=purge_dates,
    )
    raw_windows = [fold["test_dates"] for fold in inner] if inner else [dates]
    common_windows = []
    for index, raw in enumerate(raw_windows):
        # Inner folds tune entirely inside outer training. Earlier validation
        # windows may overlap one another; the final one is endpoint-trimmed so
        # its longest label cannot cross into the outer test.
        trimmed = (
            list(raw)
            if index + 1 < len(raw_windows)
            else common_nonoverlap_window(
                raw,
                next_window_start=None,
                all_dates=dates,
                maximum_holding_days=max(horizons),
                signal_every=signal_every,
            )
        )
        if trimmed:
            common_windows.append(trimmed)
    if not common_windows:
        common_windows = [dates]
    best_horizon = horizons[0]
    best_objective = -math.inf
    parallel_replays = max(1, int(calibration.get("parallel_replays", 2)))
    results_by_horizon: dict[int, dict[int, dict[str, Any]]] = {
        horizon: {} for horizon in horizons
    }
    with ThreadPoolExecutor(max_workers=parallel_replays) as executor:
        futures = {
            executor.submit(
                _run,
                config_path=config_path,
                panel_build=panel_build,
                parameters={
                    "entry_fraction": entry_fraction,
                    "exit_fraction": entry_fraction + 0.10,
                    # Keep every candidate on the same evaluation calendar.
                    # The schedule controls position exits; the global horizon
                    # only extends the replay with cash after shorter holds end.
                    "max_holding_days": max(horizons),
                    "max_holding_days_by_signal": {
                        signal_date: horizon for signal_date in window
                    },
                    "regime_mode": "unconditional",
                },
                signal_dates=window,
                work_dir=work_dir,
                label=(
                    f"{prefix}_e{int(entry_fraction * 100)}_h{horizon}_w{idx}"
                ),
            ): (horizon, idx)
            for horizon in horizons
            for idx, window in enumerate(common_windows, start=1)
        }
        for future in as_completed(futures):
            horizon, idx = futures[future]
            results_by_horizon[horizon][idx] = future.result()
    for horizon in horizons:
        results = [
            results_by_horizon[horizon][idx]
            for idx in sorted(results_by_horizon[horizon])
        ]
        objective = _objective(
            results, float(calibration.get("stability_penalty", 0.50))
        )
        grid_rows.append(
            {
                "selection_stage": prefix,
                "entry_fraction": entry_fraction,
                "max_holding_days": horizon,
                "common_validation_windows": len(results),
                "objective": objective,
                "mean_selection_alpha_ann": statistics.fmean(
                    float(result["summary"]["selection_alpha_ann"])
                    for result in results
                ),
                "selected": 0,
            }
        )
        if (objective, -horizon) > (best_objective, -best_horizon):
            best_objective = objective
            best_horizon = horizon
    for row in reversed(grid_rows):
        if (
            row["selection_stage"] == prefix
            and float(row["entry_fraction"]) == entry_fraction
            and int(row["max_holding_days"]) == best_horizon
        ):
            row["selected"] = 1
            break
    return best_horizon


def _selftest() -> None:
    dates = [f"2020-{month:02d}-01" for month in range(1, 13)]
    trimmed = common_nonoverlap_window(
        dates[2:8],
        next_window_start=dates[8],
        all_dates=dates,
        maximum_holding_days=10,
        signal_every=5,
    )
    assert trimmed == dates[2:6]
    endpoint_trimmed = common_nonoverlap_window(
        dates[6:],
        next_window_start=None,
        all_dates=dates,
        maximum_holding_days=10,
        signal_every=5,
    )
    assert endpoint_trimmed == dates[6:10]
    folds = expanding_blocks(
        [f"2020-01-{day:02d}" for day in range(1, 31)],
        folds=3,
        initial_fraction=0.5,
        minimum_train_dates=10,
        purge_dates=2,
    )
    assert len(folds) == 3
    assert all(len(fold["purged_dates"]) == 2 for fold in folds)
    assert _objective(
        [
            {"summary": {"selection_alpha_ann": 0.10}},
            {"summary": {"selection_alpha_ann": 0.08}},
        ],
        0.5,
    ) > _objective(
        [
            {"summary": {"selection_alpha_ann": 0.20}},
            {"summary": {"selection_alpha_ann": -0.10}},
        ],
        0.5,
    )
    print("rank-reconstitution calibration self-test: PASS")


def main() -> int:  # noqa: C901, PLR0912, PLR0915
    configure_utc_logging()
    args = parse_args()
    if args.selftest:
        _selftest()
        return 0
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    calibration = cfg_get(config, "rank_reconstitution_calibration", {}) or {}
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
    if not panel_path.exists():
        LOGGER.error("Calibration panel is missing")
        return 1
    dates = sorted(
        pd.read_csv(panel_path, usecols=lambda column: column == "as_of_date")[
            "as_of_date"
        ]
        .astype(str)
        .str.slice(0, 10)
        .unique()
    )
    entries = sorted(
        {float(value) for value in calibration.get("entry_fractions", [0.10, 0.20])}
    )
    horizons = sorted(
        {int(value) for value in calibration.get("max_holding_days", [15, 30, 63, 126, 252])}
    )
    modes = [str(value) for value in calibration.get("regime_modes", list(REGIME_MODES))]
    if (
        entries != [0.10, 0.20]
        or any(entry <= 0 or entry >= 0.9 for entry in entries)
        or not horizons
        or any(mode not in REGIME_MODES for mode in modes)
    ):
        LOGGER.error("Invalid pre-registered rank calibration grid")
        return 1
    signal_every = max(
        1,
        int(
            cfg_get(
                config,
                "rank_reconstitution_long.signal_every_n_snapshots",
                5,
            )
        ),
    )
    observable = dates[: -max(horizons)] if len(dates) > max(horizons) else []
    signal_dates = observable[::signal_every]
    purge_dates = int(math.ceil(max(horizons) / signal_every)) + int(
        calibration.get("embargo_signal_dates", 2)
    )
    if args.smoke:
        signal_dates = signal_dates[-40:]
        horizons = [horizons[0]]
    outer = expanding_blocks(
        signal_dates,
        folds=int(calibration.get("outer_folds", 4)),
        initial_fraction=float(calibration.get("outer_initial_fraction", 0.50)),
        minimum_train_dates=int(calibration.get("minimum_outer_train_dates", 126)),
        purge_dates=purge_dates,
    )
    if args.smoke:
        outer = [
            {
                "fold": 1,
                "train_dates": signal_dates[:20],
                "test_dates": signal_dates[20:],
                "purged_dates": [],
            }
        ]
    if len(outer) < (1 if args.smoke else int(calibration.get("minimum_valid_outer_folds", 3))):
        LOGGER.error("Insufficient outer folds: %d", len(outer))
        return 1

    out_dir = (
        paths.output_dir
        / str(calibration.get("dir", "rank_reconstitution_calibration"))
        / panel_dir.name
    )
    grid_path = out_dir / "rank_reconstitution_grid.csv"
    folds_path = out_dir / "rank_reconstitution_outer_arms.csv"
    evidence_path = out_dir / "rank_reconstitution_evidence.csv"
    parameters_path = out_dir / "rank_reconstitution_parameters.json"
    manifest_path = out_dir / "rank_reconstitution_calibration_manifest.json"
    artifacts = [grid_path, folds_path, evidence_path, parameters_path, manifest_path]
    if not args.smoke:
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
    continuous_results: dict[tuple[float, str], dict[str, Any]] = {}
    selected_horizons: dict[float, list[int]] = {entry: [] for entry in entries}
    horizon_schedule: dict[float, dict[str, int]] = {
        entry: {} for entry in entries
    }
    outer_windows: list[dict[str, Any]] = []
    replay_inputs_sha256: dict[str, str] = {}
    work_root = paths.output_dir / ".rank_reconstitution_work"
    work_root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(dir=work_root) as temp_name:
            work_dir = Path(temp_name)
            for fold in outer:
                selected = {
                    entry: choose_horizon(
                        entry_fraction=entry,
                        horizons=horizons,
                        dates=fold["train_dates"],
                        config_path=config_path,
                        panel_build=panel_dir.name,
                        calibration=calibration,
                        purge_dates=purge_dates,
                        signal_every=signal_every,
                        work_dir=work_dir,
                        prefix=f"outer{fold['fold']}",
                        grid_rows=grid_rows,
                    )
                    for entry in entries
                }
                for entry, horizon in selected.items():
                    selected_horizons[entry].append(horizon)
                # Outer folds have disjoint signal dates. Their long-horizon
                # outcomes can overlap; HAC and block-bootstrap inference handles
                # that dependence instead of deleting an entire test fold.
                common_test = list(fold["test_dates"])
                if not common_test:
                    raise RuntimeError(f"Outer fold {fold['fold']} has no common test dates")
                for entry in entries:
                    horizon_schedule[entry].update(
                        dict.fromkeys(common_test, selected[entry])
                    )
                outer_windows.append(
                    {
                        "outer_fold": fold["fold"],
                        "test_dates": common_test,
                        "selected": selected,
                    }
                )
            final_horizons = {
                entry: choose_horizon(
                    entry_fraction=entry,
                    horizons=horizons,
                    dates=signal_dates,
                    config_path=config_path,
                    panel_build=panel_dir.name,
                    calibration=calibration,
                    purge_dates=purge_dates,
                    signal_every=signal_every,
                    work_dir=work_dir,
                    prefix="full_development",
                    grid_rows=grid_rows,
                )
                for entry in entries
            }
            parallel_replays = max(
                1, int(calibration.get("parallel_replays", 2))
            )
            with ThreadPoolExecutor(max_workers=parallel_replays) as executor:
                futures = {
                    executor.submit(
                        _run,
                        config_path=config_path,
                        panel_build=panel_dir.name,
                        parameters={
                            "entry_fraction": entry,
                            "exit_fraction": entry + 0.10,
                            "max_holding_days": max(horizons),
                            "max_holding_days_by_signal": horizon_schedule[entry],
                            "regime_mode": mode,
                        },
                        signal_dates=sorted(horizon_schedule[entry]),
                        work_dir=work_dir,
                        label=f"continuous_e{int(entry*100)}_{mode}",
                    ): (entry, mode)
                    for entry in entries
                    for mode in modes
                }
                for future in as_completed(futures):
                    continuous_results[futures[future]] = future.result()
            common_input_sets = {
                json.dumps(
                    {
                        str(key): str(value)
                        for key, value in result["inputs_sha256"].items()
                        if key != "parameter_artifact.json"
                    },
                    sort_keys=True,
                )
                for result in continuous_results.values()
            }
            if len(common_input_sets) != 1:
                raise RuntimeError(
                    "Continuous arms did not consume identical sealed inputs"
                )
            replay_inputs_sha256 = json.loads(common_input_sets.pop())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        LOGGER.error("Rank calibration failed: %s", exc)
        return 1

    for entry in entries:
        for mode in modes:
            result = continuous_results[(entry, mode)]
            daily_rows = list(
                zip(
                    result["daily_dates"],
                    result["daily_selection_returns"],
                    strict=True,
                )
            )
            trade_records = result["trade_records"]
            for index, window in enumerate(outer_windows):
                start = str(window["test_dates"][0])
                next_start = (
                    str(outer_windows[index + 1]["test_dates"][0])
                    if index + 1 < len(outer_windows)
                    else None
                )
                segment = [
                    float(value)
                    for day, value in daily_rows
                    if day >= start and (next_start is None or day < next_start)
                ]
                test_dates = set(window["test_dates"])
                fold_trades = [
                    row
                    for row in trade_records
                    if str(row["signal_date"]) in test_dates
                ]
                sector_selection: dict[str, float] = {}
                sector_equal_weight: dict[str, float] = {}
                for row in fold_trades:
                    pipeline = str(row["source_pipeline"])
                    sector_selection[pipeline] = sector_selection.get(
                        pipeline, 0.0
                    ) + float(row["selection_alpha_net"])
                    sector_equal_weight[pipeline] = sector_equal_weight.get(
                        pipeline, 0.0
                    ) + float(
                        row["selection_alpha_equal_weight_net"]
                    )
                years = len(segment) / 252.0
                _mean, _se, fold_t = mean_t_hac(
                    segment,
                    max_lag=int(window["selected"][entry]),
                )
                fold_rows.append(
                    {
                        "outer_fold": window["outer_fold"],
                        "entry_fraction": entry,
                        "exit_fraction": entry + 0.10,
                        "regime_mode": mode,
                        "max_holding_days": window["selected"][entry],
                        "test_start": window["test_dates"][0],
                        "test_end": window["test_dates"][-1],
                        "trades": len(fold_trades),
                        "selection_alpha_ann": (
                            sum(segment) / years if years > 0 else 0.0
                        ),
                        "selection_alpha_equal_weight_ann": (
                            sum(sector_equal_weight.values()) / years
                            if years > 0
                            else 0.0
                        ),
                        "active_t": fold_t,
                        "minimum_execution_coverage": result["summary"][
                            "minimum_execution_coverage"
                        ],
                        "continuous_oos_stream": 1,
                        "positions_may_cross_fold_boundary": 1,
                        "sector_selection_alpha_json": json.dumps(
                            sector_selection, sort_keys=True
                        ),
                        "sector_equal_weight_selection_alpha_json": json.dumps(
                            sector_equal_weight, sort_keys=True
                        ),
                    }
                )

    evidence_rows: list[dict[str, Any]] = []
    confidence = float(calibration.get("bootstrap_confidence", 0.90))
    reps = int(calibration.get("bootstrap_replications", 1000))
    seed = int(calibration.get("bootstrap_seed", 1729))
    min_years = float(calibration.get("minimum_oos_years", 3.0))
    min_positive_sectors = int(calibration.get("minimum_positive_sectors", 4))
    min_coverage = float(calibration.get("minimum_execution_coverage", 0.95))
    for entry in entries:
        for mode in modes:
            result = continuous_results[(entry, mode)]
            daily = [
                float(value) for value in result["daily_selection_returns"]
            ]
            stress = [float(value) for value in result["daily_stress_returns"]]
            years = len(daily) / 252.0
            selected_max = max(selected_horizons[entry])
            _mean, _se, active_t = mean_t_hac(daily, max_lag=selected_max)
            low, high = circular_block_mean_ci(
                daily,
                block_length=selected_max,
                confidence=confidence,
                replications=reps,
                seed=seed,
            )
            sector_totals = {
                str(key): float(value)
                for key, value in result["sector_selection_alpha"].items()
            }
            sector_equal_weight_totals = {
                str(key): float(value)
                for key, value in result[
                    "sector_equal_weight_selection_alpha"
                ].items()
            }
            fold_alpha = [
                float(row["selection_alpha_ann"])
                for row in fold_rows
                if float(row["entry_fraction"]) == entry
                and str(row["regime_mode"]) == mode
            ]
            coverage = float(result["summary"]["minimum_execution_coverage"])
            regime_summary = result["regime_signal_summary"]
            gate_disagreements = int(
                regime_summary["h1_v1_gate_disagreement_dates"]
            )
            reasons = []
            if years < min_years:
                reasons.append("insufficient_oos_calendar_span")
            if low is None or low <= 0:
                reasons.append("block_bootstrap_lower_bound_not_positive")
            if sum(value > 0 for value in fold_alpha) < math.ceil(2 * len(fold_alpha) / 3):
                reasons.append("insufficient_positive_outer_folds")
            if sum(value > 0 for value in sector_totals.values()) < min_positive_sectors:
                reasons.append("insufficient_positive_sectors")
            if coverage < min_coverage:
                reasons.append("execution_coverage_below_threshold")
            if sum(stress) / max(years, 1e-9) <= 0:
                reasons.append("stress_return_not_positive")
            if mode == "h1_gate":
                if gate_disagreements == 0:
                    reasons.append("h1_gate_no_incremental_decisions")
                reasons.append("h1_source_shadow_only")
            evidence_rows.append(
                {
                    "entry_fraction": entry,
                    "exit_fraction": entry + 0.10,
                    "regime_mode": mode,
                    "outer_folds": len(outer_windows),
                    "oos_years": years,
                    "trades": int(result["summary"]["trades"]),
                    "selection_alpha_ann": sum(daily) / max(years, 1e-9),
                    "selection_alpha_equal_weight_ann": sum(
                        sector_equal_weight_totals.values()
                    )
                    / max(years, 1e-9),
                    "active_t": active_t,
                    "block_bootstrap_ci_low_ann": low * 252 if low is not None else "",
                    "block_bootstrap_ci_high_ann": high * 252 if high is not None else "",
                    "effective_daily_observations": effective_sample_size(
                        daily, max_lag=selected_max
                    ),
                    "positive_outer_folds": sum(value > 0 for value in fold_alpha),
                    "positive_sectors": sum(
                        value > 0 for value in sector_totals.values()
                    ),
                    "minimum_execution_coverage": coverage,
                    "h1_v1_label_disagreement_dates": int(
                        regime_summary["h1_v1_label_disagreement_dates"]
                    ),
                    "h1_v1_gate_disagreement_dates": gate_disagreements,
                    "continuous_oos_stream": 1,
                    "positions_may_cross_fold_boundary": 1,
                    "evidence_pass": int(not reasons),
                    "promotion_status": "NOT_PROMOTABLE",
                    "rejection_reasons": ";".join(
                        reasons
                        or ["retrospective_candidate_requires_prospective_confirmation"]
                    ),
                }
            )

    if args.smoke:
        LOGGER.info(
            "RANK CALIBRATION SMOKE PASS arms=%d; no sealed calibration artifacts written",
            len(evidence_rows),
        )
        return 0

    write_csv(grid_path, list(grid_rows[0]), grid_rows)
    write_csv(folds_path, list(fold_rows[0]), fold_rows)
    write_csv(evidence_path, list(evidence_rows[0]), evidence_rows)
    write_manifest(
        parameters_path,
        {
            "acceptance": "PASS",
            "promotion_status": "NOT_PROMOTABLE",
            "diagnostic_only": True,
            "parameters_by_entry_fraction": {
                str(entry): {
                    "entry_fraction": entry,
                    "exit_fraction": entry + 0.10,
                    "max_holding_days": final_horizons[entry],
                }
                for entry in entries
            },
        },
    )
    write_manifest(
        manifest_path,
        {
            "stage": "stage11_rank_reconstitution_calibration",
            "generated_at": utc_now(),
            "acceptance": "PASS",
            "promotion_status": "NOT_PROMOTABLE",
            "diagnostic_only": True,
            "panel_build": panel_dir.name,
            "entry_fractions": entries,
            "regime_modes": modes,
            "horizon_candidates": horizons,
            "outer_folds": len(outer),
            "purge_signal_dates": purge_dates,
            "inputs_sha256": {
                "config.yaml": sha256_file(config_path),
                "backtest/rank_reconstitution.py": sha256_file(
                    PACKAGE_ROOT / "backtest" / "rank_reconstitution.py"
                ),
                "backtest/16i_rank_reconstitution_long.py": sha256_file(REPLAY_SCRIPT),
                "backtest/16j_calibrate_rank_reconstitution.py": sha256_file(
                    Path(__file__).resolve()
                ),
                **{
                    f"replay:{key}": value
                    for key, value in sorted(replay_inputs_sha256.items())
                },
            },
            "files": {
                grid_path.name: {
                    "sha256": sha256_file(grid_path),
                    "rows": len(grid_rows),
                },
                folds_path.name: {
                    "sha256": sha256_file(folds_path),
                    "rows": len(fold_rows),
                },
                evidence_path.name: {
                    "sha256": sha256_file(evidence_path),
                    "rows": len(evidence_rows),
                },
                parameters_path.name: {"sha256": sha256_file(parameters_path)},
            },
        },
    )
    LOGGER.info(
        "RANK CALIBRATION PASS / NOT_PROMOTABLE folds=%d arms=%d -> %s",
        len(outer),
        len(evidence_rows),
        out_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

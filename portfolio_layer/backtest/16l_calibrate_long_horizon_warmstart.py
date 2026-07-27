#!/usr/bin/env python3
"""Warm-started nested confirmation of the Stage 11 tactical-long horizon.

The existing 16h campaign starts each test replay from cash and liquidates it
after the requested signal window. That is a valid standalone policy replay,
but it dilutes long-horizon fold evidence with artificial ramp-up and wind-down
periods. This campaign runs every horizon continuously over the development
window and slices only the daily P&L inside each held-out interval. Positions
may therefore cross fold boundaries exactly as they would in a live book.

This is development evidence only. A historical PASS is reported as
AWAITING_PROSPECTIVE_CONFIRMATION and can never change production.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import statistics
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

from portfolio_layer.backtest.short_costs import snapshot_sqlite_database  # noqa: E402
from portfolio_layer.backtest.walkforward_common import hac_lag_for_hold  # noqa: E402
from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    manifest_accepts,
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.research.stage11_common import (  # noqa: E402
    independent_windows,
    mean_t_hac,
)


LOGGER = logging.getLogger("calibrate_long_horizon_warmstart")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_CAMPAIGN = (
    PACKAGE_ROOT / "research" / "LONG_HORIZON_WARM_START_CAMPAIGN.yaml"
)
REPLAY_SCRIPT = PACKAGE_ROOT / "backtest" / "16g_tactical_long_replay.py"


@dataclass(frozen=True)
class Replay:
    horizon: int
    dates: tuple[str, ...]
    selection: tuple[float, ...]
    stress: tuple[float, ...]
    summary: dict[str, Any]

    def selection_by_date(self) -> dict[str, float]:
        return dict(zip(self.dates, self.selection, strict=True))

    def stress_by_date(self) -> dict[str, float]:
        return dict(zip(self.dates, self.stress, strict=True))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Warm-start nested confirmation for tactical-long horizons."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--campaign", type=Path, default=DEFAULT_CAMPAIGN)
    parser.add_argument("--panel-build", default=None)
    parser.add_argument("--output-suffix", default="")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def expanding_blocks(
    dates: list[str],
    *,
    folds: int,
    initial_fraction: float,
    minimum_train_dates: int,
    embargo_dates: int,
) -> list[dict[str, Any]]:
    """Chronological expanding folds with an unscored gap before each test."""
    if (
        folds < 1
        or not 0.0 < initial_fraction < 1.0
        or minimum_train_dates < 1
        or embargo_dates < 0
    ):
        raise ValueError("Invalid expanding-fold policy")
    initial = max(minimum_train_dates, int(len(dates) * initial_fraction))
    if initial + embargo_dates >= len(dates) - folds:
        return []
    remaining = len(dates) - initial - embargo_dates
    block_size = max(1, remaining // folds)
    output: list[dict[str, Any]] = []
    for fold in range(folds):
        test_start = initial + embargo_dates + fold * block_size
        test_end = (
            len(dates)
            if fold == folds - 1
            else min(len(dates), test_start + block_size)
        )
        train_end = test_start - embargo_dates
        if train_end < minimum_train_dates or test_start >= test_end:
            continue
        output.append(
            {
                "fold": fold + 1,
                "train_dates": dates[:train_end],
                "embargo_dates": dates[train_end:test_start],
                "test_dates": dates[test_start:test_end],
            }
        )
    return output


def daily_interval(
    block_dates: list[str],
    *,
    all_signal_dates: list[str],
    daily_dates: tuple[str, ...],
) -> tuple[str, str] | None:
    """Map a signal-date block to its D+1-open through next-signal-close P&L."""
    if not block_dates or not daily_dates:
        return None
    signal_pos = {day: index for index, day in enumerate(all_signal_dates)}
    try:
        last_pos = signal_pos[block_dates[-1]]
    except KeyError as exc:
        raise ValueError(f"Unknown signal date in test block: {exc}") from exc
    first_signal = block_dates[0]
    daily_after_start = [day for day in daily_dates if day > first_signal]
    if not daily_after_start:
        return None
    start = daily_after_start[0]
    next_signal = (
        all_signal_dates[last_pos + 1]
        if last_pos + 1 < len(all_signal_dates)
        else daily_dates[-1]
    )
    eligible = [day for day in daily_dates if start <= day <= next_signal]
    return (eligible[0], eligible[-1]) if eligible else None


def slice_values(
    replay: Replay,
    interval: tuple[str, str],
    *,
    stress: bool = False,
) -> list[float]:
    values = replay.stress_by_date() if stress else replay.selection_by_date()
    start, end = interval
    return [values[day] for day in sorted(values) if start <= day <= end]


def annualized_sum(values: list[float]) -> float:
    if not values:
        raise ValueError("Cannot annualize an empty return slice")
    return float(sum(values) * 252.0 / len(values))


def choose_horizon(
    replays: dict[int, Replay],
    *,
    training_signal_dates: list[str],
    all_signal_dates: list[str],
    campaign: dict[str, Any],
) -> tuple[int | None, list[dict[str, Any]], str]:
    """Choose from strictly prior inner test intervals; ties produce no choice."""
    evaluation = campaign["evaluation"]
    folds = expanding_blocks(
        training_signal_dates,
        folds=int(evaluation["inner_folds"]),
        initial_fraction=float(evaluation["inner_initial_fraction"]),
        minimum_train_dates=int(evaluation["minimum_inner_train_signal_dates"]),
        embargo_dates=int(evaluation["embargo_signal_dates"]),
    )
    required_folds = int(evaluation["minimum_valid_inner_folds"])
    required_windows = int(evaluation["minimum_objective_windows"])
    penalty = float(evaluation["stability_penalty"])
    rows: list[dict[str, Any]] = []
    objectives: list[tuple[float, int]] = []
    for horizon, replay in sorted(replays.items()):
        window_values: list[float] = []
        for fold in folds:
            interval = daily_interval(
                list(fold["test_dates"]),
                all_signal_dates=all_signal_dates,
                daily_dates=replay.dates,
            )
            if interval is None:
                continue
            values = slice_values(replay, interval)
            if values:
                window_values.append(annualized_sum(values))
        feasible = (
            len(folds) >= required_folds
            and len(window_values) >= required_windows
        )
        objective: float | None = None
        if feasible:
            objective = statistics.fmean(window_values) - penalty * (
                statistics.pstdev(window_values) if len(window_values) > 1 else 0.0
            )
            objectives.append((objective, horizon))
        rows.append(
            {
                "horizon_days": horizon,
                "inner_folds": len(folds),
                "evaluation_windows": len(window_values),
                "feasible": int(feasible),
                "mean_selection_alpha_ann": (
                    statistics.fmean(window_values) if window_values else ""
                ),
                "selection_alpha_dispersion": (
                    statistics.pstdev(window_values)
                    if len(window_values) > 1
                    else (0.0 if window_values else "")
                ),
                "objective": "" if objective is None else objective,
                "selected": 0,
            }
        )
    if not objectives:
        return None, rows, "no_feasible_candidate"
    best_value = max(value for value, _horizon in objectives)
    tolerance = float(evaluation["objective_tie_tolerance"])
    tied = [
        horizon
        for value, horizon in objectives
        if abs(value - best_value) <= tolerance
    ]
    if len(tied) != 1:
        return None, rows, "objective_tie_no_selection"
    selected = tied[0]
    for row in rows:
        row["selected"] = int(row["horizon_days"] == selected)
    return selected, rows, "selected"


def _parse_replay(payload: dict[str, Any], expected_horizon: int) -> Replay:
    if payload.get("acceptance") != "PASS":
        raise ValueError("Replay artifact is not accepted")
    parameters = payload.get("parameters") or {}
    if int(parameters.get("max_holding_days", -1)) != expected_horizon:
        raise ValueError("Replay horizon does not match request")
    dates = tuple(str(value) for value in payload["daily_selection_dates"])
    selection = tuple(float(value) for value in payload["daily_selection_returns"])
    stress = tuple(float(value) for value in payload["daily_stress_returns"])
    if (
        not dates
        or len(dates) != len(selection)
        or len(dates) != len(stress)
        or len(set(dates)) != len(dates)
        or list(dates) != sorted(dates)
        or any(not math.isfinite(value) for value in selection + stress)
    ):
        raise ValueError("Replay daily series is malformed")
    return Replay(
        horizon=expected_horizon,
        dates=dates,
        selection=selection,
        stress=stress,
        summary=dict(payload["summary"]),
    )


def _run_replay(
    *,
    config_path: Path,
    panel_build: str,
    horizon: int,
    pipelines: str,
    work_dir: Path,
    market_db: Path,
    market_db_sha256: str,
    content_key: str,
) -> Replay:
    label = f"h{horizon}_{pipelines.replace(',', '_')}"
    parameter_path = work_dir / f"{label}_parameters.json"
    result_path = work_dir / f"{label}_result.json"
    if parameter_path.exists() and result_path.exists():
        try:
            parameters = json.loads(parameter_path.read_text(encoding="utf-8"))
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            if (
                parameters.get("content_key") == content_key
                and parameters.get("parameters") == {"max_holding_days": horizon}
                and parameters.get("pipelines") == pipelines
                and payload.get("source_sha256") == sha256_file(REPLAY_SCRIPT)
                and payload.get("market_positioning_db_sha256")
                == market_db_sha256
            ):
                LOGGER.info("Warm-start replay cache hit: horizon=%d %s", horizon, pipelines)
                return _parse_replay(payload, horizon)
        except (KeyError, OSError, UnicodeError, ValueError, json.JSONDecodeError):
            pass
    write_manifest(
        parameter_path,
        {
            "acceptance": "PASS",
            "purpose": "warm_start_continuous_horizon_replay",
            "parameters": {"max_holding_days": horizon},
            "pipelines": pipelines,
            "content_key": content_key,
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
        "--evaluation-json",
        str(result_path),
        "--market-positioning-db",
        str(market_db),
        "--pipelines",
        pipelines,
    ]
    completed = subprocess.run(  # noqa: S603 - fixed local producer
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=1800,
    )
    if completed.returncode != 0 or not result_path.exists():
        raise RuntimeError(
            f"16g warm-start replay failed (h={horizon}, pipelines={pipelines}, "
            f"rc={completed.returncode}): {completed.stderr[-2000:]}"
        )
    return _parse_replay(
        json.loads(result_path.read_text(encoding="utf-8")),
        horizon,
    )


def _selftest() -> None:
    signals = [f"2020-01-{day:02d}" for day in range(1, 21)]
    daily = tuple(f"2020-01-{day:02d}" for day in range(1, 31))
    interval = daily_interval(
        signals[10:13],
        all_signal_dates=signals,
        daily_dates=daily,
    )
    assert interval == ("2020-01-12", "2020-01-14"), interval
    folds = expanding_blocks(
        signals,
        folds=3,
        initial_fraction=0.40,
        minimum_train_dates=5,
        embargo_dates=2,
    )
    assert len(folds) == 3
    assert all(
        set(fold["train_dates"]).isdisjoint(fold["test_dates"])
        and set(fold["embargo_dates"]).isdisjoint(fold["test_dates"])
        for fold in folds
    )
    base_dates = tuple(f"2020-02-{day:02d}" for day in range(1, 29))
    positive = Replay(
        126,
        base_dates,
        tuple([0.001] * len(base_dates)),
        tuple([0.0005] * len(base_dates)),
        {},
    )
    flat = Replay(
        189,
        base_dates,
        tuple([0.0] * len(base_dates)),
        tuple([0.0] * len(base_dates)),
        {},
    )
    synthetic_campaign = {
        "evaluation": {
            "inner_folds": 3,
            "inner_initial_fraction": 0.40,
            "minimum_inner_train_signal_dates": 5,
            "minimum_valid_inner_folds": 2,
            "minimum_objective_windows": 2,
            "embargo_signal_dates": 1,
            "stability_penalty": 0.5,
            "objective_tie_tolerance": 1e-9,
        }
    }
    synthetic_signals = [f"2020-02-{day:02d}" for day in range(1, 21)]
    selected, _rows, reason = choose_horizon(
        {126: positive, 189: flat},
        training_signal_dates=synthetic_signals,
        all_signal_dates=synthetic_signals,
        campaign=synthetic_campaign,
    )
    assert selected == 126 and reason == "selected", (selected, reason)
    tied, _rows, tie_reason = choose_horizon(
        {126: positive, 189: positive},
        training_signal_dates=synthetic_signals,
        all_signal_dates=synthetic_signals,
        campaign=synthetic_campaign,
    )
    assert tied is None and tie_reason == "objective_tie_no_selection"
    print("long-horizon warm-start self-test: PASS")


def main() -> int:  # noqa: C901, PLR0912, PLR0915
    configure_utc_logging()
    args = parse_args()
    if args.selftest:
        _selftest()
        return 0
    config_path = args.config.expanduser().resolve()
    campaign_path = args.campaign.expanduser().resolve()
    config = load_yaml(config_path)
    campaign = load_yaml(campaign_path)
    paths = resolve_runtime_paths(config, config_path)
    panel_build = str(args.panel_build or campaign["panel_build"])
    panel_dir = (
        paths.output_dir
        / str(cfg_get(config, "calibration_panel.dir", "calibration_panel"))
        / panel_build
    )
    panel_path = panel_dir / "calibration_panel.csv"
    panel_manifest_path = panel_dir / "calibration_panel_manifest.json"
    execution_manifest_path = (
        paths.output_dir
        / str(cfg_get(config, "execution_ohlcv_panel.dir", "execution_ohlcv_panel"))
        / panel_build
        / "execution_ohlcv_manifest.json"
    )
    required = [panel_path, panel_manifest_path, execution_manifest_path]
    if any(not path.exists() for path in required):
        LOGGER.error("Missing calibration/execution input: %s", [p for p in required if not p.exists()])
        return 1
    panel_manifest = json.loads(panel_manifest_path.read_text(encoding="utf-8"))
    execution_manifest = json.loads(execution_manifest_path.read_text(encoding="utf-8"))
    if not manifest_accepts(panel_manifest) or not manifest_accepts(execution_manifest):
        LOGGER.error("Calibration or execution manifest is not accepted")
        return 1
    if str(campaign["evaluation"]["mode"]) != "continuous_warm_start_test_window_daily_pnl":
        LOGGER.error("Campaign evaluation mode is not the supported frozen policy")
        return 1

    horizons = sorted({int(value) for value in campaign["horizon_grid_days"]})
    if len(horizons) < 2 or min(horizons) < 1:
        LOGGER.error("Frozen horizon grid is invalid")
        return 1
    pipeline_names = [str(value) for value in campaign["pipelines"]]
    if not pipeline_names or len(set(pipeline_names)) != len(pipeline_names):
        LOGGER.error("Frozen pipeline list is invalid")
        return 1
    panel_dates = sorted(
        pd.read_csv(
            panel_path,
            usecols=lambda column: column == "as_of_date",
        )["as_of_date"]
        .astype(str)
        .str.slice(0, 10)
        .unique()
    )
    signal_every = int(campaign["execution_contract"]["signal_every_n_snapshots"])
    observable_dates = (
        panel_dates[: -max(horizons)]
        if len(panel_dates) > max(horizons)
        else []
    )
    signal_dates = observable_dates[::signal_every]
    if len(signal_dates) < 100:
        LOGGER.error("Insufficient outcome-complete signal dates: %d", len(signal_dates))
        return 1

    evaluation = campaign["evaluation"]
    outer_folds = expanding_blocks(
        signal_dates,
        folds=int(evaluation["outer_folds"]),
        initial_fraction=float(evaluation["outer_initial_fraction"]),
        minimum_train_dates=int(evaluation["minimum_outer_train_signal_dates"]),
        embargo_dates=int(evaluation["embargo_signal_dates"]),
    )
    if len(outer_folds) != int(evaluation["outer_folds"]):
        LOGGER.error("Required outer folds did not materialize: %d", len(outer_folds))
        return 1

    output_dir = (
        paths.output_dir
        / "long_horizon_warm_start"
        / f"{panel_build}{str(args.output_suffix or '').strip()}"
    )
    grid_path = output_dir / "long_horizon_candidate_grid.csv"
    fold_path = output_dir / "long_horizon_outer_folds.csv"
    decision_path = output_dir / "long_horizon_decision.json"
    manifest_path = output_dir / "long_horizon_manifest.json"
    outputs = [grid_path, fold_path, decision_path, manifest_path]
    if args.force:
        for path in outputs:
            if path.exists():
                path.unlink()
    try:
        fail_if_exists(outputs, force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    work_root = paths.output_dir / ".long_horizon_warm_start_work"
    try:
        market_db_source = resolve_path(
            str(
                cfg_get(
                    config,
                    "tactical_long.long_costs.market_positioning_db_path",
                )
            ),
            base_dir=config_path.parent,
        )
        market_db, market_db_sha = snapshot_sqlite_database(
            market_db_source,
            work_root / "db_snapshots",
        )
    except (OSError, ValueError) as exc:
        LOGGER.error("Cannot snapshot market-positioning database: %s", exc)
        return 1

    source_inputs = {
        "config.yaml": sha256_file(config_path),
        "LONG_HORIZON_WARM_START_CAMPAIGN.yaml": sha256_file(campaign_path),
        "calibration_panel.csv": sha256_file(panel_path),
        "calibration_panel_manifest.json": sha256_file(panel_manifest_path),
        "execution_ohlcv_manifest.json": sha256_file(execution_manifest_path),
        "market_positioning_snapshot.sqlite": market_db_sha,
        "backtest/16g_tactical_long_replay.py": sha256_file(REPLAY_SCRIPT),
        "backtest/16l_calibrate_long_horizon_warmstart.py": sha256_file(
            Path(__file__).resolve()
        ),
        "backtest/short_costs.py": sha256_file(
            PACKAGE_ROOT / "backtest" / "short_costs.py"
        ),
        "backtest/walkforward_common.py": sha256_file(
            PACKAGE_ROOT / "backtest" / "walkforward_common.py"
        ),
        "research/stage11_common.py": sha256_file(
            PACKAGE_ROOT / "research" / "stage11_common.py"
        ),
    }
    content_key = hashlib.sha256(
        json.dumps(source_inputs, sort_keys=True).encode("utf-8")
    ).hexdigest()[:20]
    work_dir = work_root / panel_build / content_key
    work_dir.mkdir(parents=True, exist_ok=True)

    all_pipelines = ",".join(pipeline_names)
    try:
        replays = {
            horizon: _run_replay(
                config_path=config_path,
                panel_build=panel_build,
                horizon=horizon,
                pipelines=all_pipelines,
                work_dir=work_dir,
                market_db=market_db,
                market_db_sha256=market_db_sha,
                content_key=content_key,
            )
            for horizon in horizons
        }
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        LOGGER.error("Continuous replay failed: %s", exc)
        return 1
    common_daily_dates = set.intersection(
        *(set(replay.dates) for replay in replays.values())
    )
    if not common_daily_dates:
        LOGGER.error("Horizon replays have no common daily calendar")
        return 1

    grid_rows: list[dict[str, Any]] = []
    fold_rows: list[dict[str, Any]] = []
    selected_by_fold: list[int] = []
    selected_fold_pairs: list[tuple[dict[str, Any], int]] = []
    oos_selection_by_date: dict[str, float] = {}
    oos_stress_by_date: dict[str, float] = {}
    no_selection_reasons: dict[str, str] = {}
    for fold in outer_folds:
        selected, rows, reason = choose_horizon(
            replays,
            training_signal_dates=list(fold["train_dates"]),
            all_signal_dates=signal_dates,
            campaign=campaign,
        )
        for row in rows:
            grid_rows.append({"selection_stage": f"outer{fold['fold']}", **row})
        if selected is None:
            no_selection_reasons[str(fold["fold"])] = reason
            fold_rows.append(
                {
                    "outer_fold": fold["fold"],
                    "train_start": fold["train_dates"][0],
                    "train_end": fold["train_dates"][-1],
                    "test_start": fold["test_dates"][0],
                    "test_end": fold["test_dates"][-1],
                    "selected_horizon_days": "",
                    "selection_status": reason,
                    "test_daily_start": "",
                    "test_daily_end": "",
                    "test_days": 0,
                    "test_selection_alpha_ann": "",
                    "test_stress_ann": "",
                }
            )
            continue
        interval = daily_interval(
            list(fold["test_dates"]),
            all_signal_dates=signal_dates,
            daily_dates=replays[selected].dates,
        )
        if interval is None:
            no_selection_reasons[str(fold["fold"])] = "empty_daily_test_interval"
            continue
        selected_by_fold.append(selected)
        selected_fold_pairs.append((fold, selected))
        selection_map = replays[selected].selection_by_date()
        stress_map = replays[selected].stress_by_date()
        interval_dates = [
            day
            for day in sorted(common_daily_dates)
            if interval[0] <= day <= interval[1]
        ]
        if set(interval_dates) & set(oos_selection_by_date):
            LOGGER.error("Outer test intervals overlap")
            return 1
        for day in interval_dates:
            oos_selection_by_date[day] = selection_map[day]
            oos_stress_by_date[day] = stress_map[day]
        fold_rows.append(
            {
                "outer_fold": fold["fold"],
                "train_start": fold["train_dates"][0],
                "train_end": fold["train_dates"][-1],
                "test_start": fold["test_dates"][0],
                "test_end": fold["test_dates"][-1],
                "selected_horizon_days": selected,
                "selection_status": reason,
                "test_daily_start": interval[0],
                "test_daily_end": interval[1],
                "test_days": len(interval_dates),
                "test_selection_alpha_ann": annualized_sum(
                    [selection_map[day] for day in interval_dates]
                ),
                "test_stress_ann": annualized_sum(
                    [stress_map[day] for day in interval_dates]
                ),
            }
        )

    final_horizon, final_rows, final_reason = choose_horizon(
        replays,
        training_signal_dates=signal_dates,
        all_signal_dates=signal_dates,
        campaign=campaign,
    )
    for row in final_rows:
        grid_rows.append({"selection_stage": "full_development", **row})

    promotion = campaign["promotion"]
    rejection_reasons: list[str] = []
    if len(selected_by_fold) != int(evaluation["outer_folds"]):
        rejection_reasons.append("not_all_outer_folds_selected")
    oos_dates = sorted(oos_selection_by_date)
    oos_selection = [oos_selection_by_date[day] for day in oos_dates]
    oos_stress = [oos_stress_by_date[day] for day in oos_dates]
    oos_selection_ann = annualized_sum(oos_selection) if oos_selection else None
    oos_stress_ann = annualized_sum(oos_stress) if oos_stress else None
    hac_lag = (
        hac_lag_for_hold(max(selected_by_fold))
        if selected_by_fold
        else hac_lag_for_hold(max(horizons))
    )
    _mean, _se, active_t = (
        mean_t_hac(oos_selection, max_lag=hac_lag)
        if oos_selection
        else (None, None, None)
    )
    positive_folds = sum(
        float(row["test_selection_alpha_ann"]) > 0.0
        for row in fold_rows
        if row["test_selection_alpha_ann"] != ""
    )
    if (
        oos_selection_ann is None
        or oos_selection_ann
        <= float(promotion["minimum_net_selection_alpha_ann"])
    ):
        rejection_reasons.append("oos_selection_alpha_not_positive")
    if active_t is None or active_t < float(promotion["minimum_active_t_hac"]):
        rejection_reasons.append("oos_active_t_below_threshold")
    if positive_folds < int(promotion["minimum_positive_outer_folds"]):
        rejection_reasons.append("insufficient_positive_outer_folds")
    if (
        oos_stress_ann is None
        or oos_stress_ann <= float(promotion["minimum_stress_net_ann"])
    ):
        rejection_reasons.append("oos_stress_not_positive")
    selected_at_boundary = bool(
        final_horizon is not None
        and final_horizon in (min(horizons), max(horizons))
    )
    if bool(promotion["boundary_selection_blocks"]) and selected_at_boundary:
        rejection_reasons.append("full_development_selection_at_grid_boundary")
    windows = (
        independent_windows(oos_dates, max(selected_by_fold))
        if oos_dates and selected_by_fold
        else 0
    )
    if windows < int(promotion["minimum_independent_windows"]):
        rejection_reasons.append("insufficient_independent_windows")
    minimum_ohlcv = min(
        float(replays[horizon].summary["candidate_execution_ohlcv_fraction"])
        for horizon in set(selected_by_fold)
    ) if selected_by_fold else 0.0
    if minimum_ohlcv < float(promotion["minimum_execution_ohlcv_fraction"]):
        rejection_reasons.append("execution_ohlcv_coverage_below_threshold")
    minimum_exact_spread = min(
        float(replays[horizon].summary["spread_exact_weight_fraction"])
        for horizon in set(selected_by_fold)
    ) if selected_by_fold else 0.0
    if minimum_exact_spread < float(
        promotion["minimum_historical_exact_spread_weight_fraction"]
    ):
        rejection_reasons.append("exact_spread_coverage_below_threshold")

    sector_totals: dict[str, float] = {}
    if selected_by_fold:
        try:
            sector_replays = {
                (pipeline, horizon): _run_replay(
                    config_path=config_path,
                    panel_build=panel_build,
                    horizon=horizon,
                    pipelines=pipeline,
                    work_dir=work_dir,
                    market_db=market_db,
                    market_db_sha256=market_db_sha,
                    content_key=content_key,
                )
                for pipeline in pipeline_names
                for horizon in sorted(set(selected_by_fold))
            }
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            LOGGER.error("Sector-breadth replay failed: %s", exc)
            return 1
        for pipeline in pipeline_names:
            total = 0.0
            for fold, selected in selected_fold_pairs:
                interval = daily_interval(
                    list(fold["test_dates"]),
                    all_signal_dates=signal_dates,
                    daily_dates=sector_replays[(pipeline, selected)].dates,
                )
                if interval is None:
                    raise RuntimeError("Sector replay has an empty held-out interval")
                total += sum(slice_values(sector_replays[(pipeline, selected)], interval))
            sector_totals[pipeline] = total
    positive_sectors = sum(value > 0.0 for value in sector_totals.values())
    if positive_sectors < int(promotion["minimum_positive_sectors"]):
        rejection_reasons.append("oos_sector_breadth_below_threshold")

    historical_pass = not rejection_reasons
    decision_status = (
        "AWAITING_PROSPECTIVE_CONFIRMATION"
        if historical_pass
        else "NOT_PROMOTABLE"
    )
    decision = {
        "acceptance": "PASS",
        "campaign_id": campaign["campaign_id"],
        "panel_build": panel_build,
        "evidence_class": "development_only",
        "decision_status": decision_status,
        "promotable": 0,
        "historical_gates_pass": int(historical_pass),
        "production_unchanged": True,
        "final_development_horizon_days": final_horizon,
        "final_selection_status": final_reason,
        "selected_at_grid_boundary": int(selected_at_boundary),
        "outer_selected_horizons": selected_by_fold,
        "parameter_counts": dict(Counter(selected_by_fold)),
        "oos_days": len(oos_dates),
        "oos_selection_alpha_ann": oos_selection_ann,
        "oos_active_t_hac": active_t,
        "oos_active_t_hac_lag_days": hac_lag,
        "oos_stress_ann": oos_stress_ann,
        "positive_outer_folds": positive_folds,
        "positive_sectors": positive_sectors,
        "sector_oos_selection_sum": sector_totals,
        "independent_windows": windows,
        "minimum_execution_ohlcv_fraction": minimum_ohlcv,
        "minimum_exact_spread_weight_fraction": minimum_exact_spread,
        "rejection_reasons": rejection_reasons,
        "no_selection_reasons": no_selection_reasons,
        "prospective_confirmation_required": True,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    grid_fields = [
        "selection_stage",
        "horizon_days",
        "inner_folds",
        "evaluation_windows",
        "feasible",
        "mean_selection_alpha_ann",
        "selection_alpha_dispersion",
        "objective",
        "selected",
    ]
    fold_fields = [
        "outer_fold",
        "train_start",
        "train_end",
        "test_start",
        "test_end",
        "selected_horizon_days",
        "selection_status",
        "test_daily_start",
        "test_daily_end",
        "test_days",
        "test_selection_alpha_ann",
        "test_stress_ann",
    ]
    write_csv(grid_path, grid_fields, grid_rows)
    write_csv(fold_path, fold_fields, fold_rows)
    write_manifest(decision_path, decision)
    write_manifest(
        manifest_path,
        {
            "acceptance": "PASS",
            "stage": "stage11_long_horizon_warm_start_confirmation",
            "campaign_id": campaign["campaign_id"],
            "generated_at": utc_now(),
            "panel_build": panel_build,
            "content_key": content_key,
            "inputs_sha256": source_inputs,
            "outputs_sha256": {
                grid_path.name: sha256_file(grid_path),
                fold_path.name: sha256_file(fold_path),
                decision_path.name: sha256_file(decision_path),
            },
            "decision_status": decision_status,
            "promotable": 0,
            "historical_gates_pass": int(historical_pass),
            "prospective_confirmation_required": True,
        },
    )
    LOGGER.info(
        "LONG HORIZON WARM-START: %s alpha=%s t=%s folds=%s sectors=%d reasons=%s",
        decision_status,
        f"{oos_selection_ann:.6f}" if oos_selection_ann is not None else "NA",
        f"{active_t:.3f}" if active_t is not None else "NA",
        selected_by_fold,
        positive_sectors,
        rejection_reasons or "none",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

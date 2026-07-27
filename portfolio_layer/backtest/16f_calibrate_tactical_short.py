#!/usr/bin/env python3
"""Nested, purged calibration for the Stage 11 tactical short engine.

Parameter selection occurs only inside each outer training window. The selected
candidate is then evaluated on the untouched next chronological block by
calling 16e in calibration-only mode. This deliberately reuses the production
research engine instead of maintaining a second simulator.

2026-07-25 grid and selection redesign (tactical long/short diagnostic):

  * one- and two-session holds are gone. Inside a 1-session horizon neither the
    profit target nor the stop can bind, so entire target columns produced
    identical objectives and the old minimum-value tie-breaker "selected" a
    parameter that no evidence had chosen.
  * profit targets must clear the estimated round-trip cost times a configured
    multiple. Infeasible combinations are rejected at grid construction with an
    explicit log line instead of being silently evaluated.
  * ties no longer select. An objective tie means the objective did not
    discriminate; the honest response is no trade, so the fold is marked
    unstable and contributes no OOS evidence.
  * a selection sitting on a grid corner is flagged (selected_at_grid_boundary)
    and blocks promotion: the optimum may lie outside the searched box.
  * at least five outer folds and two inner folds must materialize, or the run
    fails closed. Fold-consistency numbers are suppressed below five folds.
  * the HAC lag follows the selected horizon instead of a hardcoded 5.

2026-07-26 fixes (both diagnosed on the tactical LONG twin, 16h, and applied here because the
selection and cache code is the same shape on both sides):

  * MINIMUM OBJECTIVE WINDOWS. ``mean - 0.5 * pstdev`` had no floor on the number of evaluable
    windows. With a single window ``pstdev`` is identically zero, so such a candidate pays no
    stability penalty and outranks candidates measured three times on a raw, unaveraged mean.
    Here every candidate currently shares one inner-fold set whose size is already gated by
    ``min_inner_folds``, so the hole is LATENT rather than live -- it opens the moment
    ``min_inner_folds`` is lowered to 1 or per-candidate windows are introduced. Candidates below
    ``minimum_objective_windows`` (config, default 2) are marked insufficient_evaluation_windows
    and EXCLUDED from selection; if all of them are, the stage selects nothing and says why.
  * CANDIDATE CACHE NEVER HIT. The cache compared the 3-key candidate dict against the fully
    resolved 9-key parameter dict 16e writes into its result manifest, which is always False, so
    every candidate was recomputed on every rerun. The comparison is now the shared subset test in
    walkforward_common.cached_replay_matches, guarded by the content-hash cache key.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import logging
import math
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.backtest.short_costs import snapshot_sqlite_database  # noqa: E402
from portfolio_layer.backtest.walkforward_common import (  # noqa: E402
    cached_replay_matches,
    hac_lag_for_hold,
)
from portfolio_layer.research.stage11_common import mean_t_hac  # noqa: E402


LOGGER = logging.getLogger("calibrate_tactical_short")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
REPLAY_SCRIPT = PACKAGE_ROOT / "backtest" / "16e_tactical_short_replay.py"

CANDIDATE_KEYS = ("max_holding_days", "net_profit_target", "stop_loss")
DEFAULT_MINIMUM_OBJECTIVE_WINDOWS = 2
REASON_OBJECTIVE_TIE = "objective_tie_no_selection"
REASON_NO_EVALUABLE_CANDIDATE = "no_evaluable_candidate"
REASON_ALL_BELOW_MIN_WINDOWS = "all_candidates_below_minimum_objective_windows"
REASON_NO_EVALUABLE_AFTER_WINDOW_EXCLUSION = "no_evaluable_candidate_after_window_exclusion"
WINDOW_BLOCKED_REASONS = frozenset(
    {REASON_ALL_BELOW_MIN_WINDOWS, REASON_NO_EVALUABLE_AFTER_WINDOW_EXCLUSION}
)
INSUFFICIENT_WINDOWS = "insufficient_evaluation_windows"
# Keys the pre-registered liquid-tier block may override in tactical_short_calibration. All three
# are wall-clock/retry guards against machine load and cannot move a number in the result. Anything
# statistical -- the grid, the fold layout, the objective, the penalties, the tolerances -- is
# deliberately absent, and a liquid block naming a key outside this set is a hard failure.
LIQUID_INFRASTRUCTURE_OVERRIDE_KEYS = frozenset(
    {
        "candidate_timeout_seconds",
        "candidate_max_attempts",
        "candidate_retry_delay_seconds",
    }
)
# Keys the liquid block owns for its own purposes; they are not calibration overrides.
LIQUID_OWN_KEYS = frozenset(
    {
        "min_short_entry_price",
        "min_median_dollar_volume_20d",
        "replay_dir",
        "calibration_dir",
    }
)


def liquid_calibration_overrides(liquid_cfg: dict[str, Any]) -> dict[str, Any]:
    """Infrastructure-only overrides the liquid block contributes to ``tactical_short_calibration``.

    FAIL CLOSED. A liquid-tier run that could quietly reshape the grid, the folds or the objective
    would not be the same experiment as the sealed full-universe run, so any key that is neither one
    of the block's own gates nor one of the three wall-clock guards is rejected outright.
    """
    unknown = sorted(set(liquid_cfg) - LIQUID_OWN_KEYS - LIQUID_INFRASTRUCTURE_OVERRIDE_KEYS)
    if unknown:
        raise ValueError(
            "tactical_short_liquid may only carry its own gates plus "
            f"{sorted(LIQUID_INFRASTRUCTURE_OVERRIDE_KEYS)}; refusing unknown keys {unknown}"
        )
    return {
        key: value
        for key, value in liquid_cfg.items()
        if key in LIQUID_INFRASTRUCTURE_OVERRIDE_KEYS
    }


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
    parser.add_argument(
        "--output-suffix",
        default="",
        help="Append to the panel-build output directory so a sealed run is never overwritten.",
    )
    parser.add_argument(
        "--liquid-tier",
        action="store_true",
        help=(
            "Pre-registered liquid-tier variant (LIQUID_SHORT_TEST.md). Forwards --liquid-tier to "
            "every 16e candidate, folds the flag into the candidate cache key, and publishes to "
            "the sibling tactical_short_liquid_calibration directory. The selection machinery, "
            "the candidate grid and the promotion gates are unchanged. OFF by default."
        ),
    )
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def minimum_feasible_target(cfg: dict[str, Any]) -> float:
    """Smallest net profit target that can clear the modelled round trip."""
    round_trip = float(cfg.get("estimated_round_trip_cost_bps", 60.0)) / 1e4
    multiple = float(cfg.get("target_cost_multiple", 1.5))
    if round_trip < 0 or multiple <= 0:
        raise ValueError("Invalid round-trip cost feasibility policy")
    return round_trip * multiple


def candidate_grid(config: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build the candidate grid, returning ``(feasible, rejected)``.

    A profit target below ``estimated_round_trip_cost_bps * target_cost_multiple`` cannot be
    reached net of execution and borrow. Evaluating it produces an "optimum" made entirely of
    cost, so it is rejected here and reported rather than silently searched.
    """
    cfg = cfg_get(config, "tactical_short_calibration", {}) or {}
    holds = sorted({int(value) for value in cfg.get("max_holding_days", [3, 5, 7, 10, 15])})
    targets = sorted(
        {float(value) for value in cfg.get("net_profit_targets", [0.005, 0.01, 0.02, 0.03])}
    )
    stops = sorted({float(value) for value in cfg.get("stop_losses", [0.05, 0.08])})
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
    floor = minimum_feasible_target(cfg)
    feasible: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for hold, target, stop in itertools.product(holds, targets, stops):
        candidate = {
            "max_holding_days": hold,
            "net_profit_target": target,
            "stop_loss": stop,
        }
        if target < floor:
            rejected.append(
                {**candidate, "reason": "target_below_round_trip_cost", "min_feasible_target": floor}
            )
            continue
        feasible.append(candidate)
    if not feasible:
        raise ValueError(
            f"No tactical_short candidate clears the round-trip cost floor of {floor:.4f}"
        )
    return feasible, rejected


def grid_boundary_axes(
    selected: dict[str, Any], candidates: list[dict[str, Any]]
) -> list[str]:
    """Axes on which ``selected`` sits at a grid corner.

    A parameter chosen at the edge of the searched box is not an interior optimum: the true best
    value may lie outside the grid entirely. Such a selection is flagged and blocks promotion.
    """
    axes: list[str] = []
    for key in ("max_holding_days", "net_profit_target", "stop_loss"):
        values = sorted({float(candidate[key]) for candidate in candidates})
        if len(values) < 2:
            continue
        chosen = float(selected[key])
        if chosen <= values[0] or chosen >= values[-1]:
            axes.append(key)
    return axes


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


def _objective(
    results: list[dict[str, Any]],
    stability_penalty: float,
    *,
    minimum_windows: int = DEFAULT_MINIMUM_OBJECTIVE_WINDOWS,
) -> float:
    """``mean - penalty * pstdev`` over the evaluable windows, or ``-inf`` below the window floor.

    With a single window ``pstdev`` is identically zero, so a candidate evaluated once pays NO
    stability penalty while a candidate evaluated three times pays the full one. Returning -inf
    below the floor makes the under-measured candidate unselectable rather than best.
    """
    values = [
        float(result["summary"]["selection_alpha_ann"])
        for result in results
        if result.get("acceptance") == "PASS"
    ]
    if len(values) != len(results) or len(values) < max(1, int(minimum_windows)):
        return -math.inf
    dispersion = statistics.pstdev(values) if len(values) > 1 else 0.0
    return statistics.fmean(values) - stability_penalty * dispersion


def _grid_row(
    *,
    selection_stage: str,
    candidate_index: int,
    candidate: dict[str, Any],
    evaluation_windows: int,
    min_evaluation_windows: int,
    selection_method: str,
    feasible: bool,
    objective: float | None = None,
    mean_selection_alpha_ann: float | None = None,
) -> dict[str, Any]:
    """One grid CSV row. Every row carries the same columns so the header is never truncated."""
    return {
        "selection_stage": selection_stage,
        "candidate": candidate_index,
        **candidate,
        "inner_folds": evaluation_windows,
        "evaluation_windows": evaluation_windows,
        "min_evaluation_windows": min_evaluation_windows,
        "selection_method": selection_method,
        "feasible": int(bool(feasible)),
        "objective": (
            round(objective, 10)
            if objective is not None and math.isfinite(objective)
            else ""
        ),
        "mean_selection_alpha_ann": (
            round(mean_selection_alpha_ann, 10)
            if mean_selection_alpha_ann is not None
            else ""
        ),
        "selected": 0,
        "selection_unstable_tie": 0,
    }


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
    # --- grid feasibility (2026-07-25 fix 7): a target under the round trip cannot be reached ---
    config = {
        "tactical_short_calibration": {
            "max_holding_days": [3, 5, 7, 10, 15],
            "net_profit_targets": [0.005, 0.01, 0.02, 0.03],
            "stop_losses": [0.05, 0.08],
            "estimated_round_trip_cost_bps": 60.0,
            "target_cost_multiple": 1.5,
        }
    }
    feasible, rejected = candidate_grid(config)
    assert abs(minimum_feasible_target(config["tactical_short_calibration"]) - 0.009) < 1e-12
    assert len(feasible) == 5 * 3 * 2, len(feasible)
    assert len(rejected) == 5 * 1 * 2, len(rejected)
    assert all(item["net_profit_target"] >= 0.009 for item in feasible)
    assert all(item["reason"] == "target_below_round_trip_cost" for item in rejected)
    assert min(item["max_holding_days"] for item in feasible) == 3
    infeasible_config = {
        "tactical_short_calibration": {
            "max_holding_days": [3],
            "net_profit_targets": [0.001],
            "stop_losses": [0.05],
            "estimated_round_trip_cost_bps": 60.0,
        }
    }
    try:
        candidate_grid(infeasible_config)
    except ValueError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("a wholly infeasible grid must be rejected at load")
    # --- boundary detection (2026-07-25 fix 7) ---
    assert grid_boundary_axes(
        {"max_holding_days": 3, "net_profit_target": 0.02, "stop_loss": 0.05}, feasible
    ) == ["max_holding_days", "stop_loss"]
    assert grid_boundary_axes(
        {"max_holding_days": 7, "net_profit_target": 0.02, "stop_loss": 0.05}, feasible
    ) == ["stop_loss"]
    assert grid_boundary_axes(
        {"max_holding_days": 15, "net_profit_target": 0.03, "stop_loss": 0.08}, feasible
    ) == ["max_holding_days", "net_profit_target", "stop_loss"]
    # --- HAC lag follows the horizon (2026-07-25 fix 10) ---
    assert hac_lag_for_hold(3) == 5 and hac_lag_for_hold(15) == 5 and hac_lag_for_hold(40) == 8
    _selftest_minimum_objective_windows()
    _selftest_candidate_cache()
    _selftest_liquid_overrides()
    print("tactical-short calibration self-test: PASS")


def _stub_result(alpha: float) -> dict[str, Any]:
    return {"acceptance": "PASS", "summary": {"selection_alpha_ann": alpha}}


def _selftest_minimum_objective_windows() -> None:
    """BUG 1 (2026-07-26): a one-window candidate pays no stability penalty and wins."""
    one_window = [_stub_result(0.40)]
    three_windows = [_stub_result(value) for value in (0.10, 0.09, 0.11)]
    assert _objective(one_window, 0.50, minimum_windows=1) == 0.40
    assert _objective(three_windows, 0.50, minimum_windows=1) < 0.40  # the bug, in one line
    assert _objective(one_window, 0.50, minimum_windows=2) == -math.inf
    assert math.isfinite(_objective(three_windows, 0.50, minimum_windows=2))
    assert _objective([_stub_result(0.4), _stub_result(0.4)], 0.5, minimum_windows=3) == -math.inf

    # End-to-end through _choose_candidate with 16e stubbed out. Here every candidate shares the
    # same inner-fold set, so the exclusion only bites when min_inner_folds is lowered to 1 -- the
    # latent form of the live tactical-long bug.
    dates = [f"d{index:04d}" for index in range(400)]
    calibration_cfg = {
        "inner_folds": 1,
        "min_inner_folds": 1,
        "inner_initial_fraction": 0.50,
        "minimum_inner_train_dates": 40,
        "embargo_trading_days": 2,
        "stability_penalty": 0.50,
        "objective_tie_tolerance": 1e-9,
    }
    candidates = [
        {"max_holding_days": 3, "net_profit_target": 0.01, "stop_loss": 0.05},
        {"max_holding_days": 15, "net_profit_target": 0.03, "stop_loss": 0.08},
    ]
    alphas = {3: 0.10, 15: 0.40}

    def _stub_run_replay(**kwargs: Any) -> dict[str, Any]:
        return _stub_result(alphas[int(kwargs["parameters"]["max_holding_days"])])

    real_run_replay = globals()["_run_replay"]
    globals()["_run_replay"] = _stub_run_replay
    try:
        common = {
            "dates": dates,
            "config_path": Path("unused.yaml"),
            "panel_build": "selftest",
            "work_dir": Path("unused"),
            "market_positioning_db": Path("unused.sqlite"),
            "market_positioning_db_sha256": "0" * 64,
            "cache_key": "selftest",
        }
        # (a) with a one-window floor of 1, the single-window 15-day candidate wins unpenalised
        unfloored_rows: list[dict[str, Any]] = []
        selected, diagnostics = _choose_candidate(
            candidates=candidates,
            calibration_cfg={**calibration_cfg, "minimum_objective_windows": 1},
            label_prefix="unfloored",
            grid_rows=unfloored_rows,
            **common,
        )
        assert diagnostics["inner_folds"] == 1
        assert selected == candidates[1], selected
        assert diagnostics["insufficient_window_candidates"] == 0

        # (b) with the default floor EVERY candidate is excluded, so the stage selects nothing
        rows: list[dict[str, Any]] = []
        selected, diagnostics = _choose_candidate(
            candidates=candidates,
            calibration_cfg={
                **calibration_cfg,
                "minimum_objective_windows": DEFAULT_MINIMUM_OBJECTIVE_WINDOWS,
            },
            label_prefix="all_excluded",
            grid_rows=rows,
            **common,
        )
        assert selected is None
        assert diagnostics["reason"] == REASON_ALL_BELOW_MIN_WINDOWS
        assert diagnostics["reason"] in WINDOW_BLOCKED_REASONS
        assert diagnostics["insufficient_window_candidates"] == 2
        assert all(row["selected"] == 0 for row in rows)
        assert all(row["feasible"] == 0 for row in rows)
        assert all(row["selection_method"] == INSUFFICIENT_WINDOWS for row in rows)
        assert all(row["evaluation_windows"] == 1 for row in rows)
        # the inflated mean is recorded, just not selectable
        assert abs(float(rows[1]["mean_selection_alpha_ann"]) - 0.40) < 1e-12
        # every grid row carries identical columns, so the CSV header is never truncated
        assert len({tuple(row) for row in rows + unfloored_rows}) == 1
    finally:
        globals()["_run_replay"] = real_run_replay


def _selftest_candidate_cache() -> None:
    """BUG 2 (2026-07-26): the candidate cache never hit, so every rerun recomputed the grid."""
    candidate = {"max_holding_days": 5, "net_profit_target": 0.02, "stop_loss": 0.05}
    # exactly what 16e writes back: the FULLY RESOLVED set, nine keys to the candidate's three
    resolved = {
        "tail_fraction": 0.1,
        "signal_every_n_snapshots": 5,
        "net_profit_target": 0.02,
        "stop_loss": 0.05,
        "max_holding_days": 5,
        "invalidation_score_z": 0.0,
        "cooldown_days": 5,
        "target_short_gross": 0.5,
        "max_position_weight": 0.015,
    }
    cached_result = {
        "acceptance": "PASS",
        "parameters": resolved,
        "signal_window": {"from": "2021-12-10", "to": "2022-11-02"},
        "source_sha256": "aa" * 32,
        "market_positioning_db_sha256": "bb" * 32,
    }
    base = {
        "candidate_parameters": candidate,
        "cached_parameters": {
            "acceptance": "PASS",
            "parameters": candidate,
            "cache_key": "KEY",
        },
        "cached_result": cached_result,
        "signal_from": "2021-12-10",
        "signal_to": "2022-11-02",
        "replay_source_sha256": "aa" * 32,
        "market_positioning_db_sha256": "bb" * 32,
        "cache_key": "KEY",
        "artifact_cache_key": "KEY",
    }
    # the old test -- equality of the 3-key candidate against the 9-key resolved dict -- never hit
    assert cached_result["parameters"] != candidate
    assert cached_replay_matches(**base)
    # drift in ANY pinned input must miss rather than serve a stale artifact
    assert not cached_replay_matches(**{**base, "artifact_cache_key": "OTHER"})
    assert not cached_replay_matches(**{**base, "cache_key": "OTHER"})
    assert not cached_replay_matches(**{**base, "replay_source_sha256": "cc" * 32})
    assert not cached_replay_matches(**{**base, "market_positioning_db_sha256": "cc" * 32})
    assert not cached_replay_matches(**{**base, "signal_to": "2022-11-03"})
    assert not cached_replay_matches(
        **{**base, "candidate_parameters": {**candidate, "stop_loss": 0.08}}
    )
    assert not cached_replay_matches(
        **{**base, "cached_result": {**cached_result, "acceptance": "FAIL"}}
    )


def _selftest_liquid_overrides() -> None:
    """The liquid block may move wall-clock guards and NOTHING statistical."""
    block = {
        "min_short_entry_price": 10.0,
        "min_median_dollar_volume_20d": 5_000_000,
        "replay_dir": "tactical_short_liquid",
        "calibration_dir": "tactical_short_liquid_calibration",
        "candidate_timeout_seconds": 7200,
        "candidate_max_attempts": 3,
        "candidate_retry_delay_seconds": 5,
    }
    assert liquid_calibration_overrides(block) == {
        "candidate_timeout_seconds": 7200,
        "candidate_max_attempts": 3,
        "candidate_retry_delay_seconds": 5,
    }
    assert liquid_calibration_overrides({"min_short_entry_price": 10.0}) == {}
    for statistical in (
        "max_holding_days",
        "net_profit_targets",
        "stop_losses",
        "outer_folds",
        "inner_folds",
        "stability_penalty",
        "minimum_objective_windows",
        "objective_tie_tolerance",
        "estimated_round_trip_cost_bps",
        "min_parameter_fold_consistency",
    ):
        try:
            liquid_calibration_overrides({**block, statistical: 1})
        except ValueError:
            continue
        raise AssertionError(f"liquid block must not be able to override {statistical}")


def _run_replay(
    *,
    config_path: Path,
    panel_build: str,
    parameters: dict[str, Any],
    signal_from: str,
    signal_to: str,
    work_dir: Path,
    label: str,
    timeout_seconds: float,
    max_attempts: int,
    retry_delay_seconds: float,
    market_positioning_db: Path,
    market_positioning_db_sha256: str,
    cache_key: str,
    liquid_tier: bool = False,
) -> dict[str, Any]:
    parameter_path = work_dir / f"{label}_parameters.json"
    result_path = work_dir / f"{label}_result.json"
    if parameter_path.exists() and result_path.exists():
        try:
            cached_parameters = json.loads(parameter_path.read_text(encoding="utf-8"))
            cached = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            cached_parameters = {}
            cached = {}
        if not isinstance(cached_parameters, dict) or not isinstance(cached, dict):
            cached_parameters = {}
            cached = {}
        # An artifact written before the cache key was recorded inherits it from the directory it
        # lives in, which IS the content hash; nothing is trusted that predates the hash itself.
        artifact_cache_key = str(
            cached_parameters.get("cache_key") or parameter_path.parent.name
        )
        if cached_replay_matches(
            candidate_parameters=parameters,
            cached_parameters=cached_parameters,
            cached_result=cached,
            signal_from=signal_from,
            signal_to=signal_to,
            replay_source_sha256=sha256_file(REPLAY_SCRIPT),
            market_positioning_db_sha256=market_positioning_db_sha256,
            cache_key=cache_key,
            artifact_cache_key=artifact_cache_key,
        ):
            LOGGER.info(
                "%s cache hit (%s..%s, parameters=%s); skipping 16e",
                label,
                signal_from,
                signal_to,
                parameters,
            )
            return cached
    write_manifest(
        parameter_path,
        {
            "acceptance": "PASS",
            "purpose": "nested_calibration_candidate",
            "parameters": parameters,
            "cache_key": cache_key,
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
        "--market-positioning-db",
        str(market_positioning_db),
    ]
    if liquid_tier:
        command.append("--liquid-tier")
    completed: subprocess.CompletedProcess[str] | None = None
    for attempt in range(1, max_attempts + 1):
        completed = subprocess.run(  # noqa: S603 - fixed local script and explicit arguments
            command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
        if completed.returncode == 0 and result_path.exists():
            break
        transient = "database changed during read" in completed.stderr.lower()
        if not transient or attempt >= max_attempts:
            raise RuntimeError(
                f"16e candidate failed ({label}, rc={completed.returncode}, "
                f"attempt={attempt}/{max_attempts}): {completed.stderr[-1000:]}"
            )
        LOGGER.warning(
            "%s hit a concurrent market-positioning update; retrying %d/%d",
            label,
            attempt + 1,
            max_attempts,
        )
        time.sleep(retry_delay_seconds)
    if completed is None or not result_path.exists():
        raise RuntimeError(f"16e candidate produced no result ({label})")
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
    market_positioning_db: Path,
    market_positioning_db_sha256: str,
    cache_key: str,
    liquid_tier: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Nested selection over ``dates``.

    Returns ``(selected_or_None, diagnostics)``. ``None`` means the objective did not discriminate:
    no candidate was evaluable, every candidate fell below the evaluable-window floor, or the best
    objective was tied across several candidates. A tie is NOT broken by taking the smallest grid
    value -- that manufactures a decision out of an indifference and, in this engine, systematically
    picked the minimum holding period. The honest output is no selection, which propagates as
    "no trade" evidence.
    """
    min_inner_folds = int(calibration_cfg.get("min_inner_folds", 2))
    min_objective_windows = max(
        1,
        int(
            calibration_cfg.get(
                "minimum_objective_windows", DEFAULT_MINIMUM_OBJECTIVE_WINDOWS
            )
        ),
    )
    inner_folds = expanding_blocks(
        dates,
        folds=int(calibration_cfg.get("inner_folds", 3)),
        initial_fraction=float(calibration_cfg.get("inner_initial_fraction", 0.50)),
        minimum_train_dates=int(calibration_cfg.get("minimum_inner_train_dates", 126)),
        purge_dates=max(candidate["max_holding_days"] for candidate in candidates)
        + int(calibration_cfg.get("embargo_trading_days", 2)),
    )
    if len(inner_folds) < max(1, min_inner_folds):
        raise RuntimeError(
            f"{label_prefix} materialized {len(inner_folds)} inner folds; "
            f"nested selection requires at least {min_inner_folds}"
        )
    tie_tolerance = float(calibration_cfg.get("objective_tie_tolerance", 1e-9))
    objectives: list[tuple[float, dict[str, Any]]] = []
    insufficient_windows: list[dict[str, Any]] = []
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
                timeout_seconds=float(
                    calibration_cfg.get("candidate_timeout_seconds", 900)
                ),
                max_attempts=int(
                    calibration_cfg.get("candidate_max_attempts", 3)
                ),
                retry_delay_seconds=float(
                    calibration_cfg.get("candidate_retry_delay_seconds", 5)
                ),
                market_positioning_db=market_positioning_db,
                market_positioning_db_sha256=market_positioning_db_sha256,
                cache_key=cache_key,
                liquid_tier=liquid_tier,
            )
            results.append(result)
        mean_selection_alpha = (
            statistics.fmean(
                float(result["summary"]["selection_alpha_ann"]) for result in results
            )
            if results
            else None
        )
        if len(results) < min_objective_windows:
            # FAIL CLOSED. One evaluable window means pstdev == 0, so this candidate would pay no
            # stability penalty and win on a raw, unaveraged mean. Exclude it from selection.
            insufficient_windows.append(
                {
                    **{key: candidate[key] for key in CANDIDATE_KEYS},
                    "reason": INSUFFICIENT_WINDOWS,
                    "evaluation_windows": len(results),
                    "required": min_objective_windows,
                    "unpenalised_mean_selection_alpha_ann": (
                        round(mean_selection_alpha, 10)
                        if mean_selection_alpha is not None
                        else None
                    ),
                }
            )
            grid_rows.append(
                _grid_row(
                    selection_stage=label_prefix,
                    candidate_index=candidate_index,
                    candidate=candidate,
                    evaluation_windows=len(results),
                    min_evaluation_windows=min_objective_windows,
                    selection_method=INSUFFICIENT_WINDOWS,
                    feasible=False,
                    mean_selection_alpha_ann=mean_selection_alpha,
                )
            )
            LOGGER.warning(
                "%s candidate %s evaluated in only %d window(s) < %d required; "
                "EXCLUDED from selection (its unpenalised mean was %s)",
                label_prefix,
                candidate,
                len(results),
                min_objective_windows,
                f"{mean_selection_alpha:.6f}" if mean_selection_alpha is not None else "NA",
            )
            continue
        objective = _objective(
            results,
            float(calibration_cfg.get("stability_penalty", 0.50)),
            minimum_windows=min_objective_windows,
        )
        grid_rows.append(
            _grid_row(
                selection_stage=label_prefix,
                candidate_index=candidate_index,
                candidate=candidate,
                evaluation_windows=len(results),
                min_evaluation_windows=min_objective_windows,
                selection_method="inner_walkforward",
                feasible=True,
                objective=objective,
                mean_selection_alpha_ann=mean_selection_alpha,
            )
        )
        objectives.append((objective, dict(candidate)))
        if math.isfinite(objective) and objective > best_objective:
            best = dict(candidate)
            best_objective = objective
        LOGGER.info(
            "%s candidate %d/%d complete objective=%.8f",
            label_prefix,
            candidate_index,
            len(candidates),
            objective,
        )
    diagnostics: dict[str, Any] = {
        "inner_folds": len(inner_folds),
        "candidates": len(candidates),
        "evaluable_candidates": sum(
            1 for objective, _ in objectives if math.isfinite(objective)
        ),
        "minimum_objective_windows": min_objective_windows,
        "insufficient_window_candidates": len(insufficient_windows),
        "insufficient_window_details": insufficient_windows,
        "best_objective": best_objective if math.isfinite(best_objective) else None,
        "tied_candidates": 0,
        "unstable_tie": False,
        "boundary_axes": [],
    }
    if best is None or not math.isfinite(best_objective):
        # Never fall back to an excluded candidate. If the whole stage is excluded, the stage
        # selects nothing and says exactly why.
        if not objectives and insufficient_windows:
            diagnostics["reason"] = (
                REASON_ALL_BELOW_MIN_WINDOWS
                if len(insufficient_windows) == len(candidates)
                else REASON_NO_EVALUABLE_AFTER_WINDOW_EXCLUSION
            )
            LOGGER.warning(
                "%s selected nothing: %d of %d candidates fell below the %d-window floor",
                label_prefix,
                len(insufficient_windows),
                len(candidates),
                min_objective_windows,
            )
        else:
            diagnostics["reason"] = REASON_NO_EVALUABLE_CANDIDATE
        return None, diagnostics
    tied = [
        candidate
        for objective, candidate in objectives
        if math.isfinite(objective) and abs(objective - best_objective) <= tie_tolerance
    ]
    diagnostics["tied_candidates"] = len(tied)
    if len(tied) > 1:
        # The objective did not discriminate. Refuse to pick; mark the stage unstable.
        diagnostics["unstable_tie"] = True
        diagnostics["reason"] = REASON_OBJECTIVE_TIE
        for row in grid_rows:
            if row["selection_stage"] != label_prefix or not row.get("feasible", 0):
                continue
            if any(
                all(row[key] == candidate[key] for key in CANDIDATE_KEYS)
                for candidate in tied
            ):
                row["selection_unstable_tie"] = 1
        LOGGER.warning(
            "%s produced a %d-way objective tie at %.10f; no parameter selected",
            label_prefix,
            len(tied),
            best_objective,
        )
        return None, diagnostics
    diagnostics["boundary_axes"] = grid_boundary_axes(best, candidates)
    for row in reversed(grid_rows):
        if (
            row["selection_stage"] == label_prefix
            and int(row.get("feasible", 0)) == 1
            and all(row[key] == best[key] for key in CANDIDATE_KEYS)
        ):
            row["selected"] = 1
            break
    return best, diagnostics


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
    if (
        float(calibration_cfg.get("candidate_timeout_seconds", 900)) <= 0
        or int(calibration_cfg.get("candidate_max_attempts", 3)) < 1
        or float(calibration_cfg.get("candidate_retry_delay_seconds", 5)) < 0
    ):
        LOGGER.error("Invalid tactical-short candidate retry/timeout policy")
        return 1
    # PRE-REGISTERED liquid-tier variant (LIQUID_SHORT_TEST.md). The block is not read at all
    # without the flag, and the flag only changes (a) which entry gates 16e applies, (b) the output
    # root, and (c) the cache key. The grid, the folds, the objective and the promotion gates are
    # exactly those of the sealed full-universe run.
    liquid_cfg: dict[str, Any] = {}
    if args.liquid_tier:
        liquid_cfg = dict(cfg_get(config, "tactical_short_liquid", {}) or {})
        if not liquid_cfg:
            LOGGER.error(
                "--liquid-tier requires a tactical_short_liquid config block; none is configured"
            )
            return 1
        try:
            infrastructure_overrides = liquid_calibration_overrides(liquid_cfg)
        except ValueError as exc:
            LOGGER.error("%s", exc)
            return 1
        calibration_cfg = {**calibration_cfg, **infrastructure_overrides}
        if (
            float(calibration_cfg.get("candidate_timeout_seconds", 900)) <= 0
            or int(calibration_cfg.get("candidate_max_attempts", 3)) < 1
            or float(calibration_cfg.get("candidate_retry_delay_seconds", 5)) < 0
        ):
            LOGGER.error("Invalid liquid-tier candidate retry/timeout policy")
            return 1
        LOGGER.info(
            "LIQUID TIER calibration: gates=%s infrastructure_overrides=%s",
            {key: liquid_cfg[key] for key in sorted(set(liquid_cfg) & LIQUID_OWN_KEYS)},
            infrastructure_overrides,
        )
    try:
        candidates, rejected_candidates = candidate_grid(config)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1
    if rejected_candidates:
        LOGGER.info(
            "Rejected %d infeasible candidates (net profit target below %.4f round-trip floor)",
            len(rejected_candidates),
            minimum_feasible_target(calibration_cfg),
        )
    work_root = paths.output_dir / ".tactical_short_calibration_work"
    try:
        cost_cfg = cfg_get(config, "tactical_short.short_costs", {}) or {}
        market_db_source = resolve_path(
            str(
                cost_cfg.get(
                    "market_positioning_db_path",
                    cfg_get(
                        config,
                        "sector_neutral_arm.short_costs.market_positioning_db_path",
                        r"C:\Users\josel\Documents\STAGING\DB\market_positioning.sqlite",
                    ),
                )
            ),
            base_dir=config_path.parent,
        )
        snapshot_db, snapshot_db_sha = snapshot_sqlite_database(
            market_db_source,
            work_root / "db_snapshots",
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        LOGGER.error("Cannot snapshot market-positioning database: %s", exc)
        return 1
    cache_inputs = {
        "config.yaml": sha256_file(config_path),
        "market_positioning_snapshot.sqlite": snapshot_db_sha,
        "calibration_panel_manifest.json": sha256_file(panel_manifest_path),
        "execution_ohlcv_manifest.json": sha256_file(execution_manifest_path),
        "backtest/16e_tactical_short_replay.py": sha256_file(REPLAY_SCRIPT),
        "backtest/16f_calibrate_tactical_short.py": sha256_file(
            Path(__file__).resolve()
        ),
        "backtest/short_costs.py": sha256_file(
            PACKAGE_ROOT / "backtest" / "short_costs.py"
        ),
        "backtest/walkforward_common.py": sha256_file(
            PACKAGE_ROOT / "backtest" / "walkforward_common.py"
        ),
        # The flag is a command-line input, not a config one, so it must enter the key explicitly:
        # a liquid-tier candidate and a full-universe candidate share parameters but not universes.
        "liquid_tier": "1" if args.liquid_tier else "0",
        "tactical_short_liquid": json.dumps(liquid_cfg, sort_keys=True),
    }
    cache_key = hashlib.sha256(
        json.dumps(cache_inputs, sort_keys=True).encode("utf-8")
    ).hexdigest()[:20]
    work_dir = work_root / panel_dir.name / cache_key
    work_dir.mkdir(parents=True, exist_ok=True)
    if args.smoke:
        smoke_dates = signal_dates[-30:]
        if len(smoke_dates) < 10:
            LOGGER.error("Insufficient signal dates for calibration smoke test")
            return 1
        try:
            with tempfile.TemporaryDirectory(dir=work_dir) as temp_name:
                result = _run_replay(
                    config_path=config_path,
                    panel_build=panel_dir.name,
                    parameters=candidates[0],
                    signal_from=smoke_dates[0],
                    signal_to=smoke_dates[-1],
                    work_dir=Path(temp_name),
                    label="smoke",
                    timeout_seconds=float(
                        calibration_cfg.get("candidate_timeout_seconds", 900)
                    ),
                    max_attempts=int(
                        calibration_cfg.get("candidate_max_attempts", 3)
                    ),
                    retry_delay_seconds=float(
                        calibration_cfg.get("candidate_retry_delay_seconds", 5)
                    ),
                    market_positioning_db=snapshot_db,
                    market_positioning_db_sha256=snapshot_db_sha,
                    cache_key=cache_key,
                    liquid_tier=args.liquid_tier,
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
        folds=int(calibration_cfg.get("outer_folds", 7)),
        initial_fraction=float(calibration_cfg.get("outer_initial_fraction", 0.50)),
        minimum_train_dates=int(calibration_cfg.get("minimum_outer_train_dates", 252)),
        purge_dates=purge_dates,
    )
    # FAIL CLOSED. Three outer folds is not enough evidence to publish a fold-consistency number,
    # which is why the previous run's 0.333 "consistency" was meaningless.
    min_outer_folds = max(
        int(calibration_cfg.get("min_outer_folds", 5)),
        int(calibration_cfg.get("minimum_valid_outer_folds", 3)),
    )
    if len(outer_folds) < min_outer_folds:
        LOGGER.error(
            "Insufficient outer folds: %d materialized, %d required",
            len(outer_folds),
            min_outer_folds,
        )
        return 1

    calibration_dir_name = (
        str(liquid_cfg.get("calibration_dir", "tactical_short_liquid_calibration"))
        if liquid_cfg
        else str(calibration_cfg.get("dir", "tactical_short_calibration"))
    )
    out_dir = (
        paths.output_dir
        / calibration_dir_name
        / f"{panel_dir.name}{str(args.output_suffix or '').strip()}"
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
    unstable_folds: list[int] = []
    boundary_folds: dict[str, list[str]] = {}
    folds_without_selection: dict[str, str] = {}
    try:
        for outer in outer_folds:
            selected, diagnostics = _choose_candidate(
                candidates=candidates,
                dates=outer["train_dates"],
                config_path=config_path,
                panel_build=panel_dir.name,
                calibration_cfg=calibration_cfg,
                work_dir=work_dir,
                label_prefix=f"outer{outer['fold']}",
                grid_rows=grid_rows,
                market_positioning_db=snapshot_db,
                market_positioning_db_sha256=snapshot_db_sha,
                cache_key=cache_key,
                liquid_tier=args.liquid_tier,
            )
            if selected is None:
                # No selection means no trade. The fold contributes no OOS evidence and is
                # recorded so the reader can see WHY the evidence is missing.
                no_selection_reason = str(
                    diagnostics.get("reason") or REASON_NO_EVALUABLE_CANDIDATE
                )
                folds_without_selection[str(outer["fold"])] = no_selection_reason
                if no_selection_reason == REASON_OBJECTIVE_TIE:
                    unstable_folds.append(int(outer["fold"]))
                LOGGER.warning(
                    "outer%d selected no parameter (%s); the fold contributes no OOS evidence",
                    outer["fold"],
                    no_selection_reason,
                )
                fold_rows.append(
                    {
                        "outer_fold": outer["fold"],
                        "train_start": outer["train_dates"][0],
                        "train_end": outer["train_dates"][-1],
                        "purged_dates": len(outer["purged_dates"]),
                        "test_start": outer["test_dates"][0],
                        "test_end": outer["test_dates"][-1],
                        "max_holding_days": "",
                        "net_profit_target": "",
                        "stop_loss": "",
                        "test_trades": "",
                        "test_selection_alpha_ann": "",
                        "test_profit_factor": "",
                        "selection_unstable_tie": int(
                            no_selection_reason == REASON_OBJECTIVE_TIE
                        ),
                        "no_selection_reason": no_selection_reason,
                        "insufficient_window_candidates": diagnostics[
                            "insufficient_window_candidates"
                        ],
                        "tied_candidates": diagnostics["tied_candidates"],
                        "selected_at_grid_boundary": "",
                        "boundary_axes": "",
                    }
                )
                continue
            if diagnostics["boundary_axes"]:
                boundary_folds[str(outer["fold"])] = list(diagnostics["boundary_axes"])
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
                timeout_seconds=float(
                    calibration_cfg.get("candidate_timeout_seconds", 900)
                ),
                max_attempts=int(
                    calibration_cfg.get("candidate_max_attempts", 3)
                ),
                retry_delay_seconds=float(
                    calibration_cfg.get("candidate_retry_delay_seconds", 5)
                ),
                market_positioning_db=snapshot_db,
                market_positioning_db_sha256=snapshot_db_sha,
                cache_key=cache_key,
                liquid_tier=args.liquid_tier,
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
                    "selection_unstable_tie": 0,
                    "no_selection_reason": "",
                    "insufficient_window_candidates": diagnostics[
                        "insufficient_window_candidates"
                    ],
                    "tied_candidates": diagnostics["tied_candidates"],
                    "selected_at_grid_boundary": int(bool(diagnostics["boundary_axes"])),
                    "boundary_axes": ";".join(diagnostics["boundary_axes"]),
                }
            )
        final_parameters, final_diagnostics = _choose_candidate(
            candidates=candidates,
            dates=signal_dates,
            config_path=config_path,
            panel_build=panel_dir.name,
            calibration_cfg=calibration_cfg,
            work_dir=work_dir,
            label_prefix="full_development",
            grid_rows=grid_rows,
            market_positioning_db=snapshot_db,
            market_positioning_db_sha256=snapshot_db_sha,
            cache_key=cache_key,
            liquid_tier=args.liquid_tier,
        )
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        LOGGER.error("Calibration failed: %s", exc)
        return 1
    evaluable_outer_folds = len(outer_results)
    if evaluable_outer_folds < min_outer_folds:
        LOGGER.error(
            "Only %d of %d outer folds produced OOS evidence (%d selected nothing: %s); "
            "%d are required",
            evaluable_outer_folds,
            len(outer_folds),
            len(folds_without_selection),
            folds_without_selection or "none",
            min_outer_folds,
        )
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
    # HAC lag follows the longest selected horizon rather than a hardcoded 5.
    oos_hac_lag = hac_lag_for_hold(
        max(int(item["max_holding_days"]) for item in selected_candidates),
        min_lag=int(cfg_get(config, "tactical.hac_min_lag_days", 5)),
        divisor=int(cfg_get(config, "tactical.hac_horizon_divisor", 5)),
    )
    _mean, _se, active_t = mean_t_hac(list(daily_selection), max_lag=oos_hac_lag)
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
    # A fold-consistency number computed on three folds is noise dressed as evidence. It is
    # published only once the configured floor of evaluable outer folds is met.
    min_folds_for_consistency = int(
        cfg_get(config, "tactical.min_outer_folds_for_consistency", 5)
    )
    consistency_publishable = evaluable_outer_folds >= min_folds_for_consistency
    dominant_fraction = max(parameter_counts.values()) / len(selected_candidates)
    if not consistency_publishable:
        rejection_reasons.append("fold_consistency_not_publishable")
    elif dominant_fraction < float(calibration_cfg.get("min_parameter_fold_consistency", 0.50)):
        rejection_reasons.append("selected_parameters_unstable_across_folds")
    if unstable_folds:
        rejection_reasons.append("objective_ties_blocked_fold_selection")
    window_blocked_folds = sorted(
        fold
        for fold, reason in folds_without_selection.items()
        if reason in WINDOW_BLOCKED_REASONS
    )
    if window_blocked_folds:
        rejection_reasons.append("insufficient_evaluation_windows_blocked_fold_selection")
    final_no_selection_reason = (
        str(final_diagnostics.get("reason") or REASON_NO_EVALUABLE_CANDIDATE)
        if final_parameters is None
        else ""
    )
    if final_parameters is None:
        rejection_reasons.append("no_stable_final_parameter_selection")
        if final_no_selection_reason in WINDOW_BLOCKED_REASONS:
            rejection_reasons.append("final_selection_blocked_by_evaluation_window_floor")
        LOGGER.warning(
            "full_development selected no parameter (%s)", final_no_selection_reason
        )
    final_boundary_axes = list(final_diagnostics.get("boundary_axes") or [])
    if final_boundary_axes or boundary_folds:
        # A boundary selection means the searched box may not contain the optimum. That is an
        # unresolved design question, not a promotable result.
        rejection_reasons.append("selected_at_grid_boundary")
    promotable = not rejection_reasons

    write_csv(grid_path, list(grid_rows[0]), grid_rows)
    write_csv(folds_path, list(fold_rows[0]), fold_rows)
    write_manifest(
        parameters_path,
        {
            "acceptance": "PASS",
            "promotion_status": "PROMOTABLE" if promotable else "NOT_PROMOTABLE",
            "panel_build": panel_dir.name,
            # Pre-registered liquid-tier variant (LIQUID_SHORT_TEST.md). A replay consuming this
            # artifact must be run with the SAME flag; the gates are recorded here to make a
            # mismatch obvious rather than silent.
            "liquid_tier": bool(args.liquid_tier),
            "liquid_tier_config": dict(liquid_cfg),
            "parameters": final_parameters,
            "parameters_selected": final_parameters is not None,
            "no_selection_reason": final_no_selection_reason or None,
            "minimum_objective_windows": int(
                final_diagnostics.get(
                    "minimum_objective_windows", DEFAULT_MINIMUM_OBJECTIVE_WINDOWS
                )
            ),
            "candidates_excluded_insufficient_windows": int(
                final_diagnostics.get("insufficient_window_candidates", 0)
            ),
            "selected_at_grid_boundary": bool(final_boundary_axes),
            "boundary_axes": final_boundary_axes,
            "oos_summary": {
                "outer_folds_planned": len(outer_folds),
                "outer_folds": evaluable_outer_folds,
                "outer_folds_blocked_by_tie": unstable_folds,
                "outer_folds_without_selection": folds_without_selection,
                "trades": len(trade_returns),
                "selection_alpha_ann": round(oos_selection_ann, 8),
                "selection_alpha_ann_convention": "arithmetic_sum_over_years",
                "stress_net_ann": round(oos_stress_ann, 8),
                "active_t": round(float(active_t), 6) if active_t is not None else None,
                "active_t_hac_lag_days": oos_hac_lag,
                "profit_factor": round(profit_factor, 6)
                if profit_factor is not None
                else None,
                "positive_sectors": positive_sectors,
                "parameter_fold_consistency": (
                    round(dominant_fraction, 6) if consistency_publishable else None
                ),
                "parameter_fold_consistency_publishable": consistency_publishable,
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
            "liquid_tier": bool(args.liquid_tier),
            "liquid_tier_config": dict(liquid_cfg),
            "candidate_count": len(candidates),
            "rejected_infeasible_candidates": rejected_candidates,
            "minimum_feasible_net_profit_target": minimum_feasible_target(calibration_cfg),
            "outer_folds_planned": len(outer_folds),
            "outer_folds": evaluable_outer_folds,
            "min_outer_folds": min_outer_folds,
            "outer_folds_blocked_by_tie": unstable_folds,
            "outer_folds_without_selection": folds_without_selection,
            "outer_folds_blocked_by_evaluation_window_floor": window_blocked_folds,
            "minimum_objective_windows": int(
                calibration_cfg.get(
                    "minimum_objective_windows", DEFAULT_MINIMUM_OBJECTIVE_WINDOWS
                )
            ),
            "final_no_selection_reason": final_no_selection_reason or None,
            "selected_at_grid_boundary": bool(final_boundary_axes) or bool(boundary_folds),
            "boundary_axes_by_fold": boundary_folds,
            "final_boundary_axes": final_boundary_axes,
            "final_selection_diagnostics": final_diagnostics,
            "active_t_hac_lag_days": oos_hac_lag,
            "purge_trading_dates": purge_dates,
            "candidate_cache_key": cache_key,
            "inputs_sha256": {
                "config.yaml": sha256_file(config_path),
                "market_positioning_snapshot.sqlite": snapshot_db_sha,
                "backtest/16e_tactical_short_replay.py": sha256_file(REPLAY_SCRIPT),
                "backtest/16f_calibrate_tactical_short.py": sha256_file(
                    Path(__file__).resolve()
                ),
                "backtest/short_costs.py": sha256_file(
                    PACKAGE_ROOT / "backtest" / "short_costs.py"
                ),
                "backtest/walkforward_common.py": sha256_file(
                    PACKAGE_ROOT / "backtest" / "walkforward_common.py"
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

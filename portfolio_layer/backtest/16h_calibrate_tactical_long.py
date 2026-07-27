#!/usr/bin/env python3
"""Nested, purged calibration for the Stage 11 tactical long engine.

Only the maximum holding period is selected. Every candidate uses the same
D+1-open execution, immediate first-threshold signal invalidation, portfolio
construction, costs, and promotion gates. Outer test blocks remain untouched
by parameter selection.

2026-07-25 fold and selection redesign (tactical long/short diagnostic):

  * at least five outer folds must materialize and produce OOS evidence, or the
    run fails closed. Three folds cannot support a fold-consistency statistic,
    so that number is suppressed below the configured floor.
  * the purge is now CANDIDATE SPECIFIC (ceil(hold / signal_every) + embargo).
    Purging every candidate at the 252-session horizon consumed roughly a year
    of history and left the early outer folds unable to host a second inner
    fold at ANY horizon; the code silently fell back to tuning on the outer
    training block itself. A candidate that cannot support the required number
    of inner folds, or whose horizon leaves no non-overlapping outer test
    window, is now marked INFEASIBLE for that fold and excluded from selection
    there -- recorded in the grid CSV and the manifest, never silently ignored.
  * ties no longer select. An objective tie means the objective did not
    discriminate; the fold is marked unstable and contributes no evidence.
  * a selection on a grid corner is flagged and blocks promotion.
  * the HAC lag follows the selected horizon: max(5, ceil(hold / 5)).

2026-07-26 fixes:

  * MINIMUM OBJECTIVE WINDOWS. ``mean - 0.5 * pstdev`` had no floor on the number of evaluable
    inner windows. A candidate evaluable in exactly ONE window has ``pstdev == 0``, pays no
    stability penalty at all, and ranks first on a raw mean inflated 2-4x by a single lucky
    window -- this is how the 252-session horizon was selected twice. Candidates with fewer than
    ``minimum_objective_windows`` (config, default 2) evaluable windows are now marked
    ``insufficient_evaluation_windows`` and EXCLUDED from selection: never selected, recorded in
    the grid CSV and the manifest with counts. If every candidate in a stage is excluded, the
    stage selects nothing and says why, instead of falling back to a one-window winner.
  * CANDIDATE CACHE NEVER HIT. The cache compared the 1-key candidate dict
    ``{"max_holding_days": N}`` against the FULLY RESOLVED 6-key parameter dict 16g writes into
    its result manifest. That is always False, so every candidate was recomputed on every rerun
    (~25 minutes lost per crash). The comparison is now a SUBSET test over the keys the candidate
    actually specifies, guarded by the content-hash cache key (config + code + panel shas) that
    names the artifact directory, so the cache still fails closed under drift.
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

from portfolio_layer.backtest.short_costs import snapshot_sqlite_database  # noqa: E402
from portfolio_layer.backtest.walkforward_common import (  # noqa: E402
    cached_replay_matches,
    hac_lag_for_hold,
)
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
from portfolio_layer.research.stage11_common import mean_t_hac  # noqa: E402


LOGGER = logging.getLogger("calibrate_tactical_long")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
REPLAY_SCRIPT = PACKAGE_ROOT / "backtest" / "16g_tactical_long_replay.py"

DEFAULT_MINIMUM_OBJECTIVE_WINDOWS = 2
REASON_OBJECTIVE_TIE = "objective_tie_no_selection"
REASON_NO_EVALUABLE_CANDIDATE = "no_evaluable_candidate"
REASON_ALL_BELOW_MIN_WINDOWS = "all_candidates_below_minimum_objective_windows"
REASON_NO_EVALUABLE_AFTER_WINDOW_EXCLUSION = "no_evaluable_candidate_after_window_exclusion"
WINDOW_BLOCKED_REASONS = frozenset(
    {REASON_ALL_BELOW_MIN_WINDOWS, REASON_NO_EVALUABLE_AFTER_WINDOW_EXCLUSION}
)
INSUFFICIENT_WINDOWS = "insufficient_evaluation_windows"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Calibrate the tactical long policy.")
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
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def candidate_grid(config: dict[str, Any]) -> list[dict[str, Any]]:
    cfg = cfg_get(config, "tactical_long_calibration", {}) or {}
    holds = sorted(
        {int(value) for value in cfg.get("max_holding_days", [15, 30, 63, 126, 252])}
    )
    if not holds or min(holds) < 1:
        raise ValueError("Invalid tactical_long_calibration candidate grid")
    return [{"max_holding_days": value} for value in holds]


def candidate_purge_signal_dates(
    holding_days: int, *, signal_every: int, embargo: int
) -> int:
    """Purge, in SIGNAL-DATE units, needed so a candidate's labels cannot leak into the test."""
    return int(math.ceil(int(holding_days) / max(1, int(signal_every)))) + max(0, int(embargo))


def grid_boundary_axes(
    selected: dict[str, Any], candidates: list[dict[str, Any]]
) -> list[str]:
    """Axes on which ``selected`` sits at a grid corner.

    A horizon chosen at the edge of the searched grid is not an interior optimum: the true best
    value may lie outside the grid entirely. Such a selection is flagged and blocks promotion.
    """
    values = sorted({int(candidate["max_holding_days"]) for candidate in candidates})
    if len(values) < 2:
        return []
    chosen = int(selected["max_holding_days"])
    if chosen <= values[0] or chosen >= values[-1]:
        return ["max_holding_days"]
    return []


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

    The floor is not cosmetic. With a single window ``pstdev`` is identically zero, so a candidate
    evaluated once pays NO stability penalty while a candidate evaluated three times pays the full
    one -- the single-window candidate wins on an unpenalised, unaveraged number. Returning -inf
    below the floor makes such a candidate unselectable rather than best.
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
    inner_folds: int,
    evaluation_windows: int,
    min_evaluation_windows: int,
    selection_method: str,
    purge_signal_dates: int,
    feasible: bool,
    objective: float | None = None,
    mean_selection_alpha_ann: float | None = None,
) -> dict[str, Any]:
    """One grid CSV row. Every row carries the same columns so the header is never truncated."""
    return {
        "selection_stage": selection_stage,
        "candidate": candidate_index,
        **candidate,
        "inner_folds": inner_folds,
        "evaluation_windows": evaluation_windows,
        "min_evaluation_windows": min_evaluation_windows,
        "selection_method": selection_method,
        "purge_signal_dates": purge_signal_dates,
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


def _non_overlapping_window(
    window: list[str],
    *,
    next_window_start: str | None,
    all_dates: list[str],
    holding_days: int,
    signal_every: int,
) -> list[str]:
    """Trim signal dates so positions exit before the next evaluation block."""
    if next_window_start is None:
        return window
    next_index = all_dates.index(next_window_start)
    holding_signal_intervals = int(math.ceil(holding_days / signal_every))
    safe_end_index = next_index - holding_signal_intervals - 1
    if safe_end_index < 0:
        return []
    safe_end = all_dates[safe_end_index]
    return [value for value in window if value <= safe_end]


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
    strong = [
        {"acceptance": "PASS", "summary": {"selection_alpha_ann": value}}
        for value in (0.1, 0.08)
    ]
    unstable = [
        {"acceptance": "PASS", "summary": {"selection_alpha_ann": value}}
        for value in (0.2, -0.1)
    ]
    assert _objective(strong, 1.0) > _objective(unstable, 1.0)
    weekly = [f"2020-{month:02d}-01" for month in range(1, 13)]
    trimmed = _non_overlapping_window(
        weekly[2:8],
        next_window_start=weekly[8],
        all_dates=weekly,
        holding_days=10,
        signal_every=5,
    )
    assert trimmed == weekly[2:6]
    # --- candidate-specific purge (2026-07-25 fix 7) ---
    assert candidate_purge_signal_dates(15, signal_every=5, embargo=2) == 5
    assert candidate_purge_signal_dates(63, signal_every=5, embargo=2) == 15
    assert candidate_purge_signal_dates(252, signal_every=5, embargo=2) == 53
    # a long horizon purges far more than a short one, which is exactly why a single
    # max-horizon purge starved the early folds
    assert candidate_purge_signal_dates(252, signal_every=5, embargo=2) > (
        candidate_purge_signal_dates(15, signal_every=5, embargo=2) * 10
    )
    # --- boundary detection (2026-07-25 fix 7) ---
    grid = [{"max_holding_days": value} for value in (15, 30, 63, 126, 252)]
    assert grid_boundary_axes({"max_holding_days": 15}, grid) == ["max_holding_days"]
    assert grid_boundary_axes({"max_holding_days": 252}, grid) == ["max_holding_days"]
    assert grid_boundary_axes({"max_holding_days": 63}, grid) == []
    assert grid_boundary_axes({"max_holding_days": 63}, [{"max_holding_days": 63}]) == []
    # --- HAC lag follows the horizon (2026-07-25 fix 10) ---
    assert hac_lag_for_hold(15) == 5 and hac_lag_for_hold(63) == 13 and hac_lag_for_hold(252) == 51
    _selftest_minimum_objective_windows()
    _selftest_candidate_cache()
    print("tactical-long calibration self-test: PASS")


def _stub_result(alpha: float) -> dict[str, Any]:
    return {"acceptance": "PASS", "summary": {"selection_alpha_ann": alpha}}


def _selftest_minimum_objective_windows() -> None:  # noqa: PLR0915
    """BUG 1 (2026-07-26): a one-window candidate paid no stability penalty and won."""
    # A single window has pstdev == 0, so the raw mean IS the objective. Below the floor the
    # candidate must be unselectable, not best.
    one_window = [_stub_result(0.40)]
    three_windows = [_stub_result(value) for value in (0.10, 0.09, 0.11)]
    assert _objective(one_window, 0.50, minimum_windows=1) == 0.40
    assert _objective(three_windows, 0.50, minimum_windows=1) < 0.40  # the bug, in one line
    assert _objective(one_window, 0.50, minimum_windows=2) == -math.inf
    assert math.isfinite(_objective(three_windows, 0.50, minimum_windows=2))
    assert _objective([_stub_result(0.4), _stub_result(0.4)], 0.5, minimum_windows=3) == -math.inf

    # End-to-end through _choose_candidate with 16g stubbed out. n=300 signal dates reproduces the
    # observed geometry exactly: hold=15 gets three evaluable inner windows, hold=252 gets ONE.
    dates = [f"d{index:04d}" for index in range(300)]
    calibration_cfg = {
        "inner_folds": 3,
        "min_inner_folds": 2,
        "inner_initial_fraction": 0.50,
        "minimum_inner_train_dates": 40,
        "stability_penalty": 0.50,
        "objective_tie_tolerance": 1e-9,
    }
    alphas = {15: 0.10, 252: 0.40}
    calls: list[str] = []

    def _stub_run_replay(**kwargs: Any) -> dict[str, Any]:
        calls.append(str(kwargs["label"]))
        return _stub_result(alphas[int(kwargs["parameters"]["max_holding_days"])])

    real_run_replay = globals()["_run_replay"]
    globals()["_run_replay"] = _stub_run_replay
    try:
        candidates = [{"max_holding_days": 15}, {"max_holding_days": 252}]
        common = {
            "dates": dates,
            "config_path": Path("unused.yaml"),
            "panel_build": "selftest",
            "signal_every": 5,
            "embargo_signal_dates": 2,
            "work_dir": Path("unused"),
            "market_positioning_db": Path("unused.sqlite"),
            "market_positioning_db_sha256": "0" * 64,
            "cache_key": "selftest",
        }
        # (a) WITHOUT the floor the one-window 252 candidate wins on a 4x inflated raw mean.
        unfloored_rows: list[dict[str, Any]] = []
        selected, diagnostics = _choose_candidate(
            candidates=candidates,
            calibration_cfg={**calibration_cfg, "minimum_objective_windows": 1},
            label_prefix="unfloored",
            grid_rows=unfloored_rows,
            **common,
        )
        assert selected == {"max_holding_days": 252}, selected
        assert diagnostics["insufficient_window_candidates"] == 0
        one_window_row = next(
            row for row in unfloored_rows if row["max_holding_days"] == 252
        )
        assert one_window_row["evaluation_windows"] == 1
        assert abs(float(one_window_row["objective"]) - 0.40) < 1e-12  # zero penalty paid

        # (b) WITH the default floor the one-window candidate is excluded and never selected.
        rows: list[dict[str, Any]] = []
        selected, diagnostics = _choose_candidate(
            candidates=candidates,
            calibration_cfg=calibration_cfg,
            label_prefix="floored",
            grid_rows=rows,
            **common,
        )
        assert selected == {"max_holding_days": 15}, selected
        assert diagnostics["minimum_objective_windows"] == DEFAULT_MINIMUM_OBJECTIVE_WINDOWS
        assert diagnostics["insufficient_window_candidates"] == 1
        assert diagnostics["insufficient_window_holds"] == [252]
        excluded_row = next(row for row in rows if row["max_holding_days"] == 252)
        assert excluded_row["feasible"] == 0
        assert excluded_row["selection_method"] == INSUFFICIENT_WINDOWS
        assert excluded_row["objective"] == ""
        assert excluded_row["selected"] == 0
        assert excluded_row["evaluation_windows"] == 1
        assert excluded_row["min_evaluation_windows"] == 2
        # the inflated mean is still RECORDED, just not selectable
        assert abs(float(excluded_row["mean_selection_alpha_ann"]) - 0.40) < 1e-12
        assert any(
            item["reason"] == INSUFFICIENT_WINDOWS and item["max_holding_days"] == 252
            for item in diagnostics["infeasible_candidates"]
        )
        assert sum(row["selected"] for row in rows) == 1
        assert next(row for row in rows if row["max_holding_days"] == 15)["selected"] == 1
        # every grid row carries identical columns, so the CSV header is never truncated
        assert len({tuple(row) for row in rows + unfloored_rows}) == 1

        # (c) if EVERY candidate is excluded the stage selects nothing, with an explicit reason.
        rows = []
        selected, diagnostics = _choose_candidate(
            candidates=[{"max_holding_days": 252}],
            calibration_cfg=calibration_cfg,
            label_prefix="all_excluded",
            grid_rows=rows,
            **common,
        )
        assert selected is None
        assert diagnostics["reason"] == REASON_ALL_BELOW_MIN_WINDOWS
        assert diagnostics["reason"] in WINDOW_BLOCKED_REASONS
        assert diagnostics["insufficient_window_candidates"] == 1
        assert diagnostics["evaluable_candidates"] == 0
        assert all(row["selected"] == 0 for row in rows)
    finally:
        globals()["_run_replay"] = real_run_replay
    assert calls, "the stubbed replay was never exercised"


def _selftest_candidate_cache() -> None:
    """BUG 2 (2026-07-26): the candidate cache never hit, so every rerun recomputed the grid."""
    candidate = {"max_holding_days": 15}
    # exactly what 16g writes back: the FULLY RESOLVED parameter set, six keys to the candidate's one
    resolved = {
        "tail_fraction": 0.1,
        "signal_every_n_snapshots": 5,
        "max_holding_days": 15,
        "invalidation_score_z": 0.0,
        "target_long_gross": 0.95,
        "max_position_weight": 0.05,
    }
    cached_result = {
        "acceptance": "PASS",
        "parameters": resolved,
        "signal_window": {"from": "2021-12-10", "to": "2022-11-02"},
        "source_sha256": "aa" * 32,
        "market_positioning_db_sha256": "bb" * 32,
    }
    cached_parameters = {"acceptance": "PASS", "parameters": candidate, "cache_key": "KEY"}
    base = {
        "candidate_parameters": candidate,
        "cached_parameters": cached_parameters,
        "cached_result": cached_result,
        "signal_from": "2021-12-10",
        "signal_to": "2022-11-02",
        "replay_source_sha256": "aa" * 32,
        "market_positioning_db_sha256": "bb" * 32,
        "cache_key": "KEY",
        "artifact_cache_key": "KEY",
    }
    # the old test -- equality of the 1-key candidate against the 6-key resolved dict -- is why the
    # cache never hit
    assert cached_result["parameters"] != candidate
    assert cached_replay_matches(**base)
    # drift in ANY pinned input must miss rather than serve a stale artifact
    assert not cached_replay_matches(**{**base, "artifact_cache_key": "OTHER"})
    assert not cached_replay_matches(**{**base, "cache_key": "OTHER"})
    assert not cached_replay_matches(**{**base, "replay_source_sha256": "cc" * 32})
    assert not cached_replay_matches(**{**base, "market_positioning_db_sha256": "cc" * 32})
    assert not cached_replay_matches(**{**base, "signal_to": "2022-11-03"})
    assert not cached_replay_matches(
        **{**base, "candidate_parameters": {"max_holding_days": 30}}
    )
    assert not cached_replay_matches(
        **{**base, "cached_result": {**cached_result, "acceptance": "FAIL"}}
    )
    assert not cached_replay_matches(
        **{**base, "cached_result": {**cached_result, "parameters": None}}
    )
    # a searched axis missing from the resolved set is NOT a hit
    assert not cached_replay_matches(
        **{
            **base,
            "cached_result": {
                **cached_result,
                "parameters": {
                    key: value
                    for key, value in resolved.items()
                    if key != "max_holding_days"
                },
            },
        }
    )
    # the request file must record the same candidate the caller is asking for
    assert not cached_replay_matches(
        **{
            **base,
            "cached_parameters": {**cached_parameters, "parameters": {"max_holding_days": 30}},
        }
    )
    # --- and again against a REAL cached 16g artifact, if one is present ---
    work_root = PACKAGE_ROOT / "output" / ".tactical_long_calibration_work"
    artifacts = sorted(work_root.glob("*/*/*_result.json")) if work_root.exists() else []
    checked = 0
    for result_path in artifacts:
        parameter_path = result_path.with_name(
            result_path.name.replace("_result.json", "_parameters.json")
        )
        if not parameter_path.exists():
            continue
        real_result = json.loads(result_path.read_text(encoding="utf-8"))
        real_parameters = json.loads(parameter_path.read_text(encoding="utf-8"))
        requested = real_parameters.get("parameters")
        if not isinstance(requested, dict) or real_result.get("acceptance") != "PASS":
            continue
        # legacy artifacts predate the recorded cache key and inherit it from their directory
        artifact_cache_key = str(
            real_parameters.get("cache_key") or parameter_path.parent.name
        )
        real_base = {
            "candidate_parameters": requested,
            "cached_parameters": real_parameters,
            "cached_result": real_result,
            "signal_from": real_result["signal_window"]["from"],
            "signal_to": real_result["signal_window"]["to"],
            "replay_source_sha256": real_result["source_sha256"],
            "market_positioning_db_sha256": real_result["market_positioning_db_sha256"],
            "cache_key": artifact_cache_key,
            "artifact_cache_key": artifact_cache_key,
        }
        assert real_result["parameters"] != requested, result_path  # the bug, on real data
        assert cached_replay_matches(**real_base), result_path
        assert not cached_replay_matches(**{**real_base, "cache_key": "drifted"}), result_path
        checked += 1
        if checked >= 3:
            break
    print(f"  cache-match checked against {checked} real cached 16g artifact(s)")


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
                "%s cache hit (%s..%s, parameters=%s); skipping 16g",
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
                f"16g candidate failed ({label}, rc={completed.returncode}, "
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
        raise RuntimeError(f"16g candidate produced no result ({label})")
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    if payload.get("acceptance") != "PASS":
        raise RuntimeError(f"16g candidate did not pass ({label})")
    return payload


def _choose_candidate(
    *,
    candidates: list[dict[str, Any]],
    dates: list[str],
    config_path: Path,
    panel_build: str,
    calibration_cfg: dict[str, Any],
    signal_every: int,
    embargo_signal_dates: int,
    work_dir: Path,
    label_prefix: str,
    grid_rows: list[dict[str, Any]],
    market_positioning_db: Path,
    market_positioning_db_sha256: str,
    cache_key: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Nested selection over ``dates`` with a CANDIDATE-SPECIFIC purge.

    Returns ``(selected_or_None, diagnostics)``. ``None`` means the objective did not discriminate:
    no feasible candidate, every candidate below the evaluable-window floor, or a tie at the best
    objective. A tie is NOT broken by taking the smallest horizon -- that manufactures a decision
    out of indifference. The honest output is no selection, which propagates as "no trade"
    evidence.
    """
    min_inner_folds = max(1, int(calibration_cfg.get("min_inner_folds", 2)))
    min_objective_windows = max(
        1,
        int(
            calibration_cfg.get(
                "minimum_objective_windows", DEFAULT_MINIMUM_OBJECTIVE_WINDOWS
            )
        ),
    )
    tie_tolerance = float(calibration_cfg.get("objective_tie_tolerance", 1e-9))
    objectives: list[tuple[float, dict[str, Any]]] = []
    infeasible: list[dict[str, Any]] = []
    best: dict[str, Any] | None = None
    best_objective = -math.inf
    for candidate_index, candidate in enumerate(candidates, start=1):
        hold = int(candidate["max_holding_days"])
        # Purge only what THIS horizon requires. Purging every candidate at the longest horizon
        # starved the early folds of inner folds at every horizon and silently degraded selection
        # to "tune on the outer training block".
        purge_dates = candidate_purge_signal_dates(
            hold, signal_every=signal_every, embargo=embargo_signal_dates
        )
        inner_folds = expanding_blocks(
            dates,
            folds=int(calibration_cfg.get("inner_folds", 3)),
            initial_fraction=float(calibration_cfg.get("inner_initial_fraction", 0.50)),
            minimum_train_dates=int(calibration_cfg.get("minimum_inner_train_dates", 40)),
            purge_dates=purge_dates,
        )
        if len(inner_folds) < min_inner_folds:
            # FAIL CLOSED for this candidate in this fold rather than falling back to tuning on
            # the training block, which is not a nested evaluation at all.
            infeasible.append(
                {
                    "max_holding_days": hold,
                    "reason": "insufficient_inner_folds",
                    "inner_folds": len(inner_folds),
                    "required": min_inner_folds,
                    "purge_signal_dates": purge_dates,
                }
            )
            grid_rows.append(
                _grid_row(
                    selection_stage=label_prefix,
                    candidate_index=candidate_index,
                    candidate=candidate,
                    inner_folds=len(inner_folds),
                    evaluation_windows=0,
                    min_evaluation_windows=min_objective_windows,
                    selection_method="infeasible",
                    purge_signal_dates=purge_dates,
                    feasible=False,
                )
            )
            LOGGER.warning(
                "%s candidate hold=%d is infeasible: %d inner folds < %d required (purge=%d)",
                label_prefix, hold, len(inner_folds), min_inner_folds, purge_dates,
            )
            continue
        evaluation_windows = [fold["test_dates"] for fold in inner_folds]
        selection_method = "inner_walkforward"
        results: list[dict[str, Any]] = []
        for window_index, raw_test_dates in enumerate(evaluation_windows, start=1):
            next_start = (
                evaluation_windows[window_index][0]
                if window_index < len(evaluation_windows)
                else None
            )
            test_dates = _non_overlapping_window(
                raw_test_dates,
                next_window_start=next_start,
                all_dates=dates,
                holding_days=hold,
                signal_every=signal_every,
            )
            if not test_dates:
                continue
            results.append(
                _run_replay(
                    config_path=config_path,
                    panel_build=panel_build,
                    parameters=candidate,
                    signal_from=test_dates[0],
                    signal_to=test_dates[-1],
                    work_dir=work_dir,
                    label=f"{label_prefix}_c{candidate_index}_i{window_index}",
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
                )
            )
        mean_selection_alpha = (
            statistics.fmean(
                float(result["summary"]["selection_alpha_ann"])
                for result in results
            )
            if results
            else None
        )
        if len(results) < min_objective_windows:
            # FAIL CLOSED. One evaluable window means pstdev == 0, so this candidate would pay no
            # stability penalty and win on a raw, unaveraged mean. Exclude it from selection
            # entirely rather than let a single window outrank a candidate measured three times.
            infeasible.append(
                {
                    "max_holding_days": hold,
                    "reason": INSUFFICIENT_WINDOWS,
                    "evaluation_windows": len(results),
                    "required": min_objective_windows,
                    "inner_folds": len(inner_folds),
                    "purge_signal_dates": purge_dates,
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
                    inner_folds=len(inner_folds),
                    evaluation_windows=len(results),
                    min_evaluation_windows=min_objective_windows,
                    selection_method=INSUFFICIENT_WINDOWS,
                    purge_signal_dates=purge_dates,
                    feasible=False,
                    mean_selection_alpha_ann=mean_selection_alpha,
                )
            )
            LOGGER.warning(
                "%s candidate hold=%d evaluated in only %d window(s) < %d required; "
                "EXCLUDED from selection (its unpenalised mean was %s)",
                label_prefix,
                hold,
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
                inner_folds=len(inner_folds),
                evaluation_windows=len(results),
                min_evaluation_windows=min_objective_windows,
                selection_method=selection_method,
                purge_signal_dates=purge_dates,
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
    window_excluded = [
        item for item in infeasible if item["reason"] == INSUFFICIENT_WINDOWS
    ]
    diagnostics: dict[str, Any] = {
        "candidates": len(candidates),
        "evaluable_candidates": sum(
            1 for objective, _ in objectives if math.isfinite(objective)
        ),
        "infeasible_candidates": infeasible,
        "minimum_objective_windows": min_objective_windows,
        "insufficient_window_candidates": len(window_excluded),
        "insufficient_window_holds": [
            int(item["max_holding_days"]) for item in window_excluded
        ],
        "best_objective": best_objective if math.isfinite(best_objective) else None,
        "tied_candidates": 0,
        "unstable_tie": False,
        "boundary_axes": [],
    }
    if best is None or not math.isfinite(best_objective):
        # Never fall back to a candidate that was excluded. If the whole stage is excluded, the
        # stage selects nothing and says exactly why.
        if not objectives and window_excluded:
            diagnostics["reason"] = (
                REASON_ALL_BELOW_MIN_WINDOWS
                if len(window_excluded) == len(candidates)
                else REASON_NO_EVALUABLE_AFTER_WINDOW_EXCLUSION
            )
            LOGGER.warning(
                "%s selected nothing: %d of %d candidates fell below the %d-window floor",
                label_prefix,
                len(window_excluded),
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
        diagnostics["unstable_tie"] = True
        diagnostics["reason"] = REASON_OBJECTIVE_TIE
        tied_holds = {int(candidate["max_holding_days"]) for candidate in tied}
        for row in grid_rows:
            if (
                row["selection_stage"] == label_prefix
                and int(row.get("feasible", 0)) == 1
                and int(row["max_holding_days"]) in tied_holds
            ):
                row["selection_unstable_tie"] = 1
        LOGGER.warning(
            "%s produced a %d-way objective tie at %.10f; no parameter selected",
            label_prefix,
            len(tied),
            best_objective,
        )
        return None, diagnostics
    # Boundary is judged against the FEASIBLE candidate set actually searched here.
    feasible_candidates = [candidate for _objective, candidate in objectives]
    diagnostics["boundary_axes"] = grid_boundary_axes(best, feasible_candidates)
    for row in reversed(grid_rows):
        if (
            row["selection_stage"] == label_prefix
            and int(row.get("feasible", 0)) == 1
            and row["max_holding_days"] == best["max_holding_days"]
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
    if (
        not panel_path.exists()
        or not panel_manifest_path.exists()
        or not execution_manifest_path.exists()
    ):
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
    calibration_cfg = cfg_get(config, "tactical_long_calibration", {}) or {}
    if (
        float(calibration_cfg.get("candidate_timeout_seconds", 900)) <= 0
        or int(calibration_cfg.get("candidate_max_attempts", 3)) < 1
        or float(calibration_cfg.get("candidate_retry_delay_seconds", 5)) < 0
    ):
        LOGGER.error("Invalid tactical-long candidate retry/timeout policy")
        return 1
    candidates = candidate_grid(config)
    work_root = paths.output_dir / ".tactical_long_calibration_work"
    try:
        cost_cfg = cfg_get(config, "tactical_long.long_costs", {}) or {}
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
    except (OSError, ValueError) as exc:
        LOGGER.error("Cannot snapshot market-positioning database: %s", exc)
        return 1
    cache_inputs = {
        "config.yaml": sha256_file(config_path),
        "market_positioning_snapshot.sqlite": snapshot_db_sha,
        "calibration_panel_manifest.json": sha256_file(panel_manifest_path),
        "execution_ohlcv_manifest.json": sha256_file(execution_manifest_path),
        "backtest/16g_tactical_long_replay.py": sha256_file(REPLAY_SCRIPT),
        "backtest/16h_calibrate_tactical_long.py": sha256_file(
            Path(__file__).resolve()
        ),
        "backtest/short_costs.py": sha256_file(
            PACKAGE_ROOT / "backtest" / "short_costs.py"
        ),
        "backtest/walkforward_common.py": sha256_file(
            PACKAGE_ROOT / "backtest" / "walkforward_common.py"
        ),
    }
    cache_key = hashlib.sha256(
        json.dumps(cache_inputs, sort_keys=True).encode("utf-8")
    ).hexdigest()[:20]
    work_dir = work_root / panel_dir.name / cache_key
    work_dir.mkdir(parents=True, exist_ok=True)
    signal_every = max(1, int(cfg_get(config, "tactical_long.signal_every_n_snapshots", 5)))
    max_holding_days = max(candidate["max_holding_days"] for candidate in candidates)
    # Compare every horizon on the same outcome-complete signal sample. Without
    # this common right-edge trim, shorter candidates receive later labels and
    # the 252-session candidate cannot be evaluated in the final outer block.
    observable_dates = dates[:-max_holding_days] if len(dates) > max_holding_days else []
    signal_dates = observable_dates[::signal_every]
    if not signal_dates:
        LOGGER.error("No outcome-complete signal dates for the requested holding grid")
        return 1
    embargo_signal_dates = int(calibration_cfg.get("embargo_signal_dates", 2))
    # Outer-fold PLANNING still uses the longest horizon so no outer test can ever see a label
    # from its own training block. Only the INNER selection purge is candidate specific.
    purge_signal_dates = candidate_purge_signal_dates(
        max_holding_days, signal_every=signal_every, embargo=embargo_signal_dates
    )
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
                )
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            LOGGER.error("Calibration smoke failed: %s", exc)
            return 1
        LOGGER.info(
            "TACTICAL LONG CALIBRATION SMOKE: PASS trades=%s alpha=%s parameters=%s",
            result["summary"]["trades"],
            result["summary"]["selection_alpha_ann"],
            candidates[0],
        )
        return 0

    outer_folds = expanding_blocks(
        signal_dates,
        folds=int(calibration_cfg.get("outer_folds", 5)),
        initial_fraction=float(calibration_cfg.get("outer_initial_fraction", 0.40)),
        minimum_train_dates=int(calibration_cfg.get("minimum_outer_train_dates", 60)),
        purge_dates=purge_signal_dates,
    )
    # FAIL CLOSED. Three outer folds cannot support a fold-consistency statistic, which is why
    # the previous run's 0.667 "consistency" over three folds was not evidence.
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
    out_dir = (
        paths.output_dir
        / str(calibration_cfg.get("dir", "tactical_long_calibration"))
        / f"{panel_dir.name}{str(args.output_suffix or '').strip()}"
    )
    grid_path = out_dir / "tactical_long_calibration_grid.csv"
    folds_path = out_dir / "tactical_long_outer_folds.csv"
    parameters_path = out_dir / "tactical_long_parameters.json"
    manifest_path = out_dir / "tactical_long_calibration_manifest.json"
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
    outer_infeasible: dict[str, list[dict[str, Any]]] = {}
    folds_without_selection: dict[str, str] = {}
    try:
        for outer_index, outer in enumerate(outer_folds):
            next_start = (
                outer_folds[outer_index + 1]["test_dates"][0]
                if outer_index + 1 < len(outer_folds)
                else None
            )
            # A candidate whose horizon leaves no non-overlapping OOS window in this fold cannot
            # be validated here, so it must not be selectable here either. This is a structural
            # constraint on the fold, not a data-driven filter.
            fold_candidates: list[dict[str, Any]] = []
            window_infeasible: list[dict[str, Any]] = []
            for candidate in candidates:
                trimmed = _non_overlapping_window(
                    outer["test_dates"],
                    next_window_start=next_start,
                    all_dates=signal_dates,
                    holding_days=int(candidate["max_holding_days"]),
                    signal_every=signal_every,
                )
                if trimmed:
                    fold_candidates.append(candidate)
                else:
                    window_infeasible.append(
                        {
                            "max_holding_days": int(candidate["max_holding_days"]),
                            "reason": "no_non_overlapping_outer_test_window",
                        }
                    )
            if window_infeasible:
                outer_infeasible.setdefault(str(outer["fold"]), []).extend(window_infeasible)
                LOGGER.warning(
                    "outer%d: %d candidate horizon(s) have no non-overlapping OOS window: %s",
                    outer["fold"],
                    len(window_infeasible),
                    [item["max_holding_days"] for item in window_infeasible],
                )
            if not fold_candidates:
                raise RuntimeError(
                    f"Outer fold {outer['fold']} has no candidate with a usable test window"
                )
            selected, diagnostics = _choose_candidate(
                candidates=fold_candidates,
                dates=outer["train_dates"],
                config_path=config_path,
                panel_build=panel_dir.name,
                calibration_cfg=calibration_cfg,
                signal_every=signal_every,
                embargo_signal_dates=embargo_signal_dates,
                work_dir=work_dir,
                label_prefix=f"outer{outer['fold']}",
                grid_rows=grid_rows,
                market_positioning_db=snapshot_db,
                market_positioning_db_sha256=snapshot_db_sha,
                cache_key=cache_key,
            )
            outer_infeasible.setdefault(str(outer["fold"]), []).extend(
                diagnostics["infeasible_candidates"]
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
                        "purged_signal_dates": len(outer["purged_dates"]),
                        "test_start": outer["test_dates"][0],
                        "test_end": outer["test_dates"][-1],
                        "max_holding_days": "",
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
            test_dates = _non_overlapping_window(
                outer["test_dates"],
                next_window_start=next_start,
                all_dates=signal_dates,
                holding_days=int(selected["max_holding_days"]),
                signal_every=signal_every,
            )
            if not test_dates:
                raise RuntimeError(
                    f"Outer fold {outer['fold']} has no non-overlapping test signals"
                )
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
            )
            outer_results.append(result)
            fold_rows.append(
                {
                    "outer_fold": outer["fold"],
                    "train_start": outer["train_dates"][0],
                    "train_end": outer["train_dates"][-1],
                    "purged_signal_dates": len(outer["purged_dates"]),
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
            signal_every=signal_every,
            embargo_signal_dates=embargo_signal_dates,
            work_dir=work_dir,
            label_prefix="full_development",
            grid_rows=grid_rows,
            market_positioning_db=snapshot_db,
            market_positioning_db_sha256=snapshot_db_sha,
            cache_key=cache_key,
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
    # HAC lag follows the longest SELECTED horizon: max(5, ceil(hold/5)). The prior lag was the
    # full 252-session grid maximum regardless of what was selected, which over-corrects as badly
    # as a hardcoded 5 under-corrects.
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
    promotion = cfg_get(config, "tactical_long.promotion", {}) or {}
    rejection_reasons: list[str] = []
    if len(trade_returns) < int(promotion.get("min_trades", 500)):
        rejection_reasons.append("insufficient_oos_trades")
    if oos_selection_ann <= float(promotion.get("min_selection_alpha_ann", 0.0)):
        rejection_reasons.append("oos_selection_alpha_not_positive")
    if active_t is None or active_t < float(promotion.get("min_active_t", 2.0)):
        rejection_reasons.append("oos_active_t_below_threshold")
    if profit_factor is None or profit_factor < float(promotion.get("min_profit_factor", 1.10)):
        rejection_reasons.append("oos_profit_factor_below_threshold")
    if positive_sectors < int(promotion.get("min_positive_sectors", 4)):
        rejection_reasons.append("oos_sector_breadth_below_threshold")
    if oos_stress_ann <= float(promotion.get("min_stress_net_ann", 0.0)):
        rejection_reasons.append("oos_stress_return_not_positive")
    minimum_ohlcv_coverage = min(
        float(result["summary"]["candidate_execution_ohlcv_fraction"])
        for result in outer_results
    )
    if minimum_ohlcv_coverage < float(
        promotion.get("min_candidate_execution_ohlcv_fraction", 0.95)
    ):
        rejection_reasons.append("oos_execution_ohlcv_coverage_below_threshold")
    parameter_counts = Counter(item["max_holding_days"] for item in selected_candidates)
    # A fold-consistency number computed on three folds is noise dressed as evidence. It is
    # published only once the configured floor of evaluable outer folds is met.
    min_folds_for_consistency = int(
        cfg_get(config, "tactical.min_outer_folds_for_consistency", 5)
    )
    consistency_publishable = evaluable_outer_folds >= min_folds_for_consistency
    dominant_fraction = max(parameter_counts.values()) / len(selected_candidates)
    if not consistency_publishable:
        rejection_reasons.append("fold_consistency_not_publishable")
    elif dominant_fraction < float(
        calibration_cfg.get("min_parameter_fold_consistency", 0.50)
    ):
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
        # A boundary selection means the searched grid may not contain the optimum. That is an
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
            "stage": "stage11_tactical_long_calibration",
            "generated_at": utc_now(),
            "acceptance": "PASS",
            "promotion_status": "PROMOTABLE" if promotable else "NOT_PROMOTABLE",
            "panel_build": panel_dir.name,
            "candidate_count": len(candidates),
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
            "candidates_excluded_insufficient_windows_by_fold": {
                fold: [
                    item
                    for item in items
                    if item.get("reason") == INSUFFICIENT_WINDOWS
                ]
                for fold, items in outer_infeasible.items()
                if any(item.get("reason") == INSUFFICIENT_WINDOWS for item in items)
            },
            "final_no_selection_reason": final_no_selection_reason or None,
            "infeasible_candidates_by_fold": outer_infeasible,
            "selected_at_grid_boundary": bool(final_boundary_axes) or bool(boundary_folds),
            "boundary_axes_by_fold": boundary_folds,
            "final_boundary_axes": final_boundary_axes,
            "final_selection_diagnostics": final_diagnostics,
            "active_t_hac_lag_days": oos_hac_lag,
            "purge_signal_dates_outer_planning": purge_signal_dates,
            "purge_signal_dates_inner": "candidate_specific: ceil(hold/signal_every)+embargo",
            "candidate_cache_key": cache_key,
            "inputs_sha256": {
                "config.yaml": sha256_file(config_path),
                "market_positioning_snapshot.sqlite": snapshot_db_sha,
                "backtest/16g_tactical_long_replay.py": sha256_file(REPLAY_SCRIPT),
                "backtest/16h_calibrate_tactical_long.py": sha256_file(
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
                folds_path.name: {
                    "sha256": sha256_file(folds_path),
                    "rows": len(fold_rows),
                },
                parameters_path.name: {"sha256": sha256_file(parameters_path)},
            },
        },
    )
    LOGGER.info(
        "TACTICAL LONG CALIBRATION: PASS / %s folds=%d candidates=%d "
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

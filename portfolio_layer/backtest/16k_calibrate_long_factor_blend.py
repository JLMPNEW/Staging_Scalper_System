#!/usr/bin/env python3
"""Nested long-only test of bounded residual factor blends.

Research/74 found marginal pillar signal, but its standalone-factor comparison and
top-minus-bottom spread do not answer whether a small additive tilt improves the
existing long-only composite. This campaign answers that narrower question.

For each frozen sector/pillar candidate, inner walk-forward folds choose an
unconditional residual-factor weight and, optionally, one heavily-shrunk
HEATING_UP increment. Outer folds compare the resulting score against the
unchanged composite through the executable 16g D+1-open replay. The eight
candidate-level OOS tests form one BH family. Development evidence can stop the
campaign, but it can never promote: Research/74 already viewed the full
development window, so confirmation must come from the sealed lockbox or future
prospective data.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import math
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

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
from portfolio_layer.research.stage11_common import (  # noqa: E402
    forward_status_is_valid,
    manifest_file_errors,
    mean_t_hac,
    rank_ic_of,
)


def _load_numbered_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_C72 = _load_numbered_module(
    "component_ic_for_long_blend",
    PACKAGE_ROOT / "research" / "72_component_ic_by_regime.py",
)
_C74 = _load_numbered_module(
    "factor_payoff_for_long_blend",
    PACKAGE_ROOT / "research" / "74_factor_payoff_diagnostics.py",
)
_C16H = _load_numbered_module(
    "tactical_long_calibration_for_blend",
    PACKAGE_ROOT / "backtest" / "16h_calibrate_tactical_long.py",
)


LOGGER = logging.getLogger("calibrate_long_factor_blend")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_CAMPAIGN = PACKAGE_ROOT / "research" / "LONG_ONLY_FACTOR_BLEND_CAMPAIGN.yaml"
REPLAY_SCRIPT = PACKAGE_ROOT / "backtest" / "16g_tactical_long_replay.py"
FACTOR_SCRIPT = PACKAGE_ROOT / "research" / "74_factor_payoff_diagnostics.py"
COMPONENT_SCRIPT = PACKAGE_ROOT / "research" / "72_component_ic_by_regime.py"
COMMON_SCRIPT = PACKAGE_ROOT / "research" / "stage11_common.py"


@dataclass(frozen=True)
class Candidate:
    source_pipeline: str
    component: str

    @property
    def key(self) -> str:
        return f"{self.source_pipeline}:{self.component}"


def _numeric_array(values: Any) -> np.ndarray:
    return np.asarray(pd.to_numeric(values, errors="coerce"), dtype=np.float64)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Nested executable long-only residual-factor blend calibration."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--campaign", type=Path, default=DEFAULT_CAMPAIGN)
    parser.add_argument("--panel-build", default=None)
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Build inputs and run one candidate on the last outer fold; never publish evidence.",
    )
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def parameter_grid(campaign: dict[str, Any]) -> list[tuple[float, float]]:
    base_values = sorted({float(value) for value in campaign["lambda_base_grid"]})
    heat_values = sorted(
        {float(value) for value in campaign["lambda_heating_increment_grid"]}
    )
    total_cap = float(campaign["lambda_total_cap"])
    heat_cap = float(campaign["lambda_heating_increment_cap"])
    output: list[tuple[float, float]] = []
    for base in base_values:
        for heat in heat_values:
            if base == 0.0 and heat != 0.0:
                continue
            if heat > heat_cap + 1e-12 or base + heat > total_cap + 1e-12:
                continue
            output.append((base, heat))
    if (0.0, 0.0) not in output or not output:
        raise ValueError("Blend grid must include the unchanged baseline")
    if any(base < 0.0 or heat < 0.0 for base, heat in output):
        raise ValueError("Blend weights must be non-negative")
    return output


def _residualize_factor(frame: pd.DataFrame, component: str) -> pd.Series:
    output = pd.Series(np.nan, index=frame.index, dtype=float)
    for _day, index in frame.groupby("as_of_date", sort=False).groups.items():
        index_list = list(index)
        sub = frame.loc[index_list]
        factor = _numeric_array(sub[component])
        composite = _numeric_array(sub["score_z_pipeline_date"])
        mask = np.isfinite(factor) & np.isfinite(composite)
        if int(mask.sum()) < 3:
            continue
        factor_z = np.asarray(
            _C72._zscore(pd.Series(factor[mask])),  # noqa: SLF001 - shared research primitive
            dtype=float,
        )
        controls = np.column_stack([np.ones(int(mask.sum())), composite[mask]])
        beta, *_ = np.linalg.lstsq(controls, factor_z, rcond=None)
        residual = factor_z - controls @ beta
        sd = float(np.std(residual, ddof=0))
        if not np.isfinite(sd) or sd <= 0.0:
            continue
        values = np.full(len(sub), np.nan, dtype=float)
        values[mask] = (residual - float(np.mean(residual))) / sd
        output.loc[index_list] = values
    return output


def _blend_scores(
    frame: pd.DataFrame, *, base_lambda: float, heating_increment: float
) -> np.ndarray:
    composite = _numeric_array(frame["score_z_pipeline_date"])
    residual = _numeric_array(frame["factor_residual_z"])
    heating = np.asarray(
        frame["macro_regime"].astype(str).eq("HEATING_UP"), dtype=np.float64
    )
    weight = base_lambda + heating_increment * heating
    return np.where(np.isfinite(residual), composite + weight * residual, composite)


def _mean_ic_delta(
    frame: pd.DataFrame,
    dates: list[str],
    *,
    base_lambda: float,
    heating_increment: float,
    target_column: str,
    status_column: str,
) -> float | None:
    wanted = frame.loc[frame["as_of_date"].isin(dates)].copy()
    if wanted.empty:
        return None
    wanted["candidate_score"] = _blend_scores(
        wanted,
        base_lambda=base_lambda,
        heating_increment=heating_increment,
    )
    deltas: list[float] = []
    for _day, sub in wanted.groupby("as_of_date", sort=True):
        status = np.asarray(
            sub[status_column].map(forward_status_is_valid), dtype=bool
        )
        target = _numeric_array(sub[target_column])
        baseline = _numeric_array(sub["score_z_pipeline_date"])
        candidate = _numeric_array(sub["candidate_score"])
        mask = status & np.isfinite(target) & np.isfinite(baseline) & np.isfinite(candidate)
        if int(mask.sum()) < 8:
            continue
        base_ic = rank_ic_of(baseline[mask], target[mask])
        candidate_ic = rank_ic_of(candidate[mask], target[mask])
        if base_ic is not None and candidate_ic is not None:
            deltas.append(candidate_ic - base_ic)
    return float(np.mean(deltas)) if deltas else None


def _choose_parameters(
    frame: pd.DataFrame,
    train_dates: list[str],
    *,
    grid: list[tuple[float, float]],
    campaign: dict[str, Any],
    target_column: str,
    status_column: str,
) -> tuple[tuple[float, float] | None, dict[str, Any]]:
    validation = campaign["validation"]
    purge = int(
        math.ceil(
            int(campaign["primary_horizon_days"])
            / int(campaign["signal_every_n_snapshots"])
        )
    ) + int(validation["embargo_signal_dates"])
    folds = _C16H.expanding_blocks(
        train_dates,
        folds=int(validation["inner_folds"]),
        initial_fraction=float(validation["inner_initial_fraction"]),
        minimum_train_dates=int(validation["minimum_inner_train_signal_dates"]),
        purge_dates=purge,
    )
    if len(folds) < int(validation["minimum_valid_inner_folds"]):
        return None, {"reason": "insufficient_inner_folds", "folds": len(folds)}
    scored: list[tuple[float, tuple[float, float], list[float]]] = []
    penalty = float(validation["stability_penalty"])
    for base, heat in grid:
        values = [
            _mean_ic_delta(
                frame,
                fold["test_dates"],
                base_lambda=base,
                heating_increment=heat,
                target_column=target_column,
                status_column=status_column,
            )
            for fold in folds
        ]
        finite = [float(value) for value in values if value is not None and np.isfinite(value)]
        if len(finite) != len(folds):
            continue
        objective = float(np.mean(finite) - penalty * np.std(finite, ddof=0))
        scored.append((objective, (base, heat), finite))
    if not scored:
        return None, {"reason": "no_evaluable_parameters", "folds": len(folds)}
    scored.sort(key=lambda item: item[0], reverse=True)
    best_objective = scored[0][0]
    tolerance = float(validation["objective_tie_tolerance"])
    tied = [item for item in scored if abs(item[0] - best_objective) <= tolerance]
    if len(tied) != 1:
        return None, {
            "reason": "objective_tie_no_selection",
            "folds": len(folds),
            "tied": len(tied),
        }
    selected = tied[0][1]
    return selected, {
        "reason": "selected",
        "folds": len(folds),
        "objective": best_objective,
        "fold_values": tied[0][2],
        "selected_baseline": selected == (0.0, 0.0),
        "selected_at_boundary": abs(sum(selected) - float(campaign["lambda_total_cap"]))
        <= 1e-12,
    }


def _write_override(
    replay_panel: pd.DataFrame,
    candidate_frame: pd.DataFrame,
    *,
    candidate: Candidate,
    parameters: tuple[float, float],
    panel_build: str,
    panel_sha256: str,
    work_dir: Path,
    label: str,
    campaign_sha256: str,
) -> Path:
    base, heat = parameters
    replacement = cast(
        pd.DataFrame, candidate_frame[["as_of_date", "ticker"]].copy()
    )
    replacement["score_z_pipeline_date"] = _blend_scores(
        candidate_frame,
        base_lambda=base,
        heating_increment=heat,
    )
    output = replay_panel[["as_of_date", "ticker", "score_z_pipeline_date"]].merge(
        replacement.rename(
            columns={"score_z_pipeline_date": "candidate_score_z"}
        ),
        on=["as_of_date", "ticker"],
        how="left",
        validate="one_to_one",
    )
    target_pipeline = replay_panel["source_pipeline"].eq(candidate.source_pipeline)
    has_candidate = output["candidate_score_z"].notna()
    output.loc[target_pipeline & has_candidate, "score_z_pipeline_date"] = output.loc[
        target_pipeline & has_candidate, "candidate_score_z"
    ]
    output = output[["as_of_date", "ticker", "score_z_pipeline_date"]].sort_values(
        by=["as_of_date", "ticker"]
    )  # pyright: ignore[reportCallIssue]
    csv_path = work_dir / f"{label}_scores.csv"
    manifest_path = work_dir / f"{label}_scores_manifest.json"
    output.to_csv(csv_path, index=False, lineterminator="\n")
    write_manifest(
        manifest_path,
        {
            "acceptance": "PASS",
            "purpose": "nested_long_only_factor_blend",
            "panel_build": panel_build,
            "calibration_panel_sha256": panel_sha256,
            "candidate": candidate.key,
            "parameters": {
                "lambda_base": base,
                "lambda_heating_increment": heat,
            },
            "campaign_sha256": campaign_sha256,
            "score_file": csv_path.name,
            "score_file_sha256": sha256_file(csv_path),
            "rows": len(output),
        },
    )
    return manifest_path


def _run_replay(
    *,
    config_path: Path,
    panel_build: str,
    signal_from: str,
    signal_to: str,
    work_dir: Path,
    label: str,
    market_db: Path,
    override_manifest: Path | None,
    holding_days: int,
) -> dict[str, Any]:
    parameter_path = work_dir / f"{label}_parameters.json"
    result_path = work_dir / f"{label}_result.json"
    expected_override_manifest_sha = (
        sha256_file(override_manifest) if override_manifest is not None else None
    )
    if result_path.exists():
        try:
            cached = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            cached = {}
        cached_override_hashes = dict(
            cached.get("score_override_inputs_sha256") or {}
        )
        override_current = (
            not cached_override_hashes
            if override_manifest is None
            else expected_override_manifest_sha
            in set(str(value) for value in cached_override_hashes.values())
        )
        if (
            cached.get("acceptance") == "PASS"
            and cached.get("signal_window")
            == {"from": signal_from, "to": signal_to}
            and cached.get("parameters", {}).get("max_holding_days") == holding_days
            and cached.get("source_sha256") == sha256_file(REPLAY_SCRIPT)
            and cached.get("market_positioning_db_sha256") == sha256_file(market_db)
            and override_current
        ):
            return cached
    write_manifest(
        parameter_path,
        {
            "acceptance": "PASS",
            "purpose": "long_only_factor_blend_fixed_horizon",
            "parameters": {"max_holding_days": holding_days},
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
        str(market_db),
    ]
    if override_manifest is not None:
        command.extend(["--score-override-manifest", str(override_manifest)])
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
            f"16g replay failed ({label}, rc={completed.returncode}): "
            f"{completed.stderr[-2000:]}"
        )
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    if payload.get("acceptance") != "PASS":
        raise RuntimeError(f"16g replay rejected ({label})")
    return payload


def _paired_returns(
    candidate: dict[str, Any], baseline: dict[str, Any], field: str
) -> dict[str, float]:
    candidate_dates = [str(value) for value in candidate["daily_selection_dates"]]
    baseline_dates = [str(value) for value in baseline["daily_selection_dates"]]
    candidate_values = [float(value) for value in candidate[field]]
    baseline_values = [float(value) for value in baseline[field]]
    left = dict(zip(candidate_dates, candidate_values, strict=True))
    right = dict(zip(baseline_dates, baseline_values, strict=True))
    common = sorted(set(left) & set(right))
    if not common:
        raise ValueError("Candidate and baseline have no paired replay dates")
    return {day: left[day] - right[day] for day in common}


def _selftest() -> None:
    campaign = {
        "lambda_base_grid": [0.0, 0.1, 0.25],
        "lambda_heating_increment_grid": [0.0, 0.05],
        "lambda_total_cap": 0.25,
        "lambda_heating_increment_cap": 0.05,
    }
    grid = parameter_grid(campaign)
    assert (0.0, 0.0) in grid
    assert all(base + heat <= 0.25 for base, heat in grid)
    assert (0.25, 0.05) not in grid and (0.0, 0.05) not in grid
    sample = pd.DataFrame(
        {
            "as_of_date": ["2020-01-01"] * 6,
            "score_z_pipeline_date": [-1.0, -0.5, 0.0, 0.1, 0.4, 1.0],
            "factor": [-2.0, -1.0, 0.5, 1.0, 1.5, 2.0],
        }
    )
    sample["factor_residual_z"] = _residualize_factor(sample, "factor")
    baseline = _blend_scores(sample.assign(macro_regime="ALL"), base_lambda=0.0, heating_increment=0.0)
    assert np.allclose(baseline, sample["score_z_pipeline_date"])
    assert np.isfinite(sample["factor_residual_z"]).all()
    flags = _C72.benjamini_hochberg([0.001, 0.8, 0.9], 0.10)
    assert flags == [True, False, False]
    print("long-only factor-blend self-test: PASS")


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
    panel_dir = paths.output_dir / str(
        cfg_get(config, "calibration_panel.dir", "calibration_panel")
    ) / panel_build
    panel_path = panel_dir / "calibration_panel.csv"
    panel_manifest_path = panel_dir / "calibration_panel_manifest.json"
    factor_dir = paths.output_dir / str(
        load_yaml(PACKAGE_ROOT / "research" / "FACTOR_PAYOFF_CAMPAIGN.yaml")[
            "output_dir"
        ]
    ) / panel_build
    factor_manifest_path = factor_dir / "factor_payoff_manifest.json"
    factor_primary_path = factor_dir / "factor_payoff_primary.csv"
    required = {
        "calibration_panel.csv": panel_path,
        "factor_payoff_manifest.json": factor_manifest_path,
        "factor_payoff_primary.csv": factor_primary_path,
    }
    if any(not path.exists() for path in required.values()):
        LOGGER.error("Missing campaign input(s): %s", [k for k, v in required.items() if not v.exists()])
        return 1
    panel_manifest = json.loads(panel_manifest_path.read_text(encoding="utf-8"))
    factor_manifest = json.loads(factor_manifest_path.read_text(encoding="utf-8"))
    if (
        panel_manifest.get("acceptance") != "PASS"
        or factor_manifest.get("acceptance") != "PASS"
        or str(factor_manifest.get("panel_build", "")) != panel_build
        or manifest_file_errors(
            panel_manifest, {"calibration_panel.csv": panel_path}
        )
        or sha256_file(panel_path)
        != str((factor_manifest.get("inputs_sha256") or {}).get("calibration_panel.csv", ""))
        or str(factor_manifest.get("campaign_id", ""))
        != str(campaign["factor_evidence_campaign"])
    ):
        LOGGER.error("Calibration/factor evidence is rejected or stale")
        return 1

    candidates = [
        Candidate(str(row["source_pipeline"]), str(row["component"]))
        for row in campaign["candidates"]
    ]
    if len({candidate.key for candidate in candidates}) != len(candidates):
        LOGGER.error("Frozen candidate list contains duplicates")
        return 1
    evidence = pd.read_csv(factor_primary_path)
    for candidate in candidates:
        matched = evidence.loc[
            (evidence["source_pipeline"] == candidate.source_pipeline)
            & (evidence["component"] == candidate.component)
            & (evidence["marginal_fdr_significant"].astype(int) == 1)
        ]
        if matched.empty:
            LOGGER.error("Frozen candidate lacks marginal-FDR evidence: %s", candidate.key)
            return 1

    wanted = {
        "as_of_date",
        "ticker",
        "source_pipeline",
        "score_z_pipeline_date",
        "calibration_research_eligible",
        "sidecar_stage11_eligible",
        "usable_for_promoted_training",
        "survivorship_complete",
        "in_lockbox",
        "macro_regime",
        f"excess_sector_{int(campaign['primary_horizon_days'])}d",
        f"fwd_status_{int(campaign['primary_horizon_days'])}d",
    }
    panel = pd.read_csv(panel_path, usecols=lambda column: column in wanted)
    missing_panel_columns = sorted(wanted - set(panel.columns))
    if missing_panel_columns:
        LOGGER.error(
            "Calibration panel is missing campaign columns: %s",
            missing_panel_columns,
        )
        return 1
    panel["as_of_date"] = panel["as_of_date"].astype(str).str.slice(0, 10)
    panel["ticker"] = panel["ticker"].astype(str).str.upper().str.strip()
    panel["score_z_pipeline_date"] = pd.to_numeric(
        panel["score_z_pipeline_date"], errors="coerce"
    )
    truthy = ("1", "1.0", "true", "True")
    eligible = panel["calibration_research_eligible"].astype(str).isin(truthy)
    if "sidecar_stage11_eligible" in panel:
        eligible |= panel["sidecar_stage11_eligible"].astype(str).isin(truthy)
    replay_panel = panel.loc[
        eligible
        & panel["survivorship_complete"].astype(str).isin(truthy)
        & ~panel["in_lockbox"].astype(str).isin(truthy)
    ].copy()
    if replay_panel.duplicated(["as_of_date", "ticker"]).any():
        LOGGER.error("Replay panel has duplicate date/ticker rows")
        return 1

    score_root = resolve_path(
        cfg_get(config, "score_contract.sector_output_root", "../output"),
        base_dir=config_path.parent,
    )
    sectors = {
        str(row["model_family"]): dict(row)
        for row in cfg_get(config, "score_contract.sectors", []) or []
    }
    candidate_frames: dict[str, pd.DataFrame] = {}
    pillar_hashes: dict[str, str] = {}
    minimum_coverage = float(campaign["validation"]["minimum_factor_coverage_fraction"])
    for candidate in candidates:
        base = replay_panel.loc[
            replay_panel["source_pipeline"] == candidate.source_pipeline
        ].copy()
        factor = _C72._load_pillar_frame(  # noqa: SLF001 - sealed shared loader
            sectors[candidate.source_pipeline],
            score_root,
            set(base["as_of_date"]),
            used_sha256=pillar_hashes,
            requested_pillars=[candidate.component],
        )
        merged = base.merge(
            factor[["as_of_date", "ticker", candidate.component]],
            on=["as_of_date", "ticker"],
            how="left",
            validate="one_to_one",
        )
        coverage = float(merged[candidate.component].notna().mean())
        if coverage < minimum_coverage:
            LOGGER.error(
                "%s usable coverage %.4f is below %.4f",
                candidate.key,
                coverage,
                minimum_coverage,
            )
            return 1
        merged["factor_residual_z"] = _residualize_factor(
            merged, candidate.component
        )
        candidate_frames[candidate.key] = merged

    signals = sorted(replay_panel["as_of_date"].unique())[
        :: int(campaign["signal_every_n_snapshots"])
    ]
    horizon = int(campaign["primary_horizon_days"])
    signals = signals[:-int(math.ceil(horizon / int(campaign["signal_every_n_snapshots"])))]
    validation = campaign["validation"]
    purge = int(
        math.ceil(horizon / int(campaign["signal_every_n_snapshots"]))
        + int(validation["embargo_signal_dates"])
    )
    outer_folds = _C16H.expanding_blocks(
        signals,
        folds=int(validation["outer_folds"]),
        initial_fraction=float(validation["outer_initial_fraction"]),
        minimum_train_dates=int(validation["minimum_outer_train_signal_dates"]),
        purge_dates=purge,
    )
    if len(outer_folds) != int(validation["outer_folds"]):
        LOGGER.error("Required outer folds did not materialize: %d", len(outer_folds))
        return 1
    if args.smoke:
        candidates = candidates[:1]
        outer_folds = outer_folds[-1:]

    market_db = resolve_path(
        str(
            cfg_get(
                config,
                "tactical_long.long_costs.market_positioning_db_path",
            )
        ),
        base_dir=config_path.parent,
    )
    if not market_db.exists():
        LOGGER.error("Market-positioning database is missing: %s", market_db)
        return 1
    market_db_hash_before = sha256_file(market_db)
    out_dir = paths.output_dir / "long_only_factor_blend" / panel_build
    work_dir = out_dir / "_work"
    if not args.smoke:
        outputs = [
            out_dir / "long_factor_blend_candidates.csv",
            out_dir / "long_factor_blend_outer_folds.csv",
            out_dir / "long_factor_blend_decision.json",
            out_dir / "long_factor_blend_manifest.json",
        ]
        if args.force:
            for path in outputs:
                if path.exists():
                    path.unlink()
        try:
            fail_if_exists(outputs, force=args.force)
        except FileExistsError as exc:
            LOGGER.error("%s", exc)
            return 1
    work_dir.mkdir(parents=True, exist_ok=True)
    grid = parameter_grid(campaign)
    target_column = f"excess_sector_{horizon}d"
    status_column = f"fwd_status_{horizon}d"
    campaign_hash = sha256_file(campaign_path)
    baseline_by_fold: dict[int, dict[str, Any]] = {}
    candidate_returns: dict[str, dict[str, float]] = {candidate.key: {} for candidate in candidates}
    candidate_stress: dict[str, dict[str, float]] = {candidate.key: {} for candidate in candidates}
    fold_rows: list[dict[str, Any]] = []
    boundary_candidates: set[str] = set()
    for fold in outer_folds:
        fold_number = int(fold["fold"])
        test_dates = list(fold["test_dates"])
        baseline = _run_replay(
            config_path=config_path,
            panel_build=panel_build,
            signal_from=test_dates[0],
            signal_to=test_dates[-1],
            work_dir=work_dir,
            label=f"outer{fold_number}_baseline",
            market_db=market_db,
            override_manifest=None,
            holding_days=horizon,
        )
        baseline_by_fold[fold_number] = baseline
        for candidate in candidates:
            selected, diagnostics = _choose_parameters(
                candidate_frames[candidate.key],
                list(fold["train_dates"]),
                grid=grid,
                campaign=campaign,
                target_column=target_column,
                status_column=status_column,
            )
            if selected is None or selected == (0.0, 0.0):
                baseline_dates = [
                    str(value) for value in baseline["daily_selection_dates"]
                ]
                candidate_returns[candidate.key].update(
                    dict.fromkeys(baseline_dates, 0.0)
                )
                candidate_stress[candidate.key].update(
                    dict.fromkeys(baseline_dates, 0.0)
                )
                fold_rows.append(
                    {
                        "outer_fold": fold_number,
                        "candidate": candidate.key,
                        "lambda_base": "" if selected is None else selected[0],
                        "lambda_heating_increment": "" if selected is None else selected[1],
                        "selection_status": diagnostics["reason"],
                        "selected_at_boundary": 0,
                        "active_ann": 0.0,
                        "stress_active_ann": 0.0,
                    }
                )
                continue
            if bool(diagnostics.get("selected_at_boundary")):
                boundary_candidates.add(candidate.key)
            override = _write_override(
                replay_panel,
                candidate_frames[candidate.key],
                candidate=candidate,
                parameters=selected,
                panel_build=panel_build,
                panel_sha256=sha256_file(panel_path),
                work_dir=work_dir,
                label=f"outer{fold_number}_{candidate.source_pipeline}_{candidate.component}",
                campaign_sha256=campaign_hash,
            )
            result = _run_replay(
                config_path=config_path,
                panel_build=panel_build,
                signal_from=test_dates[0],
                signal_to=test_dates[-1],
                work_dir=work_dir,
                label=f"outer{fold_number}_{candidate.source_pipeline}_{candidate.component}",
                market_db=market_db,
                override_manifest=override,
                holding_days=horizon,
            )
            paired = _paired_returns(
                result, baseline, "daily_selection_returns"
            )
            stress = _paired_returns(result, baseline, "daily_stress_returns")
            candidate_returns[candidate.key].update(paired)
            candidate_stress[candidate.key].update(stress)
            years = max(len(paired) / 252.0, 1e-9)
            fold_rows.append(
                {
                    "outer_fold": fold_number,
                    "candidate": candidate.key,
                    "lambda_base": selected[0],
                    "lambda_heating_increment": selected[1],
                    "selection_status": diagnostics["reason"],
                    "selected_at_boundary": int(
                        bool(diagnostics.get("selected_at_boundary"))
                    ),
                    "active_ann": sum(paired.values()) / years,
                    "stress_active_ann": sum(stress.values()) / years,
                }
            )

    if sha256_file(market_db) != market_db_hash_before:
        LOGGER.error("Market-positioning database changed during the campaign")
        return 1
    if args.smoke:
        LOGGER.info("LONG FACTOR BLEND SMOKE: PASS rows=%d", len(fold_rows))
        return 0

    inference = campaign["inference"]
    candidate_rows: list[dict[str, Any]] = []
    pvalues: list[float | None] = []
    for candidate in candidates:
        values_by_date = candidate_returns[candidate.key]
        stress_by_date = candidate_stress[candidate.key]
        values = [values_by_date[day] for day in sorted(values_by_date)]
        stress_values = [stress_by_date[day] for day in sorted(stress_by_date)]
        years = max(len(values) / 252.0, 1e-9)
        active_ann = sum(values) / years if values else 0.0
        stress_ann = sum(stress_values) / years if stress_values else 0.0
        _mean, _se, active_t = mean_t_hac(values, max_lag=26) if values else (None, None, None)
        bootstrap = _C74.circular_block_mean_stats(
            values,
            confidence=float(inference["bootstrap_confidence"]),
            replications=int(inference["bootstrap_replications"]),
            block_length=min(
                len(values),
                int(inference["bootstrap_block_length_trading_days"]),
            ),
            seed=int(inference["bootstrap_seed"])
            + candidates.index(candidate),
        ) if values else (None, None, None)
        pvalue = None if bootstrap[2] is None else float(bootstrap[2])
        pvalues.append(pvalue)
        positive_folds = sum(
            1
            for row in fold_rows
            if row["candidate"] == candidate.key
            and row["active_ann"] != ""
            and float(row["active_ann"]) > 0.0
        )
        candidate_rows.append(
            {
                "candidate": candidate.key,
                "oos_paired_days": len(values),
                "oos_active_ann": active_ann,
                "oos_stress_active_ann": stress_ann,
                "active_t_hac26": "" if active_t is None else active_t,
                "bootstrap_ci_low_daily": "" if bootstrap[0] is None else bootstrap[0],
                "bootstrap_ci_high_daily": "" if bootstrap[1] is None else bootstrap[1],
                "bootstrap_p_one_sided": "" if pvalue is None else pvalue,
                "positive_outer_folds": positive_folds,
                "baseline_outer_folds": sum(
                    1
                    for fold_row in fold_rows
                    if fold_row["candidate"] == candidate.key
                    and (
                        fold_row["lambda_base"] in ("", 0.0)
                        and fold_row["lambda_heating_increment"] in ("", 0.0)
                    )
                ),
                "selected_at_boundary": int(candidate.key in boundary_candidates),
                "fdr_significant": 0,
                "development_pass": 0,
                "rejection_reasons": "",
            }
        )
    flags = _C72.benjamini_hochberg(
        pvalues, float(inference["fdr_alpha"])
    )
    for row, significant in zip(candidate_rows, flags, strict=True):
        row["fdr_significant"] = int(significant)
        reasons: list[str] = []
        if not significant:
            reasons.append("not_fdr_significant")
        if float(row["oos_active_ann"]) <= float(inference["minimum_net_active_ann"]):
            reasons.append("net_active_not_positive")
        if row["active_t_hac26"] == "" or float(row["active_t_hac26"]) < float(
            inference["minimum_active_t"]
        ):
            reasons.append("active_t_below_threshold")
        if float(row["oos_stress_active_ann"]) <= float(
            inference["minimum_stress_active_ann"]
        ):
            reasons.append("stress_active_not_positive")
        if int(row["positive_outer_folds"]) < int(
            inference["minimum_outer_folds_positive"]
        ):
            reasons.append("outer_fold_breadth_below_threshold")
        if int(row["selected_at_boundary"]):
            reasons.append("lambda_selected_at_grid_boundary")
        row["rejection_reasons"] = ";".join(reasons)
        row["development_pass"] = int(not reasons)

    passing = [row for row in candidate_rows if int(row["development_pass"]) == 1]
    if len(passing) > 1:
        passing.sort(key=lambda row: float(row["oos_active_ann"]), reverse=True)
        selected_candidate = str(passing[0]["candidate"])
        decision = "AWAITING_SEALED_CONFIRMATION"
    elif len(passing) == 1:
        selected_candidate = str(passing[0]["candidate"])
        decision = "AWAITING_SEALED_CONFIRMATION"
    else:
        selected_candidate = ""
        decision = "STOP_FACTOR_REWEIGHTING_CURRENT_SIGNAL_STACK"

    candidates_path = out_dir / "long_factor_blend_candidates.csv"
    folds_path = out_dir / "long_factor_blend_outer_folds.csv"
    decision_path = out_dir / "long_factor_blend_decision.json"
    manifest_path = out_dir / "long_factor_blend_manifest.json"
    write_csv(candidates_path, list(candidate_rows[0]), candidate_rows)
    write_csv(folds_path, list(fold_rows[0]), fold_rows)
    write_manifest(
        decision_path,
        {
            "acceptance": "PASS",
            "research_decision": decision,
            "selected_candidate": selected_candidate,
            "development_passing_candidates": [row["candidate"] for row in passing],
            "confirmation_source": campaign["confirmation"]["source"],
            "promotable": False,
            "reason": (
                "Development evidence only; Research/74 already viewed the full development window."
                if passing
                else campaign["terminal_rule"]["if_development_fails"]
            ),
        },
    )
    write_manifest(
        manifest_path,
        {
            "acceptance": "PASS",
            "stage": "stage11_long_only_factor_blend",
            "campaign_id": campaign["campaign_id"],
            "panel_build": panel_build,
            "generated_at": utc_now(),
            "research_decision": decision,
            "selected_candidate": selected_candidate,
            "production_modified": False,
            "lockbox_opened": False,
            "candidate_family_size": len(candidates),
            "outer_folds": len(outer_folds),
            "defense_spread_warning": (
                "Research/74 defense 45.6% is a linear annualization of overlapping "
                "126-day label spreads, not a realizable replay return."
            ),
            "inputs_sha256": {
                "config.yaml": sha256_file(config_path),
                campaign_path.name: campaign_hash,
                "calibration_panel.csv": sha256_file(panel_path),
                "calibration_panel_manifest.json": sha256_file(panel_manifest_path),
                "factor_payoff_primary.csv": sha256_file(factor_primary_path),
                "factor_payoff_manifest.json": sha256_file(factor_manifest_path),
                "market_positioning.sqlite": market_db_hash_before,
                "backtest/16g_tactical_long_replay.py": sha256_file(REPLAY_SCRIPT),
                "backtest/16k_calibrate_long_factor_blend.py": sha256_file(
                    Path(__file__).resolve()
                ),
                "backtest/16h_calibrate_tactical_long.py": sha256_file(
                    PACKAGE_ROOT / "backtest" / "16h_calibrate_tactical_long.py"
                ),
                "research/72_component_ic_by_regime.py": sha256_file(COMPONENT_SCRIPT),
                "research/74_factor_payoff_diagnostics.py": sha256_file(FACTOR_SCRIPT),
                "research/stage11_common.py": sha256_file(COMMON_SCRIPT),
                **{
                    f"pillar_source:{path}": digest
                    for path, digest in sorted(pillar_hashes.items())
                },
            },
            "files": {
                candidates_path.name: {
                    "sha256": sha256_file(candidates_path),
                    "rows": len(candidate_rows),
                },
                folds_path.name: {
                    "sha256": sha256_file(folds_path),
                    "rows": len(fold_rows),
                },
                decision_path.name: {"sha256": sha256_file(decision_path)},
            },
        },
    )
    LOGGER.info(
        "LONG FACTOR BLEND: PASS / %s passing=%d selected=%s",
        decision,
        len(passing),
        selected_candidate or "none",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Stage 11 factor-payoff screen: stop unless existing pillars add incremental value.

This is the deliberately bounded first step before any score-weight calibration. It consumes the
sealed Research/72 component evidence and the same PIT pillar sources, then measures, at the frozen
126-session horizon:

* standalone rank IC,
* marginal IC after removing the contemporaneous composite,
* marginal IC after removing the other configured pillars,
* paired component-minus-composite IC,
* top-minus-bottom quantile spread after a conservative screening cost,
* monotonicity, rank persistence, turnover, half-sample stability and redundancy.

Primary inference is limited to the pre-registered excess-sector target in ALL and HEATING_UP.
Paired circular-block bootstrap tests respect overlapping labels; a single BH family covers every
primary marginal and component-minus-composite hypothesis. Diagnostic targets and horizons cannot
make the continue decision pass.

The script emits evidence only. A PASS acceptance means the screen ran reproducibly, not that a
factor passed. ``research_decision=STOP_NO_INCREMENTAL_FACTORS`` is a successful negative result
that prevents the more expensive weight-calibration campaign.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import logging
import math
import sys
from collections import defaultdict
from collections.abc import Sequence
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
    write_manifest,
    write_via_temp,
)
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.research.stage11_common import (  # noqa: E402
    calibration_admission_mask,
    forward_status_is_valid,
    independent_windows,
    load_lockbox,
    manifest_file_errors,
    manifest_input_errors,
    mean_t_hac,
    rank_ic_of,
)


_SPEC72 = importlib.util.spec_from_file_location(
    "component_ic_mod",
    PACKAGE_ROOT / "research" / "72_component_ic_by_regime.py",
)
assert _SPEC72 is not None and _SPEC72.loader is not None
_C72 = importlib.util.module_from_spec(_SPEC72)
_SPEC72.loader.exec_module(_C72)


LOGGER = logging.getLogger("factor_payoff_diagnostics")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_CAMPAIGN = PACKAGE_ROOT / "research" / "FACTOR_PAYOFF_CAMPAIGN.yaml"
DEFAULT_PIPELINES = [
    "semiconductors",
    "software_infrastructure",
    "technology_hardware",
    "biotech",
    "med_devices",
    "defense",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stage 11 marginal factor-payoff diagnostics."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--campaign", type=Path, default=DEFAULT_CAMPAIGN)
    parser.add_argument("--panel-build", default=None)
    parser.add_argument("--pipelines", default=",".join(DEFAULT_PIPELINES))
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def _latest_build(root: Path, wanted: str | None) -> Path | None:
    if wanted:
        candidate = root / wanted
        return (
            candidate
            if (candidate / "calibration_panel_manifest.json").exists()
            else None
        )
    if not root.exists():
        return None
    builds = sorted(
        path
        for path in root.iterdir()
        if path.is_dir()
        and (path / "calibration_panel_manifest.json").exists()
    )
    return builds[-1] if builds else None


def _target_column(kind: str, horizon: int) -> str:
    mapping = {
        "excess_sector": f"excess_sector_{horizon}d",
        "resid_sector": f"resid_sector_{horizon}d",
    }
    if kind not in mapping:
        raise ValueError(f"Unsupported factor-payoff target: {kind}")
    return mapping[kind]


def _stable_seed(base_seed: int, *parts: str) -> int:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).digest()
    return (int(base_seed) + int.from_bytes(digest[:4], "big")) % (2**32 - 1)


def circular_block_mean_stats(
    values: Sequence[float],
    *,
    block_length: int,
    confidence: float,
    replications: int,
    seed: int,
) -> tuple[float | None, float | None, float | None]:
    """Return nonparametric (CI low, CI high, one-sided null p) for a dependent mean."""
    data = np.asarray(values, dtype=float)
    data = data[np.isfinite(data)]
    if (
        len(data) < 3
        or block_length < 1
        or not 0.0 < confidence < 1.0
        or replications < 100
    ):
        return None, None, None
    block = min(int(block_length), len(data))
    blocks_needed = int(math.ceil(len(data) / block))
    offsets = np.arange(block)
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, len(data), size=(replications, blocks_needed))
    indices = (
        (starts[:, :, None] + offsets[None, None, :]) % len(data)
    ).reshape(replications, -1)[:, : len(data)]
    sampled_means = data[indices].mean(axis=1)
    tail = (1.0 - confidence) / 2.0
    ci_low = float(np.quantile(sampled_means, tail))
    ci_high = float(np.quantile(sampled_means, 1.0 - tail))

    observed = float(data.mean())
    centered = data - observed
    null_means = centered[indices].mean(axis=1)
    p_one_sided = float(
        (1 + int(np.count_nonzero(null_means >= observed)))
        / (replications + 1)
    )
    return ci_low, ci_high, p_one_sided


def _residualize(values: np.ndarray, controls: np.ndarray) -> np.ndarray | None:
    """Cross-sectional residual of values on contemporaneous controls plus an intercept."""
    if values.ndim != 1 or controls.ndim != 2 or len(values) != len(controls):
        return None
    if len(values) < controls.shape[1] + 3:
        return None
    design = np.column_stack([np.ones(len(values)), controls])
    try:
        coefficient, *_rest = np.linalg.lstsq(design, values, rcond=None)
    except np.linalg.LinAlgError:
        return None
    residual = values - design @ coefficient
    if not np.isfinite(residual).all() or float(np.std(residual)) <= 0.0:
        return None
    return residual


def _quantile_metrics(
    score: np.ndarray,
    outcome: np.ndarray,
    *,
    quantile_count: int,
    top_fraction: float,
) -> tuple[float | None, float | None]:
    """Top-minus-bottom spread and monotonicity of quantile mean outcomes."""
    mask = np.isfinite(score) & np.isfinite(outcome)
    score = score[mask]
    outcome = outcome[mask]
    if len(score) < max(quantile_count * 2, 6):
        return None, None
    order = np.argsort(score, kind="mergesort")
    bucket_indices = np.array_split(order, quantile_count)
    bucket_means = np.asarray(
        [float(outcome[index].mean()) for index in bucket_indices],
        dtype=float,
    )
    monotonicity = rank_ic_of(
        np.arange(len(bucket_means), dtype=float),
        bucket_means,
    )
    tail = max(1, int(math.ceil(len(score) * top_fraction)))
    spread = float(
        outcome[order[-tail:]].mean() - outcome[order[:tail]].mean()
    )
    return spread, monotonicity


def _half_means(values: list[float]) -> tuple[float | None, float | None]:
    clean = np.asarray([value for value in values if np.isfinite(value)], dtype=float)
    if len(clean) < 2:
        return None, None
    midpoint = len(clean) // 2
    if midpoint == 0 or midpoint == len(clean):
        return None, None
    return float(clean[:midpoint].mean()), float(clean[midpoint:].mean())


def _mean_or_none(values: list[float]) -> float | None:
    clean = [value for value in values if np.isfinite(value)]
    return float(np.mean(clean)) if clean else None


def _numeric(values: pd.Series) -> np.ndarray:
    numeric = pd.Series(pd.to_numeric(values, errors="coerce"), index=values.index)
    return np.asarray(numeric, dtype=float)


def _screen_pass(row: dict[str, Any], campaign: dict[str, Any]) -> tuple[bool, list[str]]:
    inference = dict(campaign.get("inference") or {})
    minimum_windows = int(campaign["min_independent_windows"])
    minimum_complete = float(campaign["min_complete_case_fraction"])
    minimum_delta = float(inference["minimum_delta_vs_composite_ic"])
    minimum_spread = float(inference["minimum_net_quantile_spread_ann"])
    require_halves = bool(inference["screen_requires_positive_halves"])
    reasons: list[str] = []
    if int(row["independent_windows"]) < minimum_windows:
        reasons.append("insufficient_independent_windows")
    if float(row["mean_complete_case_fraction"]) < minimum_complete:
        reasons.append("complete_case_coverage_below_floor")
    if float(row["marginal_composite_ic"]) <= 0.0:
        reasons.append("marginal_ic_not_positive")
    if row["marginal_bootstrap_ci_low"] == "" or float(
        row["marginal_bootstrap_ci_low"]
    ) <= 0.0:
        reasons.append("marginal_bootstrap_lower_bound_not_positive")
    if int(row["marginal_fdr_significant"]) != 1:
        reasons.append("marginal_not_fdr_significant")
    if float(row["delta_vs_composite_ic"]) <= minimum_delta:
        reasons.append("delta_vs_composite_below_floor")
    if row["delta_bootstrap_ci_low"] == "" or float(
        row["delta_bootstrap_ci_low"]
    ) <= minimum_delta:
        reasons.append("delta_bootstrap_lower_bound_not_positive")
    if int(row["delta_fdr_significant"]) != 1:
        reasons.append("delta_not_fdr_significant")
    if float(row["net_quantile_spread_ann"]) < minimum_spread:
        reasons.append("net_quantile_spread_below_economic_hurdle")
    if require_halves and (
        row["marginal_half1_ic"] == ""
        or row["marginal_half2_ic"] == ""
        or float(row["marginal_half1_ic"]) <= 0.0
        or float(row["marginal_half2_ic"]) <= 0.0
        or row["delta_half1_ic"] == ""
        or row["delta_half2_ic"] == ""
        or float(row["delta_half1_ic"]) <= minimum_delta
        or float(row["delta_half2_ic"]) <= minimum_delta
    ):
        reasons.append("chronological_half_stability_failed")
    return not reasons, reasons


def _selftest() -> None:
    rng = np.random.default_rng(74)
    common = rng.standard_normal(400)
    unique = rng.standard_normal(400)
    factor = 0.8 * common + 0.6 * unique
    outcome = 0.3 * unique + rng.standard_normal(400) * 0.2
    residual = _residualize(factor, common[:, None])
    assert residual is not None
    marginal_ic = rank_ic_of(residual, outcome)
    assert marginal_ic is not None and marginal_ic > 0.5, marginal_ic
    spread, monotonicity = _quantile_metrics(
        factor,
        outcome,
        quantile_count=5,
        top_fraction=0.20,
    )
    assert spread is not None and spread > 0.0
    assert monotonicity is not None and monotonicity > 0.0
    dependent_positive = list(
        np.repeat(rng.normal(0.03, 0.005, size=40), 3)
    )
    stats_a = circular_block_mean_stats(
        dependent_positive,
        block_length=5,
        confidence=0.90,
        replications=500,
        seed=74,
    )
    stats_b = circular_block_mean_stats(
        dependent_positive,
        block_length=5,
        confidence=0.90,
        replications=500,
        seed=74,
    )
    assert stats_a == stats_b
    assert stats_a[0] is not None and stats_a[0] > 0.0
    mask = _C72.benjamini_hochberg([1e-5, 0.6, 0.8], 0.10)
    assert mask == [True, False, False]
    print("factor-payoff diagnostics self-test: PASS")


def main() -> int:  # noqa: C901, PLR0915
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
    try:
        lockbox = load_lockbox(config, config_path)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1

    governance = dict(campaign.get("governance") or {})
    if (
        not bool(governance.get("evidence_only"))
        or bool(governance.get("may_modify_scoring"))
        or bool(governance.get("may_modify_production"))
        or bool(governance.get("lockbox_may_be_opened"))
    ):
        LOGGER.error("Factor-payoff campaign governance is not fail-closed: %s", governance)
        return 1

    pipelines = [
        pipeline.strip()
        for pipeline in str(args.pipelines).split(",")
        if pipeline.strip()
    ]
    primary_horizon = int(campaign["primary_horizon_days"])
    primary_target = str(campaign["primary_target"])
    diagnostic_targets = [
        str(value) for value in campaign.get("diagnostic_targets", [])
    ]
    target_kinds = list(dict.fromkeys([primary_target, *diagnostic_targets]))
    primary_regimes = [str(value) for value in campaign["primary_regimes"]]
    min_cross_section = int(campaign["min_cross_section"])
    min_dates = int(campaign["min_dates"])
    minimum_factor_coverage = float(campaign["min_complete_case_fraction"])
    quantile_count = int(campaign["quantile_count"])
    top_fraction = float(campaign["top_fraction"])
    bootstrap = dict(campaign["bootstrap"])
    confidence = float(bootstrap["confidence"])
    replications = int(bootstrap["replications"])
    base_seed = int(bootstrap["seed"])
    block_length = int(bootstrap["block_length_trading_days"])
    inference = dict(campaign["inference"])
    fdr_alpha = float(inference["fdr_alpha"])
    screening_cost = float(
        dict(campaign["economics"])["screening_round_trip_cost_bps"]
    ) / 1e4
    max_entry_lag = int(
        cfg_get(config, "calibration_targets.max_entry_lag_trading_days", 5)
    )

    panel_root = paths.output_dir / str(
        cfg_get(config, "calibration_panel.dir", "calibration_panel")
    )
    panel_dir = _latest_build(panel_root, args.panel_build)
    if panel_dir is None:
        LOGGER.error("No calibration-panel build under %s", panel_root)
        return 1
    panel_manifest_path = panel_dir / "calibration_panel_manifest.json"
    panel_path = panel_dir / "calibration_panel.csv"
    panel_manifest = json.loads(panel_manifest_path.read_text(encoding="utf-8"))
    panel_errors = manifest_file_errors(
        panel_manifest,
        {"calibration_panel.csv": panel_path},
    )
    panel_input_errors = manifest_input_errors(
        panel_manifest,
        {
            "config.yaml": config_path,
            "research/67_join_calibration_panel.py": (
                PACKAGE_ROOT / "research" / "67_join_calibration_panel.py"
            ),
            "research/stage11_common.py": (
                PACKAGE_ROOT / "research" / "stage11_common.py"
            ),
        },
    )
    if (
        panel_manifest.get("acceptance") != "PASS"
        or panel_errors
        or panel_input_errors
    ):
        LOGGER.error(
            "Calibration panel is unaccepted or stale: files=%s inputs=%s",
            panel_errors,
            panel_input_errors,
        )
        return 1

    component_dir = (
        paths.output_dir
        / str(cfg_get(config, "component_ic.dir", "component_ic"))
        / panel_dir.name
    )
    component_manifest_path = component_dir / "component_ic_manifest.json"
    component_cells_path = component_dir / "component_ic.csv"
    component_coverage_path = component_dir / "component_coverage.csv"
    component_usable_path = component_dir / "component_usable_coverage.csv"
    required_component_files = {
        "component_ic.csv": component_cells_path,
        "component_coverage.csv": component_coverage_path,
        "component_usable_coverage.csv": component_usable_path,
    }
    if not component_manifest_path.exists():
        LOGGER.error("Matching Research/72 manifest is missing for %s", panel_dir.name)
        return 1
    component_manifest = json.loads(
        component_manifest_path.read_text(encoding="utf-8")
    )
    component_errors = manifest_file_errors(
        component_manifest,
        required_component_files,
    )
    component_input_errors = manifest_input_errors(
        component_manifest,
        {
            "config.yaml": config_path,
            "research/72_component_ic_by_regime.py": (
                PACKAGE_ROOT / "research" / "72_component_ic_by_regime.py"
            ),
            "research/stage11_common.py": (
                PACKAGE_ROOT / "research" / "stage11_common.py"
            ),
            "calibration_panel_manifest.json": panel_manifest_path,
            "calibration_panel.csv": panel_path,
        },
    )
    if (
        component_manifest.get("acceptance") != "PASS"
        or str(component_manifest.get("panel_build", "")) != panel_dir.name
        or list(component_manifest.get("pipelines") or []) != pipelines
        or str(component_manifest.get("protocol_sha256", ""))
        != lockbox["protocol_sha256"]
        or component_errors
        or component_input_errors
    ):
        LOGGER.error(
            "Research/72 evidence is stale or inconsistent: files=%s inputs=%s",
            component_errors,
            component_input_errors,
        )
        return 1

    output_root = paths.output_dir / str(campaign["output_dir"])
    out_dir = output_root / panel_dir.name
    primary_path = out_dir / "factor_payoff_primary.csv"
    dates_path = out_dir / "factor_payoff_date_metrics.csv"
    decay_path = out_dir / "factor_decay.csv"
    redundancy_path = out_dir / "factor_redundancy.csv"
    manifest_path = out_dir / "factor_payoff_manifest.json"
    output_paths = [
        primary_path,
        dates_path,
        decay_path,
        redundancy_path,
        manifest_path,
    ]
    if args.force:
        for path in output_paths:
            if path.exists():
                path.unlink()
    try:
        fail_if_exists(output_paths, force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    required_columns = {
        "as_of_date",
        "ticker",
        "source_pipeline",
        "macro_regime",
        "score_z_pipeline_date",
        "calibration_research_eligible",
        "sidecar_stage11_eligible",
        "usable_for_promoted_training",
        "survivorship_complete",
        "in_lockbox",
        f"fwd_status_{primary_horizon}d",
    }
    target_columns = {
        _target_column(kind, primary_horizon) for kind in target_kinds
    }
    required_columns.update(target_columns)
    header = pd.read_csv(panel_path, nrows=0)
    missing_columns = sorted(required_columns - set(header.columns))
    if missing_columns:
        LOGGER.error("Calibration panel missing factor-payoff columns: %s", missing_columns)
        return 1
    panel = pd.read_csv(
        panel_path,
        usecols=lambda column: column in required_columns,
    )
    panel["ticker"] = panel["ticker"].astype(str).str.upper().str.strip()
    panel["as_of_date"] = panel["as_of_date"].astype(str).str.slice(0, 10)
    lockbox_rows = (
        panel["in_lockbox"]
        .astype(str)
        .str.strip()
        .str.lower()
        .isin(("1", "1.0", "true", "yes"))
    )
    admission = calibration_admission_mask(panel) & panel["source_pipeline"].isin(
        pipelines
    )
    admitted_lockbox_rows = int((admission & lockbox_rows).sum())
    panel = panel.loc[admission].copy()
    if admitted_lockbox_rows:
        LOGGER.error("Admitted lockbox rows detected: %d", admitted_lockbox_rows)
        return 1
    if panel.empty:
        LOGGER.error("No admitted rows for factor-payoff screen")
        return 1

    score_root = resolve_path(
        cfg_get(config, "score_contract.sector_output_root", "../output"),
        base_dir=config_path.parent,
    )
    sectors_config = {
        str(sector.get("model_family")): dict(sector)
        for sector in cfg_get(config, "score_contract.sectors", []) or []
    }
    sealed_pillar_sets = {
        str(pipe): [str(value) for value in values]
        for pipe, values in (component_manifest.get("pillar_sets") or {}).items()
    }
    component_inputs = dict(component_manifest.get("inputs_sha256") or {})
    pillar_sources_sha256: dict[str, str] = {}
    date_rows: list[dict[str, Any]] = []
    redundancy_rows: list[dict[str, Any]] = []
    turnover_values: dict[tuple[str, str], list[float]] = defaultdict(list)
    persistence_values: dict[tuple[str, str], list[float]] = defaultdict(list)
    complete_case_by_pipeline: dict[str, list[float]] = defaultdict(list)

    for pipe in pipelines:
        if pipe not in sectors_config or pipe not in sealed_pillar_sets:
            LOGGER.error("Missing sector/pillar contract for %s", pipe)
            return 1
        sub = panel.loc[panel["source_pipeline"] == pipe].copy()
        if sub.empty:
            LOGGER.error("No admitted rows for required pipeline %s", pipe)
            return 1
        pillars = sealed_pillar_sets[pipe]
        pillar_frame = _C72._load_pillar_frame(
            sectors_config[pipe],
            score_root,
            set(sub["as_of_date"].unique()),
            used_sha256=pillar_sources_sha256,
            requested_pillars=pillars,
        )
        if pillar_frame.empty:
            LOGGER.error("No pillar frame for %s", pipe)
            return 1
        actual_pillars = [
            column
            for column in pillar_frame.columns
            if column not in ("ticker", "as_of_date")
        ]
        if set(actual_pillars) != set(pillars):
            LOGGER.error(
                "Pillar set drift for %s: expected=%s actual=%s",
                pipe,
                pillars,
                actual_pillars,
            )
            return 1
        pillar_frame = pillar_frame[["ticker", "as_of_date", *pillars]]
        merged = sub.merge(
            pillar_frame,
            on=["as_of_date", "ticker"],
            how="left",
            validate="one_to_one",
        )
        for pillar in pillars:
            merged[pillar] = pd.to_numeric(merged[pillar], errors="coerce")
            merged[f"{pillar}__z"] = merged.groupby("as_of_date")[pillar].transform(
                _C72._zscore
            )
        merged["composite__z"] = pd.to_numeric(
            merged["score_z_pipeline_date"],
            errors="coerce",
        )

        z_columns = [f"{pillar}__z" for pillar in pillars]
        pooled_z = merged[z_columns].to_numpy(dtype=float)
        for index, pillar in enumerate(pillars):
            value = pooled_z[:, index]
            other = np.delete(pooled_z, index, axis=1)
            mask = np.isfinite(value) & np.all(np.isfinite(other), axis=1)
            vif: float | None = None
            if int(mask.sum()) > other.shape[1] + 3 and other.shape[1] > 0:
                residual = _residualize(value[mask], other[mask])
                if residual is not None:
                    variance = float(np.var(value[mask]))
                    residual_variance = float(np.var(residual))
                    if variance > 0.0 and residual_variance > 0.0:
                        vif = variance / residual_variance
            pair_correlations: list[float] = []
            for other_index, _other_pillar in enumerate(pillars):
                if other_index == index:
                    continue
                pair_mask = np.isfinite(value) & np.isfinite(pooled_z[:, other_index])
                if int(pair_mask.sum()) >= 3:
                    correlation = float(
                        np.corrcoef(
                            value[pair_mask],
                            pooled_z[pair_mask, other_index],
                        )[0, 1]
                    )
                    if np.isfinite(correlation):
                        pair_correlations.append(correlation)
            redundancy_rows.append(
                {
                    "source_pipeline": pipe,
                    "component": pillar,
                    "observations": int(np.isfinite(value).sum()),
                    "vif": "" if vif is None else round(vif, 6),
                    "max_abs_pair_correlation": (
                        ""
                        if not pair_correlations
                        else round(max(abs(value) for value in pair_correlations), 6)
                    ),
                    "mean_abs_pair_correlation": (
                        ""
                        if not pair_correlations
                        else round(float(np.mean(np.abs(pair_correlations))), 6)
                    ),
                }
            )

        previous_top: dict[str, set[str]] = {}
        previous_scores: dict[str, pd.Series] = {}
        for as_of, cross_section in merged.groupby("as_of_date", sort=True):
            cross_section = cross_section.copy()
            regimes = cross_section["macro_regime"].dropna().astype(str)
            regime = str(regimes.mode().iloc[0]) if not regimes.empty else ""
            active_z_columns: list[str] = []
            for z_column in z_columns:
                values = _numeric(cross_section[z_column])
                finite = values[np.isfinite(values)]
                if (
                    len(values) > 0
                    and len(finite) >= min_cross_section
                    and len(finite) / len(values) >= minimum_factor_coverage
                    and float(np.std(finite, ddof=0)) > 0.0
                ):
                    active_z_columns.append(z_column)
            complete_case_by_pipeline[pipe].append(
                len(active_z_columns) / max(1, len(z_columns))
            )

            for pillar, z_column in zip(pillars, z_columns, strict=True):
                if z_column not in active_z_columns:
                    continue
                score_series = pd.Series(
                    _numeric(cross_section[z_column]),
                    index=cross_section["ticker"].astype(str),
                    dtype=float,
                ).dropna()
                tail_count = max(1, int(math.ceil(len(score_series) * top_fraction)))
                top_names = {
                    str(ticker) for ticker in score_series.nlargest(tail_count).index
                }
                key = (pipe, pillar)
                if pillar in previous_top:
                    denominator = max(1, len(previous_top[pillar] | top_names))
                    turnover_values[key].append(
                        1.0
                        - len(previous_top[pillar] & top_names) / denominator
                    )
                if pillar in previous_scores:
                    common = sorted(
                        set(previous_scores[pillar].index) & set(score_series.index)
                    )
                    if len(common) >= 3:
                        persistence = rank_ic_of(
                            previous_scores[pillar].loc[common].to_numpy(dtype=float),
                            score_series.loc[common].to_numpy(dtype=float),
                        )
                        if persistence is not None:
                            persistence_values[key].append(persistence)
                previous_top[pillar] = top_names
                previous_scores[pillar] = score_series

            for target_kind in target_kinds:
                target_column = _target_column(target_kind, primary_horizon)
                outcome = _numeric(cross_section[target_column])
                status_ok = cross_section[
                    f"fwd_status_{primary_horizon}d"
                ].map(forward_status_is_valid).to_numpy(dtype=bool)
                outcome[~status_ok] = np.nan
                composite = _numeric(cross_section["composite__z"])
                for index, (pillar, z_column) in enumerate(
                    zip(pillars, z_columns, strict=True)
                ):
                    if z_column not in active_z_columns:
                        continue
                    component = _numeric(cross_section[z_column])
                    label_base = np.isfinite(composite) & np.isfinite(outcome)
                    matched = (
                        np.isfinite(component)
                        & label_base
                    )
                    if int(matched.sum()) < min_cross_section:
                        continue
                    factor_usable_fraction = int(matched.sum()) / max(
                        1,
                        int(label_base.sum()),
                    )
                    component_matched = component[matched]
                    composite_matched = composite[matched]
                    outcome_matched = outcome[matched]
                    standalone_ic = rank_ic_of(
                        component_matched,
                        outcome_matched,
                    )
                    composite_ic = rank_ic_of(
                        composite_matched,
                        outcome_matched,
                    )
                    residual_composite = _residualize(
                        component_matched,
                        composite_matched[:, None],
                    )
                    marginal_composite_ic = (
                        None
                        if residual_composite is None
                        else rank_ic_of(residual_composite, outcome_matched)
                    )
                    control_columns = [
                        column
                        for column in active_z_columns
                        if column != z_column
                    ]
                    controls = cross_section[control_columns].to_numpy(dtype=float)
                    all_matched = (
                        np.isfinite(component)
                        & np.isfinite(outcome)
                        & np.all(np.isfinite(controls), axis=1)
                    )
                    marginal_other_ic: float | None = None
                    if int(all_matched.sum()) >= max(
                        min_cross_section,
                        controls.shape[1] + 3,
                    ):
                        residual_other = _residualize(
                            component[all_matched],
                            controls[all_matched],
                        )
                        if residual_other is not None:
                            marginal_other_ic = rank_ic_of(
                                residual_other,
                                outcome[all_matched],
                            )
                    quantile_spread, monotonicity = _quantile_metrics(
                        component_matched,
                        outcome_matched,
                        quantile_count=quantile_count,
                        top_fraction=top_fraction,
                    )
                    if (
                        standalone_ic is None
                        or composite_ic is None
                        or marginal_composite_ic is None
                    ):
                        continue
                    date_rows.append(
                        {
                            "source_pipeline": pipe,
                            "component": pillar,
                            "as_of_date": str(as_of),
                            "macro_regime": regime,
                            "target_kind": target_kind,
                            "n_names": int(matched.sum()),
                            "complete_case_fraction": round(
                                factor_usable_fraction,
                                6,
                            ),
                            "standalone_ic": standalone_ic,
                            "composite_ic": composite_ic,
                            "delta_vs_composite_ic": standalone_ic - composite_ic,
                            "marginal_composite_ic": marginal_composite_ic,
                            "marginal_other_pillars_ic": (
                                ""
                                if marginal_other_ic is None
                                else marginal_other_ic
                            ),
                            "quantile_spread_gross": (
                                ""
                                if quantile_spread is None
                                else quantile_spread
                            ),
                            "quantile_spread_net_screen": (
                                ""
                                if quantile_spread is None
                                else quantile_spread - screening_cost
                            ),
                            "quantile_monotonicity": (
                                "" if monotonicity is None else monotonicity
                            ),
                        }
                    )

    for source, digest in pillar_sources_sha256.items():
        expected = str(component_inputs.get(f"pillar_source:{source}", ""))
        if not expected or expected != digest:
            LOGGER.error(
                "Pillar source is absent from or differs from Research/72 seal: %s",
                source,
            )
            return 1

    date_frame = pd.DataFrame(date_rows)
    if date_frame.empty:
        LOGGER.error("No factor-payoff date metrics were produced")
        return 1
    date_frame.sort_values(
        ["target_kind", "source_pipeline", "component", "as_of_date"],
        inplace=True,
    )

    aggregate_rows: list[dict[str, Any]] = []
    for target_kind in target_kinds:
        target_data = date_frame.loc[date_frame["target_kind"] == target_kind]
        for pipe in pipelines:
            pipe_data = target_data.loc[target_data["source_pipeline"] == pipe]
            for component in sealed_pillar_sets[pipe]:
                component_data = pipe_data.loc[pipe_data["component"] == component]
                for regime in primary_regimes:
                    cell = (
                        component_data
                        if regime == "ALL"
                        else component_data.loc[
                            component_data["macro_regime"] == regime
                        ]
                    )
                    if len(cell) < min_dates:
                        continue
                    cell = cell.sort_values("as_of_date")
                    dates = cell["as_of_date"].astype(str).tolist()
                    marginal = _numeric(cell["marginal_composite_ic"])
                    marginal_other = _numeric(cell["marginal_other_pillars_ic"])
                    delta = _numeric(cell["delta_vs_composite_ic"])
                    standalone = _numeric(cell["standalone_ic"])
                    composite = _numeric(cell["composite_ic"])
                    net_spread = _numeric(cell["quantile_spread_net_screen"])
                    monotonicity = _numeric(cell["quantile_monotonicity"])
                    promotional_family = int(target_kind == primary_target)
                    marginal_ci_low: float | str = ""
                    marginal_ci_high: float | str = ""
                    marginal_p: float | str = ""
                    delta_ci_low: float | str = ""
                    delta_ci_high: float | str = ""
                    delta_p: float | str = ""
                    if promotional_family:
                        marginal_stats = circular_block_mean_stats(
                            list(marginal),
                            block_length=block_length,
                            confidence=confidence,
                            replications=replications,
                            seed=_stable_seed(
                                base_seed,
                                pipe,
                                component,
                                regime,
                                target_kind,
                                "marginal",
                            ),
                        )
                        delta_stats = circular_block_mean_stats(
                            list(delta),
                            block_length=block_length,
                            confidence=confidence,
                            replications=replications,
                            seed=_stable_seed(
                                base_seed,
                                pipe,
                                component,
                                regime,
                                target_kind,
                                "delta",
                            ),
                        )
                        marginal_ci_low = (
                            "" if marginal_stats[0] is None else marginal_stats[0]
                        )
                        marginal_ci_high = (
                            "" if marginal_stats[1] is None else marginal_stats[1]
                        )
                        marginal_p = (
                            "" if marginal_stats[2] is None else marginal_stats[2]
                        )
                        delta_ci_low = (
                            "" if delta_stats[0] is None else delta_stats[0]
                        )
                        delta_ci_high = (
                            "" if delta_stats[1] is None else delta_stats[1]
                        )
                        delta_p = "" if delta_stats[2] is None else delta_stats[2]
                    marginal_half1, marginal_half2 = _half_means(list(marginal))
                    delta_half1, delta_half2 = _half_means(list(delta))
                    marginal_mean, _marginal_se, marginal_hac_t = mean_t_hac(
                        [float(value) for value in marginal if np.isfinite(value)],
                        max_lag=max(1, block_length - 1),
                    )
                    delta_mean, _delta_se, delta_hac_t = mean_t_hac(
                        [float(value) for value in delta if np.isfinite(value)],
                        max_lag=max(1, block_length - 1),
                    )
                    mean_net_spread = _mean_or_none(list(net_spread))
                    aggregate_rows.append(
                        {
                            "source_pipeline": pipe,
                            "component": component,
                            "target_kind": target_kind,
                            "horizon_days": primary_horizon,
                            "regime": regime,
                            "promotional_family": promotional_family,
                            "n_dates": len(cell),
                            "independent_windows": independent_windows(
                                dates,
                                primary_horizon,
                                entry_lag_trading_days=max_entry_lag,
                            ),
                            "mean_complete_case_fraction": round(
                                float(cell["complete_case_fraction"].mean()),
                                6,
                            ),
                            "standalone_ic": round(
                                float(np.nanmean(standalone)),
                                6,
                            ),
                            "composite_ic": round(
                                float(np.nanmean(composite)),
                                6,
                            ),
                            "delta_vs_composite_ic": (
                                ""
                                if delta_mean is None
                                else round(delta_mean, 6)
                            ),
                            "delta_hac_t": (
                                ""
                                if delta_hac_t is None
                                else round(delta_hac_t, 4)
                            ),
                            "delta_bootstrap_ci_low": (
                                ""
                                if delta_ci_low == ""
                                else round(float(delta_ci_low), 6)
                            ),
                            "delta_bootstrap_ci_high": (
                                ""
                                if delta_ci_high == ""
                                else round(float(delta_ci_high), 6)
                            ),
                            "delta_bootstrap_p_one_sided": delta_p,
                            "delta_half1_ic": (
                                ""
                                if delta_half1 is None
                                else round(delta_half1, 6)
                            ),
                            "delta_half2_ic": (
                                ""
                                if delta_half2 is None
                                else round(delta_half2, 6)
                            ),
                            "marginal_composite_ic": (
                                ""
                                if marginal_mean is None
                                else round(marginal_mean, 6)
                            ),
                            "marginal_other_pillars_ic": round(
                                float(np.nanmean(marginal_other)),
                                6,
                            ),
                            "marginal_hac_t": (
                                ""
                                if marginal_hac_t is None
                                else round(marginal_hac_t, 4)
                            ),
                            "marginal_bootstrap_ci_low": (
                                ""
                                if marginal_ci_low == ""
                                else round(float(marginal_ci_low), 6)
                            ),
                            "marginal_bootstrap_ci_high": (
                                ""
                                if marginal_ci_high == ""
                                else round(float(marginal_ci_high), 6)
                            ),
                            "marginal_bootstrap_p_one_sided": marginal_p,
                            "marginal_half1_ic": (
                                ""
                                if marginal_half1 is None
                                else round(marginal_half1, 6)
                            ),
                            "marginal_half2_ic": (
                                ""
                                if marginal_half2 is None
                                else round(marginal_half2, 6)
                            ),
                            "net_quantile_spread_ann": (
                                ""
                                if mean_net_spread is None
                                else round(
                                    mean_net_spread * 252.0 / primary_horizon,
                                    6,
                                )
                            ),
                            "quantile_monotonicity": round(
                                float(np.nanmean(monotonicity)),
                                6,
                            ),
                            "mean_top_bucket_turnover": round(
                                _mean_or_none(
                                    turnover_values[(pipe, component)]
                                )
                                or 0.0,
                                6,
                            ),
                            "mean_rank_persistence": round(
                                _mean_or_none(
                                    persistence_values[(pipe, component)]
                                )
                                or 0.0,
                                6,
                            ),
                            "marginal_fdr_significant": 0,
                            "delta_fdr_significant": 0,
                            "factor_pass": 0,
                            "rejection_reasons": "",
                        }
                    )

    primary_indices = [
        index
        for index, row in enumerate(aggregate_rows)
        if int(row["promotional_family"]) == 1
    ]
    family_pvalues: list[float | None] = []
    family_keys: list[tuple[int, str]] = []
    for index in primary_indices:
        row = aggregate_rows[index]
        for metric, key in (
            ("marginal", "marginal_bootstrap_p_one_sided"),
            ("delta", "delta_bootstrap_p_one_sided"),
        ):
            raw = row[key]
            family_pvalues.append(
                None if raw == "" or not np.isfinite(float(raw)) else float(raw)
            )
            family_keys.append((index, metric))
    family_flags = _C72.benjamini_hochberg(family_pvalues, fdr_alpha)
    for (index, metric), flag in zip(family_keys, family_flags, strict=True):
        aggregate_rows[index][f"{metric}_fdr_significant"] = int(flag)

    for index in primary_indices:
        passed, reasons = _screen_pass(aggregate_rows[index], campaign)
        aggregate_rows[index]["factor_pass"] = int(passed)
        aggregate_rows[index]["rejection_reasons"] = ";".join(reasons)

    passing_rows = [
        row for row in aggregate_rows if int(row["factor_pass"]) == 1
    ]
    passing_factors = {
        (str(row["source_pipeline"]), str(row["component"]))
        for row in passing_rows
    }
    passing_sectors = {str(row["source_pipeline"]) for row in passing_rows}
    stop_rule = dict(campaign["stop_rule"])
    continue_research = (
        len(passing_factors) >= int(stop_rule["minimum_passing_factors"])
        and len(passing_sectors) >= int(stop_rule["minimum_passing_sectors"])
    )
    research_decision = str(
        stop_rule["pass_decision"]
        if continue_research
        else stop_rule["fail_decision"]
    )

    component_cells = pd.read_csv(component_cells_path)
    diagnostic_horizons = [
        int(value) for value in campaign["diagnostic_horizons_days"]
    ]
    decay_frame = component_cells.loc[
        (component_cells["component"] != "composite")
        & (component_cells["regime"] == "ALL")
        & component_cells["horizon_days"].astype(int).isin(diagnostic_horizons)
    ].copy()

    complete_case_summary = {
        pipe: {
            "mean": round(float(np.mean(values)), 6) if values else 0.0,
            "minimum": round(float(np.min(values)), 6) if values else 0.0,
            "dates": len(values),
        }
        for pipe, values in complete_case_by_pipeline.items()
    }
    minimum_complete = minimum_factor_coverage
    bad_complete_case = {
        pipe: values
        for pipe, values in complete_case_summary.items()
        if values["mean"] < minimum_complete
    }
    checks = [
        {
            "check": "upstream_component_evidence_current",
            "status": "PASS",
            "detail": (
                f"panel={panel_dir.name}; Research/72 acceptance=PASS; "
                "data and producer hashes current"
            ),
        },
        {
            "check": "lockbox_no_admitted_rows",
            "status": "PASS" if admitted_lockbox_rows == 0 else "FAIL",
            "detail": f"admitted_lockbox_rows={admitted_lockbox_rows}",
        },
        {
            "check": "active_pillar_breadth",
            "status": "PASS" if not bad_complete_case else "WARN",
            "detail": (
                f"configured-pillar active-share reference={minimum_complete:.3f}; "
                f"by_pipeline={complete_case_summary}; each factor is separately "
                "hard-gated on usable cross-section coverage"
            ),
        },
        {
            "check": "primary_hypothesis_family_pre_registered",
            "status": "PASS" if family_keys else "FAIL",
            "detail": (
                f"family={inference['family']}; tests={len(family_keys)}; "
                f"fdr_alpha={fdr_alpha}; bootstrap_reps={replications}; "
                f"block_length={block_length}"
            ),
        },
        {
            "check": "research_stop_rule_applied",
            "status": "PASS",
            "detail": (
                f"decision={research_decision}; passing_factors={len(passing_factors)}; "
                f"passing_sectors={len(passing_sectors)}"
            ),
        },
        {
            "check": "evidence_only",
            "status": "PASS",
            "detail": (
                "no score weights, sector outputs, production config, or lockbox "
                "artifacts were modified"
            ),
        },
    ]
    accepted = all(check["status"] in ("PASS", "WARN") for check in checks)

    out_dir.mkdir(parents=True, exist_ok=True)
    write_via_temp(
        primary_path,
        lambda temp: pd.DataFrame(aggregate_rows).to_csv(temp, index=False),
    )
    write_via_temp(
        dates_path,
        lambda temp: date_frame.to_csv(temp, index=False),
    )
    write_via_temp(
        decay_path,
        lambda temp: decay_frame.to_csv(temp, index=False),
    )
    write_via_temp(
        redundancy_path,
        lambda temp: pd.DataFrame(redundancy_rows).to_csv(temp, index=False),
    )
    write_manifest(
        manifest_path,
        {
            "stage": "stage11_factor_payoff_diagnostics",
            "generated_at": utc_now(),
            "acceptance": "PASS" if accepted else "FAIL",
            "research_decision": research_decision,
            "campaign_id": campaign["campaign_id"],
            "panel_build": panel_dir.name,
            "protocol_sha256": lockbox["protocol_sha256"],
            "primary_horizon_days": primary_horizon,
            "primary_target": primary_target,
            "primary_regimes": primary_regimes,
            "pipelines": pipelines,
            "hypothesis_tests": len(family_keys),
            "passing_cells": len(passing_rows),
            "passing_factors": sorted(
                f"{pipe}:{component}" for pipe, component in passing_factors
            ),
            "passing_sectors": sorted(passing_sectors),
            "complete_case_summary": complete_case_summary,
            "checks": checks,
            "inputs_sha256": {
                "config.yaml": sha256_file(config_path),
                "research/FACTOR_PAYOFF_CAMPAIGN.yaml": sha256_file(campaign_path),
                "research/74_factor_payoff_diagnostics.py": sha256_file(
                    Path(__file__).resolve()
                ),
                "research/72_component_ic_by_regime.py": sha256_file(
                    PACKAGE_ROOT / "research" / "72_component_ic_by_regime.py"
                ),
                "research/stage11_common.py": sha256_file(
                    PACKAGE_ROOT / "research" / "stage11_common.py"
                ),
                "calibration_panel_manifest.json": sha256_file(panel_manifest_path),
                "calibration_panel.csv": sha256_file(panel_path),
                "component_ic_manifest.json": sha256_file(component_manifest_path),
                "component_ic.csv": sha256_file(component_cells_path),
                "component_coverage.csv": sha256_file(component_coverage_path),
                "component_usable_coverage.csv": sha256_file(
                    component_usable_path
                ),
                **{
                    f"pillar_source:{source}": digest
                    for source, digest in sorted(pillar_sources_sha256.items())
                },
            },
            "files": {
                "factor_payoff_primary.csv": {
                    "sha256": sha256_file(primary_path),
                    "rows": len(aggregate_rows),
                },
                "factor_payoff_date_metrics.csv": {
                    "sha256": sha256_file(dates_path),
                    "rows": len(date_frame),
                },
                "factor_decay.csv": {
                    "sha256": sha256_file(decay_path),
                    "rows": len(decay_frame),
                },
                "factor_redundancy.csv": {
                    "sha256": sha256_file(redundancy_path),
                    "rows": len(redundancy_rows),
                },
            },
        },
    )
    for check in checks:
        LOGGER.info(
            "[%s] %s -- %s",
            check["status"],
            check["check"],
            check["detail"],
        )
    for row in sorted(
        passing_rows,
        key=lambda value: -float(value["net_quantile_spread_ann"]),
    ):
        LOGGER.info(
            "PASSING FACTOR %s %s regime=%s marginal_ic=%s delta=%s net_spread_ann=%s",
            row["source_pipeline"],
            row["component"],
            row["regime"],
            row["marginal_composite_ic"],
            row["delta_vs_composite_ic"],
            row["net_quantile_spread_ann"],
        )
    LOGGER.info(
        "FACTOR PAYOFF: acceptance=%s decision=%s passing_factors=%d -> %s",
        "PASS" if accepted else "FAIL",
        research_decision,
        len(passing_factors),
        out_dir,
    )
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())

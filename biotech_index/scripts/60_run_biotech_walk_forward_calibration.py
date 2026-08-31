#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import json
import logging
import math
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.calibration_provenance import observation_scoring_config_hash  # noqa: E402
from biotech_index.core.calibration_metrics import (  # noqa: E402
    MetricSettings,
    equal_weight_returns_by_date,
    finite_float,
    paired_policy_comparison,
)
from biotech_index.core.calibration_splits import (  # noqa: E402
    WalkForwardFold,
    WalkForwardWindow,
    build_expanding_walk_forward_folds,
    partition_rows_for_fold,
    validate_fold_support,
)
from biotech_index.core.cohort_calibration import (  # noqa: E402
    BIOTECH_CALIBRATION_COHORTS,
    policy_supports_cohort,
    rows_for_cohort,
)
from biotech_index.core.config import cfg_get, load_yaml, resolve_optional_path, resolve_path  # noqa: E402
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402
from biotech_index.core.promotion_contract import (  # noqa: E402
    PromotionContractError,
    validate_contract_scoring_parity,
    validate_monitoring_contract,
)
from biotech_index.core.promotion_policy import (  # noqa: E402
    PromotionDecision,
    PromotionRules,
    apply_deployment_readiness_gate,
    apply_no_harm_gate,
    decide_promotion,
    deployment_active_weight,
    no_harm_reason_codes,
)
from biotech_index.core.portfolio_validation import (  # noqa: E402
    validation_candidate_survives_multimetric,
)
from biotech_index.core.score_reliability import (  # noqa: E402
    ReliabilityRecord,
    ReliabilityThreshold,
    active_weight_for_class,
    apply_reliability_threshold,
    blend_active_alpha_with_benchmark,
    build_reliability_curve,
    records_from_rows,
    reliability_class_from_metrics,
    select_reliability_threshold,
)


LOGGER = logging.getLogger("biotech_walk_forward_calibration")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CALIBRATION_SOURCE_PATH = PACKAGE_ROOT / "scripts" / "28_calibrate_biotech_opportunity.py"
FRAMEWORK_VERSION = "biotech_nested_walk_forward_v2"
PROMOTION_CONTRACT_VERSION = "biotech_promotion_contract_v1"


@dataclass(frozen=True)
class FrameworkSettings:
    output_dir: Path
    primary_horizon: int
    top_ns: tuple[int, ...]
    candidate_pool_top_n: int
    validation_shortlist_size: int
    max_workers: int
    executor_kind: str
    windows: Mapping[int, WalkForwardWindow]
    score_pct_candidates: tuple[float, ...]
    max_name_candidates: tuple[int, ...]
    active_weight_by_class: Mapping[str, object]
    metric_settings: MetricSettings
    promotion_rules: PromotionRules
    monitoring_contract: Mapping[str, object]
    optuna_enabled: bool
    optuna_trials: int
    optuna_seed: int


@dataclass(frozen=True)
class CandidateEvaluation:
    candidate_id: str
    spec: Any
    policy: Any
    top_n: int
    records: tuple[ReliabilityRecord, ...]
    threshold: ReliabilityThreshold


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Nested, purged expanding walk-forward calibration and promotion authorization for biotech. "
            "Candidate selection and optional Optuna tuning use train/validation only; outer-test rows are "
            "evaluated only after the candidate contract is frozen."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--observations-csv", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--candidate-limit", type=int, default=0)
    parser.add_argument("--max-workers", type=int, default=None)
    parser.add_argument(
        "--cohort-filter",
        choices=BIOTECH_CALIBRATION_COHORTS,
        default=None,
        help="Calibrate one official cohort independently while retaining the shared full-panel fold plan.",
    )
    parser.add_argument(
        "--no-survivor-fallback",
        choices=("xbi", "production_incumbent"),
        default="xbi",
        help=(
            "Policy used when train/validation produces no challenger. Cohort calibration must use "
            "production_incumbent so one weak cohort cannot remove the entire biotech sleeve."
        ),
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--run-optuna", action="store_true")
    parser.add_argument("--no-optuna", dest="run_optuna", action="store_false")
    parser.set_defaults(run_optuna=None)
    return parser.parse_args()


def _int_values(raw: object, default: Iterable[int]) -> tuple[int, ...]:
    values = raw if isinstance(raw, (list, tuple, set)) else default
    clean = sorted({int(float(value)) for value in values if int(float(value)) > 0})
    if not clean:
        raise ValueError("Expected at least one positive integer value")
    return tuple(clean)


def _float_values(raw: object, default: Iterable[float]) -> tuple[float, ...]:
    values = raw if isinstance(raw, (list, tuple, set)) else default
    clean = sorted({float(value) for value in values if math.isfinite(float(value))})
    if not clean:
        raise ValueError("Expected at least one finite numeric value")
    return tuple(clean)


def _bool(raw: object, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def load_framework_settings(config: dict[str, Any], config_path: Path, args: argparse.Namespace) -> FrameworkSettings:
    base_dir = config_path.parent
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir is not None
        else resolve_path(
            cfg_get(
                config,
                "calibration.walk_forward.output_dir",
                "../output/biotech_index_reports/calibration_walk_forward",
            ),
            base_dir=base_dir,
        )
    )
    raw_windows = cfg_get(config, "calibration.walk_forward.windows", {}) or {}
    if not isinstance(raw_windows, dict):
        raise ValueError("calibration.walk_forward.windows must be a mapping")
    defaults = {
        20: {"validation_months": 6, "test_months": 6, "step_months": 6, "embargo_days": 40},
        60: {"validation_months": 12, "test_months": 12, "step_months": 12, "embargo_days": 100},
        120: {"validation_months": 18, "test_months": 18, "step_months": 18, "embargo_days": 185},
    }
    min_training_years = int(cfg_get(config, "calibration.walk_forward.min_training_years", 3))
    windows: dict[int, WalkForwardWindow] = {}
    for horizon, default in defaults.items():
        raw = raw_windows.get(str(horizon), raw_windows.get(horizon, {})) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"calibration.walk_forward.windows.{horizon} must be a mapping")
        windows[horizon] = WalkForwardWindow(
            horizon_bars=horizon,
            validation_months=int(raw.get("validation_months", default["validation_months"])),
            test_months=int(raw.get("test_months", default["test_months"])),
            step_months=int(raw.get("step_months", default["step_months"])),
            embargo_days=int(raw.get("embargo_days", default["embargo_days"])),
            min_training_years=min_training_years,
            min_train_dates=int(raw.get("min_train_dates", 24)),
            min_validation_dates=int(raw.get("min_validation_dates", 6)),
            min_test_dates=int(raw.get("min_test_dates", 6)),
        )
    metric_raw = cfg_get(config, "calibration.walk_forward.metrics", {}) or {}
    promotion_raw = cfg_get(config, "calibration.walk_forward.promotion", {}) or {}
    optuna_raw = cfg_get(config, "calibration.walk_forward.optuna", {}) or {}
    monitoring_raw = cfg_get(config, "calibration.walk_forward.monitoring", {}) or {}
    active_weight_raw = cfg_get(
        config,
        "calibration.walk_forward.adaptive_selection.active_weight_by_reliability",
        {"high": 0.90, "medium": 0.55, "low": 0.20},
    )
    if not isinstance(metric_raw, dict) or not isinstance(promotion_raw, dict):
        raise ValueError("walk-forward metrics and promotion settings must be mappings")
    if not isinstance(optuna_raw, dict) or not isinstance(active_weight_raw, dict):
        raise ValueError("walk-forward Optuna and active-weight settings must be mappings")
    if not isinstance(monitoring_raw, dict):
        raise ValueError("calibration.walk_forward.monitoring must be a mapping")
    validate_monitoring_contract({"monitoring_contract": monitoring_raw})
    run_optuna = _bool(optuna_raw.get("enabled"), False) if args.run_optuna is None else bool(args.run_optuna)
    top_ns = _int_values(cfg_get(config, "calibration.walk_forward.top_n", [10, 20]), [10, 20])
    max_names = _int_values(
        cfg_get(config, "calibration.walk_forward.adaptive_selection.max_name_candidates", [4, 6, 8, 10, 12]),
        [4, 6, 8, 10, 12],
    )
    return FrameworkSettings(
        output_dir=output_dir,
        primary_horizon=int(cfg_get(config, "calibration.walk_forward.primary_horizon", 120)),
        top_ns=top_ns,
        candidate_pool_top_n=max(max(top_ns), max(max_names)),
        validation_shortlist_size=max(
            1,
            int(cfg_get(config, "calibration.walk_forward.validation_shortlist_size", 25)),
        ),
        max_workers=max(
            1,
            int(
                args.max_workers
                if args.max_workers is not None
                else cfg_get(config, "calibration.walk_forward.max_workers", 8)
            ),
        ),
        executor_kind=str(cfg_get(config, "calibration.walk_forward.candidate_grid_executor", "process")),
        windows=windows,
        score_pct_candidates=_float_values(
            cfg_get(
                config,
                "calibration.walk_forward.adaptive_selection.min_score_pct_of_top_candidates",
                [70, 75, 80, 85, 90, 95],
            ),
            [70, 75, 80, 85, 90, 95],
        ),
        max_name_candidates=max_names,
        active_weight_by_class=active_weight_raw,
        metric_settings=MetricSettings(
            lcb_z=float(metric_raw.get("lcb_z", 1.0)),
            cvar_q=float(metric_raw.get("cvar_q", 0.05)),
            profit_factor_cap=float(metric_raw.get("profit_factor_cap", 10.0)),
            min_profit_factor_wins=int(metric_raw.get("min_profit_factor_wins", 3)),
            min_profit_factor_losses=int(metric_raw.get("min_profit_factor_losses", 3)),
            bootstrap_iterations=int(metric_raw.get("bootstrap_iterations", 500)),
            bootstrap_seed=int(metric_raw.get("bootstrap_seed", 1729)),
            bootstrap_block_dates=int(metric_raw.get("bootstrap_block_dates", 4)),
        ),
        promotion_rules=PromotionRules(
            min_outer_folds=int(promotion_raw.get("min_outer_folds", 2)),
            min_fold_win_rate=float(promotion_raw.get("min_fold_win_rate", 0.60)),
            require_positive_paired_delta_lcb=_bool(
                promotion_raw.get("require_positive_paired_delta_lcb"),
                True,
            ),
            prefer_profit_factor_at_least=float(promotion_raw.get("prefer_profit_factor_at_least", 1.0)),
            min_profit_factor_improvement=float(promotion_raw.get("min_profit_factor_improvement", 0.0)),
            min_paired_delta_profit_factor=float(
                promotion_raw.get("min_paired_delta_profit_factor", 1.0)
            ),
            max_loss20_deterioration_pct=float(promotion_raw.get("max_loss20_deterioration_pct", 2.0)),
            max_loss40_deterioration_pct=float(promotion_raw.get("max_loss40_deterioration_pct", 1.0)),
            max_cvar_deterioration_pct=float(promotion_raw.get("max_cvar_deterioration_pct", 5.0)),
            max_drawdown_deterioration_pct=float(
                promotion_raw.get("max_drawdown_deterioration_pct", 5.0)
            ),
            max_top3_contribution_pct=float(promotion_raw.get("max_top3_contribution_pct", 55.0)),
            min_paired_dates=int(promotion_raw.get("min_paired_dates", 20)),
            min_active_date_coverage_pct=float(
                promotion_raw.get("min_active_date_coverage_pct", 25.0)
            ),
            max_calibration_fallback_frequency_pct=float(
                promotion_raw.get("max_calibration_fallback_frequency_pct", 40.0)
            ),
            min_robust_profit_factor=float(promotion_raw.get("min_robust_profit_factor", 1.0)),
            require_robust_profit_factor_support=_bool(
                promotion_raw.get("require_robust_profit_factor_support"),
                True,
            ),
            require_secondary_horizon_no_harm=_bool(
                promotion_raw.get("require_secondary_horizon_no_harm"),
                True,
            ),
            max_secondary_horizon_lcb_underperformance_pct=float(
                promotion_raw.get("max_secondary_horizon_lcb_underperformance_pct", 3.0)
            ),
            require_cohort_no_harm=_bool(promotion_raw.get("require_cohort_no_harm"), True),
            max_cohort_lcb_underperformance_pct=float(
                promotion_raw.get("max_cohort_lcb_underperformance_pct", 5.0)
            ),
            min_cohort_paired_dates=int(promotion_raw.get("min_cohort_paired_dates", 8)),
            required_secondary_horizons=tuple(sorted(windows)),
            required_no_harm_cohorts=tuple(
                str(value).strip()
                for value in promotion_raw.get("required_no_harm_cohorts", [])
                if str(value).strip()
            ),
            provisional_active_weight_cap=float(
                promotion_raw.get("provisional_active_weight_cap", 0.55)
            ),
        ),
        monitoring_contract=dict(monitoring_raw),
        optuna_enabled=run_optuna,
        optuna_trials=max(1, int(optuna_raw.get("n_trials", 200))),
        optuna_seed=int(optuna_raw.get("seed", 7331)),
    )


def load_calibration_module() -> Any:
    """Load script 28 through an importable facade for Windows spawn workers."""
    return importlib.import_module("biotech_index.calibration_base")


def read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv(path: Path, rows: Iterable[Mapping[str, object]]) -> None:
    materialized = [dict(row) for row in rows]
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    seen: set[str] = set()
    for row in materialized:
        for key in row:
            if key not in seen:
                fields.append(key)
                seen.add(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(materialized)


def persist_fold_grid_rows(fold_dir: Path, rows: list[dict[str, object]]) -> dict[str, object]:
    """Persist bulky diagnostic rows outside the resumable fold JSON."""
    path = fold_dir / "candidate_metrics.csv"
    write_csv(path, rows)
    return {
        "grid_rows_file": path.name,
        "grid_row_count": len(rows),
        "grid_rows_sha256": sha256_file(path),
    }


def load_cached_fold_grid_rows(payload: Mapping[str, object], fold_dir: Path) -> list[dict[str, Any]]:
    """Load current sidecar caches while accepting legacy inline fold caches."""
    legacy_rows = payload.get("grid_rows")
    if legacy_rows is not None:
        if not isinstance(legacy_rows, list) or not all(isinstance(row, Mapping) for row in legacy_rows):
            raise ValueError(f"Fold cache has invalid inline grid_rows: {fold_dir}")
        return [dict(row) for row in legacy_rows]

    raw_name = str(payload.get("grid_rows_file") or "").strip()
    if not raw_name or Path(raw_name).name != raw_name:
        raise ValueError(f"Fold cache lacks a safe grid_rows_file: {fold_dir}")
    path = fold_dir / raw_name
    if not path.exists():
        raise FileNotFoundError(path)
    expected_hash = str(payload.get("grid_rows_sha256") or "").strip().lower()
    actual_hash = sha256_file(path)
    if not expected_hash or actual_hash != expected_hash:
        raise ValueError(
            f"Fold grid sidecar hash mismatch: path={path} expected={expected_hash or '<missing>'} "
            f"actual={actual_hash}"
        )
    rows = read_csv(path)
    try:
        expected_count = int(str(payload.get("grid_row_count")))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Fold cache has invalid grid_row_count: {fold_dir}") from exc
    if len(rows) != expected_count:
        raise ValueError(
            f"Fold grid sidecar row-count mismatch: path={path} expected={expected_count} actual={len(rows)}"
        )
    return rows


def write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_code_contract(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted((item.resolve() for item in paths), key=str):
        digest.update(str(path.relative_to(PROJECT_ROOT)).replace("\\", "/").encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def resolve_observations_csv(config: dict[str, Any], config_path: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    configured = resolve_optional_path(
        cfg_get(config, "calibration.walk_forward.observations_csv", ""),
        base_dir=config_path.parent,
    )
    if configured is None:
        raise ValueError(
            "A PIT observation cache is required. Set calibration.walk_forward.observations_csv or pass "
            "--observations-csv <tier1_observations_with_forward_returns.csv>. Generate it with script 28 "
            "using the current code/config and without stale --resume state."
        )
    return configured


def validate_observation_contract(
    path: Path,
    rows: list[dict[str, Any]],
    *,
    config_hash: str,
    scoring_config_hash: str,
    required_horizons: Iterable[int],
) -> dict[str, object]:
    if not rows:
        raise ValueError(f"Observation cache is empty: {path}")
    horizons = sorted({int(value) for value in required_horizons})
    required = {"ticker", "asof_date", "biotech_primary_cohort"}
    required.update(f"fwd_{horizon}d_target_date" for horizon in horizons)
    missing = sorted(required - set(rows[0]))
    if missing:
        raise ValueError(f"Observation cache lacks required fields: {missing}")
    manifest_path = path.with_name("tier1_observations_with_forward_returns_manifest.json")
    if not manifest_path.exists():
        raise ValueError(f"Observation cache manifest is required for provenance: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError(f"Observation cache manifest must be a JSON object: {manifest_path}")
    signature = manifest.get("signature") or {}
    if not isinstance(signature, dict):
        raise ValueError(f"Observation cache manifest signature is invalid: {manifest_path}")
    manifest_horizons = {int(value) for value in signature.get("horizons") or []}
    missing_horizons = sorted(set(horizons) - manifest_horizons)
    if missing_horizons:
        raise ValueError(f"Observation cache manifest lacks required horizons: {missing_horizons}")
    if int(manifest.get("row_count") or -1) != len(rows):
        raise ValueError("Observation cache row count differs from its manifest")
    manifest_scoring_hash = str(signature.get("scoring_config_hash") or "").strip()
    if manifest_scoring_hash != scoring_config_hash:
        raise ValueError(
            "Observation cache scoring signature differs from the active scoring inputs; rebuild script 28 observations"
        )
    return {
        "observation_csv": str(path),
        "observation_csv_sha256": sha256_file(path),
        "observation_manifest": str(manifest_path),
        "observation_manifest_sha256": sha256_file(manifest_path),
        "config_sha256": config_hash,
        "row_count": len(rows),
        "asof_date_min": min(str(row.get("asof_date") or "") for row in rows),
        "asof_date_max": max(str(row.get("asof_date") or "") for row in rows),
        "cache_signature": signature,
    }


def grid_utility(row: Mapping[str, object]) -> float:
    lcb = finite_float(row.get("selected_lcb_return_pct"))
    mean_return = finite_float(row.get("selected_mean_return_pct"))
    profit_factor = finite_float(row.get("selected_profit_factor"))
    robust_pf = finite_float(row.get("selected_profit_factor_ex_largest_winner"))
    loss20 = finite_float(row.get("selected_large_loss_20pct_rate_pct"))
    top3 = finite_float(row.get("selected_top3_gain_contribution_pct"))
    coverage = finite_float(row.get("selection_date_coverage_pct"))
    if lcb is None or profit_factor is None:
        return -1e12
    return (
        lcb
        + 0.20 * (0.0 if mean_return is None else mean_return)
        + 2.0 * (min(4.0, profit_factor) - 1.0)
        + 1.0 * (min(4.0, robust_pf) - 1.0 if robust_pf is not None else -1.0)
        - 0.03 * (100.0 if loss20 is None else loss20)
        - 0.01 * (100.0 if top3 is None else top3)
        + 0.005 * (0.0 if coverage is None else coverage)
    )


def candidate_pair_by_id(module: Any, specs: list[Any], policies: list[Any]) -> dict[str, tuple[Any, Any]]:
    return {
        str(module.stable_candidate_id(spec, policy)): (spec, policy)
        for spec in specs
        for policy in policies
    }


def incumbent_pair(module: Any, config: dict[str, Any], pairs: Mapping[str, tuple[Any, Any]]) -> tuple[str, Any, Any]:
    candidate_name = str(cfg_get(config, "biotech_scoring.production_baseline.candidate_name", "") or "").strip()
    policy_name = str(
        cfg_get(config, "biotech_scoring.production_baseline.selection_policy", "core_structural_veto")
    ).strip()
    for candidate_id, (spec, policy) in pairs.items():
        if spec.candidate_name == candidate_name and policy.policy_name == policy_name:
            return candidate_id, spec, policy
    current_name = str(getattr(module, "CURRENT_CONFIG_CANDIDATE_NAME", "current_config"))
    for candidate_id, (spec, policy) in pairs.items():
        if spec.candidate_name == current_name and policy.policy_name == policy_name:
            return candidate_id, spec, policy
    raise ValueError(
        "Production incumbent is absent from the calibration grid: "
        f"candidate_name={candidate_name!r} policy={policy_name!r}"
    )


def ensure_incumbent_in_grid(
    module: Any,
    config: dict[str, Any],
    specs: list[Any],
    policies: list[Any],
    *,
    candidate_limit: int,
) -> tuple[list[Any], dict[str, tuple[Any, Any]], tuple[str, Any, Any]]:
    """Return a grid that always contains the production incumbent comparator."""
    pairs = candidate_pair_by_id(module, specs, policies)
    try:
        incumbent = incumbent_pair(module, config, pairs)
        return specs, pairs, incumbent
    except ValueError:
        if candidate_limit <= 0:
            raise
    full_specs = module.generate_weight_specs(config, candidate_limit=0)
    full_pairs = candidate_pair_by_id(module, full_specs, policies)
    incumbent = incumbent_pair(module, config, full_pairs)
    incumbent_id, incumbent_spec, _incumbent_policy = incumbent
    if incumbent_id not in pairs:
        specs = [*specs, incumbent_spec]
        pairs = candidate_pair_by_id(module, specs, policies)
    if incumbent_id not in pairs:
        raise ValueError("Failed to restore the production incumbent after applying candidate_limit")
    return specs, pairs, incumbent


def selected_rows(
    module: Any,
    rows: Iterable[Mapping[str, object]],
    spec: Any,
    policy: Any,
    *,
    horizon: int,
    top_n: int,
    params: Any,
) -> list[dict[str, Any]]:
    return module.selected_rows_by_date(
        [dict(row) for row in rows],
        spec,
        policy,
        horizon=horizon,
        top_n=top_n,
        params=params,
    )


def candidate_records(
    module: Any,
    rows: Iterable[Mapping[str, object]],
    spec: Any,
    policy: Any,
    *,
    horizon: int,
    top_n: int,
    params: Any,
) -> list[ReliabilityRecord]:
    return records_from_rows(
        selected_rows(module, rows, spec, policy, horizon=horizon, top_n=top_n, params=params),
        score_key="candidate_selection_score",
        return_key=module.objective_return_key(horizon, params),
    )


def build_fold_plan(
    observations: Iterable[Mapping[str, object]],
    *,
    module: Any,
    params: Any,
    settings: FrameworkSettings,
) -> dict[int, list[WalkForwardFold]]:
    """Build and validate every walk-forward fold before expensive grid scoring."""
    observation_rows = list(observations)
    fold_plan: dict[int, list[WalkForwardFold]] = {}
    for horizon, window in sorted(settings.windows.items()):
        return_key = module.objective_return_key(horizon, params)
        eligible_dates = [
            row.get("asof_date")
            for row in observation_rows
            if finite_float(row.get(return_key)) is not None
            and str(row.get(f"fwd_{horizon}d_target_date") or "").strip()
        ]
        fold_plan[horizon] = build_expanding_walk_forward_folds(eligible_dates, window)

    primary_folds = fold_plan.get(settings.primary_horizon, [])
    if not primary_folds:
        raise ValueError(
            "No complete walk-forward folds exist for the primary promotion horizon "
            f"{settings.primary_horizon}d; adjust the window or provide more PIT history"
        )
    required_folds = max(1, int(settings.promotion_rules.min_outer_folds))
    if len(primary_folds) < required_folds:
        LOGGER.warning(
            "Primary horizon %sd has only %s outer fold(s), below min_outer_folds=%s; "
            "the run can support provisional evidence only",
            settings.primary_horizon,
            len(primary_folds),
            required_folds,
        )
    LOGGER.info(
        "Walk-forward fold plan: %s",
        ", ".join(f"{horizon}d={len(folds)}" for horizon, folds in sorted(fold_plan.items())),
    )
    return fold_plan


def build_secondary_horizon_evaluation(
    module: Any,
    rows: Iterable[Mapping[str, object]],
    *,
    candidate_spec: Any | None,
    candidate_policy: Any | None,
    candidate_id: str,
    candidate_name: str,
    selection_policy_name: str,
    threshold: ReliabilityThreshold | None,
    frozen_top_n: int,
    candidate_pool_top_n: int,
    incumbent_spec: Any,
    incumbent_policy: Any,
    incumbent_top_n: int,
    horizon: int,
    params: Any,
    settings: FrameworkSettings,
    fold_id: str,
) -> dict[str, object]:
    """Evaluate one frozen primary policy on a secondary outer-test horizon."""
    fallback = candidate_spec is None or candidate_policy is None or threshold is None
    if fallback:
        selected: list[ReliabilityRecord] = []
        active_candidate: dict[str, float] = {}
        counts: dict[str, int] = {}
        active_weight = 0.0
        reliability_class = "benchmark_fallback"
        min_score_pct_of_top = 0.0
        max_names = 0
        validation_objective: object = ""
    else:
        if candidate_spec is None or candidate_policy is None or threshold is None:
            raise ValueError("Non-fallback secondary evaluation lacks a frozen candidate contract")
        pool = candidate_records(
            module,
            rows,
            candidate_spec,
            candidate_policy,
            horizon=horizon,
            top_n=candidate_pool_top_n,
            params=params,
        )
        selected, active_candidate, counts = apply_frozen_threshold(pool, threshold)
        active_weight = threshold.active_weight
        reliability_class = threshold.reliability_class
        min_score_pct_of_top = threshold.min_score_pct_of_top
        max_names = threshold.max_names
        validation_objective = threshold.validation_objective

    incumbent, incumbent_records = incumbent_returns(
        module,
        rows,
        incumbent_spec,
        incumbent_policy,
        horizon=horizon,
        top_n=incumbent_top_n,
        params=params,
    )
    candidate = (
        {asof_date: 0.0 for asof_date in incumbent}
        if fallback
        else blend_active_alpha_with_benchmark(
            active_candidate,
            incumbent,
            active_weight=active_weight,
        )
    )
    comparison = paired_policy_comparison(candidate, incumbent, settings.metric_settings)
    active_dates = sum(1 for asof_date in incumbent if counts.get(asof_date, 0) > 0)
    selected_name_dates = sum(counts.get(asof_date, 0) for asof_date in incumbent)
    comparison_row = {
        "fold_id": fold_id,
        "horizon_days": horizon,
        "candidate_id": candidate_id,
        "candidate_name": candidate_name,
        "selection_policy_name": selection_policy_name,
        "frozen_top_n": frozen_top_n,
        "frozen_min_score_pct_of_top": min_score_pct_of_top,
        "frozen_max_names": max_names,
        "validation_objective": validation_objective,
        "reliability_class": reliability_class,
        "active_weight": active_weight,
        "xbi_residual_weight": round(1.0 - active_weight, 6),
        "candidate_return_contract": "frozen_primary_policy_secondary_horizon",
        "test_avg_selected_names": (
            round(selected_name_dates / len(incumbent), 6) if incumbent else 0.0
        ),
        "test_active_date_count": active_dates,
        "test_evaluation_date_count": len(incumbent),
        "test_active_date_coverage_pct": (
            round(100.0 * active_dates / len(incumbent), 6) if incumbent else 0.0
        ),
        **comparison,
    }
    return {
        "horizon_days": horizon,
        "evaluation_dates": sorted(incumbent),
        "outer_test_comparison_row": comparison_row,
        "candidate_records": record_rows(
            selected,
            fold_id=fold_id,
            split="outer_test_candidate_secondary",
        ),
        "incumbent_records": record_rows(
            incumbent_records,
            fold_id=fold_id,
            split="outer_test_incumbent_secondary",
        ),
        "cohort_rows": cohort_comparisons(
            selected,
            incumbent_records,
            settings.metric_settings,
            fold_id=fold_id,
            horizon=horizon,
            active_weight=active_weight,
        ),
        "sleeve_rows": [
            {
                "fold_id": fold_id,
                "horizon_days": horizon,
                "asof_date": asof_date,
                "selected_name_count": counts.get(asof_date, 0),
                "reliability_class": reliability_class,
                "active_stock_selection_weight": (
                    active_weight if counts.get(asof_date, 0) > 0 else 0.0
                ),
                "xbi_residual_weight": (
                    round(1.0 - active_weight, 6)
                    if counts.get(asof_date, 0) > 0
                    else 1.0
                ),
                "sleeve_weight_sum": 1.0,
            }
            for asof_date in sorted(incumbent)
        ],
    }


def validated_mapping_rows(raw: object, *, label: str) -> list[dict[str, object]]:
    """Return a strict list of mapping rows from a serialized fold payload."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{label} must be a list")
    rows: list[dict[str, object]] = []
    for index, value in enumerate(raw):
        if not isinstance(value, Mapping):
            raise ValueError(f"{label}[{index}] must be an object")
        rows.append(dict(value))
    return rows


def validated_evaluation_dates(raw: object, *, label: str) -> list[str]:
    """Return a strict, unique list of evaluation dates from a fold cache."""
    if not isinstance(raw, list):
        raise ValueError(f"{label} must be a list")
    dates = [str(value).strip() for value in raw]
    if any(not value for value in dates):
        raise ValueError(f"{label} contains a blank date")
    if len(dates) != len(set(dates)):
        raise ValueError(f"{label} contains duplicate dates")
    return sorted(dates)


def ingest_frozen_evaluation(
    evaluation: Mapping[str, object],
    *,
    regime_lookup: Mapping[tuple[str, str], str],
    fold_comparison_rows: list[dict[str, object]],
    cohort_rows: list[dict[str, object]],
    selected_rows_output: list[dict[str, object]],
    sleeve_rows_output: list[dict[str, object]],
    fold_candidate_returns: dict[int, list[Mapping[str, float]]],
    fold_incumbent_returns: dict[int, list[Mapping[str, float]]],
    fold_candidate_cohort_returns: dict[int, dict[str, list[Mapping[str, float]]]],
    fold_incumbent_cohort_returns: dict[int, dict[str, list[Mapping[str, float]]]],
    fold_candidate_regime_returns: dict[int, dict[str, list[Mapping[str, float]]]],
    fold_incumbent_regime_returns: dict[int, dict[str, list[Mapping[str, float]]]],
) -> None:
    """Ingest a serialized frozen-policy outer-test evaluation."""
    comparison_row = evaluation.get("outer_test_comparison_row")
    if not isinstance(comparison_row, Mapping):
        raise ValueError("Frozen secondary evaluation lacks an outer-test comparison row")
    horizon = int(finite_float(comparison_row.get("horizon_days")) or 0)
    active_weight = finite_float(comparison_row.get("active_weight"))
    if horizon <= 0 or active_weight is None:
        raise ValueError("Frozen secondary evaluation has invalid horizon or active weight")

    candidate_records_payload = validated_mapping_rows(
        evaluation.get("candidate_records"),
        label="candidate_records",
    )
    incumbent_records_payload = validated_mapping_rows(
        evaluation.get("incumbent_records"),
        label="incumbent_records",
    )
    cohort_rows_payload = validated_mapping_rows(
        evaluation.get("cohort_rows"),
        label="cohort_rows",
    )
    sleeve_rows_payload = validated_mapping_rows(
        evaluation.get("sleeve_rows"),
        label="sleeve_rows",
    )
    evaluation_dates = validated_evaluation_dates(
        evaluation.get("evaluation_dates"),
        label="evaluation_dates",
    )
    candidate_records_rows = reliability_records_from_cache(candidate_records_payload)
    incumbent_records_rows = reliability_records_from_cache(incumbent_records_payload)
    candidate_active = equal_weight_returns_by_date(
        record_rows(candidate_records_rows, fold_id="aggregate", split="candidate"),
        return_key="objective_return",
    )
    incumbent_active = equal_weight_returns_by_date(
        record_rows(incumbent_records_rows, fold_id="aggregate", split="incumbent"),
        return_key="objective_return",
    )
    incumbent = blend_active_alpha_with_benchmark(
        incumbent_active,
        evaluation_dates,
        active_weight=1.0,
    )
    candidate = blend_active_alpha_with_benchmark(
        candidate_active,
        evaluation_dates,
        active_weight=active_weight,
    )

    fold_comparison_rows.append(dict(comparison_row))
    cohort_rows.extend(cohort_rows_payload)
    selected_rows_output.extend((*candidate_records_payload, *incumbent_records_payload))
    sleeve_rows_output.extend(sleeve_rows_payload)
    fold_candidate_returns[horizon].append(candidate)
    fold_incumbent_returns[horizon].append(incumbent)

    candidate_cohorts = returns_by_cohort(candidate_records_rows)
    incumbent_cohorts = returns_by_cohort(incumbent_records_rows)
    for cohort in sorted(set(candidate_cohorts).union(incumbent_cohorts)):
        incumbent_map = incumbent_cohorts.get(cohort, {})
        fold_candidate_cohort_returns[horizon][cohort].append(
            blend_active_alpha_with_benchmark(
                candidate_cohorts.get(cohort, {}),
                incumbent_map,
                active_weight=active_weight,
            )
        )
        fold_incumbent_cohort_returns[horizon][cohort].append(incumbent_map)

    candidate_regimes = returns_by_regime(candidate_records_rows, regime_lookup)
    incumbent_regimes = returns_by_regime(incumbent_records_rows, regime_lookup)
    for regime in sorted(set(candidate_regimes).union(incumbent_regimes)):
        incumbent_map = incumbent_regimes.get(regime, {})
        fold_candidate_regime_returns[horizon][regime].append(
            blend_active_alpha_with_benchmark(
                candidate_regimes.get(regime, {}),
                incumbent_map,
                active_weight=active_weight,
            )
        )
        fold_incumbent_regime_returns[horizon][regime].append(incumbent_map)


def incumbent_returns(
    module: Any,
    rows: Iterable[Mapping[str, object]],
    spec: Any,
    policy: Any,
    *,
    horizon: int,
    top_n: int,
    params: Any,
) -> tuple[dict[str, float], list[ReliabilityRecord]]:
    row_list = list(rows)
    return_key = module.objective_return_key(horizon, params)
    evaluation_dates = sorted(
        {
            str(row.get("asof_date") or "").strip()
            for row in row_list
            if str(row.get("asof_date") or "").strip()
            and finite_float(row.get(return_key)) is not None
        }
    )
    records = candidate_records(
        module,
        row_list,
        spec,
        policy,
        horizon=horizon,
        top_n=top_n,
        params=params,
    )
    active_returns = equal_weight_returns_by_date(
        [{"asof_date": record.asof_date, "return_value": record.return_value} for record in records],
        return_key="return_value",
    )
    returns = blend_active_alpha_with_benchmark(
        active_returns,
        evaluation_dates,
        active_weight=1.0,
    )
    return returns, records


def validation_candidate_survives(metrics: Mapping[str, object], rules: PromotionRules) -> bool:
    """Shortlist balanced challengers; final authority is the frozen daily net replay."""
    return validation_candidate_survives_multimetric(metrics, rules)


def live_contract_readiness(
    fold_contract: Mapping[str, object] | None,
    config: dict[str, Any],
) -> tuple[bool, str]:
    """Return exact live-scorer parity status and an auditable explanation."""
    if not isinstance(fold_contract, Mapping):
        return False, "missing_primary_fold_contract"
    try:
        validate_contract_scoring_parity({"latest_primary_fold_contract": fold_contract}, config)
    except PromotionContractError as exc:
        return False, str(exc)
    return True, "exact_live_scorer_parity_confirmed"


_DEPLOYABLE_FOLD_CONTRACT_FIELDS = frozenset(
    {
        "candidate_id",
        "candidate_pool_top_n",
        "candidate_spec",
        "selection_policy",
        "threshold",
        "outer_test_comparison_row",
        "signature",
    }
)


def deployable_fold_contract(
    fold_contract: Mapping[str, object] | None,
) -> dict[str, object]:
    """Return only immutable activation inputs and compact audit metadata."""
    if not isinstance(fold_contract, Mapping):
        return {}
    payload = {
        key: fold_contract[key]
        for key in _DEPLOYABLE_FOLD_CONTRACT_FIELDS
        if key in fold_contract
    }
    serialized = json.loads(json.dumps(payload))
    if not isinstance(serialized, dict):
        raise ValueError("Deployable fold contract is not a JSON object")
    return serialized


def threshold_objective(metrics: Mapping[str, object]) -> float:
    delta_lcb = finite_float(metrics.get("paired_delta_bootstrap_lcb_pct"))
    profit_factor = finite_float(metrics.get("candidate_profit_factor"))
    robust_pf = finite_float(metrics.get("candidate_profit_factor_ex_largest_winner"))
    loss20 = finite_float(metrics.get("candidate_loss20_rate_pct"))
    if delta_lcb is None or profit_factor is None:
        return -1e12
    return (
        delta_lcb
        + 2.0 * (profit_factor - 1.0)
        + (robust_pf - 1.0 if robust_pf is not None else -1.0)
        - 0.02 * (100.0 if loss20 is None else loss20)
    )


def optimize_with_optuna(
    evaluations: Mapping[str, tuple[Any, Any, int, tuple[ReliabilityRecord, ...]]],
    incumbent: Mapping[str, float],
    settings: FrameworkSettings,
    *,
    min_dates: int,
    fold_id: str,
    horizon: int,
    trial_audit_rows: list[dict[str, object]],
) -> CandidateEvaluation | None:
    if not evaluations:
        return None
    optuna = importlib.import_module("optuna")
    sampler = optuna.samplers.TPESampler(seed=settings.optuna_seed, multivariate=True)
    study = optuna.create_study(direction="maximize", sampler=sampler)
    candidate_ids = sorted(evaluations)
    min_pct = min(settings.score_pct_candidates)
    max_pct = max(settings.score_pct_candidates)
    min_names = min(settings.max_name_candidates)
    max_names = max(settings.max_name_candidates)

    def evaluate_threshold(
        records: tuple[ReliabilityRecord, ...],
        *,
        score_pct: float,
        names: int,
    ) -> tuple[dict[str, object], str, float, dict[str, int]]:
        _selected, active_returns, counts = apply_reliability_threshold(
            records,
            min_score_pct_of_top=score_pct,
            max_names=names,
        )
        active_metrics = paired_policy_comparison(active_returns, incumbent, settings.metric_settings)
        reliability_class = reliability_class_from_metrics(active_metrics)
        active_weight = active_weight_for_class(reliability_class, settings.active_weight_by_class)
        sleeve_returns = blend_active_alpha_with_benchmark(
            active_returns,
            incumbent,
            active_weight=active_weight,
        )
        metrics = paired_policy_comparison(sleeve_returns, incumbent, settings.metric_settings)
        metrics["candidate_return_contract"] = "active_stock_alpha_plus_xbi_residual"
        metrics["active_selection_paired_delta_bootstrap_lcb_pct"] = active_metrics.get(
            "paired_delta_bootstrap_lcb_pct", ""
        )
        metrics["active_selection_profit_factor"] = active_metrics.get("candidate_profit_factor", "")
        metrics["active_date_count"] = sum(1 for count in counts.values() if count > 0)
        metrics["evaluation_date_count"] = len(incumbent)
        metrics["avg_selected_names"] = (
            round(sum(counts.values()) / len(incumbent), 6) if incumbent else 0.0
        )
        return metrics, reliability_class, active_weight, counts

    def objective(trial: Any) -> float:
        candidate_id = str(trial.suggest_categorical("candidate_id", candidate_ids))
        score_pct = float(trial.suggest_float("min_score_pct_of_top", min_pct, max_pct, step=1.0))
        names = int(trial.suggest_int("max_names", min_names, max_names))
        records = evaluations[candidate_id][3]
        metrics, _reliability_class, _active_weight, _counts = evaluate_threshold(
            records,
            score_pct=score_pct,
            names=names,
        )
        if int(finite_float(metrics.get("paired_date_count")) or 0) < min_dates:
            return -1e12
        if not validation_candidate_survives(metrics, settings.promotion_rules):
            return -1e12
        return threshold_objective(metrics)

    study.optimize(objective, n_trials=settings.optuna_trials, show_progress_bar=False)
    for trial in study.trials:
        trial_audit_rows.append(
            {
                "fold_id": fold_id,
                "horizon_days": horizon,
                "trial_number": trial.number,
                "trial_state": str(trial.state.name),
                "objective_value": trial.value if trial.value is not None else "",
                "candidate_id": trial.params.get("candidate_id", ""),
                "min_score_pct_of_top": trial.params.get("min_score_pct_of_top", ""),
                "max_names": trial.params.get("max_names", ""),
                "outer_test_visible_to_objective": False,
            }
        )
    if study.best_value <= -1e11:
        return None
    candidate_id = str(study.best_params["candidate_id"])
    spec, policy, top_n, records = evaluations[candidate_id]
    score_pct = float(study.best_params["min_score_pct_of_top"])
    names = int(study.best_params["max_names"])
    metrics, reliability_class, active_weight, counts = evaluate_threshold(
        records,
        score_pct=score_pct,
        names=names,
    )
    avg_names = sum(counts.values()) / len(counts) if counts else 0.0
    threshold = ReliabilityThreshold(
        min_score_pct_of_top=score_pct,
        max_names=names,
        reliability_class=reliability_class,
        active_weight=active_weight,
        validation_objective=threshold_objective(metrics),
        validation_metrics={
            **metrics,
            "avg_selected_names": round(avg_names, 6),
            "active_date_count": len([count for count in counts.values() if count > 0]),
            "evaluation_date_count": len(incumbent),
        },
    )
    return CandidateEvaluation(candidate_id, spec, policy, top_n, records, threshold)
def evaluate_validation_shortlist(
    module: Any,
    validation_rows: Iterable[Mapping[str, object]],
    shortlist: Iterable[tuple[str, Any, Any, int]],
    incumbent: Mapping[str, float],
    *,
    horizon: int,
    params: Any,
    settings: FrameworkSettings,
    min_dates: int,
    fold_id: str,
    trial_audit_rows: list[dict[str, object]],
) -> tuple[CandidateEvaluation | None, list[dict[str, object]], list[dict[str, object]]]:
    evaluations: dict[str, tuple[Any, Any, int, tuple[ReliabilityRecord, ...]]] = {}
    threshold_rows: list[dict[str, object]] = []
    curve_rows: list[dict[str, object]] = []
    best: CandidateEvaluation | None = None
    for candidate_id, spec, policy, top_n in shortlist:
        records = tuple(
            candidate_records(
                module,
                validation_rows,
                spec,
                policy,
                horizon=horizon,
                top_n=settings.candidate_pool_top_n,
                params=params,
            )
        )
        if not records:
            continue
        threshold = select_reliability_threshold(
            records,
            incumbent,
            score_pct_candidates=settings.score_pct_candidates,
            max_name_candidates=settings.max_name_candidates,
            settings=settings.metric_settings,
            active_weight_by_class=settings.active_weight_by_class,
            min_dates=min_dates,
        )
        for row in build_reliability_curve(records, bins=5, settings=settings.metric_settings):
            curve_rows.append(
                {
                    "candidate_id": candidate_id,
                    "candidate_name": spec.candidate_name,
                    "selection_policy_name": policy.policy_name,
                    **row,
                }
            )
        if threshold is None:
            continue
        evaluation = CandidateEvaluation(candidate_id, spec, policy, top_n, records, threshold)
        survived = validation_candidate_survives(threshold.validation_metrics, settings.promotion_rules)
        if survived:
            evaluations[candidate_id] = (spec, policy, top_n, records)
        threshold_rows.append(
            {
                "candidate_id": candidate_id,
                "candidate_name": spec.candidate_name,
                "selection_policy_name": policy.policy_name,
                "source": "deterministic_validation_grid",
                **threshold.as_dict(),
            }
        )
        if survived and (best is None or evaluation.threshold.validation_objective > best.threshold.validation_objective):
            best = evaluation
    if settings.optuna_enabled:
        optuna_best = optimize_with_optuna(
            evaluations,
            incumbent,
            settings,
            min_dates=min_dates,
            fold_id=fold_id,
            horizon=horizon,
            trial_audit_rows=trial_audit_rows,
        )
        if optuna_best is not None:
            threshold_rows.append(
                {
                    "candidate_id": optuna_best.candidate_id,
                    "candidate_name": optuna_best.spec.candidate_name,
                    "selection_policy_name": optuna_best.policy.policy_name,
                    "source": "optuna_validation_only",
                    **optuna_best.threshold.as_dict(),
                }
            )
            if best is None or optuna_best.threshold.validation_objective > best.threshold.validation_objective:
                best = optuna_best
    return best, threshold_rows, curve_rows


def grid_shortlist(
    train_grid: Iterable[Mapping[str, object]],
    validation_grid: Iterable[Mapping[str, object]],
    pairs: Mapping[str, tuple[Any, Any]],
    *,
    limit: int,
    incumbent_id: str,
) -> list[tuple[str, Any, Any, int]]:
    validation_index = {
        (str(row.get("candidate_id") or ""), int(finite_float(row.get("top_n")) or 0)): row
        for row in validation_grid
    }
    ranked: list[tuple[float, str, int]] = []
    for row in train_grid:
        candidate_id = str(row.get("candidate_id") or "")
        top_n = int(finite_float(row.get("top_n")) or 0)
        validation_row = validation_index.get((candidate_id, top_n))
        if candidate_id not in pairs or validation_row is None or top_n <= 0:
            continue
        train_dates = int(finite_float(row.get("asof_dates")) or 0)
        validation_dates = int(finite_float(validation_row.get("asof_dates")) or 0)
        if train_dates <= 0 or validation_dates <= 0:
            continue
        combined = 0.35 * grid_utility(row) + 0.65 * grid_utility(validation_row)
        ranked.append((combined, candidate_id, top_n))
    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
    selected = ranked[: max(1, limit)]
    if not any(candidate_id == incumbent_id for _, candidate_id, _ in selected):
        incumbent_options = [item for item in ranked if item[1] == incumbent_id]
        if incumbent_options:
            selected.append(incumbent_options[0])
    return [(candidate_id, *pairs[candidate_id], top_n) for _, candidate_id, top_n in selected]


def record_rows(records: Iterable[ReliabilityRecord], *, fold_id: str, split: str) -> list[dict[str, object]]:
    return [
        {
            "fold_id": fold_id,
            "evaluation_split": split,
            "asof_date": record.asof_date,
            "ticker": record.ticker,
            "biotech_primary_cohort": record.cohort,
            "candidate_selection_score": record.score,
            "objective_return": record.return_value,
        }
        for record in records
    ]


def reliability_records_from_cache(raw: object) -> list[ReliabilityRecord]:
    if not isinstance(raw, list):
        raise ValueError("Fold cache reliability records must be a list")
    records: list[ReliabilityRecord] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("Fold cache reliability record must be a mapping")
        score = finite_float(item.get("candidate_selection_score"))
        return_value = finite_float(item.get("objective_return"))
        asof_date = str(item.get("asof_date") or "").strip()
        ticker = str(item.get("ticker") or "").strip().upper()
        if score is None or return_value is None or not asof_date or not ticker:
            raise ValueError("Fold cache contains an incomplete reliability record")
        records.append(
            ReliabilityRecord(
                asof_date=asof_date,
                ticker=ticker,
                score=score,
                return_value=return_value,
                cohort=str(item.get("biotech_primary_cohort") or "ALL").strip() or "ALL",
            )
        )
    return records


def returns_by_regime(
    records: Iterable[ReliabilityRecord],
    regime_lookup: Mapping[tuple[str, str], str],
) -> dict[str, dict[str, float]]:
    grouped: dict[str, list[ReliabilityRecord]] = defaultdict(list)
    for record in records:
        regime = regime_lookup.get((record.asof_date, record.ticker), "unclassified")
        grouped[regime].append(record)
    return {
        regime: equal_weight_returns_by_date(
            [
                {"asof_date": record.asof_date, "return_value": record.return_value}
                for record in regime_records
            ],
            return_key="return_value",
        )
        for regime, regime_records in grouped.items()
    }


def returns_by_cohort(records: Iterable[ReliabilityRecord]) -> dict[str, dict[str, float]]:
    grouped: dict[str, list[ReliabilityRecord]] = defaultdict(list)
    for record in records:
        grouped[record.cohort].append(record)
    return {
        cohort: equal_weight_returns_by_date(
            [
                {"asof_date": record.asof_date, "return_value": record.return_value}
                for record in cohort_records
            ],
            return_key="return_value",
        )
        for cohort, cohort_records in grouped.items()
    }


def apply_frozen_threshold(
    records: Iterable[ReliabilityRecord],
    threshold: ReliabilityThreshold,
) -> tuple[list[ReliabilityRecord], dict[str, float], dict[str, int]]:
    return apply_reliability_threshold(
        records,
        min_score_pct_of_top=threshold.min_score_pct_of_top,
        max_names=threshold.max_names,
    )


def aligned_residual_sleeves(
    candidate_active_returns: Mapping[str, float],
    incumbent_active_returns: Mapping[str, float],
    *,
    active_weight: float,
) -> tuple[dict[str, float], dict[str, float]]:
    """Align both policies on every date where either policy deployed active risk."""
    evaluation_dates = sorted(set(candidate_active_returns).union(incumbent_active_returns))
    return (
        blend_active_alpha_with_benchmark(
            candidate_active_returns,
            evaluation_dates,
            active_weight=active_weight,
        ),
        blend_active_alpha_with_benchmark(
            incumbent_active_returns,
            evaluation_dates,
            active_weight=1.0,
        ),
    )


def cohort_comparisons(
    candidate: Iterable[ReliabilityRecord],
    incumbent: Iterable[ReliabilityRecord],
    settings: MetricSettings,
    *,
    fold_id: str,
    horizon: int,
    active_weight: float,
) -> list[dict[str, object]]:
    candidate_by_cohort: dict[str, list[ReliabilityRecord]] = defaultdict(list)
    incumbent_by_cohort: dict[str, list[ReliabilityRecord]] = defaultdict(list)
    for record in candidate:
        candidate_by_cohort[record.cohort].append(record)
    for record in incumbent:
        incumbent_by_cohort[record.cohort].append(record)
    rows: list[dict[str, object]] = []
    for cohort in sorted(set(candidate_by_cohort).union(incumbent_by_cohort)):
        candidate_returns = equal_weight_returns_by_date(
            [
                {"asof_date": record.asof_date, "return_value": record.return_value}
                for record in candidate_by_cohort.get(cohort, [])
            ],
            return_key="return_value",
        )
        incumbent_returns_map = equal_weight_returns_by_date(
            [
                {"asof_date": record.asof_date, "return_value": record.return_value}
                for record in incumbent_by_cohort.get(cohort, [])
            ],
            return_key="return_value",
        )
        candidate_sleeve, incumbent_sleeve = aligned_residual_sleeves(
            candidate_returns,
            incumbent_returns_map,
            active_weight=active_weight,
        )
        rows.append(
            {
                "fold_id": fold_id,
                "horizon_days": horizon,
                "cohort": cohort,
                "active_weight": active_weight,
                **paired_policy_comparison(
                    candidate_sleeve,
                    incumbent_sleeve,
                    settings,
                ),
            }
        )
    return rows


def aggregate_nonoverlapping_returns(parts: Iterable[Mapping[str, float]]) -> dict[str, float]:
    output: dict[str, float] = {}
    for part in parts:
        overlap = set(output).intersection(part)
        if overlap:
            raise ValueError(f"Outer-test fold date overlap violates independence: {sorted(overlap)[:5]}")
        output.update(part)
    return dict(sorted(output.items()))


def promotion_markdown(decisions: Mapping[int, Mapping[str, object]]) -> str:
    lines = [
        "# Biotech Walk-Forward Promotion Decision",
        "",
        "Outer-test results are evaluated only after each fold's candidate and adaptive breadth were frozen.",
        "",
        "| Horizon | Status | Authorized | Fold wins | Paired dates | Delta LCB | Candidate PF | Incumbent PF |",
        "|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for horizon, decision in sorted(decisions.items()):
        lines.append(
            "| {h} | {status} | {auth} | {wins}/{folds} | {dates} | {lcb} | {cpf} | {ipf} |".format(
                h=horizon,
                status=decision.get("promotion_status", ""),
                auth=decision.get("production_promotion_authorized", False),
                wins=decision.get("outer_fold_wins", 0),
                folds=decision.get("outer_fold_count", 0),
                dates=decision.get("paired_date_count", 0),
                lcb=decision.get("paired_delta_bootstrap_lcb_pct", ""),
                cpf=decision.get("candidate_profit_factor", ""),
                ipf=decision.get("incumbent_profit_factor", ""),
            )
        )
    lines.extend(
        [
            "",
            "A generated contract is a promotion candidate, not an implicit config mutation. The scorer must validate and activate it explicitly.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    configure_utc_logging()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    settings = load_framework_settings(config, config_path, args)
    cohort_filter = str(args.cohort_filter or "").strip()
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    observations_path = resolve_observations_csv(config, config_path, args.observations_csv)
    observations = read_csv(observations_path)
    provenance = validate_observation_contract(
        observations_path,
        observations,
        config_hash=sha256_file(config_path),
        scoring_config_hash=observation_scoring_config_hash(config, base_dir=config_path.parent),
        required_horizons=settings.windows,
    )
    regime_field = next(
        (
            field
            for field in ("market_regime", "macro_regime", "regime", "xbi_regime")
            if field in observations[0]
        ),
        "",
    )
    regime_lookup = {
        (str(row.get("asof_date") or ""), str(row.get("ticker") or "").strip().upper()): (
            str(row.get(regime_field) or "unclassified").strip() or "unclassified"
        )
        for row in observations
    }
    provenance["framework_code_sha256"] = sha256_code_contract(
        [
            Path(__file__),
            CALIBRATION_SOURCE_PATH,
            PACKAGE_ROOT / "core" / "calibration_metrics.py",
            PACKAGE_ROOT / "core" / "calibration_splits.py",
            PACKAGE_ROOT / "core" / "promotion_policy.py",
            PACKAGE_ROOT / "core" / "promotion_contract.py",
            PACKAGE_ROOT / "core" / "calibration_provenance.py",
            PACKAGE_ROOT / "core" / "score_reliability.py",
            PACKAGE_ROOT / "core" / "cohort_calibration.py",
        ]
    )
    module = load_calibration_module()
    params = module.load_calibration_params(config)
    if not params.alpha_adjustment_enabled or str(params.return_objective).strip().lower() not in {
        "benchmark_alpha",
        "xbi_alpha",
        "sector_alpha",
    }:
        raise ValueError(
            "The mandatory XBI residual contract requires benchmark-alpha calibration returns"
        )
    fold_plan = build_fold_plan(
        observations,
        module=module,
        params=params,
        settings=settings,
    )
    full_observation_count = len(observations)
    if cohort_filter:
        observations = rows_for_cohort(observations, cohort_filter)
        if not observations:
            raise ValueError(f"No PIT calibration observations exist for cohort={cohort_filter!r}")
        settings = replace(
            settings,
            promotion_rules=replace(
                settings.promotion_rules,
                required_no_harm_cohorts=(cohort_filter,),
            ),
        )
        LOGGER.info(
            "Cohort-isolated calibration: cohort=%s rows=%d full_panel_rows=%d",
            cohort_filter,
            len(observations),
            full_observation_count,
        )
    provenance["calibration_scope"] = "cohort" if cohort_filter else "global"
    provenance["calibration_cohort"] = cohort_filter
    provenance["calibration_observation_row_count"] = len(observations)
    candidate_limit = max(0, int(args.candidate_limit))
    specs = module.generate_weight_specs(config, candidate_limit=candidate_limit)
    policies = module.generate_selection_policies(config)
    if cohort_filter:
        policies = [policy for policy in policies if policy_supports_cohort(policy, cohort_filter)]
        if not policies:
            raise ValueError(f"No selection policies support cohort={cohort_filter!r}")
    specs, pairs, incumbent = ensure_incumbent_in_grid(
        module,
        config,
        specs,
        policies,
        candidate_limit=candidate_limit,
    )
    incumbent_id, incumbent_spec, incumbent_policy = incumbent
    incumbent_top_n = int(cfg_get(config, "biotech_scoring.production_baseline.top_n", 10))

    fold_manifest_rows: list[dict[str, object]] = []
    grid_rows: list[dict[str, object]] = []
    threshold_rows: list[dict[str, object]] = []
    curve_rows: list[dict[str, object]] = []
    fold_comparison_rows: list[dict[str, object]] = []
    cohort_rows: list[dict[str, object]] = []
    selected_rows_output: list[dict[str, object]] = []
    sleeve_rows_output: list[dict[str, object]] = []
    optuna_trial_rows: list[dict[str, object]] = []
    fold_candidate_returns: dict[int, list[Mapping[str, float]]] = defaultdict(list)
    fold_incumbent_returns: dict[int, list[Mapping[str, float]]] = defaultdict(list)
    fold_candidate_cohort_returns: dict[int, dict[str, list[Mapping[str, float]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    fold_incumbent_cohort_returns: dict[int, dict[str, list[Mapping[str, float]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    fold_candidate_regime_returns: dict[int, dict[str, list[Mapping[str, float]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    fold_incumbent_regime_returns: dict[int, dict[str, list[Mapping[str, float]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    latest_primary_contract: dict[str, object] | None = None

    for horizon, window in sorted(settings.windows.items()):
        if horizon != settings.primary_horizon:
            continue
        return_key = module.objective_return_key(horizon, params)
        folds = fold_plan.get(horizon, [])
        if not folds:
            LOGGER.warning("No complete walk-forward folds for horizon=%sd", horizon)
            continue
        for fold in folds:
            LOGGER.info("Evaluating %s", fold.fold_id)
            partition = partition_rows_for_fold(observations, fold, return_key=return_key)
            support_errors = validate_fold_support(partition, window)
            fold_manifest = {
                **fold.as_dict(),
                "calibration_cohort": cohort_filter or "ALL",
                "train_rows": len(partition.train),
                "validation_rows": len(partition.validation),
                "test_rows": len(partition.test),
                "excluded_rows": len(partition.excluded),
                "support_status": "FAIL" if support_errors else "PASS",
                "support_errors": "|".join(support_errors),
                "exclusion_reasons": json.dumps(partition.exclusion_reasons, sort_keys=True),
            }
            fold_manifest_rows.append(fold_manifest)
            if support_errors:
                LOGGER.warning("Skipping %s due to support errors: %s", fold.fold_id, support_errors)
                continue

            fold_dir = settings.output_dir / "folds" / fold.fold_id
            fold_cache = fold_dir / "frozen_fold_result.json"
            fold_signature = {
                "framework_version": FRAMEWORK_VERSION,
                "calibration_cohort": cohort_filter,
                "no_survivor_fallback": str(args.no_survivor_fallback),
                "fold": fold.as_dict(),
                "config_sha256": provenance["config_sha256"],
                "observation_csv_sha256": provenance["observation_csv_sha256"],
                "observation_manifest_sha256": provenance["observation_manifest_sha256"],
                "observation_cache_signature": provenance["cache_signature"],
                "candidate_limit": max(0, int(args.candidate_limit)),
                "optuna_enabled": settings.optuna_enabled,
                "framework_code_sha256": provenance["framework_code_sha256"],
            }
            if args.resume and fold_cache.exists():
                cached = json.loads(fold_cache.read_text(encoding="utf-8"))
                if isinstance(cached, dict) and cached.get("signature") == fold_signature:
                    cached_candidate_records = reliability_records_from_cache(cached.get("candidate_records"))
                    cached_incumbent_records = reliability_records_from_cache(cached.get("incumbent_records"))
                    cached_evaluation_dates = validated_evaluation_dates(
                        cached.get("evaluation_dates"),
                        label=f"{fold.fold_id}.evaluation_dates",
                    )
                    cached_active_returns = equal_weight_returns_by_date(
                        record_rows(cached_candidate_records, fold_id=fold.fold_id, split="candidate"),
                        return_key="objective_return",
                    )
                    cached_incumbent_active_returns = equal_weight_returns_by_date(
                        record_rows(cached_incumbent_records, fold_id=fold.fold_id, split="incumbent"),
                        return_key="objective_return",
                    )
                    cached_incumbent_returns = blend_active_alpha_with_benchmark(
                        cached_incumbent_active_returns,
                        cached_evaluation_dates,
                        active_weight=1.0,
                    )
                    cached_threshold = cached.get("threshold") or {}
                    if not isinstance(cached_threshold, Mapping):
                        raise ValueError(f"Fold cache has invalid threshold payload: {fold_cache}")
                    cached_active_weight = finite_float(cached_threshold.get("active_weight"))
                    if cached_active_weight is None:
                        raise ValueError(f"Fold cache lacks active_weight: {fold_cache}")
                    cached_candidate_returns = blend_active_alpha_with_benchmark(
                        cached_active_returns,
                        cached_evaluation_dates,
                        active_weight=cached_active_weight,
                    )
                    grid_rows.extend(load_cached_fold_grid_rows(cached, fold_dir))
                    threshold_rows.extend(cached.get("threshold_rows") or [])
                    curve_rows.extend(cached.get("curve_rows") or [])
                    cached_comparison_row = cached.get("outer_test_comparison_row")
                    if not isinstance(cached_comparison_row, Mapping):
                        raise ValueError(f"Fold cache lacks outer_test_comparison_row: {fold_cache}")
                    fold_comparison_rows.append(dict(cached_comparison_row))
                    cohort_rows.extend(cached.get("cohort_rows") or [])
                    selected_rows_output.extend(
                        record_rows(cached_candidate_records, fold_id=fold.fold_id, split="outer_test_candidate")
                    )
                    selected_rows_output.extend(
                        record_rows(cached_incumbent_records, fold_id=fold.fold_id, split="outer_test_incumbent")
                    )
                    sleeve_rows_output.extend(cached.get("sleeve_rows") or [])
                    optuna_trial_rows.extend(cached.get("optuna_trial_rows") or [])
                    fold_candidate_returns[horizon].append(cached_candidate_returns)
                    fold_incumbent_returns[horizon].append(cached_incumbent_returns)
                    cached_candidate_cohorts = returns_by_cohort(cached_candidate_records)
                    cached_incumbent_cohorts = returns_by_cohort(cached_incumbent_records)
                    for cohort in sorted(set(cached_candidate_cohorts).union(cached_incumbent_cohorts)):
                        incumbent_map = cached_incumbent_cohorts.get(cohort, {})
                        fold_candidate_cohort_returns[horizon][cohort].append(
                            blend_active_alpha_with_benchmark(
                                cached_candidate_cohorts.get(cohort, {}),
                                incumbent_map,
                                active_weight=cached_active_weight,
                            )
                        )
                        fold_incumbent_cohort_returns[horizon][cohort].append(incumbent_map)
                    cached_candidate_regimes = returns_by_regime(cached_candidate_records, regime_lookup)
                    cached_incumbent_regimes = returns_by_regime(cached_incumbent_records, regime_lookup)
                    for regime in sorted(set(cached_candidate_regimes).union(cached_incumbent_regimes)):
                        incumbent_map = cached_incumbent_regimes.get(regime, {})
                        fold_candidate_regime_returns[horizon][regime].append(
                            blend_active_alpha_with_benchmark(
                                cached_candidate_regimes.get(regime, {}),
                                incumbent_map,
                                active_weight=cached_active_weight,
                            )
                        )
                        fold_incumbent_regime_returns[horizon][regime].append(incumbent_map)
                    if horizon == settings.primary_horizon:
                        latest_primary_contract = cached
                    for secondary_evaluation in cached.get("secondary_evaluations") or []:
                        if not isinstance(secondary_evaluation, Mapping):
                            raise ValueError(
                                f"Fold cache has invalid secondary evaluation: {fold_cache}"
                            )
                        ingest_frozen_evaluation(
                            secondary_evaluation,
                            regime_lookup=regime_lookup,
                            fold_comparison_rows=fold_comparison_rows,
                            cohort_rows=cohort_rows,
                            selected_rows_output=selected_rows_output,
                            sleeve_rows_output=sleeve_rows_output,
                            fold_candidate_returns=fold_candidate_returns,
                            fold_incumbent_returns=fold_incumbent_returns,
                            fold_candidate_cohort_returns=fold_candidate_cohort_returns,
                            fold_incumbent_cohort_returns=fold_incumbent_cohort_returns,
                            fold_candidate_regime_returns=fold_candidate_regime_returns,
                            fold_incumbent_regime_returns=fold_incumbent_regime_returns,
                        )
                    LOGGER.info("Replayed validated fold cache %s", fold_cache)
                    continue

            train_grid = module.build_candidate_grid_rows(
                [dict(row) for row in partition.train],
                specs,
                policies,
                [horizon],
                list(settings.top_ns),
                sample="all",
                evaluation_split="train",
                params=params,
                max_workers=settings.max_workers,
                executor_kind=settings.executor_kind,
            )
            validation_grid = module.build_candidate_grid_rows(
                [dict(row) for row in partition.validation],
                specs,
                policies,
                [horizon],
                list(settings.top_ns),
                sample="all",
                evaluation_split="validation",
                params=params,
                max_workers=settings.max_workers,
                executor_kind=settings.executor_kind,
            )
            fold_grid_rows = [
                *({"fold_id": fold.fold_id, **row} for row in train_grid),
                *({"fold_id": fold.fold_id, **row} for row in validation_grid),
            ]
            grid_rows.extend(fold_grid_rows)
            fold_grid_cache = persist_fold_grid_rows(fold_dir, fold_grid_rows)
            shortlist = grid_shortlist(
                train_grid,
                validation_grid,
                pairs,
                limit=settings.validation_shortlist_size,
                incumbent_id=incumbent_id,
            )
            validation_incumbent, _validation_incumbent_records = incumbent_returns(
                module,
                partition.validation,
                incumbent_spec,
                incumbent_policy,
                horizon=horizon,
                top_n=incumbent_top_n,
                params=params,
            )
            optuna_trial_start = len(optuna_trial_rows)
            winner, fold_thresholds, fold_curves = evaluate_validation_shortlist(
                module,
                partition.validation,
                shortlist,
                validation_incumbent,
                horizon=horizon,
                params=params,
                settings=settings,
                min_dates=window.min_validation_dates,
                fold_id=fold.fold_id,
                trial_audit_rows=optuna_trial_rows,
            )
            fold_optuna_trial_rows = optuna_trial_rows[optuna_trial_start:]
            fold_threshold_output = [
                {"fold_id": fold.fold_id, "horizon_days": horizon, **row} for row in fold_thresholds
            ]
            fold_curve_output = [
                {"fold_id": fold.fold_id, "horizon_days": horizon, **row} for row in fold_curves
            ]
            threshold_rows.extend(fold_threshold_output)
            curve_rows.extend(fold_curve_output)
            if winner is None:
                retain_incumbent = args.no_survivor_fallback == "production_incumbent"
                fallback_label = "production incumbent" if retain_incumbent else "XBI"
                LOGGER.warning(
                    "No train/validation survivor for %s; freezing %s fallback", fold.fold_id, fallback_label
                )
                test_incumbent, test_incumbent_records = incumbent_returns(
                    module,
                    partition.test,
                    incumbent_spec,
                    incumbent_policy,
                    horizon=horizon,
                    top_n=incumbent_top_n,
                    params=params,
                )
                fallback_candidate = (
                    dict(test_incumbent)
                    if retain_incumbent
                    else {asof_date: 0.0 for asof_date in test_incumbent}
                )
                fallback_candidate_records = list(test_incumbent_records) if retain_incumbent else []
                fallback_counts: dict[str, int] = defaultdict(int)
                for record in fallback_candidate_records:
                    fallback_counts[record.asof_date] += 1
                fallback_candidate_id = (
                    "production_incumbent_fallback" if retain_incumbent else "xbi_benchmark_fallback"
                )
                fallback_candidate_name = (
                    f"Production incumbent: {incumbent_spec.candidate_name}"
                    if retain_incumbent
                    else "XBI benchmark fallback"
                )
                fallback_policy_name = (
                    incumbent_policy.policy_name if retain_incumbent else "benchmark_fallback"
                )
                fallback_reliability_class = (
                    "production_incumbent_fallback" if retain_incumbent else "benchmark_fallback"
                )
                fallback_active_weight = 1.0 if retain_incumbent else 0.0
                fallback_max_names = incumbent_top_n if retain_incumbent else 0
                fallback_return_contract = (
                    "production_incumbent_retained" if retain_incumbent else "xbi_benchmark_fallback"
                )
                fallback_comparison = paired_policy_comparison(
                    fallback_candidate,
                    test_incumbent,
                    settings.metric_settings,
                )
                fallback_threshold = {
                    "min_score_pct_of_top": 0.0,
                    "max_names": fallback_max_names,
                    "reliability_class": fallback_reliability_class,
                    "active_weight": fallback_active_weight,
                    "validation_objective": "",
                    "validation_metrics": {"fallback_reason": "no_validation_survivor"},
                }
                fallback_threshold_row = {
                    "fold_id": fold.fold_id,
                    "horizon_days": horizon,
                    "candidate_id": fallback_candidate_id,
                    "candidate_name": fallback_candidate_name,
                    "selection_policy_name": fallback_policy_name,
                    "source": f"mandatory_no_survivor_{args.no_survivor_fallback}_fallback",
                    **fallback_threshold,
                }
                threshold_rows.append(fallback_threshold_row)
                fold_threshold_output.append(fallback_threshold_row)
                fallback_comparison_row = {
                    "fold_id": fold.fold_id,
                    "horizon_days": horizon,
                    "candidate_id": fallback_candidate_id,
                    "candidate_name": fallback_candidate_name,
                    "selection_policy_name": fallback_policy_name,
                    "frozen_top_n": fallback_max_names,
                    "frozen_min_score_pct_of_top": 0.0,
                    "frozen_max_names": fallback_max_names,
                    "validation_objective": "",
                    "reliability_class": fallback_reliability_class,
                    "active_weight": fallback_active_weight,
                    "xbi_residual_weight": round(1.0 - fallback_active_weight, 6),
                    "candidate_return_contract": fallback_return_contract,
                    "test_avg_selected_names": (
                        round(sum(fallback_counts.values()) / len(test_incumbent), 6) if test_incumbent else 0.0
                    ),
                    "test_active_date_count": sum(1 for count in fallback_counts.values() if count > 0),
                    "test_evaluation_date_count": len(test_incumbent),
                    "test_active_date_coverage_pct": 100.0 if retain_incumbent and test_incumbent else 0.0,
                    **fallback_comparison,
                }
                fold_comparison_rows.append(fallback_comparison_row)
                fold_cohort_rows = cohort_comparisons(
                    fallback_candidate_records,
                    test_incumbent_records,
                    settings.metric_settings,
                    fold_id=fold.fold_id,
                    horizon=horizon,
                    active_weight=fallback_active_weight,
                )
                cohort_rows.extend(fold_cohort_rows)
                selected_rows_output.extend(
                    record_rows(
                        fallback_candidate_records,
                        fold_id=fold.fold_id,
                        split="outer_test_candidate",
                    )
                )
                selected_rows_output.extend(
                    record_rows(
                        test_incumbent_records,
                        fold_id=fold.fold_id,
                        split="outer_test_incumbent",
                    )
                )
                fold_sleeve_rows = [
                    {
                        "fold_id": fold.fold_id,
                        "horizon_days": horizon,
                        "asof_date": asof_date,
                        "selected_name_count": fallback_counts.get(asof_date, 0),
                        "reliability_class": fallback_reliability_class,
                        "active_stock_selection_weight": (
                            fallback_active_weight if fallback_counts.get(asof_date, 0) > 0 else 0.0
                        ),
                        "xbi_residual_weight": (
                            round(1.0 - fallback_active_weight, 6)
                            if fallback_counts.get(asof_date, 0) > 0
                            else 1.0
                        ),
                        "sleeve_weight_sum": 1.0,
                    }
                    for asof_date in sorted(test_incumbent)
                ]
                sleeve_rows_output.extend(fold_sleeve_rows)
                fold_candidate_returns[horizon].append(fallback_candidate)
                fold_incumbent_returns[horizon].append(test_incumbent)
                incumbent_cohorts = returns_by_cohort(test_incumbent_records)
                for cohort, incumbent_map in sorted(incumbent_cohorts.items()):
                    fold_candidate_cohort_returns[horizon][cohort].append(
                        dict(incumbent_map)
                        if retain_incumbent
                        else blend_active_alpha_with_benchmark({}, incumbent_map, active_weight=0.0)
                    )
                    fold_incumbent_cohort_returns[horizon][cohort].append(incumbent_map)
                incumbent_regimes = returns_by_regime(test_incumbent_records, regime_lookup)
                for regime, incumbent_map in sorted(incumbent_regimes.items()):
                    fold_candidate_regime_returns[horizon][regime].append(
                        dict(incumbent_map)
                        if retain_incumbent
                        else blend_active_alpha_with_benchmark({}, incumbent_map, active_weight=0.0)
                    )
                    fold_incumbent_regime_returns[horizon][regime].append(incumbent_map)
                secondary_evaluations: list[dict[str, object]] = []
                for secondary_horizon in sorted(settings.windows):
                    if secondary_horizon == horizon:
                        continue
                    secondary_evaluation = build_secondary_horizon_evaluation(
                        module,
                        partition.test,
                        candidate_spec=incumbent_spec if retain_incumbent else None,
                        candidate_policy=incumbent_policy if retain_incumbent else None,
                        candidate_id=fallback_candidate_id,
                        candidate_name=fallback_candidate_name,
                        selection_policy_name=fallback_policy_name,
                        threshold=(
                            ReliabilityThreshold(
                                min_score_pct_of_top=0.0,
                                max_names=incumbent_top_n,
                                reliability_class=fallback_reliability_class,
                                active_weight=1.0,
                                validation_objective=0.0,
                                validation_metrics={"fallback_reason": "no_validation_survivor"},
                            )
                            if retain_incumbent
                            else None
                        ),
                        frozen_top_n=fallback_max_names,
                        candidate_pool_top_n=fallback_max_names,
                        incumbent_spec=incumbent_spec,
                        incumbent_policy=incumbent_policy,
                        incumbent_top_n=incumbent_top_n,
                        horizon=secondary_horizon,
                        params=params,
                        settings=settings,
                        fold_id=fold.fold_id,
                    )
                    secondary_evaluations.append(secondary_evaluation)
                    ingest_frozen_evaluation(
                        secondary_evaluation,
                        regime_lookup=regime_lookup,
                        fold_comparison_rows=fold_comparison_rows,
                        cohort_rows=cohort_rows,
                        selected_rows_output=selected_rows_output,
                        sleeve_rows_output=sleeve_rows_output,
                        fold_candidate_returns=fold_candidate_returns,
                        fold_incumbent_returns=fold_incumbent_returns,
                        fold_candidate_cohort_returns=fold_candidate_cohort_returns,
                        fold_incumbent_cohort_returns=fold_incumbent_cohort_returns,
                        fold_candidate_regime_returns=fold_candidate_regime_returns,
                        fold_incumbent_regime_returns=fold_incumbent_regime_returns,
                    )
                frozen_payload = {
                    "signature": fold_signature,
                    "evaluation_dates": sorted(test_incumbent),
                    "candidate_id": fallback_candidate_id,
                    "candidate_pool_top_n": fallback_max_names,
                    "candidate_spec": (
                        module.weight_spec_payload(incumbent_spec)
                        if retain_incumbent
                        else {"candidate_name": "XBI benchmark fallback"}
                    ),
                    "selection_policy": (
                        module.selection_policy_payload(incumbent_policy)
                        if retain_incumbent
                        else {"policy_name": "benchmark_fallback"}
                    ),
                    "threshold": fallback_threshold,
                    "outer_test_comparison": fallback_comparison,
                    "outer_test_comparison_row": fallback_comparison_row,
                    "candidate_records": record_rows(
                        fallback_candidate_records, fold_id=fold.fold_id, split="outer_test_candidate"
                    ),
                    "incumbent_records": record_rows(
                        test_incumbent_records,
                        fold_id=fold.fold_id,
                        split="outer_test_incumbent",
                    ),
                    **fold_grid_cache,
                    "threshold_rows": fold_threshold_output,
                    "curve_rows": fold_curve_output,
                    "cohort_rows": fold_cohort_rows,
                    "sleeve_rows": fold_sleeve_rows,
                    "optuna_trial_rows": fold_optuna_trial_rows,
                    "secondary_evaluations": secondary_evaluations,
                }
                write_json(fold_cache, frozen_payload)
                if horizon == settings.primary_horizon:
                    latest_primary_contract = frozen_payload
                continue

            test_candidate_pool = candidate_records(
                module,
                partition.test,
                winner.spec,
                winner.policy,
                horizon=horizon,
                top_n=settings.candidate_pool_top_n,
                params=params,
            )
            test_selected, test_active_candidate, test_counts = apply_frozen_threshold(
                test_candidate_pool,
                winner.threshold,
            )
            test_incumbent, test_incumbent_records = incumbent_returns(
                module,
                partition.test,
                incumbent_spec,
                incumbent_policy,
                horizon=horizon,
                top_n=incumbent_top_n,
                params=params,
            )
            test_candidate = blend_active_alpha_with_benchmark(
                test_active_candidate,
                test_incumbent,
                active_weight=winner.threshold.active_weight,
            )
            comparison = paired_policy_comparison(test_candidate, test_incumbent, settings.metric_settings)
            comparison_row = {
                "fold_id": fold.fold_id,
                "horizon_days": horizon,
                "candidate_id": winner.candidate_id,
                "candidate_name": winner.spec.candidate_name,
                "selection_policy_name": winner.policy.policy_name,
                "frozen_top_n": winner.top_n,
                "frozen_min_score_pct_of_top": winner.threshold.min_score_pct_of_top,
                "frozen_max_names": winner.threshold.max_names,
                "validation_objective": winner.threshold.validation_objective,
                "reliability_class": winner.threshold.reliability_class,
                "active_weight": winner.threshold.active_weight,
                "xbi_residual_weight": round(1.0 - winner.threshold.active_weight, 6),
                "candidate_return_contract": "active_stock_alpha_plus_xbi_residual",
                "test_avg_selected_names": (
                    round(sum(test_counts.values()) / len(test_incumbent), 6) if test_incumbent else 0.0
                ),
                "test_active_date_count": sum(1 for count in test_counts.values() if count > 0),
                "test_evaluation_date_count": len(test_incumbent),
                "test_active_date_coverage_pct": (
                    round(
                        100.0 * sum(1 for count in test_counts.values() if count > 0) / len(test_incumbent),
                        6,
                    )
                    if test_incumbent
                    else 0.0
                ),
                **comparison,
            }
            fold_comparison_rows.append(comparison_row)
            fold_cohort_rows = cohort_comparisons(
                test_selected,
                test_incumbent_records,
                settings.metric_settings,
                fold_id=fold.fold_id,
                horizon=horizon,
                active_weight=winner.threshold.active_weight,
            )
            cohort_rows.extend(fold_cohort_rows)
            selected_rows_output.extend(record_rows(test_selected, fold_id=fold.fold_id, split="outer_test_candidate"))
            selected_rows_output.extend(
                record_rows(test_incumbent_records, fold_id=fold.fold_id, split="outer_test_incumbent")
            )
            fold_sleeve_rows = [
                {
                    "fold_id": fold.fold_id,
                    "horizon_days": horizon,
                    "asof_date": asof_date,
                    "selected_name_count": selected_count,
                    "reliability_class": winner.threshold.reliability_class,
                    "active_stock_selection_weight": winner.threshold.active_weight if selected_count > 0 else 0.0,
                    "xbi_residual_weight": (
                        round(1.0 - winner.threshold.active_weight, 6) if selected_count > 0 else 1.0
                    ),
                    "sleeve_weight_sum": 1.0,
                }
                for asof_date, selected_count in sorted(test_counts.items())
            ]
            sleeve_rows_output.extend(fold_sleeve_rows)
            fold_candidate_returns[horizon].append(test_candidate)
            fold_incumbent_returns[horizon].append(test_incumbent)
            test_candidate_cohorts = returns_by_cohort(test_selected)
            test_incumbent_cohorts = returns_by_cohort(test_incumbent_records)
            for cohort in sorted(set(test_candidate_cohorts).union(test_incumbent_cohorts)):
                incumbent_map = test_incumbent_cohorts.get(cohort, {})
                fold_candidate_cohort_returns[horizon][cohort].append(
                    blend_active_alpha_with_benchmark(
                        test_candidate_cohorts.get(cohort, {}),
                        incumbent_map,
                        active_weight=winner.threshold.active_weight,
                    )
                )
                fold_incumbent_cohort_returns[horizon][cohort].append(incumbent_map)
            test_candidate_regimes = returns_by_regime(test_selected, regime_lookup)
            test_incumbent_regimes = returns_by_regime(test_incumbent_records, regime_lookup)
            for regime in sorted(set(test_candidate_regimes).union(test_incumbent_regimes)):
                incumbent_map = test_incumbent_regimes.get(regime, {})
                fold_candidate_regime_returns[horizon][regime].append(
                    blend_active_alpha_with_benchmark(
                        test_candidate_regimes.get(regime, {}),
                        incumbent_map,
                        active_weight=winner.threshold.active_weight,
                    )
                )
                fold_incumbent_regime_returns[horizon][regime].append(incumbent_map)
            secondary_evaluations = []
            for secondary_horizon in sorted(settings.windows):
                if secondary_horizon == horizon:
                    continue
                secondary_evaluation = build_secondary_horizon_evaluation(
                    module,
                    partition.test,
                    candidate_spec=winner.spec,
                    candidate_policy=winner.policy,
                    candidate_id=winner.candidate_id,
                    candidate_name=winner.spec.candidate_name,
                    selection_policy_name=winner.policy.policy_name,
                    threshold=winner.threshold,
                    frozen_top_n=winner.top_n,
                    candidate_pool_top_n=settings.candidate_pool_top_n,
                    incumbent_spec=incumbent_spec,
                    incumbent_policy=incumbent_policy,
                    incumbent_top_n=incumbent_top_n,
                    horizon=secondary_horizon,
                    params=params,
                    settings=settings,
                    fold_id=fold.fold_id,
                )
                secondary_evaluations.append(secondary_evaluation)
                ingest_frozen_evaluation(
                    secondary_evaluation,
                    regime_lookup=regime_lookup,
                    fold_comparison_rows=fold_comparison_rows,
                    cohort_rows=cohort_rows,
                    selected_rows_output=selected_rows_output,
                    sleeve_rows_output=sleeve_rows_output,
                    fold_candidate_returns=fold_candidate_returns,
                    fold_incumbent_returns=fold_incumbent_returns,
                    fold_candidate_cohort_returns=fold_candidate_cohort_returns,
                    fold_incumbent_cohort_returns=fold_incumbent_cohort_returns,
                    fold_candidate_regime_returns=fold_candidate_regime_returns,
                    fold_incumbent_regime_returns=fold_incumbent_regime_returns,
                )
            frozen_payload = {
                "signature": fold_signature,
                "evaluation_dates": sorted(test_incumbent),
                "candidate_id": winner.candidate_id,
                "candidate_pool_top_n": settings.candidate_pool_top_n,
                "candidate_spec": module.weight_spec_payload(winner.spec),
                "selection_policy": module.selection_policy_payload(winner.policy),
                "threshold": winner.threshold.as_dict(),
                "outer_test_comparison": comparison,
                "outer_test_comparison_row": comparison_row,
                "candidate_records": record_rows(test_selected, fold_id=fold.fold_id, split="outer_test_candidate"),
                "incumbent_records": record_rows(
                    test_incumbent_records,
                    fold_id=fold.fold_id,
                    split="outer_test_incumbent",
                ),
                **fold_grid_cache,
                "threshold_rows": fold_threshold_output,
                "curve_rows": fold_curve_output,
                "cohort_rows": fold_cohort_rows,
                "sleeve_rows": fold_sleeve_rows,
                "optuna_trial_rows": fold_optuna_trial_rows,
                "secondary_evaluations": secondary_evaluations,
            }
            write_json(fold_cache, frozen_payload)
            if horizon == settings.primary_horizon:
                latest_primary_contract = frozen_payload

    for horizon in sorted(fold_candidate_cohort_returns):
        cohorts = set(fold_candidate_cohort_returns[horizon]).union(fold_incumbent_cohort_returns[horizon])
        for cohort in sorted(cohorts):
            candidate_active = aggregate_nonoverlapping_returns(
                fold_candidate_cohort_returns[horizon].get(cohort, [])
            )
            incumbent_active = aggregate_nonoverlapping_returns(
                fold_incumbent_cohort_returns[horizon].get(cohort, [])
            )
            candidate, incumbent = aligned_residual_sleeves(
                candidate_active,
                incumbent_active,
                active_weight=1.0,
            )
            cohort_rows.append(
                {
                    "fold_id": "aggregate",
                    "horizon_days": horizon,
                    "cohort": cohort,
                    **paired_policy_comparison(candidate, incumbent, settings.metric_settings),
                }
            )

    regime_rows: list[dict[str, object]] = []
    for horizon in sorted(fold_candidate_regime_returns):
        regimes = set(fold_candidate_regime_returns[horizon]).union(fold_incumbent_regime_returns[horizon])
        for regime in sorted(regimes):
            candidate_active = aggregate_nonoverlapping_returns(
                fold_candidate_regime_returns[horizon].get(regime, [])
            )
            incumbent_active = aggregate_nonoverlapping_returns(
                fold_incumbent_regime_returns[horizon].get(regime, [])
            )
            candidate, incumbent = aligned_residual_sleeves(
                candidate_active,
                incumbent_active,
                active_weight=1.0,
            )
            regime_rows.append(
                {
                    "horizon_days": horizon,
                    "regime": regime,
                    "regime_source_field": regime_field or "unclassified_no_pit_regime_field",
                    **paired_policy_comparison(candidate, incumbent, settings.metric_settings),
                }
            )

    aggregate_comparisons: dict[int, dict[str, object]] = {}
    decision_objects: dict[int, PromotionDecision] = {}
    for horizon in sorted(fold_candidate_returns):
        candidate = aggregate_nonoverlapping_returns(fold_candidate_returns[horizon])
        incumbent = aggregate_nonoverlapping_returns(fold_incumbent_returns[horizon])
        comparison = paired_policy_comparison(candidate, incumbent, settings.metric_settings)
        fold_rows = [
            row
            for row in fold_comparison_rows
            if int(finite_float(row.get("horizon_days")) or 0) == horizon
        ]
        evaluation_dates = sum(int(finite_float(row.get("test_evaluation_date_count")) or 0) for row in fold_rows)
        active_dates = sum(int(finite_float(row.get("test_active_date_count")) or 0) for row in fold_rows)
        selected_name_dates = sum(
            (finite_float(row.get("test_avg_selected_names")) or 0.0)
            * int(finite_float(row.get("test_evaluation_date_count")) or 0)
            for row in fold_rows
        )
        fallback_folds = sum(
            1
            for row in fold_rows
            if str(row.get("candidate_id") or "")
            in {
                "xbi_benchmark_fallback",
                "production_incumbent_fallback",
            }
        )
        comparison.update(
            {
                "candidate_active_date_coverage_pct": (
                    round(100.0 * active_dates / evaluation_dates, 6) if evaluation_dates else 0.0
                ),
                "candidate_avg_selected_names": (
                    round(selected_name_dates / evaluation_dates, 6) if evaluation_dates else 0.0
                ),
                "calibration_fallback_fold_count": fallback_folds,
                "calibration_fallback_frequency_pct": (
                    round(100.0 * fallback_folds / len(fold_rows), 6) if fold_rows else 100.0
                ),
            }
        )
        aggregate_comparisons[horizon] = comparison
        decision_objects[horizon] = decide_promotion(comparison, fold_rows, settings.promotion_rules)
    primary = decision_objects.get(settings.primary_horizon)
    promotion_fold_contract = deployable_fold_contract(latest_primary_contract)
    live_deployment_ready, live_deployment_readiness_reason = live_contract_readiness(
        promotion_fold_contract,
        config,
    )
    statistical_primary_decision: dict[str, object] = {}
    if primary is not None:
        primary = apply_no_harm_gate(
            primary,
            no_harm_reason_codes(
                primary_horizon=settings.primary_horizon,
                horizon_comparisons=aggregate_comparisons,
                cohort_comparisons=cohort_rows,
                rules=settings.promotion_rules,
            ),
        )
        statistical_primary_decision = primary.as_dict()
        primary = apply_deployment_readiness_gate(
            primary,
            deployment_ready=live_deployment_ready,
            reason="live_scorer_parity_failed",
        )
        decision_objects[settings.primary_horizon] = primary
        if primary.authorized and promotion_fold_contract:
            threshold_payload = promotion_fold_contract.get("threshold") or {}
            if not isinstance(threshold_payload, dict):
                raise ValueError("Latest primary fold contract has an invalid threshold payload")
            calibrated_weight = finite_float(threshold_payload.get("active_weight"))
            if calibrated_weight is None:
                raise ValueError("Latest primary fold contract lacks a calibrated active weight")
            governed_weight = deployment_active_weight(primary, calibrated_weight, settings.promotion_rules)
            threshold_payload["calibrated_active_weight"] = calibrated_weight
            threshold_payload["active_weight"] = governed_weight
            threshold_payload["xbi_residual_weight"] = round(1.0 - governed_weight, 10)
            if governed_weight < calibrated_weight:
                threshold_payload["deployment_weight_cap_reason"] = "provisional_promotion_cap"
            promotion_fold_contract["threshold"] = threshold_payload
    decisions = {horizon: decision.as_dict() for horizon, decision in decision_objects.items()}

    primary_decision = decisions.get(settings.primary_horizon)
    contract_authorized = bool(primary_decision and primary_decision.get("production_promotion_authorized"))
    latest_policy_payload = (
        promotion_fold_contract.get("selection_policy", {})
        if promotion_fold_contract
        else {}
    )
    latest_policy_name = (
        str(latest_policy_payload.get("policy_name") or "")
        if isinstance(latest_policy_payload, Mapping)
        else ""
    )
    contract = {
        "contract_version": PROMOTION_CONTRACT_VERSION,
        "framework_version": FRAMEWORK_VERSION,
        "calibration_scope": "cohort" if cohort_filter else "global",
        "calibration_cohort": cohort_filter,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "primary_horizon": settings.primary_horizon,
        "production_promotion_authorized": contract_authorized,
        "live_deployment_ready": live_deployment_ready,
        "live_deployment_readiness_reason": live_deployment_readiness_reason,
        "live_deployment_policy_name": latest_policy_name,
        "promotion_decision": primary_decision or {},
        "statistical_promotion_decision": statistical_primary_decision,
        "latest_primary_fold_contract": promotion_fold_contract,
        "incumbent": {
            "candidate_id": incumbent_id,
            "candidate_name": incumbent_spec.candidate_name,
            "selection_policy_name": incumbent_policy.policy_name,
            "top_n": incumbent_top_n,
        },
        "source_provenance": provenance,
        "monitoring_contract": dict(settings.monitoring_contract),
        "activation_status": "candidate_requires_explicit_activation" if contract_authorized else "not_authorized",
    }
    write_csv(settings.output_dir / "walk_forward_fold_manifest.csv", fold_manifest_rows)
    write_csv(settings.output_dir / "walk_forward_candidate_metrics.csv", grid_rows)
    write_csv(settings.output_dir / "walk_forward_score_thresholds.csv", threshold_rows)
    write_csv(settings.output_dir / "score_reliability_thresholds.csv", threshold_rows)
    write_csv(settings.output_dir / "walk_forward_score_reliability_curves.csv", curve_rows)
    write_csv(settings.output_dir / "score_reliability_curves.csv", curve_rows)
    write_csv(settings.output_dir / "walk_forward_outer_test_comparisons.csv", fold_comparison_rows)
    write_csv(settings.output_dir / "walk_forward_paired_policy_comparisons.csv", fold_comparison_rows)
    write_csv(settings.output_dir / "walk_forward_cohort_no_harm.csv", cohort_rows)
    write_csv(settings.output_dir / "walk_forward_cohort_metrics.csv", cohort_rows)
    write_csv(settings.output_dir / "walk_forward_regime_metrics.csv", regime_rows)
    write_csv(settings.output_dir / "walk_forward_selected_tickers.csv", selected_rows_output)
    write_csv(settings.output_dir / "adaptive_selection_replay.csv", selected_rows_output)
    write_csv(settings.output_dir / "adaptive_sleeve_allocation_replay.csv", sleeve_rows_output)
    profit_factor_rows = [
        {
            key: value
            for key, value in row.items()
            if key in {"fold_id", "horizon_days", "candidate_id", "candidate_name", "selection_policy_name"}
            or "profit_factor" in key
            or key in {"candidate_win_count", "candidate_loss_count", "incumbent_win_count", "incumbent_loss_count"}
        }
        for row in fold_comparison_rows
    ]
    tail_rows = [
        {
            key: value
            for key, value in row.items()
            if key in {"fold_id", "horizon_days", "candidate_id", "candidate_name", "selection_policy_name"}
            or any(token in key for token in ("loss20", "loss40", "cvar", "drawdown", "contribution"))
        }
        for row in fold_comparison_rows
    ]
    write_csv(settings.output_dir / "walk_forward_profit_factor_robustness.csv", profit_factor_rows)
    write_csv(settings.output_dir / "walk_forward_tail_risk_metrics.csv", tail_rows)
    if settings.optuna_enabled:
        write_csv(settings.output_dir / "optuna_fold_trials.csv", optuna_trial_rows)
    decision_payload = {str(k): v for k, v in decisions.items()}
    write_json(settings.output_dir / "walk_forward_promotion_decisions.json", decision_payload)
    write_json(settings.output_dir / "promotion_decision.json", decision_payload)
    write_json(
        settings.output_dir / "walk_forward_statistical_promotion_decision.json",
        statistical_primary_decision,
    )
    write_json(settings.output_dir / "production_policy_contract_candidate.json", contract)
    decision_markdown = promotion_markdown(decisions)
    (settings.output_dir / "walk_forward_promotion_decision.md").write_text(decision_markdown, encoding="utf-8")
    (settings.output_dir / "promotion_decision.md").write_text(decision_markdown, encoding="utf-8")
    artifact_paths = sorted(
        path
        for path in settings.output_dir.iterdir()
        if path.is_file() and path.name != "walk_forward_run_manifest.json"
    )
    manifest = {
        "status": "success" if decisions else "insufficient_complete_folds",
        "framework_version": FRAMEWORK_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "calibration_scope": "cohort" if cohort_filter else "global",
        "calibration_cohort": cohort_filter,
        "primary_horizon": settings.primary_horizon,
        "optuna_enabled": settings.optuna_enabled,
        "outer_test_used_by_optimizer": False,
        "optuna_trial_count": len(optuna_trial_rows),
        "fold_count": len(fold_manifest_rows),
        "completed_fold_count": len(fold_comparison_rows),
        "production_promotion_authorized": contract_authorized,
        "live_deployment_ready": live_deployment_ready,
        "live_deployment_readiness_reason": live_deployment_readiness_reason,
        "source_provenance": provenance,
        "monitoring_contract": dict(settings.monitoring_contract),
        "regime_source_field": regime_field,
        "artifacts": {
            path.name: {"path": str(path), "sha256": sha256_file(path)} for path in artifact_paths
        },
    }
    write_json(settings.output_dir / "walk_forward_run_manifest.json", manifest)
    final_contract_authorized = contract_authorized
    if cfg_get(config, "calibration.walk_forward.profitability_replay.enabled", False) is True:
        profitability_script = PACKAGE_ROOT / "scripts" / "62_compare_biotech_portfolio_profitability.py"
        subprocess.run(
            [
                sys.executable,
                str(profitability_script),
                "--config",
                str(config_path),
                "--calibration-output-dir",
                str(settings.output_dir),
            ],
            check=True,
        )
        profitability_manifest_path = settings.output_dir / "portfolio_profitability_manifest.json"
        profitability_manifest = json.loads(profitability_manifest_path.read_text(encoding="utf-8"))
        if not isinstance(profitability_manifest, Mapping):
            raise ValueError("Profitability replay manifest root must be a mapping")
        final_contract_path = settings.output_dir / "production_policy_contract_profitability_candidate.json"
        final_contract = (
            json.loads(final_contract_path.read_text(encoding="utf-8"))
            if final_contract_path.exists()
            else {}
        )
        final_contract_authorized = bool(
            isinstance(final_contract, Mapping)
            and final_contract.get("production_promotion_authorized") is True
        )
        manifest["production_promotion_authorized"] = final_contract_authorized
        manifest["profitability_replay"] = dict(profitability_manifest)
        artifact_paths = sorted(
            path
            for path in settings.output_dir.iterdir()
            if path.is_file() and path.name != "walk_forward_run_manifest.json"
        )
        manifest["artifacts"] = {
            path.name: {"path": str(path), "sha256": sha256_file(path)} for path in artifact_paths
        }
        write_json(settings.output_dir / "walk_forward_run_manifest.json", manifest)
    LOGGER.info(
        "Walk-forward framework complete: folds=%d evaluated=%d promotion_authorized=%s output=%s",
        len(fold_manifest_rows),
        len(fold_comparison_rows),
        final_contract_authorized,
        settings.output_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

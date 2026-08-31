"""Label-blind preregistration for Consumer Defensive v2 calibration.

This module deliberately has no SQL that reads ``forward_*`` columns.  It
freezes the candidate, split, scoring, portfolio, cost, liquidity, framework,
shared-service, and methodology contracts before the execution process may
inspect any historical return label.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from consumer_defensive.core.config import ConfigBundle
from consumer_defensive.core.promotion_framework_v2 import (
    REQUIRED_COHORTS,
    REQUIRED_HORIZONS,
    canonical_sha256,
    framework_sha256,
    validate_framework,
)
from consumer_defensive.core.scoring_features import CORE_COMPONENT_SPECS
from consumer_defensive.core.shared_services import shared_service_contract_sha256
from consumer_defensive.core.stage7_scoring import (
    stage7_component_weights,
    stage7_contract_sha256,
)
from consumer_defensive.core.stage8_calibration import verify_factor_campaign


CANDIDATE_REGISTRY_SCHEMA = "consumer_defensive_calibration_candidate_registry_v2"
PREREGISTRATION_SCHEMA = "consumer_defensive_calibration_preregistration_v2"

SPLIT_POLICY: dict[str, Any] = {
    "initial_train_observations_after_purge": 30,
    "validation_observations_before_purge": 12,
    # Three-date blocks retain at least 30 outer-OOS observations after the
    # 126-session purge while keeping an even 10-12 fold census for PBO.
    "outer_test_observations_per_fold": 3,
    "outer_step_observations": 3,
    "embargo_observations": 0,
    "odd_fold_rule": "largest_even_chronological_prefix",
    "minimum_outer_test_observations": 30,
    "minimum_outer_folds": 4,
    "maximum_outer_folds": 18,
    "split_chronology_census": "all_true_month_end_horizon_complete_dates",
    "portfolio_readiness_scope": (
        "all_preregistered_candidates_feature_only_label_blind"
    ),
    "portfolio_readiness_fold_policy": (
        "reject_whole_fold_when_any_validation_or_test_date_is_unready_"
        "before_maximum_even_and_minimum_gates"
    ),
    "unready_train_date_policy": "allow_as_chronological_burn_in_only",
    "train_partition_role": (
        "chronology_and_label_completion_purge_burn_in_no_candidate_fit"
    ),
    "candidate_fit_performed_on_train": False,
}
SCORING_POLICY: dict[str, Any] = {
    'ticker_scope_policy': 'config_bound_exclusions_before_normalization',
    "normalization_scope": "point_in_time_cohort_then_universe_fallback",
    "neutral_score": 50.0,
    "minimum_normalization_peer_count": 5,
    "minimum_data_quality_confidence": 0.65,
    "maximum_missing_component_weight": 0.35,
    "missing_specialized_value_policy": "neutral_score_no_weight_redistribution",
    "optional_core_quality_policy": "neutral_score_excluded_from_missingness_gate",
    "structural_source_era_policy": "neutral_score_excluded_from_applicable_quality_denominator",
    "nonapplicable_specialized_policy": "zero_weight_excluded",
}
PORTFOLIO_POLICY: dict[str, Any] = {
    "construction": "long_only_equal_weight_top_score",
    "top_fraction": 0.25,
    "minimum_cross_section": 8,
    "minimum_positions": 5,
    "maximum_positions": 10,
    "maximum_single_name_weight": 0.20,
    "maximum_gross_exposure": 1.0,
    "entry_lag_trading_sessions": 1,
    "live_path_rebalance_policy": "buy_and_hold_sleeves_between_next_signal_rebalances",
    "final_path_sessions_after_last_signal": 21,
    "candidate_selection_metric_policy": "horizon_specific_forward_labels_and_relative_metrics",
    "realized_path_metric_policy": "daily_realized_monthly_rebalanced_absolute_profitability",
    "turnover_definition": "one_half_l1_including_cash",
}
COST_POLICY: dict[str, Any] = {
    "one_way_transaction_cost_bps": 20.0,
    "charge_initial_entry": True,
    "charge_only_on_rebalance_dates": True,
}
LIQUIDITY_POLICY: dict[str, Any] = {
    "reference_gross_notional_usd": 1_000_000.0,
    "maximum_fraction_of_adv": 0.10,
    "adv_component": "avg_dollar_volume_63d",
    "capacity_ratio_definition": "executable_notional_over_required_position_notional",
}
CANDIDATE_POLICY: dict[str, Any] = {
    "core_tilt_multiplier": 1.25,
    "specialized_weight": 0.05,
    "specialized_acceptance": "same_scope_horizon_direction_primary_and_robustness_required",
}

_CANDIDATE_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "model_family",
        "asof_date",
        "framework_sha256",
        "shared_service_contract_sha256",
        "source_stage6c_run_id",
        "source_stage6c_panel_sha256",
        "factor_campaign_id",
        "factor_registry_sha256",
        "accepted_factor_cells_sha256",
        "candidate_count",
        "candidates",
        "registered_before_label_evaluation",
        "production_promotion_enabled",
        "portfolio_write_enabled",
        "payload_sha256",
    }
)
_CANDIDATE_KEYS = frozenset(
    {
        "candidate_id",
        "cohort",
        "horizon_sessions",
        "candidate_kind",
        "core_weights",
        "specialized_weights",
        "accepted_factor_cell_ids",
        "factor_directions",
        "scoring_policy_id",
        "portfolio_policy_id",
        "definition_sha256",
    }
)
_PREREG_KEYS = frozenset(
    {
        "schema_version",
        "model_family",
        "asof_date",
        "framework_sha256",
        "shared_service_contract_sha256",
        "source_contract",
        "candidate_registry_sha256",
        "split_policy",
        "scoring_policy",
        "portfolio_policy",
        "cost_policy",
        "liquidity_policy",
        "estimator_settings",
        "code_file_sha256s",
        "code_sha256",
        "registered_before_label_evaluation",
        "forward_label_accessed",
        "production_promotion_enabled",
        "portfolio_write_enabled",
        "payload_sha256",
    }
)


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_date(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a canonical ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be a canonical ISO date")
    return value


def _sha(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _digest(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _finite(value: Any, *, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be finite numeric data")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be finite numeric data") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite numeric data")
    return result


def _normalized_weights(weights: Mapping[str, float], *, total: float = 1.0) -> dict[str, float]:
    parsed = {str(name): _finite(value, label=f"weight.{name}") for name, value in weights.items()}
    if not parsed or any(value < 0.0 for value in parsed.values()):
        raise ValueError("candidate weights must be nonnegative and nonempty")
    observed = sum(parsed.values())
    if observed <= 0.0:
        raise ValueError("candidate weights require positive gross exposure")
    scaled = {name: round(value * total / observed, 12) for name, value in sorted(parsed.items())}
    difference = round(total - sum(scaled.values()), 12)
    if difference:
        largest = max(scaled, key=scaled.get)
        scaled[largest] = round(scaled[largest] + difference, 12)
    return scaled


def _candidate_definition(
    *,
    cohort: str,
    horizon: int,
    kind: str,
    core_weights: Mapping[str, float],
    specialized_weights: Mapping[str, float] | None = None,
    accepted_cells: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    specialized = {
        str(name): round(_finite(value, label=f"specialized_weight.{name}"), 12)
        for name, value in sorted((specialized_weights or {}).items())
    }
    specialized_total = sum(specialized.values())
    if not 0.0 <= specialized_total <= 0.20:
        raise ValueError("specialized candidate weight is outside the preregistered range")
    core = _normalized_weights(core_weights, total=1.0 - specialized_total)
    directions: dict[str, str] = {}
    cell_ids: list[str] = []
    for cell in sorted(accepted_cells, key=lambda row: str(row["cell_id"])):
        factor = str(cell["factor_id"])
        direction = str(cell["factor_direction"])
        if factor in directions and directions[factor] != direction:
            raise ValueError(f"accepted factor direction conflict: {factor}")
        directions[factor] = direction
        cell_ids.append(str(cell["cell_id"]))
    definition = {
        "cohort": cohort,
        "horizon_sessions": horizon,
        "candidate_kind": kind,
        "core_weights": core,
        "specialized_weights": specialized,
        "accepted_factor_cell_ids": cell_ids,
        "factor_directions": dict(sorted(directions.items())),
        "scoring_policy_id": _sha(SCORING_POLICY),
        "portfolio_policy_id": _sha(PORTFOLIO_POLICY),
    }
    definition_sha = _sha(definition)
    return {
        "candidate_id": f"cdv2_{definition_sha[:24]}",
        **definition,
        "definition_sha256": definition_sha,
    }


def _supported_specialized_cells(
    accepted_cells: Sequence[Mapping[str, Any]], *, cohort: str, horizon: int
) -> dict[str, list[dict[str, Any]]]:
    applicable_scopes = {cohort, "consumer_defensive"}
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for raw in accepted_cells:
        cell = dict(raw)
        if int(cell["horizon_trading_days"]) != horizon or str(cell["scope_id"]) not in applicable_scopes:
            continue
        grouped.setdefault((str(cell["factor_id"]), str(cell["target_name"])), []).append(cell)
    output: dict[str, list[dict[str, Any]]] = {}
    primary = "forward_xlp_residual_return"
    robust = "forward_spy_beta_residual_return"
    factors = {factor for factor, _ in grouped}
    for factor in sorted(factors):
        primary_cells = grouped.get((factor, primary), [])
        robust_cells = grouped.get((factor, robust), [])
        if not primary_cells or not robust_cells:
            continue
        cells = primary_cells + robust_cells
        if len({str(cell["factor_direction"]) for cell in cells}) != 1:
            continue
        output[factor] = sorted(cells, key=lambda row: str(row["cell_id"]))
    return output


def build_candidate_registry(
    bundle: ConfigBundle,
    *,
    framework: Mapping[str, Any],
    shared_contract: Mapping[str, Any],
    asof_date: str,
    stage6c_run: Mapping[str, Any],
    campaign_summary: Mapping[str, Any],
    accepted_factor_cells: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    validated_framework = validate_framework(framework)
    asof = _canonical_date(asof_date, label="asof_date")
    if str(stage6c_run["status"]) != "complete" or str(stage6c_run["asof_date"]) != asof:
        raise ValueError("preregistration requires an exact complete Stage 6C run")
    baseline = stage7_component_weights(bundle)
    names = {spec.name for spec in CORE_COMPONENT_SPECS}
    if set(baseline) != names or not math.isclose(sum(baseline.values()), 1.0, abs_tol=1e-10):
        raise ValueError("Stage 7 seed weights do not cover the exact v2 core component census")
    groups: dict[str, list[str]] = {}
    for spec in CORE_COMPONENT_SPECS:
        groups.setdefault(spec.group, []).append(spec.name)
    candidates: list[dict[str, Any]] = []
    multiplier = float(CANDIDATE_POLICY["core_tilt_multiplier"])
    for cohort in sorted(REQUIRED_COHORTS):
        for horizon in REQUIRED_HORIZONS:
            candidates.append(
                _candidate_definition(
                    cohort=cohort,
                    horizon=horizon,
                    kind="stage7_seed",
                    core_weights=baseline,
                )
            )
            for component in sorted(baseline):
                tilted = dict(baseline)
                tilted[component] *= multiplier
                candidates.append(
                    _candidate_definition(
                        cohort=cohort,
                        horizon=horizon,
                        kind=f"core_component_tilt:{component}",
                        core_weights=tilted,
                    )
                )
            for group, component_names in sorted(groups.items()):
                tilted = dict(baseline)
                for component in component_names:
                    tilted[component] *= multiplier
                candidates.append(
                    _candidate_definition(
                        cohort=cohort,
                        horizon=horizon,
                        kind=f"core_group_tilt:{group}",
                        core_weights=tilted,
                    )
                )
            supported = _supported_specialized_cells(
                accepted_factor_cells,
                cohort=cohort,
                horizon=horizon,
            )
            for factor, cells in supported.items():
                candidates.append(
                    _candidate_definition(
                        cohort=cohort,
                        horizon=horizon,
                        kind=f"validated_specialized_overlay:{factor}",
                        core_weights=baseline,
                        specialized_weights={factor: CANDIDATE_POLICY["specialized_weight"]},
                        accepted_cells=cells,
                    )
                )
    candidates.sort(key=lambda row: (row["cohort"], row["horizon_sessions"], row["candidate_id"]))
    if len({row["candidate_id"] for row in candidates}) != len(candidates):
        raise ValueError("candidate identifiers must be globally unique")
    payload: dict[str, Any] = {
        "schema_version": CANDIDATE_REGISTRY_SCHEMA,
        "model_family": "consumer_defensive",
        "asof_date": asof,
        "framework_sha256": framework_sha256(validated_framework),
        "shared_service_contract_sha256": shared_service_contract_sha256(shared_contract),
        "source_stage6c_run_id": int(stage6c_run["stage6c_run_id"]),
        "source_stage6c_panel_sha256": str(stage6c_run["panel_sha256"]),
        "factor_campaign_id": str(campaign_summary["campaign_id"]),
        "factor_registry_sha256": str(campaign_summary["registry_sha256"]),
        "accepted_factor_cells_sha256": _sha(
            sorted((dict(cell) for cell in accepted_factor_cells), key=lambda row: str(row["cell_id"]))
        ),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "registered_before_label_evaluation": True,
        "production_promotion_enabled": False,
        "portfolio_write_enabled": False,
    }
    payload["payload_sha256"] = canonical_sha256(payload)
    return validate_candidate_registry(payload)


def _methodology_paths(repository_root: Path, bundle: ConfigBundle) -> tuple[Path, ...]:
    relative = (
        'consumer_defensive/core/calibration_scope.py',
        "consumer_defensive/core/calibration_preregistration_v2.py",
        "consumer_defensive/core/calibration_execution_v2.py",
        "consumer_defensive/core/calibration_v2.py",
        "consumer_defensive/core/historical_features_v2.py",
        "consumer_defensive/core/institutional_history_v2.py",
        "consumer_defensive/core/stage8_calibration.py",
        "consumer_defensive/core/stage8_calibration_v2.py",
        "consumer_defensive/core/stage7_scoring.py",
        "consumer_defensive/core/stage6c_panel.py",
        "consumer_defensive/core/scoring_features.py",
        "consumer_defensive/core/financial_pipeline.py",
        "consumer_defensive/core/market_data.py",
        "consumer_defensive/core/terminal_events.py",
        "consumer_defensive/core/stage3_runtime.py",
        "consumer_defensive/core/config.py",
        "consumer_defensive/core/shared_services.py",
        "consumer_defensive/core/promotion_framework_v2.py",
        "consumer_defensive/core/promotion_engine_v3.py",
        "consumer_defensive/core/promotion_input_v3.py",
        "consumer_defensive/scripts/26_validate_consumer_defensive_promotion_framework_v2.py",
        "consumer_defensive/scripts/28_preregister_consumer_defensive_calibration_v2.py",
        "consumer_defensive/scripts/29_run_consumer_defensive_calibration_v2.py",
        "consumer_defensive/data/consumer_defensive_financial_concept_map.yaml",
        "consumer_defensive/data/consumer_defensive_market_data_policy.yaml",
        "consumer_defensive/data/consumer_defensive_terminal_event_policy.yaml",
        "consumer_defensive/data/consumer_defensive_promotion_framework_v2.yaml",
        "consumer_defensive/data/consumer_defensive_shared_service_contract_v1.yaml",
        "consumer_defensive/system_csvs/consumer_defensive_terminal_events.csv",
    )
    paths = [repository_root / value for value in relative]
    paths.append(bundle.path.resolve())
    return tuple(path.resolve() for path in paths)


def methodology_hashes(repository_root: Path, bundle: ConfigBundle) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in _methodology_paths(repository_root.resolve(), bundle):
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(f"methodology file is missing or unsafe: {path}")
        key = path.relative_to(repository_root.resolve()).as_posix()
        hashes[key] = _digest_file(path)
    return dict(sorted(hashes.items()))


def build_preregistration(
    bundle: ConfigBundle,
    *,
    repository_root: Path,
    framework: Mapping[str, Any],
    shared_contract: Mapping[str, Any],
    stage6c_run: Mapping[str, Any],
    candidate_registry: Mapping[str, Any],
) -> dict[str, Any]:
    registry = validate_candidate_registry(candidate_registry)
    validated_framework = validate_framework(framework)
    code_files = methodology_hashes(repository_root, bundle)
    source_contract = {
        "stage6c_run_id": int(stage6c_run["stage6c_run_id"]),
        "stage6c_asof_date": str(stage6c_run["asof_date"]),
        "stage6c_history_start": str(stage6c_run["history_start"]),
        "stage6c_panel_sha256": str(stage6c_run["panel_sha256"]),
        "stage6c_panel_row_count": int(stage6c_run["panel_row_count"]),
        "stage6c_evaluation_date_count": int(stage6c_run["evaluation_date_count"]),
        "stage7_seed_contract_sha256": stage7_contract_sha256(bundle),
        "factor_campaign_id": registry["factor_campaign_id"],
        "factor_registry_sha256": registry["factor_registry_sha256"],
        "accepted_factor_cells_sha256": registry["accepted_factor_cells_sha256"],
    }
    payload: dict[str, Any] = {
        "schema_version": PREREGISTRATION_SCHEMA,
        "model_family": "consumer_defensive",
        "asof_date": registry["asof_date"],
        "framework_sha256": framework_sha256(validated_framework),
        "shared_service_contract_sha256": shared_service_contract_sha256(shared_contract),
        "source_contract": source_contract,
        "candidate_registry_sha256": registry["payload_sha256"],
        "split_policy": dict(SPLIT_POLICY),
        "scoring_policy": dict(SCORING_POLICY),
        "portfolio_policy": dict(PORTFOLIO_POLICY),
        "cost_policy": dict(COST_POLICY),
        "liquidity_policy": dict(LIQUIDITY_POLICY),
        "estimator_settings": dict(validated_framework["evaluation"]["estimator_settings"]),
        "code_file_sha256s": code_files,
        "code_sha256": _sha(code_files),
        "registered_before_label_evaluation": True,
        "forward_label_accessed": False,
        "production_promotion_enabled": False,
        "portfolio_write_enabled": False,
    }
    payload["payload_sha256"] = canonical_sha256(payload)
    return validate_preregistration(payload, candidate_registry=registry)


def validate_candidate_registry(payload: Mapping[str, Any]) -> dict[str, Any]:
    registry = dict(payload)
    if set(registry) != _CANDIDATE_ROOT_KEYS:
        raise ValueError("candidate registry root schema is not exact")
    if registry["schema_version"] != CANDIDATE_REGISTRY_SCHEMA or registry["model_family"] != "consumer_defensive":
        raise ValueError("unsupported candidate registry")
    _canonical_date(registry["asof_date"], label="candidate_registry.asof_date")
    for key in (
        "framework_sha256",
        "shared_service_contract_sha256",
        "source_stage6c_panel_sha256",
        "factor_registry_sha256",
        "accepted_factor_cells_sha256",
        "payload_sha256",
    ):
        _digest(registry[key], label=f"candidate_registry.{key}")
    if registry["payload_sha256"] != canonical_sha256(registry):
        raise ValueError("candidate registry self-hash mismatch")
    if isinstance(registry["source_stage6c_run_id"], bool) or not isinstance(registry["source_stage6c_run_id"], int) or registry["source_stage6c_run_id"] <= 0:
        raise ValueError("candidate registry Stage 6C run id must be positive")
    candidates = registry["candidates"]
    if not isinstance(candidates, list) or registry["candidate_count"] != len(candidates):
        raise ValueError("candidate registry count is inconsistent")
    seen: set[str] = set()
    census: dict[tuple[str, int], int] = {}
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or set(candidate) != _CANDIDATE_KEYS:
            raise ValueError("candidate definition schema is not exact")
        row = dict(candidate)
        if row["cohort"] not in REQUIRED_COHORTS or row["horizon_sessions"] not in REQUIRED_HORIZONS:
            raise ValueError("candidate cohort/horizon is unsupported")
        reconstructed = {key: row[key] for key in _CANDIDATE_KEYS if key not in {"candidate_id", "definition_sha256"}}
        expected = _sha(reconstructed)
        if row["definition_sha256"] != expected or row["candidate_id"] != f"cdv2_{expected[:24]}":
            raise ValueError("candidate identity is not definition-bound")
        if row["candidate_id"] in seen:
            raise ValueError("candidate identifiers are duplicated")
        seen.add(row["candidate_id"])
        core = row["core_weights"]
        specialized = row["specialized_weights"]
        if not isinstance(core, Mapping) or set(core) != {spec.name for spec in CORE_COMPONENT_SPECS}:
            raise ValueError("candidate core-weight census is not exact")
        if not isinstance(specialized, Mapping):
            raise ValueError("candidate specialized weights must be a mapping")
        total = sum(_finite(value, label="candidate weight") for value in core.values()) + sum(
            _finite(value, label="candidate specialized weight") for value in specialized.values()
        )
        if not math.isclose(total, 1.0, abs_tol=1e-10):
            raise ValueError("candidate weights must sum to one")
        if any(_finite(value, label="candidate weight") < 0.0 for value in (*core.values(), *specialized.values())):
            raise ValueError("candidate weights cannot be negative")
        census[(row["cohort"], int(row["horizon_sessions"]))] = census.get(
            (row["cohort"], int(row["horizon_sessions"])), 0
        ) + 1
    expected_census = {(cohort, horizon) for cohort in REQUIRED_COHORTS for horizon in REQUIRED_HORIZONS}
    if set(census) != expected_census or any(count < 2 for count in census.values()):
        raise ValueError("each cohort/horizon requires at least two preregistered candidates")
    if registry["registered_before_label_evaluation"] is not True:
        raise ValueError("candidate registry is not label-blind")
    if registry["production_promotion_enabled"] is not False or registry["portfolio_write_enabled"] is not False:
        raise ValueError("candidate registry cannot activate production")
    return registry


def validate_preregistration(
    payload: Mapping[str, Any], *, candidate_registry: Mapping[str, Any]
) -> dict[str, Any]:
    prereg = dict(payload)
    registry = validate_candidate_registry(candidate_registry)
    if set(prereg) != _PREREG_KEYS:
        raise ValueError("preregistration root schema is not exact")
    if prereg["schema_version"] != PREREGISTRATION_SCHEMA or prereg["model_family"] != "consumer_defensive":
        raise ValueError("unsupported calibration preregistration")
    _canonical_date(prereg["asof_date"], label="preregistration.asof_date")
    for key in (
        "framework_sha256",
        "shared_service_contract_sha256",
        "candidate_registry_sha256",
        "code_sha256",
        "payload_sha256",
    ):
        _digest(prereg[key], label=f"preregistration.{key}")
    if prereg["payload_sha256"] != canonical_sha256(prereg):
        raise ValueError("preregistration self-hash mismatch")
    if prereg["candidate_registry_sha256"] != registry["payload_sha256"]:
        raise ValueError("preregistration candidate-registry binding failed")
    if prereg["asof_date"] != registry["asof_date"]:
        raise ValueError("preregistration/candidate asof mismatch")
    expected_policies = {
        "split_policy": SPLIT_POLICY,
        "scoring_policy": SCORING_POLICY,
        "portfolio_policy": PORTFOLIO_POLICY,
        "cost_policy": COST_POLICY,
        "liquidity_policy": LIQUIDITY_POLICY,
    }
    for key, expected in expected_policies.items():
        if prereg[key] != expected:
            raise ValueError(f"preregistration {key} is not frozen")
    files = prereg["code_file_sha256s"]
    if not isinstance(files, Mapping) or not files:
        raise ValueError("preregistration methodology hashes are required")
    for name, value in files.items():
        if not isinstance(name, str) or not name:
            raise ValueError("methodology path must be nonblank")
        _digest(value, label=f"methodology.{name}")
    if prereg["code_sha256"] != _sha(dict(sorted(files.items()))):
        raise ValueError("preregistration code hash is inconsistent")
    if (
        prereg["registered_before_label_evaluation"] is not True
        or prereg["forward_label_accessed"] is not False
        or prereg["production_promotion_enabled"] is not False
        or prereg["portfolio_write_enabled"] is not False
    ):
        raise ValueError("preregistration safety controls are not frozen")
    return prereg


def read_stage6c_run_metadata(conn: sqlite3.Connection, *, stage6c_run_id: int) -> dict[str, Any]:
    """Read only non-label Stage 6C run metadata for preregistration."""

    sql = """
        SELECT stage6c_run_id,asof_date,history_start,evaluation_frequency,
               entry_lag_trading_days,horizons_json,freshness_days,
               config_sha256,metric_policy_sha256,source_stage6b_run_id,status,
               evaluation_date_count,panel_row_count,numeric_row_count,panel_sha256
        FROM stage6c_panel_run WHERE stage6c_run_id=?
    """
    if "forward_" in sql.lower():
        raise AssertionError("preregistration SQL cannot access forward labels")
    row = conn.execute(sql, (stage6c_run_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown Stage 6C run: {stage6c_run_id}")
    result = dict(row)
    if result["status"] != "complete":
        raise ValueError("Stage 6C run is not complete")
    return result


def publish_immutable_json(path: Path, payload: Mapping[str, Any]) -> None:
    resolved = path.expanduser().resolve()
    encoded = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n"
    if resolved.exists():
        if resolved.is_symlink() or not resolved.is_file():
            raise ValueError(f"immutable artifact path is unsafe: {resolved}")
        if resolved.read_text(encoding="utf-8") != encoded:
            raise FileExistsError(f"refusing to overwrite divergent immutable artifact: {resolved}")
        return
    resolved.parent.mkdir(parents=True, exist_ok=True)
    temporary = resolved.with_name(f".{resolved.name}.tmp")
    if temporary.exists():
        raise FileExistsError(f"stale immutable-artifact temporary exists: {temporary}")
    temporary.write_text(encoded, encoding="utf-8", newline="\n")
    temporary.replace(resolved)


def load_json(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or resolved.is_symlink():
        raise FileNotFoundError(f"required immutable JSON artifact is missing or unsafe: {resolved}")
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON artifact must be an object: {resolved}")
    return value


__all__ = [
    "CANDIDATE_POLICY",
    "CANDIDATE_REGISTRY_SCHEMA",
    "COST_POLICY",
    "LIQUIDITY_POLICY",
    "PORTFOLIO_POLICY",
    "PREREGISTRATION_SCHEMA",
    "SCORING_POLICY",
    "SPLIT_POLICY",
    "build_candidate_registry",
    "build_preregistration",
    "load_json",
    "methodology_hashes",
    "publish_immutable_json",
    "read_stage6c_run_metadata",
    "validate_candidate_registry",
    "validate_preregistration",
    "verify_factor_campaign",
]


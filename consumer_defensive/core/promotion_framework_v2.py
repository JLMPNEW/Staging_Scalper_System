"""Fail-closed Consumer Defensive calibration and promotion authority.

The contract is sector-owned.  It validates the frozen framework, horizon-level
outer-OOS evidence, and an immutable one-step decision chain.  It deliberately
contains no imports from another sector package.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml


FRAMEWORK_SCHEMA = "consumer_defensive_promotion_framework_v2"
DECISION_SCHEMA = "consumer_defensive_calibration_decision_v2"
MODEL_FAMILY = "consumer_defensive"
FRAMEWORK_VERSION = "consumer_defensive_calibration_and_promotion_v2"
REQUIRED_COHORTS = frozenset(
    {
        "beverages",
        "consumer_staples_distribution_retail",
        "household_personal_tobacco",
        "packaged_foods_agricultural_products",
    }
)
REQUIRED_HORIZONS = (21, 63, 126)
REQUIRED_HORIZON_KEYS = frozenset(str(value) for value in REQUIRED_HORIZONS)
ACTIVE_STATES = frozenset({"active_pilot", "active_scaled", "active_full"})
DECISION_STATES = frozenset({"benchmark_production", *ACTIVE_STATES, "rollback"})
REQUIRED_PERFORMANCE_METRICS = frozenset(
    {
        "paired_net_alpha_lcb",
        "net_alpha_mean",
        "absolute_profit_factor",
        "relative_profit_factor",
        "robust_profit_factor",
        "deflated_sharpe_ratio",
        "probability_of_backtest_overfitting",
        "maximum_drawdown",
        "expected_shortfall_95",
        "turnover",
        "average_transaction_cost",
        "liquidity_capacity_ratio",
        "winner_concentration_hhi",
        "maximum_single_name_weight",
        "paired_observation_count",
        "positive_return_count",
        "negative_return_count",
    }
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

EXPECTED_ACTIVE_EVIDENCE_FLOORS = {
    "minimum_paired_net_alpha_lcb": 0.0,
    "minimum_absolute_profit_factor": 1.0,
    "minimum_relative_profit_factor": 1.0,
    "minimum_robust_profit_factor": 1.0,
    "minimum_deflated_sharpe_ratio": 0.80,
    "maximum_probability_of_backtest_overfitting": 0.50,
    "maximum_drawdown": 0.30,
    "minimum_expected_shortfall_95": -0.10,
    "maximum_turnover": 2.0,
    "maximum_average_transaction_cost": 0.01,
    "minimum_liquidity_capacity_ratio": 1.0,
    "maximum_winner_concentration_hhi": 0.25,
    "maximum_single_name_weight": 0.20,
    "minimum_paired_observations": 30,
    "minimum_positive_return_observations": 10,
    "minimum_negative_return_observations": 5,
}
EXPECTED_CAPITAL_CAPS = {
    "rollback": 0.0,
    "benchmark_production": 0.0,
    "active_pilot": 0.20,
    "active_scaled": 0.60,
    "active_full": 1.0,
}
EXPECTED_ESTIMATOR_SETTINGS = {
    "bootstrap_confidence": 0.95,
    "bootstrap_samples": 2_000,
    "bootstrap_seed": 17,
    "block_size_by_horizon": {"21": 1, "63": 3, "126": 6},
    "winsor_fraction": 0.05,
    "expected_shortfall_tail_probability": 0.05,
    "maximum_pbo_combinations": 65_536,
    "require_even_pbo_folds": True,
    "maximum_portfolio_gross_exposure": 1.0,
    "candidate_matrix_measure": "paired_net_alpha_by_outer_fold",
}
EXPECTED_STATE_TRANSITION_POLICY = {
    "maximum_one_tier_per_decision": True,
    "require_immediate_predecessor": True,
    "require_new_input_panel": True,
    "minimum_new_paired_observations_per_horizon_for_advancement": 1,
    "minimum_active_pilot_dwell_days": 63,
    "minimum_active_scaled_dwell_days": 126,
    "reset_to_pilot_on_code_or_candidate_change": True,
}

_ALLOWED_SHARED_SERVICES = frozenset(
    {
        "dedicated_parser",
        "factor_validation",
        "global_orchestrator",
        "market_positioning",
        "norgate",
        "portfolio_layer",
        "sec_edgar",
        "sec_insider",
        "ticker_mapping",
        "yahoo_finance",
    }
)
_LEGACY_EVIDENCE = {
    "stage8_v1": "diagnostic_only_burned_holdout",
    "stage9_v1": "diagnostic_only_burned_holdout",
    "future_only_evidence_protocol": "retired_not_admissible",
    "biotech_framework": "conceptual_reference_only_no_code_or_runtime_dependency",
}
_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "model_family",
        "framework_version",
        "status",
        "ownership",
        "legacy_evidence",
        "cohorts",
        "evaluation",
        "active_evidence_floors",
        "state_transition_policy",
        "capital_tiers",
    }
)
_DECISION_KEYS = frozenset(
    {
        "schema_version",
        "model_family",
        "asof_date",
        "framework_sha256",
        "shared_service_contract_sha256",
        "input_panel_sha256",
        "fold_registry_sha256",
        "candidate_registry_sha256",
        "code_sha256",
        "decision_sequence",
        "previous_decision_sha256",
        "calibration_completed",
        "cohorts",
        "payload_sha256",
    }
)
_COHORT_DECISION_KEYS = frozenset(
    {
        "prior_state",
        "prior_state_entered_asof",
        "state",
        "state_entered_asof",
        "active_cap",
        "horizon_performance",
        "horizon_evidence",
        "failed_gates",
        "transition_blockers",
    }
)
_HORIZON_EVIDENCE_KEYS = frozenset(
    {
        "evaluation_role",
        "horizon_sessions",
        "observation_count",
        "observation_ids_sha256",
        "fold_ids_sha256",
        "signal_start_date",
        "signal_end_date",
        "latest_label_completion_date",
        "candidate_matrix_sha256",
        "selected_weights_sha256",
        "realized_return_stream_sha256",
        "realized_return_count",
        "realized_return_start_date",
        "realized_return_end_date",
    }
)


def canonical_sha256(payload: Mapping[str, Any]) -> str:
    """Hash a JSON object after excluding only its top-level self hash."""

    body = {key: value for key, value in payload.items() if key != "payload_sha256"}
    encoded = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    return dict(value)


def _exact_mapping(value: Any, expected_keys: frozenset[str], *, label: str) -> dict[str, Any]:
    payload = _mapping(value, label=label)
    if set(payload) != expected_keys:
        raise ValueError(f"{label} must contain exactly {sorted(expected_keys)}")
    return payload


def _finite(value: Any, *, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be finite numeric evidence")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be finite numeric evidence") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be finite numeric evidence")
    return parsed


def _integer(value: Any, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _sha(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _canonical_date(value: Any, *, label: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a canonical ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be a canonical ISO date")
    return parsed


def load_framework(path: Path) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve()
    payload = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    return validate_framework(_mapping(payload, label="promotion framework"))


def validate_framework(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate every frozen framework field and reject unknown fields."""

    framework = _exact_mapping(payload, _ROOT_KEYS, label="promotion framework")
    if framework["schema_version"] != FRAMEWORK_SCHEMA:
        raise ValueError("unsupported Consumer Defensive promotion framework")
    if framework["model_family"] != MODEL_FAMILY:
        raise ValueError("promotion framework has the wrong model family")
    if framework["framework_version"] != FRAMEWORK_VERSION:
        raise ValueError("promotion framework version changed")
    if framework["status"] != "recalibration_required":
        raise ValueError("v2 framework must remain recalibration_required")

    ownership = _exact_mapping(
        framework["ownership"],
        frozenset(
            {
                "sector_owner",
                "cross_sector_code_imports_allowed",
                "shared_service_contract_path",
                "shared_service_contract_sha256",
                "allowed_shared_services",
            }
        ),
        label="ownership",
    )
    if ownership["sector_owner"] != MODEL_FAMILY:
        raise ValueError("Consumer Defensive must own its promotion implementation")
    if ownership["cross_sector_code_imports_allowed"] is not False:
        raise ValueError("cross-sector code imports must be disabled")
    if ownership["shared_service_contract_path"] != (
        "consumer_defensive/data/consumer_defensive_shared_service_contract_v1.yaml"
    ):
        raise ValueError("shared-service contract path changed")
    _sha(
        ownership["shared_service_contract_sha256"],
        label="ownership.shared_service_contract_sha256",
    )
    services = ownership["allowed_shared_services"]
    if (
        not isinstance(services, list)
        or len(services) != len(_ALLOWED_SHARED_SERVICES)
        or set(services) != _ALLOWED_SHARED_SERVICES
    ):
        raise ValueError("allowed shared-service boundary changed")

    if framework["legacy_evidence"] != _LEGACY_EVIDENCE:
        raise ValueError("legacy-evidence admissibility contract changed")
    cohorts = _mapping(framework["cohorts"], label="cohorts")
    if set(cohorts) != REQUIRED_COHORTS:
        raise ValueError("framework must define exactly four Consumer cohorts")
    for cohort, definition in cohorts.items():
        if _mapping(definition, label=f"cohort {cohort}") != {"calibrated_independently": True}:
            raise ValueError(f"{cohort}: independent calibration contract changed")

    evaluation = _exact_mapping(
        framework["evaluation"],
        frozenset(
            {
                "benchmark",
                "broad_market_benchmark",
                "horizons_sessions",
                "splitter",
                "purge_uses_label_completion_date",
                "outer_folds_are_selection_blind",
                "returns_are_net_of_costs",
                "candidates_are_preregistered",
                "all_horizons_required_for_active",
                "required_performance_metrics",
                "estimator_settings",
            }
        ),
        label="evaluation",
    )
    expected = {
        "benchmark": "XLP",
        "broad_market_benchmark": "SPY",
        "horizons_sessions": list(REQUIRED_HORIZONS),
        "splitter": "nested_purged_walk_forward",
        "purge_uses_label_completion_date": True,
        "outer_folds_are_selection_blind": True,
        "returns_are_net_of_costs": True,
        "candidates_are_preregistered": True,
        "all_horizons_required_for_active": True,
    }
    for key, value in expected.items():
        if evaluation[key] != value:
            raise ValueError(f"evaluation.{key} changed")
    metric_names = evaluation["required_performance_metrics"]
    if (
        not isinstance(metric_names, list)
        or len(metric_names) != len(REQUIRED_PERFORMANCE_METRICS)
        or set(metric_names) != REQUIRED_PERFORMANCE_METRICS
    ):
        raise ValueError("required performance-metric contract changed")
    settings = _exact_mapping(
        evaluation["estimator_settings"],
        frozenset(EXPECTED_ESTIMATOR_SETTINGS),
        label="evaluation.estimator_settings",
    )
    for key, expected_value in EXPECTED_ESTIMATOR_SETTINGS.items():
        if settings[key] != expected_value:
            raise ValueError(f"evaluation.estimator_settings.{key} changed")

    floors = _exact_mapping(
        framework["active_evidence_floors"],
        frozenset(EXPECTED_ACTIVE_EVIDENCE_FLOORS),
        label="active_evidence_floors",
    )
    for key, expected_value in EXPECTED_ACTIVE_EVIDENCE_FLOORS.items():
        actual = _finite(floors[key], label=f"active_evidence_floors.{key}")
        if not math.isclose(actual, float(expected_value), abs_tol=1e-12):
            raise ValueError(f"active_evidence_floors.{key} changed")

    transition = _exact_mapping(
        framework["state_transition_policy"],
        frozenset(EXPECTED_STATE_TRANSITION_POLICY),
        label="state_transition_policy",
    )
    if transition != EXPECTED_STATE_TRANSITION_POLICY:
        raise ValueError("state-transition policy changed")

    tiers = _exact_mapping(framework["capital_tiers"], DECISION_STATES, label="capital_tiers")
    for state, expected_cap in EXPECTED_CAPITAL_CAPS.items():
        item = _exact_mapping(tiers[state], frozenset({"active_cap"}), label=f"capital_tiers.{state}")
        cap = _finite(item["active_cap"], label=f"capital_tiers.{state}.active_cap")
        if not math.isclose(cap, expected_cap, abs_tol=1e-12):
            raise ValueError(f"capital_tiers.{state}.active_cap changed")
    return framework


def framework_sha256(framework: Mapping[str, Any]) -> str:
    return canonical_sha256(validate_framework(framework))


def validate_performance(payload: Mapping[str, Any], *, label: str) -> dict[str, float | int]:
    """Validate exact metrics, ranges, and integral observation counts."""

    metrics = _exact_mapping(payload, REQUIRED_PERFORMANCE_METRICS, label=label)
    parsed: dict[str, float | int] = {name: _finite(value, label=f"{label}.{name}") for name, value in metrics.items()}
    for name in (
        "absolute_profit_factor",
        "relative_profit_factor",
        "robust_profit_factor",
    ):
        if not 0.0 <= float(parsed[name]) <= 1_000_000.0:
            raise ValueError(f"{label}.{name} is outside its supported range")
    if float(parsed["turnover"]) < 0.0:
        raise ValueError(f"{label}.turnover cannot be negative")
    if not 0.0 <= float(parsed["average_transaction_cost"]) < 1.0:
        raise ValueError(f"{label}.average_transaction_cost must be in [0, 1)")
    if not 0.0 < float(parsed["liquidity_capacity_ratio"]) <= 1_000_000.0:
        raise ValueError(f"{label}.liquidity_capacity_ratio is outside its range")
    for name in (
        "deflated_sharpe_ratio",
        "probability_of_backtest_overfitting",
        "maximum_drawdown",
        "winner_concentration_hhi",
        "maximum_single_name_weight",
    ):
        if not 0.0 <= float(parsed[name]) <= 1.0:
            raise ValueError(f"{label}.{name} must be in [0, 1]")
    if float(parsed["expected_shortfall_95"]) <= -1.0:
        raise ValueError(f"{label}.expected_shortfall_95 must exceed -1")
    for name in (
        "paired_observation_count",
        "positive_return_count",
        "negative_return_count",
    ):
        parsed[name] = _integer(metrics[name], label=f"{label}.{name}")
    if int(parsed["positive_return_count"]) + int(parsed["negative_return_count"]) > int(
        parsed["paired_observation_count"]
    ):
        raise ValueError(f"{label}: signed counts exceed paired observations")
    return parsed


def performance_gate_failures(
    performance: Mapping[str, Any],
    *,
    framework: Mapping[str, Any],
    label: str = "performance",
) -> tuple[str, ...]:
    validated_framework = validate_framework(framework)
    parsed = validate_performance(performance, label=label)
    floors = validated_framework["active_evidence_floors"]
    checks = {
        "paired_net_alpha_lcb": float(parsed["paired_net_alpha_lcb"]) > floors["minimum_paired_net_alpha_lcb"],
        "absolute_profit_factor": float(parsed["absolute_profit_factor"]) >= floors["minimum_absolute_profit_factor"],
        "relative_profit_factor": float(parsed["relative_profit_factor"]) >= floors["minimum_relative_profit_factor"],
        "robust_profit_factor": float(parsed["robust_profit_factor"]) >= floors["minimum_robust_profit_factor"],
        "deflated_sharpe_ratio": float(parsed["deflated_sharpe_ratio"]) >= floors["minimum_deflated_sharpe_ratio"],
        "probability_of_backtest_overfitting": float(parsed["probability_of_backtest_overfitting"])
        <= floors["maximum_probability_of_backtest_overfitting"],
        "maximum_drawdown": float(parsed["maximum_drawdown"]) <= floors["maximum_drawdown"],
        "expected_shortfall_95": float(parsed["expected_shortfall_95"]) >= floors["minimum_expected_shortfall_95"],
        "turnover": float(parsed["turnover"]) <= floors["maximum_turnover"],
        "average_transaction_cost": float(parsed["average_transaction_cost"])
        <= floors["maximum_average_transaction_cost"],
        "liquidity_capacity_ratio": float(parsed["liquidity_capacity_ratio"])
        >= floors["minimum_liquidity_capacity_ratio"],
        "winner_concentration_hhi": float(parsed["winner_concentration_hhi"])
        <= floors["maximum_winner_concentration_hhi"],
        "maximum_single_name_weight": float(parsed["maximum_single_name_weight"])
        <= floors["maximum_single_name_weight"],
        "paired_observation_count": int(parsed["paired_observation_count"]) >= floors["minimum_paired_observations"],
        "positive_return_count": int(parsed["positive_return_count"]) >= floors["minimum_positive_return_observations"],
        "negative_return_count": int(parsed["negative_return_count"]) >= floors["minimum_negative_return_observations"],
    }
    return tuple(sorted(name for name, passed in checks.items() if not passed))


def next_state_for_evidence(prior_state: str, *, failed_gates: tuple[str, ...]) -> str:
    """Compatibility helper for the evidence-only transition before dwell controls."""

    if prior_state not in DECISION_STATES:
        raise ValueError("prior state is unsupported")
    if failed_gates:
        if prior_state in ACTIVE_STATES:
            return "rollback"
        return prior_state
    return {
        "rollback": "active_pilot",
        "benchmark_production": "active_pilot",
        "active_pilot": "active_scaled",
        "active_scaled": "active_full",
        "active_full": "active_full",
    }[prior_state]


def _validate_horizon_evidence(
    payload: Mapping[str, Any],
    *,
    cohort: str,
    horizon: int,
    performance: Mapping[str, float | int],
    decision_asof: date,
) -> dict[str, Any]:
    label = f"{cohort}.horizon_evidence.{horizon}"
    evidence = _exact_mapping(payload, _HORIZON_EVIDENCE_KEYS, label=label)
    if evidence["evaluation_role"] != "outer_test":
        raise ValueError(f"{label} must contain outer-test evidence only")
    if evidence["horizon_sessions"] != horizon:
        raise ValueError(f"{label} horizon mismatch")
    count = _integer(evidence["observation_count"], label=f"{label}.observation_count", minimum=1)
    if count != performance["paired_observation_count"]:
        raise ValueError(f"{label} observation count does not match performance")
    for key in (
        "observation_ids_sha256",
        "fold_ids_sha256",
        "candidate_matrix_sha256",
        "selected_weights_sha256",
        "realized_return_stream_sha256",
    ):
        _sha(evidence[key], label=f"{label}.{key}")
    signal_start = _canonical_date(evidence["signal_start_date"], label=f"{label}.signal_start_date")
    signal_end = _canonical_date(evidence["signal_end_date"], label=f"{label}.signal_end_date")
    label_end = _canonical_date(
        evidence["latest_label_completion_date"],
        label=f"{label}.latest_label_completion_date",
    )
    realized_start = _canonical_date(
        evidence["realized_return_start_date"], label=f"{label}.realized_return_start_date"
    )
    realized_end = _canonical_date(evidence["realized_return_end_date"], label=f"{label}.realized_return_end_date")
    _integer(evidence["realized_return_count"], label=f"{label}.realized_return_count", minimum=1)
    if not signal_start <= signal_end <= label_end <= decision_asof:
        raise ValueError(f"{label} has future or inverted label chronology")
    if not realized_start <= realized_end <= decision_asof:
        raise ValueError(f"{label} has future or inverted realized-return chronology")
    return evidence


def _validate_decision_structure(
    payload: Mapping[str, Any], *, framework: Mapping[str, Any]
) -> tuple[dict[str, Any], date]:
    if not isinstance(payload, Mapping) or payload.get("schema_version") != DECISION_SCHEMA:
        raise ValueError("legacy or unsupported Consumer calibration decision")
    decision = _exact_mapping(payload, _DECISION_KEYS, label="calibration decision")
    if decision["model_family"] != MODEL_FAMILY:
        raise ValueError("calibration decision has the wrong model family")
    if decision["framework_sha256"] != framework_sha256(framework):
        raise ValueError("decision is not bound to the v2 framework")
    if decision["shared_service_contract_sha256"] != framework["ownership"]["shared_service_contract_sha256"]:
        raise ValueError("decision is not bound to the frozen shared-service contract")
    supplied_hash = _sha(decision["payload_sha256"], label="decision.payload_sha256")
    if supplied_hash != canonical_sha256(decision):
        raise ValueError("calibration decision self-hash mismatch")
    asof = _canonical_date(decision["asof_date"], label="decision.asof_date")
    if asof > date.today():
        raise ValueError("calibration decision cannot be future-dated")
    for key in (
        "input_panel_sha256",
        "fold_registry_sha256",
        "candidate_registry_sha256",
        "code_sha256",
    ):
        _sha(decision[key], label=f"decision.{key}")
    _integer(decision["decision_sequence"], label="decision.decision_sequence", minimum=1)
    if decision["previous_decision_sha256"] is not None:
        _sha(decision["previous_decision_sha256"], label="decision.previous_decision_sha256")
    if decision["calibration_completed"] is not True:
        raise ValueError("v2 recalibration is incomplete")

    cohort_decisions = _mapping(decision["cohorts"], label="decision.cohorts")
    if set(cohort_decisions) != REQUIRED_COHORTS:
        raise ValueError("decision must cover exactly four cohorts")
    for cohort, raw in cohort_decisions.items():
        item = _exact_mapping(raw, _COHORT_DECISION_KEYS, label=f"decision.cohorts.{cohort}")
        if item["prior_state"] not in DECISION_STATES or item["state"] not in DECISION_STATES:
            raise ValueError(f"{cohort}: invalid production state")
        state_entered = _canonical_date(item["state_entered_asof"], label=f"{cohort}.state_entered_asof")
        if state_entered > asof:
            raise ValueError(f"{cohort}: state entry cannot postdate the decision")
        if item["prior_state_entered_asof"] is not None:
            prior_entered = _canonical_date(
                item["prior_state_entered_asof"], label=f"{cohort}.prior_state_entered_asof"
            )
            if prior_entered > asof:
                raise ValueError(f"{cohort}: prior state entry cannot postdate the decision")
        horizons = _mapping(item["horizon_performance"], label=f"{cohort}.horizon_performance")
        evidence = _mapping(item["horizon_evidence"], label=f"{cohort}.horizon_evidence")
        if set(horizons) != REQUIRED_HORIZON_KEYS or set(evidence) != REQUIRED_HORIZON_KEYS:
            raise ValueError(f"{cohort}: exact 21/63/126 evidence is required")
        failures: list[str] = []
        for horizon in REQUIRED_HORIZONS:
            key = str(horizon)
            performance = validate_performance(horizons[key], label=f"{cohort}.horizon_{key}")
            failures.extend(
                f"{key}:{name}"
                for name in performance_gate_failures(
                    horizons[key], framework=framework, label=f"{cohort}.horizon_{key}"
                )
            )
            _validate_horizon_evidence(
                evidence[key],
                cohort=cohort,
                horizon=horizon,
                performance=performance,
                decision_asof=asof,
            )
        if item["failed_gates"] != sorted(failures):
            raise ValueError(f"{cohort}: failed-gate evidence is inconsistent")
        blockers = item["transition_blockers"]
        if not isinstance(blockers, list) or blockers != sorted(set(blockers)):
            raise ValueError(f"{cohort}: transition blockers must be sorted and unique")
        cap = _finite(item["active_cap"], label=f"{cohort}.active_cap")
        expected_cap = float(framework["capital_tiers"][item["state"]]["active_cap"])
        if not math.isclose(cap, expected_cap, abs_tol=1e-12):
            raise ValueError(f"{cohort}: active cap is not bound to its tier")
    return decision, asof


def _expected_transition(
    *,
    prior_state: str,
    failed_gates: list[str],
    material_model_change: bool,
    elapsed_days: int,
    growth_blockers: list[str],
    framework: Mapping[str, Any],
) -> tuple[str, list[str]]:
    if failed_gates:
        if prior_state in ACTIVE_STATES:
            return "rollback", []
        return prior_state, []
    if material_model_change and prior_state in ACTIVE_STATES:
        return "active_pilot", ["material_model_change_reset"]
    if prior_state in {"benchmark_production", "rollback"}:
        return "active_pilot", []
    if prior_state == "active_full":
        return "active_full", []
    policy = framework["state_transition_policy"]
    required_dwell = (
        policy["minimum_active_pilot_dwell_days"]
        if prior_state == "active_pilot"
        else policy["minimum_active_scaled_dwell_days"]
    )
    blockers = list(growth_blockers)
    if elapsed_days < required_dwell:
        blockers.append(f"minimum_{prior_state}_dwell_days")
    blockers = sorted(set(blockers))
    if blockers:
        return prior_state, blockers
    return ("active_scaled" if prior_state == "active_pilot" else "active_full"), []


def _validate_calibration_decision_link(
    payload: Mapping[str, Any],
    *,
    framework: Mapping[str, Any],
    previous_decision: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate horizon evidence and the immediate immutable decision link."""

    validated_framework = validate_framework(framework)
    decision, asof = _validate_decision_structure(payload, framework=validated_framework)
    sequence = decision["decision_sequence"]
    if sequence == 1:
        if previous_decision is not None or decision["previous_decision_sha256"] is not None:
            raise ValueError("genesis decision cannot declare a predecessor")
        for cohort, item in decision["cohorts"].items():
            if item["prior_state"] != "benchmark_production" or item["prior_state_entered_asof"] is not None:
                raise ValueError(f"{cohort}: genesis must start from benchmark_production")
            expected_state = "active_pilot" if not item["failed_gates"] else "benchmark_production"
            if item["state"] != expected_state or item["transition_blockers"] != []:
                raise ValueError(f"{cohort}: invalid genesis transition")
            if item["state_entered_asof"] != decision["asof_date"]:
                raise ValueError(f"{cohort}: genesis state entry must equal decision asof")
        return decision

    if previous_decision is None:
        raise ValueError("non-genesis decision requires its immediate predecessor")
    predecessor, predecessor_asof = _validate_decision_structure(previous_decision, framework=validated_framework)
    if sequence != predecessor["decision_sequence"] + 1:
        raise ValueError("decision sequence is not contiguous")
    if decision["previous_decision_sha256"] != predecessor["payload_sha256"]:
        raise ValueError("previous decision hash mismatch")
    if asof <= predecessor_asof:
        raise ValueError("decision asof must advance beyond its predecessor")
    if decision["input_panel_sha256"] == predecessor["input_panel_sha256"]:
        raise ValueError("a new decision requires a new input panel")
    material_change = any(decision[key] != predecessor[key] for key in ("candidate_registry_sha256", "code_sha256"))
    minimum_growth = validated_framework["state_transition_policy"][
        "minimum_new_paired_observations_per_horizon_for_advancement"
    ]
    for cohort in REQUIRED_COHORTS:
        current = decision["cohorts"][cohort]
        prior = predecessor["cohorts"][cohort]
        if current["prior_state"] != prior["state"]:
            raise ValueError(f"{cohort}: prior state is not predecessor-bound")
        if current["prior_state_entered_asof"] != prior["state_entered_asof"]:
            raise ValueError(f"{cohort}: prior state-entry date is not predecessor-bound")
        growth_blockers: list[str] = []
        for horizon in REQUIRED_HORIZONS:
            key = str(horizon)
            current_evidence = current["horizon_evidence"][key]
            prior_evidence = prior["horizon_evidence"][key]
            if current_evidence["observation_ids_sha256"] == prior_evidence["observation_ids_sha256"]:
                raise ValueError(f"{cohort}/{key}: observation identities were replayed")
            if current_evidence["latest_label_completion_date"] <= prior_evidence["latest_label_completion_date"]:
                raise ValueError(f"{cohort}/{key}: completed OOS evidence did not advance")
            if current_evidence["realized_return_stream_sha256"] == prior_evidence["realized_return_stream_sha256"]:
                raise ValueError(f"{cohort}/{key}: realized-return stream was replayed")
            growth = current_evidence["observation_count"] - prior_evidence["observation_count"]
            if growth < minimum_growth:
                growth_blockers.append(f"minimum_new_paired_observations_{key}")
        entered = _canonical_date(prior["state_entered_asof"], label=f"{cohort}.predecessor_state_entered")
        expected_state, expected_blockers = _expected_transition(
            prior_state=prior["state"],
            failed_gates=current["failed_gates"],
            material_model_change=material_change,
            elapsed_days=(asof - entered).days,
            growth_blockers=growth_blockers,
            framework=validated_framework,
        )
        if current["state"] != expected_state or current["transition_blockers"] != expected_blockers:
            raise ValueError(f"{cohort}: capital-state transition is inconsistent")
        reset_same_tier = material_change and prior["state"] in ACTIVE_STATES and not current["failed_gates"]
        expected_entered = (
            decision["asof_date"]
            if reset_same_tier or expected_state != prior["state"]
            else prior["state_entered_asof"]
        )
        if current["state_entered_asof"] != expected_entered:
            raise ValueError(f"{cohort}: state-entry date is inconsistent")
    return decision


def validate_calibration_decision(
    payload: Mapping[str, Any],
    *,
    framework: Mapping[str, Any],
    previous_decision: Mapping[str, Any] | None = None,
    decision_history: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate the complete hash chain from the strict genesis decision."""

    validated_framework = validate_framework(framework)
    if not isinstance(payload, Mapping):
        raise ValueError("calibration decision must be a mapping")
    sequence = payload.get("decision_sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        if payload.get("schema_version") != DECISION_SCHEMA:
            raise ValueError("legacy or unsupported Consumer calibration decision")
        raise ValueError("decision_sequence must be a positive integer")
    if sequence == 1:
        if decision_history not in (None, (), []):
            raise ValueError("genesis decision cannot receive decision history")
        return _validate_calibration_decision_link(
            payload,
            framework=validated_framework,
            previous_decision=previous_decision,
        )

    if decision_history is None:
        if sequence != 2:
            raise ValueError("decisions after sequence 2 require complete genesis-to-predecessor history")
        if previous_decision is None:
            raise ValueError("non-genesis decision requires its immediate predecessor")
        history = [previous_decision]
    else:
        if isinstance(decision_history, (str, bytes)):
            raise ValueError("decision_history must be an ordered artifact sequence")
        history = list(decision_history)
        if len(history) != sequence - 1:
            raise ValueError("decision history does not match decision_sequence")
        if previous_decision is not None and history[-1] != previous_decision:
            raise ValueError("decision history does not end at previous_decision")
    predecessor: Mapping[str, Any] | None = None
    for expected_sequence, artifact in enumerate(history, start=1):
        if not isinstance(artifact, Mapping) or artifact.get("decision_sequence") != expected_sequence:
            raise ValueError("decision history is not contiguous from genesis")
        predecessor = _validate_calibration_decision_link(
            artifact,
            framework=validated_framework,
            previous_decision=predecessor,
        )
    return _validate_calibration_decision_link(
        payload,
        framework=validated_framework,
        previous_decision=predecessor,
    )


__all__ = [
    "ACTIVE_STATES",
    "DECISION_SCHEMA",
    "DECISION_STATES",
    "EXPECTED_ACTIVE_EVIDENCE_FLOORS",
    "EXPECTED_ESTIMATOR_SETTINGS",
    "EXPECTED_STATE_TRANSITION_POLICY",
    "FRAMEWORK_SCHEMA",
    "MODEL_FAMILY",
    "REQUIRED_COHORTS",
    "REQUIRED_HORIZONS",
    "REQUIRED_HORIZON_KEYS",
    "REQUIRED_PERFORMANCE_METRICS",
    "canonical_sha256",
    "framework_sha256",
    "load_framework",
    "next_state_for_evidence",
    "performance_gate_failures",
    "validate_calibration_decision",
    "validate_framework",
    "validate_performance",
]

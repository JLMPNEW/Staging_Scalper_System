"""Consumer-owned four-layer calibration and promotion engine.

Version 3 deliberately lives beside the immutable v2 decoder.  It converts
matched outer-OOS strategy/benchmark paths into four distinct decisions:

1. data and safety validity (hard vetoes only),
2. normalized economic merit,
3. statistical-confidence shrinkage, and
4. standard, capacity-aware production authority.

The module has no imports from another sector package.  Calibration remains
report-only; a separately pinned activation registry is the only artifact that
Portfolio Layer may consume.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml


FRAMEWORK_SCHEMA = "consumer_defensive_promotion_framework_v3"
FRAMEWORK_VERSION = "consumer_defensive_four_layer_standard_allocation_v3"
PROMOTION_INPUT_SCHEMA = "consumer_defensive_promotion_input_v3"
CAPITAL_ALLOCATION_CONTEXT_SCHEMA = (
    "consumer_defensive_capital_allocation_context_v1"
)
DECISION_SCHEMA = "consumer_defensive_promotion_decision_v3"
MODEL_CONTRACT_SCHEMA = "consumer_defensive_production_model_contract_v3"
ACTIVATION_LOCK_SCHEMA = "consumer_defensive_activation_lock_v3"
ACTIVATION_REGISTRY_SCHEMA = "consumer_defensive_production_activation_registry_v3"
MODEL_FAMILY = "consumer_defensive"
CAPACITY_TEST_BASIS = (
    "full_consumer_defensive_sector_budget_per_cohort_conservative"
)

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
EVIDENCE_ROLES = frozenset({"design_evidence", "fresh_chronological"})
DEPLOYABLE_STATES = frozenset({"active_full"})
DECISION_STATES = frozenset(
    {"rollback", "benchmark_production", *DEPLOYABLE_STATES}
)

PERFORMANCE_METRICS = frozenset(
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
PATH_ROW_KEYS = frozenset(
    {
        "date",
        "strategy_net_return",
        "primary_benchmark_return",
        "xlp_return",
        "spy_return",
    }
)
OUTER_OOS_OBSERVATION_KEYS = frozenset(
    {
        "observation_id",
        "fold_id",
        "signal_date",
        "label_completion_date",
    }
)
SOURCE_LINEAGE_KEYS = frozenset(
    {
        "source_decision_sha256",
        "source_results_sha256",
        "input_panel_sha256",
        "fold_registry_sha256",
        "candidate_registry_sha256",
        "code_sha256",
        "benchmark_path_source_sha256",
    }
)
CAPITAL_ALLOCATION_CONTEXT_KEYS = frozenset(
    {
        "schema_version",
        "model_family",
        "asof_date",
        "account_aum_usd",
        "active_sector_count",
        "sector_max_fraction",
        "sector_max_notional_usd",
        "calibration_reference_notional_usd",
        "capacity_test_basis",
        "payload_sha256",
    }
)
MODEL_CONTRACT_KEYS = frozenset(
    {
        "schema_version",
        "model_family",
        "cohort",
        "champion_horizon_sessions",
        "selection_rule",
        "selected_candidate_id",
        "candidate_definition",
        "candidate_definition_sha256",
        "candidate_registry_sha256",
        "score_model_version",
        "scoring_contract_version",
        "payload_sha256",
    }
)
PROMOTION_INPUT_KEYS = frozenset(
    {
        "schema_version",
        "model_family",
        "asof_date",
        "framework_sha256",
        "evidence_role",
        "source_lineage",
        "capital_allocation_context",
        "safety_attestations",
        "cohorts",
        "payload_sha256",
    }
)
SHA256_LENGTH = 64


def canonical_sha256(payload: Mapping[str, Any]) -> str:
    """Hash a JSON mapping after excluding only its top-level self hash."""

    body = {key: value for key, value in payload.items() if key != "payload_sha256"}
    encoded = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def value_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
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


def _exact_mapping(
    value: Any, expected: frozenset[str], *, label: str
) -> dict[str, Any]:
    parsed = _mapping(value, label=label)
    if set(parsed) != expected:
        raise ValueError(f"{label} must contain exactly {sorted(expected)}")
    return parsed


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
    if (
        not isinstance(value, str)
        or len(value) != SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _iso_date(value: Any, *, label: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a canonical ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be a canonical ISO date")
    return parsed


def build_capital_allocation_context(
    *,
    asof_date: str,
    account_aum_usd: float,
    active_sector_count: int,
    sector_max_fraction: float,
    calibration_reference_notional_usd: float,
) -> dict[str, Any]:
    """Build a sealed, report-only capital-planning context.

    The context is deliberately separate from predictive evidence. It states
    the account and sector budget against which already-measured executable
    capacity is evaluated; it cannot add observations or change evidence role.
    """

    context: dict[str, Any] = {
        "schema_version": CAPITAL_ALLOCATION_CONTEXT_SCHEMA,
        "model_family": MODEL_FAMILY,
        "asof_date": _iso_date(
            asof_date, label="capital allocation context asof_date"
        ).isoformat(),
        "account_aum_usd": _finite(
            account_aum_usd, label="capital allocation context account_aum_usd"
        ),
        "active_sector_count": _integer(
            active_sector_count,
            label="capital allocation context active_sector_count",
            minimum=1,
        ),
        "sector_max_fraction": _finite(
            sector_max_fraction,
            label="capital allocation context sector_max_fraction",
        ),
        "sector_max_notional_usd": float(account_aum_usd)
        * float(sector_max_fraction),
        "calibration_reference_notional_usd": (
            _finite(
                calibration_reference_notional_usd,
                label=(
                    "capital allocation context "
                    "calibration_reference_notional_usd"
                ),
            )
        ),
        "capacity_test_basis": CAPACITY_TEST_BASIS,
    }
    context["payload_sha256"] = canonical_sha256(context)
    return validate_capital_allocation_context(context)


def validate_capital_allocation_context(
    payload: Mapping[str, Any],
    *,
    evidence_asof_date: str | date | None = None,
) -> dict[str, Any]:
    """Validate the exact capital context without treating it as alpha data."""

    context = _exact_mapping(
        payload,
        CAPITAL_ALLOCATION_CONTEXT_KEYS,
        label="capital allocation context",
    )
    if context["schema_version"] != CAPITAL_ALLOCATION_CONTEXT_SCHEMA:
        raise ValueError("unsupported Consumer Defensive capital allocation context")
    if context["model_family"] != MODEL_FAMILY:
        raise ValueError("capital allocation context has the wrong model family")
    context_asof = _iso_date(
        context["asof_date"], label="capital allocation context asof_date"
    )
    if evidence_asof_date is not None:
        evidence_asof = (
            evidence_asof_date
            if isinstance(evidence_asof_date, date)
            else _iso_date(evidence_asof_date, label="promotion evidence asof_date")
        )
        if context_asof < evidence_asof:
            raise ValueError(
                "capital allocation context cannot predate promotion evidence"
            )
    account_aum = _finite(
        context["account_aum_usd"],
        label="capital allocation context account_aum_usd",
    )
    if account_aum <= 0.0:
        raise ValueError("capital allocation context account AUM must be positive")
    sector_count = _integer(
        context["active_sector_count"],
        label="capital allocation context active_sector_count",
        minimum=1,
    )
    sector_fraction = _finite(
        context["sector_max_fraction"],
        label="capital allocation context sector_max_fraction",
    )
    if not 0.0 < sector_fraction <= 1.0 or not math.isclose(
        sector_fraction,
        1.0 / sector_count,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise ValueError(
            "capital allocation context sector fraction must equal the "
            "equal active-sector allocation"
        )
    sector_notional = _finite(
        context["sector_max_notional_usd"],
        label="capital allocation context sector_max_notional_usd",
    )
    if sector_notional <= 0.0 or not math.isclose(
        sector_notional,
        account_aum * sector_fraction,
        rel_tol=0.0,
        abs_tol=1e-8,
    ):
        raise ValueError("capital allocation context sector notional does not reconcile")
    reference_notional = _finite(
        context["calibration_reference_notional_usd"],
        label="capital allocation context calibration_reference_notional_usd",
    )
    if reference_notional <= 0.0:
        raise ValueError(
            "capital allocation context reference notional must be positive"
        )
    if context["capacity_test_basis"] != CAPACITY_TEST_BASIS:
        raise ValueError("capital allocation context capacity-test basis changed")
    if canonical_sha256(context) != _sha(
        context["payload_sha256"], label="capital allocation context hash"
    ):
        raise ValueError("capital allocation context self-hash mismatch")
    return context


def _weights_sum_to_one(values: Mapping[str, Any], *, label: str) -> None:
    parsed = [_finite(value, label=f"{label}.{name}") for name, value in values.items()]
    if any(value < 0.0 for value in parsed) or not math.isclose(
        sum(parsed), 1.0, rel_tol=0.0, abs_tol=1e-12
    ):
        raise ValueError(f"{label} must be nonnegative and sum to one")


def load_framework(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    return validate_framework(_mapping(payload, label="promotion framework v3"))


def validate_framework(payload: Mapping[str, Any]) -> dict[str, Any]:
    framework = _exact_mapping(
        payload,
        frozenset(
            {
                "schema_version",
                "model_family",
                "framework_version",
                "status",
                "ownership",
                "cohorts",
                "layer_1_data_and_safety",
                "layer_2_economic_performance",
                "layer_3_confidence_adjustment",
                "layer_4_controlled_deployment",
                "production_model_contract",
            }
        ),
        label="promotion framework v3",
    )
    if framework["schema_version"] != FRAMEWORK_SCHEMA:
        raise ValueError("unsupported Consumer Defensive promotion framework v3")
    if framework["model_family"] != MODEL_FAMILY:
        raise ValueError("promotion framework has the wrong model family")
    if framework["framework_version"] != FRAMEWORK_VERSION:
        raise ValueError("promotion framework v3 version changed")
    if framework["status"] != "implemented_standard_allocation_requires_pinned_registry":
        raise ValueError("promotion framework v3 status changed")

    ownership = _exact_mapping(
        framework["ownership"],
        frozenset(
            {
                "sector_owner",
                "cross_sector_code_imports_allowed",
                "shared_service_contract_path",
                "shared_service_contract_sha256",
            }
        ),
        label="ownership",
    )
    if ownership["sector_owner"] != MODEL_FAMILY:
        raise ValueError("Consumer Defensive must own its promotion engine")
    if ownership["cross_sector_code_imports_allowed"] is not False:
        raise ValueError("cross-sector strategy imports must remain disabled")
    if ownership["shared_service_contract_path"] != (
        "consumer_defensive/data/consumer_defensive_shared_service_contract_v1.yaml"
    ):
        raise ValueError("shared-service contract path changed")
    _sha(ownership["shared_service_contract_sha256"], label="ownership contract hash")

    cohorts = _mapping(framework["cohorts"], label="cohorts")
    if set(cohorts) != REQUIRED_COHORTS:
        raise ValueError("framework v3 must define exactly four Consumer cohorts")
    for cohort, raw in cohorts.items():
        definition = _exact_mapping(
            raw,
            frozenset({"calibrated_independently"}),
            label=f"cohorts.{cohort}",
        )
        if definition["calibrated_independently"] is not True:
            raise ValueError(f"{cohort} must be calibrated independently")

    layer1 = _exact_mapping(
        framework["layer_1_data_and_safety"],
        frozenset({"required_attestations", "hard_limits"}),
        label="layer_1_data_and_safety",
    )
    attestations = layer1["required_attestations"]
    if (
        not isinstance(attestations, list)
        or not attestations
        or len(attestations) != len(set(attestations))
        or any(not isinstance(value, str) or not value for value in attestations)
    ):
        raise ValueError("required safety attestations must be unique names")
    hard = _exact_mapping(
        layer1["hard_limits"],
        frozenset(
            {
                "require_all_horizons",
                "minimum_daily_path_observations",
                "minimum_paired_observations",
                "maximum_drawdown",
                "minimum_expected_shortfall_95",
                "maximum_turnover",
                "maximum_average_transaction_cost",
                "minimum_liquidity_capacity_ratio",
                "maximum_winner_concentration_hhi",
                "maximum_single_name_weight",
            }
        ),
        label="layer_1_data_and_safety.hard_limits",
    )
    if hard["require_all_horizons"] is not True:
        raise ValueError("all three horizons are required")
    _integer(
        hard["minimum_daily_path_observations"],
        label="minimum_daily_path_observations",
        minimum=2,
    )
    _integer(
        hard["minimum_paired_observations"],
        label="minimum_paired_observations",
        minimum=2,
    )
    for name in (
        "maximum_drawdown",
        "maximum_average_transaction_cost",
        "maximum_winner_concentration_hhi",
        "maximum_single_name_weight",
    ):
        value = _finite(hard[name], label=f"hard_limits.{name}")
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"hard_limits.{name} must be in [0, 1]")
    if not -1.0 < _finite(
        hard["minimum_expected_shortfall_95"], label="minimum_expected_shortfall_95"
    ) <= 0.0:
        raise ValueError("minimum_expected_shortfall_95 must be in (-1, 0]")
    if _finite(hard["maximum_turnover"], label="maximum_turnover") <= 0.0:
        raise ValueError("maximum_turnover must be positive")
    if _finite(
        hard["minimum_liquidity_capacity_ratio"],
        label="minimum_liquidity_capacity_ratio",
    ) <= 0.0:
        raise ValueError("minimum liquidity capacity must be positive")

    layer2 = _exact_mapping(
        framework["layer_2_economic_performance"],
        frozenset(
            {
                "periods_per_year",
                "horizon_weights",
                "positive_correlation_penalty",
                "neutral_score",
                "blocks",
                "metric_anchors",
            }
        ),
        label="layer_2_economic_performance",
    )
    _integer(layer2["periods_per_year"], label="periods_per_year", minimum=1)
    horizon_weights = _mapping(layer2["horizon_weights"], label="horizon_weights")
    if set(horizon_weights) != REQUIRED_HORIZON_KEYS:
        raise ValueError("horizon weights must cover exact 21/63/126 horizons")
    _weights_sum_to_one(horizon_weights, label="horizon_weights")
    if _finite(
        layer2["positive_correlation_penalty"], label="positive_correlation_penalty"
    ) < 0.0:
        raise ValueError("positive correlation penalty cannot be negative")
    neutral = _finite(layer2["neutral_score"], label="neutral_score")
    if not 0.0 <= neutral <= 100.0:
        raise ValueError("neutral score must be in [0, 100]")
    blocks = _mapping(layer2["blocks"], label="economic blocks")
    if set(blocks) != {
        "benchmark_relative_return",
        "absolute_profitability",
        "risk_efficiency",
        "deployability",
    }:
        raise ValueError("economic block census changed")
    block_weights: dict[str, float] = {}
    referenced_metrics: set[str] = set()
    for block_name, raw in blocks.items():
        block = _exact_mapping(
            raw, frozenset({"weight", "metrics"}), label=f"blocks.{block_name}"
        )
        block_weights[block_name] = _finite(
            block["weight"], label=f"blocks.{block_name}.weight"
        )
        metric_weights = _mapping(
            block["metrics"], label=f"blocks.{block_name}.metrics"
        )
        _weights_sum_to_one(metric_weights, label=f"blocks.{block_name}.metrics")
        if referenced_metrics.intersection(metric_weights):
            raise ValueError("economic metrics cannot be counted in multiple blocks")
        referenced_metrics.update(metric_weights)
    _weights_sum_to_one(block_weights, label="economic block weights")
    anchors = _mapping(layer2["metric_anchors"], label="metric_anchors")
    if set(anchors) != referenced_metrics:
        raise ValueError("metric anchors must match the exact economic metric census")
    for metric, raw in anchors.items():
        anchor = _exact_mapping(
            raw,
            frozenset({"direction", "bad", "neutral", "good"}),
            label=f"metric_anchors.{metric}",
        )
        if anchor["direction"] not in {"higher", "lower"}:
            raise ValueError(f"{metric} direction must be higher or lower")
        bad = _finite(anchor["bad"], label=f"{metric}.bad")
        midpoint = _finite(anchor["neutral"], label=f"{metric}.neutral")
        good = _finite(anchor["good"], label=f"{metric}.good")
        ordered = bad < midpoint < good if anchor["direction"] == "higher" else bad > midpoint > good
        if not ordered:
            raise ValueError(f"{metric} anchors are not monotone")

    layer3 = _exact_mapping(
        framework["layer_3_confidence_adjustment"],
        frozenset(
            {
                "floor",
                "deflated_sharpe_weight",
                "inverse_pbo_weight",
                "score_shrinkage_target",
                "do_not_reapply_to_cap",
            }
        ),
        label="layer_3_confidence_adjustment",
    )
    confidence_parts = {
        "floor": _finite(layer3["floor"], label="confidence.floor"),
        "deflated_sharpe_weight": _finite(
            layer3["deflated_sharpe_weight"], label="confidence.deflated_sharpe_weight"
        ),
        "inverse_pbo_weight": _finite(
            layer3["inverse_pbo_weight"], label="confidence.inverse_pbo_weight"
        ),
    }
    _weights_sum_to_one(confidence_parts, label="confidence components")
    if layer3["do_not_reapply_to_cap"] is not True:
        raise ValueError("confidence cannot be double-counted in the capital cap")
    target = _finite(layer3["score_shrinkage_target"], label="score_shrinkage_target")
    if not 0.0 <= target <= 100.0:
        raise ValueError("score shrinkage target must be in [0, 100]")

    layer4 = _exact_mapping(
        framework["layer_4_controlled_deployment"],
        frozenset(
            {
                "tier_order",
                "tiers",
                "modifiers",
                "standard_allocation_policy",
                "transition_policy",
            }
        ),
        label="layer_4_controlled_deployment",
    )
    tier_order = layer4["tier_order"]
    if tier_order != ["benchmark_production", "active_full"]:
        raise ValueError("deployment tier order changed")
    tiers = _mapping(layer4["tiers"], label="deployment tiers")
    if set(tiers) != DECISION_STATES:
        raise ValueError("deployment tier census changed")
    for state in ["rollback", *tier_order]:
        tier = _exact_mapping(
            tiers[state],
            frozenset({"minimum_score", "deployment_fraction", "deployable"}),
            label=f"tiers.{state}",
        )
        minimum = _finite(tier["minimum_score"], label=f"tiers.{state}.minimum_score")
        fraction = _finite(
            tier["deployment_fraction"], label=f"tiers.{state}.deployment_fraction"
        )
        if not 0.0 <= minimum <= 100.0 or not 0.0 <= fraction <= 1.0:
            raise ValueError(f"tiers.{state} has invalid score or fraction")
        if tier["deployable"] is not (state in DEPLOYABLE_STATES):
            raise ValueError(f"tiers.{state}.deployable is inconsistent")
    if tiers["rollback"] != {
        "minimum_score": 0.0,
        "deployment_fraction": 0.0,
        "deployable": False,
    } or tiers["benchmark_production"] != {
        "minimum_score": 0.0,
        "deployment_fraction": 0.0,
        "deployable": False,
    }:
        raise ValueError("nondeployable tier policy changed")
    if tiers["active_full"] != {
        "minimum_score": 60.0,
        "deployment_fraction": 1.0,
        "deployable": True,
    }:
        raise ValueError("standard production tier policy changed")
    modifiers = _exact_mapping(
        layer4["modifiers"],
        frozenset(
            {
                "capacity_full_credit_ratio",
                "apply_cohort_level_diversification_haircut",
            }
        ),
        label="deployment modifiers",
    )
    if not math.isclose(
        _finite(
            modifiers["capacity_full_credit_ratio"],
            label="capacity_full_credit_ratio",
        ),
        1.0,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise ValueError("capacity must receive full credit at the safety threshold")
    if modifiers["apply_cohort_level_diversification_haircut"] is not False:
        raise ValueError("cohort-level diversification haircuts are disabled")
    allocation = _exact_mapping(
        layer4["standard_allocation_policy"],
        frozenset(
            {
                "allocation_source",
                "allocation_method",
                "failed_or_ineligible_slot",
                "minimum_confidence_adjusted_score",
            }
        ),
        label="standard allocation policy",
    )
    if allocation != {
        "allocation_source": "portfolio_owned_consumer_defensive_sector_budget",
        "allocation_method": "equal_configured_cohort_slots",
        "failed_or_ineligible_slot": "cash",
        "minimum_confidence_adjusted_score": 60.0,
    }:
        raise ValueError("standard allocation policy changed")
    transition = _exact_mapping(
        layer4["transition_policy"],
        frozenset(
            {
                "maximum_one_upward_tier_per_decision",
                "design_evidence_maximum_state",
                "fresh_chronological_evidence_required_for_advancement",
                "require_new_input_panel",
                "minimum_new_paired_observations_per_horizon",
                "minimum_dwell_days",
                "maximum_activation_age_days",
                "material_model_change_resets_to",
                "hard_failure_state",
            }
        ),
        label="transition policy",
    )
    if transition["maximum_one_upward_tier_per_decision"] is not True:
        raise ValueError("one-tier upward transitions are mandatory")
    if transition["fresh_chronological_evidence_required_for_advancement"] is not True:
        raise ValueError("fresh evidence must govern advancement")
    if transition["require_new_input_panel"] is not True:
        raise ValueError("advancement must require a new input panel")
    if transition["design_evidence_maximum_state"] != "active_full":
        raise ValueError("qualifying design evidence must authorize standard production")
    if transition["material_model_change_resets_to"] != "benchmark_production":
        raise ValueError("material changes must reset to benchmark-only monitoring")
    if transition["hard_failure_state"] != "rollback":
        raise ValueError("active hard failures must roll back")
    _integer(
        transition["minimum_new_paired_observations_per_horizon"],
        label="minimum new observations",
        minimum=1,
    )
    dwell = _mapping(transition["minimum_dwell_days"], label="minimum_dwell_days")
    if set(dwell) != {"active_full"}:
        raise ValueError("dwell policy has the wrong state census")
    for state, value in dwell.items():
        _integer(value, label=f"minimum_dwell_days.{state}", minimum=1)
    if _integer(
        transition["maximum_activation_age_days"],
        label="maximum_activation_age_days",
        minimum=1,
    ) != 63:
        raise ValueError("v3 activation locks must expire after 63 calendar days")

    production = _exact_mapping(
        framework["production_model_contract"],
        frozenset(
            {
                "champion_horizon_sessions",
                "selection_rule",
                "require_exact_candidate_definition",
                "calibration_remains_report_only",
                "activation_requires_pinned_registry",
            }
        ),
        label="production_model_contract",
    )
    if production != {
        "champion_horizon_sessions": 63,
        "selection_rule": "latest_admissible_outer_fold_winner",
        "require_exact_candidate_definition": True,
        "calibration_remains_report_only": True,
        "activation_requires_pinned_registry": True,
    }:
        raise ValueError("production model-contract policy changed")
    return framework


def framework_sha256(framework: Mapping[str, Any]) -> str:
    return canonical_sha256(validate_framework(framework))


def build_production_model_contract(
    *,
    cohort: str,
    selected_candidate_id: str,
    candidate_definition: Mapping[str, Any],
    candidate_registry_sha256: str,
    score_model_version: str,
    scoring_contract_version: str,
) -> dict[str, Any]:
    if cohort not in REQUIRED_COHORTS:
        raise ValueError(f"unsupported Consumer cohort: {cohort}")
    definition = _mapping(candidate_definition, label="candidate_definition")
    if not selected_candidate_id or not isinstance(selected_candidate_id, str):
        raise ValueError("selected_candidate_id is required")
    selected_candidate_id = selected_candidate_id.strip()
    if definition.get("candidate_id") != selected_candidate_id:
        raise ValueError("candidate definition is not bound to selected_candidate_id")
    for label, value in {
        "score_model_version": score_model_version,
        "scoring_contract_version": scoring_contract_version,
    }.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} is required")
    contract: dict[str, Any] = {
        "schema_version": MODEL_CONTRACT_SCHEMA,
        "model_family": MODEL_FAMILY,
        "cohort": cohort,
        "champion_horizon_sessions": 63,
        "selection_rule": "latest_admissible_outer_fold_winner",
        "selected_candidate_id": selected_candidate_id,
        "candidate_definition": definition,
        "candidate_definition_sha256": value_sha256(definition),
        "candidate_registry_sha256": _sha(
            candidate_registry_sha256, label="candidate_registry_sha256"
        ),
        "score_model_version": score_model_version.strip(),
        "scoring_contract_version": scoring_contract_version.strip(),
    }
    contract["payload_sha256"] = canonical_sha256(contract)
    return validate_production_model_contract(contract, cohort=cohort)


def validate_production_model_contract(
    payload: Mapping[str, Any], *, cohort: str
) -> dict[str, Any]:
    contract = _exact_mapping(payload, MODEL_CONTRACT_KEYS, label=f"{cohort} model contract")
    if contract["schema_version"] != MODEL_CONTRACT_SCHEMA:
        raise ValueError("unsupported production model contract")
    if contract["model_family"] != MODEL_FAMILY or contract["cohort"] != cohort:
        raise ValueError("production model contract scope mismatch")
    if contract["champion_horizon_sessions"] != 63:
        raise ValueError("production champion must be selected on the 63-session horizon")
    if contract["selection_rule"] != "latest_admissible_outer_fold_winner":
        raise ValueError("production champion selection rule changed")
    if not isinstance(contract["selected_candidate_id"], str) or not contract[
        "selected_candidate_id"
    ].strip():
        raise ValueError("production model contract candidate is blank")
    definition = _mapping(contract["candidate_definition"], label="candidate_definition")
    if definition.get("candidate_id") != contract["selected_candidate_id"]:
        raise ValueError("candidate definition is not bound to the selected candidate")
    if value_sha256(definition) != _sha(
        contract["candidate_definition_sha256"], label="candidate definition hash"
    ):
        raise ValueError("candidate definition hash mismatch")
    _sha(contract["candidate_registry_sha256"], label="candidate registry hash")
    for field in ("score_model_version", "scoring_contract_version"):
        if not isinstance(contract[field], str) or not contract[field].strip():
            raise ValueError(f"production model contract {field} is blank")
    if canonical_sha256(contract) != _sha(contract["payload_sha256"], label="model contract hash"):
        raise ValueError("production model contract self-hash mismatch")
    return contract


def _validate_performance(payload: Mapping[str, Any], *, label: str) -> dict[str, float | int]:
    raw = _exact_mapping(payload, PERFORMANCE_METRICS, label=label)
    parsed: dict[str, float | int] = {
        name: _finite(value, label=f"{label}.{name}") for name, value in raw.items()
    }
    for name in ("deflated_sharpe_ratio", "probability_of_backtest_overfitting"):
        if not 0.0 <= float(parsed[name]) <= 1.0:
            raise ValueError(f"{label}.{name} must be in [0, 1]")
    for name in ("maximum_drawdown", "winner_concentration_hhi", "maximum_single_name_weight"):
        if not 0.0 <= float(parsed[name]) <= 1.0:
            raise ValueError(f"{label}.{name} must be in [0, 1]")
    for name in ("absolute_profit_factor", "relative_profit_factor", "robust_profit_factor"):
        if not 0.0 <= float(parsed[name]) <= 1_000_000.0:
            raise ValueError(f"{label}.{name} is outside its supported range")
    if float(parsed["expected_shortfall_95"]) <= -1.0:
        raise ValueError(f"{label}.expected_shortfall_95 must exceed -1")
    if float(parsed["turnover"]) < 0.0:
        raise ValueError(f"{label}.turnover cannot be negative")
    if not 0.0 <= float(parsed["average_transaction_cost"]) < 1.0:
        raise ValueError(f"{label}.average_transaction_cost must be in [0, 1)")
    if float(parsed["liquidity_capacity_ratio"]) <= 0.0:
        raise ValueError(f"{label}.liquidity_capacity_ratio must be positive")
    for name in ("paired_observation_count", "positive_return_count", "negative_return_count"):
        parsed[name] = _integer(raw[name], label=f"{label}.{name}")
    if int(parsed["positive_return_count"]) + int(parsed["negative_return_count"]) > int(
        parsed["paired_observation_count"]
    ):
        raise ValueError(f"{label}: signed counts exceed paired observations")
    return parsed


def _validate_path(
    payload: Any, *, label: str, decision_asof: date
) -> list[dict[str, float | str]]:
    if isinstance(payload, (str, bytes)) or not isinstance(payload, Sequence) or not payload:
        raise ValueError(f"{label} must be a nonempty sequence")
    rows: list[dict[str, float | str]] = []
    prior_date: date | None = None
    for position, raw in enumerate(payload):
        row = _exact_mapping(raw, PATH_ROW_KEYS, label=f"{label}[{position}]")
        current_date = _iso_date(row["date"], label=f"{label}[{position}].date")
        if current_date > decision_asof:
            raise ValueError(f"{label} contains a future return")
        if prior_date is not None and current_date <= prior_date:
            raise ValueError(f"{label} dates must be strictly increasing and unique")
        parsed: dict[str, float | str] = {"date": current_date.isoformat()}
        for field in PATH_ROW_KEYS - {"date"}:
            value = _finite(row[field], label=f"{label}[{position}].{field}")
            if value <= -1.0:
                raise ValueError(f"{label}[{position}].{field} must exceed -100%")
            parsed[field] = value
        rows.append(parsed)
        prior_date = current_date
    return rows


def _validate_outer_oos_observations(
    payload: Any, *, label: str, decision_asof: date
) -> list[dict[str, str]]:
    if isinstance(payload, (str, bytes)) or not isinstance(payload, Sequence) or not payload:
        raise ValueError(f"{label} must be a nonempty sequence")
    rows: list[dict[str, str]] = []
    identities: set[str] = set()
    prior_key: tuple[date, str] | None = None
    for position, raw in enumerate(payload):
        row = _exact_mapping(
            raw,
            OUTER_OOS_OBSERVATION_KEYS,
            label=f"{label}[{position}]",
        )
        observation_id = str(row["observation_id"])
        fold_id = str(row["fold_id"])
        if (
            not observation_id
            or observation_id != observation_id.strip()
            or not fold_id
            or fold_id != fold_id.strip()
        ):
            raise ValueError(f"{label}[{position}] has a blank/noncanonical identity")
        if observation_id in identities:
            raise ValueError(f"{label} observation identities must be unique")
        identities.add(observation_id)
        signal = _iso_date(row["signal_date"], label=f"{label}[{position}].signal_date")
        completion = _iso_date(
            row["label_completion_date"],
            label=f"{label}[{position}].label_completion_date",
        )
        if not signal < completion <= decision_asof:
            raise ValueError(f"{label}[{position}] violates outer-OOS chronology")
        current_key = (signal, observation_id)
        if prior_key is not None and current_key <= prior_key:
            raise ValueError(f"{label} must be ordered by signal date and identity")
        prior_key = current_key
        rows.append(
            {
                "observation_id": observation_id,
                "fold_id": fold_id,
                "signal_date": signal.isoformat(),
                "label_completion_date": completion.isoformat(),
            }
        )
    return rows


def seal_promotion_input(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result.pop("payload_sha256", None)
    result["payload_sha256"] = canonical_sha256(result)
    return result


def validate_promotion_input(
    payload: Mapping[str, Any], *, framework: Mapping[str, Any]
) -> dict[str, Any]:
    validated_framework = validate_framework(framework)
    item = _exact_mapping(payload, PROMOTION_INPUT_KEYS, label="promotion input v3")
    if item["schema_version"] != PROMOTION_INPUT_SCHEMA or item["model_family"] != MODEL_FAMILY:
        raise ValueError("unsupported promotion input v3")
    decision_asof = _iso_date(item["asof_date"], label="promotion input asof_date")
    if item["framework_sha256"] != framework_sha256(validated_framework):
        raise ValueError("promotion input is bound to a different framework")
    if item["evidence_role"] not in EVIDENCE_ROLES:
        raise ValueError("promotion input evidence_role is unsupported")
    capital_context = validate_capital_allocation_context(
        item["capital_allocation_context"],
        evidence_asof_date=decision_asof,
    )
    lineage = _exact_mapping(item["source_lineage"], SOURCE_LINEAGE_KEYS, label="source_lineage")
    for field, value in lineage.items():
        _sha(value, label=f"source_lineage.{field}")
    required_attestations = validated_framework["layer_1_data_and_safety"][
        "required_attestations"
    ]
    attestations = _mapping(item["safety_attestations"], label="safety_attestations")
    if set(attestations) != set(required_attestations):
        raise ValueError("safety attestation census changed")
    if any(type(value) is not bool for value in attestations.values()):
        raise ValueError("safety attestations must be booleans")
    cohorts = _mapping(item["cohorts"], label="promotion input cohorts")
    if set(cohorts) != REQUIRED_COHORTS:
        raise ValueError("promotion input must cover exactly four Consumer cohorts")
    normalized_cohorts: dict[str, Any] = {}
    for cohort, raw in cohorts.items():
        cohort_item = _exact_mapping(
            raw,
            frozenset({"production_model_contract", "horizons"}),
            label=f"cohorts.{cohort}",
        )
        contract = validate_production_model_contract(
            cohort_item["production_model_contract"], cohort=cohort
        )
        if contract["candidate_registry_sha256"] != lineage["candidate_registry_sha256"]:
            raise ValueError(f"{cohort}: model contract has the wrong candidate registry")
        horizons = _mapping(cohort_item["horizons"], label=f"{cohort}.horizons")
        if set(horizons) != REQUIRED_HORIZON_KEYS:
            raise ValueError(f"{cohort}: exact 21/63/126 horizons are required")
        normalized_horizons: dict[str, Any] = {}
        for horizon_key in sorted(horizons, key=int):
            horizon = _exact_mapping(
                horizons[horizon_key],
                frozenset(
                    {"performance", "daily_path", "outer_oos_observations"}
                ),
                label=f"{cohort}.horizons.{horizon_key}",
            )
            performance = _validate_performance(
                horizon["performance"], label=f"{cohort}.{horizon_key}.performance"
            )
            outer = _validate_outer_oos_observations(
                horizon["outer_oos_observations"],
                label=f"{cohort}.{horizon_key}.outer_oos_observations",
                decision_asof=decision_asof,
            )
            if int(performance["paired_observation_count"]) != len(outer):
                raise ValueError(
                    f"{cohort}/{horizon_key}: paired count does not match outer-OOS identities"
                )
            normalized_horizons[horizon_key] = {
                "performance": performance,
                "daily_path": _validate_path(
                    horizon["daily_path"],
                    label=f"{cohort}.{horizon_key}.daily_path",
                    decision_asof=decision_asof,
                ),
                "outer_oos_observations": outer,
            }
        normalized_cohorts[cohort] = {
            "production_model_contract": contract,
            "horizons": normalized_horizons,
        }
    if canonical_sha256(item) != _sha(item["payload_sha256"], label="promotion input hash"):
        raise ValueError("promotion input self-hash mismatch")
    return {
        **item,
        "source_lineage": lineage,
        "capital_allocation_context": capital_context,
        "safety_attestations": attestations,
        "cohorts": normalized_cohorts,
    }


def _wealth(returns: Sequence[float]) -> float:
    wealth = 1.0
    for value in returns:
        if value <= -1.0 or not math.isfinite(value):
            raise ValueError("wealth-path returns must be finite and exceed -100%")
        wealth *= 1.0 + value
    return wealth


def _maximum_drawdown(returns: Sequence[float]) -> float:
    wealth = peak = 1.0
    worst = 0.0
    for value in returns:
        wealth *= 1.0 + value
        peak = max(peak, wealth)
        worst = max(worst, (peak - wealth) / peak)
    return worst


def _profit_factor(returns: Sequence[float]) -> float:
    gains = sum(value for value in returns if value > 0.0)
    losses = -sum(value for value in returns if value < 0.0)
    if losses <= 0.0:
        return 1_000_000.0 if gains > 0.0 else 0.0
    return min(1_000_000.0, gains / losses)


def _expected_shortfall(returns: Sequence[float], *, tail_probability: float = 0.05) -> float:
    ordered = sorted(float(value) for value in returns)
    if not ordered:
        raise ValueError("expected shortfall requires observations")
    if not 0.0 < tail_probability <= 0.5:
        raise ValueError("tail probability must be in (0, 0.5]")
    count = max(1, math.ceil(len(ordered) * tail_probability))
    return statistics.fmean(ordered[:count])


def compute_path_metrics(
    daily_path: Sequence[Mapping[str, Any]], *, periods_per_year: int = 252
) -> dict[str, float | int | str]:
    rows = list(daily_path)
    if not rows:
        raise ValueError("path metrics require observations")
    strategy = [float(row["strategy_net_return"]) for row in rows]
    benchmark = [float(row["primary_benchmark_return"]) for row in rows]
    xlp = [float(row["xlp_return"]) for row in rows]
    spy = [float(row["spy_return"]) for row in rows]
    relative = [(1.0 + left) / (1.0 + right) - 1.0 for left, right in zip(strategy, benchmark)]
    excess = [left - right for left, right in zip(strategy, benchmark)]
    strategy_wealth = _wealth(strategy)
    benchmark_wealth = _wealth(benchmark)
    xlp_wealth = _wealth(xlp)
    spy_wealth = _wealth(spy)
    years = len(rows) / periods_per_year
    cagr = strategy_wealth ** (1.0 / years) - 1.0
    benchmark_cagr = benchmark_wealth ** (1.0 / years) - 1.0
    drawdown = _maximum_drawdown(strategy)
    benchmark_drawdown = _maximum_drawdown(benchmark)
    relative_drawdown = _maximum_drawdown(relative)
    if drawdown <= 1e-12:
        calmar = 100.0 if cagr > 0.0 else (-100.0 if cagr < 0.0 else 0.0)
    else:
        calmar = max(-100.0, min(100.0, cagr / drawdown))
    if len(excess) < 2 or statistics.stdev(excess) <= 1e-15:
        tracking_error = 0.0
        information_ratio = 0.0
    else:
        deviation = statistics.stdev(excess)
        tracking_error = deviation * math.sqrt(periods_per_year)
        information_ratio = statistics.fmean(excess) / deviation * math.sqrt(periods_per_year)
    return {
        "observation_count": len(rows),
        "start_date": str(rows[0]["date"]),
        "end_date": str(rows[-1]["date"]),
        "cumulative_net_return": strategy_wealth - 1.0,
        "benchmark_cumulative_return": benchmark_wealth - 1.0,
        "xlp_cumulative_return": xlp_wealth - 1.0,
        "spy_cumulative_return": spy_wealth - 1.0,
        "relative_wealth": strategy_wealth / benchmark_wealth,
        "cagr": cagr,
        "benchmark_cagr": benchmark_cagr,
        "excess_cagr": cagr - benchmark_cagr,
        "maximum_drawdown": drawdown,
        "benchmark_maximum_drawdown": benchmark_drawdown,
        "relative_maximum_drawdown": relative_drawdown,
        "calmar_ratio": calmar,
        "tracking_error": tracking_error,
        "information_ratio": information_ratio,
        "absolute_profit_factor": _profit_factor(strategy),
        "relative_profit_factor": _profit_factor(relative),
        "active_return_profit_factor": _profit_factor(excess),
        "expected_shortfall_95": _expected_shortfall(strategy),
        "net_pnl_per_starting_dollar": strategy_wealth - 1.0,
    }


def score_metric(value: float, anchor: Mapping[str, Any]) -> float:
    parsed = _finite(value, label="metric value")
    direction = str(anchor["direction"])
    bad = _finite(anchor["bad"], label="anchor.bad")
    neutral = _finite(anchor["neutral"], label="anchor.neutral")
    good = _finite(anchor["good"], label="anchor.good")
    if direction == "lower":
        parsed, bad, neutral, good = -parsed, -bad, -neutral, -good
    elif direction != "higher":
        raise ValueError("metric direction must be higher or lower")
    if not bad < neutral < good:
        raise ValueError("metric anchors must be strictly monotone")
    if parsed <= bad:
        return 0.0
    if parsed >= good:
        return 100.0
    if parsed <= neutral:
        return 50.0 * (parsed - bad) / (neutral - bad)
    return 50.0 + 50.0 * (parsed - neutral) / (good - neutral)


def _horizon_economic_score(
    performance: Mapping[str, Any],
    path_metrics: Mapping[str, Any],
    *,
    framework: Mapping[str, Any],
) -> tuple[dict[str, float], dict[str, float], float]:
    layer = framework["layer_2_economic_performance"]
    values = {**performance, **path_metrics}
    anchors = layer["metric_anchors"]
    metric_scores = {
        metric: score_metric(float(values[metric]), anchors[metric]) for metric in anchors
    }
    block_scores: dict[str, float] = {}
    for block_name, block in layer["blocks"].items():
        block_scores[block_name] = sum(
            float(weight) * metric_scores[metric]
            for metric, weight in block["metrics"].items()
        )
    total = sum(
        float(block["weight"]) * block_scores[block_name]
        for block_name, block in layer["blocks"].items()
    )
    return metric_scores, block_scores, max(0.0, min(100.0, total))


def _correlation(left: Mapping[str, float], right: Mapping[str, float]) -> float:
    common = sorted(set(left).intersection(right))
    if len(common) < 2:
        return 0.0
    a = [left[key] for key in common]
    b = [right[key] for key in common]
    if statistics.stdev(a) <= 1e-15 or statistics.stdev(b) <= 1e-15:
        return 0.0
    return max(-1.0, min(1.0, statistics.correlation(a, b)))


def effective_horizon_weights(
    paths_by_horizon: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    nominal_weights: Mapping[str, Any],
    positive_correlation_penalty: float,
) -> tuple[dict[str, float], dict[str, float]]:
    if set(paths_by_horizon) != REQUIRED_HORIZON_KEYS:
        raise ValueError("effective horizon weights require exact 21/63/126 paths")
    nominal = {key: float(nominal_weights[key]) for key in REQUIRED_HORIZON_KEYS}
    excess_by_horizon = {
        key: {
            str(row["date"]): float(row["strategy_net_return"])
            - float(row["primary_benchmark_return"])
            for row in paths_by_horizon[key]
        }
        for key in REQUIRED_HORIZON_KEYS
    }
    correlations: dict[str, float] = {}
    keys = sorted(REQUIRED_HORIZON_KEYS, key=int)
    for left_index, left in enumerate(keys):
        for right in keys[left_index + 1 :]:
            correlations[f"{left}:{right}"] = _correlation(
                excess_by_horizon[left], excess_by_horizon[right]
            )
    raw: dict[str, float] = {}
    for horizon in keys:
        redundancy = 0.0
        for other in keys:
            if other == horizon:
                continue
            pair = f"{min(int(horizon), int(other))}:{max(int(horizon), int(other))}"
            redundancy += nominal[other] * max(0.0, correlations[pair])
        raw[horizon] = nominal[horizon] / (
            1.0 + positive_correlation_penalty * redundancy
        )
    total = sum(raw.values())
    return ({key: raw[key] / total for key in keys}, correlations)


def confidence_multiplier(
    *, deflated_sharpe_ratio: float, probability_of_backtest_overfitting: float, framework: Mapping[str, Any]
) -> float:
    layer = framework["layer_3_confidence_adjustment"]
    dsr = _finite(deflated_sharpe_ratio, label="deflated_sharpe_ratio")
    pbo = _finite(
        probability_of_backtest_overfitting,
        label="probability_of_backtest_overfitting",
    )
    if not 0.0 <= dsr <= 1.0 or not 0.0 <= pbo <= 1.0:
        raise ValueError("DSR and PBO must be in [0, 1]")
    value = (
        float(layer["floor"])
        + float(layer["deflated_sharpe_weight"]) * dsr
        + float(layer["inverse_pbo_weight"]) * (1.0 - pbo)
    )
    return max(0.0, min(1.0, value))


def allocation_adjusted_capacity_evidence(
    performance_by_horizon: Mapping[str, Mapping[str, Any]],
    *,
    capital_allocation_context: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Rebase sealed capacity evidence to the full Consumer sector budget.

    Calibration reports a linear portfolio-capacity ratio against its sealed
    reference notional. Multiplying those values recovers executable dollars.
    Each cohort is then conservatively tested as though it had to absorb the
    entire Consumer Defensive sector budget, not merely its smaller cohort cap.
    Raw performance evidence is copied and never mutated.
    """

    if set(performance_by_horizon) != REQUIRED_HORIZON_KEYS:
        raise ValueError("capacity evidence requires exact 21/63/126 horizons")
    context = validate_capital_allocation_context(capital_allocation_context)
    reference_notional = float(context["calibration_reference_notional_usd"])
    sector_notional = float(context["sector_max_notional_usd"])
    adjusted: dict[str, dict[str, Any]] = {}
    audit: dict[str, dict[str, Any]] = {}
    for horizon in sorted(REQUIRED_HORIZON_KEYS, key=int):
        raw_performance = dict(performance_by_horizon[horizon])
        raw_ratio = _finite(
            raw_performance.get("liquidity_capacity_ratio"),
            label=f"{horizon}.raw_liquidity_capacity_ratio",
        )
        if raw_ratio <= 0.0:
            raise ValueError(f"{horizon}.raw_liquidity_capacity_ratio must be positive")
        executable_capacity = raw_ratio * reference_notional
        adjusted_ratio = executable_capacity / sector_notional
        adjusted[horizon] = {
            **raw_performance,
            "liquidity_capacity_ratio": adjusted_ratio,
        }
        audit[horizon] = {
            "raw_liquidity_capacity_ratio": raw_ratio,
            "calibration_reference_notional_usd": reference_notional,
            "executable_capacity_usd": executable_capacity,
            "consumer_defensive_sector_max_notional_usd": sector_notional,
            "allocation_adjusted_liquidity_capacity_ratio": adjusted_ratio,
            "capacity_test_basis": context["capacity_test_basis"],
        }
    return adjusted, audit


def hard_safety_failures(
    *,
    safety_attestations: Mapping[str, bool],
    performance_by_horizon: Mapping[str, Mapping[str, Any]],
    path_metrics_by_horizon: Mapping[str, Mapping[str, Any]],
    framework: Mapping[str, Any],
) -> tuple[str, ...]:
    limits = framework["layer_1_data_and_safety"]["hard_limits"]
    failures = [
        f"attestation:{name}" for name, passed in safety_attestations.items() if not passed
    ]
    for horizon in sorted(REQUIRED_HORIZON_KEYS, key=int):
        performance = performance_by_horizon[horizon]
        metrics = path_metrics_by_horizon[horizon]
        checks = {
            "minimum_daily_path_observations": int(metrics["observation_count"])
            >= int(limits["minimum_daily_path_observations"]),
            "minimum_paired_observations": int(performance["paired_observation_count"])
            >= int(limits["minimum_paired_observations"]),
            "maximum_drawdown": max(
                float(performance["maximum_drawdown"]), float(metrics["maximum_drawdown"])
            )
            <= float(limits["maximum_drawdown"]),
            "minimum_expected_shortfall_95": min(
                float(performance["expected_shortfall_95"]),
                float(metrics["expected_shortfall_95"]),
            )
            >= float(limits["minimum_expected_shortfall_95"]),
            "maximum_turnover": float(performance["turnover"])
            <= float(limits["maximum_turnover"]),
            "maximum_average_transaction_cost": float(
                performance["average_transaction_cost"]
            )
            <= float(limits["maximum_average_transaction_cost"]),
            "minimum_liquidity_capacity_ratio": float(
                performance["liquidity_capacity_ratio"]
            )
            >= float(limits["minimum_liquidity_capacity_ratio"]),
            "maximum_winner_concentration_hhi": float(
                performance["winner_concentration_hhi"]
            )
            <= float(limits["maximum_winner_concentration_hhi"]),
            "maximum_single_name_weight": float(performance["maximum_single_name_weight"])
            <= float(limits["maximum_single_name_weight"]),
        }
        failures.extend(f"{horizon}:{name}" for name, passed in checks.items() if not passed)
    return tuple(sorted(failures))


def target_state_for_score(score: float, *, framework: Mapping[str, Any]) -> str:
    value = _finite(score, label="confidence-adjusted score")
    if not 0.0 <= value <= 100.0:
        raise ValueError("confidence-adjusted score must be in [0, 100]")
    layer = framework["layer_4_controlled_deployment"]
    state = "benchmark_production"
    for candidate in layer["tier_order"]:
        if value >= float(layer["tiers"][candidate]["minimum_score"]):
            state = candidate
    return state


def _deployment_modifiers(
    performance_by_horizon: Mapping[str, Mapping[str, Any]], *, framework: Mapping[str, Any]
) -> tuple[float, float]:
    """Return capacity credit and the disabled cohort-diversification haircut.

    Layer 1 rejects any allocation-adjusted capacity ratio below one.  At or
    above that executable threshold, liquidity receives full credit and cannot
    shrink an otherwise eligible standard allocation.
    """

    policy = framework["layer_4_controlled_deployment"]["modifiers"]
    capacity = min(
        float(performance_by_horizon[key]["liquidity_capacity_ratio"])
        for key in REQUIRED_HORIZON_KEYS
    )
    capacity_modifier = min(
        1.0,
        max(0.0, capacity / float(policy["capacity_full_credit_ratio"])),
    )
    return capacity_modifier, 1.0


def _limited_design_target(target: str, *, framework: Mapping[str, Any]) -> str:
    order = framework["layer_4_controlled_deployment"]["tier_order"]
    maximum = framework["layer_4_controlled_deployment"]["transition_policy"][
        "design_evidence_maximum_state"
    ]
    return order[min(order.index(target), order.index(maximum))]


def _validate_previous_decision(
    previous: Mapping[str, Any],
    *,
    framework: Mapping[str, Any],
    trusted_sha256: str,
) -> dict[str, Any]:
    item = _mapping(previous, label="previous decision")
    if item.get("schema_version") != DECISION_SCHEMA or item.get("model_family") != MODEL_FAMILY:
        raise ValueError("previous decision has an unsupported schema")
    if item.get("framework_sha256") != framework_sha256(framework):
        raise ValueError("previous decision belongs to a different framework")
    _iso_date(item.get("asof_date"), label="previous decision asof_date")
    _integer(item.get("decision_sequence"), label="previous decision sequence", minimum=1)
    if set(_mapping(item.get("cohorts"), label="previous decision cohorts")) != REQUIRED_COHORTS:
        raise ValueError("previous decision has the wrong cohort census")
    observed = _sha(item.get("payload_sha256"), label="previous decision hash")
    if canonical_sha256(item) != observed:
        raise ValueError("previous decision self-hash mismatch")
    if observed != _sha(trusted_sha256, label="trusted previous decision hash"):
        raise ValueError("previous decision does not match its external trusted digest")
    return item


def _authorize_evidence_role(
    *,
    source: Mapping[str, Any],
    framework: Mapping[str, Any],
    previous_decision: Mapping[str, Any] | None,
    previous_promotion_input: Mapping[str, Any] | None,
    preregistration: Mapping[str, Any] | None,
    registration_anchor: Mapping[str, Any] | None,
    trusted_registration_anchor_sha256: str | None,
    fresh_evidence_manifest: Mapping[str, Any] | None,
) -> dict[str, Any]:
    requested = str(source["evidence_role"])
    bundle = (
        previous_promotion_input,
        preregistration,
        registration_anchor,
        trusted_registration_anchor_sha256,
        fresh_evidence_manifest,
    )
    if requested == "design_evidence":
        if any(value is not None for value in bundle[1:]):
            raise ValueError("design evidence cannot carry a partial fresh-evidence bundle")
        return {
            "requested_role": requested,
            "effective_role": "design_evidence",
            "preregistration_sha256": None,
            "registration_anchor_sha256": None,
            "fresh_evidence_manifest_sha256": None,
            "previous_promotion_input_sha256": None,
        }
    if previous_decision is None or any(value is None for value in bundle):
        raise ValueError(
            "fresh chronological evidence requires an anchored preregistration, "
            "prior input, and reproducible fresh-evidence manifest"
        )
    from consumer_defensive.core.promotion_evidence_v3 import (
        validate_fresh_evidence_manifest,
    )

    manifest = validate_fresh_evidence_manifest(
        fresh_evidence_manifest,
        preregistration=preregistration,
        registration_anchor=registration_anchor,
        trusted_anchor_sha256=str(trusted_registration_anchor_sha256),
        previous_decision=previous_decision,
        previous_promotion_input=previous_promotion_input,
        current_promotion_input=source,
        framework=framework,
    )
    return {
        "requested_role": requested,
        "effective_role": "fresh_chronological",
        "preregistration_sha256": manifest["preregistration_sha256"],
        "registration_anchor_sha256": manifest["registration_anchor_sha256"],
        "fresh_evidence_manifest_sha256": manifest["payload_sha256"],
        "previous_promotion_input_sha256": manifest[
            "previous_promotion_input_sha256"
        ],
    }


def _transition_state(
    *,
    prior_item: Mapping[str, Any] | None,
    target: str,
    hard_failures: Sequence[str],
    evidence_role: str,
    material_model_change: bool,
    asof_date: date,
    current_input_panel_sha256: str,
    prior_input_panel_sha256: str | None,
    current_counts: Mapping[str, int],
    framework: Mapping[str, Any],
) -> tuple[str, str, list[str]]:
    layer = framework["layer_4_controlled_deployment"]
    policy = layer["transition_policy"]
    order = layer["tier_order"]
    if prior_item is None:
        if hard_failures:
            return "benchmark_production", asof_date.isoformat(), []
        genesis = _limited_design_target(target, framework=framework)
        return genesis, asof_date.isoformat(), []

    prior_state = str(prior_item["state"])
    prior_entered = _iso_date(prior_item["state_entered_asof"], label="prior state entry")
    if hard_failures:
        return (
            "rollback" if prior_state in DEPLOYABLE_STATES else "benchmark_production",
            asof_date.isoformat(),
            [],
        )
    if material_model_change and prior_state in DEPLOYABLE_STATES:
        reset = str(policy["material_model_change_resets_to"])
        if order.index(reset) < order.index(prior_state):
            return reset, asof_date.isoformat(), ["material_model_change_reset"]

    prior_index = -1 if prior_state == "rollback" else order.index(prior_state)
    target_index = order.index(target)
    prior_blockers = [str(value) for value in prior_item.get("transition_blockers", ())]
    if prior_blockers and target_index >= prior_index:
        if evidence_role != "fresh_chronological":
            return prior_state, prior_entered.isoformat(), sorted(
                set([*prior_blockers, "fresh_chronological_evidence_required_for_reauthorization"])
            )
        # A state that had no live authority must first earn that same tier back.
        # Resetting its entry date prevents a later review from counting shadow
        # time toward the controlled-deployment dwell requirement.
        return prior_state, asof_date.isoformat(), []
    if target_index < prior_index:
        return target, asof_date.isoformat(), []
    if target_index == prior_index:
        if prior_state in DEPLOYABLE_STATES and evidence_role != "fresh_chronological":
            return prior_state, prior_entered.isoformat(), [
                "fresh_chronological_evidence_required_for_active_review"
            ]
        return prior_state, prior_entered.isoformat(), []

    blockers: list[str] = []
    if evidence_role != "fresh_chronological":
        blockers.append("fresh_chronological_evidence_required")
    if current_input_panel_sha256 == prior_input_panel_sha256:
        blockers.append("new_input_panel_required")
    minimum_growth = int(policy["minimum_new_paired_observations_per_horizon"])
    previous_counts = prior_item["paired_observation_count_by_horizon"]
    for horizon in sorted(REQUIRED_HORIZON_KEYS, key=int):
        if int(current_counts[horizon]) - int(previous_counts[horizon]) < minimum_growth:
            blockers.append(f"minimum_new_paired_observations_{horizon}")
    if prior_state != "rollback" and prior_state in policy["minimum_dwell_days"]:
        elapsed = (asof_date - prior_entered).days
        if elapsed < int(policy["minimum_dwell_days"][prior_state]):
            blockers.append(f"minimum_{prior_state}_dwell")
    if blockers:
        return prior_state, prior_entered.isoformat(), sorted(blockers)
    next_state = order[prior_index + 1]
    return next_state, asof_date.isoformat(), []


def build_promotion_decision(
    *,
    promotion_input: Mapping[str, Any],
    framework: Mapping[str, Any],
    previous_decision: Mapping[str, Any] | None = None,
    trusted_previous_decision_sha256: str | None = None,
    previous_promotion_input: Mapping[str, Any] | None = None,
    preregistration: Mapping[str, Any] | None = None,
    registration_anchor: Mapping[str, Any] | None = None,
    trusted_registration_anchor_sha256: str | None = None,
    fresh_evidence_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    validated_framework = validate_framework(framework)
    source = validate_promotion_input(promotion_input, framework=validated_framework)
    capital_context = source["capital_allocation_context"]
    previous = (
        None
        if previous_decision is None
        else _validate_previous_decision(
            previous_decision,
            framework=validated_framework,
            trusted_sha256=str(trusted_previous_decision_sha256 or ""),
        )
    )
    if previous is None and trusted_previous_decision_sha256 is not None:
        raise ValueError("trusted previous decision hash was supplied without a decision")
    authorization = _authorize_evidence_role(
        source=source,
        framework=validated_framework,
        previous_decision=previous,
        previous_promotion_input=previous_promotion_input,
        preregistration=preregistration,
        registration_anchor=registration_anchor,
        trusted_registration_anchor_sha256=trusted_registration_anchor_sha256,
        fresh_evidence_manifest=fresh_evidence_manifest,
    )
    effective_evidence_role = str(authorization["effective_role"])
    asof = _iso_date(source["asof_date"], label="decision asof_date")
    if previous is not None and asof <= _iso_date(
        previous["asof_date"], label="previous decision asof_date"
    ):
        raise ValueError("decision dates must advance strictly")
    sequence = 1 if previous is None else int(previous["decision_sequence"]) + 1
    layer2 = validated_framework["layer_2_economic_performance"]
    allocation_policy = validated_framework["layer_4_controlled_deployment"][
        "standard_allocation_policy"
    ]
    standard_allocation_fraction = float(
        capital_context["sector_max_fraction"]
    ) / len(REQUIRED_COHORTS)
    standard_allocation_notional_usd = float(
        capital_context["sector_max_notional_usd"]
    ) / len(REQUIRED_COHORTS)
    cohorts: dict[str, Any] = {}
    for cohort in sorted(REQUIRED_COHORTS):
        source_cohort = source["cohorts"][cohort]
        performance = {
            key: source_cohort["horizons"][key]["performance"]
            for key in sorted(REQUIRED_HORIZON_KEYS, key=int)
        }
        adjusted_performance, capacity_audit = (
            allocation_adjusted_capacity_evidence(
                performance,
                capital_allocation_context=capital_context,
            )
        )
        paths = {
            key: source_cohort["horizons"][key]["daily_path"]
            for key in sorted(REQUIRED_HORIZON_KEYS, key=int)
        }
        path_metrics: dict[str, Any] = {}
        metric_scores: dict[str, Any] = {}
        block_scores: dict[str, Any] = {}
        horizon_scores: dict[str, float] = {}
        path_evidence: dict[str, Any] = {}
        for key in sorted(REQUIRED_HORIZON_KEYS, key=int):
            metrics = compute_path_metrics(
                paths[key], periods_per_year=int(layer2["periods_per_year"])
            )
            scores, blocks, total = _horizon_economic_score(
                adjusted_performance[key],
                metrics,
                framework=validated_framework,
            )
            path_metrics[key] = metrics
            metric_scores[key] = scores
            block_scores[key] = blocks
            horizon_scores[key] = total
            path_evidence[key] = {
                "path_sha256": value_sha256(paths[key]),
                "observation_count": len(paths[key]),
                "start_date": paths[key][0]["date"],
                "end_date": paths[key][-1]["date"],
            }
        effective_weights, correlations = effective_horizon_weights(
            paths,
            nominal_weights=layer2["horizon_weights"],
            positive_correlation_penalty=float(layer2["positive_correlation_penalty"]),
        )
        economic_score = sum(
            effective_weights[key] * horizon_scores[key]
            for key in REQUIRED_HORIZON_KEYS
        )
        weighted_dsr = sum(
            effective_weights[key] * float(performance[key]["deflated_sharpe_ratio"])
            for key in REQUIRED_HORIZON_KEYS
        )
        weighted_pbo = sum(
            effective_weights[key]
            * float(performance[key]["probability_of_backtest_overfitting"])
            for key in REQUIRED_HORIZON_KEYS
        )
        confidence = confidence_multiplier(
            deflated_sharpe_ratio=weighted_dsr,
            probability_of_backtest_overfitting=weighted_pbo,
            framework=validated_framework,
        )
        shrink_target = float(
            validated_framework["layer_3_confidence_adjustment"]["score_shrinkage_target"]
        )
        adjusted_score = shrink_target + confidence * (economic_score - shrink_target)
        failures = hard_safety_failures(
            safety_attestations=source["safety_attestations"],
            performance_by_horizon=adjusted_performance,
            path_metrics_by_horizon=path_metrics,
            framework=validated_framework,
        )
        score_target = target_state_for_score(adjusted_score, framework=validated_framework)
        eligible_target = (
            _limited_design_target(score_target, framework=validated_framework)
            if effective_evidence_role == "design_evidence"
            else score_target
        )
        prior_item = None if previous is None else previous["cohorts"][cohort]
        contract = source_cohort["production_model_contract"]
        material_change = bool(
            prior_item is not None
            and (
                source["source_lineage"]["code_sha256"]
                != previous["source_lineage"]["code_sha256"]
                or source["source_lineage"]["candidate_registry_sha256"]
                != previous["source_lineage"]["candidate_registry_sha256"]
                or contract["payload_sha256"] != prior_item["production_model_contract_sha256"]
            )
        )
        counts = {
            key: int(performance[key]["paired_observation_count"])
            for key in REQUIRED_HORIZON_KEYS
        }
        state, state_entered, blockers = _transition_state(
            prior_item=prior_item,
            target=eligible_target,
            hard_failures=failures,
            evidence_role=effective_evidence_role,
            material_model_change=material_change,
            asof_date=asof,
            current_input_panel_sha256=source["source_lineage"][
                "input_panel_sha256"
            ],
            prior_input_panel_sha256=(
                None
                if previous is None
                else previous["source_lineage"]["input_panel_sha256"]
            ),
            current_counts=counts,
            framework=validated_framework,
        )
        capacity_modifier, diversification_modifier = _deployment_modifiers(
            adjusted_performance, framework=validated_framework
        )
        tier = validated_framework["layer_4_controlled_deployment"]["tiers"][state]
        tier_fraction = float(tier["deployment_fraction"])
        standard_production_eligible = bool(
            not failures
            and not blockers
            and state == "active_full"
            and adjusted_score
            >= float(allocation_policy["minimum_confidence_adjusted_score"])
            and capacity_modifier >= 1.0
        )
        effective_fraction = (
            tier_fraction * capacity_modifier
            if standard_production_eligible
            else 0.0
        )
        full_cap = standard_allocation_fraction
        optimizer_cap = full_cap * effective_fraction
        weighted_excess_cagr = sum(
            effective_weights[key] * float(path_metrics[key]["excess_cagr"])
            for key in REQUIRED_HORIZON_KEYS
        )
        expected_alpha = (
            max(0.0, weighted_excess_cagr * confidence)
            if standard_production_eligible
            else 0.0
        )
        cohorts[cohort] = {
            "prior_state": "benchmark_production" if prior_item is None else prior_item["state"],
            "state": state,
            "state_entered_asof": state_entered,
            "data_and_safety_status": "PASS" if not failures else "FAIL",
            "hard_failures": list(failures),
            "horizon_performance": performance,
            "horizon_capacity_audit": capacity_audit,
            "minimum_executable_capacity_usd": min(
                float(row["executable_capacity_usd"])
                for row in capacity_audit.values()
            ),
            "minimum_allocation_adjusted_liquidity_capacity_ratio": min(
                float(row["allocation_adjusted_liquidity_capacity_ratio"])
                for row in capacity_audit.values()
            ),
            "horizon_path_metrics": path_metrics,
            "horizon_path_evidence": path_evidence,
            "horizon_metric_scores": metric_scores,
            "horizon_block_scores": block_scores,
            "horizon_economic_scores": horizon_scores,
            "nominal_horizon_weights": {
                key: float(layer2["horizon_weights"][key])
                for key in sorted(REQUIRED_HORIZON_KEYS, key=int)
            },
            "effective_horizon_weights": effective_weights,
            "horizon_excess_return_correlations": correlations,
            "base_economic_score": economic_score,
            "weighted_deflated_sharpe_ratio": weighted_dsr,
            "weighted_probability_of_backtest_overfitting": weighted_pbo,
            "confidence_multiplier": confidence,
            "confidence_adjusted_score": adjusted_score,
            "economic_target_state": score_target,
            "evidence_eligible_target_state": eligible_target,
            "transition_blockers": blockers,
            "standard_production_eligible": standard_production_eligible,
            "tier_deployment_fraction": tier_fraction,
            "capacity_modifier": capacity_modifier,
            "diversification_modifier": diversification_modifier,
            "cohort_level_diversification_haircut_applied": False,
            "effective_deployment_fraction": effective_fraction,
            "approved_full_portfolio_cap": full_cap,
            "optimizer_cap": optimizer_cap,
            "standard_allocation_notional_usd": (
                standard_allocation_notional_usd
                if standard_production_eligible
                else 0.0
            ),
            "expected_alpha_at_full": expected_alpha,
            "paired_observation_count_by_horizon": counts,
            "production_model_contract_sha256": contract["payload_sha256"],
        }
    eligible_cohort_count = sum(
        int(item["standard_production_eligible"]) for item in cohorts.values()
    )
    allocated_sector_fraction = (
        eligible_cohort_count * standard_allocation_fraction
    )
    allocated_sector_notional_usd = (
        eligible_cohort_count * standard_allocation_notional_usd
    )
    decision: dict[str, Any] = {
        "schema_version": DECISION_SCHEMA,
        "model_family": MODEL_FAMILY,
        "framework_sha256": framework_sha256(validated_framework),
        "asof_date": source["asof_date"],
        "evidence_asof_date": source["asof_date"],
        "capital_allocation_context_asof_date": capital_context["asof_date"],
        "capital_allocation_context_sha256": capital_context["payload_sha256"],
        "capital_context_is_predictive_evidence": False,
        "account_aum_usd": float(capital_context["account_aum_usd"]),
        "consumer_defensive_sector_max_fraction": float(
            capital_context["sector_max_fraction"]
        ),
        "consumer_defensive_sector_max_notional_usd": float(
            capital_context["sector_max_notional_usd"]
        ),
        "standard_allocation_policy": dict(allocation_policy),
        "standard_cohort_allocation_fraction": standard_allocation_fraction,
        "standard_cohort_allocation_notional_usd": (
            standard_allocation_notional_usd
        ),
        "standard_production_eligible_cohort_count": eligible_cohort_count,
        "allocated_sector_fraction": allocated_sector_fraction,
        "allocated_sector_notional_usd": allocated_sector_notional_usd,
        "unallocated_sector_fraction": float(
            capital_context["sector_max_fraction"]
        )
        - allocated_sector_fraction,
        "unallocated_sector_notional_usd": float(
            capital_context["sector_max_notional_usd"]
        )
        - allocated_sector_notional_usd,
        "calibration_reference_notional_usd": float(
            capital_context["calibration_reference_notional_usd"]
        ),
        "capacity_test_basis": capital_context["capacity_test_basis"],
        "evidence_role": effective_evidence_role,
        "evidence_authorization": authorization,
        "source_input_sha256": source["payload_sha256"],
        "source_lineage": source["source_lineage"],
        "decision_sequence": sequence,
        "previous_decision_sha256": None if previous is None else previous["payload_sha256"],
        "calibration_write_performed": False,
        "portfolio_write_performed": False,
        "cohorts": cohorts,
    }
    decision["payload_sha256"] = canonical_sha256(decision)
    return decision


def validate_promotion_decision(
    payload: Mapping[str, Any],
    *,
    promotion_input: Mapping[str, Any],
    framework: Mapping[str, Any],
    previous_decision: Mapping[str, Any] | None = None,
    trusted_previous_decision_sha256: str | None = None,
    previous_promotion_input: Mapping[str, Any] | None = None,
    preregistration: Mapping[str, Any] | None = None,
    registration_anchor: Mapping[str, Any] | None = None,
    trusted_registration_anchor_sha256: str | None = None,
    fresh_evidence_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    actual = _mapping(payload, label="promotion decision v3")
    if actual.get("schema_version") != DECISION_SCHEMA:
        raise ValueError("unsupported promotion decision v3")
    if canonical_sha256(actual) != _sha(actual.get("payload_sha256"), label="decision hash"):
        raise ValueError("promotion decision self-hash mismatch")
    expected = build_promotion_decision(
        promotion_input=promotion_input,
        framework=framework,
        previous_decision=previous_decision,
        trusted_previous_decision_sha256=trusted_previous_decision_sha256,
        previous_promotion_input=previous_promotion_input,
        preregistration=preregistration,
        registration_anchor=registration_anchor,
        trusted_registration_anchor_sha256=trusted_registration_anchor_sha256,
        fresh_evidence_manifest=fresh_evidence_manifest,
    )
    if actual != expected:
        raise ValueError("promotion decision does not reproduce from its bound evidence")
    return actual


def build_activation_registry(
    *,
    decision: Mapping[str, Any],
    promotion_input: Mapping[str, Any],
    framework: Mapping[str, Any],
    previous_decision: Mapping[str, Any] | None = None,
    trusted_previous_decision_sha256: str | None = None,
    previous_promotion_input: Mapping[str, Any] | None = None,
    preregistration: Mapping[str, Any] | None = None,
    registration_anchor: Mapping[str, Any] | None = None,
    trusted_registration_anchor_sha256: str | None = None,
    fresh_evidence_manifest: Mapping[str, Any] | None = None,
    effective_from: str | None = None,
    valid_until: str | None = None,
) -> dict[str, Any]:
    validated_decision = validate_promotion_decision(
        decision,
        promotion_input=promotion_input,
        framework=framework,
        previous_decision=previous_decision,
        trusted_previous_decision_sha256=trusted_previous_decision_sha256,
        previous_promotion_input=previous_promotion_input,
        preregistration=preregistration,
        registration_anchor=registration_anchor,
        trusted_registration_anchor_sha256=trusted_registration_anchor_sha256,
        fresh_evidence_manifest=fresh_evidence_manifest,
    )
    source = validate_promotion_input(promotion_input, framework=framework)
    context_effective_date = _iso_date(
        source["capital_allocation_context"]["asof_date"],
        label="capital allocation context asof_date",
    )
    default_effective = max(
        _iso_date(validated_decision["asof_date"], label="decision asof_date"),
        context_effective_date,
    ).isoformat()
    effective = default_effective if effective_from is None else effective_from
    effective_date = _iso_date(effective, label="activation effective_from")
    decision_date = _iso_date(validated_decision["asof_date"], label="decision asof_date")
    if effective_date < decision_date:
        raise ValueError("activation cannot predate its promotion decision")
    maximum_age = int(
        validate_framework(framework)["layer_4_controlled_deployment"][
            "transition_policy"
        ]["maximum_activation_age_days"]
    )
    authority_deadline = decision_date + timedelta(days=maximum_age)
    if effective_date > authority_deadline:
        raise ValueError("activation effective date exceeds the decision authority window")
    expiry = (
        authority_deadline
        if valid_until is None
        else _iso_date(valid_until, label="activation valid_until")
    )
    if expiry < effective_date or expiry > authority_deadline:
        raise ValueError("activation expiry exceeds the decision authority window")
    locks: dict[str, Any] = {}
    for cohort in sorted(REQUIRED_COHORTS):
        item = validated_decision["cohorts"][cohort]
        contract = source["cohorts"][cohort]["production_model_contract"]
        promoted = (
            item["state"] in DEPLOYABLE_STATES
            and item["data_and_safety_status"] == "PASS"
            and not item["transition_blockers"]
            and float(item["optimizer_cap"]) > 0.0
        )
        effective_fraction = item["effective_deployment_fraction"] if promoted else 0.0
        optimizer_cap = item["optimizer_cap"] if promoted else 0.0
        expected_alpha = item["expected_alpha_at_full"] if promoted else 0.0
        lock_basis = {
            "cohort": cohort,
            "decision_sha256": validated_decision["payload_sha256"],
            "model_contract_sha256": contract["payload_sha256"],
            "effective_from": effective,
            "valid_until": expiry.isoformat(),
        }
        lock: dict[str, Any] = {
            "schema_version": ACTIVATION_LOCK_SCHEMA,
            "model_family": MODEL_FAMILY,
            "cohort": cohort,
            "lock_id": f"cdv3_{value_sha256(lock_basis)[:24]}",
            "effective_from": effective,
            "valid_until": expiry.isoformat(),
            "deployment_state": item["state"],
            "promotion_state": "promoted" if promoted else "shadow_monitor",
            "investable": promoted,
            "tier_deployment_fraction": item["tier_deployment_fraction"],
            "effective_deployment_fraction": effective_fraction,
            "approved_full_portfolio_cap": item["approved_full_portfolio_cap"],
            "optimizer_cap": optimizer_cap,
            "expected_alpha_at_full": expected_alpha,
            "confidence_multiplier": item["confidence_multiplier"],
            "decision_sha256": validated_decision["payload_sha256"],
            "framework_sha256": validated_decision["framework_sha256"],
            "source_input_sha256": validated_decision["source_input_sha256"],
            "model_contract_sha256": contract["payload_sha256"],
            "selected_candidate_id": contract["selected_candidate_id"],
            "score_model_version": contract["score_model_version"],
            "scoring_contract_version": contract["scoring_contract_version"],
        }
        lock["payload_sha256"] = canonical_sha256(lock)
        locks[cohort] = lock
    registry: dict[str, Any] = {
        "schema_version": ACTIVATION_REGISTRY_SCHEMA,
        "model_family": MODEL_FAMILY,
        "asof_date": validated_decision["asof_date"],
        "effective_from": effective,
        "valid_until": expiry.isoformat(),
        "maximum_activation_age_days": maximum_age,
        "decision_sha256": validated_decision["payload_sha256"],
        "framework_sha256": validated_decision["framework_sha256"],
        "source_input_sha256": validated_decision["source_input_sha256"],
        "calibration_write_performed": False,
        "portfolio_write_performed": False,
        "cohorts": locks,
    }
    registry["payload_sha256"] = canonical_sha256(registry)
    return validate_activation_registry(registry)


def validate_activation_registry(payload: Mapping[str, Any]) -> dict[str, Any]:
    registry = _exact_mapping(
        payload,
        frozenset(
            {
                "schema_version",
                "model_family",
                "asof_date",
                "effective_from",
                "valid_until",
                "maximum_activation_age_days",
                "decision_sha256",
                "framework_sha256",
                "source_input_sha256",
                "calibration_write_performed",
                "portfolio_write_performed",
                "cohorts",
                "payload_sha256",
            }
        ),
        label="activation registry v3",
    )
    if registry["schema_version"] != ACTIVATION_REGISTRY_SCHEMA or registry[
        "model_family"
    ] != MODEL_FAMILY:
        raise ValueError("unsupported activation registry v3")
    registry_asof = _iso_date(registry["asof_date"], label="activation registry asof_date")
    registry_effective = _iso_date(
        registry["effective_from"], label="activation registry effective_from"
    )
    registry_expiry = _iso_date(registry["valid_until"], label="activation registry valid_until")
    maximum_age = _integer(
        registry["maximum_activation_age_days"], label="maximum activation age", minimum=1
    )
    if maximum_age != 63 or not registry_asof <= registry_effective <= registry_expiry:
        raise ValueError("activation registry dates violate the frozen policy")
    authority_deadline = registry_asof + timedelta(days=maximum_age)
    if registry_effective > authority_deadline or registry_expiry > authority_deadline:
        raise ValueError(
            "activation registry dates exceed the decision-anchored authority window"
        )
    for field in ("decision_sha256", "framework_sha256", "source_input_sha256"):
        _sha(registry[field], label=f"activation registry {field}")
    if registry["calibration_write_performed"] is not False or registry[
        "portfolio_write_performed"
    ] is not False:
        raise ValueError("activation registry cannot claim calibration or portfolio writes")
    cohorts = _mapping(registry["cohorts"], label="activation registry cohorts")
    if set(cohorts) != REQUIRED_COHORTS:
        raise ValueError("activation registry has the wrong cohort census")
    lock_keys = frozenset(
        {
            "schema_version",
            "model_family",
            "cohort",
            "lock_id",
            "effective_from",
            "valid_until",
            "deployment_state",
            "promotion_state",
            "investable",
            "tier_deployment_fraction",
            "effective_deployment_fraction",
            "approved_full_portfolio_cap",
            "optimizer_cap",
            "expected_alpha_at_full",
            "confidence_multiplier",
            "decision_sha256",
            "framework_sha256",
            "source_input_sha256",
            "model_contract_sha256",
            "selected_candidate_id",
            "score_model_version",
            "scoring_contract_version",
            "payload_sha256",
        }
    )
    for cohort, raw in cohorts.items():
        lock = _exact_mapping(raw, lock_keys, label=f"activation lock {cohort}")
        if (
            lock["schema_version"] != ACTIVATION_LOCK_SCHEMA
            or lock["model_family"] != MODEL_FAMILY
            or lock["cohort"] != cohort
        ):
            raise ValueError(f"activation lock {cohort} scope mismatch")
        if lock["deployment_state"] not in DECISION_STATES:
            raise ValueError(f"activation lock {cohort} has an invalid state")
        if not isinstance(lock["lock_id"], str) or not lock["lock_id"].startswith("cdv3_"):
            raise ValueError(f"activation lock {cohort} has an invalid lock id")
        lock_effective = _iso_date(lock["effective_from"], label=f"activation lock {cohort} effective_from")
        lock_expiry = _iso_date(lock["valid_until"], label=f"activation lock {cohort} valid_until")
        if lock_effective != registry_effective or lock_expiry != registry_expiry:
            raise ValueError(f"activation lock {cohort} dates are not registry-bound")
        for field in (
            "decision_sha256",
            "framework_sha256",
            "source_input_sha256",
            "model_contract_sha256",
        ):
            _sha(lock[field], label=f"activation lock {cohort}.{field}")
        if (
            lock["decision_sha256"] != registry["decision_sha256"]
            or lock["framework_sha256"] != registry["framework_sha256"]
            or lock["source_input_sha256"] != registry["source_input_sha256"]
        ):
            raise ValueError(f"activation lock {cohort} is not registry-bound")
        expected_lock_id = "cdv3_" + value_sha256(
            {
                "cohort": cohort,
                "decision_sha256": lock["decision_sha256"],
                "model_contract_sha256": lock["model_contract_sha256"],
                "effective_from": lock["effective_from"],
                "valid_until": lock["valid_until"],
            }
        )[:24]
        if lock["lock_id"] != expected_lock_id:
            raise ValueError(f"activation lock {cohort} id is not reproducible")
        for field in (
            "tier_deployment_fraction",
            "effective_deployment_fraction",
            "approved_full_portfolio_cap",
            "optimizer_cap",
            "expected_alpha_at_full",
            "confidence_multiplier",
        ):
            value = _finite(lock[field], label=f"activation lock {cohort}.{field}")
            if value < 0.0:
                raise ValueError(f"activation lock {cohort}.{field} cannot be negative")
        for field in (
            "tier_deployment_fraction",
            "effective_deployment_fraction",
            "approved_full_portfolio_cap",
            "optimizer_cap",
            "confidence_multiplier",
        ):
            if float(lock[field]) > 1.0:
                raise ValueError(f"activation lock {cohort}.{field} cannot exceed one")
        if not 0.50 <= float(lock["confidence_multiplier"]) <= 1.0:
            raise ValueError(f"activation lock {cohort} confidence is outside policy")
        if float(lock["effective_deployment_fraction"]) > float(
            lock["tier_deployment_fraction"]
        ) + 1e-12:
            raise ValueError(f"activation lock {cohort} exceeds its tier fraction")
        if float(lock["optimizer_cap"]) > float(lock["approved_full_portfolio_cap"]) + 1e-12:
            raise ValueError(f"activation lock {cohort} exceeds its approved full cap")
        if not math.isclose(
            float(lock["optimizer_cap"]),
            float(lock["approved_full_portfolio_cap"])
            * float(lock["effective_deployment_fraction"]),
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError(f"activation lock {cohort} cap arithmetic is inconsistent")
        expected_promoted = (
            lock["deployment_state"] in DEPLOYABLE_STATES
            and float(lock["optimizer_cap"]) > 0.0
        )
        if lock["investable"] is not expected_promoted or lock["promotion_state"] != (
            "promoted" if expected_promoted else "shadow_monitor"
        ):
            raise ValueError(f"activation lock {cohort} promotion state is inconsistent")
        for field in ("selected_candidate_id", "score_model_version", "scoring_contract_version"):
            if not isinstance(lock[field], str) or not lock[field].strip():
                raise ValueError(f"activation lock {cohort}.{field} is blank")
        if canonical_sha256(lock) != _sha(
            lock["payload_sha256"], label=f"activation lock {cohort} hash"
        ):
            raise ValueError(f"activation lock {cohort} self-hash mismatch")
    if canonical_sha256(registry) != _sha(
        registry["payload_sha256"], label="activation registry hash"
    ):
        raise ValueError("activation registry self-hash mismatch")
    return registry


def apply_activation_to_rank_rows(
    rows: Sequence[Mapping[str, Any]], *, activation_registry: Mapping[str, Any]
) -> list[dict[str, Any]]:
    registry = validate_activation_registry(activation_registry)
    output: list[dict[str, Any]] = []
    for position, raw in enumerate(rows):
        row = dict(raw)
        cohort = str(row.get("calibration_cohort") or "").strip()
        if cohort not in REQUIRED_COHORTS:
            raise ValueError(f"rank row {position} has an unsupported cohort")
        lock = registry["cohorts"][cohort]
        row_asof = _iso_date(row.get("asof_date"), label=f"rank row {position} asof_date")
        if row_asof < _iso_date(lock["effective_from"], label="lock effective_from"):
            raise ValueError(f"rank row {position} predates its activation lock")
        if row_asof > _iso_date(lock["valid_until"], label="lock valid_until"):
            raise ValueError(f"rank row {position} is beyond its activation review expiry")
        if str(row.get("score_model_version") or "").strip() != lock["score_model_version"]:
            raise ValueError(f"rank row {position} has the wrong score model version")
        if str(row.get("scoring_contract_version") or "").strip() != lock[
            "scoring_contract_version"
        ]:
            raise ValueError(f"rank row {position} has the wrong scoring contract version")
        if str(row.get("consumer_defensive_selected_candidate_id") or "").strip() != lock[
            "selected_candidate_id"
        ]:
            raise ValueError(f"rank row {position} is not bound to the selected candidate")
        if str(row.get("consumer_defensive_model_contract_sha256") or "").strip().lower() != str(
            lock["model_contract_sha256"]
        ).lower():
            raise ValueError(f"rank row {position} is not bound to the model contract")
        promoted = bool(lock["investable"])
        preexisting_oos = str(row.get("oos_score_valid_flag") or "").strip().lower() in {
            "1", "1.0", "true"
        }
        row["promotion_state"] = "promoted" if promoted else "shadow_monitor"
        eligible = promoted and preexisting_oos and str(row.get("rank_ready_flag") or "0").strip() in {
            "1",
            "1.0",
            "true",
        }
        row["portfolio_candidate_gate"] = int(eligible)
        row["portfolio_candidate_status"] = "eligible" if eligible else "not_eligible"
        row["portfolio_candidate_reason"] = "ok" if eligible else "promotion_or_rank_gate"
        row["oos_invalid_reason"] = "" if eligible else str(row.get("oos_invalid_reason") or "promotion_or_rank_gate")
        row["consumer_defensive_deployment_state"] = lock["deployment_state"]
        row["consumer_defensive_production_lock_id"] = lock["lock_id"]
        row["consumer_defensive_production_lock_sha256"] = lock["payload_sha256"]
        row["consumer_defensive_model_contract_sha256"] = lock["model_contract_sha256"]
        row["consumer_defensive_decision_sha256"] = lock["decision_sha256"]
        row["consumer_defensive_optimizer_cap"] = lock["optimizer_cap"]
        row["consumer_defensive_confidence_multiplier"] = lock["confidence_multiplier"]
        output.append(row)
    return output


__all__ = [
    "ACTIVATION_REGISTRY_SCHEMA",
    "CAPITAL_ALLOCATION_CONTEXT_SCHEMA",
    "DECISION_SCHEMA",
    "DEPLOYABLE_STATES",
    "FRAMEWORK_SCHEMA",
    "PROMOTION_INPUT_SCHEMA",
    "REQUIRED_COHORTS",
    "REQUIRED_HORIZONS",
    "allocation_adjusted_capacity_evidence",
    "apply_activation_to_rank_rows",
    "build_capital_allocation_context",
    "build_activation_registry",
    "build_production_model_contract",
    "build_promotion_decision",
    "canonical_sha256",
    "compute_path_metrics",
    "confidence_multiplier",
    "effective_horizon_weights",
    "framework_sha256",
    "hard_safety_failures",
    "load_framework",
    "score_metric",
    "seal_promotion_input",
    "target_state_for_score",
    "validate_activation_registry",
    "validate_capital_allocation_context",
    "validate_framework",
    "validate_production_model_contract",
    "validate_promotion_decision",
    "validate_promotion_input",
    "value_sha256",
]

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from biotech_index.core.cohort_calibration import (
    BIOTECH_CALIBRATION_COHORTS,
    validate_cohort_budget_weights,
)
from biotech_index.core.config import cfg_get


SUPPORTED_CONTRACT_VERSION = "biotech_promotion_contract_v1"
SUPPORTED_COHORT_CONTRACT_VERSION = "biotech_cohort_promotion_contract_v1"
SUPPORTED_CONTRACT_VERSIONS = frozenset({SUPPORTED_CONTRACT_VERSION, SUPPORTED_COHORT_CONTRACT_VERSION})
LIVE_PORTABLE_SELECTION_POLICIES = frozenset({"raw_legacy_score", "core_structural_veto"})
NO_CHALLENGER_CANDIDATE_IDS = frozenset({"xbi_benchmark_fallback", "production_incumbent_fallback"})


class PromotionContractError(ValueError):
    """Raised when a live promotion contract fails a governance invariant."""


@dataclass(frozen=True)
class ActiveCohortPromotion:
    cohort: str
    candidate_id: str
    candidate_name: str
    selection_policy_name: str
    candidate_pool_top_n: int
    min_score_pct_of_top: float
    max_names: int
    reliability_class: str
    active_weight: float
    xbi_residual_weight: float
    policy_payload: Mapping[str, object]
    score_spec: Mapping[str, object]
    max_name_weight: float = 0.25


@dataclass(frozen=True)
class ActivePromotionContract:
    path: Path
    sha256: str
    effective_date: date
    contract_id: str
    candidate_id: str
    candidate_name: str
    selection_policy_name: str
    candidate_pool_top_n: int
    min_score_pct_of_top: float
    max_names: int
    reliability_class: str
    active_weight: float
    xbi_residual_weight: float
    policy_payload: Mapping[str, object]
    max_name_weight: float = 1.0
    contract_version: str = SUPPORTED_CONTRACT_VERSION
    score_spec: Mapping[str, object] = field(default_factory=dict)
    cohort_policies: Mapping[str, ActiveCohortPromotion] = field(default_factory=dict)

    def policy_for_cohort(self, cohort: str) -> ActiveCohortPromotion | None:
        return self.cohort_policies.get(str(cohort).strip())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _finite(raw: object) -> float | None:
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _profile_matches(contract_profile: object, configured_profile: object, *, tolerance: float = 1e-9) -> bool:
    if not isinstance(contract_profile, Mapping) or not isinstance(configured_profile, Mapping):
        return False
    keys = set(contract_profile).union(configured_profile)
    for key in keys:
        left = _finite(contract_profile.get(key))
        right = _finite(configured_profile.get(key))
        if left is None and right is None:
            continue
        if left is None or right is None or abs(left - right) > tolerance:
            return False
    return True


def _score_spec_matches_config(spec: Mapping[str, object], config: dict[str, Any]) -> bool:
    weights = cfg_get(config, "biotech_scoring.weights", {}) or {}
    if not isinstance(weights, Mapping):
        return False
    scalar_pairs = {
        "clinical_catalyst": "catalyst",
        "clinical_credibility": "credibility",
        "clinical_financial_quality": "financial_quality",
        "clinical_momentum": "momentum",
        "clinical_risk_penalty": "risk_penalty",
    }
    for contract_key, config_key in scalar_pairs.items():
        left = _finite(spec.get(contract_key))
        right = _finite(weights.get(config_key))
        if left is None or right is None or abs(left - right) > 1e-9:
            return False
    profiles = cfg_get(config, "biotech_scoring.investment_weight_profiles", {}) or {}
    if not isinstance(profiles, Mapping):
        return False
    return _profile_matches(spec.get("clinical_stage_profile"), profiles.get("clinical_stage")) and _profile_matches(
        spec.get("commercial_stage_profile"),
        profiles.get("commercial_stage"),
    )


_CLINICAL_POSITIVE_WEIGHT_FIELDS = (
    "clinical_catalyst",
    "clinical_credibility",
    "clinical_financial_quality",
    "clinical_momentum",
)
_PROFILE_POSITIVE_WEIGHT_FIELDS = (
    "clinical_opportunity",
    "commercial_value",
    "forward_guidance",
    "valuation",
    "upside_capacity",
    "institutional_upside",
    "financial_quality",
    "momentum",
    "borrow_signal",
    "short_interest_signal",
    "institutional_crowding",
)


def validate_score_spec(spec: Mapping[str, object], *, context: str) -> None:
    """Validate a calibrated formula before the live scorer is allowed to consume it."""
    clinical_values: list[float] = []
    for field_name in _CLINICAL_POSITIVE_WEIGHT_FIELDS:
        value = _finite(spec.get(field_name))
        if value is None or value < 0.0:
            raise PromotionContractError(f"{context} has invalid {field_name}")
        clinical_values.append(value)
    if abs(sum(clinical_values) - 1.0) > 1e-6:
        raise PromotionContractError(f"{context} clinical positive weights must sum to 1.0")
    clinical_risk = _finite(spec.get("clinical_risk_penalty"))
    if clinical_risk is None or not 0.0 < clinical_risk <= 1.0:
        raise PromotionContractError(f"{context} has invalid clinical_risk_penalty")

    for profile_name in ("clinical_stage_profile", "commercial_stage_profile"):
        profile = spec.get(profile_name)
        if not isinstance(profile, Mapping):
            raise PromotionContractError(f"{context} lacks {profile_name}")
        values: list[float] = []
        for field_name in _PROFILE_POSITIVE_WEIGHT_FIELDS:
            value = _finite(profile.get(field_name, 0.0))
            if value is None or value < 0.0:
                raise PromotionContractError(f"{context}.{profile_name} has invalid {field_name}")
            values.append(value)
        if abs(sum(values) - 1.0) > 1e-6:
            raise PromotionContractError(f"{context}.{profile_name} positive weights must sum to 1.0")
        risk_penalty = _finite(profile.get("risk_penalty", 0.15))
        if risk_penalty is None or not 0.0 < risk_penalty <= 1.0:
            raise PromotionContractError(f"{context}.{profile_name} has invalid risk_penalty")


def _active_policy_from_fold(
    fold_contract: Mapping[str, object],
    *,
    cohort: str,
    validate_formula: bool = True,
) -> ActiveCohortPromotion:
    spec = fold_contract.get("candidate_spec") or {}
    policy = fold_contract.get("selection_policy") or {}
    threshold = fold_contract.get("threshold") or {}
    if not isinstance(spec, Mapping) or not isinstance(policy, Mapping) or not isinstance(threshold, Mapping):
        raise PromotionContractError(f"Promotion contract payload is invalid for cohort={cohort or 'ALL'}")
    if validate_formula:
        validate_score_spec(spec, context=f"cohort={cohort or 'ALL'} candidate_spec")
    policy_name = str(policy.get("policy_name") or "").strip()
    if policy_name not in LIVE_PORTABLE_SELECTION_POLICIES:
        raise PromotionContractError(f"Selection policy {policy_name!r} has no proven live scorer parity implementation")
    candidate_id = str(fold_contract.get("candidate_id") or "").strip()
    if not candidate_id or candidate_id in NO_CHALLENGER_CANDIDATE_IDS:
        raise PromotionContractError(f"Cohort {cohort or 'ALL'} does not contain a promotable challenger")
    score_pct = _finite(threshold.get("min_score_pct_of_top"))
    active_weight = _finite(threshold.get("active_weight"))
    max_name_weight = _finite(threshold.get("max_name_weight"))
    if max_name_weight is None:
        max_name_weight = 0.25
    max_names = int(_finite(threshold.get("max_names")) or 0)
    candidate_pool_top_n = int(_finite(fold_contract.get("candidate_pool_top_n")) or 0)
    if score_pct is None or not 0.0 <= score_pct <= 100.0 or max_names <= 0:
        raise PromotionContractError(f"Cohort {cohort or 'ALL'} has an invalid adaptive score threshold")
    if active_weight is None or not 0.0 <= active_weight <= 1.0:
        raise PromotionContractError(f"Cohort {cohort or 'ALL'} has an invalid active sleeve weight")
    if not 0.0 < max_name_weight <= 1.0:
        raise PromotionContractError(f"Cohort {cohort or 'ALL'} has an invalid max name weight")
    return ActiveCohortPromotion(
        cohort=cohort,
        candidate_id=candidate_id,
        candidate_name=str(spec.get("candidate_name") or "").strip(),
        selection_policy_name=policy_name,
        candidate_pool_top_n=max(max_names, candidate_pool_top_n),
        min_score_pct_of_top=score_pct,
        max_names=max_names,
        reliability_class=str(threshold.get("reliability_class") or "low"),
        active_weight=active_weight,
        xbi_residual_weight=round(1.0 - active_weight, 10),
        max_name_weight=max_name_weight,
        policy_payload=dict(policy),
        score_spec=dict(spec),
    )


def validate_contract_scoring_parity(payload: Mapping[str, object], config: dict[str, Any]) -> None:
    """Fail when an authorized candidate cannot be reproduced by the live scorer."""
    fold_contract = payload.get("latest_primary_fold_contract") or {}
    if not isinstance(fold_contract, Mapping):
        raise PromotionContractError("Adaptive promotion contract lacks latest_primary_fold_contract")
    spec = fold_contract.get("candidate_spec") or {}
    policy = fold_contract.get("selection_policy") or {}
    if not isinstance(spec, Mapping) or not isinstance(policy, Mapping):
        raise PromotionContractError("Adaptive promotion contract candidate or policy payload is invalid")
    policy_name = str(policy.get("policy_name") or "")
    if policy_name not in LIVE_PORTABLE_SELECTION_POLICIES:
        raise PromotionContractError(
            f"Selection policy {policy_name!r} has no proven live scorer parity implementation"
        )
    configured_policy = str(cfg_get(config, "biotech_scoring.production_baseline.selection_policy", "") or "")
    if policy_name != configured_policy:
        raise PromotionContractError(
            "Promotion contract selection policy does not match active production scoring: "
            f"contract={policy_name!r} configured={configured_policy!r}"
        )
    if not _score_spec_matches_config(spec, config):
        raise PromotionContractError(
            "Promotion contract score weights do not match active production config; apply and version the "
            "authorized score formula before activating adaptive breadth"
        )


def validate_monitoring_contract(payload: Mapping[str, object]) -> None:
    """Require an actionable post-activation monitoring and rollback contract."""
    monitoring = payload.get("monitoring_contract") or {}
    if not isinstance(monitoring, Mapping):
        raise PromotionContractError("Promotion contract monitoring_contract must be a mapping")
    raw_windows = monitoring.get("review_windows_days") or []
    if not isinstance(raw_windows, (list, tuple, set)):
        raise PromotionContractError("Promotion contract review_windows_days must be a list")
    windows: set[int] = set()
    for raw_value in raw_windows:
        parsed = _finite(raw_value)
        if parsed is None or parsed <= 0.0 or not parsed.is_integer():
            raise PromotionContractError("Promotion contract review windows must be positive whole days")
        windows.add(int(parsed))
    if not {30, 60, 90}.issubset(windows):
        raise PromotionContractError("Promotion contract must monitor 30-, 60-, and 90-day windows")
    triggers = monitoring.get("rollback_triggers") or {}
    if not isinstance(triggers, Mapping):
        raise PromotionContractError("Promotion contract rollback_triggers must be a mapping")
    min_dates = int(_finite(triggers.get("min_live_paired_dates")) or 0)
    fallback_rate = _finite(triggers.get("max_policy_fallback_frequency_pct"))
    if min_dates <= 0:
        raise PromotionContractError("Promotion contract requires positive min_live_paired_dates")
    if fallback_rate is None or not 0.0 <= fallback_rate <= 100.0:
        raise PromotionContractError("Promotion contract has invalid fallback-frequency trigger")
    for field_name in ("max_loss20_deterioration_pct", "max_loss40_deterioration_pct"):
        trigger = _finite(triggers.get(field_name))
        if trigger is None or not 0.0 <= trigger <= 100.0:
            raise PromotionContractError(f"Promotion contract has invalid {field_name} trigger")
    has_profitability_evidence = (
        payload.get("profitability_evidence") is not None
        or payload.get("global_portfolio_profitability_decision") is not None
    )
    if has_profitability_evidence:
        for field_name in ("max_drawdown_deterioration_pct", "max_daily_cvar_deterioration_pct"):
            trigger = _finite(triggers.get(field_name))
            if trigger is None or not 0.0 <= trigger <= 100.0:
                raise PromotionContractError(f"Profitability contract has invalid {field_name} trigger")
        verification = payload.get("profitability_replay_verification") or {}
        if not isinstance(verification, Mapping):
            raise PromotionContractError("Profitability replay verification must be a mapping")
        if verification.get("verification_status") != "pass":
            raise PromotionContractError("Profitability replay did not pass independent verification")
        if verification.get("independent_normalized_input_replay") is not True:
            raise PromotionContractError("Profitability replay was not independently reproduced")

    if triggers.get("require_policy_hash_consistency") is not True:
        raise PromotionContractError("Promotion contract must require policy hash consistency")
    if str(monitoring.get("rollback_action") or "") != "xbi_residual_only":
        raise PromotionContractError("Promotion contract rollback_action must be xbi_residual_only")


def validate_cohort_contract(payload: Mapping[str, object]) -> dict[str, ActiveCohortPromotion]:
    """Validate and return only independently authorized cohort challengers."""
    raw_statuses = payload.get("cohort_promotion_status") or {}
    raw_contracts = payload.get("cohort_contracts") or {}
    raw_budgets = payload.get("cohort_budget_weights") or {}
    if not isinstance(raw_statuses, Mapping) or not isinstance(raw_contracts, Mapping):
        raise PromotionContractError("Cohort promotion status and contracts must be mappings")
    expected = set(BIOTECH_CALIBRATION_COHORTS)
    for label, values in (("status", set(raw_statuses)), ("contract", set(raw_contracts))):
        if values != expected:
            raise PromotionContractError(
                f"Cohort {label} set mismatch: missing={sorted(expected - values)} extra={sorted(values - expected)}"
            )
    if not isinstance(raw_budgets, Mapping):
        raise PromotionContractError("Cohort portfolio budget weights must be a mapping")
    try:
        validate_cohort_budget_weights(BIOTECH_CALIBRATION_COHORTS, raw_budgets)
    except ValueError as exc:
        raise PromotionContractError(str(exc)) from exc
    if payload.get("global_portfolio_risk_gate_passed") is not True:
        raise PromotionContractError("Cohort promotion contract did not pass the global portfolio risk gate")
    verification = payload.get("profitability_replay_verification") or {}
    if not isinstance(verification, Mapping) or verification.get("verification_status") != "pass":
        raise PromotionContractError("Cohort profitability replay verification did not pass")

    raw_authorized = payload.get("statistically_and_economically_authorized_cohorts") or []
    if not isinstance(raw_authorized, list):
        raise PromotionContractError("Authorized cohort list must be a list")
    authorized_list = [str(value).strip() for value in raw_authorized if str(value).strip()]
    if len(authorized_list) != len(set(authorized_list)):
        raise PromotionContractError("Authorized cohort list contains duplicates")
    unknown = sorted(set(authorized_list) - expected)
    if unknown:
        raise PromotionContractError(f"Authorized cohort list contains unknown cohorts: {unknown}")
    status_authorized = {
        cohort
        for cohort in BIOTECH_CALIBRATION_COHORTS
        if isinstance(raw_statuses.get(cohort), Mapping)
        and raw_statuses[cohort].get("cohort_promotion_authorized") is True
    }
    if set(authorized_list) != status_authorized:
        raise PromotionContractError(
            "Authorized cohort list does not match cohort_promotion_status authorization flags"
        )
    if not status_authorized:
        raise PromotionContractError("Cohort promotion contract has no independently authorized challenger")

    active: dict[str, ActiveCohortPromotion] = {}
    for cohort in BIOTECH_CALIBRATION_COHORTS:
        status = raw_statuses[cohort]
        cohort_contract = raw_contracts[cohort]
        if not isinstance(status, Mapping) or not isinstance(cohort_contract, Mapping):
            raise PromotionContractError(f"Invalid cohort contract metadata for cohort={cohort}")
        fold = cohort_contract.get("latest_primary_fold_contract") or {}
        if not isinstance(fold, Mapping):
            raise PromotionContractError(f"Missing latest fold contract for cohort={cohort}")
        if cohort not in status_authorized:
            continue
        promotion = _active_policy_from_fold(fold, cohort=cohort)
        if str(status.get("candidate_id") or "").strip() != promotion.candidate_id:
            raise PromotionContractError(f"Candidate id mismatch for cohort={cohort}")
        if str(status.get("selection_policy_name") or "").strip() != promotion.selection_policy_name:
            raise PromotionContractError(f"Selection policy mismatch for cohort={cohort}")
        raw_pre = promotion.policy_payload.get("allowed_primary_cohorts", [])
        raw_post = promotion.policy_payload.get("post_selection_allowed_primary_cohorts", [])
        if not isinstance(raw_pre, (list, tuple, set)) or not isinstance(raw_post, (list, tuple, set)):
            raise PromotionContractError(f"Selection policy cohort allowlists are invalid for cohort={cohort}")
        pre = {str(value).strip() for value in raw_pre}
        post = {
            str(value).strip()
            for value in raw_post
        }
        if (pre and cohort not in pre) or (post and cohort not in post):
            raise PromotionContractError(f"Selection policy cannot select its calibrated cohort={cohort}")
        active[cohort] = promotion
    return active


def load_active_promotion_contract(
    config: dict[str, Any],
    *,
    base_dir: Path,
) -> ActivePromotionContract | None:
    settings = cfg_get(config, "biotech_scoring.adaptive_promotion_contract", {}) or {}
    if not isinstance(settings, Mapping):
        raise PromotionContractError("biotech_scoring.adaptive_promotion_contract must be a mapping")
    enabled = str(settings.get("enabled") or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}
    if not enabled:
        return None
    raw_path = str(settings.get("path") or "").strip()
    if not raw_path:
        raise PromotionContractError("Enabled adaptive promotion contract has no path")
    path = Path(raw_path).expanduser()
    path = path if path.is_absolute() else (base_dir / path).resolve()
    if not path.exists():
        raise PromotionContractError(f"Adaptive promotion contract does not exist: {path}")
    actual_sha = sha256_file(path)
    expected_sha = str(settings.get("sha256") or "").strip().lower()
    if not expected_sha:
        raise PromotionContractError("Enabled adaptive promotion contract requires a pinned sha256")
    if actual_sha.lower() != expected_sha:
        raise PromotionContractError(
            f"Adaptive promotion contract hash mismatch: expected={expected_sha} actual={actual_sha}"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise PromotionContractError("Adaptive promotion contract root must be a JSON object")
    contract_version = str(payload.get("contract_version") or "")
    if contract_version not in SUPPORTED_CONTRACT_VERSIONS:
        raise PromotionContractError(f"Unsupported adaptive promotion contract: {payload.get('contract_version')!r}")
    if payload.get("production_promotion_authorized") is not True:
        raise PromotionContractError("Adaptive promotion contract is not authorized for production")
    if str(payload.get("activation_status") or "") != "active":
        raise PromotionContractError("Adaptive promotion contract must have activation_status='active'")
    contract_id = str(payload.get("contract_id") or "").strip()
    if not contract_id:
        raise PromotionContractError("Active adaptive promotion contract requires an immutable contract_id")
    validate_monitoring_contract(payload)
    raw_effective_date = str(payload.get("effective_date") or "").strip()
    try:
        effective_date = date.fromisoformat(raw_effective_date)
    except ValueError as exc:
        raise PromotionContractError("Adaptive promotion contract requires an ISO effective_date") from exc
    if contract_version == SUPPORTED_COHORT_CONTRACT_VERSION:
        active_cohorts = validate_cohort_contract(payload)
        return ActivePromotionContract(
            path=path,
            sha256=actual_sha,
            effective_date=effective_date,
            contract_id=contract_id,
            candidate_id="cohort_specific",
            candidate_name="cohort_specific_promotions",
            selection_policy_name="cohort_specific",
            candidate_pool_top_n=0,
            min_score_pct_of_top=0.0,
            max_names=0,
            reliability_class="cohort_specific",
            active_weight=1.0,
            xbi_residual_weight=0.0,
            policy_payload={},
            max_name_weight=1.0,
            contract_version=contract_version,
            cohort_policies=active_cohorts,
        )
    fold_contract = payload.get("latest_primary_fold_contract") or {}
    if not isinstance(fold_contract, dict):
        raise PromotionContractError("Adaptive promotion contract lacks latest_primary_fold_contract")
    validate_contract_scoring_parity(payload, config)
    spec = fold_contract.get("candidate_spec") or {}
    policy = fold_contract.get("selection_policy") or {}
    threshold = fold_contract.get("threshold") or {}
    if not isinstance(spec, Mapping) or not isinstance(policy, Mapping) or not isinstance(threshold, Mapping):
        raise PromotionContractError("Adaptive promotion contract candidate, policy, or threshold payload is invalid")
    promotion = _active_policy_from_fold(fold_contract, cohort="", validate_formula=False)
    return ActivePromotionContract(
        path=path,
        sha256=actual_sha,
        effective_date=effective_date,
        contract_id=contract_id,
        candidate_id=promotion.candidate_id,
        candidate_name=promotion.candidate_name,
        selection_policy_name=promotion.selection_policy_name,
        candidate_pool_top_n=promotion.candidate_pool_top_n,
        min_score_pct_of_top=promotion.min_score_pct_of_top,
        max_names=promotion.max_names,
        reliability_class=promotion.reliability_class,
        active_weight=promotion.active_weight,
        xbi_residual_weight=promotion.xbi_residual_weight,
        policy_payload=promotion.policy_payload,
        max_name_weight=promotion.max_name_weight,
        contract_version=contract_version,
        score_spec=promotion.score_spec,
    )

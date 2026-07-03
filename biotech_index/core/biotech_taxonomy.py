from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from typing import Any, Mapping

from biotech_index.core.constants import GOING_CONCERN_HARD_STATUSES, GOING_CONCERN_SOFT_STATUSES

MODEL_VERSION = "biotech_cohort_v4_official_five_bucket"

SERVICES_DIAGNOSTICS_TOOLS_CATEGORIES = frozenset(
    {
        "clinical_research_services",
        "diagnostics_services",
        "molecular_diagnostics",
        "molecular_diagnostics_laboratory",
        "precision_medicine_diagnostics",
        "life_science_tools_services",
        "sterilization_services",
    }
)

MEDTECH_DEVICE_CATEGORIES = frozenset(
    {
        "diabetes_device",
        "medical_device_infrastructure",
        "medical_device_therapeutic_platform",
        "post_market_device_services",
        "surgical_regenerative_devices",
        "surgical_robotics_device",
    }
)

NON_THERAPEUTIC_CATEGORIES = SERVICES_DIAGNOSTICS_TOOLS_CATEGORIES | MEDTECH_DEVICE_CATEGORIES | frozenset(
    {
        "non_therapeutic_device_removed",
    }
)


@dataclass(frozen=True)
class CohortClassification:
    primary_cohort: str
    secondary_cohort: str
    reason_codes: list[str]
    confidence: float
    margin: float
    source: str
    overlays: list[str]
    data_quality: str
    taxonomy_review_required: bool
    sparse_data_flag: bool
    calibration_weight: float
    model_version: str
    evidence: dict[str, Any]

    def as_payload(self) -> dict[str, Any]:
        return {
            "biotech_primary_cohort": self.primary_cohort,
            "biotech_secondary_cohort": self.secondary_cohort,
            "biotech_cohort_reason_codes": "|".join(self.reason_codes),
            "biotech_cohort_confidence": round(self.confidence, 4),
            "biotech_cohort_margin": round(self.margin, 4),
            "biotech_cohort_source": self.source,
            "biotech_cohort_overlays": "|".join(self.overlays),
            "biotech_cohort_data_quality": self.data_quality,
            "biotech_taxonomy_review_required": 1.0 if self.taxonomy_review_required else 0.0,
            "biotech_cohort_sparse_data_flag": 1.0 if self.sparse_data_flag else 0.0,
            "biotech_cohort_calibration_weight": round(self.calibration_weight, 4),
            "biotech_cohort_model_version": self.model_version,
            "biotech_cohort_evidence_json": json.dumps(self.evidence, ensure_ascii=True, sort_keys=True),
        }


def apply_manual_taxonomy_override(
    classification: CohortClassification,
    *,
    primary_cohort: str | None = None,
    secondary_cohort: str | None = None,
    confidence: object = None,
    reason_codes_add: list[str] | None = None,
    overlays_add: list[str] | None = None,
    source: str = "manual_taxonomy_override",
    note: str = "",
) -> CohortClassification:
    override_confidence = _to_float(confidence, math.nan)
    final_confidence = (
        _clamp(override_confidence)
        if math.isfinite(override_confidence)
        else classification.confidence
    )
    final_primary = primary_cohort or classification.primary_cohort
    final_secondary = secondary_cohort if secondary_cohort is not None else classification.secondary_cohort
    reason_codes = sorted(set(classification.reason_codes + (reason_codes_add or []) + ["manual_taxonomy_override"]))
    overlays = sorted(set(classification.overlays + (overlays_add or [])))
    data_quality = "review" if final_confidence < 75.0 else "medium" if final_confidence < 85.0 else "high"
    calibration_weight = 1.0 if final_confidence >= 85.0 else 0.5 if final_confidence >= 75.0 else 0.0
    taxonomy_review_required = final_confidence < 75.0 or final_primary == "unmapped_calibration_cohort"
    evidence = dict(classification.evidence)
    evidence["manual_override"] = {
        "primary_cohort": primary_cohort or "",
        "secondary_cohort": secondary_cohort or "",
        "confidence": final_confidence,
        "reason_codes_add": reason_codes_add or [],
        "overlays_add": overlays_add or [],
        "source": source,
        "note": note,
    }
    return replace(
        classification,
        primary_cohort=final_primary,
        secondary_cohort=final_secondary,
        reason_codes=reason_codes,
        confidence=final_confidence,
        source=source,
        overlays=overlays,
        data_quality=data_quality,
        taxonomy_review_required=taxonomy_review_required,
        calibration_weight=calibration_weight,
        evidence=evidence,
    )


def _to_float(raw: object, default: float = 0.0) -> float:
    if raw is None:
        return default
    text = str(raw).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return default
    try:
        value = float(text)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return value


def _as_bool(raw: object) -> bool:
    # Mirrors commercial_risk._bool_numeric: numeric inputs (1.0, "1.0") are
    # truthy when > 0, alongside the usual boolean string tokens.
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        token = raw.strip().lower()
        if token in {"1", "true", "yes", "y", "on"}:
            return True
        if token in {"0", "false", "no", "n", "off", ""}:
            return False
    return _to_float(raw, 0.0) > 0.0


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    if not math.isfinite(value):
        return lo
    return max(lo, min(hi, value))


def _submap(mapping: Mapping[str, Any] | None, key: str) -> Mapping[str, Any]:
    if not isinstance(mapping, Mapping):
        return {}
    value = mapping.get(key)
    return value if isinstance(value, Mapping) else {}


def classify_biotech_cohort(
    *,
    payload: Mapping[str, Any],
    commercial: Mapping[str, Any] | None = None,
    forward_guidance: Mapping[str, Any] | None = None,
    diagnostics: Mapping[str, Any] | None = None,
) -> CohortClassification:
    commercial = commercial or {}
    forward_guidance = forward_guidance or {}
    diagnostics = diagnostics or {}
    ctgov = _submap(payload, "ctgov")
    survival = _submap(payload, "financial_survival")
    sec_liq = _submap(payload, "sec_and_liquidity")
    sec_events = _submap(payload, "sec_events")

    strategy_category = str(payload.get("company_strategy_category") or "").strip().lower()
    evidence_type = str(ctgov.get("ctgov_evidence_type") or "").strip().lower()
    review_bucket = str(ctgov.get("review_bucket") or "").strip().lower()

    ttm_revenue = _to_float(commercial.get("ttm_revenue"), 0.0)
    revenue_growth = _to_float(commercial.get("revenue_yoy_growth_pct"), math.nan)
    commercial_stage = _as_bool(commercial.get("commercial_stage_flag")) or ttm_revenue >= 50_000_000.0
    profitable = _as_bool(commercial.get("profitable_flag"))
    forward_profitability = _as_bool(forward_guidance.get("forward_profitability_flag"))
    value_trap_score = _to_float(commercial.get("value_trap_score"), 0.0)
    leverage_fragility_score = _to_float(diagnostics.get("leverage_fragility_score"), 0.0)
    mature_defensive_score = _to_float(diagnostics.get("mature_defensive_score"), 0.0)
    commercial_deterioration_score = _to_float(diagnostics.get("commercial_deterioration_score"), 0.0)
    valuation_growth_mismatch_score = _to_float(diagnostics.get("valuation_growth_mismatch_score"), 0.0)
    commercial_business_shock_score = _to_float(diagnostics.get("commercial_business_shock_score"), 0.0)

    verified_active = _to_float(ctgov.get("verified_qualifying_active_trial_count"), 0.0)
    active_lead = _to_float(ctgov.get("active_lead_sponsor_trials"), 0.0)
    active_program = _to_float(ctgov.get("active_program_override_trials"), 0.0)
    active_collab = _to_float(ctgov.get("active_collaborator_trials"), 0.0)
    active_phase2 = _to_float(ctgov.get("active_phase2_trials"), 0.0)
    active_phase3 = _to_float(ctgov.get("active_phase3_trials"), 0.0)
    active_pivotal = _to_float(ctgov.get("active_pivotal_trials"), 0.0)
    lead_phase23 = _to_float(ctgov.get("lead_phase2_3_active_trials"), 0.0)
    program_phase23 = _to_float(ctgov.get("program_phase2_3_active_trials"), 0.0)
    collaborator_phase23 = _to_float(ctgov.get("collaborator_phase2_3_active_trials"), 0.0)
    collaborator_ratio = _to_float(ctgov.get("collaborator_dependency_ratio"), 0.0)
    collaborator_heavy = _as_bool(ctgov.get("collaborator_heavy_flag")) or collaborator_ratio >= 0.60
    core_pipeline_quality = _to_float(ctgov.get("core_pipeline_quality_score"), 0.0)

    sec_counts = sec_events.get("counts") if isinstance(sec_events.get("counts"), Mapping) else {}
    pdufa_count = _to_float(sec_counts.get("pdufa_date") if isinstance(sec_counts, Mapping) else None, 0.0)
    nda_bla_count = _to_float(sec_counts.get("nda_bla_accepted") if isinstance(sec_counts, Mapping) else None, 0.0)
    regulatory_count = _to_float(sec_events.get("regulatory_catalyst_count"), 0.0)
    dilution_count = _to_float(sec_events.get("dilution_event_count"), 0.0)
    negative_clinical_count = _to_float(sec_events.get("negative_clinical_event_count"), 0.0)
    partnership_count = _to_float(sec_events.get("partnership_event_count"), 0.0)
    latest_event_type = str(sec_events.get("sec_catalyst_latest_event_type") or "").strip().lower()
    latest_event_days = _to_float(sec_events.get("sec_catalyst_recency_days"), math.nan)

    cash_runway = _to_float(survival.get("cash_runway_months"), math.nan)
    severe_runway = _as_bool(survival.get("severe_runway_flag"))
    short_runway = _as_bool(survival.get("short_runway_flag")) or (math.isfinite(cash_runway) and 0.0 < cash_runway < 12.0)
    going_status = str(sec_liq.get("going_concern_status") or survival.get("going_concern_status") or "").strip().lower()
    median_addv20 = _to_float(sec_liq.get("median_addv20"), 0.0)
    survival_quality = str(survival.get("data_quality") or "").strip().lower()
    guidance_recency_penalty = _to_float(forward_guidance.get("guidance_recency_penalty"), 0.0)
    no_forward_guidance = not str(forward_guidance.get("latest_guidance_filing_date") or "").strip()

    services_tools_score = 95.0 if strategy_category in SERVICES_DIAGNOSTICS_TOOLS_CATEGORIES else 0.0
    medtech_device_score = 95.0 if strategy_category in MEDTECH_DEVICE_CATEGORIES else 0.0
    non_therapeutic_score = 95.0 if strategy_category == "non_therapeutic_device_removed" else 0.0
    late_score = _clamp(active_pivotal * 35.0 + active_phase3 * 25.0 + pdufa_count * 35.0 + nda_bla_count * 30.0 + regulatory_count * 12.0)
    phase2_score = _clamp((lead_phase23 + program_phase23) * 22.0 + active_phase2 * 12.0)
    early_score = _clamp(verified_active * 18.0 - (lead_phase23 + program_phase23 + active_phase3 + active_pivotal) * 10.0)
    platform_score = _clamp(max(0.0, verified_active - 1.0) * 14.0 + core_pipeline_quality * 0.35)
    partnered_score = _clamp(
        (35.0 if collaborator_heavy else 0.0)
        + active_collab * 12.0
        + collaborator_phase23 * 16.0
        + partnership_count * 18.0
        - (lead_phase23 + program_phase23 + active_pivotal) * 8.0
    )
    commercial_base = 70.0 if commercial_stage else 0.0
    commercial_growth_bonus = 12.0 if math.isfinite(revenue_growth) and revenue_growth >= 0.10 else 0.0
    commercial_fragility = max(
        commercial_deterioration_score,
        commercial_business_shock_score,
        valuation_growth_mismatch_score,
        value_trap_score,
    )
    commercial_fragile_score = _clamp(commercial_base + commercial_fragility * 0.45 + (20.0 if math.isfinite(revenue_growth) and revenue_growth < -0.05 else 0.0))
    commercial_profit_growth_score = _clamp(
        commercial_base
        + (22.0 if profitable or forward_profitability else 0.0)
        + commercial_growth_bonus
        - commercial_fragility * 0.20
    )
    commercial_profit_mature_score = _clamp(
        commercial_base
        + (18.0 if profitable or forward_profitability else 0.0)
        + (10.0 if mature_defensive_score >= 45.0 else 0.0)
        - max(0.0, revenue_growth if math.isfinite(revenue_growth) else 0.0) * 30.0
    )
    commercial_unprof_growth_score = _clamp(
        commercial_base
        + (24.0 if not profitable and math.isfinite(revenue_growth) and revenue_growth > 0.0 else 0.0)
        - commercial_fragility * 0.25
    )
    weak_pipeline_score = _clamp(
        80.0
        if not commercial_stage and verified_active <= 0.0
        else 35.0
        if evidence_type in {"true_historical_only", "weak_alias_match"} or review_bucket in {"historical_only", "weak_alias_match"}
        else 0.0
    )

    commercial_quality_score = max(
        commercial_profit_growth_score if profitable and commercial_profit_growth_score >= 55.0 else 0.0,
        commercial_profit_mature_score if profitable and commercial_profit_mature_score >= 55.0 else 0.0,
        services_tools_score,
        medtech_device_score,
        non_therapeutic_score,
    )
    commercial_turnaround_score = max(
        commercial_unprof_growth_score if commercial_stage and not profitable else 0.0,
        commercial_fragile_score if commercial_stage and commercial_fragility >= 45.0 else 0.0,
    )
    late_registrational_score = late_score
    platform_partnered_score = max(
        partnered_score if partnered_score >= 55.0 else 0.0,
        platform_score if late_score < 55.0 and phase2_score < 55.0 else 0.0,
    )
    early_speculative_score = max(
        phase2_score if late_score < 55.0 else 0.0,
        early_score if late_score < 55.0 and phase2_score < 55.0 else 0.0,
        weak_pipeline_score,
    )
    candidates = {
        "commercial_profitable_quality_or_mature": commercial_quality_score,
        "commercial_turnaround_or_unprofitable_growth": commercial_turnaround_score,
        "late_clinical_pivotal_or_registrational": late_registrational_score,
        "platform_partnered_modality_pipeline": platform_partnered_score,
        "early_clinical_speculative_or_single_asset_pipeline": early_speculative_score,
    }
    raw_candidates = dict(candidates)
    commercial_candidate_keys = (
        "commercial_profitable_quality_or_mature",
        "commercial_turnaround_or_unprofitable_growth",
    )
    best_commercial_key, best_commercial_score = max(
        ((key, candidates.get(key, 0.0)) for key in commercial_candidate_keys),
        key=lambda item: item[1],
    )
    # Primary cohort should reflect the dominant economic return driver.  For a
    # company with meaningful commercial revenue, active Phase 3/PDUFA evidence
    # is usually an overlay unless the pipeline asset clearly dominates value.
    has_material_commercial_anchor = commercial_stage and (
        best_commercial_score >= 55.0
        or ttm_revenue >= 50_000_000.0
        or profitable
        or forward_profitability
    )
    has_pipeline_catalyst = late_score >= 55.0 or phase2_score >= 55.0
    commercial_anchor_is_dominant = (
        has_material_commercial_anchor
        and (profitable or forward_profitability or ttm_revenue >= 100_000_000.0)
        and commercial_fragility < 75.0
    )
    pipeline_clearly_dominates = (
        has_pipeline_catalyst
        and late_score >= best_commercial_score + 35.0
        and not (profitable or forward_profitability)
        and (ttm_revenue < 150_000_000.0 or commercial_fragility >= 75.0)
    )
    if commercial_anchor_is_dominant and not pipeline_clearly_dominates:
        # Primary cohort should describe the dominant economic return driver.
        # Commercial companies can still carry late-clinical overlays, but a
        # catalyst should not automatically outrank a real commercial anchor.
        cap = max(0.0, best_commercial_score - 1.0)
        candidates["late_clinical_pivotal_or_registrational"] = min(
            candidates["late_clinical_pivotal_or_registrational"],
            cap,
        )
        candidates["platform_partnered_modality_pipeline"] = min(
            candidates["platform_partnered_modality_pipeline"],
            max(0.0, best_commercial_score - 2.0),
        )

    ordered = sorted(candidates.items(), key=lambda item: item[1], reverse=True)
    primary, top_score = ordered[0]
    secondary, second_score = ordered[1] if len(ordered) > 1 else ("", 0.0)
    if top_score < 55.0:
        primary, top_score = "unmapped_calibration_cohort", 50.0
    margin = max(0.0, top_score - second_score)

    reason_codes: list[str] = []
    if strategy_category:
        reason_codes.append(f"strategy_category:{strategy_category}")
    if commercial_stage:
        reason_codes.append("commercial_stage_or_revenue_anchor")
    if commercial_anchor_is_dominant and has_pipeline_catalyst and not pipeline_clearly_dominates:
        reason_codes.append("commercial_anchor_primary_pipeline_overlay")
    if strategy_category in SERVICES_DIAGNOSTICS_TOOLS_CATEGORIES:
        reason_codes.append("services_diagnostics_tools_strategy_category")
    if strategy_category in MEDTECH_DEVICE_CATEGORIES:
        reason_codes.append("medtech_device_strategy_category")
    if profitable:
        reason_codes.append("profitable_flag")
    if math.isfinite(revenue_growth):
        reason_codes.append("revenue_growth_available")
    if active_pivotal > 0:
        reason_codes.append("active_pivotal_count_gt_0")
    if active_phase3 > 0:
        reason_codes.append("active_phase3_count_gt_0")
    if lead_phase23 > 0 or program_phase23 > 0:
        reason_codes.append("active_phase2_3_owner_count_gt_0")
    if verified_active > 0:
        reason_codes.append("verified_active_trial_count_gt_0")
    if collaborator_heavy:
        reason_codes.append("collaborator_dependency_high")
    if partnership_count > 0:
        reason_codes.append("sec_partnership_event_count_gt_0")
    if weak_pipeline_score >= 55.0:
        reason_codes.append("weak_or_no_active_pipeline_evidence")

    overlays: list[str] = []
    if median_addv20 > 0.0 and median_addv20 < 1_000_000.0:
        overlays.append("low_liquidity")
    if short_runway or severe_runway:
        overlays.append("short_runway")
    if going_status in GOING_CONCERN_HARD_STATUSES or going_status in GOING_CONCERN_SOFT_STATUSES:
        overlays.append("going_concern")
    if dilution_count > 0:
        overlays.append("recent_dilution")
    if collaborator_heavy:
        overlays.append("collaborator_heavy")
    if partnered_score >= 55.0 or partnership_count > 0:
        overlays.append("royalty_or_partnered_economics")
    if commercial_stage and (late_score >= 55.0 or phase2_score >= 55.0):
        overlays.append("commercial_with_major_pipeline_catalyst")
    if commercial_anchor_is_dominant and has_pipeline_catalyst and not pipeline_clearly_dominates:
        overlays.append("late_clinical_overlay")
    if (late_score >= 55.0 or phase2_score >= 55.0) and not commercial_stage:
        overlays.append("pipeline_catalyst_dominant")
    if verified_active <= 1.0 and not commercial_stage and primary != "unmapped_calibration_cohort":
        overlays.append("single_asset_risk")
    if pdufa_count > 0 or (latest_event_type == "pdufa_date" and math.isfinite(latest_event_days) and latest_event_days <= 180):
        overlays.append("pdufa_within_180d")
    if active_phase3 > 0:
        overlays.append("phase3_readout_within_180d")
    if active_phase2 > 0:
        overlays.append("phase2_readout_within_180d")
    if commercial_deterioration_score >= 50.0:
        overlays.append("commercial_deterioration")
    if valuation_growth_mismatch_score >= 50.0:
        overlays.append("valuation_growth_mismatch")
    if value_trap_score >= 50.0:
        overlays.append("value_trap")
    if leverage_fragility_score >= 50.0:
        overlays.append("leverage_fragility")
    if mature_defensive_score >= 60.0:
        overlays.append("mature_defensive_low_expected_return")
    if no_forward_guidance and commercial_stage:
        overlays.append("no_forward_guidance")
    if guidance_recency_penalty > 0.0:
        overlays.append("stale_guidance")
    if survival_quality in {"low", "poor", "stale"}:
        overlays.append("data_quality_low")
    if negative_clinical_count > 0:
        overlays.append("safety_or_negative_clinical_signal")

    conflict_penalty = 10.0 if margin < 12.0 and secondary else 0.0
    data_quality_bonus = 5.0 if survival_quality not in {"low", "poor", "stale"} else -8.0
    confidence = _clamp(top_score + margin * 0.15 + data_quality_bonus - conflict_penalty)
    if commercial_anchor_is_dominant and has_pipeline_catalyst:
        confidence = min(confidence, 88.0)
    if primary == "commercial_profitable_quality_or_mature" and has_pipeline_catalyst:
        confidence = min(confidence, 86.0)
    if primary == "unmapped_calibration_cohort":
        confidence = min(confidence, 60.0)
    data_quality = "review" if confidence < 75.0 else "medium" if confidence < 85.0 else "high"
    calibration_weight = 1.0 if confidence >= 85.0 else 0.5 if confidence >= 75.0 else 0.0
    taxonomy_review_required = confidence < 75.0 or primary == "unmapped_calibration_cohort"

    evidence = {
        "cohort_scores": {key: round(value, 4) for key, value in sorted(candidates.items())},
        "raw_cohort_scores": {key: round(value, 4) for key, value in sorted(raw_candidates.items())},
        "commercial_stage": commercial_stage,
        "profitable": profitable,
        "ttm_revenue": ttm_revenue,
        "revenue_yoy_growth_pct": revenue_growth if math.isfinite(revenue_growth) else "",
        "commercial_fragility_score": round(commercial_fragility, 4),
        "verified_active_trial_count": verified_active,
        "active_lead_sponsor_trials": active_lead,
        "active_program_override_trials": active_program,
        "active_phase2_trials": active_phase2,
        "active_phase3_trials": active_phase3,
        "active_pivotal_trials": active_pivotal,
        "lead_phase2_3_active_trials": lead_phase23,
        "program_phase2_3_active_trials": program_phase23,
        "collaborator_phase2_3_active_trials": collaborator_phase23,
        "collaborator_dependency_ratio": collaborator_ratio,
        "ctgov_evidence_type": evidence_type,
        "strategy_category": strategy_category,
        "commercial_anchor_is_dominant": commercial_anchor_is_dominant,
        "pipeline_clearly_dominates": pipeline_clearly_dominates,
        "best_commercial_cohort": best_commercial_key,
        "best_commercial_score": round(best_commercial_score, 4),
    }
    return CohortClassification(
        primary_cohort=primary,
        secondary_cohort=secondary,
        reason_codes=sorted(set(reason_codes)),
        confidence=confidence,
        margin=margin,
        source="biotech_taxonomy_rule_evidence_v1",
        overlays=sorted(set(overlays)),
        data_quality=data_quality,
        taxonomy_review_required=taxonomy_review_required,
        sparse_data_flag=False,
        calibration_weight=calibration_weight,
        model_version=MODEL_VERSION,
        evidence=evidence,
    )

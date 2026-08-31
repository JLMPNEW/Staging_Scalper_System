from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .config import ConfigBundle, cfg_get
from .scoring_features import CORE_COMPONENT_SPECS
from .stage8_calibration import Candidate, _finite


STAGE8_ERA_QUALITY_V2 = 'consumer_defensive_stage8_era_quality_v2'

COMPONENT_SOURCE_KEYS = {
    'insider_net_buying': 'sec_form4',
    'institutional_flow': 'institutional_13f',
    'short_float_pct': 'short_interest',
    'short_days_to_cover': 'short_interest',
    'borrow_fee': 'borrow',
}


@dataclass(frozen=True)
class EraAdjustedEligibility:
    eligible: bool
    rank_ready: bool
    absolute_available_weight: float
    absolute_missing_weight: float
    structural_missing_weight: float
    observable_weight: float
    observable_available_fraction: float
    observable_missing_fraction: float
    structural_components: tuple[str, ...]
    reasons: tuple[str, ...]


def structural_components_for_date(
    *,
    as_of: str,
    bundle: ConfigBundle,
) -> set[str]:
    """Return components whose declared upstream source did not yet exist."""

    output: set[str] = set()
    for component, source_key in COMPONENT_SOURCE_KEYS.items():
        birth = str(cfg_get(
            bundle.payload, f'positioning.source_birthdates.{source_key}'
        ))
        if as_of < birth:
            output.add(component)
    return output


def era_adjusted_baseline_eligibility(
    row: Mapping[str, Any],
    baseline: Candidate,
    bundle: ConfigBundle,
) -> EraAdjustedEligibility:
    """Apply quality gates over observable components without reweighting.

    Structural pre-birth components remain at the frozen neutral score and
    retain their fixed economic weights.  Only the *quality denominator* is
    adjusted, because it is impossible for an issuer to supply data before a
    source exists.  This avoids both redistribution and an era-driven false
    negative.
    """

    as_of = str(row['asof_date'])
    structural = structural_components_for_date(as_of=as_of, bundle=bundle)
    scores = row['_component_scores']
    quality = row['_component_quality']
    usable = {
        spec.name
        for spec in CORE_COMPONENT_SPECS
        if spec.name not in structural
        and float(quality.get(spec.name, 0.0)) > 0.0
        and _finite(scores.get(spec.name)) is not None
    }
    reasons = [
        f'missing_required:{spec.name}'
        for spec in CORE_COMPONENT_SPECS
        if spec.rank_requirement == 'required'
        and spec.name not in structural
        and spec.name not in usable
    ]
    financial_names = {
        spec.name for spec in CORE_COMPONENT_SPECS
        if spec.rank_requirement == 'any_financial'
    }
    if not (financial_names & usable):
        reasons.append('missing_requirement:any_financial')
    observable_short_names = {
        spec.name for spec in CORE_COMPONENT_SPECS
        if spec.rank_requirement == 'any_short'
        and spec.name not in structural
    }
    if observable_short_names and not (observable_short_names & usable):
        reasons.append('missing_requirement:any_short')

    total_weight = sum(baseline.core_weights.values())
    structural_weight = sum(
        baseline.core_weights.get(name, 0.0) for name in structural
    )
    observable_weight = total_weight - structural_weight
    if observable_weight <= 0.0:
        raise RuntimeError('No observable Stage 8 components remain.')
    absolute_available = sum(
        baseline.core_weights.get(name, 0.0) for name in usable
    )
    absolute_missing = total_weight - absolute_available
    observable_available = absolute_available / observable_weight
    observable_missing = (
        observable_weight - absolute_available
    ) / observable_weight
    minimum_quality = float(cfg_get(
        bundle.payload, 'stage7_scoring.minimum_data_quality_confidence'
    ))
    maximum_missing = float(cfg_get(
        bundle.payload, 'stage7_scoring.maximum_missing_component_weight'
    ))
    if observable_available < minimum_quality:
        reasons.append(
            f'low_observable_data_quality={observable_available:.6f}'
        )
    if observable_missing > maximum_missing:
        reasons.append(
            f'observable_missing_component_weight={observable_missing:.6f}'
        )
    rank_ready = not reasons
    membership = int(row.get('membership_eligible_flag', 1)) == 1
    investable = int(row.get('investable_flag', 1)) == 1
    return EraAdjustedEligibility(
        eligible=rank_ready and membership and investable,
        rank_ready=rank_ready,
        absolute_available_weight=absolute_available,
        absolute_missing_weight=absolute_missing,
        structural_missing_weight=structural_weight,
        observable_weight=observable_weight,
        observable_available_fraction=observable_available,
        observable_missing_fraction=observable_missing,
        structural_components=tuple(sorted(structural)),
        reasons=tuple(sorted(reasons)),
    )


def prepare_era_adjusted_panel(
    rows: Sequence[Mapping[str, Any]],
    baseline: Candidate,
    bundle: ConfigBundle,
    *,
    neutral_score: float | None = None,
) -> list[dict[str, Any]]:
    """Create an executable panel with neutral structural components."""

    neutral = (
        float(neutral_score) if neutral_score is not None
        else float(cfg_get(bundle.payload, 'stage7_scoring.neutral_score'))
    )
    output: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        row['_component_scores'] = dict(source['_component_scores'])
        row['_component_quality'] = dict(source['_component_quality'])
        structural = structural_components_for_date(
            as_of=str(row['asof_date']), bundle=bundle
        )
        for component in structural:
            row['_component_scores'][component] = neutral
            row['_component_quality'][component] = 0.0
        result = era_adjusted_baseline_eligibility(row, baseline, bundle)
        row['baseline_rank_ready_flag'] = int(result.rank_ready)
        row['calibration_eligible_flag'] = int(result.eligible)
        row['available_weight'] = result.absolute_available_weight
        row['missing_weight'] = result.absolute_missing_weight
        row['structural_missing_weight'] = result.structural_missing_weight
        row['observable_weight'] = result.observable_weight
        row['observable_available_fraction'] = (
            result.observable_available_fraction
        )
        row['observable_missing_fraction'] = (
            result.observable_missing_fraction
        )
        row['structural_components'] = result.structural_components
        row['era_quality_review_reason'] = ';'.join(result.reasons)
        output.append(row)
    return output

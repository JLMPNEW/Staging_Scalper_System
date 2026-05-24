from __future__ import annotations

import math
from typing import Any, Mapping

from biotech_index.core.scoring_math import clamp as _clamp


def _to_float(raw: object, default: float | None = None) -> float | None:
    if raw is None:
        return default
    if isinstance(raw, bool):
        raw = int(raw)
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _setting(settings: Mapping[str, Any], key: str, default: float) -> float:
    value = _to_float(settings.get(key), default)
    return default if value is None else value


def _bool_numeric(raw: object) -> bool:
    if isinstance(raw, str):
        token = raw.strip().lower()
        if token in {"1", "true", "t", "yes", "y", "enabled", "on"}:
            return True
        if token in {"0", "false", "f", "no", "n", "disabled", "off", ""}:
            return False
    return (_to_float(raw, 0.0) or 0.0) > 0.0


def _decline_score(value: float | None, *, moderate: float, severe: float) -> float:
    if moderate <= severe:
        raise ValueError(f"Decline-score thresholds must satisfy moderate > severe, got {moderate=} {severe=}")
    if value is None or value >= moderate:
        return 0.0
    if value <= severe:
        return 100.0
    denominator = max(1e-9, moderate - severe)
    return _clamp(100.0 * (moderate - value) / denominator)


def _scale_above(value: float | None, *, threshold: float, severe: float) -> float:
    if value is None or value <= threshold:
        return 0.0
    if value >= severe:
        return 100.0
    return _clamp(100.0 * (value - threshold) / max(1e-9, severe - threshold))


def commercial_risk_overlay_fields(
    commercial: Mapping[str, Any],
    governance: Mapping[str, Any] | None = None,
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return data-derived commercial risk diagnostics for scoring/calibration.

    The signals are intentionally generic. They identify observable patterns such
    as revenue deterioration, expensive low-growth setups, and transient revenue
    anchors without naming individual tickers.
    """

    settings = settings or {}
    governance = governance or {}
    yoy = _to_float(commercial.get("revenue_yoy_growth_pct"))
    qoq = _to_float(commercial.get("revenue_qoq_growth_pct"))
    revenue_growth_score = _to_float(commercial.get("revenue_growth_score"))
    valuation_score = _to_float(commercial.get("valuation_score"))
    upside_capacity_score = _to_float(commercial.get("upside_capacity_score"))
    gross_margin = _to_float(commercial.get("gross_margin_pct"))
    fcf_yield = _to_float(commercial.get("fcf_yield"))
    pe_ratio = _to_float(commercial.get("pe_ratio"))
    ev_to_sales = _to_float(commercial.get("ev_to_sales"))
    price_to_sales = _to_float(commercial.get("price_to_sales"))
    ttm_revenue = _to_float(commercial.get("ttm_revenue"), 0.0) or 0.0
    commercial_fragility = _to_float(governance.get("commercial_fragility_risk_score"))
    commercial_stage = _bool_numeric(commercial.get("commercial_stage_flag"))
    profitable = _bool_numeric(commercial.get("profitable_flag"))

    moderate_yoy = _setting(settings, "moderate_revenue_decline_yoy_pct", -0.05)
    severe_yoy = _setting(settings, "severe_revenue_decline_yoy_pct", -0.20)
    moderate_qoq = _setting(settings, "moderate_revenue_decline_qoq_pct", -0.10)
    severe_qoq = _setting(settings, "severe_revenue_decline_qoq_pct", -0.35)
    weak_growth = _setting(settings, "weak_growth_yoy_pct", 0.15)
    low_growth = _setting(settings, "low_growth_yoy_pct", 0.05)
    high_pe = _setting(settings, "high_pe_ratio", 35.0)
    severe_pe = _setting(settings, "severe_pe_ratio", 70.0)
    high_ev_sales = _setting(settings, "high_ev_to_sales", 6.0)
    severe_ev_sales = _setting(settings, "severe_ev_to_sales", 12.0)
    high_ps = _setting(settings, "high_price_to_sales", 6.0)
    severe_ps = _setting(settings, "severe_price_to_sales", 12.0)
    transient_yoy = _setting(settings, "transient_anchor_yoy_decline_pct", -0.50)
    transient_qoq = _setting(settings, "transient_anchor_qoq_decline_pct", -0.45)
    high_fcf_yield = _setting(settings, "transient_anchor_high_fcf_yield", 0.25)
    shock_base = _setting(settings, "business_shock_base_score", 20.0)
    shock_reason_weight = _setting(settings, "business_shock_reason_weight", 15.0)
    low_pe = _setting(settings, "transient_anchor_low_pe_ratio", 5.0)
    low_growth_score = _setting(settings, "low_revenue_growth_score", 25.0)
    high_valuation_score = _setting(settings, "high_valuation_score", 85.0)
    high_upside_score = _setting(settings, "high_upside_capacity_score", 85.0)
    fragility_threshold = _setting(settings, "commercial_fragility_threshold", 70.0)
    revenue_min = _setting(settings, "commercial_stage_revenue_min", 50_000_000.0)

    yoy_decline = _decline_score(yoy, moderate=moderate_yoy, severe=severe_yoy)
    qoq_decline = _decline_score(qoq, moderate=moderate_qoq, severe=severe_qoq)
    deterioration_score = max(yoy_decline, qoq_decline)
    deterioration_reasons: list[str] = []
    if yoy_decline > 0.0:
        deterioration_reasons.append("revenue_yoy_decline")
    if qoq_decline > 0.0:
        deterioration_reasons.append("revenue_qoq_decline")
    if yoy is not None and yoy <= severe_yoy:
        deterioration_reasons.append("severe_revenue_yoy_decline")
    if qoq is not None and qoq <= severe_qoq:
        deterioration_reasons.append("severe_revenue_qoq_decline")

    growth_weakness = _decline_score(yoy, moderate=weak_growth, severe=low_growth)
    pe_risk = _scale_above(pe_ratio, threshold=high_pe, severe=severe_pe)
    ev_sales_risk = _scale_above(ev_to_sales, threshold=high_ev_sales, severe=severe_ev_sales)
    ps_risk = _scale_above(price_to_sales, threshold=high_ps, severe=severe_ps)
    valuation_expensiveness = max(pe_risk, ev_sales_risk, ps_risk)
    valuation_mismatch_score = _clamp(min(valuation_expensiveness, max(0.0, growth_weakness)))
    valuation_reasons: list[str] = []
    if valuation_mismatch_score > 0.0:
        valuation_reasons.append("expensive_low_growth")
        if pe_risk > 0.0:
            valuation_reasons.append("high_pe_low_growth")
        if ev_sales_risk > 0.0 or ps_risk > 0.0:
            valuation_reasons.append("high_sales_multiple_low_growth")

    has_commercial_anchor = commercial_stage or profitable or ttm_revenue >= revenue_min
    severe_anchor_decline = (yoy is not None and yoy <= transient_yoy) or (qoq is not None and qoq <= transient_qoq)
    transient_anchor_reasons: list[str] = []
    if has_commercial_anchor and severe_anchor_decline:
        anomaly_count = 0
        if gross_margin is None:
            transient_anchor_reasons.append("missing_gross_margin_with_declining_revenue")
            anomaly_count += 1
        if fcf_yield is not None and fcf_yield >= high_fcf_yield:
            transient_anchor_reasons.append("unusually_high_fcf_yield_with_declining_revenue")
            anomaly_count += 1
        if pe_ratio is not None and 0.0 < pe_ratio <= low_pe:
            transient_anchor_reasons.append("very_low_pe_with_declining_revenue")
            anomaly_count += 1
        if ev_to_sales is not None and ev_to_sales <= 0.0:
            transient_anchor_reasons.append("nonpositive_ev_to_sales_with_declining_revenue")
            anomaly_count += 1
        if revenue_growth_score is not None and revenue_growth_score <= low_growth_score:
            transient_anchor_reasons.append("low_revenue_growth_score_with_commercial_anchor")
            anomaly_count += 1
        transient_anchor_score = 0.0 if anomaly_count <= 0 else _clamp(15.0 + 10.0 * anomaly_count + 0.20 * deterioration_score)
    else:
        transient_anchor_score = 0.0

    business_shock_reasons: list[str] = []
    if yoy is not None and qoq is not None and yoy <= severe_yoy and qoq <= severe_qoq:
        business_shock_reasons.append("multi_period_revenue_shock")
    if deterioration_score > 0.0 and commercial_fragility is not None and commercial_fragility >= fragility_threshold:
        business_shock_reasons.append("commercial_fragility_with_declining_revenue")
    if deterioration_score > 0.0 and revenue_growth_score is not None and revenue_growth_score <= low_growth_score:
        business_shock_reasons.append("low_revenue_growth_score_with_declining_revenue")
    if deterioration_score > 0.0 and (
        (valuation_score is not None and valuation_score >= high_valuation_score)
        or (upside_capacity_score is not None and upside_capacity_score >= high_upside_score)
    ):
        business_shock_reasons.append("turnaround_value_signal_with_declining_revenue")
    if business_shock_reasons:
        reason_score = _clamp(shock_base + shock_reason_weight * len(business_shock_reasons))
        business_shock_score = _clamp(0.75 * deterioration_score + 0.50 * reason_score)
    else:
        business_shock_score = 0.0

    overlay_reasons = sorted(
        set(deterioration_reasons + valuation_reasons + transient_anchor_reasons + business_shock_reasons)
    )
    sub_scores = {
        "deterioration": deterioration_score,
        "valuation_growth_mismatch": valuation_mismatch_score,
        "transient_revenue_anchor": transient_anchor_score,
        "commercial_business_shock": business_shock_score,
    }
    ranked_sub_scores = sorted(sub_scores.values(), reverse=True)
    overlay_weights = (1.00, 0.40, 0.25, 0.15)
    # The weights intentionally sum above 1.0 to amplify co-occurring commercial risks.
    overlay_score = _clamp(sum(weight * score for weight, score in zip(overlay_weights, ranked_sub_scores)))

    return {
        "commercial_deterioration_score": round(deterioration_score, 6),
        "commercial_deterioration_flag": 1.0 if deterioration_score > 0.0 else 0.0,
        "commercial_deterioration_reasons": "|".join(deterioration_reasons),
        "valuation_growth_mismatch_score": round(valuation_mismatch_score, 6),
        "valuation_growth_mismatch_flag": 1.0 if valuation_mismatch_score > 0.0 else 0.0,
        "valuation_growth_mismatch_reasons": "|".join(valuation_reasons),
        "transient_revenue_anchor_score": round(transient_anchor_score, 6),
        "transient_revenue_anchor_flag": 1.0 if transient_anchor_score > 0.0 else 0.0,
        "transient_revenue_anchor_reasons": "|".join(transient_anchor_reasons),
        "commercial_business_shock_score": round(business_shock_score, 6),
        "commercial_business_shock_flag": 1.0 if business_shock_score > 0.0 else 0.0,
        "commercial_business_shock_reasons": "|".join(business_shock_reasons),
        "commercial_risk_overlay_score": round(overlay_score, 6),
        "commercial_risk_overlay_flag": 1.0 if overlay_score > 0.0 else 0.0,
        "commercial_risk_overlay_reasons": "|".join(overlay_reasons),
        "commercial_risk_sub_scores": {key: round(value, 6) for key, value in sub_scores.items()},
    }

"""Portfolio-candidate fields for technology rank-table exports."""
from __future__ import annotations

from typing import Any


RESEARCH_CALIBRATION_FIELDS = [
    "research_calibration_input_eligible_flag",
    "research_calibration_status",
    "research_calibration_reason",
    "calibration_sample_role",
    "calibration_status",
    "calibration_status_reason",
    "survivorship_corrected_panel_flag",
    "stage11_calibration_panel_source",
    "stage11_calibration_input_eligible_flag",
    "stage11_calibration_input_reason",
]

PORTFOLIO_CANDIDATE_FIELDS = [
    "portfolio_candidate_gate",
    "portfolio_candidate_score",
    "portfolio_candidate_status",
    "portfolio_candidate_reason",
    *RESEARCH_CALIBRATION_FIELDS,
    "score_scale_min",
    "score_scale_max",
    "score_neutral_value",
    "score_confidence",
    "eligibility_reason",
    "native_score_field",
    "native_score_value",
    "score_zero_is_missing_flag",
    "universe_status",
    "historical_universe_source",
    "price_start_date",
    "price_end_date",
    "terminal_date",
    "historical_price_ticker",
    "calibration_only",
    "recovery_type",
    "equity_recovery",
    "drop_otc_tape",
    "latest_price_date",
    "source_snapshot_asof_date",
    "price_data_asof_date",
    "feature_data_asof_date",
    "financial_data_asof_date",
    "short_interest_asof_date",
    "institutional_data_asof_date",
    "insider_data_asof_date",
    "borrow_data_asof_date",
    "forward_catalyst_event_date",
    "forward_catalyst_event_type",
    "forward_catalyst_nearest_days",
    "forward_catalyst_source",
    "forward_catalyst_confidence",
    "forward_catalyst_asof_date",
]


def _flag(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _has_value(value: Any) -> bool:
    text = _text(value).lower()
    return text not in {"", "none", "null", "nan"}


def portfolio_candidate_reason(row: dict[str, Any]) -> str:
    reasons: list[str] = []
    if not _flag(row.get("rank_ready_flag")):
        reasons.append("not_rank_ready")
    if not _flag(row.get("calibration_eligible_flag")):
        reasons.append("not_calibration_eligible")
    model_status = _text(row.get("model_status")).lower()
    if model_status != "complete":
        reasons.append(f"model_incomplete:{model_status or 'missing'}")
    if not _flag(row.get("oos_score_valid_flag")):
        invalid_reason = _text(row.get("oos_invalid_reason"))
        reasons.append(f"not_oos_score_valid:{invalid_reason or 'flag=0'}")
    return ";".join(reasons) if reasons else "ok"


def research_calibration_reason(row: dict[str, Any], *, price_asof: Any) -> str:
    """Return why a row can or cannot be used as a Stage 11 calibration input.

    This deliberately does not require `oos_score_valid_flag`. It only certifies
    that the row has usable score/feature provenance. The separate Stage 11
    guardrail below decides whether the snapshot itself is allowed into a
    calibration panel. Forward-return availability is checked later by Stage 11
    after targets are joined.
    """
    reasons: list[str] = []
    if not _has_value(row.get("final_score")):
        reasons.append("missing_score")
    if not _flag(row.get("rank_ready_flag")):
        reasons.append("not_rank_ready")
    if not _flag(row.get("calibration_eligible_flag")):
        reasons.append("not_calibration_eligible")
    model_status = _text(row.get("model_status")).lower()
    if model_status != "complete":
        reasons.append(f"model_incomplete:{model_status or 'missing'}")
    if not _flag(row.get("feature_point_in_time_flag")):
        reasons.append("feature_not_point_in_time")
    if not _flag(row.get("future_return_excluded_flag")):
        reasons.append("future_return_not_excluded")
    if _text(row.get("calibration_usage")) == "calibration_input_only" and not _flag(
        row.get("calibration_input_valid_flag")
    ):
        reasons.append("not_calibration_input_valid")
    if not _has_value(price_asof):
        reasons.append("missing_price_data_asof")
    return ";".join(reasons) if reasons else "ok"


def calibration_sample_role(row: dict[str, Any], *, research_input_eligible: bool) -> str:
    if not research_input_eligible:
        return "excluded"
    if _flag(row.get("oos_score_valid_flag")):
        return "strict_oos"
    return "pre_lock_research"


def calibration_status_reason(row: dict[str, Any], *, sample_role: str, research_reason: str) -> str:
    if sample_role == "strict_oos":
        return "ok"
    if sample_role == "pre_lock_research":
        invalid_reason = _text(row.get("oos_invalid_reason"))
        return f"research_input_ok;not_strict_oos:{invalid_reason or 'oos_score_valid_flag=0'}"
    return research_reason or "excluded"


def stage11_calibration_input_reason(
    row: dict[str, Any],
    *,
    sample_role: str,
    research_input_eligible: bool,
    research_reason: str,
    survivorship_corrected: bool,
) -> str:
    """Return the Stage 11 input verdict for dashboard rank snapshots.

    Dashboard rank tables replay the current investable universe at historical
    dates. That is useful for review and portfolio-snapshot provenance, but it
    is not a survivorship-correct calibration panel. Stage 11 must use strict
    OOS rows or a separately certified survivorship-correct panel.
    """
    if sample_role == "strict_oos":
        return "ok"
    if not research_input_eligible:
        return research_reason or "not_research_calibration_input_eligible"
    if survivorship_corrected:
        return "ok"
    existing_reason = _text(row.get("stage11_calibration_input_reason"))
    if existing_reason:
        return existing_reason
    return "dashboard_snapshot_not_survivorship_corrected_use_sector_diagnostics_panel"


def portfolio_candidate_field_values(row: dict[str, Any]) -> dict[str, Any]:
    """Return only the standardized portfolio/research metadata fields."""
    return {field: row.get(field, "") for field in PORTFOLIO_CANDIDATE_FIELDS}


def add_portfolio_candidate_fields(rows: list[dict[str, Any]], *, score_neutral_value: float = 50.0) -> list[dict[str, Any]]:
    """Add Stage 11/portfolio-layer self-contained fields to rank rows.

    The gate is intentionally strict and only marks rows as candidates when the
    score is rank-ready, calibration-eligible, complete, and a valid OOS score.
    Pre-production historical rows remain useful calibration inputs, but they
    are not investable portfolio candidates.
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        reason = portfolio_candidate_reason(row)
        gate = int(reason == "ok")
        asof = row.get("asof_date", "")
        price_asof = row.get("latest_price_date") or row.get("market_feature_asof_date") or asof
        positioning_asof = row.get("positioning_feature_asof_date") or ""
        research_reason = research_calibration_reason(row, price_asof=price_asof)
        research_input_eligible = research_reason == "ok"
        sample_role = calibration_sample_role(row, research_input_eligible=research_input_eligible)
        status_reason = calibration_status_reason(row, sample_role=sample_role, research_reason=research_reason)
        survivorship_corrected = _flag(row.get("survivorship_corrected_panel_flag"))
        stage11_input_eligible = research_input_eligible and (sample_role == "strict_oos" or survivorship_corrected)
        stage11_reason = stage11_calibration_input_reason(
            row,
            sample_role=sample_role,
            research_input_eligible=research_input_eligible,
            research_reason=research_reason,
            survivorship_corrected=survivorship_corrected,
        )
        stage11_panel_source = (
            row.get("stage11_calibration_panel_source")
            or row.get("calibration_panel_source")
            or "dashboard_rank_snapshot_current_universe_replay"
        )
        item = dict(row)
        item.update(
            {
                "portfolio_candidate_gate": gate,
                "portfolio_candidate_score": row.get("final_score", ""),
                "portfolio_candidate_status": "eligible" if gate else "excluded",
                "portfolio_candidate_reason": reason,
                "research_calibration_input_eligible_flag": int(research_input_eligible),
                "research_calibration_status": "eligible" if research_input_eligible else "excluded",
                "research_calibration_reason": research_reason,
                "calibration_sample_role": sample_role,
                "calibration_status": sample_role,
                "calibration_status_reason": status_reason,
                "survivorship_corrected_panel_flag": int(survivorship_corrected),
                "stage11_calibration_panel_source": stage11_panel_source,
                "stage11_calibration_input_eligible_flag": int(stage11_input_eligible),
                "stage11_calibration_input_reason": stage11_reason,
                "score_scale_min": 0.0,
                "score_scale_max": 100.0,
                "score_neutral_value": score_neutral_value,
                "score_confidence": row.get("data_quality_confidence", ""),
                "eligibility_reason": reason,
                "native_score_field": "final_score",
                "native_score_value": row.get("final_score", ""),
                "score_zero_is_missing_flag": 0,
                "universe_status": row.get("universe_status") or "active",
                "historical_universe_source": row.get("historical_universe_source") or "",
                "price_start_date": row.get("price_start_date", ""),
                "price_end_date": row.get("price_end_date", ""),
                "terminal_date": row.get("terminal_date", ""),
                "historical_price_ticker": row.get("historical_price_ticker") or row.get("ticker", ""),
                "calibration_only": int(_text(row.get("calibration_usage")) == "calibration_input_only"),
                "recovery_type": row.get("recovery_type", ""),
                "equity_recovery": row.get("equity_recovery", ""),
                "drop_otc_tape": row.get("drop_otc_tape", 0),
                "latest_price_date": price_asof,
                "source_snapshot_asof_date": asof,
                "price_data_asof_date": price_asof,
                "feature_data_asof_date": asof,
                "financial_data_asof_date": row.get("financial_feature_asof_date") or row.get("latest_sec_filing_date", ""),
                "short_interest_asof_date": positioning_asof
                if any(_has_value(row.get(key)) for key in ("latest_short_interest_pct_float", "short_interest_change_3m", "latest_days_to_cover"))
                else "",
                "institutional_data_asof_date": positioning_asof if _has_value(row.get("institutional_ownership_delta_pct")) else "",
                "insider_data_asof_date": positioning_asof
                if any(_has_value(row.get(key)) for key in ("insider_net_value_90d", "insider_cluster_buyers_90d"))
                else "",
                "borrow_data_asof_date": positioning_asof if _has_value(row.get("latest_borrow_fee_rate")) else "",
                "forward_catalyst_event_date": row.get("forward_catalyst_event_date", ""),
                "forward_catalyst_event_type": row.get("forward_catalyst_event_type", ""),
                "forward_catalyst_nearest_days": row.get("forward_catalyst_nearest_days", ""),
                "forward_catalyst_source": row.get("forward_catalyst_source", ""),
                "forward_catalyst_confidence": row.get("forward_catalyst_confidence", ""),
                "forward_catalyst_asof_date": row.get("forward_catalyst_asof_date", ""),
            }
        )
        out.append(item)
    return out

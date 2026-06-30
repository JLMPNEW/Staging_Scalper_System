"""Portfolio-candidate fields for technology rank-table exports."""
from __future__ import annotations

from typing import Any


PORTFOLIO_CANDIDATE_FIELDS = [
    "portfolio_candidate_gate",
    "portfolio_candidate_score",
    "portfolio_candidate_status",
    "portfolio_candidate_reason",
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


def add_portfolio_candidate_fields(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
        item = dict(row)
        item.update(
            {
                "portfolio_candidate_gate": gate,
                "portfolio_candidate_score": row.get("final_score", ""),
                "portfolio_candidate_status": "eligible" if gate else "excluded",
                "portfolio_candidate_reason": reason,
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

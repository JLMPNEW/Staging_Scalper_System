from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any


PANEL_SOURCE_CURRENT_UNIVERSE = "dashboard_rank_snapshot_current_universe_replay"
PANEL_SOURCE_SURVIVORSHIP_CORRECTED = (
    "survivorship_corrected_pit_membership_score_recompute"
)


def build_shadow_survivorship_sidecar(
    rows: Iterable[Mapping[str, Any]],
    *,
    fieldnames: Sequence[str],
    rank_ready_field: str = "rank_ready_flag",
    rank_reason_field: str = "rank_ready_reason",
) -> list[dict[str, str]]:
    """Build the standard Industrials Stage 11 research sidecar."""

    output: list[dict[str, str]] = []
    fields = tuple(str(field) for field in fieldnames)
    for source in rows:
        row = {str(key): str(value or "") for key, value in source.items()}
        eligible = row.get(rank_ready_field, "") == "1"
        reason = "ok" if eligible else (
            row.get(rank_reason_field, "") or "not_rank_ready"
        )
        updates = {
            "portfolio_universe_eligible_flag": "",
            "portfolio_selection_policy": "",
            "portfolio_sleeve_selected_flag": "",
            "portfolio_sleeve_target_weight": "",
            "portfolio_candidate_gate": "0",
            "portfolio_candidate_status": "shadow_only",
            "portfolio_candidate_reason": (
                "shadow_only_oos_calibration_not_available"
            ),
            "oos_score_valid_flag": "0",
            "oos_score_asof_date": "",
            "oos_invalid_reason": "shadow_pre_oos_calibration",
            "research_calibration_input_eligible_flag": "1" if eligible else "0",
            "research_calibration_reason": reason,
            "calibration_sample_role": (
                "pre_lock_research" if eligible else "excluded"
            ),
            "stage11_calibration_panel_source": (
                PANEL_SOURCE_SURVIVORSHIP_CORRECTED
            ),
            "stage11_calibration_input_eligible_flag": (
                "1" if eligible else "0"
            ),
            "stage11_calibration_input_reason": reason,
            "survivorship_corrected_panel_flag": "1",
        }
        row.update({key: value for key, value in updates.items() if key in fields})
        output.append({field: row.get(field, "") for field in fields})
    return output


def validate_shadow_survivorship_sidecar(
    rows: Sequence[Mapping[str, Any]],
    *,
    asof_date: str,
    expected_tickers: set[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    if not rows:
        return ["survivorship sidecar is empty"]
    actual_tickers: list[str] = []
    for row in rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        actual_tickers.append(ticker)
        if str(row.get("asof_date") or "") != asof_date:
            errors.append(f"{ticker or '<blank>'}: sidecar asof_date mismatch")
        if str(row.get("survivorship_corrected_panel_flag") or "") != "1":
            errors.append(
                f"{ticker or '<blank>'}: survivorship_corrected_panel_flag must be 1"
            )
        if (
            str(row.get("stage11_calibration_panel_source") or "")
            != PANEL_SOURCE_SURVIVORSHIP_CORRECTED
        ):
            errors.append(f"{ticker or '<blank>'}: invalid Stage 11 panel source")
        if str(row.get("portfolio_candidate_gate") or "") != "0":
            errors.append(f"{ticker or '<blank>'}: historical sidecar cannot be investable")
        if str(row.get("oos_score_valid_flag") or "") != "0":
            errors.append(f"{ticker or '<blank>'}: pre-lock sidecar cannot be OOS-valid")
        rank_ready = str(row.get("rank_ready_flag") or "") == "1"
        stage11_eligible = (
            str(row.get("stage11_calibration_input_eligible_flag") or "") == "1"
        )
        if rank_ready != stage11_eligible:
            errors.append(f"{ticker or '<blank>'}: Stage 11 eligibility must equal rank readiness")
    if not all(actual_tickers):
        errors.append("survivorship sidecar contains a blank ticker")
    if len(set(actual_tickers)) != len(actual_tickers):
        errors.append("survivorship sidecar contains duplicate tickers")
    if expected_tickers is not None and set(actual_tickers) != expected_tickers:
        errors.append(
            "survivorship sidecar universe mismatch "
            f"missing={sorted(expected_tickers - set(actual_tickers))[:20]} "
            f"extra={sorted(set(actual_tickers) - expected_tickers)[:20]}"
        )
    return errors

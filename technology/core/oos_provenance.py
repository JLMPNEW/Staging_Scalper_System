"""OOS provenance helpers for technology dashboard snapshots.

These fields are governance assertions, not independent proof. The helper
records the calibration lock/production dates that the pipeline is operating
under and lets downstream consumers fail closed when a replayed historical
score predates the declared production window.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from technology.core.config import cfg_get


OOS_RANK_FIELDS = [
    "calibration_usage",
    "calibration_input_valid_flag",
    "oos_score_valid_flag",
    "oos_invalid_reason",
    "feature_point_in_time_flag",
    "future_return_excluded_flag",
    "non_point_in_time_sections_omitted_flag",
    "scoring_weights_frozen_flag",
    "calibration_train_start_date",
    "calibration_train_end_date",
    "calibration_lock_date",
    "calibration_production_start_date",
    "calibration_validation_method",
    "calibration_provenance_version",
    "oos_assertion_basis",
]


@dataclass(frozen=True)
class OosProvenance:
    row_fields: dict[str, Any]
    manifest_fields: dict[str, Any]


def parse_iso_date(raw: Any) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def oos_config_for_family(config: dict[str, Any], model_family: str) -> dict[str, Any]:
    raw = cfg_get(config, f"oos_calibration_standards.families.{model_family}", {})
    return raw if isinstance(raw, dict) else {}


def build_oos_provenance(
    config: dict[str, Any],
    *,
    model_family: str,
    asof: str,
    historical_mode: bool,
) -> OosProvenance:
    family_cfg = oos_config_for_family(config, model_family)
    asof_date = parse_iso_date(asof)
    train_start = str(family_cfg.get("calibration_train_start_date") or "")
    train_end = str(family_cfg.get("calibration_train_end_date") or "")
    lock_date = str(family_cfg.get("calibration_lock_date") or family_cfg.get("promotion_effective_date") or "")
    production_start = str(family_cfg.get("calibration_production_start_date") or family_cfg.get("promotion_effective_date") or "")
    validation_method = str(family_cfg.get("calibration_validation_method") or "not_declared")
    provenance_version = str(family_cfg.get("calibration_provenance_version") or "technology_oos_provenance_v1")
    assertion_basis = "config_declared_governance_metadata"

    train_end_date = parse_iso_date(train_end)
    lock_dt = parse_iso_date(lock_date)
    production_start_date = parse_iso_date(production_start)

    reasons: list[str] = []
    scoring_weights_frozen = bool(lock_dt)
    if not scoring_weights_frozen:
        reasons.append("missing_calibration_lock_date")
    if production_start_date is None:
        reasons.append("missing_calibration_production_start_date")
    if train_end_date is None:
        reasons.append("missing_calibration_train_end_date")
    if validation_method in {"", "not_declared"}:
        reasons.append("missing_calibration_validation_method")
    if asof_date is None:
        reasons.append("invalid_asof_date")

    asof_not_future = asof_date is not None and asof_date <= date.today()
    if not asof_not_future:
        reasons.append("asof_date_in_future")

    feature_pit = 1
    future_return_excluded = 1
    non_pit_omitted = 1 if historical_mode else 0
    calibration_input_valid = int(historical_mode and feature_pit == 1 and future_return_excluded == 1 and non_pit_omitted == 1)

    model_available_on_asof = asof_date is not None and production_start_date is not None and asof_date >= production_start_date
    outside_training_window = asof_date is not None and train_end_date is not None and asof_date > train_end_date
    # A historical-mode run recomputes scores from today's database, so later
    # filings/revisions can alter the features. Strict OOS provenance requires
    # a contemporaneous capture: replays qualify only when the asof is within
    # a short live-capture window of the run date (default 5 calendar days,
    # covering T-1/weekend daily backfills). Deep retroactive replays remain
    # calibration inputs, never strict OOS.
    replay_window_days = int(cfg_get(config, "oos_calibration_standards.allow_replay_oos_within_days", 5) or 0)
    # Lower bound rejects future as-of dates: a negative age would otherwise
    # satisfy `<= replay_window_days` and mislabel a future replay as live.
    replay_within_live_window = (
        not historical_mode
        or (asof_date is not None and 0 <= (date.today() - asof_date).days <= replay_window_days)
    )
    oos_score_valid = int(
        scoring_weights_frozen
        and bool(asof_not_future)
        and bool(model_available_on_asof)
        and bool(outside_training_window)
        and feature_pit == 1
        and future_return_excluded == 1
        and (not historical_mode or non_pit_omitted == 1)
        and bool(replay_within_live_window)
    )
    if not model_available_on_asof:
        reasons.append("model_not_available_on_asof")
    if not outside_training_window:
        reasons.append("asof_in_or_before_calibration_training_window")
    if historical_mode and non_pit_omitted != 1:
        reasons.append("historical_non_pit_sections_not_omitted")
    if not replay_within_live_window:
        reasons.append("historical_replay_beyond_live_capture_window")

    invalid_reason = "" if oos_score_valid else ";".join(dict.fromkeys(reasons))
    calibration_usage = "oos_score" if oos_score_valid else "calibration_input_only" if calibration_input_valid else "not_oos_valid"
    row_fields = {
        "calibration_usage": calibration_usage,
        "calibration_input_valid_flag": calibration_input_valid,
        "oos_score_valid_flag": oos_score_valid,
        "oos_invalid_reason": invalid_reason,
        "feature_point_in_time_flag": feature_pit,
        "future_return_excluded_flag": future_return_excluded,
        "non_point_in_time_sections_omitted_flag": non_pit_omitted,
        "scoring_weights_frozen_flag": int(scoring_weights_frozen),
        "calibration_train_start_date": train_start,
        "calibration_train_end_date": train_end,
        "calibration_lock_date": lock_date,
        "calibration_production_start_date": production_start,
        "calibration_validation_method": validation_method,
        "calibration_provenance_version": provenance_version,
        "oos_assertion_basis": assertion_basis,
    }
    manifest_fields = {
        "oos_standards_status": calibration_usage,
        "oos_score_valid_flag": oos_score_valid,
        "calibration_input_valid_flag": calibration_input_valid,
        "oos_invalid_reason": invalid_reason,
        "feature_point_in_time_flag": feature_pit,
        "future_return_excluded_flag": future_return_excluded,
        "non_point_in_time_sections_omitted_flag": non_pit_omitted,
        "scoring_weights_frozen_flag": int(scoring_weights_frozen),
        "calibration_train_start_date": train_start,
        "calibration_train_end_date": train_end,
        "calibration_lock_date": lock_date,
        "calibration_production_start_date": production_start,
        "calibration_validation_method": validation_method,
        "calibration_provenance_version": provenance_version,
        "oos_assertion_basis": assertion_basis,
        "oos_governance_note": (
            "OOS flags assert that features are point-in-time, future returns are excluded, "
            "and weights have been frozen according to declared governance metadata; "
            "they do not independently recompute or audit calibration history."
        ),
    }
    return OosProvenance(row_fields=row_fields, manifest_fields=manifest_fields)


def apply_oos_fields(rows: list[dict[str, Any]], provenance: OosProvenance) -> list[dict[str, Any]]:
    return [{**row, **provenance.row_fields} for row in rows]


def validate_oos_rank_rows(
    rows: list[dict[str, Any]],
    *,
    asof: str,
    historical_mode: bool,
    require_oos_score: bool = False,
) -> list[str]:
    errors: list[str] = []
    if not rows:
        errors.append("No rank rows to validate for OOS standards.")
        return errors
    missing = sorted(set(OOS_RANK_FIELDS).difference(rows[0].keys()))
    if missing:
        errors.append(f"Rank table missing OOS provenance fields: {', '.join(missing)}")
        return errors
    bad_dates = sorted({str(row.get("asof_date") or "") for row in rows if str(row.get("asof_date") or "") != asof})
    if bad_dates:
        errors.append(f"Rank table contains rows outside asof={asof}: {bad_dates[:5]}")
    if historical_mode:
        bad_calibration_inputs = [
            str(row.get("ticker") or "")
            for row in rows
            if str(row.get("calibration_input_valid_flag") or "") != "1"
            or str(row.get("feature_point_in_time_flag") or "") != "1"
            or str(row.get("future_return_excluded_flag") or "") != "1"
            or str(row.get("non_point_in_time_sections_omitted_flag") or "") != "1"
        ]
        if bad_calibration_inputs:
            errors.append(f"Rows not valid as historical calibration inputs: {bad_calibration_inputs[:10]}")
    if require_oos_score:
        bad_scores = [str(row.get("ticker") or "") for row in rows if str(row.get("oos_score_valid_flag") or "") != "1"]
        if bad_scores:
            errors.append(f"Rows not valid as strict OOS scores: {bad_scores[:10]}")
    return errors

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import sys
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from biotech_index.core.db import (  # noqa: E402
    DAILY_FEATURES_OPTIONAL_COLUMNS,
    DAILY_SCORES_OPTIONAL_COLUMNS,
    connect,
    ensure_table_optional_columns,
    init_db,
)
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402
from biotech_index.core.scoring_math import (  # noqa: E402
    clamp,
    decomposed_risk_penalty_input,
    weighted_predictive_risk_penalty_input,
)


LOGGER = logging.getLogger("backfill_biotech_risk_decomposition")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"

DEFAULT_STRUCTURAL_RISK_WEIGHTS = {
    "liquidity": 0.18,
    "financing_survival": 0.28,
    "governance_filing": 0.18,
    "regulatory_setback": 0.18,
    "pipeline_anchor": 0.12,
    "data_quality": 0.06,
}
DEFAULT_COMPENSATED_RISK_WEIGHTS = {
    "clinical_binary": 0.35,
    "collaborator_dependency": 0.30,
    "trial_staleness": 0.20,
    "dilution_optional": 0.15,
}
DEFAULT_PREDICTIVE_RISK_PENALTY_WEIGHTS = {
    "liquidity": 0.34,
    "pipeline_anchor": 0.28,
    "collaborator_dependency": 0.18,
    "trial_staleness": 0.15,
    "data_quality": 0.05,
    "financing_survival": 0.0,
    "governance_filing": 0.0,
    "regulatory_setback": 0.0,
    "clinical_binary": 0.0,
    "dilution_optional": 0.0,
}
DEFAULT_PREDICTIVE_RISK_FREE_BANDS = {
    "liquidity": 0.0,
    "pipeline_anchor": 10.0,
    "collaborator_dependency": 20.0,
    "trial_staleness": 10.0,
    "data_quality": 0.0,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill decomposed biotech risk diagnostics from stored daily_features snapshots."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--start-asof", default=None)
    parser.add_argument("--end-asof", default=None)
    parser.add_argument("--force", action="store_true", help="Recompute rows that already have decomposed risk.")
    parser.add_argument("--dry-run", action="store_true", help="Compute counts without updating the database.")
    return parser.parse_args()


def to_float(raw: object, default: float = 0.0) -> float:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return float(int(raw))
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def to_int(raw: object, default: int = 0) -> int:
    return int(round(to_float(raw, float(default))))


def as_bool(raw: object) -> bool:
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "t", "yes", "y", "enabled", "on"}


def bounded_float(raw: object, default: float, *, low: float | None = None, high: float | None = None) -> float:
    value = to_float(raw, default)
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


def load_risk_settings(config: dict[str, Any]) -> dict[str, Any]:
    raw = cfg_get(config, "biotech_features.risk_decomposition", {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    weights_raw = raw.get("weights") if isinstance(raw.get("weights"), dict) else {}
    compensated_weights_raw = raw.get("compensated_weights") if isinstance(raw.get("compensated_weights"), dict) else {}
    penalty_weights_raw = raw.get("penalty_weights") if isinstance(raw.get("penalty_weights"), dict) else {}
    free_bands_raw = raw.get("penalty_free_bands") if isinstance(raw.get("penalty_free_bands"), dict) else {}
    caps_raw = raw.get("penalty_caps") if isinstance(raw.get("penalty_caps"), dict) else {}
    return {
        "compute_enabled": as_bool(raw.get("compute_enabled", raw.get("enabled", True))),
        "compensated_free_band": bounded_float(raw.get("compensated_free_band"), 60.0, low=0.0, high=100.0),
        "compensated_penalty_weight": bounded_float(raw.get("compensated_penalty_weight"), 0.20, low=0.0, high=1.0),
        "weights": {
            key: bounded_float(weights_raw.get(key), default, low=0.0)
            for key, default in DEFAULT_STRUCTURAL_RISK_WEIGHTS.items()
        },
        "compensated_weights": {
            key: bounded_float(compensated_weights_raw.get(key), default, low=0.0)
            for key, default in DEFAULT_COMPENSATED_RISK_WEIGHTS.items()
        },
        "penalty_weights": {
            key: bounded_float(penalty_weights_raw.get(key), default, low=0.0)
            for key, default in DEFAULT_PREDICTIVE_RISK_PENALTY_WEIGHTS.items()
        },
        "penalty_free_bands": {
            key: bounded_float(free_bands_raw.get(key), default, low=0.0, high=100.0)
            for key, default in DEFAULT_PREDICTIVE_RISK_FREE_BANDS.items()
        },
        "penalty_caps": {
            key: bounded_float(caps_raw.get(key), 100.0, low=0.0, high=100.0)
            for key in DEFAULT_PREDICTIVE_RISK_PENALTY_WEIGHTS
        },
    }


def weighted_component_score(components: dict[str, float], weights: dict[str, float]) -> float:
    total_weight = sum(max(0.0, value) for value in weights.values())
    if total_weight <= 0.0:
        return 0.0
    weighted_sum = sum(max(0.0, weights.get(key, 0.0)) * clamp(value) for key, value in components.items())
    return clamp(weighted_sum / total_weight)


def risk_decomposition_from_payload(
    payload: dict[str, Any],
    *,
    legacy_risk_score: float,
    min_liquidity_addv20: float,
    low_liquidity_addv20: float,
    strong_liquidity_addv20: float,
    settings: dict[str, Any],
) -> dict[str, Any]:
    ctgov = payload.get("ctgov", {}) if isinstance(payload.get("ctgov"), dict) else {}
    sec_liq = payload.get("sec_and_liquidity", {}) if isinstance(payload.get("sec_and_liquidity"), dict) else {}
    survival = payload.get("financial_survival", {}) if isinstance(payload.get("financial_survival"), dict) else {}
    sec_events = payload.get("sec_events", {}) if isinstance(payload.get("sec_events"), dict) else {}
    event_counts = sec_events.get("counts", {}) if isinstance(sec_events.get("counts"), dict) else {}

    median_addv20 = to_float(sec_liq.get("median_addv20"), 0.0)
    liquidity_risk = 0.0
    if median_addv20 <= 0.0 or median_addv20 < min_liquidity_addv20:
        liquidity_risk += 35.0
    elif median_addv20 < low_liquidity_addv20:
        liquidity_risk += 22.0
    elif median_addv20 < strong_liquidity_addv20:
        liquidity_risk += 8.0

    going_status = str(sec_liq.get("going_concern_status") or "").strip().lower()
    latest_gc_status = str(sec_liq.get("latest_periodic_going_concern_status") or "").strip().lower()
    reverse_2y = to_int(sec_liq.get("reverse_split_hits_2y"))
    reverse_5y = to_int(sec_liq.get("reverse_split_hits_5y"))
    recent_nt = to_int(sec_liq.get("recent_nt_filing_count_2y"))
    recent_sec_count = to_int(sec_liq.get("recent_sec_filing_count_2y"))
    sec_gc_events = to_int(sec_events.get("going_concern_event_count")) or (
        to_int(event_counts.get("going_concern_confirmed")) + to_int(event_counts.get("going_concern"))
    )
    confirmed_gc = going_status == "confirmed" or latest_gc_status == "hard"

    governance_filing_risk = 0.0
    if confirmed_gc:
        governance_filing_risk += 55.0
    elif sec_gc_events > 0 and going_status != "resolved":
        governance_filing_risk += 24.0
    elif going_status == "possible":
        governance_filing_risk += 20.0
    elif going_status == "resolved":
        governance_filing_risk += 2.0
    if reverse_2y > 0:
        governance_filing_risk += min(40.0, 16.0 + reverse_2y * 8.0)
    elif reverse_5y > 0:
        governance_filing_risk += 8.0
    if recent_nt > 0:
        governance_filing_risk += min(18.0, recent_nt * 6.0)
    if recent_sec_count <= 0:
        governance_filing_risk += 18.0

    endpoint_missed = to_int(event_counts.get("endpoint_missed"))
    clinical_update_negative = to_int(event_counts.get("clinical_update_negative"))
    clinical_hold = to_int(event_counts.get("clinical_hold"))
    partial_clinical_hold = to_int(event_counts.get("partial_clinical_hold"))
    safety_signal = to_int(event_counts.get("safety_signal"))
    critical_negative_events = clinical_hold + partial_clinical_hold + endpoint_missed + safety_signal
    regulatory_setback_risk = 0.0
    if critical_negative_events > 0:
        regulatory_setback_risk += min(
            55.0,
            clinical_hold * 30.0
            + partial_clinical_hold * 22.0
            + endpoint_missed * 20.0
            + safety_signal * 12.0,
        )
    elif clinical_update_negative > 0:
        regulatory_setback_risk += min(10.0, clinical_update_negative * 2.5)

    cash_runway_months = to_float(survival.get("cash_runway_months"), math.nan)
    severe_runway_flag = to_int(survival.get("severe_runway_flag")) if str(survival.get("severe_runway_flag") or "").strip() else None
    short_runway_flag = to_int(survival.get("short_runway_flag")) if str(survival.get("short_runway_flag") or "").strip() else None
    survival_score = to_float(survival.get("financial_survival_score"), math.nan)
    survival_score_for_calc = survival_score if math.isfinite(survival_score) else 45.0
    survival_quality = str(survival.get("data_quality") or "").strip().lower()
    dilution_pressure_score = to_float(survival.get("dilution_pressure_score"), 0.0)
    burn_acceleration_flag = to_int(survival.get("burn_acceleration_flag"))

    financing_survival_risk = 0.0
    data_quality_risk = 0.0
    dilution_optional_risk = 0.0
    if survival:
        if severe_runway_flag:
            financing_survival_risk += 55.0
        elif short_runway_flag:
            financing_survival_risk += 32.0
        elif math.isfinite(cash_runway_months) and 0 < cash_runway_months < 12:
            financing_survival_risk += 18.0
        if dilution_pressure_score > 0:
            if (math.isfinite(cash_runway_months) and cash_runway_months >= 24) or survival_score_for_calc >= 80:
                financing_survival_risk += min(8.0, dilution_pressure_score * 0.20)
                dilution_optional_risk += min(20.0, dilution_pressure_score * 0.25)
            else:
                financing_survival_risk += min(30.0, dilution_pressure_score * 0.55)
        if burn_acceleration_flag:
            financing_survival_risk += 14.0
        if survival_quality == "low":
            data_quality_risk += 14.0
    else:
        data_quality_risk += 10.0

    verified_active = to_int(ctgov.get("verified_qualifying_active_trial_count"))
    active_lead = to_int(ctgov.get("active_lead_sponsor_trials"))
    active_program = to_int(ctgov.get("active_program_override_trials"))
    active_pivotal_trials = to_int(ctgov.get("active_pivotal_trials"))
    active_phase3_trials = to_int(ctgov.get("active_phase3_trials"))
    phase2_3 = to_float(ctgov.get("phase2_3_active_trials"), 0.0)
    pdufa_date = to_int(event_counts.get("pdufa_date"))
    collaborator_heavy = as_bool(ctgov.get("collaborator_heavy_flag"))
    outcome_override_excluded = to_int(ctgov.get("outcome_override_excluded_rows"))
    outcome_override_review = to_int(ctgov.get("outcome_override_review_rows"))

    pipeline_anchor_risk = 0.0
    if verified_active == 0:
        pipeline_anchor_risk += 35.0
    pipeline_anchor_risk += min(25.0, outcome_override_excluded * 12.0 + outcome_override_review * 3.0)

    collaborator_dependency_risk = 0.0
    if collaborator_heavy and active_lead == 0 and active_program == 0:
        collaborator_dependency_risk += 60.0
    elif collaborator_heavy:
        collaborator_dependency_risk += 25.0

    stale_active = to_int(ctgov.get("stale_active_trials"))
    if stale_active <= 0 and verified_active > 0 and to_int(ctgov.get("days_since_last_update"), 0) >= 365:
        stale_active = 1
    trial_staleness_risk = min(25.0, stale_active * 5.0)

    clinical_binary_risk = 0.0
    if active_pivotal_trials > 0 or pdufa_date > 0 or active_phase3_trials > 0:
        clinical_binary_risk += 35.0
    elif phase2_3 > 0:
        clinical_binary_risk += 22.0
    elif verified_active > 0:
        clinical_binary_risk += 12.0

    structural_components = {
        "liquidity": liquidity_risk,
        "financing_survival": financing_survival_risk,
        "governance_filing": governance_filing_risk,
        "regulatory_setback": regulatory_setback_risk,
        "pipeline_anchor": pipeline_anchor_risk,
        "data_quality": data_quality_risk,
    }
    compensated_components = {
        "clinical_binary": clinical_binary_risk,
        "collaborator_dependency": collaborator_dependency_risk,
        "trial_staleness": trial_staleness_risk,
        "dilution_optional": dilution_optional_risk,
    }
    all_components = {
        **structural_components,
        **compensated_components,
    }
    if bool(settings.get("compute_enabled", True)):
        uncompensated_risk = weighted_component_score(structural_components, settings["weights"])
        compensated_risk = weighted_component_score(compensated_components, settings["compensated_weights"])
        penalty_input = decomposed_risk_penalty_input(
            structural_risk=uncompensated_risk,
            compensated_risk=compensated_risk,
            compensated_free_band=float(settings["compensated_free_band"]),
            compensated_weight=float(settings["compensated_penalty_weight"]),
        )
        predictive_penalty_input = weighted_predictive_risk_penalty_input(
            all_components,
            settings["penalty_weights"],
            free_bands=settings["penalty_free_bands"],
            caps=settings["penalty_caps"],
        )
    else:
        uncompensated_risk = legacy_risk_score
        compensated_risk = 50.0
        penalty_input = legacy_risk_score
        predictive_penalty_input = legacy_risk_score

    return {
        "legacy_risk_score_raw": round(clamp(legacy_risk_score), 4),
        "risk_penalty_input_score_raw": round(penalty_input, 4),
        "predictive_risk_penalty_input_score_raw": round(predictive_penalty_input, 4),
        "uncompensated_risk_score_raw": round(uncompensated_risk, 4),
        "compensated_risk_score_raw": round(compensated_risk, 4),
        "liquidity_risk_score_raw": round(clamp(liquidity_risk), 4),
        "financing_survival_risk_score_raw": round(clamp(financing_survival_risk), 4),
        "governance_filing_risk_score_raw": round(clamp(governance_filing_risk), 4),
        "regulatory_setback_risk_score_raw": round(clamp(regulatory_setback_risk), 4),
        "pipeline_anchor_risk_score_raw": round(clamp(pipeline_anchor_risk), 4),
        "collaborator_dependency_risk_score_raw": round(clamp(collaborator_dependency_risk), 4),
        "trial_staleness_risk_score_raw": round(clamp(trial_staleness_risk), 4),
        "risk_component_scores": {
            "structural": {key: round(clamp(value), 4) for key, value in sorted(structural_components.items())},
            "compensated": {key: round(clamp(value), 4) for key, value in sorted(compensated_components.items())},
            "compensated_free_band": round(float(settings["compensated_free_band"]), 4),
            "compensated_penalty_weight": round(float(settings["compensated_penalty_weight"]), 4),
        },
    }


def date_filter_sql(start_asof: str | None, end_asof: str | None) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if start_asof:
        clauses.append("asof_date >= ?")
        params.append(start_asof)
    if end_asof:
        clauses.append("asof_date <= ?")
        params.append(end_asof)
    if not clauses:
        return "", params
    return "WHERE " + " AND ".join(clauses), params


def backfill(conn: sqlite3.Connection, config: dict[str, Any], args: argparse.Namespace) -> dict[str, int]:
    ensure_table_optional_columns(conn, "daily_features", DAILY_FEATURES_OPTIONAL_COLUMNS)
    ensure_table_optional_columns(conn, "daily_scores", DAILY_SCORES_OPTIONAL_COLUMNS)
    settings = load_risk_settings(config)
    min_liquidity_addv20 = float(cfg_get(config, "biotech_features.min_liquidity_addv20", 1_000_000.0))
    low_liquidity_addv20 = float(cfg_get(config, "biotech_features.low_liquidity_addv20", 2_000_000.0))
    strong_liquidity_addv20 = float(cfg_get(config, "biotech_features.strong_liquidity_addv20", 10_000_000.0))

    where_sql, params = date_filter_sql(args.start_asof, args.end_asof)
    if not args.force:
        missing_clause = "(risk_penalty_input_score_raw IS NULL OR predictive_risk_penalty_input_score_raw IS NULL)"
        where_sql = f"{where_sql} AND {missing_clause}" if where_sql else f"WHERE {missing_clause}"
    rows = conn.execute(
        f"""
        SELECT asof_date, company_id, risk_score_raw, feature_json
        FROM daily_features
        {where_sql}
        ORDER BY asof_date, company_id
        """,
        params,
    ).fetchall()

    feature_updates: list[tuple[Any, ...]] = []
    score_updates: list[tuple[Any, ...]] = []
    invalid_json = 0
    for row in rows:
        try:
            payload = json.loads(row["feature_json"] or "{}")
        except json.JSONDecodeError:
            invalid_json += 1
            continue
        if not isinstance(payload, dict):
            invalid_json += 1
            continue
        raw_scores = payload.get("raw_scores", {})
        if not isinstance(raw_scores, dict):
            raw_scores = {}
        legacy_risk = to_float(raw_scores.get("risk_score_raw"), to_float(row["risk_score_raw"], 0.0))
        fields = risk_decomposition_from_payload(
            payload,
            legacy_risk_score=legacy_risk,
            min_liquidity_addv20=min_liquidity_addv20,
            low_liquidity_addv20=low_liquidity_addv20,
            strong_liquidity_addv20=strong_liquidity_addv20,
            settings=settings,
        )
        raw_scores.update(
            {
                "legacy_risk_score_raw": fields["legacy_risk_score_raw"],
                "risk_penalty_input_score_raw": fields["risk_penalty_input_score_raw"],
                "predictive_risk_penalty_input_score_raw": fields["predictive_risk_penalty_input_score_raw"],
                "uncompensated_risk_score_raw": fields["uncompensated_risk_score_raw"],
                "compensated_risk_score_raw": fields["compensated_risk_score_raw"],
                "risk_decomposition_compute_enabled": bool(settings.get("compute_enabled", True)),
                "risk_component_scores": fields["risk_component_scores"],
            }
        )
        payload["raw_scores"] = raw_scores
        feature_json = json.dumps(payload, ensure_ascii=True, sort_keys=True)
        feature_updates.append(
            (
                fields["legacy_risk_score_raw"],
                fields["risk_penalty_input_score_raw"],
                fields["predictive_risk_penalty_input_score_raw"],
                fields["uncompensated_risk_score_raw"],
                fields["compensated_risk_score_raw"],
                fields["liquidity_risk_score_raw"],
                fields["financing_survival_risk_score_raw"],
                fields["governance_filing_risk_score_raw"],
                fields["regulatory_setback_risk_score_raw"],
                fields["pipeline_anchor_risk_score_raw"],
                fields["collaborator_dependency_risk_score_raw"],
                fields["trial_staleness_risk_score_raw"],
                feature_json,
                row["asof_date"],
                row["company_id"],
            )
        )
        score_updates.append(
            (
                fields["legacy_risk_score_raw"],
                fields["risk_penalty_input_score_raw"],
                fields["predictive_risk_penalty_input_score_raw"],
                fields["uncompensated_risk_score_raw"],
                fields["compensated_risk_score_raw"],
                fields["liquidity_risk_score_raw"],
                fields["financing_survival_risk_score_raw"],
                fields["governance_filing_risk_score_raw"],
                fields["regulatory_setback_risk_score_raw"],
                fields["pipeline_anchor_risk_score_raw"],
                fields["collaborator_dependency_risk_score_raw"],
                fields["trial_staleness_risk_score_raw"],
                json.dumps(fields["risk_component_scores"], ensure_ascii=True, sort_keys=True),
                row["asof_date"],
                row["company_id"],
            )
        )

    if args.dry_run:
        return {
            "candidate_rows": len(rows),
            "feature_updates": len(feature_updates),
            "score_updates": len(score_updates),
            "invalid_json": invalid_json,
        }

    with conn:
        conn.executemany(
            """
            UPDATE daily_features
            SET legacy_risk_score_raw = ?,
                risk_penalty_input_score_raw = ?,
                predictive_risk_penalty_input_score_raw = ?,
                uncompensated_risk_score_raw = ?,
                compensated_risk_score_raw = ?,
                liquidity_risk_score_raw = ?,
                financing_survival_risk_score_raw = ?,
                governance_filing_risk_score_raw = ?,
                regulatory_setback_risk_score_raw = ?,
                pipeline_anchor_risk_score_raw = ?,
                collaborator_dependency_risk_score_raw = ?,
                trial_staleness_risk_score_raw = ?,
                feature_json = ?
            WHERE asof_date = ? AND company_id = ?
            """,
            feature_updates,
        )
        conn.executemany(
            """
            UPDATE daily_scores
            SET legacy_risk_score = ?,
                risk_penalty_input_score = ?,
                predictive_risk_penalty_input_score = ?,
                uncompensated_risk_score = ?,
                compensated_risk_score = ?,
                liquidity_risk_score = ?,
                financing_survival_risk_score = ?,
                governance_filing_risk_score = ?,
                regulatory_setback_risk_score = ?,
                pipeline_anchor_risk_score = ?,
                collaborator_dependency_risk_score = ?,
                trial_staleness_risk_score = ?,
                risk_component_json = ?
            WHERE asof_date = ? AND company_id = ?
            """,
            score_updates,
        )
    return {
        "candidate_rows": len(rows),
        "feature_updates": len(feature_updates),
        "score_updates": len(score_updates),
        "invalid_json": invalid_json,
    }


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    sqlite_timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))
    with connect(db_path, timeout_sec=sqlite_timeout_sec) as conn:
        init_db(conn)
        result = backfill(conn, config, args)
    LOGGER.info("Backfilled biotech risk decomposition: %s", result)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

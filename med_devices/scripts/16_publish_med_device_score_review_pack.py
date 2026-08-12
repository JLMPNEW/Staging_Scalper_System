#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import logging
import math
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, init_db  # noqa: E402
from med_devices.core.fda_states import REGULATORY_RISK_STATES  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SCORE_FIELDS = [
    "asof_date",
    "scoring_model_version",
    "score_model_version",
    "model_family",
    "model_version",
    "scoring_contract_version",
    "rank",
    "ticker",
    "company_name",
    "sector",
    "industry",
    "country",
    "currency",
    "score_confidence",
    "eligibility_reason",
    "native_score_field",
    "native_score_value",
    "production_score_source",
    "ic_tilt_applied_to_production_flag",
    "production_score_regime_version",
    "score_zero_is_missing_flag",
    "universe_status",
    "historical_universe_source",
    "price_start_date",
    "price_end_date",
    "terminal_date",
    "historical_price_ticker",
    "calibration_only",
    "latest_price_date",
    "source_snapshot_asof_date",
    "price_data_asof_date",
    "feature_data_asof_date",
    "subsector",
    "calibration_cohort",
    "calibration_status",
    "calibration_status_reason",
    "calibration_eligible_flag",
    "cohort_score_template_id",
    "cohort_score_template_spec",
    "cohort_score_template_tier1_role",
    "cohort_score_template_tier1_eligible",
    "single_product_risk_flag",
    "binary_event_risk_flag",
    "tier1_safety_status",
    "tier1_safety_reason",
    "passed_tier1_safety_gate",
    "portfolio_candidate_gate",
    "portfolio_candidate_status",
    "portfolio_candidate_reason",
    "portfolio_candidate_score",
    "analyst_review_decision",
    "analyst_review_reason",
    "analyst_review_owner",
    "analyst_reviewed_at",
    "analyst_review_expires_at",
    "analyst_portfolio_override_applied",
    "safe_core_score",
    "safe_core_percentile",
    "safe_core_cohort_percentile",
    "safe_core_rank",
    "safe_core_status",
    "safe_core_reason",
    "passed_safe_core_gate",
    "safe_core_model_version",
    "legacy_all_gates_gate",
    "legacy_gate_misses",
    "composite_score",
    "raw_composite_score",
    "composite_score_delta",
    "rank_delta",
    "classification_change",
    "ic_tilted_composite_score",
    "ic_tilted_composite_delta",
    "ic_tilted_composite_mode",
    "ic_tilted_component_ics_json",
    "composite_percentile",
    "cohort_percentile",
    "fundamental_quality_score",
    "fundamental_quality_component_weight",
    "durable_growth_score",
    "durable_growth_score_legacy",
    "durable_growth_alpha_score",
    "durable_growth_growth_score",
    "durable_growth_quality_score",
    "durable_growth_efficiency_score",
    "durable_growth_capital_discipline_score",
    "durable_growth_evidence_quality_score",
    "durable_growth_component_count",
    "durable_growth_signal_mode",
    "durable_growth_signal_direction",
    "durable_growth_signal_reliability",
    "durable_growth_score_source",
    "durable_growth_gate_mode",
    "durable_growth_policy_reason",
    "durable_growth_gate_excluded",
    "durable_growth_component_weight",
    "durable_growth_repair_flag",
    "durable_growth_repair_reason",
    "durable_growth_validation_status",
    "durable_growth_validation_reason",
    "durable_growth_production_state",
    "fda_product_score",
    "fda_product_score_legacy",
    "fda_alpha_score",
    "fda_safety_score",
    "fda_clearance_velocity_raw",
    "fda_clearance_velocity_score",
    "fda_clearance_acceleration_raw",
    "fda_clearance_acceleration_score",
    "fda_evidence_quality_score",
    "fda_event_risk_score",
    "fda_event_risk_breadth_adjusted_score",
    "fda_safety_breadth_adjusted_score",
    "fda_event_risk_product_family_adjusted_score",
    "fda_safety_product_family_adjusted_score",
    "fda_product_family_shadow_available_flag",
    "fda_product_family_shadow_oos_valid_flag",
    "fda_product_family_adjustment_applied_flag",
    "fda_product_family_exposure_available_count",
    "fda_product_family_exposure_waived_count",
    "fda_product_family_exposure_missing_count",
    "fda_product_family_shadow_status",
    "fda_product_family_shadow_reason",
    "fda_distinct_device_category_count",
    "fda_recall_count_raw",
    "fda_recall_count_per_category",
    "fda_class_i_recall_count",
    "fda_warning_letter_count_36m",
    "fda_mdr_death_injury_count_24m",
    "fda_mdr_malfunction_count_24m",
    "fda_mdr_malfunction_count_per_category",
    "fda_breadth_adjustment_applied",
    "fda_adjudication_applied_flag",
    "fda_adjudicated_event_count_24m",
    "fda_raw_death_count_24m",
    "fda_adjudicated_device_death_count_24m",
    "fda_adjudicated_serious_product_event_count_24m",
    "fda_adjudicated_non_device_death_count_24m",
    "fda_scoring_death_count_24m",
    "fda_scoring_injury_count_24m",
    "fda_scoring_malfunction_count_24m",
    "fda_adjudication_status",
    "fda_adjudication_reviewed_at",
    "fda_signal_mode",
    "fda_signal_direction",
    "fda_signal_reliability",
    "fda_score_source",
    "fda_gate_mode",
    "fda_policy_reason",
    "fda_gate_excluded",
    "fda_component_weight",
    "quality_value_interaction_score",
    "fda_technical_interaction_score",
    "reimbursement_score",
    "reimbursement_component_weight",
    "reimbursement_status",
    "direct_code_evidence",
    "payment_rate_evidence",
    "coverage_policy_evidence",
    "procedure_bundled_flag",
    "capital_equipment_flag",
    "diagnostics_lab_flag",
    "unknown_reimbursement_flag",
    "valuation_score",
    "valuation_component_weight",
    "technical_entry_score",
    "technical_trend_quality_score",
    "technical_relative_strength_score",
    "technical_liquidity_score",
    "technical_volume_breakout_score",
    "technical_volatility_risk_score",
    "technical_setup_score",
    "technical_core_score",
    "technical_alpha_score",
    "technical_pullback_score",
    "technical_overextension_score",
    "technical_breakdown_flag",
    "technical_liquidity_gate_flag",
    "technical_signal_mode",
    "technical_signal_direction",
    "technical_signal_reliability",
    "technical_score_source",
    "technical_entry_status_score",
    "technical_entry_status_score_source",
    "borrow_availability_score",
    "borrow_fee_score",
    "borrow_squeeze_risk_score",
    "borrow_pressure_score",
    "borrow_data_quality_score",
    "short_interest_score",
    "short_pressure_score",
    "short_squeeze_score",
    "short_volume_score",
    "short_interest_velocity_score",
    "days_to_cover_score",
    "short_data_quality_score",
    "institutional_accumulation_score",
    "institutional_crowding_score",
    "institutional_breadth_score",
    "institutional_flow_data_quality_score",
    "insider_net_buy_score",
    "insider_cluster_buy_score",
    "insider_selling_pressure_score",
    "insider_activity_score",
    "insider_data_quality_score",
    "sentiment_catalyst_score",
    "sentiment_catalyst_component_weight",
    "value_trap_score",
    "data_completeness_score",
    "live_component_count",
    "classification",
    "decision_bucket",
    "entry_status",
    "technical_gate_mode",
    "technical_overlay_status",
    "technical_policy_reason",
    "technical_gate_excluded",
    "technical_component_weight",
    "pullback_candidate_tag",
    "pullback_candidate_reason",
    "pullback_candidate_template_id",
    "gate_status",
    "review_reason",
    "failed_gates",
    "classification_reason",
    "fda_review_state",
    "dedup_class_i_recall_count_36m",
    "class_i_multi_source_recall_count_36m",
    "open_class_i_recall_count_36m",
    "terminated_class_i_recall_count_36m",
    "canonical_recall_duplicate_source_count",
    "avg_fda_mapping_confidence",
    "risk_mapping_confidence_min",
    "market_cap",
    "current_shares_outstanding",
    "diluted_weighted_average_shares",
    "basic_weighted_average_shares",
    "shares_source_concept",
    "shares_source_form",
    "shares_source_period",
    "market_cap_validated_flag",
    "avg_dollar_volume_60d",
    "avg_dollar_volume_60d_available_flag",
    "liquidity_score",
    "capacity_bucket",
    "min_position_size_feasible",
    "max_position_size_feasible",
    "passed_raw_score_gate",
    "passed_fundamental_gate",
    "passed_growth_gate",
    "passed_fda_gate",
    "passed_reimbursement_gate",
    "passed_valuation_gate",
    "passed_technical_gate",
    "passed_technical_breakdown_veto",
    "passed_value_trap_gate",
    "passed_data_quality_gate",
    "passed_liquidity_gate",
    "passed_fda_manual_review_gate",
    "final_investability_gate",
    "hard_red_flag",
    "hard_red_flag_reasons",
    "fda_data_available",
    "reimbursement_billing_category",
    "reimbursement_payment_rate_status",
    "reimbursement_primary_payment_file",
    "reimbursement_policy_evidence_count",
    "reimbursement_code_count",
    "reimbursement_rate_row_count",
    "top_positive_drivers",
    "top_negative_drivers",
]
DAILY_COMPOSITE_EXTRA_FIELDS = [
    "score_scale_min",
    "score_scale_max",
    "score_neutral_value",
    "oos_score_valid_flag",
    "research_calibration_input_eligible_flag",
    "research_calibration_status",
    "research_calibration_reason",
    "calibration_sample_role",
    "stage11_calibration_input_eligible_flag",
    "stage11_calibration_input_reason",
    "stage11_calibration_panel_source",
    "survivorship_corrected_panel_flag",
    "recovery_type",
    "equity_recovery",
    "drop_otc_tape",
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
# Explicit raise (not assert) so the guard survives python -O / PYTHONOPTIMIZE.
if "feature_data_asof_date" not in set(SCORE_FIELDS):
    raise RuntimeError(
        "Daily composite contract injection is anchored on 'feature_data_asof_date' in SCORE_FIELDS; "
        "if that field is ever removed from SCORE_FIELDS the daily-only composite fields will be silently dropped. "
        "Update the injection loop below before removing it."
    )
# Superset contract for the daily composite CSVs. Script 13's rolling copy of
# med_device_daily_composite_scores.csv emits company_id (which the review pack
# intentionally omits) and omits the PACK_ONLY_COMPOSITE_FIELDS enrichment columns
# below (which the review pack appends at the end of its dated copy). Everything
# else must match script 13's FIELDNAMES exactly; build_daily_composite_fieldnames()
# enforces both directions at runtime.
DAILY_COMPOSITE_CONTRACT_FIELDS = []
for field in SCORE_FIELDS:
    DAILY_COMPOSITE_CONTRACT_FIELDS.append(field)
    if field == "rank":
        DAILY_COMPOSITE_CONTRACT_FIELDS.append("company_id")
    if field == "feature_data_asof_date":
        DAILY_COMPOSITE_CONTRACT_FIELDS.extend(
            extra for extra in DAILY_COMPOSITE_EXTRA_FIELDS if extra not in SCORE_FIELDS
        )
REVIEW_PACK_OMITTED_CONTRACT_FIELDS = {"company_id"}
# Enrichment columns present ONLY in the dated review-pack composite CSV, appended
# after script 13's ordering so positional readers of the rolling and dated files see
# identical positions for every shared column:
# - forward_catalyst_*: DB-backed catalyst columns script 13 does not emit.
# - dedup_class_i_* / class_i_* / canonical_recall_* / *_mapping_confidence*: FDA
#   recall-dedup audit columns joined from feature_fda_product_risk by load_score_rows.
# - reimbursement_billing_category .. reimbursement_rate_row_count: reimbursement
#   enrichment columns joined from feature_reimbursement by load_score_rows.
PACK_ONLY_COMPOSITE_FIELDS = [
    "forward_catalyst_event_date",
    "forward_catalyst_event_type",
    "forward_catalyst_nearest_days",
    "forward_catalyst_source",
    "forward_catalyst_confidence",
    "forward_catalyst_asof_date",
    "dedup_class_i_recall_count_36m",
    "class_i_multi_source_recall_count_36m",
    "open_class_i_recall_count_36m",
    "terminated_class_i_recall_count_36m",
    "canonical_recall_duplicate_source_count",
    "avg_fda_mapping_confidence",
    "risk_mapping_confidence_min",
    "reimbursement_billing_category",
    "reimbursement_payment_rate_status",
    "reimbursement_primary_payment_file",
    "reimbursement_policy_evidence_count",
    "reimbursement_code_count",
    "reimbursement_rate_row_count",
]
# Stamped into the per-pack manifest so sealed packs published by older script
# revisions are self-describing. Bump whenever the pack's file set, any CSV header,
# or the markdown layout changes.
# v3: SCORE_FIELDS gained 10 fda_product_family_* columns (shadow-only FDA product
# family adjustment), changing every pack CSV header and both daily composite CSVs.
REVIEW_PACK_SCHEMA_VERSION = "med_device_review_pack_v3"


def _script13_composite_fieldnames() -> list[str]:
    module_name = "med_device_daily_scores_builder"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return list(existing.FIELDNAMES)
    script13_path = PACKAGE_ROOT / "scripts" / "13_build_med_device_daily_scores.py"
    spec = importlib.util.spec_from_file_location(module_name, script13_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load composite score builder module from {script13_path}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass definitions inside script 13 can resolve their module.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return list(module.FIELDNAMES)


def build_daily_composite_fieldnames() -> list[str]:
    """Build the dated composite CSV header from script 13's FIELDNAMES.

    The dated header is script 13's rolling header verbatim (minus company_id) with the
    review-pack-only enrichment columns appended at the end, so every shared column sits
    at the same position in both files. Raises RuntimeError (never assert, so the guard
    survives python -O) on drift in either direction:
    - script 13 emits a field outside the contract, or
    - the set of contract fields script 13 omits stops matching PACK_ONLY_COMPOSITE_FIELDS.
    Called from main() so the (expensive) script-13 module exec does not run at import
    time or for --help.
    """
    script13_fields = _script13_composite_fieldnames()
    script13_set = set(script13_fields)
    contract_set = set(DAILY_COMPOSITE_CONTRACT_FIELDS)
    pack_only_set = set(PACK_ONLY_COMPOSITE_FIELDS)
    drift_outside = [field for field in script13_fields if field not in contract_set]
    if drift_outside:
        raise RuntimeError(
            "Script 13 FIELDNAMES drifted outside the daily composite contract: "
            f"{drift_outside}. Add the new fields to SCORE_FIELDS / DAILY_COMPOSITE_EXTRA_FIELDS "
            "(or the contract injection above) so both composite CSV headers stay reconciled."
        )
    missing_from_script13 = {field for field in contract_set if field not in script13_set}
    undeclared = sorted(missing_from_script13 - pack_only_set)
    stale_pack_only = sorted(pack_only_set - missing_from_script13)
    if undeclared or stale_pack_only:
        raise RuntimeError(
            "Daily composite contract drifted versus script 13 FIELDNAMES: "
            f"contract fields missing from script 13 but not declared in PACK_ONLY_COMPOSITE_FIELDS={undeclared}; "
            f"declared pack-only fields script 13 now emits (or the contract dropped)={stale_pack_only}. "
            "Update PACK_ONLY_COMPOSITE_FIELDS / SCORE_FIELDS / DAILY_COMPOSITE_EXTRA_FIELDS so the "
            "rolling and dated composite headers stay reconciled."
        )
    if not REVIEW_PACK_OMITTED_CONTRACT_FIELDS <= script13_set:
        raise RuntimeError(
            "Review-pack omitted contract fields "
            f"{sorted(REVIEW_PACK_OMITTED_CONTRACT_FIELDS - script13_set)} are no longer emitted by "
            "script 13; update REVIEW_PACK_OMITTED_CONTRACT_FIELDS."
        )
    header = [field for field in script13_fields if field not in REVIEW_PACK_OMITTED_CONTRACT_FIELDS]
    header.extend(field for field in PACK_ONLY_COMPOSITE_FIELDS)
    return header


DAILY_COMPOSITE_FIELD_DEFAULTS: dict[str, Any] = {
    "score_scale_min": 0.0,
    "score_scale_max": 100.0,
    "score_neutral_value": 50.0,
    "oos_score_valid_flag": 0,
    "research_calibration_input_eligible_flag": 0,
    "research_calibration_status": "excluded",
    "research_calibration_reason": "missing_research_calibration_metadata",
    "calibration_sample_role": "excluded_from_research_calibration",
    "stage11_calibration_input_eligible_flag": 0,
    "stage11_calibration_input_reason": "missing_research_calibration_metadata",
    "survivorship_corrected_panel_flag": 0,
}
REIMBURSEMENT_LATEST_FIELDS = [
    "reimbursement_status",
    "direct_code_evidence",
    "payment_rate_evidence",
    "coverage_policy_evidence",
    "procedure_bundled_flag",
    "capital_equipment_flag",
    "diagnostics_lab_flag",
    "unknown_reimbursement_flag",
]
FDA_LATEST_FIELDS = [
    "fda_data_available",
]
CALIBRATED_BASELINE_FIELDS = [
    "calibrated_baseline_status",
    "calibrated_baseline_reason",
    *SCORE_FIELDS,
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish post-change med-device score review pack.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="")
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def latest_score_asof(conn: Any) -> str:
    row = conn.execute("SELECT MAX(asof_date) AS asof_date FROM med_device_daily_scores").fetchone()
    asof = str(row["asof_date"] or "") if row is not None else ""
    if not asof:
        raise RuntimeError("No med_device_daily_scores rows found; run script 13 first.")
    return asof


def table_columns(conn: Any, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except Exception:
        return set()
    return {str(row["name"]) for row in rows}


def optional_column_expr(
    columns: set[str],
    *,
    alias: str,
    column: str,
    default_sql: str,
    output_name: str | None = None,
) -> str:
    output = output_name or column
    if column in columns:
        return f"COALESCE({alias}.{column}, {default_sql}) AS {output}"
    return f"{default_sql} AS {output}"


def load_score_rows(conn: Any, *, asof: str) -> list[dict[str, Any]]:
    fda_columns = table_columns(conn, "feature_fda_product_risk")
    latest_fda_review_state_expr = optional_column_expr(
        fda_columns,
        alias="latest_fda",
        column="review_adjusted_fda_state",
        default_sql="''",
        output_name="latest_fda_review_state",
    )
    fda_data_available_expr = optional_column_expr(
        fda_columns,
        alias="latest_fda",
        column="fda_data_available",
        default_sql="0",
        output_name="fda_data_available_latest",
    )
    dedup_class_i_expr = optional_column_expr(
        fda_columns,
        alias="latest_fda",
        column="dedup_class_i_recall_count_36m",
        default_sql="0",
    )
    multi_source_class_i_expr = optional_column_expr(
        fda_columns,
        alias="latest_fda",
        column=(
            "class_i_multi_source_recall_count_36m"
            if "class_i_multi_source_recall_count_36m" in fda_columns
            else "dedup_class_i_recall_count_36m"
        ),
        default_sql="0",
        output_name="class_i_multi_source_recall_count_36m",
    )
    open_class_i_expr = optional_column_expr(
        fda_columns,
        alias="latest_fda",
        column="open_class_i_recall_count_36m",
        default_sql="0",
    )
    terminated_class_i_expr = optional_column_expr(
        fda_columns,
        alias="latest_fda",
        column="terminated_class_i_recall_count_36m",
        default_sql="0",
    )
    duplicate_source_expr = optional_column_expr(
        fda_columns,
        alias="latest_fda",
        column="canonical_recall_duplicate_source_count",
        default_sql="0",
    )
    avg_mapping_expr = optional_column_expr(
        fda_columns,
        alias="latest_fda",
        column="avg_mapping_confidence",
        default_sql="NULL",
        output_name="avg_fda_mapping_confidence",
    )
    risk_mapping_expr = optional_column_expr(
        fda_columns,
        alias="latest_fda",
        column="risk_mapping_confidence_min",
        default_sql="NULL",
    )
    rows = conn.execute(
        f"""
        WITH latest_fda AS (
            SELECT f.*
            FROM feature_fda_product_risk f
            WHERE f.rowid = (
                SELECT f2.rowid
                FROM feature_fda_product_risk f2
                WHERE f2.company_id = f.company_id
                  AND f2.asof_date <= ?
                ORDER BY f2.asof_date DESC, f2.rowid DESC
                LIMIT 1
            )
        ),
        latest_reimbursement AS (
            SELECT r.*
            FROM feature_reimbursement r
            WHERE r.rowid = (
                SELECT r2.rowid
                FROM feature_reimbursement r2
                WHERE r2.company_id = r.company_id
                  AND r2.asof_date <= ?
                ORDER BY r2.asof_date DESC, r2.rowid DESC
                LIMIT 1
            )
        )
        SELECT
            s.*,
            c.ticker,
            c.company_name,
            c.subsector,
            {latest_fda_review_state_expr},
            {fda_data_available_expr},
            {dedup_class_i_expr},
            {multi_source_class_i_expr},
            {open_class_i_expr},
            {terminated_class_i_expr},
            {duplicate_source_expr},
            {avg_mapping_expr},
            {risk_mapping_expr},
            COALESCE(latest_reimbursement.billing_category, '') AS reimbursement_billing_category,
            COALESCE(latest_reimbursement.payment_rate_status, '') AS reimbursement_payment_rate_status,
            COALESCE(latest_reimbursement.primary_payment_file, '') AS reimbursement_primary_payment_file,
            COALESCE(latest_reimbursement.policy_evidence_count, 0) AS reimbursement_policy_evidence_count,
            COALESCE(latest_reimbursement.reimbursement_code_count, 0) AS reimbursement_code_count,
            COALESCE(latest_reimbursement.rate_row_count, 0) AS reimbursement_rate_row_count,
            COALESCE(latest_reimbursement.reimbursement_status, s.reimbursement_status, '') AS reimbursement_status_latest,
            COALESCE(latest_reimbursement.direct_code_evidence, s.direct_code_evidence, 0) AS direct_code_evidence_latest,
            COALESCE(latest_reimbursement.payment_rate_evidence, s.payment_rate_evidence, 0) AS payment_rate_evidence_latest,
            COALESCE(latest_reimbursement.coverage_policy_evidence, s.coverage_policy_evidence, 0) AS coverage_policy_evidence_latest,
            COALESCE(latest_reimbursement.procedure_bundled_flag, s.procedure_bundled_flag, 0) AS procedure_bundled_flag_latest,
            COALESCE(latest_reimbursement.capital_equipment_flag, s.capital_equipment_flag, 0) AS capital_equipment_flag_latest,
            COALESCE(latest_reimbursement.diagnostics_lab_flag, s.diagnostics_lab_flag, 0) AS diagnostics_lab_flag_latest,
            COALESCE(latest_reimbursement.unknown_reimbursement_flag, s.unknown_reimbursement_flag, 0) AS unknown_reimbursement_flag_latest
        FROM med_device_daily_scores s
        JOIN dim_company c ON c.company_id = s.company_id
        LEFT JOIN latest_fda ON latest_fda.company_id = s.company_id
        LEFT JOIN latest_reimbursement ON latest_reimbursement.company_id = s.company_id
        WHERE s.asof_date = ?
        ORDER BY s.rank
        """,
        (asof, asof, asof),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for field in (*REIMBURSEMENT_LATEST_FIELDS, *FDA_LATEST_FIELDS):
            latest_key = f"{field}_latest"
            if latest_key in item:
                item[field] = item.pop(latest_key)
        out.append(item)
    return out


def decode_driver_list(raw: object) -> str:
    try:
        value = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        return str(raw or "")
    if isinstance(value, list):
        return "; ".join(str(item) for item in value)
    return str(value)


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def first_float(*raw_values: object, default: float = 0.0) -> float:
    for raw in raw_values:
        value = to_float(raw)
        if value is not None:
            return value
    return default


def parse_csv_set(raw: object) -> set[str]:
    return {item.strip() for item in str(raw or "").split(",") if item.strip()}


def cohort_profile_key(config: dict[str, Any], cohort: str) -> str:
    raw_profiles = cfg_get(config, "scoring.cohort_profiles", {}) or {}
    if not isinstance(raw_profiles, dict):
        raise ValueError("scoring.cohort_profiles must be a mapping")
    if cohort in raw_profiles:
        return cohort
    raw_aliases = cfg_get(config, "scoring.cohort_profile_aliases", {}) or {}
    if not isinstance(raw_aliases, dict):
        raise ValueError("scoring.cohort_profile_aliases must be a mapping")
    current = cohort
    seen = {current}
    for _ in range(8):
        current = str(raw_aliases.get(current) or "").strip()
        if not current or current in seen:
            return cohort
        if current in raw_profiles:
            return current
        seen.add(current)
    return cohort


def configured_gate_value(config: dict[str, Any], cohort: str, key: str) -> float | None:
    profile_key = cohort_profile_key(config, cohort)
    raw = cfg_get(config, f"scoring.cohort_profiles.{profile_key}.gates.{key}", None)
    if raw is None:
        raw = cfg_get(config, f"scoring.gates.{key}", None)
    return to_float(raw)


def production_seed_is_effective(row: dict[str, Any], config: dict[str, Any], cohort: str) -> bool:
    raw_effective_dates = (
        cfg_get(
            config,
            "calibration.calibrated_baseline.production_seed_effective_from",
            {},
        )
        or {}
    )
    if not isinstance(raw_effective_dates, dict):
        raise ValueError("calibration.calibrated_baseline.production_seed_effective_from must be a mapping")
    effective_raw = raw_effective_dates.get(cohort)
    if effective_raw in {None, ""}:
        return True
    try:
        effective_date = datetime.strptime(str(effective_raw)[:10], "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(
            f"Invalid production seed effective date for {cohort}: {effective_raw!r}; expected YYYY-MM-DD"
        ) from exc
    try:
        asof_date = datetime.strptime(str(row.get("asof_date") or "")[:10], "%Y-%m-%d")
    except ValueError:
        return False
    return asof_date >= effective_date


def passes_min_gate(row: dict[str, Any], field: str, threshold: float | None) -> bool:
    if threshold is None:
        return True
    value = to_float(row.get(field))
    return value is not None and value >= threshold


def passes_max_gate(row: dict[str, Any], field: str, threshold: float | None) -> bool:
    if threshold is None:
        return True
    value = to_float(row.get(field))
    return value is not None and value <= threshold


def calibrated_baseline_candidate_status(row: dict[str, Any], config: dict[str, Any]) -> tuple[str, str] | None:
    cohort = str(row.get("calibration_cohort") or "")
    production_cohorts = parse_csv_set(cfg_get(config, "calibration.calibrated_baseline.production_seed_cohorts", ""))
    watchlist_cohorts = parse_csv_set(cfg_get(config, "calibration.calibrated_baseline.watchlist_seed_cohorts", ""))
    production_seed_active = cohort in production_cohorts and production_seed_is_effective(
        row,
        config,
        cohort,
    )
    if not production_seed_active and cohort not in watchlist_cohorts:
        return None
    if str(row.get("classification") or "") in {
        "manual_review_regulatory_risk",
        "avoid_confirmed_regulatory_risk",
        "data_review_required",
    }:
        return None
    if int(row.get("passed_fda_manual_review_gate") or 0) != 1 or int(row.get("hard_red_flag") or 0) == 1:
        return None
    checks = [
        ("raw_composite_score", "composite_min"),
        ("cohort_percentile", "cohort_percentile_min"),
        ("fundamental_quality_score", "fundamental_quality_min"),
        ("durable_growth_score", "durable_growth_min"),
        ("fda_product_score", "fda_product_min"),
        ("reimbursement_score", "reimbursement_min"),
        ("valuation_score", "valuation_min"),
        ("technical_entry_score", "technical_entry_min"),
        ("data_completeness_score", "data_completeness_min"),
    ]
    for field, gate_key in checks:
        if not passes_min_gate(row, field, configured_gate_value(config, cohort, gate_key)):
            return None
    if not passes_max_gate(row, "value_trap_score", configured_gate_value(config, cohort, "value_trap_max")):
        return None
    status = "production_baseline_candidate" if production_seed_active else "watchlist_baseline_candidate"
    reason = (
        "final_investability_pass"
        if int(row.get("final_investability_gate") or 0) == 1
        else "baseline_gate_pass_not_tier1"
    )
    return status, reason


def calibrated_baseline_candidates(rows: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if int(row.get("portfolio_candidate_gate") or 0) != 1:
            continue
        status = calibrated_baseline_candidate_status(row, config)
        if status is None:
            continue
        baseline_status, reason = status
        item = dict(row)
        item["calibrated_baseline_status"] = baseline_status
        item["calibrated_baseline_reason"] = reason
        out.append(item)
    return sorted(
        out,
        key=lambda item: (
            0 if item["calibrated_baseline_status"] == "production_baseline_candidate" else 1,
            int(item.get("rank") or 999999),
        ),
    )


def clean_row(row: dict[str, Any], *, fieldnames: list[str] | None = None) -> dict[str, Any]:
    item = dict(row)
    item["fda_review_state"] = (
        item.get("fda_review_state") or item.get("latest_fda_review_state") or item.get("fda_state") or ""
    )
    item["top_positive_drivers"] = decode_driver_list(item.get("top_positive_drivers_json"))
    item["top_negative_drivers"] = decode_driver_list(item.get("top_negative_drivers_json"))
    research_reason = str(item.get("research_calibration_reason") or "").strip()
    if research_reason and item.get("stage11_calibration_input_reason") in {None, ""}:
        item["stage11_calibration_input_reason"] = research_reason
    output_fields = fieldnames if fieldnames is not None else SCORE_FIELDS
    return {
        field: DAILY_COMPOSITE_FIELD_DEFAULTS.get(field, "") if item.get(field) in {None, ""} else item[field]
        for field in output_fields
    }


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent, delete=False) as handle:
        tmp_name = handle.name
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp_name, path)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" keeps the JSON LF on every platform, matching the pack's CSV writers.
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent, delete=False) as handle:
        tmp_name = handle.name
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(tmp_name, path)


def calibration_eligible_flag_value(row: dict[str, Any]) -> str:
    raw = row.get("calibration_eligible_flag")
    return "" if raw in {None, ""} else str(raw)


def classification_counts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        classification = str(row.get("classification") or "unclassified")
        key = (classification, calibration_eligible_flag_value(row))
        counts[key] = counts.get(key, 0) + 1
    return [
        {"classification": classification, "calibration_eligible_flag": flag, "count": count}
        for (classification, flag), count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def reimbursement_status_counts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        status = str(row.get("reimbursement_status") or "unknown")
        key = (status, calibration_eligible_flag_value(row))
        counts[key] = counts.get(key, 0) + 1
    return [
        {"reimbursement_status": status, "calibration_eligible_flag": flag, "count": count}
        for (status, flag), count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def collapse_count_rows(items: list[dict[str, Any]], key: str) -> list[tuple[str, int]]:
    totals: dict[str, int] = {}
    for row in items:
        label = str(row[key])
        totals[label] = totals.get(label, 0) + int(row["count"])
    return sorted(totals.items(), key=lambda item: (-item[1], item[0]))


def section_heading(title: str, items: list[dict[str, Any]], limit: int | None = None) -> str:
    """Markdown section heading that states the truncation explicitly when it applies."""
    if limit is not None and len(items) > limit:
        return f"## {title} (top {limit} of {len(items)})"
    return f"## {title}"


def write_markdown(
    path: Path,
    *,
    rows: list[dict[str, Any]],
    counts: list[dict[str, Any]],
    reimbursement_counts: list[dict[str, Any]],
    tier1: list[dict[str, Any]],
    portfolio_candidates: list[dict[str, Any]],
    baseline_candidates: list[dict[str, Any]],
    safe_core: list[dict[str, Any]],
    safe_core_watchlist: list[dict[str, Any]],
    special_situations: list[dict[str, Any]],
    restricted: list[dict[str, Any]],
    regulatory_risk: list[dict[str, Any]],
    pullback_candidates: list[dict[str, Any]],
    top25: list[dict[str, Any]],
    bottom25: list[dict[str, Any]],
    asof: str,
) -> int:
    # All selection lists are computed once in main() and passed in, so the markdown
    # sections and the companion CSVs can never diverge from duplicated filter logic.
    model_version = str(rows[0].get("scoring_model_version") or "") if rows else ""

    def line_items(items: list[dict[str, Any]], *, include_reason: bool = False) -> list[str]:
        out: list[str] = []
        for row in items:
            raw_score = first_float(row.get("raw_composite_score"), row.get("composite_score"))
            percentile = first_float(row.get("composite_percentile"))
            base = (
                f"- {row.get('rank')}. {row.get('ticker')} "
                f"raw={raw_score:.2f} "
                f"pct={percentile:.2f} "
                f"({row.get('classification')})"
            )
            if include_reason:
                reason = row.get("tier1_safety_reason") or row.get("review_reason")
                reason = reason or row.get("hard_red_flag_reasons") or "no reason"
                base += f" - {reason}"
            out.append(base)
        return out

    def safe_core_line_items(items: list[dict[str, Any]], *, include_reason: bool = False) -> list[str]:
        out: list[str] = []
        for row in items:
            base = (
                f"- safe#{int(row.get('safe_core_rank') or 0)} {row.get('ticker')} "
                f"safe={first_float(row.get('safe_core_score')):.2f} "
                f"pct={first_float(row.get('safe_core_percentile')):.2f} "
                f"cohort_pct={first_float(row.get('safe_core_cohort_percentile')):.2f} "
                f"legacy_gate={int(row.get('legacy_all_gates_gate') or 0)}"
            )
            if include_reason:
                base += f" - {row.get('safe_core_reason') or row.get('legacy_gate_misses') or 'no reason'}"
            out.append(base)
        return out

    content = [
        f"# Med Device Score Review Pack - {asof}",
        "",
        f"Scoring model version: `{model_version}`",
        "",
        "## Classification Counts",
        *[f"- {label}: {count}" for label, count in collapse_count_rows(counts, "classification")],
        "",
        "## Reimbursement Status Counts",
        *[f"- {label}: {count}" for label, count in collapse_count_rows(reimbursement_counts, "reimbursement_status")],
        "",
        "## Tier-1 Long Candidates",
        *(line_items(tier1) or ["- None"]),
        "",
        section_heading("Portfolio Candidate Universe", portfolio_candidates, 25),
        *(
            [
                f"- {row.get('rank')}. {row.get('ticker')} "
                f"score={first_float(row.get('portfolio_candidate_score'), row.get('composite_score')):.2f} "
                f"status={row.get('portfolio_candidate_status') or ''} "
                f"decision={row.get('analyst_review_decision') or ''}"
                for row in portfolio_candidates[:25]
            ]
            or ["- None"]
        ),
        "",
        section_heading("Calibrated Baseline Candidates", baseline_candidates, 30),
        *(
            [
                f"- {row.get('rank')}. {row.get('ticker')} "
                f"status={row.get('calibrated_baseline_status')} "
                f"cohort={row.get('calibration_cohort') or 'unknown'} "
                f"raw={first_float(row.get('raw_composite_score'), row.get('composite_score')):.2f} "
                f"cohort_pct={first_float(row.get('cohort_percentile')):.2f} "
                f"class={row.get('classification')} "
                f"reason={row.get('calibrated_baseline_reason')}"
                for row in baseline_candidates[:30]
            ]
            or ["- None"]
        ),
        "",
        section_heading("Shadow Safe-Core Candidates", safe_core, 25),
        *(safe_core_line_items(safe_core[:25], include_reason=True) or ["- None"]),
        "",
        section_heading("Shadow Safe-Core Watchlist", safe_core_watchlist, 25),
        *(safe_core_line_items(safe_core_watchlist[:25], include_reason=True) or ["- None"]),
        "",
        section_heading("Special Situation / Binary Risk Watchlist", special_situations, 25),
        *(line_items(special_situations[:25], include_reason=True) or ["- None"]),
        "",
        section_heading("Restricted Research Cohorts", restricted, 25),
        *(
            [
                f"- {row.get('rank')}. {row.get('ticker')} "
                f"cohort={row.get('calibration_cohort') or 'unknown'} "
                f"status={row.get('calibration_status') or 'production_eligible'} "
                f"reason={row.get('calibration_status_reason') or row.get('classification_reason') or 'not specified'}"
                for row in restricted[:25]
            ]
            or ["- None"]
        ),
        "",
        section_heading("Technical Policy Snapshot", tier1, 25),
        *(
            [
                f"- {row.get('ticker')}: mode={row.get('technical_gate_mode') or 'legacy'} "
                f"overlay={row.get('technical_overlay_status') or row.get('entry_status') or 'unknown'} "
                f"weight={first_float(row.get('technical_component_weight')):.2f}"
                for row in tier1[:25]
            ]
            or ["- None"]
        ),
        "",
        "## Durable Growth Policy Snapshot",
        *(
            [
                f"- {row.get('rank')}. {row.get('ticker')}: "
                f"mode={row.get('durable_growth_signal_mode') or 'legacy'} "
                f"gate={row.get('durable_growth_gate_mode') or 'legacy'} "
                f"state={row.get('durable_growth_production_state') or 'unknown'} "
                f"validation={row.get('durable_growth_validation_status') or 'unknown'} "
                f"alpha={first_float(row.get('durable_growth_alpha_score'), row.get('durable_growth_score')):.2f} "
                f"legacy={first_float(row.get('durable_growth_score_legacy'), row.get('durable_growth_score')):.2f} "
                f"weight={first_float(row.get('durable_growth_component_weight')):.2f} "
                f"reason={row.get('durable_growth_validation_reason') or row.get('durable_growth_repair_reason') or 'none'}"
                for row in top25
            ]
            or ["- None"]
        ),
        "",
        "## FDA Policy Snapshot",
        *(
            [
                f"- {row.get('rank')}. {row.get('ticker')}: "
                f"fda={first_float(row.get('fda_product_score')):.2f} "
                f"legacy={first_float(row.get('fda_product_score_legacy'), row.get('fda_product_score')):.2f} "
                f"alpha={first_float(row.get('fda_alpha_score'), row.get('fda_product_score')):.2f} "
                f"event_risk={first_float(row.get('fda_event_risk_score')):.2f} "
                f"breadth_adj_event_risk={first_float(row.get('fda_event_risk_breadth_adjusted_score')):.2f} "
                f"breadth_adj_safety={first_float(row.get('fda_safety_breadth_adjusted_score')):.2f} "
                f"categories={first_float(row.get('fda_distinct_device_category_count')):.0f} "
                f"mode={row.get('fda_gate_mode') or 'legacy'} "
                f"source={row.get('fda_score_source') or 'fda_product_score'}"
                for row in top25
            ]
            or ["- None"]
        ),
        "",
        section_heading("Pullback Candidate Tags", pullback_candidates, 25),
        *(
            [
                f"- {row.get('rank')}. {row.get('ticker')} "
                f"cohort={row.get('calibration_cohort') or 'unknown'} "
                f"tech={first_float(row.get('technical_entry_score')):.2f} "
                f"template={row.get('pullback_candidate_template_id') or 'unknown'}"
                for row in pullback_candidates[:25]
            ]
            or ["- None"]
        ),
        "",
        "## Regulatory Risk",
        *(line_items(regulatory_risk, include_reason=True) or ["- None"]),
        "",
        "## Top 25",
        *line_items(top25),
        "",
        "## Bottom 25",
        *line_items(bottom25, include_reason=True),
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" disables platform newline translation so the markdown is LF on every
    # host, matching the pack's CSV writers (and script 76's rewrites).
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent, delete=False) as handle:
        tmp_name = handle.name
        handle.write("\n".join(content))
    os.replace(tmp_name, path)
    return len(content)


def dated_output_dir(base_output_dir: Path, asof: str) -> Path:
    return base_output_dir if base_output_dir.name == asof else base_output_dir / asof


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
    output_base_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(
            cfg_get(config, "scoring.review_pack_dir", "../output/med_devices_reports/score_review_pack"),
            base_dir=base_dir,
        )
    )

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        asof = args.asof.strip() or latest_score_asof(conn)
        rows = load_score_rows(conn, asof=asof)
        if not rows:
            raise RuntimeError(f"No med_device_daily_scores rows found for {asof}")
        daily_composite_fields = build_daily_composite_fieldnames()
        output_dir = dated_output_dir(output_base_dir, asof)
        counts = classification_counts(rows)
        clean_rows = [clean_row(row, fieldnames=daily_composite_fields) for row in rows]
        reimbursement_counts = reimbursement_status_counts(clean_rows)
        tier1 = [row for row in clean_rows if row["classification"] == "tier_1_long_candidate"]
        safe_core = sorted(
            [row for row in clean_rows if int(row.get("passed_safe_core_gate") or 0) == 1],
            key=lambda item: (int(item.get("safe_core_rank") or 999999), -first_float(item.get("safe_core_score"))),
        )
        safe_core_watchlist = sorted(
            [row for row in clean_rows if str(row.get("safe_core_status") or "").strip().lower() == "watchlist"],
            key=lambda item: -first_float(item.get("safe_core_score")),
        )
        # A bare tier1_safety_status='fail' OR-term sweeps ~97.6% of the universe into
        # this watchlist; only tier1 failures that carry an actual binary/special-
        # situation risk flag belong here.
        special_situations = [
            row
            for row in clean_rows
            if row["classification"] == "special_situation_or_binary_risk_watchlist"
            or (
                str(row.get("tier1_safety_status") or "").strip().lower() == "fail"
                and (
                    int(row.get("single_product_risk_flag") or 0) == 1
                    or int(row.get("binary_event_risk_flag") or 0) == 1
                )
            )
        ]
        pullback_candidates = [
            row for row in clean_rows if str(row.get("pullback_candidate_tag") or "").strip() in {"1", "true", "True"}
        ]
        manual = [row for row in clean_rows if row["classification"] == "manual_review_regulatory_risk"]
        restricted = [
            row
            for row in clean_rows
            if str(row.get("calibration_status") or "").strip().lower()
            in {"restricted_research_only", "excluded_from_tier1"}
        ]
        regulatory_risk = [
            row
            for row in clean_rows
            if row["classification"] in {"manual_review_regulatory_risk", "avoid_confirmed_regulatory_risk"}
            or str(row["fda_review_state"] or "").strip().lower() in REGULATORY_RISK_STATES
        ]
        portfolio_candidates = sorted(
            [row for row in clean_rows if int(row.get("portfolio_candidate_gate") or 0) == 1],
            key=lambda item: (
                -first_float(item.get("portfolio_candidate_score"), item.get("composite_score")),
                int(item.get("rank") or 999999),
            ),
        )
        top25 = clean_rows[:25]
        bottom25 = list(reversed(clean_rows[-25:]))
        baseline_candidates = calibrated_baseline_candidates(clean_rows, config)

        manifest_files: dict[str, dict[str, Any]] = {}

        def publish_csv(name: str, csv_rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
            write_csv(output_dir / name, csv_rows, fieldnames)
            manifest_files[name] = {"row_count": len(csv_rows), "column_count": len(fieldnames)}
            if not csv_rows:
                logging.warning(
                    "review pack selection %s is empty for %s; published header-only "
                    "(manifest records row_count=0 as intentional)",
                    name,
                    asof,
                )

        publish_csv("med_device_daily_composite_scores.csv", clean_rows, daily_composite_fields)
        publish_csv("med_device_score_review_all.csv", clean_rows, SCORE_FIELDS)
        publish_csv("med_device_score_review_tier1.csv", tier1, SCORE_FIELDS)
        publish_csv("med_device_score_review_portfolio_candidates.csv", portfolio_candidates, SCORE_FIELDS)
        publish_csv(
            "med_device_score_review_calibrated_baseline.csv",
            baseline_candidates,
            CALIBRATED_BASELINE_FIELDS,
        )
        publish_csv("med_device_score_review_safe_core.csv", safe_core, SCORE_FIELDS)
        publish_csv("med_device_score_review_safe_core_watchlist.csv", safe_core_watchlist, SCORE_FIELDS)
        publish_csv(
            "med_device_score_review_special_situation_binary_risk.csv",
            special_situations,
            SCORE_FIELDS,
        )
        publish_csv("med_device_score_review_manual_regulatory.csv", manual, SCORE_FIELDS)
        publish_csv("med_device_score_review_restricted_cohorts.csv", restricted, SCORE_FIELDS)
        publish_csv("med_device_score_review_regulatory_risk.csv", regulatory_risk, SCORE_FIELDS)
        publish_csv("med_device_score_review_top25.csv", top25, SCORE_FIELDS)
        publish_csv("med_device_score_review_bottom25.csv", bottom25, SCORE_FIELDS)
        publish_csv(
            "med_device_score_review_classification_counts.csv",
            counts,
            ["classification", "calibration_eligible_flag", "count"],
        )
        publish_csv(
            "med_device_score_review_reimbursement_status_counts.csv",
            reimbursement_counts,
            ["reimbursement_status", "calibration_eligible_flag", "count"],
        )
        markdown_line_count = write_markdown(
            output_dir / "med_device_score_review_pack.md",
            rows=clean_rows,
            counts=counts,
            reimbursement_counts=reimbursement_counts,
            tier1=tier1,
            portfolio_candidates=portfolio_candidates,
            baseline_candidates=baseline_candidates,
            safe_core=safe_core,
            safe_core_watchlist=safe_core_watchlist,
            special_situations=special_situations,
            restricted=restricted,
            regulatory_risk=regulatory_risk,
            pullback_candidates=pullback_candidates,
            top25=top25,
            bottom25=bottom25,
            asof=asof,
        )
        manifest_files["med_device_score_review_pack.md"] = {"line_count": markdown_line_count}
        # Manifest is written LAST so its presence positively confirms a complete pack:
        # a pack with a manifest whose row_count is 0 was published legitimately empty,
        # while a pack missing its manifest is incomplete/truncated.
        write_json(
            output_dir / "med_device_score_review_manifest.json",
            {
                "asof_date": asof,
                "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "publisher_script": Path(__file__).name,
                "review_pack_schema_version": REVIEW_PACK_SCHEMA_VERSION,
                "scoring_contract_version": str(rows[0].get("scoring_contract_version") or ""),
                "scoring_model_version": str(rows[0].get("scoring_model_version") or ""),
                "universe_row_count": len(rows),
                "files": manifest_files,
            },
        )
        print(
            f"review_pack_dir={output_dir} asof={asof} rows={len(rows)} "
            f"tier1={len(tier1)} safe_core={len(safe_core)} "
            f"portfolio_candidates={len(portfolio_candidates)} "
            f"calibrated_baseline={len(baseline_candidates)} "
            f"safe_core_watchlist={len(safe_core_watchlist)} "
            f"special_situation_binary_risk={len(special_situations)} "
            f"manual_regulatory={len(manual)} "
            f"restricted_cohort={len(restricted)} regulatory_risk={len(regulatory_risk)}"
        )


if __name__ == "__main__":
    raise SystemExit(main())

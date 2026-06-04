#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
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
    "rank",
    "ticker",
    "company_name",
    "subsector",
    "calibration_cohort",
    "calibration_status",
    "calibration_status_reason",
    "cohort_score_template_id",
    "cohort_score_template_spec",
    "cohort_score_template_tier1_role",
    "cohort_score_template_tier1_eligible",
    "single_product_risk_flag",
    "binary_event_risk_flag",
    "tier1_safety_status",
    "tier1_safety_reason",
    "passed_tier1_safety_gate",
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
    "composite_percentile",
    "cohort_percentile",
    "fundamental_quality_score",
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
    "reimbursement_status",
    "direct_code_evidence",
    "payment_rate_evidence",
    "coverage_policy_evidence",
    "procedure_bundled_flag",
    "capital_equipment_flag",
    "diagnostics_lab_flag",
    "unknown_reimbursement_flag",
    "valuation_score",
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
    "sentiment_catalyst_score",
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
            COALESCE(latest_reimbursement.reimbursement_status, s.reimbursement_status, '') AS reimbursement_status,
            COALESCE(latest_reimbursement.direct_code_evidence, s.direct_code_evidence, 0) AS direct_code_evidence,
            COALESCE(latest_reimbursement.payment_rate_evidence, s.payment_rate_evidence, 0) AS payment_rate_evidence,
            COALESCE(latest_reimbursement.coverage_policy_evidence, s.coverage_policy_evidence, 0) AS coverage_policy_evidence,
            COALESCE(latest_reimbursement.procedure_bundled_flag, s.procedure_bundled_flag, 0) AS procedure_bundled_flag,
            COALESCE(latest_reimbursement.capital_equipment_flag, s.capital_equipment_flag, 0) AS capital_equipment_flag,
            COALESCE(latest_reimbursement.diagnostics_lab_flag, s.diagnostics_lab_flag, 0) AS diagnostics_lab_flag,
            COALESCE(latest_reimbursement.unknown_reimbursement_flag, s.unknown_reimbursement_flag, 0) AS unknown_reimbursement_flag
        FROM med_device_daily_scores s
        JOIN dim_company c ON c.company_id = s.company_id
        LEFT JOIN latest_fda ON latest_fda.company_id = s.company_id
        LEFT JOIN latest_reimbursement ON latest_reimbursement.company_id = s.company_id
        WHERE s.asof_date = ?
        ORDER BY s.rank
        """,
        (asof, asof, asof),
    ).fetchall()
    return [dict(row) for row in rows]


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


def clean_row(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["fda_review_state"] = (
        item.get("fda_review_state")
        or item.get("latest_fda_review_state")
        or item.get("fda_state")
        or ""
    )
    item["top_positive_drivers"] = decode_driver_list(item.get("top_positive_drivers_json"))
    item["top_negative_drivers"] = decode_driver_list(item.get("top_negative_drivers_json"))
    return {field: item.get(field, "") for field in SCORE_FIELDS}


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def classification_counts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for row in rows:
        classification = str(row.get("classification") or "unclassified")
        counts[classification] = counts.get(classification, 0) + 1
    return [
        {"classification": classification, "count": count}
        for classification, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def reimbursement_status_counts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("reimbursement_status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return [
        {"reimbursement_status": status, "count": count}
        for status, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def write_markdown(
    path: Path,
    *,
    rows: list[dict[str, Any]],
    counts: list[dict[str, Any]],
    reimbursement_counts: list[dict[str, Any]],
    asof: str,
) -> None:
    model_version = str(rows[0].get("scoring_model_version") or "") if rows else ""
    tier1 = [row for row in rows if row.get("classification") == "tier_1_long_candidate"]
    safe_core = sorted(
        [row for row in rows if int(row.get("passed_safe_core_gate") or 0) == 1],
        key=lambda item: (int(item.get("safe_core_rank") or 999999), -first_float(item.get("safe_core_score"))),
    )
    safe_core_watchlist = sorted(
        [row for row in rows if str(row.get("safe_core_status") or "").strip().lower() == "watchlist"],
        key=lambda item: -first_float(item.get("safe_core_score")),
    )
    special_situations = [
        row
        for row in rows
        if row.get("classification") == "special_situation_or_binary_risk_watchlist"
        or str(row.get("tier1_safety_status") or "").strip().lower() == "fail"
    ]
    restricted = [
        row for row in rows
        if str(row.get("calibration_status") or "").strip().lower()
        in {"restricted_research_only", "excluded_from_tier1"}
    ]
    regulatory_risk = [
        row
        for row in rows
        if row.get("classification") in {"manual_review_regulatory_risk", "avoid_confirmed_regulatory_risk"}
        or str(row.get("fda_review_state") or "").strip().lower() in REGULATORY_RISK_STATES
    ]
    top25 = rows[:25]
    bottom25 = list(reversed(rows[-25:]))
    pullback_candidates = [row for row in rows if str(row.get("pullback_candidate_tag") or "").strip() in {"1", "true", "True"}]

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
        *[f"- {row['classification']}: {row['count']}" for row in counts],
        "",
        "## Reimbursement Status Counts",
        *[f"- {row['reimbursement_status']}: {row['count']}" for row in reimbursement_counts],
        "",
        "## Tier-1 Long Candidates",
        *(line_items(tier1) or ["- None"]),
        "",
        "## Shadow Safe-Core Candidates",
        *(safe_core_line_items(safe_core[:25], include_reason=True) or ["- None"]),
        "",
        "## Shadow Safe-Core Watchlist",
        *(safe_core_line_items(safe_core_watchlist[:25], include_reason=True) or ["- None"]),
        "",
        "## Special Situation / Binary Risk Watchlist",
        *(line_items(special_situations[:25], include_reason=True) or ["- None"]),
        "",
        "## Restricted Research Cohorts",
        *(
            [
                f"- {row.get('rank')}. {row.get('ticker')} "
                f"cohort={row.get('calibration_cohort') or 'unknown'} "
                f"status={row.get('calibration_status') or 'production_eligible'} "
                f"reason={row.get('calibration_status_reason') or row.get('classification_reason') or 'not specified'}"
                for row in restricted[:25]
            ] or ["- None"]
        ),
        "",
        "## Technical Policy Snapshot",
        *(
            [
                f"- {row.get('ticker')}: mode={row.get('technical_gate_mode') or 'legacy'} "
                f"overlay={row.get('technical_overlay_status') or row.get('entry_status') or 'unknown'} "
                f"weight={first_float(row.get('technical_component_weight')):.2f}"
                for row in tier1[:25]
            ] or ["- None"]
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
            ] or ["- None"]
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
                f"mode={row.get('fda_gate_mode') or 'legacy'} "
                f"source={row.get('fda_score_source') or 'fda_product_score'}"
                for row in top25
            ] or ["- None"]
        ),
        "",
        "## Pullback Candidate Tags",
        *(
            [
                f"- {row.get('rank')}. {row.get('ticker')} "
                f"cohort={row.get('calibration_cohort') or 'unknown'} "
                f"tech={first_float(row.get('technical_entry_score')):.2f} "
                f"template={row.get('pullback_candidate_template_id') or 'unknown'}"
                for row in pullback_candidates[:25]
            ] or ["- None"]
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
    path.write_text("\n".join(content), encoding="utf-8")


def dated_output_dir(base_output_dir: Path, asof: str) -> Path:
    return base_output_dir if base_output_dir.name == asof else base_output_dir / asof


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_base_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(cfg_get(config, "scoring.review_pack_dir", "../output/med_devices_reports/score_review_pack"), base_dir=base_dir)
    )

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        asof = args.asof.strip() or latest_score_asof(conn)
        rows = load_score_rows(conn, asof=asof)
        if not rows:
            raise RuntimeError(f"No med_device_daily_scores rows found for {asof}")
        output_dir = dated_output_dir(output_base_dir, asof)
        counts = classification_counts(rows)
        clean_rows = [clean_row(row) for row in rows]
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
        special_situations = [
            row
            for row in clean_rows
            if row["classification"] == "special_situation_or_binary_risk_watchlist"
            or str(row.get("tier1_safety_status") or "").strip().lower() == "fail"
        ]
        manual = [row for row in clean_rows if row["classification"] == "manual_review_regulatory_risk"]
        restricted = [
            row for row in clean_rows
            if str(row.get("calibration_status") or "").strip().lower()
            in {"restricted_research_only", "excluded_from_tier1"}
        ]
        regulatory_risk = [
            row
            for row in clean_rows
            if row["classification"] in {"manual_review_regulatory_risk", "avoid_confirmed_regulatory_risk"}
            or str(row["fda_review_state"] or "").strip().lower() in REGULATORY_RISK_STATES
        ]
        top25 = clean_rows[:25]
        bottom25 = list(reversed(clean_rows[-25:]))

        write_csv(output_dir / "med_device_daily_composite_scores.csv", clean_rows, SCORE_FIELDS)
        write_csv(output_dir / "med_device_score_review_all.csv", clean_rows, SCORE_FIELDS)
        write_csv(output_dir / "med_device_score_review_tier1.csv", tier1, SCORE_FIELDS)
        write_csv(output_dir / "med_device_score_review_safe_core.csv", safe_core, SCORE_FIELDS)
        write_csv(output_dir / "med_device_score_review_safe_core_watchlist.csv", safe_core_watchlist, SCORE_FIELDS)
        write_csv(
            output_dir / "med_device_score_review_special_situation_binary_risk.csv",
            special_situations,
            SCORE_FIELDS,
        )
        write_csv(output_dir / "med_device_score_review_manual_regulatory.csv", manual, SCORE_FIELDS)
        write_csv(output_dir / "med_device_score_review_restricted_cohorts.csv", restricted, SCORE_FIELDS)
        write_csv(output_dir / "med_device_score_review_regulatory_risk.csv", regulatory_risk, SCORE_FIELDS)
        write_csv(output_dir / "med_device_score_review_top25.csv", top25, SCORE_FIELDS)
        write_csv(output_dir / "med_device_score_review_bottom25.csv", bottom25, SCORE_FIELDS)
        write_csv(output_dir / "med_device_score_review_classification_counts.csv", counts, ["classification", "count"])
        write_csv(
            output_dir / "med_device_score_review_reimbursement_status_counts.csv",
            reimbursement_counts,
            ["reimbursement_status", "count"],
        )
        write_markdown(
            output_dir / "med_device_score_review_pack.md",
            rows=clean_rows,
            counts=counts,
            reimbursement_counts=reimbursement_counts,
            asof=asof,
        )
        print(
            f"review_pack_dir={output_dir} asof={asof} rows={len(rows)} "
            f"tier1={len(tier1)} safe_core={len(safe_core)} "
            f"safe_core_watchlist={len(safe_core_watchlist)} "
            f"special_situation_binary_risk={len(special_situations)} "
            f"manual_regulatory={len(manual)} "
            f"restricted_cohort={len(restricted)} regulatory_risk={len(regulatory_risk)}"
        )


if __name__ == "__main__":
    main()

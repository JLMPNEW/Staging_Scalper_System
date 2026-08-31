#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, init_db  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
LOGGER = logging.getLogger("backtest_med_device_cohort_neutral_scores")


def init_db_read_tolerant(conn: Any) -> None:
    try:
        init_db(conn)
    except sqlite3.OperationalError as exc:
        if "readonly database" not in str(exc).lower():
            raise
        LOGGER.warning("Skipping schema migration during read-only cohort-neutral backtest connection: %s", exc)


SCORE_FIELDS = [
    "calibration_cohort",
    "scoring_model_version",
    "rank",
    "classification",
    "decision_bucket",
    "entry_status",
    "composite_score",
    "raw_composite_score",
    "ic_tilted_composite_score",
    "ic_tilted_composite_delta",
    "ic_tilted_composite_mode",
    "composite_percentile",
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
    "tier1_safety_status",
    "tier1_safety_reason",
    "passed_tier1_safety_gate",
    "calibration_status",
    "calibration_status_reason",
    "cohort_score_template_id",
    "cohort_score_template_spec",
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
    "fda_event_risk_breadth_adjusted_score",
    "fda_safety_breadth_adjusted_score",
    "fda_event_risk_product_family_adjusted_score",
    "fda_safety_product_family_adjusted_score",
    "fda_product_family_shadow_available_flag",
    "fda_product_family_shadow_oos_valid_flag",
    "fda_product_family_shadow_status",
    "fda_signal_mode",
    "fda_signal_direction",
    "fda_signal_reliability",
    "fda_score_source",
    "fda_gate_mode",
    "fda_policy_reason",
    "fda_gate_excluded",
    "fda_component_weight",
    "fda_data_available",
    "avg_fda_mapping_confidence",
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
    "value_trap_score",
    "data_completeness_score",
    "live_component_count",
    "fda_review_state",
    "hard_red_flag",
    "hard_red_flag_reasons",
    "avg_dollar_volume_60d",
    "liquidity_score",
    "capacity_bucket",
    "market_cap",
    "market_cap_validated_flag",
    "failed_gates",
    "classification_reason",
    "passed_raw_score_gate",
    "passed_fundamental_gate",
    "passed_growth_gate",
    "passed_fda_gate",
    "passed_reimbursement_gate",
    "passed_valuation_gate",
    "passed_technical_gate",
    "passed_value_trap_gate",
    "passed_data_quality_gate",
    "passed_liquidity_gate",
    "passed_fda_manual_review_gate",
    "final_investability_gate",
]
TAXONOMY_FIELDS = [
    "calibration_cohort",
    "reimbursement_model",
    "regulatory_model",
    "business_model",
    "procedure_sensitivity",
    "capital_equipment_flag",
    "consumables_flag",
    "diagnostics_flag",
    "implantable_flag",
    "single_product_risk_flag",
    "taxonomy_confidence",
    "taxonomy_source",
]
SUMMARY_FIELDS = [
    "summary_type",
    "segment",
    "horizon_days",
    "count",
    "unique_tickers",
    "mean_return",
    "median_return",
    "mean_excess_return",
    "median_excess_return",
    "hit_rate",
    "excess_hit_rate",
    "lcb_excess_return",
    "sortino_excess",
    "profit_factor_excess",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build cohort-neutral med-device score backtest output.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--summary-csv", type=Path, default=None)
    return parser.parse_args()


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def first_float(*raw_values: object) -> float | None:
    for raw in raw_values:
        value = to_float(raw)
        if value is not None:
            return value
    return None


def to_int(raw: object) -> int | None:
    value = to_float(raw)
    return int(value) if value is not None else None


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def return_horizons(rows: list[dict[str, Any]]) -> list[int]:
    if not rows:
        return []
    out: list[int] = []
    for key in rows[0]:
        if key.startswith("forward_return_") and key.endswith("d"):
            text = key[len("forward_return_") : -1]
            if text.isdigit():
                out.append(int(text))
    return sorted(out)


def load_taxonomy(conn: Any, *, asofs: set[str]) -> dict[tuple[str, str], dict[str, Any]]:
    if not table_columns(conn, "dim_company_model_taxonomy_history"):
        raise RuntimeError(
            "PIT taxonomy history is missing; run script 22 for every calibration as-of before "
            "running calibration. The current taxonomy snapshot is not a valid fallback."
        )
    if not asofs:
        return {}
    rows = conn.execute(
        """
        SELECT h.*, c.company_name
        FROM dim_company_model_taxonomy_history h
        JOIN dim_company c ON c.company_id = h.company_id
        WHERE h.asof_date BETWEEN ? AND ?
        """,
        (min(asofs), max(asofs)),
    ).fetchall()
    return {
        (str(row["asof_date"]), str(row["ticker"] or "").upper()): dict(row)
        for row in rows
        if str(row["asof_date"]) in asofs
    }


def table_columns(conn: Any, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except Exception:
        return set()
    return {str(row["name"]) for row in rows}


def load_scores(conn: Any, *, asofs: set[str]) -> dict[tuple[str, str], dict[str, Any]]:
    if not asofs:
        return {}
    fda_columns = table_columns(conn, "feature_fda_product_risk")
    avg_mapping_expr = (
        "f.avg_mapping_confidence AS avg_fda_mapping_confidence"
        if "avg_mapping_confidence" in fda_columns
        else "NULL AS avg_fda_mapping_confidence"
    )
    rows = conn.execute(
        f"""
        SELECT s.*,
               c.ticker,
               {avg_mapping_expr}
        FROM med_device_daily_scores s
        JOIN dim_company c ON c.company_id = s.company_id
        LEFT JOIN feature_fda_product_risk f
          ON f.company_id = s.company_id
         AND f.asof_date = s.asof_date
        WHERE s.asof_date BETWEEN ? AND ?
        """,
        (min(asofs), max(asofs)),
    ).fetchall()
    return {
        (str(row["asof_date"]), str(row["ticker"] or "").upper()): dict(row)
        for row in rows
        if str(row["asof_date"]) in asofs
    }


def add_taxonomy_and_scores(
    rows: list[dict[str, Any]],
    taxonomy: dict[tuple[str, str], dict[str, Any]],
    scores: dict[tuple[str, str], dict[str, Any]],
) -> None:
    missing_taxonomy: set[tuple[str, str]] = set()
    cohort_mismatches: set[tuple[str, str, str, str]] = set()
    for row in rows:
        ticker = str(row.get("ticker") or "").upper()
        asof = str(row.get("asof_date") or "")
        key = (asof, ticker)
        tax = taxonomy.get(key)
        if tax is None:
            missing_taxonomy.add(key)
            continue
        score = scores.get(key, {})
        taxonomy_cohort = str(tax.get("calibration_cohort") or "").strip()
        score_cohort = str(score.get("calibration_cohort") or "").strip()
        if score_cohort and score_cohort != taxonomy_cohort:
            cohort_mismatches.add((asof, ticker, taxonomy_cohort, score_cohort))
            continue
        for field in TAXONOMY_FIELDS:
            row[field] = tax.get(field, "")
        for field in SCORE_FIELDS:
            if field in score:
                row[field] = score.get(field, "")
        if not row.get("calibration_cohort"):
            row["calibration_cohort"] = "unknown"
    if missing_taxonomy:
        examples = ", ".join(f"{asof}:{ticker}" for asof, ticker in sorted(missing_taxonomy)[:25])
        raise RuntimeError(
            "PIT taxonomy history does not cover every backtest row; "
            f"missing={len(missing_taxonomy)} examples={examples}"
        )
    if cohort_mismatches:
        examples = ", ".join(
            f"{asof}:{ticker} taxonomy={taxonomy_cohort} score={score_cohort}"
            for asof, ticker, taxonomy_cohort, score_cohort in sorted(cohort_mismatches)[:25]
        )
        raise RuntimeError(
            "PIT taxonomy history disagrees with saved score cohorts; "
            f"mismatches={len(cohort_mismatches)} examples={examples}"
        )


def add_cohort_percentiles(rows: list[dict[str, Any]]) -> None:
    grouped: dict[tuple[str, str], list[int]] = defaultdict(list)
    for idx, row in enumerate(rows):
        grouped[(str(row.get("asof_date") or ""), str(row.get("calibration_cohort") or ""))].append(idx)
    for indices in grouped.values():
        sortable: list[tuple[int, float]] = []
        for idx in indices:
            raw = first_float(rows[idx].get("raw_composite_score"), rows[idx].get("composite_score"))
            if raw is not None:
                sortable.append((idx, raw))
        if len(sortable) == 1:
            rows[sortable[0][0]]["cohort_percentile"] = 50.0
            rows[sortable[0][0]]["cohort_rank"] = 1
            rows[sortable[0][0]]["cohort_size"] = 1
            continue
        sortable.sort(key=lambda item: item[1], reverse=True)
        denom = max(1, len(sortable) - 1)
        for pos, (idx, _) in enumerate(sortable):
            rows[idx]["cohort_rank"] = pos + 1
            rows[idx]["cohort_size"] = len(sortable)
            rows[idx]["cohort_percentile"] = round(100.0 * (1.0 - (pos / denom)), 2)


def add_cohort_excess_returns(rows: list[dict[str, Any]], *, horizons: list[int]) -> None:
    for horizon in horizons:
        for prefix, output_prefix in (("", ""), ("net_", "net_")):
            field = f"{prefix}forward_return_{horizon}d"
            if not any(field in row for row in rows):
                continue
            grouped: dict[tuple[str, str], list[float]] = defaultdict(list)
            for row in rows:
                value = to_float(row.get(field))
                if value is not None:
                    grouped[(str(row.get("asof_date") or ""), str(row.get("calibration_cohort") or ""))].append(value)
            medians = {key: median(values) for key, values in grouped.items() if values}
            for row in rows:
                value = to_float(row.get(field))
                key = (str(row.get("asof_date") or ""), str(row.get("calibration_cohort") or ""))
                cohort_median = medians.get(key)
                row[f"{output_prefix}cohort_median_return_{horizon}d"] = (
                    "" if cohort_median is None else round(cohort_median, 6)
                )
                if value is None or cohort_median is None:
                    row[f"{output_prefix}cohort_excess_return_{horizon}d"] = ""
                    row[f"{output_prefix}cohort_excess_hit_{horizon}d"] = ""
                else:
                    excess = value - cohort_median
                    row[f"{output_prefix}cohort_excess_return_{horizon}d"] = round(excess, 6)
                    row[f"{output_prefix}cohort_excess_hit_{horizon}d"] = 1 if excess > 0 else 0


def rank_bucket_from_cohort_percentile(row: dict[str, Any]) -> str:
    value = to_float(row.get("cohort_percentile"))
    if value is None:
        return ""
    if value >= 90.0:
        return "cohort_top_decile"
    if value >= 80.0:
        return "cohort_top_quintile_ex_decile"
    if value <= 20.0:
        return "cohort_bottom_quintile"
    return "cohort_middle"


def downside_sortino(values: list[float]) -> str:
    if not values:
        return ""
    avg = mean(values)
    downside = [value for value in values if value < 0]
    if not downside:
        return "999.0000"
    denom = math.sqrt(sum(value * value for value in downside) / len(downside))
    if denom <= 1e-12:
        return "999.0000"
    return f"{avg / denom:.4f}"


def profit_factor(values: list[float]) -> str:
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    if losses <= 1e-12:
        return "999.0000" if gains > 0 else ""
    return f"{gains / losses:.4f}"


def lcb(values: list[float]) -> str:
    if not values:
        return ""
    if len(values) == 1:
        return f"{values[0]:.6f}"
    avg = mean(values)
    variance = sum((value - avg) ** 2 for value in values) / (len(values) - 1)
    return f"{avg - 1.64 * math.sqrt(variance) / math.sqrt(len(values)):.6f}"


def metrics_for(rows: list[dict[str, Any]], *, horizon: int) -> dict[str, Any]:
    returns: list[float] = []
    excess: list[float] = []
    tickers: set[str] = set()
    for row in rows:
        raw = to_float(row.get(f"forward_return_{horizon}d"))
        ex = to_float(row.get(f"cohort_excess_return_{horizon}d"))
        if raw is None or ex is None:
            continue
        returns.append(raw)
        excess.append(ex)
        tickers.add(str(row.get("ticker") or ""))
    if not returns:
        return {
            "count": 0,
            "unique_tickers": 0,
            "mean_return": "",
            "median_return": "",
            "mean_excess_return": "",
            "median_excess_return": "",
            "hit_rate": "",
            "excess_hit_rate": "",
            "lcb_excess_return": "",
            "sortino_excess": "",
            "profit_factor_excess": "",
        }
    return {
        "count": len(returns),
        "unique_tickers": len(tickers),
        "mean_return": f"{mean(returns):.6f}",
        "median_return": f"{median(returns):.6f}",
        "mean_excess_return": f"{mean(excess):.6f}",
        "median_excess_return": f"{median(excess):.6f}",
        "hit_rate": f"{sum(1 for value in returns if value > 0) / len(returns):.4f}",
        "excess_hit_rate": f"{sum(1 for value in excess if value > 0) / len(excess):.4f}",
        "lcb_excess_return": lcb(excess),
        "sortino_excess": downside_sortino(excess),
        "profit_factor_excess": profit_factor(excess),
    }


def summarize(rows: list[dict[str, Any]], *, horizons: list[int]) -> list[dict[str, Any]]:
    for row in rows:
        row["cohort_rank_bucket"] = rank_bucket_from_cohort_percentile(row)
    specs = [
        ("calibration_cohort", "calibration_cohort"),
        ("calibration_cohort_rank_bucket", "calibration_cohort|cohort_rank_bucket"),
        ("classification", "classification"),
        ("safe_core_status", "safe_core_status"),
        ("passed_safe_core_gate", "passed_safe_core_gate"),
        ("tier1_safety_status", "tier1_safety_status"),
        ("legacy_all_gates_gate", "legacy_all_gates_gate"),
        ("entry_status", "entry_status"),
        ("reimbursement_model", "reimbursement_model"),
        ("regulatory_model", "regulatory_model"),
    ]
    out: list[dict[str, Any]] = []
    for horizon in horizons:
        for summary_type, field_expr in specs:
            fields = field_expr.split("|")
            segments = sorted({"|".join(str(row.get(field) or "") for field in fields) for row in rows})
            for segment in segments:
                subset = [row for row in rows if "|".join(str(row.get(field) or "") for field in fields) == segment]
                item = {"summary_type": summary_type, "segment": segment, "horizon_days": horizon}
                item.update(metrics_for(subset, horizon=horizon))
                out.append(item)
    return out


def output_fields(rows: list[dict[str, Any]], *, horizons: list[int]) -> list[str]:
    base = list(rows[0].keys()) if rows else []
    appended = [
        "calibration_cohort",
        "cohort_rank",
        "cohort_size",
        "cohort_percentile",
        "cohort_rank_bucket",
        *TAXONOMY_FIELDS,
        *SCORE_FIELDS,
    ]
    for horizon in horizons:
        appended.extend(
            [
                f"cohort_median_return_{horizon}d",
                f"cohort_excess_return_{horizon}d",
                f"cohort_excess_hit_{horizon}d",
                f"net_cohort_median_return_{horizon}d",
                f"net_cohort_excess_return_{horizon}d",
                f"net_cohort_excess_hit_{horizon}d",
            ]
        )
    fields: list[str] = []
    for field in [*base, *appended]:
        if field not in fields:
            fields.append(field)
    return fields


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    input_csv = (
        args.input_csv.expanduser().resolve()
        if args.input_csv
        else resolve_path(cfg_get(config, "scoring.backtest_output_csv"), base_dir=base_dir)
    )
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(cfg_get(config, "calibration.cohort_neutral_backtest_csv"), base_dir=base_dir)
    )
    summary_csv = (
        args.summary_csv.expanduser().resolve()
        if args.summary_csv
        else resolve_path(cfg_get(config, "calibration.cohort_neutral_summary_csv"), base_dir=base_dir)
    )
    rows: list[dict[str, Any]] = read_csv(input_csv)
    horizons = return_horizons(rows)
    if not horizons:
        raise RuntimeError(f"No forward_return_<horizon>d columns found in {input_csv}")
    asofs = {str(row.get("asof_date") or "") for row in rows if str(row.get("asof_date") or "").strip()}
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db_read_tolerant(conn)
        taxonomy = load_taxonomy(conn, asofs=asofs)
        scores = load_scores(conn, asofs=asofs)
    add_taxonomy_and_scores(rows, taxonomy, scores)
    add_cohort_percentiles(rows)
    add_cohort_excess_returns(rows, horizons=horizons)
    summary_rows = summarize(rows, horizons=horizons)
    write_csv(output_csv, rows, output_fields(rows, horizons=horizons))
    write_csv(summary_csv, summary_rows, SUMMARY_FIELDS)
    LOGGER.info("Cohort-neutral backtest complete: output=%s rows=%d", output_csv, len(rows))
    LOGGER.info("Cohort-neutral summary complete: output=%s rows=%d", summary_csv, len(summary_rows))


if __name__ == "__main__":
    raise SystemExit(main())

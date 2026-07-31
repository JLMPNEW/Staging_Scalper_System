from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from industrials.core.oos_research import weighted_score
from industrials.core.policy_loader import load_eligibility_policy, resolve_policy
from industrials.core.production_lock import ProductionLock
from industrials.core.score_history import (
    build_shadow_survivorship_sidecar,
    validate_shadow_survivorship_sidecar,
)
from industrials.transportation.contracts import (
    FINAL_RANK_FIELDS,
    SCORING_FEATURE_FIELDS,
    file_sha256,
    validate_rank_rows,
    validate_scoring_rows,
    write_manifest,
    write_rank_rows,
)
from industrials.transportation.financial_contract import MetricDefinition


MODEL_FAMILY = "transportation"
OBSERVED_STATUSES = frozenset({"REPORTED", "DERIVED", "PROXY"})
COMPONENT_FIELD = {
    "market_trend": "market_trend_score",
    "quality": "quality_score",
    "growth": "growth_score",
    "valuation": "valuation_score",
    "operating_efficiency": "operating_efficiency_score",
    "capital_risk": "capital_risk_score",
    "development_stage_risk": "development_stage_risk_score",
    "positioning": "positioning_score",
}


def _number(value: object) -> float | None:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _fmt(value: object, *, digits: int = 8) -> str:
    number = _number(value)
    if number is None:
        return ""
    return f"{number:.{digits}f}".rstrip("0").rstrip(".") or "0"


def _days_between(asof: str, earlier: str) -> int | None:
    try:
        return (
            datetime.strptime(asof[:10], "%Y-%m-%d").date()
            - datetime.strptime(earlier[:10], "%Y-%m-%d").date()
        ).days
    except ValueError:
        return None


def load_members(
    conn: sqlite3.Connection,
    *,
    asof: str,
    active_source_id: str,
    membership_mode: str = "current",
    historical_source_id: str = "",
    delisted_source_id: str = "",
) -> list[dict[str, Any]]:
    if membership_mode not in {"current", "pit"}:
        raise ValueError(f"unsupported membership_mode={membership_mode!r}")
    select_fields = """
        SELECT m.ticker, c.company_name, t.sector, t.industry, t.subsector,
               t.calibration_cohort_id, t.calibration_cohort, t.calibration_use,
               t.development_stage, m.membership_source_id, m.membership_basis,
               m.start_date, m.end_date, m.membership_status, m.confidence
        FROM {membership_relation} AS m
        JOIN dim_company AS c ON c.company_id=m.company_id
        JOIN dim_industrials_taxonomy AS t
          ON t.ticker=m.ticker AND t.model_family=m.model_family
        {where_clause}
        ORDER BY m.ticker
    """
    if membership_mode == "current":
        query = select_fields.format(
            membership_relation="dim_universe_membership",
            where_clause="""
                WHERE m.model_family=? AND m.membership_source_id=?
                  AND m.membership_status='active'
                  AND m.start_date<=?
                  AND COALESCE(m.end_date,'9999-12-31')>=?
            """,
        )
        params: tuple[Any, ...] = (
            MODEL_FAMILY,
            active_source_id,
            asof,
            asof,
        )
    else:
        # Every effective membership source is eligible in historical research.
        # Prefer the curated historical row, then the current seed, then the
        # delisted seed, and retain exactly one deterministic row per ticker.
        query = select_fields.format(
            membership_relation="""(
                SELECT ranked.*
                FROM (
                    SELECT membership.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY membership.ticker
                               ORDER BY
                                 CASE membership.membership_source_id
                                   WHEN ? THEN 0
                                   WHEN ? THEN 1
                                   WHEN ? THEN 2
                                   ELSE 3
                                 END,
                                 membership.confidence DESC,
                                 membership.membership_source_id
                           ) AS membership_row_number
                    FROM dim_universe_membership AS membership
                    WHERE membership.model_family=?
                      AND membership.start_date<=?
                      AND COALESCE(membership.end_date,'9999-12-31')>=?
                ) AS ranked
                WHERE ranked.membership_row_number=1
            )""",
            where_clause="",
        )
        params = (
            historical_source_id,
            active_source_id,
            delisted_source_id,
            MODEL_FAMILY,
            asof,
            asof,
        )
    return [dict(row) for row in conn.execute(query, params).fetchall()]

def latest_feature(
    conn: sqlite3.Connection,
    *,
    table: str,
    ticker: str,
    asof: str,
) -> dict[str, Any]:
    if table not in {"feature_market_technical", "feature_financial_statement"}:
        raise ValueError(f"Unsupported feature table={table}")
    row = conn.execute(
        f"""
        SELECT * FROM {table}
        WHERE ticker=? AND model_family=? AND asof_date<=?
        ORDER BY asof_date DESC, source_id ASC LIMIT 1
        """,
        (ticker, MODEL_FAMILY, asof),
    ).fetchone()
    return dict(row) if row is not None else {}


def latest_profile(conn: sqlite3.Connection, *, ticker: str, asof: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT * FROM dim_issuer_reporting_profile_history
        WHERE ticker=? AND model_family=? AND profile_asof_date<=?
        ORDER BY profile_asof_date DESC LIMIT 1
        """,
        (ticker, MODEL_FAMILY, asof),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM dim_issuer_reporting_profile WHERE ticker=? AND model_family=?",
            (ticker, MODEL_FAMILY),
        ).fetchone()
    return dict(row) if row is not None else {}


def load_metric_rows(
    conn: sqlite3.Connection,
    *,
    asof: str,
    snapshot_mode: str = "exact",
) -> dict[str, dict[str, dict[str, Any]]]:
    if snapshot_mode == "exact":
        query = """
            SELECT * FROM feature_financial_metric_availability
            WHERE model_family=? AND asof_date=?
            ORDER BY ticker, metric_name
        """
        params: tuple[Any, ...] = (MODEL_FAMILY, asof)
    elif snapshot_mode == "latest":
        snapshot = conn.execute(
            """
            SELECT MAX(asof_date)
            FROM feature_financial_metric_availability
            WHERE model_family=? AND asof_date<=?
            """,
            (MODEL_FAMILY, asof),
        ).fetchone()
        snapshot_date = str(snapshot[0] or "") if snapshot is not None else ""
        query = """
            SELECT * FROM feature_financial_metric_availability
            WHERE model_family=? AND asof_date=?
            ORDER BY ticker, metric_name
        """
        params = (MODEL_FAMILY, snapshot_date)
    else:
        raise ValueError(f"unsupported metric snapshot_mode={snapshot_mode!r}")
    result: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in conn.execute(query, params).fetchall():
        item = {
            key: row[key]
            for key in row.keys()
            if key != "metric_row_number"
        }
        result[str(item["ticker"])][str(item["metric_name"])] = item
    return result

def _quantile(sorted_values: list[float], fraction: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * fraction
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def metric_percentiles(
    members: list[dict[str, Any]],
    definitions: list[MetricDefinition],
    metric_rows: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, dict[str, float]]:
    member_by_ticker = {str(item["ticker"]): item for item in members}
    scores: dict[str, dict[str, float]] = defaultdict(dict)
    for definition in definitions:
        by_cohort: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for ticker, member in member_by_ticker.items():
            if not definition.applies_to(
                cohort=str(member["calibration_cohort_id"]), industry=str(member["industry"])
            ):
                continue
            row = metric_rows.get(ticker, {}).get(definition.metric_id, {})
            if str(row.get("availability_status") or "") not in OBSERVED_STATUSES:
                continue
            value = _number(row.get("metric_value"))
            if value is not None:
                by_cohort[str(member["calibration_cohort_id"])].append((ticker, value))
        for cohort_rows in by_cohort.values():
            raw = sorted(value for _, value in cohort_rows)
            low = _quantile(raw, definition.winsor_lower)
            high = _quantile(raw, definition.winsor_upper)
            clipped = [(ticker, min(high, max(low, value))) for ticker, value in cohort_rows]
            unique_values = sorted({value for _, value in clipped})
            for ticker, value in clipped:
                if len(unique_values) == 1:
                    percentile = 50.0
                else:
                    equal_positions = [idx for idx, item in enumerate(unique_values) if item == value]
                    mean_position = sum(equal_positions) / len(equal_positions)
                    percentile = 100.0 * mean_position / (len(unique_values) - 1)
                scores[ticker][definition.metric_id] = (
                    percentile if definition.direction == 1 else 100.0 - percentile
                )
    return scores


def build_scoring_rows(
    conn: sqlite3.Connection,
    *,
    asof: str,
    active_source_id: str,
    definitions: list[MetricDefinition],
    membership_mode: str = "current",
    historical_source_id: str = "",
    delisted_source_id: str = "",
    metric_snapshot_mode: str = "exact",
    policy_asof: str = "",
    registry_version: str,
    policy_path: Path,
    component_weights: dict[str, float],
    max_staleness_days: int,
    minimum_avg_dollar_volume: float,
    minimum_score_confidence: float,
    minimum_specialized_coverage: float,
    specialized_overlay_weights: dict[str, float] | None = None,
) -> list[dict[str, str]]:
    members = load_members(
        conn,
        asof=asof,
        active_source_id=active_source_id,
        membership_mode=membership_mode,
        historical_source_id=historical_source_id,
        delisted_source_id=delisted_source_id,
    )
    if not members:
        raise ValueError(
            f"No transportation members at {asof} for mode={membership_mode}"
        )
    if set(component_weights) != set(COMPONENT_FIELD):
        raise ValueError(f"component_weights must define exactly {sorted(COMPONENT_FIELD)}")
    if any(weight < 0 for weight in component_weights.values()) or not math.isclose(
        sum(component_weights.values()), 1.0, abs_tol=1e-9
    ):
        raise ValueError("component_weights must be non-negative and sum to 1")
    overlay_weights = {
        str(metric_id): float(weight)
        for metric_id, weight in (specialized_overlay_weights or {}).items()
    }
    if any(weight < 0.0 or weight > 1.0 for weight in overlay_weights.values()):
        raise ValueError("specialized overlay weights must be within 0..1")
    definitions_by_id = {definition.metric_id: definition for definition in definitions}
    unknown_overlays = sorted(set(overlay_weights) - set(definitions_by_id))
    if unknown_overlays:
        raise ValueError(f"unknown specialized overlay metrics={unknown_overlays}")
    non_specialized_overlays = sorted(
        metric_id
        for metric_id, weight in overlay_weights.items()
        if weight > 0.0 and not definitions_by_id[metric_id].specialized
    )
    if non_specialized_overlays:
        raise ValueError(
            "positive overlay weights require specialized metrics="
            f"{non_specialized_overlays}"
        )
    generic_definitions = [
        definition for definition in definitions if not definition.specialized
    ]
    if not generic_definitions:
        raise ValueError("generic scoring definition set is empty")
    overlay_active = any(weight > 0.0 for weight in overlay_weights.values())
    policies = load_eligibility_policy(
        policy_path,
        asof=policy_asof or asof,
    )
    metric_rows = load_metric_rows(
        conn,
        asof=asof,
        snapshot_mode=metric_snapshot_mode,
    )
    # Use the same direction-adjusted percentile engine for the frozen generic
    # baseline and any explicitly activated bounded overlay. Optional
    # specialized metrics with zero weights never enter component averages.
    percentile_definitions = generic_definitions + [
        definitions_by_id[metric_id]
        for metric_id, weight in overlay_weights.items()
        if weight > 0.0
    ]
    percentiles = metric_percentiles(
        members,
        list(dict.fromkeys(percentile_definitions)),
        metric_rows,
    )
    output: list[dict[str, str]] = []
    for member in members:
        ticker = str(member["ticker"])
        cohort = str(member["calibration_cohort_id"])
        industry = str(member["industry"])
        market = latest_feature(conn, table="feature_market_technical", ticker=ticker, asof=asof)
        financial = latest_feature(conn, table="feature_financial_statement", ticker=ticker, asof=asof)
        profile = latest_profile(conn, ticker=ticker, asof=asof)
        reporting_profile = str(
            profile.get("reporting_profile")
            or financial.get("reporting_profile")
            or "NO_FINANCIALS_REVIEW"
        )
        financial_confidence = _number(
            profile.get("financial_confidence")
            if profile.get("financial_confidence") is not None
            else financial.get("financial_confidence")
        )
        stage = str(member.get("development_stage") or "operating")
        policy = resolve_policy(policies, reporting_profile, stage)
        if policy is None:
            raise ValueError(
                f"Missing scoring policy ticker={ticker} profile={reporting_profile} stage={stage}"
            )
        minimum_financial_confidence = _number(policy.get("minimum_financial_confidence"))
        if minimum_financial_confidence is None:
            raise ValueError(f"Policy has invalid financial confidence: {reporting_profile}:{stage}")
        rows_for_ticker = metric_rows.get(ticker, {})
        applicable = [
            definition
            for definition in definitions
            if definition.applies_to(cohort=cohort, industry=industry)
            and (not definition.birthdate or asof >= definition.birthdate)
            and str(
                rows_for_ticker.get(definition.metric_id, {}).get("availability_status") or ""
            )
            != "NOT_APPLICABLE"
        ]
        statuses = {
            definition.metric_id: str(
                rows_for_ticker.get(definition.metric_id, {}).get("availability_status") or "NOT_DISCLOSED"
            )
            for definition in applicable
        }
        values = {
            definition.metric_id: _number(rows_for_ticker.get(definition.metric_id, {}).get("metric_value"))
            for definition in applicable
            if statuses[definition.metric_id] in OBSERVED_STATUSES
            and _number(rows_for_ticker.get(definition.metric_id, {}).get("metric_value")) is not None
        }
        observed = [
            definition for definition in applicable if definition.metric_id in values
        ]
        generic_applicable = [
            definition for definition in applicable if not definition.specialized
        ]
        required = [definition for definition in applicable if definition.required_for_rank]
        specialized = [definition for definition in applicable if definition.specialized]
        generic_observed = [
            definition
            for definition in generic_applicable
            if definition.metric_id in values
        ]
        required_observed = [definition for definition in required if definition.metric_id in values]
        specialized_observed = [definition for definition in specialized if definition.metric_id in values]
        specialized_coverage = len(specialized_observed) / len(specialized) if specialized else 1.0
        component_values: dict[str, float] = {}
        component_coverage: dict[str, dict[str, int]] = {}
        for component, field in COMPONENT_FIELD.items():
            component_metrics = [
                item
                for item in generic_applicable
                if item.component == component
            ]
            available_scores: list[float] = []
            ticker_percentiles = percentiles.get(ticker, {})
            for item in component_metrics:
                score = ticker_percentiles.get(item.metric_id)
                if score is not None:
                    available_scores.append(score)
            component_coverage[component] = {
                "observed": len(available_scores),
                "applicable": len(component_metrics),
            }
            if available_scores:
                component_values[field] = sum(available_scores) / len(available_scores)
        weighted = [
            (component_values[field], component_weights[component])
            for component, field in COMPONENT_FIELD.items()
            if field in component_values and component_weights[component] > 0
        ]
        total_weight = sum(weight for _, weight in weighted)
        final_score = sum(value * weight for value, weight in weighted) / total_weight if total_weight else 0.0
        active_overlay_metrics = [
            definition
            for definition in specialized
            if overlay_weights.get(definition.metric_id, 0.0) > 0.0
        ]
        if len(active_overlay_metrics) > 1:
            raise ValueError(
                f"{ticker}: multiple active specialized overlays apply="
                f"{sorted(item.metric_id for item in active_overlay_metrics)}"
            )
        if active_overlay_metrics:
            overlay = active_overlay_metrics[0]
            overlay_percentile = percentiles.get(ticker, {}).get(overlay.metric_id)
            if overlay_percentile is not None:
                weight = overlay_weights[overlay.metric_id]
                final_score = (
                    (1.0 - weight) * final_score
                    + weight * overlay_percentile
                )
        availability_fraction = (
            len(generic_observed) / len(generic_applicable)
            if generic_applicable
            else 0.0
        )
        required_fraction = len(required_observed) / len(required) if required else 1.0
        profile_factor = max(0.0, min(1.0, financial_confidence or 0.0))
        market_factor = 1.0 if str(market.get("market_data_quality") or "") == "complete" else 0.5
        score_confidence = max(
            0.0,
            min(1.0, 0.45 * availability_fraction + 0.25 * required_fraction + 0.20 * profile_factor + 0.10 * market_factor),
        )
        reasons: list[str] = []
        latest_bar = str(market.get("latest_bar_date") or "")
        stale_days = _days_between(asof, latest_bar) if latest_bar else None
        adv = _number(market.get("avg_dollar_volume_60d"))
        if not market:
            reasons.append("missing_market_features")
        if stale_days is None or stale_days < 0 or stale_days > max_staleness_days:
            reasons.append("stale_or_invalid_market_features")
        if adv is None or adv < minimum_avg_dollar_volume:
            reasons.append("insufficient_liquidity")
        missing_required = sorted(item.metric_id for item in required if item.metric_id not in values)
        if missing_required:
            reasons.append("missing_required_metrics:" + ",".join(missing_required))
        if overlay_active and specialized_coverage < minimum_specialized_coverage:
            reasons.append(f"specialized_coverage_below_{minimum_specialized_coverage:.2f}")
        if score_confidence < minimum_score_confidence:
            reasons.append(f"score_confidence_below_{minimum_score_confidence:.2f}")
        rank_ready_policy = str(policy.get("rank_ready_policy") or "")
        financial_quality = str(financial.get("data_quality_status") or "")
        if not rank_ready_policy.startswith("eligible"):
            reasons.append("financial_policy_not_rank_ready")
        if financial_quality != "complete":
            reasons.append("financial_data_quality_not_complete")
        if financial_confidence is None or financial_confidence < minimum_financial_confidence:
            reasons.append("financial_confidence_below_policy_minimum")
        policy_pass = (
            rank_ready_policy.startswith("eligible")
            and financial_quality == "complete"
            and financial_confidence is not None
            and financial_confidence >= minimum_financial_confidence
        )
        rank_ready = not reasons
        row: dict[str, Any] = {
            "asof_date": asof,
            "ticker": ticker,
            "company_name": member["company_name"],
            "sector": member["sector"],
            "industry": industry,
            "industry_aggregate": "Transportation",
            "subsector": member["subsector"],
            "calibration_cohort": cohort,
            "calibration_cohort_name": member["calibration_cohort"],
            "calibration_use": member["calibration_use"],
            "development_stage": stage,
            "membership_source_id": member["membership_source_id"],
            "membership_basis": member["membership_basis"],
            "membership_start_date": member["start_date"],
            "membership_end_date": member["end_date"] or "",
            "membership_status": member["membership_status"],
            "membership_confidence": _fmt(member["confidence"]),
            "market_feature_asof_date": market.get("asof_date", ""),
            "market_feature_source_id": market.get("source_id", ""),
            "latest_bar_date": latest_bar,
            "market_data_quality": market.get("market_data_quality", ""),
            "avg_dollar_volume_60d": _fmt(adv),
            "financial_feature_asof_date": financial.get("asof_date", ""),
            "financial_feature_source_id": financial.get("source_id", ""),
            "reporting_profile": reporting_profile,
            "reporting_standard": profile.get("reporting_standard") or financial.get("reporting_standard", ""),
            "financial_confidence": _fmt(financial_confidence),
            "financial_data_quality_status": financial_quality,
            "financial_fallback_status": financial.get("financial_fallback_status", ""),
            "metric_registry_version": registry_version,
            "metric_values_json": json.dumps(values, sort_keys=True, separators=(",", ":")),
            "metric_status_json": json.dumps(statuses, sort_keys=True, separators=(",", ":")),
            "component_coverage_json": json.dumps(component_coverage, sort_keys=True, separators=(",", ":")),
            "applicable_metric_count": len(applicable),
            "observed_metric_count": len(observed),
            "required_metric_count": len(required),
            "required_metric_observed_count": len(required_observed),
            "specialized_metric_count": len(specialized),
            "specialized_metric_observed_count": len(specialized_observed),
            "specialized_coverage": _fmt(specialized_coverage),
            "rank_ready_policy": rank_ready_policy,
            "minimum_financial_confidence": _fmt(minimum_financial_confidence),
            "policy_valid_from": policy.get("valid_from", ""),
            "policy_gate_status": "pass" if policy_pass else "blocked",
            "score_input_available_count": len(generic_observed),
            "score_input_total_count": len(generic_applicable),
            "score_confidence": _fmt(score_confidence),
            "final_score": _fmt(max(0.0, min(100.0, final_score))),
            "rank_ready_flag": int(rank_ready),
            "rank_ready_reason": "ok" if rank_ready else ";".join(reasons),
            "model_status": "complete" if rank_ready else "incomplete",
        }
        for field in COMPONENT_FIELD.values():
            row[field] = _fmt(component_values.get(field))
        output.append({field: str(row.get(field, "")) for field in SCORING_FEATURE_FIELDS})
    errors = validate_scoring_rows(output, asof=asof)
    if errors:
        raise ValueError("; ".join(errors[:20]))
    return sorted(output, key=lambda item: item["ticker"])


def finalize_rank_rows(
    rows: list[dict[str, str]],
    *,
    score_model_version: str,
    model_version: str,
    scoring_contract_version: str,
    production_lock: ProductionLock | None = None,
) -> list[dict[str, str]]:
    if not rows:
        raise ValueError("Cannot finalize an empty transportation score contract")
    prepared = [dict(row) for row in rows]
    active_score_model_version = score_model_version
    active_model_version = model_version
    active_contract_version = scoring_contract_version
    if production_lock is not None:
        if set(production_lock.weights) != set(COMPONENT_FIELD.values()):
            raise ValueError(
                "Production lock weights do not match transportation components"
            )
        active_score_model_version = production_lock.score_model_version
        active_model_version = production_lock.score_model_version
        active_contract_version = "transportation_final_rank_table_v1_production"
        for source in prepared:
            score = weighted_score(source, production_lock.weights)
            if score is None:
                raise ValueError(f"{source.get('ticker')}: locked score is unavailable")
            source["final_score"] = _fmt(max(0.0, min(100.0, score)))
    ordered = sorted(
        prepared,
        key=lambda row: (
            -int(str(row.get("rank_ready_flag") or "0")),
            -float(str(row.get("final_score") or "0")),
            str(row.get("ticker") or ""),
        ),
    )
    cohort_order: dict[str, list[str]] = defaultdict(list)
    for row in ordered:
        cohort_order[str(row["calibration_cohort"])].append(str(row["ticker"]))
    cohort_rank = {
        ticker: rank
        for tickers in cohort_order.values()
        for rank, ticker in enumerate(tickers, start=1)
    }
    final: list[dict[str, str]] = []
    for rank, source in enumerate(ordered, start=1):
        ticker = str(source["ticker"])
        rank_ready = str(source.get("rank_ready_flag") or "0") == "1"
        row = {field: str(source.get(field) or "") for field in SCORING_FEATURE_FIELDS}
        production = production_lock is not None
        candidate_reason = (
            "ok" if rank_ready else str(source.get("rank_ready_reason") or "not_rank_ready")
        )
        row.update(
            {
                "final_rank": str(rank),
                "cohort_rank": str(cohort_rank[ticker]),
                "score_model_version": active_score_model_version,
                "model_version": active_model_version,
                "scoring_contract_version": active_contract_version,
                "portfolio_candidate_gate": "1" if production and rank_ready else "0",
                "portfolio_candidate_score": str(source.get("final_score") or ""),
                "portfolio_candidate_status": (
                    "eligible" if production and rank_ready else
                    "not_eligible" if production else "shadow_only"
                ),
                "portfolio_candidate_reason": (
                    candidate_reason if production else
                    "shadow_only_oos_calibration_not_available"
                ),
                "calibration_eligible_flag": "1" if rank_ready else "0",
                "research_calibration_input_eligible_flag": "0",
                "research_calibration_reason": "current_snapshot_not_survivorship_corrected",
                "calibration_sample_role": "strict_oos" if production else "excluded",
                "stage11_calibration_panel_source": "current_transportation_dashboard_snapshot",
                "stage11_calibration_input_eligible_flag": "0",
                "stage11_calibration_input_reason": "current_snapshot_not_survivorship_corrected",
                "survivorship_corrected_panel_flag": "0",
                "oos_score_valid_flag": "1" if production else "0",
                "oos_score_asof_date": str(source.get("asof_date") or "") if production else "",
                "oos_invalid_reason": "" if production else "shadow_pre_oos_calibration",
                "calibration_lock_date": production_lock.lock_date.isoformat() if production_lock else "",
            }
        )
        final.append({field: row.get(field, "") for field in FINAL_RANK_FIELDS})
    errors = validate_rank_rows(final, asof=str(final[0]["asof_date"]))
    if errors:
        raise ValueError("; ".join(errors[:20]))
    return final


def publish_dashboard(
    *,
    output_dir: Path,
    rows: list[dict[str, str]],
    asof: str,
    allow_overwrite: bool,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rank_path = output_dir / "transportation_final_rank_table.csv"
    sidecar_path = output_dir / "transportation_stage11_survivorship_calibration_panel.csv"
    manifest_path = output_dir / "transportation_final_rank_table_manifest.json"
    existing = [
        path
        for path in (rank_path, sidecar_path, manifest_path)
        if path.exists()
    ]
    if existing and not allow_overwrite:
        raise FileExistsError(
            "Refusing to overwrite immutable transportation dashboard artifacts: "
            f"{existing}"
        )
    errors = validate_rank_rows(rows, asof=asof)
    if errors:
        raise ValueError("; ".join(errors[:20]))
    sidecar_rows = build_shadow_survivorship_sidecar(
        rows,
        fieldnames=FINAL_RANK_FIELDS,
    )
    sidecar_errors = validate_shadow_survivorship_sidecar(
        sidecar_rows,
        asof_date=asof,
        expected_tickers={row["ticker"] for row in rows},
    )
    if sidecar_errors:
        raise ValueError("; ".join(sidecar_errors[:20]))
    write_rank_rows(rank_path, rows)
    write_rank_rows(sidecar_path, sidecar_rows)
    manifest = {
        "acceptance": "PASS",
        "model_family": MODEL_FAMILY,
        "asof_date": asof,
        "rank_table": str(rank_path),
        "rank_table_sha256": file_sha256(rank_path),
        "stage11_survivorship_calibration_panel": str(sidecar_path),
        "stage11_survivorship_calibration_panel_sha256": file_sha256(sidecar_path),
        "stage11_survivorship_calibration_panel_row_count": len(sidecar_rows),
        "stage11_calibration_input_eligible_count": sum(
            row["stage11_calibration_input_eligible_flag"] == "1"
            for row in sidecar_rows
        ),
        "row_count": len(rows),
        "rank_ready_count": sum(row["rank_ready_flag"] == "1" for row in rows),
        "portfolio_candidate_count": sum(
            row["portfolio_candidate_gate"] == "1" for row in rows
        ),
        "oos_score_valid_count": sum(
            row["oos_score_valid_flag"] == "1" for row in rows
        ),
        "contract_fields": FINAL_RANK_FIELDS,
        "scoring_contract_versions": sorted(
            {row["scoring_contract_version"] for row in rows}
        ),
        "published_at_utc": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }
    write_manifest(manifest_path, manifest)
    return manifest

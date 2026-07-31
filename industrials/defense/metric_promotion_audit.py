from __future__ import annotations

import csv
import hashlib
import json
import math
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from industrials.core.config import cfg_get
from industrials.defense.metric_contract import (
    BASE_FEATURE_ALIASES,
    PILLAR_INPUT_FIELDS,
    SPECIALIZED_SOURCE_COLUMNS,
    STRUCTURALLY_DISABLED_PILLARS,
)
from industrials.defense.research_artifacts import (
    PILLAR_SCORE_FIELDS,
    as_float,
    sha256_file,
    spearman,
    utc_now,
)


AUDIT_VERSION = "defense_metric_promotion_audit_v1"
MODEL_FAMILY = "defense"
HISTORY_START = "2019-01-04"

EXPECTED_COLUMNS = {
    "feature_market_technical": tuple(
        """
        ticker asof_date source_id model_family latest_close latest_adj_close
        latest_volume trading_days_available latest_bar_date stale_days stale_flag
        low_history_flag low_liquidity_flag ret_1m ret_3m ret_6m ret_12m_ex_1m
        rel_strength_bench_3m rel_strength_xar_3m rel_strength_ita_3m
        rel_strength_spy_3m avg_volume_20d avg_volume_60d avg_dollar_volume_20d
        avg_dollar_volume_60d realized_vol_60d max_drawdown_6m max_drawdown_12m
        distance_from_52w_high ma_50d ma_200d above_ma_50d above_ma_200d
        market_data_quality created_at updated_at rel_strength_bench2_3m
        rel_strength_bench3_3m rel_strength_bench4_3m
        """.split()
    ),
    "feature_positioning": tuple(
        """
        ticker asof_date source_id model_family insider_purchase_count_90d
        insider_purchase_value_90d insider_sale_count_90d insider_sale_value_90d
        insider_cluster_buyers_90d insider_net_value_90d
        latest_institutional_shares latest_institutional_value latest_manager_count
        institutional_ownership_delta_pct latest_short_interest_shares
        latest_short_interest_pct_float latest_days_to_cover
        short_interest_change_3m latest_borrow_fee_rate positioning_quality created_at
        updated_at form4_status form4_status_reason
        """.split()
    ),
    "fact_market_snapshot": tuple(
        """
        ticker asof_date source_id market_cap shares_outstanding
        regular_market_price currency quote_type exchange source_timestamp
        payload_json created_at updated_at
        """.split()
    ),
    "feature_financial_statement": tuple(
        """
        ticker asof_date source_id model_family accession_number form_type
        fiscal_period_end fiscal_year fiscal_period reporting_standard
        reporting_profile financial_frequency reported_currency
        fx_conversion_status fx_rate_income_statement fx_rate_balance_sheet revenue
        cost_of_sales gross_profit operating_income net_income eps_diluted assets
        liabilities equity cash_and_equivalents total_debt inventory
        accounts_receivable accounts_payable operating_cash_flow capex free_cash_flow
        research_and_development stock_based_compensation diluted_shares revenue_usd
        gross_profit_usd operating_income_usd net_income_usd operating_cash_flow_usd
        capex_usd free_cash_flow_usd assets_usd liabilities_usd equity_usd
        cash_and_equivalents_usd total_debt_usd inventory_usd
        accounts_receivable_usd accounts_payable_usd revenue_ttm gross_profit_ttm
        operating_income_ttm net_income_ttm free_cash_flow_ttm gross_margin
        operating_margin fcf_margin r_and_d_pct_revenue sbc_pct_revenue net_cash
        net_cash_to_assets inventory_days days_sales_outstanding
        days_payables_outstanding cash_conversion_cycle revenue_yoy_growth
        gross_profit_yoy_growth operating_income_yoy_growth free_cash_flow_yoy_growth
        revenue_acceleration fcf_to_net_income fcf_yield ev_gross_profit
        ev_operating_income market_cap latest_price deferred_revenue
        contract_liabilities remaining_performance_obligation book_to_bill
        funded_backlog development_stage financial_confidence
        financial_fallback_status canonical_quality data_quality_status review_reason
        created_at updated_at revenue_stub_annualized revenue_stub_annualized_usd
        revenue_stub_period_days revenue_stub_quality revenue_ttm_usd
        gross_profit_ttm_usd operating_income_ttm_usd net_income_ttm_usd
        free_cash_flow_ttm_usd net_cash_usd depreciation_and_amortization
        interest_expense pretax_income income_tax_expense orders
        depreciation_and_amortization_usd interest_expense_usd orders_usd
        depreciation_and_amortization_ttm depreciation_and_amortization_ttm_usd
        interest_expense_ttm interest_expense_ttm_usd orders_ttm orders_ttm_usd
        funded_backlog_usd orders_yoy_growth backlog_yoy_growth backlog_to_revenue
        invested_capital_usd roic asset_turnover incremental_operating_margin
        inventory_growth inventory_sales_growth_spread cash_conversion_cycle_change
        ebitda_ttm_usd net_debt_to_ebitda interest_coverage cash_burn_ttm_usd
        cash_runway_years diluted_shares_yoy_growth equity_issuance_proceeds
        debt_issuance_proceeds equity_issuance_proceeds_usd
        debt_issuance_proceeds_usd equity_issuance_proceeds_ttm
        equity_issuance_proceeds_ttm_usd debt_issuance_proceeds_ttm
        debt_issuance_proceeds_ttm_usd gross_capital_raised_ttm_usd
        capital_raise_dependence reported_backlog reported_backlog_usd
        remaining_performance_obligation_usd rpo_current rpo_current_usd
        reported_backlog_yoy_growth reported_backlog_to_revenue rpo_yoy_growth
        rpo_to_revenue rpo_implied_orders rpo_implied_orders_usd
        rpo_implied_book_to_bill financial_metric_reported_count
        financial_metric_proxy_count financial_metric_unavailable_count
        financial_metric_classified_fraction roic_not_meaningful_flag
        negative_ebitda_leverage_flag operating_cash_flow_ttm
        operating_cash_flow_ttm_usd capex_ttm capex_ttm_usd contract_load_proxy
        contract_load_proxy_usd contract_load_proxy_source
        contract_load_proxy_yoy_growth contract_load_proxy_to_revenue
        """.split()
    ),
}

METADATA_COLUMNS = {
    "feature_market_technical": {
        "ticker",
        "asof_date",
        "source_id",
        "model_family",
        "latest_bar_date",
        "created_at",
        "updated_at",
    },
    "feature_positioning": {
        "ticker",
        "asof_date",
        "source_id",
        "model_family",
        "created_at",
        "updated_at",
        "form4_status_reason",
    },
    "fact_market_snapshot": {
        "ticker",
        "asof_date",
        "source_id",
        "currency",
        "quote_type",
        "exchange",
        "source_timestamp",
        "payload_json",
        "created_at",
        "updated_at",
    },
    "feature_financial_statement": {
        "ticker",
        "asof_date",
        "source_id",
        "model_family",
        "accession_number",
        "form_type",
        "fiscal_period_end",
        "fiscal_year",
        "fiscal_period",
        "reporting_standard",
        "reporting_profile",
        "financial_frequency",
        "reported_currency",
        "contract_load_proxy_source",
        "created_at",
        "updated_at",
        "review_reason",
    },
}

QUALITY_GATE_COLUMNS = {
    "feature_market_technical": {
        "trading_days_available",
        "stale_days",
        "stale_flag",
        "low_history_flag",
        "low_liquidity_flag",
        "market_data_quality",
    },
    "feature_positioning": {"positioning_quality", "form4_status"},
    "feature_financial_statement": {
        "fx_conversion_status",
        "development_stage",
        "financial_confidence",
        "financial_fallback_status",
        "canonical_quality",
        "data_quality_status",
        "revenue_stub_quality",
        "financial_metric_reported_count",
        "financial_metric_proxy_count",
        "financial_metric_unavailable_count",
        "financial_metric_classified_fraction",
        "roic_not_meaningful_flag",
        "negative_ebitda_leverage_flag",
    },
    "fact_market_snapshot": set(),
}

SHADOW_CANDIDATE_FINANCIAL_COLUMNS = {
    "days_sales_outstanding",
    "days_payables_outstanding",
    "cash_conversion_cycle",
    "roic",
    "asset_turnover",
    "incremental_operating_margin",
    "inventory_growth",
    "inventory_sales_growth_spread",
    "cash_conversion_cycle_change",
    "net_debt_to_ebitda",
    "interest_coverage",
    "cash_burn_ttm_usd",
    "cash_runway_years",
    "diluted_shares_yoy_growth",
    "gross_capital_raised_ttm_usd",
    "capital_raise_dependence",
}

PRODUCTION_EXPORT_COLUMNS = {
    "feature_market_technical": {
        "latest_close",
        "latest_adj_close",
        "latest_volume",
        "avg_dollar_volume_60d",
    },
    "feature_positioning": {
        "latest_short_interest_shares",
    },
    "fact_market_snapshot": {
        "market_cap",
        "shares_outstanding",
        "regular_market_price",
    },
    "feature_financial_statement": {
        "market_cap",
        "latest_price",
    },
}

PILLAR_BY_ALIAS = {
    alias: pillar
    for pillar, aliases in PILLAR_INPUT_FIELDS.items()
    for alias in aliases
}
SOURCE_SCORE_COLUMNS: dict[str, dict[str, str]] = {
    table: {} for table in EXPECTED_COLUMNS
}
for alias, source_name in BASE_FEATURE_ALIASES.items():
    prefix, column = source_name.split("_", 1)
    table = {
        "financial": "feature_financial_statement",
        "market": "feature_market_technical",
        "positioning": "feature_positioning",
    }[prefix]
    SOURCE_SCORE_COLUMNS[table][column] = PILLAR_BY_ALIAS.get(alias, "export")
SOURCE_SCORE_COLUMNS["feature_market_technical"]["low_liquidity_flag"] = "risk_control"

INVENTORY_FIELDS = [
    "table_name",
    "column_name",
    "domain",
    "disposition",
    "pillar_or_consumer",
    "production_consumed_flag",
    "promotion_candidate_flag",
    "current_denominator",
    "current_non_null_count",
    "current_coverage_pct",
    "current_distinct_value_count",
    "historical_pit_row_count",
    "historical_non_null_count",
    "historical_coverage_pct",
    "historical_ticker_count",
    "first_available_asof",
    "last_available_asof",
    "notes",
]
PIT_FIELDS = [
    "snapshot_root",
    "pillar",
    "snapshot_count",
    "row_count",
    "non_null_count",
    "coverage_pct",
    "distinct_value_count",
    "constant_snapshot_count",
    "status_counts_json",
]
OVERLAP_FIELDS = [
    "dataset",
    "field_a",
    "field_b",
    "paired_rows",
    "spearman_correlation",
    "absolute_correlation",
    "overlap_class",
]
FINDING_FIELDS = [
    "severity",
    "code",
    "status",
    "detail",
    "evidence",
]


@dataclass(frozen=True)
class MetricDisposition:
    domain: str
    disposition: str
    consumer: str
    production_consumed: bool
    promotion_candidate: bool
    notes: str


def classify_column(table: str, column: str) -> MetricDisposition:
    domain = {
        "feature_market_technical": "market",
        "feature_positioning": "positioning",
        "feature_financial_statement": "financial",
        "fact_market_snapshot": "market_snapshot",
    }[table]
    if column not in EXPECTED_COLUMNS[table]:
        return MetricDisposition(domain, "unclassified", "", False, False, "schema column is not in the audit contract")
    if column in METADATA_COLUMNS[table]:
        return MetricDisposition(domain, "contract_metadata", "lineage", False, False, "")
    pillar = SOURCE_SCORE_COLUMNS[table].get(column)
    if pillar and pillar != "export":
        return MetricDisposition(domain, "production_scoring_input", pillar, True, False, "")
    if column in SPECIALIZED_SOURCE_COLUMNS:
        return MetricDisposition(
            domain,
            "specialized_candidate_input",
            "defense_budget_backlog",
            False,
            True,
            "Reviewed dedicated-parser source or derived demand-cycle feature.",
        )
    if column in SHADOW_CANDIDATE_FINANCIAL_COLUMNS:
        return MetricDisposition(
            domain,
            "shadow_candidate_input",
            "unassigned_candidate",
            False,
            True,
            "Available but requires an isolated PIT/OOS candidate test before scoring use.",
        )
    if column in QUALITY_GATE_COLUMNS[table]:
        return MetricDisposition(domain, "eligibility_or_quality_gate", "eligibility", True, False, "")
    if column in PRODUCTION_EXPORT_COLUMNS[table] or pillar == "export":
        return MetricDisposition(domain, "production_export_or_capacity", "rank_table", True, False, "")
    return MetricDisposition(
        domain,
        "diagnostic_or_derived_input",
        "diagnostics",
        False,
        False,
        "Retained for lineage, diagnostics, or construction of another registered metric.",
    )


def placeholders(values: Iterable[object]) -> str:
    materialized = list(values)
    if not materialized:
        raise ValueError("Cannot build placeholders for an empty sequence")
    return ",".join("?" for _ in materialized)


def table_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})"))


def source_ids_for_table(config: dict[str, Any], table: str) -> list[str]:
    if table in {"feature_market_technical", "fact_market_snapshot"}:
        primary = str(cfg_get(config, "market_feature_build.source_id", "yahoo_finance_adjusted"))
        raw_fallbacks = cfg_get(config, "market_data_policy.scoring_fallback_sources", []) or []
        fallbacks = (
            [part.strip() for part in raw_fallbacks.split(",")]
            if isinstance(raw_fallbacks, str)
            else [str(part).strip() for part in raw_fallbacks]
        )
        return list(dict.fromkeys(value for value in [primary, *fallbacks] if value))
    if table == "feature_financial_statement":
        return [str(cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts"))]
    return [str(cfg_get(config, "positioning_import.source_id", "industrials_positioning_composite"))]


def active_tickers(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row["ticker"])
        for row in conn.execute(
            """
            SELECT DISTINCT c.ticker
            FROM dim_company AS c
            JOIN dim_industrials_taxonomy AS t
              ON t.company_id = c.company_id
             AND t.model_family = 'defense'
            WHERE c.is_active = 1
            ORDER BY c.ticker
            """
        )
    ]


def universe_counts(conn: sqlite3.Connection, asof: str) -> tuple[int, int]:
    active = len(active_tickers(conn))
    all_members = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT ticker)
            FROM dim_universe_membership
            WHERE model_family = 'defense'
              AND start_date <= ?
            """,
            (asof,),
        ).fetchone()[0]
        or 0
    )
    return active, all_members - active


def coverage_stats(
    conn: sqlite3.Connection,
    *,
    table: str,
    column: str,
    sources: list[str],
    asof: str,
    active: list[str],
) -> dict[str, Any]:
    date_column = "asof_date"
    source_clause = placeholders(sources)
    active_clause = placeholders(active)
    current = conn.execute(
        f"""
        SELECT COUNT(DISTINCT CASE WHEN t.{column} IS NOT NULL THEN t.ticker END) AS covered,
               COUNT(DISTINCT CASE WHEN t.{column} IS NOT NULL
                                   THEN CAST(t.{column} AS TEXT) END) AS distinct_values
        FROM {table} AS t
        WHERE t.source_id IN ({source_clause})
          AND t.{date_column} = ?
          AND t.ticker IN ({active_clause})
        """,
        (*sources, asof, *active),
    ).fetchone()
    model_clause = "AND t.model_family = 'defense'" if table != "fact_market_snapshot" else ""
    historical = conn.execute(
        f"""
        WITH eligible AS (
            SELECT t.ticker, t.{date_column} AS asof_date,
                   MAX(CASE WHEN t.{column} IS NOT NULL THEN 1 ELSE 0 END) AS has_value
            FROM {table} AS t
            WHERE t.source_id IN ({source_clause})
              {model_clause}
              AND t.{date_column} BETWEEN ? AND ?
              AND EXISTS (
                  SELECT 1
                  FROM dim_universe_membership AS m
                  WHERE m.model_family = 'defense'
                    AND m.ticker = t.ticker
                    AND m.point_in_time_flag = 1
                    AND m.start_date <= t.{date_column}
                    AND COALESCE(m.end_date, '9999-12-31') >= t.{date_column}
              )
            GROUP BY t.ticker, t.{date_column}
        )
        SELECT COUNT(*) AS pit_rows,
               SUM(has_value) AS non_null_rows,
               COUNT(DISTINCT CASE WHEN has_value = 1 THEN ticker END) AS tickers,
               MIN(CASE WHEN has_value = 1 THEN asof_date END) AS first_asof,
               MAX(CASE WHEN has_value = 1 THEN asof_date END) AS last_asof
        FROM eligible
        """,
        (*sources, HISTORY_START, asof),
    ).fetchone()
    denominator = len(active)
    covered = int(current["covered"] or 0)
    pit_rows = int(historical["pit_rows"] or 0)
    pit_non_null = int(historical["non_null_rows"] or 0)
    return {
        "current_denominator": denominator,
        "current_non_null_count": covered,
        "current_coverage_pct": covered / denominator if denominator else None,
        "current_distinct_value_count": int(current["distinct_values"] or 0),
        "historical_pit_row_count": pit_rows,
        "historical_non_null_count": pit_non_null,
        "historical_coverage_pct": pit_non_null / pit_rows if pit_rows else None,
        "historical_ticker_count": int(historical["tickers"] or 0),
        "first_available_asof": str(historical["first_asof"] or ""),
        "last_available_asof": str(historical["last_asof"] or ""),
    }


def build_inventory(
    conn: sqlite3.Connection,
    *,
    config: dict[str, Any],
    asof: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    active = active_tickers(conn)
    rows: list[dict[str, Any]] = []
    drift: list[str] = []
    for table, expected in EXPECTED_COLUMNS.items():
        actual = table_columns(conn, table)
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        drift.extend(f"{table}:missing:{column}" for column in missing)
        drift.extend(f"{table}:unexpected:{column}" for column in unexpected)
        sources = source_ids_for_table(config, table)
        for column in actual:
            disposition = classify_column(table, column)
            stats: dict[str, Any] = {
                "current_denominator": len(active),
                "current_non_null_count": "",
                "current_coverage_pct": "",
                "current_distinct_value_count": "",
                "historical_pit_row_count": "",
                "historical_non_null_count": "",
                "historical_coverage_pct": "",
                "historical_ticker_count": "",
                "first_available_asof": "",
                "last_available_asof": "",
            }
            if disposition.disposition != "contract_metadata":
                stats = coverage_stats(
                    conn,
                    table=table,
                    column=column,
                    sources=sources,
                    asof=asof,
                    active=active,
                )
            rows.append(
                {
                    "table_name": table,
                    "column_name": column,
                    "domain": disposition.domain,
                    "disposition": disposition.disposition,
                    "pillar_or_consumer": disposition.consumer,
                    "production_consumed_flag": int(disposition.production_consumed),
                    "promotion_candidate_flag": int(disposition.promotion_candidate),
                    **stats,
                    "notes": disposition.notes,
                }
            )
    return rows, drift


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {str(key): str(value or "") for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def candidate_snapshot_coverage(snapshot_root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    totals = {
        field: {
            "rows": 0,
            "non_null": 0,
            "values": set(),
            "constant_snapshots": 0,
            "statuses": Counter(),
        }
        for field in PILLAR_SCORE_FIELDS
    }
    snapshot_count = 0
    hash_issues: list[str] = []
    for child in sorted(snapshot_root.iterdir()) if snapshot_root.is_dir() else []:
        try:
            date.fromisoformat(child.name)
        except ValueError:
            continue
        csv_path = child / "defense_final_rank_table.csv"
        manifest_path = child / "defense_final_rank_table_manifest.json"
        if not csv_path.is_file() or not manifest_path.is_file():
            hash_issues.append(f"{child.name}:missing_csv_or_manifest")
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        actual_hash = sha256_file(csv_path)
        if str(manifest.get("sha256") or "") != actual_hash:
            hash_issues.append(f"{child.name}:sha256_mismatch")
        if str(manifest.get("asof_date") or "") != child.name:
            hash_issues.append(f"{child.name}:manifest_asof_mismatch")
        rows = read_csv_rows(csv_path)
        snapshot_count += 1
        for field in PILLAR_SCORE_FIELDS:
            values = [
                value
                for row in rows
                if (value := as_float(row.get(field))) is not None
            ]
            total = totals[field]
            total["rows"] += len(rows)
            total["non_null"] += len(values)
            total["values"].update(values)
            if len(set(values)) <= 1:
                total["constant_snapshots"] += 1
            status_field = field.replace("_score", "_status")
            total["statuses"].update(
                str(row.get(status_field) or "missing_status") for row in rows
            )
    output: list[dict[str, Any]] = []
    for field, total in totals.items():
        row_count = int(total["rows"])
        non_null = int(total["non_null"])
        output.append(
            {
                "snapshot_root": str(snapshot_root),
                "pillar": field,
                "snapshot_count": snapshot_count,
                "row_count": row_count,
                "non_null_count": non_null,
                "coverage_pct": non_null / row_count if row_count else None,
                "distinct_value_count": len(total["values"]),
                "constant_snapshot_count": int(total["constant_snapshots"]),
                "status_counts_json": json.dumps(
                    dict(sorted(total["statuses"].items())),
                    sort_keys=True,
                ),
            }
        )
    return output, hash_issues


def pairwise_overlap(
    rows: list[dict[str, str]],
    *,
    fields: list[str],
    dataset: str,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for index, field_a in enumerate(fields):
        for field_b in fields[index + 1 :]:
            pairs = [
                (left, right)
                for row in rows
                if (left := as_float(row.get(field_a))) is not None
                and (right := as_float(row.get(field_b))) is not None
            ]
            correlation = (
                spearman(
                    [left for left, _ in pairs],
                    [right for _, right in pairs],
                )
                if len(pairs) >= 3
                else None
            )
            absolute = abs(correlation) if correlation is not None else None
            overlap_class = (
                "very_high"
                if absolute is not None and absolute >= 0.90
                else "high"
                if absolute is not None and absolute >= 0.75
                else "moderate"
                if absolute is not None and absolute >= 0.50
                else "low"
                if absolute is not None
                else "insufficient"
            )
            output.append(
                {
                    "dataset": dataset,
                    "field_a": field_a,
                    "field_b": field_b,
                    "paired_rows": len(pairs),
                    "spearman_correlation": correlation,
                    "absolute_correlation": absolute,
                    "overlap_class": overlap_class,
                }
            )
    return output


def verify_candidate_manifest(path: Path) -> tuple[list[str], dict[str, Any]]:
    if not path.is_file():
        return [f"missing candidate comparison manifest: {path}"], {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    issues: list[str] = []
    for artifact_name, sides in (payload.get("inputs") or {}).items():
        if not isinstance(sides, dict):
            issues.append(f"{artifact_name}:invalid_input_metadata")
            continue
        for side in ("baseline", "candidate"):
            artifact = Path(str(sides.get(side) or ""))
            expected_hash = str(sides.get(f"{side}_sha256") or "")
            if not artifact.is_file():
                issues.append(f"{artifact_name}:{side}:missing")
            elif not expected_hash or sha256_file(artifact) != expected_hash:
                issues.append(f"{artifact_name}:{side}:sha256_mismatch")
    if payload.get("promotable_evidence") is not True:
        issues.append("candidate comparison is not promotable")
    if payload.get("failed_gates"):
        issues.append(f"candidate comparison failed_gates={payload['failed_gates']}")
    return issues, payload


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def production_constant_pillars(
    rank_rows: list[dict[str, str]],
    promotion_manifest: dict[str, Any],
) -> list[tuple[str, float, int]]:
    raw_weights = (
        (promotion_manifest.get("promotion_payload") or {}).get("weights") or {}
    )
    output: list[tuple[str, float, int]] = []
    for field in PILLAR_SCORE_FIELDS:
        weight = as_float(raw_weights.get(field)) or 0.0
        values = {
            value
            for row in rank_rows
            if (value := as_float(row.get(field))) is not None
        }
        if weight > 0 and len(values) <= 1:
            output.append((field, weight, len(values)))
    return output


def candidate_calibration_identification_issues(
    panel_rows: list[dict[str, str]],
    comparison_manifest: dict[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    inputs = comparison_manifest.get("inputs") or {}
    summary_path = Path(
        str((inputs.get("calibration_summary") or {}).get("candidate") or "")
    )
    manifest_path = Path(
        str((inputs.get("calibration_manifest") or {}).get("candidate") or "")
    )
    issues: list[str] = []
    if not summary_path.is_file():
        return [f"candidate calibration summary missing: {summary_path}"], {}
    if not manifest_path.is_file():
        return [f"candidate calibration manifest missing: {manifest_path}"], {}

    summary_rows = read_csv_rows(summary_path)
    calibration_manifest = load_json(manifest_path)
    if len(summary_rows) != 1:
        issues.append(
            f"candidate calibration summary must have one row; found {len(summary_rows)}"
        )
        return issues, {"manifest": calibration_manifest}

    summary = summary_rows[0]
    try:
        weights_payload = json.loads(str(summary.get("best_weights_json") or ""))
    except json.JSONDecodeError as exc:
        issues.append(f"candidate best_weights_json is invalid: {exc}")
        weights_payload = {}
    if not isinstance(weights_payload, dict):
        issues.append("candidate best_weights_json must be an object")
        weights_payload = {}
    weights = {
        field: as_float(weights_payload.get(field)) or 0.0
        for field in PILLAR_SCORE_FIELDS
    }
    missing_weight_fields = sorted(set(PILLAR_SCORE_FIELDS) - set(weights_payload))
    if missing_weight_fields:
        issues.append(f"candidate weights missing pillars: {missing_weight_fields}")

    calibration_rows = [
        row
        for row in panel_rows
        if row.get("panel_row_eligible_flag") == "1"
        and row.get("split_name") in {"train", "validation"}
    ]
    constant_pillars: list[str] = []
    distinct_counts: dict[str, int] = {}
    for field in PILLAR_SCORE_FIELDS:
        distinct = {
            value
            for row in calibration_rows
            if (value := as_float(row.get(field))) is not None
        }
        distinct_counts[field] = len(distinct)
        if len(distinct) <= 1:
            constant_pillars.append(field)

    expected_inactive = sorted(
        set(constant_pillars) | set(STRUCTURALLY_DISABLED_PILLARS)
    )
    declared_inactive = sorted(
        str(field)
        for field in calibration_manifest.get("inactive_pillars") or []
    )
    if declared_inactive != expected_inactive:
        issues.append(
            "candidate inactive pillars do not match independently computed "
            f"pillars: declared={declared_inactive} expected={expected_inactive}"
        )
    for field in expected_inactive:
        if abs(weights.get(field, 0.0)) > 1e-12:
            issues.append(
                f"candidate inactive pillar {field} has nonzero weight "
                f"{weights[field]:.12f}"
            )
    active_weight_total = sum(weights.values())
    if weights and abs(active_weight_total - 1.0) > 1e-9:
        issues.append(
            f"candidate weights must sum to 1.0; found {active_weight_total:.12f}"
        )
    return issues, {
        "calibration_row_count": len(calibration_rows),
        "distinct_counts": distinct_counts,
        "constant_pillars": constant_pillars,
        "structurally_disabled_pillars": sorted(STRUCTURALLY_DISABLED_PILLARS),
        "declared_inactive_pillars": declared_inactive,
        "best_weights": weights,
    }


def build_findings(
    *,
    conn: sqlite3.Connection,
    asof: str,
    inventory: list[dict[str, Any]],
    schema_drift: list[str],
    pit_rows: list[dict[str, Any]],
    pit_hash_issues: list[str],
    candidate_manifest_path: Path,
    production_rank_path: Path,
    production_rank_manifest_path: Path,
    production_promotion_path: Path,
    parser_promotion_path: Path,
    review_policy_path: Path,
    golden_paths: list[Path],
    orchestration_step_ids: list[str],
    candidate_panel_rows: list[dict[str, str]],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    findings: list[dict[str, str]] = []

    def add(severity: str, code: str, passed: bool, detail: str, evidence: object = "") -> None:
        findings.append(
            {
                "severity": severity,
                "code": code,
                "status": "PASS" if passed else "FAIL",
                "detail": detail,
                "evidence": (
                    evidence
                    if isinstance(evidence, str)
                    else json.dumps(evidence, sort_keys=True, default=str)
                ),
            }
        )

    active_count, historical_count = universe_counts(conn, asof)
    add(
        "critical",
        "defense_universe_contract",
        active_count == 94 and historical_count == 40,
        f"Expected 94 active and 40 historical defense tickers; found {active_count} active and {historical_count} historical.",
    )
    unclassified = [
        f"{row['table_name']}.{row['column_name']}"
        for row in inventory
        if row["disposition"] == "unclassified"
    ]
    add(
        "critical",
        "metric_schema_fully_dispositioned",
        not schema_drift and not unclassified,
        "Every feature and snapshot column must have an explicit audit disposition.",
        {"schema_drift": schema_drift, "unclassified": unclassified},
    )
    add(
        "critical",
        "candidate_weekly_snapshot_hashes",
        not pit_hash_issues and bool(pit_rows) and int(pit_rows[0]["snapshot_count"]) >= 395,
        "Every candidate weekly PIT snapshot must match its manifest and the expected history must be present.",
        pit_hash_issues,
    )
    candidate_issues, candidate_manifest = verify_candidate_manifest(
        candidate_manifest_path
    )
    add(
        "critical",
        "candidate_research_evidence_integrity",
        not candidate_issues,
        "Matched candidate evidence, inputs, and hashes must be valid and promotion gates must have no failures.",
        candidate_issues,
    )
    identification_issues, identification_evidence = (
        candidate_calibration_identification_issues(
            candidate_panel_rows,
            candidate_manifest,
        )
    )
    add(
        "critical",
        "candidate_calibration_weights_identified",
        not identification_issues,
        "Candidate calibration must assign zero weight to every constant or structurally disabled pillar.",
        {
            "issues": identification_issues,
            **identification_evidence,
        },
    )
    rank_manifest = load_json(production_rank_manifest_path)
    rank_hash_ok = (
        production_rank_path.is_file()
        and str(rank_manifest.get("sha256") or "") == sha256_file(production_rank_path)
        and str(rank_manifest.get("asof_date") or "") == asof
    )
    add(
        "critical",
        "production_rank_table_seal",
        rank_hash_ok,
        "The current production dashboard table must match its dated manifest.",
        str(production_rank_path),
    )
    rank_rows = read_csv_rows(production_rank_path) if production_rank_path.is_file() else []
    add(
        "critical",
        "production_rank_row_contract",
        len(rank_rows) == active_count
        and {row.get("asof_date") for row in rank_rows} == {asof},
        "Production rank rows must cover the active universe with one uniform as-of date.",
        {"rank_rows": len(rank_rows), "active_tickers": active_count},
    )
    parser_promotion = load_json(parser_promotion_path)
    parser_ok = (
        parser_promotion.get("status") == "COMPLETED"
        and str(parser_promotion.get("asof_date") or "") == asof
        and int(parser_promotion.get("promoted_count") or 0) > 0
    )
    add(
        "critical",
        "dedicated_parser_production_promotion",
        parser_ok,
        "Reviewed parser evidence must be promoted for the audit as-of date.",
        parser_promotion,
    )
    latest_parser = conn.execute(
        """
        SELECT run_id, asof_date, status, failed_work_count
        FROM sec_parser_run
        WHERE model_family = 'defense'
        ORDER BY run_id DESC
        LIMIT 1
        """
    ).fetchone()
    latest_parser_payload = dict(latest_parser) if latest_parser else {}
    add(
        "critical",
        "latest_parser_run_complete",
        bool(latest_parser)
        and str(latest_parser["asof_date"]) == asof
        and str(latest_parser["status"]) == "COMPLETED"
        and int(latest_parser["failed_work_count"] or 0) == 0,
        "Latest defense parser run must be complete, current, and failure-free.",
        latest_parser_payload,
    )
    policy_rows = read_csv_rows(review_policy_path) if review_policy_path.is_file() else []
    enabled_policy_rows = [
        row
        for row in policy_rows
        if str(row.get("enabled") or "").lower() in {"1", "true", "yes", "y"}
    ]
    golden_counts: dict[str, int] = {}
    golden_ok = True
    for path in golden_paths:
        payload = load_json(path)
        expectations = payload.get("expectations")
        count = len(expectations) if isinstance(expectations, list) else 0
        golden_counts[str(path)] = count
        golden_ok = golden_ok and count > 0
    add(
        "critical",
        "review_policy_and_golden_corpora",
        len(enabled_policy_rows) > 0 and golden_ok,
        "Parser promotion requires attributed review policy rows and nonempty generated/curated golden corpora.",
        {
            "enabled_policy_rows": len(enabled_policy_rows),
            "golden_expectation_counts": golden_counts,
        },
    )
    production_promotion = load_json(production_promotion_path)
    constants = production_constant_pillars(rank_rows, production_promotion)
    add(
        "high",
        "production_weights_identified",
        not constants,
        "A production promotion must not assign nonzero weight to a constant pillar.",
        [
            {"pillar": field, "weight": weight, "distinct_values": distinct}
            for field, weight, distinct in constants
        ],
    )
    candidate_model = ""
    candidate_panel_manifest = (
        (candidate_manifest.get("inputs") or {})
        .get("panel_manifest", {})
        .get("candidate", "")
    )
    if candidate_panel_manifest:
        candidate_model = str(
            load_json(Path(str(candidate_panel_manifest))).get("score_model_version")
            or ""
        )
    production_models = sorted(
        {str(row.get("score_model_version") or "") for row in rank_rows}
    )
    add(
        "high",
        "promotable_candidate_activation_state",
        not candidate_manifest.get("promotable_evidence")
        or (bool(candidate_model) and production_models == [candidate_model]),
        "Once candidate evidence is promotable, sealing requires an explicit activation decision and matching production model version.",
        {
            "candidate_model": candidate_model,
            "production_models": production_models,
        },
    )
    required_order = [
        "07_sync_sec",
        "08d_dedicated_parser_shadow",
        "08e_dedicated_parser_production",
        "08_build_financial",
        "08_validate_financial",
        "17_publish",
    ]
    order_ok = all(item in orchestration_step_ids for item in required_order)
    if order_ok:
        order_ok = [
            orchestration_step_ids.index(item) for item in required_order
        ] == sorted(orchestration_step_ids.index(item) for item in required_order)
    add(
        "high",
        "daily_parser_feature_scoring_order",
        order_ok,
        "Daily production order must be SEC sync -> parser -> parser promotion -> financial build -> validation -> publish.",
        orchestration_step_ids,
    )
    specialized = [
        row
        for row in inventory
        if row["disposition"] == "specialized_candidate_input"
    ]
    loaded_specialized = [
        row for row in specialized if int(row["current_non_null_count"] or 0) > 0
    ]
    add(
        "high",
        "specialized_metric_current_materialization",
        len(loaded_specialized) >= 4,
        "At least four independent or supporting specialized financial fields must materialize on the current as-of date.",
        {
            "loaded_columns": [
                str(row["column_name"]) for row in loaded_specialized
            ],
            "registered_columns": len(specialized),
        },
    )
    disabled_weight_issues = [
        {
            "pillar": field,
            "weight": (
                (candidate_manifest.get("inputs") or {})
                .get("calibration_summary", {})
                .get("candidate", "")
            ),
        }
        for field in STRUCTURALLY_DISABLED_PILLARS
        if field not in PILLAR_SCORE_FIELDS
    ]
    add(
        "critical",
        "structurally_disabled_pillar_contract",
        not disabled_weight_issues,
        "Every structurally disabled pillar must remain part of the published pillar schema and be fixed to zero during calibration.",
        disabled_weight_issues,
    )
    candidate_warns = candidate_manifest.get("warn_gates") or []
    add(
        "medium",
        "candidate_warning_review",
        not candidate_warns,
        "Candidate warnings require explicit review but do not invalidate otherwise passing evidence.",
        candidate_warns,
    )
    return findings, {
        "candidate_manifest": candidate_manifest,
        "production_promotion": production_promotion,
        "active_ticker_count": active_count,
        "historical_ticker_count": historical_count,
    }


def summary_status(findings: list[dict[str, str]]) -> str:
    blocking = [
        row
        for row in findings
        if row["status"] == "FAIL" and row["severity"] in {"critical", "high"}
    ]
    if blocking:
        return "BLOCKED"
    if any(row["status"] == "FAIL" for row in findings):
        return "READY_WITH_WARNINGS"
    return "READY"


def manifest_for_files(
    *,
    asof: str,
    status: str,
    files: list[Path],
    summary: dict[str, Any],
) -> dict[str, Any]:
    return {
        "artifact_family": "defense_metric_promotion_readiness_audit",
        "audit_version": AUDIT_VERSION,
        "model_family": MODEL_FAMILY,
        "asof_date": asof,
        "created_at_utc": utc_now(),
        "status": status,
        "summary": summary,
        "files": {
            path.name: {
                "path": str(path),
                "sha256": sha256_file(path),
            }
            for path in files
        },
    }


def json_fingerprint(payload: object) -> str:
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def finite_text(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    return value

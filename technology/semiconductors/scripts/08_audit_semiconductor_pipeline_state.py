#!/usr/bin/env python3
"""Read-only audit for the semiconductor technology pipeline state.

This script is intentionally separate from the builders/validators. It checks
the live database after refreshes and writes a compact audit report without
changing any DB rows.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.logging_utils import configure_utc_logging  # noqa: E402
from technology.core.scoring_features import cfg_ticker_set, parse_date  # noqa: E402
from technology.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("audit_semiconductor_pipeline_state")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_REPORT_DIR = PROJECT_ROOT / "output" / "technology_reports" / "audits"
REQUIRED_CORE_COMPONENTS = {
    "quality",
    "growth",
    "valuation",
    "market_behavior",
    "positioning",
    "risk_control",
}
REQUIRED_OVERLAY_COMPONENTS = {"sector_cycle", "big_tech_capex"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit the live semiconductor pipeline state.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def readonly_connect(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.expanduser().resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row is not None else None


def fetch_dicts(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def placeholders(values: list[str]) -> str:
    if not values:
        raise ValueError("values cannot be empty")
    return ",".join("?" for _ in values)


def cfg_bool(config: dict[str, Any], key: str, default: bool = False) -> bool:
    raw = cfg_get(config, key, default)
    return str(raw).strip().lower() in {"1", "true", "yes", "y"}


def add_check(checks: list[dict[str, Any]], name: str, status: str, detail: str, **extra: Any) -> None:
    checks.append({"check": name, "status": status, "detail": detail, **extra})


def count_distinct(conn: sqlite3.Connection, table: str, tickers: list[str], *, where: str = "", params: tuple[Any, ...] = ()) -> int:
    ph = placeholders(tickers)
    where_clause = f"AND {where}" if where else ""
    sql = f"""
        SELECT COUNT(DISTINCT ticker)
        FROM {table}
        WHERE ticker IN ({ph}) {where_clause}
    """
    return int(scalar(conn, sql, (*tickers, *params)) or 0)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def missing_tickers(
    conn: sqlite3.Connection,
    table: str,
    tickers: list[str],
    *,
    model_family: str,
    where: str = "",
    params: tuple[Any, ...] = (),
) -> list[str]:
    ph = placeholders(tickers)
    where_clause = f"AND {where}" if where else ""
    sql = f"""
        SELECT c.ticker
        FROM dim_company c
        JOIN dim_technology_taxonomy t
          ON t.ticker = c.ticker
         AND t.model_family = ?
        WHERE c.is_active = 1
          AND c.ticker NOT IN (
              SELECT DISTINCT ticker FROM {table}
              WHERE ticker IN ({ph}) {where_clause}
          )
        ORDER BY c.ticker
    """
    return [str(row["ticker"]) for row in conn.execute(sql, (model_family, *tickers, *params)).fetchall()]


def latest_financial_field_counts(conn: sqlite3.Connection, tickers: list[str], model_family: str) -> dict[str, int]:
    ph = placeholders(tickers)
    fields = [
        "market_cap",
        "revenue_ttm",
        "gross_margin",
        "operating_margin",
        "fcf_margin",
        "inventory_days",
        "revenue_yoy_growth",
        "revenue_acceleration",
        "ev_gross_profit",
        "ev_operating_income",
        "fcf_yield",
    ]
    # Distinct tickers per field: several fiscal-period rows can share a
    # ticker's latest asof date, so plain row counts would inflate coverage.
    select_parts = [
        f"COUNT(DISTINCT CASE WHEN {field} IS NOT NULL THEN f.ticker END) AS {field}" for field in fields
    ]
    row = conn.execute(
        f"""
        WITH latest AS (
            SELECT ticker, MAX(asof_date) AS max_asof
            FROM feature_financial_statement
            WHERE ticker IN ({ph})
              AND model_family = ?
            GROUP BY ticker
        )
        SELECT COUNT(DISTINCT f.ticker) AS latest_rows, {", ".join(select_parts)}
        FROM feature_financial_statement f
        JOIN latest l
          ON l.ticker = f.ticker
         AND l.max_asof = f.asof_date
        WHERE f.model_family = ?
        """,
        (*tickers, model_family, model_family),
    ).fetchone()
    return {key: int(row[key] or 0) for key in row.keys()} if row is not None else {}


def component_summary(conn: sqlite3.Connection, tickers: list[str], *, source_id: str, model_family: str) -> list[dict[str, Any]]:
    ph = placeholders(tickers)
    return fetch_dicts(
        conn,
        f"""
        SELECT component_name,
               COUNT(*) AS rows,
               COUNT(DISTINCT ticker) AS tickers,
               AVG(component_score) AS avg_score,
               MIN(component_score) AS min_score,
               MAX(component_score) AS max_score,
               AVG(component_quality) AS avg_quality,
               SUM(CASE WHEN COALESCE(component_quality, 0) <= 0 THEN 1 ELSE 0 END) AS zero_quality_rows,
               SUM(CASE WHEN default_applied = 1 THEN 1 ELSE 0 END) AS default_rows
        FROM feature_scoring_component
        WHERE model_family = ?
          AND source_id = ?
          AND asof_date = (
              SELECT MAX(asof_date)
              FROM feature_scoring_component
              WHERE model_family = ? AND source_id = ?
          )
          AND ticker IN ({ph})
        GROUP BY component_name
        ORDER BY component_name
        """,
        (model_family, source_id, model_family, source_id, *tickers),
    )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(
        cfg_get(config, "semiconductor_signal_diagnostics.output_dir", DEFAULT_REPORT_DIR),
        base_dir=base_dir,
    ).parent / "audits"
    optuna_output_dir = resolve_path(
        cfg_get(config, "semiconductor_optuna_calibration.output_dir", "../output/technology_reports/optuna_calibration"),
        base_dir=base_dir,
    )
    governance_output_dir = resolve_path(
        cfg_get(config, "semiconductor_governance_reports.output_dir", "../output/technology_reports/governance"),
        base_dir=base_dir,
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    require_13f = cfg_bool(config, "positioning_import.require_upstream_13f_for_gate", True)
    require_short = cfg_bool(config, "positioning_import.require_upstream_short_for_gate", True)
    require_borrow = cfg_bool(config, "positioning_import.require_upstream_borrow_for_gate", True)
    exempt_13f = cfg_ticker_set(cfg_get(config, "positioning_import.upstream_13f_gate_exempt_tickers", []))
    model_family = str(cfg_get(config, "technology_universe.initial_subsector", "semiconductors"))
    baseline_source = str(cfg_get(config, "semiconductor_scoring_features.source_id", "semiconductor_scoring_contract"))
    calibrated_source = str(cfg_get(config, "semiconductor_calibrated_scoring.source_id", "semiconductor_calibrated_score_v1"))
    expected_universe = int(cfg_get(config, "technology_universe.expected_ticker_count", 99))
    financial_exempt = cfg_ticker_set(
        cfg_get(
            config,
            "semiconductor_pipeline_audit.financial_exempt_tickers",
            cfg_get(config, "semiconductor_calibrated_scoring.rank_ready_exempt_tickers", []),
        )
    )
    rank_ready_exempt = cfg_ticker_set(cfg_get(config, "semiconductor_calibrated_scoring.rank_ready_exempt_tickers", []))
    max_dead_pct = float(cfg_get(config, "semiconductor_scoring_features.max_dead_core_component_pct", 0.20))
    wsts_max_stale_days = int(cfg_get(config, "semiconductor_pipeline_audit.wsts_max_stale_days", 75))
    wsts_raw_source = str(cfg_get(config, "semiconductor_sector_overlays.wsts.source_id", "wsts_historical_billings"))
    capex_raw_source = str(cfg_get(config, "semiconductor_sector_overlays.big_tech_capex.source_id", "sec_big_tech_capex"))
    capex_feature_source = str(cfg_get(config, "semiconductor_sector_overlays.big_tech_capex.feature_source_id", "semiconductor_big_tech_capex_cycle"))
    require_stage8 = cfg_bool(config, "semiconductor_pipeline_audit.require_stage8_outputs", False)
    require_governance = cfg_bool(config, "semiconductor_pipeline_audit.require_governance_reports", True)
    min_historical_membership = int(cfg_get(config, "technology_universe.min_historical_membership_tickers", 20))
    today = date.today()

    checks: list[dict[str, Any]] = []
    missing_rows: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "audit_date": today.isoformat(),
        "database_path": str(db_path),
    }

    with readonly_connect(db_path) as conn:
        tickers = [
            normalize_ticker(row["ticker"])
            for row in conn.execute(
                """
                SELECT c.ticker
                FROM dim_company c
                JOIN dim_technology_taxonomy t
                  ON t.ticker = c.ticker
                 AND t.model_family = ?
                WHERE c.is_active = 1
                ORDER BY c.ticker
                """
                ,
                (model_family,),
            ).fetchall()
            if normalize_ticker(row["ticker"])
        ]
        ph = placeholders(tickers)
        summary["active_semiconductor_tickers"] = len(tickers)
        add_check(
            checks,
            "universe_count",
            "PASS" if len(tickers) == expected_universe else "FAIL",
            f"{len(tickers)}/{expected_universe} active semiconductor tickers",
        )
        if table_exists(conn, "dim_universe_membership"):
            membership_row = conn.execute(
                f"""
                SELECT COUNT(*) AS rows,
                       COUNT(DISTINCT CASE WHEN is_current_member = 1 THEN ticker END) AS current_tickers,
                       COUNT(DISTINCT CASE WHEN point_in_time_flag = 1 THEN ticker END) AS pit_tickers,
                       COUNT(DISTINCT CASE WHEN point_in_time_flag = 1 AND is_current_member = 1 THEN ticker END) AS current_pit_tickers,
                       COUNT(DISTINCT CASE WHEN point_in_time_flag = 1 AND is_current_member = 0 THEN ticker END) AS historical_pit_tickers
                FROM dim_universe_membership
                WHERE model_family = ?
                  AND (ticker IN ({ph}) OR is_current_member = 0)
                """,
                (model_family, *tickers),
            ).fetchone()
            membership_rows = int(membership_row["rows"] or 0) if membership_row is not None else 0
            current_membership_tickers = int(membership_row["current_tickers"] or 0) if membership_row is not None else 0
            pit_membership_tickers = int(membership_row["pit_tickers"] or 0) if membership_row is not None else 0
            current_pit_tickers = int(membership_row["current_pit_tickers"] or 0) if membership_row is not None else 0
            historical_pit_tickers = int(membership_row["historical_pit_tickers"] or 0) if membership_row is not None else 0
            summary["universe_membership"] = {
                "rows": membership_rows,
                "current_tickers": current_membership_tickers,
                "point_in_time_tickers": pit_membership_tickers,
                "current_point_in_time_tickers": current_pit_tickers,
                "historical_point_in_time_tickers": historical_pit_tickers,
            }
            add_check(
                checks,
                "current_universe_membership_coverage",
                "PASS" if current_membership_tickers == len(tickers) else "FAIL",
                f"{current_membership_tickers}/{len(tickers)} current membership tickers",
            )
            add_check(
                checks,
                "point_in_time_membership_backfill",
                "PASS" if current_pit_tickers >= len(tickers) else "FAIL",
                f"{current_pit_tickers}/{len(tickers)} current tickers have PIT membership rows",
            )
            add_check(
                checks,
                "historical_delisted_membership_backfill",
                "PASS" if historical_pit_tickers >= min_historical_membership else "FAIL",
                f"{historical_pit_tickers}/{min_historical_membership} minimum inactive/delisted PIT tickers loaded",
            )
        else:
            add_check(checks, "current_universe_membership_coverage", "FAIL", "dim_universe_membership table is missing")
            add_check(checks, "point_in_time_membership_backfill", "FAIL", "dim_universe_membership table is missing")
            add_check(checks, "historical_delisted_membership_backfill", "FAIL", "dim_universe_membership table is missing")

        # Coverage of exempt tickers must not mask a missing required ticker, so
        # the required checks count distinct tickers over the required set only.
        financial_required_tickers = [ticker for ticker in tickers if ticker not in financial_exempt] or ["__none__"]
        required_13f_tickers = [ticker for ticker in tickers if ticker not in exempt_13f] or ["__none__"]
        source_counts = {
            "price_tickers": count_distinct(conn, "fact_price_ohlcv", tickers),
            "market_feature_tickers": count_distinct(conn, "feature_market_technical", tickers),
            "sec_filing_tickers": count_distinct(conn, "fact_sec_filing", tickers),
            "raw_xbrl_tickers": count_distinct(conn, "fact_sec_xbrl_fact_raw", tickers),
            "financial_feature_tickers": count_distinct(
                conn,
                "feature_financial_statement",
                tickers,
                where="model_family = ?",
                params=(model_family,),
            ),
            "financial_feature_required_tickers": count_distinct(
                conn,
                "feature_financial_statement",
                financial_required_tickers,
                where="model_family = ?",
                params=(model_family,),
            ),
            "form4_tickers": count_distinct(conn, "fact_sec_form4_transaction", tickers),
            "short_interest_tickers": count_distinct(conn, "fact_short_interest", tickers),
            "borrow_tickers": count_distinct(conn, "fact_ibkr_borrow_snapshot", tickers),
            "13f_tickers": count_distinct(conn, "fact_13f_positioning", tickers),
            "13f_required_tickers": count_distinct(conn, "fact_13f_positioning", required_13f_tickers),
            "positioning_feature_tickers": count_distinct(
                conn,
                "feature_positioning",
                tickers,
                where="model_family = ?",
                params=(model_family,),
            ),
            "scoring_input_tickers": count_distinct(
                conn,
                "feature_scoring_input",
                tickers,
                where="model_family = ? AND source_id = ?",
                params=(model_family, baseline_source),
            ),
            "scoring_component_tickers": count_distinct(
                conn,
                "feature_scoring_component",
                tickers,
                where="model_family = ? AND source_id = ?",
                params=(model_family, baseline_source),
            ),
        }
        summary["source_counts"] = source_counts
        add_check(checks, "ohlcv_coverage", "PASS" if source_counts["price_tickers"] == len(tickers) else "FAIL", f"{source_counts['price_tickers']}/{len(tickers)} tickers")
        add_check(checks, "market_feature_coverage", "PASS" if source_counts["market_feature_tickers"] == len(tickers) else "FAIL", f"{source_counts['market_feature_tickers']}/{len(tickers)} tickers")
        add_check(checks, "scoring_input_coverage", "PASS" if source_counts["scoring_input_tickers"] == len(tickers) else "FAIL", f"{source_counts['scoring_input_tickers']}/{len(tickers)} tickers")

        financial_required = len([ticker for ticker in financial_required_tickers if ticker != "__none__"])
        add_check(
            checks,
            "financial_feature_coverage",
            "PASS" if source_counts["financial_feature_required_tickers"] >= financial_required else "FAIL",
            f"{source_counts['financial_feature_required_tickers']}/{financial_required} non-IPO-required tickers",
        )

        if require_short:
            add_check(checks, "finra_short_interest_required", "PASS" if source_counts["short_interest_tickers"] == len(tickers) else "FAIL", f"{source_counts['short_interest_tickers']}/{len(tickers)} tickers")
        if require_borrow:
            add_check(checks, "ibkr_borrow_required", "PASS" if source_counts["borrow_tickers"] == len(tickers) else "FAIL", f"{source_counts['borrow_tickers']}/{len(tickers)} tickers")
        if require_13f:
            required_13f = len([ticker for ticker in required_13f_tickers if ticker != "__none__"])
            add_check(checks, "13f_required", "PASS" if source_counts["13f_required_tickers"] >= required_13f else "FAIL", f"{source_counts['13f_required_tickers']}/{required_13f} required tickers")

        for source_name, table_name, exempt in [
            ("price", "fact_price_ohlcv", set()),
            ("market_feature", "feature_market_technical", set()),
            ("financial_feature", "feature_financial_statement", financial_exempt),
            ("short_interest", "fact_short_interest", set()),
            ("ibkr_borrow", "fact_ibkr_borrow_snapshot", set()),
            ("13f", "fact_13f_positioning", exempt_13f),
            ("scoring_input", "feature_scoring_input", set()),
        ]:
            where = "model_family = ?" if table_name in {"feature_financial_statement", "feature_scoring_input"} else ""
            params = (model_family,) if where else ()
            for ticker in missing_tickers(conn, table_name, tickers, model_family=model_family, where=where, params=params):
                if ticker not in exempt:
                    missing_rows.append({"source": source_name, "ticker": ticker})

        latest_fin = latest_financial_field_counts(conn, tickers, model_family)
        summary["latest_financial_field_counts"] = latest_fin
        latest_fin_rows = int(latest_fin.get("latest_rows", 0))
        for field in ("market_cap", "revenue_yoy_growth", "ev_gross_profit", "fcf_yield"):
            count = int(latest_fin.get(field, 0))
            threshold = max(1, int(latest_fin_rows * 0.75))
            add_check(
                checks,
                f"latest_financial_{field}",
                "PASS" if count >= threshold else "FAIL",
                f"{count}/{latest_fin_rows} latest financial rows populated",
            )

        components = component_summary(conn, tickers, source_id=baseline_source, model_family=model_family)
        summary["component_summary"] = components
        component_by_name = {str(row["component_name"]): row for row in components}
        for component in sorted(REQUIRED_CORE_COMPONENTS | REQUIRED_OVERLAY_COMPONENTS):
            row = component_by_name.get(component)
            ticker_count = int(row["tickers"] or 0) if row else 0
            status = "PASS" if ticker_count == len(tickers) else "FAIL"
            add_check(checks, f"component_{component}_coverage", status, f"{ticker_count}/{len(tickers)} tickers")
            if component in REQUIRED_CORE_COMPONENTS and row is not None:
                zero_pct = (float(row["zero_quality_rows"] or 0) / max(1.0, float(row["rows"] or 0)))
                add_check(
                    checks,
                    f"component_{component}_quality_nonzero",
                    "PASS" if zero_pct <= max_dead_pct else "FAIL",
                    f"{zero_pct:.1%} zero-quality rows",
                )

        wsts_row = conn.execute(
            """
            SELECT COUNT(*) AS rows,
                   MIN(period_month) AS min_month,
                   MAX(period_month) AS max_month
            FROM fact_semiconductor_wsts_billings
            """
        ).fetchone()
        latest_wsts = parse_date(wsts_row["max_month"]) if wsts_row else None
        wsts_stale_days = (today - latest_wsts).days if latest_wsts else None
        summary["wsts"] = dict(wsts_row) if wsts_row else {}
        summary["wsts"]["stale_days_from_month_start"] = wsts_stale_days
        add_check(
            checks,
            "wsts_history_loaded",
            "PASS" if int(wsts_row["rows"] or 0) > 1200 else "FAIL",
            f"{int(wsts_row['rows'] or 0)} rows, latest={wsts_row['max_month'] if wsts_row else None}",
        )
        # Audit-side escalation only (scoring staleness handling is unchanged):
        # WARN within 2x the configured cap, FAIL beyond it.
        if wsts_stale_days is not None and wsts_stale_days <= wsts_max_stale_days:
            wsts_freshness_status = "PASS"
        elif wsts_stale_days is not None and wsts_stale_days > 2 * wsts_max_stale_days:
            wsts_freshness_status = "FAIL"
        else:
            wsts_freshness_status = "WARN"
        add_check(
            checks,
            "wsts_freshness",
            wsts_freshness_status,
            f"latest month start {wsts_stale_days} days old (max {wsts_max_stale_days}, fail beyond {2 * wsts_max_stale_days})",
        )

        capex_feature = conn.execute(
            """
            SELECT *
            FROM feature_big_tech_capex_cycle
            WHERE source_id = ? AND model_family = ?
            ORDER BY asof_date DESC
            LIMIT 1
            """,
            (capex_feature_source, model_family),
        ).fetchone()
        capex_summary = conn.execute(
            """
            SELECT COUNT(*) AS rows,
                   COUNT(DISTINCT ticker) AS companies,
                   MIN(period_end_date) AS min_period_end,
                   MAX(period_end_date) AS max_period_end,
                   SUM(CASE WHEN duration_days > 120 THEN 1 ELSE 0 END) AS ytd_span_rows,
                   SUM(CASE WHEN duration_days BETWEEN 70 AND 120 THEN 1 ELSE 0 END) AS quarter_span_rows
            FROM fact_big_tech_capex
            WHERE source_id = ?
            """,
            (capex_raw_source,),
        ).fetchone()
        summary["big_tech_capex"] = dict(capex_summary) if capex_summary else {}
        summary["latest_big_tech_capex_feature"] = dict(capex_feature) if capex_feature else {}
        add_check(
            checks,
            "big_tech_capex_companies",
            "PASS" if int(capex_summary["companies"] or 0) >= 5 else "FAIL",
            f"{int(capex_summary['companies'] or 0)}/5 companies",
        )
        add_check(
            checks,
            "big_tech_capex_ytd_spans",
            "PASS" if int(capex_summary["ytd_span_rows"] or 0) > 0 else "FAIL",
            f"{int(capex_summary['ytd_span_rows'] or 0)} YTD-span rows",
        )
        if capex_feature is not None:
            add_check(
                checks,
                "big_tech_capex_current_coverage",
                "PASS" if int(capex_feature["companies_current"] or 0) >= 4 else "FAIL",
                f"{int(capex_feature['companies_current'] or 0)}/5 current companies",
            )

        raw_counts = fetch_dicts(
            conn,
            """
            SELECT source_id, COUNT(*) AS rows
            FROM raw_api_responses
            WHERE source_id IN (?, ?)
            GROUP BY source_id
            ORDER BY source_id
            """,
            (wsts_raw_source, capex_raw_source),
        )
        summary["raw_api_response_counts"] = raw_counts
        raw_by_source = {str(row["source_id"]): int(row["rows"] or 0) for row in raw_counts}
        for source_id in (wsts_raw_source, capex_raw_source):
            add_check(
                checks,
                f"raw_response_{source_id}",
                "PASS" if raw_by_source.get(source_id, 0) > 0 else "FAIL",
                f"{raw_by_source.get(source_id, 0)} raw response rows",
            )

        # Residual scoping: only this family's issues should gate the audit.
        # Family-owned stages carry 'semiconductor' in the name or are the
        # overlay sync stages; shared technology-wide stages (price/SEC/
        # positioning syncs) are included only when the issue ticker belongs
        # to the semiconductor universe.
        issue_rows = fetch_dicts(
            conn,
            f"""
            SELECT stage, severity, COALESCE(resolution_status, '') AS resolution_status, COUNT(*) AS rows
            FROM data_quality_issues
            WHERE stage LIKE '%semiconductor%'
               OR stage IN ('sync_wsts_billings', 'sync_big_tech_capex')
               OR ticker IN ({ph})
            GROUP BY stage, severity, COALESCE(resolution_status, '')
            ORDER BY stage, severity, resolution_status
            """,
            (*tickers,),
        )
        summary["data_quality_issue_summary"] = issue_rows
        unresolved_bad = [
            row for row in issue_rows
            if str(row["severity"]).lower() in {"error", "critical"}
            and str(row["resolution_status"]).lower() not in {"resolved", "expected", "exempt"}
        ]
        add_check(
            checks,
            "unresolved_error_quality_issues",
            "PASS" if not unresolved_bad else "FAIL",
            f"{sum(int(row['rows'] or 0) for row in unresolved_bad)} unresolved error/critical issues",
        )

        # Summaries and Stage 7 checks are pinned to each source's latest asof:
        # both tables accumulate one row-set per build date, so unfiltered
        # aggregates would double-count as soon as a second asof exists.
        latest_scores = fetch_dicts(
            conn,
            f"""
            SELECT COUNT(*) AS rows,
                   MAX(asof_date) AS asof_date,
                   SUM(CASE WHEN rank_ready_flag = 1 THEN 1 ELSE 0 END) AS rank_ready,
                   SUM(CASE WHEN calibration_eligible_flag = 1 THEN 1 ELSE 0 END) AS calibration_eligible,
                   AVG(core_data_quality_confidence) AS avg_core_quality,
                   AVG(full_data_quality_confidence) AS avg_full_quality,
                   AVG(sector_overlay_quality) AS avg_overlay_quality
            FROM feature_scoring_input
            WHERE model_family = ?
              AND source_id = ?
              AND asof_date = (
                  SELECT MAX(asof_date)
                  FROM feature_scoring_input
                  WHERE model_family = ? AND source_id = ?
              )
              AND ticker IN ({ph})
            """,
            (model_family, baseline_source, model_family, baseline_source, *tickers),
        )
        summary["scoring_input_summary"] = latest_scores[0] if latest_scores else {}

        calibrated = conn.execute(
            f"""
            SELECT COUNT(*) AS rows,
                   MAX(asof_date) AS asof_date,
                   SUM(CASE WHEN rank_ready_flag = 1 THEN 1 ELSE 0 END) AS rank_ready,
                   SUM(CASE WHEN calibration_eligible_flag = 1 THEN 1 ELSE 0 END) AS calibration_eligible,
                   COUNT(DISTINCT final_rank) AS distinct_ranks,
                   MIN(final_score) AS min_score,
                   MAX(final_score) AS max_score,
                   AVG(final_score) AS avg_score
            FROM feature_scoring_model_output
            WHERE source_id = ?
              AND model_family = ?
              AND asof_date = (
                  SELECT MAX(asof_date)
                  FROM feature_scoring_model_output
                  WHERE source_id = ? AND model_family = ?
              )
              AND ticker IN ({ph})
            """,
            (calibrated_source, model_family, calibrated_source, model_family, *tickers),
        ).fetchone()
        calibrated_summary = dict(calibrated) if calibrated is not None else {}
        summary["stage7_calibrated_score_summary"] = calibrated_summary
        calibrated_rows = int(calibrated_summary.get("rows") or 0)
        calibrated_rank_ready = int(calibrated_summary.get("rank_ready") or 0)
        calibrated_distinct_ranks = int(calibrated_summary.get("distinct_ranks") or 0)
        calibrated_min = calibrated_summary.get("min_score")
        calibrated_max = calibrated_summary.get("max_score")
        calibrated_range = float(calibrated_max) - float(calibrated_min) if calibrated_min is not None and calibrated_max is not None else 0.0
        expected_rank_ready = len([ticker for ticker in tickers if ticker not in rank_ready_exempt])
        add_check(
            checks,
            "stage7_calibrated_output_coverage",
            "PASS" if calibrated_rows == len(tickers) else "FAIL",
            f"{calibrated_rows}/{len(tickers)} rows at asof={calibrated_summary.get('asof_date')}",
        )
        add_check(
            checks,
            "stage7_calibrated_rank_ready",
            "PASS" if calibrated_rank_ready >= expected_rank_ready else "FAIL",
            f"{calibrated_rank_ready}/{expected_rank_ready} non-exempt expected rank-ready rows",
        )
        add_check(
            checks,
            "stage7_calibrated_rank_variance",
            "PASS" if calibrated_distinct_ranks == calibrated_rank_ready and calibrated_range > 0 else "FAIL",
            f"distinct_ranks={calibrated_distinct_ranks} rank_ready={calibrated_rank_ready} score_range={calibrated_range:.4f}",
        )

    stage8_required = [
        optuna_output_dir / "stage8_trials.csv",
        optuna_output_dir / "stage8_best_summary.csv",
        optuna_output_dir / "stage8_best_weights.json",
        optuna_output_dir / "stage8_fold_robustness.csv",
        optuna_output_dir / "stage8_candidate_current_scores.csv",
    ]
    stage8_present = any(path.exists() for path in stage8_required)
    missing_stage8 = [str(path) for path in stage8_required if not path.exists() or path.stat().st_size == 0]
    # Stage 8 runs on model review, not on every refresh. Absent outputs only
    # warn unless explicitly required; present-but-broken outputs always fail.
    if not stage8_present and not require_stage8:
        add_check(checks, "stage8_optuna_outputs_exist", "WARN", "Stage 8 has not been run yet (not required by config)")
    else:
        add_check(
            checks,
            "stage8_optuna_outputs_exist",
            "PASS" if not missing_stage8 else "FAIL",
            "all required Stage 8 outputs found" if not missing_stage8 else ";".join(missing_stage8),
        )
    best_weights_path = optuna_output_dir / "stage8_best_weights.json"
    stage8_summary: dict[str, Any] = {}
    if best_weights_path.exists():
        try:
            stage8_summary = json.loads(best_weights_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            stage8_summary = {"json_error": str(exc)}
    summary["stage8_optuna_summary"] = stage8_summary
    if stage8_present or require_stage8:
        trial_count = int(stage8_summary.get("n_trials") or 0)
        add_check(
            checks,
            "stage8_optuna_trial_count",
            "PASS" if trial_count >= 20 else "FAIL",
            f"{trial_count} trials",
        )
        add_check(
            checks,
            "stage8_promotion_decision_recorded",
            "PASS" if "promotion_candidate" in stage8_summary else "FAIL",
            f"promotion_candidate={stage8_summary.get('promotion_candidate')}",
        )

    governance_required_names = cfg_get(
        config,
        "semiconductor_governance_reports.required_outputs",
        [
            "semiconductor_signal_registry.csv",
            "semiconductor_signal_registry.json",
            "semiconductor_lockbox_ledger.csv",
            "semiconductor_lockbox_ledger.json",
            "semiconductor_governance_manifest.json",
        ],
    )
    governance_required = [governance_output_dir / str(name) for name in governance_required_names]
    governance_missing = [str(path) for path in governance_required if not path.exists() or path.stat().st_size == 0]
    governance_summary: dict[str, Any] = {}
    governance_manifest = governance_output_dir / "semiconductor_governance_manifest.json"
    if governance_manifest.exists() and governance_manifest.stat().st_size > 0:
        try:
            governance_summary = json.loads(governance_manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            governance_summary = {"json_error": str(exc)}
    summary["governance_reports"] = governance_summary
    if require_governance:
        add_check(
            checks,
            "governance_lockbox_registry_outputs",
            "PASS" if not governance_missing else "FAIL",
            "all required governance outputs found" if not governance_missing else ";".join(governance_missing),
        )
    elif governance_missing:
        add_check(
            checks,
            "governance_lockbox_registry_outputs",
            "WARN",
            "governance outputs missing but not required by config",
        )
    else:
        add_check(checks, "governance_lockbox_registry_outputs", "PASS", "all governance outputs found")

    failed = [row for row in checks if row["status"] == "FAIL"]
    summary["check_summary"] = {
        "total": len(checks),
        "passed": len(checks) - len(failed),
        "failed": len(failed),
    }
    summary["checks"] = checks
    summary["missing_rows"] = missing_rows

    summary_path = output_dir / "semiconductor_pipeline_audit.json"
    checks_path = output_dir / "semiconductor_pipeline_audit_checks.csv"
    missing_path = output_dir / "semiconductor_pipeline_audit_missing.csv"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str), encoding="utf-8")
    write_csv(checks_path, checks)
    write_csv(missing_path, missing_rows)

    LOGGER.info("Audit written to %s", summary_path)
    print(json.dumps(summary["check_summary"], sort_keys=True))
    if failed:
        print("FAILED CHECKS:")
        for row in failed:
            print(f"- {row['check']}: {row['detail']}")
    else:
        print("All audit checks passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

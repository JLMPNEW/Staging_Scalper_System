#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.machinery.scoring import parse_asof  # noqa: E402
from industrials.machinery.financial_contract import (  # noqa: E402
    AVAILABILITY_STATUSES,
    required_metric_names,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
COVERAGE_FIELDS = [
    "metric",
    "category",
    "gate_mode",
    "implemented_flag",
    "covered_count",
    "eligible_count",
    "applicable_count",
    "excluded_count",
    "coverage_fraction",
    "minimum_count",
    "minimum_fraction",
    "minimum_cohort_fraction",
    "cohort_coverage",
    "status",
]
CONCEPT_FIELDS = ["taxonomy", "concept_name", "ticker_count", "fact_count"]


@dataclass(frozen=True)
class MetricGate:
    metric: str
    category: str
    minimum_count: int
    minimum_fraction: float
    minimum_cohort_fraction: float = 0.0
    gate_mode: str = "calibration"


METRIC_GATES = [
    MetricGate("orders", "orders_backlog_source", 10, 0.10),
    MetricGate(
        "funded_backlog",
        "orders_backlog_source",
        1,
        0.0,
        gate_mode="limited_universe_diagnostic",
    ),
    MetricGate("reported_backlog", "orders_backlog_source", 5, 0.04),
    MetricGate("remaining_performance_obligation", "rpo_source", 5, 0.04),
    MetricGate("rpo_current", "rpo_source", 3, 0.02),
    MetricGate("orders_yoy_growth", "orders_backlog", 10, 0.10),
    MetricGate("book_to_bill", "orders_backlog", 10, 0.10),
    MetricGate(
        "backlog_yoy_growth",
        "orders_backlog",
        1,
        0.0,
        gate_mode="limited_universe_diagnostic",
    ),
    MetricGate(
        "backlog_to_revenue",
        "orders_backlog",
        1,
        0.0,
        gate_mode="limited_universe_diagnostic",
    ),
    MetricGate("reported_backlog_yoy_growth", "orders_backlog", 5, 0.04),
    MetricGate("reported_backlog_to_revenue", "orders_backlog", 5, 0.04),
    MetricGate("rpo_yoy_growth", "rpo", 5, 0.04),
    MetricGate("rpo_to_revenue", "rpo", 5, 0.04),
    MetricGate("rpo_implied_orders", "rpo_proxy", 5, 0.04),
    MetricGate("rpo_implied_book_to_bill", "rpo_proxy", 5, 0.04),
    MetricGate(
        "contract_load_proxy",
        "contract_load_proxy",
        5,
        0.04,
        gate_mode="limited_universe_diagnostic",
    ),
    MetricGate(
        "contract_load_proxy_yoy_growth",
        "contract_load_proxy",
        5,
        0.04,
        gate_mode="limited_universe_diagnostic",
    ),
    MetricGate(
        "contract_load_proxy_to_revenue",
        "contract_load_proxy",
        5,
        0.04,
        gate_mode="limited_universe_diagnostic",
    ),
    MetricGate("roic", "capital_efficiency", 50, 0.45, 0.25),
    MetricGate("asset_turnover", "capital_efficiency", 60, 0.55, 0.30),
    MetricGate("incremental_operating_margin", "operating_leverage", 40, 0.35, 0.20),
    MetricGate("inventory_sales_growth_spread", "working_capital", 40, 0.35, 0.20),
    MetricGate("cash_conversion_cycle_change", "working_capital", 35, 0.30, 0.15),
    MetricGate("net_debt_to_ebitda", "leverage", 50, 0.45, 0.25),
    MetricGate("interest_coverage", "leverage", 50, 0.45, 0.25),
    MetricGate("cash_runway_years", "development_stage", 5, 0.04),
    MetricGate("capital_raise_dependence", "development_stage", 5, 0.04),
    MetricGate("diluted_shares_yoy_growth", "development_stage", 50, 0.45, 0.25),
]


def gate_status(gate: MetricGate, *, implemented: bool, ready: bool) -> str:
    if not implemented:
        return "NOT_IMPLEMENTED"
    if gate.gate_mode == "limited_universe_diagnostic":
        return "LIMITED_UNIVERSE_READY" if ready else "LIMITED_UNIVERSE_PENDING_COVERAGE"
    return "CALIBRATION_READY" if ready else "IMPLEMENTED_PENDING_COVERAGE"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit machinery-specific financial metric coverage.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--require-calibration-ready", action="store_true")
    return parser.parse_args()


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def eligible_members(conn: sqlite3.Connection, *, asof: str) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT DISTINCT m.ticker, t.calibration_cohort_id
        FROM dim_universe_membership m
        JOIN dim_industrials_taxonomy t
          ON t.ticker = m.ticker AND t.model_family = m.model_family
        WHERE m.model_family = 'machinery'
          AND m.start_date <= ?
          AND COALESCE(m.end_date, '9999-12-31') >= ?
        """,
        (asof, asof),
    ).fetchall()
    return {str(row["ticker"]): str(row["calibration_cohort_id"] or "unclassified") for row in rows}


def metric_values(
    conn: sqlite3.Connection,
    *,
    metric: str,
    asof: str,
    members: dict[str, str],
) -> dict[str, float]:
    if not members:
        return {}
    placeholders = ",".join("?" for _ in members)
    rows = conn.execute(
        f"""
        SELECT ticker, {metric} AS metric_value
        FROM feature_financial_statement
        WHERE model_family = 'machinery'
          AND asof_date = ?
          AND ticker IN ({placeholders})
          AND {metric} IS NOT NULL
        """,
        (asof, *members),
    ).fetchall()
    output: dict[str, float] = {}
    for row in rows:
        value = float(row["metric_value"])
        if math.isfinite(value):
            output[str(row["ticker"])] = value
    return output


def raw_concept_candidates(
    conn: sqlite3.Connection,
    *,
    members: dict[str, str],
    asof: str,
) -> list[dict[str, Any]]:
    if not members:
        return []
    placeholders = ",".join("?" for _ in members)
    rows = conn.execute(
        f"""
        SELECT taxonomy, concept_name,
               COUNT(DISTINCT ticker) AS ticker_count,
               COUNT(*) AS fact_count
        FROM fact_sec_xbrl_fact_raw
        WHERE ticker IN ({placeholders})
          AND (
                CASE
                    WHEN COALESCE(accepted_at, '') GLOB '????-??-??*' THEN SUBSTR(accepted_at, 1, 10)
                    WHEN COALESCE(accepted_at, '') GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]*'
                        THEN SUBSTR(accepted_at, 1, 4) || '-' || SUBSTR(accepted_at, 5, 2) || '-' || SUBSTR(accepted_at, 7, 2)
                    ELSE COALESCE(NULLIF(filing_date, ''), '9999-12-31')
                END
          ) <= ?
          AND (
              LOWER(concept_name) LIKE '%order%'
              OR LOWER(concept_name) LIKE '%booking%'
              OR LOWER(concept_name) LIKE '%backlog%'
              OR LOWER(concept_name) LIKE '%performanceobligation%'
              OR LOWER(concept_name) LIKE '%remaining%obligation%'
              OR LOWER(concept_name) LIKE '%depreciation%'
              OR LOWER(concept_name) LIKE '%ebitda%'
              OR LOWER(concept_name) LIKE '%interestexpense%'
              OR LOWER(concept_name) LIKE '%proceedsfromissuance%'
              OR LOWER(concept_name) LIKE '%proceedsfromissuing%'
              OR LOWER(concept_name) LIKE '%proceedsfromborrowings%'
          )
        GROUP BY taxonomy, concept_name
        ORDER BY ticker_count DESC, fact_count DESC, taxonomy, concept_name
        """,
        (*members, asof),
    ).fetchall()
    return [dict(row) for row in rows]


def audit_metric_availability(
    conn: sqlite3.Connection,
    *,
    asof: str,
    members: dict[str, str],
) -> tuple[dict[str, int], list[str], dict[str, dict[str, str]]]:
    metrics = required_metric_names()
    expected = {(ticker, metric) for ticker in members for metric in metrics}
    rows = conn.execute(
        """
        SELECT ticker, metric_name, availability_status
        FROM feature_financial_metric_availability
        WHERE model_family = 'machinery'
          AND asof_date = ?
        """,
        (asof,),
    ).fetchall()
    actual = {
        (str(row["ticker"]), str(row["metric_name"]))
        for row in rows
        if str(row["ticker"]) in members
    }
    errors: list[str] = []
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    invalid = sorted(
        {
            str(row["availability_status"])
            for row in rows
            if str(row["ticker"]) in members
            and str(row["availability_status"]) not in AVAILABILITY_STATUSES
        }
    )
    if missing:
        errors.append(f"metric availability missing {len(missing)} ticker/metric rows; sample={missing[:10]}")
    if unexpected:
        errors.append(f"metric availability has {len(unexpected)} unexpected ticker/metric rows; sample={unexpected[:10]}")
    if invalid:
        errors.append(f"metric availability contains invalid statuses: {invalid}")
    counts = {status: 0 for status in sorted(AVAILABILITY_STATUSES)}
    by_metric: dict[str, dict[str, str]] = {}
    for row in rows:
        if str(row["ticker"]) not in members:
            continue
        status = str(row["availability_status"])
        if status in counts:
            counts[status] += 1
        by_metric.setdefault(str(row["metric_name"]), {})[str(row["ticker"])] = status
    counts["EXPECTED"] = len(expected)
    counts["CLASSIFIED"] = len(actual & expected)
    return counts, errors, by_metric


def main() -> int:
    args = parse_args()
    asof = parse_asof(args.asof)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(
        cfg_get(config, "paths.database_path"),
        base_dir=base_dir,
    )
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(
        "../../output/industrials/machinery/stage4",
        base_dir=base_dir,
    )
    errors: list[str] = []
    coverage_rows: list[dict[str, Any]] = []
    gate_metrics = {gate.metric for gate in METRIC_GATES}
    contract_metrics = set(required_metric_names())
    if gate_metrics != contract_metrics:
        errors.append(
            "coverage gates do not match the financial metric contract: "
            f"missing={sorted(contract_metrics - gate_metrics)} "
            f"unexpected={sorted(gate_metrics - contract_metrics)}"
        )
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))) as conn:
        members = eligible_members(conn, asof=asof)
        if not members:
            errors.append(f"No machinery members are effective at {asof}")
        columns = table_columns(conn, "feature_financial_statement")
        availability_counts, availability_errors, availability_by_metric = audit_metric_availability(
            conn,
            asof=asof,
            members=members,
        )
        errors.extend(availability_errors)
        for gate in METRIC_GATES:
            implemented = gate.metric in columns
            values = metric_values(conn, metric=gate.metric, asof=asof, members=members) if implemented else {}
            statuses = availability_by_metric.get(gate.metric, {})
            applicable_members = {
                ticker: cohort
                for ticker, cohort in members.items()
                if statuses.get(ticker) not in {"EXEMPT", "NOT_APPLICABLE"}
            }
            covered_tickers = set(values).intersection(applicable_members)
            coverage = len(covered_tickers) / len(applicable_members) if applicable_members else 0.0
            cohort_sizes: dict[str, int] = {}
            for cohort in applicable_members.values():
                cohort_sizes[cohort] = cohort_sizes.get(cohort, 0) + 1
            cohort_counts: dict[str, int] = {}
            for ticker in covered_tickers:
                cohort = applicable_members[ticker]
                cohort_counts[cohort] = cohort_counts.get(cohort, 0) + 1
            cohort_fractions = {
                cohort: cohort_counts.get(cohort, 0) / size
                for cohort, size in sorted(cohort_sizes.items())
                if size > 0
            }
            minimum_cohort = min(cohort_fractions.values(), default=0.0)
            ready = (
                implemented
                and len(covered_tickers) >= gate.minimum_count
                and coverage >= gate.minimum_fraction
                and minimum_cohort >= gate.minimum_cohort_fraction
            )
            status = gate_status(gate, implemented=implemented, ready=ready)
            if (
                args.require_calibration_ready
                and gate.gate_mode == "calibration"
                and not ready
            ):
                errors.append(
                    f"{gate.metric}: count={len(covered_tickers)} coverage={coverage:.3f} "
                    f"minimum_cohort={minimum_cohort:.3f}"
                )
            coverage_rows.append(
                {
                    "metric": gate.metric,
                    "category": gate.category,
                    "gate_mode": gate.gate_mode,
                    "implemented_flag": int(implemented),
                    "covered_count": len(covered_tickers),
                    "eligible_count": len(members),
                    "applicable_count": len(applicable_members),
                    "excluded_count": len(members) - len(applicable_members),
                    "coverage_fraction": f"{coverage:.6f}",
                    "minimum_count": gate.minimum_count,
                    "minimum_fraction": f"{gate.minimum_fraction:.6f}",
                    "minimum_cohort_fraction": f"{gate.minimum_cohort_fraction:.6f}",
                    "cohort_coverage": json.dumps(cohort_fractions, sort_keys=True),
                    "status": status,
                }
            )
        concept_rows = raw_concept_candidates(conn, members=members, asof=asof)
    output_dir.mkdir(parents=True, exist_ok=True)
    coverage_path = output_dir / "machinery_financial_metric_coverage.csv"
    concepts_path = output_dir / "machinery_financial_concept_candidates.csv"
    manifest_path = output_dir / "machinery_financial_metric_coverage.json"
    write_csv_atomic(coverage_path, COVERAGE_FIELDS, coverage_rows)
    write_csv_atomic(concepts_path, CONCEPT_FIELDS, concept_rows)
    summary = {
        "acceptance": "PASS" if not errors else "FAIL",
        "asof_date": asof,
        "eligible_count": len(members),
        "implemented_metric_count": sum(int(row["implemented_flag"]) for row in coverage_rows),
        "calibration_required_metric_count": sum(
            row["gate_mode"] == "calibration" for row in coverage_rows
        ),
        "calibration_ready_metric_count": sum(row["status"] == "CALIBRATION_READY" for row in coverage_rows),
        "limited_universe_metric_count": sum(
            row["gate_mode"] == "limited_universe_diagnostic" for row in coverage_rows
        ),
        "limited_universe_ready_metric_count": sum(
            row["status"] == "LIMITED_UNIVERSE_READY" for row in coverage_rows
        ),
        "availability_counts": availability_counts,
        "coverage_csv": str(coverage_path),
        "concept_candidates_csv": str(concepts_path),
        "errors": errors,
    }
    write_text_atomic(manifest_path, json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())

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


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
COVERAGE_FIELDS = [
    "metric",
    "category",
    "implemented_flag",
    "covered_count",
    "eligible_count",
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


METRIC_GATES = [
    MetricGate("orders_yoy_growth", "orders_backlog", 10, 0.10),
    MetricGate("book_to_bill", "orders_backlog", 10, 0.10),
    MetricGate("backlog_yoy_growth", "orders_backlog", 10, 0.10),
    MetricGate("backlog_to_revenue", "orders_backlog", 10, 0.10),
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


def raw_concept_candidates(conn: sqlite3.Connection, *, members: dict[str, str]) -> list[dict[str, Any]]:
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
              LOWER(concept_name) LIKE '%order%'
              OR LOWER(concept_name) LIKE '%booking%'
              OR LOWER(concept_name) LIKE '%backlog%'
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
        tuple(members),
    ).fetchall()
    return [dict(row) for row in rows]


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
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))) as conn:
        members = eligible_members(conn, asof=asof)
        if not members:
            errors.append(f"No machinery members are effective at {asof}")
        columns = table_columns(conn, "feature_financial_statement")
        cohort_sizes: dict[str, int] = {}
        for cohort in members.values():
            cohort_sizes[cohort] = cohort_sizes.get(cohort, 0) + 1
        for gate in METRIC_GATES:
            implemented = gate.metric in columns
            values = metric_values(conn, metric=gate.metric, asof=asof, members=members) if implemented else {}
            coverage = len(values) / len(members) if members else 0.0
            cohort_counts: dict[str, int] = {}
            for ticker in values:
                cohort = members[ticker]
                cohort_counts[cohort] = cohort_counts.get(cohort, 0) + 1
            cohort_fractions = {
                cohort: cohort_counts.get(cohort, 0) / size
                for cohort, size in sorted(cohort_sizes.items())
                if size > 0
            }
            minimum_cohort = min(cohort_fractions.values(), default=0.0)
            ready = (
                implemented
                and len(values) >= gate.minimum_count
                and coverage >= gate.minimum_fraction
                and minimum_cohort >= gate.minimum_cohort_fraction
            )
            status = "CALIBRATION_READY" if ready else "IMPLEMENTED_PENDING_COVERAGE" if implemented else "NOT_IMPLEMENTED"
            if args.require_calibration_ready and not ready:
                errors.append(
                    f"{gate.metric}: count={len(values)} coverage={coverage:.3f} "
                    f"minimum_cohort={minimum_cohort:.3f}"
                )
            coverage_rows.append(
                {
                    "metric": gate.metric,
                    "category": gate.category,
                    "implemented_flag": int(implemented),
                    "covered_count": len(values),
                    "eligible_count": len(members),
                    "coverage_fraction": f"{coverage:.6f}",
                    "minimum_count": gate.minimum_count,
                    "minimum_fraction": f"{gate.minimum_fraction:.6f}",
                    "minimum_cohort_fraction": f"{gate.minimum_cohort_fraction:.6f}",
                    "cohort_coverage": json.dumps(cohort_fractions, sort_keys=True),
                    "status": status,
                }
            )
        concept_rows = raw_concept_candidates(conn, members=members)
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
        "calibration_ready_metric_count": sum(row["status"] == "CALIBRATION_READY" for row in coverage_rows),
        "coverage_csv": str(coverage_path),
        "concept_candidates_csv": str(concepts_path),
        "errors": errors,
    }
    write_text_atomic(manifest_path, json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())

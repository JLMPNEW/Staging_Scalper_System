#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect, init_db  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.transportation.contracts import write_manifest  # noqa: E402
from industrials.transportation.financial_contract import (  # noqa: E402
    VALID_STATUSES,
    load_metric_registry,
)
from industrials.transportation.scripts._shared import DEFAULT_CONFIG, MODEL_FAMILY  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate transportation metric availability contracts.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--output-json", type=Path, default=None)
    parser.add_argument("--coverage-csv", type=Path, default=None)
    parser.add_argument("--strict-specialized", action="store_true")
    return parser.parse_args()


def finite(value: object) -> bool:
    try:
        return math.isfinite(float(str(value).strip()))
    except (TypeError, ValueError):
        return False


def coverage_summary(bucket: dict[str, int]) -> dict[str, int]:
    applicable = int(bucket.get("applicable", 0))
    observed = int(bucket.get("observed", 0))
    return {
        "applicable": applicable,
        "observed": observed,
        "review_required": int(bucket.get("review_required", 0)),
        "not_disclosed": int(bucket.get("not_disclosed", 0)),
        "missing_market_denominator": int(bucket.get("missing_market_denominator", 0)),
        "not_meaningful": int(bucket.get("not_meaningful", 0)),
        "coverage_bps": round(10000 * observed / applicable) if applicable else 10000,
    }


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    universe = family["universe"]
    financial = family["financial"]
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    registry_path = resolve_path(financial["metric_registry"], base_dir=base_dir)
    output_path = args.output_json.expanduser().resolve() if args.output_json else resolve_path(
        financial["metric_validation_output_json"], base_dir=base_dir
    )
    coverage_csv_path = (
        args.coverage_csv.expanduser().resolve()
        if args.coverage_csv
        else output_path.with_name("transportation_metric_coverage.csv")
    )
    registry_version, definitions = load_metric_registry(registry_path)
    definition_by_name = {item.metric_id: item for item in definitions}
    errors: list[str] = []
    warnings: list[str] = []
    coverage: dict[str, dict[str, int]] = {}
    metric_coverage = {
        item.metric_id: {
            "applicable": 0,
            "observed": 0,
            "review_required": 0,
            "not_disclosed": 0,
            "missing_market_denominator": 0,
            "not_meaningful": 0,
        }
        for item in definitions
    }
    overall_bucket = {
        "applicable": 0,
        "observed": 0,
        "review_required": 0,
        "not_disclosed": 0,
        "missing_market_denominator": 0,
        "not_meaningful": 0,
    }
    required_bucket = dict(overall_bucket)
    specialized_bucket = dict(overall_bucket)
    status_counts = {status: 0 for status in sorted(VALID_STATUSES)}
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))) as conn:
        init_db(conn)
        members = [
            dict(row)
            for row in conn.execute(
                """
                SELECT m.ticker, t.industry, t.calibration_cohort_id
                FROM dim_universe_membership AS m
                JOIN dim_industrials_taxonomy AS t
                  ON t.ticker=m.ticker AND t.model_family=m.model_family
                WHERE m.model_family=? AND m.membership_source_id=?
                  AND m.membership_status='active'
                  AND m.start_date<=? AND COALESCE(m.end_date,'9999-12-31')>=?
                ORDER BY m.ticker
                """,
                (MODEL_FAMILY, str(universe["seed_source_id"]), args.asof, args.asof),
            ).fetchall()
        ]
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT * FROM feature_financial_metric_availability
                WHERE model_family=? AND asof_date=? ORDER BY ticker, metric_name
                """,
                (MODEL_FAMILY, args.asof),
            ).fetchall()
        ]
        financial_rows = conn.execute(
            """
            SELECT ticker, cash_burn_ttm_usd
            FROM feature_financial_statement
            WHERE model_family=? AND asof_date<=?
            ORDER BY ticker, asof_date DESC, source_id ASC
            """,
            (MODEL_FAMILY, args.asof),
        ).fetchall()
        cash_burn_by_ticker: dict[str, float | None] = {}
        for financial_row in financial_rows:
            ticker = str(financial_row["ticker"])
            if ticker not in cash_burn_by_ticker:
                value = financial_row["cash_burn_ttm_usd"]
                cash_burn_by_ticker[ticker] = float(value) if finite(value) else None
    expected = {(str(member["ticker"]), metric.metric_id) for member in members for metric in definitions}
    actual = {(str(row["ticker"]), str(row["metric_name"])) for row in rows}
    if len(actual) != len(rows):
        errors.append("duplicate ticker/metric rows in availability table")
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        errors.append(f"missing metric rows={missing[:20]} count={len(missing)}")
    if extra:
        errors.append(f"unexpected metric rows={extra[:20]} count={len(extra)}")
    member_by_ticker = {str(item["ticker"]): item for item in members}
    observed_statuses = {"REPORTED", "DERIVED", "PROXY"}
    for row in rows:
        ticker = str(row["ticker"])
        metric_name = str(row["metric_name"])
        status = str(row["availability_status"])
        definition = definition_by_name.get(metric_name)
        member = member_by_ticker.get(ticker)
        if definition is None or member is None:
            continue
        if status not in VALID_STATUSES:
            errors.append(f"{ticker}:{metric_name}: invalid status={status!r}")
            continue
        status_counts[status] = status_counts.get(status, 0) + 1
        registry_applies = definition.applies_to(
            cohort=str(member["calibration_cohort_id"]), industry=str(member["industry"])
        ) and (not definition.birthdate or args.asof >= definition.birthdate)
        cash_burn = cash_burn_by_ticker.get(ticker)
        extraction_method = str(row.get("extraction_method") or "")
        reviewed_inapplicable = (
            status == "NOT_APPLICABLE"
            and extraction_method
            in {
                "reviewed_metric_availability_override",
                "reviewed_aligned_annual_formula",
            }
        )
        conditionally_inapplicable = (
            metric_name == "cash_runway_years"
            and cash_burn is not None
            and cash_burn <= 0
        ) or reviewed_inapplicable
        applies = registry_applies and not conditionally_inapplicable
        if not applies and status != "NOT_APPLICABLE":
            errors.append(f"{ticker}:{metric_name}: non-applicable metric status={status}")
        if applies and status == "NOT_APPLICABLE":
            errors.append(f"{ticker}:{metric_name}: applicable metric marked NOT_APPLICABLE")
        if status in observed_statuses and not finite(row.get("metric_value")):
            errors.append(f"{ticker}:{metric_name}: observed status requires finite value")
        if status not in observed_statuses and row.get("metric_value") not in {None, ""}:
            errors.append(f"{ticker}:{metric_name}: unavailable status must not carry a value")
        if status == "PARSER_FAILURE":
            errors.append(f"{ticker}:{metric_name}: parser failure")
        if applies:
            buckets = [overall_bucket, metric_coverage[metric_name]]
            if definition.required_for_rank:
                buckets.append(required_bucket)
            if definition.specialized:
                buckets.append(specialized_bucket)
            for bucket in buckets:
                bucket["applicable"] += 1
                if status in observed_statuses:
                    bucket["observed"] += 1
                elif status in {"DISCLOSED_UNPARSED", "PARSER_FAILURE"}:
                    bucket["review_required"] += 1
                elif status == "NOT_DISCLOSED":
                    bucket["not_disclosed"] += 1
                elif status == "MISSING_MARKET_DENOMINATOR":
                    bucket["missing_market_denominator"] += 1
                elif status == "NEGATIVE_PROFIT_NOT_MEANINGFUL":
                    bucket["not_meaningful"] += 1
        if definition.specialized and applies:
            cohort = str(member["calibration_cohort_id"])
            bucket = coverage.setdefault(
                cohort,
                {
                    "applicable": 0,
                    "observed": 0,
                    "review_required": 0,
                    "not_disclosed": 0,
                    "missing_market_denominator": 0,
                    "not_meaningful": 0,
                },
            )
            bucket["applicable"] += 1
            bucket["observed"] += int(status in observed_statuses)
            bucket["review_required"] += int(
                status in {"DISCLOSED_UNPARSED", "PARSER_FAILURE"}
            )
            bucket["not_disclosed"] += int(status == "NOT_DISCLOSED")
            bucket["missing_market_denominator"] += int(
                status == "MISSING_MARKET_DENOMINATOR"
            )
            bucket["not_meaningful"] += int(
                status == "NEGATIVE_PROFIT_NOT_MEANINGFUL"
            )
    for cohort, bucket in sorted(coverage.items()):
        fraction = bucket["observed"] / bucket["applicable"] if bucket["applicable"] else 1.0
        bucket["coverage_bps"] = round(10000 * fraction)
        if fraction == 0:
            warnings.append(f"{cohort}: zero specialized metric coverage")
        if args.strict_specialized and fraction < 1.0:
            errors.append(f"{cohort}: strict specialized coverage={fraction:.4f}")
    metric_coverage_rows: list[dict[str, Any]] = []
    for definition in definitions:
        summary = coverage_summary(metric_coverage[definition.metric_id])
        metric_coverage_rows.append(
            {
                "metric_name": definition.metric_id,
                "component": definition.component,
                "source": definition.source,
                "required_for_rank": int(definition.required_for_rank),
                "specialized": int(definition.specialized),
                **summary,
            }
        )
    coverage_fields = [
        "metric_name",
        "component",
        "source",
        "required_for_rank",
        "specialized",
        "applicable",
        "observed",
        "review_required",
        "not_disclosed",
        "missing_market_denominator",
        "not_meaningful",
        "coverage_bps",
    ]
    write_csv_atomic(coverage_csv_path, coverage_fields, metric_coverage_rows)
    result: dict[str, Any] = {
        "acceptance": "PASS" if not errors else "FAIL",
        "asof_date": args.asof,
        "registry_version": registry_version,
        "member_count": len(members),
        "metric_count": len(definitions),
        "row_count": len(rows),
        "expected_row_count": len(expected),
        "coverage_csv": str(coverage_csv_path),
        "availability_status_counts": {
            status: count for status, count in status_counts.items() if count
        },
        "overall_coverage": coverage_summary(overall_bucket),
        "required_coverage": coverage_summary(required_bucket),
        "specialized_coverage_overall": coverage_summary(specialized_bucket),
        "specialized_coverage": coverage,
        "errors": errors,
        "warnings": warnings,
    }
    write_manifest(output_path, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.selected_feature_history import (  # noqa: E402
    read_csv,
    read_json,
    sha256,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)


REPORT_FIELDS = (
    "gate_id",
    "required",
    "status",
    "actual",
    "expected",
    "detail",
)
REQUIRED_TABLES = (
    "dim_universe_membership",
    "dim_industrials_taxonomy",
    "dim_ticker_alias",
    "dim_security_continuity_policy",
    "fact_price_ohlcv",
    "fact_fx_rate",
    "fact_financial_statement_canonical",
    "feature_market_technical",
    "feature_financial_statement",
    "feature_financial_metric_availability",
    "dim_issuer_reporting_profile_history",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only transportation Stage 0-4 production-readiness audit. "
            "This gate validates loaded foundations and features; it cannot "
            "authorize OOS scoring or promotion."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default="")
    parser.add_argument("--output-json", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    return parser.parse_args()


def _count(
    connection: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
) -> int:
    row = connection.execute(sql, params).fetchone()
    return int(row[0] or 0) if row is not None else 0


def _value(
    connection: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
) -> str:
    row = connection.execute(sql, params).fetchone()
    return str(row[0] or "") if row is not None else ""


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    foundation = resolve_foundation(config_path, args.db)
    historical = family.get("historical_load") or {}
    raw_dir = resolve_path(
        historical["output_dir"],
        base_dir=config_path.parent,
    )
    raw_validation_path = (
        raw_dir / "transportation_historical_raw_load_validation.json"
    )
    policy = load_yaml(foundation.policy_path)
    active_rows = read_csv(foundation.active_path)
    delisted_rows = read_csv(foundation.delisted_path)
    historical_rows = read_csv(foundation.historical_path)
    expected_active = int(policy["expected_ticker_count"])
    expected_delisted = int(policy["expected_delisted_count"])
    checks: list[dict[str, object]] = []

    def add(
        gate_id: str,
        passed: bool,
        *,
        actual: object,
        expected: object,
        detail: str,
        required: bool = True,
    ) -> None:
        checks.append(
            {
                "gate_id": gate_id,
                "required": int(required),
                "status": "PASS" if passed else (
                    "FAIL" if required else "WARN"
                ),
                "actual": actual,
                "expected": expected,
                "detail": detail,
            }
        )

    for label, path in (
        ("active_seed", foundation.active_path),
        ("delisted_seed", foundation.delisted_path),
        ("historical_membership", foundation.historical_path),
        ("ticker_aliases", foundation.aliases_path),
        ("listing_dates", foundation.listing_path),
        ("universe_policy", foundation.policy_path),
        ("cohort_policy", foundation.cohort_path),
        ("source_registry", foundation.registry_path),
    ):
        add(
            f"file_{label}",
            path.is_file(),
            actual=str(path),
            expected="existing file",
            detail="family-scoped source contract",
        )
    add(
        "active_seed_count",
        len(active_rows) == expected_active,
        actual=len(active_rows),
        expected=expected_active,
        detail="active source-of-truth count",
    )
    add(
        "delisted_seed_count",
        len(delisted_rows) == expected_delisted,
        actual=len(delisted_rows),
        expected=expected_delisted,
        detail="curated inactive calibration count",
    )
    historical_tickers = {
        row["internal_ticker"] for row in historical_rows
    }
    expected_history = expected_active + expected_delisted
    approved_price_exclusions = {
        str(ticker).strip().upper()
        for ticker in historical.get("approved_price_exclusions", [])
        if str(ticker).strip()
    }
    source_tickers = {
        str(
            row.get("internal_ticker") or row.get("ticker") or ""
        ).strip().upper()
        for row in active_rows + delisted_rows
        if str(
            row.get("internal_ticker") or row.get("ticker") or ""
        ).strip()
    }
    expected_historical_tickers = (
        source_tickers - approved_price_exclusions
    )
    missing_historical = sorted(
        expected_historical_tickers - historical_tickers
    )
    unexpected_historical = sorted(
        historical_tickers - expected_historical_tickers
    )
    add(
        "historical_membership_ticker_count",
        historical_tickers == expected_historical_tickers,
        actual=len(historical_tickers),
        expected=len(expected_historical_tickers),
        detail=(
            "active plus delisted membership less approved price exclusions; "
            f"missing={missing_historical}; unexpected={unexpected_historical}"
        ),
    )
    if raw_validation_path.is_file():
        raw_validation = read_json(raw_validation_path)
        raw_pass = (
            raw_validation.get("acceptance") == "PASS"
            and raw_validation.get("model_family") == MODEL_FAMILY
            and not raw_validation.get("errors")
        )
        raw_asof = str(raw_validation.get("asof_date") or "")
    else:
        raw_validation = {}
        raw_pass = False
        raw_asof = ""
    add(
        "historical_raw_load_validation",
        raw_pass,
        actual=raw_validation.get("acceptance", "MISSING"),
        expected="PASS",
        detail=str(raw_validation_path),
    )
    asof = str(args.asof or raw_asof)[:10]
    if not asof:
        raise ValueError("--asof is required when raw validation has no date")

    db_path = foundation.db_path
    before = (
        db_path.stat().st_size,
        db_path.stat().st_mtime_ns,
    )
    connection = sqlite3.connect(
        f"{db_path.as_uri()}?mode=ro",
        uri=True,
        timeout=foundation.timeout_sec,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        missing_tables = [
            table for table in REQUIRED_TABLES if table not in tables
        ]
        add(
            "required_database_tables",
            not missing_tables,
            actual="|".join(missing_tables),
            expected="none missing",
            detail="Stage 0-4 database contract",
        )
        active_members = _count(
            connection,
            """
            SELECT COUNT(DISTINCT ticker)
            FROM dim_universe_membership
            WHERE model_family=?
              AND membership_status='active'
              AND start_date<=?
              AND COALESCE(end_date, '9999-12-31')>=?
            """,
            (MODEL_FAMILY, asof, asof),
        )
        add(
            "active_membership_count",
            active_members == expected_active,
            actual=active_members,
            expected=expected_active,
            detail=f"point-in-time active membership at {asof}",
        )
        all_members = _count(
            connection,
            """
            SELECT COUNT(DISTINCT ticker)
            FROM dim_universe_membership
            WHERE model_family=?
            """,
            (MODEL_FAMILY,),
        )
        add(
            "all_membership_count",
            all_members == expected_history,
            actual=all_members,
            expected=expected_history,
            detail="survivorship membership breadth",
        )
        taxonomy = _count(
            connection,
            """
            SELECT COUNT(DISTINCT ticker)
            FROM dim_industrials_taxonomy
            WHERE model_family=?
            """,
            (MODEL_FAMILY,),
        )
        add(
            "taxonomy_count",
            taxonomy >= expected_history,
            actual=taxonomy,
            expected=f">={expected_history}",
            detail="family-scoped cohort assignments",
        )
        market_count = _count(
            connection,
            """
            SELECT COUNT(DISTINCT ticker)
            FROM feature_market_technical
            WHERE model_family=? AND asof_date=?
            """,
            (MODEL_FAMILY, asof),
        )
        add(
            "market_feature_count",
            market_count == expected_active,
            actual=market_count,
            expected=expected_active,
            detail=f"exact-date market features at {asof}",
        )
        financial_count = _count(
            connection,
            """
            SELECT COUNT(DISTINCT ticker)
            FROM feature_financial_statement
            WHERE model_family=? AND asof_date=?
            """,
            (MODEL_FAMILY, asof),
        )
        add(
            "financial_feature_count",
            financial_count == expected_active,
            actual=financial_count,
            expected=expected_active,
            detail=f"exact-date financial features at {asof}",
        )
        availability_tickers = _count(
            connection,
            """
            SELECT COUNT(DISTINCT ticker)
            FROM feature_financial_metric_availability
            WHERE model_family=? AND asof_date=?
            """,
            (MODEL_FAMILY, asof),
        )
        add(
            "metric_availability_ticker_count",
            availability_tickers == expected_active,
            actual=availability_tickers,
            expected=expected_active,
            detail=f"explicit metric states at {asof}",
        )
        profiles = _count(
            connection,
            """
            SELECT COUNT(DISTINCT ticker)
            FROM dim_issuer_reporting_profile_history
            WHERE model_family=? AND profile_asof_date<=?
              AND ticker IN (
                SELECT ticker FROM dim_universe_membership
                WHERE model_family=?
                  AND membership_status='active'
                  AND start_date<=?
                  AND COALESCE(end_date, '9999-12-31')>=?
              )
            """,
            (MODEL_FAMILY, asof, MODEL_FAMILY, asof, asof),
        )
        add(
            "reporting_profile_count",
            profiles == expected_active,
            actual=profiles,
            expected=expected_active,
            detail="PIT reporting profiles for active universe",
        )
        benchmarks = tuple(policy.get("benchmark_tickers") or ())
        benchmark_failures: list[str] = []
        for ticker in benchmarks:
            last = _value(
                connection,
                """
                SELECT MAX(bar_date) FROM fact_price_ohlcv
                WHERE ticker=? AND source_id='yahoo_finance_adjusted'
                  AND is_adjusted=1 AND bar_date<=?
                """,
                (ticker, asof),
            )
            if last != asof:
                benchmark_failures.append(f"{ticker}:{last}")
        add(
            "benchmark_price_right_edge",
            not benchmark_failures,
            actual="|".join(benchmark_failures),
            expected=f"{'|'.join(benchmarks)} at {asof}",
            detail="IYT/XTN/SPY adjusted benchmark coverage",
        )
    finally:
        connection.close()
    after = (
        db_path.stat().st_size,
        db_path.stat().st_mtime_ns,
    )
    add(
        "database_read_only",
        before == after,
        actual=str(after),
        expected=str(before),
        detail="database size and modification time unchanged",
    )

    failures = [
        row["gate_id"]
        for row in checks
        if row["required"] == 1 and row["status"] != "PASS"
    ]
    acceptance = "PASS" if not failures else "FAIL"
    output_root = (
        PROJECT_ROOT
        / "output"
        / "industrials"
        / "transportation"
        / "production_readiness"
    )
    output_json = (
        args.output_json.expanduser().resolve()
        if args.output_json
        else output_root
        / "transportation_stage0_4_production_readiness.json"
    )
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else output_root
        / "transportation_stage0_4_production_readiness.csv"
    )
    write_csv_atomic(output_csv, REPORT_FIELDS, checks)
    payload = {
        "acceptance": acceptance,
        "gate": "TRANSPORTATION_STAGE0_4_PRODUCTION_READINESS",
        "model_family": MODEL_FAMILY,
        "asof_date": asof,
        "database_path": str(db_path),
        "database_open_mode": "read_only",
        "check_count": len(checks),
        "required_failure_count": len(failures),
        "required_failures": failures,
        "report_csv": str(output_csv),
        "report_csv_sha256": sha256(output_csv),
        "production_promotion_authorized": False,
        "oos_score_valid_authorized": False,
        "next_gate": (
            "IMPLEMENT_AND_VALIDATE_STAGE5_POSITIONING"
            if acceptance == "PASS"
            else "REPAIR_STAGE0_4_PRODUCTION_READINESS"
        ),
    }
    write_text_atomic(
        output_json,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if acceptance == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())

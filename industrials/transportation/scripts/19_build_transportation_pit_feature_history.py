#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.transportation.contracts import write_manifest  # noqa: E402
from industrials.transportation.financial_contract import load_metric_registry  # noqa: E402
from industrials.transportation.scripts._shared import DEFAULT_CONFIG, MODEL_FAMILY  # noqa: E402


FIELDS = [
    "asof_date",
    "expected_ticker_count",
    "market_feature_count",
    "financial_feature_count",
    "metric_availability_count",
    "expected_metric_availability_count",
    "reporting_profile_count",
    "status",
    "elapsed_seconds",
    "output_dir",
    "message",
]
STAGES = (
    "reporting_profiles",
    "market_features",
    "financial_features",
    "specialized_metrics",
)
SNAPSHOT_ARTIFACTS = (
    "reporting_profiles.csv",
    "market_features.csv",
    "financial_features.csv",
    "metric_availability.csv",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build resumable point-in-time transportation market, financial, "
            "and specialized feature history from already-loaded raw data."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", required=True)
    parser.add_argument(
        "--dates",
        default="",
        help="Optional comma-separated explicit as-of dates; bypasses cadence selection.",
    )
    parser.add_argument(
        "--max-dates",
        type=int,
        default=0,
        help="Optional bounded batch size after skipping completed dates.",
    )
    parser.add_argument(
        "--rebuild-existing",
        action="store_true",
        help="Rebuild dates whose exact database coverage already passes.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--output-json", type=Path, default=None)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def read_existing_report(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return {
            str(row.get("asof_date") or ""): dict(row)
            for row in csv.DictReader(handle)
            if str(row.get("asof_date") or "")
        }


def read_only_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def explicit_dates(raw: str) -> list[str]:
    return sorted(
        {
            item.strip()[:10]
            for item in str(raw or "").split(",")
            if item.strip()
        }
    )


def validated_date(value: str, *, label: str) -> str:
    text = str(value or "")[:10]
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid {label}={value!r}; expected YYYY-MM-DD") from exc
    return text


def month_end_dates(
    connection: sqlite3.Connection,
    *,
    ticker: str,
    source_id: str,
    start_date: str,
    end_date: str,
) -> list[str]:
    rows = connection.execute(
        """
        SELECT MAX(bar_date) AS asof_date
        FROM fact_price_ohlcv
        WHERE ticker=? AND source_id=?
          AND bar_date>=? AND bar_date<=?
        GROUP BY SUBSTR(bar_date, 1, 7)
        ORDER BY asof_date
        """,
        (ticker, source_id, start_date, end_date),
    ).fetchall()
    dates = [str(row["asof_date"]) for row in rows if str(row["asof_date"] or "")]
    start_bar = connection.execute(
        """
        SELECT MIN(bar_date)
        FROM fact_price_ohlcv
        WHERE ticker=? AND source_id=? AND bar_date>=? AND bar_date<=?
        """,
        (ticker, source_id, start_date, end_date),
    ).fetchone()
    first = str(start_bar[0] or "") if start_bar else ""
    if first and first not in dates:
        dates.insert(0, first)
    return dates


def coverage_counts(
    connection: sqlite3.Connection,
    *,
    asof: str,
    metric_count: int,
) -> dict[str, int]:
    expected = int(
        connection.execute(
            """
            SELECT COUNT(DISTINCT ticker)
            FROM dim_universe_membership
            WHERE model_family=?
              AND start_date<=?
              AND COALESCE(end_date, '9999-12-31')>=?
            """,
            (MODEL_FAMILY, asof, asof),
        ).fetchone()[0]
    )
    market = int(
        connection.execute(
            """
            SELECT COUNT(DISTINCT ticker)
            FROM feature_market_technical
            WHERE model_family=? AND asof_date=?
            """,
            (MODEL_FAMILY, asof),
        ).fetchone()[0]
    )
    financial = int(
        connection.execute(
            """
            SELECT COUNT(DISTINCT ticker)
            FROM feature_financial_statement
            WHERE model_family=? AND asof_date=?
            """,
            (MODEL_FAMILY, asof),
        ).fetchone()[0]
    )
    availability = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM feature_financial_metric_availability
            WHERE model_family=? AND asof_date=?
            """,
            (MODEL_FAMILY, asof),
        ).fetchone()[0]
    )
    profiles = int(
        connection.execute(
            """
            SELECT COUNT(DISTINCT ticker)
            FROM dim_issuer_reporting_profile_history
            WHERE model_family=? AND profile_asof_date<=?
              AND ticker IN (
                SELECT ticker
                FROM dim_universe_membership
                WHERE model_family=?
                  AND start_date<=?
                  AND COALESCE(end_date, '9999-12-31')>=?
              )
            """,
            (MODEL_FAMILY, asof, MODEL_FAMILY, asof, asof),
        ).fetchone()[0]
    )
    return {
        "expected": expected,
        "market": market,
        "financial": financial,
        "availability": availability,
        "expected_availability": expected * metric_count,
        "profiles": profiles,
    }


def coverage_passes(counts: dict[str, int]) -> bool:
    return (
        counts["expected"] > 0
        and counts["market"] == counts["expected"]
        and counts["financial"] == counts["expected"]
        and counts["availability"] == counts["expected_availability"]
        and counts["profiles"] == counts["expected"]
    )


def snapshot_is_complete(
    *,
    asof: str,
    counts: dict[str, int],
    report_row: dict[str, Any] | None,
    output_root: Path,
) -> bool:
    output_dir = output_root / asof
    return (
        coverage_passes(counts)
        and str((report_row or {}).get("status") or "") == "PASS"
        and all(
            (output_dir / filename).is_file()
            and (output_dir / filename).stat().st_size > 0
            for filename in SNAPSHOT_ARTIFACTS
        )
    )


def run_stage(
    *,
    command: list[str],
    output_dir: Path,
    stage: str,
    environment: dict[str, str],
) -> None:
    stdout_path = output_dir / f"{stage}.stdout.log"
    stderr_path = output_dir / f"{stage}.stderr.log"
    with (
        stdout_path.open("w", encoding="utf-8") as stdout_handle,
        stderr_path.open("w", encoding="utf-8") as stderr_handle,
    ):
        subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            env=environment,
            stdout=stdout_handle,
            stderr=stderr_handle,
            check=True,
        )


def stage_commands(
    *,
    asof: str,
    config_path: Path,
    db_path: Path,
    output_dir: Path,
) -> list[tuple[str, list[str]]]:
    python = sys.executable
    common = ["--config", str(config_path), "--db", str(db_path)]
    return [
        (
            "reporting_profiles",
            [
                python,
                str(
                    PROJECT_ROOT
                    / "industrials"
                    / "scripts"
                    / "07_sync_industrials_sec_fundamentals.py"
                ),
                *common,
                "--model-family",
                MODEL_FAMILY,
                "--include-historical",
                "--profiles-only",
                "--asof",
                asof,
                "--output-csv",
                str(output_dir / "reporting_profiles.csv"),
            ],
        ),
        (
            "market_features",
            [
                python,
                str(
                    PROJECT_ROOT
                    / "industrials"
                    / "scripts"
                    / "05_build_industrials_market_features.py"
                ),
                *common,
                "--model-family",
                MODEL_FAMILY,
                "--benchmark-tickers",
                "IYT,XTN,SPY",
                "--primary-benchmark",
                "IYT",
                "--asof",
                asof,
                "--output-csv",
                str(output_dir / "market_features.csv"),
            ],
        ),
        (
            "financial_features",
            [
                python,
                str(
                    PROJECT_ROOT
                    / "industrials"
                    / "scripts"
                    / "08_build_industrials_financial_features.py"
                ),
                *common,
                "--model-family",
                MODEL_FAMILY,
                "--asof",
                asof,
                "--suppress-data-quality-issues",
                "--output-csv",
                str(output_dir / "financial_features.csv"),
            ],
        ),
        (
            "specialized_metrics",
            [
                python,
                str(
                    PROJECT_ROOT
                    / "industrials"
                    / "transportation"
                    / "scripts"
                    / "08a_build_transportation_specialized_metrics.py"
                ),
                *common,
                "--include-historical",
                "--asof",
                asof,
                "--output-csv",
                str(output_dir / "metric_availability.csv"),
            ],
        ),
    ]


def main() -> int:
    args = parse_args()
    if args.max_dates < 0:
        raise ValueError("--max-dates cannot be negative")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    historical = family["historical_features"]
    specialized = family["specialized_disclosures"]
    financial = family["financial"]
    base_dir = config_path.parent
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    start_date = validated_date(
        str(args.start_date or historical["start_date"]),
        label="start date",
    )
    end_date = validated_date(str(args.end_date), label="end date")
    if start_date > end_date:
        raise ValueError("--start-date cannot be after --end-date")
    output_root = resolve_path(historical["output_root"], base_dir=base_dir)
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(historical["build_report_csv"], base_dir=base_dir)
    )
    output_json = (
        args.output_json.expanduser().resolve()
        if args.output_json
        else resolve_path(historical["build_manifest_json"], base_dir=base_dir)
    )
    specialized_validation = resolve_path(
        specialized["historical_validation_output_json"], base_dir=base_dir
    )
    specialized_gate = read_json(specialized_validation)
    if specialized_gate.get("acceptance") != "PASS":
        raise ValueError(
            "Historical specialized-disclosure validation must PASS before "
            "building PIT feature history"
        )
    registry_path = resolve_path(financial["metric_registry"], base_dir=base_dir)
    _, metrics = load_metric_registry(registry_path)
    metric_count = len(metrics)
    with read_only_connection(db_path) as connection:
        dates = explicit_dates(args.dates)
        dates = [
            validated_date(value, label="snapshot date")
            for value in dates
        ]
        if not dates:
            dates = month_end_dates(
                connection,
                ticker=str(historical["benchmark_ticker"]),
                source_id=str(
                    cfg_get(
                        config,
                        "market_data_policy.scoring_primary_source",
                        "yahoo_finance_adjusted",
                    )
                ),
                start_date=start_date,
                end_date=end_date,
            )
        invalid_dates = [
            value for value in dates if value < start_date or value > end_date
        ]
        if invalid_dates:
            raise ValueError(f"Snapshot dates outside requested range={invalid_dates}")
        initial_counts = {
            asof: coverage_counts(connection, asof=asof, metric_count=metric_count)
            for asof in dates
        }
    report_by_date = read_existing_report(output_csv)
    pending = [
        asof
        for asof in dates
        if args.rebuild_existing
        or not snapshot_is_complete(
            asof=asof,
            counts=initial_counts[asof],
            report_row=report_by_date.get(asof),
            output_root=output_root,
        )
    ]
    if args.max_dates:
        pending = pending[: args.max_dates]
    if args.dry_run:
        result = {
            "acceptance": "DRY_RUN",
            "start_date": start_date,
            "end_date": end_date,
            "observation_cadence": (
                "explicit_dates" if args.dates else historical["observation_cadence"]
            ),
            "selected_date_count": len(dates),
            "pending_date_count": len(pending),
            "pending_dates": pending,
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    environment = os.environ.copy()
    environment["INDUSTRIALS_HISTORICAL_APPEND"] = "1"
    failures: list[str] = []
    for asof in pending:
        started = time.monotonic()
        output_dir = output_root / asof
        output_dir.mkdir(parents=True, exist_ok=True)
        status = "PASS"
        message = ""
        try:
            for stage, command in stage_commands(
                asof=asof,
                config_path=config_path,
                db_path=db_path,
                output_dir=output_dir,
            ):
                run_stage(
                    command=command,
                    output_dir=output_dir,
                    stage=stage,
                    environment=environment,
                )
            with read_only_connection(db_path) as connection:
                counts = coverage_counts(
                    connection,
                    asof=asof,
                    metric_count=metric_count,
                )
            if not coverage_passes(counts):
                raise ValueError(f"incomplete exact-date feature coverage={counts}")
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            status = "FAIL"
            message = f"{type(exc).__name__}: {exc}"
            failures.append(f"{asof}:{message}")
            counts = initial_counts[asof]
        report_by_date[asof] = {
            "asof_date": asof,
            "expected_ticker_count": counts["expected"],
            "market_feature_count": counts["market"],
            "financial_feature_count": counts["financial"],
            "metric_availability_count": counts["availability"],
            "expected_metric_availability_count": counts[
                "expected_availability"
            ],
            "reporting_profile_count": counts["profiles"],
            "status": status,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "output_dir": str(output_dir),
            "message": message,
        }
        write_csv_atomic(
            output_csv,
            FIELDS,
            [report_by_date[key] for key in sorted(report_by_date)],
        )
        print(
            f"transportation_pit_progress asof={asof} status={status}",
            flush=True,
        )
        if status == "FAIL":
            break

    completed_dates = sorted(
        asof
        for asof, row in report_by_date.items()
        if str(row.get("status") or "") == "PASS"
    )
    result = {
        "acceptance": "PASS" if not failures else "FAIL",
        "model_family": MODEL_FAMILY,
        "start_date": start_date,
        "end_date": end_date,
        "observation_cadence": (
            "explicit_dates" if args.dates else historical["observation_cadence"]
        ),
        "selected_date_count": len(dates),
        "attempted_date_count": len(pending),
        "completed_date_count": len(completed_dates),
        "completed_dates": completed_dates,
        "metric_count": metric_count,
        "output_csv": str(output_csv),
        "errors": failures,
    }
    write_manifest(output_json, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())

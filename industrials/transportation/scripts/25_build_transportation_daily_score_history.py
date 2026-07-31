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

from industrials.core.config import (  # noqa: E402
    cfg_get,
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.historical_score_history import (  # noqa: E402
    benchmark_trading_dates,
    run_logged,
    select_dates,
    valid_score_snapshot,
)
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.transportation.contracts import write_manifest  # noqa: E402
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)

RANK_FILENAME = "transportation_final_rank_table.csv"
SIDECAR_FILENAME = "transportation_stage11_survivorship_calibration_panel.csv"
RANK_MANIFEST_FILENAME = "transportation_final_rank_table_manifest.json"
VALIDATION_FILENAME = "transportation_final_rank_table_validation.json"
REPORT_FIELDS = (
    "asof_date",
    "expected_ticker_count",
    "market_feature_count",
    "positioning_feature_count",
    "rank_row_count",
    "stage11_eligible_count",
    "status",
    "elapsed_seconds",
    "message",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build resumable daily transportation PIT market features, scores, "
            "rank snapshots, and Stage 11 survivorship sidecars from already-"
            "loaded raw history and sealed monthly financial snapshots."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--max-dates", type=int, default=0)
    parser.add_argument(
        "--selection", choices=("oldest", "newest"), default="oldest"
    )
    parser.add_argument("--rebuild-existing", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--output-json", type=Path, default=None)
    return parser.parse_args()


def iso_date(raw: str, *, label: str) -> str:
    value = str(raw or "")[:10]
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"invalid {label}={raw!r}; expected YYYY-MM-DD") from exc


def read_only_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def read_report(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return {
            str(row.get("asof_date") or ""): dict(row)
            for row in csv.DictReader(handle)
            if str(row.get("asof_date") or "")
        }


def exact_counts(connection: sqlite3.Connection, *, asof: str) -> dict[str, int]:
    expected = int(
        connection.execute(
            """
            SELECT COUNT(DISTINCT ticker)
            FROM dim_universe_membership
            WHERE model_family=? AND start_date<=?
              AND COALESCE(end_date,'9999-12-31')>=?
            """,
            (MODEL_FAMILY, asof, asof),
        ).fetchone()[0]
    )
    market = int(
        connection.execute(
            """
            SELECT COUNT(DISTINCT ticker) FROM feature_market_technical
            WHERE model_family=? AND asof_date=?
            """,
            (MODEL_FAMILY, asof),
        ).fetchone()[0]
    )
    positioning = int(
        connection.execute(
            """
            SELECT COUNT(DISTINCT ticker) FROM feature_positioning
            WHERE model_family=? AND asof_date=?
            """,
            (MODEL_FAMILY, asof),
        ).fetchone()[0]
    )
    return {"expected": expected, "market": market, "positioning": positioning}


def stage_commands(
    *,
    asof: str,
    config_path: Path,
    db_path: Path,
    feature_dir: Path,
    dashboard_dir: Path,
    force_scoring: bool,
    force_publish: bool,
) -> list[tuple[str, list[str]]]:
    python = sys.executable
    common = ["--config", str(config_path), "--db", str(db_path)]
    scoring = [
        python,
        str(
            PROJECT_ROOT
            / "industrials"
            / "transportation"
            / "scripts"
            / "06a_build_transportation_scoring_features.py"
        ),
        *common,
        "--asof",
        asof,
        "--membership-mode",
        "pit",
        "--metric-snapshot-mode",
        "latest",
        "--output-csv",
        str(feature_dir / "scoring_features.csv"),
    ]
    if force_scoring:
        scoring.append("--force")
    publish = [
        python,
        str(
            PROJECT_ROOT
            / "industrials"
            / "transportation"
            / "scripts"
            / "17_publish_transportation_shadow_rank_table.py"
        ),
        "--config",
        str(config_path),
        "--asof",
        asof,
        "--input-csv",
        str(feature_dir / "scoring_features.csv"),
        "--output-dir",
        str(dashboard_dir),
    ]
    if force_publish:
        publish.append("--force")
    return [
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
                str(feature_dir / "market_features.csv"),
            ],
        ),
        (
            "positioning_features",
            [
                python,
                str(
                    PROJECT_ROOT
                    / "industrials"
                    / "scripts"
                    / "09_import_industrials_positioning.py"
                ),
                *common,
                "--model-family",
                MODEL_FAMILY,
                "--asof",
                asof,
                "--include-historical-members",
                "--feature-membership-mode",
                "pit",
                "--features-only",
                "--output-csv",
                str(feature_dir / "positioning_features.csv"),
            ],
        ),
        ("scoring_features", scoring),
        ("publish_rank", publish),
        (
            "validate_rank",
            [
                python,
                str(
                    PROJECT_ROOT
                    / "industrials"
                    / "transportation"
                    / "scripts"
                    / "18_validate_transportation_shadow_rank_table.py"
                ),
                *common,
                "--asof",
                asof,
                "--membership-mode",
                "pit",
                "--input-csv",
                str(dashboard_dir / RANK_FILENAME),
                "--output-json",
                str(dashboard_dir / VALIDATION_FILENAME),
            ],
        ),
    ]


def snapshot_valid(
    *, feature_dir: Path, dashboard_dir: Path
) -> bool:
    return valid_score_snapshot(
        snapshot_dir=dashboard_dir,
        rank_filename=RANK_FILENAME,
        sidecar_filename=SIDECAR_FILENAME,
        rank_manifest_filename=RANK_MANIFEST_FILENAME,
        validation_filename=VALIDATION_FILENAME,
        scoring_manifest=(feature_dir / "scoring_features.manifest.json"),
        membership_mode="pit",
        metric_snapshot_mode="latest",
    )


def main() -> int:
    args = parse_args()
    if args.max_dates < 0:
        raise ValueError("--max-dates cannot be negative")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    historical = family["historical_features"]
    score_history = family["historical_scores"]
    scoring = family["scoring"]
    base_dir = config_path.parent
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    start_date = iso_date(
        str(args.start_date or score_history["start_date"]), label="start date"
    )
    end_date = iso_date(args.end_date, label="end date")
    if start_date > end_date:
        raise ValueError("--start-date cannot be after --end-date")
    feature_root = resolve_path(historical["output_root"], base_dir=base_dir)
    dashboard_root = resolve_path(scoring["dashboard_root"], base_dir=base_dir)
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(score_history["build_report_csv"], base_dir=base_dir)
    )
    output_json = (
        args.output_json.expanduser().resolve()
        if args.output_json
        else resolve_path(score_history["build_manifest_json"], base_dir=base_dir)
    )
    with read_only_connection(db_path) as connection:
        dates = benchmark_trading_dates(
            connection,
            ticker=str(score_history["benchmark_ticker"]),
            source_id=str(score_history["benchmark_source_id"]),
            start_date=start_date,
            end_date=end_date,
        )
    if not dates:
        raise ValueError("no benchmark trading dates in requested range")
    valid_before = {
        asof
        for asof in dates
        if snapshot_valid(
            feature_dir=feature_root / asof,
            dashboard_dir=dashboard_root / asof,
        )
    }
    pending_all = [
        asof for asof in dates if args.rebuild_existing or asof not in valid_before
    ]
    pending = select_dates(
        pending_all,
        maximum=args.max_dates,
        selection=args.selection,
    )
    if args.dry_run:
        result = {
            "acceptance": "DRY_RUN",
            "model_family": MODEL_FAMILY,
            "start_date": start_date,
            "end_date": end_date,
            "selected_date_count": len(dates),
            "valid_existing_date_count": len(valid_before),
            "pending_date_count": len(pending_all),
            "batch_date_count": len(pending),
            "batch_dates": pending,
            "membership_mode": "pit",
            "metric_snapshot_mode": "latest",
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    report_by_date = read_report(output_csv)
    environment = os.environ.copy()
    environment["INDUSTRIALS_HISTORICAL_APPEND"] = "1"
    failures: list[str] = []
    for asof in pending:
        started = time.monotonic()
        feature_dir = feature_root / asof
        dashboard_dir = dashboard_root / asof
        feature_dir.mkdir(parents=True, exist_ok=True)
        dashboard_dir.mkdir(parents=True, exist_ok=True)
        status = "PASS"
        message = ""
        try:
            force_scoring = (feature_dir / "scoring_features.csv").exists()
            force_publish = any(
                (dashboard_dir / name).exists()
                for name in (
                    RANK_FILENAME,
                    SIDECAR_FILENAME,
                    RANK_MANIFEST_FILENAME,
                )
            )
            with read_only_connection(db_path) as connection:
                pre_counts = exact_counts(connection, asof=asof)
            for stage, command in stage_commands(
                asof=asof,
                config_path=config_path,
                db_path=db_path,
                feature_dir=feature_dir,
                dashboard_dir=dashboard_dir,
                force_scoring=force_scoring,
                force_publish=force_publish,
            ):
                # Bulk-prepared shared features are immutable inputs here. Do
                # not rerun an expensive exact-date builder when DB coverage
                # and its dated artifact are already complete.
                if (
                    stage == "market_features"
                    and pre_counts["market"] == pre_counts["expected"]
                    and (feature_dir / "market_features.csv").is_file()
                ):
                    continue
                if (
                    stage == "positioning_features"
                    and pre_counts["positioning"] == pre_counts["expected"]
                    and (feature_dir / "positioning_features.csv").is_file()
                ):
                    continue
                # Financial/specialized values are selected from the latest
                # sealed snapshot <= asof by the scoring stage.
                run_logged(
                    command,
                    cwd=PROJECT_ROOT,
                    stdout_path=feature_dir / f"daily_score_{stage}.stdout.log",
                    stderr_path=feature_dir / f"daily_score_{stage}.stderr.log",
                    environment=environment,
                )
            if not snapshot_valid(
                feature_dir=feature_dir, dashboard_dir=dashboard_dir
            ):
                raise ValueError("published score snapshot failed immutable validation")
            with read_only_connection(db_path) as connection:
                counts = exact_counts(connection, asof=asof)
            validation = json.loads(
                (dashboard_dir / VALIDATION_FILENAME).read_text(encoding="utf-8")
            )
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            status = "FAIL"
            message = f"{type(exc).__name__}: {exc}"
            failures.append(f"{asof}:{message}")
            with read_only_connection(db_path) as connection:
                counts = exact_counts(connection, asof=asof)
            validation = {}
        report_by_date[asof] = {
            "asof_date": asof,
            "expected_ticker_count": counts["expected"],
            "market_feature_count": counts["market"],
            "positioning_feature_count": counts["positioning"],
            "rank_row_count": int(validation.get("row_count") or 0),
            "stage11_eligible_count": int(
                validation.get("stage11_calibration_input_eligible_count") or 0
            ),
            "status": status,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "message": message,
        }
        write_csv_atomic(
            output_csv,
            REPORT_FIELDS,
            [report_by_date[key] for key in sorted(report_by_date)],
        )
        print(
            f"transportation_daily_score_progress asof={asof} status={status}",
            flush=True,
        )
        if status == "FAIL":
            break

    valid_after = [
        asof
        for asof in dates
        if snapshot_valid(
            feature_dir=feature_root / asof,
            dashboard_dir=dashboard_root / asof,
        )
    ]
    remaining = sorted(set(dates) - set(valid_after))
    acceptance = (
        "FAIL"
        if failures
        else "PASS"
        if not remaining
        else "PARTIAL_PASS"
    )
    result = {
        "acceptance": acceptance,
        "model_family": MODEL_FAMILY,
        "history_contract_version": "industrials_daily_pit_score_history_v1",
        "start_date": start_date,
        "end_date": end_date,
        "observation_cadence": "daily_benchmark_trading_sessions",
        "benchmark_ticker": str(score_history["benchmark_ticker"]),
        "membership_mode": "pit",
        "metric_snapshot_mode": "latest",
        "financial_snapshot_cadence": str(historical["observation_cadence"]),
        "selected_date_count": len(dates),
        "attempted_date_count": len(pending),
        "completed_date_count": len(valid_after),
        "remaining_date_count": len(remaining),
        "first_completed_date": valid_after[0] if valid_after else "",
        "last_completed_date": valid_after[-1] if valid_after else "",
        "remaining_dates": remaining,
        "build_report_csv": str(output_csv),
        "dashboard_root": str(dashboard_root),
        "errors": failures,
    }
    write_manifest(output_json, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

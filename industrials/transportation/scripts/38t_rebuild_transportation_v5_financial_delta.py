#!/usr/bin/env python3
"""Rebuild only v5 financial/availability snapshots affected by reviewed repairs."""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.contracts import file_sha256  # noqa: E402
from industrials.transportation.scripts._shared import DEFAULT_CONFIG, MODEL_FAMILY  # noqa: E402


ROOT = PROJECT_ROOT / "output" / "industrials" / "transportation"
DEFAULT_CONTRACT = ROOT / "investable_v5" / "prebuild_contract" / "2026-08-15" / "transportation_v5_prebuild_contract.json"
DEFAULT_VALIDATION = ROOT / "investable_v5" / "historical_rebuild" / "2026-08-15" / "transportation_v5_historical_rebuild_validation.json"
DEFAULT_MATERIALIZATION = ROOT / "investable_v5" / "ticker_scoped_xbrl" / "2026-07-30" / "transportation_v5_ticker_scoped_xbrl_materialization.json"
DEFAULT_OUTPUT_DIR = ROOT / "investable_v5" / "financial_delta" / "2026-08-15"
TARGET_RATIOS = ("operating_margin", "fcf_margin", "capex_to_revenue")
OBSERVED = ("REPORTED", "DERIVED", "PROXY")
REPORT_FIELDS = ("asof_date", "ticker", "status", "elapsed_seconds", "error")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--validation", type=Path, default=DEFAULT_VALIDATION)
    parser.add_argument("--materialization", type=Path, default=DEFAULT_MATERIALIZATION)
    parser.add_argument("--additional-repair-artifact", type=Path, action="append", default=[])
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--tickers", default="TRMD")
    parser.add_argument("--max-dates", type=int, default=0)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def completed_snapshot_rows(output_dir: Path, tickers: set[str]) -> dict[tuple[str, str], dict[str, Any]]:
    """Recover successful prior delta snapshots so bounded canaries remain in lineage."""
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    report_path = output_dir / "transportation_v5_financial_delta.csv"
    if report_path.exists():
        for row in read_csv(report_path):
            ticker = str(row.get("ticker") or "").strip().upper()
            asof = str(row.get("asof_date") or "").strip()
            if ticker in tickers and asof and row.get("status") == "PASS":
                rows[(asof, ticker)] = dict(row)
    if not output_dir.exists():
        return rows
    for date_dir in output_dir.iterdir():
        if not date_dir.is_dir():
            continue
        asof = date_dir.name
        for ticker in tickers:
            snapshot_dir = date_dir / ticker
            if not (
                (snapshot_dir / "financial_features.csv").is_file()
                and (snapshot_dir / "metric_availability.csv").is_file()
            ):
                continue
            rows.setdefault(
                (asof, ticker),
                {
                    "asof_date": asof,
                    "ticker": ticker,
                    "status": "PASS",
                    "elapsed_seconds": "",
                    "error": "",
                },
            )
    return rows


def main() -> int:
    args = parse_args()
    if args.max_dates < 0:
        raise ValueError("--max-dates cannot be negative")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(
        cfg_get(config, "paths.database_path"), base_dir=config_path.parent
    )
    contract_path = args.contract.expanduser().resolve()
    validation_path = args.validation.expanduser().resolve()
    materialization_path = args.materialization.expanduser().resolve()
    contract = read_json(contract_path)
    validation = read_json(validation_path)
    materialization = read_json(materialization_path)
    if contract.get("acceptance") != "PASS":
        raise ValueError("prebuild contract is not accepted")
    if materialization.get("acceptance") != "PASS" or materialization.get("mode") != "execute":
        raise ValueError("ticker-scoped materialization was not executed successfully")
    repair_artifacts = [materialization_path]
    for raw_path in args.additional_repair_artifact:
        repair_path = raw_path.expanduser().resolve()
        repair = read_json(repair_path)
        if repair.get("acceptance") != "PASS" or repair.get("mode") != "execute":
            raise ValueError(f"additional repair artifact was not executed={repair_path}")
        repair_artifacts.append(repair_path)
    tickers = sorted({value.strip().upper() for value in args.tickers.split(",") if value.strip()})
    allowed = set()
    for cohort in dict(validation.get("cohort_results") or {}).values():
        allowed.update(dict(cohort.get("missing_required_dates_by_ticker") or {}))
    if not tickers or not set(tickers) <= allowed:
        raise ValueError(f"delta ticker scope is not a validated missing scope={tickers}")
    source_dates_path = Path(str(contract["artifacts"]["source_readiness_by_date"]["path"]))
    dates = sorted({row["asof_date"] for row in read_csv(source_dates_path)})
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        pending: list[tuple[str, str]] = []
        for asof in dates:
            for ticker in tickers:
                marks = ",".join("?" for _ in TARGET_RATIOS)
                ready = int(
                    connection.execute(
                        f"""
                        SELECT COUNT(DISTINCT metric_name)
                        FROM feature_financial_metric_availability
                        WHERE model_family=? AND ticker=? AND asof_date=?
                          AND metric_name IN ({marks})
                          AND availability_status IN ({','.join('?' for _ in OBSERVED)})
                        """,
                        (MODEL_FAMILY, ticker, asof, *TARGET_RATIOS, *OBSERVED),
                    ).fetchone()[0]
                )
                if ready != len(TARGET_RATIOS):
                    pending.append((asof, ticker))
    finally:
        connection.close()
    if args.max_dates:
        allowed_dates = set(sorted({asof for asof, _ in pending})[: args.max_dates])
        pending = [item for item in pending if item[0] in allowed_dates]
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["INDUSTRIALS_HISTORICAL_APPEND"] = "1"
    prior_rows = completed_snapshot_rows(output_dir, set(tickers))
    rows: list[dict[str, Any]] = list(prior_rows.values())
    run_rows: list[dict[str, Any]] = []
    failures: list[str] = []
    for asof, ticker in pending:
        started = time.monotonic()
        snapshot_dir = output_dir / asof / ticker
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        commands = (
            (
                "financial",
                [
                    sys.executable,
                    str(PROJECT_ROOT / "industrials" / "transportation" / "scripts" / "08_build_transportation_financial_features.py"),
                    "--config", str(config_path), "--db", str(db_path),
                    "--tickers", ticker, "--model-family", MODEL_FAMILY,
                    "--asof", asof, "--suppress-data-quality-issues",
                    "--output-csv", str(snapshot_dir / "financial_features.csv"),
                ],
            ),
            (
                "availability",
                [
                    sys.executable,
                    str(PROJECT_ROOT / "industrials" / "transportation" / "scripts" / "08a_build_transportation_specialized_metrics.py"),
                    "--config", str(config_path), "--db", str(db_path),
                    "--tickers", ticker, "--include-historical", "--asof", asof,
                    "--output-csv", str(snapshot_dir / "metric_availability.csv"),
                ],
            ),
        )
        error = ""
        try:
            for stage, command in commands:
                with (
                    (snapshot_dir / f"{stage}.stdout.log").open("w", encoding="utf-8") as stdout,
                    (snapshot_dir / f"{stage}.stderr.log").open("w", encoding="utf-8") as stderr,
                ):
                    subprocess.run(command, cwd=PROJECT_ROOT, env=environment, stdout=stdout, stderr=stderr, check=True)
            status = "PASS"
        except subprocess.CalledProcessError as exc:
            status = "FAIL"
            error = f"{exc.cmd[1]} exit={exc.returncode}"
            failures.append(f"{asof}:{ticker}:{error}")
        result_row = {
            "asof_date": asof,
            "ticker": ticker,
            "status": status,
            "elapsed_seconds": round(time.monotonic() - started, 6),
            "error": error,
        }
        prior_rows[(asof, ticker)] = result_row
        run_rows.append(result_row)
        rows = [prior_rows[key] for key in sorted(prior_rows)]
        write_csv_atomic(output_dir / "transportation_v5_financial_delta.csv", REPORT_FIELDS, rows)
        if failures:
            break
    payload = {
        "acceptance": "PASS" if not failures else "FAIL",
        "contract_version": "transportation_v5_financial_delta_v1",
        "requested_tickers": tickers,
        "pending_pair_count": len(pending),
        "completed_pair_count": sum(row["status"] == "PASS" for row in rows),
        "completed_pair_count_this_run": sum(row["status"] == "PASS" for row in run_rows),
        "resumed_pair_count": sum(row["status"] == "PASS" for row in rows) - sum(row["status"] == "PASS" for row in run_rows),
        "failures": failures,
        "network_requests": 0,
        "parser_invocations": 0,
        "stages_executed": ["financial_features", "metric_availability"],
        "source_artifacts": {
            "prebuild_contract": {"path": str(contract_path), "sha256": file_sha256(contract_path)},
            "prior_validation": {"path": str(validation_path), "sha256": file_sha256(validation_path)},
            "ticker_scoped_materialization": {"path": str(materialization_path), "sha256": file_sha256(materialization_path)},
            "additional_repairs": [
                {"path": str(path), "sha256": file_sha256(path)}
                for path in repair_artifacts[1:]
            ],
        },
    }
    write_text_atomic(output_dir / "transportation_v5_financial_delta.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())

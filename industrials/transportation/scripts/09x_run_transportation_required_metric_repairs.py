#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.required_metric_repair import (  # noqa: E402
    DEPENDENCY_FIELDS,
    PAIR_FIELDS,
    build_repair_contract,
    read_scope,
)
from industrials.transportation.scripts._shared import DEFAULT_CONFIG  # noqa: E402


DEFAULT_SCOPE = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "review_policies"
    / "transportation_required_metric_repair_scope.csv"
)
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "required_metric_repair"
)
STEP_FIELDS = (
    "step_id",
    "status",
    "return_code",
    "duration_seconds",
    "network",
    "command",
    "stdout_log",
    "stderr_log",
)
DELTA_FIELDS = (
    "pair_key",
    "ticker",
    "metric_name",
    "baseline_classification",
    "post_classification",
    "baseline_value",
    "post_value",
    "coverage_changed",
    "post_required_action",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Execute the sealed transportation required-metric sequence once: "
            "one bounded SEC archive parse, one financial/availability rebuild, "
            "one scoring rebuild, and post-repair acceptance gates."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--scope-csv", type=Path, default=DEFAULT_SCOPE)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--skip-archive",
        action="store_true",
        help="Use only already-loaded facts; intended for deterministic tests.",
    )
    return parser.parse_args()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {str(key): str(value or "") for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def _sealed(
    manifest: Mapping[str, Any],
    artifact_name: str,
    path: Path,
) -> bool:
    artifact = (manifest.get("artifacts") or {}).get(artifact_name) or {}
    return (
        str(artifact.get("path") or "") == str(path.resolve())
        and str(artifact.get("sha256") or "") == file_sha256(path)
    )


def build_commands(
    *,
    python_executable: str,
    config_path: Path,
    asof_date: str,
    db_path: Path,
    accession_path: Path,
    tickers: Sequence[str],
    output_dir: Path,
    skip_archive: bool,
) -> list[dict[str, object]]:
    ticker_arg = ",".join(sorted(set(tickers)))
    commands: list[dict[str, object]] = []
    if not skip_archive:
        commands.append(
            {
                "step_id": "bounded_sec_archive_parse",
                "network": True,
                "command": [
                    python_executable,
                    str(
                        PROJECT_ROOT
                        / "industrials"
                        / "scripts"
                        / "07_sync_industrials_sec_fundamentals.py"
                    ),
                    "--config",
                    str(config_path),
                    "--db",
                    str(db_path),
                    "--model-family",
                    "transportation",
                    "--asof",
                    asof_date,
                    "--tickers",
                    ticker_arg,
                    "--archive-selected",
                    "--archive-accession-scope-csv",
                    str(accession_path),
                    "--archive-max-filings-per-ticker",
                    "0",
                    "--archive-max-documents-per-filing",
                    "0",
                    "--archive-scan-all-documents",
                    "--allow-partial",
                    "--output-csv",
                    str(output_dir / "transportation_bounded_sec_sync.csv"),
                ],
            }
        )
    commands.extend(
        [
            {
                "step_id": "bounded_financial_feature_rebuild",
                "network": False,
                "command": [
                    python_executable,
                    str(
                        PROJECT_ROOT
                        / "industrials"
                        / "scripts"
                        / "08_build_industrials_financial_features.py"
                    ),
                    "--config",
                    str(config_path),
                    "--db",
                    str(db_path),
                    "--model-family",
                    "transportation",
                    "--asof",
                    asof_date,
                    "--tickers",
                    ticker_arg,
                    "--output-csv",
                    str(
                        output_dir
                        / "transportation_repaired_financial_features.csv"
                    ),
                    "--availability-output-csv",
                    str(
                        output_dir
                        / "transportation_repaired_financial_availability.csv"
                    ),
                ],
            },
            {
                "step_id": "single_metric_availability_rebuild",
                "network": False,
                "command": [
                    python_executable,
                    str(
                        PROJECT_ROOT
                        / "industrials"
                        / "transportation"
                        / "scripts"
                        / "08a_build_transportation_specialized_metrics.py"
                    ),
                    "--config",
                    str(config_path),
                    "--db",
                    str(db_path),
                    "--asof",
                    asof_date,
                    "--output-csv",
                    str(
                        output_dir
                        / "transportation_repaired_metric_availability.csv"
                    ),
                ],
            },
            {
                "step_id": "financial_acceptance_validation",
                "network": False,
                "command": [
                    python_executable,
                    str(
                        PROJECT_ROOT
                        / "industrials"
                        / "transportation"
                        / "scripts"
                        / "08_validate_transportation_financial_stage.py"
                    ),
                    "--config",
                    str(config_path),
                    "--db",
                    str(db_path),
                    "--asof",
                    asof_date,
                ],
            },
            {
                "step_id": "metric_acceptance_validation",
                "network": False,
                "command": [
                    python_executable,
                    str(
                        PROJECT_ROOT
                        / "industrials"
                        / "transportation"
                        / "scripts"
                        / "08a_validate_transportation_specialized_metrics.py"
                    ),
                    "--config",
                    str(config_path),
                    "--db",
                    str(db_path),
                    "--asof",
                    asof_date,
                ],
            },
            {
                "step_id": "single_scoring_rebuild",
                "network": False,
                "command": [
                    python_executable,
                    str(
                        PROJECT_ROOT
                        / "industrials"
                        / "transportation"
                        / "scripts"
                        / "06a_build_transportation_scoring_features.py"
                    ),
                    "--config",
                    str(config_path),
                    "--db",
                    str(db_path),
                    "--asof",
                    asof_date,
                    "--force",
                    "--output-csv",
                    str(
                        output_dir
                        / "transportation_repaired_scoring_features.csv"
                    ),
                ],
            },
            {
                "step_id": "scoring_acceptance_validation",
                "network": False,
                "command": [
                    python_executable,
                    str(
                        PROJECT_ROOT
                        / "industrials"
                        / "transportation"
                        / "scripts"
                        / "06a_validate_transportation_scoring_features.py"
                    ),
                    "--config",
                    str(config_path),
                    "--db",
                    str(db_path),
                    "--asof",
                    asof_date,
                    "--input-csv",
                    str(
                        output_dir
                        / "transportation_repaired_scoring_features.csv"
                    ),
                    "--output-json",
                    str(
                        output_dir
                        / "transportation_repaired_scoring_validation.json"
                    ),
                ],
            },
        ]
    )
    return commands


def _run_steps(
    commands: Sequence[Mapping[str, object]],
    *,
    output_dir: Path,
) -> tuple[list[dict[str, object]], list[str]]:
    logs_dir = output_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, object]] = []
    errors: list[str] = []
    for step in commands:
        step_id = str(step["step_id"])
        raw_command = step["command"]
        if not isinstance(raw_command, Sequence) or isinstance(
            raw_command, (str, bytes)
        ):
            raise ValueError(f"{step_id}: command must be a sequence")
        command = [str(value) for value in raw_command]
        stdout_path = logs_dir / f"{step_id}.stdout.log"
        stderr_path = logs_dir / f"{step_id}.stderr.log"
        started = time.monotonic()
        with stdout_path.open("w", encoding="utf-8") as stdout_handle:
            with stderr_path.open("w", encoding="utf-8") as stderr_handle:
                process = subprocess.run(
                    command,
                    cwd=PROJECT_ROOT,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    text=True,
                    check=False,
                )
        duration = time.monotonic() - started
        status = "PASS" if process.returncode == 0 else "FAIL"
        records.append(
            {
                "step_id": step_id,
                "status": status,
                "return_code": process.returncode,
                "duration_seconds": f"{duration:.3f}",
                "network": int(bool(step["network"])),
                "command": json.dumps(command),
                "stdout_log": str(stdout_path.resolve()),
                "stderr_log": str(stderr_path.resolve()),
            }
        )
        print(
            f"transportation_required_metric_repair step={step_id} "
            f"status={status} seconds={duration:.1f}",
            flush=True,
        )
        if status == "FAIL":
            errors.append(
                f"{step_id}: return_code={process.returncode}; "
                f"stderr={stderr_path}"
            )
            break
    return records, errors


def _post_contract(
    *,
    db_path: Path,
    scope_path: Path,
    asof_date: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    scope_rows = read_scope(scope_path)
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return build_repair_contract(
            connection,
            scope_rows=scope_rows,
            asof_date=asof_date,
        )
    finally:
        connection.close()


def _sync_gate(
    *,
    output_dir: Path,
    expected_tickers: Sequence[str],
    skip_archive: bool,
) -> tuple[dict[str, int], list[str], list[str]]:
    if skip_archive:
        return {}, [], []
    sync_path = output_dir / "transportation_bounded_sec_sync.csv"
    if not sync_path.is_file():
        return {}, [], [f"SEC sync report missing: {sync_path}"]
    rows = _read_csv(sync_path)
    expected = {ticker.upper() for ticker in expected_tickers}
    scoped = [
        row for row in rows if str(row.get("ticker") or "").upper() in expected
    ]
    actual = {str(row.get("ticker") or "").upper() for row in scoped}
    status_counts = Counter(
        str(row.get("status") or "missing").lower() for row in scoped
    )
    failed = sorted(
        str(row.get("ticker") or "").upper()
        for row in scoped
        if str(row.get("status") or "").lower() == "failed"
    )
    errors: list[str] = []
    missing = sorted(expected - actual)
    if missing:
        errors.append(f"SEC sync report missing expected tickers={missing}")
    if failed:
        errors.append(f"SEC sync ticker failures={failed}")
    return dict(sorted(status_counts.items())), failed, errors


def _delta_rows(
    baseline: Sequence[Mapping[str, object]],
    post: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    baseline_by_pair = {str(row["pair_key"]): row for row in baseline}
    output: list[dict[str, object]] = []
    for row in post:
        pair_key = str(row["pair_key"])
        before = baseline_by_pair[pair_key]
        baseline_classification = str(before["repair_classification"])
        post_classification = str(row["repair_classification"])
        output.append(
            {
                "pair_key": pair_key,
                "ticker": row["ticker"],
                "metric_name": row["metric_name"],
                "baseline_classification": baseline_classification,
                "post_classification": post_classification,
                "baseline_value": before["current_metric_value"],
                "post_value": row["current_metric_value"],
                "coverage_changed": int(
                    baseline_classification != "ALREADY_RESOLVED"
                    and post_classification == "ALREADY_RESOLVED"
                ),
                "post_required_action": row["required_action"],
            }
        )
    return output


def main() -> int:
    args = parse_args()
    asof_date = str(args.asof)[:10]
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = (
        args.db.expanduser().resolve()
        if args.db is not None
        else resolve_path(
            cfg_get(config, "paths.database_path"),
            base_dir=config_path.parent,
        )
    )
    scope_path = args.scope_csv.expanduser().resolve()
    output_dir = args.output_root.expanduser().resolve() / asof_date
    plan_path = output_dir / "transportation_required_metric_repair_plan.json"
    pair_path = output_dir / "transportation_required_metric_repair_pairs.csv"
    dependency_path = (
        output_dir / "transportation_required_metric_repair_dependencies.csv"
    )
    accession_path = (
        output_dir / "transportation_required_metric_repair_accessions.csv"
    )
    required = (plan_path, pair_path, dependency_path, accession_path)
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            f"Run 09w planner first; missing sealed inputs={missing}"
        )
    plan = _read_json(plan_path)
    errors: list[str] = []
    if plan.get("acceptance") != "PASS":
        errors.append("required-metric repair plan is not passing")
    for name, path in (
        ("pair_contract", pair_path),
        ("dependency_contract", dependency_path),
        ("accession_manifest", accession_path),
    ):
        if not _sealed(plan, name, path):
            errors.append(f"{name} is not sealed by the plan manifest")
    baseline_pairs = _read_csv(pair_path)
    financial_tickers = sorted(
        {
            row["ticker"].upper()
            for row in baseline_pairs
            if row["source_type"] == "financial"
            and row["repair_classification"] != "ALREADY_RESOLVED"
        }
    )
    if len(baseline_pairs) != 32 or len(financial_tickers) != 18:
        errors.append(
            "sealed scope changed: expected 32 pairs and 18 financial tickers"
        )
    commands = build_commands(
        python_executable=sys.executable,
        config_path=config_path,
        asof_date=asof_date,
        db_path=db_path,
        accession_path=accession_path,
        tickers=financial_tickers,
        output_dir=output_dir,
        skip_archive=bool(args.skip_archive),
    )
    if args.dry_run:
        payload = {
            "acceptance": "DRY_RUN" if not errors else "FAIL",
            "asof_date": asof_date,
            "pair_count": len(baseline_pairs),
            "financial_tickers": financial_tickers,
            "commands": commands,
            "errors": errors,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if not errors else 2
    if errors:
        print(json.dumps({"acceptance": "FAIL", "errors": errors}, indent=2))
        return 2

    started_at = datetime.now(timezone.utc)
    step_rows, step_errors = _run_steps(commands, output_dir=output_dir)
    errors.extend(step_errors)
    sync_status_counts, sync_failed_tickers, sync_errors = _sync_gate(
        output_dir=output_dir,
        expected_tickers=financial_tickers,
        skip_archive=bool(args.skip_archive),
    )
    errors.extend(sync_errors)
    steps_path = output_dir / "transportation_required_metric_repair_steps.csv"
    write_csv_atomic(steps_path, STEP_FIELDS, step_rows)

    post_pairs: list[dict[str, object]] = []
    post_dependencies: list[dict[str, object]] = []
    delta: list[dict[str, object]] = []
    if not step_errors:
        post_pairs, post_dependencies = _post_contract(
            db_path=db_path,
            scope_path=scope_path,
            asof_date=asof_date,
        )
        delta = _delta_rows(baseline_pairs, post_pairs)
    post_pair_path = (
        output_dir / "transportation_required_metric_repair_post_pairs.csv"
    )
    post_dependency_path = (
        output_dir
        / "transportation_required_metric_repair_post_dependencies.csv"
    )
    delta_path = (
        output_dir / "transportation_required_metric_repair_coverage_delta.csv"
    )
    write_csv_atomic(post_pair_path, PAIR_FIELDS, post_pairs)
    write_csv_atomic(
        post_dependency_path, DEPENDENCY_FIELDS, post_dependencies
    )
    write_csv_atomic(delta_path, DELTA_FIELDS, delta)

    resolved_count = sum(int(str(row["coverage_changed"])) for row in delta)
    unresolved = [
        row
        for row in post_pairs
        if str(row["repair_classification"]) != "ALREADY_RESOLVED"
    ]
    unresolved_counts = Counter(
        str(row["repair_classification"]) for row in unresolved
    )
    rubi = next(
        (
            row
            for row in post_pairs
            if str(row["pair_key"]) == "RUBI|maximum_drawdown_12m"
        ),
        {},
    )
    if rubi and str(rubi["repair_classification"]) not in {
        "INSUFFICIENT_MARKET_HISTORY",
        "ALREADY_RESOLVED",
    }:
        errors.append("RUBI market-history classification violated")
    acceptance = (
        "FAIL"
        if errors
        else (
            "PASS_WITH_EXPLICIT_LIMITATIONS"
            if unresolved
            else "PASS"
        )
    )
    manifest_path = (
        output_dir / "transportation_required_metric_repair_execution.json"
    )
    payload = {
        "acceptance": acceptance,
        "gate": "TRANSPORTATION_REQUIRED_METRIC_REPAIR_EXECUTION",
        "asof_date": asof_date,
        "started_at_utc": started_at.isoformat(),
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "sealed_pair_count": len(baseline_pairs),
        "financial_ticker_count": len(financial_tickers),
        "accession_count": len(_read_csv(accession_path)),
        "completed_step_ids": [
            row["step_id"] for row in step_rows if row["status"] == "PASS"
        ],
        "failed_step_ids": [
            row["step_id"] for row in step_rows if row["status"] == "FAIL"
        ],
        "archive_parse_invocations": int(not args.skip_archive),
        "financial_feature_build_invocations": int(
            any(
                row["step_id"] == "bounded_financial_feature_rebuild"
                and row["status"] == "PASS"
                for row in step_rows
            )
        ),
        "metric_availability_build_invocations": int(
            any(
                row["step_id"] == "single_metric_availability_rebuild"
                and row["status"] == "PASS"
                for row in step_rows
            )
        ),
        "scoring_build_invocations": int(
            any(
                row["step_id"] == "single_scoring_rebuild"
                and row["status"] == "PASS"
                for row in step_rows
            )
        ),
        "sec_sync_status_counts": sync_status_counts,
        "sec_sync_failed_tickers": sync_failed_tickers,
        "coverage_improved_pair_count": resolved_count,
        "post_resolved_pair_count": sum(
            str(row["repair_classification"]) == "ALREADY_RESOLVED"
            for row in post_pairs
        ),
        "post_unresolved_pair_count": len(unresolved),
        "post_unresolved_classification_counts": dict(
            sorted(unresolved_counts.items())
        ),
        "rubi_status": rubi,
        "production_promotion_authorized": False,
        "portfolio_layer_invocations": 0,
        "calibration_invocations": 0,
        "artifacts": {
            "steps": {
                "path": str(steps_path.resolve()),
                "sha256": file_sha256(steps_path),
            },
            "post_pair_contract": {
                "path": str(post_pair_path.resolve()),
                "sha256": file_sha256(post_pair_path),
                "row_count": len(post_pairs),
            },
            "post_dependency_contract": {
                "path": str(post_dependency_path.resolve()),
                "sha256": file_sha256(post_dependency_path),
                "row_count": len(post_dependencies),
            },
            "coverage_delta": {
                "path": str(delta_path.resolve()),
                "sha256": file_sha256(delta_path),
                "row_count": len(delta),
            },
        },
        "errors": errors,
        "next_gate": (
            "RUN_RESIDUAL_DEDICATED_PARSER_ONLY_FOR_UNRESOLVED_PAIRS"
            if not errors and unresolved
            else (
                "REVIEW_REQUIRED_METRIC_REPAIR_ERRORS"
                if errors
                else "AUDIT_FINAL_TRANSPORTATION_RELEASE"
            )
        ),
    }
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if acceptance != "FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())

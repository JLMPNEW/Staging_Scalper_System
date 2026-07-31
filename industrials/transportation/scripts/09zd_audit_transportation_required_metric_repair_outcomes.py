#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from dedicated_parser.storage import connect_database  # noqa: E402
from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.reviewed_operand_repair import (  # noqa: E402
    load_policy,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    resolve_foundation,
)


DEFAULT_SCOPE = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "review_policies"
    / "transportation_required_metric_repair_scope.csv"
)
DEFAULT_POLICY = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "review_policies"
    / "transportation_required_metric_operand_repairs.json"
)
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "required_metric_repair"
)
OBSERVED_STATUSES = frozenset({"REPORTED", "DERIVED", "PROXY"})
FIELDS = (
    "scope_version",
    "ticker",
    "metric_name",
    "availability_status",
    "metric_value",
    "extraction_method",
    "period_start",
    "period_end",
    "status_reason",
    "outcome",
    "documented_deferred",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only acceptance audit for the 32-pair transportation "
            "required-metric repair scope."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--scope", type=Path, default=DEFAULT_SCOPE)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return parser.parse_args()


def _read_scope(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    identities = [
        (str(row["ticker"]).upper(), str(row["metric_name"]))
        for row in rows
    ]
    if len(identities) != len(set(identities)):
        raise ValueError(f"{path}: duplicate ticker/metric scope")
    if len(rows) != 32:
        raise ValueError(f"{path}: expected 32 repair pairs, found={len(rows)}")
    return rows


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {field: row.get(field, "") for field in FIELDS}
            )
    temporary.replace(path)


def main() -> int:
    args = parse_args()
    asof = str(args.asof)[:10]
    scope_path = args.scope.expanduser().resolve()
    policy_path = args.policy.expanduser().resolve()
    scope_rows = _read_scope(scope_path)
    policy = load_policy(policy_path)
    if str(policy.get("asof_date") or "")[:10] != asof:
        raise ValueError("reviewed policy and audit as-of dates differ")
    documented_deferred = {
        (str(row["ticker"]).upper(), str(metric))
        for row in policy.get("deferred_scope") or []
        for metric in row.get("metrics") or []
    }
    foundation = resolve_foundation(
        args.config.expanduser().resolve(),
        args.db,
    )
    identities = [
        (str(row["ticker"]).upper(), str(row["metric_name"]))
        for row in scope_rows
    ]
    with connect_database(
        foundation.db_path,
        timeout_seconds=foundation.timeout_sec,
        readonly=True,
    ) as connection:
        connection.row_factory = sqlite3.Row
        availability = {
            (str(row["ticker"]), str(row["metric_name"])): dict(row)
            for row in connection.execute(
                """
                SELECT ticker, metric_name, availability_status, metric_value,
                       extraction_method, period_start, period_end,
                       status_reason
                FROM feature_financial_metric_availability
                WHERE model_family='transportation' AND asof_date=?
                """,
                (asof,),
            ).fetchall()
            if (str(row["ticker"]), str(row["metric_name"])) in identities
        }
    output_rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for scope in scope_rows:
        ticker = str(scope["ticker"]).upper()
        metric_name = str(scope["metric_name"])
        identity = (ticker, metric_name)
        row = availability.get(identity)
        deferred = identity in documented_deferred
        if row is None:
            outcome = "MISSING_AVAILABILITY_ROW"
            errors.append(f"{ticker}:{metric_name}:missing availability row")
            row = {}
        else:
            status = str(row["availability_status"])
            if status in OBSERVED_STATUSES:
                outcome = (
                    "RESOLVED_AFTER_POLICY"
                    if deferred
                    else "RESOLVED_VALUE"
                )
            elif status == "NOT_APPLICABLE":
                outcome = (
                    "RESOLVED_AFTER_POLICY"
                    if deferred
                    else "RESOLVED_NOT_APPLICABLE"
                )
            elif deferred:
                outcome = "DEFERRED_DOCUMENTED"
            else:
                outcome = "UNEXPECTED_UNRESOLVED"
                errors.append(
                    f"{ticker}:{metric_name}:unexpected status={status}"
                )
        output_rows.append(
            {
                "scope_version": scope["scope_version"],
                "ticker": ticker,
                "metric_name": metric_name,
                "availability_status": row.get("availability_status", ""),
                "metric_value": row.get("metric_value", ""),
                "extraction_method": row.get("extraction_method", ""),
                "period_start": row.get("period_start", ""),
                "period_end": row.get("period_end", ""),
                "status_reason": row.get("status_reason", ""),
                "outcome": outcome,
                "documented_deferred": int(deferred),
            }
        )
    output_dir = args.output_root.expanduser().resolve() / asof
    output_csv = (
        output_dir
        / "transportation_required_metric_repair_outcomes.csv"
    )
    _write_csv(output_csv, output_rows)
    outcome_counts = Counter(str(row["outcome"]) for row in output_rows)
    status_counts = Counter(
        str(row["availability_status"]) for row in output_rows
    )
    observed_count = sum(
        status_counts.get(status, 0) for status in OBSERVED_STATUSES
    )
    not_applicable_count = status_counts.get("NOT_APPLICABLE", 0)
    resolved_count = observed_count + not_applicable_count
    manifest = {
        "acceptance": "PASS" if not errors else "FAIL",
        "gate": "TRANSPORTATION_REQUIRED_METRIC_REPAIR_OUTCOMES",
        "asof_date": asof,
        "scope_pair_count": len(output_rows),
        "resolved_pair_count": resolved_count,
        "observed_value_pair_count": observed_count,
        "not_applicable_pair_count": not_applicable_count,
        "documented_deferred_pair_count": outcome_counts.get(
            "DEFERRED_DOCUMENTED",
            0,
        ),
        "unexpected_unresolved_pair_count": outcome_counts.get(
            "UNEXPECTED_UNRESOLVED",
            0,
        ),
        "outcome_counts": dict(sorted(outcome_counts.items())),
        "availability_status_counts": dict(sorted(status_counts.items())),
        "scope": {
            "path": str(scope_path),
            "sha256": file_sha256(scope_path),
        },
        "policy": {
            "path": str(policy_path),
            "sha256": file_sha256(policy_path),
        },
        "output_csv": {
            "path": str(output_csv),
            "sha256": file_sha256(output_csv),
        },
        "parser_invocations": 0,
        "network_requests": 0,
        "errors": errors,
        "next_gate": (
            "SCORING_AND_ELIGIBILITY_REBUILD"
            if not errors
            else "REPAIR_UNEXPECTED_REQUIRED_METRIC_RESIDUALS"
        ),
    }
    output_json = (
        output_dir
        / "transportation_required_metric_repair_outcomes.json"
    )
    write_text_atomic(
        output_json,
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())

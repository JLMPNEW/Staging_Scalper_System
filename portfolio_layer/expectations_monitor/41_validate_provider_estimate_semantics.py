#!/usr/bin/env python3
"""Validate normalized estimate semantics without combining providers."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.expectations_monitor.estimate_policy import canonicalize_snapshot  # noqa: E402
from portfolio_layer.expectations_monitor.monitor_common import (  # noqa: E402
    artifact_snapshot_dependency_errors,
    connect_monitor_db,
    record_snapshot_dependencies,
    supersede_artifact_dependencies,
    writer_lock,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
OUTPUT_FIELDS = [
    "snapshot_id",
    "provider",
    "ticker",
    "metric",
    "canonical_period",
    "fiscal_period_end",
    "estimate_average",
    "estimate_low",
    "estimate_high",
    "analyst_count",
    "currency",
    "scope_status",
    "quality_status",
    "quality_reasons",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--retrieval-cycle")
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def evaluate(
    rows: list[Any],
    *,
    as_of: date,
    active_period_grace_days: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if active_period_grace_days < 0:
        raise ValueError("active_period_grace_days must be non-negative")
    active_cutoff = as_of - timedelta(days=active_period_grace_days)
    output: list[dict[str, Any]] = []
    identities: set[tuple[str, str, str, str, str]] = set()
    duplicate_count = 0
    for raw in rows:
        row = canonicalize_snapshot(raw)
        scope_status = (
            "ACTIVE_FORECAST"
            if date.fromisoformat(row.fiscal_period_end) >= active_cutoff
            else "HISTORICAL_REFERENCE"
        )
        identity = (row.provider, *row.key)
        if identity in identities:
            duplicate_count += 1
        identities.add(identity)
        output.append(
            {
                "snapshot_id": row.snapshot_id,
                "provider": row.provider,
                "ticker": row.ticker,
                "metric": row.metric,
                "canonical_period": row.canonical_period,
                "fiscal_period_end": row.fiscal_period_end,
                "estimate_average": row.estimate_average,
                "estimate_low": row.estimate_low,
                "estimate_high": row.estimate_high,
                "analyst_count": row.analyst_count,
                "currency": row.currency,
                "scope_status": scope_status,
                "quality_status": row.quality_status,
                "quality_reasons": ",".join(row.quality_reasons),
            }
        )
    counts = {
        status: sum(row["quality_status"] == status for row in output)
        for status in ("PASS", "WARN", "FAIL")
    }
    active_failures = sum(
        row["scope_status"] == "ACTIVE_FORECAST" and row["quality_status"] == "FAIL"
        for row in output
    )
    summary = {
        "row_count": len(output),
        "quality_counts": counts,
        "active_cutoff": active_cutoff.isoformat(),
        "active_forecast_count": sum(
            row["scope_status"] == "ACTIVE_FORECAST" for row in output
        ),
        "active_failure_count": active_failures,
        "historical_reference_count": sum(
            row["scope_status"] == "HISTORICAL_REFERENCE" for row in output
        ),
        "duplicate_canonical_identity_count": duplicate_count,
        "acceptance": (
            "PASS"
            if active_failures == 0 and duplicate_count == 0 and output
            else "FAIL"
        ),
    }
    return output, summary


def _source_digest(rows: list[Any]) -> str:
    payload = [
        (str(row["snapshot_id"]), str(row["normalized_sha256"]))
        for row in sorted(rows, key=lambda item: str(item["snapshot_id"]))
    ]
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def run_selftest() -> None:
    good = {
        "snapshot_id": "a" * 64,
        "provider": "alpha_vantage",
        "ticker": "AAA",
        "estimate_type": "eps_fiscal_year",
        "fiscal_period": "fiscal_year",
        "fiscal_period_end": "2026-12-31",
        "estimate_average": 2.0,
        "estimate_low": 1.5,
        "estimate_high": 2.5,
        "analyst_count": 4,
        "currency": "USD",
        "fetched_at_utc": "2026-07-31T22:00:00+00:00",
        "retrieval_cycle": "cycle",
    }
    output, summary = evaluate(
        [good],
        as_of=date(2026, 7, 31),
        active_period_grace_days=90,
    )
    assert summary["acceptance"] == "PASS"
    assert output[0]["canonical_period"] == "annual"
    bad = dict(good, snapshot_id="b" * 64, estimate_average=3.0)
    _, bad_summary = evaluate(
        [bad],
        as_of=date(2026, 7, 31),
        active_period_grace_days=90,
    )
    assert bad_summary["acceptance"] == "FAIL"
    print("provider estimate semantic validation selftest: PASS")


def main() -> int:
    args = parse_args()
    if args.selftest:
        run_selftest()
        return 0
    if not args.retrieval_cycle or args.as_of is None:
        raise ValueError("--retrieval-cycle and --as-of are required")

    config_path = args.config.resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    monitor_cfg = cfg_get(config, "expectations_monitor", {})
    if not isinstance(monitor_cfg, dict):
        raise ValueError("expectations_monitor config must be a mapping")
    db_path = ensure_not_prod_path(
        args.db.resolve()
        if args.db
        else resolve_path(
            monitor_cfg.get("database_path", "db/expectations_monitor.sqlite"),
            base_dir=config_path.parent,
        ),
        label="expectations monitor database",
    )
    conn = connect_monitor_db(
        db_path,
        timeout_sec=float(monitor_cfg.get("writer_lock_timeout_sec", 30.0)),
    )
    try:
        runs = conn.execute(
            "SELECT provider, status, completed_at_utc FROM provider_snapshot_runs "
            "WHERE retrieval_cycle=? ORDER BY provider",
            (args.retrieval_cycle,),
        ).fetchall()
        run_status = {str(row["provider"]): str(row["status"]) for row in runs}
        if run_status != {"alpha_vantage": "PASS", "fmp": "PASS"}:
            raise ValueError(f"Retrieval cycle does not contain two PASS provider runs: {run_status}")
        rows = conn.execute(
            "SELECT * FROM provider_estimate_snapshots WHERE retrieval_cycle=? "
            "ORDER BY provider,ticker,fiscal_period_end,estimate_type",
            (args.retrieval_cycle,),
        ).fetchall()
    finally:
        conn.close()
    completed_at = max(str(row["completed_at_utc"]) for row in runs)
    policy = monitor_cfg.get("provider_reconciliation", {})
    if not isinstance(policy, dict):
        raise ValueError("expectations_monitor.provider_reconciliation must be a mapping")
    active_period_grace_days = int(policy.get("active_period_grace_days", 90))
    output, summary = evaluate(
        list(rows),
        as_of=args.as_of,
        active_period_grace_days=active_period_grace_days,
    )
    summary.update(
        {
            "schema_version": "provider_estimate_semantic_validation_v1",
            "as_of_date": args.as_of.isoformat(),
            "retrieval_cycle": args.retrieval_cycle,
            "generated_at_utc": completed_at,
            "provider_run_status": run_status,
            "source_snapshot_count": len(rows),
            "source_snapshot_digest": _source_digest(list(rows)),
        }
    )
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else paths.output_dir / "provider_reconciliation" / args.retrieval_cycle
    )
    csv_path = output_dir / "provider_estimate_semantic_validation.csv"
    manifest_path = output_dir / "provider_estimate_semantic_validation_manifest.json"
    fail_if_exists([csv_path, manifest_path], force=args.force)
    write_csv(csv_path, OUTPUT_FIELDS, output)
    source_path = Path(__file__).resolve()
    policy_path = Path(__file__).with_name("estimate_policy.py")
    write_manifest(
        manifest_path,
        {
            **summary,
            "inputs_sha256": {
                str(config_path): sha256_file(config_path),
                str(source_path): sha256_file(source_path),
                str(policy_path): sha256_file(policy_path),
                str(Path(__file__).with_name("monitor_common.py")): sha256_file(
                    Path(__file__).with_name("monitor_common.py")
                ),
            },
            "outputs_sha256": {csv_path.name: sha256_file(csv_path)},
        },
    )
    snapshot_ids = [str(row["snapshot_id"]) for row in rows]
    lock_path = db_path.with_suffix(db_path.suffix + ".writer.lock")
    timeout = float(monitor_cfg.get("writer_lock_timeout_sec", 30.0))
    with writer_lock(lock_path, timeout_sec=timeout):
        conn = connect_monitor_db(db_path, timeout_sec=timeout)
        try:
            for path in (csv_path, manifest_path):
                artifact_sha256 = sha256_file(path)
                supersede_artifact_dependencies(
                    conn,
                    artifact_path=str(path),
                    current_artifact_sha256=artifact_sha256,
                )
                record_snapshot_dependencies(
                    conn,
                    artifact_path=str(path),
                    artifact_sha256=artifact_sha256,
                    snapshot_ids=snapshot_ids,
                )
                errors = artifact_snapshot_dependency_errors(
                    conn,
                    artifact_path=str(path),
                    artifact_sha256=artifact_sha256,
                )
                valid_count = conn.execute(
                    "SELECT COUNT(*) FROM provider_snapshot_dependencies "
                    "WHERE artifact_path=? AND artifact_sha256=? AND status='valid'",
                    (str(path), artifact_sha256),
                ).fetchone()[0]
                stale_valid_count = conn.execute(
                    "SELECT COUNT(*) FROM provider_snapshot_dependencies "
                    "WHERE artifact_path=? AND artifact_sha256<>? AND status='valid'",
                    (str(path), artifact_sha256),
                ).fetchone()[0]
                if errors or int(valid_count) != len(set(snapshot_ids)) or stale_valid_count:
                    raise RuntimeError(
                        f"Invalid semantic-artifact snapshot lineage for {path}: "
                        f"errors={errors}; valid={valid_count}; "
                        f"expected={len(set(snapshot_ids))}; stale_valid={stale_valid_count}"
                    )
        finally:
            conn.close()
    print(f"PROVIDER ESTIMATE SEMANTICS: {summary['acceptance']}")
    print(
        f"active={summary['active_forecast_count']}; "
        f"active_failures={summary['active_failure_count']}; "
        f"historical={summary['historical_reference_count']}; "
        f"all_quality={summary['quality_counts']}; "
        f"duplicates={summary['duplicate_canonical_identity_count']}"
    )
    return 0 if summary["acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

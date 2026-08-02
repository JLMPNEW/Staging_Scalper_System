#!/usr/bin/env python3
"""Dry-run or execute a provider/date snapshot purge with dependency invalidation."""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import date
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import sha256_file, write_manifest  # noqa: E402
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.expectations_monitor.monitor_common import (  # noqa: E402
    append_estimate_snapshots,
    connect_monitor_db,
    execute_provider_purge,
    plan_provider_purge,
    record_snapshot_dependencies,
    writer_lock,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--provider", choices=("alpha_vantage", "fmp"))
    parser.add_argument("--from-date", type=date.fromisoformat)
    parser.add_argument("--to-date", type=date.fromisoformat)
    parser.add_argument("--reason", default="")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def run_selftest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "monitor.sqlite"
        conn = connect_monitor_db(db_path, timeout_sec=1.0)
        try:
            row = {
                "provider": "fmp",
                "endpoint_id": "analyst_estimates",
                "ticker": "AAA",
                "fiscal_period_end": "2026-12-31",
                "fiscal_period": "annual",
                "estimate_type": "eps",
                "estimate_average": 2.0,
                "fetched_at_utc": "2026-07-31T22:00:00+00:00",
                "available_at_utc": "2026-07-31T22:00:00+00:00",
                "retrieval_cycle": "2026-07-31-eod",
                "response_sha256": "a" * 64,
                "entitlement_version": "provider_entitlements_v1",
                "retention_class": "provisional_user_authorized",
                "coverage_status": "available",
            }
            assert append_estimate_snapshots(conn, [row]) == (1, 0)
            snapshot_id = conn.execute("SELECT snapshot_id FROM provider_estimate_snapshots").fetchone()["snapshot_id"]
            record_snapshot_dependencies(
                conn,
                artifact_path="out/state.csv",
                artifact_sha256="b" * 64,
                snapshot_ids=[snapshot_id],
            )
            dry = plan_provider_purge(conn, provider="fmp", from_date="2026-07-31", to_date="2026-07-31")
            assert dry["snapshot_count"] == 1
            assert conn.execute("SELECT COUNT(*) FROM provider_estimate_snapshots").fetchone()[0] == 1
            done = execute_provider_purge(
                conn,
                provider="fmp",
                from_date="2026-07-31",
                to_date="2026-07-31",
                reason="selftest",
            )
            assert done["snapshot_count"] == 1
            assert done["invalidated_dependency_count"] == 1
        finally:
            conn.close()
    print("provider snapshot purge selftest: PASS")


def main() -> int:
    args = parse_args()
    if args.selftest:
        run_selftest()
        return 0
    if args.provider is None or args.from_date is None or args.to_date is None:
        raise ValueError("--provider, --from-date, and --to-date are required")
    if args.execute and not args.reason.strip():
        raise ValueError("--reason is required with --execute")

    config_path = args.config.resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    monitor_cfg = cfg_get(config, "expectations_monitor", {})
    if not isinstance(monitor_cfg, dict):
        raise ValueError("expectations_monitor config must be a mapping")
    retention_cfg = monitor_cfg.get("retention", {})
    if not isinstance(retention_cfg, dict):
        raise ValueError("expectations_monitor.retention must be a mapping")
    if args.execute and bool(retention_cfg.get("purge_requires_reason", True)):
        if not args.reason.strip():
            raise ValueError("The configured retention policy requires a purge reason")

    db_path = ensure_not_prod_path(
        args.db.resolve()
        if args.db
        else resolve_path(
            monitor_cfg.get("database_path", "db/expectations_monitor.sqlite"),
            base_dir=config_path.parent,
        ),
        label="expectations monitor database",
    )
    if not db_path.exists():
        raise FileNotFoundError(f"Expectations monitor database not found: {db_path}")
    timeout_sec = float(monitor_cfg.get("writer_lock_timeout_sec", 30.0))
    from_date = args.from_date.isoformat()
    to_date = args.to_date.isoformat()

    if args.execute:
        lock_path = db_path.with_suffix(db_path.suffix + ".writer.lock")
        with writer_lock(lock_path, timeout_sec=timeout_sec):
            conn = connect_monitor_db(db_path, timeout_sec=timeout_sec)
            try:
                preflight = plan_provider_purge(
                    conn,
                    provider=args.provider,
                    from_date=from_date,
                    to_date=to_date,
                )
                if preflight["snapshot_count"] == 0:
                    raise ValueError("Refusing to execute an empty provider purge")
                result = execute_provider_purge(
                    conn,
                    provider=args.provider,
                    from_date=from_date,
                    to_date=to_date,
                    reason=args.reason,
                )
            finally:
                conn.close()
        mode = "executed"
    else:
        conn = connect_monitor_db(db_path, timeout_sec=timeout_sec)
        try:
            result = plan_provider_purge(
                conn,
                provider=args.provider,
                from_date=from_date,
                to_date=to_date,
            )
        finally:
            conn.close()
        mode = "dry_run"

    output_root = (
        args.output_dir.resolve()
        if args.output_dir
        else paths.output_dir / "provider_retention"
    )
    report_dir = output_root / f"{args.provider}-{from_date}-{to_date}-{mode}"
    report_path = report_dir / "purge_report.json"
    report = {
        "schema_version": "provider_purge_report_v1",
        "acceptance": "PASS",
        "mode": mode,
        "reason": args.reason.strip(),
        "database_path": str(db_path),
        "config_sha256": sha256_file(config_path),
        "source_sha256": sha256_file(Path(__file__).resolve()),
        **result,
    }
    write_manifest(report_path, report)
    print(f"PROVIDER SNAPSHOT PURGE: {mode.upper()}")
    print(f"provider={args.provider}; snapshots={result['snapshot_count']}; dependencies={result['dependency_count']}")
    print(f"report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

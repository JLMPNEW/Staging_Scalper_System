#!/usr/bin/env python3
"""Migrate legacy normalized estimate snapshots without inventing provider vintages."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.provider_ingestion.health import validate_provider_ingestion_policy  # noqa: E402
from portfolio_layer.provider_ingestion.store import (  # noqa: E402
    connect_store,
    freeze_universe,
    persist_capture,
    verify_store,
    writer_lock,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SUMMARY_FIELDS = (
    "retrieval_cycle",
    "legacy_rows",
    "request_count",
    "available_start_utc",
    "available_end_utc",
    "legacy_asof_mismatch",
    "status",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--legacy-db", type=Path)
    parser.add_argument("--store-db", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def _normalized_row(row: sqlite3.Row) -> dict[str, Any]:
    fields = (
        "provider",
        "endpoint_id",
        "ticker",
        "fiscal_period_end",
        "fiscal_period",
        "estimate_type",
        "estimate_average",
        "estimate_high",
        "estimate_low",
        "analyst_count",
        "estimate_average_7_days_ago",
        "estimate_average_30_days_ago",
        "estimate_average_60_days_ago",
        "estimate_average_90_days_ago",
        "revision_up_7_days",
        "revision_down_7_days",
        "revision_up_30_days",
        "revision_down_30_days",
        "currency",
    )
    return {field: row[field] for field in fields}


def _cycle_requests(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], list[sqlite3.Row]] = {}
    for row in rows:
        key = (
            str(row["provider"]),
            str(row["endpoint_id"]),
            str(row["ticker"]),
            str(row["available_at_utc"]),
        )
        grouped.setdefault(key, []).append(row)
    output: list[dict[str, Any]] = []
    for (provider, endpoint, ticker, available), group in sorted(grouped.items()):
        response_hashes = {str(row["response_sha256"]) for row in group}
        if len(response_hashes) != 1:
            raise ValueError(
                f"Legacy request has conflicting response hashes: {provider}/{endpoint}/{ticker}/{available}"
            )
        output.append(
            {
                "provider": provider,
                "endpoint_id": endpoint,
                "ticker": ticker,
                "provider_symbol": ticker,
                "status": "AVAILABLE",
                "http_status": None,
                "elapsed_ms": 0,
                "provider_row_count": len(group),
                "normalized_rows": [_normalized_row(row) for row in group],
                "request_started_at_utc": min(str(row["fetched_at_utc"]) for row in group),
                "response_received_at_utc": available,
                "response_sha256": str(group[0]["response_sha256"]),
                "detail": "migrated_normalized_legacy_snapshot",
            }
        )
    return output


def _cycle_date(cycle: str) -> str:
    dashed = re.search(r"(?<!\d)(20\d{2}-\d{2}-\d{2})(?!\d)", cycle)
    if dashed:
        return date.fromisoformat(dashed.group(1)).isoformat()
    compact = re.search(r"(?<!\d)(20\d{6})(?!\d)", cycle)
    if compact:
        return datetime.strptime(compact.group(1), "%Y%m%d").date().isoformat()
    return ""


def run_selftest() -> None:
    assert _cycle_date("2026-07-31-eod") == "2026-07-31"
    assert _cycle_date("capture-20260731-tier0-b001") == "2026-07-31"
    assert _cycle_date("unknown") == ""
    print("provider legacy migration selftest: PASS")


def main() -> int:
    args = parse_args()
    if args.selftest:
        run_selftest()
        return 0
    config_path = args.config.resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    monitor = cfg_get(config, "expectations_monitor", {})
    ingestion = cfg_get(config, "provider_ingestion", {})
    if not isinstance(monitor, dict) or not isinstance(ingestion, dict):
        raise ValueError("Provider and monitor configuration must be mappings")
    validate_provider_ingestion_policy(ingestion)
    legacy_path = ensure_not_prod_path(
        args.legacy_db.resolve()
        if args.legacy_db
        else resolve_path(
            monitor.get("database_path", "db/expectations_monitor.sqlite"),
            base_dir=config_path.parent,
        ),
        label="legacy expectations monitor database",
    )
    store_path = ensure_not_prod_path(
        args.store_db.resolve()
        if args.store_db
        else resolve_path(
            ingestion.get("database_path", "db/provider_observations.sqlite"),
            base_dir=config_path.parent,
        ),
        label="provider observation database",
    )
    output_dir = ensure_not_prod_path(
        (
            args.output_dir
            if args.output_dir
            else paths.output_dir / str(ingestion.get("output_subdir", "provider_ingestion")) / "migration"
        ),
        label="provider migration output path",
    )
    if not legacy_path.is_file():
        raise FileNotFoundError(f"Legacy expectations-monitor database is missing: {legacy_path}")
    legacy = sqlite3.connect(legacy_path.as_uri() + "?mode=ro", uri=True)
    legacy.execute("PRAGMA query_only=ON")
    legacy.row_factory = sqlite3.Row
    try:
        cycles = legacy.execute(
            "SELECT retrieval_cycle,COUNT(*) row_count,MIN(available_at_utc) first_seen,"
            "MAX(available_at_utc) last_seen FROM provider_estimate_snapshots "
            "GROUP BY retrieval_cycle ORDER BY first_seen,retrieval_cycle"
        ).fetchall()
        legacy_count = int(legacy.execute("SELECT COUNT(*) FROM provider_estimate_snapshots").fetchone()[0])
        if not args.execute:
            write_manifest(
                output_dir / "legacy_migration_manifest.json",
                {
                    "schema_version": "provider_legacy_migration_manifest_v1",
                    "acceptance": "DRY_RUN",
                    "legacy_db": str(legacy_path),
                    "store_db": str(store_path),
                    "legacy_row_count": legacy_count,
                    "cycle_count": len(cycles),
                    "preserve_observed_timestamps": True,
                    "invent_provider_vintages": False,
                },
            )
            print(f"PROVIDER LEGACY MIGRATION: DRY_RUN; rows={legacy_count}; cycles={len(cycles)}")
            return 0
        timeout = float(ingestion.get("writer_lock_timeout_sec", 30.0))
        zone = ZoneInfo(str(ingestion.get("timezone", "America/New_York")))
        calendar_name = str(ingestion.get("exchange_calendar", "XNYS"))
        decision_cutoff = str(ingestion.get("decision_cutoff_local", "09:25"))
        summary: list[dict[str, Any]] = []
        lock_path = store_path.with_suffix(store_path.suffix + ".writer.lock")
        with writer_lock(lock_path, timeout_sec=timeout):
            store = connect_store(store_path, timeout_sec=timeout)
            try:
                for cycle_row in cycles:
                    cycle = str(cycle_row["retrieval_cycle"])
                    rows = legacy.execute(
                        "SELECT * FROM provider_estimate_snapshots WHERE retrieval_cycle=? "
                        "ORDER BY available_at_utc,provider,endpoint_id,ticker,estimate_type,fiscal_period_end",
                        (cycle,),
                    ).fetchall()
                    requests = _cycle_requests(rows)
                    members = [
                        {
                            "ticker": ticker,
                            "tier": "legacy",
                            "sector": "",
                            "source_pipeline": "legacy_provider_snapshot",
                        }
                        for ticker in sorted({str(row["ticker"]) for row in rows})
                    ]
                    providers = sorted({str(row["provider"]) for row in rows})
                    available_start = min(str(row["available_at_utc"]) for row in rows)
                    available_end = max(str(row["available_at_utc"]) for row in rows)
                    cycle_started = min(str(row["fetched_at_utc"]) for row in rows)
                    observed_date = (
                        datetime.fromisoformat(available_end.replace("Z", "+00:00")).astimezone(zone).date().isoformat()
                    )
                    stated_date = _cycle_date(cycle)
                    mismatch = bool(stated_date and stated_date != observed_date)
                    universe_id = freeze_universe(
                        store,
                        source_run_as_of=stated_date or observed_date,
                        capture_phase="legacy_migration",
                        members=members,
                        providers=providers,
                        created_at_utc=available_start,
                    )
                    result = persist_capture(
                        store,
                        cycle_id=f"legacy:{cycle}",
                        capture_phase="legacy_migration",
                        requested_portfolio_as_of=stated_date,
                        actual_capture_date=observed_date,
                        universe_id=universe_id,
                        started_at_utc=cycle_started,
                        completed_at_utc=available_end,
                        request_records=requests,
                        source_code_digest=sha256_file(Path(__file__).resolve()),
                        config_digest=sha256_file(config_path),
                        timezone_name=str(ingestion.get("timezone", "America/New_York")),
                        calendar_name=calendar_name,
                        decision_cutoff_local=decision_cutoff,
                        status="MIGRATED",
                        metadata={
                            "legacy_asof_mismatch": mismatch,
                            "legacy_retrieval_cycle": cycle,
                            "provider_vintage_invented": False,
                        },
                    )
                    run_row = store.execute(
                        "SELECT run_id FROM capture_runs WHERE cycle_id=?",
                        (f"legacy:{cycle}",),
                    ).fetchone()
                    if run_row is None:
                        raise RuntimeError(f"Migrated capture run is missing: {cycle}")
                    annotation = {
                        "run_id": str(run_row["run_id"]),
                        "legacy_retrieval_cycle": cycle,
                        "stated_as_of_date": stated_date,
                        "observed_capture_date": observed_date,
                        "legacy_asof_mismatch": int(mismatch),
                        "annotation_version": "legacy_migration_annotation_v1",
                    }
                    with store:
                        store.execute(
                            "INSERT OR IGNORE INTO legacy_migration_annotations VALUES(?,?,?,?,?,?,?,?)",
                            (
                                annotation["run_id"],
                                cycle,
                                stated_date,
                                observed_date,
                                int(mismatch),
                                annotation["annotation_version"],
                                datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                                hashlib.sha256(
                                    json.dumps(annotation, sort_keys=True, separators=(",", ":")).encode("utf-8")
                                ).hexdigest(),
                            ),
                        )
                    summary.append(
                        {
                            "retrieval_cycle": cycle,
                            "legacy_rows": len(rows),
                            "request_count": len(requests),
                            "available_start_utc": available_start,
                            "available_end_utc": available_end,
                            "legacy_asof_mismatch": int(mismatch),
                            "status": "IDEMPOTENT" if result.get("idempotent") else "MIGRATED",
                        }
                    )
                errors = verify_store(store)
                migrated_count = int(store.execute("SELECT COUNT(*) FROM estimate_observations").fetchone()[0])
                mismatch_count = int(
                    store.execute(
                        "SELECT COUNT(*) FROM legacy_migration_annotations WHERE legacy_asof_mismatch=1"
                    ).fetchone()[0]
                )
            finally:
                store.close()
    finally:
        legacy.close()
    if errors:
        raise RuntimeError(f"Migrated provider store failed verification: {errors}")
    summary_path = output_dir / "legacy_migration_cycles.csv"
    write_csv(summary_path, SUMMARY_FIELDS, summary)
    write_manifest(
        output_dir / "legacy_migration_manifest.json",
        {
            "schema_version": "provider_legacy_migration_manifest_v1",
            "acceptance": "PASS",
            "legacy_db": str(legacy_path),
            "store_db": str(store_path),
            "legacy_row_count": legacy_count,
            "store_observation_count": migrated_count,
            "cycle_count": len(cycles),
            "legacy_asof_mismatch_count": mismatch_count,
            "authoritative_mismatch_source": "legacy_migration_annotations_v1",
            "preserve_observed_timestamps": True,
            "invent_provider_vintages": False,
            "inputs_sha256": {
                str(config_path): sha256_file(config_path),
                str(Path(__file__).resolve()): sha256_file(Path(__file__).resolve()),
            },
            "outputs_sha256": {summary_path.name: sha256_file(summary_path)},
        },
    )
    print(
        f"PROVIDER LEGACY MIGRATION: PASS; legacy_rows={legacy_count}; "
        f"store_observations={migrated_count}; cycles={len(cycles)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

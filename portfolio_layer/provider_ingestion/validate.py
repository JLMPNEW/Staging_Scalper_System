#!/usr/bin/env python3
"""Validate provider-store integrity and scheduled-capture continuity."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.provider_ingestion.health import (  # noqa: E402
    CONTINUITY_FIELDS,
    capture_continuity_rows,
    continuity_gaps,
    expected_capture_slots,
    validate_provider_ingestion_policy,
)
from portfolio_layer.provider_ingestion.store import (  # noqa: E402
    connect_store_readonly,
    digest,
    verify_store,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--now-utc", type=datetime.fromisoformat)
    parser.add_argument(
        "--require-continuity",
        action="store_true",
        help="Exit nonzero when any elapsed scheduled slot lacks an accepted capture.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    ingestion = cfg_get(config, "provider_ingestion", {})
    if not isinstance(ingestion, dict):
        raise ValueError("provider_ingestion config must be a mapping")
    validate_provider_ingestion_policy(ingestion)
    recovery = ingestion.get("recovery", {})
    schedules = ingestion.get("schedules", {})
    if not isinstance(recovery, dict) or not isinstance(schedules, dict):
        raise ValueError("provider ingestion recovery and schedules must be mappings")
    now_utc = args.now_utc or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        raise ValueError("--now-utc must include a timezone")
    if args.now_utc is not None and args.output_dir is None:
        raise ValueError("--now-utc requires --output-dir so simulated validation cannot overwrite live reports")
    timezone_name = str(ingestion.get("timezone", "America/New_York"))
    calendar_name = str(ingestion.get("exchange_calendar", "XNYS"))
    local_today = now_utc.astimezone(ZoneInfo(timezone_name)).date()
    service_started = date.fromisoformat(str(recovery.get("service_started_on", local_today.isoformat())))
    if service_started > local_today:
        raise ValueError("provider ingestion service start cannot be in the future")
    continuity_start = service_started
    slots = expected_capture_slots(
        start=continuity_start,
        end=local_today,
        now_utc=now_utc,
        schedules=schedules,
        timezone_name=timezone_name,
        calendar_name=calendar_name,
        grace_minutes=ingestion.get(
            "phase_grace_minutes",
            int(ingestion.get("schedule_grace_minutes", 20)),
        ),
        service_started_on=service_started,
    )

    store_path = ensure_not_prod_path(
        resolve_path(
            ingestion.get("database_path", "db/provider_observations.sqlite"),
            base_dir=config_path.parent,
        ),
        label="provider observation database",
    )
    conn = connect_store_readonly(
        store_path,
        timeout_sec=float(ingestion.get("writer_lock_timeout_sec", 30.0)),
    )
    try:
        conn.execute("BEGIN")
        errors = verify_store(conn)
        continuity_rows = capture_continuity_rows(conn, slots=slots)
        counts = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "capture_runs",
                "scheduled_dispatch_attempts",
                "capture_requests",
                "estimate_versions",
                "estimate_observations",
                "estimate_changes",
                "coverage_daily",
                "legacy_migration_annotations",
            )
        }
        latest = conn.execute(
            "SELECT cycle_id,completed_at_utc,status,run_digest FROM capture_runs ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        latest_accepted = conn.execute(
            "SELECT cycle_id,completed_at_utc,status,run_digest FROM capture_runs "
            "WHERE status IN ('PASS','PASS_WITH_WARNINGS','MIGRATED') "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        mismatch_count = int(
            conn.execute("SELECT COUNT(*) FROM legacy_migration_annotations WHERE legacy_asof_mismatch=1").fetchone()[0]
        )
        conn.rollback()
    finally:
        conn.close()

    gaps = continuity_gaps(continuity_rows)
    strict_continuity_failure = bool(gaps and args.require_continuity)
    acceptance = "FAIL" if errors or strict_continuity_failure else "PASS_WITH_WARNINGS" if gaps else "PASS"
    output_dir = ensure_not_prod_path(
        (
            args.output_dir
            if args.output_dir
            else paths.output_dir / str(ingestion.get("output_subdir", "provider_ingestion")) / "validation"
        ),
        label="provider validation output path",
    )
    continuity_path = output_dir / "provider_capture_continuity.csv"
    manifest_path = output_dir / "provider_store_validation_manifest.json"
    write_csv(continuity_path, CONTINUITY_FIELDS, continuity_rows)
    latest_payload = {} if latest is None else dict(latest)
    latest_accepted_payload = {} if latest_accepted is None else dict(latest_accepted)
    write_manifest(
        manifest_path,
        {
            "schema_version": "provider_store_validation_manifest_v2",
            "acceptance": acceptance,
            "require_continuity": bool(args.require_continuity),
            "store_errors": errors,
            "continuity_start": continuity_start.isoformat(),
            "continuity_through": local_today.isoformat(),
            "elapsed_slot_count": len(continuity_rows),
            "continuity_gap_count": len(gaps),
            "continuity_gaps": gaps,
            "counts": counts,
            "legacy_asof_mismatch_count": mismatch_count,
            "latest_run": latest_payload,
            "latest_accepted_run": latest_accepted_payload,
            "state_digest": digest(
                {
                    "counts": counts,
                    "latest": latest_payload,
                    "latest_accepted": latest_accepted_payload,
                    "continuity": continuity_rows,
                }
            ),
            "inputs_sha256": {
                str(config_path): sha256_file(config_path),
                str(Path(__file__).resolve()): sha256_file(Path(__file__).resolve()),
                str(Path(__file__).with_name("health.py")): sha256_file(Path(__file__).with_name("health.py")),
                str(Path(__file__).with_name("store.py")): sha256_file(Path(__file__).with_name("store.py")),
            },
            "outputs_sha256": {
                continuity_path.name: sha256_file(continuity_path),
            },
        },
    )
    print(f"PROVIDER STORE VALIDATION: {acceptance}; counts={counts}; continuity_gaps={len(gaps)}")
    return 1 if errors or strict_continuity_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())

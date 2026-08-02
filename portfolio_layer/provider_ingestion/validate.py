#!/usr/bin/env python3
"""Validate the independent provider observation store and seal its current state."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import sha256_file, write_manifest  # noqa: E402
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.provider_ingestion.store import connect_store, digest, verify_store  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    ingestion = cfg_get(config, "provider_ingestion", {})
    if not isinstance(ingestion, dict):
        raise ValueError("provider_ingestion config must be a mapping")
    store_path = ensure_not_prod_path(
        resolve_path(
            ingestion.get("database_path", "db/provider_observations.sqlite"),
            base_dir=config_path.parent,
        ),
        label="provider observation database",
    )
    conn = connect_store(
        store_path,
        timeout_sec=float(ingestion.get("writer_lock_timeout_sec", 30.0)),
    )
    try:
        errors = verify_store(conn)
        counts = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "capture_runs",
                "capture_requests",
                "estimate_versions",
                "estimate_observations",
                "estimate_changes",
                "coverage_daily",
                "legacy_migration_annotations",
            )
        }
        latest = conn.execute(
            "SELECT cycle_id,completed_at_utc,status,run_digest FROM capture_runs "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        mismatch_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM legacy_migration_annotations WHERE legacy_asof_mismatch=1"
            ).fetchone()[0]
        )
    finally:
        conn.close()
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else paths.output_dir / str(ingestion.get("output_subdir", "provider_ingestion")) / "validation"
    )
    write_manifest(
        output_dir / "provider_store_validation_manifest.json",
        {
            "schema_version": "provider_store_validation_manifest_v1",
            "acceptance": "PASS" if not errors else "FAIL",
            "errors": errors,
            "counts": counts,
            "legacy_asof_mismatch_count": mismatch_count,
            "latest_run": {} if latest is None else dict(latest),
            "state_digest": digest({"counts": counts, "latest": None if latest is None else dict(latest)}),
            "inputs_sha256": {
                str(config_path): sha256_file(config_path),
                str(Path(__file__).resolve()): sha256_file(Path(__file__).resolve()),
                str(Path(__file__).with_name("store.py")): sha256_file(Path(__file__).with_name("store.py")),
            },
        },
    )
    print(f"PROVIDER STORE VALIDATION: {'PASS' if not errors else 'FAIL'}; counts={counts}")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())


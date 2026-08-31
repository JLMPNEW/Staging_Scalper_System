"""Rehearse bounded Consumer Defensive activation against a SQLite backup."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from consumer_defensive.core.atomic_io import atomic_write_text  # noqa: E402
from consumer_defensive.core.config import load_config  # noqa: E402
from consumer_defensive.core.stage3_runtime import database_path  # noqa: E402
from consumer_defensive.core.stage12_operational import (  # noqa: E402
    validate_operational_snapshot,
)

DEFAULT_CONFIG = ROOT / "consumer_defensive" / "config.yaml"


def iso_date(raw: str) -> str:
    date.fromisoformat(raw)
    return raw


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--source-db", type=Path)
    parser.add_argument("--backup-db", type=Path, required=True)
    parser.add_argument("--asof", type=iso_date, required=True)
    parser.add_argument("--stage8-root", type=Path, required=True)
    parser.add_argument("--stage9-root", type=Path, required=True)
    parser.add_argument("--factor-validation-root", type=Path, required=True)
    parser.add_argument("--activation-registry", type=Path, required=True)
    parser.add_argument("--activation-registry-sha256", required=True)
    parser.add_argument("--change-control-public-key", type=Path, required=True)
    parser.add_argument("--rehearsal-output-root", type=Path, required=True)
    return parser.parse_args()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_backup(source: Path, target: Path) -> None:
    source = source.expanduser().resolve()
    target = target.expanduser().resolve()
    if source == target:
        raise ValueError("backup database must be distinct from source database")
    if target.exists():
        raise FileExistsError(f"rehearsal backup already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as source_conn, sqlite3.connect(target) as target_conn:
        source_conn.backup(target_conn)
        if target_conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("rehearsal backup failed SQLite integrity_check")


def main() -> int:
    args = parse_args()
    bundle = load_config(args.config)
    source_db = database_path(bundle, args.source_db).expanduser().resolve()
    backup_db = args.backup_db.expanduser().resolve()
    source_before = _sha256(source_db)
    _sqlite_backup(source_db, backup_db)
    output_root = args.rehearsal_output_root.expanduser().resolve()
    command = [
        sys.executable,
        str(ROOT / "consumer_defensive/scripts/28_run_consumer_defensive_stage12_pipeline.py"),
        "--config", str(args.config.resolve()),
        "--db", str(backup_db),
        "--asof", args.asof,
        "--stage8-root", str(args.stage8_root.resolve()),
        "--stage9-root", str(args.stage9_root.resolve()),
        "--factor-validation-root", str(args.factor_validation_root.resolve()),
        "--operational-output-root", str(output_root),
        "--activation-registry", str(args.activation_registry.resolve()),
        "--activation-registry-sha256", args.activation_registry_sha256,
        "--change-control-public-key", str(args.change_control_public_key.resolve()),
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode:
        raise RuntimeError(
            "Stage 12 rehearsal failed: "
            + json.dumps(
                {
                    "returncode": completed.returncode,
                    "stdout_tail": completed.stdout[-4000:],
                    "stderr_tail": completed.stderr[-4000:],
                },
                sort_keys=True,
            )
        )
    operational = validate_operational_snapshot(output_root / args.asof)
    source_after = _sha256(source_db)
    if source_after != source_before:
        raise RuntimeError("source database changed during backup rehearsal")
    manifest = {
        "schema_version": "consumer_defensive_production_rehearsal_v1",
        "acceptance": "PASS",
        "asof_date": args.asof,
        "source_database": str(source_db),
        "source_database_sha256_before": source_before,
        "source_database_sha256_after": source_after,
        "backup_database": str(backup_db),
        "backup_database_sha256": _sha256(backup_db),
        "operational_manifest_sha256": operational["payload_sha256"],
        "promoted_scopes": operational["promoted_scopes"],
        "portfolio_candidate_count": operational["portfolio_candidate_count"],
        "production_database_write_count": 0,
    }
    manifest_path = output_root / args.asof / "consumer_defensive_production_rehearsal.json"
    atomic_write_text(manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

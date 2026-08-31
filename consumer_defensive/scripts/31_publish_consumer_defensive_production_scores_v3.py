#!/usr/bin/env python3
"""Publish immutable Consumer Defensive production scores from completed PIT features."""

from __future__ import annotations

# Direct execution bootstraps the repository root before package imports.
# ruff: noqa: E402

import argparse
import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from consumer_defensive.core.config import load_config
from consumer_defensive.core.production_scores_v3 import (
    build_production_rank_rows,
    file_sha256,
    load_bound_artifacts,
    publish_production_scores,
    publisher_bindings,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Apply the exact calibrated Consumer candidates to a completed Stage 6A "
            "signal snapshot and publish the v3 lock-bound rank contract."
        )
    )
    parser.add_argument("--asof", required=True, help="Allocation date (YYYY-MM-DD).")
    parser.add_argument(
        "--signal-asof-date",
        help="Completed signal-session date; defaults to the latest Stage 6A snapshot.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "consumer_defensive" / "config.yaml",
    )
    parser.add_argument("--db", type=Path, help="Override the pinned read-only source DB.")
    parser.add_argument(
        "--output-root",
        type=Path,
        help="Override the repo output root; dated Consumer paths remain fixed.",
    )
    parser.add_argument(
        "--activation-registry",
        type=Path,
        help="Override the pinned v3 activation-registry path.",
    )
    parser.add_argument(
        "--trusted-activation-registry-file-sha256",
        help="Exact trusted SHA-256 of activation-registry file bytes.",
    )
    parser.add_argument(
        "--candidate-registry",
        type=Path,
        help="Override the pinned v2 calibration candidate-registry path.",
    )
    parser.add_argument(
        "--trusted-candidate-registry-file-sha256",
        help="Exact trusted SHA-256 of candidate-registry file bytes.",
    )
    return parser


def _safe_file(path: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or resolved.is_symlink():
        raise FileNotFoundError(f"{label} is missing or unsafe: {resolved}")
    return resolved


def _open_read_only(path: Path) -> sqlite3.Connection:
    resolved = _safe_file(path, label="--db")
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
        connection.close()
        raise RuntimeError("failed to establish a query-only SQLite connection")
    return connection


def _latest_signal_date(
    connection: sqlite3.Connection, *, allocation_asof_date: str
) -> str:
    row = connection.execute(
        """SELECT MAX(asof_date) FROM feature_scoring_input
           WHERE model_family='consumer_defensive' AND asof_date<?""",
        (allocation_asof_date,),
    ).fetchone()
    value = "" if row is None or row[0] is None else str(row[0])
    if not value:
        raise RuntimeError("source DB has no completed Consumer Stage 6A feature snapshot")
    return value


def _database_byte_identities(path: Path) -> dict[str, str]:
    resolved = _safe_file(path, label="--db")
    wal_path = Path(f"{resolved}-wal")
    if wal_path.exists() and (not wal_path.is_file() or wal_path.is_symlink()):
        raise RuntimeError(f"source database WAL path is unsafe: {wal_path}")
    return {
        "source_database_file_sha256": file_sha256(resolved),
        "source_database_wal_file_sha256": (
            file_sha256(wal_path) if wal_path.is_file() else ""
        ),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config_path = _safe_file(args.config, label="--config")
    bundle = load_config(config_path)
    bindings = publisher_bindings(bundle)
    db_path = (args.db or bindings["source_database_path"]).expanduser().resolve()
    output_root = (args.output_root or bindings["output_root"]).expanduser().resolve()
    activation_path = (
        args.activation_registry or bindings["activation_registry_path"]
    ).expanduser().resolve()
    candidate_path = (
        args.candidate_registry or bindings["candidate_registry_path"]
    ).expanduser().resolve()
    activation_file_pin = (
        args.trusted_activation_registry_file_sha256
        or bindings["activation_registry_file_sha256"]
    )
    candidate_file_pin = (
        args.trusted_candidate_registry_file_sha256
        or bindings["candidate_registry_file_sha256"]
    )
    activation, candidates, artifact_identities = load_bound_artifacts(
        activation_registry_path=activation_path,
        trusted_activation_registry_file_sha256=activation_file_pin,
        trusted_activation_registry_payload_sha256=bindings[
            "activation_registry_payload_sha256"
        ],
        candidate_registry_path=candidate_path,
        trusted_candidate_registry_file_sha256=candidate_file_pin,
        trusted_candidate_registry_payload_sha256=bindings[
            "candidate_registry_payload_sha256"
        ],
    )
    connection = _open_read_only(db_path)
    try:
        connection.execute("BEGIN")
        data_version_before = int(connection.execute("PRAGMA data_version").fetchone()[0])
        signal_asof_date = args.signal_asof_date or _latest_signal_date(
            connection,
            allocation_asof_date=args.asof,
        )
        database_before = _database_byte_identities(db_path)
        rows, source = build_production_rank_rows(
            connection,
            bundle,
            signal_asof_date=signal_asof_date,
            allocation_asof_date=args.asof,
            activation_registry=activation,
            candidate_registry=candidates,
            bindings=bindings,
        )
        database_after = _database_byte_identities(db_path)
        data_version_after = int(connection.execute("PRAGMA data_version").fetchone()[0])
        if database_after != database_before or data_version_after != data_version_before:
            raise RuntimeError(
                "source database main/WAL bytes changed during score construction"
            )
        source.update(database_before)
        source["source_database_data_version"] = data_version_before
        connection.rollback()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    manifest = publish_production_scores(
        output_root=output_root,
        allocation_asof_date=args.asof,
        rows=rows,
        source=source,
        artifact_identities=artifact_identities,
        source_database_path=db_path,
    )
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "allocation_asof_date": manifest["allocation_asof_date"],
                "signal_asof_date": manifest["signal_asof_date"],
                "rank_csv_path": manifest["rank_csv_path"],
                "rank_row_count": manifest["rank_row_count"],
                "rank_ready_count": manifest["rank_ready_count"],
                "oos_valid_count": manifest["oos_valid_count"],
                "payload_sha256": manifest["payload_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

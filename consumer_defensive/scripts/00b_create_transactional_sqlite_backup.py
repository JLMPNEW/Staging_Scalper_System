#!/usr/bin/env python3
"""Create and verify an atomic transaction-consistent SQLite backup."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser.parse_args()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _readonly_uri(path: Path) -> str:
    return f"file:{path.as_posix()}?mode=ro"


def create_backup(source: Path, destination: Path) -> dict[str, object]:
    source = source.expanduser().resolve(strict=True)
    destination = destination.expanduser().resolve()
    if source == destination:
        raise ValueError("Backup destination must differ from source")
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite existing backup: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with closing(sqlite3.connect(_readonly_uri(source), uri=True)) as source_conn:
            with closing(sqlite3.connect(temporary)) as destination_conn:
                source_conn.backup(destination_conn)
                destination_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        with closing(sqlite3.connect(_readonly_uri(temporary), uri=True)) as check_conn:
            quick_check = str(check_conn.execute("PRAGMA quick_check").fetchone()[0])
            foreign_key_sample = [tuple(row) for row in check_conn.execute("PRAGMA foreign_key_check").fetchmany(10)]
        if quick_check != "ok" or foreign_key_sample:
            raise RuntimeError(
                f"Backup integrity failed: quick_check={quick_check!r} foreign_keys={foreign_key_sample!r}"
            )

        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

    return {
        "source": str(source),
        "destination": str(destination),
        "bytes": destination.stat().st_size,
        "sha256": _sha256(destination),
        "quick_check": "ok",
        "foreign_key_violations": 0,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


def main() -> int:
    args = parse_args()
    payload = create_backup(args.source, args.destination)
    report = args.report.expanduser().resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({**payload, "report": str(report)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

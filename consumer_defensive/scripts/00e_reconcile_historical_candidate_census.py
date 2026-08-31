#!/usr/bin/env python3
"""Reconcile PIT-overlap candidates to reviewed Norgate asset identities."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from consumer_defensive.core.atomic_io import (  # noqa: E402
    atomic_text_writer,
    atomic_write_text,
)
from consumer_defensive.core.historical_census_reconciliation import (  # noqa: E402
    reconcile_historical_candidates,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-csv", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    materialized = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not materialized:
        atomic_write_text(path, "", encoding="utf-8")
        return
    with atomic_text_writer(path, encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(materialized[0]))
        writer.writeheader()
        writer.writerows(materialized)


def require_new_output(output_dir: Path, names: tuple[str, ...]) -> None:
    existing = [str(output_dir / name) for name in names if (output_dir / name).exists()]
    if existing:
        raise FileExistsError(
            "Candidate reconciliation artifacts are immutable; choose a new "
            f"--output-dir: {existing}"
        )


def main() -> int:
    args = parse_args()
    require_new_output(
        args.output_dir,
        ("historical_candidate_reconciliation.csv", "summary.json"),
    )
    database = args.database.expanduser().resolve()
    uri = f"file:{database.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows, summary = reconcile_historical_candidates(
            conn,
            read_csv(args.candidate_csv),
        )
    finally:
        conn.close()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(
        args.output_dir / "historical_candidate_reconciliation.csv",
        rows,
    )
    atomic_write_text(
        args.output_dir / "summary.json",
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"output_dir": str(args.output_dir), **summary},
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

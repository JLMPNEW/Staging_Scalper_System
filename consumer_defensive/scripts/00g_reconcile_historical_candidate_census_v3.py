#!/usr/bin/env python3
"""Reconcile census union with optional reviewed PIT-membership overrides."""

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
from consumer_defensive.core.historical_census_reconciliation_v3 import (  # noqa: E402
    reconcile_historical_candidates_v3,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate-csv', type=Path, required=True)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--reviewed-pit-overrides', type=Path)
    parser.add_argument('--output-dir', type=Path, required=True)
    return parser.parse_args()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open('r', encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def _read_overrides(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    payload = json.loads(path.read_text(encoding='utf-8'))
    if isinstance(payload, dict):
        payload = payload.get('overrides')
    if not isinstance(payload, list) or not all(
        isinstance(row, dict) for row in payload
    ):
        raise ValueError('Reviewed PIT overrides must be a JSON row list.')
    return [dict(row) for row in payload]


def _union_fieldnames(rows: Iterable[dict[str, Any]]) -> list[str]:
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fields.append(key)
    return fields


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        atomic_write_text(path, '', encoding='utf-8')
        return
    with atomic_text_writer(path, encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=_union_fieldnames(rows))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    if args.output_dir.exists():
        raise FileExistsError(
            'Census reconciliation output is immutable; choose a new directory: '
            f'{args.output_dir}'
        )
    database = args.database.expanduser().resolve()
    conn = sqlite3.connect(f'file:{database.as_posix()}?mode=ro', uri=True)
    try:
        rows, summary = reconcile_historical_candidates_v3(
            conn,
            _read_csv(args.candidate_csv),
            reviewed_pit_overrides=_read_overrides(
                args.reviewed_pit_overrides
            ),
        )
    finally:
        conn.close()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    _write_csv(
        args.output_dir / 'historical_candidate_reconciliation.csv', rows
    )
    atomic_write_text(
        args.output_dir / 'summary.json',
        json.dumps(summary, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )
    print(json.dumps(
        {'output_dir': str(args.output_dir), **summary},
        indent=2,
        sort_keys=True,
    ))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

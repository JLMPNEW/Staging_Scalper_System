#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.adapters import load_registry  # noqa: E402
from dedicated_parser.promotion import promote_run  # noqa: E402
from dedicated_parser.storage import connect_database  # noqa: E402
from industrials.core.config import (  # noqa: E402
    cfg_get,
    load_yaml,
    resolve_path,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
ADAPTER = (
    "industrials.machinery.dedicated_parser_adapter:"
    "extract_metric_evidence"
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Promote reviewed machinery parser evidence into the canonical "
            "SEC input lane."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--run-id", type=int, default=0)
    parser.add_argument("--output-json", type=Path, default=None)
    return parser.parse_args(argv)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _latest_run_id(
    conn: sqlite3.Connection,
    *,
    asof_date: str,
    adapter_version: str,
) -> int:
    row = conn.execute(
        """
        SELECT run_id
        FROM sec_parser_run
        WHERE model_family = 'machinery'
          AND asof_date = ?
          AND adapter_version = ?
          AND status = 'COMPLETED'
          AND failed_work_count = 0
        ORDER BY run_id DESC
        LIMIT 1
        """,
        (asof_date, adapter_version),
    ).fetchone()
    if row is None:
        raise ValueError(
            "No fully completed machinery parser run matches "
            f"asof={asof_date} adapter={adapter_version}"
        )
    return int(row["run_id"])


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = (
        args.db.expanduser().resolve()
        if args.db is not None
        else resolve_path(
            cfg_get(config, "paths.database_path"),
            base_dir=config_path.parent,
        )
    )
    registry = load_registry(ADAPTER)
    source_id = str(
        cfg_get(
            config,
            "dedicated_parser.production_source_id",
            "dedicated_parser_production",
        )
    ).strip()
    min_confidence = float(
        cfg_get(
            config,
            "dedicated_parser.production_min_confidence",
            0.90,
        )
    )
    with connect_database(db_path) as conn:
        run_id = args.run_id or _latest_run_id(
            conn,
            asof_date=args.asof,
            adapter_version=registry.adapter_version,
        )
        summary = promote_run(
            conn,
            run_id=run_id,
            registry=registry,
            source_id=source_id,
            min_confidence=min_confidence,
        )
    output_path = (
        args.output_json.expanduser().resolve()
        if args.output_json is not None
        else resolve_path(
            cfg_get(
                config,
                "dedicated_parser.production_manifest_json",
                (
                    "../../output/industrials/machinery/dedicated_parser/"
                    "dedicated_parser_production_promotion.json"
                ),
            ),
            base_dir=config_path.parent,
        )
    )
    _write_json(output_path, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Load the independent Basic Materials Stage 5A metric and all-source contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from basic_materials.core.config import load_config, resolve_cli_path  # noqa: E402
from basic_materials.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from basic_materials.core.source_registry import load_source_registry, upsert_source_registry  # noqa: E402
from basic_materials.core.specialized_contract import (  # noqa: E402
    load_specialized_contract,
    load_specialized_metric_registry,
    load_specialized_source_policy,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--db", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    database = resolve_cli_path(args.db, config.paths.database)
    if database.name.lower() != "basic_materials.sqlite":
        raise ValueError("Database override filename must be basic_materials.sqlite")
    registry = load_specialized_metric_registry(config.paths.specialized_metric_registry)
    source_policy = load_specialized_source_policy(config.paths.specialized_source_policy)
    conn = connect(database, config.runtime.sqlite_timeout_seconds)
    run_id = ""
    try:
        init_db(conn)
        conn.execute("BEGIN IMMEDIATE")
        upsert_source_registry(conn, load_source_registry(config.paths.source_registry), utc_now())
        conn.commit()
        run_id = start_run(
            conn,
            stage="stage_5a_specialized_contract",
            command="12_load_basic_materials_specialized_contract.py",
            database_path=database,
            input_path=registry.path,
            input_sha256=registry.checksum,
            input_row_count=len(registry.metrics),
            details={
                "source_policy_sha256": source_policy.checksum,
                "parser_execution_enabled": False,
                "historical_feature_materialization_allowed": False,
            },
        )
        stats = load_specialized_contract(conn, registry=registry, source_policy=source_policy)
        details = {**stats.as_dict(), "database_path": str(database)}
        finish_run(conn, run_id, succeeded=True, details=details)
        print(json.dumps({"succeeded": True, "run_id": run_id, **details}, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        conn.rollback()
        if run_id:
            try:
                finish_run(conn, run_id, succeeded=False, error_message=f"{type(exc).__name__}: {exc}")
            except Exception:
                pass
        print(json.dumps({"succeeded": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2), file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())


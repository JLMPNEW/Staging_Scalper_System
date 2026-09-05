"""Initialize and identify the dedicated Basic Materials SQLite database."""

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
from basic_materials.core.independence import run_independence_checks  # noqa: E402
from basic_materials.core.input_manifest import validate_authoritative_input  # noqa: E402
from basic_materials.core.source_registry import load_source_registry, upsert_source_registry  # noqa: E402
from basic_materials.core.universe import load_universe_policy  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Basic Materials config path")
    parser.add_argument("--db", type=Path, help="Dedicated database path; filename must be basic_materials.sqlite")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    conn = None
    run_id = None
    try:
        config = load_config(args.config)
        independence = run_independence_checks(config)
        if not independence.passed:
            raise RuntimeError("Basic Materials independence checks failed")
        manifest = validate_authoritative_input(
            config.paths.authoritative_input_manifest,
            config.paths.universe_csv,
        )
        policy = load_universe_policy(config.paths.universe_policy)
        registry = load_source_registry(config.paths.source_registry)
        if manifest.row_count != policy.expected_current_rows:
            raise RuntimeError("Manifest and universe policy row counts differ")

        database_path = resolve_cli_path(args.db, config.paths.database)
        if database_path.name.lower() != "basic_materials.sqlite":
            raise ValueError("Database override filename must be basic_materials.sqlite")
        conn = connect(database_path, config.runtime.sqlite_timeout_seconds)
        result = init_db(conn)
        conn.execute("BEGIN IMMEDIATE")
        source_count = upsert_source_registry(conn, registry, utc_now())
        conn.commit()
        run_id = start_run(
            conn,
            stage="stage_1_database",
            command="00_init_basic_materials_db",
            database_path=database_path,
            input_path=manifest.path,
            input_sha256=manifest.sha256,
            input_row_count=manifest.row_count,
            details={"policy_version": policy.policy_version, "registry_version": registry.version},
        )
        details = {**result, "source_registry_rows": source_count, "database_path": str(database_path)}
        finish_run(conn, run_id, succeeded=True, details=details)
        print(json.dumps({"succeeded": True, "run_id": run_id, **details}, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        if conn is not None and run_id is not None:
            try:
                finish_run(conn, run_id, succeeded=False, error_message=f"{type(exc).__name__}: {exc}")
            except Exception:
                pass
        print(json.dumps({"succeeded": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2), file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())


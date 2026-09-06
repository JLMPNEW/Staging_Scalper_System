"""Load the governed Basic Materials Stage 4A financial contract."""

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
from basic_materials.core.financial_data_contract import (  # noqa: E402
    load_financial_contract,
    load_financial_data_policy,
    read_and_validate_financial_contract,
    validate_financial_data_manifest,
)
from basic_materials.core.source_registry import (  # noqa: E402
    load_source_registry,
    upsert_source_registry,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Basic Materials config path")
    parser.add_argument("--db", type=Path, help="Dedicated basic_materials.sqlite path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    conn = None
    run_id = None
    try:
        config = load_config(args.config)
        database_path = resolve_cli_path(args.db, config.paths.database)
        if database_path.name.lower() != "basic_materials.sqlite":
            raise ValueError("Database override filename must be basic_materials.sqlite")
        policy = load_financial_data_policy(config.paths.financial_data_policy)
        manifest = validate_financial_data_manifest(
            config.paths.financial_data_manifest,
            policy,
            config.package_root,
        )
        bundle = read_and_validate_financial_contract(
            policy=policy,
            manifest=manifest,
            universe_path=config.paths.universe_csv,
            historical_membership_path=config.paths.historical_membership_csv,
        )
        conn = connect(database_path, config.runtime.sqlite_timeout_seconds)
        init_db(conn)
        registry = load_source_registry(config.paths.source_registry)
        conn.execute("BEGIN IMMEDIATE")
        upsert_source_registry(conn, registry, utc_now())
        conn.commit()
        run_id = start_run(
            conn,
            stage="stage_4a_financial_contract",
            command="05_load_basic_materials_financial_contract",
            database_path=database_path,
            input_path=manifest.path,
            input_sha256=manifest.checksum,
            input_row_count=len(bundle.profiles) + len(bundle.metrics),
            details={
                "policy_version": policy.version,
                "calibration_eligible": False,
            },
        )
        stats = load_financial_contract(
            conn,
            policy=policy,
            manifest=manifest,
            bundle=bundle,
        )
        details = {**stats.as_dict(), "database_path": str(database_path)}
        finish_run(conn, run_id, succeeded=True, details=details)
        print(
            json.dumps(
                {"succeeded": True, "run_id": run_id, **details},
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except Exception as exc:
        if conn is not None:
            conn.rollback()
        if conn is not None and run_id is not None:
            try:
                finish_run(
                    conn,
                    run_id,
                    succeeded=False,
                    error_message=f"{type(exc).__name__}: {exc}",
                )
            except Exception:
                pass
        print(
            json.dumps(
                {"succeeded": False, "error": f"{type(exc).__name__}: {exc}"},
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

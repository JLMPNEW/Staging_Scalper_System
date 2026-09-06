#!/usr/bin/env python3
"""Ingest immutable SEC metadata and mapped raw facts for Basic Materials."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from basic_materials.core.config import load_config, resolve_cli_path  # noqa: E402
from basic_materials.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from basic_materials.core.financial_ingestion import (  # noqa: E402
    ingest_sec_financials,
    load_financial_ingestion_policy,
    write_sec_ingestion_reports,
)
from basic_materials.core.source_registry import (  # noqa: E402
    load_source_registry,
    upsert_source_registry,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=PACKAGE_ROOT / "config.yaml")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--cache-only", action="store_true")
    parser.add_argument("--allow-partial", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    database = resolve_cli_path(args.db, config.paths.database)
    policy_path = resolve_cli_path(args.policy, config.paths.financial_ingestion_policy)
    policy = load_financial_ingestion_policy(policy_path)
    report_dir = resolve_cli_path(
        args.report_dir,
        config.paths.output_root / "stage4b_financials" / policy.as_of_date / "sec_ingestion",
    )
    conn = connect(database, config.runtime.sqlite_timeout_seconds)
    run_id = ""
    try:
        init_db(conn)
        conn.execute("BEGIN IMMEDIATE")
        upsert_source_registry(conn, load_source_registry(config.paths.source_registry), utc_now())
        conn.commit()
        run_id = start_run(
            conn,
            stage="stage4b_sec_ingestion",
            command="07_ingest_basic_materials_sec_financials.py",
            database_path=database,
            input_path=policy.path,
            input_sha256=policy.checksum,
        )
        stats = ingest_sec_financials(
            conn,
            config=config,
            policy=policy,
            cache_only=args.cache_only,
            allow_partial=args.allow_partial,
        )
        artifacts = write_sec_ingestion_reports(conn, stats=stats, report_dir=report_dir)
        result = {**stats.as_dict(), "artifacts": artifacts}
        finish_run(conn, run_id, succeeded=True, details=result)
        print(json.dumps(result, indent=2, sort_keys=True))
    except Exception as exc:
        if run_id:
            finish_run(
                conn,
                run_id,
                succeeded=False,
                details={"policy_sha256": policy.checksum},
                error_message=f"{type(exc).__name__}: {exc}",
            )
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()

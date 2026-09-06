#!/usr/bin/env python3
"""Sync immutable reporting-currency/USD rates for Basic Materials Stage 4B."""

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
from basic_materials.core.db import connect, finish_run, init_db, start_run  # noqa: E402
from basic_materials.core.financial_fx import sync_financial_fx_rates, write_fx_reports  # noqa: E402
from basic_materials.core.financial_ingestion import load_financial_ingestion_policy  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=PACKAGE_ROOT / "config.yaml")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--snapshot-key")
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--cache-only", action="store_true")
    parser.add_argument("--allow-partial", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    database = resolve_cli_path(args.db, config.paths.database)
    policy = load_financial_ingestion_policy(
        resolve_cli_path(args.policy, config.paths.financial_ingestion_policy)
    )
    report_dir = resolve_cli_path(
        args.report_dir,
        config.paths.output_root / "stage4b_financials" / policy.as_of_date / "fx",
    )
    conn = connect(database, config.runtime.sqlite_timeout_seconds)
    run_id = ""
    try:
        init_db(conn)
        run_id = start_run(
            conn,
            stage="stage4b_fx",
            command="08_sync_basic_materials_fx_rates.py",
            database_path=database,
            input_path=policy.path,
            input_sha256=policy.checksum,
        )
        stats = sync_financial_fx_rates(
            conn,
            config=config,
            policy=policy,
            snapshot_key=args.snapshot_key,
            cache_only=args.cache_only,
            allow_partial=args.allow_partial,
        )
        artifacts = write_fx_reports(stats=stats, report_dir=report_dir)
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

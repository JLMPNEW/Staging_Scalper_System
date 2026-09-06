#!/usr/bin/env python3
"""Load the governed Basic Materials current security-share ratio contract."""

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
from basic_materials.core.security_ratios import (  # noqa: E402
    load_security_ratio_policy,
    load_security_share_ratios,
    write_security_ratio_reports,
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
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    database = resolve_cli_path(args.db, config.paths.database)
    policy = load_security_ratio_policy(
        resolve_cli_path(args.policy, config.paths.security_ratio_policy)
    )
    report_dir = resolve_cli_path(
        args.report_dir,
        config.paths.output_root / "stage4c_financial_remediation" / policy.as_of_date
        / "security_ratios",
    )
    conn = connect(database, config.runtime.sqlite_timeout_seconds)
    run_id = ""
    result: dict[str, object] = {}
    try:
        init_db(conn)
        conn.execute("BEGIN IMMEDIATE")
        upsert_source_registry(conn, load_source_registry(config.paths.source_registry), utc_now())
        conn.commit()
        run_id = start_run(
            conn,
            stage="stage4c_security_share_ratios",
            command="08a_load_basic_materials_security_ratios.py",
            database_path=database,
            input_path=policy.path,
            input_sha256=policy.checksum,
            details={"cache_only": args.cache_only},
        )
        stats = load_security_share_ratios(
            conn,
            config=config,
            policy=policy,
            cache_only=args.cache_only,
        )
        result["security_ratios"] = stats.as_dict()
        result["artifacts"] = write_security_ratio_reports(
            conn,
            stats=stats,
            report_dir=report_dir,
        )
        finish_run(conn, run_id, succeeded=True, details=result)
        print(json.dumps(result, indent=2, sort_keys=True))
    except Exception as exc:
        if run_id:
            finish_run(
                conn,
                run_id,
                succeeded=False,
                details=result,
                error_message=f"{type(exc).__name__}: {exc}",
            )
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()

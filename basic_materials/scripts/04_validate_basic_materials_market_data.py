"""Validate an existing Basic Materials Stage 3 market snapshot read-only."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from basic_materials.core.config import load_config, resolve_cli_path  # noqa: E402
from basic_materials.core.db import connect  # noqa: E402
from basic_materials.core.market_data import (  # noqa: E402
    latest_market_snapshot,
    validate_market_stage,
    write_market_validation_reports,
)
from basic_materials.core.market_data_contract import (  # noqa: E402
    load_market_data_policy,
    validate_market_data_manifest,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Basic Materials config path")
    parser.add_argument("--db", type=Path, help="Dedicated basic_materials.sqlite path")
    parser.add_argument("--as-of", help="Snapshot as-of date; defaults to latest loaded")
    parser.add_argument("--report-dir", type=Path, help="Validation output directory")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    conn = None
    try:
        config = load_config(args.config)
        database_path = resolve_cli_path(args.db, config.paths.database)
        policy = load_market_data_policy(config.paths.market_data_policy)
        manifest = validate_market_data_manifest(
            config.paths.market_data_manifest,
            policy,
            config.package_root,
        )
        conn = connect(database_path, config.runtime.sqlite_timeout_seconds, read_only=True)
        snapshot = latest_market_snapshot(conn, as_of=args.as_of)
        as_of = args.as_of or str(snapshot["extraction_asof_date"])
        report = validate_market_stage(
            conn,
            policy=policy,
            manifest=manifest,
            as_of=as_of,
            snapshot_key=str(snapshot["snapshot_key"]),
        )
        report_dir = resolve_cli_path(
            args.report_dir,
            config.paths.output_root / "stage3" / as_of,
        )
        artifacts = write_market_validation_reports(conn, report, report_dir=report_dir)
        payload = {**report.summary_dict(), "database_path": str(database_path), "artifacts": artifacts}
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if report.passed else 1
    except Exception as exc:
        print(
            json.dumps({"passed": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2),
            file=sys.stderr,
        )
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

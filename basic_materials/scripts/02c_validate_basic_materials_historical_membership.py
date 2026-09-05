"""Validate Stage 2B and publish historical-membership evidence."""

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
from basic_materials.core.historical_membership import (  # noqa: E402
    load_historical_reconciliation_policy,
    read_and_validate_historical_reconciliation,
    validate_historical_reconciliation_database,
    validate_historical_reconciliation_manifest,
    write_historical_reconciliation_reports,
)
from basic_materials.core.universe import load_universe_policy  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Basic Materials config path")
    parser.add_argument("--db", type=Path, help="Dedicated database path; filename must be basic_materials.sqlite")
    parser.add_argument("--report-dir", type=Path, help="Explicit scratch/report directory")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    conn = None
    try:
        config = load_config(args.config)
        policy = load_historical_reconciliation_policy(config.paths.historical_reconciliation_policy)
        manifest = validate_historical_reconciliation_manifest(
            config.paths.historical_reconciliation_manifest,
            policy,
            config.package_root,
        )
        bundle = read_and_validate_historical_reconciliation(
            policy=policy,
            manifest=manifest,
            candidate_policy_path=config.paths.historical_candidate_policy,
            candidate_manifest_path=config.paths.historical_candidate_manifest,
            candidate_path=config.paths.historical_candidates_csv,
        )
        current_policy = load_universe_policy(config.paths.universe_policy)
        database_path = resolve_cli_path(args.db, config.paths.database)
        if database_path.name.lower() != "basic_materials.sqlite":
            raise ValueError("Database override filename must be basic_materials.sqlite")
        report_dir = resolve_cli_path(
            args.report_dir,
            config.paths.output_root / "stage2_historical_membership" / policy.as_of_date,
        )

        conn = connect(database_path, config.runtime.sqlite_timeout_seconds, read_only=True)
        report = validate_historical_reconciliation_database(
            conn,
            policy=policy,
            manifest=manifest,
            bundle=bundle,
            expected_current_rows=current_policy.expected_current_rows,
        )
        artifacts = write_historical_reconciliation_reports(report, bundle=bundle, report_dir=report_dir)
        payload = {**report.summary_dict(), "artifacts": artifacts, "database_path": str(database_path)}
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if report.passed else 2
    except Exception as exc:
        print(json.dumps({"passed": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2), file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

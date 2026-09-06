#!/usr/bin/env python3
"""Validate Basic Materials Stage 4B and publish the QA evidence pack."""

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
from basic_materials.core.db import connect  # noqa: E402
from basic_materials.core.financial_ingestion import load_financial_ingestion_policy  # noqa: E402
from basic_materials.core.financial_validation import (  # noqa: E402
    validate_financial_stage,
    write_financial_validation_reports,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=PACKAGE_ROOT / "config.yaml")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--snapshot-key")
    parser.add_argument("--report-dir", type=Path)
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
        config.paths.output_root / "stage4b_financials" / policy.as_of_date / "validation",
    )
    conn = connect(database, config.runtime.sqlite_timeout_seconds, read_only=True)
    try:
        report = validate_financial_stage(conn, policy=policy, snapshot_key=args.snapshot_key)
        artifacts = write_financial_validation_reports(report, conn=conn, report_dir=report_dir)
        result = {**report.summary_dict(), "artifacts": artifacts}
        print(json.dumps(result, indent=2, sort_keys=True))
        if not report.passed:
            raise SystemExit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()

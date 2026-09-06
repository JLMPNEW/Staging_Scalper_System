"""Validate the loaded Basic Materials Stage 4A financial contract read-only."""

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
from basic_materials.core.financial_data_contract import (  # noqa: E402
    load_financial_data_policy,
    read_and_validate_financial_contract,
    validate_financial_contract_database,
    validate_financial_data_manifest,
    write_financial_contract_reports,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Basic Materials config path")
    parser.add_argument("--db", type=Path, help="Dedicated basic_materials.sqlite path")
    parser.add_argument("--report-dir", type=Path, help="Validation output directory")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    conn = None
    try:
        config = load_config(args.config)
        database_path = resolve_cli_path(args.db, config.paths.database)
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
        conn = connect(
            database_path,
            config.runtime.sqlite_timeout_seconds,
            read_only=True,
        )
        report = validate_financial_contract_database(
            conn,
            policy=policy,
            manifest=manifest,
            bundle=bundle,
        )
        report_dir = resolve_cli_path(
            args.report_dir,
            config.paths.output_root / "stage4_financial_contract" / policy.as_of_date,
        )
        artifacts = write_financial_contract_reports(report, report_dir=report_dir)
        payload = {
            **report.summary_dict(),
            "database_path": str(database_path),
            "artifacts": artifacts,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if report.passed else 2
    except Exception as exc:
        print(
            json.dumps(
                {"passed": False, "error": f"{type(exc).__name__}: {exc}"},
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

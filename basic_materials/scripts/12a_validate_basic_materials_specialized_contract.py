"""Validate Stage 5A definitions, applicability accounting, and source closure read-only."""

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
from basic_materials.core.specialized_contract import (  # noqa: E402
    load_specialized_metric_registry,
    load_specialized_source_policy,
    validate_specialized_contract_database,
    write_specialized_contract_reports,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--allow-open", action="store_true", help="Return zero for a structurally valid but unsealed Stage 5A contract")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    database = resolve_cli_path(args.db, config.paths.database)
    registry = load_specialized_metric_registry(config.paths.specialized_metric_registry)
    source_policy = load_specialized_source_policy(config.paths.specialized_source_policy)
    report_dir = resolve_cli_path(
        args.report_dir,
        config.paths.output_root / "stage5a_specialized_contract" / registry.as_of_date,
    )
    conn = connect(database, config.runtime.sqlite_timeout_seconds, read_only=True)
    try:
        report = validate_specialized_contract_database(conn, registry=registry, source_policy=source_policy)
        artifacts = write_specialized_contract_reports(report, report_dir=report_dir)
        payload = {**report.summary_dict(), "database_path": str(database), "artifacts": artifacts}
        print(json.dumps(payload, indent=2, sort_keys=True))
        if report.stage5a_sealed or (args.allow_open and report.contract_valid):
            return 0
        return 2
    except Exception as exc:
        print(json.dumps({"contract_valid": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2), file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())


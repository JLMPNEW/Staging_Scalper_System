"""Run the Basic Materials Stage 4D no-write 2019-forward PIT feasibility audit."""

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
from basic_materials.core.historical_pit_preflight import (  # noqa: E402
    load_historical_pit_preflight_policy,
    run_historical_pit_preflight,
    write_historical_pit_preflight_reports,
)
from basic_materials.core.specialized_contract import (  # noqa: E402
    load_specialized_metric_registry,
    load_specialized_source_policy,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--allow-blocked", action="store_true", help="Return zero after producing an expected fail-closed blocker report")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    database = resolve_cli_path(args.db, config.paths.database)
    policy = load_historical_pit_preflight_policy(config.paths.historical_pit_preflight_policy)
    registry = load_specialized_metric_registry(config.paths.specialized_metric_registry)
    source_policy = load_specialized_source_policy(config.paths.specialized_source_policy)
    report_dir = resolve_cli_path(
        args.report_dir,
        config.paths.output_root / "stage4d_historical_pit_preflight" / policy.as_of_date,
    )
    conn = connect(database, config.runtime.sqlite_timeout_seconds, read_only=True)
    try:
        report = run_historical_pit_preflight(
            conn,
            database_path=database,
            policy=policy,
            registry=registry,
            source_policy=source_policy,
            historical_candidate_policy_path=config.paths.historical_candidate_policy,
            historical_candidate_manifest_path=config.paths.historical_candidate_manifest,
            historical_candidates_path=config.paths.historical_candidates_csv,
        )
        artifacts = write_historical_pit_preflight_reports(report, report_dir=report_dir)
        payload = {**report.summary_dict(), "artifacts": artifacts}
        print(json.dumps(payload, indent=2, sort_keys=True))
        if report.pit_materialization_allowed or (args.allow_blocked and report.feasibility_passed and report.database_unchanged):
            return 0
        return 2
    except Exception as exc:
        print(json.dumps({"feasibility_passed": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2), file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

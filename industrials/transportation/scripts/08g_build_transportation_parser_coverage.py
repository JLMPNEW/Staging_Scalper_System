#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import (  # noqa: E402
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.transportation.parser_coverage import (  # noqa: E402
    build_cohort_summary,
    build_final_coverage,
    build_metric_summary,
    build_support_coverage,
    load_evidence_stats,
    load_financial_values,
    load_run,
    load_work_stats,
    read_csv,
    read_only_connection,
    write_coverage_artifacts,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build the post-search transportation coverage report for all 90 "
            "final metrics without rebuilding features or calibrating."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--run-id", type=int, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    parser_cfg = family["dedicated_parser"]
    base_dir = config_path.parent
    foundation = resolve_foundation(config_path, args.db)
    scope_path = resolve_path(
        parser_cfg["scope_manifest_csv"],
        base_dir=base_dir,
    )
    support_scope_path = resolve_path(
        parser_cfg["supporting_scope_manifest_csv"],
        base_dir=base_dir,
    )
    output_dir = (
        resolve_path(parser_cfg["output_root"], base_dir=base_dir)
        / str(parser_cfg["source_census_asof_date"])
    )
    with read_only_connection(
        foundation.db_path,
        timeout_sec=foundation.timeout_sec,
    ) as connection:
        run = load_run(connection, run_id=args.run_id)
        evidence = load_evidence_stats(connection, run_id=args.run_id)
        work = load_work_stats(connection, run_id=args.run_id)
        financial = load_financial_values(
            connection,
            asof_date=str(run["asof_date"]),
        )
    final_rows = build_final_coverage(
        run_id=args.run_id,
        scope_rows=read_csv(scope_path),
        evidence=evidence,
        work=work,
        financial_values=financial,
    )
    support_rows = build_support_coverage(
        run_id=args.run_id,
        scope_rows=read_csv(support_scope_path),
        evidence=evidence,
        work=work,
    )
    metric_rows = build_metric_summary(final_rows)
    cohort_rows = build_cohort_summary(final_rows)
    payload = write_coverage_artifacts(
        final_rows=final_rows,
        metric_rows=metric_rows,
        cohort_rows=cohort_rows,
        support_rows=support_rows,
        final_path=(
            output_dir / "transportation_ticker_metric_coverage.csv"
        ),
        metric_path=(
            output_dir / "transportation_metric_coverage_summary.csv"
        ),
        cohort_path=(
            output_dir / "transportation_cohort_metric_coverage.csv"
        ),
        support_path=(
            output_dir / "transportation_support_metric_coverage.csv"
        ),
        manifest_path=(
            output_dir / "transportation_parser_coverage_manifest.json"
        ),
        run=run,
        scope_path=scope_path,
        support_scope_path=support_scope_path,
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["acceptance"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.db import connect, init_db  # noqa: E402
from technology.core.dedicated_parser.db_contract import (  # noqa: E402
    ensure_technology_parser_schema,
)
from technology.software_infrastructure.software_parser_hydration import (  # noqa: E402
    atomic_csv,
    atomic_json,
)
from technology.software_infrastructure.software_specialized_metrics import (  # noqa: E402
    PlausibilityThresholds,
    adjudicated_facts,
    build_attrition_report,
    build_pit_features,
    load_policy,
    manifest_payload,
    upsert_facts,
    validate_pit_panel,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_POLICY = (
    PROJECT_ROOT
    / "dedicated_parser"
    / "golden_corpus"
    / "software_metrics_policy_v3.json"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "output"
    / "technology_reports"
    / "software_infrastructure"
    / "specialized_metrics"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Materialize sealed software specialized metrics and build a "
            "survivorship-correct, acceptance-datetime PIT research panel."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--start-date", default="2018-01-01")
    parser.add_argument("--end-date", default=date.today().isoformat())
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and validate reports without changing research tables.",
    )
    return parser.parse_args()


def _iso(value: str, *, field: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc


def main() -> int:
    args = parse_args()
    start_date = _iso(args.start_date, field="start-date")
    end_date = _iso(args.end_date, field="end-date")
    if start_date > end_date:
        raise ValueError("start-date cannot be after end-date")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(
            cfg_get(config, "paths.database_path"),
            base_dir=config_path.parent,
        )
    )
    timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))
    policy_path = args.policy.expanduser().resolve()
    policy = load_policy(policy_path)
    write_database = not args.dry_run
    with connect(db_path, timeout_sec=timeout_sec) as conn:
        init_db(conn)
        with conn:
            ensure_technology_parser_schema(conn)
        facts, reconciliation = adjudicated_facts(
            conn,
            policy=policy,
            thresholds=PlausibilityThresholds(),
        )
        if write_database:
            with conn:
                fact_count = upsert_facts(conn, facts=facts)
                panel, coverage, feature_count = build_pit_features(
                    conn,
                    start_date=start_date,
                    end_date=end_date,
                    write_database=True,
                )
        else:
            # Exercise the exact write/read path in a rollback-only
            # transaction. Otherwise a first dry run cannot see the sealed
            # facts it is supposed to validate and reports an empty panel.
            conn.execute("BEGIN")
            try:
                fact_count = upsert_facts(conn, facts=facts)
                panel, coverage, feature_count = build_pit_features(
                    conn,
                    start_date=start_date,
                    end_date=end_date,
                    write_database=True,
                )
            finally:
                conn.rollback()
    errors = validate_pit_panel(panel)
    attrition = build_attrition_report(
        policy=policy,
        reconciliation_rows=reconciliation,
        panel_rows=panel,
    )
    output_dir = args.output_dir.expanduser().resolve()
    atomic_csv(
        output_dir / "software_specialized_metric_reconciliation.csv",
        reconciliation,
    )
    atomic_csv(
        output_dir / "software_specialized_metric_pit_panel.csv",
        panel,
    )
    atomic_csv(
        output_dir / "software_specialized_metric_attrition.csv",
        attrition,
    )
    atomic_csv(
        output_dir / "software_specialized_metric_coverage.csv",
        coverage,
    )
    manifest = manifest_payload(
        start_date=start_date,
        end_date=end_date,
        policy_path=policy_path,
        fact_count=fact_count,
        feature_count=feature_count,
        panel_rows=panel,
        coverage_rows=coverage,
        reconciliation_rows=reconciliation,
        attrition_rows=attrition,
        errors=errors,
        write_database=write_database,
    )
    atomic_json(
        output_dir / "software_specialized_metric_manifest.json",
        manifest,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

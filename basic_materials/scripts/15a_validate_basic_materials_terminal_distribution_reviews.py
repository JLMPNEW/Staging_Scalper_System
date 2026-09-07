"""Validate the applied F0.2 terminal-distribution overlay without database writes."""

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
from basic_materials.core.terminal_distribution_contract import (  # noqa: E402
    load_terminal_distribution_policy,
    read_terminal_distribution_reviews,
    validate_terminal_distribution_manifest,
)
from basic_materials.core.terminal_distribution_reviews import (  # noqa: E402
    hydrate_terminal_distribution_evidence,
    validate_terminal_distribution_database,
    write_terminal_distribution_reports,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--as-of")
    parser.add_argument("--snapshot-key")
    parser.add_argument("--report-dir", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    conn = None
    try:
        config = load_config(args.config)
        database = resolve_cli_path(args.db, config.paths.database)
        policy = load_terminal_distribution_policy(
            config.paths.terminal_distribution_policy
        )
        manifest = validate_terminal_distribution_manifest(
            config.paths.terminal_distribution_manifest,
            policy=policy,
            package_root=config.package_root,
        )
        reviews = read_terminal_distribution_reviews(
            config.paths.terminal_distribution_reviews_csv,
            policy=policy,
            manifest=manifest,
        )
        evidence = hydrate_terminal_distribution_evidence(
            config=config,
            policy=policy,
            manifest=manifest,
            reviews=reviews,
            cache_only=True,
        )
        market_policy = load_market_data_policy(config.paths.market_data_policy)
        market_manifest = validate_market_data_manifest(
            config.paths.market_data_manifest,
            market_policy,
            config.package_root,
        )
        conn = connect(
            database,
            config.runtime.sqlite_timeout_seconds,
            read_only=True,
        )
        snapshot = (
            conn.execute(
                """
                SELECT * FROM fact_market_provider_snapshot
                WHERE snapshot_key = ? AND status = 'loaded'
                """,
                (args.snapshot_key,),
            ).fetchone()
            if args.snapshot_key
            else latest_market_snapshot(conn, as_of=args.as_of)
        )
        if snapshot is None:
            raise RuntimeError("Requested loaded market snapshot does not exist")
        snapshot_key = str(snapshot["snapshot_key"])
        calculation_asof = args.as_of or str(snapshot["extraction_asof_date"])
        validation = validate_terminal_distribution_database(
            conn,
            policy=policy,
            manifest=manifest,
            reviews=reviews,
            calculation_asof_date=calculation_asof,
            require_reconciled=True,
        )
        market_validation = validate_market_stage(
            conn,
            policy=market_policy,
            manifest=market_manifest,
            as_of=calculation_asof,
            snapshot_key=snapshot_key,
        )
        if not market_validation.passed:
            raise RuntimeError("Overlay-aware Stage 3 validation failed")
        report_dir = resolve_cli_path(
            args.report_dir,
            config.paths.output_root
            / "f0_2_terminal_distributions"
            / policy.as_of_date
            / "validation",
        )
        artifacts = write_terminal_distribution_reports(
            conn,
            policy=policy,
            manifest=manifest,
            evidence=evidence,
            load_stats={"validation_only": True},
            terminal_stats={
                "resolved_terminal_events": (
                    market_validation.resolved_terminal_events
                ),
                "unresolved_terminal_events": (
                    market_validation.unresolved_terminal_events
                ),
                "calibration_activated": False,
            },
            validation=validation,
            calculation_asof_date=calculation_asof,
            report_dir=report_dir,
        )
        market_artifacts = write_market_validation_reports(
            conn,
            market_validation,
            report_dir=report_dir / "stage3_validation",
        )
        print(
            json.dumps(
                {
                    "passed": True,
                    "database_path": str(database),
                    "snapshot_key": snapshot_key,
                    "calculation_asof_date": calculation_asof,
                    "validation": validation,
                    "market_validation": market_validation.summary_dict(),
                    "artifacts": artifacts,
                    "market_artifacts": market_artifacts,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
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

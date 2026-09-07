"""Apply the sealed F0.2 terminal-distribution overlay to an isolated database."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from basic_materials.core.config import load_config, resolve_cli_path  # noqa: E402
from basic_materials.core.db import (  # noqa: E402
    connect,
    finish_run,
    init_db,
    start_run,
    utc_now,
)
from basic_materials.core.market_data import (  # noqa: E402
    latest_market_snapshot,
    validate_market_stage,
    write_market_validation_reports,
)
from basic_materials.core.market_data_contract import (  # noqa: E402
    load_market_data_policy,
    validate_market_data_manifest,
)
from basic_materials.core.source_registry import (  # noqa: E402
    load_source_registry,
    upsert_source_registry,
)
from basic_materials.core.terminal_distribution_contract import (  # noqa: E402
    TerminalDistributionContractError,
    load_terminal_distribution_policy,
    read_terminal_distribution_reviews,
    validate_terminal_distribution_manifest,
)
from basic_materials.core.terminal_distribution_reviews import (  # noqa: E402
    apply_terminal_distribution_reviews,
    hydrate_terminal_distribution_evidence,
    validate_terminal_distribution_database,
    write_terminal_distribution_reports,
)
from basic_materials.core.terminal_returns import reconcile_terminal_returns  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--as-of", help="Terminal calculation date; defaults to latest loaded snapshot")
    parser.add_argument("--snapshot-key", help="Exact loaded market snapshot; defaults to latest")
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--cache-only", action="store_true")
    parser.add_argument(
        "--allow-live-database",
        action="store_true",
        help="Explicitly authorize the configured live database (not recommended before F0 closes)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    conn = None
    run_id = None
    try:
        config = load_config(args.config)
        database = resolve_cli_path(args.db, config.paths.database)
        if database.name.lower() != "basic_materials.sqlite":
            raise TerminalDistributionContractError(
                "Database override filename must be basic_materials.sqlite"
            )
        if (
            database.resolve() == config.paths.database.resolve()
            and not args.allow_live_database
        ):
            raise TerminalDistributionContractError(
                "Refusing the configured live database without --allow-live-database"
            )
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
            cache_only=args.cache_only,
        )
        market_policy = load_market_data_policy(config.paths.market_data_policy)
        market_manifest = validate_market_data_manifest(
            config.paths.market_data_manifest,
            market_policy,
            config.package_root,
        )
        if (
            market_manifest.checksum
            != policy.payload["base_contract"]["market_manifest_sha256"]
        ):
            raise TerminalDistributionContractError(
                "Loaded Stage 3 manifest differs from the F0.2 base-contract seal"
            )

        conn = connect(database, config.runtime.sqlite_timeout_seconds)
        migration = init_db(conn)
        registry = load_source_registry(config.paths.source_registry)
        conn.execute("BEGIN IMMEDIATE")
        upsert_source_registry(conn, registry, utc_now())
        conn.commit()
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
            raise TerminalDistributionContractError(
                "Requested loaded market snapshot does not exist"
            )
        snapshot_key = str(snapshot["snapshot_key"])
        calculation_asof = args.as_of or str(snapshot["extraction_asof_date"])
        if str(snapshot["extraction_asof_date"]) > calculation_asof:
            raise TerminalDistributionContractError(
                "Market snapshot is later than the terminal calculation cutoff"
            )
        report_dir = resolve_cli_path(
            args.report_dir,
            config.paths.output_root
            / "f0_2_terminal_distributions"
            / policy.as_of_date,
        )
        run_id = start_run(
            conn,
            stage="f0_2_terminal_distribution_closure",
            command="15_apply_basic_materials_terminal_distribution_reviews",
            database_path=database,
            input_path=manifest.path,
            input_sha256=manifest.checksum,
            input_row_count=len(reviews),
            details={
                "policy_version": policy.version,
                "snapshot_key": snapshot_key,
                "calculation_asof_date": calculation_asof,
                "cache_only": args.cache_only,
            },
        )
        load_stats = apply_terminal_distribution_reviews(
            conn,
            policy=policy,
            manifest=manifest,
            reviews=reviews,
            evidence=evidence,
        )
        terminal_stats = reconcile_terminal_returns(
            conn,
            policy=market_policy,
            as_of=calculation_asof,
            snapshot_key=snapshot_key,
        )
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
            raise TerminalDistributionContractError(
                "Overlay-aware Stage 3 validation failed"
            )
        artifacts = write_terminal_distribution_reports(
            conn,
            policy=policy,
            manifest=manifest,
            evidence=evidence,
            load_stats=load_stats.as_dict(),
            terminal_stats=terminal_stats,
            validation=validation,
            calculation_asof_date=calculation_asof,
            report_dir=report_dir,
        )
        market_artifacts = write_market_validation_reports(
            conn,
            market_validation,
            report_dir=report_dir / "stage3_validation",
        )
        details = {
            "database_path": str(database),
            "migration": migration,
            "snapshot_key": snapshot_key,
            "calculation_asof_date": calculation_asof,
            "load": load_stats.as_dict(),
            "terminal_reconciliation": terminal_stats,
            "validation": validation,
            "market_validation": market_validation.summary_dict(),
            "artifacts": artifacts,
            "market_artifacts": market_artifacts,
        }
        finish_run(conn, run_id, succeeded=True, details=details)
        run_id = None
        print(json.dumps({"succeeded": True, **details}, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        if conn is not None and run_id is not None:
            try:
                finish_run(
                    conn,
                    run_id,
                    succeeded=False,
                    error_message=f"{type(exc).__name__}: {exc}",
                )
            except Exception:
                pass
        print(
            json.dumps(
                {"succeeded": False, "error": f"{type(exc).__name__}: {exc}"},
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

"""Run the complete self-contained Basic Materials Stage 3 market pipeline."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from basic_materials.core.config import load_config, resolve_cli_path  # noqa: E402
from basic_materials.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from basic_materials.core.market_data import (  # noqa: E402
    build_market_coverage,
    build_market_features,
    validate_market_stage,
    write_market_validation_reports,
)
from basic_materials.core.market_data_contract import (  # noqa: E402
    load_market_data_contract,
    load_market_data_policy,
    read_and_validate_market_contract,
    validate_market_data_manifest,
)
from basic_materials.core.norgate_prices import load_norgate_market_data  # noqa: E402
from basic_materials.core.source_registry import (  # noqa: E402
    load_source_registry,
    upsert_source_registry,
)
from basic_materials.core.terminal_returns import reconcile_terminal_returns  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Basic Materials config path")
    parser.add_argument("--db", type=Path, help="Dedicated basic_materials.sqlite path")
    parser.add_argument("--as-of", default=date.today().isoformat(), help="ISO calculation date")
    parser.add_argument("--report-dir", type=Path, help="Stage 3 report output directory")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    conn = None
    run_id = None
    try:
        date.fromisoformat(args.as_of)
        config = load_config(args.config)
        database_path = resolve_cli_path(args.db, config.paths.database)
        if database_path.name.lower() != "basic_materials.sqlite":
            raise ValueError("Database override filename must be basic_materials.sqlite")
        policy = load_market_data_policy(config.paths.market_data_policy)
        manifest = validate_market_data_manifest(
            config.paths.market_data_manifest,
            policy,
            config.package_root,
        )
        bundle = read_and_validate_market_contract(
            policy=policy,
            manifest=manifest,
            universe_path=config.paths.universe_csv,
            historical_membership_path=config.paths.historical_membership_csv,
            terminal_events_path=config.paths.terminal_events_csv,
        )
        conn = connect(database_path, config.runtime.sqlite_timeout_seconds)
        init_db(conn)
        registry = load_source_registry(config.paths.source_registry)
        conn.execute("BEGIN IMMEDIATE")
        upsert_source_registry(conn, registry, utc_now())
        conn.commit()
        contract_stats = load_market_data_contract(
            conn,
            policy=policy,
            manifest=manifest,
            bundle=bundle,
        )
        run_id = start_run(
            conn,
            stage="stage_3_adjusted_market_data",
            command="03_run_basic_materials_market_stage",
            database_path=database_path,
            input_path=config.paths.market_data_manifest,
            input_sha256=manifest.checksum,
            input_row_count=len(bundle.market_instruments) + len(bundle.terminal_return_rules),
            details={"as_of_date": args.as_of, "policy_version": policy.policy_version},
        )
        import norgatedata as provider

        market_stats = load_norgate_market_data(
            conn,
            policy=policy,
            manifest=manifest,
            provider=provider,
            cache_root=config.paths.cache_root,
            as_of=args.as_of,
        )
        snapshot_key = str(market_stats["snapshot_key"])
        coverage_stats = build_market_coverage(
            conn,
            policy=policy,
            as_of=args.as_of,
            snapshot_key=snapshot_key,
        )
        feature_stats = build_market_features(
            conn,
            policy=policy,
            as_of=args.as_of,
            snapshot_key=snapshot_key,
        )
        terminal_stats = reconcile_terminal_returns(
            conn,
            policy=policy,
            as_of=args.as_of,
            snapshot_key=snapshot_key,
        )
        report = validate_market_stage(
            conn,
            policy=policy,
            manifest=manifest,
            as_of=args.as_of,
            snapshot_key=snapshot_key,
        )
        report_dir = resolve_cli_path(
            args.report_dir,
            config.paths.output_root / "stage3" / args.as_of,
        )
        artifacts = write_market_validation_reports(conn, report, report_dir=report_dir)
        details = {
            "database_path": str(database_path),
            "as_of_date": args.as_of,
            "contract": contract_stats.as_dict(),
            "market_data": market_stats,
            "coverage": coverage_stats,
            "features": feature_stats,
            "terminal_returns": terminal_stats,
            "validation": report.summary_dict(),
            "artifacts": artifacts,
        }
        finish_run(conn, run_id, succeeded=report.passed, details=details)
        run_id = None
        print(json.dumps({"succeeded": report.passed, **details}, indent=2, sort_keys=True))
        return 0 if report.passed else 1
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
            json.dumps({"succeeded": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2),
            file=sys.stderr,
        )
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

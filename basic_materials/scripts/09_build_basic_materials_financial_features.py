#!/usr/bin/env python3
"""Normalize Stage 4B facts and publish Basic Materials common financial features."""

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
from basic_materials.core.db import connect, finish_run, init_db, start_run  # noqa: E402
from basic_materials.core.financial_fx import latest_financial_snapshot  # noqa: E402
from basic_materials.core.financial_ingestion import load_financial_ingestion_policy  # noqa: E402
from basic_materials.core.financial_normalization import (  # noqa: E402
    build_financial_features,
    normalize_financial_facts,
    write_financial_feature_reports,
    write_normalization_report,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=PACKAGE_ROOT / "config.yaml")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--snapshot-key")
    parser.add_argument("--tickers", default="")
    parser.add_argument("--pilot-only", action="store_true")
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
        config.paths.output_root / "stage4b_financials" / policy.as_of_date,
    )
    conn = connect(database, config.runtime.sqlite_timeout_seconds)
    run_id = ""
    try:
        init_db(conn)
        snapshot_key = args.snapshot_key or str(
            latest_financial_snapshot(conn, as_of_date=policy.as_of_date)["snapshot_key"]
        )
        cli_tickers = {value.strip().upper() for value in args.tickers.split(",") if value.strip()}
        tickers = (
            cli_tickers
            or (
                {str(value) for value in policy.payload["snapshot"]["representative_pilot_tickers"]}
                if args.pilot_only
                else set()
            )
        )
        run_id = start_run(
            conn,
            stage="stage4b_financial_features",
            command="09_build_basic_materials_financial_features.py",
            database_path=database,
            input_path=policy.path,
            input_sha256=policy.checksum,
            details={"snapshot_key": snapshot_key, "tickers": sorted(tickers), "pilot_only": args.pilot_only},
        )
        normalization = normalize_financial_facts(
            conn,
            policy=policy,
            snapshot_key=snapshot_key,
            tickers=tickers or None,
        )
        normalization_name = (
            "representative_normalization_pilot_summary.json"
            if args.pilot_only
            else "financial_normalization_summary.json"
        )
        normalization_artifact = write_normalization_report(
            stats=normalization,
            report_dir=report_dir / "normalization",
            filename=normalization_name,
        )
        result: dict[str, object] = {
            "normalization": normalization.as_dict(),
            "normalization_artifact": normalization_artifact,
        }
        if not args.pilot_only and not tickers:
            features = build_financial_features(conn, policy=policy, snapshot_key=snapshot_key)
            result["features"] = features.as_dict()
            result["feature_artifacts"] = write_financial_feature_reports(
                conn,
                stats=features,
                report_dir=report_dir / "features",
            )
        finish_run(conn, run_id, succeeded=True, details=result)
        print(json.dumps(result, indent=2, sort_keys=True))
    except Exception as exc:
        if run_id:
            finish_run(
                conn,
                run_id,
                succeeded=False,
                details={"policy_sha256": policy.checksum},
                error_message=f"{type(exc).__name__}: {exc}",
            )
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()

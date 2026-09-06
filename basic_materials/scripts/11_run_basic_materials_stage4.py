#!/usr/bin/env python3
"""Run the independent Basic Materials Stage 4C-remediated pipeline end to end."""

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
from basic_materials.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from basic_materials.core.financial_fx import sync_financial_fx_rates, write_fx_reports  # noqa: E402
from basic_materials.core.financial_ingestion import (  # noqa: E402
    ingest_sec_financials,
    load_financial_ingestion_policy,
    write_sec_ingestion_reports,
)
from basic_materials.core.financial_normalization import (  # noqa: E402
    build_financial_features,
    normalize_financial_facts,
    write_financial_feature_reports,
    write_normalization_report,
)
from basic_materials.core.financial_validation import (  # noqa: E402
    validate_financial_stage,
    write_financial_validation_reports,
)
from basic_materials.core.source_registry import (  # noqa: E402
    load_source_registry,
    upsert_source_registry,
)
from basic_materials.core.security_ratios import (  # noqa: E402
    load_security_ratio_policy,
    load_security_share_ratios,
    write_security_ratio_reports,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=PACKAGE_ROOT / "config.yaml")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--report-root", type=Path)
    parser.add_argument("--cache-only", action="store_true")
    parser.add_argument("--allow-partial", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    database = resolve_cli_path(args.db, config.paths.database)
    policy = load_financial_ingestion_policy(
        resolve_cli_path(args.policy, config.paths.financial_ingestion_policy)
    )
    ratio_policy = load_security_ratio_policy(config.paths.security_ratio_policy)
    expected_ratio_sha = str(
        policy.payload["security_ratio_contract"]["policy_sha256"]
    )
    if ratio_policy.checksum != expected_ratio_sha:
        raise RuntimeError("Security-ratio policy does not match the financial policy")
    if ratio_policy.as_of_date != policy.as_of_date:
        raise RuntimeError("Security-ratio and financial policy as-of dates differ")
    report_root = resolve_cli_path(
        args.report_root,
        config.paths.output_root / "stage4c_financial_remediation" / policy.as_of_date,
    )
    conn = connect(database, config.runtime.sqlite_timeout_seconds)
    run_id = ""
    result: dict[str, object] = {}
    try:
        init_db(conn)
        conn.execute("BEGIN IMMEDIATE")
        upsert_source_registry(conn, load_source_registry(config.paths.source_registry), utc_now())
        conn.commit()
        run_id = start_run(
            conn,
            stage="stage4c_financial_remediation",
            command="11_run_basic_materials_stage4.py",
            database_path=database,
            input_path=policy.path,
            input_sha256=policy.checksum,
            details={
                "cache_only": args.cache_only,
                "allow_partial": args.allow_partial,
                "promotion_state": "shadow_monitor",
                "security_ratio_policy_sha256": ratio_policy.checksum,
            },
        )
        ratios = load_security_share_ratios(
            conn,
            config=config,
            policy=ratio_policy,
            cache_only=args.cache_only,
        )
        result["security_ratios"] = ratios.as_dict()
        result["security_ratio_artifacts"] = write_security_ratio_reports(
            conn,
            stats=ratios,
            report_dir=report_root / "security_ratios",
        )
        sec = ingest_sec_financials(
            conn,
            config=config,
            policy=policy,
            cache_only=args.cache_only,
            allow_partial=args.allow_partial,
        )
        result["sec_ingestion"] = sec.as_dict()
        result["sec_artifacts"] = write_sec_ingestion_reports(
            conn,
            stats=sec,
            report_dir=report_root / "sec_ingestion",
        )
        pilot_tickers = tuple(str(value) for value in policy.payload["snapshot"]["representative_pilot_tickers"])
        pilot = normalize_financial_facts(
            conn,
            policy=policy,
            snapshot_key=sec.snapshot_key,
            tickers=pilot_tickers,
        )
        result["normalization_pilot"] = pilot.as_dict()
        result["normalization_pilot_artifact"] = write_normalization_report(
            stats=pilot,
            report_dir=report_root / "normalization",
            filename="representative_normalization_pilot_summary.json",
        )
        fx = sync_financial_fx_rates(
            conn,
            config=config,
            policy=policy,
            snapshot_key=sec.snapshot_key,
            cache_only=args.cache_only,
            allow_partial=args.allow_partial,
        )
        result["fx"] = fx.as_dict()
        result["fx_artifacts"] = write_fx_reports(stats=fx, report_dir=report_root / "fx")
        normalization = normalize_financial_facts(
            conn,
            policy=policy,
            snapshot_key=sec.snapshot_key,
        )
        result["normalization"] = normalization.as_dict()
        result["normalization_artifact"] = write_normalization_report(
            stats=normalization,
            report_dir=report_root / "normalization",
        )
        features = build_financial_features(
            conn,
            policy=policy,
            snapshot_key=sec.snapshot_key,
        )
        result["features"] = features.as_dict()
        result["feature_artifacts"] = write_financial_feature_reports(
            conn,
            stats=features,
            report_dir=report_root / "features",
        )
        validation = validate_financial_stage(
            conn,
            policy=policy,
            snapshot_key=sec.snapshot_key,
        )
        result["validation"] = validation.summary_dict()
        result["validation_artifacts"] = write_financial_validation_reports(
            validation,
            conn=conn,
            report_dir=report_root / "validation",
        )
        if not validation.passed:
            raise RuntimeError(
                "Stage 4C validation failed: "
                + ",".join(
                    item.issue_code for item in validation.issues if item.severity == "error"
                )
            )
        finish_run(conn, run_id, succeeded=True, details=result)
        print(json.dumps(result, indent=2, sort_keys=True))
    except Exception as exc:
        if run_id:
            finish_run(
                conn,
                run_id,
                succeeded=False,
                details=result,
                error_message=f"{type(exc).__name__}: {exc}",
            )
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()

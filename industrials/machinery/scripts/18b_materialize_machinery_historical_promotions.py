#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import runpy
import sys
from pathlib import Path
from typing import Any, Callable


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.adapters import load_registry  # noqa: E402
from industrials.core.config import (  # noqa: E402
    cfg_get,
    load_yaml,
    resolve_path,
)
from industrials.core.db import connect  # noqa: E402
from industrials.core.policy_loader import load_eligibility_policy  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.machinery.build_contract import (  # noqa: E402
    HISTORICAL_BUILD_METADATA_FILENAME,
    historical_build_metadata,
)
from industrials.machinery.financial_contract import (  # noqa: E402
    required_metric_names,
)
from industrials.machinery.historical_coverage import (  # noqa: E402
    build_combined_historical_coverage,
    load_validated_sidecar,
)
from industrials.machinery.historical_promotion_materializer import (  # noqa: E402
    RestoredFeatureState,
    affected_partition_map,
    compact_restored_features,
    restore_validated_sidecar_features,
)
from industrials.machinery.historical_promotion_preflight import (  # noqa: E402
    HistoricalDepthThresholds,
    run_historical_promotion_preflight,
)
from industrials.machinery.scoring import (  # noqa: E402
    build_scoring_feature_rows,
    finalize_rank_rows,
    publish_dashboard,
    survivorship_sidecar,
    write_json_atomic,
)
from portfolio_layer.scores.adapters import run_adapter  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_PREFLIGHT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "machinery"
    / "historical_backfill"
    / "preflight"
    / "machinery_historical_preflight_summary.json"
)
ADAPTER = (
    "industrials.machinery.dedicated_parser_adapter:"
    "extract_metric_evidence"
)
REPORT_FIELDS = [
    "asof_date",
    "status",
    "affected_tickers",
    "row_count",
    "rank_ready_count",
    "portfolio_adapter_row_count",
    "preflight_fingerprint",
    "error",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Materialize only preflight-approved machinery historical "
            "partitions after dedicated-parser production promotions."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument(
        "--preflight-summary",
        type=Path,
        default=DEFAULT_PREFLIGHT,
    )
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--max-dates", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--skip-all-date-adapter-validation",
        action="store_true",
    )
    return parser.parse_args(argv)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _load_preflight(
    path: Path,
) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        summary = json.load(handle)
    if summary.get("acceptance") != "PASS":
        raise ValueError("Promotion preflight did not pass")
    if summary.get("decision") != "GO_AFFECTED_PARTITIONS_ONLY":
        raise ValueError(
            f"Promotion preflight decision is {summary.get('decision')}"
        )
    if summary.get("full_rebuild_required") is not False:
        raise ValueError("Promotion preflight does not authorize a targeted run")
    if not str(summary.get("input_fingerprint") or ""):
        raise ValueError("Promotion preflight has no input fingerprint")
    return summary


def _fresh_preflight(
    *,
    conn: Any,
    summary: dict[str, Any],
    config: dict[str, Any],
    dashboard_root: Path,
) -> dict[str, object]:
    registry = load_registry(ADAPTER)
    output_root = dashboard_root.parent
    threshold_values = summary["thresholds"]
    return run_historical_promotion_preflight(
        conn,
        promotion_ids=tuple(
            int(item) for item in summary["selected_promotion_ids"]
        ),
        registry=registry,
        source_id=str(
            cfg_get(
                config,
                "dedicated_parser.production_source_id",
                "dedicated_parser_production",
            )
        ),
        current_coverage_csv=(
            output_root
            / "stage4"
            / "machinery_financial_metric_coverage.csv"
        ),
        historical_summary_json=(
            output_root
            / "historical_backfill"
            / "machinery_combined_historical_coverage.json"
        ),
        dashboard_root=dashboard_root,
        thresholds=HistoricalDepthThresholds(
            minimum_total_observations=int(
                threshold_values["minimum_total_observations"]
            ),
            minimum_qualified_dates=int(
                threshold_values["minimum_qualified_dates"]
            ),
            minimum_qualified_years=int(
                threshold_values["minimum_qualified_years"]
            ),
            minimum_delisted_tickers=int(
                threshold_values["minimum_delisted_tickers"]
            ),
        ),
    )


def _load_financial_main() -> Callable[[], object]:
    script = (
        PROJECT_ROOT
        / "industrials"
        / "scripts"
        / "08_build_industrials_financial_features.py"
    )
    namespace = runpy.run_path(
        str(script),
        run_name="_machinery_promotion_materializer",
    )
    stage_main = namespace.get("main")
    if not callable(stage_main):
        raise RuntimeError("Shared financial builder does not expose main()")
    return stage_main


def _run_financial_stage(
    stage_main: Callable[[], object],
    *,
    config_path: Path,
    db_path: Path,
    asof_date: str,
    tickers: tuple[str, ...],
    scratch_root: Path,
) -> None:
    previous_argv = sys.argv
    previous_fast_init = os.environ.get("INDUSTRIALS_FAST_INIT")
    previous_append = os.environ.get("INDUSTRIALS_HISTORICAL_APPEND")
    previous_logging_disable = logging.root.manager.disable
    try:
        sys.argv = [
            "08_build_industrials_financial_features.py",
            "--config",
            str(config_path),
            "--db",
            str(db_path),
            "--model-family",
            "machinery",
            "--asof",
            asof_date,
            "--tickers",
            ",".join(tickers),
            "--include-historical",
            "--output-csv",
            str(scratch_root / "financial_feature_coverage.csv"),
            "--availability-output-csv",
            str(scratch_root / "financial_metric_availability.csv"),
            "--suppress-data-quality-issues",
        ]
        os.environ["INDUSTRIALS_FAST_INIT"] = "1"
        os.environ["INDUSTRIALS_HISTORICAL_APPEND"] = "1"
        logging.disable(logging.INFO)
        result = stage_main()
        if isinstance(result, int) and result != 0:
            raise RuntimeError(
                f"Financial stage returned exit code {result}"
            )
    finally:
        logging.disable(previous_logging_disable)
        sys.argv = previous_argv
        if previous_fast_init is None:
            os.environ.pop("INDUSTRIALS_FAST_INIT", None)
        else:
            os.environ["INDUSTRIALS_FAST_INIT"] = previous_fast_init
        if previous_append is None:
            os.environ.pop("INDUSTRIALS_HISTORICAL_APPEND", None)
        else:
            os.environ["INDUSTRIALS_HISTORICAL_APPEND"] = previous_append


def _validate_portfolio(
    sector_output_root: Path,
    *,
    asof_date: str,
) -> int:
    result = run_adapter(
        {
            "model_family": "machinery",
            "adapter": "industrial_family",
            "file_mode": "dated",
            "file_path": (
                "industrials/machinery/dashboard/{yyyy-mm-dd}/"
                "machinery_final_rank_table.csv"
            ),
            "sector": "Industrials",
            "industry": "Machinery",
            "industry_aggregate": "Machinery",
            "require_oos_score_valid": True,
        },
        sector_output_root,
        asof_date,
    )
    if not result.rows or result.source_asof_date != asof_date:
        raise ValueError(f"Portfolio adapter failed for {asof_date}")
    if any(row.investable_eligible for row in result.rows):
        raise ValueError(f"Shadow rows became investable for {asof_date}")
    if any(row.oos_score_valid_flag for row in result.rows):
        raise ValueError(f"Shadow rows became OOS-valid for {asof_date}")
    return len(result.rows)


def _metadata_matches(
    output_dir: Path,
    *,
    fingerprint: str,
) -> bool:
    path = output_dir / HISTORICAL_BUILD_METADATA_FILENAME
    if not path.exists():
        return False
    metadata = json.loads(path.read_text(encoding="utf-8"))
    return (
        metadata.get("acceptance") == "PASS"
        and metadata.get("promotion_preflight_fingerprint") == fingerprint
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = (
        args.db.expanduser().resolve()
        if args.db is not None
        else resolve_path(
            cfg_get(config, "paths.database_path"),
            base_dir=config_path.parent,
        )
    )
    dashboard_root = resolve_path(
        cfg_get(config, "machinery_scoring.dashboard_root"),
        base_dir=config_path.parent,
    )
    output_root = dashboard_root.parent
    historical_root = output_root / "historical_backfill"
    scratch_root = historical_root / "_promotion_scratch"
    scratch_root.mkdir(parents=True, exist_ok=True)
    summary_path = args.preflight_summary.expanduser().resolve()
    preflight_summary = _load_preflight(summary_path)
    fingerprint = str(preflight_summary["input_fingerprint"])
    with connect(db_path) as conn:
        fresh = _fresh_preflight(
            conn=conn,
            summary=preflight_summary,
            config=config,
            dashboard_root=dashboard_root,
        )
    fresh_summary = fresh["summary"]
    if not isinstance(fresh_summary, dict):
        raise TypeError("Fresh preflight summary is invalid")
    if fresh_summary.get("input_fingerprint") != fingerprint:
        raise ValueError(
            "Promotion preflight is stale; rerun 18a before materialization"
        )
    partition_path = Path(
        str(preflight_summary["affected_partitions_csv"])
    )
    partitions = affected_partition_map(_read_csv(partition_path))
    selected_dates = [
        asof_date
        for asof_date in sorted(partitions)
        if (not args.start_date or asof_date >= args.start_date)
        and (not args.end_date or asof_date <= args.end_date)
    ]
    if args.max_dates > 0:
        selected_dates = selected_dates[: args.max_dates]
    if not selected_dates:
        raise ValueError("No affected partitions selected")

    weights = cfg_get(
        config,
        "machinery_scoring.component_weights",
        {},
    )
    if not isinstance(weights, dict):
        raise ValueError("machinery scoring component_weights must be a mapping")
    policy_lock_date = str(
        cfg_get(
            config,
            "machinery_scoring.historical_policy_lock_date",
            "",
        )
    )
    policies = load_eligibility_policy(
        resolve_path(
            cfg_get(
                config,
                "scoring_policy.families.machinery.eligibility_policy_csv",
            ),
            base_dir=config_path.parent,
        ),
        asof=policy_lock_date,
    )
    market_sources = tuple(
        source
        for source in (
            str(
                cfg_get(
                    config,
                    "market_data_policy.scoring_primary_source",
                    "",
                )
            ),
            *(
                str(item)
                for item in cfg_get(
                    config,
                    "market_data_policy.scoring_fallback_sources",
                    [],
                )
            ),
        )
        if source
    )
    build_metadata = historical_build_metadata(
        config,
        policy_lock_date=policy_lock_date,
        required_metrics=required_metric_names(),
    )
    financial_main = _load_financial_main()
    sector_output_root = dashboard_root.parents[2]
    report: list[dict[str, object]] = []
    report_csv = (
        historical_root
        / "machinery_historical_promotion_materialization.csv"
    )
    live_partition_date = str(preflight_summary["historical_end_date"])

    for index, asof_date in enumerate(selected_dates, start=1):
        output_dir = dashboard_root / asof_date
        tickers = partitions[asof_date]
        restore_state: RestoredFeatureState | None = None
        try:
            if args.resume and _metadata_matches(
                output_dir,
                fingerprint=fingerprint,
            ):
                adapter_count = _validate_portfolio(
                    sector_output_root,
                    asof_date=asof_date,
                )
                rows = load_validated_sidecar(
                    output_dir,
                    asof=asof_date,
                )
                report.append(
                    {
                        "asof_date": asof_date,
                        "status": "PASS_EXISTING",
                        "affected_tickers": ",".join(tickers),
                        "row_count": len(rows),
                        "rank_ready_count": sum(
                            row["rank_ready_flag"] == "1"
                            for row in rows
                        ),
                        "portfolio_adapter_row_count": adapter_count,
                        "preflight_fingerprint": fingerprint,
                        "error": "",
                    }
                )
                write_csv_atomic(report_csv, REPORT_FIELDS, report)
                if index % 25 == 0 or index == len(selected_dates):
                    print(
                        json.dumps(
                            {
                                "progress": (
                                    f"{index}/{len(selected_dates)}"
                                ),
                                "asof_date": asof_date,
                                "status": report[-1]["status"],
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )
                continue
            baseline_rows = load_validated_sidecar(
                output_dir,
                asof=asof_date,
            )
            if asof_date != live_partition_date:
                with connect(db_path) as conn:
                    restore_state = restore_validated_sidecar_features(
                        conn,
                        asof_date=asof_date,
                        rows=baseline_rows,
                    )
            try:
                _run_financial_stage(
                    financial_main,
                    config_path=config_path,
                    db_path=db_path,
                    asof_date=asof_date,
                    tickers=tickers,
                    scratch_root=scratch_root,
                )
                with connect(db_path) as conn:
                    feature_rows = build_scoring_feature_rows(
                        conn,
                        asof=asof_date,
                        eligibility_policies=policies,
                        market_source_priority=market_sources,
                        financial_source_priority=(
                            str(
                                cfg_get(
                                    config,
                                    "sec_fundamentals.companyfacts_source_id",
                                    "sec_companyfacts",
                                )
                            ),
                        ),
                        positioning_source_priority=(
                            str(
                                cfg_get(
                                    config,
                                    "positioning_import.source_id",
                                    "industrials_positioning_composite",
                                )
                            ),
                        ),
                        component_weights=weights,
                        min_score_confidence=float(
                            cfg_get(
                                config,
                                "machinery_scoring.min_score_confidence",
                                0.40,
                            )
                        ),
                        max_staleness_days=int(
                            cfg_get(
                                config,
                                "market_data_policy.max_staleness_days",
                                7,
                            )
                        ),
                        min_avg_dollar_volume=float(
                            cfg_get(
                                config,
                                (
                                    "market_data_policy."
                                    "min_avg_dollar_volume_60d_for_full_features"
                                ),
                                5_000_000,
                            )
                        ),
                        negative_profit_valuation_score_cap=float(
                            cfg_get(
                                config,
                                "machinery_scoring.negative_profit_valuation_score_cap",
                                25.0,
                            )
                        ),
                    )
                rank_rows = finalize_rank_rows(
                    feature_rows,
                    score_model_version=str(
                        cfg_get(
                            config,
                            "machinery_scoring.score_model_version",
                        )
                    ),
                    model_version=str(
                        cfg_get(config, "machinery_scoring.model_version")
                    ),
                    scoring_contract_version=str(
                        cfg_get(
                            config,
                            "machinery_scoring.contract_version",
                        )
                    ),
                )
                historical_rows = survivorship_sidecar(rank_rows)
                publish_dashboard(
                    output_dir=output_dir,
                    rows=historical_rows,
                    asof=asof_date,
                    allow_overwrite=True,
                )
                adapter_count = _validate_portfolio(
                    sector_output_root,
                    asof_date=asof_date,
                )
                write_json_atomic(
                    output_dir / HISTORICAL_BUILD_METADATA_FILENAME,
                    {
                        **build_metadata,
                        "acceptance": "PASS",
                        "asof_date": asof_date,
                        "promotion_preflight_fingerprint": fingerprint,
                        "promotion_ids": preflight_summary[
                            "selected_promotion_ids"
                        ],
                        "promotion_affected_tickers": list(tickers),
                    },
                )
            finally:
                if restore_state is not None:
                    with connect(db_path) as conn:
                        compact_restored_features(
                            conn,
                            asof_date=asof_date,
                            restore_state=restore_state,
                        )
            report.append(
                {
                    "asof_date": asof_date,
                    "status": "PASS",
                    "affected_tickers": ",".join(tickers),
                    "row_count": len(historical_rows),
                    "rank_ready_count": sum(
                        row["rank_ready_flag"] == "1"
                        for row in historical_rows
                    ),
                    "portfolio_adapter_row_count": adapter_count,
                    "preflight_fingerprint": fingerprint,
                    "error": "",
                }
            )
        except Exception as exc:
            report.append(
                {
                    "asof_date": asof_date,
                    "status": "FAIL",
                    "affected_tickers": ",".join(tickers),
                    "row_count": 0,
                    "rank_ready_count": 0,
                    "portfolio_adapter_row_count": 0,
                    "preflight_fingerprint": fingerprint,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            write_csv_atomic(report_csv, REPORT_FIELDS, report)
            raise
        write_csv_atomic(report_csv, REPORT_FIELDS, report)
        if index % 25 == 0 or index == len(selected_dates):
            print(
                json.dumps(
                    {
                        "progress": f"{index}/{len(selected_dates)}",
                        "asof_date": asof_date,
                        "status": report[-1]["status"],
                    },
                    sort_keys=True,
                ),
                flush=True,
            )

    all_dates = sorted(
        path.name
        for path in dashboard_root.iterdir()
        if path.is_dir()
        and (
            path / "machinery_stage11_survivorship_calibration_panel.csv"
        ).exists()
    )
    with connect(db_path) as conn:
        combined = build_combined_historical_coverage(
            conn,
            dates=all_dates,
            dashboard_root=dashboard_root,
            report_root=historical_root,
            start_date=all_dates[0],
            end_date=all_dates[-1],
        )
    all_date_adapter_failures: list[str] = []
    if not args.skip_all_date_adapter_validation:
        for adapter_index, asof_date in enumerate(all_dates, start=1):
            try:
                _validate_portfolio(
                    sector_output_root,
                    asof_date=asof_date,
                )
            except ValueError as exc:
                all_date_adapter_failures.append(
                    f"{asof_date}: {exc}"
                )
            if (
                adapter_index % 250 == 0
                or adapter_index == len(all_dates)
            ):
                print(
                    json.dumps(
                        {
                            "adapter_validation_progress": (
                                f"{adapter_index}/{len(all_dates)}"
                            ),
                            "asof_date": asof_date,
                            "failure_count": len(
                                all_date_adapter_failures
                            ),
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
    acceptance = (
        "PASS"
        if combined["acceptance"] == "PASS"
        and not all_date_adapter_failures
        and len(report) == len(selected_dates)
        else "FAIL"
    )
    summary = {
        "acceptance": acceptance,
        "preflight_fingerprint": fingerprint,
        "selected_partition_count": len(selected_dates),
        "passed_partition_count": sum(
            str(row["status"]).startswith("PASS") for row in report
        ),
        "failed_partition_count": sum(
            row["status"] == "FAIL" for row in report
        ),
        "combined_coverage_acceptance": combined["acceptance"],
        "all_date_adapter_validation_count": (
            0
            if args.skip_all_date_adapter_validation
            else len(all_dates)
        ),
        "all_date_adapter_failures": all_date_adapter_failures,
        "report_csv": str(report_csv),
    }
    write_json_atomic(
        historical_root
        / "machinery_historical_promotion_materialization.json",
        summary,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if acceptance == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

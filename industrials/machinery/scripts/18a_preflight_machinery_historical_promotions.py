#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import cast


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
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.machinery.historical_promotion_preflight import (  # noqa: E402
    CATEGORY_FIELDS,
    IMPACT_FIELDS,
    METRIC_DEPTH_FIELDS,
    PARTITION_FIELDS,
    RANGE_FIELDS,
    HistoricalDepthThresholds,
    run_historical_promotion_preflight,
)
from industrials.machinery.scoring import write_json_atomic  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
ADAPTER = (
    "industrials.machinery.dedicated_parser_adapter:"
    "extract_metric_evidence"
)


def _promotion_ids(value: str) -> tuple[int, ...]:
    try:
        result = tuple(
            sorted(
                {
                    int(item.strip())
                    for item in value.split(",")
                    if item.strip()
                }
            )
        )
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "promotion ids must be comma-separated integers"
        ) from exc
    if not result or any(item <= 0 for item in result):
        raise argparse.ArgumentTypeError(
            "at least one positive promotion id is required"
        )
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the read-only machinery historical promotion go/no-go "
            "preflight without rebuilding or publishing historical files."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument(
        "--promotion-ids",
        type=_promotion_ids,
        required=True,
        help="Explicit comma-separated production promotion ids.",
    )
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--minimum-total-observations", type=int, default=500)
    parser.add_argument("--minimum-qualified-dates", type=int, default=252)
    parser.add_argument("--minimum-qualified-years", type=int, default=3)
    parser.add_argument("--minimum-delisted-tickers", type=int, default=1)
    return parser.parse_args(argv)


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
    machinery_output_root = dashboard_root.parent
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir is not None
        else machinery_output_root / "historical_backfill" / "preflight"
    )
    current_coverage_csv = (
        machinery_output_root
        / "stage4"
        / "machinery_financial_metric_coverage.csv"
    )
    historical_summary_json = (
        machinery_output_root
        / "historical_backfill"
        / "machinery_combined_historical_coverage.json"
    )
    registry = load_registry(ADAPTER)
    source_id = str(
        cfg_get(
            config,
            "dedicated_parser.production_source_id",
            "dedicated_parser_production",
        )
    )
    thresholds = HistoricalDepthThresholds(
        minimum_total_observations=args.minimum_total_observations,
        minimum_qualified_dates=args.minimum_qualified_dates,
        minimum_qualified_years=args.minimum_qualified_years,
        minimum_delisted_tickers=args.minimum_delisted_tickers,
    )
    with connect(db_path) as conn:
        result = run_historical_promotion_preflight(
            conn,
            promotion_ids=args.promotion_ids,
            registry=registry,
            source_id=source_id,
            current_coverage_csv=current_coverage_csv,
            historical_summary_json=historical_summary_json,
            dashboard_root=dashboard_root,
            thresholds=thresholds,
        )
    metric_rows = cast(list[dict[str, object]], result["metric_rows"])
    category_rows = cast(list[dict[str, object]], result["category_rows"])
    impact_rows = cast(list[dict[str, object]], result["impact_rows"])
    partition_rows = cast(
        list[dict[str, object]],
        result["partition_rows"],
    )
    range_rows = cast(list[dict[str, object]], result["range_rows"])
    summary_data = cast(dict[str, object], result["summary"])
    output_dir.mkdir(parents=True, exist_ok=True)
    metric_csv = output_dir / "machinery_historical_preflight_metric_depth.csv"
    category_csv = (
        output_dir / "machinery_historical_preflight_category_gates.csv"
    )
    impact_csv = output_dir / "machinery_historical_preflight_impacts.csv"
    partition_csv = (
        output_dir / "machinery_historical_preflight_affected_partitions.csv"
    )
    range_csv = (
        output_dir / "machinery_historical_preflight_affected_ranges.csv"
    )
    summary_json = output_dir / "machinery_historical_preflight_summary.json"
    write_csv_atomic(metric_csv, METRIC_DEPTH_FIELDS, metric_rows)
    write_csv_atomic(
        category_csv,
        CATEGORY_FIELDS,
        category_rows,
    )
    write_csv_atomic(impact_csv, IMPACT_FIELDS, impact_rows)
    write_csv_atomic(
        partition_csv,
        PARTITION_FIELDS,
        partition_rows,
    )
    write_csv_atomic(range_csv, RANGE_FIELDS, range_rows)
    summary = {
        **summary_data,
        "metric_depth_csv": str(metric_csv),
        "category_gates_csv": str(category_csv),
        "impact_csv": str(impact_csv),
        "affected_partitions_csv": str(partition_csv),
        "affected_ranges_csv": str(range_csv),
    }
    write_json_atomic(summary_json, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

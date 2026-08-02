#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import (  # noqa: E402
    cfg_get,
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.historical_score_history import (  # noqa: E402
    benchmark_trading_dates,
    select_dates,
    valid_score_snapshot,
)
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.core.score_history import (  # noqa: E402
    validate_shadow_survivorship_sidecar,
)
from industrials.transportation.contracts import (  # noqa: E402
    file_sha256,
    validate_rank_rows,
    write_manifest,
    write_scoring_rows,
)
from industrials.transportation.financial_contract import (  # noqa: E402
    load_metric_registry,
)
from industrials.transportation.scoring import (  # noqa: E402
    build_scoring_rows,
    finalize_rank_rows,
    publish_dashboard,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)

RANK_FILENAME = "transportation_final_rank_table.csv"
SIDECAR_FILENAME = "transportation_stage11_survivorship_calibration_panel.csv"
RANK_MANIFEST_FILENAME = "transportation_final_rank_table_manifest.json"
VALIDATION_FILENAME = "transportation_final_rank_table_validation.json"
REPORT_FIELDS = (
    "asof_date",
    "expected_ticker_count",
    "market_feature_count",
    "rank_row_count",
    "rank_ready_count",
    "stage11_eligible_count",
    "status",
    "elapsed_seconds",
    "message",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Bulk-materialize transportation daily PIT scoring/rank history "
            "from exact daily shared market features and sealed financial panels."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--max-dates", type=int, default=0)
    parser.add_argument(
        "--selection", choices=("oldest", "newest"), default="oldest"
    )
    parser.add_argument("--rebuild-existing", action="store_true")
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--output-json", type=Path, default=None)
    return parser.parse_args()


def iso_date(raw: str, *, label: str) -> str:
    value = str(raw or "")[:10]
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"invalid {label}={raw!r}") from exc


def read_report(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return {
            str(row.get("asof_date") or ""): {
                field: str(row.get(field) or "") for field in REPORT_FIELDS
            }
            for row in csv.DictReader(handle)
            if str(row.get("asof_date") or "")
        }


def expected_tickers(
    connection: sqlite3.Connection, *, asof: str
) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            """
            SELECT DISTINCT ticker FROM dim_universe_membership
            WHERE model_family=? AND start_date<=?
              AND COALESCE(end_date,'9999-12-31')>=?
            """,
            (MODEL_FAMILY, asof, asof),
        ).fetchall()
    }


def market_tickers(
    connection: sqlite3.Connection, *, asof: str
) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            """
            SELECT DISTINCT ticker FROM feature_market_technical
            WHERE model_family=? AND asof_date=?
            """,
            (MODEL_FAMILY, asof),
        ).fetchall()
    }


def snapshot_valid(
    *, feature_dir: Path, dashboard_dir: Path
) -> bool:
    return valid_score_snapshot(
        snapshot_dir=dashboard_dir,
        rank_filename=RANK_FILENAME,
        sidecar_filename=SIDECAR_FILENAME,
        rank_manifest_filename=RANK_MANIFEST_FILENAME,
        validation_filename=VALIDATION_FILENAME,
        scoring_manifest=feature_dir / "scoring_features.manifest.json",
        membership_mode="pit",
        metric_snapshot_mode="latest",
    )


def main() -> int:
    args = parse_args()
    if args.max_dates < 0:
        raise ValueError("--max-dates cannot be negative")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    universe = family["universe"]
    historical = family["historical_features"]
    score_history = family["historical_scores"]
    scoring = family["scoring"]
    financial = family["financial"]
    base_dir = config_path.parent
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    start_date = iso_date(
        str(args.start_date or score_history["start_date"]), label="start date"
    )
    end_date = iso_date(args.end_date, label="end date")
    feature_root = resolve_path(
        score_history.get("feature_output_root", historical["output_root"]),
        base_dir=base_dir,
    )
    dashboard_root = resolve_path(
        score_history.get("output_root", scoring["dashboard_root"]),
        base_dir=base_dir,
    )
    report_path = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(score_history["build_report_csv"], base_dir=base_dir)
    )
    manifest_path = (
        args.output_json.expanduser().resolve()
        if args.output_json
        else resolve_path(score_history["build_manifest_json"], base_dir=base_dir)
    )
    registry_version, definitions = load_metric_registry(
        resolve_path(financial["metric_registry"], base_dir=base_dir)
    )
    policy_path = resolve_path(
        cfg_get(
            config,
            "scoring_policy.families.transportation.eligibility_policy_csv",
        ),
        base_dir=base_dir,
    )
    component_weights = {
        str(key): float(value)
        for key, value in scoring["component_weights"].items()
    }
    overlay_weights = {
        str(key): float(value)
        for key, value in (
            scoring.get("specialized_overlay_weights") or {}
        ).items()
    }
    timeout = float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))
    connection = sqlite3.connect(db_path, timeout=timeout)
    connection.row_factory = sqlite3.Row
    try:
        dates = benchmark_trading_dates(
            connection,
            ticker=str(score_history["benchmark_ticker"]),
            source_id=str(score_history["benchmark_source_id"]),
            start_date=start_date,
            end_date=end_date,
        )
        valid_before = {
            asof
            for asof in dates
            if snapshot_valid(
                feature_dir=feature_root / asof,
                dashboard_dir=dashboard_root / asof,
            )
        }
        pending_all = [
            asof
            for asof in dates
            if args.rebuild_existing or asof not in valid_before
        ]
        pending = select_dates(
            pending_all, maximum=args.max_dates, selection=args.selection
        )
        report_by_date = read_report(report_path)
        failures: list[str] = []
        for asof in pending:
            started = time.monotonic()
            feature_dir = feature_root / asof
            dashboard_dir = dashboard_root / asof
            feature_dir.mkdir(parents=True, exist_ok=True)
            dashboard_dir.mkdir(parents=True, exist_ok=True)
            status = "PASS"
            message = ""
            expected = expected_tickers(connection, asof=asof)
            market = market_tickers(connection, asof=asof)
            rows: list[dict[str, str]] = []
            sidecar_rows: list[dict[str, str]] = []
            try:
                if market != expected:
                    raise ValueError(
                        "exact daily market history incomplete "
                        f"missing={sorted(expected-market)[:20]} "
                        f"extra={sorted(market-expected)[:20]}"
                    )
                rows = build_scoring_rows(
                    connection,
                    asof=asof,
                    active_source_id=str(universe["seed_source_id"]),
                    membership_mode="pit",
                    historical_source_id=str(
                        universe["historical_membership_source_id"]
                    ),
                    delisted_source_id=str(universe["delisted_source_id"]),
                    metric_snapshot_mode="latest",
                    policy_asof=str(score_history["policy_lock_date"]),
                    definitions=definitions,
                    registry_version=registry_version,
                    policy_path=policy_path,
                    component_weights=component_weights,
                    max_staleness_days=int(scoring["max_staleness_days"]),
                    minimum_avg_dollar_volume=float(
                        scoring["minimum_avg_dollar_volume_60d"]
                    ),
                    minimum_score_confidence=float(
                        scoring["minimum_score_confidence"]
                    ),
                    minimum_specialized_coverage=float(
                        scoring["minimum_specialized_coverage"]
                    ),
                    positioning_source_id=str(
                        scoring["positioning_feature_source_id"]
                    ),
                    minimum_positioning_input_coverage=float(
                        scoring["minimum_positioning_input_coverage"]
                    ),
                    specialized_overlay_weights=overlay_weights,
                    classification_overlays_path=resolve_path(
                        universe["classification_overlays_csv"],
                        base_dir=base_dir,
                    ),
                )
                scoring_path = feature_dir / "scoring_features.csv"
                write_scoring_rows(scoring_path, rows)
                scoring_manifest = {
                    "acceptance": "PASS",
                    "model_family": MODEL_FAMILY,
                    "asof_date": asof,
                    "membership_mode": "pit",
                    "metric_snapshot_mode": "latest",
                    "policy_asof": str(score_history["policy_lock_date"]),
                    "policy_replay_mode": "frozen_policy_on_pit_features",
                    "positioning_snapshot_mode": "exact_date_shared_feature",
                    "positioning_populated_count": sum(
                        bool(str(row.get("positioning_score") or "")) for row in rows
                    ),
                    "row_count": len(rows),
                    "rank_ready_count": sum(
                        row["rank_ready_flag"] == "1" for row in rows
                    ),
                    "blocked_count": sum(
                        row["rank_ready_flag"] == "0" for row in rows
                    ),
                    "metric_registry_version": registry_version,
                    "score_construction_mode": str(
                        scoring.get("score_construction_mode")
                    ),
                    "specialized_overlay_weights": overlay_weights,
                    "specialized_overlay_active": any(
                        value > 0.0 for value in overlay_weights.values()
                    ),
                    "output_artifact": {
                        "path": str(scoring_path),
                        "sha256": file_sha256(scoring_path),
                        "row_count": len(rows),
                    },
                }
                write_manifest(
                    feature_dir / "scoring_features.manifest.json",
                    scoring_manifest,
                )
                final_rows = finalize_rank_rows(
                    rows,
                    score_model_version=str(scoring["score_model_version"]),
                    model_version=str(scoring["model_version"]),
                    scoring_contract_version=str(
                        scoring["scoring_contract_version"]
                    ),
                )
                publish_dashboard(
                    output_dir=dashboard_dir,
                    rows=final_rows,
                    asof=asof,
                    allow_overwrite=True,
                )
                errors = validate_rank_rows(final_rows, asof=asof)
                sidecar_path = dashboard_dir / SIDECAR_FILENAME
                with sidecar_path.open(
                    "r", encoding="utf-8-sig", newline=""
                ) as handle:
                    sidecar_rows = [dict(row) for row in csv.DictReader(handle)]
                errors.extend(
                    validate_shadow_survivorship_sidecar(
                        sidecar_rows,
                        asof_date=asof,
                        expected_tickers=expected,
                    )
                )
                if {row["ticker"] for row in final_rows} != expected:
                    errors.append("rank table PIT universe mismatch")
                if errors:
                    raise ValueError("; ".join(errors[:20]))
                validation = {
                    "acceptance": "PASS",
                    "model_family": MODEL_FAMILY,
                    "asof_date": asof,
                    "membership_mode": "pit",
                    "row_count": len(final_rows),
                    "expected_row_count": len(expected),
                    "rank_ready_count": sum(
                        row["rank_ready_flag"] == "1" for row in final_rows
                    ),
                    "portfolio_candidate_count": 0,
                    "oos_score_valid_count": 0,
                    "stage11_sidecar_row_count": len(sidecar_rows),
                    "stage11_calibration_input_eligible_count": sum(
                        row["stage11_calibration_input_eligible_flag"] == "1"
                        for row in sidecar_rows
                    ),
                    "errors": [],
                }
                write_manifest(
                    dashboard_dir / VALIDATION_FILENAME, validation
                )
                if not snapshot_valid(
                    feature_dir=feature_dir, dashboard_dir=dashboard_dir
                ):
                    raise ValueError("immutable snapshot validation failed")
            except (OSError, ValueError, sqlite3.Error) as exc:
                status = "FAIL"
                message = f"{type(exc).__name__}: {exc}"
                failures.append(f"{asof}:{message}")
                validation = {}
            report_by_date[asof] = {
                "asof_date": asof,
                "expected_ticker_count": len(expected),
                "market_feature_count": len(market),
                "rank_row_count": len(rows),
                "rank_ready_count": sum(
                    row.get("rank_ready_flag") == "1" for row in rows
                ),
                "stage11_eligible_count": sum(
                    row.get("stage11_calibration_input_eligible_flag") == "1"
                    for row in sidecar_rows
                ),
                "status": status,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "message": message,
            }
            write_csv_atomic(
                report_path,
                REPORT_FIELDS,
                [report_by_date[key] for key in sorted(report_by_date)],
            )
            if len(report_by_date) % 25 == 0 or status == "FAIL":
                print(
                    f"transportation_daily_rank_progress asof={asof} "
                    f"status={status}",
                    flush=True,
                )
            if failures:
                break
        valid_after = [
            asof
            for asof in dates
            if snapshot_valid(
                feature_dir=feature_root / asof,
                dashboard_dir=dashboard_root / asof,
            )
        ]
    finally:
        connection.close()
    remaining = sorted(set(dates) - set(valid_after))
    acceptance = "FAIL" if failures else "PASS" if not remaining else "PARTIAL_PASS"
    result = {
        "acceptance": acceptance,
        "model_family": MODEL_FAMILY,
        "history_contract_version": "industrials_daily_pit_score_history_v1",
        "start_date": start_date,
        "end_date": end_date,
        "selected_date_count": len(dates),
        "attempted_date_count": len(pending),
        "completed_date_count": len(valid_after),
        "remaining_date_count": len(remaining),
        "remaining_dates": remaining,
        "membership_mode": "pit",
        "market_snapshot_mode": "exact_daily",
        "metric_snapshot_mode": "latest_sealed_month_end_pit",
        "policy_asof": str(score_history["policy_lock_date"]),
        "policy_replay_mode": "frozen_policy_on_pit_features",
        "positioning_snapshot_mode": "not_materialized_daily_zero_component_weight",
        "survivorship_corrected_panel": True,
        "report_csv": str(report_path),
        "errors": failures,
    }
    write_manifest(manifest_path, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

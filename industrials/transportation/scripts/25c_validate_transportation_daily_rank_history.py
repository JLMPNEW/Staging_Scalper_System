#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import (  # noqa: E402
    cfg_get,
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.db import connect  # noqa: E402
from industrials.core.historical_score_history import (  # noqa: E402
    benchmark_trading_dates,
    read_json,
    sha256_file,
    valid_score_snapshot,
)
from industrials.core.oos_research import select_weekly_dates  # noqa: E402
from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.contracts import read_rows  # noqa: E402
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Independently audit every transportation daily rank snapshot, "
            "survivorship sidecar, and PIT-universe key."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    family = family_config(config, MODEL_FAMILY)
    universe = family["universe"]
    history = family["historical_scores"]
    dashboard = resolve_path(
        history["output_root"],
        base_dir=base_dir,
    )
    feature_root = resolve_path(
        history.get(
            "feature_output_root",
            family["historical_features"]["output_root"],
        ),
        base_dir=base_dir,
    )
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(config["paths"]["database_path"], base_dir=base_dir)
    )
    build_manifest_path = resolve_path(
        history["build_manifest_json"],
        base_dir=base_dir,
    )
    build_manifest = read_json(build_manifest_path)
    surface_policy = load_yaml(
        resolve_path(
            family["scoring"]["surface_freight_score_policy"],
            base_dir=base_dir,
        )
    )
    frozen_tickers = {str(value) for value in surface_policy["eligible_tickers"]}
    connection = connect(
        db_path,
        timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 120)),
    )
    try:
        dates = benchmark_trading_dates(
            connection,
            ticker=str(history["benchmark_ticker"]),
            source_id=str(history["benchmark_source_id"]),
            start_date=str(history["start_date"]),
            end_date=str(history["policy_lock_date"]),
        )
        if build_manifest.get("observation_cadence") == "weekly_oos_observations":
            standards = cfg_get(
                config,
                "oos_calibration_standards.families.transportation",
                {},
            )
            dates = select_weekly_dates(
                dates,
                anchor=str(standards["weekly_anchor_date"]),
                selection=str(standards.get("weekly_selection") or "last"),
            )
        issues: list[str] = []
        rank_ready_counts: list[int] = []
        eligible_counts: list[int] = []
        row_counts: list[int] = []
        membership_sources: Counter[str] = Counter()
        for asof in dates:
            snapshot_dir = dashboard / asof
            feature_dir = feature_root / asof
            if not valid_score_snapshot(
                snapshot_dir=snapshot_dir,
                rank_filename="transportation_final_rank_table.csv",
                sidecar_filename=(
                    "transportation_stage11_survivorship_calibration_panel.csv"
                ),
                rank_manifest_filename=(
                    "transportation_final_rank_table_manifest.json"
                ),
                validation_filename=(
                    "transportation_final_rank_table_validation.json"
                ),
                scoring_manifest=(
                    feature_dir / "scoring_features.manifest.json"
                ),
                membership_mode="pit",
                metric_snapshot_mode="latest",
            ):
                issues.append(f"{asof}: immutable snapshot validation failed")
                continue
            sidecar = (
                snapshot_dir
                / "transportation_stage11_survivorship_calibration_panel.csv"
            )
            rows = read_rows(sidecar)
            expected = {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT DISTINCT ticker
                    FROM dim_universe_membership
                    WHERE model_family=?
                      AND start_date<=?
                      AND COALESCE(end_date,'9999-12-31')>=?
                    """,
                    (MODEL_FAMILY, asof, asof),
                ).fetchall()
            } & frozen_tickers
            actual = {row["ticker"] for row in rows}
            if len(actual) != len(rows) or actual != expected:
                issues.append(
                    f"{asof}: PIT universe mismatch "
                    f"missing={sorted(expected-actual)[:10]} "
                    f"extra={sorted(actual-expected)[:10]}"
                )
            if any(
                row["survivorship_corrected_panel_flag"] != "1"
                or row["portfolio_candidate_gate"] != "0"
                or row["oos_score_valid_flag"] != "0"
                for row in rows
            ):
                issues.append(f"{asof}: sidecar fail-closed contract violation")
            row_counts.append(len(rows))
            rank_ready_counts.append(
                sum(row["rank_ready_flag"] == "1" for row in rows)
            )
            eligible_counts.append(
                sum(
                    row["stage11_calibration_input_eligible_flag"] == "1"
                    for row in rows
                )
            )
            membership_sources.update(
                row["membership_source_id"] for row in rows
            )
    finally:
        connection.close()
    if (
        build_manifest.get("acceptance") != "PASS"
        or int(build_manifest.get("completed_date_count") or -1)
        != len(dates)
        or int(build_manifest.get("remaining_date_count", -1)) != 0
    ):
        issues.append("daily-history build manifest is incomplete")
    result = {
        "artifact_family": "transportation_weekly_rank_history_validation",
        "model_family": MODEL_FAMILY,
        "acceptance": "PASS" if not issues else "FAIL",
        "expected_date_count": len(dates),
        "validated_date_count": len(row_counts),
        "start_date": dates[0] if dates else "",
        "end_date": dates[-1] if dates else "",
        "minimum_row_count": min(row_counts) if row_counts else 0,
        "maximum_row_count": max(row_counts) if row_counts else 0,
        "minimum_rank_ready_count": (
            min(rank_ready_counts) if rank_ready_counts else 0
        ),
        "maximum_rank_ready_count": (
            max(rank_ready_counts) if rank_ready_counts else 0
        ),
        "minimum_stage11_eligible_count": (
            min(eligible_counts) if eligible_counts else 0
        ),
        "maximum_stage11_eligible_count": (
            max(eligible_counts) if eligible_counts else 0
        ),
        "membership_source_row_counts": dict(membership_sources),
        "active_and_inactive_membership_sources_present": (
            str(universe["historical_membership_source_id"])
            in membership_sources
            and str(universe["delisted_source_id"])
            in membership_sources
        ),
        "build_manifest_path": str(build_manifest_path),
        "build_manifest_sha256": (
            sha256_file(build_manifest_path)
            if build_manifest_path.is_file()
            else ""
        ),
        "issues": issues[:200],
    }
    output = build_manifest_path.with_name(
        "transportation_weekly_rank_history_validation.json"
    )
    write_text_atomic(
        output,
        json.dumps(result, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not issues else 1


if __name__ == "__main__":
    raise SystemExit(main())

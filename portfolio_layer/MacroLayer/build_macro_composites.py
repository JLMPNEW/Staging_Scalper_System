#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sqlite3
import uuid
from dataclasses import asdict
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from macro_composite_policy import CompositePolicy, load_composite_policy
from macro_raw_config import (
    cfg_get,
    configure_pipeline_logging,
    connect_sqlite,
    load_macro_raw_config,
    parse_iso_date,
    resolve_path,
    utc_now_iso,
)
from macro_serving_common import resolve_serving_db_path
from macro_serving_storage import (
    clear_composite_range,
    finish_serving_run,
    init_db,
    insert_many,
    start_serving_run,
)

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the tier-1 macro composite layer.")
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--serving-db-path", type=Path, default=None, help="Optional serving SQLite path override.")
    parser.add_argument("--composite-policy-csv", type=Path, default=None, help="Optional composite policy CSV override.")
    parser.add_argument("--start-date", type=str, default=None, help="Optional composite start YYYY-MM-DD override.")
    parser.add_argument("--end-date", type=str, default=None, help="Optional composite end YYYY-MM-DD override.")
    parser.add_argument("--composite-keys", nargs="*", default=None, help="Optional composite_key filter.")
    return parser.parse_args()


def _resolve_feature_bounds(
    conn: sqlite3.Connection,
    *,
    start_override: str | None,
    end_override: str | None,
) -> tuple[date, date]:
    start_date = parse_iso_date(start_override)
    end_date = parse_iso_date(end_override)
    row = conn.execute(
        """
        SELECT MIN(as_of_date) AS min_as_of_date, MAX(as_of_date) AS max_as_of_date
        FROM macro_feature_daily
        """
    ).fetchone()
    if start_date is None and row is not None:
        start_date = parse_iso_date(row["min_as_of_date"])
    if end_date is None and row is not None:
        end_date = parse_iso_date(row["max_as_of_date"])
    if start_date is None or end_date is None:
        raise ValueError("Unable to resolve composite build dates from macro_feature_daily.")
    if end_date < start_date:
        raise ValueError(f"Composite end date {end_date.isoformat()} is before start date {start_date.isoformat()}.")
    return start_date, end_date


def _latest_feature_run_raw_ingest_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        """
        SELECT raw_ingest_run_id
        FROM macro_serving_run
        WHERE build_step = 'feature_layer'
          AND status = 'completed'
        ORDER BY COALESCE(completed_at_utc, started_at_utc) DESC, rowid DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return str(row["raw_ingest_run_id"] or "") or None


def _load_feature_frame(
    conn: sqlite3.Connection,
    *,
    start_date: str,
    end_date: str,
    metric_keys: list[str],
) -> pd.DataFrame:
    placeholders = ",".join("?" for _ in metric_keys)
    query = f"""
        SELECT
            as_of_date,
            metric_key,
            feature_name,
            feature_event_as_of_date,
            standardized_value,
            source_quality_weight,
            carry_forward_flag,
            coverage_flag,
            staleness_days
        FROM macro_feature_daily
        WHERE as_of_date BETWEEN ? AND ?
          AND metric_key IN ({placeholders})
    """
    return pd.read_sql_query(query, conn, params=[start_date, end_date, *metric_keys])


def _load_calendar_frame(conn: sqlite3.Connection, *, start_date: str, end_date: str) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT as_of_date
        FROM macro_calendar_daily
        WHERE as_of_date BETWEEN ? AND ?
        ORDER BY as_of_date
        """,
        conn,
        params=[start_date, end_date],
    )


def _policy_frame(policy_rows: list[CompositePolicy]) -> pd.DataFrame:
    return pd.DataFrame([asdict(item) for item in policy_rows])


def _validate_policy_feature_pairs(
    conn: sqlite3.Connection,
    policy_rows: list[CompositePolicy],
) -> None:
    expected = {(item.metric_key, item.feature_name) for item in policy_rows}
    available = {
        (str(row[0]), str(row[1]))
        for row in conn.execute(
            "SELECT DISTINCT metric_key, feature_name FROM macro_feature_daily"
        ).fetchall()
    }
    missing = sorted(expected - available)
    if missing:
        sample = ", ".join(f"{metric}::{feature}" for metric, feature in missing[:12])
        suffix = "..." if len(missing) > 12 else ""
        raise ValueError(
            f"Composite policy references {len(missing)} feature pair(s) never materialized "
            f"in macro_feature_daily: {sample}{suffix}"
        )


def _ensure_consistent_scalar(policy_frame: pd.DataFrame, column_name: str, composite_key: str) -> float | int:
    values = [item for item in policy_frame[column_name].dropna().unique().tolist()]
    if len(values) != 1:
        raise ValueError(
            f"Composite policy must have exactly one distinct {column_name} for composite_key={composite_key}, "
            f"found {values!r}"
        )
    return values[0]


def _component_rows_for_composite(
    *,
    composite_key: str,
    policy_rows: list[CompositePolicy],
    calendar_frame: pd.DataFrame,
    feature_frame: pd.DataFrame,
) -> tuple[list[tuple], list[tuple]]:
    policy_frame = _policy_frame(policy_rows)
    policy_frame["required_flag"] = policy_frame["required_flag"].astype(int)
    policy_frame["min_feature_coverage_flag"] = policy_frame["min_feature_coverage_flag"].astype(int)
    policy_frame["base_weight"] = policy_frame["base_weight"].astype(float)
    policy_frame["source_quality_multiplier"] = policy_frame["source_quality_multiplier"].astype(float)
    policy_frame["min_composite_coverage_ratio"] = policy_frame["min_composite_coverage_ratio"].astype(float)
    policy_frame["min_required_coverage_ratio"] = policy_frame["min_required_coverage_ratio"].astype(float)
    policy_frame["smoothing_window_days"] = policy_frame["smoothing_window_days"].astype(int)
    policy_frame["_join_key"] = 1

    composite_feature = feature_frame[
        feature_frame["metric_key"].isin(policy_frame["metric_key"].tolist())
    ].copy()
    composite_feature.rename(
        columns={
            "coverage_flag": "feature_coverage_flag",
            "source_quality_weight": "feature_source_quality_weight",
        },
        inplace=True,
    )

    dates = calendar_frame.copy()
    dates["_join_key"] = 1
    merged = dates.merge(policy_frame, on="_join_key", how="inner").drop(columns=["_join_key"])
    merged = merged.merge(
        composite_feature,
        on=["as_of_date", "metric_key", "feature_name"],
        how="left",
    )

    merged["feature_source_quality_weight"] = merged["feature_source_quality_weight"].fillna(1.0)
    merged["feature_coverage_flag"] = merged["feature_coverage_flag"].fillna(0).astype(int)
    merged["carry_forward_flag"] = merged["carry_forward_flag"].fillna(0).astype(int)

    has_standardized = merged["standardized_value"].notna()
    meets_coverage = merged["feature_coverage_flag"] >= merged["min_feature_coverage_flag"]
    staleness_override = merged["max_staleness_days_override"].notna()
    staleness_days = merged["staleness_days"].fillna(np.inf)
    meets_staleness = (~staleness_override) | (staleness_days <= merged["max_staleness_days_override"].fillna(np.inf))
    merged["included_flag"] = (has_standardized & meets_coverage & meets_staleness).astype(int)

    exclusion_reason = np.select(
        [
            ~has_standardized,
            has_standardized & ~meets_coverage,
            has_standardized & meets_coverage & ~meets_staleness,
        ],
        [
            "missing_standardized_value",
            "feature_coverage_failed",
            "staleness_override_failed",
        ],
        default="",
    )
    merged["exclusion_reason"] = np.where(merged["included_flag"] == 1, "", exclusion_reason)

    merged["effective_weight"] = np.where(
        merged["included_flag"] == 1,
        merged["base_weight"] * merged["feature_source_quality_weight"] * merged["source_quality_multiplier"],
        0.0,
    )
    merged["weighted_value"] = np.where(
        merged["included_flag"] == 1,
        merged["standardized_value"] * merged["effective_weight"],
        0.0,
    )
    merged["required_included"] = merged["required_flag"] * merged["included_flag"]

    summary = (
        merged.groupby("as_of_date", as_index=False)
        .agg(
            expected_component_count=("metric_key", "size"),
            available_component_count=("included_flag", "sum"),
            required_component_count=("required_flag", "sum"),
            available_required_count=("required_included", "sum"),
            effective_weight_sum=("effective_weight", "sum"),
            weighted_value_sum=("weighted_value", "sum"),
        )
        .sort_values("as_of_date")
        .reset_index(drop=True)
    )

    summary["coverage_ratio"] = np.where(
        summary["expected_component_count"] > 0,
        summary["available_component_count"] / summary["expected_component_count"],
        np.nan,
    )
    summary["required_coverage_ratio"] = np.where(
        summary["required_component_count"] > 0,
        summary["available_required_count"] / summary["required_component_count"],
        1.0,
    )
    summary["composite_value_raw"] = np.where(
        summary["effective_weight_sum"] > 0,
        summary["weighted_value_sum"] / summary["effective_weight_sum"],
        np.nan,
    )

    smoothing_window_days = int(_ensure_consistent_scalar(policy_frame, "smoothing_window_days", composite_key))
    min_composite_coverage_ratio = float(
        _ensure_consistent_scalar(policy_frame, "min_composite_coverage_ratio", composite_key)
    )
    min_required_coverage_ratio = float(
        _ensure_consistent_scalar(policy_frame, "min_required_coverage_ratio", composite_key)
    )

    summary["coverage_flag"] = (
        (summary["effective_weight_sum"] > 0.0)
        & (summary["coverage_ratio"] >= min_composite_coverage_ratio)
        & (summary["required_coverage_ratio"] >= min_required_coverage_ratio)
    ).astype(int)

    smooth_input = summary["composite_value_raw"].where(summary["coverage_flag"] == 1, np.nan)
    summary["composite_value_smoothed"] = smooth_input.rolling(window=smoothing_window_days, min_periods=1).mean()
    summary.loc[summary["coverage_flag"] != 1, "composite_value_smoothed"] = np.nan

    merged = merged.merge(
        summary[
            [
                "as_of_date",
                "effective_weight_sum",
            ]
        ],
        on="as_of_date",
        how="left",
    )
    merged["normalized_weight"] = np.where(
        (merged["included_flag"] == 1) & (merged["effective_weight_sum"] > 0.0),
        merged["effective_weight"] / merged["effective_weight_sum"],
        np.nan,
    )
    merged["contribution_value"] = np.where(
        merged["included_flag"] == 1,
        merged["standardized_value"] * merged["normalized_weight"],
        np.nan,
    )

    component_rows = [
        (
            str(row["as_of_date"]),
            composite_key,
            str(row["metric_key"]),
            str(row["feature_name"]),
            str(row["ref_area"] or ""),
            str(row["regime_block"] or ""),
            str(row["feature_event_as_of_date"] or "") or None,
            float(row["standardized_value"]) if pd.notna(row["standardized_value"]) else None,
            float(row["base_weight"]),
            float(row["effective_weight"]),
            float(row["normalized_weight"]) if pd.notna(row["normalized_weight"]) else None,
            float(row["contribution_value"]) if pd.notna(row["contribution_value"]) else None,
            float(row["feature_source_quality_weight"]),
            int(row["carry_forward_flag"]),
            int(row["feature_coverage_flag"]),
            int(row["required_flag"]),
            int(row["included_flag"]),
            str(row["exclusion_reason"] or ""),
            utc_now_iso(),
        )
        for _, row in merged.iterrows()
    ]

    daily_rows = [
        (
            str(row["as_of_date"]),
            composite_key,
            float(row["composite_value_raw"]) if pd.notna(row["composite_value_raw"]) else None,
            float(row["composite_value_smoothed"]) if pd.notna(row["composite_value_smoothed"]) else None,
            int(row["expected_component_count"]),
            int(row["available_component_count"]),
            int(row["required_component_count"]),
            int(row["available_required_count"]),
            float(row["coverage_ratio"]) if pd.notna(row["coverage_ratio"]) else None,
            float(row["required_coverage_ratio"]) if pd.notna(row["required_coverage_ratio"]) else None,
            float(row["effective_weight_sum"]) if pd.notna(row["effective_weight_sum"]) else None,
            smoothing_window_days,
            int(row["coverage_flag"]),
            utc_now_iso(),
        )
        for _, row in summary.iterrows()
    ]
    return component_rows, daily_rows


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, cfg = load_macro_raw_config(args.config)
    serving_db_path = resolve_serving_db_path(cfg, config_path, override=args.serving_db_path)
    composite_policy_path = args.composite_policy_csv or resolve_path(
        config_path,
        str(cfg_get(cfg, "composite_policy_csv", default="MacroLayer/macro_composite_policy.csv")),
    )
    if composite_policy_path is None:
        raise ValueError("macro_raw.composite_policy_csv is required for composite builds.")

    conn = connect_sqlite(serving_db_path, row_factory=sqlite3.Row)
    serving_run_id = uuid.uuid4().hex
    run_started = False
    rows_written = 0
    try:
        init_db(conn)
        start_date, end_date = _resolve_feature_bounds(
            conn,
            start_override=args.start_date,
            end_override=args.end_date,
        )
        all_policy_rows = load_composite_policy(composite_policy_path)
        composite_filter = {str(item).strip().upper() for item in (args.composite_keys or []) if str(item).strip()}
        if composite_filter:
            policy_rows = [item for item in all_policy_rows if item.composite_key in composite_filter]
        else:
            policy_rows = all_policy_rows
        if not policy_rows:
            raise ValueError("No composite policy rows matched the requested build.")
        _validate_policy_feature_pairs(conn, policy_rows)

        policy_by_composite: dict[str, list[CompositePolicy]] = {}
        for item in policy_rows:
            policy_by_composite.setdefault(item.composite_key, []).append(item)
        composite_keys = sorted(policy_by_composite)
        if composite_filter:
            skipped_keys = sorted({item.composite_key for item in all_policy_rows} - set(composite_keys))
            logger.info(
                "Composite build filter active: selected_keys=%s skipped_keys=%s",
                composite_keys,
                skipped_keys,
            )

        feature_metric_keys = sorted({item.metric_key for item in policy_rows})
        calendar_frame = _load_calendar_frame(
            conn,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
        )
        feature_frame = _load_feature_frame(
            conn,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            metric_keys=feature_metric_keys,
        )
        raw_ingest_run_id = _latest_feature_run_raw_ingest_id(conn)

        start_serving_run(
            conn,
            serving_run_id=serving_run_id,
            build_step="composite_layer",
            raw_ingest_run_id=raw_ingest_run_id,
            as_of_start_date=start_date.isoformat(),
            as_of_end_date=end_date.isoformat(),
            metric_count=len(composite_keys),
            notes=f"Building macro composites for {len(composite_keys)} composites.",
        )
        run_started = True
        clear_composite_range(
            conn,
            table_name="macro_composite_daily",
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            composite_keys=composite_keys if composite_filter else None,
        )
        clear_composite_range(
            conn,
            table_name="macro_composite_component_daily",
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            composite_keys=composite_keys if composite_filter else None,
        )

        rows_written = 0
        logger.info(
            "Building macro composite layer: composites=%d as_of_start=%s as_of_end=%s",
            len(composite_keys),
            start_date.isoformat(),
            end_date.isoformat(),
        )
        for composite_key in composite_keys:
            component_rows, daily_rows = _component_rows_for_composite(
                composite_key=composite_key,
                policy_rows=policy_by_composite[composite_key],
                calendar_frame=calendar_frame,
                feature_frame=feature_frame,
            )
            rows_written += insert_many(
                conn,
                sql="""
                    INSERT INTO macro_composite_component_daily (
                        as_of_date, composite_key, metric_key, feature_name, ref_area, regime_block,
                        feature_event_as_of_date, standardized_value, base_weight, effective_weight,
                        normalized_weight, contribution_value, source_quality_weight, carry_forward_flag,
                        coverage_flag, required_flag, included_flag, exclusion_reason, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows=component_rows,
                chunk_size=50000,
            )
            rows_written += insert_many(
                conn,
                sql="""
                    INSERT INTO macro_composite_daily (
                        as_of_date, composite_key, composite_value_raw, composite_value_smoothed,
                        expected_component_count, available_component_count, required_component_count,
                        available_required_count, coverage_ratio, required_coverage_ratio,
                        effective_weight_sum, smoothing_window_days, coverage_flag, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows=daily_rows,
                chunk_size=50000,
            )
            logger.info(
                "Built composite rows for composite_key=%s component_rows=%d daily_rows=%d",
                composite_key,
                len(component_rows),
                len(daily_rows),
            )

        finish_serving_run(
            conn,
            serving_run_id=serving_run_id,
            status="completed",
            rows_written=rows_written,
            notes=f"Composite layer built for {len(composite_keys)} composites.",
        )
        logger.info(
            "Macro composite build complete: serving_run_id=%s rows_written=%d composites=%d",
            serving_run_id,
            rows_written,
            len(composite_keys),
        )
    except BaseException as exc:
        if run_started:
            fail_notes = f"Composite layer failed: {type(exc).__name__}: {exc}"
            try:
                finish_serving_run(
                    conn,
                    serving_run_id=serving_run_id,
                    status="failed",
                    rows_written=rows_written,
                    notes=fail_notes,
                )
            except Exception:
                logger.exception("Failed to record failed composite layer run for serving_run_id=%s", serving_run_id)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()

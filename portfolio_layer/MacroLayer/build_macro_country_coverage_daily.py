#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sqlite3
import uuid
from collections import defaultdict
from datetime import date
from pathlib import Path

from macro_policy import load_metric_policy, required_for_country_class
from macro_raw_config import cfg_get, configure_pipeline_logging, connect_sqlite, load_macro_raw_config, resolve_db_path, resolve_path, utc_now_iso
from macro_serving_common import load_country_rows, parse_calendar_date, resolve_calendar_bounds, resolve_serving_db_path
from macro_serving_storage import clear_country_coverage_range, finish_serving_run, init_db, insert_many, start_serving_run

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Materialize daily country coverage and confidence inputs from the PIT table.")
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--raw-db-path", type=Path, default=None, help="Optional raw SQLite path override.")
    parser.add_argument("--serving-db-path", type=Path, default=None, help="Optional serving SQLite path override.")
    parser.add_argument("--policy-csv", type=Path, default=None, help="Optional metric policy CSV override.")
    parser.add_argument("--start-date", type=str, default=None, help="Optional coverage start YYYY-MM-DD override.")
    parser.add_argument("--end-date", type=str, default=None, help="Optional coverage end YYYY-MM-DD override.")
    return parser.parse_args()


def _load_calendar_dates(serving_conn: sqlite3.Connection, *, start_date: str, end_date: str) -> list[str]:
    rows = serving_conn.execute(
        """
        SELECT as_of_date
        FROM macro_calendar_daily
        WHERE as_of_date BETWEEN ? AND ?
        ORDER BY as_of_date
        """,
        (start_date, end_date),
    ).fetchall()
    return [str(row["as_of_date"]) for row in rows]


def _load_metric_keys_by_ref_area(raw_conn: sqlite3.Connection) -> dict[str, list[str]]:
    rows = raw_conn.execute(
        """
        SELECT ref_area, metric_key
        FROM macro_metric_registry
        WHERE enabled = 1
        ORDER BY ref_area, metric_key
        """
    ).fetchall()
    out: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        out[str(row["ref_area"] or "")].append(str(row["metric_key"]))
    return out


def _load_pit_status_by_ref_area(
    serving_conn: sqlite3.Connection,
    *,
    ref_area: str,
    start_date: str,
    end_date: str,
) -> dict[str, dict[str, tuple[int, float]]]:
    rows = serving_conn.execute(
        """
        SELECT as_of_date, metric_key, coverage_flag, source_quality_weight
        FROM macro_observation_daily_pit
        WHERE ref_area = ?
          AND as_of_date BETWEEN ? AND ?
        """,
        (ref_area, start_date, end_date),
    ).fetchall()
    out: dict[str, dict[str, tuple[int, float]]] = defaultdict(dict)
    for row in rows:
        out[str(row["as_of_date"])][str(row["metric_key"])] = (
            int(row["coverage_flag"] or 0),
            float(row["source_quality_weight"] or 0.0),
        )
    return out


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, cfg = load_macro_raw_config(args.config)
    raw_db_path = resolve_db_path(cfg, config_path, override=args.raw_db_path)
    serving_db_path = resolve_serving_db_path(cfg, config_path, override=args.serving_db_path)
    policy_path = args.policy_csv or resolve_path(
        config_path,
        str(cfg_get(cfg, "metric_policy_csv", default="MacroLayer/macro_metric_policy.csv")),
    )
    if policy_path is None:
        raise ValueError("macro_raw.metric_policy_csv is required for country coverage builds.")

    raw_conn = connect_sqlite(raw_db_path, row_factory=sqlite3.Row)
    serving_conn = connect_sqlite(serving_db_path, row_factory=sqlite3.Row)
    serving_run_id = uuid.uuid4().hex
    run_started = False
    rows_written = 0
    try:
        init_db(serving_conn)
        start_date, end_date, raw_ingest_run_id = resolve_calendar_bounds(
            raw_conn,
            cfg=cfg,
            config_path=config_path,
            start_override=args.start_date,
            end_override=args.end_date,
        )
        as_of_dates = _load_calendar_dates(
            serving_conn,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
        )
        if not as_of_dates:
            raise ValueError(
                "macro_calendar_daily is empty for the requested range. Run build_macro_calendar_daily.py first."
            )
        policies = load_metric_policy(policy_path)
        countries = load_country_rows(raw_conn)
        metric_keys_by_ref_area = _load_metric_keys_by_ref_area(raw_conn)
        start_serving_run(
            serving_conn,
            serving_run_id=serving_run_id,
            build_step="country_coverage_daily",
            raw_ingest_run_id=raw_ingest_run_id,
            as_of_start_date=start_date.isoformat(),
            as_of_end_date=end_date.isoformat(),
            metric_count=len(countries),
            notes="Building daily country coverage from PIT.",
        )
        run_started = True
        clear_country_coverage_range(
            serving_conn,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
        )
        now_iso = utc_now_iso()
        rows_to_insert: list[tuple] = []
        missing_policy_pairs: set[tuple[str, str]] = set()
        for country in countries:
            ref_area = str(country["oecd_ref_area"] or country["ref_area"] or "")
            country_class = str(country["country_class"] or "")
            metric_policy_pairs: list[tuple[str, object]] = []
            for key in metric_keys_by_ref_area.get(ref_area, []):
                policy = policies.get(key)
                if policy is None:
                    missing_policy_pairs.add((ref_area, key))
                    continue
                metric_policy_pairs.append((key, policy))
            metric_keys = [key for key, _ in metric_policy_pairs]
            required_metric_keys = [
                key for key, policy in metric_policy_pairs if required_for_country_class(policy, country_class)
            ]
            pit_status = _load_pit_status_by_ref_area(
                serving_conn,
                ref_area=ref_area,
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
            )
            expected_metric_count = len(metric_keys)
            required_metric_count = len(required_metric_keys)
            for as_of_date in as_of_dates:
                status_for_date = pit_status.get(as_of_date, {})
                available_metric_count = 0
                available_required_count = 0
                source_quality_total = 0.0
                for metric_key in metric_keys:
                    metric_status = status_for_date.get(metric_key)
                    is_available = bool(metric_status and metric_status[0])
                    if is_available:
                        available_metric_count += 1
                        source_quality_total += float(metric_status[1])
                    if metric_key in required_metric_keys and is_available:
                        available_required_count += 1
                stale_metric_count = expected_metric_count - available_metric_count
                coverage_ratio = round(available_metric_count / expected_metric_count, 6) if expected_metric_count else None
                required_coverage_ratio = round(available_required_count / required_metric_count, 6) if required_metric_count else 1.0
                source_quality_score = round(source_quality_total / available_metric_count, 6) if available_metric_count else 0.0
                coverage_flag = 1 if expected_metric_count > 0 and available_required_count == required_metric_count else 0
                rows_to_insert.append(
                    (
                        as_of_date,
                        str(country["ticker"]),
                        ref_area,
                        country_class or None,
                        expected_metric_count,
                        available_metric_count,
                        required_metric_count,
                        available_required_count,
                        stale_metric_count,
                        coverage_ratio,
                        required_coverage_ratio,
                        source_quality_score,
                        coverage_flag,
                        now_iso,
                    )
                )
        if missing_policy_pairs:
            logger.warning(
                "Skipped %d ref_area/metric_key pair(s) in country coverage because no metric policy row was found: %s",
                len(missing_policy_pairs),
                sorted(missing_policy_pairs),
            )
        rows_written = insert_many(
            serving_conn,
            """
            INSERT INTO macro_country_coverage_daily (
                as_of_date, ticker, ref_area, country_class, expected_metric_count,
                available_metric_count, required_metric_count, available_required_count,
                stale_metric_count, coverage_ratio, required_coverage_ratio,
                source_quality_score, coverage_flag, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows_to_insert,
        )
        finish_serving_run(
            serving_conn,
            serving_run_id=serving_run_id,
            status="completed",
            rows_written=rows_written,
            notes="Daily country coverage built from PIT.",
        )
        logger.info(
            "Macro country coverage built: serving_run_id=%s rows_written=%d countries=%d",
            serving_run_id,
            rows_written,
            len(countries),
        )
    except BaseException as exc:
        if run_started:
            fail_notes = f"Country coverage build failed: {type(exc).__name__}: {exc}"
            try:
                finish_serving_run(
                    serving_conn,
                    serving_run_id=serving_run_id,
                    status="failed",
                    rows_written=rows_written,
                    notes=fail_notes,
                )
            except Exception:
                logger.exception("Failed to record failed country coverage run for serving_run_id=%s", serving_run_id)
        raise
    finally:
        raw_conn.close()
        serving_conn.close()


if __name__ == "__main__":
    main()

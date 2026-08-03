#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sqlite3
import uuid
from datetime import date, timedelta
from pathlib import Path

from macro_raw_config import configure_pipeline_logging, connect_sqlite, load_macro_raw_config, resolve_db_path, utc_now_iso
from macro_serving_common import daterange, resolve_calendar_bounds, resolve_serving_db_path
from macro_serving_storage import clear_table, finish_serving_run, init_db, insert_many, start_serving_run

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the daily macro serving calendar.")
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--raw-db-path", type=Path, default=None, help="Optional raw SQLite path override.")
    parser.add_argument("--serving-db-path", type=Path, default=None, help="Optional serving SQLite path override.")
    parser.add_argument("--start-date", type=str, default=None, help="Optional calendar start YYYY-MM-DD override.")
    parser.add_argument("--end-date", type=str, default=None, help="Optional calendar end YYYY-MM-DD override.")
    return parser.parse_args()


def _is_month_end(value: date) -> bool:
    return (value + timedelta(days=1)).month != value.month


def _is_quarter_end(value: date) -> bool:
    return _is_month_end(value) and value.month in {3, 6, 9, 12}


def build_calendar_rows(start_date: date, end_date: date) -> list[tuple[str, int, int, int, int, str]]:
    now = utc_now_iso()
    rows: list[tuple[str, int, int, int, int, str]] = []
    for current in daterange(start_date, end_date):
        rows.append(
            (
                current.isoformat(),
                current.weekday(),
                1 if _is_month_end(current) else 0,
                1 if _is_quarter_end(current) else 0,
                1 if (current.month == 12 and current.day == 31) else 0,
                now,
            )
        )
    return rows


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, cfg = load_macro_raw_config(args.config)
    raw_db_path = resolve_db_path(cfg, config_path, override=args.raw_db_path)
    serving_db_path = resolve_serving_db_path(cfg, config_path, override=args.serving_db_path)
    serving_db_path.parent.mkdir(parents=True, exist_ok=True)

    raw_conn = connect_sqlite(raw_db_path, row_factory=sqlite3.Row)
    serving_conn = connect_sqlite(serving_db_path)
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
        start_serving_run(
            serving_conn,
            serving_run_id=serving_run_id,
            build_step="calendar",
            raw_ingest_run_id=raw_ingest_run_id,
            as_of_start_date=start_date.isoformat(),
            as_of_end_date=end_date.isoformat(),
            metric_count=0,
            notes="Building macro daily serving calendar.",
        )
        run_started = True
        rows = build_calendar_rows(start_date, end_date)
        if args.start_date is None and args.end_date is None:
            clear_table(serving_conn, "macro_calendar_daily")
        else:
            serving_conn.execute(
                "DELETE FROM macro_calendar_daily WHERE as_of_date BETWEEN ? AND ?",
                (start_date.isoformat(), end_date.isoformat()),
            )
            serving_conn.commit()
        rows_written = insert_many(
            serving_conn,
            """
            INSERT INTO macro_calendar_daily (
                as_of_date, weekday, month_end_flag, quarter_end_flag, year_end_flag, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        finish_serving_run(
            serving_conn,
            serving_run_id=serving_run_id,
            status="completed",
            rows_written=rows_written,
            notes=f"Calendar rows built for {start_date.isoformat()} to {end_date.isoformat()}.",
        )
        logger.info(
            "Macro serving calendar built: serving_run_id=%s start_date=%s end_date=%s rows_written=%d",
            serving_run_id,
            start_date.isoformat(),
            end_date.isoformat(),
            rows_written,
        )
    except BaseException as exc:
        if run_started:
            fail_notes = f"Calendar build failed: {type(exc).__name__}: {exc}"
            try:
                finish_serving_run(
                    serving_conn,
                    serving_run_id=serving_run_id,
                    status="failed",
                    rows_written=rows_written,
                    notes=fail_notes,
                )
            except Exception:
                logger.exception("Failed to record failed calendar run for serving_run_id=%s", serving_run_id)
        raise
    finally:
        raw_conn.close()
        serving_conn.close()


if __name__ == "__main__":
    main()

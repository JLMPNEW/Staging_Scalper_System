#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import uuid
from pathlib import Path

from macro_raw_config import configure_pipeline_logging, connect_sqlite, load_macro_raw_config
from macro_serving_common import resolve_serving_db_path
from macro_serving_storage import clear_table, finish_serving_run, init_db, start_serving_run

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Materialize the latest macro metric snapshot from the PIT table.")
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--serving-db-path", type=Path, default=None, help="Optional serving SQLite path override.")
    return parser.parse_args()


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, cfg = load_macro_raw_config(args.config)
    serving_db_path = resolve_serving_db_path(cfg, config_path, override=args.serving_db_path)
    conn = connect_sqlite(serving_db_path)
    serving_run_id = uuid.uuid4().hex
    run_started = False
    rows_written = 0
    try:
        init_db(conn)
        start_serving_run(
            conn,
            serving_run_id=serving_run_id,
            build_step="metric_latest",
            raw_ingest_run_id=None,
            as_of_start_date=None,
            as_of_end_date=None,
            metric_count=0,
            notes="Materializing latest metric snapshot from PIT.",
        )
        run_started = True
        clear_table(conn, "macro_metric_latest")
        conn.execute(
            """
            INSERT INTO macro_metric_latest (
                metric_key, as_of_date, registry_key, ref_area, source_name, source_series_id,
                frequency, value_selected, observation_period_selected, observation_date_selected,
                release_date_selected, vintage_date_selected, effective_available_date_selected,
                staleness_days, max_staleness_days, source_quality_weight,
                carry_forward_allowed, carry_forward_flag, coverage_flag, updated_at_utc
            )
            WITH ranked AS (
                SELECT
                    *,
                    ROW_NUMBER() OVER (
                        PARTITION BY metric_key
                        ORDER BY as_of_date DESC
                    ) AS rn
                FROM macro_observation_daily_pit
            )
            SELECT
                metric_key, as_of_date, registry_key, ref_area, source_name, source_series_id,
                frequency, value_selected, observation_period_selected, observation_date_selected,
                release_date_selected, vintage_date_selected, effective_available_date_selected,
                staleness_days, max_staleness_days, source_quality_weight,
                carry_forward_allowed, carry_forward_flag, coverage_flag, updated_at_utc
            FROM ranked
            WHERE rn = 1
            """
        )
        rows_written = int(conn.execute("SELECT COUNT(*) FROM macro_metric_latest").fetchone()[0] or 0)
        conn.commit()
        finish_serving_run(
            conn,
            serving_run_id=serving_run_id,
            status="completed",
            rows_written=rows_written,
            notes="Latest metric snapshot built from PIT.",
        )
        logger.info("Macro metric latest built: serving_run_id=%s rows_written=%d", serving_run_id, rows_written)
    except BaseException as exc:
        if run_started:
            fail_notes = f"Metric latest build failed: {type(exc).__name__}: {exc}"
            try:
                finish_serving_run(
                    conn,
                    serving_run_id=serving_run_id,
                    status="failed",
                    rows_written=rows_written,
                    notes=fail_notes,
                )
            except Exception:
                logger.exception("Failed to record failed metric_latest run for serving_run_id=%s", serving_run_id)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()

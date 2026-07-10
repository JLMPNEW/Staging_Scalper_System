#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from macro_policy import MetricPolicy, load_metric_policy
from macro_raw_config import cfg_get, configure_pipeline_logging, connect_sqlite, load_macro_raw_config, resolve_db_path, resolve_path, utc_now_iso
from macro_serving_common import (
    RELEASE_STALENESS_POLICY_VERSION,
    RawCandidate,
    candidate_rank,
    effective_available_date,
    freshness_anchor_date,
    load_metric_serving_specs,
    parse_calendar_date,
    release_staleness_days,
    resolve_calendar_bounds,
    resolve_serving_db_path,
    select_latest_completed_ingest_run,
)
from macro_serving_storage import clear_pit_range, finish_serving_run, init_db, insert_many, start_serving_run

logger = logging.getLogger(__name__)

_AVAILABILITY_EXPR = "MAX(COALESCE(o.vintage_date,''), COALESCE(o.release_date,''), COALESCE(o.observation_date,''))"


@dataclass(frozen=True)
class MetricTask:
    metric_key: str
    ref_area: str
    frequency: str
    observation_count: int
    policy: MetricPolicy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Materialize the daily point-in-time macro observation table.")
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--raw-db-path", type=Path, default=None, help="Optional raw SQLite path override.")
    parser.add_argument("--serving-db-path", type=Path, default=None, help="Optional serving SQLite path override.")
    parser.add_argument("--policy-csv", type=Path, default=None, help="Optional metric policy CSV override.")
    parser.add_argument("--start-date", type=str, default=None, help="Optional PIT start YYYY-MM-DD override.")
    parser.add_argument("--end-date", type=str, default=None, help="Optional PIT end YYYY-MM-DD override.")
    parser.add_argument("--metric-keys", nargs="*", default=None, help="Optional metric_key filter.")
    parser.add_argument("--workers", type=int, default=0, help="Optional worker count override for per-metric PIT builds.")
    return parser.parse_args()


def _load_calendar_dates(serving_conn: sqlite3.Connection, *, start_date: str, end_date: str) -> list[date]:
    rows = serving_conn.execute(
        """
        SELECT as_of_date
        FROM macro_calendar_daily
        WHERE as_of_date BETWEEN ? AND ?
        ORDER BY as_of_date
        """,
        (start_date, end_date),
    ).fetchall()
    return [parse_calendar_date(str(row["as_of_date"])) for row in rows if parse_calendar_date(str(row["as_of_date"])) is not None]


def _raw_candidate_from_row(row: sqlite3.Row) -> RawCandidate | None:
    observation_date = parse_calendar_date(str(row["observation_date"] or row["observation_period"] or ""))
    release_date = parse_calendar_date(str(row["release_date"] or ""))
    vintage_date = parse_calendar_date(str(row["vintage_date"] or ""))
    available_date = effective_available_date(
        observation_date=observation_date,
        release_date=release_date,
        vintage_date=vintage_date,
    )
    if available_date is None:
        return None
    return RawCandidate(
        registry_key=str(row["registry_key"]),
        metric_key=str(row["metric_key"]),
        ref_area=str(row["ref_area"] or ""),
        source_name=str(row["source_name"] or ""),
        source_series_id=str(row["source_series_id"] or "") or None,
        frequency=str(row["frequency"] or ""),
        observation_period=str(row["observation_period"] or ""),
        observation_date=observation_date,
        observation_date_text=str(row["observation_date"] or "") or None,
        release_date=release_date,
        release_date_text=str(row["release_date"] or "") or None,
        vintage_date=vintage_date,
        vintage_date_text=str(row["vintage_date"] or "") or None,
        effective_available_date=available_date,
        effective_available_date_text=available_date.isoformat(),
        value=float(row["value"]),
        retrieved_at=str(row["retrieved_at"] or ""),
        source_priority=int(row["source_priority"] or 0),
    )


def _iter_metric_candidates(raw_conn: sqlite3.Connection, metric_key: str, end_date: str):
    cursor = raw_conn.execute(
        f"""
        SELECT
            o.registry_key,
            o.metric_key,
            o.ref_area,
            o.source_name,
            o.source_series_id,
            o.frequency,
            o.observation_period,
            o.observation_date,
            o.release_date,
            o.vintage_date,
            o.value,
            o.retrieved_at,
            r.source_priority
        FROM macro_observation_raw o
        JOIN macro_metric_registry r
          ON r.registry_key = o.registry_key
        WHERE r.enabled = 1
          AND o.metric_key = ?
          AND {_AVAILABILITY_EXPR} != ''
          AND {_AVAILABILITY_EXPR} <= ?
        ORDER BY
            {_AVAILABILITY_EXPR} ASC,
            COALESCE(o.vintage_date, '') ASC,
            COALESCE(o.release_date, '') ASC,
            COALESCE(o.observation_date, '') ASC,
            r.source_priority ASC,
            o.retrieved_at ASC
        """,
        (metric_key, end_date),
    )
    for row in cursor:
        candidate = _raw_candidate_from_row(row)
        if candidate is not None:
            yield candidate


def _null_pit_row(metric: MetricTask, as_of: date, now_iso: str) -> tuple:
    return (
        as_of.isoformat(),
        metric.metric_key,
        None,
        metric.ref_area,
        None,
        None,
        metric.frequency,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        metric.policy.max_staleness_days,
        metric.policy.source_quality_weight,
        1 if metric.policy.carry_forward_allowed else 0,
        0,
        0,
        now_iso,
    )


def _pit_row(metric: MetricTask, as_of: date, candidate: RawCandidate, now_iso: str) -> tuple:
    anchor_date = freshness_anchor_date(candidate, metric.policy)
    staleness_days = release_staleness_days(
        as_of=as_of,
        anchor=anchor_date,
        frequency=metric.frequency or metric.policy.frequency,
    )
    carry_forward_flag = 1 if as_of > candidate.effective_available_date else 0
    within_staleness = staleness_days <= metric.policy.max_staleness_days
    coverage_flag = 1 if within_staleness and (metric.policy.carry_forward_allowed or carry_forward_flag == 0) else 0
    return (
        as_of.isoformat(),
        metric.metric_key,
        candidate.registry_key,
        candidate.ref_area,
        candidate.source_name,
        candidate.source_series_id,
        metric.frequency,
        candidate.value,
        candidate.observation_period,
        candidate.observation_date_text,
        candidate.release_date_text,
        candidate.vintage_date_text,
        candidate.effective_available_date_text,
        staleness_days,
        metric.policy.max_staleness_days,
        metric.policy.source_quality_weight,
        1 if metric.policy.carry_forward_allowed else 0,
        carry_forward_flag,
        coverage_flag,
        now_iso,
    )


def build_metric_pit_rows(
    *,
    raw_db_path: Path,
    metric: MetricTask,
    as_of_dates: list[date],
    end_date: str,
) -> list[tuple]:
    raw_conn = connect_sqlite(raw_db_path, row_factory=sqlite3.Row)
    now_iso = utc_now_iso()
    try:
        candidate_iter = _iter_metric_candidates(raw_conn, metric.metric_key, end_date)
        next_candidate = next(candidate_iter, None)
        current_best: RawCandidate | None = None
        rows: list[tuple] = []
        for as_of in as_of_dates:
            while next_candidate is not None and next_candidate.effective_available_date <= as_of:
                if current_best is None or candidate_rank(next_candidate) > candidate_rank(current_best):
                    current_best = next_candidate
                next_candidate = next(candidate_iter, None)
            if current_best is None:
                rows.append(_null_pit_row(metric, as_of, now_iso))
            else:
                rows.append(_pit_row(metric, as_of, current_best, now_iso))
        return rows
    finally:
        raw_conn.close()


def _load_metric_tasks(
    raw_conn: sqlite3.Connection,
    *,
    policies: dict[str, MetricPolicy],
    metric_filter: set[str] | None,
) -> tuple[list[MetricTask], list[str]]:
    specs = load_metric_serving_specs(raw_conn)
    tasks: list[MetricTask] = []
    skipped_metric_keys: list[str] = []
    for spec in specs:
        if metric_filter and spec.metric_key not in metric_filter:
            continue
        policy = policies.get(spec.metric_key)
        if policy is None:
            logger.warning(
                "Skipping PIT build for metric_key=%s because no metric policy row was found.",
                spec.metric_key,
            )
            skipped_metric_keys.append(spec.metric_key)
            continue
        tasks.append(
            MetricTask(
                metric_key=spec.metric_key,
                ref_area=spec.ref_area,
                frequency=spec.frequency,
                observation_count=spec.observation_count,
                policy=policy,
            )
        )
    tasks.sort(key=lambda item: (-item.observation_count, item.metric_key))
    return tasks, skipped_metric_keys


def resolve_worker_count(cfg: dict, override: int, task_count: int) -> int:
    if override and override > 0:
        return max(1, min(override, task_count))
    configured = int(cfg_get(cfg, "serving", "pit_workers", default=2))
    return max(1, min(configured, task_count))


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, cfg = load_macro_raw_config(args.config)
    configured_staleness_policy = str(
        cfg_get(
            cfg,
            "serving",
            "staleness_policy_version",
            default=RELEASE_STALENESS_POLICY_VERSION,
        )
    ).strip()
    if configured_staleness_policy != RELEASE_STALENESS_POLICY_VERSION:
        raise ValueError(
            "Unsupported macro_raw.serving.staleness_policy_version="
            f"{configured_staleness_policy!r}; expected {RELEASE_STALENESS_POLICY_VERSION!r}."
        )
    raw_db_path = resolve_db_path(cfg, config_path, override=args.raw_db_path)
    serving_db_path = resolve_serving_db_path(cfg, config_path, override=args.serving_db_path)
    policy_path = args.policy_csv or resolve_path(
        config_path,
        str(cfg_get(cfg, "metric_policy_csv", default="MacroLayer/macro_metric_policy.csv")),
    )
    if policy_path is None:
        raise ValueError("macro_raw.metric_policy_csv is required for serving PIT builds.")

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
        metric_filter = {str(item).strip() for item in (args.metric_keys or []) if str(item).strip()}
        tasks, skipped_metric_keys = _load_metric_tasks(raw_conn, policies=policies, metric_filter=metric_filter or None)
        if not tasks:
            raise ValueError("No PIT build tasks matched the requested metric universe after policy filtering.")
        worker_count = resolve_worker_count(cfg, args.workers, len(tasks))
        latest_ingest_run = select_latest_completed_ingest_run(raw_conn)
        start_serving_run(
            serving_conn,
            serving_run_id=serving_run_id,
            build_step="observation_daily_pit",
            raw_ingest_run_id=latest_ingest_run.run_id if latest_ingest_run is not None else raw_ingest_run_id,
            as_of_start_date=start_date.isoformat(),
            as_of_end_date=end_date.isoformat(),
            metric_count=len(tasks),
            notes=(
                f"Building PIT rows with workers={worker_count}. "
                f"Staleness policy={configured_staleness_policy}. "
                f"Skipped metrics without policy={len(skipped_metric_keys)}."
            ),
        )
        run_started = True
        clear_pit_range(
            serving_conn,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            metric_keys=[item.metric_key for item in tasks] if metric_filter else None,
        )
        logger.info(
            "Building macro PIT table: metrics=%d as_of_start=%s as_of_end=%s workers=%d",
            len(tasks),
            start_date.isoformat(),
            end_date.isoformat(),
            worker_count,
        )
        if skipped_metric_keys:
            logger.warning(
                "Skipped %d metric(s) with no metric policy: %s",
                len(skipped_metric_keys),
                skipped_metric_keys,
            )
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(
                    build_metric_pit_rows,
                    raw_db_path=raw_db_path,
                    metric=task,
                    as_of_dates=as_of_dates,
                    end_date=end_date.isoformat(),
                ): task
                for task in tasks
            }
            failed_metric_keys: list[str] = []
            for future in as_completed(futures):
                task = futures[future]
                try:
                    metric_rows = future.result()
                except Exception as exc:
                    logger.exception("PIT build failed for metric_key=%s: %s", task.metric_key, exc)
                    failed_metric_keys.append(task.metric_key)
                    continue
                rows_written += insert_many(
                    serving_conn,
                    """
                    INSERT INTO macro_observation_daily_pit (
                        as_of_date, metric_key, registry_key, ref_area, source_name, source_series_id,
                        frequency, value_selected, observation_period_selected, observation_date_selected,
                        release_date_selected, vintage_date_selected, effective_available_date_selected,
                        staleness_days, max_staleness_days, source_quality_weight,
                        carry_forward_allowed, carry_forward_flag, coverage_flag, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    metric_rows,
                )
                logger.info(
                    "Built PIT rows for metric_key=%s rows=%d raw_observation_count=%d",
                    task.metric_key,
                    len(metric_rows),
                    task.observation_count,
                )
        if failed_metric_keys:
            raise RuntimeError(
                f"PIT build failed for {len(failed_metric_keys)} metric(s): {sorted(failed_metric_keys)}"
            )
        finish_serving_run(
            serving_conn,
            serving_run_id=serving_run_id,
            status="completed",
            rows_written=rows_written,
            notes=(
                f"PIT rows built for {len(tasks)} metrics. "
                f"Staleness policy={configured_staleness_policy}. "
                f"Skipped metrics without policy={len(skipped_metric_keys)}."
            ),
        )
        logger.info(
            "Macro PIT build complete: serving_run_id=%s rows_written=%d metrics=%d",
            serving_run_id,
            rows_written,
            len(tasks),
        )
    except BaseException as exc:
        if run_started:
            fail_notes = f"PIT build failed: {type(exc).__name__}: {exc}"
            try:
                finish_serving_run(
                    serving_conn,
                    serving_run_id=serving_run_id,
                    status="failed",
                    rows_written=rows_written,
                    notes=fail_notes,
                )
            except Exception:
                logger.exception("Failed to record failed PIT run for serving_run_id=%s", serving_run_id)
        raise
    finally:
        raw_conn.close()
        serving_conn.close()


if __name__ == "__main__":
    main()

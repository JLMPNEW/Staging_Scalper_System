#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from connectors.eia_seriesid import EiaSeriesIdConnector
from connectors.fred_alfred import FredAlfredConnector
from connectors.imf_sdmx import ImfSdmxConnector
from connectors.oecd_sdmx import OecdSdmxConnector
from connectors.phillyfed_ads import PhillyFedAdsConnector
from connectors.sdmx_csv import SdmxCsvConnector
from macro_http import HttpClient, RateLimiter, RequestSettings
from macro_raw_config import (
    cfg_get,
    configure_pipeline_logging,
    connect_sqlite,
    getenv_str,
    load_macro_raw_config,
    parse_boolish,
    parse_iso_date,
    previous_or_same_business_day,
    resolve_db_path,
    resolve_path,
)
from macro_registry import enabled_specs, filter_specs_by_sources, load_metric_registry
from macro_storage import (
    finish_run,
    init_db,
    load_sync_state,
    repair_true_vintage_dedupe_keys,
    seed_country_metadata,
    seed_release_calendar,
    start_run,
    upsert_metric_registry,
    write_fetch_result,
)
from macro_types import FetchResult, FetchTask

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run stage-1 macro raw ingestion with parallel fetch and single-writer SQLite upserts."
    )
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument(
        "--mode",
        choices=["daily", "weekly", "monthly", "quarterly", "backfill"],
        default=None,
        help="Optional run mode override.",
    )
    parser.add_argument("--as-of-date", type=str, default=None, help="Optional YYYY-MM-DD as-of date.")
    parser.add_argument(
        "--history-start-date",
        type=str,
        default=None,
        help="Optional global observation history start YYYY-MM-DD override, useful for 25-year backfills.",
    )
    parser.add_argument("--db-path", type=Path, default=None, help="Optional SQLite DB path override.")
    parser.add_argument("--sources", nargs="*", default=None, help="Optional source filter.")
    parser.add_argument("--limit", type=int, default=0, help="Optional limit on registry rows after filtering.")
    parser.add_argument("--dry-run", action="store_true", help="Build tasks and print counts without fetching data.")
    return parser.parse_args()


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, cfg = load_macro_raw_config(args.config)
    db_path = resolve_db_path(cfg, config_path, override=args.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = connect_sqlite(db_path)
    run_started = False
    rows_written = 0
    error_count = 0
    run_id = uuid.uuid4().hex
    try:
        init_db(conn)
        registry_csv = resolve_path(
            config_path,
            str(cfg_get(cfg, "registry_csv", default="MacroLayer/macro_metric_registry_seed.csv")),
        )
        if registry_csv is None:
            raise ValueError("macro_raw.registry_csv is required.")
        all_specs = load_metric_registry(registry_csv)
        upsert_metric_registry(conn, all_specs)
        if not args.dry_run:
            repair_true_vintage_dedupe_keys(conn)
        specs = enabled_specs(all_specs)
        specs = filter_specs_by_sources(specs, set(args.sources or []))
        if args.limit and args.limit > 0:
            specs = specs[: args.limit]
        seed_country_metadata(conn, resolve_path(config_path, cfg_get(cfg, "country_metadata_csv", default=None)))
        seed_release_calendar(conn, resolve_path(config_path, cfg_get(cfg, "release_calendar_csv", default=None)))
        state = load_sync_state(conn)

        mode = str(args.mode or cfg_get(cfg, "run_mode", default="daily")).strip().lower()
        as_of_date = previous_or_same_business_day(
            parse_iso_date(args.as_of_date) or parse_iso_date(cfg_get(cfg, "as_of_date", default=None)) or date.today()
        )
        history_start_override = parse_iso_date(args.history_start_date)
        tasks = build_fetch_tasks(
            specs=specs,
            state=state,
            as_of_date=as_of_date,
            mode=mode,
            history_start_override=history_start_override,
        )
        grouped = group_tasks_by_source(tasks)
        logger.info(
            "Macro raw task plan: mode=%s as_of_date=%s registry_rows=%d task_count=%d sources=%d",
            mode,
            as_of_date.isoformat(),
            len(specs),
            len(tasks),
            len(grouped),
        )
        for source_name, source_tasks in grouped.items():
            logger.info("Source %s -> %d task(s)", source_name, len(source_tasks))
        start_run(
            conn,
            run_id=run_id,
            mode=mode,
            as_of_date=as_of_date.isoformat(),
            source_filter=",".join(args.sources) if args.sources else None,
            dry_run=bool(args.dry_run),
            task_count=len(tasks),
            source_count=len(grouped),
        )
        run_started = True
        if args.dry_run:
            finish_run(conn, run_id=run_id, status="dry_run", rows_written=0, error_count=0)
            return

        for source_name, source_tasks in grouped.items():
            connector = build_connector(source_name=source_name, cfg=cfg, config_path=config_path)
            max_workers = resolve_worker_count(cfg=cfg, source_name=source_name, task_count=len(source_tasks))
            logger.info("Running source=%s with max_workers=%d", source_name, max_workers)
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(connector.fetch_task, task): task for task in source_tasks}
                for future in as_completed(futures):
                    task = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        logger.exception("Fetch failed for registry_key=%s", task.spec.registry_key)
                        result = FetchResult(spec=task.spec, error_text=str(exc))
                    if result.error_text:
                        error_count += 1
                        logger.error(
                            "Fetch returned an error for registry_key=%s: %s",
                            task.spec.registry_key,
                            result.error_text,
                        )
                    rows_written += write_fetch_result(conn, run_id, result)
        finish_run(
            conn,
            run_id=run_id,
            status="completed" if error_count == 0 else "completed_with_errors",
            rows_written=rows_written,
            error_count=error_count,
        )
        logger.info("Macro raw pipeline complete: rows_written=%d error_count=%d", rows_written, error_count)
    except BaseException as exc:
        if run_started:
            fail_notes = f"Macro raw pipeline failed: {type(exc).__name__}: {exc}"
            try:
                finish_run(
                    conn,
                    run_id=run_id,
                    status="failed",
                    rows_written=rows_written,
                    error_count=error_count + 1,
                    notes=fail_notes,
                )
            except Exception:
                logger.exception("Failed to record failed macro raw run for run_id=%s", run_id)
        raise
    finally:
        conn.close()

    if error_count:
        raise SystemExit(f"Macro raw pipeline completed with {error_count} fetch error(s).")


def build_fetch_tasks(
    *,
    specs: list[Any],
    state: dict[str, dict[str, Any]],
    as_of_date: date,
    mode: str,
    history_start_override: date | None = None,
) -> list[FetchTask]:
    tasks: list[FetchTask] = []
    for spec in specs:
        sync = state.get(spec.registry_key, {})
        history_start = history_start_override or spec.history_start_date or cfg_default_history_start(mode)
        observation_start = history_start
        if mode != "backfill" and spec.vintage_policy != "true_vintage":
            last_obs = parse_iso_date(sync.get("last_observation_date"))
            if last_obs is not None:
                candidate = last_obs - timedelta(days=spec.revision_window_days)
                if candidate > observation_start:
                    observation_start = candidate
        # ALFRED realtime bounds are independent from observation-history bounds.
        # Truncating this range fabricates a recent vintage for old observations.
        vintage_start = date(1776, 7, 4)
        tasks.append(
            FetchTask(
                spec=spec,
                observation_start=observation_start,
                observation_end=as_of_date,
                vintage_start=vintage_start if spec.vintage_policy == "true_vintage" else None,
                as_of_date=as_of_date,
            )
        )
    return _normalize_oecd_bundle_windows(tasks)


def _oecd_bundle_identity(task: FetchTask) -> tuple[str, str, str, tuple[tuple[str, str], ...]] | None:
    if task.spec.source_name != "oecd_sdmx":
        return None
    source_params = dict(task.spec.source_params or {})
    return (
        str(source_params.pop("agency_id", "")),
        str(task.spec.source_dataset or ""),
        str(source_params.pop("dataset_version", "")),
        tuple(sorted((str(key), str(value)) for key, value in source_params.items())),
    )


def _normalize_oecd_bundle_windows(tasks: list[FetchTask]) -> list[FetchTask]:
    """Give every series in one OECD dataset request the same time window.

    OECD returns the whole dataset bundle before local series filtering. A shared
    window allows one network/cache fetch per dataset and prevents per-series
    sync-state differences from defeating bundle reuse.
    """
    starts: dict[tuple[str, str, str, tuple[tuple[str, str], ...]], date] = {}
    for task in tasks:
        identity = _oecd_bundle_identity(task)
        if identity is None or task.observation_start is None:
            continue
        starts[identity] = min(starts.get(identity, task.observation_start), task.observation_start)
    normalized: list[FetchTask] = []
    for task in tasks:
        identity = _oecd_bundle_identity(task)
        shared_start = starts.get(identity) if identity is not None else None
        if shared_start is None or shared_start == task.observation_start:
            normalized.append(task)
            continue
        normalized.append(
            FetchTask(
                spec=task.spec,
                observation_start=shared_start,
                observation_end=task.observation_end,
                vintage_start=task.vintage_start,
                as_of_date=task.as_of_date,
            )
        )
    return normalized


def cfg_default_history_start(mode: str) -> date:
    if mode == "daily":
        return date(2018, 1, 1)
    if mode == "weekly":
        return date(2010, 1, 1)
    if mode == "monthly":
        return date(2000, 1, 1)
    if mode == "quarterly":
        return date(1990, 1, 1)
    return date(1990, 1, 1)


def group_tasks_by_source(tasks: list[FetchTask]) -> dict[str, list[FetchTask]]:
    grouped: dict[str, list[FetchTask]] = {}
    for task in tasks:
        grouped.setdefault(task.spec.source_name, []).append(task)
    return grouped


def build_connector(source_name: str, cfg: dict[str, Any], config_path: Path) -> Any:
    request_settings = RequestSettings(
        timeout_seconds=int(cfg_get(cfg, "request", "timeout_seconds", default=60)),
        max_retries=int(cfg_get(cfg, "request", "max_retries", default=3)),
        backoff_base_seconds=float(cfg_get(cfg, "request", "backoff_base_seconds", default=1.0)),
        backoff_cap_seconds=float(cfg_get(cfg, "request", "backoff_cap_seconds", default=30.0)),
        user_agent=str(cfg_get(cfg, "request", "user_agent", default="macro-raw-ingestor/1.0")),
    )
    limiter = RateLimiter(float(cfg_get(cfg, "sources", source_name, "min_interval_seconds", default=0.0)))
    http_client = HttpClient(request_settings, limiter=limiter)
    if source_name == "fred_alfred":
        api_key = _required_source_api_key(cfg=cfg, source_name=source_name, default_env_name="FRED_API_KEY")
        return FredAlfredConnector(http_client=http_client, api_key=api_key)
    if source_name == "phillyfed_ads":
        page_url = str(cfg_get(cfg, "sources", "phillyfed_ads", "page_url", default="")).strip() or None
        return PhillyFedAdsConnector(http_client=http_client, page_url=page_url)
    if source_name == "sdmx_csv":
        base_url = str(cfg_get(cfg, "sources", "sdmx_csv", "base_url", default="")).strip()
        if not base_url:
            raise ValueError("macro_raw.sources.sdmx_csv.base_url is required before enabling sdmx_csv rows.")
        default_agency = str(cfg_get(cfg, "sources", "sdmx_csv", "default_agency", default="")).strip() or None
        return SdmxCsvConnector(http_client=http_client, base_url=base_url, default_agency=default_agency)
    if source_name == "oecd_sdmx":
        base_url = str(cfg_get(cfg, "sources", "oecd_sdmx", "base_url", default="https://sdmx.oecd.org/public/rest/data"))
        cache_dir = resolve_path(
            config_path,
            str(cfg_get(cfg, "sources", "oecd_sdmx", "cache_dir", default="MacroLayer/cache/oecd")),
        )
        cache_max_age_hours = float(cfg_get(cfg, "sources", "oecd_sdmx", "cache_max_age_hours", default=24.0))
        return OecdSdmxConnector(
            http_client=http_client,
            base_url=base_url,
            cache_dir=cache_dir,
            cache_max_age_hours=cache_max_age_hours,
        )
    if source_name == "eia_seriesid":
        api_key = _required_source_api_key(cfg=cfg, source_name=source_name, default_env_name="EIA_API_KEY")
        base_url = str(cfg_get(cfg, "sources", "eia_seriesid", "base_url", default="https://api.eia.gov/v2/seriesid"))
        return EiaSeriesIdConnector(http_client=http_client, api_key=api_key, base_url=base_url)
    if source_name == "imf_sdmx":
        if not parse_boolish(cfg_get(cfg, "sources", "imf_sdmx", "enabled", default=False), default=False):
            raise ValueError(
                "macro_raw.sources.imf_sdmx is disabled. Enable it and configure a valid base_url before adding IMF rows."
            )
        base_url = str(cfg_get(cfg, "sources", "imf_sdmx", "base_url", default="")).strip()
        if not base_url:
            raise ValueError("macro_raw.sources.imf_sdmx.base_url is required before enabling IMF rows.")
        return ImfSdmxConnector(http_client=http_client, base_url=base_url)
    raise ValueError(f"Unsupported source connector: {source_name}")


def resolve_worker_count(*, cfg: dict[str, Any], source_name: str, task_count: int) -> int:
    configured = int(cfg_get(cfg, "sources", source_name, "max_workers", default=1))
    if configured <= 0:
        configured = 1
    return max(1, min(configured, task_count))


def _required_env(*, env_name: str, source_name: str) -> str:
    value = getenv_str(env_name)
    if value:
        return value
    raise ValueError(f"Missing required environment variable {env_name} for source {source_name}.")


def _required_source_api_key(*, cfg: dict[str, Any], source_name: str, default_env_name: str) -> str:
    env_name = str(cfg_get(cfg, "sources", source_name, "api_key_env", default=default_env_name)).strip()
    env_value = getenv_str(env_name)
    if env_value:
        return env_value
    configured_key = str(cfg_get(cfg, "sources", source_name, "api_key", default="") or "").strip()
    if configured_key:
        raise ValueError(
            f"Literal API keys are forbidden in config for source {source_name}; "
            f"use environment variable {env_name}."
        )
    return _required_env(env_name=env_name, source_name=source_name)


if __name__ == "__main__":
    main()

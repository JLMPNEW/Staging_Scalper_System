#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import sqlite3
import uuid
from bisect import bisect_left, insort
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Iterable

from macro_feature_policy import FeaturePolicy, load_feature_policy
from macro_raw_config import (
    cfg_get,
    configure_pipeline_logging,
    connect_sqlite,
    load_macro_raw_config,
    resolve_db_path,
    resolve_path,
    utc_now_iso,
)
from macro_serving_common import (
    RawCandidate,
    candidate_rank,
    effective_available_date,
    load_metric_serving_specs,
    parse_calendar_date,
    resolve_calendar_bounds,
    resolve_serving_db_path,
    select_latest_completed_ingest_run,
)
from macro_serving_storage import (
    clear_feature_range,
    finish_serving_run,
    init_db,
    insert_many,
    start_serving_run,
)

import logging

logger = logging.getLogger(__name__)

_AVAILABILITY_EXPR = "MAX(COALESCE(o.vintage_date,''), COALESCE(o.release_date,''), COALESCE(o.observation_date,''))"


@dataclass(frozen=True)
class MetricTask:
    metric_key: str
    observation_count: int
    policy: FeaturePolicy


@dataclass(frozen=True)
class PitDailyRow:
    as_of_date: date
    as_of_date_text: str
    registry_key: str | None
    ref_area: str
    source_name: str | None
    source_series_id: str | None
    frequency: str
    raw_value_selected: float | None
    observation_period_selected: str | None
    observation_date_selected: str | None
    release_date_selected: str | None
    vintage_date_selected: str | None
    effective_available_date_selected: str | None
    staleness_days: int | None
    max_staleness_days: int | None
    source_quality_weight: float | None
    carry_forward_allowed: int
    carry_forward_flag: int
    coverage_flag: int


@dataclass(frozen=True)
class FeatureState:
    as_of_date_text: str
    raw_value_selected: float | None
    transformed_value: float | None
    sign_adjusted_value: float | None
    zscore_value: float | None
    percentile_value: float | None
    standardized_value: float | None
    registry_key: str | None
    source_name: str | None
    source_series_id: str | None
    observation_period_selected: str | None
    observation_date_selected: str | None
    release_date_selected: str | None
    vintage_date_selected: str | None
    effective_available_date_selected: str | None
    staleness_days: int | None
    max_staleness_days: int | None
    source_quality_weight: float | None
    coverage_flag: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the tier-1 macro feature layer.")
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--raw-db-path", type=Path, default=None, help="Optional raw SQLite path override.")
    parser.add_argument("--serving-db-path", type=Path, default=None, help="Optional serving SQLite path override.")
    parser.add_argument("--feature-policy-csv", type=Path, default=None, help="Optional macro feature policy CSV override.")
    parser.add_argument("--start-date", type=str, default=None, help="Optional feature start YYYY-MM-DD override.")
    parser.add_argument("--end-date", type=str, default=None, help="Optional feature end YYYY-MM-DD override.")
    parser.add_argument("--metric-keys", nargs="*", default=None, help="Optional metric_key filter.")
    parser.add_argument("--workers", type=int, default=0, help="Optional per-metric feature worker count override.")
    return parser.parse_args()


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


def _iter_metric_candidates(raw_conn: sqlite3.Connection, metric_key: str, end_date: str) -> Iterable[RawCandidate]:
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


def _group_candidates_by_date(candidates: Iterable[RawCandidate]) -> Iterable[tuple[date, list[RawCandidate]]]:
    current_date: date | None = None
    current_group: list[RawCandidate] = []
    for candidate in candidates:
        available_date = candidate.effective_available_date
        if current_date is None:
            current_date = available_date
        if available_date != current_date:
            yield current_date, current_group
            current_date = available_date
            current_group = [candidate]
        else:
            current_group.append(candidate)
    if current_date is not None and current_group:
        yield current_date, current_group


def _load_pit_rows(
    serving_conn: sqlite3.Connection,
    metric_key: str,
    *,
    start_date: str | None,
    end_date: str,
) -> list[PitDailyRow]:
    if start_date is None:
        rows = serving_conn.execute(
            """
            SELECT
                as_of_date,
                registry_key,
                ref_area,
                source_name,
                source_series_id,
                frequency,
                value_selected,
                observation_period_selected,
                observation_date_selected,
                release_date_selected,
                vintage_date_selected,
                effective_available_date_selected,
                staleness_days,
                max_staleness_days,
                source_quality_weight,
                carry_forward_allowed,
                carry_forward_flag,
                coverage_flag
            FROM macro_observation_daily_pit
            WHERE metric_key = ?
              AND as_of_date <= ?
            ORDER BY as_of_date
            """,
            (metric_key, end_date),
        ).fetchall()
    else:
        rows = serving_conn.execute(
            """
            SELECT
                as_of_date,
                registry_key,
                ref_area,
                source_name,
                source_series_id,
                frequency,
                value_selected,
                observation_period_selected,
                observation_date_selected,
                release_date_selected,
                vintage_date_selected,
                effective_available_date_selected,
                staleness_days,
                max_staleness_days,
                source_quality_weight,
                carry_forward_allowed,
                carry_forward_flag,
                coverage_flag
            FROM macro_observation_daily_pit
            WHERE metric_key = ?
              AND as_of_date BETWEEN ? AND ?
            ORDER BY as_of_date
            """,
            (metric_key, start_date, end_date),
        ).fetchall()
    out: list[PitDailyRow] = []
    for row in rows:
        as_of = parse_calendar_date(str(row["as_of_date"]))
        if as_of is None:
            continue
        out.append(
            PitDailyRow(
                as_of_date=as_of,
                as_of_date_text=as_of.isoformat(),
                registry_key=str(row["registry_key"] or "") or None,
                ref_area=str(row["ref_area"] or ""),
                source_name=str(row["source_name"] or "") or None,
                source_series_id=str(row["source_series_id"] or "") or None,
                frequency=str(row["frequency"] or ""),
                raw_value_selected=float(row["value_selected"]) if row["value_selected"] is not None else None,
                observation_period_selected=str(row["observation_period_selected"] or "") or None,
                observation_date_selected=str(row["observation_date_selected"] or "") or None,
                release_date_selected=str(row["release_date_selected"] or "") or None,
                vintage_date_selected=str(row["vintage_date_selected"] or "") or None,
                effective_available_date_selected=str(row["effective_available_date_selected"] or "") or None,
                staleness_days=int(row["staleness_days"]) if row["staleness_days"] is not None else None,
                max_staleness_days=int(row["max_staleness_days"]) if row["max_staleness_days"] is not None else None,
                source_quality_weight=float(row["source_quality_weight"]) if row["source_quality_weight"] is not None else None,
                carry_forward_allowed=int(row["carry_forward_allowed"] or 0),
                carry_forward_flag=int(row["carry_forward_flag"] or 0),
                coverage_flag=int(row["coverage_flag"] or 0),
            )
        )
    return out


def _build_level_events_from_pit(pit_rows: list[PitDailyRow], policy: FeaturePolicy) -> list[FeatureState]:
    events: list[FeatureState] = []
    for pit_row in pit_rows:
        state = FeatureState(
            as_of_date_text=pit_row.as_of_date_text,
            raw_value_selected=pit_row.raw_value_selected,
            transformed_value=pit_row.raw_value_selected,
            sign_adjusted_value=(pit_row.raw_value_selected * policy.sign_multiplier) if pit_row.raw_value_selected is not None else None,
            zscore_value=None,
            percentile_value=None,
            standardized_value=None,
            registry_key=pit_row.registry_key,
            source_name=pit_row.source_name,
            source_series_id=pit_row.source_series_id,
            observation_period_selected=pit_row.observation_period_selected,
            observation_date_selected=pit_row.observation_date_selected,
            release_date_selected=pit_row.release_date_selected,
            vintage_date_selected=pit_row.vintage_date_selected,
            effective_available_date_selected=pit_row.effective_available_date_selected,
            staleness_days=pit_row.staleness_days,
            max_staleness_days=pit_row.max_staleness_days,
            source_quality_weight=pit_row.source_quality_weight,
            coverage_flag=pit_row.coverage_flag,
        )
        if not events or _state_identity(state) != _state_identity(events[-1]):
            events.append(state)
    return events


def _subtract_months(value: date, months: int) -> date:
    month_index = value.year * 12 + (value.month - 1) - months
    year = month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def _lookup_lag_candidate(
    *,
    policy: FeaturePolicy,
    current_period: date,
    best_by_period: dict[date, RawCandidate],
    sorted_periods: list[date],
) -> RawCandidate | None:
    lag = policy.lookback_periods
    if lag <= 0:
        return None
    freq = policy.frequency.lower()
    if freq == "monthly":
        return best_by_period.get(_subtract_months(current_period, lag))
    if freq == "quarterly":
        return best_by_period.get(_subtract_months(current_period, lag * 3))
    idx = bisect_left(sorted_periods, current_period)
    target_idx = idx - lag
    if target_idx < 0:
        return None
    return best_by_period.get(sorted_periods[target_idx])


def _compute_transform(
    *,
    policy: FeaturePolicy,
    current_candidate: RawCandidate,
    current_period: date,
    best_by_period: dict[date, RawCandidate],
    sorted_periods: list[date],
) -> float | None:
    transform_code = policy.transform_code
    if transform_code == "level":
        return current_candidate.value

    lag_candidate = _lookup_lag_candidate(
        policy=policy,
        current_period=current_period,
        best_by_period=best_by_period,
        sorted_periods=sorted_periods,
    )
    if lag_candidate is None:
        return None
    current_value = current_candidate.value
    lag_value = lag_candidate.value
    if transform_code == "diff":
        return current_value - lag_value
    if lag_value == 0:
        return None
    if transform_code == "pct_change":
        return (current_value / lag_value) - 1.0
    if transform_code == "annualized_pct_change":
        if policy.annualization_basis is None or current_value <= 0 or lag_value <= 0:
            return None
        return (current_value / lag_value) ** (policy.annualization_basis / policy.lookback_periods) - 1.0
    raise ValueError(f"Unsupported transform_code={transform_code} for metric_key={policy.metric_key}")


def _state_identity(state: FeatureState) -> tuple:
    return (
        state.raw_value_selected,
        state.transformed_value,
        state.sign_adjusted_value,
        state.registry_key,
        state.source_name,
        state.source_series_id,
        state.observation_period_selected,
        state.observation_date_selected,
        state.release_date_selected,
        state.vintage_date_selected,
        state.effective_available_date_selected,
    )


def _event_state_from_candidate(
    *,
    as_of_date_text: str,
    candidate: RawCandidate,
    transformed_value: float | None,
    policy: FeaturePolicy,
) -> FeatureState:
    sign_adjusted_value = transformed_value * policy.sign_multiplier if transformed_value is not None else None
    return FeatureState(
        as_of_date_text=as_of_date_text,
        raw_value_selected=candidate.value,
        transformed_value=transformed_value,
        sign_adjusted_value=sign_adjusted_value,
        zscore_value=None,
        percentile_value=None,
        standardized_value=None,
        registry_key=candidate.registry_key,
        source_name=candidate.source_name,
        source_series_id=candidate.source_series_id,
        observation_period_selected=candidate.observation_period,
        observation_date_selected=candidate.observation_date_text,
        release_date_selected=candidate.release_date_text,
        vintage_date_selected=candidate.vintage_date_text,
        effective_available_date_selected=candidate.effective_available_date_text,
        staleness_days=None,
        max_staleness_days=None,
        source_quality_weight=None,
        coverage_flag=0,
    )


def _build_level_events(candidates: Iterable[RawCandidate], policy: FeaturePolicy) -> list[FeatureState]:
    current_selected: RawCandidate | None = None
    events: list[FeatureState] = []
    for event_date, group in _group_candidates_by_date(candidates):
        changed = False
        for candidate in group:
            current_period = candidate.observation_date
            selected_period = current_selected.observation_date if current_selected is not None else None
            if current_period is None:
                continue
            if selected_period is None or current_period > selected_period:
                current_selected = candidate
                changed = True
            elif current_period == selected_period and current_selected is not None and candidate_rank(candidate) > candidate_rank(current_selected):
                current_selected = candidate
                changed = True
        if not changed or current_selected is None:
            continue
        state = _event_state_from_candidate(
            as_of_date_text=event_date.isoformat(),
            candidate=current_selected,
            transformed_value=current_selected.value,
            policy=policy,
        )
        if not events or _state_identity(state) != _state_identity(events[-1]):
            events.append(state)
    return events


def _build_panel_events(candidates: Iterable[RawCandidate], policy: FeaturePolicy) -> list[FeatureState]:
    best_by_period: dict[date, RawCandidate] = {}
    sorted_periods: list[date] = []
    events: list[FeatureState] = []
    for event_date, group in _group_candidates_by_date(candidates):
        changed = False
        for candidate in group:
            period = candidate.observation_date
            if period is None:
                continue
            existing = best_by_period.get(period)
            if existing is None:
                best_by_period[period] = candidate
                insort(sorted_periods, period)
                changed = True
            elif candidate_rank(candidate) > candidate_rank(existing):
                best_by_period[period] = candidate
                changed = True
        if not changed or not sorted_periods:
            continue
        current_period = sorted_periods[-1]
        current_candidate = best_by_period[current_period]
        transformed_value = _compute_transform(
            policy=policy,
            current_candidate=current_candidate,
            current_period=current_period,
            best_by_period=best_by_period,
            sorted_periods=sorted_periods,
        )
        state = _event_state_from_candidate(
            as_of_date_text=event_date.isoformat(),
            candidate=current_candidate,
            transformed_value=transformed_value,
            policy=policy,
        )
        if not events or _state_identity(state) != _state_identity(events[-1]):
            events.append(state)
    return events


def _standardize_events(events: list[FeatureState], policy: FeaturePolicy) -> list[FeatureState]:
    valid_values: list[float] = []
    out: list[FeatureState] = []
    for state in events:
        current = state.sign_adjusted_value
        if current is not None:
            valid_values.append(current)
        zscore_value: float | None = None
        percentile_value: float | None = None
        standardized_value: float | None = None
        if current is not None:
            z_window = valid_values[-policy.zscore_window :]
            if len(z_window) >= policy.min_history_periods:
                mean_value = sum(z_window) / len(z_window)
                variance = sum((item - mean_value) ** 2 for item in z_window) / len(z_window)
                std_value = math.sqrt(max(variance, 0.0))
                if std_value > 0:
                    zscore_value = (current - mean_value) / std_value
                else:
                    zscore_value = 0.0
            p_window = valid_values[-policy.percentile_window :]
            if len(p_window) >= policy.min_history_periods:
                less_count = sum(1 for item in p_window if item < current)
                equal_count = sum(1 for item in p_window if item == current)
                percentile_value = (less_count + 0.5 * equal_count) / len(p_window)
            if zscore_value is not None:
                standardized_value = zscore_value
                if policy.standardized_clip_min is not None:
                    standardized_value = max(policy.standardized_clip_min, standardized_value)
                if policy.standardized_clip_max is not None:
                    standardized_value = min(policy.standardized_clip_max, standardized_value)
        out.append(
            replace(
                state,
                zscore_value=zscore_value,
                percentile_value=percentile_value,
                standardized_value=standardized_value,
            )
        )
    return out


def _load_metric_tasks(
    raw_conn: sqlite3.Connection,
    *,
    policies: dict[str, FeaturePolicy],
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
                "Skipping feature build for metric_key=%s because no feature policy row was found.",
                spec.metric_key,
            )
            skipped_metric_keys.append(spec.metric_key)
            continue
        tasks.append(MetricTask(metric_key=spec.metric_key, observation_count=spec.observation_count, policy=policy))
    tasks.sort(key=lambda item: (-item.observation_count, item.metric_key))
    return tasks, skipped_metric_keys


def _resolve_worker_count(cfg: dict, override: int, task_count: int) -> int:
    if override and override > 0:
        return max(1, min(override, task_count))
    configured = int(cfg_get(cfg, "serving", "feature_workers", default=2))
    return max(1, min(configured, task_count))


def _event_row_tuple(policy: FeaturePolicy, state: FeatureState, pit_row: PitDailyRow | None) -> tuple:
    return (
        state.as_of_date_text,
        policy.metric_key,
        policy.feature_name,
        policy.ref_area,
        policy.frequency,
        policy.regime_block,
        policy.transform_code,
        state.raw_value_selected,
        state.transformed_value,
        state.sign_adjusted_value,
        state.zscore_value,
        state.percentile_value,
        state.standardized_value,
        state.registry_key or (pit_row.registry_key if pit_row else None),
        state.source_name or (pit_row.source_name if pit_row else None),
        state.source_series_id or (pit_row.source_series_id if pit_row else None),
        state.observation_period_selected or (pit_row.observation_period_selected if pit_row else None),
        state.observation_date_selected or (pit_row.observation_date_selected if pit_row else None),
        state.release_date_selected or (pit_row.release_date_selected if pit_row else None),
        state.vintage_date_selected or (pit_row.vintage_date_selected if pit_row else None),
        state.effective_available_date_selected or (pit_row.effective_available_date_selected if pit_row else None),
        pit_row.staleness_days if pit_row else state.staleness_days,
        pit_row.max_staleness_days if pit_row else state.max_staleness_days,
        pit_row.source_quality_weight if pit_row else state.source_quality_weight,
        pit_row.coverage_flag if pit_row else state.coverage_flag,
        utc_now_iso(),
    )


def _daily_row_tuple(policy: FeaturePolicy, pit_row: PitDailyRow, latest_event: FeatureState | None) -> tuple:
    return (
        pit_row.as_of_date_text,
        policy.metric_key,
        policy.feature_name,
        latest_event.as_of_date_text if latest_event is not None else None,
        policy.ref_area,
        policy.frequency,
        policy.regime_block,
        policy.transform_code,
        pit_row.raw_value_selected,
        latest_event.transformed_value if latest_event is not None else None,
        latest_event.sign_adjusted_value if latest_event is not None else None,
        latest_event.zscore_value if latest_event is not None else None,
        latest_event.percentile_value if latest_event is not None else None,
        latest_event.standardized_value if latest_event is not None else None,
        pit_row.registry_key,
        pit_row.source_name,
        pit_row.source_series_id,
        pit_row.observation_period_selected,
        pit_row.observation_date_selected,
        pit_row.release_date_selected,
        pit_row.vintage_date_selected,
        pit_row.effective_available_date_selected,
        pit_row.staleness_days,
        pit_row.max_staleness_days,
        pit_row.source_quality_weight,
        pit_row.carry_forward_allowed,
        0 if latest_event is None or latest_event.as_of_date_text == pit_row.as_of_date_text else 1,
        pit_row.coverage_flag,
        utc_now_iso(),
    )


def build_metric_feature_rows(
    *,
    raw_db_path: Path,
    serving_db_path: Path,
    metric_task: MetricTask,
    start_date: date,
    end_date: date,
) -> tuple[list[tuple], list[tuple]]:
    raw_conn = connect_sqlite(raw_db_path, row_factory=sqlite3.Row)
    serving_conn = connect_sqlite(serving_db_path, row_factory=sqlite3.Row)
    try:
        pit_rows_all = _load_pit_rows(
            serving_conn,
            metric_task.metric_key,
            start_date=None,
            end_date=end_date.isoformat(),
        )
        pit_by_date = {row.as_of_date_text: row for row in pit_rows_all}
        pit_rows = [row for row in pit_rows_all if start_date <= row.as_of_date <= end_date]
        if metric_task.policy.transform_code == "level":
            events = _build_level_events_from_pit(pit_rows_all, metric_task.policy)
        else:
            candidates = _iter_metric_candidates(raw_conn, metric_task.metric_key, end_date.isoformat())
            events = _build_panel_events(candidates, metric_task.policy)
        all_events = _standardize_events(events, metric_task.policy)
        filtered_events = [event for event in all_events if start_date.isoformat() <= event.as_of_date_text <= end_date.isoformat()]
        event_rows = [
            _event_row_tuple(metric_task.policy, event, pit_by_date.get(event.as_of_date_text))
            for event in filtered_events
        ]
        daily_rows: list[tuple] = []
        event_idx = 0
        latest_event: FeatureState | None = None
        for pit_row in pit_rows:
            while event_idx < len(all_events) and all_events[event_idx].as_of_date_text <= pit_row.as_of_date_text:
                latest_event = all_events[event_idx]
                event_idx += 1
            daily_rows.append(_daily_row_tuple(metric_task.policy, pit_row, latest_event))
        return event_rows, daily_rows
    finally:
        raw_conn.close()
        serving_conn.close()


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, cfg = load_macro_raw_config(args.config)
    raw_db_path = resolve_db_path(cfg, config_path, override=args.raw_db_path)
    serving_db_path = resolve_serving_db_path(cfg, config_path, override=args.serving_db_path)
    feature_policy_path = args.feature_policy_csv or resolve_path(
        config_path,
        str(cfg_get(cfg, "feature_policy_csv", default="MacroLayer/macro_feature_policy.csv")),
    )
    if feature_policy_path is None:
        raise ValueError("macro_raw.feature_policy_csv is required for feature builds.")

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
        policies = load_feature_policy(feature_policy_path)
        metric_filter = {str(item).strip() for item in (args.metric_keys or []) if str(item).strip()}
        tasks, skipped_metric_keys = _load_metric_tasks(raw_conn, policies=policies, metric_filter=metric_filter or None)
        if not tasks:
            raise ValueError("No feature build tasks matched the requested metric universe after policy filtering.")
        worker_count = _resolve_worker_count(cfg, args.workers, len(tasks))
        latest_ingest_run = select_latest_completed_ingest_run(raw_conn)
        start_serving_run(
            serving_conn,
            serving_run_id=serving_run_id,
            build_step="feature_layer",
            raw_ingest_run_id=latest_ingest_run.run_id if latest_ingest_run is not None else raw_ingest_run_id,
            as_of_start_date=start_date.isoformat(),
            as_of_end_date=end_date.isoformat(),
            metric_count=len(tasks),
            notes=(
                f"Building macro feature event and daily tables with workers={worker_count}. "
                f"Skipped metrics without policy={len(skipped_metric_keys)}."
            ),
        )
        run_started = True
        clear_feature_range(
            serving_conn,
            table_name="macro_feature_event",
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            metric_keys=[task.metric_key for task in tasks] if metric_filter else None,
        )
        clear_feature_range(
            serving_conn,
            table_name="macro_feature_daily",
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            metric_keys=[task.metric_key for task in tasks] if metric_filter else None,
        )
        rows_written = 0
        logger.info(
            "Building macro feature layer: metrics=%d as_of_start=%s as_of_end=%s workers=%d",
            len(tasks),
            start_date.isoformat(),
            end_date.isoformat(),
            worker_count,
        )
        if skipped_metric_keys:
            logger.warning(
                "Skipped %d metric(s) with no feature policy: %s",
                len(skipped_metric_keys),
                skipped_metric_keys,
            )
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(
                    build_metric_feature_rows,
                    raw_db_path=raw_db_path,
                    serving_db_path=serving_db_path,
                    metric_task=task,
                    start_date=start_date,
                    end_date=end_date,
                ): task
                for task in tasks
            }
            failed_metric_keys: list[str] = []
            for future in as_completed(futures):
                task = futures[future]
                try:
                    event_rows, daily_rows = future.result()
                except Exception as exc:
                    logger.exception("Feature build failed for metric_key=%s: %s", task.metric_key, exc)
                    failed_metric_keys.append(task.metric_key)
                    continue
                rows_written += insert_many(
                    serving_conn,
                    """
                    INSERT INTO macro_feature_event (
                        as_of_date, metric_key, feature_name, ref_area, frequency, regime_block,
                        transform_code, raw_value_selected, transformed_value, sign_adjusted_value,
                        zscore_value, percentile_value, standardized_value, registry_key, source_name,
                        source_series_id, observation_period_selected, observation_date_selected,
                        release_date_selected, vintage_date_selected, effective_available_date_selected,
                        staleness_days, max_staleness_days, source_quality_weight, coverage_flag, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    event_rows,
                )
                rows_written += insert_many(
                    serving_conn,
                    """
                    INSERT INTO macro_feature_daily (
                        as_of_date, metric_key, feature_name, feature_event_as_of_date, ref_area, frequency,
                        regime_block, transform_code, raw_value_selected, transformed_value, sign_adjusted_value,
                        zscore_value, percentile_value, standardized_value, registry_key, source_name,
                        source_series_id, observation_period_selected, observation_date_selected,
                        release_date_selected, vintage_date_selected, effective_available_date_selected,
                        staleness_days, max_staleness_days, source_quality_weight, carry_forward_allowed,
                        carry_forward_flag, coverage_flag, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    daily_rows,
                )
                logger.info(
                    "Built feature rows for metric_key=%s event_rows=%d daily_rows=%d raw_observation_count=%d",
                    task.metric_key,
                    len(event_rows),
                    len(daily_rows),
                    task.observation_count,
                )
        if failed_metric_keys:
            raise RuntimeError(
                f"Feature build failed for {len(failed_metric_keys)} metric(s): {sorted(failed_metric_keys)}"
            )
        finish_serving_run(
            serving_conn,
            serving_run_id=serving_run_id,
            status="completed",
            rows_written=rows_written,
            notes=(
                f"Feature layer built for {len(tasks)} metrics. "
                f"Skipped metrics without policy={len(skipped_metric_keys)}."
            ),
        )
        logger.info(
            "Macro feature build complete: serving_run_id=%s rows_written=%d metrics=%d",
            serving_run_id,
            rows_written,
            len(tasks),
        )
    except BaseException as exc:
        if run_started:
            fail_notes = f"Feature layer failed: {type(exc).__name__}: {exc}"
            try:
                finish_serving_run(
                    serving_conn,
                    serving_run_id=serving_run_id,
                    status="failed",
                    rows_written=rows_written,
                    notes=fail_notes,
                )
            except Exception:
                logger.exception("Failed to record failed feature layer run for serving_run_id=%s", serving_run_id)
        raise
    finally:
        raw_conn.close()
        serving_conn.close()


if __name__ == "__main__":
    main()

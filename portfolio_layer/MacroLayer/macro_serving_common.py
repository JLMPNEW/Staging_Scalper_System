#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from pandas.tseries.holiday import USFederalHolidayCalendar

from macro_policy import MetricPolicy
from macro_raw_config import cfg_get, parse_iso_date, resolve_db_path, resolve_path


RELEASE_STALENESS_POLICY_VERSION = "release_business_days_v1"
DEFAULT_PUBLICATION_LAG_DAYS = {
    "daily": 1,
    "weekly": 7,
    "monthly": 30,
    "quarterly": 60,
    "annual": 90,
}


@dataclass(frozen=True)
class MetricServingSpec:
    metric_key: str
    ref_area: str
    frequency: str
    observation_count: int


@dataclass(frozen=True)
class IngestRunRef:
    run_id: str
    as_of_date: str
    status: str


@dataclass(frozen=True)
class RawCandidate:
    registry_key: str
    metric_key: str
    ref_area: str
    source_name: str
    source_series_id: str | None
    frequency: str
    observation_period: str
    observation_date: date | None
    observation_date_text: str | None
    release_date: date | None
    release_date_text: str | None
    vintage_date: date | None
    vintage_date_text: str | None
    effective_available_date: date
    effective_available_date_text: str
    value: float
    retrieved_at: str
    source_priority: int


def resolve_serving_db_path(cfg: dict, config_path: Path, override: Path | None = None) -> Path:
    if override is not None:
        return Path(override).expanduser().resolve()
    raw_value = cfg_get(cfg, "serving_db_path", default="MacroLayer/macro_serving.sqlite")
    serving_db_path = resolve_path(config_path, str(raw_value))
    if serving_db_path is None:
        raise ValueError("macro_raw.serving_db_path is required in config.")
    return serving_db_path


def select_latest_completed_ingest_run(conn: sqlite3.Connection) -> IngestRunRef | None:
    row = conn.execute(
        """
        SELECT run_id, as_of_date, status
        FROM macro_ingest_run
        WHERE status IN ('completed', 'completed_with_errors')
        ORDER BY COALESCE(completed_at_utc, started_at_utc) DESC, rowid DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return IngestRunRef(run_id=str(row["run_id"]), as_of_date=str(row["as_of_date"]), status=str(row["status"]))


def resolve_calendar_bounds(
    raw_conn: sqlite3.Connection,
    *,
    cfg: dict,
    config_path: Path,
    start_override: str | None = None,
    end_override: str | None = None,
) -> tuple[date, date, str | None]:
    start_date = parse_iso_date(start_override)
    end_date = parse_iso_date(end_override)
    ingest_run = select_latest_completed_ingest_run(raw_conn)

    if start_date is None:
        row = raw_conn.execute(
            """
            SELECT MIN(COALESCE(observation_date, observation_period)) AS min_observation_date
            FROM macro_observation_raw
            """
        ).fetchone()
        start_date = parse_iso_date(row["min_observation_date"] if row else None)
    if end_date is None and ingest_run is not None:
        end_date = parse_iso_date(ingest_run.as_of_date)
    if end_date is None:
        row = raw_conn.execute(
            """
            SELECT MAX(COALESCE(release_date, vintage_date, observation_date, observation_period)) AS max_available_date
            FROM macro_observation_raw
            """
        ).fetchone()
        end_date = parse_iso_date(row["max_available_date"] if row else None)
    if start_date is None or end_date is None:
        raw_db_path = resolve_db_path(cfg, config_path)
        raise ValueError(f"Unable to resolve serving calendar bounds from raw DB: {raw_db_path}")
    if end_date < start_date:
        raise ValueError(f"Serving calendar end date {end_date.isoformat()} is before start date {start_date.isoformat()}.")
    return start_date, end_date, ingest_run.run_id if ingest_run is not None else None


def load_metric_serving_specs(raw_conn: sqlite3.Connection) -> list[MetricServingSpec]:
    rows = raw_conn.execute(
        """
        WITH registry_counts AS (
            SELECT
                r.registry_key,
                r.metric_key,
                r.ref_area,
                r.frequency,
                r.source_priority,
                COUNT(o.observation_id) AS observation_count
            FROM macro_metric_registry r
            LEFT JOIN macro_observation_raw o
              ON o.registry_key = r.registry_key
            WHERE r.enabled = 1
            GROUP BY
                r.registry_key,
                r.metric_key,
                r.ref_area,
                r.frequency,
                r.source_priority
        ), ranked AS (
            SELECT
                *,
                ROW_NUMBER() OVER (
                    PARTITION BY metric_key
                    ORDER BY observation_count DESC, source_priority ASC, registry_key ASC
                ) AS registry_rank
            FROM registry_counts
        )
        SELECT metric_key, ref_area, frequency, observation_count
        FROM ranked
        WHERE registry_rank = 1
        ORDER BY observation_count DESC, metric_key
        """
    ).fetchall()
    return [
        MetricServingSpec(
            metric_key=str(row["metric_key"]),
            ref_area=str(row["ref_area"] or ""),
            frequency=str(row["frequency"] or ""),
            observation_count=int(row["observation_count"] or 0),
        )
        for row in rows
    ]

def load_country_rows(raw_conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return raw_conn.execute(
        """
        SELECT ticker, ref_area, oecd_ref_area, country_class
        FROM macro_country_metadata
        WHERE enabled = 1
          AND country_pack_enabled = 1
          AND country_pack_scope = 'single_country'
        ORDER BY ticker
        """
    ).fetchall()


def daterange(start_date: date, end_date: date) -> Iterable[date]:
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def daterange_strings(start_date: date, end_date: date) -> list[str]:
    return [item.isoformat() for item in daterange(start_date, end_date)]


def period_end_date(period_start: date, frequency: str) -> date:
    freq = str(frequency or "").strip().lower()
    if freq == "monthly":
        next_month = (period_start.replace(day=28) + timedelta(days=4)).replace(day=1)
        return next_month - timedelta(days=1)
    if freq == "quarterly":
        month = ((period_start.month - 1) // 3) * 3 + 1
        quarter_start = period_start.replace(month=month, day=1)
        if quarter_start.month >= 10:
            next_quarter = quarter_start.replace(year=quarter_start.year + 1, month=1, day=1)
        else:
            next_quarter = quarter_start.replace(month=quarter_start.month + 3, day=1)
        return next_quarter - timedelta(days=1)
    return period_start


def parse_calendar_date(value: str | None) -> date | None:
    parsed = parse_iso_date(value)
    if parsed is not None:
        return parsed
    text = str(value or "").strip()
    if len(text) == 7 and text[:4].isdigit() and text[4] == "-" and text[5] in "Qq" and text[6].isdigit():
        quarter = int(text[6])
        if 1 <= quarter <= 4:
            return date(int(text[:4]), (quarter - 1) * 3 + 1, 1)
    if len(text) == 6 and text[:4].isdigit() and text[4] in "Qq" and text[5].isdigit():
        quarter = int(text[5])
        if 1 <= quarter <= 4:
            return date(int(text[:4]), (quarter - 1) * 3 + 1, 1)
    return None


def _retrieval_date(value: str | None) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return parse_calendar_date(text[:10])


def effective_available_date(
    *,
    observation_date: date | None,
    release_date: date | None,
    vintage_date: date | None,
    retrieved_at: str | None = None,
    frequency: str = "",
    source_name: str = "",
    publication_lag_days: int | None = None,
) -> date | None:
    """Return the first date on which a raw value can safely be used.

    Explicit release/vintage metadata wins. For providers that expose neither,
    availability is conservatively bounded by both a cadence-based publication
    lag and the date on which this system actually retrieved the snapshot.

    Daily-frequency rows are exempt from the retrieval bound: they are market
    prints (yields, breakevens, spot prices, FX, vol) that are public and
    effectively unrevised on their observation date, so a later backfill
    retrieval still reproduces exactly what was knowable then. Clamping them
    to the retrieval date would collapse decades of daily history onto a
    handful of ingest dates and starve every standardization window.
    """
    if release_date is not None or vintage_date is not None:
        values = [item for item in (observation_date, release_date, vintage_date) if item is not None]
        return max(values) if values else None
    if observation_date is None:
        return _retrieval_date(retrieved_at)
    normalized_frequency = str(frequency or "").strip().lower()
    lag_days = publication_lag_days
    if lag_days is None:
        lag_days = DEFAULT_PUBLICATION_LAG_DAYS.get(normalized_frequency, 1)
    inferred = period_end_date(observation_date, frequency) + timedelta(days=max(int(lag_days), 0))
    if normalized_frequency == "daily":
        return inferred
    retrieved = _retrieval_date(retrieved_at)
    return max(inferred, retrieved) if retrieved is not None else inferred


def candidate_rank(candidate: RawCandidate) -> tuple[date, date, date, str, int, str]:
    # A newer observation period always outranks revisions to an older period.
    return (
        candidate.observation_date or date.min,
        candidate.vintage_date or date.min,
        candidate.release_date or date.min,
        candidate.retrieved_at,
        -candidate.source_priority,
        candidate.registry_key,
    )

def freshness_anchor_date(candidate: RawCandidate, policy: MetricPolicy) -> date:
    if candidate.release_date is not None:
        return candidate.release_date
    if candidate.observation_date is not None:
        return period_end_date(candidate.observation_date, policy.frequency or candidate.frequency)
    return candidate.effective_available_date


@lru_cache(maxsize=32)
def _us_release_holidays(start_year: int, end_year: int) -> frozenset[date]:
    calendar = USFederalHolidayCalendar()
    holidays = calendar.holidays(
        start=f"{start_year}-01-01",
        end=f"{end_year}-12-31",
    )
    return frozenset(timestamp.date() for timestamp in holidays)


def release_staleness_days(*, as_of: date, anchor: date, frequency: str) -> int:
    """Age a released observation using the cadence implied by its policy.

    Daily U.S.-served macro series age only on federal business days so weekends and
    observed holidays cannot invalidate an otherwise current release. Lower-frequency
    policy thresholds remain calendar-day based, matching the existing 10/45/120-day
    weekly/monthly/quarterly buffers.
    """
    calendar_age = (as_of - anchor).days
    if calendar_age <= 0 or str(frequency or "").strip().lower() != "daily":
        return calendar_age

    holidays = _us_release_holidays(anchor.year, as_of.year)
    current = anchor + timedelta(days=1)
    business_age = 0
    while current <= as_of:
        if current.weekday() < 5 and current not in holidays:
            business_age += 1
        current += timedelta(days=1)
    return business_age

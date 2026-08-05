#!/usr/bin/env python3
"""Surprise-factor research builder (shadow-only, PIT-gated).

Implements the builder scaffold from SURPRISE_FACTOR_CANDIDATE_SPEC.md:

* First print per (metric_key, observation_period) from ``macro_observation_raw`` for the
  true-vintage registry block, available at its first vintage date.
* Expanding-window AR(1) expectation on the first-print series (random-walk fallback), fit
  only on values available strictly before the current period's availability date.
* ``surprise_z`` = (first_print - expectation) / expanding std of strictly-earlier surprises.
* Decay-weighted (half-life 60 calendar days) daily surprise index per regime block.
* Expanding mean absolute revision factor per metric as a reliability-weight candidate.

Outputs go to a NEW research SQLite DB (``macro_surprise_research.sqlite``) plus a dated
manifest + CSV export under ``MacroLayer/out/surprise_research/<max availability date>/``.
Diagnostic only: walk-forward Pearson correlation of each block's surprise index against the
corresponding composite's forward 21-day change is logged, with no gate attached.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import math
import os
import sqlite3
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote

from macro_raw_config import (
    cfg_get,
    configure_pipeline_logging,
    load_macro_raw_config,
    parse_iso_date,
    resolve_db_path,
    resolve_path,
    utc_now_iso,
)

logger = logging.getLogger(__name__)

MIN_OBSERVATIONS_FOR_EXPECTATION = 24
MIN_PRIOR_SURPRISES_FOR_Z = 12
HALF_LIFE_CALENDAR_DAYS = 60.0
SEASONAL_NAIVE_LAG_DAYS = 364
FORWARD_CHANGE_CALENDAR_DAYS = 21
BLOCK_COMPOSITE_KEYS = {
    "growth_now": "G_NOW",
    "growth_lead": "G_LEAD",
    "inflation_now": "PI_NOW",
}
DEFAULT_OUTPUT_DB_RELPATH = "MacroLayer/macro_surprise_research.sqlite"
DEFAULT_OUT_DIR_RELPATH = "MacroLayer/out/surprise_research"
_VARIANCE_EPS = 1e-12

RESEARCH_DB_SCHEMA = """
CREATE TABLE surprise_events (
    metric_key TEXT NOT NULL,
    regime_block TEXT NOT NULL,
    frequency TEXT NOT NULL,
    observation_period TEXT NOT NULL,
    availability_date TEXT NOT NULL,
    first_print REAL NOT NULL,
    expectation REAL,
    expectation_model TEXT,
    seasonal_naive_expectation REAL,
    surprise REAL,
    surprise_z REAL,
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY (metric_key, observation_period)
);
CREATE INDEX idx_surprise_events_availability ON surprise_events(availability_date, metric_key);
CREATE TABLE surprise_index_daily (
    as_of_date TEXT NOT NULL,
    regime_block TEXT NOT NULL,
    surprise_index REAL NOT NULL,
    contributing_metric_count INTEGER NOT NULL,
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY (as_of_date, regime_block)
);
CREATE TABLE revision_factor (
    metric_key TEXT NOT NULL,
    observation_period TEXT NOT NULL,
    first_print REAL NOT NULL,
    latest_value REAL NOT NULL,
    abs_revision REAL NOT NULL,
    mean_abs_revision_z REAL,
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY (metric_key, observation_period)
);
"""


@dataclass(frozen=True)
class MetricSpec:
    metric_key: str
    regime_block: str
    frequency: str


@dataclass(frozen=True)
class PrintRow:
    observation_period: str
    vintage_date_text: str
    vintage_date: date
    value: float
    registry_key: str
    vintage_policy: str


@dataclass(frozen=True)
class SurpriseRow:
    metric_key: str
    observation_period: str
    availability_date: date
    first_print: float
    expectation: float | None
    expectation_model: str | None
    seasonal_naive_expectation: float | None
    surprise: float | None
    surprise_z: float | None


@dataclass(frozen=True)
class RevisionRow:
    metric_key: str
    observation_period: str
    first_print: float
    latest_value: float
    abs_revision: float
    mean_abs_revision_z: float | None


class _RunningMoments:
    """Welford accumulator for an expanding sample standard deviation (ddof=1)."""

    __slots__ = ("count", "mean", "m2")

    def __init__(self) -> None:
        self.count = 0
        self.mean = 0.0
        self.m2 = 0.0

    def add(self, value: float) -> None:
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        self.m2 += delta * (value - self.mean)

    def sample_std(self) -> float | None:
        if self.count < 2:
            return None
        variance = self.m2 / (self.count - 1)
        if not math.isfinite(variance) or variance < 0.0:
            return None
        return math.sqrt(variance)


class _RunningOls:
    """Streaming least squares for y = intercept + slope * x over released AR(1) pairs."""

    __slots__ = ("count", "sx", "sy", "sxx", "sxy")

    def __init__(self) -> None:
        self.count = 0
        self.sx = 0.0
        self.sy = 0.0
        self.sxx = 0.0
        self.sxy = 0.0

    def add(self, x: float, y: float) -> None:
        self.count += 1
        self.sx += x
        self.sy += y
        self.sxx += x * x
        self.sxy += x * y

    def fit(self) -> tuple[float, float] | None:
        if self.count < 2:
            return None
        var_x = self.sxx - (self.sx * self.sx) / self.count
        if not math.isfinite(var_x) or var_x <= _VARIANCE_EPS * max(1.0, abs(self.sxx)):
            return None
        slope = (self.sxy - (self.sx * self.sy) / self.count) / var_x
        intercept = (self.sy - slope * self.sx) / self.count
        if not (math.isfinite(slope) and math.isfinite(intercept)):
            return None
        return intercept, slope


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    resolved = db_path.resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"SQLite database does not exist: {resolved}")
    uri = "file:///" + quote(resolved.as_posix().lstrip("/"), safe="/:") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def load_true_vintage_metrics(raw_conn: sqlite3.Connection) -> list[MetricSpec]:
    rows = raw_conn.execute(
        """
        SELECT registry_key, metric_key, regime_block, frequency
        FROM macro_metric_registry
        WHERE vintage_policy = 'true_vintage' AND enabled = 1
        ORDER BY metric_key, registry_key
        """
    ).fetchall()
    specs: dict[str, MetricSpec] = {}
    for row in rows:
        spec = MetricSpec(
            metric_key=str(row["metric_key"]),
            regime_block=str(row["regime_block"]),
            frequency=str(row["frequency"]),
        )
        existing = specs.get(spec.metric_key)
        if existing is not None and existing != spec:
            raise ValueError(
                f"Conflicting true-vintage registry rows for metric_key={spec.metric_key!r}: "
                f"{existing} vs {spec}"
            )
        specs[spec.metric_key] = spec
    if not specs:
        raise ValueError("No enabled registry rows with vintage_policy='true_vintage' were found.")
    return [specs[key] for key in sorted(specs)]


def _parse_vintage_strict(vintage_text: str, *, metric_key: str, observation_period: str) -> date:
    parsed = parse_iso_date(vintage_text)
    if parsed is None or parsed.isoformat() != vintage_text:
        raise ValueError(
            "Fail closed: malformed vintage_date "
            f"{vintage_text!r} for metric_key={metric_key!r} observation_period={observation_period!r}."
        )
    return parsed


def _better_candidate(a: sqlite3.Row, b: sqlite3.Row, *, prefer_late_retrieval: bool) -> sqlite3.Row:
    a_priority = int(a["source_priority"] or 0)
    b_priority = int(b["source_priority"] or 0)
    if a_priority != b_priority:
        return a if a_priority > b_priority else b
    a_retrieved = str(a["retrieved_at"] or "")
    b_retrieved = str(b["retrieved_at"] or "")
    if a_retrieved != b_retrieved:
        if prefer_late_retrieval:
            return a if a_retrieved > b_retrieved else b
        return a if a_retrieved < b_retrieved else b
    a_id = int(a["observation_id"])
    b_id = int(b["observation_id"])
    if prefer_late_retrieval:
        return a if a_id > b_id else b
    return a if a_id < b_id else b


def extract_extreme_prints(
    raw_conn: sqlite3.Connection,
    metric_key: str,
    *,
    extreme: str,
) -> dict[str, PrintRow]:
    """Return one row per observation_period at the MIN (first print) or MAX (latest) vintage."""
    if extreme not in ("MIN", "MAX"):
        raise ValueError(f"extreme must be 'MIN' or 'MAX', got {extreme!r}")
    sql = f"""
        SELECT
            fp.observation_period AS observation_period,
            fp.extreme_vintage AS vintage_date,
            o.value AS value,
            o.registry_key AS registry_key,
            COALESCE(r.vintage_policy, '') AS vintage_policy,
            COALESCE(r.source_priority, 0) AS source_priority,
            o.retrieved_at AS retrieved_at,
            o.observation_id AS observation_id
        FROM (
            SELECT observation_period, {extreme}(vintage_date) AS extreme_vintage
            FROM macro_observation_raw
            WHERE metric_key = ? AND COALESCE(vintage_date, '') <> ''
            GROUP BY observation_period
        ) fp
        JOIN macro_observation_raw o
          ON o.metric_key = ?
         AND o.observation_period = fp.observation_period
         AND o.vintage_date = fp.extreme_vintage
        LEFT JOIN macro_metric_registry r ON r.registry_key = o.registry_key
    """
    prefer_late_retrieval = extreme == "MAX"
    winners: dict[str, sqlite3.Row] = {}
    for row in raw_conn.execute(sql, (metric_key, metric_key)):
        period = str(row["observation_period"])
        current = winners.get(period)
        if current is None:
            winners[period] = row
        else:
            winners[period] = _better_candidate(current, row, prefer_late_retrieval=prefer_late_retrieval)
    out: dict[str, PrintRow] = {}
    for period, row in winners.items():
        vintage_text = str(row["vintage_date"])
        out[period] = PrintRow(
            observation_period=period,
            vintage_date_text=vintage_text,
            vintage_date=_parse_vintage_strict(vintage_text, metric_key=metric_key, observation_period=period),
            value=float(row["value"]),
            registry_key=str(row["registry_key"]),
            vintage_policy=str(row["vintage_policy"]),
        )
    return out


def build_metric_surprise_rows(
    *,
    metric_key: str,
    periods: Sequence[str],
    availability_dates: Sequence[date],
    first_prints: Sequence[float],
    frequency: str,
    min_observations: int = MIN_OBSERVATIONS_FOR_EXPECTATION,
    min_prior_surprises: int = MIN_PRIOR_SURPRISES_FOR_Z,
    seasonal_lag_days: int = SEASONAL_NAIVE_LAG_DAYS,
) -> list[SurpriseRow]:
    """Compute PIT-gated first-print expectations and standardized surprises for one metric.

    Inputs must be aligned arrays sorted by ``observation_period`` ascending with unique
    periods. Every expectation (and the surprise std window) is fit only on first prints whose
    availability date is STRICTLY BEFORE the target row's availability date; a violation of
    that invariant raises (fail closed) rather than producing a row.
    """
    n = len(periods)
    if not (n == len(availability_dates) == len(first_prints)):
        raise ValueError("periods, availability_dates and first_prints must be aligned.")
    if list(periods) != sorted(periods):
        raise ValueError(f"Fail closed: periods must be sorted ascending for metric_key={metric_key!r}.")
    if len(set(periods)) != n:
        raise ValueError(f"Fail closed: duplicate observation_period for metric_key={metric_key!r}.")

    index_by_period = {str(periods[i]): i for i in range(n)}
    is_weekly = frequency.strip().lower() == "weekly"

    pairs = sorted(
        (
            (max(availability_dates[k - 1], availability_dates[k]), first_prints[k - 1], first_prints[k])
            for k in range(1, n)
        ),
        key=lambda item: item[0],
    )
    order = sorted(range(n), key=lambda i: (availability_dates[i], periods[i]))

    released_periods: list[str] = []
    released_values: list[float] = []
    released_max_avail: date | None = None
    ols = _RunningOls()
    surprise_moments = _RunningMoments()
    pending_surprises: list[tuple[date, float]] = []
    pair_idx = 0
    release_idx = 0
    surprise_idx = 0
    results: list[SurpriseRow | None] = [None] * n

    i = 0
    while i < n:
        group_avail = availability_dates[order[i]]
        j = i
        while j < n and availability_dates[order[j]] == group_avail:
            j += 1
        while release_idx < i:
            idx = order[release_idx]
            pos = bisect_left(released_periods, str(periods[idx]))
            released_periods.insert(pos, str(periods[idx]))
            released_values.insert(pos, float(first_prints[idx]))
            if released_max_avail is None or availability_dates[idx] > released_max_avail:
                released_max_avail = availability_dates[idx]
            release_idx += 1
        while pair_idx < len(pairs) and pairs[pair_idx][0] < group_avail:
            ols.add(float(pairs[pair_idx][1]), float(pairs[pair_idx][2]))
            pair_idx += 1
        while surprise_idx < len(pending_surprises) and pending_surprises[surprise_idx][0] < group_avail:
            surprise_moments.add(pending_surprises[surprise_idx][1])
            surprise_idx += 1
        if released_max_avail is not None and released_max_avail >= group_avail:
            raise RuntimeError(
                "PIT violation (fail closed): expectation window for "
                f"metric_key={metric_key!r} availability={group_avail.isoformat()} would include data "
                f"available at {released_max_avail.isoformat()}."
            )
        prior_std = surprise_moments.sample_std() if surprise_moments.count >= min_prior_surprises else None

        for k in range(i, j):
            idx = order[k]
            period = str(periods[idx])
            value = float(first_prints[idx])
            pos = bisect_left(released_periods, period)
            x_prev = released_values[pos - 1] if pos > 0 else None

            expectation: float | None = None
            model: str | None = None
            if len(released_periods) >= min_observations and x_prev is not None:
                fit = ols.fit()
                if fit is not None:
                    intercept, slope = fit
                    expectation = intercept + slope * x_prev
                    model = "ar1"
                else:
                    expectation = x_prev
                    model = "random_walk"

            seasonal: float | None = None
            if is_weekly:
                period_date = parse_iso_date(period)
                if period_date is not None:
                    lag_period = (period_date - timedelta(days=seasonal_lag_days)).isoformat()
                    lag_idx = index_by_period.get(lag_period)
                    if lag_idx is not None and availability_dates[lag_idx] < group_avail:
                        seasonal = float(first_prints[lag_idx])

            surprise = value - expectation if expectation is not None else None
            surprise_z: float | None = None
            if surprise is not None and prior_std is not None and prior_std > _VARIANCE_EPS:
                surprise_z = surprise / prior_std
            if surprise is not None:
                pending_surprises.append((group_avail, surprise))

            results[idx] = SurpriseRow(
                metric_key=metric_key,
                observation_period=period,
                availability_date=group_avail,
                first_print=value,
                expectation=expectation,
                expectation_model=model,
                seasonal_naive_expectation=seasonal,
                surprise=surprise,
                surprise_z=surprise_z,
            )
        i = j

    out = [row for row in results if row is not None]
    if len(out) != n:
        raise RuntimeError(f"Internal error: surprise rows incomplete for metric_key={metric_key!r}.")
    return out


def build_revision_factor_rows(
    *,
    metric_key: str,
    periods: Sequence[str],
    first_prints: Sequence[float],
    latest_values: Sequence[float],
) -> list[RevisionRow]:
    """Expanding mean absolute (first_print - latest) / expanding first-print std proxy."""
    n = len(periods)
    if not (n == len(first_prints) == len(latest_values)):
        raise ValueError("periods, first_prints and latest_values must be aligned.")
    moments = _RunningMoments()
    z_sum = 0.0
    z_count = 0
    rows: list[RevisionRow] = []
    for idx in range(n):
        first_print = float(first_prints[idx])
        latest_value = float(latest_values[idx])
        moments.add(first_print)
        abs_revision = abs(first_print - latest_value)
        scale = moments.sample_std()
        if scale is not None and scale > _VARIANCE_EPS:
            z_sum += abs_revision / scale
            z_count += 1
        rows.append(
            RevisionRow(
                metric_key=metric_key,
                observation_period=str(periods[idx]),
                first_print=first_print,
                latest_value=latest_value,
                abs_revision=abs_revision,
                mean_abs_revision_z=(z_sum / z_count) if z_count > 0 else None,
            )
        )
    return rows


def build_surprise_index_daily(
    impulses: Sequence[tuple[str, str, date, float]],
    calendar_dates: Sequence[date],
    *,
    half_life_days: float = HALF_LIFE_CALENDAR_DAYS,
    end_date: date | None = None,
) -> list[tuple[str, str, float, int]]:
    """Decay-weighted daily surprise index per regime block.

    ``impulses`` rows are (regime_block, metric_key, availability_date, surprise_z), at most one
    per (metric_key, availability_date). Each metric contributes its LATEST impulse with
    availability <= as_of, decayed by ``0.5 ** (calendar_days_since / half_life_days)``.
    Returns (as_of_date_iso, regime_block, surprise_index, contributing_metric_count) rows.
    """
    if half_life_days <= 0.0:
        raise ValueError("half_life_days must be positive.")
    if not impulses:
        return []
    calendar = sorted(set(calendar_dates))
    if not calendar:
        raise ValueError("calendar_dates must not be empty when impulses exist.")
    resolved_end = end_date if end_date is not None else max(item[2] for item in impulses)
    by_block: dict[str, list[tuple[date, str, float]]] = {}
    for regime_block, metric_key, availability, surprise_z in impulses:
        by_block.setdefault(str(regime_block), []).append((availability, str(metric_key), float(surprise_z)))
    out: list[tuple[str, str, float, int]] = []
    for regime_block in sorted(by_block):
        events = sorted(by_block[regime_block], key=lambda item: (item[0], item[1]))
        start = events[0][0]
        lo = bisect_left(calendar, start)
        hi = bisect_right(calendar, resolved_end)
        pointer = 0
        active: dict[str, tuple[date, float]] = {}
        for day in calendar[lo:hi]:
            while pointer < len(events) and events[pointer][0] <= day:
                availability, metric_key, surprise_z = events[pointer]
                active[metric_key] = (availability, surprise_z)
                pointer += 1
            total = 0.0
            for availability, surprise_z in active.values():
                age_days = (day - availability).days
                total += surprise_z * (0.5 ** (age_days / half_life_days))
            out.append((day.isoformat(), regime_block, total, len(active)))
    out.sort(key=lambda item: (item[0], item[1]))
    return out


def load_calendar_dates(serving_conn: sqlite3.Connection | None, start: date, end: date) -> list[date]:
    if serving_conn is not None:
        try:
            rows = serving_conn.execute(
                """
                SELECT as_of_date FROM macro_calendar_daily
                WHERE as_of_date BETWEEN ? AND ?
                ORDER BY as_of_date
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            logger.warning("macro_calendar_daily unavailable (%s); falling back to business-day calendar.", exc)
            rows = []
        dates = [parse_iso_date(str(row["as_of_date"])) for row in rows]
        out = [item for item in dates if item is not None]
        if out and out[0] <= start and out[-1] >= end:
            return out
        if out:
            logger.warning(
                "macro_calendar_daily covers %s..%s but %s..%s was requested; falling back to business days.",
                out[0].isoformat(),
                out[-1].isoformat(),
                start.isoformat(),
                end.isoformat(),
            )
    return business_day_calendar(start, end)


def business_day_calendar(start: date, end: date) -> list[date]:
    import pandas as pd

    return [ts.date() for ts in pd.date_range(start=start, end=end, freq="B")]


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = 0.0
    syy = 0.0
    sxy = 0.0
    for x, y in zip(xs, ys):
        dx = x - mean_x
        dy = y - mean_y
        sxx += dx * dx
        syy += dy * dy
        sxy += dx * dy
    if sxx <= 0.0 or syy <= 0.0:
        return None
    return sxy / math.sqrt(sxx * syy)


def compute_correlation_diagnostics(
    serving_conn: sqlite3.Connection | None,
    index_rows: Sequence[tuple[str, str, float, int]],
    *,
    forward_days: int = FORWARD_CHANGE_CALENDAR_DAYS,
) -> list[dict[str, Any]]:
    """Diagnostic only (no gate): per-block Pearson r of the surprise index vs the
    corresponding composite's forward ``forward_days``-day change."""
    diagnostics: list[dict[str, Any]] = []
    if serving_conn is None:
        return diagnostics
    for regime_block in sorted({row[1] for row in index_rows}):
        composite_key = BLOCK_COMPOSITE_KEYS.get(regime_block)
        if composite_key is None:
            logger.warning("No composite mapping for regime_block=%s; skipping diagnostic.", regime_block)
            continue
        try:
            comp_rows = serving_conn.execute(
                """
                SELECT as_of_date, composite_value_smoothed
                FROM macro_composite_daily
                WHERE composite_key = ? AND composite_value_smoothed IS NOT NULL
                """,
                (composite_key,),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            logger.warning("macro_composite_daily unavailable (%s); skipping correlation diagnostics.", exc)
            return diagnostics
        composite: dict[date, float] = {}
        for row in comp_rows:
            as_of = parse_iso_date(str(row["as_of_date"]))
            if as_of is not None:
                composite[as_of] = float(row["composite_value_smoothed"])
        xs: list[float] = []
        ys: list[float] = []
        first_used: str | None = None
        last_used: str | None = None
        for as_of_text, block, index_value, _count in index_rows:
            if block != regime_block:
                continue
            as_of = parse_iso_date(as_of_text)
            if as_of is None:
                continue
            level_now = composite.get(as_of)
            level_fwd = composite.get(as_of + timedelta(days=forward_days))
            if level_now is None or level_fwd is None:
                continue
            xs.append(index_value)
            ys.append(level_fwd - level_now)
            if first_used is None:
                first_used = as_of_text
            last_used = as_of_text
        pearson_r = _pearson(xs, ys)
        record: dict[str, Any] = {
            "regime_block": regime_block,
            "composite_key": composite_key,
            "forward_days": forward_days,
            "n_obs": len(xs),
            "pearson_r": pearson_r,
            "window_start": first_used,
            "window_end": last_used,
        }
        diagnostics.append(record)
        logger.info(
            "Walk-forward diagnostic (no gate): block=%s composite=%s fwd=%dd n_obs=%d pearson_r=%s window=%s..%s",
            regime_block,
            composite_key,
            forward_days,
            len(xs),
            "n/a" if pearson_r is None else f"{pearson_r:.4f}",
            first_used,
            last_used,
        )
    return diagnostics


def write_research_db(
    output_db_path: Path,
    *,
    events: Sequence[SurpriseRow],
    metric_specs: dict[str, MetricSpec],
    index_rows: Sequence[tuple[str, str, float, int]],
    revision_rows: Sequence[RevisionRow],
) -> dict[str, int]:
    output_db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_db_path.with_name(output_db_path.name + f".tmp{os.getpid()}")
    if tmp_path.exists():
        tmp_path.unlink()
    now_iso = utc_now_iso()
    conn = sqlite3.connect(tmp_path)
    try:
        conn.executescript(RESEARCH_DB_SCHEMA)
        conn.executemany(
            """
            INSERT INTO surprise_events (
                metric_key, regime_block, frequency, observation_period, availability_date,
                first_print, expectation, expectation_model, seasonal_naive_expectation,
                surprise, surprise_z, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row.metric_key,
                    metric_specs[row.metric_key].regime_block,
                    metric_specs[row.metric_key].frequency,
                    row.observation_period,
                    row.availability_date.isoformat(),
                    row.first_print,
                    row.expectation,
                    row.expectation_model,
                    row.seasonal_naive_expectation,
                    row.surprise,
                    row.surprise_z,
                    now_iso,
                )
                for row in events
            ],
        )
        conn.executemany(
            """
            INSERT INTO surprise_index_daily (
                as_of_date, regime_block, surprise_index, contributing_metric_count, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [(as_of, block, value, count, now_iso) for as_of, block, value, count in index_rows],
        )
        conn.executemany(
            """
            INSERT INTO revision_factor (
                metric_key, observation_period, first_print, latest_value, abs_revision,
                mean_abs_revision_z, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row.metric_key,
                    row.observation_period,
                    row.first_print,
                    row.latest_value,
                    row.abs_revision,
                    row.mean_abs_revision_z,
                    now_iso,
                )
                for row in revision_rows
            ],
        )
        conn.commit()
        row_counts = {
            "surprise_events": int(conn.execute("SELECT COUNT(*) FROM surprise_events").fetchone()[0]),
            "surprise_index_daily": int(conn.execute("SELECT COUNT(*) FROM surprise_index_daily").fetchone()[0]),
            "revision_factor": int(conn.execute("SELECT COUNT(*) FROM revision_factor").fetchone()[0]),
        }
    except BaseException:
        conn.close()
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    conn.close()
    os.replace(tmp_path, output_db_path)
    return row_counts


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)


def _script_sha256() -> str:
    return hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest()


def write_manifest_artifacts(
    out_root: Path,
    *,
    max_availability_date: date,
    config_path: Path,
    raw_db_path: Path,
    serving_db_path: Path | None,
    output_db_path: Path,
    row_counts: dict[str, int],
    metric_summaries: dict[str, dict[str, int]],
    index_rows: Sequence[tuple[str, str, float, int]],
    diagnostics: Sequence[dict[str, Any]],
) -> Path:
    dated_dir = out_root / max_availability_date.isoformat()
    manifest = {
        "artifact": "macro_surprise_research",
        "spec": "SURPRISE_FACTOR_CANDIDATE_SPEC.md",
        "status": "shadow_only_research",
        "built_at_utc": utc_now_iso(),
        "script": {"name": Path(__file__).name, "sha256": _script_sha256()},
        "config_path": str(config_path),
        "inputs": {
            "raw_db_path": str(raw_db_path),
            "serving_db_path": None if serving_db_path is None else str(serving_db_path),
        },
        "output_db_path": str(output_db_path),
        "max_availability_date": max_availability_date.isoformat(),
        "parameters": {
            "min_observations_for_expectation": MIN_OBSERVATIONS_FOR_EXPECTATION,
            "min_prior_surprises_for_z": MIN_PRIOR_SURPRISES_FOR_Z,
            "half_life_calendar_days": HALF_LIFE_CALENDAR_DAYS,
            "seasonal_naive_lag_days": SEASONAL_NAIVE_LAG_DAYS,
            "forward_change_calendar_days": FORWARD_CHANGE_CALENDAR_DAYS,
            "block_composite_keys": BLOCK_COMPOSITE_KEYS,
        },
        "row_counts": row_counts,
        "metrics": metric_summaries,
        "correlation_diagnostics": list(diagnostics),
    }
    _atomic_write_text(dated_dir / "manifest.json", json.dumps(manifest, indent=2, sort_keys=False) + "\n")
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["as_of_date", "regime_block", "surprise_index", "contributing_metric_count"])
    for as_of, block, value, count in index_rows:
        writer.writerow([as_of, block, f"{value:.10g}", count])
    _atomic_write_text(dated_dir / "surprise_index_daily.csv", buffer.getvalue())
    return dated_dir


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build PIT-gated macro surprise-factor research artifacts (shadow-only).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to macro raw YAML config (default: MacroLayer/config_macro_raw.yaml).",
    )
    parser.add_argument("--raw-db-path", type=Path, default=None, help="Optional raw SQLite path override.")
    parser.add_argument("--serving-db-path", type=Path, default=None, help="Optional serving SQLite path override.")
    parser.add_argument(
        "--output-db-path",
        type=Path,
        default=None,
        help=f"Optional research SQLite output override (default: {DEFAULT_OUTPUT_DB_RELPATH}).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help=f"Optional manifest output root override (default: {DEFAULT_OUT_DIR_RELPATH}).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    configure_pipeline_logging()
    args = parse_args(argv)
    config_path, cfg = load_macro_raw_config(args.config)
    raw_db_path = resolve_db_path(cfg, config_path, override=args.raw_db_path)
    serving_db_path: Path | None
    if args.serving_db_path is not None:
        serving_db_path = Path(args.serving_db_path).expanduser().resolve()
    else:
        serving_db_path = resolve_path(
            config_path, str(cfg_get(cfg, "serving_db_path", default="MacroLayer/macro_serving.sqlite"))
        )
    output_db_path = (
        Path(args.output_db_path).expanduser().resolve()
        if args.output_db_path is not None
        else resolve_path(config_path, DEFAULT_OUTPUT_DB_RELPATH)
    )
    out_root = (
        Path(args.out_dir).expanduser().resolve()
        if args.out_dir is not None
        else resolve_path(config_path, DEFAULT_OUT_DIR_RELPATH)
    )
    if output_db_path is None or out_root is None:
        raise ValueError("Failed to resolve research output paths.")

    raw_conn = connect_readonly(raw_db_path)
    serving_conn: sqlite3.Connection | None = None
    try:
        if serving_db_path is not None and serving_db_path.exists():
            serving_conn = connect_readonly(serving_db_path)
        else:
            logger.warning("Serving DB not found at %s; using business-day calendar and skipping diagnostics.",
                           serving_db_path)

        metric_specs_list = load_true_vintage_metrics(raw_conn)
        metric_specs = {spec.metric_key: spec for spec in metric_specs_list}
        logger.info("Building surprise factors for %d true-vintage metrics.", len(metric_specs_list))

        all_events: list[SurpriseRow] = []
        all_revision_rows: list[RevisionRow] = []
        impulses: list[tuple[str, str, date, float]] = []
        metric_summaries: dict[str, dict[str, int]] = {}
        for spec in metric_specs_list:
            first_prints = extract_extreme_prints(raw_conn, spec.metric_key, extreme="MIN")
            if not first_prints:
                logger.warning("No vintaged observations for metric_key=%s; skipping.", spec.metric_key)
                continue
            foreign = sorted(
                {row.registry_key for row in first_prints.values() if row.vintage_policy != "true_vintage"}
            )
            if foreign:
                raise RuntimeError(
                    "Fail closed: first prints for metric_key="
                    f"{spec.metric_key!r} include non-true-vintage registry rows {foreign}."
                )
            latest_prints = extract_extreme_prints(raw_conn, spec.metric_key, extreme="MAX")
            periods = sorted(first_prints)
            availability_dates = [first_prints[p].vintage_date for p in periods]
            values = [first_prints[p].value for p in periods]
            for period in periods:
                row = first_prints[period]
                if row.vintage_date.isoformat() != row.vintage_date_text:
                    raise RuntimeError(
                        f"PIT violation (fail closed): availability_date != first-print vintage_date for "
                        f"metric_key={spec.metric_key!r} observation_period={period!r}."
                    )
                if period not in latest_prints:
                    raise RuntimeError(
                        f"Fail closed: latest vintage missing for metric_key={spec.metric_key!r} "
                        f"observation_period={period!r}."
                    )
            events = build_metric_surprise_rows(
                metric_key=spec.metric_key,
                periods=periods,
                availability_dates=availability_dates,
                first_prints=values,
                frequency=spec.frequency,
            )
            revision_rows = build_revision_factor_rows(
                metric_key=spec.metric_key,
                periods=periods,
                first_prints=values,
                latest_values=[latest_prints[p].value for p in periods],
            )
            latest_by_availability: dict[date, tuple[str, float]] = {}
            for row in events:
                if row.surprise_z is not None:
                    latest_by_availability[row.availability_date] = (row.observation_period, row.surprise_z)
            impulses.extend(
                (spec.regime_block, spec.metric_key, availability, z)
                for availability, (_period, z) in latest_by_availability.items()
            )
            all_events.extend(events)
            all_revision_rows.extend(revision_rows)
            metric_summaries[spec.metric_key] = {
                "events": len(events),
                "with_expectation": sum(1 for row in events if row.expectation is not None),
                "with_surprise_z": sum(1 for row in events if row.surprise_z is not None),
            }
            logger.info(
                "metric_key=%s block=%s freq=%s events=%d with_expectation=%d with_surprise_z=%d",
                spec.metric_key,
                spec.regime_block,
                spec.frequency,
                metric_summaries[spec.metric_key]["events"],
                metric_summaries[spec.metric_key]["with_expectation"],
                metric_summaries[spec.metric_key]["with_surprise_z"],
            )

        if not all_events:
            raise RuntimeError("No surprise events were built; refusing to write an empty research DB.")
        max_availability = max(row.availability_date for row in all_events)

        index_rows: list[tuple[str, str, float, int]] = []
        if impulses:
            calendar_start = min(item[2] for item in impulses)
            calendar_dates = load_calendar_dates(serving_conn, calendar_start, max_availability)
            index_rows = build_surprise_index_daily(impulses, calendar_dates, end_date=max_availability)
        else:
            logger.warning("No standardized surprises available; surprise_index_daily will be empty.")

        diagnostics = compute_correlation_diagnostics(serving_conn, index_rows)

        row_counts = write_research_db(
            output_db_path,
            events=all_events,
            metric_specs=metric_specs,
            index_rows=index_rows,
            revision_rows=all_revision_rows,
        )
        dated_dir = write_manifest_artifacts(
            out_root,
            max_availability_date=max_availability,
            config_path=config_path,
            raw_db_path=raw_db_path,
            serving_db_path=serving_db_path,
            output_db_path=output_db_path,
            row_counts=row_counts,
            metric_summaries=metric_summaries,
            index_rows=index_rows,
            diagnostics=diagnostics,
        )
        logger.info(
            "Surprise research build complete: db=%s manifest_dir=%s surprise_events=%d "
            "surprise_index_daily=%d revision_factor=%d max_availability=%s",
            output_db_path,
            dated_dir,
            row_counts["surprise_events"],
            row_counts["surprise_index_daily"],
            row_counts["revision_factor"],
            max_availability.isoformat(),
        )
    finally:
        raw_conn.close()
        if serving_conn is not None:
            serving_conn.close()


if __name__ == "__main__":
    main()

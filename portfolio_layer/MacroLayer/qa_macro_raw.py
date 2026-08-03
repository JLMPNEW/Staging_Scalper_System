#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from macro_policy import MetricPolicy, load_metric_policy, required_for_country_class
from macro_raw_config import (
    cfg_get,
    configure_pipeline_logging,
    connect_sqlite,
    load_macro_raw_config,
    parse_iso_date,
    resolve_db_path,
    resolve_path,
    utc_now_iso,
)
from macro_storage import init_db, seed_country_metadata

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestRunRef:
    run_id: str
    as_of_date: str
    status: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run persisted QA checks against the macro raw database.")
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--db-path", type=Path, default=None, help="Optional SQLite DB path override.")
    parser.add_argument("--run-id", type=str, default=None, help="Optional ingest run id to QA. Defaults to the latest completed run.")
    parser.add_argument("--policy-csv", type=Path, default=None, help="Optional macro metric policy CSV override.")
    return parser.parse_args()


def _default_max_staleness_days(frequency: str) -> int:
    mapping = {
        "daily": 3,
        "weekly": 10,
        "monthly": 45,
        "quarterly": 120,
    }
    return mapping.get(str(frequency or "").strip().lower(), 45)


def _period_end_date(period_start: date, frequency: str) -> date:
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


def _select_ingest_run(conn: sqlite3.Connection, run_id: str | None) -> IngestRunRef:
    if run_id:
        row = conn.execute(
            """
            SELECT run_id, as_of_date, status
            FROM macro_ingest_run
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Macro ingest run not found: {run_id}")
        return IngestRunRef(run_id=str(row["run_id"]), as_of_date=str(row["as_of_date"]), status=str(row["status"]))
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
        raise ValueError("No completed macro ingest run found to QA.")
    return IngestRunRef(run_id=str(row["run_id"]), as_of_date=str(row["as_of_date"]), status=str(row["status"]))


def _load_enabled_registry_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT registry_key, metric_key, source_name, ref_area, frequency, vintage_policy, regime_block
        FROM macro_metric_registry
        WHERE enabled = 1
        ORDER BY source_priority, metric_key, registry_key
        """
    ).fetchall()


def _load_country_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT ticker, ref_area, oecd_ref_area, country_class, country_pack_scope, country_pack_enabled, enabled
        FROM macro_country_metadata
        WHERE enabled = 1
          AND country_pack_enabled = 1
          AND country_pack_scope = 'single_country'
        ORDER BY ticker
        """
    ).fetchall()


def _start_qa_run(conn: sqlite3.Connection, qa_run_id: str, ingest_run: IngestRunRef, metric_count: int) -> None:
    conn.execute(
        """
        INSERT INTO macro_qa_run (
            qa_run_id, ingest_run_id, as_of_date, status, metric_count, started_at_utc
        ) VALUES (?, ?, ?, 'running', ?, ?)
        """,
        (qa_run_id, ingest_run.run_id, ingest_run.as_of_date, metric_count, utc_now_iso()),
    )
    conn.commit()


def _finish_qa_run(
    conn: sqlite3.Connection,
    *,
    qa_run_id: str,
    status: str,
    issue_count: int,
    error_count: int,
    warning_count: int,
    notes: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE macro_qa_run
        SET status = ?, issue_count = ?, error_count = ?, warning_count = ?, completed_at_utc = ?, notes = ?
        WHERE qa_run_id = ?
        """,
        (status, issue_count, error_count, warning_count, utc_now_iso(), notes, qa_run_id),
    )
    conn.commit()


def _issue_tuple(
    *,
    qa_run_id: str,
    severity: str,
    issue_type: str,
    registry_key: str | None,
    metric_key: str | None,
    ref_area: str | None,
    source_name: str | None,
    issue_count: int,
    details: dict[str, Any] | None,
) -> tuple[Any, ...]:
    return (
        qa_run_id,
        severity,
        issue_type,
        registry_key,
        metric_key,
        ref_area,
        source_name,
        issue_count,
        json.dumps(details, separators=(",", ":"), sort_keys=True) if details else None,
        utc_now_iso(),
    )


def _compute_span_rows(conn: sqlite3.Connection, qa_run_id: str, ingest_run_id: str) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT
            ? AS qa_run_id,
            ? AS ingest_run_id,
            r.registry_key,
            r.metric_key,
            r.source_name,
            r.ref_area,
            r.frequency,
            r.vintage_policy,
            COUNT(o.observation_id) AS observation_count,
            MIN(COALESCE(o.observation_date, o.observation_period)) AS min_observation_date,
            MAX(COALESCE(o.observation_date, o.observation_period)) AS max_observation_date,
            MIN(o.release_date) AS min_release_date,
            MAX(o.release_date) AS max_release_date,
            MIN(o.vintage_date) AS min_vintage_date,
            MAX(o.vintage_date) AS max_vintage_date,
            COUNT(DISTINCT o.release_date) AS distinct_release_count,
            COUNT(DISTINCT o.vintage_date) AS distinct_vintage_count
        FROM macro_metric_registry r
        LEFT JOIN macro_observation_raw o
          ON o.registry_key = r.registry_key
        WHERE r.enabled = 1
        GROUP BY
            r.registry_key, r.metric_key, r.source_name, r.ref_area, r.frequency, r.vintage_policy
        ORDER BY r.metric_key, r.registry_key
        """,
        (qa_run_id, ingest_run_id),
    ).fetchall()
    payload = [
        (
            row["qa_run_id"],
            row["ingest_run_id"],
            row["registry_key"],
            row["metric_key"],
            row["source_name"],
            row["ref_area"],
            row["frequency"],
            row["vintage_policy"],
            row["observation_count"],
            row["min_observation_date"],
            row["max_observation_date"],
            row["min_release_date"],
            row["max_release_date"],
            row["min_vintage_date"],
            row["max_vintage_date"],
            row["distinct_release_count"],
            row["distinct_vintage_count"],
            utc_now_iso(),
        )
        for row in rows
    ]
    conn.executemany(
        """
        INSERT INTO macro_metric_span_summary (
            qa_run_id, ingest_run_id, registry_key, metric_key, source_name, ref_area, frequency,
            vintage_policy, observation_count, min_observation_date, max_observation_date,
            min_release_date, max_release_date, min_vintage_date, max_vintage_date,
            distinct_release_count, distinct_vintage_count, updated_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        payload,
    )
    conn.commit()
    return rows


def _compute_freshness_rows(
    conn: sqlite3.Connection,
    *,
    qa_run_id: str,
    ingest_run_id: str,
    as_of_date: str,
    spans: list[sqlite3.Row],
    policies: dict[str, MetricPolicy],
) -> list[dict[str, Any]]:
    as_of = parse_iso_date(as_of_date)
    rows: list[dict[str, Any]] = []
    payload: list[tuple[Any, ...]] = []
    for span in spans:
        metric_key = str(span["metric_key"])
        policy = policies.get(metric_key)
        max_staleness_days = policy.max_staleness_days if policy else _default_max_staleness_days(str(span["frequency"]))
        latest_observation_date = str(span["max_observation_date"] or "") or None
        freshness_anchor_date = str(span["max_release_date"] or "") or latest_observation_date
        freshness_days: int | None = None
        if as_of is not None and freshness_anchor_date:
            latest_date = parse_iso_date(freshness_anchor_date)
            if latest_date is not None:
                if not str(span["max_release_date"] or "").strip():
                    latest_date = _period_end_date(latest_date, str(span["frequency"] or ""))
                freshness_days = (as_of - latest_date).days
        is_stale = latest_observation_date is None or freshness_days is None or freshness_days > max_staleness_days
        row = {
            "qa_run_id": qa_run_id,
            "ingest_run_id": ingest_run_id,
            "registry_key": str(span["registry_key"]),
            "metric_key": metric_key,
            "source_name": str(span["source_name"]),
            "ref_area": str(span["ref_area"] or ""),
            "frequency": str(span["frequency"] or ""),
            "as_of_date": as_of_date,
            "latest_observation_date": latest_observation_date,
            "freshness_days": freshness_days,
            "max_staleness_days": max_staleness_days,
            "carry_forward_allowed": 1 if (policy.carry_forward_allowed if policy else True) else 0,
            "is_stale": 1 if is_stale else 0,
            "source_quality_weight": policy.source_quality_weight if policy else 1.0,
        }
        rows.append(row)
        payload.append(
            (
                row["qa_run_id"],
                row["ingest_run_id"],
                row["registry_key"],
                row["metric_key"],
                row["source_name"],
                row["ref_area"],
                row["frequency"],
                row["as_of_date"],
                row["latest_observation_date"],
                row["freshness_days"],
                row["max_staleness_days"],
                row["carry_forward_allowed"],
                row["is_stale"],
                row["source_quality_weight"],
                utc_now_iso(),
            )
        )
    conn.executemany(
        """
        INSERT INTO macro_metric_freshness_summary (
            qa_run_id, ingest_run_id, registry_key, metric_key, source_name, ref_area, frequency,
            as_of_date, latest_observation_date, freshness_days, max_staleness_days,
            carry_forward_allowed, is_stale, source_quality_weight, updated_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        payload,
    )
    conn.commit()
    return rows


def _compute_country_coverage_rows(
    conn: sqlite3.Connection,
    *,
    qa_run_id: str,
    ingest_run_id: str,
    country_rows: list[sqlite3.Row],
    registry_rows: list[sqlite3.Row],
    freshness_rows: list[dict[str, Any]],
    policies: dict[str, MetricPolicy],
) -> list[dict[str, Any]]:
    registry_by_ref_area: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in registry_rows:
        registry_by_ref_area[str(row["ref_area"])].append(row)
    freshness_by_registry = {str(row["registry_key"]): row for row in freshness_rows}
    rows: list[dict[str, Any]] = []
    payload: list[tuple[Any, ...]] = []
    for country in country_rows:
        ref_area = str(country["oecd_ref_area"] or country["ref_area"] or "")
        country_class = str(country["country_class"] or "")
        regs = registry_by_ref_area.get(ref_area, [])
        expected_metric_count = len(regs)
        available_metric_count = 0
        stale_metric_count = 0
        required_metric_count = 0
        available_required_count = 0
        missing_required: list[str] = []
        for reg in regs:
            registry_key = str(reg["registry_key"])
            metric_key = str(reg["metric_key"])
            freshness = freshness_by_registry.get(registry_key)
            is_available = bool(freshness and not freshness["is_stale"] and freshness["latest_observation_date"])
            if is_available:
                available_metric_count += 1
            else:
                stale_metric_count += 1
            policy = policies.get(metric_key)
            is_required = bool(policy and required_for_country_class(policy, country_class))
            if is_required:
                required_metric_count += 1
                if is_available:
                    available_required_count += 1
                else:
                    missing_required.append(metric_key)
        coverage_ratio = round(available_metric_count / expected_metric_count, 6) if expected_metric_count else None
        required_coverage_ratio = (
            round(available_required_count / required_metric_count, 6)
            if required_metric_count
            else None
        )
        row = {
            "qa_run_id": qa_run_id,
            "ingest_run_id": ingest_run_id,
            "ticker": str(country["ticker"]),
            "ref_area": ref_area,
            "country_class": country_class,
            "expected_metric_count": expected_metric_count,
            "available_metric_count": available_metric_count,
            "required_metric_count": required_metric_count,
            "available_required_count": available_required_count,
            "stale_metric_count": stale_metric_count,
            "coverage_ratio": coverage_ratio,
            "required_coverage_ratio": required_coverage_ratio,
            "missing_required_metrics_json": json.dumps(sorted(missing_required), separators=(",", ":")) if missing_required else None,
        }
        rows.append(row)
        payload.append(
            (
                row["qa_run_id"],
                row["ingest_run_id"],
                row["ticker"],
                row["ref_area"],
                row["country_class"],
                row["expected_metric_count"],
                row["available_metric_count"],
                row["required_metric_count"],
                row["available_required_count"],
                row["stale_metric_count"],
                row["coverage_ratio"],
                row["required_coverage_ratio"],
                row["missing_required_metrics_json"],
                utc_now_iso(),
            )
        )
    conn.executemany(
        """
        INSERT INTO macro_country_coverage_summary (
            qa_run_id, ingest_run_id, ticker, ref_area, country_class, expected_metric_count,
            available_metric_count, required_metric_count, available_required_count,
            stale_metric_count, coverage_ratio, required_coverage_ratio,
            missing_required_metrics_json, updated_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        payload,
    )
    conn.commit()
    return rows


def _query_grouped_counts(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def _registry_row_for_issue(
    registry_by_key: dict[str, sqlite3.Row],
    registry_key: str,
    issue_type: str,
) -> sqlite3.Row | None:
    reg = registry_by_key.get(registry_key)
    if reg is None:
        logger.warning("Skipping QA issue %s because registry_key=%s is not in the enabled registry snapshot.", issue_type, registry_key)
    return reg


def _value_rule_conditions(policy: MetricPolicy) -> tuple[list[str], list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []
    if policy.qa_rule == "positive":
        conditions.append("value <= 0")
    elif policy.qa_rule == "finite":
        conditions.append("ABS(value) > 1.0e308")
    elif policy.qa_rule == "bounded":
        pass
    elif policy.qa_rule:
        logger.warning("Unknown QA rule %r encountered in metric policy; falling back to numeric bounds only.", policy.qa_rule)
    if policy.qa_min_value is not None:
        conditions.append("value < ?")
        params.append(policy.qa_min_value)
    if policy.qa_max_value is not None:
        conditions.append("value > ?")
        params.append(policy.qa_max_value)
    return conditions, params


def _top_error_registry_keys(issues: Sequence[tuple[Any, ...]], limit: int = 5) -> list[str]:
    counts: dict[str, int] = {}
    for issue in issues:
        severity = issue[1]
        registry_key = issue[3]
        issue_count = int(issue[7] or 0)
        if severity != "error" or not registry_key:
            continue
        counts[str(registry_key)] = counts.get(str(registry_key), 0) + max(issue_count, 1)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [key for key, _ in ranked[:limit]]


def _build_issue_rows(
    conn: sqlite3.Connection,
    *,
    qa_run_id: str,
    ingest_run: IngestRunRef,
    registry_rows: list[sqlite3.Row],
    spans: list[sqlite3.Row],
    freshness_rows: list[dict[str, Any]],
    country_rows: list[dict[str, Any]],
    policies: dict[str, MetricPolicy],
    full_history_revision_keys: frozenset[str] = frozenset(),
) -> list[tuple[Any, ...]]:
    issues: list[tuple[Any, ...]] = []
    registry_by_key = {str(row["registry_key"]): row for row in registry_rows}

    for reg in registry_rows:
        metric_key = str(reg["metric_key"])
        if metric_key not in policies:
            issues.append(
                _issue_tuple(
                    qa_run_id=qa_run_id,
                    severity="error",
                    issue_type="missing_metric_policy",
                    registry_key=str(reg["registry_key"]),
                    metric_key=metric_key,
                    ref_area=str(reg["ref_area"]),
                    source_name=str(reg["source_name"]),
                    issue_count=1,
                    details={"message": "Enabled registry row is missing a metric policy."},
                )
            )

    for span in spans:
        if int(span["observation_count"] or 0) == 0:
            issues.append(
                _issue_tuple(
                    qa_run_id=qa_run_id,
                    severity="error",
                    issue_type="missing_enabled_series",
                    registry_key=str(span["registry_key"]),
                    metric_key=str(span["metric_key"]),
                    ref_area=str(span["ref_area"] or ""),
                    source_name=str(span["source_name"]),
                    issue_count=1,
                    details={"message": "Enabled registry row has no raw observations."},
                )
            )
        if str(span["vintage_policy"] or "") == "true_vintage" and int(span["distinct_vintage_count"] or 0) < 2:
            issues.append(
                _issue_tuple(
                    qa_run_id=qa_run_id,
                    severity="warning",
                    issue_type="weak_vintage_coverage",
                    registry_key=str(span["registry_key"]),
                    metric_key=str(span["metric_key"]),
                    ref_area=str(span["ref_area"] or ""),
                    source_name=str(span["source_name"]),
                    issue_count=int(span["distinct_vintage_count"] or 0),
                    details={
                        "distinct_vintage_count": int(span["distinct_vintage_count"] or 0),
                        "message": "True-vintage series has fewer than two distinct vintages.",
                    },
                )
            )

    for fresh in freshness_rows:
        if fresh["is_stale"] and fresh["latest_observation_date"]:
            issues.append(
                _issue_tuple(
                    qa_run_id=qa_run_id,
                    severity="warning",
                    issue_type="stale_metric",
                    registry_key=str(fresh["registry_key"]),
                    metric_key=str(fresh["metric_key"]),
                    ref_area=str(fresh["ref_area"]),
                    source_name=str(fresh["source_name"]),
                    issue_count=1,
                    details={
                        "latest_observation_date": fresh["latest_observation_date"],
                        "freshness_days": fresh["freshness_days"],
                        "max_staleness_days": fresh["max_staleness_days"],
                    },
                )
            )

    duplicate_non_vintage = _query_grouped_counts(
        conn,
        """
        SELECT d.registry_key, COUNT(*) AS duplicate_groups, SUM(d.row_count - 1) AS duplicate_rows
        FROM (
            SELECT
                o.registry_key,
                o.observation_period,
                COALESCE(o.observation_date, '') AS observation_date,
                o.retrieved_at,
                COUNT(*) AS row_count
            FROM macro_observation_raw o
            JOIN macro_metric_registry r
              ON r.registry_key = o.registry_key
            WHERE r.enabled = 1
              AND r.vintage_policy != 'true_vintage'
            GROUP BY o.registry_key, o.observation_period, COALESCE(o.observation_date, ''), o.retrieved_at
            HAVING COUNT(*) > 1
        ) d
        GROUP BY d.registry_key
        """,
    )
    for row in duplicate_non_vintage:
        reg = _registry_row_for_issue(
            registry_by_key,
            str(row["registry_key"]),
            "duplicate_natural_key_non_vintage",
        )
        if reg is None:
            continue
        issues.append(
            _issue_tuple(
                qa_run_id=qa_run_id,
                severity="error",
                issue_type="duplicate_natural_key_non_vintage",
                registry_key=str(row["registry_key"]),
                metric_key=str(reg["metric_key"]),
                ref_area=str(reg["ref_area"]),
                source_name=str(reg["source_name"]),
                issue_count=int(row["duplicate_rows"] or 0),
                details={"duplicate_groups": int(row["duplicate_groups"] or 0)},
            )
        )

    duplicate_vintage = _query_grouped_counts(
        conn,
        """
        SELECT d.registry_key, COUNT(*) AS duplicate_groups, SUM(d.row_count - 1) AS duplicate_rows
        FROM (
            SELECT
                o.registry_key,
                o.observation_period,
                COALESCE(o.observation_date, '') AS observation_date,
                COALESCE(o.release_date, '') AS release_date,
                COALESCE(o.vintage_date, '') AS vintage_date,
                COUNT(*) AS row_count
            FROM macro_observation_raw o
            JOIN macro_metric_registry r
              ON r.registry_key = o.registry_key
            WHERE r.enabled = 1
              AND r.vintage_policy = 'true_vintage'
            GROUP BY
                o.registry_key,
                o.observation_period,
                COALESCE(o.observation_date, ''),
                COALESCE(o.release_date, ''),
                COALESCE(o.vintage_date, '')
            HAVING COUNT(*) > 1
        ) d
        GROUP BY d.registry_key
        """,
    )
    for row in duplicate_vintage:
        reg = _registry_row_for_issue(
            registry_by_key,
            str(row["registry_key"]),
            "duplicate_natural_key_vintage",
        )
        if reg is None:
            continue
        issues.append(
            _issue_tuple(
                qa_run_id=qa_run_id,
                severity="error",
                issue_type="duplicate_natural_key_vintage",
                registry_key=str(row["registry_key"]),
                metric_key=str(reg["metric_key"]),
                ref_area=str(reg["ref_area"]),
                source_name=str(reg["source_name"]),
                issue_count=int(row["duplicate_rows"] or 0),
                details={"duplicate_groups": int(row["duplicate_groups"] or 0)},
            )
        )

    exempt_clause = ""
    exempt_params: tuple[Any, ...] = ()
    if full_history_revision_keys:
        placeholders = ",".join("?" for _ in full_history_revision_keys)
        exempt_clause = f" AND v.registry_key NOT IN ({placeholders})"
        exempt_params = tuple(sorted(full_history_revision_keys))
    vintage_concentration = _query_grouped_counts(
        conn,
        """
        WITH period_totals AS (
            SELECT o.registry_key, COUNT(DISTINCT o.observation_period) AS total_periods
            FROM macro_observation_raw o
            JOIN macro_metric_registry r ON r.registry_key = o.registry_key
            WHERE r.enabled = 1
              AND r.vintage_policy = 'true_vintage'
            GROUP BY o.registry_key
        ), vintage_counts AS (
            SELECT o.registry_key, o.vintage_date,
                   COUNT(DISTINCT o.observation_period) AS vintage_periods
            FROM macro_observation_raw o
            JOIN macro_metric_registry r ON r.registry_key = o.registry_key
            WHERE r.enabled = 1
              AND r.vintage_policy = 'true_vintage'
              AND o.vintage_date IS NOT NULL
            GROUP BY o.registry_key, o.vintage_date
        )
        SELECT v.registry_key,
               COUNT(*) AS suspicious_vintage_count,
               MAX(v.vintage_periods) AS max_periods_per_vintage,
               MAX(t.total_periods) AS total_periods
        FROM vintage_counts v
        JOIN period_totals t ON t.registry_key = v.registry_key
        WHERE t.total_periods >= 100
          AND v.vintage_periods >= 100
          AND (1.0 * v.vintage_periods / t.total_periods) >= 0.80
        """
        + exempt_clause
        + """
        GROUP BY v.registry_key
        HAVING COUNT(*) >= 3
        """,
        exempt_params,
    )
    for row in vintage_concentration:
        reg = _registry_row_for_issue(
            registry_by_key,
            str(row["registry_key"]),
            "implausible_vintage_concentration",
        )
        if reg is None:
            continue
        issues.append(
            _issue_tuple(
                qa_run_id=qa_run_id,
                severity="error",
                issue_type="implausible_vintage_concentration",
                registry_key=str(row["registry_key"]),
                metric_key=str(reg["metric_key"]),
                ref_area=str(reg["ref_area"]),
                source_name=str(reg["source_name"]),
                issue_count=int(row["suspicious_vintage_count"] or 0),
                details={
                    "suspicious_vintage_count": int(row["suspicious_vintage_count"] or 0),
                    "max_periods_per_vintage": int(row["max_periods_per_vintage"] or 0),
                    "total_periods": int(row["total_periods"] or 0),
                    "threshold": "at least 3 vintages each carrying >=80% of >=100 periods",
                },
            )
        )
    chronology_release = _query_grouped_counts(
        conn,
        """
        SELECT o.registry_key, COUNT(*) AS violation_count
        FROM macro_observation_raw o
        JOIN macro_metric_registry r
          ON r.registry_key = o.registry_key
        WHERE r.enabled = 1
          AND o.release_date IS NOT NULL
          AND o.observation_date IS NOT NULL
          AND o.release_date < o.observation_date
        GROUP BY o.registry_key
        HAVING COUNT(*) > 0
        """,
    )
    for row in chronology_release:
        reg = _registry_row_for_issue(
            registry_by_key,
            str(row["registry_key"]),
            "chronology_release_before_observation",
        )
        if reg is None:
            continue
        issues.append(
            _issue_tuple(
                qa_run_id=qa_run_id,
                severity="error",
                issue_type="chronology_release_before_observation",
                registry_key=str(row["registry_key"]),
                metric_key=str(reg["metric_key"]),
                ref_area=str(reg["ref_area"]),
                source_name=str(reg["source_name"]),
                issue_count=int(row["violation_count"] or 0),
                details=None,
            )
        )

    chronology_vintage = _query_grouped_counts(
        conn,
        """
        SELECT o.registry_key, COUNT(*) AS violation_count
        FROM macro_observation_raw o
        JOIN macro_metric_registry r
          ON r.registry_key = o.registry_key
        WHERE r.enabled = 1
          AND r.vintage_policy = 'true_vintage'
          AND o.release_date IS NOT NULL
          AND o.vintage_date IS NOT NULL
          AND o.vintage_date < o.release_date
        GROUP BY o.registry_key
        HAVING COUNT(*) > 0
        """,
    )
    for row in chronology_vintage:
        reg = _registry_row_for_issue(
            registry_by_key,
            str(row["registry_key"]),
            "chronology_vintage_before_release",
        )
        if reg is None:
            continue
        issues.append(
            _issue_tuple(
                qa_run_id=qa_run_id,
                severity="error",
                issue_type="chronology_vintage_before_release",
                registry_key=str(row["registry_key"]),
                metric_key=str(reg["metric_key"]),
                ref_area=str(reg["ref_area"]),
                source_name=str(reg["source_name"]),
                issue_count=int(row["violation_count"] or 0),
                details=None,
            )
        )

    future_dated = _query_grouped_counts(
        conn,
        """
        SELECT
            o.registry_key,
            SUM(CASE WHEN o.observation_date IS NOT NULL AND o.observation_date > ? THEN 1 ELSE 0 END) AS future_observation_count,
            SUM(CASE WHEN o.release_date IS NOT NULL AND o.release_date > ? THEN 1 ELSE 0 END) AS future_release_count,
            SUM(CASE WHEN o.vintage_date IS NOT NULL AND o.vintage_date > ? THEN 1 ELSE 0 END) AS future_vintage_count
        FROM macro_observation_raw o
        JOIN macro_metric_registry r
          ON r.registry_key = o.registry_key
        WHERE r.enabled = 1
        GROUP BY o.registry_key
        HAVING future_observation_count > 0 OR future_release_count > 0 OR future_vintage_count > 0
        """,
        (ingest_run.as_of_date, ingest_run.as_of_date, ingest_run.as_of_date),
    )
    for row in future_dated:
        reg = _registry_row_for_issue(
            registry_by_key,
            str(row["registry_key"]),
            "future_dated_rows",
        )
        if reg is None:
            continue
        details = {
            "future_observation_count": int(row["future_observation_count"] or 0),
            "future_release_count": int(row["future_release_count"] or 0),
            "future_vintage_count": int(row["future_vintage_count"] or 0),
        }
        issues.append(
            _issue_tuple(
                qa_run_id=qa_run_id,
                severity="error",
                issue_type="future_dated_rows",
                registry_key=str(row["registry_key"]),
                metric_key=str(reg["metric_key"]),
                ref_area=str(reg["ref_area"]),
                source_name=str(reg["source_name"]),
                issue_count=sum(details.values()),
                details=details,
            )
        )

    for metric_key, policy in policies.items():
        conditions, params = _value_rule_conditions(policy)
        params = [metric_key, *params]
        if not conditions:
            continue
        sql = f"""
            SELECT registry_key, COUNT(*) AS violation_count, MIN(value) AS min_bad_value, MAX(value) AS max_bad_value
            FROM macro_observation_raw
            WHERE metric_key = ?
              AND ({' OR '.join(conditions)})
            GROUP BY registry_key
        """
        rows = _query_grouped_counts(conn, sql, tuple(params))
        for row in rows:
            reg = registry_by_key.get(str(row["registry_key"]))
            if reg is None:
                continue
            issues.append(
                _issue_tuple(
                    qa_run_id=qa_run_id,
                    severity="error",
                    issue_type="value_rule_violation",
                    registry_key=str(row["registry_key"]),
                    metric_key=metric_key,
                    ref_area=str(reg["ref_area"]),
                    source_name=str(reg["source_name"]),
                    issue_count=int(row["violation_count"] or 0),
                    details={
                        "qa_rule": policy.qa_rule,
                        "qa_min_value": policy.qa_min_value,
                        "qa_max_value": policy.qa_max_value,
                        "min_bad_value": row["min_bad_value"],
                        "max_bad_value": row["max_bad_value"],
                    },
                )
            )

    for country in country_rows:
        if int(country["expected_metric_count"] or 0) == 0:
            issues.append(
                _issue_tuple(
                    qa_run_id=qa_run_id,
                    severity="error",
                    issue_type="country_expected_metric_count_zero",
                    registry_key=None,
                    metric_key=None,
                    ref_area=str(country["ref_area"]),
                    source_name=None,
                    issue_count=1,
                    details={"country_class": country["country_class"], "ticker": country["ticker"]},
                )
            )
        if int(country["required_metric_count"] or 0) > int(country["available_required_count"] or 0):
            issues.append(
                _issue_tuple(
                    qa_run_id=qa_run_id,
                    severity="error",
                    issue_type="country_required_coverage_gap",
                    registry_key=None,
                    metric_key=None,
                    ref_area=str(country["ref_area"]),
                    source_name=None,
                    issue_count=int(country["required_metric_count"] or 0) - int(country["available_required_count"] or 0),
                    details={
                        "ticker": country["ticker"],
                        "country_class": country["country_class"],
                        "required_metric_count": country["required_metric_count"],
                        "available_required_count": country["available_required_count"],
                        "missing_required_metrics_json": country["missing_required_metrics_json"],
                    },
                )
            )
        elif float(country["coverage_ratio"] or 0.0) < 1.0:
            issues.append(
                _issue_tuple(
                    qa_run_id=qa_run_id,
                    severity="warning",
                    issue_type="country_partial_coverage",
                    registry_key=None,
                    metric_key=None,
                    ref_area=str(country["ref_area"]),
                    source_name=None,
                    issue_count=int(country["stale_metric_count"] or 0),
                    details={
                        "ticker": country["ticker"],
                        "country_class": country["country_class"],
                        "coverage_ratio": country["coverage_ratio"],
                        "expected_metric_count": country["expected_metric_count"],
                        "available_metric_count": country["available_metric_count"],
                    },
                )
            )
    return issues


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, cfg = load_macro_raw_config(args.config)
    db_path = resolve_db_path(cfg, config_path, override=args.db_path)
    policy_path = args.policy_csv or resolve_path(
        config_path,
        str(cfg_get(cfg, "metric_policy_csv", default="MacroLayer/macro_metric_policy.csv")),
    )
    if policy_path is None:
        raise ValueError("macro_raw.metric_policy_csv is required for QA.")
    country_metadata_path = resolve_path(config_path, cfg_get(cfg, "country_metadata_csv", default=None))

    conn = connect_sqlite(db_path, row_factory=sqlite3.Row)
    try:
        init_db(conn)
        seed_country_metadata(conn, country_metadata_path)
        policies = load_metric_policy(policy_path)
        ingest_run = _select_ingest_run(conn, args.run_id)
        registry_rows = _load_enabled_registry_rows(conn)
        qa_run_id = uuid.uuid4().hex
        _start_qa_run(conn, qa_run_id, ingest_run, len(registry_rows))

        spans = _compute_span_rows(conn, qa_run_id, ingest_run.run_id)
        freshness_rows = _compute_freshness_rows(
            conn,
            qa_run_id=qa_run_id,
            ingest_run_id=ingest_run.run_id,
            as_of_date=ingest_run.as_of_date,
            spans=spans,
            policies=policies,
        )
        country_rows = _compute_country_coverage_rows(
            conn,
            qa_run_id=qa_run_id,
            ingest_run_id=ingest_run.run_id,
            country_rows=_load_country_rows(conn),
            registry_rows=registry_rows,
            freshness_rows=freshness_rows,
            policies=policies,
        )
        full_history_keys = frozenset(
            str(item)
            for item in (cfg_get(cfg, "qa_full_history_revision_registry_keys", default=None) or [])
        )
        issues = _build_issue_rows(
            conn,
            qa_run_id=qa_run_id,
            ingest_run=ingest_run,
            registry_rows=registry_rows,
            spans=spans,
            freshness_rows=freshness_rows,
            country_rows=country_rows,
            policies=policies,
            full_history_revision_keys=full_history_keys,
        )
        if issues:
            conn.executemany(
                """
                INSERT INTO macro_qa_issue (
                    qa_run_id, severity, issue_type, registry_key, metric_key, ref_area,
                    source_name, issue_count, details_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                issues,
            )
            conn.commit()
        error_count = sum(1 for issue in issues if issue[1] == "error")
        warning_count = sum(1 for issue in issues if issue[1] == "warning")
        status = "passed" if error_count == 0 else "failed"
        notes = f"QA completed for ingest_run_id={ingest_run.run_id}"
        _finish_qa_run(
            conn,
            qa_run_id=qa_run_id,
            status=status,
            issue_count=len(issues),
            error_count=error_count,
            warning_count=warning_count,
            notes=notes,
        )
        top_error_keys = _top_error_registry_keys(issues)
        logger.info(
            "Macro QA complete: qa_run_id=%s ingest_run_id=%s status=%s issues=%d errors=%d warnings=%d top_error_registry_keys=%s",
            qa_run_id,
            ingest_run.run_id,
            status,
            len(issues),
            error_count,
            warning_count,
            ",".join(top_error_keys) if top_error_keys else "none",
        )
    finally:
        conn.close()
    if error_count:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

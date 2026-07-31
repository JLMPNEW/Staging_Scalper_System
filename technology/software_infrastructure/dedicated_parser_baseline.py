from __future__ import annotations

import csv
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from technology.core.config import load_yaml


MODEL_FAMILY = "software_infrastructure"
ALLOWED_APPLICABILITY = frozenset({"universal", "conditional", "diagnostic"})
ALLOWED_EXTRACTION_POLICIES = frozenset({"recover_if_missing", "parser_primary"})


@dataclass(frozen=True)
class MetricDefinition:
    metric_name: str
    definition_version: str
    tier: int
    extraction_policy: str
    value_type: str
    period_type: str
    canonical_metrics: tuple[str, ...]
    concept_tokens: tuple[str, ...]
    feature_fields: tuple[str, ...]


@dataclass(frozen=True)
class MetricRegistry:
    registry_version: str
    model_family: str
    history_start_date: str
    filing_forms: tuple[str, ...]
    metrics: tuple[MetricDefinition, ...]
    raw: dict[str, Any]


@dataclass(frozen=True)
class UniverseMember:
    ticker: str
    cik: str
    company_name: str
    cohort_id: str
    start_date: str
    end_date: str
    membership_status: str
    point_in_time_flag: int


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso_date(value: str, *, field_name: str) -> str:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field_name} must be YYYY-MM-DD; received {value!r}") from exc


def open_read_only_database(path: Path, *, timeout_sec: float) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Technology database not found: {resolved}")
    uri = f"file:{resolved.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout_sec)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {max(1, int(timeout_sec * 1000))}")
    conn.execute("PRAGMA query_only = ON")
    return conn


def load_metric_registry(path: Path) -> MetricRegistry:
    payload = load_yaml(path)
    model_family = str(payload.get("model_family") or "").strip()
    if model_family != MODEL_FAMILY:
        raise ValueError(
            f"Metric registry model_family must be {MODEL_FAMILY!r}; found {model_family!r}"
        )
    registry_version = str(payload.get("registry_version") or "").strip()
    if not registry_version:
        raise ValueError("Metric registry must define registry_version")
    history_start_date = parse_iso_date(
        str(payload.get("history_start_date") or ""),
        field_name="history_start_date",
    )
    filing_scope = payload.get("filing_scope")
    if not isinstance(filing_scope, dict):
        raise ValueError("Metric registry filing_scope must be a mapping")
    filing_forms: list[str] = []
    for key in ("periodic_forms", "event_forms", "registration_forms"):
        values = filing_scope.get(key, [])
        if not isinstance(values, list):
            raise ValueError(f"filing_scope.{key} must be a list")
        filing_forms.extend(str(value).strip().upper() for value in values if str(value).strip())

    metric_payloads = payload.get("metrics")
    if not isinstance(metric_payloads, list) or not metric_payloads:
        raise ValueError("Metric registry must contain a non-empty metrics list")
    metrics: list[MetricDefinition] = []
    seen: set[str] = set()
    for item in metric_payloads:
        if not isinstance(item, dict):
            raise ValueError("Every metric registry item must be a mapping")
        metric_name = str(item.get("metric_name") or "").strip()
        if not metric_name or metric_name in seen:
            raise ValueError(f"Metric names must be non-empty and unique: {metric_name!r}")
        seen.add(metric_name)
        extraction_policy = str(item.get("extraction_policy") or "").strip()
        if extraction_policy not in ALLOWED_EXTRACTION_POLICIES:
            raise ValueError(
                f"{metric_name}: unsupported extraction_policy={extraction_policy!r}"
            )
        metrics.append(
            MetricDefinition(
                metric_name=metric_name,
                definition_version=str(item.get("definition_version") or "").strip(),
                tier=int(item.get("tier") or 0),
                extraction_policy=extraction_policy,
                value_type=str(item.get("value_type") or "").strip(),
                period_type=str(item.get("period_type") or "").strip(),
                canonical_metrics=tuple(
                    str(value).strip()
                    for value in item.get("canonical_metrics", [])
                    if str(value).strip()
                ),
                concept_tokens=tuple(
                    str(value).strip().lower()
                    for value in item.get("concept_tokens", [])
                    if str(value).strip()
                ),
                feature_fields=tuple(
                    str(value).strip()
                    for value in item.get("feature_fields", [])
                    if str(value).strip()
                ),
            )
        )
    invalid = [
        metric.metric_name
        for metric in metrics
        if not metric.definition_version
        or metric.tier not in {1, 2, 3}
        or not metric.value_type
        or not metric.period_type
    ]
    if invalid:
        raise ValueError(f"Incomplete metric definitions: {invalid}")
    return MetricRegistry(
        registry_version=registry_version,
        model_family=model_family,
        history_start_date=history_start_date,
        filing_forms=tuple(dict.fromkeys(filing_forms)),
        metrics=tuple(metrics),
        raw=payload,
    )


def load_applicability_rows(
    path: Path,
    *,
    registry: MetricRegistry,
) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [
            {str(key): str(value or "").strip() for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]
    if not rows:
        raise ValueError(f"Applicability contract is empty: {path}")
    required = {
        "metric_name",
        "cohort_id",
        "applicability",
        "applicability_rule",
        "cross_section_policy",
        "notes",
    }
    missing_columns = sorted(required - set(rows[0]))
    if missing_columns:
        raise ValueError(f"Applicability contract missing columns: {missing_columns}")
    registry_metrics = {metric.metric_name for metric in registry.metrics}
    unknown_metrics = sorted({row["metric_name"] for row in rows} - registry_metrics)
    missing_metrics = sorted(registry_metrics - {row["metric_name"] for row in rows})
    invalid_values = sorted(
        {
            row["applicability"]
            for row in rows
            if row["applicability"] not in ALLOWED_APPLICABILITY
        }
    )
    duplicate_keys = [
        key
        for key, count in Counter(
            (row["metric_name"], row["cohort_id"]) for row in rows
        ).items()
        if count > 1
    ]
    if unknown_metrics or missing_metrics or invalid_values or duplicate_keys:
        raise ValueError(
            "Invalid applicability contract: "
            f"unknown_metrics={unknown_metrics}, missing_metrics={missing_metrics}, "
            f"invalid_values={invalid_values}, duplicate_keys={duplicate_keys}"
        )
    return rows


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table_name})")}


def load_universe_members(
    conn: sqlite3.Connection,
    *,
    history_start_date: str,
    asof_date: str,
) -> list[UniverseMember]:
    rows = conn.execute(
        """
        SELECT
            m.ticker,
            COALESCE(c.cik, '') AS cik,
            COALESCE(c.company_name, '') AS company_name,
            COALESCE(t.calibration_cohort_id, '') AS cohort_id,
            m.start_date,
            COALESCE(m.end_date, '') AS end_date,
            m.membership_status,
            m.point_in_time_flag
        FROM dim_universe_membership AS m
        LEFT JOIN dim_company AS c
          ON c.company_id = m.company_id
        LEFT JOIN dim_technology_taxonomy AS t
          ON t.model_family = m.model_family
         AND t.ticker = m.ticker
        WHERE m.model_family = ?
          AND m.start_date <= ?
          AND COALESCE(NULLIF(m.end_date, ''), '9999-12-31') >= ?
          AND m.membership_status IN ('active', 'historical', 'inactive', 'review')
        ORDER BY m.ticker, m.start_date
        """,
        (MODEL_FAMILY, asof_date, history_start_date),
    ).fetchall()
    members: dict[str, UniverseMember] = {}
    for row in rows:
        ticker = str(row["ticker"] or "").strip().upper()
        if not ticker:
            continue
        candidate = UniverseMember(
            ticker=ticker,
            cik=str(row["cik"] or "").strip().zfill(10) if str(row["cik"] or "").strip() else "",
            company_name=str(row["company_name"] or "").strip(),
            cohort_id=str(row["cohort_id"] or "").strip(),
            start_date=str(row["start_date"] or "").strip(),
            end_date=str(row["end_date"] or "").strip(),
            membership_status=str(row["membership_status"] or "").strip(),
            point_in_time_flag=int(row["point_in_time_flag"] or 0),
        )
        previous = members.get(ticker)
        if previous is None or candidate.start_date > previous.start_date:
            members[ticker] = candidate
    return [members[ticker] for ticker in sorted(members)]

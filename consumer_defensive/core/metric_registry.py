from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


ALLOWED_COHORTS = frozenset(
    {
        "beverages",
        "consumer_staples_distribution_retail",
        "household_personal_tobacco",
        "packaged_foods_agricultural_products",
    }
)
ALLOWED_INITIAL_STATUSES = frozenset(
    {"research_candidate", "measurement_only", "deferred", "rejected"}
)


@dataclass(frozen=True)
class SpecializedMetric:
    metric_id: str
    cohorts: tuple[str, ...]
    applicability_subtypes: tuple[str, ...]
    unit_family: str
    direction_hint: str
    purpose: str
    initial_status: str
    production_weight: float


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_metric_registry(path: Path) -> tuple[str, list[SpecializedMetric]]:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Specialized metric registry not found: {resolved}")
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to load the specialized metric registry.") from exc
    payload = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError("Specialized metric registry root must be a mapping.")
    if str(payload.get("model_family") or "") != "consumer_defensive":
        raise ValueError("Specialized metric registry model_family must be consumer_defensive.")
    version = str(payload.get("registry_version") or "").strip()
    if not version:
        raise ValueError("Specialized metric registry_version is required.")
    raw_metrics = payload.get("metrics")
    if not isinstance(raw_metrics, list) or not raw_metrics:
        raise ValueError("Specialized metric registry must contain a non-empty metrics list.")

    metrics: list[SpecializedMetric] = []
    seen: set[str] = set()
    allowed_root_keys = {
        'model_family',
        'registry_version',
        'default_initial_status',
        'default_production_weight',
        'admission_stage',
        'scoring_evidence_stage',
        'source_priority',
        'metrics',
    }
    unknown_root = sorted(set(payload) - allowed_root_keys)
    if unknown_root:
        raise ValueError(f'Specialized metric registry has unknown fields: {unknown_root}')
    allowed_metric_keys = set(SpecializedMetric.__dataclass_fields__)
    for index, raw in enumerate(raw_metrics, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"Specialized metric row {index} must be a mapping.")
        unknown = sorted(set(raw) - allowed_metric_keys)
        if unknown:
            raise ValueError(f'Specialized metric row {index} has unknown fields: {unknown}')
        metric_id = str(raw.get("metric_id") or "").strip()
        if not metric_id:
            raise ValueError(f"Specialized metric row {index} is missing metric_id.")
        if metric_id in seen:
            raise ValueError(f"Duplicate specialized metric_id: {metric_id}")
        seen.add(metric_id)
        cohorts = tuple(str(value).strip() for value in raw.get("cohorts", []) if str(value).strip())
        if not cohorts or not set(cohorts).issubset(ALLOWED_COHORTS):
            raise ValueError(f"Metric {metric_id} has invalid cohorts: {cohorts}")
        subtypes = tuple(
            str(value).strip()
            for value in raw.get("applicability_subtypes", [])
            if str(value).strip()
        )
        if not subtypes:
            raise ValueError(f"Metric {metric_id} requires applicability_subtypes.")
        if len(set(cohorts)) != len(cohorts) or len(set(subtypes)) != len(subtypes):
            raise ValueError(f'Metric {metric_id} has duplicate cohort or subtype entries.')
        status = str(raw.get("initial_status") or "").strip()
        if status not in ALLOWED_INITIAL_STATUSES:
            raise ValueError(f"Metric {metric_id} has invalid initial_status {status!r}.")
        weight = float(raw.get("production_weight") or 0.0)
        if weight != 0.0:
            raise ValueError(f"Stage 0 metric {metric_id} must have production_weight=0.")
        for field in ('unit_family', 'direction_hint', 'purpose'):
            if not str(raw.get(field) or '').strip():
                raise ValueError(f'Metric {metric_id} requires {field}.')
        metrics.append(
            SpecializedMetric(
                metric_id=metric_id,
                cohorts=cohorts,
                applicability_subtypes=subtypes,
                unit_family=str(raw.get("unit_family") or "").strip(),
                direction_hint=str(raw.get("direction_hint") or "").strip(),
                purpose=str(raw.get("purpose") or "").strip(),
                initial_status=status,
                production_weight=weight,
            )
        )
    return version, metrics


def upsert_metric_registry(
    conn: sqlite3.Connection,
    *,
    registry_version: str,
    metrics: list[SpecializedMetric],
) -> int:
    now = utc_now()
    with conn:
        metric_ids = [metric.metric_id for metric in metrics]
        if not metric_ids:
            raise ValueError('Cannot synchronize an empty specialized metric registry.')
        placeholders = ','.join('?' for _ in metric_ids)
        conn.execute(
            f'''UPDATE dim_specialized_metric
                SET production_status='retired', production_weight=0.0, updated_at=?
                WHERE metric_id NOT IN ({placeholders})''',
            (now, *metric_ids),
        )
        for metric in metrics:
            conn.execute(
                """
                INSERT INTO dim_specialized_metric(
                    metric_id, registry_version, cohorts_json, applicability_subtypes_json,
                    unit_family, direction_hint, purpose, production_status,
                    production_weight, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(metric_id) DO UPDATE SET
                    registry_version=excluded.registry_version,
                    cohorts_json=excluded.cohorts_json,
                    applicability_subtypes_json=excluded.applicability_subtypes_json,
                    unit_family=excluded.unit_family,
                    direction_hint=excluded.direction_hint,
                    purpose=excluded.purpose,
                    production_status=excluded.production_status,
                    production_weight=excluded.production_weight,
                    updated_at=excluded.updated_at
                """,
                (
                    metric.metric_id,
                    registry_version,
                    json.dumps(metric.cohorts),
                    json.dumps(metric.applicability_subtypes),
                    metric.unit_family,
                    metric.direction_hint,
                    metric.purpose,
                    metric.initial_status,
                    metric.production_weight,
                    now,
                    now,
                ),
            )
    return len(metrics)

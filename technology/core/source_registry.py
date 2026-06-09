from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SourceRegistryRow:
    source_id: str
    stage: str
    source_name: str
    source_owner: str
    source_type: str
    base_url: str
    documentation_url: str = ""
    authentication_required: int = 0
    free_key_required: int = 0
    api_key_env: str = ""
    rate_limit_notes: str = ""
    refresh_frequency: str = ""
    terms_url: str = ""
    data_owner: str = ""
    raw_schema: str = ""
    canonical_tables: str = ""
    feature_stages: str = ""
    subsector_scope: str = "technology"
    priority: int = 100
    status: str = "planned"
    notes: str = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _as_text(raw: Any) -> str:
    if isinstance(raw, (list, tuple, dict)):
        return json.dumps(raw, ensure_ascii=False, sort_keys=True)
    return str(raw or "").strip()


def _as_int(raw: Any, default: int = 0) -> int:
    if isinstance(raw, bool):
        return int(raw)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(default)


def load_source_registry(path: Path) -> list[SourceRegistryRow]:
    if not path.exists():
        raise FileNotFoundError(f"Source registry YAML not found: {path}")
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to load source registry YAML.") from exc
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows_raw = payload.get("sources", payload)
    if not isinstance(rows_raw, list):
        raise ValueError(f"Source registry must be a list or contain a sources list: {path}")

    rows: list[SourceRegistryRow] = []
    seen: set[str] = set()
    for idx, raw in enumerate(rows_raw, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"Source registry row {idx} must be a mapping.")
        source_id = _as_text(raw.get("source_id"))
        if not source_id:
            raise ValueError(f"Source registry row {idx} is missing source_id.")
        if source_id in seen:
            raise ValueError(f"Duplicate source_id in {path}: {source_id}")
        seen.add(source_id)
        rows.append(
            SourceRegistryRow(
                source_id=source_id,
                stage=_as_text(raw.get("stage")),
                source_name=_as_text(raw.get("source_name")),
                source_owner=_as_text(raw.get("source_owner")),
                source_type=_as_text(raw.get("source_type")),
                base_url=_as_text(raw.get("base_url")),
                documentation_url=_as_text(raw.get("documentation_url")),
                authentication_required=_as_int(raw.get("authentication_required"), 0),
                free_key_required=_as_int(raw.get("free_key_required"), 0),
                api_key_env=_as_text(raw.get("api_key_env")),
                rate_limit_notes=_as_text(raw.get("rate_limit_notes")),
                refresh_frequency=_as_text(raw.get("refresh_frequency")),
                terms_url=_as_text(raw.get("terms_url")),
                data_owner=_as_text(raw.get("data_owner")),
                raw_schema=_as_text(raw.get("raw_schema")),
                canonical_tables=_as_text(raw.get("canonical_tables")),
                feature_stages=_as_text(raw.get("feature_stages")),
                subsector_scope=_as_text(raw.get("subsector_scope")) or "technology",
                priority=_as_int(raw.get("priority"), 100),
                status=_as_text(raw.get("status")) or "planned",
                notes=_as_text(raw.get("notes")),
            )
        )
    return rows


def upsert_source_registry(conn: sqlite3.Connection, rows: list[SourceRegistryRow]) -> int:
    now = utc_now()
    with conn:
        for row in rows:
            conn.execute(
                """
                INSERT INTO source_registry(
                    source_id, stage, source_name, source_owner, source_type, base_url,
                    documentation_url, authentication_required, free_key_required, api_key_env,
                    rate_limit_notes, refresh_frequency, terms_url, data_owner, raw_schema,
                    canonical_tables, feature_stages, subsector_scope, priority, status, notes,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    stage = excluded.stage,
                    source_name = excluded.source_name,
                    source_owner = excluded.source_owner,
                    source_type = excluded.source_type,
                    base_url = excluded.base_url,
                    documentation_url = excluded.documentation_url,
                    authentication_required = excluded.authentication_required,
                    free_key_required = excluded.free_key_required,
                    api_key_env = excluded.api_key_env,
                    rate_limit_notes = excluded.rate_limit_notes,
                    refresh_frequency = excluded.refresh_frequency,
                    terms_url = excluded.terms_url,
                    data_owner = excluded.data_owner,
                    raw_schema = excluded.raw_schema,
                    canonical_tables = excluded.canonical_tables,
                    feature_stages = excluded.feature_stages,
                    subsector_scope = excluded.subsector_scope,
                    priority = excluded.priority,
                    status = excluded.status,
                    notes = excluded.notes,
                    updated_at = excluded.updated_at
                """,
                (
                    row.source_id,
                    row.stage,
                    row.source_name,
                    row.source_owner,
                    row.source_type,
                    row.base_url,
                    row.documentation_url,
                    row.authentication_required,
                    row.free_key_required,
                    row.api_key_env,
                    row.rate_limit_notes,
                    row.refresh_frequency,
                    row.terms_url,
                    row.data_owner,
                    row.raw_schema,
                    row.canonical_tables,
                    row.feature_stages,
                    row.subsector_scope,
                    row.priority,
                    row.status,
                    row.notes,
                    now,
                    now,
                ),
            )
    return len(rows)


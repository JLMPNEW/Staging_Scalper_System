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
    staging_tables: str = ""
    canonical_tables: str = ""
    feature_stages: str = ""
    subsector_scope: str = "consumer_defensive"
    priority: int = 100
    status: str = "planned"
    notes: str = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _text(raw: Any) -> str:
    if isinstance(raw, (list, tuple, dict)):
        return json.dumps(raw, ensure_ascii=False, sort_keys=True)
    return str(raw or "").strip()


def _integer(raw: Any, default: int = 0) -> int:
    if isinstance(raw, bool):
        return int(raw)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(default)


def load_source_registry(path: Path) -> list[SourceRegistryRow]:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Consumer Defensive source registry not found: {resolved}")
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to load the Consumer Defensive source registry.") from exc
    payload = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    raw_rows = payload.get("sources", payload) if isinstance(payload, dict) else payload
    if not isinstance(raw_rows, list):
        raise ValueError(f"Source registry must be a list or contain a sources list: {resolved}")

    rows: list[SourceRegistryRow] = []
    seen: set[str] = set()
    allowed_keys = set(SourceRegistryRow.__dataclass_fields__)
    required_text = {
        'source_id',
        'stage',
        'source_name',
        'source_owner',
        'source_type',
        'base_url',
        'status',
    }
    for index, raw in enumerate(raw_rows, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"Source registry row {index} must be a mapping.")
        unknown = sorted(set(raw) - allowed_keys)
        if unknown:
            raise ValueError(f'Source registry row {index} has unknown fields: {unknown}')
        missing = sorted(key for key in required_text if not _text(raw.get(key)))
        if missing:
            raise ValueError(f'Source registry row {index} has blank required fields: {missing}')
        for flag in ('authentication_required', 'free_key_required'):
            if raw.get(flag, 0) not in (0, 1, False, True):
                raise ValueError(f'Source registry row {index} has invalid {flag}.')
        try:
            priority = int(raw.get('priority', 100))
        except (TypeError, ValueError) as exc:
            raise ValueError(f'Source registry row {index} has invalid priority.') from exc
        if priority < 0:
            raise ValueError(f'Source registry row {index} has negative priority.')
        if _text(raw.get('status')) not in {'active', 'planned', 'retired'}:
            raise ValueError(f'Source registry row {index} has invalid status.')
        source_id = _text(raw.get("source_id"))
        if not source_id:
            raise ValueError(f"Source registry row {index} is missing source_id.")
        if source_id in seen:
            raise ValueError(f"Duplicate source_id in {resolved}: {source_id}")
        seen.add(source_id)
        scope = _text(raw.get("subsector_scope")) or "consumer_defensive"
        if scope != "consumer_defensive":
            raise ValueError(f"Source {source_id} has cross-sector scope {scope!r}.")
        rows.append(
            SourceRegistryRow(
                source_id=source_id,
                stage=_text(raw.get("stage")),
                source_name=_text(raw.get("source_name")),
                source_owner=_text(raw.get("source_owner")),
                source_type=_text(raw.get("source_type")),
                base_url=_text(raw.get("base_url")),
                documentation_url=_text(raw.get("documentation_url")),
                authentication_required=_integer(raw.get("authentication_required")),
                free_key_required=_integer(raw.get("free_key_required")),
                api_key_env=_text(raw.get("api_key_env")),
                rate_limit_notes=_text(raw.get("rate_limit_notes")),
                refresh_frequency=_text(raw.get("refresh_frequency")),
                terms_url=_text(raw.get("terms_url")),
                data_owner=_text(raw.get("data_owner")),
                raw_schema=_text(raw.get("raw_schema")),
                staging_tables=_text(raw.get("staging_tables")),
                canonical_tables=_text(raw.get("canonical_tables")),
                feature_stages=_text(raw.get("feature_stages")),
                subsector_scope=scope,
                priority=_integer(raw.get("priority"), 100),
                status=_text(raw.get("status")) or "planned",
                notes=_text(raw.get("notes")),
            )
        )
    return rows


def upsert_source_registry(
    conn: sqlite3.Connection,
    rows: list[SourceRegistryRow],
    *,
    retire_absent: bool = False,
) -> int:
    now = utc_now()
    columns = tuple(SourceRegistryRow.__dataclass_fields__)
    assignments = ", ".join(f"{column}=excluded.{column}" for column in columns if column != "source_id")
    placeholders = ", ".join("?" for _ in columns)
    sql = (
        f"INSERT INTO source_registry({', '.join(columns)}, created_at, updated_at) "
        f"VALUES ({placeholders}, ?, ?) "
        f"ON CONFLICT(source_id) DO UPDATE SET {assignments}, updated_at=excluded.updated_at"
    )
    with conn:
        for row in rows:
            values = tuple(getattr(row, column) for column in columns)
            conn.execute(sql, (*values, now, now))
        if retire_absent:
            source_ids = [row.source_id for row in rows]
            if not source_ids:
                raise ValueError('Cannot retire absent sources from an empty registry snapshot.')
            placeholders = ','.join('?' for _ in source_ids)
            conn.execute(
                f'''UPDATE source_registry
                    SET status='retired',
                        notes=CASE
                            WHEN COALESCE(notes, '')='' THEN 'Retired: absent from authoritative registry snapshot.'
                            ELSE notes || ' Retired: absent from authoritative registry snapshot.'
                        END,
                        updated_at=?
                    WHERE source_id NOT IN ({placeholders}) AND status<>'retired' ''',
                (now, *source_ids),
            )
    return len(rows)

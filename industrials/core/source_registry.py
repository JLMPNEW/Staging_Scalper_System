from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from industrials.core.config import load_yaml
from industrials.core.db import utc_now


LIST_FIELDS = {"raw_schema", "staging_tables", "canonical_tables", "feature_stages"}
BOOL_FIELDS = {"authentication_required", "free_key_required"}


def _as_json_list(raw: Any) -> str:
    if raw is None:
        return "[]"
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        raise ValueError(f"Expected list-like source registry value, got {type(raw).__name__}")
    return json.dumps([str(value) for value in values if str(value).strip()], ensure_ascii=True, sort_keys=True)


def _as_bool_int(raw: Any) -> int:
    if isinstance(raw, bool):
        return 1 if raw else 0
    return 1 if str(raw or "").strip().lower() in {"1", "true", "t", "yes", "y"} else 0


def _as_priority(raw: Any) -> int:
    if raw is None:
        return 100
    text = str(raw).strip()
    return int(text) if text else 100


def _normalize_source_row(row: dict[str, Any]) -> dict[str, Any]:
    required = ["source_id", "stage", "source_name", "source_type", "base_url"]
    missing = [field for field in required if not str(row.get(field) or "").strip()]
    if missing:
        raise ValueError(f"Source registry row missing required field(s): {', '.join(missing)}")
    out = dict(row)
    for field in LIST_FIELDS:
        out[field] = _as_json_list(row.get(field))
    for field in BOOL_FIELDS:
        out[field] = _as_bool_int(row.get(field))
    out["priority"] = _as_priority(row.get("priority"))
    out["status"] = str(row.get("status") or "planned").strip() or "planned"
    return out


def load_source_registry(path: Path) -> list[dict[str, Any]]:
    payload = load_yaml(path)
    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list):
        raise ValueError(f"Source registry YAML must contain a 'sources' list: {path}")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_sources:
        if not isinstance(raw, dict):
            raise ValueError(f"Source registry rows must be mappings: {path}")
        row = _normalize_source_row(raw)
        source_id = str(row["source_id"])
        if source_id in seen:
            raise ValueError(f"Duplicate source_id in source registry: {source_id}")
        seen.add(source_id)
        rows.append(row)
    rows.sort(key=lambda item: (_as_priority(item.get("priority")), str(item.get("source_id") or "")))
    return rows


def upsert_source_registry(conn: sqlite3.Connection, sources: list[dict[str, Any]]) -> int:
    if not sources:
        return 0
    now = utc_now()
    fields = [
        "source_id",
        "stage",
        "source_name",
        "source_owner",
        "source_type",
        "base_url",
        "documentation_url",
        "authentication_required",
        "free_key_required",
        "api_key_env",
        "rate_limit_notes",
        "refresh_frequency",
        "terms_url",
        "data_owner",
        "raw_schema",
        "staging_tables",
        "canonical_tables",
        "feature_stages",
        "subsector_scope",
        "priority",
        "status",
        "notes",
    ]
    update_fields = [field for field in fields if field != "source_id"]
    placeholders = ", ".join("?" for _ in [*fields, "created_at", "updated_at"])
    update_sql = ", ".join(f"{field} = excluded.{field}" for field in [*update_fields, "updated_at"])
    conn.executemany(
        f"""
        INSERT INTO source_registry({", ".join([*fields, "created_at", "updated_at"])})
        VALUES ({placeholders})
        ON CONFLICT(source_id) DO UPDATE SET
            {update_sql}
        """,
        [tuple(row.get(field) for field in fields) + (now, now) for row in sources],
    )
    return len(sources)


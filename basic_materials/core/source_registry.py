"""Versioned source registry for the Basic Materials package."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import sqlite3
from typing import Any, Mapping

import yaml


class SourceRegistryError(ValueError):
    """Raised when the package source registry is invalid."""


@dataclass(frozen=True)
class SourceDefinition:
    source_id: str
    stage: str
    provider: str
    category: str
    description: str
    point_in_time_role: str
    active: bool


@dataclass(frozen=True)
class SourceRegistry:
    version: str
    checksum: str
    sources: tuple[SourceDefinition, ...]


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SourceRegistryError(f"{context} must be a mapping")
    return value


def load_source_registry(path: str | Path) -> SourceRegistry:
    registry_path = Path(path).resolve()
    if not registry_path.is_file():
        raise SourceRegistryError(f"Source registry not found: {registry_path}")
    payload = registry_path.read_bytes()
    raw = yaml.safe_load(payload.decode("utf-8"))
    root = _mapping(raw, "source registry")
    if set(root) != {"registry_version", "sources"}:
        raise SourceRegistryError("Source registry must contain only registry_version and sources")
    version = str(root["registry_version"])
    if version != "basic_materials_source_registry_v2":
        raise SourceRegistryError(f"Unsupported source registry version: {version}")
    raw_sources = root["sources"]
    if not isinstance(raw_sources, list) or not raw_sources:
        raise SourceRegistryError("Source registry must contain at least one source")

    expected_keys = {
        "source_id",
        "stage",
        "provider",
        "category",
        "description",
        "point_in_time_role",
        "active",
    }
    sources: list[SourceDefinition] = []
    seen: set[str] = set()
    for index, raw_source in enumerate(raw_sources):
        source = _mapping(raw_source, f"sources[{index}]")
        if set(source) != expected_keys:
            raise SourceRegistryError(f"Invalid keys for sources[{index}]")
        source_id = str(source["source_id"]).strip()
        if not source_id or source_id in seen:
            raise SourceRegistryError(f"Invalid or duplicate source_id: {source_id!r}")
        if not isinstance(source["active"], bool):
            raise SourceRegistryError(f"sources[{index}].active must be true or false")
        seen.add(source_id)
        sources.append(
            SourceDefinition(
                source_id=source_id,
                stage=str(source["stage"]).strip(),
                provider=str(source["provider"]).strip(),
                category=str(source["category"]).strip(),
                description=str(source["description"]).strip(),
                point_in_time_role=str(source["point_in_time_role"]).strip(),
                active=source["active"],
            )
        )
    return SourceRegistry(
        version=version,
        checksum=hashlib.sha256(payload).hexdigest(),
        sources=tuple(sources),
    )


def upsert_source_registry(conn: sqlite3.Connection, registry: SourceRegistry, loaded_at_utc: str) -> int:
    for source in registry.sources:
        conn.execute(
            """
            INSERT INTO source_registry (
                source_id, stage, provider, category, description, point_in_time_role,
                active, registry_version, registry_checksum, loaded_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                stage = excluded.stage,
                provider = excluded.provider,
                category = excluded.category,
                description = excluded.description,
                point_in_time_role = excluded.point_in_time_role,
                active = excluded.active,
                registry_version = excluded.registry_version,
                registry_checksum = excluded.registry_checksum,
                loaded_at_utc = excluded.loaded_at_utc
            """,
            (
                source.source_id,
                source.stage,
                source.provider,
                source.category,
                source.description,
                source.point_in_time_role,
                int(source.active),
                registry.version,
                registry.checksum,
                loaded_at_utc,
            ),
        )
    return len(registry.sources)

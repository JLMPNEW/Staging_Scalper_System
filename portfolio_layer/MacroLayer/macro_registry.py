#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from macro_raw_config import parse_boolish, parse_iso_date


def _parse_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_json_dict(value: Any) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed source_params_json; not valid JSON: {text!r}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object, got {type(payload).__name__}")
    return payload


@dataclass(frozen=True)
class MetricSpec:
    registry_key: str
    metric_key: str
    regime_block: str
    source_name: str
    source_dataset: str | None
    source_series_id: str | None
    ref_area: str
    frequency: str
    seasonal_adjustment: str | None
    units: str | None
    vintage_policy: str
    update_cadence: str
    history_start_date: date | None
    revision_window_days: int
    source_priority: int
    worker_hint: int
    enabled: bool
    source_params: dict[str, Any]
    notes: str | None


def load_metric_registry(registry_csv: Path) -> list[MetricSpec]:
    if not registry_csv.exists():
        raise FileNotFoundError(f"Macro registry CSV not found: {registry_csv}")
    specs: list[MetricSpec] = []
    with open(registry_csv, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            registry_key = str(row.get("registry_key", "") or "").strip()
            metric_key = str(row.get("metric_key", "") or "").strip()
            source_name = str(row.get("source_name", "") or "").strip()
            enabled = parse_boolish(row.get("enabled"), default=True)
            if not registry_key or not metric_key or not source_name:
                raise ValueError(f"Registry row missing required identifiers: {row}")
            source_series_id = str(row.get("source_series_id", "") or "").strip() or None
            if enabled and source_name != "phillyfed_ads" and not source_series_id:
                raise ValueError(
                    f"Enabled registry row {registry_key} is missing source_series_id."
                )
            try:
                source_params = _parse_json_dict(row.get("source_params_json"))
            except ValueError as exc:
                raise ValueError(f"Registry row {registry_key} has invalid source_params_json: {exc}") from exc
            specs.append(
                MetricSpec(
                    registry_key=registry_key,
                    metric_key=metric_key,
                    regime_block=str(row.get("regime_block", "") or "").strip(),
                    source_name=source_name,
                    source_dataset=str(row.get("source_dataset", "") or "").strip() or None,
                    source_series_id=source_series_id,
                    ref_area=str(row.get("ref_area", "") or "").strip() or "USA",
                    frequency=str(row.get("frequency", "") or "").strip() or "unknown",
                    seasonal_adjustment=str(row.get("seasonal_adjustment", "") or "").strip() or None,
                    units=str(row.get("units", "") or "").strip() or None,
                    vintage_policy=str(row.get("vintage_policy", "") or "").strip() or "none",
                    update_cadence=str(row.get("update_cadence", "") or "").strip() or "daily",
                    history_start_date=parse_iso_date(row.get("history_start_date")),
                    revision_window_days=max(0, _parse_int(row.get("revision_window_days"), default=0)),
                    source_priority=max(1, _parse_int(row.get("source_priority"), default=1)),
                    worker_hint=max(1, _parse_int(row.get("worker_hint"), default=1)),
                    enabled=enabled,
                    source_params=source_params,
                    notes=str(row.get("notes", "") or "").strip() or None,
                )
            )
    return specs


def enabled_specs(specs: Iterable[MetricSpec]) -> list[MetricSpec]:
    return [spec for spec in specs if spec.enabled]


def filter_specs_by_sources(specs: Iterable[MetricSpec], source_names: set[str] | None) -> list[MetricSpec]:
    if not source_names:
        return list(specs)
    normalized = {str(item).strip().lower() for item in source_names if str(item).strip()}
    return [spec for spec in specs if spec.source_name.lower() in normalized]


def group_specs_by_source(specs: Iterable[MetricSpec]) -> dict[str, list[MetricSpec]]:
    grouped: dict[str, list[MetricSpec]] = {}
    for spec in specs:
        grouped.setdefault(spec.source_name, []).append(spec)
    for source_name in grouped:
        grouped[source_name].sort(key=lambda item: (item.source_priority, item.metric_key, item.registry_key))
    return grouped

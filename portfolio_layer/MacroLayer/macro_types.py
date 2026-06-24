#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from macro_registry import MetricSpec


@dataclass(frozen=True)
class FetchTask:
    spec: MetricSpec
    observation_start: date | None
    observation_end: date | None
    vintage_start: date | None
    as_of_date: date


@dataclass(frozen=True)
class ObservationRecord:
    metric_key: str
    source_name: str
    source_dataset: str | None
    source_series_id: str | None
    ref_area: str
    frequency: str
    seasonal_adjustment: str | None
    units: str | None
    observation_period: str
    observation_date: str | None
    release_date: str | None
    vintage_date: str | None
    value: float
    source_last_updated: str | None
    retrieved_at: str
    revision_flag: int
    notes_hash: str | None


@dataclass(frozen=True)
class SourceArtifact:
    registry_key: str
    source_name: str
    request_url: str
    payload_hash: str | None
    http_status: int | None
    fetched_at: str
    row_count: int
    error_text: str | None = None
    extra_json: dict[str, Any] = field(default_factory=dict)


@dataclass
class FetchResult:
    spec: MetricSpec
    observations: list[ObservationRecord] = field(default_factory=list)
    artifacts: list[SourceArtifact] = field(default_factory=list)
    source_last_updated: str | None = None
    error_text: str | None = None

    @property
    def row_count(self) -> int:
        return len(self.observations)

#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import logging
from typing import Any

from macro_http import HttpClient
from macro_types import FetchResult, FetchTask, ObservationRecord, SourceArtifact

logger = logging.getLogger(__name__)
FRED_BASE_URL = "https://api.stlouisfed.org/fred"


class FredAlfredConnector:
    source_name = "fred_alfred"

    def __init__(self, http_client: HttpClient, api_key: str) -> None:
        if not api_key:
            raise ValueError("FRED API key is required for fred_alfred connector.")
        self.http_client = http_client
        self.api_key = api_key

    def fetch_task(self, task: FetchTask) -> FetchResult:
        spec = task.spec
        if spec.vintage_policy == "true_vintage":
            return self._fetch_true_vintage(task)
        return self._fetch_current(task)

    def _base_params(self, spec_series_id: str) -> dict[str, Any]:
        return {
            "series_id": spec_series_id,
            "api_key": self.api_key,
            "file_type": "json",
        }

    def _fetch_current(self, task: FetchTask) -> FetchResult:
        spec = task.spec
        observations: list[ObservationRecord] = []
        artifacts: list[SourceArtifact] = []
        note_hash = _notes_hash(spec.notes)
        for response, payload in self._paged_observation_payloads(task=task, output_type=1):
            rows_this_page = 0
            for item in payload.get("observations", []):
                value = _parse_float(item.get("value"))
                if value is None:
                    continue
                obs_date = str(item.get("date", "") or "").strip()
                if not obs_date:
                    continue
                observations.append(
                    ObservationRecord(
                        metric_key=spec.metric_key,
                        source_name=spec.source_name,
                        source_dataset=spec.source_dataset,
                        source_series_id=spec.source_series_id,
                        ref_area=spec.ref_area,
                        frequency=spec.frequency,
                        seasonal_adjustment=spec.seasonal_adjustment,
                        units=spec.units,
                        observation_period=obs_date,
                        observation_date=obs_date,
                        release_date=None,
                        vintage_date=None,
                        value=value,
                        source_last_updated=None,
                        retrieved_at=response.fetched_at,
                        revision_flag=0,
                        notes_hash=note_hash,
                    )
                )
                rows_this_page += 1
            artifacts.append(
                SourceArtifact(
                    registry_key=spec.registry_key,
                    source_name=spec.source_name,
                    request_url=response.url,
                    payload_hash=_payload_hash(response.content),
                    http_status=response.status_code,
                    fetched_at=response.fetched_at,
                    row_count=rows_this_page,
                    extra_json={"artifact_kind": "observations_current"},
                )
            )
        if not observations:
            logger.warning("FRED/ALFRED returned no current observations for %s", spec.registry_key)
        return FetchResult(spec=spec, observations=observations, artifacts=artifacts)

    def _fetch_true_vintage(self, task: FetchTask) -> FetchResult:
        spec = task.spec
        observations: list[ObservationRecord] = []
        artifacts: list[SourceArtifact] = []
        note_hash = _notes_hash(spec.notes)
        for response, payload in self._paged_observation_payloads(task=task, output_type=1, compact_revisions=True):
            rows_this_vintage = 0
            for item in payload.get("observations", []):
                value = _parse_float(item.get("value"))
                if value is None:
                    continue
                obs_date = str(item.get("date", "") or "").strip()
                if not obs_date:
                    continue
                release_date = str(item.get("realtime_start", "") or "").strip() or None
                observations.append(
                    ObservationRecord(
                        metric_key=spec.metric_key,
                        source_name=spec.source_name,
                        source_dataset=spec.source_dataset,
                        source_series_id=spec.source_series_id,
                        ref_area=spec.ref_area,
                        frequency=spec.frequency,
                        seasonal_adjustment=spec.seasonal_adjustment,
                        units=spec.units,
                        observation_period=obs_date,
                        observation_date=obs_date,
                        release_date=release_date,
                        vintage_date=release_date,
                        value=value,
                        source_last_updated=None,
                        retrieved_at=response.fetched_at,
                        revision_flag=0,
                        notes_hash=note_hash,
                    )
                )
                rows_this_vintage += 1
            artifacts.append(
                SourceArtifact(
                    registry_key=spec.registry_key,
                    source_name=spec.source_name,
                    request_url=response.url,
                    payload_hash=_payload_hash(response.content),
                    http_status=response.status_code,
                    fetched_at=response.fetched_at,
                    row_count=rows_this_vintage,
                    extra_json={"artifact_kind": "observations_compact_revisions"},
                )
            )
        if not observations:
            logger.warning("FRED/ALFRED returned no vintage observations for %s", spec.registry_key)
        return FetchResult(spec=spec, observations=observations, artifacts=artifacts)

    def _paged_observation_payloads(
        self,
        *,
        task: FetchTask,
        output_type: int,
        compact_revisions: bool = False,
    ) -> list[tuple[Any, dict[str, Any]]]:
        spec = task.spec
        limit = 100000
        offset = 0
        pages: list[tuple[Any, dict[str, Any]]] = []
        while True:
            params: dict[str, Any] = self._base_params(spec.source_series_id or "")
            params["sort_order"] = "asc"
            params["output_type"] = str(output_type)
            params["limit"] = str(limit)
            params["offset"] = str(offset)
            if task.observation_start is not None:
                params["observation_start"] = task.observation_start.isoformat()
            if task.observation_end is not None:
                params["observation_end"] = task.observation_end.isoformat()
            if compact_revisions:
                params["realtime_start"] = (
                    task.vintage_start.isoformat() if task.vintage_start is not None else "1776-07-04"
                )
                params["realtime_end"] = task.as_of_date.isoformat()
            response = self.http_client.get(f"{FRED_BASE_URL}/series/observations", params=params)
            payload = response.json()
            pages.append((response, payload))
            count = int(payload.get("count", 0) or 0)
            current_offset = int(payload.get("offset", offset) or offset)
            current_limit = int(payload.get("limit", limit) or limit)
            next_offset = current_offset + current_limit
            if next_offset >= count:
                break
            offset = next_offset
        return pages


def _parse_float(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text or text == ".":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _payload_hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _notes_hash(text: str | None) -> str | None:
    if not text:
        return None
    return hashlib.sha1(text.encode("utf-8")).hexdigest()

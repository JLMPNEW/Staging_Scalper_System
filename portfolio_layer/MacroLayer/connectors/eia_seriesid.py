#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from macro_http import HttpClient
from macro_types import FetchResult, FetchTask, ObservationRecord, SourceArtifact

logger = logging.getLogger(__name__)
ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class EiaSeriesIdConnector:
    source_name = "eia_seriesid"

    def __init__(self, http_client: HttpClient, api_key: str, base_url: str = "https://api.eia.gov/v2/seriesid") -> None:
        if not api_key:
            raise ValueError("EIA API key is required for eia_seriesid connector.")
        self.http_client = http_client
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def fetch_task(self, task: FetchTask) -> FetchResult:
        spec = task.spec
        observations: list[ObservationRecord] = []
        artifacts: list[SourceArtifact] = []
        note_hash = hashlib.sha1(spec.notes.encode("utf-8")).hexdigest() if spec.notes else None
        for response, rows in self._paged_rows(task):
            page_count = 0
            for item in rows:
                period = str(item.get("period", "") or item.get("date", "") or "").strip()
                if not period:
                    continue
                normalized_date = _normalize_eia_period(period)
                if not ISO_DATE_RE.fullmatch(normalized_date):
                    logger.warning("EIA: unrecognized period format %r for %s; skipping row", period, spec.registry_key)
                    continue
                if task.observation_start is not None and normalized_date < task.observation_start.isoformat():
                    continue
                if task.observation_end is not None and normalized_date > task.observation_end.isoformat():
                    continue
                value = _parse_float(item)
                if value is None:
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
                        observation_period=period,
                        observation_date=normalized_date,
                        release_date=None,
                        vintage_date=None,
                        value=value,
                        source_last_updated=None,
                        retrieved_at=response.fetched_at,
                        revision_flag=0,
                        notes_hash=note_hash,
                    )
                )
                page_count += 1
            artifacts.append(
                SourceArtifact(
                    registry_key=spec.registry_key,
                    source_name=spec.source_name,
                    request_url=response.url,
                    payload_hash=hashlib.sha256(response.content).hexdigest(),
                    http_status=response.status_code,
                    fetched_at=response.fetched_at,
                    row_count=page_count,
                    extra_json={"artifact_kind": "seriesid_page"},
                )
            )
        return FetchResult(spec=spec, observations=observations, artifacts=artifacts)

    def _paged_rows(self, task: FetchTask) -> list[tuple[Any, list[dict[str, Any]]]]:
        spec = task.spec
        length = 5000
        offset = 0
        pages: list[tuple[Any, list[dict[str, Any]]]] = []
        seen_page_signatures: set[str] = set()
        while True:
            params = {
                "api_key": self.api_key,
                "length": str(length),
                "offset": str(offset),
                "sort[0][column]": "period",
                "sort[0][direction]": "asc",
            }
            response = self.http_client.get(f"{self.base_url}/{spec.source_series_id}", params=params)
            payload = response.json()
            rows = _extract_rows(payload.get("response", {}).get("data") or payload.get("series", []))
            page_signature = hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()
            if rows and page_signature in seen_page_signatures:
                raise RuntimeError(
                    f"EIA pagination repeated a page for {spec.registry_key}: offset={offset} rows={len(rows)}"
                )
            seen_page_signatures.add(page_signature)
            pages.append((response, rows))
            total = _extract_total(payload)
            if not rows or len(rows) < length:
                break
            if total is not None and offset + len(rows) >= total:
                break
            next_offset = offset + len(rows)
            if next_offset <= offset:
                raise RuntimeError(
                    f"EIA pagination made no progress for {spec.registry_key}: "
                    f"offset={offset} rows={len(rows)} total={total}"
                )
            offset = next_offset
        return pages


def _extract_rows(series_payload: Any) -> list[dict[str, Any]]:
    if isinstance(series_payload, list):
        if series_payload and isinstance(series_payload[0], dict) and "data" in series_payload[0]:
            nested = series_payload[0].get("data")
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
        return [item for item in series_payload if isinstance(item, dict)]
    return []


def _extract_total(payload: dict[str, Any]) -> int | None:
    response = payload.get("response", {})
    if isinstance(response, dict):
        total = response.get("total")
        if total is not None:
            try:
                return int(str(total))
            except ValueError:
                pass
    return None


def _parse_float(item: dict[str, Any]) -> float | None:
    for key in ("value", "series-value", "price", "close"):
        if key in item:
            raw_value = item.get(key)
            text = "" if raw_value is None else str(raw_value).strip()
            if not text or text.lower() in {"null", "w", "*"}:
                return None
            try:
                return float(text)
            except ValueError:
                return None
    return None


def _normalize_eia_period(period: str) -> str:
    text = str(period).strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    if len(text) == 6 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-01"
    if len(text) == 7 and text[:4].isdigit() and text[4] == "-" and text[5] in "Qq" and text[6].isdigit():
        quarter = int(text[6])
        if 1 <= quarter <= 4:
            return f"{text[:4]}-{(quarter - 1) * 3 + 1:02d}-01"
    if len(text) == 6 and text[:4].isdigit() and text[4] in "Qq" and text[5].isdigit():
        quarter = int(text[5])
        if 1 <= quarter <= 4:
            return f"{text[:4]}-{(quarter - 1) * 3 + 1:02d}-01"
    if len(text) == 4 and text.isdigit():
        return f"{text}-01-01"
    return text

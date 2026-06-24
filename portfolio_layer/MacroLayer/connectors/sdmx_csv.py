#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import io
import logging
from typing import Any

import pandas as pd
from pandas.errors import ParserError

from macro_http import HttpClient
from macro_types import FetchResult, FetchTask, ObservationRecord, SourceArtifact


class SdmxCsvConnector:
    def __init__(self, http_client: HttpClient, *, base_url: str, default_agency: str | None = None) -> None:
        self.http_client = http_client
        self.base_url = base_url.rstrip("/")
        self.default_agency = default_agency
        self.logger = logging.getLogger(f"{type(self).__module__}.{type(self).__name__}")

    def fetch_task(self, task: FetchTask) -> FetchResult:
        spec = task.spec
        params = {
            "dimensionAtObservation": "AllDimensions",
            "format": "csvfilewithlabels",
        }
        if task.observation_start is not None:
            params["startPeriod"] = task.observation_start.isoformat()
        if task.observation_end is not None:
            params["endPeriod"] = task.observation_end.isoformat()
        params.update(
            {
                str(k): str(v)
                for k, v in spec.source_params.items()
                if str(k) not in {"agency_id", "dataset_version"}
            }
        )
        agency_id = str(spec.source_params.get("agency_id") or self.default_agency or "").strip()
        if not agency_id:
            raise ValueError(f"agency_id is required for SDMX connector ({spec.registry_key})")
        dataset_version = str(spec.source_params.get("dataset_version", "") or "").strip()
        dataset_token = f"{agency_id},{spec.source_dataset}"
        if dataset_version:
            dataset_token = f"{dataset_token},{dataset_version}"
        url = f"{self.base_url}/{dataset_token}/{spec.source_series_id}"
        response = self.http_client.get(url, params=params)
        response_text = response.text()
        artifact_extra = {
            "artifact_kind": "sdmx_csv",
            "content_type": response.headers.get("Content-Type"),
        }
        artifact = SourceArtifact(
            registry_key=spec.registry_key,
            source_name=spec.source_name,
            request_url=response.url,
            payload_hash=hashlib.sha256(response.content).hexdigest(),
            http_status=response.status_code,
            fetched_at=response.fetched_at,
            row_count=0,
            extra_json=artifact_extra,
        )
        try:
            frame = _read_sdmx_frame(response_text=response_text, content_type=response.headers.get("Content-Type"))
            observations = _frame_to_observations(spec=spec, frame=frame, retrieved_at=response.fetched_at)
            artifact = SourceArtifact(
                registry_key=artifact.registry_key,
                source_name=artifact.source_name,
                request_url=artifact.request_url,
                payload_hash=artifact.payload_hash,
                http_status=artifact.http_status,
                fetched_at=artifact.fetched_at,
                row_count=len(observations),
                extra_json=artifact.extra_json,
            )
            return FetchResult(spec=spec, observations=observations, artifacts=[artifact])
        except (ParserError, RuntimeError, ValueError) as exc:
            self.logger.warning("SDMX CSV parse failed for %s: %s", spec.registry_key, exc)
            artifact = SourceArtifact(
                registry_key=artifact.registry_key,
                source_name=artifact.source_name,
                request_url=artifact.request_url,
                payload_hash=artifact.payload_hash,
                http_status=artifact.http_status,
                fetched_at=artifact.fetched_at,
                row_count=0,
                error_text=str(exc),
                extra_json={
                    **artifact_extra,
                    "body_preview": response_text[:200],
                },
            )
            return FetchResult(spec=spec, artifacts=[artifact], error_text=str(exc))


def _frame_to_observations(*, spec: Any, frame: pd.DataFrame, retrieved_at: str) -> list[ObservationRecord]:
    time_col = _find_first(frame.columns, {"TIME_PERIOD", "TIME", "time_period", "TIME_PERIOD:Time period"})
    value_col = _find_first(frame.columns, {"OBS_VALUE", "OBS_VALUE:Observation value", "obs_value"})
    if time_col is None or value_col is None:
        raise RuntimeError(f"Unable to locate SDMX time/value columns for {spec.registry_key}")
    out: list[ObservationRecord] = []
    note_hash = hashlib.sha1(spec.notes.encode("utf-8")).hexdigest() if spec.notes else None
    for row in frame.to_dict(orient="records"):
        period = str(row.get(time_col, "") or "").strip()
        if not period:
            continue
        value = pd.to_numeric(row.get(value_col, None), errors="coerce")
        if pd.isna(value):
            continue
        out.append(
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
                observation_date=_normalize_period(period),
                release_date=None,
                vintage_date=None,
                value=float(value),
                source_last_updated=None,
                retrieved_at=retrieved_at,
                revision_flag=0,
                notes_hash=note_hash,
            )
        )
    return out


def _find_first(columns: Any, candidates: set[str]) -> str | None:
    for col in columns:
        if str(col) in candidates:
            return str(col)
    return None


def _read_sdmx_frame(*, response_text: str, content_type: str | None) -> pd.DataFrame:
    content_type_lc = str(content_type or "").lower()
    body = response_text.lstrip()
    if body.startswith("<"):
        raise RuntimeError(f"Expected SDMX CSV payload, got markup content-type={content_type or 'unknown'}")
    if any(token in content_type_lc for token in ("xml", "html", "json")):
        raise RuntimeError(f"Expected SDMX CSV payload, got content-type={content_type or 'unknown'}")
    return pd.read_csv(io.StringIO(response_text))


def _normalize_period(period: str) -> str:
    text = str(period).strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    if len(text) == 6 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-01"
    if len(text) == 7 and text[4:6] == "-Q" and text[6].isdigit():
        quarter = int(text[6])
        if 1 <= quarter <= 4:
            month = (quarter - 1) * 3 + 1
            return f"{text[:4]}-{month:02d}-01"
    if len(text) == 7 and text[4] == "-" and text[5:7].isdigit():
        return f"{text}-01"
    if len(text) == 6 and text[:4].isdigit() and text[4] in {"Q", "q"} and text[5].isdigit():
        quarter = int(text[5])
        if 1 <= quarter <= 4:
            month = (quarter - 1) * 3 + 1
            return f"{text[:4]}-{month:02d}-01"
    if len(text) == 4 and text.isdigit():
        return f"{text}-01-01"
    return text

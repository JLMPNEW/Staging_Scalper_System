#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from pandas.errors import ParserError

from macro_http import HttpClient
from macro_raw_config import utc_now_iso
from macro_types import FetchResult, FetchTask, SourceArtifact

from .sdmx_csv import _frame_to_observations, _read_sdmx_frame

logger = logging.getLogger(__name__)

_DATASET_DIMENSIONS: dict[str, list[str]] = {
    "DSD_STES@DF_CLI": [
        "REF_AREA",
        "FREQ",
        "MEASURE",
        "UNIT_MEASURE",
        "ACTIVITY",
        "ADJUSTMENT",
        "TRANSFORMATION",
        "TIME_HORIZ",
        "METHODOLOGY",
    ],
    "DSD_STES@DF_FINMARK": [
        "REF_AREA",
        "FREQ",
        "MEASURE",
        "UNIT_MEASURE",
        "ACTIVITY",
        "ADJUSTMENT",
        "TRANSFORMATION",
        "TIME_HORIZ",
        "METHODOLOGY",
    ],
    "DSD_PRICES@DF_PRICES_ALL": [
        "REF_AREA",
        "FREQ",
        "METHODOLOGY",
        "MEASURE",
        "UNIT_MEASURE",
        "EXPENDITURE",
        "ADJUSTMENT",
        "TRANSFORMATION",
    ],
    "DSD_PRICES@DF_PRICES_N_TXCP01_NRG": [
        "REF_AREA",
        "FREQ",
        "METHODOLOGY",
        "MEASURE",
        "UNIT_MEASURE",
        "EXPENDITURE",
        "ADJUSTMENT",
        "TRANSFORMATION",
    ],
    "DSD_KEI@DF_KEI": [
        "REF_AREA",
        "FREQ",
        "MEASURE",
        "UNIT_MEASURE",
        "ACTIVITY",
        "ADJUSTMENT",
        "TRANSFORMATION",
    ],
    "DSD_LFS@DF_IALFS_UNE_M": [
        "REF_AREA",
        "MEASURE",
        "UNIT_MEASURE",
        "TRANSFORMATION",
        "ADJUSTMENT",
        "SEX",
        "AGE",
        "ACTIVITY",
        "FREQ",
    ],
    "DSD_NAMAIN1@DF_QNA_EXPENDITURE_GROWTH_OECD": [
        "FREQ",
        "ADJUSTMENT",
        "REF_AREA",
        "SECTOR",
        "COUNTERPART_SECTOR",
        "TRANSACTION",
        "INSTR_ASSET",
        "ACTIVITY",
        "EXPENDITURE",
        "UNIT_MEASURE",
        "PRICE_BASE",
        "TRANSFORMATION",
        "TABLE_IDENTIFIER",
    ],
}


@dataclass(frozen=True)
class _BundlePayload:
    frame: pd.DataFrame
    request_url: str
    fetched_at: str
    payload_hash: str
    http_status: int
    content_type: str | None
    bundle_row_count: int
    bundle_key: str
    cache_hit: bool = False
    cache_path: str | None = None


@dataclass(frozen=True)
class _BundleFailure:
    request_url: str
    fetched_at: str
    error_text: str
    http_status: int | None
    bundle_key: str


class OecdSdmxConnector:
    source_name = "oecd_sdmx"

    def __init__(
        self,
        http_client: HttpClient,
        base_url: str = "https://sdmx.oecd.org/public/rest/data",
        *,
        cache_dir: Path | None = None,
        cache_max_age_hours: float = 24.0,
    ) -> None:
        self.http_client = http_client
        self.base_url = base_url.rstrip("/")
        self.logger = logging.getLogger(f"{type(self).__module__}.{type(self).__name__}")
        self.cache_dir = cache_dir.resolve() if cache_dir is not None else None
        self.cache_max_age_hours = max(0.0, float(cache_max_age_hours))
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._bundle_cache: dict[tuple[str, str, str, str | None, str | None, str], _BundlePayload | _BundleFailure] = {}
        self._bundle_lock = threading.Lock()

    def fetch_task(self, task: FetchTask) -> FetchResult:
        spec = task.spec
        bundle_state = self._get_bundle(task)
        if isinstance(bundle_state, _BundleFailure):
            artifact = SourceArtifact(
                registry_key=spec.registry_key,
                source_name=spec.source_name,
                request_url=bundle_state.request_url,
                payload_hash=None,
                http_status=bundle_state.http_status,
                fetched_at=bundle_state.fetched_at,
                row_count=0,
                error_text=bundle_state.error_text,
                extra_json={
                    "artifact_kind": "oecd_sdmx_bundle_error",
                    "bundle_key": bundle_state.bundle_key,
                    "source_series_id": spec.source_series_id,
                },
            )
            return FetchResult(spec=spec, artifacts=[artifact], error_text=bundle_state.error_text)
        bundle = bundle_state
        artifact_extra = {
            "artifact_kind": "oecd_sdmx_bundle",
            "content_type": bundle.content_type,
            "bundle_key": bundle.bundle_key,
            "bundle_row_count": bundle.bundle_row_count,
            "cache_hit": bundle.cache_hit,
        }
        if bundle.cache_path:
            artifact_extra["cache_path"] = bundle.cache_path
        try:
            matched = _filter_bundle_frame(spec=spec, frame=bundle.frame)
            observations = _frame_to_observations(spec=spec, frame=matched, retrieved_at=bundle.fetched_at)
            if not observations:
                raise RuntimeError(f"OECD bundle returned no rows for {spec.registry_key} ({spec.source_series_id})")
            artifact = SourceArtifact(
                registry_key=spec.registry_key,
                source_name=spec.source_name,
                request_url=bundle.request_url,
                payload_hash=bundle.payload_hash,
                http_status=bundle.http_status,
                fetched_at=bundle.fetched_at,
                row_count=len(observations),
                extra_json={
                    **artifact_extra,
                    "matched_row_count": len(matched),
                },
            )
            return FetchResult(spec=spec, observations=observations, artifacts=[artifact])
        except (RuntimeError, ValueError, ParserError) as exc:
            self.logger.warning("OECD SDMX filter failed for %s: %s", spec.registry_key, exc)
            artifact = SourceArtifact(
                registry_key=spec.registry_key,
                source_name=spec.source_name,
                request_url=bundle.request_url,
                payload_hash=bundle.payload_hash,
                http_status=bundle.http_status,
                fetched_at=bundle.fetched_at,
                row_count=0,
                error_text=str(exc),
                extra_json={
                    **artifact_extra,
                    "source_series_id": spec.source_series_id,
                },
            )
            return FetchResult(spec=spec, artifacts=[artifact], error_text=str(exc))

    def _get_bundle(self, task: FetchTask) -> _BundlePayload | _BundleFailure:
        spec = task.spec
        agency_id = str(spec.source_params.get("agency_id") or "").strip()
        if not agency_id:
            raise ValueError(f"OECD agency_id is required for {spec.registry_key}")
        params = {
            str(k): str(v)
            for k, v in spec.source_params.items()
            if str(k) not in {"agency_id", "dataset_version"}
        }
        params_token = _params_token(params)
        cache_key = (
            agency_id,
            str(spec.source_dataset or "").strip(),
            str(spec.source_params.get("dataset_version", "") or "").strip(),
            task.observation_start.isoformat() if task.observation_start is not None else None,
            task.observation_end.isoformat() if task.observation_end is not None else None,
            params_token,
        )
        with self._bundle_lock:
            cached = self._bundle_cache.get(cache_key)
            if cached is not None:
                return cached
            bundle = self._read_cached_bundle(
                agency_id=agency_id,
                source_dataset=str(spec.source_dataset or "").strip(),
                dataset_version=str(spec.source_params.get("dataset_version", "") or "").strip(),
                observation_start=task.observation_start.isoformat() if task.observation_start is not None else None,
                observation_end=task.observation_end.isoformat() if task.observation_end is not None else None,
                params=params,
            )
            if bundle is None:
                bundle = self._fetch_bundle(
                    agency_id=agency_id,
                    source_dataset=str(spec.source_dataset or "").strip(),
                    dataset_version=str(spec.source_params.get("dataset_version", "") or "").strip(),
                    observation_start=task.observation_start.isoformat() if task.observation_start is not None else None,
                    observation_end=task.observation_end.isoformat() if task.observation_end is not None else None,
                    params=params,
                )
            self._bundle_cache[cache_key] = bundle
            return bundle

    def _fetch_bundle(
        self,
        *,
        agency_id: str,
        source_dataset: str,
        dataset_version: str,
        observation_start: str | None,
        observation_end: str | None,
        params: dict[str, str],
    ) -> _BundlePayload | _BundleFailure:
        dataset_token = f"{agency_id},{source_dataset}"
        if dataset_version:
            dataset_token = f"{dataset_token},{dataset_version}"
        request_params = {
            "dimensionAtObservation": "AllDimensions",
            "format": "csvfile",
            **params,
        }
        if observation_start is not None:
            request_params["startPeriod"] = observation_start
        if observation_end is not None:
            request_params["endPeriod"] = observation_end
        url = f"{self.base_url}/{dataset_token}/all"
        bundle_key = _bundle_key(
            dataset_token=dataset_token,
            observation_start=observation_start,
            observation_end=observation_end,
            params=params,
        )
        try:
            response = self.http_client.get(url, params=request_params)
            response_text = response.text()
            frame = _read_sdmx_frame(response_text=response_text, content_type=response.headers.get("Content-Type"))
            self.logger.info(
                "OECD bundle fetched: dataset=%s start=%s end=%s rows=%d",
                source_dataset,
                observation_start or "<none>",
                observation_end or "<none>",
                len(frame),
            )
            bundle = _BundlePayload(
                frame=frame,
                request_url=response.url,
                fetched_at=response.fetched_at,
                payload_hash=hashlib.sha256(response.content).hexdigest(),
                http_status=response.status_code,
                content_type=response.headers.get("Content-Type"),
                bundle_row_count=len(frame),
                bundle_key=bundle_key,
            )
            self._write_cached_bundle(bundle=bundle, response_text=response_text)
            return bundle
        except (RuntimeError, ParserError, ValueError) as exc:
            failure = _BundleFailure(
                request_url=url,
                fetched_at=utc_now_iso(),
                error_text=str(exc),
                http_status=_infer_http_status(str(exc)),
                bundle_key=bundle_key,
            )
            self.logger.warning(
                "OECD bundle fetch failed: dataset=%s start=%s end=%s error=%s",
                source_dataset,
                observation_start or "<none>",
                observation_end or "<none>",
                exc,
            )
            return failure

    def _read_cached_bundle(
        self,
        *,
        agency_id: str,
        source_dataset: str,
        dataset_version: str,
        observation_start: str | None,
        observation_end: str | None,
        params: dict[str, str],
    ) -> _BundlePayload | None:
        if self.cache_dir is None:
            return None
        dataset_token = f"{agency_id},{source_dataset}"
        if dataset_version:
            dataset_token = f"{dataset_token},{dataset_version}"
        bundle_key = _bundle_key(
            dataset_token=dataset_token,
            observation_start=observation_start,
            observation_end=observation_end,
            params=params,
        )
        csv_path, meta_path = _cache_paths(self.cache_dir, bundle_key)
        if not csv_path.exists() or not meta_path.exists():
            return None
        if not _is_cache_fresh(meta_path=meta_path, max_age_hours=self.cache_max_age_hours):
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            response_text = csv_path.read_text(encoding="utf-8")
            frame = _read_sdmx_frame(response_text=response_text, content_type=meta.get("content_type"))
            bundle = _BundlePayload(
                frame=frame,
                request_url=str(meta.get("request_url") or ""),
                fetched_at=str(meta.get("fetched_at") or ""),
                payload_hash=str(meta.get("payload_hash") or ""),
                http_status=int(meta.get("http_status") or 200),
                content_type=str(meta.get("content_type") or "").strip() or None,
                bundle_row_count=len(frame),
                bundle_key=bundle_key,
                cache_hit=True,
                cache_path=str(csv_path),
            )
            self.logger.info("OECD bundle cache hit: %s", bundle_key)
            return bundle
        except (OSError, ValueError, TypeError, ParserError, RuntimeError) as exc:
            self.logger.warning("OECD bundle cache read failed for %s: %s", bundle_key, exc)
            return None

    def _write_cached_bundle(self, *, bundle: _BundlePayload, response_text: str) -> None:
        if self.cache_dir is None:
            return
        csv_path, meta_path = _cache_paths(self.cache_dir, bundle.bundle_key)
        meta = {
            "bundle_key": bundle.bundle_key,
            "request_url": bundle.request_url,
            "fetched_at": bundle.fetched_at,
            "payload_hash": bundle.payload_hash,
            "http_status": bundle.http_status,
            "content_type": bundle.content_type,
            "bundle_row_count": bundle.bundle_row_count,
            "cache_created_at_utc": utc_now_iso(),
        }
        try:
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            csv_path.write_text(response_text, encoding="utf-8")
            meta_path.write_text(json.dumps(meta, separators=(",", ":"), sort_keys=True), encoding="utf-8")
        except OSError as exc:
            self.logger.warning("OECD bundle cache write failed for %s: %s", bundle.bundle_key, exc)


def _filter_bundle_frame(*, spec: Any, frame: pd.DataFrame) -> pd.DataFrame:
    dataset = str(spec.source_dataset or "").strip()
    dimensions = _DATASET_DIMENSIONS.get(dataset)
    if dimensions is None:
        raise RuntimeError(f"Unsupported OECD dataset dimension map: {dataset}")
    series_id = str(spec.source_series_id or "").strip()
    tokens = series_id.split(".")
    if len(tokens) != len(dimensions):
        raise RuntimeError(
            f"OECD series id for {spec.registry_key} has {len(tokens)} token(s); expected {len(dimensions)} for {dataset}"
        )
    filtered = frame
    for dimension_name, token in zip(dimensions, tokens):
        column_name = _find_dimension_column(filtered.columns, dimension_name)
        if column_name is None:
            raise RuntimeError(f"OECD bundle is missing dimension column {dimension_name} for {spec.registry_key}")
        filtered = filtered.loc[filtered[column_name].astype(str) == token]
        if filtered.empty:
            raise RuntimeError(
                f"OECD bundle returned no rows after filtering {dimension_name}={token} for {spec.registry_key}"
            )
    return filtered.copy()


def _find_dimension_column(columns: Any, target: str) -> str | None:
    for column in columns:
        name = str(column)
        if name == target or name.split(":", 1)[0] == target:
            return name
    return None


def _params_token(params: dict[str, str]) -> str:
    return json.dumps(params, separators=(",", ":"), sort_keys=True)


def _bundle_key(
    *,
    dataset_token: str,
    observation_start: str | None,
    observation_end: str | None,
    params: dict[str, str],
) -> str:
    return "|".join(
        [
            dataset_token,
            observation_start or "",
            observation_end or "",
            _params_token(params),
        ]
    )


def _cache_paths(cache_dir: Path, bundle_key: str) -> tuple[Path, Path]:
    token = hashlib.sha256(bundle_key.encode("utf-8")).hexdigest()
    return cache_dir / f"{token}.csv", cache_dir / f"{token}.meta.json"


def _is_cache_fresh(*, meta_path: Path, max_age_hours: float) -> bool:
    if max_age_hours <= 0:
        return True
    max_age_seconds = max_age_hours * 3600.0
    age_seconds = max(0.0, time.time() - meta_path.stat().st_mtime)
    return age_seconds <= max_age_seconds


def _infer_http_status(error_text: str) -> int | None:
    for code in (429, 408, 425, 500, 502, 503, 504):
        if f"{code}" in error_text:
            return code
    return None

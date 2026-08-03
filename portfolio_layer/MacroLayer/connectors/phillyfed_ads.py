#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import io
import logging
import re
import zipfile
from typing import Any

import pandas as pd

from macro_http import HttpClient
from macro_types import FetchResult, FetchTask, ObservationRecord, SourceArtifact

logger = logging.getLogger(__name__)
ADS_PAGE_URL = "https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/ads"
CURRENT_FILE_RE = re.compile(r'href="([^"]*ADS_Index_Most_Current_Vintage[^"]*\.xlsx[^"]*)"')
ALL_VINTAGES_RE = re.compile(r'href="([^"]*ADS_All_Vintages[^"]*(?:\.zip|\.xlsx)[^"]*)"')
ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
ADS_VINTAGE_RE = re.compile(r"ADS_INDEX_(\d{6})$", re.IGNORECASE)


class PhillyFedAdsConnector:
    source_name = "phillyfed_ads"

    def __init__(self, http_client: HttpClient, page_url: str | None = None) -> None:
        self.http_client = http_client
        self.page_url = page_url or ADS_PAGE_URL

    def fetch_task(self, task: FetchTask) -> FetchResult:
        spec = task.spec
        page_response = self.http_client.get(self.page_url)
        html = page_response.text()
        current_url = _extract_href(html, CURRENT_FILE_RE)
        all_vintages_url = _extract_href(html, ALL_VINTAGES_RE)
        if not current_url:
            logger.warning("PhillyFed ADS: could not find current-vintage URL on %s", page_response.url)
        if not all_vintages_url:
            logger.warning("PhillyFed ADS: could not find all-vintages URL on %s", page_response.url)
        artifacts = [
            SourceArtifact(
                registry_key=spec.registry_key,
                source_name=spec.source_name,
                request_url=page_response.url,
                payload_hash=_payload_hash(page_response.content),
                http_status=page_response.status_code,
                fetched_at=page_response.fetched_at,
                row_count=0,
                extra_json={"artifact_kind": "ads_page"},
            )
        ]
        observations: list[ObservationRecord] = []
        if all_vintages_url:
            vintages_response = self.http_client.get(all_vintages_url)
            wide_frame = _read_ads_wide_frame(vintages_response.content, all_vintages_url)
            long_frame = _wide_ads_to_long(
                wide_frame,
                observation_start=task.observation_start.isoformat() if task.observation_start is not None else None,
                vintage_start=task.vintage_start.isoformat() if task.vintage_start is not None else None,
            )
            observations.extend(_frame_to_observations(spec=spec, frame=long_frame, retrieved_at=vintages_response.fetched_at))
            artifacts.append(
                SourceArtifact(
                    registry_key=spec.registry_key,
                    source_name=spec.source_name,
                    request_url=vintages_response.url,
                    payload_hash=_payload_hash(vintages_response.content),
                    http_status=vintages_response.status_code,
                    fetched_at=vintages_response.fetched_at,
                    row_count=len(long_frame),
                    extra_json={"artifact_kind": "ads_all_vintages"},
                )
            )
        if current_url:
            current_response = self.http_client.get(current_url)
            current_frame = _read_ads_current_frame(current_response.content)
            current_obs = _frame_to_observations(spec=spec, frame=current_frame, retrieved_at=current_response.fetched_at)
            if current_obs:
                observations.extend(current_obs)
            artifacts.append(
                SourceArtifact(
                    registry_key=spec.registry_key,
                    source_name=spec.source_name,
                    request_url=current_response.url,
                    payload_hash=_payload_hash(current_response.content),
                    http_status=current_response.status_code,
                    fetched_at=current_response.fetched_at,
                    row_count=len(current_frame),
                    extra_json={"artifact_kind": "ads_current_vintage"},
                )
            )
        return FetchResult(spec=spec, observations=observations, artifacts=artifacts)


def _extract_href(html: str, pattern: re.Pattern[str]) -> str | None:
    match = pattern.search(html)
    if not match:
        return None
    href = match.group(1)
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return f"https://www.philadelphiafed.org{href}"
    return href


def _read_ads_wide_frame(payload: bytes, request_url: str) -> pd.DataFrame:
    payload_io = io.BytesIO(payload)
    if zipfile.is_zipfile(payload_io):
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = [str(name).replace("\\", "/") for name in archive.namelist()]
            if _looks_like_excel_workbook(names):
                return pd.read_excel(io.BytesIO(payload))
            member = next((name for name in names if name.lower().endswith((".xlsx", ".xls"))), None)
            if member is None:
                raise RuntimeError(
                    f"ADS all-vintages payload from {request_url} was a ZIP archive but did not contain an Excel file."
                )
            with archive.open(member) as fh:
                return pd.read_excel(io.BytesIO(fh.read()))
    return pd.read_excel(io.BytesIO(payload))


def _read_ads_current_frame(payload: bytes) -> pd.DataFrame:
    frame = pd.read_excel(io.BytesIO(payload))
    return _coerce_current_ads_frame(frame)


def _coerce_current_ads_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["observation_date", "value", "release_date", "vintage_date"])
    date_col = _find_ads_date_column(frame)
    value_col = _find_ads_value_column(frame, exclude={date_col})
    out = pd.DataFrame(
        {
            "observation_date": _parse_ads_observation_dates(frame[date_col]),
            "value": pd.to_numeric(frame[value_col], errors="coerce"),
        }
    )
    out = out.dropna(subset=["observation_date", "value"]).copy()
    out["release_date"] = None
    out["vintage_date"] = None
    return _filter_iso_date_rows(out, date_columns=["observation_date"])


def _wide_ads_to_long(
    frame: pd.DataFrame,
    *,
    observation_start: str | None = None,
    vintage_start: str | None = None,
) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["observation_date", "value", "release_date", "vintage_date"])
    date_col = _find_ads_date_column(frame)
    wide = frame.copy()
    wide["observation_date"] = _parse_ads_observation_dates(wide[date_col])
    wide = wide.dropna(subset=["observation_date"]).copy()
    if observation_start:
        wide = wide.loc[wide["observation_date"] >= observation_start].copy()
    rename_map: dict[str, str] = {}
    parsed_header_count = 0
    for col in wide.columns:
        if col in {date_col, "observation_date"}:
            continue
        parsed = _parse_ads_vintage_header(col)
        if parsed is None:
            continue
        parsed_header_count += 1
        if vintage_start and parsed < vintage_start:
            continue
        rename_map[col] = parsed
    if parsed_header_count == 0:
        raise RuntimeError(
            "ADS all-vintages workbook contained no recognized ADS_INDEX_MMDDYY vintage columns."
        )
    wide = wide.rename(columns=rename_map)
    value_cols = [col for col in wide.columns if col not in {date_col, "observation_date"} and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(col))]
    if not value_cols:
        return pd.DataFrame(columns=["observation_date", "value", "release_date", "vintage_date"])
    long = wide[["observation_date"] + value_cols].melt(
        id_vars=["observation_date"],
        value_vars=value_cols,
        var_name="vintage_date",
        value_name="value",
    )
    long["value"] = pd.to_numeric(long["value"], errors="coerce")
    long["release_date"] = long["vintage_date"]
    long = long.dropna(subset=["observation_date", "value"]).copy()
    long = _filter_iso_date_rows(long, date_columns=["observation_date", "release_date", "vintage_date"])
    return long[["observation_date", "value", "release_date", "vintage_date"]]


def _frame_to_observations(*, spec: Any, frame: pd.DataFrame, retrieved_at: str) -> list[ObservationRecord]:
    observations: list[ObservationRecord] = []
    if frame.empty:
        return observations
    note_hash = hashlib.sha1(spec.notes.encode("utf-8")).hexdigest() if spec.notes else None
    for row in frame.itertuples(index=False):
        obs_date = str(getattr(row, "observation_date", "") or "").strip()
        if not obs_date:
            continue
        value = getattr(row, "value", None)
        if value is None or pd.isna(value):
            continue
        release_date = getattr(row, "release_date", None)
        vintage_date = getattr(row, "vintage_date", None)
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
                release_date=str(release_date).strip() if release_date else None,
                vintage_date=str(vintage_date).strip() if vintage_date else None,
                value=float(value),
                source_last_updated=None,
                retrieved_at=retrieved_at,
                revision_flag=0,
                notes_hash=note_hash,
            )
        )
    return observations


def _find_ads_date_column(frame: pd.DataFrame) -> str:
    for col in frame.columns:
        normalized = _normalize_ads_column_name(col)
        if normalized == "date":
            return str(col)
    for col in frame.columns:
        parsed = _parse_ads_observation_dates(frame[col])
        if parsed.notna().sum() >= max(3, int(len(frame) * 0.8)):
            return str(col)
    raise RuntimeError("Unable to locate ADS observation-date column.")


def _find_ads_value_column(frame: pd.DataFrame, *, exclude: set[str]) -> str:
    for col in frame.columns:
        if str(col) in exclude:
            continue
        normalized = _normalize_ads_column_name(col)
        if normalized in {"ads_index", "adsindex"} or normalized.startswith("ads_index_"):
            return str(col)
    best_col = None
    best_count = -1
    for col in frame.columns:
        if str(col) in exclude:
            continue
        numeric = pd.to_numeric(frame[col], errors="coerce")
        count = int(numeric.notna().sum())
        if count > best_count:
            best_count = count
            best_col = str(col)
    if best_col is None:
        raise RuntimeError("Unable to infer ADS value column.")
    return best_col


def _payload_hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _looks_like_excel_workbook(member_names: list[str]) -> bool:
    normalized = {name.lstrip("/") for name in member_names}
    if "[Content_Types].xml" in normalized:
        return True
    return any(name.startswith("xl/") for name in normalized)


def _normalize_ads_column_name(col: Any) -> str:
    text = str(col or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def _parse_ads_observation_dates(series: pd.Series) -> pd.Series:
    text = series.astype(str).str.strip()
    parsed = pd.to_datetime(text, format="%Y:%m:%d", errors="coerce")
    missing = parsed.isna()
    if missing.any():
        parsed.loc[missing] = pd.to_datetime(text.loc[missing], format="%Y-%m-%d", errors="coerce")
    missing = parsed.isna()
    if missing.any():
        parsed.loc[missing] = pd.to_datetime(text.loc[missing], errors="coerce")
    return parsed.dt.strftime("%Y-%m-%d")


def _parse_ads_vintage_header(col: Any) -> str | None:
    match = ADS_VINTAGE_RE.fullmatch(str(col or "").strip())
    if not match:
        return None
    parsed = pd.to_datetime(match.group(1), format="%m%d%y", errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.strftime("%Y-%m-%d")


def _filter_iso_date_rows(frame: pd.DataFrame, *, date_columns: list[str]) -> pd.DataFrame:
    out = frame.copy()
    for col in date_columns:
        if col not in out.columns:
            continue
        out[col] = out[col].where(out[col].astype(str).str.fullmatch(ISO_DATE_RE.pattern))
    before = len(out)
    out = out.dropna(subset=[col for col in date_columns if col in out.columns]).copy()
    dropped = before - len(out)
    if dropped > 0:
        logger.warning("PhillyFed ADS: dropped %d row(s) with non-ISO dates in columns %s", dropped, ",".join(date_columns))
    return out

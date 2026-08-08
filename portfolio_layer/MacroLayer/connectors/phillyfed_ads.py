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
        as_of_iso = task.as_of_date.isoformat() if task.as_of_date is not None else None
        observation_start_iso = task.observation_start.isoformat() if task.observation_start is not None else None
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
        all_vintages_long: pd.DataFrame | None = None
        if all_vintages_url:
            vintages_response = self.http_client.get(all_vintages_url)
            wide_frame = _read_ads_wide_frame(vintages_response.content, all_vintages_url)
            all_vintages_long = _wide_ads_to_long(
                wide_frame,
                observation_start=observation_start_iso,
                vintage_start=task.vintage_start.isoformat() if task.vintage_start is not None else None,
            )
            long_frame = _enforce_ads_pit_window(
                all_vintages_long,
                as_of_date=as_of_iso,
                fetched_date=_fetched_date_utc(vintages_response.fetched_at),
                context="all_vintages",
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
            current_frame = _read_ads_current_frame(
                current_response.content,
                observation_start=observation_start_iso,
                all_vintages_long=all_vintages_long,
            )
            current_frame = _enforce_ads_pit_window(
                current_frame,
                as_of_date=as_of_iso,
                fetched_date=_fetched_date_utc(current_response.fetched_at),
                context="current_vintage",
            )
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


def _empty_ads_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=["observation_date", "value", "release_date", "vintage_date"])


def _read_ads_current_frame(
    payload: bytes,
    *,
    observation_start: str | None = None,
    all_vintages_long: pd.DataFrame | None = None,
) -> pd.DataFrame:
    frame = pd.read_excel(io.BytesIO(payload))
    return _coerce_current_ads_frame(
        frame,
        observation_start=observation_start,
        all_vintages_long=all_vintages_long,
    )


def _coerce_current_ads_frame(
    frame: pd.DataFrame,
    *,
    observation_start: str | None = None,
    all_vintages_long: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Coerce the current-vintage workbook into vintage-stamped long rows.

    The vintage identity always comes from provider data: the value-column
    header when it carries an ADS_INDEX_MMDDYY stamp, otherwise an exact
    value-match against the newest vintages in the all-vintages archive. Rows
    are never emitted with a NULL release/vintage (that produced natural-key
    duplicates for this true-vintage series) and a vintage is never fabricated
    from wall-clock time; if no provider identity can be resolved, the
    current-file rows are skipped and the all-vintages archive - which contains
    every vintage including the current one - remains authoritative.
    """
    if frame.empty:
        return _empty_ads_frame()
    date_col = _find_ads_date_column(frame)
    value_col = _find_ads_value_column(frame, exclude={date_col})
    out = pd.DataFrame(
        {
            "observation_date": _parse_ads_observation_dates(frame[date_col]),
            "value": pd.to_numeric(frame[value_col], errors="coerce"),
        }
    )
    out = out.dropna(subset=["observation_date", "value"]).copy()
    if observation_start:
        out = out.loc[out["observation_date"] >= observation_start].copy()
    vintage_date = _parse_ads_vintage_header(value_col)
    if vintage_date is None:
        vintage_date = _infer_current_vintage(out, all_vintages_long)
    if vintage_date is None:
        logger.warning(
            "PhillyFed ADS: could not resolve a provider vintage identity for the current-vintage "
            "workbook (value column %r); skipping its %d row(s) - the all-vintages archive remains "
            "authoritative for this true-vintage series.",
            value_col,
            len(out),
        )
        return _empty_ads_frame()
    out["release_date"] = vintage_date
    out["vintage_date"] = vintage_date
    return _filter_iso_date_rows(out, date_columns=["observation_date", "release_date", "vintage_date"])


_CURRENT_VINTAGE_MATCH_CANDIDATES = 5
_CURRENT_VINTAGE_MIN_OVERLAP = 100
_CURRENT_VINTAGE_ATOL = 1e-9


def _infer_current_vintage(
    current_frame: pd.DataFrame,
    all_vintages_long: pd.DataFrame | None,
) -> str | None:
    """Identify the current workbook's vintage by exact value match to the archive.

    Both workbooks are published together per release, so the current file must
    reproduce one of the newest archive vintages. Newest candidates are checked
    first; a candidate matches only when every shared observation agrees to
    within float tolerance and the overlap is large enough to be conclusive.
    """
    if all_vintages_long is None or all_vintages_long.empty or current_frame.empty:
        return None
    current_values = (
        current_frame.drop_duplicates(subset=["observation_date"], keep="last")
        .set_index("observation_date")["value"]
    )
    vintages = sorted({str(v) for v in all_vintages_long["vintage_date"].dropna()}, reverse=True)
    for candidate in vintages[:_CURRENT_VINTAGE_MATCH_CANDIDATES]:
        candidate_frame = all_vintages_long.loc[all_vintages_long["vintage_date"] == candidate]
        candidate_values = (
            candidate_frame.drop_duplicates(subset=["observation_date"], keep="last")
            .set_index("observation_date")["value"]
        )
        shared = current_values.index.intersection(candidate_values.index)
        if len(shared) < _CURRENT_VINTAGE_MIN_OVERLAP:
            continue
        diffs = (current_values.loc[shared].astype(float) - candidate_values.loc[shared].astype(float)).abs()
        if bool((diffs <= _CURRENT_VINTAGE_ATOL).all()):
            return candidate
    return None


def _fetched_date_utc(fetched_at: str) -> str | None:
    text = str(fetched_at or "").strip()
    if ISO_DATE_RE.match(text):
        return text[:10]
    return None


def _enforce_ads_pit_window(
    frame: pd.DataFrame,
    *,
    as_of_date: str | None,
    fetched_date: str | None,
    context: str,
) -> pd.DataFrame:
    """Fail on impossible vintage stamps and drop rows after the run's as-of date.

    A provider vintage can never postdate the moment we fetched it, so any
    release/vintage beyond ``fetched_date`` is a header misparse and aborts the
    fetch (fail closed). Vintages published between the run's as-of date and the
    physical fetch time are legitimate provider vintages, but they do not belong
    in an as-of-dated ingest; they are dropped here and picked up unchanged by
    the next run whose as-of date covers them.
    """
    if frame.empty:
        return frame
    out = frame.copy()
    if fetched_date:
        misparse_mask = (out["release_date"] > fetched_date) | (out["vintage_date"] > fetched_date)
        if bool(misparse_mask.any()):
            sample = sorted(
                set(out.loc[misparse_mask, "vintage_date"].astype(str))
                | set(out.loc[misparse_mask, "release_date"].astype(str))
            )[:5]
            raise RuntimeError(
                f"ADS {context}: {int(misparse_mask.sum())} row(s) carry release/vintage dates after the "
                f"fetch date {fetched_date} (sample: {sample}); this indicates a vintage-header misparse."
            )
    if as_of_date:
        keep_mask = (
            (out["release_date"] <= as_of_date)
            & (out["vintage_date"] <= as_of_date)
            & (out["observation_date"] <= as_of_date)
        )
        dropped = int(len(out) - int(keep_mask.sum()))
        if dropped > 0:
            logger.info(
                "PhillyFed ADS %s: dropped %d row(s) with release/vintage/observation after as-of %s "
                "(post-as-of provider vintage; a later as-of run will ingest it).",
                context,
                dropped,
                as_of_date,
            )
        out = out.loc[keep_mask].copy()
    return out


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


def _selftest() -> None:
    import sqlite3
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import macro_storage

    spec = SimpleNamespace(
        registry_key="us_ads_index",
        metric_key="us_ads_index",
        source_name="phillyfed_ads",
        source_dataset="ads",
        source_series_id="ADS_INDEX",
        ref_area="USA",
        frequency="daily",
        seasonal_adjustment="sa",
        units="index level",
        notes="selftest",
    )

    # 1) Wide all-vintages frame: MMDDYY headers become ISO vintage dates with release == vintage.
    wide = pd.DataFrame(
        {
            "Date": ["2026:07:31", "2026:08:01"],
            "ADS_INDEX_073026": [0.10, None],
            "ADS_INDEX_080626": [0.20, 0.25],
            "not_a_vintage": ["x", "y"],
        }
    )
    long = _wide_ads_to_long(wide, observation_start="2008-01-01", vintage_start=None)
    assert set(long["vintage_date"]) == {"2026-07-30", "2026-08-06"}, long
    assert (long["release_date"] == long["vintage_date"]).all()
    assert long["release_date"].notna().all() and long["vintage_date"].notna().all()

    # 2) Current-vintage frame with a dated header: vintage identity comes from
    #    the value-column header; release == vintage, never NULL.
    current = pd.DataFrame({"Date": ["2026:07:31", "2026:08:01"], "ADS_Index_080626": [0.20, 0.25]})
    coerced = _coerce_current_ads_frame(current, observation_start="2008-01-01")
    assert len(coerced) == 2, coerced
    assert (coerced["vintage_date"] == "2026-08-06").all()
    assert (coerced["release_date"] == "2026-08-06").all()

    # 2b) Header-less current workbook (live format uses a bare 'ADS_Index' column):
    #     the vintage identity is inferred by exact value match against the newest
    #     archive vintages; unresolvable rows are skipped, never emitted vintage-less.
    inference_dates = [d.strftime("%Y-%m-%d") for d in pd.date_range("2026-01-01", periods=120, freq="D")]
    inference_values = [round(0.001 * i, 6) for i in range(120)]
    archive = pd.DataFrame(
        {
            "observation_date": inference_dates * 2,
            "value": [v - 0.5 for v in inference_values] + inference_values,
            "release_date": ["2026-07-30"] * 120 + ["2026-08-06"] * 120,
            "vintage_date": ["2026-07-30"] * 120 + ["2026-08-06"] * 120,
        }
    )
    headerless = pd.DataFrame(
        {"Date": [d.replace("-", ":") for d in inference_dates], "ADS_Index": inference_values}
    )
    inferred = _coerce_current_ads_frame(headerless, observation_start="2008-01-01", all_vintages_long=archive)
    assert len(inferred) == 120, len(inferred)
    assert (inferred["vintage_date"] == "2026-08-06").all()
    assert (inferred["release_date"] == "2026-08-06").all()
    perturbed = headerless.copy()
    perturbed["ADS_Index"] = [v + 1.0 for v in inference_values]
    skipped = _coerce_current_ads_frame(perturbed, observation_start="2008-01-01", all_vintages_long=archive)
    assert skipped.empty, skipped
    no_archive = _coerce_current_ads_frame(headerless, observation_start="2008-01-01", all_vintages_long=None)
    assert no_archive.empty, no_archive

    # 3) Future-dating guard: vintages after the run's as-of date are dropped;
    #    vintages after the physical fetch date abort the fetch (header misparse).
    gated = _enforce_ads_pit_window(long, as_of_date="2026-08-05", fetched_date="2026-08-06", context="selftest")
    assert set(gated["vintage_date"]) == {"2026-07-30"}, gated
    assert (gated["vintage_date"] <= "2026-08-05").all() and (gated["release_date"] <= "2026-08-05").all()
    try:
        _enforce_ads_pit_window(long, as_of_date="2026-08-05", fetched_date="2026-08-01", context="selftest")
    except RuntimeError:
        pass
    else:
        raise AssertionError("vintage after the fetch date must raise (misparse guard)")

    # 4) Idempotent same-vintage re-ingest: two fetches of identical vintages carry
    #    stable natural keys, so re-upserting never duplicates rows.
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(macro_storage.DDL)
        obs_first = _frame_to_observations(spec=spec, frame=gated, retrieved_at="2026-08-06T23:00:00Z")
        obs_second = _frame_to_observations(spec=spec, frame=gated, retrieved_at="2026-08-07T00:08:00Z")
        assert obs_first and all(o.release_date and o.vintage_date for o in obs_first)
        macro_storage._upsert_observations(conn, run_id="run_a", registry_key=spec.registry_key, observations=obs_first)
        count_first = int(conn.execute("SELECT COUNT(*) FROM macro_observation_raw").fetchone()[0])
        macro_storage._upsert_observations(conn, run_id="run_b", registry_key=spec.registry_key, observations=obs_second)
        count_second = int(conn.execute("SELECT COUNT(*) FROM macro_observation_raw").fetchone()[0])
        assert count_first == count_second == len(obs_first), (count_first, count_second, len(obs_first))

        # 5) Restatement-as-new-vintage: a re-estimated value for the same observation
        #    under a NEW provider vintage lands as a distinct row; the first print stays.
        restated = gated.copy()
        restated["release_date"] = "2026-08-06"
        restated["vintage_date"] = "2026-08-06"
        restated["value"] = restated["value"] + 0.05
        obs_restated = _frame_to_observations(spec=spec, frame=restated, retrieved_at="2026-08-07T23:00:00Z")
        macro_storage._upsert_observations(conn, run_id="run_c", registry_key=spec.registry_key, observations=obs_restated)
        count_third = int(conn.execute("SELECT COUNT(*) FROM macro_observation_raw").fetchone()[0])
        assert count_third == count_second + len(obs_restated), (count_third, count_second)
        first_print = conn.execute(
            "SELECT value FROM macro_observation_raw WHERE vintage_date='2026-07-30' ORDER BY observation_period LIMIT 1"
        ).fetchone()
        assert first_print is not None and abs(float(first_print[0]) - 0.10) < 1e-12, first_print
        dup_groups = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT observation_period, COALESCE(release_date,''), COALESCE(vintage_date,''), COUNT(*) c
                FROM macro_observation_raw
                GROUP BY 1, 2, 3 HAVING COUNT(*) > 1
            )
            """
        ).fetchone()
        assert int(dup_groups[0]) == 0, dup_groups
    finally:
        conn.close()

    print("phillyfed_ads selftest OK")


if __name__ == "__main__":
    _selftest()

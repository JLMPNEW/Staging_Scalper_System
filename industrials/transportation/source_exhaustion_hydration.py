from __future__ import annotations

import csv
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence
from urllib.parse import quote

from dedicated_parser.contracts import file_sha256
from industrials.core.reports import write_csv_atomic, write_text_atomic


HYDRATION_VERSION = "transportation_dp6e_source_hydration_v2"
RESULT_FIELDS = (
    "hydration_version",
    "request_key",
    "phase",
    "priority",
    "ticker",
    "cik",
    "accession_number",
    "form_type",
    "document_name",
    "url",
    "cache_path",
    "status",
    "http_status",
    "attempt_count",
    "content_sha256",
    "error",
)


@dataclass(frozen=True)
class HydrationRequest:
    request_key: str
    phase: str
    priority: int
    ticker: str
    cik: str
    accession_number: str
    form_type: str
    url: str
    cache_path: Path


class _RequestThrottle:
    def __init__(self, interval_seconds: float) -> None:
        self._interval_seconds = max(0.0, interval_seconds)
        self._lock = threading.Lock()
        self._last_request_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait_seconds = (
                self._last_request_at + self._interval_seconds
            ) - now
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            self._last_request_at = time.monotonic()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {
                str(key): str(value or "").strip()
                for key, value in row.items()
            }
            for row in csv.DictReader(handle)
        ]


def validate_sealed_csv_artifact(
    *,
    source_manifest: Mapping[str, object],
    artifact_name: str,
    path: Path,
    rows: Sequence[Mapping[str, str]],
) -> list[str]:
    errors: list[str] = []
    artifacts = source_manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        return ["source manifest has no artifacts mapping"]
    descriptor = artifacts.get(artifact_name)
    if not isinstance(descriptor, Mapping):
        return [f"source manifest has no {artifact_name} artifact"]
    expected_path = str(descriptor.get("path") or "")
    if not expected_path:
        errors.append(f"{artifact_name} artifact path is empty")
    elif Path(expected_path).resolve() != path.resolve():
        errors.append(
            f"{artifact_name} path does not match the sealed manifest"
        )
    raw_expected_count = descriptor.get("row_count")
    expected_count = (
        int(str(raw_expected_count))
        if raw_expected_count is not None
        else -1
    )
    if expected_count != len(rows):
        errors.append(
            f"{artifact_name} row_count={len(rows)} expected={expected_count}"
        )
    expected_hash = str(descriptor.get("sha256") or "")
    actual_hash = file_sha256(path)
    if not expected_hash or actual_hash != expected_hash:
        errors.append(f"{artifact_name} sha256 does not match the sealed manifest")
    return errors


def _valid_json(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict)


def _valid_document(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size <= 0:
        return False
    if path.suffix.lower() == ".pdf":
        try:
            with path.open("rb") as handle:
                return handle.read(5) == b"%PDF-"
        except OSError:
            return False
    return True


def build_hydration_requests(
    *,
    gap_rows: Sequence[Mapping[str, str]],
    delta_rows: Sequence[Mapping[str, str]],
    submissions_cache_dir: Path,
    archive_cache_dir: Path,
    phase: str,
    priority_max: int = 3,
) -> list[HydrationRequest]:
    if phase not in {"submissions", "indexes", "documents"}:
        raise ValueError(f"Unsupported hydration phase: {phase}")
    requests: list[HydrationRequest] = []
    if phase == "submissions":
        for row in gap_rows:
            action = str(row.get("required_action") or "")
            source_file = str(row.get("source_file") or "")
            if action not in {
                "HYDRATE_SUBMISSIONS_MAIN",
                "HYDRATE_SUBMISSIONS_HISTORY",
            } or not source_file:
                continue
            requests.append(
                HydrationRequest(
                    request_key=(
                        f"submissions|{row.get('ticker', '')}|{source_file}"
                    ),
                    phase=phase,
                    priority=0,
                    ticker=str(row.get("ticker") or ""),
                    cik=str(row.get("cik") or ""),
                    accession_number="",
                    form_type="SUBMISSIONS_METADATA",
                    url=(
                        "https://data.sec.gov/submissions/"
                        f"{source_file}"
                    ),
                    cache_path=submissions_cache_dir / source_file,
                )
            )
    elif phase == "indexes":
        for row in delta_rows:
            if str(row.get("delta_action") or "") != "HYDRATE_INDEX_ONLY":
                continue
            priority = int(row.get("candidate_priority") or 0)
            if priority <= 0 or priority > priority_max:
                continue
            cik = str(row.get("cik") or "")
            accession = str(row.get("accession_number") or "")
            if not cik or not accession:
                continue
            requests.append(
                HydrationRequest(
                    request_key=(
                        f"index|{row.get('ticker', '')}|{accession}"
                    ),
                    phase=phase,
                    priority=priority,
                    ticker=str(row.get("ticker") or ""),
                    cik=cik,
                    accession_number=accession,
                    form_type=str(row.get("form_type") or ""),
                    url=str(row.get("index_url") or ""),
                    cache_path=(
                        archive_cache_dir
                        / f"CIK{cik}"
                        / accession.replace("-", "")
                        / "index.json"
                    ),
                )
            )
    else:
        for row in delta_rows:
            if (
                str(row.get("delta_action") or "")
                != "HYDRATE_SELECTED_DOCUMENTS"
            ):
                continue
            priority = int(row.get("candidate_priority") or 0)
            if priority <= 0 or priority > priority_max:
                continue
            cik = str(row.get("cik") or "")
            accession = str(row.get("accession_number") or "")
            if not cik or not accession:
                continue
            for document_name in str(
                row.get("selected_document_names") or ""
            ).split("|"):
                document_name = document_name.strip()
                if (
                    not document_name
                    or Path(document_name).name != document_name
                ):
                    continue
                cache_path = (
                    archive_cache_dir
                    / f"CIK{cik}"
                    / accession.replace("-", "")
                    / document_name
                )
                if _valid_document(cache_path):
                    continue
                requests.append(
                    HydrationRequest(
                        request_key=(
                            "document|"
                            f"{row.get('ticker', '')}|{accession}|"
                            f"{document_name}"
                        ),
                        phase=phase,
                        priority=priority,
                        ticker=str(row.get("ticker") or ""),
                        cik=cik,
                        accession_number=accession,
                        form_type=str(row.get("form_type") or ""),
                        url=(
                            "https://www.sec.gov/Archives/edgar/data/"
                            f"{int(cik)}/{accession.replace('-', '')}/"
                            f"{quote(document_name)}"
                        ),
                        cache_path=cache_path,
                    )
                )
    unique: dict[str, HydrationRequest] = {}
    for request in requests:
        unique.setdefault(request.request_key, request)
    return sorted(
        unique.values(),
        key=lambda request: (
            request.priority,
            request.ticker,
            request.accession_number,
            request.request_key,
        ),
    )


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(
        f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    temporary.write_text(text, encoding="utf-8")
    try:
        _replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_with_retry(source: Path, target: Path) -> None:
    for attempt in range(8):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.05 * (attempt + 1))


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    temporary.write_bytes(payload)
    try:
        _replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _default_fetch(
    url: str,
    *,
    user_agent: str,
    timeout_sec: float,
) -> tuple[int, bytes]:
    try:
        import requests  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Package 'requests' is required for SEC metadata hydration"
        ) from exc
    response = requests.get(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept-Encoding": "gzip, deflate",
        },
        timeout=timeout_sec,
    )
    return int(response.status_code), bytes(response.content)


def _hydrate_one(
    request: HydrationRequest,
    *,
    execute: bool,
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
    request_spacing_sec: float,
    throttle: _RequestThrottle,
    fetch: Callable[..., tuple[int, object]],
) -> tuple[dict[str, object], int]:
    status = ""
    http_status = 0
    attempts = 0
    error = ""
    network_requests = 0
    valid_cache = (
        _valid_document(request.cache_path)
        if request.phase == "documents"
        else _valid_json(request.cache_path)
    )
    if valid_cache:
        status = "CACHE_HIT_VALID"
    elif not execute:
        status = "PLANNED_NOT_EXECUTED"
    else:
        for attempt in range(1, max(1, max_retries) + 1):
            attempts = attempt
            throttle.wait()
            try:
                http_status, response_payload = fetch(
                    request.url,
                    user_agent=user_agent,
                    timeout_sec=timeout_sec,
                )
                network_requests += 1
            except Exception as exc:  # network boundary
                network_requests += 1
                error = f"{type(exc).__name__}: {exc}"
                if attempt < max(1, max_retries):
                    time.sleep(request_spacing_sec * attempt)
                continue
            if http_status == 200:
                if isinstance(response_payload, bytes):
                    response_bytes = response_payload
                    response_text = response_payload.decode(
                        "utf-8",
                        errors="replace",
                    )
                else:
                    response_text = str(response_payload)
                    response_bytes = response_text.encode("utf-8")
                if request.phase == "documents":
                    if not response_bytes:
                        error = "empty document response"
                    elif (
                        request.cache_path.suffix.lower() == ".pdf"
                        and not response_bytes.startswith(b"%PDF-")
                    ):
                        error = "PDF response does not have a PDF signature"
                    else:
                        _write_bytes_atomic(
                            request.cache_path,
                            response_bytes,
                        )
                        status = "HYDRATED"
                        error = ""
                        break
                else:
                    try:
                        payload = json.loads(response_text)
                    except json.JSONDecodeError as exc:
                        error = f"JSONDecodeError: {exc}"
                    else:
                        if isinstance(payload, dict):
                            _write_json_atomic(
                                request.cache_path,
                                payload,
                            )
                            status = "HYDRATED"
                            error = ""
                            break
                        error = "response JSON is not an object"
            else:
                error = f"HTTP {http_status}"
            if (
                http_status not in {0, 429, 500, 502, 503, 504}
                or attempt >= max(1, max_retries)
            ):
                break
            time.sleep(request_spacing_sec * attempt)
        if not status:
            status = "FAILED"
    valid_after = (
        _valid_document(request.cache_path)
        if request.phase == "documents"
        else _valid_json(request.cache_path)
    )
    content_hash = file_sha256(request.cache_path) if valid_after else ""
    return (
        {
            "hydration_version": HYDRATION_VERSION,
            "request_key": request.request_key,
            "phase": request.phase,
            "priority": request.priority,
            "ticker": request.ticker,
            "cik": request.cik,
            "accession_number": request.accession_number,
            "form_type": request.form_type,
            "document_name": request.cache_path.name,
            "url": request.url,
            "cache_path": str(request.cache_path.resolve()),
            "status": status,
            "http_status": http_status,
            "attempt_count": attempts,
            "content_sha256": content_hash,
            "error": error,
        },
        network_requests,
    )


def hydrate_metadata(
    requests: Sequence[HydrationRequest],
    *,
    execute: bool,
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
    request_spacing_sec: float,
    progress_path: Path,
    source_manifest_path: Path,
    max_requests: int = 0,
    workers: int = 1,
    fetch: Callable[..., tuple[int, object]] = _default_fetch,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    source_manifest_sha256 = file_sha256(source_manifest_path)
    selected = list(requests[:max_requests]) if max_requests > 0 else list(requests)
    results: list[dict[str, object]] = []
    network_requests = 0
    cache_hits = 0
    failures = 0
    started_at = time.time()
    throttle = _RequestThrottle(request_spacing_sec)

    def process(request: HydrationRequest) -> tuple[dict[str, object], int]:
        return _hydrate_one(
            request,
            execute=execute,
            user_agent=user_agent,
            timeout_sec=timeout_sec,
            max_retries=max_retries,
            request_spacing_sec=request_spacing_sec,
            throttle=throttle,
            fetch=fetch,
        )

    completed: Iterable[tuple[HydrationRequest, dict[str, object], int]]
    if max(1, workers) == 1:
        completed = (
            (request, *process(request))
            for request in selected
        )
        executor = None
    else:
        executor = ThreadPoolExecutor(
            max_workers=max(1, workers),
            thread_name_prefix="transportation-sec-metadata",
        )
        future_requests = {
            executor.submit(process, request): request
            for request in selected
        }
        completed = (
            (future_requests[future], *future.result())
            for future in as_completed(future_requests)
        )
    try:
        for index, (request, row, request_count) in enumerate(
            completed,
            start=1,
        ):
            results.append(row)
            network_requests += request_count
            status = str(row["status"])
            cache_hits += int(status == "CACHE_HIT_VALID")
            failures += int(status == "FAILED")
            if index == len(selected) or (
                execute and index % 25 == 0
            ):
                _write_json_atomic(
                    progress_path,
                    {
                        "hydration_version": HYDRATION_VERSION,
                        "source_manifest_path": str(
                            source_manifest_path.resolve()
                        ),
                        "source_manifest_sha256": source_manifest_sha256,
                        "execute": execute,
                        "workers": max(1, workers),
                        "request_count": len(selected),
                        "completed_count": index,
                        "remaining_count": len(selected) - index,
                        "network_requests": network_requests,
                        "cache_hits": cache_hits,
                        "failures": failures,
                        "elapsed_seconds": round(
                            time.time() - started_at,
                            3,
                        ),
                        "last_request_key": request.request_key,
                        "last_status": status,
                    },
                )
    finally:
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=False)
    order = {
        request.request_key: index
        for index, request in enumerate(selected)
    }
    results.sort(key=lambda row: order[str(row["request_key"])])
    source_manifest_sha256_after = file_sha256(source_manifest_path)
    source_manifest_unchanged = (
        source_manifest_sha256_after == source_manifest_sha256
    )
    summary = {
        "acceptance": (
            "FAIL"
            if failures or not source_manifest_unchanged
            else ("PASS" if execute else "DRY_RUN")
        ),
        "hydration_version": HYDRATION_VERSION,
        "source_manifest_path": str(source_manifest_path.resolve()),
        "source_manifest_sha256": source_manifest_sha256,
        "source_manifest_sha256_after": source_manifest_sha256_after,
        "source_manifest_unchanged": source_manifest_unchanged,
        "execute": execute,
        "workers": max(1, workers),
        "planned_request_count": len(selected),
        "completed_count": len(results),
        "network_requests": network_requests,
        "cache_hits": cache_hits,
        "hydrated_count": sum(
            str(row["status"]) == "HYDRATED" for row in results
        ),
        "failed_count": failures,
        "seal_error_count": int(not source_manifest_unchanged),
        "parser_invocations": 0,
        "feature_build_invocations": 0,
        "historical_materialization_invocations": 0,
        "calibration_invocations": 0,
        "database_writes": 0,
        "elapsed_seconds": round(time.time() - started_at, 3),
    }
    return results, summary


def write_hydration_results(
    *,
    result_rows: Iterable[Mapping[str, object]],
    summary: Mapping[str, object],
    result_path: Path,
    manifest_path: Path,
) -> dict[str, object]:
    rows = list(result_rows)
    write_csv_atomic(result_path, RESULT_FIELDS, rows)
    payload = {
        **dict(summary),
        "result_artifact": {
            "path": str(result_path.resolve()),
            "row_count": len(rows),
            "sha256": file_sha256(result_path),
        },
    }
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    return payload

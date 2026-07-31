from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import tempfile
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.parse import urlparse

from dedicated_parser.contracts import file_sha256
from industrials.transportation.non_sec_endpoints import normalized_domain
from industrials.transportation.primary_document_enumeration import (
    canonicalize_url,
)


PRIMARY_DOCUMENT_HYDRATION_VERSION = (
    "transportation_dp6r_primary_document_hydration_v1"
)

REQUEST_RESULT_FIELDS = (
    "hydration_version",
    "hydration_request_id",
    "retrieval_identity_type",
    "retrieval_identity_sha256",
    "retrieval_url",
    "canonical_url",
    "source_content_digest",
    "source_content_digest_algorithm",
    "fanout_document_count",
    "fanout_ticker_count",
    "fanout_tickers",
    "source_domains",
    "document_types",
    "status",
    "content_ready",
    "http_status",
    "final_url",
    "final_domain",
    "content_type",
    "content_bytes",
    "content_sha256",
    "content_cache_path",
    "attempt_count",
    "network_request_count",
    "retryable",
    "error_class",
    "error",
    "retrieved_at",
    "request_manifest_sha256",
    "parser_execution_authorized",
)

DOCUMENT_RESULT_FIELDS = (
    "hydration_version",
    "document_id",
    "ticker",
    "endpoint_id",
    "document_type",
    "published_date_hint",
    "source_domain",
    "canonical_url",
    "retrieval_url",
    "include_in_hydration",
    "hydration_request_id",
    "document_hydration_status",
    "content_ready",
    "content_type",
    "content_bytes",
    "content_sha256",
    "content_cache_path",
    "source_authority_class",
    "review_evidence_label",
    "applicable_parser_metric_count",
    "applicable_parser_metric_ids",
    "applicable_supporting_metric_count",
    "applicable_supporting_metric_ids",
    "parse_all_applicable_metrics",
    "request_status",
    "retryable",
    "error_class",
    "error",
    "parser_execution_authorized",
)

CONTENT_CATALOG_FIELDS = (
    "hydration_version",
    "content_sha256",
    "content_cache_path",
    "content_bytes",
    "content_types",
    "request_count",
    "document_count",
    "ticker_count",
    "tickers",
    "source_domains",
    "document_types",
    "applicable_parser_metric_count",
    "applicable_parser_metric_ids",
    "applicable_supporting_metric_count",
    "applicable_supporting_metric_ids",
    "parse_all_applicable_metrics",
    "parser_execution_authorized",
)

SUCCESS_STATUSES = frozenset(
    {
        "HYDRATED",
        "CACHE_HIT_VALID",
        "CACHE_HIT_REDIRECT_POLICY_APPROVED",
        "DISCOVERY_CACHE_IMPORTED",
    }
)
TERMINAL_EXCLUDED_STATUSES = frozenset(
    {"EXCLUDED_AFTER_DP6R_REDIRECT_REVIEW"}
)
RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
NON_DOCUMENT_CONTENT_PREFIXES = (
    "audio/",
    "font/",
    "image/",
    "video/",
)
NON_DOCUMENT_CONTENT_TYPES = frozenset(
    {
        "text/css",
        "text/javascript",
        "application/javascript",
        "application/x-javascript",
    }
)

_CONTENT_LOCKS_GUARD = threading.Lock()
_CONTENT_LOCKS: dict[str, threading.Lock] = {}


@dataclass(frozen=True)
class FetchPayload:
    http_status: int
    final_url: str
    content_type: str
    payload: bytes
    attempt_count: int
    network_request_count: int
    error_class: str
    error: str


@dataclass(frozen=True)
class HydrationOutcome:
    request_id: str
    status: str
    content_ready: bool
    http_status: int
    final_url: str
    content_type: str
    content_bytes: int
    content_sha256: str
    content_cache_path: str
    attempt_count: int
    network_request_count: int
    retryable: bool
    error_class: str
    error: str
    retrieved_at: str


class DomainThrottle:
    def __init__(self, spacing_seconds: float) -> None:
        self._spacing_seconds = max(0.0, spacing_seconds)
        self._condition = threading.Condition()
        self._last_request: dict[str, float] = {}

    def wait(self, url: str) -> None:
        domain = normalized_domain(url)
        with self._condition:
            while True:
                now = time.monotonic()
                wait_seconds = (
                    self._last_request.get(domain, 0.0)
                    + self._spacing_seconds
                    - now
                )
                if wait_seconds <= 0:
                    self._last_request[domain] = now
                    return
                self._condition.wait(wait_seconds)


class HydrationRunLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._descriptor: int | None = None

    def __enter__(self) -> HydrationRunLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError as exc:
            raise RuntimeError(
                f"Hydration lock already exists: {self.path}"
            ) from exc
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "started_at": _utc_now(),
                "hydration_version": PRIMARY_DOCUMENT_HYDRATION_VERSION,
            },
            sort_keys=True,
        ).encode("utf-8")
        os.write(self._descriptor, payload)
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None
        self.path.unlink(missing_ok=True)


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _replace_with_retry(source: Path, target: Path) -> None:
    for attempt in range(8):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.05 * (attempt + 1))


def write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / (
        f".dp6r-{os.getpid()}-{time.time_ns()}.tmp"
    )
    temporary.write_bytes(payload)
    try:
        _replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json_atomic(path: Path, payload: object) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    write_bytes_atomic(path, text.encode("utf-8"))


def _metadata_path(cache_root: Path, request_id: str) -> Path:
    return cache_root / "request_metadata" / f"{request_id}.json"


def _content_path(cache_root: Path, content_sha256: str) -> Path:
    return (
        cache_root
        / "content_sha256"
        / content_sha256[:2]
        / f"{content_sha256}.bin"
    )


def _content_lock(content_sha256: str) -> threading.Lock:
    with _CONTENT_LOCKS_GUARD:
        return _CONTENT_LOCKS.setdefault(content_sha256, threading.Lock())


def store_content(
    *,
    cache_root: Path,
    payload: bytes,
    expected_sha256: str = "",
) -> tuple[str, Path]:
    content_sha256 = hashlib.sha256(payload).hexdigest()
    if expected_sha256 and content_sha256 != expected_sha256:
        raise ValueError(
            "content SHA-256 does not match the sealed expected hash"
        )
    target = _content_path(cache_root, content_sha256)
    with _content_lock(content_sha256):
        if target.is_file():
            if target.stat().st_size != len(payload):
                raise ValueError(
                    f"content-addressed size mismatch for {content_sha256}"
                )
            if file_sha256(target) != content_sha256:
                raise ValueError(
                    f"content-addressed hash mismatch for {content_sha256}"
                )
            return content_sha256, target
        write_bytes_atomic(target, payload)
        if file_sha256(target) != content_sha256:
            raise ValueError(
                "content-addressed write verification failed for "
                f"{content_sha256}"
            )
    return content_sha256, target


def import_cached_content(
    *,
    source_path: Path,
    expected_sha256: str,
    expected_bytes: int,
    cache_root: Path,
) -> tuple[str, Path]:
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if source_path.stat().st_size != expected_bytes:
        raise ValueError(f"{source_path}: sealed byte count mismatch")
    payload = source_path.read_bytes()
    return store_content(
        cache_root=cache_root,
        payload=payload,
        expected_sha256=expected_sha256,
    )


def _curl_cookie_fetch(
    url: str,
    *,
    user_agent: str,
    timeout_sec: float,
    max_bytes: int,
) -> FetchPayload:
    curl = shutil.which("curl") or shutil.which("curl.exe")
    if not curl:
        return FetchPayload(
            http_status=0,
            final_url=url,
            content_type="",
            payload=b"",
            attempt_count=1,
            network_request_count=0,
            error_class="CURL_NOT_AVAILABLE",
            error="Reviewed cookie-preflight recovery requires curl",
        )
    parsed_url = urlparse(url)
    referer = f"{parsed_url.scheme}://{parsed_url.netloc}/"
    timeout = str(max(1, int(timeout_sec)))
    browser_user_agent = user_agent.replace(
        " TransportationPrimaryDocumentHydrator/1.0",
        "",
    )
    with tempfile.TemporaryDirectory(prefix="transportation-dp6r-") as tmp:
        root = Path(tmp)
        cookie_path = root / "cookies.txt"
        body_path = root / "document.bin"
        common = [
            "-sS",
            "-L",
            "--max-time",
            timeout,
            "-A",
            browser_user_agent,
            "-e",
            referer,
        ]
        head = subprocess.run(
            [curl, *common, "-I", "-c", str(cookie_path), url],
            capture_output=True,
            check=False,
            timeout=timeout_sec + 5.0,
        )
        if head.returncode != 0:
            message = head.stderr.decode("utf-8", errors="replace").strip()
            return FetchPayload(
                http_status=0,
                final_url=url,
                content_type="",
                payload=b"",
                attempt_count=1,
                network_request_count=1,
                error_class=f"CURL_HEAD_EXIT_{head.returncode}",
                error=message or "curl cookie preflight failed",
            )
        write_out = "%{http_code}\n%{url_effective}\n%{content_type}\n"
        fetched = subprocess.run(
            [
                curl,
                *common,
                "--max-filesize",
                str(max_bytes),
                "-b",
                str(cookie_path),
                "-c",
                str(cookie_path),
                "-o",
                str(body_path),
                "-w",
                write_out,
                url,
            ],
            capture_output=True,
            check=False,
            timeout=timeout_sec + 5.0,
        )
        metadata = fetched.stdout.decode(
            "utf-8", errors="replace"
        ).splitlines()
        http_status = (
            int(metadata[0])
            if metadata and metadata[0].isdigit()
            else 0
        )
        final_url = metadata[1] if len(metadata) > 1 else url
        content_type = metadata[2] if len(metadata) > 2 else ""
        if fetched.returncode != 0:
            message = fetched.stderr.decode(
                "utf-8", errors="replace"
            ).strip()
            return FetchPayload(
                http_status=http_status,
                final_url=final_url,
                content_type=content_type,
                payload=b"",
                attempt_count=1,
                network_request_count=2,
                error_class=f"CURL_GET_EXIT_{fetched.returncode}",
                error=message or "curl document recovery failed",
            )
        payload = body_path.read_bytes() if body_path.is_file() else b""
        if len(payload) > max_bytes:
            return FetchPayload(
                http_status=http_status,
                final_url=final_url,
                content_type=content_type,
                payload=b"",
                attempt_count=1,
                network_request_count=2,
                error_class="ValueError",
                error=f"response exceeds max_document_bytes={max_bytes}",
            )
        if http_status != 200:
            payload = b""
        return FetchPayload(
            http_status=http_status,
            final_url=final_url,
            content_type=content_type,
            payload=payload,
            attempt_count=1,
            network_request_count=2,
            error_class=("" if http_status == 200 else f"HTTP_{http_status}"),
            error=("" if http_status == 200 else f"HTTP {http_status}"),
        )


def _default_fetch(
    url: str,
    *,
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
    max_bytes: int,
    throttle: DomainThrottle,
    preflight_head_for_cookie: bool = False,
) -> FetchPayload:
    if preflight_head_for_cookie:
        return _curl_cookie_fetch(
            url,
            user_agent=user_agent,
            timeout_sec=timeout_sec,
            max_bytes=max_bytes,
        )
    try:
        import requests  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Package 'requests' is required for primary-document hydration"
        ) from exc

    last_status = 0
    final_url = url
    content_type = ""
    error_class = ""
    error = ""
    network_requests = 0
    attempts = 0
    for attempt in range(1, max(1, max_retries) + 1):
        attempts = attempt
        throttle.wait(url)
        network_requests += 1
        response = None
        session = None
        try:
            parsed_url = urlparse(url)
            origin_referer = (
                f"{parsed_url.scheme}://{parsed_url.netloc}/"
            )
            headers = {
                "User-Agent": user_agent,
                "Accept": (
                    "application/pdf, application/json, "
                    "application/xml, application/vnd.ms-excel, "
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet, text/csv, text/html, "
                    "text/plain;q=0.9, */*;q=0.5"
                ),
                "Accept-Encoding": "gzip, deflate",
                "Referer": origin_referer,
            }
            requester = requests
            if preflight_head_for_cookie:
                session = requests.Session()
                requester = session
                head_response = session.head(
                    url,
                    headers=headers,
                    timeout=(min(15.0, timeout_sec), timeout_sec),
                    allow_redirects=True,
                )
                network_requests += 1
                head_response.close()
                throttle.wait(url)
            response = requester.get(
                url,
                headers=headers,
                timeout=(min(15.0, timeout_sec), timeout_sec),
                allow_redirects=True,
                stream=True,
            )
            last_status = int(response.status_code)
            final_url = canonicalize_url(str(response.url)) or str(
                response.url
            )
            content_type = str(
                response.headers.get("content-type") or ""
            ).split(";", 1)[0].strip().lower()
            if last_status != 200:
                error_class = f"HTTP_{last_status}"
                error = f"HTTP {last_status}"
                if (
                    last_status in RETRYABLE_HTTP_STATUSES
                    and attempt < max(1, max_retries)
                ):
                    retry_after = str(
                        response.headers.get("retry-after") or ""
                    ).strip()
                    delay = min(
                        float(retry_after)
                        if retry_after.replace(".", "", 1).isdigit()
                        else float(attempt),
                        10.0,
                    )
                    response.close()
                    if session is not None:
                        session.close()
                    time.sleep(max(0.0, delay))
                    continue
                response.close()
                if session is not None:
                    session.close()
                return FetchPayload(
                    http_status=last_status,
                    final_url=final_url,
                    content_type=content_type,
                    payload=b"",
                    attempt_count=attempts,
                    network_request_count=network_requests,
                    error_class=error_class,
                    error=error,
                )
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=262_144):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(
                        f"response exceeds max_document_bytes={max_bytes}"
                    )
                chunks.append(bytes(chunk))
            response.close()
            if session is not None:
                session.close()
            return FetchPayload(
                http_status=last_status,
                final_url=final_url,
                content_type=content_type,
                payload=b"".join(chunks),
                attempt_count=attempts,
                network_request_count=network_requests,
                error_class="",
                error="",
            )
        except Exception as exc:  # network boundary
            if response is not None:
                response.close()
            if session is not None:
                session.close()
            error_class = type(exc).__name__
            error = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, ValueError):
                break
            if attempt < max(1, max_retries):
                time.sleep(min(float(attempt), 5.0))
                continue
    return FetchPayload(
        http_status=last_status,
        final_url=final_url,
        content_type=content_type,
        payload=b"",
        attempt_count=attempts,
        network_request_count=network_requests,
        error_class=error_class or "NETWORK_ERROR",
        error=error or "document request failed",
    )


def validate_document_payload(
    *,
    request: Mapping[str, str],
    fetched: FetchPayload,
) -> tuple[str, bool, bool, str, str]:
    if fetched.http_status != 200:
        retryable = fetched.http_status in RETRYABLE_HTTP_STATUSES or (
            fetched.http_status == 0
        )
        return (
            "FAILED",
            False,
            retryable,
            fetched.error_class or "HTTP_ERROR",
            fetched.error or f"HTTP {fetched.http_status}",
        )
    if fetched.error and not fetched.payload:
        return (
            "FAILED",
            False,
            fetched.error_class not in {"ValueError"},
            fetched.error_class or "RETRIEVAL_ERROR",
            fetched.error,
        )
    if not fetched.payload:
        return (
            "FAILED_CONTENT_VALIDATION",
            False,
            False,
            "EMPTY_RESPONSE",
            "HTTP 200 response contained no document bytes",
        )

    content_type = fetched.content_type
    if (
        content_type in NON_DOCUMENT_CONTENT_TYPES
        or content_type.startswith(NON_DOCUMENT_CONTENT_PREFIXES)
    ):
        return (
            "FAILED_CONTENT_VALIDATION",
            False,
            False,
            "NON_DOCUMENT_CONTENT_TYPE",
            f"Unexpected document content type: {content_type}",
        )

    canonical_url = str(request.get("canonical_url") or "")
    suffix = Path(urlparse(canonical_url).path).suffix.lower()
    if suffix == ".pdf" and not fetched.payload.startswith(b"%PDF-"):
        return (
            "FAILED_CONTENT_VALIDATION",
            False,
            False,
            "EXPECTED_PDF_SIGNATURE_MISSING",
            "PDF URL returned content without a PDF signature",
        )
    if suffix == ".xlsx" and not fetched.payload.startswith(b"PK"):
        return (
            "FAILED_CONTENT_VALIDATION",
            False,
            False,
            "EXPECTED_XLSX_SIGNATURE_MISSING",
            "XLSX URL returned content without a ZIP signature",
        )

    request_domain = normalized_domain(
        str(request.get("retrieval_url") or "")
    )
    final_domain = normalized_domain(fetched.final_url)
    identity_type = str(
        request.get("retrieval_identity_type") or ""
    )
    allowed_domains = {
        request_domain,
        *(
            domain
            for domain in str(
                request.get("source_domains") or ""
            ).split("|")
            if domain
        ),
    }
    if identity_type == "SOURCE_CONTENT_DIGEST":
        allowed_domains.add("web.archive.org")
    approved_redirect_domains = {
        domain
        for domain in str(
            request.get("approved_redirect_domains") or ""
        ).split("|")
        if domain
    }
    excluded_redirect_domains = {
        domain
        for domain in str(
            request.get("excluded_redirect_domains") or ""
        ).split("|")
        if domain
    }
    if final_domain in excluded_redirect_domains:
        return (
            "EXCLUDED_AFTER_DP6R_REDIRECT_REVIEW",
            False,
            False,
            "REVIEWED_NON_FINANCIAL_REDIRECT",
            "Reviewed redirect resolved to a non-financial page",
        )
    allowed_domains.update(approved_redirect_domains)
    if final_domain and final_domain not in allowed_domains:
        return (
            "QUARANTINED_REDIRECT_DOMAIN_REVIEW_REQUIRED",
            False,
            False,
            "UNREVIEWED_REDIRECT_DOMAIN",
            (
                "Final document domain was not in the reviewed request "
                f"contract: {final_domain}"
            ),
        )
    return "HYDRATED", True, False, "", ""


def _outcome_to_metadata(
    *,
    request: Mapping[str, str],
    outcome: HydrationOutcome,
    request_manifest_sha256: str,
) -> dict[str, object]:
    return {
        "hydration_version": PRIMARY_DOCUMENT_HYDRATION_VERSION,
        "hydration_request_id": outcome.request_id,
        "retrieval_identity_sha256": request[
            "retrieval_identity_sha256"
        ],
        "retrieval_url": request["retrieval_url"],
        "request_manifest_sha256": request_manifest_sha256,
        "status": outcome.status,
        "content_ready": outcome.content_ready,
        "http_status": outcome.http_status,
        "final_url": outcome.final_url,
        "content_type": outcome.content_type,
        "content_bytes": outcome.content_bytes,
        "content_sha256": outcome.content_sha256,
        "content_cache_path": outcome.content_cache_path,
        "attempt_count": outcome.attempt_count,
        "retryable": outcome.retryable,
        "error_class": outcome.error_class,
        "error": outcome.error,
        "retrieved_at": outcome.retrieved_at,
    }


def _load_cached_outcome(
    *,
    request: Mapping[str, str],
    cache_root: Path,
    request_manifest_sha256: str,
    retry_failures: bool,
) -> HydrationOutcome | None:
    request_id = request["hydration_request_id"]
    path = _metadata_path(cache_root, request_id)
    if not path.is_file():
        return None
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(metadata, dict):
        return None
    if (
        metadata.get("hydration_request_id") != request_id
        or metadata.get("retrieval_identity_sha256")
        != request["retrieval_identity_sha256"]
        or metadata.get("retrieval_url") != request["retrieval_url"]
        or metadata.get("request_manifest_sha256")
        != request_manifest_sha256
    ):
        return None
    content_ready = bool(metadata.get("content_ready"))
    content_sha256 = str(metadata.get("content_sha256") or "")
    content_path = Path(str(metadata.get("content_cache_path") or ""))
    approved_redirect_domains = {
        domain
        for domain in str(
            request.get("approved_redirect_domains") or ""
        ).split("|")
        if domain
    }
    excluded_redirect_domains = {
        domain
        for domain in str(
            request.get("excluded_redirect_domains") or ""
        ).split("|")
        if domain
    }
    cached_final_domain = normalized_domain(
        str(metadata.get("final_url") or "")
    )
    cached_status = str(metadata.get("status") or "FAILED")
    redirect_policy_approved = (
        cached_status
        == "QUARANTINED_REDIRECT_DOMAIN_REVIEW_REQUIRED"
        and cached_final_domain in approved_redirect_domains
    )
    redirect_policy_excluded = (
        cached_status
        == "QUARANTINED_REDIRECT_DOMAIN_REVIEW_REQUIRED"
        and cached_final_domain in excluded_redirect_domains
    )
    if content_ready or redirect_policy_approved:
        if (
            not content_sha256
            or not content_path.is_file()
            or content_path.stat().st_size
            != int(metadata.get("content_bytes") or -1)
            or file_sha256(content_path) != content_sha256
        ):
            return None
        content_ready = True
        status = (
            "CACHE_HIT_REDIRECT_POLICY_APPROVED"
            if redirect_policy_approved
            else "CACHE_HIT_VALID"
        )
    elif redirect_policy_excluded:
        status = "EXCLUDED_AFTER_DP6R_REDIRECT_REVIEW"
    else:
        if retry_failures:
            return None
        status = cached_status
        if content_sha256:
            if (
                not content_path.is_file()
                or file_sha256(content_path) != content_sha256
            ):
                return None
    return HydrationOutcome(
        request_id=request_id,
        status=status,
        content_ready=content_ready,
        http_status=int(metadata.get("http_status") or 0),
        final_url=str(metadata.get("final_url") or ""),
        content_type=str(metadata.get("content_type") or ""),
        content_bytes=int(metadata.get("content_bytes") or 0),
        content_sha256=content_sha256,
        content_cache_path=(
            str(content_path.resolve()) if content_sha256 else ""
        ),
        attempt_count=int(metadata.get("attempt_count") or 0),
        network_request_count=0,
        retryable=(
            False
            if redirect_policy_approved or redirect_policy_excluded
            else bool(metadata.get("retryable"))
        ),
        error_class=(
            ""
            if redirect_policy_approved
            else (
                "REVIEWED_NON_FINANCIAL_REDIRECT"
                if redirect_policy_excluded
                else str(metadata.get("error_class") or "")
            )
        ),
        error=(
            ""
            if redirect_policy_approved
            else (
                "Reviewed redirect resolved to a non-financial page"
                if redirect_policy_excluded
                else str(metadata.get("error") or "")
            )
        ),
        retrieved_at=str(metadata.get("retrieved_at") or ""),
    )


def hydrate_one_request(
    request: Mapping[str, str],
    *,
    execute: bool,
    cache_root: Path,
    request_manifest_sha256: str,
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
    max_bytes: int,
    throttle: DomainThrottle,
    retry_failures: bool,
    fetch: Callable[..., FetchPayload] = _default_fetch,
) -> HydrationOutcome:
    request_id = str(request["hydration_request_id"])
    cached = _load_cached_outcome(
        request=request,
        cache_root=cache_root,
        request_manifest_sha256=request_manifest_sha256,
        retry_failures=retry_failures,
    )
    if cached is not None:
        return cached
    if not execute:
        return HydrationOutcome(
            request_id=request_id,
            status="PLANNED_NOT_EXECUTED",
            content_ready=False,
            http_status=0,
            final_url="",
            content_type="",
            content_bytes=0,
            content_sha256="",
            content_cache_path="",
            attempt_count=0,
            network_request_count=0,
            retryable=False,
            error_class="",
            error="",
            retrieved_at="",
        )

    url = canonicalize_url(str(request.get("retrieval_url") or ""))
    if not url:
        fetched = FetchPayload(
            http_status=0,
            final_url="",
            content_type="",
            payload=b"",
            attempt_count=0,
            network_request_count=0,
            error_class="INVALID_URL",
            error="Request URL is not a safe HTTP(S) URL",
        )
    else:
        fetched = fetch(
            url,
            user_agent=user_agent,
            timeout_sec=timeout_sec,
            max_retries=max_retries,
            max_bytes=max_bytes,
            throttle=throttle,
            preflight_head_for_cookie=(
                str(request.get("preflight_head_for_cookie") or "")
                == "1"
            ),
        )

    status, ready, retryable, error_class, error = (
        validate_document_payload(
            request=request,
            fetched=fetched,
        )
    )
    content_sha256 = ""
    content_path = Path()
    if fetched.payload:
        content_sha256, content_path = store_content(
            cache_root=cache_root,
            payload=fetched.payload,
        )
    outcome = HydrationOutcome(
        request_id=request_id,
        status=status,
        content_ready=ready,
        http_status=fetched.http_status,
        final_url=fetched.final_url,
        content_type=fetched.content_type,
        content_bytes=len(fetched.payload),
        content_sha256=content_sha256,
        content_cache_path=(
            str(content_path.resolve()) if content_sha256 else ""
        ),
        attempt_count=fetched.attempt_count,
        network_request_count=fetched.network_request_count,
        retryable=retryable,
        error_class=error_class or fetched.error_class,
        error=error or fetched.error,
        retrieved_at=_utc_now(),
    )
    write_json_atomic(
        _metadata_path(cache_root, request_id),
        _outcome_to_metadata(
            request=request,
            outcome=outcome,
            request_manifest_sha256=request_manifest_sha256,
        ),
    )
    return outcome


def _request_result_row(
    *,
    request: Mapping[str, str],
    outcome: HydrationOutcome,
    request_manifest_sha256: str,
) -> dict[str, object]:
    return {
        "hydration_version": PRIMARY_DOCUMENT_HYDRATION_VERSION,
        "hydration_request_id": request["hydration_request_id"],
        "retrieval_identity_type": request[
            "retrieval_identity_type"
        ],
        "retrieval_identity_sha256": request[
            "retrieval_identity_sha256"
        ],
        "retrieval_url": request["retrieval_url"],
        "canonical_url": request["canonical_url"],
        "source_content_digest": request["source_content_digest"],
        "source_content_digest_algorithm": request[
            "source_content_digest_algorithm"
        ],
        "fanout_document_count": request["fanout_document_count"],
        "fanout_ticker_count": request["fanout_ticker_count"],
        "fanout_tickers": request["fanout_tickers"],
        "source_domains": request["source_domains"],
        "document_types": request["document_types"],
        "status": outcome.status,
        "content_ready": int(outcome.content_ready),
        "http_status": outcome.http_status,
        "final_url": outcome.final_url,
        "final_domain": normalized_domain(outcome.final_url),
        "content_type": outcome.content_type,
        "content_bytes": outcome.content_bytes,
        "content_sha256": outcome.content_sha256,
        "content_cache_path": outcome.content_cache_path,
        "attempt_count": outcome.attempt_count,
        "network_request_count": outcome.network_request_count,
        "retryable": int(outcome.retryable),
        "error_class": outcome.error_class,
        "error": outcome.error,
        "retrieved_at": outcome.retrieved_at,
        "request_manifest_sha256": request_manifest_sha256,
        "parser_execution_authorized": 0,
    }


def hydration_request_ids_sha256(
    requests: Sequence[Mapping[str, str]],
) -> str:
    request_ids = [
        str(row.get("hydration_request_id") or "") for row in requests
    ]
    if not all(request_ids) or len(request_ids) != len(set(request_ids)):
        raise ValueError("Hydration request ids are blank or duplicated")
    payload = ("\n".join(request_ids) + "\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_hydration_resume_progress(
    progress: Mapping[str, object],
    *,
    progress_path: Path,
    request_manifest_path: Path,
    request_manifest_sha256: str,
    full_selection: Sequence[Mapping[str, str]],
    expected_batch_size: int,
) -> dict[str, object]:
    errors: list[str] = []

    def integer(name: str, *, default: int | None = None) -> int:
        value = progress.get(name, default)
        try:
            return int(str(value))
        except (TypeError, ValueError):
            errors.append(f"{name} is not an integer")
            return 0

    if (
        str(progress.get("hydration_version") or "")
        != PRIMARY_DOCUMENT_HYDRATION_VERSION
    ):
        errors.append("hydration version does not match")
    if progress.get("execute") is not True:
        errors.append("checkpoint is not from an execute run")
    if integer("parser_invocations") != 0:
        errors.append("checkpoint invoked the parser")
    if str(progress.get("phase") or "") != "COOLDOWN":
        errors.append("checkpoint is not at a completed batch boundary")

    recorded_manifest_path = Path(
        str(progress.get("request_manifest_path") or "")
    )
    try:
        recorded_manifest_path = recorded_manifest_path.resolve()
    except OSError:
        errors.append("request manifest path cannot be resolved")
    if recorded_manifest_path != request_manifest_path.resolve():
        errors.append("request manifest path does not match")
    if (
        str(progress.get("request_manifest_sha256") or "")
        != request_manifest_sha256
    ):
        errors.append("request manifest hash does not match")

    full_selection_count = len(full_selection)
    full_selection_sha256 = hydration_request_ids_sha256(full_selection)
    legacy_checkpoint = "selection_total_count" not in progress
    if legacy_checkpoint:
        selection_start_offset = 0
        selection_total_count = integer("planned_request_count")
    else:
        selection_start_offset = integer("selection_start_offset")
        selection_total_count = integer("selection_total_count")
        if (
            str(progress.get("selection_request_id_sha256") or "")
            != full_selection_sha256
        ):
            errors.append("full recovery selection hash does not match")

    if selection_total_count != full_selection_count:
        errors.append("full recovery selection count does not match")
    if selection_start_offset < 0:
        errors.append("selection start offset is negative")

    planned_request_count = integer("planned_request_count")
    expected_run_count = full_selection_count - selection_start_offset
    if planned_request_count != expected_run_count:
        errors.append("checkpoint run selection count does not match")
    completed_count = integer("completed_count")
    remaining_count = integer("remaining_count")
    if completed_count <= 0:
        errors.append("checkpoint has no completed requests")
    if completed_count >= planned_request_count:
        errors.append("checkpoint is already terminal")
    if remaining_count != planned_request_count - completed_count:
        errors.append("checkpoint remaining count does not reconcile")

    status_counts = progress.get("status_counts")
    if not isinstance(status_counts, Mapping):
        errors.append("checkpoint status counts are missing")
    else:
        status_total = 0
        for value in status_counts.values():
            try:
                status_total += int(str(value))
            except (TypeError, ValueError):
                errors.append("checkpoint status count is not an integer")
                break
        if status_total != completed_count:
            errors.append("checkpoint status counts do not reconcile")

    batch_size = integer("batch_size")
    if batch_size != expected_batch_size or batch_size <= 0:
        errors.append("checkpoint batch size does not match")
    batch_number = integer("batch_number")
    batch_count = integer("batch_count")
    expected_batch_count = (
        (planned_request_count + batch_size - 1) // batch_size
        if batch_size > 0
        else 0
    )
    if batch_count != expected_batch_count:
        errors.append("checkpoint batch count does not reconcile")
    if batch_size > 0 and completed_count % batch_size != 0:
        errors.append("checkpoint is not aligned to a full batch")
    if batch_size > 0 and batch_number != completed_count // batch_size:
        errors.append("checkpoint batch number does not reconcile")

    next_selection_offset = selection_start_offset + completed_count
    if next_selection_offset >= full_selection_count:
        errors.append("checkpoint has no remaining selection")
    if errors:
        raise ValueError(
            f"{progress_path}: invalid hydration resume checkpoint: "
            + "; ".join(errors)
        )
    return {
        "validated": True,
        "legacy_checkpoint": legacy_checkpoint,
        "progress_path": str(progress_path.resolve()),
        "progress_sha256": file_sha256(progress_path),
        "request_manifest_sha256": request_manifest_sha256,
        "selection_request_id_sha256": full_selection_sha256,
        "selection_total_count": full_selection_count,
        "selection_start_offset": selection_start_offset,
        "checkpoint_completed_count": completed_count,
        "next_selection_offset": next_selection_offset,
        "remaining_selection_count": (
            full_selection_count - next_selection_offset
        ),
        "checkpoint_phase": str(progress["phase"]),
        "checkpoint_batch_number": batch_number,
        "checkpoint_batch_count": batch_count,
    }


def hydrate_requests(
    requests: Sequence[Mapping[str, str]],
    *,
    execute: bool,
    cache_root: Path,
    request_manifest_path: Path,
    source_manifest_paths: Sequence[Path],
    progress_path: Path,
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
    request_spacing_sec: float,
    max_bytes: int,
    workers: int,
    retry_failures: bool = False,
    progress_every: int = 10,
    batch_size: int = 0,
    batch_pause_sec: float = 0.0,
    selection_start_offset: int = 0,
    selection_total_count: int | None = None,
    selection_request_id_sha256: str = "",
    fetch: Callable[..., FetchPayload] = _default_fetch,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    request_manifest_sha256 = file_sha256(request_manifest_path)
    source_hashes = {
        str(path.resolve()): file_sha256(path)
        for path in source_manifest_paths
    }
    ids = [str(row.get("hydration_request_id") or "") for row in requests]
    if not all(ids) or len(ids) != len(set(ids)):
        raise ValueError("Hydration request ids are blank or duplicated")
    if any(
        str(row.get("hydration_status") or "")
        != "PLANNED_NOT_AUTHORIZED"
        or str(row.get("parse_all_applicable_metrics") or "") != "1"
        for row in requests
    ):
        raise ValueError("Hydration request contract is not sealed for DP6R")
    if selection_start_offset < 0:
        raise ValueError("Hydration selection start offset is negative")
    resolved_selection_total = (
        len(requests)
        if selection_total_count is None
        else int(selection_total_count)
    )
    if (
        resolved_selection_total < len(requests)
        or selection_start_offset + len(requests)
        != resolved_selection_total
    ):
        raise ValueError(
            "Hydration continuation selection counts do not reconcile"
        )
    run_selection_sha256 = hydration_request_ids_sha256(requests)
    if not selection_request_id_sha256:
        if selection_start_offset != 0:
            raise ValueError(
                "Hydration continuation requires the full selection hash"
            )
        selection_request_id_sha256 = run_selection_sha256

    throttle = DomainThrottle(request_spacing_sec)
    lock_path = cache_root / "transportation_dp6r_hydration.lock"
    started = time.time()
    results: list[dict[str, object]] = []
    status_counts: Counter[str] = Counter()
    network_requests = 0
    order = {request_id: index for index, request_id in enumerate(ids)}

    def process(
        request: Mapping[str, str],
    ) -> tuple[Mapping[str, str], HydrationOutcome]:
        return (
            request,
            hydrate_one_request(
                request,
                execute=execute,
                cache_root=cache_root,
                request_manifest_sha256=request_manifest_sha256,
                user_agent=user_agent,
                timeout_sec=timeout_sec,
                max_retries=max_retries,
                max_bytes=max_bytes,
                throttle=throttle,
                retry_failures=retry_failures,
                fetch=fetch,
            ),
        )

    effective_batch_size = (
        len(requests)
        if batch_size <= 0
        else max(1, min(batch_size, len(requests)))
    )
    request_batches = [
        requests[start : start + effective_batch_size]
        for start in range(0, len(requests), effective_batch_size)
    ]

    def write_progress(*, phase: str, batch_number: int) -> None:
        completed = len(results)
        progress = {
            "hydration_version": PRIMARY_DOCUMENT_HYDRATION_VERSION,
            "execute": execute,
            "request_manifest_path": str(request_manifest_path.resolve()),
            "request_manifest_sha256": request_manifest_sha256,
            "planned_request_count": len(requests),
            "completed_count": completed,
            "remaining_count": len(requests) - completed,
            "status_counts": dict(sorted(status_counts.items())),
            "network_requests": network_requests,
            "elapsed_seconds": round(time.time() - started, 3),
            "updated_at": _utc_now(),
            "parser_invocations": 0,
            "phase": phase,
            "batch_size": effective_batch_size,
            "batch_number": batch_number,
            "batch_count": len(request_batches),
            "batch_pause_sec": max(0.0, batch_pause_sec),
            "selection_start_offset": selection_start_offset,
            "selection_total_count": resolved_selection_total,
            "selection_request_id_sha256": (
                selection_request_id_sha256
            ),
            "run_selection_request_id_sha256": run_selection_sha256,
            "selection_completed_count": (
                selection_start_offset + completed
            ),
            "selection_remaining_count": (
                resolved_selection_total
                - selection_start_offset
                - completed
            ),
        }
        write_json_atomic(progress_path, progress)
        if execute:
            print(
                "[hydrate] "
                f"{completed}/{len(requests)} "
                f"ready={sum(int(str(item['content_ready'])) for item in results)} "
                f"failed={sum(str(item['status']).startswith('FAILED') for item in results)} "
                f"quarantined={sum(str(item['status']).startswith('QUARANTINED') for item in results)} "
                f"phase={phase}",
                flush=True,
            )

    with HydrationRunLock(lock_path):
        for batch_number, batch in enumerate(request_batches, start=1):
            executor = ThreadPoolExecutor(
                max_workers=max(1, workers),
                thread_name_prefix="transportation-primary-hydration",
            )
            futures = {
                executor.submit(process, request): request
                for request in batch
            }
            try:
                for future in as_completed(futures):
                    request, outcome = future.result()
                    row = _request_result_row(
                        request=request,
                        outcome=outcome,
                        request_manifest_sha256=request_manifest_sha256,
                    )
                    results.append(row)
                    status_counts[outcome.status] += 1
                    network_requests += outcome.network_request_count
                    completed = len(results)
                    if completed == len(requests) or completed % max(
                        1, progress_every
                    ) == 0:
                        write_progress(
                            phase="RUNNING",
                            batch_number=batch_number,
                        )
            finally:
                executor.shutdown(wait=True, cancel_futures=False)
            if (
                batch_number < len(request_batches)
                and batch_pause_sec > 0
            ):
                write_progress(
                    phase="COOLDOWN",
                    batch_number=batch_number,
                )
                time.sleep(batch_pause_sec)

    results.sort(
        key=lambda row: order[str(row["hydration_request_id"])]
    )
    source_hashes_after = {
        str(path.resolve()): file_sha256(path)
        for path in source_manifest_paths
    }
    source_unchanged = source_hashes_after == source_hashes
    ready_count = sum(
        int(str(row["content_ready"])) for row in results
    )
    failure_count = sum(
        str(row["status"]).startswith("FAILED") for row in results
    )
    quarantine_count = sum(
        str(row["status"]).startswith("QUARANTINED")
        for row in results
    )
    terminal_excluded_count = sum(
        str(row["status"]) in TERMINAL_EXCLUDED_STATUSES
        for row in results
    )
    summary = {
        "hydration_version": PRIMARY_DOCUMENT_HYDRATION_VERSION,
        "execute": execute,
        "planned_request_count": len(requests),
        "completed_request_count": len(results),
        "content_ready_request_count": ready_count,
        "failed_request_count": failure_count,
        "quarantined_request_count": quarantine_count,
        "terminal_excluded_request_count": terminal_excluded_count,
        "retryable_failure_count": sum(
            int(str(row["retryable"]))
            for row in results
            if not int(str(row["content_ready"]))
        ),
        "status_counts": dict(sorted(status_counts.items())),
        "network_requests": network_requests,
        "cache_hit_count": int(
            status_counts.get("CACHE_HIT_VALID", 0)
        ),
        "hydrated_count": int(status_counts.get("HYDRATED", 0)),
        "unique_ready_content_sha256_count": len(
            {
                str(row["content_sha256"])
                for row in results
                if int(str(row["content_ready"]))
            }
        ),
        "request_manifest_path": str(request_manifest_path.resolve()),
        "request_manifest_sha256": request_manifest_sha256,
        "source_artifact_hashes": source_hashes,
        "source_artifact_hashes_after": source_hashes_after,
        "source_artifacts_unchanged": source_unchanged,
        "workers": max(1, workers),
        "timeout_sec": timeout_sec,
        "max_retries": max_retries,
        "request_spacing_sec": request_spacing_sec,
        "max_document_bytes": max_bytes,
        "retry_failures": retry_failures,
        "batch_size": effective_batch_size,
        "batch_pause_sec": max(0.0, batch_pause_sec),
        "batch_count": len(request_batches),
        "selection_start_offset": selection_start_offset,
        "selection_total_count": resolved_selection_total,
        "selection_request_id_sha256": selection_request_id_sha256,
        "run_selection_request_id_sha256": run_selection_sha256,
        "selection_completed_count": (
            selection_start_offset + len(results)
        ),
        "selection_remaining_count": (
            resolved_selection_total
            - selection_start_offset
            - len(results)
        ),
        "elapsed_seconds": round(time.time() - started, 3),
        "document_retrieval_invocations": network_requests,
        "parser_invocations": 0,
        "feature_build_invocations": 0,
        "historical_materialization_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_invocations": 0,
        "production_promotion_authorized": False,
    }
    return results, summary


def build_document_results(
    *,
    reviewed_documents: Sequence[Mapping[str, str]],
    request_results: Sequence[Mapping[str, object]],
    cache_root: Path,
    require_complete_requests: bool,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[str],
]:
    by_request = {
        str(row["hydration_request_id"]): row
        for row in request_results
    }
    output: list[dict[str, object]] = []
    errors: list[str] = []
    seen_documents: set[str] = set()
    for raw in sorted(
        reviewed_documents,
        key=lambda row: str(row.get("document_id") or ""),
    ):
        document_id = str(raw.get("document_id") or "")
        if not document_id or document_id in seen_documents:
            errors.append(f"invalid or duplicate document id={document_id}")
            continue
        seen_documents.add(document_id)
        include = str(raw.get("include_in_hydration") or "") == "1"
        request_id = str(raw.get("hydration_request_id") or "")
        status = ""
        ready = False
        request_status = ""
        content_type = ""
        content_bytes = 0
        content_sha256 = ""
        content_cache_path = ""
        retryable = False
        error_class = ""
        error = ""
        if not include:
            status = "EXCLUDED_AFTER_DP6Q_REVIEW"
        elif str(raw.get("cache_reuse_available") or "") == "1":
            try:
                content_sha256, path = import_cached_content(
                    source_path=Path(
                        str(raw.get("content_cache_path") or "")
                    ),
                    expected_sha256=str(
                        raw.get("content_sha256") or ""
                    ),
                    expected_bytes=int(
                        str(raw.get("content_bytes") or "0")
                    ),
                    cache_root=cache_root,
                )
            except (OSError, ValueError) as exc:
                status = "FAILED_DISCOVERY_CACHE_IMPORT"
                error_class = type(exc).__name__
                error = str(exc)
                errors.append(
                    f"{document_id}: discovery cache import failed: {exc}"
                )
            else:
                status = "DISCOVERY_CACHE_IMPORTED"
                ready = True
                content_cache_path = str(path.resolve())
                content_type = str(raw.get("content_type") or "")
                content_bytes = int(
                    str(raw.get("content_bytes") or "0")
                )
        elif request_id:
            request_row = by_request.get(request_id)
            if request_row is None:
                status = "REQUEST_NOT_SELECTED_IN_DIAGNOSTIC"
                if require_complete_requests:
                    errors.append(
                        f"{document_id}: missing request result={request_id}"
                    )
            else:
                request_status = str(request_row["status"])
                ready = (
                    int(str(request_row["content_ready"])) == 1
                )
                if ready:
                    status = "HYDRATED_CONTENT_READY"
                elif request_status in TERMINAL_EXCLUDED_STATUSES:
                    status = request_status
                else:
                    status = "PRIMARY_DOCUMENT_SOURCE_GAP"
                content_type = str(request_row["content_type"])
                content_bytes = int(
                    str(request_row["content_bytes"] or "0")
                )
                content_sha256 = str(request_row["content_sha256"])
                content_cache_path = str(
                    request_row["content_cache_path"]
                )
                retryable = (
                    int(str(request_row["retryable"])) == 1
                )
                error_class = str(request_row["error_class"])
                error = str(request_row["error"])
        else:
            status = "FAILED_MISSING_HYDRATION_ROUTE"
            errors.append(f"{document_id}: missing hydration route")

        output.append(
            {
                "hydration_version": (
                    PRIMARY_DOCUMENT_HYDRATION_VERSION
                ),
                "document_id": document_id,
                "ticker": raw.get("ticker", ""),
                "endpoint_id": raw.get("endpoint_id", ""),
                "document_type": raw.get("document_type", ""),
                "published_date_hint": raw.get(
                    "published_date_hint", ""
                ),
                "source_domain": raw.get("source_domain", ""),
                "canonical_url": raw.get("canonical_url", ""),
                "retrieval_url": raw.get("retrieval_url", ""),
                "include_in_hydration": int(include),
                "hydration_request_id": request_id,
                "document_hydration_status": status,
                "content_ready": int(ready),
                "content_type": content_type,
                "content_bytes": content_bytes,
                "content_sha256": content_sha256,
                "content_cache_path": content_cache_path,
                "source_authority_class": raw.get(
                    "source_authority_class", ""
                ),
                "review_evidence_label": raw.get(
                    "review_evidence_label", ""
                ),
                "applicable_parser_metric_count": raw.get(
                    "applicable_parser_metric_count", ""
                ),
                "applicable_parser_metric_ids": raw.get(
                    "applicable_parser_metric_ids", ""
                ),
                "applicable_supporting_metric_count": raw.get(
                    "applicable_supporting_metric_count", ""
                ),
                "applicable_supporting_metric_ids": raw.get(
                    "applicable_supporting_metric_ids", ""
                ),
                "parse_all_applicable_metrics": raw.get(
                    "parse_all_applicable_metrics", ""
                ),
                "request_status": request_status,
                "retryable": int(retryable),
                "error_class": error_class,
                "error": error,
                "parser_execution_authorized": 0,
            }
        )

    catalog_groups: dict[
        str, list[Mapping[str, object]]
    ] = defaultdict(list)
    for row in output:
        if int(str(row["content_ready"])):
            catalog_groups[str(row["content_sha256"])].append(row)
    catalog: list[dict[str, object]] = []
    for content_sha256, rows in sorted(catalog_groups.items()):
        paths = {
            str(row["content_cache_path"])
            for row in rows
            if str(row["content_cache_path"])
        }
        if len(paths) != 1:
            errors.append(
                f"{content_sha256}: content hash maps to multiple cache paths"
            )
        parser_metrics = sorted(
            {
                metric
                for row in rows
                for metric in str(
                    row["applicable_parser_metric_ids"]
                ).split("|")
                if metric
            }
        )
        supporting_metrics = sorted(
            {
                metric
                for row in rows
                for metric in str(
                    row["applicable_supporting_metric_ids"]
                ).split("|")
                if metric
            }
        )
        request_ids = {
            str(row["hydration_request_id"])
            for row in rows
            if str(row["hydration_request_id"])
        }
        catalog.append(
            {
                "hydration_version": (
                    PRIMARY_DOCUMENT_HYDRATION_VERSION
                ),
                "content_sha256": content_sha256,
                "content_cache_path": (
                    sorted(paths)[0] if paths else ""
                ),
                "content_bytes": int(str(rows[0]["content_bytes"])),
                "content_types": "|".join(
                    sorted(
                        {
                            str(row["content_type"])
                            for row in rows
                            if str(row["content_type"])
                        }
                    )
                ),
                "request_count": len(request_ids),
                "document_count": len(rows),
                "ticker_count": len(
                    {str(row["ticker"]) for row in rows}
                ),
                "tickers": "|".join(
                    sorted({str(row["ticker"]) for row in rows})
                ),
                "source_domains": "|".join(
                    sorted(
                        {
                            str(row["source_domain"])
                            for row in rows
                        }
                    )
                ),
                "document_types": "|".join(
                    sorted(
                        {
                            str(row["document_type"])
                            for row in rows
                        }
                    )
                ),
                "applicable_parser_metric_count": len(parser_metrics),
                "applicable_parser_metric_ids": "|".join(
                    parser_metrics
                ),
                "applicable_supporting_metric_count": len(
                    supporting_metrics
                ),
                "applicable_supporting_metric_ids": "|".join(
                    supporting_metrics
                ),
                "parse_all_applicable_metrics": 1,
                "parser_execution_authorized": 0,
            }
        )
    return output, catalog, errors


def summarize_document_results(
    *,
    document_rows: Sequence[Mapping[str, object]],
    content_rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    statuses = Counter(
        str(row["document_hydration_status"]) for row in document_rows
    )
    included = [
        row
        for row in document_rows
        if int(str(row["include_in_hydration"]))
        and str(row["document_hydration_status"])
        not in TERMINAL_EXCLUDED_STATUSES
    ]
    ready = [
        row for row in included if int(str(row["content_ready"]))
    ]
    return {
        "reviewed_document_count": len(document_rows),
        "included_document_count": len(included),
        "excluded_document_count": len(document_rows) - len(included),
        "content_ready_document_count": len(ready),
        "source_gap_document_count": len(included) - len(ready),
        "document_hydration_status_counts": dict(sorted(statuses.items())),
        "unique_content_sha256_count": len(content_rows),
        "content_level_document_deduplication_savings": (
            len(ready) - len(content_rows)
        ),
        "content_ready_ticker_count": len(
            {str(row["ticker"]) for row in ready}
        ),
    }

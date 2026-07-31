#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from industrials.core.config import (  # noqa: E402
    expand_env_vars,
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.reports import (  # noqa: E402
    write_csv_atomic,
    write_text_atomic,
)
from industrials.transportation.non_sec_endpoints import (  # noqa: E402
    normalized_domain,
)
from industrials.transportation.parser_coverage import (  # noqa: E402
    read_csv,
)
from industrials.transportation.primary_document_enumeration import (  # noqa: E402
    DISCOVERY_PAGE_FIELDS,
    ENDPOINT_ENUMERATION_FIELDS,
    EXTERNAL_DOMAIN_FIELDS,
    FUTURE_EXCLUSION_FIELDS,
    PRIMARY_DOCUMENT_ENUMERATION_VERSION,
    PRIMARY_DOCUMENT_FIELDS,
    build_archive_document_candidates,
    build_archive_queries,
    build_archive_year_queries,
    build_endpoint_enumeration_rows,
    build_live_document_candidates,
    build_sitemap_document_candidates,
    canonicalize_url,
    deduplicate_document_rows,
    is_issuer_domain,
    is_navigation_url,
    parse_html_links,
    parse_html_metadata,
    parse_robots_sitemaps,
    parse_sitemap_urls,
    summarize_primary_document_enumeration,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)

ISSUER_DISCOVERY_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36 "
    "TransportationPrimarySourceCensus/1.0"
)


@dataclass(frozen=True)
class CachedResponse:
    request_url: str
    final_url: str
    http_status: int
    content_type: str
    payload: bytes
    content_sha256: str
    cache_path: Path
    network_request_count: int
    error: str


class _DomainThrottle:
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Enumerate, canonicalize, URL-deduplicate, and hash-seal the "
            "complete transportation primary-document retrieval manifest "
            "from the 160 DP6N issuer roots. Discovery responses are cached; "
            "document bodies are not retrieved and parser execution remains "
            "disabled."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform bounded root/index discovery requests.",
    )
    parser.add_argument(
        "--ticker",
        action="append",
        default=[],
        help="Limit to one or more tickers for diagnostics.",
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--active-timeout-sec",
        type=float,
        default=20.0,
    )
    parser.add_argument(
        "--archive-timeout-sec",
        type=float,
        default=150.0,
    )
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument(
        "--request-spacing-sec",
        type=float,
        default=0.35,
    )
    parser.add_argument(
        "--max-discovery-bytes",
        type=int,
        default=35_000_000,
    )
    parser.add_argument(
        "--max-navigation-pages-per-root",
        type=int,
        default=18,
    )
    parser.add_argument(
        "--max-sitemaps-per-root",
        type=int,
        default=16,
    )
    parser.add_argument(
        "--max-navigation-depth",
        type=int,
        default=2,
    )
    parser.add_argument(
        "--max-archive-year-slice-failures-per-lane",
        type=int,
        default=3,
    )
    return parser.parse_args(argv)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def _apply_root_repair_policy(
    *,
    endpoint_rows: Sequence[Mapping[str, str]],
    repair_rows: Sequence[Mapping[str, str]],
) -> list[dict[str, str]]:
    by_ticker: dict[str, Mapping[str, str]] = {}
    for repair in repair_rows:
        ticker = str(repair.get("ticker") or "").strip().upper()
        if not ticker or ticker in by_ticker:
            raise ValueError(
                f"Invalid or duplicate root repair ticker={ticker!r}"
            )
        if repair.get("review_status") != "APPROVED":
            raise ValueError(
                f"{ticker}: root repair is not APPROVED"
            )
        by_ticker[ticker] = repair
    endpoint_tickers = {
        str(row["ticker"]).upper() for row in endpoint_rows
    }
    unknown = set(by_ticker) - endpoint_tickers
    if unknown:
        raise ValueError(
            f"Root repair policy has unknown tickers={sorted(unknown)}"
        )
    output: list[dict[str, str]] = []
    for raw in endpoint_rows:
        endpoint = dict(raw)
        ticker = endpoint["ticker"].upper()
        endpoint["original_discovery_url"] = endpoint[
            "discovery_url"
        ]
        endpoint["root_repair_action"] = ""
        endpoint["root_repair_review_status"] = ""
        endpoint["root_repair_unresolved_disposition"] = ""
        endpoint["root_repair_fallback_source_lane"] = ""
        repair = by_ticker.get(ticker)
        if repair is None:
            output.append(endpoint)
            continue
        if (
            canonicalize_url(repair["original_discovery_url"])
            != canonicalize_url(endpoint["discovery_url"])
        ):
            raise ValueError(
                f"{ticker}: repair original root does not match "
                "the DP6N hash-sealed endpoint"
            )
        action = repair["resolution_action"]
        endpoint["root_repair_action"] = action
        endpoint["root_repair_review_status"] = repair[
            "review_status"
        ]
        endpoint["root_repair_unresolved_disposition"] = repair[
            "unresolved_disposition"
        ]
        endpoint["root_repair_fallback_source_lane"] = repair[
            "fallback_source_lane"
        ]
        if action == "REPLACE_DISCOVERY_ROOT":
            replacement = canonicalize_url(
                repair["replacement_discovery_url"]
            )
            if not replacement:
                raise ValueError(
                    f"{ticker}: invalid replacement discovery root"
                )
            approved_domain = normalized_domain(
                repair["replacement_approved_domain"]
            )
            if approved_domain != normalized_domain(replacement):
                raise ValueError(
                    f"{ticker}: replacement approved domain mismatch"
                )
            endpoint["discovery_url"] = replacement
            endpoint["approved_domain"] = approved_domain
            endpoint["target_domain"] = normalized_domain(
                repair["replacement_target_domain"]
                or approved_domain
            )
        elif action != "RETRY_ARCHIVE_WITH_YEAR_SLICES":
            raise ValueError(
                f"{ticker}: unsupported root repair action={action!r}"
            )
        output.append(endpoint)
    return output


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    temporary.write_bytes(payload)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_atomic(path: Path, payload: object) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    _write_bytes_atomic(path, text.encode("utf-8"))


def _cache_paths(
    *,
    cache_root: Path,
    url: str,
) -> tuple[Path, Path]:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    raw_suffix = Path(urlparse(url).path).suffix.lower()
    suffix = (
        raw_suffix
        if raw_suffix in {".html", ".htm", ".json", ".txt", ".xml"}
        else ".bin"
    )
    return (
        cache_root / "content" / f"{digest}{suffix}",
        cache_root / "metadata" / f"{digest}.json",
    )


def _load_cached_response(
    *,
    request_url: str,
    content_path: Path,
    metadata_path: Path,
) -> CachedResponse | None:
    if not content_path.is_file() or not metadata_path.is_file():
        return None
    try:
        metadata = json.loads(
            metadata_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(metadata, dict):
        return None
    payload = content_path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    http_status = int(metadata.get("http_status") or 0)
    if (
        metadata.get("request_url") != request_url
        or metadata.get("content_sha256") != digest
        or http_status not in {200, 404, 410}
    ):
        return None
    return CachedResponse(
        request_url=request_url,
        final_url=str(metadata.get("final_url") or request_url),
        http_status=http_status,
        content_type=str(metadata.get("content_type") or ""),
        payload=payload,
        content_sha256=digest,
        cache_path=content_path,
        network_request_count=int(
            metadata.get("network_request_count") or 1
        ),
        error="",
    )


def _fetch_cached(
    *,
    url: str,
    cache_root: Path,
    execute: bool,
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
    max_bytes: int,
    throttle: _DomainThrottle,
) -> CachedResponse:
    canonical_url = canonicalize_url(url)
    if not canonical_url:
        return CachedResponse(
            request_url=url,
            final_url="",
            http_status=0,
            content_type="",
            payload=b"",
            content_sha256="",
            cache_path=Path(),
            network_request_count=0,
            error="invalid or unsafe HTTP URL",
        )
    content_path, metadata_path = _cache_paths(
        cache_root=cache_root,
        url=canonical_url,
    )
    cached = _load_cached_response(
        request_url=canonical_url,
        content_path=content_path,
        metadata_path=metadata_path,
    )
    if cached is not None:
        return cached
    if not execute:
        return CachedResponse(
            request_url=canonical_url,
            final_url="",
            http_status=0,
            content_type="",
            payload=b"",
            content_sha256="",
            cache_path=content_path,
            network_request_count=0,
            error="discovery request planned but not executed",
        )
    try:
        import requests  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Package 'requests' is required for primary-source discovery"
        ) from exc
    error = ""
    request_count = 0
    final_url = canonical_url
    http_status = 0
    content_type = ""
    for attempt in range(1, max(1, max_retries) + 1):
        throttle.wait(canonical_url)
        request_count += 1
        try:
            response = requests.get(
                canonical_url,
                headers={
                    "User-Agent": user_agent,
                    "Accept": (
                        "application/json, application/xml, text/xml, "
                        "text/html, text/plain;q=0.9, */*;q=0.5"
                    ),
                },
                timeout=timeout_sec,
                allow_redirects=True,
                stream=True,
            )
            http_status = int(response.status_code)
            final_url = canonicalize_url(str(response.url))
            content_type = str(
                response.headers.get("content-type") or ""
            ).split(";", 1)[0].strip().lower()
            if http_status != 200:
                error = f"HTTP {http_status}"
                if http_status not in {429, 500, 502, 503, 504}:
                    payload = bytes(response.content)
                    digest = hashlib.sha256(payload).hexdigest()
                    _write_bytes_atomic(content_path, payload)
                    _write_json_atomic(
                        metadata_path,
                        {
                            "request_url": canonical_url,
                            "final_url": final_url,
                            "http_status": http_status,
                            "content_type": content_type,
                            "content_bytes": len(payload),
                            "content_sha256": digest,
                            "network_request_count": request_count,
                        },
                    )
                    return CachedResponse(
                        request_url=canonical_url,
                        final_url=final_url,
                        http_status=http_status,
                        content_type=content_type,
                        payload=payload,
                        content_sha256=digest,
                        cache_path=content_path,
                        network_request_count=request_count,
                        error=error,
                    )
                # Retryable status: release the pooled connection and apply
                # the same backoff as the exception path before retrying.
                response.close()
                if attempt < max(1, max_retries):
                    time.sleep(min(2.0 * attempt, 5.0))
                continue
            chunks: list[bytes] = []
            content_length = 0
            for chunk in response.iter_content(chunk_size=131_072):
                if not chunk:
                    continue
                content_length += len(chunk)
                if content_length > max_bytes:
                    raise ValueError(
                        "discovery response exceeds "
                        f"max_discovery_bytes={max_bytes}"
                    )
                chunks.append(bytes(chunk))
            payload = b"".join(chunks)
            digest = hashlib.sha256(payload).hexdigest()
            _write_bytes_atomic(content_path, payload)
            _write_json_atomic(
                metadata_path,
                {
                    "request_url": canonical_url,
                    "final_url": final_url,
                    "http_status": http_status,
                    "content_type": content_type,
                    "content_bytes": len(payload),
                    "content_sha256": digest,
                    "network_request_count": request_count,
                },
            )
            return CachedResponse(
                request_url=canonical_url,
                final_url=final_url,
                http_status=http_status,
                content_type=content_type,
                payload=payload,
                content_sha256=digest,
                cache_path=content_path,
                network_request_count=request_count,
                error="",
            )
        except Exception as exc:  # network boundary
            error = f"{type(exc).__name__}: {exc}"
        if attempt < max(1, max_retries):
            time.sleep(min(2.0 * attempt, 5.0))
    return CachedResponse(
        request_url=canonical_url,
        final_url=final_url,
        http_status=http_status,
        content_type=content_type,
        payload=b"",
        content_sha256="",
        cache_path=content_path,
        network_request_count=request_count,
        error=error or "discovery request failed",
    )


def _page_record(
    *,
    endpoint: Mapping[str, str],
    page_role: str,
    depth: int,
    response: CachedResponse,
    candidate_link_count: int = 0,
    navigation_link_count: int = 0,
    page_title: str = "",
    published_date_hint: str = "",
) -> dict[str, object]:
    if response.http_status == 200 and response.payload:
        page_status = "READY"
    elif response.http_status in {404, 410} and page_role in {
        "ROBOTS",
        "SITEMAP",
    }:
        page_status = "TERMINAL_NOT_FOUND"
    else:
        page_status = "FAILED"
    return {
        "enumeration_version": PRIMARY_DOCUMENT_ENUMERATION_VERSION,
        "endpoint_id": endpoint["endpoint_id"],
        "ticker": endpoint["ticker"],
        "page_role": page_role,
        "depth": depth,
        "request_url": response.request_url,
        "canonical_url": canonicalize_url(response.request_url),
        "approved_domain": endpoint["approved_domain"],
        "target_domain": endpoint["target_domain"],
        "page_status": page_status,
        "http_status": response.http_status,
        "final_url": response.final_url,
        "final_domain": normalized_domain(response.final_url),
        "content_type": response.content_type,
        "page_title": page_title,
        "published_date_hint": published_date_hint,
        "content_bytes": len(response.payload),
        "content_sha256": response.content_sha256,
        "cache_path": (
            str(response.cache_path.resolve())
            if response.cache_path != Path()
            else ""
        ),
        "candidate_link_count": candidate_link_count,
        "navigation_link_count": navigation_link_count,
        "error": response.error,
        "network_request_count": response.network_request_count,
        "payload": response.payload,
    }


def _active_discovery_urls(endpoint: Mapping[str, str]) -> tuple[str, str]:
    root = canonicalize_url(endpoint["discovery_url"])
    parsed = urlparse(root)
    origin = f"{parsed.scheme}://{parsed.netloc}/"
    return (
        canonicalize_url("robots.txt", base_url=origin),
        canonicalize_url("sitemap.xml", base_url=origin),
    )


def _fetch_page(
    *,
    endpoint: Mapping[str, str],
    page_role: str,
    depth: int,
    url: str,
    cache_root: Path,
    args: argparse.Namespace,
    user_agent: str,
    throttle: _DomainThrottle,
) -> dict[str, object]:
    is_archive = page_role in {
        "ARCHIVE_CDX_HTML",
        "ARCHIVE_CDX_PDF",
        "ARCHIVE_CDX_DATA",
    }
    response = _fetch_cached(
        url=url,
        cache_root=cache_root,
        execute=bool(args.execute),
        user_agent=user_agent,
        timeout_sec=float(
            args.archive_timeout_sec
            if is_archive
            else args.active_timeout_sec
        ),
        max_retries=int(args.max_retries),
        max_bytes=int(args.max_discovery_bytes),
        throttle=throttle,
    )
    if (
        response.final_url
        and page_role
        not in {
            "ARCHIVE_CDX_HTML",
            "ARCHIVE_CDX_PDF",
            "ARCHIVE_CDX_DATA",
        }
        and not is_issuer_domain(
            normalized_domain(response.final_url),
            approved_domain=endpoint["approved_domain"],
            target_domain=endpoint["target_domain"],
        )
    ):
        response = CachedResponse(
            request_url=response.request_url,
            final_url=response.final_url,
            http_status=response.http_status,
            content_type=response.content_type,
            payload=b"",
            content_sha256="",
            cache_path=response.cache_path,
            network_request_count=response.network_request_count,
            error=(
                "discovery redirect escaped sealed issuer domain: "
                f"{response.final_url}"
            ),
        )
    links: list[tuple[str, str]] = []
    page_title = ""
    published_date_hint = ""
    if response.payload and (
        "html" in response.content_type
        or response.final_url.lower().endswith(
            (".htm", ".html", "/")
        )
    ):
        page_title, published_date_hint = parse_html_metadata(
            response.payload
        )
        links = parse_html_links(
            response.payload,
            base_url=response.final_url or response.request_url,
        )
    navigation = [
        url
        for url, title in links
        if is_navigation_url(url, title)
    ]
    record = _page_record(
        endpoint=endpoint,
        page_role=page_role,
        depth=depth,
        response=response,
        candidate_link_count=len(links),
        navigation_link_count=len(navigation),
        page_title=page_title,
        published_date_hint=published_date_hint,
    )
    print(
        f"[page] {endpoint['ticker']} {page_role} "
        f"{record['page_status']} {response.request_url}",
        flush=True,
    )
    return record


def _enumerate_active_endpoint(
    *,
    endpoint: Mapping[str, str],
    scope: Mapping[str, str],
    search_terms: Sequence[str],
    cache_root: Path,
    args: argparse.Namespace,
    user_agent: str,
    throttle: _DomainThrottle,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    pages: list[dict[str, object]] = []
    root = _fetch_page(
        endpoint=endpoint,
        page_role="ROOT",
        depth=0,
        url=endpoint["discovery_url"],
        cache_root=cache_root,
        args=args,
        user_agent=user_agent,
        throttle=throttle,
    )
    pages.append(root)
    if (
        root["page_status"] == "FAILED"
        and str(
            endpoint.get("root_repair_unresolved_disposition")
            or ""
        ).startswith("REVIEWED_")
    ):
        return pages, [], []
    robots_url, sitemap_url = _active_discovery_urls(endpoint)
    robots = _fetch_page(
        endpoint=endpoint,
        page_role="ROBOTS",
        depth=0,
        url=robots_url,
        cache_root=cache_root,
        args=args,
        user_agent=user_agent,
        throttle=throttle,
    )
    pages.append(robots)
    sitemap_queue = [sitemap_url]
    robots_payload = robots.get("payload")
    if isinstance(robots_payload, bytes):
        sitemap_queue.extend(
            parse_robots_sitemaps(
                robots_payload,
                base_url=robots_url,
            )
        )
    seen_sitemaps: set[str] = set()
    while sitemap_queue and len(seen_sitemaps) < int(
        args.max_sitemaps_per_root
    ):
        url = canonicalize_url(sitemap_queue.pop(0))
        if (
            not url
            or url in seen_sitemaps
            or not is_issuer_domain(
                normalized_domain(url),
                approved_domain=endpoint["approved_domain"],
                target_domain=endpoint["target_domain"],
            )
        ):
            continue
        seen_sitemaps.add(url)
        page = _fetch_page(
            endpoint=endpoint,
            page_role="SITEMAP",
            depth=0,
            url=url,
            cache_root=cache_root,
            args=args,
            user_agent=user_agent,
            throttle=throttle,
        )
        pages.append(page)
        payload = page.get("payload")
        if isinstance(payload, bytes):
            for child in parse_sitemap_urls(payload):
                child_path = urlparse(child).path.lower()
                if child_path.endswith((".xml", ".xml.gz")):
                    sitemap_queue.append(child)
    navigation_queue: list[tuple[str, int]] = []
    for page in pages:
        payload = page.get("payload")
        if not isinstance(payload, bytes):
            continue
        page_url = str(
            page.get("final_url") or page.get("request_url") or ""
        )
        for url, title in parse_html_links(
            payload,
            base_url=page_url,
        ):
            if (
                is_navigation_url(url, title)
                and is_issuer_domain(
                    normalized_domain(url),
                    approved_domain=endpoint["approved_domain"],
                    target_domain=endpoint["target_domain"],
                )
            ):
                navigation_queue.append((url, 1))
        if str(page.get("page_role") or "") == "SITEMAP":
            for url in parse_sitemap_urls(payload):
                if is_navigation_url(url) and is_issuer_domain(
                    normalized_domain(url),
                    approved_domain=endpoint["approved_domain"],
                    target_domain=endpoint["target_domain"],
                ):
                    navigation_queue.append((url, 1))
    seen_navigation: set[str] = {
        canonicalize_url(endpoint["discovery_url"])
    }
    navigation_count = 0
    while (
        navigation_queue
        and navigation_count < int(args.max_navigation_pages_per_root)
    ):
        url, depth = navigation_queue.pop(0)
        url = canonicalize_url(url)
        if (
            not url
            or url in seen_navigation
            or depth > int(args.max_navigation_depth)
        ):
            continue
        seen_navigation.add(url)
        navigation_count += 1
        page = _fetch_page(
            endpoint=endpoint,
            page_role="NAVIGATION",
            depth=depth,
            url=url,
            cache_root=cache_root,
            args=args,
            user_agent=user_agent,
            throttle=throttle,
        )
        pages.append(page)
        payload = page.get("payload")
        if not isinstance(payload, bytes):
            continue
        page_url = str(page.get("final_url") or page["request_url"])
        for child, title in parse_html_links(
            payload,
            base_url=page_url,
        ):
            if (
                is_navigation_url(child, title)
                and is_issuer_domain(
                    normalized_domain(child),
                    approved_domain=endpoint["approved_domain"],
                    target_domain=endpoint["target_domain"],
                )
            ):
                navigation_queue.append((child, depth + 1))
    documents, external = build_live_document_candidates(
        endpoint=endpoint,
        scope=scope,
        pages=pages,
        metric_search_terms=search_terms,
    )
    documents.extend(
        build_sitemap_document_candidates(
            endpoint=endpoint,
            scope=scope,
            pages=pages,
            metric_search_terms=search_terms,
        )
    )
    return pages, documents, external


def _enumerate_archive_endpoint(
    *,
    endpoint: Mapping[str, str],
    scope: Mapping[str, str],
    search_terms: Sequence[str],
    cache_root: Path,
    args: argparse.Namespace,
    user_agent: str,
    throttle: _DomainThrottle,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    html_url, pdf_url, data_url = build_archive_queries(
        endpoint["discovery_url"]
    )
    pages = [
        _fetch_page(
            endpoint=endpoint,
            page_role="ARCHIVE_CDX_HTML",
            depth=0,
            url=html_url,
            cache_root=cache_root,
            args=args,
            user_agent=user_agent,
            throttle=throttle,
        ),
        _fetch_page(
            endpoint=endpoint,
            page_role="ARCHIVE_CDX_PDF",
            depth=0,
            url=pdf_url,
            cache_root=cache_root,
            args=args,
            user_agent=user_agent,
            throttle=throttle,
        ),
        _fetch_page(
            endpoint=endpoint,
            page_role="ARCHIVE_CDX_DATA",
            depth=0,
            url=data_url,
            cache_root=cache_root,
            args=args,
            user_agent=user_agent,
            throttle=throttle,
        ),
    ]
    failed_roles = {
        str(page["page_role"])
        for page in pages
        if page["page_status"] == "FAILED"
    }
    if failed_roles:
        failed_year_slices: dict[str, int] = {
            role: 0 for role in failed_roles
        }
        for role, year, url in build_archive_year_queries(
            endpoint["discovery_url"]
        ):
            if (
                role not in failed_roles
                or failed_year_slices[role]
                >= int(
                    args.max_archive_year_slice_failures_per_lane
                )
            ):
                continue
            page = _fetch_page(
                endpoint=endpoint,
                page_role=f"{role}_YEAR_{year}",
                depth=0,
                url=url,
                cache_root=cache_root,
                args=args,
                user_agent=user_agent,
                throttle=throttle,
            )
            pages.append(page)
            if page["page_status"] == "FAILED":
                failed_year_slices[role] += 1
    documents = build_archive_document_candidates(
        endpoint=endpoint,
        scope=scope,
        cdx_pages=pages,
        metric_search_terms=search_terms,
    )
    return pages, documents, []


def _strip_runtime_fields(
    rows: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    return [
        {
            key: value
            for key, value in row.items()
            if key not in {"payload", "network_request_count"}
        }
        for row in rows
    ]


def _apply_prefetched_content_hashes(
    *,
    rows: Sequence[Mapping[str, object]],
    cache_root: Path,
) -> tuple[list[dict[str, object]], int]:
    output: list[dict[str, object]] = []
    prefetched = 0
    for raw in rows:
        row = dict(raw)
        canonical_url = str(row.get("canonical_url") or "")
        if canonical_url:
            content_path, metadata_path = _cache_paths(
                cache_root=cache_root,
                url=canonical_url,
            )
            cached = _load_cached_response(
                request_url=canonical_url,
                content_path=content_path,
                metadata_path=metadata_path,
            )
        else:
            cached = None
        if cached is not None and cached.http_status == 200:
            row["content_type"] = cached.content_type
            row["content_bytes"] = len(cached.payload)
            row["content_cache_path"] = str(
                cached.cache_path.resolve()
            )
            row["content_sha256"] = cached.content_sha256
            row["content_hash_status"] = (
                "HASHED_FROM_DISCOVERY_CACHE"
            )
            prefetched += 1
        output.append(row)
    return output, prefetched


def _validate_sealed_artifact(
    *,
    manifest: Mapping[str, Any],
    artifact_name: str,
    path: Path,
) -> None:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise ValueError("Preflight manifest has no artifacts")
    artifact = artifacts.get(artifact_name)
    if not isinstance(artifact, Mapping):
        raise ValueError(
            f"Preflight manifest has no {artifact_name}"
        )
    if (
        int(artifact.get("row_count") or -1) != len(read_csv(path))
        or str(artifact.get("sha256") or "") != file_sha256(path)
    ):
        raise ValueError(
            f"{artifact_name} is not hash-sealed by DP6N"
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    parser_cfg = family["dedicated_parser"]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "Primary document enumeration requires parser execution disabled"
        )
    base_dir = config_path.parent
    asof_date = str(parser_cfg["source_census_asof_date"])
    output_dir = (
        resolve_path(parser_cfg["output_root"], base_dir=base_dir)
        / asof_date
    )
    endpoint_path = (
        output_dir / "transportation_non_sec_endpoint_roots.csv"
    )
    requirement_path = (
        output_dir
        / "transportation_one_pass_source_requirement_map.csv"
    )
    ticker_scope_path = (
        output_dir
        / "transportation_one_pass_ticker_parser_scope.csv"
    )
    preflight_manifest_path = (
        output_dir
        / "transportation_one_pass_preflight_manifest.json"
    )
    root_repair_policy_path = (
        PROJECT_ROOT
        / "industrials"
        / "transportation"
        / "review_policies"
        / "transportation_primary_source_root_repair_policy.csv"
    )
    required = (
        endpoint_path,
        requirement_path,
        ticker_scope_path,
        preflight_manifest_path,
        root_repair_policy_path,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            f"Missing primary-document enumeration inputs: {missing}"
        )
    preflight_manifest = _read_json(preflight_manifest_path)
    if (
        preflight_manifest.get("acceptance") != "PASS"
        or preflight_manifest.get("next_gate")
        != "ENUMERATE_DEDUPLICATE_AND_HASH_PRIMARY_DOCUMENTS_ONCE"
        or bool(
            preflight_manifest.get("parser_execution_authorized")
        )
    ):
        raise ValueError("DP6N preflight has not passed")
    _validate_sealed_artifact(
        manifest=preflight_manifest,
        artifact_name="one_pass_source_requirement_map",
        path=requirement_path,
    )
    _validate_sealed_artifact(
        manifest=preflight_manifest,
        artifact_name="one_pass_ticker_parser_scope",
        path=ticker_scope_path,
    )
    endpoint_rows = _apply_root_repair_policy(
        endpoint_rows=read_csv(endpoint_path),
        repair_rows=read_csv(root_repair_policy_path),
    )
    requirement_rows = read_csv(requirement_path)
    scope_rows = read_csv(ticker_scope_path)
    selected_tickers = {
        str(ticker).strip().upper()
        for ticker in args.ticker
        if str(ticker).strip()
    }
    if selected_tickers:
        endpoint_rows = [
            row
            for row in endpoint_rows
            if row["ticker"].upper() in selected_tickers
        ]
        unknown = selected_tickers - {
            row["ticker"].upper() for row in endpoint_rows
        }
        if unknown:
            raise ValueError(f"Unknown --ticker values={sorted(unknown)}")
    scope_by_ticker = {
        row["ticker"].upper(): dict(row) for row in scope_rows
    }
    search_terms_by_ticker: dict[str, set[str]] = {}
    lane_ids_by_ticker: dict[str, set[str]] = {}
    required_pair_counts: dict[str, int] = {}
    for row in requirement_rows:
        if row["document_discovery_required"] != "1":
            continue
        ticker = row["ticker"].upper()
        required_pair_counts[ticker] = (
            required_pair_counts.get(ticker, 0) + 1
        )
        search_terms_by_ticker.setdefault(ticker, set()).update(
            term
            for term in row["search_terms"].split("|")
            if term
        )
        lane_ids_by_ticker.setdefault(ticker, set()).update(
            lane
            for lane in row["candidate_source_lane_ids"].split("|")
            if lane
        )
    for ticker, scope in scope_by_ticker.items():
        scope["candidate_source_lane_ids"] = "|".join(
            sorted(lane_ids_by_ticker.get(ticker, set()))
        )
    # Keep this cache outside the dated report directory. The shorter path is
    # required for Windows-safe atomic temporary filenames, and the shared
    # content-addressed location makes interrupted discovery runs resumable.
    cache_root = (
        PROJECT_ROOT
        / "output"
        / "industrials_cache"
        / "transportation"
        / "non_sec_primary_discovery"
    )
    # sec_fundamentals is the configured user-agent section; the previously
    # referenced sec_disclosures key never existed, so the env override was
    # silently ignored.
    archive_user_agent = expand_env_vars(
        str(
            config.get("sec_fundamentals", {}).get(
                "user_agent",
                "JL, Independent Research, jm.357@hotmail.com",
            )
        )
    )
    throttle = _DomainThrottle(float(args.request_spacing_sec))
    discovery_rows: list[dict[str, object]] = []
    document_rows: list[dict[str, object]] = []
    external_rows: list[dict[str, object]] = []

    def enumerate_endpoint(
        endpoint: Mapping[str, str],
    ) -> tuple[
        list[dict[str, object]],
        list[dict[str, object]],
        list[dict[str, object]],
    ]:
        ticker = endpoint["ticker"].upper()
        scope = scope_by_ticker.get(ticker)
        if scope is None:
            raise ValueError(f"{ticker}: missing DP6N ticker scope")
        if endpoint["endpoint_type"] == "ARCHIVED_ISSUER_WEBSITE_CDX_ROOT":
            return _enumerate_archive_endpoint(
                endpoint=endpoint,
                scope=scope,
                search_terms=sorted(
                    search_terms_by_ticker.get(ticker, set())
                ),
                cache_root=cache_root,
                args=args,
                user_agent=archive_user_agent,
                throttle=throttle,
            )
        return _enumerate_active_endpoint(
            endpoint=endpoint,
            scope=scope,
            search_terms=sorted(
                search_terms_by_ticker.get(ticker, set())
            ),
            cache_root=cache_root,
            args=args,
            user_agent=ISSUER_DISCOVERY_USER_AGENT,
            throttle=throttle,
        )

    with ThreadPoolExecutor(
        max_workers=max(1, int(args.workers)),
        thread_name_prefix="transportation-primary-discovery",
    ) as executor:
        future_endpoint = {
            executor.submit(enumerate_endpoint, endpoint): endpoint
            for endpoint in endpoint_rows
        }
        for index, future in enumerate(
            as_completed(future_endpoint),
            start=1,
        ):
            endpoint = future_endpoint[future]
            try:
                pages, documents, external = future.result()
            except Exception as exc:
                response = CachedResponse(
                    request_url=endpoint["discovery_url"],
                    final_url="",
                    http_status=0,
                    content_type="",
                    payload=b"",
                    content_sha256="",
                    cache_path=Path(),
                    network_request_count=0,
                    error=f"{type(exc).__name__}: {exc}",
                )
                pages = [
                    _page_record(
                        endpoint=endpoint,
                        page_role=(
                            "ARCHIVE_CDX_HTML"
                            if endpoint["endpoint_type"].startswith(
                                "ARCHIVED_"
                            )
                            else "ROOT"
                        ),
                        depth=0,
                        response=response,
                    )
                ]
                documents = []
                external = []
            discovery_rows.extend(pages)
            document_rows.extend(documents)
            external_rows.extend(external)
            print(
                f"[{index}/{len(endpoint_rows)}] "
                f"{endpoint['ticker']} pages={len(pages)} "
                f"documents={len(documents)}",
                flush=True,
            )
    document_rows = deduplicate_document_rows(document_rows)
    future_exclusion_rows: list[dict[str, object]] = []
    asof_document_rows: list[dict[str, object]] = []
    for row in document_rows:
        published_hint = str(
            row.get("published_date_hint") or ""
        )
        archive_timestamp = str(
            row.get("earliest_archive_timestamp")
            or row.get("archive_timestamp")
            or ""
        )
        archive_date = (
            f"{archive_timestamp[:4]}-{archive_timestamp[4:6]}-"
            f"{archive_timestamp[6:8]}"
            if len(archive_timestamp) >= 8
            and archive_timestamp[:8].isdigit()
            else ""
        )
        exclusion_reason = ""
        exclusion_date = published_hint
        if published_hint and published_hint > asof_date:
            exclusion_reason = (
                "URL_OR_TITLE_DATE_AFTER_SOURCE_CENSUS_ASOF"
            )
        elif archive_date and archive_date > asof_date:
            exclusion_reason = (
                "ARCHIVE_CAPTURE_AFTER_SOURCE_CENSUS_ASOF"
            )
            exclusion_date = archive_date
        if exclusion_reason:
            future_exclusion_rows.append(
                {
                    "enumeration_version": (
                        PRIMARY_DOCUMENT_ENUMERATION_VERSION
                    ),
                    "document_id": row["document_id"],
                    "ticker": row["ticker"],
                    "source_url": row["source_url"],
                    "published_date_hint": exclusion_date,
                    "exclusion_reason": exclusion_reason,
                    "retrieval_authorized": 0,
                    "parser_execution_authorized": 0,
                }
            )
        else:
            asof_document_rows.append(row)
    document_rows = asof_document_rows
    document_rows, prefetched_document_count = (
        _apply_prefetched_content_hashes(
            rows=document_rows,
            cache_root=cache_root,
        )
    )
    stable_discovery_rows = sorted(
        _strip_runtime_fields(discovery_rows),
        key=lambda row: (
            str(row["ticker"]),
            str(row["page_role"]),
            str(row["request_url"]),
        ),
    )
    external_rows = sorted(
        {
            (
                str(row["ticker"]),
                str(row["source_url"]),
            ): dict(row)
            for row in external_rows
        }.values(),
        key=lambda row: (
            str(row["ticker"]),
            str(row["source_domain"]),
            str(row["source_url"]),
        ),
    )
    endpoint_enumeration_rows, errors = (
        build_endpoint_enumeration_rows(
            endpoint_rows=endpoint_rows,
            discovery_rows=stable_discovery_rows,
            document_rows=document_rows,
            required_pair_counts=required_pair_counts,
        )
    )
    if len(endpoint_enumeration_rows) != len(endpoint_rows):
        errors.append(
            "endpoint enumeration row count does not match selected roots"
        )
    duplicate_ids = len(document_rows) - len(
        {str(row["document_id"]) for row in document_rows}
    )
    if duplicate_ids:
        errors.append(f"duplicate document ids={duplicate_ids}")
    if any(
        int(str(row["parse_all_applicable_metrics"])) != 1
        or int(str(row["retrieval_authorized"])) != 0
        or int(str(row["parser_execution_authorized"])) != 0
        for row in document_rows
    ):
        errors.append(
            "document manifest violates the all-metric/no-execution contract"
        )
    discovery_path = (
        output_dir
        / "transportation_primary_document_discovery_pages.csv"
    )
    document_path = (
        output_dir
        / "transportation_primary_document_manifest.csv"
    )
    endpoint_enumeration_path = (
        output_dir
        / "transportation_primary_document_endpoint_enumeration.csv"
    )
    external_path = (
        output_dir
        / "transportation_primary_document_external_domain_review.csv"
    )
    future_exclusion_path = (
        output_dir
        / "transportation_primary_document_future_exclusions.csv"
    )
    manifest_path = (
        output_dir
        / "transportation_primary_document_enumeration_manifest.json"
    )
    write_csv_atomic(
        discovery_path,
        DISCOVERY_PAGE_FIELDS,
        stable_discovery_rows,
    )
    write_csv_atomic(
        document_path,
        PRIMARY_DOCUMENT_FIELDS,
        document_rows,
    )
    write_csv_atomic(
        endpoint_enumeration_path,
        ENDPOINT_ENUMERATION_FIELDS,
        endpoint_enumeration_rows,
    )
    write_csv_atomic(
        external_path,
        EXTERNAL_DOMAIN_FIELDS,
        external_rows,
    )
    write_csv_atomic(
        future_exclusion_path,
        FUTURE_EXCLUSION_FIELDS,
        future_exclusion_rows,
    )
    summary = summarize_primary_document_enumeration(
        endpoint_rows=endpoint_enumeration_rows,
        discovery_rows=stable_discovery_rows,
        document_rows=document_rows,
        external_rows=external_rows,
    )
    zero_document_count = sum(
        str(row["endpoint_status"])
        == "NO_PRIMARY_DOCUMENT_CANDIDATES_REVIEW_REQUIRED"
        for row in endpoint_enumeration_rows
    )
    partial_failure_count = sum(
        str(row["endpoint_status"])
        == "ENUMERATED_WITH_PARTIAL_DISCOVERY_FAILURES"
        for row in endpoint_enumeration_rows
    )
    reviewed_access_limitation_count = sum(
        str(row["endpoint_status"]).startswith("REVIEWED_")
        for row in endpoint_enumeration_rows
    )
    is_full_run = not selected_tickers
    acceptance = (
        "PASS"
        if is_full_run
        and len(endpoint_rows)
        == int(preflight_manifest["ticker_parser_scope_count"])
        and not errors
        else "FAIL"
    )
    if not is_full_run:
        errors.append(
            "diagnostic ticker filter cannot seal the full DP6O gate"
        )
    payload = {
        "acceptance": acceptance,
        "gate": "DP6O_PRIMARY_DOCUMENT_ENUMERATION_SEAL",
        "enumeration_version": PRIMARY_DOCUMENT_ENUMERATION_VERSION,
        "model_family": MODEL_FAMILY,
        "asof_date": asof_date,
        **summary,
        "selected_endpoint_count": len(endpoint_rows),
        "expected_endpoint_count": int(
            preflight_manifest["ticker_parser_scope_count"]
        ),
        "zero_document_endpoint_count": zero_document_count,
        "partial_discovery_failure_endpoint_count": (
            partial_failure_count
        ),
        "reviewed_root_access_limitation_count": (
            reviewed_access_limitation_count
        ),
        "root_repair_gate": "DP6P_FAILED_DISCOVERY_ROOT_REPAIR",
        "root_repair_policy_applied": True,
        "execution_contract": {
            "active_timeout_sec": float(args.active_timeout_sec),
            "archive_timeout_sec": float(args.archive_timeout_sec),
            "max_retries": int(args.max_retries),
            "request_spacing_sec": float(
                args.request_spacing_sec
            ),
            "max_discovery_bytes": int(
                args.max_discovery_bytes
            ),
            "max_navigation_pages_per_root": int(
                args.max_navigation_pages_per_root
            ),
            "max_sitemaps_per_root": int(
                args.max_sitemaps_per_root
            ),
            "max_navigation_depth": int(
                args.max_navigation_depth
            ),
            "max_archive_year_slice_failures_per_lane": int(
                args.max_archive_year_slice_failures_per_lane
            ),
        },
        "future_document_exclusion_count": len(
            future_exclusion_rows
        ),
        "primary_source_hierarchy_enforced": True,
        "manifest_url_identity_hash_sealed": not errors,
        "archive_source_digests_preserved": True,
        "document_bodies_retrieved": (
            prefetched_document_count > 0
        ),
        "document_body_prefetch_count": prefetched_document_count,
        "document_content_sha256_complete": (
            bool(document_rows)
            and prefetched_document_count == len(document_rows)
        ),
        "retrieval_manifest_ready_for_review": acceptance == "PASS",
        "retrieval_authorized": False,
        "parser_execution_authorized": False,
        "network_requests": sum(
            int(str(row.get("network_request_count") or 0))
            for row in discovery_rows
        ),
        "document_retrieval_invocations": 0,
        "parser_invocations": 0,
        "feature_build_invocations": 0,
        "historical_materialization_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_invocations": 0,
        "production_promotion_authorized": False,
        "errors": errors,
        "inputs": {
            "one_pass_preflight_manifest": {
                "path": str(preflight_manifest_path.resolve()),
                "sha256": file_sha256(preflight_manifest_path),
            },
            "endpoint_roots": {
                "path": str(endpoint_path.resolve()),
                "sha256": file_sha256(endpoint_path),
            },
            "root_repair_policy": {
                "path": str(root_repair_policy_path.resolve()),
                "sha256": file_sha256(root_repair_policy_path),
                "row_count": len(read_csv(root_repair_policy_path)),
            },
            "one_pass_source_requirement_map": {
                "path": str(requirement_path.resolve()),
                "sha256": file_sha256(requirement_path),
            },
            "one_pass_ticker_parser_scope": {
                "path": str(ticker_scope_path.resolve()),
                "sha256": file_sha256(ticker_scope_path),
            },
        },
        "artifacts": {
            "discovery_pages": {
                "path": str(discovery_path.resolve()),
                "row_count": len(stable_discovery_rows),
                "sha256": file_sha256(discovery_path),
            },
            "primary_document_manifest": {
                "path": str(document_path.resolve()),
                "row_count": len(document_rows),
                "sha256": file_sha256(document_path),
            },
            "endpoint_enumeration": {
                "path": str(endpoint_enumeration_path.resolve()),
                "row_count": len(endpoint_enumeration_rows),
                "sha256": file_sha256(endpoint_enumeration_path),
            },
            "external_domain_review": {
                "path": str(external_path.resolve()),
                "row_count": len(external_rows),
                "sha256": file_sha256(external_path),
            },
            "future_document_exclusions": {
                "path": str(future_exclusion_path.resolve()),
                "row_count": len(future_exclusion_rows),
                "sha256": file_sha256(future_exclusion_path),
            },
        },
        "next_gate": (
            "REPAIR_FAILED_DISCOVERY_ROOTS"
            if errors
            else (
                "REVIEW_ZERO_PARTIAL_AND_ACCESS_LIMITED_ENDPOINTS"
                if (
                    zero_document_count
                    or partial_failure_count
                    or reviewed_access_limitation_count
                )
                else (
                    "REVIEW_EXTERNAL_ASSET_DOMAINS_BEFORE_HYDRATION"
                    if external_rows
                    else (
                        "HYDRATE_HASH_AND_CONTENT_DEDUPLICATE_"
                        "PRIMARY_DOCUMENTS_ONCE"
                    )
                )
            )
        ),
    }
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if acceptance == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode, urlparse

import requests


LOGGER = logging.getLogger(__name__)
_CACHE_WRITE_LOCK = threading.Lock()


class HostThrottle:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_allowed: dict[str, float] = {}

    def wait(self, url: str, min_interval_sec: float) -> None:
        if min_interval_sec <= 0:
            return
        host = urlparse(url).netloc.lower()
        if not host:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                allowed_at = self._next_allowed.get(host, 0.0)
                if now >= allowed_at:
                    self._next_allowed[host] = now + min_interval_sec
                    return
                sleep_for = allowed_at - now
            time.sleep(sleep_for)


class CachedHttpClient:
    def __init__(
        self,
        *,
        cache_dir: Path,
        sleep_sec: float = 0.2,
        timeout_sec: float = 45.0,
        max_retries: int = 3,
        throttle: Optional[HostThrottle] = None,
    ) -> None:
        self.cache_dir = cache_dir
        self.sleep_sec = sleep_sec
        self.timeout_sec = timeout_sec
        self.max_retries = max(1, int(max_retries))
        self.throttle = throttle or HostThrottle()
        self.session = requests.Session()

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "CachedHttpClient":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def cache_path(self, namespace: str, url: str, params: Optional[dict[str, Any]] = None) -> Path:
        key = url
        if params:
            key += "?" + urlencode(sorted((str(k), str(v)) for k, v in params.items()))
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return self.cache_dir / namespace / digest[:2] / f"{digest}.cache"

    @staticmethod
    def cache_is_fresh(path: Path, ttl_hours: float) -> bool:
        """Return true when cached content is usable.

        A negative TTL disables expiry and treats an existing cache file as fresh.
        """
        if not path.exists():
            return False
        if ttl_hours < 0:
            return True
        try:
            age_seconds = time.time() - path.stat().st_mtime
        except FileNotFoundError:
            return False
        return age_seconds <= ttl_hours * 3600.0

    def _get_text(self, *, url: str, params: Optional[dict[str, Any]], headers: dict[str, str]) -> str:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            self.throttle.wait(url, self.sleep_sec)
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout_sec)
                if resp.status_code in {429, 500, 502, 503, 504} and attempt < self.max_retries - 1:
                    time.sleep(min(8.0, 2.0**attempt))
                    continue
                resp.raise_for_status()
                return resp.text
            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status is not None and 400 <= status < 500 and status != 429:
                    # Non-retryable client error: retrying cannot succeed.
                    raise
                last_exc = exc
                if attempt < self.max_retries - 1:
                    time.sleep(min(8.0, 2.0**attempt))
            except Exception as exc:
                last_exc = exc
                if attempt < self.max_retries - 1:
                    time.sleep(min(8.0, 2.0**attempt))
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"Request failed without a captured exception: {url}")

    def fetch_json(
        self,
        *,
        namespace: str,
        url: str,
        params: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
        ttl_hours: float = 24.0,
    ) -> Any:
        path = self.cache_path(namespace, url, params)
        if self.cache_is_fresh(path, ttl_hours):
            try:
                cached_text = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                cached_text = ""
            if cached_text:
                try:
                    parsed_cache = json.loads(cached_text)
                except json.JSONDecodeError:
                    LOGGER.warning("Ignoring invalid JSON cache file: %s", path)
                    with _CACHE_WRITE_LOCK:
                        try:
                            path.unlink()
                        except FileNotFoundError:
                            pass
                else:
                    if parsed_cache is not None:
                        return parsed_cache
                    LOGGER.warning("Ignoring null JSON cache file: %s", path)
                    with _CACHE_WRITE_LOCK:
                        try:
                            path.unlink()
                        except FileNotFoundError:
                            pass
            elif path.exists():
                LOGGER.warning("Ignoring empty JSON cache file: %s", path)
        text = self._get_text(url=url, params=params, headers=headers or {})
        parsed = json.loads(text)
        if parsed is None:
            raise ValueError(f"Refusing to cache empty JSON response from {url}")
        with _CACHE_WRITE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return parsed

    def fetch_text(
        self,
        *,
        namespace: str,
        url: str,
        params: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
        ttl_hours: float = 168.0,
    ) -> str:
        path = self.cache_path(namespace, url, params)
        if self.cache_is_fresh(path, ttl_hours):
            try:
                cached_text = path.read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                cached_text = ""
            if cached_text:
                return cached_text
            if path.exists():
                LOGGER.warning("Ignoring empty text cache file: %s", path)

        text = self._get_text(url=url, params=params, headers=headers or {})
        if not text:
            raise ValueError(f"Refusing to cache empty text response from {url}")
        with _CACHE_WRITE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return text

#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from macro_raw_config import utc_now_iso

logger = logging.getLogger(__name__)
RETRYABLE_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class RequestSettings:
    timeout_seconds: int
    max_retries: int
    backoff_base_seconds: float
    backoff_cap_seconds: float
    user_agent: str


@dataclass(frozen=True)
class HttpResponse:
    url: str
    status_code: int
    headers: dict[str, str]
    content: bytes
    fetched_at: str

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8", errors="replace"))

    def text(self, encoding: str = "utf-8") -> str:
        return self.content.decode(encoding, errors="replace")


class RateLimiter:
    def __init__(self, min_interval_seconds: float) -> None:
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._next_allowed = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self.min_interval_seconds <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                wait_for = self._next_allowed - now
                if wait_for <= 0:
                    self._next_allowed = max(self._next_allowed, now) + self.min_interval_seconds
                    return
            time.sleep(min(wait_for, 0.05))


class HttpClient:
    def __init__(self, settings: RequestSettings, limiter: RateLimiter | None = None) -> None:
        self.settings = settings
        self.limiter = limiter
        self._local = threading.local()

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"User-Agent": self.settings.user_agent})
            self._local.session = session
        return session

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse:
        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            if self.limiter is not None:
                self.limiter.wait()
            try:
                response = self._session().get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=self.settings.timeout_seconds,
                )
                if response.status_code in RETRYABLE_HTTP_CODES and attempt < self.settings.max_retries:
                    self._sleep_backoff(
                        attempt,
                        response.status_code,
                        retry_after_seconds=_retry_after_seconds(response.headers.get("Retry-After")),
                    )
                    continue
                response.raise_for_status()
                return HttpResponse(
                    url=response.url,
                    status_code=response.status_code,
                    headers={k: v for k, v in response.headers.items()},
                    content=response.content,
                    fetched_at=utc_now_iso(),
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= self.settings.max_retries:
                    break
                self._sleep_backoff(attempt, None)
        raise RuntimeError(f"HTTP GET failed for {url}: {last_error}") from last_error

    def _sleep_backoff(self, attempt: int, status_code: int | None, retry_after_seconds: float | None = None) -> None:
        base = max(0.1, self.settings.backoff_base_seconds)
        cap = max(base, self.settings.backoff_cap_seconds)
        delay = min(cap, base * (2 ** attempt))
        jitter = random.uniform(0.0, delay * 0.25)
        if retry_after_seconds is not None:
            delay = min(cap, max(delay, retry_after_seconds))
            jitter = 0.0
        total_attempts = self.settings.max_retries + 1
        current_attempt = attempt + 1
        if status_code is not None:
            logger.warning(
                "Retrying HTTP request after status=%s in %.2fs (attempt %d of %d)",
                status_code,
                delay + jitter,
                current_attempt + 1,
                total_attempts,
            )
        else:
            logger.warning(
                "Retrying HTTP request after transport error in %.2fs (attempt %d of %d)",
                delay + jitter,
                current_attempt + 1,
                total_attempts,
            )
        time.sleep(delay + jitter)


def _retry_after_seconds(value: str | None) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        seconds = float(text)
        return max(0.0, seconds)
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return max(0.0, (retry_at - now).total_seconds())

"""Shared capacity controls for Interactive Brokers market-data requests."""
from __future__ import annotations

import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType


IBKR_ACCOUNT_MARKET_DATA_LIMIT = 100
IBKR_STREAMING_BATCH_LIMIT = 90
DEFAULT_IBKR_LOCK_TIMEOUT_SEC = 30 * 60.0
DEFAULT_IBKR_LOCK_PATH = Path(tempfile.gettempdir()) / "staging_scalper_ibkr_market_data.lock"


def bounded_streaming_batch_size(requested: int) -> int:
    """Return a positive batch size with 10% headroom below IB's 100-line limit."""
    return min(IBKR_STREAMING_BATCH_LIMIT, max(1, int(requested)))


class IBKRMarketDataLock:
    """Cross-process mutex preventing sector pipelines from stacking IB subscriptions."""

    def __init__(
        self,
        path: Path = DEFAULT_IBKR_LOCK_PATH,
        *,
        timeout_sec: float = DEFAULT_IBKR_LOCK_TIMEOUT_SEC,
        poll_sec: float = 0.25,
    ) -> None:
        if timeout_sec < 0:
            raise ValueError(f"timeout_sec must be non-negative, got {timeout_sec}")
        if poll_sec <= 0:
            raise ValueError(f"poll_sec must be positive, got {poll_sec}")
        self.path = path
        self.timeout_sec = float(timeout_sec)
        self.poll_sec = float(poll_sec)
        self.fd: int | None = None

    def __enter__(self) -> IBKRMarketDataLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            deadline = time.monotonic() + self.timeout_sec
            while True:
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    self._try_lock(fd)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"Timed out after {self.timeout_sec:.1f}s waiting for shared IB market-data lock "
                            f"{self.path}"
                        ) from exc
                    time.sleep(min(self.poll_sec, max(0.0, deadline - time.monotonic())))
        except BaseException:
            os.close(fd)
            raise

        self.fd = fd
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(
                fd,
                (
                    f"pid={os.getpid()} "
                    f"started_utc={datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
                ).encode(),
            )
            os.fsync(fd)
        except BaseException:
            self._unlock(fd)
            os.close(fd)
            self.fd = None
            raise
        return self

    @staticmethod
    def _try_lock(fd: int) -> None:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        if self.fd is None:
            return
        try:
            self._unlock(self.fd)
        finally:
            os.close(self.fd)
            self.fd = None

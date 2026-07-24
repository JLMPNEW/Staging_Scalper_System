"""Crash-safe process lock shared by industrials refresh pipelines."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType


class RefreshLock:
    """Exclusive advisory lock automatically released by the OS after a crash."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> RefreshLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            # Windows byte-range locking requires a lockable byte to exist.
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            self._lock(fd)
        except OSError as exc:
            os.close(fd)
            try:
                detail = self.path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                # Windows can deny reads while another process holds the byte-range
                # lock. Lock contention itself is authoritative; metadata is optional.
                detail = "owner metadata unavailable while locked"
            raise RuntimeError(f"Another industrials refresh owns {self.path}: {detail}") from exc

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
            # __exit__ is not invoked when __enter__ raises, so release explicitly.
            try:
                self._unlock(fd)
            finally:
                os.close(fd)
                self.fd = None
            raise
        return self

    @staticmethod
    def _lock(fd: int) -> None:
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

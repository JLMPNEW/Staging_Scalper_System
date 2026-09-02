"""Crash-safe cross-process locks for shared SEC/Form 4 resources."""
from __future__ import annotations

import json
import os
import socket
import time
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import BinaryIO


class WaitFileLock:
    """Exclusive advisory lock that waits and is released by the OS on process exit."""

    def __init__(
        self,
        path: Path,
        *,
        timeout_seconds: float,
        poll_seconds: float = 1.0,
        owner: str = "sec_form4_orchestrator",
    ) -> None:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be >= 0")
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be > 0")
        self.path = path
        self.timeout_seconds = float(timeout_seconds)
        self.poll_seconds = float(poll_seconds)
        self.owner = str(owner).strip() or "sec_form4_orchestrator"
        self._handle: BinaryIO | None = None

    @staticmethod
    def _try_lock(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _owner_detail(self) -> str:
        try:
            raw = self.path.read_bytes()
        except OSError:
            return "owner metadata unavailable while locked"
        # Byte zero is reserved for the Windows byte-range lock.
        return raw[1:].decode("utf-8", errors="replace").strip() or "owner metadata unavailable"

    def __enter__(self) -> WaitFileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())

        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                self._try_lock(handle)
                break
            except (OSError, BlockingIOError) as exc:
                if time.monotonic() >= deadline:
                    detail = self._owner_detail()
                    handle.close()
                    raise TimeoutError(
                        f"Timed out after {self.timeout_seconds:.1f}s waiting for {self.path}: {detail}"
                    ) from exc
                time.sleep(min(self.poll_seconds, max(0.0, deadline - time.monotonic())))

        self._handle = handle
        try:
            metadata = {
                "host": socket.gethostname(),
                "owner": self.owner,
                "pid": os.getpid(),
                "started_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            handle.seek(1)
            handle.truncate()
            handle.write((json.dumps(metadata, sort_keys=True) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            try:
                self._unlock(handle)
            finally:
                handle.close()
                self._handle = None
            raise
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        if self._handle is None:
            return
        try:
            self._unlock(self._handle)
        finally:
            self._handle.close()
            self._handle = None

"""Atomic report/CSV writers shared by all industrials pipeline scripts (XC-10).

Every writer that produces a final-path artifact (audit CSVs, review packs,
generated universe CSVs that later stages consume as inputs) must go through
these helpers instead of opening the destination path with ``'w'``. The write
lands in a temporary file in the destination directory and is published with
``os.replace``, so readers never observe a truncated file — a crash mid-write
leaves the previous artifact intact.
"""

from __future__ import annotations

import csv
import os
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Callable, TextIO


ATOMIC_REPLACE_ATTEMPTS = 8
TRANSIENT_WINDOWS_REPLACE_ERRORS = frozenset({5, 32, 33})


def _replace_with_retry(source: Path, destination: Path) -> None:
    for attempt in range(ATOMIC_REPLACE_ATTEMPTS):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            transient = isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in TRANSIENT_WINDOWS_REPLACE_ERRORS
            if not transient or attempt + 1 >= ATOMIC_REPLACE_ATTEMPTS:
                raise
            time.sleep(min(0.05 * (2**attempt), 1.0))


def _write_atomic(
    path: Path,
    write_body: Callable[[TextIO], None],
    *,
    encoding: str,
    newline: str | None,
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the temporary basename short. Windows paths can otherwise exceed
    # legacy path limits when a long report name sits under a deep run root.
    handle_fd, tmp_name = tempfile.mkstemp(
        prefix=".tmp-",
        suffix=path.suffix or ".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with open(handle_fd, "w", encoding=encoding, newline=newline) as handle:
            write_body(handle)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def write_csv_atomic(
    path: Path | str,
    fieldnames: Sequence[str],
    rows: Iterable[Mapping[str, Any]],
    *,
    encoding: str = "utf-8",
) -> None:
    """Write a CSV with a header row atomically (tmp file + os.replace).

    ``rows`` must be mappings keyed by ``fieldnames``; an unexpected key raises
    (csv.DictWriter's default) so schema drift fails loudly instead of silently
    dropping columns.
    """
    field_list = list(fieldnames)
    if not field_list:
        raise ValueError(f"write_csv_atomic({path}): fieldnames must not be empty")

    def body(handle: TextIO) -> None:
        writer = csv.DictWriter(handle, fieldnames=field_list)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    _write_atomic(Path(path), body, encoding=encoding, newline="")


def write_text_atomic(path: Path | str, text: str, *, encoding: str = "utf-8") -> None:
    """Write a text artifact atomically (tmp file + os.replace)."""

    def body(handle: TextIO) -> None:
        handle.write(text)

    _write_atomic(Path(path), body, encoding=encoding, newline=None)

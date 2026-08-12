"""Filesystem operations that remain correct beyond legacy Windows ``MAX_PATH``.

Python's ordinary ``Path`` methods can report a long existing file as missing on
Windows when the process has not opted into long-path semantics. Keep the
platform-specific extended-length prefix at the I/O boundary and return ordinary
absolute paths from resolution functions so callers can still perform readable,
stable containment comparisons.
"""

from __future__ import annotations

import builtins
import os
from pathlib import Path
from typing import Any, BinaryIO, IO, TextIO, overload


PathLike = str | os.PathLike[str]
_WINDOWS_EXTENDED_PREFIX = "\\\\?\\"
_WINDOWS_EXTENDED_UNC_PREFIX = "\\\\?\\UNC\\"


def _without_windows_extended_prefix(value: str) -> str:
    if os.name != "nt":
        return value
    if value.casefold().startswith(_WINDOWS_EXTENDED_UNC_PREFIX.casefold()):
        return "\\\\" + value[len(_WINDOWS_EXTENDED_UNC_PREFIX) :]
    if value.startswith(_WINDOWS_EXTENDED_PREFIX):
        return value[len(_WINDOWS_EXTENDED_PREFIX) :]
    return value


def absolute_path(path: PathLike) -> Path:
    """Return an ordinary (non-``\\?\\``) absolute path without resolving links."""

    raw = _without_windows_extended_prefix(os.fsdecode(os.fspath(path)))
    return Path(os.path.abspath(raw))


def filesystem_path(path: PathLike) -> Path:
    """Return an absolute path suitable for filesystem calls on this platform."""

    ordinary = str(absolute_path(path))
    if os.name != "nt":
        return Path(ordinary)
    if ordinary.startswith("\\\\"):
        return Path(_WINDOWS_EXTENDED_UNC_PREFIX + ordinary[2:])
    return Path(_WINDOWS_EXTENDED_PREFIX + ordinary)


def runtime_path(path: PathLike) -> Path:
    """Return a path spelling safe to persist or hand to a worker process.

    Preserve ordinary Windows spellings while they are below legacy MAX_PATH,
    because those are easier to compare and serialize. Long paths retain the
    extended prefix so generic ``Path.open`` consumers remain able to read them.
    """

    ordinary = absolute_path(path)
    if os.name == "nt" and len(str(ordinary)) >= 260:
        return filesystem_path(ordinary)
    return ordinary


def resolve_path(path: PathLike, *, strict: bool = False) -> Path:
    """Resolve links and return an ordinary absolute path.

    ``os.path.realpath`` is deliberately invoked with the extended-length path;
    invoking it on the ordinary long path can silently leave links unresolved.
    """

    io_path = os.fspath(filesystem_path(path))
    resolved = os.path.realpath(io_path, strict=strict)
    return absolute_path(_without_windows_extended_prefix(resolved))


def lexists_path(path: PathLike) -> bool:
    return os.path.lexists(filesystem_path(path))


def path_exists(path: PathLike) -> bool:
    return os.path.exists(filesystem_path(path))


def is_file(path: PathLike) -> bool:
    return os.path.isfile(filesystem_path(path))


def is_dir(path: PathLike) -> bool:
    return os.path.isdir(filesystem_path(path))


# Explicit aliases make call sites self-documenting while preserving the short
# names used by the SEC path-validation layer.
is_file_path = is_file
is_dir_path = is_dir


def stat_path(path: PathLike, *, follow_symlinks: bool = True) -> os.stat_result:
    return os.stat(filesystem_path(path), follow_symlinks=follow_symlinks)


@overload
def open_path(path: PathLike, mode: str = "r", **kwargs: Any) -> TextIO: ...


@overload
def open_path(path: PathLike, mode: str, **kwargs: Any) -> BinaryIO: ...


def open_path(path: PathLike, mode: str = "r", **kwargs: Any) -> IO[Any]:
    return builtins.open(filesystem_path(path), mode, **kwargs)


def read_bytes(path: PathLike) -> bytes:
    with open_path(path, "rb") as handle:
        return handle.read()


def mkdir_path(
    path: PathLike,
    *,
    parents: bool = False,
    exist_ok: bool = False,
) -> None:
    target = filesystem_path(path)
    if parents:
        os.makedirs(target, exist_ok=exist_ok)
        return
    try:
        os.mkdir(target)
    except FileExistsError:
        if not exist_ok or not is_dir(path):
            raise


def link_path(source: PathLike, target: PathLike) -> None:
    os.link(filesystem_path(source), filesystem_path(target))


def replace_path(source: PathLike, target: PathLike) -> None:
    os.replace(filesystem_path(source), filesystem_path(target))


def unlink_path(path: PathLike, *, missing_ok: bool = False) -> None:
    try:
        os.unlink(filesystem_path(path))
    except FileNotFoundError:
        if not missing_ok:
            raise

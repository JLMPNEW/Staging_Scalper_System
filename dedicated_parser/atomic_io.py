'''Secure atomic writers for dedicated-parser artifacts.'''

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO


@contextmanager
def atomic_text_writer(
    path: Path,
    *,
    encoding: str = 'utf-8',
    newline: str | None = None,
) -> Iterator[TextIO]:
    '''Yield an exclusive same-directory temporary and atomically publish it.'''
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f'.{destination.name}.',
        suffix='.tmp',
    )
    temporary = Path(temporary_name)
    handle: TextIO | None = None
    try:
        handle = os.fdopen(
            descriptor, 'w', encoding=encoding, newline=newline
        )
        descriptor = -1
        yield handle
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.replace(temporary, destination)
    finally:
        if handle is not None:
            handle.close()
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def atomic_write_text(
    path: Path,
    text: str,
    *,
    encoding: str = 'utf-8',
) -> None:
    with atomic_text_writer(path, encoding=encoding) as handle:
        handle.write(text)

"""Immutable artifact publishing for the Consumer Defensive v3 promotion lane."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping


def publish_immutable_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Publish canonical review JSON atomically without overwriting divergence."""

    requested = Path(path).expanduser()
    if requested.is_symlink():
        raise ValueError(f"immutable artifact path cannot be a symlink: {requested}")
    resolved = requested.resolve()
    encoded = (
        json.dumps(
            payload,
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    )
    if resolved.exists():
        if resolved.is_symlink() or not resolved.is_file():
            raise ValueError(f"immutable artifact path is unsafe: {resolved}")
        if resolved.read_text(encoding="utf-8") != encoded:
            raise FileExistsError(
                f"refusing to overwrite divergent immutable artifact: {resolved}"
            )
        return
    resolved.parent.mkdir(parents=True, exist_ok=True)
    temporary = resolved.with_name(f".{resolved.name}.{os.getpid()}.tmp")
    if temporary.exists():
        raise FileExistsError(
            f"stale immutable-artifact temporary exists: {temporary}"
        )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, resolved)
    finally:
        if temporary.exists():
            temporary.unlink()


__all__ = ["publish_immutable_json"]

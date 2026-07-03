"""Shared Stage 11 helpers: the lockbox declaration is loaded and enforced identically everywhere."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from portfolio_layer.core.config import cfg_get, resolve_path
from portfolio_layer.core.contracts import sha256_file


def load_lockbox(config: dict[str, Any], config_path: Path) -> dict[str, Any]:
    """Verify the config lockbox mirror against the canonical protocol document, or refuse to run.

    docs/LOCKBOX_PROTOCOL.md is canonical; the config `stage11_lockbox` block is its machine-readable
    mirror. Any divergence (missing doc, missing keys, inconsistent ordering, dates absent from the
    doc text) raises ValueError so Stage 11 scripts fail closed.
    """
    block = cfg_get(config, "stage11_lockbox", None)
    if not isinstance(block, dict):
        raise ValueError("config stage11_lockbox block missing; it must mirror docs/LOCKBOX_PROTOCOL.md")
    required = ("protocol_doc", "declared", "dev_window_start", "dev_window_end", "sealed_start", "lockbox_opened")
    missing = [key for key in required if key not in block]
    if missing:
        raise ValueError(f"config stage11_lockbox missing keys: {missing}")
    dev_start = date.fromisoformat(str(block["dev_window_start"]))
    dev_end = date.fromisoformat(str(block["dev_window_end"]))
    sealed_start = date.fromisoformat(str(block["sealed_start"]))
    if not dev_start <= dev_end < sealed_start:
        raise ValueError(
            f"stage11_lockbox dates inconsistent: need dev_window_start <= dev_window_end < sealed_start, "
            f"got {dev_start} / {dev_end} / {sealed_start}"
        )
    doc = resolve_path(str(block["protocol_doc"]), base_dir=config_path.parent)
    if not doc.exists():
        raise ValueError(f"lockbox protocol document missing: {doc}")
    text = doc.read_text(encoding="utf-8")
    divergent = [
        value for value in (str(block["dev_window_start"]), str(block["dev_window_end"]), str(block["sealed_start"]))
        if value not in text
    ]
    if divergent:
        raise ValueError(f"config stage11_lockbox dates not present in protocol doc (divergence): {divergent}")
    return {
        "dev_window_start": str(block["dev_window_start"]),
        "dev_window_end": str(block["dev_window_end"]),
        "sealed_start": str(block["sealed_start"]),
        "training_label_end_max": str(block.get("training_label_end_max", block["dev_window_end"])),
        "lockbox_opened": bool(block.get("lockbox_opened", False)),
        "protocol_path": doc,
        "protocol_sha256": sha256_file(doc),
    }

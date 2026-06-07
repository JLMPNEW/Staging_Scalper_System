#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEFAULT_SEC_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config_sec_form4.yaml"


def resolve_config_path(config_path: Path | None) -> Path:
    return Path(config_path) if config_path is not None else DEFAULT_SEC_CONFIG_PATH


def load_sec_form4_config(config_path: Path | None) -> tuple[Path, dict[str, Any]]:
    path = resolve_config_path(config_path)
    if not path.exists():
        return path, {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        return path, {}
    if isinstance(data.get("sec_form4"), dict):
        return path, data["sec_form4"]
    return path, data


def cfg_get(cfg: dict[str, Any], *keys: str, default: Any = None) -> Any:
    cur: Any = cfg
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur

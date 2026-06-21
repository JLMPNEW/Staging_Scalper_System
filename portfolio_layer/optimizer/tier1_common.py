#!/usr/bin/env python3
"""
Shared helpers for Tier-1 scripts.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd


def _get_tier1_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    if "tier1_optimizer" in cfg:
        tcfg = cfg.get("tier1_optimizer", None)
        if isinstance(tcfg, dict):
            return tcfg
        raise ValueError("tier1_optimizer must be a mapping/dict when present in config.")
    return cfg


def _get_dict(d: Dict[str, Any], key: str) -> Dict[str, Any]:
    v = d.get(key, {})
    return v if isinstance(v, dict) else {}


def _as_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return bool(default)
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y", "t"):
        return True
    if s in ("0", "false", "no", "n", "f"):
        return False
    return bool(default)


def _parse_date_maybe(v: Any) -> Optional[date]:
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.lower() in ("none", "null", "nan"):
        return None
    ts = pd.to_datetime(s, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.date()


def _resolve_output_dir(cfg: Dict[str, Any], cfg_path: Path) -> Path:
    raw = cfg.get("output_dir", None)
    if raw is None or str(raw).strip() == "":
        return cfg_path.parent
    p = Path(str(raw)).expanduser()
    if not p.is_absolute():
        p = (cfg_path.parent / p).resolve()
    return p


def _resolve_tier1_input_path(output_dir: Path, raw_path: Any) -> Path:
    p = Path(str(raw_path)).expanduser()
    if p.is_absolute():
        return p
    return (output_dir / p).resolve()

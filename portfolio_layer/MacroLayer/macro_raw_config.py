#!/usr/bin/env python3
from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

DEFAULT_MACRO_CONFIG_PATH = Path(__file__).resolve().parent / "config_macro_raw.yaml"
SQLITE_BUSY_TIMEOUT_MS = 30000
TRUE_STRINGS = {"1", "true", "t", "yes", "y", "on"}
FALSE_STRINGS = {"0", "false", "f", "no", "n", "off"}


def resolve_config_path(config_path: Path | None) -> Path:
    return Path(config_path) if config_path is not None else DEFAULT_MACRO_CONFIG_PATH


def load_macro_raw_config(config_path: Path | None) -> tuple[Path, dict[str, Any]]:
    path = resolve_config_path(config_path)
    if not path.exists():
        return path, {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        return path, {}
    if isinstance(data.get("macro_raw"), dict):
        return path, data["macro_raw"]
    return path, data


def cfg_get(cfg: dict[str, Any], *keys: str, default: Any = None) -> Any:
    cur: Any = cfg
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def repo_root_from_config(config_path: Path) -> Path:
    return config_path.resolve().parent.parent


def resolve_path(config_path: Path, raw_value: str | None) -> Path | None:
    if not raw_value:
        return None
    path = Path(str(raw_value)).expanduser()
    if path.is_absolute():
        return path
    return (repo_root_from_config(config_path) / path).resolve()


def resolve_db_path(cfg: dict[str, Any], config_path: Path, override: Path | None = None) -> Path:
    if override is not None:
        return Path(override).expanduser().resolve()
    db_path_raw = cfg_get(cfg, "db_path", default=None)
    if not db_path_raw:
        raise ValueError("macro_raw.db_path is required in config.")
    db_path = resolve_path(config_path, str(db_path_raw))
    if db_path is None:
        raise ValueError("Failed to resolve macro_raw.db_path.")
    return db_path


def configure_pipeline_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    for handler in root.handlers:
        if handler.formatter is not None:
            handler.formatter.converter = time.gmtime


def parse_boolish(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    if text in TRUE_STRINGS:
        return True
    if text in FALSE_STRINGS:
        return False
    return default


def connect_sqlite(db_path: Path, *, row_factory: Any | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0)
    if row_factory is not None:
        conn.row_factory = row_factory
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_date(value: str | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y-%m", "%Y%m"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.date()
        except ValueError:
            continue
    return None


def previous_or_same_business_day(d: date) -> date:
    out = d
    while out.weekday() >= 5:
        out -= timedelta(days=1)
    return out


def getenv_str(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    text = value.strip()
    return text or None

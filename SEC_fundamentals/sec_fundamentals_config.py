#!/usr/bin/env python3
from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml
from pandas.tseries.holiday import USFederalHolidayCalendar

DEFAULT_SEC_FUND_CONFIG_PATH = Path(__file__).resolve().parent / "config_sec_fundamentals.yaml"
SQL_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
QUARTERLY_FORMS = frozenset({"10-Q", "10-Q/A", "10-QT", "10-QT/A"})
FOREIGN_ANNUAL_FORMS = frozenset({"20-F", "20-F/A", "40-F", "40-F/A"})
ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "10-KT", "10-KT/A"}) | FOREIGN_ANNUAL_FORMS
PERIODIC_FORMS = frozenset(ANNUAL_FORMS | QUARTERLY_FORMS)
SUPPLEMENTAL_FORMS = frozenset({"8-K", "8-K/A", "6-K", "6-K/A"})
SAFE_DIVIDE_MIN_ABS_DENOMINATOR = 1e-8


def resolve_config_path(config_path: Path | None) -> Path:
    return Path(config_path) if config_path is not None else DEFAULT_SEC_FUND_CONFIG_PATH


def load_sec_fundamentals_config(config_path: Path | None) -> tuple[Path, dict[str, Any]]:
    path = resolve_config_path(config_path)
    if not path.exists():
        return path, {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        return path, {}
    if isinstance(data.get("sec_fundamentals"), dict):
        return path, data["sec_fundamentals"]
    return path, data


def cfg_get(cfg: dict[str, Any], *keys: str, default: Any = None) -> Any:
    cur: Any = cfg
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def parse_iso_date(value: str | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def normalize_cik_10d(raw: str | int | None) -> str | None:
    if raw is None:
        return None
    text = re.sub(r"\D", "", str(raw).strip())
    if not text:
        return None
    if set(text) == {"0"}:
        return None
    return text.zfill(10)


def sql_normalized_cik_expr(column_sql: str) -> str:
    return (
        "CASE "
        f"WHEN COALESCE(TRIM(CAST({column_sql} AS TEXT)), '') = '' THEN NULL "
        f"WHEN REPLACE(TRIM(CAST({column_sql} AS TEXT)), '0', '') = '' THEN NULL "
        f"ELSE printf('%010d', CAST({column_sql} AS INTEGER)) END"
    )


def validate_sql_identifier(name: str, label: str, *, allow_dotted: bool = False) -> str:
    out = str(name or "").strip()
    if not out:
        raise ValueError(f"Invalid {label}: {name!r}")
    parts = out.split(".") if allow_dotted else [out]
    if any(not part or not SQL_IDENTIFIER_RE.fullmatch(part) for part in parts):
        raise ValueError(f"Invalid {label}: {name!r}")
    return out


@lru_cache(maxsize=32)
def _us_federal_holidays(start_year: int, end_year: int) -> frozenset[date]:
    cal = USFederalHolidayCalendar()
    holidays = cal.holidays(
        start=f"{start_year}-01-01",
        end=f"{end_year}-12-31",
    )
    return frozenset(ts.date() for ts in holidays)


def previous_or_same_business_day(d: date) -> date:
    out = d
    holidays = _us_federal_holidays(out.year - 1, out.year + 1)
    while out.weekday() >= 5 or out in holidays:
        out -= timedelta(days=1)
    return out


def safe_div_series(
    numerator: pd.Series,
    denominator: pd.Series,
    eps: float = SAFE_DIVIDE_MIN_ABS_DENOMINATOR,
) -> pd.Series:
    num = pd.to_numeric(numerator, errors="coerce")
    den = pd.to_numeric(denominator, errors="coerce")
    den = den.where(den.abs() >= float(eps), np.nan)
    out = num / den
    return out.where(np.isfinite(out), np.nan)


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

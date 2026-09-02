#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from time import perf_counter, sleep
from typing import Any

try:
    from datetime import UTC as utc_tz
except ImportError:  # pragma: no cover - Python < 3.11 compatibility.
    from datetime import timezone as _timezone

    utc_tz = _timezone.utc

import yaml

try:
    from .process_lock import WaitFileLock
except ImportError:  # Script execution from this directory.
    _RUNNER_DIR = Path(__file__).resolve().parent
    if str(_RUNNER_DIR) not in sys.path:
        sys.path.insert(0, str(_RUNNER_DIR))
    from process_lock import WaitFileLock

DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("config_sec_form4_orchestrator.yaml")
DEFAULT_SEC_SNAPSHOT_TABLE_CANDIDATES = (
    "sec_fundamental_snapshot_filled_security_t1_resolved",
    "sec_fundamental_snapshot_filled_security_t1",
)
DEFAULT_FORM4_SNAPSHOT_TABLE = "stock_signal_snapshot_tier1"
DEFAULT_SEC_FILING_DATE_CANDIDATES = (
    ("sec_filing_index", "filing_date"),
    ("sec_filing_index", "acceptance_datetime"),
    ("sec_xbrl_facts_raw", "filed_date"),
)
DEFAULT_FORM4_FILING_DATE_CANDIDATES = (
    ("sec_ownership_submission", "filing_date"),
    ("sec_form4_daily_ingest_log", "filing_date"),
)
DEFAULT_PROFILE_FALLBACK_DAYS = {
    "daily": {"sec": 7, "form4": 7},
    "weekly": {"sec": 30, "form4": 30},
    "quarterly": {"sec": 420, "form4": 120},
}
DATE_SCAN_FALLBACK_LIMIT = 5000
ISO_ASOF_GLOB = "????-??-??*"
YMD_ASOF_GLOB = "[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]"
SLASH_ASOF_GLOB = "[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]"
VALID_TARGETS = {"both", "sec", "form4"}
VALID_PROFILES = {"daily", "weekly", "quarterly"}
VALID_SEC_FETCH_MODES = {"daily", "weekly", "quarterly", "backfill"}
VALID_FORM4_FETCH_MODES = {"daily", "weekly"}
SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LEXICALLY_SORTABLE_DATE_RE = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}(?:[T ].*)?|\d{8}|\d{4}/\d{2}/\d{2})$"
)


def warn(message: str) -> None:
    print(f"WARNING: {message}", file=sys.stderr)


def parse_iso_date(value: str | None) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Invalid date '{value}'. Expected YYYY-MM-DD.") from exc


def parse_optional_config_date(raw_value: Any, *, config_key: str) -> date | None:
    try:
        return parse_iso_date(raw_value)
    except ValueError as exc:
        raise ValueError(f"{config_key} must be YYYY-MM-DD or null, got {raw_value!r}.") from exc


def to_iso(d: date) -> str:
    return d.isoformat()


def observed_fixed_holiday(year: int, month: int, day: int) -> date:
    holiday = date(year, month, day)
    if holiday.weekday() == 5:
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:
        return holiday + timedelta(days=1)
    return holiday


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    current = date(year, month, 1)
    days_until = (weekday - current.weekday()) % 7
    return current + timedelta(days=days_until + (n - 1) * 7)


def last_weekday(year: int, month: int, weekday: int) -> date:
    current = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return current - timedelta(days=(current.weekday() - weekday) % 7)


def easter_date(year: int) -> date:
    # Anonymous Gregorian algorithm.
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    weekday_offset = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * weekday_offset) // 451
    month = (h + weekday_offset - 7 * m + 114) // 31
    day = ((h + weekday_offset - 7 * m + 114) % 31) + 1
    return date(year, month, day)


@lru_cache(maxsize=8)
def us_market_holidays(year: int) -> frozenset[date]:
    holidays = {
        observed_fixed_holiday(year, 1, 1),
        nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        easter_date(year) - timedelta(days=2),  # Good Friday
        last_weekday(year, 5, 0),  # Memorial Day
        observed_fixed_holiday(year, 7, 4),
        nth_weekday(year, 9, 0, 1),  # Labor Day
        nth_weekday(year, 11, 3, 4),  # Thanksgiving
        observed_fixed_holiday(year, 12, 25),
    }
    if year >= 2022:
        holidays.add(observed_fixed_holiday(year, 6, 19))
    # Handles New Year's Day observed on Dec 31 of the prior calendar year.
    next_new_year_observed = observed_fixed_holiday(year + 1, 1, 1)
    if next_new_year_observed.year == year:
        holidays.add(next_new_year_observed)
    return frozenset(holidays)


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in us_market_holidays(d.year)


def previous_or_same_business_day(d: date) -> date:
    out = d
    while not is_business_day(out):
        out -= timedelta(days=1)
    return out


def cfg_get(cfg: dict[str, Any], *keys: str, default: Any = None) -> Any:
    cur: Any = cfg
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def coerce_str(raw_value: Any, *, default: str) -> str:
    if raw_value is None:
        return default
    text = str(raw_value).strip()
    return text or default


def load_yaml_map(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid YAML root in {path}. Expected a mapping.")
    if "sec_form4_orchestrator" in raw:
        if not isinstance(raw.get("sec_form4_orchestrator"), dict):
            raise ValueError(f"Invalid sec_form4_orchestrator section in {path}. Expected a mapping.")
        return raw["sec_form4_orchestrator"]
    return raw


def validate_sql_identifier(name: str, *, kind: str) -> str:
    text = str(name).strip()
    if not text or SQL_IDENTIFIER_RE.fullmatch(text) is None:
        raise ValueError(f"Invalid SQL {kind} identifier: {name!r}")
    return text


def resolve_repo_path(repo_root: Path, raw_value: str | None) -> Path | None:
    if not raw_value:
        return None
    p = Path(str(raw_value)).expanduser()
    if p.is_absolute():
        return p
    return (repo_root / p).resolve()


def resolve_config_relative_path(config_path: Path, raw_value: str | None) -> Path | None:
    if raw_value is None or not str(raw_value).strip():
        return None
    path = Path(str(raw_value)).expanduser()
    if path.is_absolute():
        return path
    return (config_path.parent / path).resolve()


def parse_optional_timeout(raw_value: Any, *, default: int | None) -> int | None:
    if raw_value is None:
        return default
    text = str(raw_value).strip()
    if not text:
        return default
    if text.lower() in {"none", "null"}:
        return None
    try:
        timeout_value = float(text)
        if not timeout_value.is_integer():
            raise ValueError
        timeout_seconds = int(timeout_value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(
            f"run.step_timeout_seconds must be integer seconds or null, got {raw_value!r}."
        ) from exc
    if timeout_seconds <= 0:
        raise ValueError("run.step_timeout_seconds must be > 0 or null.")
    return timeout_seconds


def parse_optional_int(raw_value: Any, *, config_key: str) -> int | None:
    if raw_value is None:
        return None
    if isinstance(raw_value, bool):
        raise ValueError(f"{config_key} must be an integer or null, got {raw_value!r}.")
    if isinstance(raw_value, int):
        return raw_value
    if isinstance(raw_value, float):
        if raw_value.is_integer():
            return int(raw_value)
        raise ValueError(f"{config_key} must be an integer or null, got {raw_value!r}.")
    text = str(raw_value).strip()
    if not text or text.lower() in {"none", "null"}:
        return None
    if re.fullmatch(r"[+-]?\d+", text):
        return int(text)
    if re.fullmatch(r"[+-]?\d+\.0+", text):
        return int(float(text))
    raise ValueError(f"{config_key} must be an integer or null, got {raw_value!r}.")


def parse_optional_int_range(
    raw_value: Any,
    *,
    config_key: str,
    min_value: int,
    max_value: int,
) -> int | None:
    out = parse_optional_int(raw_value, config_key=config_key)
    if out is not None and not (min_value <= out <= max_value):
        raise ValueError(f"{config_key} must be between {min_value} and {max_value}, got {out}.")
    return out


def parse_required_int(raw_value: Any, *, config_key: str, min_value: int | None = None) -> int:
    out = parse_optional_int(raw_value, config_key=config_key)
    if out is None:
        raise ValueError(f"{config_key} must be an integer, got null.")
    if min_value is not None and out < min_value:
        raise ValueError(f"{config_key} must be >= {min_value}, got {out}.")
    return out


def parse_table_column_candidates(
    raw_value: Any,
    *,
    default: tuple[tuple[str, str], ...],
    config_key: str,
) -> tuple[tuple[str, str], ...]:
    if raw_value is None:
        return default
    if not isinstance(raw_value, list):
        raise ValueError(f"{config_key} must be a list of table/column mappings.")

    out: list[tuple[str, str]] = []
    for idx, item in enumerate(raw_value):
        table_raw: Any
        column_raw: Any
        if isinstance(item, dict):
            table_raw = item.get("table")
            column_raw = item.get("column")
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            table_raw, column_raw = item
        else:
            raise ValueError(
                f"{config_key}[{idx}] must be {{table, column}} or a two-item list."
            )
        table_name = validate_sql_identifier(str(table_raw or ""), kind=f"{config_key}[{idx}].table")
        column_name = validate_sql_identifier(str(column_raw or ""), kind=f"{config_key}[{idx}].column")
        out.append((table_name, column_name))

    if not out:
        raise ValueError(f"{config_key} must contain at least one table/column candidate.")
    return tuple(out)


def parse_snapshot_table_candidates(
    raw_value: Any,
    *,
    default: tuple[str, ...],
    config_key: str,
) -> tuple[str, ...]:
    if raw_value is None:
        return default
    if not isinstance(raw_value, (list, tuple)):
        raise ValueError(f"{config_key} must be a list of SQL table names or null.")
    out: list[str] = []
    for idx, raw_candidate in enumerate(raw_value):
        if raw_candidate is None:
            continue
        if not isinstance(raw_candidate, str):
            raise ValueError(
                f"{config_key}[{idx}] must be a SQL table name string or null, got {raw_candidate!r}."
            )
        candidate = raw_candidate.strip()
        if candidate:
            out.append(validate_sql_identifier(candidate, kind=f"{config_key}[{idx}]"))
    return tuple(out) or default


def format_table_column_candidates(candidates: tuple[tuple[str, str], ...]) -> str:
    return ", ".join(f"{table}.{column}" for table, column in candidates)


def _terminate_process(proc: subprocess.Popen[Any]) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def run_step(
    cmd: list[str],
    *,
    dry_run: bool,
    timeout_seconds: int | None = None,
    heartbeat_seconds: int | None = 60,
) -> None:
    if len(cmd) >= 2 and Path(cmd[0]).name.lower().startswith("python"):
        step_name = Path(cmd[1]).name
    else:
        step_name = Path(cmd[0]).name
    started_at = datetime.now(utc_tz)
    print(f"Running [{started_at.strftime('%Y-%m-%dT%H:%M:%SZ')}]:", " ".join(cmd))
    if dry_run:
        return
    started = perf_counter()
    next_heartbeat = float(heartbeat_seconds or 0)
    try:
        proc = subprocess.Popen(cmd)
        while True:
            return_code = proc.poll()
            elapsed = perf_counter() - started
            if return_code is not None:
                if return_code != 0:
                    print(f"FAILED {step_name} after {elapsed:.1f}s.")
                    raise RuntimeError(
                        f"{step_name} failed with exit code {return_code}: {' '.join(cmd)}"
                    )
                break
            if timeout_seconds is not None and elapsed >= timeout_seconds:
                print(f"TIMEOUT {step_name} after {elapsed:.1f}s; terminating child process.")
                _terminate_process(proc)
                raise RuntimeError(
                    f"{step_name} timed out after {timeout_seconds}s: {' '.join(cmd)}"
                )
            if heartbeat_seconds and elapsed >= next_heartbeat:
                print(f"Still running {step_name}: elapsed={elapsed:.1f}s timeout={timeout_seconds}")
                next_heartbeat += heartbeat_seconds
            sleep(1.0)
    except OSError as exc:
        elapsed = perf_counter() - started
        print(f"OSERROR {step_name} after {elapsed:.1f}s.")
        raise RuntimeError(
            f"{step_name} failed ({type(exc).__name__}): {exc}. Command: {' '.join(cmd)}"
        ) from exc
    elapsed = perf_counter() - started
    print(f"Completed {step_name} in {elapsed:.1f}s.")


def sqlite_table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def fetch_existing_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if row and row[0] is not None
    }


def parse_db_date(text: str | None) -> date | None:
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    # Common formats seen in SEC/Form4 SQLite tables.
    base_candidates = [raw]
    if "T" in raw:
        base_candidates.append(raw.split("T", 1)[0])
    elif raw[:10] != raw:
        base_candidates.append(raw[:10])
    for candidate in base_candidates:
        if not candidate:
            continue
        try:
            dt = parse_iso_date(candidate)
        except ValueError:
            dt = None
        if dt is not None:
            return dt
    for fmt in ("%Y%m%d", "%Y/%m/%d", "%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    return None


def db_date_text_is_lexically_sortable(text: str | None) -> bool:
    """True when SQLite text MAX() has the same order as parsed dates."""
    raw = str(text or "").strip()
    if not raw:
        return False
    return bool(LEXICALLY_SORTABLE_DATE_RE.match(raw))


def max_parsed_date_from_conn(
    conn: sqlite3.Connection,
    *,
    table: str,
    column: str,
    context: str,
) -> date | None:
    """Scan distinct date strings and compare parsed dates.

    Form 4 tables may store dates as DD-MON-YYYY.  SQLite MAX() on those strings
    is lexical, so 31-DEC-2025 can incorrectly sort after 05-JUN-2026.  The
    distinct scan is only used for non-sortable date formats.
    """
    rows = conn.execute(
        f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL AND {column} <> ''"
    ).fetchall()
    best: date | None = None
    parsed_count = 0
    for row in rows:
        dt = parse_db_date(row[0] if row else None)
        if dt is None:
            continue
        parsed_count += 1
        if best is None or dt > best:
            best = dt
    if best is None and rows:
        warn(
            f"{context}: parsed-date scan found no valid dates in {table}.{column} "
            f"({len(rows)} distinct values). Incremental start may use fallback lookback."
        )
    elif parsed_count < len(rows):
        warn(
            f"{context}: ignored {len(rows) - parsed_count} unparseable distinct date values "
            f"while scanning {table}.{column}."
        )
    return best


def max_date_from_conn(
    conn: sqlite3.Connection,
    *,
    table: str,
    column: str,
    context: str,
    existing_tables: set[str] | None = None,
) -> date | None:
    table_name = validate_sql_identifier(table, kind="table")
    column_name = validate_sql_identifier(column, kind="column")
    try:
        if existing_tables is not None:
            table_exists = table_name in existing_tables
        else:
            table_exists = sqlite_table_exists(conn, table_name)
        if not table_exists:
            return None
        row = conn.execute(
            f"SELECT MAX({column_name}) FROM {table_name} "
            f"WHERE {column_name} IS NOT NULL AND {column_name} <> ''"
        ).fetchone()
        raw_max = row[0] if row else None
        fast = parse_db_date(raw_max)
        if fast is not None and db_date_text_is_lexically_sortable(raw_max):
            return fast
        if fast is not None:
            parsed_best = max_parsed_date_from_conn(
                conn,
                table=table_name,
                column=column_name,
                context=context,
            )
            if parsed_best is not None:
                if parsed_best != fast:
                    warn(
                        f"{context}: corrected non-sortable text MAX for {table_name}.{column_name}: "
                        f"sqlite_max={raw_max!r} parsed={fast.isoformat()} "
                        f"parsed_distinct_max={parsed_best.isoformat()}."
                    )
                return parsed_best
            return fast
        # Fallback: scan recent values if lexical MAX is non-date text.
        rows = conn.execute(
            f"SELECT {column_name} FROM {table_name} "
            f"WHERE {column_name} IS NOT NULL AND {column_name} <> '' "
            f"ORDER BY {column_name} DESC LIMIT {DATE_SCAN_FALLBACK_LIMIT}"
        ).fetchall()
        best: date | None = None
        for r in rows:
            dt = parse_db_date(r[0] if r else None)
            if dt is not None and (best is None or dt > best):
                best = dt
        if best is None and rows:
            warn(
                f"{context}: SQL MAX({column_name}) from {table_name} was not parseable and "
                f"fallback scan found no valid dates in {len(rows)} recent rows "
                f"(limit={DATE_SCAN_FALLBACK_LIMIT}). Incremental start may use fallback lookback."
            )
        elif best is not None and len(rows) >= DATE_SCAN_FALLBACK_LIMIT:
            warn(
                f"{context}: SQL MAX({column_name}) from {table_name} was not parseable; "
                f"fallback date came from a capped scan of {DATE_SCAN_FALLBACK_LIMIT} rows."
            )
        return best
    except sqlite3.Error as exc:
        warn(f"{context}: failed reading {table_name}.{column_name}: {exc}")
        return None


def max_date_from_table(
    db_path: Path,
    *,
    table: str,
    column: str,
    context: str,
) -> date | None:
    if not db_path.exists():
        return None
    if not db_path.is_file():
        warn(f"{context}: SQLite DB path is not a file: {db_path}")
        return None
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error as exc:
        warn(f"{context}: failed to open SQLite DB {db_path}: {exc}")
        return None
    try:
        existing_tables = fetch_existing_tables(conn)
        return max_date_from_conn(
            conn,
            table=table,
            column=column,
            context=context,
            existing_tables=existing_tables,
        )
    finally:
        conn.close()


def latest_sec_filing_date(
    db_path: Path,
    *,
    candidates: tuple[tuple[str, str], ...] = DEFAULT_SEC_FILING_DATE_CANDIDATES,
) -> date | None:
    if not db_path.exists():
        warn(f"SEC latest filing probe: DB not found: {db_path}")
        return None
    if not db_path.is_file():
        warn(f"SEC latest filing probe: DB path is not a file: {db_path}")
        return None
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error as exc:
        warn(f"SEC latest filing probe: failed to open SQLite DB {db_path}: {exc}")
        return None
    try:
        existing_tables = fetch_existing_tables(conn)
        best: date | None = None
        for table, col in candidates:
            dt = max_date_from_conn(
                conn,
                table=table,
                column=col,
                context="SEC latest filing probe",
                existing_tables=existing_tables,
            )
            if dt is not None and (best is None or dt > best):
                best = dt
    finally:
        conn.close()
    if best is None:
        warn(
            "SEC latest filing probe found no usable dates; "
            f"checked {format_table_column_candidates(candidates)} in {db_path}. "
            "Incremental start will use fallback lookback."
        )
    return best


def latest_form4_filing_date(
    db_path: Path,
    *,
    candidates: tuple[tuple[str, str], ...] = DEFAULT_FORM4_FILING_DATE_CANDIDATES,
) -> date | None:
    if not db_path.exists():
        warn(f"Form4 latest filing probe: DB not found: {db_path}")
        return None
    if not db_path.is_file():
        warn(f"Form4 latest filing probe: DB path is not a file: {db_path}")
        return None
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error as exc:
        warn(f"Form4 latest filing probe: failed to open SQLite DB {db_path}: {exc}")
        return None
    try:
        existing_tables = fetch_existing_tables(conn)
        best: date | None = None
        for table, col in candidates:
            dt = max_date_from_conn(
                conn,
                table=table,
                column=col,
                context="Form4 latest filing probe",
                existing_tables=existing_tables,
            )
            if dt is not None and (best is None or dt > best):
                best = dt
    finally:
        conn.close()
    if best is None:
        warn(
            "Form4 latest filing probe found no usable dates; "
            f"checked {format_table_column_candidates(candidates)} in {db_path}. "
            "Incremental start will use fallback lookback."
        )
    return best


def compute_incremental_start(
    *,
    latest_date: date | None,
    end_date: date,
    fallback_lookback_days_if_empty: int,
    incremental_from_latest: bool,
) -> date | None:
    if not incremental_from_latest:
        start = end_date - timedelta(days=max(fallback_lookback_days_if_empty, 1))
        return start
    if latest_date is None:
        start = end_date - timedelta(days=max(fallback_lookback_days_if_empty, 1))
        return start
    start = latest_date + timedelta(days=1)
    if start > end_date:
        return None
    return start


def load_sec_config(sec_cfg_path: Path) -> dict[str, Any]:
    raw = load_yaml_map(sec_cfg_path)
    if isinstance(raw.get("sec_fundamentals"), dict):
        return raw["sec_fundamentals"]
    return raw


def load_form4_config(form4_cfg_path: Path) -> dict[str, Any]:
    raw = load_yaml_map(form4_cfg_path)
    if isinstance(raw.get("sec_form4"), dict):
        return raw["sec_form4"]
    return raw


def run_sec_pipeline(
    *,
    py_exe: str,
    sec_pipeline_script: Path,
    sec_config_path: Path,
    sec_mode: str,
    as_of_date: date,
    start_date: date | None,
    end_date: date,
    quality_gate_override: bool,
    dry_run: bool,
    timeout_seconds: int | None,
) -> None:
    cmd = [
        py_exe,
        str(sec_pipeline_script),
        "--config",
        str(sec_config_path),
        "--mode",
        sec_mode,
        "--as-of-date",
        to_iso(as_of_date),
    ]
    if start_date is not None:
        cmd.extend(["--start-date", to_iso(start_date)])
    cmd.extend(["--end-date", to_iso(end_date)])
    if quality_gate_override:
        cmd.append("--quality-gate-override")
    run_step(cmd, dry_run=dry_run, timeout_seconds=timeout_seconds)


def run_form4_incremental_update(
    *,
    py_exe: str,
    form4_update_script: Path,
    form4_config_path: Path,
    form4_mode: str,
    start_date: date,
    end_date: date,
    reconcile_current_quarter: bool,
    max_index_days: int | None,
    max_filings: int | None,
    max_reconcile_filings: int | None,
    progress_every_filings: int | None,
    progress_interval_sec: int | None,
    stop_after_sec: int | None,
    dry_run: bool,
    timeout_seconds: int | None,
    heartbeat_seconds: int | None,
) -> None:
    cmd = [
        py_exe,
        str(form4_update_script),
        "--config",
        str(form4_config_path),
        "--mode",
        form4_mode,
        "--end-date",
        to_iso(end_date),
    ]
    cmd.extend(["--start-date", to_iso(start_date)])
    if reconcile_current_quarter:
        cmd.append("--reconcile-current-quarter")
    else:
        cmd.append("--no-reconcile-current-quarter")
    if max_index_days is not None:
        cmd.extend(["--max-index-days", str(max_index_days)])
    if max_filings is not None:
        cmd.extend(["--max-filings", str(max_filings)])
    if max_reconcile_filings is not None:
        cmd.extend(["--max-reconcile-filings", str(max_reconcile_filings)])
    if progress_every_filings is not None:
        cmd.extend(["--progress-every-filings", str(progress_every_filings)])
    if progress_interval_sec is not None:
        cmd.extend(["--progress-interval-sec", str(progress_interval_sec)])
    if stop_after_sec is not None:
        cmd.extend(["--stop-after-sec", str(stop_after_sec)])
    run_step(
        cmd,
        dry_run=dry_run,
        timeout_seconds=timeout_seconds,
        heartbeat_seconds=heartbeat_seconds,
    )


def run_form4_quarterly_backfill(
    *,
    py_exe: str,
    quarterly_script: Path,
    form4_config_path: Path,
    start_year: int | None,
    start_quarter: int | None,
    end_year: int | None,
    end_quarter: int | None,
    dry_run: bool,
    timeout_seconds: int | None,
) -> None:
    cmd = [py_exe, str(quarterly_script), "--config", str(form4_config_path)]
    if start_year is not None:
        cmd.extend(["--start-year", str(start_year)])
    if start_quarter is not None:
        cmd.extend(["--start-quarter", str(start_quarter)])
    if end_year is not None:
        cmd.extend(["--end-year", str(end_year)])
    if end_quarter is not None:
        cmd.extend(["--end-quarter", str(end_quarter)])
    run_step(cmd, dry_run=dry_run, timeout_seconds=timeout_seconds)


def run_sec_snapshot_history(
    *,
    py_exe: str,
    history_script: Path,
    sec_config_path: Path,
    cadence: str,
    end_date: date,
    start_date: date,
    quality_gate_override: bool,
    skip_existing: bool,
    dry_run: bool,
    timeout_seconds: int | None,
) -> None:
    cmd = [
        py_exe,
        str(history_script),
        "--config",
        str(sec_config_path),
        "--cadence",
        cadence,
        "--end-date",
        to_iso(end_date),
    ]
    cmd.append("--skip-existing" if skip_existing else "--no-skip-existing")
    if cadence in {"daily", "both"}:
        cmd.extend(["--daily-start-date", to_iso(start_date)])
    if cadence in {"weekly", "both"}:
        cmd.extend(["--weekly-start-date", to_iso(start_date)])
    if quality_gate_override:
        cmd.append("--quality-gate-override")
    run_step(cmd, dry_run=dry_run, timeout_seconds=timeout_seconds)


def run_form4_snapshot_history(
    *,
    py_exe: str,
    history_script: Path,
    form4_config_path: Path,
    cadence: str,
    end_date: date,
    start_date: date,
    run_reports_at_end: bool,
    skip_existing: bool,
    refresh_legacy_buy_table: bool,
    dry_run: bool,
    timeout_seconds: int | None,
) -> None:
    cmd = [
        py_exe,
        str(history_script),
        "--config",
        str(form4_config_path),
        "--cadence",
        cadence,
        "--end-date",
        to_iso(end_date),
    ]
    cmd.append("--skip-existing" if skip_existing else "--no-skip-existing")
    cmd.append("--refresh-legacy-buy-table" if refresh_legacy_buy_table else "--no-refresh-legacy-buy-table")
    if cadence in {"daily", "both"}:
        cmd.extend(["--daily-start-date", to_iso(start_date)])
    if cadence in {"weekly", "both"}:
        cmd.extend(["--weekly-start-date", to_iso(start_date)])
    cmd.append("--run-reports-at-end" if run_reports_at_end else "--no-run-reports-at-end")
    run_step(cmd, dry_run=dry_run, timeout_seconds=timeout_seconds)


def load_asof_set(
    db_path: Path,
    *,
    table_candidates: tuple[str, ...],
    start_date: date | None,
    end_date: date | None,
    business_days_only: bool,
    known_table_name: str | None = None,
) -> tuple[str, set[str]]:
    if not db_path.exists():
        warn(f"Alignment as_of load: DB not found: {db_path}")
        return "", set()
    if not db_path.is_file():
        warn(f"Alignment as_of load: DB path is not a file: {db_path}")
        return "", set()
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error as exc:
        warn(f"Alignment as_of load: failed to open SQLite DB {db_path}: {exc}")
        return "", set()
    try:
        if known_table_name:
            table_name = validate_sql_identifier(known_table_name, kind="table")
        else:
            table_name = ""
            existing_tables = fetch_existing_tables(conn)
            for candidate in table_candidates:
                candidate_name = validate_sql_identifier(candidate, kind="table")
                if candidate_name in existing_tables:
                    table_name = candidate_name
                    break
            if not table_name:
                warn(
                    "Alignment as_of load: no snapshot table found in "
                    f"{db_path}; checked {', '.join(table_candidates)}."
                )
                return "", set()
        rows: list[tuple[Any, ...]] = []

        def fetch_asof(where_parts: list[str], params: list[Any]) -> None:
            rows.extend(conn.execute(
                f"SELECT DISTINCT as_of_date FROM {table_name} WHERE " + " AND ".join(where_parts),
                params,
            ).fetchall())

        base_where = ["as_of_date IS NOT NULL", "as_of_date <> ''"]

        # Keep common fixed-width date formats range-filtered in SQL. Residual
        # legacy formats are still parsed and filtered below for correctness.
        iso_where = [*base_where, "as_of_date GLOB ?"]
        iso_params: list[Any] = [ISO_ASOF_GLOB]
        if start_date is not None:
            iso_where.append("as_of_date >= ?")
            iso_params.append(start_date.isoformat())
        if end_date is not None:
            iso_where.append("as_of_date < ?")
            iso_params.append((end_date + timedelta(days=1)).isoformat())
        fetch_asof(iso_where, iso_params)

        has_legacy_asof = conn.execute(
            f"SELECT 1 FROM {table_name} WHERE "
            + " AND ".join([*base_where, "as_of_date NOT GLOB ?"])
            + " LIMIT 1",
            [ISO_ASOF_GLOB],
        ).fetchone() is not None
        if has_legacy_asof:
            ymd_where = [*base_where, "as_of_date GLOB ?"]
            ymd_params: list[Any] = [YMD_ASOF_GLOB]
            if start_date is not None:
                ymd_where.append("as_of_date >= ?")
                ymd_params.append(start_date.strftime("%Y%m%d"))
            if end_date is not None:
                ymd_where.append("as_of_date <= ?")
                ymd_params.append(end_date.strftime("%Y%m%d"))
            fetch_asof(ymd_where, ymd_params)

            slash_where = [*base_where, "as_of_date GLOB ?"]
            slash_params: list[Any] = [SLASH_ASOF_GLOB]
            if start_date is not None:
                slash_where.append("as_of_date >= ?")
                slash_params.append(start_date.strftime("%Y/%m/%d"))
            if end_date is not None:
                slash_where.append("as_of_date <= ?")
                slash_params.append(end_date.strftime("%Y/%m/%d"))
            fetch_asof(slash_where, slash_params)

            fetch_asof(
                [
                    *base_where,
                    "as_of_date NOT GLOB ?",
                    "as_of_date NOT GLOB ?",
                    "as_of_date NOT GLOB ?",
                ],
                [ISO_ASOF_GLOB, YMD_ASOF_GLOB, SLASH_ASOF_GLOB],
            )
    except sqlite3.Error as exc:
        warn(f"Alignment as_of load: failed reading as_of_date from {db_path}: {exc}")
        return "", set()
    finally:
        conn.close()

    out: set[str] = set()
    for row in rows:
        raw = str(row[0]).strip() if row and row[0] is not None else ""
        dt = parse_db_date(raw)
        if dt is None:
            if raw:
                warn(f"Alignment as_of load: skipping unparseable as_of_date={raw!r} from {table_name}.")
            continue
        if start_date is not None and dt < start_date:
            continue
        if end_date is not None and dt > end_date:
            continue
        if business_days_only and not is_business_day(dt):
            continue
        out.add(dt.isoformat())
    return table_name, out


def contiguous_date_runs(dates: list[date]) -> list[tuple[date, date]]:
    ordered = sorted(set(dates))
    if not ordered:
        return []
    runs: list[tuple[date, date]] = []
    start = ordered[0]
    prev = ordered[0]
    for current in ordered[1:]:
        if (current - prev).days == 1:
            prev = current
            continue
        runs.append((start, prev))
        start = prev = current
    runs.append((start, prev))
    return runs


def parse_missing_asof_dates(values: list[str], *, context: str) -> list[date]:
    out: list[date] = []
    for raw in values:
        dt = parse_db_date(raw)
        if dt is None:
            warn(f"{context}: skipping unparseable missing as_of_date={raw!r}.")
            continue
        out.append(dt)
    return out


def fix_missing_sec_dates(
    *,
    py_exe: str,
    history_script: Path,
    sec_config_path: Path,
    missing_dates: list[date],
    quality_gate_override: bool,
    skip_existing: bool,
    dry_run: bool,
    timeout_seconds: int | None,
) -> None:
    for start_dt, end_dt in contiguous_date_runs(missing_dates):
        run_sec_snapshot_history(
            py_exe=py_exe,
            history_script=history_script,
            sec_config_path=sec_config_path,
            cadence="daily",
            end_date=end_dt,
            start_date=start_dt,
            quality_gate_override=quality_gate_override,
            skip_existing=skip_existing,
            dry_run=dry_run,
            timeout_seconds=timeout_seconds,
        )


def fix_missing_form4_dates(
    *,
    py_exe: str,
    history_script: Path,
    form4_config_path: Path,
    missing_dates: list[date],
    skip_existing: bool,
    refresh_legacy_buy_table: bool,
    dry_run: bool,
    timeout_seconds: int | None,
) -> None:
    for start_dt, end_dt in contiguous_date_runs(missing_dates):
        run_form4_snapshot_history(
            py_exe=py_exe,
            history_script=history_script,
            form4_config_path=form4_config_path,
            cadence="daily",
            end_date=end_dt,
            start_date=start_dt,
            run_reports_at_end=False,
            skip_existing=skip_existing,
            refresh_legacy_buy_table=refresh_legacy_buy_table,
            dry_run=dry_run,
            timeout_seconds=timeout_seconds,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Unified SEC fundamentals + Form 4 orchestrator. "
            "Supports incremental fetch from latest DB filing date to today and automatic as_of date-set alignment."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Orchestrator YAML path.")
    parser.add_argument("--target", choices=sorted(VALID_TARGETS), default=None, help="Override run target.")
    parser.add_argument("--profile", choices=sorted(VALID_PROFILES), default=None, help="Override run profile.")
    parser.add_argument("--as-of-date", type=str, default=None, help="Override end date YYYY-MM-DD.")
    parser.add_argument(
        "--skip-alignment",
        action="store_true",
        help="Skip as_of date-set alignment checks/fixes.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print commands and exit.")
    return parser.parse_args()


def parse_lock_float(
    raw_value: Any,
    *,
    config_key: str,
    default: float,
    allow_zero: bool,
) -> float:
    if raw_value is None or not str(raw_value).strip():
        value = float(default)
    else:
        if isinstance(raw_value, bool):
            raise ValueError(f"{config_key} must be numeric, got {raw_value!r}.")
        try:
            value = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{config_key} must be numeric, got {raw_value!r}.") from exc
    if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        comparator = ">= 0" if allow_zero else "> 0"
        raise ValueError(f"{config_key} must be finite and {comparator}, got {raw_value!r}.")
    return value


def writer_lock_settings(args: argparse.Namespace) -> tuple[Path | None, float, float]:
    """Resolve the shared Form 4 writer lock before any database access."""
    repo_root = Path(__file__).resolve().parent.parent
    orch_cfg_path = Path(args.config).expanduser()
    if not orch_cfg_path.is_absolute():
        orch_cfg_path = (Path.cwd() / orch_cfg_path).resolve()
    orch_cfg = load_yaml_map(orch_cfg_path)
    run_cfg = cfg_get(orch_cfg, "run", default={}) or {}
    target = str(args.target or cfg_get(run_cfg, "target", default="both")).strip().lower()
    dry_run = bool(args.dry_run or cfg_get(run_cfg, "dry_run", default=False))
    timeout_seconds = parse_lock_float(
        cfg_get(run_cfg, "form4_writer_lock_timeout_seconds", default=1800),
        config_key="run.form4_writer_lock_timeout_seconds",
        default=1800,
        allow_zero=True,
    )
    poll_seconds = parse_lock_float(
        cfg_get(run_cfg, "form4_writer_lock_poll_seconds", default=1),
        config_key="run.form4_writer_lock_poll_seconds",
        default=1,
        allow_zero=False,
    )
    if target not in {"both", "form4"} or dry_run:
        return None, timeout_seconds, poll_seconds

    form4_cfg = cfg_get(orch_cfg, "form4", default={}) or {}
    form4_config_path = resolve_repo_path(
        repo_root,
        cfg_get(form4_cfg, "config_path", default="config_sec_form4.yaml"),
    )
    if form4_config_path is None:
        raise ValueError("Missing form4.config_path in orchestrator config.")
    form4_runtime_cfg = load_form4_config(form4_config_path)
    form4_db_raw = cfg_get(form4_runtime_cfg, "db_path", default="")
    if not str(form4_db_raw).strip():
        raise ValueError("Missing sec_form4.db_path.")
    form4_db_path = resolve_config_relative_path(form4_config_path, form4_db_raw)
    if form4_db_path is None:
        raise ValueError("Failed to resolve Form4 db_path value.")

    configured_lock_path = cfg_get(run_cfg, "form4_writer_lock_path", default=None)
    if configured_lock_path is None or not str(configured_lock_path).strip():
        lock_path = form4_db_path.with_suffix(form4_db_path.suffix + ".writer.lock")
    else:
        lock_path = resolve_config_relative_path(orch_cfg_path, str(configured_lock_path))
        if lock_path is None:
            raise ValueError("Failed to resolve run.form4_writer_lock_path.")
    return lock_path, timeout_seconds, poll_seconds


def run(args: argparse.Namespace) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    orch_cfg_path = Path(args.config).expanduser()
    if not orch_cfg_path.is_absolute():
        orch_cfg_path = (Path.cwd() / orch_cfg_path).resolve()

    orch_cfg = load_yaml_map(orch_cfg_path)
    run_cfg = cfg_get(orch_cfg, "run", default={}) or {}
    target = str(args.target or cfg_get(run_cfg, "target", default="both")).strip().lower()
    profile = str(args.profile or cfg_get(run_cfg, "profile", default="daily")).strip().lower()
    if target not in VALID_TARGETS:
        raise ValueError(f"Invalid target={target!r}. Expected one of: {sorted(VALID_TARGETS)}")
    if profile not in VALID_PROFILES:
        raise ValueError(f"Invalid profile={profile!r}. Expected one of: {sorted(VALID_PROFILES)}")

    cli_end_date = parse_optional_config_date(args.as_of_date, config_key="--as-of-date")
    cfg_end_date = parse_optional_config_date(cfg_get(run_cfg, "as_of_date", default=None), config_key="run.as_of_date")
    end_date = cli_end_date or cfg_end_date
    if end_date is None:
        end_date = datetime.now(utc_tz).date()
    if bool(cfg_get(run_cfg, "business_day_adjust_as_of_date", default=True)):
        end_date = previous_or_same_business_day(end_date)
    today_utc = datetime.now(utc_tz).date()
    if end_date > today_utc:
        warn(
            f"as_of_date {to_iso(end_date)} is in the future relative to UTC today "
            f"{to_iso(today_utc)}; SEC/Form4 fetches may return no data."
        )

    incremental_from_latest = bool(cfg_get(run_cfg, "incremental_from_latest_filing", default=True))
    legacy_quality_gate_override = cfg_get(run_cfg, "quality_gate_override", default=None)
    if legacy_quality_gate_override:
        warn("run.quality_gate_override is deprecated; use run.sec_quality_gate_override.")
    run_sec_quality_gate_override = bool(
        cfg_get(run_cfg, "sec_quality_gate_override", default=bool(legacy_quality_gate_override or False))
    )
    dry_run = bool(args.dry_run or cfg_get(run_cfg, "dry_run", default=False))
    step_timeout_seconds = parse_optional_timeout(
        cfg_get(run_cfg, "step_timeout_seconds", default=86400),
        default=86400,
    )
    step_heartbeat_seconds = parse_optional_timeout(
        cfg_get(run_cfg, "step_heartbeat_seconds", default=60),
        default=60,
    )
    report_output_path = resolve_config_relative_path(
        orch_cfg_path,
        cfg_get(run_cfg, "report_output_path", default=str(Path("..") / "output" / "sec_form4_orchestrator_report.json")),
    )

    sec_cfg = cfg_get(orch_cfg, "sec", default={}) or {}
    form4_cfg = cfg_get(orch_cfg, "form4", default={}) or {}
    profile_cfg = cfg_get(orch_cfg, "profiles", profile, default={}) or {}
    alignment_cfg = cfg_get(orch_cfg, "alignment", default={}) or {}

    align_enabled = bool(cfg_get(alignment_cfg, "enabled", default=True)) and (not args.skip_alignment)
    sec_table: str | None = None
    form4_table_name: str | None = None
    sec_dates: set[str] | None = None
    form4_dates: set[str] | None = None
    missing_in_sec: list[str] | None = None
    missing_in_form4: list[str] | None = None

    if align_enabled:
        lookback_days = parse_required_int(
            cfg_get(alignment_cfg, "lookback_days", default=120),
            config_key="alignment.lookback_days",
            min_value=1,
        )
        align_start_default = end_date - timedelta(days=max(lookback_days, 1))
        align_start = (
            parse_optional_config_date(
                cfg_get(alignment_cfg, "start_date", default=None),
                config_key="alignment.start_date",
            )
            or align_start_default
        )
    else:
        align_start = None
    business_days_only = bool(cfg_get(alignment_cfg, "include_only_business_days", default=True))
    skip_existing_snapshots = bool(cfg_get(alignment_cfg, "skip_existing_snapshots", default=True))
    refresh_legacy_buy_table = bool(cfg_get(form4_cfg, "refresh_legacy_buy_table", default=False))
    auto_fix = bool(cfg_get(alignment_cfg, "auto_fix_missing_dates", default=True))
    fail_on_misalignment = bool(cfg_get(alignment_cfg, "fail_on_misalignment", default=True))

    sec_config_path = resolve_repo_path(
        repo_root, cfg_get(sec_cfg, "config_path", default=str(Path("fundamental_data") / "config_sec_fundamentals.yaml"))
    )
    form4_config_path = resolve_repo_path(
        repo_root, cfg_get(form4_cfg, "config_path", default="config_sec_form4.yaml")
    )
    if sec_config_path is None or form4_config_path is None:
        raise ValueError("Missing sec/form4 config paths in orchestrator config.")
    assert sec_config_path is not None
    assert form4_config_path is not None

    sec_pipeline_script = resolve_repo_path(
        repo_root, cfg_get(sec_cfg, "pipeline_script", default=str(Path("fundamental_data") / "run_sec_fundamentals_pipeline.py"))
    )
    sec_history_script = resolve_repo_path(
        repo_root, cfg_get(sec_cfg, "snapshot_history_script", default=str(Path("fundamental_data") / "run_sec_fundamental_snapshot_history.py"))
    )
    form4_update_script = resolve_repo_path(
        repo_root, cfg_get(form4_cfg, "update_script", default=str(Path("helper_scripts") / "update_sec_form4_daily.py"))
    )
    form4_history_script = resolve_repo_path(
        repo_root, cfg_get(form4_cfg, "snapshot_history_script", default=str(Path("helper_scripts") / "run_sec_form4_snapshot_history.py"))
    )
    form4_quarterly_script = resolve_repo_path(
        repo_root, cfg_get(form4_cfg, "quarterly_ingest_script", default=str(Path("helper_scripts") / "ingest_sec_insider_quarterly.py"))
    )
    if any(p is None for p in (sec_pipeline_script, sec_history_script, form4_update_script, form4_history_script, form4_quarterly_script)):
        raise ValueError("Failed to resolve one or more orchestrator scripts.")
    assert sec_pipeline_script is not None
    assert sec_history_script is not None
    assert form4_update_script is not None
    assert form4_history_script is not None
    assert form4_quarterly_script is not None
    required_script_paths: list[Path] = []
    if target in {"both", "sec"}:
        required_script_paths.extend([sec_pipeline_script, sec_history_script])
    if target in {"both", "form4"}:
        required_script_paths.extend([form4_update_script, form4_history_script, form4_quarterly_script])
    for script_path in required_script_paths:
        if not script_path.is_file():
            raise FileNotFoundError(f"Orchestrator script path is not a file: {script_path}")

    sec_runtime_cfg: dict[str, Any] = {}
    form4_runtime_cfg: dict[str, Any] = {}
    sec_db_path: Path | None = None
    form4_db_path: Path | None = None
    if target in {"both", "sec"}:
        sec_runtime_cfg = load_sec_config(sec_config_path)
        sec_db_raw = cfg_get(sec_runtime_cfg, "db_path", default="")
        if not str(sec_db_raw).strip():
            raise ValueError("Missing sec_fundamentals.db_path.")
        sec_db_path = resolve_config_relative_path(sec_config_path, sec_db_raw)
        if sec_db_path is None:
            raise ValueError("Failed to resolve SEC db_path value.")
    if target in {"both", "form4"}:
        form4_runtime_cfg = load_form4_config(form4_config_path)
        form4_db_raw = cfg_get(form4_runtime_cfg, "db_path", default="")
        if not str(form4_db_raw).strip():
            raise ValueError("Missing sec_form4.db_path.")
        form4_db_path = resolve_config_relative_path(form4_config_path, form4_db_raw)
        if form4_db_path is None:
            raise ValueError("Failed to resolve Form4 db_path value.")
    for label, db_path in (("SEC", sec_db_path), ("Form4", form4_db_path)):
        if db_path is None:
            continue
        if db_path.exists() and not db_path.is_file():
            raise FileNotFoundError(f"{label} database path is not a file: {db_path}")
        if not dry_run and not db_path.exists():
            warn(
                f"{label} database not found: {db_path}. Treating it as empty; "
                "incremental windows will use fallback lookback until the DB exists."
            )

    sec_mode = str(cfg_get(profile_cfg, "sec_fetch_mode", default="daily")).strip().lower()
    form4_mode = str(cfg_get(profile_cfg, "form4_fetch_mode", default="daily")).strip().lower()
    reconcile_cadence = str(cfg_get(profile_cfg, "reconciliation_cadence", default="daily")).strip().lower()
    if sec_mode not in VALID_SEC_FETCH_MODES:
        raise ValueError(
            "profiles.<name>.sec_fetch_mode must be one of: "
            f"{', '.join(sorted(VALID_SEC_FETCH_MODES))}; got {sec_mode!r}"
        )
    if form4_mode not in VALID_FORM4_FETCH_MODES:
        raise ValueError(
            "profiles.<name>.form4_fetch_mode must be one of: "
            f"{', '.join(sorted(VALID_FORM4_FETCH_MODES))}; got {form4_mode!r}"
        )
    if reconcile_cadence not in {"daily", "weekly", "both"}:
        raise ValueError("profiles.<name>.reconciliation_cadence must be one of: daily, weekly, both")

    fallback_defaults = DEFAULT_PROFILE_FALLBACK_DAYS.get(profile, DEFAULT_PROFILE_FALLBACK_DAYS["daily"])
    sec_fallback_days = parse_required_int(
        cfg_get(profile_cfg, "fallback_lookback_days_if_empty", "sec", default=fallback_defaults["sec"]),
        config_key=f"profiles.{profile}.fallback_lookback_days_if_empty.sec",
        min_value=1,
    )
    form4_fallback_days = parse_required_int(
        cfg_get(profile_cfg, "fallback_lookback_days_if_empty", "form4", default=fallback_defaults["form4"]),
        config_key=f"profiles.{profile}.fallback_lookback_days_if_empty.form4",
        min_value=1,
    )
    if not incremental_from_latest:
        warn(
            "run.incremental_from_latest_filing=false; using fallback lookback windows "
            f"sec={sec_fallback_days} days, form4={form4_fallback_days} days."
        )
    form4_reconcile_current_quarter = bool(cfg_get(profile_cfg, "form4_reconcile_current_quarter", default=False))
    form4_max_index_days = parse_optional_int(
        cfg_get(profile_cfg, "form4_max_index_days", default=None),
        config_key=f"profiles.{profile}.form4_max_index_days",
    )
    form4_max_filings = parse_optional_int(
        cfg_get(profile_cfg, "form4_max_filings", default=None),
        config_key=f"profiles.{profile}.form4_max_filings",
    )
    form4_max_reconcile_filings = parse_optional_int(
        cfg_get(profile_cfg, "form4_max_reconcile_filings", default=None),
        config_key=f"profiles.{profile}.form4_max_reconcile_filings",
    )
    form4_progress_every_filings = parse_optional_int(
        cfg_get(profile_cfg, "form4_progress_every_filings", default=None),
        config_key=f"profiles.{profile}.form4_progress_every_filings",
    )
    form4_progress_interval_sec = parse_optional_int(
        cfg_get(profile_cfg, "form4_progress_interval_sec", default=None),
        config_key=f"profiles.{profile}.form4_progress_interval_sec",
    )
    form4_stop_after_sec = parse_optional_int(
        cfg_get(profile_cfg, "form4_stop_after_sec", default=None),
        config_key=f"profiles.{profile}.form4_stop_after_sec",
    )
    for config_key, value in {
        f"profiles.{profile}.form4_max_index_days": form4_max_index_days,
        f"profiles.{profile}.form4_max_filings": form4_max_filings,
        f"profiles.{profile}.form4_max_reconcile_filings": form4_max_reconcile_filings,
        f"profiles.{profile}.form4_progress_every_filings": form4_progress_every_filings,
        f"profiles.{profile}.form4_progress_interval_sec": form4_progress_interval_sec,
        f"profiles.{profile}.form4_stop_after_sec": form4_stop_after_sec,
    }.items():
        if value is not None and value < 0:
            raise ValueError(f"{config_key} must be >= 0 or null, got {value!r}")
    run_form4_quarterly_backfill_enabled = bool(
        cfg_get(profile_cfg, "run_form4_quarterly_backfill", default=False)
    )
    form4_quarterly_start_year = parse_optional_int(
        cfg_get(profile_cfg, "form4_quarterly_start_year", default=None),
        config_key=f"profiles.{profile}.form4_quarterly_start_year",
    )
    form4_quarterly_start_quarter = parse_optional_int_range(
        cfg_get(profile_cfg, "form4_quarterly_start_quarter", default=None),
        config_key=f"profiles.{profile}.form4_quarterly_start_quarter",
        min_value=1,
        max_value=4,
    )
    form4_quarterly_end_year = parse_optional_int(
        cfg_get(profile_cfg, "form4_quarterly_end_year", default=None),
        config_key=f"profiles.{profile}.form4_quarterly_end_year",
    )
    form4_quarterly_end_quarter = parse_optional_int_range(
        cfg_get(profile_cfg, "form4_quarterly_end_quarter", default=None),
        config_key=f"profiles.{profile}.form4_quarterly_end_quarter",
        min_value=1,
        max_value=4,
    )
    if (form4_quarterly_start_year is None) != (form4_quarterly_start_quarter is None):
        raise ValueError(
            f"profiles.{profile}.form4_quarterly_start_year and "
            f"profiles.{profile}.form4_quarterly_start_quarter must both be set or both be null."
        )
    if (form4_quarterly_end_year is None) != (form4_quarterly_end_quarter is None):
        raise ValueError(
            f"profiles.{profile}.form4_quarterly_end_year and "
            f"profiles.{profile}.form4_quarterly_end_quarter must both be set or both be null."
        )
    run_form4_reports = bool(cfg_get(profile_cfg, "run_form4_reports", default=False))
    sec_quality_gate_override = (
        bool(cfg_get(profile_cfg, "sec_quality_gate_override", default=False))
        or run_sec_quality_gate_override
    )

    py_exe = sys.executable
    sec_filing_date_candidates = parse_table_column_candidates(
        cfg_get(sec_cfg, "latest_filing_date_candidates", default=None),
        default=DEFAULT_SEC_FILING_DATE_CANDIDATES,
        config_key="sec.latest_filing_date_candidates",
    )
    form4_filing_date_candidates = parse_table_column_candidates(
        cfg_get(form4_cfg, "latest_filing_date_candidates", default=None),
        default=DEFAULT_FORM4_FILING_DATE_CANDIDATES,
        config_key="form4.latest_filing_date_candidates",
    )
    if dry_run:
        print("[dry-run] Skipping DB filing date probes.")
        sec_latest = None
        form4_latest = None
    else:
        sec_latest = (
            latest_sec_filing_date(sec_db_path, candidates=sec_filing_date_candidates)
            if sec_db_path is not None and sec_db_path.is_file()
            else None
        )
        form4_latest = (
            latest_form4_filing_date(form4_db_path, candidates=form4_filing_date_candidates)
            if form4_db_path is not None and form4_db_path.is_file()
            else None
        )
    sec_start = compute_incremental_start(
        latest_date=sec_latest,
        end_date=end_date,
        fallback_lookback_days_if_empty=sec_fallback_days,
        incremental_from_latest=incremental_from_latest,
    )
    form4_start = compute_incremental_start(
        latest_date=form4_latest,
        end_date=end_date,
        fallback_lookback_days_if_empty=form4_fallback_days,
        incremental_from_latest=incremental_from_latest,
    )

    print(f"Target: {target} | Profile: {profile} | End date: {to_iso(end_date)}")
    print(f"SEC latest filing in DB: {to_iso(sec_latest) if sec_latest else 'none'}")
    print(f"Form4 latest filing in DB: {to_iso(form4_latest) if form4_latest else 'none'}")
    print(f"SEC incremental window: {to_iso(sec_start)} -> {to_iso(end_date)}" if sec_start else "SEC incremental window: no new filing dates")
    print(
        f"Form4 incremental window: {to_iso(form4_start)} -> {to_iso(end_date)}"
        if form4_start
        else "Form4 incremental window: no new filing dates"
    )

    if target in {"both", "sec"} and sec_start is not None:
        run_sec_pipeline(
            py_exe=py_exe,
            sec_pipeline_script=sec_pipeline_script,
            sec_config_path=sec_config_path,
            sec_mode=sec_mode,
            as_of_date=end_date,
            start_date=sec_start,
            end_date=end_date,
            quality_gate_override=sec_quality_gate_override,
            dry_run=dry_run,
            timeout_seconds=step_timeout_seconds,
        )
    elif target in {"both", "sec"}:
        print("Skipping SEC ingest/fetch: no new dates to pull.")

    if target in {"both", "form4"}:
        if run_form4_quarterly_backfill_enabled:
            run_form4_quarterly_backfill(
                py_exe=py_exe,
                quarterly_script=form4_quarterly_script,
                form4_config_path=form4_config_path,
                start_year=form4_quarterly_start_year,
                start_quarter=form4_quarterly_start_quarter,
                end_year=form4_quarterly_end_year,
                end_quarter=form4_quarterly_end_quarter,
                dry_run=dry_run,
                timeout_seconds=step_timeout_seconds,
            )
        if form4_start is not None:
            run_form4_incremental_update(
                py_exe=py_exe,
                form4_update_script=form4_update_script,
                form4_config_path=form4_config_path,
                form4_mode=form4_mode,
                start_date=form4_start,
                end_date=end_date,
                reconcile_current_quarter=form4_reconcile_current_quarter,
                max_index_days=form4_max_index_days,
                max_filings=form4_max_filings,
                max_reconcile_filings=form4_max_reconcile_filings,
                progress_every_filings=form4_progress_every_filings,
                progress_interval_sec=form4_progress_interval_sec,
                stop_after_sec=form4_stop_after_sec,
                dry_run=dry_run,
                timeout_seconds=step_timeout_seconds,
                heartbeat_seconds=step_heartbeat_seconds,
            )
        else:
            print("Skipping Form4 ingest/fetch: no new dates to pull.")

    if align_enabled:
        assert align_start is not None
        if target in {"both", "sec"}:
            run_sec_snapshot_history(
                py_exe=py_exe,
                history_script=sec_history_script,
                sec_config_path=sec_config_path,
                cadence=reconcile_cadence,
                end_date=end_date,
                start_date=align_start,
                quality_gate_override=sec_quality_gate_override,
                skip_existing=skip_existing_snapshots,
                dry_run=dry_run,
                timeout_seconds=step_timeout_seconds,
            )
        if target in {"both", "form4"}:
            run_form4_snapshot_history(
                py_exe=py_exe,
                history_script=form4_history_script,
                form4_config_path=form4_config_path,
                cadence=reconcile_cadence,
                end_date=end_date,
                start_date=align_start,
                run_reports_at_end=run_form4_reports,
                skip_existing=skip_existing_snapshots,
                refresh_legacy_buy_table=refresh_legacy_buy_table,
                dry_run=dry_run,
                timeout_seconds=step_timeout_seconds,
            )

        if dry_run:
            print("[dry-run] Skipping alignment DB date-set load/check.")
            sec_table = None
            form4_table_name = None
            sec_dates = None
            form4_dates = None
            missing_in_sec = None
            missing_in_form4 = None
            relevant_missing_in_sec = []
            relevant_missing_in_form4 = []
            misaligned = False
        else:
            sec_table = None
            form4_table_name = None
            sec_dates = set()
            form4_dates = set()
            if target in {"both", "sec"}:
                sec_table_candidates = parse_snapshot_table_candidates(
                    cfg_get(sec_cfg, "snapshot_table_candidates", default=None),
                    default=DEFAULT_SEC_SNAPSHOT_TABLE_CANDIDATES,
                    config_key="sec.snapshot_table_candidates",
                )
                assert sec_db_path is not None
                sec_table, sec_dates = load_asof_set(
                    sec_db_path,
                    table_candidates=sec_table_candidates,
                    start_date=align_start,
                    end_date=end_date,
                    business_days_only=business_days_only,
                )
            if target in {"both", "form4"}:
                form4_table = coerce_str(
                    cfg_get(form4_cfg, "snapshot_table", default=DEFAULT_FORM4_SNAPSHOT_TABLE),
                    default=DEFAULT_FORM4_SNAPSHOT_TABLE,
                )
                assert form4_db_path is not None
                form4_table_name, form4_dates = load_asof_set(
                    form4_db_path,
                    table_candidates=(form4_table,),
                    start_date=align_start,
                    end_date=end_date,
                    business_days_only=business_days_only,
                )
            missing_in_sec = sorted(form4_dates - sec_dates)
            missing_in_form4 = sorted(sec_dates - form4_dates)

            if auto_fix:
                fixed_sec = False
                fixed_form4 = False
                if missing_in_sec and target in {"both", "sec"}:
                    fix_missing_sec_dates(
                        py_exe=py_exe,
                        history_script=sec_history_script,
                        sec_config_path=sec_config_path,
                        missing_dates=parse_missing_asof_dates(missing_in_sec, context="SEC auto-fix"),
                        quality_gate_override=sec_quality_gate_override,
                        skip_existing=skip_existing_snapshots,
                        dry_run=dry_run,
                        timeout_seconds=step_timeout_seconds,
                    )
                    fixed_sec = True
                if missing_in_form4 and target in {"both", "form4"}:
                    fix_missing_form4_dates(
                        py_exe=py_exe,
                        history_script=form4_history_script,
                        form4_config_path=form4_config_path,
                        missing_dates=parse_missing_asof_dates(missing_in_form4, context="Form4 auto-fix"),
                        skip_existing=skip_existing_snapshots,
                        refresh_legacy_buy_table=refresh_legacy_buy_table,
                        dry_run=dry_run,
                        timeout_seconds=step_timeout_seconds,
                    )
                    fixed_form4 = True

                if fixed_sec:
                    assert sec_db_path is not None
                    sec_table_candidates = parse_snapshot_table_candidates(
                        cfg_get(sec_cfg, "snapshot_table_candidates", default=None),
                        default=DEFAULT_SEC_SNAPSHOT_TABLE_CANDIDATES,
                        config_key="sec.snapshot_table_candidates",
                    )
                    sec_table, sec_dates = load_asof_set(
                        sec_db_path,
                        table_candidates=sec_table_candidates,
                        start_date=align_start,
                        end_date=end_date,
                        business_days_only=business_days_only,
                        known_table_name=sec_table or None,
                    )
                if fixed_form4:
                    assert form4_db_path is not None
                    form4_table = coerce_str(
                        cfg_get(form4_cfg, "snapshot_table", default=DEFAULT_FORM4_SNAPSHOT_TABLE),
                        default=DEFAULT_FORM4_SNAPSHOT_TABLE,
                    )
                    form4_table_name, form4_dates = load_asof_set(
                        form4_db_path,
                        table_candidates=(form4_table,),
                        start_date=align_start,
                        end_date=end_date,
                        business_days_only=business_days_only,
                        known_table_name=form4_table_name or None,
                    )
                if fixed_sec or fixed_form4:
                    missing_in_sec = sorted(form4_dates - sec_dates)
                    missing_in_form4 = sorted(sec_dates - form4_dates)

            relevant_missing_in_sec = missing_in_sec if target in {"both", "sec"} else []
            relevant_missing_in_form4 = missing_in_form4 if target in {"both", "form4"} else []
            print(
                "Alignment check:"
                f" sec_table={sec_table or 'missing'} ({len(sec_dates)} dates),"
                f" form4_table={form4_table_name or 'missing'} ({len(form4_dates)} dates),"
                f" missing_in_sec={len(missing_in_sec)} (relevant={len(relevant_missing_in_sec)}),"
                f" missing_in_form4={len(missing_in_form4)} (relevant={len(relevant_missing_in_form4)})"
            )

            misaligned = bool(relevant_missing_in_sec or relevant_missing_in_form4)
        if misaligned and fail_on_misalignment:
            raise RuntimeError(
                "SEC/Form4 as_of date-set misalignment remains after auto-fix. "
                f"missing_in_sec={len(relevant_missing_in_sec)} "
                f"missing_in_form4={len(relevant_missing_in_form4)}"
            )
    else:
        sec_table = None
        form4_table_name = None
        sec_dates = None
        form4_dates = None
        missing_in_sec = None
        missing_in_form4 = None

    report = {
        "run_utc": datetime.now(utc_tz).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "target": target,
        "profile": profile,
        "end_date": to_iso(end_date),
        "dry_run": dry_run,
        "step_timeout_seconds": step_timeout_seconds,
        "step_heartbeat_seconds": step_heartbeat_seconds,
        "incremental_from_latest_filing": incremental_from_latest,
        "sec_latest_filing_date_before_run": to_iso(sec_latest) if sec_latest else None,
        "form4_latest_filing_date_before_run": to_iso(form4_latest) if form4_latest else None,
        "sec_start_date": to_iso(sec_start) if sec_start else None,
        "form4_start_date": to_iso(form4_start) if form4_start else None,
        "form4_reconcile_current_quarter": form4_reconcile_current_quarter,
        "form4_max_index_days": form4_max_index_days,
        "form4_max_filings": form4_max_filings,
        "form4_max_reconcile_filings": form4_max_reconcile_filings,
        "form4_progress_every_filings": form4_progress_every_filings,
        "form4_progress_interval_sec": form4_progress_interval_sec,
        "form4_stop_after_sec": form4_stop_after_sec,
        "alignment_enabled": align_enabled,
        "alignment_start_date": to_iso(align_start) if align_start is not None else None,
        "sec_snapshot_table": sec_table,
        "form4_snapshot_table": form4_table_name,
        "sec_asof_count": len(sec_dates) if sec_dates is not None else None,
        "form4_asof_count": len(form4_dates) if form4_dates is not None else None,
        "missing_in_sec_count": len(missing_in_sec) if missing_in_sec is not None else None,
        "missing_in_form4_count": len(missing_in_form4) if missing_in_form4 is not None else None,
        "missing_in_sec_sample": missing_in_sec[:20] if missing_in_sec is not None else None,
        "missing_in_form4_sample": missing_in_form4[:20] if missing_in_form4 is not None else None,
    }

    if report_output_path is not None:
        try:
            report_output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(report_output_path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, ensure_ascii=True)
            print(f"Saved orchestrator report: {report_output_path}")
        except OSError as exc:
            warn(f"Failed to write orchestrator report {report_output_path}: {exc}")

    print("SEC/Form4 orchestrator completed.")


def main() -> None:
    args = parse_args()
    lock_path, timeout_seconds, poll_seconds = writer_lock_settings(args)
    if lock_path is None:
        run(args)
        return
    print(
        f"Waiting for shared Form4 writer lock: {lock_path} "
        f"(timeout={timeout_seconds:.0f}s)"
    )
    with WaitFileLock(
        lock_path,
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
        owner="sec_form4_orchestrator",
    ):
        print(f"Acquired shared Form4 writer lock: {lock_path}")
        run(args)


if __name__ == "__main__":
    main()

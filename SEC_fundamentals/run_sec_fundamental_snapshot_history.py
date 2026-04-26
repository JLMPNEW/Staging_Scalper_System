#!/usr/bin/env python3
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
import logging
import os
import stat
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from sec_fundamentals_config import (
    _us_federal_holidays,
    cfg_get,
    configure_pipeline_logging,
    load_sec_fundamentals_config,
    parse_iso_date,
    previous_or_same_business_day,
    validate_sql_identifier,
)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("config_sec_fundamentals.yaml")
DEFAULT_DB_PATH = Path(r"C:\Users\josel\Documents\PROD\DB\sec_fundamentals.sqlite")
DEFAULT_SNAPSHOT_TABLE_CANDIDATES = (
    "sec_fundamental_snapshot_filled_security_t1_resolved",
    "sec_fundamental_snapshot_filled_security_t1",
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SnapshotTableSettings:
    strict_table: str
    filled_table: str
    security_strict_table: str
    security_filled_table: str
    run_table: str


@dataclass(frozen=True)
class ResolverRunSettings:
    snapshot_table: str
    facts_table: str
    output_table: str
    candidate_table: str | None
    extension_yaml: Path | None
    applicability_yaml: Path | None
    issuer_override_csv: Path | None
    candidate_csv: Path | None
    missing_tickers_csv: Path | None
    prior_enabled: bool
    prior_max_days: int


@dataclass(frozen=True)
class StagedDateArtifacts:
    as_of_date: date
    ordinal: int
    artifact_dir: Path
    builder_artifact_dir: Path
    resolver_artifact_dir: Path | None


def _connect_sqlite(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def _to_sqlite_timestamp(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _prepare_sqlite_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in out.columns:
        s = out[col]
        if pd.api.types.is_datetime64_any_dtype(s):
            out[col] = pd.to_datetime(s, errors="coerce").map(
                lambda v: None if pd.isna(v) else v.isoformat()
            )
            continue
        if s.dtype == object and s.map(lambda v: isinstance(v, (pd.Timestamp, datetime))).any():
            out[col] = s.map(_to_sqlite_timestamp)
    return out


def _quote_sqlite_identifier(identifier: str) -> str:
    return '"' + str(identifier).replace('"', '""') + '"'


def _format_sqlite_table_name(table_name: str) -> str:
    return ".".join(_quote_sqlite_identifier(part) for part in table_name.split("."))


def _sqlite_column_type(series: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(series) or pd.api.types.is_integer_dtype(series):
        return "INTEGER"
    if pd.api.types.is_float_dtype(series):
        return "REAL"
    return "TEXT"


def _sqlite_scalar(value: object) -> object:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, bytes, bytearray)):
        return value
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except Exception:
            return value
    return value


def _create_table_for_frame(conn: sqlite3.Connection, table_name: str, df: pd.DataFrame) -> None:
    if df.empty and not list(df.columns):
        return
    sql_table_name = _format_sqlite_table_name(table_name)
    columns_sql = ", ".join(
        f"{_quote_sqlite_identifier(str(col))} {_sqlite_column_type(df[col])}"
        for col in df.columns
    )
    conn.execute(f"CREATE TABLE IF NOT EXISTS {sql_table_name} ({columns_sql})")


def _insert_frame_rows(conn: sqlite3.Connection, table_name: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    columns = [str(col) for col in df.columns]
    sql_table_name = _format_sqlite_table_name(table_name)
    columns_sql = ", ".join(_quote_sqlite_identifier(col) for col in columns)
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO {sql_table_name} ({columns_sql}) VALUES ({placeholders})"
    rows = [
        tuple(_sqlite_scalar(value) for value in row)
        for row in df.itertuples(index=False, name=None)
    ]
    conn.executemany(sql, rows)


def _upsert_asof_df(
    conn: sqlite3.Connection,
    table_name: str,
    as_of_date: str,
    df: pd.DataFrame,
) -> None:
    table_name = validate_sql_identifier(table_name, "table_name", allow_dotted=True)
    sql_table_name = _format_sqlite_table_name(table_name)
    write_df = _prepare_sqlite_frame(df)
    dedupe_candidates = [
        ["as_of_date", "ticker", "cik", "metric_name", "taxonomy", "concept_name"],
        ["as_of_date", "ticker", "cik", "accession_number", "report_period_end"],
        ["as_of_date", "ticker", "cik", "accession_number"],
        ["as_of_date", "ticker", "cik"],
        ["as_of_date", "cik", "accession_number"],
        ["as_of_date"],
    ]
    for subset in dedupe_candidates:
        if all(col in write_df.columns for col in subset):
            write_df = write_df.drop_duplicates(subset=subset, keep="last")
            break
    write_df = write_df.drop_duplicates(keep="last")
    table_exists = _table_exists(conn, table_name)
    if not table_exists:
        if write_df.empty:
            return
        _create_table_for_frame(conn, table_name, write_df)
    else:
        conn.execute(f"DELETE FROM {sql_table_name} WHERE as_of_date = ?", (as_of_date,))
    if not write_df.empty:
        _insert_frame_rows(conn, table_name, write_df)
    return


def _resolve_snapshot_table_settings(cfg: dict) -> SnapshotTableSettings:
    snap_cfg = cfg_get(cfg, "snapshot_enhanced", default={})
    if not isinstance(snap_cfg, dict):
        snap_cfg = {}
    return SnapshotTableSettings(
        strict_table=validate_sql_identifier(
            str(cfg_get(snap_cfg, "strict_table", default="sec_fundamental_snapshot_strict_t1")),
            "snapshot_enhanced.strict_table",
            allow_dotted=True,
        ),
        filled_table=validate_sql_identifier(
            str(cfg_get(snap_cfg, "filled_table", default="sec_fundamental_snapshot_filled_t1")),
            "snapshot_enhanced.filled_table",
            allow_dotted=True,
        ),
        security_strict_table=validate_sql_identifier(
            str(cfg_get(snap_cfg, "security_strict_table", default="sec_fundamental_snapshot_strict_security_t1")),
            "snapshot_enhanced.security_strict_table",
            allow_dotted=True,
        ),
        security_filled_table=validate_sql_identifier(
            str(cfg_get(snap_cfg, "security_filled_table", default="sec_fundamental_snapshot_filled_security_t1")),
            "snapshot_enhanced.security_filled_table",
            allow_dotted=True,
        ),
        run_table=validate_sql_identifier(
            str(cfg_get(snap_cfg, "run_table", default="sec_fundamental_snapshot_run_t1")),
            "snapshot_enhanced.run_table",
            allow_dotted=True,
        ),
    )


def _resolve_resolver_run_settings(
    *,
    config_path: Path,
    cfg: dict,
) -> ResolverRunSettings | None:
    resolver_cfg = cfg_get(cfg, "snapshot_resolver", default={})
    if not isinstance(resolver_cfg, dict) or not bool(cfg_get(resolver_cfg, "enabled", default=True)):
        return None

    snap_cfg = cfg_get(cfg, "snapshot_enhanced", default={})
    if not isinstance(snap_cfg, dict):
        snap_cfg = {}

    snapshot_table = validate_sql_identifier(
        str(
            cfg_get(
                resolver_cfg,
                "snapshot_table",
                default=str(cfg_get(snap_cfg, "security_filled_table", default="sec_fundamental_snapshot_filled_security_t1")),
            )
        ),
        "resolver snapshot_table",
        allow_dotted=True,
    )
    facts_table = validate_sql_identifier(
        str(cfg_get(resolver_cfg, "facts_table", default="sec_xbrl_facts_raw")),
        "resolver facts_table",
        allow_dotted=True,
    )
    output_table = validate_sql_identifier(
        str(cfg_get(resolver_cfg, "output_table", default=f"{snapshot_table}_resolved")),
        "resolver output_table",
        allow_dotted=True,
    )
    candidate_table_raw = cfg_get(resolver_cfg, "candidate_table", default=None)
    candidate_table = (
        validate_sql_identifier(str(candidate_table_raw), "resolver candidate_table", allow_dotted=True)
        if candidate_table_raw
        else None
    )
    return ResolverRunSettings(
        snapshot_table=snapshot_table,
        facts_table=facts_table,
        output_table=output_table,
        candidate_table=candidate_table,
        extension_yaml=_resolve_path(
            config_path,
            cfg_get(
                resolver_cfg,
                "extension_rule_yaml",
                default=str(Path("fundamental_data") / "sec_extension_pattern_library.yaml"),
            ),
        ),
        applicability_yaml=_resolve_path(
            config_path,
            cfg_get(
                resolver_cfg,
                "applicability_yaml",
                default=str(Path("fundamental_data") / "sec_metric_applicability_policy.yaml"),
            ),
        ),
        issuer_override_csv=_resolve_path(
            config_path,
            cfg_get(
                resolver_cfg,
                "issuer_override_csv",
                default=str(Path("fundamental_data") / "sec_issuer_metric_override_seed.csv"),
            ),
        ),
        candidate_csv=_resolve_path(
            config_path,
            cfg_get(resolver_cfg, "candidate_csv", default=str(Path("output") / "sec_gap_candidates_latest.csv")),
        ),
        missing_tickers_csv=_resolve_path(
            config_path,
            cfg_get(
                resolver_cfg,
                "missing_tickers_csv",
                default=str(Path("output") / "sec_missing_metrics_tickers.csv"),
            ),
        ),
        prior_enabled=bool(cfg_get(resolver_cfg, "prior_filing_fallback_enabled", default=True)),
        prior_max_days=int(cfg_get(resolver_cfg, "prior_filing_max_staleness_days", default=550)),
    )


def bdays_between(start: date, end: date) -> list[date]:
    holidays = _us_federal_holidays(start.year, end.year + 1)
    out: list[date] = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5 and cur not in holidays:
            out.append(cur)
        cur += timedelta(days=1)
    return out


def weekly_fridays_between(start: date, end: date) -> list[date]:
    out: list[date] = []
    cur = start
    while cur.weekday() != 4:
        cur += timedelta(days=1)
    while cur <= end:
        normalized = previous_or_same_business_day(cur)
        if not out or out[-1] != normalized:
            out.append(normalized)
        cur += timedelta(days=7)
    return out


def normalize_asof_dates(dates: list[date]) -> list[date]:
    """Normalize selected dates to effective business dates and remove duplicates."""
    out: list[date] = []
    seen: set[date] = set()
    adjusted = 0
    duplicates = 0
    for raw_date in dates:
        effective_date = previous_or_same_business_day(raw_date)
        if effective_date != raw_date:
            adjusted += 1
        if effective_date in seen:
            duplicates += 1
            continue
        seen.add(effective_date)
        out.append(effective_date)
    if adjusted or duplicates:
        logger.info(
            "Normalized selected as_of dates: adjusted_non_business=%d dropped_duplicates=%d",
            adjusted,
            duplicates,
        )
    return out


def subtract_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year - years)
    except ValueError:
        # Handle leap-day rollover.
        return d.replace(month=2, day=28, year=d.year - years)


def load_existing_asof_dates(
    db_path: Path,
    snapshot_tables: SnapshotTableSettings | None = None,
) -> set[str]:
    if not db_path.exists():
        return set()
    conn = _connect_sqlite(db_path)
    try:
        table_name = ""
        if snapshot_tables is None:
            candidates = DEFAULT_SNAPSHOT_TABLE_CANDIDATES
        else:
            candidates = tuple(
                dict.fromkeys(
                    [
                        snapshot_tables.security_filled_table,
                        snapshot_tables.security_strict_table,
                        snapshot_tables.filled_table,
                        snapshot_tables.strict_table,
                        snapshot_tables.run_table,
                    ]
                )
            )
        for candidate in candidates:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
                (candidate,),
            ).fetchone()
            if row:
                table_name = candidate
                break
        if not table_name:
            return set()
        rows = conn.execute(f"SELECT DISTINCT as_of_date FROM {table_name}").fetchall()
    except sqlite3.OperationalError:
        return set()
    finally:
        conn.close()
    return {str(row[0]) for row in rows if row[0]}


def load_period_asof_dates(
    db_path: Path,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[date]:
    if not db_path.exists():
        return []
    conn = _connect_sqlite(db_path)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sec_fundamental_period_t1' LIMIT 1"
        ).fetchone()
        if not exists:
            return []
        where_parts: list[str] = []
        params: list[str] = []
        if start_date is not None:
            where_parts.append("as_of_date >= ?")
            params.append(start_date.isoformat())
        if end_date is not None:
            where_parts.append("as_of_date <= ?")
            params.append(end_date.isoformat())
        where_sql = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""
        rows = conn.execute(
            f"SELECT DISTINCT as_of_date FROM sec_fundamental_period_t1{where_sql} ORDER BY as_of_date",
            params,
        ).fetchall()
    finally:
        conn.close()
    out: list[date] = []
    for row in rows:
        dt = parse_iso_date(str(row[0]) if row and row[0] else None)
        if dt is not None:
            out.append(dt)
    return out


def _resolve_path(config_path: Path, raw_value: str | None) -> Path | None:
    if not raw_value:
        return None
    path = Path(str(raw_value))
    if path.is_absolute():
        return path
    return (config_path.parent.parent / path).resolve()


def _resolve_db_path(
    *,
    config_path: Path,
    cfg: dict,
    db_path_override: Path | None,
) -> Path:
    raw_value = db_path_override if db_path_override is not None else cfg_get(cfg, "db_path", default=str(DEFAULT_DB_PATH))
    path = Path(raw_value).expanduser()
    if path.is_absolute():
        return path
    if db_path_override is not None:
        return (Path.cwd() / path).resolve()
    return (config_path.parent.parent / path).resolve()


def _remove_tree(path: Path, *, retries: int = 5, delay_seconds: float = 0.5) -> bool:
    def _handle_remove_readonly(func: object, target: str, _exc_info: object) -> None:
        try:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
            func(target)
        except Exception:
            pass

    if not path.exists():
        return True
    for attempt in range(retries):
        try:
            shutil.rmtree(path, onerror=_handle_remove_readonly)
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            if attempt >= retries - 1:
                logger.warning("Failed to remove staging path %s after %d attempts: %s", path, retries, exc)
                return False
            time.sleep(delay_seconds * float(attempt + 1))
    return not path.exists()


def run_build_for_date(
    py_exe: str,
    build_script: Path,
    config_path: Path,
    db_path: Path,
    as_of_date: date,
    quality_gate_override: bool = False,
    *,
    no_persist: bool = False,
    artifact_dir: Path | None = None,
) -> None:
    cmd = [
        py_exe,
        str(build_script),
        "--config",
        str(config_path),
        "--db-path",
        str(db_path),
        "--as-of-date",
        as_of_date.isoformat(),
    ]
    if no_persist:
        cmd.append("--no-persist")
    if artifact_dir is not None:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        cmd.extend(["--artifact-dir", str(artifact_dir)])
    if quality_gate_override:
        cmd.append("--quality-gate-override")
    subprocess.run(cmd, check=True)


def run_resolver_for_date(
    *,
    py_exe: str,
    config_path: Path,
    cfg: dict,
    db_path: Path,
    as_of_date: date,
    emit_side_outputs: bool = True,
    persist: bool = True,
    snapshot_csv: Path | None = None,
    candidate_csv_override: Path | None = None,
    missing_tickers_csv_override: Path | None = None,
    resolved_csv: Path | None = None,
    summary_csv: Path | None = None,
) -> None:
    resolver_settings = _resolve_resolver_run_settings(config_path=config_path, cfg=cfg)
    if resolver_settings is None:
        return

    resolver_script = Path(__file__).resolve().with_name("sec_snapshot_gap_resolver.py")

    db_url = f"sqlite:///{db_path.as_posix()}"
    cmd = [
        py_exe,
        str(resolver_script),
        "--db-url",
        db_url,
        "--snapshot-table",
        resolver_settings.snapshot_table,
        "--facts-table",
        resolver_settings.facts_table,
        "--output-table",
        resolver_settings.output_table,
        "--as-of-date",
        as_of_date.isoformat(),
        "--prior-filing-fallback-enabled",
        "true" if resolver_settings.prior_enabled else "false",
        "--prior-filing-max-staleness-days",
        str(resolver_settings.prior_max_days),
    ]
    if persist:
        cmd.append("--persist")
    if snapshot_csv is not None:
        cmd.extend(["--snapshot-csv", str(snapshot_csv)])
    if persist and resolver_settings.candidate_table:
        cmd.extend(["--candidate-table", resolver_settings.candidate_table])
    if resolver_settings.extension_yaml is not None and resolver_settings.extension_yaml.exists():
        cmd.extend(["--extension-rule-yaml", str(resolver_settings.extension_yaml)])
    if resolver_settings.applicability_yaml is not None and resolver_settings.applicability_yaml.exists():
        cmd.extend(["--applicability-yaml", str(resolver_settings.applicability_yaml)])
    if resolver_settings.issuer_override_csv is not None and resolver_settings.issuer_override_csv.exists():
        cmd.extend(["--issuer-override-csv", str(resolver_settings.issuer_override_csv)])
    candidate_csv = candidate_csv_override or (resolver_settings.candidate_csv if emit_side_outputs else None)
    missing_tickers_csv = missing_tickers_csv_override or (
        resolver_settings.missing_tickers_csv if emit_side_outputs else None
    )
    if candidate_csv is not None:
        candidate_csv.parent.mkdir(parents=True, exist_ok=True)
        cmd.extend(["--candidate-csv", str(candidate_csv)])
    if missing_tickers_csv is not None:
        missing_tickers_csv.parent.mkdir(parents=True, exist_ok=True)
        cmd.extend(["--missing-tickers-csv", str(missing_tickers_csv)])
    if resolved_csv is not None:
        resolved_csv.parent.mkdir(parents=True, exist_ok=True)
        cmd.extend(["--resolved-csv", str(resolved_csv)])
    if summary_csv is not None:
        summary_csv.parent.mkdir(parents=True, exist_ok=True)
        cmd.extend(["--summary-csv", str(summary_csv)])
    subprocess.run(cmd, check=True)


def run_export_for_date(
    *,
    py_exe: str,
    config_path: Path,
    db_path: Path,
    as_of_date: date,
) -> None:
    export_script = Path(__file__).resolve().with_name("export_sec_fundamentals_for_pipeline.py")
    cmd = [
        py_exe,
        str(export_script),
        "--config",
        str(config_path),
        "--db-path",
        str(db_path),
        "--as-of-date",
        as_of_date.isoformat(),
    ]
    subprocess.run(cmd, check=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build historical SEC feature snapshots by as_of_date. "
            "Daily cadence uses business days; weekly cadence uses Fridays."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to fundamentals YAML config.",
    )
    parser.add_argument("--db-path", type=Path, default=None, help="Override fundamentals SQLite DB path.")
    parser.add_argument(
        "--cadence",
        choices=["daily", "weekly", "both"],
        default="both",
        help="Snapshot cadence to build.",
    )
    parser.add_argument("--end-date", type=str, default=None, help="End date YYYY-MM-DD (default: today UTC).")
    parser.add_argument(
        "--daily-start-date",
        type=str,
        default=None,
        help="Daily cadence start date YYYY-MM-DD (overrides --daily-lookback-days).",
    )
    parser.add_argument(
        "--weekly-start-date",
        type=str,
        default=None,
        help="Weekly cadence start date YYYY-MM-DD (overrides --weekly-lookback-years).",
    )
    parser.add_argument(
        "--daily-lookback-days",
        type=int,
        default=30,
        help="Daily cadence business-day lookback when --daily-start-date is not provided.",
    )
    parser.add_argument(
        "--weekly-lookback-years",
        type=int,
        default=None,
        help=(
            "Weekly cadence lookback years when --weekly-start-date is not provided. "
            "Defaults to sec_fundamentals.backfill_years."
        ),
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help=(
            "Skip as_of dates already present in resolved snapshot table "
            "(fallback to filled table if resolved table is unavailable)."
        ),
    )
    parser.add_argument(
        "--max-dates",
        type=int,
        default=0,
        help="Optional cap on number of dates to build after filtering (0 = no cap).",
    )
    parser.add_argument(
        "--date-order",
        choices=["newest", "oldest"],
        default="newest",
        help="Build newest dates first or oldest dates first.",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue building remaining dates if one date fails.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print planned dates and exit.")
    parser.add_argument(
        "--skip-resolver",
        action="store_true",
        help="Skip snapshot gap resolver step for each as_of_date.",
    )
    parser.add_argument(
        "--skip-export",
        action="store_true",
        help="Skip Yahoo-compatible export after the history run.",
    )
    parser.add_argument(
        "--quality-gate-override",
        action="store_true",
        help="Pass --quality-gate-override to each snapshot build date.",
    )
    parser.add_argument(
        "--use-period-asof-dates",
        action="store_true",
        help=(
            "Use distinct as_of_date values from sec_fundamental_period_t1 as the build list "
            "instead of calendar-derived daily/weekly dates."
        ),
    )
    parser.add_argument(
        "--period-start-date",
        type=str,
        default=None,
        help="Optional start date filter (YYYY-MM-DD) when --use-period-asof-dates is set.",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=1,
        help="Parallel worker cap for per-date build/resolver subprocesses (1 = serial).",
    )
    return parser.parse_args()


def _read_csv_artifact(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Expected staged artifact not found: {path}")
    return pd.read_csv(path)


def _persist_staged_date_artifacts(
    *,
    db_path: Path,
    as_of_date: date,
    artifacts: StagedDateArtifacts,
    snapshot_tables: SnapshotTableSettings,
    resolver_settings: ResolverRunSettings | None,
) -> None:
    as_of_text = as_of_date.isoformat()
    conn = _connect_sqlite(db_path)
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        _upsert_asof_df(
            conn,
            snapshot_tables.strict_table,
            as_of_text,
            _read_csv_artifact(artifacts.builder_artifact_dir / "entity_strict.csv"),
        )
        _upsert_asof_df(
            conn,
            snapshot_tables.filled_table,
            as_of_text,
            _read_csv_artifact(artifacts.builder_artifact_dir / "entity_filled.csv"),
        )
        _upsert_asof_df(
            conn,
            snapshot_tables.security_strict_table,
            as_of_text,
            _read_csv_artifact(artifacts.builder_artifact_dir / "security_strict.csv"),
        )
        _upsert_asof_df(
            conn,
            snapshot_tables.security_filled_table,
            as_of_text,
            _read_csv_artifact(artifacts.builder_artifact_dir / "security_filled.csv"),
        )
        _upsert_asof_df(
            conn,
            snapshot_tables.run_table,
            as_of_text,
            _read_csv_artifact(artifacts.builder_artifact_dir / "run_row.csv"),
        )

        if resolver_settings is not None and artifacts.resolver_artifact_dir is not None:
            _upsert_asof_df(
                conn,
                resolver_settings.output_table,
                as_of_text,
                _read_csv_artifact(artifacts.resolver_artifact_dir / "resolved.csv"),
            )
            if resolver_settings.candidate_table:
                _upsert_asof_df(
                    conn,
                    resolver_settings.candidate_table,
                    as_of_text,
                    _read_csv_artifact(artifacts.resolver_artifact_dir / "candidate.csv"),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _copy_file(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _create_staging_root(logs_dir: Path) -> Path:
    stem = f"sec_snapshot_stage_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"
    candidate = logs_dir / stem
    suffix = 1
    while candidate.exists():
        candidate = logs_dir / f"{stem}_{suffix}"
        suffix += 1
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def _publish_resolver_side_outputs(
    *,
    staged_resolver_dir: Path | None,
    resolver_settings: ResolverRunSettings | None,
) -> bool:
    if staged_resolver_dir is None or resolver_settings is None:
        return False
    wrote_any = False
    if resolver_settings.candidate_csv is not None:
        wrote_any = _copy_file(
            staged_resolver_dir / "candidate.csv",
            resolver_settings.candidate_csv,
        ) or wrote_any
    if resolver_settings.missing_tickers_csv is not None:
        wrote_any = _copy_file(
            staged_resolver_dir / "missing_tickers.csv",
            resolver_settings.missing_tickers_csv,
        ) or wrote_any
    return wrote_any


def _run_single_date_staged(
    *,
    py_exe: str,
    build_script: Path,
    config_path: Path,
    cfg: dict,
    db_path: Path,
    as_of_date: date,
    ordinal: int,
    staging_root: Path,
    quality_gate_override: bool,
    skip_resolver: bool,
) -> StagedDateArtifacts:
    requested_as_of_date = as_of_date
    as_of_date = previous_or_same_business_day(as_of_date)
    if as_of_date != requested_as_of_date:
        logger.info(
            "Adjusted non-business as_of_date %s to %s.",
            requested_as_of_date.isoformat(),
            as_of_date.isoformat(),
        )
    artifact_dir = staging_root / as_of_date.isoformat()
    if artifact_dir.exists():
        _remove_tree(artifact_dir)
    builder_artifact_dir = artifact_dir / "builder"
    resolver_artifact_dir = artifact_dir / "resolver"
    run_build_for_date(
        py_exe=py_exe,
        build_script=build_script,
        config_path=config_path,
        db_path=db_path,
        as_of_date=as_of_date,
        quality_gate_override=quality_gate_override,
        no_persist=True,
        artifact_dir=builder_artifact_dir,
    )
    resolver_dir: Path | None = None
    if not skip_resolver:
        run_resolver_for_date(
            py_exe=py_exe,
            config_path=config_path,
            cfg=cfg,
            db_path=db_path,
            as_of_date=as_of_date,
            emit_side_outputs=True,
            persist=False,
            snapshot_csv=builder_artifact_dir / "security_filled.csv",
            candidate_csv_override=resolver_artifact_dir / "candidate.csv",
            missing_tickers_csv_override=resolver_artifact_dir / "missing_tickers.csv",
            resolved_csv=resolver_artifact_dir / "resolved.csv",
            summary_csv=resolver_artifact_dir / "summary.csv",
        )
        resolver_dir = resolver_artifact_dir
    return StagedDateArtifacts(
        as_of_date=as_of_date,
        ordinal=ordinal,
        artifact_dir=artifact_dir,
        builder_artifact_dir=builder_artifact_dir,
        resolver_artifact_dir=resolver_dir,
    )


def _run_single_date(
    *,
    py_exe: str,
    build_script: Path,
    config_path: Path,
    cfg: dict,
    db_path: Path,
    as_of_date: date,
    quality_gate_override: bool,
    skip_resolver: bool,
    emit_resolver_side_outputs: bool,
) -> date:
    requested_as_of_date = as_of_date
    as_of_date = previous_or_same_business_day(as_of_date)
    if as_of_date != requested_as_of_date:
        logger.info(
            "Adjusted non-business as_of_date %s to %s.",
            requested_as_of_date.isoformat(),
            as_of_date.isoformat(),
        )
    run_build_for_date(
        py_exe=py_exe,
        build_script=build_script,
        config_path=config_path,
        db_path=db_path,
        as_of_date=as_of_date,
        quality_gate_override=quality_gate_override,
    )
    if not skip_resolver:
        run_resolver_for_date(
            py_exe=py_exe,
            config_path=config_path,
            cfg=cfg,
            db_path=db_path,
            as_of_date=as_of_date,
            emit_side_outputs=emit_resolver_side_outputs,
        )
    return as_of_date


def _is_single_date_subprocess_failure(exc: BaseException) -> bool:
    return isinstance(exc, (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError))


def _format_single_date_failure(exc: BaseException) -> str:
    exc_type = type(exc).__name__
    if isinstance(exc, subprocess.CalledProcessError):
        return f"{exc_type} exit={exc.returncode}"
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"{exc_type} timeout={exc.timeout}"
    return f"{exc_type}: {exc}"


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    _, cfg = load_sec_fundamentals_config(args.config)

    db_path = _resolve_db_path(config_path=args.config, cfg=cfg, db_path_override=args.db_path)
    end_date = parse_iso_date(args.end_date)
    if args.end_date and end_date is None:
        raise ValueError(f"Invalid --end-date: {args.end_date!r}")
    end_date = end_date or datetime.now(timezone.utc).date()
    end_date = previous_or_same_business_day(end_date)

    backfill_years = int(cfg_get(cfg, "backfill_years", default=7))
    weekly_years = int(args.weekly_lookback_years if args.weekly_lookback_years is not None else backfill_years)

    if args.use_period_asof_dates:
        period_start = parse_iso_date(args.period_start_date)
        if args.period_start_date and period_start is None:
            raise ValueError(f"Invalid --period-start-date: {args.period_start_date!r}")
        all_dates = load_period_asof_dates(
            db_path,
            start_date=period_start,
            end_date=end_date,
        )
    else:
        dates_daily: list[date] = []
        dates_weekly: list[date] = []
        if args.cadence in {"daily", "both"}:
            if args.daily_start_date:
                daily_start = parse_iso_date(args.daily_start_date)
                if daily_start is None:
                    raise ValueError(f"Invalid --daily-start-date: {args.daily_start_date!r}")
            else:
                daily_start = end_date - timedelta(days=max(int(args.daily_lookback_days), 1) * 2)
            # Keep only business days; this guarantees enough points across weekends/holidays.
            daily_candidates = bdays_between(daily_start, end_date)
            dates_daily = daily_candidates[-max(int(args.daily_lookback_days), 1) :]

        if args.cadence in {"weekly", "both"}:
            if args.weekly_start_date:
                weekly_start = parse_iso_date(args.weekly_start_date)
                if weekly_start is None:
                    raise ValueError(f"Invalid --weekly-start-date: {args.weekly_start_date!r}")
            else:
                weekly_start = subtract_years(end_date, max(weekly_years, 1))
            dates_weekly = weekly_fridays_between(weekly_start, end_date)

        all_dates = sorted(set(dates_daily + dates_weekly))
    if not all_dates:
        logger.info("No as_of dates selected. Nothing to run.")
        return
    all_dates = normalize_asof_dates(all_dates)
    if not all_dates:
        logger.info("No effective business as_of dates selected. Nothing to run.")
        return

    snapshot_tables = _resolve_snapshot_table_settings(cfg)
    resolver_settings = _resolve_resolver_run_settings(config_path=args.config, cfg=cfg)

    if args.skip_existing:
        existing = load_existing_asof_dates(db_path, snapshot_tables=snapshot_tables)
        all_dates = [d for d in all_dates if d.isoformat() not in existing]

    if args.max_dates and args.max_dates > 0:
        if args.date_order == "newest":
            all_dates = all_dates[-args.max_dates :]
        else:
            all_dates = all_dates[: args.max_dates]

    if args.date_order == "newest":
        all_dates = sorted(all_dates, reverse=True)
    else:
        all_dates = sorted(all_dates)

    if not all_dates:
        logger.info("All selected as_of dates already exist. Nothing to run.")
        return

    oldest = min(all_dates)
    newest = max(all_dates)
    logger.info("DB path: %s", db_path)
    logger.info("Total as_of dates queued: %s", f"{len(all_dates):,}")
    logger.info("Range: %s -> %s", oldest.isoformat(), newest.isoformat())
    if args.dry_run:
        return

    py_exe = sys.executable
    build_script = Path(__file__).resolve().with_name("build_sec_tier1_snapshot_enhanced.py")
    max_workers = max(1, int(args.max_workers))
    ok = 0
    failed: list[str] = []
    succeeded: set[str] = set()

    if max_workers == 1:
        for i, d in enumerate(all_dates, start=1):
            logger.info("[%d/%d] Building as_of_date=%s ...", i, len(all_dates), d.isoformat())
            try:
                completed_date = _run_single_date(
                    py_exe=py_exe,
                    build_script=build_script,
                    config_path=args.config,
                    cfg=cfg,
                    db_path=db_path,
                    as_of_date=d,
                    quality_gate_override=bool(args.quality_gate_override),
                    skip_resolver=bool(args.skip_resolver),
                    emit_resolver_side_outputs=True,
                )
                ok += 1
                succeeded.add(completed_date.isoformat())
            except Exception as exc:
                if not _is_single_date_subprocess_failure(exc):
                    raise
                failed.append(d.isoformat())
                logger.warning("FAILED for %s (%s)", d.isoformat(), _format_single_date_failure(exc))
                if not args.continue_on_error:
                    break
    else:
        logger.info(
            "Parallel history mode enabled: max_workers=%d. "
            "Worker dates stage artifacts only; SQLite imports stay serialized in the parent process.",
            max_workers,
        )
        total = len(all_dates)
        next_idx = 0
        stop_submitting = False
        future_to_date: dict[Future[object], date] = {}
        logs_dir = Path(__file__).resolve().with_name("logs")
        logs_dir.mkdir(parents=True, exist_ok=True)
        staging_root = _create_staging_root(logs_dir)
        latest_side_output_dir = staging_root / "_latest_side_outputs"
        best_side_output_date: date | None = None
        logger.info("Staging root: %s", staging_root)

        def _submit(executor: ThreadPoolExecutor, d: date, ordinal: int) -> None:
            logger.info("[%d/%d] Dispatching as_of_date=%s ...", ordinal, total, d.isoformat())
            fut = executor.submit(
                _run_single_date_staged,
                py_exe=py_exe,
                build_script=build_script,
                config_path=args.config,
                cfg=cfg,
                db_path=db_path,
                as_of_date=d,
                ordinal=ordinal,
                staging_root=staging_root,
                quality_gate_override=bool(args.quality_gate_override),
                skip_resolver=bool(args.skip_resolver),
            )
            future_to_date[fut] = d

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            while next_idx < len(all_dates) and len(future_to_date) < max_workers:
                _submit(executor, all_dates[next_idx], next_idx + 1)
                next_idx += 1

            while future_to_date:
                done, _ = wait(set(future_to_date.keys()), return_when=FIRST_COMPLETED)
                for fut in done:
                    d = future_to_date.pop(fut)
                    try:
                        artifacts = fut.result()
                    except Exception as exc:
                        if not _is_single_date_subprocess_failure(exc):
                            raise
                        failed.append(d.isoformat())
                        logger.warning("FAILED for %s (%s)", d.isoformat(), _format_single_date_failure(exc))
                        if not args.continue_on_error:
                            stop_submitting = True
                    else:
                        completed_date = artifacts.as_of_date
                        try:
                            _persist_staged_date_artifacts(
                                db_path=db_path,
                                as_of_date=completed_date,
                                artifacts=artifacts,
                                snapshot_tables=snapshot_tables,
                                resolver_settings=None if args.skip_resolver else resolver_settings,
                            )
                        except Exception as exc:
                            failed.append(completed_date.isoformat())
                            logger.warning(
                                "FAILED import for %s (%s)",
                                completed_date.isoformat(),
                                f"{type(exc).__name__}: {exc}",
                            )
                            logger.warning(
                                "Retained staged artifacts for %s at %s",
                                completed_date.isoformat(),
                                artifacts.artifact_dir,
                            )
                            if not args.continue_on_error:
                                stop_submitting = True
                        else:
                            ok += 1
                            succeeded.add(completed_date.isoformat())
                            logger.info("COMPLETED %s", completed_date.isoformat())
                            if (
                                not args.skip_resolver
                                and artifacts.resolver_artifact_dir is not None
                                and (best_side_output_date is None or completed_date > best_side_output_date)
                            ):
                                best_side_output_date = completed_date
                                _copy_file(
                                    artifacts.resolver_artifact_dir / "candidate.csv",
                                    latest_side_output_dir / "candidate.csv",
                                )
                                _copy_file(
                                    artifacts.resolver_artifact_dir / "missing_tickers.csv",
                                    latest_side_output_dir / "missing_tickers.csv",
                                )
                            _remove_tree(artifacts.artifact_dir)
                    if not stop_submitting and next_idx < len(all_dates):
                        _submit(executor, all_dates[next_idx], next_idx + 1)
                        next_idx += 1

    logger.info("Completed builds: %s", f"{ok:,}")
    logger.info("Failed builds: %s", f"{len(failed):,}")
    if failed:
        logger.warning("Failed dates:")
        for d in failed:
            logger.warning("  - %s", d)
    last_success_date = max((d for d in all_dates if d.isoformat() in succeeded), default=None)
    if max_workers > 1:
        if (not args.skip_resolver) and resolver_settings is not None and last_success_date is not None:
            if not _publish_resolver_side_outputs(
                staged_resolver_dir=latest_side_output_dir if latest_side_output_dir.exists() else None,
                resolver_settings=resolver_settings,
            ):
                logger.info(
                    "Regenerating resolver side-output artifacts for final successful as_of_date=%s ...",
                    last_success_date.isoformat(),
                )
                run_resolver_for_date(
                    py_exe=py_exe,
                    config_path=args.config,
                    cfg=cfg,
                    db_path=db_path,
                    as_of_date=last_success_date,
                    emit_side_outputs=True,
                    persist=False,
                )
        if not failed:
            _remove_tree(staging_root)
        else:
            logger.warning("Retained staged artifacts at %s for debugging.", staging_root)
    if (not args.skip_export) and last_success_date is not None:
        run_export_for_date(
            py_exe=py_exe,
            config_path=args.config,
            db_path=db_path,
            as_of_date=last_success_date,
        )
        logger.info("Exported Yahoo-compatible snapshot for as_of_date=%s", last_success_date.isoformat())


if __name__ == "__main__":
    main()

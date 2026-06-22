from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SEVERITIES = frozenset({"info", "warning", "error", "critical"})


# Stage 0 foundation schema. Contract/score/risk tables are added by later stages.
SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_type TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    input_path TEXT,
    row_count INTEGER,
    message TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS data_quality_issues (
    issue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage TEXT NOT NULL,
    source_id TEXT,
    issue_type TEXT NOT NULL,
    detail TEXT,
    severity TEXT NOT NULL DEFAULT 'warning',
    created_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def connect(db_path: Path, *, timeout_sec: float = 30.0) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=float(timeout_sec))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {int(float(timeout_sec) * 1000)}")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(SCHEMA_SQL)


def table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [str(row["name"]) for row in rows]


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    if not SAFE_IDENTIFIER_RE.match(table_name):
        raise ValueError(f"Unsafe table name: {table_name}")
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def count_rows(conn: sqlite3.Connection, table_name: str) -> int:
    if not SAFE_IDENTIFIER_RE.match(table_name):
        raise ValueError(f"Unsafe table name: {table_name}")
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table_name}").fetchone()
    return int(row["n"]) if row is not None else 0


def start_run(conn: sqlite3.Connection, *, run_type: str, input_path: Path | str | None = None) -> int:
    now = utc_now()
    with conn:
        cur = conn.execute(
            """
            INSERT INTO runs(run_type, started_at, status, input_path, created_at)
            VALUES (?, ?, 'running', ?, ?)
            """,
            (run_type, now, str(input_path or ""), now),
        )
    if cur.lastrowid is None:
        raise RuntimeError("SQLite did not return a run_id for inserted run")
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    status: str,
    row_count: int = 0,
    message: str = "",
) -> None:
    with conn:
        conn.execute(
            """
            UPDATE runs
            SET completed_at = ?, status = ?, row_count = ?, message = ?
            WHERE run_id = ?
            """,
            (utc_now(), status, int(row_count), str(message or ""), int(run_id)),
        )


def add_issue(
    conn: sqlite3.Connection,
    *,
    stage: str,
    issue_type: str,
    detail: str,
    source_id: str | None = None,
    severity: str = "warning",
) -> None:
    if severity not in SEVERITIES:
        raise ValueError(f"Unknown severity '{severity}'; expected one of {sorted(SEVERITIES)}")
    with conn:
        conn.execute(
            """
            INSERT INTO data_quality_issues(stage, source_id, issue_type, detail, severity, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (stage, source_id, issue_type, detail, severity, utc_now()),
        )

"""Stage 6 macro contract schema and SQLite helpers.

The vendored MacroLayer owns macro data construction. This package owns the portfolio-layer
contract: PIT-filtered, provenance-sealed CSVs keyed to source_pipeline sleeves.
"""
from __future__ import annotations

import math
import hashlib
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any


MACRO_REGIME_FIELDS = [
    "run_as_of",
    "macro_as_of_date",
    "active_current_regime",
    "active_next_regime",
    "current_confidence",
    "next_confidence",
    "coverage_flag",
    "regime_override_reason",
    "staleness_days",
]

MACRO_SECTOR_FIELDS = [
    "run_as_of",
    "source_pipeline",
    "macro_as_of_date",
    "macro_level",
    "macro_key",
    "macro_sector_name",
    "target_weight",
    "macro_fit_score",
    "coverage_flag",
    "fallback_used",
    "fallback_reason",
    "staleness_days",
]

MACRO_STOCK_FIELDS = [
    "run_as_of",
    "ticker",
    "source_pipeline",
    "macro_as_of_date",
    "macro_stock_fit_z",
    "industry_macro_fit",
    "industry_aggregate_macro_fit",
    "sector_macro_fit",
    "coverage_flag",
    "fallback_used",
    "fallback_reason",
    "staleness_days",
]

MACRO_COUNTRY_FIELDS = [
    "run_as_of",
    "ticker",
    "macro_as_of_date",
    "ref_area",
    "country_name",
    "region",
    "market_class",
    "country_macro_fit",
    "confidence_adjusted_fit",
    "coverage_flag",
    "staleness_days",
]

MACRO_FOREIGN_BUDGET_FIELDS = [
    "run_as_of",
    "macro_as_of_date",
    "active_flag",
    "foreign_budget",
    "min_budget",
    "max_budget",
    "eligible_candidate_count",
    "selected_candidate_count",
    "activation_reason",
    "coverage_flag",
    "staleness_days",
]

MACRO_FOREIGN_CANDIDATE_FIELDS = [
    "run_as_of",
    "ticker",
    "macro_as_of_date",
    "market_name",
    "region",
    "candidate_score",
    "sleeve_weight",
    "portfolio_weight_at_budget",
    "eligible_flag",
    "selected_flag",
    "active_flag",
    "coverage_flag",
    "staleness_days",
]

MACRO_SERVING_CONTRACT_TABLES = [
    "macro_regime_decision_daily",
    "sector_macro_fit_daily",
    "industry_macro_fit_daily",
    "industry_aggregate_macro_fit_daily",
    "stock_macro_fit_daily",
    "country_macro_fit_daily",
    "foreign_sleeve_budget_daily",
    "foreign_sleeve_candidate_daily",
]


def finite_or_blank(value: Any) -> float | str:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return ""
    return parsed if math.isfinite(parsed) else ""


def int_or_blank(value: Any) -> int | str:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(parsed):
        return ""
    return int(parsed)


def staleness_days(run_as_of: str, macro_as_of: str) -> int | None:
    try:
        return (date.fromisoformat(run_as_of) - date.fromisoformat(macro_as_of)).days
    except (TypeError, ValueError):
        return None


def open_macro_serving_db(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Macro serving DB not found: {resolved}")
    uri = f"file:{resolved.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def sqlite_snapshot_inputs(path: Path) -> dict[str, Path]:
    """Return authoritative SQLite files that define the readable snapshot.

    In WAL mode, uncheckpointed committed pages live in ``<db>-wal``. The ``-shm`` file is an
    ephemeral shared-memory index and is deliberately not hashed.
    """
    resolved = path.expanduser().resolve()
    inputs = {resolved.name: resolved}
    wal = Path(f"{resolved}-wal")
    if wal.exists():
        inputs[wal.name] = wal
    return inputs


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
    return [str(row["name"]) for row in rows]


def _digest_value(value: Any) -> bytes:
    if value is None:
        return b"<NULL>"
    if isinstance(value, bytes):
        return b"<BLOB>" + value.hex().encode("ascii")
    return str(value).encode("utf-8")


def macro_serving_content_sha256(path: Path, run_as_of: str) -> str:
    """Hash the deterministic serving DB rows consumed by the Stage 6 contract.

    The live SQLite file and its WAL sidecar are mutable storage artifacts: checkpoints, readers,
    and journal state can change their bytes without changing the portfolio-visible macro
    contract. This digest hashes the latest PIT rows Stage 6 reads from each serving table instead.
    """
    h = hashlib.sha256()
    conn = open_macro_serving_db(path)
    try:
        h.update(f"run_as_of={run_as_of}\n".encode("utf-8"))
        for table in MACRO_SERVING_CONTRACT_TABLES:
            columns = _table_columns(conn, table)
            h.update(f"table={table}\n".encode("utf-8"))
            if not columns:
                h.update(b"missing_table\n")
                continue
            h.update(("columns=" + "\x1f".join(columns) + "\n").encode("utf-8"))
            if "as_of_date" not in columns:
                h.update(b"missing_as_of_date\n")
                continue
            as_of = latest_as_of(conn, table, run_as_of)
            h.update(f"as_of={as_of or ''}\n".encode("utf-8"))
            if not as_of:
                continue
            quoted_table = _quote_identifier(table)
            order_clause = ", ".join(_quote_identifier(col) for col in columns)
            sql = f"SELECT * FROM {quoted_table} WHERE as_of_date = ? ORDER BY {order_clause}"
            row_count = 0
            for row in conn.execute(sql, (as_of,)):
                row_count += 1
                for col in columns:
                    h.update(col.encode("utf-8"))
                    h.update(b"=")
                    h.update(_digest_value(row[col]))
                    h.update(b"\x1e")
                h.update(b"\n")
            h.update(f"rows={row_count}\n".encode("utf-8"))
    finally:
        conn.close()
    return h.hexdigest()


def latest_as_of(conn: sqlite3.Connection, table: str, run_as_of: str) -> str | None:
    row = conn.execute(
        f"SELECT MAX(as_of_date) AS as_of_date FROM {table} WHERE as_of_date <= ?",
        (run_as_of,),
    ).fetchone()
    value = None if row is None else row["as_of_date"]
    return str(value) if value else None


def rows_at_latest(conn: sqlite3.Connection, table: str, run_as_of: str) -> tuple[str | None, list[sqlite3.Row]]:
    as_of = latest_as_of(conn, table, run_as_of)
    if not as_of:
        return None, []
    rows = conn.execute(f"SELECT * FROM {table} WHERE as_of_date = ?", (as_of,)).fetchall()
    return as_of, list(rows)


def single_latest_row(conn: sqlite3.Connection, table: str, run_as_of: str) -> sqlite3.Row | None:
    as_of = latest_as_of(conn, table, run_as_of)
    if not as_of:
        return None
    return conn.execute(f"SELECT * FROM {table} WHERE as_of_date = ? LIMIT 1", (as_of,)).fetchone()

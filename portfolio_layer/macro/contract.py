"""Stage 6 macro contract schema and SQLite helpers.

The vendored MacroLayer owns macro data construction. This package owns the portfolio-layer
contract: PIT-filtered, provenance-sealed CSVs keyed to source_pipeline sleeves.
"""
from __future__ import annotations

import math
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

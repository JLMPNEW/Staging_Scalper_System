"""Canonical cross-sector `stocks_scores` contract: schema, IO, calibration, persistence.

The portfolio layer consumes ONE artifact per as-of date: `runs/<as_of>/stocks_scores.csv`.
Each sector publishes its own native score export; Stage 1 adapters map those onto this contract.
Nothing here imports a sector package or reads a sector DB.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from portfolio_layer.core.db import utc_now


DEFAULT_CONTRACT_VERSION = "stocks_scores_v1"
CONTRACT_VERSION = DEFAULT_CONTRACT_VERSION

# Intermediate artifact written by Stage 1 collect (native score, pre-calibration).
COLLECTED_FIELDS = [
    "as_of_date",            # run as-of (snapshot date)
    "ticker",
    "source_pipeline",
    "sector",
    "industry",
    "industry_aggregate",
    "native_score",          # the sector's own headline score (0-100 composite)
    "investable_eligible",   # 0/1 hard gate carried from the sector's native gate
    "eligibility_reason",
    "score_confidence",      # 0-1
    "source_asof_date",      # the sector file's own date
]

# Final canonical contract consumed by the optimizer.
CONTRACT_FIELDS = [
    "as_of_date",
    "ticker",
    "source_pipeline",
    "sector",
    "industry",
    "industry_aggregate",
    "final_score",               # calibrated expected forward alpha (annualized fraction)
    "rating",
    "within_sector_percentile",  # legacy name; percentile is computed within source_pipeline/sleeve
    "score_confidence",
    "investable_eligible",
    "eligibility_reason",
    "native_score",
    "source_asof_date",
    "staleness_days",
    "score_version",
]

DEFAULT_RATING_BANDS = {  # within-sector percentile floor -> rating label
    "strong_buy": 90.0,
    "buy": 70.0,
    "hold": 40.0,
    "reduce": 20.0,
    "avoid": 0.0,
}


@dataclass
class CanonicalScore:
    """One adapter-normalized row, before calibration/percentile/rating are applied."""

    ticker: str
    source_pipeline: str
    sector: str
    industry: str
    industry_aggregate: str
    native_score: float
    investable_eligible: int
    eligibility_reason: str
    score_confidence: float
    source_asof_date: str


@dataclass
class AdapterResult:
    source_pipeline: str
    adapter: str
    source_file: Path
    source_asof_date: str
    rows: list[CanonicalScore]


# ---------------------------------------------------------------------------
# IO helpers (atomic writes so consumers never read a half-written file)
# ---------------------------------------------------------------------------
def write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore", lineterminator="\n")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
                count += 1
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
    return count


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)


def fail_if_exists(paths: Iterable[Path], *, force: bool = False) -> None:
    """Protect run artifacts from accidental overwrite unless the caller opts in."""
    existing = [path for path in paths if path.exists()]
    if existing and not force:
        joined = ", ".join(str(path) for path in existing)
        raise FileExistsError(f"Refusing to overwrite existing run artifacts without --force: {joined}")


def contract_version(config: dict[str, Any]) -> str:
    value = (
        config.get("score_contract", {}).get("contract_version")
        if isinstance(config.get("score_contract"), dict)
        else None
    )
    return str(value or DEFAULT_CONTRACT_VERSION)


# ---------------------------------------------------------------------------
# Calibration + rating
# ---------------------------------------------------------------------------
def expected_alpha(native_score: float, *, neutral: float, scale: float, expected_alpha_at_full: float) -> float:
    """Map a sector's 0-100 native composite to a common expected-alpha scale (annualized fraction).

    Monotonic-increasing linear anchor: native==neutral -> 0 alpha; native==neutral+scale ->
    +expected_alpha_at_full. Provisional magnitudes per sector; empirical IC re-calibration against the
    Stage 2 return panel refines the slopes later. The transform preserves each sector's own rank-IC sign.
    """
    values = (native_score, neutral, scale, expected_alpha_at_full)
    if not all(math.isfinite(v) for v in values):
        raise ValueError(f"expected_alpha inputs must be finite: {values}")
    if scale == 0:
        return 0.0
    return expected_alpha_at_full * (native_score - neutral) / scale


def rating_for_percentile(pct: float, bands: dict[str, float]) -> str:
    for label in ("strong_buy", "buy", "hold", "reduce", "avoid"):
        threshold = _f(bands[label]) if label in bands else None
        if threshold is not None and pct >= threshold:
            return label
    return "avoid"


def validate_rating_bands(bands: dict[str, Any]) -> list[str]:
    """Validate percentile rating thresholds before they are used for ranking/exit labels."""
    labels = ("strong_buy", "buy", "hold", "reduce", "avoid")
    errors: list[str] = []
    values: dict[str, float] = {}
    for label in labels:
        if label not in bands:
            errors.append(f"missing:{label}")
            continue
        value = _f(bands.get(label))
        if value is None:
            errors.append(f"non_numeric:{label}={bands.get(label)!r}")
            continue
        if not 0.0 <= value <= 100.0:
            errors.append(f"out_of_range:{label}={value}")
            continue
        values[label] = value
    for higher, lower in zip(labels, labels[1:]):
        if higher in values and lower in values and values[higher] < values[lower]:
            errors.append(f"not_descending:{higher}={values[higher]} < {lower}={values[lower]}")
    return errors


def percentiles_within(values: Sequence[float]) -> list[float]:
    """Rank-based percentile (0-100, higher score -> higher pct) with order-independent ties."""
    n = len(values)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: values[i])
    pct = [0.0] * n
    pos = 0
    while pos < n:
        end = pos + 1
        while end < n and values[order[end]] == values[order[pos]]:
            end += 1
        # Average the rank percentile across exact ties so input order cannot change the result.
        avg_rank = (pos + 1 + end) / 2.0
        tied_pct = 100.0 * (avg_rank - 0.5) / n
        for tied_pos in range(pos, end):
            pct[order[tied_pos]] = tied_pct
        pos = end
    return pct


# ---------------------------------------------------------------------------
# Persistence (Stage 1 introduces the contract table)
# ---------------------------------------------------------------------------
STOCKS_SCORES_DDL = """
CREATE TABLE IF NOT EXISTS stocks_scores (
    run_as_of_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    source_pipeline TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    sector TEXT,
    industry TEXT,
    industry_aggregate TEXT,
    final_score REAL,
    rating TEXT,
    within_sector_percentile REAL,
    score_confidence REAL,
    investable_eligible INTEGER NOT NULL DEFAULT 0,
    eligibility_reason TEXT,
    native_score REAL,
    source_asof_date TEXT,
    staleness_days INTEGER,
    score_version TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_as_of_date, ticker)
);
"""


def init_contract_tables(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(STOCKS_SCORES_DDL)
        if _stocks_scores_pk_columns(conn) == ["run_as_of_date", "ticker"]:
            conn.execute("DROP INDEX IF EXISTS ux_stocks_scores_run_ticker")
        else:
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_stocks_scores_run_ticker
                ON stocks_scores(run_as_of_date, ticker)
                """
            )


def _stocks_scores_pk_columns(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("PRAGMA table_info(stocks_scores)").fetchall()
    pk_cols: list[tuple[int, str]] = []
    for row in rows:
        pk_order = int(row["pk"] if isinstance(row, sqlite3.Row) else row[5])
        if pk_order:
            name = str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
            pk_cols.append((pk_order, name))
    return [name for _, name in sorted(pk_cols)]


def upsert_stocks_scores(conn: sqlite3.Connection, run_as_of: str, rows: Sequence[dict[str, Any]]) -> int:
    init_contract_tables(conn)
    now = utc_now()
    payload = [
        (
            run_as_of, r["ticker"], r["source_pipeline"], r["as_of_date"], r["sector"], r["industry"],
            r["industry_aggregate"], _f(r.get("final_score")), r.get("rating"),
            _f(r.get("within_sector_percentile")), _f(r.get("score_confidence")),
            int(r.get("investable_eligible") or 0), r.get("eligibility_reason"), _f(r.get("native_score")),
            r.get("source_asof_date"), _i(r.get("staleness_days")), r.get("score_version"), now,
        )
        for r in rows
    ]
    with conn:
        conn.execute("DELETE FROM stocks_scores WHERE run_as_of_date = ?", (run_as_of,))
        conn.executemany(
            """
            INSERT INTO stocks_scores(
                run_as_of_date, ticker, source_pipeline, as_of_date, sector, industry, industry_aggregate,
                final_score, rating, within_sector_percentile, score_confidence, investable_eligible,
                eligibility_reason, native_score, source_asof_date, staleness_days, score_version, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            payload,
        )
    return len(payload)


def _f(value: Any) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _i(value: Any) -> int | None:
    f = _f(value)
    return None if f is None else int(f)

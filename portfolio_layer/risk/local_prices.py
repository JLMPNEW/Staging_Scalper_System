from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from portfolio_layer.core.config import resolve_path


@dataclass(frozen=True)
class LocalPriceProvenance:
    provider: str
    source_id: str
    database_path: str
    first_date: str
    last_date: str
    row_count: int
    extracted_sha256: str


def _chunks(values: list[str], size: int = 400) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _read_source_rows(
    database_path: Path,
    *,
    tickers: list[str],
    source_id: str,
    start: date,
    end: date,
    accepted_adjustments: set[str],
) -> dict[str, list[tuple[str, float]]]:
    if not database_path.exists() or not tickers:
        return {}
    uri = f"{database_path.resolve().as_uri()}?mode=ro"
    grouped: dict[str, list[tuple[str, float]]] = {}
    with sqlite3.connect(uri, uri=True) as conn:
        for ticker_chunk in _chunks(tickers):
            placeholders = ",".join("?" for _ in ticker_chunk)
            rows = conn.execute(
                f"""
                SELECT ticker, bar_date, adj_close, price_adjustment
                FROM fact_price_ohlcv
                WHERE ticker IN ({placeholders})
                  AND source_id = ?
                  AND bar_date >= ?
                  AND bar_date <= ?
                  AND is_adjusted = 1
                  AND adj_close IS NOT NULL
                  AND adj_close > 0
                ORDER BY ticker, bar_date
                """,
                (*ticker_chunk, source_id, start.isoformat(), end.isoformat()),
            )
            for raw_ticker, raw_date, raw_value, raw_adjustment in rows:
                adjustment = str(raw_adjustment or "").strip().lower()
                if adjustment not in accepted_adjustments:
                    continue
                ticker = str(raw_ticker).strip().upper()
                grouped.setdefault(ticker, []).append(
                    (str(raw_date)[:10], float(raw_value))
                )
    return grouped


def _series_sha256(rows: list[tuple[str, float]]) -> str:
    digest = hashlib.sha256()
    for bar_date, value in rows:
        digest.update(f"{bar_date},{value:.17g}\n".encode())
    return digest.hexdigest()


def load_local_adjusted_price_fallbacks(
    raw_sources: object,
    *,
    base_dir: Path,
    universe: list[dict[str, Any]],
    start: date,
    end: date,
) -> tuple[
    dict[str, list[tuple[str, float]]],
    dict[str, LocalPriceProvenance],
    list[dict[str, Any]],
]:
    """Load approved adjusted-close histories from read-only local SQLite stores.

    Sources are evaluated in configuration order. The first source ID that has
    rows for a ticker wins, which makes source precedence explicit and stable.
    """
    if not isinstance(raw_sources, list):
        return {}, {}, []

    universe_pipeline = {
        str(row.get("ticker") or "").strip().upper(): str(
            row.get("source_pipeline") or ""
        ).strip()
        for row in universe
        if str(row.get("ticker") or "").strip()
    }
    prices: dict[str, list[tuple[str, float]]] = {}
    provenance: dict[str, LocalPriceProvenance] = {}
    summaries: list[dict[str, Any]] = []

    for index, raw_source in enumerate(raw_sources):
        if not isinstance(raw_source, dict) or not bool(
            raw_source.get("enabled", True)
        ):
            continue
        name = str(raw_source.get("name") or f"local_sqlite_{index + 1}").strip()
        database_path = resolve_path(
            raw_source.get("database_path"), base_dir=base_dir
        )
        source_ids = [
            str(value).strip()
            for value in raw_source.get("source_ids", [])
            if str(value).strip()
        ]
        pipelines = {
            str(value).strip()
            for value in raw_source.get("source_pipelines", [])
            if str(value).strip()
        }
        accepted_adjustments = {
            str(value).strip().lower()
            for value in raw_source.get(
                "accepted_price_adjustments", ["adjusted_close"]
            )
            if str(value).strip()
        }
        candidate_tickers = sorted(
            ticker
            for ticker, pipeline in universe_pipeline.items()
            if ticker not in prices and (not pipelines or pipeline in pipelines)
        )
        source_summary: dict[str, Any] = {
            "name": name,
            "database_path": str(database_path),
            "database_exists": database_path.exists(),
            "source_ids": source_ids,
            "source_pipelines": sorted(pipelines),
            "accepted_price_adjustments": sorted(accepted_adjustments),
            "requested_ticker_count": len(candidate_tickers),
            "loaded_ticker_count": 0,
            "loaded_row_count": 0,
            "first_date": "",
            "last_date": "",
        }
        if not database_path.exists() or not source_ids or not accepted_adjustments:
            summaries.append(source_summary)
            continue

        loaded_dates: list[str] = []
        for source_id in source_ids:
            remaining = [
                ticker for ticker in candidate_tickers if ticker not in prices
            ]
            grouped = _read_source_rows(
                database_path,
                tickers=remaining,
                source_id=source_id,
                start=start,
                end=end,
                accepted_adjustments=accepted_adjustments,
            )
            for ticker, rows in grouped.items():
                if not rows:
                    continue
                prices[ticker] = rows
                loaded_dates.extend(bar_date for bar_date, _ in rows)
                provenance[ticker] = LocalPriceProvenance(
                    provider=f"local_sqlite:{name}:{source_id}",
                    source_id=source_id,
                    database_path=str(database_path),
                    first_date=rows[0][0],
                    last_date=rows[-1][0],
                    row_count=len(rows),
                    extracted_sha256=_series_sha256(rows),
                )
        loaded_for_source = [
            item
            for item in provenance.values()
            if item.database_path == str(database_path)
            and item.provider.startswith(f"local_sqlite:{name}:")
        ]
        source_summary["loaded_ticker_count"] = len(loaded_for_source)
        source_summary["loaded_row_count"] = sum(
            item.row_count for item in loaded_for_source
        )
        source_summary["first_date"] = min(loaded_dates) if loaded_dates else ""
        source_summary["last_date"] = max(loaded_dates) if loaded_dates else ""
        summaries.append(source_summary)

    return prices, provenance, summaries

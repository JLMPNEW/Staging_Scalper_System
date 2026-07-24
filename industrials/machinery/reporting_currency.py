from __future__ import annotations

import sqlite3
from typing import Any


INVALID_MONETARY_UNITS = frozenset({"", "PURE", "SHARES", "USD/SHARES"})


def resolve_reporting_currency(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    model_family: str,
    asof: str,
    fallback: str = "",
) -> str:
    feature_row = conn.execute(
        """
        SELECT reported_currency
        FROM feature_financial_statement
        WHERE ticker = ? AND model_family = ? AND asof_date <= ?
          AND COALESCE(reported_currency, '') != ''
        ORDER BY asof_date DESC
        LIMIT 1
        """,
        (ticker, model_family, asof),
    ).fetchone()
    if feature_row is not None:
        currency = str(feature_row[0] or "").strip().upper()
        if currency not in INVALID_MONETARY_UNITS:
            return currency
    canonical_row = conn.execute(
        """
        SELECT UPPER(unit) AS currency, MAX(period_end) AS latest_period, COUNT(*) AS fact_count
        FROM fact_financial_statement_canonical
        WHERE ticker = ? AND model_family = ?
          AND canonical_metric IN ('assets', 'revenue')
          AND COALESCE(unit, '') != ''
          AND LENGTH(unit) = 3
          AND COALESCE(NULLIF(filing_date, ''), '9999-12-31') <= ?
        GROUP BY UPPER(unit)
        ORDER BY latest_period DESC, fact_count DESC, currency
        LIMIT 1
        """,
        (ticker, model_family, asof),
    ).fetchone()
    if canonical_row is not None:
        currency = str(canonical_row[0] or "").strip().upper()
        if currency not in INVALID_MONETARY_UNITS:
            return currency
    return str(fallback or "").strip().upper()


def apply_reporting_currencies(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    model_family: str,
    asof: str,
) -> list[dict[str, Any]]:
    for row in rows:
        row["currency"] = resolve_reporting_currency(
            conn,
            ticker=str(row.get("ticker") or ""),
            model_family=model_family,
            asof=asof,
            fallback=str(row.get("currency") or ""),
        )
    return rows

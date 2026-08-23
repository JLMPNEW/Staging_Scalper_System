from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Mapping

from med_devices.core.text_norm import normalize_ticker


@dataclass(frozen=True)
class SecurityIdentityWindow:
    company_id: int
    ticker: str
    listing_start_date: date | None


def parse_iso_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def load_primary_security_identity_windows(
    conn: Any,
    *,
    active_only: bool = False,
) -> dict[str, SecurityIdentityWindow]:
    active_clause = "AND c.is_active = 1" if active_only else ""
    rows = conn.execute(
        f"""
        SELECT c.company_id, c.ticker, s.listing_start_date
        FROM dim_company c
        LEFT JOIN dim_security s
          ON s.company_id = c.company_id
         AND COALESCE(s.is_primary_listing, 0) = 1
        WHERE 1 = 1
          {active_clause}
        ORDER BY c.ticker, s.security_id DESC
        """
    ).fetchall()
    out: dict[str, SecurityIdentityWindow] = {}
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        if not ticker or ticker in out:
            continue
        out[ticker] = SecurityIdentityWindow(
            company_id=int(row["company_id"]),
            ticker=ticker,
            listing_start_date=parse_iso_date(row["listing_start_date"]),
        )
    return out


def date_within_listing_identity(window: SecurityIdentityWindow, raw_date: object) -> bool:
    observation_date = parse_iso_date(raw_date)
    if observation_date is None:
        return False
    return window.listing_start_date is None or observation_date >= window.listing_start_date


def filter_rows_to_listing_identity(
    rows: Iterable[Mapping[str, Any]],
    *,
    windows: Mapping[str, SecurityIdentityWindow],
    ticker_field: str,
    date_field: str,
) -> list[Mapping[str, Any]]:
    filtered: list[Mapping[str, Any]] = []
    for row in rows:
        ticker = normalize_ticker(row.get(ticker_field))
        window = windows.get(ticker)
        if window is None or not date_within_listing_identity(window, row.get(date_field)):
            continue
        filtered.append(row)
    return filtered

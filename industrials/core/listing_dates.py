from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from industrials.core.csv_utils import read_csv_flexible, row_get
from industrials.core.text_norm import normalize_ticker


@dataclass(frozen=True)
class ListingWindow:
    ticker: str
    first_eligible_date: str
    last_eligible_date: str
    eligibility_basis: str
    source: str
    confidence: float
    notes: str


def _parse_date(raw: object, *, field: str, ticker: str, allow_blank: bool = False) -> str:
    text = str(raw or "").strip()[:10]
    if allow_blank and not text:
        return ""
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"{ticker}: invalid {field}={raw!r}; expected YYYY-MM-DD") from exc
    return text


def _confidence(raw: object) -> float:
    text = str(raw or "").strip()
    if not text:
        return 0.0
    try:
        value = float(text)
    except ValueError:
        return 0.0
    return min(1.0, max(0.0, value))


def load_listing_windows(path: Path | None) -> dict[str, ListingWindow]:
    if path is None or not path.exists():
        return {}
    windows: dict[str, ListingWindow] = {}
    for row in read_csv_flexible(path):
        ticker = normalize_ticker(row_get(row, "ticker"))
        if not ticker:
            continue
        if ticker in windows:
            raise ValueError(f"{path}: duplicate listing-date ticker={ticker}")
        first_eligible_date = _parse_date(row_get(row, "first_eligible_date", "listing_start_date"), field="first_eligible_date", ticker=ticker)
        last_eligible_date = _parse_date(
            row_get(row, "last_eligible_date", "listing_end_date"),
            field="last_eligible_date",
            ticker=ticker,
            allow_blank=True,
        )
        if last_eligible_date and last_eligible_date < first_eligible_date:
            raise ValueError(f"{ticker}: last_eligible_date {last_eligible_date} precedes first_eligible_date {first_eligible_date}")
        windows[ticker] = ListingWindow(
            ticker=ticker,
            first_eligible_date=first_eligible_date,
            last_eligible_date=last_eligible_date,
            eligibility_basis=row_get(row, "eligibility_basis", "basis"),
            source=row_get(row, "source"),
            confidence=_confidence(row_get(row, "confidence")),
            notes=row_get(row, "notes"),
        )
    return windows


def listing_window_for_ticker(windows: dict[str, ListingWindow], *tickers: str) -> ListingWindow | None:
    for ticker in tickers:
        normalized = normalize_ticker(ticker)
        if normalized in windows:
            return windows[normalized]
    return None


def bound_membership_window(
    *,
    default_start_date: str,
    default_end_date: str | None,
    listing_window: ListingWindow | None,
) -> tuple[str, str | None, float, str]:
    if listing_window is None:
        return default_start_date, default_end_date, 1.0, "No listing-date override; using source membership dates."

    start_date = max(default_start_date, listing_window.first_eligible_date)
    end_candidates = [value for value in (default_end_date, listing_window.last_eligible_date or None) if value]
    end_date = min(end_candidates) if end_candidates else None
    reason = (
        f"Membership bounded by listing-date contract: first_eligible_date={listing_window.first_eligible_date}; "
        f"last_eligible_date={listing_window.last_eligible_date or 'open'}; basis={listing_window.eligibility_basis}; "
        f"source={listing_window.source}."
    )
    return start_date, end_date, listing_window.confidence, reason

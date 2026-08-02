from __future__ import annotations

import csv
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


# These priorities are deliberately field-specific.  In particular, IBKR
# ``shortableShares`` is borrow inventory and is never an accepted public-float
# source.  A source is considered only when it has a row in fact_share_snapshot.
DEFAULT_OUTSTANDING_SOURCE_PRIORITY = (
    "interactive_brokers_fundamentals",
    "yahoo_finance_share_statistics",
    "yahoo_finance_adjusted",
    "reviewed_filing_share_override",
    "sec_companyfacts",
)
DEFAULT_FLOAT_SOURCE_PRIORITY = (
    "interactive_brokers_fundamentals",
    "yahoo_finance_share_statistics",
    "sec_companyfacts",
)
DEFAULT_MARKET_CAP_SOURCE_PRIORITY = DEFAULT_OUTSTANDING_SOURCE_PRIORITY
DEFAULT_PRICE_SOURCE_PRIORITY = DEFAULT_OUTSTANDING_SOURCE_PRIORITY
DEFAULT_MAX_STALENESS_DAYS = {
    "interactive_brokers_fundamentals": 10,
    "yahoo_finance_share_statistics": 10,
    "yahoo_finance_adjusted": 10,
    "reviewed_filing_share_override": 1100,
    # Annual foreign issuers can have a long reporting interval.  This only
    # governs the last-resort filing value; it does not turn it into true float.
    "sec_companyfacts": 550,
}
REVIEWED_SHARE_OBSERVATION_FIELDS = (
    "ticker",
    "available_date",
    "measurement_date",
    "shares_outstanding",
    "method",
    "proxy_flag",
    "source_url",
    "notes",
)


def positive_finite(raw: object) -> float | None:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0.0 else None


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None



@dataclass(frozen=True)
class ShareConversion:
    ticker: str
    effective_from: date
    effective_to: date | None
    ratio: float | None
    status: str

    def active_on(self, day: date) -> bool:
        return self.effective_from <= day and (
            self.effective_to is None or day <= self.effective_to
        )


def load_share_conversions(
    path: Path | None,
) -> dict[str, tuple[ShareConversion, ...]]:
    if path is None or not path.is_file():
        return {}
    grouped: dict[str, list[ShareConversion]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "ticker",
            "effective_from",
            "effective_to",
            "underlying_shares_per_traded_security",
            "review_status",
        }
        if not required.issubset(set(reader.fieldnames or ())):
            raise ValueError(
                f"{path}: share conversion file is missing fields="
                f"{sorted(required - set(reader.fieldnames or ()))}"
            )
        for row in reader:
            ticker = str(row.get("ticker") or "").strip().upper()
            effective_from = parse_date(row.get("effective_from"))
            effective_to = parse_date(row.get("effective_to"))
            status = str(row.get("review_status") or "").strip().upper()
            if not ticker or effective_from is None or not status:
                raise ValueError(f"{path}: incomplete share conversion row={row}")
            if effective_to is not None and effective_to < effective_from:
                raise ValueError(
                    f"{path}: {ticker} effective_to precedes effective_from"
                )
            grouped.setdefault(ticker, []).append(
                ShareConversion(
                    ticker=ticker,
                    effective_from=effective_from,
                    effective_to=effective_to,
                    ratio=positive_finite(
                        row.get("underlying_shares_per_traded_security")
                    ),
                    status=status,
                )
            )
    return {
        ticker: tuple(sorted(items, key=lambda item: item.effective_from))
        for ticker, items in grouped.items()
    }


def resolve_share_conversion(
    conversions: Mapping[str, Iterable[ShareConversion]],
    *,
    ticker: str,
    day: date,
) -> ShareConversion | None:
    matches = [
        item
        for item in conversions.get(ticker.upper(), ())
        if item.active_on(day)
    ]
    if len(matches) > 1:
        raise ValueError(f"multiple active share conversions for {ticker} at {day}")
    return matches[0] if matches else None


@dataclass(frozen=True)
class ShareObservation:
    ticker: str
    model_family: str
    asof_date: date
    source_asof_date: date | None
    source_id: str
    shares_outstanding: float | None = None
    float_shares: float | None = None
    market_cap: float | None = None
    price: float | None = None
    currency: str = ""
    outstanding_method: str = ""
    float_method: str = ""
    outstanding_proxy_flag: bool = False
    float_proxy_flag: bool = False
    quality_status: str = "accepted"
    review_reason: str = ""
    payload: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ResolvedShareSnapshot:
    ticker: str
    model_family: str
    asof_date: date
    shares_outstanding: float | None
    shares_outstanding_source_id: str
    shares_outstanding_method: str
    shares_outstanding_proxy_flag: bool
    float_shares: float | None
    float_shares_source_id: str
    float_shares_method: str
    float_shares_proxy_flag: bool
    market_cap: float | None
    market_cap_source_id: str
    market_cap_method: str
    price: float | None
    price_source_id: str


def load_reviewed_share_observations(
    path: Path,
    *,
    model_family: str,
    history_start: date,
    asof: date,
    allowed_tickers: Iterable[str] | None = None,
) -> list[ShareObservation]:
    """Load family-owned reviewed rows through a subsector-neutral core contract."""
    allowed = (
        {str(ticker).strip().upper() for ticker in allowed_tickers}
        if allowed_tickers is not None
        else None
    )
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != REVIEWED_SHARE_OBSERVATION_FIELDS:
            raise ValueError(
                f"{path}: expected fields={list(REVIEWED_SHARE_OBSERVATION_FIELDS)} "
                f"actual={reader.fieldnames}"
            )
        raw_rows = list(reader)
    output: list[ShareObservation] = []
    seen: set[tuple[str, date]] = set()
    carry_start = date.fromordinal(history_start.toordinal() - 1100)
    for row in raw_rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        available = parse_date(row.get("available_date"))
        measured = parse_date(row.get("measurement_date"))
        shares = positive_finite(row.get("shares_outstanding"))
        method = str(row.get("method") or "").strip()
        source_url = str(row.get("source_url") or "").strip()
        proxy_text = str(row.get("proxy_flag") or "").strip()
        if not ticker or available is None or measured is None or shares is None:
            raise ValueError(f"{path}: incomplete reviewed share observation={row}")
        if measured > available:
            raise ValueError(f"{path}: {ticker} measurement_date follows available_date")
        if not method or not source_url.startswith(("https://", "http://")):
            raise ValueError(f"{path}: {ticker} requires method and primary source URL")
        if proxy_text not in {"0", "1"}:
            raise ValueError(f"{path}: {ticker} proxy_flag must be 0 or 1")
        key = (ticker, available)
        if key in seen:
            raise ValueError(f"{path}: duplicate ticker/available_date={key}")
        seen.add(key)
        if allowed is not None and ticker not in allowed:
            continue
        if available < carry_start or available > asof:
            continue
        output.append(
            ShareObservation(
                ticker=ticker,
                model_family=model_family,
                asof_date=available,
                source_asof_date=measured,
                source_id="reviewed_filing_share_override",
                shares_outstanding=shares,
                outstanding_method=method,
                outstanding_proxy_flag=proxy_text == "1",
                review_reason=str(row.get("notes") or "").strip(),
                payload={"source_url": source_url},
            )
        )
    return sorted(output, key=lambda item: (item.ticker, item.asof_date))


def _source_rank(source_id: str, priority: Sequence[str]) -> int:
    try:
        return priority.index(source_id)
    except ValueError:
        return len(priority) + 100


def _eligible(
    observations: Iterable[ShareObservation],
    *,
    asof: date,
    max_staleness_days: Mapping[str, int],
) -> list[ShareObservation]:
    output: list[ShareObservation] = []
    for item in observations:
        if item.asof_date > asof or item.quality_status != "accepted":
            continue
        max_age = int(max_staleness_days.get(item.source_id, 0))
        if max_age >= 0 and (asof - item.asof_date).days > max_age:
            continue
        output.append(item)
    return output


def _select(
    observations: Sequence[ShareObservation],
    *,
    field: str,
    priority: Sequence[str],
) -> tuple[float | None, ShareObservation | None]:
    candidates: list[tuple[int, int, str, ShareObservation, float]] = []
    for item in observations:
        value = positive_finite(getattr(item, field))
        if value is None or item.source_id not in priority:
            continue
        candidates.append(
            (
                _source_rank(item.source_id, priority),
                -item.asof_date.toordinal(),
                item.source_id,
                item,
                value,
            )
        )
    if not candidates:
        return None, None
    selected = min(candidates, key=lambda item: item[:3])
    return selected[4], selected[3]


def resolve_observations(
    observations: Iterable[ShareObservation],
    *,
    ticker: str,
    model_family: str,
    asof: date,
    outstanding_priority: Sequence[str] = DEFAULT_OUTSTANDING_SOURCE_PRIORITY,
    float_priority: Sequence[str] = DEFAULT_FLOAT_SOURCE_PRIORITY,
    market_cap_priority: Sequence[str] = DEFAULT_MARKET_CAP_SOURCE_PRIORITY,
    price_priority: Sequence[str] = DEFAULT_PRICE_SOURCE_PRIORITY,
    max_staleness_days: Mapping[str, int] = DEFAULT_MAX_STALENESS_DAYS,
) -> ResolvedShareSnapshot:
    scoped = [
        item
        for item in observations
        if item.ticker.upper() == ticker.upper() and item.model_family == model_family
    ]
    eligible = _eligible(scoped, asof=asof, max_staleness_days=max_staleness_days)
    outstanding, outstanding_row = _select(
        eligible, field="shares_outstanding", priority=outstanding_priority
    )
    float_shares, float_row = _select(
        eligible, field="float_shares", priority=float_priority
    )
    market_cap, market_cap_row = _select(
        eligible, field="market_cap", priority=market_cap_priority
    )
    price, price_row = _select(eligible, field="price", priority=price_priority)
    market_cap_method = "direct_source" if market_cap is not None else ""
    market_cap_source_id = market_cap_row.source_id if market_cap_row else ""
    if market_cap is None and outstanding is not None and price is not None:
        market_cap = outstanding * price
        market_cap_source_id = "+".join(
            part
            for part in (
                outstanding_row.source_id if outstanding_row else "",
                price_row.source_id if price_row else "",
            )
            if part
        )
        market_cap_method = "price_times_shares_outstanding"
    return ResolvedShareSnapshot(
        ticker=ticker.upper(),
        model_family=model_family,
        asof_date=asof,
        shares_outstanding=outstanding,
        shares_outstanding_source_id=(outstanding_row.source_id if outstanding_row else ""),
        shares_outstanding_method=(outstanding_row.outstanding_method if outstanding_row else ""),
        shares_outstanding_proxy_flag=(
            outstanding_row.outstanding_proxy_flag if outstanding_row else False
        ),
        float_shares=float_shares,
        float_shares_source_id=(float_row.source_id if float_row else ""),
        float_shares_method=(float_row.float_method if float_row else ""),
        float_shares_proxy_flag=(float_row.float_proxy_flag if float_row else False),
        market_cap=market_cap,
        market_cap_source_id=market_cap_source_id,
        market_cap_method=market_cap_method,
        price=price,
        price_source_id=(price_row.source_id if price_row else ""),
    )


def observations_from_rows(rows: Iterable[Mapping[str, Any]]) -> list[ShareObservation]:
    output: list[ShareObservation] = []
    for row in rows:
        asof = parse_date(row.get("asof_date"))
        if asof is None:
            continue
        output.append(
            ShareObservation(
                ticker=str(row.get("ticker") or "").strip().upper(),
                model_family=str(row.get("model_family") or "").strip(),
                asof_date=asof,
                source_asof_date=parse_date(row.get("source_asof_date")),
                source_id=str(row.get("source_id") or "").strip(),
                shares_outstanding=positive_finite(row.get("shares_outstanding")),
                float_shares=positive_finite(row.get("float_shares")),
                market_cap=positive_finite(row.get("market_cap")),
                price=positive_finite(row.get("price")),
                currency=str(row.get("currency") or ""),
                outstanding_method=str(row.get("outstanding_method") or ""),
                float_method=str(row.get("float_method") or ""),
                outstanding_proxy_flag=bool(row.get("outstanding_proxy_flag") or 0),
                float_proxy_flag=bool(row.get("float_proxy_flag") or 0),
                quality_status=str(row.get("quality_status") or "accepted"),
                review_reason=str(row.get("review_reason") or ""),
            )
        )
    return output


def load_observations(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    model_family: str,
    asof: date,
) -> list[ShareObservation]:
    rows = conn.execute(
        """
        SELECT ticker, model_family, asof_date, source_asof_date, source_id,
               shares_outstanding, float_shares, market_cap, price, currency,
               outstanding_method, float_method, outstanding_proxy_flag,
               float_proxy_flag, quality_status, review_reason
        FROM fact_share_snapshot
        WHERE ticker = ? AND model_family = ? AND asof_date <= ?
        """,
        (ticker.upper(), model_family, asof.isoformat()),
    ).fetchall()
    return observations_from_rows(dict(row) for row in rows)


def resolve_share_snapshot(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    model_family: str,
    asof: date,
) -> ResolvedShareSnapshot:
    return resolve_observations(
        load_observations(
            conn,
            ticker=ticker,
            model_family=model_family,
            asof=asof,
        ),
        ticker=ticker,
        model_family=model_family,
        asof=asof,
    )


def upsert_observations(
    conn: sqlite3.Connection,
    observations: Iterable[ShareObservation],
) -> int:
    rows = list(observations)
    if not rows:
        return 0
    from industrials.core.db import utc_now

    now = utc_now()
    conn.executemany(
        """
        INSERT INTO fact_share_snapshot(
            ticker, model_family, asof_date, source_asof_date, source_id,
            shares_outstanding, float_shares, market_cap, price, currency,
            outstanding_method, float_method, outstanding_proxy_flag,
            float_proxy_flag, quality_status, review_reason, payload_json,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, model_family, asof_date, source_id) DO UPDATE SET
            source_asof_date = excluded.source_asof_date,
            shares_outstanding = excluded.shares_outstanding,
            float_shares = excluded.float_shares,
            market_cap = excluded.market_cap,
            price = excluded.price,
            currency = excluded.currency,
            outstanding_method = excluded.outstanding_method,
            float_method = excluded.float_method,
            outstanding_proxy_flag = excluded.outstanding_proxy_flag,
            float_proxy_flag = excluded.float_proxy_flag,
            quality_status = excluded.quality_status,
            review_reason = excluded.review_reason,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        [
            (
                item.ticker.upper(),
                item.model_family,
                item.asof_date.isoformat(),
                item.source_asof_date.isoformat() if item.source_asof_date else None,
                item.source_id,
                item.shares_outstanding,
                item.float_shares,
                item.market_cap,
                item.price,
                item.currency,
                item.outstanding_method,
                item.float_method,
                int(item.outstanding_proxy_flag),
                int(item.float_proxy_flag),
                item.quality_status,
                item.review_reason,
                json.dumps(item.payload or {}, sort_keys=True, default=str),
                now,
                now,
            )
            for item in rows
        ],
    )
    return len(rows)

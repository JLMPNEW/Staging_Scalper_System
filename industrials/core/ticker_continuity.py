from __future__ import annotations

from datetime import date
from typing import Any

from industrials.core.text_norm import normalize_ticker


def ticker_continuity_chain(conn: Any, ticker: str, *, asof: date) -> tuple[str, ...]:
    """Return current ticker followed by governed predecessors effective by ``asof``.

    Raw positioning sources retain historical symbols after an issuer changes
    ticker. Consumers use this chain for issuer-continuous lookups while still
    writing the feature under the point-in-time contract ticker.
    """
    current = normalize_ticker(ticker)
    if not current:
        return ()
    ordered = [current]
    seen = {current}
    while True:
        row = conn.execute(
            """
            SELECT predecessor_ticker
            FROM dim_ticker_alias
            WHERE verified_flag = 1
              AND effective_date <= ?
              AND (active_ticker = ? OR contract_ticker = ?)
              AND COALESCE(predecessor_ticker, '') <> ''
            ORDER BY effective_date DESC
            LIMIT 1
            """,
            (asof.isoformat(), current, current),
        ).fetchone()
        predecessor = normalize_ticker(row["predecessor_ticker"]) if row is not None else ""
        if not predecessor or predecessor in seen:
            break
        ordered.append(predecessor)
        seen.add(predecessor)
        current = predecessor
    return tuple(ordered)

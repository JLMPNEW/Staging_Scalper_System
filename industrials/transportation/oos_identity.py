from __future__ import annotations

import sqlite3
from datetime import date

from industrials.core.oos_research import parse_date


def optional_date(value: object) -> date | None:
    raw = str(value or "").strip()
    return parse_date(raw) if raw else None


def load_aliases(
    connection: sqlite3.Connection,
    *,
    source_id: str,
) -> dict[str, list[dict[str, object]]]:
    output: dict[str, list[dict[str, object]]] = {}
    rows = connection.execute(
        """
        SELECT contract_ticker, active_ticker, predecessor_ticker,
               effective_date
        FROM dim_ticker_alias
        WHERE verified_flag=1 AND source_id=?
        ORDER BY contract_ticker, effective_date
        """,
        (source_id,),
    )
    for row in rows:
        ticker = str(row["contract_ticker"]).upper()
        output.setdefault(ticker, []).append(
            {
                "active_ticker": str(
                    row["active_ticker"] or ticker
                ).upper(),
                "predecessor_ticker": str(
                    row["predecessor_ticker"] or ticker
                ).upper(),
                "effective_date": parse_date(row["effective_date"]),
            }
        )
    return output


def resolve_price_ticker(
    ticker: str,
    *,
    asof: date,
    aliases: dict[str, list[dict[str, object]]],
) -> tuple[str, str]:
    policies = aliases.get(ticker.upper(), [])
    if not policies:
        return ticker.upper(), ""
    for policy in policies:
        effective_date = policy["effective_date"]
        if not isinstance(effective_date, date):
            raise TypeError("alias effective_date must be a date")
        if asof < effective_date:
            return (
                str(policy["predecessor_ticker"]),
                "verified_predecessor",
            )
    return (
        str(policies[-1]["active_ticker"]),
        "verified_active_alias",
    )


def load_memberships(
    connection: sqlite3.Connection,
) -> dict[tuple[str, str], dict[str, object]]:
    rows = connection.execute(
        """
        SELECT m.ticker, m.membership_source_id, m.start_date, m.end_date,
               m.membership_status,
               COALESCE(s.terminal_type, '') AS terminal_type,
               COALESCE(s.exit_type, '') AS exit_type
        FROM dim_universe_membership AS m
        LEFT JOIN dim_delisted_calibration_seed AS s
          ON s.model_family=m.model_family
         AND s.internal_ticker=m.ticker
        WHERE m.model_family='transportation'
          AND m.point_in_time_flag=1
        ORDER BY m.ticker, m.membership_source_id
        """
    )
    return {
        (
            str(row["ticker"]).upper(),
            str(row["membership_source_id"]),
        ): {
            "start_date": parse_date(row["start_date"]),
            "end_date": optional_date(row["end_date"]),
            "membership_status": str(row["membership_status"] or ""),
            "terminal_type": str(row["terminal_type"] or "").lower(),
            "exit_type": str(row["exit_type"] or "").lower(),
        }
        for row in rows
    }


def load_continuity(
    connection: sqlite3.Connection,
) -> dict[str, dict[str, object]]:
    rows = connection.execute(
        """
        SELECT ticker, current_security_start_date,
               structural_break_date, continuity_policy,
               history_treatment
        FROM dim_security_continuity_policy
        WHERE model_family='transportation'
        ORDER BY ticker
        """
    )
    return {
        str(row["ticker"]).upper(): {
            "current_security_start_date": parse_date(
                row["current_security_start_date"]
            ),
            "structural_break_date": optional_date(
                row["structural_break_date"]
            ),
            "continuity_policy": str(
                row["continuity_policy"] or ""
            ),
            "history_treatment": str(
                row["history_treatment"] or ""
            ),
        }
        for row in rows
    }

"""Read-only reconstruction of Consumer 13F history from canonical holdings.

The shared positioning database owns the raw holdings and remains read-only to
Consumer Defensive.  This adapter reconstructs the same period-bucketed,
manager-deduplicated ownership series used by the neutral positioning service,
without mutating that service or trusting its incomplete legacy snapshot table.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

from consumer_defensive.core.config import ConfigBundle, cfg_get, resolve_path


INSTITUTIONAL_HISTORY_SCHEMA = "consumer_defensive_institutional_history_v2"
SOURCE_ID = "sec_13f_data_sets"
KNOWLEDGE_LAG_DAYS = 50


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _finite(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("13F holdings must be finite numeric data")
    parsed = float(value or 0.0)
    if not math.isfinite(parsed):
        raise ValueError("13F holdings must be finite numeric data")
    return parsed


def _safe_database(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or resolved.is_symlink():
        raise FileNotFoundError(f"shared positioning database is missing or unsafe: {resolved}")
    return resolved


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{_safe_database(path).as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
        connection.close()
        raise RuntimeError("institutional-history source is not query-only")
    return connection


def _normalized_tickers(tickers: Iterable[str]) -> tuple[str, ...]:
    values = tuple(sorted({str(value).strip().upper() for value in tickers if str(value).strip()}))
    if not values:
        raise ValueError("institutional history requires a nonempty ticker scope")
    return values


def derive_institutional_history_v2(
    database_path: Path,
    *,
    tickers: Iterable[str],
    history_start: str,
    maximum_date: str,
) -> tuple[dict[str, tuple[dict[str, Any], ...]], dict[str, Any]]:
    """Derive immutable ticker-period snapshots from raw holdings, read-only."""

    start = date.fromisoformat(history_start)
    maximum = date.fromisoformat(maximum_date)
    if maximum < start:
        raise ValueError("institutional-history maximum date predates history start")
    # One prior year is sufficient to compute the first in-window quarterly
    # change while avoiding a scan of decades that cannot affect calibration.
    minimum_period = (start - timedelta(days=400)).isoformat()
    scope = _normalized_tickers(tickers)
    placeholders = ",".join("?" for _ in scope)
    sql = f"""SELECT ticker,filing_date,accepted_at,period_of_report,
                     COALESCE(NULLIF(manager_cik,''),NULLIF(manager_name,''),filing_key) AS manager_key,
                     COALESCE(NULLIF(filing_key,''),filing_date) AS accession_key,
                     COALESCE(shares,0.0) AS shares,
                     COALESCE(market_value,0.0) AS market_value
                FROM institutional_13f_holdings
               WHERE UPPER(ticker) IN ({placeholders})
                 AND UPPER(COALESCE(share_type,'')) IN ('','SH')
                 AND COALESCE(put_call,'')=''
                 AND COALESCE(period_of_report,'')>=?
                 AND COALESCE(filing_date,'')<=?
                 AND COALESCE(period_of_report,'')<>''
               ORDER BY ticker,period_of_report,manager_key,filing_date,accession_key"""
    # (ticker, period) -> manager -> latest filing aggregate. Repeated rows in
    # one filing are summed; a later amendment replaces the earlier filing.
    buckets: dict[
        tuple[str, str],
        dict[str, tuple[str, str, float, float, list[list[Any]]]],
    ] = {}
    source_row_count = 0
    with _open_read_only(database_path) as connection:
        cursor = connection.execute(sql, (*scope, minimum_period, maximum.isoformat()))
        for row in cursor:
            source_row_count += 1
            ticker = str(row["ticker"]).upper()
            period = str(row["period_of_report"])
            manager = str(row["manager_key"] or "").strip()
            if not manager:
                continue
            knowledge = str(row["accepted_at"] or row["filing_date"] or "")
            knowledge_date = knowledge[:10]
            availability = (
                date.fromisoformat(period) + timedelta(days=KNOWLEDGE_LAG_DAYS)
            ).isoformat()
            # Freeze each quarter at a conservative post-deadline cutoff. Late
            # amendments/backfills are real later knowledge, but allowing them
            # to rewrite an old calibration snapshot would erase the fact set
            # investors actually had at that time.
            if (
                not knowledge_date
                or knowledge_date > availability
                or availability > maximum.isoformat()
            ):
                continue
            accession = str(row["accession_key"] or "")
            shares = _finite(row["shares"])
            market_value = _finite(row["market_value"])
            managers = buckets.setdefault((ticker, period), {})
            current = managers.get(manager)
            identity = [knowledge, accession, shares, market_value]
            if current is None or (knowledge, accession) < (current[0], current[1]):
                managers[manager] = (
                    knowledge,
                    accession,
                    shares,
                    market_value,
                    [identity],
                )
            elif (knowledge, accession) == (current[0], current[1]):
                managers[manager] = (
                    knowledge,
                    accession,
                    current[2] + shares,
                    current[3] + market_value,
                    [*current[4], identity],
                )

    by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    prior_by_ticker: dict[str, tuple[float, set[str]]] = {}
    for ticker, period in sorted(buckets, key=lambda item: (item[0], item[1])):
        managers = buckets[(ticker, period)]
        publication_date = (
            date.fromisoformat(period) + timedelta(days=KNOWLEDGE_LAG_DAYS)
        ).isoformat()
        total_shares = sum(value[2] for value in managers.values())
        total_value = sum(value[3] for value in managers.values())
        manager_set = set(managers)
        prior = prior_by_ticker.get(ticker)
        if prior is None:
            delta = None
            new_buyers = exiting = 0
        else:
            prior_shares, prior_managers = prior
            delta = (total_shares - prior_shares) / prior_shares if prior_shares > 0.0 else None
            new_buyers = len(manager_set - prior_managers)
            exiting = len(prior_managers - manager_set)
        prior_by_ticker[ticker] = (total_shares, manager_set)
        manager_payload = [
            [manager, *managers[manager][:4], managers[manager][4]]
            for manager in sorted(managers)
        ]
        source_observation_id = f"cd13fv2_{_sha([ticker, period, manager_payload])[:32]}"
        by_ticker[ticker].append(
            {
                "ticker": ticker,
                "publication_date": publication_date,
                "period_of_report": period,
                "institutional_shares": total_shares,
                "institutional_value": total_value,
                "manager_count": len(manager_set),
                "new_buyer_count": new_buyers,
                "exiting_holder_count": exiting,
                "net_buyer_count": new_buyers - exiting,
                "institutional_ownership_delta_pct": delta,
                "source_id": SOURCE_ID,
                "source_observation_id": source_observation_id,
            }
        )
    output = {
        ticker: tuple(sorted(rows, key=lambda row: (row["publication_date"], row["period_of_report"])))
        for ticker, rows in sorted(by_ticker.items())
    }
    snapshot_payload = [row for ticker in sorted(output) for row in output[ticker]]
    summary = {
        "schema_version": INSTITUTIONAL_HISTORY_SCHEMA,
        "source_database": str(_safe_database(database_path)),
        "source_table": "institutional_13f_holdings",
        "source_id": SOURCE_ID,
        "knowledge_policy": "first_filed_as_reported_frozen_after_13f_deadline",
        "knowledge_lag_days": KNOWLEDGE_LAG_DAYS,
        "history_start": history_start,
        "maximum_date": maximum_date,
        "ticker_scope_count": len(scope),
        "source_row_count": source_row_count,
        "snapshot_row_count": len(snapshot_payload),
        "snapshot_ticker_count": len(output),
        "snapshot_sha256": _sha(snapshot_payload),
        "mutation_performed": False,
    }
    summary["payload_sha256"] = _sha(summary)
    return output, summary


def load_institutional_history_v2(
    bundle: ConfigBundle,
    *,
    tickers: Iterable[str],
    history_start: str,
    maximum_date: str,
) -> tuple[dict[str, tuple[dict[str, Any], ...]], dict[str, Any]]:
    path = resolve_path(
        cfg_get(bundle.payload, "positioning.market_positioning_upstream_db"),
        base_dir=bundle.base_dir,
    )
    return derive_institutional_history_v2(
        path,
        tickers=tickers,
        history_start=history_start,
        maximum_date=maximum_date,
    )


__all__ = [
    "INSTITUTIONAL_HISTORY_SCHEMA",
    "derive_institutional_history_v2",
    "load_institutional_history_v2",
]


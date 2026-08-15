"""Read-only Stage 5 source imports and PIT positioning feature construction."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import ConfigBundle, cfg_get, resolve_path
from .db import utc_now
from .stage5_schema import ensure_stage5_schema
from .universe import normalize_ticker


POSITIONING_DEFINITION_VERSION = "consumer_defensive_positioning_v2"
SHARE_PROXY_CONCEPTS = (
    "EntityCommonStockSharesOutstanding",
    "CommonStockSharesOutstanding",
    "NumberOfSharesOutstanding",
    "SharesOutstanding",
    "WeightedAverageNumberOfDilutedSharesOutstanding",
    "WeightedAverageNumberOfSharesOutstandingBasic",
)


def _ro_connect(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Stage 5 upstream database not found: {resolved}")
    conn = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _require_columns(conn: sqlite3.Connection, table: str, required: set[str]) -> None:
    actual = _table_columns(conn, table)
    missing = sorted(required - actual)
    if missing:
        raise RuntimeError(f"Stage 5 upstream table {table} is missing columns: {missing}")


def _parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    text = text.split("T", 1)[0].split(" ", 1)[0]
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d-%b-%Y", "%d-%B-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text.upper(), fmt).date()
        except ValueError:
            continue
    return None


def _iso_date(raw: object, *, label: str) -> str:
    text = str(raw or "").strip()
    parsed = _parse_date(text)
    if parsed is None or text != parsed.isoformat():
        raise RuntimeError(f"Stage 5 upstream {label} must be an ISO date; got {raw!r}")
    return text


def _accepted_at(raw: object, fallback: object) -> str | None:
    text = str(raw or "").strip()
    if text:
        normalized = text.replace(" ", "T")
        if len(normalized) == 8 and normalized.isdigit():
            parsed = _parse_date(normalized)
            return f"{parsed.isoformat()}T23:59:59Z" if parsed else None
        if len(normalized) >= 10 and _parse_date(normalized):
            if len(normalized) == 10:
                return f"{normalized}T23:59:59Z"
            if normalized.endswith("Z"):
                return normalized
            if "+" in normalized[10:] or normalized[10:].count("-"):
                try:
                    parsed_dt = datetime.fromisoformat(normalized)
                    return parsed_dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                except ValueError:
                    pass
            return normalized[:19] + "Z"
    parsed = _parse_date(fallback)
    return f"{parsed.isoformat()}T23:59:59Z" if parsed else None


def _float(raw: object, *, nonnegative: bool = False) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or (nonnegative and value < 0.0):
        return None
    return value


def _integer(raw: object) -> int | None:
    value = _float(raw, nonnegative=True)
    return int(value) if value is not None and value.is_integer() else None


def _digest(kind: str, *values: object) -> str:
    payload = json.dumps([kind, *values], ensure_ascii=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _source_path(bundle: ConfigBundle, key: str) -> Path:
    return resolve_path(cfg_get(bundle.payload, f"positioning.{key}"), base_dir=bundle.base_dir)


def _source_birthdate(bundle: ConfigBundle, key: str) -> str:
    value = str(cfg_get(bundle.payload, f"positioning.source_birthdates.{key}", ""))
    parsed = _parse_date(value)
    if parsed is None or parsed.isoformat() != value:
        raise ValueError(f"positioning.source_birthdates.{key} must be an ISO date")
    return value


def _universe(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT t.ticker, c.cik, c.company_name, c.company_id, s.security_id,
               s.exchange, s.provider_price_symbol, s.listing_status,
               MIN(m.start_date) AS first_membership_date,
               MAX(m.end_date) AS last_membership_end
        FROM dim_consumer_defensive_taxonomy AS t
        JOIN dim_company AS c ON c.company_id=t.company_id
        JOIN dim_security AS s ON s.security_id=t.security_id
        LEFT JOIN dim_universe_membership AS m
          ON m.ticker=t.ticker AND m.model_family='consumer_defensive'
        WHERE t.model_family='consumer_defensive'
        GROUP BY t.ticker, c.cik, c.company_name, c.company_id, s.security_id,
                 s.exchange, s.provider_price_symbol, s.listing_status
        ORDER BY t.ticker
        """
    ).fetchall()


def _share_proxy_history(
    conn: sqlite3.Connection,
    *,
    tickers: list[str],
    as_of: str,
) -> dict[str, list[tuple[str, str, int, float, str]]]:
    """Load consolidated SEC share-count candidates using indexed ticker slices."""

    history: dict[str, list[tuple[str, str, int, float, str]]] = {}
    cutoff = f"{as_of}T23:59:59Z"
    concept_placeholders = ",".join("?" for _ in SHARE_PROXY_CONCEPTS)
    priorities = {concept: index for index, concept in enumerate(SHARE_PROXY_CONCEPTS)}
    for ticker in tickers:
        candidates: list[tuple[str, str, int, float, str]] = []
        for row in conn.execute(
            f"""SELECT concept,numeric_value,accepted_at,period_end
                 FROM fact_sec_xbrl_fact_raw
                 WHERE ticker=? AND concept IN ({concept_placeholders})
                   AND accepted_at<=? AND numeric_value>0
                   AND LOWER(COALESCE(unit,'')) IN ('shares','share')
                   AND COALESCE(dimensions_json,'') IN ('','{{}}','[]')
                 ORDER BY accepted_at,period_end,concept""",
            (ticker, *SHARE_PROXY_CONCEPTS, cutoff),
        ):
            accepted = str(row["accepted_at"] or "")
            period_end = str(row["period_end"] or "")
            value = _float(row["numeric_value"], nonnegative=True)
            concept = str(row["concept"])
            if not accepted or not _parse_date(accepted) or not _parse_date(period_end):
                continue
            if value is None or value <= 0.0:
                continue
            candidates.append(
                (accepted, period_end, priorities[concept], value, concept)
            )
        history[ticker] = candidates
    return history


def _share_proxy_at(
    history: dict[str, list[tuple[str, str, int, float, str]]],
    *,
    ticker: str,
    available_date: str,
) -> tuple[float | None, str | None, str | None, str | None]:
    cutoff = f"{available_date}T23:59:59Z"
    eligible = [
        row
        for row in history.get(ticker, [])
        if row[0] <= cutoff and row[1] <= available_date
    ]
    if not eligible:
        return None, None, None, None
    accepted, _period_end, _priority, value, concept = max(
        eligible,
        key=lambda row: (row[0], row[1], -row[2]),
    )
    return value, concept, accepted, "sec_xbrl_pit_share_proxy_v1"


def import_sec_insider_transactions(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    as_of: str,
) -> dict[str, Any]:
    """Replace the Consumer Defensive slice from the read-only SEC Form 4 mirror."""

    ensure_stage5_schema(conn)
    cutoff = f"{as_of}T23:59:59Z"
    start = str(cfg_get(bundle.payload, "positioning.start_date"))
    start_date = _parse_date(start)
    if start_date is None:
        raise ValueError("positioning.start_date must be an ISO date")
    source_id = str(cfg_get(bundle.payload, "positioning.ownership_source_id"))
    universe = _universe(conn)
    cik_to_ticker = {
        str(row["cik"] or "").lstrip("0"): str(row["ticker"])
        for row in universe
        if str(row["cik"] or "").strip()
    }
    if not cik_to_ticker:
        raise RuntimeError("Stage 5 ownership import requires issuer CIKs in the taxonomy.")

    upstream_path = _source_path(bundle, "form4_upstream_db")
    required = {
        "event_key", "is_current_truth", "accession_number", "document_type",
        "filing_date", "filing_date_sort", "accepted_ts_utc", "issuer_cik",
        "rptowner_cik", "rptowner_name", "rptowner_relationship", "rptowner_title",
        "security_title", "trans_date", "trans_code", "trans_shares",
        "trans_price_per_share", "trans_acquired_disp_cd",
    }
    records: list[tuple[Any, ...]] = []
    seen_ids: set[str] = set()
    invalid_rows = 0
    with _ro_connect(upstream_path) as upstream:
        _require_columns(upstream, "form4_events_tier1", required)
        placeholders = ",".join("?" for _ in cik_to_ticker)
        rows = upstream.execute(
            f"""
            SELECT event_key, accession_number, document_type, filing_date,
                   filing_date_sort, accepted_ts_utc, issuer_cik, rptowner_cik,
                   rptowner_name, rptowner_relationship, rptowner_title,
                   security_title, trans_date, trans_code, trans_shares,
                   trans_price_per_share, trans_acquired_disp_cd
            FROM form4_events_tier1
            WHERE is_current_truth=1
              AND LTRIM(COALESCE(issuer_cik,''),'0') IN ({placeholders})
            ORDER BY issuer_cik, accepted_ts_utc, filing_date_sort,
                     accession_number, event_key
            """,
            tuple(sorted(cik_to_ticker)),
        )
        for row in rows:
            ticker = cik_to_ticker.get(str(row["issuer_cik"] or "").lstrip("0"))
            accepted = _accepted_at(row["accepted_ts_utc"], row["filing_date_sort"] or row["filing_date"])
            accepted_date = _parse_date(accepted)
            if ticker is None or accepted is None or accepted_date is None:
                invalid_rows += 1
                continue
            if accepted_date < start_date or accepted > cutoff:
                continue
            shares = _float(row["trans_shares"], nonnegative=True)
            price = _float(row["trans_price_per_share"], nonnegative=True)
            transaction_id = str(row["event_key"] or "").strip() or _digest(
                "form4-key", row["accession_number"], row["rptowner_cik"],
                row["trans_date"], row["trans_code"], shares, price,
            )
            observation_id = _digest(
                "form4-v1", transaction_id, ticker, row["accession_number"], row["rptowner_cik"],
                row["trans_date"], accepted, row["trans_code"], shares, price,
                row["trans_acquired_disp_cd"], row["security_title"],
            )
            if transaction_id in seen_ids:
                raise RuntimeError(f"Duplicate Form 4 transaction identity: {transaction_id}")
            seen_ids.add(transaction_id)
            relationship = str(row["rptowner_relationship"] or row["rptowner_title"] or "").strip()
            records.append(
                (
                    transaction_id, ticker, str(row["rptowner_cik"] or "").strip() or None,
                    _parse_date(row["trans_date"]).isoformat() if _parse_date(row["trans_date"]) else None,
                    accepted, str(row["trans_code"] or "").strip().upper() or None,
                    shares, price, str(row["trans_acquired_disp_cd"] or "").strip().upper() or None,
                    source_id, utc_now(), str(row["accession_number"] or "").strip() or None,
                    str(row["rptowner_name"] or "").strip() or None, relationship or None,
                    str(row["security_title"] or "").strip() or None, accepted,
                    accepted_date.isoformat(), 1, observation_id,
                )
            )

    tickers = [str(row["ticker"]) for row in universe]
    placeholders = ",".join("?" for _ in tickers)
    with conn:
        conn.execute(
            f"DELETE FROM fact_sec_ownership_transaction WHERE source_id=? AND ticker IN ({placeholders})",
            (source_id, *tickers),
        )
        conn.executemany(
            """
            INSERT INTO fact_sec_ownership_transaction(
                transaction_id,ticker,owner_cik,transaction_date,filed_at,
                transaction_code,shares,price,acquired_disposed,source_id,created_at,
                accession_number,owner_name,owner_relationship,security_title,
                accepted_at,availability_date,is_current_truth,source_observation_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            records,
        )
    return {
        "source_id": source_id,
        "upstream_database": str(upstream_path),
        "rows": len(records),
        "tickers": len({row[1] for row in records}),
        "invalid_rows": invalid_rows,
        "as_of": as_of,
    }


def import_market_positioning(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    as_of: str,
) -> dict[str, Any]:
    """Replace Consumer Defensive 13F/short/borrow facts from a read-only store."""

    ensure_stage5_schema(conn)
    upstream_path = _source_path(bundle, "market_positioning_upstream_db")
    source_id = str(cfg_get(bundle.payload, "positioning.market_positioning_source_id"))
    tickers = [str(row["ticker"]) for row in _universe(conn)]
    ticker_set = set(tickers)
    placeholders = ",".join("?" for _ in tickers)
    births = {
        "13f": _source_birthdate(bundle, "institutional_13f"),
        "short": _source_birthdate(bundle, "short_interest"),
        "borrow": _source_birthdate(bundle, "borrow"),
    }
    upstream_sources = {
        key: str(cfg_get(bundle.payload, f"positioning.upstream_source_names.{config_key}"))
        for key, config_key in (
            ("13f", "institutional_13f"),
            ("short", "short_interest"),
            ("borrow", "borrow"),
        )
    }
    now = utc_now()
    institutional: list[tuple[Any, ...]] = []
    shorts: list[tuple[Any, ...]] = []
    borrow_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    share_history = _share_proxy_history(conn, tickers=tickers, as_of=as_of)

    with _ro_connect(upstream_path) as upstream:
        _require_columns(
            upstream,
            "institutional_13f_ownership_snapshots",
            {"ticker", "asof_date", "period_of_report", "institutional_shares", "institutional_value", "manager_count", "new_buyer_count", "exiting_holder_count", "net_buyer_count", "institutional_ownership_delta_pct"},
        )
        for row in upstream.execute(
            f"""SELECT * FROM institutional_13f_ownership_snapshots
                 WHERE UPPER(REPLACE(ticker,'.','-')) IN ({placeholders})
                   AND asof_date>=? AND asof_date<=?
                   AND source=?
                 ORDER BY ticker,asof_date,source""",
            (*tickers, births["13f"], as_of, upstream_sources["13f"]),
        ):
            ticker = normalize_ticker(row["ticker"])
            if ticker not in ticker_set:
                continue
            available = _iso_date(row["asof_date"], label="13F asof_date")
            period_of_report = _iso_date(
                row["period_of_report"],
                label="13F period_of_report",
            )
            observation_id = _digest("13f-v1", ticker, available, period_of_report, row["institutional_shares"], row["institutional_value"], row["manager_count"], row["institutional_ownership_delta_pct"])
            institutional.append((
                ticker, available, available, source_id,
                _float(row["institutional_value"], nonnegative=True),
                _float(row["institutional_shares"], nonnegative=True),
                _integer(row["manager_count"]), now,
                period_of_report,
                _integer(row["new_buyer_count"]), _integer(row["exiting_holder_count"]),
                _integer(row["net_buyer_count"]), _float(row["institutional_ownership_delta_pct"]),
                births["13f"], observation_id,
            ))

        _require_columns(
            upstream,
            "short_interest_snapshots",
            {"ticker", "settlement_date", "publication_date", "short_interest_shares", "short_interest_pct_float", "days_to_cover"},
        )
        for row in upstream.execute(
            f"""SELECT * FROM short_interest_snapshots
                 WHERE UPPER(REPLACE(ticker,'.','-')) IN ({placeholders})
                   AND publication_date>=? AND publication_date<=?
                   AND source=?
                 ORDER BY ticker,publication_date,settlement_date,source""",
            (*tickers, births["short"], as_of, upstream_sources["short"]),
        ):
            ticker = normalize_ticker(row["ticker"])
            if ticker not in ticker_set:
                continue
            settlement = _iso_date(
                row["settlement_date"],
                label="short-interest settlement_date",
            )
            publication = _iso_date(
                row["publication_date"],
                label="short-interest publication_date",
            )
            if settlement > publication:
                raise RuntimeError(
                    f"Stage 5 short-interest publication precedes settlement for {ticker}"
                )
            short_shares = _float(row["short_interest_shares"], nonnegative=True)
            short_float_pct = _float(
                row["short_interest_pct_float"],
                nonnegative=True,
            )
            proxy_shares, proxy_concept, proxy_accepted, proxy_method = _share_proxy_at(
                share_history,
                ticker=ticker,
                available_date=publication,
            )
            if short_float_pct is None and short_shares is not None and proxy_shares:
                short_float_pct = short_shares / proxy_shares
            observation_id = _digest(
                "short-v1",
                ticker,
                settlement,
                publication,
                short_shares,
                short_float_pct,
                row["days_to_cover"],
                proxy_shares,
                proxy_concept,
                proxy_accepted,
            )
            shorts.append((
                ticker, settlement, publication, source_id,
                short_shares,
                short_float_pct,
                _float(row["days_to_cover"], nonnegative=True), now,
                births["short"], observation_id, proxy_shares, proxy_concept,
                proxy_accepted, proxy_method,
            ))

        _require_columns(upstream, "ibkr_borrow_fee_rate_daily", {"ticker", "asof_date", "borrow_fee_rate"})
        for row in upstream.execute(
            f"""SELECT * FROM ibkr_borrow_fee_rate_daily
                 WHERE UPPER(REPLACE(ticker,'.','-')) IN ({placeholders})
                   AND asof_date>=? AND asof_date<=?
                   AND source=?
                 ORDER BY ticker,asof_date,source""",
            (*tickers, births["borrow"], as_of, upstream_sources["borrow"]),
        ):
            ticker = normalize_ticker(row["ticker"])
            if ticker in ticker_set:
                available = _iso_date(row["asof_date"], label="borrow-fee asof_date")
                borrow_by_key.setdefault((ticker, available), {})["fee"] = _float(row["borrow_fee_rate"], nonnegative=True)

        _require_columns(upstream, "ibkr_shortable_shares_snapshots", {"ticker", "asof_date", "shortable_shares"})
        for row in upstream.execute(
            f"""SELECT * FROM ibkr_shortable_shares_snapshots
                 WHERE UPPER(REPLACE(ticker,'.','-')) IN ({placeholders})
                   AND asof_date>=? AND asof_date<=?
                   AND source=?
                 ORDER BY ticker,asof_date,source""",
            (*tickers, births["borrow"], as_of, upstream_sources["borrow"]),
        ):
            ticker = normalize_ticker(row["ticker"])
            if ticker in ticker_set:
                available = _iso_date(row["asof_date"], label="shortable-shares asof_date")
                borrow_by_key.setdefault((ticker, available), {})["available"] = _float(row["shortable_shares"], nonnegative=True)

    borrows: list[tuple[Any, ...]] = []
    for (ticker, available), values in sorted(borrow_by_key.items()):
        observation_id = _digest("borrow-v1", ticker, available, values.get("fee"), values.get("available"))
        borrows.append((ticker, available, source_id, values.get("fee"), values.get("available"), births["borrow"], now, observation_id))

    with conn:
        for table in ("fact_13f_positioning", "fact_short_interest", "fact_borrow_snapshot"):
            conn.execute(
                f"DELETE FROM {table} WHERE source_id=? AND ticker IN ({placeholders})",
                (source_id, *tickers),
            )
        conn.executemany(
            """INSERT INTO fact_13f_positioning(
                   ticker,asof_date,publication_date,source_id,institutional_value,
                   institutional_shares,owner_count,created_at,period_of_report,
                   new_buyer_count,exiting_holder_count,net_buyer_count,
                   institutional_ownership_delta_pct,source_birthdate,source_observation_id
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            institutional,
        )
        conn.executemany(
            """INSERT INTO fact_short_interest(
                   ticker,settlement_date,publication_date,source_id,short_interest,
                   short_float_pct,days_to_cover,created_at,source_birthdate,
                   source_observation_id,float_shares_proxy,float_proxy_concept,
                   float_proxy_accepted_at,float_proxy_method
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            shorts,
        )
        conn.executemany(
            """INSERT INTO fact_borrow_snapshot(
                   ticker,asof_date,source_id,borrow_fee,available_shares,
                   source_birthdate,created_at,source_observation_id
               ) VALUES (?,?,?,?,?,?,?,?)""",
            borrows,
        )
    return {
        "source_id": source_id,
        "upstream_database": str(upstream_path),
        "as_of": as_of,
        "institutional_rows": len(institutional),
        "institutional_tickers": len({row[0] for row in institutional}),
        "short_interest_rows": len(shorts),
        "short_interest_tickers": len({row[0] for row in shorts}),
        "borrow_rows": len(borrows),
        "borrow_tickers": len({row[0] for row in borrows}),
    }


def _latest(conn: sqlite3.Connection, table: str, ticker: str, date_column: str, cutoff: str, source_id: str) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT * FROM {table} WHERE ticker=? AND source_id=? AND {date_column}<=? "
        f"ORDER BY {date_column} DESC, rowid DESC LIMIT 1",
        (ticker, source_id, cutoff),
    ).fetchone()


def build_positioning_features(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    as_of: str,
) -> dict[str, Any]:
    """Build one PIT positioning row per reviewed current/historical ticker."""

    ensure_stage5_schema(conn)
    source_id = str(cfg_get(bundle.payload, "positioning.feature_source_id"))
    ownership_source = str(cfg_get(bundle.payload, "positioning.ownership_source_id"))
    market_source = str(cfg_get(bundle.payload, "positioning.market_positioning_source_id"))
    lookback = int(cfg_get(bundle.payload, "positioning.lookback_days.insider", 90))
    cutoff = f"{as_of}T23:59:59Z"
    window_start = (date.fromisoformat(as_of) - timedelta(days=lookback)).isoformat()
    births = {
        "form4": _source_birthdate(bundle, "sec_form4"),
        "13f": _source_birthdate(bundle, "institutional_13f"),
        "short": _source_birthdate(bundle, "short_interest"),
        "borrow": _source_birthdate(bundle, "borrow"),
    }
    now = utc_now()
    records: list[tuple[Any, ...]] = []
    statuses: dict[str, int] = {}
    for row in _universe(conn):
        ticker = str(row["ticker"])
        ownership = conn.execute(
            """
            SELECT SUM(
                       CASE UPPER(COALESCE(acquired_disposed,''))
                         WHEN 'A' THEN 1.0 WHEN 'D' THEN -1.0 ELSE 0.0 END
                       * shares * price
                   ) AS net_value,
                   COUNT(*) AS event_count,
                   MAX(accepted_at) AS latest_accepted
            FROM fact_sec_ownership_transaction
            WHERE ticker=? AND source_id=? AND is_current_truth=1
              AND accepted_at<=? AND COALESCE(transaction_date,availability_date)>=?
              AND shares IS NOT NULL AND price IS NOT NULL
            """,
            (ticker, ownership_source, cutoff, window_start),
        ).fetchone()
        institutional = _latest(conn, "fact_13f_positioning", ticker, "publication_date", as_of, market_source)
        short = _latest(conn, "fact_short_interest", ticker, "publication_date", as_of, market_source)
        borrow = _latest(conn, "fact_borrow_snapshot", ticker, "asof_date", as_of, market_source)

        insider_value = _float(ownership["net_value"]) if ownership else None
        institutional_flow = _float(institutional["institutional_ownership_delta_pct"]) if institutional else None
        short_pct = _float(short["short_float_pct"], nonnegative=True) if short else None
        short_days = _float(short["days_to_cover"], nonnegative=True) if short else None
        borrow_fee = _float(borrow["borrow_fee"], nonnegative=True) if borrow else None
        short_signal = short_pct if short_pct is not None else short_days
        values = [institutional_flow, short_signal]
        present = sum(value is not None for value in values)
        available_sources = [key for key in ('13f', 'short') if births[key] <= as_of]
        if not available_sources:
            status = "unavailable"
            reason = "all_sources_before_birthdate"
        elif present == 0:
            status = "missing"
            reason = "no_pit_observations_after_source_birthdates"
        elif present < len(available_sources):
            status = "partial"
            reason = "some_pit_sources_missing"
        else:
            status = "complete"
            reason = ""
        statuses[status] = statuses.get(status, 0) + 1
        lineage = {
            "definition_version": POSITIONING_DEFINITION_VERSION,
            "lookback_days": {"insider": lookback},
            "source_birthdates": births,
            "ownership": {
                "source_id": ownership_source,
                "event_count": int(ownership["event_count"] or 0) if ownership else 0,
                "latest_accepted_at": str(ownership["latest_accepted"] or "") if ownership else "",
            },
            "institutional": dict(institutional) if institutional else None,
            "short_interest": dict(short) if short else None,
            "borrow": dict(borrow) if borrow else None,
        }
        records.append((
            "consumer_defensive", ticker, as_of, source_id,
            insider_value, institutional_flow, short_pct, short_days, borrow_fee,
            min(births.values()), status, now, reason,
            json.dumps(lineage, sort_keys=True, separators=(",", ":"), default=str),
            POSITIONING_DEFINITION_VERSION,
        ))

    with conn:
        conn.execute(
            "DELETE FROM feature_positioning WHERE model_family='consumer_defensive' AND asof_date=? AND source_id=?",
            (as_of, source_id),
        )
        conn.executemany(
            """INSERT INTO feature_positioning(
                   model_family,ticker,asof_date,source_id,insider_net_buying,
                   institutional_flow,short_float_pct,short_days_to_cover,borrow_fee,source_birthdate,
                   quality_status,created_at,quality_reason,lineage_json,definition_version
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            records,
        )
    return {"as_of": as_of, "rows": len(records), "quality_status_counts": statuses, "definition_version": POSITIONING_DEFINITION_VERSION}

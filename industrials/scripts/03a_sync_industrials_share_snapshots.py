#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect, init_db  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.core.share_sources import (  # noqa: E402
    ShareObservation,
    load_reviewed_share_observations,
    positive_finite,
    resolve_share_snapshot,
    upsert_observations,
)
from industrials.core.source_registry import load_source_registry, upsert_source_registry  # noqa: E402


LOGGER = logging.getLogger("sync_industrials_share_snapshots")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
FOREIGN_FORMS = frozenset({"20-F", "20-F/A", "40-F", "40-F/A", "6-K"})
IB_OUTSTANDING_KEYS = frozenset(
    {
        "SHARESOUT",
        "SHARESOUTSTANDING",
        "COMMONSHARESOUTSTANDING",
        "TOTALCOMMONSHARESOUTSTANDING",
        "SHARESOUTSTANDINGCURRENT",
    }
)
IB_FLOAT_KEYS = frozenset({"FLOAT", "FLOATSHARES", "PUBLICFLOAT", "FREEFLOATSHARES"})
IB_MARKET_CAP_KEYS = frozenset({"MKTCAP", "MARKETCAP", "MARKETCAPITALIZATION"})
REPORT_FIELDS = (
    "ticker",
    "model_family",
    "asof_date",
    "shares_outstanding",
    "shares_outstanding_source_id",
    "shares_outstanding_method",
    "shares_outstanding_proxy_flag",
    "float_shares",
    "float_shares_source_id",
    "float_shares_method",
    "float_shares_proxy_flag",
    "market_cap",
    "market_cap_source_id",
    "market_cap_method",
    "price",
    "price_source_id",
    "status",
)


@dataclass(frozen=True)
class Company:
    ticker: str
    currency: str
    exchange: str
    evaluation_asof: date


@dataclass(frozen=True)
class Conversion:
    ticker: str
    effective_from: date
    effective_to: date | None
    ratio: float | None
    status: str

    def active_on(self, day: date) -> bool:
        return self.effective_from <= day and (
            self.effective_to is None or day <= self.effective_to
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Load family-scoped share snapshots with separate outstanding-share "
            "and public-float source hierarchies (IB, Yahoo, SEC fallback)."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--model-family", required=True)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--history-start", default="")
    parser.add_argument("--include-historical", action="store_true")
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="Materialize already-loaded Yahoo/SEC facts without live IB/Yahoo requests.",
    )
    parser.add_argument("--skip-ib", action="store_true")
    parser.add_argument("--skip-yahoo", action="store_true")
    parser.add_argument("--tickers", default="", help="Optional comma-separated bounded ticker subset.")
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--output-csv", type=Path, default=None)
    return parser.parse_args()


def parse_day(raw: object, *, field: str) -> date:
    text = str(raw or "").strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid {field}={raw!r}; expected YYYY-MM-DD") from exc


def _date_or_none(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _accepted_day(row: Mapping[str, Any]) -> date | None:
    return _date_or_none(row.get("accepted_at")) or _date_or_none(
        row.get("filing_date")
    ) or _date_or_none(row.get("period_end"))


def load_conversions(path: Path | None) -> dict[str, tuple[Conversion, ...]]:
    if path is None or not path.is_file():
        return {}
    grouped: dict[str, list[Conversion]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            ticker = str(row.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            ratio = positive_finite(row.get("underlying_shares_per_traded_security"))
            item = Conversion(
                ticker=ticker,
                effective_from=parse_day(row.get("effective_from"), field="effective_from"),
                effective_to=(
                    parse_day(row.get("effective_to"), field="effective_to")
                    if str(row.get("effective_to") or "").strip()
                    else None
                ),
                ratio=ratio,
                status=str(row.get("review_status") or "").strip().upper(),
            )
            grouped.setdefault(ticker, []).append(item)
    return {
        ticker: tuple(sorted(items, key=lambda item: item.effective_from))
        for ticker, items in grouped.items()
    }


def conversion_for(
    conversions: Mapping[str, Iterable[Conversion]],
    *,
    ticker: str,
    day: date,
) -> Conversion | None:
    matches = [item for item in conversions.get(ticker.upper(), ()) if item.active_on(day)]
    if len(matches) > 1:
        raise ValueError(f"multiple active share conversions for {ticker} at {day}")
    return matches[0] if matches else None


def load_companies(
    conn: Any,
    *,
    model_family: str,
    history_start: date,
    asof: date,
    include_historical: bool,
) -> list[Company]:
    if include_historical:
        rows = conn.execute(
            """
            SELECT DISTINCT c.ticker, COALESCE(s.currency, c.currency, 'USD') AS currency,
                   COALESCE(s.exchange, '') AS exchange,
                   CASE
                     WHEN MAX(COALESCE(m.end_date, '9999-12-31')) >= ? THEN ?
                     ELSE MAX(m.end_date)
                   END AS evaluation_asof
            FROM dim_universe_membership m
            JOIN dim_company c ON c.company_id = m.company_id
            LEFT JOIN dim_security s ON s.company_id = c.company_id AND s.ticker = c.ticker
            WHERE m.model_family = ? AND m.start_date <= ?
              AND COALESCE(m.end_date, '9999-12-31') >= ?
            GROUP BY c.ticker
            ORDER BY c.ticker
            """,
            (
                asof.isoformat(),
                asof.isoformat(),
                model_family,
                asof.isoformat(),
                history_start.isoformat(),
            ),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT DISTINCT c.ticker, COALESCE(s.currency, c.currency, 'USD') AS currency,
                   COALESCE(s.exchange, '') AS exchange, ? AS evaluation_asof
            FROM dim_universe_membership m
            JOIN dim_company c ON c.company_id = m.company_id
            LEFT JOIN dim_security s ON s.company_id = c.company_id AND s.ticker = c.ticker
            WHERE m.model_family = ? AND m.start_date <= ?
              AND COALESCE(m.end_date, '9999-12-31') >= ?
            GROUP BY c.ticker
            ORDER BY c.ticker
            """,
            (asof.isoformat(), model_family, asof.isoformat(), asof.isoformat()),
        ).fetchall()
    return [
        Company(
            ticker=str(row["ticker"]).upper(),
            currency=str(row["currency"] or "USD"),
            exchange=str(row["exchange"] or ""),
            evaluation_asof=parse_day(row["evaluation_asof"], field="evaluation_asof"),
        )
        for row in rows
    ]


def yahoo_chart_observations(
    conn: Any,
    *,
    companies: Iterable[Company],
    model_family: str,
    history_start: date,
    asof: date,
) -> list[ShareObservation]:
    company_by_ticker = {item.ticker: item for item in companies}
    if not company_by_ticker:
        return []
    placeholders = ",".join("?" for _ in company_by_ticker)
    rows = conn.execute(
        f"""
        SELECT ticker, asof_date, shares_outstanding, market_cap,
               regular_market_price, currency, payload_json
        FROM fact_market_snapshot
        WHERE source_id = 'yahoo_finance_adjusted'
          AND ticker IN ({placeholders})
          AND asof_date >= ? AND asof_date <= ?
        ORDER BY ticker, asof_date
        """,
        (*sorted(company_by_ticker), history_start.isoformat(), asof.isoformat()),
    ).fetchall()
    output: list[ShareObservation] = []
    for row in rows:
        row_day = _date_or_none(row["asof_date"])
        if row_day is None:
            continue
        outstanding = positive_finite(row["shares_outstanding"])
        market_cap = positive_finite(row["market_cap"])
        price = positive_finite(row["regular_market_price"])
        if outstanding is None and market_cap is not None and price is not None:
            outstanding = market_cap / price
            method = "yahoo_chart_market_cap_div_price"
            proxy = True
        else:
            method = "yahoo_chart_shares_outstanding" if outstanding else ""
            proxy = False
        if not any(value is not None for value in (outstanding, market_cap, price)):
            continue
        output.append(
            ShareObservation(
                ticker=str(row["ticker"]),
                model_family=model_family,
                asof_date=row_day,
                source_asof_date=row_day,
                source_id="yahoo_finance_adjusted",
                shares_outstanding=outstanding,
                market_cap=market_cap,
                price=price,
                currency=str(row["currency"] or company_by_ticker[str(row["ticker"])].currency),
                outstanding_method=method,
                outstanding_proxy_flag=proxy,
                payload={"upstream": "fact_market_snapshot"},
            )
        )
    return output


def _price_on_or_before(conn: Any, *, ticker: str, day: date) -> tuple[date, float] | None:
    row = conn.execute(
        """
        SELECT bar_date, close
        FROM fact_price_ohlcv
        WHERE ticker = ? AND bar_date <= ? AND COALESCE(close, 0.0) > 0.0
        ORDER BY bar_date DESC,
                 CASE source_id
                   WHEN 'yahoo_finance_adjusted' THEN 0
                   WHEN 'norgate_us_equities_total_return' THEN 1
                   ELSE 2
                 END
        LIMIT 1
        """,
        (ticker, day.isoformat()),
    ).fetchone()
    if row is None:
        return None
    price_day = _date_or_none(row["bar_date"])
    price = positive_finite(row["close"])
    return (price_day, price) if price_day and price else None


def sec_observations(
    conn: Any,
    *,
    companies: Iterable[Company],
    model_family: str,
    history_start: date,
    asof: date,
    conversions: Mapping[str, Iterable[Conversion]],
) -> tuple[list[ShareObservation], list[str]]:
    # Retain the same bounded pre-window carry-in that the PIT resolver can use.
    # Otherwise the first research dates lose a still-valid SEC observation only
    # because it was accepted shortly before history_start.
    carry_start = history_start - timedelta(days=550)
    company_by_ticker = {item.ticker: item for item in companies}
    if not company_by_ticker:
        return [], []
    placeholders = ",".join("?" for _ in company_by_ticker)
    outstanding_rows = conn.execute(
        f"""
        SELECT ticker, accepted_at, filing_date, period_end, accession_number,
               form_type, taxonomy, concept_name, value, source_priority
        FROM fact_sec_xbrl_fact
        WHERE source_id = 'sec_companyfacts'
          AND canonical_metric = 'shares_outstanding'
          AND ticker IN ({placeholders})
          AND COALESCE(value, 0.0) > 0.0
        ORDER BY ticker, accepted_at, filing_date, period_end, source_priority
        """,
        tuple(sorted(company_by_ticker)),
    ).fetchall()
    public_float_rows = conn.execute(
        f"""
        SELECT ticker, accepted_at, filing_date, period_end, accession_number,
               form_type, taxonomy, concept_name, unit, raw_value
        FROM fact_sec_xbrl_fact_raw
        WHERE source_id = 'sec_companyfacts'
          AND concept_name = 'EntityPublicFloat'
          AND ticker IN ({placeholders})
          AND UPPER(COALESCE(unit, '')) = 'USD'
          AND COALESCE(raw_value, 0.0) > 0.0
        ORDER BY ticker, accepted_at, filing_date, period_end
        """,
        tuple(sorted(company_by_ticker)),
    ).fetchall()
    by_key: dict[tuple[str, date], ShareObservation] = {}
    skipped: list[str] = []
    ranked: dict[tuple[str, date], tuple[tuple[int, int, str], ShareObservation]] = {}
    for raw in outstanding_rows:
        row = dict(raw)
        available = _accepted_day(row)
        measured = _date_or_none(row.get("period_end"))
        ticker = str(row.get("ticker") or "").upper()
        if (
            available is None
            or measured is None
            or available < carry_start
            or available > asof
        ):
            continue
        shares = positive_finite(row.get("value"))
        if shares is None:
            continue
        # A pre-window filing carried into research is resolved under the
        # listing conversion effective on the first date it can be used, not
        # the older financial measurement date.
        conversion_day = max(available, history_start)
        conversion = conversion_for(conversions, ticker=ticker, day=conversion_day)
        foreign = str(row.get("form_type") or "").upper() in FOREIGN_FORMS
        if conversion and conversion.status in {"REVIEWED_ADR", "REVIEWED_DIRECT"} and conversion.ratio:
            shares /= conversion.ratio
            conversion_method = conversion.status.lower()
        elif foreign or (conversion and conversion.status == "PENDING_REVIEW"):
            skipped.append(f"{ticker}:{available}:unreviewed_traded_security_conversion")
            continue
        else:
            conversion_method = "domestic_direct"
        observation = ShareObservation(
            ticker=ticker,
            model_family=model_family,
            asof_date=available,
            source_asof_date=measured,
            source_id="sec_companyfacts",
            shares_outstanding=shares,
            currency=company_by_ticker[ticker].currency,
            outstanding_method=(
                f"sec_{row.get('taxonomy')}_{row.get('concept_name')}_{conversion_method}"
            ),
            outstanding_proxy_flag=False,
            payload={"accession_number": row.get("accession_number"), "form": row.get("form_type")},
        )
        key = (ticker, available)
        rank = (
            int(row.get("source_priority") or 100),
            -measured.toordinal(),
            str(row.get("accession_number") or ""),
        )
        current = ranked.get(key)
        if current is None or rank < current[0]:
            ranked[key] = (rank, observation)
    by_key.update({key: item[1] for key, item in ranked.items()})
    for raw in public_float_rows:
        row = dict(raw)
        available = _accepted_day(row)
        measured = _date_or_none(row.get("period_end"))
        ticker = str(row.get("ticker") or "").upper()
        public_float_usd = positive_finite(row.get("raw_value"))
        if (
            available is None
            or measured is None
            or public_float_usd is None
            or available < carry_start
            or available > asof
        ):
            continue
        price_row = _price_on_or_before(conn, ticker=ticker, day=measured)
        if price_row is None:
            skipped.append(f"{ticker}:{available}:public_float_price_missing")
            continue
        price_day, price = price_row
        float_shares = public_float_usd / price
        key = (ticker, available)
        current = by_key.get(key)
        payload = dict(current.payload or {}) if current else {}
        payload.update(
            {
                "public_float_usd": public_float_usd,
                "public_float_price_date": price_day.isoformat(),
                "public_float_price": price,
                "public_float_accession_number": row.get("accession_number"),
            }
        )
        by_key[key] = ShareObservation(
            ticker=ticker,
            model_family=model_family,
            asof_date=available,
            source_asof_date=measured,
            source_id="sec_companyfacts",
            shares_outstanding=(current.shares_outstanding if current else None),
            float_shares=float_shares,
            currency=company_by_ticker[ticker].currency,
            outstanding_method=(current.outstanding_method if current else ""),
            float_method="sec_entity_public_float_usd_div_unadjusted_close",
            outstanding_proxy_flag=(current.outstanding_proxy_flag if current else False),
            float_proxy_flag=True,
            payload=payload,
        )
    return sorted(by_key.values(), key=lambda item: (item.ticker, item.asof_date)), skipped


def _normalized_key(raw: str) -> str:
    return "".join(character for character in raw.upper() if character.isalnum())


def _parse_ib_ratios(raw: Any) -> tuple[float | None, float | None, float | None]:
    outstanding = None
    float_shares = None
    market_cap = None
    if raw is None:
        return outstanding, float_shares, market_cap
    for key in dir(raw):
        if key.startswith("_"):
            continue
        normalized = _normalized_key(key)
        value = positive_finite(getattr(raw, key, None))
        if value is None:
            continue
        if normalized in IB_OUTSTANDING_KEYS:
            outstanding = value * 1_000_000.0 if value < 1_000_000 else value
        elif normalized in IB_FLOAT_KEYS:
            float_shares = value * 1_000_000.0 if value < 1_000_000 else value
        elif normalized in IB_MARKET_CAP_KEYS:
            market_cap = value
    return outstanding, float_shares, market_cap


def ib_observations(
    companies: Iterable[Company],
    *,
    model_family: str,
    asof: date,
    policy: Mapping[str, Any],
) -> list[ShareObservation]:
    from ib_insync import IB, Stock  # type: ignore[import-not-found]

    logging.getLogger("ib_insync.wrapper").setLevel(logging.CRITICAL)
    logging.getLogger("ib_insync.client").setLevel(logging.WARNING)
    logging.getLogger("ib_insync.ib").setLevel(logging.WARNING)
    ib = IB()
    output: list[ShareObservation] = []
    try:
        ib.connect(
            str(policy.get("ib_host") or "127.0.0.1"),
            int(policy.get("ib_port") or 7497),
            clientId=int(policy.get("ib_client_id") or 7831),
            readonly=True,
            timeout=30,
        )
        ib.reqMarketDataType(int(policy.get("ib_market_data_type") or 1))
        wait_seconds = float(policy.get("ib_snapshot_wait_sec") or 4.0)
        company_list = list(companies)
        batch_size = max(1, int(policy.get("ib_batch_size") or 25))
        for start in range(0, len(company_list), batch_size):
            subscriptions: list[tuple[Company, Any, Any]] = []
            for company in company_list[start : start + batch_size]:
                try:
                    contract = Stock(company.ticker, "SMART", company.currency or "USD")
                    qualified = ib.qualifyContracts(contract)
                    if qualified:
                        contract = qualified[0]
                    ticker_obj = ib.reqMktData(
                        contract,
                        genericTickList=str(policy.get("ib_generic_tick_list") or "258"),
                        snapshot=True,
                        regulatorySnapshot=False,
                    )
                    subscriptions.append((company, contract, ticker_obj))
                except Exception as exc:
                    LOGGER.debug("IB share subscription failed for %s: %s", company.ticker, exc)
            if subscriptions:
                # One wait per bounded batch, not one wait per ticker.
                ib.sleep(wait_seconds)
            for company, contract, ticker_obj in subscriptions:
                try:
                    outstanding, float_shares, market_cap = _parse_ib_ratios(
                        getattr(ticker_obj, "fundamentalRatios", None)
                    )
                    price = positive_finite(getattr(ticker_obj, "marketPrice", lambda: None)())
                    # IB ratios can express market cap in raw currency or millions.
                    # Reconcile it to shares*price before allowing the value into
                    # the primary-source row.
                    if market_cap is not None and outstanding is not None and price is not None:
                        implied_market_cap = outstanding * price
                        raw_ratio = market_cap / implied_market_cap
                        million_ratio = (market_cap * 1_000_000.0) / implied_market_cap
                        if 0.20 <= raw_ratio <= 5.0:
                            pass
                        elif 0.20 <= million_ratio <= 5.0:
                            market_cap *= 1_000_000.0
                        else:
                            market_cap = None
                    if outstanding is None and market_cap is not None and price is not None:
                        outstanding = market_cap / price
                        method = "ib_market_cap_div_price"
                        proxy = True
                    else:
                        method = "ib_fundamental_ratios_shares_outstanding" if outstanding else ""
                        proxy = False
                    if any(value is not None for value in (outstanding, float_shares, market_cap, price)):
                        output.append(
                            ShareObservation(
                                ticker=company.ticker,
                                model_family=model_family,
                                asof_date=asof,
                                source_asof_date=asof,
                                source_id="interactive_brokers_fundamentals",
                                shares_outstanding=outstanding,
                                float_shares=float_shares,
                                market_cap=market_cap,
                                price=price,
                                currency=company.currency,
                                outstanding_method=method,
                                float_method=("ib_fundamental_ratios_public_float" if float_shares else ""),
                                outstanding_proxy_flag=proxy,
                                float_proxy_flag=False,
                                payload={"con_id": int(getattr(contract, "conId", 0) or 0)},
                            )
                        )
                except Exception as exc:
                    LOGGER.debug("IB share snapshot failed for %s: %s", company.ticker, exc)
    finally:
        if ib.isConnected():
            ib.disconnect()
    return output


def _yahoo_company_observation(
    company: Company,
    *,
    model_family: str,
    asof: date,
) -> ShareObservation | None:
    import yfinance as yf  # type: ignore[import-not-found]

    symbol = company.ticker.replace(".", "-")
    ticker_obj = yf.Ticker(symbol)
    raw_info = ticker_obj.get_info()
    info = raw_info if isinstance(raw_info, dict) else {}
    fast_info = ticker_obj.fast_info

    def fast_value(*keys: str) -> object:
        for key in keys:
            try:
                value = fast_info.get(key)
            except Exception:
                value = getattr(fast_info, key, None)
            if value is not None:
                return value
        return None

    outstanding = positive_finite(
        info.get("sharesOutstanding")
        or info.get("impliedSharesOutstanding")
        or fast_value("shares", "shares_outstanding", "sharesOutstanding")
    )
    float_shares = positive_finite(info.get("floatShares"))
    market_cap = positive_finite(
        info.get("marketCap") or fast_value("market_cap", "marketCap")
    )
    price = positive_finite(
        info.get("regularMarketPrice")
        or info.get("currentPrice")
        or info.get("previousClose")
        or fast_value("last_price", "lastPrice", "previous_close", "previousClose")
    )
    proxy = False
    method = "yahoo_shares_outstanding"
    if outstanding is None and market_cap is not None and price is not None:
        outstanding = market_cap / price
        method = "yahoo_market_cap_div_price"
        proxy = True
    if outstanding is None:
        try:
            series = ticker_obj.get_shares_full(
                start=(asof - timedelta(days=370)).isoformat(),
                end=(asof + timedelta(days=1)).isoformat(),
            )
            if series is not None and not getattr(series, "empty", True):
                outstanding = positive_finite(series.dropna().iloc[-1])
                if outstanding is not None:
                    method = "yahoo_shares_full_latest"
                    proxy = False
        except Exception:
            pass
    if not any(value is not None for value in (outstanding, float_shares, market_cap, price)):
        return None
    return ShareObservation(
        ticker=company.ticker,
        model_family=model_family,
        asof_date=asof,
        source_asof_date=asof,
        source_id="yahoo_finance_share_statistics",
        shares_outstanding=outstanding,
        float_shares=float_shares,
        market_cap=market_cap,
        price=price,
        currency=str(info.get("currency") or company.currency),
        outstanding_method=method if outstanding else "",
        float_method="yahoo_float_shares" if float_shares else "",
        outstanding_proxy_flag=proxy,
        float_proxy_flag=False,
        payload={
            "symbol": symbol,
            "sharesOutstanding": info.get("sharesOutstanding"),
            "impliedSharesOutstanding": info.get("impliedSharesOutstanding"),
            "floatShares": info.get("floatShares"),
            "marketCap": info.get("marketCap"),
        },
    )


def yahoo_live_observations(
    companies: Iterable[Company],
    *,
    model_family: str,
    asof: date,
    workers: int,
    request_spacing_sec: float,
) -> list[ShareObservation]:
    output: list[ShareObservation] = []

    def run(company: Company) -> ShareObservation | None:
        if request_spacing_sec > 0:
            time.sleep(request_spacing_sec)
        return _yahoo_company_observation(company, model_family=model_family, asof=asof)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(run, company): company for company in companies}
        for future in as_completed(futures):
            company = futures[future]
            try:
                item = future.result()
                if item is not None:
                    output.append(item)
            except Exception as exc:
                LOGGER.debug("Yahoo share snapshot failed for %s: %s", company.ticker, exc)
    return sorted(output, key=lambda item: item.ticker)


def report_rows(conn: Any, *, companies: Iterable[Company], model_family: str, asof: date) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for company in companies:
        item = resolve_share_snapshot(
            conn,
            ticker=company.ticker,
            model_family=model_family,
            asof=company.evaluation_asof,
        )
        if item.shares_outstanding is None:
            status = "MISSING_OUTSTANDING"
        elif item.float_shares is None:
            status = "OUTSTANDING_ONLY"
        else:
            status = "COMPLETE"
        rows.append(
            {
                field: getattr(item, field, "")
                for field in REPORT_FIELDS
                if field != "status"
            }
            | {
                "asof_date": company.evaluation_asof.isoformat(),
                "shares_outstanding_proxy_flag": int(item.shares_outstanding_proxy_flag),
                "float_shares_proxy_flag": int(item.float_shares_proxy_flag),
                "status": status,
            }
        )
    return rows


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    asof = parse_day(args.asof, field="asof")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    model_family = str(args.model_family).strip()
    family = family_config(config, model_family)
    policy_raw = family.get("share_snapshot_ingestion")
    if not isinstance(policy_raw, dict):
        raise KeyError(f"model_families.{model_family}.share_snapshot_ingestion is required")
    policy: dict[str, Any] = policy_raw
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    source_registry_path = resolve_path(cfg_get(config, "source_registry.path"), base_dir=base_dir)
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(policy["output_csv"], base_dir=base_dir)
    )
    configured_history_start = parse_day(
        args.history_start
        or policy.get("history_start")
        or family.get("historical_load", {}).get("start_date")
        or asof.isoformat(),
        field="history_start",
    )
    # Daily refresh only needs the filing-staleness window. The explicit
    # --include-historical run performs the one-time full materialization.
    history_start = (
        configured_history_start
        if args.include_historical
        else max(configured_history_start, asof - timedelta(days=550))
    )
    conversion_path_raw = policy.get("share_conversion_overrides_csv") or family.get(
        "financial", {}
    ).get("share_conversion_overrides_csv")
    conversion_path = (
        resolve_path(conversion_path_raw, base_dir=base_dir) if conversion_path_raw else None
    )
    conversions = load_conversions(conversion_path)
    reviewed_path_raw = policy.get("reviewed_share_observations_csv")
    reviewed_path = (
        resolve_path(reviewed_path_raw, base_dir=base_dir)
        if reviewed_path_raw
        else None
    )
    source_failures: list[str] = []
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))) as conn:
        init_db(conn)
        upsert_source_registry(conn, load_source_registry(source_registry_path))
        companies = load_companies(
            conn,
            model_family=model_family,
            history_start=history_start,
            asof=asof,
            include_historical=bool(args.include_historical),
        )
        ticker_filter = {
            item.strip().upper()
            for item in str(args.tickers or "").split(",")
            if item.strip()
        }
        if ticker_filter:
            companies = [item for item in companies if item.ticker in ticker_filter]
        if not companies:
            raise ValueError(f"no universe members for model_family={model_family} asof={asof}")
        active_companies = load_companies(
            conn,
            model_family=model_family,
            history_start=history_start,
            asof=asof,
            include_historical=False,
        )
        if ticker_filter:
            active_companies = [item for item in active_companies if item.ticker in ticker_filter]
        observations: list[ShareObservation] = []
        if reviewed_path is not None:
            observations.extend(
                load_reviewed_share_observations(
                    reviewed_path,
                    model_family=model_family,
                    history_start=history_start,
                    asof=asof,
                    allowed_tickers=(company.ticker for company in companies),
                )
            )
        observations.extend(
            yahoo_chart_observations(
                conn,
                companies=companies,
                model_family=model_family,
                history_start=history_start,
                asof=asof,
            )
        )
        sec_rows, sec_skipped = sec_observations(
            conn,
            companies=companies,
            model_family=model_family,
            history_start=history_start,
            asof=asof,
            conversions=conversions,
        )
        observations.extend(sec_rows)
        if sec_skipped:
            LOGGER.info("SEC share observations skipped=%d", len(sec_skipped))
        if not args.local_only and not args.skip_ib and bool(policy.get("enable_ib_fundamentals", True)):
            try:
                ib_rows = ib_observations(
                    active_companies,
                    model_family=model_family,
                    asof=asof,
                    policy=policy,
                )
                observations.extend(ib_rows)
                if not any(
                    row.shares_outstanding is not None or row.float_shares is not None
                    for row in ib_rows
                ):
                    source_failures.append(
                        "IB:no_fundamental_share_entitlement_or_observations"
                    )
            except Exception as exc:
                source_failures.append(f"IB:{type(exc).__name__}:{exc}")
                LOGGER.warning("IB share source unavailable; continuing to Yahoo/SEC: %s", exc)
        if not args.local_only and not args.skip_yahoo and bool(policy.get("enable_yahoo_statistics", True)):
            try:
                observations.extend(
                    yahoo_live_observations(
                        active_companies,
                        model_family=model_family,
                        asof=asof,
                        workers=int(policy.get("yahoo_workers") or 4),
                        request_spacing_sec=float(policy.get("request_spacing_sec") or 0.10),
                    )
                )
            except Exception as exc:
                source_failures.append(f"Yahoo:{type(exc).__name__}:{exc}")
                LOGGER.warning("Yahoo share source unavailable; continuing to SEC: %s", exc)
        with conn:
            written = upsert_observations(conn, observations)
        rows = report_rows(conn, companies=companies, model_family=model_family, asof=asof)
    write_csv_atomic(output_csv, list(REPORT_FIELDS), rows)
    outstanding_count = sum(1 for row in rows if row["status"] != "MISSING_OUTSTANDING")
    float_count = sum(
        1 for row in rows if positive_finite(row.get("float_shares")) is not None
    )
    outstanding_fraction = outstanding_count / len(rows) if rows else 0.0
    minimum = float(policy.get("minimum_outstanding_coverage") or 0.0)
    acceptance = "PASS" if outstanding_fraction >= minimum else "FAIL"
    result = {
        "acceptance": acceptance,
        "model_family": model_family,
        "asof_date": asof.isoformat(),
        "universe_count": len(rows),
        "observations_written": written,
        "shares_outstanding_count": outstanding_count,
        "float_shares_count": float_count,
        "shares_outstanding_coverage": round(outstanding_fraction, 6),
        "float_shares_coverage": round(float_count / len(rows), 6) if rows else 0.0,
        "source_failures": source_failures,
        "sec_skipped_count": len(sec_skipped),
        "output_csv": str(output_csv),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    if acceptance != "PASS" and not args.allow_partial:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

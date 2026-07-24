"""Minimal self-contained price fetchers (urllib only; no sector imports).

Primary source returns Yahoo ``adjclose`` (dividend + split adjusted). Stooq can be probed as a
diagnostic fallback, but its dividend-unadjusted closes are never admitted to the adjusted-price panel.
"""
from __future__ import annotations

import csv
import io
import json
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone


ADJUSTED_OHLCV_FIELDS = (
    "date",
    "adj_open",
    "adj_high",
    "adj_low",
    "adj_close",
    "volume",
)


def _unix(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def _parse_split_events(result: dict) -> list[dict[str, str]]:
    events = (result.get("events") or {}).get("splits") or {}
    out: list[dict[str, str]] = []
    for raw in events.values():
        if not raw.get("date"):
            continue
        split_date = datetime.fromtimestamp(int(raw["date"]), tz=timezone.utc).date().isoformat()
        numerator = str(raw.get("numerator") or "")
        denominator = str(raw.get("denominator") or "")
        split_ratio = str(raw.get("splitRatio") or "")
        out.append({
            "split_date": split_date,
            "numerator": numerator,
            "denominator": denominator,
            "split_ratio": split_ratio,
        })
    return sorted(out, key=lambda r: r["split_date"])


def _fetch_yahoo_adjclose(
    ticker: str,
    *,
    start: date,
    end: date,
    url_template: str,
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
) -> tuple[list[tuple[str, float]], list[dict[str, str]], str]:
    endpoint = url_template.format(ticker=ticker)
    url = (
        f"{endpoint}?period1={_unix(start)}&period2={_unix(end + timedelta(days=1))}"
        f"&interval=1d&events=div%2Csplits"
    )
    last_err = "unknown"
    for attempt in range(max(1, max_retries) + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": user_agent})
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                payload = json.load(resp)
            chart = payload.get("chart") or {}
            result = chart.get("result") or []
            if not result:
                return [], [], f"empty:{chart.get('error')}"
            res = result[0]
            timestamps = res.get("timestamp") or []
            indicators = res.get("indicators") if isinstance(res.get("indicators"), dict) else {}
            adj_block = (indicators.get("adjclose") or [{}])[0]
            adj = adj_block.get("adjclose") if isinstance(adj_block, dict) else None
            if not adj:
                return [], [], "no_adjclose"
            out: list[tuple[str, float]] = []
            for raw_ts, raw_adj in zip(timestamps, adj):
                if raw_adj is None:
                    continue
                day = datetime.fromtimestamp(int(raw_ts), tz=timezone.utc).date().isoformat()
                out.append((day, float(raw_adj)))
            return out, _parse_split_events(res), "ok"
        except urllib.error.HTTPError as exc:
            last_err = f"http_{exc.code}"
            time.sleep((1.5 if exc.code in (429, 502, 503) else 0.5) * (attempt + 1))
        except Exception as exc:  # noqa: BLE001 - network/parse errors route to coverage, not fatal
            last_err = type(exc).__name__
            time.sleep(0.8 * (attempt + 1))
    return [], [], last_err


def _fetch_yahoo_adjusted_ohlcv(
    ticker: str,
    *,
    start: date,
    end: date,
    url_template: str,
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
) -> tuple[list[dict[str, float | str]], str]:
    """Fetch dividend/split-adjusted daily OHLCV from one Yahoo chart host."""
    endpoint = url_template.format(ticker=ticker)
    url = (
        f"{endpoint}?period1={_unix(start)}&period2={_unix(end + timedelta(days=1))}"
        f"&interval=1d&events=div%2Csplits"
    )
    last_err = "unknown"
    for attempt in range(max(1, max_retries) + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": user_agent})
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                payload = json.load(resp)
            chart = payload.get("chart") or {}
            result = chart.get("result") or []
            if not result:
                return [], f"empty:{chart.get('error')}"
            res = result[0]
            timestamps = res.get("timestamp") or []
            indicators = res.get("indicators") if isinstance(res.get("indicators"), dict) else {}
            quote_blocks = indicators.get("quote") or []
            adj_blocks = indicators.get("adjclose") or []
            if not quote_blocks or not adj_blocks:
                return [], "missing_quote_or_adjclose"
            quote = quote_blocks[0] if isinstance(quote_blocks[0], dict) else {}
            adj = adj_blocks[0] if isinstance(adj_blocks[0], dict) else {}
            opens = quote.get("open") or []
            highs = quote.get("high") or []
            lows = quote.get("low") or []
            closes = quote.get("close") or []
            volumes = quote.get("volume") or []
            adjusted = adj.get("adjclose") or []
            out: list[dict[str, float | str]] = []
            for idx, raw_ts in enumerate(timestamps):
                values = [
                    sequence[idx] if idx < len(sequence) else None
                    for sequence in (opens, highs, lows, closes, adjusted)
                ]
                if any(value is None for value in values):
                    continue
                raw_open, raw_high, raw_low, raw_close, adj_close = (
                    float(str(value)) for value in values
                )
                if min(raw_open, raw_high, raw_low, raw_close, adj_close) <= 0:
                    continue
                factor = adj_close / raw_close
                adj_open = raw_open * factor
                adj_high = raw_high * factor
                adj_low = raw_low * factor
                if adj_high + 1e-12 < max(adj_open, adj_close):
                    continue
                if adj_low - 1e-12 > min(adj_open, adj_close):
                    continue
                raw_volume = volumes[idx] if idx < len(volumes) else None
                volume = float(raw_volume) if raw_volume is not None else 0.0
                day = datetime.fromtimestamp(int(raw_ts), tz=timezone.utc).date().isoformat()
                out.append(
                    {
                        "date": day,
                        "adj_open": adj_open,
                        "adj_high": adj_high,
                        "adj_low": adj_low,
                        "adj_close": adj_close,
                        "volume": max(0.0, volume),
                    }
                )
            return out, "ok" if out else "no_complete_ohlcv"
        except urllib.error.HTTPError as exc:
            last_err = f"http_{exc.code}"
            time.sleep((1.5 if exc.code in (429, 502, 503) else 0.5) * (attempt + 1))
        except Exception as exc:  # noqa: BLE001 - network/parse failures are audited by callers
            last_err = type(exc).__name__
            time.sleep(0.8 * (attempt + 1))
    return [], last_err


def fetch_adjusted_ohlcv(
    ticker: str,
    *,
    start: date,
    end: date,
    url_templates: list[str],
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
) -> tuple[list[dict[str, float | str]], str, str, str]:
    """Return adjusted OHLCV rows plus status, provider, and source symbol."""
    statuses: list[str] = []
    for idx, url_template in enumerate(url_templates, start=1):
        rows, status = _fetch_yahoo_adjusted_ohlcv(
            ticker,
            start=start,
            end=end,
            url_template=url_template,
            user_agent=user_agent,
            timeout_sec=timeout_sec,
            max_retries=max_retries,
        )
        provider = f"yahoo_query{idx}"
        if status == "ok" and rows:
            return rows, "ok", provider, ticker
        statuses.append(f"{provider}:{status}")
    return [], "|".join(statuses) if statuses else "no_providers", "", ticker


def _stooq_symbol(ticker: str) -> str:
    return ticker.strip().lower().replace(".", "-") + ".us"


def _fetch_stooq_close(
    ticker: str,
    *,
    start: date,
    end: date,
    user_agent: str,
    timeout_sec: float,
) -> tuple[list[tuple[str, float]], str, str]:
    """Return Stooq daily close rows for US tickers. Provider is fallback-only and audited."""
    symbol = _stooq_symbol(ticker)
    url = (
        "https://stooq.com/q/d/l/"
        f"?s={symbol}&i=d&d1={start.strftime('%Y%m%d')}&d2={end.strftime('%Y%m%d')}"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        rows = list(csv.DictReader(io.StringIO(text)))
        out: list[tuple[str, float]] = []
        for row in rows:
            raw_date = str(row.get("Date", "")).strip()
            raw_close = str(row.get("Close", "")).strip()
            if not raw_date or raw_close in ("", "N/D"):
                continue
            out.append((raw_date, float(raw_close)))
        return out, "ok" if out else "stooq_empty", symbol
    except urllib.error.HTTPError as exc:
        return [], f"stooq_http_{exc.code}", symbol
    except Exception as exc:  # noqa: BLE001 - fallback failures route to coverage
        return [], f"stooq_{type(exc).__name__}", symbol


def fetch_adjclose(
    ticker: str,
    *,
    start: date,
    end: date,
    url_templates: list[str],
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
    enable_stooq_fallback: bool = False,
) -> tuple[list[tuple[str, float]], str, str, str]:
    """Return (rows, status, provider, source_symbol). ``status == "ok"`` on success."""
    rows, _splits, status, provider, source_symbol = fetch_adjclose_with_splits(
        ticker,
        start=start,
        end=end,
        url_templates=url_templates,
        user_agent=user_agent,
        timeout_sec=timeout_sec,
        max_retries=max_retries,
        enable_stooq_fallback=enable_stooq_fallback,
    )
    return rows, status, provider, source_symbol


def fetch_adjclose_with_splits(
    ticker: str,
    *,
    start: date,
    end: date,
    url_templates: list[str],
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
    enable_stooq_fallback: bool = False,
) -> tuple[list[tuple[str, float]], list[dict[str, str]], str, str, str]:
    """Return (rows, split_events, status, provider, source_symbol). ``status == "ok"`` on success."""
    statuses: list[str] = []
    for idx, url_template in enumerate(url_templates, start=1):
        rows, splits, status = _fetch_yahoo_adjclose(
            ticker,
            start=start,
            end=end,
            url_template=url_template,
            user_agent=user_agent,
            timeout_sec=timeout_sec,
            max_retries=max_retries,
        )
        provider = f"yahoo_query{idx}"
        if status == "ok" and rows:
            return rows, splits, "ok", provider, ticker
        statuses.append(f"{provider}:{status}")
    if enable_stooq_fallback:
        rows, status, source_symbol = _fetch_stooq_close(
            ticker,
            start=start,
            end=end,
            user_agent=user_agent,
            timeout_sec=timeout_sec,
        )
        if status == "ok" and rows:
            statuses.append("stooq_us_daily:unadjusted_close_not_admissible")
        else:
            statuses.append(f"stooq_us_daily:{status}")
    return [], [], "|".join(statuses) if statuses else "no_providers", "", ticker


def fetch_splits(
    ticker: str,
    *,
    start: date,
    end: date,
    url_templates: list[str],
    user_agent: str,
    timeout_sec: float,
) -> tuple[list[str], str]:
    """Return ([split effective dates ISO], status). 'ok' even when there are no splits."""
    for url_template in url_templates:
        endpoint = url_template.format(ticker=ticker)
        url = (
            f"{endpoint}?period1={_unix(start)}&period2={_unix(end + timedelta(days=1))}"
            f"&interval=1d&events=splits"
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": user_agent})
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                payload = json.load(resp)
            result = (payload.get("chart") or {}).get("result") or []
            if not result:
                continue
            events = (result[0].get("events") or {}).get("splits") or {}
            dates = [
                datetime.fromtimestamp(int(ev.get("date", 0)), tz=timezone.utc).date().isoformat()
                for ev in events.values()
                if ev.get("date")
            ]
            return sorted(dates), "ok"
        except Exception:  # noqa: BLE001 - try next host, else report failure
            continue
    return [], "split_lookup_failed"

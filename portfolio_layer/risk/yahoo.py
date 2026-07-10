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

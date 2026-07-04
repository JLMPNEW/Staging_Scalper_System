#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, expand_env_vars, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.core.source_registry import load_source_registry, upsert_source_registry  # noqa: E402


LOGGER = logging.getLogger("sync_industrials_yahoo_fx_rates")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
RUN_TYPE = "sync_industrials_yahoo_fx_rates"
REPORT_FIELDS = [
    "currency_pair",
    "from_currency",
    "to_currency",
    "yahoo_symbol",
    "status",
    "loaded_rows",
    "first_rate_date",
    "last_rate_date",
    "latest_fx_rate",
    "error",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync Yahoo Finance FX rates into fact_fx_rate.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--start-date", default="", help="Defaults to fx_rates.start_date.")
    parser.add_argument("--end-date", default="", help="Defaults to today UTC.")
    parser.add_argument("--pairs", default="", help="Optional comma-separated pairs, e.g. CADUSD,GBPUSD.")
    parser.add_argument("--force", action="store_true", help="Ignore cached Yahoo chart JSON.")
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Exit 0 even when some FX pairs fail to sync (default: any failed pair exits 1).",
    )
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--skip-source-registry", action="store_true")
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_pair(raw: object) -> tuple[str, str] | None:
    text = str(raw or "").strip().upper().replace("=X", "").replace("/", "")
    if len(text) != 6 or not text.isalpha():
        return None
    return text[:3], text[3:]


def parse_pair_list(raw: object) -> list[tuple[str, str]]:
    values = raw if isinstance(raw, list) else str(raw or "").split(",")
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        pair = parse_pair(value)
        if pair is not None and pair not in seen:
            out.append(pair)
            seen.add(pair)
    return out


def payload_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def unix_ts(value: date) -> int:
    return int(datetime(value.year, value.month, value.day, tzinfo=timezone.utc).timestamp())


def yahoo_symbol(from_currency: str, to_currency: str) -> str:
    return f"{from_currency}{to_currency}=X"


def chart_url(template: str, *, symbol: str, start: date, end: date, interval: str) -> str:
    end_exclusive = unix_ts(end) + 86400
    return f"{template.format(pair=symbol)}?period1={unix_ts(start)}&period2={end_exclusive}&interval={interval}"


def cache_file(cache_dir: Path, *, source_id: str, pair: str, start: date, end: date) -> Path:
    return cache_dir / source_id / f"{pair}_{start.isoformat()}_{end.isoformat()}.json"


def request_json(url: str, *, user_agent: str, timeout_sec: float, max_retries: int, sleep_sec: float) -> tuple[int, dict[str, Any], str]:
    try:
        import requests  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Package 'requests' is required for Yahoo FX sync.") from exc

    headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
    last_status = 0
    last_error = ""
    for attempt in range(max(1, max_retries) + 1):
        try:
            response = requests.get(url, headers=headers, timeout=timeout_sec)
            last_status = int(response.status_code)
            text = response.text
            if response.status_code == 200:
                return last_status, response.json(), text
            last_error = f"HTTP {last_status}: {text[:200]}"
            if response.status_code not in {429, 500, 502, 503, 504}:
                break
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < max_retries:
            time.sleep(sleep_sec * (attempt + 1))
    raise RuntimeError(f"Yahoo FX request failed status={last_status} url={url} error={last_error}")


def load_or_fetch_json(
    url: str,
    *,
    cache_path: Path,
    force: bool,
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
    sleep_sec: float,
) -> tuple[int, dict[str, Any], str]:
    if cache_path.exists() and not force:
        text = cache_path.read_text(encoding="utf-8")
        try:
            return 200, json.loads(text), text
        except json.JSONDecodeError:
            LOGGER.warning("Corrupt FX cache file %s; deleting and refetching once.", cache_path)
            cache_path.unlink(missing_ok=True)
    status, payload, text = request_json(url, user_agent=user_agent, timeout_sec=timeout_sec, max_retries=max_retries, sleep_sec=sleep_sec)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_path.with_name(cache_path.name + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, cache_path)
    return status, payload, text


def extract_chart_rates(payload: dict[str, Any]) -> list[tuple[str, float]]:
    result = ((payload.get("chart") or {}).get("result") or [])
    if not result:
        return []
    first = result[0]
    timestamps = first.get("timestamp") or []
    quotes = ((first.get("indicators") or {}).get("quote") or [])
    closes = quotes[0].get("close") if quotes else []
    if not isinstance(timestamps, list) or not isinstance(closes, list):
        return []
    rows: list[tuple[str, float]] = []
    for timestamp, close in zip(timestamps, closes, strict=False):
        if close is None:
            continue
        try:
            rate = float(close)
        except (TypeError, ValueError):
            continue
        if rate <= 0:
            continue
        rate_date = datetime.fromtimestamp(int(timestamp), tz=timezone.utc).date().isoformat()
        rows.append((rate_date, rate))
    return rows


def discover_required_pairs(conn: Any, *, configured_pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen = set(configured_pairs)
    rows = conn.execute(
        """
        SELECT DISTINCT UPPER(unit) AS currency
        FROM fact_sec_xbrl_fact
        WHERE LENGTH(unit) = 3
          AND unit GLOB '[A-Za-z][A-Za-z][A-Za-z]'
          AND UPPER(unit) <> 'USD'
        UNION
        SELECT DISTINCT UPPER(currency) AS currency
        FROM dim_company
        WHERE LENGTH(currency) = 3
          AND currency GLOB '[A-Za-z][A-Za-z][A-Za-z]'
          AND UPPER(currency) <> 'USD'
        """
    ).fetchall()
    for row in rows:
        currency = str(row["currency"] or "").upper()
        if currency and currency != "USD":
            seen.add((currency, "USD"))
    return sorted(seen)


def record_raw_response(conn: Any, *, source_id: str, endpoint: str, status: int, payload_text: str, asof_date: str) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO raw_api_responses(
            source_id, endpoint, query_params_json, request_time_utc, response_status,
            response_hash, asof_date, payload_text, ingestion_run_id, created_at
        )
        VALUES (?, ?, '{}', ?, ?, ?, ?, ?, NULL, ?)
        """,
        (source_id, endpoint, now, status, payload_hash(payload_text), asof_date, payload_text, now),
    )


def update_source_active(conn: Any, *, source_id: str) -> None:
    row = conn.execute(
        "SELECT status, notes FROM source_registry WHERE source_id = ?",
        (source_id,),
    ).fetchone()
    if row is None:
        return
    current_status = str(row["status"] or "")
    current_notes = str(row["notes"] or "")
    marker = "FX loader enabled."
    new_notes = current_notes if marker in current_notes else f"{current_notes} {marker}"
    if current_status == "active" and new_notes == current_notes:
        return
    conn.execute(
        "UPDATE source_registry SET status = 'active', notes = ?, updated_at = ? WHERE source_id = ?",
        (new_notes, utc_now(), source_id),
    )


def upsert_rates(conn: Any, *, source_id: str, from_currency: str, to_currency: str, rows: list[tuple[str, float]]) -> int:
    now = utc_now()
    pair = f"{from_currency}{to_currency}"
    inserted = 0
    for rate_date, rate in rows:
        conn.execute(
            """
            INSERT INTO fact_fx_rate(
                currency_pair, rate_date, source_id, from_currency, to_currency,
                fx_rate, rate_type, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'spot_close', ?, ?)
            ON CONFLICT(currency_pair, rate_date, source_id) DO UPDATE SET
                from_currency = excluded.from_currency,
                to_currency = excluded.to_currency,
                fx_rate = excluded.fx_rate,
                rate_type = excluded.rate_type,
                updated_at = excluded.updated_at
            """,
            (pair, rate_date, source_id, from_currency, to_currency, rate, now, now),
        )
        inserted += 1
    return inserted


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    source_id = str(cfg_get(config, "fx_rates.source_id", "yahoo_fx_rates") or "yahoo_fx_rates")
    start = parse_date(args.start_date) or parse_date(cfg_get(config, "fx_rates.start_date", "2010-01-01"))
    end = parse_date(args.end_date) or datetime.now(timezone.utc).date()
    if start is None or end < start:
        raise ValueError(f"Invalid FX date range start={args.start_date!r} end={args.end_date!r}")
    configured_pairs = parse_pair_list(args.pairs) or parse_pair_list(cfg_get(config, "fx_rates.required_pairs", []) or [])
    template = str(cfg_get(config, "fx_rates.chart_url_template") or "https://query1.finance.yahoo.com/v8/finance/chart/{pair}")
    interval = str(cfg_get(config, "fx_rates.interval", "1d") or "1d")
    user_agent = expand_env_vars(cfg_get(config, "fx_rates.user_agent", "") or "").strip()
    if not user_agent or "@" not in user_agent:
        raise ValueError(
            "fx_rates.user_agent must expand to a non-empty identity with a contact email "
            f"(got {user_agent!r}); set fx_rates.user_agent in config or the referenced env var."
        )
    timeout_sec = float(cfg_get(config, "fx_rates.timeout_sec", 30.0))
    max_retries = int(cfg_get(config, "fx_rates.max_retries", 3))
    sleep_sec = float(cfg_get(config, "fx_rates.request_sleep_sec", 0.12))
    cache_dir = resolve_path(cfg_get(config, "fx_rates.cache_dir", "../output/industrials_cache/yahoo_fx_rates"), base_dir=base_dir)
    output_csv = args.output_csv.expanduser().resolve() if args.output_csv else resolve_path(cfg_get(config, "fx_rates.output_csv"), base_dir=base_dir)

    report_rows: list[dict[str, Any]] = []
    loaded_rows = 0
    with closing(connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0)))) as conn:
        init_db(conn)
        if not args.skip_source_registry:
            registry_path = resolve_path(cfg_get(config, "source_registry.path"), base_dir=base_dir)
            with conn:
                upsert_source_registry(conn, load_source_registry(registry_path))
        pairs = discover_required_pairs(conn, configured_pairs=configured_pairs)
        run_id = start_run(conn, run_type=RUN_TYPE, input_path=config_path)
        try:
            for from_currency, to_currency in pairs:
                pair = f"{from_currency}{to_currency}"
                symbol = yahoo_symbol(from_currency, to_currency)
                url = chart_url(template, symbol=symbol, start=start, end=end, interval=interval)
                status = "loaded"
                error = ""
                rows: list[tuple[str, float]] = []
                try:
                    response_status, payload, payload_text = load_or_fetch_json(
                        url,
                        cache_path=cache_file(cache_dir, source_id=source_id, pair=pair, start=start, end=end),
                        force=args.force,
                        user_agent=user_agent,
                        timeout_sec=timeout_sec,
                        max_retries=max_retries,
                        sleep_sec=sleep_sec,
                    )
                    rows = extract_chart_rates(payload)
                    if not rows:
                        status = "no_rates"
                    with conn:
                        record_raw_response(conn, source_id=source_id, endpoint=url, status=response_status, payload_text=payload_text, asof_date=end.isoformat())
                        if rows:
                            loaded_rows += upsert_rates(
                                conn,
                                source_id=source_id,
                                from_currency=from_currency,
                                to_currency=to_currency,
                                rows=rows,
                            )
                            update_source_active(conn, source_id=source_id)
                except Exception as exc:  # noqa: BLE001
                    status = "error"
                    error = repr(exc)
                report_rows.append(
                    {
                        "currency_pair": pair,
                        "from_currency": from_currency,
                        "to_currency": to_currency,
                        "yahoo_symbol": symbol,
                        "status": status,
                        "loaded_rows": len(rows) if status == "loaded" else 0,
                        "first_rate_date": rows[0][0] if rows else "",
                        "last_rate_date": rows[-1][0] if rows else "",
                        "latest_fx_rate": round(rows[-1][1], 8) if rows else "",
                        "error": error,
                    }
                )
                time.sleep(sleep_sec)
            write_csv_atomic(output_csv, REPORT_FIELDS, report_rows)
            failed = sum(1 for row in report_rows if row["status"] != "loaded")
            finish_run(conn, run_id=run_id, status="success" if failed == 0 else "partial", row_count=loaded_rows, message=f"pairs={len(report_rows)} failed={failed} rows={loaded_rows} output={output_csv}")
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=loaded_rows, message=f"{type(exc).__name__}: {exc}")
            raise
        if failed > 0 and not args.allow_partial:
            failed_pairs = sorted(str(row["currency_pair"]) for row in report_rows if row["status"] != "loaded")
            LOGGER.error(
                "FX sync failed for %d/%d pairs (%s); see %s. Rerun, or pass --allow-partial to accept partial FX coverage.",
                failed,
                len(report_rows),
                ",".join(failed_pairs),
                output_csv,
            )
            raise SystemExit(1)

    LOGGER.info("Wrote FX coverage report: %s", output_csv)
    LOGGER.info("Yahoo FX sync complete: pairs=%d rows=%d", len(report_rows), loaded_rows)


if __name__ == "__main__":
    main()

"""Point-in-time reporting-currency FX ingestion for Basic Materials Stage 4B."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterable, Mapping
from urllib.parse import urlencode

import requests

from basic_materials.core.atomic_io import atomic_write_bytes, atomic_write_csv, atomic_write_json
from basic_materials.core.config import BasicMaterialsConfig
from basic_materials.core.db import assert_database_identity, utc_now
from basic_materials.core.financial_ingestion import (
    FinancialIngestionError,
    FinancialIngestionPolicy,
)


@dataclass(frozen=True)
class FxObservation:
    base_currency: str
    quote_currency: str
    rate_date: str
    rate: float
    source_timestamp_utc: str
    payload_sha256: str


@dataclass(frozen=True)
class FxSyncStats:
    snapshot_key: str
    currencies: tuple[str, ...]
    observations: int
    first_rate_date: str
    last_rate_date: str
    cache_manifest_sha256: str
    failures: tuple[Mapping[str, str], ...]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["currencies"] = list(self.currencies)
        payload["failures"] = [dict(item) for item in self.failures]
        return payload


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _unix(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def latest_financial_snapshot(
    conn: sqlite3.Connection,
    *,
    as_of_date: str | None = None,
) -> sqlite3.Row:
    query = "SELECT * FROM fact_financial_ingestion_snapshot"
    params: tuple[Any, ...] = ()
    if as_of_date:
        date.fromisoformat(as_of_date)
        query += " WHERE extraction_asof_date <= ?"
        params = (as_of_date,)
    query += " ORDER BY extraction_asof_date DESC, created_at_utc DESC LIMIT 1"
    row = conn.execute(query, params).fetchone()
    if row is None:
        raise FinancialIngestionError("No Stage 4B financial snapshot is available")
    return row


def financial_currencies(
    conn: sqlite3.Connection,
    *,
    snapshot_key: str,
) -> list[str]:
    values = {
        str(row[0]).upper()
        for row in conn.execute(
            """
            SELECT DISTINCT unit
            FROM fact_sec_xbrl_fact_raw
            WHERE snapshot_key = ? AND quality_status = 'usable'
              AND length(unit) = 3 AND unit NOT GLOB '*[^A-Za-z]*'
            UNION
            SELECT DISTINCT effective_reporting_currency
            FROM dim_financial_profile_resolution
            WHERE canonical_eligible = 1
              AND length(effective_reporting_currency) = 3
              AND effective_reporting_currency NOT GLOB '*[^A-Za-z]*'
            """,
            (snapshot_key,),
        ).fetchall()
        if str(row[0] or "")
    }
    values.add("USD")
    return sorted(values)


def fx_symbol_candidates(base_currency: str) -> tuple[tuple[str, bool], ...]:
    currency = base_currency.upper()
    if currency == "USD":
        return (("USDUSD=X", False),)
    return (
        (f"{currency}USD=X", False),
        (f"USD{currency}=X", True),
        (f"{currency}=X", True),
    )


def parse_yahoo_rates(
    payload: Mapping[str, Any],
    *,
    base_currency: str,
    invert: bool,
    payload_sha256: str,
) -> list[FxObservation]:
    chart = payload.get("chart")
    if not isinstance(chart, Mapping):
        return []
    results = chart.get("result")
    if not isinstance(results, list) or not results or not isinstance(results[0], Mapping):
        return []
    result = results[0]
    timestamps = result.get("timestamp")
    indicators = result.get("indicators")
    if not isinstance(timestamps, list) or not isinstance(indicators, Mapping):
        return []
    quote = indicators.get("quote")
    if not isinstance(quote, list) or not quote or not isinstance(quote[0], Mapping):
        return []
    closes = quote[0].get("close")
    if not isinstance(closes, list):
        return []
    metadata = result.get("meta") if isinstance(result.get("meta"), Mapping) else {}
    source_time = metadata.get("regularMarketTime")
    try:
        source_timestamp = datetime.fromtimestamp(int(source_time), tz=timezone.utc).replace(
            microsecond=0
        ).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        source_timestamp = ""
    observations: list[FxObservation] = []
    for raw_timestamp, raw_close in zip(timestamps, closes):
        value = _safe_float(raw_close)
        if value is None:
            continue
        rate = 1.0 / value if invert else value
        try:
            rate_date = datetime.fromtimestamp(int(raw_timestamp), tz=timezone.utc).date().isoformat()
        except (TypeError, ValueError, OSError):
            continue
        observations.append(
            FxObservation(
                base_currency=base_currency.upper(),
                quote_currency="USD",
                rate_date=rate_date,
                rate=rate,
                source_timestamp_utc=source_timestamp,
                payload_sha256=payload_sha256,
            )
        )
    return observations


def _fetch_yahoo_payload(
    *,
    base_currency: str,
    start: date,
    end: date,
    cache_root: Path,
    policy: FinancialIngestionPolicy,
    user_agent: str,
    cache_only: bool,
) -> tuple[list[FxObservation], dict[str, Any]]:
    fx_policy = policy.payload["fx"]
    timeout = float(fx_policy["request_timeout_seconds"])
    retries = int(fx_policy["max_retries"])
    interval = float(fx_policy["request_interval_seconds"])
    template = str(fx_policy["chart_url_template"])
    last_reason = ""
    for symbol, invert in fx_symbol_candidates(base_currency):
        params = {
            "period1": _unix(start),
            "period2": _unix(end + timedelta(days=1)),
            "interval": "1d",
            "events": "history",
        }
        endpoint = template.format(symbol=symbol)
        url = f"{endpoint}?{urlencode(params)}"
        safe_symbol = "".join(char if char.isalnum() else "_" for char in symbol)
        path = (cache_root / f"{safe_symbol}_{start.isoformat()}_{end.isoformat()}.json").resolve()
        if path.is_file():
            raw = path.read_bytes()
            status = "cache_hit"
        else:
            if cache_only:
                last_reason = f"missing_cache:{path.name}"
                continue
            raw = b""
            for attempt in range(retries):
                try:
                    response = requests.get(
                        endpoint,
                        params=params,
                        headers={"User-Agent": user_agent, "Accept": "application/json"},
                        timeout=timeout,
                    )
                    if response.status_code == 200:
                        raw = response.content
                        break
                    last_reason = f"{symbol}:HTTP_{response.status_code}"
                    if response.status_code not in {429, 500, 502, 503, 504}:
                        break
                except requests.RequestException as exc:
                    last_reason = f"{symbol}:{type(exc).__name__}"
                if attempt + 1 < retries:
                    time.sleep(max(interval, 0.25) * (attempt + 1))
            if not raw:
                continue
            atomic_write_bytes(path, raw)
            status = "downloaded"
            time.sleep(interval)
        digest = _sha256_bytes(raw)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            last_reason = f"{symbol}:invalid_json"
            continue
        observations = parse_yahoo_rates(
            payload,
            base_currency=base_currency,
            invert=invert,
            payload_sha256=digest,
        )
        observations = [item for item in observations if start.isoformat() <= item.rate_date <= end.isoformat()]
        if observations:
            return observations, {
                "base_currency": base_currency,
                "symbol": symbol,
                "invert": invert,
                "url": url,
                "cache_path": str(path),
                "sha256": digest,
                "byte_size": len(raw),
                "status": status,
            }
        last_reason = f"{symbol}:no_usable_rates"
    raise FinancialIngestionError(f"No Yahoo FX series for {base_currency}: {last_reason}")


def _usd_identity_rates(
    conn: sqlite3.Connection,
    *,
    start: date,
    end: date,
) -> tuple[list[FxObservation], dict[str, Any]]:
    dates = [
        str(row[0])
        for row in conn.execute(
            """
            SELECT session_date
            FROM dim_trading_calendar_session
            WHERE calendar_code = 'XNYS_PROXY_SPY'
              AND session_date BETWEEN ? AND ?
            ORDER BY session_date
            """,
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    ]
    if not dates:
        raise FinancialIngestionError("Stage 3 trading calendar is required for USD identity FX")
    raw = json.dumps(dates, separators=(",", ":")).encode("utf-8")
    digest = _sha256_bytes(raw)
    timestamp = f"{end.isoformat()}T23:59:59Z"
    rates = [
        FxObservation(
            base_currency="USD",
            quote_currency="USD",
            rate_date=day,
            rate=1.0,
            source_timestamp_utc=timestamp,
            payload_sha256=digest,
        )
        for day in dates
    ]
    return rates, {
        "base_currency": "USD",
        "symbol": "USD_IDENTITY",
        "invert": False,
        "url": "stage3://XNYS_PROXY_SPY",
        "cache_path": "",
        "sha256": digest,
        "byte_size": len(raw),
        "status": "derived_identity",
    }


def _seal_fx_manifest(
    cache_root: Path,
    *,
    policy: FinancialIngestionPolicy,
    snapshot_key: str,
    records: Iterable[Mapping[str, Any]],
) -> tuple[str, Path]:
    normalized = [
        {
            **{key: record[key] for key in ("base_currency", "symbol", "invert", "url", "sha256", "byte_size")},
            "cache_path": str(record.get("cache_path") or ""),
            "status": "available" if record.get("cache_path") else str(record["status"]),
        }
        for record in records
    ]
    normalized.sort(key=lambda item: str(item["base_currency"]))
    projection = {
        "artifact_id": "basic_materials_fx_cache_v1",
        "snapshot_key": snapshot_key,
        "policy_sha256": policy.checksum,
        "records": normalized,
    }
    digest = _sha256_bytes(json.dumps(projection, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    path = cache_root / "fx_cache_manifest.json"
    payload = {**projection, "manifest_sha256": digest}
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        existing_projection = {key: value for key, value in existing.items() if key != "manifest_sha256"}
        existing_digest = _sha256_bytes(
            json.dumps(existing_projection, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        if existing.get("manifest_sha256") != existing_digest or existing_digest != digest:
            raise FinancialIngestionError("Existing immutable FX cache manifest differs")
    else:
        atomic_write_json(path, payload)
    return digest, path


def sync_financial_fx_rates(
    conn: sqlite3.Connection,
    *,
    config: BasicMaterialsConfig,
    policy: FinancialIngestionPolicy,
    snapshot_key: str | None = None,
    cache_only: bool = False,
    allow_partial: bool = False,
) -> FxSyncStats:
    """Fetch, cache, and atomically load all reporting-currency/USD rates."""

    assert_database_identity(conn)
    snapshot = (
        conn.execute(
            "SELECT * FROM fact_financial_ingestion_snapshot WHERE snapshot_key = ?",
            (snapshot_key,),
        ).fetchone()
        if snapshot_key
        else latest_financial_snapshot(conn, as_of_date=policy.as_of_date)
    )
    if snapshot is None:
        raise FinancialIngestionError("Requested financial snapshot does not exist")
    snapshot_key = str(snapshot["snapshot_key"])
    if str(snapshot["policy_sha256"]) != policy.checksum:
        raise FinancialIngestionError("FX policy does not match the financial snapshot")
    currencies = financial_currencies(conn, snapshot_key=snapshot_key)
    fx_policy = policy.payload["fx"]
    start = date.fromisoformat(str(fx_policy["start_date"]))
    end = date.fromisoformat(str(fx_policy["end_date"]))
    cache_base = (
        config.paths.cache_root
        / str(policy.payload["immutable_cache"]["ingestion_cache_relative_path"])
        / "fx"
    )
    observations: list[FxObservation] = []
    records: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for currency in currencies:
        try:
            if currency == "USD":
                rates, record = _usd_identity_rates(conn, start=start, end=end)
            else:
                rates, record = _fetch_yahoo_payload(
                    base_currency=currency,
                    start=start,
                    end=end,
                    cache_root=cache_base,
                    policy=policy,
                    user_agent=config.sec_fundamentals.user_agent,
                    cache_only=cache_only,
                )
            observations.extend(rates)
            records.append(record)
        except FinancialIngestionError as exc:
            failures.append({"currency": currency, "reason": str(exc)})
            if not allow_partial:
                raise
    manifest_sha, _ = _seal_fx_manifest(
        cache_base,
        policy=policy,
        snapshot_key=snapshot_key,
        records=records,
    )
    now = utc_now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for item in observations:
            existing = conn.execute(
                """
                SELECT rate, payload_sha256
                FROM fact_fx_rate
                WHERE base_currency = ? AND quote_currency = ? AND rate_date = ?
                  AND source_id = 'yahoo_fx_rates'
                """,
                (item.base_currency, item.quote_currency, item.rate_date),
            ).fetchone()
            if existing is not None and (
                abs(float(existing["rate"]) - item.rate) > 1e-12
                or str(existing["payload_sha256"]) != item.payload_sha256
            ):
                raise FinancialIngestionError(
                    f"Immutable FX observation changed for {item.base_currency} {item.rate_date}"
                )
            conn.execute(
                """
                INSERT INTO fact_fx_rate (
                    base_currency, quote_currency, rate_date, rate, source_id,
                    source_timestamp_utc, payload_sha256, snapshot_key,
                    quality_status, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, 'yahoo_fx_rates', ?, ?, ?, 'usable', ?, ?)
                ON CONFLICT(base_currency, quote_currency, rate_date, source_id) DO NOTHING
                """,
                (
                    item.base_currency,
                    item.quote_currency,
                    item.rate_date,
                    item.rate,
                    item.source_timestamp_utc,
                    item.payload_sha256,
                    snapshot_key,
                    now,
                    now,
                ),
            )
        count = int(
            conn.execute(
                "SELECT COUNT(*) FROM fact_fx_rate WHERE snapshot_key = ?",
                (snapshot_key,),
            ).fetchone()[0]
        )
        details = json.loads(str(snapshot["details_json"]) or "{}")
        details["fx_cache_manifest_sha256"] = manifest_sha
        details["fx_currencies"] = currencies
        details["fx_failures"] = failures
        conn.execute(
            """
            UPDATE fact_financial_ingestion_snapshot
            SET fx_observation_count = ?, details_json = ?
            WHERE snapshot_key = ?
            """,
            (count, json.dumps(details, sort_keys=True), snapshot_key),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    dates = [item.rate_date for item in observations]
    return FxSyncStats(
        snapshot_key=snapshot_key,
        currencies=tuple(currencies),
        observations=len(observations),
        first_rate_date=min(dates) if dates else "",
        last_rate_date=max(dates) if dates else "",
        cache_manifest_sha256=manifest_sha,
        failures=tuple(failures),
    )


def write_fx_reports(
    *,
    stats: FxSyncStats,
    report_dir: str | Path,
) -> dict[str, str]:
    target = Path(report_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    summary = atomic_write_json(target / "fx_sync_summary.json", stats.as_dict())
    failures = atomic_write_csv(
        target / "fx_sync_failures.csv",
        [dict(item) for item in stats.failures],
        ("currency", "reason"),
    )
    artifacts = {"summary": str(summary), "failures": str(failures)}
    manifest = atomic_write_json(
        target / "artifact_manifest.json",
        {
            "stage": "stage4b_fx",
            "snapshot_key": stats.snapshot_key,
            "artifacts": [
                {
                    "artifact": name,
                    "path": path,
                    "sha256": _sha256_path(Path(path)),
                    "byte_size": Path(path).stat().st_size,
                }
                for name, path in sorted(artifacts.items())
            ],
        },
    )
    artifacts["artifact_manifest"] = str(manifest)
    return artifacts

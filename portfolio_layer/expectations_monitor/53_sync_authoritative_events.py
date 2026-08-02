#!/usr/bin/env python3
"""Ingest structured local event sources into the immutable monitor raw-item store."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.expectations_monitor.monitor_common import (  # noqa: E402
    connect_monitor_db,
    fetch_universe_snapshot,
    utc_now,
    database_writer_lock,
    monitor_output_subdir,
)
from portfolio_layer.expectations_monitor.state_common import (  # noqa: E402
    append_raw_items,
    digest,
    ensure_state_schema,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
RAW_FIELDS = [
    "item_id", "source", "source_uid", "ticker_hint", "published_at_utc",
    "fetched_at_utc", "title", "summary", "url", "payload_json",
    "content_sha256", "status",
]
SOURCE_FIELDS = ["source", "status", "row_count", "detail"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--universe-as-of", type=date.fromisoformat)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def _iso_timestamp(value: Any, *, fallback_date: str) -> str:
    text = str(value or "").strip().replace(" ", "T", 1)
    if text:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()
        except ValueError:
            pass
    return f"{fallback_date}T21:00:00+00:00"


def _chunks(values: list[str], size: int = 800) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _form4_items(
    path: Path,
    *,
    tickers: list[str],
    as_of: str,
    lookback_days: int,
    fetched_at: str,
) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    floor = (date.fromisoformat(as_of) - timedelta(days=lookback_days)).isoformat()
    clusters: dict[tuple[str, str, str], dict[str, Any]] = {}
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        for batch in _chunks(tickers):
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"""
                SELECT event_key,issuer_trading_symbol,signal_side,filing_date_sort,
                       accepted_ts_utc,cluster_insiders_10bd,event_score,accession_number
                FROM form4_events_tier1
                WHERE is_current_truth=1
                  AND issuer_trading_symbol IN ({placeholders})
                  AND filing_date_sort BETWEEN ? AND ?
                  AND signal_side IN ('BUY','SELL')
                  AND COALESCE(cluster_insiders_10bd,0)>=2
                ORDER BY filing_date_sort,event_key
                """,
                (*batch, floor, as_of),
            ).fetchall()
            for row in rows:
                side = str(row["signal_side"]).upper()
                event_date = str(row["filing_date_sort"])
                ticker = str(row["issuer_trading_symbol"]).upper()
                key = (ticker, side, event_date)
                aggregate = clusters.setdefault(
                    key,
                    {
                        "accepted": [],
                        "event_keys": [],
                        "accessions": [],
                        "cluster_insiders": 0,
                        "source_score": 0.0,
                    },
                )
                aggregate["accepted"].append(
                    _iso_timestamp(row["accepted_ts_utc"], fallback_date=event_date)
                )
                aggregate["event_keys"].append(str(row["event_key"]))
                aggregate["accessions"].append(str(row["accession_number"] or ""))
                aggregate["cluster_insiders"] = max(
                    int(aggregate["cluster_insiders"]),
                    int(row["cluster_insiders_10bd"] or 0),
                )
                aggregate["source_score"] = max(
                    float(aggregate["source_score"]),
                    abs(float(row["event_score"] or 0.0)),
                )
    finally:
        conn.close()
    output: list[dict[str, Any]] = []
    for (ticker, side, event_date), aggregate in sorted(clusters.items()):
        event_keys = sorted(set(aggregate["event_keys"]))
        output.append(
            {
                "source": "form4",
                "source_uid": f"{ticker}:{side}:{event_date}:cluster10",
                "ticker_hint": ticker,
                "published_at_utc": max(aggregate["accepted"]),
                "fetched_at_utc": fetched_at,
                "title": f"Form 4 {side} cluster",
                "summary": "One economic cluster event aggregated from Staging-owned SEC filings",
                "url": "",
                "payload": {
                    "kind": "form4_cluster",
                    "event_date": event_date,
                    "direction": 1.0 if side == "BUY" else -1.0,
                    "cluster_insiders_10bd": int(aggregate["cluster_insiders"]),
                    "source_score": float(aggregate["source_score"]),
                    "source_event_keys": event_keys,
                    "accession_numbers": sorted(
                        value for value in set(aggregate["accessions"]) if value
                    ),
                    "rationale": "at least two distinct insiders in one ticker/side/date cluster",
                },
            }
        )
    return output


def _guidance_items(
    path: Path,
    *,
    tickers: list[str],
    as_of: str,
    lookback_days: int,
    fetched_at: str,
) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    floor = (date.fromisoformat(as_of) - timedelta(days=lookback_days)).isoformat()
    all_rows: list[dict[str, Any]] = []
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        for batch in _chunks(tickers):
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"""
                SELECT guidance_id,guidance_unique_key,ticker,filing_date,metric,
                       midpoint_value,confidence,accession_nodash,created_at
                FROM company_forward_guidance
                WHERE ticker IN ({placeholders}) AND filing_date<=?
                ORDER BY ticker,metric,filing_date,guidance_id
                """,
                (*batch, as_of),
            ).fetchall()
            all_rows.extend(dict(row) for row in rows)
    finally:
        conn.close()
    prior: dict[tuple[str, str], float] = {}
    output: list[dict[str, Any]] = []
    for row in all_rows:
        ticker = str(row["ticker"]).upper()
        metric = str(row["metric"] or "").casefold()
        midpoint_raw = row["midpoint_value"]
        if midpoint_raw is None:
            continue
        midpoint = float(midpoint_raw)
        key = (ticker, metric)
        previous = prior.get(key)
        prior[key] = midpoint
        event_date = str(row["filing_date"])
        if event_date < floor or previous is None:
            continue
        scale = max(abs(previous), abs(midpoint), 1e-9)
        change = (midpoint - previous) / scale
        if abs(change) < 0.005:
            direction = 0.2
        else:
            direction = 1.0 if change > 0 else -1.0
        output.append(
            {
                "source": "biotech_guidance",
                "source_uid": str(row["guidance_unique_key"]),
                "ticker_hint": ticker,
                "published_at_utc": _iso_timestamp(row["created_at"], fallback_date=event_date),
                "fetched_at_utc": fetched_at,
                "title": f"Structured {metric} guidance update",
                "summary": "Point-in-time issuer guidance record from biotech sector database",
                "url": "",
                "payload": {
                    "kind": "guidance_change",
                    "event_date": event_date,
                    "direction": direction,
                    "metric": metric,
                    "prior_midpoint": previous,
                    "current_midpoint": midpoint,
                    "relative_change": change,
                    "credibility": max(0.0, min(1.0, float(row["confidence"] or 0.0))),
                    "accession_number": str(row["accession_nodash"] or ""),
                    "rationale": "structured company guidance midpoint compared with prior PIT guidance",
                },
            }
        )
    return output


def _provider_items(
    conn: sqlite3.Connection,
    *,
    tickers: list[str],
    as_of: str,
    lookback_days: int,
    active_period_grace_days: int,
    fetched_at: str,
) -> list[dict[str, Any]]:
    if active_period_grace_days < 0:
        raise ValueError("active_period_grace_days must be non-negative")
    as_of_date = date.fromisoformat(as_of)
    floor = (as_of_date - timedelta(days=lookback_days)).isoformat()
    minimum_fiscal_period_end = as_of_date - timedelta(days=active_period_grace_days)
    ticker_set = set(tickers)
    revisions = conn.execute(
        """
        SELECT * FROM provider_estimate_snapshots
        WHERE coverage_status='available'
          AND substr(available_at_utc,1,10) BETWEEN ? AND ?
          AND estimate_average IS NOT NULL
          AND estimate_average_30_days_ago IS NOT NULL
        ORDER BY available_at_utc,snapshot_id
        """,
        (floor, as_of),
    ).fetchall()
    output: list[dict[str, Any]] = []
    for row in revisions:
        ticker = str(row["ticker"]).upper()
        if ticker not in ticker_set:
            continue
        try:
            fiscal_period_end = date.fromisoformat(str(row["fiscal_period_end"]))
        except ValueError as exc:
            raise ValueError(
                f"Invalid provider fiscal_period_end for {ticker}: "
                f"{row['fiscal_period_end']!r}"
            ) from exc
        if fiscal_period_end < minimum_fiscal_period_end:
            continue
        current = float(row["estimate_average"])
        prior = float(row["estimate_average_30_days_ago"])
        threshold = 0.02 if str(row["estimate_type"]).casefold() == "eps" else 0.01
        relative = (current - prior) / max(abs(current), abs(prior), 1e-9)
        if abs(relative) < threshold:
            continue
        available = str(row["available_at_utc"])
        output.append(
            {
                "source": f"{row['provider']}_estimates",
                "source_uid": f"{row['snapshot_id']}:revision30",
                "ticker_hint": ticker,
                "published_at_utc": available,
                "fetched_at_utc": fetched_at,
                "title": f"{row['estimate_type']} estimate revision",
                "summary": "Structured provider estimate revision; providers remain separate",
                "url": "",
                "payload": {
                    "kind": "estimate_revision",
                    "event_date": available[:10],
                    "direction": 1.0 if relative > 0 else -1.0,
                    "metric": str(row["estimate_type"]),
                    "fiscal_period_end": str(row["fiscal_period_end"]),
                    "relative_change": relative,
                    "provider": str(row["provider"]),
                    "rationale": "30-day provider estimate change exceeded the metric threshold",
                },
            }
        )
    outcomes = conn.execute(
        """
        SELECT * FROM provider_forecast_outcome_links_v3
        WHERE evaluation_status='eligible' AND report_date BETWEEN ? AND ?
        ORDER BY report_date,link_id
        """,
        (floor, as_of),
    ).fetchall()
    for row in outcomes:
        ticker = str(row["ticker"]).upper()
        if ticker not in ticker_set:
            continue
        forecast = float(row["forecast_value"])
        actual = float(row["actual_value"])
        surprise = (actual - forecast) / max(abs(actual), abs(forecast), 1e-9)
        if abs(surprise) < 0.02:
            continue
        report_date = str(row["report_date"])
        output.append(
            {
                "source": f"{row['estimate_provider']}_actuals",
                "source_uid": str(row["link_id"]),
                "ticker_hint": ticker,
                "published_at_utc": _iso_timestamp(
                    row["outcome_available_at_utc"], fallback_date=report_date
                ),
                "fetched_at_utc": fetched_at,
                "title": f"{row['metric']} earnings surprise",
                "summary": "Structured actual-versus-prior-consensus outcome",
                "url": "",
                "payload": {
                    "kind": "earnings_surprise",
                    "event_date": report_date,
                    "direction": 1.0 if surprise > 0 else -1.0,
                    "metric": str(row["metric"]),
                    "fiscal_period_end": str(row["fiscal_period_end"]),
                    "surprise": surprise,
                    "provider": str(row["estimate_provider"]),
                    "rationale": "reported actual differed from the strictly prior forecast",
                },
            }
        )
    return output


def run_selftest() -> None:
    assert _iso_timestamp("2026-07-30 20:00:00", fallback_date="2026-07-30").startswith(
        "2026-07-30T20:00:00"
    )
    assert _chunks([str(index) for index in range(9)], 4) == [
        ["0", "1", "2", "3"], ["4", "5", "6", "7"], ["8"]
    ]
    as_of = date(2026, 7, 31)
    minimum = as_of - timedelta(days=90)
    assert date.fromisoformat("2026-06-30") >= minimum
    assert date.fromisoformat("2026-09-30") >= minimum
    assert date.fromisoformat("2025-03-31") < minimum
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE provider_estimate_snapshots (
            snapshot_id TEXT, provider TEXT, ticker TEXT, estimate_type TEXT,
            fiscal_period_end TEXT, estimate_average REAL,
            estimate_average_30_days_ago REAL, available_at_utc TEXT,
            coverage_status TEXT
        );
        CREATE TABLE provider_forecast_outcome_links_v3 (
            evaluation_status TEXT, report_date TEXT, link_id TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO provider_estimate_snapshots VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                "stale", "alpha_vantage", "LHX", "eps", "2021-03-31",
                2.0, 1.0, "2026-07-31T20:00:00+00:00", "available",
            ),
            (
                "active", "alpha_vantage", "LHX", "eps", "2026-09-30",
                2.0, 1.0, "2026-07-31T20:00:00+00:00", "available",
            ),
        ],
    )
    provider_items = _provider_items(
        conn,
        tickers=["LHX"],
        as_of="2026-07-31",
        lookback_days=200,
        active_period_grace_days=90,
        fetched_at="2026-07-31T21:00:00+00:00",
    )
    conn.close()
    assert len(provider_items) == 1
    assert provider_items[0]["payload"]["fiscal_period_end"] == "2026-09-30"
    print("authoritative event ingestion selftest: PASS")


def main() -> int:
    args = parse_args()
    if args.selftest:
        run_selftest()
        return 0
    if args.as_of is None:
        raise ValueError("--as-of is required")
    config_path = args.config.resolve()
    config = load_yaml(config_path)
    monitor_cfg = cfg_get(config, "expectations_monitor", {})
    event_cfg = cfg_get(config, "expectations_monitor.events", {})
    reconciliation_cfg = cfg_get(
        config, "expectations_monitor.provider_reconciliation", {}
    )
    if (
        not isinstance(monitor_cfg, dict)
        or not isinstance(event_cfg, dict)
        or not isinstance(reconciliation_cfg, dict)
    ):
        raise ValueError(
            "expectations_monitor events and provider_reconciliation must be mappings"
        )
    db_path = ensure_not_prod_path(
        resolve_path(monitor_cfg.get("database_path", "db/expectations_monitor.sqlite"), base_dir=config_path.parent),
        label="expectations monitor database",
    )
    timeout = float(monitor_cfg.get("writer_lock_timeout_sec", 30.0))
    universe_as_of = (args.universe_as_of or args.as_of).isoformat()
    paths = resolve_runtime_paths(config, config_path)
    output_dir = (
        args.output_dir
        or paths.output_dir
        / "runs"
        / args.as_of.isoformat()
        / monitor_output_subdir(config)
        / "events"
    )
    raw_path = output_dir / "raw_items.csv"
    source_path = output_dir / "event_source_status.csv"
    manifest_path = output_dir / "event_ingestion_manifest.json"
    fail_if_exists([raw_path, source_path, manifest_path], force=args.force)
    fetched_at = utc_now()
    conn = connect_monitor_db(db_path, timeout_sec=timeout)
    try:
        ensure_state_schema(conn)
        universe = fetch_universe_snapshot(conn, universe_as_of)
        if not universe:
            raise ValueError(f"No monitor universe for {universe_as_of}")
        tickers = sorted({str(row["ticker"]) for row in universe})
        lookback = int(event_cfg.get("lookback_calendar_days", 200))
        active_period_grace_days = int(
            reconciliation_cfg.get("active_period_grace_days", 90)
        )
        if active_period_grace_days < 0:
            raise ValueError("active_period_grace_days must be non-negative")
        source_rows: list[dict[str, Any]] = []
        raw_rows: list[dict[str, Any]] = []
        source_specs = (
            (
                "form4",
                str(event_cfg.get("form4_database_path", "")).strip(),
                _form4_items,
            ),
            (
                "biotech_guidance",
                str(event_cfg.get("biotech_database_path", "")).strip(),
                _guidance_items,
            ),
        )
        for source_name, configured_path, loader in source_specs:
            if not configured_path:
                raise ValueError(
                    f"expectations_monitor.events path is required for {source_name}"
                )
            path = resolve_path(configured_path, base_dir=config_path.parent)
            try:
                rows = loader(
                    path,
                    tickers=tickers,
                    as_of=args.as_of.isoformat(),
                    lookback_days=lookback,
                    fetched_at=fetched_at,
                )
                raw_rows.extend(rows)
                source_rows.append(
                    {"source": source_name, "status": "PASS", "row_count": len(rows), "detail": str(path)}
                )
            except (FileNotFoundError, sqlite3.Error, ValueError) as exc:
                source_rows.append(
                    {"source": source_name, "status": "DEFERRED", "row_count": 0, "detail": str(exc)}
                )
        provider_rows = _provider_items(
            conn,
            tickers=tickers,
            as_of=args.as_of.isoformat(),
            lookback_days=lookback,
            active_period_grace_days=active_period_grace_days,
            fetched_at=fetched_at,
        )
        raw_rows.extend(provider_rows)
        source_rows.append(
            {
                "source": "sealed_provider_snapshots",
                "status": "PASS" if provider_rows else "DEFERRED",
                "row_count": len(provider_rows),
                "detail": (
                    (
                        "expectations_monitor.sqlite;"
                        f"active_period_grace_days={active_period_grace_days}"
                    )
                    if provider_rows
                    else "no eligible sealed provider events"
                ),
            }
        )
        with database_writer_lock(db_path, timeout_sec=timeout):
            inserted, duplicates = append_raw_items(conn, raw_rows)
        ids = [digest({"source": row["source"], "source_uid": row["source_uid"]}) for row in raw_rows]
        db_rows = [
            dict(row)
            for batch in _chunks(ids)
            for row in conn.execute(
                f"SELECT * FROM raw_items WHERE item_id IN ({','.join('?' for _ in batch)}) ORDER BY published_at_utc,item_id",
                batch,
            ).fetchall()
        ]
    finally:
        conn.close()
    write_csv(raw_path, RAW_FIELDS, db_rows)
    write_csv(source_path, SOURCE_FIELDS, source_rows)
    input_paths = [config_path, Path(__file__).resolve(), Path(__file__).with_name("state_common.py")]
    company_sources = [
        row for row in source_rows if row["source"] in {"form4", "biotech_guidance"}
    ]
    available_company_sources = sum(row["status"] == "PASS" for row in company_sources)
    acceptance = (
        "PASS"
        if available_company_sources == len(company_sources)
        else "PASS_WITH_DEFERRED"
        if available_company_sources > 0
        else "FAIL"
    )
    write_manifest(
        manifest_path,
        {
            "schema_version": "event_ingestion_manifest_v1",
            "acceptance": acceptance,
            "as_of_date": args.as_of.isoformat(),
            "universe_as_of": universe_as_of,
            "provider_revision_active_period_grace_days": active_period_grace_days,
            "inserted": inserted,
            "idempotent_duplicates": duplicates,
            "source_status": source_rows,
            "implementation_or_policy_data_sent": False,
            "inputs_sha256": {str(path): sha256_file(path) for path in input_paths},
            "outputs_sha256": {
                raw_path.name: sha256_file(raw_path),
                source_path.name: sha256_file(source_path),
            },
        },
    )
    print(f"AUTHORITATIVE EVENT INGESTION: {acceptance}")
    print(f"rows={len(db_rows)}; inserted={inserted}; duplicates={duplicates}; manifest={manifest_path}")
    return 0 if acceptance.startswith("PASS") else 1


if __name__ == "__main__":
    raise SystemExit(main())

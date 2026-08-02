from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import os
import tempfile
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from portfolio_layer.core.config import cfg_get, resolve_path
from portfolio_layer.core.contracts import (
    read_csv,
    read_manifest,
    replace_atomic,
    sha256_file,
    write_manifest,
)
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths
from portfolio_layer.expectations_monitor.monitor_common import (
    connect_monitor_db,
    fetch_universe_snapshot,
)
from portfolio_layer.risk.liquidity import active_symbol_for_ticker
from portfolio_layer.risk.ohlcv_sources import SOURCE_PRIORITY


SELECTED_OHLCV_FILENAME = "monitor_ohlcv.csv.gz"
BENCHMARK_OHLCV_FILENAME = "monitor_benchmark_ohlcv.csv.gz"
OBSERVATION_FIELDS = [
    "date",
    "ticker",
    "source",
    "source_symbol",
    "retrieved_at_utc",
    "open",
    "high",
    "low",
    "close",
    "adj_open",
    "adj_high",
    "adj_low",
    "adj_close",
    "raw_volume",
    "volume",
    "adjustment_factor",
    "split_factor",
    "dividend_cash",
    "adjustment_status",
]

SELECTED_FIELDS = [
    *OBSERVATION_FIELDS,
    "source_count",
    "sources_observed",
    "max_adj_close_disagreement_bps",
    "disagreement_status",
    "session_final",
]

COVERAGE_FIELDS = [
    "ticker",
    "tier",
    "first_required_date",
    "latest_required_date",
    "required_sessions",
    "observed_sessions",
    "missing_sessions",
    "missing_fraction",
    "latest_session_present",
    "sources_observed",
    "status",
]

FETCH_RESULT_FIELDS = [
    "ticker",
    "tier",
    "provider",
    "source_symbol",
    "status",
    "row_count",
    "first_date",
    "last_date",
]

DISAGREEMENT_FIELDS = [
    "date",
    "ticker",
    "selected_source",
    "sources_observed",
    "source_count",
    "max_adj_close_disagreement_bps",
    "status",
]

VALIDATION_FIELDS = ["check", "status", "detail"]


def row_digest(rows: Iterable[dict[str, Any]]) -> str:
    payload = json.dumps(
        list(rows),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_gzip_csv(
    path: Path,
    fieldnames: list[str],
    rows: Iterable[dict[str, Any]],
) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    os.close(fd)
    try:
        with gzip.GzipFile(filename=tmp_name, mode="wb", mtime=0) as compressed:
            import io

            handle = io.TextIOWrapper(compressed, encoding="utf-8", newline="")
            writer = csv.DictWriter(
                handle,
                fieldnames=fieldnames,
                extrasaction="ignore",
                lineterminator="\n",
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
                count += 1
            handle.flush()
        replace_atomic(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
    return count


def read_gzip_csv(path: Path) -> list[dict[str, str]]:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def normalized_cache_path(cache_root: Path, *, provider: str, ticker: str) -> Path:
    key = ticker.strip().upper()
    safe = "".join(character if character.isalnum() else "_" for character in key)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]
    return cache_root / "monitor_ohlcv" / provider / f"{safe}-{digest}.json"


def write_normalized_provider_cache(
    path: Path,
    *,
    provider: str,
    ticker: str,
    source_symbol: str,
    rows: list[dict[str, Any]],
) -> None:
    ordered = sorted(rows, key=lambda row: str(row["date"]))
    write_manifest(
        path,
        {
            "schema_version": "monitor_ohlcv_normalized_cache_v1",
            "provider": provider,
            "ticker": ticker.strip().upper(),
            "source_symbol": source_symbol.strip().upper(),
            "updated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "row_count": len(ordered),
            "row_digest": row_digest(ordered),
            "rows": ordered,
        },
    )


def read_normalized_provider_cache(
    path: Path,
    *,
    provider: str,
    ticker: str,
    source_symbol: str,
) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        payload = read_manifest(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    rows = payload.get("rows")
    if (
        payload.get("schema_version") != "monitor_ohlcv_normalized_cache_v1"
        or payload.get("provider") != provider
        or payload.get("ticker") != ticker.strip().upper()
        or payload.get("source_symbol") != source_symbol.strip().upper()
        or not isinstance(rows, list)
        or any(not isinstance(row, dict) for row in rows)
    ):
        return []
    typed_rows = [dict(row) for row in rows]
    if int(payload.get("row_count", -1)) != len(typed_rows):
        return []
    if payload.get("row_digest") != row_digest(typed_rows):
        return []
    return typed_rows


def market_policy_errors(config: dict[str, Any]) -> list[str]:
    market = cfg_get(config, "expectations_monitor.market_data", {})
    if not isinstance(market, dict):
        return ["market_data_not_mapping"]
    errors: list[str] = []
    expected = list(SOURCE_PRIORITY)
    if market.get("policy_version") != "monitor_market_data_v1":
        errors.append("policy_version_mismatch")
    if market.get("source_priority") != expected:
        errors.append("source_priority_mismatch")
    if market.get("primary_eod_source") != "yahoo":
        errors.append("primary_not_yahoo")
    if market.get("current_confirmation_source") != "ibkr":
        errors.append("confirmation_not_ibkr")
    if market.get("recovery_source") != "tiingo":
        errors.append("recovery_not_tiingo")
    if market.get("average_conflicting_prices") is not False:
        errors.append("price_averaging_not_prohibited")
    if market.get("retain_source_disagreements") is not True:
        errors.append("source_disagreements_not_retained")
    batch_size = int(market.get("ib_max_batch_size", 0))
    if batch_size < 1 or batch_size >= 100:
        errors.append("ib_batch_size_out_of_bounds")
    tier0_target = float(market.get("tier0_latest_coverage_floor", 0.98))
    tier0_hard_floor = float(
        market.get("tier0_latest_coverage_hard_floor", 0.90)
    )
    if not 0.0 <= tier0_target <= 1.0:
        errors.append("tier0_latest_coverage_floor_out_of_bounds")
    if not 0.0 <= tier0_hard_floor <= 1.0:
        errors.append("tier0_latest_coverage_hard_floor_out_of_bounds")
    if tier0_hard_floor > tier0_target:
        errors.append("tier0_latest_coverage_hard_floor_above_target")
    if market.get("require_final_daily_bar") is not True:
        errors.append("daily_bar_finality_not_required")
    if market.get("require_corporate_action_validation") is not True:
        errors.append("corporate_action_validation_not_required")
    return errors


def tier0_coverage_status(
    coverage: float,
    *,
    target: float,
    hard_floor: float,
) -> str:
    if not all(math.isfinite(value) for value in (coverage, target, hard_floor)):
        raise ValueError("Tier-0 coverage thresholds must be finite")
    if not 0.0 <= hard_floor <= target <= 1.0 or not 0.0 <= coverage <= 1.0:
        raise ValueError("Tier-0 coverage thresholds are invalid")
    if coverage + 1e-12 < hard_floor:
        return "FAIL"
    if coverage + 1e-12 < target:
        return "WARN"
    return "PASS"


def effective_request_end(
    as_of: date,
    *,
    timezone_name: str,
    same_day_final_after: str,
    now: datetime | None = None,
) -> date:
    zone = ZoneInfo(timezone_name)
    current = now.astimezone(zone) if now is not None else datetime.now(zone)
    if as_of > current.date():
        raise ValueError(f"OHLCV as-of date {as_of} is in the future")
    parts = same_day_final_after.split(":", maxsplit=1)
    if len(parts) != 2:
        raise ValueError("same_day_final_after must be HH:MM")
    cutoff = time(int(parts[0]), int(parts[1]))
    effective = as_of
    if as_of == current.date() and current.timetz().replace(tzinfo=None) < cutoff:
        effective -= timedelta(days=1)
    while effective.weekday() >= 5:
        effective -= timedelta(days=1)
    return effective


def provider_symbols(
    config: dict[str, Any],
    ticker: str,
    *,
    as_of: str,
) -> dict[str, str]:
    key = ticker.strip().upper()
    yahoo = key.replace(".", "-")
    aliases = cfg_get(config, "risk_panel.ticker_aliases", {}) or {}
    if isinstance(aliases, dict):
        raw = aliases.get(key)
        if isinstance(raw, dict):
            yahoo = str(
                raw.get("query_symbol") or raw.get("active_ticker") or yahoo
            ).strip().upper()
    ibkr, _ = active_symbol_for_ticker(config, key, as_of)
    return {"yahoo": yahoo, "ibkr": ibkr, "tiingo": yahoo}


def load_sealed_universe(
    config: dict[str, Any],
    config_path: Path,
    *,
    universe_as_of: str,
) -> tuple[list[dict[str, Any]], Path, list[dict[str, Any]], Path]:
    paths = resolve_runtime_paths(config, config_path)
    monitor = cfg_get(config, "expectations_monitor", {})
    if not isinstance(monitor, dict):
        raise ValueError("expectations_monitor config must be a mapping")
    output_subdir = str(monitor.get("output_subdir", "expectations_monitor"))
    root = paths.output_dir / "runs" / universe_as_of / output_subdir
    manifest_path = root / "monitor_universe_manifest.json"
    universe_path = root / "monitor_universe.csv"
    if not manifest_path.is_file() or not universe_path.is_file():
        raise FileNotFoundError(f"Sealed monitor universe is missing for {universe_as_of}")
    manifest = read_manifest(manifest_path)
    if manifest.get("acceptance") != "PASS" or manifest.get("run_as_of") != universe_as_of:
        raise ValueError("Monitor universe manifest is not accepted/current")
    outputs = manifest.get("outputs_sha256", {})
    if not isinstance(outputs, dict) or outputs.get(universe_path.name) != sha256_file(universe_path):
        raise ValueError("Monitor universe CSV hash does not match its manifest")
    db_path = ensure_not_prod_path(
        resolve_path(
            monitor.get("database_path", "db/expectations_monitor.sqlite"),
            base_dir=config_path.parent,
        ),
        label="expectations monitor database",
    )
    timeout = float(monitor.get("writer_lock_timeout_sec", 30.0))
    conn = connect_monitor_db(db_path, timeout_sec=timeout)
    try:
        db_rows = fetch_universe_snapshot(conn, universe_as_of)
        source_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM monitor_source_artifacts WHERE run_as_of=? ORDER BY source_role",
                (universe_as_of,),
            ).fetchall()
        ]
    finally:
        conn.close()
    csv_rows = read_csv(universe_path)
    csv_identity = [(row.get("ticker", ""), row.get("tier", "")) for row in csv_rows]
    db_identity = [(str(row.get("ticker", "")), str(row.get("tier", ""))) for row in db_rows]
    if not db_rows or csv_identity != db_identity:
        raise ValueError("Monitor universe CSV and SQLite identity rows differ")
    if not source_rows:
        raise ValueError("Monitor universe has no sealed source-artifact lineage")
    for source in source_rows:
        artifact = Path(str(source["artifact_path"]))
        source_manifest = Path(str(source["manifest_path"]))
        if (
            not artifact.is_file()
            or not source_manifest.is_file()
            or sha256_file(artifact) != str(source["artifact_sha256"])
            or sha256_file(source_manifest) != str(source["manifest_sha256"])
        ):
            raise ValueError(f"Monitor source lineage is stale: {source['source_role']}")
    return db_rows, manifest_path, source_rows, db_path


def market_artifact_dir(
    config: dict[str, Any],
    config_path: Path,
    *,
    as_of: str,
) -> Path:
    paths = resolve_runtime_paths(config, config_path)
    monitor = cfg_get(config, "expectations_monitor", {})
    output_subdir = (
        str(monitor.get("output_subdir", "expectations_monitor"))
        if isinstance(monitor, dict)
        else "expectations_monitor"
    )
    return paths.output_dir / "runs" / as_of / output_subdir / "market_data"

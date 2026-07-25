#!/usr/bin/env python3
"""Build the sealed Stage 11 adjusted-OHLC execution panel.

The Stage 11 survivorship panel remains authoritative for adjusted closes and
membership. This companion panel obtains raw Yahoo OHLC bars, applies Yahoo's
daily adjustment factor, then normalizes each row back to the already-sealed
adjusted close. A row is rejected when its fetched adjusted close materially
disagrees with the sealed close. Missing opens are never replaced by closes.

Published delisted-price exports may supply optional ``adj_open``,
``adj_high``, and ``adj_low`` fields. Existing close-only exports remain valid
for 15b but are explicitly reported as incomplete for execution research.
"""
from __future__ import annotations

import argparse
import glob as globmod
import json
import logging
import math
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    read_csv,
    read_manifest,
    sha256_file,
    write_csv,
    write_manifest,
    write_via_temp,
)
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.research.stage11_common import manifest_file_errors  # noqa: E402
from portfolio_layer.risk.yahoo import fetch_adjusted_ohlcv  # noqa: E402


LOGGER = logging.getLogger("build_execution_ohlcv_panel")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
OHLC_FIELDS = [
    "date",
    "ticker",
    "adj_open",
    "adj_high",
    "adj_low",
    "adj_close",
    "volume",
    "source",
]
COVERAGE_FIELDS = [
    "ticker",
    "source_pipeline",
    "required_close_rows",
    "execution_rows",
    "coverage_fraction",
    "source_close_disagreements",
    "unresolved_close_disagreements",
    "status",
    "sources",
]
FETCH_FIELDS = ["ticker", "status", "provider", "source_symbol", "rows", "cache_hit"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build adjusted-OHLC execution research panel.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--panel-build", default=None)
    parser.add_argument("--max-workers", type=int, default=0)
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def _latest(root: Path, wanted: str | None) -> Path | None:
    if wanted:
        candidate = root / wanted
        return candidate if (candidate / "survivorship_manifest.json").exists() else None
    if not root.exists():
        return None
    builds = sorted(
        path
        for path in root.iterdir()
        if path.is_dir() and (path / "survivorship_manifest.json").exists()
    )
    return builds[-1] if builds else None


def _cache_path(cache_dir: Path, ticker: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in ticker)
    return cache_dir / f"{safe}.json"


def _load_cache(
    cache_dir: Path,
    ticker: str,
    *,
    start: str,
    end: str,
    expected_source_symbol: str,
) -> tuple[list[dict[str, Any]], str, str] | None:
    path = _cache_path(cache_dir, ticker)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if str(payload.get("fetched_from", "")) > start:
        return None
    if str(payload.get("fetched_through", "")) < end:
        return None
    if str(payload.get("source_symbol", "")).strip().upper() != expected_source_symbol:
        return None
    rows = [
        row
        for row in payload.get("rows", [])
        if isinstance(row, dict) and start <= str(row.get("date", "")) <= end
    ]
    if not rows:
        return None
    return rows, str(payload.get("provider", "cache")), str(
        payload.get("source_symbol", ticker)
    )


def _write_cache(
    cache_dir: Path,
    ticker: str,
    *,
    rows: list[dict[str, Any]],
    provider: str,
    source_symbol: str,
    start: str,
    end: str,
) -> None:
    write_manifest(
        _cache_path(cache_dir, ticker),
        {
            "ticker": ticker,
            "provider": provider,
            "source_symbol": source_symbol,
            "fetched_from": start,
            "fetched_through": end,
            "rows": rows,
        },
    )


def _float(row: dict[str, str], *names: str) -> float | None:
    for name in names:
        raw = str(row.get(name, "")).strip()
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if math.isfinite(value):
            return value
    return None


def _load_export_ohlcv(files: list[Path]) -> dict[tuple[str, str], dict[str, Any]]:
    output: dict[tuple[str, str], dict[str, Any]] = {}
    for path in files:
        for row in read_csv(path):
            ticker = str(row.get("ticker", "")).strip().upper()
            day = str(row.get("date", "")).strip()
            adj_open = _float(row, "adj_open", "adjopen")
            adj_high = _float(row, "adj_high", "adjhigh")
            adj_low = _float(row, "adj_low", "adjlow")
            adj_close = _float(row, "adj_close", "adjclose")
            if (
                not ticker
                or len(day) != 10
                or min(adj_open or 0, adj_high or 0, adj_low or 0, adj_close or 0) <= 0
            ):
                continue
            output[(ticker, day)] = {
                "date": day,
                "adj_open": adj_open,
                "adj_high": adj_high,
                "adj_low": adj_low,
                "adj_close": adj_close,
                "volume": max(0.0, _float(row, "volume") or 0.0),
                "source": f"delisted_export:{path.name}",
            }
    return output


def _normalize_row(
    row: dict[str, Any],
    sealed_close: float,
    *,
    disagreement_tolerance: float,
) -> tuple[dict[str, Any] | None, bool]:
    fetched_close = float(row["adj_close"])
    disagreement = abs(fetched_close - sealed_close) / sealed_close
    if disagreement > disagreement_tolerance:
        return None, True
    scale = sealed_close / fetched_close
    adjusted = {
        "adj_open": float(row["adj_open"]) * scale,
        "adj_high": float(row["adj_high"]) * scale,
        "adj_low": float(row["adj_low"]) * scale,
        "adj_close": sealed_close,
        "volume": max(0.0, float(row.get("volume", 0.0))),
    }
    valid = (
        min(adjusted["adj_open"], adjusted["adj_high"], adjusted["adj_low"], sealed_close)
        > 0
        and adjusted["adj_high"] + 1e-10 >= max(adjusted["adj_open"], sealed_close)
        and adjusted["adj_low"] - 1e-10 <= min(adjusted["adj_open"], sealed_close)
    )
    return (adjusted if valid else None), False


def _select_normalized_row(
    candidates: list[tuple[str, dict[str, Any]]],
    sealed_close: float,
    *,
    disagreement_tolerance: float,
) -> tuple[dict[str, Any] | None, str, int, int]:
    """Choose the first valid source, allowing a rejected primary to fall through."""
    disagreements = 0
    invalid_shapes = 0
    for source, row in candidates:
        normalized, disagreed = _normalize_row(
            row,
            sealed_close,
            disagreement_tolerance=disagreement_tolerance,
        )
        if disagreed:
            disagreements += 1
            continue
        if normalized is None:
            invalid_shapes += 1
            continue
        return normalized, source, disagreements, invalid_shapes
    return None, "", disagreements, invalid_shapes


def _selftest() -> None:
    row = {
        "adj_open": 9.0,
        "adj_high": 11.0,
        "adj_low": 8.0,
        "adj_close": 10.0,
        "volume": 100,
    }
    normalized, disagreed = _normalize_row(row, 10.1, disagreement_tolerance=0.02)
    assert not disagreed and normalized is not None
    assert abs(float(normalized["adj_close"]) - 10.1) < 1e-12
    rejected, disagreed = _normalize_row(row, 12.0, disagreement_tolerance=0.02)
    assert rejected is None and disagreed
    fallback = {
        "adj_open": 10.0,
        "adj_high": 10.5,
        "adj_low": 9.5,
        "adj_close": 10.1,
        "volume": 50,
    }
    selected, source, source_disagreements, invalid_shapes = _select_normalized_row(
        [("bad_primary", row), ("audited_export", fallback)],
        10.1,
        disagreement_tolerance=0.005,
    )
    assert selected is not None and source == "audited_export"
    assert source_disagreements == 1 and invalid_shapes == 0
    print("execution-OHLCV panel self-test: PASS")


def main() -> int:  # noqa: C901, PLR0912, PLR0915
    configure_utc_logging()
    args = parse_args()
    if args.selftest:
        _selftest()
        return 0
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    sp_root = paths.output_dir / str(
        cfg_get(config, "survivorship_panel.dir", "survivorship_panel")
    )
    source_dir = _latest(sp_root, args.panel_build)
    if source_dir is None:
        LOGGER.error("No accepted-looking survivorship panel directory found")
        return 1
    source_manifest_path = source_dir / "survivorship_manifest.json"
    prices_path = source_dir / "prices_adjclose.csv"
    coverage_path = source_dir / "ticker_coverage.csv"
    source_manifest = read_manifest(source_manifest_path)
    source_errors = manifest_file_errors(
        source_manifest,
        {"prices_adjclose.csv": prices_path, "ticker_coverage.csv": coverage_path},
    )
    if source_manifest.get("acceptance") != "PASS" or source_errors:
        LOGGER.error("Survivorship panel is rejected/stale: %s", source_errors)
        return 1

    prices = pd.read_csv(prices_path, index_col=0)
    prices.index = prices.index.astype(str).str.slice(0, 10)
    prices.columns = [str(column).strip().upper() for column in prices.columns]
    if prices.empty or prices.index.has_duplicates:
        LOGGER.error("Sealed adjusted-close panel is empty or has duplicate dates")
        return 1
    price_days: list[str] = [str(day) for day in prices.index.tolist()]
    coverage = pd.read_csv(coverage_path).fillna("")
    pipeline_by_ticker = {
        str(row["ticker"]).strip().upper(): str(row["source_pipeline"])
        for _, row in coverage.iterrows()
    }
    tickers = list(prices.columns)
    start = str(prices.index.min())
    end = str(prices.index.max())

    cfg = cfg_get(config, "execution_ohlcv_panel", {}) or {}
    close_tolerance = float(cfg.get("close_disagreement_tolerance", 0.02))
    complete_fraction = float(cfg.get("ticker_complete_fraction", 0.95))
    if not 0 <= close_tolerance <= 0.25 or not 0 < complete_fraction <= 1:
        LOGGER.error("Invalid execution_ohlcv_panel thresholds")
        return 1
    export_files: list[Path] = []
    for pattern in cfg_get(config, "survivorship_panel.delisted_price_export_globs", []) or []:
        resolved = resolve_path(str(pattern), base_dir=config_path.parent)
        export_files.extend(
            Path(hit) for hit in sorted(globmod.glob(str(resolved))) if Path(hit).is_file()
        )
    export_rows = _load_export_ohlcv(export_files)
    aliases = cfg_get(config, "risk_panel.ticker_aliases", {}) or {}
    query_symbols = {
        ticker: str((aliases.get(ticker) or {}).get("active_ticker") or ticker).strip().upper()
        for ticker in tickers
    }
    fetch_cfg = cfg_get(config, "risk_panel.fetch", {}) or {}
    url_templates = [str(value) for value in fetch_cfg.get("chart_url_templates", [])] or [
        "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    ]
    user_agent = str(fetch_cfg.get("user_agent", "portfolio_layer/0.1"))
    timeout = float(fetch_cfg.get("request_timeout_sec", 20))
    retries = int(fetch_cfg.get("max_retries", 3))
    workers = args.max_workers or int(fetch_cfg.get("max_workers", 10))
    cache_dir = paths.cache_dir / "survivorship_execution_ohlcv"

    def fetch_one(
        ticker: str,
        *,
        use_cache: bool = True,
    ) -> tuple[str, list[dict[str, Any]], str, str, str, bool]:
        symbol = query_symbols[ticker]
        cached = (
            _load_cache(
                cache_dir,
                ticker,
                start=start,
                end=end,
                expected_source_symbol=symbol,
            )
            if use_cache
            else None
        )
        if cached:
            rows, provider, symbol = cached
            return ticker, rows, "ok", f"cache:{provider}", symbol, True
        rows, status, provider, source_symbol = fetch_adjusted_ohlcv(
            symbol,
            start=date.fromisoformat(start),
            end=date.fromisoformat(end),
            url_templates=url_templates,
            user_agent=user_agent,
            timeout_sec=timeout,
            max_retries=retries,
        )
        if status == "ok" and rows:
            cache_dir.mkdir(parents=True, exist_ok=True)
            _write_cache(
                cache_dir,
                ticker,
                rows=rows,
                provider=provider,
                source_symbol=source_symbol,
                start=start,
                end=end,
            )
        return ticker, rows, status, provider, source_symbol, False

    fetched: dict[str, dict[str, dict[str, Any]]] = {}
    fetch_rows_by_ticker: dict[str, dict[str, Any]] = {}
    LOGGER.info("Fetching adjusted OHLCV for %d tickers with %d workers", len(tickers), workers)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fetch_one, ticker): ticker for ticker in tickers}
        for future in as_completed(futures):
            ticker, rows, status, provider, symbol, cache_hit = future.result()
            fetched[ticker] = {str(row["date"]): row for row in rows}
            fetch_rows_by_ticker[ticker] = {
                "ticker": ticker,
                "status": status,
                "provider": provider,
                "source_symbol": symbol,
                "rows": len(rows),
                "cache_hit": int(cache_hit),
            }

    refresh_tickers: list[str] = []
    for ticker in tickers:
        fetch_meta = fetch_rows_by_ticker[ticker]
        if int(fetch_meta["cache_hit"]) != 1:
            continue
        for day in price_days:
            if pd.isna(prices.at[day, ticker]):
                continue
            cached_row = fetched.get(ticker, {}).get(day)
            if cached_row is None:
                continue
            _normalized, disagreed = _normalize_row(
                cached_row,
                float(prices.at[day, ticker]),
                disagreement_tolerance=close_tolerance,
            )
            if not disagreed:
                continue
            export_row = export_rows.get((ticker, day))
            if export_row is not None:
                export_normalized, export_disagreed = _normalize_row(
                    export_row,
                    float(prices.at[day, ticker]),
                    disagreement_tolerance=close_tolerance,
                )
                if export_normalized is not None and not export_disagreed:
                    continue
            refresh_tickers.append(ticker)
            break
    if refresh_tickers:
        LOGGER.warning(
            "Refreshing %d cached ticker(s) whose adjusted closes disagree with "
            "the sealed panel",
            len(refresh_tickers),
        )
        with ThreadPoolExecutor(max_workers=min(workers, len(refresh_tickers))) as executor:
            futures = {
                executor.submit(fetch_one, ticker, use_cache=False): ticker
                for ticker in refresh_tickers
            }
            for future in as_completed(futures):
                ticker, rows, status, provider, symbol, cache_hit = future.result()
                fetched[ticker] = {str(row["date"]): row for row in rows}
                fetch_rows_by_ticker[ticker] = {
                    "ticker": ticker,
                    "status": status,
                    "provider": provider,
                    "source_symbol": symbol,
                    "rows": len(rows),
                    "cache_hit": int(cache_hit),
                }

    output_rows: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []
    invalid_shape = 0
    total_source_disagreements = 0
    total_unresolved_disagreements = 0
    provider_by_ticker = {
        str(row["ticker"]): str(row["provider"])
        for row in fetch_rows_by_ticker.values()
    }
    for ticker in tickers:
        required_dates = [
            day for day in price_days if pd.notna(prices.at[day, ticker])
        ]
        execution_rows = 0
        source_disagreements = 0
        unresolved_disagreements = 0
        sources: set[str] = set()
        for day in required_dates:
            candidates: list[tuple[str, dict[str, Any]]] = []
            fetched_row = fetched.get(ticker, {}).get(day)
            if fetched_row is not None:
                candidates.append(
                    (provider_by_ticker.get(ticker, "unknown"), fetched_row)
                )
            export_row = export_rows.get((ticker, day))
            if export_row is not None:
                candidates.append((str(export_row["source"]), export_row))
            if not candidates:
                continue
            normalized, source, disagreed_count, invalid_count = (
                _select_normalized_row(
                    candidates,
                    float(prices.at[day, ticker]),
                    disagreement_tolerance=close_tolerance,
                )
            )
            source_disagreements += disagreed_count
            invalid_shape += invalid_count
            if normalized is None:
                if disagreed_count:
                    unresolved_disagreements += 1
                continue
            sources.add(source)
            output_rows.append(
                {
                    "date": day,
                    "ticker": ticker,
                    **normalized,
                    "source": source,
                }
            )
            execution_rows += 1
        total_source_disagreements += source_disagreements
        total_unresolved_disagreements += unresolved_disagreements
        fraction = execution_rows / len(required_dates) if required_dates else 0.0
        status = (
            "complete"
            if (
                required_dates
                and fraction >= complete_fraction
                and unresolved_disagreements == 0
            )
            else "incomplete"
        )
        coverage_rows.append(
            {
                "ticker": ticker,
                "source_pipeline": pipeline_by_ticker.get(ticker, ""),
                "required_close_rows": len(required_dates),
                "execution_rows": execution_rows,
                "coverage_fraction": round(fraction, 8),
                "source_close_disagreements": source_disagreements,
                "unresolved_close_disagreements": unresolved_disagreements,
                "status": status,
                "sources": ";".join(sorted(sources)),
            }
        )

    output = pd.DataFrame(output_rows, columns=pd.Index(OHLC_FIELDS))
    duplicate_count = int(output.duplicated(["date", "ticker"]).sum()) if not output.empty else 0
    master = str(cfg_get(config, "risk_panel.master_calendar_ticker", "SPY")).upper()
    master_row = next((row for row in coverage_rows if row["ticker"] == master), None)
    master_ok = bool(master_row and float(master_row["coverage_fraction"]) >= complete_fraction)
    checks = [
        {
            "check": "survivorship_input_sealed",
            "status": "PASS",
            "detail": f"build={source_dir.name}",
        },
        {
            "check": "ohlcv_shape_valid",
            "status": "PASS" if invalid_shape == 0 and duplicate_count == 0 else "FAIL",
            "detail": f"invalid={invalid_shape} duplicates={duplicate_count}",
        },
        {
            "check": "sealed_close_consistency",
            "status": (
                "PASS"
                if total_source_disagreements == 0
                else "WARN"
                if total_unresolved_disagreements == 0
                else "FAIL"
            ),
            "detail": (
                f"source_disagreements={total_source_disagreements} "
                f"unresolved={total_unresolved_disagreements}"
            ),
        },
        {
            "check": "master_calendar_execution_coverage",
            "status": "PASS" if master_ok else "FAIL",
            "detail": str(master_row or "missing master ticker"),
        },
    ]
    accepted = all(check["status"] in {"PASS", "WARN"} for check in checks)

    out_root = paths.output_dir / str(cfg.get("dir", "execution_ohlcv_panel"))
    out_dir = out_root / source_dir.name
    ohlcv_path = out_dir / "prices_adjusted_ohlcv.csv.gz"
    out_coverage = out_dir / "execution_ohlcv_coverage.csv"
    out_fetch = out_dir / "execution_ohlcv_fetch_results.csv"
    manifest_path = out_dir / "execution_ohlcv_manifest.json"
    artifacts = [ohlcv_path, out_coverage, out_fetch, manifest_path]
    if args.force:
        for path in artifacts:
            if path.exists():
                path.unlink()
    try:
        fail_if_exists(artifacts, force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)
    write_via_temp(
        ohlcv_path,
        lambda temp: output.to_csv(
            temp,
            index=False,
            lineterminator="\n",
            compression="gzip",
        ),
    )
    write_csv(out_coverage, COVERAGE_FIELDS, coverage_rows)
    fetch_rows = sorted(
        fetch_rows_by_ticker.values(), key=lambda row: str(row["ticker"])
    )
    write_csv(out_fetch, FETCH_FIELDS, fetch_rows)
    write_manifest(
        manifest_path,
        {
            "stage": "stage11_execution_ohlcv_panel",
            "generated_at": utc_now(),
            "acceptance": "PASS" if accepted else "FAIL",
            "panel_build": source_dir.name,
            "window": {"start": start, "end": end},
            "checks": checks,
            "coverage": {
                "tickers": len(tickers),
                "complete_tickers": sum(row["status"] == "complete" for row in coverage_rows),
                "rows": len(output),
            },
            "inputs_sha256": {
                "config.yaml": sha256_file(config_path),
                "backtest/15c_build_execution_ohlcv_panel.py": sha256_file(
                    Path(__file__).resolve()
                ),
                "risk/yahoo.py": sha256_file(PACKAGE_ROOT / "risk" / "yahoo.py"),
                "survivorship_manifest.json": sha256_file(source_manifest_path),
                "prices_adjclose.csv": sha256_file(prices_path),
                "ticker_coverage.csv": sha256_file(coverage_path),
                **{
                    f"delisted_price_export:{path}": sha256_file(path)
                    for path in export_files
                },
            },
            "survivorship_manifest_sha256": sha256_file(source_manifest_path),
            "files": {
                ohlcv_path.name: {"sha256": sha256_file(ohlcv_path), "rows": len(output)},
                out_coverage.name: {
                    "sha256": sha256_file(out_coverage),
                    "rows": len(coverage_rows),
                },
                out_fetch.name: {
                    "sha256": sha256_file(out_fetch),
                    "rows": len(fetch_rows),
                },
            },
        },
    )
    for check in checks:
        LOGGER.info("[%s] %s -- %s", check["status"], check["check"], check["detail"])
    LOGGER.info(
        "EXECUTION OHLCV PANEL: %s rows=%d complete=%d/%d -> %s",
        "PASS" if accepted else "FAIL",
        len(output),
        sum(row["status"] == "complete" for row in coverage_rows),
        len(coverage_rows),
        out_dir,
    )
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path
from typing import Any

import pandas as pd


LOGGER = logging.getLogger("fetch_ticker_industry_yf")
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT_CSV = SCRIPT_DIR / "tiker_industry.csv"
LEGACY_INPUT_CSV = SCRIPT_DIR / "ticker_industry.csv"
DEFAULT_OUTPUT_CSV = SCRIPT_DIR / "ticker_industry_output.csv"
DEFAULT_CACHE_DIR = SCRIPT_DIR / "_yf_cache"
DEFAULT_COL_CANDIDATES = ("Ticker", "Tickers", "ticker", "tickers", "Symbol", "symbol")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read a ticker CSV, fetch company name and industry from Yahoo Finance, and write an output CSV."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_CSV, help="Input CSV containing tickers.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_CSV, help="Output CSV path.")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR, help="Writable cache directory for yfinance.")
    parser.add_argument("--ticker-column", type=str, default="", help="Optional explicit ticker column name.")
    parser.add_argument("--sleep-sec", type=float, default=0.35, help="Delay between Yahoo Finance requests.")
    return parser.parse_args()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def normalize_ticker(raw: Any) -> str:
    return str(raw or "").strip().upper().replace(".", "-")


def read_csv_flexible(path: Path) -> pd.DataFrame:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return pd.read_csv(path, dtype=str, encoding=encoding).fillna("")
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
        except pd.errors.EmptyDataError:
            raise ValueError(f"Input CSV is empty: {path}")
    raise ValueError(f"Could not decode CSV {path}: {last_error}")


def _normalize_header(value: str) -> str:
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())


def find_ticker_column(df: pd.DataFrame, explicit: str) -> str:
    if explicit.strip():
        want = _normalize_header(explicit)
        for col in df.columns:
            if _normalize_header(col) == want:
                return str(col)
        raise ValueError(f"Ticker column '{explicit}' not found in input CSV.")

    norm_to_raw = {_normalize_header(col): str(col) for col in df.columns}
    for candidate in DEFAULT_COL_CANDIDATES:
        found = norm_to_raw.get(_normalize_header(candidate))
        if found:
            return found
    if len(df.columns) == 0:
        raise ValueError("Input CSV has no columns.")
    return str(df.columns[0])


def configure_yfinance_cache(cache_dir: Path) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    import yfinance as yf  # type: ignore

    try:
        from yfinance import cache as yf_cache  # type: ignore

        yf_cache.set_cache_location(str(cache_dir))
    except Exception:
        try:
            yf.set_tz_cache_location(str(cache_dir))
        except Exception:
            LOGGER.warning("Could not redirect yfinance cache location; continuing with library defaults.")


def extract_company_name(info: dict[str, Any]) -> str:
    for key in ("longName", "shortName", "displayName", "name"):
        value = str(info.get(key) or "").strip()
        if value:
            return value
    return ""


def extract_industry(info: dict[str, Any]) -> str:
    for key in ("industry", "industryDisp", "industryKey"):
        value = str(info.get(key) or "").strip()
        if value:
            return value
    return ""


def fetch_yahoo_profile(ticker: str) -> tuple[str, str]:
    import yfinance as yf  # type: ignore

    info = yf.Ticker(ticker).get_info() or {}
    if not isinstance(info, dict):
        info = {}
    return extract_company_name(info), extract_industry(info)


def main() -> None:
    configure_logging()
    args = parse_args()

    input_csv = args.input.expanduser().resolve()
    output_csv = args.output.expanduser().resolve()
    cache_dir = args.cache_dir.expanduser().resolve()

    if not input_csv.exists() and input_csv == DEFAULT_INPUT_CSV.resolve() and LEGACY_INPUT_CSV.exists():
        input_csv = LEGACY_INPUT_CSV.resolve()

    if not input_csv.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_csv}")

    df = read_csv_flexible(input_csv)
    ticker_col = find_ticker_column(df, args.ticker_column)
    configure_yfinance_cache(cache_dir)

    tickers = []
    seen: set[str] = set()
    for raw in df[ticker_col].tolist():
        ticker = normalize_ticker(raw)
        if ticker and ticker not in seen:
            seen.add(ticker)
            tickers.append(ticker)

    if not tickers:
        raise ValueError(f"No tickers found in column '{ticker_col}' from {input_csv}")

    rows: list[dict[str, str]] = []
    total = len(tickers)
    for idx, ticker in enumerate(tickers, start=1):
        company_name = ""
        industry = ""
        try:
            company_name, industry = fetch_yahoo_profile(ticker)
            LOGGER.info("[%d/%d] %s company=%r industry=%r", idx, total, ticker, company_name, industry)
        except Exception as exc:
            LOGGER.warning("[%d/%d] %s failed: %s", idx, total, ticker, exc)
        rows.append(
            {
                "Ticker": ticker,
                "CompanyName": company_name,
                "Industry": industry,
            }
        )
        if idx < total and args.sleep_sec > 0:
            time.sleep(float(args.sleep_sec))

    out_df = pd.DataFrame(rows, columns=["Ticker", "CompanyName", "Industry"])
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(output_csv, index=False)
    LOGGER.info("Wrote %d rows to %s", len(out_df), output_csv)


if __name__ == "__main__":
    main()

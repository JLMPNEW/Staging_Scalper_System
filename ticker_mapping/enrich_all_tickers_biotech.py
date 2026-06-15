#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import requests


LOGGER = logging.getLogger("enrich_all_tickers_biotech")
SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_INPUT_CSV = SCRIPT_DIR / "All_tickers_biotech.csv"
DEFAULT_OUTPUT_CSV = SCRIPT_DIR / "All_tickers_biotech_enriched.csv"
DEFAULT_CACHE_DIR = SCRIPT_DIR / "_identity_enrichment_cache"

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

BASE_COLUMNS = [
    "Ticker",
    "CIK",
    "CompanyName",
    "Exchange",
    "sector",
    "industry",
    "industry_aggregate",
]
REQUIRED_IDENTITY_COLUMNS = [
    "SecurityType",
    "IsPrimaryListing",
    "ListingStatus",
    "Country",
    "Currency",
    "ManualInclude",
    "ManualExclude",
    "ManualReview",
    "Notes",
]
AUDIT_COLUMNS = [
    "IdentityDataSources",
    "MissingIdentityFields",
]

DERIVATIVE_TYPES = {"ETF", "Preferred", "Warrant", "Unit", "Right"}
PRIMARY_ELIGIBLE_TYPES = {"Common Stock", "Ordinary Shares", "ADR/ADS", "Unknown"}
EXCHANGE_PRIORITY = {
    "NASDAQ": 10,
    "NASD": 10,
    "NYSE": 9,
    "NEW YORK STOCK EXCHANGE": 9,
    "NYSE AMERICAN": 8,
    "NYSE MKT": 8,
    "AMEX": 8,
    "NYSE ARCA": 7,
    "CBOE": 6,
    "BATS": 6,
    "IEX": 5,
    "OTC": 1,
    "OTCQX": 1,
    "OTCQB": 1,
    "OTC PINK": 1,
}


@dataclass(frozen=True)
class DirectoryEntry:
    ticker: str
    security_name: str
    exchange: str
    etf: str
    test_issue: str
    financial_status: str
    source: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Enrich All_tickers_biotech.csv with identity fields needed by the "
            "biotech screener/index foundation."
        )
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_CSV)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_CSV)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--overwrite-existing", action="store_true", help="Overwrite populated enrichment fields.")
    parser.add_argument("--disable-ib", action="store_true", help="Skip Interactive Brokers contract-detail enrichment.")
    parser.add_argument("--ib-host", default="127.0.0.1")
    parser.add_argument("--ib-ports", default="7497,7496,4002,4001", help="Comma-separated TWS/Gateway ports to try.")
    parser.add_argument("--ib-client-id", type=int, default=71)
    parser.add_argument("--ib-timeout-sec", type=float, default=8.0)
    parser.add_argument("--ib-sleep-sec", type=float, default=0.05)
    parser.add_argument("--max-ib-consecutive-failures", type=int, default=25)
    parser.add_argument(
        "--disable-us-listing-fallback",
        action="store_true",
        help="Do not infer Country=United States and Currency=USD from confirmed US exchange listings.",
    )
    parser.add_argument("--http-timeout-sec", type=float, default=30.0)
    return parser.parse_args()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logging.getLogger("ib_insync").setLevel(logging.WARNING)
    logging.getLogger("ib_insync.client").setLevel(logging.WARNING)
    logging.getLogger("ib_insync.wrapper").setLevel(logging.WARNING)


def normalize_ticker(raw: Any) -> str:
    return str(raw or "").strip().upper().replace(".", "-")


def normalize_cik(raw: Any) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits.zfill(10) if digits else ""


def normalize_header(raw: Any) -> str:
    return "".join(ch.lower() for ch in str(raw or "") if ch.isalnum())


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


def standardize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    norm_to_raw = {normalize_header(col): str(col) for col in df.columns}
    rename: dict[str, str] = {}
    aliases = {
        "Ticker": ("ticker", "tickers", "symbol"),
        "CIK": ("cik",),
        "CompanyName": ("companyname", "company", "name", "securityname"),
        "Exchange": ("exchange",),
    }
    for target, candidates in aliases.items():
        if target in df.columns:
            continue
        for candidate in candidates:
            raw = norm_to_raw.get(candidate)
            if raw:
                rename[raw] = target
                break
    if rename:
        df = df.rename(columns=rename)
    if "Ticker" not in df.columns:
        raise ValueError("Input CSV must contain a ticker column such as Ticker, Tickers, or Symbol.")
    df["Ticker"] = df["Ticker"].map(normalize_ticker)
    if "CIK" in df.columns:
        df["CIK"] = df["CIK"].map(normalize_cik)
    for col in BASE_COLUMNS + REQUIRED_IDENTITY_COLUMNS + AUDIT_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df


def cache_get_text(cache_dir: Path, name: str, url: str, timeout_sec: float) -> str:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / name
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8", errors="replace")
    headers = {"User-Agent": "JL, Independent Research, jm.357@hotmail.com"}
    resp = requests.get(url, headers=headers, timeout=timeout_sec)
    resp.raise_for_status()
    text = resp.text
    cache_path.write_text(text, encoding="utf-8")
    return text


def parse_pipe_directory(text: str, *, source: str) -> list[dict[str, str]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    lines = [line for line in lines if not line.lower().startswith("file creation time")]
    if not lines:
        return []
    header = lines[0].split("|")
    rows = []
    for line in lines[1:]:
        parts = line.split("|")
        if len(parts) != len(header):
            continue
        row = {header[i]: parts[i] for i in range(len(header))}
        row["_source"] = source
        rows.append(row)
    return rows


def load_nasdaq_directories(cache_dir: Path, timeout_sec: float) -> dict[str, DirectoryEntry]:
    listed = parse_pipe_directory(
        cache_get_text(cache_dir, "nasdaqlisted.txt", NASDAQ_LISTED_URL, timeout_sec),
        source="nasdaqtrader:nasdaqlisted",
    )
    other = parse_pipe_directory(
        cache_get_text(cache_dir, "otherlisted.txt", OTHER_LISTED_URL, timeout_sec),
        source="nasdaqtrader:otherlisted",
    )
    entries: dict[str, DirectoryEntry] = {}
    for row in listed:
        ticker = normalize_ticker(row.get("Symbol"))
        if not ticker:
            continue
        entries[ticker] = DirectoryEntry(
            ticker=ticker,
            security_name=str(row.get("Security Name") or "").strip(),
            exchange="Nasdaq",
            etf=str(row.get("ETF") or "").strip().upper(),
            test_issue=str(row.get("Test Issue") or "").strip().upper(),
            financial_status=str(row.get("Financial Status") or "").strip().upper(),
            source=str(row.get("_source") or ""),
        )
    for row in other:
        ticker = normalize_ticker(row.get("ACT Symbol") or row.get("NASDAQ Symbol"))
        if not ticker:
            continue
        entries[ticker] = DirectoryEntry(
            ticker=ticker,
            security_name=str(row.get("Security Name") or "").strip(),
            exchange=map_other_exchange(str(row.get("Exchange") or "").strip().upper()),
            etf=str(row.get("ETF") or "").strip().upper(),
            test_issue=str(row.get("Test Issue") or "").strip().upper(),
            financial_status="",
            source=str(row.get("_source") or ""),
        )
    return entries


def map_other_exchange(code: str) -> str:
    return {
        "A": "NYSE American",
        "N": "NYSE",
        "P": "NYSE Arca",
        "Z": "Cboe BZX",
        "V": "IEX",
    }.get(code, code)


def infer_security_type(security_name: str, etf_flag: str = "") -> str:
    text = str(security_name or "").upper()
    if etf_flag == "Y" or "ETF" in text or "EXCHANGE TRADED FUND" in text:
        return "ETF"
    if re.search(r"\bWARRANTS?\b|\bWT\b", text):
        return "Warrant"
    if re.search(r"\bUNITS?\b", text):
        return "Unit"
    if re.search(r"\bRIGHTS?\b", text):
        return "Right"
    if "PREFERRED" in text or "PREFERENCE" in text or "DEPOSITARY SHARES" in text:
        return "Preferred"
    if "AMERICAN DEPOSITARY" in text or re.search(r"\bADS\b|\bADR\b", text):
        return "ADR/ADS"
    if "ORDINARY SHARES" in text or "ORDINARY SHARE" in text:
        return "Ordinary Shares"
    if "COMMON STOCK" in text or "COMMON SHARES" in text or "COMMON SHARE" in text:
        return "Common Stock"
    return "Unknown"


def infer_listing_status(entry: Optional[DirectoryEntry]) -> str:
    if entry is None:
        return "unknown"
    if entry.test_issue == "Y":
        return "test_issue"
    if entry.financial_status and entry.financial_status != "N":
        return f"active_financial_status_{entry.financial_status}"
    return "active"


def should_fill(current: Any, overwrite_existing: bool) -> bool:
    return overwrite_existing or not str(current or "").strip()


def append_source(existing: Any, source: str) -> str:
    values = [x for x in str(existing or "").split(";") if x]
    if source and source not in values:
        values.append(source)
    return ";".join(values)


def apply_nasdaq_fields(df: pd.DataFrame, directory: dict[str, DirectoryEntry], overwrite_existing: bool) -> pd.DataFrame:
    df = df.copy()
    for idx, row in df.iterrows():
        ticker = normalize_ticker(row.get("Ticker"))
        entry = directory.get(ticker)
        if entry is None:
            continue
        if should_fill(row.get("SecurityType"), overwrite_existing):
            df.at[idx, "SecurityType"] = infer_security_type(entry.security_name, entry.etf)
        if should_fill(row.get("ListingStatus"), overwrite_existing):
            df.at[idx, "ListingStatus"] = infer_listing_status(entry)
        if should_fill(row.get("Exchange"), overwrite_existing) and entry.exchange:
            df.at[idx, "Exchange"] = entry.exchange
        if should_fill(row.get("CompanyName"), overwrite_existing) and entry.security_name:
            df.at[idx, "CompanyName"] = entry.security_name
        df.at[idx, "IdentityDataSources"] = append_source(row.get("IdentityDataSources"), entry.source)
    return df


def parse_ib_ports(raw: Any) -> list[int]:
    ports: list[int] = []
    for item in str(raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            ports.append(int(item))
        except ValueError:
            LOGGER.warning("Ignoring invalid IB port: %s", item)
    return ports or [7497, 7496, 4002, 4001]


def ib_symbol_candidates(ticker: str) -> list[str]:
    ticker = normalize_ticker(ticker)
    values = [ticker]
    if "-" in ticker:
        values.append(ticker.replace("-", "."))
        values.append(ticker.replace("-", " "))
    return list(dict.fromkeys(x for x in values if x))


def connect_ib(host: str, ports: list[int], client_id: int, timeout_sec: float) -> tuple[Any, int] | tuple[None, None]:
    try:
        from ib_insync import IB  # type: ignore
    except ImportError:
        LOGGER.warning("Interactive Brokers enrichment requires ib_insync. Install it with: pip install ib_insync")
        return None, None

    for port in ports:
        ib = IB()
        try:
            ib.connect(host, int(port), clientId=int(client_id), timeout=float(timeout_sec), readonly=True)
            if ib.isConnected():
                LOGGER.info("Connected to Interactive Brokers at %s:%s with clientId=%s", host, port, client_id)
                return ib, int(port)
        except TypeError:
            try:
                ib.connect(host, int(port), clientId=int(client_id), timeout=float(timeout_sec))
                if ib.isConnected():
                    LOGGER.info("Connected to Interactive Brokers at %s:%s with clientId=%s", host, port, client_id)
                    return ib, int(port)
            except Exception as exc:
                LOGGER.debug("IB connection failed on %s:%s: %s", host, port, exc)
        except Exception as exc:
            LOGGER.debug("IB connection failed on %s:%s: %s", host, port, exc)
        try:
            ib.disconnect()
        except Exception:
            pass
    LOGGER.warning("Could not connect to Interactive Brokers on %s ports %s", host, ",".join(str(p) for p in ports))
    return None, None


def ib_security_type(sec_type: str, stock_type: str = "") -> str:
    sec = str(sec_type or "").upper()
    stock = str(stock_type or "").upper()
    if sec == "STK":
        if "ADR" in stock or "ADS" in stock:
            return "ADR/ADS"
        return "Common Stock"
    if sec == "ETF":
        return "ETF"
    if sec == "WAR":
        return "Warrant"
    if sec == "OPT":
        return "Option"
    if sec:
        return sec.title()
    return ""


def infer_country_from_ib(currency: str, exchange_text: str) -> str:
    cur = str(currency or "").upper()
    exch = str(exchange_text or "").upper()
    if any(token in exch for token in ("NASDAQ", "NYSE", "AMEX", "ARCA", "BATS", "CBOE", "IEX", "PINK", "OTC")):
        return "United States"
    if cur == "USD":
        return "United States"
    if cur == "CAD" or any(token in exch for token in ("TSE", "VENTURE", "CNQ", "ALPHA")):
        return "Canada"
    if cur == "GBP" or "LSE" in exch:
        return "United Kingdom"
    if cur == "EUR":
        return "European Union"
    if cur == "CHF":
        return "Switzerland"
    if cur == "JPY":
        return "Japan"
    if cur == "AUD":
        return "Australia"
    if cur == "HKD":
        return "Hong Kong"
    return ""


def choose_ib_contract_detail(details: list[Any], ticker: str) -> Any | None:
    if not details:
        return None
    symbols = {x.upper() for x in ib_symbol_candidates(ticker)}

    def score(detail: Any) -> int:
        contract = getattr(detail, "contract", None)
        symbol = str(getattr(contract, "symbol", "") or "").upper()
        sec_type = str(getattr(contract, "secType", "") or "").upper()
        currency = str(getattr(contract, "currency", "") or "").upper()
        primary = str(getattr(contract, "primaryExchange", "") or "").upper()
        valid = str(getattr(detail, "validExchanges", "") or "").upper()
        value = 0
        if symbol in symbols:
            value += 100
        if sec_type == "STK":
            value += 50
        if currency == "USD":
            value += 20
        if any(token in f"{primary},{valid}" for token in ("NASDAQ", "NYSE", "AMEX", "ARCA", "PINK", "OTC")):
            value += 10
        return value

    return max(details, key=score)


def fetch_ib_identity(ib: Any, ticker: str, existing_currency: str = "") -> dict[str, str]:
    from ib_insync import Stock  # type: ignore

    currencies = [str(existing_currency or "").strip().upper(), "USD", "CAD"]
    currencies = list(dict.fromkeys(x for x in currencies if x))
    for symbol in ib_symbol_candidates(ticker):
        for currency in currencies:
            contract = Stock(symbol, "SMART", currency)
            details = ib.reqContractDetails(contract)
            detail = choose_ib_contract_detail(details, ticker)
            if detail is None:
                continue
            ib_contract = getattr(detail, "contract", None)
            sec_type = str(getattr(ib_contract, "secType", "") or "")
            stock_type = str(getattr(detail, "stockType", "") or "")
            primary_exchange = str(getattr(ib_contract, "primaryExchange", "") or "")
            valid_exchanges = str(getattr(detail, "validExchanges", "") or "")
            ib_currency = str(getattr(ib_contract, "currency", "") or "").strip()
            exchange_text = f"{primary_exchange},{valid_exchanges}"
            return {
                "CompanyName": str(getattr(detail, "longName", "") or "").strip(),
                "SecurityType": ib_security_type(sec_type, stock_type),
                "Country": infer_country_from_ib(ib_currency, exchange_text),
                "Currency": ib_currency,
                "Exchange": primary_exchange,
            }
    return {}


def apply_ib_fields(
    df: pd.DataFrame,
    *,
    host: str,
    ports: list[int],
    client_id: int,
    timeout_sec: float,
    overwrite_existing: bool,
    sleep_sec: float,
    disable_ib: bool,
    max_consecutive_failures: int,
) -> pd.DataFrame:
    if disable_ib:
        LOGGER.info("Interactive Brokers enrichment disabled; leaving IB fields blank unless already populated.")
        return df
    ib, _port = connect_ib(host, ports, client_id, timeout_sec)
    if ib is None:
        return df

    df = df.copy()
    needed = df[
        df.apply(
            lambda row: any(
                should_fill(row.get(col), overwrite_existing)
                for col in ("Country", "Currency", "SecurityType", "CompanyName")
            ),
            axis=1,
        )
    ]
    total = len(needed)
    consecutive_failures = 0
    try:
        for count, (idx, row) in enumerate(needed.iterrows(), start=1):
            ticker = normalize_ticker(row.get("Ticker"))
            if not ticker:
                continue
            try:
                info = fetch_ib_identity(ib, ticker, str(row.get("Currency") or ""))
            except Exception as exc:
                consecutive_failures += 1
                LOGGER.warning("[%d/%d] IB failed for %s: %s", count, total, ticker, exc)
                if consecutive_failures >= max(1, int(max_consecutive_failures)):
                    LOGGER.warning(
                        "Interactive Brokers enrichment stopped after %d consecutive failures; leaving remaining IB fields blank.",
                        consecutive_failures,
                    )
                    break
                continue
            if not info:
                consecutive_failures += 1
                LOGGER.info("[%d/%d] IB returned no contract details for %s", count, total, ticker)
                if consecutive_failures >= max(1, int(max_consecutive_failures)):
                    LOGGER.warning(
                        "Interactive Brokers enrichment stopped after %d consecutive misses; leaving remaining IB fields blank.",
                        consecutive_failures,
                    )
                    break
                continue
            consecutive_failures = 0
            for col in ("Country", "Currency", "SecurityType", "CompanyName", "Exchange"):
                value = str(info.get(col) or "").strip()
                current = str(df.at[idx, col] or "").strip()
                if col == "SecurityType" and value == "Unknown" and current and current != "Unknown":
                    continue
                if col == "SecurityType" and value == "Common Stock" and current not in {"", "Unknown", "Common Stock"}:
                    continue
                if value and should_fill(df.at[idx, col], overwrite_existing):
                    df.at[idx, col] = value
            df.at[idx, "IdentityDataSources"] = append_source(df.at[idx, "IdentityDataSources"], "interactivebrokers:contract_details")
            LOGGER.info("[%d/%d] IB enriched %s", count, total, ticker)
            if count < total and sleep_sec > 0:
                time.sleep(float(sleep_sec))
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
    return df


def apply_us_listing_fallback_fields(df: pd.DataFrame, overwrite_existing: bool) -> pd.DataFrame:
    df = df.copy()
    for idx, row in df.iterrows():
        sources = str(row.get("IdentityDataSources") or "")
        if "nasdaqtrader:" not in sources:
            continue
        changed = False
        if should_fill(row.get("Country"), overwrite_existing):
            df.at[idx, "Country"] = "United States"
            changed = True
        if should_fill(row.get("Currency"), overwrite_existing):
            df.at[idx, "Currency"] = "USD"
            changed = True
        if changed:
            df.at[idx, "IdentityDataSources"] = append_source(
                df.at[idx, "IdentityDataSources"],
                "nasdaqtrader:us_listing_inference",
            )
    return df


def derive_primary_listing(df: pd.DataFrame, overwrite_existing: bool) -> pd.DataFrame:
    df = df.copy()
    df["_cik_norm"] = df["CIK"].map(normalize_cik) if "CIK" in df.columns else ""
    df["_primary_score"] = df.apply(primary_score, axis=1)
    for group_key, group in df.groupby(df["_cik_norm"].where(df["_cik_norm"] != "", df["Ticker"])):
        if not str(group_key).strip():
            continue
        eligible = group[group["_primary_score"] > -10_000]
        if eligible.empty:
            selected_idx = group["_primary_score"].idxmax()
        else:
            selected_idx = eligible["_primary_score"].idxmax()
        for idx in group.index:
            if should_fill(df.at[idx, "IsPrimaryListing"], overwrite_existing):
                df.at[idx, "IsPrimaryListing"] = "True" if idx == selected_idx else "False"
    return df.drop(columns=["_cik_norm", "_primary_score"], errors="ignore")


def primary_score(row: pd.Series) -> int:
    status = str(row.get("ListingStatus") or "").lower()
    security_type = str(row.get("SecurityType") or "Unknown").strip() or "Unknown"
    exchange = str(row.get("Exchange") or "").upper()
    ticker = str(row.get("Ticker") or "").upper()
    score = 0
    if status == "active":
        score += 100
    elif status == "unknown":
        score += 20
    else:
        score -= 100
    if security_type in PRIMARY_ELIGIBLE_TYPES:
        score += 50
    if security_type in DERIVATIVE_TYPES:
        score -= 10_000
    score += max(EXCHANGE_PRIORITY.get(exchange, 0), max((v for k, v in EXCHANGE_PRIORITY.items() if k in exchange), default=0))
    if ticker.endswith(("W", "WS", "WT", "U", "R")):
        score -= 100
    if ticker.endswith("F") and "OTC" in exchange:
        score -= 10
    return score


def update_missing_fields(df: pd.DataFrame) -> pd.DataFrame:
    tracked = ["SecurityType", "IsPrimaryListing", "ListingStatus", "Country", "Currency"]
    df = df.copy()
    for idx, row in df.iterrows():
        missing = [col for col in tracked if not str(row.get(col) or "").strip()]
        df.at[idx, "MissingIdentityFields"] = ";".join(missing)
    return df


def order_columns(df: pd.DataFrame) -> pd.DataFrame:
    first = BASE_COLUMNS + REQUIRED_IDENTITY_COLUMNS + AUDIT_COLUMNS
    present_first = [col for col in first if col in df.columns]
    rest = [col for col in df.columns if col not in present_first]
    return df[present_first + rest]


def main() -> None:
    configure_logging()
    args = parse_args()

    input_csv = args.input.expanduser().resolve()
    output_csv = args.output.expanduser().resolve()
    cache_dir = args.cache_dir.expanduser().resolve()

    if not input_csv.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_csv}")

    df = standardize_columns(read_csv_flexible(input_csv))
    LOGGER.info("Loaded %d rows from %s", len(df), input_csv)

    try:
        directory = load_nasdaq_directories(cache_dir, float(args.http_timeout_sec))
        LOGGER.info("Loaded %d Nasdaq Trader symbol directory entries", len(directory))
        df = apply_nasdaq_fields(df, directory, bool(args.overwrite_existing))
    except Exception as exc:
        LOGGER.warning("Nasdaq Trader enrichment failed; continuing with existing fields: %s", exc)

    df = apply_ib_fields(
        df,
        host=str(args.ib_host),
        ports=parse_ib_ports(args.ib_ports),
        client_id=int(args.ib_client_id),
        timeout_sec=float(args.ib_timeout_sec),
        overwrite_existing=bool(args.overwrite_existing),
        sleep_sec=float(args.ib_sleep_sec),
        disable_ib=bool(args.disable_ib),
        max_consecutive_failures=int(args.max_ib_consecutive_failures),
    )
    if not bool(args.disable_us_listing_fallback):
        df = apply_us_listing_fallback_fields(df, bool(args.overwrite_existing))
    df = derive_primary_listing(df, bool(args.overwrite_existing))
    df = update_missing_fields(df)
    df = order_columns(df)

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_csv, index=False)
    LOGGER.info("Wrote enriched universe CSV: %s", output_csv)
    LOGGER.info("Rows=%d missing_any_identity=%d", len(df), int((df["MissingIdentityFields"] != "").sum()))


if __name__ == "__main__":
    main()

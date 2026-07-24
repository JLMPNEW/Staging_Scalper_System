#!/usr/bin/env python3
"""Generic technology-subsector ticker identity enrichment.

This script intentionally contains no semiconductor/software-specific defaults.
Pass subsector defaults by CLI so the same entry point can enrich future
technology universes without forking the enrichment logic.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import build_cik_ticker_mapping as sec_mapping  # noqa: E402
import enrich_all_tickers_biotech as identity_enrichment  # noqa: E402


LOGGER = logging.getLogger("enrich_technology_tickers")
DEFAULT_USER_AGENT = "JL, Independent Research, jm.357@hotmail.com"

OUTPUT_COLUMNS = [
    "ticker",
    "investability_status",
    "company_name",
    "cik",
    "exchange",
    "sector",
    "industry",
    "subsector",
    "country",
    "currency",
    "security_type",
    "listing_status",
    "is_primary_listing",
]

AUDIT_COLUMNS = [
    "ticker",
    "sec_match_type",
    "sec_source",
    "identity_sources",
    "identity_missing_fields",
    "yahoo_status",
    "yahoo_error",
    "missing_fields",
]

SEC_TO_LOCAL_COLUMNS = {
    "CIK": "cik",
    "CompanyName": "company_name",
    "Exchange": "exchange",
}

IDENTITY_TO_LOCAL_COLUMNS = {
    "CIK": "cik",
    "CompanyName": "company_name",
    "Exchange": "exchange",
    "Country": "country",
    "Currency": "currency",
    "SecurityType": "security_type",
    "ListingStatus": "listing_status",
    "IsPrimaryListing": "is_primary_listing",
}

HEADER_ALIASES = {
    "ticker": ("ticker", "tickers", "symbol", "symbols", "name"),
    "company_name": ("companyname", "company", "name", "securityname", "issuer", "issuername"),
    "cik": ("cik",),
    "exchange": ("exchange", "primaryexchange", "listingexchange"),
    "sector": ("sector",),
    "industry": ("industry", "industryname"),
    "subsector": ("subsector", "subindustry", "industryaggregate", "industrygroup", "cohort", "role"),
    "country": ("country", "domicile"),
    "currency": ("currency", "tradingcurrency"),
    "security_type": ("securitytype", "security", "sectype", "instrumenttype"),
    "listing_status": ("listingstatus", "status"),
    "investability_status": ("investabilitystatus", "investablestatus"),
    "is_primary_listing": ("isprimarylisting", "primarylisting", "primary"),
}

KNOWN_INVESTABILITY_STATUSES = {
    "investable",
    "non_investable_listing_status",
    "non_investable_security_type",
    "review_listing_status",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enrich any technology subsector ticker CSV with CIK/listing/profile identity fields."
    )
    parser.add_argument("--input", type=Path, required=True, help="Input CSV containing at least ticker/company columns.")
    parser.add_argument("--output", type=Path, default=None, help="Output CSV. Defaults to overwriting --input.")
    parser.add_argument("--audit-output", type=Path, default=None, help="Audit CSV. Defaults beside output.")
    parser.add_argument("--cache-prefix", default="", help="Cache/report prefix. Defaults to input file stem.")
    parser.add_argument("--sec-cache-dir", type=Path, default=None)
    parser.add_argument("--identity-cache-dir", type=Path, default=None)
    parser.add_argument("--yahoo-cache-dir", type=Path, default=None)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--http-timeout-sec", type=float, default=30.0)
    parser.add_argument("--yahoo-ttl-days", type=float, default=30.0)
    parser.add_argument("--yahoo-sleep-sec", type=float, default=0.15)
    parser.add_argument("--overwrite-existing", action="store_true")
    parser.add_argument("--force-yahoo-refresh", action="store_true")
    parser.add_argument("--skip-sec", action="store_true")
    parser.add_argument("--skip-nasdaq", action="store_true")
    parser.add_argument("--skip-yahoo", action="store_true")
    parser.add_argument("--disable-ib", action="store_true", default=True)
    parser.add_argument("--enable-ib", dest="disable_ib", action="store_false")
    parser.add_argument("--ib-host", default="127.0.0.1")
    parser.add_argument("--ib-ports", default="7497,7496,4002,4001")
    parser.add_argument("--ib-client-id", type=int, default=73)
    parser.add_argument("--ib-timeout-sec", type=float, default=8.0)
    parser.add_argument("--ib-sleep-sec", type=float, default=0.05)
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--default-sector", default="")
    parser.add_argument("--default-industry", default="")
    parser.add_argument("--default-subsector", default="")
    parser.add_argument("--default-country", default="")
    parser.add_argument("--default-currency", default="")
    parser.add_argument("--default-security-type", default="")
    parser.add_argument("--default-listing-status", default="")
    parser.add_argument("--default-primary-listing", default="")
    parser.add_argument("--panel", type=Path, default=None, help="Optional workbook/CSV with authoritative subsector labels.")
    parser.add_argument("--panel-sheet", default="", help="Optional Excel sheet name for --panel.")
    parser.add_argument("--preserve-extra-columns", action=argparse.BooleanOptionalAction, default=True)
    return parser.parse_args()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logging.getLogger("yfinance").setLevel(logging.WARNING)
    logging.getLogger("ib_insync").setLevel(logging.WARNING)


def normalize_ticker(raw: Any) -> str:
    return str(raw or "").strip().upper().replace(".", "-")


def normalize_cik(raw: Any) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits.zfill(10) if digits else ""


def normalize_header(raw: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(raw or "").strip().lower())


def blank(raw: Any) -> bool:
    text = str(raw or "").strip()
    return text == "" or text.lower() in {"nan", "none", "null"}


def clean_text(raw: Any) -> str:
    return re.sub(r"\s+", " ", str(raw or "").strip())


def normalize_status_label(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def derive_investability_status(row: pd.Series) -> str:
    listing_status = normalize_status_label(row.get("listing_status"))
    security_type = normalize_status_label(row.get("security_type"))
    non_investable_listing_statuses = {
        "active_financial_status_d",
        "active_financial_status_e",
        "inactive_or_not_investable",
        "invalid_or_inactive",
    }
    investable_security_types = {
        "common_stock",
        "ordinary_shares",
        "adr_ads",
        "american_depositary_shares",
        "new_york_registry_shares",
    }
    if listing_status in non_investable_listing_statuses:
        return "non_investable_listing_status"
    if security_type and security_type not in investable_security_types:
        return "non_investable_security_type"
    if listing_status and listing_status != "active":
        return "review_listing_status"
    return "investable"


def should_fill(current: Any, overwrite_existing: bool) -> bool:
    return overwrite_existing or blank(current)


def normalize_bool(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    low = text.lower()
    if low in {"1", "true", "t", "yes", "y"}:
        return "TRUE"
    if low in {"0", "false", "f", "no", "n"}:
        return "FALSE"
    return text


def read_csv_flexible(path: Path) -> pd.DataFrame:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return pd.read_csv(path, dtype=str, encoding=encoding, keep_default_na=False).fillna("")
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
        except pd.errors.EmptyDataError:
            raise ValueError(f"Input CSV is empty: {path}")
    raise ValueError(f"Could not decode CSV {path}: {last_error}")


def apply_column_aliases(df: pd.DataFrame) -> pd.DataFrame:
    norm_to_raw = {normalize_header(col): str(col) for col in df.columns}
    rename: dict[str, str] = {}
    for target, candidates in HEADER_ALIASES.items():
        if target in df.columns:
            continue
        for candidate in candidates:
            raw = norm_to_raw.get(normalize_header(candidate))
            if raw:
                rename[raw] = target
                break
    return df.rename(columns=rename) if rename else df


def standardize_input_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = apply_column_aliases(df.copy().fillna(""))
    if "ticker" not in df.columns:
        raise ValueError("Input CSV must contain a ticker column.")
    for column in OUTPUT_COLUMNS:
        if column not in df.columns:
            df[column] = ""
    shifted_company_mask = df.apply(
        lambda row: blank(row.get("company_name"))
        and not blank(row.get("investability_status"))
        and normalize_status_label(row.get("investability_status")) not in KNOWN_INVESTABILITY_STATUSES,
        axis=1,
    )
    shifted_company_count = int(shifted_company_mask.sum())
    if shifted_company_count:
        LOGGER.warning(
            "Recovered company_name from non-status investability_status values for %d rows",
            shifted_company_count,
        )
        df.loc[shifted_company_mask, "company_name"] = df.loc[shifted_company_mask, "investability_status"].map(clean_text)
        df.loc[shifted_company_mask, "investability_status"] = ""
    df["ticker"] = df["ticker"].map(normalize_ticker)
    df["cik"] = df["cik"].map(normalize_cik)
    df["is_primary_listing"] = df["is_primary_listing"].map(normalize_bool)
    df = df[df["ticker"] != ""]
    duplicate_mask = df["ticker"].duplicated(keep=False)
    if duplicate_mask.any():
        duplicate_tickers = sorted(set(df.loc[duplicate_mask, "ticker"].astype(str)))
        LOGGER.warning(
            "Input contains %d duplicate ticker rows across %d tickers; keeping first row per ticker: %s",
            int(duplicate_mask.sum()),
            len(duplicate_tickers),
            duplicate_tickers[:50],
        )
    df = df.drop_duplicates(subset=["ticker"], keep="first").reset_index(drop=True)
    return df


def make_sec_input_rows(df: pd.DataFrame) -> list[sec_mapping.InputTicker]:
    return [
        sec_mapping.InputTicker(ticker=str(row["ticker"]), company_name=str(row.get("company_name", "")))
        for row in df.to_dict("records")
        if normalize_ticker(row.get("ticker"))
    ]


def apply_sec_fields(
    df: pd.DataFrame,
    *,
    cache_dir: Path,
    timeout_sec: float,
    user_agent: str,
    overwrite_existing: bool,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    entries = sec_mapping.load_sec_entries(
        cache_dir=cache_dir,
        timeout=float(timeout_sec),
        user_agent=sec_mapping.resolve_user_agent(user_agent),
    )
    rows = sec_mapping.build_rows(
        make_sec_input_rows(df),
        entries,
        cache_dir=cache_dir,
        timeout=float(timeout_sec),
        user_agent=sec_mapping.resolve_user_agent(user_agent),
    )
    sec_df = pd.DataFrame(rows).fillna("")
    df = df.copy()
    sec_by_ticker = {normalize_ticker(row.get("Ticker")): row for row in sec_df.to_dict("records")}
    for idx, row in df.iterrows():
        sec_row = sec_by_ticker.get(normalize_ticker(row.get("ticker")))
        if not sec_row:
            continue
        for source_col, local_col in SEC_TO_LOCAL_COLUMNS.items():
            value = clean_text(sec_row.get(source_col))
            if source_col == "CIK":
                value = normalize_cik(value)
            if value and should_fill(row.get(local_col), overwrite_existing):
                df.at[idx, local_col] = value
    return df, sec_df


def to_identity_frame(df: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Ticker": df["ticker"].map(normalize_ticker),
            "CIK": df["cik"].map(normalize_cik),
            "CompanyName": df["company_name"].fillna("").astype(str),
            "Exchange": df["exchange"].fillna("").astype(str),
            "sector": df["sector"].fillna("").astype(str),
            "industry": df["industry"].fillna("").astype(str),
            "industry_aggregate": df["subsector"].fillna("").astype(str),
            "SecurityType": df["security_type"].fillna("").astype(str),
            "IsPrimaryListing": df["is_primary_listing"].fillna("").astype(str),
            "ListingStatus": df["listing_status"].fillna("").astype(str),
            "Country": df["country"].fillna("").astype(str),
            "Currency": df["currency"].fillna("").astype(str),
            "ManualInclude": "",
            "ManualExclude": "",
            "ManualReview": "",
            "Notes": "",
            "IdentityDataSources": "",
            "MissingIdentityFields": "",
        }
    )


def apply_identity_fields(
    df: pd.DataFrame,
    *,
    cache_dir: Path,
    timeout_sec: float,
    overwrite_existing: bool,
    skip_nasdaq: bool,
    disable_ib: bool,
    ib_host: str,
    ib_ports: str,
    ib_client_id: int,
    ib_timeout_sec: float,
    ib_sleep_sec: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    identity_df = to_identity_frame(df)
    if not skip_nasdaq:
        directory = identity_enrichment.load_nasdaq_directories(cache_dir, float(timeout_sec))
        LOGGER.info("Loaded %d Nasdaq Trader symbol directory entries", len(directory))
        identity_df = identity_enrichment.apply_nasdaq_fields(identity_df, directory, overwrite_existing)
    identity_df = identity_enrichment.apply_ib_fields(
        identity_df,
        host=ib_host,
        ports=identity_enrichment.parse_ib_ports(ib_ports),
        client_id=int(ib_client_id),
        timeout_sec=float(ib_timeout_sec),
        overwrite_existing=overwrite_existing,
        sleep_sec=float(ib_sleep_sec),
        disable_ib=disable_ib,
        max_consecutive_failures=25,
    )
    identity_df = identity_enrichment.apply_us_listing_fallback_fields(identity_df, overwrite_existing)
    identity_df = identity_enrichment.derive_primary_listing(identity_df, overwrite_existing)
    identity_df = identity_enrichment.update_missing_fields(identity_df)

    df = df.copy()
    by_ticker = {normalize_ticker(row.get("Ticker")): row for row in identity_df.to_dict("records")}
    for idx, row in df.iterrows():
        identity_row = by_ticker.get(normalize_ticker(row.get("ticker")))
        if not identity_row:
            continue
        for source_col, local_col in IDENTITY_TO_LOCAL_COLUMNS.items():
            value = clean_text(identity_row.get(source_col))
            if source_col == "CIK":
                value = normalize_cik(value)
            if source_col == "IsPrimaryListing":
                value = normalize_bool(value)
            if value and should_fill(row.get(local_col), overwrite_existing):
                df.at[idx, local_col] = value
    return df, identity_df


def cache_is_fresh(path: Path, ttl_days: float) -> bool:
    if ttl_days <= 0 or not path.exists():
        return False
    return time.time() - path.stat().st_mtime <= ttl_days * 86400.0


def yahoo_cache_name(ticker: str) -> str:
    safe = re.sub(r"[^A-Z0-9._-]+", "_", normalize_ticker(ticker))
    return f"{safe}.json"


def load_yahoo_info(
    ticker: str,
    *,
    cache_dir: Path,
    ttl_days: float,
    force_refresh: bool,
) -> tuple[dict[str, Any], str, str]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / yahoo_cache_name(ticker)
    if not force_refresh and cache_is_fresh(cache_path, ttl_days):
        try:
            return json.loads(cache_path.read_text(encoding="utf-8")), "cache", ""
        except Exception as exc:
            LOGGER.warning("Could not read Yahoo cache for %s: %s", ticker, exc)
    try:
        import yfinance as yf  # type: ignore
    except ImportError as exc:
        return {}, "import_error", str(exc)
    try:
        info = yf.Ticker(ticker).get_info() or {}
        if not isinstance(info, dict):
            info = {}
        cache_path.write_text(json.dumps(info, ensure_ascii=False, default=str), encoding="utf-8")
        return info, "live", ""
    except Exception as exc:
        if cache_path.exists():
            try:
                return json.loads(cache_path.read_text(encoding="utf-8")), "stale_cache_after_error", str(exc)
            except Exception:
                pass
        return {}, "error", str(exc)


def first_profile_value(info: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = clean_text(info.get(key))
        if value:
            return value
    return ""


def infer_security_type_from_yahoo(ticker: str, info: dict[str, Any]) -> str:
    quote_type = first_profile_value(info, ("quoteType",)).upper()
    if quote_type in {"ETF", "MUTUALFUND"}:
        return "ETF"
    if quote_type not in {"EQUITY", ""}:
        return quote_type.title()
    name_blob = first_profile_value(info, ("longName", "shortName", "displayName")).upper()
    ticker_norm = normalize_ticker(ticker)
    country = first_profile_value(info, ("country",))
    if "ADR" in name_blob or "ADS" in name_blob or (ticker_norm.endswith("Y") and country and country != "United States"):
        return "ADR/ADS"
    if ticker_norm.endswith("F") and country and country != "United States":
        return "Ordinary Shares"
    if quote_type == "EQUITY":
        return "Common Stock"
    return ""


def infer_listing_status_from_yahoo(info: dict[str, Any]) -> str:
    quote_type = first_profile_value(info, ("quoteType",))
    exchange = first_profile_value(info, ("exchange", "fullExchangeName"))
    price = info.get("regularMarketPrice", None)
    if quote_type.upper() == "EQUITY" and (exchange or price not in (None, "")):
        return "active"
    return ""


def apply_yahoo_fields(
    df: pd.DataFrame,
    *,
    cache_dir: Path,
    ttl_days: float,
    force_refresh: bool,
    sleep_sec: float,
    overwrite_existing: bool,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = df.copy()
    audit_rows: list[dict[str, str]] = []
    total = len(df)
    for idx, row in df.iterrows():
        ticker = normalize_ticker(row.get("ticker"))
        if not ticker:
            continue
        info, status, error = load_yahoo_info(
            ticker,
            cache_dir=cache_dir,
            ttl_days=ttl_days,
            force_refresh=force_refresh,
        )
        field_values = {
            "company_name": first_profile_value(info, ("longName", "shortName", "displayName", "name")),
            "sector": first_profile_value(info, ("sector",)),
            "industry": first_profile_value(info, ("industry", "industryDisp", "industryKey")),
            "country": first_profile_value(info, ("country",)),
            "currency": first_profile_value(info, ("currency", "financialCurrency")),
            "exchange": first_profile_value(info, ("fullExchangeName", "exchange")),
            "security_type": infer_security_type_from_yahoo(ticker, info),
            "listing_status": infer_listing_status_from_yahoo(info),
        }
        for column, value in field_values.items():
            if value and should_fill(row.get(column), overwrite_existing):
                df.at[idx, column] = value
        audit_rows.append({"ticker": ticker, "yahoo_status": status, "yahoo_error": error[:300]})
        LOGGER.info("[%d/%d] Yahoo %s status=%s", idx + 1, total, ticker, status)
        if idx + 1 < total and sleep_sec > 0:
            time.sleep(float(sleep_sec))
    return df, pd.DataFrame(audit_rows).fillna("")


def read_panel(path: Path, sheet: str) -> pd.DataFrame:
    if path.suffix.lower() in {".xlsx", ".xlsm", ".xls"}:
        kwargs: dict[str, Any] = {"dtype": str, "keep_default_na": False}
        if sheet:
            kwargs["sheet_name"] = sheet
        return pd.read_excel(path, **kwargs).fillna("")
    return read_csv_flexible(path)


def load_panel_map(path: Path | None, sheet: str) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    panel_path = path.expanduser().resolve()
    if not panel_path.exists():
        raise FileNotFoundError(f"Panel file not found: {panel_path}")
    raw = read_panel(panel_path, sheet)
    panel = apply_column_aliases(raw.copy().fillna(""))
    if "ticker" not in panel.columns:
        raise ValueError(f"Panel must contain a ticker/symbol column: {panel_path}")
    for column in OUTPUT_COLUMNS:
        if column not in panel.columns:
            panel[column] = ""
    out: dict[str, dict[str, str]] = {}
    for row in panel.to_dict("records"):
        ticker = normalize_ticker(row.get("ticker"))
        if not ticker:
            continue
        out[ticker] = {column: clean_text(row.get(column)) for column in OUTPUT_COLUMNS if column != "ticker"}
    LOGGER.info("Loaded %d panel mapping rows from %s", len(out), panel_path)
    return out


def apply_panel_fields(
    df: pd.DataFrame,
    panel_map: dict[str, dict[str, str]],
    overwrite_existing: bool,
) -> pd.DataFrame:
    if not panel_map:
        return df
    df = df.copy()
    for idx, row in df.iterrows():
        info = panel_map.get(normalize_ticker(row.get("ticker")))
        if not info:
            continue
        for column in OUTPUT_COLUMNS:
            if column == "ticker":
                continue
            value = info.get(column, "")
            if column == "cik":
                value = normalize_cik(value)
            if column == "is_primary_listing":
                value = normalize_bool(value)
            if value and should_fill(row.get(column), overwrite_existing):
                df.at[idx, column] = value
    return df


def apply_default_fields(df: pd.DataFrame, args: argparse.Namespace, overwrite_existing: bool) -> pd.DataFrame:
    defaults = {
        "sector": args.default_sector,
        "industry": args.default_industry,
        "subsector": args.default_subsector,
        "country": args.default_country,
        "currency": args.default_currency,
        "security_type": args.default_security_type,
        "listing_status": args.default_listing_status,
        "is_primary_listing": normalize_bool(args.default_primary_listing),
    }
    df = df.copy()
    for idx, row in df.iterrows():
        for column, raw_value in defaults.items():
            value = clean_text(raw_value)
            if value and should_fill(row.get(column), overwrite_existing):
                df.at[idx, column] = value
    return df


def missing_fields_for_row(row: pd.Series) -> str:
    return ";".join(column for column in OUTPUT_COLUMNS if blank(row.get(column)))


def build_audit(
    df: pd.DataFrame,
    sec_df: pd.DataFrame,
    identity_df: pd.DataFrame,
    yahoo_audit: pd.DataFrame,
) -> pd.DataFrame:
    audit = pd.DataFrame({"ticker": df["ticker"].map(normalize_ticker)})
    if not sec_df.empty:
        sec_small = sec_df.rename(
            columns={"Ticker": "ticker", "MatchType": "sec_match_type", "Source": "sec_source"}
        )
        audit = audit.merge(sec_small[["ticker", "sec_match_type", "sec_source"]], on="ticker", how="left")
    if not identity_df.empty:
        identity_small = identity_df.rename(
            columns={
                "Ticker": "ticker",
                "IdentityDataSources": "identity_sources",
                "MissingIdentityFields": "identity_missing_fields",
            }
        )
        audit = audit.merge(
            identity_small[["ticker", "identity_sources", "identity_missing_fields"]],
            on="ticker",
            how="left",
        )
    if not yahoo_audit.empty:
        audit = audit.merge(yahoo_audit, on="ticker", how="left")
    audit["missing_fields"] = df.apply(missing_fields_for_row, axis=1)
    for column in AUDIT_COLUMNS:
        if column not in audit.columns:
            audit[column] = ""
    return audit[AUDIT_COLUMNS].fillna("")


def backup_output(path: Path, cache_prefix: str) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_prefix = re.sub(r"[^A-Za-z0-9_-]+", "_", cache_prefix).strip("_") or "technology"
    backup_path = path.with_name(f"{path.stem}.pre_{safe_prefix}_enrichment_{stamp}{path.suffix}")
    shutil.copy2(path, backup_path)
    return backup_path


def summarize(df: pd.DataFrame) -> str:
    parts = []
    for column in OUTPUT_COLUMNS:
        populated = int(df[column].astype(str).str.strip().ne("").sum())
        parts.append(f"{column}={populated}/{len(df)}")
    return " ".join(parts)


def output_columns(df: pd.DataFrame, preserve_extra: bool) -> list[str]:
    if not preserve_extra:
        return OUTPUT_COLUMNS
    return OUTPUT_COLUMNS + [column for column in df.columns if column not in OUTPUT_COLUMNS]


def main() -> None:
    configure_logging()
    args = parse_args()
    input_path = args.input.expanduser().resolve()
    output_path = args.output.expanduser().resolve() if args.output else input_path
    cache_prefix = clean_text(args.cache_prefix) or input_path.stem
    safe_prefix = re.sub(r"[^A-Za-z0-9_-]+", "_", cache_prefix).strip("_") or "technology"
    audit_output = (
        args.audit_output.expanduser().resolve()
        if args.audit_output
        else output_path.with_name(f"{output_path.stem}_enrichment_audit.csv")
    )
    sec_cache_dir = args.sec_cache_dir.expanduser().resolve() if args.sec_cache_dir else SCRIPT_DIR / f"_{safe_prefix}_sec_cache"
    identity_cache_dir = (
        args.identity_cache_dir.expanduser().resolve()
        if args.identity_cache_dir
        else SCRIPT_DIR / f"_{safe_prefix}_identity_cache"
    )
    yahoo_cache_dir = (
        args.yahoo_cache_dir.expanduser().resolve()
        if args.yahoo_cache_dir
        else SCRIPT_DIR / f"_{safe_prefix}_yahoo_profile_cache"
    )

    if not input_path.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_path}")

    overwrite_existing = bool(args.overwrite_existing)
    df = standardize_input_columns(read_csv_flexible(input_path))
    LOGGER.info("Loaded %d technology tickers from %s", len(df), input_path)

    panel_map = load_panel_map(args.panel, str(args.panel_sheet or ""))
    df = apply_panel_fields(df, panel_map, overwrite_existing)

    sec_df = pd.DataFrame()
    if not bool(args.skip_sec):
        df, sec_df = apply_sec_fields(
            df,
            cache_dir=sec_cache_dir,
            timeout_sec=float(args.http_timeout_sec),
            user_agent=str(args.user_agent),
            overwrite_existing=overwrite_existing,
        )
        LOGGER.info("SEC CIK populated: %d/%d", int(df["cik"].astype(str).str.strip().ne("").sum()), len(df))

    identity_df = pd.DataFrame()
    if not bool(args.skip_nasdaq) or not bool(args.disable_ib):
        df, identity_df = apply_identity_fields(
            df,
            cache_dir=identity_cache_dir,
            timeout_sec=float(args.http_timeout_sec),
            overwrite_existing=overwrite_existing,
            skip_nasdaq=bool(args.skip_nasdaq),
            disable_ib=bool(args.disable_ib),
            ib_host=str(args.ib_host),
            ib_ports=str(args.ib_ports),
            ib_client_id=int(args.ib_client_id),
            ib_timeout_sec=float(args.ib_timeout_sec),
            ib_sleep_sec=float(args.ib_sleep_sec),
        )

    yahoo_audit = pd.DataFrame()
    if not bool(args.skip_yahoo):
        df, yahoo_audit = apply_yahoo_fields(
            df,
            cache_dir=yahoo_cache_dir,
            ttl_days=float(args.yahoo_ttl_days),
            force_refresh=bool(args.force_yahoo_refresh),
            sleep_sec=float(args.yahoo_sleep_sec),
            overwrite_existing=overwrite_existing,
        )

    df = apply_default_fields(df, args, overwrite_existing=False)
    df["cik"] = df["cik"].map(normalize_cik)
    df["is_primary_listing"] = df["is_primary_listing"].map(normalize_bool)
    df["investability_status"] = df.apply(derive_investability_status, axis=1)
    for column in OUTPUT_COLUMNS:
        df[column] = df[column].fillna("").astype(str).map(clean_text)
    df = df[output_columns(df, bool(args.preserve_extra_columns))]

    if output_path == input_path and not bool(args.no_backup):
        backup_path = backup_output(output_path, safe_prefix)
        if backup_path is not None:
            LOGGER.info("Backup saved: %s", backup_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False, encoding="utf-8", quoting=csv.QUOTE_MINIMAL)

    audit = build_audit(df, sec_df, identity_df, yahoo_audit)
    audit_output.parent.mkdir(parents=True, exist_ok=True)
    audit.to_csv(audit_output, index=False, encoding="utf-8")

    LOGGER.info("Wrote enriched technology CSV: %s", output_path)
    LOGGER.info("Wrote audit CSV: %s", audit_output)
    LOGGER.info("Population summary: %s", summarize(df))
    missing_any = int(df.apply(lambda row: bool(missing_fields_for_row(row)), axis=1).sum())
    LOGGER.info("Rows with at least one missing field: %d/%d", missing_any, len(df))


if __name__ == "__main__":
    main()

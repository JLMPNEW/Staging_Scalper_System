#!/usr/bin/env python3
from __future__ import annotations

import argparse
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


LOGGER = logging.getLogger("enrich_semiconductor_tickers")

DEFAULT_INPUT = SCRIPT_DIR / "semiconductor_tickers.csv"
DEFAULT_OUTPUT = SCRIPT_DIR / "semiconductor_tickers.csv"
DEFAULT_PANEL = SCRIPT_DIR / "semiconductor_panel.xlsx"
DEFAULT_SEC_CACHE_DIR = SCRIPT_DIR / "_semiconductor_sec_cache"
DEFAULT_IDENTITY_CACHE_DIR = SCRIPT_DIR / "_semiconductor_identity_cache"
DEFAULT_YAHOO_CACHE_DIR = SCRIPT_DIR / "_semiconductor_yahoo_profile_cache"
DEFAULT_AUDIT_OUTPUT = SCRIPT_DIR / "semiconductor_tickers_enrichment_audit.csv"
DEFAULT_USER_AGENT = "JL, Independent Research, jm.357@hotmail.com"

OUTPUT_COLUMNS = [
    "ticker",
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
    "missing_fields",
    "yahoo_status",
    "yahoo_error",
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Populate semiconductor_tickers.csv with SEC, listing, cohort, and Yahoo profile fields."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--panel", type=Path, default=DEFAULT_PANEL)
    parser.add_argument("--audit-output", type=Path, default=DEFAULT_AUDIT_OUTPUT)
    parser.add_argument("--sec-cache-dir", type=Path, default=DEFAULT_SEC_CACHE_DIR)
    parser.add_argument("--identity-cache-dir", type=Path, default=DEFAULT_IDENTITY_CACHE_DIR)
    parser.add_argument("--yahoo-cache-dir", type=Path, default=DEFAULT_YAHOO_CACHE_DIR)
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
    parser.add_argument("--ib-client-id", type=int, default=72)
    parser.add_argument("--ib-timeout-sec", type=float, default=8.0)
    parser.add_argument("--ib-sleep-sec", type=float, default=0.05)
    parser.add_argument("--no-backup", action="store_true")
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


def should_fill(current: Any, overwrite_existing: bool) -> bool:
    return overwrite_existing or blank(current)


def clean_text(raw: Any) -> str:
    return re.sub(r"\s+", " ", str(raw or "").strip())


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


def standardize_input_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy().fillna("")
    norm_to_raw = {normalize_header(col): str(col) for col in df.columns}
    aliases = {
        "ticker": ("ticker", "tickers", "symbol"),
        "company_name": ("companyname", "company", "name", "securityname"),
        "cik": ("cik",),
        "exchange": ("exchange",),
        "sector": ("sector",),
        "industry": ("industry",),
        "subsector": ("subsector", "industryaggregate", "industrygroup", "subindustry"),
        "country": ("country",),
        "currency": ("currency",),
        "security_type": ("securitytype", "securitytype", "security"),
        "listing_status": ("listingstatus", "status"),
        "is_primary_listing": ("isprimarylisting", "primarylisting"),
    }
    rename: dict[str, str] = {}
    for target, candidates in aliases.items():
        if target in df.columns:
            continue
        for candidate in candidates:
            raw = norm_to_raw.get(normalize_header(candidate))
            if raw:
                rename[raw] = target
                break
    if rename:
        df = df.rename(columns=rename)
    if "ticker" not in df.columns:
        raise ValueError("Input CSV must contain a ticker column.")
    for column in OUTPUT_COLUMNS:
        if column not in df.columns:
            df[column] = ""
    df["ticker"] = df["ticker"].map(normalize_ticker)
    df["cik"] = df["cik"].map(normalize_cik)
    df = df[df["ticker"] != ""].drop_duplicates(subset=["ticker"], keep="first").reset_index(drop=True)
    return df[OUTPUT_COLUMNS]


def strip_cohort_prefix(raw: Any) -> str:
    text = clean_text(raw)
    return re.sub(r"^(?:C\d+|EX)\s+", "", text).strip()


def load_panel_map(panel_path: Path) -> dict[str, dict[str, str]]:
    if not panel_path.exists():
        LOGGER.warning("Semiconductor panel not found; sector/industry/subsector will use defaults: %s", panel_path)
        return {}
    df = pd.read_excel(panel_path, sheet_name="Cohort Map", dtype=str, keep_default_na=False).fillna("")
    required = {
        "Input Ticker",
        "Canonical Ticker",
        "Company",
        "Calibration Cohort",
        "Sub-cohort / Role",
        "Calibration Use",
        "Liquidity/Instrument Flag",
    }
    missing = sorted(required.difference(df.columns))
    if missing:
        raise ValueError(f"{panel_path} Cohort Map is missing required columns: {missing}")

    priority = {"Core": 0, "Secondary": 1, "Exclude_duplicate": 2}
    records: dict[str, tuple[int, dict[str, str]]] = {}
    for raw in df.to_dict("records"):
        input_ticker = normalize_ticker(raw.get("Input Ticker"))
        canonical = normalize_ticker(raw.get("Canonical Ticker"))
        if not input_ticker:
            continue
        calibration_use = clean_text(raw.get("Calibration Use"))
        rank = priority.get(calibration_use, 5)
        record = {
            "canonical_ticker": canonical,
            "company_name": clean_text(raw.get("Company")),
            "sector": "Technology",
            "industry": strip_cohort_prefix(raw.get("Calibration Cohort")) or "Semiconductors",
            "subsector": clean_text(raw.get("Sub-cohort / Role")),
            "calibration_use": calibration_use,
            "liquidity_flag": clean_text(raw.get("Liquidity/Instrument Flag")),
        }
        existing = records.get(input_ticker)
        if existing is None or rank < existing[0]:
            records[input_ticker] = (rank, record)
    return {ticker: record for ticker, (_rank, record) in records.items()}


def apply_panel_fields(df: pd.DataFrame, panel_map: dict[str, dict[str, str]], overwrite_existing: bool) -> pd.DataFrame:
    df = df.copy()
    for idx, row in df.iterrows():
        info = panel_map.get(normalize_ticker(row.get("ticker")))
        if not info:
            if should_fill(row.get("sector"), overwrite_existing):
                df.at[idx, "sector"] = "Technology"
            if should_fill(row.get("industry"), overwrite_existing):
                df.at[idx, "industry"] = "Semiconductors"
            continue
        for column in ("company_name", "sector", "industry", "subsector"):
            value = info.get(column, "")
            if value and should_fill(row.get(column), overwrite_existing):
                df.at[idx, column] = value
        flag = info.get("liquidity_flag", "").lower()
        calibration_use = info.get("calibration_use", "").lower()
        if "primary/liquid" in flag and should_fill(row.get("is_primary_listing"), overwrite_existing):
            df.at[idx, "is_primary_listing"] = "True"
        elif (
            "duplicate" in flag
            or "otc foreign ordinary" in flag
            or "otc/foreign ordinary" in flag
            or "otc adr" in flag
            or "derivative" in flag
            or calibration_use.startswith("exclude")
        ) and should_fill(row.get("is_primary_listing"), overwrite_existing):
            df.at[idx, "is_primary_listing"] = "False"
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
    out = pd.DataFrame(
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
    return out


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


def infer_security_type_from_yahoo(ticker: str, info: dict[str, Any], panel_info: dict[str, str] | None) -> str:
    quote_type = first_profile_value(info, ("quoteType",)).upper()
    if quote_type in {"ETF", "MUTUALFUND"}:
        return "ETF"
    if quote_type not in {"EQUITY", ""}:
        return quote_type.title()

    name_blob = " ".join(
        [
            first_profile_value(info, ("longName", "shortName", "displayName")),
            (panel_info or {}).get("liquidity_flag", ""),
        ]
    ).upper()
    ticker_norm = normalize_ticker(ticker)
    country = first_profile_value(info, ("country",))
    if "ADR" in name_blob or "ADS" in name_blob or (ticker_norm.endswith("Y") and country and country != "United States"):
        return "ADR/ADS"
    if ticker_norm.endswith("F") and country and country != "United States":
        return "Ordinary Shares"
    if quote_type == "EQUITY":
        return "Common Stock"
    return ""


def infer_listing_status_from_yahoo(info: dict[str, Any], panel_info: dict[str, str] | None) -> str:
    calibration_use = (panel_info or {}).get("calibration_use", "").lower()
    flag = (panel_info or {}).get("liquidity_flag", "").lower()
    if "chapter 7" in flag or "expert market" in flag or "near-zero" in flag:
        return "inactive_or_not_investable"
    if calibration_use == "exclude_invalid":
        return "invalid_or_inactive"
    quote_type = first_profile_value(info, ("quoteType",))
    exchange = first_profile_value(info, ("exchange", "fullExchangeName"))
    price = info.get("regularMarketPrice", None)
    if quote_type.upper() == "EQUITY" and (exchange or price not in (None, "")):
        return "active"
    return ""


def infer_country_from_context(company_name: str, panel_info: dict[str, str] | None) -> str:
    text = " ".join(
        [
            clean_text(company_name),
            (panel_info or {}).get("company_name", ""),
            (panel_info or {}).get("liquidity_flag", ""),
            (panel_info or {}).get("subsector", ""),
        ]
    ).upper()
    keyword_map = [
        ("SHANGHAI", "China"),
        ("HUA HONG", "China"),
        ("CHINA ADR", "China"),
        ("FUDAN", "China"),
        ("TAIWAN", "Taiwan"),
        ("TOKYO", "Japan"),
        ("RENESAS", "Japan"),
        ("ROHM", "Japan"),
        ("SUMCO", "Japan"),
        ("NORDIC SEMICONDUCTOR", "Norway"),
        ("AIXTRON", "Germany"),
        ("SOITEC", "France"),
        ("SUSS MICROTEC", "Germany"),
        ("OXFORD INSTRUMENTS", "United Kingdom"),
    ]
    for keyword, country in keyword_map:
        if keyword in text:
            return country
    return ""


def apply_yahoo_fields(
    df: pd.DataFrame,
    *,
    panel_map: dict[str, dict[str, str]],
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
        panel_info = panel_map.get(ticker)
        field_values = {
            "company_name": first_profile_value(info, ("longName", "shortName", "displayName", "name")),
            "sector": first_profile_value(info, ("sector",)),
            "industry": first_profile_value(info, ("industry", "industryDisp", "industryKey")),
            "country": first_profile_value(info, ("country",)),
            "currency": first_profile_value(info, ("currency", "financialCurrency")),
            "exchange": first_profile_value(info, ("fullExchangeName", "exchange")),
            "security_type": infer_security_type_from_yahoo(ticker, info, panel_info),
            "listing_status": infer_listing_status_from_yahoo(info, panel_info),
        }
        if not field_values["country"]:
            field_values["country"] = infer_country_from_context(
                field_values["company_name"] or str(row.get("company_name", "")),
                panel_info,
            )
        for column, value in field_values.items():
            if value and should_fill(row.get(column), overwrite_existing):
                df.at[idx, column] = value
        audit_rows.append(
            {
                "ticker": ticker,
                "yahoo_status": status,
                "yahoo_error": error[:300],
            }
        )
        LOGGER.info("[%d/%d] Yahoo %s status=%s", idx + 1, total, ticker, status)
        if idx + 1 < total and sleep_sec > 0:
            time.sleep(float(sleep_sec))
    return df, pd.DataFrame(audit_rows).fillna("")


def reapply_panel_primary_flags(
    df: pd.DataFrame,
    panel_map: dict[str, dict[str, str]],
    overwrite_existing: bool,
) -> pd.DataFrame:
    df = df.copy()
    for idx, row in df.iterrows():
        info = panel_map.get(normalize_ticker(row.get("ticker")))
        if not info:
            continue
        flag = info.get("liquidity_flag", "").lower()
        calibration_use = info.get("calibration_use", "").lower()
        if "primary/liquid" in flag:
            if overwrite_existing or blank(row.get("is_primary_listing")):
                df.at[idx, "is_primary_listing"] = "True"
        elif (
            "duplicate" in flag
            or "otc foreign ordinary" in flag
            or "otc/foreign ordinary" in flag
            or "otc adr" in flag
            or "derivative" in flag
            or calibration_use.startswith("exclude")
        ):
            if overwrite_existing or blank(row.get("is_primary_listing")) or str(row.get("is_primary_listing")) == "True":
                df.at[idx, "is_primary_listing"] = "False"
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
            columns={
                "Ticker": "ticker",
                "MatchType": "sec_match_type",
                "Source": "sec_source",
            }
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
        audit = audit.merge(identity_small[["ticker", "identity_sources", "identity_missing_fields"]], on="ticker", how="left")
    if not yahoo_audit.empty:
        audit = audit.merge(yahoo_audit, on="ticker", how="left")
    audit["missing_fields"] = df.apply(missing_fields_for_row, axis=1)
    for column in AUDIT_COLUMNS:
        if column not in audit.columns:
            audit[column] = ""
    return audit[AUDIT_COLUMNS].fillna("")


def backup_output(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = path.with_name(f"{path.stem}.pre_semiconductor_enrichment_{stamp}{path.suffix}")
    shutil.copy2(path, backup_path)
    return backup_path


def summarize(df: pd.DataFrame) -> str:
    parts = []
    for column in OUTPUT_COLUMNS:
        populated = int(df[column].astype(str).str.strip().ne("").sum())
        parts.append(f"{column}={populated}/{len(df)}")
    return " ".join(parts)


def main() -> None:
    configure_logging()
    args = parse_args()
    input_path = args.input.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    panel_path = args.panel.expanduser().resolve()
    audit_output = args.audit_output.expanduser().resolve()
    overwrite_existing = bool(args.overwrite_existing)

    if not input_path.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_path}")

    df = standardize_input_columns(read_csv_flexible(input_path))
    LOGGER.info("Loaded %d semiconductor tickers from %s", len(df), input_path)

    panel_map = load_panel_map(panel_path)
    df = apply_panel_fields(df, panel_map, overwrite_existing)

    sec_df = pd.DataFrame()
    if not bool(args.skip_sec):
        df, sec_df = apply_sec_fields(
            df,
            cache_dir=args.sec_cache_dir.expanduser().resolve(),
            timeout_sec=float(args.http_timeout_sec),
            user_agent=str(args.user_agent),
            overwrite_existing=overwrite_existing,
        )
        LOGGER.info("SEC CIK populated: %d/%d", int(df["cik"].astype(str).str.strip().ne("").sum()), len(df))

    identity_df = pd.DataFrame()
    if not bool(args.skip_nasdaq) or not bool(args.disable_ib):
        df, identity_df = apply_identity_fields(
            df,
            cache_dir=args.identity_cache_dir.expanduser().resolve(),
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
            panel_map=panel_map,
            cache_dir=args.yahoo_cache_dir.expanduser().resolve(),
            ttl_days=float(args.yahoo_ttl_days),
            force_refresh=bool(args.force_yahoo_refresh),
            sleep_sec=float(args.yahoo_sleep_sec),
            overwrite_existing=overwrite_existing,
        )

    df = reapply_panel_primary_flags(df, panel_map, overwrite_existing=True)
    df["cik"] = df["cik"].map(normalize_cik)
    for column in OUTPUT_COLUMNS:
        df[column] = df[column].fillna("").astype(str).map(clean_text)
    df = df[OUTPUT_COLUMNS]

    if output_path == input_path and not bool(args.no_backup):
        backup_path = backup_output(output_path)
        if backup_path is not None:
            LOGGER.info("Backup saved: %s", backup_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False, encoding="utf-8")

    audit = build_audit(df, sec_df, identity_df, yahoo_audit)
    audit_output.parent.mkdir(parents=True, exist_ok=True)
    audit.to_csv(audit_output, index=False, encoding="utf-8")

    LOGGER.info("Wrote enriched semiconductor CSV: %s", output_path)
    LOGGER.info("Wrote audit CSV: %s", audit_output)
    LOGGER.info("Population summary: %s", summarize(df))
    missing_any = int(df.apply(lambda row: bool(missing_fields_for_row(row)), axis=1).sum())
    LOGGER.info("Rows with at least one missing field: %d/%d", missing_any, len(df))


if __name__ == "__main__":
    main()

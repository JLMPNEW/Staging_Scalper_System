#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import requests

try:
    import yaml
except ImportError:  # pragma: no cover - handled at runtime when config is used.
    yaml = None  # type: ignore[assignment]


LOGGER = logging.getLogger("fetch_fmp_reverse_splits")
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR.parent / "biotech_index" / "config.yaml"
DEFAULT_INPUT_CSV = SCRIPT_DIR / "second_check_reverse_plit.csv"
DEFAULT_OUTPUT_CSV = SCRIPT_DIR / "reverse_split_events_fmp.csv"
DEFAULT_AUDIT_OUTPUT_CSV = SCRIPT_DIR / "reverse_split_second_check_results.csv"
DEFAULT_CACHE_DIR = SCRIPT_DIR / "_fmp_reverse_split_cache"

FMP_SPLITS_URL = "https://financialmodelingprep.com/stable/splits"

EVENT_COLUMNS = [
    "Ticker",
    "CompanyName",
    "EffectiveDate",
    "Action",
    "Numerator",
    "Denominator",
    "SplitRatio",
    "Source",
    "FetchedAt",
]

AUDIT_COLUMNS = [
    "Ticker",
    "CompanyName",
    "CheckedAt",
    "FmpStatus",
    "ReverseSplitConfirmed",
    "ReverseSplitCount",
    "LatestReverseSplitDate",
    "LatestReverseSplitRatio",
    "SplitCount",
    "CacheStatus",
    "FetchError",
]


class FatalFmpError(RuntimeError):
    """Raised when the FMP account/key cannot access the endpoint at all."""


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def load_yaml_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if yaml is None:
        raise RuntimeError("PyYAML is required to read config files. Install pyyaml or pass CLI arguments directly.")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def cfg_get(config: dict[str, Any], dotted_key: str, default: Any = None) -> Any:
    cur: Any = config
    for part in dotted_key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def resolve_config_path(raw: Any, *, base_dir: Path, default: Path) -> Path:
    value = str(raw or "").strip()
    path = Path(value) if value else default
    if not path.is_absolute():
        path = base_dir / path
    return path.expanduser().resolve()


def parse_args() -> argparse.Namespace:
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    pre_args, _ = pre_parser.parse_known_args()

    config_path = pre_args.config.expanduser().resolve()
    config = load_yaml_config(config_path)
    config_base_dir = config_path.parent

    parser = argparse.ArgumentParser(
        parents=[pre_parser],
        description=(
            "Second-check reverse splits through Financial Modeling Prep for a limited "
            "CSV of tickers that need confirmation."
        ),
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=resolve_config_path(
            cfg_get(config, "fmp_reverse_split.input_csv", None),
            base_dir=config_base_dir,
            default=DEFAULT_INPUT_CSV,
        ),
        help="CSV containing only tickers that need reverse-split confirmation.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=resolve_config_path(
            cfg_get(config, "fmp_reverse_split.output_csv", None),
            base_dir=config_base_dir,
            default=DEFAULT_OUTPUT_CSV,
        ),
        help="Screener-compatible CSV of confirmed reverse split events.",
    )
    parser.add_argument(
        "--audit-output",
        type=Path,
        default=resolve_config_path(
            cfg_get(config, "fmp_reverse_split.audit_output_csv", None),
            base_dir=config_base_dir,
            default=DEFAULT_AUDIT_OUTPUT_CSV,
        ),
        help="Audit CSV with one row per checked ticker.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=resolve_config_path(
            cfg_get(config, "fmp_reverse_split.cache_dir", None),
            base_dir=config_base_dir,
            default=DEFAULT_CACHE_DIR,
        ),
    )
    parser.add_argument("--api-key-env", default=str(cfg_get(config, "fmp_reverse_split.api_key_env", "FMP_API_KEY")))
    parser.add_argument(
        "--api-key",
        default=str(cfg_get(config, "fmp_reverse_split.api_key", "") or ""),
        help="Optional FMP API key override. Prefer the configured environment variable.",
    )
    parser.add_argument("--ttl-days", type=float, default=float(cfg_get(config, "fmp_reverse_split.ttl_days", 30.0)))
    parser.add_argument("--sleep-sec", type=float, default=float(cfg_get(config, "fmp_reverse_split.sleep_sec", 0.25)))
    parser.add_argument("--timeout-sec", type=float, default=float(cfg_get(config, "fmp_reverse_split.timeout_sec", 30.0)))
    parser.add_argument("--max-tickers", type=int, default=int(cfg_get(config, "fmp_reverse_split.max_tickers", 0)))
    parser.add_argument("--force-refresh", action="store_true", default=bool(cfg_get(config, "fmp_reverse_split.force_refresh", False)))
    return parser.parse_args()


def normalize_header(raw: Any) -> str:
    return "".join(ch.lower() for ch in str(raw or "") if ch.isalnum())


def normalize_ticker(raw: Any) -> str:
    return str(raw or "").strip().upper().replace(".", "-")


def read_csv_flexible(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Input CSV not found: {path}")
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


def find_column(df: pd.DataFrame, candidates: tuple[str, ...]) -> Optional[str]:
    norm_to_raw = {normalize_header(col): str(col) for col in df.columns}
    for candidate in candidates:
        found = norm_to_raw.get(normalize_header(candidate))
        if found:
            return found
    return None


def load_tickers(path: Path, max_tickers: int = 0) -> list[dict[str, str]]:
    df = read_csv_flexible(path)
    if df.empty:
        LOGGER.warning("Input CSV contains no ticker rows: %s", path)
        return []
    ticker_col = find_column(df, ("Ticker", "Tickers", "Symbol", "Issue Symbol", "Current Symbol"))
    if ticker_col is None:
        raise ValueError("Input CSV must contain a ticker column such as Ticker or Symbol.")
    company_col = find_column(df, ("CompanyName", "Company Name", "Company", "Name", "Security Name"))

    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_rec in df.to_dict("records"):
        rec = {str(k): str(v or "").strip() for k, v in raw_rec.items()}
        ticker = normalize_ticker(rec.get(ticker_col, ""))
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        rows.append(
            {
                "Ticker": ticker,
                "CompanyName": rec.get(company_col, "") if company_col else "",
            }
        )
        if max_tickers > 0 and len(rows) >= max_tickers:
            break
    return rows


def cache_name(ticker: str) -> str:
    safe = re.sub(r"[^A-Z0-9._-]+", "_", normalize_ticker(ticker))
    return f"{safe}.json"


def cache_is_fresh(path: Path, ttl_days: float) -> bool:
    if ttl_days <= 0 or not path.exists():
        return False
    age_sec = time.time() - path.stat().st_mtime
    return age_sec <= ttl_days * 86400.0


def detect_fmp_error(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("Error Message", "error", "Error"):
        value = payload.get(key)
        if value:
            return str(value)
    message = str(payload.get("message") or payload.get("Message") or "").strip()
    if message and re.search(r"\b(error|invalid|apikey|api key|limit|upgrade|plan)\b", message, re.IGNORECASE):
        return message
    return ""


def fetch_splits(
    *,
    session: requests.Session,
    ticker: str,
    api_key: str,
    cache_dir: Path,
    ttl_days: float,
    timeout_sec: float,
    force_refresh: bool,
) -> tuple[Any, str]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / cache_name(ticker)
    if not force_refresh and cache_is_fresh(path, ttl_days):
        envelope = json.loads(path.read_text(encoding="utf-8"))
        return envelope.get("payload", envelope), "cache"

    if not api_key:
        raise RuntimeError("FMP API key is missing. Set FMP_API_KEY or configure fmp_reverse_split.api_key.")

    params = {"symbol": ticker, "apikey": api_key}
    resp = session.get(FMP_SPLITS_URL, params=params, timeout=timeout_sec)
    if resp.status_code in {402, 403}:
        details = resp.text.strip().replace(api_key, "***")
        raise FatalFmpError(f"FMP endpoint is not accessible with this key; HTTP {resp.status_code}: {details[:240]}")
    resp.raise_for_status()
    payload = json.loads(resp.text)
    fmp_error = detect_fmp_error(payload)
    if fmp_error:
        raise RuntimeError(fmp_error)

    envelope = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "url": FMP_SPLITS_URL,
        "params": {"symbol": ticker},
        "payload": payload,
    }
    path.write_text(json.dumps(envelope, ensure_ascii=True, sort_keys=True), encoding="utf-8")
    return payload, "network"


def rec_get_any(rec: dict[str, Any], *keys: str) -> Any:
    lower_to_key = {str(k).lower(): k for k in rec}
    norm_to_key = {normalize_header(k): k for k in rec}
    for key in keys:
        if key in rec:
            return rec[key]
        lower_key = lower_to_key.get(key.lower())
        if lower_key is not None:
            return rec[lower_key]
        norm_key = norm_to_key.get(normalize_header(key))
        if norm_key is not None:
            return rec[norm_key]
    return ""


def extract_split_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("historical", "data", "splits", "stockSplits"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        if any(normalize_header(key) in {"date", "splitdate", "numerator", "denominator"} for key in payload):
            return [payload]
    return []


def parse_float(raw: Any) -> Optional[float]:
    text = str(raw or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_ratio_text(raw: Any) -> tuple[Optional[float], Optional[float]]:
    text = str(raw or "").strip()
    if not text:
        return None, None
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:[:/]|-for-|for)\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if not match:
        return None, None
    return parse_float(match.group(1)), parse_float(match.group(2))


def parse_split_date(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y%m%d"):
        try:
            return datetime.strptime(text[:10] if fmt == "%Y-%m-%d" else text, fmt).date().isoformat()
        except ValueError:
            continue
    return text[:10]


def normalize_split_record(rec: dict[str, Any]) -> dict[str, Any]:
    split_ratio = rec_get_any(rec, "splitRatio", "split ratio", "ratio", "label")
    numerator = parse_float(
        rec_get_any(rec, "numerator", "splitNumerator", "split numerator", "fromFactor", "from")
    )
    denominator = parse_float(
        rec_get_any(rec, "denominator", "splitDenominator", "split denominator", "toFactor", "to")
    )
    if numerator is None or denominator is None:
        ratio_num, ratio_den = parse_ratio_text(split_ratio)
        numerator = numerator if numerator is not None else ratio_num
        denominator = denominator if denominator is not None else ratio_den
    event_date = parse_split_date(
        rec_get_any(rec, "date", "splitDate", "split date", "effectiveDate", "effective date")
    )
    return {
        "date": event_date,
        "numerator": numerator,
        "denominator": denominator,
        "split_ratio": str(split_ratio or "").strip(),
    }


def format_number(raw: Optional[float]) -> str:
    if raw is None:
        return ""
    if float(raw).is_integer():
        return str(int(raw))
    return f"{raw:g}"


def build_rows_for_ticker(
    *,
    ticker: str,
    company_name: str,
    payload: Any,
    cache_status: str,
    checked_at: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    split_records = extract_split_records(payload)
    normalized = [normalize_split_record(rec) for rec in split_records]
    reverse_splits = [
        row
        for row in normalized
        if row["date"] and row["numerator"] is not None and row["denominator"] is not None and row["numerator"] < row["denominator"]
    ]
    reverse_splits.sort(key=lambda row: str(row["date"]), reverse=True)

    event_rows: list[dict[str, Any]] = []
    for row in reverse_splits:
        numerator = row["numerator"]
        denominator = row["denominator"]
        ratio = row["split_ratio"] or f"{format_number(numerator)}:{format_number(denominator)}"
        event_rows.append(
            {
                "Ticker": ticker,
                "CompanyName": company_name,
                "EffectiveDate": row["date"],
                "Action": "reverse split",
                "Numerator": format_number(numerator),
                "Denominator": format_number(denominator),
                "SplitRatio": ratio,
                "Source": "FMP",
                "FetchedAt": checked_at,
            }
        )

    if reverse_splits:
        status = "reverse_split_confirmed"
    elif split_records:
        status = "no_reverse_split"
    else:
        status = "no_splits"

    latest = reverse_splits[0] if reverse_splits else {}
    audit_row = {
        "Ticker": ticker,
        "CompanyName": company_name,
        "CheckedAt": checked_at,
        "FmpStatus": status,
        "ReverseSplitConfirmed": bool(reverse_splits),
        "ReverseSplitCount": len(reverse_splits),
        "LatestReverseSplitDate": latest.get("date", ""),
        "LatestReverseSplitRatio": latest.get("split_ratio", "")
        or (
            f"{format_number(latest.get('numerator'))}:{format_number(latest.get('denominator'))}"
            if latest
            else ""
        ),
        "SplitCount": len(split_records),
        "CacheStatus": cache_status,
        "FetchError": "",
    }
    return event_rows, audit_row


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=columns).to_csv(path, index=False)


def main() -> int:
    configure_logging()
    args = parse_args()

    api_key = str(args.api_key or os.getenv(args.api_key_env, "") or "").strip()
    if not api_key:
        raise SystemExit(
            f"FMP API key is missing. Set the {args.api_key_env} environment variable, "
            "or pass --api-key for a one-off run."
        )

    checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    ticker_rows = load_tickers(args.input, args.max_tickers)

    LOGGER.info("Loaded %d ticker(s) for FMP reverse-split second check from %s", len(ticker_rows), args.input)
    if not ticker_rows:
        write_csv(args.output, [], EVENT_COLUMNS)
        write_csv(args.audit_output, [], AUDIT_COLUMNS)
        LOGGER.warning("No tickers to check; wrote empty outputs.")
        return 0

    event_rows: list[dict[str, Any]] = []
    audit_rows: list[dict[str, Any]] = []
    fetch_errors = 0

    session = requests.Session()
    session.headers.update({"User-Agent": "JL, Independent Research, jm.357@hotmail.com"})

    for idx, row in enumerate(ticker_rows, start=1):
        ticker = row["Ticker"]
        company_name = row.get("CompanyName", "")
        try:
            payload, cache_status = fetch_splits(
                session=session,
                ticker=ticker,
                api_key=api_key,
                cache_dir=args.cache_dir,
                ttl_days=args.ttl_days,
                timeout_sec=args.timeout_sec,
                force_refresh=args.force_refresh,
            )
            ticker_events, audit_row = build_rows_for_ticker(
                ticker=ticker,
                company_name=company_name,
                payload=payload,
                cache_status=cache_status,
                checked_at=checked_at,
            )
            event_rows.extend(ticker_events)
            audit_rows.append(audit_row)
        except FatalFmpError as exc:
            fetch_errors += 1
            audit_rows.append(
                {
                    "Ticker": ticker,
                    "CompanyName": company_name,
                    "CheckedAt": checked_at,
                    "FmpStatus": "fatal_fmp_error",
                    "ReverseSplitConfirmed": False,
                    "ReverseSplitCount": 0,
                    "LatestReverseSplitDate": "",
                    "LatestReverseSplitRatio": "",
                    "SplitCount": 0,
                    "CacheStatus": "error",
                    "FetchError": str(exc),
                }
            )
            LOGGER.error("%s", exc)
            LOGGER.error("Stopping FMP run early because the endpoint is not accessible with the configured key.")
            break
        except Exception as exc:
            fetch_errors += 1
            audit_rows.append(
                {
                    "Ticker": ticker,
                    "CompanyName": company_name,
                    "CheckedAt": checked_at,
                    "FmpStatus": "fetch_error",
                    "ReverseSplitConfirmed": False,
                    "ReverseSplitCount": 0,
                    "LatestReverseSplitDate": "",
                    "LatestReverseSplitRatio": "",
                    "SplitCount": 0,
                    "CacheStatus": "error",
                    "FetchError": f"{type(exc).__name__}: {exc}",
                }
            )
            LOGGER.warning("FMP reverse-split check failed for %s: %s", ticker, exc)

        if idx < len(ticker_rows) and args.sleep_sec > 0:
            time.sleep(args.sleep_sec)

    write_csv(args.output, event_rows, EVENT_COLUMNS)
    write_csv(args.audit_output, audit_rows, AUDIT_COLUMNS)

    confirmed_count = len({row["Ticker"] for row in event_rows})
    LOGGER.info("Wrote %d confirmed reverse-split event(s) for %d ticker(s): %s", len(event_rows), confirmed_count, args.output)
    LOGGER.info("Wrote audit results for %d ticker(s): %s", len(audit_rows), args.audit_output)
    if fetch_errors:
        LOGGER.warning("FMP fetch errors: %d", fetch_errors)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

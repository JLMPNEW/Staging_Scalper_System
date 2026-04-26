#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import re
import time
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Optional

import pandas as pd

try:
    import yaml
except ImportError:  # pragma: no cover - handled at runtime when config is used.
    yaml = None  # type: ignore[assignment]

try:
    import yfinance as yf
except ImportError as exc:  # pragma: no cover - handled by main().
    yf = None  # type: ignore[assignment]
    YFINANCE_IMPORT_ERROR: Exception | None = exc
else:
    YFINANCE_IMPORT_ERROR = None


LOGGER = logging.getLogger("fetch_yahoo_reverse_splits")
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR.parent / "biotech_index" / "config.yaml"
DEFAULT_INPUT_CSV = SCRIPT_DIR / "second_check_reverse_plit.csv"
DEFAULT_OUTPUT_CSV = SCRIPT_DIR / "reverse_split_events_yahoo.csv"
DEFAULT_AUDIT_OUTPUT_CSV = SCRIPT_DIR / "reverse_split_second_check_results_yahoo.csv"
DEFAULT_CACHE_DIR = SCRIPT_DIR / "_yahoo_reverse_split_cache"

EVENT_COLUMNS = [
    "Ticker",
    "CompanyName",
    "EffectiveDate",
    "Action",
    "Numerator",
    "Denominator",
    "SplitRatio",
    "SplitFactor",
    "Source",
    "FetchedAt",
]

AUDIT_COLUMNS = [
    "Ticker",
    "YahooSymbol",
    "CompanyName",
    "CheckedAt",
    "YahooStatus",
    "ReverseSplitConfirmed",
    "ReverseSplitCount",
    "LatestReverseSplitDate",
    "LatestReverseSplitRatio",
    "SplitCount",
    "CacheStatus",
    "FetchError",
]


class FatalYahooError(RuntimeError):
    """Raised when Yahoo/yfinance is unavailable for the run, not a single ticker."""


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logging.getLogger("yfinance").setLevel(logging.WARNING)


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
            "Second-check reverse splits through Yahoo Finance/yfinance for a limited "
            "CSV of tickers that need confirmation."
        ),
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=resolve_config_path(
            cfg_get(config, "yahoo_reverse_split.input_csv", None),
            base_dir=config_base_dir,
            default=DEFAULT_INPUT_CSV,
        ),
        help="CSV containing only tickers that need reverse-split confirmation.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=resolve_config_path(
            cfg_get(config, "yahoo_reverse_split.output_csv", None),
            base_dir=config_base_dir,
            default=DEFAULT_OUTPUT_CSV,
        ),
        help="Screener-compatible CSV of confirmed reverse split events.",
    )
    parser.add_argument(
        "--audit-output",
        type=Path,
        default=resolve_config_path(
            cfg_get(config, "yahoo_reverse_split.audit_output_csv", None),
            base_dir=config_base_dir,
            default=DEFAULT_AUDIT_OUTPUT_CSV,
        ),
        help="Audit CSV with one row per checked ticker.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=resolve_config_path(
            cfg_get(config, "yahoo_reverse_split.cache_dir", None),
            base_dir=config_base_dir,
            default=DEFAULT_CACHE_DIR,
        ),
    )
    parser.add_argument("--ttl-days", type=float, default=float(cfg_get(config, "yahoo_reverse_split.ttl_days", 30.0)))
    parser.add_argument("--sleep-sec", type=float, default=float(cfg_get(config, "yahoo_reverse_split.sleep_sec", 2.0)))
    parser.add_argument("--max-tickers", type=int, default=int(cfg_get(config, "yahoo_reverse_split.max_tickers", 0)))
    parser.add_argument("--force-refresh", action="store_true", default=bool(cfg_get(config, "yahoo_reverse_split.force_refresh", False)))
    parser.add_argument(
        "--use-stale-cache-on-error",
        action=argparse.BooleanOptionalAction,
        default=bool(cfg_get(config, "yahoo_reverse_split.use_stale_cache_on_error", True)),
        help="Use stale per-ticker cache if Yahoo is rate-limited or temporarily unavailable.",
    )
    return parser.parse_args()


def normalize_header(raw: Any) -> str:
    return "".join(ch.lower() for ch in str(raw or "") if ch.isalnum())


def normalize_ticker(raw: Any) -> str:
    return str(raw or "").strip().upper().replace(".", "-")


def yahoo_symbol(raw: Any) -> str:
    # Yahoo uses '-' for US class-share tickers such as BRK-B.
    return normalize_ticker(raw)


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
                "YahooSymbol": yahoo_symbol(ticker),
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


def read_cache(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def normalize_date(raw: Any) -> str:
    if raw is None:
        return ""
    try:
        ts = pd.Timestamp(raw)
        if pd.isna(ts):
            return ""
        return ts.date().isoformat()
    except Exception:
        text = str(raw or "").strip()
        return text[:10] if text else ""


def parse_float(raw: Any) -> Optional[float]:
    text = str(raw or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def records_from_series(series: Any) -> list[dict[str, Any]]:
    if series is None or not hasattr(series, "items"):
        return []
    rows: list[dict[str, Any]] = []
    for idx, value in series.items():
        factor = parse_float(value)
        if factor is None or factor <= 0:
            continue
        rows.append({"date": normalize_date(idx), "factor": factor})
    rows.sort(key=lambda row: str(row["date"]))
    return rows


def records_from_history_frame(frame: Any) -> list[dict[str, Any]]:
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty or "Stock Splits" not in frame.columns:
        return []
    split_series = frame["Stock Splits"]
    split_series = split_series[split_series.fillna(0).astype(float) > 0]
    return records_from_series(split_series)


def fetch_yahoo_splits(symbol: str) -> list[dict[str, Any]]:
    if yf is None:
        raise FatalYahooError(f"yfinance is not installed: {YFINANCE_IMPORT_ERROR}")
    ticker_obj = yf.Ticker(symbol)

    last_error: Exception | None = None
    try:
        rows = records_from_series(ticker_obj.splits)
        if rows:
            return rows
    except Exception as exc:
        last_error = exc

    try:
        frame = ticker_obj.history(period="max", actions=True, auto_adjust=False)
        rows = records_from_history_frame(frame)
        if rows:
            return rows
        if last_error is not None:
            raise last_error
        return []
    except Exception as exc:
        if last_error is not None:
            raise last_error
        raise exc


def load_or_fetch_splits(
    *,
    ticker: str,
    symbol: str,
    cache_dir: Path,
    ttl_days: float,
    force_refresh: bool,
    use_stale_cache_on_error: bool,
) -> tuple[list[dict[str, Any]], str]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / cache_name(ticker)
    cached = read_cache(path)
    if cached and not force_refresh and cache_is_fresh(path, ttl_days):
        rows = cached.get("splits", [])
        return rows if isinstance(rows, list) else [], "cache"

    try:
        rows = fetch_yahoo_splits(symbol)
    except Exception:
        if cached and use_stale_cache_on_error:
            rows = cached.get("splits", [])
            if isinstance(rows, list):
                return rows, "stale_cache"
        raise

    envelope = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "ticker": ticker,
        "yahoo_symbol": symbol,
        "splits": rows,
    }
    path.write_text(json.dumps(envelope, ensure_ascii=True, sort_keys=True), encoding="utf-8")
    return rows, "network"


def split_factor_to_ratio(factor: float) -> tuple[str, str, str]:
    frac = Fraction(float(factor)).limit_denominator(1000)
    numerator = frac.numerator
    denominator = frac.denominator
    return str(numerator), str(denominator), f"{numerator}:{denominator}"


def build_rows_for_ticker(
    *,
    ticker: str,
    symbol: str,
    company_name: str,
    split_rows: list[dict[str, Any]],
    cache_status: str,
    checked_at: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in split_rows:
        event_date = normalize_date(row.get("date"))
        factor = parse_float(row.get("factor"))
        if not event_date or factor is None or factor <= 0:
            continue
        normalized.append({"date": event_date, "factor": factor})

    reverse_splits = [row for row in normalized if float(row["factor"]) < 1.0]
    reverse_splits.sort(key=lambda row: str(row["date"]), reverse=True)

    event_rows: list[dict[str, Any]] = []
    for row in reverse_splits:
        factor = float(row["factor"])
        numerator, denominator, ratio = split_factor_to_ratio(factor)
        event_rows.append(
            {
                "Ticker": ticker,
                "CompanyName": company_name,
                "EffectiveDate": row["date"],
                "Action": "reverse split",
                "Numerator": numerator,
                "Denominator": denominator,
                "SplitRatio": ratio,
                "SplitFactor": f"{factor:g}",
                "Source": "YahooFinance:yfinance",
                "FetchedAt": checked_at,
            }
        )

    if reverse_splits:
        status = "reverse_split_confirmed"
    elif normalized:
        status = "no_reverse_split"
    else:
        status = "no_splits"

    latest = reverse_splits[0] if reverse_splits else {}
    latest_ratio = ""
    if latest:
        _, _, latest_ratio = split_factor_to_ratio(float(latest["factor"]))
    audit_row = {
        "Ticker": ticker,
        "YahooSymbol": symbol,
        "CompanyName": company_name,
        "CheckedAt": checked_at,
        "YahooStatus": status,
        "ReverseSplitConfirmed": bool(reverse_splits),
        "ReverseSplitCount": len(reverse_splits),
        "LatestReverseSplitDate": latest.get("date", ""),
        "LatestReverseSplitRatio": latest_ratio,
        "SplitCount": len(normalized),
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
    if yf is None:
        raise SystemExit(f"yfinance is not installed: {YFINANCE_IMPORT_ERROR}")

    checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    ticker_rows = load_tickers(args.input, args.max_tickers)

    LOGGER.info("Loaded %d ticker(s) for Yahoo reverse-split second check from %s", len(ticker_rows), args.input)
    if not ticker_rows:
        write_csv(args.output, [], EVENT_COLUMNS)
        write_csv(args.audit_output, [], AUDIT_COLUMNS)
        LOGGER.warning("No tickers to check; wrote empty outputs.")
        return 0

    event_rows: list[dict[str, Any]] = []
    audit_rows: list[dict[str, Any]] = []
    fetch_errors = 0

    for idx, row in enumerate(ticker_rows, start=1):
        ticker = row["Ticker"]
        symbol = row["YahooSymbol"]
        company_name = row.get("CompanyName", "")
        try:
            split_rows, cache_status = load_or_fetch_splits(
                ticker=ticker,
                symbol=symbol,
                cache_dir=args.cache_dir,
                ttl_days=args.ttl_days,
                force_refresh=args.force_refresh,
                use_stale_cache_on_error=args.use_stale_cache_on_error,
            )
            ticker_events, audit_row = build_rows_for_ticker(
                ticker=ticker,
                symbol=symbol,
                company_name=company_name,
                split_rows=split_rows,
                cache_status=cache_status,
                checked_at=checked_at,
            )
            event_rows.extend(ticker_events)
            audit_rows.append(audit_row)
        except Exception as exc:
            fetch_errors += 1
            audit_rows.append(
                {
                    "Ticker": ticker,
                    "YahooSymbol": symbol,
                    "CompanyName": company_name,
                    "CheckedAt": checked_at,
                    "YahooStatus": "fetch_error",
                    "ReverseSplitConfirmed": False,
                    "ReverseSplitCount": 0,
                    "LatestReverseSplitDate": "",
                    "LatestReverseSplitRatio": "",
                    "SplitCount": 0,
                    "CacheStatus": "error",
                    "FetchError": f"{type(exc).__name__}: {exc}",
                }
            )
            LOGGER.warning("Yahoo reverse-split check failed for %s: %s", ticker, exc)

        if idx < len(ticker_rows) and args.sleep_sec > 0:
            time.sleep(args.sleep_sec)

    write_csv(args.output, event_rows, EVENT_COLUMNS)
    write_csv(args.audit_output, audit_rows, AUDIT_COLUMNS)

    confirmed_count = len({row["Ticker"] for row in event_rows})
    LOGGER.info("Wrote %d confirmed reverse-split event(s) for %d ticker(s): %s", len(event_rows), confirmed_count, args.output)
    LOGGER.info("Wrote audit results for %d ticker(s): %s", len(audit_rows), args.audit_output)
    if fetch_errors:
        LOGGER.warning("Yahoo fetch errors: %d", fetch_errors)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

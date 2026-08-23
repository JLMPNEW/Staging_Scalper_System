#!/usr/bin/env python3
"""
Build ticker -> CIK mapping (with company name) for a ticker universe CSV.

Default behavior:
  input : index_constituents_out/All_tickers.csv
  output: index_constituents_out/cik_ticker_mapping.csv

Data source:
  SEC public files endpoint:
    - https://www.sec.gov/files/company_tickers_exchange.json
    - fallback: https://www.sec.gov/files/company_tickers.json

Notes:
  - This script writes one output row per input ticker.
  - If no SEC match is found, CIK/name fields are left blank and MatchType='unmatched'.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import ssl
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

import pandas as pd

def _https_context() -> ssl.SSLContext:
    """Build a verified HTTPS context without depending on a broken Windows trust store."""
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


SEC_TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SEC_TICKERS_BASIC_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_BROWSE_EDGAR_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={query}&owner=exclude&count=40"
DEFAULT_USER_AGENT = os.getenv("SEC_USER_AGENT", "").strip()
PLACEHOLDER_USER_AGENT = "StagingScalperSystem/1.0 (admin@example.com)"

DEFAULT_INPUT = Path("index_constituents_out/All_tickers.csv")
DEFAULT_OUTPUT = Path("index_constituents_out/cik_ticker_mapping.csv")
DEFAULT_CACHE_DIR = Path("index_constituents_out/_sec_cache")

_DEFAULT_COL_CANDIDATES = [
    "ticker",
    "tickers",
    "symbol",
    "symbols",
    "ticker_symbol",
    "ticker symbol",
]

_DEFAULT_NAME_COL_CANDIDATES = [
    "name",
    "company",
    "company name",
    "company_name",
    "issuer",
    "issuer name",
    "issuer_name",
]

CORPORATE_SUFFIXES = {
    "INC",
    "INCORPORATED",
    "CORP",
    "CORPORATION",
    "COMPANY",
    "CO",
    "LTD",
    "LIMITED",
    "PLC",
    "AG",
    "NV",
    "N V",
    "SA",
    "S A",
    "SE",
    "LP",
    "LLC",
    "HOLDINGS",
    "HOLDING",
    "GROUP",
}

BROWSE_COMPANY_RE = re.compile(
    r'<span class="companyName">\s*(?P<name>.*?)\s*<acronym[^>]*>\s*CIK\s*</acronym>#:\s*<a [^>]*>\s*(?P<cik>\d{10})\s*\(see all company filings\)',
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class SecEntry:
    ticker: str
    cik: str
    company_name: str
    exchange: str
    source: str


@dataclass(frozen=True)
class InputTicker:
    ticker: str
    company_name: str


def _strip_inline_comment(text: str) -> str:
    in_single = False
    in_double = False
    out: list[str] = []
    for ch in text:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            break
        out.append(ch)
    return "".join(out).strip()


def _parse_scalar(raw: str) -> Any:
    text = _strip_inline_comment(str(raw or "").strip())
    if not text:
        return ""
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        return text[1:-1]
    lower = text.lower()
    if lower in {"true", "yes", "on"}:
        return True
    if lower in {"false", "no", "off"}:
        return False
    try:
        if re.fullmatch(r"[+-]?\d+", text):
            return int(text)
        if re.fullmatch(r"[+-]?(?:\d+\.\d*|\d*\.\d+)", text):
            return float(text)
    except Exception:
        pass
    return text


def load_simple_yaml_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config YAML not found: {path}")
    config: Dict[str, Any] = {}
    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw_line[: len(raw_line) - len(raw_line.lstrip())]:
            raise ValueError(f"Nested YAML is not supported in {path} at line {lineno}.")
        if ":" not in raw_line:
            raise ValueError(f"Invalid YAML line in {path} at line {lineno}: {raw_line}")
        key, value = raw_line.split(":", 1)
        key = str(key).strip()
        if not key:
            raise ValueError(f"Missing key in {path} at line {lineno}.")
        config[key] = _parse_scalar(value)
    return config


def _resolve_config_path(config_path: Path, raw_value: Any) -> str:
    text = str(raw_value or "").strip()
    if not text:
        return text
    path_like = Path(text)
    if path_like.is_absolute():
        return str(path_like)
    return str((config_path.parent / path_like).resolve())


def apply_config_defaults(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    argv: Sequence[str],
) -> argparse.Namespace:
    config_path_raw = getattr(args, "config", "")
    if not config_path_raw:
        return args

    config_path = Path(str(config_path_raw)).expanduser().resolve()
    config = load_simple_yaml_config(config_path)
    allowed_keys = {"input", "output", "ticker_column", "cache_dir", "timeout_sec", "user_agent"}
    unknown_keys = sorted(set(config).difference(allowed_keys))
    if unknown_keys:
        raise ValueError(f"Unsupported config keys in {config_path.name}: {unknown_keys}")

    cli_keys = {
        token[2:].split("=", 1)[0].replace("-", "_")
        for token in argv
        if isinstance(token, str) and token.startswith("--") and token != "--config"
    }
    for key, value in config.items():
        if key in cli_keys:
            continue
        if key in {"input", "output", "cache_dir"}:
            value = _resolve_config_path(config_path, value)
        setattr(args, key, value)
    return args


def resolve_user_agent(raw_user_agent: str) -> str:
    user_agent = (raw_user_agent or "").strip()
    lower_ua = user_agent.lower()
    if (
        not user_agent
        or user_agent == PLACEHOLDER_USER_AGENT
        or "example.com" in lower_ua
        or "your name" in lower_ua
    ):
        raise SystemExit(
            "Missing SEC User-Agent. Pass --user-agent with a real identity string. "
            "Example: 'JL, Independent Research, jm.357@hotmail.com'."
        )
    return user_agent


def normalize_ticker(value: str) -> str:
    s = str(value or "").strip().upper()
    if not s:
        return ""
    return s.replace(".", "-")


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())


def _normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _find_ticker_col(df: pd.DataFrame, explicit: Optional[str]) -> str:
    norm_to_raw = {_normalize_header(c): str(c) for c in df.columns}
    if explicit is not None and str(explicit).strip():
        explicit_s = str(explicit).strip()
        if explicit_s in df.columns:
            return explicit_s
        norm_explicit = _normalize_header(explicit_s)
        found = norm_to_raw.get(norm_explicit)
        if found is not None:
            return found
        raise ValueError(f"Requested ticker column '{explicit_s}' not found in input CSV.")

    for cand in _DEFAULT_COL_CANDIDATES:
        found = norm_to_raw.get(_normalize_header(cand))
        if found is not None:
            return found
    return str(df.columns[0])


def _find_optional_name_col(df: pd.DataFrame) -> Optional[str]:
    norm_to_raw = {_normalize_header(c): str(c) for c in df.columns}
    for cand in _DEFAULT_NAME_COL_CANDIDATES:
        found = norm_to_raw.get(_normalize_header(cand))
        if found is not None:
            return found
    return None


def normalize_company_name(value: str) -> str:
    text = str(value or "").upper()
    text = text.replace("&", " AND ")
    text = re.sub(r"[^A-Z0-9 ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def strip_company_suffixes(norm_name: str) -> str:
    tokens = [tok for tok in str(norm_name or "").split() if tok]
    while tokens and tokens[-1] in CORPORATE_SUFFIXES:
        tokens.pop()
    return " ".join(tokens).strip()


def load_tickers(csv_path: Path, ticker_column: Optional[str] = None) -> List[InputTicker]:
    if not csv_path.exists():
        raise FileNotFoundError(f"Input CSV not found: {csv_path}")
    last_error: Exception | None = None
    df: pd.DataFrame | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            df = pd.read_csv(csv_path, dtype=str, encoding=encoding)
            break
        except UnicodeDecodeError as e:
            last_error = e
            continue
        except pd.errors.EmptyDataError:
            raise ValueError(f"Input CSV is empty: {csv_path}")
    if df is None:
        raise ValueError(f"Could not decode CSV {csv_path}: {last_error}")
    if df.empty:
        raise ValueError(f"Input CSV is empty: {csv_path}")

    ticker_col = _find_ticker_col(df, ticker_column)
    name_col = _find_optional_name_col(df)
    out: List[InputTicker] = []
    seen: set[str] = set()
    ticker_series = df[ticker_col].fillna("").astype(str).tolist()
    name_series = df[name_col].fillna("").astype(str).tolist() if name_col else [""] * len(df)
    for raw_ticker, raw_name in zip(ticker_series, name_series):
        t = normalize_ticker(raw_ticker)
        if not t:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(InputTicker(ticker=t, company_name=str(raw_name or "").strip()))
    if not out:
        raise ValueError(f"No usable tickers found in input column '{ticker_col}'.")
    return out


def _http_get_json(url: str, *, timeout: float, user_agent: str) -> Any:
    req = Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urlopen(req, timeout=timeout, context=_https_context()) as resp:
        data = resp.read()
    return json.loads(data.decode("utf-8"))


def _http_get_text(url: str, *, timeout: float, user_agent: str) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,text/plain,*/*",
        },
    )
    with urlopen(req, timeout=timeout, context=_https_context()) as resp:
        data = resp.read()
    return data.decode("utf-8", errors="replace")


def _load_text_cached(url: str, *, cache_path: Path, timeout: float, user_agent: str) -> str:
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8", errors="replace")
    text = _http_get_text(url, timeout=timeout, user_agent=user_agent)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(text, encoding="utf-8")
    except OSError as e:
        logging.warning("Could not write SEC browse cache %s: %s", cache_path, e)
    return text


def _parse_exchange_payload(payload: Any, *, source: str) -> List[SecEntry]:
    if not isinstance(payload, dict):
        raise ValueError("SEC exchange payload is not a JSON object.")
    fields = payload.get("fields", None)
    data = payload.get("data", None)
    if not isinstance(fields, list) or not isinstance(data, list):
        raise ValueError("SEC exchange payload missing fields/data arrays.")

    field_map = {str(name): idx for idx, name in enumerate(fields)}
    required = ("ticker", "cik", "name")
    missing = [f for f in required if f not in field_map]
    if missing:
        raise ValueError(f"SEC exchange payload missing required fields: {missing}")

    idx_ticker = field_map["ticker"]
    idx_cik = field_map["cik"]
    idx_name = field_map["name"]
    idx_exchange = field_map.get("exchange")

    out: List[SecEntry] = []
    for row in data:
        if not isinstance(row, list):
            continue
        try:
            ticker = normalize_ticker(row[idx_ticker])
            cik_val = str(row[idx_cik]).strip()
            company = str(row[idx_name] or "").strip()
            exchange = str(row[idx_exchange] or "").strip() if idx_exchange is not None else ""
            if not ticker or not cik_val:
                continue
            cik10 = str(int(float(cik_val))).zfill(10)
        except Exception:
            continue
        out.append(
            SecEntry(
                ticker=ticker,
                cik=cik10,
                company_name=company,
                exchange=exchange,
                source=source,
            )
        )
    if not out:
        raise ValueError("SEC exchange payload parsed to zero entries.")
    return out


def _parse_basic_payload(payload: Any, *, source: str) -> List[SecEntry]:
    if not isinstance(payload, dict):
        raise ValueError("SEC basic payload is not a JSON object.")

    out: List[SecEntry] = []
    for v in payload.values():
        if not isinstance(v, dict):
            continue
        ticker = normalize_ticker(v.get("ticker", ""))
        cik_raw = v.get("cik_str", "")
        title = str(v.get("title", "")).strip()
        if not ticker or cik_raw in ("", None):
            continue
        try:
            cik10 = str(int(float(cik_raw))).zfill(10)
        except Exception:
            continue
        out.append(
            SecEntry(
                ticker=ticker,
                cik=cik10,
                company_name=title,
                exchange="",
                source=source,
            )
        )
    if not out:
        raise ValueError("SEC basic payload parsed to zero entries.")
    return out


def _read_cached_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_sec_entries(
    *,
    cache_dir: Path,
    timeout: float,
    user_agent: str,
) -> List[SecEntry]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    attempts: List[Tuple[str, str, str]] = [
        (SEC_TICKERS_EXCHANGE_URL, "company_tickers_exchange.json", "exchange"),
        (SEC_TICKERS_BASIC_URL, "company_tickers.json", "basic"),
    ]

    # Try live download first.
    errors: List[str] = []
    for url, cache_name, kind in attempts:
        try:
            payload = _http_get_json(url, timeout=timeout, user_agent=user_agent)
            if kind == "exchange":
                entries = _parse_exchange_payload(payload, source=url)
            else:
                entries = _parse_basic_payload(payload, source=url)
            cache_path = cache_dir / cache_name
            try:
                cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            except OSError as e:
                logging.warning("Could not write SEC cache %s: %s", cache_path, e)
            logging.info("Loaded %d SEC entries from %s", len(entries), url)
            return entries
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as e:
            errors.append(f"{url}: {type(e).__name__}: {e}")

    # Fallback to cached files.
    for _, cache_name, kind in attempts:
        cache_path = cache_dir / cache_name
        if not cache_path.exists():
            continue
        try:
            payload = _read_cached_json(cache_path)
            if kind == "exchange":
                entries = _parse_exchange_payload(payload, source=str(cache_path))
            else:
                entries = _parse_basic_payload(payload, source=str(cache_path))
            logging.info("Loaded %d SEC entries from cache %s", len(entries), cache_path)
            return entries
        except Exception as e:
            errors.append(f"{cache_path}: {type(e).__name__}: {e}")

    raise RuntimeError(
        "Unable to load SEC ticker mapping from live URLs or cache.\n"
        + "\n".join(errors)
    )


def lookup_browse_edgar_ticker(
    ticker: str,
    *,
    cache_dir: Path,
    timeout: float,
    user_agent: str,
) -> Optional[SecEntry]:
    query = normalize_ticker(ticker)
    if not query:
        return None
    url = SEC_BROWSE_EDGAR_URL.format(query=quote_plus(query))
    cache_path = cache_dir / "browse_edgar" / f"{_slug(query)}.html"
    text = _load_text_cached(url, cache_path=cache_path, timeout=timeout, user_agent=user_agent)
    match = BROWSE_COMPANY_RE.search(text)
    if match is None:
        return None
    cik10 = str(match.group("cik") or "").strip()
    company_name = re.sub(r"<[^>]+>", " ", str(match.group("name") or ""))
    company_name = re.sub(r"\s+", " ", html.unescape(company_name)).strip()
    if not cik10 or not company_name:
        return None
    return SecEntry(
        ticker=query,
        cik=cik10,
        company_name=company_name,
        exchange="",
        source=url,
    )


def _exchange_rank(exchange: str) -> int:
    ex = str(exchange or "").strip().lower()
    priority = {
        "nasdaq": 0,
        "nyse": 1,
        "nyse american": 2,
        "nyse arca": 3,
        "cboe": 4,
        "cboe bzx": 4,
    }
    return priority.get(ex, 99)


def _pick_best(entries: Sequence[SecEntry]) -> SecEntry:
    return sorted(
        entries,
        key=lambda x: (_exchange_rank(x.exchange), x.cik, x.company_name),
    )[0]


def _name_keys(value: str) -> List[str]:
    norm_full = normalize_company_name(value)
    norm_stripped = strip_company_suffixes(norm_full)
    out = [x for x in [norm_full, norm_stripped] if x]
    return list(dict.fromkeys(out))


def _candidate_tickers(ticker: str) -> List[str]:
    t = normalize_ticker(ticker)
    return [t] if t else []


def build_rows(
    input_rows: Iterable[InputTicker],
    entries: Sequence[SecEntry],
    *,
    cache_dir: Path,
    timeout: float,
    user_agent: str,
) -> List[Dict[str, str]]:
    by_ticker: Dict[str, List[SecEntry]] = {}
    by_name: Dict[str, List[SecEntry]] = {}
    for e in entries:
        by_ticker.setdefault(normalize_ticker(e.ticker), []).append(e)
        for key in _name_keys(e.company_name):
            by_name.setdefault(key, []).append(e)

    rows: List[Dict[str, str]] = []
    for item in input_rows:
        t = item.ticker
        input_company_name = str(item.company_name or "").strip()
        matched: Optional[SecEntry] = None
        match_type = "unmatched"
        for cand in _candidate_tickers(t):
            cands = by_ticker.get(cand, [])
            if not cands:
                continue
            matched = _pick_best(cands)
            match_type = "exact"
            break

        if matched is None and input_company_name:
            name_matches: List[SecEntry] = []
            for key in _name_keys(input_company_name):
                name_matches.extend(by_name.get(key, []))
            if name_matches:
                deduped = list({(e.cik, e.ticker): e for e in name_matches}.values())
                matched = _pick_best(deduped)
                match_type = "company_name_local"

        if matched is None:
            try:
                matched = lookup_browse_edgar_ticker(
                    t,
                    cache_dir=cache_dir,
                    timeout=timeout,
                    user_agent=user_agent,
                )
            except (HTTPError, URLError, TimeoutError, ValueError, OSError) as e:
                logging.warning("Browse EDGAR fallback failed for %s: %s", normalize_ticker(t), e)
            if matched is not None:
                match_type = "edgar_browse_ticker"

        if matched is None:
            rows.append(
                {
                    "Ticker": normalize_ticker(t),
                    "MatchedTicker": "",
                    "CIK": "",
                    "CompanyName": "",
                    "Exchange": "",
                    "MatchType": match_type,
                    "Source": "",
                }
            )
            continue

        rows.append(
            {
                "Ticker": normalize_ticker(t),
                "MatchedTicker": matched.ticker,
                "CIK": matched.cik,
                "CompanyName": matched.company_name,
                "Exchange": matched.exchange,
                "MatchType": match_type,
                "Source": matched.source,
            }
        )
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build ticker->CIK mapping (with company name) using SEC public mapping files."
    )
    parser.add_argument(
        "--config",
        default="",
        help="Optional simple YAML config file with default parameter values.",
    )
    parser.add_argument(
        "--input",
        default=str(DEFAULT_INPUT),
        help=f"Input tickers CSV (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help=f"Output mapping CSV (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--ticker-column",
        default="",
        help="Optional explicit ticker column name in input CSV.",
    )
    parser.add_argument(
        "--cache-dir",
        default=str(DEFAULT_CACHE_DIR),
        help=f"Directory to cache SEC JSON files (default: {DEFAULT_CACHE_DIR})",
    )
    parser.add_argument(
        "--timeout-sec",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds for SEC download (default: 30).",
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT or PLACEHOLDER_USER_AGENT,
        help="User-Agent header required by SEC endpoint.",
    )
    args = parser.parse_args()
    return apply_config_defaults(parser, args, sys.argv[1:])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args()

    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    cache_dir = Path(args.cache_dir).expanduser().resolve()
    ticker_col = str(args.ticker_column).strip() or None
    user_agent = resolve_user_agent(str(args.user_agent))

    tickers = load_tickers(input_path, ticker_column=ticker_col)
    entries = load_sec_entries(
        cache_dir=cache_dir,
        timeout=float(args.timeout_sec),
        user_agent=user_agent,
    )

    rows = build_rows(
        tickers,
        entries,
        cache_dir=cache_dir,
        timeout=float(args.timeout_sec),
        user_agent=user_agent,
    )
    out_df = pd.DataFrame(rows)
    out_df = out_df.sort_values("Ticker", kind="stable").reset_index(drop=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(output_path, index=False)

    total = len(out_df)
    matched = int((out_df["CIK"].astype(str).str.strip() != "").sum())
    unmatched = total - matched
    logging.info("Saved CIK mapping to: %s", output_path)
    logging.info("Tickers: %d | Matched: %d | Unmatched: %d", total, matched, unmatched)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Enrich a CIK/ticker mapping file with best-effort CUSIP extraction from SEC EDGAR filings.

Input (default):
  index_constituents_out/cik_ticker_mapping.csv

Output (default):
  index_constituents_out/cik_ticker_mapping_with_cusip.csv

Important:
  - CUSIP extraction from filing text is heuristic and may miss/overlook edge cases.
  - Respect SEC fair-access requirements: set a real User-Agent and keep request pacing.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd


SEC_SUBMISSIONS_URL_TMPL = "https://data.sec.gov/submissions/CIK{cik10}.json"
SEC_DATA_BASE = "https://data.sec.gov"
SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
DEFAULT_USER_AGENT = os.getenv("SEC_USER_AGENT", "").strip()
PLACEHOLDER_USER_AGENT = "StagingScalperSystem/1.0 (admin@example.com)"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = PROJECT_ROOT / "index_constituents_out" / "cik_ticker_mapping.csv"
DEFAULT_OUTPUT = PROJECT_ROOT / "index_constituents_out" / "cik_ticker_mapping_with_cusip.csv"
DEFAULT_CACHE_DIR = PROJECT_ROOT / "index_constituents_out" / "_sec_cusip_cache"
DEFAULT_SUBMISSIONS_CACHE_TTL_HOURS = 24.0
DEFAULT_MAX_SUBMISSIONS_PAGES = 5

# Default to issuer-equity-oriented forms. Debt prospectuses, acquisition 8-Ks, and broad 6-Ks frequently
# contain non-common CUSIPs before the issuer common-equity CUSIP; users can opt back in via --forms.
DEFAULT_FORMS = [
    "10-K",
    "10-Q",
    "20-F",
    "40-F",
    "DEF 14A",
    "S-1",
    "S-3",
    "S-4",
]


@dataclass(frozen=True)
class FilingRef:
    cik10: str
    accession: str
    accession_nodash: str
    filing_date: str
    form: str
    primary_document: str


@dataclass(frozen=True)
class CusipHit:
    cusip: str
    method: str
    source_url: str
    form: str
    filing_date: str
    accession: str


def _safe_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and math.isnan(v):
        return ""
    s = str(v).strip()
    if s.lower() in {"", "nan", "none", "null"}:
        return ""
    return s


def normalize_ticker(v: Any) -> str:
    return _safe_text(v).upper().replace(".", "-")


def normalize_cik(v: Any) -> str:
    s = _safe_text(v)
    if not s:
        return ""
    if re.fullmatch(r"\d+(?:\.0+)?", s):
        digits = s.split(".", 1)[0]
    else:
        return ""
    if not digits or len(digits) > 10:
        return ""
    return digits.zfill(10)


def cik_to_int_str(cik10: str) -> str:
    return str(int(cik10))


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
            "Example: 'JL, Independent Research, jm.357@yhotmail.com'."
        )
    return user_agent


def _http_get(
    url: str,
    *,
    timeout: float,
    user_agent: str,
    accept: str = "*/*",
    max_attempts: int = 4,
    retry_backoff_sec: float = 0.75,
) -> bytes:
    attempts = max(1, int(max_attempts))
    retryable_status = {429, 500, 502, 503, 504}
    last_error: Exception | None = None
    for attempt in range(attempts):
        req = Request(
            url,
            headers={
                "User-Agent": user_agent,
                "Accept": accept,
            },
        )
        try:
            with urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except HTTPError as exc:
            last_error = exc
            if exc.code not in retryable_status or attempt >= attempts - 1:
                raise
        except (URLError, TimeoutError) as exc:
            last_error = exc
            if attempt >= attempts - 1:
                raise
        sleep_for = min(max(0.0, retry_backoff_sec) * (2**attempt), 8.0)
        if sleep_for > 0:
            time.sleep(sleep_for)
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"HTTP request failed without exception: {url}")


def _http_get_json(url: str, *, timeout: float, user_agent: str) -> Any:
    raw = _http_get(url, timeout=timeout, user_agent=user_agent, accept="application/json,text/plain,*/*")
    return json.loads(raw.decode("utf-8", errors="replace"))


def _http_get_text(url: str, *, timeout: float, user_agent: str) -> str:
    raw = _http_get(url, timeout=timeout, user_agent=user_agent, accept="text/plain,text/html,*/*")
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def _slug(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", s)


def _load_json_cached(
    url: str,
    *,
    cache_path: Path,
    timeout: float,
    user_agent: str,
    max_age_seconds: Optional[float] = None,
    request_sleep_sec: float = 0.0,
) -> Any:
    if cache_path.exists():
        use_cache = True
        if max_age_seconds is not None and max_age_seconds >= 0:
            age_seconds = time.time() - cache_path.stat().st_mtime
            use_cache = age_seconds <= max_age_seconds
        if use_cache:
            with cache_path.open("r", encoding="utf-8") as f:
                return json.load(f)
    try:
        payload = _http_get_json(url, timeout=timeout, user_agent=user_agent)
    finally:
        if request_sleep_sec > 0:
            time.sleep(request_sleep_sec)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


def _load_text_cached(
    url: str,
    *,
    cache_path: Path,
    timeout: float,
    user_agent: str,
    use_cache: bool,
) -> str:
    if use_cache and cache_path.exists():
        return cache_path.read_text(encoding="utf-8", errors="replace")
    text = _http_get_text(url, timeout=timeout, user_agent=user_agent)
    if use_cache:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(text, encoding="utf-8")
    return text


def _char_to_cusip_value(ch: str) -> int:
    if ch.isdigit():
        return int(ch)
    u = ch.upper()
    if "A" <= u <= "Z":
        return ord(u) - ord("A") + 10
    if u == "*":
        return 36
    if u == "@":
        return 37
    if u == "#":
        return 38
    return -1


def validate_cusip(cusip9: str) -> bool:
    """
    Validate a 9-char CUSIP using the standard checksum algorithm.
    """
    c = re.sub(r"[^0-9A-Za-z*@#]", "", str(cusip9 or "").upper())
    if len(c) != 9:
        return False
    if not c[8].isdigit():
        return False

    total = 0
    for i in range(8):
        v = _char_to_cusip_value(c[i])
        if v < 0:
            return False
        if i % 2 == 1:
            v *= 2
        total += (v // 10) + (v % 10)
    check = (10 - (total % 10)) % 10
    return check == int(c[8])


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Search only whole CUSIP-shaped tokens. Do not slide across long cleaned strings:
# adjacent unrelated numbers/FIGIs/ISINs can otherwise merge into checksum-valid false positives.
_CUSIP_LABEL_RE = re.compile(r"(?i)\bC\.?\s*U\.?\s*S\.?\s*I\.?\s*P\.?(?:\s*(?:No\.?|Number|#))?\b")
_CUSIP_TOKEN_RE = re.compile(
    r"(?<![0-9A-Za-z*@#])"
    r"([0-9A-Za-z*@#]{6}(?:[-\s]?[0-9A-Za-z*@#]{2})(?:[-\s]?[0-9A-Za-z*@#]))"
    r"(?![0-9A-Za-z*@#])"
)
_CUSIP_NEAR_LABEL_CHARS = 240


def _clean_doc_text(raw: str) -> str:
    txt = html.unescape(raw or "")
    txt = _TAG_RE.sub(" ", txt)
    txt = txt.replace("\x00", " ")
    return txt


def _extract_cusip_from_fragment(fragment: str) -> Optional[str]:
    """
    Return the first valid standalone 9-char CUSIP token found in a fragment.
    """
    text = str(fragment or "").upper()
    for match in _CUSIP_TOKEN_RE.finditer(text):
        cand = re.sub(r"[^0-9A-Za-z*@#]", "", match.group(1))
        if len(cand) == 9 and validate_cusip(cand):
            return cand
    cleaned = re.sub(r"[^0-9A-Za-z*@#]", "", text)
    if len(cleaned) == 9:
        cand = cleaned
        if validate_cusip(cand):
            return cand
    return None


def extract_cusip_from_document(text: str) -> Tuple[Optional[str], str]:
    """
    Return (cusip, method) from document text.
    method: labeled | generic | none
    """
    doc = _clean_doc_text(text)
    if not doc:
        return None, "none"

    # Only accept candidates close to an actual CUSIP label. Scanning an entire filing for checksum-valid
    # 9-character windows produces false positives from CIKs, dates, share counts, FIGIs, and ISINs.
    for m in _CUSIP_LABEL_RE.finditer(doc):
        frag = doc[m.start() : m.end() + _CUSIP_NEAR_LABEL_CHARS]
        cusip = _extract_cusip_from_fragment(frag)
        if cusip:
            return cusip, "labeled"
    return None, "none"


def _recent_filings_from_submissions(payload: Any, *, fallback_cik10: str = "") -> List[FilingRef]:
    if not isinstance(payload, dict):
        return []
    filings = payload.get("filings", {})
    if not isinstance(filings, dict):
        return []
    recent = filings.get("recent", {})
    if not isinstance(recent, dict):
        return []

    forms = recent.get("form", []) or []
    accs = recent.get("accessionNumber", []) or []
    dates = recent.get("filingDate", []) or []
    docs = recent.get("primaryDocument", []) or []

    n = min(len(forms), len(accs), len(dates), len(docs))
    out: List[FilingRef] = []
    cik10 = normalize_cik(payload.get("cik")) or normalize_cik(fallback_cik10)
    for i in range(n):
        acc_dashed = _safe_text(accs[i])
        acc = acc_dashed.replace("-", "")
        form = _safe_text(forms[i])
        fdate = _safe_text(dates[i])
        pdoc = _safe_text(docs[i])
        if not cik10 or not acc:
            continue
        out.append(
            FilingRef(
                cik10=cik10,
                accession=acc_dashed,
                accession_nodash=acc,
                filing_date=fdate,
                form=form,
                primary_document=pdoc,
            )
        )
    return out


def _build_submission_urls(f: FilingRef) -> List[str]:
    cik_int = cik_to_int_str(f.cik10)
    base = f"{SEC_ARCHIVES_BASE}/{cik_int}/{f.accession_nodash}"
    urls = []
    if f.primary_document:
        urls.append(f"{base}/{f.primary_document}")
    if f.accession:
        urls.append(f"{base}/{f.accession}.txt")
    return list(dict.fromkeys(urls))


def _doc_cache_path(cache_dir: Path, url: str) -> Path:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return cache_dir / "docs" / f"{digest}.txt"


def _submissions_cache_path(cache_dir: Path, cik10: str) -> Path:
    return cache_dir / "submissions" / f"CIK{cik10}.json"


def _submissions_page_cache_path(cache_dir: Path, cik10: str, page_url: str) -> Path:
    digest = hashlib.sha1(page_url.encode("utf-8")).hexdigest()
    return cache_dir / "submissions" / f"CIK{cik10}_{digest}.json"


def _normalize_submissions_page_url(raw: str) -> str:
    text = _safe_text(raw)
    if not text:
        return ""
    if text.startswith("http://") or text.startswith("https://"):
        return text
    if text.startswith("/"):
        return f"{SEC_DATA_BASE}{text}"
    if text.lower().startswith("submissions/"):
        return f"{SEC_DATA_BASE}/{text}"
    return f"{SEC_DATA_BASE}/submissions/{text.lstrip('/')}"


def _submissions_page_urls(payload: Any) -> List[str]:
    if not isinstance(payload, dict):
        return []
    filings = payload.get("filings", {})
    if not isinstance(filings, dict):
        return []
    files = filings.get("files", [])
    if not isinstance(files, list):
        return []
    out: List[str] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        href = _normalize_submissions_page_url(_safe_text(item.get("filingHref")))
        if not href:
            href = _normalize_submissions_page_url(_safe_text(item.get("name")))
        if href:
            out.append(href)
    return list(dict.fromkeys(out))


def normalize_asof_date(v: Any) -> str:
    text = _safe_text(v)
    if not text:
        return ""
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", text)
    if match:
        return match.group(0)
    match = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", text)
    if match:
        month, day, year = match.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    return ""


def _row_asof_date(row: pd.Series, fallback: str) -> str:
    for column in ("asof_date", "AsOfDate", "as_of_date", "Date", "date", "snapshot_date"):
        if column in row.index:
            asof = normalize_asof_date(row.get(column))
            if asof:
                return asof
    return fallback


def find_cusip_for_cik(
    cik10: str,
    *,
    cache_dir: Path,
    timeout: float,
    user_agent: str,
    request_sleep_sec: float,
    max_filings: int,
    allowed_forms: Optional[set[str]],
    use_doc_cache: bool,
    submissions_cache_ttl_seconds: Optional[float],
    max_submissions_pages: int,
    asof_date: str = "",
) -> Tuple[Optional[CusipHit], str]:
    """
    Return (CusipHit or None, error_message).
    """
    if not cik10:
        return None, "missing_cik"

    sub_url = SEC_SUBMISSIONS_URL_TMPL.format(cik10=cik10)
    try:
        payload = _load_json_cached(
            sub_url,
            cache_path=_submissions_cache_path(cache_dir, cik10),
            timeout=timeout,
            user_agent=user_agent,
            max_age_seconds=submissions_cache_ttl_seconds,
            request_sleep_sec=max(0.0, request_sleep_sec),
        )
    except Exception as e:
        return None, f"submissions_error:{type(e).__name__}"

    filings = _recent_filings_from_submissions(payload, fallback_cik10=cik10)
    if max_submissions_pages <= 0:
        page_urls = []
    else:
        page_urls = _submissions_page_urls(payload)[:max_submissions_pages]
    for page_url in page_urls:
        try:
            page_payload = _load_json_cached(
                page_url,
                cache_path=_submissions_page_cache_path(cache_dir, cik10, page_url),
                timeout=timeout,
                user_agent=user_agent,
                max_age_seconds=submissions_cache_ttl_seconds,
                request_sleep_sec=max(0.0, request_sleep_sec),
            )
        except Exception:
            continue
        filings.extend(_recent_filings_from_submissions(page_payload, fallback_cik10=cik10))

    # Deduplicate by accession; keep the first (most recent) occurrence.
    deduped: List[FilingRef] = []
    seen_accessions: set[str] = set()
    for filing in sorted(
        filings,
        key=lambda x: (x.filing_date, x.accession_nodash),
        reverse=True,
    ):
        if filing.accession_nodash in seen_accessions:
            continue
        seen_accessions.add(filing.accession_nodash)
        deduped.append(filing)
    filings = deduped
    if allowed_forms is not None:
        filings = [f for f in filings if f.form.upper() in allowed_forms]
    asof = normalize_asof_date(asof_date)
    if asof:
        filings = [f for f in filings if not f.filing_date or f.filing_date <= asof]
    if max_filings > 0:
        filings = filings[:max_filings]
    if not filings:
        return None, "no_recent_filings"

    for f in filings:
        urls = _build_submission_urls(f)
        for u in urls:
            cache_path = _doc_cache_path(cache_dir, u)
            from_cache = bool(use_doc_cache and cache_path.exists())
            try:
                text = _load_text_cached(
                    u,
                    cache_path=cache_path,
                    timeout=timeout,
                    user_agent=user_agent,
                    use_cache=use_doc_cache,
                )
            except (HTTPError, URLError, TimeoutError):
                continue
            except Exception:
                continue
            finally:
                if request_sleep_sec > 0 and not from_cache:
                    time.sleep(request_sleep_sec)

            cusip, method = extract_cusip_from_document(text)
            if cusip:
                hit = CusipHit(
                    cusip=cusip,
                    method=method,
                    source_url=u,
                    form=f.form,
                    filing_date=f.filing_date,
                    accession=f.accession or f.accession_nodash,
                )
                return hit, ""
    return None, "not_found_in_scanned_filings"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Extract best-effort CUSIPs from SEC EDGAR filings for rows in cik_ticker_mapping.csv."
    )
    p.add_argument("--input", default=str(DEFAULT_INPUT), help=f"Input mapping CSV (default: {DEFAULT_INPUT})")
    p.add_argument("--output", default=str(DEFAULT_OUTPUT), help=f"Output enriched CSV (default: {DEFAULT_OUTPUT})")
    p.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR), help=f"Cache directory (default: {DEFAULT_CACHE_DIR})")
    p.add_argument("--timeout-sec", type=float, default=30.0, help="HTTP timeout seconds (default: 30)")
    p.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT or PLACEHOLDER_USER_AGENT,
        help="User-Agent for SEC requests (required by SEC fair access policy).",
    )
    p.add_argument(
        "--sleep-sec",
        type=float,
        default=0.20,
        help="Sleep between EDGAR document requests (default: 0.20).",
    )
    p.add_argument(
        "--max-filings-per-cik",
        type=int,
        default=20,
        help="Max recent filings to scan per CIK (default: 20).",
    )
    p.add_argument(
        "--submissions-cache-ttl-hours",
        type=float,
        default=DEFAULT_SUBMISSIONS_CACHE_TTL_HOURS,
        help="Max age for cached submissions JSON before refresh (default: 24). Use <0 for no expiry.",
    )
    p.add_argument(
        "--max-submissions-pages",
        type=int,
        default=DEFAULT_MAX_SUBMISSIONS_PAGES,
        help="Max paginated submissions pages to scan beyond recent (default: 5).",
    )
    p.add_argument(
        "--forms",
        default=",".join(DEFAULT_FORMS),
        help="Comma-separated forms to scan (default curated set). Use empty string for all forms.",
    )
    p.add_argument(
        "--asof-date",
        default="",
        help=(
            "Optional PIT cutoff date (YYYY-MM-DD). Rows may override this with asof_date/AsOfDate/date columns; "
            "filings after the row as-of date are ignored."
        ),
    )
    p.add_argument(
        "--max-tickers",
        type=int,
        default=0,
        help="Optional cap for testing. 0 means all.",
    )
    p.add_argument(
        "--use-doc-cache",
        action="store_true",
        help="Cache fetched EDGAR filing documents locally for repeat runs.",
    )
    return p.parse_args()


def _allowed_forms_from_arg(raw: str) -> Optional[set[str]]:
    s = str(raw or "").strip()
    if not s:
        return None
    vals = [x.strip().upper() for x in s.split(",") if x.strip()]
    if not vals:
        return None
    return set(vals)


def _ensure_columns(df: pd.DataFrame, cols: Sequence[str]) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"Input CSV missing required columns: {missing}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args()

    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    cache_dir = Path(args.cache_dir).expanduser().resolve()
    allowed_forms = _allowed_forms_from_arg(args.forms)
    user_agent = resolve_user_agent(str(args.user_agent))
    ttl_hours = float(args.submissions_cache_ttl_hours)
    submissions_cache_ttl_seconds: Optional[float]
    if ttl_hours < 0:
        submissions_cache_ttl_seconds = None
    else:
        submissions_cache_ttl_seconds = max(0.0, ttl_hours) * 3600.0

    if not input_path.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_path}")

    df = pd.read_csv(input_path, dtype=str, encoding="utf-8-sig", keep_default_na=False)
    if df.empty:
        raise ValueError(f"Input CSV is empty: {input_path}")
    _ensure_columns(df, ["Ticker", "CIK"])

    # Normalize core fields.
    df["Ticker"] = df["Ticker"].map(normalize_ticker)
    df["CIK"] = df["CIK"].map(normalize_cik)

    if int(args.max_tickers) > 0:
        df = df.head(int(args.max_tickers)).copy()

    out = df.copy().reset_index(drop=True)
    out["CUSIP"] = ""
    out["CusipMethod"] = ""
    out["CusipSourceUrl"] = ""
    out["CusipForm"] = ""
    out["CusipFilingDate"] = ""
    out["CusipAccession"] = ""
    out["CusipAsOfDate"] = ""
    out["CusipError"] = ""

    cik_ticker_counts = (
        out.loc[out["CIK"].astype(str).str.len() > 0, ["CIK", "Ticker"]]
        .drop_duplicates()
        .groupby("CIK")["Ticker"]
        .nunique()
    )
    ambiguous_ciks = set(cik_ticker_counts[cik_ticker_counts > 1].index)
    default_asof_date = normalize_asof_date(args.asof_date)

    total = len(out)
    hits = 0
    for n, idx in enumerate(out.index, start=1):
        cik10 = _safe_text(out.at[idx, "CIK"])
        if not cik10:
            out.at[idx, "CusipError"] = "missing_cik"
            continue
        row_asof_date = _row_asof_date(out.loc[idx], default_asof_date)
        out.at[idx, "CusipAsOfDate"] = row_asof_date
        if cik10 in ambiguous_ciks:
            out.at[idx, "CusipError"] = "ambiguous_cik_multiple_tickers"
            continue

        hit, err = find_cusip_for_cik(
            cik10,
            cache_dir=cache_dir,
            timeout=float(args.timeout_sec),
            user_agent=user_agent,
            request_sleep_sec=max(0.0, float(args.sleep_sec)),
            max_filings=max(0, int(args.max_filings_per_cik)),
            allowed_forms=allowed_forms,
            use_doc_cache=bool(args.use_doc_cache),
            submissions_cache_ttl_seconds=submissions_cache_ttl_seconds,
            max_submissions_pages=max(0, int(args.max_submissions_pages)),
            asof_date=row_asof_date,
        )
        if hit is None:
            out.at[idx, "CusipError"] = err
        else:
            out.at[idx, "CUSIP"] = hit.cusip
            out.at[idx, "CusipMethod"] = hit.method
            out.at[idx, "CusipSourceUrl"] = hit.source_url
            out.at[idx, "CusipForm"] = hit.form
            out.at[idx, "CusipFilingDate"] = hit.filing_date
            out.at[idx, "CusipAccession"] = hit.accession
            out.at[idx, "CusipError"] = ""
            hits += 1

        if n % 50 == 0 or n == total:
            logging.info("Processed %d/%d | CUSIP found: %d", n, total, hits)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(output_path, index=False)
    logging.info("Saved enriched mapping: %s", output_path)
    logging.info("Rows: %d | CUSIP found: %d | Missing: %d", total, hits, total - hits)


if __name__ == "__main__":
    main()

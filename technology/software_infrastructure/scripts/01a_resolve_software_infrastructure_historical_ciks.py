#!/usr/bin/env python3
"""Resolve SEC CIKs and generate the software-infrastructure historical seed."""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, expand_env_vars, load_yaml  # noqa: E402
from technology.core.logging_utils import configure_utc_logging  # noqa: E402
from technology.core.text_norm import normalize_cik  # noqa: E402


LOGGER = logging.getLogger("resolve_software_infrastructure_historical_ciks")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_INPUT = (
    PROJECT_ROOT
    / "output"
    / "technology_reports"
    / "software_infrastructure"
    / "universe"
    / "software_infrastructure_historical_membership_final_include_list.csv"
)
DEFAULT_OUTPUT = PACKAGE_ROOT / "software_infrastructure" / "data" / "software_infrastructure_historical_membership.csv"
DEFAULT_AUDIT = (
    PROJECT_ROOT
    / "output"
    / "technology_reports"
    / "software_infrastructure"
    / "universe"
    / "software_infrastructure_historical_membership_cik_resolution.csv"
)
DEFAULT_CACHE_DIR = (
    PROJECT_ROOT / "output" / "technology_cache" / "software_infrastructure" / "sec_cik_resolution"
)
SEC_BROWSE_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company={query}&owner=exclude&count=40"


QUERY_OVERRIDES = {
    "VMW": ["VMWARE, INC.", "VMware"],
    "CLDR": ["CLOUDERA, INC.", "Cloudera"],
    "HDP": ["HORTONWORKS, INC.", "Hortonworks"],
    "SUMO": ["SUMO LOGIC, INC.", "Sumo Logic"],
    "WORK": ["Slack Technologies, Inc.", "Slack"],
    "HCP": ["HashiCorp, Inc.", "HashiCorp"],
    "PVTL": ["Pivotal Software, Inc.", "Pivotal"],
    "RAX": ["Rackspace Hosting, Inc.", "Rackspace"],
    "AYX": ["Alteryx, Inc.", "Alteryx"],
    "LOGM": ["LogMeIn, Inc.", "LogMeIn"],
    "SWI_2016": ["SolarWinds, Inc.", "SolarWinds"],
    "SWI_2025": ["SolarWinds Corp", "SolarWinds"],
    "EVBG": ["Everbridge, Inc.", "Everbridge"],
    "SEND": ["SendGrid, Inc.", "SendGrid"],
    "CBLK": ["Carbon Black, Inc.", "Carbon Black"],
    "FORG": ["ForgeRock, Inc.", "ForgeRock"],
    "MOBL": ["MobileIron, Inc.", "MobileIron"],
    "LLNW_EGIO": ["Edgio, Inc.", "EGIO", "Limelight Networks Inc"],
    "MNDT": ["Mandiant Inc", "FireEye Inc"],
    "FIRE": ["Sourcefire Inc"],
    "SAAS": ["inContact, Inc.", "inContact Inc"],
    "CA": ["CA, Inc.", "CA Technologies Inc"],
    "MFE": ["McAfee, Inc.", "Network Associates Inc", "McAfee Inc"],
    "MCFE": ["McAfee Corp"],
    "BCSI": ["Blue Coat Systems Inc"],
}
AUDIT_FIELDS = [
    "internal_ticker",
    "exchange_ticker",
    "price_source_symbol",
    "company_name",
    "resolved_cik",
    "sec_name",
    "match_score",
    "query",
    "method",
    "status",
    "candidate_count",
    "errors",
]

MANUAL_CIK_OVERRIDES: dict[str, tuple[str, str]] = {
    # SEC browse can point reused symbols at current companies. These are
    # retained as deterministic guardrails for known historical rows.
    "RHT": ("1087423", "manual_operating_company_cik_red_hat"),
    "VG": ("1272830", "manual_known_company_cik_vonage"),
    "SAIL_2022": ("1627857", "manual_known_company_cik_sailpoint_2017_2022"),
}

STOPWORDS = {
    "a",
    "and",
    "class",
    "co",
    "company",
    "corp",
    "corporation",
    "holdings",
    "inc",
    "incorporated",
    "international",
    "limited",
    "llc",
    "ltd",
    "nv",
    "plc",
    "sa",
    "software",
    "systems",
    "technologies",
    "technology",
    "the",
}


@dataclass(frozen=True)
class SecCandidate:
    cik: str
    name: str
    source: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--input-csv", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--audit-csv", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--user-agent", default="")
    parser.add_argument("--sleep-sec", type=float, default=0.12)
    parser.add_argument("--min-score", type=float, default=0.35)
    parser.add_argument("--allow-unresolved", action="store_true")
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [{str(k): str(v or "").strip() for k, v in row.items()} for row in csv.DictReader(handle)]


def normalize_name(raw: str) -> list[str]:
    text = html.unescape(raw or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return [token for token in text.split() if token and token not in STOPWORDS]


def name_score(expected: str, candidate: str) -> float:
    expected_tokens = set(normalize_name(expected))
    candidate_tokens = set(normalize_name(candidate))
    if not expected_tokens or not candidate_tokens:
        return 0.0
    overlap = len(expected_tokens & candidate_tokens)
    union = len(expected_tokens | candidate_tokens)
    score = overlap / union if union else 0.0
    expected_norm = " ".join(normalize_name(expected))
    candidate_norm = " ".join(normalize_name(candidate))
    if expected_norm and expected_norm in candidate_norm:
        score += 0.35
    if candidate_norm and candidate_norm in expected_norm:
        score += 0.20
    return min(score, 1.0)


def cache_path(cache_dir: Path, query: str) -> Path:
    digest = hashlib.sha256(f"company:{query}".encode("utf-8")).hexdigest()[:16]
    safe = re.sub(r"[^A-Za-z0-9]+", "_", query).strip("_")[:60] or "query"
    return cache_dir / f"{safe}_{digest}.html"


def fetch_sec_browse(query: str, *, cache_dir: Path, user_agent: str, sleep_sec: float) -> tuple[str, str]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_path(cache_dir, query)
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace"), "cache"
    url = SEC_BROWSE_URL.format(query=quote_plus(query))
    response = requests.get(url, headers={"User-Agent": user_agent}, timeout=30)
    response.raise_for_status()
    text = response.text
    path.write_text(text, encoding="utf-8")
    time.sleep(max(0.0, sleep_sec))
    return text, "sec_browse"


def candidates_from_html(text: str, *, source: str) -> list[SecCandidate]:
    candidates: list[SecCandidate] = []
    soup = BeautifulSoup(text, "html.parser")
    company = soup.find("span", class_="companyName")
    if company is not None:
        company_text = " ".join(company.get_text(" ", strip=True).split())
        match = re.search(r"CIK[#:\s]*(\d{1,10})", company_text, flags=re.IGNORECASE)
        if match:
            name = re.sub(r"\s*CIK[#:\s]*\d{1,10}.*$", "", company_text, flags=re.IGNORECASE).strip()
            candidates.append(SecCandidate(normalize_cik(match.group(1)), name, source))

    for link in soup.find_all("a", href=True):
        href = str(link.get("href") or "")
        match = re.search(r"(?:CIK=|/CIK)(\d{1,10})", href, flags=re.IGNORECASE)
        if not match:
            continue
        name = " ".join(link.get_text(" ", strip=True).split())
        if not name or name.isdigit():
            parent = link.find_parent("tr")
            if parent is not None:
                name = " ".join(parent.get_text(" ", strip=True).split())
        cik = normalize_cik(match.group(1))
        if cik and name:
            candidates.append(SecCandidate(cik, name, source))

    deduped: dict[tuple[str, str], SecCandidate] = {}
    for candidate in candidates:
        if candidate.cik:
            deduped[(candidate.cik, candidate.name.lower())] = candidate
    return list(deduped.values())


BARE_TICKER_MAX_AGE_DAYS = 730


def membership_end_date(row: dict[str, str]) -> date | None:
    text = str(row.get("end_date") or "").strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def query_terms(row: dict[str, str]) -> list[str]:
    ticker = str(row.get("internal_ticker") or "").strip().upper()
    terms = list(QUERY_OVERRIDES.get(ticker, []))
    company = str(row.get("company_name") or "").strip()
    if company:
        terms.append(company)
    source_name = str(row.get("notes") or "").strip()
    if source_name:
        terms.append(source_name)
    # Bare exchange-ticker browse queries resolve recycled symbols to their
    # CURRENT SEC owner. Rows whose membership ended more than
    # BARE_TICKER_MAX_AGE_DAYS ago are the recycling risk, so those rows must
    # resolve through name-based queries only.
    end_date = membership_end_date(row)
    ticker_query_allowed = end_date is not None and (date.today() - end_date).days <= BARE_TICKER_MAX_AGE_DAYS
    exchange_ticker = str(row.get("exchange_ticker") or "").strip().upper()
    if (
        exchange_ticker
        and ticker_query_allowed
        and exchange_ticker not in {ticker, "VG", "SAIL", "INFA", "SWI", "CA"}
    ):
        terms.append(exchange_ticker)
    out: list[str] = []
    seen: set[str] = set()
    for term in terms:
        key = term.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(term.strip())
    return out


def resolve_cik(row: dict[str, str], *, cache_dir: Path, user_agent: str, sleep_sec: float, min_score: float) -> dict[str, Any]:
    ticker = str(row.get("internal_ticker") or "").strip().upper()
    expected_name = str(row.get("company_name") or "").strip()
    if ticker in MANUAL_CIK_OVERRIDES:
        cik, method = MANUAL_CIK_OVERRIDES[ticker]
        return {
            "cik": normalize_cik(cik),
            "sec_name": expected_name,
            "match_score": 1.0,
            "query": "",
            "method": method,
            "status": "resolved",
            "candidate_count": 0,
        }

    all_candidates: list[tuple[float, str, SecCandidate]] = []
    errors: list[str] = []
    for query in query_terms(row):
        try:
            text, source = fetch_sec_browse(query, cache_dir=cache_dir, user_agent=user_agent, sleep_sec=sleep_sec)
            candidates = candidates_from_html(text, source=source)
            for candidate in candidates:
                score = name_score(expected_name, candidate.name)
                all_candidates.append((score, query, candidate))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{query}:{type(exc).__name__}:{exc}")

    if not all_candidates:
        return {
            "cik": "",
            "sec_name": "",
            "match_score": 0.0,
            "query": "",
            "method": "unresolved_no_candidates",
            "status": "unresolved",
            "candidate_count": 0,
            "errors": "; ".join(errors),
        }

    all_candidates.sort(key=lambda item: (item[0], len(item[2].name)), reverse=True)
    score, query, candidate = all_candidates[0]
    return {
        "cik": candidate.cik,
        "sec_name": candidate.name,
        "match_score": round(score, 4),
        "query": query,
        "method": f"sec_browse_name_match:{candidate.source}",
        "status": "resolved" if score >= min_score else "review",
        "candidate_count": len(all_candidates),
        "errors": "; ".join(errors),
    }


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config = load_yaml(args.config)
    user_agent = expand_env_vars(
        args.user_agent or str(cfg_get(config, "sec_fundamentals.user_agent", ""))
    ).strip()
    if not user_agent:
        raise SystemExit("SEC user-agent is required. Set TECHNOLOGY_USER_AGENT or pass --user-agent.")

    rows = read_csv(args.input_csv)
    if not rows:
        raise SystemExit(f"No rows found in {args.input_csv}")

    output_rows: list[dict[str, str]] = []
    audit_rows: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for row in rows:
        resolution = resolve_cik(
            row,
            cache_dir=args.cache_dir,
            user_agent=user_agent,
            sleep_sec=args.sleep_sec,
            min_score=args.min_score,
        )
        cik = normalize_cik(resolution.get("cik"))
        score = float(resolution.get("match_score") or 0.0)
        status = str(resolution.get("status") or "")
        low_confidence = not cik or score < args.min_score
        if low_confidence:
            unresolved.append(str(row.get("internal_ticker") or ""))

        out = dict(row)
        if low_confidence:
            # Never write a low-confidence candidate CIK into the seed CSV;
            # downstream loaders must not ingest it. The full candidate detail
            # stays in the audit CSV for manual review.
            out["cik"] = ""
            out["cik_resolution_status"] = "review"
        else:
            out["cik"] = cik
            out["cik_resolution_status"] = "resolved"
        output_rows.append(out)
        audit_rows.append(
            {
                "internal_ticker": row.get("internal_ticker", ""),
                "exchange_ticker": row.get("exchange_ticker", ""),
                "price_source_symbol": row.get("price_source_symbol", ""),
                "company_name": row.get("company_name", ""),
                "resolved_cik": cik,
                "sec_name": resolution.get("sec_name", ""),
                "match_score": resolution.get("match_score", ""),
                "query": resolution.get("query", ""),
                "method": resolution.get("method", ""),
                "status": status,
                "candidate_count": resolution.get("candidate_count", ""),
                "errors": resolution.get("errors", ""),
            }
        )

    if unresolved and not args.allow_unresolved:
        raise SystemExit(f"Unresolved/low-confidence CIKs: {', '.join(unresolved)}")

    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    args.audit_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.output_csv.open("w", newline="", encoding="utf-8") as handle:
        output_fields = list(output_rows[0].keys()) if output_rows else list(rows[0].keys())
        writer = csv.DictWriter(handle, fieldnames=output_fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(output_rows)
    with args.audit_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=AUDIT_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(audit_rows)

    LOGGER.info(
        "Wrote software-infrastructure historical seed: rows=%d unresolved=%d output=%s audit=%s",
        len(output_rows),
        len(unresolved),
        args.output_csv,
        args.audit_csv,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

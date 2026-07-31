from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import quote, urlparse


NON_SEC_ENDPOINT_VERSION = (
    "transportation_dp6k_non_sec_endpoint_roots_v1"
)
ENDPOINT_FIELDS = (
    "endpoint_version",
    "endpoint_id",
    "ticker",
    "company_name",
    "universe_role",
    "calibration_cohort",
    "endpoint_type",
    "discovery_url",
    "approved_domain",
    "target_domain",
    "source_basis",
    "source_id",
    "freshness_status",
    "confidence",
    "domain_observation_count",
    "domain_name_token_match",
    "metric_pair_count",
    "metric_ids",
    "candidate_source_lane_ids",
    "endpoint_status",
    "retrieval_authorized",
)
PAIR_ENDPOINT_FIELDS = (
    "endpoint_version",
    "pair_key",
    "ticker",
    "metric_id",
    "coverage_status",
    "required_action",
    "endpoint_id",
    "endpoint_type",
    "discovery_url",
    "approved_domain",
    "target_domain",
    "candidate_source_lane_ids",
    "search_aliases",
    "endpoint_status",
    "retrieval_authorized",
)

URL_PATTERN = re.compile(
    r"""https?://[^\s"'<>]+|"""
    r"""(?:www\.)[a-z0-9][a-z0-9.-]+\.[a-z]{2,}"""
    r"""(?:/[^\s"'<>]*)?""",
    re.IGNORECASE,
)
DOMAIN_STOPWORDS = frozenset(
    {
        "adobe.com",
        "astfinancial.com",
        "astproxyportal.com",
        "businesswire.com",
        "calpers-governance.org",
        "capitallink.com",
        "census.gov",
        "cii.org",
        "computershare.com",
        "cstproxy.com",
        "cstproxyvote.com",
        "dfinsolutions.com",
        "edgar-online.com",
        "envisionreports.com",
        "facebook.com",
        "fasb.org",
        "financial.rrd.com",
        "globenewswire.com",
        "google.com",
        "instagram.com",
        "investorvote.com",
        "issuerservices.net",
        "linkedin.com",
        "meetings.computershare.com",
        "meetnow.global",
        "microsoft.com",
        "nasdaq.com",
        "nyse.com",
        "otcmarkets.com",
        "proxyvote.com",
        "proxyvotenow.com",
        "proxydocs.com",
        "proxypush.com",
        "prnewswire.com",
        "rrd.com",
        "rrdonnelley.com",
        "schema.org",
        "sec.gov",
        "shareowneronline.com",
        "taxonomies.xbrl.us",
        "twitter.com",
        "virtualshareholdermeeting.com",
        "voteproxy.com",
        "w3.org",
        "xbrl.ifrs.org",
        "xbrl.org",
        "xbrl.us",
        "youtube.com",
    }
)
NAME_STOPWORDS = frozenset(
    {
        "air",
        "airlines",
        "company",
        "corp",
        "corporation",
        "group",
        "holding",
        "holdings",
        "inc",
        "limited",
        "logistics",
        "plc",
        "services",
        "shipping",
        "transport",
        "transportation",
    }
)


def normalized_domain(raw_url: str) -> str:
    text = str(raw_url or "").strip()
    if not text:
        return ""
    candidate = (
        text if text.lower().startswith(("http://", "https://")) else "https://" + text
    )
    host = (urlparse(candidate).hostname or "").lower().rstrip(".")
    return host.removeprefix("www.")


def is_excluded_domain(domain: str) -> bool:
    normalized = domain.lower().rstrip(".")
    return any(
        normalized == blocked or normalized.endswith("." + blocked)
        for blocked in DOMAIN_STOPWORDS
    )


def extract_domain_counts(text: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for raw in URL_PATTERN.findall(text):
        domain = normalized_domain(raw)
        if (
            domain
            and "." in domain
            and not is_excluded_domain(domain)
        ):
            counts[domain] += 1
    return counts


def company_name_tokens(company_name: str) -> tuple[str, ...]:
    tokens = re.findall(r"[a-z0-9]+", company_name.lower())
    return tuple(
        dict.fromkeys(
            token
            for token in tokens
            if len(token) >= 4 and token not in NAME_STOPWORDS
        )
    )


def select_issuer_domain(
    *,
    ticker: str,
    company_name: str,
    counts: Mapping[str, int],
) -> tuple[str, int, bool, float]:
    tokens = company_name_tokens(company_name)
    ticker_token = ticker.lower()
    ranked: list[tuple[float, int, str, bool]] = []
    for domain, raw_count in counts.items():
        if not domain or is_excluded_domain(domain):
            continue
        count = int(raw_count)
        token_match = any(token in domain for token in tokens)
        ticker_match = len(ticker_token) >= 3 and ticker_token in domain
        investor_signal = (
            domain.startswith(("ir.", "investor.", "investors."))
            or "investor" in domain
        )
        score = (
            min(math.log1p(max(count, 0)) * 10.0, 45.0)
            + (55.0 if token_match else 0.0)
            + (20.0 if ticker_match else 0.0)
            + (20.0 if investor_signal else 0.0)
        )
        ranked.append(
            (score, count, domain, token_match or ticker_match)
        )
    if not ranked:
        return "", 0, False, 0.0
    score, count, domain, name_match = max(
        ranked,
        key=lambda item: (item[0], item[1], item[2]),
    )
    confidence = min(
        0.95,
        0.45
        + (0.25 if name_match else 0.0)
        + (0.1 if count >= 3 else 0.0)
        + (0.05 if score >= 80 else 0.0),
    )
    return domain, count, name_match, confidence


def read_domain_counts(
    paths: Sequence[Path],
    *,
    max_documents: int = 30,
    max_bytes_per_document: int = 8_000_000,
) -> tuple[Counter[str], int]:
    counts: Counter[str] = Counter()
    opened = 0
    for path in paths[:max_documents]:
        if path.suffix.lower() == ".pdf" or not path.is_file():
            continue
        try:
            with path.open("rb") as handle:
                payload = handle.read(max_bytes_per_document)
        except OSError:
            continue
        opened += 1
        counts.update(
            extract_domain_counts(
                payload.decode("utf-8", errors="ignore")
            )
        )
    return counts, opened


def endpoint_id(*, ticker: str, discovery_url: str) -> str:
    digest = hashlib.sha256(
        f"{NON_SEC_ENDPOINT_VERSION}|{ticker}|{discovery_url}".encode(
            "utf-8"
        )
    ).hexdigest()[:20]
    return f"trnsep_{ticker.lower()}_{digest}"


def archived_discovery_url(
    *,
    domain: str,
    start_year: int,
    end_year: int,
) -> str:
    target = quote(f"{domain}/*", safe="")
    return (
        "https://web.archive.org/cdx/search/cdx"
        f"?url={target}&output=json&filter=statuscode:200"
        "&filter=mimetype:text/html&collapse=digest"
        f"&from={start_year}&to={end_year}"
    )


def build_endpoint_rows(
    *,
    residual_rows: Sequence[Mapping[str, str]],
    issuers: Mapping[str, Mapping[str, str]],
    profile_websites: Mapping[str, str],
    inferred_domains: Mapping[
        str,
        tuple[str, int, bool, float],
    ],
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[str],
]:
    eligible = [
        row
        for row in residual_rows
        if str(row.get("retrieval_eligible") or "") == "1"
    ]
    by_ticker: dict[str, list[Mapping[str, str]]] = {}
    for row in eligible:
        by_ticker.setdefault(row["ticker"].upper(), []).append(row)
    endpoints: list[dict[str, object]] = []
    pair_map: list[dict[str, object]] = []
    errors: list[str] = []
    endpoint_by_ticker: dict[str, dict[str, object]] = {}
    for ticker, pairs in sorted(by_ticker.items()):
        issuer = issuers.get(ticker, {})
        company_name = str(issuer.get("company_name") or ticker)
        universe_role = str(
            pairs[0].get("universe_role") or ""
        )
        profile_url = str(profile_websites.get(ticker) or "")
        if profile_url:
            domain = normalized_domain(profile_url)
            source_basis = "CACHED_YAHOO_ISSUER_PROFILE_WEBSITE"
            source_id = "yahoo_profile_cache"
            observation_count = 1
            name_match = bool(
                any(
                    token in domain
                    for token in company_name_tokens(company_name)
                )
            )
            confidence = 0.9 if universe_role == "active" else 0.7
        else:
            (
                domain,
                observation_count,
                name_match,
                confidence,
            ) = inferred_domains.get(ticker, ("", 0, False, 0.0))
            source_basis = "ISSUER_DOMAIN_INFERRED_FROM_SEC_LINKS"
            source_id = "sec_cached_document_link_inventory"
        if not domain or is_excluded_domain(domain):
            errors.append(f"{ticker}: no issuer-owned discovery domain")
            continue
        if universe_role == "active":
            endpoint_type = "ISSUER_WEBSITE_DISCOVERY_ROOT"
            discovery_url = f"https://{domain}/"
            freshness = (
                "current"
                if source_basis
                == "CACHED_YAHOO_ISSUER_PROFILE_WEBSITE"
                else "unknown"
            )
        else:
            endpoint_type = "ARCHIVED_ISSUER_WEBSITE_CDX_ROOT"
            start_year = int(
                str(issuer.get("start_date") or "2000")[:4]
            )
            raw_end = str(issuer.get("end_date") or "2026")[:4]
            end_year = int(raw_end) + 1
            discovery_url = archived_discovery_url(
                domain=domain,
                start_year=start_year,
                end_year=end_year,
            )
            freshness = "acceptable_for_period"
        endpoint_key = endpoint_id(
            ticker=ticker,
            discovery_url=discovery_url,
        )
        metric_ids = sorted(
            {str(row["metric_id"]) for row in pairs}
        )
        lane_ids = sorted(
            {
                lane
                for row in pairs
                for lane in str(
                    row.get("candidate_source_lane_ids") or ""
                ).split("|")
                if lane
            }
        )
        endpoint = {
            "endpoint_version": NON_SEC_ENDPOINT_VERSION,
            "endpoint_id": endpoint_key,
            "ticker": ticker,
            "company_name": company_name,
            "universe_role": universe_role,
            "calibration_cohort": (
                pairs[0].get("calibration_cohort") or ""
            ),
            "endpoint_type": endpoint_type,
            "discovery_url": discovery_url,
            "approved_domain": (
                "web.archive.org"
                if endpoint_type.startswith("ARCHIVED_")
                else domain
            ),
            "target_domain": domain,
            "source_basis": source_basis,
            "source_id": source_id,
            "freshness_status": freshness,
            "confidence": f"{confidence:.2f}",
            "domain_observation_count": observation_count,
            "domain_name_token_match": int(name_match),
            "metric_pair_count": len(pairs),
            "metric_ids": "|".join(metric_ids),
            "candidate_source_lane_ids": "|".join(lane_ids),
            "endpoint_status": "SEALED_DISCOVERY_ROOT",
            "retrieval_authorized": 0,
        }
        endpoints.append(endpoint)
        endpoint_by_ticker[ticker] = endpoint
    for row in sorted(
        eligible,
        key=lambda item: (item["ticker"], item["metric_id"]),
    ):
        ticker = row["ticker"].upper()
        endpoint = endpoint_by_ticker.get(ticker)
        if endpoint is None:
            continue
        pair_map.append(
            {
                "endpoint_version": NON_SEC_ENDPOINT_VERSION,
                "pair_key": row["pair_key"],
                "ticker": ticker,
                "metric_id": row["metric_id"],
                "coverage_status": row["coverage_status"],
                "required_action": row["required_action"],
                "endpoint_id": endpoint["endpoint_id"],
                "endpoint_type": endpoint["endpoint_type"],
                "discovery_url": endpoint["discovery_url"],
                "approved_domain": endpoint["approved_domain"],
                "target_domain": endpoint["target_domain"],
                "candidate_source_lane_ids": row[
                    "candidate_source_lane_ids"
                ],
                "search_aliases": row["search_aliases"],
                "endpoint_status": endpoint["endpoint_status"],
                "retrieval_authorized": 0,
            }
        )
    if len(pair_map) != len(eligible):
        errors.append(
            f"mapped pair count={len(pair_map)} expected={len(eligible)}"
        )
    return endpoints, pair_map, errors

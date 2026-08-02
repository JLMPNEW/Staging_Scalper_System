#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.https import verified_https_context  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_OUTPUT = Path("../output/technology_reports/sector_overlays/stage6b_source_smoke_test.csv")
DEFAULT_USER_AGENT = "JL, Independent Research, jm.357@hotmail.com"

FIELDNAMES = [
    "source_id",
    "overlay_component",
    "source_name",
    "url",
    "status",
    "http_status",
    "auth_required",
    "key_required",
    "latest_date",
    "sample_rows",
    "parse_status",
    "recommended_gate_status",
    "notes",
]

BIG_TECH_CAPEX_CIKS = {
    "MSFT": "0000789019",
    "AMZN": "0001018724",
    "GOOGL": "0001652044",
    "META": "0001326801",
    "ORCL": "0001341439",
}

CAPEX_CONCEPTS = (
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
    "PaymentsToAcquireBusinessesNetOfCashAcquired",
)


@dataclass(frozen=True)
class FetchResult:
    url: str
    ok: bool
    http_status: int | None
    body: bytes
    headers: dict[str, str]
    error: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke test Stage 6B semiconductor overlay data sources.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--timeout-sec", type=float, default=20.0)
    parser.add_argument("--max-bytes", type=int, default=20_000_000)
    parser.add_argument("--sleep-sec", type=float, default=0.15)
    return parser.parse_args()


def text_preview(body: bytes, limit: int = 200_000) -> str:
    raw = body[:limit]
    for encoding in ("utf-8", "windows-1252", "latin-1"):
        try:
            return raw.decode(encoding, errors="ignore")
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def fetch_url(
    url: str,
    *,
    user_agent: str,
    timeout_sec: float,
    max_bytes: int,
    method: str = "GET",
    payload: bytes | None = None,
    content_type: str | None = None,
) -> FetchResult:
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/html,*/*",
    }
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(  # noqa: S310
            request,
            timeout=timeout_sec,
            context=verified_https_context(),
        ) as response:
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                body = body[:max_bytes]
            return FetchResult(
                url=url,
                ok=200 <= int(response.status) < 300,
                http_status=int(response.status),
                body=body,
                headers={str(k): str(v) for k, v in response.headers.items()},
                error="",
            )
    except urllib.error.HTTPError as exc:
        body = exc.read(min(max_bytes, 200_000))
        return FetchResult(
            url=url,
            ok=False,
            http_status=int(exc.code),
            body=body,
            headers={str(k): str(v) for k, v in exc.headers.items()},
            error=f"HTTPError:{exc.code}",
        )
    except Exception as exc:
        return FetchResult(url=url, ok=False, http_status=None, body=b"", headers={}, error=f"{type(exc).__name__}:{exc}")


def make_row(
    *,
    source_id: str,
    overlay_component: str,
    source_name: str,
    url: str,
    result: FetchResult | None,
    status: str,
    auth_required: bool,
    key_required: bool,
    parse_status: str,
    recommended_gate_status: str,
    notes: str,
    latest_date: str = "",
    sample_rows: int = 0,
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "overlay_component": overlay_component,
        "source_name": source_name,
        "url": url,
        "status": status,
        "http_status": result.http_status if result is not None else "",
        "auth_required": int(auth_required),
        "key_required": int(key_required),
        "latest_date": latest_date,
        "sample_rows": sample_rows,
        "parse_status": parse_status,
        "recommended_gate_status": recommended_gate_status,
        "notes": notes,
    }


def extract_links(html: str, base_url: str) -> list[str]:
    links: list[str] = []
    for match in re.finditer(r"""href\s*=\s*["']([^"']+)["']""", html, flags=re.IGNORECASE):
        href = match.group(1).strip()
        if not href or href.startswith("#") or href.lower().startswith(("mailto:", "tel:", "javascript:")):
            continue
        links.append(urllib.parse.urljoin(base_url, href))
    return links


MONTH_NUMBERS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def parse_year_month(candidate: str) -> tuple[int, int] | None:
    text = candidate.strip()
    numeric = re.fullmatch(r"((?:20|19)\d{2})-(\d{2})", text)
    if numeric:
        return int(numeric.group(1)), int(numeric.group(2))
    parts = re.split(r"\s+", text)
    if len(parts) == 2 and parts[0].lower() in MONTH_NUMBERS:
        return int(parts[1]), MONTH_NUMBERS[parts[0].lower()]
    return None


def latest_year_month(text: str) -> str:
    candidates: list[str] = []
    month_names = "January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
    for match in re.finditer(rf"\b({month_names})\s+((?:20|19)\d{{2}})\b", text, flags=re.IGNORECASE):
        candidates.append(f"{match.group(1)} {match.group(2)}")
    for match in re.finditer(rf"\b({month_names})[-_]((?:20|19)\d{{2}})\b", text, flags=re.IGNORECASE):
        candidates.append(f"{match.group(1)} {match.group(2)}")
    for match in re.finditer(r"\b((?:20|19)\d{2})[-/](0[1-9]|1[0-2])\b", text):
        candidates.append(f"{match.group(1)}-{match.group(2)}")
    # Matches arrive in document order, not chronological order, so pick the
    # maximum by parsed (year, month) rather than the last regex hit.
    dated = [(parsed, candidate) for parsed, candidate in ((parse_year_month(candidate), candidate) for candidate in candidates) if parsed is not None]
    if not dated:
        return ""
    return max(dated, key=lambda item: item[0])[1]


def parse_xlsx_smoke(body: bytes) -> tuple[int, str, str]:
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            worksheet_names = [name for name in archive.namelist() if name.startswith("xl/worksheets/") and name.endswith(".xml")]
            shared_text = ""
            if "xl/sharedStrings.xml" in archive.namelist():
                shared_text = archive.read("xl/sharedStrings.xml").decode("utf-8", errors="ignore")
            sheet_text = ""
            for name in worksheet_names[:2]:
                sheet_text += archive.read(name).decode("utf-8", errors="ignore")
            row_count = len(re.findall(r"<row\b", sheet_text))
            latest = latest_year_month(shared_text + "\n" + sheet_text)
            return row_count, latest, "xlsx_parsed"
    except Exception as exc:
        return 0, "", f"xlsx_parse_failed:{type(exc).__name__}"
    return 0, "", "xlsx_no_worksheets"


def smoke_wsts(user_agent: str, timeout_sec: float, max_bytes: int) -> list[dict[str, Any]]:
    candidates = [
        "https://www.wsts.org/67/Historical-Billings-Report",
        "https://www.wsts.org/",
    ]
    rows: list[dict[str, Any]] = []
    best: FetchResult | None = None
    for url in candidates:
        result = fetch_url(url, user_agent=user_agent, timeout_sec=timeout_sec, max_bytes=max_bytes)
        if result.ok or best is None:
            best = result
        if result.ok:
            break
    if best is None:
        return rows
    html = text_preview(best.body)
    links = extract_links(html, best.url)
    data_links = [
        link
        for link in links
        if any(token in link.lower() for token in ("billings", "wsts", "historical"))
        and any(ext in link.lower() for ext in (".xlsx", ".xls", ".csv", ".zip"))
    ]
    if data_links:
        data_url = data_links[0]
        data_result = fetch_url(data_url, user_agent=user_agent, timeout_sec=timeout_sec, max_bytes=max_bytes)
        sample_rows = 0
        latest = ""
        parse_status = "download_failed"
        if data_result.ok and data_url.lower().endswith(".xlsx"):
            sample_rows, latest, parse_status = parse_xlsx_smoke(data_result.body)
            latest = latest or latest_year_month(data_url)
        elif data_result.ok:
            text = text_preview(data_result.body)
            sample_rows = max(0, len(text.splitlines()) - 1)
            latest = latest_year_month(text) or latest_year_month(data_url)
            parse_status = "text_or_csv_downloaded"
        rows.append(
            make_row(
                source_id="wsts_historical_billings",
                overlay_component="sector_cycle",
                source_name="WSTS historical billings",
                url=data_url,
                result=data_result,
                status="success" if data_result.ok else "review",
                auth_required=False,
                key_required=False,
                latest_date=latest,
                sample_rows=sample_rows,
                parse_status=parse_status,
                recommended_gate_status="required_free" if sample_rows else "manual_review",
                notes="Discovered a likely structured WSTS historical-billings file from the landing page.",
            )
        )
        return rows
    rows.append(
        make_row(
            source_id="wsts_historical_billings",
            overlay_component="sector_cycle",
            source_name="WSTS historical billings landing page",
            url=best.url,
            result=best,
            status="review" if best.ok else "failed",
            auth_required=False,
            key_required=False,
            latest_date=latest_year_month(html),
            sample_rows=0,
            parse_status="landing_page_reachable_no_structured_link_found" if best.ok else best.error,
            recommended_gate_status="manual_review",
            notes="Landing page probe did not discover a direct XLS/CSV/ZIP link. Loader may need a maintained direct URL or manual file drop.",
        )
    )
    return rows


def smoke_public_page(
    *,
    source_id: str,
    overlay_component: str,
    source_name: str,
    url: str,
    user_agent: str,
    timeout_sec: float,
    max_bytes: int,
    keywords: tuple[str, ...],
    likely_paid: bool = False,
) -> dict[str, Any]:
    result = fetch_url(url, user_agent=user_agent, timeout_sec=timeout_sec, max_bytes=max_bytes)
    text = text_preview(result.body)
    found = [keyword for keyword in keywords if keyword.lower() in text.lower()]
    status = "success" if result.ok and found else "review" if result.ok else "failed"
    recommended = "optional_paid_or_manual" if likely_paid else "optional_text_overlay"
    return make_row(
        source_id=source_id,
        overlay_component=overlay_component,
        source_name=source_name,
        url=url,
        result=result,
        status=status,
        auth_required=False,
        key_required=False,
        latest_date=latest_year_month(text),
        sample_rows=len(found),
        parse_status="page_keyword_probe" if result.ok else result.error,
        recommended_gate_status=recommended,
        notes=f"Matched keywords={found}. {'Likely paid/licensed for structured history.' if likely_paid else 'Public page looks text-oriented; use as commentary/text signal unless structured feed is found.'}",
    )


def smoke_sec_big_tech_capex(user_agent: str, timeout_sec: float, max_bytes: int, sleep_sec: float) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ticker, cik in BIG_TECH_CAPEX_CIKS.items():
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        result = fetch_url(url, user_agent=user_agent, timeout_sec=timeout_sec, max_bytes=max_bytes)
        sample_rows = 0
        latest = ""
        parse_status = result.error or "download_failed"
        notes = ""
        if result.ok:
            try:
                payload = json.loads(result.body.decode("utf-8"))
                facts = payload.get("facts", {}).get("us-gaap", {})
                dates: list[str] = []
                for concept in CAPEX_CONCEPTS:
                    units = facts.get(concept, {}).get("units", {})
                    for unit_rows in units.values():
                        if isinstance(unit_rows, list):
                            sample_rows += len(unit_rows)
                            dates.extend(str(row.get("filed") or row.get("end") or "") for row in unit_rows if isinstance(row, dict))
                latest = max([value[:10] for value in dates if value[:10]], default="")
                parse_status = "companyfacts_capex_concepts_found" if sample_rows else "companyfacts_no_capex_concepts_found"
                notes = f"{ticker} capex concept rows={sample_rows}"
            except Exception as exc:
                parse_status = f"json_parse_failed:{type(exc).__name__}"
                notes = str(exc)
        rows.append(
            make_row(
                source_id=f"sec_big_tech_capex_{ticker.lower()}",
                overlay_component="big_tech_capex",
                source_name=f"SEC companyfacts capex proxy: {ticker}",
                url=url,
                result=result,
                status="success" if result.ok and sample_rows else "review" if result.ok else "failed",
                auth_required=False,
                key_required=False,
                latest_date=latest,
                sample_rows=sample_rows,
                parse_status=parse_status,
                recommended_gate_status="required_free" if sample_rows else "manual_review",
                notes=notes,
            )
        )
        time.sleep(sleep_sec)
    return rows


def smoke_openalex(user_agent: str, timeout_sec: float, max_bytes: int) -> dict[str, Any]:
    url = "https://api.openalex.org/works?search=semiconductor&per-page=1"
    result = fetch_url(url, user_agent=user_agent, timeout_sec=timeout_sec, max_bytes=max_bytes)
    sample_rows = 0
    parse_status = result.error or "download_failed"
    notes = ""
    if result.ok:
        try:
            payload = json.loads(result.body.decode("utf-8"))
            sample_rows = len(payload.get("results", []))
            parse_status = "json_parsed"
            notes = f"meta_count={payload.get('meta', {}).get('count', '')}"
        except Exception as exc:
            parse_status = f"json_parse_failed:{type(exc).__name__}"
            notes = str(exc)
    key_required = bool(result.http_status in {401, 403})
    return make_row(
        source_id="openalex_semiconductor_research",
        overlay_component="innovation",
        source_name="OpenAlex semiconductor research search",
        url=url,
        result=result,
        status="success" if result.ok and sample_rows else "review" if result.ok else "failed",
        auth_required=False,
        key_required=key_required,
        sample_rows=sample_rows,
        parse_status=parse_status,
        recommended_gate_status="optional_free_api" if result.ok else "manual_review",
        notes=notes,
    )


def smoke_github(user_agent: str, timeout_sec: float, max_bytes: int) -> dict[str, Any]:
    url = "https://api.github.com/search/repositories?q=semiconductor+stars:%3E100&per_page=1"
    result = fetch_url(url, user_agent=user_agent, timeout_sec=timeout_sec, max_bytes=max_bytes)
    sample_rows = 0
    parse_status = result.error or "download_failed"
    notes = ""
    if result.ok:
        try:
            payload = json.loads(result.body.decode("utf-8"))
            sample_rows = len(payload.get("items", []))
            parse_status = "json_parsed"
            notes = f"total_count={payload.get('total_count', '')}; rate_remaining={result.headers.get('X-RateLimit-Remaining', '')}"
        except Exception as exc:
            parse_status = f"json_parse_failed:{type(exc).__name__}"
            notes = str(exc)
    return make_row(
        source_id="github_semiconductor_repos",
        overlay_component="innovation",
        source_name="GitHub repository search",
        url=url,
        result=result,
        status="success" if result.ok and sample_rows else "review" if result.ok else "failed",
        auth_required=False,
        key_required=False,
        sample_rows=sample_rows,
        parse_status=parse_status,
        recommended_gate_status="optional_token_recommended",
        notes=notes,
    )


def smoke_huggingface(user_agent: str, timeout_sec: float, max_bytes: int) -> dict[str, Any]:
    url = "https://huggingface.co/api/models?search=semiconductor&limit=1"
    result = fetch_url(url, user_agent=user_agent, timeout_sec=timeout_sec, max_bytes=max_bytes)
    sample_rows = 0
    parse_status = result.error or "download_failed"
    notes = ""
    if result.ok:
        try:
            payload = json.loads(result.body.decode("utf-8"))
            sample_rows = len(payload) if isinstance(payload, list) else 0
            parse_status = "json_parsed"
        except Exception as exc:
            parse_status = f"json_parse_failed:{type(exc).__name__}"
            notes = str(exc)
    return make_row(
        source_id="huggingface_semiconductor_models",
        overlay_component="innovation",
        source_name="Hugging Face model search",
        url=url,
        result=result,
        status="success" if result.ok else "failed",
        auth_required=False,
        key_required=False,
        sample_rows=sample_rows,
        parse_status=parse_status,
        recommended_gate_status="optional_free_api",
        notes=notes,
    )


def smoke_patentsview(user_agent: str, timeout_sec: float, max_bytes: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    docs_url = "https://patentsview.org/apis/api-endpoints"
    docs_result = fetch_url(docs_url, user_agent=user_agent, timeout_sec=timeout_sec, max_bytes=max_bytes)
    docs_text = text_preview(docs_result.body)
    rows.append(
        make_row(
            source_id="patentsview_docs",
            overlay_component="innovation",
            source_name="PatentsView API documentation",
            url=docs_url,
            result=docs_result,
            status="success" if docs_result.ok else "failed",
            auth_required=False,
            key_required=False,
            sample_rows=1 if "api" in docs_text.lower() else 0,
            parse_status="docs_page_probe" if docs_result.ok else docs_result.error,
            recommended_gate_status="manual_review",
            notes="Documentation page probe only; API endpoint probe follows.",
        )
    )
    api_url = "https://api.patentsview.org/patents/query"
    payload = json.dumps(
        {
            "q": {"_text_any": {"patent_title": "semiconductor"}},
            "f": ["patent_id", "patent_date", "patent_title"],
            "o": {"per_page": 1},
        }
    ).encode("utf-8")
    api_result = fetch_url(
        api_url,
        user_agent=user_agent,
        timeout_sec=timeout_sec,
        max_bytes=max_bytes,
        method="POST",
        payload=payload,
        content_type="application/json",
    )
    sample_rows = 0
    parse_status = api_result.error or "download_failed"
    notes = ""
    if api_result.ok:
        try:
            parsed = json.loads(api_result.body.decode("utf-8"))
            patents = parsed.get("patents", []) if isinstance(parsed, dict) else []
            sample_rows = len(patents)
            parse_status = "json_parsed"
            notes = f"count={parsed.get('count', '')}" if isinstance(parsed, dict) else ""
        except Exception as exc:
            parse_status = f"json_parse_failed:{type(exc).__name__}"
            notes = str(exc)
    body_text = text_preview(api_result.body, limit=2000)
    key_required = bool(api_result.http_status in {401, 403} or "key" in body_text.lower())
    rows.append(
        make_row(
            source_id="patentsview_patent_query",
            overlay_component="innovation",
            source_name="PatentsView patent query",
            url=api_url,
            result=api_result,
            status="success" if api_result.ok and sample_rows else "review" if api_result.ok else "failed",
            auth_required=key_required,
            key_required=key_required,
            sample_rows=sample_rows,
            parse_status=parse_status,
            recommended_gate_status="optional_keyed_api" if key_required else "optional_free_api",
            notes=notes or body_text[:180].replace("\n", " "),
        )
    )
    return rows


def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(cfg_get(config, "semiconductor_sector_overlays.smoke_test_output_csv", DEFAULT_OUTPUT), base_dir=base_dir)
    )
    user_agent = str(cfg_get(config, "sec_fundamentals.user_agent", DEFAULT_USER_AGENT) or DEFAULT_USER_AGENT)
    rows: list[dict[str, Any]] = []
    rows.extend(smoke_wsts(user_agent, args.timeout_sec, args.max_bytes))
    time.sleep(args.sleep_sec)
    rows.append(
        smoke_public_page(
            source_id="sia_monthly_sales_releases",
            overlay_component="sector_cycle",
            source_name="SIA public news and sales releases",
            url="https://www.semiconductors.org/news-events/news/",
            user_agent=user_agent,
            timeout_sec=args.timeout_sec,
            max_bytes=args.max_bytes,
            keywords=("global semiconductor sales", "WSTS", "sales"),
        )
    )
    time.sleep(args.sleep_sec)
    rows.append(
        smoke_public_page(
            source_id="semi_market_intelligence",
            overlay_component="equipment_cycle",
            source_name="SEMI market intelligence and billings products",
            url="https://www.semi.org/en/products-services/market-intelligence",
            user_agent=user_agent,
            timeout_sec=args.timeout_sec,
            max_bytes=args.max_bytes,
            keywords=("billings", "market intelligence", "subscription", "semiconductor equipment"),
            likely_paid=True,
        )
    )
    time.sleep(args.sleep_sec)
    rows.append(
        smoke_public_page(
            source_id="taiwan_trade_statistics",
            overlay_component="memory_ai_proxy",
            source_name="Taiwan trade statistics landing page",
            url="https://portal.sw.nat.gov.tw/APGA/GA03E",
            user_agent=user_agent,
            timeout_sec=args.timeout_sec,
            max_bytes=args.max_bytes,
            keywords=("export", "import", "statistics"),
        )
    )
    time.sleep(args.sleep_sec)
    rows.extend(smoke_sec_big_tech_capex(user_agent, args.timeout_sec, args.max_bytes, args.sleep_sec))
    rows.extend(smoke_patentsview(user_agent, args.timeout_sec, args.max_bytes))
    rows.append(smoke_openalex(user_agent, args.timeout_sec, args.max_bytes))
    rows.append(smoke_github(user_agent, args.timeout_sec, args.max_bytes))
    rows.append(smoke_huggingface(user_agent, args.timeout_sec, args.max_bytes))

    write_report(output_csv, rows)
    print(f"Wrote {len(rows)} Stage 6B smoke-test rows: {output_csv}")
    for row in rows:
        print(
            f"{row['source_id']}: {row['status']} "
            f"http={row['http_status']} parse={row['parse_status']} gate={row['recommended_gate_status']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

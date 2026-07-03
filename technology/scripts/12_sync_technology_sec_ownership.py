#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path
from typing import Any

import requests  # type: ignore[reportMissingModuleSource]


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, expand_env_vars, load_yaml, resolve_path  # noqa: E402
from technology.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from technology.core.logging_utils import configure_utc_logging  # noqa: E402
from technology.core.source_registry import load_source_registry, upsert_source_registry  # noqa: E402
from technology.core.text_norm import normalize_cik, normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("sync_technology_sec_ownership")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
RUN_TYPE = "sync_technology_sec_ownership"
OWNERSHIP_FORMS = {"3", "3/A", "4", "4/A", "5", "5/A"}
HFIA_EFFECTIVE_DEFAULT = date(2026, 3, 18)
CSV_FIELDS = [
    "ticker",
    "issuer_type",
    "section16_expected_status",
    "coverage_status",
    "ownership_filings",
    "nonderivative_transactions",
    "derivative_transactions",
    "form4_compat_transactions",
    "latest_ownership_filing_date",
    "review_reason",
]

EEA_COUNTRIES = {
    "AUSTRIA",
    "BELGIUM",
    "BULGARIA",
    "CROATIA",
    "CYPRUS",
    "CZECH REPUBLIC",
    "CZECHIA",
    "DENMARK",
    "ESTONIA",
    "FINLAND",
    "FRANCE",
    "GERMANY",
    "GREECE",
    "HUNGARY",
    "ICELAND",
    "IRELAND",
    "ITALY",
    "LATVIA",
    "LIECHTENSTEIN",
    "LITHUANIA",
    "LUXEMBOURG",
    "MALTA",
    "NETHERLANDS",
    "NORWAY",
    "POLAND",
    "PORTUGAL",
    "ROMANIA",
    "SLOVAKIA",
    "SLOVENIA",
    "SPAIN",
    "SWEDEN",
}
QUALIFYING_HFIA_COUNTRIES = EEA_COUNTRIES | {
    "CANADA",
    "CHILE",
    "SOUTH KOREA",
    "REPUBLIC OF KOREA",
    "KOREA",
    "SWITZERLAND",
    "UNITED KINGDOM",
    "UK",
    "AUSTRALIA",
    "INDIA",
    "SINGAPORE",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync direct SEC Forms 3/4/5 ownership filings for technology tickers.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--model-family", default="", help="Technology model family to sync, e.g. semiconductors.")
    parser.add_argument("--tickers", default="")
    parser.add_argument("--max-tickers", type=int, default=0)
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--output-csv", type=Path, default=None)
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(text.upper(), fmt).date()
        except ValueError:
            continue
    return None


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def bool_int(raw: object) -> int:
    return int(str(raw or "").strip().lower() in {"1", "true", "yes", "y"})


def cik10(raw: object) -> str:
    return normalize_cik(str(raw or "")).zfill(10)


def local_name(tag: str) -> str:
    return tag.split("}", 1)[-1].lower()


def children(node: ET.Element, name: str) -> list[ET.Element]:
    wanted = name.lower()
    return [child for child in list(node) if local_name(child.tag) == wanted]


def child(node: ET.Element, name: str) -> ET.Element | None:
    found = children(node, name)
    return found[0] if found else None


def text_at(node: ET.Element | None, *path: str) -> str:
    current = node
    for part in path:
        if current is None:
            return ""
        current = child(current, part)
    if current is None or current.text is None:
        return ""
    return str(current.text).strip()


def first_desc(node: ET.Element, name: str) -> ET.Element | None:
    wanted = name.lower()
    for item in node.iter():
        if local_name(item.tag) == wanted:
            return item
    return None


def footnotes_json(node: ET.Element) -> str:
    ids: list[str] = []
    for item in node.iter():
        if local_name(item.tag) == "footnoteid":
            value = item.attrib.get("id") or item.attrib.get("{http://www.w3.org/1999/xlink}href") or ""
            if value:
                ids.append(str(value).lstrip("#"))
    return json.dumps(sorted(set(ids)), ensure_ascii=True)


def request_text(url: str, *, headers: dict[str, str], timeout_sec: float, retries: int, sleep_sec: float) -> tuple[int, str]:
    last_exc: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            response = requests.get(url, headers=headers, timeout=timeout_sec)
            if response.status_code in {429, 500, 502, 503, 504} and attempt + 1 < retries:
                time.sleep(sleep_sec * (attempt + 1))
                continue
            return response.status_code, response.text
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt + 1 < retries:
                time.sleep(sleep_sec * (attempt + 1))
                continue
    raise RuntimeError(f"Request failed for {url}: {last_exc}")


def cached_text(
    url: str,
    cache_path: Path,
    *,
    headers: dict[str, str],
    timeout_sec: float,
    retries: int,
    sleep_sec: float,
    force_refresh: bool,
) -> tuple[int, str, str]:
    if cache_path.exists() and not force_refresh:
        return 200, cache_path.read_text(encoding="utf-8", errors="replace"), "cache"
    status, text = request_text(url, headers=headers, timeout_sec=timeout_sec, retries=retries, sleep_sec=sleep_sec)
    # Only cache successful responses; a cached error body would replay as 200 forever.
    if status == 200:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(text, encoding="utf-8")
    time.sleep(sleep_sec)
    return status, text, "live"


def json_payload(text: str) -> dict[str, Any]:
    return json.loads(text) if text.strip() else {}


def record_raw_response(conn: Any, *, source_id: str, endpoint: str, status: int, text: str, asof: str) -> None:
    now = utc_now()
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    conn.execute(
        """
        INSERT INTO raw_api_responses(
            source_id, endpoint, query_params_json, request_time_utc, response_status,
            response_hash, asof_date, payload_text, ingestion_run_id, created_at
        )
        VALUES (?, ?, '', ?, ?, ?, ?, ?, NULL, ?)
        """,
        (source_id, endpoint, now, int(status), digest, asof, text, now),
    )


def load_universe(conn: Any, ticker_filter: set[str], *, model_family: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT DISTINCT c.ticker, c.cik, c.company_name, c.country,
               COALESCE(p.is_foreign_private_issuer, 0) AS is_fpi
        FROM dim_company c
        JOIN dim_technology_taxonomy t
          ON t.ticker = c.ticker
         AND t.model_family = ?
        LEFT JOIN dim_issuer_reporting_profile p ON p.ticker = c.ticker
        WHERE c.is_active = 1
        ORDER BY c.ticker
        """,
        (model_family,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        if ticker_filter and ticker not in ticker_filter:
            continue
        out.append(
            {
                "ticker": ticker,
                "cik": cik10(row["cik"]),
                "company_name": str(row["company_name"] or ""),
                "country": str(row["country"] or ""),
                "is_fpi": int(row["is_fpi"] or 0),
            }
        )
    return out


def filing_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    recent = payload.get("filings", {}).get("recent", {})
    keys = list(recent.keys())
    count = max((len(recent.get(key) or []) for key in keys), default=0)
    rows: list[dict[str, Any]] = []
    for idx in range(count):
        row: dict[str, Any] = {}
        for key in keys:
            values = recent.get(key) or []
            row[key] = values[idx] if idx < len(values) else ""
        rows.append(row)
    return rows


def archive_file_names(payload: dict[str, Any]) -> list[str]:
    files = payload.get("filings", {}).get("files", [])
    if not isinstance(files, list):
        return []
    return [str(row.get("name") or "") for row in files if isinstance(row, dict) and str(row.get("name") or "").strip()]


def ownership_filings(rows: list[dict[str, Any]], *, start: date, forms: set[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        form = str(row.get("form") or "").strip().upper()
        filing_date = parse_date(row.get("filingDate"))
        accession = str(row.get("accessionNumber") or "").strip()
        primary_document = str(row.get("primaryDocument") or "").strip()
        if form not in forms or filing_date is None or filing_date < start or not accession or not primary_document:
            continue
        if accession in seen:
            continue
        seen.add(accession)
        out.append(
            {
                "accession_number": accession,
                "form_type": form,
                "filing_date": filing_date.isoformat(),
                "report_date": str(row.get("reportDate") or ""),
                "acceptance_datetime": str(row.get("acceptanceDateTime") or ""),
                "primary_document": primary_document,
            }
        )
    out.sort(key=lambda item: (str(item["filing_date"]), str(item["accession_number"])))
    return out


def ownership_xml_root(text: str) -> ET.Element:
    try:
        return ET.fromstring(text.encode("utf-8"))
    except ET.ParseError:
        start = text.find("<ownershipDocument")
        end = text.rfind("</ownershipDocument>")
        if start >= 0 and end >= 0:
            return ET.fromstring(text[start : end + len("</ownershipDocument>")].encode("utf-8"))
        raise


def parse_owner(owner_node: ET.Element) -> dict[str, Any]:
    relationship = child(owner_node, "reportingOwnerRelationship")
    owner_id = child(owner_node, "reportingOwnerId")
    return {
        "reporting_owner_cik": cik10(text_at(owner_id, "rptOwnerCik")) if text_at(owner_id, "rptOwnerCik") else "",
        "reporting_owner_name": text_at(owner_id, "rptOwnerName"),
        "reporting_owner_relationship": "director" if bool_int(text_at(relationship, "isDirector")) else ("officer" if bool_int(text_at(relationship, "isOfficer")) else ""),
        "reporting_owner_title": text_at(relationship, "officerTitle"),
        "is_director": bool_int(text_at(relationship, "isDirector")),
        "is_officer": bool_int(text_at(relationship, "isOfficer")),
        "is_ten_percent_owner": bool_int(text_at(relationship, "isTenPercentOwner")),
    }


def parse_nonderivative_transaction(tx: ET.Element, seq: int) -> dict[str, Any]:
    coding = child(tx, "transactionCoding")
    amounts = child(tx, "transactionAmounts")
    post = child(tx, "postTransactionAmounts")
    ownership = child(tx, "ownershipNature")
    shares = to_float(text_at(amounts, "transactionShares", "value"))
    price = to_float(text_at(amounts, "transactionPricePerShare", "value"))
    return {
        "transaction_seq": seq,
        "security_title": text_at(tx, "securityTitle", "value"),
        "transaction_date": text_at(tx, "transactionDate", "value"),
        "deemed_execution_date": text_at(tx, "deemedExecutionDate", "value"),
        "transaction_code": text_at(coding, "transactionCode").upper(),
        "equity_swap_involved": bool_int(text_at(coding, "equitySwapInvolved")),
        "transaction_shares": shares,
        "transaction_price_per_share": price,
        "transaction_value": shares * price if shares is not None and price is not None else None,
        "acquired_disposed_code": text_at(amounts, "transactionAcquiredDisposedCode", "value").upper(),
        "shares_owned_following_transaction": to_float(text_at(post, "sharesOwnedFollowingTransaction", "value")),
        "direct_or_indirect_ownership": text_at(ownership, "directOrIndirectOwnership", "value").upper(),
        "nature_of_ownership": text_at(ownership, "natureOfOwnership", "value"),
        "footnotes_json": footnotes_json(tx),
    }


def parse_derivative_transaction(tx: ET.Element, seq: int) -> dict[str, Any]:
    coding = child(tx, "transactionCoding")
    amounts = child(tx, "transactionAmounts")
    post = child(tx, "postTransactionAmounts")
    ownership = child(tx, "ownershipNature")
    underlying = child(tx, "underlyingSecurity")
    shares = to_float(text_at(amounts, "transactionShares", "value"))
    price = to_float(text_at(amounts, "transactionPricePerShare", "value"))
    return {
        "transaction_seq": seq,
        "security_title": text_at(tx, "securityTitle", "value"),
        "conversion_or_exercise_price": to_float(text_at(tx, "conversionOrExercisePrice", "value")),
        "transaction_date": text_at(tx, "transactionDate", "value"),
        "deemed_execution_date": text_at(tx, "deemedExecutionDate", "value"),
        "transaction_code": text_at(coding, "transactionCode").upper(),
        "equity_swap_involved": bool_int(text_at(coding, "equitySwapInvolved")),
        "transaction_shares": shares,
        "transaction_price_per_share": price,
        "transaction_value": shares * price if shares is not None and price is not None else None,
        "acquired_disposed_code": text_at(amounts, "transactionAcquiredDisposedCode", "value").upper(),
        "exercise_date": text_at(tx, "exerciseDate", "value"),
        "expiration_date": text_at(tx, "expirationDate", "value"),
        "underlying_security_title": text_at(underlying, "underlyingSecurityTitle", "value"),
        "underlying_security_shares": to_float(text_at(underlying, "underlyingSecurityShares", "value")),
        "shares_owned_following_transaction": to_float(text_at(post, "sharesOwnedFollowingTransaction", "value")),
        "direct_or_indirect_ownership": text_at(ownership, "directOrIndirectOwnership", "value").upper(),
        "nature_of_ownership": text_at(ownership, "natureOfOwnership", "value"),
        "footnotes_json": footnotes_json(tx),
    }


def parse_holding(node: ET.Element, seq: int, holding_type: str) -> dict[str, Any]:
    ownership = child(node, "ownershipNature")
    underlying = child(node, "underlyingSecurity")
    return {
        "holding_type": holding_type,
        "holding_seq": seq,
        "security_title": text_at(node, "securityTitle", "value"),
        "conversion_or_exercise_price": to_float(text_at(node, "conversionOrExercisePrice", "value")),
        "exercise_date": text_at(node, "exerciseDate", "value"),
        "expiration_date": text_at(node, "expirationDate", "value"),
        "ownership_shares": to_float(text_at(node, "sharesOwnedFollowingTransaction", "value") or text_at(node, "postTransactionAmounts", "sharesOwnedFollowingTransaction", "value")),
        "underlying_security_title": text_at(underlying, "underlyingSecurityTitle", "value"),
        "underlying_security_shares": to_float(text_at(underlying, "underlyingSecurityShares", "value")),
        "direct_or_indirect_ownership": text_at(ownership, "directOrIndirectOwnership", "value").upper(),
        "nature_of_ownership": text_at(ownership, "natureOfOwnership", "value"),
        "footnotes_json": footnotes_json(node),
    }


def parse_ownership_document(text: str) -> dict[str, Any]:
    root = ownership_xml_root(text)
    issuer = child(root, "issuer")
    nonderiv_table = child(root, "nonDerivativeTable")
    deriv_table = child(root, "derivativeTable")
    owners = [parse_owner(node) for node in children(root, "reportingOwner")]
    if not owners:
        owners = [
            {
                "reporting_owner_cik": "",
                "reporting_owner_name": "",
                "reporting_owner_relationship": "",
                "reporting_owner_title": "",
                "is_director": 0,
                "is_officer": 0,
                "is_ten_percent_owner": 0,
            }
        ]
    nonderiv_transactions = [
        parse_nonderivative_transaction(node, idx)
        for idx, node in enumerate(children(nonderiv_table, "nonDerivativeTransaction") if nonderiv_table is not None else [], start=1)
    ]
    deriv_transactions = [
        parse_derivative_transaction(node, idx)
        for idx, node in enumerate(children(deriv_table, "derivativeTransaction") if deriv_table is not None else [], start=1)
    ]
    holdings: list[dict[str, Any]] = []
    for idx, node in enumerate(children(nonderiv_table, "nonDerivativeHolding") if nonderiv_table is not None else [], start=1):
        holdings.append(parse_holding(node, idx, "nonderivative"))
    for idx, node in enumerate(children(deriv_table, "derivativeHolding") if deriv_table is not None else [], start=1):
        holdings.append(parse_holding(node, idx, "derivative"))
    return {
        "document_type": text_at(root, "documentType").upper(),
        "period_of_report": text_at(root, "periodOfReport"),
        "issuer_cik": cik10(text_at(issuer, "issuerCik")) if text_at(issuer, "issuerCik") else "",
        "issuer_name": text_at(issuer, "issuerName"),
        "issuer_trading_symbol": normalize_ticker(text_at(issuer, "issuerTradingSymbol")),
        "foreign_trading_symbol": text_at(issuer, "foreignTradingSymbol"),
        "owners": owners,
        "nonderiv_transactions": nonderiv_transactions,
        "deriv_transactions": deriv_transactions,
        "holdings": holdings,
    }


def upsert_filing(
    conn: Any,
    *,
    ticker: str,
    cik: str,
    source_id: str,
    filing: dict[str, Any],
    parsed: dict[str, Any] | None,
    owner: dict[str, Any],
    source_url: str,
    raw_hash: str,
    parse_error: str = "",
) -> None:
    now = utc_now()
    parsed_ok = int(parsed is not None and not parse_error)
    conn.execute(
        """
        INSERT INTO fact_sec_ownership_filing(
            ticker, accession_number, reporting_owner_cik, source_id, cik, issuer_cik,
            issuer_name, issuer_trading_symbol, foreign_trading_symbol, form_type,
            filed_date, accepted_datetime, period_of_report, reporting_owner_name,
            reporting_owner_relationship, reporting_owner_title, is_director, is_officer,
            is_ten_percent_owner, has_nonderivative_transactions, has_derivative_transactions,
            has_holdings, parsed_successfully, parse_error, source_url, raw_xml_hash,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, accession_number, reporting_owner_cik, source_id) DO UPDATE SET
            issuer_cik = excluded.issuer_cik,
            issuer_name = excluded.issuer_name,
            issuer_trading_symbol = excluded.issuer_trading_symbol,
            foreign_trading_symbol = excluded.foreign_trading_symbol,
            form_type = excluded.form_type,
            filed_date = excluded.filed_date,
            accepted_datetime = excluded.accepted_datetime,
            period_of_report = excluded.period_of_report,
            reporting_owner_name = excluded.reporting_owner_name,
            reporting_owner_relationship = excluded.reporting_owner_relationship,
            reporting_owner_title = excluded.reporting_owner_title,
            is_director = excluded.is_director,
            is_officer = excluded.is_officer,
            is_ten_percent_owner = excluded.is_ten_percent_owner,
            has_nonderivative_transactions = excluded.has_nonderivative_transactions,
            has_derivative_transactions = excluded.has_derivative_transactions,
            has_holdings = excluded.has_holdings,
            parsed_successfully = excluded.parsed_successfully,
            parse_error = excluded.parse_error,
            source_url = excluded.source_url,
            raw_xml_hash = excluded.raw_xml_hash,
            updated_at = excluded.updated_at
        """,
        (
            ticker,
            str(filing["accession_number"]),
            str(owner.get("reporting_owner_cik") or ""),
            source_id,
            cik,
            str((parsed or {}).get("issuer_cik") or ""),
            str((parsed or {}).get("issuer_name") or ""),
            str((parsed or {}).get("issuer_trading_symbol") or ticker),
            str((parsed or {}).get("foreign_trading_symbol") or ""),
            str((parsed or {}).get("document_type") or filing["form_type"]),
            str(filing["filing_date"]),
            str(filing.get("acceptance_datetime") or ""),
            str((parsed or {}).get("period_of_report") or filing.get("report_date") or ""),
            str(owner.get("reporting_owner_name") or ""),
            str(owner.get("reporting_owner_relationship") or ""),
            str(owner.get("reporting_owner_title") or ""),
            int(owner.get("is_director") or 0),
            int(owner.get("is_officer") or 0),
            int(owner.get("is_ten_percent_owner") or 0),
            int(bool((parsed or {}).get("nonderiv_transactions"))),
            int(bool((parsed or {}).get("deriv_transactions"))),
            int(bool((parsed or {}).get("holdings"))),
            parsed_ok,
            parse_error,
            source_url,
            raw_hash,
            now,
            now,
        ),
    )


def upsert_nonderiv_transaction(conn: Any, *, ticker: str, source_id: str, accession: str, owner: dict[str, Any], tx: dict[str, Any]) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO fact_sec_ownership_nonderivative_transaction(
            ticker, accession_number, transaction_seq, reporting_owner_cik, source_id,
            security_title, transaction_date, deemed_execution_date, transaction_code,
            equity_swap_involved, transaction_shares, transaction_price_per_share,
            transaction_value, acquired_disposed_code, shares_owned_following_transaction,
            direct_or_indirect_ownership, nature_of_ownership, footnotes_json,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, accession_number, transaction_seq, reporting_owner_cik, source_id) DO UPDATE SET
            security_title = excluded.security_title,
            transaction_date = excluded.transaction_date,
            deemed_execution_date = excluded.deemed_execution_date,
            transaction_code = excluded.transaction_code,
            equity_swap_involved = excluded.equity_swap_involved,
            transaction_shares = excluded.transaction_shares,
            transaction_price_per_share = excluded.transaction_price_per_share,
            transaction_value = excluded.transaction_value,
            acquired_disposed_code = excluded.acquired_disposed_code,
            shares_owned_following_transaction = excluded.shares_owned_following_transaction,
            direct_or_indirect_ownership = excluded.direct_or_indirect_ownership,
            nature_of_ownership = excluded.nature_of_ownership,
            footnotes_json = excluded.footnotes_json,
            updated_at = excluded.updated_at
        """,
        (
            ticker,
            accession,
            int(tx["transaction_seq"]),
            str(owner.get("reporting_owner_cik") or ""),
            source_id,
            tx.get("security_title"),
            tx.get("transaction_date"),
            tx.get("deemed_execution_date"),
            tx.get("transaction_code"),
            tx.get("equity_swap_involved"),
            tx.get("transaction_shares"),
            tx.get("transaction_price_per_share"),
            tx.get("transaction_value"),
            tx.get("acquired_disposed_code"),
            tx.get("shares_owned_following_transaction"),
            tx.get("direct_or_indirect_ownership"),
            tx.get("nature_of_ownership"),
            tx.get("footnotes_json"),
            now,
            now,
        ),
    )


def upsert_deriv_transaction(conn: Any, *, ticker: str, source_id: str, accession: str, owner: dict[str, Any], tx: dict[str, Any]) -> None:
    now = utc_now()
    fields = [
        "ticker", "accession_number", "transaction_seq", "reporting_owner_cik", "source_id",
        "security_title", "conversion_or_exercise_price", "transaction_date",
        "deemed_execution_date", "transaction_code", "equity_swap_involved",
        "transaction_shares", "transaction_price_per_share", "transaction_value",
        "acquired_disposed_code", "exercise_date", "expiration_date",
        "underlying_security_title", "underlying_security_shares",
        "shares_owned_following_transaction", "direct_or_indirect_ownership",
        "nature_of_ownership", "footnotes_json",
    ]
    row = {
        **tx,
        "ticker": ticker,
        "accession_number": accession,
        "reporting_owner_cik": str(owner.get("reporting_owner_cik") or ""),
        "source_id": source_id,
    }
    values = [row.get(field) for field in fields] + [now, now]
    update_clause = ", ".join(f"{field}=excluded.{field}" for field in fields[5:])
    conn.execute(
        f"""
        INSERT INTO fact_sec_ownership_derivative_transaction(
            {", ".join(fields)}, created_at, updated_at
        )
        VALUES ({", ".join("?" for _ in values)})
        ON CONFLICT(ticker, accession_number, transaction_seq, reporting_owner_cik, source_id) DO UPDATE SET
            {update_clause},
            updated_at = excluded.updated_at
        """,
        values,
    )


def upsert_holding(conn: Any, *, ticker: str, source_id: str, accession: str, owner: dict[str, Any], holding: dict[str, Any]) -> None:
    now = utc_now()
    fields = [
        "ticker", "accession_number", "holding_type", "holding_seq", "reporting_owner_cik",
        "source_id", "security_title", "conversion_or_exercise_price", "exercise_date",
        "expiration_date", "ownership_shares", "underlying_security_title",
        "underlying_security_shares", "direct_or_indirect_ownership",
        "nature_of_ownership", "footnotes_json",
    ]
    row = {
        **holding,
        "ticker": ticker,
        "accession_number": accession,
        "reporting_owner_cik": str(owner.get("reporting_owner_cik") or ""),
        "source_id": source_id,
    }
    values = [row.get(field) for field in fields] + [now, now]
    update_clause = ", ".join(f"{field}=excluded.{field}" for field in fields[6:])
    conn.execute(
        f"""
        INSERT INTO fact_sec_ownership_holding(
            {", ".join(fields)}, created_at, updated_at
        )
        VALUES ({", ".join("?" for _ in values)})
        ON CONFLICT(ticker, accession_number, holding_type, holding_seq, reporting_owner_cik, source_id) DO UPDATE SET
            {update_clause},
            updated_at = excluded.updated_at
        """,
        values,
    )


def upsert_form4_compat(
    conn: Any,
    *,
    ticker: str,
    source_id: str,
    filing: dict[str, Any],
    owner: dict[str, Any],
    tx: dict[str, Any],
    period_of_report: str,
) -> None:
    if str(filing["form_type"]).upper() not in {"4", "4/A"}:
        return
    trans_date = parse_date(tx.get("transaction_date")) or parse_date(period_of_report) or parse_date(filing.get("filing_date"))
    if trans_date is None:
        return
    now = utc_now()
    code = str(tx.get("transaction_code") or "").upper()
    acq_disp = str(tx.get("acquired_disposed_code") or "").upper()
    conn.execute(
        """
        INSERT INTO fact_sec_form4_transaction(
            ticker, accession_number, nonderiv_trans_sk, rptowner_cik, source_id,
            filing_date, period_of_report, transaction_date, transaction_code,
            acquired_disposed_code, transaction_shares, transaction_price_per_share,
            transaction_value, shares_owned_following_transaction,
            direct_or_indirect_ownership, reporting_owner_name,
            reporting_owner_relationship, reporting_owner_title, is_director,
            is_officer, is_ten_percent_owner, is_open_market_purchase,
            is_open_market_sale, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, accession_number, nonderiv_trans_sk, rptowner_cik, source_id) DO UPDATE SET
            filing_date = excluded.filing_date,
            period_of_report = excluded.period_of_report,
            transaction_date = excluded.transaction_date,
            transaction_code = excluded.transaction_code,
            acquired_disposed_code = excluded.acquired_disposed_code,
            transaction_shares = excluded.transaction_shares,
            transaction_price_per_share = excluded.transaction_price_per_share,
            transaction_value = excluded.transaction_value,
            shares_owned_following_transaction = excluded.shares_owned_following_transaction,
            direct_or_indirect_ownership = excluded.direct_or_indirect_ownership,
            reporting_owner_name = excluded.reporting_owner_name,
            reporting_owner_relationship = excluded.reporting_owner_relationship,
            reporting_owner_title = excluded.reporting_owner_title,
            is_director = excluded.is_director,
            is_officer = excluded.is_officer,
            is_ten_percent_owner = excluded.is_ten_percent_owner,
            is_open_market_purchase = excluded.is_open_market_purchase,
            is_open_market_sale = excluded.is_open_market_sale,
            updated_at = excluded.updated_at
        """,
        (
            ticker,
            str(filing["accession_number"]),
            f"direct_nonderiv_{int(tx['transaction_seq']):04d}",
            str(owner.get("reporting_owner_cik") or ""),
            source_id,
            str(filing.get("filing_date") or ""),
            period_of_report,
            trans_date.isoformat(),
            code,
            acq_disp,
            tx.get("transaction_shares"),
            tx.get("transaction_price_per_share"),
            tx.get("transaction_value"),
            tx.get("shares_owned_following_transaction"),
            tx.get("direct_or_indirect_ownership"),
            str(owner.get("reporting_owner_name") or ""),
            str(owner.get("reporting_owner_relationship") or ""),
            str(owner.get("reporting_owner_title") or ""),
            int(owner.get("is_director") or 0),
            int(owner.get("is_officer") or 0),
            int(owner.get("is_ten_percent_owner") or 0),
            int(code == "P" and acq_disp in {"", "A"}),
            int(code == "S" and acq_disp in {"", "D"}),
            now,
            now,
        ),
    )


def classify_insider_profile(company: dict[str, Any], hfia_effective_date: date) -> dict[str, Any]:
    country = str(company.get("country") or "").upper()
    is_fpi = int(company.get("is_fpi") or 0) == 1
    if not is_fpi:
        return {
            "issuer_type": "domestic",
            "primary_insider_source": "SEC Forms 3/4/5",
            "section16_expected_status": "SEC_FORM4_EXPECTED_DOMESTIC",
            "fpi_qualifying_exemption_status": "",
            "local_insider_source_required": 0,
        }
    if country in QUALIFYING_HFIA_COUNTRIES:
        local_source = "local insider reporting"
        if country == "CANADA":
            local_source = "SEDI/Sedar+"
        elif country in EEA_COUNTRIES or country in {"UNITED KINGDOM", "UK", "SWITZERLAND"}:
            local_source = "EU/UK/Swiss MAR PDMR reporting"
        return {
            "issuer_type": "foreign_private_issuer",
            "primary_insider_source": local_source,
            "section16_expected_status": "SEC_FORM4_NOT_EXPECTED_FPI_QUALIFYING_EXEMPT",
            "fpi_qualifying_exemption_status": "qualifying_jurisdiction_conditions_review",
            "local_insider_source_required": 1,
        }
    return {
        "issuer_type": "foreign_private_issuer",
        "primary_insider_source": "SEC Forms 3/4/5",
        "section16_expected_status": "SEC_FORM4_EXPECTED_FPI_POST_HFIA",
        "fpi_qualifying_exemption_status": f"not_qualifying_as_of_{hfia_effective_date.isoformat()}",
        "local_insider_source_required": 0,
    }


def add_issue(conn: Any, ticker: str, source_id: str, issue_type: str, detail: str, severity: str = "warning") -> None:
    now = utc_now()
    row = conn.execute("SELECT company_id FROM dim_company WHERE ticker = ?", (ticker,)).fetchone()
    company_id = int(row["company_id"]) if row is not None else None
    conn.execute(
        """
        INSERT INTO data_quality_issues(
            detected_at, severity, stage, ticker, company_id, source_id, issue_type,
            issue_detail, resolution_status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
        """,
        (now, severity, RUN_TYPE, ticker, company_id, source_id, issue_type, detail, now, now),
    )


def upsert_reporting_profile(conn: Any, company: dict[str, Any], *, source_id: str, hfia_effective_date: date) -> dict[str, Any]:
    ticker = str(company["ticker"])
    classification = classify_insider_profile(company, hfia_effective_date)
    latest = conn.execute(
        """
        SELECT form_type, filed_date
        FROM fact_sec_ownership_filing
        WHERE ticker = ? AND source_id = ?
          AND parsed_successfully = 1
        ORDER BY filed_date DESC, accession_number DESC
        LIMIT 1
        """,
        (ticker, source_id),
    ).fetchone()
    filing_count = conn.execute(
        "SELECT COUNT(DISTINCT accession_number) FROM fact_sec_ownership_filing WHERE ticker = ? AND source_id = ? AND parsed_successfully = 1",
        (ticker, source_id),
    ).fetchone()[0]
    nonderiv_count = conn.execute(
        "SELECT COUNT(*) FROM fact_sec_ownership_nonderivative_transaction WHERE ticker = ? AND source_id = ?",
        (ticker, source_id),
    ).fetchone()[0]
    deriv_count = conn.execute(
        "SELECT COUNT(*) FROM fact_sec_ownership_derivative_transaction WHERE ticker = ? AND source_id = ?",
        (ticker, source_id),
    ).fetchone()[0]
    trans_count = int(nonderiv_count or 0) + int(deriv_count or 0)
    parse_failures = conn.execute(
        "SELECT COUNT(*) FROM fact_sec_ownership_filing WHERE ticker = ? AND source_id = ? AND parsed_successfully = 0",
        (ticker, source_id),
    ).fetchone()[0]
    expected = str(classification["section16_expected_status"])
    review_reason = ""
    if filing_count and trans_count:
        coverage_status = "ownership_rows_found"
    elif filing_count:
        coverage_status = "ownership_sec_filings_found_no_transactions"
        review_reason = "Ownership filings exist but no derivative or non-derivative transactions were parsed."
    elif parse_failures:
        coverage_status = "ownership_sec_filings_found_parser_failed"
        review_reason = "Ownership filings were found but XML parsing failed."
    elif expected == "SEC_FORM4_EXPECTED_DOMESTIC":
        coverage_status = "ownership_domestic_expected_missing_review"
        review_reason = "Domestic issuer has no direct SEC Forms 3/4/5 rows in the configured window."
    elif expected == "SEC_FORM4_EXPECTED_FPI_POST_HFIA":
        coverage_status = "ownership_fpi_post_hfia_expected_direct_sec_not_found"
        review_reason = "FPI is not in a known qualifying exemption jurisdiction; monitor direct SEC Forms 3/4/5 after HFIA."
    elif expected == "SEC_FORM4_NOT_EXPECTED_FPI_QUALIFYING_EXEMPT":
        coverage_status = "ownership_fpi_qualifying_exempt_use_local_source"
        review_reason = "FPI jurisdiction may satisfy SEC exemptive relief; local insider source should be used if conditions apply."
    else:
        coverage_status = "ownership_unknown_review"
        review_reason = "Insider reporting expectation is unknown."
    now = utc_now()
    conn.execute(
        """
        INSERT INTO dim_insider_reporting_profile(
            ticker, cik, source_id, issuer_type, issuer_country, primary_insider_source,
            section16_expected_status, fpi_qualifying_exemption_status,
            local_insider_source_required, hfia_effective_date,
            latest_ownership_filing_date, latest_ownership_form,
            ownership_filing_count, ownership_transaction_count, direct_sec_checked_at,
            coverage_status, review_reason, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            cik = excluded.cik,
            source_id = excluded.source_id,
            issuer_type = excluded.issuer_type,
            issuer_country = excluded.issuer_country,
            primary_insider_source = excluded.primary_insider_source,
            section16_expected_status = excluded.section16_expected_status,
            fpi_qualifying_exemption_status = excluded.fpi_qualifying_exemption_status,
            local_insider_source_required = excluded.local_insider_source_required,
            hfia_effective_date = excluded.hfia_effective_date,
            latest_ownership_filing_date = excluded.latest_ownership_filing_date,
            latest_ownership_form = excluded.latest_ownership_form,
            ownership_filing_count = excluded.ownership_filing_count,
            ownership_transaction_count = excluded.ownership_transaction_count,
            direct_sec_checked_at = excluded.direct_sec_checked_at,
            coverage_status = excluded.coverage_status,
            review_reason = excluded.review_reason,
            updated_at = excluded.updated_at
        """,
        (
            ticker,
            str(company["cik"]),
            source_id,
            classification["issuer_type"],
            str(company.get("country") or ""),
            classification["primary_insider_source"],
            expected,
            classification["fpi_qualifying_exemption_status"],
            int(classification["local_insider_source_required"]),
            hfia_effective_date.isoformat(),
            str(latest["filed_date"] or "") if latest is not None else "",
            str(latest["form_type"] or "") if latest is not None else "",
            int(filing_count or 0),
            trans_count,
            now,
            coverage_status,
            review_reason,
            now,
            now,
        ),
    )
    if coverage_status != "ownership_rows_found":
        add_issue(conn, ticker, source_id, coverage_status, review_reason or coverage_status)
    return {
        **classification,
        "coverage_status": coverage_status,
        "ownership_filing_count": int(filing_count or 0),
        "ownership_transaction_count": trans_count,
        "latest_ownership_filing_date": str(latest["filed_date"] or "") if latest is not None else "",
        "review_reason": review_reason,
    }


def document_url(template: str, cik: str, filing: dict[str, Any]) -> str:
    primary_document = str(filing["primary_document"])
    if primary_document.lower().startswith("xslf345"):
        primary_document = primary_document.split("/", 1)[1] if "/" in primary_document else primary_document
    return template.format(
        cik_int=int(cik),
        accession_no_dash=str(filing["accession_number"]).replace("-", ""),
        primary_document=primary_document,
    )


def sync_company(
    conn: Any,
    company: dict[str, Any],
    *,
    config: dict[str, Any],
    source_id: str,
    cache_dir: Path,
    start: date,
    forms: set[str],
    hfia_effective_date: date,
    headers_json: dict[str, str],
    headers_xml: dict[str, str],
    timeout_sec: float,
    retries: int,
    sleep_sec: float,
    force_refresh: bool,
) -> dict[str, Any]:
    ticker = str(company["ticker"])
    cik = str(company["cik"])
    submissions_url = str(cfg_get(config, "sec_ownership_direct.submissions_url_template")).format(cik=cik)
    status, text, _ = cached_text(
        submissions_url,
        cache_dir / "submissions" / f"CIK{cik}.json",
        headers=headers_json,
        timeout_sec=timeout_sec,
        retries=retries,
        sleep_sec=sleep_sec,
        force_refresh=force_refresh,
    )
    record_raw_response(conn, source_id=source_id, endpoint=submissions_url, status=status, text=text, asof=date.today().isoformat())
    if status != 200:
        raise RuntimeError(f"SEC submissions fetch failed status={status}")
    payload = json_payload(text)
    rows = filing_records(payload)
    if str(cfg_get(config, "sec_ownership_direct.include_submission_archives", True)).lower() in {"1", "true", "yes", "y"}:
        for file_name in archive_file_names(payload):
            archive_url = str(cfg_get(config, "sec_ownership_direct.submissions_archive_url_template")).format(file_name=file_name)
            archive_status, archive_text, _ = cached_text(
                archive_url,
                cache_dir / "submissions" / file_name,
                headers=headers_json,
                timeout_sec=timeout_sec,
                retries=retries,
                sleep_sec=sleep_sec,
                force_refresh=force_refresh,
            )
            record_raw_response(conn, source_id=source_id, endpoint=archive_url, status=archive_status, text=archive_text, asof=date.today().isoformat())
            if archive_status == 200:
                rows.extend(filing_records({"filings": {"recent": json_payload(archive_text)}}))
    filings = ownership_filings(rows, start=start, forms=forms)
    raw_filings = 0
    nonderiv_rows = 0
    deriv_rows = 0
    compat_rows = 0
    parse_failures = 0
    archive_template = str(cfg_get(config, "sec_ownership_direct.sec_archive_url_template"))
    for filing in filings:
        url = document_url(archive_template, cik, filing)
        cache_name = f"raw_{cik}_{str(filing['accession_number']).replace('-', '')}_{url.rsplit('/', 1)[-1]}"
        doc_status, doc_text, _ = cached_text(
            url,
            cache_dir / "documents" / cache_name,
            headers=headers_xml,
            timeout_sec=timeout_sec,
            retries=retries,
            sleep_sec=sleep_sec,
            force_refresh=force_refresh,
        )
        record_raw_response(conn, source_id=source_id, endpoint=url, status=doc_status, text=doc_text, asof=str(filing["filing_date"]))
        raw_hash = hashlib.sha256(doc_text.encode("utf-8", errors="replace")).hexdigest()
        if doc_status != 200:
            parse_failures += 1
            upsert_filing(
                conn,
                ticker=ticker,
                cik=cik,
                source_id=source_id,
                filing=filing,
                parsed=None,
                owner={},
                source_url=url,
                raw_hash=raw_hash,
                parse_error=f"http_status_{doc_status}",
            )
            continue
        try:
            parsed = parse_ownership_document(doc_text)
        except Exception as exc:  # noqa: BLE001
            parse_failures += 1
            upsert_filing(
                conn,
                ticker=ticker,
                cik=cik,
                source_id=source_id,
                filing=filing,
                parsed=None,
                owner={},
                source_url=url,
                raw_hash=raw_hash,
                parse_error=f"{type(exc).__name__}: {exc}",
            )
            continue
        raw_filings += 1
        for owner in parsed["owners"]:
            upsert_filing(conn, ticker=ticker, cik=cik, source_id=source_id, filing=filing, parsed=parsed, owner=owner, source_url=url, raw_hash=raw_hash)
            for tx in parsed["nonderiv_transactions"]:
                upsert_nonderiv_transaction(conn, ticker=ticker, source_id=source_id, accession=str(filing["accession_number"]), owner=owner, tx=tx)
                upsert_form4_compat(conn, ticker=ticker, source_id=source_id, filing=filing, owner=owner, tx=tx, period_of_report=str(parsed.get("period_of_report") or ""))
                nonderiv_rows += 1
                if str(filing["form_type"]).upper() in {"4", "4/A"}:
                    compat_rows += 1
            for tx in parsed["deriv_transactions"]:
                upsert_deriv_transaction(conn, ticker=ticker, source_id=source_id, accession=str(filing["accession_number"]), owner=owner, tx=tx)
                deriv_rows += 1
            for holding in parsed["holdings"]:
                upsert_holding(conn, ticker=ticker, source_id=source_id, accession=str(filing["accession_number"]), owner=owner, holding=holding)
    profile = upsert_reporting_profile(conn, company, source_id=source_id, hfia_effective_date=hfia_effective_date)
    return {
        "ticker": ticker,
        "raw_filings": raw_filings,
        "nonderivative_transactions": nonderiv_rows,
        "derivative_transactions": deriv_rows,
        "form4_compat_transactions": compat_rows,
        "parse_failures": parse_failures,
        **profile,
    }


def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def qmarks(values: list[Any]) -> str:
    if not values:
        raise ValueError("values cannot be empty")
    return ",".join("?" for _ in values)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    LOGGER.info("Using technology DB: %s", db_path)
    source_id = str(cfg_get(config, "sec_ownership_direct.source_id", "sec_ownership_direct"))
    model_family = str(
        args.model_family
        or cfg_get(config, "technology_universe.initial_subsector", "semiconductors")
        or "semiconductors"
    ).strip()
    if not model_family:
        raise ValueError("model_family cannot be empty")
    registry_path = resolve_path(cfg_get(config, "source_registry.path"), base_dir=base_dir)
    output_csv = args.output_csv.expanduser().resolve() if args.output_csv else resolve_path(cfg_get(config, "sec_ownership_direct.report_output_csv"), base_dir=base_dir)
    cache_dir = resolve_path(cfg_get(config, "sec_ownership_direct.cache_dir"), base_dir=base_dir)
    start = parse_date(cfg_get(config, "sec_ownership_direct.start_date", "2016-01-01")) or date(2016, 1, 1)
    hfia_effective_date = parse_date(cfg_get(config, "sec_ownership_direct.hfia_effective_date", HFIA_EFFECTIVE_DEFAULT.isoformat())) or HFIA_EFFECTIVE_DEFAULT
    forms = {str(x).strip().upper() for x in cfg_get(config, "sec_ownership_direct.forms", sorted(OWNERSHIP_FORMS)) if str(x).strip()}
    user_agent = expand_env_vars(cfg_get(config, "sec_ownership_direct.user_agent", "technology-research/1.0"))
    headers_json = {"User-Agent": user_agent, "Accept": "application/json,text/plain,*/*", "Accept-Encoding": "gzip, deflate", "Host": "data.sec.gov"}
    headers_xml = {"User-Agent": user_agent, "Accept": "application/xml,text/xml,text/html,*/*"}
    timeout_sec = float(cfg_get(config, "sec_ownership_direct.timeout_sec", 30.0))
    retries = int(cfg_get(config, "sec_ownership_direct.max_retries", 3))
    sleep_sec = float(cfg_get(config, "sec_ownership_direct.request_sleep_sec", 0.12))
    max_tickers = args.max_tickers or int(cfg_get(config, "sec_ownership_direct.max_tickers_per_run", 0) or 0)
    ticker_filter = {normalize_ticker(x) for x in args.tickers.split(",") if normalize_ticker(x)}
    report_rows: list[dict[str, Any]] = []
    failures = 0
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        upsert_source_registry(conn, load_source_registry(registry_path))
        companies = load_universe(conn, ticker_filter, model_family=model_family)
        if max_tickers > 0:
            companies = companies[:max_tickers]
        if not companies:
            raise ValueError(f"No direct ownership universe tickers found for model_family={model_family}.")
        run_id = start_run(conn, run_type=RUN_TYPE, input_path=config_path)
        try:
            with conn:
                conn.execute(
                    f"DELETE FROM data_quality_issues WHERE stage = ? AND ticker IN ({qmarks([company['ticker'] for company in companies])})",
                    (RUN_TYPE, *[str(company["ticker"]) for company in companies]),
                )
            for idx, company in enumerate(companies, start=1):
                try:
                    with conn:
                        stats = sync_company(
                            conn,
                            company,
                            config=config,
                            source_id=source_id,
                            cache_dir=cache_dir,
                            start=start,
                            forms=forms,
                            hfia_effective_date=hfia_effective_date,
                            headers_json=headers_json,
                            headers_xml=headers_xml,
                            timeout_sec=timeout_sec,
                            retries=retries,
                            sleep_sec=sleep_sec,
                            force_refresh=args.force_refresh,
                        )
                    report_rows.append(
                        {
                            "ticker": company["ticker"],
                            "issuer_type": stats["issuer_type"],
                            "section16_expected_status": stats["section16_expected_status"],
                            "coverage_status": stats["coverage_status"],
                            "ownership_filings": stats["ownership_filing_count"],
                            "nonderivative_transactions": stats["nonderivative_transactions"],
                            "derivative_transactions": stats["derivative_transactions"],
                            "form4_compat_transactions": stats["form4_compat_transactions"],
                            "latest_ownership_filing_date": stats["latest_ownership_filing_date"],
                            "review_reason": stats["review_reason"],
                        }
                    )
                    LOGGER.info(
                        "[%d/%d] %s filings=%d nonderiv=%d deriv=%d compat_form4=%d status=%s",
                        idx,
                        len(companies),
                        company["ticker"],
                        stats["ownership_filing_count"],
                        stats["nonderivative_transactions"],
                        stats["derivative_transactions"],
                        stats["form4_compat_transactions"],
                        stats["coverage_status"],
                    )
                except Exception as exc:  # noqa: BLE001
                    failures += 1
                    with conn:
                        add_issue(conn, str(company["ticker"]), source_id, "ownership_direct_sec_fetch_failed", f"{type(exc).__name__}: {exc}", "error")
                    report_rows.append(
                        {
                            "ticker": company["ticker"],
                            "issuer_type": "",
                            "section16_expected_status": "",
                            "coverage_status": "ownership_direct_sec_fetch_failed",
                            "ownership_filings": 0,
                            "nonderivative_transactions": 0,
                            "derivative_transactions": 0,
                            "form4_compat_transactions": 0,
                            "latest_ownership_filing_date": "",
                            "review_reason": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    LOGGER.exception("[%d/%d] %s ownership sync failed", idx, len(companies), company["ticker"])
            write_report(output_csv, report_rows)
            status = "success" if failures == 0 else ("partial" if args.allow_partial else "failed")
            finish_run(
                conn,
                run_id=run_id,
                status=status,
                row_count=sum(int(row["form4_compat_transactions"] or 0) for row in report_rows),
                message=f"tickers={len(report_rows)} failures={failures} output={output_csv}",
            )
            LOGGER.info("Wrote SEC ownership coverage report: %s", output_csv)
            LOGGER.info("SEC ownership sync complete: tickers=%d failures=%d", len(report_rows), failures)
            if failures and not args.allow_partial:
                raise SystemExit(1)
        except BaseException as exc:
            if not isinstance(exc, SystemExit):
                finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()

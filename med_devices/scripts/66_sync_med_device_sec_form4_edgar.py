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
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.text_norm import normalize_cik, normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("sync_med_device_sec_form4_edgar")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_SUBMISSIONS_ARCHIVE_URL = "https://data.sec.gov/submissions/{file_name}"
SEC_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/{primary_doc}"
FIELDNAMES = [
    "accession_nodash",
    "transaction_id",
    "source_id",
    "company_id",
    "ticker",
    "issuer_cik",
    "reporting_owner_cik",
    "reporting_owner_name",
    "officer_title",
    "is_director",
    "is_officer",
    "is_ten_percent_owner",
    "transaction_date",
    "transaction_code",
    "transaction_shares",
    "transaction_price",
    "transaction_value_usd",
    "direct_or_indirect",
    "post_transaction_shares",
    "derivative_flag",
]


@dataclass(frozen=True)
class Company:
    company_id: int
    ticker: str
    cik: str
    company_name: str


@dataclass(frozen=True)
class Filing:
    company: Company
    accession_number: str
    primary_document: str
    filing_date: str
    form: str

    @property
    def accession_nodash(self) -> str:
        return self.accession_number.replace("-", "")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync med-device SEC Form 4 transactions directly from EDGAR.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--tickers", default="")
    parser.add_argument("--max-companies", type=int, default=0)
    parser.add_argument("--max-filings-per-company", type=int, default=None)
    parser.add_argument("--include-archives", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument(
        "--allow-fallback",
        action="store_true",
        help="Explicitly allow direct EDGAR fallback ingestion. Primary production path is SEC_FORM4_Runner.",
    )
    return parser.parse_args()


def to_float(raw: object) -> float | None:
    text = str(raw or "").replace(",", "").strip()
    if not text or text.lower() in {"none", "null", "nan", "n/a", "na"}:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def int_flag(raw: object) -> int:
    text = str(raw or "").strip().lower()
    return 1 if text in {"1", "true", "yes", "y"} else 0


def parse_date_text(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def default_start_date(config: dict[str, Any], end: date) -> date:
    raw = str(cfg_get(config, "sec_form4_ingestion.start_date", "") or "").strip()
    parsed = parse_date_text(raw)
    if parsed is not None:
        return parsed
    lookback_days = int(cfg_get(config, "sec_form4_ingestion.default_lookback_days", 365))
    return end - timedelta(days=max(1, lookback_days))


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def first_child(element: ET.Element | None, name: str) -> ET.Element | None:
    if element is None:
        return None
    for child in list(element):
        if local_name(child.tag) == name:
            return child
    return None


def text_path(element: ET.Element | None, *names: str) -> str:
    cur = element
    for name in names:
        cur = first_child(cur, name)
        if cur is None:
            return ""
    return str(cur.text or "").strip()


def descendants(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element.iter() if local_name(child.tag) == name]


def parse_bool_text(raw: str) -> int:
    return 1 if raw.strip().lower() in {"1", "true", "yes"} else 0


def ensure_source(conn: Any, source_id: str) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO source_registry(
            source_id, stage, source_name, source_type, base_url,
            authentication_required, free_key_required, priority, status, created_at, updated_at
        )
        VALUES (?, 'stage_1', 'SEC Form 4 insider transactions', 'edgar_xml',
                'https://data.sec.gov/submissions/', 0, 0, 64, 'planned', ?, ?)
        ON CONFLICT(source_id) DO UPDATE SET updated_at = excluded.updated_at
        """,
        (source_id, now, now),
    )


def load_companies(conn: Any, *, tickers: set[str], max_companies: int) -> list[Company]:
    rows = conn.execute(
        """
        SELECT company_id, ticker, cik, company_name
        FROM dim_company
        WHERE is_active = 1
          AND COALESCE(cik, '') <> ''
        ORDER BY ticker
        """
    ).fetchall()
    companies: list[Company] = []
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        if tickers and ticker not in tickers:
            continue
        cik = normalize_cik(row["cik"])
        if not cik:
            continue
        companies.append(
            Company(
                company_id=int(row["company_id"]),
                ticker=ticker,
                cik=cik.zfill(10),
                company_name=str(row["company_name"] or ""),
            )
        )
        if max_companies > 0 and len(companies) >= max_companies:
            break
    return companies


def fetch_json(url: str, *, user_agent: str, timeout_sec: float, retries: int, sleep_sec: float) -> dict[str, Any]:
    text = fetch_text(url, user_agent=user_agent, timeout_sec=timeout_sec, retries=retries, sleep_sec=sleep_sec)
    return json.loads(text)


def fetch_text(url: str, *, user_agent: str, timeout_sec: float, retries: int, sleep_sec: float) -> str:
    request = Request(url, headers={"User-Agent": user_agent, "Accept-Encoding": "identity"})
    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            with urlopen(request, timeout=timeout_sec) as response:
                return response.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError) as exc:
            last_error = exc
            if isinstance(exc, HTTPError) and exc.code in {403, 404}:
                raise
            if attempt + 1 < retries:
                time.sleep(sleep_sec * (attempt + 1))
                continue
    raise RuntimeError(f"Failed to fetch EDGAR resource: url={url} error={last_error}")


def records_from_columnar_filing_data(columnar: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(columnar, dict):
        return []
    keys = list(columnar)
    row_count = max((len(value) for value in columnar.values() if isinstance(value, list)), default=0)
    records: list[dict[str, Any]] = []
    for idx in range(row_count):
        item: dict[str, Any] = {}
        for key in keys:
            values = columnar.get(key)
            if isinstance(values, list) and idx < len(values):
                item[key] = values[idx]
        records.append(item)
    return records


def filing_records(data: dict[str, Any]) -> list[dict[str, Any]]:
    recent = data.get("filings", {}).get("recent", {})
    if isinstance(recent, dict):
        return records_from_columnar_filing_data(recent)
    if "accessionNumber" in data:
        return records_from_columnar_filing_data(data)
    return []


def archived_filing_records(
    submissions: dict[str, Any],
    *,
    user_agent: str,
    timeout_sec: float,
    retries: int,
    sleep_sec: float,
) -> list[dict[str, Any]]:
    files = submissions.get("filings", {}).get("files", [])
    if not isinstance(files, list):
        return []
    records: list[dict[str, Any]] = []
    for file_info in files:
        if not isinstance(file_info, dict):
            continue
        file_name = str(file_info.get("name") or "").strip()
        if not file_name:
            continue
        try:
            archive = fetch_json(
                SEC_SUBMISSIONS_ARCHIVE_URL.format(file_name=file_name),
                user_agent=user_agent,
                timeout_sec=timeout_sec,
                retries=retries,
                sleep_sec=sleep_sec,
            )
        except (HTTPError, URLError, RuntimeError, json.JSONDecodeError) as exc:
            LOGGER.warning("Skipping SEC submissions archive %s after fetch error: %s", file_name, exc)
            continue
        records.extend(filing_records(archive))
        time.sleep(sleep_sec)
    return records


def selected_filings(
    company: Company,
    records: list[dict[str, Any]],
    *,
    start: date,
    end: date,
    max_filings: int,
) -> list[Filing]:
    out: list[Filing] = []
    seen_accessions: set[str] = set()
    for record in records:
        form = str(record.get("form") or "").upper()
        filing_date = parse_date_text(record.get("filingDate"))
        accession = str(record.get("accessionNumber") or "").strip()
        primary_doc = str(record.get("primaryDocument") or "").strip()
        if form not in {"4", "4/A"} or filing_date is None or not accession or not primary_doc:
            continue
        if accession in seen_accessions:
            continue
        if filing_date < start or filing_date > end:
            continue
        seen_accessions.add(accession)
        out.append(
            Filing(
                company=company,
                accession_number=accession,
                primary_document=primary_doc,
                filing_date=filing_date.isoformat(),
                form=form,
            )
        )
        if max_filings > 0 and len(out) >= max_filings:
            break
    return out


def parse_ownership_xml(text: str) -> ET.Element:
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        start = text.find("<ownershipDocument")
        end = text.rfind("</ownershipDocument>")
        if start >= 0 and end >= 0:
            return ET.fromstring(text[start : end + len("</ownershipDocument>")])
        raise


def first_reporting_owner(root: ET.Element) -> dict[str, Any]:
    owner = first_child(root, "reportingOwner")
    owner_id = first_child(owner, "reportingOwnerId")
    owner_rel = first_child(owner, "reportingOwnerRelationship")
    return {
        "reporting_owner_cik": normalize_cik(text_path(owner_id, "rptOwnerCik")),
        "reporting_owner_name": text_path(owner_id, "rptOwnerName"),
        "officer_title": text_path(owner_rel, "officerTitle"),
        "is_director": parse_bool_text(text_path(owner_rel, "isDirector")),
        "is_officer": parse_bool_text(text_path(owner_rel, "isOfficer")),
        "is_ten_percent_owner": parse_bool_text(text_path(owner_rel, "isTenPercentOwner")),
        "owner_count": len(descendants(root, "reportingOwner")),
    }


def transaction_rows_from_xml(
    filing: Filing,
    text: str,
    *,
    source_id: str,
    include_derivatives: bool,
) -> list[dict[str, Any]]:
    root = parse_ownership_xml(text)
    issuer = first_child(root, "issuer")
    issuer_cik = normalize_cik(text_path(issuer, "issuerCik"))
    issuer_ticker = normalize_ticker(text_path(issuer, "issuerTradingSymbol")) or filing.company.ticker
    owner = first_reporting_owner(root)
    rows: list[dict[str, Any]] = []
    transaction_groups: list[tuple[int, str, list[ET.Element]]] = [
        (0, "nonDerivativeTransaction", descendants(root, "nonDerivativeTransaction")),
    ]
    if include_derivatives:
        transaction_groups.append((1, "derivativeTransaction", descendants(root, "derivativeTransaction")))
    for derivative_flag, tag_name, txs in transaction_groups:
        for tx in txs:
            code = text_path(tx, "transactionCoding", "transactionCode").upper()
            if code not in {"P", "S"}:
                continue
            transaction_date = text_path(tx, "transactionDate", "value")
            shares = to_float(text_path(tx, "transactionAmounts", "transactionShares", "value"))
            price = to_float(text_path(tx, "transactionAmounts", "transactionPricePerShare", "value"))
            value = shares * price if shares is not None and price is not None else None
            transaction_identity = {
                "tag": tag_name,
                "derivative_flag": derivative_flag,
                "code": code,
                "transaction_date": transaction_date,
                "shares": shares,
                "price": price,
                "direct_or_indirect": text_path(tx, "ownershipNature", "directOrIndirectOwnership", "value"),
                "post_transaction_shares": text_path(
                    tx,
                    "postTransactionAmounts",
                    "sharesOwnedFollowingTransaction",
                    "value",
                ),
                "acquired_disposed": text_path(
                    tx,
                    "transactionAmounts",
                    "transactionAcquiredDisposedCode",
                    "value",
                ),
                "xml": ET.tostring(tx, encoding="unicode"),
            }
            transaction_digest = hashlib.sha1(
                json.dumps(transaction_identity, sort_keys=True, ensure_ascii=True).encode("utf-8")
            ).hexdigest()[:20]
            transaction_id = f"{tag_name}_{transaction_digest}"
            row = {
                "accession_nodash": filing.accession_nodash,
                "transaction_id": transaction_id,
                "source_id": source_id,
                "company_id": filing.company.company_id,
                "ticker": issuer_ticker,
                "issuer_cik": issuer_cik,
                **owner,
                "transaction_date": transaction_date,
                "transaction_code": code,
                "transaction_shares": shares,
                "transaction_price": price,
                "transaction_value_usd": value,
                "direct_or_indirect": text_path(tx, "ownershipNature", "directOrIndirectOwnership", "value"),
                "post_transaction_shares": to_float(
                    text_path(tx, "postTransactionAmounts", "sharesOwnedFollowingTransaction", "value")
                ),
                "derivative_flag": derivative_flag,
                "payload_json": json.dumps(
                    {
                        "filing_date": filing.filing_date,
                        "form": filing.form,
                        "primary_document": filing.primary_document,
                        "acquired_disposed": text_path(
                            tx,
                            "transactionAmounts",
                            "transactionAcquiredDisposedCode",
                            "value",
                        ),
                        "owner_count": owner["owner_count"],
                    },
                    sort_keys=True,
                    ensure_ascii=True,
                ),
            }
            rows.append(row)
    return rows


def cache_path(cache_dir: Path, filing: Filing) -> Path:
    return cache_dir / filing.company.ticker / f"{filing.accession_nodash}_{filing.primary_document}"


def load_filing_text(
    filing: Filing,
    *,
    cache_dir: Path,
    user_agent: str,
    timeout_sec: float,
    retries: int,
    sleep_sec: float,
) -> str:
    path = cache_path(cache_dir, filing)
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace")
    url = SEC_ARCHIVE_URL.format(
        cik_int=int(filing.company.cik),
        accession_nodash=filing.accession_nodash,
        primary_doc=filing.primary_document,
    )
    text = fetch_text(url, user_agent=user_agent, timeout_sec=timeout_sec, retries=retries, sleep_sec=sleep_sec)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text


def build_rows(
    companies: list[Company],
    *,
    start: date,
    end: date,
    source_id: str,
    cache_dir: Path,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    user_agent = str(
        cfg_get(config, "sec_form4_ingestion.user_agent", cfg_get(config, "sec_filings.user_agent", "med-devices-research/1.0"))
    )
    timeout_sec = float(cfg_get(config, "sec_form4_ingestion.timeout_sec", 30.0))
    retries = int(cfg_get(config, "sec_form4_ingestion.download_retries", 3))
    sleep_sec = float(cfg_get(config, "sec_form4_ingestion.request_sleep_sec", 0.2))
    max_filings = int(cfg_get(config, "sec_form4_ingestion.max_filings_per_company", 0))
    include_archives = str(cfg_get(config, "sec_form4_ingestion.include_archives", True)).strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }
    include_derivatives = str(cfg_get(config, "sec_form4_ingestion.include_derivatives", False)).strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }
    out: list[dict[str, Any]] = []
    for company in companies:
        submissions_url = SEC_SUBMISSIONS_URL.format(cik=company.cik)
        try:
            submissions = fetch_json(
                submissions_url,
                user_agent=user_agent,
                timeout_sec=timeout_sec,
                retries=retries,
                sleep_sec=sleep_sec,
            )
        except (HTTPError, URLError, RuntimeError, json.JSONDecodeError) as exc:
            LOGGER.warning("Skipping Form 4 submissions for %s after fetch error: %s", company.ticker, exc)
            continue
        records = filing_records(submissions)
        if include_archives:
            records.extend(
                archived_filing_records(
                    submissions,
                    user_agent=user_agent,
                    timeout_sec=timeout_sec,
                    retries=retries,
                    sleep_sec=sleep_sec,
                )
            )
        filings = selected_filings(company, records, start=start, end=end, max_filings=max_filings)
        for filing in filings:
            try:
                text = load_filing_text(
                    filing,
                    cache_dir=cache_dir,
                    user_agent=user_agent,
                    timeout_sec=timeout_sec,
                    retries=retries,
                    sleep_sec=sleep_sec,
                )
                out.extend(
                    transaction_rows_from_xml(
                        filing,
                        text,
                        source_id=source_id,
                        include_derivatives=include_derivatives,
                    )
                )
            except (HTTPError, URLError, RuntimeError, ET.ParseError, ValueError) as exc:
                LOGGER.warning("Skipping Form 4 filing %s %s: %s", filing.company.ticker, filing.accession_number, exc)
            time.sleep(sleep_sec)
        LOGGER.info("Form 4 processed: ticker=%s filings=%d rows_so_far=%d", company.ticker, len(filings), len(out))
        time.sleep(sleep_sec)
    return out


def upsert_rows(conn: Any, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    now = utc_now()
    conn.executemany(
        """
        INSERT INTO fact_sec_form4_transaction(
            accession_nodash, transaction_id, source_id, company_id, ticker, issuer_cik,
            reporting_owner_cik, reporting_owner_name, officer_title, is_director, is_officer,
            is_ten_percent_owner, transaction_date, transaction_code, transaction_shares,
            transaction_price, transaction_value_usd, direct_or_indirect, post_transaction_shares,
            derivative_flag, payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(accession_nodash, transaction_id, source_id) DO UPDATE SET
            company_id = excluded.company_id,
            ticker = excluded.ticker,
            issuer_cik = excluded.issuer_cik,
            reporting_owner_cik = excluded.reporting_owner_cik,
            reporting_owner_name = excluded.reporting_owner_name,
            officer_title = excluded.officer_title,
            is_director = excluded.is_director,
            is_officer = excluded.is_officer,
            is_ten_percent_owner = excluded.is_ten_percent_owner,
            transaction_date = excluded.transaction_date,
            transaction_code = excluded.transaction_code,
            transaction_shares = excluded.transaction_shares,
            transaction_price = excluded.transaction_price,
            transaction_value_usd = excluded.transaction_value_usd,
            direct_or_indirect = excluded.direct_or_indirect,
            post_transaction_shares = excluded.post_transaction_shares,
            derivative_flag = excluded.derivative_flag,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        [
            (
                row["accession_nodash"],
                row["transaction_id"],
                row["source_id"],
                row["company_id"],
                row["ticker"],
                row.get("issuer_cik", ""),
                row.get("reporting_owner_cik", ""),
                row.get("reporting_owner_name", ""),
                row.get("officer_title", ""),
                row.get("is_director", 0),
                row.get("is_officer", 0),
                row.get("is_ten_percent_owner", 0),
                row.get("transaction_date", ""),
                row.get("transaction_code", ""),
                row.get("transaction_shares"),
                row.get("transaction_price"),
                row.get("transaction_value_usd"),
                row.get("direct_or_indirect", ""),
                row.get("post_transaction_shares"),
                row.get("derivative_flag", 0),
                row.get("payload_json", "{}"),
                now,
                now,
            )
            for row in rows
        ],
    )
    return len(rows)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    direct_enabled = str(cfg_get(config, "sec_form4_ingestion.direct_edgar_enabled", False)).strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }
    if not args.allow_fallback and not direct_enabled:
        raise RuntimeError(
            "Direct med-device Form 4 EDGAR ingestion is fallback-only. "
            "Use SEC_FORM4_Runner as the canonical source path, or pass --allow-fallback for repair runs."
        )
    end = parse_date_text(args.end_date) or date.today()
    start = parse_date_text(args.start_date) or default_start_date(config, end)
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(cfg_get(config, "sec_form4_ingestion.output_csv"), base_dir=base_dir)
    )
    cache_dir = (
        args.cache_dir.expanduser().resolve()
        if args.cache_dir
        else resolve_path(
            cfg_get(config, "sec_form4_ingestion.cache_dir", "../output/med_devices_cache/sec_form4"),
            base_dir=base_dir,
        )
    )
    source_id = str(cfg_get(config, "sec_form4_ingestion.source_id", "sec_form4_edgar"))
    ticker_filter = {normalize_ticker(value) for value in str(args.tickers or "").split(",") if normalize_ticker(value)}
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        ensure_source(conn, source_id)
        run_id = start_run(conn, run_type="sync_med_device_sec_form4_edgar", input_path=config_path)
        try:
            companies = load_companies(conn, tickers=ticker_filter, max_companies=max(0, int(args.max_companies)))
            if args.max_filings_per_company is not None:
                config.setdefault("sec_form4_ingestion", {})["max_filings_per_company"] = int(args.max_filings_per_company)
            if args.include_archives is not None:
                config.setdefault("sec_form4_ingestion", {})["include_archives"] = bool(args.include_archives)
            rows = build_rows(
                companies,
                start=start,
                end=end,
                source_id=source_id,
                cache_dir=cache_dir,
                config=config,
            )
            count = upsert_rows(conn, rows)
            write_csv(output_csv, rows)
            finish_run(conn, run_id=run_id, status="success", row_count=count, message=f"start={start} end={end} rows={count}")
            LOGGER.info("SEC Form 4 EDGAR sync complete: rows=%d output=%s", count, output_csv)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()

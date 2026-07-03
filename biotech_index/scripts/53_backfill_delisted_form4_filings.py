#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from biotech_index.core.db import connect  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_UNIVERSE = PACKAGE_ROOT / "data" / "delisted_biotech_calibration_universe.csv"
DEFAULT_OUTPUT = PROJECT_ROOT / "output" / "biotech_index_reports" / "delisted_calibration_universe" / "delisted_form4_backfill.csv"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVE_DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession}/{document}"
BACKFILL_TABLE = "delisted_calibration_form4_filings"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill SEC Form 4/4-A filing ledgers for delisted biotech calibration "
            "issuers into a biotech-owned table. This intentionally does not write to "
            "the shared staging Form 4 production database."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--universe-csv", type=Path, default=DEFAULT_UNIVERSE)
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--asof", type=str, default="")
    parser.add_argument("--tickers", type=str, default="", help="Comma-separated original tickers to backfill; default is all strict universe rows.")
    parser.add_argument("--metadata-only", action="store_true", help="Do not fetch individual Form 4 documents; store SEC ledger rows only.")
    parser.add_argument("--max-document-fetches", type=int, default=0, help="Optional cap for document fetches; 0 means no cap.")
    parser.add_argument("--sleep-sec", type=float, default=0.12, help="SEC request throttle delay.")
    parser.add_argument("--user-agent", type=str, default="")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_asof(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return date.today().isoformat()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return date.fromisoformat(text[:10]).isoformat()


def normalize_cik(raw: object) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    if not digits:
        return ""
    return (digits.lstrip("0") or "0").zfill(10)


def parse_date(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    for candidate in (text, text[:10]):
        for fmt in ("%Y-%m-%d", "%Y%m%d", "%m/%d/%Y"):
            try:
                return datetime.strptime(candidate, fmt).date().isoformat()
            except ValueError:
                continue
    return ""


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        rows: list[dict[str, str]] = []
        for row in reader:
            rows.append({str(key): str(value or "").strip() for key, value in row.items()})
        return rows


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def request_json(url: str, *, user_agent: str, sleep_sec: float) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if sleep_sec > 0:
        time.sleep(sleep_sec)
    return payload


def request_text(url: str, *, user_agent: str, sleep_sec: float) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=30) as response:
        payload = response.read().decode("utf-8", errors="replace")
    if sleep_sec > 0:
        time.sleep(sleep_sec)
    return payload


def append_filings(out: list[dict[str, str]], block: dict[str, Any]) -> None:
    forms = block.get("form") or []
    accessions = block.get("accessionNumber") or []
    filing_dates = block.get("filingDate") or []
    report_dates = block.get("reportDate") or []
    primary_docs = block.get("primaryDocument") or []
    for idx, form in enumerate(forms):
        if str(form or "").strip() not in {"4", "4/A"}:
            continue
        accession = str(accessions[idx] if idx < len(accessions) else "").strip()
        out.append(
            {
                "form": str(form or "").strip(),
                "accession_nodash": accession.replace("-", ""),
                "accession_number": accession,
                "filing_date": parse_date(filing_dates[idx] if idx < len(filing_dates) else ""),
                "report_date": parse_date(report_dates[idx] if idx < len(report_dates) else ""),
                "primary_document": str(primary_docs[idx] if idx < len(primary_docs) else "").strip(),
            }
        )


def load_sec_form4_ledger(cik: str, *, user_agent: str, sleep_sec: float) -> tuple[dict[str, Any], list[dict[str, str]]]:
    cik = normalize_cik(cik)
    submissions = request_json(SEC_SUBMISSIONS_URL.format(cik=cik), user_agent=user_agent, sleep_sec=sleep_sec)
    rows: list[dict[str, str]] = []
    append_filings(rows, (submissions.get("filings") or {}).get("recent") or {})
    for file_info in (submissions.get("filings") or {}).get("files") or []:
        shard_name = str(file_info.get("name") or "").strip()
        if not shard_name:
            continue
        shard = request_json(f"https://data.sec.gov/submissions/{shard_name}", user_agent=user_agent, sleep_sec=sleep_sec)
        append_filings(rows, shard)
    return submissions, rows


def raw_document_name(primary_document: str) -> str:
    text = str(primary_document or "").strip()
    if not text:
        return ""
    if "/" in text and text.lower().startswith("xslf345"):
        return text.rsplit("/", 1)[-1]
    return text


def raw_document_url(*, cik: str, accession_nodash: str, primary_document: str) -> str:
    document = raw_document_name(primary_document)
    if not document:
        return ""
    return SEC_ARCHIVE_DOC_URL.format(cik_int=str(int(normalize_cik(cik))), accession=accession_nodash, document=document)


def text_of(node: ET.Element | None) -> str:
    return str(node.text or "").strip() if node is not None else ""


def parse_form4_document(text: str) -> dict[str, Any]:
    out = {
        "issuer_name_document": "",
        "issuer_trading_symbol_document": "",
        "issuer_cik_document": "",
        "issuer_document_match": 0,
        "transaction_codes": "",
        "purchase_transaction_count": 0,
        "document_parse_status": "",
    }
    try:
        root = ET.fromstring(text.encode("utf-8"))
        issuer = root.find(".//issuer")
        if issuer is not None:
            out["issuer_cik_document"] = normalize_cik(text_of(issuer.find("issuerCik")))
            out["issuer_name_document"] = text_of(issuer.find("issuerName"))
            out["issuer_trading_symbol_document"] = text_of(issuer.find("issuerTradingSymbol")).upper()
        codes = sorted(
            {
                text_of(node).upper()
                for node in root.findall(".//transactionCoding/transactionCode")
                if text_of(node)
            }
        )
        out["transaction_codes"] = "|".join(codes)
        out["purchase_transaction_count"] = sum(
            1
            for node in root.findall(".//transactionCoding/transactionCode")
            if text_of(node).upper() == "P"
        )
        out["document_parse_status"] = "xml_ok"
        return out
    except ET.ParseError:
        # Very old ownership forms can be HTML-only.  Keep a narrow parser for
        # issuer identity; transaction parsing is intentionally conservative.
        match = re.search(
            r"Issuer Name.*?CIK=(\d+)[^>]*>([^<]+)</a>\s*\[\s*<span[^>]*>([^<]+)</span>\s*\]",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if match:
            out["issuer_cik_document"] = normalize_cik(match.group(1))
            out["issuer_name_document"] = re.sub(r"\s+", " ", match.group(2)).strip()
            out["issuer_trading_symbol_document"] = re.sub(r"\s+", " ", match.group(3)).strip().upper()
            out["document_parse_status"] = "html_issuer_ok"
        else:
            out["document_parse_status"] = "parse_error"
        return out


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {BACKFILL_TABLE} (
            accession_nodash TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            calibration_company_ticker TEXT NOT NULL,
            company_id INTEGER,
            company_name TEXT,
            issuer_cik TEXT NOT NULL,
            sec_company_name TEXT,
            sec_company_tickers TEXT,
            form TEXT NOT NULL,
            filing_date TEXT NOT NULL,
            report_date TEXT,
            primary_document TEXT,
            raw_document_url TEXT,
            issuer_name_document TEXT,
            issuer_trading_symbol_document TEXT,
            issuer_cik_document TEXT,
            issuer_document_match INTEGER NOT NULL DEFAULT 0,
            transaction_codes TEXT,
            purchase_transaction_count INTEGER NOT NULL DEFAULT 0,
            document_parse_status TEXT,
            source_url TEXT,
            valid_window_start TEXT,
            valid_window_end TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE SET NULL
        )
        """
    )


def company_id_for(conn: sqlite3.Connection, calibration_company_ticker: str) -> int | None:
    row = conn.execute("SELECT company_id FROM companies WHERE ticker = ?", (calibration_company_ticker,)).fetchone()
    if row is None:
        return None
    return int(row["company_id"])


def upsert_rows(conn: sqlite3.Connection, rows: list[dict[str, Any]], *, dry_run: bool) -> int:
    if dry_run or not rows:
        return 0
    now = utc_now()
    for row in rows:
        row = dict(row)
        row.setdefault("created_at", now)
        row["updated_at"] = now
        conn.execute(
            f"""
            INSERT INTO {BACKFILL_TABLE} (
                accession_nodash, ticker, calibration_company_ticker, company_id, company_name,
                issuer_cik, sec_company_name, sec_company_tickers, form, filing_date, report_date,
                primary_document, raw_document_url, issuer_name_document, issuer_trading_symbol_document,
                issuer_cik_document, issuer_document_match, transaction_codes, purchase_transaction_count,
                document_parse_status, source_url, valid_window_start, valid_window_end, created_at, updated_at
            ) VALUES (
                :accession_nodash, :ticker, :calibration_company_ticker, :company_id, :company_name,
                :issuer_cik, :sec_company_name, :sec_company_tickers, :form, :filing_date, :report_date,
                :primary_document, :raw_document_url, :issuer_name_document, :issuer_trading_symbol_document,
                :issuer_cik_document, :issuer_document_match, :transaction_codes, :purchase_transaction_count,
                :document_parse_status, :source_url, :valid_window_start, :valid_window_end, :created_at, :updated_at
            )
            ON CONFLICT(accession_nodash) DO UPDATE SET
                ticker=excluded.ticker,
                calibration_company_ticker=excluded.calibration_company_ticker,
                company_id=excluded.company_id,
                company_name=excluded.company_name,
                issuer_cik=excluded.issuer_cik,
                sec_company_name=excluded.sec_company_name,
                sec_company_tickers=excluded.sec_company_tickers,
                form=excluded.form,
                filing_date=excluded.filing_date,
                report_date=excluded.report_date,
                primary_document=excluded.primary_document,
                raw_document_url=excluded.raw_document_url,
                issuer_name_document=excluded.issuer_name_document,
                issuer_trading_symbol_document=excluded.issuer_trading_symbol_document,
                issuer_cik_document=excluded.issuer_cik_document,
                issuer_document_match=excluded.issuer_document_match,
                transaction_codes=excluded.transaction_codes,
                purchase_transaction_count=excluded.purchase_transaction_count,
                document_parse_status=excluded.document_parse_status,
                source_url=excluded.source_url,
                valid_window_start=excluded.valid_window_start,
                valid_window_end=excluded.valid_window_end,
                updated_at=excluded.updated_at
            """,
            row,
        )
    conn.commit()
    return len(rows)


def main() -> int:
    args = parse_args()
    asof = parse_asof(args.asof)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    user_agent = args.user_agent or str(cfg_get(config, "market_positioning.user_agent", "StagingScalperSystem jose.martinez.research@example.com"))
    selected = {item.strip().upper() for item in str(args.tickers or "").split(",") if item.strip()}

    candidates = [
        row
        for row in read_csv(args.universe_csv.expanduser().resolve())
        if str(row.get("source_candidate_row_status") or "") == "strict_usable"
        and (not selected or str(row.get("ticker") or "").upper() in selected)
    ]
    conn = connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_table(conn)

    output_rows: list[dict[str, Any]] = []
    document_fetches = 0
    written = 0
    ledger_fetch_failures = 0
    try:
        for idx, row in enumerate(candidates, start=1):
            candidate_payload: list[dict[str, Any]] = []
            ticker = str(row.get("ticker") or "").upper()
            calibration_ticker = str(row.get("calibration_company_ticker") or "").upper()
            cik = normalize_cik(row.get("cik"))
            start_date = parse_date(row.get("price_start_date"))
            end_date = parse_date(row.get("price_end_date"))
            company_id = company_id_for(conn, calibration_ticker)
            if not cik:
                continue
            try:
                submissions, filings = load_sec_form4_ledger(cik, user_agent=user_agent, sleep_sec=float(args.sleep_sec))
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                # One dead/missing CIK (e.g. a 404 submissions ledger) must not
                # abort the whole multi-issuer backfill.
                ledger_fetch_failures += 1
                print(
                    f"WARNING: [{idx}/{len(candidates)}] {ticker} cik={cik} ledger fetch failed: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                continue
            sec_name = str(submissions.get("name") or "")
            sec_tickers = "|".join(str(item) for item in (submissions.get("tickers") or []))
            kept = 0
            for filing in filings:
                filing_date = filing.get("filing_date", "")
                if not filing_date:
                    continue
                if start_date and filing_date < start_date:
                    continue
                if end_date and filing_date > end_date:
                    continue
                doc_url = raw_document_url(cik=cik, accession_nodash=filing["accession_nodash"], primary_document=filing["primary_document"])
                parsed = {
                    "issuer_name_document": "",
                    "issuer_trading_symbol_document": "",
                    "issuer_cik_document": "",
                    "issuer_document_match": 0,
                    "transaction_codes": "",
                    "purchase_transaction_count": 0,
                    "document_parse_status": "metadata_only" if args.metadata_only else "",
                }
                if not args.metadata_only and doc_url and (args.max_document_fetches <= 0 or document_fetches < args.max_document_fetches):
                    try:
                        parsed = parse_form4_document(request_text(doc_url, user_agent=user_agent, sleep_sec=float(args.sleep_sec)))
                        parsed["issuer_document_match"] = 1 if normalize_cik(parsed.get("issuer_cik_document")) == cik else 0
                    except (urllib.error.URLError, TimeoutError, OSError) as exc:
                        parsed["document_parse_status"] = f"fetch_error:{type(exc).__name__}"
                    document_fetches += 1
                elif not args.metadata_only:
                    parsed["document_parse_status"] = "document_fetch_cap_skipped"
                payload = {
                    "accession_nodash": filing["accession_nodash"],
                    "ticker": ticker,
                    "calibration_company_ticker": calibration_ticker,
                    "company_id": company_id,
                    "company_name": row.get("company_name", ""),
                    "issuer_cik": cik,
                    "sec_company_name": sec_name,
                    "sec_company_tickers": sec_tickers,
                    "form": filing["form"],
                    "filing_date": filing_date,
                    "report_date": filing.get("report_date", ""),
                    "primary_document": filing.get("primary_document", ""),
                    "raw_document_url": doc_url,
                    "source_url": SEC_SUBMISSIONS_URL.format(cik=cik),
                    "valid_window_start": start_date,
                    "valid_window_end": end_date,
                    **parsed,
                }
                candidate_payload.append(payload)
                output_rows.append(payload)
                kept += 1
            written += upsert_rows(conn, candidate_payload, dry_run=bool(args.dry_run))
            print(f"[{idx}/{len(candidates)}] {ticker} cik={cik} filings={len(filings)} kept={kept}")
    finally:
        conn.close()

    fieldnames = [
        "ticker",
        "calibration_company_ticker",
        "company_id",
        "company_name",
        "issuer_cik",
        "sec_company_name",
        "sec_company_tickers",
        "form",
        "filing_date",
        "report_date",
        "accession_nodash",
        "primary_document",
        "raw_document_url",
        "issuer_name_document",
        "issuer_trading_symbol_document",
        "issuer_cik_document",
        "issuer_document_match",
        "transaction_codes",
        "purchase_transaction_count",
        "document_parse_status",
        "valid_window_start",
        "valid_window_end",
    ]
    write_csv(args.output_csv.expanduser().resolve(), output_rows, fieldnames)
    summary = {
        "asof_date": asof,
        "candidate_count": len(candidates),
        "output_rows": len(output_rows),
        "db_rows_written": written,
        "document_fetches": document_fetches,
        "ledger_fetch_failures": ledger_fetch_failures,
        "metadata_only": bool(args.metadata_only),
        "dry_run": bool(args.dry_run),
        "db_path": str(db_path),
        "output_csv": str(args.output_csv.expanduser().resolve()),
        "created_at": utc_now(),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

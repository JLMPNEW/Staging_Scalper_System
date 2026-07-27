from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

from dedicated_parser.contracts import DocumentRef, FilingRef, file_sha256


DOCUMENT_SUFFIXES = frozenset({".htm", ".html", ".xhtml", ".xml", ".txt", ".pdf"})
EVENT_FORMS = frozenset({"8-K", "8-K/A", "6-K", "6-K/A"})
EXCLUDED_MARKERS = (
    "_cal.xml",
    "_def.xml",
    "_lab.xml",
    "_pre.xml",
    "report.css",
    "show.js",
)
INVALID_MONETARY_UNITS = frozenset({"", "PURE", "SHARES", "USD/SHARES"})


def _table_has_columns(
    conn: sqlite3.Connection,
    table: str,
    required: set[str],
) -> bool:
    exists = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (table,),
    ).fetchone()
    if exists is None:
        return False
    columns = {
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({table})")
    }
    return required <= columns


def _reporting_currency(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    model_family: str,
    asof_date: str,
    fallback: str,
) -> str:
    currency = ""
    if _table_has_columns(
        conn,
        "feature_financial_statement",
        {"ticker", "model_family", "asof_date", "reported_currency"},
    ):
        row = conn.execute(
            """
            SELECT UPPER(reported_currency) AS currency
            FROM feature_financial_statement
            WHERE ticker = ? AND model_family = ? AND asof_date <= ?
              AND COALESCE(reported_currency, '') <> ''
            ORDER BY asof_date DESC
            LIMIT 1
            """,
            (ticker, model_family, asof_date),
        ).fetchone()
        currency = str(row["currency"] or "").strip() if row else ""
    if (
        currency in INVALID_MONETARY_UNITS
        and _table_has_columns(
            conn,
            "fact_financial_statement_canonical",
            {
                "ticker",
                "model_family",
                "canonical_metric",
                "period_end",
                "filing_date",
                "unit",
            },
        )
    ):
        row = conn.execute(
            """
            SELECT UPPER(unit) AS currency
            FROM fact_financial_statement_canonical
            WHERE ticker = ? AND model_family = ?
              AND canonical_metric IN ('assets', 'revenue')
              AND COALESCE(unit, '') <> ''
              AND LENGTH(unit) = 3
              AND COALESCE(NULLIF(filing_date, ''), '9999-12-31') <= ?
            ORDER BY period_end DESC, filing_date DESC
            LIMIT 1
            """,
            (ticker, model_family, asof_date),
        ).fetchone()
        currency = str(row["currency"] or "").strip() if row else ""
    if currency in INVALID_MONETARY_UNITS:
        currency = str(fallback or "USD").strip().upper()
    return currency or "USD"


def accession_directory(cache_dir: Path, filing: FilingRef) -> Path:
    return (
        cache_dir
        / "sec_archive_xbrl"
        / f"CIK{filing.cik}"
        / filing.accession_number.replace("-", "")
    )


def _index_items(accession_dir: Path) -> list[dict[str, str]]:
    index_path = accession_dir / "index.json"
    if not index_path.exists():
        return []
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    output: list[dict[str, str]] = []
    for item in ((payload.get("directory") or {}).get("item") or []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        output.append(
            {
                "name": name,
                "type": str(
                    item.get("type")
                    or item.get("document_type")
                    or ""
                ).strip(),
                "description": str(
                    item.get("description")
                    or item.get("title")
                    or ""
                ).strip(),
            }
        )
    return output


def _filing_summary_documents(
    accession_dir: Path,
    *,
    keywords: tuple[str, ...],
) -> set[str]:
    path = accession_dir / "FilingSummary.xml"
    if not path.exists():
        return set()
    try:
        root = ET.fromstring(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ET.ParseError):
        return set()
    selected: set[str] = set()
    normalized_keywords = tuple(keyword.lower().strip() for keyword in keywords)
    for report in root.iter():
        if report.tag.rsplit("}", 1)[-1].lower() != "report":
            continue
        fields = {
            child.tag.rsplit("}", 1)[-1].lower(): str(child.text or "").strip()
            for child in report
        }
        document_name = fields.get("htmlfilename", "")
        description = " ".join(
            fields.get(key, "")
            for key in ("shortname", "longname", "menucategory", "role")
        ).lower()
        if document_name and any(
            keyword in description for keyword in normalized_keywords
        ):
            selected.add(document_name)
    return selected


def _full_submission_name(
    accession_dir: Path,
    *,
    accession_number: str,
) -> str:
    expected = f"{accession_number}.txt"
    if (accession_dir / expected).exists():
        return expected
    compact = accession_number.replace("-", "")
    candidates = sorted(
        path.name
        for path in accession_dir.glob("*.txt")
        if compact in path.stem.replace("-", "")
    )
    return candidates[0] if candidates else ""


def relevant_document_names(
    accession_dir: Path,
    *,
    filing: FilingRef,
    keywords: tuple[str, ...],
) -> tuple[str, ...]:
    index_items = _index_items(accession_dir)
    available = {item["name"] for item in index_items}
    available.update(path.name for path in accession_dir.iterdir() if path.is_file())
    selected = {filing.primary_document}
    selected.update(
        _filing_summary_documents(
            accession_dir,
            keywords=keywords,
        )
    )
    if filing.form_type.upper() in EVENT_FORMS:
        for item in index_items:
            name = item["name"]
            lower = name.lower()
            metadata = f"{item['type']} {item['description']}".lower()
            if re.search(
                r"(?:ex(?:hibit)?[-_ ]?99|earnings|presentation|release)",
                f"{lower} {metadata}",
            ):
                selected.add(name)
        for name in available:
            if re.search(
                r"(?:ex(?:hibit)?[-_ ]?99|earnings|presentation|release)",
                name.lower(),
            ):
                selected.add(name)
    full_submission = _full_submission_name(
        accession_dir,
        accession_number=filing.accession_number,
    )
    if full_submission:
        selected.add(full_submission)
    names = [
        name
        for name in selected
        if name
        and (accession_dir / name).is_file()
        and Path(name).suffix.lower() in DOCUMENT_SUFFIXES
        and not any(marker in name.lower() for marker in EXCLUDED_MARKERS)
    ]
    return tuple(
        sorted(
            names,
            key=lambda name: (
                0 if name == filing.primary_document else 1,
                2 if name == full_submission else 1,
                name.lower(),
            ),
        )
    )


def _known_hash(
    conn: sqlite3.Connection,
    *,
    filing: FilingRef,
    name: str,
    path: Path,
) -> str:
    stat = path.stat()
    row = conn.execute(
        """
        SELECT content_sha256
        FROM sec_parser_document_catalog
        WHERE cik = ? AND accession_number = ? AND document_name = ?
          AND source_path = ? AND file_size = ? AND modified_ns = ?
        ORDER BY cataloged_at DESC, rowid DESC
        LIMIT 1
        """,
        (
            filing.cik,
            filing.accession_number,
            name,
            str(path.resolve()),
            int(stat.st_size),
            int(stat.st_mtime_ns),
        ),
    ).fetchone()
    return str(row["content_sha256"]) if row else ""


def build_document_refs(
    conn: sqlite3.Connection,
    *,
    cache_dir: Path,
    filing: FilingRef,
    keywords: tuple[str, ...],
    max_documents: int = 16,
) -> tuple[DocumentRef, ...]:
    directory = accession_directory(cache_dir, filing)
    if not directory.exists():
        return ()
    names = relevant_document_names(
        directory,
        filing=filing,
        keywords=keywords,
    )
    full_submission = _full_submission_name(
        directory,
        accession_number=filing.accession_number,
    )
    if max_documents > 0 and len(names) > max_documents:
        truncated = list(names[:max_documents])
        # The sort places the full submission last, so a plain cap silently
        # drops it and the edgartools SGML inspection never runs. Reserve a
        # slot for it instead.
        if full_submission and full_submission in names and full_submission not in truncated:
            truncated[-1] = full_submission
        names = tuple(truncated)
    documents: list[DocumentRef] = []
    for name in names:
        path = directory / name
        stat = path.stat()
        content_hash = _known_hash(
            conn,
            filing=filing,
            name=name,
            path=path,
        ) or file_sha256(path)
        documents.append(
            DocumentRef(
                name=name,
                path=str(path.resolve()),
                content_sha256=content_hash,
                file_size=int(stat.st_size),
                modified_ns=int(stat.st_mtime_ns),
                is_primary=name == filing.primary_document,
                is_full_submission=name == full_submission,
                source_kind=(
                    "sec_full_submission_sgml"
                    if name == full_submission
                    else "sec_archive_document"
                ),
            )
        )
    return tuple(documents)


def filing_rows(
    conn: sqlite3.Connection,
    *,
    model_family: str,
    asof_date: str,
    tickers: Iterable[str],
    accessions: Iterable[str] | None,
    supported_forms: tuple[str, ...],
    max_filings_per_ticker: int,
    target_periods_by_ticker: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, list[FilingRef]]:
    ticker_list = sorted(set(tickers))
    if not ticker_list:
        return {}
    ticker_placeholders = ",".join("?" for _ in ticker_list)
    form_placeholders = ",".join("?" for _ in supported_forms)
    accession_list = sorted(set(accessions or ()))
    accession_filter = ""
    accession_params: tuple[str, ...] = ()
    if accession_list:
        accession_placeholders = ",".join("?" for _ in accession_list)
        accession_filter = (
            f" AND f.accession_number IN ({accession_placeholders})"
        )
        accession_params = tuple(accession_list)
    company_table = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'dim_company'
        """
    ).fetchone()
    company_join = (
        "LEFT JOIN dim_company AS c ON c.ticker = f.ticker"
        if company_table is not None
        else ""
    )
    currency_expression = (
        "COALESCE(NULLIF(c.currency, ''), 'USD')"
        if company_table is not None
        else "'USD'"
    )
    rows = conn.execute(
        f"""
        SELECT DISTINCT f.ticker, f.cik, f.accession_number, f.form_type,
               f.filing_date, f.accepted_at, f.report_date,
               f.primary_document, f.source_id,
               {currency_expression} AS company_currency
        FROM fact_sec_filing AS f
        {company_join}
        WHERE f.ticker IN ({ticker_placeholders})
          AND UPPER(f.form_type) IN ({form_placeholders})
          {accession_filter}
          AND SUBSTR(
                COALESCE(NULLIF(f.accepted_at, ''), f.filing_date),
                1,
                10
              ) <= ?
        ORDER BY f.ticker,
                 COALESCE(NULLIF(f.accepted_at, ''), f.filing_date) DESC,
                 f.accession_number DESC
        """,
        (
            *ticker_list,
            *(form.upper() for form in supported_forms),
            *accession_params,
            asof_date,
        ),
    ).fetchall()
    grouped_all: dict[str, list[FilingRef]] = {
        ticker: [] for ticker in ticker_list
    }
    reporting_currencies: dict[str, str] = {}
    for row in rows:
        ticker = str(row["ticker"])
        if ticker not in reporting_currencies:
            reporting_currencies[ticker] = _reporting_currency(
                conn,
                ticker=ticker,
                model_family=model_family,
                asof_date=asof_date,
                fallback=str(row["company_currency"] or "USD"),
            )
        grouped_all[ticker].append(
            FilingRef(
                ticker=ticker,
                cik=str(row["cik"] or ""),
                accession_number=str(row["accession_number"] or ""),
                form_type=str(row["form_type"] or ""),
                filing_date=str(row["filing_date"] or ""),
                accepted_at=str(row["accepted_at"] or row["filing_date"] or ""),
                report_date=str(row["report_date"] or ""),
                primary_document=str(row["primary_document"] or ""),
                source_id=str(row["source_id"] or ""),
                company_currency=reporting_currencies[ticker],
            )
        )
    grouped: dict[str, list[FilingRef]] = {
        ticker: [] for ticker in ticker_list
    }
    target_map = target_periods_by_ticker or {}
    periodic_forms = {
        "10-Q",
        "10-Q/A",
        "10-K",
        "10-K/A",
        "20-F",
        "20-F/A",
        "40-F",
        "40-F/A",
        "6-K",
        "6-K/A",
    }
    for ticker, ticker_filings in grouped_all.items():
        targets: list[date] = []
        for value in target_map.get(ticker, ()):
            try:
                targets.append(date.fromisoformat(value[:10]))
            except ValueError:
                continue

        def priority(item: FilingRef) -> tuple[int, int]:
            report_date: date | None
            try:
                report_date = date.fromisoformat(item.report_date[:10])
            except ValueError:
                report_date = None
            distances = (
                [abs((report_date - target).days) for target in targets]
                if report_date is not None
                else []
            )
            nearest = min(distances) if distances else 10_000
            form = item.form_type.upper()
            if nearest == 0 and form in periodic_forms:
                target_priority = 0
            elif nearest <= 45 and form in periodic_forms:
                target_priority = 1
            elif targets and form in periodic_forms:
                target_priority = 2
            else:
                target_priority = 3
            try:
                accepted_ordinal = date.fromisoformat(
                    item.accepted_at[:10]
                ).toordinal()
            except ValueError:
                accepted_ordinal = 0
            return target_priority, -accepted_ordinal

        prioritized = sorted(ticker_filings, key=priority)
        grouped[ticker] = (
            prioritized[:max_filings_per_ticker]
            if max_filings_per_ticker > 0
            else prioritized
        )
    return grouped

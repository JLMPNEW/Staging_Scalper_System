from __future__ import annotations

import json
import hashlib
import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

from dedicated_parser.contracts import DocumentRef, FilingRef, file_sha256
from dedicated_parser.path_io import (
    filesystem_path,
    is_dir_path,
    is_file_path,
    open_path,
    path_exists,
    read_bytes,
    resolve_path as resolve_filesystem_path,
    runtime_path,
    stat_path,
)
from dedicated_parser.sec_paths import (
    SEC_ARCHIVE_ENTRY_SUFFIXES,
    SEC_DOCUMENT_SUFFIXES,
    SEC_PRIMARY_DOCUMENT_SUFFIXES,
    SEC_SUBMISSIONS_ARCHIVE_SUFFIXES,
    resolve_sec_document_path,
    resolve_sec_relative_document_path,
    resolve_sec_seal_root,
    validate_sec_document_basename,
    validate_sec_relative_document_path,
)


def _cache_seal_valid(row: sqlite3.Row, *, cache_dir: Path | None = None) -> bool:
    try:
        stored_root = resolve_filesystem_path(
            Path(str(row['cache_root'])), strict=False
        )
        if cache_dir is None:
            if stored_root.parent.name != 'sealed':
                return False
            base_root = stored_root.parent.parent
        else:
            base_root = Path(cache_dir)
        root = resolve_sec_seal_root(
            base_root, str(row['seal_relative_path']),
            expected_asof=str(row['asof_date']),
        )
        if cache_dir is None and root != stored_root:
            return False
        entries = json.loads(str(row['cache_manifest_json']))
        normalized: list[dict[str, object]] = []
        if not is_dir_path(root) or not isinstance(entries, list) or not entries:
            return False
        for entry in entries:
            path = resolve_filesystem_path(
                root / str(entry['object_path']), strict=True
            )
            path.relative_to(root)
            payload = read_bytes(path)
            digest = hashlib.sha256(payload).hexdigest()
            if digest != str(entry['sha256']) or len(payload) != int(entry['bytes']):
                return False
            normalized.append({
                'logical_path': str(entry['logical_path']),
                'object_path': str(entry['object_path']),
                'bytes': len(payload), 'sha256': digest,
            })
        normalized.sort(key=lambda item: str(item['logical_path']))
        if len({str(item['logical_path']) for item in normalized}) != len(normalized):
            return False
        encoded = json.dumps(
            normalized, sort_keys=True, separators=(',', ':')
        ).encode()
        return hashlib.sha256(encoded).hexdigest() == str(row['cache_manifest_sha256'])
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _consumer_defensive_issuer_scope(
    conn: sqlite3.Connection,
) -> tuple[int, str]:
    rows = conn.execute('''SELECT t.ticker,c.company_id,c.cik,
        COALESCE(NULLIF(UPPER(TRIM(c.reporting_currency)),''),'USD')
        FROM dim_consumer_defensive_taxonomy t
        JOIN dim_company c ON c.company_id=t.company_id
        WHERE t.model_family='consumer_defensive'
        ORDER BY t.ticker,c.company_id''').fetchall()
    payload = sorted(
        [
            str(row[0]), int(row[1]), str(row[2] or '').zfill(10),
            str(row[3] or 'USD').strip().upper() or 'USD',
        ]
        for row in rows
    )
    encoded = json.dumps(
        payload,sort_keys=True,separators=(',', ':'),ensure_ascii=True,
    ).encode()
    return len(payload), hashlib.sha256(encoded).hexdigest()


def _consumer_defensive_association_manifest(
    conn: sqlite3.Connection, *, cutoff: str,
) -> dict[str, object]:
    '''Recompute the exact current PIT association identity independently.'''

    tickers = [
        str(row[0]) for row in conn.execute('''SELECT t.ticker
            FROM dim_consumer_defensive_taxonomy t
            WHERE t.model_family='consumer_defensive' ORDER BY t.ticker''')
    ]
    payload: Iterable[sqlite3.Row] = ()
    if tickers:
        placeholders = ','.join('?' for _ in tickers)
        payload = conn.execute(f'''SELECT b.accession_number,
            b.issuer_company_id,b.issuer_ticker,b.issuer_cik,b.relationship,
            b.form_type,b.filing_date,b.accepted_at,
            COALESCE(b.report_date,''),COALESCE(b.primary_document,''),
            b.source_id,COALESCE(b.source_url,'')
            FROM bridge_sec_filing_company b
            JOIN fact_sec_filing f ON f.accession_number=b.accession_number
            WHERE b.issuer_ticker IN ({placeholders}) AND f.accepted_at<=?
              AND COALESCE((SELECT e.event_type
                  FROM sec_filing_company_association_event e
                  WHERE e.accession_number=b.accession_number
                    AND e.issuer_company_id=b.issuer_company_id
                    AND e.effective_asof<=?
                  ORDER BY e.effective_asof DESC,e.event_id DESC LIMIT 1),
                  CASE WHEN b.association_status='active'
                       THEN 'observed' ELSE 'retired' END)
                  IN ('observed','reactivated')
            ORDER BY b.accession_number,b.issuer_company_id''',
            [*tickers, cutoff, cutoff],
        )
    digest = hashlib.sha256()
    association_count = 0
    accession_count = 0
    shared_accession_count = 0
    previous_accession = ''
    current_accession_rows = 0
    for raw in payload:
        row = list(raw)
        accession = str(row[0])
        if accession != previous_accession:
            if previous_accession:
                accession_count += 1
                shared_accession_count += int(current_accession_rows > 1)
            previous_accession = accession
            current_accession_rows = 0
        current_accession_rows += 1
        association_count += 1
        digest.update(json.dumps(
            row, ensure_ascii=True, separators=(',', ':'),
        ).encode())
        digest.update(b'\n')
    if previous_accession:
        accession_count += 1
        shared_accession_count += int(current_accession_rows > 1)
    return {
        'association_count': association_count,
        'accession_count': accession_count,
        'shared_accession_count': shared_accession_count,
        'association_sha256': digest.hexdigest(),
    }


def validate_consumer_defensive_catalog_contract(
    conn: sqlite3.Connection, *, asof_date: str,
    expected_ingestion_config_sha256: str | None,
    cache_dir: Path | None = None,
) -> None:
    '''Fail closed unless independently expected inputs match the exact seal.'''
    parser_view = conn.execute('''SELECT 1 FROM sqlite_master
        WHERE type='view' AND name='consumer_defensive_sec_parser_filing_input' ''').fetchone()
    if parser_view is None:
        raise RuntimeError(
            'Consumer Defensive parser catalog requires '
            'consumer_defensive_sec_parser_filing_input'
        )
    expected = str(expected_ingestion_config_sha256 or '').strip().lower()
    if not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise RuntimeError(
            'Consumer Defensive parser catalog requires an independently '
            'supplied expected ingestion-config SHA-256'
        )
    scope_count, scope_sha256 = _consumer_defensive_issuer_scope(conn)
    seal = conn.execute(
        '''SELECT r.asof_date,r.cache_root,r.cache_manifest_json,r.cache_manifest_sha256,
                  s.seal_relative_path,r.ingestion_config_sha256,
                  r.issuer_scope_sha256,r.association_count,
                  r.accession_count,r.shared_accession_count,
                  r.association_sha256
           FROM consumer_defensive_sec_reconciliation_state r
           JOIN consumer_defensive_sec_cache_snapshot s USING(asof_date)
           WHERE r.status='complete' AND r.asof_date=?
             AND r.scope_contract_version=3
             AND s.scope_contract_version=3
             AND r.trust_state='trusted_current'
             AND s.trust_state='trusted_current'
             AND r.scope_issuer_count=?
             AND r.cache_manifest_sha256=s.cache_manifest_sha256
             AND r.cache_manifest_json=s.cache_manifest_json
             AND r.ingestion_config_sha256=?
             AND s.ingestion_config_sha256=?
             AND r.issuer_scope_sha256=?
             AND s.issuer_scope_sha256=?''',
        (asof_date,scope_count,expected,expected,scope_sha256,scope_sha256),
    ).fetchone()
    if seal is None or not _cache_seal_valid(seal, cache_dir=cache_dir):
        raise RuntimeError(
            'Consumer Defensive parser catalog requires an exact complete '
            'full-scope SEC reconciliation seal and immutable cache'
        )
    cutoff = asof_date + 'T23:59:59Z'
    association_manifest = _consumer_defensive_association_manifest(
        conn, cutoff=cutoff
    )
    sealed_association_manifest = {
        'association_count': int(seal[7]),
        'accession_count': int(seal[8]),
        'shared_accession_count': int(seal[9]),
        'association_sha256': str(seal[10]),
    }
    if association_manifest != sealed_association_manifest:
        raise RuntimeError(
            'Consumer Defensive parser catalog association manifest does not '
            'match the exact reconciliation seal'
        )
    missing_lifecycle = int(conn.execute('''SELECT COUNT(*)
        FROM bridge_sec_filing_company b
        JOIN fact_sec_filing f ON f.accession_number=b.accession_number
        WHERE f.accepted_at<=? AND NOT EXISTS(
          SELECT 1 FROM sec_filing_company_association_event e
          WHERE e.accession_number=b.accession_number
            AND e.issuer_company_id=b.issuer_company_id
            AND e.effective_asof<=?)''',(cutoff,cutoff)).fetchone()[0])
    if missing_lifecycle:
        raise RuntimeError(
            'Consumer Defensive parser catalog requires complete association '
            f'lifecycle events; missing={missing_lifecycle}'
        )
    lifecycle_mismatches = 0
    for event in conn.execute('''SELECT e.accession_number,e.issuer_company_id,
        e.issuer_ticker,e.issuer_cik,e.effective_asof,e.event_type,e.reason,
        e.event_sha256
        FROM sec_filing_company_association_event e
        WHERE e.effective_asof<=? ORDER BY e.event_id''',(cutoff,)):
        expected_event_hash = hashlib.sha256(json.dumps(
            [str(event[0]),int(event[1]),str(event[2]),str(event[3]),
             str(event[4]),str(event[5]),str(event[6])],
            ensure_ascii=True,separators=(',', ':'),
        ).encode()).hexdigest()
        lifecycle_mismatches += int(
            str(event[7]) != expected_event_hash
        )
    if lifecycle_mismatches:
        raise RuntimeError(
            'Consumer Defensive parser catalog requires exact association '
            f'lifecycle identity and hashes; mismatches={lifecycle_mismatches}'
        )


DOCUMENT_SUFFIXES = SEC_DOCUMENT_SUFFIXES
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
    archive_cik = filing.archive_cik or filing.cik
    if not re.fullmatch(r"\d{1,10}", str(archive_cik)):
        raise ValueError(f"Invalid SEC archive CIK: {archive_cik!r}")
    if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", filing.accession_number):
        raise ValueError(
            f"Invalid SEC accession number: {filing.accession_number!r}"
        )
    cache_root = resolve_filesystem_path(Path(cache_dir), strict=False)
    lexical_directory = (
        cache_root
        / "sec_archive_xbrl"
        / f"CIK{archive_cik}"
        / filing.accession_number.replace("-", "")
    )
    directory = resolve_filesystem_path(lexical_directory, strict=False)
    try:
        directory.relative_to(cache_root)
    except ValueError as exc:
        raise ValueError("SEC accession directory escapes the cache root") from exc
    if directory != lexical_directory:
        raise ValueError(
            "SEC accession directory contains a symlinked identity component"
        )
    return directory


def _index_items(accession_dir: Path) -> list[dict[str, str]]:
    index_path = resolve_sec_document_path(
        accession_dir, "index.json",
        allowed_suffixes=SEC_SUBMISSIONS_ARCHIVE_SUFFIXES,
        containment_root=accession_dir,
        context="SEC filing index filename",
    )
    if not path_exists(index_path):
        return []
    try:
        with open_path(index_path, 'r', encoding='utf-8') as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    output: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for item in ((payload.get("directory") or {}).get("item") or []):
        if not isinstance(item, dict):
            continue
        raw_name = item.get("name")
        name = str(raw_name or "")
        if not name:
            continue
        name = validate_sec_relative_document_path(
            raw_name,
            allowed_suffixes=(
                SEC_ARCHIVE_ENTRY_SUFFIXES | SEC_PRIMARY_DOCUMENT_SUFFIXES
            ),
            context="SEC filing index document name",
        )
        if Path(name).suffix.casefold() not in SEC_ARCHIVE_ENTRY_SUFFIXES:
            # SEC index metadata includes non-parser `.paper` wrappers. Keep
            # validating their path safety, but never schedule those bytes.
            continue
        folded = name.casefold()
        if folded in seen_names:
            raise ValueError(
                f'Duplicate case-insensitive SEC index document name: {name!r}'
            )
        seen_names.add(folded)
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
    path = resolve_sec_document_path(
        accession_dir, "FilingSummary.xml",
        containment_root=accession_dir,
        context="SEC FilingSummary filename",
    )
    if not path_exists(path):
        return set()
    try:
        with open_path(path, 'r', encoding='utf-8', errors='replace') as handle:
            root = ET.fromstring(handle.read())
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
        if document_name:
            document_name = validate_sec_relative_document_path(
                document_name,
                allowed_suffixes=SEC_ARCHIVE_ENTRY_SUFFIXES,
                context="SEC FilingSummary HtmlFileName",
            )
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
    validate_sec_document_basename(
        expected, context="SEC full-submission filename"
    )
    expected_path = resolve_sec_document_path(
        accession_dir, expected, containment_root=accession_dir,
        context="SEC full-submission filename",
    )
    if is_file_path(expected_path):
        return expected
    compact = accession_number.replace("-", "")
    candidates: list[str] = []
    for path in filesystem_path(accession_dir).glob("*.txt"):
        name = validate_sec_document_basename(
            path.name, context="SEC full-submission filename"
        )
        resolved = resolve_sec_document_path(
            accession_dir, name, containment_root=accession_dir,
            require_file=True, context="SEC full-submission filename",
        )
        if compact in resolved.stem.replace("-", ""):
            candidates.append(name)
    candidates.sort()
    return candidates[0] if candidates else ""


def relevant_document_names(
    accession_dir: Path,
    *,
    filing: FilingRef,
    keywords: tuple[str, ...],
) -> tuple[str, ...]:
    primary_document = validate_sec_relative_document_path(
        filing.primary_document, allowed_suffixes=SEC_PRIMARY_DOCUMENT_SUFFIXES,
        context="SEC filing primary_document",
    )
    index_items = _index_items(accession_dir)
    available = {item["name"] for item in index_items}
    for path in filesystem_path(accession_dir).iterdir():
        if path.suffix.casefold() not in SEC_ARCHIVE_ENTRY_SUFFIXES:
            continue
        name = validate_sec_document_basename(
            path.name, allowed_suffixes=SEC_ARCHIVE_ENTRY_SUFFIXES,
            context="SEC accession-directory entry",
        )
        resolve_sec_document_path(
            accession_dir, name, allowed_suffixes=SEC_ARCHIVE_ENTRY_SUFFIXES,
            containment_root=accession_dir, require_file=True,
            context="SEC accession-directory entry",
        )
        available.add(name)
    folded_available: dict[str, str] = {}
    for name in available:
        previous = folded_available.get(name.casefold())
        if previous is not None and previous != name:
            raise ValueError(
                'Case-insensitive SEC accession-document collision: '
                f'{previous!r} and {name!r}'
            )
        folded_available[name.casefold()] = name
    selected = set()
    if Path(primary_document).suffix.casefold() in SEC_DOCUMENT_SUFFIXES:
        selected.add(primary_document)
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
    names: list[str] = []
    selected_casefold: set[str] = set()
    for raw_name in selected:
        if not raw_name:
            continue
        name = validate_sec_relative_document_path(
            raw_name, allowed_suffixes=SEC_ARCHIVE_ENTRY_SUFFIXES,
            context="selected SEC filing document",
        )
        # SEC index metadata often labels earnings-related image assets with the
        # same words as the actual release. They are valid archive entries, but
        # they are not parser documents. Keep broad validation above so unsafe
        # archive names still fail closed; only enqueue parseable formats.
        if Path(name).suffix.casefold() not in SEC_DOCUMENT_SUFFIXES:
            continue
        if name.casefold() in selected_casefold:
            raise ValueError(
                f'Duplicate case-insensitive selected SEC document: {name!r}'
            )
        selected_casefold.add(name.casefold())
        path = resolve_sec_relative_document_path(
            accession_dir, name, allowed_suffixes=SEC_ARCHIVE_ENTRY_SUFFIXES,
            containment_root=accession_dir,
            context="selected SEC filing document",
        )
        if is_file_path(path) and not any(
            marker in name.lower() for marker in EXCLUDED_MARKERS
        ):
            names.append(name)
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
    stat = stat_path(path)
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
            str(resolve_filesystem_path(path, strict=True)),
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
    required_documents: Iterable[str] | None = None,
) -> tuple[DocumentRef, ...]:
    validate_sec_relative_document_path(
        filing.primary_document, allowed_suffixes=SEC_PRIMARY_DOCUMENT_SUFFIXES,
        context="SEC filing primary_document",
    )
    directory = accession_directory(cache_dir, filing)
    if not path_exists(directory):
        return ()
    names = (
        tuple(
            sorted(
                {
                    validate_sec_relative_document_path(
                        name, allowed_suffixes=SEC_ARCHIVE_ENTRY_SUFFIXES,
                        context="required SEC parser document",
                    )
                    for name in required_documents
                },
                key=str.lower,
            )
        )
        if required_documents is not None
        else relevant_document_names(
            directory,
            filing=filing,
            keywords=keywords,
        )
    )
    folded_names = [name.casefold() for name in names]
    if len(folded_names) != len(set(folded_names)):
        raise ValueError('Required SEC parser documents collide case-insensitively')
    full_submission = _full_submission_name(
        directory,
        accession_number=filing.accession_number,
    )
    if (
        required_documents is None
        and max_documents > 0
        and len(names) > max_documents
    ):
        truncated = list(names[:max_documents])
        # The sort places the full submission last, so a plain cap silently
        # drops it and the edgartools SGML inspection never runs. Reserve a
        # slot for it instead.
        if full_submission and full_submission in names and full_submission not in truncated:
            truncated[-1] = full_submission
        names = tuple(truncated)
    documents: list[DocumentRef] = []
    for name in names:
        path = resolve_sec_relative_document_path(
            directory, name, allowed_suffixes=SEC_ARCHIVE_ENTRY_SUFFIXES,
            containment_root=cache_dir,
            context="SEC parser document", require_file=False,
        )
        if not is_file_path(path):
            continue
        path = resolve_sec_relative_document_path(
            directory, name, allowed_suffixes=SEC_ARCHIVE_ENTRY_SUFFIXES,
            containment_root=cache_dir,
            context="SEC parser document", require_file=True,
        )
        stat = stat_path(path)
        # Size/mtime are diagnostics only. Hash bytes on every plan so equal-size
        # tampering with a restored timestamp cannot reuse a stale work key.
        content_hash = file_sha256(path)
        documents.append(
            DocumentRef(
                name=name,
                path=str(runtime_path(path)),
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
    cache_dir: Path | None = None,
    expected_ingestion_config_sha256: str | None = None,
) -> dict[str, list[FilingRef]]:
    ticker_list = sorted(set(tickers))
    if not ticker_list:
        return {}
    ticker_placeholders = ",".join("?" for _ in ticker_list)
    form_placeholders = ",".join("?" for _ in supported_forms)
    accession_list = sorted(set(accessions or ()))
    pit_cutoff = asof_date + 'T23:59:59Z'
    accession_filter = ""
    accession_params: tuple[str, ...] = ()
    if accession_list:
        accession_placeholders = ",".join("?" for _ in accession_list)
        accession_filter = (
            f" AND f.accession_number IN ({accession_placeholders})"
        )
        accession_params = tuple(accession_list)
    if model_family == 'consumer_defensive':
        validate_consumer_defensive_catalog_contract(
            conn,asof_date=asof_date,
            expected_ingestion_config_sha256=expected_ingestion_config_sha256,
            cache_dir=cache_dir,
        )
    parser_view = conn.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type='view' AND name='consumer_defensive_sec_parser_filing_input'
        """
    ).fetchone()
    use_consumer_defensive_view = model_family == "consumer_defensive" and parser_view is not None
    company_table = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'dim_company'
        """
    ).fetchone()
    company_join = (
        "LEFT JOIN dim_company AS c ON c.ticker = f.ticker"
        if company_table is not None and not use_consumer_defensive_view
        else ""
    )
    currency_expression = (
        "f.company_currency"
        if use_consumer_defensive_view
        else
        "COALESCE(NULLIF(c.currency, ''), 'USD')"
        if company_table is not None
        else "'USD'"
    )
    filing_source = (
        "consumer_defensive_sec_parser_filing_input"
        if use_consumer_defensive_view
        else "fact_sec_filing"
    )
    archive_cik_expression = "f.archive_cik" if use_consumer_defensive_view else "f.cik"
    lifecycle_filter = (
        '''AND (SELECT e.event_type
           FROM sec_filing_company_association_event e
           WHERE e.accession_number=f.accession_number
             AND e.issuer_company_id=f.issuer_company_id
             AND e.effective_asof<=?
           ORDER BY e.effective_asof DESC,e.event_id DESC LIMIT 1)
           IN ('observed','reactivated')'''
        if use_consumer_defensive_view
        else ''
    )
    rows = conn.execute(
        f"""
        SELECT DISTINCT f.ticker, f.cik, {archive_cik_expression} AS archive_cik,
               f.accession_number, f.form_type,
               f.filing_date, f.accepted_at, f.report_date,
               f.primary_document, f.source_id,
               {currency_expression} AS company_currency
        FROM {filing_source} AS f
        {company_join}
        WHERE f.ticker IN ({ticker_placeholders})
          AND UPPER(f.form_type) IN ({form_placeholders})
          {lifecycle_filter}
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
            *((pit_cutoff,) if use_consumer_defensive_view else ()),
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
            if use_consumer_defensive_view:
                # Consumer Defensive seals reporting currency in issuer-scope
                # identity v3. Do not let mutable downstream feature rows
                # silently alter parser semantics under an unchanged seal.
                reporting_currencies[ticker] = str(
                    row["company_currency"] or "USD"
                ).strip().upper() or "USD"
            else:
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
                archive_cik=str(row["archive_cik"] or row["cik"] or ""),
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

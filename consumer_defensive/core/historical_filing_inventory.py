from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sqlite3
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable

from dedicated_parser.path_io import path_exists, read_bytes
from dedicated_parser.sec_paths import (
    SEC_ARCHIVE_ENTRY_SUFFIXES,
    SEC_DOCUMENT_SUFFIXES,
    SEC_SUBMISSIONS_ARCHIVE_SUFFIXES,
    quote_sec_relative_document_path,
    resolve_sec_relative_document_path,
    validate_sec_relative_document_path,
)

from .config import ConfigBundle, cfg_get
from .db import utc_now
from .market_data import write_csv, write_json
from .specialized_metrics import (
    _applicable_metric_ids,
    _metric_registry,
    _taxonomy,
    bootstrap_stage6b,
    _trusted_seal,
)


CORE_FORM_FAMILIES = {
    '10-K': 'annual',
    '10-K/A': 'annual',
    '10-KT': 'annual',
    '10-KT/A': 'annual',
    '10-Q': 'quarterly',
    '10-Q/A': 'quarterly',
    '10-QT': 'quarterly',
    '10-QT/A': 'quarterly',
    '20-F': 'annual',
    '20-F/A': 'annual',
    '40-F': 'annual',
    '40-F/A': 'annual',
}
EVENT_FORMS = frozenset({'8-K', '8-K/A', '6-K', '6-K/A'})
CAPTURE_FORMS = frozenset({*CORE_FORM_FAMILIES, *EVENT_FORMS})
INVENTORY_POLICY_VERSION = 'consumer_defensive_historical_inventory_v1'
EVENT_DOCUMENT_POLICY_VERSION = 'consumer_defensive_event_documents_v4'
_ACCESSION_RE = re.compile(r'^[0-9]{10}-[0-9]{2}-[0-9]{6}$')
_EVENT_EXHIBIT_TYPE_RE = re.compile(r'^EX-99(?:\.|$)', re.IGNORECASE)
_EVENT_DESCRIPTION_RE = re.compile(
    r'\b(?:earnings?|financial results?|results? of operations|'
    r'press release|news release|investor presentation|supplemental|'
    r'quarterly results?|annual results?)\b',
    re.IGNORECASE,
)


def _event_document_policy_sha256(maximum_documents: int) -> str:
    return hashlib.sha256(_canonical_json({
        'version': EVENT_DOCUMENT_POLICY_VERSION,
        'maximum_documents_per_filing': maximum_documents,
        'exhibit_type_pattern': _EVENT_EXHIBIT_TYPE_RE.pattern,
        'description_pattern': _EVENT_DESCRIPTION_RE.pattern,
        'allowed_document_suffixes': sorted(SEC_DOCUMENT_SUFFIXES),
        'selection_order': 'primary_then_ex99_then_contextual_index_order',
        'membership_authority': (
            'directory_index_or_exact_same_accession_direct_or_ix_href'
        ),
    }).encode()).hexdigest()


def _event_index_url(cik: str, accession_number: str) -> str:
    normalized_cik = str(cik).strip()
    if not normalized_cik.isdigit() or len(normalized_cik) > 10:
        raise ValueError(f'Invalid event archive CIK: {cik!r}')
    if not _ACCESSION_RE.fullmatch(accession_number):
        raise ValueError(f'Invalid event accession number: {accession_number!r}')
    return (
        'https://www.sec.gov/Archives/edgar/data/'
        f'{int(normalized_cik)}/{accession_number.replace("-", "")}/index.json'
    )


def _parse_event_index(payload: bytes, *, logical_path: str) -> tuple[dict[str, str], ...]:
    if not payload or not payload.strip():
        raise ValueError(f'Event filing index is empty: {logical_path}')
    try:
        root = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'Event filing index is invalid JSON: {logical_path}') from exc
    items = root.get('directory', {}).get('item') if isinstance(root, dict) else None
    if not isinstance(items, list):
        raise ValueError(f'Event filing index lacks directory.item: {logical_path}')
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for sequence, raw in enumerate(items, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f'Event filing index item is not an object: {logical_path}')
        name = validate_sec_relative_document_path(
            raw.get('name'), allowed_suffixes=SEC_ARCHIVE_ENTRY_SUFFIXES,
            context=f'Event filing index item in {logical_path}',
        )
        folded = name.casefold()
        if folded in seen:
            raise ValueError(f'Duplicate event filing index document: {name!r}')
        seen.add(folded)
        output.append({
            'name': name,
            'type': str(raw.get('type') or '').strip().upper(),
            'description': str(raw.get('description') or '').strip(),
            'sequence': str(raw.get('sequence') or sequence).strip(),
        })
    return tuple(output)


class _EventFilingIndexHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[str, ...]] = []
        self.row_hrefs: list[tuple[str, ...]] = []
        self._row: list[str] | None = None
        self._row_hrefs: list[str] | None = None
        self._cell: list[str] | None = None
        self._anchor: list[str] | None = None
        self._anchor_hrefs: list[str] | None = None
        self._inside_anchor = False

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        lowered = tag.casefold()
        if lowered == 'tr':
            self._row = []
            self._row_hrefs = []
        elif lowered in {'td', 'th'} and self._row is not None:
            self._cell = []
            self._anchor = None
            self._anchor_hrefs = []
            self._inside_anchor = False
        elif lowered == 'a' and self._cell is not None:
            self._anchor = []
            assert self._anchor_hrefs is not None
            hrefs = [
                value for key, value in attrs
                if key.casefold() == 'href' and value is not None
            ]
            self._anchor_hrefs.extend(hrefs)
            self._inside_anchor = True

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)
            if self._inside_anchor and self._anchor is not None:
                self._anchor.append(data)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered == 'a':
            self._inside_anchor = False
        elif lowered in {'td', 'th'} and self._cell is not None:
            assert self._row is not None
            assert self._row_hrefs is not None
            source = self._anchor if self._anchor else self._cell
            self._row.append(' '.join(''.join(source).split()))
            hrefs = self._anchor_hrefs or []
            self._row_hrefs.append(hrefs[0] if len(hrefs) == 1 else '')
            self._cell = None
            self._anchor = None
            self._anchor_hrefs = None
            self._inside_anchor = False
        elif lowered == 'tr' and self._row is not None:
            if self._row:
                self.rows.append(tuple(self._row))
                assert self._row_hrefs is not None
                self.row_hrefs.append(tuple(self._row_hrefs))
            self._row = None
            self._row_hrefs = None
            self._cell = None
            self._anchor = None
            self._anchor_hrefs = None
            self._inside_anchor = False


def _parse_event_filing_index_html(
    payload: bytes,
    *,
    logical_path: str,
) -> tuple[dict[str, str], ...]:
    if not payload or not payload.strip():
        raise ValueError(f'Event filing index HTML is empty: {logical_path}')
    parser = _EventFilingIndexHTMLParser()
    try:
        parser.feed(payload.decode('utf-8', errors='replace'))
        parser.close()
    except (AssertionError, UnicodeError, ValueError) as exc:
        raise ValueError(
            f'Event filing index HTML is invalid: {logical_path}'
        ) from exc
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for row, row_hrefs in zip(parser.rows, parser.row_hrefs, strict=True):
        if len(row) < 4:
            continue
        sequence, description, name, sec_type = row[:4]
        if name.casefold() in {'document', 'document format files'}:
            continue
        normalized_name = validate_sec_relative_document_path(
            name,
            allowed_suffixes=SEC_ARCHIVE_ENTRY_SUFFIXES,
            context=f'Event filing index HTML item in {logical_path}',
        )
        folded = normalized_name.casefold()
        if folded in seen:
            raise ValueError(
                f'Duplicate event filing index HTML document: {normalized_name!r}'
            )
        seen.add(folded)
        output.append({
            'name': normalized_name,
            'type': sec_type.strip().upper(),
            'description': description.strip(),
            'sequence': sequence.strip(),
            'href': row_hrefs[2] if len(row_hrefs) > 2 else '',
        })
    if not output:
        raise ValueError(
            f'Event filing index HTML contains no document rows: {logical_path}'
        )
    return tuple(output)


def _event_item_has_exact_same_accession_href(
    item: dict[str, str],
    *,
    archive_cik: str,
    accession_number: str,
) -> bool:
    '''Prove an HTML-index item points to this exact SEC accession member.

    Some historical EDGAR ``index.json`` responses omit exhibits that remain
    listed in, and directly retrievable from, the accession's filing-index
    HTML.  The HTML is a valid secondary membership authority only when its
    anchor target is an exact canonical SEC path for the same CIK, accession,
    and already-validated document name.  The only query form admitted is the
    SEC's exact one-parameter iXBRL viewer wrapper, ``/ix?doc=<exact path>``.
    Extra parameters, fragments, alternate hosts, traversal, and
    cross-accession links therefore remain fail-closed.
    '''
    cik = str(archive_cik).strip()
    if not cik.isdigit() or len(cik) > 10:
        raise ValueError(f'Invalid event archive CIK: {archive_cik!r}')
    if not _ACCESSION_RE.fullmatch(accession_number):
        raise ValueError(
            f'Invalid event accession number: {accession_number!r}'
        )
    name = validate_sec_relative_document_path(
        item.get('name'),
        allowed_suffixes=SEC_DOCUMENT_SUFFIXES,
        context='Event filing index HTML membership document',
    )
    href = item.get('href')
    if not isinstance(href, str) or not href:
        return False
    quoted_name = quote_sec_relative_document_path(
        name,
        allowed_suffixes=SEC_DOCUMENT_SUFFIXES,
        context='Event filing index HTML membership URL',
    )
    exact_path = (
        f'/Archives/edgar/data/{int(cik)}/'
        f'{accession_number.replace("-", "")}/{quoted_name}'
    )
    exact_hrefs = {
        exact_path,
        f'https://www.sec.gov{exact_path}',
        f'/ix?doc={exact_path}',
        f'https://www.sec.gov/ix?doc={exact_path}',
    }
    return href in exact_hrefs


def _validate_selected_event_document_membership(
    directory_items: Iterable[dict[str, str]],
    selected_items: Iterable[dict[str, str]],
    *,
    archive_cik: str,
    accession_number: str,
    context: str,
) -> tuple[str, ...]:
    '''Return audited JSON-index discrepancies or reject unbound documents.'''
    directory_names = {
        str(item['name']).casefold() for item in directory_items
    }
    discrepancies: list[str] = []
    unbound: list[str] = []
    for item in selected_items:
        name = str(item['name'])
        if name.casefold() in directory_names:
            continue
        if _event_item_has_exact_same_accession_href(
            item,
            archive_cik=archive_cik,
            accession_number=accession_number,
        ):
            discrepancies.append(name)
        else:
            unbound.append(name)
    if unbound:
        raise RuntimeError(
            'Filing index HTML selected documents absent from the exact '
            'directory index without an exact same-accession SEC href: '
            f'{context}: {unbound}'
        )
    return tuple(discrepancies)


def _event_filing_index_name(
    items: Iterable[dict[str, str]],
    *,
    accession_number: str,
) -> str:
    expected = f'{accession_number}-index.html'.casefold()
    matches = [
        str(item['name']) for item in items
        if str(item['name']).casefold() == expected
    ]
    if len(matches) != 1:
        raise ValueError(
            'Event directory index must contain exactly one canonical filing '
            f'index HTML document for {accession_number}.'
        )
    return matches[0]


def _select_event_documents(
    items: Iterable[dict[str, str]],
    *,
    primary_document: str,
    maximum_documents: int,
) -> tuple[dict[str, str], ...]:
    if maximum_documents < 1:
        raise ValueError('Event document limit must be positive.')
    primary = validate_sec_relative_document_path(
        primary_document, allowed_suffixes=SEC_DOCUMENT_SUFFIXES,
        context='Event filing primary document',
    )
    selected: list[tuple[int, int, dict[str, str]]] = []
    for position, item in enumerate(items):
        name = str(item['name'])
        suffix = Path(name).suffix.casefold()
        if suffix not in SEC_DOCUMENT_SUFFIXES:
            continue
        sec_type = str(item.get('type') or '')
        description = str(item.get('description') or '')
        is_primary = name.casefold() == primary.casefold()
        is_exhibit = bool(_EVENT_EXHIBIT_TYPE_RE.match(sec_type))
        contextual = bool(_EVENT_DESCRIPTION_RE.search(f'{description} {name}'))
        if not (is_primary or is_exhibit or contextual):
            continue
        role = 'primary_event_filing' if is_primary else (
            'earnings_exhibit' if is_exhibit else 'results_supplement'
        )
        normalized = {**item, 'document_role': role}
        selected.append((0 if is_primary else 1 if is_exhibit else 2, position, normalized))
    selected.sort(key=lambda value: (value[0], value[1], value[2]['name'].casefold()))
    primary_rows = [row for _, _, row in selected if row['document_role'] == 'primary_event_filing']
    if len(primary_rows) != 1:
        raise ValueError(
            f'Event filing index must contain its exact primary document: {primary!r}'
        )
    retained = [primary_rows[0]]
    retained.extend(
        row for _, _, row in selected
        if row['document_role'] != 'primary_event_filing'
    )
    return tuple(retained[:maximum_documents])


@dataclass(frozen=True)
class FilingRow:
    ticker: str
    accession_number: str
    form_type: str
    accepted_at: str
    filing_date: str
    report_date: str
    primary_document: str
    existing_hydration_status: str

    @property
    def asof_date(self) -> str:
        return self.accepted_at[:10]


@dataclass(frozen=True)
class HistoricalReplayTarget:
    ticker: str
    accession_number: str
    primary_document: str
    replay_sequence: int
    replay_asof_date: str
    capture_rank: int


@dataclass(frozen=True)
class HistoricalReplayStep:
    replay_sequence: int
    asof_date: str
    targets: tuple[HistoricalReplayTarget, ...]


@dataclass(frozen=True)
class HistoricalReplayPlan:
    inventory_path: Path
    schedule_path: Path
    steps: tuple[HistoricalReplayStep, ...]
    target_filing_count: int
    event_index_candidate_count: int


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def _policy_sha256(*, start: str, end: str, maximum_documents: int) -> str:
    payload = {
        'version': INVENTORY_POLICY_VERSION,
        'history_start': start,
        'history_end': end,
        'maximum_documents_per_issuer': maximum_documents,
        'core_form_families': sorted(CORE_FORM_FAMILIES.items()),
        'event_forms': sorted(EVENT_FORMS),
        'base_replay_frequency': 'calendar_month',
        'adaptive_capture_proof': True,
        'event_document_policy': 'index_and_exhibit_discovery_required',
    }
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _filing_rows(
    conn: sqlite3.Connection,
    *,
    history_start: str,
    history_end: str,
) -> list[FilingRow]:
    cutoff = history_end + 'T23:59:59Z'
    rows: list[FilingRow] = []
    for row in conn.execute(
        '''
        SELECT b.issuer_ticker,b.accession_number,b.form_type,f.accepted_at,
               b.filing_date,b.report_date,b.primary_document,
               COALESCE((
                   SELECT CASE
                       WHEN d.hydration_status='hydrated'
                            AND COALESCE(d.content_sha256,'')<>'' THEN 'hydrated'
                       ELSE d.hydration_status
                   END
                   FROM bridge_sec_filing_document_company d
                   WHERE d.accession_number=b.accession_number
                     AND d.issuer_company_id=b.issuer_company_id
                   ORDER BY d.updated_at DESC,d.source_id LIMIT 1
               ),'not_hydrated') AS existing_hydration_status
        FROM bridge_sec_filing_company b
        JOIN fact_sec_filing f ON f.accession_number=b.accession_number
        JOIN dim_consumer_defensive_taxonomy t
          ON t.ticker=b.issuer_ticker
         AND t.model_family='consumer_defensive'
        WHERE f.accepted_at>=? AND f.accepted_at<=?
          AND b.form_type IN (
              '10-K','10-K/A','10-KT','10-KT/A',
              '10-Q','10-Q/A','10-QT','10-QT/A',
              '20-F','20-F/A','40-F','40-F/A',
              '8-K','8-K/A','6-K','6-K/A'
          )
          AND COALESCE(b.primary_document,'')<>''
          AND COALESCE((
              SELECT e.event_type
              FROM sec_filing_company_association_event e
              WHERE e.accession_number=b.accession_number
                AND e.issuer_company_id=b.issuer_company_id
                AND e.effective_asof<=?
              ORDER BY e.effective_asof DESC,e.event_id DESC LIMIT 1
          ),'missing') IN ('observed','reactivated')
        ORDER BY b.issuer_ticker,f.accepted_at,b.accession_number
        ''',
        (history_start + 'T00:00:00Z', cutoff, cutoff),
    ):
        rows.append(FilingRow(
            ticker=str(row['issuer_ticker']),
            accession_number=str(row['accession_number']),
            form_type=str(row['form_type']),
            accepted_at=str(row['accepted_at']),
            filing_date=str(row['filing_date'] or ''),
            report_date=str(row['report_date'] or ''),
            primary_document=str(row['primary_document']),
            existing_hydration_status=str(
                row['existing_hydration_status'] or 'not_hydrated'
            ),
        ))
    identities = {
        (row.ticker, row.accession_number) for row in rows
    }
    if len(identities) != len(rows):
        raise RuntimeError('Historical filing inventory contains duplicate issuer filings.')
    return rows


def _base_cutoffs(targets: Iterable[FilingRow]) -> set[str]:
    monthly: dict[str, str] = {}
    for row in targets:
        month = row.asof_date[:7]
        monthly[month] = max(monthly.get(month, ''), row.asof_date)
    return set(monthly.values())


def _selection_at_cutoff(
    rows_by_ticker: dict[str, list[FilingRow]],
    *,
    cutoff: str,
    maximum_documents: int,
) -> dict[tuple[str, str], int]:
    selected: dict[tuple[str, str], int] = {}
    cutoff_timestamp = cutoff + 'T23:59:59Z'
    for ticker, rows in rows_by_ticker.items():
        eligible = sorted(
            (
                row for row in rows
                if row.accepted_at <= cutoff_timestamp
                and row.form_type in CAPTURE_FORMS
            ),
            key=lambda row: (row.accepted_at, row.accession_number),
            reverse=True,
        )
        rank_before = 0
        offset = 0
        while offset < len(eligible) and rank_before < maximum_documents:
            accepted_at = eligible[offset].accepted_at
            tied: list[FilingRow] = []
            while (
                offset < len(eligible)
                and eligible[offset].accepted_at == accepted_at
            ):
                tied.append(eligible[offset])
                offset += 1
            worst_rank = rank_before + len(tied)
            if worst_rank > maximum_documents:
                break
            for row in tied:
                selected[(ticker, row.accession_number)] = worst_rank
            rank_before = worst_rank
    return selected


def _capture_schedule(
    all_rows: list[FilingRow],
    targets: list[FilingRow],
    *,
    maximum_documents: int,
) -> tuple[list[str], dict[tuple[str, str], tuple[str, int]]]:
    rows_by_ticker: dict[str, list[FilingRow]] = defaultdict(list)
    for row in all_rows:
        rows_by_ticker[row.ticker].append(row)
    cutoffs = _base_cutoffs(targets)
    unresolved = {(row.ticker, row.accession_number) for row in targets}
    captures: dict[tuple[str, str], tuple[str, int]] = {}
    while True:
        for cutoff in sorted(cutoffs):
            selection = _selection_at_cutoff(
                rows_by_ticker,
                cutoff=cutoff,
                maximum_documents=maximum_documents,
            )
            for key in sorted(unresolved & set(selection)):
                captures[key] = (cutoff, selection[key])
                unresolved.remove(key)
        additions = {
            row.asof_date
            for row in targets
            if (row.ticker, row.accession_number) in unresolved
            and row.asof_date not in cutoffs
        }
        if not additions:
            break
        cutoffs.update(additions)
    return sorted(cutoffs), captures


def build_historical_filing_inventory(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    generated_asof: str,
    history_start: str | None = None,
    history_end: str | None = None,
    maximum_documents_per_issuer: int | None = None,
) -> dict[str, Any]:
    bootstrap_stage6b(conn, bundle)
    start = history_start or str(
        cfg_get(bundle.payload, 'stage6b.historical_inventory_start')
    )
    end = history_end or generated_asof
    if date.fromisoformat(start) > date.fromisoformat(end):
        raise ValueError('Historical inventory start must not exceed its end.')
    maximum_documents = int(
        maximum_documents_per_issuer
        or cfg_get(bundle.payload, 'sec_fundamentals.documents_per_issuer')
    )
    if maximum_documents < 1:
        raise ValueError('Historical inventory maximum_documents_per_issuer must be positive.')

    rows = _filing_rows(
        conn,
        history_start=start,
        history_end=end,
    )
    targets = [row for row in rows if row.form_type in CORE_FORM_FAMILIES]
    events = [row for row in rows if row.form_type in EVENT_FORMS]
    cutoffs, captures = _capture_schedule(
        rows,
        targets,
        maximum_documents=maximum_documents,
    )
    cutoff_sequence = {
        cutoff: sequence for sequence, cutoff in enumerate(cutoffs, start=1)
    }
    taxonomy = _taxonomy(conn)
    _, metrics = _metric_registry(bundle)
    policy_sha = _policy_sha256(
        start=start,
        end=end,
        maximum_documents=maximum_documents,
    )
    now = utc_now()
    inventory_rows: list[dict[str, Any]] = []
    for row in [*targets, *events]:
        member = taxonomy[row.ticker]
        requested = _applicable_metric_ids(
            (metric for metric in metrics if metric.sec_addressable),
            cohort_id=member['cohort_id'],
            subtype=member['subtype'],
        )
        key = (row.ticker, row.accession_number)
        capture = captures.get(key)
        is_event = row.form_type in EVENT_FORMS
        if is_event:
            status = 'requires_filing_index_discovery'
        elif row.existing_hydration_status == 'hydrated':
            status = 'already_hydrated'
        elif capture is not None:
            status = 'planned_chronological_replay'
        else:
            status = 'requires_targeted_hydration'
        inventory_rows.append({
            'ticker': row.ticker,
            'accession_number': row.accession_number,
            'form_type': row.form_type,
            'form_family': (
                CORE_FORM_FAMILIES[row.form_type]
                if not is_event else 'event_report'
            ),
            'filing_date': row.filing_date,
            'accepted_at': row.accepted_at,
            'report_date': row.report_date,
            'primary_document': row.primary_document,
            'replay_sequence': (
                cutoff_sequence[capture[0]] if capture is not None else None
            ),
            'replay_asof_date': capture[0] if capture is not None else None,
            'capture_rank': capture[1] if capture is not None else None,
            'target_reason': (
                'periodic_financial_filing'
                if not is_event
                else 'event_filing_requires_index_and_exhibit_classification'
            ),
            'existing_hydration_status': row.existing_hydration_status,
            'inventory_status': status,
            'requires_index_discovery': int(is_event),
            'requested_metrics_json': _canonical_json(requested),
            'created_at': now,
        })
    uncovered = [
        row for row in inventory_rows
        if row['form_family'] != 'event_report'
        and row['inventory_status'] == 'requires_targeted_hydration'
    ]
    tickers_with_targets = {row.ticker for row in targets}
    missing_tickers = sorted(set(taxonomy) - tickers_with_targets)
    metadata = {
        'policy_version': INVENTORY_POLICY_VERSION,
        'core_target_count': len(targets),
        'event_index_candidate_count': len(events),
        'ticker_count': len(tickers_with_targets),
        'taxonomy_ticker_count': len(taxonomy),
        'tickers_without_core_target': missing_tickers,
        'chronological_replay_required': True,
        'fresh_database_required_for_backfill': True,
        'production_database_mutation_authorized': False,
    }
    status = 'PASS' if not uncovered else 'FAIL_UNCOVERED_CORE_FILINGS'
    with conn:
        conn.execute(
            '''
            INSERT INTO stage6b_historical_inventory_run(
                generated_asof,history_start,history_end,
                maximum_documents_per_issuer,selection_policy_sha256,status,
                replay_cutoff_count,target_filing_count,
                uncovered_target_count,metadata_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(
                generated_asof,history_start,history_end,
                maximum_documents_per_issuer,selection_policy_sha256
            ) DO UPDATE SET
                status=excluded.status,
                replay_cutoff_count=excluded.replay_cutoff_count,
                target_filing_count=excluded.target_filing_count,
                uncovered_target_count=excluded.uncovered_target_count,
                metadata_json=excluded.metadata_json,
                created_at=excluded.created_at
            ''',
            (
                generated_asof, start, end, maximum_documents, policy_sha,
                status, len(cutoffs), len(targets), len(uncovered),
                _canonical_json(metadata), now,
            ),
        )
        run_row = conn.execute(
            '''
            SELECT inventory_run_id
            FROM stage6b_historical_inventory_run
            WHERE generated_asof=? AND history_start=? AND history_end=?
              AND maximum_documents_per_issuer=?
              AND selection_policy_sha256=?
            ''',
            (generated_asof, start, end, maximum_documents, policy_sha),
        ).fetchone()
        if run_row is None:
            raise RuntimeError('Historical inventory run identity was not persisted.')
        run_id = int(run_row[0])
        conn.execute(
            'DELETE FROM stage6b_historical_filing_inventory '
            'WHERE inventory_run_id=?',
            (run_id,),
        )
        conn.executemany(
            '''
            INSERT INTO stage6b_historical_filing_inventory(
                inventory_run_id,ticker,accession_number,form_type,form_family,
                filing_date,accepted_at,report_date,primary_document,
                replay_sequence,replay_asof_date,capture_rank,target_reason,
                existing_hydration_status,inventory_status,
                requires_index_discovery,requested_metrics_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''',
            [
                (
                    run_id,
                    row['ticker'],
                    row['accession_number'],
                    row['form_type'],
                    row['form_family'],
                    row['filing_date'],
                    row['accepted_at'],
                    row['report_date'],
                    row['primary_document'],
                    row['replay_sequence'],
                    row['replay_asof_date'],
                    row['capture_rank'],
                    row['target_reason'],
                    row['existing_hydration_status'],
                    row['inventory_status'],
                    row['requires_index_discovery'],
                    row['requested_metrics_json'],
                    row['created_at'],
                )
                for row in inventory_rows
            ],
        )
    schedule = []
    for cutoff in cutoffs:
        scheduled = [
            row for row in inventory_rows
            if row['replay_asof_date'] == cutoff
        ]
        schedule.append({
            'replay_sequence': cutoff_sequence[cutoff],
            'asof_date': cutoff,
            'target_filing_count': len(scheduled),
            'ticker_count': len({row['ticker'] for row in scheduled}),
            'maximum_capture_rank': max(
                (int(row['capture_rank']) for row in scheduled),
                default=0,
            ),
            'required_database_state': (
                'fresh_database_then_prior_sequence_complete'
            ),
        })
    return {
        'status': status,
        'inventory_run_id': run_id,
        'generated_asof': generated_asof,
        'history_start': start,
        'history_end': end,
        'maximum_documents_per_issuer': maximum_documents,
        'selection_policy_sha256': policy_sha,
        'replay_cutoff_count': len(cutoffs),
        'target_filing_count': len(targets),
        'captured_target_count': len(targets) - len(uncovered),
        'uncovered_target_count': len(uncovered),
        'event_index_candidate_count': len(events),
        'ticker_count': len(tickers_with_targets),
        'tickers_without_core_target': missing_tickers,
        'inventory_rows': inventory_rows,
        'replay_schedule': schedule,
    }


def write_historical_inventory_artifacts(
    result: dict[str, Any],
    *,
    output_dir: Path,
) -> None:
    write_csv(
        output_dir / 'consumer_defensive_historical_filing_inventory.csv',
        result['inventory_rows'],
    )
    write_csv(
        output_dir / 'consumer_defensive_historical_replay_schedule.csv',
        result['replay_schedule'],
    )
    summary = {
        key: value
        for key, value in result.items()
        if key not in {'inventory_rows', 'replay_schedule'}
    }
    write_json(
        output_dir / 'consumer_defensive_historical_inventory_summary.json',
        summary,
    )


def _valid_historical_document_payload(payload: bytes, *, logical_path: str) -> None:
    if not payload or not payload.strip():
        raise ValueError(f'Historical SEC document is empty: {logical_path}')
    head = payload[:8192].lower()
    blocked_markers = (
        b'your request originates from an undeclared automated tool',
        b'request rate threshold exceeded',
        b'access denied',
    )
    if any(marker in head for marker in blocked_markers):
        raise ValueError(
            f'Historical SEC document contains a provider error page: {logical_path}'
        )


def hydrate_historical_document_snapshot(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    as_of: str,
    inventory_run_id: int,
    cache_dir: Path,
    cache_only: bool = False,
    fetch: Callable[[str], bytes] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    '''Fetch each governed historical primary document once and seal the corpus.

    This is deliberately Stage 6B-owned.  It relies on one final trusted Stage 4
    filing/association projection, but it neither rewinds the Stage 4 watermark
    nor mutates Stage 4 filing, fact, document, profile, or reconciliation rows.
    '''
    from .stage4 import (
        _atomic_promote_bytes,
        _cache_manifest_record,
        _http_policy,
        _seal_cache_manifest,
        _sec_archive_url,
        _verify_cache_manifest,
        http_fetcher,
    )

    bootstrap_stage6b(conn, bundle)
    as_of = as_of[:10]
    date.fromisoformat(as_of)
    cache_dir = cache_dir.expanduser().resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    stage4_seal = _trusted_seal(
        conn, as_of=as_of, cache_dir=cache_dir
    )
    inventory_run = conn.execute(
        '''SELECT inventory_run_id,history_start,history_end,status,
                  target_filing_count,uncovered_target_count
           FROM stage6b_historical_inventory_run
           WHERE inventory_run_id=?''',
        (inventory_run_id,),
    ).fetchone()
    if inventory_run is None:
        raise ValueError(f'Unknown Stage 6B inventory run: {inventory_run_id}')
    if (
        str(inventory_run['status']) != 'PASS'
        or int(inventory_run['uncovered_target_count']) != 0
        or str(inventory_run['history_end']) > as_of
    ):
        raise RuntimeError(
            'Historical document hydration requires a complete inventory '
            'bounded by the trusted Stage 4 as-of date.'
        )

    existing = conn.execute(
        '''SELECT * FROM stage6b_historical_document_snapshot_run
           WHERE inventory_run_id=? AND asof_date=?''',
        (inventory_run_id, as_of),
    ).fetchone()
    if existing is not None and str(existing['status']) == 'PASS':
        seal_root = cache_dir / str(existing['seal_relative_path'])
        document_count = int(conn.execute(
            '''SELECT COUNT(*) FROM stage6b_historical_document_snapshot
               WHERE snapshot_run_id=?''',
            (int(existing['snapshot_run_id']),),
        ).fetchone()[0])
        if (
            document_count != int(existing['target_document_count'])
            or document_count != int(existing['hydrated_document_count'])
            or not _verify_cache_manifest(
                seal_root,
                str(existing['manifest_json']),
                str(existing['manifest_sha256']),
            )
        ):
            raise RuntimeError('Completed historical document snapshot is corrupt.')
        return {
            'status': 'PASS',
            'immutable_replay': True,
            'snapshot_run_id': int(existing['snapshot_run_id']),
            'inventory_run_id': inventory_run_id,
            'asof_date': as_of,
            'document_count': document_count,
            'manifest_sha256': str(existing['manifest_sha256']),
            'seal_relative_path': str(existing['seal_relative_path']),
        }

    cutoff = as_of + 'T23:59:59Z'
    targets = list(conn.execute(
        '''SELECT i.ticker,i.accession_number,i.form_type,i.filing_date,
                  i.accepted_at,i.report_date,i.primary_document,
                  i.requested_metrics_json,f.archive_cik,f.company_currency,
                  f.source_id,f.issuer_company_id
           FROM stage6b_historical_filing_inventory i
           JOIN consumer_defensive_sec_parser_filing_input f
             ON f.ticker=i.ticker AND f.accession_number=i.accession_number
           WHERE i.inventory_run_id=? AND i.form_family<>'event_report'
             AND f.accepted_at<=?
             AND (SELECT e.event_type
                  FROM sec_filing_company_association_event e
                  WHERE e.accession_number=f.accession_number
                    AND e.issuer_company_id=f.issuer_company_id
                    AND e.effective_asof<=?
                  ORDER BY e.effective_asof DESC,e.event_id DESC LIMIT 1)
                 IN ('observed','reactivated')
           ORDER BY i.ticker,i.accepted_at,i.accession_number''',
        (inventory_run_id, cutoff, cutoff),
    ))
    expected_count = int(inventory_run['target_filing_count'])
    if len(targets) != expected_count:
        raise RuntimeError(
            'Historical inventory no longer matches the exact active Stage 4 '
            f'filing projection: expected={expected_count} observed={len(targets)}'
        )
    for row in targets:
        if any((
            str(row['form_type']) not in CORE_FORM_FAMILIES,
            not str(row['primary_document'] or ''),
            not str(row['archive_cik'] or ''),
        )):
            raise RuntimeError(
                'Historical target has invalid governed filing metadata: '
                f"{row['ticker']}/{row['accession_number']}"
            )

    started_at = utc_now()
    with conn:
        conn.execute(
            '''INSERT INTO stage6b_historical_document_snapshot_run(
                   inventory_run_id,asof_date,history_start,history_end,status,
                   target_document_count,hydrated_document_count,
                   manifest_sha256,manifest_json,seal_relative_path,
                   ingestion_config_sha256,issuer_scope_sha256,started_at,
                   completed_at,metadata_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(inventory_run_id,asof_date) DO UPDATE SET
                   status='RUNNING',target_document_count=excluded.target_document_count,
                   hydrated_document_count=0,manifest_sha256='',manifest_json='[]',
                   seal_relative_path='',ingestion_config_sha256=excluded.ingestion_config_sha256,
                   issuer_scope_sha256=excluded.issuer_scope_sha256,
                   started_at=excluded.started_at,completed_at=NULL,metadata_json='{}' ''',
            (
                inventory_run_id, as_of, str(inventory_run['history_start']),
                str(inventory_run['history_end']), 'RUNNING', len(targets), 0,
                '', '[]', '', str(stage4_seal['ingestion_config_sha256']),
                str(stage4_seal['issuer_scope_sha256']), started_at, None, '{}',
            ),
        )
        snapshot_run_id = int(conn.execute(
            '''SELECT snapshot_run_id
               FROM stage6b_historical_document_snapshot_run
               WHERE inventory_run_id=? AND asof_date=?''',
            (inventory_run_id, as_of),
        ).fetchone()[0])
        conn.execute(
            '''DELETE FROM stage6b_historical_document_snapshot
               WHERE snapshot_run_id=?''',
            (snapshot_run_id,),
        )

    fetcher = fetch or http_fetcher(
        _http_policy(bundle.payload, 'sec_fundamentals')
    )
    cache_records_by_path: dict[str, dict[str, Any]] = {}
    staged_rows: list[dict[str, Any]] = []
    try:
        for index, row in enumerate(targets, start=1):
            ticker = str(row['ticker'])
            accession = str(row['accession_number'])
            document = str(row['primary_document'])
            cik = str(row['archive_cik']).zfill(10)
            path = resolve_sec_relative_document_path(
                cache_dir / 'filings' / cik / accession,
                document,
                allowed_suffixes=SEC_DOCUMENT_SUFFIXES,
                containment_root=cache_dir,
                context=f'Stage 6B historical document {ticker}/{accession}',
            )
            logical = f'filings/{cik}/{accession}/{document}'
            payload: bytes | None = None
            if path_exists(path):
                candidate = read_bytes(path)
                try:
                    _valid_historical_document_payload(candidate, logical_path=logical)
                    payload = candidate
                except ValueError:
                    if cache_only:
                        raise
            if payload is None:
                if cache_only or os.environ.get(
                    'CONSUMER_DEFENSIVE_CACHE_ONLY', ''
                ).strip().casefold() in {'1', 'true', 'yes', 'on'}:
                    raise FileNotFoundError(
                        f'Historical SEC document cache entry missing: {logical}'
                    )
                url = _sec_archive_url(cik, accession, document)
                candidate = fetcher(url)
                _valid_historical_document_payload(candidate, logical_path=logical)
                _atomic_promote_bytes(path, candidate, cache_root=cache_dir)
                payload = candidate
            record = _cache_manifest_record(cache_dir, path, payload)
            prior = cache_records_by_path.get(str(record['path']))
            if prior is not None and prior != record:
                raise RuntimeError(f'Conflicting shared historical document: {logical}')
            cache_records_by_path[str(record['path'])] = record
            staged_rows.append({
                'ticker': ticker,
                'accession_number': accession,
                'document_name': document,
                'form_type': str(row['form_type']),
                'filing_date': str(row['filing_date'] or ''),
                'accepted_at': str(row['accepted_at']),
                'report_date': str(row['report_date'] or ''),
                'archive_cik': cik,
                'company_currency': str(row['company_currency'] or 'USD').upper(),
                'source_id': str(row['source_id']),
                'source_url': _sec_archive_url(cik, accession, document),
                'logical_path': str(record['path']),
                'content_sha256': str(record['sha256']),
                'bytes': int(record['bytes']),
                'requested_metrics_json': str(row['requested_metrics_json']),
            })
            if progress is not None and (index == 1 or index % 100 == 0 or index == len(targets)):
                progress({
                    'status': 'RUNNING', 'completed': index, 'total': len(targets),
                    'ticker': ticker, 'accession_number': accession,
                })

        label = f'stage6b-history-{inventory_run_id}-{as_of}'
        seal_root, projection = _seal_cache_manifest(
            cache_dir, label,
            [cache_records_by_path[key] for key in sorted(cache_records_by_path)],
        )
        entry_by_logical = {
            str(entry['logical_path']): entry for entry in projection['entries']
        }
        if set(entry_by_logical) != set(cache_records_by_path):
            raise RuntimeError('Historical seal does not match the exact target corpus.')
        manifest_json = _canonical_json(projection['entries'])
        completed_at = utc_now()
        with conn:
            conn.executemany(
                '''INSERT INTO stage6b_historical_document_snapshot(
                       snapshot_run_id,ticker,accession_number,document_name,
                       form_type,filing_date,accepted_at,report_date,archive_cik,
                       company_currency,source_id,source_url,logical_path,
                       content_sha256,bytes,object_path,requested_metrics_json,
                       created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                [
                    (
                        snapshot_run_id, item['ticker'], item['accession_number'],
                        item['document_name'], item['form_type'], item['filing_date'],
                        item['accepted_at'], item['report_date'], item['archive_cik'],
                        item['company_currency'], item['source_id'], item['source_url'],
                        item['logical_path'], item['content_sha256'], item['bytes'],
                        str(entry_by_logical[item['logical_path']]['object_path']),
                        item['requested_metrics_json'], completed_at,
                    )
                    for item in staged_rows
                ],
            )
            conn.execute(
                '''UPDATE stage6b_historical_document_snapshot_run
                   SET status='PASS',hydrated_document_count=?,manifest_sha256=?,
                       manifest_json=?,seal_relative_path=?,completed_at=?,
                       metadata_json=? WHERE snapshot_run_id=?''',
                (
                    len(staged_rows), str(projection['sha256']), manifest_json,
                    seal_root.relative_to(cache_dir).as_posix(), completed_at,
                    _canonical_json({
                        'manifest_file_count': int(projection['files']),
                        'manifest_bytes': int(projection['bytes']),
                        'execution_model': 'single_fetch_immutable_historical_corpus',
                        'stage4_rows_mutated': 0,
                    }),
                    snapshot_run_id,
                ),
            )
        return {
            'status': 'PASS', 'immutable_replay': False,
            'snapshot_run_id': snapshot_run_id,
            'inventory_run_id': inventory_run_id, 'asof_date': as_of,
            'document_count': len(staged_rows),
            'unique_object_count': int(projection['files']),
            'bytes': int(projection['bytes']),
            'manifest_sha256': str(projection['sha256']),
            'seal_relative_path': seal_root.relative_to(cache_dir).as_posix(),
        }
    except BaseException as exc:
        with conn:
            conn.execute(
                '''UPDATE stage6b_historical_document_snapshot_run
                   SET status='FAIL',completed_at=?,metadata_json=?
                   WHERE snapshot_run_id=?''',
                (
                    utc_now(),
                    _canonical_json({
                        'error_type': type(exc).__name__, 'error': str(exc)
                    }),
                    snapshot_run_id,
                ),
            )
        raise


def hydrate_event_document_snapshot(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    as_of: str,
    inventory_run_id: int,
    cache_dir: Path,
    maximum_documents_per_filing: int | None = None,
    workers: int | None = None,
    cache_only: bool = False,
    fetch: Callable[[str], bytes] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    '''Discover, fetch, and immutably seal governed 8-K/6-K documents.

    Event exhibits are a separate Stage 6B corpus.  The function consumes the
    reviewed historical filing inventory and trusted Stage 4 association
    projection, but it never mutates Stage 4 state.  Only an exact primary
    event document plus explicitly typed/described results exhibits are
    selected; unrelated exhibits are deliberately excluded.
    '''
    from .stage4 import (
        _atomic_promote_bytes,
        _cache_manifest_record,
        _http_policy,
        _seal_cache_manifest,
        _sec_archive_url,
        _verify_cache_manifest,
        http_fetcher,
    )

    bootstrap_stage6b(conn, bundle)
    as_of = as_of[:10]
    date.fromisoformat(as_of)
    cache_dir = cache_dir.expanduser().resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    maximum_documents = int(
        maximum_documents_per_filing
        or cfg_get(bundle.payload, 'stage6b.maximum_event_documents_per_filing')
    )
    if not 1 <= maximum_documents <= 8:
        raise ValueError('Event documents per filing must be in [1,8].')
    worker_count = int(
        workers
        if workers is not None
        else cfg_get(bundle.payload, 'stage6b.event_hydration_workers')
    )
    if not 1 <= worker_count <= 16:
        raise ValueError('Event hydration workers must be in [1,16].')
    event_policy_sha256 = _event_document_policy_sha256(maximum_documents)
    stage4_seal = _trusted_seal(conn, as_of=as_of, cache_dir=cache_dir)
    inventory_run = conn.execute(
        '''SELECT inventory_run_id,history_start,history_end,status,
                  uncovered_target_count
           FROM stage6b_historical_inventory_run
           WHERE inventory_run_id=?''',
        (inventory_run_id,),
    ).fetchone()
    if inventory_run is None:
        raise ValueError(f'Unknown Stage 6B inventory run: {inventory_run_id}')
    if (
        str(inventory_run['status']) != 'PASS'
        or int(inventory_run['uncovered_target_count']) != 0
        or str(inventory_run['history_end']) > as_of
    ):
        raise RuntimeError(
            'Event document hydration requires a complete inventory bounded '
            'by the trusted Stage 4 as-of date.'
        )

    existing = conn.execute(
        '''SELECT * FROM stage6b_event_document_snapshot_run
           WHERE inventory_run_id=? AND asof_date=?''',
        (inventory_run_id, as_of),
    ).fetchone()
    if existing is not None and str(existing['status']) == 'PASS':
        try:
            existing_metadata = json.loads(str(existing['metadata_json'] or '{}'))
        except json.JSONDecodeError as exc:
            raise RuntimeError('Completed event snapshot metadata is invalid.') from exc
        if existing_metadata.get('event_policy_sha256') != event_policy_sha256:
            raise RuntimeError(
                'Completed event snapshot uses a different selection policy; '
                'build a new governed inventory/as-of snapshot.'
            )
        seal_root = cache_dir / str(existing['seal_relative_path'])
        selected = int(conn.execute(
            '''SELECT COUNT(*) FROM stage6b_event_document_snapshot
               WHERE event_snapshot_run_id=?''',
            (int(existing['event_snapshot_run_id']),),
        ).fetchone()[0])
        if any((
            int(existing['indexed_filing_count']) != int(existing['target_filing_count']),
            selected != int(existing['selected_document_count']),
            not _verify_cache_manifest(
                seal_root,
                str(existing['manifest_json']),
                str(existing['manifest_sha256']),
            ),
        )):
            raise RuntimeError('Completed event document snapshot is corrupt.')
        return {
            'status': 'PASS',
            'immutable_replay': True,
            'event_snapshot_run_id': int(existing['event_snapshot_run_id']),
            'inventory_run_id': inventory_run_id,
            'asof_date': as_of,
            'indexed_filing_count': int(existing['indexed_filing_count']),
            'document_count': selected,
            'directory_index_discrepancy_documents': int(
                existing_metadata.get(
                    'directory_index_discrepancy_documents', 0
                )
            ),
            'manifest_sha256': str(existing['manifest_sha256']),
            'seal_relative_path': str(existing['seal_relative_path']),
        }

    cutoff = as_of + 'T23:59:59Z'
    targets = [dict(row) for row in conn.execute(
        '''SELECT i.ticker,i.accession_number,i.form_type,i.filing_date,
                  i.accepted_at,i.report_date,i.primary_document,
                  i.requested_metrics_json,f.archive_cik,f.company_currency,
                  f.source_id,f.issuer_company_id
           FROM stage6b_historical_filing_inventory i
           JOIN consumer_defensive_sec_parser_filing_input f
             ON f.ticker=i.ticker AND f.accession_number=i.accession_number
           WHERE i.inventory_run_id=? AND i.form_family='event_report'
             AND f.accepted_at<=?
             AND (SELECT e.event_type
                  FROM sec_filing_company_association_event e
                  WHERE e.accession_number=f.accession_number
                    AND e.issuer_company_id=f.issuer_company_id
                    AND e.effective_asof<=?
                  ORDER BY e.effective_asof DESC,e.event_id DESC LIMIT 1)
                 IN ('observed','reactivated')
           ORDER BY i.ticker,i.accepted_at,i.accession_number''',
        (inventory_run_id, cutoff, cutoff),
    )]
    inventory_event_count = int(conn.execute(
        '''SELECT COUNT(*) FROM stage6b_historical_filing_inventory
           WHERE inventory_run_id=? AND form_family='event_report' ''',
        (inventory_run_id,),
    ).fetchone()[0])
    if len(targets) != inventory_event_count:
        raise RuntimeError(
            'Event inventory no longer matches the exact active Stage 4 '
            f'filing projection: expected={inventory_event_count} observed={len(targets)}'
        )

    started_at = utc_now()
    with conn:
        conn.execute(
            '''INSERT INTO stage6b_event_document_snapshot_run(
                   inventory_run_id,asof_date,history_start,history_end,status,
                   target_filing_count,indexed_filing_count,
                   selected_document_count,manifest_sha256,manifest_json,
                   seal_relative_path,ingestion_config_sha256,
                   issuer_scope_sha256,started_at,completed_at,metadata_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(inventory_run_id,asof_date) DO UPDATE SET
                   status='RUNNING',target_filing_count=excluded.target_filing_count,
                   indexed_filing_count=0,selected_document_count=0,
                   manifest_sha256='',manifest_json='[]',seal_relative_path='',
                   ingestion_config_sha256=excluded.ingestion_config_sha256,
                   issuer_scope_sha256=excluded.issuer_scope_sha256,
                   started_at=excluded.started_at,completed_at=NULL,metadata_json='{}' ''',
            (
                inventory_run_id, as_of, str(inventory_run['history_start']),
                str(inventory_run['history_end']), 'RUNNING', len(targets), 0, 0,
                '', '[]', '', str(stage4_seal['ingestion_config_sha256']),
                str(stage4_seal['issuer_scope_sha256']), started_at, None, '{}',
            ),
        )
        event_snapshot_run_id = int(conn.execute(
            '''SELECT event_snapshot_run_id
               FROM stage6b_event_document_snapshot_run
               WHERE inventory_run_id=? AND asof_date=?''',
            (inventory_run_id, as_of),
        ).fetchone()[0])
        conn.execute(
            '''DELETE FROM stage6b_event_document_snapshot
               WHERE event_snapshot_run_id=?''',
            (event_snapshot_run_id,),
        )

    fetcher = fetch or http_fetcher(
        _http_policy(bundle.payload, 'sec_fundamentals')
    )

    def load_event_index_artifacts(
        row: dict[str, Any],
    ) -> dict[str, Any]:
        ticker = str(row['ticker'])
        accession = str(row['accession_number'])
        cik = str(row['archive_cik']).zfill(10)
        primary = str(row['primary_document'])
        index_dir = cache_dir / 'stage6b_event_indexes' / cik / accession
        directory_path = resolve_sec_relative_document_path(
            index_dir, 'index.json',
            allowed_suffixes=SEC_SUBMISSIONS_ARCHIVE_SUFFIXES,
            containment_root=cache_dir,
            context=f'Stage 6B event index {ticker}/{accession}',
        )
        directory_logical = (
            f'stage6b_event_indexes/{cik}/{accession}/index.json'
        )
        directory_payload: bytes | None = None
        if path_exists(directory_path):
            candidate = read_bytes(directory_path)
            try:
                _parse_event_index(candidate, logical_path=directory_logical)
                directory_payload = candidate
            except ValueError:
                if cache_only:
                    raise
        if directory_payload is None:
            if cache_only or os.environ.get(
                'CONSUMER_DEFENSIVE_CACHE_ONLY', ''
            ).strip().casefold() in {'1', 'true', 'yes', 'on'}:
                raise FileNotFoundError(
                    'Event filing directory index cache entry missing: '
                    f'{directory_logical}'
                )
            candidate = fetcher(_event_index_url(cik, accession))
            _parse_event_index(candidate, logical_path=directory_logical)
            _atomic_promote_bytes(
                directory_path, candidate, cache_root=cache_dir
            )
            directory_payload = candidate
        directory_items = _parse_event_index(
            directory_payload, logical_path=directory_logical
        )
        filing_index_name = _event_filing_index_name(
            directory_items, accession_number=accession
        )
        filing_index_path = resolve_sec_relative_document_path(
            index_dir, filing_index_name,
            allowed_suffixes=SEC_DOCUMENT_SUFFIXES,
            containment_root=cache_dir,
            context=f'Stage 6B filing index HTML {ticker}/{accession}',
        )
        filing_index_logical = (
            f'stage6b_event_indexes/{cik}/{accession}/{filing_index_name}'
        )
        filing_index_payload: bytes | None = None
        if path_exists(filing_index_path):
            candidate = read_bytes(filing_index_path)
            try:
                _parse_event_filing_index_html(
                    candidate, logical_path=filing_index_logical
                )
                filing_index_payload = candidate
            except ValueError:
                if cache_only:
                    raise
        if filing_index_payload is None:
            if cache_only or os.environ.get(
                'CONSUMER_DEFENSIVE_CACHE_ONLY', ''
            ).strip().casefold() in {'1', 'true', 'yes', 'on'}:
                raise FileNotFoundError(
                    'Event filing index HTML cache entry missing: '
                    f'{filing_index_logical}'
                )
            candidate = fetcher(
                _sec_archive_url(cik, accession, filing_index_name)
            )
            _parse_event_filing_index_html(
                candidate, logical_path=filing_index_logical
            )
            _atomic_promote_bytes(
                filing_index_path, candidate, cache_root=cache_dir
            )
            filing_index_payload = candidate
        filing_items = _parse_event_filing_index_html(
            filing_index_payload, logical_path=filing_index_logical
        )
        selected = _select_event_documents(
            filing_items,
            primary_document=primary,
            maximum_documents=maximum_documents,
        )
        directory_index_discrepancies = (
            _validate_selected_event_document_membership(
                directory_items,
                selected,
                archive_cik=cik,
                accession_number=accession,
                context=f'{ticker}/{accession}',
            )
        )
        return {
            'directory_path': directory_path,
            'directory_payload': directory_payload,
            'filing_index_path': filing_index_path,
            'filing_index_payload': filing_index_payload,
            'selected': selected,
            'directory_index_discrepancies': directory_index_discrepancies,
        }

    def prefetch_index(row: dict[str, Any]) -> tuple[dict[str, str], ...]:
        return tuple(load_event_index_artifacts(row)['selected'])

    def prefetch_document(
        job: tuple[dict[str, Any], dict[str, str]],
    ) -> None:
        row, item = job
        ticker = str(row['ticker'])
        accession = str(row['accession_number'])
        cik = str(row['archive_cik']).zfill(10)
        document = str(item['name'])
        document_dir = cache_dir / 'stage6b_event_filings' / cik / accession
        path = resolve_sec_relative_document_path(
            document_dir, document,
            allowed_suffixes=SEC_DOCUMENT_SUFFIXES,
            containment_root=cache_dir,
            context=f'Stage 6B event document {ticker}/{accession}',
        )
        logical = f'stage6b_event_filings/{cik}/{accession}/{document}'
        if path_exists(path):
            candidate = read_bytes(path)
            try:
                _valid_historical_document_payload(
                    candidate, logical_path=logical
                )
                return
            except ValueError:
                if cache_only:
                    raise
        if cache_only or os.environ.get(
            'CONSUMER_DEFENSIVE_CACHE_ONLY', ''
        ).strip().casefold() in {'1', 'true', 'yes', 'on'}:
            raise FileNotFoundError(
                f'Event SEC document cache entry missing: {logical}'
            )
        candidate = fetcher(_sec_archive_url(cik, accession, document))
        _valid_historical_document_payload(candidate, logical_path=logical)
        _atomic_promote_bytes(path, candidate, cache_root=cache_dir)

    if worker_count > 1:
        selected_by_target: list[tuple[dict[str, str], ...]] = []
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            for position, selected in enumerate(
                executor.map(prefetch_index, targets), start=1
            ):
                selected_by_target.append(selected)
                if progress is not None and (
                    position == 1
                    or position % 100 == 0
                    or position == len(targets)
                ):
                    progress({
                        'status': 'RUNNING',
                        'phase': 'event_index_prefetch',
                        'completed': position,
                        'total': len(targets),
                    })
        document_jobs_by_path: dict[
            str, tuple[dict[str, Any], dict[str, str]]
        ] = {}
        for row, selected in zip(targets, selected_by_target, strict=True):
            cik = str(row['archive_cik']).zfill(10)
            accession = str(row['accession_number'])
            for item in selected:
                logical = (
                    f'stage6b_event_filings/{cik}/{accession}/{item["name"]}'
                )
                document_jobs_by_path.setdefault(logical, (row, item))
        document_jobs = [
            document_jobs_by_path[key] for key in sorted(document_jobs_by_path)
        ]
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            for position, _ in enumerate(
                executor.map(prefetch_document, document_jobs), start=1
            ):
                if progress is not None and (
                    position == 1
                    or position % 100 == 0
                    or position == len(document_jobs)
                ):
                    progress({
                        'status': 'RUNNING',
                        'phase': 'event_document_prefetch',
                        'completed': position,
                        'total': len(document_jobs),
                    })

    cache_records_by_path: dict[str, dict[str, Any]] = {}
    staged_rows: list[dict[str, Any]] = []
    no_exhibit_filings = 0
    directory_index_discrepancy_documents = 0
    try:
        for position, row in enumerate(targets, start=1):
            ticker = str(row['ticker'])
            accession = str(row['accession_number'])
            cik = str(row['archive_cik']).zfill(10)
            artifacts = load_event_index_artifacts(row)
            selected = tuple(artifacts['selected'])
            directory_index_discrepancy_documents += len(
                artifacts['directory_index_discrepancies']
            )
            if len(selected) == 1:
                no_exhibit_filings += 1
            for index_path_key, index_payload_key in (
                ('directory_path', 'directory_payload'),
                ('filing_index_path', 'filing_index_payload'),
            ):
                index_record = _cache_manifest_record(
                    cache_dir,
                    Path(artifacts[index_path_key]),
                    bytes(artifacts[index_payload_key]),
                )
                cache_records_by_path[str(index_record['path'])] = index_record
            for item in selected:
                document = str(item['name'])
                document_dir = cache_dir / 'stage6b_event_filings' / cik / accession
                path = resolve_sec_relative_document_path(
                    document_dir, document,
                    allowed_suffixes=SEC_DOCUMENT_SUFFIXES,
                    containment_root=cache_dir,
                    context=f'Stage 6B event document {ticker}/{accession}',
                )
                logical = f'stage6b_event_filings/{cik}/{accession}/{document}'
                payload: bytes | None = None
                if path_exists(path):
                    candidate = read_bytes(path)
                    try:
                        _valid_historical_document_payload(
                            candidate, logical_path=logical
                        )
                        payload = candidate
                    except ValueError:
                        if cache_only:
                            raise
                if payload is None:
                    if cache_only or os.environ.get(
                        'CONSUMER_DEFENSIVE_CACHE_ONLY', ''
                    ).strip().casefold() in {'1', 'true', 'yes', 'on'}:
                        raise FileNotFoundError(
                            f'Event SEC document cache entry missing: {logical}'
                        )
                    url = _sec_archive_url(cik, accession, document)
                    candidate = fetcher(url)
                    _valid_historical_document_payload(
                        candidate, logical_path=logical
                    )
                    _atomic_promote_bytes(path, candidate, cache_root=cache_dir)
                    payload = candidate
                record = _cache_manifest_record(cache_dir, path, payload)
                prior = cache_records_by_path.get(str(record['path']))
                if prior is not None and prior != record:
                    raise RuntimeError(f'Conflicting shared event document: {logical}')
                cache_records_by_path[str(record['path'])] = record
                staged_rows.append({
                    'ticker': ticker,
                    'accession_number': accession,
                    'document_name': document,
                    'document_role': str(item['document_role']),
                    'sec_document_type': str(item['type']),
                    'document_sequence': str(item['sequence']),
                    'document_description': str(item['description']),
                    'content_type': Path(document).suffix.casefold().lstrip('.'),
                    'form_type': str(row['form_type']),
                    'filing_date': str(row['filing_date'] or ''),
                    'accepted_at': str(row['accepted_at']),
                    'report_date': str(row['report_date'] or ''),
                    'archive_cik': cik,
                    'company_currency': str(row['company_currency'] or 'USD').upper(),
                    'source_id': str(row['source_id']),
                    'source_url': _sec_archive_url(cik, accession, document),
                    'logical_path': str(record['path']),
                    'content_sha256': str(record['sha256']),
                    'bytes': int(record['bytes']),
                    'requested_metrics_json': str(row['requested_metrics_json']),
                })
            if progress is not None and (
                position == 1 or position % 100 == 0 or position == len(targets)
            ):
                progress({
                    'status': 'RUNNING', 'completed': position,
                    'total': len(targets), 'ticker': ticker,
                    'accession_number': accession,
                    'selected_document_count': len(staged_rows),
                })

        label = f'stage6b-events-{inventory_run_id}-{as_of}'
        seal_root, projection = _seal_cache_manifest(
            cache_dir, label,
            [cache_records_by_path[key] for key in sorted(cache_records_by_path)],
        )
        entry_by_logical = {
            str(entry['logical_path']): entry for entry in projection['entries']
        }
        if set(entry_by_logical) != set(cache_records_by_path):
            raise RuntimeError('Event seal does not match the exact selected corpus.')
        completed_at = utc_now()
        with conn:
            conn.executemany(
                '''INSERT INTO stage6b_event_document_snapshot(
                       event_snapshot_run_id,ticker,accession_number,
                       document_name,document_role,sec_document_type,
                       document_sequence,document_description,content_type,
                       form_type,filing_date,accepted_at,report_date,archive_cik,
                       company_currency,source_id,source_url,logical_path,
                       content_sha256,bytes,object_path,requested_metrics_json,
                       created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                [(
                    event_snapshot_run_id, item['ticker'], item['accession_number'],
                    item['document_name'], item['document_role'],
                    item['sec_document_type'], item['document_sequence'],
                    item['document_description'], item['content_type'],
                    item['form_type'], item['filing_date'], item['accepted_at'],
                    item['report_date'], item['archive_cik'],
                    item['company_currency'], item['source_id'], item['source_url'],
                    item['logical_path'], item['content_sha256'], item['bytes'],
                    str(entry_by_logical[item['logical_path']]['object_path']),
                    item['requested_metrics_json'], completed_at,
                ) for item in staged_rows],
            )
            conn.execute(
                '''UPDATE stage6b_event_document_snapshot_run
                   SET status='PASS',indexed_filing_count=?,
                       selected_document_count=?,manifest_sha256=?,manifest_json=?,
                       seal_relative_path=?,completed_at=?,metadata_json=?
                   WHERE event_snapshot_run_id=?''',
                (
                    len(targets), len(staged_rows), str(projection['sha256']),
                    _canonical_json(projection['entries']),
                    seal_root.relative_to(cache_dir).as_posix(), completed_at,
                    _canonical_json({
                        'policy_version': EVENT_DOCUMENT_POLICY_VERSION,
                        'event_policy_sha256': event_policy_sha256,
                        'maximum_documents_per_filing': maximum_documents,
                        'filings_without_selected_exhibit': no_exhibit_filings,
                        'directory_index_discrepancy_documents': (
                            directory_index_discrepancy_documents
                        ),
                        'manifest_file_count': int(projection['files']),
                        'manifest_bytes': int(projection['bytes']),
                        'event_hydration_workers': worker_count,
                        'stage4_rows_mutated': 0,
                    }),
                    event_snapshot_run_id,
                ),
            )
        return {
            'status': 'PASS', 'immutable_replay': False,
            'event_snapshot_run_id': event_snapshot_run_id,
            'inventory_run_id': inventory_run_id, 'asof_date': as_of,
            'indexed_filing_count': len(targets),
            'document_count': len(staged_rows),
            'filings_without_selected_exhibit': no_exhibit_filings,
            'directory_index_discrepancy_documents': (
                directory_index_discrepancy_documents
            ),
            'unique_object_count': int(projection['files']),
            'bytes': int(projection['bytes']),
            'manifest_sha256': str(projection['sha256']),
            'event_policy_sha256': event_policy_sha256,
            'event_hydration_workers': worker_count,
            'seal_relative_path': seal_root.relative_to(cache_dir).as_posix(),
        }
    except BaseException as exc:
        with conn:
            conn.execute(
                '''UPDATE stage6b_event_document_snapshot_run
                   SET status='FAIL',completed_at=?,metadata_json=?
                   WHERE event_snapshot_run_id=?''',
                (
                    utc_now(),
                    _canonical_json({
                        'error_type': type(exc).__name__, 'error': str(exc)
                    }),
                    event_snapshot_run_id,
                ),
            )
        raise


def _csv_rows(path: Path) -> list[dict[str, str]]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    with resolved.open('r', encoding='utf-8-sig', newline='') as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _positive_int(raw: str, *, field: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'Historical replay {field} must be an integer.') from exc
    if value < 1:
        raise ValueError(f'Historical replay {field} must be positive.')
    return value


def load_historical_replay_plan(
    *,
    inventory_path: Path,
    schedule_path: Path,
) -> HistoricalReplayPlan:
    inventory_rows = _csv_rows(inventory_path)
    schedule_rows = _csv_rows(schedule_path)
    if not inventory_rows:
        raise ValueError('Historical filing inventory is empty.')
    if not schedule_rows:
        raise ValueError('Historical replay schedule is empty.')

    targets: list[HistoricalReplayTarget] = []
    events = 0
    identities: set[tuple[str, str]] = set()
    for row in inventory_rows:
        if row.get('form_family', '').strip() == 'event_report':
            events += 1
            continue
        if row.get('inventory_status', '').strip() == 'requires_targeted_hydration':
            raise RuntimeError(
                'Historical replay plan contains an uncovered core filing: '
                f"{row.get('ticker', '')} {row.get('accession_number', '')}"
            )
        ticker = row.get('ticker', '').strip().upper()
        accession = row.get('accession_number', '').strip()
        primary_document = row.get('primary_document', '').strip()
        replay_asof = row.get('replay_asof_date', '').strip()
        if not ticker or not accession or not primary_document or not replay_asof:
            raise ValueError('Historical core target identity is incomplete.')
        date.fromisoformat(replay_asof)
        sequence = _positive_int(
            row.get('replay_sequence', ''), field='replay_sequence'
        )
        capture_rank = _positive_int(
            row.get('capture_rank', ''), field='capture_rank'
        )
        identity = (ticker, accession)
        if identity in identities:
            raise ValueError(
                f'Historical replay target is duplicated: {ticker} {accession}'
            )
        identities.add(identity)
        targets.append(HistoricalReplayTarget(
            ticker=ticker,
            accession_number=accession,
            primary_document=primary_document,
            replay_sequence=sequence,
            replay_asof_date=replay_asof,
            capture_rank=capture_rank,
        ))

    grouped: dict[int, list[HistoricalReplayTarget]] = defaultdict(list)
    for target in targets:
        grouped[target.replay_sequence].append(target)
    schedule: list[HistoricalReplayStep] = []
    prior_date = ''
    for expected_sequence, row in enumerate(schedule_rows, start=1):
        sequence = _positive_int(
            row.get('replay_sequence', ''), field='schedule replay_sequence'
        )
        if sequence != expected_sequence:
            raise ValueError('Historical replay sequences must be an exact prefix.')
        asof_date = row.get('asof_date', '').strip()
        date.fromisoformat(asof_date)
        if prior_date and asof_date <= prior_date:
            raise ValueError('Historical replay cutoff dates must be strictly increasing.')
        prior_date = asof_date
        step_targets = tuple(sorted(
            grouped.get(sequence, []),
            key=lambda target: (target.ticker, target.accession_number),
        ))
        if not step_targets:
            raise ValueError(f'Historical replay sequence {sequence} has no targets.')
        if {target.replay_asof_date for target in step_targets} != {asof_date}:
            raise ValueError(
                f'Historical replay sequence {sequence} cutoff does not match inventory.'
            )
        expected_targets = _positive_int(
            row.get('target_filing_count', ''),
            field='schedule target_filing_count',
        )
        expected_tickers = _positive_int(
            row.get('ticker_count', ''), field='schedule ticker_count'
        )
        maximum_rank = _positive_int(
            row.get('maximum_capture_rank', ''),
            field='schedule maximum_capture_rank',
        )
        if expected_targets != len(step_targets):
            raise ValueError(
                f'Historical replay sequence {sequence} target count mismatch.'
            )
        if expected_tickers != len({target.ticker for target in step_targets}):
            raise ValueError(
                f'Historical replay sequence {sequence} ticker count mismatch.'
            )
        if maximum_rank != max(target.capture_rank for target in step_targets):
            raise ValueError(
                f'Historical replay sequence {sequence} capture-rank mismatch.'
            )
        schedule.append(HistoricalReplayStep(
            replay_sequence=sequence,
            asof_date=asof_date,
            targets=step_targets,
        ))
    if set(grouped) != set(range(1, len(schedule) + 1)):
        raise ValueError('Historical inventory contains targets outside the schedule.')
    return HistoricalReplayPlan(
        inventory_path=inventory_path.expanduser().resolve(),
        schedule_path=schedule_path.expanduser().resolve(),
        steps=tuple(schedule),
        target_filing_count=len(targets),
        event_index_candidate_count=events,
    )


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _sec_watermark(conn: sqlite3.Connection) -> str:
    if not _table_exists(conn, 'consumer_defensive_sec_ingestion_watermark'):
        return ''
    row = conn.execute(
        '''SELECT asof_date FROM consumer_defensive_sec_ingestion_watermark
           WHERE model_family='consumer_defensive' '''
    ).fetchone()
    return str(row[0]) if row else ''


def _stage4_semantic_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = (
        'fact_sec_filing',
        'bridge_sec_filing_company',
        'bridge_sec_filing_document_company',
        'fact_sec_xbrl_fact_raw',
    )
    return {
        table: (
            int(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0])
            if _table_exists(conn, table) else 0
        )
        for table in tables
    }


def _missing_historical_targets(
    conn: sqlite3.Connection,
    targets: Iterable[HistoricalReplayTarget],
) -> list[tuple[str, str, str]]:
    target_rows = list(targets)
    if not target_rows:
        return []
    conn.execute(
        '''CREATE TEMP TABLE IF NOT EXISTS stage6b_expected_historical_document(
               ticker TEXT NOT NULL,
               accession_number TEXT NOT NULL,
               primary_document TEXT NOT NULL,
               PRIMARY KEY(ticker,accession_number)
           ) WITHOUT ROWID'''
    )
    conn.execute('DELETE FROM stage6b_expected_historical_document')
    conn.executemany(
        '''INSERT INTO stage6b_expected_historical_document(
               ticker,accession_number,primary_document
           ) VALUES (?,?,?)''',
        [
            (target.ticker, target.accession_number, target.primary_document)
            for target in target_rows
        ],
    )
    return [
        (str(row[0]), str(row[1]), str(row[2]))
        for row in conn.execute(
            '''SELECT e.ticker,e.accession_number,e.primary_document
               FROM stage6b_expected_historical_document e
               WHERE NOT EXISTS (
                   SELECT 1
                   FROM bridge_sec_filing_document_company d
                   WHERE d.issuer_ticker=e.ticker
                     AND d.accession_number=e.accession_number
                     AND d.primary_document=e.primary_document
                     AND d.hydration_status='hydrated'
                     AND LENGTH(COALESCE(d.content_sha256,''))=64
               )
               ORDER BY e.ticker,e.accession_number'''
        )
    ]


def _resume_sequence(
    conn: sqlite3.Connection,
    plan: HistoricalReplayPlan,
) -> int:
    watermark = _sec_watermark(conn)
    if not watermark:
        populated = {
            table: count for table, count in _stage4_semantic_counts(conn).items()
            if count
        }
        if populated:
            raise RuntimeError(
                'Historical SEC replay requires an empty Stage 4 foundation; '
                f'found populated tables: {populated}'
            )
        return 0
    matching = [
        step for step in plan.steps if step.asof_date == watermark
    ]
    if len(matching) != 1:
        raise RuntimeError(
            'Historical SEC replay watermark is not an exact schedule cutoff: '
            f'{watermark}'
        )
    watermark_sequence = matching[0].replay_sequence
    reconciliation = conn.execute(
        '''SELECT 1 FROM consumer_defensive_sec_reconciliation_state
           WHERE asof_date=? AND status='complete'
             AND scope_contract_version=3 AND trust_state='trusted_current' ''',
        (watermark,),
    ).fetchone()
    snapshot = conn.execute(
        '''SELECT 1 FROM consumer_defensive_sec_cache_snapshot
           WHERE asof_date=? AND scope_contract_version=3
             AND trust_state='trusted_current' ''',
        (watermark,),
    ).fetchone()
    if reconciliation is not None and snapshot is not None:
        completed = watermark_sequence
    else:
        completed = watermark_sequence - 1
        if completed:
            prior = plan.steps[completed - 1]
            prior_snapshot = conn.execute(
                '''SELECT 1 FROM consumer_defensive_sec_cache_snapshot
                   WHERE asof_date=? AND scope_contract_version=3
                     AND trust_state='trusted_current' ''',
                (prior.asof_date,),
            ).fetchone()
            if prior_snapshot is None:
                raise RuntimeError(
                    'Historical SEC replay interrupted state has no trusted '
                    'prior schedule snapshot.'
                )
    prior_targets = [
        target
        for step in plan.steps[:completed]
        for target in step.targets
    ]
    missing = _missing_historical_targets(conn, prior_targets)
    if missing:
        raise RuntimeError(
            'Historical SEC replay resume state is missing prior captures: '
            f'{missing[:10]}'
        )
    return completed


def _compact_sync_result(result: dict[str, Any]) -> dict[str, Any]:
    compact = {
        key: value for key, value in result.items()
        if key != 'cache_manifest'
    }
    manifest = result.get('cache_manifest')
    if isinstance(manifest, dict):
        compact['cache_manifest'] = {
            key: manifest[key]
            for key in ('sha256', 'files', 'bytes', 'immutable_replay')
            if key in manifest
        }
    return compact


def execute_historical_filing_replay(
    conn: sqlite3.Connection,
    plan: HistoricalReplayPlan,
    *,
    output_dir: Path,
    sync_step: Callable[..., dict[str, Any]],
    stop_after_sequence: int | None = None,
) -> dict[str, Any]:
    if stop_after_sequence is not None and stop_after_sequence < 1:
        raise ValueError('stop_after_sequence must be positive.')
    resolved_output = output_dir.expanduser().resolve()
    resolved_output.mkdir(parents=True, exist_ok=True)
    completed_sequence = _resume_sequence(conn, plan)
    initial_sequence = completed_sequence
    if stop_after_sequence is not None and stop_after_sequence < completed_sequence:
        raise ValueError(
            'stop_after_sequence is earlier than the persisted replay watermark.'
        )

    for step in plan.steps[completed_sequence:]:
        if (
            stop_after_sequence is not None
            and step.replay_sequence > stop_after_sequence
        ):
            break
        prior_asof = (
            plan.steps[step.replay_sequence - 2].asof_date
            if step.replay_sequence > 1 else None
        )
        result = sync_step(
            as_of=step.asof_date,
            tickers=None,
            force_refresh=False,
            incremental_from_asof=prior_asof,
        )
        if result.get('failures') or not result.get('full_scope_reconciled'):
            raise RuntimeError(
                f'Historical SEC replay sequence {step.replay_sequence} failed: '
                f"{result.get('failures') or 'full-scope reconciliation false'}"
            )
        missing = _missing_historical_targets(conn, step.targets)
        if missing:
            raise RuntimeError(
                f'Historical SEC replay sequence {step.replay_sequence} did not '
                f'hydrate every planned filing: {missing[:10]}'
            )
        completed_sequence = step.replay_sequence
        write_json(
            resolved_output
            / f'historical_replay_{step.replay_sequence:03d}_{step.asof_date}.json',
            {
                'replay_sequence': step.replay_sequence,
                'asof_date': step.asof_date,
                'planned_target_count': len(step.targets),
                'verified_target_count': len(step.targets),
                'sync_result': _compact_sync_result(result),
            },
        )
        write_json(
            resolved_output / 'consumer_defensive_historical_replay_state.json',
            {
                'status': 'running',
                'completed_sequence': completed_sequence,
                'total_sequence_count': len(plan.steps),
                'current_asof_date': step.asof_date,
                'verified_target_count': sum(
                    len(prior.targets)
                    for prior in plan.steps[:completed_sequence]
                ),
                'target_filing_count': plan.target_filing_count,
                'inventory_path': str(plan.inventory_path),
                'schedule_path': str(plan.schedule_path),
            },
        )

    completed_targets = [
        target
        for step in plan.steps[:completed_sequence]
        for target in step.targets
    ]
    missing = _missing_historical_targets(conn, completed_targets)
    if missing:
        raise RuntimeError(
            f'Historical SEC replay cumulative capture check failed: {missing[:10]}'
        )
    complete = completed_sequence == len(plan.steps)
    final = {
        'status': 'PASS' if complete else 'PARTIAL',
        'initial_completed_sequence': initial_sequence,
        'completed_sequence': completed_sequence,
        'total_sequence_count': len(plan.steps),
        'last_asof_date': (
            plan.steps[completed_sequence - 1].asof_date
            if completed_sequence else None
        ),
        'target_filing_count': plan.target_filing_count,
        'verified_target_count': len(completed_targets),
        'remaining_target_count': plan.target_filing_count - len(completed_targets),
        'event_index_candidate_count': plan.event_index_candidate_count,
        'inventory_path': str(plan.inventory_path),
        'schedule_path': str(plan.schedule_path),
    }
    write_json(
        resolved_output / 'consumer_defensive_historical_replay_state.json',
        final,
    )
    return final

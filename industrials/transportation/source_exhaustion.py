from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping, Sequence, cast

from dedicated_parser.contracts import file_sha256
from industrials.core.reports import write_csv_atomic, write_text_atomic
from industrials.transportation.source_census import (
    _inside_registration_window,
)


SOURCE_EXHAUSTION_VERSION = "transportation_dp6e_source_exhaustion_v2"

EVENT_FORMS = frozenset({"8-K", "8-K/A", "6-K", "6-K/A"})
PERIODIC_ANNUAL_FORMS = frozenset(
    {
        "10-K",
        "10-K/A",
        "10-K405",
        "10-K405/A",
        "10-KT",
        "10-KT/A",
        "20-F",
        "20-F/A",
        "40-F",
        "40-F/A",
    }
)
PERIODIC_INTERIM_FORMS = frozenset(
    {"10-Q", "10-Q/A", "10-QT", "10-QT/A"}
)
PROXY_FORMS = frozenset(
    {
        "DEF 14A",
        "DEF 14A/A",
        "DEF 14C",
        "DEF 14C/A",
        "DEFM14A",
        "DEFM14A/A",
        "PREM14A",
        "PREM14A/A",
    }
)
ANNUAL_REPORT_FORMS = frozenset({"ARS"})
SUPPLEMENTAL_DISCLOSURE_FORMS = frozenset({"FWP"})
TRANSACTION_FORMS = frozenset(
    {
        "S-1",
        "S-1/A",
        "F-1",
        "F-1/A",
        "S-4",
        "S-4/A",
        "F-4",
        "F-4/A",
        "10-12B",
        "10-12B/A",
        "10-12G",
        "10-12G/A",
        "424B3",
        "424B4",
        "424B5",
    }
)
RELEVANT_FORMS = (
    EVENT_FORMS
    | PERIODIC_ANNUAL_FORMS
    | PERIODIC_INTERIM_FORMS
    | PROXY_FORMS
    | ANNUAL_REPORT_FORMS
    | SUPPLEMENTAL_DISCLOSURE_FORMS
    | TRANSACTION_FORMS
)
RESULT_ITEMS = frozenset({"2.02", "7.01"})
MATERIAL_EXHIBIT_ITEMS = frozenset({"8.01", "9.01"})
EVENT_METADATA_PATTERN = re.compile(
    r"\b(?:earnings|financial\s+results|operating\s+results|"
    r"quarterly\s+results|annual\s+results|results\s+release|"
    r"earnings\s+release|press\s+release|investor\s+presentation|"
    r"traffic\s+release|operating\s+statistics|railroad\s+performance|"
    r"network\s+velocity|terminal\s+dwell|service\s+reliability|"
    r"sustainability|ESG|driver\s+turnover|empty\s+miles)\b",
    re.IGNORECASE,
)
RELEVANT_DOCUMENT_PATTERN = re.compile(
    r"(?:\bex(?:hibit)?[-_ ]?(?:13|99)\b|earnings|results|release|"
    r"presentation|traffic|operating\s+statistics|railroad\s+performance|"
    r"network\s+velocity|terminal\s+dwell|service\s+reliability|"
    r"sustainability|ESG|driver\s+turnover|empty\s+miles|fleet|annual\s+report)",
    re.IGNORECASE,
)
DOCUMENT_SUFFIXES = frozenset({".htm", ".html", ".txt", ".xml", ".xhtml", ".pdf"})
CALIBRATION_COMPLETE_DISPOSITIONS = frozenset({"CALIBRATION_CANDIDATE"})


def _as_int(value: object) -> int:
    try:
        return int(str(value or "0"))
    except ValueError:
        return 0

FILING_FIELDS = (
    "manifest_version",
    "ticker",
    "company_name",
    "universe_role",
    "calibration_cohort",
    "industry",
    "development_overlay",
    "cik",
    "source_window_start",
    "source_window_end",
    "submissions_source_file",
    "accession_number",
    "filing_date",
    "accepted_at",
    "report_date",
    "form_type",
    "items",
    "primary_document",
    "primary_document_description",
    "source_category",
    "candidate_priority",
    "candidate_basis",
    "in_fact_sec_filing",
    "fact_form_type",
    "in_dp3_decisions",
    "dp3_decision",
    "dp3_selection_rule",
    "index_status",
    "index_document_count",
    "index_pdf_count",
    "index_relevant_document_count",
    "index_metric_alias_count",
    "selected_document_names",
    "selected_document_count",
    "cached_selected_document_count",
    "target_metric_ids",
    "target_metric_count",
    "near_gate_metric_ids",
    "delta_action",
    "delta_reason",
    "index_url",
)

DELTA_FIELDS = (
    "candidate_key",
    "manifest_version",
    "ticker",
    "universe_role",
    "calibration_cohort",
    "industry",
    "cik",
    "accession_number",
    "filing_date",
    "form_type",
    "items",
    "primary_document",
    "primary_document_description",
    "source_category",
    "candidate_priority",
    "candidate_basis",
    "database_registry_gap",
    "dp3_decision",
    "index_status",
    "selected_document_names",
    "selected_document_count",
    "cached_selected_document_count",
    "target_metric_ids",
    "target_metric_count",
    "near_gate_metric_ids",
    "delta_action",
    "delta_reason",
    "index_url",
)

GAP_FIELDS = (
    "gap_key",
    "manifest_version",
    "ticker",
    "cik",
    "universe_role",
    "gap_type",
    "source_file",
    "declared_filing_from",
    "declared_filing_to",
    "source_window_start",
    "source_window_end",
    "required_action",
    "reason",
)

FORM_FIELDS = (
    "manifest_version",
    "form_type",
    "source_category",
    "scope_disposition",
    "filing_count",
    "ticker_count",
    "database_registry_count",
    "database_registry_gap_count",
    "dp3_included_count",
    "dp3_excluded_count",
    "dp3_missing_count",
    "cached_index_count",
    "missing_index_count",
    "actionable_delta_count",
)

METRIC_FIELDS = (
    "manifest_version",
    "metric_id",
    "metric_pack",
    "source_lane",
    "metric_disposition",
    "post_active_accepted_count",
    "post_active_usable_count",
    "broad_required_count",
    "broad_accepted_shortfall",
    "best_accepted_niche_shortfall",
    "historical_depth_gate_pass",
    "applicable_ticker_count",
    "candidate_ticker_count",
    "candidate_filing_count",
    "priority_1_filing_count",
    "priority_2_filing_count",
    "priority_3_filing_count",
    "index_hydration_count",
    "selected_document_hydration_count",
    "cached_document_parse_count",
    "submission_gap_ticker_count",
    "source_exhaustion_status",
)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {
                str(key): str(value or "").strip()
                for key, value in row.items()
            }
            for row in csv.DictReader(handle)
        ]


def _normalized_cik(value: object) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits.zfill(10) if digits else ""


def _date_min(left: str, right: str) -> str:
    values = [value[:10] for value in (left, right) if value]
    return min(values) if values else ""


def _source_window(
    member: Mapping[str, str],
    *,
    active_start_date: str,
    inactive_start_date: str,
    asof_date: str,
) -> tuple[str, str]:
    if str(member.get("universe_role") or "") == "active":
        return active_start_date, asof_date
    end = _date_min(str(member.get("membership_end_date") or ""), asof_date)
    return inactive_start_date, end or asof_date


def _overlaps(
    start: str,
    end: str,
    *,
    window_start: str,
    window_end: str,
) -> bool:
    return not ((end and end < window_start) or (start and start > window_end))


def _vector_value(block: Mapping[str, object], key: str, index: int) -> str:
    values = block.get(key)
    if not isinstance(values, list) or index >= len(values):
        return ""
    return str(values[index] or "").strip()


def _submission_rows(
    block: Mapping[str, object],
    *,
    ticker: str,
    cik: str,
    source_file: str,
    window_start: str,
    window_end: str,
) -> tuple[list[dict[str, str]], list[str]]:
    accessions = block.get("accessionNumber")
    if not isinstance(accessions, list):
        return [], [f"{source_file}: accessionNumber is not a list"]
    errors: list[str] = []
    for key in ("filingDate", "form", "primaryDocument"):
        values = block.get(key)
        if not isinstance(values, list):
            errors.append(f"{source_file}: {key} is not a list")
        elif len(values) != len(accessions):
            errors.append(
                f"{source_file}: {key} count={len(values)} "
                f"accession count={len(accessions)}"
            )
    rows: list[dict[str, str]] = []
    for index, raw_accession in enumerate(accessions):
        accession = str(raw_accession or "").strip()
        filing_date = _vector_value(block, "filingDate", index)[:10]
        if (
            not accession
            or not filing_date
            or filing_date < window_start
            or filing_date > window_end
        ):
            continue
        rows.append(
            {
                "ticker": ticker,
                "cik": cik,
                "submissions_source_file": source_file,
                "accession_number": accession,
                "filing_date": filing_date,
                "accepted_at": _vector_value(
                    block,
                    "acceptanceDateTime",
                    index,
                ),
                "report_date": _vector_value(block, "reportDate", index)[:10],
                "form_type": _vector_value(block, "form", index).upper(),
                "items": _vector_value(block, "items", index),
                "primary_document": _vector_value(
                    block,
                    "primaryDocument",
                    index,
                ),
                "primary_document_description": _vector_value(
                    block,
                    "primaryDocDescription",
                    index,
                ),
            }
        )
    return rows, errors


def load_submission_inventory(
    submissions_cache_dir: Path,
    *,
    members: Mapping[str, Mapping[str, str]],
    active_start_date: str,
    inactive_start_date: str,
    asof_date: str,
    manifest_version: str = SOURCE_EXHAUSTION_VERSION,
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str], dict[str, object]]:
    rows_by_key: dict[tuple[str, str], dict[str, str]] = {}
    gaps: list[dict[str, str]] = []
    errors: list[str] = []
    main_file_count = 0
    local_history_file_count = 0
    referenced_history_file_count = 0
    for ticker, member in sorted(members.items()):
        cik = _normalized_cik(member.get("cik"))
        role = str(member.get("universe_role") or "")
        window_start, window_end = _source_window(
            member,
            active_start_date=active_start_date,
            inactive_start_date=inactive_start_date,
            asof_date=asof_date,
        )
        main_name = f"CIK{cik}.json"
        main_path = submissions_cache_dir / main_name
        if not main_path.is_file():
            gaps.append(
                {
                    "gap_key": f"{ticker}|MAIN_SUBMISSIONS|{main_name}",
                    "manifest_version": manifest_version,
                    "ticker": ticker,
                    "cik": cik,
                    "universe_role": role,
                    "gap_type": "MAIN_SUBMISSIONS_FILE_MISSING",
                    "source_file": main_name,
                    "declared_filing_from": "",
                    "declared_filing_to": "",
                    "source_window_start": window_start,
                    "source_window_end": window_end,
                    "required_action": "HYDRATE_SUBMISSIONS_MAIN",
                    "reason": "main SEC submissions cache is unavailable",
                }
            )
            continue
        try:
            payload = json.loads(main_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{main_path}: {exc}")
            continue
        main_file_count += 1
        recent = (payload.get("filings") or {}).get("recent") or {}
        if not isinstance(recent, dict):
            errors.append(f"{main_path}: filings.recent is not an object")
            recent = {}
        recent_rows, recent_errors = _submission_rows(
            recent,
            ticker=ticker,
            cik=cik,
            source_file=main_name,
            window_start=window_start,
            window_end=window_end,
        )
        errors.extend(recent_errors)
        for row in recent_rows:
            rows_by_key[(ticker, row["accession_number"])] = row
        history_files = (payload.get("filings") or {}).get("files") or []
        if not isinstance(history_files, list):
            errors.append(f"{main_path}: filings.files is not a list")
            history_files = []
        for history in history_files:
            if not isinstance(history, dict):
                continue
            source_file = str(history.get("name") or "").strip()
            filing_from = str(history.get("filingFrom") or "")[:10]
            filing_to = str(history.get("filingTo") or "")[:10]
            if not source_file or not _overlaps(
                filing_from,
                filing_to,
                window_start=window_start,
                window_end=window_end,
            ):
                continue
            referenced_history_file_count += 1
            history_path = submissions_cache_dir / source_file
            if not history_path.is_file():
                gaps.append(
                    {
                        "gap_key": (
                            f"{ticker}|SUBMISSIONS_HISTORY|{source_file}"
                        ),
                        "manifest_version": manifest_version,
                        "ticker": ticker,
                        "cik": cik,
                        "universe_role": role,
                        "gap_type": "SUBMISSIONS_HISTORY_FILE_MISSING",
                        "source_file": source_file,
                        "declared_filing_from": filing_from,
                        "declared_filing_to": filing_to,
                        "source_window_start": window_start,
                        "source_window_end": window_end,
                        "required_action": "HYDRATE_SUBMISSIONS_HISTORY",
                        "reason": (
                            "referenced SEC submissions history overlaps the "
                            "sealed transportation source window"
                        ),
                    }
                )
                continue
            try:
                history_payload = json.loads(
                    history_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"{history_path}: {exc}")
                continue
            if not isinstance(history_payload, dict):
                errors.append(f"{history_path}: payload is not an object")
                continue
            local_history_file_count += 1
            history_rows, history_errors = _submission_rows(
                history_payload,
                ticker=ticker,
                cik=cik,
                source_file=source_file,
                window_start=window_start,
                window_end=window_end,
            )
            errors.extend(history_errors)
            for row in history_rows:
                rows_by_key.setdefault(
                    (ticker, row["accession_number"]),
                    row,
                )
    rows = sorted(
        rows_by_key.values(),
        key=lambda row: (
            row["ticker"],
            row["filing_date"],
            row["accession_number"],
        ),
    )
    gaps.sort(key=lambda row: (row["ticker"], row["gap_type"], row["source_file"]))
    summary: dict[str, object] = {
        "main_submissions_file_count": main_file_count,
        "referenced_overlapping_history_file_count": (
            referenced_history_file_count
        ),
        "cached_overlapping_history_file_count": local_history_file_count,
        "missing_overlapping_history_file_count": (
            referenced_history_file_count - local_history_file_count
        ),
        "enumerated_filing_count": len(rows),
    }
    return rows, gaps, errors, summary


def _item_set(value: str) -> set[str]:
    return {
        item
        for item in re.split(r"[\s,;|]+", str(value or ""))
        if re.fullmatch(r"\d+\.\d+", item)
    }


def classify_source(
    *,
    form_type: str,
    items: str,
    primary_description: str,
    development_overlay: bool,
    registration_window: bool,
) -> tuple[str, int, str]:
    form = form_type.upper()
    item_values = _item_set(items)
    description_match = bool(
        EVENT_METADATA_PATTERN.search(primary_description or "")
    )
    if form in {"8-K", "8-K/A"}:
        if item_values & RESULT_ITEMS:
            return "DOMESTIC_RESULTS_EVENT", 1, "RESULTS_ITEM_2_02_OR_7_01"
        if description_match:
            return "DOMESTIC_RESULTS_EVENT", 2, "RESULTS_DESCRIPTION_SIGNAL"
        if item_values & MATERIAL_EXHIBIT_ITEMS:
            return "DOMESTIC_MATERIAL_EVENT", 3, "MATERIAL_ITEM_8_01_OR_9_01"
        return "DOMESTIC_OTHER_EVENT", 0, "NO_RESULTS_OR_MATERIAL_EXHIBIT_SIGNAL"
    if form in {"6-K", "6-K/A"}:
        return (
            "FOREIGN_RESULTS_EVENT",
            1 if description_match else 2,
            (
                "FOREIGN_RESULTS_DESCRIPTION_SIGNAL"
                if description_match
                else "FOREIGN_REPORT_REQUIRES_INDEX_METADATA"
            ),
        )
    if form in PERIODIC_ANNUAL_FORMS:
        return "PERIODIC_ANNUAL_REPORT", 1, "PRIMARY_ANNUAL_STATEMENT"
    if form in PERIODIC_INTERIM_FORMS:
        return "PERIODIC_INTERIM_REPORT", 1, "PRIMARY_INTERIM_STATEMENT"
    if form in PROXY_FORMS:
        return "PROXY_ACTUAL_KPI", 3, "ANNUAL_PROXY_PRIMARY_DOCUMENT"
    if form in ANNUAL_REPORT_FORMS:
        return "ANNUAL_REPORT_EXHIBIT", 2, "ANNUAL_REPORT_TO_SECURITY_HOLDERS"
    if form in SUPPLEMENTAL_DISCLOSURE_FORMS:
        return (
            "SUPPLEMENTAL_INVESTOR_DISCLOSURE",
            3,
            "FREE_WRITING_PROSPECTUS_PRIMARY_DOCUMENT",
        )
    if form in TRANSACTION_FORMS:
        if registration_window:
            return "TRANSACTION_REGISTRATION", 2, "SEALED_REGISTRATION_WINDOW"
        if development_overlay and form in {"424B5", "S-1", "S-1/A", "F-1", "F-1/A"}:
            return (
                "DEVELOPMENT_REGISTRATION",
                3,
                "DEVELOPMENT_STAGE_PROSPECTUS",
            )
        if form in {
            "S-4",
            "S-4/A",
            "F-4",
            "F-4/A",
            "10-12B",
            "10-12B/A",
            "10-12G",
            "10-12G/A",
        }:
            return "TRANSACTION_REGISTRATION", 3, "PREDECESSOR_OR_TRANSACTION_HISTORY"
        return "OTHER_REGISTRATION", 0, "OUTSIDE_TARGETED_REGISTRATION_POLICY"
    return "OUT_OF_SCOPE_FORM", 0, "FORM_NOT_IN_SOURCE_EXHAUSTION_POLICY"


def _compile_aliases(
    aliases: Mapping[str, Sequence[str]],
) -> dict[str, tuple[re.Pattern[str], ...]]:
    compiled: dict[str, tuple[re.Pattern[str], ...]] = {}
    for metric_id, values in aliases.items():
        patterns: list[re.Pattern[str]] = []
        for value in values:
            try:
                patterns.append(re.compile(value, re.IGNORECASE))
            except re.error:
                patterns.append(re.compile(re.escape(value), re.IGNORECASE))
        compiled[metric_id] = tuple(patterns)
    return compiled


def _index_metadata(
    *,
    cache_dir: Path,
    cik: str,
    accession_number: str,
    primary_document: str,
    target_metrics: Sequence[str],
    aliases: Mapping[str, tuple[re.Pattern[str], ...]],
    source_category: str,
) -> dict[str, object]:
    accession_dir = (
        cache_dir
        / "sec_archive_xbrl"
        / f"CIK{cik}"
        / accession_number.replace("-", "")
    )
    index_path = accession_dir / "index.json"
    if not index_path.is_file():
        return {
            "index_status": "MISSING",
            "index_document_count": 0,
            "index_pdf_count": 0,
            "index_relevant_document_count": 0,
            "index_metric_alias_count": 0,
            "selected_document_names": (),
            "cached_selected_document_count": 0,
        }
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "index_status": "INVALID",
            "index_document_count": 0,
            "index_pdf_count": 0,
            "index_relevant_document_count": 0,
            "index_metric_alias_count": 0,
            "selected_document_names": (),
            "cached_selected_document_count": 0,
        }
    raw_items = (payload.get("directory") or {}).get("item") or []
    items = [item for item in raw_items if isinstance(item, dict)]
    item_names = {
        str(item.get("name") or "").strip()
        for item in items
        if str(item.get("name") or "").strip()
    }
    selected: set[str] = set()
    alias_metrics: set[str] = set()
    pdf_count = 0
    relevant_count = 0
    legacy_numeric_primary = bool(
        re.fullmatch(
            r"\d+\.(?:txt|html?|xhtml)",
            primary_document,
            re.IGNORECASE,
        )
    )
    primary_required = source_category in {
        "DOMESTIC_RESULTS_EVENT",
        "DOMESTIC_MATERIAL_EVENT",
        "FOREIGN_RESULTS_EVENT",
        "PERIODIC_ANNUAL_REPORT",
        "PERIODIC_INTERIM_REPORT",
        "PROXY_ACTUAL_KPI",
        "ANNUAL_REPORT_EXHIBIT",
        "SUPPLEMENTAL_INVESTOR_DISCLOSURE",
        "TRANSACTION_REGISTRATION",
        "DEVELOPMENT_REGISTRATION",
    }
    for item in items:
        name = str(item.get("name") or "").strip()
        suffix = Path(name).suffix.lower()
        if not name or suffix not in DOCUMENT_SUFFIXES:
            continue
        metadata = " ".join(
            str(item.get(field) or "")
            for field in (
                "name",
                "type",
                "document_type",
                "description",
                "title",
            )
        )
        pdf_count += int(suffix == ".pdf")
        relevant = suffix == ".pdf" or bool(
            RELEVANT_DOCUMENT_PATTERN.search(metadata)
        )
        if relevant:
            relevant_count += 1
        for metric_id in target_metrics:
            if any(
                pattern.search(metadata)
                for pattern in aliases.get(metric_id, ())
            ):
                alias_metrics.add(metric_id)
                relevant = True
        if relevant or (
            primary_required
            and name == primary_document
            and not legacy_numeric_primary
        ):
            selected.add(name)
    primary_selected = (
        primary_required
        and bool(primary_document)
        and Path(primary_document).suffix.lower() in DOCUMENT_SUFFIXES
        and primary_document in item_names
        and not legacy_numeric_primary
    )
    if primary_selected:
        selected.add(primary_document)
    complete_submission_name = f"{accession_number}.txt"
    if (
        primary_required
        and not primary_selected
        and not primary_document.lower().endswith(".paper")
        and complete_submission_name in item_names
    ):
        selected.add(complete_submission_name)
    selected_names = tuple(sorted(selected, key=str.lower))
    return {
        "index_status": "CACHED",
        "index_document_count": len(items),
        "index_pdf_count": pdf_count,
        "index_relevant_document_count": relevant_count,
        "index_metric_alias_count": len(alias_metrics),
        "selected_document_names": selected_names,
        "cached_selected_document_count": sum(
            int((accession_dir / name).is_file())
            for name in selected_names
        ),
    }


def _delta_action(
    *,
    priority: int,
    dp3_decision: str,
    index_status: str,
    selected_document_count: int,
    cached_selected_document_count: int,
    source_category: str,
    primary_document: str,
) -> tuple[str, str]:
    if priority <= 0:
        return "NO_DELTA_LOW_YIELD_FORM", "form has no targeted source-policy signal"
    if dp3_decision == "INCLUDE":
        return "NO_DELTA_ALREADY_IN_DP3", "accession is already in the sealed DP3 corpus"
    if index_status in {"MISSING", "INVALID"}:
        return (
            "HYDRATE_INDEX_ONLY",
            "filing is actionable but archive document metadata is unavailable",
        )
    if selected_document_count <= 0:
        if primary_document.lower().endswith(".paper"):
            return (
                "NO_DELTA_NON_ELECTRONIC_PAPER_FILING",
                "SEC metadata identifies a paper filing with no electronic document",
            )
        if source_category in {
            "DOMESTIC_MATERIAL_EVENT",
            "FOREIGN_RESULTS_EVENT",
        }:
            return (
                "NO_DELTA_METADATA_NEGATIVE",
                "cached index has no results, exhibit, PDF, or metric signal",
            )
        return (
            "REVIEW_PRIMARY_DOCUMENT_METADATA",
            "targeted filing has no selected index document under the current rules",
        )
    if cached_selected_document_count < selected_document_count:
        return (
            "HYDRATE_SELECTED_DOCUMENTS",
            "selected delta documents are not fully cached",
        )
    return (
        "PARSE_NEW_CACHED_DOCUMENT_HASHES",
        "selected documents are cached and were not included in DP3",
    )


def _scope_maps(
    scope_rows: Iterable[Mapping[str, str]],
) -> tuple[
    dict[str, tuple[str, ...]],
    dict[str, tuple[str, ...]],
    dict[str, bool],
]:
    metrics: dict[str, set[str]] = defaultdict(set)
    packs: dict[str, set[str]] = defaultdict(set)
    development: dict[str, bool] = defaultdict(bool)
    for row in scope_rows:
        ticker = str(row.get("ticker") or "")
        if not ticker:
            continue
        development[ticker] = development[ticker] or str(
            row.get("development_overlay") or ""
        ) in {"1", "true", "True"}
        if str(row.get("applicability_status") or "") != "APPLICABLE":
            continue
        metrics[ticker].add(str(row.get("metric_id") or ""))
        packs[ticker].add(str(row.get("metric_pack") or ""))
    return (
        {
            ticker: tuple(sorted(value for value in values if value))
            for ticker, values in metrics.items()
        },
        {
            ticker: tuple(sorted(value for value in values if value))
            for ticker, values in packs.items()
        },
        dict(development),
    )


def _form_category(form_type: str) -> tuple[str, str]:
    form = form_type.upper()
    if form in EVENT_FORMS:
        return "EVENT", "ACTIONABLE_SOURCE_LANE"
    if form in PERIODIC_ANNUAL_FORMS | PERIODIC_INTERIM_FORMS:
        return "PERIODIC", "ACTIONABLE_SOURCE_LANE"
    if form in PROXY_FORMS:
        return "PROXY", "ACTIONABLE_SOURCE_LANE"
    if form in ANNUAL_REPORT_FORMS:
        return "ANNUAL_REPORT", "ACTIONABLE_SOURCE_LANE"
    if form in TRANSACTION_FORMS:
        return "REGISTRATION_TRANSACTION", "ACTIONABLE_SOURCE_LANE"
    if form in SUPPLEMENTAL_DISCLOSURE_FORMS:
        return "SUPPLEMENTAL_DISCLOSURE", "ACTIONABLE_SOURCE_LANE"
    if form in {"DEFA14A", "PRE 14A", "PRE 14A/A", "IRANNOTICE"}:
        return "AUDIT_ONLY", "AUDIT_ONLY_LOW_YIELD"
    return "OTHER", "IRRELEVANT_TO_SPECIALIZED_METRICS"


def build_source_exhaustion(
    connection: sqlite3.Connection,
    *,
    members: Mapping[str, Mapping[str, str]],
    submissions_cache_dir: Path,
    cache_dir: Path,
    scope_rows: Sequence[Mapping[str, str]],
    metric_acceptance_rows: Sequence[Mapping[str, str]],
    dp3_decisions: Sequence[Mapping[str, str]],
    metric_aliases: Mapping[str, Sequence[str]],
    registration_anchors: Mapping[str, date],
    source_id: str,
    active_start_date: str,
    inactive_start_date: str,
    asof_date: str,
    expected_identity_count: int,
    manifest_version: str = SOURCE_EXHAUSTION_VERSION,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, str]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[str],
    dict[str, object],
]:
    errors: list[str] = []
    if len(members) != expected_identity_count:
        errors.append(
            f"transportation identities={len(members)} expected={expected_identity_count}"
        )
    inventory, gaps, inventory_errors, submission_summary = (
        load_submission_inventory(
            submissions_cache_dir,
            members=members,
            active_start_date=active_start_date,
            inactive_start_date=inactive_start_date,
            asof_date=asof_date,
            manifest_version=manifest_version,
        )
    )
    errors.extend(inventory_errors)
    ticker_metrics, ticker_packs, development = _scope_maps(scope_rows)
    disposition_by_metric = {
        str(row.get("metric_id") or ""): dict(row)
        for row in metric_acceptance_rows
    }
    # The delta parser will scan every specialized metric once on every new
    # document hash. Current acceptance affects priority reporting, not which
    # metrics are searched.
    target_metrics = set(disposition_by_metric)
    near_gate_metrics = {
        metric_id
        for metric_id, row in disposition_by_metric.items()
        if str(row.get("accepted_breadth_gate_pass") or "") in {"0", ""}
        and min(
            int(row.get("broad_accepted_shortfall") or 10**6),
            int(row.get("best_accepted_niche_shortfall") or 10**6),
        )
        in {1, 2}
    }
    compiled_aliases = _compile_aliases(metric_aliases)
    tickers = sorted(members)
    placeholders = ",".join("?" for _ in tickers)
    database_rows = connection.execute(
        f"""
        SELECT ticker, accession_number, form_type
        FROM fact_sec_filing
        WHERE source_id=? AND ticker IN ({placeholders})
        """,
        (source_id, *tickers),
    ).fetchall()
    database_by_key = {
        (str(row["ticker"]), str(row["accession_number"])): str(
            row["form_type"] or ""
        ).upper()
        for row in database_rows
    }
    dp3_by_key = {
        (
            str(row.get("ticker") or ""),
            str(row.get("accession_number") or ""),
        ): dict(row)
        for row in dp3_decisions
    }
    filing_rows: list[dict[str, object]] = []
    delta_rows: list[dict[str, object]] = []
    all_form_stats: dict[str, list[dict[str, object]]] = defaultdict(list)
    for source in inventory:
        ticker = source["ticker"]
        member = members[ticker]
        window_start, window_end = _source_window(
            member,
            active_start_date=active_start_date,
            inactive_start_date=inactive_start_date,
            asof_date=asof_date,
        )
        form = source["form_type"].upper()
        key = (ticker, source["accession_number"])
        database_form = database_by_key.get(key, "")
        dp3 = dp3_by_key.get(key, {})
        applicable = tuple(
            metric_id
            for metric_id in ticker_metrics.get(ticker, ())
            if metric_id in target_metrics
        )
        near = tuple(
            metric_id
            for metric_id in applicable
            if metric_id in near_gate_metrics
        )
        source_category, priority, basis = classify_source(
            form_type=form,
            items=source["items"],
            primary_description=source["primary_document_description"],
            development_overlay=bool(development.get(ticker)),
            registration_window=_inside_registration_window(
                source["filing_date"],
                anchor=registration_anchors.get(ticker),
            ),
        )
        index = (
            _index_metadata(
                cache_dir=cache_dir,
                cik=_normalized_cik(member.get("cik")),
                accession_number=source["accession_number"],
                primary_document=source["primary_document"],
                target_metrics=applicable,
                aliases=compiled_aliases,
                source_category=source_category,
            )
            if form in RELEVANT_FORMS
            else {
                "index_status": "NOT_INSPECTED_IRRELEVANT_FORM",
                "index_document_count": 0,
                "index_pdf_count": 0,
                "index_relevant_document_count": 0,
                "index_metric_alias_count": 0,
                "selected_document_names": (),
                "cached_selected_document_count": 0,
            }
        )
        selected_names = tuple(
            cast(Sequence[str], index["selected_document_names"])
        )
        action, reason = _delta_action(
            priority=priority,
            dp3_decision=str(dp3.get("decision") or ""),
            index_status=str(index["index_status"]),
            selected_document_count=len(selected_names),
            cached_selected_document_count=_as_int(
                index["cached_selected_document_count"]
            ),
            source_category=source_category,
            primary_document=source["primary_document"],
        )
        row: dict[str, object] = {
            "manifest_version": manifest_version,
            "ticker": ticker,
            "company_name": str(member.get("company_name") or ""),
            "universe_role": str(member.get("universe_role") or ""),
            "calibration_cohort": str(
                member.get("calibration_cohort") or ""
            ),
            "industry": str(member.get("industry") or ""),
            "development_overlay": int(bool(development.get(ticker))),
            "cik": _normalized_cik(member.get("cik")),
            "source_window_start": window_start,
            "source_window_end": window_end,
            **source,
            "source_category": source_category,
            "candidate_priority": priority,
            "candidate_basis": basis,
            "in_fact_sec_filing": int(bool(database_form)),
            "fact_form_type": database_form,
            "in_dp3_decisions": int(bool(dp3)),
            "dp3_decision": str(dp3.get("decision") or ""),
            "dp3_selection_rule": str(
                dp3.get("selection_rule") or ""
            ),
            "index_status": index["index_status"],
            "index_document_count": index["index_document_count"],
            "index_pdf_count": index["index_pdf_count"],
            "index_relevant_document_count": index[
                "index_relevant_document_count"
            ],
            "index_metric_alias_count": index[
                "index_metric_alias_count"
            ],
            "selected_document_names": "|".join(selected_names),
            "selected_document_count": len(selected_names),
            "cached_selected_document_count": index[
                "cached_selected_document_count"
            ],
            "target_metric_ids": "|".join(applicable),
            "target_metric_count": len(applicable),
            "near_gate_metric_ids": "|".join(near),
            "delta_action": action,
            "delta_reason": reason,
            "index_url": (
                "https://www.sec.gov/Archives/edgar/data/"
                f"{int(_normalized_cik(member.get('cik')) or '0')}/"
                f"{source['accession_number'].replace('-', '')}/index.json"
            ),
        }
        all_form_stats[form].append(row)
        if form not in RELEVANT_FORMS:
            continue
        filing_rows.append(row)
        if action.startswith("NO_DELTA"):
            continue
        delta_rows.append(
            {
                "candidate_key": (
                    f"{ticker}|{source['accession_number']}|{action}"
                ),
                **{field: row[field] for field in DELTA_FIELDS if field in row},
                "database_registry_gap": int(not bool(database_form)),
            }
        )
    filing_rows.sort(
        key=lambda row: (
            str(row["ticker"]),
            str(row["filing_date"]),
            str(row["accession_number"]),
        )
    )
    delta_rows.sort(
        key=lambda row: (
            _as_int(row["candidate_priority"]),
            str(row["ticker"]),
            str(row["filing_date"]),
            str(row["accession_number"]),
        )
    )
    duplicate_candidates = [
        key
        for key, count in Counter(
            str(row["candidate_key"]) for row in delta_rows
        ).items()
        if count > 1
    ]
    if duplicate_candidates:
        errors.append(
            f"duplicate delta candidate keys={duplicate_candidates[:10]}"
        )
    form_rows: list[dict[str, object]] = []
    for form, rows in sorted(all_form_stats.items()):
        category, disposition = _form_category(form)
        form_rows.append(
            {
                "manifest_version": manifest_version,
                "form_type": form,
                "source_category": category,
                "scope_disposition": disposition,
                "filing_count": len(rows),
                "ticker_count": len(
                    {str(row["ticker"]) for row in rows}
                ),
                "database_registry_count": sum(
                    _as_int(row["in_fact_sec_filing"]) for row in rows
                ),
                "database_registry_gap_count": sum(
                    1 - _as_int(row["in_fact_sec_filing"]) for row in rows
                ),
                "dp3_included_count": sum(
                    str(row["dp3_decision"]) == "INCLUDE"
                    for row in rows
                ),
                "dp3_excluded_count": sum(
                    str(row["dp3_decision"]).startswith("EXCLUDE")
                    for row in rows
                ),
                "dp3_missing_count": sum(
                    not str(row["dp3_decision"]) for row in rows
                ),
                "cached_index_count": sum(
                    str(row["index_status"]) == "CACHED"
                    for row in rows
                ),
                "missing_index_count": sum(
                    str(row["index_status"]) != "CACHED"
                    for row in rows
                ),
                "actionable_delta_count": sum(
                    not str(row["delta_action"]).startswith("NO_DELTA")
                    for row in rows
                ),
            }
        )
    candidate_metrics: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in delta_rows:
        for metric_id in str(row["target_metric_ids"]).split("|"):
            if metric_id:
                candidate_metrics[metric_id].append(row)
    gap_tickers = {str(row["ticker"]) for row in gaps}
    applicable_tickers: dict[str, set[str]] = defaultdict(set)
    for ticker, metrics in ticker_metrics.items():
        for metric_id in metrics:
            applicable_tickers[metric_id].add(ticker)
    metric_rows: list[dict[str, object]] = []
    for metric_id, acceptance in sorted(disposition_by_metric.items()):
        candidates = candidate_metrics.get(metric_id, [])
        actions = Counter(str(row["delta_action"]) for row in candidates)
        disposition = str(acceptance.get("metric_disposition") or "")
        if disposition in CALIBRATION_COMPLETE_DISPOSITIONS:
            status = "ALREADY_FORMAL_GATE_PASS"
        elif candidates:
            status = "DELTA_SOURCE_CANDIDATES_IDENTIFIED"
        elif applicable_tickers[metric_id] & gap_tickers:
            status = "SUBMISSIONS_CACHE_GAP_BLOCKS_EXHAUSTION"
        else:
            status = "NO_ADDITIONAL_SEC_SOURCE_CANDIDATE"
        metric_rows.append(
            {
                "manifest_version": manifest_version,
                "metric_id": metric_id,
                "metric_pack": str(acceptance.get("metric_pack") or ""),
                "source_lane": str(acceptance.get("source_lane") or ""),
                "metric_disposition": disposition,
                "post_active_accepted_count": int(
                    acceptance.get("post_active_accepted_count") or 0
                ),
                "post_active_usable_count": int(
                    acceptance.get("post_active_usable_count") or 0
                ),
                "broad_required_count": int(
                    acceptance.get("broad_required_count") or 0
                ),
                "broad_accepted_shortfall": int(
                    acceptance.get("broad_accepted_shortfall") or 0
                ),
                "best_accepted_niche_shortfall": int(
                    acceptance.get("best_accepted_niche_shortfall") or 0
                ),
                "historical_depth_gate_pass": int(
                    acceptance.get("historical_depth_gate_pass") or 0
                ),
                "applicable_ticker_count": len(
                    applicable_tickers[metric_id]
                ),
                "candidate_ticker_count": len(
                    {str(row["ticker"]) for row in candidates}
                ),
                "candidate_filing_count": len(candidates),
                "priority_1_filing_count": sum(
                    _as_int(row["candidate_priority"]) == 1
                    for row in candidates
                ),
                "priority_2_filing_count": sum(
                    _as_int(row["candidate_priority"]) == 2
                    for row in candidates
                ),
                "priority_3_filing_count": sum(
                    _as_int(row["candidate_priority"]) == 3
                    for row in candidates
                ),
                "index_hydration_count": actions["HYDRATE_INDEX_ONLY"],
                "selected_document_hydration_count": actions[
                    "HYDRATE_SELECTED_DOCUMENTS"
                ],
                "cached_document_parse_count": actions[
                    "PARSE_NEW_CACHED_DOCUMENT_HASHES"
                ],
                "submission_gap_ticker_count": len(
                    applicable_tickers[metric_id] & gap_tickers
                ),
                "source_exhaustion_status": status,
            }
        )
    summary: dict[str, object] = {
        **submission_summary,
        "identity_count": len(members),
        "active_identity_count": sum(
            str(member.get("universe_role") or "") == "active"
            for member in members.values()
        ),
        "inactive_identity_count": sum(
            str(member.get("universe_role") or "") != "active"
            for member in members.values()
        ),
        "relevant_filing_count": len(filing_rows),
        "delta_candidate_count": len(delta_rows),
        "index_metadata_gap_count": sum(
            str(row["delta_action"]) == "HYDRATE_INDEX_ONLY"
            for row in delta_rows
        ),
        "document_delta_candidate_count": sum(
            str(row["delta_action"]) != "HYDRATE_INDEX_ONLY"
            for row in delta_rows
        ),
        "database_registry_gap_count": sum(
            _as_int(row["database_registry_gap"]) for row in delta_rows
        ),
        "source_gap_count": len(gaps),
        "form_count": len(form_rows),
        "metric_count": len(metric_rows),
        "delta_action_counts": dict(
            sorted(
                Counter(
                    str(row["delta_action"]) for row in delta_rows
                ).items()
            )
        ),
        "candidate_priority_counts": dict(
            sorted(
                Counter(
                    str(row["candidate_priority"]) for row in delta_rows
                ).items()
            )
        ),
        "source_category_counts": dict(
            sorted(
                Counter(
                    str(row["source_category"]) for row in delta_rows
                ).items()
            )
        ),
        "errors": errors,
    }
    return (
        filing_rows,
        delta_rows,
        gaps,
        form_rows,
        metric_rows,
        errors,
        summary,
    )


def _artifact(path: Path, row_count: int) -> dict[str, object]:
    return {
        "path": str(path.resolve()),
        "row_count": row_count,
        "sha256": file_sha256(path),
    }


def write_source_exhaustion(
    *,
    filing_rows: Sequence[Mapping[str, object]],
    delta_rows: Sequence[Mapping[str, object]],
    gap_rows: Sequence[Mapping[str, object]],
    form_rows: Sequence[Mapping[str, object]],
    metric_rows: Sequence[Mapping[str, object]],
    summary: Mapping[str, object],
    input_artifacts: Mapping[str, Mapping[str, object]],
    output_dir: Path,
    manifest_version: str = SOURCE_EXHAUSTION_VERSION,
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    filing_path = (
        output_dir / "transportation_source_exhaustion_filing_inventory.csv"
    )
    delta_path = (
        output_dir / "transportation_source_exhaustion_delta_candidates.csv"
    )
    gap_path = output_dir / "transportation_source_exhaustion_gaps.csv"
    form_path = (
        output_dir / "transportation_source_exhaustion_form_inventory.csv"
    )
    metric_path = (
        output_dir / "transportation_source_exhaustion_metric_targets.csv"
    )
    manifest_path = (
        output_dir / "transportation_source_exhaustion_manifest.json"
    )
    write_csv_atomic(filing_path, FILING_FIELDS, filing_rows)
    write_csv_atomic(delta_path, DELTA_FIELDS, delta_rows)
    write_csv_atomic(gap_path, GAP_FIELDS, gap_rows)
    write_csv_atomic(form_path, FORM_FIELDS, form_rows)
    write_csv_atomic(metric_path, METRIC_FIELDS, metric_rows)
    errors = list(cast(Sequence[str], summary.get("errors") or ()))
    index_metadata_gap_count = _as_int(
        summary.get("index_metadata_gap_count") or 0
    )
    metadata_complete = (
        not errors
        and not gap_rows
        and index_metadata_gap_count == 0
    )
    source_complete = (
        metadata_complete
        and not delta_rows
    )
    delta_actions = {
        str(row.get("delta_action") or "")
        for row in delta_rows
    }
    documents_parse_ready = bool(delta_rows) and delta_actions == {
        "PARSE_NEW_CACHED_DOCUMENT_HASHES"
    }
    acceptance = (
        "FAIL"
        if errors
        else (
            "PASS_SOURCE_EXHAUSTED"
            if source_complete
            else "PASS_WITH_REQUIRED_DELTA"
        )
    )
    payload = {
        "acceptance": acceptance,
        "gate": "DP6E_SOURCE_UNIVERSE_EXHAUSTION_AUDIT",
        "manifest_version": manifest_version,
        "model_family": "transportation",
        "source_universe_scope": "SEC_PRIMARY_DISCLOSURES",
        **dict(summary),
        "metadata_exhaustion_complete": metadata_complete,
        "delta_document_manifest_ready": (
            metadata_complete and bool(delta_rows)
        ),
        "sec_source_exhaustion_complete": source_complete,
        "non_sec_primary_source_audit_complete": False,
        "global_source_exhaustion_complete": False,
        # Backward-compatible name; this audit covers SEC disclosures only.
        "source_exhaustion_complete": source_complete,
        "database_mode": "read_only",
        "network_requests": 0,
        "parser_invocations": 0,
        "feature_build_invocations": 0,
        "historical_materialization_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_invocations": 0,
        "hydration_authorized": False,
        "parser_execution_authorized": False,
        "historical_materialization_authorized": False,
        "production_promotion_authorized": False,
        "inputs": dict(input_artifacts),
        "artifacts": {
            "filing_inventory": _artifact(
                filing_path,
                len(filing_rows),
            ),
            "delta_candidates": _artifact(
                delta_path,
                len(delta_rows),
            ),
            "source_gaps": _artifact(gap_path, len(gap_rows)),
            "form_inventory": _artifact(form_path, len(form_rows)),
            "metric_targets": _artifact(metric_path, len(metric_rows)),
        },
        "next_gate": (
            (
                "HYDRATE_MISSING_SUBMISSION_SHARDS_AND_CANDIDATE_INDEXES_ONLY"
                if not metadata_complete
                else (
                    "BUILD_AND_VALIDATE_DELTA_PARSER_PLAN"
                    if documents_parse_ready
                    else (
                        "SEAL_AND_HYDRATE_DELTA_DOCUMENT_MANIFEST"
                        if delta_rows
                        else "AUDIT_NON_SEC_PRIMARY_SOURCE_LANES"
                    )
                )
            )
            if not errors
            else "REPAIR_SOURCE_UNIVERSE_AUDIT_ERRORS"
        ),
    }
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    return payload


def validate_written_source_exhaustion(
    *,
    output_dir: Path,
    expected_identity_count: int = 160,
    expected_metric_count: int = 90,
) -> list[str]:
    manifest_path = (
        output_dir / "transportation_source_exhaustion_manifest.json"
    )
    if not manifest_path.is_file():
        return [f"missing source-exhaustion manifest: {manifest_path}"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"invalid source-exhaustion manifest: {exc}"]
    errors: list[str] = []
    if manifest.get("acceptance") == "FAIL":
        errors.append("source-exhaustion manifest acceptance is FAIL")
    if manifest.get("manifest_version") != SOURCE_EXHAUSTION_VERSION:
        errors.append("source-exhaustion manifest version mismatch")
    if int(manifest.get("identity_count") or 0) != expected_identity_count:
        errors.append("source-exhaustion identity count mismatch")
    if int(manifest.get("metric_count") or 0) != expected_metric_count:
        errors.append("source-exhaustion metric count mismatch")
    metadata_complete = bool(
        manifest.get("metadata_exhaustion_complete")
    )
    if metadata_complete and (
        int(manifest.get("source_gap_count") or 0) != 0
        or int(manifest.get("index_metadata_gap_count") or 0) != 0
    ):
        errors.append(
            "metadata exhaustion cannot pass with source or index gaps"
        )
    for field in (
        "network_requests",
        "parser_invocations",
        "feature_build_invocations",
        "historical_materialization_invocations",
        "calibration_invocations",
        "portfolio_invocations",
    ):
        if int(manifest.get(field) or 0) != 0:
            errors.append(f"{field} must be zero")
    for field in (
        "hydration_authorized",
        "parser_execution_authorized",
        "historical_materialization_authorized",
        "production_promotion_authorized",
    ):
        if manifest.get(field) is not False:
            errors.append(f"{field} must be false")
    if manifest.get("non_sec_primary_source_audit_complete") is not False:
        errors.append(
            "DP6E must not claim completion of non-SEC source lanes"
        )
    if manifest.get("global_source_exhaustion_complete") is not False:
        errors.append("DP6E must not claim global source exhaustion")
    expected_fields = {
        "filing_inventory": FILING_FIELDS,
        "delta_candidates": DELTA_FIELDS,
        "source_gaps": GAP_FIELDS,
        "form_inventory": FORM_FIELDS,
        "metric_targets": METRIC_FIELDS,
    }
    for name, fields in expected_fields.items():
        artifact = (manifest.get("artifacts") or {}).get(name) or {}
        path = Path(str(artifact.get("path") or ""))
        if not path.is_file():
            errors.append(f"missing {name} artifact: {path}")
            continue
        rows = read_csv(path)
        if rows and tuple(rows[0]) != fields:
            errors.append(
                f"{name} fields={tuple(rows[0])!r} expected={fields!r}"
            )
        if len(rows) != int(artifact.get("row_count") or 0):
            errors.append(f"{name} row count mismatch")
        if file_sha256(path) != str(artifact.get("sha256") or ""):
            errors.append(f"{name} SHA-256 mismatch")
    delta_artifact = (manifest.get("artifacts") or {}).get(
        "delta_candidates"
    ) or {}
    delta_path = Path(str(delta_artifact.get("path") or ""))
    if delta_path.is_file():
        delta_rows = read_csv(delta_path)
        keys = [row["candidate_key"] for row in delta_rows]
        if len(keys) != len(set(keys)):
            errors.append("delta candidate keys are not unique")
        if any(row["delta_action"].startswith("NO_DELTA") for row in delta_rows):
            errors.append("delta artifact contains non-actionable rows")
    return errors

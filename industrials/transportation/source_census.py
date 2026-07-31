from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from dedicated_parser.catalog import accession_directory, relevant_document_names
from dedicated_parser.contracts import FilingRef, file_sha256
from industrials.core.reports import write_csv_atomic, write_text_atomic
from industrials.transportation.dedicated_parser_adapter import get_registry


MODEL_FAMILY = "transportation"
BASE_PERIODIC_FORMS = frozenset(
    {
        "10-K",
        "10-K/A",
        "10-Q",
        "10-Q/A",
        "10-12B",
        "10-12B/A",
        "20-F",
        "20-F/A",
        "40-F",
        "40-F/A",
        "6-K",
        "6-K/A",
    }
)
REGISTRATION_FORMS = frozenset(
    {
        "S-1",
        "S-1/A",
        "F-1",
        "F-1/A",
        "F-4",
        "F-4/A",
        "424B3",
        "424B4",
    }
)
EVENT_FORMS = frozenset({"8-K", "8-K/A", "6-K", "6-K/A"})
EVENT_METADATA_PATTERN = re.compile(
    r"\b(?:earnings|financial\s+results|operating\s+results|"
    r"quarterly\s+results|annual\s+results|results\s+release|"
    r"earnings\s+release|press\s+release|investor\s+presentation)\b",
    re.IGNORECASE,
)
VALID_GAP_DISPOSITIONS = frozenset(
    {
        "PERMANENTLY_UNAVAILABLE",
        "NOT_REQUIRED",
        "SUPERSEDED_BY_FULL_SUBMISSION",
    }
)

CENSUS_FIELDS = (
    "manifest_version",
    "dp0_scope_sha256",
    "row_key",
    "ticker",
    "company_name",
    "universe_role",
    "calibration_cohort",
    "industry",
    "primary_archetype",
    "cik",
    "accession_number",
    "form_type",
    "filing_date",
    "accepted_at",
    "report_date",
    "source_id",
    "document_name",
    "document_kind",
    "local_path",
    "file_size",
    "content_sha256",
    "is_primary",
    "is_full_submission",
    "is_exhibit",
    "selection_tier",
    "selection_rule",
    "applicable_metric_packs",
    "applicable_metric_ids",
    "applicable_metric_count",
    "cache_status",
    "gap_disposition",
    "duplicate_cik_accession_count",
    "duplicate_content_count",
)

DECISION_FIELDS = (
    "manifest_version",
    "ticker",
    "cik",
    "accession_number",
    "form_type",
    "filing_date",
    "candidate_type",
    "decision",
    "selection_rule",
    "submissions_items",
    "index_status",
    "index_event_match",
    "selected_document_count",
    "reason",
)

GAP_FIELDS = (
    "manifest_version",
    "gap_key",
    "gap_type",
    "ticker",
    "cik",
    "accession_number",
    "form_type",
    "filing_date",
    "document_name",
    "local_path",
    "cache_status",
    "required_action",
    "gap_disposition",
    "override_reviewer",
    "override_reviewed_at",
    "override_reason",
)

GAP_OVERRIDE_FIELDS = (
    "ticker",
    "accession_number",
    "document_name",
    "gap_disposition",
    "reviewer",
    "reviewed_at",
    "reason",
)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [{str(key): str(value or "").strip() for key, value in row.items()} for row in csv.DictReader(handle)]


def canonical_rows_hash(
    rows: Iterable[Mapping[str, object]],
    *,
    fields: Sequence[str],
) -> str:
    digest = hashlib.sha256()
    for row in rows:
        payload = {field: "" if row.get(field, "") is None else str(row.get(field, "")) for field in fields}
        digest.update(
            json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def read_only_connection(path: Path, *, timeout_sec: float = 120.0) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Industrials database does not exist: {resolved}")
    connection = sqlite3.connect(
        f"{resolved.as_uri()}?mode=ro",
        uri=True,
        timeout=timeout_sec,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _normalized_cik(value: object) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits.zfill(10) if digits else ""


def _members(
    connection: sqlite3.Connection,
    *,
    asof_date: str,
    active_source_id: str,
    historical_source_id: str,
) -> dict[str, dict[str, str]]:
    rows = connection.execute(
        """
        SELECT t.ticker, c.company_name, c.cik, t.industry,
               t.calibration_cohort_id,
               COALESCE((
                 SELECT MIN(history.start_date)
                 FROM dim_universe_membership AS history
                 WHERE history.ticker=t.ticker
                   AND history.model_family=t.model_family
               ), '') AS membership_start_date,
               COALESCE((
                 SELECT MAX(history.end_date)
                 FROM dim_universe_membership AS history
                 WHERE history.ticker=t.ticker
                   AND history.model_family=t.model_family
                   AND COALESCE(history.end_date, '')<>''
               ), '') AS membership_end_date,
               CASE
                 WHEN EXISTS (
                   SELECT 1
                   FROM dim_universe_membership AS active
                   WHERE active.ticker=t.ticker
                     AND active.model_family=t.model_family
                     AND active.membership_source_id=?
                     AND active.membership_status='active'
                     AND active.start_date<=?
                     AND COALESCE(active.end_date, '9999-12-31')>=?
                 ) THEN 'active'
                 WHEN EXISTS (
                   SELECT 1
                   FROM dim_universe_membership AS historical
                   WHERE historical.ticker=t.ticker
                     AND historical.model_family=t.model_family
                     AND historical.membership_source_id=?
                 ) THEN 'delisted_usable'
                 ELSE 'delisted_excluded'
               END AS universe_role
        FROM dim_industrials_taxonomy AS t
        JOIN dim_company AS c ON c.company_id=t.company_id
        WHERE t.model_family=?
        ORDER BY t.ticker
        """,
        (
            active_source_id,
            asof_date,
            asof_date,
            historical_source_id,
            MODEL_FAMILY,
        ),
    ).fetchall()
    return {
        str(row["ticker"]): {
            "ticker": str(row["ticker"]),
            "company_name": str(row["company_name"] or ""),
            "cik": _normalized_cik(row["cik"]),
            "industry": str(row["industry"] or ""),
            "calibration_cohort": str(row["calibration_cohort_id"] or ""),
            "universe_role": str(row["universe_role"] or ""),
            "membership_start_date": str(
                row["membership_start_date"] or ""
            ),
            "membership_end_date": str(row["membership_end_date"] or ""),
        }
        for row in rows
    }


def _scope_by_ticker(
    *,
    final_scope_path: Path,
    support_scope_path: Path,
) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "metrics": set(),
            "packs": set(),
            "primary_archetype": "",
            "universe_role": "",
        }
    )
    for path, metric_field in (
        (final_scope_path, "metric_id"),
        (support_scope_path, "support_metric_id"),
    ):
        for row in read_csv(path):
            if row.get("applicability_status") != "APPLICABLE":
                continue
            ticker = row.get("ticker", "").upper()
            source_lane = row.get("source_lane", "")
            if path == final_scope_path and source_lane != "DP":
                continue
            metric_id = row.get(metric_field, "")
            if metric_id:
                cast_metrics = output[ticker]["metrics"]
                assert isinstance(cast_metrics, set)
                cast_metrics.add(metric_id)
            metric_pack = row.get("metric_pack", "")
            if metric_pack:
                cast_packs = output[ticker]["packs"]
                assert isinstance(cast_packs, set)
                cast_packs.add(metric_pack)
            output[ticker]["primary_archetype"] = row.get("primary_archetype", "")
            output[ticker]["universe_role"] = row.get("universe_role", "")
    return output


def _filing_rows(
    connection: sqlite3.Connection,
    *,
    tickers: Sequence[str],
    members: Mapping[str, Mapping[str, str]],
    source_id: str,
    start_date: str,
    legacy_inactive_start_date: str,
    asof_date: str,
) -> list[dict[str, str]]:
    if not tickers:
        return []
    placeholders = ",".join("?" for _ in tickers)
    candidates = [
        {
            key: str(row[key] or "")
            for key in (
                "ticker",
                "cik",
                "source_id",
                "accession_number",
                "form_type",
                "filing_date",
                "accepted_at",
                "report_date",
                "primary_document",
            )
        }
        for row in connection.execute(
            f"""
            SELECT ticker, cik, source_id, accession_number, form_type,
                   filing_date, accepted_at, report_date, primary_document
            FROM fact_sec_filing
            WHERE source_id=?
              AND ticker IN ({placeholders})
              AND filing_date>=? AND filing_date<=?
              AND COALESCE(primary_document, '')<>''
            ORDER BY ticker, filing_date, accession_number
            """,
            (
                source_id,
                *tickers,
                legacy_inactive_start_date,
                asof_date,
            ),
        ).fetchall()
    ]
    output: list[dict[str, str]] = []
    for row in candidates:
        member = members[row["ticker"]]
        if member["universe_role"] == "active":
            if row["filing_date"] >= start_date:
                output.append(row)
            continue
        membership_end = member["membership_end_date"] or asof_date
        if row["filing_date"] <= membership_end:
            output.append(row)
    return output


def _raw_xbrl_accessions(
    connection: sqlite3.Connection,
    *,
    tickers: Sequence[str],
) -> set[tuple[str, str]]:
    if not tickers or not _table_exists(connection, "fact_sec_xbrl_fact_raw"):
        return set()
    placeholders = ",".join("?" for _ in tickers)
    return {
        (str(row["ticker"]), str(row["accession_number"]))
        for row in connection.execute(
            f"""
            SELECT DISTINCT ticker, accession_number
            FROM fact_sec_xbrl_fact_raw
            WHERE ticker IN ({placeholders})
              AND COALESCE(accession_number, '')<>''
            """,
            tuple(tickers),
        ).fetchall()
    }


def _submissions_metadata(
    submissions_cache_dir: Path,
    *,
    ciks: Iterable[str],
) -> dict[tuple[str, str], dict[str, str]]:
    output: dict[tuple[str, str], dict[str, str]] = {}
    for cik in sorted(set(ciks)):
        path = submissions_cache_dir / f"CIK{cik}.json"
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        recent = (payload.get("filings") or {}).get("recent") or {}
        if not isinstance(recent, dict):
            continue
        accessions = recent.get("accessionNumber") or []
        items = recent.get("items") or []
        descriptions = recent.get("primaryDocDescription") or []
        if not isinstance(accessions, list):
            continue
        for index, accession in enumerate(accessions):
            item_value = items[index] if isinstance(items, list) and index < len(items) else ""
            description = descriptions[index] if isinstance(descriptions, list) and index < len(descriptions) else ""
            output[(cik, str(accession or ""))] = {
                "items": str(item_value or ""),
                "primary_document_description": str(description or ""),
            }
    return output


def _registration_anchors(
    *,
    listing_dates_path: Path,
    continuity_path: Path,
    clipped_history_start: str,
) -> dict[str, date]:
    anchors: dict[str, date] = {}
    for row in read_csv(listing_dates_path):
        value = row.get("first_eligible_date", "")[:10]
        if not value or value <= clipped_history_start:
            continue
        try:
            anchors[row.get("ticker", "").upper()] = date.fromisoformat(value)
        except ValueError:
            continue
    for row in read_csv(continuity_path):
        value = (row.get("structural_break_date") or row.get("current_security_start_date") or "")[:10]
        if not value:
            continue
        try:
            anchors[row.get("ticker", "").upper()] = date.fromisoformat(value)
        except ValueError:
            continue
    return anchors


def _inside_registration_window(
    filing_date: str,
    *,
    anchor: date | None,
) -> bool:
    if anchor is None:
        return False
    try:
        filed = date.fromisoformat(filing_date[:10])
    except ValueError:
        return False
    # Registration statements can start well before trading. Post-listing
    # resale prospectus supplements quickly become repetitive for this
    # operating-metric census, so keep one bounded launch window.
    return anchor - timedelta(days=730) <= filed <= anchor + timedelta(days=90)


def _index_event_metadata(accession_dir: Path) -> tuple[str, bool]:
    path = accession_dir / "index.json"
    if not path.is_file():
        return "MISSING", False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "INVALID", False
    items = (payload.get("directory") or {}).get("item") or []
    for item in items:
        if not isinstance(item, dict):
            continue
        metadata = " ".join(
            str(item.get(field) or "") for field in ("name", "type", "document_type", "description", "title")
        )
        if EVENT_METADATA_PATTERN.search(metadata):
            return "CACHED", True
    return "CACHED", False


def _gap_overrides(path: Path) -> tuple[dict[tuple[str, str, str], dict[str, str]], list[str]]:
    rows = read_csv(path)
    errors: list[str] = []
    output: dict[tuple[str, str, str], dict[str, str]] = {}
    if rows and tuple(rows[0]) != GAP_OVERRIDE_FIELDS:
        errors.append(f"{path}: fields={tuple(rows[0])!r} expected={GAP_OVERRIDE_FIELDS!r}")
        return output, errors
    for line_number, row in enumerate(rows, start=2):
        key = (
            row.get("ticker", "").upper(),
            row.get("accession_number", ""),
            row.get("document_name", ""),
        )
        disposition = row.get("gap_disposition", "")
        if not all(key):
            errors.append(f"{path}:{line_number}: incomplete override key")
        elif key in output:
            errors.append(f"{path}:{line_number}: duplicate override key={key!r}")
        elif disposition not in VALID_GAP_DISPOSITIONS:
            errors.append(f"{path}:{line_number}: invalid gap_disposition={disposition!r}")
        elif not row.get("reviewer") or not row.get("reviewed_at") or not row.get("reason"):
            errors.append(f"{path}:{line_number}: reviewer lineage is required")
        else:
            output[key] = row
    return output, errors


def _known_document_hashes(
    connection: sqlite3.Connection,
    *,
    tickers: Sequence[str],
) -> dict[tuple[str, int, int], str]:
    catalog_hashes: dict[tuple[str, int, int], str] = {}
    if not tickers:
        return catalog_hashes
    placeholders = ",".join("?" for _ in tickers)
    if _table_exists(connection, "sec_parser_document_catalog"):
        for row in connection.execute(
            f"""
            SELECT source_path, file_size, modified_ns, content_sha256
            FROM sec_parser_document_catalog
            WHERE ticker IN ({placeholders}) AND LENGTH(content_sha256)=64
            ORDER BY cataloged_at
            """,
            tuple(tickers),
        ).fetchall():
            catalog_hashes[
                (
                    str(row["source_path"]),
                    int(row["file_size"]),
                    int(row["modified_ns"]),
                )
            ] = str(row["content_sha256"])
    return catalog_hashes


def _document_kind(
    *,
    name: str,
    is_primary: bool,
    is_full_submission: bool,
    is_event: bool,
) -> str:
    if is_full_submission:
        return "sec_full_submission_sgml"
    if Path(name).suffix.lower() == ".pdf":
        return "sec_archive_pdf"
    if is_event and not is_primary:
        return "sec_event_exhibit"
    return "sec_archive_primary" if is_primary else "sec_archive_supplemental"


def _row_key(*values: object) -> str:
    payload = "\x1f".join(str(value or "") for value in values)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_source_census(
    connection: sqlite3.Connection,
    *,
    cache_dir: Path,
    submissions_cache_dir: Path,
    final_scope_path: Path,
    support_scope_path: Path,
    listing_dates_path: Path,
    continuity_path: Path,
    dp0_manifest_path: Path,
    gap_override_path: Path,
    manifest_version: str,
    source_id: str,
    active_source_id: str,
    historical_source_id: str,
    start_date: str,
    legacy_inactive_start_date: str,
    asof_date: str,
    expected_identity_count: int,
    expected_base_accession_count: int,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    dict[str, object],
]:
    errors: list[str] = []
    dp0 = json.loads(dp0_manifest_path.read_text(encoding="utf-8"))
    dp0_scope_sha256 = str((dp0.get("hashes") or {}).get("scope_sha256") or "")
    if not dp0_scope_sha256:
        errors.append("DP0 manifest does not contain the sealed scope SHA-256")
    elif file_sha256(final_scope_path) != dp0_scope_sha256:
        errors.append("DP0 scope file hash does not match the sealed DP0 manifest")

    members = _members(
        connection,
        asof_date=asof_date,
        active_source_id=active_source_id,
        historical_source_id=historical_source_id,
    )
    if len(members) != expected_identity_count:
        errors.append(f"transportation identities={len(members)} expected={expected_identity_count}")
    scope = _scope_by_ticker(
        final_scope_path=final_scope_path,
        support_scope_path=support_scope_path,
    )
    parser_metric_ids = {request.metric_name for request in get_registry().parser_metrics}
    scoped_metric_ids = {str(metric) for ticker_scope in scope.values() for metric in ticker_scope["metrics"]}
    if scoped_metric_ids != parser_metric_ids:
        errors.append(
            "applicable scope/parser registry mismatch: "
            f"scope_only={sorted(scoped_metric_ids - parser_metric_ids)} "
            f"registry_only={sorted(parser_metric_ids - scoped_metric_ids)}"
        )

    filings = _filing_rows(
        connection,
        tickers=sorted(members),
        members=members,
        source_id=source_id,
        start_date=start_date,
        legacy_inactive_start_date=legacy_inactive_start_date,
        asof_date=asof_date,
    )
    raw_xbrl = _raw_xbrl_accessions(connection, tickers=sorted(members))
    catalog_hashes = _known_document_hashes(
        connection,
        tickers=sorted(members),
    )
    submission_metadata = _submissions_metadata(
        submissions_cache_dir,
        ciks=(member["cik"] for member in members.values()),
    )
    registration_anchors = _registration_anchors(
        listing_dates_path=listing_dates_path,
        continuity_path=continuity_path,
        clipped_history_start="2019-01-02",
    )
    overrides, override_errors = _gap_overrides(gap_override_path)
    errors.extend(override_errors)

    selected: list[tuple[dict[str, str], str, str]] = []
    decisions: list[dict[str, object]] = []
    metadata_gaps: list[dict[str, object]] = []
    selected_keys: set[tuple[str, str]] = set()
    base_keys: set[tuple[str, str]] = set()

    for filing in filings:
        ticker = filing["ticker"]
        accession = filing["accession_number"]
        key = (ticker, accession)
        form = filing["form_type"].upper()
        cik = _normalized_cik(filing["cik"] or members[ticker]["cik"])
        filing["cik"] = cik
        ref = FilingRef(
            ticker=ticker,
            cik=cik,
            accession_number=accession,
            form_type=form,
            filing_date=filing["filing_date"],
            accepted_at=filing["accepted_at"] or filing["filing_date"],
            report_date=filing["report_date"],
            primary_document=filing["primary_document"],
            source_id=filing["source_id"],
        )
        accession_dir = accession_directory(cache_dir, ref)
        index_status, index_match = _index_event_metadata(accession_dir)
        sec_metadata = submission_metadata.get((cik, accession), {})
        items = sec_metadata.get("items", "")
        primary_description = sec_metadata.get("primary_document_description", "")
        selected_rule = ""
        candidate_type = ""
        decision = "EXCLUDE"
        reason = ""

        if form in BASE_PERIODIC_FORMS and (form not in {"6-K", "6-K/A"} or key in raw_xbrl):
            candidate_type = "base_periodic"
            decision = "INCLUDE"
            selected_rule = "base_periodic_financial_6k" if form in {"6-K", "6-K/A"} else "base_periodic"
            reason = "matches historical 3,019-accession eligibility contract"
            base_keys.add(key)
        elif form in REGISTRATION_FORMS:
            candidate_type = "supplemental_registration"
            anchor = registration_anchors.get(ticker)
            if _inside_registration_window(filing["filing_date"], anchor=anchor):
                decision = "INCLUDE"
                selected_rule = "supplemental_registration_listing_window"
                reason = "registration filing is within the sealed listing/relisting/structural-break window"
            else:
                reason = "registration filing is outside every sealed listing/relisting/structural-break window"
        elif form in EVENT_FORMS:
            candidate_type = "supplemental_event"
            item_results = bool(re.search(r"(?:^|,\s*)(?:2\.02|7\.01)(?:\s*,|$)", items))
            description_match = bool(EVENT_METADATA_PATTERN.search(primary_description))
            if item_results:
                decision = "INCLUDE"
                selected_rule = "supplemental_earnings_item_2_02_or_7_01"
                reason = "SEC submissions metadata identifies Item 2.02/7.01"
            elif description_match or index_match:
                decision = "INCLUDE"
                selected_rule = "supplemental_event_index_metadata"
                reason = "cached SEC index identifies results/release/presentation"
            elif index_status in {"MISSING", "INVALID"}:
                decision = "EXCLUDE_NO_METADATA_SIGNAL"
                selected_rule = "supplemental_event_positive_metadata_only"
                reason = (
                    "no SEC submissions or cached-index results signal; unbounded "
                    "6-K/8-K hydration is outside the sealed policy"
                )
            else:
                reason = "cached metadata has no results/release/presentation indicator"
        else:
            continue

        if decision == "INCLUDE":
            selected.append((filing, candidate_type, selected_rule))
            selected_keys.add(key)
        decisions.append(
            {
                "manifest_version": manifest_version,
                "ticker": ticker,
                "cik": cik,
                "accession_number": accession,
                "form_type": form,
                "filing_date": filing["filing_date"],
                "candidate_type": candidate_type,
                "decision": decision,
                "selection_rule": selected_rule,
                "submissions_items": items,
                "index_status": index_status,
                "index_event_match": int(index_match),
                "selected_document_count": 0,
                "reason": reason,
            }
        )

    if len(base_keys) != expected_base_accession_count:
        errors.append(f"base accessions={len(base_keys)} expected={expected_base_accession_count}")

    census_rows: list[dict[str, object]] = []
    document_gaps: list[dict[str, object]] = []
    decision_by_key = {
        (str(row["ticker"]), str(row["accession_number"])): row for row in decisions if row["decision"] == "INCLUDE"
    }
    for filing, candidate_type, selection_rule in selected:
        ticker = filing["ticker"]
        accession = filing["accession_number"]
        form = filing["form_type"].upper()
        cik = _normalized_cik(filing["cik"] or members[ticker]["cik"])
        ref = FilingRef(
            ticker=ticker,
            cik=cik,
            accession_number=accession,
            form_type=form,
            filing_date=filing["filing_date"],
            accepted_at=filing["accepted_at"] or filing["filing_date"],
            report_date=filing["report_date"],
            primary_document=filing["primary_document"],
            source_id=filing["source_id"],
        )
        accession_dir = accession_directory(cache_dir, ref)
        names = (
            relevant_document_names(
                accession_dir,
                filing=ref,
                keywords=get_registry().document_keywords,
            )
            if accession_dir.is_dir()
            else ()
        )
        full_submission_name = f"{accession}.txt" if (accession_dir / f"{accession}.txt").is_file() else ""
        if not names:
            names = (filing["primary_document"],)
        ticker_scope = scope.get(ticker, {})
        metrics = sorted(str(value) for value in ticker_scope.get("metrics", set()))
        packs = sorted(str(value) for value in ticker_scope.get("packs", set()))
        selected_document_count = 0
        for name in names:
            path = accession_dir / name
            cached = path.is_file()
            content_hash = ""
            file_size = 0
            if cached:
                stat = path.stat()
                file_size = int(stat.st_size)
                content_hash = catalog_hashes.get(
                    (
                        str(path.resolve()),
                        int(stat.st_size),
                        int(stat.st_mtime_ns),
                    ),
                    "",
                )
                if not content_hash:
                    content_hash = file_sha256(path)
            is_primary = name == filing["primary_document"]
            is_full_submission = bool(full_submission_name and name == full_submission_name)
            is_event = form in EVENT_FORMS
            override = overrides.get((ticker, accession, name), {})
            disposition = override.get("gap_disposition", "") if not cached else ""
            row = {
                "manifest_version": manifest_version,
                "dp0_scope_sha256": dp0_scope_sha256,
                "row_key": _row_key(ticker, accession, name),
                "ticker": ticker,
                "company_name": members[ticker]["company_name"],
                "universe_role": str(ticker_scope.get("universe_role") or members[ticker]["universe_role"]),
                "calibration_cohort": members[ticker]["calibration_cohort"],
                "industry": members[ticker]["industry"],
                "primary_archetype": str(ticker_scope.get("primary_archetype", "")),
                "cik": cik,
                "accession_number": accession,
                "form_type": form,
                "filing_date": filing["filing_date"],
                "accepted_at": filing["accepted_at"] or filing["filing_date"],
                "report_date": filing["report_date"],
                "source_id": filing["source_id"],
                "document_name": name,
                "document_kind": _document_kind(
                    name=name,
                    is_primary=is_primary,
                    is_full_submission=is_full_submission,
                    is_event=is_event,
                ),
                "local_path": str(path.resolve()),
                "file_size": file_size,
                "content_sha256": content_hash,
                "is_primary": int(is_primary),
                "is_full_submission": int(is_full_submission),
                "is_exhibit": int(is_event and not is_primary and not is_full_submission),
                "selection_tier": candidate_type,
                "selection_rule": selection_rule,
                "applicable_metric_packs": "|".join(packs),
                "applicable_metric_ids": "|".join(metrics),
                "applicable_metric_count": len(metrics),
                "cache_status": "CACHED_HASHED" if cached else "MISSING",
                "gap_disposition": disposition,
                "duplicate_cik_accession_count": 0,
                "duplicate_content_count": 0,
            }
            census_rows.append(row)
            selected_document_count += int(cached)
            if not cached:
                document_gaps.append(
                    {
                        "manifest_version": manifest_version,
                        "gap_key": _row_key(ticker, accession, name),
                        "gap_type": "SOURCE_DOCUMENT",
                        "ticker": ticker,
                        "cik": cik,
                        "accession_number": accession,
                        "form_type": form,
                        "filing_date": filing["filing_date"],
                        "document_name": name,
                        "local_path": str(path.resolve()),
                        "cache_status": "MISSING",
                        "required_action": "HYDRATE_SEALED_DOCUMENT",
                        "gap_disposition": disposition,
                        "override_reviewer": override.get("reviewer", ""),
                        "override_reviewed_at": override.get("reviewed_at", ""),
                        "override_reason": override.get("reason", ""),
                    }
                )
        decision_by_key[(ticker, accession)]["selected_document_count"] = selected_document_count

    cik_accession_counts = Counter((str(row["cik"]), str(row["accession_number"])) for row in census_rows)
    content_counts = Counter(str(row["content_sha256"]) for row in census_rows if str(row["content_sha256"]))
    for row in census_rows:
        row["duplicate_cik_accession_count"] = cik_accession_counts[(str(row["cik"]), str(row["accession_number"]))]
        content_hash = str(row["content_sha256"])
        row["duplicate_content_count"] = content_counts[content_hash] if content_hash else 0

    census_rows.sort(
        key=lambda row: (
            str(row["ticker"]),
            str(row["filing_date"]),
            str(row["accession_number"]),
            0 if int(str(row["is_primary"])) else 1,
            str(row["document_name"]),
        )
    )
    decisions.sort(
        key=lambda row: (
            str(row["ticker"]),
            str(row["filing_date"]),
            str(row["accession_number"]),
        )
    )
    gaps = sorted(
        [*metadata_gaps, *document_gaps],
        key=lambda row: (
            str(row["ticker"]),
            str(row["filing_date"]),
            str(row["accession_number"]),
            str(row["gap_type"]),
            str(row["document_name"]),
        ),
    )

    unresolved_gaps = [row for row in gaps if not str(row.get("gap_disposition") or "")]
    stale_override_keys = sorted(
        set(overrides)
        - {
            (
                str(row["ticker"]),
                str(row["accession_number"]),
                str(row["document_name"]),
            )
            for row in gaps
        }
    )
    if stale_override_keys:
        errors.append(f"gap overrides do not match current gaps={stale_override_keys}")
    if any(not str(row["applicable_metric_ids"]) for row in census_rows):
        errors.append("one or more selected source rows has no applicable parser metric")
    if len({row["row_key"] for row in census_rows}) != len(census_rows):
        errors.append("source census contains duplicate row keys")
    selected_tickers = {
        str(row["ticker"]) for row in census_rows
    }
    identities_without_selected_sources = sorted(
        set(members) - selected_tickers
    )
    if identities_without_selected_sources:
        errors.append(
            "transportation identities without selected parser sources="
            f"{identities_without_selected_sources}"
        )

    summary: dict[str, object] = {
        "acceptance": "PASS" if not errors and not unresolved_gaps else "NO_GO",
        "model_family": MODEL_FAMILY,
        "manifest_version": manifest_version,
        "asof_date": asof_date,
        "start_date": start_date,
        "legacy_inactive_start_date": legacy_inactive_start_date,
        "parser_execution_authorized": False,
        "database_mode": "read_only",
        "network_requests": 0,
        "expected_identity_count": expected_identity_count,
        "identity_count": len(members),
        "active_identity_count": sum(member["universe_role"] == "active" for member in members.values()),
        "inactive_identity_count": sum(member["universe_role"] != "active" for member in members.values()),
        "selected_identity_count": len(selected_tickers),
        "identities_without_selected_sources": (
            identities_without_selected_sources
        ),
        "parser_metric_count": len(parser_metric_ids),
        "adapter_version": get_registry().adapter_version,
        "parser_execution_options": {
            "all_metrics": True,
            "no_network": True,
            "require_complete_cache": True,
            "accession_selection": "sealed_manifest_only",
            "document_selection": "sealed_manifest_only",
            "max_filings_per_ticker": 0,
            "max_documents_per_filing": 0,
            "enable_arelle": True,
            "enable_edgartools": True,
            "enable_pdf_ocr": False,
        },
        "expected_base_accession_count": expected_base_accession_count,
        "base_accession_count": len(base_keys),
        "legacy_inactive_selected_accession_count": sum(
            str(row["filing_date"]) < start_date
            for row in decisions
            if row["decision"] == "INCLUDE"
        ),
        "selected_accession_count": len(selected_keys),
        "selected_document_row_count": len(census_rows),
        "cached_document_row_count": sum(row["cache_status"] == "CACHED_HASHED" for row in census_rows),
        "missing_document_row_count": sum(row["cache_status"] != "CACHED_HASHED" for row in census_rows),
        "supplemental_accession_count": len(selected_keys - base_keys),
        "decision_counts": dict(sorted(Counter(str(row["decision"]) for row in decisions).items())),
        "selection_rule_counts": dict(
            sorted(Counter(str(row["selection_rule"]) for row in decisions if str(row["selection_rule"])).items())
        ),
        "gap_type_counts": dict(sorted(Counter(str(row["gap_type"]) for row in gaps).items())),
        "unresolved_gap_count": len(unresolved_gaps),
        "approved_gap_count": len(gaps) - len(unresolved_gaps),
        "duplicate_cik_accession_group_count": sum(count > 1 for count in cik_accession_counts.values()),
        "duplicate_content_group_count": sum(count > 1 for count in content_counts.values()),
        "unique_content_hash_count": len(content_counts),
        "dp0_scope_sha256": dp0_scope_sha256,
        "errors": errors,
        "next_gate": (
            "DP4_OFFLINE_PLAN_ONLY" if not errors and not unresolved_gaps else "HYDRATE_OR_ADJUDICATE_EXACT_DP3_GAPS"
        ),
    }
    return census_rows, decisions, gaps, summary


def write_source_census(
    *,
    census_rows: Sequence[Mapping[str, object]],
    decisions: Sequence[Mapping[str, object]],
    gaps: Sequence[Mapping[str, object]],
    summary: Mapping[str, object],
    census_path: Path,
    decisions_path: Path,
    gaps_path: Path,
    manifest_path: Path,
) -> dict[str, object]:
    write_csv_atomic(census_path, CENSUS_FIELDS, census_rows)
    write_csv_atomic(decisions_path, DECISION_FIELDS, decisions)
    write_csv_atomic(gaps_path, GAP_FIELDS, gaps)
    payload = dict(summary)
    payload["artifacts"] = {
        "source_census": {
            "path": str(census_path),
            "row_count": len(census_rows),
            "sha256": file_sha256(census_path),
            "canonical_rows_sha256": canonical_rows_hash(
                census_rows,
                fields=CENSUS_FIELDS,
            ),
        },
        "source_decisions": {
            "path": str(decisions_path),
            "row_count": len(decisions),
            "sha256": file_sha256(decisions_path),
            "canonical_rows_sha256": canonical_rows_hash(
                decisions,
                fields=DECISION_FIELDS,
            ),
        },
        "cache_gaps": {
            "path": str(gaps_path),
            "row_count": len(gaps),
            "sha256": file_sha256(gaps_path),
            "canonical_rows_sha256": canonical_rows_hash(
                gaps,
                fields=GAP_FIELDS,
            ),
        },
    }
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    return payload


def validate_written_source_census(
    *,
    census_path: Path,
    decisions_path: Path,
    gaps_path: Path,
    manifest_path: Path,
    verify_content_hashes: bool = False,
) -> list[str]:
    errors: list[str] = []
    if not manifest_path.is_file():
        return [f"missing source census manifest: {manifest_path}"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"invalid source census manifest JSON: {exc}"]
    artifacts = manifest.get("artifacts") or {}
    checks = (
        ("source_census", census_path, CENSUS_FIELDS),
        ("source_decisions", decisions_path, DECISION_FIELDS),
        ("cache_gaps", gaps_path, GAP_FIELDS),
    )
    loaded: dict[str, list[dict[str, str]]] = {}
    for key, path, fields in checks:
        rows = read_csv(path)
        loaded[key] = rows
        if not path.is_file():
            errors.append(f"missing artifact: {path}")
            continue
        if rows and tuple(rows[0]) != fields:
            errors.append(f"{path}: fields={tuple(rows[0])!r} expected={fields!r}")
        metadata = artifacts.get(key) or {}
        if int(metadata.get("row_count") or 0) != len(rows):
            errors.append(f"{path}: row count does not match manifest")
        if str(metadata.get("sha256") or "") != file_sha256(path):
            errors.append(f"{path}: file SHA-256 does not match manifest")
        if str(metadata.get("canonical_rows_sha256") or "") != canonical_rows_hash(
            rows,
            fields=fields,
        ):
            errors.append(f"{path}: canonical row hash does not match manifest")

    census_rows = loaded.get("source_census", [])
    if len({row.get("row_key", "") for row in census_rows}) != len(census_rows):
        errors.append("source census row keys are not unique")
    for row in census_rows:
        cached = row.get("cache_status") == "CACHED_HASHED"
        path = Path(row.get("local_path", ""))
        if cached and not path.is_file():
            errors.append(
                f"{row.get('ticker')}:{row.get('accession_number')}:"
                f"{row.get('document_name')}: cached document is missing"
            )
            continue
        if cached and int(row.get("file_size") or 0) != path.stat().st_size:
            errors.append(
                f"{row.get('ticker')}:{row.get('accession_number')}:"
                f"{row.get('document_name')}: cached document size changed"
            )
        if cached and verify_content_hashes:
            actual_hash = file_sha256(path)
            if actual_hash != row.get("content_sha256"):
                errors.append(
                    f"{row.get('ticker')}:{row.get('accession_number')}:"
                    f"{row.get('document_name')}: content SHA-256 changed"
                )
    if manifest.get("parser_execution_authorized") is not False:
        errors.append("source census must not authorize parser execution")
    if manifest.get("database_mode") != "read_only":
        errors.append("source census database mode must be read_only")
    if int(manifest.get("network_requests") or 0) != 0:
        errors.append("source census must record zero network requests")
    return errors

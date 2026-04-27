#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
import logging
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, normalize_string_list, resolve_optional_path, resolve_path
from biotech_index.core.text_norm import normalize_ticker


LOGGER = logging.getLogger("audit_hard_remove_candidates")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CONFIRM_TOKEN = "DELETE_INVALID_COMPANIES"

SAFE_REASON_CODES = {
    "wrong_entity",
    "duplicate_security",
    "not_tradable",
    "not_biotech_universe",
    "bad_reference",
    "symbol_reuse",
}
ACTIVE_IDENTITY_ERROR_CODES = {"wrong_entity", "duplicate_security", "bad_reference", "symbol_reuse"}

CANDIDATE_FIELDS = [
    "enabled",
    "ticker",
    "reason_code",
    "confirmed_invalid_identity",
    "approved_by",
    "approval_date",
    "delete_historical_records",
    "notes",
]

AUDIT_FIELDS = [
    "ticker",
    "candidate_enabled",
    "candidate_found",
    "company_found",
    "company_id",
    "company_name",
    "cik",
    "universe_status",
    "is_active",
    "listing_status",
    "reason_code",
    "confirmed_invalid_identity",
    "approved_by",
    "approval_date",
    "delete_historical_records",
    "safe_to_apply",
    "blockers",
    "db_tables_affected",
    "db_rows_affected",
    "csv_files_affected",
    "csv_rows_affected",
    "status",
    "notes",
]

TABLE_COUNT_FIELDS = [
    "ticker",
    "company_id",
    "table_name",
    "rows_deleted_on_db_purge",
]

CSV_REFERENCE_FIELDS = [
    "ticker",
    "file_path",
    "rows_with_ticker_reference",
    "ticker_columns",
]


@dataclass(frozen=True)
class Candidate:
    enabled: bool
    ticker: str
    reason_code: str
    confirmed_invalid_identity: bool
    approved_by: str
    approval_date: str
    delete_historical_records: bool
    notes: str
    candidate_found: bool = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run and optionally apply hard company removals. Use hard removal only for invalid "
            "company identities, duplicate securities, non-tradable bad references, or out-of-universe entities."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--candidates-csv", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--tickers", type=str, default="", help="Optional comma-separated ticker subset.")
    parser.add_argument("--apply-db", action="store_true", help="Apply DB purge for candidates that pass all safety checks.")
    parser.add_argument("--confirm", type=str, default="", help=f"Required token for --apply-db: {CONFIRM_TOKEN}")
    return parser.parse_args()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    for handler in logging.getLogger().handlers:
        if handler.formatter is not None:
            handler.formatter.converter = time.gmtime


def parse_boolish(raw: Any, *, default: bool = False) -> bool:
    text = str(raw or "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "t", "yes", "y"}


def row_get(row: dict[str, Any], *keys: str) -> str:
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for key in keys:
        raw = row.get(key)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
        raw = lowered.get(key.lower())
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return ""


def read_csv_flexible(path: Path) -> list[dict[str, str]]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames is None:
                    raise ValueError(f"CSV has no header: {path}")
                return [{str(k): str(v or "") for k, v in row.items()} for row in reader]
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    raise ValueError(f"Could not decode CSV {path}: {last_error}")


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def split_tickers(raw: str) -> set[str]:
    return {normalize_ticker(part) for part in str(raw or "").split(",") if normalize_ticker(part)}


def load_candidates(path: Path | None, *, ticker_filter: set[str]) -> list[Candidate]:
    rows = read_csv_flexible(path) if path is not None and path.exists() else []
    candidates: dict[str, Candidate] = {}
    for row in rows:
        ticker = normalize_ticker(row_get(row, "ticker", "Ticker", "symbol", "Symbol"))
        if not ticker:
            continue
        candidates[ticker] = Candidate(
            enabled=parse_boolish(row_get(row, "enabled"), default=True),
            ticker=ticker,
            reason_code=row_get(row, "reason_code", "reason").strip().lower(),
            confirmed_invalid_identity=parse_boolish(row_get(row, "confirmed_invalid_identity")),
            approved_by=row_get(row, "approved_by"),
            approval_date=row_get(row, "approval_date"),
            delete_historical_records=parse_boolish(row_get(row, "delete_historical_records")),
            notes=row_get(row, "notes"),
        )

    if ticker_filter:
        for ticker in sorted(ticker_filter):
            candidates.setdefault(
                ticker,
                Candidate(
                    enabled=True,
                    ticker=ticker,
                    reason_code="",
                    confirmed_invalid_identity=False,
                    approved_by="",
                    approval_date="",
                    delete_historical_records=False,
                    notes="Ad hoc ticker supplied via --tickers; add to candidates CSV before applying.",
                    candidate_found=False,
                ),
            )
        return [candidates[ticker] for ticker in sorted(ticker_filter) if ticker in candidates]

    return [candidates[ticker] for ticker in sorted(candidates)]


def quote_ident(identifier: str) -> str:
    return '"' + str(identifier).replace('"', '""') + '"'


def connect_readonly(db_path: Path, *, timeout_sec: float) -> sqlite3.Connection:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout_sec)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA query_only=ON")
    return conn


def load_company_rows(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        """
        SELECT company_id, ticker, cik, company_name, universe_status, is_active, listing_status
        FROM companies
        """
    ):
        out[normalize_ticker(row["ticker"])] = dict(row)
    return out


def table_names(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row["name"])
        for row in conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
    ]


def table_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    return [str(row["name"]) for row in conn.execute(f"PRAGMA table_info({quote_ident(table_name)})")]


def purge_candidate_on_connection(conn: sqlite3.Connection, *, ticker: str, company_id: int | None) -> None:
    normalized = normalize_ticker(ticker)
    for table in table_names(conn):
        columns = {col.lower(): col for col in table_columns(conn, table)}
        ticker_col = columns.get("ticker") or columns.get("symbol")
        if ticker_col:
            conn.execute(
                f"DELETE FROM {quote_ident(table)} WHERE UPPER({quote_ident(ticker_col)}) = ?",
                (normalized,),
            )
    if company_id is not None:
        conn.execute("DELETE FROM companies WHERE company_id = ?", (company_id,))


def count_candidate_rows_in_table(
    conn: sqlite3.Connection,
    *,
    table: str,
    columns: set[str],
    ticker: str,
    company_id: int | None,
) -> int:
    conditions: list[str] = []
    params: list[Any] = []
    if company_id is not None and "company_id" in columns:
        conditions.append(f"{quote_ident('company_id')} = ?")
        params.append(company_id)

    ticker_col = "ticker" if "ticker" in columns else "symbol" if "symbol" in columns else ""
    if ticker_col:
        conditions.append(f"{quote_ident(ticker_col)} = ? COLLATE NOCASE")
        params.append(ticker)

    if company_id is not None and "accession_nodash" in columns and "company_id" not in columns:
        conditions.append(
            f"{quote_ident('accession_nodash')} IN ("
            "SELECT accession_nodash FROM sec_filings WHERE company_id = ?"
            ")"
        )
        params.append(company_id)

    if not conditions:
        return 0
    where_clause = " OR ".join(f"({condition})" for condition in conditions)
    row = conn.execute(f"SELECT COUNT(*) FROM {quote_ident(table)} WHERE {where_clause}", params).fetchone()
    return int(row[0] if row else 0)


def simulate_db_purge(
    source_conn: sqlite3.Connection,
    *,
    ticker: str,
    company_id: int | None,
) -> dict[str, int]:
    normalized = normalize_ticker(ticker)
    out: dict[str, int] = {}
    for table in table_names(source_conn):
        columns = {col.lower() for col in table_columns(source_conn, table)}
        count = count_candidate_rows_in_table(
            source_conn,
            table=table,
            columns=columns,
            ticker=normalized,
            company_id=company_id,
        )
        if count:
            out[table] = count
    return out


def apply_db_purge(db_path: Path, *, ticker: str, company_id: int | None, timeout_sec: float) -> None:
    conn = sqlite3.connect(db_path, timeout=timeout_sec)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        with conn:
            purge_candidate_on_connection(conn, ticker=ticker, company_id=company_id)
    finally:
        conn.close()


def csv_paths_for_reference_scan(base_dir: Path, configured_dirs: list[str]) -> list[Path]:
    paths: list[Path] = []
    for raw_dir in configured_dirs:
        raw_dir = str(raw_dir or "").strip()
        if not raw_dir:
            continue
        path = resolve_path(raw_dir, base_dir=base_dir)
        if path.is_file() and path.suffix.lower() == ".csv":
            paths.append(path)
        elif path.is_dir():
            paths.extend(sorted(path.rglob("*.csv")))
    deduped: dict[str, Path] = {}
    for path in paths:
        deduped[str(path.resolve()).lower()] = path.resolve()
    return sorted(deduped.values(), key=lambda p: str(p).lower())


def row_references_ticker(row: dict[str, str], ticker: str) -> tuple[bool, list[str]]:
    ticker_columns: list[str] = []
    for key, value in row.items():
        key_norm = str(key or "").strip().lower()
        if key_norm not in {"ticker", "tickers", "symbol", "symbols"}:
            continue
        values = {normalize_ticker(part) for part in str(value or "").replace(";", ",").split(",") if normalize_ticker(part)}
        if ticker in values:
            ticker_columns.append(str(key))
    return bool(ticker_columns), ticker_columns


def scan_csv_references(paths: list[Path], candidates: list[Candidate]) -> list[dict[str, Any]]:
    tickers = {candidate.ticker for candidate in candidates}
    out: list[dict[str, Any]] = []
    for path in paths:
        try:
            rows = read_csv_flexible(path)
        except Exception as exc:
            LOGGER.warning("Skipping unreadable CSV reference scan path=%s error=%s", path, exc)
            continue
        counts: dict[str, int] = {ticker: 0 for ticker in tickers}
        columns: dict[str, set[str]] = {ticker: set() for ticker in tickers}
        for row in rows:
            for ticker in tickers:
                found, cols = row_references_ticker(row, ticker)
                if found:
                    counts[ticker] += 1
                    columns[ticker].update(cols)
        for ticker, count in counts.items():
            if count:
                out.append(
                    {
                        "ticker": ticker,
                        "file_path": str(path),
                        "rows_with_ticker_reference": count,
                        "ticker_columns": ";".join(sorted(columns[ticker])),
                    }
                )
    out.sort(key=lambda row: (str(row["ticker"]), str(row["file_path"]).lower()))
    return out


def validate_candidate(
    candidate: Candidate,
    *,
    company_row: dict[str, Any] | None,
    safe_reason_codes: set[str],
    active_identity_error_codes: set[str],
    apply_requested: bool,
) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    reason_code = candidate.reason_code.strip().lower()
    if not candidate.candidate_found:
        blockers.append("missing_from_candidates_csv")
    if not candidate.enabled:
        blockers.append("candidate_disabled")
    if not reason_code:
        blockers.append("missing_reason_code")
    elif reason_code not in safe_reason_codes:
        blockers.append("unsafe_reason_code")
    if not candidate.confirmed_invalid_identity:
        blockers.append("not_confirmed_invalid_identity")
    if company_row is None:
        blockers.append("company_not_found_in_db")
    else:
        is_active = int(company_row.get("is_active") or 0) == 1
        if is_active and reason_code not in active_identity_error_codes:
            blockers.append("active_company_requires_identity_error_reason")
    if apply_requested:
        if not candidate.approved_by:
            blockers.append("missing_approved_by_for_apply")
        if not candidate.approval_date:
            blockers.append("missing_approval_date_for_apply")
        if not candidate.delete_historical_records:
            blockers.append("delete_historical_records_not_confirmed")
    return not blockers, blockers


def build_audit(
    candidates: list[Candidate],
    *,
    company_rows: dict[str, dict[str, Any]],
    source_conn: sqlite3.Connection,
    csv_reference_rows: list[dict[str, Any]],
    safe_reason_codes: set[str],
    active_identity_error_codes: set[str],
    apply_requested: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    csv_counts = Counter()
    csv_files = Counter()
    for row in csv_reference_rows:
        ticker = str(row["ticker"])
        csv_counts[ticker] += int(row["rows_with_ticker_reference"])
        csv_files[ticker] += 1

    audit_rows: list[dict[str, Any]] = []
    table_count_rows: list[dict[str, Any]] = []
    for candidate in candidates:
        company_row = company_rows.get(candidate.ticker)
        company_id = int(company_row["company_id"]) if company_row is not None else None
        db_counts = simulate_db_purge(source_conn, ticker=candidate.ticker, company_id=company_id)
        db_rows_affected = sum(db_counts.values())
        for table_name, deleted_count in sorted(db_counts.items()):
            table_count_rows.append(
                {
                    "ticker": candidate.ticker,
                    "company_id": company_id if company_id is not None else "",
                    "table_name": table_name,
                    "rows_deleted_on_db_purge": deleted_count,
                }
            )

        safe_to_apply, blockers = validate_candidate(
            candidate,
            company_row=company_row,
            safe_reason_codes=safe_reason_codes,
            active_identity_error_codes=active_identity_error_codes,
            apply_requested=apply_requested,
        )
        if safe_to_apply:
            status = "apply_ready" if apply_requested else "dry_run_ready"
        elif not candidate.enabled:
            status = "disabled"
        else:
            status = "blocked"

        audit_rows.append(
            {
                "ticker": candidate.ticker,
                "candidate_enabled": int(candidate.enabled),
                "candidate_found": int(candidate.candidate_found),
                "company_found": int(company_row is not None),
                "company_id": company_id if company_id is not None else "",
                "company_name": company_row.get("company_name", "") if company_row else "",
                "cik": company_row.get("cik", "") if company_row else "",
                "universe_status": company_row.get("universe_status", "") if company_row else "",
                "is_active": company_row.get("is_active", "") if company_row else "",
                "listing_status": company_row.get("listing_status", "") if company_row else "",
                "reason_code": candidate.reason_code,
                "confirmed_invalid_identity": int(candidate.confirmed_invalid_identity),
                "approved_by": candidate.approved_by,
                "approval_date": candidate.approval_date,
                "delete_historical_records": int(candidate.delete_historical_records),
                "safe_to_apply": int(safe_to_apply),
                "blockers": ";".join(blockers),
                "db_tables_affected": len(db_counts),
                "db_rows_affected": db_rows_affected,
                "csv_files_affected": csv_files[candidate.ticker],
                "csv_rows_affected": csv_counts[candidate.ticker],
                "status": status,
                "notes": candidate.notes,
            }
        )
    return audit_rows, table_count_rows


def main() -> None:
    configure_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent

    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    candidates_path = (
        args.candidates_csv.expanduser().resolve()
        if args.candidates_csv
        else resolve_optional_path(cfg_get(config, "hard_remove.candidates_csv"), base_dir=base_dir)
    )
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(cfg_get(config, "hard_remove.output_dir", "../output/biotech_index_reports"), base_dir=base_dir)
    )
    ticker_filter = split_tickers(args.tickers)
    safe_reason_codes = {
        value.strip().lower()
        for value in normalize_string_list(cfg_get(config, "hard_remove.safe_reason_codes"), sorted(SAFE_REASON_CODES))
        if value.strip()
    }
    active_identity_error_codes = {
        value.strip().lower()
        for value in normalize_string_list(
            cfg_get(config, "hard_remove.active_identity_error_reason_codes"),
            sorted(ACTIVE_IDENTITY_ERROR_CODES),
        )
        if value.strip()
    }
    csv_reference_dirs = normalize_string_list(
        cfg_get(config, "hard_remove.csv_reference_dirs"),
        ["data", "../ticker_mapping"],
    )
    audit_csv = output_dir / str(cfg_get(config, "hard_remove.audit_csv", "company_hard_remove_audit.csv"))
    table_counts_csv = output_dir / str(
        cfg_get(config, "hard_remove.table_counts_csv", "company_hard_remove_table_counts.csv")
    )
    csv_references_csv = output_dir / str(
        cfg_get(config, "hard_remove.csv_references_csv", "company_hard_remove_csv_references.csv")
    )
    manifest_json = output_dir / str(cfg_get(config, "hard_remove.manifest_json", "company_hard_remove_manifest.json"))

    if args.apply_db and args.confirm != CONFIRM_TOKEN:
        raise SystemExit(f"--apply-db requires --confirm {CONFIRM_TOKEN}")

    candidates = load_candidates(candidates_path, ticker_filter=ticker_filter)
    csv_reference_paths = csv_paths_for_reference_scan(base_dir, csv_reference_dirs)
    csv_reference_rows = scan_csv_references(csv_reference_paths, candidates)

    timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))
    with connect_readonly(db_path, timeout_sec=timeout_sec) as source_conn:
        company_rows = load_company_rows(source_conn)
        audit_rows, table_count_rows = build_audit(
            candidates,
            company_rows=company_rows,
            source_conn=source_conn,
            csv_reference_rows=csv_reference_rows,
            safe_reason_codes=safe_reason_codes,
            active_identity_error_codes=active_identity_error_codes,
            apply_requested=bool(args.apply_db),
        )

    applied_tickers: list[str] = []
    if args.apply_db:
        for row in audit_rows:
            if int(row.get("safe_to_apply") or 0) != 1:
                continue
            apply_db_purge(
                db_path,
                ticker=str(row["ticker"]),
                company_id=int(row["company_id"]) if str(row.get("company_id") or "").strip() else None,
                timeout_sec=timeout_sec,
            )
            applied_tickers.append(str(row["ticker"]))

    write_csv(audit_csv, audit_rows, AUDIT_FIELDS)
    write_csv(table_counts_csv, table_count_rows, TABLE_COUNT_FIELDS)
    write_csv(csv_references_csv, csv_reference_rows, CSV_REFERENCE_FIELDS)
    manifest = {
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "database_path": str(db_path),
        "candidates_csv": str(candidates_path) if candidates_path else "",
        "candidate_count": len(candidates),
        "ticker_filter": sorted(ticker_filter),
        "apply_db": bool(args.apply_db),
        "applied_tickers": applied_tickers,
        "audit_csv": str(audit_csv),
        "table_counts_csv": str(table_counts_csv),
        "csv_references_csv": str(csv_references_csv),
        "safe_to_apply_count": sum(1 for row in audit_rows if int(row.get("safe_to_apply") or 0) == 1),
        "blocked_count": sum(1 for row in audit_rows if str(row.get("status")) == "blocked"),
        "disabled_count": sum(1 for row in audit_rows if str(row.get("status")) == "disabled"),
        "db_rows_affected_total": sum(int(row.get("db_rows_affected") or 0) for row in audit_rows),
        "csv_rows_affected_total": sum(int(row.get("csv_rows_affected") or 0) for row in audit_rows),
        "status_counts": dict(Counter(str(row.get("status")) for row in audit_rows)),
        "safe_reason_codes": sorted(safe_reason_codes),
        "active_identity_error_reason_codes": sorted(active_identity_error_codes),
        "csv_reference_dirs": csv_reference_dirs,
        "csv_reference_file_count": len(csv_reference_paths),
    }
    manifest_json.parent.mkdir(parents=True, exist_ok=True)
    manifest_json.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    LOGGER.info(
        "Hard-remove audit complete: candidates=%d safe=%d blocked=%d applied=%d audit_csv=%s",
        len(candidates),
        manifest["safe_to_apply_count"],
        manifest["blocked_count"],
        len(applied_tickers),
        audit_csv,
    )


if __name__ == "__main__":
    main()

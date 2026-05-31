#!/usr/bin/env python3
# ruff: noqa: E402
from __future__ import annotations

import argparse
import csv
import logging
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.clients.ctgov_client import parse_sponsors, parse_study
from biotech_index.core.config import cfg_get, load_yaml, resolve_path
from biotech_index.core.db import connect, init_db, utc_now
from biotech_index.core.http_cache import CachedHttpClient, HostThrottle
from biotech_index.core.logging_utils import configure_utc_logging
from biotech_index.core.text_norm import alias_token_sets, names_match, normalize_ticker


LOGGER = logging.getLogger("reconcile_ctgov_nct_seeds")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_SEED = PACKAGE_ROOT / "data" / "ctgov_nct_reconciliation_seed.csv"

ACTIVE_STATUSES = {"RECRUITING", "ACTIVE_NOT_RECRUITING", "ENROLLING_BY_INVITATION", "NOT_YET_RECRUITING"}
NCT_RE = re.compile(r"^NCT\d{8}$", re.IGNORECASE)
SEARCH_OVERRIDE_FIELDS = ["ticker", "search_term", "query_field", "source", "confidence", "link_from_search", "notes", "enabled"]
PROGRAM_OVERRIDE_FIELDS = ["enabled", "ticker", "nct_id", "confidence", "source_name", "notes"]
AUDIT_FIELDS = [
    "ticker",
    "nct_id",
    "expected_company",
    "candidate",
    "expected_relation",
    "company_found",
    "company_status",
    "ctgov_found",
    "db_trial_before",
    "db_link_before",
    "local_linked_tickers",
    "overall_status",
    "active_like",
    "study_type",
    "interventional",
    "phase_text",
    "brief_title",
    "lead_sponsor",
    "collaborators",
    "last_update_post_date",
    "sponsor_relation",
    "existing_link_roles",
    "validation_bucket",
    "recommendation",
    "applied_db_update",
    "search_override_added",
    "program_override_added",
    "notes",
]
OVERRIDE_VALIDATION_FIELDS = [
    "source_file",
    "override_type",
    "row_number",
    "enabled",
    "ticker",
    "nct_id",
    "company_found",
    "company_active",
    "nct_found",
    "linked_to_ticker",
    "local_linked_tickers",
    "status",
    "notes",
]
OVERRIDE_STRICT_FAILURE_STATUSES = {"invalid_nct_format", "missing_company", "missing_nct", "missing_required_columns"}


@dataclass(frozen=True)
class SeedRow:
    ticker: str
    nct_id: str
    expected_company: str
    candidate: str
    expected_relation: str
    notes: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reconcile user-supplied ticker/NCT pairs against CTGov and the staging DB.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--seed-csv", type=Path, default=DEFAULT_SEED)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="Snapshot date for applied CTGov rows. Defaults to UTC today.")
    parser.add_argument("--apply-db", action="store_true", help="Upsert fetched CTGov study/sponsor/snapshot rows into the DB.")
    parser.add_argument(
        "--apply-overrides",
        action="store_true",
        help="Append safe exact-NCT refresh seeds and explicit program owner overrides when recommended.",
    )
    parser.add_argument(
        "--validate-overrides",
        action="store_true",
        help="Validate configured NCT-bearing override CSVs against the local CTGov DB.",
    )
    parser.add_argument(
        "--validate-overrides-only",
        action="store_true",
        help="Only validate override CSVs; do not read/fetch seed NCTs.",
    )
    parser.add_argument("--override-validation-csv", type=Path, default=None)
    parser.add_argument(
        "--strict-overrides",
        action="store_true",
        help="Exit non-zero when override validation finds missing company or missing NCT references.",
    )
    return parser.parse_args()


def as_bool(raw: object, *, default: bool = False) -> bool:
    text = str(raw or "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "y", "t"}


def read_seed(path: Path) -> list[SeedRow]:
    rows: list[SeedRow] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"NCT reconciliation seed CSV has no header: {path}")
        for line_no, row in enumerate(reader, start=2):
            if not as_bool(row.get("enabled", "true"), default=True):
                continue
            ticker = normalize_ticker(row.get("ticker"))
            nct_id = str(row.get("nct_id") or "").strip().upper()
            if not ticker or not nct_id:
                LOGGER.warning("Skipping seed row with blank ticker/NCT at %s:%d", path, line_no)
                continue
            rows.append(
                SeedRow(
                    ticker=ticker,
                    nct_id=nct_id,
                    expected_company=str(row.get("expected_company") or "").strip(),
                    candidate=str(row.get("candidate") or "").strip(),
                    expected_relation=str(row.get("expected_relation") or "").strip().lower(),
                    notes=str(row.get("notes") or "").strip(),
                )
            )
    return rows


def fetch_study_by_nct(
    http: CachedHttpClient,
    *,
    studies_url: str,
    nct_id: str,
    ttl_hours: float,
) -> tuple[dict[str, Any] | None, str]:
    url = f"{studies_url.rstrip('/')}/{nct_id}"
    try:
        payload = http.fetch_json(
            namespace="ctgov_v2_nct",
            url=url,
            headers={"Accept": "application/json"},
            ttl_hours=ttl_hours,
        )
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else 0
        if status_code == 404:
            return None, "not_found"
        return None, f"http_{status_code}"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return payload if isinstance(payload, dict) else None, ""


def company_context(conn: sqlite3.Connection, ticker: str) -> tuple[dict[str, Any] | None, set[str], list[set[str]]]:
    company = conn.execute(
        """
        SELECT company_id, ticker, company_name, universe_status, is_active
        FROM companies
        WHERE ticker = ?
        """,
        (ticker,),
    ).fetchone()
    if company is None:
        return None, set(), []
    aliases = {str(company["ticker"] or ""), str(company["company_name"] or "")}
    alias_rows = conn.execute(
        """
        SELECT alias_raw, alias_norm
        FROM company_aliases
        WHERE company_id = ?
        """,
        (int(company["company_id"]),),
    ).fetchall()
    for row in alias_rows:
        aliases.add(str(row["alias_raw"] or ""))
        aliases.add(str(row["alias_norm"] or ""))
    norm_aliases = {alias for alias in aliases if alias.strip()}
    return dict(company), norm_aliases, alias_token_sets(norm_aliases)


def existing_links(conn: sqlite3.Connection, *, company_id: int, nct_id: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT match_role, match_method, confidence
        FROM trial_company_links
        WHERE company_id = ? AND nct_id = ?
        ORDER BY match_role, confidence DESC
        """,
        (company_id, nct_id),
    ).fetchall()
    return [f"{row['match_role']}:{row['match_method']}:{float(row['confidence'] or 0.0):.2f}" for row in rows]


def local_linked_tickers(conn: sqlite3.Connection, nct_id: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT c.ticker, l.match_role, l.match_method, l.confidence
        FROM trial_company_links l
        JOIN companies c ON c.company_id = l.company_id
        WHERE l.nct_id = ?
        ORDER BY c.ticker, l.match_role, l.confidence DESC
        """,
        (nct_id,),
    ).fetchall()
    return [
        f"{row['ticker']}:{row['match_role']}:{row['match_method']}:{float(row['confidence'] or 0.0):.2f}"
        for row in rows
    ]


def sponsor_relation(study: dict[str, Any], norm_aliases: set[str], tokens: list[set[str]]) -> tuple[str, str]:
    roles: list[str] = []
    sponsors: list[str] = []
    for sponsor in parse_sponsors(study):
        sponsors.append(f"{sponsor.sponsor_name} [{sponsor.sponsor_role}]")
        if names_match(sponsor.sponsor_name, norm_aliases, tokens):
            roles.append(sponsor.sponsor_role)
    role_order = {"lead": 0, "collaborator": 1}
    roles = sorted(set(roles), key=lambda role: role_order.get(role, 9))
    return ";".join(roles), "; ".join(sponsors)


def upsert_study(conn: sqlite3.Connection, study: dict[str, Any], *, asof_date: str) -> None:
    parsed = parse_study(study)
    if parsed is None:
        raise ValueError("Cannot upsert malformed CTGov study without nct_id")
    now = utc_now()
    with conn:
        conn.execute(
            """
            INSERT INTO trials(
                nct_id, brief_title, study_type, phase_text, overall_status,
                lead_sponsor, last_update_post_date, has_results, raw_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(nct_id) DO UPDATE SET
                brief_title = excluded.brief_title,
                study_type = excluded.study_type,
                phase_text = excluded.phase_text,
                overall_status = excluded.overall_status,
                lead_sponsor = excluded.lead_sponsor,
                last_update_post_date = excluded.last_update_post_date,
                has_results = excluded.has_results,
                raw_json = excluded.raw_json,
                updated_at = excluded.updated_at
            """,
            (
                parsed.nct_id,
                parsed.brief_title,
                parsed.study_type,
                parsed.phase_text,
                parsed.overall_status,
                parsed.lead_sponsor,
                parsed.last_update_post_date,
                1 if parsed.has_results else 0,
                parsed.raw_json,
                now,
                now,
            ),
        )
        sponsors = parse_sponsors(study)
        if sponsors:
            conn.execute("DELETE FROM trial_sponsors WHERE nct_id = ?", (parsed.nct_id,))
            conn.executemany(
                """
                INSERT INTO trial_sponsors(
                    nct_id, sponsor_name, sponsor_name_norm, sponsor_role, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        sponsor.nct_id,
                        sponsor.sponsor_name,
                        sponsor.sponsor_name_norm,
                        sponsor.sponsor_role,
                        now,
                        now,
                    )
                    for sponsor in sponsors
                ],
            )
        conn.execute(
            """
            INSERT INTO trial_snapshot_daily(
                asof_date, nct_id, overall_status, phase_text, has_results,
                primary_completion_date, enrollment_count, raw_hash, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asof_date, nct_id) DO UPDATE SET
                overall_status = excluded.overall_status,
                phase_text = excluded.phase_text,
                has_results = excluded.has_results,
                primary_completion_date = excluded.primary_completion_date,
                enrollment_count = excluded.enrollment_count,
                raw_hash = excluded.raw_hash
            """,
            (
                asof_date,
                parsed.nct_id,
                parsed.overall_status,
                parsed.phase_text,
                1 if parsed.has_results else 0,
                parsed.primary_completion_date,
                parsed.enrollment_count,
                parsed.raw_hash,
                now,
            ),
        )


def read_existing_override_keys(path: Path, key_fields: tuple[str, ...]) -> set[tuple[str, ...]]:
    if not path.exists():
        return set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return {
            tuple(str(row.get(field) or "").strip().upper() for field in key_fields)
            for row in reader
        }


def append_rows(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        if not exists:
            writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def write_audit(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=AUDIT_FIELDS, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def recommendation_for(
    *,
    company: dict[str, Any] | None,
    found: bool,
    active_like: bool,
    interventional: bool,
    relation: str,
    expected_relation: str,
) -> str:
    if company is None:
        return "reject_missing_company"
    if not found:
        return "missing_from_ctgov"
    if not active_like:
        return "reject_inactive_status"
    if not interventional:
        return "reject_non_interventional"
    if relation:
        return "accept_sponsor_link"
    if expected_relation == "program":
        return "accept_program_override"
    return "reject_wrong_company_or_needs_review"


def validation_bucket_for(
    *,
    company: dict[str, Any] | None,
    found: bool,
    active_like: bool,
    interventional: bool,
    relation: str,
    expected_relation: str,
    linked_tickers: list[str],
    seed_ticker: str,
) -> str:
    if company is None:
        return "missing_company"
    if not found:
        return "nct_not_found_ctgov"
    if linked_tickers and not any(item.split(":", 1)[0].upper() == seed_ticker.upper() for item in linked_tickers):
        return "different_ticker_local_link"
    if not active_like:
        return "inactive_or_terminal"
    if not interventional:
        return "non_interventional"
    if relation:
        return "valid_ticker_match"
    if expected_relation == "program":
        return "program_owner_candidate"
    return "sponsor_mismatch_needs_review"


def read_csv_records(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        return [], []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), [{str(k): str(v or "") for k, v in row.items()} for row in reader]


def validate_override_rows(
    conn: sqlite3.Connection,
    *,
    path: Path,
    override_type: str,
    nct_field: str,
    ticker_field: str = "ticker",
) -> list[dict[str, Any]]:
    fieldnames, rows = read_csv_records(path)
    if not rows:
        return []
    if ticker_field not in fieldnames or nct_field not in fieldnames:
        return [
            {
                "source_file": str(path),
                "override_type": override_type,
                "row_number": "",
                "enabled": "",
                "ticker": "",
                "nct_id": "",
                "company_found": False,
                "company_active": False,
                "nct_found": False,
                "linked_to_ticker": False,
                "local_linked_tickers": "",
                "status": "missing_required_columns",
                "notes": f"required_columns={ticker_field},{nct_field}",
            }
        ]

    out: list[dict[str, Any]] = []
    for row_number, row in enumerate(rows, start=2):
        enabled = as_bool(row.get("enabled", "true"), default=True)
        if not enabled:
            continue
        ticker = normalize_ticker(row.get(ticker_field))
        raw_nct = str(row.get(nct_field) or "").strip().upper()
        if not ticker or not raw_nct:
            continue
        if not NCT_RE.match(raw_nct):
            if override_type == "search_override":
                continue
            out.append(
                {
                    "source_file": str(path),
                    "override_type": override_type,
                    "row_number": row_number,
                    "enabled": enabled,
                    "ticker": ticker,
                    "nct_id": raw_nct,
                    "company_found": False,
                    "company_active": False,
                    "nct_found": False,
                    "linked_to_ticker": False,
                    "local_linked_tickers": "",
                    "status": "invalid_nct_format",
                    "notes": "",
                }
            )
            continue

        company = conn.execute(
            "SELECT company_id, is_active FROM companies WHERE ticker = ?",
            (ticker,),
        ).fetchone()
        nct_found = conn.execute("SELECT 1 FROM trials WHERE nct_id = ?", (raw_nct,)).fetchone() is not None
        linked = local_linked_tickers(conn, raw_nct) if nct_found else []
        linked_to_ticker = any(item.split(":", 1)[0].upper() == ticker.upper() for item in linked)
        if company is None:
            status = "missing_company"
        elif not nct_found:
            status = "missing_nct"
        elif not bool(company["is_active"]):
            status = "inactive_company_reference"
        elif not linked_to_ticker and override_type != "program_owner_override":
            status = "nct_not_linked_to_ticker"
        elif not linked_to_ticker and override_type == "program_owner_override":
            status = "program_override_pending_link"
        else:
            status = "ok"
        out.append(
            {
                "source_file": str(path),
                "override_type": override_type,
                "row_number": row_number,
                "enabled": enabled,
                "ticker": ticker,
                "nct_id": raw_nct,
                "company_found": company is not None,
                "company_active": bool(company["is_active"]) if company else False,
                "nct_found": nct_found,
                "linked_to_ticker": linked_to_ticker,
                "local_linked_tickers": "|".join(linked),
                "status": status,
                "notes": "",
            }
        )
    return out


def write_override_validation(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OVERRIDE_VALIDATION_FIELDS, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    seed_csv = args.seed_csv.expanduser().resolve()
    asof_date = args.asof or datetime.now(timezone.utc).date().isoformat()
    source_tag = f"manual_nct_reconcile_{asof_date.replace('-', '')}"
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path("../output/biotech_index_reports/ctgov_nct_reconciliation_audit.csv", base_dir=base_dir)
    )
    override_validation_csv = (
        args.override_validation_csv.expanduser().resolve()
        if args.override_validation_csv
        else resolve_path("../output/biotech_index_reports/ctgov_nct_override_validation.csv", base_dir=base_dir)
    )
    studies_url = str(cfg_get(config, "ctgov.studies_url", "https://clinicaltrials.gov/api/v2/studies"))
    cache_dir = resolve_path(cfg_get(config, "ctgov.cache_dir", "../output/biotech_index_cache"), base_dir=base_dir)
    ttl_hours = float(cfg_get(config, "ctgov.json_ttl_hours", 168.0))
    search_overrides_path = resolve_path(cfg_get(config, "ctgov.search_overrides_csv", "data/ctgov_search_overrides.csv"), base_dir=base_dir)
    program_overrides_path = resolve_path(
        cfg_get(config, "trial_linking.program_owner_overrides_csv", "data/ctgov_program_owner_overrides.csv"),
        base_dir=base_dir,
    )
    trial_status_overrides_path = resolve_path(
        cfg_get(config, "ctgov_audit.trial_status_overrides_csv", "data/ctgov_trial_status_overrides.csv"),
        base_dir=base_dir,
    )

    seeds = [] if args.validate_overrides_only else read_seed(seed_csv)
    if not seeds and not args.validate_overrides_only:
        raise ValueError(f"No enabled NCT seed rows found: {seed_csv}")
    audit_rows: list[dict[str, Any]] = []
    override_validation_rows: list[dict[str, Any]] = []
    search_rows_to_add: list[dict[str, Any]] = []
    program_rows_to_add: list[dict[str, Any]] = []
    existing_search_keys = read_existing_override_keys(search_overrides_path, ("ticker", "search_term", "query_field"))
    existing_program_keys = read_existing_override_keys(program_overrides_path, ("ticker", "nct_id"))

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        if not args.validate_overrides_only:
            with CachedHttpClient(
                cache_dir=cache_dir,
                sleep_sec=float(cfg_get(config, "ctgov.sleep_sec", 0.2)),
                timeout_sec=float(cfg_get(config, "ctgov.timeout_sec", 45.0)),
                max_retries=int(cfg_get(config, "ctgov.max_retries", 3)),
                throttle=HostThrottle(),
            ) as http:
                for seed in seeds:
                    company, aliases, tokens = company_context(conn, seed.ticker)
                    db_trial_before = conn.execute("SELECT 1 FROM trials WHERE nct_id = ?", (seed.nct_id,)).fetchone() is not None
                    links_before = existing_links(conn, company_id=int(company["company_id"]), nct_id=seed.nct_id) if company else []
                    linked_tickers = local_linked_tickers(conn, seed.nct_id)
                    study, fetch_error = fetch_study_by_nct(http, studies_url=studies_url, nct_id=seed.nct_id, ttl_hours=ttl_hours)
                    parsed = parse_study(study) if study else None
                    relation, sponsors_text = sponsor_relation(study, aliases, tokens) if study and company else ("", "")
                    collaborators = "; ".join(
                        sponsor.sponsor_name
                        for sponsor in parse_sponsors(study or {})
                        if sponsor.sponsor_role == "collaborator"
                    )
                    overall_status = parsed.overall_status if parsed else ""
                    study_type = parsed.study_type if parsed else ""
                    active_like = overall_status.upper() in ACTIVE_STATUSES
                    interventional = study_type.upper() == "INTERVENTIONAL"
                    recommendation = recommendation_for(
                        company=company,
                        found=bool(parsed),
                        active_like=active_like,
                        interventional=interventional,
                        relation=relation,
                        expected_relation=seed.expected_relation,
                    )
                    validation_bucket = validation_bucket_for(
                        company=company,
                        found=bool(parsed),
                        active_like=active_like,
                        interventional=interventional,
                        relation=relation,
                        expected_relation=seed.expected_relation,
                        linked_tickers=linked_tickers,
                        seed_ticker=seed.ticker,
                    )
                    applied_db_update = False
                    if args.apply_db and study and parsed:
                        upsert_study(conn, study, asof_date=asof_date)
                        applied_db_update = True

                    search_added = False
                    program_added = False
                    if args.apply_overrides and recommendation == "accept_sponsor_link":
                        key = (seed.ticker.upper(), seed.nct_id.upper(), "QUERY.TERM")
                        if key not in existing_search_keys:
                            search_rows_to_add.append(
                                {
                                    "ticker": seed.ticker,
                                    "search_term": seed.nct_id,
                                    "query_field": "query.term",
                                    "source": source_tag,
                                    "confidence": "0.95",
                                    "link_from_search": "false",
                                    "notes": f"Exact NCT refresh seed from NCT-first reconciliation; relation={relation or 'sponsor'}",
                                    "enabled": "true",
                                }
                            )
                            existing_search_keys.add(key)
                            search_added = True
                    elif args.apply_overrides and recommendation == "needs_program_owner_review" and seed.expected_relation == "program":
                        key = (seed.ticker.upper(), seed.nct_id.upper(), "QUERY.TERM")
                        if key not in existing_search_keys:
                            search_rows_to_add.append(
                                {
                                    "ticker": seed.ticker,
                                    "search_term": seed.nct_id,
                                    "query_field": "query.term",
                                    "source": f"{source_tag}_review",
                                    "confidence": "0.95",
                                    "link_from_search": "false",
                                    "notes": "Exact NCT refresh seed pending program-owner review; does not create program link.",
                                    "enabled": "true",
                                }
                            )
                            existing_search_keys.add(key)
                            search_added = True
                    if args.apply_overrides and recommendation == "accept_program_override":
                        key = (seed.ticker.upper(), seed.nct_id.upper())
                        if key not in existing_program_keys:
                            program_rows_to_add.append(
                                {
                                    "enabled": "true",
                                    "ticker": seed.ticker,
                                    "nct_id": seed.nct_id,
                                    "confidence": "0.95",
                                    "source_name": source_tag,
                                    "notes": seed.notes,
                                }
                            )
                            existing_program_keys.add(key)
                            program_added = True

                    audit_rows.append(
                        {
                            "ticker": seed.ticker,
                            "nct_id": seed.nct_id,
                            "expected_company": seed.expected_company,
                            "candidate": seed.candidate,
                            "expected_relation": seed.expected_relation,
                            "company_found": bool(company),
                            "company_status": "" if not company else f"{company['universe_status']}/active={company['is_active']}",
                            "ctgov_found": bool(parsed),
                            "db_trial_before": db_trial_before,
                            "db_link_before": bool(links_before),
                            "local_linked_tickers": "|".join(linked_tickers),
                            "overall_status": overall_status,
                            "active_like": active_like,
                            "study_type": study_type,
                            "interventional": interventional,
                            "phase_text": parsed.phase_text if parsed else "",
                            "brief_title": parsed.brief_title if parsed else "",
                            "lead_sponsor": parsed.lead_sponsor if parsed else "",
                            "collaborators": collaborators,
                            "last_update_post_date": parsed.last_update_post_date if parsed else "",
                            "sponsor_relation": relation,
                            "existing_link_roles": "|".join(links_before),
                            "validation_bucket": validation_bucket,
                            "recommendation": recommendation,
                            "applied_db_update": applied_db_update,
                            "search_override_added": search_added,
                            "program_override_added": program_added,
                            "notes": fetch_error or " | ".join(part for part in [seed.notes, sponsors_text] if part),
                        }
                    )
        if args.validate_overrides or args.validate_overrides_only:
            override_validation_rows.extend(
                validate_override_rows(
                    conn,
                    path=search_overrides_path,
                    override_type="search_override",
                    nct_field="search_term",
                )
            )
            override_validation_rows.extend(
                validate_override_rows(
                    conn,
                    path=program_overrides_path,
                    override_type="program_owner_override",
                    nct_field="nct_id",
                )
            )
            override_validation_rows.extend(
                validate_override_rows(
                    conn,
                    path=trial_status_overrides_path,
                    override_type="trial_status_override",
                    nct_field="nct_id",
                )
            )

    if args.apply_overrides:
        append_rows(search_overrides_path, SEARCH_OVERRIDE_FIELDS, search_rows_to_add)
        append_rows(program_overrides_path, PROGRAM_OVERRIDE_FIELDS, program_rows_to_add)
    if audit_rows or not args.validate_overrides_only:
        write_audit(output_csv, audit_rows)
    if args.validate_overrides or args.validate_overrides_only:
        write_override_validation(override_validation_csv, override_validation_rows)
        failures = [
            row for row in override_validation_rows if str(row.get("status") or "") in OVERRIDE_STRICT_FAILURE_STATUSES
        ]
        if failures:
            LOGGER.warning(
                "NCT override validation found %d strict failure(s); report=%s",
                len(failures),
                override_validation_csv,
            )
        if failures and args.strict_overrides:
            raise RuntimeError(
                f"NCT override validation failed strict mode: failures={len(failures)} report={override_validation_csv}"
            )
    counts: dict[str, int] = {}
    for row in audit_rows:
        counts[str(row["recommendation"])] = counts.get(str(row["recommendation"]), 0) + 1
    override_counts: dict[str, int] = {}
    for row in override_validation_rows:
        override_counts[str(row["status"])] = override_counts.get(str(row["status"]), 0) + 1
    LOGGER.info(
        "NCT reconciliation complete: seeds=%d audit=%s apply_db=%s search_overrides_added=%d program_overrides_added=%d recommendations=%s override_validation=%s override_statuses=%s",
        len(seeds),
        output_csv,
        args.apply_db,
        len(search_rows_to_add),
        len(program_rows_to_add),
        counts,
        override_validation_csv if (args.validate_overrides or args.validate_overrides_only) else "",
        override_counts,
    )


if __name__ == "__main__":
    try:
        main()
    except (SystemExit, KeyboardInterrupt, GeneratorExit):
        raise
    except Exception as exc:
        LOGGER.exception("Fatal CTGov NCT reconciliation error: %s", exc)
        raise SystemExit(1) from exc

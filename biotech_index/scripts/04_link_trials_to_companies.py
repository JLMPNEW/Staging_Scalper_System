#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, normalize_string_list, resolve_optional_path, resolve_path
from biotech_index.core.db import connect, finish_run, init_db, start_run, utc_now
from biotech_index.core.text_norm import alias_token_sets, meaningful_org_tokens, normalize_org_name, strip_corporate_suffixes


LOGGER = logging.getLogger("link_trials_to_companies")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


@dataclass(frozen=True)
class CompanyAliases:
    company_id: int
    ticker: str
    company_name: str
    alias_norms: frozenset[str]
    alias_tokens: tuple[frozenset[str], ...]


@dataclass(frozen=True)
class SponsorRow:
    nct_id: str
    sponsor_name: str
    sponsor_name_norm: str
    sponsor_role: str


@dataclass(frozen=True)
class LinkCandidate:
    nct_id: str
    company_id: int
    match_role: str
    match_method: str
    confidence: float


@dataclass(frozen=True)
class QueryHitRow:
    nct_id: str
    company_id: int
    query_field: str
    source: str
    confidence: float


@dataclass(frozen=True)
class ProgramOwnerOverride:
    nct_id: str
    company_id: int
    confidence: float
    source_name: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Link CTGov trial sponsors/collaborators to companies.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--min-confidence", type=float, default=None)
    parser.add_argument("--tickers", type=str, default="", help="Optional comma-separated ticker subset.")
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


def load_companies(conn: sqlite3.Connection, *, status_filter: set[str], ticker_filter: set[str]) -> list[CompanyAliases]:
    rows = conn.execute(
        """
        SELECT company_id, ticker, company_name, universe_status
        FROM companies
        WHERE is_active = 1
        ORDER BY ticker
        """
    ).fetchall()
    companies: list[CompanyAliases] = []
    for row in rows:
        ticker = str(row["ticker"] or "").upper()
        status = str(row["universe_status"] or "").lower()
        if status_filter and status not in status_filter:
            continue
        if ticker_filter and ticker not in ticker_filter:
            continue
        alias_rows = conn.execute(
            """
            SELECT alias_norm
            FROM company_aliases
            WHERE company_id = ?
            ORDER BY is_manual DESC, confidence DESC, LENGTH(alias_norm) DESC
            """,
            (int(row["company_id"]),),
        ).fetchall()
        alias_norms = {
            normalize_org_name(alias_row["alias_norm"])
            for alias_row in alias_rows
            if str(alias_row["alias_norm"] or "").strip()
        }
        alias_norms.add(normalize_org_name(row["company_name"]))
        alias_norms.add(strip_corporate_suffixes(normalize_org_name(row["company_name"])))
        alias_norms = {alias for alias in alias_norms if alias}
        tokens = tuple(frozenset(value) for value in alias_token_sets(alias_norms))
        companies.append(
            CompanyAliases(
                company_id=int(row["company_id"]),
                ticker=ticker,
                company_name=str(row["company_name"] or ""),
                alias_norms=frozenset(alias_norms),
                alias_tokens=tokens,
            )
        )
    return companies


def load_sponsors(conn: sqlite3.Connection) -> list[SponsorRow]:
    rows = conn.execute(
        """
        SELECT nct_id, sponsor_name, sponsor_name_norm, sponsor_role
        FROM trial_sponsors
        ORDER BY nct_id, sponsor_role, sponsor_name
        """
    ).fetchall()
    return [
        SponsorRow(
            nct_id=str(row["nct_id"] or ""),
            sponsor_name=str(row["sponsor_name"] or ""),
            sponsor_name_norm=normalize_org_name(row["sponsor_name_norm"] or row["sponsor_name"]),
            sponsor_role=str(row["sponsor_role"] or ""),
        )
        for row in rows
    ]


def load_query_hits(conn: sqlite3.Connection, *, company_ids: set[int] | None = None) -> list[QueryHitRow]:
    params: tuple[int, ...] = ()
    where = ""
    if company_ids:
        placeholders = ",".join("?" for _ in sorted(company_ids))
        where = f"WHERE company_id IN ({placeholders})"
        params = tuple(sorted(company_ids))
    rows = conn.execute(
        f"""
        SELECT company_id, nct_id, query_field, source, confidence
        FROM ctgov_query_hits
        {where}
        ORDER BY company_id, nct_id, confidence DESC
        """,
        params,
    ).fetchall()
    return [
        QueryHitRow(
            nct_id=str(row["nct_id"] or ""),
            company_id=int(row["company_id"]),
            query_field=str(row["query_field"] or ""),
            source=str(row["source"] or ""),
            confidence=float(row["confidence"] or 0.0),
        )
        for row in rows
    ]


def as_bool(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "t", "yes", "y"}


def load_program_owner_overrides(
    path: Path | None,
    *,
    companies: Iterable[CompanyAliases],
    ticker_filter: set[str],
) -> list[ProgramOwnerOverride]:
    if path is None or not path.exists():
        return []
    companies_by_ticker = {company.ticker: company for company in companies}
    overrides: list[ProgramOwnerOverride] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"Program owner overrides CSV has no header: {path}")
        for line_no, row in enumerate(reader, start=2):
            try:
                if not as_bool(row.get("enabled", "true")):
                    continue
                ticker = str(row.get("ticker") or "").strip().upper()
                nct_id = str(row.get("nct_id") or "").strip().upper()
                if not ticker or not nct_id:
                    continue
                if ticker_filter and ticker not in ticker_filter:
                    continue
                company = companies_by_ticker.get(ticker)
                if company is None:
                    LOGGER.warning("Ignoring program owner override for unknown ticker at %s:%d: %s", path, line_no, ticker)
                    continue
                confidence = float(row.get("confidence") or 0.95)
                source_name = str(row.get("source_name") or "manual_program_owner").strip() or "manual_program_owner"
                overrides.append(
                    ProgramOwnerOverride(
                        nct_id=nct_id,
                        company_id=company.company_id,
                        confidence=confidence,
                        source_name=source_name,
                    )
                )
            except Exception as exc:
                LOGGER.warning("Ignoring invalid program owner override at %s:%d: %s", path, line_no, exc)
    return overrides


def best_match(
    sponsor_norm: str,
    company: CompanyAliases,
    *,
    allow_single_token_match: bool,
    allow_single_token_prefix_match: bool,
    single_token_prefix_min_length: int,
) -> tuple[str, float]:
    sponsor_norm = normalize_org_name(sponsor_norm)
    sponsor_stripped = strip_corporate_suffixes(sponsor_norm)
    if sponsor_norm in company.alias_norms:
        return "exact_norm", 1.0
    if sponsor_stripped and sponsor_stripped in company.alias_norms:
        return "suffix_stripped_exact", 0.95

    sponsor_tokens = set(meaningful_org_tokens(sponsor_norm))
    if not sponsor_tokens:
        return "", 0.0
    if len(company.ticker) >= 4 and company.ticker in sponsor_tokens:
        return "ticker_token_match", 0.90
    best_method = ""
    best_confidence = 0.0
    for tokens_raw in company.alias_tokens:
        tokens = set(tokens_raw)
        if not tokens:
            continue
        if len(tokens) >= 2 and tokens.issubset(sponsor_tokens):
            confidence = 0.88 if len(tokens) >= 3 else 0.82
            if confidence > best_confidence:
                best_method = "alias_token_subset"
                best_confidence = confidence
        elif allow_single_token_match and len(tokens) == 1:
            token = next(iter(tokens))
            if len(token) >= 5 and token in sponsor_tokens:
                confidence = 0.72
                if confidence > best_confidence:
                    best_method = "single_token_match"
                    best_confidence = confidence
        elif allow_single_token_prefix_match and len(tokens) == 1:
            token = next(iter(tokens))
            if (
                len(token) >= single_token_prefix_min_length
                and sponsor_stripped.startswith(f"{token} ")
                and token not in {"UNITED", "AMERICAN", "NATIONAL", "GLOBAL", "GENERAL", "ADVANCED"}
            ):
                confidence = 0.70
                if confidence > best_confidence:
                    best_method = "single_token_prefix"
                    best_confidence = confidence
    return best_method, best_confidence


def build_links(
    sponsors: Iterable[SponsorRow],
    companies: Iterable[CompanyAliases],
    *,
    min_confidence: float,
    allow_single_token_match: bool,
    allow_single_token_prefix_match: bool,
    single_token_prefix_min_length: int,
) -> list[LinkCandidate]:
    best_by_key: dict[tuple[str, int, str], LinkCandidate] = {}
    company_list = list(companies)
    exact_index: dict[str, list[CompanyAliases]] = {}
    ticker_index: dict[str, list[CompanyAliases]] = {}
    token_index: dict[str, list[tuple[frozenset[str], CompanyAliases]]] = {}
    single_token_index: dict[str, list[CompanyAliases]] = {}

    for company in company_list:
        if len(company.ticker) >= 4:
            ticker_index.setdefault(company.ticker, []).append(company)
        for alias in company.alias_norms:
            if alias:
                exact_index.setdefault(alias, []).append(company)
                stripped = strip_corporate_suffixes(alias)
                if stripped:
                    exact_index.setdefault(stripped, []).append(company)
        seen_token_sets: set[frozenset[str]] = set()
        for tokens_raw in company.alias_tokens:
            tokens = frozenset(tokens_raw)
            if not tokens or tokens in seen_token_sets:
                continue
            seen_token_sets.add(tokens)
            if len(tokens) == 1:
                token = next(iter(tokens))
                single_token_index.setdefault(token, []).append(company)
            else:
                for token in tokens:
                    token_index.setdefault(token, []).append((tokens, company))

    def remember(
        candidates: dict[int, tuple[CompanyAliases, str, float]],
        company: CompanyAliases,
        method: str,
        confidence: float,
    ) -> None:
        if confidence <= 0.0:
            return
        old = candidates.get(company.company_id)
        if old is None or confidence > old[2]:
            candidates[company.company_id] = (company, method, confidence)

    for sponsor in sponsors:
        if not sponsor.nct_id or not sponsor.sponsor_name_norm:
            continue
        role = "lead" if sponsor.sponsor_role == "lead" else "collaborator"
        sponsor_norm = normalize_org_name(sponsor.sponsor_name_norm)
        sponsor_stripped = strip_corporate_suffixes(sponsor_norm)
        sponsor_tokens = set(meaningful_org_tokens(sponsor_norm))
        candidates: dict[int, tuple[CompanyAliases, str, float]] = {}

        for company in exact_index.get(sponsor_norm, []):
            remember(candidates, company, "exact_norm", 1.0)
        if sponsor_stripped and sponsor_stripped != sponsor_norm:
            for company in exact_index.get(sponsor_stripped, []):
                remember(candidates, company, "suffix_stripped_exact", 0.95)

        for token in sponsor_tokens:
            for company in ticker_index.get(token, []):
                remember(candidates, company, "ticker_token_match", 0.90)

        checked_token_sets: set[tuple[frozenset[str], int]] = set()
        for token in sponsor_tokens:
            for tokens, company in token_index.get(token, []):
                key = (tokens, company.company_id)
                if key in checked_token_sets:
                    continue
                checked_token_sets.add(key)
                if tokens.issubset(sponsor_tokens):
                    confidence = 0.88 if len(tokens) >= 3 else 0.82
                    remember(candidates, company, "alias_token_subset", confidence)

        if allow_single_token_match:
            for token in sponsor_tokens:
                if len(token) < 5:
                    continue
                for company in single_token_index.get(token, []):
                    remember(candidates, company, "single_token_match", 0.72)

        if allow_single_token_prefix_match:
            first_token = sponsor_stripped.split()[0] if sponsor_stripped.split() else ""
            if first_token and sponsor_stripped.startswith(f"{first_token} "):
                for company in single_token_index.get(first_token, []):
                    if (
                        len(first_token) >= single_token_prefix_min_length
                        and first_token not in {"UNITED", "AMERICAN", "NATIONAL", "GLOBAL", "GENERAL", "ADVANCED"}
                    ):
                        remember(candidates, company, "single_token_prefix", 0.70)

        for company, method, confidence in candidates.values():
            if confidence < min_confidence:
                continue
            key = (sponsor.nct_id, company.company_id, role)
            candidate = LinkCandidate(
                nct_id=sponsor.nct_id,
                company_id=company.company_id,
                match_role=role,
                match_method=method,
                confidence=confidence,
            )
            old = best_by_key.get(key)
            if old is None or candidate.confidence > old.confidence:
                best_by_key[key] = candidate
    return list(best_by_key.values())


def query_hit_links(query_hits: Iterable[QueryHitRow], *, min_confidence: float) -> list[LinkCandidate]:
    best_by_key: dict[tuple[str, int, str], LinkCandidate] = {}
    for hit in query_hits:
        if not hit.nct_id or hit.confidence < min_confidence:
            continue
        query_field = str(hit.query_field or "").strip().lower()
        if query_field in {"query.spons", "query.lead"}:
            # Sponsor/lead overrides are discovery terms. They should not create
            # separate program links because sponsor matching already establishes
            # the lead/collaborator relationship from CTGov sponsor fields.
            continue
        method = f"manual_search_term:{hit.query_field or 'unknown'}"
        if hit.source:
            method = f"{method}:{hit.source}"
        candidate = LinkCandidate(
            nct_id=hit.nct_id,
            company_id=hit.company_id,
            match_role="program",
            match_method=method[:120],
            confidence=hit.confidence,
        )
        key = (candidate.nct_id, candidate.company_id, candidate.match_role)
        old = best_by_key.get(key)
        if old is None or candidate.confidence > old.confidence:
            best_by_key[key] = candidate
    return list(best_by_key.values())


def program_owner_links(
    overrides: Iterable[ProgramOwnerOverride],
    *,
    min_confidence: float,
) -> list[LinkCandidate]:
    best_by_key: dict[tuple[str, int, str], LinkCandidate] = {}
    for override in overrides:
        if not override.nct_id or override.confidence < min_confidence:
            continue
        candidate = LinkCandidate(
            nct_id=override.nct_id,
            company_id=override.company_id,
            match_role="program",
            match_method=f"program_owner_override:{override.source_name}"[:120],
            confidence=override.confidence,
        )
        key = (candidate.nct_id, candidate.company_id, candidate.match_role)
        old = best_by_key.get(key)
        if old is None or candidate.confidence > old.confidence:
            best_by_key[key] = candidate
    return list(best_by_key.values())


def dedupe_links(links: Iterable[LinkCandidate]) -> list[LinkCandidate]:
    best_by_key: dict[tuple[str, int, str], LinkCandidate] = {}
    for link in links:
        key = (link.nct_id, link.company_id, link.match_role)
        old = best_by_key.get(key)
        if old is None or link.confidence > old.confidence:
            best_by_key[key] = link
    return list(best_by_key.values())


def replace_links(conn: sqlite3.Connection, links: list[LinkCandidate], *, company_ids: set[int] | None = None) -> None:
    now = utc_now()
    if company_ids:
        placeholders = ",".join("?" for _ in sorted(company_ids))
        conn.execute(f"DELETE FROM trial_company_links WHERE company_id IN ({placeholders})", tuple(sorted(company_ids)))
    else:
        conn.execute("DELETE FROM trial_company_links")
    for link in links:
        conn.execute(
            """
            INSERT INTO trial_company_links(
                nct_id, company_id, match_role, match_method, confidence, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(nct_id, company_id, match_role) DO UPDATE SET
                match_method = excluded.match_method,
                confidence = excluded.confidence,
                updated_at = excluded.updated_at
            """,
            (
                link.nct_id,
                link.company_id,
                link.match_role,
                link.match_method,
                float(link.confidence),
                now,
                now,
            ),
        )


def main() -> None:
    configure_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    status_filter = {
        value.lower()
        for value in normalize_string_list(cfg_get(config, "trial_linking.status_filter"), ["keep", "review"])
    }
    min_confidence = float(
        args.min_confidence if args.min_confidence is not None else cfg_get(config, "trial_linking.min_confidence", 0.65)
    )
    allow_single_token_match = str(cfg_get(config, "trial_linking.allow_single_token_match", False)).strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }
    allow_single_token_prefix_match = str(
        cfg_get(config, "trial_linking.allow_single_token_prefix_match", True)
    ).strip().lower() in {"1", "true", "yes", "y"}
    single_token_prefix_min_length = int(cfg_get(config, "trial_linking.single_token_prefix_min_length", 7))
    ticker_filter = {value.strip().upper().replace(".", "-") for value in args.tickers.split(",") if value.strip()}

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        run_id = start_run(conn, run_type="link_trials_to_companies", input_path=db_path)
        try:
            companies = load_companies(conn, status_filter=status_filter, ticker_filter=ticker_filter)
            company_ids = {company.company_id for company in companies}
            sponsors = load_sponsors(conn)
            manual_query_hits = load_query_hits(conn, company_ids=company_ids)
            program_owner_overrides_path = resolve_optional_path(
                cfg_get(config, "trial_linking.program_owner_overrides_csv"),
                base_dir=base_dir,
            )
            manual_program_owner_overrides = load_program_owner_overrides(
                program_owner_overrides_path,
                companies=companies,
                ticker_filter=ticker_filter,
            )
            LOGGER.info(
                "Loaded companies=%d sponsors=%d query_hits=%d program_owner_overrides=%d min_confidence=%.2f allow_single_token_match=%s allow_single_token_prefix_match=%s",
                len(companies),
                len(sponsors),
                len(manual_query_hits),
                len(manual_program_owner_overrides),
                min_confidence,
                allow_single_token_match,
                allow_single_token_prefix_match,
            )
            sponsor_links = build_links(
                sponsors,
                companies,
                min_confidence=min_confidence,
                allow_single_token_match=allow_single_token_match,
                allow_single_token_prefix_match=allow_single_token_prefix_match,
                single_token_prefix_min_length=single_token_prefix_min_length,
            )
            links = dedupe_links(
                [
                    *sponsor_links,
                    *query_hit_links(manual_query_hits, min_confidence=min_confidence),
                    *program_owner_links(manual_program_owner_overrides, min_confidence=min_confidence),
                ]
            )
            replace_company_ids = company_ids if ticker_filter else None
            with conn:
                replace_links(conn, links, company_ids=replace_company_ids)
            linked_companies = len({link.company_id for link in links})
            linked_trials = len({link.nct_id for link in links})
            message = f"links={len(links)} linked_companies={linked_companies} linked_trials={linked_trials}"
            finish_run(conn, run_id=run_id, status="success", row_count=len(links), message=message)
            LOGGER.info("Trial linking complete: %s", message)
        except Exception as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()

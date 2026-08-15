#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import FilingRef  # noqa: E402
from technology.core.config import cfg_get, expand_env_vars, load_yaml, resolve_path  # noqa: E402
from technology.core.db import connect, init_db  # noqa: E402
from technology.core.financial_filing_recovery import (  # noqa: E402
    RECOVERY_VERSION,
    recover_cached_filing,
)
from technology.software_infrastructure.software_parser_hydration import (  # noqa: E402
    HydrationFiling,
    hydrate_filings,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_CACHE = PROJECT_ROOT / "output" / "technology_cache" / "dedicated_parser"
DEFAULT_OUTPUT = PROJECT_ROOT / "output" / "technology_reports" / "financial_lineage_recovery"
SUPPORTED_FAMILIES = frozenset(
    {"semiconductors", "software_infrastructure", "technology_hardware"}
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recover canonical source facts from cached or explicitly hydrated "
            "foreign-filer 6-K document sets."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--family", required=True, choices=sorted(SUPPORTED_FAMILIES))
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--tickers", default="")
    parser.add_argument("--accessions", default="")
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=550,
        help=(
            "Calendar-day window for foreign-filer recovery. The default spans "
            "at least one annual reporting cycle so a valid latest financial 6-K "
            "is not skipped merely because the issuer files sparse interim reports."
        ),
    )
    parser.add_argument("--max-filings-per-ticker", type=int, default=12)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--hydrate", action="store_true")
    parser.add_argument("--require-all-recovered", action="store_true")
    return parser.parse_args()


def _parse_date(value: str, *, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD: {value!r}") from exc


def _atomic_text(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with open(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        _atomic_text(path, "ticker,accession_number,status\n")
        return
    from io import StringIO

    handle = StringIO(newline="")
    fieldnames: list[str] = []
    for row in rows:
        fieldnames.extend(key for key in row if key not in fieldnames)
    writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    _atomic_text(path, handle.getvalue())


def _select_filings(
    conn: sqlite3.Connection,
    *,
    family: str,
    asof: str,
    start_date: str,
    tickers: tuple[str, ...],
    accessions: tuple[str, ...],
    max_filings_per_ticker: int,
) -> list[dict[str, Any]]:
    filters: list[str] = []
    params: list[Any] = [family, asof, asof, start_date, asof]
    if tickers:
        filters.append(f"f.ticker IN ({','.join('?' for _ in tickers)})")
        params.extend(tickers)
    if accessions:
        filters.append(
            f"f.accession_number IN ({','.join('?' for _ in accessions)})"
        )
        params.extend(accessions)
    extra_filter = "" if not filters else "AND " + " AND ".join(filters)
    rows = conn.execute(
        f"""
        WITH eligible AS (
            SELECT DISTINCT
                f.ticker, f.cik, f.accession_number, UPPER(f.form_type) AS form_type,
                COALESCE(f.filing_date, '') AS filing_date,
                COALESCE(f.acceptance_datetime, '') AS accepted_at,
                COALESCE(f.report_date, '') AS report_date,
                COALESCE(f.primary_document, '') AS primary_document,
                f.source_id,
                COALESCE(NULLIF(p.primary_reporting_taxonomy, ''), 'ifrs-full')
                    AS primary_taxonomy,
                COALESCE(NULLIF(c.currency, ''), '') AS company_currency,
                ROW_NUMBER() OVER (
                    PARTITION BY f.ticker
                    ORDER BY COALESCE(NULLIF(f.acceptance_datetime, ''), f.filing_date) DESC,
                             f.accession_number DESC
                ) AS ticker_sequence
            FROM fact_sec_filing AS f
            JOIN (
                SELECT DISTINCT ticker
                FROM dim_universe_membership
                WHERE model_family = ?
                  AND start_date <= ?
                  AND COALESCE(NULLIF(end_date, ''), '9999-12-31') >= ?
            ) AS m ON m.ticker = f.ticker
            LEFT JOIN dim_issuer_reporting_profile AS p ON p.ticker = f.ticker
            LEFT JOIN dim_company AS c ON c.ticker = f.ticker
            WHERE UPPER(f.form_type) IN ('6-K', '6-K/A')
              AND SUBSTR(COALESCE(NULLIF(f.acceptance_datetime, ''), f.filing_date), 1, 10)
                    BETWEEN ? AND ?
              {extra_filter}
        )
        SELECT * FROM eligible
        WHERE (? = 0 OR ticker_sequence <= ?)
        ORDER BY ticker, ticker_sequence
        """,
        (*params, max_filings_per_ticker, max_filings_per_ticker),
    ).fetchall()
    selected = [dict(row) for row in rows]
    if accessions:
        found = {str(row["accession_number"]) for row in selected}
        missing = sorted(set(accessions) - found)
        if missing:
            raise ValueError(f"Requested 6-K accessions were not eligible: {missing}")
    return selected


def _filing_ref(row: dict[str, Any]) -> FilingRef:
    return FilingRef(
        ticker=str(row["ticker"]),
        cik=str(row["cik"]),
        accession_number=str(row["accession_number"]),
        form_type=str(row["form_type"]),
        filing_date=str(row["filing_date"]),
        accepted_at=str(row["accepted_at"]),
        report_date=str(row["report_date"]),
        primary_document=str(row["primary_document"]),
        source_id=str(row["source_id"]),
        company_currency=str(row["company_currency"] or "USD").upper(),
    )


def _hydration_filing(row: dict[str, Any]) -> HydrationFiling:
    return HydrationFiling(
        ticker=str(row["ticker"]),
        cik=str(row["cik"]),
        accession_number=str(row["accession_number"]),
        form_type=str(row["form_type"]),
        filing_date=str(row["filing_date"]),
        accepted_at=str(row["accepted_at"]),
        report_date=str(row["report_date"]),
        primary_document=str(row["primary_document"]),
        source_id=str(row["source_id"]),
    )


def main() -> int:
    args = parse_args()
    if args.lookback_days < 0:
        raise ValueError("--lookback-days must be non-negative")
    if args.max_filings_per_ticker < 0:
        raise ValueError("--max-filings-per-ticker must be non-negative")
    asof_date = _parse_date(args.asof, field="asof")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=config_path.parent)
    )
    cache_dir = args.cache_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve() / args.family / args.asof
    tickers = tuple(
        sorted({value.strip().upper() for value in args.tickers.split(",") if value.strip()})
    )
    accessions = tuple(
        dict.fromkeys(value.strip() for value in args.accessions.split(",") if value.strip())
    )
    start_date = (asof_date - timedelta(days=args.lookback_days)).isoformat()
    timeout = float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))
    with connect(db_path, timeout_sec=timeout) as conn:
        init_db(conn)
        selected = _select_filings(
            conn,
            family=args.family,
            asof=args.asof,
            start_date=start_date,
            tickers=tickers,
            accessions=accessions,
            max_filings_per_ticker=args.max_filings_per_ticker,
        )
    if args.hydrate and selected:
        user_agent = expand_env_vars(
            cfg_get(
                config,
                "sec_fundamentals.user_agent",
                "Independent technology research contact@example.com",
            )
        )
        if "@" not in user_agent:
            raise ValueError("SEC User-Agent must contain a contact email address")
        hydrate_filings(
            [_hydration_filing(row) for row in selected],
            cache_dir=cache_dir,
            output_dir=output_dir / "hydration",
            user_agent=user_agent,
            timeout_sec=30.0,
            max_retries=3,
            request_spacing_sec=0.2,
            execute=True,
            model_family=args.family,
            artifact_stem="technology_financial_lineage",
            hydration_version="technology_financial_lineage_hydration_v1",
        )

    facts_source_id = str(
        cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts")
    )
    results: list[dict[str, Any]] = []
    with connect(db_path, timeout_sec=timeout) as conn:
        init_db(conn)
        for row in selected:
            taxonomy = str(row["primary_taxonomy"] or "ifrs-full")
            if taxonomy not in {"ifrs-full", "us-gaap"}:
                taxonomy = "ifrs-full"
            with conn:
                results.append(
                    recover_cached_filing(
                        conn,
                        cache_dir=cache_dir,
                        filing=_filing_ref(row),
                        facts_source_id=facts_source_id,
                        primary_taxonomy=taxonomy,
                        fallback_currency=str(row["company_currency"] or "").upper(),
                    )
                )

    report_path = output_dir / "technology_6k_financial_recovery.csv"
    _write_csv(report_path, results)
    insufficient = [
        row
        for row in results
        if row["status"] not in {"RECOVERED", "CACHE_MISSING", "CACHE_EMPTY"}
    ]
    missing_cache = [
        row for row in results if row["status"] in {"CACHE_MISSING", "CACHE_EMPTY"}
    ]
    acceptance = (
        "FAIL"
        if args.require_all_recovered and (insufficient or missing_cache)
        else "PASS"
    )
    manifest = {
        "schema_version": RECOVERY_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model_family": args.family,
        "asof_date": args.asof,
        "database_path": str(db_path),
        "cache_dir": str(cache_dir),
        "selected_filing_count": len(selected),
        "recovered_filing_count": sum(row["status"] == "RECOVERED" for row in results),
        "insufficient_core_filing_count": len(insufficient),
        "missing_cache_filing_count": len(missing_cache),
        "report_path": str(report_path),
        "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        "hydration_requested": bool(args.hydrate),
        "require_all_recovered": bool(args.require_all_recovered),
        "acceptance": acceptance,
    }
    _atomic_text(
        output_dir / "technology_6k_financial_recovery.json",
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if acceptance == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

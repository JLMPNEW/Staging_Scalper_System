#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import logging
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.sec_predecessor_bridge import (  # noqa: E402
    BRIDGE_METRICS,
    certified_predecessor_payload,
    load_certified_predecessor_rows,
)
from industrials.core.text_norm import normalize_ticker  # noqa: E402


SEC_SYNC = importlib.import_module("industrials.scripts.07_sync_industrials_sec_fundamentals")
LOGGER = logging.getLogger("refresh_industrials_predecessor_bridge")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reparse one cached SEC registration document for an audited predecessor bridge."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--model-family", default="")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--accession", required=True)
    parser.add_argument("--asof", required=True)
    return parser.parse_args()


def load_filing(
    conn: Any,
    *,
    ticker: str,
    accession: str,
    source_id: str,
    asof: str,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT accession_number, form_type, filing_date, accepted_at, report_date,
               fiscal_year, fiscal_period, primary_document
        FROM fact_sec_filing
        WHERE ticker = ? AND accession_number = ? AND source_id = ?
          AND COALESCE(substr(accepted_at, 1, 10), filing_date) <= ?
        """,
        (ticker, accession, source_id, asof),
    ).fetchone()
    if row is None:
        raise ValueError(f"No PIT filing metadata for ticker={ticker} accession={accession} asof={asof}")
    return dict(row)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    ticker = normalize_ticker(args.ticker)
    accession = str(args.accession or "").strip()
    asof = SEC_SYNC.parse_date(args.asof)
    if not ticker or not accession or not asof:
        raise ValueError("--ticker, --accession, and a valid YYYY-MM-DD --asof are required")
    model_family = str(args.model_family or cfg_get(config, "industrials_universe.initial_subsector", "defense") or "defense").strip()
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    cache_dir = resolve_path(cfg_get(config, "sec_fundamentals.cache_dir"), base_dir=base_dir)
    submissions_source_id = str(cfg_get(config, "sec_fundamentals.submissions_source_id", "sec_submissions") or "sec_submissions")
    companyfacts_source_id = str(cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts") or "sec_companyfacts")
    start_date = SEC_SYNC.parse_date(cfg_get(config, "sec_fundamentals.start_date", "2000-01-01"))

    with closing(connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0)))) as conn:
        filing = load_filing(
            conn,
            ticker=ticker,
            accession=accession,
            source_id=submissions_source_id,
            asof=asof,
        )
        company = conn.execute(
            """
            SELECT c.cik, c.currency
            FROM dim_company c
            JOIN dim_industrials_taxonomy t ON t.company_id = c.company_id
            WHERE c.ticker = ? AND t.model_family = ?
            """,
            (ticker, model_family),
        ).fetchone()
        if company is None:
            raise ValueError(f"Ticker not found in dim_company: {ticker}")
        cik = SEC_SYNC.sec_cik(company["cik"])
        document_name = str(filing.get("primary_document") or "").strip()
        cache_file = SEC_SYNC.archive_cache_file(
            cache_dir,
            cik=cik,
            accession=accession,
            document_name=document_name,
        )
        if not cache_file.exists():
            raise FileNotFoundError(f"Cached SEC document not found: {cache_file}")
        document_text = cache_file.read_text(encoding="utf-8", errors="replace")
        facts = SEC_SYNC.parse_archive_text_table_facts(
            document_text,
            document_name=document_name,
            filing=filing,
            company_currency=str(company["currency"] or ""),
        )
        certified_facts = [
            fact
            for fact in facts
            if fact.concept_name in {
                "Revenue",
                "OperatingCashFlow",
                "Capex",
                "NetIncomeLoss",
                "Assets",
                "CashAndCashEquivalents",
            }
            and certified_predecessor_payload(fact.payload_json)
        ]
        if not certified_facts:
            raise ValueError(f"No certified audited predecessor facts parsed from {cache_file}")
        concept_map = SEC_SYNC.load_concept_map(conn)
        with conn:
            raw_count, mapped_count = SEC_SYNC.upsert_archive_facts(
                conn,
                ticker=ticker,
                cik=cik,
                source_id=companyfacts_source_id,
                filing=filing,
                document_name=document_name,
                facts=facts,
                concept_map=concept_map,
                start_date=start_date,
            )
        bridge_rows = load_certified_predecessor_rows(
            conn,
            ticker=ticker,
            source_id=companyfacts_source_id,
            asof=datetime.strptime(asof, "%Y-%m-%d").date(),
        )
        bridge_metrics = {str(row.get("canonical_metric") or "") for row in bridge_rows}
        required = {"revenue", "operating_cash_flow", "capex", "net_income"}
        missing = sorted(required - bridge_metrics)
        if missing:
            raise ValueError(f"Certified predecessor bridge is incomplete for {ticker}: missing={missing}")
        LOGGER.info(
            "Refreshed predecessor bridge ticker=%s accession=%s raw=%d mapped=%d bridge_rows=%d bridge_metrics=%s configured_metrics=%d",
            ticker,
            accession,
            raw_count,
            mapped_count,
            len(bridge_rows),
            sorted(bridge_metrics),
            len(BRIDGE_METRICS),
        )


if __name__ == "__main__":
    main()

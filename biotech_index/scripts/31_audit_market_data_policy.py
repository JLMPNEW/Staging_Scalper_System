#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from biotech_index.core.db import connect  # noqa: E402
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402
from biotech_index.core.market_policy import scoring_market_sources, select_latest_rows_by_source_priority  # noqa: E402
from biotech_index.core.pipeline_guards import read_final_scoring_tickers  # noqa: E402
from biotech_index.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("audit_market_data_policy")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
FIELDNAMES = [
    "asof_date",
    "ticker",
    "company_id",
    "selected_source",
    "selected_source_rank",
    "selected_asof_date",
    "selected_last_bar_date",
    "selected_price_adjustment",
    "selected_is_adjusted",
    "selected_bar_count",
    "selected_market_data_quality",
    "available_sources",
    "available_adjustments",
    "requires_adjusted_for_scoring",
    "policy_status",
    "review_reason",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit biotech market-data source policy and selected scoring rows.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="Audit date in YYYY-MM-DD. Defaults to latest daily_scores date.")
    parser.add_argument("--output-csv", type=Path, default=None)
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def as_bool(raw: object, default: bool = False) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def load_companies(conn: sqlite3.Connection, tickers: set[str]) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT company_id, ticker, company_name
        FROM companies
        WHERE is_active = 1
        ORDER BY ticker
        """
    ).fetchall()
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        if ticker in tickers:
            out[int(row["company_id"])] = dict(row)
    return out


def latest_score_date(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT MAX(asof_date) AS asof_date FROM daily_scores").fetchone()
    asof = str(row["asof_date"] or "") if row else ""
    if not asof:
        raise ValueError("No daily_scores rows available to infer audit asof date")
    return asof


def load_market_rows(conn: sqlite3.Connection, company_ids: list[int], asof_date: str, sources: list[str]) -> list[sqlite3.Row]:
    if not company_ids:
        return []
    company_placeholders = ",".join("?" for _ in company_ids)
    source_clause = ""
    params: list[Any] = [*company_ids, asof_date]
    if sources:
        source_clause = " AND source IN (" + ",".join("?" for _ in sources) + ")"
        params.extend(sources)
    return conn.execute(
        f"""
        SELECT f.*
        FROM market_features_daily f
        JOIN (
            SELECT company_id, source, MAX(asof_date) AS max_asof
            FROM market_features_daily
            WHERE company_id IN ({company_placeholders})
              AND asof_date <= ?{source_clause}
            GROUP BY company_id, source
        ) latest
          ON latest.company_id = f.company_id
         AND latest.source = f.source
         AND latest.max_asof = f.asof_date
        """,
        tuple(params),
    ).fetchall()


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(cfg_get(config, "market_data_policy.audit_output_csv"), base_dir=base_dir)
    )
    universe_csv = resolve_path(
        cfg_get(config, "biotech_features.final_scoring_universe_csv", "../output/biotech_index_reports/ctgov_final_scoring_universe.csv"),
        base_dir=base_dir,
    )
    sources = scoring_market_sources(config)
    require_adjusted = as_bool(cfg_get(config, "market_data_policy.require_adjusted_for_scoring", True), True)
    max_staleness_days = int(cfg_get(config, "biotech_refresh.max_upstream_staleness_days", 0))

    with connect(db_path) as conn:
        asof = args.asof or latest_score_date(conn)
        asof_obj = parse_date(asof)
        if asof_obj is None:
            raise ValueError(f"Invalid asof date: {asof}")
        expected_tickers = read_final_scoring_tickers(universe_csv)
        companies = load_companies(conn, expected_tickers)
        rows = load_market_rows(conn, sorted(companies), asof, sources)
        selected = select_latest_rows_by_source_priority(
            rows,
            asof_date=asof_obj,
            source_priority=sources,
            max_staleness_days=max_staleness_days,
        )
        available_by_company: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            available_by_company.setdefault(int(row["company_id"]), []).append(dict(row))

    source_rank = {source: idx for idx, source in enumerate(sources)}
    audit_rows: list[dict[str, Any]] = []
    for company_id, company in sorted(companies.items(), key=lambda item: str(item[1]["ticker"] or "")):
        ticker = normalize_ticker(company["ticker"])
        selected_row = selected.get(company_id, {})
        available = available_by_company.get(company_id, [])
        available_sources = sorted({str(row.get("source") or "") for row in available if str(row.get("source") or "")})
        available_adjustments = sorted(
            {
                f"{row.get('source') or ''}:{row.get('price_adjustment') or ''}:{int(row.get('is_adjusted') or 0)}"
                for row in available
                if str(row.get("source") or "")
            }
        )
        reasons: list[str] = []
        source = str(selected_row.get("source") or "")
        is_adjusted = int(selected_row.get("is_adjusted") or 0) if selected_row else 0
        if not selected_row:
            reasons.append("missing_market_row")
        if require_adjusted and selected_row and not is_adjusted:
            reasons.append("selected_row_not_adjusted")
        if selected_row and source != sources[0]:
            reasons.append("fallback_source_used")
        audit_rows.append(
            {
                "asof_date": asof,
                "ticker": ticker,
                "company_id": company_id,
                "selected_source": source,
                "selected_source_rank": source_rank.get(source, ""),
                "selected_asof_date": selected_row.get("asof_date", ""),
                "selected_last_bar_date": selected_row.get("last_bar_date", ""),
                "selected_price_adjustment": selected_row.get("price_adjustment", ""),
                "selected_is_adjusted": is_adjusted if selected_row else "",
                "selected_bar_count": selected_row.get("bar_count", ""),
                "selected_market_data_quality": selected_row.get("market_data_quality", ""),
                "available_sources": ";".join(available_sources),
                "available_adjustments": ";".join(available_adjustments),
                "requires_adjusted_for_scoring": int(require_adjusted),
                "policy_status": "pass" if not reasons else "fail",
                "review_reason": ";".join(reasons),
            }
        )
    write_csv(output_csv, audit_rows)
    counts: dict[str, int] = {}
    for row in audit_rows:
        key = f"{row['policy_status']}:{row['selected_source'] or '<missing>'}"
        counts[key] = counts.get(key, 0) + 1
    LOGGER.info("Market policy audit written: %s rows=%d counts=%s", output_csv, len(audit_rows), counts)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as exc:
        configure_utc_logging()
        LOGGER.exception("Fatal market policy audit error: %s", exc)
        raise SystemExit(1) from exc

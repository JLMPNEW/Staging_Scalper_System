#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.db import connect, init_db  # noqa: E402
from technology.core.logging_utils import configure_utc_logging  # noqa: E402
from technology.core.text_norm import normalize_ticker  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
FIELDNAMES = [
    "ticker",
    "membership_scope",
    "is_active",
    "universe_status",
    "company_name",
    "cik",
    "exchange",
    "security_type",
    "country",
    "section16_expected_status",
    "insider_coverage_status",
    "primary_insider_source",
    "fpi_qualifying_exemption_status",
    "local_insider_source_required",
    "latest_ownership_filing_date",
    "ownership_filing_count",
    "ownership_transaction_count",
    "upstream_form4_count",
    "upstream_form4_min_date",
    "upstream_form4_max_date",
    "direct_form4_count",
    "direct_form4_min_date",
    "direct_form4_max_date",
    "direct_ownership_filing_count",
    "direct_ownership_nonderiv_count",
    "positioning_feature_rows",
    "positioning_quality",
    "reconciliation_status",
    "recommended_next_step",
]


@dataclass(frozen=True)
class Summary:
    count: int = 0
    min_date: str = ""
    max_date: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit technology Form 4/direct ownership reconciliation.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--model-family", default="", help="Technology model family to audit, e.g. semiconductors.")
    parser.add_argument("--asof", default="")
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--include-historical", action=argparse.BooleanOptionalAction, default=True)
    return parser.parse_args()


def table_exists(conn: Any, table: str) -> bool:
    return bool(conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,)).fetchone())


def scalar(conn: Any, sql: str, params: tuple[Any, ...]) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def summary_map(
    conn: Any,
    *,
    table: str,
    ticker_col: str,
    date_col: str,
    source_id: str,
    source_col: str = "source_id",
    code_filter: str = "",
    date_max: str = "",
) -> dict[str, Summary]:
    if not table_exists(conn, table):
        return {}
    filters = [f"{source_col} = ?"]
    params: list[Any] = [source_id]
    if date_max:
        filters.append(f"COALESCE({date_col}, '') <= ?")
        params.append(date_max)
    if code_filter:
        filters.append(code_filter)
    rows = conn.execute(
        f"""
        SELECT
            UPPER({ticker_col}) AS ticker,
            COUNT(*) AS row_count,
            MIN({date_col}) AS min_date,
            MAX({date_col}) AS max_date
        FROM {table}
        WHERE {" AND ".join(filters)}
          AND COALESCE({date_col}, '') <> ''
        GROUP BY UPPER({ticker_col})
        """,
        tuple(params),
    ).fetchall()
    return {
        normalize_ticker(row["ticker"]): Summary(
            count=int(row["row_count"] or 0),
            min_date=str(row["min_date"] or ""),
            max_date=str(row["max_date"] or ""),
        )
        for row in rows
        if normalize_ticker(row["ticker"])
    }


def count_map(
    conn: Any,
    *,
    table: str,
    ticker_col: str,
    source_id: str,
    source_col: str = "source_id",
    code_filter: str = "",
) -> dict[str, int]:
    if not table_exists(conn, table):
        return {}
    filters = [f"{source_col} = ?"]
    params: list[Any] = [source_id]
    if code_filter:
        filters.append(code_filter)
    rows = conn.execute(
        f"""
        SELECT UPPER({ticker_col}) AS ticker, COUNT(*) AS row_count
        FROM {table}
        WHERE {" AND ".join(filters)}
        GROUP BY UPPER({ticker_col})
        """,
        tuple(params),
    ).fetchall()
    return {normalize_ticker(row["ticker"]): int(row["row_count"] or 0) for row in rows if normalize_ticker(row["ticker"])}


def active_companies(conn: Any, *, model_family: str, include_historical: bool) -> list[dict[str, Any]]:
    if include_historical:
        model_join = """
        JOIN dim_universe_membership m
          ON m.ticker = c.ticker
         AND m.model_family = ?
         AND (m.is_current_member = 1 OR m.point_in_time_flag = 1)
        """
        where = ""
    else:
        model_join = """
        JOIN dim_technology_taxonomy t
          ON t.ticker = c.ticker
         AND t.model_family = ?
        """
        where = "WHERE c.is_active = 1"
    rows = conn.execute(
        f"""
        SELECT
            c.ticker,
            c.company_name,
            c.cik,
            c.country,
            c.universe_status,
            c.is_active,
            COALESCE(s.exchange, '') AS exchange,
            COALESCE(s.security_type, '') AS security_type
        FROM dim_company c
        {model_join}
        LEFT JOIN dim_security s
          ON s.company_id = c.company_id
         AND s.is_primary_listing = 1
        {where}
        ORDER BY c.is_active DESC, UPPER(c.ticker)
        """,
        (model_family,),
    ).fetchall()
    return [dict(row) for row in rows]


def profile_map(conn: Any) -> dict[str, dict[str, Any]]:
    if not table_exists(conn, "dim_insider_reporting_profile"):
        return {}
    rows = conn.execute("SELECT * FROM dim_insider_reporting_profile").fetchall()
    return {normalize_ticker(row["ticker"]): dict(row) for row in rows if normalize_ticker(row["ticker"])}


def feature_map(conn: Any, *, source_id: str, model_family: str, asof: str) -> dict[str, dict[str, Any]]:
    if not table_exists(conn, "feature_positioning"):
        return {}
    date_filter = "AND asof_date <= ?" if asof else ""
    params: list[Any] = [source_id, model_family]
    if asof:
        params.append(asof)
    rows = conn.execute(
        f"""
        WITH ranked AS (
            SELECT
                *,
                ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY asof_date DESC) AS rn
            FROM feature_positioning
            WHERE source_id = ?
              AND model_family = ?
              {date_filter}
        )
        SELECT ticker, asof_date, positioning_quality
        FROM ranked
        WHERE rn = 1
        """,
        tuple(params),
    ).fetchall()
    return {normalize_ticker(row["ticker"]): dict(row) for row in rows if normalize_ticker(row["ticker"])}


def is_exempt_or_local(profile: dict[str, Any]) -> bool:
    text = " ".join(
        str(profile.get(key) or "").lower()
        for key in (
            "section16_expected_status",
            "coverage_status",
            "primary_insider_source",
            "fpi_qualifying_exemption_status",
            "review_reason",
        )
    )
    return bool(int(profile.get("local_insider_source_required") or 0)) or any(
        token in text for token in ("fpi", "foreign", "exempt", "local")
    )


def classify(
    *,
    company: dict[str, Any],
    profile: dict[str, Any],
    upstream: Summary,
    direct: Summary,
    direct_filing_count: int,
    direct_nonderiv_count: int,
    feature_rows: int,
) -> tuple[str, str]:
    is_active = int(company.get("is_active") or 0) == 1
    has_form4 = upstream.count > 0 or direct.count > 0
    has_direct_evidence = direct_filing_count > 0 or direct_nonderiv_count > 0 or int(profile.get("ownership_filing_count") or 0) > 0
    if has_form4:
        return (
            "current_covered" if is_active else "historical_covered",
            "No action; Form 4/direct ownership transaction evidence exists.",
        )
    if is_exempt_or_local(profile):
        return (
            "current_expected_non_sec_or_local_source" if is_active else "historical_expected_non_sec_or_local_source",
            "No SEC Form 4 remediation; issuer is foreign/FPI/local-source expected or exempt.",
        )
    if has_direct_evidence:
        return (
            "current_filings_found_no_transactions" if is_active else "historical_filings_found_no_transactions",
            "No importer failure indicated; SEC ownership filings exist but no open-market P/S transaction rows were found.",
        )
    if not is_active:
        return "historical_no_current_requirement", "Historical/inactive ticker; no current production Form 4 coverage requirement."
    if feature_rows <= 0:
        return "current_missing_feature_row_review", "Positioning feature row is missing; rerun Stage 5 positioning import/build."
    return "current_missing_expected_review", "Current active ticker has no Form 4/direct ownership evidence; review CIK and SEC ownership applicability."


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    asof = args.asof.strip() or date.today().isoformat()
    model_family = str(
        args.model_family
        or cfg_get(config, "technology_universe.initial_subsector", "semiconductors")
        or "semiconductors"
    ).strip()
    if not model_family:
        raise ValueError("model_family cannot be empty")
    upstream_source = str(cfg_get(config, "positioning_import.form4_source_id", "sec_insider_upstream"))
    direct_source = str(cfg_get(config, "positioning_import.direct_ownership_source_id", "sec_ownership_direct"))
    feature_source = str(cfg_get(config, "positioning_import.source_id", "technology_positioning_composite"))
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(
                config,
                "positioning_import.form4_reconciliation_output_csv",
                "../output/technology_reports/positioning/form4_missing_ticker_reconciliation.csv",
            ),
            base_dir=base_dir,
        )
    )
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))) as conn:
        init_db(conn)
        companies = active_companies(conn, model_family=model_family, include_historical=bool(args.include_historical))
        profiles = profile_map(conn)
        features = feature_map(conn, source_id=feature_source, model_family=model_family, asof=asof)
        upstream_form4 = summary_map(
            conn,
            table="fact_sec_form4_transaction",
            ticker_col="ticker",
            date_col="transaction_date",
            source_id=upstream_source,
            code_filter="UPPER(COALESCE(transaction_code, '')) IN ('P', 'S')",
            date_max=asof,
        )
        direct_form4 = summary_map(
            conn,
            table="fact_sec_form4_transaction",
            ticker_col="ticker",
            date_col="transaction_date",
            source_id=direct_source,
            code_filter="UPPER(COALESCE(transaction_code, '')) IN ('P', 'S')",
            date_max=asof,
        )
        direct_filing_counts = count_map(
            conn,
            table="fact_sec_ownership_filing",
            ticker_col="ticker",
            source_id=direct_source,
        )
        direct_nonderiv_counts = count_map(
            conn,
            table="fact_sec_ownership_nonderivative_transaction",
            ticker_col="ticker",
            source_id=direct_source,
            code_filter="UPPER(COALESCE(transaction_code, '')) IN ('P', 'S')",
        )
        rows: list[dict[str, Any]] = []
        for company in companies:
            ticker = normalize_ticker(company["ticker"])
            profile = profiles.get(ticker, {})
            upstream = upstream_form4.get(ticker, Summary())
            direct = direct_form4.get(ticker, Summary())
            feature = features.get(ticker, {})
            direct_filing_count = int(direct_filing_counts.get(ticker, 0))
            direct_nonderiv_count = int(direct_nonderiv_counts.get(ticker, 0))
            status, next_step = classify(
                company=company,
                profile=profile,
                upstream=upstream,
                direct=direct,
                direct_filing_count=direct_filing_count,
                direct_nonderiv_count=direct_nonderiv_count,
                feature_rows=1 if feature else 0,
            )
            rows.append(
                {
                    "ticker": ticker,
                    "membership_scope": "current_active" if int(company.get("is_active") or 0) == 1 else "historical_inactive",
                    "is_active": int(company.get("is_active") or 0),
                    "universe_status": str(company.get("universe_status") or ""),
                    "company_name": str(company.get("company_name") or ""),
                    "cik": str(company.get("cik") or ""),
                    "exchange": str(company.get("exchange") or ""),
                    "security_type": str(company.get("security_type") or ""),
                    "country": str(company.get("country") or ""),
                    "section16_expected_status": str(profile.get("section16_expected_status") or ""),
                    "insider_coverage_status": str(profile.get("coverage_status") or ""),
                    "primary_insider_source": str(profile.get("primary_insider_source") or ""),
                    "fpi_qualifying_exemption_status": str(profile.get("fpi_qualifying_exemption_status") or ""),
                    "local_insider_source_required": int(profile.get("local_insider_source_required") or 0),
                    "latest_ownership_filing_date": str(profile.get("latest_ownership_filing_date") or ""),
                    "ownership_filing_count": int(profile.get("ownership_filing_count") or 0),
                    "ownership_transaction_count": int(profile.get("ownership_transaction_count") or 0),
                    "upstream_form4_count": upstream.count,
                    "upstream_form4_min_date": upstream.min_date,
                    "upstream_form4_max_date": upstream.max_date,
                    "direct_form4_count": direct.count,
                    "direct_form4_min_date": direct.min_date,
                    "direct_form4_max_date": direct.max_date,
                    "direct_ownership_filing_count": direct_filing_count,
                    "direct_ownership_nonderiv_count": direct_nonderiv_count,
                    "positioning_feature_rows": 1 if feature else 0,
                    "positioning_quality": str(feature.get("positioning_quality") or ""),
                    "reconciliation_status": status,
                    "recommended_next_step": next_step,
                }
            )
    write_csv(output_csv, rows)
    current_rows = [row for row in rows if row["membership_scope"] == "current_active"]
    current_review = [row for row in current_rows if str(row["reconciliation_status"]).startswith("current_missing")]
    print(
        f"form4_reconciliation={output_csv} rows={len(rows)} "
        f"current_active={len(current_rows)} current_missing_review={len(current_review)} asof={asof}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

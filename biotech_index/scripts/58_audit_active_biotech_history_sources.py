#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from biotech_index.core.security_identity import load_security_identity_rules  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output" / "biotech_new_ticker_intake_20260822"
PRICE_SOURCES = ("yahoo_adjusted", "interactive_brokers", "norgate_us_equities_total_return")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit PIT source coverage for governed active-biotech additions")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--market-positioning-db", type=Path, default=None)
    parser.add_argument("--form4-db", type=Path, default=None)
    parser.add_argument("--asof", default="")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--fail-on-core-gaps", action="store_true")
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%Y%m%d"):
        try:
            return datetime.strptime(text[:11].strip(), fmt).date()
        except ValueError:
            continue
    return None


def connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> Any:
    row = conn.execute(sql, params).fetchone()
    return None if row is None else row[0]


def date_span(
    conn: sqlite3.Connection,
    *,
    table: str,
    ticker: str,
    date_field: str,
    start: date,
    asof: date,
    source_filter: str = "",
    source_params: tuple[Any, ...] = (),
) -> tuple[int, str, str]:
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS n, MIN({date_field}) AS first_date, MAX({date_field}) AS last_date
        FROM {table}
        WHERE UPPER(ticker) = ?
          AND {date_field} >= ?
          AND {date_field} <= ?
          {source_filter}
        """,
        (ticker, start.isoformat(), asof.isoformat(), *source_params),
    ).fetchone()
    if row is None:
        return 0, "", ""
    return int(row["n"] or 0), str(row["first_date"] or ""), str(row["last_date"] or "")


def form4_counts(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    accepted_ciks: set[str],
    start: date,
    asof: date,
) -> tuple[int, int]:
    rows = conn.execute(
        """
        SELECT issuer_cik, issuer_trading_symbol, trans_date, filing_date
        FROM form4_events_tier1
        WHERE is_current_truth = 1
          AND COALESCE(trans_date, filing_date) <= ?
          AND (
                UPPER(issuer_trading_symbol) = ?
                OR LTRIM(issuer_cik, '0') IN ({})
              )
        """.format(",".join("?" for _ in accepted_ciks)),
        (asof.isoformat(), ticker, *(str(int(cik)) for cik in sorted(accepted_ciks))),
    ).fetchall()
    valid = 0
    wrong_identity = 0
    for row in rows:
        event_date = parse_date(row["trans_date"]) or parse_date(row["filing_date"])
        if event_date is None or event_date < start or event_date > asof:
            continue
        row_cik = str(row["issuer_cik"] or "").strip().zfill(10)
        if row_cik in accepted_ciks:
            valid += 1
        else:
            wrong_identity += 1
    return valid, wrong_identity


def main() -> None:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    asof = parse_date(args.asof) if args.asof else datetime.now(timezone.utc).date()
    if asof is None:
        raise ValueError(f"Invalid --asof date: {args.asof}")
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    market_db_path = (
        args.market_positioning_db.expanduser().resolve()
        if args.market_positioning_db
        else resolve_path(cfg_get(config, "market_positioning.database_path"), base_dir=base_dir)
    )
    form4_db_path = (
        args.form4_db.expanduser().resolve()
        if args.form4_db
        else resolve_path(cfg_get(config, "governance_events.form4_db_path"), base_dir=base_dir)
    )
    registry_path = resolve_path(
        cfg_get(config, "active_biotech_history.registry_csv", "data/active_biotech_historical_additions.csv"),
        base_dir=base_dir,
    )
    rules = load_security_identity_rules(registry_path)
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    with connect_readonly(db_path) as biotech, connect_readonly(market_db_path) as market, connect_readonly(form4_db_path) as form4:
        for ticker, rule in sorted(rules.items()):
            company = biotech.execute(
                "SELECT company_id, ticker, cik, company_name, is_active FROM companies WHERE UPPER(ticker) = ?",
                (ticker,),
            ).fetchone()
            company_id = int(company["company_id"]) if company is not None else 0
            accepted_ciks = {rule.cik, *rule.historical_ciks}
            price_count, price_first, price_last = date_span(
                biotech,
                table="market_bars_daily",
                ticker=rule.historical_price_ticker,
                date_field="bar_date",
                start=rule.membership_start_date,
                asof=asof,
                source_filter="AND source IN ({})".format(",".join("?" for _ in PRICE_SOURCES)),
                source_params=PRICE_SOURCES,
            )
            pre_identity_price_rows = int(
                scalar(
                    biotech,
                    "SELECT COUNT(*) FROM market_bars_daily WHERE UPPER(ticker) = ? AND bar_date < ? AND source IN ({})".format(
                        ",".join("?" for _ in PRICE_SOURCES)
                    ),
                    (rule.historical_price_ticker, rule.membership_start_date.isoformat(), *PRICE_SOURCES),
                )
                or 0
            )
            sec_filing_count = int(
                scalar(
                    biotech,
                    "SELECT COUNT(*) FROM sec_filings WHERE company_id = ? AND filing_date >= ? AND filing_date <= ?",
                    (company_id, rule.membership_start_date.isoformat(), asof.isoformat()),
                )
                or 0
            )
            sec_fact_count = int(
                scalar(
                    biotech,
                    "SELECT COUNT(*) FROM company_facts_quarterly WHERE company_id = ? AND period_end >= ? AND period_end <= ? AND (filed_date IS NULL OR filed_date <= ?)",
                    (company_id, rule.membership_start_date.isoformat(), asof.isoformat(), asof.isoformat()),
                )
                or 0
            )
            sec_event_count = int(
                scalar(
                    biotech,
                    "SELECT COUNT(*) FROM sec_events WHERE company_id = ? AND filing_date >= ? AND filing_date <= ?",
                    (company_id, rule.membership_start_date.isoformat(), asof.isoformat()),
                )
                or 0
            )
            ctgov_snapshot_count = int(
                scalar(
                    biotech,
                    """
                    SELECT COUNT(*)
                    FROM trial_snapshot_daily s
                    JOIN trial_company_links l ON l.nct_id = s.nct_id
                    WHERE l.company_id = ? AND s.asof_date >= ? AND s.asof_date <= ?
                    """,
                    (company_id, rule.membership_start_date.isoformat(), asof.isoformat()),
                )
                or 0
            )
            form4_count, form4_wrong_identity = form4_counts(
                form4,
                ticker=ticker,
                accepted_ciks=accepted_ciks,
                start=rule.membership_start_date,
                asof=asof,
            )
            thirteenf_count, thirteenf_first, thirteenf_last = date_span(
                market,
                table="institutional_13f_ownership_snapshots",
                ticker=ticker,
                date_field="asof_date",
                start=rule.membership_start_date,
                asof=asof,
            )
            short_count, short_first, short_last = date_span(
                market,
                table="short_interest_snapshots",
                ticker=ticker,
                date_field="asof_date",
                start=rule.membership_start_date,
                asof=asof,
            )
            borrow_count, borrow_first, borrow_last = date_span(
                market,
                table="ibkr_borrow_fee_rate_daily",
                ticker=ticker,
                date_field="asof_date",
                start=rule.membership_start_date,
                asof=asof,
            )
            core_gaps: list[str] = []
            if company is None:
                core_gaps.append("missing_company")
            if price_count <= 0:
                core_gaps.append("missing_price_in_identity_window")
            if sec_filing_count <= 0:
                core_gaps.append("missing_sec_filings_in_identity_window")
            if form4_wrong_identity > 0:
                core_gaps.append("form4_wrong_identity_match")
            optional_gaps = [
                name
                for name, count in (
                    ("companyfacts", sec_fact_count),
                    ("form4", form4_count),
                    ("13f", thirteenf_count),
                    ("short_interest", short_count),
                    ("borrow", borrow_count),
                    ("ctgov_pit_snapshot", ctgov_snapshot_count),
                )
                if count <= 0
            ]
            rows.append(
                {
                    "ticker": ticker,
                    "company_name": rule.company_name,
                    "company_id": company_id,
                    "current_cik": rule.cik,
                    "accepted_ciks": ";".join(sorted(accepted_ciks)),
                    "calibration_cohort": rule.calibration_cohort,
                    "membership_start_date": rule.membership_start_date.isoformat(),
                    "asof_date": asof.isoformat(),
                    "company_active_flag": int(company is not None and int(company["is_active"] or 0) > 0),
                    "price_row_count": price_count,
                    "price_first_date": price_first,
                    "price_last_date": price_last,
                    "pre_identity_price_rows_quarantined": pre_identity_price_rows,
                    "sec_filing_count": sec_filing_count,
                    "sec_fact_count": sec_fact_count,
                    "sec_event_count": sec_event_count,
                    "form4_event_count": form4_count,
                    "form4_wrong_identity_count": form4_wrong_identity,
                    "institutional_13f_snapshot_count": thirteenf_count,
                    "institutional_13f_first_date": thirteenf_first,
                    "institutional_13f_last_date": thirteenf_last,
                    "short_interest_snapshot_count": short_count,
                    "short_interest_first_date": short_first,
                    "short_interest_last_date": short_last,
                    "ibkr_borrow_row_count": borrow_count,
                    "ibkr_borrow_first_date": borrow_first,
                    "ibkr_borrow_last_date": borrow_last,
                    "ctgov_pit_snapshot_count": ctgov_snapshot_count,
                    "historical_panel_core_ready_flag": int(not core_gaps),
                    "core_gap_reasons": ";".join(core_gaps),
                    "optional_missing_sources": ";".join(optional_gaps),
                }
            )

    csv_path = output_dir / "active_biotech_history_source_audit.csv"
    json_path = output_dir / "active_biotech_history_source_audit.json"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        "asof_date": asof.isoformat(),
        "registry_path": str(registry_path),
        "ticker_count": len(rows),
        "core_ready_count": sum(int(row["historical_panel_core_ready_flag"]) for row in rows),
        "core_gap_tickers": [row["ticker"] for row in rows if not row["historical_panel_core_ready_flag"]],
        "coverage": {
            field: sum(int(row[field]) > 0 for row in rows)
            for field in (
                "price_row_count",
                "sec_filing_count",
                "sec_fact_count",
                "sec_event_count",
                "form4_event_count",
                "institutional_13f_snapshot_count",
                "short_interest_snapshot_count",
                "ibkr_borrow_row_count",
                "ctgov_pit_snapshot_count",
            )
        },
        "csv_path": str(csv_path),
    }
    json_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.fail_on_core_gaps and summary["core_gap_tickers"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()

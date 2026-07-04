#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import math
import re
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, expand_env_vars, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.source_registry import load_source_registry, upsert_source_registry  # noqa: E402
from industrials.core.text_norm import normalize_cik, normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("import_industrials_positioning")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
RUN_TYPE = "import_industrials_positioning"
CSV_FIELDS = [
    "ticker",
    "form4_transactions",
    "direct_form4_transactions",
    "form4_latest_transaction_date",
    "form4_status",
    "form4_status_reason",
    "institutional_rows",
    "short_interest_rows",
    "borrow_rows",
    "feature_status",
    "review_reason",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import Industrials positioning facts from read-only upstream databases.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--model-family", default="", help="Industrials model family to import, e.g. defense.")
    parser.add_argument("--asof", default="", help="Feature as-of date. Defaults to the latest market feature date.")
    parser.add_argument("--tickers", default="")
    parser.add_argument("--output-csv", type=Path, default=None)
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    candidates = [text, text[:10], text[:11]]
    if "T" in text:
        candidates.append(text.split("T", 1)[0])
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d-%b-%y"):
        for candidate in candidates:
            try:
                return datetime.strptime(candidate.upper(), fmt).date()
            except ValueError:
                continue
    return None


def safe_float(raw: object) -> float | None:
    if raw is None:
        return None
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def ro_connect(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def cfg_bool(config: dict[str, Any], key: str, default: bool = False) -> bool:
    raw = cfg_get(config, key, default)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "y"}


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def load_positioning_overrides(config: dict[str, Any], *, base_dir: Path) -> dict[str, dict[str, str]]:
    path_value = cfg_get(config, "positioning_import.positioning_overrides_csv", "")
    if not path_value:
        return {}
    path = resolve_path(path_value, base_dir=base_dir)
    overrides: dict[str, dict[str, str]] = {}
    for row in read_csv_rows(path):
        ticker = normalize_ticker(row.get("ticker"))
        if not ticker:
            continue
        overrides[ticker] = {str(key): str(value or "").strip() for key, value in row.items()}
    return overrides


def truthy(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "y"}


def cfg_ticker_set(raw: Any) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, str):
        values = raw.split(",")
    elif isinstance(raw, (list, tuple, set)):
        values = raw
    else:
        values = []
    return {ticker for ticker in (normalize_ticker(value) for value in values) if ticker}


def cfg_ticker_list(raw: Any) -> list[str]:
    return sorted(cfg_ticker_set(raw))


def institutional_13f_gate_config(config: dict[str, Any]) -> tuple[date | None, list[str], int]:
    gate = cfg_get(config, "positioning_import.institutional_13f_data_gate", {}) or {}
    required_period = parse_date(gate.get("required_period_of_report"))
    anchor_tickers = cfg_ticker_list(gate.get("anchor_tickers", []))
    min_anchor_count = int(gate.get("min_anchor_tickers_with_period", len(anchor_tickers) if anchor_tickers else 0) or 0)
    return required_period, anchor_tickers, min_anchor_count


def institutional_13f_period_available(
    conn: sqlite3.Connection,
    *,
    required_period: date | None,
    anchor_tickers: list[str],
    min_anchor_count: int,
    table_name: str,
    source_id: str = "",
) -> bool:
    if required_period is None:
        return True
    where_source = "AND source = ?" if table_name == "institutional_13f_ownership_snapshots" and source_id else ""
    params: list[Any] = [*([source_id] if where_source else [])]
    max_period_row = conn.execute(
        f"""
        SELECT MAX(period_of_report) AS max_period
        FROM {table_name}
        WHERE COALESCE(period_of_report, '') <> ''
        {where_source}
        """,
        params,
    ).fetchone()
    max_period = parse_date(max_period_row["max_period"] if max_period_row is not None else "")
    if max_period is None or max_period < required_period:
        return False
    if not anchor_tickers or min_anchor_count <= 0:
        return True
    anchors = sorted(set(anchor_tickers))
    placeholders = ",".join("?" for _ in anchors)
    if table_name == "institutional_13f_ownership_snapshots":
        sql = f"""
            SELECT COUNT(DISTINCT ticker) AS covered
            FROM institutional_13f_ownership_snapshots
            WHERE ticker IN ({placeholders})
              AND period_of_report >= ?
              {where_source}
        """
        row = conn.execute(sql, (*anchors, required_period.isoformat(), *([source_id] if where_source else []))).fetchone()
    else:
        sql = f"""
            SELECT COUNT(DISTINCT ticker) AS covered
            FROM {table_name}
            WHERE ticker IN ({placeholders})
              AND period_of_report >= ?
        """
        row = conn.execute(sql, (*anchors, required_period.isoformat())).fetchone()
    covered = int(row["covered"] or 0) if row is not None else 0
    return covered >= min_anchor_count


def upstream_institutional_13f_period_available(config: dict[str, Any], mp_db: Path) -> bool:
    required_period, anchor_tickers, min_anchor_count = institutional_13f_gate_config(config)
    with closing(ro_connect(mp_db)) as conn:
        available = institutional_13f_period_available(
            conn,
            required_period=required_period,
            anchor_tickers=anchor_tickers,
            min_anchor_count=min_anchor_count,
            table_name="institutional_13f_ownership_snapshots",
            source_id="sec_13f_data_sets",
        )
    LOGGER.info(
        "13F DERA availability gate: required_period=%s anchors=%s min_anchor=%d available=%s",
        required_period.isoformat() if required_period else "",
        anchor_tickers,
        min_anchor_count,
        available,
    )
    return available


def exemption_active(row: dict[str, str], flag_key: str, *, until_key: str = "", policy_date: date | None = None) -> bool:
    if not truthy(row.get(flag_key)):
        return False
    if not until_key:
        return True
    until = parse_date(row.get(until_key))
    return until is None or (policy_date or date.today()) <= until


def reporting_owner_key(raw_cik: object, raw_name: object) -> str:
    """Collision-safe reporting-owner key for the Form 4 PK.

    Joint filings occasionally omit an owner CIK; collapsing them all to '' would
    merge distinct owners on ON CONFLICT and undercount cluster buyers. Fall back
    to a normalized-name surrogate so each blank-CIK owner keeps its own row.
    """
    cik = normalize_cik(raw_cik)
    if cik:
        return cik
    name = re.sub(r"[^A-Z0-9]+", "_", str(raw_name or "").strip().upper()).strip("_")
    return f"NAME:{name}" if name else "UNKNOWN_OWNER"


def load_universe(conn: Any, ticker_filter: set[str], *, model_family: str, include_historical: bool = False) -> list[str]:
    if include_historical:
        # Historical mode covers every membership row (current + delisted internal
        # tickers). The previous (is_current_member OR point_in_time_flag) filter was
        # a tautology because point_in_time_flag defaults to 1 on every row.
        rows = conn.execute(
            """
            SELECT DISTINCT ticker
            FROM dim_universe_membership
            WHERE model_family = ?
            ORDER BY ticker
            """,
            (model_family,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT DISTINCT c.ticker
            FROM dim_company c
            JOIN dim_industrials_taxonomy t
              ON t.ticker = c.ticker
             AND t.model_family = ?
            WHERE c.is_active = 1
            ORDER BY c.ticker
            """,
            (model_family,),
        ).fetchall()
    tickers = [normalize_ticker(row["ticker"]) for row in rows if normalize_ticker(row["ticker"])]
    return [ticker for ticker in tickers if not ticker_filter or ticker in ticker_filter]


def qmarks(values: list[str]) -> str:
    if not values:
        raise ValueError("values cannot be empty")
    return ",".join("?" for _ in values)


def load_source_ticker_map(conn: Any, internal_tickers: list[str]) -> tuple[list[str], dict[str, str], set[str]]:
    """Return source tickers to query and a source->internal ticker map."""
    source_to_internal = {ticker: ticker for ticker in internal_tickers}
    query_tickers = set(internal_tickers)
    ambiguous_sources: set[str] = set()
    if not internal_tickers:
        return [], source_to_internal, ambiguous_sources
    rows = conn.execute(
        f"""
        SELECT c.ticker AS internal_ticker, i.identifier_value AS source_ticker
        FROM dim_company c
        JOIN dim_identifier i ON i.company_id = c.company_id
        WHERE c.ticker IN ({qmarks(internal_tickers)})
          AND i.identifier_type = 'EXCHANGE_TICKER'
        """,
        internal_tickers,
    ).fetchall()
    source_groups: dict[str, set[str]] = {}
    for row in rows:
        internal = normalize_ticker(row["internal_ticker"])
        source = normalize_ticker(row["source_ticker"])
        if internal and source:
            source_groups.setdefault(source, set()).add(internal)
    for source, internals in sorted(source_groups.items()):
        query_tickers.add(source)
        existing = source_to_internal.get(source)
        if len(internals) > 1 or (existing and existing not in internals):
            source_to_internal.pop(source, None)
            ambiguous_sources.add(source)
            LOGGER.warning(
                "Skipping ambiguous source ticker mapping: source=%s internals=%s existing=%s",
                source,
                sorted(internals),
                existing or "",
            )
            continue
        source_to_internal[source] = next(iter(internals))
    return sorted(query_tickers), source_to_internal, ambiguous_sources


def apply_positioning_source_overrides(
    *,
    internal_tickers: list[str],
    overrides: dict[str, dict[str, str]],
    query_tickers: list[str],
    source_to_internal: dict[str, str],
    ambiguous_source_tickers: set[str],
    policy_date: date | None = None,
    institutional_13f_data_available: bool = True,
) -> tuple[list[str], dict[str, str], set[str], set[str], set[str], set[str]]:
    query_set = set(query_tickers)
    short_exempt_tickers: set[str] = set()
    institutional_13f_exempt_tickers: set[str] = set()
    short_pct_float_exempt_tickers: set[str] = set()
    internal_set = set(internal_tickers)
    for internal_ticker, row in overrides.items():
        if internal_ticker not in internal_set:
            continue
        source_ticker = normalize_ticker(row.get("source_ticker"))
        if source_ticker:
            query_set.add(source_ticker)
            source_to_internal[source_ticker] = internal_ticker
            ambiguous_source_tickers.discard(source_ticker)
        if exemption_active(row, "short_interest_exempt", policy_date=policy_date):
            short_exempt_tickers.add(internal_ticker)
        if not institutional_13f_data_available and truthy(row.get("institutional_13f_exempt")):
            institutional_13f_exempt_tickers.add(internal_ticker)
        elif exemption_active(
            row,
            "institutional_13f_exempt",
            until_key="institutional_13f_exempt_until",
            policy_date=policy_date,
        ):
            institutional_13f_exempt_tickers.add(internal_ticker)
        if exemption_active(row, "short_pct_float_exempt", policy_date=policy_date):
            short_pct_float_exempt_tickers.add(internal_ticker)
    return (
        sorted(query_set),
        source_to_internal,
        ambiguous_source_tickers,
        short_exempt_tickers,
        institutional_13f_exempt_tickers,
        short_pct_float_exempt_tickers,
    )


Form4Route = tuple[str, re.Pattern[str] | None, re.Pattern[str] | None]


def compile_regex(raw: str, *, ticker: str, field: str) -> re.Pattern[str] | None:
    pattern = str(raw or "").strip()
    if not pattern:
        return None
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"Invalid {field} regex for {ticker}: {pattern!r}") from exc


def load_form4_override_policy(
    *,
    internal_tickers: list[str],
    overrides: dict[str, dict[str, str]],
) -> tuple[set[str], dict[str, str], set[str], dict[str, str], dict[str, list[Form4Route]]]:
    internal_set = set(internal_tickers)
    form4_exempt_tickers: set[str] = set()
    form4_exempt_reasons: dict[str, str] = {}
    forced_query_ciks: set[str] = set()
    forced_cik_to_internal: dict[str, str] = {}
    routes_by_cik: dict[str, list[Form4Route]] = {}
    for ticker, row in overrides.items():
        if ticker not in internal_set:
            continue
        if exemption_active(row, "form4_exempt"):
            form4_exempt_tickers.add(ticker)
            form4_exempt_reasons[ticker] = str(row.get("form4_exemption_reason") or "FORM4_POLICY_EXEMPT").strip()
        cik = normalize_cik(row.get("form4_cik"))
        if not cik:
            continue
        forced_query_ciks.add(cik)
        include = compile_regex(str(row.get("form4_security_title_regex") or ""), ticker=ticker, field="form4_security_title_regex")
        exclude = compile_regex(
            str(row.get("form4_security_title_exclude_regex") or ""),
            ticker=ticker,
            field="form4_security_title_exclude_regex",
        )
        if include is not None or exclude is not None:
            routes_by_cik.setdefault(cik, []).append((ticker, include, exclude))
        else:
            forced_cik_to_internal[cik] = ticker
    return form4_exempt_tickers, form4_exempt_reasons, forced_query_ciks, forced_cik_to_internal, routes_by_cik


def form4_status_for_ticker(
    ticker: str,
    *,
    form4_rows: int,
    direct_rows: int,
    form4_exempt_tickers: set[str],
    form4_exempt_reasons: dict[str, str],
) -> tuple[str, str]:
    if ticker in form4_exempt_tickers:
        return "not_applicable", form4_exempt_reasons.get(ticker) or "FORM4_POLICY_EXEMPT"
    if form4_rows + direct_rows > 0:
        return "covered", ""
    return "missing", "NO_FORM4_TRANSACTIONS"


def route_form4_ticker(
    *,
    source_cik: str,
    source_ticker: str,
    security_title: str,
    source_to_internal: dict[str, str],
    cik_to_internal: dict[str, str],
    routes_by_cik: dict[str, list[Form4Route]],
) -> str:
    if source_cik in routes_by_cik:
        for ticker, include, exclude in routes_by_cik[source_cik]:
            if include is not None and include.search(security_title) is None:
                continue
            if exclude is not None and exclude.search(security_title) is not None:
                continue
            return ticker
        return ""
    return cik_to_internal.get(source_cik) or source_to_internal.get(source_ticker) or source_ticker


def load_unique_cik_map(conn: Any, internal_tickers: list[str]) -> tuple[list[str], dict[str, str]]:
    """Return SEC CIKs that map to exactly one requested internal ticker."""
    if not internal_tickers:
        return [], {}
    rows = conn.execute(
        f"""
        SELECT ticker, cik
        FROM dim_company
        WHERE ticker IN ({qmarks(internal_tickers)})
          AND COALESCE(cik, '') <> ''
        """,
        internal_tickers,
    ).fetchall()
    groups: dict[str, set[str]] = {}
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        cik = normalize_cik(row["cik"])
        if ticker and cik:
            groups.setdefault(cik, set()).add(ticker)
    cik_to_internal: dict[str, str] = {}
    for cik, tickers in sorted(groups.items()):
        if len(tickers) > 1:
            LOGGER.warning("Skipping ambiguous CIK mapping: cik=%s tickers=%s", cik, sorted(tickers))
            continue
        cik_to_internal[cik] = next(iter(tickers))
    return sorted(cik_to_internal), cik_to_internal


def latest_market_feature_asof(conn: Any, model_family: str) -> date | None:
    row = conn.execute(
        "SELECT MAX(asof_date) AS asof_date FROM feature_market_technical WHERE model_family = ?",
        (model_family,),
    ).fetchone()
    return parse_date(row["asof_date"] if row is not None else "")


def add_issue(conn: Any, ticker: str, source_id: str, issue_type: str, detail: str, severity: str = "warning") -> None:
    now = utc_now()
    row = conn.execute("SELECT company_id FROM dim_company WHERE ticker = ?", (ticker,)).fetchone()
    company_id = int(row["company_id"]) if row is not None else None
    conn.execute(
        """
        INSERT INTO data_quality_issues(
            detected_at, severity, stage, ticker, company_id, source_id, issue_type,
            issue_detail, resolution_status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
        """,
        (now, severity, RUN_TYPE, ticker, company_id, source_id, issue_type, detail, now, now),
    )


def import_form4(
    dest: Any,
    source: sqlite3.Connection,
    tickers: list[str],
    *,
    query_tickers: list[str],
    query_ciks: list[str],
    source_to_internal: dict[str, str],
    cik_to_internal: dict[str, str],
    routes_by_cik: dict[str, list[Form4Route]],
    source_id: str,
    start: date,
) -> dict[str, dict[str, Any]]:
    stats = {ticker: {"form4_transactions": 0, "form4_latest_transaction_date": ""} for ticker in tickers}
    rows = source.execute(
        f"""
        SELECT
            UPPER(s.issuer_trading_symbol) AS ticker,
            s.issuer_cik,
            s.accession_number,
            s.filing_date,
            s.period_of_report,
            t.nonderiv_trans_sk,
            t.security_title,
            t.transaction_date,
            t.transaction_code,
            t.transaction_shares,
            t.transaction_price_per_share,
            t.transaction_acquired_disposed_code,
            t.shares_owned_following_transaction,
            t.direct_or_indirect_ownership,
            ro.rptowner_cik,
            ro.rptowner_name,
            ro.rptowner_relationship,
            ro.rptowner_title,
            ro.is_director,
            ro.is_officer,
            ro.is_ten_percent_owner
        FROM sec_ownership_submission s
        JOIN sec_ownership_nonderiv_trans t
          ON t.accession_number = s.accession_number
        LEFT JOIN sec_ownership_reporting_owner ro
          ON ro.accession_number = s.accession_number
        WHERE UPPER(s.issuer_trading_symbol) IN ({qmarks(query_tickers)})
           OR printf('%010d', CAST(s.issuer_cik AS INTEGER)) IN ({qmarks(query_ciks)})
        """,
        (*query_tickers, *query_ciks),
    )
    now = utc_now()
    for row in rows:
        source_ticker = normalize_ticker(row["ticker"])
        source_cik = normalize_cik(row["issuer_cik"])
        ticker = route_form4_ticker(
            source_cik=source_cik,
            source_ticker=source_ticker,
            security_title=str(row["security_title"] or ""),
            source_to_internal=source_to_internal,
            cik_to_internal=cik_to_internal,
            routes_by_cik=routes_by_cik,
        )
        if ticker not in stats:
            continue
        trans_date = parse_date(row["transaction_date"]) or parse_date(row["period_of_report"]) or parse_date(row["filing_date"])
        filing_date = parse_date(row["filing_date"])
        period_date = parse_date(row["period_of_report"])
        if trans_date is None or trans_date < start:
            continue
        code = str(row["transaction_code"] or "").strip().upper()
        acq_disp = str(row["transaction_acquired_disposed_code"] or "").strip().upper()
        shares = safe_float(row["transaction_shares"])
        price = safe_float(row["transaction_price_per_share"])
        value = shares * price if shares is not None and price is not None else None
        dest.execute(
            """
            INSERT INTO fact_sec_form4_transaction(
                ticker, accession_number, nonderiv_trans_sk, rptowner_cik, source_id,
                filing_date, period_of_report, transaction_date, transaction_code,
                acquired_disposed_code, transaction_shares, transaction_price_per_share,
                transaction_value, shares_owned_following_transaction,
                direct_or_indirect_ownership, reporting_owner_name,
                reporting_owner_relationship, reporting_owner_title, is_director,
                is_officer, is_ten_percent_owner, is_open_market_purchase,
                is_open_market_sale, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, accession_number, nonderiv_trans_sk, rptowner_cik, source_id) DO UPDATE SET
                filing_date = excluded.filing_date,
                period_of_report = excluded.period_of_report,
                transaction_date = excluded.transaction_date,
                transaction_code = excluded.transaction_code,
                acquired_disposed_code = excluded.acquired_disposed_code,
                transaction_shares = excluded.transaction_shares,
                transaction_price_per_share = excluded.transaction_price_per_share,
                transaction_value = excluded.transaction_value,
                shares_owned_following_transaction = excluded.shares_owned_following_transaction,
                reporting_owner_name = excluded.reporting_owner_name,
                reporting_owner_relationship = excluded.reporting_owner_relationship,
                reporting_owner_title = excluded.reporting_owner_title,
                is_director = excluded.is_director,
                is_officer = excluded.is_officer,
                is_ten_percent_owner = excluded.is_ten_percent_owner,
                is_open_market_purchase = excluded.is_open_market_purchase,
                is_open_market_sale = excluded.is_open_market_sale,
                updated_at = excluded.updated_at
            """,
            (
                ticker,
                str(row["accession_number"] or ""),
                str(row["nonderiv_trans_sk"] or ""),
                reporting_owner_key(row["rptowner_cik"], row["rptowner_name"]),
                source_id,
                filing_date.isoformat() if filing_date else "",
                period_date.isoformat() if period_date else "",
                trans_date.isoformat(),
                code,
                acq_disp,
                shares,
                price,
                value,
                safe_float(row["shares_owned_following_transaction"]),
                str(row["direct_or_indirect_ownership"] or ""),
                str(row["rptowner_name"] or ""),
                str(row["rptowner_relationship"] or ""),
                str(row["rptowner_title"] or ""),
                int(row["is_director"] or 0),
                int(row["is_officer"] or 0),
                int(row["is_ten_percent_owner"] or 0),
                int(code == "P" and acq_disp in {"", "A"}),
                int(code == "S" and acq_disp in {"", "D"}),
                now,
                now,
            ),
        )
        stats[ticker]["form4_transactions"] += 1
        latest = stats[ticker]["form4_latest_transaction_date"]
        if not latest or trans_date.isoformat() > latest:
            stats[ticker]["form4_latest_transaction_date"] = trans_date.isoformat()
    return stats


def import_13f(
    dest: Any,
    source: sqlite3.Connection,
    tickers: list[str],
    *,
    query_tickers: list[str],
    source_to_internal: dict[str, str],
    ambiguous_source_tickers: set[str],
    source_id: str,
    start: date,
) -> dict[str, int]:
    stats = {ticker: 0 for ticker in tickers}
    now = utc_now()
    # Full replace: upstream snapshots are period-level aggregates, so any stale
    # legacy filing-day-slice rows must not survive alongside them.
    dest.execute(
        f"DELETE FROM fact_13f_positioning WHERE source_id = ? AND ticker IN ({qmarks(tickers)})",
        (source_id, *tickers),
    )
    rows = source.execute(
        f"""
        SELECT * FROM institutional_13f_ownership_snapshots
        WHERE UPPER(ticker) IN ({qmarks(query_tickers)})
        """,
        query_tickers,
    )
    for row in rows:
        source_ticker = normalize_ticker(row["ticker"])
        if source_ticker in ambiguous_source_tickers:
            continue
        ticker = source_to_internal.get(source_ticker, source_ticker)
        if ticker not in stats:
            continue
        asof = parse_date(row["asof_date"])
        if asof is None or asof < start:
            continue
        dest.execute(
            """
            INSERT INTO fact_13f_positioning(
                ticker, asof_date, period_of_report, source_id, institutional_shares,
                institutional_value, manager_count, institutional_ownership_delta_pct,
                new_buyer_count, exiting_holder_count, net_buyer_count, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, asof_date, source_id) DO UPDATE SET
                period_of_report = excluded.period_of_report,
                institutional_shares = excluded.institutional_shares,
                institutional_value = excluded.institutional_value,
                manager_count = excluded.manager_count,
                institutional_ownership_delta_pct = excluded.institutional_ownership_delta_pct,
                new_buyer_count = excluded.new_buyer_count,
                exiting_holder_count = excluded.exiting_holder_count,
                net_buyer_count = excluded.net_buyer_count,
                updated_at = excluded.updated_at
            """,
            (
                ticker,
                asof.isoformat(),
                str(row["period_of_report"] or ""),
                source_id,
                safe_float(row["institutional_shares"]),
                safe_float(row["institutional_value"]),
                int(row["manager_count"] or 0),
                safe_float(row["institutional_ownership_delta_pct"]),
                int(row["new_buyer_count"] or 0),
                int(row["exiting_holder_count"] or 0),
                int(row["net_buyer_count"] or 0),
                now,
                now,
            ),
        )
        stats[ticker] += 1
    return stats


def import_short_interest(
    dest: Any,
    source: sqlite3.Connection,
    tickers: list[str],
    *,
    query_tickers: list[str],
    source_to_internal: dict[str, str],
    ambiguous_source_tickers: set[str],
    source_id: str,
    start: date,
) -> dict[str, int]:
    stats = {ticker: 0 for ticker in tickers}
    now = utc_now()
    rows = source.execute(
        f"SELECT * FROM short_interest_snapshots WHERE UPPER(ticker) IN ({qmarks(query_tickers)})",
        query_tickers,
    )
    for row in rows:
        source_ticker = normalize_ticker(row["ticker"])
        if source_ticker in ambiguous_source_tickers:
            continue
        ticker = source_to_internal.get(source_ticker, source_ticker)
        if ticker not in stats:
            continue
        settlement = parse_date(row["settlement_date"]) or parse_date(row["asof_date"])
        asof = parse_date(row["asof_date"])
        publication = parse_date(row["publication_date"])
        if settlement is None or settlement < start:
            continue
        dest.execute(
            """
            INSERT INTO fact_short_interest(
                ticker, settlement_date, source_id, asof_date, publication_date,
                short_interest_shares, float_shares, short_interest_pct_float,
                days_to_cover, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, settlement_date, source_id) DO UPDATE SET
                asof_date = excluded.asof_date,
                publication_date = excluded.publication_date,
                short_interest_shares = excluded.short_interest_shares,
                float_shares = excluded.float_shares,
                short_interest_pct_float = excluded.short_interest_pct_float,
                days_to_cover = excluded.days_to_cover,
                updated_at = excluded.updated_at
            """,
            (
                ticker,
                settlement.isoformat(),
                source_id,
                asof.isoformat() if asof else settlement.isoformat(),
                publication.isoformat() if publication else "",
                safe_float(row["short_interest_shares"]),
                safe_float(row["float_shares"]),
                safe_float(row["short_interest_pct_float"]),
                safe_float(row["days_to_cover"]),
                now,
                now,
            ),
        )
        stats[ticker] += 1
    return stats


def import_borrow(
    dest: Any,
    source: sqlite3.Connection,
    tickers: list[str],
    *,
    query_tickers: list[str],
    source_to_internal: dict[str, str],
    ambiguous_source_tickers: set[str],
    source_id: str,
    start: date,
) -> dict[str, int]:
    stats = {ticker: 0 for ticker in tickers}
    now = utc_now()
    rows = source.execute(
        f"SELECT * FROM ibkr_borrow_fee_rate_daily WHERE UPPER(ticker) IN ({qmarks(query_tickers)})",
        query_tickers,
    )
    for row in rows:
        source_ticker = normalize_ticker(row["ticker"])
        if source_ticker in ambiguous_source_tickers:
            continue
        ticker = source_to_internal.get(source_ticker, source_ticker)
        if ticker not in stats:
            continue
        asof = parse_date(row["asof_date"])
        if asof is None or asof < start:
            continue
        dest.execute(
            """
            INSERT INTO fact_ibkr_borrow_snapshot(
                ticker, asof_date, source_id, con_id, borrow_fee_rate, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, asof_date, source_id) DO UPDATE SET
                con_id = excluded.con_id,
                borrow_fee_rate = excluded.borrow_fee_rate,
                updated_at = excluded.updated_at
            """,
            (ticker, asof.isoformat(), source_id, str(row["con_id"] or ""), safe_float(row["borrow_fee_rate"]), now, now),
        )
        stats[ticker] += 1
    return stats


def direct_form4_stats(conn: Any, tickers: list[str], *, source_id: str) -> dict[str, dict[str, Any]]:
    stats = {ticker: {"direct_form4_transactions": 0, "direct_form4_latest_transaction_date": ""} for ticker in tickers}
    if not tickers:
        return stats
    rows = conn.execute(
        f"""
        SELECT ticker, COUNT(*) AS n, MAX(transaction_date) AS latest_date
        FROM fact_sec_form4_transaction
        WHERE source_id = ? AND ticker IN ({qmarks(tickers)})
        GROUP BY ticker
        """,
        (source_id, *tickers),
    ).fetchall()
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        stats[ticker] = {
            "direct_form4_transactions": int(row["n"] or 0),
            "direct_form4_latest_transaction_date": str(row["latest_date"] or ""),
        }
    return stats


def latest_row(conn: Any, table: str, ticker: str, date_col: str, asof_iso: str) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT * FROM {table} WHERE ticker = ? AND {date_col} <= ? ORDER BY {date_col} DESC LIMIT 1",
        (ticker, asof_iso),
    ).fetchone()


def latest_short_row(conn: Any, ticker: str, asof_iso: str) -> sqlite3.Row | None:
    # Point-in-time: FINRA short interest is only known once published; when
    # publication_date is missing, assume the typical settlement+14d lag.
    return conn.execute(
        """
        SELECT * FROM fact_short_interest
        WHERE ticker = ? AND settlement_date <= ?
          AND (
              (COALESCE(publication_date, '') <> '' AND publication_date <= ?)
              OR (COALESCE(publication_date, '') = '' AND DATE(settlement_date, '+14 day') <= ?)
          )
        ORDER BY settlement_date DESC
        LIMIT 1
        """,
        (ticker, asof_iso, asof_iso, asof_iso),
    ).fetchone()


def preferred_form4_source(conn: Any, ticker: str, *, window_start: str, asof_iso: str, direct_source: str, upstream_source: str) -> str:
    """The same Form 4 can exist under both source feeds; aggregate exactly one."""
    rows = conn.execute(
        """
        SELECT source_id, COUNT(*) AS n
        FROM fact_sec_form4_transaction
        WHERE ticker = ? AND source_id IN (?, ?)
          AND COALESCE(NULLIF(filing_date, ''), transaction_date) BETWEEN ? AND ?
        GROUP BY source_id
        """,
        (ticker, direct_source, upstream_source, window_start, asof_iso),
    ).fetchall()
    counts = {str(row["source_id"]): int(row["n"] or 0) for row in rows}
    return direct_source if counts.get(direct_source, 0) > 0 else upstream_source


def build_positioning_features(
    conn: Any,
    tickers: list[str],
    *,
    asof: date,
    feature_source_id: str,
    model_family: str,
    insider_days: int,
    short_change_days: int,
    direct_source: str,
    upstream_source: str,
    require_13f: bool,
    require_short: bool,
    require_short_pct_float: bool,
    require_borrow: bool,
    short_exempt_tickers: set[str],
    institutional_13f_exempt_tickers: set[str],
    short_pct_float_exempt_tickers: set[str],
    form4_status_by_ticker: dict[str, str],
    form4_status_reason_by_ticker: dict[str, str],
) -> dict[str, str]:
    now = utc_now()
    statuses: dict[str, str] = {}
    insider_start = (asof - timedelta(days=insider_days)).isoformat()
    short_prior_cutoff = (asof - timedelta(days=short_change_days)).isoformat()
    for ticker in tickers:
        insider_source = preferred_form4_source(
            conn,
            ticker,
            window_start=insider_start,
            asof_iso=asof.isoformat(),
            direct_source=direct_source,
            upstream_source=upstream_source,
        )
        # Rows are stored per reporting owner, so a joint filing repeats the same
        # economic transaction; dedupe on (accession_number, nonderiv_trans_sk) for
        # counts/values while keeping owners (cluster buyers) from the raw rows.
        purchase = conn.execute(
            """
            SELECT COUNT(*) AS n, COALESCE(SUM(transaction_value), 0) AS v,
                   (
                       SELECT COUNT(DISTINCT rptowner_cik)
                       FROM fact_sec_form4_transaction
                       WHERE ticker = ? AND source_id = ?
                         AND COALESCE(NULLIF(filing_date, ''), transaction_date) BETWEEN ? AND ?
                         AND is_open_market_purchase = 1
                   ) AS owners
            FROM (
                SELECT accession_number, nonderiv_trans_sk, MAX(transaction_value) AS transaction_value
                FROM fact_sec_form4_transaction
                WHERE ticker = ? AND source_id = ?
                  AND COALESCE(NULLIF(filing_date, ''), transaction_date) BETWEEN ? AND ?
                  AND is_open_market_purchase = 1
                GROUP BY accession_number, nonderiv_trans_sk
            )
            """,
            (
                ticker, insider_source, insider_start, asof.isoformat(),
                ticker, insider_source, insider_start, asof.isoformat(),
            ),
        ).fetchone()
        sale = conn.execute(
            """
            SELECT COUNT(*) AS n, COALESCE(SUM(transaction_value), 0) AS v
            FROM (
                SELECT accession_number, nonderiv_trans_sk, MAX(transaction_value) AS transaction_value
                FROM fact_sec_form4_transaction
                WHERE ticker = ? AND source_id = ?
                  AND COALESCE(NULLIF(filing_date, ''), transaction_date) BETWEEN ? AND ?
                  AND is_open_market_sale = 1
                GROUP BY accession_number, nonderiv_trans_sk
            )
            """,
            (ticker, insider_source, insider_start, asof.isoformat()),
        ).fetchone()
        inst = latest_row(conn, "fact_13f_positioning", ticker, "asof_date", asof.isoformat())
        short = latest_short_row(conn, ticker, asof.isoformat())
        borrow = latest_row(conn, "fact_ibkr_borrow_snapshot", ticker, "asof_date", asof.isoformat())
        short_pct_float = safe_float(short["short_interest_pct_float"]) if short is not None else None
        short_change = None
        if short is not None:
            prior = conn.execute(
                """
                SELECT short_interest_pct_float, short_interest_shares, float_shares
                FROM fact_short_interest
                WHERE ticker = ? AND settlement_date <= ?
                  AND (
                      (COALESCE(publication_date, '') <> '' AND publication_date <= ?)
                      OR (COALESCE(publication_date, '') = '' AND DATE(settlement_date, '+14 day') <= ?)
                  )
                ORDER BY settlement_date DESC
                LIMIT 1
                """,
                (ticker, short_prior_cutoff, asof.isoformat(), asof.isoformat()),
            ).fetchone()
            # Change in percent-of-float, so the signal is comparable across companies.
            latest_pct = safe_float(short["short_interest_pct_float"])
            prior_pct = safe_float(prior["short_interest_pct_float"]) if prior is not None else None
            if latest_pct is not None and prior_pct is not None:
                short_change = latest_pct - prior_pct
            else:
                latest_shares = safe_float(short["short_interest_shares"])
                prior_shares = safe_float(prior["short_interest_shares"]) if prior is not None else None
                float_shares = safe_float(short["float_shares"])
                if latest_shares is not None and prior_shares is not None and float_shares and float_shares > 0:
                    short_change = (latest_shares - prior_shares) / float_shares
                elif latest_shares is not None and prior_shares is not None and prior_shares > 0:
                    # The free FINRA feed carries no float, so fall back to the
                    # relative change in short-interest shares. Same sign and
                    # cross-sectionally comparable, just not float-scaled.
                    short_change = (latest_shares - prior_shares) / prior_shares
        reasons: list[str] = []
        waived_reasons: list[str] = []
        institutional_13f_exempt = ticker in institutional_13f_exempt_tickers
        if require_13f and inst is None:
            if institutional_13f_exempt:
                waived_reasons.append("institutional_13f_policy_exempt")
            else:
                reasons.append("missing_13f")
        short_exempt = ticker in short_exempt_tickers
        short_pct_float_exempt = ticker in short_pct_float_exempt_tickers
        if require_short and short is None and not short_exempt:
            reasons.append("missing_short_interest")
        elif require_short and require_short_pct_float and short_pct_float is None and not short_exempt:
            # Percent-of-float needs a float-shares source the default FINRA feed
            # does not provide; only gate on it when the config says the feed can.
            if short_pct_float_exempt:
                waived_reasons.append("short_pct_float_policy_exempt")
            else:
                reasons.append("missing_short_interest_pct_float")
        if require_borrow and borrow is None:
            reasons.append("missing_borrow")
        quality = "complete" if not reasons and not waived_reasons else ("policy_exempt" if not reasons else "review")
        form4_status = form4_status_by_ticker.get(ticker) or "missing"
        form4_status_reason = form4_status_reason_by_ticker.get(ticker) or (
            "NO_FORM4_TRANSACTIONS" if form4_status == "missing" else ""
        )
        purchase_value = safe_float(purchase["v"])
        sale_value = safe_float(sale["v"])
        insider_net_value = purchase_value - sale_value if purchase_value is not None and sale_value is not None else None
        conn.execute(
            """
            INSERT INTO feature_positioning(
                ticker, asof_date, source_id, model_family, insider_purchase_count_90d,
                insider_purchase_value_90d, insider_sale_count_90d, insider_sale_value_90d,
                insider_cluster_buyers_90d, insider_net_value_90d, latest_institutional_shares,
                latest_institutional_value, latest_manager_count, institutional_ownership_delta_pct,
                latest_short_interest_shares, latest_short_interest_pct_float, latest_days_to_cover,
                short_interest_change_3m, latest_borrow_fee_rate, form4_status, form4_status_reason,
                positioning_quality, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, asof_date, source_id, model_family) DO UPDATE SET
                insider_purchase_count_90d = excluded.insider_purchase_count_90d,
                insider_purchase_value_90d = excluded.insider_purchase_value_90d,
                insider_sale_count_90d = excluded.insider_sale_count_90d,
                insider_sale_value_90d = excluded.insider_sale_value_90d,
                insider_cluster_buyers_90d = excluded.insider_cluster_buyers_90d,
                insider_net_value_90d = excluded.insider_net_value_90d,
                latest_institutional_shares = excluded.latest_institutional_shares,
                latest_institutional_value = excluded.latest_institutional_value,
                latest_manager_count = excluded.latest_manager_count,
                institutional_ownership_delta_pct = excluded.institutional_ownership_delta_pct,
                latest_short_interest_shares = excluded.latest_short_interest_shares,
                latest_short_interest_pct_float = excluded.latest_short_interest_pct_float,
                latest_days_to_cover = excluded.latest_days_to_cover,
                short_interest_change_3m = excluded.short_interest_change_3m,
                latest_borrow_fee_rate = excluded.latest_borrow_fee_rate,
                form4_status = excluded.form4_status,
                form4_status_reason = excluded.form4_status_reason,
                positioning_quality = excluded.positioning_quality,
                updated_at = excluded.updated_at
            """,
            (
                ticker,
                asof.isoformat(),
                feature_source_id,
                model_family,
                int(purchase["n"] or 0),
                purchase_value,
                int(sale["n"] or 0),
                sale_value,
                int(purchase["owners"] or 0),
                insider_net_value,
                safe_float(inst["institutional_shares"]) if inst is not None else None,
                safe_float(inst["institutional_value"]) if inst is not None else None,
                int(inst["manager_count"]) if inst is not None and inst["manager_count"] is not None else None,
                safe_float(inst["institutional_ownership_delta_pct"]) if inst is not None else None,
                safe_float(short["short_interest_shares"]) if short is not None else None,
                short_pct_float,
                safe_float(short["days_to_cover"]) if short is not None else None,
                short_change,
                safe_float(borrow["borrow_fee_rate"]) if borrow is not None else None,
                form4_status,
                form4_status_reason,
                quality,
                now,
                now,
            ),
        )
        statuses[ticker] = ";".join(reasons)
    return statuses


def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    registry_path = resolve_path(cfg_get(config, "source_registry.path"), base_dir=base_dir)
    positioning_overrides = load_positioning_overrides(config, base_dir=base_dir)
    form4_db = Path(expand_env_vars(cfg_get(config, "upstream_databases.form4.db_path"))).expanduser()
    mp_db = Path(expand_env_vars(cfg_get(config, "upstream_databases.market_positioning.db_path"))).expanduser()
    output_csv = args.output_csv.expanduser().resolve() if args.output_csv else resolve_path(cfg_get(config, "positioning_import.output_csv"), base_dir=base_dir)
    start = parse_date(cfg_get(config, "positioning_import.start_date", "2016-01-01")) or date(2016, 1, 1)
    form4_source = str(cfg_get(config, "positioning_import.form4_source_id", "sec_insider_upstream"))
    direct_ownership_source = str(cfg_get(config, "positioning_import.direct_ownership_source_id", "sec_ownership_direct"))
    mp_source = str(cfg_get(config, "positioning_import.market_positioning_source_id", "market_positioning_upstream"))
    feature_source = str(cfg_get(config, "positioning_import.source_id", "industrials_positioning_composite"))
    model_family = str(
        args.model_family
        or cfg_get(config, "industrials_universe.initial_subsector", "defense")
        or "defense"
    ).strip()
    if not model_family:
        raise ValueError("model_family cannot be empty")
    require_13f = cfg_bool(config, "positioning_import.require_upstream_13f_for_gate", False)
    require_short = cfg_bool(config, "positioning_import.require_upstream_short_for_gate", False)
    require_short_pct_float = cfg_bool(config, "positioning_import.require_short_pct_float_for_gate", False)
    require_borrow = cfg_bool(config, "positioning_import.require_upstream_borrow_for_gate", False)
    include_historical = cfg_bool(config, "positioning_import.include_historical_members", False)
    ticker_filter = {normalize_ticker(x) for x in args.tickers.split(",") if normalize_ticker(x)}

    if not form4_db.exists():
        raise FileNotFoundError(f"Form 4 upstream DB not found: {form4_db}")
    if not mp_db.exists():
        raise FileNotFoundError(f"Market positioning upstream DB not found: {mp_db}")
    institutional_13f_data_available = upstream_institutional_13f_period_available(config, mp_db)

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        with conn:
            upsert_source_registry(conn, load_source_registry(registry_path))
        run_id = start_run(conn, run_type=RUN_TYPE, input_path=config_path)
        try:
            fact_tickers = load_universe(conn, ticker_filter, model_family=model_family, include_historical=include_historical)
            feature_tickers = load_universe(conn, ticker_filter, model_family=model_family, include_historical=False)
            if not fact_tickers or not feature_tickers:
                raise ValueError(f"No positioning universe tickers found for model_family={model_family}.")
            feature_ticker_set = set(feature_tickers)
            query_tickers, source_to_internal, ambiguous_source_tickers = load_source_ticker_map(conn, fact_tickers)
            (
                query_tickers,
                source_to_internal,
                ambiguous_source_tickers,
                short_exempt_tickers,
                institutional_13f_exempt_tickers,
                short_pct_float_exempt_tickers,
            ) = apply_positioning_source_overrides(
                internal_tickers=fact_tickers,
                overrides=positioning_overrides,
                query_tickers=query_tickers,
                source_to_internal=source_to_internal,
                ambiguous_source_tickers=ambiguous_source_tickers,
                policy_date=date.today(),
                institutional_13f_data_available=institutional_13f_data_available,
            )
            short_exempt_tickers.update(cfg_ticker_set(cfg_get(config, "positioning_import.upstream_short_gate_exempt_tickers", [])))
            institutional_13f_exempt_tickers.update(cfg_ticker_set(cfg_get(config, "positioning_import.upstream_13f_gate_exempt_tickers", [])))
            short_pct_float_exempt_tickers.update(
                cfg_ticker_set(cfg_get(config, "positioning_import.upstream_short_pct_float_gate_exempt_tickers", []))
            )
            query_ciks, cik_to_internal = load_unique_cik_map(conn, fact_tickers)
            (
                form4_exempt_tickers,
                form4_exempt_reasons,
                forced_form4_query_ciks,
                forced_form4_cik_to_internal,
                form4_routes_by_cik,
            ) = load_form4_override_policy(internal_tickers=fact_tickers, overrides=positioning_overrides)
            form4_exempt_tickers.update(cfg_ticker_set(cfg_get(config, "positioning_import.upstream_form4_gate_exempt_tickers", [])))
            for ticker in cfg_ticker_set(cfg_get(config, "positioning_import.upstream_form4_gate_exempt_tickers", [])):
                form4_exempt_reasons.setdefault(ticker, "CONFIG_FORM4_POLICY_EXEMPT")
            query_ciks = sorted(set(query_ciks) | forced_form4_query_ciks)
            for cik, ticker in forced_form4_cik_to_internal.items():
                if cik not in form4_routes_by_cik:
                    cik_to_internal[cik] = ticker
            query_ciks_for_sql = query_ciks or ["__NO_CIK__"]
            with closing(ro_connect(form4_db)) as form4_conn, closing(ro_connect(mp_db)) as mp_conn:
                with conn:
                    conn.execute(
                        f"DELETE FROM data_quality_issues WHERE stage = ? AND ticker IN ({qmarks(fact_tickers)})",
                        (RUN_TYPE, *fact_tickers),
                    )
                    form4_stats = import_form4(
                        conn,
                        form4_conn,
                        fact_tickers,
                        query_tickers=query_tickers,
                        query_ciks=query_ciks_for_sql,
                        source_to_internal=source_to_internal,
                        cik_to_internal=cik_to_internal,
                        routes_by_cik=form4_routes_by_cik,
                        source_id=form4_source,
                        start=start,
                    )
                    direct_stats = direct_form4_stats(conn, fact_tickers, source_id=direct_ownership_source)
                    form4_status_by_ticker: dict[str, str] = {}
                    form4_status_reason_by_ticker: dict[str, str] = {}
                    for ticker in fact_tickers:
                        status, reason = form4_status_for_ticker(
                            ticker,
                            form4_rows=int(form4_stats[ticker]["form4_transactions"]),
                            direct_rows=int(direct_stats[ticker]["direct_form4_transactions"]),
                            form4_exempt_tickers=form4_exempt_tickers,
                            form4_exempt_reasons=form4_exempt_reasons,
                        )
                        form4_status_by_ticker[ticker] = status
                        form4_status_reason_by_ticker[ticker] = reason
                    inst_stats = import_13f(
                        conn,
                        mp_conn,
                        fact_tickers,
                        query_tickers=query_tickers,
                        source_to_internal=source_to_internal,
                        ambiguous_source_tickers=ambiguous_source_tickers,
                        source_id=mp_source,
                        start=start,
                    )
                    short_stats = import_short_interest(
                        conn,
                        mp_conn,
                        fact_tickers,
                        query_tickers=query_tickers,
                        source_to_internal=source_to_internal,
                        ambiguous_source_tickers=ambiguous_source_tickers,
                        source_id=mp_source,
                        start=start,
                    )
                    borrow_stats = import_borrow(
                        conn,
                        mp_conn,
                        fact_tickers,
                        query_tickers=query_tickers,
                        source_to_internal=source_to_internal,
                        ambiguous_source_tickers=ambiguous_source_tickers,
                        source_id=mp_source,
                        start=start,
                    )
                    feature_asof = parse_date(args.asof) or latest_market_feature_asof(conn, model_family) or date.today()
                    feature_status = build_positioning_features(
                        conn,
                        feature_tickers,
                        asof=feature_asof,
                        feature_source_id=feature_source,
                        model_family=model_family,
                        insider_days=int(cfg_get(config, "positioning_import.lookback_days.insider", 90)),
                        short_change_days=int(cfg_get(config, "positioning_import.lookback_days.short_change", 92)),
                        direct_source=direct_ownership_source,
                        upstream_source=form4_source,
                        require_13f=require_13f,
                        require_short=require_short,
                        require_short_pct_float=require_short_pct_float,
                        require_borrow=require_borrow,
                        short_exempt_tickers=short_exempt_tickers,
                        institutional_13f_exempt_tickers=institutional_13f_exempt_tickers,
                        short_pct_float_exempt_tickers=short_pct_float_exempt_tickers,
                        form4_status_by_ticker=form4_status_by_ticker,
                        form4_status_reason_by_ticker=form4_status_reason_by_ticker,
                    )
                    rows: list[dict[str, Any]] = []
                    for ticker in fact_tickers:
                        reasons: list[str] = []
                        if (
                            form4_stats[ticker]["form4_transactions"] == 0
                            and direct_stats[ticker]["direct_form4_transactions"] == 0
                            and ticker not in form4_exempt_tickers
                        ):
                            reasons.append("no_form4_transactions")
                            if ticker in feature_ticker_set:
                                add_issue(conn, ticker, form4_source, "missing_form4_upstream_rows", "No Form 4 rows imported from sec_insider.sqlite.")
                        elif form4_stats[ticker]["form4_transactions"] == 0 and direct_stats[ticker]["direct_form4_transactions"] > 0:
                            reasons.append("form4_direct_sec_rows_found_no_upstream")
                            if ticker in feature_ticker_set:
                                add_issue(conn, ticker, form4_source, "form4_upstream_missing_direct_sec_rows_found", "No upstream Form 4 rows, but direct SEC ownership rows exist in Industrials.sqlite.")
                        if inst_stats[ticker] == 0 and ticker not in institutional_13f_exempt_tickers:
                            reasons.append("no_13f_rows")
                            if ticker in feature_ticker_set:
                                add_issue(conn, ticker, mp_source, "missing_13f_upstream_rows", "No 13F snapshot rows available in market_positioning.sqlite for this ticker.")
                        if short_stats[ticker] == 0 and ticker not in short_exempt_tickers:
                            reasons.append("no_short_interest_rows")
                            if ticker in feature_ticker_set:
                                add_issue(conn, ticker, mp_source, "missing_short_interest_upstream_rows", "No short-interest rows available in market_positioning.sqlite for this ticker.")
                        if borrow_stats[ticker] == 0:
                            reasons.append("no_borrow_rows")
                            if ticker in feature_ticker_set:
                                add_issue(conn, ticker, mp_source, "missing_borrow_upstream_rows", "No IBKR borrow rows available in market_positioning.sqlite for this ticker.")
                        if ticker in feature_ticker_set and feature_status.get(ticker):
                            reasons.append(feature_status[ticker])
                        rows.append(
                            {
                                "ticker": ticker,
                                "form4_transactions": form4_stats[ticker]["form4_transactions"],
                                "direct_form4_transactions": direct_stats[ticker]["direct_form4_transactions"],
                                "form4_latest_transaction_date": form4_stats[ticker]["form4_latest_transaction_date"],
                                "form4_status": form4_status_by_ticker.get(ticker, ""),
                                "form4_status_reason": form4_status_reason_by_ticker.get(ticker, ""),
                                "institutional_rows": inst_stats[ticker],
                                "short_interest_rows": short_stats[ticker],
                                "borrow_rows": borrow_stats[ticker],
                                "feature_status": "review" if reasons else "success",
                                "review_reason": ";".join(reason for reason in reasons if reason),
                            }
                        )
            write_report(output_csv, rows)
            finish_run(conn, run_id=run_id, status="success", row_count=sum(int(row["form4_transactions"]) for row in rows), message=f"fact_tickers={len(rows)} feature_tickers={len(feature_tickers)} output={output_csv}")
            LOGGER.info("Wrote positioning import report: %s", output_csv)
            LOGGER.info("Positioning import complete: fact_tickers=%d feature_tickers=%d form4_rows=%d 13f_rows=%d short_rows=%d borrow_rows=%d", len(rows), len(feature_tickers), sum(int(row["form4_transactions"]) for row in rows), sum(int(row["institutional_rows"]) for row in rows), sum(int(row["short_interest_rows"]) for row in rows), sum(int(row["borrow_rows"]) for row in rows))
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.csv_utils import load_yaml_map, read_csv_flexible, row_get  # noqa: E402
from industrials.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.source_registry import load_source_registry, upsert_source_registry  # noqa: E402
from industrials.core.text_norm import as_bool, normalize_cik, normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("load_defense_historical_membership")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
RUN_TYPE = "load_defense_historical_membership"
LOAD_STAGE = "defense_historical_membership_load"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load defense historical and delisted calibration membership.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--membership-csv", type=Path, default=None)
    parser.add_argument("--delisted-csv", type=Path, default=None)
    parser.add_argument("--active-csv", type=Path, default=None)
    parser.add_argument("--skip-source-registry", action="store_true")
    return parser.parse_args()


def parse_date_text(raw: object, *, field: str, ticker: str, allow_blank: bool = False) -> str:
    text = str(raw or "").strip()[:10]
    if allow_blank and not text:
        return ""
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"{ticker}: invalid {field}={raw!r}; expected YYYY-MM-DD") from exc
    return text


def confidence_score(raw: object) -> float:
    label = str(raw or "").strip().lower()
    if label == "verified":
        return 0.95
    if label == "high":
        return 0.90
    if label == "medium":
        return 0.70
    if label == "low":
        return 0.50
    try:
        return float(label)
    except ValueError:
        return 0.60


def load_cohort_names(path: Path, *, model_family: str) -> dict[str, str]:
    data = load_yaml_map(path)
    if str(data.get("model_family") or model_family).strip() != model_family:
        raise ValueError(f"{path} model_family does not match {model_family}")
    cohorts = data.get("cohorts")
    if not isinstance(cohorts, list):
        raise ValueError(f"{path} must contain a cohorts list.")
    out: dict[str, str] = {}
    for raw in cohorts:
        if not isinstance(raw, dict):
            continue
        cohort_id = str(raw.get("cohort_id") or "").strip()
        cohort_name = str(raw.get("cohort_name") or "").strip()
        if cohort_id and cohort_name:
            out[cohort_id] = cohort_name
    return out


def load_active_rows(path: Path) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for row in read_csv_flexible(path):
        ticker = normalize_ticker(row_get(row, "ticker"))
        if ticker:
            out[ticker] = row
    return out


def csv_ticker_set(path: Path, *ticker_fields: str) -> set[str]:
    fields = ticker_fields or ("ticker",)
    tickers: set[str] = set()
    for row in read_csv_flexible(path):
        ticker = normalize_ticker(row_get(row, *fields))
        if ticker:
            tickers.add(ticker)
    return tickers


def source_id_or_none(conn: Any, source_id: str) -> str | None:
    row = conn.execute("SELECT 1 FROM source_registry WHERE source_id = ? LIMIT 1", (source_id,)).fetchone()
    return source_id if row is not None else None


def add_issue(
    conn: Any,
    *,
    ticker: str,
    company_id: int | None,
    issue_type: str,
    issue_detail: str,
    severity: str = "warning",
    source_id: str | None = None,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO data_quality_issues(
            detected_at, severity, stage, ticker, company_id, source_id, issue_type,
            issue_detail, resolution_status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
        """,
        (now, severity, LOAD_STAGE, ticker, company_id, source_id, issue_type, issue_detail, now, now),
    )


def upsert_historical_company(
    conn: Any,
    *,
    ticker: str,
    source_ticker: str | None = None,
    company_name: str,
    cik: str,
    cohort_id: str,
    cohort_name: str,
    model_family: str,
    source_id: str,
    confidence: float,
    reason: str,
) -> int:
    now = utc_now()
    source_ticker = normalize_ticker(source_ticker) or ticker
    conn.execute(
        """
        INSERT INTO dim_company(
            ticker, cik, company_name, sector, industry, subsector, country, currency,
            universe_status, is_active, data_quality_status, first_seen_at, updated_at
        )
        VALUES (?, ?, ?, 'Industrials', 'Aerospace & Defense', 'Defense', 'United States', 'USD',
                'historical_delisted', 0, 'historical_membership_seed', ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            cik = CASE
                WHEN dim_company.is_active = 1 THEN dim_company.cik
                ELSE COALESCE(NULLIF(excluded.cik, ''), dim_company.cik)
            END,
            company_name = CASE
                WHEN dim_company.is_active = 1 THEN dim_company.company_name
                ELSE COALESCE(NULLIF(excluded.company_name, ''), dim_company.company_name)
            END,
            sector = CASE
                WHEN dim_company.is_active = 1 THEN dim_company.sector
                ELSE COALESCE(NULLIF(dim_company.sector, ''), excluded.sector)
            END,
            industry = CASE
                WHEN dim_company.is_active = 1 THEN dim_company.industry
                ELSE COALESCE(NULLIF(dim_company.industry, ''), excluded.industry)
            END,
            subsector = CASE
                WHEN dim_company.is_active = 1 THEN dim_company.subsector
                ELSE COALESCE(NULLIF(dim_company.subsector, ''), excluded.subsector)
            END,
            country = CASE
                WHEN dim_company.is_active = 1 THEN dim_company.country
                ELSE COALESCE(NULLIF(dim_company.country, ''), excluded.country)
            END,
            currency = CASE
                WHEN dim_company.is_active = 1 THEN dim_company.currency
                ELSE COALESCE(NULLIF(dim_company.currency, ''), excluded.currency)
            END,
            universe_status = CASE WHEN dim_company.is_active = 1 THEN dim_company.universe_status ELSE excluded.universe_status END,
            is_active = CASE WHEN dim_company.is_active = 1 THEN dim_company.is_active ELSE excluded.is_active END,
            data_quality_status = CASE WHEN dim_company.is_active = 1 THEN dim_company.data_quality_status ELSE excluded.data_quality_status END,
            updated_at = excluded.updated_at
        """,
        (ticker, cik, company_name, now, now),
    )
    row = conn.execute("SELECT company_id FROM dim_company WHERE ticker = ?", (ticker,)).fetchone()
    if row is None:
        raise RuntimeError(f"Company upsert failed for historical ticker {ticker}")
    company_id = int(row["company_id"])
    conn.execute(
        """
        INSERT INTO dim_security(
            company_id, ticker, exchange, security_type, listing_status, is_primary_listing, currency, created_at, updated_at
        )
        VALUES (?, ?, 'historical_delisted', 'Common Stock', 'delisted', 1, 'USD', ?, ?)
        ON CONFLICT(ticker, exchange) DO UPDATE SET
            company_id = excluded.company_id,
            security_type = excluded.security_type,
            listing_status = excluded.listing_status,
            currency = excluded.currency,
            updated_at = excluded.updated_at
        """,
        (company_id, ticker, now, now),
    )
    conn.execute(
        """
        INSERT INTO dim_identifier(
            company_id, identifier_type, identifier_value, source_id, confidence, created_at, updated_at
        )
        VALUES (?, 'TICKER', ?, ?, ?, ?, ?)
        ON CONFLICT(company_id, identifier_type, identifier_value) DO UPDATE SET
            source_id = excluded.source_id,
            confidence = excluded.confidence,
            updated_at = excluded.updated_at
        """,
        (company_id, ticker, source_id_or_none(conn, source_id), confidence, now, now),
    )
    if source_ticker != ticker:
        conn.execute(
            """
            INSERT INTO dim_identifier(
                company_id, identifier_type, identifier_value, source_id, confidence, created_at, updated_at
            )
            VALUES (?, 'HISTORICAL_TICKER', ?, ?, ?, ?, ?)
            ON CONFLICT(company_id, identifier_type, identifier_value) DO UPDATE SET
                source_id = excluded.source_id,
                confidence = excluded.confidence,
                updated_at = excluded.updated_at
            """,
            (company_id, source_ticker, source_id_or_none(conn, source_id), confidence, now, now),
        )
    if cik:
        conn.execute(
            """
            INSERT INTO dim_identifier(
                company_id, identifier_type, identifier_value, source_id, confidence, created_at, updated_at
            )
            VALUES (?, 'CIK', ?, ?, ?, ?, ?)
            ON CONFLICT(company_id, identifier_type, identifier_value) DO UPDATE SET
                source_id = excluded.source_id,
                confidence = excluded.confidence,
                updated_at = excluded.updated_at
            """,
            (company_id, cik, source_id_or_none(conn, source_id), confidence, now, now),
        )
    conn.execute(
        """
        INSERT INTO dim_industrials_taxonomy(
            company_id, ticker, model_family, sector, industry, subsector,
            calibration_cohort_id, calibration_cohort, calibration_use, development_stage,
            taxonomy_confidence, taxonomy_source, analyst_reviewed, updated_at
        )
        VALUES (?, ?, ?, 'Industrials', 'Aerospace & Defense', 'Defense', ?, ?, 'historical_backtest',
                'historical_delisted', ?, ?, 0, ?)
        ON CONFLICT(ticker, model_family) DO UPDATE SET
            company_id = excluded.company_id,
            calibration_cohort_id = excluded.calibration_cohort_id,
            calibration_cohort = excluded.calibration_cohort,
            calibration_use = excluded.calibration_use,
            development_stage = excluded.development_stage,
            taxonomy_confidence = excluded.taxonomy_confidence,
            taxonomy_source = excluded.taxonomy_source,
            updated_at = excluded.updated_at
        """,
        (company_id, ticker, model_family, cohort_id, cohort_name, confidence, source_id, now),
    )
    if not cik:
        add_issue(
            conn,
            ticker=ticker,
            company_id=company_id,
            source_id=source_id_or_none(conn, source_id),
            issue_type="missing_historical_cik",
            issue_detail=f"Historical/delisted member lacks CIK. {reason}",
            severity="warning",
        )
    return company_id


def internal_delisted_ticker(
    conn: Any,
    *,
    ticker: str,
    company_name: str,
    cik: str,
    exit_year: int,
    active_rows: dict[str, dict[str, str]],
) -> tuple[str, str | None]:
    """Return a collision-safe internal ticker for historical rows.

    Reused tickers are common in aerospace and defense. The delisted seed must
    preserve the original ticker for Norgate resolution, but the shared
    industrials security master cannot let an old issuer overwrite an active
    same-symbol company.
    """
    internal = f"{ticker}-DEL{exit_year}"
    if ticker in active_rows:
        active = active_rows[ticker]
        active_cik = normalize_cik(row_get(active, "cik"))
        active_name = row_get(active, "company_name")
        return internal, (
            f"Delisted ticker {ticker} is reserved by active defense_tickers.csv "
            f"({active_name}, CIK {active_cik or 'missing'}); using internal_ticker={internal}."
        )

    row = conn.execute(
        """
        SELECT ticker, cik, company_name, is_active
        FROM dim_company
        WHERE ticker = ?
        LIMIT 1
        """,
        (ticker,),
    ).fetchone()
    if row is None:
        return ticker, None
    same_cik = bool(cik) and str(row["cik"] or "") == cik
    same_name = normalize_ticker(row["ticker"]) == ticker and str(row["company_name"] or "").strip().lower() == company_name.strip().lower()
    if int(row["is_active"] or 0) == 0 and (same_cik or same_name):
        return ticker, None
    return internal, (
        f"Delisted ticker {ticker} collides with existing active/security-master row "
        f"{row['ticker']} ({row['company_name']}); using internal_ticker={internal}."
    )


def historical_entity_tickers_for_source(conn: Any, *, model_family: str, source_id: str) -> set[str]:
    tickers: set[str] = set()
    for row in conn.execute(
        """
        SELECT DISTINCT c.ticker
        FROM dim_company c
        JOIN dim_industrials_taxonomy t
          ON t.company_id = c.company_id
         AND t.model_family = ?
         AND t.taxonomy_source = ?
        WHERE c.is_active = 0
        """,
        (model_family, source_id),
    ).fetchall():
        ticker = normalize_ticker(row["ticker"])
        if ticker:
            tickers.add(ticker)
    for row in conn.execute(
        """
        SELECT DISTINCT c.ticker
        FROM dim_company c
        JOIN dim_universe_membership m
          ON m.company_id = c.company_id
         AND m.model_family = ?
         AND m.membership_source_id = ?
        WHERE c.is_active = 0
        """,
        (model_family, source_id),
    ).fetchall():
        ticker = normalize_ticker(row["ticker"])
        if ticker:
            tickers.add(ticker)
    for row in conn.execute(
        """
        SELECT DISTINCT c.ticker
        FROM dim_company c
        JOIN dim_delisted_calibration_seed d
          ON d.internal_ticker = c.ticker
         AND d.model_family = ?
         AND d.source_id = ?
        WHERE c.is_active = 0
        """,
        (model_family, source_id),
    ).fetchall():
        ticker = normalize_ticker(row["ticker"])
        if ticker:
            tickers.add(ticker)
    return tickers


def delete_inactive_historical_entities(conn: Any, *, stale_tickers: set[str]) -> None:
    for ticker in sorted(stale_tickers):
        conn.execute("DELETE FROM dim_company WHERE ticker = ? AND is_active = 0", (ticker,))


def reset_historical_source_artifacts(
    conn: Any,
    *,
    model_family: str,
    source_id: str,
    incoming_tickers: set[str],
    delete_delisted_seed: bool = False,
    extra_security_tickers: set[str] | None = None,
) -> None:
    old_entity_tickers = historical_entity_tickers_for_source(conn, model_family=model_family, source_id=source_id)
    conn.execute(
        """
        DELETE FROM dim_universe_membership
        WHERE model_family = ? AND membership_source_id = ?
        """,
        (model_family, source_id),
    )
    if delete_delisted_seed:
        conn.execute("DELETE FROM dim_delisted_calibration_seed WHERE model_family = ?", (model_family,))
    stale_tickers = old_entity_tickers.difference(incoming_tickers)
    for ticker in sorted(stale_tickers.union(extra_security_tickers or set())):
        conn.execute(
            "DELETE FROM dim_security WHERE ticker = ? AND exchange = 'historical_delisted'",
            (ticker,),
        )
    delete_inactive_historical_entities(conn, stale_tickers=stale_tickers)


def reset_delisted_seed_artifacts(
    conn: Any,
    *,
    model_family: str,
    source_id: str,
    incoming_source_tickers: set[str],
    incoming_internal_tickers: set[str],
) -> None:
    old_internal_tickers = {
        normalize_ticker(row["internal_ticker"])
        for row in conn.execute(
            "SELECT internal_ticker FROM dim_delisted_calibration_seed WHERE model_family = ?",
            (model_family,),
        ).fetchall()
        if normalize_ticker(row["internal_ticker"])
    }
    reset_historical_source_artifacts(
        conn,
        model_family=model_family,
        source_id=source_id,
        incoming_tickers=incoming_internal_tickers,
        delete_delisted_seed=True,
        extra_security_tickers=set(incoming_source_tickers).union(old_internal_tickers),
    )


def reset_explicit_historical_artifacts(
    conn: Any,
    *,
    model_family: str,
    source_id: str,
    incoming_tickers: set[str],
) -> None:
    reset_historical_source_artifacts(
        conn,
        model_family=model_family,
        source_id=source_id,
        incoming_tickers=incoming_tickers,
    )


def upsert_membership(
    conn: Any,
    *,
    company_id: int,
    ticker: str,
    model_family: str,
    source_id: str,
    basis: str,
    start_date: str,
    end_date: str | None,
    membership_status: str,
    is_current_member: int,
    point_in_time_flag: int,
    confidence: float,
    reason: str,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO dim_universe_membership(
            company_id, ticker, model_family, membership_source_id, membership_basis,
            start_date, end_date, membership_status, is_current_member,
            point_in_time_flag, confidence, reason, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, model_family, membership_source_id, start_date) DO UPDATE SET
            company_id = excluded.company_id,
            membership_basis = excluded.membership_basis,
            end_date = excluded.end_date,
            membership_status = excluded.membership_status,
            is_current_member = excluded.is_current_member,
            point_in_time_flag = excluded.point_in_time_flag,
            confidence = excluded.confidence,
            reason = excluded.reason,
            updated_at = excluded.updated_at
        """,
        (
            company_id,
            ticker,
            model_family,
            source_id,
            basis,
            start_date,
            end_date,
            membership_status,
            int(is_current_member),
            int(point_in_time_flag),
            float(confidence),
            reason,
            now,
            now,
        ),
    )


def load_delisted_seed(
    conn: Any,
    *,
    path: Path,
    model_family: str,
    source_id: str,
    cohort_names: dict[str, str],
    default_start_date: str,
    active_rows: dict[str, dict[str, str]],
) -> int:
    rows = read_csv_flexible(path)
    tickers: set[str] = set()
    records: list[dict[str, Any]] = []
    incoming_internal_tickers: set[str] = set()
    now = utc_now()
    for raw in rows:
        ticker = normalize_ticker(row_get(raw, "ticker"))
        if not ticker:
            continue
        if ticker in tickers:
            raise ValueError(f"Duplicate delisted ticker in {path}: {ticker}")
        tickers.add(ticker)
        company_name = row_get(raw, "company", "company_name")
        cohort_id = row_get(raw, "cohort", "calibration_cohort_id")
        if cohort_id not in cohort_names:
            raise ValueError(f"{ticker}: unknown delisted cohort {cohort_id!r}")
        cik = normalize_cik(row_get(raw, "cik"))
        confidence_label = row_get(raw, "confidence")
        score = confidence_score(confidence_label)
        exit_year_text = row_get(raw, "exit_year")
        if not exit_year_text.isdigit():
            raise ValueError(f"{ticker}: invalid exit_year={exit_year_text!r}")
        exit_year = int(exit_year_text)
        end_date = f"{exit_year:04d}-12-31"
        start_date = default_start_date
        if end_date < start_date:
            start_date = f"{exit_year:04d}-01-01"
        reason = (
            f"exit_type={row_get(raw, 'exit_type')} terminal_type={row_get(raw, 'terminal_type')} "
            f"acquirer={row_get(raw, 'acquirer')} exit_year={exit_year}"
        )
        internal_ticker, collision_reason = internal_delisted_ticker(
            conn,
            ticker=ticker,
            company_name=company_name,
            cik=cik,
            exit_year=exit_year,
            active_rows=active_rows,
        )
        incoming_internal_tickers.add(internal_ticker)
        records.append(
            {
                "raw": raw,
                "ticker": ticker,
                "internal_ticker": internal_ticker,
                "collision_reason": collision_reason,
                "company_name": company_name,
                "cohort_id": cohort_id,
                "cik": cik,
                "confidence_label": confidence_label,
                "score": score,
                "exit_year": exit_year,
                "start_date": start_date,
                "end_date": end_date,
                "reason": reason,
            }
        )

    reset_delisted_seed_artifacts(
        conn,
        model_family=model_family,
        source_id=source_id,
        incoming_source_tickers=tickers,
        incoming_internal_tickers=incoming_internal_tickers,
    )
    for record in records:
        raw = record["raw"]
        ticker = str(record["ticker"])
        internal_ticker = str(record["internal_ticker"])
        collision_reason = record["collision_reason"]
        company_name = str(record["company_name"])
        cohort_id = str(record["cohort_id"])
        cik = str(record["cik"])
        confidence_label = str(record["confidence_label"])
        score = float(record["score"])
        exit_year = int(record["exit_year"])
        start_date = str(record["start_date"])
        end_date = str(record["end_date"])
        reason = str(record["reason"])
        if collision_reason:
            add_issue(
                conn,
                ticker=ticker,
                company_id=None,
                source_id=source_id_or_none(conn, source_id),
                issue_type="reused_delisted_ticker_internalized",
                issue_detail=collision_reason,
                severity="warning",
            )
            reason = f"{reason}; {collision_reason}"
        company_id = upsert_historical_company(
            conn,
            ticker=internal_ticker,
            source_ticker=ticker,
            company_name=company_name,
            cik=cik,
            cohort_id=cohort_id,
            cohort_name=cohort_names[cohort_id],
            model_family=model_family,
            source_id=source_id,
            confidence=score,
            reason=reason,
        )
        conn.execute(
            """
            INSERT INTO dim_delisted_calibration_seed(
                ticker, internal_ticker, model_family, company_name, calibration_cohort_id, exit_type,
                terminal_type, acquirer, exit_year, cik, confidence_label, confidence_score,
                source_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, model_family) DO UPDATE SET
                internal_ticker = excluded.internal_ticker,
                company_name = excluded.company_name,
                calibration_cohort_id = excluded.calibration_cohort_id,
                exit_type = excluded.exit_type,
                terminal_type = excluded.terminal_type,
                acquirer = excluded.acquirer,
                exit_year = excluded.exit_year,
                cik = excluded.cik,
                confidence_label = excluded.confidence_label,
                confidence_score = excluded.confidence_score,
                source_id = excluded.source_id,
                updated_at = excluded.updated_at
            """,
            (
                ticker,
                internal_ticker,
                model_family,
                company_name,
                cohort_id,
                row_get(raw, "exit_type"),
                row_get(raw, "terminal_type"),
                row_get(raw, "acquirer"),
                exit_year,
                cik,
                confidence_label,
                score,
                source_id,
                now,
                now,
            ),
        )
        upsert_membership(
            conn,
            company_id=company_id,
            ticker=internal_ticker,
            model_family=model_family,
            source_id=source_id,
            basis="delisted_calibration_seed",
            start_date=start_date,
            end_date=end_date,
            membership_status="delisted",
            is_current_member=0,
            point_in_time_flag=1,
            confidence=score,
            reason=reason,
        )
    return len(tickers)


def load_explicit_historical_membership(
    conn: Any,
    *,
    path: Path,
    model_family: str,
    source_id: str,
    cohort_names: dict[str, str],
) -> int:
    rows = read_csv_flexible(path)
    records: list[dict[str, Any]] = []
    incoming_tickers: set[str] = set()
    for raw in rows:
        ticker = normalize_ticker(row_get(raw, "ticker"))
        if not ticker:
            continue
        if ticker in incoming_tickers:
            raise ValueError(f"Duplicate explicit historical ticker in {path}: {ticker}")
        incoming_tickers.add(ticker)
        company_name = row_get(raw, "company_name") or ticker
        start_date = parse_date_text(row_get(raw, "membership_start_date", "start_date"), field="membership_start_date", ticker=ticker)
        end_date = parse_date_text(row_get(raw, "membership_end_date", "end_date"), field="membership_end_date", ticker=ticker, allow_blank=True)
        if end_date and end_date < start_date:
            raise ValueError(f"{ticker}: membership_end_date {end_date} precedes membership_start_date {start_date}")
        cohort_id = row_get(raw, "calibration_cohort_id", "cohort") or "historical_defense"
        cohort_name = cohort_names.get(cohort_id, "Historical defense membership")
        cik = normalize_cik(row_get(raw, "cik"))
        confidence = confidence_score(row_get(raw, "confidence") or "medium")
        reason = "; ".join(
            part
            for part in (
                row_get(raw, "source_membership"),
                f"successor={row_get(raw, 'successor_ticker')}" if row_get(raw, "successor_ticker") else "",
                row_get(raw, "notes"),
            )
            if part
        )
        records.append(
            {
                "ticker": ticker,
                "company_name": company_name,
                "start_date": start_date,
                "end_date": end_date,
                "cohort_id": cohort_id,
                "cohort_name": cohort_name,
                "cik": cik,
                "confidence": confidence,
                "reason": reason,
                "point_in_time_flag": 1 if as_bool(row_get(raw, "point_in_time_flag"), default=True) else 0,
            }
        )

    reset_explicit_historical_artifacts(
        conn,
        model_family=model_family,
        source_id=source_id,
        incoming_tickers=incoming_tickers,
    )
    for record in records:
        ticker = str(record["ticker"])
        company_name = str(record["company_name"])
        start_date = str(record["start_date"])
        end_date = str(record["end_date"])
        cohort_id = str(record["cohort_id"])
        cohort_name = str(record["cohort_name"])
        cik = str(record["cik"])
        confidence = float(record["confidence"])
        reason = str(record["reason"])
        company_id = upsert_historical_company(
            conn,
            ticker=ticker,
            company_name=company_name,
            cik=cik,
            cohort_id=cohort_id,
            cohort_name=cohort_name,
            model_family=model_family,
            source_id=source_id,
            confidence=confidence,
            reason=reason,
        )
        upsert_membership(
            conn,
            company_id=company_id,
            ticker=ticker,
            model_family=model_family,
            source_id=source_id,
            basis="explicit_historical_membership",
            start_date=start_date,
            end_date=end_date or None,
            membership_status="historical" if end_date else "active_historical_interval",
            is_current_member=0 if end_date else 1,
            point_in_time_flag=int(record["point_in_time_flag"]),
            confidence=confidence,
            reason=reason,
        )
    return len(records)


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    membership_csv = args.membership_csv.expanduser().resolve() if args.membership_csv else resolve_path(cfg_get(config, "industrials_universe.historical_membership_csv"), base_dir=base_dir)
    delisted_csv = args.delisted_csv.expanduser().resolve() if args.delisted_csv else resolve_path(cfg_get(config, "industrials_universe.delisted_seed_csv"), base_dir=base_dir)
    active_csv = args.active_csv.expanduser().resolve() if args.active_csv else resolve_path(cfg_get(config, "industrials_universe.seed_csv"), base_dir=base_dir)
    cohort_path = resolve_path(cfg_get(config, "industrials_universe.cohort_path"), base_dir=base_dir)
    model_family = str(cfg_get(config, "industrials_universe.initial_subsector", "defense"))
    historical_source_id = str(cfg_get(config, "industrials_universe.historical_membership_source_id", "defense_historical_membership_seed"))
    delisted_source_id = str(cfg_get(config, "industrials_universe.delisted_source_id", "defense_delisted_calibration_seed"))
    default_start_date = str(cfg_get(config, "industrials_universe.delisted_default_start_date", "2000-01-01"))
    cohort_names = load_cohort_names(cohort_path, model_family=model_family)
    active_rows = load_active_rows(active_csv)
    explicit_tickers = csv_ticker_set(membership_csv, "ticker")
    delisted_tickers = csv_ticker_set(delisted_csv, "ticker")
    overlap = sorted(explicit_tickers.intersection(delisted_tickers))
    if overlap:
        raise ValueError(
            "Historical membership and delisted calibration CSVs contain overlapping ticker(s): "
            f"{overlap}. Move each issuer into exactly one source file before loading."
        )

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        if not args.skip_source_registry:
            registry_path = resolve_path(cfg_get(config, "source_registry.path"), base_dir=base_dir)
            upsert_source_registry(conn, load_source_registry(registry_path))
        run_id = start_run(conn, run_type=RUN_TYPE, input_path=f"{membership_csv};{delisted_csv}")
        try:
            with conn:
                conn.execute("DELETE FROM data_quality_issues WHERE stage = ?", (LOAD_STAGE,))
                explicit_count = load_explicit_historical_membership(
                    conn,
                    path=membership_csv,
                    model_family=model_family,
                    source_id=historical_source_id,
                    cohort_names=cohort_names,
                )
                delisted_count = load_delisted_seed(
                    conn,
                    path=delisted_csv,
                    model_family=model_family,
                    source_id=delisted_source_id,
                    cohort_names=cohort_names,
                    default_start_date=default_start_date,
                    active_rows=active_rows,
                )
            finish_run(
                conn,
                run_id=run_id,
                status="success",
                row_count=explicit_count + delisted_count,
                message=f"explicit_historical={explicit_count} delisted_seed={delisted_count}",
            )
            LOGGER.info("Loaded defense historical membership: explicit=%d delisted=%d", explicit_count, delisted_count)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

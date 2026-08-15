from __future__ import annotations

import bisect
import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from technology.core.db import utc_now
from technology.core.text_norm import normalize_ticker


PUBLIC_FLOAT_CONCEPT = "EntityPublicFloat"
ENTITY_SHARES_CONCEPT = "EntityCommonStockSharesOutstanding"
COMMON_SHARES_CONCEPT = "CommonStockSharesOutstanding"
DILUTED_SHARES_CONCEPT = "WeightedAverageNumberOfDilutedSharesOutstanding"
SUPPORTED_CONCEPTS = (
    PUBLIC_FLOAT_CONCEPT,
    ENTITY_SHARES_CONCEPT,
    COMMON_SHARES_CONCEPT,
    DILUTED_SHARES_CONCEPT,
)


@dataclass(frozen=True)
class FloatPolicy:
    public_float_max_age_days: int = 550
    shares_outstanding_max_age_days: int = 550
    diluted_shares_max_age_days: int = 550
    public_float_price_max_lag_days: int = 10
    minimum_float_shares: float = 1_000.0
    maximum_float_shares: float = 1_000_000_000_000.0


@dataclass(frozen=True)
class FloatCandidate:
    ticker: str
    availability_date: date
    measurement_date: date
    split_basis_date: date
    float_shares: float
    source: str
    source_accession: str
    priority: int
    proxy_flag: int
    confidence: float
    max_age_days: int
    foreign_private_issuer: bool
    multi_class_issuer: bool


@dataclass(frozen=True)
class FloatEnrichmentStats:
    rows_examined: int
    rows_enriched: int
    rows_without_candidate: int
    tickers_examined: int
    tickers_enriched: int
    source_counts: dict[str, int]

    @property
    def coverage_pct(self) -> float:
        if self.rows_examined <= 0:
            return 0.0
        return 100.0 * self.rows_enriched / self.rows_examined


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def finite_positive(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0.0:
        return None
    return value


def placeholders(values: Iterable[object]) -> str:
    materialized = list(values)
    if not materialized:
        raise ValueError("values cannot be empty")
    return ",".join("?" for _ in materialized)


def short_interest_availability_date(row: sqlite3.Row) -> date | None:
    settlement = parse_date(row["settlement_date"])
    if settlement is None:
        return None
    publication = parse_date(row["publication_date"])
    return publication or settlement + timedelta(days=14)


def load_issuer_flags(
    conn: sqlite3.Connection,
    tickers: list[str],
) -> dict[str, tuple[bool, bool]]:
    if not tickers:
        return {}
    ph = placeholders(tickers)
    # Count simultaneously active listings, not every historical ticker ever
    # associated with the CIK. Otherwise ticker changes such as NANO/ONTO and
    # IIVI/COHR are falsely treated as multi-class issuers.
    active_cik_counts = {
        str(row["cik"]): int(row["ticker_count"] or 0)
        for row in conn.execute(
            """
            SELECT cik, COUNT(DISTINCT ticker) AS ticker_count
            FROM dim_company
            WHERE COALESCE(cik, '') <> ''
              AND is_active = 1
            GROUP BY cik
            """
        )
    }
    rows = conn.execute(
        f"""
        SELECT c.ticker, c.cik,
               COALESCE(p.is_foreign_private_issuer, 0) AS is_foreign_private_issuer
        FROM dim_company c
        LEFT JOIN dim_issuer_reporting_profile p ON p.ticker = c.ticker
        WHERE c.ticker IN ({ph})
        """,
        tickers,
    )
    return {
        normalize_ticker(row["ticker"]): (
            bool(int(row["is_foreign_private_issuer"] or 0)),
            active_cik_counts.get(str(row["cik"] or ""), 0) > 1,
        )
        for row in rows
        if normalize_ticker(row["ticker"])
    }


def latest_raw_close(
    conn: sqlite3.Connection,
    ticker: str,
    measurement_date: date,
    *,
    max_lag_days: int,
    cache: dict[tuple[str, date], tuple[date, float] | None],
) -> tuple[date, float] | None:
    key = (ticker, measurement_date)
    if key in cache:
        return cache[key]
    earliest = measurement_date - timedelta(days=max(0, int(max_lag_days)))
    row = conn.execute(
        """
        SELECT bar_date, close
        FROM fact_price_ohlcv
        WHERE ticker = ?
          AND bar_date BETWEEN ? AND ?
          AND COALESCE(close, 0.0) > 0.0
        ORDER BY
            bar_date DESC,
            CASE source_id
                WHEN 'yahoo_finance_adjusted' THEN 0
                WHEN 'norgate_us_equities_total_return' THEN 1
                ELSE 2
            END
        LIMIT 1
        """,
        (ticker, earliest.isoformat(), measurement_date.isoformat()),
    ).fetchone()
    if row is None:
        cache[key] = None
        return None
    price_date = parse_date(row["bar_date"])
    close = finite_positive(row["close"])
    result = (price_date, close) if price_date is not None and close is not None else None
    cache[key] = result
    return result


def candidate_definition(
    concept: str,
    *,
    policy: FloatPolicy,
) -> tuple[str, int, int, float, int, bool] | None:
    if concept == PUBLIC_FLOAT_CONCEPT:
        return (
            "sec_entity_public_float_price_proxy",
            10,
            1,
            0.90,
            policy.public_float_max_age_days,
            True,
        )
    if concept == ENTITY_SHARES_CONCEPT:
        return (
            "sec_entity_common_stock_shares_outstanding_proxy",
            20,
            1,
            0.72,
            policy.shares_outstanding_max_age_days,
            False,
        )
    if concept == COMMON_SHARES_CONCEPT:
        return (
            "sec_common_stock_shares_outstanding_proxy",
            30,
            1,
            0.65,
            policy.shares_outstanding_max_age_days,
            False,
        )
    if concept == DILUTED_SHARES_CONCEPT:
        return (
            "sec_diluted_weighted_average_shares_proxy",
            40,
            1,
            0.45,
            policy.diluted_shares_max_age_days,
            False,
        )
    return None


def load_sec_float_candidates(
    conn: sqlite3.Connection,
    tickers: list[str],
    *,
    policy: FloatPolicy,
) -> dict[str, list[FloatCandidate]]:
    if not tickers:
        return {}
    issuer_flags = load_issuer_flags(conn, tickers)
    raw_close_cache: dict[tuple[str, date], tuple[date, float] | None] = {}
    candidates: dict[str, list[FloatCandidate]] = defaultdict(list)
    seen: set[tuple[str, str, date, date, str, int]] = set()
    for requested_ticker in tickers:
        # fact_sec_xbrl_fact_raw is indexed by (ticker, taxonomy). Per-ticker
        # lookups avoid a broad scan of the multi-gigabyte raw fact table.
        rows = conn.execute(
            """
            SELECT
                r.ticker,
                r.concept,
                r.unit,
                r.value,
                r.end_date,
                r.accession_number,
                MIN(f.filing_date) AS filing_date
            FROM fact_sec_xbrl_fact_raw r
            JOIN fact_sec_filing f
              ON f.ticker = r.ticker
             AND f.accession_number = r.accession_number
             AND COALESCE(f.filing_date, '') <> ''
            WHERE r.ticker = ?
              AND (
                    (LOWER(r.taxonomy) = 'dei'
                     AND r.concept IN (?, ?))
                 OR (LOWER(r.taxonomy) = 'us-gaap'
                     AND r.concept IN (?, ?))
              )
              AND COALESCE(r.value, 0.0) > 0.0
            GROUP BY r.fact_key
            ORDER BY filing_date, r.end_date
            """,
            (
                requested_ticker,
                PUBLIC_FLOAT_CONCEPT,
                ENTITY_SHARES_CONCEPT,
                COMMON_SHARES_CONCEPT,
                DILUTED_SHARES_CONCEPT,
            ),
        )
        for row in rows:
            ticker = normalize_ticker(row["ticker"])
            availability = parse_date(row["filing_date"])
            measurement = parse_date(row["end_date"])
            value = finite_positive(row["value"])
            concept = str(row["concept"] or "")
            unit = str(row["unit"] or "").strip().lower()
            definition = candidate_definition(concept, policy=policy)
            if (
                not ticker
                or availability is None
                or measurement is None
                or value is None
                or definition is None
            ):
                continue
            if concept == PUBLIC_FLOAT_CONCEPT and unit not in {"usd", "usd/shares"}:
                continue
            if concept != PUBLIC_FLOAT_CONCEPT and "share" not in unit:
                continue
            source, priority, proxy_flag, confidence, max_age_days, requires_price = definition
            split_basis_date = measurement
            float_shares = value
            if requires_price:
                raw_close = latest_raw_close(
                    conn,
                    ticker,
                    measurement,
                    max_lag_days=policy.public_float_price_max_lag_days,
                    cache=raw_close_cache,
                )
                if raw_close is None:
                    continue
                _, close = raw_close
                float_shares = value / close
            elif concept == DILUTED_SHARES_CONCEPT:
                # Per-share accounting data are retrospectively split-adjusted
                # by the filing that made them available. Avoid applying the
                # same pre-filing split a second time.
                split_basis_date = availability
            if not (
                policy.minimum_float_shares
                <= float_shares
                <= policy.maximum_float_shares
            ):
                continue
            foreign_private_issuer, multi_class_issuer = issuer_flags.get(
                ticker,
                (False, False),
            )
            if foreign_private_issuer:
                confidence -= 0.12
            if multi_class_issuer:
                confidence -= 0.12
            confidence = max(0.0, min(1.0, confidence))
            accession = str(row["accession_number"] or "")
            dedupe_key = (
                ticker,
                source,
                availability,
                measurement,
                accession,
                int(round(float_shares)),
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            candidates[ticker].append(
                FloatCandidate(
                    ticker=ticker,
                    availability_date=availability,
                    measurement_date=measurement,
                    split_basis_date=split_basis_date,
                    float_shares=float_shares,
                    source=source,
                    source_accession=accession,
                    priority=priority,
                    proxy_flag=proxy_flag,
                    confidence=confidence,
                    max_age_days=max_age_days,
                    foreign_private_issuer=foreign_private_issuer,
                    multi_class_issuer=multi_class_issuer,
                )
            )
    for ticker in candidates:
        candidates[ticker].sort(
            key=lambda item: (
                item.availability_date,
                item.measurement_date,
                -item.priority,
                item.confidence,
            )
        )
    return dict(candidates)


def load_split_events(
    conn: sqlite3.Connection,
    tickers: list[str],
) -> dict[str, tuple[list[date], list[float]]]:
    if not tickers:
        return {}
    ph = placeholders(tickers)
    grouped: dict[str, dict[date, float]] = defaultdict(dict)
    rows = conn.execute(
        f"""
        SELECT ticker, action_date, MAX(split_factor) AS split_factor
        FROM fact_corporate_action
        WHERE ticker IN ({ph})
          AND LOWER(action_type) = 'split'
          AND COALESCE(split_factor, 0.0) > 0.0
        GROUP BY ticker, action_date
        ORDER BY ticker, action_date
        """,
        tickers,
    )
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        action_date = parse_date(row["action_date"])
        factor = finite_positive(row["split_factor"])
        if ticker and action_date is not None and factor is not None:
            grouped[ticker][action_date] = factor
    output: dict[str, tuple[list[date], list[float]]] = {}
    for ticker, events in grouped.items():
        dates = sorted(events)
        output[ticker] = (dates, [events[event_date] for event_date in dates])
    return output


def cumulative_split_factor(
    split_events: tuple[list[date], list[float]] | None,
    *,
    after_date: date,
    through_date: date,
) -> float:
    if split_events is None or through_date <= after_date:
        return 1.0
    dates, factors = split_events
    start_index = bisect.bisect_right(dates, after_date)
    end_index = bisect.bisect_right(dates, through_date)
    result = 1.0
    for factor in factors[start_index:end_index]:
        result *= factor
    return result


def choose_candidate(
    candidates: list[FloatCandidate],
    *,
    settlement_date: date,
    availability_date: date,
) -> FloatCandidate | None:
    eligible = [
        candidate
        for candidate in candidates
        if candidate.availability_date <= availability_date
        and candidate.measurement_date <= settlement_date
        and 0 <= (settlement_date - candidate.measurement_date).days <= candidate.max_age_days
    ]
    if not eligible:
        return None
    latest_by_source: dict[str, FloatCandidate] = {}
    for candidate in eligible:
        previous = latest_by_source.get(candidate.source)
        if previous is None or (
            candidate.measurement_date,
            candidate.availability_date,
            candidate.confidence,
        ) > (
            previous.measurement_date,
            previous.availability_date,
            previous.confidence,
        ):
            latest_by_source[candidate.source] = candidate
    return min(
        latest_by_source.values(),
        key=lambda item: (
            item.priority,
            -item.measurement_date.toordinal(),
            -item.availability_date.toordinal(),
            -item.confidence,
        ),
    )


def enrich_short_interest_float(
    conn: sqlite3.Connection,
    tickers: list[str],
    *,
    source_id: str,
    policy: FloatPolicy | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> FloatEnrichmentStats:
    normalized_tickers = sorted(
        {ticker for ticker in (normalize_ticker(value) for value in tickers) if ticker}
    )
    if not normalized_tickers:
        return FloatEnrichmentStats(0, 0, 0, 0, 0, {})
    policy = policy or FloatPolicy()
    candidates = load_sec_float_candidates(conn, normalized_tickers, policy=policy)
    split_events = load_split_events(conn, normalized_tickers)
    ph = placeholders(normalized_tickers)
    date_clause = ""
    date_params: tuple[str, ...] = ()
    if start_date is not None:
        date_clause += " AND settlement_date >= ?"
        date_params += (start_date.isoformat(),)
    if end_date is not None:
        date_clause += " AND settlement_date <= ?"
        date_params += (end_date.isoformat(),)
    rows = conn.execute(
        f"""
        SELECT ticker, settlement_date, publication_date, short_interest_shares
        FROM fact_short_interest
        WHERE source_id = ?
          AND ticker IN ({ph})
          {date_clause}
        ORDER BY ticker, settlement_date
        """,
        (source_id, *normalized_tickers, *date_params),
    ).fetchall()
    updates: list[tuple[Any, ...]] = []
    source_counts: Counter[str] = Counter()
    enriched_tickers: set[str] = set()
    now = utc_now()
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        settlement = parse_date(row["settlement_date"])
        available = short_interest_availability_date(row)
        short_shares = finite_positive(row["short_interest_shares"])
        selected = (
            choose_candidate(
                candidates.get(ticker, []),
                settlement_date=settlement,
                availability_date=available,
            )
            if ticker and settlement is not None and available is not None
            else None
        )
        if selected is None or short_shares is None or settlement is None:
            updates.append(
                (
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    "no_pit_float_candidate",
                    None,
                    now,
                    ticker,
                    row["settlement_date"],
                    source_id,
                )
            )
            continue
        split_factor = cumulative_split_factor(
            split_events.get(ticker),
            after_date=selected.split_basis_date,
            through_date=settlement,
        )
        adjusted_float = selected.float_shares * split_factor
        if not (
            policy.minimum_float_shares
            <= adjusted_float
            <= policy.maximum_float_shares
        ):
            updates.append(
                (
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    "selected_float_out_of_bounds",
                    split_factor,
                    now,
                    ticker,
                    row["settlement_date"],
                    source_id,
                )
            )
            continue
        pct_float = short_shares / adjusted_float
        age_days = (settlement - selected.measurement_date).days
        reason_parts = [
            f"selected:{selected.source}",
            f"accession:{selected.source_accession}",
            f"age_days:{age_days}",
        ]
        if selected.foreign_private_issuer:
            reason_parts.append("foreign_private_issuer")
        if selected.multi_class_issuer:
            reason_parts.append("multi_class_issuer")
        if not math.isclose(split_factor, 1.0):
            reason_parts.append(f"split_adjusted:{split_factor:.12g}")
        updates.append(
            (
                adjusted_float,
                pct_float,
                selected.source,
                selected.availability_date.isoformat(),
                selected.measurement_date.isoformat(),
                selected.proxy_flag,
                ";".join(reason_parts),
                split_factor,
                now,
                ticker,
                row["settlement_date"],
                source_id,
            )
        )
        # Confidence is deliberately stored separately so the selection reason
        # remains stable and machine-readable.
        updates[-1] = (
            *updates[-1][:6],
            selected.confidence,
            *updates[-1][6:],
        )
        source_counts[selected.source] += 1
        enriched_tickers.add(ticker)
    conn.executemany(
        """
        UPDATE fact_short_interest
        SET float_shares = ?,
            short_interest_pct_float = ?,
            float_source = ?,
            float_source_asof_date = ?,
            float_measurement_date = ?,
            float_proxy_flag = ?,
            float_confidence = ?,
            float_selection_reason = ?,
            float_split_adjustment_factor = ?,
            updated_at = ?
        WHERE ticker = ?
          AND settlement_date = ?
          AND source_id = ?
        """,
        updates,
    )
    enriched = sum(source_counts.values())
    return FloatEnrichmentStats(
        rows_examined=len(rows),
        rows_enriched=enriched,
        rows_without_candidate=len(rows) - enriched,
        tickers_examined=len(normalized_tickers),
        tickers_enriched=len(enriched_tickers),
        source_counts=dict(source_counts),
    )


def validate_float_enrichment(
    conn: sqlite3.Connection,
    tickers: list[str],
    *,
    source_id: str,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[str]:
    normalized_tickers = sorted(
        {ticker for ticker in (normalize_ticker(value) for value in tickers) if ticker}
    )
    if not normalized_tickers:
        return []
    ph = placeholders(normalized_tickers)
    date_clause = ""
    date_params: tuple[str, ...] = ()
    if start_date is not None:
        date_clause += " AND settlement_date >= ?"
        date_params += (start_date.isoformat(),)
    if end_date is not None:
        date_clause += " AND settlement_date <= ?"
        date_params += (end_date.isoformat(),)
    rows = conn.execute(
        f"""
        SELECT *
        FROM fact_short_interest
        WHERE source_id = ?
          AND ticker IN ({ph})
          AND short_interest_pct_float IS NOT NULL
          {date_clause}
        """,
        (source_id, *normalized_tickers, *date_params),
    )
    errors: list[str] = []
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        settlement = parse_date(row["settlement_date"])
        available = short_interest_availability_date(row)
        source_available = parse_date(row["float_source_asof_date"])
        measurement = parse_date(row["float_measurement_date"])
        short_shares = finite_positive(row["short_interest_shares"])
        float_shares = finite_positive(row["float_shares"])
        pct_float = finite_positive(row["short_interest_pct_float"])
        if not row["float_source"] or source_available is None or measurement is None:
            errors.append(f"{ticker}:{row['settlement_date']}:missing_float_provenance")
            continue
        if available is None or source_available > available:
            errors.append(f"{ticker}:{row['settlement_date']}:float_available_after_short_interest")
        if settlement is None or measurement > settlement:
            errors.append(f"{ticker}:{row['settlement_date']}:float_measured_after_settlement")
        if (
            short_shares is None
            or float_shares is None
            or pct_float is None
            or not math.isclose(pct_float, short_shares / float_shares, rel_tol=1e-9, abs_tol=1e-12)
        ):
            errors.append(f"{ticker}:{row['settlement_date']}:short_interest_pct_float_mismatch")
        split_factor = finite_positive(row["float_split_adjustment_factor"])
        if split_factor is None:
            errors.append(f"{ticker}:{row['settlement_date']}:invalid_split_adjustment_factor")
    return errors

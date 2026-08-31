from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SourceCoverageObservation:
    table: str
    date_column: str
    identity_column: str
    coverage_mode: str
    max_date: str
    rows_on_asof: int
    active_tickers_on_asof: int | None
    missing_active_tickers: tuple[str, ...] | None
    distinct_identities_on_asof: int | None


@dataclass(frozen=True)
class SourceCoverageResult:
    model_family: str
    asof: str
    active_ticker_count: int
    observations: tuple[SourceCoverageObservation, ...]
    errors: tuple[str, ...]

    @property
    def acceptance(self) -> str:
        return "PASS" if not self.errors else "FAIL"


TICKER_BACKED_CHECKS = (
    ("fact_price_ohlcv", "bar_date", "ticker", "point_in_time"),
    ("fact_market_snapshot", "asof_date", "ticker", "point_in_time"),
    ("feature_market_technical", "asof_date", "ticker", "exact"),
    ("feature_financial_statement", "asof_date", "ticker", "exact"),
    ("feature_positioning", "asof_date", "ticker", "exact"),
)
INFORMATIONAL_CHECKS = (("fact_fx_rate", "rate_date", "currency_pair", "exact"),)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def audit_industrials_source_coverage(
    conn: sqlite3.Connection,
    *,
    model_family: str,
    asof: str,
) -> SourceCoverageResult:
    family = str(model_family or "").strip()
    if not family:
        raise ValueError("model_family is required for source coverage")
    active_sql = """
        SELECT DISTINCT m.ticker
        FROM dim_universe_membership m
        JOIN dim_company c ON c.company_id = m.company_id
        JOIN dim_industrials_taxonomy t
          ON t.company_id = m.company_id AND t.model_family = m.model_family
        WHERE m.model_family = ?
          AND m.membership_basis = 'current_source_of_truth'
          AND m.is_current_member = 1
          AND c.is_active = 1
    """
    active_tickers = tuple(
        str(row[0])
        for row in conn.execute(active_sql + " ORDER BY m.ticker", (family,)).fetchall()
    )
    active_count = len(active_tickers)
    errors: list[str] = []
    if active_count <= 0:
        errors.append(f"active {family} universe is empty")

    observations: list[SourceCoverageObservation] = []
    for table, date_column, identity_column, coverage_mode in (
        *TICKER_BACKED_CHECKS,
        *INFORMATIONAL_CHECKS,
    ):
        if not _table_exists(conn, table):
            errors.append(f"required source table missing: {table}")
            continue
        max_row = conn.execute(f"SELECT MAX({date_column}) FROM {table}").fetchone()
        max_date = str(max_row[0] or "") if max_row else ""
        rows_on_asof = int(
            conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {date_column} = ?",
                (asof,),
            ).fetchone()[0]
            or 0
        )
        active_covered: int | None = None
        missing_active_tickers: tuple[str, ...] | None = None
        distinct_identities: int | None = None
        if identity_column == "ticker":
            covered_tickers = {
                str(row[0])
                for row in conn.execute(
                    f"""
                    SELECT DISTINCT source.ticker
                    FROM {table} source
                    JOIN ({active_sql}) active ON active.ticker = source.ticker
                    WHERE source.{date_column} {"=" if coverage_mode == "exact" else "<="} ?
                    """,
                    (family, asof),
                ).fetchall()
            }
            active_covered = len(covered_tickers)
            missing_active_tickers = tuple(
                ticker for ticker in active_tickers if ticker not in covered_tickers
            )
            if missing_active_tickers:
                errors.append(
                    f"{table}.{date_column} {coverage_mode} active coverage="
                    f"{active_covered}/{active_count};missing="
                    + ",".join(missing_active_tickers)
                )
        else:
            distinct_identities = int(
                conn.execute(
                    f"SELECT COUNT(DISTINCT {identity_column}) "
                    f"FROM {table} WHERE {date_column} = ?",
                    (asof,),
                ).fetchone()[0]
                or 0
            )
        observations.append(
            SourceCoverageObservation(
                table=table,
                date_column=date_column,
                identity_column=identity_column,
                coverage_mode=coverage_mode,
                max_date=max_date,
                rows_on_asof=rows_on_asof,
                active_tickers_on_asof=active_covered,
                missing_active_tickers=missing_active_tickers,
                distinct_identities_on_asof=distinct_identities,
            )
        )
    return SourceCoverageResult(
        model_family=family,
        asof=asof,
        active_ticker_count=active_count,
        observations=tuple(observations),
        errors=tuple(errors),
    )


def coverage_manifest(result: SourceCoverageResult) -> dict[str, Any]:
    return {
        "acceptance": result.acceptance,
        "model_family": result.model_family,
        "asof_date": result.asof,
        "active_ticker_count": result.active_ticker_count,
        "errors": list(result.errors),
        "observations": [
            {
                "table": observation.table,
                "date_column": observation.date_column,
                "identity_column": observation.identity_column,
                "coverage_mode": observation.coverage_mode,
                "max_date": observation.max_date,
                "rows_on_asof": observation.rows_on_asof,
                "active_tickers_on_asof": observation.active_tickers_on_asof,
                "missing_active_tickers": (
                    list(observation.missing_active_tickers)
                    if observation.missing_active_tickers is not None
                    else None
                ),
                "distinct_identities_on_asof": observation.distinct_identities_on_asof,
            }
            for observation in result.observations
        ],
    }


def require_source_coverage(result: SourceCoverageResult) -> None:
    if result.errors:
        raise ValueError(
            f"{result.model_family.title()} source coverage audit failed: "
            + "; ".join(result.errors)
        )

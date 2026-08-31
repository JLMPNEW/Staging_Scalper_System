from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from technology.core.text_norm import normalize_cik, normalize_ticker


CIK_LINEAGE_SOURCE_ID = "sec_submissions"


def _cik10(value: object) -> str:
    cik = normalize_cik(str(value or ""))
    return cik.zfill(10) if cik else ""


def configured_legacy_ciks(raw_config: object, ticker: str) -> tuple[str, ...]:
    """Return validated legacy CIKs for a ticker in configured order."""
    if not isinstance(raw_config, Mapping):
        return ()
    raw_rows = raw_config.get(normalize_ticker(ticker), ())
    if isinstance(raw_rows, (str, int)):
        raw_rows = (raw_rows,)
    if not isinstance(raw_rows, Iterable) or isinstance(raw_rows, Mapping):
        raise ValueError(f"Legacy CIK config for {ticker} must be a list")

    ciks: list[str] = []
    for raw_row in raw_rows:
        raw_cik = raw_row.get("cik") if isinstance(raw_row, Mapping) else raw_row
        cik = _cik10(raw_cik)
        if not cik or not cik.strip("0"):
            raise ValueError(f"Invalid legacy CIK configured for {ticker}: {raw_cik!r}")
        if cik not in ciks:
            ciks.append(cik)
    return tuple(ciks)


def expand_company_cik_lineage(
    companies: list[dict[str, Any]],
    raw_config: object,
) -> list[dict[str, Any]]:
    """Expand companies to legacy CIK jobs followed by the primary CIK job."""
    expanded: list[dict[str, Any]] = []
    for company in companies:
        ticker = normalize_ticker(company.get("ticker"))
        primary_cik = _cik10(company.get("cik"))
        for legacy_cik in configured_legacy_ciks(raw_config, ticker):
            if legacy_cik == primary_cik:
                continue
            expanded.append(
                {
                    **company,
                    "ticker": ticker,
                    "cik": legacy_cik,
                    "primary_cik": primary_cik,
                    "cik_role": "legacy",
                }
            )
        expanded.append(
            {
                **company,
                "ticker": ticker,
                "cik": primary_cik,
                "primary_cik": primary_cik,
                "cik_role": "primary",
            }
        )
    return expanded


def upsert_configured_cik_identifiers(
    connection: Any,
    companies: list[dict[str, Any]],
) -> int:
    """Persist expanded CIK lineage in the existing identifier dimension."""
    changed = 0
    for company in companies:
        if company.get("cik_role") != "legacy":
            continue
        ticker = normalize_ticker(company.get("ticker"))
        cik = _cik10(company.get("cik"))
        before = int(connection.total_changes)
        existing = connection.execute(
            """
            SELECT identifier_id
            FROM dim_identifier
            WHERE company_id = (SELECT company_id FROM dim_company WHERE ticker = ?)
              AND identifier_type = 'CIK'
              AND identifier_value = ?
            ORDER BY identifier_id
            LIMIT 1
            """,
            (ticker, cik),
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO dim_identifier(
                    company_id, identifier_type, identifier_value, source_id,
                    confidence, created_at, updated_at
                )
                SELECT company_id, 'CIK', ?, ?, 1.0,
                       strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                       strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                FROM dim_company
                WHERE ticker = ?
                """,
                (cik, CIK_LINEAGE_SOURCE_ID, ticker),
            )
        else:
            connection.execute(
                """
                UPDATE dim_identifier
                SET source_id = ?, confidence = 1.0,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE identifier_id = ?
                """,
                (CIK_LINEAGE_SOURCE_ID, int(existing[0])),
            )
        changed += int(connection.total_changes) - before
    return changed

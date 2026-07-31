from __future__ import annotations

import sqlite3


FILING_ALIAS_COLUMNS = {
    "accepted_at": "TEXT",
}
FACT_ALIAS_COLUMNS = {
    "canonical_metric": "TEXT",
    "period_start": "TEXT",
    "period_end": "TEXT",
    "accepted_at": "TEXT",
}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_columns(
    conn: sqlite3.Connection,
    *,
    table: str,
    declarations: dict[str, str],
) -> None:
    existing = _columns(conn, table)
    for column, declaration in declarations.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def ensure_shared_planner_compatibility(conn: sqlite3.Connection) -> None:
    """Maintain the neutral parser aliases expected by the shared planner."""
    _ensure_columns(
        conn,
        table="fact_sec_filing",
        declarations=FILING_ALIAS_COLUMNS,
    )
    _ensure_columns(
        conn,
        table="fact_sec_xbrl_fact",
        declarations=FACT_ALIAS_COLUMNS,
    )
    conn.executescript(
        """
        UPDATE fact_sec_filing
        SET accepted_at = acceptance_datetime
        WHERE COALESCE(accepted_at, '') <> COALESCE(acceptance_datetime, '');

        UPDATE fact_sec_xbrl_fact
        SET canonical_metric = metric_name,
            period_start = start_date,
            period_end = end_date,
            accepted_at = (
                SELECT MAX(f.acceptance_datetime)
                FROM fact_sec_filing AS f
                WHERE f.ticker = fact_sec_xbrl_fact.ticker
                  AND f.accession_number = fact_sec_xbrl_fact.accession_number
            )
        WHERE COALESCE(canonical_metric, '') <> COALESCE(metric_name, '')
           OR COALESCE(period_start, '') <> COALESCE(start_date, '')
           OR COALESCE(period_end, '') <> COALESCE(end_date, '')
           OR COALESCE(accepted_at, '') <> COALESCE((
                SELECT MAX(f.acceptance_datetime)
                FROM fact_sec_filing AS f
                WHERE f.ticker = fact_sec_xbrl_fact.ticker
                  AND f.accession_number = fact_sec_xbrl_fact.accession_number
           ), '');

        CREATE TRIGGER IF NOT EXISTS trg_technology_sec_filing_parser_alias_insert
        AFTER INSERT ON fact_sec_filing
        BEGIN
            UPDATE fact_sec_filing
            SET accepted_at = NEW.acceptance_datetime
            WHERE ticker = NEW.ticker
              AND accession_number = NEW.accession_number
              AND source_id = NEW.source_id;
            UPDATE fact_sec_xbrl_fact
            SET accepted_at = NEW.acceptance_datetime
            WHERE ticker = NEW.ticker
              AND accession_number = NEW.accession_number;
        END;

        CREATE TRIGGER IF NOT EXISTS trg_technology_sec_filing_parser_alias_update
        AFTER UPDATE OF acceptance_datetime ON fact_sec_filing
        BEGIN
            UPDATE fact_sec_filing
            SET accepted_at = NEW.acceptance_datetime
            WHERE ticker = NEW.ticker
              AND accession_number = NEW.accession_number
              AND source_id = NEW.source_id;
            UPDATE fact_sec_xbrl_fact
            SET accepted_at = NEW.acceptance_datetime
            WHERE ticker = NEW.ticker
              AND accession_number = NEW.accession_number;
        END;

        CREATE TRIGGER IF NOT EXISTS trg_technology_sec_fact_parser_alias_insert
        AFTER INSERT ON fact_sec_xbrl_fact
        BEGIN
            UPDATE fact_sec_xbrl_fact
            SET canonical_metric = NEW.metric_name,
                period_start = NEW.start_date,
                period_end = NEW.end_date,
                accepted_at = (
                    SELECT MAX(f.acceptance_datetime)
                    FROM fact_sec_filing AS f
                    WHERE f.ticker = NEW.ticker
                      AND f.accession_number = NEW.accession_number
                )
            WHERE fact_key = NEW.fact_key;
        END;

        CREATE TRIGGER IF NOT EXISTS trg_technology_sec_fact_parser_alias_update
        AFTER UPDATE OF metric_name, start_date, end_date, accession_number
        ON fact_sec_xbrl_fact
        BEGIN
            UPDATE fact_sec_xbrl_fact
            SET canonical_metric = NEW.metric_name,
                period_start = NEW.start_date,
                period_end = NEW.end_date,
                accepted_at = (
                    SELECT MAX(f.acceptance_datetime)
                    FROM fact_sec_filing AS f
                    WHERE f.ticker = NEW.ticker
                      AND f.accession_number = NEW.accession_number
                )
            WHERE fact_key = NEW.fact_key;
        END;
        """
    )


def validate_shared_planner_compatibility(conn: sqlite3.Connection) -> None:
    missing_filing = sorted(set(FILING_ALIAS_COLUMNS) - _columns(conn, "fact_sec_filing"))
    missing_fact = sorted(set(FACT_ALIAS_COLUMNS) - _columns(conn, "fact_sec_xbrl_fact"))
    if missing_filing or missing_fact:
        raise RuntimeError(
            "Shared parser compatibility is incomplete: "
            f"missing_filing={missing_filing}, missing_fact={missing_fact}"
        )

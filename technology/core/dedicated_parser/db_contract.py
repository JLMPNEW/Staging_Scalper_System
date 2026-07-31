from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from dedicated_parser.schema import ensure_schema as ensure_shared_parser_schema


TECHNOLOGY_PARSER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS feature_financial_metric_availability (
    model_family TEXT NOT NULL,
    ticker TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    availability_status TEXT NOT NULL,
    metric_value REAL,
    unit TEXT,
    source_id TEXT,
    accession_number TEXT,
    filing_date TEXT,
    period_start TEXT,
    period_end TEXT,
    taxonomy TEXT,
    concept_name TEXT,
    extraction_method TEXT,
    provenance_json TEXT,
    source_tier TEXT NOT NULL,
    source_accession_number TEXT,
    source_filing_date TEXT,
    source_document TEXT,
    confidence REAL NOT NULL DEFAULT 0.0,
    review_required_flag INTEGER NOT NULL DEFAULT 0,
    status_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(model_family, ticker, asof_date, metric_name)
);

CREATE INDEX IF NOT EXISTS idx_financial_metric_availability_lookup
ON feature_financial_metric_availability(
    model_family, asof_date, metric_name, availability_status
);

CREATE TABLE IF NOT EXISTS fact_technology_specialized_metric (
    specialized_fact_key TEXT PRIMARY KEY,
    model_family TEXT NOT NULL,
    ticker TEXT NOT NULL,
    cik TEXT,
    metric_name TEXT NOT NULL,
    metric_version TEXT NOT NULL,
    value REAL,
    value_text TEXT,
    unit TEXT,
    period_start TEXT,
    period_end TEXT,
    availability_datetime TEXT NOT NULL,
    filing_date TEXT,
    accession_number TEXT,
    form_type TEXT,
    source_document TEXT NOT NULL,
    source_document_sha256 TEXT NOT NULL,
    source_id TEXT NOT NULL,
    evidence_key TEXT NOT NULL,
    extraction_method TEXT NOT NULL,
    confidence REAL NOT NULL,
    review_required_flag INTEGER NOT NULL DEFAULT 0,
    status_reason TEXT,
    definition_version TEXT NOT NULL,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(
        model_family, ticker, metric_name, metric_version, period_start,
        period_end, source_id, evidence_key
    )
);

CREATE INDEX IF NOT EXISTS idx_technology_specialized_metric_lookup
ON fact_technology_specialized_metric(
    model_family, ticker, metric_name, availability_datetime, period_end
);

CREATE TABLE IF NOT EXISTS feature_technology_specialized_metric (
    model_family TEXT NOT NULL,
    ticker TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    metric_version TEXT NOT NULL,
    value REAL,
    unit TEXT,
    availability_status TEXT NOT NULL,
    source_accession_number TEXT,
    source_availability_datetime TEXT,
    confidence REAL NOT NULL DEFAULT 0.0,
    review_required_flag INTEGER NOT NULL DEFAULT 0,
    status_reason TEXT,
    definition_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(model_family, ticker, asof_date, metric_name, metric_version)
);

CREATE INDEX IF NOT EXISTS idx_technology_specialized_feature_lookup
ON feature_technology_specialized_metric(
    model_family, asof_date, metric_name, availability_status
);
"""

AVAILABILITY_COMPAT_COLUMNS = {
    "metric_value": "REAL",
    "unit": "TEXT",
    "source_id": "TEXT",
    "accession_number": "TEXT",
    "filing_date": "TEXT",
    "period_start": "TEXT",
    "period_end": "TEXT",
    "taxonomy": "TEXT",
    "concept_name": "TEXT",
    "extraction_method": "TEXT",
    "provenance_json": "TEXT",
}

FILING_INPUT_VIEW_SQL = """
CREATE VIEW sec_parser_filing_input AS
SELECT
    ticker,
    cik,
    accession_number,
    source_id,
    form_type,
    filing_date,
    report_date,
    acceptance_datetime AS accepted_at,
    primary_document,
    primary_doc_description,
    fiscal_year,
    fiscal_period,
    is_amendment,
    created_at,
    updated_at
FROM fact_sec_filing
"""

FINANCIAL_FACT_INPUT_VIEW_SQL = """
CREATE VIEW sec_parser_financial_fact_input AS
SELECT
    f.fact_key,
    f.ticker,
    f.cik,
    f.taxonomy,
    f.concept AS concept_name,
    f.metric_name AS canonical_metric,
    f.unit,
    f.accession_number,
    f.source_id,
    f.form_type,
    f.filing_date,
    (
        SELECT MAX(s.acceptance_datetime)
        FROM fact_sec_filing AS s
        WHERE s.ticker = f.ticker
          AND s.accession_number = f.accession_number
    ) AS accepted_at,
    f.fiscal_year,
    f.fiscal_period,
    f.start_date AS period_start,
    f.end_date AS period_end,
    f.frame,
    f.value,
    f.decimals,
    f.created_at,
    f.updated_at
FROM fact_sec_xbrl_fact AS f
"""

LEGACY_DISCLOSURE_CANDIDATE_VIEW_SQL = """
CREATE VIEW fact_sec_metric_disclosure_candidate AS
SELECT
    CAST(NULL AS TEXT) AS candidate_key,
    CAST(NULL AS TEXT) AS ticker,
    CAST(NULL AS TEXT) AS cik,
    CAST(NULL AS TEXT) AS source_id,
    CAST(NULL AS TEXT) AS model_family,
    CAST(NULL AS TEXT) AS accession_number,
    CAST(NULL AS TEXT) AS form_type,
    CAST(NULL AS TEXT) AS filing_date,
    CAST(NULL AS TEXT) AS accepted_at,
    CAST(NULL AS TEXT) AS document_name,
    CAST(NULL AS TEXT) AS metric_name,
    CAST(NULL AS TEXT) AS concept_name,
    CAST(NULL AS REAL) AS candidate_value,
    CAST(NULL AS TEXT) AS unit,
    CAST(NULL AS TEXT) AS period_start,
    CAST(NULL AS TEXT) AS period_end,
    CAST(NULL AS TEXT) AS scope,
    CAST(NULL AS TEXT) AS extraction_method,
    CAST(NULL AS REAL) AS confidence,
    CAST(NULL AS TEXT) AS candidate_status,
    CAST(NULL AS TEXT) AS status_reason,
    CAST(NULL AS TEXT) AS evidence_text,
    CAST(NULL AS TEXT) AS provenance_json,
    CAST(NULL AS TEXT) AS created_at,
    CAST(NULL AS TEXT) AS updated_at
WHERE 0
"""

REQUIRED_TABLES = (
    "sec_parser_run",
    "sec_parser_document_catalog",
    "sec_parser_work_ledger",
    "sec_parser_normalized_fact_shadow",
    "sec_parser_metric_evidence_shadow",
    "feature_financial_metric_availability",
    "fact_technology_specialized_metric",
    "feature_technology_specialized_metric",
)
REQUIRED_VIEWS = (
    "sec_parser_filing_input",
    "sec_parser_financial_fact_input",
    "fact_sec_metric_disclosure_candidate",
)


def _replace_view(conn: sqlite3.Connection, view_name: str, view_sql: str) -> None:
    if view_name not in REQUIRED_VIEWS:
        raise ValueError(f"Unsupported technology parser view: {view_name}")
    conn.execute(f"DROP VIEW IF EXISTS {view_name}")
    conn.execute(view_sql)


def _missing_objects(
    conn: sqlite3.Connection,
    *,
    object_type: str,
    names: Iterable[str],
) -> list[str]:
    expected = set(names)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = ?",
        (object_type,),
    ).fetchall()
    actual = {str(row[0]) for row in rows}
    return sorted(expected - actual)


def _ensure_availability_compatibility(conn: sqlite3.Connection) -> None:
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(feature_financial_metric_availability)")
    }
    for name, declaration in AVAILABILITY_COMPAT_COLUMNS.items():
        if name not in columns:
            conn.execute(
                f"ALTER TABLE feature_financial_metric_availability "
                f"ADD COLUMN {name} {declaration}"
            )


def ensure_technology_parser_schema(conn: sqlite3.Connection) -> None:
    """Install shared shadow tables plus technology-owned compatibility objects."""
    ensure_shared_parser_schema(conn)
    conn.executescript(TECHNOLOGY_PARSER_SCHEMA_SQL)
    _ensure_availability_compatibility(conn)
    _replace_view(conn, "sec_parser_filing_input", FILING_INPUT_VIEW_SQL)
    _replace_view(
        conn,
        "sec_parser_financial_fact_input",
        FINANCIAL_FACT_INPUT_VIEW_SQL,
    )
    _replace_view(
        conn,
        "fact_sec_metric_disclosure_candidate",
        LEGACY_DISCLOSURE_CANDIDATE_VIEW_SQL,
    )


def validate_technology_parser_schema(conn: sqlite3.Connection) -> None:
    missing_tables = _missing_objects(
        conn,
        object_type="table",
        names=REQUIRED_TABLES,
    )
    missing_views = _missing_objects(
        conn,
        object_type="view",
        names=REQUIRED_VIEWS,
    )
    if missing_tables or missing_views:
        raise RuntimeError(
            "Technology dedicated-parser schema is incomplete: "
            f"missing_tables={missing_tables}, missing_views={missing_views}"
        )

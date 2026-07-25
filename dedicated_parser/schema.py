from __future__ import annotations

import sqlite3


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sec_parser_run (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_family TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    parser_release TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    mode TEXT NOT NULL,
    worker_count INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    planned_work_count INTEGER NOT NULL DEFAULT 0,
    completed_work_count INTEGER NOT NULL DEFAULT 0,
    failed_work_count INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS sec_parser_document_catalog (
    cik TEXT NOT NULL,
    accession_number TEXT NOT NULL,
    document_name TEXT NOT NULL,
    ticker TEXT NOT NULL,
    form_type TEXT NOT NULL,
    filing_date TEXT,
    accepted_at TEXT,
    report_date TEXT,
    source_path TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    modified_ns INTEGER NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 0,
    is_full_submission INTEGER NOT NULL DEFAULT 0,
    source_kind TEXT NOT NULL,
    cataloged_at TEXT NOT NULL,
    PRIMARY KEY(cik, accession_number, document_name, content_sha256)
);

CREATE INDEX IF NOT EXISTS idx_sec_parser_document_catalog_lookup
ON sec_parser_document_catalog(ticker, accession_number, document_name);

CREATE TABLE IF NOT EXISTS sec_parser_work_ledger (
    work_key TEXT PRIMARY KEY,
    run_id INTEGER NOT NULL,
    model_family TEXT NOT NULL,
    ticker TEXT NOT NULL,
    cik TEXT NOT NULL,
    accession_number TEXT NOT NULL,
    parser_release TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    requested_metrics_json TEXT NOT NULL,
    input_hashes_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    normalized_fact_count INTEGER NOT NULL DEFAULT 0,
    evidence_count INTEGER NOT NULL DEFAULT 0,
    elapsed_seconds REAL NOT NULL DEFAULT 0.0,
    error TEXT,
    started_at TEXT,
    completed_at TEXT,
    FOREIGN KEY(run_id) REFERENCES sec_parser_run(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sec_parser_work_ledger_resume
ON sec_parser_work_ledger(model_family, parser_release, adapter_version, status);

CREATE TABLE IF NOT EXISTS sec_parser_run_work (
    run_id INTEGER NOT NULL,
    work_key TEXT NOT NULL,
    ticker TEXT NOT NULL,
    accession_number TEXT NOT NULL,
    PRIMARY KEY(run_id, work_key),
    FOREIGN KEY(run_id) REFERENCES sec_parser_run(run_id) ON DELETE CASCADE,
    FOREIGN KEY(work_key) REFERENCES sec_parser_work_ledger(work_key) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sec_parser_normalized_fact_shadow (
    fact_fingerprint TEXT PRIMARY KEY,
    run_id INTEGER NOT NULL,
    work_key TEXT NOT NULL,
    ticker TEXT NOT NULL,
    cik TEXT NOT NULL,
    accession_number TEXT NOT NULL,
    form_type TEXT NOT NULL,
    filing_date TEXT,
    accepted_at TEXT,
    report_date TEXT,
    taxonomy TEXT NOT NULL,
    concept_name TEXT NOT NULL,
    value_text TEXT,
    numeric_value REAL,
    unit TEXT,
    period_start TEXT,
    period_end TEXT,
    context_id TEXT,
    dimensions_json TEXT NOT NULL DEFAULT '{}',
    scope TEXT NOT NULL,
    source_document TEXT NOT NULL,
    provider TEXT NOT NULL,
    decimals TEXT,
    concept_metadata_json TEXT NOT NULL DEFAULT '{}',
    parser_release TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES sec_parser_run(run_id) ON DELETE CASCADE,
    FOREIGN KEY(work_key) REFERENCES sec_parser_work_ledger(work_key) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sec_parser_normalized_fact_shadow_lookup
ON sec_parser_normalized_fact_shadow(ticker, concept_name, period_end, accepted_at);

CREATE TABLE IF NOT EXISTS sec_parser_run_normalized_fact (
    run_id INTEGER NOT NULL,
    fact_fingerprint TEXT NOT NULL,
    PRIMARY KEY(run_id, fact_fingerprint),
    FOREIGN KEY(run_id) REFERENCES sec_parser_run(run_id) ON DELETE CASCADE,
    FOREIGN KEY(fact_fingerprint)
        REFERENCES sec_parser_normalized_fact_shadow(fact_fingerprint)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sec_parser_metric_evidence_shadow (
    evidence_key TEXT PRIMARY KEY,
    run_id INTEGER NOT NULL,
    work_key TEXT NOT NULL,
    model_family TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    ticker TEXT NOT NULL,
    cik TEXT NOT NULL,
    accession_number TEXT NOT NULL,
    form_type TEXT NOT NULL,
    filing_date TEXT,
    accepted_at TEXT,
    report_date TEXT,
    metric_name TEXT NOT NULL,
    concept_name TEXT NOT NULL,
    candidate_value REAL,
    unit TEXT,
    period_start TEXT,
    period_end TEXT,
    scope TEXT NOT NULL,
    confidence REAL NOT NULL,
    candidate_status TEXT NOT NULL,
    status_reason TEXT,
    evidence_text TEXT,
    source_document TEXT NOT NULL,
    extraction_method TEXT NOT NULL,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    parser_release TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES sec_parser_run(run_id) ON DELETE CASCADE,
    FOREIGN KEY(work_key) REFERENCES sec_parser_work_ledger(work_key) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sec_parser_metric_evidence_shadow_lookup
ON sec_parser_metric_evidence_shadow(
    model_family, ticker, metric_name, period_end, candidate_status
);

CREATE TABLE IF NOT EXISTS sec_parser_run_metric_evidence (
    run_id INTEGER NOT NULL,
    evidence_key TEXT NOT NULL,
    PRIMARY KEY(run_id, evidence_key),
    FOREIGN KEY(run_id) REFERENCES sec_parser_run(run_id) ON DELETE CASCADE,
    FOREIGN KEY(evidence_key)
        REFERENCES sec_parser_metric_evidence_shadow(evidence_key)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sec_parser_shadow_comparison (
    run_id INTEGER NOT NULL,
    model_family TEXT NOT NULL,
    ticker TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    legacy_accepted_count INTEGER NOT NULL,
    shadow_accepted_count INTEGER NOT NULL,
    matched_count INTEGER NOT NULL,
    legacy_only_count INTEGER NOT NULL,
    shadow_only_count INTEGER NOT NULL,
    comparison_status TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, model_family, ticker, metric_name),
    FOREIGN KEY(run_id) REFERENCES sec_parser_run(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sec_parser_recovery_assessment (
    run_id INTEGER NOT NULL,
    model_family TEXT NOT NULL,
    ticker TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    baseline_status TEXT NOT NULL,
    baseline_value REAL,
    anchor_period_end TEXT,
    recovery_class TEXT NOT NULL,
    predicted_status TEXT NOT NULL,
    accepted_current_count INTEGER NOT NULL DEFAULT 0,
    accepted_historical_count INTEGER NOT NULL DEFAULT 0,
    review_required_count INTEGER NOT NULL DEFAULT 0,
    rejected_count INTEGER NOT NULL DEFAULT 0,
    parser_failure_count INTEGER NOT NULL DEFAULT 0,
    searched_filing_count INTEGER NOT NULL DEFAULT 0,
    searched_document_count INTEGER NOT NULL DEFAULT 0,
    failed_filing_count INTEGER NOT NULL DEFAULT 0,
    missing_cache_filing_count INTEGER NOT NULL DEFAULT 0,
    evidence_keys_json TEXT NOT NULL DEFAULT '[]',
    status_reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, model_family, ticker, metric_name),
    FOREIGN KEY(run_id) REFERENCES sec_parser_run(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sec_parser_recovery_assessment_lookup
ON sec_parser_recovery_assessment(
    model_family, asof_date, metric_name, recovery_class
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    fact_columns = {
        str(row[1])
        for row in conn.execute(
            "PRAGMA table_info(sec_parser_normalized_fact_shadow)"
        )
    }
    if "concept_metadata_json" not in fact_columns:
        conn.execute(
            """
            ALTER TABLE sec_parser_normalized_fact_shadow
            ADD COLUMN concept_metadata_json TEXT NOT NULL DEFAULT '{}'
            """
        )
    assessment_columns = {
        str(row[1])
        for row in conn.execute(
            "PRAGMA table_info(sec_parser_recovery_assessment)"
        )
    }
    if "parser_failure_count" not in assessment_columns:
        conn.execute(
            """
            ALTER TABLE sec_parser_recovery_assessment
            ADD COLUMN parser_failure_count INTEGER NOT NULL DEFAULT 0
            """
        )
    if "missing_cache_filing_count" not in assessment_columns:
        conn.execute(
            """
            ALTER TABLE sec_parser_recovery_assessment
            ADD COLUMN missing_cache_filing_count INTEGER NOT NULL DEFAULT 0
            """
        )

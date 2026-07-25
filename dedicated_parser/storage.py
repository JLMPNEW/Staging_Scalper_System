from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from dedicated_parser.contracts import (
    DOCUMENT_PARSER_RELEASE,
    DocumentRef,
    WorkItem,
    WorkResult,
)
from dedicated_parser.schema import ensure_schema


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def connect_database(
    path: Path,
    *,
    timeout_seconds: float = 120.0,
    readonly: bool = False,
) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(
            f"file:{path.resolve().as_posix()}?mode=ro",
            uri=True,
            timeout=timeout_seconds,
        )
    else:
        conn = sqlite3.connect(path, timeout=timeout_seconds)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(timeout_seconds * 1000)}")
    conn.execute("PRAGMA foreign_keys = ON")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        ensure_schema(conn)
    return conn


def start_run(
    conn: sqlite3.Connection,
    *,
    model_family: str,
    asof_date: str,
    adapter_version: str,
    mode: str,
    worker_count: int,
    metadata: dict[str, Any] | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO sec_parser_run(
            model_family, asof_date, parser_release, adapter_version,
            mode, worker_count, started_at, status, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'RUNNING', ?)
        """,
        (
            model_family,
            asof_date,
            DOCUMENT_PARSER_RELEASE,
            adapter_version,
            mode,
            worker_count,
            utc_now(),
            json.dumps(metadata or {}, sort_keys=True, separators=(",", ":")),
        ),
    )
    conn.commit()
    if cursor.lastrowid is None:
        raise RuntimeError("Failed to allocate sec_parser_run row")
    return int(cursor.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    status: str,
    planned: int,
    completed: int,
    failed: int,
    metadata: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        UPDATE sec_parser_run
        SET completed_at = ?, status = ?, planned_work_count = ?,
            completed_work_count = ?, failed_work_count = ?, metadata_json = ?
        WHERE run_id = ?
        """,
        (
            utc_now(),
            status,
            planned,
            completed,
            failed,
            json.dumps(metadata or {}, sort_keys=True, separators=(",", ":")),
            run_id,
        ),
    )
    conn.commit()


def load_run(conn: sqlite3.Connection, *, run_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM sec_parser_run WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Dedicated-parser run_id={run_id} does not exist")
    return dict(row)


def merge_run_metadata(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    updates: dict[str, Any],
) -> dict[str, Any]:
    run = load_run(conn, run_id=run_id)
    try:
        metadata = json.loads(str(run.get("metadata_json") or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Dedicated-parser run_id={run_id} has invalid metadata_json"
        ) from exc
    if not isinstance(metadata, dict):
        raise ValueError(
            f"Dedicated-parser run_id={run_id} metadata_json is not an object"
        )
    metadata.update(updates)
    conn.execute(
        "UPDATE sec_parser_run SET metadata_json = ? WHERE run_id = ?",
        (
            json.dumps(metadata, sort_keys=True, separators=(",", ":")),
            run_id,
        ),
    )
    conn.commit()
    return metadata


def catalog_documents(
    conn: sqlite3.Connection,
    *,
    filing: Any,
    documents: Iterable[DocumentRef],
) -> None:
    now = utc_now()
    conn.executemany(
        """
        INSERT INTO sec_parser_document_catalog(
            cik, accession_number, document_name, ticker, form_type,
            filing_date, accepted_at, report_date, source_path,
            content_sha256, file_size, modified_ns, is_primary,
            is_full_submission, source_kind, cataloged_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cik, accession_number, document_name, content_sha256)
        DO UPDATE SET
            ticker = excluded.ticker,
            form_type = excluded.form_type,
            filing_date = excluded.filing_date,
            accepted_at = excluded.accepted_at,
            report_date = excluded.report_date,
            source_path = excluded.source_path,
            file_size = excluded.file_size,
            modified_ns = excluded.modified_ns,
            is_primary = excluded.is_primary,
            is_full_submission = excluded.is_full_submission,
            source_kind = excluded.source_kind,
            cataloged_at = excluded.cataloged_at
        """,
        [
            (
                filing.cik,
                filing.accession_number,
                document.name,
                filing.ticker,
                filing.form_type,
                filing.filing_date,
                filing.accepted_at,
                filing.report_date,
                document.path,
                document.content_sha256,
                document.file_size,
                document.modified_ns,
                int(document.is_primary),
                int(document.is_full_submission),
                document.source_kind,
                now,
            )
            for document in documents
        ],
    )


def completed_work_keys(
    conn: sqlite3.Connection,
    *,
    model_family: str,
    adapter_version: str,
) -> set[str]:
    return {
        str(row["work_key"])
        for row in conn.execute(
            """
            SELECT work_key
            FROM sec_parser_work_ledger
            WHERE model_family = ? AND parser_release = ?
              AND adapter_version = ? AND status = 'COMPLETED'
            """,
            (model_family, DOCUMENT_PARSER_RELEASE, adapter_version),
        )
    }


def register_work(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    item: WorkItem,
) -> None:
    conn.execute(
        """
        INSERT INTO sec_parser_work_ledger(
            work_key, run_id, model_family, ticker, cik, accession_number,
            parser_release, adapter_version, requested_metrics_json,
            input_hashes_json, status, attempt_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0)
        ON CONFLICT(work_key) DO UPDATE SET
            run_id = excluded.run_id,
            requested_metrics_json = excluded.requested_metrics_json,
            input_hashes_json = excluded.input_hashes_json,
            status = CASE
                WHEN sec_parser_work_ledger.status = 'COMPLETED' THEN 'COMPLETED'
                ELSE 'PENDING'
            END,
            error = NULL
        """,
        (
            item.work_key,
            run_id,
            item.model_family,
            item.filing.ticker,
            item.filing.cik,
            item.filing.accession_number,
            item.parser_release,
            item.adapter_version,
            json.dumps(
                [asdict(metric) for metric in item.requested_metrics],
                sort_keys=True,
                separators=(",", ":"),
            ),
            json.dumps(
                {
                    document.name: document.content_sha256
                    for document in item.documents
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO sec_parser_run_work(
            run_id, work_key, ticker, accession_number
        ) VALUES (?, ?, ?, ?)
        """,
        (
            run_id,
            item.work_key,
            item.filing.ticker,
            item.filing.accession_number,
        ),
    )


def mark_work_started(
    conn: sqlite3.Connection,
    *,
    item: WorkItem,
) -> None:
    conn.execute(
        """
        UPDATE sec_parser_work_ledger
        SET status = 'RUNNING', attempt_count = attempt_count + 1,
            started_at = ?, completed_at = NULL, error = NULL
        WHERE work_key = ?
        """,
        (utc_now(), item.work_key),
    )


def persist_result(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    result: WorkResult,
) -> None:
    now = utc_now()
    filing = result.filing
    fact_rows = [
        (
            fact.fingerprint(filing=filing),
            run_id,
            result.work_key,
            filing.ticker,
            filing.cik,
            filing.accession_number,
            filing.form_type,
            filing.filing_date,
            filing.accepted_at,
            filing.report_date,
            fact.taxonomy,
            fact.concept_name,
            fact.value_text,
            fact.numeric_value,
            fact.unit,
            fact.period_start,
            fact.period_end,
            fact.context_id,
            fact.dimensions_json,
            fact.scope,
            fact.source_document,
            fact.provider,
            fact.decimals,
            fact.concept_metadata_json,
            result.parser_release,
            now,
        )
        for fact in result.normalized_facts
    ]
    conn.executemany(
        """
        INSERT INTO sec_parser_normalized_fact_shadow(
            fact_fingerprint, run_id, work_key, ticker, cik,
            accession_number, form_type, filing_date, accepted_at,
            report_date, taxonomy, concept_name, value_text, numeric_value,
            unit, period_start, period_end, context_id, dimensions_json,
            scope, source_document, provider, decimals,
            concept_metadata_json, parser_release, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(fact_fingerprint) DO UPDATE SET
            run_id = excluded.run_id,
            work_key = excluded.work_key,
            concept_metadata_json = excluded.concept_metadata_json,
            parser_release = excluded.parser_release,
            created_at = excluded.created_at
        """,
        fact_rows,
    )
    conn.executemany(
        """
        INSERT OR IGNORE INTO sec_parser_run_normalized_fact(
            run_id, fact_fingerprint
        ) VALUES (?, ?)
        """,
        [(run_id, row[0]) for row in fact_rows],
    )
    evidence_rows = [
        (
            evidence.evidence_key(
                model_family=result.model_family,
                filing=filing,
            ),
            run_id,
            result.work_key,
            result.model_family,
            result.adapter_version,
            filing.ticker,
            filing.cik,
            filing.accession_number,
            filing.form_type,
            filing.filing_date,
            filing.accepted_at,
            filing.report_date,
            evidence.metric_name,
            evidence.concept_name,
            evidence.value,
            evidence.unit,
            evidence.period_start,
            evidence.period_end,
            evidence.scope,
            evidence.confidence,
            evidence.status,
            evidence.reason,
            evidence.evidence_text,
            evidence.source_document,
            evidence.extraction_method,
            json.dumps(
                evidence.provenance,
                sort_keys=True,
                separators=(",", ":"),
            ),
            result.parser_release,
            now,
        )
        for evidence in result.metric_evidence
    ]
    conn.executemany(
        """
        INSERT INTO sec_parser_metric_evidence_shadow(
            evidence_key, run_id, work_key, model_family, adapter_version,
            ticker, cik, accession_number, form_type, filing_date,
            accepted_at, report_date, metric_name, concept_name,
            candidate_value, unit, period_start, period_end, scope,
            confidence, candidate_status, status_reason, evidence_text,
            source_document, extraction_method, provenance_json,
            parser_release, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(evidence_key) DO UPDATE SET
            run_id = excluded.run_id,
            work_key = excluded.work_key,
            confidence = excluded.confidence,
            candidate_status = excluded.candidate_status,
            status_reason = excluded.status_reason,
            provenance_json = excluded.provenance_json,
            parser_release = excluded.parser_release,
            created_at = excluded.created_at
        """,
        evidence_rows,
    )
    conn.executemany(
        """
        INSERT OR IGNORE INTO sec_parser_run_metric_evidence(
            run_id, evidence_key
        ) VALUES (?, ?)
        """,
        [(run_id, row[0]) for row in evidence_rows],
    )
    conn.execute(
        """
        UPDATE sec_parser_work_ledger
        SET status = ?, normalized_fact_count = ?, evidence_count = ?,
            elapsed_seconds = ?, error = ?, completed_at = ?
        WHERE work_key = ?
        """,
        (
            result.status,
            len(result.normalized_facts),
            len(result.metric_evidence),
            result.elapsed_seconds,
            result.error or None,
            now,
            result.work_key,
        ),
    )

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from dedicated_parser.contracts import (
    DocumentRef,
    FilingRef,
    MetricEvidence,
    MetricRequest,
    WorkItem,
    file_sha256,
    stable_hash,
)
from dedicated_parser.policy import apply_review_policies, load_review_policies
from dedicated_parser.storage import load_run, utc_now


REVIEW_EVALUATION_CONTRACT_VERSION = "review_evaluation_v1"

_REPLAY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sec_parser_review_evaluation (
    evaluation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    base_run_id INTEGER NOT NULL,
    model_family TEXT NOT NULL,
    adapter_path TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    policy_path TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL,
    evaluation_contract_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    base_scope_hash_before TEXT NOT NULL,
    base_scope_hash_after TEXT,
    base_evidence_count INTEGER NOT NULL DEFAULT 0,
    evaluated_evidence_count INTEGER NOT NULL DEFAULT 0,
    changed_evidence_count INTEGER NOT NULL DEFAULT 0,
    materialized_evidence_count INTEGER NOT NULL DEFAULT 0,
    applied_policy_count INTEGER NOT NULL DEFAULT 0,
    source_document_open_count INTEGER NOT NULL DEFAULT 0,
    arelle_invocation_count INTEGER NOT NULL DEFAULT 0,
    edgartools_invocation_count INTEGER NOT NULL DEFAULT 0,
    ocr_invocation_count INTEGER NOT NULL DEFAULT 0,
    materialized_run_id INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(base_run_id, policy_sha256, evaluation_contract_version),
    FOREIGN KEY(base_run_id) REFERENCES sec_parser_run(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sec_parser_review_evaluation_lookup
ON sec_parser_review_evaluation(
    model_family, base_run_id, policy_sha256, status
);

CREATE TABLE IF NOT EXISTS sec_parser_review_evidence (
    evaluation_id INTEGER NOT NULL,
    evaluated_evidence_key TEXT NOT NULL,
    base_evidence_key TEXT,
    work_key TEXT NOT NULL,
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
    policy_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(evaluation_id, evaluated_evidence_key),
    FOREIGN KEY(evaluation_id)
        REFERENCES sec_parser_review_evaluation(evaluation_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sec_parser_review_evidence_lookup
ON sec_parser_review_evidence(
    evaluation_id, ticker, metric_name, period_end, candidate_status
);

CREATE INDEX IF NOT EXISTS idx_sec_parser_review_evidence_base
ON sec_parser_review_evidence(evaluation_id, base_evidence_key);
"""

_EVIDENCE_FIELDS = (
    "metric_name",
    "concept_name",
    "value",
    "unit",
    "period_start",
    "period_end",
    "scope",
    "confidence",
    "status",
    "reason",
    "evidence_text",
    "source_document",
    "extraction_method",
    "provenance",
)


@dataclass(frozen=True)
class ReviewReplaySummary:
    evaluation_id: int
    base_run_id: int
    model_family: str
    policy_sha256: str
    evaluation_contract_version: str
    status: str
    base_scope_hash_before: str
    base_scope_hash_after: str
    base_evidence_count: int
    evaluated_evidence_count: int
    changed_evidence_count: int
    materialized_evidence_count: int
    applied_policy_count: int
    source_document_open_count: int
    arelle_invocation_count: int
    edgartools_invocation_count: int
    ocr_invocation_count: int
    idempotent_reuse: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def ensure_review_replay_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_REPLAY_SCHEMA_SQL)
    columns = {
        str(row[1])
        for row in conn.execute(
            'PRAGMA table_info(sec_parser_review_evaluation)'
        )
    }
    if 'materialized_run_id' not in columns:
        conn.execute(
            'ALTER TABLE sec_parser_review_evaluation '
            'ADD COLUMN materialized_run_id INTEGER'
        )
        conn.commit()


def _json_object(value: object, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(str(value or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _json_array(value: object, *, label: str) -> list[Any]:
    try:
        payload = json.loads(str(value or "[]"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(payload, list):
        raise ValueError(f"{label} must be a JSON array")
    return payload


def _run_work_rows(
    conn: sqlite3.Connection,
    *,
    base_run_id: int,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT ledger.*
            FROM sec_parser_run_work AS relation
            JOIN sec_parser_work_ledger AS ledger
              ON ledger.work_key = relation.work_key
            WHERE relation.run_id = ?
            ORDER BY ledger.work_key
            """,
            (base_run_id,),
        )
    ]


def _base_evidence_rows(
    conn: sqlite3.Connection,
    *,
    base_run_id: int,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT evidence.*
            FROM sec_parser_run_metric_evidence AS relation
            JOIN sec_parser_metric_evidence_shadow AS evidence
              ON evidence.evidence_key = relation.evidence_key
            WHERE relation.run_id = ?
            ORDER BY evidence.work_key, evidence.evidence_key
            """,
            (base_run_id,),
        )
    ]


def base_run_scope_hash(
    conn: sqlite3.Connection,
    *,
    base_run_id: int,
) -> str:
    work = _run_work_rows(conn, base_run_id=base_run_id)
    evidence = _base_evidence_rows(conn, base_run_id=base_run_id)
    work_payload = [
        {
            "work_key": row["work_key"],
            "model_family": row["model_family"],
            "ticker": row["ticker"],
            "cik": row["cik"],
            "accession_number": row["accession_number"],
            "parser_release": row["parser_release"],
            "adapter_version": row["adapter_version"],
            "requested_metrics_json": row["requested_metrics_json"],
            "input_hashes_json": row["input_hashes_json"],
            "status": row["status"],
        }
        for row in work
    ]
    evidence_payload = [
        {
            key: row.get(key)
            for key in (
                "evidence_key",
                "work_key",
                "model_family",
                "adapter_version",
                "ticker",
                "cik",
                "accession_number",
                "form_type",
                "filing_date",
                "accepted_at",
                "report_date",
                "metric_name",
                "concept_name",
                "candidate_value",
                "unit",
                "period_start",
                "period_end",
                "scope",
                "confidence",
                "candidate_status",
                "status_reason",
                "evidence_text",
                "source_document",
                "extraction_method",
                "provenance_json",
                "parser_release",
            )
        }
        for row in evidence
    ]
    return stable_hash({"work": work_payload, "evidence": evidence_payload})


def _metric_requests(row: Mapping[str, Any]) -> tuple[MetricRequest, ...]:
    requests: list[MetricRequest] = []
    for item in _json_array(
        row.get("requested_metrics_json"),
        label=f"{row.get('work_key')}: requested_metrics_json",
    ):
        if not isinstance(item, dict):
            raise ValueError(f"{row.get('work_key')}: requested metric must be an object")
        metric_name = str(item.get("metric_name") or "").strip()
        if not metric_name:
            raise ValueError(f"{row.get('work_key')}: requested metric_name is required")
        patterns = item.get("concept_patterns") or ()
        if not isinstance(patterns, (list, tuple)):
            raise ValueError(f"{row.get('work_key')}: concept_patterns must be an array")
        requests.append(
            MetricRequest(
                metric_name,
                tuple(str(pattern) for pattern in patterns),
            )
        )
    return tuple(requests)


def _documents_for_work(
    conn: sqlite3.Connection,
    *,
    row: Mapping[str, Any],
) -> tuple[DocumentRef, ...]:
    hashes = _json_object(
        row.get("input_hashes_json"),
        label=f"{row.get('work_key')}: input_hashes_json",
    )
    documents: list[DocumentRef] = []
    for name, content_hash in sorted(hashes.items()):
        catalog = conn.execute(
            """
            SELECT *
            FROM sec_parser_document_catalog
            WHERE cik = ?
              AND accession_number = ?
              AND document_name = ?
              AND content_sha256 = ?
            ORDER BY is_primary DESC, is_full_submission DESC, cataloged_at DESC
            LIMIT 1
            """,
            (
                row["cik"],
                row["accession_number"],
                str(name),
                str(content_hash),
            ),
        ).fetchone()
        if catalog is None:
            raise ValueError(
                "Policy replay cannot reconstruct sealed document metadata: "
                f"{row['ticker']} {row['accession_number']} {name} "
                f"sha256={content_hash}"
            )
        documents.append(
            DocumentRef(
                name=str(catalog["document_name"]),
                path=str(catalog["source_path"]),
                content_sha256=str(catalog["content_sha256"]),
                file_size=int(catalog["file_size"]),
                modified_ns=int(catalog["modified_ns"]),
                is_primary=bool(catalog["is_primary"]),
                is_full_submission=bool(catalog["is_full_submission"]),
                source_kind=str(catalog["source_kind"]),
            )
        )
    return tuple(documents)


def _work_item(
    conn: sqlite3.Connection,
    *,
    row: Mapping[str, Any],
    adapter_path: str,
    policy_path: Path,
    policy_sha256: str,
) -> WorkItem:
    documents = _documents_for_work(conn, row=row)
    if not documents:
        raise ValueError(f"{row['work_key']}: base work has no sealed documents")
    catalog = conn.execute(
        """
        SELECT *
        FROM sec_parser_document_catalog
        WHERE cik = ? AND accession_number = ?
        ORDER BY is_primary DESC, is_full_submission ASC, cataloged_at DESC
        LIMIT 1
        """,
        (row["cik"], row["accession_number"]),
    ).fetchone()
    if catalog is None:
        raise ValueError(f"{row['work_key']}: base work has no filing catalog row")
    primary = next(
        (document.name for document in documents if document.is_primary),
        documents[0].name,
    )
    return WorkItem(
        model_family=str(row["model_family"]),
        adapter_path=adapter_path,
        adapter_version=str(row["adapter_version"]),
        filing=FilingRef(
            ticker=str(row["ticker"]),
            cik=str(row["cik"]),
            accession_number=str(row["accession_number"]),
            form_type=str(catalog["form_type"]),
            filing_date=str(catalog["filing_date"] or ""),
            accepted_at=str(catalog["accepted_at"] or ""),
            report_date=str(catalog["report_date"] or ""),
            primary_document=primary,
            source_id="dedicated_parser_base_run",
        ),
        documents=documents,
        requested_metrics=_metric_requests(row),
        review_policy_path=str(policy_path),
        review_policy_sha256=policy_sha256,
        parser_release=str(row["parser_release"]),
        # Policy replay must never invoke normalized providers or OCR.
        enable_arelle=False,
        enable_edgartools=False,
        enable_pdf_ocr=False,
    )


def _metric_evidence(row: Mapping[str, Any]) -> MetricEvidence:
    provenance = _json_object(
        row.get("provenance_json"),
        label=f"{row.get('evidence_key')}: provenance_json",
    )
    if (
        "review_policy" in provenance
        or str(row.get("extraction_method") or "") == "dedicated_parser:review_policy_registry"
    ):
        raise ValueError(f"Base run is not immutable pre-policy evidence: evidence_key={row.get('evidence_key')}")
    value = row.get("candidate_value")
    return MetricEvidence(
        metric_name=str(row["metric_name"]),
        concept_name=str(row["concept_name"]),
        value=float(value) if value is not None else None,
        unit=str(row.get("unit") or ""),
        period_start=str(row.get("period_start") or ""),
        period_end=str(row.get("period_end") or ""),
        scope=str(row.get("scope") or ""),
        confidence=float(row["confidence"]),
        status=str(row["candidate_status"]),
        reason=str(row.get("status_reason") or ""),
        evidence_text=str(row.get("evidence_text") or ""),
        source_document=str(row["source_document"]),
        extraction_method=str(row["extraction_method"]),
        provenance=provenance,
    )


def _evidence_changed(
    base: MetricEvidence,
    evaluated: MetricEvidence,
) -> bool:
    return any(getattr(base, field) != getattr(evaluated, field) for field in _EVIDENCE_FIELDS)


def _policy_id(evidence: MetricEvidence) -> str:
    policy = evidence.provenance.get("review_policy")
    return str(policy.get("policy_id") or "") if isinstance(policy, dict) else ""


def _evaluation_key(
    *,
    base_run_id: int,
    policy_sha256: str,
    work_key: str,
    base_evidence_key: str,
    evidence: MetricEvidence,
) -> str:
    return stable_hash(
        {
            "contract_version": REVIEW_EVALUATION_CONTRACT_VERSION,
            "base_run_id": base_run_id,
            "policy_sha256": policy_sha256,
            "work_key": work_key,
            "base_evidence_key": base_evidence_key,
            "evidence": asdict(evidence),
        }
    )


def _summary(
    row: Mapping[str, Any],
    *,
    idempotent_reuse: bool,
) -> ReviewReplaySummary:
    return ReviewReplaySummary(
        evaluation_id=int(row["evaluation_id"]),
        base_run_id=int(row["base_run_id"]),
        model_family=str(row["model_family"]),
        policy_sha256=str(row["policy_sha256"]),
        evaluation_contract_version=str(row["evaluation_contract_version"]),
        status=str(row["status"]),
        base_scope_hash_before=str(row["base_scope_hash_before"]),
        base_scope_hash_after=str(row.get("base_scope_hash_after") or ""),
        base_evidence_count=int(row["base_evidence_count"]),
        evaluated_evidence_count=int(row["evaluated_evidence_count"]),
        changed_evidence_count=int(row["changed_evidence_count"]),
        materialized_evidence_count=int(row["materialized_evidence_count"]),
        applied_policy_count=int(row["applied_policy_count"]),
        source_document_open_count=int(row["source_document_open_count"]),
        arelle_invocation_count=int(row["arelle_invocation_count"]),
        edgartools_invocation_count=int(row["edgartools_invocation_count"]),
        ocr_invocation_count=int(row["ocr_invocation_count"]),
        idempotent_reuse=idempotent_reuse,
    )


def replay_review_policies(
    conn: sqlite3.Connection,
    *,
    base_run_id: int,
    adapter_path: str,
    policy_path: Path,
    expected_model_family: str = "",
) -> ReviewReplaySummary:
    ensure_review_replay_schema(conn)
    run = load_run(conn, run_id=base_run_id)
    model_family = str(run["model_family"])
    if expected_model_family and model_family != expected_model_family:
        raise ValueError(f"base_run_id={base_run_id} belongs to {model_family!r}, not {expected_model_family!r}")
    resolved_policy = policy_path.expanduser().resolve()
    policy_sha256 = file_sha256(resolved_policy)
    policies = load_review_policies(
        resolved_policy,
        expected_sha256=policy_sha256,
    )
    wrong_family = sorted({policy.model_family for policy in policies if policy.model_family != model_family})
    if wrong_family:
        raise ValueError(f"Review policy registry contains other model families: {wrong_family}")
    existing = conn.execute(
        """
        SELECT *
        FROM sec_parser_review_evaluation
        WHERE base_run_id = ?
          AND policy_sha256 = ?
          AND evaluation_contract_version = ?
        """,
        (
            base_run_id,
            policy_sha256,
            REVIEW_EVALUATION_CONTRACT_VERSION,
        ),
    ).fetchone()
    if existing is not None and str(existing["status"]) == "COMPLETED":
        current_hash = base_run_scope_hash(conn, base_run_id=base_run_id)
        sealed_hash = str(existing["base_scope_hash_after"] or "")
        if current_hash != sealed_hash:
            raise RuntimeError("Base run scope changed after the completed review evaluation")
        return _summary(dict(existing), idempotent_reuse=True)

    work_rows = _run_work_rows(conn, base_run_id=base_run_id)
    if not work_rows:
        raise ValueError(f"base_run_id={base_run_id} has no linked work")
    incomplete = [str(row["work_key"]) for row in work_rows if str(row["status"]) != "COMPLETED"]
    if incomplete:
        raise ValueError(f"base_run_id={base_run_id} has incomplete work: {incomplete[:10]}")
    base_rows = _base_evidence_rows(conn, base_run_id=base_run_id)
    base_by_work: dict[str, list[dict[str, Any]]] = {}
    for row in base_rows:
        base_by_work.setdefault(str(row["work_key"]), []).append(row)
        # Validate that the selected base run is genuinely pre-policy before
        # allocating an evaluation row.
        _metric_evidence(row)

    work_items = {
        str(row["work_key"]): _work_item(
            conn,
            row=row,
            adapter_path=adapter_path,
            policy_path=resolved_policy,
            policy_sha256=policy_sha256,
        )
        for row in work_rows
    }
    policy_scope = {
        (
            item.filing.ticker,
            item.filing.accession_number,
            document.name,
            request.metric_name,
        )
        for item in work_items.values()
        for document in item.documents
        for request in item.requested_metrics
    }
    outside_scope = [
        policy.policy_id
        for policy in policies
        if (
            policy.ticker,
            policy.accession_number,
            policy.source_document,
            policy.metric_name,
        )
        not in policy_scope
    ]
    if outside_scope:
        raise ValueError(
            f"Review policies target documents or metrics outside the sealed base run: {outside_scope[:10]}"
        )

    before_hash = base_run_scope_hash(conn, base_run_id=base_run_id)
    now = utc_now()
    if existing is None:
        cursor = conn.execute(
            """
            INSERT INTO sec_parser_review_evaluation(
                base_run_id, model_family, adapter_path, adapter_version,
                policy_path, policy_sha256, evaluation_contract_version,
                started_at, status, base_scope_hash_before,
                base_evidence_count, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'RUNNING', ?, ?, ?)
            """,
            (
                base_run_id,
                model_family,
                adapter_path,
                str(run["adapter_version"]),
                str(resolved_policy),
                policy_sha256,
                REVIEW_EVALUATION_CONTRACT_VERSION,
                now,
                before_hash,
                len(base_rows),
                json.dumps(
                    {"zero_provider_contract": True},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("Failed to allocate review evaluation")
        evaluation_id = int(cursor.lastrowid)
    else:
        evaluation_id = int(existing["evaluation_id"])
        conn.execute(
            """
            DELETE FROM sec_parser_review_evidence
            WHERE evaluation_id = ?
            """,
            (evaluation_id,),
        )
        conn.execute(
            """
            UPDATE sec_parser_review_evaluation
            SET adapter_path = ?, adapter_version = ?, policy_path = ?,
                started_at = ?, completed_at = NULL, status = 'RUNNING',
                base_scope_hash_before = ?, base_scope_hash_after = NULL,
                base_evidence_count = ?, evaluated_evidence_count = 0,
                changed_evidence_count = 0,
                materialized_evidence_count = 0,
                applied_policy_count = 0,
                source_document_open_count = 0,
                arelle_invocation_count = 0,
                edgartools_invocation_count = 0,
                ocr_invocation_count = 0,
                metadata_json = ?
            WHERE evaluation_id = ?
            """,
            (
                adapter_path,
                str(run["adapter_version"]),
                str(resolved_policy),
                now,
                before_hash,
                len(base_rows),
                json.dumps(
                    {"zero_provider_contract": True},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                evaluation_id,
            ),
        )

    evaluated_count = 0
    changed_count = 0
    materialized_count = 0
    applied_policy_ids: set[str] = set()
    insert_rows: list[tuple[Any, ...]] = []
    try:
        for work_key, item in work_items.items():
            source_rows = base_by_work.get(work_key, [])
            base_evidence = tuple(_metric_evidence(row) for row in source_rows)
            evaluated = apply_review_policies(item, base_evidence)
            if len(evaluated) < len(base_evidence):
                raise RuntimeError("Policy evaluation unexpectedly removed base evidence")
            for index, evidence in enumerate(evaluated):
                base_row = source_rows[index] if index < len(source_rows) else None
                base_key = str(base_row["evidence_key"]) if base_row is not None else ""
                if base_row is None:
                    materialized_count += 1
                elif _evidence_changed(base_evidence[index], evidence):
                    changed_count += 1
                policy_id = _policy_id(evidence)
                if policy_id:
                    applied_policy_ids.add(policy_id)
                evaluated_key = _evaluation_key(
                    base_run_id=base_run_id,
                    policy_sha256=policy_sha256,
                    work_key=work_key,
                    base_evidence_key=base_key,
                    evidence=evidence,
                )
                provenance_json = json.dumps(
                    evidence.provenance,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                insert_rows.append(
                    (
                        evaluation_id,
                        evaluated_key,
                        base_key or None,
                        work_key,
                        item.filing.ticker,
                        item.filing.cik,
                        item.filing.accession_number,
                        item.filing.form_type,
                        item.filing.filing_date,
                        item.filing.accepted_at,
                        item.filing.report_date,
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
                        provenance_json,
                        policy_id or None,
                        now,
                    )
                )
                evaluated_count += 1
        expected_policy_ids = {policy.policy_id for policy in policies}
        if applied_policy_ids != expected_policy_ids:
            raise RuntimeError(
                "Not every enabled policy produced an evaluated row: "
                f"applied={sorted(applied_policy_ids)} "
                f"expected={sorted(expected_policy_ids)}"
            )
        conn.executemany(
            """
            INSERT INTO sec_parser_review_evidence(
                evaluation_id, evaluated_evidence_key, base_evidence_key,
                work_key, ticker, cik, accession_number, form_type,
                filing_date, accepted_at, report_date, metric_name,
                concept_name, candidate_value, unit, period_start, period_end,
                scope, confidence, candidate_status, status_reason,
                evidence_text, source_document, extraction_method,
                provenance_json, policy_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            insert_rows,
        )
        after_hash = base_run_scope_hash(conn, base_run_id=base_run_id)
        if after_hash != before_hash:
            raise RuntimeError("Base run scope changed during policy-only evaluation")
        completed_at = utc_now()
        conn.execute(
            """
            UPDATE sec_parser_review_evaluation
            SET completed_at = ?, status = 'COMPLETED',
                base_scope_hash_after = ?,
                evaluated_evidence_count = ?,
                changed_evidence_count = ?,
                materialized_evidence_count = ?,
                applied_policy_count = ?,
                metadata_json = ?
            WHERE evaluation_id = ?
            """,
            (
                completed_at,
                after_hash,
                evaluated_count,
                changed_count,
                materialized_count,
                len(applied_policy_ids),
                json.dumps(
                    {
                        "base_hash_unchanged": True,
                        "zero_provider_contract": True,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                evaluation_id,
            ),
        )
        conn.commit()
    except Exception as exc:
        conn.execute(
            """
            UPDATE sec_parser_review_evaluation
            SET completed_at = ?, status = 'FAILED', metadata_json = ?
            WHERE evaluation_id = ?
            """,
            (
                utc_now(),
                json.dumps(
                    {
                        "error": f"{type(exc).__name__}: {exc}",
                        "zero_provider_contract": True,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                evaluation_id,
            ),
        )
        conn.commit()
        raise

    completed = conn.execute(
        """
        SELECT *
        FROM sec_parser_review_evaluation
        WHERE evaluation_id = ?
        """,
        (evaluation_id,),
    ).fetchone()
    if completed is None:
        raise RuntimeError("Completed review evaluation disappeared")
    return _summary(dict(completed), idempotent_reuse=False)


def load_review_evidence(
    conn: sqlite3.Connection,
    *,
    evaluation_id: int,
    statuses: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    ensure_review_replay_schema(conn)
    evaluation = conn.execute(
        """
        SELECT status
        FROM sec_parser_review_evaluation
        WHERE evaluation_id = ?
        """,
        (evaluation_id,),
    ).fetchone()
    if evaluation is None:
        raise ValueError(f"review evaluation_id={evaluation_id} does not exist")
    if str(evaluation["status"]) != "COMPLETED":
        raise ValueError(f"review evaluation_id={evaluation_id} is not COMPLETED")
    selected = tuple(sorted({str(status) for status in statuses or ()}))
    sql = """
        SELECT *
        FROM sec_parser_review_evidence
        WHERE evaluation_id = ?
    """
    params: list[Any] = [evaluation_id]
    if selected:
        placeholders = ",".join("?" for _ in selected)
        sql += f" AND candidate_status IN ({placeholders})"
        params.extend(selected)
    sql += " ORDER BY ticker, metric_name, period_end, accession_number, evaluated_evidence_key"
    return [dict(row) for row in conn.execute(sql, params)]


def materialize_review_evaluation_run(
    conn: sqlite3.Connection,
    *,
    evaluation_id: int,
) -> int:
    """Publish immutable review output as a normal zero-provider parser run."""
    ensure_review_replay_schema(conn)
    evaluation_row = conn.execute(
        '''
        SELECT evaluation.*, base.asof_date, base.parser_release
        FROM sec_parser_review_evaluation AS evaluation
        JOIN sec_parser_run AS base ON base.run_id=evaluation.base_run_id
        WHERE evaluation.evaluation_id=?
        ''',
        (evaluation_id,),
    ).fetchone()
    if evaluation_row is None:
        raise ValueError(f'review evaluation_id={evaluation_id} does not exist')
    evaluation = dict(evaluation_row)
    if str(evaluation['status']) != 'COMPLETED':
        raise ValueError(f'review evaluation_id={evaluation_id} is not COMPLETED')
    current_hash = base_run_scope_hash(
        conn, base_run_id=int(evaluation['base_run_id'])
    )
    if current_hash != str(evaluation['base_scope_hash_after'] or ''):
        raise RuntimeError('Base run scope changed after review evaluation')
    existing_run_id = int(evaluation.get('materialized_run_id') or 0)
    if existing_run_id:
        existing = conn.execute(
            '''SELECT status,metadata_json FROM sec_parser_run WHERE run_id=?''',
            (existing_run_id,),
        ).fetchone()
        if existing is None or str(existing['status']) != 'COMPLETED':
            raise RuntimeError('Materialized review run is missing or incomplete')
        metadata = _json_object(
            existing['metadata_json'], label='materialized run metadata_json'
        )
        if int(metadata.get('review_evaluation_id') or 0) != evaluation_id:
            raise RuntimeError('Materialized review run identity does not match')
        linked_count = int(conn.execute(
            '''SELECT COUNT(*) FROM sec_parser_run_metric_evidence WHERE run_id=?''',
            (existing_run_id,),
        ).fetchone()[0])
        if linked_count != int(evaluation['evaluated_evidence_count']):
            raise RuntimeError('Materialized review run evidence is incomplete')
        return existing_run_id

    savepoint = 'materialize_review_evaluation'
    conn.execute(f'SAVEPOINT {savepoint}')
    try:
        now = utc_now()
        metadata_json = json.dumps(
            {
                'base_run_id': int(evaluation['base_run_id']),
                'base_scope_hash': current_hash,
                'policy_sha256': str(evaluation['policy_sha256']),
                'review_evaluation_id': evaluation_id,
                'zero_provider_contract': True,
            },
            sort_keys=True,
            separators=(',', ':'),
        )
        work_count = int(conn.execute(
            'SELECT COUNT(*) FROM sec_parser_run_work WHERE run_id=?',
            (int(evaluation['base_run_id']),),
        ).fetchone()[0])
        cursor = conn.execute(
            '''
            INSERT INTO sec_parser_run(
                model_family,asof_date,parser_release,adapter_version,
                mode,worker_count,started_at,completed_at,status,
                planned_work_count,completed_work_count,failed_work_count,
                metadata_json
            ) VALUES (?,?,?,?,?,0,?,?,'COMPLETED',?,?,0,?)
            ''',
            (
                str(evaluation['model_family']),
                str(evaluation['asof_date']),
                str(evaluation['parser_release']),
                str(evaluation['adapter_version']),
                'review_replay',
                now,
                now,
                work_count,
                work_count,
                metadata_json,
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError('Failed to allocate materialized review run')
        run_id = int(cursor.lastrowid)
        conn.execute(
            '''
            INSERT INTO sec_parser_run_work(
                run_id,work_key,ticker,accession_number
            )
            SELECT ?,work_key,ticker,accession_number
            FROM sec_parser_run_work WHERE run_id=?
            ''',
            (run_id, int(evaluation['base_run_id'])),
        )
        conn.execute(
            '''
            INSERT INTO sec_parser_run_normalized_fact(run_id,fact_fingerprint)
            SELECT ?,fact_fingerprint FROM sec_parser_run_normalized_fact
            WHERE run_id=?
            ''',
            (run_id, int(evaluation['base_run_id'])),
        )
        conn.execute(
            '''
            INSERT INTO sec_parser_metric_evidence_shadow(
                evidence_key,run_id,work_key,model_family,adapter_version,
                ticker,cik,accession_number,form_type,filing_date,accepted_at,
                report_date,metric_name,concept_name,candidate_value,unit,
                period_start,period_end,scope,confidence,candidate_status,
                status_reason,evidence_text,source_document,extraction_method,
                provenance_json,parser_release,created_at
            )
            SELECT evaluated_evidence_key,?,work_key,?, ?,ticker,cik,
                   accession_number,form_type,filing_date,accepted_at,
                   report_date,metric_name,concept_name,candidate_value,unit,
                   period_start,period_end,scope,confidence,candidate_status,
                   status_reason,evidence_text,source_document,
                   extraction_method,provenance_json,?,created_at
            FROM sec_parser_review_evidence WHERE evaluation_id=?
            ''',
            (
                run_id,
                str(evaluation['model_family']),
                str(evaluation['adapter_version']),
                str(evaluation['parser_release']),
                evaluation_id,
            ),
        )
        conn.execute(
            '''
            INSERT INTO sec_parser_run_metric_evidence(run_id,evidence_key)
            SELECT ?,evaluated_evidence_key FROM sec_parser_review_evidence
            WHERE evaluation_id=?
            ''',
            (run_id, evaluation_id),
        )
        linked_count = int(conn.execute(
            'SELECT COUNT(*) FROM sec_parser_run_metric_evidence WHERE run_id=?',
            (run_id,),
        ).fetchone()[0])
        if linked_count != int(evaluation['evaluated_evidence_count']):
            raise RuntimeError('Materialized review evidence count mismatch')
        conn.execute(
            '''UPDATE sec_parser_review_evaluation
               SET materialized_run_id=? WHERE evaluation_id=?''',
            (run_id, evaluation_id),
        )
        conn.execute(f'RELEASE SAVEPOINT {savepoint}')
    except Exception:
        conn.execute(f'ROLLBACK TO SAVEPOINT {savepoint}')
        conn.execute(f'RELEASE SAVEPOINT {savepoint}')
        raise
    return run_id

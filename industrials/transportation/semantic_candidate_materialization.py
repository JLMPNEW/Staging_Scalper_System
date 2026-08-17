from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


MODEL_FAMILY = "transportation"
CONTRACT_VERSION = "transportation_reviewed_semantic_candidates_v1"
SOURCE_ID = "transportation_reviewed_semantic_candidate_v1"
EXTRACTION_METHOD = "transportation_reviewed_semantic_replay_v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ReviewedSemanticCandidate:
    candidate_key: str
    lane: str
    ticker: str
    accession_number: str
    form_type: str
    filing_date: str
    accepted_at: str
    document_name: str
    metric_name: str
    concept_name: str
    value: float
    unit: str
    period_start: str
    period_end: str
    evidence_key: str
    source_content_sha256: str
    provenance_json: str


def _finite(value: object) -> float:
    parsed = float(str(value or "").strip())
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite semantic replay value={value!r}")
    return parsed


def _destination_key(*, lane: str, row: Mapping[str, object]) -> str:
    evidence_identity = str(
        row.get("evidence_key") or row.get("candidate_key") or ""
    ).strip()
    payload = "|".join(
        (
            CONTRACT_VERSION,
            lane,
            evidence_identity,
            str(row.get("ticker") or "").strip().upper(),
            str(row.get("metric_id") or "").strip(),
            str(row.get("period_end") or "").strip()[:10],
            str(row.get("filing_date") or "").strip()[:10],
            str(row.get("accession_number") or "").strip(),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_materialization_candidates(
    rows: Iterable[Mapping[str, object]],
    *,
    lane: str,
    allowed_pairs: set[tuple[str, str]],
    asof: str,
    lineage: Mapping[str, object],
) -> list[ReviewedSemanticCandidate]:
    """Build canonical candidates only from reviewed, conflict-free rows.

    The caller is responsible for deriving ``allowed_pairs`` from accepted
    breadth/history gates. This function deliberately cannot promote a metric
    merely because a reviewed row happens to exist.
    """
    if lane not in {"surface", "tanker"}:
        raise ValueError(f"unsupported semantic replay lane={lane!r}")
    if not allowed_pairs:
        raise ValueError(f"{lane}: empty qualifying ticker/metric allowlist")
    asof_date = str(asof)[:10]
    output: list[ReviewedSemanticCandidate] = []
    seen_keys: set[str] = set()
    seen_observations: set[tuple[str, str, str, str, str]] = set()
    for raw in rows:
        ticker = str(raw.get("ticker") or "").strip().upper()
        metric = str(raw.get("metric_id") or "").strip()
        if (ticker, metric) not in allowed_pairs:
            continue
        if str(raw.get("replay_status") or "").strip().upper() != "ACCEPTED":
            raise ValueError(f"{lane}/{ticker}/{metric}: replay row is not ACCEPTED")
        filing_date = str(raw.get("filing_date") or "").strip()[:10]
        period_end = str(raw.get("period_end") or "").strip()[:10]
        accession = str(raw.get("accession_number") or "").strip()
        source_lane = str(raw.get("source_lane") or "").strip()
        evidence_key = str(
            raw.get("evidence_key") or raw.get("candidate_key") or ""
        ).strip()
        document = str(raw.get("source_document") or "").strip()
        content_sha256 = str(raw.get("source_content_sha256") or "").strip().lower()
        if not all((ticker, metric, filing_date, period_end, accession, evidence_key, document)):
            raise ValueError(f"{lane}/{ticker}/{metric}: incomplete replay identity")
        if filing_date > asof_date:
            continue
        if source_lane == "fact_store_ratio":
            if content_sha256:
                raise ValueError(
                    f"{lane}/{ticker}/{metric}: fact-store row has unexpected document hash"
                )
            if not all(
                str(raw.get(field) or "").strip()
                for field in ("formula", "numerator_concept", "denominator_concept")
            ):
                raise ValueError(
                    f"{lane}/{ticker}/{metric}: incomplete fact-store formula lineage"
                )
        elif not _SHA256.fullmatch(content_sha256):
            raise ValueError(f"{lane}/{ticker}/{metric}: invalid source content hash")
        observation = (ticker, metric, period_end, filing_date, accession)
        if observation in seen_observations:
            raise ValueError(f"{lane}: replay input is not conflict-free at {observation}")
        seen_observations.add(observation)
        key = _destination_key(lane=lane, row=raw)
        if key in seen_keys:
            raise ValueError(f"{lane}: duplicate destination candidate key={key}")
        seen_keys.add(key)
        provenance = {
            "contract_version": CONTRACT_VERSION,
            "materialization_lane": lane,
            "source_lane": source_lane,
            "source_candidate_key": str(raw.get("candidate_key") or ""),
            "definition_id": str(raw.get("definition_id") or ""),
            "evidence_key": evidence_key,
            "review_policy_version": str(raw.get("review_policy_version") or ""),
            "reviewed_by": str(raw.get("reviewed_by") or ""),
            "reviewed_at": str(raw.get("reviewed_at") or ""),
            "replay_reason": str(raw.get("replay_reason") or ""),
            "source_document": document,
            "source_content_sha256": content_sha256,
            "lineage": dict(lineage),
        }
        output.append(
            ReviewedSemanticCandidate(
                candidate_key=key,
                lane=lane,
                ticker=ticker,
                accession_number=accession,
                form_type=str(raw.get("form_type") or "").strip(),
                filing_date=filing_date,
                accepted_at=str(raw.get("accepted_at") or "").strip(),
                document_name=document,
                metric_name=metric,
                concept_name=str(raw.get("concept_name") or metric).strip() or metric,
                value=_finite(raw.get("value")),
                unit=str(raw.get("unit") or "").strip(),
                period_start=str(raw.get("period_start") or "").strip()[:10],
                period_end=period_end,
                evidence_key=evidence_key,
                source_content_sha256=content_sha256,
                provenance_json=json.dumps(
                    provenance, sort_keys=True, separators=(",", ":")
                ),
            )
        )
    return sorted(
        output,
        key=lambda item: (
            item.lane,
            item.ticker,
            item.metric_name,
            item.filing_date,
            item.period_end,
            item.candidate_key,
        ),
    )


def _ensure_source_registry(connection: sqlite3.Connection, *, now: str) -> None:
    connection.execute(
        """
        INSERT INTO source_registry(
            source_id, stage, source_name, source_owner, source_type,
            base_url, authentication_required, free_key_required,
            refresh_frequency, data_owner, raw_schema, staging_tables,
            canonical_tables, feature_stages, subsector_scope, priority,
            status, notes, created_at, updated_at
        )
        VALUES (
            ?, 'specialized_disclosures',
            'Transportation Reviewed Semantic Candidates',
            'internal', 'reviewed_sec_filing_evidence',
            'semantic-replay://transportation/conflict-free/v1', 0, 0,
            'review_batch', 'SEC issuers',
            '["dedicated_parser_semantic_replay","cached_sec_filings"]',
            '["fact_sec_metric_disclosure_candidate"]',
            '["fact_sec_metric_disclosure_candidate"]',
            '["specialized_metric_availability","transportation_scoring"]',
            'transportation', 4, 'active',
            'Conflict-free, definition-reviewed candidates admitted only after metric-domain breadth and history gates pass.',
            ?, ?
        )
        ON CONFLICT(source_id) DO UPDATE SET
            source_name=excluded.source_name,
            source_type=excluded.source_type,
            base_url=excluded.base_url,
            refresh_frequency=excluded.refresh_frequency,
            raw_schema=excluded.raw_schema,
            staging_tables=excluded.staging_tables,
            canonical_tables=excluded.canonical_tables,
            feature_stages=excluded.feature_stages,
            subsector_scope=excluded.subsector_scope,
            priority=excluded.priority,
            status=excluded.status,
            notes=excluded.notes,
            updated_at=excluded.updated_at
        """,
        (SOURCE_ID, now, now),
    )


def persist_materialization_candidates(
    connection: sqlite3.Connection,
    candidates: Iterable[ReviewedSemanticCandidate],
    *,
    now: str,
) -> dict[str, int]:
    rows = list(candidates)
    _ensure_source_registry(connection, now=now)
    for item in rows:
        cik_row = connection.execute(
            """
            SELECT cik FROM fact_sec_filing
            WHERE ticker=? AND accession_number=? AND cik IS NOT NULL
            ORDER BY source_id LIMIT 1
            """,
            (item.ticker, item.accession_number),
        ).fetchone()
        cik = str(cik_row[0] or "") if cik_row is not None else ""
        evidence_text = (
            "reviewed conflict-free semantic replay; "
            f"evidence_key={item.evidence_key}; "
            f"document={item.document_name}; sha256={item.source_content_sha256}"
        )
        connection.execute(
            """
            INSERT INTO fact_sec_metric_disclosure_candidate(
                candidate_key, ticker, cik, source_id, model_family,
                accession_number, form_type, filing_date, accepted_at,
                document_name, metric_name, concept_name, candidate_value,
                unit, period_start, period_end, scope, extraction_method,
                confidence, candidate_status, status_reason, evidence_text,
                provenance_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    1.0, 'ACCEPTED', 'conflict_free_reviewed_semantic_replay',
                    ?, ?, ?, ?)
            ON CONFLICT(candidate_key) DO UPDATE SET
                candidate_value=excluded.candidate_value,
                unit=excluded.unit,
                period_start=excluded.period_start,
                period_end=excluded.period_end,
                confidence=excluded.confidence,
                candidate_status=excluded.candidate_status,
                status_reason=excluded.status_reason,
                evidence_text=excluded.evidence_text,
                provenance_json=excluded.provenance_json,
                updated_at=excluded.updated_at
            """,
            (
                item.candidate_key,
                item.ticker,
                cik,
                SOURCE_ID,
                MODEL_FAMILY,
                item.accession_number,
                item.form_type,
                item.filing_date,
                item.accepted_at,
                item.document_name,
                item.metric_name,
                item.concept_name,
                item.value,
                item.unit,
                item.period_start,
                item.period_end,
                "reviewed_conflict_free_semantic_replay",
                EXTRACTION_METHOD,
                evidence_text,
                item.provenance_json,
                now,
                now,
            ),
        )
    materialized = connection.execute(
        """
        SELECT COUNT(*)
        FROM fact_sec_metric_disclosure_candidate
        WHERE model_family=? AND source_id=? AND extraction_method=?
        """,
        (MODEL_FAMILY, SOURCE_ID, EXTRACTION_METHOD),
    ).fetchone()[0]
    return {"requested_count": len(rows), "materialized_source_row_count": int(materialized)}

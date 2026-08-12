from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from datetime import date
from typing import Any

from dedicated_parser.contracts import AdapterRegistry, stable_hash
from dedicated_parser.storage import utc_now


MONETARY_UNITS = frozenset(
    {
        "AUD",
        "CAD",
        "CHF",
        "EUR",
        "GBP",
        "JPY",
        "USD",
    }
)
AUTOMATIC_SUPPRESSION_REASONS = frozenset(
    {
        "revenue_contract_narrative_is_not_orders",
        "rpo_amount_requires_monetary_unit",
        "timing_dimension_current_bucket_not_twelve_months",
        "timing_dimension_current_fraction_outside_valid_range",
    }
)


def _ensure_source_registry(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    now: str,
) -> None:
    exists = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'source_registry'
        """
    ).fetchone()
    if exists is None:
        return
    conn.execute(
        """
        INSERT INTO source_registry(
            source_id, stage, source_name, source_owner, source_type,
            base_url, authentication_required, free_key_required,
            refresh_frequency, data_owner, raw_schema, staging_tables,
            canonical_tables, feature_stages, subsector_scope, priority,
            status, notes, created_at, updated_at
        )
        VALUES (
            ?, 'financial_fundamentals', 'Shared Dedicated SEC Parser',
            'internal', 'derived_sec_filing_evidence',
            'sec-cache://dedicated-parser', 0, 0, 'daily', 'SEC issuers',
            '["sec_parser_metric_evidence_shadow"]',
            '["fact_sec_xbrl_fact_raw","fact_sec_xbrl_fact"]',
            '["fact_financial_statement_canonical"]',
            '["financial_features"]', 'industrials', 175, 'active',
            'Reviewed high-confidence parser evidence supplemental source.',
            ?, ?
        )
        ON CONFLICT(source_id) DO UPDATE SET
            stage = excluded.stage,
            source_name = excluded.source_name,
            source_type = excluded.source_type,
            base_url = excluded.base_url,
            refresh_frequency = excluded.refresh_frequency,
            staging_tables = excluded.staging_tables,
            canonical_tables = excluded.canonical_tables,
            feature_stages = excluded.feature_stages,
            subsector_scope = excluded.subsector_scope,
            priority = excluded.priority,
            status = excluded.status,
            notes = excluded.notes,
            updated_at = excluded.updated_at
        """,
        (source_id, now, now),
    )


def _accepted_date(row: sqlite3.Row) -> str:
    return str(row["accepted_at"] or row["filing_date"] or "")[:10]


def _valid_date(value: object) -> bool:
    try:
        date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return False
    return True


def _evidence_rows(
    conn: sqlite3.Connection,
    *,
    run_id: int,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT evidence.*
        FROM sec_parser_run_metric_evidence AS relation
        JOIN sec_parser_metric_evidence_shadow AS evidence
          ON evidence.evidence_key = relation.evidence_key
        WHERE relation.run_id = ?
        ORDER BY evidence.ticker, evidence.metric_name,
                 evidence.period_end, evidence.period_start,
                 evidence.accession_number, evidence.evidence_key
        """,
        (run_id,),
    ).fetchall()


def _conflicting_evidence_keys(
    rows: list[sqlite3.Row],
    *,
    registry: AdapterRegistry,
    asof_date: str,
    min_confidence: float,
) -> set[str]:
    grouped: dict[
        tuple[str, str, str, str, str, str],
        list[sqlite3.Row],
    ] = defaultdict(list)
    for row in rows:
        metric_name = str(row["metric_name"])
        if (
            str(row["candidate_status"]) != "ACCEPTED"
            or metric_name not in registry.production_mappings
            or row["candidate_value"] is None
            or float(row["confidence"] or 0.0) < min_confidence
            or str(row["scope"]) != "consolidated"
            or _accepted_date(row) > asof_date
            or str(row["period_end"] or "")[:10] > asof_date
        ):
            continue
        grouped[
            (
                str(row["ticker"]),
                str(row["accession_number"]),
                metric_name,
                str(row["period_start"] or ""),
                str(row["period_end"] or ""),
                str(row["unit"] or "").upper(),
            )
        ].append(row)
    conflicts: set[str] = set()
    for candidates in grouped.values():
        values = {round(float(row["candidate_value"]), 6) for row in candidates}
        if len(values) > 1:
            conflicts.update(str(row["evidence_key"]) for row in candidates)
    return conflicts


def _promotion_block_reason(
    row: sqlite3.Row,
    *,
    registry: AdapterRegistry,
    asof_date: str,
    min_confidence: float,
    conflicting_keys: set[str],
) -> str:
    metric_name = str(row["metric_name"])
    mapping = registry.production_mappings.get(metric_name)
    if str(row["candidate_status"]) != "ACCEPTED":
        return "candidate_not_accepted"
    if mapping is None:
        return "metric_not_registered_for_production"
    if row["candidate_value"] is None:
        return "candidate_value_missing"
    value = float(row["candidate_value"])
    if not math.isfinite(value):
        return "candidate_value_not_finite"
    if mapping.sign_policy == "positive_abs" and value < 0.0:
        return "negative_value_for_positive_metric"
    if float(row["confidence"] or 0.0) < min_confidence:
        return "confidence_below_production_threshold"
    if str(row["scope"]) != "consolidated":
        return "scope_not_consolidated"
    if str(row["unit"] or "").upper() not in MONETARY_UNITS:
        return "unit_not_supported_monetary_currency"
    if not _valid_date(row["period_end"]):
        return "period_end_invalid"
    if str(row["period_end"])[:10] > asof_date:
        return "period_end_after_asof"
    accepted_date = _accepted_date(row)
    if not _valid_date(accepted_date) or accepted_date > asof_date:
        return "accepted_date_invalid_or_after_asof"
    if mapping.period_type == "duration":
        if not _valid_date(row["period_start"]):
            return "duration_period_start_invalid"
        if str(row["period_start"])[:10] > str(row["period_end"])[:10]:
            return "duration_period_inverted"
    elif str(row["period_start"] or ""):
        return "instant_metric_has_period_start"
    if str(row["evidence_key"]) in conflicting_keys:
        return "conflicting_accepted_values_same_observation"
    return ""


def _filing_metadata(
    conn: sqlite3.Connection,
    *,
    model_family: str,
    ticker: str,
    accession_number: str,
    asof_date: str | None = None,
) -> dict[str, Any]:
    if model_family == "consumer_defensive":
        view = conn.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='view' AND name='consumer_defensive_sec_parser_filing_input'"""
        ).fetchone()
        if view is None:
            raise RuntimeError(
                "Consumer Defensive parser promotion requires "
                "consumer_defensive_sec_parser_filing_input"
            )
        if asof_date is None:
            raise RuntimeError(
                'Consumer Defensive parser metadata requires an explicit as-of date'
            )
        row = conn.execute(
            """
            SELECT fiscal_year,fiscal_period,form_type,filing_date,accepted_at
            FROM consumer_defensive_sec_parser_filing_input
            WHERE ticker=? AND accession_number=?
              AND SUBSTR(COALESCE(NULLIF(accepted_at,''),filing_date),1,10)<=?
              AND (SELECT e.event_type
                  FROM sec_filing_company_association_event e
                  WHERE e.accession_number=consumer_defensive_sec_parser_filing_input.accession_number
                    AND e.issuer_company_id=consumer_defensive_sec_parser_filing_input.issuer_company_id
                    AND e.effective_asof<=? || 'T23:59:59Z'
                  ORDER BY e.effective_asof DESC,e.event_id DESC LIMIT 1)
                  IN ('observed','reactivated')
            ORDER BY COALESCE(NULLIF(accepted_at,''),filing_date) DESC
            LIMIT 1
            """,
            (ticker, accession_number,asof_date,asof_date),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"Consumer Defensive parser promotion has no issuer association "
                f"for {ticker} {accession_number}"
            )
        return dict(row)
    row = conn.execute(
        """
        SELECT fiscal_year, fiscal_period, form_type, filing_date, accepted_at
        FROM fact_sec_filing
        WHERE ticker = ? AND accession_number = ?
        ORDER BY COALESCE(NULLIF(accepted_at, ''), filing_date) DESC
        LIMIT 1
        """,
        (ticker, accession_number),
    ).fetchone()
    return dict(row) if row is not None else {}


def _sync_accession_slice(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    rows: list[sqlite3.Row],
    registry: AdapterRegistry,
) -> None:
    slices = {
        (
            str(row["ticker"]),
            str(row["accession_number"]),
            registry.production_mappings[str(row["metric_name"])].canonical_metric,
        )
        for row in rows
        if str(row["metric_name"]) in registry.production_mappings
    }
    for ticker, accession_number, canonical_metric in sorted(slices):
        conn.execute(
            """
            DELETE FROM fact_sec_xbrl_fact
            WHERE source_id = ? AND ticker = ? AND accession_number = ?
              AND canonical_metric = ?
            """,
            (source_id, ticker, accession_number, canonical_metric),
        )
        conn.execute(
            """
            DELETE FROM fact_sec_xbrl_fact_raw
            WHERE source_id = ? AND ticker = ? AND accession_number = ?
              AND source_detail = ?
            """,
            (
                source_id,
                ticker,
                accession_number,
                f"{canonical_metric}:dedicated_parser_production",
            ),
        )


def _insert_fact(
    conn: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    registry: AdapterRegistry,
    source_id: str,
    now: str,
    asof_date: str,
) -> int:
    metric_name = str(row["metric_name"])
    mapping = registry.production_mappings[metric_name]
    metadata = _filing_metadata(
        conn,
        model_family=registry.model_family,
        ticker=str(row["ticker"]),
        accession_number=str(row["accession_number"]),
        asof_date=asof_date,
    )
    value = float(row["candidate_value"])
    if mapping.sign_policy == "positive_abs":
        value = abs(value)
    period_start = str(row["period_start"] or "") if mapping.period_type == "duration" else ""
    period_end = str(row["period_end"] or "")[:10]
    unit = str(row["unit"] or "").upper()
    evidence_key = str(row["evidence_key"])
    fact_key = stable_hash(
        {
            "source_id": source_id,
            "evidence_key": evidence_key,
            "canonical_metric": mapping.canonical_metric,
        }
    )
    frame = f"dedicated-parser:{evidence_key[:24]}"
    source_detail = f"{mapping.canonical_metric}:dedicated_parser_production"
    payload = {
        "evidence_key": evidence_key,
        "run_evidence": True,
        "confidence": float(row["confidence"]),
        "status_reason": str(row["status_reason"] or ""),
        "source_document": str(row["source_document"]),
        "extraction_method": str(row["extraction_method"]),
        "provenance": json.loads(str(row["provenance_json"] or "{}")),
    }
    conn.execute(
        """
        INSERT INTO fact_sec_xbrl_fact_raw(
            fact_key, ticker, cik, source_id, accession_number, form_type,
            filing_date, accepted_at, fiscal_year, fiscal_period, period_start,
            period_end, frame, taxonomy, concept_name, unit, raw_value, decimals,
            source_detail, payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'dedicated-parser',
                ?, ?, ?, '', ?, ?, ?, ?)
        ON CONFLICT(fact_key) DO UPDATE SET
            filing_date = excluded.filing_date,
            accepted_at = excluded.accepted_at,
            fiscal_year = excluded.fiscal_year,
            fiscal_period = excluded.fiscal_period,
            raw_value = excluded.raw_value,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        (
            fact_key,
            str(row["ticker"]),
            str(row["cik"]),
            source_id,
            str(row["accession_number"]),
            str(metadata.get("form_type") or row["form_type"] or "").upper(),
            str(metadata.get("filing_date") or row["filing_date"] or "")[:10],
            str(metadata.get("accepted_at") or row["accepted_at"] or ""),
            metadata.get("fiscal_year"),
            str(metadata.get("fiscal_period") or ""),
            period_start,
            period_end,
            frame,
            str(row["concept_name"]),
            unit.lower(),
            value,
            source_detail,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            now,
            now,
        ),
    )
    raw_row = conn.execute(
        """
        SELECT raw_fact_id
        FROM fact_sec_xbrl_fact_raw
        WHERE fact_key = ?
        """,
        (fact_key,),
    ).fetchone()
    if raw_row is None:
        raise RuntimeError("Promoted raw parser fact was not persisted")
    raw_fact_id = int(raw_row["raw_fact_id"])
    conn.execute(
        """
        INSERT INTO fact_sec_xbrl_fact(
            raw_fact_id, ticker, cik, source_id, accession_number,
            form_type, filing_date, accepted_at, fiscal_year, fiscal_period,
            period_start, period_end, frame, taxonomy, concept_name,
            canonical_metric, financial_statement, period_type, unit,
            value, sign_policy, source_priority, source_detail,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'dedicated-parser',
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, source_id, accession_number, taxonomy, concept_name,
                    canonical_metric, unit, period_start, period_end, frame)
        DO UPDATE SET
            raw_fact_id = excluded.raw_fact_id,
            filing_date = excluded.filing_date,
            accepted_at = excluded.accepted_at,
            fiscal_year = excluded.fiscal_year,
            fiscal_period = excluded.fiscal_period,
            value = excluded.value,
            source_priority = excluded.source_priority,
            source_detail = excluded.source_detail,
            updated_at = excluded.updated_at
        """,
        (
            raw_fact_id,
            str(row["ticker"]),
            str(row["cik"]),
            source_id,
            str(row["accession_number"]),
            str(metadata.get("form_type") or row["form_type"] or "").upper(),
            str(metadata.get("filing_date") or row["filing_date"] or "")[:10],
            str(metadata.get("accepted_at") or row["accepted_at"] or ""),
            metadata.get("fiscal_year"),
            str(metadata.get("fiscal_period") or ""),
            period_start,
            period_end,
            frame,
            str(row["concept_name"]),
            mapping.canonical_metric,
            mapping.financial_statement,
            mapping.period_type,
            unit.lower(),
            value,
            mapping.sign_policy,
            mapping.source_priority,
            f"{source_detail}_mapped",
            now,
            now,
        ),
    )
    return raw_fact_id


def _persist_reviewed_suppressions(
    conn: sqlite3.Connection,
    *,
    rows: list[sqlite3.Row],
    registry: AdapterRegistry,
    now: str,
) -> int:
    reviewed_policy_ids: set[str] = set()
    for row in rows:
        try:
            provenance = json.loads(str(row["provenance_json"] or "{}"))
        except json.JSONDecodeError:
            continue
        review = provenance.get("review_policy")
        if isinstance(review, dict) and review.get("policy_id"):
            reviewed_policy_ids.add(str(review["policy_id"]))
    for policy_id in sorted(reviewed_policy_ids):
        conn.execute(
            """
            UPDATE sec_parser_production_suppression
            SET active = 0
            WHERE model_family = ? AND policy_id = ?
            """,
            (registry.model_family, policy_id),
        )
    for row in rows:
        conn.execute(
            """
            UPDATE sec_parser_production_suppression
            SET active = 0
            WHERE model_family = ? AND evidence_key = ?
              AND policy_id LIKE 'automatic:%'
            """,
            (registry.model_family, str(row["evidence_key"])),
        )

    inserted = 0
    for row in rows:
        metric_name = str(row["metric_name"])
        mapping = registry.production_mappings.get(metric_name)
        if (
            mapping is None
            or str(row["candidate_status"]) != "REJECTED_POLICY"
            or row["candidate_value"] is None
            or not _valid_date(row["period_end"])
        ):
            continue
        try:
            provenance = json.loads(str(row["provenance_json"] or "{}"))
        except json.JSONDecodeError:
            continue
        review = provenance.get("review_policy")
        if isinstance(review, dict) and review.get("policy_id"):
            policy_id = str(review["policy_id"])
        elif str(row["status_reason"]) in AUTOMATIC_SUPPRESSION_REASONS:
            policy_id = f"automatic:{row['status_reason']}"
        else:
            continue
        valid_from = _accepted_date(row)
        before = conn.total_changes
        conn.execute(
            """
            INSERT INTO sec_parser_production_suppression(
                model_family, ticker, canonical_metric, period_start,
                period_end, candidate_value, value_tolerance, unit,
                accession_number, evidence_key, policy_id, valid_from,
                active, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(model_family, evidence_key, policy_id) DO UPDATE SET
                candidate_value = excluded.candidate_value,
                unit = excluded.unit,
                valid_from = excluded.valid_from,
                active = 1
            """,
            (
                registry.model_family,
                str(row["ticker"]),
                mapping.canonical_metric,
                str(row["period_start"] or ""),
                str(row["period_end"])[:10],
                float(row["candidate_value"]),
                max(1e-6, abs(float(row["candidate_value"])) * 1e-12),
                str(row["unit"] or "").upper(),
                str(row["accession_number"] or ""),
                str(row["evidence_key"]),
                policy_id,
                valid_from,
                now,
            ),
        )
        inserted += int(conn.total_changes > before)
    return inserted


def _persist_structural_overrides(
    conn: sqlite3.Connection,
    *,
    rows: list[sqlite3.Row],
    registry: AdapterRegistry,
    now: str,
) -> int:
    inserted = 0
    allowed_original_reasons = {
        "asc606_practical_expedient_requires_review",
        "short_cycle_or_no_binding_backlog_requires_review",
    }
    for row in rows:
        if str(row["candidate_status"]) != "STRUCTURAL_NA":
            continue
        try:
            provenance = json.loads(str(row["provenance_json"] or "{}"))
        except json.JSONDecodeError:
            continue
        review = provenance.get("review_policy")
        if (
            not isinstance(review, dict)
            or not review.get("policy_id")
            or str(review.get("matched_reason") or "") not in allowed_original_reasons
        ):
            continue
        before = conn.total_changes
        conn.execute(
            """
            INSERT INTO sec_parser_production_metric_override(
                model_family, ticker, metric_name, availability_status,
                status_reason, evidence_key, valid_from, active, created_at
            )
            VALUES (?, ?, ?, 'EXEMPT', ?, ?, ?, 1, ?)
            ON CONFLICT(model_family, ticker, metric_name, evidence_key)
            DO UPDATE SET
                availability_status = excluded.availability_status,
                status_reason = excluded.status_reason,
                valid_from = excluded.valid_from,
                active = 1
            """,
            (
                registry.model_family,
                str(row["ticker"]),
                str(row["metric_name"]),
                str(row["status_reason"] or ""),
                str(row["evidence_key"]),
                _accepted_date(row),
                now,
            ),
        )
        inserted += int(conn.total_changes > before)
    return inserted


def promote_run(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    registry: AdapterRegistry,
    source_id: str,
    min_confidence: float = 0.90,
) -> dict[str, Any]:
    if registry.model_family == "consumer_defensive":
        raise RuntimeError(
            "Consumer Defensive parser promotion is disabled until its "
            "financial-fact storage adapter is implemented; catalog and "
            "shadow census parsing remain available"
        )
    if not 0.0 <= min_confidence <= 1.0:
        raise ValueError("min_confidence must be in [0, 1]")
    run = conn.execute(
        "SELECT * FROM sec_parser_run WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if run is None:
        raise ValueError(f"Parser run_id={run_id} does not exist")
    if str(run["model_family"]) != registry.model_family:
        raise ValueError("Parser run model family does not match adapter")
    if str(run["status"]) != "COMPLETED" or int(run["failed_work_count"]) != 0:
        raise ValueError("Only fully completed, zero-failure runs may be promoted")
    asof_date = str(run["asof_date"])
    rows = _evidence_rows(conn, run_id=run_id)
    now = utc_now()
    existing = conn.execute(
        """
        SELECT promotion_id
        FROM sec_parser_production_promotion_run
        WHERE run_id = ? AND source_id = ?
        """,
        (run_id, source_id),
    ).fetchone()
    try:
        with conn:
            _ensure_source_registry(
                conn,
                source_id=source_id,
                now=now,
            )
            if existing is None:
                cursor = conn.execute(
                    """
                    INSERT INTO sec_parser_production_promotion_run(
                        run_id, model_family, asof_date, source_id,
                        min_confidence, started_at, status
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 'RUNNING')
                    """,
                    (
                        run_id,
                        registry.model_family,
                        asof_date,
                        source_id,
                        min_confidence,
                        now,
                    ),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("Failed to create parser promotion run")
                promotion_id = int(cursor.lastrowid)
            else:
                promotion_id = int(existing["promotion_id"])
                conn.execute(
                    """
                    UPDATE sec_parser_production_promotion_run
                    SET min_confidence = ?, started_at = ?, completed_at = NULL,
                        status = 'RUNNING', candidate_count = 0,
                        promoted_count = 0, blocked_count = 0,
                        suppression_count = 0, metadata_json = '{}'
                    WHERE promotion_id = ?
                    """,
                    (min_confidence, now, promotion_id),
                )
                conn.execute(
                    """
                    DELETE FROM sec_parser_production_evidence
                    WHERE promotion_id = ?
                    """,
                    (promotion_id,),
                )
            _sync_accession_slice(
                conn,
                source_id=source_id,
                rows=rows,
                registry=registry,
            )
            conflicting_keys = _conflicting_evidence_keys(
                rows,
                registry=registry,
                asof_date=asof_date,
                min_confidence=min_confidence,
            )
            promoted = 0
            blocked = 0
            for row in rows:
                reason = _promotion_block_reason(
                    row,
                    registry=registry,
                    asof_date=asof_date,
                    min_confidence=min_confidence,
                    conflicting_keys=conflicting_keys,
                )
                raw_fact_id: int | None = None
                action = "BLOCKED"
                if not reason:
                    raw_fact_id = _insert_fact(
                        conn,
                        row=row,
                        registry=registry,
                        source_id=source_id,
                        now=now,
                        asof_date=asof_date,
                    )
                    action = "PROMOTED"
                    reason = "accepted_evidence_promoted"
                    promoted += 1
                else:
                    blocked += 1
                conn.execute(
                    """
                    INSERT INTO sec_parser_production_evidence(
                        promotion_id, evidence_key, action, raw_fact_id,
                        status_reason, promoted_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        promotion_id,
                        str(row["evidence_key"]),
                        action,
                        raw_fact_id,
                        reason,
                        now,
                    ),
                )
            suppression_count = _persist_reviewed_suppressions(
                conn,
                rows=rows,
                registry=registry,
                now=now,
            )
            structural_override_count = _persist_structural_overrides(
                conn,
                rows=rows,
                registry=registry,
                now=now,
            )
            metadata = {
                "conflicting_evidence_count": len(conflicting_keys),
                "production_metric_count": len(registry.production_mappings),
                "structural_override_count": structural_override_count,
            }
            conn.execute(
                """
                UPDATE sec_parser_production_promotion_run
                SET completed_at = ?, status = 'COMPLETED',
                    candidate_count = ?, promoted_count = ?,
                    blocked_count = ?, suppression_count = ?,
                    metadata_json = ?
                WHERE promotion_id = ?
                """,
                (
                    utc_now(),
                    len(rows),
                    promoted,
                    blocked,
                    suppression_count,
                    json.dumps(metadata, sort_keys=True),
                    promotion_id,
                ),
            )
    except BaseException:
        conn.rollback()
        raise
    return {
        "promotion_id": promotion_id,
        "run_id": run_id,
        "model_family": registry.model_family,
        "asof_date": asof_date,
        "source_id": source_id,
        "candidate_count": len(rows),
        "promoted_count": promoted,
        "blocked_count": blocked,
        "suppression_count": suppression_count,
        "structural_override_count": structural_override_count,
        "conflicting_evidence_count": len(conflicting_keys),
        "status": "COMPLETED",
    }

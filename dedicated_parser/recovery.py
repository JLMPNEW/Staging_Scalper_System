from __future__ import annotations

import csv
import json
import os
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from dedicated_parser.contracts import AdapterRegistry
from dedicated_parser.storage import utc_now


STRUCTURAL_STATUSES = frozenset({"EXEMPT", "NOT_APPLICABLE"})
COVERED_STATUSES = frozenset({"PROXY", "REPORTED"})
REVIEW_STATUSES = frozenset({"REVIEW_REQUIRED"})


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        is not None
    )


def _baseline_rows(
    conn: sqlite3.Connection,
    *,
    model_family: str,
    asof_date: str,
    tickers: list[str],
) -> dict[tuple[str, str], dict[str, Any]]:
    if not tickers or not _table_exists(
        conn,
        "feature_financial_metric_availability",
    ):
        return {}
    placeholders = ",".join("?" for _ in tickers)
    rows = conn.execute(
        f"""
        SELECT a.ticker, a.metric_name, a.availability_status,
               a.metric_value, a.period_end, a.status_reason
        FROM feature_financial_metric_availability AS a
        WHERE a.model_family = ?
          AND a.ticker IN ({placeholders})
          AND a.asof_date = (
              SELECT MAX(a2.asof_date)
              FROM feature_financial_metric_availability AS a2
              WHERE a2.model_family = a.model_family
                AND a2.ticker = a.ticker
                AND a2.asof_date <= ?
          )
        """,
        (model_family, *tickers, asof_date),
    ).fetchall()
    return {
        (str(row["ticker"]), str(row["metric_name"])): dict(row)
        for row in rows
    }


def _anchor_periods(
    conn: sqlite3.Connection,
    *,
    model_family: str,
    asof_date: str,
    tickers: list[str],
) -> dict[str, str]:
    if not tickers or not _table_exists(conn, "feature_financial_statement"):
        return {}
    placeholders = ",".join("?" for _ in tickers)
    return {
        str(row["ticker"]): str(row["anchor_period_end"] or "")
        for row in conn.execute(
            f"""
            SELECT ticker, MAX(fiscal_period_end) AS anchor_period_end
            FROM feature_financial_statement
            WHERE model_family = ? AND asof_date <= ?
              AND ticker IN ({placeholders})
            GROUP BY ticker
            """,
            (model_family, asof_date, *tickers),
        )
    }


def _work_stats(
    conn: sqlite3.Connection,
    *,
    run_id: int,
) -> dict[str, dict[str, int]]:
    rows = conn.execute(
        """
        SELECT rw.ticker,
               COUNT(DISTINCT rw.accession_number) AS filing_count,
               COUNT(DISTINCT CASE WHEN ledger.status = 'FAILED'
                                   THEN rw.accession_number END) AS failed_count,
               COUNT(DISTINCT catalog.accession_number || ':' ||
                                      catalog.document_name || ':' ||
                                      catalog.content_sha256) AS document_count
        FROM sec_parser_run_work AS rw
        JOIN sec_parser_work_ledger AS ledger
          ON ledger.work_key = rw.work_key
        LEFT JOIN sec_parser_document_catalog AS catalog
          ON catalog.ticker = rw.ticker
         AND catalog.accession_number = rw.accession_number
        WHERE rw.run_id = ?
        GROUP BY rw.ticker
        """,
        (run_id,),
    ).fetchall()
    return {
        str(row["ticker"]): {
            "filing_count": int(row["filing_count"] or 0),
            "failed_count": int(row["failed_count"] or 0),
            "document_count": int(row["document_count"] or 0),
        }
        for row in rows
    }


def _evidence_rows(
    conn: sqlite3.Connection,
    *,
    run_id: int,
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    output: dict[tuple[str, str], list[dict[str, Any]]] = {}
    rows = conn.execute(
        """
        SELECT e.evidence_key, e.ticker, e.metric_name, e.period_end,
               e.candidate_status, e.candidate_value, e.unit,
               e.status_reason, e.source_document, e.confidence
        FROM sec_parser_run_metric_evidence AS run_evidence
        JOIN sec_parser_metric_evidence_shadow AS e
          ON e.evidence_key = run_evidence.evidence_key
        WHERE run_evidence.run_id = ?
        ORDER BY e.ticker, e.metric_name, e.period_end,
                 e.candidate_status, e.confidence DESC, e.evidence_key
        """,
        (run_id,),
    ).fetchall()
    for row in rows:
        key = (str(row["ticker"]), str(row["metric_name"]))
        output.setdefault(key, []).append(dict(row))
    return output


def _classify(
    *,
    baseline_status: str,
    accepted_current: int,
    accepted_historical: int,
    review_required: int,
    rejected: int,
    baseline_rejected_match: bool,
    evidence_parser_failures: int,
    filing_count: int,
    document_count: int,
    failed_count: int,
    missing_cache_count: int,
) -> tuple[str, str, str]:
    if baseline_status in STRUCTURAL_STATUSES:
        return (
            "STRUCTURAL_NA",
            baseline_status,
            "baseline_policy_marks_metric_structurally_unavailable",
        )
    if baseline_status in COVERED_STATUSES:
        if accepted_current:
            return (
                "CONFIRMED_REPORTED",
                baseline_status,
                "shadow_parser_confirmed_current_baseline_evidence",
            )
        if accepted_historical:
            return (
                "BASELINE_REPORTED_HISTORICAL_ONLY",
                baseline_status,
                "shadow_parser_found_history_but_not_current_anchor",
            )
        if baseline_rejected_match:
            return (
                "BASELINE_POLICY_CORRECTION",
                "NOT_DISCLOSED",
                "baseline_value_is_intentionally_suppressed_by_shadow_policy",
            )
        return (
            "BASELINE_REPORTED_UNCONFIRMED",
            baseline_status,
            "baseline_is_covered_but_shadow_search_did_not_confirm_current_fact",
        )
    if accepted_current:
        return (
            "RECOVERED_REPORTED",
            "REPORTED_SHADOW",
            "accepted_shadow_evidence_matches_current_anchor_period",
        )
    if accepted_historical:
        return (
            "HISTORICAL_RECOVERY_ONLY",
            baseline_status,
            "accepted_shadow_evidence_does_not_match_current_anchor_period",
        )
    if review_required:
        return (
            "FOUND_AMBIGUOUS",
            baseline_status,
            "candidate_requires_scope_period_or_semantic_review",
        )
    if rejected:
        return (
            "DISCLOSURE_REJECTED_POLICY",
            baseline_status,
            "only_policy_rejected_or_suppressed candidates were found",
        )
    if evidence_parser_failures:
        return (
            "PARSER_FAILURE",
            baseline_status,
            "source_document_could_not_be_converted_to_searchable_text",
        )
    if filing_count == 0 or document_count == 0:
        return (
            "SOURCE_DOCUMENT_MISSING",
            baseline_status,
            "no_cached_source_document_was_available_for_search",
        )
    if missing_cache_count:
        return (
            "SOURCE_DOCUMENT_INCOMPLETE",
            baseline_status,
            "part_of_the_selected_filing_window_was_not_cached",
        )
    if failed_count >= filing_count:
        return (
            "PARSER_FAILURE",
            baseline_status,
            "every_scheduled_filing_failed_parser_execution",
        )
    return (
        "NOT_FOUND_IN_SEARCHED_DOCUMENTS",
        baseline_status,
        "no_matching_fact_or_disclosure_candidate_found",
    )


def build_recovery_assessments(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    registry: AdapterRegistry,
    asof_date: str,
    tickers: Iterable[str],
    missing_cache_details: Iterable[dict[str, str]] = (),
) -> list[dict[str, Any]]:
    selected = sorted(
        {
            str(ticker).strip().upper()
            for ticker in tickers
            if str(ticker).strip()
        }
    )
    if not selected:
        selected = [
            str(row["ticker"])
            for row in conn.execute(
                """
                SELECT DISTINCT ticker
                FROM sec_parser_run_work
                WHERE run_id = ?
                ORDER BY ticker
                """,
                (run_id,),
            )
        ]
    baseline = _baseline_rows(
        conn,
        model_family=registry.model_family,
        asof_date=asof_date,
        tickers=selected,
    )
    anchors = _anchor_periods(
        conn,
        model_family=registry.model_family,
        asof_date=asof_date,
        tickers=selected,
    )
    work = _work_stats(conn, run_id=run_id)
    evidence = _evidence_rows(conn, run_id=run_id)
    missing_cache_by_ticker = Counter(
        str(item.get("ticker") or "").strip().upper()
        for item in missing_cache_details
        if str(item.get("ticker") or "").strip()
    )
    now = utc_now()
    output: list[dict[str, Any]] = []
    for ticker in selected:
        stats = work.get(
            ticker,
            {"filing_count": 0, "failed_count": 0, "document_count": 0},
        )
        for request in registry.source_metrics:
            metric_name = request.metric_name
            baseline_row = baseline.get((ticker, metric_name), {})
            baseline_status = str(
                baseline_row.get("availability_status") or "UNKNOWN"
            )
            anchor = str(
                baseline_row.get("period_end")
                or anchors.get(ticker, "")
            )
            metric_evidence = evidence.get((ticker, metric_name), [])
            accepted_by_observation: dict[
                tuple[str, float | None, str],
                dict[str, Any],
            ] = {}
            for row in metric_evidence:
                if str(row["candidate_status"]) != "ACCEPTED":
                    continue
                observation_key = (
                    str(row["period_end"] or ""),
                    (
                        round(float(row["candidate_value"]), 6)
                        if row["candidate_value"] is not None
                        else None
                    ),
                    str(row["unit"] or ""),
                )
                current = accepted_by_observation.get(observation_key)
                if current is None or float(row["confidence"] or 0.0) > float(
                    current["confidence"] or 0.0
                ):
                    accepted_by_observation[observation_key] = row
            accepted = list(accepted_by_observation.values())
            baseline_value = baseline_row.get("metric_value")
            if (
                baseline_status in COVERED_STATUSES
                and baseline_value is not None
            ):
                matching_baseline_rows = [
                    row
                    for row in accepted
                    if row["candidate_value"] is not None
                    and abs(
                        float(row["candidate_value"])
                        - float(baseline_value)
                    )
                    <= max(1.0, abs(float(baseline_value)) * 1e-9)
                    and str(row["period_end"] or "")
                ]
                if matching_baseline_rows:
                    anchor = max(
                        str(row["period_end"])
                        for row in matching_baseline_rows
                    )
            accepted_periods = [
                str(row["period_end"])
                for row in accepted
                if str(row["period_end"] or "")
                and str(row["period_end"]) <= asof_date
            ]
            if accepted_periods:
                anchor = max([anchor, *accepted_periods])
            accepted_current_rows = [
                row
                for row in accepted
                if anchor and str(row["period_end"] or "") == anchor
            ]
            accepted_historical_rows = [
                row for row in accepted if row not in accepted_current_rows
            ]
            review_required = sum(
                str(row["candidate_status"]) in REVIEW_STATUSES
                for row in metric_evidence
            )
            rejected = sum(
                str(row["candidate_status"]).startswith(
                    ("REJECTED", "SUPPRESSED")
                )
                for row in metric_evidence
            )
            baseline_rejected_match = bool(
                baseline_value is not None
                and any(
                    str(row["candidate_status"]).startswith(
                        ("REJECTED", "SUPPRESSED")
                    )
                    and row["candidate_value"] is not None
                    and abs(
                        float(row["candidate_value"])
                        - float(baseline_value)
                    )
                    <= max(
                        1.0,
                        abs(float(baseline_value)) * 0.005,
                    )
                    for row in metric_evidence
                )
            )
            evidence_parser_failures = sum(
                str(row["candidate_status"]) == "PARSER_FAILURE"
                for row in metric_evidence
            )
            recovery_class, predicted_status, reason = _classify(
                baseline_status=baseline_status,
                accepted_current=len(accepted_current_rows),
                accepted_historical=len(accepted_historical_rows),
                review_required=review_required,
                rejected=rejected,
                baseline_rejected_match=baseline_rejected_match,
                evidence_parser_failures=evidence_parser_failures,
                filing_count=stats["filing_count"],
                document_count=stats["document_count"],
                failed_count=stats["failed_count"],
                missing_cache_count=missing_cache_by_ticker[ticker],
            )
            output.append(
                {
                    "run_id": run_id,
                    "model_family": registry.model_family,
                    "ticker": ticker,
                    "metric_name": metric_name,
                    "asof_date": asof_date,
                    "baseline_status": baseline_status,
                    "baseline_value": baseline_value,
                    "anchor_period_end": anchor,
                    "recovery_class": recovery_class,
                    "predicted_status": predicted_status,
                    "accepted_current_count": len(accepted_current_rows),
                    "accepted_historical_count": len(
                        accepted_historical_rows
                    ),
                    "review_required_count": review_required,
                    "rejected_count": rejected,
                    "parser_failure_count": evidence_parser_failures,
                    "searched_filing_count": stats["filing_count"],
                    "searched_document_count": stats["document_count"],
                    "failed_filing_count": stats["failed_count"],
                    "missing_cache_filing_count": (
                        missing_cache_by_ticker[ticker]
                    ),
                    "evidence_keys_json": json.dumps(
                        [str(row["evidence_key"]) for row in metric_evidence],
                        separators=(",", ":"),
                    ),
                    "status_reason": reason,
                    "created_at": now,
                }
            )
    return output


def persist_recovery_assessments(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    rows: Iterable[dict[str, Any]],
) -> None:
    records = list(rows)
    conn.execute(
        "DELETE FROM sec_parser_recovery_assessment WHERE run_id = ?",
        (run_id,),
    )
    if not records:
        conn.commit()
        return
    columns = tuple(records[0])
    placeholders = ",".join("?" for _ in columns)
    conn.executemany(
        f"""
        INSERT INTO sec_parser_recovery_assessment({','.join(columns)})
        VALUES ({placeholders})
        """,
        [tuple(record[column] for column in columns) for record in records],
    )
    conn.commit()


def assessment_summary(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    records = list(rows)
    per_metric: dict[str, dict[str, int]] = {}
    for metric_name in sorted(
        {str(row["metric_name"]) for row in records}
    ):
        metric_rows = [
            row for row in records if row["metric_name"] == metric_name
        ]
        applicable = [
            row
            for row in metric_rows
            if row["baseline_status"] not in STRUCTURAL_STATUSES
        ]
        baseline_covered = sum(
            row["baseline_status"] in COVERED_STATUSES
            for row in applicable
        )
        predicted_covered = sum(
            row["predicted_status"]
            in {*COVERED_STATUSES, "REPORTED_SHADOW"}
            for row in applicable
        )
        per_metric[metric_name] = {
            "applicable": len(applicable),
            "baseline_covered": baseline_covered,
            "predicted_covered": predicted_covered,
            "recovered_current": sum(
                row["recovery_class"] == "RECOVERED_REPORTED"
                for row in applicable
            ),
            "historical_only": sum(
                row["recovery_class"] == "HISTORICAL_RECOVERY_ONLY"
                for row in applicable
            ),
            "ambiguous": sum(
                row["recovery_class"] == "FOUND_AMBIGUOUS"
                for row in applicable
            ),
        }
    return {
        "assessment_count": len(records),
        "recovery_class_counts": dict(
            sorted(Counter(row["recovery_class"] for row in records).items())
        ),
        "metric_coverage": per_metric,
    }


def write_assessment_csv(
    path: Path,
    rows: Iterable[dict[str, Any]],
) -> None:
    records = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    columns = list(records[0]) if records else [
        "run_id",
        "model_family",
        "ticker",
        "metric_name",
        "asof_date",
        "recovery_class",
        "status_reason",
    ]
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(records)
    os.replace(temporary, path)

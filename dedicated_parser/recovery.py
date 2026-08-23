from __future__ import annotations

import csv
import json
import math
import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path

from dedicated_parser.atomic_io import atomic_text_writer
from typing import Any, Iterable, cast

from dedicated_parser.contracts import AdapterRegistry
from dedicated_parser.storage import utc_now


STRUCTURAL_STATUSES = frozenset({"EXEMPT", "NOT_APPLICABLE"})
COVERED_STATUSES = frozenset({"PROXY", "REPORTED"})
REVIEW_STATUSES = frozenset({"REVIEW_REQUIRED"})


def _finite_float_or_none(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        number = float(cast(Any, value))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        is not None
    )


def _table_columns(conn: sqlite3.Connection, name: str) -> set[str]:
    if not _table_exists(conn, name):
        return set()
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({name})")}


def _baseline_rows(
    conn: sqlite3.Connection,
    *,
    registry: AdapterRegistry,
    model_family: str,
    asof_date: str,
    tickers: list[str],
) -> dict[tuple[str, str], dict[str, Any]]:
    if not tickers:
        return {}
    placeholders = ",".join("?" for _ in tickers)
    output: dict[tuple[str, str], dict[str, Any]] = {}
    if _table_exists(conn, "feature_financial_metric_availability"):
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
        output.update({(str(row["ticker"]), str(row["metric_name"])): dict(row) for row in rows})

    # Families introduced before metric-availability classification can still
    # have valid PIT source metrics in the financial feature table. Treat
    # those rows as the legacy baseline instead of falsely reporting zero
    # pre-parser coverage. Explicit availability rows always win.
    feature_columns = _table_columns(conn, "feature_financial_statement")
    source_fields = {
        request.metric_name for request in registry.source_metrics if request.metric_name in feature_columns
    }
    required_columns = {
        "ticker",
        "model_family",
        "asof_date",
        "fiscal_period_end",
    }
    if not source_fields or not required_columns <= feature_columns:
        return output
    selected_fields = ", ".join(f"ranked.{field}" for field in sorted(source_fields))
    confidence_order = "COALESCE(f.financial_confidence, 0.0)" if "financial_confidence" in feature_columns else "0.0"
    source_order = "COALESCE(f.source_id, '')" if "source_id" in feature_columns else "''"
    feature_rows = conn.execute(
        f"""
        WITH ranked AS (
            SELECT f.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY f.ticker
                       ORDER BY f.asof_date DESC,
                                {confidence_order} DESC,
                                {source_order} ASC
                   ) AS row_number
            FROM feature_financial_statement AS f
            WHERE f.model_family = ?
              AND f.asof_date <= ?
              AND f.ticker IN ({placeholders})
        )
        SELECT ranked.ticker, ranked.fiscal_period_end, {selected_fields}
        FROM ranked
        WHERE ranked.row_number = 1
        """,
        (model_family, asof_date, *tickers),
    ).fetchall()
    for row in feature_rows:
        ticker = str(row["ticker"])
        for metric_name in source_fields:
            key = (ticker, metric_name)
            if key in output:
                continue
            value = row[metric_name]
            output[key] = {
                "ticker": ticker,
                "metric_name": metric_name,
                "availability_status": ("REPORTED" if value is not None else "NOT_DISCLOSED"),
                "metric_value": value,
                "period_end": row["fiscal_period_end"],
                "status_reason": (
                    "legacy_feature_value_before_availability_classifier"
                    if value is not None
                    else "legacy_feature_value_missing"
                ),
            }
    return output


def _anchor_periods(
    conn: sqlite3.Connection,
    *,
    model_family: str,
    asof_date: str,
    tickers: list[str],
) -> dict[str, str]:
    required_columns = {
        "ticker", "model_family", "asof_date", "fiscal_period_end"
    }
    if (
        not tickers
        or not required_columns
        <= _table_columns(conn, "feature_financial_statement")
    ):
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
          ON catalog.cik = ledger.cik
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
               e.period_start, e.form_type, e.filing_date, e.accepted_at,
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
    current_match_mode: str = "exact_anchor",
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
                (
                    "shadow_parser_confirmed_recent_disclosure_under_metric_freshness_policy"
                    if current_match_mode == "metric_freshness_fallback"
                    else "shadow_parser_confirmed_current_baseline_evidence"
                ),
            )
        # A policy-rejected current baseline is a correction even when
        # historical-period evidence exists — checking historical first would
        # keep the rejected baseline counted as covered, contradicting the
        # corrected-coverage contract.
        if baseline_rejected_match:
            return (
                "BASELINE_POLICY_CORRECTION",
                "NOT_DISCLOSED",
                "baseline_value_is_intentionally_suppressed_by_shadow_policy",
            )
        if accepted_historical:
            return (
                "BASELINE_REPORTED_HISTORICAL_ONLY",
                baseline_status,
                "shadow_parser_found_history_but_not_current_anchor",
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
            (
                "accepted_shadow_evidence_within_metric_freshness_policy"
                if current_match_mode == "metric_freshness_fallback"
                else "accepted_shadow_evidence_matches_current_anchor_period"
            ),
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


def _parse_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _freshness_fallback_rows(
    rows: list[dict[str, Any]],
    *,
    metric_name: str,
    asof_date: str,
    max_age_days: int,
) -> tuple[list[dict[str, Any]], int | None]:
    """Select the latest valid disclosure under a metric-specific age limit."""
    if max_age_days <= 0:
        return [], None
    asof = _parse_date(asof_date)
    if asof is None:
        return [], None
    eligible: list[tuple[date, dict[str, Any]]] = []
    for row in rows:
        period_end = _parse_date(row.get("period_end"))
        available_date = _parse_date(row.get("accepted_at") or row.get("filing_date"))
        if (
            period_end is None
            or available_date is None
            or period_end > asof
            or available_date > asof
            or period_end > available_date
        ):
            continue
        age_days = (asof - period_end).days
        if age_days > max_age_days:
            continue
        if metric_name == "orders":
            period_start = _parse_date(row.get("period_start"))
            if period_start is None:
                continue
            duration_days = (period_end - period_start).days
            if not 300 <= duration_days <= 400:
                continue
        eligible.append((period_end, row))
    if not eligible:
        return [], None
    latest_period = max(period for period, _ in eligible)
    selected = [row for period, row in eligible if period == latest_period]
    return selected, (asof - latest_period).days


def build_recovery_assessments(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    registry: AdapterRegistry,
    asof_date: str,
    tickers: Iterable[str],
    missing_cache_details: Iterable[dict[str, str]] = (),
) -> list[dict[str, Any]]:
    selected = sorted({str(ticker).strip().upper() for ticker in tickers if str(ticker).strip()})
    if not selected:
        # Universe mode: run_work only contains tickers that had cached work.
        # A ticker whose entire filing window is absent from cache has no
        # run_work rows and would silently receive NO recovery class at all —
        # union in the missing-cache tickers so every requested pair is
        # classified (mirrors the reassess path).
        run_work_tickers = {
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
        }
        missing_tickers = {
            str(detail.get("ticker") or "").strip().upper()
            for detail in missing_cache_details
            if str(detail.get("ticker") or "").strip()
        }
        selected = sorted(run_work_tickers | missing_tickers)
    baseline = _baseline_rows(
        conn,
        registry=registry,
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
            baseline_status = str(baseline_row.get("availability_status") or "UNKNOWN")
            anchor = str(baseline_row.get("period_end") or anchors.get(ticker, ""))
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
                    (round(float(row["candidate_value"]), 6) if row["candidate_value"] is not None else None),
                    str(row["unit"] or ""),
                )
                current = accepted_by_observation.get(observation_key)
                if current is None or float(row["confidence"] or 0.0) > float(current["confidence"] or 0.0):
                    accepted_by_observation[observation_key] = row
            accepted = list(accepted_by_observation.values())
            baseline_value = baseline_row.get("metric_value")
            baseline_numeric = _finite_float_or_none(baseline_value)
            if (
                baseline_status in COVERED_STATUSES
                and baseline_numeric is not None
            ):
                matching_baseline_rows = [
                    row
                    for row in accepted
                    if row["candidate_value"] is not None
                    and abs(
                        float(row["candidate_value"]) - baseline_numeric
                    )
                    <= max(1.0, abs(baseline_numeric) * 1e-9)
                    and str(row["period_end"] or "")
                    # PIT guard (matches accepted_periods below): a post-asof
                    # period must never become the anchor, or forward RPO
                    # windows demote legitimately-current rows to historical.
                    and str(row["period_end"]) <= asof_date
                ]
                if matching_baseline_rows:
                    anchor = max(str(row["period_end"]) for row in matching_baseline_rows)
            accepted_periods = [
                str(row["period_end"])
                for row in accepted
                if str(row["period_end"] or "") and str(row["period_end"]) <= asof_date
            ]
            if accepted_periods:
                anchor = max([anchor, *accepted_periods])
            accepted_current_rows = [row for row in accepted if anchor and str(row["period_end"] or "") == anchor]
            current_match_mode = "exact_anchor" if accepted_current_rows else "none"
            current_evidence_age_days: int | None = None
            if accepted_current_rows:
                current_period = _parse_date(anchor)
                current_asof = _parse_date(asof_date)
                if current_period is not None and current_asof is not None:
                    current_evidence_age_days = (current_asof - current_period).days
            if not accepted_current_rows:
                (
                    accepted_current_rows,
                    current_evidence_age_days,
                ) = _freshness_fallback_rows(
                    accepted,
                    metric_name=metric_name,
                    asof_date=asof_date,
                    max_age_days=int(registry.metric_freshness_days.get(metric_name, 0)),
                )
                if accepted_current_rows:
                    current_match_mode = "metric_freshness_fallback"
            current_evidence_period_end = (
                max(str(row["period_end"]) for row in accepted_current_rows if str(row["period_end"] or ""))
                if accepted_current_rows
                else ""
            )
            accepted_historical_rows = [row for row in accepted if row not in accepted_current_rows]
            review_required = sum(str(row["candidate_status"]) in REVIEW_STATUSES for row in metric_evidence)
            rejected = sum(
                str(row["candidate_status"]).startswith(("REJECTED", "SUPPRESSED")) for row in metric_evidence
            )
            baseline_rejected_match = bool(
                baseline_numeric is not None
                and any(
                    str(row["candidate_status"]).startswith(("REJECTED", "SUPPRESSED"))
                    and row["candidate_value"] is not None
                    and abs(
                        float(row["candidate_value"]) - baseline_numeric
                    )
                    <= max(
                        1.0,
                        abs(baseline_numeric) * 0.005,
                    )
                    for row in metric_evidence
                )
            )
            evidence_parser_failures = sum(str(row["candidate_status"]) == "PARSER_FAILURE" for row in metric_evidence)
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
                current_match_mode=current_match_mode,
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
                    "current_match_mode": current_match_mode,
                    "current_evidence_period_end": (current_evidence_period_end),
                    "current_evidence_age_days": (current_evidence_age_days),
                    "recovery_class": recovery_class,
                    "predicted_status": predicted_status,
                    "accepted_current_count": len(accepted_current_rows),
                    "accepted_historical_count": len(accepted_historical_rows),
                    "review_required_count": review_required,
                    "rejected_count": rejected,
                    "parser_failure_count": evidence_parser_failures,
                    "searched_filing_count": stats["filing_count"],
                    "searched_document_count": stats["document_count"],
                    "failed_filing_count": stats["failed_count"],
                    "missing_cache_filing_count": (missing_cache_by_ticker[ticker]),
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
        INSERT INTO sec_parser_recovery_assessment({",".join(columns)})
        VALUES ({placeholders})
        """,
        [tuple(record[column] for column in columns) for record in records],
    )
    conn.commit()


def assessment_summary(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    records = list(rows)
    per_metric: dict[str, dict[str, int]] = {}
    for metric_name in sorted({str(row["metric_name"]) for row in records}):
        metric_rows = [row for row in records if row["metric_name"] == metric_name]
        applicable = [row for row in metric_rows if row["baseline_status"] not in STRUCTURAL_STATUSES]
        baseline_covered = sum(row["baseline_status"] in COVERED_STATUSES for row in applicable)
        predicted_covered = sum(row["predicted_status"] in {*COVERED_STATUSES, "REPORTED_SHADOW"} for row in applicable)
        per_metric[metric_name] = {
            "applicable": len(applicable),
            "baseline_covered": baseline_covered,
            "predicted_covered": predicted_covered,
            "recovered_current": sum(row["recovery_class"] == "RECOVERED_REPORTED" for row in applicable),
            "historical_only": sum(row["recovery_class"] == "HISTORICAL_RECOVERY_ONLY" for row in applicable),
            "ambiguous": sum(row["recovery_class"] == "FOUND_AMBIGUOUS" for row in applicable),
        }
    return {
        "assessment_count": len(records),
        "recovery_class_counts": dict(sorted(Counter(row["recovery_class"] for row in records).items())),
        "metric_coverage": per_metric,
    }


def write_assessment_csv(
    path: Path,
    rows: Iterable[dict[str, Any]],
) -> None:
    records = list(rows)
    columns = (
        list(records[0])
        if records
        else [
            "run_id",
            "model_family",
            "ticker",
            "metric_name",
            "asof_date",
            "recovery_class",
            "status_reason",
        ]
    )
    with atomic_text_writer(path, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(records)

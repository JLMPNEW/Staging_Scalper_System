#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.adapters import load_registry  # noqa: E402
from dedicated_parser.contracts import file_sha256  # noqa: E402
from dedicated_parser.storage import connect_database  # noqa: E402
from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT.parent / "config.yaml"
ADAPTER = "industrials.defense.dedicated_parser_adapter:extract_metric_evidence"
OUTPUT_FIELDS = [
    "record_type",
    "review_priority",
    "run_id",
    "asof_date",
    "ticker",
    "company_name",
    "cik",
    "calibration_cohort",
    "membership_status",
    "metric_name",
    "recovery_class",
    "baseline_status",
    "baseline_value",
    "predicted_status",
    "current_match_mode",
    "current_evidence_period_end",
    "current_evidence_age_days",
    "candidate_status",
    "candidate_value",
    "unit",
    "period_start",
    "period_end",
    "scope",
    "confidence",
    "concept_name",
    "accession_number",
    "form_type",
    "filing_date",
    "accepted_at",
    "report_date",
    "source_document",
    "source_path",
    "source_content_sha256",
    "evidence_key",
    "extraction_method",
    "status_reason",
    "evidence_text",
    "provenance_json",
    "searched_filing_count",
    "searched_document_count",
    "failed_filing_count",
    "missing_cache_filing_count",
    "review_decision",
    "review_notes",
    "reviewed_by",
    "reviewed_at",
]
HIGH_PRIORITY_CLASSES = frozenset(
    {
        "FOUND_AMBIGUOUS",
        "RECOVERED_REPORTED",
        "BASELINE_REPORTED_UNCONFIRMED",
        "PARSER_FAILURE",
        "SOURCE_DOCUMENT_INCOMPLETE",
    }
)
KNOWN_EDGARTOOLS_STDERR_PREFIX = "Subheader 'COMPANY DATA' not found in header"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build the complete defense evidence-review package after "
            "exhaustive cache hydration and full-universe shadow extraction."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--run-id", type=int, default=0)
    parser.add_argument(
        "--hydration-manifest",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--hydration-sync-csv",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--hydration-validation-csv",
        type=Path,
        default=None,
    )
    parser.add_argument("--parser-stderr-log", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--summary-json", type=Path, default=None)
    return parser.parse_args(argv)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _load_hydration_manifest(path: Path, *, asof_date: str) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"Hydration manifest not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Hydration manifest is not an object: {path}")
    if str(payload.get("asof_date") or "") != asof_date:
        raise ValueError("Hydration manifest asof does not match review asof")
    if not bool(payload.get("exhaustive")):
        raise ValueError("Evidence review requires exhaustive hydration")
    if not bool(payload.get("review_ready")):
        raise ValueError("Hydration has not completed or exhausted all configured passes")
    if int(payload.get("max_filings_per_ticker", -1)) != 0:
        raise ValueError("Exhaustive hydration must use unlimited filings")
    if int(payload.get("max_documents_per_filing", -1)) != 0:
        raise ValueError("Exhaustive hydration must use unlimited documents")
    return payload


def _read_hydration_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise ValueError(f"Hydration audit CSV not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _validate_event_catalog(
    hydration: dict[str, Any],
) -> dict[str, Any]:
    catalog = hydration.get("catalog")
    if not isinstance(catalog, dict) or catalog.get("status") == "NOT_REQUESTED":
        return {
            "status": "NOT_AVAILABLE_LEGACY_HYDRATION",
            "catalog_row_count": 0,
            "cataloged_filing_count": 0,
            "tickers_with_event_filings": 0,
        }
    if str(catalog.get("status") or "") != "COMPLETED":
        raise ValueError("Event filing catalog did not complete successfully")
    path = Path(str(catalog.get("output_csv") or "")).expanduser().resolve()
    rows = _read_hydration_rows(path)
    expected_rows = int(catalog.get("ticker_count") or 0)
    tickers = [str(row.get("ticker") or "").strip().upper() for row in rows]
    duplicates = sorted(ticker for ticker, count in Counter(tickers).items() if ticker and count > 1)
    invalid_rows = [
        row
        for row in rows
        if str(row.get("status") or "") != "cataloged"
        or str(row.get("catalog_end_date") or "") != str(hydration.get("asof_date") or "")
        or int(row.get("missing_history_cache_count") or 0)
    ]
    if len(rows) != expected_rows or duplicates or invalid_rows:
        raise ValueError(
            "Event filing catalog audit failed: "
            f"rows={len(rows)}/{expected_rows}; "
            f"duplicates={duplicates[:10]}; "
            f"invalid_rows={len(invalid_rows)}"
        )
    expected_hash = str(catalog.get("output_csv_sha256") or "")
    actual_hash = file_sha256(path)
    if expected_hash != actual_hash:
        raise ValueError("Event filing catalog hash does not match hydration manifest")
    total_filings = sum(int(row.get("cataloged_filing_count") or 0) for row in rows)
    if total_filings <= 0:
        raise ValueError("Event filing catalog contains zero 8-K/8-K-A filings")
    return {
        "status": "PASS",
        "path": str(path),
        "sha256": actual_hash,
        "catalog_row_count": len(rows),
        "cataloged_filing_count": total_filings,
        "tickers_with_event_filings": sum(int(row.get("cataloged_filing_count") or 0) > 0 for row in rows),
        "forms": list(catalog.get("forms") or []),
        "start_date": str(catalog.get("start_date") or ""),
        "end_date": str(hydration.get("asof_date") or ""),
    }


def _validate_hydration_audit(
    hydration: dict[str, Any],
    *,
    sync_csv: Path,
    validation_csv: Path,
) -> dict[str, Any]:
    before = hydration.get("before")
    if not isinstance(before, dict):
        raise ValueError("Hydration manifest is missing the preflight audit")
    initial_gap_count = int(before.get("missing_cache_accessions") or 0)
    if not initial_gap_count:
        return {
            "status": "NOT_REQUIRED_NO_INITIAL_GAPS",
            "sync_csv": "",
            "sync_row_count": 0,
            "original_failure_count": 0,
            "supplemental_validation_csv": "",
            "supplemental_validation_row_count": 0,
        }

    sync_rows = _read_hydration_rows(sync_csv)
    failed_rows = [row for row in sync_rows if str(row.get("status") or "") != "cache_hydrated"]
    if not failed_rows:
        return {
            "status": "PASS",
            "sync_csv": str(sync_csv),
            "sync_csv_sha256": file_sha256(sync_csv),
            "sync_row_count": len(sync_rows),
            "original_failure_count": 0,
            "supplemental_validation_csv": "",
            "supplemental_validation_row_count": 0,
        }

    validation_rows = _read_hydration_rows(validation_csv)
    successful_validation = {
        (
            str(row.get("ticker") or "").strip().upper(),
            str(row.get("cik") or "").strip(),
        )
        for row in validation_rows
        if str(row.get("status") or "") == "cache_hydrated"
    }
    unresolved = sorted(
        {
            (
                str(row.get("ticker") or "").strip().upper(),
                str(row.get("cik") or "").strip(),
            )
            for row in failed_rows
        }
        - successful_validation
    )
    if unresolved:
        raise ValueError(f"Hydration failures lack successful supplemental validation: {unresolved}")
    return {
        "status": "PASS_WITH_SUPPLEMENTAL_VALIDATION",
        "sync_csv": str(sync_csv),
        "sync_csv_sha256": file_sha256(sync_csv),
        "sync_row_count": len(sync_rows),
        "original_failure_count": len(failed_rows),
        "original_failed_tickers": sorted({str(row.get("ticker") or "").strip().upper() for row in failed_rows}),
        "supplemental_validation_csv": str(validation_csv),
        "supplemental_validation_csv_sha256": file_sha256(validation_csv),
        "supplemental_validation_row_count": len(validation_rows),
    }


def _audit_parser_stderr(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "status": "NOT_AVAILABLE",
            "path": "",
            "line_count": 0,
            "known_warning_count": 0,
            "unknown_line_count": 0,
        }
    lines = [
        line.strip()
        for line in path.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()
        if line.strip()
    ]
    known = [line for line in lines if line.startswith(KNOWN_EDGARTOOLS_STDERR_PREFIX)]
    unknown = [line for line in lines if line not in known]
    if unknown:
        raise ValueError(f"Parser stderr contains unclassified messages: {sorted(set(unknown))[:10]}")
    return {
        "status": "PASS_KNOWN_WARNINGS" if known else "PASS_EMPTY",
        "path": str(path),
        "sha256": file_sha256(path),
        "line_count": len(lines),
        "known_warning_count": len(known),
        "unknown_line_count": 0,
        "unique_known_messages": sorted(set(known)),
    }


def _load_run(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    asof_date: str,
    adapter_version: str,
) -> tuple[sqlite3.Row, dict[str, Any]]:
    row = (
        conn.execute(
            "SELECT * FROM sec_parser_run WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if run_id
        else conn.execute(
            """
            SELECT *
            FROM sec_parser_run
            WHERE model_family = 'defense'
              AND asof_date = ?
              AND adapter_version = ?
            ORDER BY run_id DESC
            LIMIT 1
            """,
            (asof_date, adapter_version),
        ).fetchone()
    )
    if row is None:
        raise ValueError("No matching defense parser run was found")
    if (
        str(row["model_family"]) != "defense"
        or str(row["asof_date"]) != asof_date
        or str(row["adapter_version"]) != adapter_version
    ):
        raise ValueError("Parser run does not match defense/asof/adapter")
    if str(row["status"]) != "COMPLETED" or int(row["failed_work_count"] or 0):
        raise ValueError("Evidence review requires a zero-failure parser run")
    metadata = json.loads(str(row["metadata_json"] or "{}"))
    if not isinstance(metadata, dict):
        raise ValueError("Parser run metadata is not an object")
    plan = metadata.get("plan")
    if not isinstance(plan, dict):
        raise ValueError("Parser run is missing plan provenance")
    scope = plan.get("execution_scope")
    if not isinstance(scope, dict):
        raise ValueError("Parser run is missing execution-scope provenance")
    if int(scope.get("max_filings_per_ticker", -1)) != 0:
        raise ValueError("Evidence review requires unlimited parser filings")
    if int(scope.get("max_documents_per_filing", -1)) != 0:
        raise ValueError("Evidence review requires unlimited parser documents")
    if not bool(scope.get("all_metrics")):
        raise ValueError("Evidence review requires all-metrics extraction")
    return row, plan


def _universe_rows(
    conn: sqlite3.Connection,
    *,
    asof_date: str,
) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT m.ticker,
               COALESCE(c.company_name, '') AS company_name,
               COALESCE(c.cik, '') AS cik,
               COALESCE(t.calibration_cohort, '') AS calibration_cohort,
               MAX(
                   CASE
                       WHEN m.start_date <= ?
                        AND COALESCE(m.end_date, '9999-12-31') >= ?
                       THEN 1 ELSE 0
                   END
               ) AS current_member_flag
        FROM dim_universe_membership AS m
        LEFT JOIN dim_company AS c
          ON c.ticker = m.ticker
        LEFT JOIN dim_industrials_taxonomy AS t
          ON t.ticker = m.ticker
         AND t.model_family = m.model_family
        WHERE m.model_family = 'defense'
          AND m.start_date <= ?
        GROUP BY m.ticker, c.company_name, c.cik, t.calibration_cohort
        ORDER BY m.ticker
        """,
        (asof_date, asof_date, asof_date),
    ).fetchall()
    return {
        str(row["ticker"]): {
            **dict(row),
            "membership_status": ("active" if int(row["current_member_flag"] or 0) else "historical"),
        }
        for row in rows
    }


def _review_priority(
    *,
    recovery_class: str,
    candidate_status: str,
) -> str:
    if recovery_class in HIGH_PRIORITY_CLASSES:
        return "1_high"
    if candidate_status == "ACCEPTED":
        return "2_accepted_validation"
    if candidate_status == "REVIEW_REQUIRED":
        return "2_review_required"
    if candidate_status.startswith(("REJECTED", "SUPPRESSED")):
        return "3_policy_rejection_validation"
    return "4_no_evidence_or_structural"


def build_review_package(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    asof_date: str,
    metric_names: tuple[str, ...],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    universe = _universe_rows(conn, asof_date=asof_date)
    assessments = conn.execute(
        """
        SELECT *
        FROM sec_parser_recovery_assessment
        WHERE run_id = ? AND model_family = 'defense'
        ORDER BY ticker, metric_name
        """,
        (run_id,),
    ).fetchall()
    assessment_by_pair = {(str(row["ticker"]), str(row["metric_name"])): row for row in assessments}
    expected_pairs = {(ticker, metric_name) for ticker in universe for metric_name in metric_names}
    missing_pairs = sorted(expected_pairs - set(assessment_by_pair))
    unexpected_pairs = sorted(set(assessment_by_pair) - expected_pairs)
    if missing_pairs or unexpected_pairs:
        raise ValueError(f"Assessment matrix mismatch: missing={missing_pairs[:10]} unexpected={unexpected_pairs[:10]}")

    evidence_rows = conn.execute(
        """
        SELECT e.*,
               (
                   SELECT d.source_path
                   FROM sec_parser_document_catalog AS d
                   WHERE d.cik = e.cik
                     AND d.accession_number = e.accession_number
                     AND d.document_name = e.source_document
                   ORDER BY d.cataloged_at DESC
                   LIMIT 1
               ) AS source_path,
               (
                   SELECT d.content_sha256
                   FROM sec_parser_document_catalog AS d
                   WHERE d.cik = e.cik
                     AND d.accession_number = e.accession_number
                     AND d.document_name = e.source_document
                   ORDER BY d.cataloged_at DESC
                   LIMIT 1
               ) AS source_content_sha256
        FROM sec_parser_run_metric_evidence AS relation
        JOIN sec_parser_metric_evidence_shadow AS e
          ON e.evidence_key = relation.evidence_key
        WHERE relation.run_id = ?
        ORDER BY e.ticker, e.metric_name, e.period_end,
                 e.accession_number, e.evidence_key
        """,
        (run_id,),
    ).fetchall()
    evidence_by_pair: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in evidence_rows:
        evidence_by_pair.setdefault(
            (str(row["ticker"]), str(row["metric_name"])),
            [],
        ).append(row)

    output: list[dict[str, Any]] = []
    for ticker, metric_name in sorted(expected_pairs):
        membership = universe[ticker]
        assessment = assessment_by_pair[(ticker, metric_name)]
        candidates = evidence_by_pair.get((ticker, metric_name), [])
        if not candidates:
            candidates = [None]
        for evidence in candidates:
            candidate_status = str(evidence["candidate_status"]) if evidence is not None else ""
            row = {
                "record_type": ("evidence" if evidence is not None else "assessment_no_evidence"),
                "review_priority": _review_priority(
                    recovery_class=str(assessment["recovery_class"]),
                    candidate_status=candidate_status,
                ),
                "run_id": run_id,
                "asof_date": asof_date,
                "ticker": ticker,
                "company_name": membership["company_name"],
                "cik": membership["cik"],
                "calibration_cohort": membership["calibration_cohort"],
                "membership_status": membership["membership_status"],
                "metric_name": metric_name,
                "recovery_class": assessment["recovery_class"],
                "baseline_status": assessment["baseline_status"],
                "baseline_value": assessment["baseline_value"],
                "predicted_status": assessment["predicted_status"],
                "current_match_mode": assessment["current_match_mode"],
                "current_evidence_period_end": assessment["current_evidence_period_end"],
                "current_evidence_age_days": assessment["current_evidence_age_days"],
                "searched_filing_count": assessment["searched_filing_count"],
                "searched_document_count": assessment["searched_document_count"],
                "failed_filing_count": assessment["failed_filing_count"],
                "missing_cache_filing_count": assessment["missing_cache_filing_count"],
                "review_decision": "",
                "review_notes": "",
                "reviewed_by": "",
                "reviewed_at": "",
            }
            for field in (
                "candidate_status",
                "candidate_value",
                "unit",
                "period_start",
                "period_end",
                "scope",
                "confidence",
                "concept_name",
                "accession_number",
                "form_type",
                "filing_date",
                "accepted_at",
                "report_date",
                "source_document",
                "source_path",
                "source_content_sha256",
                "evidence_key",
                "extraction_method",
                "status_reason",
                "evidence_text",
                "provenance_json",
            ):
                row[field] = evidence[field] if evidence is not None else ""
            output.append(row)

    status_counts = Counter(str(row["candidate_status"] or "NO_EVIDENCE") for row in output)
    priority_counts = Counter(str(row["review_priority"]) for row in output)
    no_evidence_rows = [row for row in output if row["record_type"] == "assessment_no_evidence"]
    no_evidence_priority_counts = Counter(str(row["review_priority"]) for row in no_evidence_rows)
    reconciliation_counts = Counter(
        (
            str(row["record_type"]),
            str(row["recovery_class"]),
            str(row["review_priority"]),
        )
        for row in output
    )
    summary = {
        "acceptance": "PASS",
        "asof_date": asof_date,
        "run_id": run_id,
        "ticker_count": len(universe),
        "active_ticker_count": sum(row["membership_status"] == "active" for row in universe.values()),
        "historical_ticker_count": sum(row["membership_status"] == "historical" for row in universe.values()),
        "metric_count": len(metric_names),
        "expected_assessment_pairs": len(expected_pairs),
        "assessment_pair_count": len(assessment_by_pair),
        "review_row_count": len(output),
        "evidence_row_count": len(evidence_rows),
        "no_evidence_pair_count": len(no_evidence_rows),
        "no_evidence_priority_counts": dict(sorted(no_evidence_priority_counts.items())),
        "high_priority_no_evidence_pairs": [
            {
                "ticker": str(row["ticker"]),
                "metric_name": str(row["metric_name"]),
                "recovery_class": str(row["recovery_class"]),
            }
            for row in no_evidence_rows
            if row["review_priority"] == "1_high"
        ],
        "review_reconciliation_counts": [
            {
                "record_type": record_type,
                "recovery_class": recovery_class,
                "review_priority": review_priority,
                "row_count": count,
            }
            for (
                record_type,
                recovery_class,
                review_priority,
            ), count in sorted(reconciliation_counts.items())
        ],
        "candidate_status_counts": dict(sorted(status_counts.items())),
        "review_priority_counts": dict(sorted(priority_counts.items())),
    }
    return output, summary


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = (
        args.db.expanduser().resolve()
        if args.db is not None
        else resolve_path(
            cfg_get(config, "paths.database_path"),
            base_dir=config_path.parent,
        )
    )
    output_root = resolve_path(
        cfg_get(
            config,
            "dedicated_parser.output_root",
            "../output/industrials/defense/dedicated_parser",
        ),
        base_dir=config_path.parent,
    )
    output_dir = output_root / args.asof
    hydration_manifest_path = (
        args.hydration_manifest.expanduser().resolve()
        if args.hydration_manifest is not None
        else output_dir / "dedicated_parser_cache_hydration.json"
    )
    hydration = _load_hydration_manifest(
        hydration_manifest_path,
        asof_date=args.asof,
    )
    event_catalog_audit = _validate_event_catalog(hydration)
    hydration_sync_csv = (
        args.hydration_sync_csv.expanduser().resolve()
        if args.hydration_sync_csv is not None
        else output_dir / "dedicated_parser_cache_hydration_sync.csv"
    )
    hydration_validation_csv = (
        args.hydration_validation_csv.expanduser().resolve()
        if args.hydration_validation_csv is not None
        else output_dir / "dedicated_parser_shared_cik_validation.csv"
    )
    hydration_audit = _validate_hydration_audit(
        hydration,
        sync_csv=hydration_sync_csv,
        validation_csv=hydration_validation_csv,
    )
    parser_stderr_log = (
        args.parser_stderr_log.expanduser().resolve()
        if args.parser_stderr_log is not None
        else output_dir / "exhaustive_extraction.stderr.log"
    )
    parser_stderr_audit = _audit_parser_stderr(parser_stderr_log)
    registry = load_registry(ADAPTER)
    with connect_database(db_path) as conn:
        run, plan = _load_run(
            conn,
            run_id=args.run_id,
            asof_date=args.asof,
            adapter_version=registry.adapter_version,
        )
        remaining_hydration_gaps = int(hydration.get("remaining_source_gap_count") or 0)
        run_missing_cache = int(plan.get("missing_cache_accessions") or 0)
        if run_missing_cache > remaining_hydration_gaps:
            raise ValueError(
                "Parser run has more cache gaps than the sealed hydration manifest; rerun extraction after hydration"
            )
        rows, summary = build_review_package(
            conn,
            run_id=int(run["run_id"]),
            asof_date=args.asof,
            metric_names=tuple(request.metric_name for request in registry.source_metrics),
        )
        total_filing_work_items = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM sec_parser_run_work
                WHERE run_id = ?
                """,
                (int(run["run_id"]),),
            ).fetchone()[0]
        )
        run_form_counts = {
            str(row["form_type"]): int(row["work_count"])
            for row in conn.execute(
                """
                SELECT UPPER(f.form_type) AS form_type,
                       COUNT(DISTINCT relation.work_key) AS work_count
                FROM sec_parser_run_work AS relation
                JOIN fact_sec_filing AS f
                  ON f.ticker = relation.ticker
                 AND f.accession_number = relation.accession_number
                 AND f.source_id = ?
                WHERE relation.run_id = ?
                GROUP BY UPPER(f.form_type)
                ORDER BY UPPER(f.form_type)
                """,
                (
                    str(
                        cfg_get(
                            config,
                            "sec_fundamentals.submissions_source_id",
                            "sec_submissions",
                        )
                    ),
                    int(run["run_id"]),
                ),
            )
        }
    event_filing_work_items = sum(
        count for form_type, count in run_form_counts.items() if form_type in {"8-K", "8-K/A"}
    )
    if event_catalog_audit["status"] == "PASS" and event_filing_work_items <= 0:
        raise ValueError("Event catalog is populated but the parser run contains zero 8-K/8-K-A work items")
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv is not None
        else output_dir / "defense_specialized_metric_evidence_review.csv"
    )
    summary_json = (
        args.summary_json.expanduser().resolve()
        if args.summary_json is not None
        else output_dir / "defense_specialized_metric_evidence_review_summary.json"
    )
    summary.update(
        {
            "hydration_manifest": str(hydration_manifest_path),
            "hydration_manifest_sha256": file_sha256(hydration_manifest_path),
            "hydration_status": hydration.get("status"),
            "hydration_audit": hydration_audit,
            "event_catalog_audit": event_catalog_audit,
            "parser_stderr_audit": parser_stderr_audit,
            "work_units": {
                "parser_run_id": int(run["run_id"]),
                "newly_executed_filing_work_items": int(run["completed_work_count"] or 0),
                "reused_completed_filing_work_items": int(plan.get("linked_completed_work_count") or 0),
                "total_filing_work_items": total_filing_work_items,
                "documents_for_new_work_items": int(plan.get("scheduled_documents") or 0),
                "filing_form_counts": run_form_counts,
                "event_filing_work_items": event_filing_work_items,
            },
            "remaining_source_gap_count": int(hydration.get("remaining_source_gap_count") or 0),
            "parser_run_missing_cache_accessions": int(plan.get("missing_cache_accessions") or 0),
            "output_csv": str(output_csv),
        }
    )
    write_csv_atomic(output_csv, OUTPUT_FIELDS, rows)
    summary["output_csv_sha256"] = file_sha256(output_csv)
    _write_json(summary_json, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

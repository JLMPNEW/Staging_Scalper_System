from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any

from dedicated_parser.adapters import load_registry
from dedicated_parser.adjudication import (
    build_adjudication_skeleton,
    write_adjudication_skeleton,
)
from dedicated_parser.benchmark import load_cohort_tickers
from dedicated_parser.comparison import compare_shadow_run
from dedicated_parser.contracts import AdapterRegistry, PlanSummary
from dedicated_parser.funnel import (
    build_extraction_funnel,
    write_funnel_csv,
)
from dedicated_parser.planner import (
    audit_cache_completeness,
    build_plan,
)
from dedicated_parser.policy import export_registry_golden_corpus
from dedicated_parser.recovery import (
    assessment_summary,
    build_recovery_assessments,
    persist_recovery_assessments,
    write_assessment_csv,
)
from dedicated_parser.runtime import (
    default_worker_count,
    execute_plan,
    validate_provider_dependencies,
)
from dedicated_parser.storage import (
    connect_database,
    finish_run,
    link_completed_work,
    load_run,
    merge_run_metadata,
    start_run,
    utc_now,
)


REVIEW_CLASSES = frozenset(
    {
        "BASELINE_REPORTED_UNCONFIRMED",
        "DISCLOSURE_REJECTED_POLICY",
        "FOUND_AMBIGUOUS",
        "PARSER_FAILURE",
        "SOURCE_DOCUMENT_MISSING",
        "SOURCE_DOCUMENT_INCOMPLETE",
    }
)


def _plan_summary_payload(summary: PlanSummary) -> dict[str, Any]:
    payload = asdict(summary)
    linked_work = payload.pop("skipped_completed_work", [])
    payload["linked_completed_work_count"] = len(linked_work)
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the shared SEC parser against existing database/cache content.")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--asof", default="")
    parser.add_argument(
        "--adapter",
        required=True,
        help=(
            "Sector adapter as module:function, e.g. "
            "industrials.machinery.dedicated_parser_adapter:"
            "extract_metric_evidence. Required: the shared parser must not "
            "default to any one sector's adapter."
        ),
    )
    parser.add_argument("--tickers", default="")
    parser.add_argument("--ticker-cohort", type=Path, default=None)
    parser.add_argument("--accessions", default="")
    parser.add_argument("--workers", type=int, default=default_worker_count())
    parser.add_argument("--max-filings-per-ticker", type=int, default=8)
    parser.add_argument("--max-documents-per-filing", type=int, default=16)
    parser.add_argument("--write-batch-size", type=int, default=8)
    parser.add_argument(
        "--provider-state-dir",
        type=Path,
        default=Path("tmp/edgartools"),
    )
    parser.add_argument("--output-json", type=Path, default=None)
    parser.add_argument("--cache-gate-output-json", type=Path, default=None)
    parser.add_argument(
        "--skip-adjudication-skeleton",
        action="store_true",
        help=(
            "Skip the large evidence-level adjudication CSV when a sector "
            "publishes its own governed pair-level review queue."
        ),
    )
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--require-complete-cache", action="store_true")
    parser.add_argument(
        "--reassess-run-id",
        type=int,
        default=0,
        help=("Rebuild assessments and funnel artifacts from an existing run without reparsing source documents."),
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--all-metrics",
        action="store_true",
        help=(
            "Evaluate every selected ticker/metric pair while still reusing "
            "completed work. Unlike --force, this does not reparse unchanged "
            "filings."
        ),
    )
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--disable-arelle", action="store_true")
    parser.add_argument("--disable-edgartools", action="store_true")
    parser.add_argument("--enable-pdf-ocr", action="store_true")
    parser.add_argument("--max-pdf-pages", type=int, default=250)
    parser.add_argument("--max-pdf-bytes", type=int, default=25_000_000)
    parser.add_argument(
        "--pdf-extraction-timeout-seconds",
        type=float,
        default=30.0,
    )
    return parser.parse_args(argv)


def _write_json(path: Path | None, payload: object) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _ticker_list(raw: object) -> list[str]:
    return sorted({ticker.strip().upper() for ticker in str(raw or "").split(",") if ticker.strip()})


def _accession_list(raw: object) -> list[str]:
    return sorted({accession.strip() for accession in str(raw or "").split(",") if accession.strip()})


def _missing_cache_details(metadata: dict[str, Any]) -> list[dict[str, str]]:
    plan = metadata.get("plan")
    if not isinstance(plan, dict):
        return []
    details = plan.get("missing_cache_details")
    if not isinstance(details, list):
        return []
    return [{str(key): str(value) for key, value in row.items()} for row in details if isinstance(row, dict)]


def _export_policy_corpus(registry: AdapterRegistry) -> None:
    if not registry.review_policy_path and not registry.review_policy_golden_path:
        return
    if not registry.review_policy_path or not registry.review_policy_golden_path:
        raise ValueError("Adapter registry must configure both review_policy_path and review_policy_golden_path")
    export_registry_golden_corpus(
        registry_path=Path(registry.review_policy_path),
        output_path=Path(registry.review_policy_golden_path),
        corpus_id=f"{registry.model_family}_review_policy_generated",
    )


def reassess_existing_run(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    registry: AdapterRegistry,
    tickers: list[str],
    requested_asof: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    run = load_run(conn, run_id=run_id)
    if str(run["model_family"]) != registry.model_family:
        raise ValueError(f"run_id={run_id} belongs to {run['model_family']!r}, not {registry.model_family!r}")
    asof_date = str(run["asof_date"])
    if requested_asof and requested_asof != asof_date:
        raise ValueError(f"--asof {requested_asof} does not match run_id={run_id} asof_date {asof_date}")
    try:
        metadata = json.loads(str(run.get("metadata_json") or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"run_id={run_id} has invalid metadata_json") from exc
    if not isinstance(metadata, dict):
        raise ValueError(f"run_id={run_id} metadata_json is not an object")
    requested_metric_sets = {
        frozenset(
            str(row.get("metric_name") or "")
            for row in json.loads(str(item["requested_metrics_json"]))
            if isinstance(row, dict) and str(row.get("metric_name") or "")
        )
        for item in conn.execute(
            """
            SELECT DISTINCT ledger.requested_metrics_json
            FROM sec_parser_run_work AS relation
            JOIN sec_parser_work_ledger AS ledger
              ON ledger.work_key = relation.work_key
            WHERE relation.run_id = ?
            """,
            (run_id,),
        )
    }
    current_metrics = frozenset(request.metric_name for request in registry.source_metrics)
    if requested_metric_sets and requested_metric_sets != {current_metrics}:
        raise ValueError(
            f"run_id={run_id} requested metrics do not match the current "
            "adapter registry; reassessment would change the run contract"
        )
    plan_metadata = metadata.get("plan")
    planned_tickers = plan_metadata.get("selected_tickers", []) if isinstance(plan_metadata, dict) else []
    selected_tickers = tickers or sorted(
        {str(value).strip().upper() for value in planned_tickers if str(value).strip()}
        | {
            str(row["ticker"])
            for row in conn.execute(
                """
                SELECT ticker
                FROM sec_parser_recovery_assessment
                WHERE run_id = ?
                UNION
                SELECT ticker
                FROM sec_parser_run_work
                WHERE run_id = ?
                """,
                (run_id, run_id),
            )
        }
        | {
            str(row.get("ticker") or "").strip().upper()
            for row in _missing_cache_details(metadata)
            if str(row.get("ticker") or "").strip()
        }
    )
    assessments = build_recovery_assessments(
        conn,
        run_id=run_id,
        registry=registry,
        asof_date=asof_date,
        tickers=selected_tickers,
        missing_cache_details=_missing_cache_details(metadata),
    )
    persist_recovery_assessments(
        conn,
        run_id=run_id,
        rows=assessments,
    )
    summary = assessment_summary(assessments)
    merge_run_metadata(
        conn,
        run_id=run_id,
        updates={
            "recovery_assessment": summary,
            "last_reassessed_at": utc_now(),
            "reassessed_with_adapter_version": registry.adapter_version,
        },
    )
    funnel = build_extraction_funnel(conn, run_id=run_id)
    return run, assessments, funnel


def existing_shadow_run_payload(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    assessments: list[dict[str, Any]],
    funnel: dict[str, Any],
) -> dict[str, Any]:
    run = load_run(conn, run_id=run_id)
    try:
        metadata = json.loads(str(run.get("metadata_json") or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"run_id={run_id} has invalid metadata_json") from exc
    if not isinstance(metadata, dict):
        raise ValueError(f"run_id={run_id} metadata_json is not an object")
    comparison_counts = {
        str(row["comparison_status"]): int(row["count"])
        for row in conn.execute(
            """
            SELECT comparison_status, COUNT(*) AS count
            FROM sec_parser_shadow_comparison
            WHERE run_id = ?
            GROUP BY comparison_status
            """,
            (run_id,),
        )
    }
    return {
        "mode": str(run["mode"]),
        "run_id": run_id,
        "summary": metadata.get("plan") or {},
        "completed_work_count": int(run["completed_work_count"]),
        "failed_work_count": int(run["failed_work_count"]),
        "comparison_status_counts": dict(sorted(comparison_counts.items())),
        "recovery_assessment": assessment_summary(assessments),
        "extraction_funnel": {key: value for key, value in funnel.items() if key != "detail_rows"},
    }


def _write_run_artifacts(
    *,
    output_json: Path | None,
    assessments: list[dict[str, Any]],
    funnel: dict[str, Any],
    comparison: list[dict[str, object]] | None = None,
    adjudication_rows: list[dict[str, Any]] | None = None,
) -> None:
    if output_json is None:
        return
    if comparison is not None:
        _write_json(
            output_json.with_name("dedicated_parser_shadow_comparison.json"),
            comparison,
        )
    assessment_path = output_json.with_name("dedicated_parser_recovery_assessment.json")
    _write_json(assessment_path, assessments)
    write_assessment_csv(
        assessment_path.with_suffix(".csv"),
        assessments,
    )
    write_assessment_csv(
        assessment_path.with_name("dedicated_parser_review_queue.csv"),
        [row for row in assessments if row["recovery_class"] in REVIEW_CLASSES],
    )
    funnel_path = output_json.with_name("dedicated_parser_extraction_funnel.json")
    _write_json(funnel_path, funnel)
    write_funnel_csv(
        funnel_path.with_suffix(".csv"),
        funnel["detail_rows"],
    )
    if adjudication_rows is not None:
        write_adjudication_skeleton(
            output_json.with_name("dedicated_parser_adjudication_skeleton.csv"),
            adjudication_rows,
        )


def _validate_args(args: argparse.Namespace) -> None:
    if args.workers < 1:
        raise ValueError("--workers must be at least 1")
    if args.write_batch_size < 1:
        raise ValueError("--write-batch-size must be at least 1")
    if args.reassess_run_id:
        incompatible = {
            "--plan-only": args.plan_only,
            "--force": args.force,
            "--all-metrics": args.all_metrics,
            "--no-resume": args.no_resume,
            # Ticker restriction on reassess would DELETE the whole run's
            # assessments and re-insert only the subset, and the ignored
            # cache-gate/accession flags would silently do nothing.
            "--tickers": bool(args.tickers),
            "--ticker-cohort": args.ticker_cohort is not None,
            "--accessions": bool(args.accessions),
            "--require-complete-cache": args.require_complete_cache,
        }
        invalid = [name for name, enabled in incompatible.items() if enabled]
        if invalid:
            raise ValueError("--reassess-run-id cannot be combined with " + ", ".join(invalid))
    elif not args.asof:
        raise ValueError("--asof is required unless --reassess-run-id is used")
    if not args.reassess_run_id and args.cache_dir is None:
        raise ValueError("--cache-dir is required unless --reassess-run-id is used")
    if args.tickers and args.ticker_cohort is not None:
        raise ValueError("--tickers and --ticker-cohort are mutually exclusive")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _validate_args(args)
    registry = load_registry(args.adapter)
    _export_policy_corpus(registry)
    tickers = load_cohort_tickers(args.ticker_cohort) if args.ticker_cohort is not None else _ticker_list(args.tickers)
    accessions = _accession_list(args.accessions)

    if args.reassess_run_id:
        with closing(connect_database(args.db)) as conn, conn:
            run, assessments, funnel = reassess_existing_run(
                conn,
                run_id=args.reassess_run_id,
                registry=registry,
                tickers=tickers,
                requested_asof=args.asof,
            )
            shadow_payload = existing_shadow_run_payload(
                conn,
                run_id=args.reassess_run_id,
                assessments=assessments,
                funnel=funnel,
            )
            adjudication_rows = (
                None
                if args.skip_adjudication_skeleton
                else build_adjudication_skeleton(
                    conn,
                    run_id=args.reassess_run_id,
                )
            )
        payload = {
            "mode": "assessment_only",
            "run_id": args.reassess_run_id,
            "asof_date": run["asof_date"],
            "source_adapter_version": run["adapter_version"],
            "assessment_adapter_version": registry.adapter_version,
            "recovery_assessment": assessment_summary(assessments),
            "extraction_funnel": {key: value for key, value in funnel.items() if key != "detail_rows"},
        }
        _write_json(args.output_json, payload)
        if args.output_json is not None:
            _write_json(
                args.output_json.with_name("dedicated_parser_shadow_run.json"),
                shadow_payload,
            )
        _write_run_artifacts(
            output_json=args.output_json,
            assessments=assessments,
            funnel=funnel,
            adjudication_rows=adjudication_rows,
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    with closing(connect_database(args.db)) as conn, conn:
        if args.require_complete_cache:
            cache_audit = audit_cache_completeness(
                conn,
                registry=registry,
                adapter_path=args.adapter,
                asof_date=args.asof,
                cache_dir=args.cache_dir,
                tickers=tickers or None,
                accessions=accessions or None,
                max_filings_per_ticker=args.max_filings_per_ticker,
                max_documents_per_filing=args.max_documents_per_filing,
                force=args.force,
                all_metrics=args.all_metrics,
            )
            if cache_audit.missing_cache_accessions:
                payload = {
                    "mode": "cache_gate_failed",
                    "summary": asdict(cache_audit),
                    "work_keys": [],
                }
                _write_json(
                    args.cache_gate_output_json or args.output_json,
                    payload,
                )
                print(json.dumps(payload, indent=2, sort_keys=True))
                return 2
        work, summary = build_plan(
            conn,
            registry=registry,
            adapter_path=args.adapter,
            asof_date=args.asof,
            cache_dir=args.cache_dir,
            tickers=tickers or None,
            accessions=accessions or None,
            max_filings_per_ticker=args.max_filings_per_ticker,
            max_documents_per_filing=args.max_documents_per_filing,
            resume=not args.no_resume,
            force=args.force,
            all_metrics=args.all_metrics,
            enable_arelle=not args.disable_arelle,
            enable_edgartools=not args.disable_edgartools,
            enable_pdf_ocr=args.enable_pdf_ocr,
            max_pdf_pages=args.max_pdf_pages,
            max_pdf_bytes=args.max_pdf_bytes,
            pdf_extraction_timeout_seconds=(args.pdf_extraction_timeout_seconds),
        )
        plan_payload = _plan_summary_payload(summary)
        plan_payload["execution_scope"] = {
            "max_filings_per_ticker": args.max_filings_per_ticker,
            "max_documents_per_filing": args.max_documents_per_filing,
            "all_metrics": bool(args.all_metrics),
            "force": bool(args.force),
            "resume": not bool(args.no_resume),
            "enable_arelle": not bool(args.disable_arelle),
            "enable_edgartools": not bool(args.disable_edgartools),
            "enable_pdf_ocr": bool(args.enable_pdf_ocr),
        }
        if args.plan_only:
            payload = {
                "mode": "plan_only",
                "summary": plan_payload,
                "work_keys": [item.work_key for item in work],
            }
            _write_json(args.output_json, payload)
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0

        if work:
            validate_provider_dependencies(
                enable_arelle=not args.disable_arelle,
                enable_edgartools=not args.disable_edgartools,
            )

        run_id = start_run(
            conn,
            model_family=registry.model_family,
            asof_date=args.asof,
            adapter_version=registry.adapter_version,
            mode="shadow",
            worker_count=args.workers,
            metadata={"plan": plan_payload},
        )
        completed = 0
        failed = 0
        comparison: list[dict[str, object]] = []
        assessments: list[dict[str, Any]] = []
        try:
            # Link resume-skipped completed work (and its evidence) into this
            # run so its recovery assessment sees the prior results instead of
            # regressing those pairs to UNCONFIRMED/MISSING.
            if summary.skipped_completed_work:
                link_completed_work(
                    conn,
                    run_id=run_id,
                    entries=summary.skipped_completed_work,
                )
                conn.commit()
            completed, failed = execute_plan(
                conn,
                run_id=run_id,
                work_items=work,
                worker_count=args.workers,
                provider_state_dir=args.provider_state_dir.resolve(),
                write_batch_size=args.write_batch_size,
            )
            comparison = compare_shadow_run(
                conn,
                run_id=run_id,
                model_family=registry.model_family,
                asof_date=args.asof,
                requested_metrics=tuple(request.metric_name for request in registry.source_metrics),
            )
            assessments = build_recovery_assessments(
                conn,
                run_id=run_id,
                registry=registry,
                asof_date=args.asof,
                tickers=tickers or list(summary.selected_tickers),
                missing_cache_details=summary.missing_cache_details,
            )
            persist_recovery_assessments(
                conn,
                run_id=run_id,
                rows=assessments,
            )
            finish_run(
                conn,
                run_id=run_id,
                status="COMPLETED" if failed == 0 else "FAILED",
                planned=len(work),
                completed=completed,
                failed=failed,
                metadata={
                    "plan": plan_payload,
                    "comparison_rows": len(comparison),
                    "recovery_assessment": assessment_summary(assessments),
                },
            )
        except BaseException:
            # Discard any uncommitted partial batch before the failure
            # bookkeeping commits — otherwise finish_run's commit persists
            # half-written evidence rows from the failed batch.
            conn.rollback()
            finish_run(
                conn,
                run_id=run_id,
                status="FAILED",
                planned=len(work),
                completed=completed,
                failed=max(1, failed),
                metadata={"plan": plan_payload},
            )
            raise
        funnel = build_extraction_funnel(conn, run_id=run_id)
        merge_run_metadata(
            conn,
            run_id=run_id,
            updates={"extraction_funnel": {key: value for key, value in funnel.items() if key != "detail_rows"}},
        )
        adjudication_rows = (
            None
            if args.skip_adjudication_skeleton
            else build_adjudication_skeleton(
                conn,
                run_id=run_id,
            )
        )
    payload = {
        "mode": "shadow",
        "run_id": run_id,
        "summary": plan_payload,
        "completed_work_count": completed,
        "failed_work_count": failed,
        "adjudication_skeleton_written": (adjudication_rows is not None),
        "comparison_status_counts": {
            status: sum(row["comparison_status"] == status for row in comparison)
            for status in sorted({str(row["comparison_status"]) for row in comparison})
        },
        "recovery_assessment": assessment_summary(assessments),
        "extraction_funnel": {key: value for key, value in funnel.items() if key != "detail_rows"},
    }
    _write_json(args.output_json, payload)
    _write_run_artifacts(
        output_json=args.output_json,
        assessments=assessments,
        funnel=funnel,
        comparison=comparison,
        adjudication_rows=adjudication_rows,
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

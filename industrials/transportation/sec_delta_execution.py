from __future__ import annotations

from typing import Any, Mapping


SEC_DELTA_EXECUTION_VERSION = "transportation_dp6f_sec_delta_execution_v1"


def validate_execution_preflight(
    *,
    source_manifest: Mapping[str, Any],
    source_csv_sha256: str,
    plan_gate: Mapping[str, Any],
    plan_payload: Mapping[str, Any],
    adapter_version: str,
    parser_metric_count: int,
) -> list[str]:
    errors: list[str] = []
    artifact = source_manifest.get("artifact")
    summary = plan_payload.get("summary")
    execution = (
        summary.get("execution_scope")
        if isinstance(summary, Mapping)
        else None
    )
    planned_source = (
        execution.get("source_manifest")
        if isinstance(execution, Mapping)
        else None
    )
    expected_accessions = int(
        source_manifest.get("selected_accession_count") or 0
    )
    expected_documents = int(
        source_manifest.get("selected_document_row_count") or 0
    )
    if source_manifest.get("acceptance") != "PASS":
        errors.append("delta source manifest is not PASS")
    if not isinstance(artifact, Mapping) or (
        str(artifact.get("sha256") or "") != source_csv_sha256
    ):
        errors.append("delta source CSV hash does not match its manifest")
    if int(source_manifest.get("parser_metric_count") or 0) != (
        parser_metric_count
    ):
        errors.append("delta source parser-metric count mismatch")
    if plan_gate.get("acceptance") != "PASS":
        errors.append("offline plan gate is not PASS")
    if plan_gate.get("mode") != "plan_only":
        errors.append("offline gate is not plan-only")
    if str(plan_gate.get("source_manifest_sha256") or "") != (
        source_csv_sha256
    ):
        errors.append("offline gate source hash mismatch")
    if str(plan_gate.get("adapter_version") or "") != adapter_version:
        errors.append("offline gate adapter version mismatch")
    if int(plan_gate.get("parser_metric_count") or 0) != (
        parser_metric_count
    ):
        errors.append("offline gate parser-metric count mismatch")
    if int(plan_gate.get("missing_cache_accessions") or 0) != 0:
        errors.append("offline gate has missing-cache accessions")
    if not bool(plan_gate.get("all_parser_metrics")):
        errors.append("offline gate does not enable all parser metrics")
    if not isinstance(summary, Mapping):
        errors.append("offline plan has no summary")
        return errors
    if plan_payload.get("mode") != "plan_only":
        errors.append("offline plan payload is not plan-only")
    if int(summary.get("scheduled_accessions") or 0) != (
        expected_accessions
    ):
        errors.append("offline plan accession count mismatch")
    if int(summary.get("scheduled_documents") or 0) != (
        expected_documents
    ):
        errors.append("offline plan document count mismatch")
    if int(summary.get("missing_cache_accessions") or 0) != 0:
        errors.append("offline plan has missing-cache accessions")
    if not isinstance(execution, Mapping):
        errors.append("offline plan has no execution scope")
        return errors
    if not bool(execution.get("all_metrics")):
        errors.append("offline plan does not request all metrics")
    if int(execution.get("max_filings_per_ticker", -1)) != 0:
        errors.append("offline plan filing limit is not unlimited")
    if int(execution.get("max_documents_per_filing", -1)) != 0:
        errors.append("offline plan document limit is not unlimited")
    if bool(execution.get("enable_pdf_ocr")):
        errors.append("offline plan unexpectedly enables PDF OCR")
    if not isinstance(planned_source, Mapping) or (
        str(planned_source.get("sha256") or "") != source_csv_sha256
    ):
        errors.append("offline plan source hash mismatch")
    return errors


def validate_execution_payload(
    *,
    payload: Mapping[str, Any],
    source_manifest: Mapping[str, Any],
    source_csv_sha256: str,
    parser_return_code: int,
) -> list[str]:
    errors: list[str] = []
    summary = payload.get("summary")
    if not isinstance(summary, Mapping):
        return ["execution payload has no summary"]
    execution = summary.get("execution_scope")
    if not isinstance(execution, Mapping):
        return ["execution payload has no execution scope"]
    source = execution.get("source_manifest")
    expected_accessions = int(
        source_manifest.get("selected_accession_count") or 0
    )
    expected_documents = int(
        source_manifest.get("selected_document_row_count") or 0
    )
    scheduled = int(summary.get("scheduled_accessions") or 0)
    skipped = int(summary.get("skipped_completed_accessions") or 0)
    linked = int(summary.get("linked_completed_work_count") or 0)
    newly_completed = int(payload.get("completed_work_count") or 0)
    failed = int(payload.get("failed_work_count") or 0)
    if parser_return_code != 0:
        errors.append(
            f"shared parser returned nonzero code={parser_return_code}"
        )
    if payload.get("mode") != "shadow":
        errors.append("execution payload is not a shadow run")
    if int(payload.get("run_id") or 0) <= 0:
        errors.append("execution payload has no valid run_id")
    if failed != 0:
        errors.append(f"execution has failed work count={failed}")
    if scheduled + skipped != expected_accessions:
        errors.append(
            "scheduled plus resume-linked accessions does not equal "
            "the sealed delta"
        )
    if linked != skipped:
        errors.append("linked completed work does not equal resume skips")
    if newly_completed + linked != expected_accessions:
        errors.append(
            "new plus linked completed work does not cover the sealed delta"
        )
    if skipped == 0 and int(
        summary.get("scheduled_documents") or 0
    ) != expected_documents:
        errors.append("executed document count does not equal sealed delta")
    if int(summary.get("missing_cache_accessions") or 0) != 0:
        errors.append("execution discovered missing-cache accessions")
    if not bool(execution.get("all_metrics")):
        errors.append("execution did not request all parser metrics")
    if bool(execution.get("enable_pdf_ocr")):
        errors.append("execution unexpectedly enabled PDF OCR")
    if not isinstance(source, Mapping) or (
        str(source.get("sha256") or "") != source_csv_sha256
    ):
        errors.append("execution source-manifest hash mismatch")
    if bool(payload.get("adjudication_skeleton_written")):
        errors.append("execution wrote an unrequested adjudication skeleton")
    return errors

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from industrials.core.config import (  # noqa: E402
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.reports import (  # noqa: E402
    write_csv_atomic,
    write_text_atomic,
)
from industrials.transportation.parser_coverage import (  # noqa: E402
    read_csv,
)
from industrials.transportation.non_sec_endpoints import (  # noqa: E402
    normalized_domain,
)
from industrials.transportation.primary_document_hydration import (  # noqa: E402
    CONTENT_CATALOG_FIELDS,
    DOCUMENT_RESULT_FIELDS,
    PRIMARY_DOCUMENT_HYDRATION_VERSION,
    REQUEST_RESULT_FIELDS,
    build_document_results,
    hydration_request_ids_sha256,
    hydrate_requests,
    summarize_document_results,
    validate_hydration_resume_progress,
)
from industrials.transportation.primary_document_review import (  # noqa: E402
    PRIMARY_DOCUMENT_REVIEW_VERSION,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)


HYDRATION_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36 "
    "TransportationPrimaryDocumentHydrator/1.0"
)
HYDRATION_REDIRECT_POLICY_VERSION = (
    "transportation_dp6r_redirect_policy_v1"
)
HYDRATION_HOST_RECOVERY_POLICY_VERSION = (
    "transportation_dp6r_host_recovery_policy_v1"
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Hydrate the exact DP6Q transportation primary-document request "
            "manifest once, validate and SHA-256 every body, store unique "
            "content in a content-addressed cache, and preserve document "
            "fanout. The command is restartable and never invokes the parser."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout-sec", type=float, default=45.0)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument(
        "--request-spacing-sec",
        type=float,
        default=0.35,
    )
    parser.add_argument(
        "--max-document-bytes",
        type=int,
        default=75_000_000,
    )
    parser.add_argument(
        "--request-id",
        action="append",
        default=[],
        help="Select an exact hydration request id for diagnostics.",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=0,
        help="Zero selects every sealed request.",
    )
    parser.add_argument(
        "--retry-failures",
        action="store_true",
        help="Retry previously sealed failed or quarantined requests.",
    )
    parser.add_argument(
        "--retryable-failures-only",
        action="store_true",
        help=(
            "Select only requests marked retryable in the canonical DP6R "
            "failure artifact. Requires --execute --retry-failures."
        ),
    )
    parser.add_argument(
        "--failure-http-status",
        action="append",
        type=int,
        default=[],
        help=(
            "Select only requests with one of these HTTP statuses in the "
            "canonical DP6R failure artifact. Requires --execute "
            "--retry-failures."
        ),
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=10,
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=0,
        help="Zero disables cooldown batching.",
    )
    parser.add_argument(
        "--batch-pause-sec",
        type=float,
        default=0.0,
        help="Pause between non-overlapping request batches.",
    )
    parser.add_argument(
        "--resume-progress",
        type=Path,
        default=None,
        help=(
            "Resume a stopped recovery selection after validating its "
            "completed cooldown-boundary progress checkpoint. The "
            "continuation must use a distinct --diagnostic-tag."
        ),
    )
    parser.add_argument("--cache-root", type=Path, default=None)
    parser.add_argument("--diagnostic-tag", default="")
    return parser.parse_args(argv)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def _validate_artifact(
    *,
    manifest: Mapping[str, Any],
    artifact_name: str,
    path: Path,
    rows: Sequence[Mapping[str, object]],
) -> list[str]:
    descriptor = (manifest.get("artifacts") or {}).get(
        artifact_name
    ) or {}
    errors: list[str] = []
    if Path(str(descriptor.get("path") or "")).resolve() != path.resolve():
        errors.append(f"{artifact_name}: sealed path mismatch")
    if int(descriptor.get("row_count") or -1) != len(rows):
        errors.append(f"{artifact_name}: sealed row count mismatch")
    if str(descriptor.get("sha256") or "") != file_sha256(path):
        errors.append(f"{artifact_name}: sealed SHA-256 mismatch")
    return errors


def _select_requests(
    rows: Sequence[dict[str, str]],
    *,
    request_ids: Sequence[str],
    max_requests: int,
) -> list[dict[str, str]]:
    selected = list(rows)
    if request_ids:
        requested = {str(value).strip() for value in request_ids}
        available = {
            str(row["hydration_request_id"]) for row in selected
        }
        missing = requested - available
        if missing:
            raise ValueError(
                f"Unknown hydration request ids={sorted(missing)}"
            )
        selected = [
            row
            for row in selected
            if row["hydration_request_id"] in requested
        ]
    if max_requests > 0:
        selected = selected[:max_requests]
    return selected


def _apply_redirect_policy(
    requests: Sequence[dict[str, str]],
    policy_rows: Sequence[Mapping[str, str]],
) -> list[str]:
    errors: list[str] = []
    routes: dict[tuple[str, str], dict[str, set[str]]] = {}
    used_routes: set[tuple[str, str]] = set()
    for row in policy_rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        source_domain = str(row.get("source_domain") or "").strip().lower()
        final_domain = str(row.get("final_domain") or "").strip().lower()
        disposition = str(
            row.get("review_disposition") or ""
        ).strip()
        key = (ticker, source_domain)
        if (
            row.get("policy_version")
            != HYDRATION_REDIRECT_POLICY_VERSION
            or row.get("review_status") != "APPROVED"
            or not all(key)
            or not final_domain
            or disposition
            not in {
                "APPROVE_CACHED_REDIRECT",
                "EXCLUDE_NON_FINANCIAL_REDIRECT",
            }
        ):
            errors.append(
                "invalid hydration redirect policy row="
                f"{ticker}/{source_domain}/{final_domain}"
            )
            continue
        bucket = routes.setdefault(
            key,
            {"approved": set(), "excluded": set()},
        )
        target = (
            "approved"
            if disposition == "APPROVE_CACHED_REDIRECT"
            else "excluded"
        )
        if final_domain in bucket["approved"] | bucket["excluded"]:
            errors.append(
                "duplicate hydration redirect policy route="
                f"{ticker}/{source_domain}/{final_domain}"
            )
            continue
        bucket[target].add(final_domain)

    for request in requests:
        tickers = {
            value
            for value in str(request.get("fanout_tickers") or "").split("|")
            if value
        }
        source_domains = {
            value
            for value in str(request.get("source_domains") or "").split("|")
            if value
        }
        approved: set[str] = set()
        excluded: set[str] = set()
        for ticker in tickers:
            for source_domain in source_domains:
                key = (ticker, source_domain)
                route = routes.get(key)
                if route is None:
                    continue
                used_routes.add(key)
                approved.update(route["approved"])
                excluded.update(route["excluded"])
        if approved & excluded:
            errors.append(
                "redirect domain has conflicting dispositions for request="
                f"{request.get('hydration_request_id')}"
            )
        request["approved_redirect_domains"] = "|".join(sorted(approved))
        request["excluded_redirect_domains"] = "|".join(sorted(excluded))

    unused_routes = set(routes) - used_routes
    if unused_routes:
        errors.append(
            "unused hydration redirect policy routes="
            f"{sorted(unused_routes)}"
        )
    return errors


def _apply_host_recovery_policy(
    requests: Sequence[dict[str, str]],
    policy_rows: Sequence[Mapping[str, str]],
) -> list[str]:
    errors: list[str] = []
    approved_domains: set[str] = set()
    for row in policy_rows:
        domain = str(row.get("domain") or "").strip().lower()
        if (
            row.get("policy_version")
            != HYDRATION_HOST_RECOVERY_POLICY_VERSION
            or row.get("review_status") != "APPROVED"
            or row.get("recovery_method")
            != "ORIGIN_REFERER_COOKIE_PREFLIGHT"
            or not domain
            or domain in approved_domains
        ):
            errors.append(
                f"invalid hydration host recovery policy domain={domain}"
            )
            continue
        approved_domains.add(domain)

    used_domains: set[str] = set()
    for request in requests:
        domain = normalized_domain(str(request.get("retrieval_url") or ""))
        enabled = domain in approved_domains
        request["preflight_head_for_cookie"] = "1" if enabled else "0"
        if enabled:
            used_domains.add(domain)
    unused_domains = approved_domains - used_domains
    if unused_domains:
        errors.append(
            "unused hydration host recovery domains="
            f"{sorted(unused_domains)}"
        )
    return errors


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.workers <= 0:
        raise ValueError("--workers must be positive")
    if args.max_retries <= 0:
        raise ValueError("--max-retries must be positive")
    if args.timeout_sec <= 0:
        raise ValueError("--timeout-sec must be positive")
    if args.max_document_bytes <= 0:
        raise ValueError("--max-document-bytes must be positive")
    if args.batch_size < 0 or args.batch_pause_sec < 0:
        raise ValueError("Batch size and pause must be non-negative")
    if args.batch_pause_sec > 0 and args.batch_size <= 0:
        raise ValueError("--batch-pause-sec requires --batch-size")
    if args.retryable_failures_only and (
        not args.execute or not args.retry_failures
    ):
        raise ValueError(
            "--retryable-failures-only requires --execute "
            "--retry-failures"
        )
    if args.failure_http_status and (
        not args.execute or not args.retry_failures
    ):
        raise ValueError(
            "--failure-http-status requires --execute --retry-failures"
        )
    if args.retryable_failures_only and args.failure_http_status:
        raise ValueError(
            "Choose either --retryable-failures-only or "
            "--failure-http-status"
        )
    if args.resume_progress is not None and (
        not args.execute
        or not args.retry_failures
        or not (
            args.retryable_failures_only
            or args.failure_http_status
        )
    ):
        raise ValueError(
            "--resume-progress requires an execute recovery selection"
        )
    if args.resume_progress is not None and (
        args.request_id or int(args.max_requests) != 0
    ):
        raise ValueError(
            "--resume-progress cannot be combined with --request-id "
            "or --max-requests"
        )
    if args.resume_progress is not None and not str(
        args.diagnostic_tag or ""
    ).strip():
        raise ValueError(
            "--resume-progress requires a distinct --diagnostic-tag"
        )

    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)[
        "dedicated_parser"
    ]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "Primary-document hydration requires parser execution disabled"
        )
    base_dir = config_path.parent
    asof_date = str(parser_cfg["source_census_asof_date"])
    output_dir = (
        resolve_path(parser_cfg["output_root"], base_dir=base_dir)
        / asof_date
    )
    review_manifest_path = (
        output_dir
        / "transportation_primary_document_review_manifest.json"
    )
    request_path = (
        output_dir
        / "transportation_primary_document_hydration_requests.csv"
    )
    reviewed_document_path = (
        output_dir
        / "transportation_primary_document_reviewed_manifest.csv"
    )
    redirect_policy_path = (
        PROJECT_ROOT
        / "industrials"
        / "transportation"
        / "review_policies"
        / "transportation_hydration_redirect_policy.csv"
    ).resolve()
    host_recovery_policy_path = (
        PROJECT_ROOT
        / "industrials"
        / "transportation"
        / "review_policies"
        / "transportation_hydration_host_recovery_policy.csv"
    ).resolve()
    for path in (
        review_manifest_path,
        request_path,
        reviewed_document_path,
        redirect_policy_path,
        host_recovery_policy_path,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)

    review_manifest = _read_json(review_manifest_path)
    errors: list[str] = []
    if review_manifest.get("acceptance") != "PASS":
        errors.append("DP6Q primary-document review is not PASS")
    if (
        review_manifest.get("review_version")
        != PRIMARY_DOCUMENT_REVIEW_VERSION
    ):
        errors.append("DP6Q review version is not supported")
    if review_manifest.get("asof_date") != asof_date:
        errors.append("DP6Q as-of date does not match config")
    if review_manifest.get("retrieval_manifest_frozen") is not True:
        errors.append("DP6Q retrieval manifest is not frozen")
    if (
        review_manifest.get("next_gate")
        != "HYDRATE_HASH_AND_CONTENT_DEDUPLICATE_PRIMARY_DOCUMENTS_ONCE"
    ):
        errors.append("DP6Q is not at the hydration gate")
    if review_manifest.get("parser_execution_authorized"):
        errors.append("DP6Q parser authorization must remain disabled")

    all_requests = read_csv(request_path)
    reviewed_documents = read_csv(reviewed_document_path)
    redirect_policy_rows = read_csv(redirect_policy_path)
    host_recovery_policy_rows = read_csv(host_recovery_policy_path)
    errors.extend(
        _apply_redirect_policy(all_requests, redirect_policy_rows)
    )
    errors.extend(
        _apply_host_recovery_policy(
            all_requests,
            host_recovery_policy_rows,
        )
    )
    errors.extend(
        _validate_artifact(
            manifest=review_manifest,
            artifact_name="hydration_requests",
            path=request_path,
            rows=all_requests,
        )
    )
    errors.extend(
        _validate_artifact(
            manifest=review_manifest,
            artifact_name="reviewed_document_manifest",
            path=reviewed_document_path,
            rows=reviewed_documents,
        )
    )
    if errors:
        raise ValueError("DP6R preflight failed: " + "; ".join(errors))

    recovery_filter_active = bool(
        args.retryable_failures_only or args.failure_http_status
    )
    selected_requests = _select_requests(
        all_requests,
        request_ids=args.request_id,
        max_requests=(
            0
            if recovery_filter_active
            else int(args.max_requests)
        ),
    )
    recovery_failure_path = (
        output_dir
        / "transportation_primary_document_hydration_failures.csv"
    )
    retryable_failure_ids: set[str] = set()
    recovery_selection_ids: set[str] = set()
    resume_checkpoint: dict[str, object] | None = None
    selection_start_offset = 0
    full_selection_count = len(selected_requests)
    full_selection_sha256 = hydration_request_ids_sha256(
        selected_requests
    )
    if recovery_filter_active:
        if not recovery_failure_path.is_file():
            raise FileNotFoundError(recovery_failure_path)
        recovery_failures = read_csv(recovery_failure_path)
        retryable_failure_ids = {
            str(row["hydration_request_id"])
            for row in recovery_failures
            if int(str(row["retryable"])) == 1
        }
        selected_http_statuses = {
            int(value) for value in args.failure_http_status
        }
        recovery_selection_ids = (
            retryable_failure_ids
            if args.retryable_failures_only
            else {
                str(row["hydration_request_id"])
                for row in recovery_failures
                if int(str(row["http_status"] or "0"))
                in selected_http_statuses
            }
        )
        selected_requests = [
            row
            for row in selected_requests
            if row["hydration_request_id"] in recovery_selection_ids
        ]
        if int(args.max_requests) > 0:
            selected_requests = selected_requests[: int(args.max_requests)]
        if not selected_requests:
            raise ValueError(
                "Canonical DP6R failure artifact has no requests matching "
                "the selected recovery filter"
            )
        full_selection_count = len(selected_requests)
        full_selection_sha256 = hydration_request_ids_sha256(
            selected_requests
        )
        if args.resume_progress is not None:
            resume_progress_path = (
                args.resume_progress.expanduser().resolve()
            )
            if not resume_progress_path.is_file():
                raise FileNotFoundError(resume_progress_path)
            resume_checkpoint = validate_hydration_resume_progress(
                _read_json(resume_progress_path),
                progress_path=resume_progress_path,
                request_manifest_path=request_path,
                request_manifest_sha256=file_sha256(request_path),
                full_selection=selected_requests,
                expected_batch_size=int(args.batch_size),
            )
            selection_start_offset = int(
                str(resume_checkpoint["next_selection_offset"])
            )
            selected_requests = selected_requests[
                selection_start_offset:
            ]
            if not selected_requests:
                raise ValueError(
                    "Hydration resume checkpoint has no remaining requests"
                )
    complete_selection = len(selected_requests) == len(all_requests)
    canonical_run = bool(
        args.execute
        and complete_selection
        and not args.request_id
        and int(args.max_requests) == 0
        and not recovery_filter_active
    )
    diagnostic_tag = str(args.diagnostic_tag or "").strip()
    run_output_dir = (
        output_dir
        if canonical_run
        else (
            output_dir
            / "dp6r_diagnostics"
            / (
                diagnostic_tag
                or (
                    "dry_run"
                    if not args.execute
                    else f"selected_{len(selected_requests)}"
                )
            )
        )
    )
    run_output_dir.mkdir(parents=True, exist_ok=True)
    cache_root = (
        args.cache_root.expanduser().resolve()
        if args.cache_root
        else (
            PROJECT_ROOT
            / "output"
            / "industrials_cache"
            / "transportation"
            / "non_sec_primary_documents"
        ).resolve()
    )
    progress_path = (
        run_output_dir
        / "transportation_primary_document_hydration_progress.json"
    )
    if (
        args.resume_progress is not None
        and progress_path.resolve()
        == args.resume_progress.expanduser().resolve()
    ):
        raise ValueError(
            "Hydration continuation output must not overwrite its "
            "parent progress checkpoint"
        )
    request_result_path = (
        run_output_dir
        / "transportation_primary_document_hydration_request_results.csv"
    )
    document_result_path = (
        run_output_dir
        / "transportation_primary_document_hydrated_manifest.csv"
    )
    content_catalog_path = (
        run_output_dir
        / "transportation_primary_document_content_catalog.csv"
    )
    failure_path = (
        run_output_dir
        / "transportation_primary_document_hydration_failures.csv"
    )
    manifest_path = (
        run_output_dir
        / "transportation_primary_document_hydration_manifest.json"
    )

    source_manifest_paths = [
        review_manifest_path,
        request_path,
        reviewed_document_path,
        redirect_policy_path,
        host_recovery_policy_path,
    ]
    if args.resume_progress is not None:
        source_manifest_paths.append(
            args.resume_progress.expanduser().resolve()
        )

    request_results, request_summary = hydrate_requests(
        selected_requests,
        execute=bool(args.execute),
        cache_root=cache_root,
        request_manifest_path=request_path,
        source_manifest_paths=tuple(source_manifest_paths),
        progress_path=progress_path,
        user_agent=HYDRATION_USER_AGENT,
        timeout_sec=float(args.timeout_sec),
        max_retries=int(args.max_retries),
        request_spacing_sec=float(args.request_spacing_sec),
        max_bytes=int(args.max_document_bytes),
        workers=int(args.workers),
        retry_failures=bool(args.retry_failures),
        progress_every=int(args.progress_every),
        batch_size=int(args.batch_size),
        batch_pause_sec=float(args.batch_pause_sec),
        selection_start_offset=selection_start_offset,
        selection_total_count=full_selection_count,
        selection_request_id_sha256=full_selection_sha256,
    )
    document_results, content_catalog, document_errors = (
        build_document_results(
            reviewed_documents=reviewed_documents,
            request_results=request_results,
            cache_root=cache_root,
            require_complete_requests=complete_selection,
        )
    )
    errors.extend(document_errors)
    document_summary = summarize_document_results(
        document_rows=document_results,
        content_rows=content_catalog,
    )
    failures = [
        row
        for row in request_results
        if int(str(row["content_ready"])) == 0
        and str(row["status"]) != "PLANNED_NOT_EXECUTED"
        and not str(row["status"]).startswith("EXCLUDED_AFTER_DP6R")
    ]

    write_csv_atomic(
        request_result_path,
        REQUEST_RESULT_FIELDS,
        request_results,
    )
    write_csv_atomic(
        document_result_path,
        DOCUMENT_RESULT_FIELDS,
        document_results,
    )
    write_csv_atomic(
        content_catalog_path,
        CONTENT_CATALOG_FIELDS,
        content_catalog,
    )
    write_csv_atomic(
        failure_path,
        REQUEST_RESULT_FIELDS,
        failures,
    )

    if not args.execute:
        acceptance = "DRY_RUN"
    elif not canonical_run:
        acceptance = "DIAGNOSTIC_COMPLETE"
    elif errors or not request_summary["source_artifacts_unchanged"]:
        acceptance = "FAIL"
    elif (
        int(str(request_summary["content_ready_request_count"]))
        + int(str(request_summary["terminal_excluded_request_count"]))
        == len(all_requests)
        and int(str(document_summary["source_gap_document_count"])) == 0
    ):
        acceptance = "PASS"
    else:
        acceptance = "PASS_WITH_REQUIRED_RECOVERY"

    next_gate = {
        "PASS": "BUILD_AND_VALIDATE_ONE_PASS_PRIMARY_DOCUMENT_PARSE_PLAN",
        "PASS_WITH_REQUIRED_RECOVERY": (
            "REPAIR_FAILED_AND_QUARANTINED_PRIMARY_DOCUMENTS"
        ),
        "FAIL": "REPAIR_PRIMARY_DOCUMENT_HYDRATION_SEAL",
        "DRY_RUN": "EXECUTE_FULL_PRIMARY_DOCUMENT_HYDRATION",
        "DIAGNOSTIC_COMPLETE": (
            "EXECUTE_OR_RESUME_FULL_PRIMARY_DOCUMENT_HYDRATION"
        ),
    }[acceptance]
    payload = {
        "acceptance": acceptance,
        "gate": (
            "DP6R_PRIMARY_DOCUMENT_HYDRATION_AND_CONTENT_DEDUPLICATION"
        ),
        "hydration_version": PRIMARY_DOCUMENT_HYDRATION_VERSION,
        "model_family": MODEL_FAMILY,
        "asof_date": asof_date,
        "canonical_run": canonical_run,
        "complete_selection": complete_selection,
        "available_request_count": len(all_requests),
        "selected_request_count": len(selected_requests),
        "resume_checkpoint": resume_checkpoint,
        "retryable_failures_only": bool(args.retryable_failures_only),
        "retryable_failure_selection_count": len(retryable_failure_ids),
        "recovery_filter_selection_count": len(recovery_selection_ids),
        "failure_http_status_filter": sorted(
            {int(value) for value in args.failure_http_status}
        ),
        "recovery_failure_manifest": (
            {
                "path": str(recovery_failure_path.resolve()),
                "sha256": file_sha256(recovery_failure_path),
            }
            if recovery_filter_active
            else None
        ),
        **request_summary,
        **document_summary,
        "failure_artifact_row_count": len(failures),
        "cache_root": str(cache_root),
        "point_in_time_cutoff_inherited": True,
        "primary_source_hierarchy_inherited": True,
        "retrieval_scope_authorized_by_dp6q": True,
        "further_retrieval_authorized": False,
        "parser_execution_authorized": False,
        "errors": errors,
        "support_handoff": {
            "owning_workflow": "standalone_support_request",
            "decision_impact": (
                "Hydrated primary-source bytes and explicit retrieval gaps "
                "determine which documents can enter the one-pass parse."
            ),
            "readiness_effect": (
                "research_grade"
                if acceptance == "PASS"
                else "needs_targeted_fixes"
            ),
            "artifact_role": "standalone_support_artifact",
            "hidden_unless_requested": True,
        },
        "inputs": {
            "primary_document_review_manifest": {
                "path": str(review_manifest_path.resolve()),
                "sha256": file_sha256(review_manifest_path),
            },
            "hydration_requests": {
                "path": str(request_path.resolve()),
                "row_count": len(all_requests),
                "sha256": file_sha256(request_path),
            },
            "reviewed_document_manifest": {
                "path": str(reviewed_document_path.resolve()),
                "row_count": len(reviewed_documents),
                "sha256": file_sha256(reviewed_document_path),
            },
            "hydration_redirect_policy": {
                "path": str(redirect_policy_path.resolve()),
                "row_count": len(redirect_policy_rows),
                "sha256": file_sha256(redirect_policy_path),
                "policy_version": HYDRATION_REDIRECT_POLICY_VERSION,
            },
            "hydration_host_recovery_policy": {
                "path": str(host_recovery_policy_path.resolve()),
                "row_count": len(host_recovery_policy_rows),
                "sha256": file_sha256(host_recovery_policy_path),
                "policy_version": (
                    HYDRATION_HOST_RECOVERY_POLICY_VERSION
                ),
            },
        },
        "artifacts": {
            "request_results": {
                "path": str(request_result_path.resolve()),
                "row_count": len(request_results),
                "sha256": file_sha256(request_result_path),
            },
            "hydrated_document_manifest": {
                "path": str(document_result_path.resolve()),
                "row_count": len(document_results),
                "sha256": file_sha256(document_result_path),
            },
            "content_catalog": {
                "path": str(content_catalog_path.resolve()),
                "row_count": len(content_catalog),
                "sha256": file_sha256(content_catalog_path),
            },
            "failures": {
                "path": str(failure_path.resolve()),
                "row_count": len(failures),
                "sha256": file_sha256(failure_path),
            },
            "progress": {
                "path": str(progress_path.resolve()),
                "sha256": file_sha256(progress_path),
            },
        },
        "next_gate": next_gate,
    }
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 2 if acceptance == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from typing import Mapping, Sequence


PRIMARY_DOCUMENT_REVIEW_VERSION = (
    "transportation_dp6q_primary_document_review_v1"
)

ENDPOINT_REVIEW_FIELDS = (
    "review_version",
    "endpoint_id",
    "ticker",
    "endpoint_type",
    "universe_role",
    "enumeration_status",
    "required_pair_count",
    "discovery_page_count",
    "ready_discovery_page_count",
    "failed_discovery_page_count",
    "candidate_document_count",
    "external_asset_document_count",
    "archive_digest_document_count",
    "failed_page_roles",
    "failure_class_counts",
    "root_repair_review_status",
    "root_repair_unresolved_disposition",
    "root_repair_fallback_source_lane",
    "exception_review_required",
    "review_status",
    "endpoint_disposition",
    "source_gap_status",
    "source_posture",
    "evidence_label",
    "review_note",
    "include_candidate_documents",
    "retrieval_authorized",
    "parser_execution_authorized",
)

EXTERNAL_DOMAIN_ADJUDICATION_FIELDS = (
    "review_version",
    "ticker",
    "endpoint_id",
    "source_domain",
    "source_url",
    "discovered_from_url",
    "title",
    "document_type",
    "enumeration_domain_status",
    "policy_review_status",
    "review_disposition",
    "source_authority_class",
    "evidence_label",
    "decision_basis",
    "notes",
    "include_in_hydration",
    "retrieval_authorized",
    "parser_execution_authorized",
)

REVIEWED_DOCUMENT_FIELDS = (
    "review_version",
    "document_id",
    "ticker",
    "endpoint_id",
    "endpoint_type",
    "universe_role",
    "source_id",
    "source_type",
    "source_rank",
    "freshness_status",
    "document_type",
    "title",
    "published_date_hint",
    "canonical_url",
    "retrieval_url",
    "source_domain",
    "enumeration_domain_status",
    "source_authority_class",
    "external_domain_review_disposition",
    "document_review_disposition",
    "review_evidence_label",
    "candidate_source_lane_ids",
    "applicable_parser_metric_count",
    "applicable_parser_metric_ids",
    "applicable_supporting_metric_count",
    "applicable_supporting_metric_ids",
    "parse_all_applicable_metrics",
    "url_identity_sha256",
    "source_content_digest",
    "source_content_digest_algorithm",
    "content_type",
    "content_bytes",
    "content_cache_path",
    "content_sha256",
    "enumeration_content_hash_status",
    "include_in_hydration",
    "cache_reuse_available",
    "hydration_required",
    "hydration_request_id",
    "hydration_disposition",
    "retrieval_authorized",
    "parser_execution_authorized",
)

HYDRATION_REQUEST_FIELDS = (
    "review_version",
    "hydration_request_id",
    "retrieval_identity_type",
    "retrieval_identity_sha256",
    "retrieval_url",
    "canonical_url",
    "source_content_digest",
    "source_content_digest_algorithm",
    "fanout_document_count",
    "fanout_ticker_count",
    "fanout_tickers",
    "source_domains",
    "document_types",
    "applicable_parser_metric_count",
    "applicable_parser_metric_ids",
    "applicable_supporting_metric_count",
    "applicable_supporting_metric_ids",
    "parse_all_applicable_metrics",
    "hydration_status",
    "retrieval_authorized",
    "parser_execution_authorized",
)

EXTERNAL_DOMAIN_POLICY_FIELDS = (
    "policy_version",
    "ticker",
    "source_domain",
    "review_status",
    "review_disposition",
    "source_authority_class",
    "evidence_label",
    "decision_basis",
    "notes",
)

APPROVED_EXTERNAL_DISPOSITION = "APPROVE_FOR_ONE_TIME_HYDRATION"
EXCLUDED_EXTERNAL_DISPOSITION = "EXCLUDE_FROM_HYDRATION"
ALLOWED_EXTERNAL_DISPOSITIONS = frozenset(
    {
        APPROVED_EXTERNAL_DISPOSITION,
        EXCLUDED_EXTERNAL_DISPOSITION,
    }
)


def _as_int(value: object) -> int:
    text = str(value or "").strip()
    return int(text) if text else 0


def _failure_class(row: Mapping[str, object]) -> str:
    status = _as_int(row.get("http_status"))
    error = str(row.get("error") or "")
    lowered = error.lower()
    if "exceeds max_discovery_bytes" in lowered:
        return "DISCOVERY_SIZE_LIMIT"
    if "readtimeout" in lowered or "read timed out" in lowered:
        return "READ_TIMEOUT"
    if "sslcertverificationerror" in lowered:
        return "TLS_VALIDATION_FAILURE"
    if "wrong_version_number" in lowered:
        return "TLS_PROTOCOL_FAILURE"
    if "connectionreseterror" in lowered:
        return "REMOTE_CONNECTION_RESET"
    if "escaped sealed issuer domain" in lowered:
        return "DOMAIN_ESCAPE_BLOCKED"
    if status >= 400:
        return f"HTTP_{status}"
    if error:
        return error.split(":", 1)[0].strip().upper() or "UNKNOWN"
    if status:
        return f"HTTP_{status}_UNREADABLE"
    return "UNKNOWN"


def _failure_summaries(
    rows: Sequence[Mapping[str, object]],
) -> tuple[str, str]:
    roles = sorted(
        {
            str(row.get("page_role") or "")
            for row in rows
            if str(row.get("page_role") or "")
        }
    )
    counts = Counter(_failure_class(row) for row in rows)
    return (
        "|".join(roles),
        "|".join(
            f"{failure_class}:{count}"
            for failure_class, count in sorted(counts.items())
        ),
    )


def build_endpoint_review_rows(
    *,
    endpoint_rows: Sequence[Mapping[str, object]],
    discovery_rows: Sequence[Mapping[str, object]],
) -> tuple[list[dict[str, object]], list[str]]:
    failures_by_endpoint: dict[
        str, list[Mapping[str, object]]
    ] = defaultdict(list)
    for row in discovery_rows:
        if str(row.get("page_status") or "") == "FAILED":
            failures_by_endpoint[str(row["endpoint_id"])].append(row)

    output: list[dict[str, object]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for raw in sorted(
        endpoint_rows,
        key=lambda row: (
            str(row.get("ticker") or ""),
            str(row.get("endpoint_id") or ""),
        ),
    ):
        endpoint_id = str(raw.get("endpoint_id") or "")
        ticker = str(raw.get("ticker") or "").upper()
        if not endpoint_id or endpoint_id in seen:
            errors.append(
                f"{ticker or '<unknown>'}: invalid or duplicate endpoint id"
            )
            continue
        seen.add(endpoint_id)
        status = str(raw.get("endpoint_status") or "")
        candidates = _as_int(raw.get("candidate_document_count"))
        failed = _as_int(raw.get("failed_discovery_page_count"))
        repair_status = str(
            raw.get("root_repair_review_status") or ""
        )
        unresolved = str(
            raw.get("root_repair_unresolved_disposition") or ""
        )
        fallback = str(
            raw.get("root_repair_fallback_source_lane") or ""
        )
        exception_required = int(
            status != "ENUMERATED_AND_URL_DEDUPLICATED"
        )
        include_documents = int(candidates > 0)
        source_posture = "research_grade"
        evidence_label = "analyst_interpretation"

        if status == "ENUMERATED_AND_URL_DEDUPLICATED":
            disposition = "ACCEPT_ENUMERATED_SET"
            gap_status = "PRIMARY_DOCUMENT_CANDIDATES_AVAILABLE"
            note = (
                "Bounded issuer-root discovery completed without a "
                "recorded page failure."
            )
            if candidates <= 0 or failed:
                errors.append(
                    f"{ticker}: clean enumeration status is inconsistent"
                )
        elif status == "ENUMERATED_WITH_PARTIAL_DISCOVERY_FAILURES":
            disposition = (
                "ACCEPT_ENUMERATED_SET_WITH_BOUNDED_DISCOVERY_GAPS"
            )
            gap_status = "PARTIAL_DISCOVERY_RISK_RETAINED"
            note = (
                "Available primary candidates are retained; failed optional "
                "navigation or sitemap branches remain explicit and do not "
                "authorize a second broad discovery pass."
            )
            if candidates <= 0 or failed <= 0:
                errors.append(
                    f"{ticker}: partial enumeration status is inconsistent"
                )
        elif status == "NO_PRIMARY_DOCUMENT_CANDIDATES_REVIEW_REQUIRED":
            disposition = (
                "RETAIN_ZERO_RESULT_WITH_DECLARED_FALLBACK_LANE"
                if fallback
                else "RETAIN_ZERO_RESULT_AFTER_BOUNDED_DISCOVERY"
            )
            gap_status = "PRIMARY_DOCUMENT_CANDIDATE_GAP_RETAINED"
            source_posture = "needs_targeted_fixes"
            evidence_label = "missing_required_source"
            note = (
                "No candidate survived bounded primary discovery. The gap "
                "is retained as missing evidence; no value may be inferred."
            )
            if candidates:
                errors.append(
                    f"{ticker}: zero-document status has candidates"
                )
        elif status.startswith("REVIEWED_"):
            source_posture = "needs_targeted_fixes"
            if candidates:
                disposition = (
                    "ACCEPT_ENUMERATED_SET_WITH_REVIEWED_ACCESS_LIMITATION"
                )
                gap_status = "PRIMARY_SOURCE_ACCESS_LIMITATION_RETAINED"
                note = (
                    "Available candidates are retained while the reviewed "
                    "root/index access limitation and fallback lane remain "
                    "part of the source record."
                )
            else:
                disposition = (
                    "RETAIN_ZERO_RESULT_AND_REQUIRE_DECLARED_FALLBACK_LANE"
                )
                gap_status = "FALLBACK_PRIMARY_SOURCE_LANE_REQUIRED"
                evidence_label = "missing_required_source"
                note = (
                    "The primary root was access-limited and produced no "
                    "candidate; only the declared fallback lane may close "
                    "the evidence gap."
                )
            if (
                repair_status != "APPROVED"
                or not unresolved.startswith("REVIEWED_")
                or not fallback
            ):
                errors.append(
                    f"{ticker}: access limitation lacks approved fallback"
                )
        else:
            disposition = "BLOCK_UNREVIEWED_ENDPOINT_STATUS"
            gap_status = "UNREVIEWED_ENDPOINT_FAILURE"
            source_posture = "blocked"
            evidence_label = "unknown"
            note = "The endpoint status is outside the sealed review contract."
            errors.append(f"{ticker}: unsupported endpoint status={status}")

        failure_roles, failure_counts = _failure_summaries(
            failures_by_endpoint.get(endpoint_id, [])
        )
        if len(failures_by_endpoint.get(endpoint_id, [])) != failed:
            errors.append(
                f"{ticker}: failed discovery-page count does not reconcile"
            )
        output.append(
            {
                "review_version": PRIMARY_DOCUMENT_REVIEW_VERSION,
                "endpoint_id": endpoint_id,
                "ticker": ticker,
                "endpoint_type": raw.get("endpoint_type", ""),
                "universe_role": raw.get("universe_role", ""),
                "enumeration_status": status,
                "required_pair_count": _as_int(
                    raw.get("required_pair_count")
                ),
                "discovery_page_count": _as_int(
                    raw.get("discovery_page_count")
                ),
                "ready_discovery_page_count": _as_int(
                    raw.get("ready_discovery_page_count")
                ),
                "failed_discovery_page_count": failed,
                "candidate_document_count": candidates,
                "external_asset_document_count": _as_int(
                    raw.get("external_asset_document_count")
                ),
                "archive_digest_document_count": _as_int(
                    raw.get("archive_digest_document_count")
                ),
                "failed_page_roles": failure_roles,
                "failure_class_counts": failure_counts,
                "root_repair_review_status": repair_status,
                "root_repair_unresolved_disposition": unresolved,
                "root_repair_fallback_source_lane": fallback,
                "exception_review_required": exception_required,
                "review_status": "REVIEWED",
                "endpoint_disposition": disposition,
                "source_gap_status": gap_status,
                "source_posture": source_posture,
                "evidence_label": evidence_label,
                "review_note": note,
                "include_candidate_documents": include_documents,
                "retrieval_authorized": 0,
                "parser_execution_authorized": 0,
            }
        )
    return output, errors


def index_external_domain_policy(
    policy_rows: Sequence[Mapping[str, str]],
) -> tuple[dict[tuple[str, str], dict[str, str]], list[str]]:
    output: dict[tuple[str, str], dict[str, str]] = {}
    errors: list[str] = []
    for raw in policy_rows:
        row = {field: str(raw.get(field) or "").strip() for field in raw}
        ticker = row.get("ticker", "").upper()
        domain = row.get("source_domain", "").lower()
        key = (ticker, domain)
        if not ticker or not domain or key in output:
            errors.append(
                f"invalid or duplicate external-domain policy key={key}"
            )
            continue
        if row.get("review_status") != "APPROVED":
            errors.append(f"{ticker}/{domain}: policy is not APPROVED")
        if row.get("review_disposition") not in (
            ALLOWED_EXTERNAL_DISPOSITIONS
        ):
            errors.append(
                f"{ticker}/{domain}: invalid review disposition"
            )
        if row.get("evidence_label") != "analyst_interpretation":
            errors.append(
                f"{ticker}/{domain}: review must be analyst_interpretation"
            )
        output[key] = row
    return output, errors


def build_external_domain_adjudications(
    *,
    external_rows: Sequence[Mapping[str, object]],
    policy_rows: Sequence[Mapping[str, str]],
) -> tuple[
    list[dict[str, object]],
    dict[tuple[str, str], dict[str, str]],
    list[str],
]:
    policy, errors = index_external_domain_policy(policy_rows)
    observed_keys = {
        (
            str(row.get("ticker") or "").upper(),
            str(row.get("source_domain") or "").lower(),
        )
        for row in external_rows
    }
    missing = observed_keys - set(policy)
    unknown = set(policy) - observed_keys
    if missing:
        errors.append(
            f"missing external-domain policies={sorted(missing)}"
        )
    if unknown:
        errors.append(
            f"unused external-domain policies={sorted(unknown)}"
        )

    output: list[dict[str, object]] = []
    for raw in sorted(
        external_rows,
        key=lambda row: (
            str(row.get("ticker") or ""),
            str(row.get("source_domain") or ""),
            str(row.get("source_url") or ""),
        ),
    ):
        key = (
            str(raw.get("ticker") or "").upper(),
            str(raw.get("source_domain") or "").lower(),
        )
        decision = policy.get(key, {})
        disposition = decision.get(
            "review_disposition",
            "BLOCK_MISSING_DOMAIN_POLICY",
        )
        include = int(disposition == APPROVED_EXTERNAL_DISPOSITION)
        output.append(
            {
                "review_version": PRIMARY_DOCUMENT_REVIEW_VERSION,
                "ticker": key[0],
                "endpoint_id": raw.get("endpoint_id", ""),
                "source_domain": key[1],
                "source_url": raw.get("source_url", ""),
                "discovered_from_url": raw.get(
                    "discovered_from_url", ""
                ),
                "title": raw.get("title", ""),
                "document_type": raw.get("document_type", ""),
                "enumeration_domain_status": raw.get(
                    "domain_status", ""
                ),
                "policy_review_status": decision.get(
                    "review_status", "MISSING"
                ),
                "review_disposition": disposition,
                "source_authority_class": decision.get(
                    "source_authority_class", "UNKNOWN"
                ),
                "evidence_label": decision.get(
                    "evidence_label", "unknown"
                ),
                "decision_basis": decision.get("decision_basis", ""),
                "notes": decision.get("notes", ""),
                "include_in_hydration": include,
                "retrieval_authorized": 0,
                "parser_execution_authorized": 0,
            }
        )
    return output, policy, errors


def _hydration_identity(
    row: Mapping[str, object],
) -> tuple[str, str, str]:
    source_digest = str(row.get("source_content_digest") or "")
    source_algorithm = str(
        row.get("source_content_digest_algorithm") or ""
    )
    if source_digest:
        identity_type = "SOURCE_CONTENT_DIGEST"
        identity_value = f"{source_algorithm}|{source_digest}"
    else:
        identity_type = "CANONICAL_URL"
        identity_value = str(row.get("canonical_url") or "")
    identity_hash = hashlib.sha256(
        f"{identity_type}|{identity_value}".encode("utf-8")
    ).hexdigest()
    return identity_type, identity_value, identity_hash


def build_reviewed_document_and_hydration_rows(
    *,
    document_rows: Sequence[Mapping[str, object]],
    external_policy: Mapping[tuple[str, str], Mapping[str, str]],
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[str],
]:
    reviewed: list[dict[str, object]] = []
    errors: list[str] = []
    seen_document_ids: set[str] = set()
    request_identity: dict[str, tuple[str, str, str]] = {}

    for raw in sorted(
        document_rows,
        key=lambda row: str(row.get("document_id") or ""),
    ):
        document_id = str(raw.get("document_id") or "")
        ticker = str(raw.get("ticker") or "").upper()
        domain = str(raw.get("source_domain") or "").lower()
        domain_status = str(raw.get("domain_status") or "")
        if not document_id or document_id in seen_document_ids:
            errors.append(
                f"{ticker}: invalid or duplicate document id={document_id}"
            )
            continue
        seen_document_ids.add(document_id)

        external_disposition = "NOT_REQUIRED"
        evidence_label = "fact_source_reported"
        include = True
        if domain_status == "ISSUER_CONTROLLED_DOMAIN":
            authority = "ISSUER_CONTROLLED_DOMAIN"
            document_disposition = (
                "APPROVED_PRECLASSIFIED_ISSUER_CONTROLLED"
            )
        elif domain_status == "ISSUER_LINKED_KNOWN_ASSET_DOMAIN":
            authority = "PRECLASSIFIED_ISSUER_ASSET_HOST"
            document_disposition = (
                "APPROVED_PRECLASSIFIED_KNOWN_ASSET_HOST"
            )
        elif domain_status.endswith("REVIEW_REQUIRED"):
            decision = external_policy.get((ticker, domain))
            if decision is None:
                errors.append(
                    f"{ticker}/{domain}: document lacks domain policy"
                )
                authority = "UNKNOWN"
                external_disposition = "BLOCK_MISSING_DOMAIN_POLICY"
                document_disposition = "BLOCKED_MISSING_DOMAIN_POLICY"
                evidence_label = "unknown"
                include = False
            else:
                authority = str(
                    decision.get("source_authority_class") or "UNKNOWN"
                )
                external_disposition = str(
                    decision.get("review_disposition") or ""
                )
                evidence_label = str(
                    decision.get("evidence_label") or "unknown"
                )
                include = (
                    external_disposition
                    == APPROVED_EXTERNAL_DISPOSITION
                )
                document_disposition = (
                    "APPROVED_REVIEWED_EXTERNAL_ASSET"
                    if include
                    else "EXCLUDED_REVIEWED_EXTERNAL_ASSET"
                )
        else:
            authority = "UNKNOWN"
            document_disposition = "BLOCKED_UNKNOWN_DOMAIN_STATUS"
            evidence_label = "unknown"
            include = False
            errors.append(
                f"{ticker}/{document_id}: unknown domain status"
            )

        content_sha256 = str(raw.get("content_sha256") or "")
        cache_reuse = int(include and bool(content_sha256))
        hydration_required = int(include and not content_sha256)
        request_id = ""
        if not include:
            hydration_disposition = "EXCLUDE_AFTER_SOURCE_REVIEW"
        elif cache_reuse:
            hydration_disposition = "REUSE_DISCOVERY_CACHE_BODY"
        else:
            (
                identity_type,
                identity_value,
                identity_hash,
            ) = _hydration_identity(raw)
            if not identity_value:
                errors.append(
                    f"{ticker}/{document_id}: missing hydration identity"
                )
            request_id = f"trnhyd_{identity_hash[:24]}"
            existing_identity = request_identity.setdefault(
                request_id,
                (identity_type, identity_value, identity_hash),
            )
            if existing_identity != (
                identity_type,
                identity_value,
                identity_hash,
            ):
                errors.append(
                    f"{request_id}: truncated hydration-id collision"
                )
            hydration_disposition = "HYDRATE_ONCE_AND_SHA256"

        reviewed.append(
            {
                "review_version": PRIMARY_DOCUMENT_REVIEW_VERSION,
                "document_id": document_id,
                "ticker": ticker,
                "endpoint_id": raw.get("endpoint_id", ""),
                "endpoint_type": raw.get("endpoint_type", ""),
                "universe_role": raw.get("universe_role", ""),
                "source_id": raw.get("source_id", ""),
                "source_type": raw.get("source_type", ""),
                "source_rank": raw.get("source_rank", ""),
                "freshness_status": raw.get("freshness_status", ""),
                "document_type": raw.get("document_type", ""),
                "title": raw.get("title", ""),
                "published_date_hint": raw.get(
                    "published_date_hint", ""
                ),
                "canonical_url": raw.get("canonical_url", ""),
                "retrieval_url": raw.get("retrieval_url", ""),
                "source_domain": domain,
                "enumeration_domain_status": domain_status,
                "source_authority_class": authority,
                "external_domain_review_disposition": (
                    external_disposition
                ),
                "document_review_disposition": document_disposition,
                "review_evidence_label": evidence_label,
                "candidate_source_lane_ids": raw.get(
                    "candidate_source_lane_ids", ""
                ),
                "applicable_parser_metric_count": raw.get(
                    "applicable_parser_metric_count", ""
                ),
                "applicable_parser_metric_ids": raw.get(
                    "applicable_parser_metric_ids", ""
                ),
                "applicable_supporting_metric_count": raw.get(
                    "applicable_supporting_metric_count", ""
                ),
                "applicable_supporting_metric_ids": raw.get(
                    "applicable_supporting_metric_ids", ""
                ),
                "parse_all_applicable_metrics": raw.get(
                    "parse_all_applicable_metrics", ""
                ),
                "url_identity_sha256": raw.get(
                    "url_identity_sha256", ""
                ),
                "source_content_digest": raw.get(
                    "source_content_digest", ""
                ),
                "source_content_digest_algorithm": raw.get(
                    "source_content_digest_algorithm", ""
                ),
                "content_type": raw.get("content_type", ""),
                "content_bytes": raw.get("content_bytes", 0),
                "content_cache_path": raw.get(
                    "content_cache_path", ""
                ),
                "content_sha256": content_sha256,
                "enumeration_content_hash_status": raw.get(
                    "content_hash_status", ""
                ),
                "include_in_hydration": int(include),
                "cache_reuse_available": cache_reuse,
                "hydration_required": hydration_required,
                "hydration_request_id": request_id,
                "hydration_disposition": hydration_disposition,
                "retrieval_authorized": 0,
                "parser_execution_authorized": 0,
            }
        )

    rows_by_request: dict[str, list[dict[str, object]]] = defaultdict(
        list
    )
    for row in reviewed:
        request_id = str(row["hydration_request_id"])
        if request_id:
            rows_by_request[request_id].append(row)

    requests: list[dict[str, object]] = []
    for request_id, rows in sorted(rows_by_request.items()):
        identity_type, _, identity_hash = request_identity[request_id]
        representative = rows[0]
        parser_metrics = sorted(
            {
                metric
                for row in rows
                for metric in str(
                    row.get("applicable_parser_metric_ids") or ""
                ).split("|")
                if metric
            }
        )
        supporting_metrics = sorted(
            {
                metric
                for row in rows
                for metric in str(
                    row.get("applicable_supporting_metric_ids") or ""
                ).split("|")
                if metric
            }
        )
        requests.append(
            {
                "review_version": PRIMARY_DOCUMENT_REVIEW_VERSION,
                "hydration_request_id": request_id,
                "retrieval_identity_type": identity_type,
                "retrieval_identity_sha256": identity_hash,
                "retrieval_url": representative["retrieval_url"],
                "canonical_url": representative["canonical_url"],
                "source_content_digest": representative[
                    "source_content_digest"
                ],
                "source_content_digest_algorithm": representative[
                    "source_content_digest_algorithm"
                ],
                "fanout_document_count": len(rows),
                "fanout_ticker_count": len(
                    {str(row["ticker"]) for row in rows}
                ),
                "fanout_tickers": "|".join(
                    sorted({str(row["ticker"]) for row in rows})
                ),
                "source_domains": "|".join(
                    sorted(
                        {
                            str(row["source_domain"])
                            for row in rows
                        }
                    )
                ),
                "document_types": "|".join(
                    sorted(
                        {
                            str(row["document_type"])
                            for row in rows
                        }
                    )
                ),
                "applicable_parser_metric_count": len(parser_metrics),
                "applicable_parser_metric_ids": "|".join(
                    parser_metrics
                ),
                "applicable_supporting_metric_count": len(
                    supporting_metrics
                ),
                "applicable_supporting_metric_ids": "|".join(
                    supporting_metrics
                ),
                "parse_all_applicable_metrics": 1,
                "hydration_status": "PLANNED_NOT_AUTHORIZED",
                "retrieval_authorized": 0,
                "parser_execution_authorized": 0,
            }
        )
    return reviewed, requests, errors


def summarize_review(
    *,
    endpoint_rows: Sequence[Mapping[str, object]],
    external_rows: Sequence[Mapping[str, object]],
    document_rows: Sequence[Mapping[str, object]],
    hydration_rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    endpoint_dispositions = Counter(
        str(row["endpoint_disposition"]) for row in endpoint_rows
    )
    external_dispositions = Counter(
        str(row["review_disposition"]) for row in external_rows
    )
    document_dispositions = Counter(
        str(row["document_review_disposition"])
        for row in document_rows
    )
    included = sum(
        _as_int(row.get("include_in_hydration")) for row in document_rows
    )
    cached = sum(
        _as_int(row.get("cache_reuse_available")) for row in document_rows
    )
    hydration_required = sum(
        _as_int(row.get("hydration_required")) for row in document_rows
    )
    return {
        "endpoint_review_count": len(endpoint_rows),
        "exception_endpoint_review_count": sum(
            _as_int(row.get("exception_review_required"))
            for row in endpoint_rows
        ),
        "endpoint_disposition_counts": dict(
            sorted(endpoint_dispositions.items())
        ),
        "zero_document_endpoint_count": sum(
            _as_int(row.get("candidate_document_count")) == 0
            for row in endpoint_rows
        ),
        "partial_discovery_endpoint_count": sum(
            str(row.get("enumeration_status") or "")
            == "ENUMERATED_WITH_PARTIAL_DISCOVERY_FAILURES"
            for row in endpoint_rows
        ),
        "access_limited_endpoint_count": sum(
            str(row.get("enumeration_status") or "").startswith(
                "REVIEWED_"
            )
            for row in endpoint_rows
        ),
        "external_domain_row_count": len(external_rows),
        "external_domain_disposition_counts": dict(
            sorted(external_dispositions.items())
        ),
        "reviewed_document_count": len(document_rows),
        "document_review_disposition_counts": dict(
            sorted(document_dispositions.items())
        ),
        "hydration_included_document_count": included,
        "hydration_excluded_document_count": len(document_rows) - included,
        "discovery_cache_reuse_document_count": cached,
        "hydration_required_document_count": hydration_required,
        "unique_hydration_request_count": len(hydration_rows),
        "physical_request_deduplication_savings": (
            hydration_required - len(hydration_rows)
        ),
    }

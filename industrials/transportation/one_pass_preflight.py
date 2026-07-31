from __future__ import annotations

from collections import Counter
from typing import Mapping, Sequence


ONE_PASS_PREFLIGHT_VERSION = (
    "transportation_dp6n_all_inclusive_one_pass_preflight_v1"
)
ONE_PASS_REQUIREMENT_FIELDS = (
    "preflight_version",
    "pair_key",
    "ticker",
    "metric_id",
    "source_lane",
    "coverage_status",
    "readiness_lane",
    "required_action",
    "candidate_source_lane_ids",
    "search_terms",
    "endpoint_id",
    "endpoint_type",
    "discovery_url",
    "approved_domain",
    "target_domain",
    "semantic_fixture_id",
    "financial_repair_id",
    "document_discovery_required",
    "expected_parse_path",
    "parse_all_applicable_metrics",
    "retrieval_authorized",
    "parser_execution_authorized",
)
ONE_PASS_TICKER_SCOPE_FIELDS = (
    "preflight_version",
    "ticker",
    "universe_role",
    "endpoint_id",
    "endpoint_type",
    "discovery_url",
    "discovery_requirement_count",
    "document_discovery_required_count",
    "applicable_parser_metric_count",
    "applicable_parser_metric_ids",
    "applicable_supporting_metric_count",
    "applicable_supporting_metric_ids",
    "financial_repair_metric_count",
    "financial_repair_metric_ids",
    "parse_all_applicable_metrics",
    "retrieval_authorized",
    "parser_execution_authorized",
)


def build_one_pass_preflight(
    *,
    residual_rows: Sequence[Mapping[str, str]],
    endpoint_rows: Sequence[Mapping[str, str]],
    base_pair_endpoint_rows: Sequence[Mapping[str, str]],
    semantic_pair_rows: Sequence[Mapping[str, str]],
    financial_pair_rows: Sequence[Mapping[str, str]],
    full_scope_rows: Sequence[Mapping[str, str]],
    supporting_scope_rows: Sequence[Mapping[str, str]],
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[str],
]:
    endpoints = {row["ticker"]: row for row in endpoint_rows}
    base = {row["pair_key"]: row for row in base_pair_endpoint_rows}
    semantic = {row["pair_key"]: row for row in semantic_pair_rows}
    financial = {row["pair_key"]: row for row in financial_pair_rows}
    residual_keys = {row["pair_key"] for row in residual_rows}
    errors: list[str] = []
    lane_sets = (set(base), set(semantic), set(financial))
    if any(
        lane_sets[index] & lane_sets[other]
        for index in range(len(lane_sets))
        for other in range(index + 1, len(lane_sets))
    ):
        errors.append("preflight readiness lanes overlap")
    union = set().union(*lane_sets)
    if union != residual_keys:
        errors.append(
            "preflight lanes do not exactly reconcile the residual audit"
        )
    requirements: list[dict[str, object]] = []
    for residual in sorted(
        residual_rows,
        key=lambda row: (row["ticker"], row["metric_id"]),
    ):
        pair_key = residual["pair_key"]
        ticker = residual["ticker"]
        endpoint = endpoints.get(ticker)
        if endpoint is None:
            errors.append(f"{pair_key}: missing sealed endpoint root")
            continue
        semantic_fixture_id = ""
        financial_repair_id = ""
        if pair_key in base:
            lane = "BASE_RETRIEVAL_REQUIREMENT"
            required_action = residual["required_action"]
            candidate_lanes = residual["candidate_source_lane_ids"]
            search_terms = residual["search_aliases"]
            discovery_required = 1
            parse_path = "DEDICATED_PARSER_ALL_APPLICABLE_METRICS"
        elif pair_key in semantic:
            lane = "SEMANTIC_FIXTURE_RETRIEVAL_REQUIREMENT"
            required_action = (
                "RETRIEVE_WITH_FROZEN_SEMANTIC_CONTRACT"
            )
            candidate_lanes = residual["candidate_source_lane_ids"]
            search_terms = residual["search_aliases"]
            discovery_required = 1
            parse_path = "DEDICATED_PARSER_ALL_APPLICABLE_METRICS"
            semantic_fixture_id = semantic[pair_key]["fixture_id"]
        elif pair_key in financial:
            repair = financial[pair_key]
            lane = "FINANCIAL_INPUT_REPAIR_REQUIREMENT"
            required_action = repair["required_action"]
            candidate_lanes = repair["candidate_source_lane_ids"]
            search_terms = repair["search_terms"]
            discovery_required = int(
                repair["retrieval_included_in_one_pass"]
            )
            parse_path = (
                "FINANCIAL_NORMALIZER_INPUT_EXTRACTION"
                if discovery_required
                else "NO_DOCUMENT_PARSE_REPAIR_FROM_EXISTING_FACTS"
            )
            financial_repair_id = repair["repair_id"]
        else:
            errors.append(f"{pair_key}: no preflight readiness lane")
            continue
        requirements.append(
            {
                "preflight_version": ONE_PASS_PREFLIGHT_VERSION,
                "pair_key": pair_key,
                "ticker": ticker,
                "metric_id": residual["metric_id"],
                "source_lane": residual["source_lane"],
                "coverage_status": residual["coverage_status"],
                "readiness_lane": lane,
                "required_action": required_action,
                "candidate_source_lane_ids": candidate_lanes,
                "search_terms": search_terms,
                "endpoint_id": endpoint["endpoint_id"],
                "endpoint_type": endpoint["endpoint_type"],
                "discovery_url": endpoint["discovery_url"],
                "approved_domain": endpoint["approved_domain"],
                "target_domain": endpoint["target_domain"],
                "semantic_fixture_id": semantic_fixture_id,
                "financial_repair_id": financial_repair_id,
                "document_discovery_required": discovery_required,
                "expected_parse_path": parse_path,
                "parse_all_applicable_metrics": 1,
                "retrieval_authorized": 0,
                "parser_execution_authorized": 0,
            }
        )
    parser_scope_by_ticker: dict[str, list[str]] = {}
    for row in full_scope_rows:
        if (
            row["source_lane"] in {"DP", "DP-D"}
            and row["applicability_status"] == "APPLICABLE"
        ):
            parser_scope_by_ticker.setdefault(
                row["ticker"],
                [],
            ).append(row["metric_id"])
    support_scope_by_ticker: dict[str, list[str]] = {}
    for row in supporting_scope_rows:
        if row["applicability_status"] == "APPLICABLE":
            support_scope_by_ticker.setdefault(
                row["ticker"],
                [],
            ).append(row["support_metric_id"])
    requirements_by_ticker: dict[
        str,
        list[Mapping[str, object]],
    ] = {}
    for row in requirements:
        requirements_by_ticker.setdefault(
            str(row["ticker"]),
            [],
        ).append(row)
    financial_by_ticker: dict[str, list[str]] = {}
    for row in financial_pair_rows:
        financial_by_ticker.setdefault(
            row["ticker"],
            [],
        ).append(row["metric_id"])
    ticker_scope: list[dict[str, object]] = []
    for ticker, endpoint in sorted(endpoints.items()):
        ticker_requirements = requirements_by_ticker.get(ticker, [])
        parser_metrics = sorted(
            set(parser_scope_by_ticker.get(ticker, ()))
        )
        support_metrics = sorted(
            set(support_scope_by_ticker.get(ticker, ()))
        )
        financial_metrics = sorted(
            set(financial_by_ticker.get(ticker, ()))
        )
        ticker_scope.append(
            {
                "preflight_version": ONE_PASS_PREFLIGHT_VERSION,
                "ticker": ticker,
                "universe_role": endpoint["universe_role"],
                "endpoint_id": endpoint["endpoint_id"],
                "endpoint_type": endpoint["endpoint_type"],
                "discovery_url": endpoint["discovery_url"],
                "discovery_requirement_count": len(
                    ticker_requirements
                ),
                "document_discovery_required_count": sum(
                    int(str(row["document_discovery_required"]))
                    for row in ticker_requirements
                ),
                "applicable_parser_metric_count": len(parser_metrics),
                "applicable_parser_metric_ids": "|".join(
                    parser_metrics
                ),
                "applicable_supporting_metric_count": len(
                    support_metrics
                ),
                "applicable_supporting_metric_ids": "|".join(
                    support_metrics
                ),
                "financial_repair_metric_count": len(
                    financial_metrics
                ),
                "financial_repair_metric_ids": "|".join(
                    financial_metrics
                ),
                "parse_all_applicable_metrics": 1,
                "retrieval_authorized": 0,
                "parser_execution_authorized": 0,
            }
        )
    missing_ticker_requirements = sorted(
        ticker
        for ticker in endpoints
        if not requirements_by_ticker.get(ticker)
    )
    if missing_ticker_requirements:
        errors.append(
            "endpoint tickers without discovery requirements="
            f"{missing_ticker_requirements[:10]}"
        )
    return requirements, ticker_scope, errors


def summarize_one_pass_preflight(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    return {
        "reconciled_residual_pair_count": len(rows),
        "readiness_lane_counts": dict(
            sorted(
                Counter(
                    str(row["readiness_lane"]) for row in rows
                ).items()
            )
        ),
        "document_discovery_required_pair_count": sum(
            int(str(row["document_discovery_required"]))
            for row in rows
        ),
        "no_document_repair_pair_count": sum(
            not int(str(row["document_discovery_required"]))
            for row in rows
        ),
        "mapped_ticker_count": len(
            {str(row["ticker"]) for row in rows}
        ),
        "mapped_metric_count": len(
            {str(row["metric_id"]) for row in rows}
        ),
    }

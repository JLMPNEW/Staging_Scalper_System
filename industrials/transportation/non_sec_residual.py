from __future__ import annotations

from collections import Counter
from typing import Mapping, Sequence


NON_SEC_RESIDUAL_VERSION = "transportation_dp6g_non_sec_residual_v1"
NON_SEC_POST_REPAIR_VERSION = (
    "transportation_dp6j_post_repair_non_sec_residual_v1"
)

NON_SEC_RESIDUAL_FIELDS = (
    "residual_version",
    "pair_key",
    "ticker",
    "universe_role",
    "calibration_cohort",
    "industry",
    "primary_archetype",
    "metric_id",
    "metric_pack",
    "source_lane",
    "coverage_status",
    "searched_filing_count",
    "text_hit_count",
    "value_candidate_count",
    "accepted_value_count",
    "review_value_count",
    "rejected_value_count",
    "priority",
    "required_action",
    "retrieval_eligible",
    "candidate_source_lane_ids",
    "search_aliases",
    "endpoint_manifest_status",
    "retrieval_authorized",
    "parser_execution_authorized",
)


def _dedupe(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def candidate_source_lanes(
    *,
    calibration_cohort: str,
    metric_pack: str,
    universe_role: str,
    foreign_private_issuer: bool,
    development_overlay: bool,
) -> tuple[str, ...]:
    lanes = [
        "issuer_ir_earnings_release",
        "issuer_ir_results_presentation",
        "issuer_ir_operating_statistics_supplement",
        "issuer_ir_annual_report_pdf",
    ]
    cohort = calibration_cohort.lower()
    pack = metric_pack.lower()
    if "air" in cohort or pack == "air":
        lanes.extend(
            (
                "transport_regulator_air_traffic_statistics",
                "airport_authority_traffic_statistics",
                "aviation_fleet_and_certification_records",
            )
        )
    if "marine" in cohort or pack == "marine":
        lanes.extend(
            (
                "exchange_or_issuer_fleet_status_report",
                "maritime_fleet_and_vessel_registry",
                "port_authority_throughput_statistics",
            )
        )
    if "surface" in cohort or pack == "surface":
        lanes.extend(
            (
                "surface_transport_regulator_operating_statistics",
                "rail_or_trucking_weekly_operating_report",
            )
        )
    if "logistics" in cohort or pack == "logistics":
        lanes.append("logistics_operating_kpi_supplement")
    if development_overlay or pack == "development":
        lanes.extend(
            (
                "issuer_project_milestone_update",
                "transport_certification_or_permit_record",
            )
        )
    if foreign_private_issuer:
        lanes.extend(
            (
                "primary_local_exchange_regulatory_filing",
                "foreign_issuer_home_market_annual_report",
            )
        )
    if universe_role != "active":
        lanes.extend(
            (
                "archived_issuer_ir_site",
                "acquirer_or_merger_archive",
                "exchange_delisting_archive",
            )
        )
    return _dedupe(lanes)


def residual_disposition(
    *,
    coverage_status: str,
    source_lane: str,
) -> tuple[int, str, int]:
    if source_lane == "FIN-D":
        return 4, "REPAIR_FINANCIAL_INPUT_PIPELINE", 0
    if coverage_status == "COVERED_REVIEW_REQUIRED":
        return 3, "ADJUDICATE_EXISTING_EVIDENCE_FIRST", 0
    if coverage_status == "PARSER_FAILURE_ONLY":
        return 1, "REPAIR_EXISTING_PARSER_FAILURE_FIRST", 0
    if coverage_status == "SEARCH_INCOMPLETE":
        return 1, "COMPLETE_EXISTING_SEALED_SEARCH_FIRST", 0
    if coverage_status in {
        "SEARCHED_NOT_FOUND",
        "TEXT_HIT_NO_VALUE",
    }:
        return 1, "SEAL_NON_SEC_ENDPOINTS_FOR_ONE_PASS_RETRIEVAL", 1
    if coverage_status == "DISCOVERED_REJECTED":
        return 2, "SEAL_ALTERNATE_PRIMARY_SOURCE_ENDPOINTS", 1
    return 4, "REVIEW_RESIDUAL_COVERAGE_STATE", 0


def build_non_sec_residual_rows(
    *,
    coverage_rows: Sequence[Mapping[str, str]],
    metric_aliases: Mapping[str, Sequence[str]],
    foreign_tickers: set[str],
    residual_version: str = NON_SEC_RESIDUAL_VERSION,
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for row in coverage_rows:
        if row.get("applicability_status") != "APPLICABLE":
            continue
        status = str(row.get("coverage_status") or "")
        if status in {
            "COVERED_ACCEPTED",
            "COVERED_FINANCIAL_DERIVED",
        }:
            continue
        ticker = str(row.get("ticker") or "").upper()
        metric_id = str(row.get("metric_id") or "")
        source_lane = str(row.get("source_lane") or "")
        priority, action, retrieval_eligible = residual_disposition(
            coverage_status=status,
            source_lane=source_lane,
        )
        if source_lane == "FIN-D":
            lanes = ("financial_statement_input_pipeline",)
        else:
            lanes = candidate_source_lanes(
                calibration_cohort=str(
                    row.get("calibration_cohort") or ""
                ),
                metric_pack=str(row.get("metric_pack") or ""),
                universe_role=str(row.get("universe_role") or ""),
                foreign_private_issuer=ticker in foreign_tickers,
                development_overlay=(
                    str(row.get("metric_pack") or "") == "development"
                ),
            )
        output.append(
            {
                "residual_version": residual_version,
                "pair_key": f"{ticker}|{metric_id}",
                "ticker": ticker,
                "universe_role": row.get("universe_role") or "",
                "calibration_cohort": (
                    row.get("calibration_cohort") or ""
                ),
                "industry": row.get("industry") or "",
                "primary_archetype": (
                    row.get("primary_archetype") or ""
                ),
                "metric_id": metric_id,
                "metric_pack": row.get("metric_pack") or "",
                "source_lane": source_lane,
                "coverage_status": status,
                "searched_filing_count": (
                    row.get("searched_filing_count") or "0"
                ),
                "text_hit_count": row.get("text_hit_count") or "0",
                "value_candidate_count": (
                    row.get("value_candidate_count") or "0"
                ),
                "accepted_value_count": (
                    row.get("accepted_value_count") or "0"
                ),
                "review_value_count": (
                    row.get("review_value_count") or "0"
                ),
                "rejected_value_count": (
                    row.get("rejected_value_count") or "0"
                ),
                "priority": priority,
                "required_action": action,
                "retrieval_eligible": retrieval_eligible,
                "candidate_source_lane_ids": "|".join(lanes),
                "search_aliases": "|".join(
                    metric_aliases.get(metric_id, ())
                ),
                "endpoint_manifest_status": "NOT_YET_SEALED",
                "retrieval_authorized": 0,
                "parser_execution_authorized": 0,
            }
        )
    output.sort(
        key=lambda row: (
            int(str(row["priority"])),
            str(row["metric_id"]),
            str(row["ticker"]),
        )
    )
    return output


def summarize_residual_rows(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    return {
        "residual_pair_count": len(rows),
        "residual_metric_count": len(
            {str(row["metric_id"]) for row in rows}
        ),
        "residual_ticker_count": len(
            {str(row["ticker"]) for row in rows}
        ),
        "retrieval_eligible_pair_count": sum(
            int(str(row["retrieval_eligible"])) for row in rows
        ),
        "coverage_status_counts": dict(
            sorted(
                Counter(
                    str(row["coverage_status"]) for row in rows
                ).items()
            )
        ),
        "required_action_counts": dict(
            sorted(
                Counter(
                    str(row["required_action"]) for row in rows
                ).items()
            )
        ),
        "priority_counts": dict(
            sorted(
                Counter(str(row["priority"]) for row in rows).items()
            )
        ),
    }

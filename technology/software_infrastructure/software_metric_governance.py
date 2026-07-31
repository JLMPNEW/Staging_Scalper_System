from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from dedicated_parser.contracts import file_sha256, stable_hash
from dedicated_parser.golden import validate_corpus


MODEL_FAMILY = "software_infrastructure"
RELEASE_ID = "software_metrics_v1"
POLICY_VERSION = "software_metrics_adjudication_v1"
SOURCE_RUN_IDS = (13, 14)
EXPECTED_COUNTS = {
    "ACCEPTED": 23,
    "CORRECTED": 6,
    "REJECTED_POLICY": 48,
}
PRIMARY_PROSE_METRICS = frozenset(
    {
        "annual_recurring_revenue",
        "net_revenue_retention",
        "disclosed_billings",
        "subscription_revenue",
    }
)
RECONCILIATION_METRICS = frozenset(
    {
        "remaining_performance_obligation",
        "current_remaining_performance_obligation",
        "deferred_revenue_current",
        "deferred_revenue_noncurrent",
        "deferred_revenue_total",
    }
)
EVENT_METRICS = frozenset({"customer_count_threshold"})
EXPANSION_FAMILIES = (
    "remaining_performance_obligation",
    "deferred_revenue_total",
    "annual_recurring_revenue",
    "net_revenue_retention",
    "disclosed_billings",
    "subscription_revenue",
    "customer_count_threshold",
)
CORPUS_TARGET_PER_FAMILY = 20


@dataclass(frozen=True)
class DecisionSpec:
    decision: str
    reason: str
    period_end: str
    effective_metric: str = ""
    effective_value: float | None = None
    scope: str = "consolidated"
    period_kind: str = "instant"
    definition_variant: str = "total"
    calibration_eligible: int = 1


def _key(ticker: str, metric: str, value: float) -> tuple[str, str, float]:
    return ticker, metric, float(value)


_KEPT: dict[tuple[str, str, float], DecisionSpec] = {
    _key("AI", "subscription_revenue", 48_400_000): DecisionSpec(
        "ACCEPTED",
        "confirmed_quarterly_subscription_revenue",
        "2026-04-30",
        period_kind="quarterly",
        definition_variant="total_subscription_revenue",
    ),
    _key("AI", "subscription_revenue", 227_100_000): DecisionSpec(
        "ACCEPTED",
        "confirmed_annual_subscription_revenue",
        "2026-04-30",
        period_kind="annual",
        definition_variant="total_subscription_revenue",
    ),
    _key("CVLT", "subscription_revenue", 208_000_000): DecisionSpec(
        "ACCEPTED",
        "confirmed_quarterly_subscription_revenue",
        "2026-03-31",
        period_kind="quarterly",
        definition_variant="total_subscription_revenue",
    ),
    _key("CVLT", "subscription_revenue", 768_000_000): DecisionSpec(
        "ACCEPTED",
        "confirmed_annual_subscription_revenue",
        "2026-03-31",
        period_kind="annual",
        definition_variant="total_subscription_revenue",
    ),
    _key("DOCN", "annual_recurring_revenue", 1_032_000_000): DecisionSpec(
        "ACCEPTED",
        "confirmed_total_arr",
        "2026-03-31",
        definition_variant="total_arr",
    ),
    _key("DT", "annual_recurring_revenue", 2_054_000_000): DecisionSpec(
        "ACCEPTED",
        "confirmed_total_arr",
        "2026-03-31",
        definition_variant="total_arr",
    ),
    _key("DT", "subscription_revenue", 506_000_000): DecisionSpec(
        "ACCEPTED",
        "confirmed_quarterly_subscription_revenue",
        "2026-03-31",
        period_kind="quarterly",
        definition_variant="total_subscription_revenue",
    ),
    _key("DT", "subscription_revenue", 1_930_000_000): DecisionSpec(
        "ACCEPTED",
        "confirmed_annual_subscription_revenue",
        "2026-03-31",
        period_kind="annual",
        definition_variant="total_subscription_revenue",
    ),
    _key("ESTC", "remaining_performance_obligation", 1_982_000_000): DecisionSpec(
        "ACCEPTED",
        "confirmed_total_rpo",
        "2026-04-30",
        definition_variant="total_rpo",
    ),
    _key("ESTC", "subscription_revenue", 422_000_000): DecisionSpec(
        "ACCEPTED",
        "confirmed_quarterly_total_subscription_revenue",
        "2026-04-30",
        period_kind="quarterly",
        definition_variant="total_subscription_revenue",
    ),
    _key("ESTC", "subscription_revenue", 1_634_000_000): DecisionSpec(
        "ACCEPTED",
        "confirmed_annual_total_subscription_revenue",
        "2026-04-30",
        period_kind="annual",
        definition_variant="total_subscription_revenue",
    ),
    _key("MDB", "remaining_performance_obligation", 1_458_600_000): DecisionSpec(
        "ACCEPTED",
        "confirmed_total_rpo",
        "2026-04-30",
        definition_variant="total_rpo",
    ),
    _key("ORCL", "remaining_performance_obligation", 638_000_000_000): DecisionSpec(
        "ACCEPTED",
        "confirmed_total_rpo",
        "2026-05-31",
        definition_variant="total_rpo",
    ),
    _key("PATH", "annual_recurring_revenue", 1_901_000_000): DecisionSpec(
        "ACCEPTED",
        "confirmed_total_arr",
        "2026-04-30",
        definition_variant="total_arr",
    ),
    _key("PATH", "net_revenue_retention", 1.09): DecisionSpec(
        "ACCEPTED",
        "confirmed_dollar_based_net_retention",
        "2026-04-30",
        definition_variant="dollar_based_net_retention",
    ),
    _key("PD", "annual_recurring_revenue", 496_000_000): DecisionSpec(
        "ACCEPTED",
        "confirmed_total_arr",
        "2026-04-30",
        definition_variant="total_arr",
    ),
    _key("PD", "remaining_performance_obligation", 441_000_000): DecisionSpec(
        "ACCEPTED",
        "confirmed_total_rpo",
        "2026-04-30",
        definition_variant="total_rpo",
    ),
    _key("SNOW", "customer_count_threshold", 779): DecisionSpec(
        "ACCEPTED",
        "confirmed_threshold_customer_count_censored",
        "2026-04-30",
        definition_variant="ttm_product_revenue_gt_1m",
        calibration_eligible=0,
    ),
    _key("SNOW", "remaining_performance_obligation", 9_210_000_000): DecisionSpec(
        "ACCEPTED",
        "confirmed_total_rpo",
        "2026-04-30",
        definition_variant="total_rpo",
    ),
    _key("TEAM", "remaining_performance_obligation", 3_996_000_000): DecisionSpec(
        "ACCEPTED",
        "confirmed_total_rpo",
        "2026-03-31",
        definition_variant="total_rpo",
    ),
    _key("CLBT", "annual_recurring_revenue", 493_000_000): DecisionSpec(
        "ACCEPTED",
        "confirmed_total_arr",
        "2026-03-31",
        definition_variant="total_arr",
    ),
    _key("CLBT", "subscription_revenue", 117_900_000): DecisionSpec(
        "ACCEPTED",
        "confirmed_quarterly_subscription_revenue",
        "2026-03-31",
        period_kind="quarterly",
        definition_variant="total_subscription_revenue",
    ),
    _key("BB", "annual_recurring_revenue", 171_000_000): DecisionSpec(
        "ACCEPTED",
        "confirmed_cylance_segment_arr",
        "2019-11-30",
        scope="segment",
        definition_variant="cylance_segment_arr",
        calibration_eligible=0,
    ),
}

_CORRECTED: dict[tuple[str, str, float], DecisionSpec] = {
    _key("OKTA", "subscription_revenue", 765_000_000): DecisionSpec(
        "CORRECTED",
        "corrected_total_revenue_to_subscription_revenue",
        "2026-04-30",
        effective_value=750_000_000,
        period_kind="quarterly",
        definition_variant="total_subscription_revenue",
    ),
    _key("AUID", "deferred_revenue_total", 2_000_000): DecisionSpec(
        "CORRECTED",
        "corrected_rpo_to_deferred_revenue",
        "2026-03-31",
        effective_value=380_000,
        definition_variant="total_deferred_revenue",
    ),
    _key("PD", "current_remaining_performance_obligation", 441_000_000): DecisionSpec(
        "CORRECTED",
        "corrected_total_rpo_to_current_12m_rpo",
        "2026-04-30",
        effective_value=316_000_000,
        definition_variant="current_12m_rpo",
    ),
    _key("ESTC", "remaining_performance_obligation", 1_203_000_000): DecisionSpec(
        "CORRECTED",
        "reclassified_total_rpo_candidate_to_current_rpo",
        "2026-04-30",
        effective_metric="current_remaining_performance_obligation",
        definition_variant="current_rpo",
    ),
    _key("DOX", "current_remaining_performance_obligation", 6_600_000_000): DecisionSpec(
        "CORRECTED",
        "reclassified_current_rpo_candidate_to_total_rpo",
        "2026-03-31",
        effective_metric="remaining_performance_obligation",
        definition_variant="total_rpo",
    ),
}


def _row_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    source = dict(row)
    fields = (
        "evidence_key",
        "work_key",
        "model_family",
        "adapter_version",
        "ticker",
        "cik",
        "accession_number",
        "form_type",
        "filing_date",
        "accepted_at",
        "report_date",
        "metric_name",
        "concept_name",
        "candidate_value",
        "unit",
        "period_start",
        "period_end",
        "scope",
        "confidence",
        "candidate_status",
        "status_reason",
        "evidence_text",
        "source_document",
        "extraction_method",
        "provenance_json",
        "parser_release",
    )
    return {field: source.get(field) for field in fields}


def load_source_rows(
    conn: sqlite3.Connection,
    *,
    run_ids: Iterable[int] = SOURCE_RUN_IDS,
) -> list[dict[str, Any]]:
    ids = tuple(int(value) for value in run_ids)
    if not ids:
        raise ValueError("At least one source run id is required")
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT evidence.*
        FROM sec_parser_run_metric_evidence AS relation
        JOIN sec_parser_metric_evidence_shadow AS evidence
          ON evidence.evidence_key = relation.evidence_key
        WHERE relation.run_id IN ({placeholders})
        ORDER BY relation.run_id, evidence.ticker, evidence.metric_name,
                 evidence.source_document, evidence.candidate_value,
                 evidence.evidence_key
        """,
        ids,
    ).fetchall()
    output = [_row_payload(row) for row in rows]
    if len(output) != 77:
        raise RuntimeError(
            f"{RELEASE_ID} requires exactly 77 source rows; found {len(output)}"
        )
    if any(row["candidate_status"] != "REVIEW_REQUIRED" for row in output):
        raise RuntimeError(
            f"{RELEASE_ID} source runs must contain only REVIEW_REQUIRED rows"
        )
    return output


def _is_corrected_dox_total(row: dict[str, Any]) -> bool:
    return (
        row["ticker"] == "DOX"
        and row["metric_name"] == "deferred_revenue_total"
        and float(row["candidate_value"]) == 28_393.0
        and "Total Deferred revenue" in str(row["evidence_text"])
    )


def _is_primary_path_arr(row: dict[str, Any]) -> bool:
    return (
        row["ticker"] == "PATH"
        and row["metric_name"] == "annual_recurring_revenue"
        and float(row["candidate_value"]) == 1_901_000_000.0
        and "as of April 30, 2026" in str(row["evidence_text"])
    )


def _reject_reason(row: dict[str, Any]) -> str:
    text = str(row["evidence_text"] or "").lower()
    ticker = str(row["ticker"])
    value = float(row["candidate_value"])
    if "customers with" in text or "customers spending" in text:
        return "customer_threshold_misread_as_metric_value"
    if (
        "expected to be" in text
        or "in the range of" in text
        or "guidance" in text
        or ticker == "PANW"
        or (ticker == "CRWD" and value < 10_000)
    ):
        return "guidance_or_forecast_not_actual"
    if ticker == "DOX" and value == 28_393:
        return "duplicate_false_match_allowance_not_deferred_revenue"
    if ticker == "PATH" and value == 1_901_000_000:
        return "duplicate_rounded_or_repeated_disclosure"
    if "increase in rpo" in text or (
        ticker == "ORCL" and value == 85_000_000_000
    ):
        return "change_or_flow_not_balance"
    if ticker == "MIME":
        return "change_or_flow_not_balance"
    if ticker == "BB" and "business combination accounting" in text:
        return "purchase_accounting_adjustment_not_balance"
    if ticker == "DT" and value == 2_000_000_000:
        return "rounded_duplicate_of_precise_arr"
    if "net new arr" in text or "incremental organic arr" in text:
        return "net_new_or_incremental_flow_not_level"
    if "sales-led subscription revenue" in text:
        return "non_gaap_subset_not_total_metric"
    if ticker == "TEAM" and value == 1_000_000_000:
        return "segment_or_product_line_not_consolidated"
    if ticker == "AUID" and "booked annual recurring revenue" in text:
        return "bookings_flow_not_ending_arr"
    if ticker == "SNOW" and value == 1_000_000:
        return "customer_threshold_misread_as_rpo"
    return "reviewed_candidate_rejected_not_target_actual"


def adjudicate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    previous_hash = "0" * 64
    for sequence, row in enumerate(rows, start=1):
        value = float(row["candidate_value"])
        key = _key(str(row["ticker"]), str(row["metric_name"]), value)
        spec: DecisionSpec | None = None
        if _is_corrected_dox_total(row):
            spec = DecisionSpec(
                "CORRECTED",
                "corrected_allowance_false_match_to_total_deferred_revenue",
                "2026-03-31",
                effective_value=163_031_000,
                definition_variant="total_deferred_revenue",
            )
        elif key == _key(
            "PATH",
            "annual_recurring_revenue",
            1_901_000_000,
        ):
            spec = _KEPT[key] if _is_primary_path_arr(row) else None
        else:
            spec = _KEPT.get(key) or _CORRECTED.get(key)

        if spec is None:
            spec = DecisionSpec(
                "REJECTED_POLICY",
                _reject_reason(row),
                str(row["period_end"] or ""),
                effective_metric=str(row["metric_name"]),
                effective_value=value,
                scope=str(row["scope"] or "unknown"),
                calibration_eligible=0,
            )
        effective_metric = spec.effective_metric or str(row["metric_name"])
        effective_value = (
            spec.effective_value
            if spec.effective_value is not None
            else value
        )
        source_hash = stable_hash(_row_payload(row))
        decision = {
            "release_id": RELEASE_ID,
            "sequence": sequence,
            "source_run_ids": list(SOURCE_RUN_IDS),
            "source_parser_release": str(row.get("parser_release") or ""),
            "source_adapter_version": str(row.get("adapter_version") or ""),
            "source_evidence_key": row["evidence_key"],
            "source_row_sha256": source_hash,
            "ticker": row["ticker"],
            "cik": row["cik"],
            "accession_number": row["accession_number"],
            "form_type": row["form_type"],
            "filing_date": row["filing_date"],
            "accepted_at": row["accepted_at"],
            "source_document": row["source_document"],
            "source_document_sha256": _source_document_sha256(row),
            "source_metric": row["metric_name"],
            "source_value": value,
            "source_unit": row["unit"],
            "source_period_start": row["period_start"],
            "source_period_end": row["period_end"],
            "decision": spec.decision,
            "decision_reason": spec.reason,
            "effective_metric": effective_metric,
            "effective_value": effective_value,
            "effective_unit": row["unit"],
            "effective_period_start": row["period_start"],
            "effective_period_end": spec.period_end,
            "effective_scope": spec.scope,
            "period_kind": spec.period_kind,
            "definition_variant": spec.definition_variant,
            "calibration_eligible_flag": spec.calibration_eligible,
            "previous_decision_hash": previous_hash,
        }
        decision_hash = stable_hash(decision)
        decision["decision_hash"] = decision_hash
        previous_hash = decision_hash
        decisions.append(decision)
    counts = Counter(row["decision"] for row in decisions)
    if dict(counts) != EXPECTED_COUNTS:
        raise RuntimeError(
            f"Adjudication count mismatch: expected={EXPECTED_COUNTS} "
            f"actual={dict(counts)}"
        )
    return decisions


def _source_document_sha256(row: dict[str, Any]) -> str:
    try:
        provenance = json.loads(str(row.get("provenance_json") or "{}"))
    except json.JSONDecodeError:
        return ""
    return str(provenance.get("document_sha256") or "")


def build_golden_corpus(rows: list[dict[str, Any]]) -> dict[str, Any]:
    expectations = []
    for index, row in enumerate(rows, start=1):
        expectations.append(
            {
                "id": f"software_v1_source_{index:03d}",
                "ticker": row["ticker"],
                "accession_number": row["accession_number"],
                "document_name": row["source_document"],
                "metric_name": row["metric_name"],
                "candidate_status": "REVIEW_REQUIRED",
                "candidate_value": row["candidate_value"],
                "unit": row["unit"],
                "period_start": row["period_start"],
                "period_end": row["period_end"],
                "reason_contains": "prose_candidate_requires_period_unit_scope_review",
                "value_tolerance": 1e-6,
            }
        )
    return {
        "corpus_id": RELEASE_ID,
        "description": (
            "Immutable 77-observation software-metrics extraction corpus from "
            "software parser runs 13 and 14. Human decisions are sealed in "
            "software_metrics_policy_v1.json."
        ),
        "source_run_ids": list(SOURCE_RUN_IDS),
        "expectations": expectations,
    }


def build_policy_payload(
    *,
    decisions: list[dict[str, Any]],
    registry_path: Path,
    adapter_path: Path,
) -> dict[str, Any]:
    return {
        "policy_id": POLICY_VERSION,
        "release_id": RELEASE_ID,
        "model_family": MODEL_FAMILY,
        "source_run_ids": list(SOURCE_RUN_IDS),
        "source_parser_release": sorted(
            {
                str(value)
                for value in _decision_source_values(
                    decisions,
                    "source_parser_release",
                )
                if value
            }
        ),
        "source_adapter_versions": sorted(
            {
                str(value)
                for value in _decision_source_values(
                    decisions,
                    "source_adapter_version",
                )
                if value
            }
        ),
        "registry_path": str(registry_path),
        "registry_sha256": file_sha256(registry_path),
        "adapter_path": str(adapter_path),
        "adapter_sha256": file_sha256(adapter_path),
        "decision_count": len(decisions),
        "decision_counts": dict(Counter(row["decision"] for row in decisions)),
        "chain_root_sha256": decisions[-1]["decision_hash"],
        "decisions": decisions,
    }


def _decision_source_values(
    decisions: list[dict[str, Any]],
    field: str,
) -> set[Any]:
    return {decision.get(field) for decision in decisions}


def build_expansion_queue(
    conn: sqlite3.Connection,
    *,
    source_rows: list[dict[str, Any]],
    per_family: int = CORPUS_TARGET_PER_FAMILY,
    minimum_hard_negatives: int = 5,
    minimum_historical_members: int = 3,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    existing = {
        (
            row["ticker"],
            row["accession_number"],
            row["source_document"],
            row["metric_name"],
            float(row["candidate_value"]),
        )
        for row in source_rows
    }
    metric_groups = {
        "deferred_revenue_current": "deferred_revenue_total",
        "deferred_revenue_noncurrent": "deferred_revenue_total",
        "deferred_revenue_total": "deferred_revenue_total",
        "current_remaining_performance_obligation": (
            "remaining_performance_obligation"
        ),
    }
    membership: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for row in conn.execute(
        """
        SELECT ticker, start_date,
               COALESCE(NULLIF(end_date, ''), '9999-12-31') AS end_date,
               membership_status
        FROM dim_universe_membership
        WHERE model_family = ?
          AND point_in_time_flag = 1
          AND membership_status IN (
              'active', 'historical', 'inactive', 'review'
          )
        ORDER BY ticker, start_date
        """,
        (MODEL_FAMILY,),
    ):
        membership[str(row["ticker"])].append(
            (
                str(row["start_date"]),
                str(row["end_date"]),
                str(row["membership_status"]),
            )
        )

    def membership_status(ticker: str, accepted_at: object) -> str:
        available_date = str(accepted_at or "")[:10]
        for interval_start, interval_end, status in membership.get(
            ticker,
            [],
        ):
            if interval_start <= available_date <= interval_end:
                return status
        return "outside_pit_membership"

    candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rows = conn.execute(
        """
        SELECT evidence.*
        FROM sec_parser_metric_evidence_shadow AS evidence
        WHERE evidence.model_family = ?
          AND evidence.extraction_method LIKE '%semantic_prose%'
          AND evidence.candidate_value IS NOT NULL
        ORDER BY
          CASE evidence.candidate_status
            WHEN 'REJECTED_POLICY' THEN 0
            WHEN 'REVIEW_REQUIRED' THEN 1
            ELSE 2
          END,
          evidence.accepted_at DESC,
          evidence.ticker,
          evidence.evidence_key
        """,
        (MODEL_FAMILY,),
    ).fetchall()
    seen: set[tuple[Any, ...]] = set()
    for raw in rows:
        row = _row_payload(raw)
        key = (
            row["ticker"],
            row["accession_number"],
            row["source_document"],
            row["metric_name"],
            float(row["candidate_value"]),
        )
        if key in existing or key in seen:
            continue
        seen.add(key)
        family = metric_groups.get(
            str(row["metric_name"]),
            str(row["metric_name"]),
        )
        if family not in EXPANSION_FAMILIES:
            continue
        status = membership_status(
            str(row["ticker"]),
            row["accepted_at"],
        )
        candidates[family].append(
            {
                "metric_family": family,
                "review_status": "PENDING_ADJUDICATION",
                "hard_negative_candidate_flag": int(
                    row["candidate_status"] == "REJECTED_POLICY"
                ),
                "historical_member_flag": int(status != "active"),
                "membership_status_at_filing": status,
                "ticker": row["ticker"],
                "accession_number": row["accession_number"],
                "form_type": row["form_type"],
                "accepted_at": row["accepted_at"],
                "source_document": row["source_document"],
                "source_metric": row["metric_name"],
                "candidate_value": row["candidate_value"],
                "unit": row["unit"],
                "period_end": row["period_end"],
                "parser_status": row["candidate_status"],
                "parser_reason": row["status_reason"],
                "source_evidence_key": row["evidence_key"],
                "source_row_sha256": stable_hash(row),
                "evidence_text": row["evidence_text"],
            }
        )

    source_decisions = adjudicate_rows(source_rows)
    reviewed_counts: Counter[str] = Counter()
    reviewed_hard_negative_counts: Counter[str] = Counter()
    reviewed_historical_counts: Counter[str] = Counter()
    source_by_key = {
        str(row["evidence_key"]): row for row in source_rows
    }
    for decision in source_decisions:
        family = metric_groups.get(
            str(decision["source_metric"]),
            str(decision["source_metric"]),
        )
        reviewed_counts[family] += 1
        if decision["decision"] == "REJECTED_POLICY":
            reviewed_hard_negative_counts[family] += 1
        source = source_by_key[str(decision["source_evidence_key"])]
        status = membership_status(
            str(decision["ticker"]),
            source["accepted_at"],
        )
        if status != "active":
            reviewed_historical_counts[family] += 1

    queue: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []
    for family in EXPANSION_FAMILIES:
        available = candidates[family]
        selected: list[dict[str, Any]] = []
        selected_keys: set[str] = set()

        def take(predicate: Any, count: int) -> None:
            for candidate in available:
                if len(
                    [
                        row
                        for row in selected
                        if predicate(row)
                    ]
                ) >= count:
                    break
                key = str(candidate["source_evidence_key"])
                if key in selected_keys or not predicate(candidate):
                    continue
                selected.append(candidate)
                selected_keys.add(key)

        hard_shortfall = max(
            0,
            minimum_hard_negatives
            - reviewed_hard_negative_counts[family],
        )
        historical_shortfall = max(
            0,
            minimum_historical_members
            - reviewed_historical_counts[family],
        )
        total_shortfall = max(0, per_family - reviewed_counts[family])
        take(
            lambda row: bool(row["hard_negative_candidate_flag"])
            and bool(row["historical_member_flag"]),
            min(hard_shortfall, historical_shortfall),
        )
        take(
            lambda row: bool(row["hard_negative_candidate_flag"]),
            hard_shortfall,
        )
        take(
            lambda row: bool(row["historical_member_flag"]),
            historical_shortfall,
        )
        for candidate in available:
            if len(selected) >= total_shortfall:
                break
            key = str(candidate["source_evidence_key"])
            if key not in selected_keys:
                selected.append(candidate)
                selected_keys.add(key)
        queue.extend(selected)
        queued_hard = sum(
            int(row["hard_negative_candidate_flag"]) for row in selected
        )
        queued_historical = sum(
            int(row["historical_member_flag"]) for row in selected
        )
        summary.append(
            {
                "metric_family": family,
                "reviewed_v1_count": reviewed_counts[family],
                "reviewed_hard_negative_count": (
                    reviewed_hard_negative_counts[family]
                ),
                "reviewed_historical_member_count": (
                    reviewed_historical_counts[family]
                ),
                "target_reviewed_count": per_family,
                "target_hard_negative_count": minimum_hard_negatives,
                "target_historical_member_count": (
                    minimum_historical_members
                ),
                "queued_candidate_count": len(selected),
                "queued_hard_negative_count": queued_hard,
                "queued_historical_member_count": queued_historical,
                "total_candidate_shortfall_after_queue": max(
                    0,
                    total_shortfall - len(selected),
                ),
                "hard_negative_shortfall_after_queue": max(
                    0,
                    hard_shortfall - queued_hard,
                ),
                "historical_member_shortfall_after_queue": max(
                    0,
                    historical_shortfall - queued_historical,
                ),
                "stratified_family_certified_flag": int(
                    reviewed_counts[family] >= per_family
                    and reviewed_hard_negative_counts[family]
                    >= minimum_hard_negatives
                    and reviewed_historical_counts[family]
                    >= minimum_historical_members
                ),
            }
        )
    return queue, summary

def validate_release(
    conn: sqlite3.Connection,
    *,
    corpus_path: Path,
    policy_path: Path,
    registry_path: Path,
    adapter_path: Path,
) -> list[str]:
    errors = validate_corpus(
        conn,
        corpus_path=corpus_path,
        table="sec_parser_metric_evidence_shadow",
    )
    payload = json.loads(policy_path.read_text(encoding="utf-8"))
    decisions = payload.get("decisions")
    if not isinstance(decisions, list) or len(decisions) != 77:
        errors.append("software policy must contain exactly 77 decisions")
        return errors
    previous_hash = "0" * 64
    for expected_sequence, decision in enumerate(decisions, start=1):
        if int(decision.get("sequence") or 0) != expected_sequence:
            errors.append(
                f"decision sequence mismatch at {expected_sequence}"
            )
            break
        if decision.get("previous_decision_hash") != previous_hash:
            errors.append(
                f"decision hash-chain predecessor mismatch at {expected_sequence}"
            )
            break
        expected_hash = str(decision.get("decision_hash") or "")
        hash_payload = dict(decision)
        hash_payload.pop("decision_hash", None)
        actual_hash = stable_hash(hash_payload)
        if actual_hash != expected_hash:
            errors.append(
                f"decision hash mismatch at {expected_sequence}"
            )
            break
        previous_hash = expected_hash
    if payload.get("chain_root_sha256") != previous_hash:
        errors.append("policy chain root does not match final decision hash")
    counts = Counter(str(row.get("decision")) for row in decisions)
    if dict(counts) != EXPECTED_COUNTS:
        errors.append(
            f"decision counts mismatch: {dict(counts)}"
        )
    if payload.get("registry_sha256") != file_sha256(registry_path):
        errors.append("specialized metric registry hash changed after release")
    if payload.get("adapter_sha256") != file_sha256(adapter_path):
        errors.append("software parser adapter hash changed after release")
    source_by_key = {
        row["evidence_key"]: row
        for row in load_source_rows(conn)
    }
    for decision in decisions:
        row = source_by_key.get(decision["source_evidence_key"])
        if row is None:
            errors.append(
                f"missing source evidence {decision['source_evidence_key']}"
            )
            continue
        if stable_hash(_row_payload(row)) != decision["source_row_sha256"]:
            errors.append(
                f"source row hash mismatch {decision['source_evidence_key']}"
            )
    return errors


def policy_csv_rows(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "policy_id": f"software_v1_{int(row['sequence']):03d}",
            "policy_version": POLICY_VERSION,
            "enabled": 1,
            "model_family": MODEL_FAMILY,
            "source_evidence_key": row["source_evidence_key"],
            "ticker": row["ticker"],
            "accession_number": row["accession_number"],
            "source_document": row["source_document"],
            "source_metric": row["source_metric"],
            "source_value": row["source_value"],
            "source_unit": row["source_unit"],
            "source_period_end": row["source_period_end"],
            "decision": row["decision"],
            "decision_reason": row["decision_reason"],
            "effective_metric": row["effective_metric"],
            "effective_value": row["effective_value"],
            "effective_unit": row["effective_unit"],
            "effective_period_end": row["effective_period_end"],
            "effective_scope": row["effective_scope"],
            "period_kind": row["period_kind"],
            "definition_variant": row["definition_variant"],
            "calibration_eligible_flag": row[
                "calibration_eligible_flag"
            ],
            "decision_hash": row["decision_hash"],
        }
        for row in decisions
    ]


def release_manifest(
    *,
    corpus_path: Path,
    policy_path: Path,
    policy_csv_path: Path,
    expansion_queue_path: Path,
    expansion_summary_path: Path,
    policy_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "manifest_version": "software_metric_governance_release_v1",
        "release_id": RELEASE_ID,
        "model_family": MODEL_FAMILY,
        "source_run_ids": list(SOURCE_RUN_IDS),
        "decision_counts": EXPECTED_COUNTS,
        "chain_root_sha256": policy_payload["chain_root_sha256"],
        "parser_release": "0.4.6",
        "production_facts_modified_flag": 0,
        "production_scores_modified_flag": 0,
        "predictive_validation_completed_flag": 0,
        "artifacts": {
            "golden_corpus": _artifact(corpus_path),
            "policy_json": _artifact(policy_path),
            "policy_csv": _artifact(policy_csv_path),
            "expansion_queue": _artifact(expansion_queue_path),
            "expansion_summary": _artifact(expansion_summary_path),
        },
    }


def _artifact(path: Path) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "sha256": file_sha256(path),
        "bytes": path.stat().st_size,
    }


def decision_to_dict(spec: DecisionSpec) -> dict[str, Any]:
    return asdict(spec)

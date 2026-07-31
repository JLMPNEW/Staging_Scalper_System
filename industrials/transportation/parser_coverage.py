from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from dedicated_parser.contracts import file_sha256
from industrials.core.reports import write_csv_atomic, write_text_atomic


MODEL_FAMILY = "transportation"
FINAL_COVERAGE_FIELDS = (
    "run_id",
    "ticker",
    "universe_role",
    "calibration_cohort",
    "industry",
    "primary_archetype",
    "metric_id",
    "metric_pack",
    "source_lane",
    "applicability_status",
    "coverage_status",
    "derivation_basis",
    "searched_filing_count",
    "completed_filing_count",
    "failed_filing_count",
    "text_hit_count",
    "value_candidate_count",
    "accepted_value_count",
    "review_value_count",
    "rejected_value_count",
    "parser_failure_count",
    "distinct_period_count",
    "first_period_end",
    "last_period_end",
)
METRIC_SUMMARY_FIELDS = (
    "run_id",
    "metric_id",
    "metric_pack",
    "source_lane",
    "applicable_ticker_count",
    "active_applicable_ticker_count",
    "inactive_applicable_ticker_count",
    "searched_ticker_count",
    "text_hit_ticker_count",
    "value_candidate_ticker_count",
    "accepted_ticker_count",
    "review_ticker_count",
    "rejected_only_ticker_count",
    "parser_failure_only_ticker_count",
    "searched_not_found_ticker_count",
    "search_incomplete_ticker_count",
    "accepted_coverage_rate",
    "usable_coverage_rate",
    "discovery_coverage_rate",
)
COHORT_SUMMARY_FIELDS = (
    "run_id",
    "calibration_cohort",
    "metric_id",
    "metric_pack",
    "source_lane",
    "applicable_ticker_count",
    "accepted_ticker_count",
    "usable_ticker_count",
    "discovered_ticker_count",
    "accepted_coverage_rate",
    "usable_coverage_rate",
    "discovery_coverage_rate",
)
SUPPORT_COVERAGE_FIELDS = (
    "run_id",
    "ticker",
    "universe_role",
    "calibration_cohort",
    "primary_archetype",
    "support_metric_id",
    "consumer_metric_ids",
    "applicability_status",
    "coverage_status",
    "searched_filing_count",
    "text_hit_count",
    "value_candidate_count",
    "accepted_value_count",
    "review_value_count",
    "rejected_value_count",
    "parser_failure_count",
    "distinct_period_count",
    "first_period_end",
    "last_period_end",
)

PARSER_DERIVATIONS: dict[str, dict[str, Any]] = {
    "surface_volume_growth": {
        "mode": "any",
        "dependencies": (
            "rail_carload_growth",
            "rail_intermodal_volume_growth",
            "revenue_ton_miles_growth",
            "shipment_or_load_growth",
        ),
    },
    "fuel_efficiency_per_capacity_unit": {
        "mode": "all",
        "dependencies": (
            "airline_fuel_consumed",
            "airline_capacity_units",
        ),
    },
    "fuel_cost_per_capacity_unit": {
        "mode": "all",
        "dependencies": (
            "airline_fuel_expense",
            "airline_capacity_units",
        ),
    },
    "aeronautical_revenue_per_passenger": {
        "mode": "all",
        "dependencies": (
            "airport_aeronautical_revenue",
            "airport_passenger_throughput",
        ),
    },
    "non_aeronautical_revenue_per_passenger": {
        "mode": "all",
        "dependencies": (
            "airport_non_aeronautical_revenue",
            "airport_passenger_throughput",
        ),
    },
    "fleet_capacity_growth": {
        "mode": "series",
        "dependencies": ("fleet_capacity",),
        "minimum_periods": 2,
    },
    "milestone_slippage_days": {
        "mode": "series",
        "dependencies": ("milestone_target_date",),
        "minimum_periods": 2,
    },
}

FINANCIAL_DERIVATIONS = {
    "pre_revenue_flag": "revenue_ttm_usd",
    "cash_runway_years": "cash_runway_years",
    "quarterly_cash_burn": "cash_burn_ttm_usd",
    "capital_raise_dependence": "capital_raise_dependence",
    "diluted_share_growth": "diluted_shares_yoy_growth",
    "stock_compensation_to_revenue": "sbc_pct_revenue",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {
                str(key): str(value or "").strip()
                for key, value in row.items()
            }
            for row in csv.DictReader(handle)
        ]


def read_only_connection(
    path: Path,
    *,
    timeout_sec: float = 120.0,
) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    connection = sqlite3.connect(
        f"{resolved.as_uri()}?mode=ro",
        uri=True,
        timeout=timeout_sec,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _empty_stats() -> dict[str, Any]:
    return {
        "text_hit_count": 0,
        "value_candidate_count": 0,
        "accepted_value_count": 0,
        "review_value_count": 0,
        "rejected_value_count": 0,
        "parser_failure_count": 0,
        "periods": set(),
        "accepted_periods": set(),
        "usable_periods": set(),
    }


def _target_date(provenance_json: str) -> str:
    try:
        payload = json.loads(provenance_json or "{}")
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, Mapping):
        return ""
    return str(payload.get("target_date") or "")[:10]


def load_evidence_stats(
    connection: sqlite3.Connection,
    *,
    run_id: int,
) -> dict[tuple[str, str], dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT evidence.ticker, evidence.metric_name,
               evidence.candidate_value, evidence.candidate_status,
               evidence.period_end, evidence.provenance_json
        FROM sec_parser_run_metric_evidence AS relation
        JOIN sec_parser_metric_evidence_shadow AS evidence
          ON evidence.evidence_key=relation.evidence_key
        WHERE relation.run_id=?
          AND evidence.model_family=?
        """,
        (run_id, MODEL_FAMILY),
    )
    return _accumulate_evidence_stats(rows)


def load_review_evidence_stats(
    connection: sqlite3.Connection,
    *,
    evaluation_id: int,
) -> dict[tuple[str, str], dict[str, Any]]:
    evaluation = connection.execute(
        """
        SELECT base_run_id, model_family, status,
               source_document_open_count, arelle_invocation_count,
               edgartools_invocation_count, ocr_invocation_count
        FROM sec_parser_review_evaluation
        WHERE evaluation_id=?
        """,
        (evaluation_id,),
    ).fetchone()
    if evaluation is None:
        raise ValueError(
            f"review evaluation_id={evaluation_id} does not exist"
        )
    if (
        str(evaluation["model_family"]) != MODEL_FAMILY
        or str(evaluation["status"]) != "COMPLETED"
        or any(
            int(evaluation[field] or 0) != 0
            for field in (
                "source_document_open_count",
                "arelle_invocation_count",
                "edgartools_invocation_count",
                "ocr_invocation_count",
            )
        )
    ):
        raise ValueError(
            f"review evaluation_id={evaluation_id} is not a valid "
            "zero-source-operation transportation evaluation"
        )
    rows = connection.execute(
        """
        SELECT ticker, metric_name, candidate_value, candidate_status,
               period_end, provenance_json
        FROM sec_parser_review_evidence
        WHERE evaluation_id=?
        """,
        (evaluation_id,),
    )
    return _accumulate_evidence_stats(rows)


def _accumulate_evidence_stats(
    rows: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    output: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        _empty_stats
    )
    for row in rows:
        key = (str(row["ticker"]), str(row["metric_name"]))
        stats = output[key]
        status = str(row["candidate_status"])
        if status == "PARSER_FAILURE":
            stats["parser_failure_count"] += 1
            continue
        stats["text_hit_count"] += 1
        target_date = (
            _target_date(str(row["provenance_json"] or ""))
            if key[1] == "milestone_target_date"
            else ""
        )
        has_value = row["candidate_value"] is not None or bool(target_date)
        if not has_value:
            continue
        stats["value_candidate_count"] += 1
        if status == "ACCEPTED":
            stats["accepted_value_count"] += 1
        elif status == "REVIEW_REQUIRED":
            stats["review_value_count"] += 1
        elif status == "REJECTED_POLICY":
            stats["rejected_value_count"] += 1
        period = target_date or str(row["period_end"] or "")[:10]
        if period:
            stats["periods"].add(period)
            if status == "ACCEPTED":
                stats["accepted_periods"].add(period)
                stats["usable_periods"].add(period)
            elif status == "REVIEW_REQUIRED":
                stats["usable_periods"].add(period)
    return dict(output)


def load_work_stats(
    connection: sqlite3.Connection,
    *,
    run_id: int,
) -> dict[str, dict[str, int]]:
    output: dict[str, dict[str, int]] = {}
    for row in connection.execute(
        """
        SELECT relation.ticker,
               COUNT(*) AS searched,
               SUM(CASE WHEN ledger.status='COMPLETED' THEN 1 ELSE 0 END)
                   AS completed,
               SUM(CASE WHEN ledger.status='FAILED' THEN 1 ELSE 0 END)
                   AS failed
        FROM sec_parser_run_work AS relation
        JOIN sec_parser_work_ledger AS ledger
          ON ledger.work_key=relation.work_key
        WHERE relation.run_id=?
        GROUP BY relation.ticker
        """,
        (run_id,),
    ):
        output[str(row["ticker"])] = {
            "searched": int(row["searched"] or 0),
            "completed": int(row["completed"] or 0),
            "failed": int(row["failed"] or 0),
        }
    return output


def load_financial_values(
    connection: sqlite3.Connection,
    *,
    asof_date: str,
) -> dict[str, dict[str, float | None]]:
    columns = set(FINANCIAL_DERIVATIONS.values())
    select_columns = ", ".join(sorted(columns))
    output: dict[str, dict[str, float | None]] = {}
    for row in connection.execute(
        f"""
        SELECT ticker, {select_columns}
        FROM feature_financial_statement AS feature
        WHERE model_family=?
          AND asof_date=(
            SELECT MAX(candidate.asof_date)
            FROM feature_financial_statement AS candidate
            WHERE candidate.model_family=feature.model_family
              AND candidate.ticker=feature.ticker
              AND candidate.asof_date<=?
          )
        """,
        (MODEL_FAMILY, asof_date),
    ):
        output[str(row["ticker"])] = {
            field: (
                float(row[field]) if row[field] is not None else None
            )
            for field in columns
        }
    return output


def _period_fields(stats: Mapping[str, Any]) -> tuple[int, str, str]:
    periods = sorted(str(value) for value in stats.get("periods", set()))
    return (
        len(periods),
        periods[0] if periods else "",
        periods[-1] if periods else "",
    )


def direct_status(
    stats: Mapping[str, Any],
    work: Mapping[str, int],
) -> str:
    if int(stats.get("accepted_value_count") or 0):
        return "COVERED_ACCEPTED"
    if int(stats.get("review_value_count") or 0):
        return "COVERED_REVIEW_REQUIRED"
    if int(stats.get("rejected_value_count") or 0):
        return "DISCOVERED_REJECTED"
    if int(stats.get("text_hit_count") or 0):
        return "TEXT_HIT_NO_VALUE"
    if int(stats.get("parser_failure_count") or 0):
        return "PARSER_FAILURE_ONLY"
    if int(work.get("failed") or 0):
        return "SEARCH_INCOMPLETE"
    return "SEARCHED_NOT_FOUND"


def _derived_stats(
    *,
    ticker: str,
    metric_id: str,
    evidence: Mapping[tuple[str, str], Mapping[str, Any]],
) -> tuple[dict[str, Any], str]:
    rule = PARSER_DERIVATIONS[metric_id]
    dependencies = tuple(str(value) for value in rule["dependencies"])
    inputs = [evidence.get((ticker, dependency), _empty_stats()) for dependency in dependencies]
    combined = _empty_stats()
    for stats in inputs:
        for field in (
            "text_hit_count",
            "value_candidate_count",
            "accepted_value_count",
            "review_value_count",
            "rejected_value_count",
            "parser_failure_count",
        ):
            combined[field] += int(stats.get(field) or 0)
        combined["periods"].update(stats.get("periods", set()))
        combined["accepted_periods"].update(
            stats.get("accepted_periods", set())
        )
        combined["usable_periods"].update(
            stats.get("usable_periods", set())
        )
    mode = str(rule["mode"])
    observed = [int(stats.get("value_candidate_count") or 0) > 0 for stats in inputs]
    accepted = [int(stats.get("accepted_value_count") or 0) > 0 for stats in inputs]
    usable = [
        int(stats.get("accepted_value_count") or 0)
        + int(stats.get("review_value_count") or 0)
        > 0
        for stats in inputs
    ]
    minimum_periods = int(rule.get("minimum_periods") or 1)
    if mode == "any":
        is_observed, is_accepted, is_usable = (
            any(observed),
            any(accepted),
            any(usable),
        )
    elif mode == "all":
        candidate_intersection = set.intersection(
            *(set(stats.get("periods", set())) for stats in inputs)
        )
        accepted_intersection = set.intersection(
            *(
                set(stats.get("accepted_periods", set()))
                for stats in inputs
            )
        )
        usable_intersection = set.intersection(
            *(
                set(stats.get("usable_periods", set()))
                for stats in inputs
            )
        )
        is_observed, is_accepted, is_usable = (
            all(observed) and bool(candidate_intersection),
            all(accepted) and bool(accepted_intersection),
            all(usable) and bool(usable_intersection),
        )
        combined["periods"] = candidate_intersection
        combined["accepted_periods"] = accepted_intersection
        combined["usable_periods"] = usable_intersection
    else:
        is_observed = (
            bool(observed and observed[0])
            and len(combined["periods"]) >= minimum_periods
        )
        is_accepted = (
            bool(accepted and accepted[0])
            and len(combined["periods"]) >= minimum_periods
        )
        is_usable = (
            bool(usable and usable[0])
            and len(combined["periods"]) >= minimum_periods
        )
    if not is_observed:
        combined["value_candidate_count"] = 0
        combined["accepted_value_count"] = 0
        combined["review_value_count"] = 0
    elif is_accepted:
        combined["accepted_value_count"] = 1
        combined["review_value_count"] = 0
    elif is_usable:
        combined["accepted_value_count"] = 0
        combined["review_value_count"] = 1
    return combined, f"{mode}:" + "|".join(dependencies)


def accepted_periods_for_final_metric(
    *,
    ticker: str,
    metric_id: str,
    source_lane: str,
    evidence: Mapping[tuple[str, str], Mapping[str, Any]],
) -> set[str]:
    if source_lane == "DP":
        return set(
            evidence.get((ticker, metric_id), {}).get(
                "accepted_periods",
                set(),
            )
        )
    if source_lane != "DP-D":
        return set()
    rule = PARSER_DERIVATIONS[metric_id]
    dependencies = tuple(str(value) for value in rule["dependencies"])
    periods = [
        set(
            evidence.get((ticker, dependency), {}).get(
                "accepted_periods",
                set(),
            )
        )
        for dependency in dependencies
    ]
    mode = str(rule["mode"])
    if mode == "any":
        return set().union(*periods)
    if mode == "all":
        return set.intersection(*periods) if periods else set()
    minimum_periods = int(rule.get("minimum_periods") or 1)
    return (
        periods[0]
        if periods and len(periods[0]) >= minimum_periods
        else set()
    )


def _financial_stats(
    *,
    ticker: str,
    metric_id: str,
    financial_values: Mapping[str, Mapping[str, float | None]],
) -> tuple[dict[str, Any], str]:
    stats = _empty_stats()
    field = FINANCIAL_DERIVATIONS[metric_id]
    value = financial_values.get(ticker, {}).get(field)
    if value is not None:
        stats["text_hit_count"] = 1
        stats["value_candidate_count"] = 1
        stats["accepted_value_count"] = 1
    return stats, f"existing_feature_financial_statement:{field}"


def build_final_coverage(
    *,
    run_id: int,
    scope_rows: Sequence[Mapping[str, str]],
    evidence: Mapping[tuple[str, str], Mapping[str, Any]],
    work: Mapping[str, Mapping[str, int]],
    financial_values: Mapping[str, Mapping[str, float | None]],
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for scope in scope_rows:
        ticker = scope["ticker"]
        metric_id = scope["metric_id"]
        lane = scope["source_lane"]
        applicability = scope["applicability_status"]
        ticker_work = work.get(
            ticker,
            {"searched": 0, "completed": 0, "failed": 0},
        )
        derivation_basis = "direct_parser_evidence"
        if applicability != "APPLICABLE":
            stats = _empty_stats()
            status = "NOT_APPLICABLE"
            derivation_basis = scope.get("applicability_reason", "")
        elif lane == "DP":
            stats = dict(evidence.get((ticker, metric_id), _empty_stats()))
            status = direct_status(stats, ticker_work)
        elif lane == "DP-D":
            stats, derivation_basis = _derived_stats(
                ticker=ticker,
                metric_id=metric_id,
                evidence=evidence,
            )
            status = direct_status(stats, ticker_work)
        elif lane == "FIN-D":
            stats, derivation_basis = _financial_stats(
                ticker=ticker,
                metric_id=metric_id,
                financial_values=financial_values,
            )
            status = (
                "COVERED_FINANCIAL_DERIVED"
                if stats["accepted_value_count"]
                else "FINANCIAL_INPUTS_MISSING"
            )
        else:
            raise ValueError(f"Unsupported source lane: {lane}")
        period_count, first_period, last_period = _period_fields(stats)
        output.append(
            {
                "run_id": run_id,
                "ticker": ticker,
                "universe_role": scope["universe_role"],
                "calibration_cohort": scope["calibration_cohort"],
                "industry": scope["industry"],
                "primary_archetype": scope["primary_archetype"],
                "metric_id": metric_id,
                "metric_pack": scope["metric_pack"],
                "source_lane": lane,
                "applicability_status": applicability,
                "coverage_status": status,
                "derivation_basis": derivation_basis,
                "searched_filing_count": ticker_work["searched"],
                "completed_filing_count": ticker_work["completed"],
                "failed_filing_count": ticker_work["failed"],
                "text_hit_count": stats["text_hit_count"],
                "value_candidate_count": stats["value_candidate_count"],
                "accepted_value_count": stats["accepted_value_count"],
                "review_value_count": stats["review_value_count"],
                "rejected_value_count": stats["rejected_value_count"],
                "parser_failure_count": stats["parser_failure_count"],
                "distinct_period_count": period_count,
                "first_period_end": first_period,
                "last_period_end": last_period,
            }
        )
    return output


def build_support_coverage(
    *,
    run_id: int,
    scope_rows: Sequence[Mapping[str, str]],
    evidence: Mapping[tuple[str, str], Mapping[str, Any]],
    work: Mapping[str, Mapping[str, int]],
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for scope in scope_rows:
        ticker = scope["ticker"]
        metric_id = scope["support_metric_id"]
        applicability = scope["applicability_status"]
        ticker_work = work.get(
            ticker,
            {"searched": 0, "completed": 0, "failed": 0},
        )
        stats = dict(evidence.get((ticker, metric_id), _empty_stats()))
        status = (
            direct_status(stats, ticker_work)
            if applicability == "APPLICABLE"
            else "NOT_APPLICABLE"
        )
        period_count, first_period, last_period = _period_fields(stats)
        output.append(
            {
                "run_id": run_id,
                "ticker": ticker,
                "universe_role": scope["universe_role"],
                "calibration_cohort": scope["calibration_cohort"],
                "primary_archetype": scope["primary_archetype"],
                "support_metric_id": metric_id,
                "consumer_metric_ids": scope["consumer_metric_ids"],
                "applicability_status": applicability,
                "coverage_status": status,
                "searched_filing_count": ticker_work["searched"],
                "text_hit_count": stats["text_hit_count"],
                "value_candidate_count": stats["value_candidate_count"],
                "accepted_value_count": stats["accepted_value_count"],
                "review_value_count": stats["review_value_count"],
                "rejected_value_count": stats["rejected_value_count"],
                "parser_failure_count": stats["parser_failure_count"],
                "distinct_period_count": period_count,
                "first_period_end": first_period,
                "last_period_end": last_period,
            }
        )
    return output


def _rates(rows: Sequence[Mapping[str, object]]) -> dict[str, float]:
    denominator = len(rows)
    if not denominator:
        return {
            "accepted": 0.0,
            "usable": 0.0,
            "discovered": 0.0,
        }
    accepted = sum(
        str(row["coverage_status"])
        in {"COVERED_ACCEPTED", "COVERED_FINANCIAL_DERIVED"}
        for row in rows
    )
    usable = accepted + sum(
        row["coverage_status"] == "COVERED_REVIEW_REQUIRED"
        for row in rows
    )
    discovered = usable + sum(
        row["coverage_status"]
        in {"DISCOVERED_REJECTED", "TEXT_HIT_NO_VALUE"}
        for row in rows
    )
    return {
        "accepted": accepted / denominator,
        "usable": usable / denominator,
        "discovered": discovered / denominator,
    }


def build_metric_summary(
    rows: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        if row["applicability_status"] == "APPLICABLE":
            grouped[str(row["metric_id"])].append(row)
    output: list[dict[str, object]] = []
    for metric_id, scoped in sorted(grouped.items()):
        counts = Counter(str(row["coverage_status"]) for row in scoped)
        rates = _rates(scoped)
        first = scoped[0]
        output.append(
            {
                "run_id": first["run_id"],
                "metric_id": metric_id,
                "metric_pack": first["metric_pack"],
                "source_lane": first["source_lane"],
                "applicable_ticker_count": len(scoped),
                "active_applicable_ticker_count": sum(
                    row["universe_role"] == "active" for row in scoped
                ),
                "inactive_applicable_ticker_count": sum(
                    row["universe_role"] != "active" for row in scoped
                ),
                "searched_ticker_count": sum(
                    int(str(row["searched_filing_count"])) > 0
                    or row["source_lane"] == "FIN-D"
                    for row in scoped
                ),
                "text_hit_ticker_count": sum(
                    int(str(row["text_hit_count"])) > 0 for row in scoped
                ),
                "value_candidate_ticker_count": sum(
                    int(str(row["value_candidate_count"])) > 0 for row in scoped
                ),
                "accepted_ticker_count": (
                    counts["COVERED_ACCEPTED"]
                    + counts["COVERED_FINANCIAL_DERIVED"]
                ),
                "review_ticker_count": counts[
                    "COVERED_REVIEW_REQUIRED"
                ],
                "rejected_only_ticker_count": counts[
                    "DISCOVERED_REJECTED"
                ],
                "parser_failure_only_ticker_count": counts[
                    "PARSER_FAILURE_ONLY"
                ],
                "searched_not_found_ticker_count": counts[
                    "SEARCHED_NOT_FOUND"
                ],
                "search_incomplete_ticker_count": counts[
                    "SEARCH_INCOMPLETE"
                ],
                "accepted_coverage_rate": rates["accepted"],
                "usable_coverage_rate": rates["usable"],
                "discovery_coverage_rate": rates["discovered"],
            }
        )
    return output


def build_cohort_summary(
    rows: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    grouped: dict[
        tuple[str, str],
        list[Mapping[str, object]],
    ] = defaultdict(list)
    for row in rows:
        if row["applicability_status"] == "APPLICABLE":
            grouped[
                (str(row["calibration_cohort"]), str(row["metric_id"]))
            ].append(row)
    output: list[dict[str, object]] = []
    for (cohort, metric_id), scoped in sorted(grouped.items()):
        rates = _rates(scoped)
        first = scoped[0]
        statuses = [str(row["coverage_status"]) for row in scoped]
        accepted = sum(
            status
            in {"COVERED_ACCEPTED", "COVERED_FINANCIAL_DERIVED"}
            for status in statuses
        )
        usable = accepted + statuses.count("COVERED_REVIEW_REQUIRED")
        discovered = (
            usable
            + statuses.count("DISCOVERED_REJECTED")
            + statuses.count("TEXT_HIT_NO_VALUE")
        )
        output.append(
            {
                "run_id": first["run_id"],
                "calibration_cohort": cohort,
                "metric_id": metric_id,
                "metric_pack": first["metric_pack"],
                "source_lane": first["source_lane"],
                "applicable_ticker_count": len(scoped),
                "accepted_ticker_count": accepted,
                "usable_ticker_count": usable,
                "discovered_ticker_count": discovered,
                "accepted_coverage_rate": rates["accepted"],
                "usable_coverage_rate": rates["usable"],
                "discovery_coverage_rate": rates["discovered"],
            }
        )
    return output


def write_coverage_artifacts(
    *,
    final_rows: Sequence[Mapping[str, object]],
    metric_rows: Sequence[Mapping[str, object]],
    cohort_rows: Sequence[Mapping[str, object]],
    support_rows: Sequence[Mapping[str, object]],
    final_path: Path,
    metric_path: Path,
    cohort_path: Path,
    support_path: Path,
    manifest_path: Path,
    run: Mapping[str, object],
    scope_path: Path,
    support_scope_path: Path,
) -> dict[str, object]:
    write_csv_atomic(final_path, FINAL_COVERAGE_FIELDS, final_rows)
    write_csv_atomic(metric_path, METRIC_SUMMARY_FIELDS, metric_rows)
    write_csv_atomic(cohort_path, COHORT_SUMMARY_FIELDS, cohort_rows)
    write_csv_atomic(
        support_path,
        SUPPORT_COVERAGE_FIELDS,
        support_rows,
    )
    applicable = [
        row
        for row in final_rows
        if row["applicability_status"] == "APPLICABLE"
    ]
    rates = _rates(applicable)
    status_counts = Counter(
        str(row["coverage_status"]) for row in applicable
    )
    newly_executed = int(str(run["completed_work_count"]))
    linked_completed = int(str(run.get("linked_completed_work_count") or 0))
    effective_completed = newly_executed + linked_completed
    effective_planned = max(
        int(str(run["planned_work_count"])),
        effective_completed,
    )
    payload: dict[str, object] = {
        "acceptance": (
            "PASS"
            if str(run["status"]) == "COMPLETED"
            and int(str(run["failed_work_count"])) == 0
            and len(final_rows) == 14_400
            and len(metric_rows) == 90
            and len(support_rows) == 1_120
            else "FAIL"
        ),
        "model_family": MODEL_FAMILY,
        "gate": "DP6_POST_SEARCH_COVERAGE_ONLY",
        "run_id": run["run_id"],
        "run_status": run["status"],
        "planned_work_count": effective_planned,
        "completed_work_count": effective_completed,
        "newly_executed_work_count": newly_executed,
        "linked_completed_work_count": linked_completed,
        "failed_work_count": run["failed_work_count"],
        "feature_build_invocations": 0,
        "calibration_invocations": 0,
        "final_metric_count": len(metric_rows),
        "final_scope_row_count": len(final_rows),
        "applicable_final_scope_row_count": len(applicable),
        "support_scope_row_count": len(support_rows),
        "coverage_status_counts": dict(sorted(status_counts.items())),
        "accepted_coverage_rate": rates["accepted"],
        "usable_coverage_rate": rates["usable"],
        "discovery_coverage_rate": rates["discovered"],
        "inputs": {
            "final_scope_sha256": file_sha256(scope_path),
            "support_scope_sha256": file_sha256(support_scope_path),
        },
        "artifacts": {},
        "next_gate": (
            "REVIEW_COVERAGE_AND_REDUCE_METRIC_SET"
            if str(run["status"]) == "COMPLETED"
            and int(str(run["failed_work_count"])) == 0
            else "RESUME_FAILED_PARSER_WORK_BEFORE_COVERAGE_REVIEW"
        ),
    }
    payload["artifacts"] = {
        "ticker_metric_coverage": {
            "path": str(final_path),
            "row_count": len(final_rows),
            "sha256": file_sha256(final_path),
        },
        "metric_summary": {
            "path": str(metric_path),
            "row_count": len(metric_rows),
            "sha256": file_sha256(metric_path),
        },
        "cohort_metric_summary": {
            "path": str(cohort_path),
            "row_count": len(cohort_rows),
            "sha256": file_sha256(cohort_path),
        },
        "support_metric_coverage": {
            "path": str(support_path),
            "row_count": len(support_rows),
            "sha256": file_sha256(support_path),
        },
    }
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    return payload


def load_run(
    connection: sqlite3.Connection,
    *,
    run_id: int,
) -> dict[str, object]:
    row = connection.execute(
        """
        SELECT run_id, model_family, asof_date, adapter_version, status,
               planned_work_count, completed_work_count, failed_work_count,
               metadata_json
        FROM sec_parser_run
        WHERE run_id=?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown dedicated-parser run_id={run_id}")
    result = dict(row)
    try:
        metadata = json.loads(str(result.pop("metadata_json") or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"run_id={run_id} has invalid metadata_json") from exc
    plan = metadata.get("plan") if isinstance(metadata, dict) else None
    result["linked_completed_work_count"] = (
        int(plan.get("linked_completed_work_count") or 0)
        if isinstance(plan, Mapping)
        else 0
    )
    if result["model_family"] != MODEL_FAMILY:
        raise ValueError(
            f"run_id={run_id} belongs to {result['model_family']!r}"
        )
    return result

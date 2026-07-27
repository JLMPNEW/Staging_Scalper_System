from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from dedicated_parser.contracts import AdapterRegistry, file_sha256, stable_hash
from industrials.machinery.financial_contract import required_metric_names
from industrials.machinery.historical_coverage import (
    COVERED_STATUSES,
    EXCLUDED_STATUSES,
    VALID_AVAILABILITY_STATUSES,
    load_validated_sidecar,
)


METRIC_DEPTH_FIELDS = [
    "metric",
    "category",
    "current_gate_status",
    "baseline_observation_count",
    "baseline_applicable_count",
    "baseline_covered_count",
    "baseline_coverage_fraction",
    "suppression_exposed_covered_count",
    "worst_case_covered_count",
    "worst_case_coverage_fraction",
    "worst_case_covered_ticker_count",
    "worst_case_delisted_ticker_count",
    "qualified_date_count",
    "qualified_year_count",
    "covered_cohort_count",
    "potential_recovery_upper_bound",
    "minimum_cross_section_count",
    "minimum_cross_section_fraction",
    "minimum_total_observations",
    "minimum_qualified_dates",
    "minimum_qualified_years",
    "minimum_delisted_tickers",
    "historical_status",
    "status_reason",
]
IMPACT_FIELDS = [
    "impact_id",
    "impact_kind",
    "promotion_ids",
    "run_ids",
    "ticker",
    "parser_metric",
    "canonical_metric",
    "period_start",
    "period_end",
    "effective_start_date",
    "effective_end_date",
    "affected_metrics",
    "affected_partition_count",
    "baseline_covered_exposure_count",
    "potential_recovery_upper_bound",
    "evidence_key",
    "policy_id",
]
PARTITION_FIELDS = [
    "asof_date",
    "affected_ticker_count",
    "affected_tickers",
    "impact_count",
    "impact_ids",
]
RANGE_FIELDS = [
    "range_number",
    "start_date",
    "end_date",
    "scheduled_partition_count",
    "affected_ticker_count",
    "affected_tickers",
]
CATEGORY_FIELDS = [
    "category",
    "calibration_metric_count",
    "production_candidate_count",
    "diagnostic_only_count",
    "production_candidates",
    "diagnostic_only_metrics",
    "status",
]


@dataclass(frozen=True)
class HistoricalDepthThresholds:
    minimum_total_observations: int = 500
    minimum_qualified_dates: int = 252
    minimum_qualified_years: int = 3
    minimum_delisted_tickers: int = 1


@dataclass(frozen=True)
class PromotionImpact:
    impact_id: str
    impact_kind: str
    promotion_ids: tuple[int, ...]
    run_ids: tuple[int, ...]
    ticker: str
    parser_metric: str
    canonical_metric: str
    period_start: str
    period_end: str
    effective_start_date: str
    effective_end_date: str
    affected_metrics: tuple[str, ...]
    evidence_key: str
    policy_id: str = ""


@dataclass
class MetricAccumulator:
    observations: int = 0
    applicable: int = 0
    covered: int = 0
    parser_failures: int = 0
    invalid_statuses: int = 0
    covered_by_ticker: Counter[str] | None = None
    covered_by_delisted_ticker: Counter[str] | None = None
    covered_by_date: Counter[str] | None = None
    applicable_by_date: Counter[str] | None = None
    covered_by_cohort: Counter[str] | None = None
    exposed_by_ticker: Counter[str] | None = None
    exposed_by_delisted_ticker: Counter[str] | None = None
    exposed_by_date: Counter[str] | None = None
    exposed_count: int = 0
    potential_recovery_count: int = 0

    def __post_init__(self) -> None:
        self.covered_by_ticker = Counter()
        self.covered_by_delisted_ticker = Counter()
        self.covered_by_date = Counter()
        self.applicable_by_date = Counter()
        self.covered_by_cohort = Counter()
        self.exposed_by_ticker = Counter()
        self.exposed_by_delisted_ticker = Counter()
        self.exposed_by_date = Counter()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _load_current_calibration_gates(
    path: Path,
) -> dict[str, dict[str, str]]:
    rows = _read_csv(path)
    output = {
        str(row["metric"]): row
        for row in rows
        if str(row.get("gate_mode") or "") == "calibration"
    }
    if not output:
        raise ValueError(f"No calibration metric gates found in {path}")
    missing = sorted(set(output) - set(required_metric_names()))
    if missing:
        raise ValueError(f"Unknown calibration metrics in coverage artifact: {missing}")
    return output


def _accepted_date(row: sqlite3.Row) -> str:
    return str(row["accepted_at"] or row["filing_date"] or "")[:10]


def _dependent_metrics(
    registry: AdapterRegistry,
    parser_metric: str,
) -> tuple[str, ...]:
    required = set(required_metric_names())
    metrics = {
        metric
        for metric, source_metric in registry.metric_dependencies.items()
        if source_metric == parser_metric and metric in required
    }
    if parser_metric in required:
        metrics.add(parser_metric)
    return tuple(sorted(metrics))


def _validate_promotions(
    conn: sqlite3.Connection,
    *,
    promotion_ids: tuple[int, ...],
    registry: AdapterRegistry,
    source_id: str,
) -> tuple[dict[int, sqlite3.Row], list[str]]:
    placeholders = ",".join("?" for _ in promotion_ids)
    rows = conn.execute(
        f"""
        SELECT *
        FROM sec_parser_production_promotion_run
        WHERE promotion_id IN ({placeholders})
        ORDER BY promotion_id
        """,
        promotion_ids,
    ).fetchall()
    by_id = {int(row["promotion_id"]): row for row in rows}
    errors: list[str] = []
    missing = sorted(set(promotion_ids) - set(by_id))
    if missing:
        errors.append(f"promotion ids not found={missing}")
    for promotion_id, row in by_id.items():
        if str(row["model_family"]) != registry.model_family:
            errors.append(
                f"promotion {promotion_id} model_family="
                f"{row['model_family']} expected={registry.model_family}"
            )
        if str(row["source_id"]) != source_id:
            errors.append(
                f"promotion {promotion_id} source_id="
                f"{row['source_id']} expected={source_id}"
            )
        if str(row["status"]) != "COMPLETED":
            errors.append(
                f"promotion {promotion_id} status={row['status']} is not COMPLETED"
            )
        metadata = json.loads(str(row["metadata_json"] or "{}"))
        if int(metadata.get("conflicting_evidence_count", 0) or 0):
            errors.append(f"promotion {promotion_id} contains conflicting evidence")
    return by_id, errors


def _load_promotion_impacts(
    conn: sqlite3.Connection,
    *,
    promotion_ids: tuple[int, ...],
    registry: AdapterRegistry,
    source_id: str,
) -> tuple[list[PromotionImpact], dict[str, object], list[str]]:
    promotions, errors = _validate_promotions(
        conn,
        promotion_ids=promotion_ids,
        registry=registry,
        source_id=source_id,
    )
    if not promotions:
        return [], {}, errors
    placeholders = ",".join("?" for _ in promotion_ids)
    evidence_rows = conn.execute(
        f"""
        SELECT
            promotion.promotion_id,
            promotion.run_id,
            relation.action,
            evidence.*
        FROM sec_parser_production_promotion_run AS promotion
        JOIN sec_parser_production_evidence AS relation
          ON relation.promotion_id = promotion.promotion_id
        JOIN sec_parser_metric_evidence_shadow AS evidence
          ON evidence.evidence_key = relation.evidence_key
        WHERE promotion.promotion_id IN ({placeholders})
        ORDER BY promotion.promotion_id, evidence.evidence_key
        """,
        promotion_ids,
    ).fetchall()
    evidence_by_key: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in evidence_rows:
        evidence_by_key[str(row["evidence_key"])].append(row)

    impacts: list[PromotionImpact] = []
    promoted_keys: set[str] = set()
    for evidence_key, rows in sorted(evidence_by_key.items()):
        promoted_rows = [
            row for row in rows if str(row["action"]) == "PROMOTED"
        ]
        if not promoted_rows:
            continue
        row = promoted_rows[-1]
        parser_metric = str(row["metric_name"])
        mapping = registry.production_mappings.get(parser_metric)
        if mapping is None:
            errors.append(
                f"promoted evidence {evidence_key} has no production mapping"
            )
            continue
        fact_key = stable_hash(
            {
                "source_id": source_id,
                "evidence_key": evidence_key,
                "canonical_metric": mapping.canonical_metric,
            }
        )
        current_fact = conn.execute(
            """
            SELECT mapped.raw_fact_id
            FROM fact_sec_xbrl_fact_raw AS raw
            JOIN fact_sec_xbrl_fact AS mapped
              ON mapped.raw_fact_id = raw.raw_fact_id
            WHERE raw.fact_key = ?
              AND mapped.source_id = ?
              AND mapped.canonical_metric = ?
            LIMIT 1
            """,
            (fact_key, source_id, mapping.canonical_metric),
        ).fetchone()
        if current_fact is None:
            errors.append(
                f"promoted evidence is absent from current production facts: "
                f"{evidence_key}"
            )
            continue
        promoted_keys.add(evidence_key)
        impacts.append(
            PromotionImpact(
                impact_id=f"fact:{evidence_key}",
                impact_kind="PROMOTED_FACT",
                promotion_ids=tuple(
                    sorted({int(item["promotion_id"]) for item in promoted_rows})
                ),
                run_ids=tuple(
                    sorted({int(item["run_id"]) for item in promoted_rows})
                ),
                ticker=str(row["ticker"]),
                parser_metric=parser_metric,
                canonical_metric=mapping.canonical_metric,
                period_start=str(row["period_start"] or "")[:10],
                period_end=str(row["period_end"] or "")[:10],
                effective_start_date=_accepted_date(row),
                effective_end_date="",
                affected_metrics=_dependent_metrics(registry, parser_metric),
                evidence_key=evidence_key,
            )
        )

    selected_evidence_keys = tuple(sorted(evidence_by_key))
    if selected_evidence_keys:
        evidence_placeholders = ",".join("?" for _ in selected_evidence_keys)
        suppressions = conn.execute(
            f"""
            SELECT *
            FROM sec_parser_production_suppression
            WHERE active = 1
              AND model_family = ?
              AND evidence_key IN ({evidence_placeholders})
            ORDER BY suppression_id
            """,
            (registry.model_family, *selected_evidence_keys),
        ).fetchall()
        overrides = conn.execute(
            f"""
            SELECT *
            FROM sec_parser_production_metric_override
            WHERE active = 1
              AND model_family = ?
              AND evidence_key IN ({evidence_placeholders})
            ORDER BY ticker, metric_name, evidence_key
            """,
            (registry.model_family, *selected_evidence_keys),
        ).fetchall()
    else:
        suppressions = []
        overrides = []

    for row in suppressions:
        evidence_key = str(row["evidence_key"])
        evidence = evidence_by_key[evidence_key][-1]
        parser_metric = str(evidence["metric_name"])
        impacts.append(
            PromotionImpact(
                impact_id=f"suppression:{row['suppression_id']}",
                impact_kind="SUPPRESSION",
                promotion_ids=tuple(
                    sorted(
                        {
                            int(item["promotion_id"])
                            for item in evidence_by_key[evidence_key]
                        }
                    )
                ),
                run_ids=tuple(
                    sorted(
                        {
                            int(item["run_id"])
                            for item in evidence_by_key[evidence_key]
                        }
                    )
                ),
                ticker=str(row["ticker"]),
                parser_metric=parser_metric,
                canonical_metric=str(row["canonical_metric"]),
                period_start=str(row["period_start"] or "")[:10],
                period_end=str(row["period_end"] or "")[:10],
                effective_start_date=str(row["valid_from"] or "")[:10],
                effective_end_date=str(row["valid_to"] or "")[:10],
                affected_metrics=_dependent_metrics(registry, parser_metric),
                evidence_key=evidence_key,
                policy_id=str(row["policy_id"] or ""),
            )
        )
    for row in overrides:
        evidence_key = str(row["evidence_key"])
        evidence = evidence_by_key[evidence_key][-1]
        metric_name = str(row["metric_name"])
        impacts.append(
            PromotionImpact(
                impact_id=(
                    f"override:{row['ticker']}:{row['metric_name']}:"
                    f"{evidence_key}"
                ),
                impact_kind="STRUCTURAL_OVERRIDE",
                promotion_ids=tuple(
                    sorted(
                        {
                            int(item["promotion_id"])
                            for item in evidence_by_key[evidence_key]
                        }
                    )
                ),
                run_ids=tuple(
                    sorted(
                        {
                            int(item["run_id"])
                            for item in evidence_by_key[evidence_key]
                        }
                    )
                ),
                ticker=str(row["ticker"]),
                parser_metric=str(evidence["metric_name"]),
                canonical_metric=metric_name,
                period_start="",
                period_end="",
                effective_start_date=str(row["valid_from"] or "")[:10],
                effective_end_date=str(row["valid_to"] or "")[:10],
                affected_metrics=(metric_name,),
                evidence_key=evidence_key,
                policy_id="structural_override",
            )
        )

    summary = {
        "selected_promotion_count": len(promotions),
        "selected_promotion_ids": list(promotion_ids),
        "selected_run_ids": sorted(
            {int(row["run_id"]) for row in promotions.values()}
        ),
        "selected_candidate_count": sum(
            int(row["candidate_count"]) for row in promotions.values()
        ),
        "selected_promoted_evidence_count": len(promoted_keys),
        "selected_active_suppression_count": len(suppressions),
        "selected_active_structural_override_count": len(overrides),
    }
    return impacts, summary, errors


def _within_impact(impact: PromotionImpact, asof_date: str) -> bool:
    if not impact.effective_start_date or asof_date < impact.effective_start_date:
        return False
    return not impact.effective_end_date or asof_date <= impact.effective_end_date


def _positive_counter_keys(
    baseline: Counter[str],
    exposed: Counter[str],
) -> set[str]:
    return {
        key
        for key, count in baseline.items()
        if count - exposed.get(key, 0) > 0
    }


def _safe_fraction(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator > 0 else 0.0


def _compress_affected_ranges(
    scheduled_dates: list[str],
    affected_dates: set[str],
    partition_tickers: dict[str, set[str]],
) -> list[dict[str, object]]:
    selected_indexes = [
        index
        for index, asof_date in enumerate(scheduled_dates)
        if asof_date in affected_dates
    ]
    if not selected_indexes:
        return []
    groups: list[list[int]] = [[selected_indexes[0]]]
    for index in selected_indexes[1:]:
        if index == groups[-1][-1] + 1:
            groups[-1].append(index)
        else:
            groups.append([index])
    rows: list[dict[str, object]] = []
    for range_number, indexes in enumerate(groups, start=1):
        dates = [scheduled_dates[index] for index in indexes]
        tickers = sorted(
            {
                ticker
                for asof_date in dates
                for ticker in partition_tickers[asof_date]
            }
        )
        rows.append(
            {
                "range_number": range_number,
                "start_date": dates[0],
                "end_date": dates[-1],
                "scheduled_partition_count": len(dates),
                "affected_ticker_count": len(tickers),
                "affected_tickers": ",".join(tickers),
            }
        )
    return rows


def run_historical_promotion_preflight(
    conn: sqlite3.Connection,
    *,
    promotion_ids: tuple[int, ...],
    registry: AdapterRegistry,
    source_id: str,
    current_coverage_csv: Path,
    historical_summary_json: Path,
    dashboard_root: Path,
    thresholds: HistoricalDepthThresholds,
) -> dict[str, object]:
    if not promotion_ids:
        raise ValueError("At least one explicit promotion id is required")
    if thresholds.minimum_total_observations <= 0:
        raise ValueError("minimum_total_observations must be positive")
    if thresholds.minimum_qualified_dates <= 0:
        raise ValueError("minimum_qualified_dates must be positive")
    if thresholds.minimum_qualified_years <= 0:
        raise ValueError("minimum_qualified_years must be positive")
    if thresholds.minimum_delisted_tickers < 0:
        raise ValueError("minimum_delisted_tickers cannot be negative")
    current_gates = _load_current_calibration_gates(current_coverage_csv)
    with historical_summary_json.open("r", encoding="utf-8") as handle:
        historical_summary = json.load(handle)
    impacts, promotion_summary, errors = _load_promotion_impacts(
        conn,
        promotion_ids=promotion_ids,
        registry=registry,
        source_id=source_id,
    )
    if str(historical_summary.get("acceptance") or "") != "PASS":
        errors.append("existing combined historical coverage does not pass")
    start_date = str(historical_summary.get("start_date") or "")
    end_date = str(historical_summary.get("end_date") or "")
    scheduled_dates = sorted(
        path.name
        for path in dashboard_root.iterdir()
        if path.is_dir()
        and start_date <= path.name <= end_date
        and (
            path / "machinery_stage11_survivorship_calibration_panel.csv"
        ).exists()
    )
    expected_date_count = int(
        historical_summary.get("scheduled_date_count", 0) or 0
    )
    if len(scheduled_dates) != expected_date_count:
        errors.append(
            "historical sidecar count mismatch "
            f"expected={expected_date_count} actual={len(scheduled_dates)}"
        )

    metrics = tuple(sorted(current_gates))
    accumulators = {
        metric: MetricAccumulator()
        for metric in metrics
    }
    impacts_by_ticker: dict[str, list[PromotionImpact]] = defaultdict(list)
    for impact in impacts:
        impacts_by_ticker[impact.ticker].append(impact)
    affected_dates: set[str] = set()
    partition_tickers: dict[str, set[str]] = defaultdict(set)
    partition_impacts: dict[str, set[str]] = defaultdict(set)
    impact_dates: dict[str, set[str]] = defaultdict(set)
    impact_exposures: Counter[str] = Counter()
    impact_potential: Counter[str] = Counter()

    for asof_date in scheduled_dates:
        rows = load_validated_sidecar(dashboard_root / asof_date, asof=asof_date)
        for row in rows:
            ticker = str(row["ticker"])
            cohort = str(row.get("calibration_cohort") or "unclassified")
            delisted = (
                str(row.get("membership_status") or "")
                == "historical_delisted"
            )
            active_impacts = [
                impact
                for impact in impacts_by_ticker.get(ticker, ())
                if _within_impact(impact, asof_date)
            ]
            if active_impacts:
                affected_dates.add(asof_date)
                partition_tickers[asof_date].add(ticker)
                for impact in active_impacts:
                    partition_impacts[asof_date].add(impact.impact_id)
                    impact_dates[impact.impact_id].add(asof_date)
            negative_metrics: dict[str, set[str]] = defaultdict(set)
            positive_metrics: dict[str, set[str]] = defaultdict(set)
            for impact in active_impacts:
                target = (
                    positive_metrics
                    if impact.impact_kind == "PROMOTED_FACT"
                    else negative_metrics
                )
                for metric in impact.affected_metrics:
                    if metric in accumulators:
                        target[metric].add(impact.impact_id)

            for metric, accumulator in accumulators.items():
                status = str(
                    row.get(f"{metric}_availability_status") or ""
                )
                accumulator.observations += 1
                if status not in VALID_AVAILABILITY_STATUSES:
                    accumulator.invalid_statuses += 1
                    continue
                if status == "PARSER_FAILURE":
                    accumulator.parser_failures += 1
                applicable = status not in EXCLUDED_STATUSES
                covered = status in COVERED_STATUSES
                if applicable:
                    accumulator.applicable += 1
                    assert accumulator.applicable_by_date is not None
                    accumulator.applicable_by_date[asof_date] += 1
                if covered:
                    accumulator.covered += 1
                    assert accumulator.covered_by_ticker is not None
                    assert accumulator.covered_by_date is not None
                    assert accumulator.covered_by_cohort is not None
                    accumulator.covered_by_ticker[ticker] += 1
                    accumulator.covered_by_date[asof_date] += 1
                    accumulator.covered_by_cohort[cohort] += 1
                    if delisted:
                        assert (
                            accumulator.covered_by_delisted_ticker is not None
                        )
                        accumulator.covered_by_delisted_ticker[ticker] += 1
                if covered and negative_metrics.get(metric):
                    accumulator.exposed_count += 1
                    assert accumulator.exposed_by_ticker is not None
                    assert accumulator.exposed_by_date is not None
                    accumulator.exposed_by_ticker[ticker] += 1
                    accumulator.exposed_by_date[asof_date] += 1
                    if delisted:
                        assert (
                            accumulator.exposed_by_delisted_ticker is not None
                        )
                        accumulator.exposed_by_delisted_ticker[ticker] += 1
                    for impact_id in negative_metrics[metric]:
                        impact_exposures[impact_id] += 1
                if (
                    applicable
                    and not covered
                    and positive_metrics.get(metric)
                ):
                    accumulator.potential_recovery_count += 1
                    for impact_id in positive_metrics[metric]:
                        impact_potential[impact_id] += 1

    metric_rows: list[dict[str, object]] = []
    metrics_by_category: dict[str, list[dict[str, object]]] = defaultdict(list)
    for metric in metrics:
        accumulator = accumulators[metric]
        gate = current_gates[metric]
        minimum_count = int(gate["minimum_count"])
        minimum_fraction = float(gate["minimum_fraction"])
        assert accumulator.covered_by_ticker is not None
        assert accumulator.covered_by_delisted_ticker is not None
        assert accumulator.covered_by_date is not None
        assert accumulator.applicable_by_date is not None
        assert accumulator.covered_by_cohort is not None
        assert accumulator.exposed_by_ticker is not None
        assert accumulator.exposed_by_delisted_ticker is not None
        assert accumulator.exposed_by_date is not None
        worst_covered = accumulator.covered - accumulator.exposed_count
        worst_tickers = _positive_counter_keys(
            accumulator.covered_by_ticker,
            accumulator.exposed_by_ticker,
        )
        worst_delisted = _positive_counter_keys(
            accumulator.covered_by_delisted_ticker,
            accumulator.exposed_by_delisted_ticker,
        )
        qualified_dates: set[str] = set()
        for asof_date, baseline_covered in accumulator.covered_by_date.items():
            covered_count = (
                baseline_covered
                - accumulator.exposed_by_date.get(asof_date, 0)
            )
            applicable_count = accumulator.applicable_by_date.get(
                asof_date,
                0,
            )
            if (
                covered_count >= minimum_count
                and _safe_fraction(covered_count, applicable_count)
                >= minimum_fraction
            ):
                qualified_dates.add(asof_date)
        qualified_years = {asof_date[:4] for asof_date in qualified_dates}
        reasons: list[str] = []
        if str(gate["status"]) != "CALIBRATION_READY":
            reasons.append("current_gate_not_ready")
        if accumulator.parser_failures:
            reasons.append("historical_parser_failures")
        if accumulator.invalid_statuses:
            reasons.append("historical_unclassified_statuses")
        if worst_covered < thresholds.minimum_total_observations:
            reasons.append("insufficient_total_observations")
        if len(worst_tickers) < minimum_count:
            reasons.append("insufficient_distinct_tickers")
        if len(qualified_dates) < thresholds.minimum_qualified_dates:
            reasons.append("insufficient_qualified_dates")
        if len(qualified_years) < thresholds.minimum_qualified_years:
            reasons.append("insufficient_qualified_years")
        if (
            len(worst_delisted)
            < thresholds.minimum_delisted_tickers
        ):
            reasons.append("insufficient_delisted_coverage")
        historical_status = (
            "PRODUCTION_CANDIDATE" if not reasons else "DIAGNOSTIC_ONLY"
        )
        row = {
            "metric": metric,
            "category": str(gate["category"]),
            "current_gate_status": str(gate["status"]),
            "baseline_observation_count": accumulator.observations,
            "baseline_applicable_count": accumulator.applicable,
            "baseline_covered_count": accumulator.covered,
            "baseline_coverage_fraction": (
                f"{_safe_fraction(accumulator.covered, accumulator.applicable):.8f}"
            ),
            "suppression_exposed_covered_count": accumulator.exposed_count,
            "worst_case_covered_count": worst_covered,
            "worst_case_coverage_fraction": (
                f"{_safe_fraction(worst_covered, accumulator.applicable):.8f}"
            ),
            "worst_case_covered_ticker_count": len(worst_tickers),
            "worst_case_delisted_ticker_count": len(worst_delisted),
            "qualified_date_count": len(qualified_dates),
            "qualified_year_count": len(qualified_years),
            "covered_cohort_count": len(accumulator.covered_by_cohort),
            "potential_recovery_upper_bound": (
                accumulator.potential_recovery_count
            ),
            "minimum_cross_section_count": minimum_count,
            "minimum_cross_section_fraction": f"{minimum_fraction:.8f}",
            "minimum_total_observations": (
                thresholds.minimum_total_observations
            ),
            "minimum_qualified_dates": thresholds.minimum_qualified_dates,
            "minimum_qualified_years": thresholds.minimum_qualified_years,
            "minimum_delisted_tickers": thresholds.minimum_delisted_tickers,
            "historical_status": historical_status,
            "status_reason": ",".join(reasons) if reasons else "ok",
        }
        metric_rows.append(row)
        metrics_by_category[str(gate["category"])].append(row)

    category_rows: list[dict[str, object]] = []
    failed_categories: list[str] = []
    for category, rows in sorted(metrics_by_category.items()):
        candidates = sorted(
            str(row["metric"])
            for row in rows
            if row["historical_status"] == "PRODUCTION_CANDIDATE"
        )
        diagnostics = sorted(
            str(row["metric"])
            for row in rows
            if row["historical_status"] == "DIAGNOSTIC_ONLY"
        )
        status = "PASS" if candidates else "FAIL"
        if status == "FAIL":
            failed_categories.append(category)
        category_rows.append(
            {
                "category": category,
                "calibration_metric_count": len(rows),
                "production_candidate_count": len(candidates),
                "diagnostic_only_count": len(diagnostics),
                "production_candidates": ",".join(candidates),
                "diagnostic_only_metrics": ",".join(diagnostics),
                "status": status,
            }
        )

    current_not_ready = sorted(
        metric
        for metric, gate in current_gates.items()
        if str(gate["status"]) != "CALIBRATION_READY"
    )
    if current_not_ready:
        errors.append(f"current calibration gates not ready={current_not_ready}")
    if failed_categories:
        errors.append(
            f"no production-capable historical metric in categories="
            f"{failed_categories}"
        )

    impact_rows: list[dict[str, object]] = []
    for impact in impacts:
        impact_rows.append(
            {
                "impact_id": impact.impact_id,
                "impact_kind": impact.impact_kind,
                "promotion_ids": ",".join(
                    str(item) for item in impact.promotion_ids
                ),
                "run_ids": ",".join(str(item) for item in impact.run_ids),
                "ticker": impact.ticker,
                "parser_metric": impact.parser_metric,
                "canonical_metric": impact.canonical_metric,
                "period_start": impact.period_start,
                "period_end": impact.period_end,
                "effective_start_date": impact.effective_start_date,
                "effective_end_date": impact.effective_end_date,
                "affected_metrics": ",".join(impact.affected_metrics),
                "affected_partition_count": len(
                    impact_dates[impact.impact_id]
                ),
                "baseline_covered_exposure_count": impact_exposures[
                    impact.impact_id
                ],
                "potential_recovery_upper_bound": impact_potential[
                    impact.impact_id
                ],
                "evidence_key": impact.evidence_key,
                "policy_id": impact.policy_id,
            }
        )
    partition_rows = [
        {
            "asof_date": asof_date,
            "affected_ticker_count": len(partition_tickers[asof_date]),
            "affected_tickers": ",".join(
                sorted(partition_tickers[asof_date])
            ),
            "impact_count": len(partition_impacts[asof_date]),
            "impact_ids": ",".join(sorted(partition_impacts[asof_date])),
        }
        for asof_date in sorted(affected_dates)
    ]
    range_rows = _compress_affected_ranges(
        scheduled_dates,
        affected_dates,
        partition_tickers,
    )
    if errors:
        decision = "BLOCK_REBUILD"
        acceptance = "FAIL"
    elif not affected_dates:
        decision = "NO_REBUILD_REQUIRED"
        acceptance = "PASS"
    elif len(affected_dates) < len(scheduled_dates):
        decision = "GO_AFFECTED_PARTITIONS_ONLY"
        acceptance = "PASS"
    else:
        decision = "GO_FULL_REBUILD"
        acceptance = "PASS"

    summary = {
        "acceptance": acceptance,
        "decision": decision,
        "errors": errors,
        "historical_start_date": start_date,
        "historical_end_date": end_date,
        "validated_historical_partition_count": len(scheduled_dates),
        "affected_partition_count": len(affected_dates),
        "unaffected_partition_count": (
            len(scheduled_dates) - len(affected_dates)
        ),
        "affected_partition_fraction": _safe_fraction(
            len(affected_dates),
            len(scheduled_dates),
        ),
        "affected_ticker_count": len(
            {
                ticker
                for tickers in partition_tickers.values()
                for ticker in tickers
            }
        ),
        "affected_tickers": sorted(
            {
                ticker
                for tickers in partition_tickers.values()
                for ticker in tickers
            }
        ),
        "affected_range_count": len(range_rows),
        "production_candidate_metric_count": sum(
            row["historical_status"] == "PRODUCTION_CANDIDATE"
            for row in metric_rows
        ),
        "diagnostic_only_metric_count": sum(
            row["historical_status"] == "DIAGNOSTIC_ONLY"
            for row in metric_rows
        ),
        "diagnostic_only_metrics": sorted(
            str(row["metric"])
            for row in metric_rows
            if row["historical_status"] == "DIAGNOSTIC_ONLY"
        ),
        "category_gate_count": len(category_rows),
        "category_gate_pass_count": sum(
            row["status"] == "PASS" for row in category_rows
        ),
        "full_rebuild_required": len(affected_dates) == len(scheduled_dates),
        "publication_performed": False,
        "projection_policy": (
            "existing-panel lower bound; no unmaterialized promotion gain "
            "credited; all suppression/override exposures removed"
        ),
        "thresholds": {
            "minimum_total_observations": (
                thresholds.minimum_total_observations
            ),
            "minimum_qualified_dates": thresholds.minimum_qualified_dates,
            "minimum_qualified_years": thresholds.minimum_qualified_years,
            "minimum_delisted_tickers": thresholds.minimum_delisted_tickers,
        },
        **promotion_summary,
    }
    summary["input_fingerprint"] = stable_hash(
        {
            "promotion_ids": promotion_ids,
            "promotion_impacts": [
                {
                    "impact_id": impact.impact_id,
                    "kind": impact.impact_kind,
                    "ticker": impact.ticker,
                    "metric": impact.parser_metric,
                    "start": impact.effective_start_date,
                    "end": impact.effective_end_date,
                    "affected_metrics": impact.affected_metrics,
                }
                for impact in impacts
            ],
            "current_coverage_sha256": file_sha256(current_coverage_csv),
            "historical_summary_sha256": file_sha256(
                historical_summary_json
            ),
            "historical_dates": {
                "first": scheduled_dates[0] if scheduled_dates else "",
                "last": scheduled_dates[-1] if scheduled_dates else "",
                "count": len(scheduled_dates),
            },
            "thresholds": summary["thresholds"],
        }
    )
    return {
        "summary": summary,
        "metric_rows": metric_rows,
        "category_rows": category_rows,
        "impact_rows": impact_rows,
        "partition_rows": partition_rows,
        "range_rows": range_rows,
    }

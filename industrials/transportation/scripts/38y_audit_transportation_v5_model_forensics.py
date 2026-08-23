#!/usr/bin/env python3
"""Independently audit v5 score mechanics and research-only model attribution."""
from __future__ import annotations

import argparse
import csv
import html
import json
import math
import random
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.oos_research import spearman  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.contracts import file_sha256  # noqa: E402
from industrials.transportation.financial_contract import load_metric_registry  # noqa: E402
from industrials.transportation.surface_freight_score_engine import load_cohort_score_policy  # noqa: E402


ROOT = PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v5"
DEFAULT_PANEL_DIR = ROOT / "outcome_panel_v6" / "2026-08-16"
DEFAULT_VALIDATION = ROOT / "outcome_validation_v6" / "2026-08-16" / "transportation_v5_outcome_panel_validation.json"
DEFAULT_PROTOCOL = ROOT / "research_protocol_v6" / "2026-08-16" / "transportation_v5_research_protocol.json"
DEFAULT_DIAGNOSTIC_DIR = ROOT / "diagnostic_calibration_v6" / "2026-08-16"
DEFAULT_REGISTRY = PROJECT_ROOT / "industrials" / "transportation" / "data" / "transportation_metric_registry.yaml"
DEFAULT_OUTPUT_DIR = ROOT / "model_forensic_audit_v7" / "2026-08-21"

OBSERVED = frozenset({"REPORTED", "DERIVED", "PROXY"})
COMPONENTS = (
    "market_trend_score", "quality_score", "growth_score", "valuation_score",
    "operating_efficiency_score", "capital_risk_score", "positioning_score",
    "baseline_final_score",
)
CORE_COMPONENTS = COMPONENTS[:6]
TOLERANCE = 1e-10
COMPONENT_FIELDS = (
    "cohort_id", "evaluation_block", "component", "snapshot_count", "mean_ic",
    "median_ic", "positive_ic_fraction", "non_overlapping_count",
    "non_overlapping_mean_ic", "hac_t_stat", "neutral_50_fraction",
    "average_cross_section_std",
)
METRIC_FIELDS = (
    "cohort_id", "metric_id", "comparison_domain", "applicable_rows",
    "observed_rows", "coverage", "ic_dates", "average_names_per_ic_date",
    "mean_directional_ic", "median_directional_ic", "positive_ic_fraction",
    "block_1_ic", "block_2_ic", "block_3_ic", "research_disposition",
)
ABLATION_FIELDS = (
    "cohort_id", "candidate_id", "selection_method", "snapshot_count", "mean_ic",
    "non_overlapping_count", "non_overlapping_mean_ic", "mean_top_excess_net",
    "mean_cohort_excess", "mean_top_minus_cohort_net",
    "mean_top_minus_bottom_gross", "top_excess_hit_rate", "average_turnover",
    "permutation_p_value", "evidence_role",
)
ATTRIBUTION_FIELDS = (
    "cohort_id", "candidate_id", "comparison_group", "available_rows",
    "selected_slots", "universe_row_share", "selected_slot_share",
    "dates_represented", "total_dates",
)
TIEOUT_FIELDS = ("check_id", "scope", "expected", "actual", "absolute_error", "status")
FINDING_FIELDS = (
    "severity", "finding_type", "category", "location", "finding",
    "decision_impact", "recommended_fix", "owner",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panel-dir", type=Path, default=DEFAULT_PANEL_DIR)
    parser.add_argument("--outcome-validation", type=Path, default=DEFAULT_VALIDATION)
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--diagnostic-dir", type=Path, default=DEFAULT_DIAGNOSTIC_DIR)
    parser.add_argument("--metric-registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--permutations", type=int, default=1000)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def number(value: object) -> float | None:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def mean(values: Iterable[float | None]) -> float | None:
    clean = [float(item) for item in values if item is not None and math.isfinite(float(item))]
    return sum(clean) / len(clean) if clean else None


def median(values: Iterable[float | None]) -> float | None:
    clean = [float(item) for item in values if item is not None and math.isfinite(float(item))]
    return statistics.median(clean) if clean else None


def parse_date(value: object) -> date:
    return date.fromisoformat(str(value)[:10])


def weighted_score(row: Mapping[str, object], weights: Mapping[str, float]) -> float | None:
    pairs = [
        (number(row.get(field)), weight)
        for field, weight in weights.items()
        if weight > 0
    ]
    pairs = [(value, weight) for value, weight in pairs if value is not None]
    total = sum(weight for _, weight in pairs)
    return sum(float(value) * weight for value, weight in pairs) / total if total else None


def fixed_block(asof: str, calendar_blocks: Sequence[Mapping[str, object]]) -> str:
    matches = [
        str(item["block_id"])
        for item in calendar_blocks
        if str(item["start_date"]) <= asof <= str(item["end_date"])
    ]
    if len(matches) != 1:
        raise ValueError(f"{asof}: expected one calendar block; got {matches}")
    return matches[0]


def hac_t_stat(values: Sequence[float], *, lag: int = 3) -> float | None:
    clean = [float(item) for item in values if math.isfinite(float(item))]
    count = len(clean)
    if count < 4:
        return None
    center = sum(clean) / count
    residuals = [item - center for item in clean]
    long_run = sum(item * item for item in residuals) / count
    for offset in range(1, min(lag, count - 1) + 1):
        covariance = sum(
            residuals[index] * residuals[index - offset]
            for index in range(offset, count)
        ) / count
        long_run += 2.0 * (1.0 - offset / (lag + 1.0)) * covariance
    variance_mean = max(0.0, long_run / count)
    return center / math.sqrt(variance_mean) if variance_mean > 0 else None


def non_overlapping(period_rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    selected: list[dict[str, object]] = []
    last_exit: date | None = None
    for row in sorted(period_rows, key=lambda item: str(item["asof_date"])):
        asof = parse_date(row["asof_date"])
        exit_date = parse_date(row["exit_date"])
        if last_exit is not None and asof <= last_exit:
            continue
        selected.append(dict(row))
        last_exit = exit_date
    return selected


def hit_rate(values: Iterable[float | None]) -> float | None:
    clean = [float(item) for item in values if item is not None and math.isfinite(float(item))]
    return sum(item > 0 for item in clean) / len(clean) if clean else None


def summarize_periods(period_rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    non_overlap = non_overlapping(period_rows)
    return {
        "snapshot_count": len(period_rows),
        "mean_ic": mean(number(row.get("ic")) for row in period_rows),
        "non_overlapping_count": len(non_overlap),
        "non_overlapping_mean_ic": mean(number(row.get("ic")) for row in non_overlap),
        "mean_top_excess_net": mean(number(row.get("net_excess")) for row in period_rows),
        "mean_cohort_excess": mean(number(row.get("cohort_excess")) for row in period_rows),
        "mean_top_minus_cohort_net": mean(
            number(row.get("top_minus_cohort_net")) for row in period_rows
        ),
        "mean_top_minus_bottom_gross": mean(
            number(row.get("top_minus_bottom_gross")) for row in period_rows
        ),
        "top_excess_hit_rate": hit_rate(number(row.get("net_excess")) for row in period_rows),
        "average_turnover": mean(number(row.get("turnover")) for row in period_rows),
    }


def independent_candidate(
    rows: Sequence[Mapping[str, object]],
    *,
    cohort_id: str,
    weights: Mapping[str, float],
    horizon_sessions: int,
    minimum_cross_section: int,
    top_fraction: float,
    transaction_cost_bps: float,
    group_by_ticker: Mapping[str, str] | None = None,
    group_neutral: bool = False,
) -> dict[str, object]:
    by_date: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        if str(row.get("calibration_cohort") or "") != cohort_id:
            continue
        if int(float(str(row.get("horizon_sessions") or 0))) != horizon_sessions:
            continue
        if str(row.get("calibration_eligible_flag") or "") != "1":
            continue
        if str(row.get("outcome_available_flag") or "") != "1":
            continue
        outcome = number(row.get("forward_excess_return"))
        score = weighted_score(row, weights)
        if score is None or outcome is None:
            continue
        ticker = str(row.get("ticker") or "")
        by_date[str(row.get("asof_date") or "")].append(
            {
                "ticker": ticker,
                "score": score,
                "outcome": outcome,
                "exit_date": str(row.get("benchmark_exit_date") or row.get("exit_date") or ""),
                "group": (group_by_ticker or {}).get(ticker, "unmapped"),
            }
        )

    periods: list[dict[str, object]] = []
    selected_groups: Counter[str] = Counter()
    universe_groups: Counter[str] = Counter()
    selected_group_dates: dict[str, set[str]] = defaultdict(set)
    previous: set[str] = set()
    for asof in sorted(by_date):
        values = by_date[asof]
        if len(values) < minimum_cross_section:
            continue
        count = max(1, math.ceil(len(values) * top_fraction))
        ranked = sorted(values, key=lambda item: (-float(item["score"]), str(item["ticker"])))
        if group_neutral and group_by_ticker:
            grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
            for item in ranked:
                grouped[str(item["group"])].append(item)
            selected = [items[0] for _, items in sorted(grouped.items()) if items]
            if len(selected) > count:
                selected = sorted(
                    selected, key=lambda item: (-float(item["score"]), str(item["ticker"]))
                )[:count]
            seed = {str(item["ticker"]) for item in selected}
            selected.extend(item for item in ranked if str(item["ticker"]) not in seed)
            selected = selected[:count]
        else:
            selected = ranked[:count]
        bottom = ranked[-count:]
        selected_tickers = {str(item["ticker"]) for item in selected}
        turnover = 0.0 if not previous else 1.0 - len(previous & selected_tickers) / max(
            len(previous), len(selected_tickers)
        )
        gross = sum(float(item["outcome"]) for item in selected) / len(selected)
        cohort = sum(float(item["outcome"]) for item in values) / len(values)
        bottom_return = sum(float(item["outcome"]) for item in bottom) / len(bottom)
        cost = turnover * transaction_cost_bps / 10000.0
        periods.append(
            {
                "asof_date": asof,
                "exit_date": str(selected[0]["exit_date"]),
                "cross_section": len(values),
                "selected": len(selected),
                "ic": spearman(
                    [float(item["score"]) for item in values],
                    [float(item["outcome"]) for item in values],
                ),
                "turnover": turnover,
                "gross_excess": gross,
                "net_excess": gross - cost,
                "cohort_excess": cohort,
                "bottom_excess": bottom_return,
                "top_minus_cohort_net": gross - cohort - cost,
                "top_minus_bottom_gross": gross - bottom_return,
                "selected_tickers": sorted(selected_tickers),
            }
        )
        for item in values:
            universe_groups[str(item["group"])] += 1
        for item in selected:
            group = str(item["group"])
            selected_groups[group] += 1
            selected_group_dates[group].add(asof)
        previous = selected_tickers
    return {
        **summarize_periods(periods),
        "period_rows": periods,
        "selection_groups": selected_groups,
        "universe_groups": universe_groups,
        "selection_group_dates": selected_group_dates,
    }


def component_diagnostics(
    rows: Sequence[Mapping[str, object]],
    *,
    cohorts: Sequence[str],
    horizon_sessions: int,
    minimum_cross_sections: Mapping[str, int],
    calendar_blocks: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for cohort_id in cohorts:
        minimum = int(minimum_cross_sections[cohort_id])
        eligible = [
            row for row in rows
            if str(row.get("calibration_cohort") or "") == cohort_id
            and int(float(str(row.get("horizon_sessions") or 0))) == horizon_sessions
            and str(row.get("calibration_eligible_flag") or "") == "1"
            and str(row.get("outcome_available_flag") or "") == "1"
        ]
        exit_by_date = {
            str(row.get("asof_date") or ""): str(
                row.get("benchmark_exit_date") or row.get("exit_date") or ""
            )
            for row in eligible
        }
        for component in COMPONENTS:
            by_date: dict[str, list[tuple[float, float]]] = defaultdict(list)
            for row in eligible:
                score = number(row.get(component))
                outcome = number(row.get("forward_excess_return"))
                if score is not None and outcome is not None:
                    by_date[str(row.get("asof_date") or "")].append((score, outcome))
            date_rows: list[dict[str, object]] = []
            for asof, values in sorted(by_date.items()):
                if len(values) < minimum:
                    continue
                scores = [item[0] for item in values]
                date_rows.append(
                    {
                        "asof_date": asof,
                        "exit_date": exit_by_date[asof],
                        "ic": spearman(scores, [item[1] for item in values]),
                        "std": statistics.pstdev(scores) if len(scores) > 1 else 0.0,
                        "neutral": sum(abs(item - 50.0) <= 1e-12 for item in scores) / len(scores),
                    }
                )
            block_ids = ["all", *[str(item["block_id"]) for item in calendar_blocks]]
            for block_id in block_ids:
                selected = date_rows if block_id == "all" else [
                    row for row in date_rows
                    if fixed_block(str(row["asof_date"]), calendar_blocks) == block_id
                ]
                overlap_free = non_overlapping(selected)
                ics = [number(row.get("ic")) for row in selected]
                output.append(
                    {
                        "cohort_id": cohort_id,
                        "evaluation_block": block_id,
                        "component": component,
                        "snapshot_count": len(selected),
                        "mean_ic": mean(ics),
                        "median_ic": median(ics),
                        "positive_ic_fraction": hit_rate(ics),
                        "non_overlapping_count": len(overlap_free),
                        "non_overlapping_mean_ic": mean(
                            number(row.get("ic")) for row in overlap_free
                        ),
                        "hac_t_stat": hac_t_stat(
                            [float(item) for item in ics if item is not None]
                        ),
                        "neutral_50_fraction": mean(
                            number(row.get("neutral")) for row in selected
                        ),
                        "average_cross_section_std": mean(
                            number(row.get("std")) for row in selected
                        ),
                    }
                )
    return output


def parse_metric_payload(row: Mapping[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    values = json.loads(str(row.get("metric_values_json") or "{}"))
    statuses = json.loads(str(row.get("metric_status_json") or "{}"))
    return (
        values if isinstance(values, dict) else {},
        statuses if isinstance(statuses, dict) else {},
    )


def metric_diagnostics(
    rows: Sequence[Mapping[str, object]],
    *,
    cohort_id: str,
    policy: Mapping[str, object],
    registry_definitions: Sequence[object],
    horizon_sessions: int,
    calendar_blocks: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    metric_directions = {
        str(getattr(item, "metric_id")): float(getattr(item, "direction"))
        for item in registry_definitions
    }
    construction = policy.get("score_construction", {})
    retained = construction.get("retained_specialized_metrics", []) if isinstance(
        construction, dict
    ) else []
    domains = policy.get("metric_comparison_domains", {})
    output: list[dict[str, object]] = []
    for metric_id in retained:
        metric_id = str(metric_id)
        direction = metric_directions.get(metric_id, 1.0)
        metric_domains = domains.get(metric_id, {}) if isinstance(domains, dict) else {}
        if not isinstance(metric_domains, dict) or not metric_domains:
            metric_domains = {"cohort": list(policy.get("eligible_tickers", []))}
        for domain, tickers_raw in metric_domains.items():
            tickers = {str(item) for item in tickers_raw}
            by_date: dict[str, list[tuple[float, float]]] = defaultdict(list)
            applicable = 0
            observed = 0
            for row in rows:
                if str(row.get("calibration_cohort") or "") != cohort_id:
                    continue
                if int(float(str(row.get("horizon_sessions") or 0))) != horizon_sessions:
                    continue
                if str(row.get("calibration_eligible_flag") or "") != "1":
                    continue
                if str(row.get("outcome_available_flag") or "") != "1":
                    continue
                if str(row.get("ticker") or "") not in tickers:
                    continue
                applicable += 1
                values, statuses = parse_metric_payload(row)
                value = number(values.get(metric_id))
                if value is None or str(statuses.get(metric_id) or "") not in OBSERVED:
                    continue
                observed += 1
                by_date[str(row.get("asof_date") or "")].append(
                    (value * direction, float(row["forward_excess_return"]))
                )
            ic_rows: list[dict[str, object]] = []
            for asof, values in sorted(by_date.items()):
                if len(values) < 3:
                    continue
                ic_rows.append(
                    {
                        "asof_date": asof,
                        "ic": spearman(
                            [item[0] for item in values],
                            [item[1] for item in values],
                        ),
                        "names": len(values),
                    }
                )
            coverage = observed / applicable if applicable else 0.0
            overall_ic = mean(number(row.get("ic")) for row in ic_rows)
            positive_fraction = hit_rate(number(row.get("ic")) for row in ic_rows)
            if metric_id == "freight_weight_per_shipment":
                disposition = "RESEARCH_DIRECTION_UNRESOLVED_NO_EXTRACTION"
            elif (
                overall_ic is not None and overall_ic >= 0.075
                and (positive_fraction or 0.0) >= 0.55
                and len(ic_rows) >= 24
                and (mean(number(row.get("names")) for row in ic_rows) or 0.0) >= 3
                and coverage < 0.80
            ):
                disposition = "TARGETED_RESEARCH_EXTRACTION_ONLY"
            elif coverage >= 0.60 and (overall_ic or -999.0) >= 0.025:
                disposition = "RETAIN_CURRENT_RESEARCH_INPUT"
            else:
                disposition = "DO_NOT_EXPAND_FROM_REVEALED_HISTORY"
            block_ics = {}
            for block in calendar_blocks:
                block_id = str(block["block_id"])
                block_ics[block_id] = mean(
                    number(row.get("ic")) for row in ic_rows
                    if fixed_block(str(row["asof_date"]), calendar_blocks) == block_id
                )
            output.append(
                {
                    "cohort_id": cohort_id,
                    "metric_id": metric_id,
                    "comparison_domain": str(domain),
                    "applicable_rows": applicable,
                    "observed_rows": observed,
                    "coverage": coverage,
                    "ic_dates": len(ic_rows),
                    "average_names_per_ic_date": mean(
                        number(row.get("names")) for row in ic_rows
                    ),
                    "mean_directional_ic": overall_ic,
                    "median_directional_ic": median(number(row.get("ic")) for row in ic_rows),
                    "positive_ic_fraction": positive_fraction,
                    "block_1_ic": block_ics.get("diagnostic_block_1"),
                    "block_2_ic": block_ics.get("diagnostic_block_2"),
                    "block_3_ic": block_ics.get("diagnostic_block_3"),
                    "research_disposition": disposition,
                }
            )
    return output


def add_tieout(
    output: list[dict[str, object]],
    *,
    check_id: str,
    scope: str,
    expected: object,
    actual: object,
    tolerance: float = TOLERANCE,
) -> None:
    expected_number = number(expected)
    actual_number = number(actual)
    if expected_number is not None and actual_number is not None:
        error = abs(expected_number - actual_number)
        status = "PASS" if error <= tolerance else "FAIL"
    else:
        error = None
        status = "PASS" if str(expected) == str(actual) else "FAIL"
    output.append(
        {
            "check_id": check_id,
            "scope": scope,
            "expected": expected,
            "actual": actual,
            "absolute_error": error,
            "status": status,
        }
    )


def candidate_tieout(
    rows: Sequence[Mapping[str, object]],
    *,
    protocol: Mapping[str, object],
    stored_results: Sequence[Mapping[str, object]],
    stored_periods: Sequence[Mapping[str, object]],
) -> tuple[list[dict[str, object]], dict[tuple[str, str, int], dict[str, object]]]:
    evaluation = protocol["evaluation"]
    top_fraction = float(evaluation["selection_fraction"])
    costs = float(evaluation["transaction_cost_bps"])
    reconstructed: dict[tuple[str, str, int], dict[str, object]] = {}
    checks: list[dict[str, object]] = []
    summary_fields = (
        "snapshot_count", "mean_ic", "mean_top_excess_net", "mean_cohort_excess",
        "mean_top_minus_cohort_net", "mean_top_minus_bottom_gross",
        "top_excess_hit_rate", "average_turnover",
    )
    period_fields = (
        "cross_section", "selected", "ic", "turnover", "gross_excess",
        "net_excess", "cohort_excess", "bottom_excess",
        "top_minus_cohort_net", "top_minus_bottom_gross",
    )
    for cohort_id, registry_raw in protocol["candidate_registries"].items():
        registry = dict(registry_raw)
        minimum = int(registry["minimum_cross_section"])
        for candidate_id, weights in registry["candidates"].items():
            for horizon in evaluation["horizons_sessions"]:
                key = (str(cohort_id), str(candidate_id), int(horizon))
                result = independent_candidate(
                    rows,
                    cohort_id=str(cohort_id),
                    weights=weights,
                    horizon_sessions=int(horizon),
                    minimum_cross_section=minimum,
                    top_fraction=top_fraction,
                    transaction_cost_bps=costs,
                )
                reconstructed[key] = result
                stored_summary = next(
                    row for row in stored_results
                    if str(row["cohort_id"]) == cohort_id
                    and str(row["candidate_id"]) == candidate_id
                    and int(float(str(row["horizon_sessions"]))) == int(horizon)
                    and str(row["evaluation_block"]) == "diagnostic_all"
                )
                scope = f"{cohort_id}/{candidate_id}/{horizon}"
                for field in summary_fields:
                    add_tieout(
                        checks, check_id=f"summary:{field}", scope=scope,
                        expected=stored_summary.get(field), actual=result.get(field),
                    )
                stored_by_date = {
                    str(row["asof_date"]): row for row in stored_periods
                    if str(row["cohort_id"]) == cohort_id
                    and str(row["candidate_id"]) == candidate_id
                    and int(float(str(row["horizon_sessions"]))) == int(horizon)
                }
                reconstructed_periods = {
                    str(row["asof_date"]): row for row in result["period_rows"]
                }
                add_tieout(
                    checks, check_id="period:date_set", scope=scope,
                    expected="|".join(sorted(stored_by_date)),
                    actual="|".join(sorted(reconstructed_periods)),
                )
                for asof in sorted(set(stored_by_date) & set(reconstructed_periods)):
                    for field in period_fields:
                        add_tieout(
                            checks, check_id=f"period:{field}", scope=f"{scope}/{asof}",
                            expected=stored_by_date[asof].get(field),
                            actual=reconstructed_periods[asof].get(field),
                        )
    return checks, reconstructed


def permutation_p_value(
    rows: Sequence[Mapping[str, object]],
    *,
    cohort_id: str,
    weights: Mapping[str, float],
    horizon_sessions: int,
    minimum_cross_section: int,
    observed_mean_ic: float | None,
    permutations: int,
) -> float | None:
    if observed_mean_ic is None or permutations <= 0:
        return None
    by_date: dict[str, tuple[list[float], list[float]]] = {}
    grouped: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in rows:
        if str(row.get("calibration_cohort") or "") != cohort_id:
            continue
        if int(float(str(row.get("horizon_sessions") or 0))) != horizon_sessions:
            continue
        if str(row.get("calibration_eligible_flag") or "") != "1":
            continue
        if str(row.get("outcome_available_flag") or "") != "1":
            continue
        score = weighted_score(row, weights)
        outcome = number(row.get("forward_excess_return"))
        if score is not None and outcome is not None:
            grouped[str(row.get("asof_date") or "")].append((score, outcome))
    for asof, values in grouped.items():
        if len(values) >= minimum_cross_section:
            by_date[asof] = ([item[0] for item in values], [item[1] for item in values])
    rng = random.Random(f"transportation-v7-{cohort_id}-{horizon_sessions}")
    null_means: list[float] = []
    for _ in range(permutations):
        date_ics: list[float] = []
        for scores, outcomes in by_date.values():
            shuffled = list(outcomes)
            rng.shuffle(shuffled)
            value = spearman(scores, shuffled)
            if value is not None:
                date_ics.append(float(value))
        if date_ics:
            null_means.append(sum(date_ics) / len(date_ics))
    return (
        (1 + sum(abs(item) >= abs(observed_mean_ic) for item in null_means))
        / (len(null_means) + 1)
        if null_means else None
    )


def group_map(policy: Mapping[str, object]) -> dict[str, str]:
    output: dict[str, str] = {}
    for group, tickers in dict(policy.get("comparison_group_tickers") or {}).items():
        for ticker in tickers:
            output[str(ticker)] = str(group)
    for ticker, payload in dict(policy.get("historical_calibration_only") or {}).items():
        if isinstance(payload, dict) and payload.get("comparison_group"):
            output[str(ticker)] = str(payload["comparison_group"])
    return output


def build_ablations(
    rows: Sequence[Mapping[str, object]],
    *,
    protocol: Mapping[str, object],
    diagnostic: Mapping[str, object],
    policies: Mapping[str, Mapping[str, object]],
    permutations: int,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    evaluation = protocol["evaluation"]
    horizon = int(evaluation["primary_horizon_sessions"])
    top_fraction = float(evaluation["selection_fraction"])
    costs = float(evaluation["transaction_cost_bps"])
    ablations: list[dict[str, object]] = []
    attributions: list[dict[str, object]] = []
    for cohort_id, registry_raw in protocol["candidate_registries"].items():
        registry = dict(registry_raw)
        minimum = int(registry["minimum_cross_section"])
        selected_id = str(
            diagnostic["cohort_summaries"][cohort_id]["diagnostic_selected_candidate"]
        )
        selected_weights = dict(registry["candidates"][selected_id])
        candidates: list[tuple[str, str, dict[str, float], bool]] = [
            (selected_id, "global", selected_weights, False),
            (
                "growth_capital_equal_diagnostic",
                "global",
                {"growth_score": 0.5, "capital_risk_score": 0.5},
                False,
            ),
        ]
        candidates.extend(
            (f"component_only_{component}", "global", {component: 1.0}, False)
            for component in CORE_COMPONENTS
        )
        for removed in CORE_COMPONENTS:
            reduced = {
                field: float(weight) for field, weight in selected_weights.items()
                if field != removed and float(weight) > 0
            }
            candidates.append((f"selected_without_{removed}", "global", reduced, False))
        mapping = group_map(policies[cohort_id])
        if cohort_id == "north_american_surface_freight_and_logistics_v5":
            candidates.append(
                (f"{selected_id}_group_neutral", "group_neutral", selected_weights, True)
            )
        for candidate_id, method, weights, group_neutral in candidates:
            result = independent_candidate(
                rows,
                cohort_id=cohort_id,
                weights=weights,
                horizon_sessions=horizon,
                minimum_cross_section=minimum,
                top_fraction=top_fraction,
                transaction_cost_bps=costs,
                group_by_ticker=mapping,
                group_neutral=group_neutral,
            )
            p_value = None
            if candidate_id == selected_id or group_neutral:
                p_value = permutation_p_value(
                    rows,
                    cohort_id=cohort_id,
                    weights=weights,
                    horizon_sessions=horizon,
                    minimum_cross_section=minimum,
                    observed_mean_ic=number(result.get("mean_ic")),
                    permutations=permutations,
                )
            ablations.append(
                {
                    "cohort_id": cohort_id,
                    "candidate_id": candidate_id,
                    "selection_method": method,
                    **{field: result.get(field) for field in ABLATION_FIELDS if field in result},
                    "permutation_p_value": p_value,
                    "evidence_role": "DESCRIPTIVE_ONLY_REVEALED_HISTORY",
                }
            )
            if candidate_id == selected_id:
                total_available = sum(result["universe_groups"].values())
                total_selected = sum(result["selection_groups"].values())
                all_groups = sorted(
                    set(result["universe_groups"]) | set(result["selection_groups"])
                )
                for group in all_groups:
                    attributions.append(
                        {
                            "cohort_id": cohort_id,
                            "candidate_id": candidate_id,
                            "comparison_group": group,
                            "available_rows": result["universe_groups"].get(group, 0),
                            "selected_slots": result["selection_groups"].get(group, 0),
                            "universe_row_share": (
                                result["universe_groups"].get(group, 0) / total_available
                                if total_available else None
                            ),
                            "selected_slot_share": (
                                result["selection_groups"].get(group, 0) / total_selected
                                if total_selected else None
                            ),
                            "dates_represented": len(
                                result["selection_group_dates"].get(group, set())
                            ),
                            "total_dates": result["snapshot_count"],
                        }
                    )
    return ablations, attributions


def artifact_and_pit_checks(
    rows: Sequence[Mapping[str, object]],
    *,
    panel_path: Path,
    panel_manifest: Mapping[str, object],
    validation: Mapping[str, object],
) -> list[dict[str, object]]:
    checks: list[dict[str, object]] = []
    add_tieout(
        checks, check_id="artifact:panel_sha256", scope=str(panel_path),
        expected=panel_manifest.get("panel_sha256"), actual=file_sha256(panel_path),
    )
    for key in ("normalized_price_slice", "pinned_raw_price_slice"):
        path = Path(str(panel_manifest[f"{key}_path"]))
        add_tieout(
            checks, check_id=f"artifact:{key}_sha256", scope=str(path),
            expected=panel_manifest.get(f"{key}_sha256"), actual=file_sha256(path),
        )
    add_tieout(
        checks, check_id="validation:acceptance", scope="outcome_panel",
        expected="PASS", actual=validation.get("acceptance"),
    )
    add_tieout(
        checks, check_id="validation:return_reconstruction", scope="outcome_panel",
        expected="PASS", actual=validation.get("return_reconstruction", {}).get("acceptance"),
    )
    violations = Counter()
    for row in rows:
        asof = parse_date(row["asof_date"])
        entry = str(row.get("entry_date") or "")
        exit_value = str(row.get("exit_date") or "")
        if entry and parse_date(entry) <= asof:
            violations["entry_not_after_asof"] += 1
        if entry and exit_value and parse_date(exit_value) <= parse_date(entry):
            violations["exit_not_after_entry"] += 1
        if str(row.get("historical_calibration_only_flag") or "") == "1" and str(
            row.get("current_portfolio_eligibility_authorized") or ""
        ) == "1":
            violations["historical_only_currently_authorized"] += 1
        if str(row.get("outcome_available_flag") or "") != "1" and number(
            row.get("forward_excess_return")
        ) is not None:
            violations["outcome_present_while_unavailable"] += 1
        for field in (
            "source_score_sha256", "source_calibration_sidecar_sha256",
            "source_snapshot_manifest_sha256",
        ):
            value = str(row.get(field) or "")
            if len(value) != 64:
                violations[f"invalid_{field}"] += 1
    for check_id in (
        "entry_not_after_asof", "exit_not_after_entry",
        "historical_only_currently_authorized", "outcome_present_while_unavailable",
        "invalid_source_score_sha256", "invalid_source_calibration_sidecar_sha256",
        "invalid_source_snapshot_manifest_sha256",
    ):
        add_tieout(
            checks, check_id=f"pit:{check_id}", scope="all_panel_rows",
            expected=0, actual=violations.get(check_id, 0), tolerance=0.0,
        )
    return checks


def finding(
    severity: str,
    finding_type: str,
    category: str,
    location: str,
    description: str,
    decision_impact: str,
    recommended_fix: str,
) -> dict[str, object]:
    return {
        "severity": severity,
        "finding_type": finding_type,
        "category": category,
        "location": location,
        "finding": description,
        "decision_impact": decision_impact,
        "recommended_fix": recommended_fix,
        "owner": "transportation_research",
    }


def build_findings(
    *,
    tieouts: Sequence[Mapping[str, object]],
    component_rows: Sequence[Mapping[str, object]],
    metric_rows: Sequence[Mapping[str, object]],
    ablations: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    failures = [row for row in tieouts if str(row.get("status")) == "FAIL"]
    if failures:
        findings.append(
            finding(
                "CRITICAL", "MECHANICAL_DEFECT", "model_recalculation",
                "v6 frozen outcome and candidate artifacts",
                f"{len(failures)} independent formula, selection, hash, or PIT checks failed.",
                "No historical model conclusion is reliable until the tie-out is repaired.",
                "Repair the first failing dependency and rerun this audit before redesign or parsing.",
            )
        )
    else:
        findings.append(
            finding(
                "INFO", "CONTROL_VERIFIED", "model_recalculation",
                "v6 frozen outcome and candidate artifacts",
                "Independent score, selection, turnover, return, hash, and PIT checks tie out.",
                "The weak ranking result is not explained by arithmetic or artifact-lineage drift.",
                "Preserve v6; address model specification without rewriting the diagnostic record.",
            )
        )
    findings.append(
        finding(
            "HIGH", "CONTROL_DESIGN_GAP", "independence",
            "38q_run_transportation_v5_diagnostic_calibration.py ranking gate",
            "The ranking gate uses the mean of overlapping monthly 63-session IC observations; "
            "non-overlapping count is checked separately but non-overlapping IC is not a gate.",
            "Serially dependent observations can overstate effective evidence and could create a false pass.",
            "For any future protocol require non-overlapping IC/spread and a HAC uncertainty check; "
            "do not reinterpret the sealed v6 result.",
        )
    )
    tanker_all = {
        str(row["component"]): number(row.get("mean_ic"))
        for row in component_rows
        if str(row["cohort_id"]) == "oil_tanker_operators_v5"
        and str(row["evaluation_block"]) == "all"
    }
    if (tanker_all.get("valuation_score") or 0.0) < 0 and (
        tanker_all.get("operating_efficiency_score") or 0.0
    ) < 0:
        findings.append(
            finding(
                "HIGH", "MODEL_SPECIFICATION", "economic_definition",
                "transportation_tanker_score_policy_v1.yaml",
                "Generic valuation and asset-turnover operating-efficiency components are "
                "anti-correlated with forward tanker returns in the revealed diagnostic history.",
                "The current tanker score is cycle-insensitive; more observations of the same inputs "
                "are unlikely to repair cross-sectional ranking.",
                "Design a research-only cycle-aware tanker score using rate/breakeven spread, rate "
                "momentum, utilization/coverage, leverage, fleet supply, and NAV-based valuation.",
            )
        )
    operating_ratio_rows = [
        row for row in metric_rows
        if str(row.get("metric_id")) == "operating_ratio"
        and (number(row.get("mean_directional_ic")) or 0.0) < 0
    ]
    if operating_ratio_rows:
        findings.append(
            finding(
                "HIGH", "MODEL_SPECIFICATION", "metric_transform",
                "transportation_surface_freight_score_policy_v3.yaml operating_ratio",
                "The lower-is-better operating-ratio level has negative directional IC in each "
                "tested surface comparison domain.",
                "Expanding level coverage can amplify a mis-specified signal.",
                "Test operating-ratio change/improvement and within-group normalization; do not "
                "authorize further level parsing from these revealed outcomes.",
            )
        )
    selected = [
        row for row in ablations
        if not str(row.get("candidate_id", "")).startswith("component_only_")
        and str(row.get("selection_method")) == "global"
        and str(row.get("candidate_id")) in {"surface_balanced_v5", "tanker_quality_fleet_v1"}
    ]
    if selected and all((number(row.get("permutation_p_value")) or 1.0) > 0.10 for row in selected):
        findings.append(
            finding(
                "HIGH", "INSUFFICIENT_EVIDENCE", "ranking_power",
                "v6 selected cohort candidates",
                "Selected-candidate mean IC is not distinguishable from a within-date permutation null.",
                "Historical v6 results cannot support production promotion or weight optimization.",
                "Keep production fail-closed and use only a pre-registered future proof after redesign.",
            )
        )
    targets = sorted(
        {
            f"{row['metric_id']}:{row['comparison_domain']}"
            for row in metric_rows
            if str(row.get("research_disposition")) == "TARGETED_RESEARCH_EXTRACTION_ONLY"
        }
    )
    findings.append(
        finding(
            "MEDIUM", "WORK_SEQUENCING", "parser_scope",
            "specialized metric research queue",
            (
                "Only outcome-informed diagnostic targets meet the narrow signal-and-coverage screen: "
                + (", ".join(targets) if targets else "none")
                + "."
            ),
            "Broad reparsing would spend resources without first demonstrating incremental information.",
            "Treat any listed target as research-only and require frozen-definition incremental proof "
            "before a one-time targeted extraction; do not use this revealed history for promotion.",
        )
    )
    return findings


def fmt(value: object, digits: int = 4) -> str:
    parsed = number(value)
    if parsed is None:
        return "n/a"
    return f"{parsed:.{digits}f}"


def markdown_report(payload: Mapping[str, object]) -> str:
    summary = payload["summary"]
    findings = payload["findings"]
    lines = [
        "# Transportation v5 model forensic audit",
        "",
        f"**Audit verdict:** {summary['audit_verdict']}",
        "",
        f"**Permitted use:** {summary['permitted_use']}",
        "",
        "The audit is read-only against the frozen v6 lineage. It performs no network "
        "requests, parser calls, score rebuilds, policy changes, or production activation.",
        "",
        "## Executive conclusion",
        "",
        str(summary["executive_conclusion"]),
        "",
        "## Cohort evidence",
        "",
        "| Cohort | Selected model | Mean IC | Non-overlap IC | Permutation p | Top-cohort net |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in payload["selected_candidate_audits"]:
        lines.append(
            f"| {row['cohort_id']} | {row['candidate_id']} | "
            f"{fmt(row.get('mean_ic'))} | {fmt(row.get('non_overlapping_mean_ic'))} | "
            f"{fmt(row.get('permutation_p_value'))} | "
            f"{fmt(row.get('mean_top_minus_cohort_net'))} |"
        )
    lines.extend(["", "## Findings", ""])
    for item in findings:
        lines.extend(
            [
                f"### {item['severity']}  {item['finding_type']}: {item['category']}",
                "",
                str(item["finding"]),
                "",
                f"Decision impact: {item['decision_impact']}",
                "",
                f"Recommended fix: {item['recommended_fix']}",
                "",
            ]
        )
    lines.extend(
        [
            "## Decision gates",
            "",
            f"- Mechanical tie-out: **{summary['mechanical_tieout']}**.",
            f"- Broad specialized-metric reparse: **{summary['broad_reparse_authorized']}**.",
            f"- Historical v7 production calibration: **{summary['historical_recalibration_authorized']}**.",
            f"- Production promotion: **{summary['production_activation_authorized']}**.",
            f"- Next action: **{summary['next_action']}**.",
            "",
            "All ablations and signal screens in this report use previously revealed outcomes. "
            "They diagnose architecture; they are not untouched promotion evidence.",
            "",
        ]
    )
    return "\n".join(lines)


def html_report(markdown_text: str, payload: Mapping[str, object]) -> str:
    findings = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(str(row["severity"])),
            html.escape(str(row["finding_type"])),
            html.escape(str(row["finding"])),
            html.escape(str(row["recommended_fix"])),
        )
        for row in payload["findings"]
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Transportation model audit</title>
<style>body{{font:15px system-ui;margin:2rem;max-width:1100px;color:#17202a}}table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #ccd1d1;padding:.55rem;vertical-align:top}}th{{background:#eef2f3}}code{{background:#f5f5f5}}</style>
</head><body><h1>Transportation v5 model forensic audit</h1>
<p><strong>Verdict:</strong> {html.escape(str(payload['summary']['audit_verdict']))}</p>
<p>{html.escape(str(payload['summary']['executive_conclusion']))}</p>
<h2>Findings</h2><table><thead><tr><th>Severity</th><th>Type</th><th>Finding</th><th>Fix</th></tr></thead>
<tbody>{findings}</tbody></table><p>Machine-readable evidence is in the adjacent JSON and CSV artifacts.</p>
</body></html>"""


def main() -> int:
    args = parse_args()
    panel_path = args.panel_dir / "transportation_v5_outcome_panel.csv"
    manifest_path = args.panel_dir / "transportation_v5_outcome_panel_manifest.json"
    diagnostic_path = args.diagnostic_dir / "transportation_v5_diagnostic_calibration.json"
    candidate_results_path = args.diagnostic_dir / "transportation_v5_candidate_results.csv"
    candidate_periods_path = args.diagnostic_dir / "transportation_v5_candidate_period_results.csv"
    required_paths = (
        panel_path, manifest_path, args.outcome_validation, args.protocol,
        diagnostic_path, candidate_results_path, candidate_periods_path,
        args.metric_registry,
    )
    missing = [str(path) for path in required_paths if not path.exists()]
    if missing:
        raise FileNotFoundError(f"missing forensic audit inputs={missing}")

    rows = read_csv(panel_path)
    panel_manifest = read_json(manifest_path)
    validation = read_json(args.outcome_validation)
    protocol = read_json(args.protocol)
    diagnostic = read_json(diagnostic_path)
    stored_results = read_csv(candidate_results_path)
    stored_periods = read_csv(candidate_periods_path)
    _, registry_definitions = load_metric_registry(args.metric_registry)
    policies = {
        str(cohort_id): load_cohort_score_policy(Path(str(payload["policy_path"])))
        for cohort_id, payload in protocol["candidate_registries"].items()
    }
    evaluation = protocol["evaluation"]
    cohorts = list(protocol["candidate_registries"])
    minimums = {
        str(cohort_id): int(payload["minimum_cross_section"])
        for cohort_id, payload in protocol["candidate_registries"].items()
    }
    horizon = int(evaluation["primary_horizon_sessions"])
    calendar_blocks = list(evaluation["calendar_blocks"])

    tieouts, _ = candidate_tieout(
        rows,
        protocol=protocol,
        stored_results=stored_results,
        stored_periods=stored_periods,
    )
    tieouts.extend(
        artifact_and_pit_checks(
            rows,
            panel_path=panel_path,
            panel_manifest=panel_manifest,
            validation=validation,
        )
    )
    components = component_diagnostics(
        rows,
        cohorts=cohorts,
        horizon_sessions=horizon,
        minimum_cross_sections=minimums,
        calendar_blocks=calendar_blocks,
    )
    metrics: list[dict[str, object]] = []
    for cohort_id in cohorts:
        metrics.extend(
            metric_diagnostics(
                rows,
                cohort_id=cohort_id,
                policy=policies[cohort_id],
                registry_definitions=registry_definitions,
                horizon_sessions=horizon,
                calendar_blocks=calendar_blocks,
            )
        )
    ablations, attributions = build_ablations(
        rows,
        protocol=protocol,
        diagnostic=diagnostic,
        policies=policies,
        permutations=args.permutations,
    )
    findings = build_findings(
        tieouts=tieouts,
        component_rows=components,
        metric_rows=metrics,
        ablations=ablations,
    )
    failed_tieouts = [row for row in tieouts if str(row.get("status")) == "FAIL"]
    selected_ids = {
        str(cohort_id): str(payload["diagnostic_selected_candidate"])
        for cohort_id, payload in diagnostic["cohort_summaries"].items()
    }
    selected_audits = [
        row for row in ablations
        if str(row.get("candidate_id")) == selected_ids.get(str(row.get("cohort_id")))
        and str(row.get("selection_method")) == "global"
    ]
    targeted = sorted(
        {
            f"{row['metric_id']}:{row['comparison_domain']}"
            for row in metrics
            if str(row.get("research_disposition")) == "TARGETED_RESEARCH_EXTRACTION_ONLY"
        }
    )
    if failed_tieouts:
        verdict = "FAIL_MECHANICAL_CONTROL"
        conclusion = (
            "The frozen v6 calculation or lineage does not independently tie out. "
            "Model redesign and parser work remain blocked until the failed controls are repaired."
        )
        next_action = "REPAIR_FAILED_TIEOUTS_AND_RERUN_38Y"
    else:
        verdict = "PASS_MECHANICS_FAIL_MODEL_SPECIFICATION"
        conclusion = (
            "The weak recent ranking is not a return, PIT, score, sorting, turnover, or artifact-hash bug. "
            "It is a model-specification problem: surface signals are subgroup/regime dependent, while "
            "the tanker model applies cycle-insensitive generic valuation and efficiency measures. "
            "More broad parsing of the current definitions is not the efficient remedy."
        )
        next_action = "BUILD_FAIL_CLOSED_V7_RESEARCH_DECISION"
    summary = {
        "audit_verdict": verdict,
        "permitted_use": "RESEARCH_DIAGNOSIS_ONLY_NO_PRODUCTION_AUTHORITY",
        "executive_conclusion": conclusion,
        "mechanical_tieout": "PASS" if not failed_tieouts else "FAIL",
        "tieout_check_count": len(tieouts),
        "tieout_failure_count": len(failed_tieouts),
        "broad_reparse_authorized": False,
        "targeted_research_extraction_candidates": targeted,
        "historical_recalibration_authorized": False,
        "production_activation_authorized": False,
        "next_action": next_action,
        "network_requests": 0,
        "parser_invocations": 0,
    }
    payload: dict[str, object] = {
        "contract_version": "transportation_v5_model_forensic_audit_v1",
        "asof_date": "2026-08-21",
        "summary": summary,
        "selected_candidate_audits": selected_audits,
        "component_diagnostics": components,
        "specialized_metric_diagnostics": metrics,
        "research_ablations": ablations,
        "selection_attribution": attributions,
        "findings": findings,
        "input_lineage": {
            "panel_path": str(panel_path.resolve()),
            "panel_sha256": file_sha256(panel_path),
            "panel_manifest_path": str(manifest_path.resolve()),
            "panel_manifest_sha256": file_sha256(manifest_path),
            "outcome_validation_path": str(args.outcome_validation.resolve()),
            "outcome_validation_sha256": file_sha256(args.outcome_validation),
            "research_protocol_path": str(args.protocol.resolve()),
            "research_protocol_sha256": file_sha256(args.protocol),
            "diagnostic_calibration_path": str(diagnostic_path.resolve()),
            "diagnostic_calibration_sha256": file_sha256(diagnostic_path),
        },
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    component_path = args.output_dir / "transportation_v5_component_ic_audit.csv"
    metric_path = args.output_dir / "transportation_v5_specialized_metric_signal_audit.csv"
    ablation_path = args.output_dir / "transportation_v5_research_ablations.csv"
    attribution_path = args.output_dir / "transportation_v5_selection_attribution.csv"
    tieout_path = args.output_dir / "transportation_v5_model_tieout.csv"
    finding_path = args.output_dir / "transportation_v5_model_audit_findings.csv"
    json_path = args.output_dir / "transportation_v5_model_forensic_audit.json"
    markdown_path = args.output_dir / "TRANSPORTATION_V5_MODEL_FORENSIC_AUDIT.md"
    html_path = args.output_dir / "transportation_v5_model_forensic_audit.html"
    write_csv_atomic(component_path, COMPONENT_FIELDS, components)
    write_csv_atomic(metric_path, METRIC_FIELDS, metrics)
    write_csv_atomic(ablation_path, ABLATION_FIELDS, ablations)
    write_csv_atomic(attribution_path, ATTRIBUTION_FIELDS, attributions)
    write_csv_atomic(tieout_path, TIEOUT_FIELDS, tieouts)
    write_csv_atomic(finding_path, FINDING_FIELDS, findings)
    markdown_text = markdown_report(payload)
    write_text_atomic(markdown_path, markdown_text)
    write_text_atomic(html_path, html_report(markdown_text, payload))
    write_text_atomic(json_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not failed_tieouts else 1


if __name__ == "__main__":
    raise SystemExit(main())

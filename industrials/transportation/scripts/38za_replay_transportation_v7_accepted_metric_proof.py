#!/usr/bin/env python3
"""Replay accepted PIT facts to decide whether any targeted parsing is worthwhile."""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.oos_research import spearman, weighted_score  # noqa: E402
from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.contracts import file_sha256  # noqa: E402
from industrials.transportation.semantic_replay_contract import (  # noqa: E402
    resolve_semantic_replay_rows,
)


ROOT = PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v5"
DEFAULT_PANEL = (
    ROOT / "outcome_panel_v6" / "2026-08-16" / "transportation_v5_outcome_panel.csv"
)
DEFAULT_SEMANTIC_MANIFEST = (
    ROOT / "semantic_materialization" / "2026-08-13"
    / "transportation_semantic_materialization_audit.json"
)
DEFAULT_CONFIG = PROJECT_ROOT / "industrials" / "config.yaml"
DEFAULT_DECISION = (
    ROOT / "research_decision_v7" / "2026-08-21"
    / "transportation_v7_research_decision.json"
)
DEFAULT_PROTOCOL = (
    ROOT / "research_protocol_v6" / "2026-08-16"
    / "transportation_v5_research_protocol.json"
)
DEFAULT_OUTPUT_DIR = ROOT / "accepted_metric_proof_v7" / "2026-08-21"
RESULT_FIELDS = (
    "cohort_id", "metric_id", "comparison_domain", "observed_rows", "ic_dates",
    "average_names", "baseline_mean_ic", "metric_mean_ic", "combined_mean_ic",
    "incremental_mean_ic", "nonoverlap_baseline_ic", "nonoverlap_combined_ic",
    "nonoverlap_incremental_ic", "baseline_top_minus_domain",
    "combined_top_minus_domain", "incremental_top_minus_domain",
    "positive_increment_blocks", "proof_gate", "parser_authorization",
)
ACCEPTED_FACT_FIELDS = (
    "candidate_key", "ticker", "metric_id", "value", "unit", "period_end",
    "filing_date", "accession_number", "replay_status",
)
BLOCKS = (
    ("diagnostic_block_1", "2019-01-01", "2021-12-31"),
    ("diagnostic_block_2", "2022-01-01", "2023-12-31"),
    ("diagnostic_block_3", "2024-01-01", "2026-07-30"),
)
SURFACE_COHORT = "north_american_surface_freight_and_logistics_v5"
TANKER_COHORT = "oil_tanker_operators_v5"
LTL = {"ARCB", "ODFL", "SAIA", "XPO"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outcome-panel", type=Path, default=DEFAULT_PANEL)
    parser.add_argument("--semantic-manifest", type=Path, default=DEFAULT_SEMANTIC_MANIFEST)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--research-decision", type=Path, default=DEFAULT_DECISION)
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
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


def zscores(values: Sequence[float]) -> list[float]:
    if not values:
        return []
    center = sum(values) / len(values)
    scale = statistics.pstdev(values)
    return [(value - center) / scale for value in values] if scale > 0 else [0.0] * len(values)


def block_id(asof: str) -> str:
    matches = [name for name, start, end in BLOCKS if start <= asof <= end]
    if len(matches) != 1:
        raise ValueError(f"{asof}: not in exactly one fixed block")
    return matches[0]


def nonoverlap(rows: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    output: list[Mapping[str, object]] = []
    last_exit: date | None = None
    for row in sorted(rows, key=lambda item: str(item["asof_date"])):
        asof = date.fromisoformat(str(row["asof_date"]))
        exit_date = date.fromisoformat(str(row["exit_date"]))
        if last_exit is not None and asof <= last_exit:
            continue
        output.append(row)
        last_exit = exit_date
    return output


def evaluate_feature(
    observations: Sequence[Mapping[str, object]],
    *,
    cohort_id: str,
    metric_id: str,
    domain: str,
) -> dict[str, object]:
    by_date: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in observations:
        by_date[str(row["asof_date"])].append(row)
    periods: list[dict[str, object]] = []
    observed_rows = 0
    for asof, values in sorted(by_date.items()):
        if len(values) < 3:
            continue
        observed_rows += len(values)
        baseline = [float(row["baseline_score"]) for row in values]
        metric = [float(row["metric_value"]) for row in values]
        outcomes = [float(row["outcome"]) for row in values]
        baseline_z = zscores(baseline)
        metric_z = zscores(metric)
        combined = [0.8 * base + 0.2 * feature for base, feature in zip(baseline_z, metric_z)]
        count = max(1, math.ceil(len(values) * 0.20))

        def top_minus(scores: Sequence[float]) -> float:
            ranked = sorted(
                zip(scores, outcomes, [str(row["ticker"]) for row in values]),
                key=lambda item: (-item[0], item[2]),
            )
            top = sum(item[1] for item in ranked[:count]) / count
            return top - sum(outcomes) / len(outcomes)

        baseline_ic = spearman(baseline, outcomes)
        metric_ic = spearman(metric, outcomes)
        combined_ic = spearman(combined, outcomes)
        periods.append(
            {
                "asof_date": asof,
                "exit_date": str(values[0]["exit_date"]),
                "names": len(values),
                "baseline_ic": baseline_ic,
                "metric_ic": metric_ic,
                "combined_ic": combined_ic,
                "incremental_ic": (
                    float(combined_ic) - float(baseline_ic)
                    if combined_ic is not None and baseline_ic is not None else None
                ),
                "baseline_top_minus_domain": top_minus(baseline),
                "combined_top_minus_domain": top_minus(combined),
            }
        )
    overlap_free = nonoverlap(periods)
    block_deltas = {
        name: mean(
            number(row.get("incremental_ic")) for row in periods
            if block_id(str(row["asof_date"])) == name
        )
        for name, _, _ in BLOCKS
    }
    positive_blocks = sum(value is not None and value > 0 for value in block_deltas.values())
    incremental_ic = mean(number(row.get("incremental_ic")) for row in periods)
    nonoverlap_delta = mean(number(row.get("incremental_ic")) for row in overlap_free)
    baseline_spread = mean(number(row.get("baseline_top_minus_domain")) for row in periods)
    combined_spread = mean(number(row.get("combined_top_minus_domain")) for row in periods)
    incremental_spread = (
        combined_spread - baseline_spread
        if baseline_spread is not None and combined_spread is not None else None
    )
    passed = (
        len(periods) >= 24
        and (incremental_ic or -999.0) > 0
        and (nonoverlap_delta or -999.0) > 0
        and (incremental_spread or -999.0) > 0
        and positive_blocks >= 2
    )
    return {
        "cohort_id": cohort_id,
        "metric_id": metric_id,
        "comparison_domain": domain,
        "observed_rows": observed_rows,
        "ic_dates": len(periods),
        "average_names": mean(number(row.get("names")) for row in periods),
        "baseline_mean_ic": mean(number(row.get("baseline_ic")) for row in periods),
        "metric_mean_ic": mean(number(row.get("metric_ic")) for row in periods),
        "combined_mean_ic": mean(number(row.get("combined_ic")) for row in periods),
        "incremental_mean_ic": incremental_ic,
        "nonoverlap_baseline_ic": mean(
            number(row.get("baseline_ic")) for row in overlap_free
        ),
        "nonoverlap_combined_ic": mean(
            number(row.get("combined_ic")) for row in overlap_free
        ),
        "nonoverlap_incremental_ic": nonoverlap_delta,
        "baseline_top_minus_domain": baseline_spread,
        "combined_top_minus_domain": combined_spread,
        "incremental_top_minus_domain": incremental_spread,
        "positive_increment_blocks": positive_blocks,
        "proof_gate": "PASS" if passed else "FAIL",
        "parser_authorization": (
            "CONDITIONAL_ONE_TIME_QUEUE_ALLOWED" if passed
            else "DENY_MORE_PARSING_CURRENT_DEFINITION"
        ),
    }


def surface_observations(
    panel_rows: Sequence[Mapping[str, object]],
    weights: Mapping[str, float],
) -> dict[str, list[dict[str, object]]]:
    output = {
        "pricing_or_yield_growth": [],
        "shipment_or_load_growth": [],
    }
    for row in panel_rows:
        if str(row.get("calibration_cohort")) != SURFACE_COHORT:
            continue
        if str(row.get("ticker")) not in LTL:
            continue
        if str(row.get("horizon_sessions")) != "63":
            continue
        if str(row.get("calibration_eligible_flag")) != "1":
            continue
        if str(row.get("outcome_available_flag")) != "1":
            continue
        baseline = weighted_score(row, weights, require_complete=False)
        outcome = number(row.get("forward_excess_return"))
        values = json.loads(str(row.get("metric_values_json") or "{}"))
        statuses = json.loads(str(row.get("metric_status_json") or "{}"))
        if baseline is None or outcome is None:
            continue
        for metric_id in output:
            value = number(values.get(metric_id))
            if value is None or str(statuses.get(metric_id)) not in {
                "REPORTED", "DERIVED", "PROXY"
            }:
                continue
            output[metric_id].append(
                {
                    "asof_date": str(row["asof_date"]),
                    "exit_date": str(row["benchmark_exit_date"]),
                    "ticker": str(row["ticker"]),
                    "baseline_score": baseline,
                    "metric_value": value,
                    "outcome": outcome,
                }
            )
    return output


def accepted_specialized_rows(
    manifest_path: Path,
    *,
    config_path: Path,
    db_override: Path | None,
) -> list[dict[str, object]]:
    manifest = read_json(manifest_path)
    if manifest.get("acceptance") != "PASS":
        raise ValueError("semantic materialization must pass")
    lane = dict(manifest["lanes"]["tanker"])
    replay_path = Path(str(lane["conflict_free_csv"]))
    if file_sha256(replay_path) != str(lane["conflict_free_csv_sha256"]):
        raise ValueError("conflict-free tanker replay hash changed")
    combined = read_csv(replay_path)
    config = load_yaml(config_path)
    db_path = db_override.resolve() if db_override else resolve_path(
        cfg_get(config, "paths.database_path"), base_dir=config_path.parent
    )
    tickers = ("DHT", "ECO", "FRO", "NAT", "ASC", "HAFN", "STNG", "TRMD", "INSW", "TNK", "TEN")
    metrics = ("tce_day_rate", "revenue_days")
    ticker_sql = ",".join("?" for _ in tickers)
    metric_sql = ",".join("?" for _ in metrics)
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    db_rows = connection.execute(
        "SELECT candidate_key,ticker,metric_name,candidate_value,unit,period_end,"
        "filing_date,accession_number,candidate_status "
        "FROM fact_sec_metric_disclosure_candidate "
        "WHERE model_family='transportation' AND candidate_status='ACCEPTED' "
        f"AND ticker IN ({ticker_sql}) AND metric_name IN ({metric_sql}) "
        "AND filing_date<='2026-07-30'",
        (*tickers, *metrics),
    ).fetchall()
    connection.close()
    for row in db_rows:
        combined.append(
            {
                "candidate_key": str(row["candidate_key"]),
                "ticker": str(row["ticker"]),
                "metric_id": str(row["metric_name"]),
                "value": "" if row["candidate_value"] is None else str(row["candidate_value"]),
                "unit": str(row["unit"] or ""),
                "period_end": str(row["period_end"] or "")[:10],
                "filing_date": str(row["filing_date"] or "")[:10],
                "accession_number": str(row["accession_number"] or ""),
                "replay_status": str(row["candidate_status"] or ""),
            }
        )
    resolution = resolve_semantic_replay_rows(combined)
    return [
        dict(row) for row in resolution.conflict_free_rows
        if str(row.get("ticker")) in tickers
        and str(row.get("metric_id")) in metrics
        and number(row.get("value")) is not None
        and str(row.get("period_end") or "")
        and str(row.get("filing_date") or "")
    ]


def tanker_growth_observations(
    panel_rows: Sequence[Mapping[str, object]],
    specialized_rows: Sequence[Mapping[str, object]],
    weights: Mapping[str, float],
) -> dict[str, list[dict[str, object]]]:
    unique: dict[tuple[str, str, str, str], dict[str, object]] = {}
    for row in specialized_rows:
        ticker = str(row["ticker"])
        metric_id = str(row["metric_id"])
        period_end = str(row["period_end"])
        value = str(row["value"])
        unique[(ticker, metric_id, period_end, value)] = {
            "ticker": ticker,
            "metric_id": metric_id,
            "period_end": date.fromisoformat(period_end),
            "availability_date": date.fromisoformat(str(row["filing_date"])),
            "value": float(value),
        }
    history: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for record in unique.values():
        history[(str(record["ticker"]), str(record["metric_id"]))].append(record)
    for records in history.values():
        records.sort(key=lambda item: (item["period_end"], item["availability_date"]))

    output = {
        "tce_rate_yoy_growth": [],
        "revenue_days_yoy_growth": [],
    }
    source_to_feature = {
        "tce_day_rate": "tce_rate_yoy_growth",
        "revenue_days": "revenue_days_yoy_growth",
    }
    for row in panel_rows:
        if str(row.get("calibration_cohort")) != TANKER_COHORT:
            continue
        if str(row.get("horizon_sessions")) != "63":
            continue
        if str(row.get("calibration_eligible_flag")) != "1":
            continue
        if str(row.get("outcome_available_flag")) != "1":
            continue
        baseline = weighted_score(row, weights, require_complete=False)
        outcome = number(row.get("forward_excess_return"))
        if baseline is None or outcome is None:
            continue
        asof = str(row["asof_date"])
        asof_date = date.fromisoformat(asof)
        ticker = str(row["ticker"])
        for source_metric, feature_id in source_to_feature.items():
            available = [
                item for item in history.get((ticker, source_metric), [])
                if item["availability_date"] <= asof_date
                and item["period_end"] <= asof_date
            ]
            if not available:
                continue
            current = max(
                available, key=lambda item: (item["period_end"], item["availability_date"])
            )
            current_period_end = current["period_end"]
            current_value = float(current["value"])
            candidates = [
                item for item in available
                if 300 <= (current_period_end - item["period_end"]).days <= 430
            ]
            if not candidates:
                continue
            prior = min(
                candidates,
                key=lambda item: (
                    abs((current_period_end - item["period_end"]).days - 365),
                    -item["period_end"].toordinal(),
                ),
            )
            prior_value = float(prior["value"])
            if prior_value == 0:
                continue
            output[feature_id].append(
                {
                    "asof_date": asof,
                    "exit_date": str(row["benchmark_exit_date"]),
                    "ticker": ticker,
                    "baseline_score": baseline,
                    "metric_value": current_value / prior_value - 1.0,
                    "outcome": outcome,
                }
            )
    return output


def markdown(payload: Mapping[str, object]) -> str:
    lines = [
        "# Transportation v7 accepted-metric incremental proof",
        "",
        "This replay used only already accepted, point-in-time facts and the frozen v6 outcome panel. "
        "It made zero network requests and invoked no parser.",
        "",
        "| Cohort | Metric | Dates | Incremental IC | Non-overlap delta | Incremental spread | Gate |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for row in payload["results"]:
        lines.append(
            f"| {row['cohort_id']} | {row['metric_id']} | {row['ic_dates']} | "
            f"{row['incremental_mean_ic'] if row['incremental_mean_ic'] is not None else 'n/a'} | "
            f"{row['nonoverlap_incremental_ic'] if row['nonoverlap_incremental_ic'] is not None else 'n/a'} | "
            f"{row['incremental_top_minus_domain'] if row['incremental_top_minus_domain'] is not None else 'n/a'} | "
            f"{row['proof_gate']} |"
        )
    lines.extend(
        [
            "",
            f"**Decision:** {payload['decision']}",
            "",
            "Passing this diagnostic can authorize one frozen, targeted missing-cell queue; it cannot "
            "authorize production because these outcomes were revealed before the v7 design freeze.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    required = (
        args.outcome_panel, args.semantic_manifest, args.config,
        args.research_decision, args.protocol,
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError(f"missing accepted-proof inputs={missing}")
    decision = read_json(args.research_decision)
    if str(decision.get("decision")) != "APPROVE_RESEARCH_SPEC_AND_ACCEPTED_FACT_REPLAY_ONLY":
        raise ValueError("accepted-fact replay is not authorized by the v7 decision")
    panel_rows = read_csv(args.outcome_panel)
    protocol = read_json(args.protocol)
    specialized_rows = accepted_specialized_rows(
        args.semantic_manifest,
        config_path=args.config,
        db_override=args.db,
    )
    surface_registry = protocol["candidate_registries"][SURFACE_COHORT]
    tanker_registry = protocol["candidate_registries"][TANKER_COHORT]
    surface_weights = surface_registry["candidates"]["surface_balanced_v5"]
    tanker_weights = tanker_registry["candidates"]["tanker_quality_fleet_v1"]

    results: list[dict[str, object]] = []
    for metric_id, observations in surface_observations(
        panel_rows, surface_weights
    ).items():
        results.append(
            evaluate_feature(
                observations,
                cohort_id=SURFACE_COHORT,
                metric_id=metric_id,
                domain="ltl_carriers",
            )
        )
    for metric_id, observations in tanker_growth_observations(
        panel_rows, specialized_rows, tanker_weights
    ).items():
        results.append(
            evaluate_feature(
                observations,
                cohort_id=TANKER_COHORT,
                metric_id=metric_id,
                domain="oil_tankers",
            )
        )
    passed = [row for row in results if str(row["proof_gate"]) == "PASS"]
    payload: dict[str, object] = {
        "contract_version": "transportation_v7_accepted_metric_incremental_proof_v1",
        "evidence_role": "REVEALED_HISTORY_RESOURCE_ALLOCATION_ONLY",
        "decision": (
            "ALLOW_ONE_TIME_TARGETED_QUEUE_FOR_PASSING_METRICS"
            if passed else "DENY_TARGETED_PARSING_NO_INCREMENTAL_PROOF"
        ),
        "passing_metrics": [
            f"{row['cohort_id']}:{row['metric_id']}" for row in passed
        ],
        "production_activation_authorized": False,
        "historical_recalibration_authorized": False,
        "results": results,
        "lineage": {
            "outcome_panel_path": str(args.outcome_panel.resolve()),
            "outcome_panel_sha256": file_sha256(args.outcome_panel),
            "semantic_manifest_path": str(args.semantic_manifest.resolve()),
            "semantic_manifest_sha256": file_sha256(args.semantic_manifest),
            "research_decision_path": str(args.research_decision.resolve()),
            "research_decision_sha256": file_sha256(args.research_decision),
        },
        "network_requests": 0,
        "parser_invocations": 0,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.output_dir / "transportation_v7_accepted_metric_incremental_proof.csv"
    fact_path = args.output_dir / "transportation_v7_accepted_tanker_fact_slice.csv"
    json_path = args.output_dir / "transportation_v7_accepted_metric_incremental_proof.json"
    md_path = args.output_dir / "TRANSPORTATION_V7_ACCEPTED_METRIC_PROOF.md"
    write_csv_atomic(csv_path, RESULT_FIELDS, results)
    write_csv_atomic(
        fact_path,
        ACCEPTED_FACT_FIELDS,
        [
            {field: row.get(field, "") for field in ACCEPTED_FACT_FIELDS}
            for row in specialized_rows
        ],
    )
    payload["lineage"]["accepted_tanker_fact_slice_path"] = str(fact_path.resolve())
    payload["lineage"]["accepted_tanker_fact_slice_sha256"] = file_sha256(fact_path)
    write_text_atomic(json_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    write_text_atomic(md_path, markdown(payload))
    print(json.dumps({
        "decision": payload["decision"],
        "passing_metrics": payload["passing_metrics"],
        "network_requests": 0,
        "parser_invocations": 0,
        "production_activation_authorized": False,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

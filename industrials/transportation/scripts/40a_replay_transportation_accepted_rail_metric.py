#!/usr/bin/env python3
"""Test accepted rail operating-ratio facts before any historical rebuild."""

from __future__ import annotations

import argparse
import csv
import json
import math
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
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.contemporaneous_metric_coverage import (  # noqa: E402
    availability_date,
    comparison_key,
)
from industrials.transportation.contracts import file_sha256  # noqa: E402


ROOT = PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v5"
DEFAULT_FREEZE = (
    ROOT / "specialized_metric_freeze_v8" / "2026-08-21"
    / "transportation_specialized_metric_freeze.json"
)
DEFAULT_PANEL = (
    ROOT / "outcome_panel_v6" / "2026-08-16"
    / "transportation_v5_outcome_panel.csv"
)
DEFAULT_PROTOCOL = (
    ROOT / "research_protocol_v6" / "2026-08-16"
    / "transportation_v5_research_protocol.json"
)
DEFAULT_DECISION = (
    ROOT / "research_decision_v7" / "2026-08-21"
    / "transportation_v7_research_decision.json"
)
DEFAULT_REPLAY = (
    PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v3"
    / "surface_delta" / "2026-08-21"
    / "transportation_surface_semantic_replay_accepted.csv"
)
DEFAULT_OUTPUT = ROOT / "accepted_metric_proof_v8" / "2026-08-21"
COHORT = "north_american_surface_freight_and_logistics_v5"
TICKERS = ("CNI", "CP", "CSX", "NSC", "UNP")
BLOCKS = (
    ("diagnostic_block_1", "2019-01-01", "2021-12-31"),
    ("diagnostic_block_2", "2022-01-01", "2023-12-31"),
    ("diagnostic_block_3", "2024-01-01", "2026-07-30"),
)
RESULT_FIELDS = (
    "cohort_id", "metric_id", "comparison_domain", "observed_rows", "ic_dates",
    "average_names", "baseline_mean_ic", "metric_mean_ic", "combined_mean_ic",
    "incremental_mean_ic", "nonoverlap_baseline_ic", "nonoverlap_combined_ic",
    "nonoverlap_incremental_ic", "baseline_top_minus_domain",
    "combined_top_minus_domain", "incremental_top_minus_domain",
    "positive_increment_blocks", "proof_gate",
)
FACT_FIELDS = (
    "candidate_key", "ticker", "metric_id", "value", "unit", "period_start",
    "period_end", "filing_date", "accepted_at", "accession_number",
    "comparability_class", "definition_basis", "source_document",
    "source_content_sha256", "replay_status",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", type=Path, default=DEFAULT_FREEZE)
    parser.add_argument("--panel", type=Path, default=DEFAULT_PANEL)
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--research-decision", type=Path, default=DEFAULT_DECISION)
    parser.add_argument("--surface-replay", type=Path, default=DEFAULT_REPLAY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
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
    clean = [float(value) for value in values if value is not None]
    return sum(clean) / len(clean) if clean else None


def zscores(values: Sequence[float]) -> list[float]:
    center = sum(values) / len(values)
    scale = statistics.pstdev(values)
    return (
        [(value - center) / scale for value in values]
        if scale > 0
        else [0.0] * len(values)
    )


def block_id(asof: str) -> str:
    matches = [name for name, start, end in BLOCKS if start <= asof <= end]
    if len(matches) != 1:
        raise ValueError(f"{asof}: not in exactly one diagnostic block")
    return matches[0]


def nonoverlap(rows: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    selected: list[Mapping[str, object]] = []
    last_exit: date | None = None
    for row in sorted(rows, key=lambda item: str(item["asof_date"])):
        asof = date.fromisoformat(str(row["asof_date"]))
        exit_date = date.fromisoformat(str(row["exit_date"]))
        if last_exit is not None and asof <= last_exit:
            continue
        selected.append(row)
        last_exit = exit_date
    return selected


def accepted_records(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, list[dict[str, object]]]:
    records: dict[str, list[dict[str, object]]] = defaultdict(list)
    seen: set[tuple[str, str, str, str, str]] = set()
    for row in rows:
        ticker = str(row.get("ticker") or "").upper()
        if ticker not in TICKERS or str(row.get("metric_id") or "") != "operating_ratio":
            continue
        if str(row.get("replay_status") or "").upper() != "ACCEPTED":
            continue
        value = number(row.get("value"))
        available = availability_date(row)
        period_end_text = str(row.get("period_end") or "")[:10]
        if value is None or available is None or not period_end_text:
            continue
        period_end = date.fromisoformat(period_end_text)
        identity = (
            ticker,
            period_end_text,
            available.isoformat(),
            str(row.get("comparability_class") or ""),
            str(value),
        )
        if identity in seen:
            continue
        seen.add(identity)
        records[ticker].append(
            {
                **dict(row),
                "ticker": ticker,
                "value": value,
                "availability_date": available,
                "period_end_date": period_end,
                "comparison_key": comparison_key(row),
            }
        )
    for values in records.values():
        values.sort(
            key=lambda row: (
                row["availability_date"],
                row["period_end_date"],
                str(row.get("candidate_key") or ""),
            )
        )
    return records


def feature_values(
    records: Mapping[str, Sequence[Mapping[str, object]]],
    *,
    asof: date,
) -> tuple[dict[str, float], dict[str, float]]:
    latest: dict[str, Mapping[str, object]] = {}
    for ticker in TICKERS:
        available = [
            row
            for row in records.get(ticker, ())
            if row["availability_date"] <= asof
            and row["period_end_date"] <= asof
            and (asof - row["period_end_date"]).days <= 550
        ]
        if available:
            latest[ticker] = max(
                available,
                key=lambda row: (
                    row["availability_date"],
                    row["period_end_date"],
                    str(row.get("candidate_key") or ""),
                ),
            )
    by_definition: dict[tuple[str, ...], set[str]] = defaultdict(set)
    for ticker, row in latest.items():
        by_definition[row["comparison_key"]].add(ticker)
    if not by_definition:
        return {}, {}
    selected_key, selected_tickers = max(
        by_definition.items(), key=lambda item: (len(item[1]), item[0])
    )
    levels = {
        ticker: -float(latest[ticker]["value"])
        for ticker in selected_tickers
    }
    improvements: dict[str, float] = {}
    for ticker in selected_tickers:
        current = latest[ticker]
        candidates = [
            row
            for row in records.get(ticker, ())
            if row["comparison_key"] == selected_key
            and row["availability_date"] <= asof
            and row["period_end_date"] < current["period_end_date"]
            and 300
            <= (current["period_end_date"] - row["period_end_date"]).days
            <= 430
        ]
        if not candidates:
            continue
        prior = min(
            candidates,
            key=lambda row: (
                abs(
                    (current["period_end_date"] - row["period_end_date"]).days
                    - 365
                ),
                -row["period_end_date"].toordinal(),
            ),
        )
        prior_value = float(prior["value"])
        if prior_value != 0:
            improvements[ticker] = (
                prior_value - float(current["value"])
            ) / abs(prior_value)
    return levels, improvements


def build_observations(
    panel_rows: Sequence[Mapping[str, object]],
    records: Mapping[str, Sequence[Mapping[str, object]]],
    *,
    weights: Mapping[str, float],
) -> dict[str, list[dict[str, object]]]:
    output = {
        "operating_ratio_level": [],
        "operating_ratio_yoy_improvement": [],
    }
    by_date: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in panel_rows:
        if str(row.get("calibration_cohort") or "") != COHORT:
            continue
        if str(row.get("ticker") or "") not in TICKERS:
            continue
        if str(row.get("horizon_sessions") or "") != "63":
            continue
        if str(row.get("calibration_eligible_flag") or "") != "1":
            continue
        if str(row.get("outcome_available_flag") or "") != "1":
            continue
        by_date[str(row["asof_date"])].append(row)
    for asof, rows in sorted(by_date.items()):
        level, improvement = feature_values(
            records, asof=date.fromisoformat(asof)
        )
        for feature_id, values in (
            ("operating_ratio_level", level),
            ("operating_ratio_yoy_improvement", improvement),
        ):
            for row in rows:
                ticker = str(row["ticker"])
                baseline = weighted_score(row, weights, require_complete=False)
                outcome = number(row.get("forward_excess_return"))
                if ticker not in values or baseline is None or outcome is None:
                    continue
                output[feature_id].append(
                    {
                        "asof_date": asof,
                        "exit_date": str(row["benchmark_exit_date"]),
                        "ticker": ticker,
                        "baseline_score": baseline,
                        "metric_value": values[ticker],
                        "outcome": outcome,
                    }
                )
    return output


def evaluate(
    observations: Sequence[Mapping[str, object]],
    *,
    metric_id: str,
) -> dict[str, object]:
    by_date: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in observations:
        by_date[str(row["asof_date"])].append(row)
    periods: list[dict[str, object]] = []
    observed_rows = 0
    for asof, rows in sorted(by_date.items()):
        if len(rows) < 4:
            continue
        observed_rows += len(rows)
        baseline = [float(row["baseline_score"]) for row in rows]
        feature = [float(row["metric_value"]) for row in rows]
        outcomes = [float(row["outcome"]) for row in rows]
        combined = [
            0.8 * base + 0.2 * metric
            for base, metric in zip(zscores(baseline), zscores(feature))
        ]

        def top_minus(scores: Sequence[float]) -> float:
            ranked = sorted(
                zip(scores, outcomes, [str(row["ticker"]) for row in rows]),
                key=lambda item: (-item[0], item[2]),
            )
            return ranked[0][1] - sum(outcomes) / len(outcomes)

        baseline_ic = spearman(baseline, outcomes)
        combined_ic = spearman(combined, outcomes)
        periods.append(
            {
                "asof_date": asof,
                "exit_date": str(rows[0]["exit_date"]),
                "names": len(rows),
                "baseline_ic": baseline_ic,
                "metric_ic": spearman(feature, outcomes),
                "combined_ic": combined_ic,
                "incremental_ic": (
                    float(combined_ic) - float(baseline_ic)
                    if combined_ic is not None and baseline_ic is not None
                    else None
                ),
                "baseline_spread": top_minus(baseline),
                "combined_spread": top_minus(combined),
            }
        )
    overlap_free = nonoverlap(periods)
    incremental_ic = mean(number(row.get("incremental_ic")) for row in periods)
    nonoverlap_delta = mean(
        number(row.get("incremental_ic")) for row in overlap_free
    )
    baseline_spread = mean(number(row.get("baseline_spread")) for row in periods)
    combined_spread = mean(number(row.get("combined_spread")) for row in periods)
    spread_delta = (
        combined_spread - baseline_spread
        if baseline_spread is not None and combined_spread is not None
        else None
    )
    block_deltas = {
        name: mean(
            number(row.get("incremental_ic"))
            for row in periods
            if block_id(str(row["asof_date"])) == name
        )
        for name, _, _ in BLOCKS
    }
    positive_blocks = sum(
        value is not None and value > 0 for value in block_deltas.values()
    )
    passed = (
        len(periods) >= 24
        and incremental_ic is not None
        and incremental_ic > 0
        and nonoverlap_delta is not None
        and nonoverlap_delta > 0
        and spread_delta is not None
        and spread_delta > 0
        and positive_blocks >= 2
    )
    return {
        "cohort_id": COHORT,
        "metric_id": metric_id,
        "comparison_domain": "rail_networks",
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
        "incremental_top_minus_domain": spread_delta,
        "positive_increment_blocks": positive_blocks,
        "proof_gate": "PASS" if passed else "FAIL",
    }


def markdown(payload: Mapping[str, object]) -> str:
    lines = [
        "# Transportation v8 accepted rail-metric proof",
        "",
        "This diagnostic used the immutable v6 outcome panel and the newly accepted "
        "rail operating-ratio facts. It performed no fetch, parse, database write, "
        "historical feature rebuild, or calibration.",
        "",
        "| Feature | Dates | Incremental IC | Non-overlap delta | Spread delta | Gate |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for row in payload["results"]:
        lines.append(
            f"| {row['metric_id']} | {row['ic_dates']} | "
            f"{row['incremental_mean_ic']} | {row['nonoverlap_incremental_ic']} | "
            f"{row['incremental_top_minus_domain']} | {row['proof_gate']} |"
        )
    lines.extend(
        [
            "",
            f"**Decision:** {payload['decision']}",
            "",
            "These outcomes are revealed and can only decide research resource "
            "allocation. They cannot authorize production.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    paths = {
        "freeze": args.freeze.resolve(),
        "panel": args.panel.resolve(),
        "protocol": args.protocol.resolve(),
        "research_decision": args.research_decision.resolve(),
        "surface_replay": args.surface_replay.resolve(),
    }
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"missing accepted-proof inputs={missing}")
    freeze = read_json(paths["freeze"])
    decision = read_json(paths["research_decision"])
    if (
        freeze.get("acceptance") != "PASS"
        or not freeze.get("accepted_fact_incremental_proof_authorized")
        or freeze.get("full_historical_feature_rebuild_authorized")
    ):
        raise ValueError("accepted-metric freeze does not authorize no-rebuild proof")
    if (
        decision.get("decision")
        != "APPROVE_RESEARCH_SPEC_AND_ACCEPTED_FACT_REPLAY_ONLY"
        or decision.get("research_specification", {}).get("production_authority")
        is not False
    ):
        raise ValueError("v7 research decision is not fail-closed")

    protocol = read_json(paths["protocol"])
    weights = protocol["candidate_registries"][COHORT]["candidates"][
        "surface_balanced_v5"
    ]
    replay_rows = read_csv(paths["surface_replay"])
    records = accepted_records(replay_rows)
    observations = build_observations(
        read_csv(paths["panel"]), records, weights=weights
    )
    results = [
        evaluate(rows, metric_id=metric_id)
        for metric_id, rows in observations.items()
    ]
    improvement = next(
        row for row in results
        if row["metric_id"] == "operating_ratio_yoy_improvement"
    )
    improvement_passed = improvement["proof_gate"] == "PASS"
    payload: dict[str, object] = {
        "acceptance": "PASS",
        "contract_version": "transportation_v8_accepted_rail_metric_proof_v1",
        "evidence_role": "REVEALED_HISTORY_RESOURCE_ALLOCATION_ONLY",
        "decision": (
            "ALLOW_FUTURE_ONLY_RAIL_IMPROVEMENT_FEATURE"
            if improvement_passed
            else "DENY_RAIL_OPERATING_RATIO_FEATURE_NO_INCREMENTAL_PROOF"
        ),
        "results": results,
        "passing_metrics": [
            str(row["metric_id"]) for row in results
            if row["proof_gate"] == "PASS"
        ],
        "historical_feature_rebuild_authorized": False,
        "historical_recalibration_authorized": False,
        "production_activation_authorized": False,
        "network_requests": 0,
        "parser_invocations": 0,
        "database_mutations": 0,
        "next_gate": (
            "FREEZE_V8_FUTURE_ONLY_SCORE_SPECIFICATION"
            if improvement_passed
            else "RETAIN_V7_FUTURE_ONLY_BASELINE_WITHOUT_RAIL_FEATURE"
        ),
        "lineage": {
            label: {"path": str(path), "sha256": file_sha256(path)}
            for label, path in paths.items()
        },
    }
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / "transportation_v8_accepted_rail_metric_proof.csv"
    fact_path = output_dir / "transportation_v8_accepted_rail_fact_slice.csv"
    manifest_path = output_dir / "transportation_v8_accepted_rail_metric_proof.json"
    markdown_path = output_dir / "TRANSPORTATION_V8_ACCEPTED_RAIL_METRIC_PROOF.md"
    write_csv_atomic(result_path, RESULT_FIELDS, results)
    write_csv_atomic(
        fact_path,
        FACT_FIELDS,
        [
            {field: row.get(field, "") for field in FACT_FIELDS}
            for row in replay_rows
            if str(row.get("ticker") or "") in TICKERS
            and str(row.get("metric_id") or "") == "operating_ratio"
            and str(row.get("replay_status") or "") == "ACCEPTED"
        ],
    )
    payload["artifacts"] = {
        "result": {"path": str(result_path), "sha256": file_sha256(result_path)},
        "accepted_fact_slice": {
            "path": str(fact_path),
            "sha256": file_sha256(fact_path),
        },
    }
    write_text_atomic(
        manifest_path, json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )
    write_text_atomic(markdown_path, markdown(payload))
    print(
        json.dumps(
            {
                "decision": payload["decision"],
                "results": results,
                "historical_feature_rebuild_authorized": False,
                "historical_recalibration_authorized": False,
                "production_activation_authorized": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

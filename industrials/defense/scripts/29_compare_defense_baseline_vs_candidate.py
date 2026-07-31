#!/usr/bin/env python3
"""Validate and compare matched defense baseline/candidate research runs."""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.defense.research_artifacts import (  # noqa: E402
    PILLAR_SCORE_FIELDS,
    as_float,
    normalize_weights,
    sha256_file,
    spearman,
    utc_now,
    weighted_score,
    write_json_atomic,
)

REPORT_FIELDS = ["gate", "status", "baseline", "candidate", "detail"]
COMPARABLE_MANIFEST_FIELDS = [
    "snapshot_cadence",
    "weekly_start_date",
    "weekly_selection",
    "snapshot_count",
    "snapshot_start_date",
    "snapshot_end_date",
    "forward_days",
    "embargo_days",
    "benchmark_ticker",
    "price_source_order",
    "evaluation_calendar_sha256",
]
OUTCOME_FIELDS = [
    "price_ticker",
    "price_source_id",
    "price_basis",
    "price_adjustment",
    "price_asof_date",
    "price_forward_date",
    "forward_days",
    "forward_return",
    "benchmark_ticker",
    "benchmark_price_source_id",
    "benchmark_price_basis",
    "benchmark_asof_date",
    "benchmark_forward_date",
    "benchmark_forward_return",
    "forward_excess_return_vs_sector",
    "return_available_flag",
    "return_unavailable_reason",
]
BASELINE_PILLAR_FIELDS = [
    field for field in PILLAR_SCORE_FIELDS if field != "defense_budget_backlog_score"
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare matched defense baseline and candidate research artifacts.")
    parser.add_argument("--candidate-label", required=True)
    parser.add_argument(
        "--baseline-label",
        default="",
        help="Matched baseline candidate namespace. Blank retains the legacy production-baseline location.",
    )
    parser.add_argument(
        "--baseline-stage8",
        type=Path,
        default=PROJECT_ROOT / "output" / "industrials" / "defense" / "stage8",
    )
    parser.add_argument(
        "--baseline-stage9",
        type=Path,
        default=PROJECT_ROOT / "output" / "industrials" / "defense" / "stage9",
    )
    parser.add_argument(
        "--selection-count-tolerance",
        type=float,
        default=0.35,
        help="Allowed fractional change in mean selected_count.",
    )
    parser.add_argument("--bootstrap-samples", type=int, default=20_000)
    parser.add_argument(
        "--bootstrap-block-periods",
        type=int,
        default=13,
        help="Moving-block length for weekly 63-trading-day forward outcomes.",
    )
    parser.add_argument("--bootstrap-seed", type=int, default=20_260_727)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [{str(key): str(value or "") for key, value in row.items()} for row in csv.DictReader(handle)]


def read_summary_csv(path: Path) -> dict[str, str]:
    rows = read_csv_rows(path) if path.exists() else []
    return rows[0] if rows else {}


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def namespace_paths(stage8: Path, stage9: Path) -> dict[str, Path]:
    return {
        "panel_csv": stage8 / "oos_calibration_panel_weekly" / "defense_oos_calibration_panel.csv",
        "panel_manifest": (
            stage8 / "oos_calibration_panel_weekly" / "defense_oos_calibration_panel_manifest.json"
        ),
        "splits_csv": stage8 / "oos_calibration_panel_weekly" / "defense_oos_calibration_splits.csv",
        "calibration_summary": (
            stage8 / "optuna_calibration_weekly" / "defense_optuna_calibration_summary.csv"
        ),
        "calibration_trials": (
            stage8 / "optuna_calibration_weekly" / "defense_optuna_calibration_trials.csv"
        ),
        "calibration_manifest": (
            stage8 / "optuna_calibration_weekly" / "defense_optuna_calibration_manifest.json"
        ),
        "backtest_summary": stage9 / "score_backtest_weekly" / "defense_score_backtest_summary.csv",
        "backtest_periods": stage9 / "score_backtest_weekly" / "defense_score_backtest_periods.csv",
        "backtest_manifest": stage9 / "score_backtest_weekly" / "defense_score_backtest_manifest.json",
    }


def labeled_roots(stage8: Path, stage9: Path, label: str) -> tuple[Path, Path]:
    clean = label.strip().lower()
    if not clean:
        return stage8, stage9
    return stage8 / "candidates" / clean, stage9 / "candidates" / clean


def panel_index(rows: list[dict[str, str]]) -> tuple[dict[tuple[str, str], dict[str, str]], list[str]]:
    out: dict[tuple[str, str], dict[str, str]] = {}
    duplicates: list[str] = []
    for row in rows:
        key = (str(row.get("ticker") or ""), str(row.get("asof_date") or ""))
        if key in out:
            duplicates.append(f"{key[0]}@{key[1]}")
        out[key] = row
    return out, duplicates


def first_mismatches(
    baseline: dict[tuple[str, str], dict[str, str]],
    candidate: dict[tuple[str, str], dict[str, str]],
    fields: list[str],
    *,
    limit: int = 10,
) -> list[str]:
    mismatches: list[str] = []
    for key in sorted(set(baseline) & set(candidate)):
        for field in fields:
            if str(baseline[key].get(field) or "") != str(candidate[key].get(field) or ""):
                mismatches.append(f"{key[0]}@{key[1]}:{field}")
                if len(mismatches) >= limit:
                    return mismatches
    return mismatches


def source_dates(manifest: dict[str, Any]) -> list[str]:
    return [
        str(item.get("asof_date") or "")
        for item in manifest.get("source_snapshots", [])
        if isinstance(item, dict)
    ]


def source_isolation_issues(manifest: dict[str, Any]) -> list[str]:
    return [
        str(item.get("asof_date") or "")
        for item in manifest.get("source_snapshots", [])
        if isinstance(item, dict)
        and (
            item.get("research_candidate") is not True
            or item.get("shadow_only") is not True
            or item.get("production_promoted") is True
        )
    ]


def mean_selected_count(path: Path) -> float | None:
    counts = [
        value
        for row in read_csv_rows(path)
        if (value := as_float(row.get("selected_count"))) is not None
    ]
    return sum(counts) / len(counts) if counts else None


def summary_weights(summary: dict[str, str]) -> dict[str, float]:
    raw = str(summary.get("best_weights_json") or "").strip()
    if not raw:
        raise ValueError("Calibration summary is missing best_weights_json")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("best_weights_json must contain an object")
    return normalize_weights({str(key): float(value) for key, value in payload.items()})


def evaluation_metrics(
    rows: list[dict[str, str]],
    weights: dict[str, float],
    *,
    split_name: str,
    neutralize_demand: bool = False,
    top_quantile: float = 0.20,
    min_positions: int = 5,
) -> dict[str, float | int | None]:
    grouped: dict[str, list[tuple[float, str, float]]] = defaultdict(list)
    for row in rows:
        if str(row.get("panel_row_eligible_flag") or "") != "1":
            continue
        if str(row.get("split_name") or "") != split_name:
            continue
        score_row = row
        if neutralize_demand:
            score_row = dict(row)
            score_row["defense_budget_backlog_score"] = "50"
        score = weighted_score(score_row, weights)
        outcome = as_float(row.get("forward_excess_return_vs_sector"))
        if score is None or outcome is None:
            continue
        grouped[str(row.get("asof_date") or "")].append(
            (score, str(row.get("ticker") or ""), outcome)
        )
    period_ics: list[float] = []
    top_excess: list[float] = []
    for pairs in grouped.values():
        period_ic = spearman(
            [score for score, _, _ in pairs],
            [outcome for _, _, outcome in pairs],
        )
        if period_ic is not None and math.isfinite(period_ic):
            period_ics.append(period_ic)
        pairs.sort(key=lambda item: (-item[0], item[1]))
        selected_count = min(
            len(pairs),
            max(min_positions, int(math.ceil(len(pairs) * top_quantile))),
        )
        if selected_count:
            top_excess.append(
                sum(outcome for _, _, outcome in pairs[:selected_count])
                / selected_count
            )
    return {
        "ic": sum(period_ics) / len(period_ics) if period_ics else None,
        "top_quantile_excess": (
            sum(top_excess) / len(top_excess) if top_excess else None
        ),
        "periods": len(top_excess),
    }


def trial_weight_bank(path: Path) -> list[tuple[str, str]]:
    rows = read_csv_rows(path)
    return sorted(
        (
            str(row.get("trial_number") or ""),
            str(
                row.get("proposal_weights_json")
                or row.get("weights_json")
                or ""
            ),
        )
        for row in rows
    )


def period_excess(path: Path, *, split_name: str) -> dict[str, float]:
    return {
        str(row.get("asof_date") or ""): value
        for row in read_csv_rows(path)
        if str(row.get("split_name") or "") == split_name
        and (value := as_float(row.get("selected_excess_vs_benchmark"))) is not None
    }


def moving_block_bootstrap_mean_delta(
    baseline: dict[str, float],
    candidate: dict[str, float],
    *,
    samples: int,
    block_periods: int,
    seed: int,
) -> dict[str, float | int]:
    dates = sorted(set(baseline) & set(candidate))
    deltas = [candidate[asof] - baseline[asof] for asof in dates]
    if not deltas:
        raise ValueError("No paired period excess returns are available")
    block = min(block_periods, len(deltas))
    rng = random.Random(seed)
    boot_means: list[float] = []
    for _ in range(samples):
        sampled: list[float] = []
        while len(sampled) < len(deltas):
            start = rng.randrange(0, len(deltas) - block + 1)
            sampled.extend(deltas[start : start + block])
        boot_means.append(sum(sampled[: len(deltas)]) / len(deltas))
    boot_means.sort()
    lower_index = max(0, int(math.floor(samples * 0.025)))
    upper_index = min(samples - 1, int(math.ceil(samples * 0.975)) - 1)
    return {
        "paired_periods": len(deltas),
        "mean_delta": sum(deltas) / len(deltas),
        "win_rate": sum(delta > 0.0 for delta in deltas) / len(deltas),
        "block_periods": block,
        "ci_95_lower": boot_means[lower_index],
        "ci_95_upper": boot_means[upper_index],
        "probability_positive": (
            sum(value > 0.0 for value in boot_means) / len(boot_means)
        ),
    }


def main() -> int:
    args = parse_args()
    if args.bootstrap_samples <= 0:
        raise ValueError("--bootstrap-samples must be positive")
    if args.bootstrap_block_periods <= 0:
        raise ValueError("--bootstrap-block-periods must be positive")
    base8 = args.baseline_stage8.expanduser().resolve()
    base9 = args.baseline_stage9.expanduser().resolve()
    baseline8, baseline9 = labeled_roots(base8, base9, args.baseline_label)
    candidate8, candidate9 = labeled_roots(base8, base9, args.candidate_label)
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else candidate8 / "baseline_vs_candidate"
    )
    baseline = namespace_paths(baseline8, baseline9)
    candidate = namespace_paths(candidate8, candidate9)
    missing = [
        f"{side}:{name}:{path}"
        for side, paths in [("baseline", baseline), ("candidate", candidate)]
        for name, path in paths.items()
        if not path.exists()
    ]
    if missing:
        raise FileNotFoundError("Matched research namespace incomplete: " + "; ".join(missing[:8]))

    b_cal = read_summary_csv(baseline["calibration_summary"])
    c_cal = read_summary_csv(candidate["calibration_summary"])
    b_bt = read_summary_csv(baseline["backtest_summary"])
    c_bt = read_summary_csv(candidate["backtest_summary"])
    b_manifest = read_json(baseline["panel_manifest"])
    c_manifest = read_json(candidate["panel_manifest"])
    b_panel_rows = read_csv_rows(baseline["panel_csv"])
    c_panel_rows = read_csv_rows(candidate["panel_csv"])
    b_index, b_duplicates = panel_index(b_panel_rows)
    c_index, c_duplicates = panel_index(c_panel_rows)

    rows: list[dict[str, str]] = []
    failures: list[str] = []

    def gate(
        name: str,
        ok: bool | None,
        baseline_value: object,
        candidate_value: object,
        detail: str = "",
        *,
        required: bool = True,
    ) -> None:
        status = "PASS" if ok else ("WARN" if ok is None or not required else "FAIL")
        if ok is False and required:
            failures.append(name)
        rows.append(
            {
                "gate": name,
                "status": status,
                "baseline": "" if baseline_value is None else str(baseline_value),
                "candidate": "" if candidate_value is None else str(candidate_value),
                "detail": detail,
            }
        )

    gate(
        "panel_keys_unique",
        not b_duplicates and not c_duplicates,
        len(b_duplicates),
        len(c_duplicates),
        f"baseline={b_duplicates[:5]} candidate={c_duplicates[:5]}",
    )
    b_dates = source_dates(b_manifest)
    c_dates = source_dates(c_manifest)
    gate(
        "exact_snapshot_calendar_match",
        b_dates == c_dates and bool(b_dates),
        f"{len(b_dates)}:{b_dates[:1]}..{b_dates[-1:]}",
        f"{len(c_dates)}:{c_dates[:1]}..{c_dates[-1:]}",
    )
    setting_mismatches = [
        field
        for field in COMPARABLE_MANIFEST_FIELDS
        if b_manifest.get(field) != c_manifest.get(field)
    ]
    gate(
        "experiment_settings_match",
        not setting_mismatches,
        "matched" if not setting_mismatches else setting_mismatches,
        "matched" if not setting_mismatches else setting_mismatches,
    )
    calibration_settings = [
        "search_method",
        "search_metric",
        "selection_metric",
        "trial_count",
        "top_quantile",
        "min_positions",
    ]
    calibration_setting_mismatches = [
        field for field in calibration_settings if b_cal.get(field) != c_cal.get(field)
    ]
    gate(
        "calibration_settings_match",
        not calibration_setting_mismatches,
        "matched" if not calibration_setting_mismatches else calibration_setting_mismatches,
        "matched" if not calibration_setting_mismatches else calibration_setting_mismatches,
    )
    b_trial_bank = trial_weight_bank(baseline["calibration_trials"])
    c_trial_bank = trial_weight_bank(candidate["calibration_trials"])
    gate(
        "exact_trial_weight_bank_match",
        bool(b_trial_bank) and b_trial_bank == c_trial_bank,
        len(b_trial_bank),
        len(c_trial_bank),
        "Matched random Optuna proposals isolate score-input effects from search-path effects.",
    )
    gate(
        "frozen_calendar_hash_present",
        bool(b_manifest.get("evaluation_calendar_sha256"))
        and b_manifest.get("evaluation_calendar_sha256") == c_manifest.get("evaluation_calendar_sha256"),
        b_manifest.get("evaluation_calendar_sha256"),
        c_manifest.get("evaluation_calendar_sha256"),
    )
    gate(
        "exact_panel_key_match",
        set(b_index) == set(c_index) and bool(b_index),
        len(b_index),
        len(c_index),
        (
            f"baseline_only={len(set(b_index) - set(c_index))} "
            f"candidate_only={len(set(c_index) - set(b_index))}"
        ),
    )
    split_mismatches = first_mismatches(b_index, c_index, ["split_name"])
    gate("exact_split_assignment_match", not split_mismatches, "same", "same", str(split_mismatches))
    eligibility_mismatches = first_mismatches(
        b_index,
        c_index,
        ["panel_row_eligible_flag", "panel_row_eligible_reason"],
    )
    gate(
        "exact_eligibility_sample_match",
        not eligibility_mismatches,
        sum(row.get("panel_row_eligible_flag") == "1" for row in b_panel_rows),
        sum(row.get("panel_row_eligible_flag") == "1" for row in c_panel_rows),
        str(eligibility_mismatches),
    )
    outcome_mismatches = first_mismatches(b_index, c_index, OUTCOME_FIELDS)
    gate("exact_forward_outcomes_match", not outcome_mismatches, "same", "same", str(outcome_mismatches))
    baseline_pillar_mismatches = first_mismatches(
        b_index,
        c_index,
        [*BASELINE_PILLAR_FIELDS, "final_score", "rank_ready_flag", "model_status"],
    )
    gate(
        "noncandidate_scores_and_gates_match",
        not baseline_pillar_mismatches,
        "same",
        "same",
        str(baseline_pillar_mismatches),
    )

    baseline_modes_ok = (
        b_manifest.get("scoring_mode") == "baseline"
        and b_manifest.get("research_candidate") is True
        and not source_isolation_issues(b_manifest)
    )
    candidate_modes_ok = (
        c_manifest.get("scoring_mode") == "specialized_v1"
        and c_manifest.get("research_candidate") is True
        and not source_isolation_issues(c_manifest)
    )
    gate(
        "research_namespaces_fail_closed",
        baseline_modes_ok and candidate_modes_ok,
        f"{b_manifest.get('scoring_mode')}:{b_manifest.get('score_model_version')}",
        f"{c_manifest.get('scoring_mode')}:{c_manifest.get('score_model_version')}",
        (
            f"baseline_unsafe={source_isolation_issues(b_manifest)[:5]} "
            f"candidate_unsafe={source_isolation_issues(c_manifest)[:5]}"
        ),
    )
    gate(
        "score_model_versions_distinct",
        bool(b_manifest.get("score_model_version"))
        and bool(c_manifest.get("score_model_version"))
        and b_manifest.get("score_model_version") != c_manifest.get("score_model_version"),
        b_manifest.get("score_model_version"),
        c_manifest.get("score_model_version"),
    )
    baseline_demand_ok = all(
        as_float(row.get("defense_budget_backlog_score")) == 50.0
        and row.get("defense_budget_backlog_status") == "neutralized_not_loaded"
        for row in b_panel_rows
    )
    candidate_loaded = [
        row
        for row in c_panel_rows
        if str(row.get("defense_budget_backlog_status") or "").startswith("candidate_specialized_")
        and str(row.get("defense_budget_backlog_status") or "") != "candidate_specialized_missing_neutralized"
        and as_float(row.get("defense_budget_backlog_score")) is not None
    ]
    candidate_validation_holdout = {
        str(row.get("split_name") or "")
        for row in candidate_loaded
        if str(row.get("split_name") or "") in {"validation", "holdout"}
    }
    distinct_candidate_scores = {
        round(float(score), 8)
        for row in candidate_loaded
        if (score := as_float(row.get("defense_budget_backlog_score"))) is not None
    }
    gate(
        "candidate_signal_is_active",
        baseline_demand_ok
        and len(candidate_loaded) >= 20
        and len(distinct_candidate_scores) >= 3
        and candidate_validation_holdout == {"validation", "holdout"},
        "baseline_neutral" if baseline_demand_ok else "baseline_not_neutral",
        f"loaded={len(candidate_loaded)} distinct={len(distinct_candidate_scores)}",
        f"evaluation_splits={sorted(candidate_validation_holdout)}",
    )

    top_quantile = as_float(b_cal.get("top_quantile")) or 0.20
    min_positions = int(as_float(b_cal.get("min_positions")) or 5)
    baseline_weights = summary_weights(b_cal)
    candidate_weights = summary_weights(c_cal)
    for split in ["validation", "holdout"]:
        baseline_fixed = evaluation_metrics(
            b_panel_rows,
            baseline_weights,
            split_name=split,
            top_quantile=top_quantile,
            min_positions=min_positions,
        )
        candidate_fixed = evaluation_metrics(
            c_panel_rows,
            baseline_weights,
            split_name=split,
            top_quantile=top_quantile,
            min_positions=min_positions,
        )
        candidate_neutral = evaluation_metrics(
            c_panel_rows,
            candidate_weights,
            split_name=split,
            neutralize_demand=True,
            top_quantile=top_quantile,
            min_positions=min_positions,
        )
        candidate_official = evaluation_metrics(
            c_panel_rows,
            candidate_weights,
            split_name=split,
            top_quantile=top_quantile,
            min_positions=min_positions,
        )
        for metric in ["ic", "top_quantile_excess"]:
            baseline_value = as_float(baseline_fixed.get(metric))
            candidate_value = as_float(candidate_fixed.get(metric))
            gate(
                f"candidate_signal_fixed_weights_{split}_{metric}_improves",
                baseline_value is not None
                and candidate_value is not None
                and candidate_value > baseline_value,
                baseline_value,
                candidate_value,
                "Same baseline weights; only the specialized demand-pillar values differ.",
                required=False,
            )
            neutral_value = as_float(candidate_neutral.get(metric))
            official_value = as_float(candidate_official.get(metric))
            gate(
                f"candidate_signal_ablation_{split}_{metric}_improves",
                neutral_value is not None
                and official_value is not None
                and official_value > neutral_value,
                neutral_value,
                official_value,
                "Same candidate weights; candidate demand pillar is neutralized in the baseline value.",
            )

    c_val_ic = as_float(c_cal.get("validation_ic"))
    c_hold_ic = as_float(c_cal.get("holdout_ic"))
    b_val_ic = as_float(b_cal.get("validation_ic"))
    b_hold_ic = as_float(b_cal.get("holdout_ic"))
    gate("candidate_validation_ic_positive", c_val_ic is not None and c_val_ic > 0, b_val_ic, c_val_ic)
    gate("candidate_holdout_ic_positive", c_hold_ic is not None and c_hold_ic > 0, b_hold_ic, c_hold_ic)
    c_hold_exc = as_float(c_bt.get("holdout_mean_excess_vs_benchmark"))
    b_hold_exc = as_float(b_bt.get("holdout_mean_excess_vs_benchmark"))
    gate(
        "candidate_holdout_excess_vs_xar_positive",
        c_hold_exc is not None and c_hold_exc > 0,
        b_hold_exc,
        c_hold_exc,
    )
    gate(
        "candidate_beats_baseline_validation_ic",
        c_val_ic is not None and b_val_ic is not None and c_val_ic > b_val_ic,
        b_val_ic,
        c_val_ic,
        required=(
            b_cal.get("selection_metric") == "validation_ic"
            and c_cal.get("selection_metric") == "validation_ic"
        ),
    )
    selection_metric = str(c_cal.get("selection_metric") or "")
    b_selection_value = as_float(b_cal.get(selection_metric))
    c_selection_value = as_float(c_cal.get(selection_metric))
    gate(
        "candidate_beats_baseline_validation_selection_metric",
        b_selection_value is not None
        and c_selection_value is not None
        and c_selection_value > b_selection_value,
        b_selection_value,
        c_selection_value,
        f"selection_metric={selection_metric}",
    )
    gate(
        "candidate_beats_baseline_holdout_ic",
        c_hold_ic is not None and b_hold_ic is not None and c_hold_ic > b_hold_ic,
        b_hold_ic,
        c_hold_ic,
    )
    gate(
        "candidate_beats_baseline_holdout_excess",
        c_hold_exc is not None and b_hold_exc is not None and c_hold_exc > b_hold_exc,
        b_hold_exc,
        c_hold_exc,
    )
    bootstrap = moving_block_bootstrap_mean_delta(
        period_excess(baseline["backtest_periods"], split_name="holdout"),
        period_excess(candidate["backtest_periods"], split_name="holdout"),
        samples=args.bootstrap_samples,
        block_periods=args.bootstrap_block_periods,
        seed=args.bootstrap_seed,
    )
    gate(
        "candidate_holdout_excess_block_bootstrap_positive",
        as_float(bootstrap.get("ci_95_lower")) is not None
        and float(bootstrap["ci_95_lower"]) > 0.0,
        0.0,
        bootstrap.get("mean_delta"),
        (
            f"95% CI=[{float(bootstrap['ci_95_lower']):.10f},"
            f"{float(bootstrap['ci_95_upper']):.10f}] "
            f"positive_probability={float(bootstrap['probability_positive']):.6f} "
            f"paired_periods={bootstrap['paired_periods']} "
            f"block_periods={bootstrap['block_periods']}"
        ),
    )
    gate(
        "portfolio_aligned_validation_selection",
        c_cal.get("selection_metric") == "validation_top_quantile_excess"
        and b_cal.get("selection_metric") == "validation_top_quantile_excess",
        b_cal.get("selection_metric"),
        c_cal.get("selection_metric"),
    )
    gate(
        "purged_embargo_enforced",
        int(b_manifest.get("embargoed_snapshots") or 0) > 0
        and b_manifest.get("embargoed_snapshots") == c_manifest.get("embargoed_snapshots"),
        b_manifest.get("embargoed_snapshots"),
        c_manifest.get("embargoed_snapshots"),
    )
    b_selected = mean_selected_count(baseline["backtest_periods"])
    c_selected = mean_selected_count(candidate["backtest_periods"])
    if b_selected and c_selected is not None:
        selection_change = abs(c_selected - b_selected) / b_selected
        selection_ok: bool | None = selection_change <= args.selection_count_tolerance
        selection_detail = f"change={selection_change:.6f} tolerance={args.selection_count_tolerance}"
    else:
        selection_ok = None
        selection_detail = "selected_count unavailable"
    gate(
        "selection_count_within_tolerance",
        selection_ok,
        b_selected,
        c_selected,
        selection_detail,
    )
    for split in ["validation", "holdout"]:
        gate(
            f"{split}_cross_sectional_ic_periods_match",
            b_cal.get(f"{split}_ic_periods") == c_cal.get(f"{split}_ic_periods")
            and bool(b_cal.get(f"{split}_ic_periods")),
            b_cal.get(f"{split}_ic_periods"),
            c_cal.get(f"{split}_ic_periods"),
            "IC is the mean of per-snapshot cross-sectional Spearman correlations.",
        )

    promotable = not failures
    output_dir.mkdir(parents=True, exist_ok=True)
    report_csv = output_dir / "defense_baseline_vs_candidate_report.csv"
    write_csv_atomic(report_csv, REPORT_FIELDS, rows)
    manifest = {
        "artifact_family": "defense_baseline_vs_candidate",
        "created_at_utc": utc_now(),
        "baseline_label": args.baseline_label.strip().lower(),
        "candidate_label": args.candidate_label.strip().lower(),
        "comparison_method": "exact_matched_calendar_panel_and_outcome_comparison_v2",
        "promotable_evidence": promotable,
        "failed_gates": failures,
        "warn_gates": [row["gate"] for row in rows if row["status"] == "WARN"],
        "paired_holdout_excess_block_bootstrap": {
            **bootstrap,
            "samples": args.bootstrap_samples,
            "seed": args.bootstrap_seed,
        },
        "inputs": {
            name: {
                "baseline": str(baseline[name]),
                "baseline_sha256": sha256_file(baseline[name]),
                "candidate": str(candidate[name]),
                "candidate_sha256": sha256_file(candidate[name]),
            }
            for name in baseline
        },
        "report_csv": str(report_csv),
        "report_csv_sha256": sha256_file(report_csv),
        "note": (
            "Report-only. Candidate performance is admissible only after exact sample, split, "
            "outcome, provenance, and isolation gates pass."
        ),
    }
    write_json_atomic(output_dir / "defense_baseline_vs_candidate_manifest.json", manifest)
    for row in rows:
        print(
            f"{row['status']:4} {row['gate']:44} "
            f"baseline={row['baseline'] or '-':>14} candidate={row['candidate'] or '-':>14} "
            f"{row['detail']}"
        )
    print(f"Promotion evidence: {'PASS' if promotable else 'FAIL'} -> {report_csv}")
    return 0 if promotable else 1


if __name__ == "__main__":
    raise SystemExit(main())

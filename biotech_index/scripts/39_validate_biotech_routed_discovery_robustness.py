#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_FALSE_POSITIVES = ("CNMD", "MDXG", "KROS", "OPK")


@dataclass(frozen=True)
class SelectedRow:
    evaluation_split: str
    horizon_days: int
    asof_date: str
    ticker: str
    company_name: str
    cohort: str
    score: float
    selected_rank: int
    net_return: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate routed discovery robustness versus current production allocation risk."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--legacy-dir", type=Path, default=None)
    parser.add_argument("--predictive-dir", type=Path, default=None)
    parser.add_argument("--horizons", type=str, default="20,60,120")
    parser.add_argument("--top-n", type=str, default="10,20")
    parser.add_argument("--bootstrap-iterations", type=int, default=1000)
    parser.add_argument("--bootstrap-seed", type=int, default=1729)
    parser.add_argument("--decision-date", type=str, default=date.today().isoformat())
    parser.add_argument("--false-positive-tickers", type=str, default=",".join(DEFAULT_FALSE_POSITIVES))
    parser.add_argument("--apply-operational-exclusions", action=argparse.BooleanOptionalAction, default=True)
    return parser.parse_args()


def parse_int_list(raw: str) -> list[int]:
    values = []
    for part in str(raw or "").replace(";", ",").replace("|", ",").split(","):
        text = part.strip()
        if text:
            values.append(int(text))
    if not values:
        raise ValueError("Expected at least one integer value")
    return sorted(set(values))


def parse_tickers(raw: str) -> set[str]:
    return {part.strip().upper() for part in str(raw or "").replace(";", ",").replace("|", ",").split(",") if part.strip()}


def to_float(raw: object, default: float = math.nan) -> float:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def to_int(raw: object, default: int = 0) -> int:
    value = to_float(raw, math.nan)
    return int(value) if math.isfinite(value) else default


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Required calibration diagnostic not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        return list(reader)


def load_cohorts(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    rows = conn.execute(
        """
        SELECT
            asof_date,
            ticker,
            biotech_primary_cohort
        FROM daily_scores
        WHERE ticker IS NOT NULL
          AND biotech_primary_cohort IS NOT NULL
        """
    ).fetchall()
    return {
        (str(row["asof_date"]), str(row["ticker"]).upper()): str(row["biotech_primary_cohort"] or "")
        for row in rows
    }


def routed_predictive_cohorts(config: dict[str, Any]) -> set[str]:
    routing = cfg_get(config, "biotech_scoring.risk_mode_routing", {}) or {}
    if not isinstance(routing, dict):
        return set()
    cohorts = routing.get("cohort_modes", {})
    if not isinstance(cohorts, dict):
        return set()
    return {
        str(cohort)
        for cohort, cohort_cfg in cohorts.items()
        if isinstance(cohort_cfg, dict) and str(cohort_cfg.get("discovery_mode") or "").strip().lower() == "predictive"
    }


def discovery_excluded_tickers(config: dict[str, Any]) -> set[str]:
    raw = cfg_get(config, "biotech_reports.discovery_operational_guardrails.excluded_tickers", []) or []
    if isinstance(raw, str):
        parts = raw.replace(";", ",").replace("|", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        parts = [str(item) for item in raw]
    else:
        parts = [str(raw)]
    return {part.strip().upper() for part in parts if part.strip()}


def robustness_gate_settings(config: dict[str, Any]) -> dict[str, Any]:
    raw = cfg_get(config, "biotech_scoring.risk_mode_routing.robustness_gates", {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    promotion_top_n_raw = raw.get("promotion_top_n", [20])
    if isinstance(promotion_top_n_raw, str):
        promotion_top_n = parse_int_list(promotion_top_n_raw)
    elif isinstance(promotion_top_n_raw, (list, tuple, set)):
        promotion_top_n = sorted({int(value) for value in promotion_top_n_raw})
    else:
        promotion_top_n = [20]
    return {
        "promotion_top_n": promotion_top_n,
        "min_lcb_delta_pct": to_float(raw.get("min_lcb_delta_pct"), 0.0),
        "min_profit_factor_delta": to_float(raw.get("min_profit_factor_delta"), 0.0),
        "max_loss20_delta_pct": to_float(raw.get("max_loss20_delta_pct"), 3.0),
        "min_bootstrap_win_rate_pct": to_float(raw.get("min_bootstrap_win_rate_pct"), 60.0),
        "min_bootstrap_lcb_p05_delta_pct": to_float(raw.get("min_bootstrap_lcb_p05_delta_pct"), -0.50),
        "min_improved_unique_ticker_rate_pct": to_float(
            raw.get("min_improved_unique_ticker_rate_pct"),
            60.0,
        ),
        "max_top3_gain_contribution_pct": to_float(raw.get("max_top3_gain_contribution_pct"), 50.0),
        "require_no_known_false_positive": bool(raw.get("require_no_known_false_positive", True)),
    }


def load_selected_rows(path: Path, cohorts_by_date_ticker: dict[tuple[str, str], str]) -> list[SelectedRow]:
    selected: list[SelectedRow] = []
    for row in read_csv_rows(path):
        if str(row.get("sample") or "").strip() != "all":
            continue
        if str(row.get("candidate_name") or "").strip() != "current_config":
            continue
        if str(row.get("selection_policy_name") or "").strip() != "core_structural_veto":
            continue
        ticker = str(row.get("ticker") or "").strip().upper()
        asof = str(row.get("asof_date") or "").strip()
        horizon = to_int(row.get("horizon_days"))
        net_return = to_float(row.get("net_forward_return"))
        score = to_float(row.get("candidate_selection_score"))
        if not ticker or not asof or horizon <= 0 or not math.isfinite(net_return) or not math.isfinite(score):
            continue
        cohort = cohorts_by_date_ticker.get((asof, ticker), "")
        selected.append(
            SelectedRow(
                evaluation_split=str(row.get("evaluation_split") or "").strip() or "unknown",
                horizon_days=horizon,
                asof_date=asof,
                ticker=ticker,
                company_name=str(row.get("company_name") or "").strip(),
                cohort=cohort,
                score=score,
                selected_rank=to_int(row.get("selected_rank_within_date")),
                net_return=net_return,
            )
        )
    if not selected:
        raise ValueError(f"No usable selected ticker diagnostics found in {path}")
    return selected


def grouped_by_date(rows: Iterable[SelectedRow]) -> dict[tuple[str, int, str], list[SelectedRow]]:
    grouped: dict[tuple[str, int, str], list[SelectedRow]] = defaultdict(list)
    for row in rows:
        grouped[(row.evaluation_split, row.horizon_days, row.asof_date)].append(row)
    return grouped


def select_top(rows: list[SelectedRow], top_n: int) -> list[SelectedRow]:
    dedup: dict[str, SelectedRow] = {}
    for row in sorted(rows, key=lambda item: (-item.score, item.selected_rank, item.ticker)):
        dedup.setdefault(row.ticker, row)
    return list(dedup.values())[:top_n]


def build_mode_selection(
    rows: list[SelectedRow],
    *,
    horizons: set[int],
    top_n_values: list[int],
) -> dict[tuple[str, int, int], list[SelectedRow]]:
    selections: dict[tuple[str, int, int], list[SelectedRow]] = defaultdict(list)
    for (split, horizon, _asof), date_rows in grouped_by_date(rows).items():
        if horizon not in horizons:
            continue
        for top_n in top_n_values:
            selections[(split, horizon, top_n)].extend(select_top(date_rows, top_n))
    return selections


def build_routed_selection(
    *,
    legacy_rows: list[SelectedRow],
    predictive_rows: list[SelectedRow],
    predictive_cohorts: set[str],
    excluded_tickers: set[str],
    horizons: set[int],
    top_n_values: list[int],
) -> dict[tuple[str, int, int], list[SelectedRow]]:
    legacy_by_date = grouped_by_date(legacy_rows)
    predictive_by_date = grouped_by_date(predictive_rows)
    keys = set(legacy_by_date).union(predictive_by_date)
    selections: dict[tuple[str, int, int], list[SelectedRow]] = defaultdict(list)
    for split, horizon, asof in sorted(keys):
        if horizon not in horizons:
            continue
        candidates: list[SelectedRow] = []
        candidates.extend(
            row
            for row in predictive_by_date.get((split, horizon, asof), [])
            if row.cohort in predictive_cohorts and row.ticker not in excluded_tickers
        )
        candidates.extend(
            row
            for row in legacy_by_date.get((split, horizon, asof), [])
            if row.cohort not in predictive_cohorts and row.ticker not in excluded_tickers
        )
        for top_n in top_n_values:
            selections[(split, horizon, top_n)].extend(select_top(candidates, top_n))
    return selections


def stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    avg = mean(values)
    return math.sqrt(sum((value - avg) ** 2 for value in values) / (len(values) - 1))


def lcb(values: list[float], z: float = 1.0) -> float:
    if not values:
        return math.nan
    return mean(values) - z * stdev(values) / math.sqrt(len(values))


def profit_factor(values: list[float]) -> float:
    gains = sum(max(value, 0.0) for value in values)
    losses = sum(max(-value, 0.0) for value in values)
    if losses <= 0.0:
        return 999.0 if gains > 0.0 else 0.0
    return gains / losses


def top3_gain_contribution(values: list[float]) -> float:
    gains = sorted((value for value in values if value > 0.0), reverse=True)
    total = sum(gains)
    if total <= 0.0:
        return 0.0
    return 100.0 * sum(gains[:3]) / total


def summarize(rows: list[SelectedRow]) -> dict[str, float | int]:
    values = [row.net_return for row in rows]
    return {
        "n": len(values),
        "unique_tickers": len({row.ticker for row in rows}),
        "mean_pct": 100.0 * mean(values) if values else math.nan,
        "median_pct": 100.0 * median(values) if values else math.nan,
        "lcb_pct": 100.0 * lcb(values) if values else math.nan,
        "profit_factor": profit_factor(values),
        "loss20_rate_pct": 100.0 * sum(1 for value in values if value <= -0.20) / len(values) if values else math.nan,
        "loss40_rate_pct": 100.0 * sum(1 for value in values if value <= -0.40) / len(values) if values else math.nan,
        "top3_gain_contribution_pct": top3_gain_contribution(values),
        "late_clinical_share_pct": 100.0
        * sum(1 for row in rows if row.cohort == "late_clinical_pivotal_or_registrational")
        / len(rows)
        if rows
        else math.nan,
    }


def bootstrap_delta_lcb(
    current_rows: list[SelectedRow],
    routed_rows: list[SelectedRow],
    *,
    iterations: int,
    seed: int,
) -> dict[str, float | int]:
    current_by_date: dict[str, list[SelectedRow]] = defaultdict(list)
    routed_by_date: dict[str, list[SelectedRow]] = defaultdict(list)
    for row in current_rows:
        current_by_date[row.asof_date].append(row)
    for row in routed_rows:
        routed_by_date[row.asof_date].append(row)
    dates = sorted(set(current_by_date).intersection(routed_by_date))
    if not dates or iterations <= 0:
        return {
            "bootstrap_iterations": 0,
            "bootstrap_win_rate_pct": math.nan,
            "bootstrap_delta_lcb_p05_pct": math.nan,
            "bootstrap_delta_lcb_median_pct": math.nan,
        }
    rng = random.Random(seed)
    deltas: list[float] = []
    for _ in range(iterations):
        sampled_dates = [rng.choice(dates) for _ in dates]
        current_values: list[float] = []
        routed_values: list[float] = []
        for sampled_date in sampled_dates:
            current_values.extend(row.net_return for row in current_by_date[sampled_date])
            routed_values.extend(row.net_return for row in routed_by_date[sampled_date])
        deltas.append(100.0 * (lcb(routed_values) - lcb(current_values)))
    sorted_deltas = sorted(deltas)
    p05_idx = max(0, min(len(sorted_deltas) - 1, int(0.05 * (len(sorted_deltas) - 1))))
    return {
        "bootstrap_iterations": iterations,
        "bootstrap_win_rate_pct": 100.0 * sum(1 for delta in deltas if delta >= 0.0) / len(deltas),
        "bootstrap_delta_lcb_p05_pct": sorted_deltas[p05_idx],
        "bootstrap_delta_lcb_median_pct": median(sorted_deltas),
    }


def ticker_breadth(current_rows: list[SelectedRow], routed_rows: list[SelectedRow]) -> dict[str, float | int]:
    current_by_ticker: dict[str, list[float]] = defaultdict(list)
    routed_by_ticker: dict[str, list[float]] = defaultdict(list)
    for row in current_rows:
        current_by_ticker[row.ticker].append(row.net_return)
    for row in routed_rows:
        routed_by_ticker[row.ticker].append(row.net_return)
    comparable = sorted(set(current_by_ticker).union(routed_by_ticker))
    if not comparable:
        return {"comparable_unique_tickers": 0, "improved_unique_tickers": 0, "improved_unique_ticker_rate_pct": math.nan}
    improved = 0
    for ticker in comparable:
        # Missing from one side means the ticker was not selected by that list.
        # For selection breadth, compare selected return versus a zero-return
        # no-selection baseline so removed losers and added winners both count.
        current_avg = mean(current_by_ticker[ticker]) if ticker in current_by_ticker else 0.0
        routed_avg = mean(routed_by_ticker[ticker]) if ticker in routed_by_ticker else 0.0
        if routed_avg > current_avg:
            improved += 1
    return {
        "comparable_unique_tickers": len(comparable),
        "improved_unique_tickers": improved,
        "improved_unique_ticker_rate_pct": 100.0 * improved / len(comparable),
    }


def selected_false_positives(rows: list[SelectedRow], false_positive_tickers: set[str]) -> list[str]:
    return sorted({row.ticker for row in rows if row.ticker in false_positive_tickers})


def rounded(raw: object) -> object:
    value = to_float(raw, math.nan)
    return round(value, 6) if math.isfinite(value) else ""


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "evaluation_split",
        "horizon_days",
        "top_n",
        "current_n",
        "routed_n",
        "current_unique_tickers",
        "routed_unique_tickers",
        "current_lcb_pct",
        "routed_lcb_pct",
        "delta_lcb_pct",
        "current_mean_pct",
        "routed_mean_pct",
        "delta_mean_pct",
        "current_profit_factor",
        "routed_profit_factor",
        "delta_profit_factor",
        "current_loss20_rate_pct",
        "routed_loss20_rate_pct",
        "delta_loss20_rate_pct",
        "routed_top3_gain_contribution_pct",
        "routed_late_clinical_share_pct",
        "comparable_unique_tickers",
        "improved_unique_tickers",
        "improved_unique_ticker_rate_pct",
        "bootstrap_iterations",
        "bootstrap_win_rate_pct",
        "bootstrap_delta_lcb_p05_pct",
        "bootstrap_delta_lcb_median_pct",
        "false_positive_tickers",
        "gate_status",
        "gate_fail_reasons",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def gate_status(row: dict[str, Any], settings: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if str(row["evaluation_split"]) != "test":
        return "diagnostic_only", reasons
    promotion_top_n = {int(value) for value in settings.get("promotion_top_n", [20])}
    if int(row["top_n"]) not in promotion_top_n:
        return "diagnostic_only", reasons
    if to_float(row["delta_lcb_pct"], -1e9) < float(settings["min_lcb_delta_pct"]):
        reasons.append("lcb_delta_negative")
    current_pf = to_float(row["current_profit_factor"], math.nan)
    routed_pf = to_float(row["routed_profit_factor"], math.nan)
    if current_pf >= 999.0 or routed_pf >= 999.0:
        # Zero-loss sentinel profit factor makes the delta meaningless; treat as inconclusive-fail.
        reasons.append("insufficient_losses")
    elif to_float(row["delta_profit_factor"], -1e9) < float(settings["min_profit_factor_delta"]):
        reasons.append("profit_factor_delta_negative")
    if to_float(row["delta_loss20_rate_pct"], 1e9) > float(settings["max_loss20_delta_pct"]):
        reasons.append(f"loss20_delta_gt_{float(settings['max_loss20_delta_pct']):g}pct")
    if to_float(row["bootstrap_win_rate_pct"], 0.0) < float(settings["min_bootstrap_win_rate_pct"]):
        reasons.append(f"bootstrap_win_rate_lt_{float(settings['min_bootstrap_win_rate_pct']):g}pct")
    if to_float(row["bootstrap_delta_lcb_p05_pct"], -1e9) < float(settings["min_bootstrap_lcb_p05_delta_pct"]):
        reasons.append(f"bootstrap_lcb_p05_lt_{float(settings['min_bootstrap_lcb_p05_delta_pct']):g}pct")
    if to_float(row["improved_unique_ticker_rate_pct"], 0.0) < float(
        settings["min_improved_unique_ticker_rate_pct"]
    ):
        reasons.append(
            f"improved_unique_ticker_rate_lt_{float(settings['min_improved_unique_ticker_rate_pct']):g}pct"
        )
    if to_float(row["routed_top3_gain_contribution_pct"], 100.0) > float(
        settings["max_top3_gain_contribution_pct"]
    ):
        reasons.append(f"top3_gain_contribution_gt_{float(settings['max_top3_gain_contribution_pct']):g}pct")
    if bool(settings.get("require_no_known_false_positive", True)) and str(
        row.get("false_positive_tickers") or ""
    ).strip():
        reasons.append("known_false_positive_selected")
    return ("pass" if not reasons else "fail", reasons)


def main() -> int:
    args = parse_args()
    config = load_yaml(args.config)
    base_dir = args.config.resolve().parent
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else resolve_path(cfg_get(config, "biotech_reports.output_dir", "../output/biotech_index_reports"), base_dir=base_dir)
    )
    legacy_dir = args.legacy_dir or output_dir / "risk_mode_validation_legacy_top50"
    predictive_dir = args.predictive_dir or output_dir / "risk_mode_validation_predictive_top50"
    db_path = args.db.resolve() if args.db is not None else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    horizons = set(parse_int_list(args.horizons))
    top_n_values = parse_int_list(args.top_n)
    false_positive_tickers = parse_tickers(args.false_positive_tickers)
    predictive_cohorts = routed_predictive_cohorts(config)
    operational_excluded_tickers = discovery_excluded_tickers(config) if args.apply_operational_exclusions else set()
    gate_settings = robustness_gate_settings(config)
    if not predictive_cohorts:
        raise ValueError("No predictive discovery cohorts configured under biotech_scoring.risk_mode_routing.cohort_modes")

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cohorts_by_date_ticker = load_cohorts(conn)

    legacy_rows = load_selected_rows(legacy_dir / "tier1_selected_ticker_diagnostics.csv", cohorts_by_date_ticker)
    predictive_rows = load_selected_rows(predictive_dir / "tier1_selected_ticker_diagnostics.csv", cohorts_by_date_ticker)
    # Apply the same operational-guardrail exclusions to the baseline so the
    # routed-vs-current delta is not confounded by the exclusion set.
    current_selection = build_mode_selection(
        [row for row in legacy_rows if row.ticker not in operational_excluded_tickers],
        horizons=horizons,
        top_n_values=top_n_values,
    )
    routed_selection = build_routed_selection(
        legacy_rows=legacy_rows,
        predictive_rows=predictive_rows,
        predictive_cohorts=predictive_cohorts,
        excluded_tickers=operational_excluded_tickers,
        horizons=horizons,
        top_n_values=top_n_values,
    )

    output_rows: list[dict[str, Any]] = []
    for key in sorted(set(current_selection).intersection(routed_selection)):
        split, horizon, top_n = key
        current_rows = current_selection[key]
        routed_rows = routed_selection[key]
        current_summary = summarize(current_rows)
        routed_summary = summarize(routed_rows)
        breadth = ticker_breadth(current_rows, routed_rows)
        boot = bootstrap_delta_lcb(
            current_rows,
            routed_rows,
            iterations=args.bootstrap_iterations,
            seed=args.bootstrap_seed + horizon * 100 + top_n,
        )
        row: dict[str, Any] = {
            "evaluation_split": split,
            "horizon_days": horizon,
            "top_n": top_n,
            "current_n": current_summary["n"],
            "routed_n": routed_summary["n"],
            "current_unique_tickers": current_summary["unique_tickers"],
            "routed_unique_tickers": routed_summary["unique_tickers"],
            "current_lcb_pct": rounded(current_summary["lcb_pct"]),
            "routed_lcb_pct": rounded(routed_summary["lcb_pct"]),
            "delta_lcb_pct": rounded(to_float(routed_summary["lcb_pct"]) - to_float(current_summary["lcb_pct"])),
            "current_mean_pct": rounded(current_summary["mean_pct"]),
            "routed_mean_pct": rounded(routed_summary["mean_pct"]),
            "delta_mean_pct": rounded(to_float(routed_summary["mean_pct"]) - to_float(current_summary["mean_pct"])),
            "current_profit_factor": rounded(current_summary["profit_factor"]),
            "routed_profit_factor": rounded(routed_summary["profit_factor"]),
            "delta_profit_factor": rounded(
                to_float(routed_summary["profit_factor"]) - to_float(current_summary["profit_factor"])
            ),
            "current_loss20_rate_pct": rounded(current_summary["loss20_rate_pct"]),
            "routed_loss20_rate_pct": rounded(routed_summary["loss20_rate_pct"]),
            "delta_loss20_rate_pct": rounded(
                to_float(routed_summary["loss20_rate_pct"]) - to_float(current_summary["loss20_rate_pct"])
            ),
            "routed_top3_gain_contribution_pct": rounded(routed_summary["top3_gain_contribution_pct"]),
            "routed_late_clinical_share_pct": rounded(routed_summary["late_clinical_share_pct"]),
            "false_positive_tickers": "|".join(selected_false_positives(routed_rows, false_positive_tickers)),
        }
        row.update({key_: rounded(value) for key_, value in breadth.items()})
        row.update({key_: rounded(value) for key_, value in boot.items()})
        status, reasons = gate_status(row, gate_settings)
        row["gate_status"] = status
        row["gate_fail_reasons"] = "|".join(reasons)
        output_rows.append(row)

    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = args.decision_date.replace("-", "")
    csv_path = output_dir / f"risk_mode_routed_discovery_robustness_{suffix}.csv"
    json_path = output_dir / f"risk_mode_routed_discovery_robustness_{suffix}.json"
    write_csv(csv_path, output_rows)
    promotion_rows = [
        row
        for row in output_rows
        if row["evaluation_split"] == "test"
        and int(row["top_n"]) in {int(value) for value in gate_settings["promotion_top_n"]}
    ]
    decision = {
        "decision_date": args.decision_date,
        "predictive_discovery_cohorts": sorted(predictive_cohorts),
        "bootstrap_iterations": args.bootstrap_iterations,
        "gate_settings": gate_settings,
        "false_positive_tickers": sorted(false_positive_tickers),
        "operational_excluded_tickers": sorted(operational_excluded_tickers),
        "status": "pass"
        if promotion_rows and all(row["gate_status"] == "pass" for row in promotion_rows)
        else "fail",
        "promotion_rows": promotion_rows,
        # Key name kept for downstream compatibility; contents follow the
        # config-driven promotion_top_n rather than a hardcoded Top20.
        "test_top20_rows": promotion_rows,
        "source_artifacts": {
            "legacy_selected_diagnostics": str(legacy_dir / "tier1_selected_ticker_diagnostics.csv"),
            "predictive_selected_diagnostics": str(predictive_dir / "tier1_selected_ticker_diagnostics.csv"),
        },
    }
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(decision, handle, indent=2, sort_keys=True)
        handle.write("\n")

    print(f"Wrote {csv_path}")
    print(f"Wrote {json_path}")
    print(f"Routed discovery robustness status: {decision['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

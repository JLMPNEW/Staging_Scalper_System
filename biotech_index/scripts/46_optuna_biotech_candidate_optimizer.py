#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402


LOGGER = logging.getLogger("optuna_biotech_candidate_optimizer")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "output" / "biotech_index_reports" / "optuna_candidate_optimizer"

REQUIRED_CALIBRATION_FILES = (
    "tier1_weight_calibration_manifest.json",
    "tier1_weight_calibration_holdout.csv",
    "tier1_weight_calibration_bootstrap_ci.csv",
    "tier1_weight_calibration_best.csv",
)
REQUIRED_IC_FILES = (
    "feature_ic_classification.csv",
    "feature_ic_summary.csv",
)
PROMOTABLE_IC_CLASSES = frozenset({"promote_candidate", "cohort_specific_only"})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Gated Optuna meta-optimizer for biotech candidate structures. "
            "This runs only after clean historical QA, IC/monotonicity, and candidate calibration."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--input-dir", type=Path, required=True, help="Candidate calibration output directory.")
    parser.add_argument("--feature-ic-dir", type=Path, required=True, help="Feature IC monitor output directory.")
    parser.add_argument("--sequence-dir", type=Path, default=None, help="Clean historical sequence root directory.")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--start-asof", type=str, default="")
    parser.add_argument("--end-asof", type=str, default="")
    parser.add_argument("--horizons", type=str, default="20,60,120")
    parser.add_argument("--top-n", type=str, default="10,20")
    parser.add_argument("--n-trials", type=int, default=500)
    parser.add_argument("--seed", type=int, default=7331)
    parser.add_argument("--min-panel-dates", type=int, default=250)
    parser.add_argument("--min-promote-or-cohort-factors", type=int, default=1)
    parser.add_argument("--min-selected-n", type=int, default=60)
    parser.add_argument("--min-lcb-return-pct", type=float, default=0.0)
    parser.add_argument("--min-profit-factor", type=float, default=1.15)
    parser.add_argument("--max-loss20-rate-pct", type=float, default=15.0)
    parser.add_argument("--max-loss40-rate-pct", type=float, default=12.5)
    parser.add_argument("--max-top3-gain-contribution-pct", type=float, default=50.0)
    parser.add_argument(
        "--allow-missing-clean-qa",
        action="store_true",
        help="For development only. Do not use for production Optuna runs.",
    )
    parser.add_argument("--check-only", action="store_true", help="Run gates and survivor selection without Optuna trials.")
    parser.add_argument("--dry-run", action="store_true", help="Report planned run without importing/running Optuna.")
    return parser.parse_args()


def parse_int_set(raw: str) -> set[int]:
    values: set[int] = set()
    for part in str(raw or "").replace(";", ",").replace("|", ",").split(","):
        text = part.strip()
        if not text:
            continue
        values.add(int(float(text)))
    return values


def as_bool(raw: object, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "t", "yes", "y", "pass", "passed", "enabled", "on"}:
        return True
    if text in {"0", "false", "f", "no", "n", "fail", "failed", "disabled", "off"}:
        return False
    return default


def to_float(raw: object, default: float | None = None) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        keys: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for key in row:
                if key not in seen:
                    keys.append(key)
                    seen.add(key)
        fieldnames = keys
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def resolve_sequence_dir(input_dir: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    if input_dir.name == "candidate_calibration":
        return input_dir.parent
    return input_dir.parent


def validate_required_files(input_dir: Path, feature_ic_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for filename in REQUIRED_CALIBRATION_FILES:
        path = input_dir / filename
        rows.append(
            {
                "gate": f"calibration_file:{filename}",
                "status": "PASS" if path.exists() else "FAIL",
                "value": str(path),
                "details": "candidate calibration prerequisite",
            }
        )
    for filename in REQUIRED_IC_FILES:
        path = feature_ic_dir / filename
        rows.append(
            {
                "gate": f"feature_ic_file:{filename}",
                "status": "PASS" if path.exists() else "FAIL",
                "value": str(path),
                "details": "IC/monotonicity prerequisite",
            }
        )
    return rows


def validate_clean_qa(sequence_dir: Path, *, min_panel_dates: int, allow_missing: bool) -> list[dict[str, Any]]:
    summary_path = sequence_dir / "clean_historical_qa_summary.csv"
    panel_path = sequence_dir / "clean_historical_panel_qa_by_asof.csv"
    if allow_missing and (not summary_path.exists() or not panel_path.exists()):
        return [
            {
                "gate": "clean_panel_qa",
                "status": "WARN",
                "value": "missing_allowed",
                "details": "Missing clean QA was explicitly allowed. Do not use for production promotion.",
            }
        ]
    summary = read_csv(summary_path)
    panel = read_csv(panel_path)
    failed = [row for row in summary if str(row.get("status") or "").strip().upper() == "FAIL"]
    warning_count = sum(1 for row in summary if str(row.get("status") or "").strip().upper() == "WARN")
    panel_failed = [row for row in panel if str(row.get("status") or "").strip().upper() == "FAIL"]
    asof_dates = {str(row.get("asof_date") or "") for row in panel if str(row.get("asof_date") or "").strip()}
    return [
        {
            "gate": "clean_qa_summary_no_fail",
            "status": "PASS" if not failed else "FAIL",
            "value": len(failed),
            "details": f"warnings={warning_count}; file={summary_path}",
        },
        {
            "gate": "clean_panel_dates",
            "status": "PASS" if len(asof_dates) >= min_panel_dates else "FAIL",
            "value": len(asof_dates),
            "details": f"min_panel_dates={min_panel_dates}; file={panel_path}",
        },
        {
            "gate": "clean_panel_date_rows_no_fail",
            "status": "PASS" if not panel_failed else "FAIL",
            "value": len(panel_failed),
            "details": f"file={panel_path}",
        },
    ]


def validate_ic(feature_ic_dir: Path, *, min_promotable: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows = read_csv(feature_ic_dir / "feature_ic_classification.csv")
    class_counts: dict[str, int] = {}
    promotable: set[str] = set()
    for row in rows:
        classification = str(row.get("classification") or "").strip()
        factor = str(row.get("factor") or "").strip()
        if not classification:
            continue
        class_counts[classification] = class_counts.get(classification, 0) + 1
        if factor and classification in PROMOTABLE_IC_CLASSES:
            promotable.add(factor)
    gate_rows = [
        {
            "gate": "feature_ic_classification_rows",
            "status": "PASS" if rows else "FAIL",
            "value": len(rows),
            "details": str(feature_ic_dir / "feature_ic_classification.csv"),
        },
        {
            "gate": "feature_ic_promote_or_cohort_specific_factor_count",
            "status": "PASS" if len(promotable) >= min_promotable else "FAIL",
            "value": len(promotable),
            "details": f"min_promote_or_cohort_factors={min_promotable}",
        },
    ]
    return gate_rows, class_counts


def candidate_key(row: dict[str, str]) -> str:
    return "|".join(
        [
            str(row.get("candidate_id") or ""),
            str(row.get("candidate_name") or ""),
            str(row.get("selection_policy_name") or ""),
            str(row.get("horizon_days") or ""),
            str(row.get("top_n") or ""),
        ]
    )


def filter_survivors(
    rows: list[dict[str, str]],
    *,
    horizons: set[int],
    top_ns: set[int],
    min_selected_n: int,
    min_lcb: float,
    min_profit_factor: float,
    max_loss20: float,
    max_loss40: float,
    max_top3: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    survivors: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in rows:
        horizon = int(to_float(row.get("horizon_days"), -1) or -1)
        top_n = int(to_float(row.get("top_n"), -1) or -1)
        if horizons and horizon not in horizons:
            continue
        if top_ns and top_n not in top_ns:
            continue
        train_pass = as_bool(row.get("train_calibration_pass"), False) or str(row.get("train_calibration_pass_state")) == "pass"
        test_pass = as_bool(row.get("test_calibration_pass"), False) or str(row.get("test_calibration_pass_state")) == "pass"
        train_n = to_float(row.get("train_n"), 0.0) or 0.0
        test_n = to_float(row.get("test_n"), 0.0) or 0.0
        test_lcb = to_float(row.get("test_selected_lcb_return_pct"), -1e9) or -1e9
        test_profit = to_float(row.get("test_selected_profit_factor"), 0.0) or 0.0
        test_loss20 = to_float(row.get("test_selected_large_loss_20pct_rate_pct"), 100.0) or 100.0
        test_loss40 = to_float(row.get("test_selected_large_loss_40pct_rate_pct"), 100.0) or 100.0
        test_top3 = to_float(row.get("test_selected_top3_gain_contribution_pct"), 100.0) or 100.0
        fail_reasons: list[str] = []
        if not train_pass:
            fail_reasons.append("train_calibration_failed")
        if not test_pass:
            fail_reasons.append("test_calibration_failed")
        if train_n < min_selected_n or test_n < min_selected_n:
            fail_reasons.append("selected_n_below_min")
        if test_lcb < min_lcb:
            fail_reasons.append("test_lcb_below_min")
        if test_profit < min_profit_factor:
            fail_reasons.append("test_profit_factor_below_min")
        if test_loss20 > max_loss20:
            fail_reasons.append("test_loss20_above_max")
        if test_loss40 > max_loss40:
            fail_reasons.append("test_loss40_above_max")
        if test_top3 > max_top3:
            fail_reasons.append("test_top3_concentration_above_max")
        out = dict(row)
        out["optimizer_candidate_key"] = candidate_key(row)
        out["optimizer_reject_reasons"] = "|".join(fail_reasons)
        if fail_reasons:
            rejected.append(out)
        else:
            survivors.append(out)
    return survivors, rejected


def metric(row: dict[str, Any], key: str, default: float = 0.0) -> float:
    return to_float(row.get(key), default) or default


def trial_candidate_score(row: dict[str, Any], params: dict[str, float], *, split: str) -> float:
    prefix = f"{split}_selected_"
    lcb = metric(row, prefix + "lcb_return_pct")
    mean = metric(row, prefix + "mean_return_pct")
    hit = metric(row, prefix + "hit_rate_pct") / 100.0
    profit = min(metric(row, prefix + "profit_factor"), 5.0)
    loss20 = metric(row, prefix + "large_loss_20pct_rate_pct") / 100.0
    loss40 = metric(row, prefix + "large_loss_40pct_rate_pct") / 100.0
    top3 = metric(row, prefix + "top3_gain_contribution_pct") / 100.0
    sortino = max(-5.0, min(5.0, metric(row, prefix + "sortino_like")))
    return (
        params["w_lcb"] * lcb
        + params["w_mean"] * mean
        + params["w_hit"] * hit * 10.0
        + params["w_profit"] * profit * 4.0
        + params["w_sortino"] * sortino * 2.0
        - params["w_loss20"] * loss20 * 25.0
        - params["w_loss40"] * loss40 * 35.0
        - params["w_top3"] * top3 * 10.0
    )


def run_optuna_trials(
    survivors: list[dict[str, Any]],
    *,
    n_trials: int,
    seed: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        import optuna  # type: ignore
    except Exception as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("Optuna is not installed. Install with `pip install optuna` before running this step.") from exc

    def objective(trial: Any) -> float:
        params = {
            "w_lcb": trial.suggest_float("w_lcb", 0.50, 2.00),
            "w_mean": trial.suggest_float("w_mean", 0.00, 0.75),
            "w_hit": trial.suggest_float("w_hit", 0.00, 0.75),
            "w_profit": trial.suggest_float("w_profit", 0.00, 0.75),
            "w_sortino": trial.suggest_float("w_sortino", 0.00, 0.60),
            "w_loss20": trial.suggest_float("w_loss20", 0.40, 2.00),
            "w_loss40": trial.suggest_float("w_loss40", 0.40, 2.00),
            "w_top3": trial.suggest_float("w_top3", 0.20, 1.50),
            "w_train_test_gap": trial.suggest_float("w_train_test_gap", 0.00, 1.50),
        }
        train_scored = [(trial_candidate_score(row, params, split="train"), row) for row in survivors]
        train_scored.sort(key=lambda item: item[0], reverse=True)
        selected = train_scored[0][1]
        train_score = train_scored[0][0]
        test_score = trial_candidate_score(selected, params, split="test")
        gap = abs(train_score - test_score)
        trial.set_user_attr("selected_candidate_key", selected["optimizer_candidate_key"])
        trial.set_user_attr("candidate_name", selected.get("candidate_name", ""))
        trial.set_user_attr("selection_policy_name", selected.get("selection_policy_name", ""))
        trial.set_user_attr("horizon_days", selected.get("horizon_days", ""))
        trial.set_user_attr("top_n", selected.get("top_n", ""))
        trial.set_user_attr("train_score", round(train_score, 6))
        trial.set_user_attr("test_score", round(test_score, 6))
        trial.set_user_attr("test_lcb_return_pct", selected.get("test_selected_lcb_return_pct", ""))
        trial.set_user_attr("test_profit_factor", selected.get("test_selected_profit_factor", ""))
        trial.set_user_attr("test_large_loss_20pct_rate_pct", selected.get("test_selected_large_loss_20pct_rate_pct", ""))
        trial.set_user_attr("test_top3_gain_contribution_pct", selected.get("test_selected_top3_gain_contribution_pct", ""))
        return test_score - params["w_train_test_gap"] * gap

    sampler = optuna.samplers.TPESampler(seed=seed)
    study = optuna.create_study(direction="maximize", sampler=sampler)
    study.optimize(objective, n_trials=max(1, int(n_trials)), show_progress_bar=False)
    trial_rows: list[dict[str, Any]] = []
    for trial in study.trials:
        row = {
            "trial_number": trial.number,
            "state": str(trial.state),
            "objective_value": trial.value if trial.value is not None else "",
            **trial.params,
            **trial.user_attrs,
        }
        trial_rows.append(row)
    best = {
        "best_trial_number": study.best_trial.number,
        "best_value": study.best_value,
        **study.best_trial.params,
        **study.best_trial.user_attrs,
    }
    return trial_rows, best


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    start = time.perf_counter()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=config_path.parent)
    input_dir = args.input_dir.expanduser().resolve()
    feature_ic_dir = args.feature_ic_dir.expanduser().resolve()
    sequence_dir = resolve_sequence_dir(input_dir, args.sequence_dir)
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else DEFAULT_OUTPUT_ROOT / f"{sequence_dir.name}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    horizons = parse_int_set(args.horizons)
    top_ns = parse_int_set(args.top_n)

    gate_rows = validate_required_files(input_dir, feature_ic_dir)
    if all(row["status"] == "PASS" for row in gate_rows):
        gate_rows.extend(
            validate_clean_qa(
                sequence_dir,
                min_panel_dates=max(1, int(args.min_panel_dates)),
                allow_missing=bool(args.allow_missing_clean_qa),
            )
        )
        ic_gates, ic_class_counts = validate_ic(
            feature_ic_dir,
            min_promotable=max(0, int(args.min_promote_or_cohort_factors)),
        )
        gate_rows.extend(ic_gates)
    else:
        ic_class_counts = {}

    write_csv(output_dir / "optuna_gate_report.csv", gate_rows)
    gate_failures = [row for row in gate_rows if row["status"] == "FAIL"]
    if gate_failures:
        write_json(
            output_dir / "optuna_optimizer_manifest.json",
            {
                "status": "gated",
                "reason": "prerequisite_gate_failed",
                "failures": gate_failures[:20],
                "input_dir": str(input_dir),
                "feature_ic_dir": str(feature_ic_dir),
                "sequence_dir": str(sequence_dir),
            },
        )
        raise RuntimeError("Optuna prerequisite gates failed. See optuna_gate_report.csv.")

    holdout_rows = read_csv(input_dir / "tier1_weight_calibration_holdout.csv")
    survivors, rejected = filter_survivors(
        holdout_rows,
        horizons=horizons,
        top_ns=top_ns,
        min_selected_n=max(1, int(args.min_selected_n)),
        min_lcb=float(args.min_lcb_return_pct),
        min_profit_factor=float(args.min_profit_factor),
        max_loss20=float(args.max_loss20_rate_pct),
        max_loss40=float(args.max_loss40_rate_pct),
        max_top3=float(args.max_top3_gain_contribution_pct),
    )
    write_csv(output_dir / "optuna_survivor_candidates.csv", survivors)
    write_csv(output_dir / "optuna_rejected_candidates.csv", rejected)
    if not survivors:
        write_json(
            output_dir / "optuna_optimizer_manifest.json",
            {
                "status": "gated",
                "reason": "no_candidate_calibration_survivors",
                "input_holdout_rows": len(holdout_rows),
                "rejected_rows": len(rejected),
                "constraints": {
                    "horizons": sorted(horizons),
                    "top_n": sorted(top_ns),
                    "min_selected_n": args.min_selected_n,
                    "min_lcb_return_pct": args.min_lcb_return_pct,
                    "min_profit_factor": args.min_profit_factor,
                    "max_loss20_rate_pct": args.max_loss20_rate_pct,
                    "max_loss40_rate_pct": args.max_loss40_rate_pct,
                    "max_top3_gain_contribution_pct": args.max_top3_gain_contribution_pct,
                },
            },
        )
        raise RuntimeError("No candidate structures survived calibration constraints; Optuna not run.")

    if args.dry_run or args.check_only:
        status = "dry_run" if args.dry_run else "check_only"
        write_json(
            output_dir / "optuna_optimizer_manifest.json",
            {
                "status": status,
                "db_path": str(db_path),
                "input_dir": str(input_dir),
                "feature_ic_dir": str(feature_ic_dir),
                "sequence_dir": str(sequence_dir),
                "survivor_count": len(survivors),
                "ic_class_counts": ic_class_counts,
                "n_trials_planned": args.n_trials,
                "elapsed_sec": round(time.perf_counter() - start, 3),
            },
        )
        LOGGER.info("Optuna candidate optimizer %s: survivors=%d output_dir=%s", status, len(survivors), output_dir)
        return

    trial_rows, best = run_optuna_trials(survivors, n_trials=args.n_trials, seed=args.seed)
    write_csv(output_dir / "optuna_trial_results.csv", trial_rows)
    write_csv(output_dir / "optuna_best_candidate.csv", [best])
    write_json(
        output_dir / "optuna_optimizer_manifest.json",
        {
            "status": "success",
            "optimizer_type": "gated_candidate_survivor_meta_optimizer",
            "db_path": str(db_path),
            "input_dir": str(input_dir),
            "feature_ic_dir": str(feature_ic_dir),
            "sequence_dir": str(sequence_dir),
            "start_asof": args.start_asof,
            "end_asof": args.end_asof,
            "survivor_count": len(survivors),
            "trial_count": len(trial_rows),
            "best": best,
            "ic_class_counts": ic_class_counts,
            "elapsed_sec": round(time.perf_counter() - start, 3),
            "notes": [
                "This optimizer selects among candidate structures that already survived calibration constraints.",
                "It does not discover factor structure from scratch.",
                "Use output as a challenger selection aid, not as automatic production promotion.",
            ],
        },
    )
    LOGGER.info("Optuna candidate optimizer complete: survivors=%d trials=%d output_dir=%s", len(survivors), len(trial_rows), output_dir)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except BaseException as exc:
        if isinstance(exc, SystemExit) and exc.code in (0, None):
            raise
        LOGGER.exception("Unhandled exception in main()")
        sys.exit(1)

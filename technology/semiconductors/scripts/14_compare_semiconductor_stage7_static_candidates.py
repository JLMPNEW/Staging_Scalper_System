#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.logging_utils import configure_utc_logging  # noqa: E402
from technology.semiconductors.optuna_calibration import (  # noqa: E402
    Candidate,
    build_panel,
    contiguous_folds,
    evaluate_candidate,
    flatten_metrics,
    json_ready_weights,
    load_eval_kwargs,
    split_dates,
    stage7_candidate,
    write_csv,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CONFIG_KEY = "semiconductor_optuna_calibration"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare report-only static Stage 7 semiconductor weight candidates against the current baseline."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def static_candidates(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "candidate_name": "stage7_current_v1",
            "production_eligible": 1,
            "notes": "Current Stage 7 production baseline from semiconductor_calibrated_scoring.",
            "candidate": stage7_candidate(config),
        },
        {
            "candidate_name": "stage7_v1_1_conservative_no_growth",
            "production_eligible": 1,
            "notes": "Report-only candidate: raises risk/quality emphasis, reduces positioning, keeps growth pinned at zero.",
            "candidate": Candidate(
                component_weights={
                    "valuation": 0.28,
                    "quality": 0.27,
                    "risk_control": 0.30,
                    "market_behavior": 0.10,
                    "positioning": 0.05,
                    "growth": 0.00,
                },
                subfeature_specs={
                    "quality": [
                        ("operating_margin_score", 0.30),
                        ("fcf_margin_score", 0.40),
                        ("sbc_pct_revenue_score", 0.15),
                        ("share_count_yoy_growth_score", 0.15),
                    ],
                    "valuation": [
                        ("fcf_yield_score", 0.55),
                        ("ev_gross_profit_score", 0.45),
                    ],
                    "risk_control": [
                        ("realized_vol_60d_score", 0.45),
                        ("max_drawdown_12m_score", 0.25),
                        ("avg_dollar_volume_60d_score", 0.15),
                        ("latest_borrow_fee_rate_score", 0.10),
                        ("sbc_pct_revenue_score", 0.05),
                    ],
                    "market_behavior": [
                        ("distance_from_52w_high_score", 0.40),
                        ("max_drawdown_12m_score", 0.35),
                        ("avg_dollar_volume_60d_score", 0.20),
                        ("ret_12m_ex_1m_score", 0.05),
                    ],
                    "positioning": [
                        ("latest_borrow_fee_rate_score", 0.45),
                        ("institutional_ownership_delta_pct_score", 0.35),
                        ("latest_days_to_cover_score", 0.20),
                    ],
                    "growth": [],
                },
            ),
        },
        {
            "candidate_name": "stage7_growth_probe_not_production",
            "production_eligible": 0,
            "notes": "Report-only sensitivity probe: small growth weight to test the 2011-panel rehabilitation; not v1 production eligible.",
            "candidate": Candidate(
                component_weights={
                    "valuation": 0.27,
                    "quality": 0.26,
                    "risk_control": 0.29,
                    "market_behavior": 0.10,
                    "positioning": 0.03,
                    "growth": 0.05,
                },
                subfeature_specs={
                    "quality": [
                        ("operating_margin_score", 0.30),
                        ("fcf_margin_score", 0.40),
                        ("sbc_pct_revenue_score", 0.15),
                        ("share_count_yoy_growth_score", 0.15),
                    ],
                    "growth": [
                        ("revenue_acceleration_score", 0.55),
                        ("revenue_yoy_growth_score", 0.30),
                        ("operating_income_yoy_growth_score", 0.15),
                    ],
                    "valuation": [
                        ("fcf_yield_score", 0.55),
                        ("ev_gross_profit_score", 0.45),
                    ],
                    "risk_control": [
                        ("realized_vol_60d_score", 0.45),
                        ("max_drawdown_12m_score", 0.25),
                        ("avg_dollar_volume_60d_score", 0.15),
                        ("latest_borrow_fee_rate_score", 0.10),
                        ("sbc_pct_revenue_score", 0.05),
                    ],
                    "market_behavior": [
                        ("distance_from_52w_high_score", 0.40),
                        ("max_drawdown_12m_score", 0.35),
                        ("avg_dollar_volume_60d_score", 0.20),
                        ("ret_12m_ex_1m_score", 0.05),
                    ],
                    "positioning": [
                        ("latest_borrow_fee_rate_score", 0.50),
                        ("institutional_ownership_delta_pct_score", 0.40),
                        ("latest_days_to_cover_score", 0.10),
                    ],
                },
            ),
        },
    ]


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(
        "../output/technology_reports/scoring",
        base_dir=base_dir,
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    step = int(cfg_get(config, "semiconductor_signal_diagnostics.step_trading_days", 21))
    horizons: list[int]
    panel, panel_dates, horizons = build_panel(config, db_path)
    train_dates, holdout_dates = split_dates(config, panel_dates, horizons=horizons, step=step)
    folds = contiguous_folds(panel_dates, int(cfg_get(config, f"{CONFIG_KEY}.robustness_folds", 5)))
    eval_kwargs = load_eval_kwargs(config)

    stage7 = stage7_candidate(config)
    stage7_holdout = evaluate_candidate(panel, holdout_dates, horizons, stage7, **eval_kwargs)
    stage7_folds = [
        evaluate_candidate(panel, fold_dates, horizons, stage7, **eval_kwargs)
        for fold_dates in folds
        if len(fold_dates) >= 4
    ]

    primary = horizons[0]
    secondary = horizons[1] if len(horizons) > 1 else horizons[0]
    max_turnover = float(cfg_get(config, f"{CONFIG_KEY}.max_turnover", 0.55))
    max_top_cohort_share = float(cfg_get(config, f"{CONFIG_KEY}.max_top_cohort_share", 0.45))
    min_ic_primary = float(cfg_get(config, f"{CONFIG_KEY}.min_holdout_mean_ic_21", 0.01))
    min_ic_secondary = float(cfg_get(config, f"{CONFIG_KEY}.min_holdout_mean_ic_63", 0.01))
    min_hit = float(cfg_get(config, f"{CONFIG_KEY}.min_holdout_hit_rate", 0.50))
    min_improvement = float(cfg_get(config, f"{CONFIG_KEY}.promotion_min_objective_improvement", 0.002))
    min_fold_win_fraction = float(cfg_get(config, f"{CONFIG_KEY}.min_fold_win_fraction", 0.5))

    rows: list[dict[str, Any]] = []
    weight_rows: list[dict[str, Any]] = []
    for item in static_candidates(config):
        candidate = item["candidate"]
        train_metrics = evaluate_candidate(panel, train_dates, horizons, candidate, **eval_kwargs)
        holdout_metrics = evaluate_candidate(panel, holdout_dates, horizons, candidate, **eval_kwargs)
        fold_improvements: list[float] = []
        fold_wins = 0
        scored_folds = 0
        for fold_idx, fold_dates in enumerate([fold for fold in folds if len(fold) >= 4]):
            candidate_fold = evaluate_candidate(panel, fold_dates, horizons, candidate, **eval_kwargs)
            baseline_fold = stage7_folds[fold_idx]
            improvement = float(candidate_fold.get("objective", 0.0)) - float(baseline_fold.get("objective", 0.0))
            fold_improvements.append(improvement)
            scored_folds += 1
            fold_wins += int(improvement > 0)
        fold_win_fraction = fold_wins / scored_folds if scored_folds else 0.0
        objective_improvement = float(holdout_metrics.get("objective", 0.0)) - float(stage7_holdout.get("objective", 0.0))
        review_pass = int(
            int(item["production_eligible"]) == 1
            and objective_improvement >= min_improvement
            and float(holdout_metrics.get(f"mean_ic_{primary}", 0.0)) >= min_ic_primary
            and float(holdout_metrics.get(f"mean_ic_{secondary}", 0.0)) >= min_ic_secondary
            and float(holdout_metrics.get(f"hit_rate_{primary}", 0.0)) >= min_hit
            and float(holdout_metrics.get("avg_top_turnover", 1.0)) <= max_turnover
            and float(holdout_metrics.get("avg_top_cohort_share", 1.0)) <= max_top_cohort_share
            and fold_win_fraction >= min_fold_win_fraction
        )
        row = {
            "candidate_name": item["candidate_name"],
            "production_eligible": item["production_eligible"],
            "static_review_pass": review_pass,
            "notes": item["notes"],
            "objective_improvement_vs_stage7_holdout": objective_improvement,
            "fold_win_fraction_vs_stage7": fold_win_fraction,
            "mean_fold_objective_improvement_vs_stage7": mean(fold_improvements),
            **flatten_metrics("train", train_metrics, horizons),
            **flatten_metrics("holdout", holdout_metrics, horizons),
            "weights_json": json.dumps(json_ready_weights(candidate), sort_keys=True),
        }
        rows.append(row)

        ready = json_ready_weights(candidate)
        for component, weight in ready["component_weights"].items():
            weight_rows.append(
                {
                    "candidate_name": item["candidate_name"],
                    "weight_type": "component",
                    "component": component,
                    "subfeature": "",
                    "weight": weight,
                }
            )
        for component, specs in ready["subfeature_weights"].items():
            for subfeature, weight in specs.items():
                weight_rows.append(
                    {
                        "candidate_name": item["candidate_name"],
                        "weight_type": "subfeature",
                        "component": component,
                        "subfeature": subfeature,
                        "weight": weight,
                    }
                )

    rows.sort(key=lambda row: float(row["holdout_objective"] or 0.0), reverse=True)
    write_csv(output_dir / "semiconductor_stage7_static_candidate_review.csv", rows)
    write_csv(output_dir / "semiconductor_stage7_static_candidate_weights.csv", weight_rows)
    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "database_path": str(db_path),
        "panel_dates": len(panel_dates),
        "train_dates": len(train_dates),
        "holdout_dates": len(holdout_dates),
        "horizons": horizons,
        "candidates": [
            {
                "candidate_name": row["candidate_name"],
                "production_eligible": row["production_eligible"],
                "static_review_pass": row["static_review_pass"],
                "holdout_objective": row["holdout_objective"],
                "objective_improvement_vs_stage7_holdout": row["objective_improvement_vs_stage7_holdout"],
                "fold_win_fraction_vs_stage7": row["fold_win_fraction_vs_stage7"],
            }
            for row in rows
        ],
    }
    (output_dir / "semiconductor_stage7_static_candidate_review.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

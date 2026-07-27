#!/usr/bin/env python3
"""Stage 11 research sweep for the regime-lever walk-forward arm.

This is evidence-only. It does not promote an overlay, mutate any production book, or change
the standing Stage 16 arm comparison. It repeats the same PIT walk-forward engine with a small
grid over:

  * supportive-regime score multiplier
  * rebalance cadence
  * unsupported-regime fallback mode

The intent is to test whether the regime-conditioned signal discovered by research/71 can be
monetized by being more aggressive when the regime supports scores, without hard-coding a single
parameter from one exploratory run.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from statistics import NormalDist
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from portfolio_layer.backtest.walkforward_common import (  # noqa: E402
    build_real_providers,
    run_walkforward,
    summarize_arms,
)
from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.macro.contract import open_macro_serving_db  # noqa: E402
from portfolio_layer.macro.taxonomy import sleeve_taxonomy  # noqa: E402
from portfolio_layer.research.stage11_common import load_lockbox  # noqa: E402


LOGGER = logging.getLogger("regime_parameter_sweep")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
LOCKBOX_PROTOCOL = PACKAGE_ROOT / "docs" / "LOCKBOX_PROTOCOL.md"

SWEEP_FIELDS = [
    "case_id",
    "supportive_regimes",
    "unsupported_mode",
    "mu_multiplier",
    "rebalance_every_n_snapshots",
    "n_rebalances",
    "n_days",
    "baseline_net_return",
    "candidate_net_return",
    "baseline_net_sharpe",
    "candidate_net_sharpe",
    "candidate_max_drawdown_net",
    "active_net_ann_vs_baseline",
    "tracking_error_ann",
    "net_ir_vs_baseline",
    "active_t",
    "turnover_per_year",
    "cost_drag_per_year_bps",
    "solver_failures",
    "raw_promotable",
    "multiple_test_active_t_min",
    "promotable",
    "rejection_reasons",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage 11 regime-lever parameter sweep.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--max-cases", type=int, default=0, help="Optional smoke-test cap on grid cases.")
    return parser.parse_args()


def _float_list(values: Any, default: list[float]) -> list[float]:
    if values is None:
        return default
    out: list[float] = []
    for value in values:
        out.append(float(value))
    return out


def _int_list(values: Any, default: list[int]) -> list[int]:
    if values is None:
        return default
    out: list[int] = []
    for value in values:
        out.append(max(1, int(value)))
    return out


def _str_list(values: Any, default: list[str]) -> list[str]:
    if values is None:
        return default
    out = [str(v).strip() for v in values if str(v).strip()]
    return out


def _base_params(config: dict[str, Any]) -> dict[str, Any]:
    wf = cfg_get(config, "walkforward", {}) or {}
    supportive_raw = wf.get("regime_gate_supportive_regimes")
    if supportive_raw is None:
        supportive_raw = ["HEATING_UP"]
    return dict(
        rebalance_every_n_snapshots=int(wf.get("rebalance_every_n_snapshots", 5)),
        one_way_cost_bps=float(wf.get("one_way_cost_bps", 5.0)),
        cov_lookback_trading_days=int(wf.get("cov_lookback_trading_days", 252)),
        cov_min_obs=int(wf.get("cov_min_obs", 60)),
        shrinkage_intensity=float(cfg_get(config, "risk_panel.shrinkage_intensity", 0.2)),
        max_universe=int(wf.get("max_universe", 150)),
        min_universe=int(wf.get("min_universe", 20)),
        use_confidence=bool(cfg_get(config, "optimizer.use_confidence_adjusted_mu", True)),
        risk_aversion=float(cfg_get(config, "optimizer.risk_aversion", 5.0)),
        max_weight=float(cfg_get(config, "optimizer.max_weight_per_name", 0.05)),
        min_weight=float(cfg_get(config, "optimizer.min_weight_to_hold", 0.002)),
        gross=float(cfg_get(config, "optimizer.gross_exposure", 1.0)),
        solver=str(cfg_get(config, "optimizer.solver", "ECOS")),
        macro_shift_scale=float(cfg_get(config, "black_litterman_fusion.macro_sector_shift_scale", 0.5)),
        macro_max_shift=float(cfg_get(config, "black_litterman_fusion.macro_sector_max_shift", 0.15)),
        rc_cap=float(cfg_get(config, "sleeves.per_name_risk_contribution_cap", 0.08)),
        regime_gate_supportive_regimes=[str(s) for s in supportive_raw],
        regime_lever_mu_multiplier=float(wf.get("regime_lever_mu_multiplier", 1.5)),
        regime_lever_unsupported_mode=str(wf.get("regime_lever_unsupported_mode", "min_var")),
    )


def _grid_cases(config: dict[str, Any]) -> list[dict[str, Any]]:
    wf = cfg_get(config, "walkforward", {}) or {}
    sweep = wf.get("regime_sweep", {}) or {}
    base = _base_params(config)
    multipliers = _float_list(sweep.get("mu_multipliers"), [1.25, 1.5, 2.0])
    cadences = _int_list(sweep.get("rebalance_every_n_snapshots"),
                         [int(base["rebalance_every_n_snapshots"])])
    modes = _str_list(sweep.get("unsupported_modes"),
                      [str(base["regime_lever_unsupported_mode"])])
    supportive = _str_list(sweep.get("supportive_regimes"),
                           [str(s) for s in base["regime_gate_supportive_regimes"]])
    cases: list[dict[str, Any]] = []
    case_no = 0
    for cadence in cadences:
        for mode in modes:
            for multiplier in multipliers:
                case_no += 1
                cases.append({
                    "case_id": f"case_{case_no:03d}",
                    "supportive_regimes": supportive,
                    "unsupported_mode": mode,
                    "mu_multiplier": multiplier,
                    "rebalance_every_n_snapshots": cadence,
                })
    return cases


def _load_snapshots(config: dict[str, Any], paths: Any, lockbox: dict[str, str]) -> tuple[dict[str, list[dict[str, str]]], int]:
    store_dir = paths.output_dir / str(cfg_get(config, "snapshot_store.dir", "snapshot_store"))
    snapshots: dict[str, list[dict[str, str]]] = {}
    sealed_skipped = 0
    if store_dir.exists():
        for snap in sorted(store_dir.iterdir()):
            if not snap.is_dir() or not (snap / "stocks_scores.csv").exists():
                continue
            if snap.name >= lockbox["sealed_start"]:
                sealed_skipped += 1
                continue
            snapshots[snap.name] = read_csv(snap / "stocks_scores.csv")
    return snapshots, sealed_skipped


def _latest_panel(config: dict[str, Any], paths: Any) -> tuple[Path, dict[str, Any], pd.DataFrame]:
    panel_root = paths.output_dir / str(cfg_get(config, "survivorship_panel.dir", "survivorship_panel"))
    builds = sorted(
        p for p in panel_root.iterdir()
        if p.is_dir() and (p / "survivorship_manifest.json").exists()
    ) if panel_root.exists() else []
    if not builds:
        raise FileNotFoundError(f"No survivorship panel build under {panel_root}; run backtest/15b first")
    panel_dir = builds[-1]
    manifest_path = panel_dir / "survivorship_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("acceptance") != "PASS":
        raise ValueError(f"Survivorship panel {panel_dir.name} acceptance={manifest.get('acceptance')}; refusing")
    prices = pd.read_csv(panel_dir / "prices_adjclose.csv", index_col=0)
    prices.columns = [str(c).strip().upper() for c in prices.columns]
    return panel_dir, manifest, prices


def _selftest() -> None:
    rng = np.random.default_rng(16)
    start = date(2021, 1, 1)
    dates = [(start + timedelta(days=i)).isoformat() for i in range(260)]
    tickers = [f"T{i:02d}" for i in range(12)]
    scores = np.linspace(-0.20, 0.20, len(tickers))
    prices = pd.DataFrame(index=pd.Index(dates), columns=pd.Index(tickers), dtype=float)
    prices.iloc[0] = 100.0
    for i in range(1, len(dates)):
        supportive = 70 <= i < 150
        sign = 1.0 if supportive else -0.15
        daily = 0.0002 + sign * scores * 0.012 + rng.normal(0.0, 0.001, len(tickers))
        prices.iloc[i] = prices.iloc[i - 1].astype(float).to_numpy() * (1.0 + daily)
    snapshots: dict[str, list[dict[str, str]]] = {}
    for d in dates[75:-1]:
        snapshots[d] = [
            {
                "ticker": t,
                "final_score": f"{float(s):.8f}",
                "score_confidence": "1.0",
                "source_pipeline": "test_pipe",
            }
            for t, s in zip(tickers, scores)
        ]

    def regime_provider(d: str) -> dict[str, Any]:
        idx = dates.index(d)
        label = "HEATING_UP" if 70 <= idx < 150 else "STAGFLATION"
        return {"label": label, "gross_scalar": 1.0, "budgets": {}}

    params = dict(
        rebalance_every_n_snapshots=5,
        one_way_cost_bps=1.0,
        cov_lookback_trading_days=60,
        cov_min_obs=20,
        shrinkage_intensity=0.2,
        max_universe=12,
        min_universe=6,
        use_confidence=True,
        risk_aversion=5.0,
        max_weight=0.20,
        min_weight=0.0,
        gross=1.0,
        solver="ECOS",
        macro_shift_scale=0.0,
        macro_max_shift=0.0,
        rc_cap=0.50,
        regime_gate_supportive_regimes=["HEATING_UP"],
        regime_lever_mu_multiplier=2.0,
        regime_lever_unsupported_mode="min_var",
    )
    result = run_walkforward(
        snapshots=snapshots,
        prices=prices,
        arms=["aqr_only", "regime_lever"],
        params=params,
        regime_provider=regime_provider,
        sector_fit_provider=lambda _d: {"test_pipe": 0.0},
        rotation_provider=lambda _d: {"test_pipe": {"state": "Neutral", "rotation_multiplier": 1.0}},
    )
    summary = summarize_arms(
        result,
        ["aqr_only", "regime_lever"],
        verdict_cfg={
            "min_days": 50,
            "min_independent_windows": 2,
            "promote_net_ir_min": 0.0,
            "promote_active_t_min": 2.0,
        },
    )
    assert len(summary) == 2, summary
    assert not result["pit_violations"], result["pit_violations"]

    cfg = {
        "walkforward": {
            "regime_sweep": {
                "mu_multipliers": [1.25, 1.5],
                "rebalance_every_n_snapshots": [5, 21],
                "unsupported_modes": ["min_var", "cash"],
            }
        }
    }
    cases = _grid_cases(cfg)
    assert len(cases) == 8, cases
    assert {c["unsupported_mode"] for c in cases} == {"min_var", "cash"}
    print("SELFTEST PASS: regime parameter sweep")


def main() -> int:
    args = parse_args()
    configure_utc_logging()
    if args.selftest:
        _selftest()
        return 0

    config_path = args.config
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path=config_path)
    try:
        lockbox = load_lockbox(config, config_path)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1

    snapshots, sealed_skipped = _load_snapshots(config, paths, lockbox)
    if not snapshots:
        LOGGER.error("No dev-window snapshots; run research/65 first")
        return 1
    try:
        panel_dir, panel_manifest, prices = _latest_panel(config, paths)
    except (FileNotFoundError, ValueError) as exc:
        LOGGER.error("%s", exc)
        return 1

    out_dir = paths.output_dir / str(cfg_get(config, "walkforward.dir", "walkforward")) / panel_dir.name
    out_dir.mkdir(parents=True, exist_ok=True)
    sweep_path = out_dir / "regime_sweep.csv"
    manifest_path = out_dir / "regime_sweep_manifest.json"
    if args.force:
        for p in (sweep_path, manifest_path):
            if p.exists():
                p.unlink()
    try:
        fail_if_exists([sweep_path, manifest_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    taxonomy = sleeve_taxonomy(config)
    pipelines = [
        str(s.get("model_family"))
        for s in cfg_get(config, "score_contract.sectors", []) or []
        if bool(s.get("enabled", True))
    ]
    base_params = _base_params(config)
    cases = _grid_cases(config)
    if args.max_cases and args.max_cases > 0:
        cases = cases[:args.max_cases]
    if not cases:
        LOGGER.error("No regime sweep cases configured")
        return 1

    verdict_cfg = cfg_get(config, "walkforward", {}) or {}
    rows: list[dict[str, Any]] = []
    conn = open_macro_serving_db(paths.macro_serving_db_path)
    try:
        regime_provider, sector_fit_provider, rotation_provider = build_real_providers(
            config,
            conn=conn,
            prices=prices,
            pipelines=pipelines,
            taxonomy=taxonomy,
        )
        for case in cases:
            params = dict(base_params)
            params["regime_gate_supportive_regimes"] = list(case["supportive_regimes"])
            params["regime_lever_mu_multiplier"] = float(case["mu_multiplier"])
            params["regime_lever_unsupported_mode"] = str(case["unsupported_mode"])
            params["rebalance_every_n_snapshots"] = int(case["rebalance_every_n_snapshots"])
            LOGGER.info("Running %s", case)
            result = run_walkforward(
                snapshots=snapshots,
                prices=prices,
                arms=["aqr_only", "regime_lever"],
                params=params,
                regime_provider=regime_provider,
                sector_fit_provider=sector_fit_provider,
                rotation_provider=rotation_provider,
            )
            if result["pit_violations"]:
                LOGGER.error("%s PIT violations: %s", case["case_id"], result["pit_violations"][:5])
                return 1
            summary = summarize_arms(result, ["aqr_only", "regime_lever"], verdict_cfg=verdict_cfg)
            by_arm = {str(r["arm"]): r for r in summary}
            base = by_arm["aqr_only"]
            candidate = by_arm["regime_lever"]
            solver_failures = int(result["skipped"].get("solver", 0)) + int(
                (result.get("arm_solver_fallbacks") or {}).get("regime_lever", 0)
            )
            rejection_reasons = str(candidate["rejection_reasons"]).strip(";")
            if solver_failures:
                rejection_reasons = ";".join(
                    reason for reason in (rejection_reasons, f"solver_failures={solver_failures}") if reason
                )
            rows.append({
                "case_id": case["case_id"],
                "supportive_regimes": "|".join(str(s).upper() for s in case["supportive_regimes"]),
                "unsupported_mode": str(case["unsupported_mode"]),
                "mu_multiplier": float(case["mu_multiplier"]),
                "rebalance_every_n_snapshots": int(case["rebalance_every_n_snapshots"]),
                "n_rebalances": candidate["n_rebalances"],
                "n_days": candidate["n_days"],
                "baseline_net_return": base["net_ann_return"],
                "candidate_net_return": candidate["net_ann_return"],
                "baseline_net_sharpe": base["net_sharpe"],
                "candidate_net_sharpe": candidate["net_sharpe"],
                "candidate_max_drawdown_net": candidate["max_drawdown_net"],
                "active_net_ann_vs_baseline": candidate["active_net_ann_vs_baseline"],
                "tracking_error_ann": candidate["tracking_error_ann"],
                "net_ir_vs_baseline": candidate["net_ir_vs_baseline"],
                "active_t": candidate["active_t"],
                "turnover_per_year": candidate["turnover_per_year"],
                "cost_drag_per_year_bps": candidate["cost_drag_per_year_bps"],
                "solver_failures": solver_failures,
                "raw_promotable": int(candidate["promotable"] == 1 and solver_failures == 0),
                "multiple_test_active_t_min": "",
                "promotable": 0,
                "rejection_reasons": rejection_reasons,
            })
    finally:
        conn.close()

    familywise_alpha = float(
        (verdict_cfg.get("regime_sweep") or {}).get("familywise_alpha", 0.05)
    )
    if not 0.0 < familywise_alpha < 1.0:
        LOGGER.error("walkforward.regime_sweep.familywise_alpha must be in (0,1)")
        return 1
    corrected_t = max(
        float(verdict_cfg.get("promote_active_t_min", 2.0)),
        NormalDist().inv_cdf(1.0 - familywise_alpha / max(1, len(rows))),
    )
    for row in rows:
        active_t = float(row["active_t"])
        raw = str(row.get("raw_promotable")) == "1"
        familywise = raw and np.isfinite(active_t) and active_t >= corrected_t
        row["multiple_test_active_t_min"] = round(corrected_t, 6)
        row["promotable"] = int(familywise)
        if raw and not familywise:
            existing = str(row.get("rejection_reasons", "")).strip(";")
            correction_reason = f"familywise_active_t<{corrected_t:.3f}"
            row["rejection_reasons"] = ";".join(x for x in (existing, correction_reason) if x)
    write_csv(sweep_path, SWEEP_FIELDS, rows)
    sortable = [
        r for r in rows
        if str(r.get("candidate_net_sharpe", "")) not in {"", "nan", "None"}
    ]
    best_sharpe = max(sortable, key=lambda r: float(r["candidate_net_sharpe"])) if sortable else {}
    best_ir_rows = [r for r in sortable if str(r.get("net_ir_vs_baseline", "")) not in {"", "nan", "None"}]
    best_ir = max(best_ir_rows, key=lambda r: float(r["net_ir_vs_baseline"])) if best_ir_rows else {}
    promoted = [r for r in rows if str(r.get("promotable")) == "1"]
    manifest = {
        "stage": "11_regime_parameter_sweep",
        "acceptance": "PASS",
        "generated_at": utc_now(),
        "panel_build": panel_dir.name,
        "cases": len(rows),
        "promoted_cases": len(promoted),
        "raw_promotable_cases": sum(str(row.get("raw_promotable")) == "1" for row in rows),
        "solver_failure_cases": sum(int(row.get("solver_failures", 0)) > 0 for row in rows),
        "multiple_testing": {
            "method": "bonferroni_one_sided_normal_active_t",
            "familywise_alpha": familywise_alpha,
            "tests": len(rows),
            "required_active_t": corrected_t,
        },
        "sealed_snapshots_skipped": sealed_skipped,
        "inputs_sha256": {
            "config": sha256_file(config_path),
            "backtest/16b_run_regime_parameter_sweep.py": sha256_file(
                Path(__file__).resolve()
            ),
            "backtest/walkforward_common.py": sha256_file(
                PACKAGE_ROOT / "backtest" / "walkforward_common.py"
            ),
            "lockbox_protocol": sha256_file(LOCKBOX_PROTOCOL) if LOCKBOX_PROTOCOL.exists() else "",
            "survivorship_manifest": sha256_file(panel_dir / "survivorship_manifest.json"),
            "prices_adjclose": sha256_file(panel_dir / "prices_adjclose.csv"),
        },
        "source_panel_acceptance": panel_manifest.get("acceptance"),
        "best_by_candidate_net_sharpe": best_sharpe,
        "best_by_net_ir_vs_baseline": best_ir,
        "notes": [
            "Research-only sweep. A promotable row is evidence for review, not a production change.",
            "Promotable is familywise-error controlled across the full configured parameter grid.",
            "Unsupported regimes fail closed according to the tested mode; default is min_var.",
        ],
        "files": {
            "regime_sweep": str(sweep_path),
        },
    }
    write_manifest(manifest_path, manifest)
    LOGGER.info("Regime sweep complete: %d cases, %d promotable", len(rows), len(promoted))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

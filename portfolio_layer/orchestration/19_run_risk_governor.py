#!/usr/bin/env python3
"""Stage 12 - rule-based portfolio risk governor (SHADOW directive; nothing applies it yet).

Computes a gross-exposure directive for the current book from two rule-based signals only:

  drawdown circuit-breaker   trailing drawdown of the cost-adjusted book, marked on the sealed
                             Stage 2 returns panel, against `dd_limit`
  regime kill-switch         PIT macro regime (serving DB) in `risk_off_regimes`

The directive is the MINIMUM of the applicable multipliers (cuts compound conservatively, never
average), with hysteresis on recovery: after a drawdown cut, gross is restored only when the
trailing drawdown recovers inside `dd_limit * recovery_fraction`. State persists per as-of in the
directive file chain so re-risking is deliberate rather than flickering.

Output: runs/<as_of>/governor/gross_exposure_directive.json. SHADOW-ONLY by protocol: no stage
consumes the directive until Stage 11 promotes the governor; ML/forecast governors are optional
later plugins and are not part of this baseline.

`--selftest` exercises the decision rule: breach cuts, recovery re-risks with hysteresis,
risk-off regime cuts, and simultaneous conditions take the minimum.
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

from portfolio_layer.core.artifacts import invalidate_dependents  # noqa: E402
from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    read_csv,
    read_manifest,
    sealed_artifact_errors,
    sha256_file,
    write_manifest,
)
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.macro.contract import open_macro_serving_db, single_latest_row  # noqa: E402
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402
from portfolio_layer.sleeves.risk_model import trailing_book_drawdown  # noqa: E402


LOGGER = logging.getLogger("run_risk_governor")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stage 12 rule-based risk governor (shadow directive).")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", default=None)
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--force", action="store_true")
    return p.parse_args()


# ---------------------------------------------------------------------------
# pure decision rule (self-tested)
# ---------------------------------------------------------------------------
def governor_decision(
    *, trailing_drawdown: float, regime_label: str, was_dd_cut: bool, cfg: dict[str, Any],
) -> tuple[float, list[str], bool]:
    """Return (gross_multiplier, reasons, dd_cut_active).

    trailing_drawdown is <= 0 (0 = at the high). Hysteresis: once the drawdown breaker trips, it
    stays tripped until drawdown recovers inside dd_limit * recovery_fraction.
    """
    dd_limit = float(cfg.get("dd_limit", 0.15))
    dd_mult = float(cfg.get("dd_cut_multiplier", 0.5))
    recovery_fraction = float(cfg.get("recovery_fraction", 0.5))
    risk_off_mult = float(cfg.get("risk_off_multiplier", 0.75))
    risk_off = {str(r).upper() for r in cfg.get("risk_off_regimes", []) or []}

    numeric = {
        "dd_limit": dd_limit,
        "dd_cut_multiplier": dd_mult,
        "recovery_fraction": recovery_fraction,
        "risk_off_multiplier": risk_off_mult,
        "trailing_drawdown": float(trailing_drawdown),
    }
    bad_finite = [f"{k}={v}" for k, v in numeric.items() if not math.isfinite(v)]
    if bad_finite:
        raise ValueError(f"governor inputs must be finite: {bad_finite}")
    if not 0.0 < dd_limit <= 1.0:
        raise ValueError(f"dd_limit must be in (0,1], got {dd_limit}")
    for name, value in (("dd_cut_multiplier", dd_mult), ("risk_off_multiplier", risk_off_mult)):
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} must be in [0,1], got {value}")
    if not 0.0 <= recovery_fraction <= 1.0:
        raise ValueError(f"recovery_fraction must be in [0,1], got {recovery_fraction}")

    reasons: list[str] = []
    multipliers = [1.0]
    dd = abs(float(trailing_drawdown))
    dd_cut_active = bool(was_dd_cut)
    if dd >= dd_limit:
        dd_cut_active = True
    elif was_dd_cut and dd <= dd_limit * recovery_fraction:
        dd_cut_active = False
        reasons.append(f"drawdown_recovered:{dd:.4f}<= {dd_limit * recovery_fraction:.4f}")
    if dd_cut_active:
        multipliers.append(dd_mult)
        reasons.append(f"drawdown_breaker:{dd:.4f}>=limit_{dd_limit:.4f}"
                       if dd >= dd_limit else f"drawdown_hysteresis_hold:{dd:.4f}")
    if str(regime_label).upper() in risk_off:
        multipliers.append(risk_off_mult)
        reasons.append(f"regime_kill_switch:{regime_label}")
    if len(multipliers) == 1:
        reasons.append("no_cuts")
    return min(multipliers), reasons, dd_cut_active


def _selftest() -> None:
    cfg = {"dd_limit": 0.15, "dd_cut_multiplier": 0.5, "recovery_fraction": 0.5,
           "risk_off_multiplier": 0.75, "risk_off_regimes": ["CRISIS", "CONTRACTION"]}
    m, r, cut = governor_decision(trailing_drawdown=-0.05, regime_label="EXPANSION",
                                  was_dd_cut=False, cfg=cfg)
    assert m == 1.0 and not cut and "no_cuts" in r, (m, r, cut)
    m, r, cut = governor_decision(trailing_drawdown=-0.20, regime_label="EXPANSION",
                                  was_dd_cut=False, cfg=cfg)
    assert m == 0.5 and cut and any("drawdown_breaker" in x for x in r)
    # hysteresis: recovered to -10% (inside limit but above recovery threshold) -> cut holds
    m, r, cut = governor_decision(trailing_drawdown=-0.10, regime_label="EXPANSION",
                                  was_dd_cut=True, cfg=cfg)
    assert m == 0.5 and cut and any("hysteresis" in x for x in r)
    # full recovery to -5% (<= 7.5%) -> re-risk
    m, r, cut = governor_decision(trailing_drawdown=-0.05, regime_label="EXPANSION",
                                  was_dd_cut=True, cfg=cfg)
    assert m == 1.0 and not cut and any("recovered" in x for x in r)
    # regime cut alone
    m, r, cut = governor_decision(trailing_drawdown=-0.02, regime_label="CRISIS",
                                  was_dd_cut=False, cfg=cfg)
    assert m == 0.75 and not cut and any("kill_switch" in x for x in r)
    # both -> min, not product
    m, r, cut = governor_decision(trailing_drawdown=-0.30, regime_label="CRISIS",
                                  was_dd_cut=False, cfg=cfg)
    assert m == 0.5 and cut, (m, cut)
    # drawdown math: monotone decline of one name
    rets = pd.DataFrame({"A": [-0.01] * 30})
    rets.index = [f"2000-01-{i+1:02d}" for i in range(30)]
    dd = trailing_book_drawdown({"A": 1.0}, rets, window=30)
    assert -0.27 < dd < -0.25, dd
    try:
        governor_decision(
            trailing_drawdown=-0.05,
            regime_label="EXPANSION",
            was_dd_cut=False,
            cfg={**cfg, "risk_off_multiplier": 1.1},
        )
    except ValueError:
        pass
    else:
        raise AssertionError("out-of-range governor multiplier must fail")
    try:
        trailing_book_drawdown({"MISSING": 1.0}, rets, window=30)
    except ValueError:
        pass
    else:
        raise AssertionError("missing held-name returns must fail closed")
    print("risk-governor self-test: PASS")


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    if args.selftest:
        _selftest()
        return 0
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    gov = cfg_get(config, "risk_governor", {}) or {}
    gov.setdefault("risk_off_regimes", cfg_get(config, "sleeves.risk_off_regimes", []) or [])
    runs_root = paths.output_dir / "runs"
    run_as_of = args.as_of or latest_run_with(runs_root, "stocks_scores.csv")
    if not run_as_of:
        LOGGER.error("No run found under %s", runs_root)
        return 1
    run_dir = runs_root / run_as_of
    weights_path = run_dir / "costs" / "cost_adjusted_target_weights.csv"
    weights_manifest_path = run_dir / "costs" / "cost_manifest.json"
    weights_manifest_key = "cost_adjusted_target_weights.csv"
    if not weights_path.exists():
        weights_path = run_dir / "optimizer" / "target_weights.csv"
        weights_manifest_path = run_dir / "optimizer" / "optimizer_manifest.json"
        weights_manifest_key = "target_weights.csv"
    returns_path = run_dir / "risk" / "returns_panel.csv"
    risk_manifest_path = run_dir / "risk" / "risk_manifest.json"
    if not all(p.exists() for p in (weights_path, weights_manifest_path, returns_path, risk_manifest_path)):
        LOGGER.error("Need a book (%s) and returns panel (%s)", weights_path, returns_path)
        return 1
    out_path = run_dir / "governor" / "gross_exposure_directive.json"
    manifest_path = run_dir / "governor" / "governor_manifest.json"
    if args.force:
        invalidate_dependents(run_dir, "governor")
    try:
        fail_if_exists([out_path, manifest_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    weights_manifest = read_manifest(weights_manifest_path)
    risk_manifest = read_manifest(risk_manifest_path)
    input_errors = sealed_artifact_errors(
        weights_manifest, weights_path, weights_manifest_key, run_as_of=run_as_of,
    )
    input_errors.extend(
        sealed_artifact_errors(risk_manifest, returns_path, "returns_panel.csv", run_as_of=run_as_of)
    )
    if input_errors:
        LOGGER.error("Governor inputs are not sealed/current: %s", input_errors)
        return 1

    weights: dict[str, float] = {}
    book_errors: list[str] = []
    for row_number, r in enumerate(read_csv(weights_path), start=2):
        t = str(r.get("ticker", "")).strip().upper()
        raw_weight = r.get("weight")
        try:
            w = float(raw_weight)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            book_errors.append(f"row={row_number}:{t or '<blank>'}:weight={raw_weight!r}")
            continue
        if not t or not math.isfinite(w) or w < -1e-12 or t in weights:
            book_errors.append(f"row={row_number}:ticker={t!r}:weight={raw_weight!r}:duplicate={t in weights}")
            continue
        if t and t != "CASH" and w > 0:
            weights[t] = w
    if book_errors or not weights:
        LOGGER.error("Governor book is malformed/empty: %s", book_errors[:12])
        return 1
    returns = pd.read_csv(returns_path, index_col=0)
    returns.columns = [str(c).strip().upper() for c in returns.columns]
    window = int(gov.get("drawdown_window_trading_days", 63))
    min_complete = float(gov.get("drawdown_min_complete_fraction", 0.80))
    try:
        dd = trailing_book_drawdown(
            weights, returns, window=window, min_complete_fraction=min_complete,
        )
    except ValueError as exc:
        LOGGER.error("Cannot calculate fail-closed governor drawdown: %s", exc)
        return 1

    macro_db = paths.macro_serving_db_path
    if not macro_db.exists():
        LOGGER.error("Macro serving DB missing: %s", macro_db)
        return 1
    macro_hash_before = sha256_file(macro_db)
    conn = open_macro_serving_db(paths.macro_serving_db_path)
    try:
        row = single_latest_row(conn, "macro_regime_decision_daily", run_as_of)
    finally:
        conn.close()
    macro_hash_after = sha256_file(macro_db)
    if macro_hash_before != macro_hash_after:
        LOGGER.error("Macro serving DB changed while the governor was reading it; retry the run")
        return 1
    regime_label = str(row["active_current_regime"] or "").strip() if row is not None else "UNKNOWN"
    regime_as_of = str(row["as_of_date"] or "").strip() if row is not None else ""
    if regime_as_of and regime_as_of > run_as_of:
        LOGGER.error("Governor macro regime is future-dated: %s > %s", regime_as_of, run_as_of)
        return 1
    decision_cfg = dict(gov)
    if row is None or not regime_label:
        regime_label = "UNKNOWN"
        decision_cfg["risk_off_regimes"] = sorted(
            {str(x).upper() for x in gov.get("risk_off_regimes", []) or []} | {"UNKNOWN"}
        )

    # hysteresis state: read the previous directive (latest earlier run with one)
    was_dd_cut = False
    prior_state_status = "none"
    prior = sorted(
        p for p in runs_root.iterdir()
        if p.is_dir() and p.name < run_as_of and (p / "governor" / "gross_exposure_directive.json").exists()
    )
    if prior:
        prior_path = prior[-1] / "governor" / "gross_exposure_directive.json"
        prior_manifest_path = prior[-1] / "governor" / "governor_manifest.json"
        try:
            prev_manifest = read_manifest(prior_manifest_path)
            prior_bad = sealed_artifact_errors(
                prev_manifest,
                prior_path,
                "gross_exposure_directive.json",
                run_as_of=prior[-1].name,
            )
            if prior_bad:
                raise ValueError(str(prior_bad))
            prev = read_manifest(prior_path)
            was_dd_cut = bool(prev.get("dd_cut_active", False))
            prior_state_status = "sealed"
        except (OSError, ValueError):
            # Unknown hysteresis state must never cause an automatic re-risk. Hold the cut until a
            # subsequent sealed directive observes recovery.
            was_dd_cut = True
            prior_state_status = "invalid_assume_cut"

    multiplier, reasons, dd_cut_active = governor_decision(
        trailing_drawdown=dd, regime_label=regime_label, was_dd_cut=was_dd_cut, cfg=decision_cfg,
    )
    if prior_state_status == "invalid_assume_cut":
        reasons.append("prior_directive_unsealed:conservative_cut_hold")
    directive = {
        "stage": "stage12_risk_governor",
        "run_as_of": run_as_of,
        "generated_at": utc_now(),
        "shadow_only": True,
        "applied": False,
        "gross_exposure_multiplier": multiplier,
        "reasons": reasons,
        "dd_cut_active": dd_cut_active,
        "trailing_drawdown": round(dd, 6),
        "drawdown_window_trading_days": window,
        "drawdown_min_complete_fraction": min_complete,
        "regime": regime_label,
        "regime_as_of": regime_as_of,
        "book_source": weights_path.name,
        "held_names": len(weights),
        "prior_directive": prior[-1].name if prior else "",
        "prior_state_status": prior_state_status,
    }
    write_manifest(out_path, directive)
    checks = [
        {"check": "inputs_sealed_current", "status": "PASS", "detail": "book and return panel hashes match"},
        {"check": "drawdown_coverage", "status": "PASS",
         "detail": f"held={len(weights)} window={window} min_complete={min_complete}"},
        {"check": "macro_pit", "status": "PASS",
         "detail": f"regime={regime_label} regime_as_of={regime_as_of or 'missing_conservative'}<=run={run_as_of}"},
        {"check": "multiplier_bounded", "status": "PASS" if 0.0 <= multiplier <= 1.0 else "FAIL",
         "detail": f"multiplier={multiplier}"},
    ]
    acceptance = "PASS" if all(c["status"] == "PASS" for c in checks) else "FAIL"
    write_manifest(manifest_path, {
        "stage": "stage12_risk_governor_validation",
        "run_as_of": run_as_of,
        "generated_at": utc_now(),
        "acceptance": acceptance,
        "checks": checks,
        "inputs_sha256": {
            "book": sha256_file(weights_path),
            "book_manifest": sha256_file(weights_manifest_path),
            "returns_panel.csv": sha256_file(returns_path),
            "risk_manifest.json": sha256_file(risk_manifest_path),
            "macro_serving.sqlite": macro_hash_after,
            **({"prior_directive": sha256_file(prior_path),
                "prior_governor_manifest": sha256_file(prior_manifest_path)}
               if prior and prior_state_status == "sealed" else {}),
        },
        "files": {
            "gross_exposure_directive.json": {"sha256": sha256_file(out_path)},
        },
    })
    LOGGER.info("GOVERNOR %s: multiplier=%.2f dd=%.4f regime=%s reasons=%s (SHADOW, not applied) -> %s",
                run_as_of, multiplier, dd, regime_label or "?", reasons, out_path)
    return 0 if acceptance == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

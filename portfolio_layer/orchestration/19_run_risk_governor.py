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
import json
import logging
import sys
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import read_csv, write_manifest  # noqa: E402
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
    if not weights_path.exists():
        weights_path = run_dir / "optimizer" / "target_weights.csv"
    returns_path = run_dir / "risk" / "returns_panel.csv"
    if not (weights_path.exists() and returns_path.exists()):
        LOGGER.error("Need a book (%s) and returns panel (%s)", weights_path, returns_path)
        return 1
    out_path = run_dir / "governor" / "gross_exposure_directive.json"
    if out_path.exists() and not args.force:
        LOGGER.error("%s exists (use --force)", out_path)
        return 1

    weights = {}
    for r in read_csv(weights_path):
        t = str(r.get("ticker", "")).strip().upper()
        raw_weight = r.get("weight")
        if raw_weight is None:
            continue
        try:
            w = float(raw_weight)
        except (TypeError, ValueError):
            continue
        if t and t != "CASH" and w > 0:
            weights[t] = w
    returns = pd.read_csv(returns_path, index_col=0)
    returns.columns = [str(c).strip().upper() for c in returns.columns]
    window = int(gov.get("drawdown_window_trading_days", 63))
    dd = trailing_book_drawdown(weights, returns, window=window)

    conn = open_macro_serving_db(paths.macro_serving_db_path)
    try:
        row = single_latest_row(conn, "macro_regime_decision_daily", run_as_of)
    finally:
        conn.close()
    regime_label = str(row["active_current_regime"] or "") if row is not None else ""

    # hysteresis state: read the previous directive (latest earlier run with one)
    was_dd_cut = False
    prior = sorted(
        p for p in runs_root.iterdir()
        if p.is_dir() and p.name < run_as_of and (p / "governor" / "gross_exposure_directive.json").exists()
    )
    if prior:
        try:
            prev = json.loads((prior[-1] / "governor" / "gross_exposure_directive.json").read_text(encoding="utf-8"))
            was_dd_cut = bool(prev.get("dd_cut_active", False))
        except (OSError, json.JSONDecodeError):
            was_dd_cut = False

    multiplier, reasons, dd_cut_active = governor_decision(
        trailing_drawdown=dd, regime_label=regime_label, was_dd_cut=was_dd_cut, cfg=gov,
    )
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
        "regime": regime_label,
        "book_source": weights_path.name,
        "held_names": len(weights),
        "prior_directive": prior[-1].name if prior else "",
    }
    write_manifest(out_path, directive)
    LOGGER.info("GOVERNOR %s: multiplier=%.2f dd=%.4f regime=%s reasons=%s (SHADOW, not applied) -> %s",
                run_as_of, multiplier, dd, regime_label or "?", reasons, out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

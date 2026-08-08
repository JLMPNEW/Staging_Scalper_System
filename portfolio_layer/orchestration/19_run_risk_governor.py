#!/usr/bin/env python3
"""Stage 12 - rule-based portfolio risk governor (SHADOW directive; nothing applies it yet).

Computes a gross-exposure directive for the current book from two rule-based signals only:

  drawdown circuit-breaker   trailing drawdown of the cost-adjusted book, marked on the sealed
                             Stage 2 returns panel, against `dd_limit`
  regime kill-switch         PIT macro regime from the CONFIGURED source (`macro.regime_source`
                             via the Stage 6 contract module), validated with
                             `regime_application_errors` and the `macro.freshness_tolerance_days.
                             regime` gate; any defect routes the regime to UNKNOWN, which is
                             added to the risk_off set (conservative cut, never a silent pass)

The directive is the MINIMUM of the applicable multipliers (cuts compound conservatively, never
average), with hysteresis on recovery: after a drawdown cut, gross is restored only when the
trailing drawdown recovers inside `dd_limit * recovery_fraction`. State persists per as-of in the
directive file chain so re-risking is deliberate rather than flickering. A legitimately all-CASH
sealed book is valid: the directive is 1.0 with reason `no_risky_positions` (there is no gross
exposure to govern) while any held drawdown-cut state carries forward.

Macro serving DB reads are sealed with the WAL-aware content digest from
`portfolio_layer/macro/contract.py` (`macro_serving_content_sha256` + `sqlite_snapshot_inputs`),
both for the read-stability concurrency guard and the manifest input pins.

Force-rerun hygiene: rewriting the directive for date D leaves later-dated directives that pinned
D's directive sha stale. After a rewrite, any later governor run whose sealed
`inputs_sha256.prior_directive` no longer matches gets a NON-destructive
`governor/PRIOR_DIRECTIVE_STALE.json` marker (plus an ERROR log). When this script reads a prior
directive for hysteresis and that prior governor dir carries the marker, the prior is treated as
invalid via the existing conservative assume-cut path.

Output: runs/<as_of>/governor/gross_exposure_directive.json. SHADOW-ONLY by protocol: no stage
consumes the directive until Stage 11 promotes the governor; ML/forecast governors are optional
later plugins and are not part of this baseline.

`--selftest` exercises the decision rule (breach cuts, hysteresis, regime cuts, minimum of
simultaneous conditions), the regime gate (validation, freshness, future-dated), book parsing
(all-cash validity, malformed rows), regime-source resolution, and the stale-marker scan.
"""
from __future__ import annotations

import argparse
import logging
import math
import sqlite3
import sys
from datetime import date
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
from portfolio_layer.macro.contract import (  # noqa: E402
    macro_serving_content_sha256,
    open_macro_serving_db,
    regime_application_errors,
    regime_table_for_source,
    single_latest_regime_row,
    sqlite_snapshot_inputs,
    staleness_days,
)
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402
from portfolio_layer.sleeves.risk_model import trailing_book_drawdown  # noqa: E402


LOGGER = logging.getLogger("run_risk_governor")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
# Shared contract with orchestration/20: a governor dir carrying this marker consumed a
# force-replaced prior directive and must be regenerated before it is trusted again.
STALE_MARKER_NAME = "PRIOR_DIRECTIVE_STALE.json"
DEFAULT_REGIME_FRESHNESS_TOLERANCE_DAYS = 5
_CONSERVATIVE_CHECK_STATUSES = {
    "CONSERVATIVE_UNKNOWN",
    "STALE_CONSERVATIVE",
    "CONSERVATIVE_ASSUME_CUT",
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stage 12 rule-based risk governor (shadow directive).")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", default=None)
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def _is_iso_run_name(name: str) -> bool:
    try:
        return date.fromisoformat(name).isoformat() == name
    except ValueError:
        return False


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


def resolve_regime_model_version(config: dict[str, Any], regime_source: str) -> str | None:
    """Resolve the model_version the configured regime source requires (None for v1)."""
    if regime_source == "v2":
        version = str(cfg_get(config, "macro.regime_v2_model_version", "") or "").strip()
        if not version:
            raise ValueError("macro.regime_v2_model_version is required when macro.regime_source=v2")
        return version
    if regime_source == "h1":
        version = str(
            cfg_get(config, "macro.regime_h1_model_version", "macro_regime_h1_hybrid_v1") or ""
        ).strip()
        if not version:
            raise ValueError("macro.regime_h1_model_version is required when macro.regime_source=h1")
        return version
    return None


def regime_gate(
    *, row: dict[str, Any] | None, run_as_of: str, tolerance_days: int,
) -> dict[str, Any]:
    """Validate a raw serving-DB regime row for governor use.

    Any application-validation error or freshness breach routes the label to UNKNOWN (callers add
    UNKNOWN to the risk_off set: conservative cut). A future-dated row raises: that is corrupt
    PIT data, not a conservative condition.
    """
    if tolerance_days < 0:
        raise ValueError(f"regime freshness tolerance must be >= 0 days, got {tolerance_days}")
    validation_errors = regime_application_errors(row)
    label = str((row or {}).get("active_current_regime") or "").strip()
    regime_as_of = str((row or {}).get("as_of_date") or "").strip()
    stale = staleness_days(run_as_of, regime_as_of) if regime_as_of else None
    # Defense-in-depth: a regime decision dated after the run is never PIT-consumable.
    if (stale is not None and stale < 0) or (regime_as_of and regime_as_of > run_as_of):
        raise ValueError(f"macro regime is future-dated: {regime_as_of} > {run_as_of}")
    freshness_status = (
        "PASS" if stale is not None and stale <= tolerance_days else "STALE_CONSERVATIVE"
    )
    validation_status = "PASS" if not validation_errors else "CONSERVATIVE_UNKNOWN"
    if validation_errors or freshness_status != "PASS":
        label = "UNKNOWN"
    return {
        "regime": label,
        "regime_as_of": regime_as_of,
        "staleness_days": stale,
        "validation_errors": validation_errors,
        "validation_status": validation_status,
        "freshness_status": freshness_status,
        "tolerance_days": tolerance_days,
    }


def parse_governor_book(rows: list[dict[str, str]]) -> tuple[dict[str, float], list[str]]:
    """Parse sealed book rows into positive risky weights. All-CASH (empty) is a VALID book."""
    weights: dict[str, float] = {}
    errors: list[str] = []
    for row_number, r in enumerate(rows, start=2):
        t = str(r.get("ticker", "")).strip().upper()
        raw_weight = r.get("weight")
        try:
            w = float(raw_weight)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            errors.append(f"row={row_number}:{t or '<blank>'}:weight={raw_weight!r}")
            continue
        if not t or not math.isfinite(w) or w < -1e-12 or t in weights:
            errors.append(f"row={row_number}:ticker={t!r}:weight={raw_weight!r}:duplicate={t in weights}")
            continue
        if t != "CASH" and w > 0:
            weights[t] = w
    return weights, errors


def mark_stale_dependent_runs(runs_root: Path, run_as_of: str, new_directive_sha: str) -> list[str]:
    """Non-destructively flag later governor runs whose sealed prior-directive pin was replaced.

    A later run is stale when its directive consumed THIS run's directive
    (`prior_directive == run_as_of`) and its governor manifest pinned a sha that no longer matches
    the rewritten directive. The marker never deletes artifacts; orchestration/20 refuses a run
    dir carrying it and a forced 19 re-run regenerates + clears it.
    """
    stale_runs: list[str] = []
    if not runs_root.exists():
        return stale_runs
    later_dirs = sorted(
        p for p in runs_root.iterdir()
        if p.is_dir() and _is_iso_run_name(p.name) and p.name > run_as_of
    )
    for later in later_dirs:
        governor_dir = later / "governor"
        directive_path = governor_dir / "gross_exposure_directive.json"
        manifest_path = governor_dir / "governor_manifest.json"
        if not directive_path.exists() or not manifest_path.exists():
            continue
        try:
            later_directive = read_manifest(directive_path)
            later_manifest = read_manifest(manifest_path)
        except ValueError as exc:
            LOGGER.warning("Cannot inspect later governor run %s for a stale prior pin: %s", later.name, exc)
            continue
        if str(later_directive.get("prior_directive", "")) != run_as_of:
            continue
        inputs = later_manifest.get("inputs_sha256")
        pinned = str(inputs.get("prior_directive", "")) if isinstance(inputs, dict) else ""
        if not pinned or pinned == new_directive_sha:
            continue
        write_manifest(governor_dir / STALE_MARKER_NAME, {
            "marker": "PRIOR_DIRECTIVE_STALE",
            "reason": (
                "prior gross_exposure_directive.json was force-rewritten after this run "
                "consumed its dd_cut_active state; the sealed prior_directive pin no longer matches"
            ),
            "stale_run_as_of": later.name,
            "replaced_prior_run_as_of": run_as_of,
            "pinned_prior_directive_sha256": pinned,
            "new_prior_directive_sha256": new_directive_sha,
            "generated_at": utc_now(),
        })
        stale_runs.append(later.name)
    return stale_runs


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
    # a gated UNKNOWN regime routed through the conservative risk_off set cuts gross
    m, r, cut = governor_decision(
        trailing_drawdown=-0.02, regime_label="UNKNOWN", was_dd_cut=False,
        cfg={**cfg, "risk_off_regimes": ["CRISIS", "CONTRACTION", "UNKNOWN"]},
    )
    assert m == 0.75 and any("kill_switch" in x for x in r), (m, r)
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

    # --- regime gate: contract validation + freshness + future-dated defense-in-depth ---
    good_row = {
        "as_of_date": "2026-01-05", "active_current_regime": "STAGFLATION",
        "active_next_regime": "SLOW_GROWTH", "current_confidence": 0.8,
        "next_confidence": 0.6, "coverage_flag": 1, "regime_override_reason": "",
    }
    state = regime_gate(row=good_row, run_as_of="2026-01-07", tolerance_days=5)
    assert state["regime"] == "STAGFLATION" and state["staleness_days"] == 2, state
    assert state["validation_status"] == "PASS" and state["freshness_status"] == "PASS", state
    state = regime_gate(row=None, run_as_of="2026-01-07", tolerance_days=5)
    assert state["regime"] == "UNKNOWN" and "missing_regime_row" in state["validation_errors"], state
    assert state["freshness_status"] == "STALE_CONSERVATIVE", state
    state = regime_gate(row={**good_row, "as_of_date": "2025-12-01"},
                        run_as_of="2026-01-07", tolerance_days=5)
    assert state["regime"] == "UNKNOWN" and state["freshness_status"] == "STALE_CONSERVATIVE", state
    for bad in (
        {**good_row, "active_current_regime": "EXPANSION"},   # legacy label outside vocabulary
        {**good_row, "coverage_flag": 0},
        {**good_row, "regime_override_reason": "UNCOVERED_MACRO"},
        {**good_row, "current_confidence": 1.5},
    ):
        state = regime_gate(row=bad, run_as_of="2026-01-07", tolerance_days=5)
        assert state["regime"] == "UNKNOWN" and state["validation_errors"], (bad, state)
    try:
        regime_gate(row={**good_row, "as_of_date": "2026-02-01"},
                    run_as_of="2026-01-07", tolerance_days=5)
    except ValueError:
        pass
    else:
        raise AssertionError("future-dated regime must fail closed")

    # --- regime source / model-version resolution ---
    assert resolve_regime_model_version({}, "v1") is None
    assert resolve_regime_model_version({}, "h1") == "macro_regime_h1_hybrid_v1"
    assert resolve_regime_model_version({"macro": {"regime_v2_model_version": "x"}}, "v2") == "x"
    try:
        resolve_regime_model_version({}, "v2")
    except ValueError:
        pass
    else:
        raise AssertionError("v2 regime source without a model_version must fail")

    # --- book parsing: all-CASH is valid; malformed rows are not ---
    weights, errors = parse_governor_book([
        {"ticker": "aapl", "weight": "0.5"}, {"ticker": "CASH", "weight": "0.5"},
    ])
    assert weights == {"AAPL": 0.5} and not errors, (weights, errors)
    weights, errors = parse_governor_book([{"ticker": "CASH", "weight": "1.0"}])
    assert weights == {} and not errors, (weights, errors)
    for bad_rows in (
        [{"ticker": "AAPL", "weight": "abc"}],
        [{"ticker": "AAPL", "weight": "0.2"}, {"ticker": "AAPL", "weight": "0.1"}],
        [{"ticker": "MSFT", "weight": "-0.2"}],
    ):
        _, errors = parse_governor_book(bad_rows)
        assert errors, bad_rows

    # --- stale-marker scan after a forced rewrite ---
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        stale_dir = root / "2026-01-02" / "governor"
        write_manifest(stale_dir / "gross_exposure_directive.json", {"prior_directive": "2026-01-01"})
        write_manifest(stale_dir / "governor_manifest.json",
                       {"inputs_sha256": {"prior_directive": "oldsha"}})
        fresh_dir = root / "2026-01-03" / "governor"
        write_manifest(fresh_dir / "gross_exposure_directive.json", {"prior_directive": "2026-01-01"})
        write_manifest(fresh_dir / "governor_manifest.json",
                       {"inputs_sha256": {"prior_directive": "newsha"}})
        unpinned_dir = root / "2026-01-04" / "governor"
        write_manifest(unpinned_dir / "gross_exposure_directive.json", {"prior_directive": "2026-01-03"})
        write_manifest(unpinned_dir / "governor_manifest.json", {"inputs_sha256": {}})
        marked = mark_stale_dependent_runs(root, "2026-01-01", "newsha")
        assert marked == ["2026-01-02"], marked
        marker = read_manifest(stale_dir / STALE_MARKER_NAME)
        assert marker["pinned_prior_directive_sha256"] == "oldsha"
        assert marker["new_prior_directive_sha256"] == "newsha"
        assert not (fresh_dir / STALE_MARKER_NAME).exists()
        assert not (unpinned_dir / STALE_MARKER_NAME).exists()
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
    # Hysteresis scans and PIT comparisons are lexicographic: only canonical zero-padded ISO is safe.
    try:
        parsed_as_of = date.fromisoformat(str(run_as_of))
    except ValueError:
        LOGGER.error("--as-of must be an ISO date (YYYY-MM-DD), got %r", run_as_of)
        return 1
    if parsed_as_of.isoformat() != run_as_of:
        LOGGER.error("--as-of must use canonical YYYY-MM-DD form, got %r", run_as_of)
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

    weights, book_errors = parse_governor_book(read_csv(weights_path))
    if book_errors:
        LOGGER.error("Governor book is malformed: %s", book_errors[:12])
        return 1
    # An empty risky book after the CASH/zero filter is a legitimate all-CASH stance, not an error.
    all_cash_book = not weights

    window = int(gov.get("drawdown_window_trading_days", 63))
    min_complete = float(gov.get("drawdown_min_complete_fraction", 0.80))
    dd: float | None = None
    if not all_cash_book:
        returns = pd.read_csv(returns_path, index_col=0)
        returns.columns = [str(c).strip().upper() for c in returns.columns]
        try:
            dd = trailing_book_drawdown(
                weights, returns, window=window, min_complete_fraction=min_complete,
            )
        except ValueError as exc:
            LOGGER.error("Cannot calculate fail-closed governor drawdown: %s", exc)
            return 1

    # ------------------------------------------------------------------
    # macro regime consumption through the Stage 6 contract module
    # ------------------------------------------------------------------
    regime_source = str(cfg_get(config, "macro.regime_source", "v1") or "").strip().lower()
    regime_table = ""
    regime_model_version: str | None = None
    regime_state: dict[str, Any] = {
        "regime": "",
        "regime_as_of": "",
        "staleness_days": None,
        "validation_errors": [],
        "validation_status": "SKIPPED_ALL_CASH",
        "freshness_status": "SKIPPED_ALL_CASH",
        "tolerance_days": None,
    }
    macro_input_hashes: dict[str, str] = {}
    if not all_cash_book:
        try:
            regime_table = regime_table_for_source(regime_source)
            regime_model_version = resolve_regime_model_version(config, regime_source)
        except ValueError as exc:
            LOGGER.error("%s", exc)
            return 1
        raw_tolerance = cfg_get(
            config, "macro.freshness_tolerance_days.regime", DEFAULT_REGIME_FRESHNESS_TOLERANCE_DAYS,
        )
        try:
            regime_tolerance_days = int(raw_tolerance)
        except (TypeError, ValueError):
            LOGGER.error("macro.freshness_tolerance_days.regime must be an integer, got %r", raw_tolerance)
            return 1
        macro_db = paths.macro_serving_db_path
        if not macro_db.exists():
            LOGGER.error("Macro serving DB missing: %s", macro_db)
            return 1
        try:
            content_before = macro_serving_content_sha256(
                macro_db, run_as_of, regime_table=regime_table, regime_model_version=regime_model_version,
            )
            conn = open_macro_serving_db(macro_db)
            try:
                row = single_latest_regime_row(
                    conn,
                    source=regime_source,
                    run_as_of=run_as_of,
                    model_version=regime_model_version,
                    covered_only=True,
                )
            finally:
                conn.close()
            content_after = macro_serving_content_sha256(
                macro_db, run_as_of, regime_table=regime_table, regime_model_version=regime_model_version,
            )
            snapshot_files = sqlite_snapshot_inputs(macro_db)
        except (sqlite3.Error, OSError, ValueError) as exc:
            LOGGER.error("Cannot read the macro regime fail-closed: %s", exc)
            return 1
        if content_before != content_after:
            LOGGER.error("Macro serving DB content changed while the governor was reading it; retry the run")
            return 1
        macro_input_hashes["macro_serving.sqlite:content"] = content_after
        for name, snapshot_path in snapshot_files.items():
            macro_input_hashes[f"macro_serving_snapshot:{name}"] = sha256_file(snapshot_path)
        try:
            regime_state = regime_gate(
                row=dict(row) if row is not None else None,
                run_as_of=run_as_of,
                tolerance_days=regime_tolerance_days,
            )
        except ValueError as exc:
            LOGGER.error("Governor macro regime failed fail-closed validation: %s", exc)
            return 1
        if regime_state["freshness_status"] != "PASS":
            LOGGER.warning(
                "Macro regime is stale/missing for %s (regime_as_of=%s staleness_days=%s tolerance=%s); "
                "routing UNKNOWN through the conservative risk_off path",
                run_as_of, regime_state["regime_as_of"] or "<missing>",
                regime_state["staleness_days"], regime_tolerance_days,
            )
        if regime_state["validation_errors"]:
            LOGGER.warning(
                "Macro regime row failed application validation %s; routing UNKNOWN through the "
                "conservative risk_off path", regime_state["validation_errors"],
            )

    regime_label = str(regime_state["regime"])
    decision_cfg = dict(gov)
    if not all_cash_book and regime_label == "UNKNOWN":
        decision_cfg["risk_off_regimes"] = sorted(
            {str(x).upper() for x in gov.get("risk_off_regimes", []) or []} | {"UNKNOWN"}
        )

    # hysteresis state: read the previous directive (latest earlier run with one)
    was_dd_cut = False
    prior_state_status = "none"
    prior_path: Path | None = None
    prior_manifest_path: Path | None = None
    prior = sorted(
        p for p in runs_root.iterdir()
        if p.is_dir() and _is_iso_run_name(p.name) and p.name < run_as_of
        and (p / "governor" / "gross_exposure_directive.json").exists()
    )
    if prior:
        prior_governor_dir = prior[-1] / "governor"
        prior_path = prior_governor_dir / "gross_exposure_directive.json"
        prior_manifest_path = prior_governor_dir / "governor_manifest.json"
        if (prior_governor_dir / STALE_MARKER_NAME).exists():
            # The prior directive consumed a force-replaced ancestor: its dd_cut_active state is
            # untrusted. Unknown hysteresis state must never cause an automatic re-risk.
            was_dd_cut = True
            prior_state_status = "stale_marker_assume_cut"
        else:
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
                # Unknown hysteresis state must never cause an automatic re-risk. Hold the cut
                # until a subsequent sealed directive observes recovery.
                was_dd_cut = True
                prior_state_status = "invalid_assume_cut"

    if all_cash_book:
        # Nothing risky to govern: full multiplier by definition, while any held drawdown-cut
        # state carries forward (recovery cannot be observed without a marked book).
        multiplier = 1.0
        dd_cut_active = was_dd_cut
        reasons = ["no_risky_positions"]
        if was_dd_cut:
            reasons.append("dd_cut_state_held:all_cash_book")
    else:
        assert dd is not None  # set above for every non-all-cash book
        multiplier, reasons, dd_cut_active = governor_decision(
            trailing_drawdown=dd, regime_label=regime_label, was_dd_cut=was_dd_cut, cfg=decision_cfg,
        )
    if prior_state_status == "invalid_assume_cut":
        reasons.append("prior_directive_unsealed:conservative_cut_hold")
    elif prior_state_status == "stale_marker_assume_cut":
        reasons.append("prior_directive_stale_marker:conservative_cut_hold")

    # Bounds are enforced BEFORE any artifact exists so a bad directive can never be published.
    if not (math.isfinite(multiplier) and 0.0 <= multiplier <= 1.0):
        LOGGER.error("Governor multiplier is out of bounds; refusing to write a directive: %r", multiplier)
        return 1

    directive = {
        "stage": "stage12_risk_governor",
        "run_as_of": run_as_of,
        "generated_at": utc_now(),
        "shadow_only": True,
        "applied": False,
        "gross_exposure_multiplier": multiplier,
        "reasons": reasons,
        "dd_cut_active": dd_cut_active,
        "book_state": "all_cash" if all_cash_book else "risky",
        "trailing_drawdown": None if dd is None else round(dd, 6),
        "drawdown_window_trading_days": window,
        "drawdown_min_complete_fraction": min_complete,
        "regime": regime_label,
        "regime_source": regime_source,
        "regime_table": regime_table,
        "regime_model_version": regime_model_version or "",
        "regime_as_of": regime_state["regime_as_of"],
        "regime_staleness_days": regime_state["staleness_days"],
        "regime_freshness_tolerance_days": regime_state["tolerance_days"],
        "regime_freshness_status": regime_state["freshness_status"],
        "regime_validation_status": regime_state["validation_status"],
        "regime_validation_errors": regime_state["validation_errors"],
        "book_source": weights_path.name,
        "held_names": len(weights),
        "prior_directive": prior[-1].name if prior else "",
        "prior_state_status": prior_state_status,
    }
    write_manifest(out_path, directive)

    # Checks record only verifications whose outcome can genuinely vary in a written manifest;
    # verifications that abort the run before any artifact exists are listed as hard gates.
    checks = [
        {"check": "regime_validation", "status": regime_state["validation_status"],
         "detail": ";".join(regime_state["validation_errors"]) or f"regime={regime_label or 'skipped'}"},
        {"check": "regime_freshness", "status": regime_state["freshness_status"],
         "detail": (f"regime_as_of={regime_state['regime_as_of'] or 'missing'} "
                    f"staleness_days={regime_state['staleness_days']} "
                    f"tolerance_days={regime_state['tolerance_days']}")},
        {"check": "prior_directive_state",
         "status": "PASS" if prior_state_status in {"none", "sealed"} else "CONSERVATIVE_ASSUME_CUT",
         "detail": f"prior={prior[-1].name if prior else ''} state={prior_state_status}"},
    ]
    acceptance = (
        "PASS_CONSERVATIVE"
        if any(c["status"] in _CONSERVATIVE_CHECK_STATUSES for c in checks)
        else "PASS"
    )
    hard_gates = ["as_of_canonical_iso", "inputs_sealed_current", "book_well_formed"]
    if not all_cash_book:
        hard_gates.extend([
            "drawdown_coverage",
            "macro_regime_not_future_dated",
            "macro_serving_content_stable_during_read",
        ])
    hard_gates.append("multiplier_bounded_before_write")
    inputs_sha256 = {
        "book": sha256_file(weights_path),
        "book_manifest": sha256_file(weights_manifest_path),
        "returns_panel.csv": sha256_file(returns_path),
        "risk_manifest.json": sha256_file(risk_manifest_path),
        **macro_input_hashes,
    }
    if prior_path is not None and prior_manifest_path is not None and prior_state_status == "sealed":
        inputs_sha256["prior_directive"] = sha256_file(prior_path)
        inputs_sha256["prior_governor_manifest"] = sha256_file(prior_manifest_path)
    new_directive_sha = sha256_file(out_path)
    write_manifest(manifest_path, {
        "stage": "stage12_risk_governor_validation",
        "run_as_of": run_as_of,
        "generated_at": utc_now(),
        "acceptance": acceptance,
        "checks": checks,
        # Verifications that abort the run BEFORE any artifact is written (fail-closed); they are
        # deliberately not "checks" because a written manifest can never carry them as FAIL.
        "hard_gates_verified_before_write": hard_gates,
        "inputs_sha256": inputs_sha256,
        "files": {
            "gross_exposure_directive.json": {"sha256": new_directive_sha},
        },
    })

    # Rewriting this directive strands any later run that pinned the previous sha: flag them
    # non-destructively so they are regenerated instead of silently trusted.
    stale_runs = mark_stale_dependent_runs(runs_root, run_as_of, new_directive_sha)
    if stale_runs:
        LOGGER.error(
            "Rewritten %s directive invalidated %d later governor run(s) whose sealed prior-directive "
            "pin no longer matches: %s. Wrote governor/%s markers; re-run those dates with --force.",
            run_as_of, len(stale_runs), stale_runs, STALE_MARKER_NAME,
        )
    own_marker = run_dir / "governor" / STALE_MARKER_NAME
    if own_marker.exists():
        own_marker.unlink()
        LOGGER.info("Cleared %s after regenerating the directive against current inputs", own_marker)

    LOGGER.info(
        "GOVERNOR %s: multiplier=%.2f dd=%s regime=%s (source=%s as_of=%s stale=%s) reasons=%s "
        "acceptance=%s (SHADOW, not applied) -> %s",
        run_as_of, multiplier, "n/a" if dd is None else f"{dd:.4f}", regime_label or "?",
        regime_source, regime_state["regime_as_of"] or "?", regime_state["staleness_days"],
        reasons, acceptance, out_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

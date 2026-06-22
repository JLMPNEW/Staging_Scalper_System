#!/usr/bin/env python3
"""Stage 5 - validate rotation signals and seal a provenance-hashed rotation_manifest.json.

Stage 5 PASSES on {valid, bounded, deterministic, non-destructive} - the ablation (19) is a
WARN-only diagnostic and promotion waits for Stage 7/Stage 11. Hard gates only here.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.artifacts import invalidate_rotation_outputs_after_validation  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.risk.liquidity import finite_float  # noqa: E402
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402
from portfolio_layer.rotation.rotation_book import aggregate_by_pipeline, apply_rotation_tilt  # noqa: E402
from portfolio_layer.rotation.foreign_market_evaluator import build_foreign_rotation  # noqa: E402
from portfolio_layer.rotation.sector_rotation_selector import build_sector_rotation  # noqa: E402


LOGGER = logging.getLogger("validate_rotation_signals")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SECTOR_OPT_COLS = {"SectorName", "Ticker", "ScorePct", "State"}
FOREIGN_OPT_COLS = {"Ticker", "MarketName", "Score", "ScorePct", "State"}
SECTOR_STATES = {"Positive", "Neutral", "Negative"}
FOREIGN_STATES = {"Eligible", "Avoid"}
SIGNAL_SOURCE_FILES = [
    "rotation_timeseries.py",
    "sector_rotation_selector.py",
    "foreign_market_evaluator.py",
    "rotation_book.py",
    "17_build_rotation_signals.py",
]
SOURCE_FILES = [
    *SIGNAL_SOURCE_FILES,
    "18_validate_rotation_signals.py",
    "19_run_rotation_ablation_replay.py",
]


def _safe_float(value, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Validate Stage 5 rotation signals.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, default=None)
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def main() -> int:  # noqa: C901
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    runs_root = paths.output_dir / "runs"
    run_as_of = args.as_of or latest_run_with(runs_root, "manifest.json")
    if not run_as_of:
        LOGGER.error("No run found under %s", runs_root)
        return 1
    run_dir = runs_root / run_as_of
    risk_dir = run_dir / "risk"
    rotation_dir = run_dir / "rotation"
    opt_dir = run_dir / "optimizer"
    costs_dir = run_dir / "costs"
    sector_path = rotation_dir / "sector_rotation.csv"
    sector_opt_path = rotation_dir / "sector_rotation_optimizer.csv"
    foreign_path = rotation_dir / "foreign_etfs.csv"
    foreign_opt_path = rotation_dir / "foreign_etfs_optimizer.csv"
    meta_path = rotation_dir / "rotation_signals_meta.json"
    risk_manifest_path = risk_dir / "risk_manifest.json"
    prices_path = risk_dir / "prices_adjclose.csv"
    returns_path = risk_dir / "returns_panel.csv"
    risk_coverage_path = risk_dir / "risk_coverage.csv"
    for required in (
        sector_path, sector_opt_path, foreign_path, foreign_opt_path, meta_path,
        risk_manifest_path, prices_path, returns_path, risk_coverage_path,
    ):
        if not required.exists():
            LOGGER.error("Run 17 / Stage 2 first; missing %s", required)
            return 1
    validation_path = rotation_dir / "validation" / "rotation_validation.csv"
    manifest_path = rotation_dir / "rotation_manifest.json"
    if args.force:
        invalidate_rotation_outputs_after_validation(rotation_dir)
        for p in (validation_path, manifest_path):
            if p.exists():
                p.unlink()
    try:
        fail_if_exists([validation_path, manifest_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    sector = read_csv(sector_path)
    sector_opt = read_csv(sector_opt_path)
    foreign = read_csv(foreign_path)
    foreign_opt = read_csv(foreign_opt_path)
    risk_coverage = read_csv(risk_coverage_path)
    prices = pd.read_csv(prices_path, index_col=0)
    prices.columns = [str(c).strip().upper() for c in prices.columns]
    params = meta.get("params", {})
    mult_min = float(params.get("tilt", {}).get("mult_min", 0.7))
    mult_max = float(params.get("tilt", {}).get("mult_max", 1.3))
    max_shift = float(params.get("tilt", {}).get("max_sector_budget_shift", 0.30))
    ma_days = int(params.get("ma_days", 200))
    slope_lookback = int(params.get("slope_lookback_days", 21))

    checks: list[dict] = []

    def rec(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    # 1. Independence: no PROD reference in the rotation logic/scripts (this validator is excluded - it
    #    necessarily contains the search token; Stage 0's AST gate is the authoritative full-package check).
    prod_token = "PROD_" + "Scalper_System"
    prod_hits = []
    for name in SOURCE_FILES + ["17_build_rotation_signals.py", "19_run_rotation_ablation_replay.py"]:
        fp = PACKAGE_ROOT / "rotation" / name
        if fp.exists() and prod_token in fp.read_text(encoding="utf-8"):
            prod_hits.append(name)
    rec("independence_no_prod_ref", "PASS" if not prod_hits else "FAIL",
        "no PROD path reference in rotation logic/scripts" if not prod_hits else f"{prod_hits}")

    # 2. PIT / no-lookahead: panel right edge <= as_of and matches the sealed meta.
    panel_end = str(prices.index[-1]) if not prices.empty else ""
    pit_ok = bool(panel_end) and panel_end <= run_as_of and str(meta.get("panel_end")) == panel_end
    rec("pit_no_lookahead", "PASS" if pit_ok else "FAIL",
        f"panel_end={panel_end} <= as_of={run_as_of}, meta_panel_end={meta.get('panel_end')}")

    # 2b. Stage 2 panel is sealed and current: risk manifest PASS + panel hashes match.
    stage2_bad = []
    try:
        risk_manifest = json.loads(risk_manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        risk_manifest = {}
        stage2_bad.append(f"risk_manifest_unreadable:{type(exc).__name__}")
    if risk_manifest.get("acceptance") != "PASS":
        stage2_bad.append(f"risk_manifest_acceptance={risk_manifest.get('acceptance')}")
    risk_files = risk_manifest.get("files") or {}
    for filename, path in {
        "prices_adjclose.csv": prices_path,
        "returns_panel.csv": returns_path,
        "risk_coverage.csv": risk_coverage_path,
    }.items():
        expected = (risk_files.get(filename) or {}).get("sha256")
        actual = sha256_file(path)
        if expected != actual:
            stage2_bad.append(f"{filename}:manifest_hash_mismatch")
    rec("stage2_panel_sealed", "PASS" if not stage2_bad else "FAIL",
        "risk_manifest PASS and panel/coverage hashes match" if not stage2_bad else f"{stage2_bad[:8]}")

    # 2c. Signal meta still describes the exact config/input/source files on disk.
    meta_bad = []
    meta_inputs = meta.get("inputs_sha256") or {}
    for filename, path in {
        "prices_adjclose.csv": prices_path,
        "returns_panel.csv": returns_path,
        "config.yaml": config_path,
    }.items():
        if meta_inputs.get(filename) != sha256_file(path):
            meta_bad.append(f"{filename}:meta_hash_mismatch")
    meta_sources = meta.get("source_sha256") or {}
    for name in SIGNAL_SOURCE_FILES:
        path = PACKAGE_ROOT / "rotation" / name
        if not path.exists():
            meta_bad.append(f"{name}:missing_source")
            continue
        if meta_sources.get(name) != sha256_file(path):
            meta_bad.append(f"{name}:meta_source_hash_mismatch")
    rec("rotation_meta_inputs_current", "PASS" if not meta_bad else "FAIL",
        "meta input/source hashes match current files" if not meta_bad else f"{meta_bad[:8]}")

    # 3. Optimizer-contract schema: exact columns, State enum, ScorePct/Score numeric domains.
    schema_bad = []
    if set(sector_opt[0].keys()) != SECTOR_OPT_COLS if sector_opt else True:
        schema_bad.append(f"sector_opt_cols={sorted(sector_opt[0].keys()) if sector_opt else None}")
    if set(foreign_opt[0].keys()) != FOREIGN_OPT_COLS if foreign_opt else True:
        schema_bad.append(f"foreign_opt_cols={sorted(foreign_opt[0].keys()) if foreign_opt else None}")
    for r in sector_opt:
        if str(r.get("State")) not in SECTOR_STATES:
            schema_bad.append(f"sector_state={r.get('SectorName')}:{r.get('State')}")
        try:
            sp = finite_float(r.get("ScorePct"), name="ScorePct")
            if not (0.0 <= sp <= 100.0):
                schema_bad.append(f"sector_scorepct_range={r.get('SectorName')}:{sp}")
        except ValueError as exc:
            schema_bad.append(str(exc))
    for r in foreign_opt:
        if str(r.get("State")) not in FOREIGN_STATES:
            schema_bad.append(f"foreign_state={r.get('Ticker')}:{r.get('State')}")
        try:
            finite_float(r.get("Score"), name="Score")
            sp = finite_float(r.get("ScorePct"), name="ScorePct")
            if not (0.0 <= sp <= 100.0):
                schema_bad.append(f"foreign_scorepct_range={r.get('Ticker')}:{sp}")
        except ValueError as exc:
            schema_bad.append(str(exc))
    rec("optimizer_contract_schema", "PASS" if not schema_bad else "FAIL",
        "exact columns + State enum + ScorePct/Score domains" if not schema_bad else f"{schema_bad[:8]}")

    # 4. SectorName domain == source_pipeline set in stocks_scores.csv, bijective with sector_etf_map.
    pipelines = {str(r.get("source_pipeline", "")).strip() for r in read_csv(run_dir / "stocks_scores.csv")
                 if str(r.get("source_pipeline", "")).strip()}
    etf_map = {str(k).strip(): str(v).strip().upper() for k, v in (meta.get("sector_etf_map") or {}).items()}
    foreign_market_map = {
        str(k).strip().upper(): str(v).strip()
        for k, v in (meta.get("foreign_market_map") or {}).items()
    }
    sectornames = [str(r.get("SectorName", "")).strip() for r in sector_opt]
    dup = sorted({s for s in sectornames if sectornames.count(s) > 1})
    map_bad = []
    if set(sectornames) != set(etf_map):
        map_bad.append(f"sectorname_set!=etf_map_keys diff={sorted(set(sectornames) ^ set(etf_map))}")
    missing_from_scores = sorted(set(etf_map) - pipelines)
    if missing_from_scores:
        map_bad.append(f"pipelines_not_in_scores={missing_from_scores}")
    if dup:
        map_bad.append(f"duplicate_sectorname={dup}")
    if len(set(etf_map.values())) != len(etf_map):
        map_bad.append("non_unique_etf_mapping")
    rec("sectorname_mapping_bijective", "PASS" if not map_bad else "FAIL",
        f"{len(etf_map)} sleeves map 1:1 to ETFs and exist in stocks_scores" if not map_bad else f"{map_bad}")

    # 5. ETF coverage: every rotation ETF present in the Stage 2 panel with enough history, not stale.
    need_hist = ma_days + slope_lookback
    cov_bad = []
    coverage_by_ticker = {str(r.get("ticker", "")).strip().upper(): r for r in risk_coverage}
    rot_etfs = set(etf_map.values()) | {str(t).strip().upper() for t in params.get("rank_universe_etfs", [])} \
        | {str(r.get("Ticker", "")).strip().upper() for r in foreign_opt}
    for etf in sorted(rot_etfs):
        if etf not in prices.columns:
            cov_bad.append(f"{etf}:absent")
            continue
        col = prices[etf].dropna()
        if len(col) < need_hist:
            cov_bad.append(f"{etf}:hist={len(col)}<{need_hist}")
        elif str(col.index[-1]) != panel_end:
            cov_bad.append(f"{etf}:stale_last={col.index[-1]}")
        cov = coverage_by_ticker.get(etf)
        if cov is None:
            cov_bad.append(f"{etf}:missing_risk_coverage")
        elif (
            str(cov.get("role", "")).strip() != "market_instrument"
            or str(cov.get("risk_status", "")).strip() != "direct"
            or str(cov.get("risk_eligible", "")).strip() != "1"
            or int(_safe_float(cov.get("right_edge_missing_day_count"), 999999.0)) != 0
        ):
            cov_bad.append(
                f"{etf}:coverage role={cov.get('role')} status={cov.get('risk_status')} "
                f"eligible={cov.get('risk_eligible')} right_edge={cov.get('right_edge_missing_day_count')}"
            )
    rec("etf_panel_coverage", "PASS" if not cov_bad else "FAIL",
        f"{len(rot_etfs)} rotation ETFs direct + full history per Stage 2 coverage"
        if not cov_bad else f"{cov_bad[:8]}")

    # 6. Bounded tilt: multiplier in [mult_min, mult_max]; trend-gate-fail capped at 1.0; budget shift bounded.
    tilt_bad = []
    for r in sector:
        m = finite_float(r.get("rotation_multiplier"), name="rotation_multiplier")
        if not (mult_min - 1e-9 <= m <= mult_max + 1e-9):
            tilt_bad.append(f"{r.get('source_pipeline')}:mult={m}")
        if str(r.get("trend_gate")) == "fail" and m > 1.0 + 1e-9:
            tilt_bad.append(f"{r.get('source_pipeline')}:downtrend_mult>{1.0}")
    target_path = opt_dir / "target_weights.csv"
    scores_path = run_dir / "stocks_scores.csv"
    gross = float(cfg_get(config, "optimizer.gross_exposure", 1.0))
    max_weight = float(cfg_get(config, "optimizer.max_weight_per_name", 0.05))
    if not target_path.exists() or not scores_path.exists():
        tilt_bad.append("missing_target_weights_or_scores_for_actual_tilt_check")
    else:
        base_weights = {
            str(r.get("ticker", "")).strip().upper(): finite_float(r.get("weight"), name="target_weight")
            for r in read_csv(target_path)
            if _safe_float(r.get("weight")) > 0.0
        }
        pipe_by_ticker = {
            str(r.get("ticker", "")).strip().upper(): str(r.get("source_pipeline", "")).strip()
            for r in read_csv(scores_path)
            if str(r.get("ticker", "")).strip()
        }
        mult_by_pipe = {
            str(r.get("source_pipeline", "")).strip(): finite_float(r.get("rotation_multiplier"), name="rotation_multiplier")
            for r in sector
        }
        missing_pipe = sorted(t for t in base_weights if not pipe_by_ticker.get(t))
        if missing_pipe:
            tilt_bad.append(f"target_tickers_missing_source_pipeline={missing_pipe[:8]}")
        try:
            tilted_weights = apply_rotation_tilt(
                base_weights, pipe_by_ticker, mult_by_pipe, gross=gross, max_weight=max_weight,
            )
        except ValueError as exc:
            tilted_weights = {}
            tilt_bad.append(f"actual_tilt_projection_failed:{exc}")
        if tilted_weights:
            total = sum(tilted_weights.values())
            if abs(total - gross) > 1e-8:
                tilt_bad.append(f"tilted_gross={total:.12f}!={gross}")
            if any(w < -1e-12 for w in tilted_weights.values()):
                tilt_bad.append("tilted_book_has_short")
            max_actual = max(tilted_weights.values())
            if max_actual > max_weight + 1e-8:
                tilt_bad.append(f"tilted_max_weight={max_actual:.10f}>{max_weight}")
            base_by_pipe = aggregate_by_pipeline(base_weights, pipe_by_ticker)
            tilted_by_pipe = aggregate_by_pipeline(tilted_weights, pipe_by_ticker)
            shift_bad = [
                f"{pipe}:{tilted_by_pipe.get(pipe, 0.0) - base_by_pipe.get(pipe, 0.0):.6f}"
                for pipe in sorted(set(base_by_pipe) | set(tilted_by_pipe))
                if abs(tilted_by_pipe.get(pipe, 0.0) - base_by_pipe.get(pipe, 0.0)) > max_shift + 1e-8
            ]
            if shift_bad:
                tilt_bad.append(f"sector_budget_shift_exceeds_{max_shift}={shift_bad[:8]}")
    rec("bounded_tilt", "PASS" if not tilt_bad else "FAIL",
        f"multiplier in [{mult_min},{mult_max}], actual tilted book long-only/gross/capped/shift-bounded"
        if not tilt_bad else f"{tilt_bad[:8]}")

    # 7. Canonical <-> optimizer consistency + State derivation correctness.
    consist_bad = []
    canon_by_pipe = {str(r.get("source_pipeline")): r for r in sector}
    for r in sector_opt:
        c = canon_by_pipe.get(str(r.get("SectorName")))
        if not c:
            consist_bad.append(f"opt_sector_missing_canon={r.get('SectorName')}")
            continue
        if abs(finite_float(r.get("ScorePct"), name="ScorePct") - finite_float(c.get("score_pct"), name="score_pct")) > 1e-6:
            consist_bad.append(f"{r.get('SectorName')}:scorepct_mismatch")
        if str(r.get("State")) != str(c.get("state")):
            consist_bad.append(f"{r.get('SectorName')}:state_mismatch")
        if str(c.get("trend_state")) == "down" and str(c.get("state")) != "Negative":
            consist_bad.append(f"{r.get('SectorName')}:downtrend_not_negative")
    rec("canonical_optimizer_consistent", "PASS" if not consist_bad else "FAIL",
        "optimizer rows mirror canonical + State derivation correct" if not consist_bad else f"{consist_bad[:8]}")

    # 8. Foreign applied budget == 0 (locked until Stage 6).
    fb = float(params.get("foreign", {}).get("applied_budget", 0.0))
    rec("foreign_budget_zero", "PASS" if fb == 0.0 else "FAIL", f"applied_budget={fb}")

    # 9. Determinism: rebuild from the same sealed panel + config; outputs must be identical.
    returns = pd.read_csv(returns_path, index_col=0)
    returns.columns = [str(c).strip().upper() for c in returns.columns]
    rebuilt_sec = build_sector_rotation(
        prices, returns, sector_etf_map=etf_map,
        rank_universe=[str(t).strip().upper() for t in params.get("rank_universe_etfs", [])],
        windows=[int(w) for w in params.get("momentum_windows_days", [])],
        weights=[float(w) for w in params.get("momentum_weights", [])],
        ma_days=ma_days, slope_lookback=slope_lookback,
        positive_score_pct=float(params.get("positive_score_pct", 60.0)),
        negative_score_pct=float(params.get("negative_score_pct", 40.0)),
        mult_min=mult_min, mult_max=mult_max,
    )
    rebuilt_foreign = build_foreign_rotation(
        prices, returns, market_map=foreign_market_map,
        windows=[int(w) for w in params.get("momentum_windows_days", [])],
        weights=[float(w) for w in params.get("momentum_weights", [])],
        ma_days=ma_days, slope_lookback=slope_lookback,
        eligible_score_pct=float(params.get("foreign", {}).get("eligible_score_pct", 55.0)),
    )
    det_bad = []
    if len(rebuilt_sec) != len(sector):
        det_bad.append("sector_row_count")
    for rb, r in zip(rebuilt_sec, sector):
        if str(rb["source_pipeline"]) != str(r.get("source_pipeline")) \
                or abs(rb["score_pct"] - finite_float(r.get("score_pct"), name="score_pct")) > 1e-6 \
                or str(rb["state"]) != str(r.get("state")) \
                or abs(rb["rotation_multiplier"] - finite_float(r.get("rotation_multiplier"), name="mult")) > 1e-6:
            det_bad.append(f"sector:{rb['source_pipeline']}")
    if len(rebuilt_foreign) != len(foreign):
        det_bad.append("foreign_row_count")
    for rb, r in zip(rebuilt_foreign, foreign):
        if str(rb["ticker"]) != str(r.get("ticker")) \
                or abs(rb["score"] - finite_float(r.get("score"), name="foreign_score")) > 1e-6 \
                or abs(rb["score_pct"] - finite_float(r.get("score_pct"), name="foreign_score_pct")) > 1e-6 \
                or str(rb["state"]) != str(r.get("state")):
            det_bad.append(f"foreign:{rb['ticker']}")
    rec("deterministic_rebuild", "PASS" if not det_bad else "FAIL",
        "sector + foreign rebuilds match sealed signals" if not det_bad else f"mismatch={det_bad[:8]}")

    # 10. Shadow-only / non-destructive: Stage 3 book + Stage 4 adjusted book unchanged (match their seals);
    #     production not enabled.
    shadow_bad = []
    if bool(meta.get("enabled_in_production", False)) or bool(cfg_get(config, "rotation.enabled_in_production", False)):
        shadow_bad.append("enabled_in_production=true")
    opt_manifest_path = opt_dir / "optimizer_manifest.json"
    target_path = opt_dir / "target_weights.csv"
    if not opt_manifest_path.exists() or not target_path.exists():
        shadow_bad.append("missing_stage3_manifest_or_target_weights")
    else:
        om = json.loads(opt_manifest_path.read_text(encoding="utf-8"))
        sealed = (om.get("provenance_sha256") or {}).get("target_weights.csv")
        if om.get("acceptance") != "PASS" or sha256_file(target_path) != sealed:
            shadow_bad.append("stage3_target_weights_changed")
    cost_manifest_path = costs_dir / "cost_manifest.json"
    adj_path = costs_dir / "cost_adjusted_target_weights.csv"
    if not cost_manifest_path.exists() or not adj_path.exists():
        shadow_bad.append("missing_stage4_manifest_or_adjusted_book")
    else:
        cm = json.loads(cost_manifest_path.read_text(encoding="utf-8"))
        sealed = (cm.get("provenance_sha256") or {}).get("cost_adjusted_target_weights.csv")
        if cm.get("acceptance") != "PASS" or sha256_file(adj_path) != sealed:
            shadow_bad.append("stage4_adjusted_book_changed")
    rec("shadow_only_non_destructive", "PASS" if not shadow_bad else "FAIL",
        "production disabled; Stage 3/4 books intact" if not shadow_bad else f"{shadow_bad}")

    # 11. Signal artifacts reproducible (meta file hashes match disk).
    repro_bad = [fn for fn, info in (meta.get("files") or {}).items()
                 if not (rotation_dir / fn).exists() or sha256_file(rotation_dir / fn) != info.get("sha256")]
    rec("rotation_artifacts_reproducible", "PASS" if not repro_bad else "FAIL",
        "meta file hashes match disk" if not repro_bad else f"{repro_bad}")

    validation_path.parent.mkdir(parents=True, exist_ok=True)
    write_csv(validation_path, ["check", "status", "detail"], checks)
    hard = [c for c in checks if c["status"] != "WARN"]
    passed = all(c["status"] == "PASS" for c in hard)

    provenance = {
        "sector_rotation.csv": sector_path, "sector_rotation_optimizer.csv": sector_opt_path,
        "foreign_etfs.csv": foreign_path, "foreign_etfs_optimizer.csv": foreign_opt_path,
        "rotation_signals_meta.json": meta_path, "risk_manifest.json": risk_manifest_path,
        "risk_coverage.csv": risk_coverage_path, "prices_adjclose.csv": prices_path,
        "returns_panel.csv": returns_path, "validation/rotation_validation.csv": validation_path,
        "optimizer_manifest.json": opt_dir / "optimizer_manifest.json",
        "target_weights.csv": opt_dir / "target_weights.csv",
        "cost_manifest.json": costs_dir / "cost_manifest.json",
        "cost_adjusted_target_weights.csv": costs_dir / "cost_adjusted_target_weights.csv",
        "config.yaml": config_path,
    }
    for name in SOURCE_FILES:
        source_path = PACKAGE_ROOT / "rotation" / name
        if source_path.exists():
            provenance[f"source/{name}"] = source_path
    manifest = {
        "run_as_of": run_as_of, "stage": "stage5_rotation_sleeve",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "acceptance": "PASS" if passed else "FAIL",
        "shadow_only": True,
        "enabled_in_production": bool(cfg_get(config, "rotation.enabled_in_production", False)),
        "rotation_signals_meta_sha256": sha256_file(meta_path),
        "provenance_sha256": {n: sha256_file(p) for n, p in provenance.items() if p.exists()},
        "checks": checks,
    }
    write_manifest(manifest_path, manifest)

    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    if passed:
        LOGGER.info("STAGE 5 ACCEPTANCE: PASS (as_of=%s) -> %s", run_as_of, manifest_path)
        return 0
    LOGGER.error("STAGE 5 ACCEPTANCE: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

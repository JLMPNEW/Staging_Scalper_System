#!/usr/bin/env python3
"""Stage 8 - assign the sealed Stage 7 book into horizon sleeves and compute the risk model (SHADOW-ONLY).

Phase 1: long_core + medium_rotation only. short_catalyst stays disabled until a formal
events/catalyst_events.csv contract exists (absent => WARN + disable, never final_score-faked).
This step does NOT re-budget weights; it seals the assignments + risk diagnostics for 28.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402
from portfolio_layer.sleeves.risk_model import (  # noqa: E402
    effective_number_of_bets,
    factor_decomposition,
    information_ratios,
    risk_contributions,
)


LOGGER = logging.getLogger("build_sleeve_framework")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SOURCE_FILES = ["risk_model.py", "27_build_sleeve_framework.py"]
ASSIGN_FIELDS = ["ticker", "source_pipeline", "sleeve", "reason", "weight", "final_score",
                 "score_confidence", "rating", "sector_name", "rotation_state", "sigma_annual",
                 "information_ratio", "risk_contribution"]


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build Stage 8 sleeve framework + risk model (shadow-only).")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, default=None)
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def _f(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_iso_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


def main() -> int:  # noqa: C901
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    runs_root = paths.output_dir / "runs"
    run_as_of = args.as_of or latest_run_with(runs_root, "blacklitterman/bl_manifest.json")
    if not run_as_of:
        LOGGER.error("No sealed Stage 1 run found under %s", runs_root)
        return 1
    run_dir = runs_root / run_as_of
    bl_dir = run_dir / "blacklitterman"
    art = {
        "bl_manifest": bl_dir / "bl_manifest.json",
        "bl_cost_adjusted": bl_dir / "costs" / "bl_cost_adjusted_target_weights.csv",
        "bl_target_weights": bl_dir / "bl_target_weights.csv",
        "scores": run_dir / "stocks_scores.csv",
        "rotation": run_dir / "rotation" / "sector_rotation.csv",
        "macro_regime": run_dir / "macro" / "macro_regime.csv",
        "covariance": run_dir / "risk" / "covariance.csv",
        "risk_manifest": run_dir / "risk" / "risk_manifest.json",
        "config": config_path,
    }
    missing = [k for k, p in art.items() if not p.exists()]
    if missing:
        LOGGER.error("Missing Stage 7 / upstream artifacts: %s", missing)
        return 1

    sleeves_dir = run_dir / "sleeves"
    assignments_path = sleeves_dir / "sleeve_assignments.csv"
    meta_path = sleeves_dir / "risk_model_meta.json"
    validation_path = sleeves_dir / "validation" / "sleeve_framework_validation.csv"
    outputs = [assignments_path, meta_path]
    if args.force:
        for path in (*outputs, validation_path):
            if path.exists():
                path.unlink()
    try:
        fail_if_exists(outputs, force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    checks: list[dict[str, str]] = []

    def rec(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    # --- Stage 7 sealed + cost-adjusted book hash verified (not just read) ---
    bl_manifest = _load_json(art["bl_manifest"])
    seal_bad = []
    if bl_manifest.get("acceptance") != "PASS":
        seal_bad.append(f"bl_manifest_acceptance={bl_manifest.get('acceptance')}")
    sealed_cost_hash = (bl_manifest.get("provenance_sha256") or {}).get("costs/bl_cost_adjusted_target_weights.csv")
    if sealed_cost_hash != sha256_file(art["bl_cost_adjusted"]):
        seal_bad.append("bl_cost_adjusted_hash_mismatch")
    rec("stage7_sealed_and_current", "PASS" if not seal_bad else "FAIL",
        "Stage 7 manifest PASS and cost-adjusted book hash matches" if not seal_bad else f"{seal_bad}")

    # --- covariance hash == sealed Stage 2 ---
    risk_manifest = _load_json(art["risk_manifest"])
    cov_sealed = ((risk_manifest.get("files") or {}).get("covariance.csv") or {}).get("sha256")
    cov_ok = cov_sealed == sha256_file(art["covariance"])
    rec("covariance_sealed_stage2", "PASS" if cov_ok else "FAIL",
        "covariance.csv hash matches sealed Stage 2" if cov_ok else "covariance_hash_mismatch")

    # --- load book + metadata ---
    scores = {str(r.get("ticker", "")).strip().upper(): r for r in read_csv(art["scores"])}
    sector_by_ticker = {str(r.get("Ticker", "")).strip().upper(): str(r.get("SectorName", "")).strip()
                        for r in read_csv(art["bl_target_weights"])}
    rotation_state = {str(r.get("source_pipeline", "")).strip(): str(r.get("state", "")).strip()
                      for r in read_csv(art["rotation"])}
    regime_rows = read_csv(art["macro_regime"])
    regime_label = str(regime_rows[0].get("active_current_regime", "")).strip() if regime_rows else ""

    held: dict[str, float] = {}
    cash_weight = 0.0
    for row in read_csv(art["bl_cost_adjusted"]):
        ticker = str(row.get("ticker") or row.get("Ticker") or "").strip().upper()
        weight = _f(row.get("weight") or row.get("Weight"))
        if not ticker or weight is None:
            continue
        if ticker == "CASH":
            cash_weight += max(0.0, weight)
        elif weight > 0:
            held[ticker] = weight

    # --- catalyst event contract (Phase 1: absent => disable + WARN) ---
    events_rel = str(cfg_get(config, "sleeves.catalyst_events_csv", "events/catalyst_events.csv"))
    events_path = run_dir / events_rel
    catalyst_enabled = bool(cfg_get(config, "sleeves.sleeve_defs.short_catalyst.enabled", False))
    catalyst_tickers: set[str] = set()
    run_date = _parse_iso_date(run_as_of)
    if catalyst_enabled and events_path.exists():
        event_rows = read_csv(events_path)
        required_event_fields = {
            "ticker", "event_type", "event_date", "event_asof_date", "source_pipeline",
            "confidence", "source_artifact", "source_sha256",
        }
        header = set(event_rows[0].keys()) if event_rows else set()
        missing_event_fields = sorted(required_event_fields - header)
        if missing_event_fields:
            rec("short_catalyst_contract", "FAIL", f"missing event fields: {missing_event_fields}")
        horizons = cfg_get(config, "sleeves.sleeve_defs.short_catalyst.horizon_months", [1, 3]) or [1, 3]
        max_days = int(max(float(v) for v in horizons) * 31) if horizons else 93
        for row in read_csv(events_path):
            ev_asof = _parse_iso_date(row.get("event_asof_date"))
            ev_date = _parse_iso_date(row.get("event_date"))
            tkr = str(row.get("ticker", "")).strip().upper()
            if (
                tkr
                and run_date is not None
                and ev_asof is not None
                and ev_date is not None
                and ev_asof <= run_date
                and run_date <= ev_date <= run_date + timedelta(days=max_days)
            ):
                catalyst_tickers.add(tkr)
        if not missing_event_fields:
            rec("short_catalyst_contract", "PASS", f"catalyst events active: {len(catalyst_tickers)} PIT events")
    else:
        rec("short_catalyst_contract", "WARN",
            "short_catalyst disabled (no events/catalyst_events.csv contract); Phase 1 long_core+medium_rotation only")

    # --- sleeve assignment (exactly one per held name) ---
    positive_pipes = {p for p, s in rotation_state.items() if s == "Positive"}
    assign_rows = []
    sleeve_of: dict[str, str] = {}
    for ticker in sorted(held):
        srow = scores.get(ticker, {})
        pipe = str(srow.get("source_pipeline", "")).strip()
        if ticker in catalyst_tickers:
            sleeve, reason = "short_catalyst", "pit_catalyst_event"
        elif pipe in positive_pipes:
            sleeve, reason = "medium_rotation", f"rotation_state_positive:{pipe}"
        else:
            sleeve, reason = "long_core", "final_score_driver"
        sleeve_of[ticker] = sleeve
        assign_rows.append({
            "ticker": ticker, "source_pipeline": pipe, "sleeve": sleeve, "reason": reason,
            "weight": round(held[ticker], 10),
            "final_score": round(_f(srow.get("final_score")) or 0.0, 10),
            "score_confidence": round(_f(srow.get("score_confidence")) or 0.0, 8),
            "rating": str(srow.get("rating", "")).strip(),
            "sector_name": sector_by_ticker.get(ticker, pipe),
            "rotation_state": rotation_state.get(pipe, ""),
        })

    partition_ok = len(sleeve_of) == len(held) and all(t in sleeve_of for t in held)
    rec("partition_complete_disjoint", "PASS" if partition_ok else "FAIL",
        f"{len(held)} held names -> exactly one sleeve each" if partition_ok else "partition_incomplete")

    # --- risk model on the held (non-CASH) book ---
    cov = pd.read_csv(art["covariance"], index_col=0)
    cov.index = [str(i).strip().upper() for i in cov.index]
    cov.columns = [str(c).strip().upper() for c in cov.columns]
    market_etf = str(cfg_get(config, "sleeves.market_factor_etf", "SPY")).strip().upper()
    sector_etfs = {k: str(v).strip().upper() for k, v in (cfg_get(config, "sleeves.sector_factor_etfs", {}) or {}).items()}
    required_factors = sorted({market_etf, *sector_etfs.values()} - {""})
    missing_factors = sorted(t for t in required_factors if t not in cov.index or t not in cov.columns)
    rec("factor_universe_present", "PASS" if not missing_factors else "FAIL",
        f"all configured factor ETFs present: {required_factors}" if not missing_factors else f"missing={missing_factors}")
    not_in_cov = sorted(t for t in held if t not in cov.index)
    rec("held_universe_in_covariance", "PASS" if not not_in_cov else "FAIL",
        f"all {len(held)} held names in covariance" if not not_in_cov else f"missing={not_in_cov[:10]}")

    risk_bad = []
    rc = factor = enb = ir = None
    try:
        rc = risk_contributions(held, cov)
        factor = factor_decomposition(held, cov, market_etf=market_etf, sector_etfs=sector_etfs)
        enb = effective_number_of_bets(held, cov)
        ir = information_ratios({t: _f(scores.get(t, {}).get("final_score")) or 0.0 for t in held}, rc.sigma)
    except ValueError as exc:
        risk_bad.append(str(exc))
    rec("risk_model_computed", "PASS" if not risk_bad else "FAIL",
        "RC + factor decomposition + ENB computed" if not risk_bad else f"{risk_bad}")

    # enrich assignment rows with per-name risk stats
    if rc is not None and ir is not None:
        for row in assign_rows:
            t = row["ticker"]
            row["sigma_annual"] = round(rc.sigma.get(t, 0.0), 8)
            row["information_ratio"] = round(ir.get(t, 0.0), 8)
            row["risk_contribution"] = round(rc.rc.get(t, 0.0), 10)
    else:
        for row in assign_rows:
            row["sigma_annual"] = row["information_ratio"] = row["risk_contribution"] = ""

    # per-sleeve realized risk share + selected regime budgets
    sleeve_rc_share: dict[str, float] = {}
    if rc is not None:
        for t, share in rc.rc.items():
            sleeve_rc_share[sleeve_of.get(t, "")] = sleeve_rc_share.get(sleeve_of.get(t, ""), 0.0) + share
    risk_off_regimes = set(cfg_get(config, "sleeves.risk_off_regimes", []) or [])
    budget_key = "risk_off" if regime_label in risk_off_regimes else "default"
    sleeve_budgets = dict(cfg_get(config, f"sleeves.sleeve_risk_budgets.{budget_key}", {}) or {})

    sleeves_dir.mkdir(parents=True, exist_ok=True)
    write_csv(assignments_path, ASSIGN_FIELDS, assign_rows)

    sleeve_counts = {s: sum(1 for r in assign_rows if r["sleeve"] == s) for s in {r["sleeve"] for r in assign_rows}}
    validation_path.parent.mkdir(parents=True, exist_ok=True)
    write_csv(validation_path, ["check", "status", "detail"], checks)
    passed = all(c["status"] == "PASS" for c in checks if c["status"] != "WARN")

    meta = {
        "run_as_of": run_as_of,
        "stage": "stage8_sleeve_framework",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "shadow_only": True,
        "enabled_in_production": bool(cfg_get(config, "sleeves.enabled_in_production", False)),
        "acceptance": "PASS" if passed else "FAIL",
        "regime": {"label": regime_label, "budget_key": budget_key, "sleeve_risk_budgets": sleeve_budgets},
        "held_count": len(held),
        "cash_weight": round(cash_weight, 10),
        "sleeve_counts": sleeve_counts,
        "sleeve_realized_risk_share": {k: round(v, 6) for k, v in sleeve_rc_share.items()},
        "risk_model": {
            "annual_vol": None if rc is None else round(rc.annual_vol, 8),
            "factor": factor,
            "effective_bets": enb,
            "per_name_rc_max": None if rc is None else round(max(rc.rc.values()), 6),
        },
        "short_catalyst_enabled": catalyst_enabled and bool(catalyst_tickers),
        "input_paths": {k: str(p) for k, p in art.items()},
        "inputs_sha256": {k: sha256_file(p) for k, p in art.items()},
        "outputs_sha256": {"sleeve_assignments.csv": sha256_file(assignments_path)},
        "source_sha256": {n: sha256_file(PACKAGE_ROOT / "sleeves" / n)
                          for n in SOURCE_FILES if (PACKAGE_ROOT / "sleeves" / n).exists()},
        "checks": checks,
    }
    write_manifest(meta_path, meta)

    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    if passed:
        enb_v = (enb or {}).get("enb")
        idio = (factor or {}).get("idiosyncratic_share")
        LOGGER.info("STAGE 8 SLEEVE FRAMEWORK: PASS (as_of=%s, held=%d, sleeves=%s, ENB=%.2f, idio_share=%.3f) -> %s",
                    run_as_of, len(held), sleeve_counts, enb_v or float("nan"), idio or float("nan"), meta_path)
        return 0
    LOGGER.error("STAGE 8 SLEEVE FRAMEWORK: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

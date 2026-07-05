#!/usr/bin/env python3
"""Stage 11 - calibration feedback gate: turn APPROVED + OOS-VALIDATED slopes into proposed
Stage 1 / Stage 7 calibration updates. SHIPS INERT: with no qualifying evidence it emits zero
proposals and says so.

Evidence gate per (pipeline, horizon) cell — ALL required:
  research/68  alpha_slopes.csv        approved = 1
  research/69  oos_validation.csv      oos_validated = 1  (same pipeline + horizon)

For qualifying cells the script converts the empirical ridge slope (return per 1 z of standardized
score over the horizon) into the Stage 1 calibration vocabulary:

  slope_annualized        = fm_slope * 252 / horizon_days
  native_sigma            = mean within-(pipeline,date) std of native_score (from the 67 panel)
  expected_alpha_at_full  = slope_annualized * (100 - neutral_estimate) / native_sigma

and writes a REVIEW artifact + a proposed config snippet. It NEVER edits config.yaml: promoting
calibration into production is a human, provenance-logged step (and, per the lockbox protocol,
overlay promotion additionally requires the Stage 16 walk-forward evidence and a dated protocol
amendment). The proposal captures everything needed to make that edit auditable.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.research.stage11_common import load_lockbox, parse_finite  # noqa: E402


LOGGER = logging.getLogger("apply_calibration_feedback")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"

PROPOSAL_FIELDS = [
    "source_pipeline", "horizon_days", "fm_slope", "fm_t", "oof_rank_ic", "oos_r2_vs_zero",
    "slope_annualized", "native_sigma", "neutral_estimate", "current_expected_alpha_at_full",
    "proposed_expected_alpha_at_full", "evidence",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stage 11 calibration feedback gate (inert without evidence).")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--build", default=None, help="alpha_calibration/validation build (default: latest).")
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def _latest(root: Path, marker: str, wanted: str | None) -> Path | None:
    if wanted:
        cand = root / wanted
        return cand if (cand / marker).exists() else None
    if not root.exists():
        return None
    builds = sorted(p for p in root.iterdir() if p.is_dir() and (p / marker).exists())
    return builds[-1] if builds else None


def main() -> int:  # noqa: C901
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    try:
        lockbox = load_lockbox(config, config_path)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1

    fb = cfg_get(config, "calibration_feedback", {}) or {}
    feedback_horizon = int(fb.get("horizon_days", 252))
    slopes_dir = _latest(paths.output_dir / str(cfg_get(config, "alpha_calibration.dir", "alpha_calibration")),
                         "alpha_calibration_manifest.json", args.build)
    valid_dir = _latest(paths.output_dir / str(cfg_get(config, "calibration_validation.dir", "calibration_validation")),
                        "validation_manifest.json", args.build)
    panel_dir = _latest(paths.output_dir / str(cfg_get(config, "calibration_panel.dir", "calibration_panel")),
                        "calibration_panel_manifest.json", args.build)
    if not (slopes_dir and valid_dir and panel_dir):
        LOGGER.error("Need 68 + 69 + 67 builds first (missing: %s)",
                     [n for n, d in (("68", slopes_dir), ("69", valid_dir), ("67", panel_dir)) if d is None])
        return 1

    out_dir = paths.output_dir / str(fb.get("dir", "calibration_feedback")) / slopes_dir.name
    proposal_path = out_dir / "proposed_calibration.csv"
    snippet_path = out_dir / "proposed_config_snippet.yaml"
    manifest_path = out_dir / "feedback_manifest.json"
    if args.force:
        for p in (proposal_path, snippet_path, manifest_path):
            if p.exists():
                p.unlink()
    try:
        fail_if_exists([proposal_path, snippet_path, manifest_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    slopes = {(r["source_pipeline"], r["horizon_days"]): r
              for r in read_csv(slopes_dir / "alpha_slopes.csv")}
    validations = {(r["source_pipeline"], r["horizon_days"]): r
                   for r in read_csv(valid_dir / "oos_validation.csv")}

    # native dispersion per pipeline from the joined panel (for the z -> native-scale conversion)
    sigma_acc: dict[str, dict[str, list[float]]] = {}
    for r in read_csv(panel_dir / "calibration_panel.csv"):
        pipe = str(r.get("source_pipeline", ""))
        native = parse_finite(r.get("native_score"))
        if native is None:
            continue
        sigma_acc.setdefault(pipe, {}).setdefault(str(r.get("as_of_date", "")), []).append(native)
    native_sigma: dict[str, float] = {}
    neutral_est: dict[str, float] = {}
    for pipe, by_date in sigma_acc.items():
        stds = [float(np.std(v, ddof=1)) for v in by_date.values() if len(v) >= 8]
        medians = [float(np.median(v)) for v in by_date.values() if len(v) >= 8]
        if stds:
            native_sigma[pipe] = float(np.mean(stds))
            neutral_est[pipe] = float(np.mean(medians))

    current_alpha = {
        str(s.get("model_family")): float((s.get("calibration") or {}).get("expected_alpha_at_full", 0.15))
        for s in cfg_get(config, "score_contract.sectors", []) or []
    }

    proposals: list[dict[str, Any]] = []
    rejected: dict[str, int] = {"not_approved_68": 0, "not_validated_69": 0, "no_native_sigma": 0,
                                "wrong_horizon": 0}
    for key, s in slopes.items():
        pipe, horizon = key
        if pipe == "ALL":
            continue
        if int(horizon) != feedback_horizon:
            rejected["wrong_horizon"] += 1
            continue
        if str(s.get("approved", "0")).strip() != "1":
            rejected["not_approved_68"] += 1
            continue
        v = validations.get(key)
        if v is None or str(v.get("oos_validated", "0")).strip() != "1":
            rejected["not_validated_69"] += 1
            continue
        sigma = native_sigma.get(pipe)
        if not sigma or sigma <= 0:
            rejected["no_native_sigma"] += 1
            continue
        raw_fm_slope = s.get("fm_slope")
        if raw_fm_slope is None:
            rejected["missing_fm_slope"] += 1
            continue
        fm_slope = float(raw_fm_slope)
        slope_ann = fm_slope * 252.0 / float(horizon)
        neutral = neutral_est.get(pipe, 50.0)
        proposed = slope_ann * (100.0 - neutral) / sigma
        proposals.append({
            "source_pipeline": pipe, "horizon_days": horizon,
            "fm_slope": s.get("fm_slope"), "fm_t": s.get("fm_t"),
            "oof_rank_ic": v.get("oof_rank_ic"), "oos_r2_vs_zero": v.get("oos_r2_vs_zero"),
            "slope_annualized": round(slope_ann, 6),
            "native_sigma": round(sigma, 4), "neutral_estimate": round(neutral, 4),
            "current_expected_alpha_at_full": current_alpha.get(pipe, ""),
            "proposed_expected_alpha_at_full": round(proposed, 6),
            "evidence": f"68:{slopes_dir.name};69:{valid_dir.name}",
        })

    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(proposal_path, PROPOSAL_FIELDS, proposals)
    snippet_lines = ["# PROPOSED calibration updates — review + apply by hand with a protocol note.",
                     "# Generated by research/70; production promotion also requires backtest/16 evidence.",
                     ""]
    for p in proposals:
        snippet_lines.append(f"# {p['source_pipeline']}: expected_alpha_at_full "
                             f"{p['current_expected_alpha_at_full']} -> {p['proposed_expected_alpha_at_full']} "
                             f"(fm_t={p['fm_t']}, oof_rank_ic={p['oof_rank_ic']})")
    if not proposals:
        snippet_lines.append("# NO QUALIFYING EVIDENCE: zero cells are both 68-approved and 69-validated.")
        snippet_lines.append("# This file is intentionally empty of proposals (the gate is working).")
    snippet_path.write_text("\n".join(snippet_lines) + "\n", encoding="utf-8")

    checks = [
        {"check": "evidence_gate", "status": "PASS",
         "detail": f"proposals={len(proposals)}; rejected={rejected}"},
        {"check": "no_auto_apply", "status": "PASS",
         "detail": "config.yaml untouched; promotion is a human, protocol-logged step"},
        {"check": "lockbox_intact", "status": "PASS",
         "detail": f"protocol sha={lockbox['protocol_sha256'][:12]}"},
    ]
    write_manifest(manifest_path, {
        "stage": "stage11_calibration_feedback",
        "generated_at": utc_now(),
        "acceptance": "PASS",
        "feedback_horizon_days": feedback_horizon,
        "slopes_build": slopes_dir.name,
        "validation_build": valid_dir.name,
        "panel_build": panel_dir.name,
        "slopes_sha256": sha256_file(slopes_dir / "alpha_slopes.csv"),
        "validation_sha256": sha256_file(valid_dir / "oos_validation.csv"),
        "proposals": len(proposals),
        "rejected": rejected,
        "checks": checks,
    })
    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    LOGGER.info("CALIBRATION FEEDBACK: %d proposals -> %s", len(proposals), out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

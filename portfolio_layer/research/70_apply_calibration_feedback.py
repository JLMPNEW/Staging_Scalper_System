#!/usr/bin/env python3
"""Stage 11 - calibration feedback gate: turn APPROVED + OOS-VALIDATED slopes into proposed
Stage 1 / Stage 7 calibration updates. SHIPS INERT: with no qualifying evidence it emits zero
proposals and says so.

Evidence gate per (pipeline, horizon) cell — ALL required:
  research/68  alpha_slopes.csv        approved = 1
  research/69  oos_validation.csv      oos_validated = 1  (same pipeline + horizon)

For qualifying cells the script converts the empirical ridge slope (return per 1 z of standardized
score over the horizon) into the Stage 1 calibration vocabulary:

  slope_annualized        = slope_pooled_ridge * 252 / horizon_days
  native_sigma            = mean within-(pipeline,date) std of native_score (from the 67 panel)
  expected_alpha_at_full  = slope_annualized * configured_native_scale / native_sigma

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
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    manifest_accepts,
    read_csv,
    read_manifest,
    sha256_file,
    write_csv,
    write_manifest,
    write_text_atomic,
)
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.research.stage11_common import load_lockbox, parse_finite  # noqa: E402


LOGGER = logging.getLogger("apply_calibration_feedback")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"

PROPOSAL_FIELDS = [
    "source_pipeline", "horizon_days", "ridge_slope", "fm_t", "oof_rank_ic", "oos_r2_vs_zero",
    "slope_annualized", "native_sigma", "neutral_estimate", "calibration_scale",
    "current_expected_alpha_at_full",
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


def _common_build(
    roots: list[tuple[Path, str]], wanted: str | None,
) -> str | None:
    if wanted:
        return wanted if all((root / wanted / marker).exists() for root, marker in roots) else None
    names: set[str] | None = None
    for root, marker in roots:
        available = {p.name for p in root.iterdir() if p.is_dir() and (p / marker).exists()} if root.exists() else set()
        names = available if names is None else names & available
    return max(names) if names else None


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
    slopes_root = paths.output_dir / str(cfg_get(config, "alpha_calibration.dir", "alpha_calibration"))
    valid_root = paths.output_dir / str(cfg_get(config, "calibration_validation.dir", "calibration_validation"))
    panel_root = paths.output_dir / str(cfg_get(config, "calibration_panel.dir", "calibration_panel"))
    common_build = _common_build([
        (slopes_root, "alpha_calibration_manifest.json"),
        (valid_root, "validation_manifest.json"),
        (panel_root, "calibration_panel_manifest.json"),
    ], args.build)
    if common_build is None:
        LOGGER.error("Need a common 67/68/69 build id; pass --build only after all three stages complete")
        return 1
    slopes_dir = slopes_root / common_build
    valid_dir = valid_root / common_build
    panel_dir = panel_root / common_build

    slopes_manifest_path = slopes_dir / "alpha_calibration_manifest.json"
    validation_manifest_path = valid_dir / "validation_manifest.json"
    panel_manifest_path = panel_dir / "calibration_panel_manifest.json"
    slopes_manifest = read_manifest(slopes_manifest_path)
    validation_manifest = read_manifest(validation_manifest_path)
    panel_manifest = read_manifest(panel_manifest_path)
    lineage_errors: list[str] = []
    for label, manifest in (("68", slopes_manifest), ("69", validation_manifest), ("67", panel_manifest)):
        if not manifest_accepts(manifest, allow_deferred=False):
            lineage_errors.append(f"{label}:acceptance")
    if str(slopes_manifest.get("panel_build", "")) != common_build:
        lineage_errors.append("68:panel_build")
    if str(validation_manifest.get("panel_build", "")) != common_build:
        lineage_errors.append("69:panel_build")
    panel_manifest_sha = sha256_file(panel_manifest_path)
    if str(slopes_manifest.get("panel_manifest_sha256", "")) != panel_manifest_sha:
        lineage_errors.append("68:panel_manifest_sha")
    if str(validation_manifest.get("panel_manifest_sha256", "")) != panel_manifest_sha:
        lineage_errors.append("69:panel_manifest_sha")
    if str(validation_manifest.get("alpha_calibration_manifest_sha256", "")) != sha256_file(slopes_manifest_path):
        lineage_errors.append("69:alpha_manifest_sha")
    input_files = {
        "alpha_slopes.csv": slopes_dir / "alpha_slopes.csv",
        "oos_validation.csv": valid_dir / "oos_validation.csv",
        "calibration_panel.csv": panel_dir / "calibration_panel.csv",
    }
    for name, path in input_files.items():
        source_manifest = slopes_manifest if name == "alpha_slopes.csv" else (
            validation_manifest if name == "oos_validation.csv" else panel_manifest
        )
        expected = ((source_manifest.get("files") or {}).get(name) or {}).get("sha256")
        if not path.exists() or not expected or sha256_file(path) != expected:
            lineage_errors.append(f"{name}:hash")
    if lineage_errors:
        LOGGER.error("Calibration-feedback evidence chain is stale/mixed: %s", lineage_errors)
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

    calibration_cfg = {
        str(s.get("model_family")): dict(s.get("calibration") or {})
        for s in cfg_get(config, "score_contract.sectors", []) or []
    }
    current_alpha = {
        pipe: float(values.get("expected_alpha_at_full", 0.15)) for pipe, values in calibration_cfg.items()
    }

    proposals: list[dict[str, Any]] = []
    rejected: dict[str, int] = {
        "not_approved_68": 0,
        "not_validated_69": 0,
        "no_native_sigma": 0,
        "missing_ridge_slope": 0,
        "invalid_calibration_scale": 0,
        "wrong_horizon": 0,
    }
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
        ridge_slope = parse_finite(s.get("slope_pooled_ridge"))
        if ridge_slope is None:
            rejected["missing_ridge_slope"] += 1
            continue
        scale = parse_finite(calibration_cfg.get(pipe, {}).get("scale"))
        if scale is None or scale <= 0.0:
            rejected["invalid_calibration_scale"] += 1
            continue
        slope_ann = ridge_slope * 252.0 / float(horizon)
        neutral = neutral_est.get(pipe, 50.0)
        proposed = slope_ann * scale / sigma
        proposals.append({
            "source_pipeline": pipe, "horizon_days": horizon,
            "ridge_slope": s.get("slope_pooled_ridge"), "fm_t": s.get("fm_t"),
            "oof_rank_ic": v.get("oof_rank_ic"), "oos_r2_vs_zero": v.get("oos_r2_vs_zero"),
            "slope_annualized": round(slope_ann, 6),
            "native_sigma": round(sigma, 4), "neutral_estimate": round(neutral, 4),
            "calibration_scale": round(scale, 4),
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
    write_text_atomic(snippet_path, "\n".join(snippet_lines) + "\n")

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
        "inputs_sha256": {
            "config.yaml": sha256_file(config_path),
            "alpha_calibration_manifest.json": sha256_file(slopes_manifest_path),
            "alpha_slopes.csv": sha256_file(input_files["alpha_slopes.csv"]),
            "validation_manifest.json": sha256_file(validation_manifest_path),
            "oos_validation.csv": sha256_file(input_files["oos_validation.csv"]),
            "calibration_panel_manifest.json": sha256_file(panel_manifest_path),
            "calibration_panel.csv": sha256_file(input_files["calibration_panel.csv"]),
        },
        "files": {
            "proposed_calibration.csv": {"sha256": sha256_file(proposal_path), "rows": len(proposals)},
            "proposed_config_snippet.yaml": {"sha256": sha256_file(snippet_path)},
        },
    })
    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    LOGGER.info("CALIBRATION FEEDBACK: %d proposals -> %s", len(proposals), out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

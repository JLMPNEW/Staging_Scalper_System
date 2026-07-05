#!/usr/bin/env python3
"""Stage 12 - compose the DEPLOYABLE final target book with full provenance.

One sealed artifact answers "what would we trade right now, and exactly which layers produced it":

  base book  = the highest PROMOTED book in the precedence chain
               sleeves (sleeves.enabled_in_production)
               -> BL fusion (black_litterman_fusion.enabled_in_production)
               -> Stage 4 cost-adjusted AQR baseline (always available)
  exits      = exit-adjusted book replaces the base iff exit_engine.apply_in_final (else shadow)
  payout     = payout-adjusted book replaces the base iff payout.enabled_in_production (else shadow)
  governor   = gross multiplier applied iff risk_governor.apply_directive (else shadow);
               freed weight moves to CASH, never silently dropped

Promotion gates are enforced FAIL-CLOSED: with every flag at its shipped default (false) the final
book is byte-equivalent in weights to the Stage 4 baseline, and every shadow layer present in the
run is still hashed into the manifest so the full stack is auditable. Flipping any flag without its
Stage 11 evidence is a config act this script records verbatim — the manifest shows which layers
were APPLIED vs SHADOW for exactly this reason.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("compose_final_target_book")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CASH_TICKER = "CASH"
BOOK_FIELDS = ["ticker", "weight", "layer_source"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Compose the deployable final target book (Stage 12).")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", default=None)
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def _f(value: object, default: float = 0.0) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return out if out == out and out not in (float("inf"), float("-inf")) else default


def manifest_acceptance(path: Path) -> str:
    if not path.exists():
        return "MISSING"
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("acceptance", "UNKNOWN"))
    except (OSError, json.JSONDecodeError):
        return "UNREADABLE"


def load_book(path: Path, *, weight_col: str = "weight") -> dict[str, float]:
    return {
        str(r["ticker"]).strip(): _f(r.get(weight_col))
        for r in read_csv(path)
        if str(r.get("ticker", "")).strip()
    }


def main() -> int:  # noqa: C901
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    runs_root = paths.output_dir / "runs"
    run_as_of = args.as_of or latest_run_with(runs_root, "stocks_scores.csv") or date.today().isoformat()
    run_dir = runs_root / run_as_of

    out_dir = run_dir / "final"
    book_path = out_dir / "final_target_book.csv"
    manifest_path = out_dir / "final_manifest.json"
    try:
        fail_if_exists([book_path, manifest_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    sleeves_on = bool(cfg_get(config, "sleeves.enabled_in_production", False))
    bl_on = bool(cfg_get(config, "black_litterman_fusion.enabled_in_production", False))
    exits_on = bool(cfg_get(config, "exit_engine.apply_in_final", False))
    payout_on = bool(cfg_get(config, "payout.enabled_in_production", False))
    governor_on = bool(cfg_get(config, "risk_governor.apply_directive", False))

    candidates = [
        ("sleeves", sleeves_on, run_dir / "sleeves" / "sleeve_adjusted_target_weights.csv",
         run_dir / "sleeves" / "sleeve_manifest.json", "weight"),
        ("bl_fusion", bl_on, run_dir / "blacklitterman" / "costs" / "bl_cost_adjusted_target_weights.csv",
         run_dir / "blacklitterman" / "bl_manifest.json", "weight"),
        ("aqr_cost_baseline", True, run_dir / "costs" / "cost_adjusted_target_weights.csv",
         run_dir / "costs" / "cost_manifest.json", "weight"),
    ]
    layers: list[dict[str, Any]] = []
    base_name = ""
    book: dict[str, float] = {}
    for name, enabled, path, mpath, wcol in candidates:
        acceptance = manifest_acceptance(mpath)
        status = "absent" if not path.exists() else "shadow"
        if not base_name and enabled:
            if not path.exists() or not acceptance.startswith("PASS"):
                LOGGER.error("Promoted base layer %s unusable (present=%s acceptance=%s); refusing",
                             name, path.exists(), acceptance)
                return 1
            book = load_book(path, weight_col=wcol)
            base_name = name
            status = "applied_base"
        layers.append({
            "layer": name, "enabled_in_production": enabled, "status": status,
            "artifact": str(path.relative_to(run_dir)) if path.exists() else "",
            "artifact_sha256": sha256_file(path) if path.exists() else "",
            "manifest_acceptance": acceptance,
        })
    if not base_name:
        LOGGER.error("No usable base book (Stage 4 baseline missing?); run the pipeline first")
        return 1

    # exits overlay
    exits_book = run_dir / "exits" / "exit_adjusted_book.csv"
    exits_meta = run_dir / "exits" / "exit_adjusted_book_meta.json"
    exits_acc = manifest_acceptance(exits_meta)
    exits_status = "absent"
    if exits_book.exists():
        exits_status = "shadow"
        if exits_on:
            if not exits_acc.startswith("PASS"):
                LOGGER.error("exit_engine.apply_in_final=true but exit book acceptance=%s; refusing", exits_acc)
                return 1
            book = load_book(exits_book, weight_col="post_exit_weight")
            exits_status = "applied"
    elif exits_on:
        LOGGER.error("exit_engine.apply_in_final=true but no exit-adjusted book in this run; refusing")
        return 1
    layers.append({"layer": "exits_adjusted", "enabled_in_production": exits_on, "status": exits_status,
                   "artifact": "exits/exit_adjusted_book.csv" if exits_book.exists() else "",
                   "artifact_sha256": sha256_file(exits_book) if exits_book.exists() else "",
                   "manifest_acceptance": exits_acc})

    # payout overlay
    payout_book = run_dir / "payout" / "payout_adjusted_book.csv"
    payout_meta = run_dir / "payout" / "payout_manifest.json"
    payout_acc = manifest_acceptance(payout_meta)
    payout_status = "absent"
    if payout_book.exists():
        payout_status = "shadow"
        if payout_on:
            if not payout_acc.startswith("PASS"):
                LOGGER.error("payout.enabled_in_production=true but payout acceptance=%s; refusing", payout_acc)
                return 1
            book = load_book(payout_book)
            payout_status = "applied"
    elif payout_on:
        LOGGER.error("payout.enabled_in_production=true but no payout book in this run; refusing")
        return 1
    layers.append({"layer": "payout_liability", "enabled_in_production": payout_on, "status": payout_status,
                   "artifact": "payout/payout_adjusted_book.csv" if payout_book.exists() else "",
                   "artifact_sha256": sha256_file(payout_book) if payout_book.exists() else "",
                   "manifest_acceptance": payout_acc})

    # governor directive
    directive_path = run_dir / "governor" / "gross_exposure_directive.json"
    multiplier = 1.0
    governor_status = "absent"
    directive: dict[str, Any] = {}
    if directive_path.exists():
        directive = json.loads(directive_path.read_text(encoding="utf-8"))
        multiplier = _f(directive.get("gross_exposure_multiplier"), 1.0)
        governor_status = "shadow"
        if governor_on:
            governor_status = "applied"
    elif governor_on:
        LOGGER.error("risk_governor.apply_directive=true but no directive in this run; refusing")
        return 1
    layers.append({"layer": "risk_governor", "enabled_in_production": governor_on, "status": governor_status,
                   "artifact": "governor/gross_exposure_directive.json" if directive_path.exists() else "",
                   "artifact_sha256": sha256_file(directive_path) if directive_path.exists() else "",
                   "manifest_acceptance": f"multiplier={multiplier}" if directive_path.exists() else "MISSING"})

    gross_before = sum(book.values())
    applied_multiplier = multiplier if (governor_on and governor_status == "applied") else 1.0
    if applied_multiplier != 1.0:
        cash = book.get(CASH_TICKER, 0.0)
        risky = {t: w for t, w in book.items() if t != CASH_TICKER}
        scaled = {t: w * applied_multiplier for t, w in risky.items()}
        freed = sum(risky.values()) - sum(scaled.values())
        book = {**scaled, CASH_TICKER: cash + freed}

    rows = [{"ticker": t, "weight": round(w, 10),
             "layer_source": base_name if t != CASH_TICKER else f"{base_name}+overlays"}
            for t, w in sorted(book.items(), key=lambda kv: (-kv[1], kv[0]))]
    gross_after = sum(_f(r["weight"]) for r in rows)

    checks = [
        {"check": "base_layer_sealed",
         "status": "PASS",
         "detail": f"base={base_name} (promotion precedence sleeves->bl->aqr_cost_baseline)"},
        {"check": "conservation_weights_sum",
         "status": "PASS" if abs(gross_after - gross_before) < 1e-6 else "FAIL",
         "detail": f"gross {round(gross_before, 10)} -> {round(gross_after, 10)} (multiplier {applied_multiplier})"},
        {"check": "promotion_gates_fail_closed",
         "status": "PASS" if all(
             layer["status"] != "applied" or layer["enabled_in_production"]
             for layer in layers) else "FAIL",
         "detail": "every applied layer has its production flag set; all others recorded as shadow"},
    ]
    acceptance = "PASS" if all(c["status"] == "PASS" for c in checks) else "FAIL"

    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(book_path, BOOK_FIELDS, rows)
    write_manifest(manifest_path, {
        "stage": "stage12_final_target_book",
        "generated_at": utc_now(),
        "run_as_of": run_as_of,
        "acceptance": acceptance,
        "base_layer": base_name,
        "governor_multiplier_observed": multiplier,
        "governor_multiplier_applied": applied_multiplier,
        "gross_exposure": round(gross_after, 10),
        "n_positions": sum(1 for r in rows if r["ticker"] not in (CASH_TICKER, "PAYOUT_RESERVED")
                           and _f(r["weight"]) > 0),
        "layers": layers,
        "checks": checks,
    })
    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    LOGGER.info("FINAL TARGET BOOK (%s): base=%s positions=%s gross=%.6f multiplier=%.2f -> %s",
                acceptance, base_name,
                sum(1 for r in rows if _f(r["weight"]) > 0), gross_after, applied_multiplier, out_dir)
    return 0 if acceptance == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

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
import logging
import math
import sys
from datetime import date
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    manifest_acceptance_value,
    read_csv,
    read_manifest,
    sealed_artifact_errors,
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("compose_final_target_book")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CASH_TICKER = "CASH"
PAYOUT_RESERVED_TICKER = "PAYOUT_RESERVED"
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


def load_book(path: Path, *, weight_col: str = "weight") -> dict[str, float]:
    """Load a normalized long-only book and reject malformed or non-conserving inputs."""
    rows = read_csv(path)
    if not rows:
        raise ValueError(f"Book is empty: {path}")
    book: dict[str, float] = {}
    cash_rows = 0
    for row_number, row in enumerate(rows, start=2):
        ticker = str(row.get("ticker", "")).strip().upper()
        if not ticker:
            raise ValueError(f"{path}:{row_number}: blank ticker")
        if ticker in book:
            raise ValueError(f"{path}:{row_number}: duplicate ticker {ticker}")
        raw = row.get(weight_col)
        try:
            weight = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{path}:{row_number}: {ticker} non-numeric {weight_col}={raw!r}") from exc
        if not math.isfinite(weight):
            raise ValueError(f"{path}:{row_number}: {ticker} non-finite {weight_col}={raw!r}")
        if weight < -1e-12:
            raise ValueError(f"{path}:{row_number}: {ticker} negative {weight_col}={weight}")
        book[ticker] = max(0.0, weight)
        cash_rows += int(ticker == CASH_TICKER)
    if cash_rows != 1:
        raise ValueError(f"{path}: expected exactly one CASH row, got {cash_rows}")
    total = sum(book.values())
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"{path}: weights sum to {total:.12f}, expected 1.0")
    return book


def load_sealed_book(
    path: Path,
    manifest_path: Path,
    *,
    manifest_keys: tuple[str, ...],
    run_as_of: str,
    weight_col: str = "weight",
) -> tuple[dict[str, float], dict[str, Any]]:
    if not manifest_path.exists():
        raise ValueError(f"Manifest missing: {manifest_path}")
    manifest = read_manifest(manifest_path)
    errors = sealed_artifact_errors(manifest, path, *manifest_keys, run_as_of=run_as_of)
    if errors:
        raise ValueError(f"Unsealed/stale artifact {path}: {errors}")
    return load_book(path, weight_col=weight_col), manifest


def read_manifest_or_error(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return {}, f"manifest_missing={path}"
    try:
        return read_manifest(path), ""
    except (OSError, ValueError) as exc:
        return {}, f"manifest_unreadable={path}:{exc}"


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
         run_dir / "sleeves" / "sleeve_manifest.json", "weight", ("sleeve_adjusted_target_weights.csv",)),
        ("bl_fusion", bl_on, run_dir / "blacklitterman" / "costs" / "bl_cost_adjusted_target_weights.csv",
         run_dir / "blacklitterman" / "bl_manifest.json", "weight",
         ("costs/bl_cost_adjusted_target_weights.csv",)),
        ("aqr_cost_baseline", True, run_dir / "costs" / "cost_adjusted_target_weights.csv",
         run_dir / "costs" / "cost_manifest.json", "weight", ("cost_adjusted_target_weights.csv",)),
    ]
    layers: list[dict[str, Any]] = []
    base_name = ""
    book: dict[str, float] = {}
    current_book_sha256 = ""
    base_seal_errors: list[str] = []
    for name, enabled, path, mpath, wcol, manifest_keys in candidates:
        manifest: dict[str, Any] = {}
        manifest_error = ""
        if mpath.exists():
            try:
                manifest = read_manifest(mpath)
            except ValueError as exc:
                manifest_error = str(exc)
        acceptance = manifest_acceptance_value(manifest) if manifest else ("UNREADABLE" if manifest_error else "MISSING")
        seal_errors = (
            sealed_artifact_errors(manifest, path, *manifest_keys, run_as_of=run_as_of)
            if manifest else [manifest_error or f"manifest_missing={mpath}"]
        )
        status = "absent" if not path.exists() else "shadow"
        if not base_name and enabled:
            if seal_errors:
                LOGGER.error("Promoted base layer %s is not sealed/current: %s", name, seal_errors)
                return 1
            try:
                book = load_book(path, weight_col=wcol)
            except ValueError as exc:
                LOGGER.error("Promoted base layer %s is malformed: %s", name, exc)
                return 1
            base_name = name
            current_book_sha256 = sha256_file(path)
            status = "applied_base"
            base_seal_errors = seal_errors
        elif path.exists() and seal_errors:
            status = "shadow_stale"
        layers.append({
            "layer": name, "enabled_in_production": enabled, "status": status,
            "artifact": str(path.relative_to(run_dir)) if path.exists() else "",
            "artifact_sha256": sha256_file(path) if path.exists() else "",
            "manifest_sha256": sha256_file(mpath) if mpath.exists() else "",
            "manifest_acceptance": acceptance,
            "seal_errors": seal_errors,
        })
    if not base_name:
        LOGGER.error("No usable base book (Stage 4 baseline missing?); run the pipeline first")
        return 1

    # exits overlay
    exits_book = run_dir / "exits" / "exit_adjusted_book.csv"
    exits_meta = run_dir / "exits" / "exit_adjusted_book_meta.json"
    exits_manifest, exits_manifest_error = read_manifest_or_error(exits_meta)
    exits_acc = manifest_acceptance_value(exits_manifest) if exits_manifest else "MISSING"
    exits_seal = (
        sealed_artifact_errors(
            exits_manifest, exits_book, "exit_adjusted_book.csv", run_as_of=run_as_of,
        ) if exits_manifest else [exits_manifest_error]
    )
    exits_status = "absent"
    if exits_book.exists():
        exits_status = "shadow"
        if exits_on:
            if exits_seal:
                LOGGER.error("exit_engine.apply_in_final=true but exit book is unsealed/stale: %s", exits_seal)
                return 1
            try:
                book = load_book(exits_book, weight_col="post_exit_weight")
            except ValueError as exc:
                LOGGER.error("Exit-adjusted book is malformed: %s", exc)
                return 1
            current_book_sha256 = sha256_file(exits_book)
            exits_status = "applied"
        elif exits_seal:
            exits_status = "shadow_stale"
    elif exits_on:
        LOGGER.error("exit_engine.apply_in_final=true but no exit-adjusted book in this run; refusing")
        return 1
    layers.append({"layer": "exits_adjusted", "enabled_in_production": exits_on, "status": exits_status,
                   "artifact": "exits/exit_adjusted_book.csv" if exits_book.exists() else "",
                   "artifact_sha256": sha256_file(exits_book) if exits_book.exists() else "",
                   "manifest_sha256": sha256_file(exits_meta) if exits_meta.exists() else "",
                   "manifest_acceptance": exits_acc, "seal_errors": exits_seal})

    # payout overlay
    payout_book = run_dir / "payout" / "payout_adjusted_book.csv"
    payout_meta = run_dir / "payout" / "payout_manifest.json"
    payout_manifest, payout_manifest_error = read_manifest_or_error(payout_meta)
    payout_acc = manifest_acceptance_value(payout_manifest) if payout_manifest else "MISSING"
    payout_seal = (
        sealed_artifact_errors(
            payout_manifest, payout_book, "payout_adjusted_book.csv", run_as_of=run_as_of,
        ) if payout_manifest else [payout_manifest_error]
    )
    payout_status = "absent"
    if payout_book.exists():
        payout_status = "shadow"
        if payout_on:
            if payout_seal:
                LOGGER.error("payout.enabled_in_production=true but payout book is unsealed/stale: %s", payout_seal)
                return 1
            payout_source_sha = str(payout_manifest.get("book_sha256", "")).strip()
            if not current_book_sha256 or payout_source_sha != current_book_sha256:
                LOGGER.error(
                    "Payout source book does not match the active pre-payout book: recorded=%s active=%s source=%s",
                    payout_source_sha or "MISSING",
                    current_book_sha256 or "MISSING",
                    payout_manifest.get("book_source", "MISSING"),
                )
                return 1
            try:
                book = load_book(payout_book)
            except ValueError as exc:
                LOGGER.error("Payout-adjusted book is malformed: %s", exc)
                return 1
            current_book_sha256 = sha256_file(payout_book)
            payout_status = "applied"
        elif payout_seal:
            payout_status = "shadow_stale"
    elif payout_on:
        LOGGER.error("payout.enabled_in_production=true but no payout book in this run; refusing")
        return 1
    layers.append({"layer": "payout_liability", "enabled_in_production": payout_on, "status": payout_status,
                   "artifact": "payout/payout_adjusted_book.csv" if payout_book.exists() else "",
                   "artifact_sha256": sha256_file(payout_book) if payout_book.exists() else "",
                   "manifest_sha256": sha256_file(payout_meta) if payout_meta.exists() else "",
                   "manifest_acceptance": payout_acc, "seal_errors": payout_seal})

    # governor directive
    directive_path = run_dir / "governor" / "gross_exposure_directive.json"
    governor_manifest_path = run_dir / "governor" / "governor_manifest.json"
    multiplier = 1.0
    governor_status = "absent"
    directive: dict[str, Any] = {}
    governor_seal: list[str] = []
    if directive_path.exists():
        try:
            directive = read_manifest(directive_path)
            governor_manifest = read_manifest(governor_manifest_path)
            governor_seal = sealed_artifact_errors(
                governor_manifest,
                directive_path,
                "gross_exposure_directive.json",
                run_as_of=run_as_of,
            )
            raw_multiplier = directive.get("gross_exposure_multiplier")
            if raw_multiplier is None:
                raise ValueError("gross_exposure_directive is missing gross_exposure_multiplier")
            multiplier = float(raw_multiplier)
            if not math.isfinite(multiplier) or not 0.0 <= multiplier <= 1.0:
                governor_seal.append(f"gross_exposure_multiplier_out_of_range={raw_multiplier!r}")
            if str(directive.get("run_as_of", "")) != run_as_of:
                governor_seal.append(f"directive_run_as_of={directive.get('run_as_of')} expected={run_as_of}")
        except (OSError, ValueError, TypeError) as exc:
            governor_seal.append(str(exc))
        governor_status = "shadow_stale" if governor_seal else "shadow"
        if governor_on and not governor_seal:
            governor_status = "applied"
        elif governor_on:
            LOGGER.error("risk_governor.apply_directive=true but directive is unsealed/invalid: %s", governor_seal)
            return 1
    elif governor_on:
        LOGGER.error("risk_governor.apply_directive=true but no directive in this run; refusing")
        return 1
    layers.append({"layer": "risk_governor", "enabled_in_production": governor_on, "status": governor_status,
                   "artifact": "governor/gross_exposure_directive.json" if directive_path.exists() else "",
                   "artifact_sha256": sha256_file(directive_path) if directive_path.exists() else "",
                   "manifest_sha256": sha256_file(governor_manifest_path) if governor_manifest_path.exists() else "",
                   "manifest_acceptance": f"multiplier={multiplier}" if directive_path.exists() else "MISSING",
                   "seal_errors": governor_seal})

    gross_before = sum(book.values())
    applied_multiplier = multiplier if (governor_on and governor_status == "applied") else 1.0
    if applied_multiplier != 1.0:
        cash = book.get(CASH_TICKER, 0.0)
        reserved = book.get(PAYOUT_RESERVED_TICKER, 0.0)
        risky = {
            t: w for t, w in book.items()
            if t not in {CASH_TICKER, PAYOUT_RESERVED_TICKER}
        }
        scaled = {t: w * applied_multiplier for t, w in risky.items()}
        freed = sum(risky.values()) - sum(scaled.values())
        book = {**scaled, CASH_TICKER: cash + freed}
        if reserved > 0.0:
            book[PAYOUT_RESERVED_TICKER] = reserved

    malformed_final = [f"{ticker}={weight}" for ticker, weight in book.items()
                       if not math.isfinite(weight) or weight < -1e-12]

    rows = [{"ticker": t, "weight": round(w, 10),
             "layer_source": base_name if t != CASH_TICKER else f"{base_name}+overlays"}
            for t, w in sorted(book.items(), key=lambda kv: (-kv[1], kv[0]))]
    gross_after = sum(float(r["weight"]) for r in rows)

    checks = [
        {"check": "base_layer_sealed",
         "status": "PASS" if not base_seal_errors else "FAIL",
         "detail": f"base={base_name}; seal_errors={base_seal_errors}"},
        {"check": "conservation_weights_sum",
         "status": "PASS" if abs(gross_before - 1.0) < 1e-6 and abs(gross_after - 1.0) < 1e-6 else "FAIL",
         "detail": f"weights {round(gross_before, 10)} -> {round(gross_after, 10)} (multiplier {applied_multiplier})"},
        {"check": "long_only_finite_unique_cash",
         "status": "PASS" if not malformed_final and list(book).count(CASH_TICKER) == 1 else "FAIL",
         "detail": "finite non-negative unique ticker weights with exactly one CASH row"
         if not malformed_final else str(malformed_final[:8])},
        {"check": "promotion_gates_fail_closed",
         "status": "PASS" if all(
             layer["status"] != "applied" or layer["enabled_in_production"]
             for layer in layers) else "FAIL",
         "detail": "every applied layer has its production flag set; all others recorded as shadow"},
    ]
    acceptance = "PASS" if all(c["status"] == "PASS" for c in checks) else "FAIL"

    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(book_path, BOOK_FIELDS, rows)
    manifest_payload = {
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
        "provenance_sha256": {
            "config.yaml": sha256_file(config_path),
            "20_compose_final_target_book.py": sha256_file(Path(__file__).resolve()),
        },
        "files": {
            "final_target_book.csv": {"sha256": sha256_file(book_path), "rows": len(rows)},
        },
    }
    write_manifest(manifest_path, manifest_payload)
    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    LOGGER.info("FINAL TARGET BOOK (%s): base=%s positions=%s gross=%.6f multiplier=%.2f -> %s",
                acceptance, base_name,
                sum(1 for r in rows if _f(r["weight"]) > 0), gross_after, applied_multiplier, out_dir)
    return 0 if acceptance == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

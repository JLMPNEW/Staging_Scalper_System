#!/usr/bin/env python3
"""Stage 12a - compose sealed deployable target weights with full provenance.

This pre-monitor artifact answers which weights are deployable and which layers produced them.
Stage 12b enriches these immutable weights with monitor, levels, macro, earnings, and IB context.

  base book  = the highest PROMOTED book in the precedence chain
               sleeves (sleeves.enabled_in_production)
               -> BL fusion (black_litterman_fusion.enabled_in_production)
               -> Stage 4 cost-adjusted AQR baseline (always available)
  exits      = exit-adjusted book replaces the base iff exit_engine.apply_in_final AND its
               manifest's inputs_sha256.book equals the composed base book sha (else shadow);
               on replacement the superseded layers are recorded as superseded_by_exits
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
from portfolio_layer.core.artifacts import mark_final_report_stale  # noqa: E402
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
WEIGHT_FIELDS = ["ticker", "weight"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Compose deployable target weights (Stage 12a).")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", default=None)
    p.add_argument("--force", action="store_true")
    p.add_argument(
        "--monitor-bootstrap",
        action="store_true",
        help=(
            "Write a sealed but explicitly non-deployable preliminary target for "
            "same-day monitor construction."
        ),
    )
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


def read_manifest_or_error(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return {}, f"manifest_missing={path}"
    try:
        return read_manifest(path), ""
    except (OSError, ValueError) as exc:
        return {}, f"manifest_unreadable={path}:{exc}"


def require_monitor_filtered_aqr_lineage(
    run_dir: Path,
    *,
    run_as_of: str,
    cost_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Require the same-date monitor-filtered Stage 3 book behind Stage 4."""
    optimizer_dir = run_dir / "optimizer"
    optimizer_manifest_path = optimizer_dir / "optimizer_manifest.json"
    target_path = optimizer_dir / "target_weights.csv"
    monitor_manifest_path = optimizer_dir / "monitor_eligibility_manifest.json"
    monitor_overlay_path = optimizer_dir / "monitor_eligibility_overlay.csv"
    required = (
        optimizer_manifest_path,
        target_path,
        monitor_manifest_path,
        monitor_overlay_path,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ValueError(f"monitor-filtered AQR lineage is incomplete: {missing}")

    optimizer_manifest = read_manifest(optimizer_manifest_path)
    monitor_manifest = read_manifest(monitor_manifest_path)
    errors = sealed_artifact_errors(
        optimizer_manifest,
        target_path,
        "target_weights.csv",
        run_as_of=run_as_of,
        allow_deferred=False,
    )
    errors.extend(
        sealed_artifact_errors(
            monitor_manifest,
            monitor_overlay_path,
            "monitor_eligibility_overlay.csv",
            run_as_of=run_as_of,
            allow_deferred=False,
        )
    )
    monitor_meta = optimizer_manifest.get("monitor_entry_policy", {})
    if optimizer_manifest.get("deployable") is not True:
        errors.append("optimizer_manifest.deployable!=true")
    if not isinstance(monitor_meta, dict) or monitor_meta.get("status") != "applied":
        errors.append("optimizer monitor_entry_policy.status!=applied")
    if monitor_manifest.get("production_entry_gate") is not True:
        errors.append("monitor manifest production_entry_gate!=true")

    cost_provenance = cost_manifest.get("provenance_sha256", {})
    if not isinstance(cost_provenance, dict):
        errors.append("cost manifest provenance_sha256 missing")
        cost_provenance = {}
    if cost_provenance.get("optimizer_manifest.json") != sha256_file(
        optimizer_manifest_path
    ):
        errors.append("cost manifest does not consume current optimizer manifest")
    if cost_provenance.get("target_weights.csv") != sha256_file(target_path):
        errors.append("cost manifest does not consume current target weights")

    optimizer_provenance = optimizer_manifest.get("provenance_sha256", {})
    if not isinstance(optimizer_provenance, dict):
        errors.append("optimizer provenance_sha256 missing")
        optimizer_provenance = {}
    if optimizer_provenance.get(
        "monitor_eligibility_overlay.csv"
    ) != sha256_file(monitor_overlay_path):
        errors.append("optimizer does not seal current monitor eligibility overlay")
    if optimizer_provenance.get(
        "monitor_eligibility_manifest.json"
    ) != sha256_file(monitor_manifest_path):
        errors.append("optimizer does not seal current monitor eligibility manifest")
    if errors:
        raise ValueError("; ".join(errors))

    return {
        "status": "applied",
        "policy_version": str(
            monitor_manifest.get("policy", {}).get("policy_version", "")
        )
        if isinstance(monitor_manifest.get("policy"), dict)
        else "",
        "optimizer_manifest_sha256": sha256_file(optimizer_manifest_path),
        "monitor_manifest_sha256": sha256_file(monitor_manifest_path),
        "monitor_overlay_sha256": sha256_file(monitor_overlay_path),
        "entry_eligible_count": monitor_manifest.get("entry_eligible_count"),
    }


def main() -> int:  # noqa: C901
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    runs_root = paths.output_dir / "runs"
    if args.as_of:
        try:
            date.fromisoformat(args.as_of)
        except ValueError:
            LOGGER.error("Invalid --as-of %r: expected an ISO date (YYYY-MM-DD)", args.as_of)
            return 1
    run_as_of = args.as_of or latest_run_with(runs_root, "stocks_scores.csv") or date.today().isoformat()
    run_dir = runs_root / run_as_of

    # A stale-prior marker means the governor for this date was built against a superseded
    # directive chain; composing on top of it would launder a stale risk state into the book.
    stale_marker = run_dir / "governor" / "PRIOR_DIRECTIVE_STALE.json"
    if stale_marker.exists():
        LOGGER.error(
            "Governor prior-directive stale marker present (%s); re-run the governor for %s "
            "before composing the final book",
            stale_marker, run_as_of,
        )
        return 1

    out_dir = run_dir / "final"
    weights_name = (
        "bootstrap_target_weights.csv"
        if args.monitor_bootstrap
        else "final_target_weights.csv"
    )
    manifest_name = (
        "bootstrap_final_weights_manifest.json"
        if args.monitor_bootstrap
        else "final_weights_manifest.json"
    )
    weights_path = out_dir / weights_name
    manifest_path = out_dir / manifest_name
    guarded_outputs = [weights_path, manifest_path]
    try:
        fail_if_exists(guarded_outputs, force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    sleeves_on = bool(cfg_get(config, "sleeves.enabled_in_production", False))
    bl_on = bool(cfg_get(config, "black_litterman_fusion.enabled_in_production", False))
    exits_on = bool(cfg_get(config, "exit_engine.apply_in_final", False))
    payout_on = bool(cfg_get(config, "payout.enabled_in_production", False))
    governor_on = bool(cfg_get(config, "risk_governor.apply_directive", False))
    monitor_policy = cfg_get(config, "optimizer.monitor_entry_policy", {})
    monitor_policy_on = (
        isinstance(monitor_policy, dict)
        and monitor_policy.get("enabled_in_production") is True
    )

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
    base_manifest: dict[str, Any] = {}
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
            sealed_artifact_errors(
                manifest, path, *manifest_keys, run_as_of=run_as_of, allow_deferred=False,
            )
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
            base_manifest = manifest
            current_book_sha256 = sha256_file(path)
            status = "applied_base"
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

    monitor_lineage: dict[str, Any] = {"status": "disabled"}
    if monitor_policy_on and args.monitor_bootstrap:
        monitor_lineage = {
            "status": "bootstrap_ignored",
            "policy_version": str(monitor_policy.get("policy_version", "")),
        }
    elif monitor_policy_on:
        if base_name != "aqr_cost_baseline":
            LOGGER.error(
                "Monitor optimizer entry policy is production-enabled, but promoted base %s "
                "does not yet declare the monitor-filter lineage",
                base_name,
            )
            return 1
        try:
            monitor_lineage = require_monitor_filtered_aqr_lineage(
                run_dir,
                run_as_of=run_as_of,
                cost_manifest=base_manifest,
            )
        except ValueError as exc:
            LOGGER.error("Refusing non-filtered deployable AQR book: %s", exc)
            return 1
        layers.append(
            {
                "layer": "monitor_optimizer_entry",
                "enabled_in_production": True,
                "status": "applied",
                "artifact": "optimizer/monitor_eligibility_overlay.csv",
                "artifact_sha256": monitor_lineage["monitor_overlay_sha256"],
                "manifest_sha256": monitor_lineage["monitor_manifest_sha256"],
                "manifest_acceptance": "PASS",
                "seal_errors": [],
            }
        )

    # exits overlay
    exits_book = run_dir / "exits" / "exit_adjusted_book.csv"
    exits_meta = run_dir / "exits" / "exit_adjusted_book_meta.json"
    exits_manifest, exits_manifest_error = read_manifest_or_error(exits_meta)
    exits_acc = manifest_acceptance_value(exits_manifest) if exits_manifest else "MISSING"
    exits_seal = (
        sealed_artifact_errors(
            exits_manifest, exits_book, "exit_adjusted_book.csv", run_as_of=run_as_of,
            allow_deferred=False,
        ) if exits_manifest else [exits_manifest_error]
    )
    exits_status = "absent"
    exits_replaced_book_sha256 = ""
    if exits_book.exists():
        exits_status = "shadow"
        if exits_on:
            if exits_seal:
                LOGGER.error("exit_engine.apply_in_final=true but exit book is unsealed/stale: %s", exits_seal)
                return 1
            # Lineage guard: the exit-adjusted book may only REPLACE the composed base book if its
            # producer declares it adjusted exactly that book. A book adjusted from another source
            # (e.g. broker holdings) is a different universe and must not be promoted here.
            exits_inputs = exits_manifest.get("inputs_sha256")
            exits_source_sha = (
                str(exits_inputs.get("book", "")).strip() if isinstance(exits_inputs, dict) else ""
            )
            exits_book_source = str(exits_manifest.get("book_source", "")).strip()
            if not exits_source_sha or exits_source_sha != current_book_sha256:
                LOGGER.error(
                    "exit_engine.apply_in_final=true but the exit-adjusted book was not built from "
                    "the composed base book: exits inputs_sha256.book=%s (book_source=%s) != base "
                    "%s sha256=%s; refusing to replace the deployable book",
                    exits_source_sha or "MISSING", exits_book_source or "MISSING",
                    base_name, current_book_sha256,
                )
                return 1
            try:
                book = load_book(exits_book, weight_col="post_exit_weight")
            except ValueError as exc:
                LOGGER.error("Exit-adjusted book is malformed: %s", exc)
                return 1
            exits_replaced_book_sha256 = current_book_sha256
            current_book_sha256 = sha256_file(exits_book)
            exits_status = "applied"
            # The earlier layers' weights do not survive the replacement; record that truthfully.
            for layer in layers:
                if layer["status"] in ("applied_base", "applied"):
                    layer["status"] = "superseded_by_exits"
            if monitor_lineage.get("status") == "applied":
                monitor_lineage["weights_superseded_by_exits"] = True
        elif exits_seal:
            exits_status = "shadow_stale"
    elif exits_on:
        LOGGER.error("exit_engine.apply_in_final=true but no exit-adjusted book in this run; refusing")
        return 1
    layers.append({"layer": "exits_adjusted", "enabled_in_production": exits_on, "status": exits_status,
                   "artifact": "exits/exit_adjusted_book.csv" if exits_book.exists() else "",
                   "artifact_sha256": sha256_file(exits_book) if exits_book.exists() else "",
                   "manifest_sha256": sha256_file(exits_meta) if exits_meta.exists() else "",
                   "manifest_acceptance": exits_acc,
                   "replaced_book_sha256": exits_replaced_book_sha256,
                   "seal_errors": exits_seal})

    # payout overlay
    payout_book = run_dir / "payout" / "payout_adjusted_book.csv"
    payout_meta = run_dir / "payout" / "payout_manifest.json"
    payout_manifest, payout_manifest_error = read_manifest_or_error(payout_meta)
    payout_acc = manifest_acceptance_value(payout_manifest) if payout_manifest else "MISSING"
    payout_seal = (
        sealed_artifact_errors(
            payout_manifest, payout_book, "payout_adjusted_book.csv", run_as_of=run_as_of,
            allow_deferred=False,
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
    governor_acc = "MISSING"
    if directive_path.exists():
        # A present-but-unreadable/corrupt directive is a hard error even in shadow mode:
        # silently composing with the 1.0 default would bury the failure in seal_errors.
        try:
            directive = read_manifest(directive_path)
            raw_multiplier = directive.get("gross_exposure_multiplier")
            if raw_multiplier is None:
                raise ValueError("gross_exposure_directive is missing gross_exposure_multiplier")
            multiplier = float(raw_multiplier)
            if not math.isfinite(multiplier):
                raise ValueError(f"gross_exposure_multiplier is non-finite: {raw_multiplier!r}")
        except (OSError, ValueError, TypeError) as exc:
            LOGGER.error("Governor directive %s is unreadable/corrupt; refusing to compose: %s",
                         directive_path, exc)
            return 1
        governor_manifest, governor_manifest_error = read_manifest_or_error(governor_manifest_path)
        if governor_manifest:
            governor_acc = manifest_acceptance_value(governor_manifest)
            governor_seal = sealed_artifact_errors(
                governor_manifest,
                directive_path,
                "gross_exposure_directive.json",
                run_as_of=run_as_of,
            )
        else:
            governor_acc = "UNREADABLE" if "unreadable" in governor_manifest_error else "MISSING"
            governor_seal = [governor_manifest_error]
        if not 0.0 <= multiplier <= 1.0:
            governor_seal.append(f"gross_exposure_multiplier_out_of_range={multiplier!r}")
        if str(directive.get("run_as_of", "")) != run_as_of:
            governor_seal.append(f"directive_run_as_of={directive.get('run_as_of')} expected={run_as_of}")
        # Lineage guard: the multiplier was measured on a specific book; applying it to any other
        # book (sleeves/BL base, exits- or payout-replaced) is a different risk state.
        governor_inputs = governor_manifest.get("inputs_sha256")
        governor_book_sha = (
            str(governor_inputs.get("book", "")).strip() if isinstance(governor_inputs, dict) else ""
        )
        if governor_book_sha != current_book_sha256:
            governor_seal.append(
                "directive_book_lineage_mismatch: governor measured inputs_sha256.book="
                f"{governor_book_sha or 'MISSING'} (directive book_source="
                f"{directive.get('book_source', 'MISSING')}) but the book being scaled has "
                f"sha256={current_book_sha256}"
            )
        governor_status = "shadow_stale" if governor_seal else "shadow"
        if governor_on and not governor_seal:
            governor_status = "applied"
        elif governor_on:
            LOGGER.error(
                "risk_governor.apply_directive=true but directive is unsealed/invalid or was "
                "measured on a different book than the one being scaled: %s", governor_seal)
            return 1
    elif governor_on:
        LOGGER.error("risk_governor.apply_directive=true but no directive in this run; refusing")
        return 1
    layers.append({"layer": "risk_governor", "enabled_in_production": governor_on, "status": governor_status,
                   "artifact": "governor/gross_exposure_directive.json" if directive_path.exists() else "",
                   "artifact_sha256": sha256_file(directive_path) if directive_path.exists() else "",
                   "manifest_sha256": sha256_file(governor_manifest_path) if governor_manifest_path.exists() else "",
                   "manifest_acceptance": governor_acc,
                   "gross_exposure_multiplier": multiplier if directive_path.exists() else None,
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

    rows = [{"ticker": t, "weight": round(w, 10)}
            for t, w in sorted(book.items(), key=lambda kv: (-kv[1], kv[0]))]
    gross_after = sum(float(r["weight"]) for r in rows)

    out_dir.mkdir(parents=True, exist_ok=True)
    if not args.monitor_bootstrap:
        # Preserve the last accepted Streamlit/report pair while making its
        # staleness explicit to the orchestration resume gate. Stage 12b swaps
        # in a passing replacement only after its full validation completes.
        mark_final_report_stale(run_dir, "final")
    write_csv(weights_path, WEIGHT_FIELDS, rows)
    # Re-validate the WRITTEN artifact end-to-end (long-only, finite, unique tickers, exactly one
    # CASH row, conservation after rounding) so the acceptance reflects what consumers will read.
    written_book_error = ""
    try:
        load_book(weights_path)
    except (OSError, ValueError) as exc:
        written_book_error = str(exc)

    checks = [
        {"check": "conservation_weights_sum",
         "status": "PASS" if abs(gross_before - 1.0) < 1e-6 and abs(gross_after - 1.0) < 1e-6 else "FAIL",
         "detail": f"weights {round(gross_before, 10)} -> {round(gross_after, 10)} (multiplier {applied_multiplier})"},
        {"check": "written_book_valid",
         "status": "PASS" if not written_book_error else "FAIL",
         "detail": "written weights re-loaded: long-only finite unique tickers, exactly one CASH "
         "row, weights sum to 1.0" if not written_book_error else written_book_error},
        {"check": "promotion_gates_fail_closed",
         "status": "PASS" if all(
             layer["status"] != "applied" or layer["enabled_in_production"]
             for layer in layers) else "FAIL",
         "detail": "every applied layer has its production flag set; all others recorded as shadow"},
        {"check": "monitor_optimizer_entry_lineage",
         "status": (
             "PASS"
             if (
                 not monitor_policy_on
                 or monitor_lineage.get("status") == "applied"
                 or (
                     args.monitor_bootstrap
                     and monitor_lineage.get("status") == "bootstrap_ignored"
                 )
             )
             else "FAIL"
         ),
         "detail": (
             f"production_enabled={monitor_policy_on}; "
             f"bootstrap={args.monitor_bootstrap}; "
             f"status={monitor_lineage.get('status')}"
         )},
    ]
    acceptance = "PASS" if all(c["status"] == "PASS" for c in checks) else "FAIL"

    n_positions = sum(
        1 for r in rows
        if r["ticker"] not in (CASH_TICKER, PAYOUT_RESERVED_TICKER) and _f(r["weight"]) > 0
    )
    manifest_payload = {
        "stage": (
            "stage12a_monitor_bootstrap_target"
            if args.monitor_bootstrap
            else "stage12a_final_target_weights"
        ),
        "generated_at": utc_now(),
        "run_as_of": run_as_of,
        "acceptance": acceptance,
        "deployable": acceptance == "PASS" and not args.monitor_bootstrap,
        "book_role": "monitor_bootstrap" if args.monitor_bootstrap else "deployable_final",
        "base_layer": base_name,
        "governor_multiplier_observed": multiplier,
        "governor_multiplier_applied": applied_multiplier,
        "gross_exposure": round(gross_after, 10),
        "n_positions": n_positions,
        "layers": layers,
        "monitor_entry_policy": monitor_lineage,
        "checks": checks,
        "provenance_sha256": {
            "config.yaml": sha256_file(config_path),
            "20_compose_final_target_book.py": sha256_file(Path(__file__).resolve()),
            **(
                {
                    "optimizer/optimizer_manifest.json": monitor_lineage[
                        "optimizer_manifest_sha256"
                    ],
                    "optimizer/monitor_eligibility_manifest.json": monitor_lineage[
                        "monitor_manifest_sha256"
                    ],
                    "optimizer/monitor_eligibility_overlay.csv": monitor_lineage[
                        "monitor_overlay_sha256"
                    ],
                }
                if monitor_policy_on and not args.monitor_bootstrap
                else {}
            ),
        },
        "files": {
            weights_name: {
                "sha256": sha256_file(weights_path),
                "rows": len(rows),
            },
        },
    }
    write_manifest(manifest_path, manifest_payload)
    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    LOGGER.info("FINAL TARGET WEIGHTS (%s): base=%s positions=%s gross=%.6f multiplier=%.2f -> %s",
                acceptance, base_name, n_positions, gross_after, applied_multiplier, out_dir)
    return 0 if acceptance == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

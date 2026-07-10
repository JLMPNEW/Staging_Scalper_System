#!/usr/bin/env python3
"""Stage 12 - one-command portfolio pipeline runner (core DAG, strategic/tactical cadences).

Runs the sealed per-as-of chain in dependency order, verifying each stage's manifest acceptance
before continuing:

  scores    01 -> 02 -> 03                       (Stage 1 contract)
  risk      04 -> 05 -> 06 -> 07 -> [05d] -> 08    (Stage 2 panel; 05d runs when a spread snapshot exists)
  optimizer 09 -> 10                             (Stage 3 AQR baseline)
  costs     12 -> 13 -> 14 -> 15                 (Stage 4)
  rotation  17 -> 18                             (Stage 5, shadow)
  macro     21 -> 22                             (Stage 6, shadow)
  bl        23 -> 24 -> 25 -> 26                 (Stage 7, shadow)
  sleeves   27 -> 28 -> 29                       (Stage 8, shadow)
  ledger    31 -> 32                             (Stage 8.5; skipped unless broker imports exist)
  exits     33 -> 34 -> 35                       (Stage 9, needs ledger)
  governor  19                                   (Stage 12 bounded gross directive)

Cadences (config `orchestration`): `tactical` refreshes the fast loop, including rotation;
`strategic` runs every group. Stages are immutable by default; `--force` explicitly rebuilds them.
Each producer invalidates dependent seals first. IB liquidity collection (05c) is never launched here; it needs a live
IB session and belongs to the overnight process; 13's spread_source fallback covers its absence.

Every run writes runs/<as_of>/orchestration_meta.json with per-step durations, exit codes, and the
acceptance read from each stage manifest. Forecast and hedging stay out of the DAG until Stage 11
promotes them; payout/final composition are implemented as shadow-aware Stage 12 groups.
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    manifest_acceptance_value,
    read_manifest,
    sha256_file,
    write_manifest,
)
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("run_portfolio_pipeline")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"

# group -> ordered (subdir, script, acceptance manifest relative to the run dir | None)
GROUPS: dict[str, list[tuple[str, str, str | None]]] = {
    "scores": [
        ("scores", "01_collect_sector_scores.py", None),
        ("scores", "02_calibrate_cross_sector_scores.py", None),
        ("scores", "03_validate_score_contract.py", "manifest.json"),
    ],
    "risk": [
        ("risk", "04_check_risk_readiness.py", None),
        ("risk", "05_build_return_panel.py", None),
        ("risk", "06_build_risk_coverage.py", None),
        ("risk", "07_build_covariance_model.py", None),
        ("risk", "05d_audit_liquidity_panel.py", None),
        ("risk", "08_validate_risk_panel.py", "risk/risk_manifest.json"),
    ],
    "optimizer": [
        ("optimizer", "09_run_portfolio_optimizer.py", None),
        ("optimizer", "10_validate_optimizer_outputs.py", "optimizer/optimizer_manifest.json"),
    ],
    "costs": [
        ("costs", "12_build_trade_list.py", None),
        ("costs", "13_build_cost_model.py", None),
        ("costs", "14_apply_no_trade_bands.py", None),
        ("costs", "15_validate_cost_model.py", "costs/cost_manifest.json"),
    ],
    "rotation": [
        ("rotation", "17_build_rotation_signals.py", None),
        ("rotation", "18_validate_rotation_signals.py", "rotation/rotation_manifest.json"),
    ],
    "macro": [
        ("macro", "21_build_macro_contract.py", None),
        ("macro", "22_validate_macro_contract.py", "macro/macro_manifest.json"),
    ],
    "bl": [
        ("blacklitterman", "23_build_bl_inputs.py", None),
        ("blacklitterman", "24_run_bl_optimizer.py", None),
        ("blacklitterman", "25_apply_bl_cost_overlay.py", None),
        ("blacklitterman", "26_validate_bl_fusion.py", "blacklitterman/bl_manifest.json"),
    ],
    "sleeves": [
        ("sleeves", "27_build_sleeve_framework.py", None),
        ("sleeves", "28_apply_risk_budgets.py", None),
        ("sleeves", "29_validate_sleeves.py", "sleeves/sleeve_manifest.json"),
    ],
    "ledger": [
        ("ledger", "30_import_ib_activity_statement.py", None),
        ("ledger", "31_build_holdings_ledger.py", None),
        ("ledger", "32_validate_holdings_ledger.py", "ledger/ledger_manifest.json"),
    ],
    "exits": [
        ("exits", "33_build_exit_signals.py", None),
        ("exits", "34_apply_exits.py", None),
        ("exits", "35_validate_exits.py", "exits/exit_manifest.json"),
        ("exits", "36_build_exit_adjusted_book.py", "exits/exit_adjusted_book_meta.json"),
    ],
    "payout": [
        ("payout", "14_build_payout_liability.py", "payout/payout_manifest.json"),
    ],
    "governor": [
        ("orchestration", "19_run_risk_governor.py", "governor/governor_manifest.json"),
    ],
    "final": [
        ("orchestration", "20_compose_final_target_book.py", "final/final_manifest.json"),
    ],
}
# read-only checks take no --force (nothing to overwrite)
NO_FORCE_SCRIPTS = {"04_check_risk_readiness.py"}
DEFAULT_CADENCES = {
    "tactical": ["scores", "risk", "optimizer", "costs", "rotation", "governor", "final"],
    "strategic": ["scores", "risk", "optimizer", "costs", "rotation", "macro", "bl", "sleeves",
                  "ledger", "exits", "payout", "governor", "final"],
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stage 12 one-command portfolio pipeline runner.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", default=None, help="Run as-of date (default: latest run folder, else today).")
    p.add_argument("--cadence", choices=("tactical", "strategic"), default="strategic")
    p.add_argument("--groups", default="", help="Comma-separated explicit group list (overrides cadence).")
    p.add_argument("--skip", default="", help="Comma-separated groups to skip.")
    p.add_argument("--force", action="store_true", help="Forwarded to every stage script.")
    p.add_argument(
        "--reuse-risk-price-data",
        action="store_true",
        help=(
            "Explicitly allow Stage 2 to seed from this run's hash-checked adjusted-price panel "
            "and current local price cache. Useful for deterministic historical rebuilds when "
            "the remote provider is unavailable; never enabled implicitly."
        ),
    )
    p.add_argument("--dry-run", action="store_true", help="Print the plan without executing.")
    p.add_argument("--continue-on-fail", action="store_true",
                   help="Keep running later groups after a failure (default: stop).")
    return p.parse_args()


def script_args(args: argparse.Namespace, script: str) -> list[str]:
    """Return the exact optional flags for a stage script.

    Dry-run and execution both call this function so the displayed plan cannot drift from the
    command that is actually launched.
    """
    flags: list[str] = []
    if args.force and script not in NO_FORCE_SCRIPTS:
        flags.append("--force")
    if args.force and script == "01_collect_sector_scores.py":
        flags.append("--reuse-sealed-run-raw")
    if args.reuse_risk_price_data and script == "05_build_return_panel.py":
        flags.extend(("--reuse-existing-panel", "--reuse-price-cache"))
    return flags


def manifest_acceptance(run_dir: Path, rel: str) -> str:
    path = run_dir / rel
    if not path.exists():
        return "MISSING"
    try:
        manifest = read_manifest(path)
        manifest_as_of = str(
            manifest.get("run_as_of", manifest.get("run_as_of_date", manifest.get("ledger_as_of", "")))
        ).strip()
        if manifest_as_of and manifest_as_of != run_dir.name:
            return f"DATE_MISMATCH:{manifest_as_of}"
        return manifest_acceptance_value(manifest) or "UNKNOWN"
    except ValueError:
        return "UNREADABLE"


def _broker_statement_available(config: dict[str, Any], config_path: Path, as_of: str) -> bool:
    """True when the configured IB report dir holds a statement whose period ends on as_of."""
    from portfolio_layer.core.config import resolve_path
    from portfolio_layer.ledger.ledger_common import peek_statement_period_end

    source_dir = resolve_path(
        cfg_get(config, "holdings_ledger.source_reports_dir", "../IB_reports"), base_dir=config_path.parent
    )
    if not source_dir.exists():
        return False
    glob = str(cfg_get(config, "holdings_ledger.statement_glob", "U*.csv") or "U*.csv")
    try:
        return any(peek_statement_period_end(f) == as_of for f in sorted(source_dir.glob(glob)))
    except OSError:
        return False


def plan_groups(args: argparse.Namespace, config: dict[str, Any], run_dir: Path) -> list[str]:
    orch = cfg_get(config, "orchestration", {}) or {}
    cadences = {**DEFAULT_CADENCES, **(orch.get("cadences") or {})}
    explicit = [g.strip() for g in args.groups.split(",") if g.strip()]
    cadence_groups = cadences.get(args.cadence)
    if not explicit and not isinstance(cadence_groups, (list, tuple)):
        raise ValueError(f"orchestration cadence {args.cadence!r} must be a list of group names")
    groups = explicit
    if not groups:
        assert isinstance(cadence_groups, (list, tuple))
        groups = [str(group).strip() for group in cadence_groups if str(group).strip()]
    skip = {g.strip() for g in args.skip.split(",") if g.strip()}
    unknown = sorted((set(groups) | skip) - set(GROUPS))
    if unknown:
        raise ValueError(f"unknown orchestration groups: {unknown}; valid={sorted(GROUPS)}")
    planned = [g for g in groups if g not in skip]
    # ledger needs a broker statement dated exactly at this as-of (ledger/30 imports it as the first
    # ledger step); exits need the ledger. Skip both gracefully when neither a sealed import nor a
    # matching statement exists.
    if "ledger" in planned and not (run_dir / "ledger" / "broker_statement_sources.csv").exists():
        config_path = args.config.expanduser().resolve()
        if not _broker_statement_available(config, config_path, run_dir.name):
            LOGGER.info("group ledger skipped: no sealed import and no IB statement ending %s "
                        "in the configured report dir", run_dir.name)
            planned = [g for g in planned if g not in ("ledger", "exits")]
    return planned


def main() -> int:  # noqa: C901
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    runs_root = paths.output_dir / "runs"
    run_as_of = args.as_of or latest_run_with(runs_root, "stocks_scores.csv") or date.today().isoformat()
    run_dir = runs_root / run_as_of
    orch = cfg_get(config, "orchestration", {}) or {}
    step_timeout = float(orch.get("step_timeout_sec", 1800))
    try:
        planned = plan_groups(args, config, run_dir)
    except ValueError as exc:
        LOGGER.error("Invalid orchestration plan: %s", exc)
        return 1
    if not planned:
        LOGGER.error("Nothing to run (groups empty after skips)")
        return 1
    LOGGER.info(
        "PIPELINE as_of=%s cadence=%s groups=%s force=%s reuse_risk_price_data=%s",
        run_as_of,
        args.cadence,
        planned,
        args.force,
        args.reuse_risk_price_data,
    )
    if args.dry_run:
        for g in planned:
            for subdir, script, _m in GROUPS[g]:
                if script == "05d_audit_liquidity_panel.py" and not (run_dir / "risk" / "spread_snapshot.csv").exists():
                    LOGGER.info("  would skip %s/%s (no spread_snapshot.csv)", subdir, script)
                    continue
                flags = script_args(args, script)
                suffix = f" {' '.join(flags)}" if flags else ""
                LOGGER.info("  would run %s/%s --as-of %s%s", subdir, script, run_as_of, suffix)
        return 0

    steps: list[dict[str, Any]] = []
    failed_groups: list[str] = []
    completed_groups: list[str] = []
    for group in planned:
        group_failed = False
        for subdir, script, manifest_rel in GROUPS[group]:
            if script == "05d_audit_liquidity_panel.py" and not (run_dir / "risk" / "spread_snapshot.csv").exists():
                steps.append({
                    "group": group,
                    "script": script,
                    "rc": 0,
                    "seconds": 0.0,
                    "acceptance": "SKIPPED_NO_SPREAD_SNAPSHOT",
                    "manifest": "",
                    "manifest_sha256": "",
                    "command": [],
                    "tail": "",
                })
                LOGGER.info("[SKIP] %-45s no spread_snapshot.csv", f"{subdir}/{script}")
                continue
            cmd = [sys.executable, str(PACKAGE_ROOT / subdir / script), "--as-of", run_as_of,
                   "--config", str(config_path)]
            cmd.extend(script_args(args, script))
            started = time.monotonic()
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT),
                                      timeout=step_timeout)
                rc = proc.returncode
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-2:]
            except subprocess.TimeoutExpired:
                rc = -1
                tail = [f"timeout after {step_timeout:.0f}s"]
            elapsed = round(time.monotonic() - started, 1)
            acceptance = manifest_acceptance(run_dir, manifest_rel) if manifest_rel else ""
            stage_manifest_sha = (
                sha256_file(run_dir / manifest_rel)
                if manifest_rel and (run_dir / manifest_rel).is_file()
                else ""
            )
            steps.append({"group": group, "script": script, "rc": rc, "seconds": elapsed,
                          "acceptance": acceptance, "manifest": manifest_rel or "",
                          "manifest_sha256": stage_manifest_sha, "command": cmd,
                          "tail": " | ".join(tail) if rc != 0 else ""})
            status = "OK" if rc == 0 else "FAIL"
            LOGGER.info("[%s] %-45s rc=%d %5.1fs %s", status, f"{subdir}/{script}", rc, elapsed,
                        acceptance or "")
            acceptance_ok = acceptance == "" or acceptance.startswith("PASS")  # PASS_WITH_DEFERRED is sealed-OK
            if rc != 0 or (manifest_rel and not acceptance_ok):
                group_failed = True
                if rc == 0 and manifest_rel:
                    LOGGER.error("group %s: %s acceptance=%s", group, manifest_rel, acceptance)
                break
        if group_failed:
            failed_groups.append(group)
            if not args.continue_on_fail:
                LOGGER.error("Stopping after failed group %s (use --continue-on-fail to proceed)", group)
                break
        else:
            completed_groups.append(group)

    meta = {
        "stage": "stage12_orchestration",
        "acceptance": "PASS" if not failed_groups else "FAIL",
        "run_as_of": run_as_of,
        "cadence": args.cadence,
        "groups_planned": planned,
        "groups_completed": completed_groups,
        "groups_failed": failed_groups,
        "force": bool(args.force),
        "reuse_risk_price_data": bool(args.reuse_risk_price_data),
        "steps": steps,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "inputs_sha256": {"config.yaml": sha256_file(config_path)},
        "source_sha256": {"18_run_portfolio_pipeline.py": sha256_file(Path(__file__).resolve())},
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    write_manifest(run_dir / "orchestration_meta.json", meta)
    LOGGER.info("PIPELINE %s: %d/%d groups clean -> %s",
                "PASS" if not failed_groups else "FAIL", len(completed_groups),
                len(planned), run_dir / "orchestration_meta.json")
    return 0 if not failed_groups else 1


if __name__ == "__main__":
    raise SystemExit(main())

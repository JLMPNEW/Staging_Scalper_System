#!/usr/bin/env python3
"""Stage 12 - one-command portfolio pipeline runner (core DAG, strategic/tactical cadences).

Runs the sealed per-as-of chain in dependency order, verifying each stage's manifest acceptance
before continuing:

  scores    01 -> 02 -> 03                       (Stage 1 contract)
  risk      04 -> 05a -> 05 -> 06 -> 07 -> [05c -> 05d] -> 08
            (Stage 2 panel; 05a hydrates market instruments; 05c is an optional
             after-hours IB attempt and 05d/08 remain authoritative)
  optimizer 09 -> 10                             (Stage 3 AQR baseline)
  costs     12 -> 13 -> 14 -> 15                 (Stage 4)
  rotation  17 -> 18                             (Stage 5, shadow)
  macro     20a raw -> 20 serving -> 21 -> 22    (Stage 6, shadow)
  bl        23 -> 24 -> 25 -> 26                 (Stage 7, shadow)
  sleeves   27 -> 28 -> 29                       (Stage 8, shadow)
  ledger    31 -> 32                             (Stage 8.5; skipped unless broker imports exist)
  exits     33 -> 34 -> 35                       (Stage 9, needs ledger)
  governor  19                                   (Stage 12 bounded gross directive)
  final     20                                   (immutable deployable target weights)
  monitor   39 -> 64                             (shadow expectations + advisory levels)
  final_report 21                                (enriched target/IB/monitor report)

Cadences (config `orchestration`): `tactical` refreshes the fast loop, including rotation;
`strategic` runs every group. Stages are immutable by default; `--force` explicitly rebuilds them.
Each producer invalidates dependent seals first. IB liquidity collection (05c) is attempted by this
overnight process when enabled; connection failure is WARN-only, while 05d/08 fail closed on any
partial or invalid panel. Stage 4's explicit spread fallback covers a wholly absent panel.

Every run writes runs/<as_of>/orchestration_meta.json with per-step durations, exit codes, and the
acceptance read from each stage manifest. A narrowly scoped recovery run may select a different
basename so it cannot overwrite the original full-run provenance. Forecast and hedging stay out of
the DAG until Stage 11 promotes them; payout/final composition are implemented as shadow-aware
Stage 12 groups.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
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
GLOBAL_ORCHESTRATION_LOCK = PROJECT_ROOT / "orchestration" / ".orchestrator.lock"
MASTER_PID_ENV = "STAGING_ORCHESTRATOR_PID"


class GlobalOrchestrationCoordination:
    """Share the master lock or acquire it for a direct Tier-1 invocation.

    The cross-sector master owns this lock while it updates sector artifacts and
    passes its PID through ``MASTER_PID_ENV`` to children. A direct portfolio run
    atomically creates the same lock, preventing a master from starting midway
    through portfolio construction. Stale-lock recovery remains centralized in
    ``orchestration/run_all.py``; this lower-level runner fails closed instead of
    guessing that an existing lock is stale.
    """

    def __init__(self, path: Path = GLOBAL_ORCHESTRATION_LOCK) -> None:
        self.path = path
        self.owned = False

    @staticmethod
    def _recorded_pid(path: Path) -> int | None:
        lines: list[str] | None = None
        # The master rewrites child-PID metadata while lanes advance. On Windows
        # and OneDrive that replace can briefly deny readers; retry before treating
        # the owner as unknown or rejecting a legitimate master-launched child.
        for attempt in range(10):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
                break
            except OSError:
                if attempt == 9:
                    return None
                time.sleep(0.05)
        assert lines is not None
        for line in lines:
            if line.startswith("pid="):
                try:
                    return int(line.split("=", 1)[1].strip().split()[0])
                except ValueError:
                    return None
        return None

    def __enter__(self) -> GlobalOrchestrationCoordination:
        owner_text = os.environ.get(MASTER_PID_ENV, "").strip()
        if owner_text:
            try:
                expected_owner = int(owner_text)
            except ValueError as exc:
                raise RuntimeError(f"invalid {MASTER_PID_ENV}={owner_text!r}") from exc
            recorded_owner = self._recorded_pid(self.path)
            if recorded_owner != expected_owner:
                raise RuntimeError(
                    f"master lock ownership mismatch: env pid={expected_owner}, "
                    f"lock pid={recorded_owner}, path={self.path}"
                )
            return self

        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            fd = os.open(self.path, flags, 0o600)
        except FileExistsError as exc:
            owner = self._recorded_pid(self.path)
            raise RuntimeError(
                f"global orchestration lock is already held by pid={owner}: {self.path}; "
                "run through orchestration/run_all.py or wait for the active master"
            ) from exc
        try:
            payload = f"pid={os.getpid()} started_utc={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
            os.write(fd, payload.encode("utf-8"))
            os.fsync(fd)
            self.owned = True
        except BaseException:
            # We created the path exclusively, so no other owner can legitimately
            # replace it before this cleanup. Do not strand a malformed lock when
            # metadata persistence fails.
            self.path.unlink(missing_ok=True)
            raise
        finally:
            os.close(fd)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if not self.owned:
            return
        try:
            if self._recorded_pid(self.path) == os.getpid():
                self.path.unlink(missing_ok=True)
        finally:
            self.owned = False

# group -> ordered (subdir, script, acceptance manifest relative to the run dir | None)
GROUPS: dict[str, list[tuple[str, str, str | None]]] = {
    "scores": [
        ("scores", "01_collect_sector_scores.py", None),
        ("scores", "02_calibrate_cross_sector_scores.py", None),
        ("scores", "03_validate_score_contract.py", "manifest.json"),
    ],
    "risk": [
        ("risk", "04_check_risk_readiness.py", None),
        ("risk", "05a_hydrate_norgate_market_instruments.py", None),
        ("risk", "05_build_return_panel.py", None),
        ("risk", "06_build_risk_coverage.py", None),
        ("risk", "07_build_covariance_model.py", None),
        ("risk", "05c_collect_ib_historical_spread_samples.py", None),
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
        ("macro", "20a_run_macro_raw.py", None),
        ("macro", "20_run_macro_serving.py", None),
        ("macro", "21_build_macro_contract.py", None),
        ("macro", "22_validate_macro_contract.py", "macro/macro_manifest.json"),
    ],
    # Rebuild only the run-dir macro contract after the monitor-filtered
    # optimizer changes. Raw/serving databases were already refreshed by macro.
    "macro_contract": [
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
        (
            "orchestration",
            "20_compose_final_target_book.py",
            "final/final_weights_manifest.json",
        ),
    ],
    "earnings": [
        ("earnings_dates", "37_sync_earnings_dates.py", None),
        ("earnings_dates", "38_validate_earnings_dates.py",
         "earnings_dates/validation/earnings_validation_summary.json"),
    ],
    "monitor": [
        (
            "expectations_monitor",
            "39_sync_monitor_universe.py",
            "expectations_monitor/monitor_universe_manifest.json",
        ),
        (
            "expectations_monitor",
            "50_run_expectations_monitor_daily.py",
            "expectations_monitor/daily_monitor_manifest.json",
        ),
    ],
    "monitor_filter": [
        (
            "optimizer",
            "08_build_monitor_eligibility_overlay.py",
            "optimizer/monitor_eligibility_manifest.json",
        ),
        ("optimizer", "09_run_portfolio_optimizer.py", None),
        (
            "optimizer",
            "10_validate_optimizer_outputs.py",
            "optimizer/optimizer_manifest.json",
        ),
        ("costs", "12_build_trade_list.py", None),
        ("costs", "13_build_cost_model.py", None),
        ("costs", "14_apply_no_trade_bands.py", None),
        ("costs", "15_validate_cost_model.py", "costs/cost_manifest.json"),
    ],
    "bootstrap_final": [
        (
            "orchestration",
            "20_compose_final_target_book.py",
            "final/bootstrap_final_weights_manifest.json",
        ),
    ],
    "final_report": [
        (
            "orchestration",
            "21_enrich_final_target_book.py",
            "final/final_manifest.json",
        ),
    ],
}
# Earnings remains advisory. The monitor is now a production Stage-3 entry input,
# so its failure must stop the deployable pass rather than reuse an old state file.
SOFT_GROUPS = {"earnings"}
OPTIONAL_STEP_SCRIPTS = {"05c_collect_ib_historical_spread_samples.py"}
# Scripts in this set do not expose a --force flag. The macro wrappers are
# deterministic refresh entry points, while readiness is read-only.
NO_FORCE_SCRIPTS = {
    "04_check_risk_readiness.py",
    "20a_run_macro_raw.py",
    "20_run_macro_serving.py",
    "38_validate_earnings_dates.py",
}
DEFAULT_CADENCES = {
    "tactical": [
        "scores", "risk", "optimizer", "costs", "rotation", "governor",
        "bootstrap_final", "earnings", "monitor", "monitor_filter",
        "rotation", "macro_contract", "governor", "final", "final_report",
    ],
    "strategic": [
        "scores", "risk", "optimizer", "costs", "rotation", "macro", "governor",
        "bootstrap_final", "earnings", "monitor", "monitor_filter", "rotation",
        "macro_contract", "bl", "sleeves", "ledger", "exits", "payout",
        "governor", "final", "final_report",
    ],
}
MACRO_REFRESH_SCRIPTS = {"20a_run_macro_raw.py", "20_run_macro_serving.py"}


def terminate_process_tree(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        completed = subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode != 0 and proc.poll() is None:
            proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if proc.poll() is None:
            proc.kill()


def run_command(cmd: list[str], *, timeout: float) -> tuple[int, str, str]:
    """Run one stage and guarantee that timeout/interruption cannot orphan descendants."""
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=creationflags,
        start_new_session=os.name != "nt",
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        terminate_process_tree(proc)
        stdout, stderr = proc.communicate()
        return -1, stdout, f"{stderr}\ntimeout after {timeout:.0f}s".strip()
    except BaseException:
        terminate_process_tree(proc)
        raise
    return int(proc.returncode or 0), stdout, stderr


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
    p.add_argument(
        "--historical-catchup",
        action="store_true",
        help=(
            "Mark a past-date recovery run and suppress current provider event endpoints. "
            "Point-in-time observations already in the independent store remain consumable."
        ),
    )
    p.add_argument("--dry-run", action="store_true", help="Print the plan without executing.")
    p.add_argument("--continue-on-fail", action="store_true",
                   help="Keep running later groups after a failure (default: stop).")
    p.add_argument(
        "--orchestration-meta-name",
        default="orchestration_meta.json",
        help=(
            "Run-local orchestration manifest basename. Recovery tools use a distinct name so "
            "the original full-run manifest remains immutable."
        ),
    )
    return p.parse_args()


def script_args(
    args: argparse.Namespace, script: str, *, group: str = ""
) -> list[str]:
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
    if script == "20_run_macro_serving.py":
        flags.append("--refresh-industry-stock-foreign")
    if (
        script == "50_run_expectations_monitor_daily.py"
        and getattr(args, "historical_catchup", False)
    ):
        flags.append("--skip-event-cycle")
    if script == "09_run_portfolio_optimizer.py":
        flags.extend(
            (
                "--monitor-overlay-mode",
                "required" if group == "monitor_filter" else "ignore",
            )
        )
    if (
        script == "20_compose_final_target_book.py"
        and group == "bootstrap_final"
    ):
        flags.append("--monitor-bootstrap")
    return flags


def manifest_acceptance(run_dir: Path, rel: str) -> str:
    path = run_dir / rel
    if not path.exists():
        return "MISSING"
    try:
        manifest = read_manifest(path)
        manifest_as_of = str(
            manifest.get(
                "run_as_of",
                manifest.get(
                    "run_as_of_date",
                    manifest.get("as_of_date", manifest.get("ledger_as_of", "")),
                ),
            )
        ).strip()
        if manifest_as_of and manifest_as_of != run_dir.name:
            return f"DATE_MISMATCH:{manifest_as_of}"
        parent_path = str(manifest.get("parent_manifest_path", "")).strip()
        parent_sha = str(manifest.get("parent_manifest_sha256", "")).strip()
        if parent_path or parent_sha:
            if not parent_path or not parent_sha:
                return "INCOMPLETE_PARENT_SEAL"
            parent = Path(parent_path)
            if not parent.is_file():
                return "MISSING_PARENT"
            if sha256_file(parent) != parent_sha:
                return "STALE_PARENT"
            try:
                parent_manifest = read_manifest(parent)
            except ValueError:
                return "UNREADABLE_PARENT"
            if manifest_acceptance_value(parent_manifest) != manifest_acceptance_value(manifest):
                return "PARENT_ACCEPTANCE_MISMATCH"
            parent_as_of = str(
                parent_manifest.get(
                    "as_of_date",
                    parent_manifest.get("run_as_of", ""),
                )
            ).strip()
            if parent_as_of and parent_as_of != run_dir.name:
                return f"PARENT_DATE_MISMATCH:{parent_as_of}"
            outputs = parent_manifest.get("outputs_sha256", {})
            if not isinstance(outputs, dict) or not outputs:
                return "PARENT_OUTPUT_SEAL_MISSING"
            for name, expected in outputs.items():
                output = parent.parent / str(name)
                if not output.is_file() or sha256_file(output) != str(expected):
                    return f"STALE_PARENT_OUTPUT:{name}"
            children = parent_manifest.get("child_manifests", [])
            if not isinstance(children, list):
                return "PARENT_CHILD_SEAL_INVALID"
            for child in children:
                if not isinstance(child, dict):
                    return "PARENT_CHILD_SEAL_INVALID"
                child_path = Path(str(child.get("manifest_path", "")))
                expected = str(child.get("manifest_sha256", ""))
                if not child_path.is_file() or not expected:
                    return "PARENT_CHILD_MISSING"
                if sha256_file(child_path) != expected:
                    return f"STALE_PARENT_CHILD:{child_path.name}"
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


def run_pipeline() -> int:  # noqa: C901
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    source_path = Path(__file__).resolve()
    startup_source_sha256 = sha256_file(source_path)
    startup_config_sha256 = sha256_file(config_path)
    paths = resolve_runtime_paths(config, config_path)
    runs_root = paths.output_dir / "runs"
    run_as_of = args.as_of or latest_run_with(runs_root, "stocks_scores.csv") or date.today().isoformat()
    try:
        parsed_as_of = date.fromisoformat(run_as_of)
    except ValueError:
        LOGGER.error("--as-of must be an ISO date (YYYY-MM-DD), got %r", run_as_of)
        return 1
    if parsed_as_of.isoformat() != run_as_of:
        LOGGER.error("--as-of must use canonical YYYY-MM-DD form, got %r", run_as_of)
        return 1
    run_dir = runs_root / run_as_of
    orchestration_meta_name = str(args.orchestration_meta_name).strip()
    if (
        not orchestration_meta_name
        or Path(orchestration_meta_name).name != orchestration_meta_name
        or not orchestration_meta_name.endswith(".json")
    ):
        LOGGER.error(
            "--orchestration-meta-name must be a JSON basename, got %r",
            args.orchestration_meta_name,
        )
        return 1
    orchestration_meta_path = run_dir / orchestration_meta_name
    orch = cfg_get(config, "orchestration", {}) or {}
    step_timeout = float(orch.get("step_timeout_sec", 1800))
    macro_step_timeout = float(orch.get("macro_step_timeout_sec", 7200))
    monitor_step_timeout = float(orch.get("monitor_step_timeout_sec", 7200))
    if step_timeout <= 0 or macro_step_timeout <= 0 or monitor_step_timeout <= 0:
        LOGGER.error(
            "orchestration timeouts must be positive: step=%s macro=%s monitor=%s",
            step_timeout,
            macro_step_timeout,
            monitor_step_timeout,
        )
        return 1
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
                if (
                    script == "05d_audit_liquidity_panel.py"
                    and not (run_dir / "risk" / "spread_snapshot.csv").exists()
                    and not bool(cfg_get(config, "liquidity_panel.enhanced_intraday_enabled", False))
                ):
                    LOGGER.info("  would skip %s/%s (no spread_snapshot.csv)", subdir, script)
                    continue
                flags = script_args(args, script, group=g)
                suffix = f" {' '.join(flags)}" if flags else ""
                LOGGER.info("  would run %s/%s --as-of %s%s", subdir, script, run_as_of, suffix)
        return 0

    steps: list[dict[str, Any]] = []
    failed_groups: list[str] = []
    soft_failed_groups: list[str] = []
    completed_groups: list[str] = []
    run_dir.mkdir(parents=True, exist_ok=True)

    def persist(acceptance: str, *, active_group: str = "") -> None:
        write_manifest(
            orchestration_meta_path,
            {
                "stage": "stage12_orchestration",
                "acceptance": acceptance,
                "run_as_of": run_as_of,
                "cadence": args.cadence,
                "groups_planned": planned,
                "groups_completed": completed_groups,
                "groups_failed": failed_groups,
                "groups_soft_failed": soft_failed_groups,
                "active_group": active_group,
                "force": bool(args.force),
                "reuse_risk_price_data": bool(args.reuse_risk_price_data),
                "historical_catchup": bool(args.historical_catchup),
                "steps": steps,
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                # Freeze provenance at process start. Re-reading these files for every
                # progress write can falsely attribute newly edited source/config to an
                # already-running interpreter that loaded the previous contents.
                "inputs_sha256": {"config.yaml": startup_config_sha256},
                "source_sha256": {"18_run_portfolio_pipeline.py": startup_source_sha256},
            },
        )

    persist("RUNNING")
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
            cmd.extend(script_args(args, script, group=group))
            # Persist the step's group before launch. Long-running refreshes (notably
            # MacroLayer raw/serving) must not leave active_group pointing at the prior
            # completed group for their entire runtime.
            persist("RUNNING", active_group=group)
            started = time.monotonic()
            timeout = (
                macro_step_timeout
                if script in MACRO_REFRESH_SCRIPTS
                else monitor_step_timeout
                if script == "50_run_expectations_monitor_daily.py"
                else step_timeout
            )
            rc, stdout, stderr = run_command(cmd, timeout=timeout)
            tail = (stderr or stdout or "").strip().splitlines()[-2:]
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
            persist("RUNNING", active_group=group)
            status = "OK" if rc == 0 else "FAIL"
            LOGGER.info("[%s] %-45s rc=%d %5.1fs %s", status, f"{subdir}/{script}", rc, elapsed,
                        acceptance or "")
            if rc != 0 and script in OPTIONAL_STEP_SCRIPTS:
                warning = f"{group}:{script}"
                if warning not in soft_failed_groups:
                    soft_failed_groups.append(warning)
                LOGGER.warning(
                    "Optional step %s failed; Stage 2 validation will decide whether "
                    "an existing panel is usable or an absent panel may use fallback",
                    script,
                )
                continue
            acceptance_ok = acceptance == "" or acceptance.startswith("PASS")  # PASS_WITH_DEFERRED is sealed-OK
            if rc != 0 or (manifest_rel and not acceptance_ok):
                group_failed = True
                if rc == 0 and manifest_rel:
                    LOGGER.error("group %s: %s acceptance=%s", group, manifest_rel, acceptance)
                break
        if group_failed:
            if group in SOFT_GROUPS:
                soft_failed_groups.append(group)
                LOGGER.warning("Advisory group %s failed; WARN-only, pipeline continues", group)
            else:
                failed_groups.append(group)
                if not args.continue_on_fail:
                    LOGGER.error("Stopping after failed group %s (use --continue-on-fail to proceed)", group)
                    break
        else:
            completed_groups.append(group)

    provenance_drift: list[str] = []
    try:
        if sha256_file(source_path) != startup_source_sha256:
            provenance_drift.append("18_run_portfolio_pipeline.py changed during execution")
    except OSError as exc:
        provenance_drift.append(f"18_run_portfolio_pipeline.py unavailable at completion: {exc}")
    try:
        if sha256_file(config_path) != startup_config_sha256:
            provenance_drift.append("config.yaml changed during execution")
    except OSError as exc:
        provenance_drift.append(f"config.yaml unavailable at completion: {exc}")
    if provenance_drift:
        failed_groups.append("orchestration_integrity")
        steps.append(
            {
                "group": "orchestration_integrity",
                "script": source_path.name,
                "rc": 1,
                "seconds": 0.0,
                "acceptance": "FAIL_SOURCE_OR_CONFIG_DRIFT",
                "manifest": "",
                "manifest_sha256": "",
                "command": [],
                "tail": " | ".join(provenance_drift),
            }
        )
        LOGGER.error("Orchestration provenance drift: %s", "; ".join(provenance_drift))

    if failed_groups:
        overall = "FAIL"
    elif soft_failed_groups:
        overall = "PASS_WITH_ADVISORY_WARNINGS"
    else:
        overall = "PASS"
    persist(overall)
    LOGGER.info("PIPELINE %s: %d/%d groups clean -> %s",
                overall, len(completed_groups),
                len(planned), orchestration_meta_path)
    return 0 if not failed_groups else 1


def main() -> int:
    try:
        with GlobalOrchestrationCoordination():
            return run_pipeline()
    except (OSError, RuntimeError) as exc:
        configure_utc_logging()
        LOGGER.error("Portfolio orchestration lock failure: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

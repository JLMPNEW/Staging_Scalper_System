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
  ledger    30 -> 31 -> 32                       (Stage 8.5; skipped unless broker imports exist)
  exits     33 -> 34 -> 35 -> 36                 (Stage 9, needs ledger)
  governor  19                                   (Stage 12 bounded gross directive)
  final     20                                   (immutable deployable target weights)
  monitor   39 -> 50                             (shadow expectations + advisory levels)
  final_report 21                                (enriched target/IB/monitor report)

Cadences (config `orchestration`): `tactical` refreshes the fast loop, including rotation;
`strategic` runs every group. Both cadences intentionally run some scripts twice (the
monitor_filter re-solve of Stage 3/4, the second rotation/governor pass, and the final book
after the bootstrap book).

Immutability contract:
  * Sealed stages are immutable without an operator ``--force``.
  * A repeat occurrence of a script inside one planned run is an intentional re-pass and is
    self-forced (``--force`` appended for that step only), so a non-force run can complete a
    shipped cadence end to end.
  * A first occurrence whose gate acceptance manifest is already sealed PASS* for this as-of
    (no date mismatch, no parent/provenance drift) is skipped as ALREADY_SEALED, giving
    crash-resume without rebuilding sealed artifacts.
  * A first occurrence whose gate manifest is absent or non-PASS and whose child refuses to
    overwrite existing partial outputs is relaunched once with ``--force``: the step never
    sealed, so its partial outputs are safe to rebuild. This never applies to sealed steps.
  * ``--dry-run`` uses the same predicate functions and displays RE_PASS / RESUME_SKIP
    markers so the printed plan cannot drift from execution.
  * Without an explicit ``--as-of`` the default may only resolve to the current NYSE
    session (previous trading day on/before today). If the latest started run dir is any
    other date the run hard-errors instead of resuming it, because self-forced re-pass
    steps would rebuild that prior day's sealed final book. ``--force`` always requires
    an explicit ``--as-of``.

Each producer invalidates dependent seals first. IB liquidity collection (05c) is attempted by
this overnight process when enabled. Under the explicit connection-failure policy it may rebuild
the current snapshot from the newest stored sample partition inside the configured staleness
bound; 05d/08 still hard-fail stale rows, quote defects, incomplete universes, or excess fallback.
Without that policy (or without a usable stored partition), an overnight IB outage hard-stops the
risk group. 05d is skipped only when the panel is wholly absent AND enhanced intraday collection
is disabled; only in that configuration does Stage 4's explicit spread fallback cover the absent
panel.

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
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

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
from portfolio_layer.core.runtime_env import (  # noqa: E402
    hydrate_missing_user_environment,
)
from portfolio_layer.expectations_monitor.monitor_common import monitor_output_subdir  # noqa: E402
from portfolio_layer.ledger.ledger_common import (  # noqa: E402
    latest_sealed_ledger_run,
)
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("run_portfolio_pipeline")
# Set by run_pipeline() once the run meta path is known; main() uses it to persist
# a terminal FAIL meta when an exception escapes, so the meta never stays RUNNING.
_TERMINAL_FAIL_PERSIST: Callable[[str], None] | None = None
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
# No group is advisory. The monitor is a production Stage-3 entry input, and
# 21_enrich_final_target_book (final_report) hard-requires a same-date sealed
# earnings_calendar.csv, so an earnings failure must hard-stop the run early
# instead of resurfacing hours later at the last step. The soft-fail mechanism
# is retained for future genuinely advisory groups.
SOFT_GROUPS: set[str] = set()
OPTIONAL_STEP_SCRIPTS = {"05c_collect_ib_historical_spread_samples.py"}
# Scripts in this set do not expose a --force flag. The macro wrappers are
# deterministic refresh entry points, while readiness is read-only.
NO_FORCE_SCRIPTS = {
    "04_check_risk_readiness.py",
    "20a_run_macro_raw.py",
    "20_run_macro_serving.py",
    "38_validate_earnings_dates.py",
}
# Kept byte-identical to config.yaml `orchestration.cadences` (the shipped
# defaults). plan_groups() logs whenever a config override differs from these
# built-ins so cadence drift is visible in the run log. plan_groups() itself
# normalizes one ledger pass before the first monitor group when holdings are
# required, so ledger needs no fixed slot here beyond the config's.
DEFAULT_CADENCES = {
    "tactical": [
        "scores", "risk", "optimizer", "costs", "rotation", "governor",
        "bootstrap_final", "earnings", "monitor", "monitor_filter", "rotation",
        "macro_contract", "governor", "final", "final_report",
    ],
    "strategic": [
        "scores", "risk", "optimizer", "costs", "rotation", "macro", "governor",
        "bootstrap_final", "earnings", "monitor", "monitor_filter", "rotation",
        "macro_contract", "bl", "sleeves", "ledger", "exits", "payout",
        "governor", "final", "final_report",
    ],
}
MACRO_REFRESH_SCRIPTS = {"20a_run_macro_raw.py", "20_run_macro_serving.py"}
DEFERRED_LEDGER_POLICY = (
    "use_latest_sealed_ledger_and_defer_current_broker_groups"
)
CURRENT_BROKER_GROUPS = {"ledger", "exits", "payout"}
# The exact refusal raised by portfolio_layer.core.contracts.fail_if_exists when a
# producer meets its own prior partial outputs. Used by the partial-step recovery
# path (never for sealed steps).
OVERWRITE_REFUSAL_SIGNATURE = "Refusing to overwrite existing run artifacts without --force"


def resolved_groups(config: dict[str, Any]) -> dict[str, list[tuple[str, str, str | None]]]:
    """GROUPS with the monitor gate manifests resolved from config.

    Monitor producers and consumers resolve their run-dir output directory from
    `expectations_monitor.output_subdir` (monitor_common.monitor_output_subdir,
    default "expectations_monitor"). The orchestrator must evaluate gate
    acceptance at that same configured location instead of a hardcoded subdir.
    """
    subdir = monitor_output_subdir(config)
    groups = dict(GROUPS)
    groups["monitor"] = [
        (
            "expectations_monitor",
            "39_sync_monitor_universe.py",
            f"{subdir}/monitor_universe_manifest.json",
        ),
        (
            "expectations_monitor",
            "50_run_expectations_monitor_daily.py",
            f"{subdir}/daily_monitor_manifest.json",
        ),
    ]
    return groups


def skip_liquidity_audit(config: dict[str, Any], run_dir: Path) -> bool:
    """Single skip predicate for 05d used by BOTH dry-run and execution.

    05d is skipped only when there is no spread snapshot to audit AND the
    enhanced intraday collection is disabled; with the collector enabled the
    audit must run (and fail closed) rather than be silently skipped.
    """
    return (
        not (run_dir / "risk" / "spread_snapshot.csv").exists()
        and not bool(cfg_get(config, "liquidity_panel.enhanced_intraday_enabled", False))
    )


def build_step_plan(
    planned: list[str], groups: dict[str, list[tuple[str, str, str | None]]]
) -> list[list[dict[str, Any]]]:
    """Executed step plan, one inner list per planned group pass.

    Counts occurrences of every script basename across the WHOLE planned step
    list: any repeat occurrence is an intentional re-pass (monitor_filter
    re-solve, second rotation/governor pass, final after bootstrap_final) and is
    self-forced so a non-force run can complete a shipped cadence. Each step also
    carries its gate manifest: the step's own acceptance manifest when it has
    one, else the next acceptance manifest at-or-after it inside the group (the
    seal that certifies the step's outputs).
    """
    occurrences: dict[str, int] = {}
    plan: list[list[dict[str, Any]]] = []
    for group in planned:
        steps = groups[group]
        pass_steps: list[dict[str, Any]] = []
        for index, (subdir, script, manifest_rel) in enumerate(steps):
            gate_rel = manifest_rel
            if gate_rel is None:
                for _subdir, _script, later_rel in steps[index + 1:]:
                    if later_rel:
                        gate_rel = later_rel
                        break
            occurrences[script] = occurrences.get(script, 0) + 1
            occurrence = occurrences[script]
            if occurrence > 1 and script in NO_FORCE_SCRIPTS:
                # Cannot self-force a script without a --force flag; the macro
                # wrappers/readiness are internally idempotent so a re-pass is
                # still safe, but surface the combination explicitly.
                LOGGER.warning(
                    "re-pass of %s cannot be self-forced (NO_FORCE_SCRIPTS); "
                    "relying on the script's own idempotency",
                    script,
                )
            pass_steps.append(
                {
                    "group": group,
                    "subdir": subdir,
                    "script": script,
                    "manifest_rel": manifest_rel,
                    "gate_rel": gate_rel,
                    "occurrence": occurrence,
                    "self_force": occurrence > 1 and script not in NO_FORCE_SCRIPTS,
                }
            )
        plan.append(pass_steps)
    return plan


def step_resume_skip(run_dir: Path, step: dict[str, Any], *, operator_force: bool) -> bool:
    """Skip-if-sealed resume predicate used by BOTH dry-run and execution.

    A FIRST occurrence without operator --force is skipped when its gate
    acceptance manifest is already sealed PASS* for this run dir.
    manifest_acceptance() fail-closes on as-of mismatch and parent/provenance
    drift (returns DATE_MISMATCH/STALE_PARENT/... rather than PASS*), so a PASS*
    result here certifies a same-date, drift-free seal. Re-pass occurrences are
    never skipped: they exist to rebuild on refreshed inputs.
    """
    if operator_force or int(step["occurrence"]) > 1:
        return False
    gate_rel = step["gate_rel"]
    if not gate_rel:
        return False
    return manifest_acceptance(run_dir, str(gate_rel)).startswith("PASS")


def _previous_nyse_trading_day(today: date) -> date:
    """Latest plausible NYSE session on or before `today`.

    The repo's authoritative NYSE holiday calendar lives in
    orchestration/run_all.py, which is a script directory (not an importable
    package), so it is loaded by file path. When that load is impossible the
    fallback is weekday-only stepping: strictly better than raw date.today() on
    weekends, and a holiday default still requires an explicit --as-of.
    """
    is_trading_day: Callable[[date], bool] | None = None
    spec_name = "_staging_run_all_calendar"
    try:
        import importlib.util

        run_all_path = PROJECT_ROOT / "orchestration" / "run_all.py"
        spec = importlib.util.spec_from_file_location(spec_name, run_all_path)
        if spec is not None and spec.loader is not None:
            module = importlib.util.module_from_spec(spec)
            # run_all defines dataclasses, whose creation resolves the owning
            # module through sys.modules; register before exec, drop after.
            sys.modules[spec_name] = module
            try:
                spec.loader.exec_module(module)
                is_trading_day = module.is_trading_day
            finally:
                sys.modules.pop(spec_name, None)
    except (ImportError, OSError, AttributeError, SyntaxError, SystemExit):
        is_trading_day = None
    candidate = today
    if is_trading_day is None:
        # Weekday-only fallback (explicit): NYSE holiday awareness could not be
        # loaded from orchestration/run_all.py.
        while candidate.weekday() >= 5:
            candidate -= timedelta(days=1)
        return candidate
    while not is_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def default_run_as_of(runs_root: Path, *, today: date | None = None) -> str:
    """Fail-closed default as-of when the operator passes no ``--as-of``.

    Re-pass occurrences (second rotation/governor pass, final, final_report,
    the monitor_filter re-solve) are self-forced by design, so letting a bare
    run default into an OLDER existing run dir would rebuild that prior day's
    sealed second-pass artifacts without an operator ``--force``. The default
    may therefore only be the current NYSE session (previous trading day
    on/before today): resuming that session is allowed; if the latest started
    run dir is any other date this raises ValueError instructing the operator
    to pass an explicit ``--as-of``.
    """
    calendar_default = _previous_nyse_trading_day(today or date.today()).isoformat()
    latest_started = latest_run_with(runs_root, "stocks_scores.csv")
    if latest_started is not None and latest_started != calendar_default:
        raise ValueError(
            f"No --as-of given and the latest started run dir ({latest_started}) is not "
            f"the current NYSE session ({calendar_default}). A bare default would resume "
            f"{latest_started} and its self-forced re-pass steps would rebuild that day's "
            f"sealed final book. Pass --as-of {calendar_default} to run the current "
            f"session, or --as-of {latest_started} (with --force for sealed steps) to "
            "intentionally rework the old run."
        )
    return calendar_default


def _windows_descendant_pids(root_pid: int) -> list[int]:
    """Best-effort transitive child PIDs of `root_pid`, deepest-first.

    Used only when `taskkill /T /F` fails: killing just the direct child would
    orphan grandchildren that may still hold SQLite locks. Enumerates the
    process table via CIM (PowerShell) with a WMIC fallback; any failure returns
    an empty list (the caller still calls proc.kill()).
    """
    pid_parent: list[tuple[int, int]] = []
    source = ""
    try:
        completed = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                "Get-CimInstance Win32_Process | ForEach-Object "
                "{ \"$($_.ProcessId) $($_.ParentProcessId)\" }",
            ],
            capture_output=True, text=True, timeout=20, check=False,
        )
        if completed.returncode == 0:
            source = completed.stdout
    except (OSError, subprocess.SubprocessError):
        source = ""
    if source:
        for line in source.splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            try:
                pid_parent.append((int(parts[0]), int(parts[1])))
            except ValueError:
                continue
    else:
        try:
            completed = subprocess.run(
                ["wmic", "process", "get", "ProcessId,ParentProcessId"],
                capture_output=True, text=True, timeout=20, check=False,
            )
            if completed.returncode != 0:
                return []
        except (OSError, subprocess.SubprocessError):
            return []
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        if not lines:
            return []
        # WMIC prints columns alphabetically regardless of the requested order;
        # map them from the header row.
        header = lines[0].split()
        try:
            pid_col = header.index("ProcessId")
            parent_col = header.index("ParentProcessId")
        except ValueError:
            return []
        for line in lines[1:]:
            parts = line.split()
            if len(parts) != len(header):
                continue
            try:
                pid_parent.append((int(parts[pid_col]), int(parts[parent_col])))
            except ValueError:
                continue
    children: dict[int, list[int]] = {}
    for pid, parent in pid_parent:
        children.setdefault(parent, []).append(pid)
    ordered: list[int] = []
    frontier = [root_pid]
    seen: set[int] = set()
    while frontier:
        next_frontier: list[int] = []
        for parent in frontier:
            for child in children.get(parent, []):
                if child not in seen:
                    seen.add(child)
                    ordered.append(child)
                    next_frontier.append(child)
        frontier = next_frontier
    return list(reversed(ordered))  # leaves first


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
            # taskkill /T failed: proc.kill() alone would orphan grandchildren
            # (which may hold SQLite locks). Kill enumerated descendants
            # leaves-first, then the direct child.
            for pid in _windows_descendant_pids(proc.pid):
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            if proc.poll() is None:
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
    p.add_argument(
        "--as-of",
        default=None,
        help=(
            "Run as-of date (default: latest run folder, else the previous NYSE trading day). "
            "REQUIRED with --force: a forced rebuild must name the run it destroys."
        ),
    )
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
    p.add_argument(
        "--late-holding-supplement",
        action="store_true",
        help=(
            "Late IB statement only: supplement newly held names while preserving "
            "first-write monitor/levels evidence for previously published names."
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
    args: argparse.Namespace, script: str, *, group: str = "", self_force: bool = False
) -> list[str]:
    """Return the exact optional flags for a stage script.

    Dry-run and execution both call this function so the displayed plan cannot drift from the
    command that is actually launched. `self_force` marks an intentional re-pass occurrence
    (see build_step_plan): the step gets --force even without operator --force, because the
    re-pass exists to rebuild on refreshed inputs. Operator-only semantics
    (--reuse-sealed-run-raw) stay tied to the operator flag.
    """
    flags: list[str] = []
    if (args.force or self_force) and script not in NO_FORCE_SCRIPTS:
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
    if (
        script == "50_run_expectations_monitor_daily.py"
        and getattr(args, "late_holding_supplement", False)
    ):
        flags.append("--late-holding-supplement")
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


def _sealed_ledger_available(run_dir: Path) -> bool:
    try:
        selected, age_days, _skipped = latest_sealed_ledger_run(
            run_dir.parent,
            run_dir.name,
            max_staleness_days=0,
        )
    except (FileNotFoundError, ValueError, OSError):
        return False
    return selected.resolve() == run_dir.resolve() and age_days == 0


def _configured_max_ledger_staleness(config: dict[str, Any]) -> int:
    raw = cfg_get(config, "holdings_ledger.max_staleness_days", 7)
    try:
        value = int(str(raw))
    except ValueError as exc:
        raise ValueError(
            f"holdings_ledger.max_staleness_days must be an integer, got {raw!r}"
        ) from exc
    if value < 0:
        raise ValueError(
            f"holdings_ledger.max_staleness_days must be >= 0, got {value}"
        )
    return value


def plan_groups_with_metadata(
    args: argparse.Namespace,
    config: dict[str, Any],
    run_dir: Path,
) -> tuple[list[str], dict[str, Any]]:
    orch = cfg_get(config, "orchestration", {}) or {}
    overrides = orch.get("cadences") or {}
    for name, override in overrides.items():
        if not isinstance(override, (list, tuple)):
            continue
        normalized = [str(group).strip() for group in override if str(group).strip()]
        default = DEFAULT_CADENCES.get(str(name))
        if default is not None and normalized != default:
            LOGGER.info(
                "cadence %r config override differs from built-in default: config=%s default=%s",
                name, normalized, default,
            )
    cadences = {**DEFAULT_CADENCES, **overrides}
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
    requested_after_skip = list(planned)
    monitor_planned = bool({"monitor", "monitor_filter"} & set(planned))
    require_holdings = bool(
        cfg_get(
            config,
            "expectations_monitor.universe.require_broker_holdings",
            True,
        )
    )
    ledger_required = monitor_planned and require_holdings
    ledger_ready = _sealed_ledger_available(run_dir)
    config_path = Path(args.config).expanduser().resolve()
    statement_available = _broker_statement_available(
        config,
        config_path,
        run_dir.name,
    )
    policy = str(
        cfg_get(
            config,
            "holdings_ledger.missing_same_date_statement_policy",
            "fail",
        )
    ).strip()
    selected_ledger_as_of = run_dir.name if ledger_ready else ""
    selected_ledger_age_days: int | None = 0 if ledger_ready else None
    skipped_ledger_candidates: list[dict[str, str]] = []
    fallback_error = ""

    if ledger_required and not ledger_ready:
        if statement_available and "ledger" in skip:
            raise ValueError(
                "expectations monitor requires same-date broker holdings, "
                "but group ledger was explicitly skipped"
            )
        if statement_available:
            # Custom group lists and configured cadences cannot place a required
            # source after its consumer. Normalize one ledger pass immediately
            # before the first monitor group.
            planned = [group for group in planned if group != "ledger"]
            monitor_index = min(
                index
                for index, group in enumerate(planned)
                if group in {"monitor", "monitor_filter"}
            )
            planned.insert(monitor_index, "ledger")

    needs_prior_ledger = (
        not ledger_ready
        and not statement_available
        and (
            ledger_required
            or "final_report" in planned
            or bool(CURRENT_BROKER_GROUPS & set(planned))
        )
    )
    if needs_prior_ledger:
        if policy != DEFERRED_LEDGER_POLICY:
            raise ValueError(
                "expectations monitor requires same-date broker holdings, but no "
                f"sealed ledger or IB statement exists for {run_dir.name}; "
                f"missing_same_date_statement_policy={policy or 'MISSING'}"
            )
        try:
            selected, selected_ledger_age_days, skipped_ledger_candidates = (
                latest_sealed_ledger_run(
                    run_dir.parent,
                    run_dir.name,
                    max_staleness_days=_configured_max_ledger_staleness(config),
                )
            )
            selected_ledger_as_of = selected.name
        except (FileNotFoundError, ValueError, OSError) as exc:
            fallback_error = f"{type(exc).__name__}: {exc}"
            raise ValueError(
                f"Same-date broker data is unavailable for {run_dir.name}, and "
                f"no bounded hash-verified prior ledger can support the monitor: {exc}"
            ) from exc
        planned = [
            group for group in planned if group not in CURRENT_BROKER_GROUPS
        ]
        LOGGER.warning(
            "No same-date IB statement for %s: monitor/final report will use "
            "sealed ledger %s (age=%s days); deferred groups=%s",
            run_dir.name,
            selected_ledger_as_of,
            selected_ledger_age_days,
            sorted(CURRENT_BROKER_GROUPS & set(requested_after_skip)),
        )

    deferred_groups = [
        group
        for group in requested_after_skip
        if group in CURRENT_BROKER_GROUPS and group not in planned
    ]
    metadata: dict[str, Any] = {
        "missing_same_date_statement_policy": policy,
        "same_date_statement_available": statement_available,
        "same_date_ledger_available": ledger_ready,
        "broker_holdings_source_as_of": selected_ledger_as_of,
        "broker_holdings_age_days": selected_ledger_age_days,
        "deferred_groups": deferred_groups,
        "skipped_ledger_candidates": skipped_ledger_candidates,
        "fallback_error": fallback_error,
        "requested_groups_after_skip": requested_after_skip,
    }
    return planned, metadata


def plan_groups(args: argparse.Namespace, config: dict[str, Any], run_dir: Path) -> list[str]:
    planned, _metadata = plan_groups_with_metadata(args, config, run_dir)
    return planned


def configured_runtime_command(
    config: dict[str, Any],
    *,
    argv: list[str] | None = None,
    current_executable: str | Path | None = None,
) -> list[str] | None:
    """Return a deterministic self-relaunch command when the runtime is wrong."""
    raw = str(cfg_get(config, "orchestration.python_executable", "")).strip()
    if not raw:
        return None
    configured = Path(os.path.expandvars(raw)).expanduser()
    if not configured.is_absolute():
        configured = PROJECT_ROOT / configured
    configured = configured.resolve()
    if not configured.is_file():
        raise FileNotFoundError(
            f"Configured orchestration.python_executable does not exist: {configured}"
        )
    current = Path(current_executable or sys.executable).expanduser().resolve()
    try:
        same_runtime = os.path.samefile(configured, current)
    except OSError:
        same_runtime = os.path.normcase(str(configured)) == os.path.normcase(str(current))
    if same_runtime:
        return None
    return [
        str(configured),
        str(Path(__file__).resolve()),
        *(list(sys.argv[1:]) if argv is None else argv),
    ]


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
    if args.force and not args.as_of:
        # A forced rebuild invalidates and rebuilds sealed artifacts, so it must
        # name its target explicitly rather than inherit any default (a Monday
        # --force defaulting into Friday would destroy Friday's sealed book).
        LOGGER.error(
            "--force requires an explicit --as-of: a forced run invalidates and rebuilds "
            "sealed artifacts, so it must name its target run dir. "
            "Pass --as-of YYYY-MM-DD for the run you intend to rebuild."
        )
        return 1
    if args.as_of:
        run_as_of = str(args.as_of)
    else:
        try:
            run_as_of = default_run_as_of(runs_root)
        except ValueError as exc:
            LOGGER.error("%s", exc)
            return 1
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
    if args.late_holding_supplement and (
        not args.force
        or not args.groups
        or not orchestration_meta_name.startswith("late_statement_")
    ):
        LOGGER.error(
            "--late-holding-supplement requires --force, explicit --groups, and a "
            "late_statement_*.json recovery manifest"
        )
        return 1
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
        planned, broker_dependency = plan_groups_with_metadata(
            args, config, run_dir
        )
        groups_map = resolved_groups(config)
    except ValueError as exc:
        LOGGER.error("Invalid orchestration plan: %s", exc)
        return 1
    if not planned:
        LOGGER.error("Nothing to run (groups empty after skips)")
        return 1
    step_plan = build_step_plan(planned, groups_map)
    LOGGER.info(
        "PIPELINE as_of=%s cadence=%s groups=%s force=%s reuse_risk_price_data=%s",
        run_as_of,
        args.cadence,
        planned,
        args.force,
        args.reuse_risk_price_data,
    )
    if args.dry_run:
        # Same predicate functions as execution (skip_liquidity_audit,
        # step_resume_skip, script_args) so the plan cannot drift from it.
        for pass_steps in step_plan:
            for step in pass_steps:
                subdir, script = step["subdir"], step["script"]
                if script == "05d_audit_liquidity_panel.py" and skip_liquidity_audit(config, run_dir):
                    LOGGER.info(
                        "  would skip %s/%s (no spread_snapshot.csv, enhanced intraday disabled)",
                        subdir, script,
                    )
                    continue
                if step_resume_skip(run_dir, step, operator_force=args.force):
                    LOGGER.info(
                        "  RESUME_SKIP %s/%s (gate %s sealed PASS*)",
                        subdir, script, step["gate_rel"],
                    )
                    continue
                flags = script_args(args, script, group=step["group"], self_force=step["self_force"])
                suffix = f" {' '.join(flags)}" if flags else ""
                marker = " [RE_PASS]" if step["self_force"] else ""
                LOGGER.info("  would run %s/%s --as-of %s%s%s", subdir, script, run_as_of, suffix, marker)
        return 0

    steps: list[dict[str, Any]] = []
    failed_groups: list[str] = []
    soft_failed_groups: list[str] = []
    completed_groups: list[str] = []
    run_dir.mkdir(parents=True, exist_ok=True)

    def persist(acceptance: str, *, active_group: str = "", error: str = "") -> None:
        payload: dict[str, Any] = {
            "stage": "stage12_orchestration",
            "acceptance": acceptance,
            "run_as_of": run_as_of,
            "cadence": args.cadence,
            "groups_planned": planned,
            "groups_requested": broker_dependency["requested_groups_after_skip"],
            "groups_completed": completed_groups,
            "groups_failed": failed_groups,
            "groups_soft_failed": soft_failed_groups,
            "groups_deferred": broker_dependency["deferred_groups"],
            "broker_dependency": broker_dependency,
            "active_group": active_group,
            "force": bool(args.force),
            "reuse_risk_price_data": bool(args.reuse_risk_price_data),
            "historical_catchup": bool(args.historical_catchup),
            "late_holding_supplement": bool(args.late_holding_supplement),
            "steps": steps,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            # Freeze provenance at process start. Re-reading these files for every
            # progress write can falsely attribute newly edited source/config to an
            # already-running interpreter that loaded the previous contents.
            "inputs_sha256": {"config.yaml": startup_config_sha256},
            "source_sha256": {"18_run_portfolio_pipeline.py": startup_source_sha256},
        }
        if error:
            payload["error"] = error
        write_manifest(orchestration_meta_path, payload)

    # Terminal-failure hook: main() persists a FAIL meta (with the error string)
    # if any exception escapes this function, so the meta can never stay frozen
    # at acceptance=RUNNING after a crash/interrupt.
    def _persist_terminal_fail(message: str) -> None:
        persist("FAIL", error=message)

    global _TERMINAL_FAIL_PERSIST
    _TERMINAL_FAIL_PERSIST = _persist_terminal_fail

    persist("RUNNING")
    for pass_steps in step_plan:
        if not pass_steps:
            continue
        group = str(pass_steps[0]["group"])
        group_failed = False
        failed_script = ""
        for step in pass_steps:
            subdir = str(step["subdir"])
            script = str(step["script"])
            manifest_rel = step["manifest_rel"]
            gate_rel = step["gate_rel"]
            if script == "05d_audit_liquidity_panel.py" and skip_liquidity_audit(config, run_dir):
                steps.append({
                    "group": group,
                    "script": script,
                    "rc": 0,
                    "seconds": 0.0,
                    "status": "SKIPPED_NO_SPREAD_SNAPSHOT",
                    "acceptance": "SKIPPED_NO_SPREAD_SNAPSHOT",
                    "manifest": "",
                    "manifest_sha256": "",
                    "command": [],
                    "tail": "",
                })
                LOGGER.info("[SKIP] %-45s no spread_snapshot.csv", f"{subdir}/{script}")
                continue
            if step_resume_skip(run_dir, step, operator_force=args.force):
                sealed_acceptance = manifest_acceptance(run_dir, str(gate_rel))
                steps.append({
                    "group": group,
                    "script": script,
                    "rc": 0,
                    "seconds": 0.0,
                    "status": "ALREADY_SEALED",
                    "acceptance": sealed_acceptance,
                    "manifest": str(gate_rel),
                    "manifest_sha256": (
                        sha256_file(run_dir / str(gate_rel))
                        if (run_dir / str(gate_rel)).is_file()
                        else ""
                    ),
                    "command": [],
                    "tail": "",
                })
                LOGGER.info("[SEALED] %-45s gate=%s %s (resume skip)",
                            f"{subdir}/{script}", gate_rel, sealed_acceptance)
                continue
            cmd = [sys.executable, str(PACKAGE_ROOT / subdir / script), "--as-of", run_as_of,
                   "--config", str(config_path)]
            cmd.extend(script_args(args, script, group=group, self_force=bool(step["self_force"])))
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
            recovered_with_force = False
            if rc != 0 and int(step["occurrence"]) == 1 and not args.force:
                # Partial-step recovery: the step's gate never sealed, so its
                # partial outputs (from an earlier crashed/aborted run) are safe
                # to rebuild. A sealed gate never reaches here: the step would
                # have been resume-skipped above.
                gate_sealed = bool(gate_rel) and manifest_acceptance(
                    run_dir, str(gate_rel)
                ).startswith("PASS")
                if (
                    not gate_sealed
                    and script not in NO_FORCE_SCRIPTS
                    and OVERWRITE_REFUSAL_SIGNATURE in f"{stdout}\n{stderr}"
                ):
                    LOGGER.warning(
                        "Partial-step recovery: %s/%s refused to overwrite prior partial "
                        "outputs and gate %s is not sealed PASS; relaunching once with --force",
                        subdir, script, gate_rel or "<none>",
                    )
                    cmd = [*cmd, "--force"]
                    rc, stdout, stderr = run_command(cmd, timeout=timeout)
                    recovered_with_force = True
            tail = (stderr or stdout or "").strip().splitlines()[-2:]
            elapsed = round(time.monotonic() - started, 1)
            acceptance = manifest_acceptance(run_dir, manifest_rel) if manifest_rel else ""
            stage_manifest_sha = (
                sha256_file(run_dir / manifest_rel)
                if manifest_rel and (run_dir / manifest_rel).is_file()
                else ""
            )
            status = "OK" if rc == 0 else "FAIL"
            steps.append({"group": group, "script": script, "rc": rc, "seconds": elapsed,
                          "status": status,
                          "acceptance": acceptance, "manifest": manifest_rel or "",
                          "manifest_sha256": stage_manifest_sha, "command": cmd,
                          "recovered_with_force": recovered_with_force,
                          "tail": " | ".join(tail) if rc != 0 else ""})
            persist("RUNNING", active_group=group)
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
                failed_script = script
                if rc == 0 and manifest_rel:
                    LOGGER.error("group %s: %s acceptance=%s", group, manifest_rel, acceptance)
                break
        if group_failed:
            if group in SOFT_GROUPS:
                soft_tag = f"{group}:{failed_script}"
                if soft_tag not in soft_failed_groups:
                    soft_failed_groups.append(soft_tag)
                LOGGER.warning("Advisory group %s failed; WARN-only, pipeline continues", group)
            else:
                if group not in failed_groups:
                    failed_groups.append(group)
                # A group that passed an earlier pass but failed this one is
                # failed-only: never list it in both completed and failed.
                while group in completed_groups:
                    completed_groups.remove(group)
                if not args.continue_on_fail:
                    LOGGER.error("Stopping after failed group %s (use --continue-on-fail to proceed)", group)
                    break
        else:
            if group not in completed_groups and group not in failed_groups:
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
    elif broker_dependency["deferred_groups"]:
        overall = "PASS_WITH_DEFERRED"
    else:
        overall = "PASS"
    persist(overall)
    _TERMINAL_FAIL_PERSIST = None  # terminal acceptance persisted; disarm the crash hook
    LOGGER.info("PIPELINE %s: %d/%d groups clean -> %s",
                overall, len(completed_groups),
                len(planned), orchestration_meta_path)
    return 0 if not failed_groups else 1


def main() -> int:
    configure_utc_logging()
    hydrated = hydrate_missing_user_environment()
    if hydrated:
        LOGGER.info(
            "Hydrated %d missing variable(s) from local Windows user scope: %s",
            len(hydrated),
            ", ".join(hydrated),
        )
    startup_args = parse_args()
    startup_config_path = startup_args.config.expanduser().resolve()
    try:
        startup_config = load_yaml(startup_config_path)
        runtime_command = configured_runtime_command(startup_config)
    except (FileNotFoundError, OSError, ValueError) as exc:
        LOGGER.error("Portfolio orchestration runtime configuration failure: %s", exc)
        return 1
    if runtime_command is not None:
        LOGGER.info(
            "Re-launching portfolio pipeline with configured runtime: %s",
            runtime_command[0],
        )
        completed = subprocess.run(
            runtime_command,
            cwd=str(PROJECT_ROOT),
            check=False,
        )
        return int(completed.returncode)

    coordination = GlobalOrchestrationCoordination()
    # Narrow lock-failure handling to lock ACQUISITION only: any failure inside
    # run_pipeline() must not be mislabeled as a lock failure.
    try:
        coordination.__enter__()
    except (OSError, RuntimeError) as exc:
        LOGGER.error("Portfolio orchestration lock failure: %s", exc)
        return 1
    try:
        return run_pipeline()
    except SystemExit:
        raise  # argparse --help/usage errors keep their exit semantics
    except BaseException as exc:  # noqa: BLE001 - meta must not stay RUNNING
        LOGGER.error("Portfolio orchestration failed: %s: %s", type(exc).__name__, exc)
        persist_fail = _TERMINAL_FAIL_PERSIST
        if persist_fail is not None:
            try:
                persist_fail(f"{type(exc).__name__}: {exc}")
            except Exception as persist_exc:  # noqa: BLE001 - best effort only
                LOGGER.error("Could not persist terminal FAIL orchestration meta: %s", persist_exc)
        return 1
    finally:
        coordination.__exit__(None, None, None)


if __name__ == "__main__":
    raise SystemExit(main())

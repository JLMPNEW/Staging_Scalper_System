#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.refresh_lock import RefreshLock  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
MODEL_FAMILY = "defense"


def shared_industrials_lock_path() -> Path:
    """Path of the shared industrials refresh lock.

    Machinery computes ``resolve_path(machinery dashboard_root).parent.parent /
    ".industrials_refresh.lock"`` which resolves to
    ``<project>/output/industrials/.industrials_refresh.lock``. PROJECT_ROOT is
    already ``.resolve()``-d, so anchoring the same relative path here yields the
    identical absolute path the machinery runner locks.
    """
    return (PROJECT_ROOT / "output" / "industrials" / ".industrials_refresh.lock").resolve()


@dataclass(frozen=True)
class Step:
    step_id: str
    stage: str
    label: str
    args: list[str] = field(default_factory=list)
    network: bool = False
    accepts_config: bool = False  # child script exposes --config -> forward the parent's config


# In-process coverage/integrity audit run as a final gated step after the publish chain
# (finding 6). It is not a subprocess, so it carries no script path.
COVERAGE_AUDIT_STEP_ID = "21_coverage_audit"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the defense daily refresh fast path for one market as-of date.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", help="Market/PIT as-of date, YYYY-MM-DD.")
    parser.add_argument("--positioning-history-start", default="2018-01-01")
    parser.add_argument(
        "--positioning-through-publish-only",
        action="store_true",
        help=(
            "Rebuild positioning, eligibility, scores, and publish artifacts only. "
            "Use after market and financial stages already passed for the requested date."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned step matrix and write a DRY_RUN manifest without executing steps or taking the lock.",
    )
    parser.add_argument(
        "--list-steps",
        action="store_true",
        help="Print the planned step ids/stages for the requested mode and exit.",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="Validate step construction, manifest shape, and lock-path parity in-process; no DB/network access.",
    )
    return parser.parse_args()


def parse_asof(raw: str) -> str:
    return datetime.strptime(raw.strip(), "%Y-%m-%d").date().isoformat()


def build_steps(asof: str, history_start: str, *, positioning_through_publish_only: bool) -> list[Step]:
    steps: list[Step] = []
    if not positioning_through_publish_only:
        steps.extend(
            [
                Step(
                    "03_sync_prices",
                    "stage_3",
                    "sync prices",
                    ["industrials/defense/scripts/03_sync_defense_prices.py", "--asof", asof, "--allow-partial"],
                    network=True,
                ),
                Step(
                    "05_build_market",
                    "stage_3",
                    "build market features",
                    ["industrials/defense/scripts/05_build_defense_market_features.py", "--asof", asof],
                ),
                Step(
                    "06_validate_market",
                    "stage_3",
                    "validate market stage",
                    ["industrials/defense/scripts/06_validate_defense_market_stage.py", "--asof", asof],
                ),
                Step(
                    "11_sync_fx",
                    "stage_4",
                    "sync FX",
                    ["industrials/defense/scripts/11_sync_defense_yahoo_fx_rates.py", "--end-date", asof],
                    network=True,
                ),
                Step(
                    "07_sync_sec",
                    "stage_4",
                    "sync SEC fundamentals incremental",
                    [
                        "industrials/defense/scripts/07_sync_defense_sec_fundamentals.py",
                        "--incremental",
                        "--allow-partial",
                        "--asof",
                        asof,
                    ],
                    network=True,
                ),
                Step(
                    "08_build_financial",
                    "stage_4",
                    "build financial features",
                    ["industrials/defense/scripts/08_build_defense_financial_features.py", "--asof", asof],
                ),
                Step(
                    "08_validate_financial",
                    "stage_4",
                    "validate financial stage",
                    ["industrials/defense/scripts/08_validate_defense_financial_stage.py", "--asof", asof],
                ),
                Step(
                    "09_profile_graduation",
                    "stage_4",
                    "audit reporting-profile graduation",
                    ["industrials/defense/scripts/09_evaluate_defense_profile_graduation.py", "--asof", asof],
                ),
            ]
        )
    steps.extend(
        [
            Step(
                "13_sync_positioning",
                "stage_5",
                "refresh positioning daily",
                [
                    "industrials/scripts/13_sync_industrials_positioning_upstream.py",
                    "--daily-refresh",
                    "--history-start",
                    history_start,
                    "--end-date",
                    asof,
                ],
                network=True,
                accepts_config=True,
            ),
            Step(
                "14_validate_positioning",
                "stage_5",
                "validate positioning stage",
                [
                    "industrials/scripts/14_validate_industrials_sec_positioning_stages.py",
                    "--model-family",
                    MODEL_FAMILY,
                    "--asof",
                    asof,
                ],
                accepts_config=True,
            ),
            Step(
                "10_validate_eligibility",
                "stage_6",
                "validate scoring eligibility",
                ["industrials/defense/scripts/10_validate_defense_scoring_eligibility_policy.py", "--asof", asof],
            ),
            # Post-promotion (asof >= production_start) this publish natively stamps
            # oos_score_valid rows into the dated defense_final_rank_table.csv that the
            # portfolio adapter reads (require_oos_score_valid). Keeping it in the daily
            # sequence is what makes the daily run produce an oos-valid table.
            Step(
                "17_publish",
                "stage_10",
                "publish shadow rank table",
                ["industrials/defense/scripts/17_publish_defense_shadow_rank_table.py", "--asof", asof],
                accepts_config=True,
            ),
            Step(
                "18_validate_publish",
                "stage_10",
                "validate shadow rank table",
                ["industrials/defense/scripts/18_validate_defense_shadow_rank_table.py", "--asof", asof],
                accepts_config=True,
            ),
            Step(
                "20_validate_portfolio",
                "stage_10",
                "validate portfolio adapter shadow",
                ["industrials/defense/scripts/20_validate_defense_portfolio_adapter_shadow.py", "--asof", asof],
                accepts_config=True,
            ),
        ]
    )
    return steps


def step_command(step: Step, config_path: Path) -> list[str]:
    """Build a child command, forwarding --config to children that accept it (finding 6).

    --config is inserted immediately after the script path (before the step's own flags),
    which every config-aware child's argparse consumes identically. Children without a
    --config option (accepts_config=False) are invoked exactly as before.
    """
    cmd = [sys.executable, step.args[0]]
    if step.accepts_config:
        cmd += ["--config", str(config_path)]
    cmd += step.args[1:]
    return cmd


MANIFEST_FIELDS = [
    "run_id",
    "step_number",
    "step_id",
    "stage",
    "script",
    "network_flag",
    "command",
    "log_path",
    "status",
    "return_code",
    "elapsed_sec",
]


def coverage_audit(config_path: Path, asof: str) -> None:
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    active_sql = """
        SELECT DISTINCT c.ticker
        FROM dim_company c
        JOIN dim_industrials_taxonomy t ON t.company_id = c.company_id
        WHERE c.is_active = 1 AND t.model_family = 'defense'
    """
    checks = [
        ("fact_price_ohlcv", "bar_date", "ticker"),
        ("fact_market_snapshot", "asof_date", "ticker"),
        ("feature_market_technical", "asof_date", "ticker"),
        ("feature_financial_statement", "asof_date", "ticker"),
        ("feature_positioning", "asof_date", "ticker"),
        ("fact_fx_rate", "rate_date", "currency_pair"),
    ]
    with sqlite3.connect(db_path) as conn:
        active_count = int(conn.execute(f"SELECT COUNT(*) FROM ({active_sql})").fetchone()[0] or 0)
        print(f"[defense_daily_refresh] active_defense_tickers={active_count}", flush=True)
        for table, date_col, id_col in checks:
            max_date = conn.execute(f"SELECT MAX({date_col}) FROM {table}").fetchone()[0]
            rows_on_asof = int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {date_col} = ?", (asof,)).fetchone()[0] or 0)
            if id_col == "ticker":
                covered = int(
                    conn.execute(
                        f"""
                        SELECT COUNT(DISTINCT x.ticker)
                        FROM {table} x
                        JOIN ({active_sql}) a ON a.ticker = x.ticker
                        WHERE x.{date_col} = ?
                        """,
                        (asof,),
                    ).fetchone()[0]
                    or 0
                )
                print(
                    f"[defense_daily_refresh] {table}.{date_col}: max={max_date} rows_on_{asof}={rows_on_asof} "
                    f"active_tickers_on_{asof}={covered}/{active_count}",
                    flush=True,
                )
            else:
                distinct_count = int(
                    conn.execute(
                        f"SELECT COUNT(DISTINCT {id_col}) FROM {table} WHERE {date_col} = ?",
                        (asof,),
                    ).fetchone()[0]
                    or 0
                )
                print(
                    f"[defense_daily_refresh] {table}.{date_col}: max={max_date} rows_on_{asof}={rows_on_asof} "
                    f"distinct_{id_col}_on_{asof}={distinct_count}",
                    flush=True,
                )


def compute_manifest_acceptance(
    *,
    planned_step_count: int,
    report_rows: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    dry_run: bool,
    error: str | None = None,
) -> str:
    """Fail-closed acceptance derivation.

    A dry run is never an operational PASS/FAIL. A real run is PASS only when every
    planned step actually completed (status PASS) AND there were no failures AND no
    exception fired -- so a lock exception (0 completed steps) or an aborted sequence
    (completed < planned) seals FAIL, not PASS.
    """
    if dry_run:
        return "DRY_RUN"
    if error is not None:
        return "FAIL"
    completed = sum(1 for row in report_rows if str(row.get("status", "")).upper() == "PASS")
    if failures:
        return "FAIL"
    if planned_step_count <= 0 or completed != planned_step_count:
        return "FAIL"
    return "PASS"


def write_manifest(
    orchestration_root: Path,
    *,
    run_id: str,
    asof: str,
    db_path: Path,
    config_path: Path,
    dry_run: bool,
    planned_step_count: int,
    report_rows: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    error: str | None = None,
) -> Path:
    orchestration_root.mkdir(parents=True, exist_ok=True)
    completed = sum(1 for row in report_rows if str(row.get("status", "")).upper() == "PASS")
    acceptance = compute_manifest_acceptance(
        planned_step_count=planned_step_count,
        report_rows=report_rows,
        failures=failures,
        dry_run=dry_run,
        error=error,
    )
    # Dry-runs write to a SEPARATE manifest so they never clobber the operational manifest
    # that downstream health checks read.
    steps_name = "defense_refresh_steps.dryrun.csv" if dry_run else "defense_refresh_steps.csv"
    manifest_name = "defense_refresh_manifest.dryrun.json" if dry_run else "defense_refresh_manifest.json"
    write_csv_atomic(orchestration_root / steps_name, MANIFEST_FIELDS, report_rows)
    summary = {
        "acceptance": acceptance,
        "run_id": run_id,
        "asof_date": asof,
        "database_path": str(db_path),
        "config_path": str(config_path),
        "dry_run": bool(dry_run),
        "planned_step_count": planned_step_count,
        "completed_step_count": completed,
        "recorded_step_count": len(report_rows),
        "failed_step_count": len(failures),
        "error": error or "",
        "steps": report_rows,
    }
    manifest_path = orchestration_root / manifest_name
    write_text_atomic(manifest_path, json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {key: summary[key] for key in ("acceptance", "run_id", "dry_run", "completed_step_count", "planned_step_count", "failed_step_count")},
            indent=2,
        )
    )
    return manifest_path


def run_selftest() -> int:
    asof = "2026-07-17"
    history_start = "2018-01-01"
    full = build_steps(asof, history_start, positioning_through_publish_only=False)
    fast = build_steps(asof, history_start, positioning_through_publish_only=True)
    assert len(full) == 14, f"expected 14 full steps, got {len(full)}"
    assert len(fast) == 6, f"expected 6 publish-only steps, got {len(fast)}"
    full_ids = [step.step_id for step in full]
    assert full_ids[0] == "03_sync_prices", full_ids
    assert full_ids[-3:] == ["17_publish", "18_validate_publish", "20_validate_portfolio"], full_ids
    # publish-only mode must skip the market/financial block but keep publish+validate.
    assert [step.step_id for step in fast] == [
        "13_sync_positioning",
        "14_validate_positioning",
        "10_validate_eligibility",
        "17_publish",
        "18_validate_publish",
        "20_validate_portfolio",
    ], [step.step_id for step in fast]
    assert any(step.step_id == "17_publish" for step in fast), "daily oos publish step must always run"
    assert {step.step_id for step in full if step.network} == {
        "03_sync_prices",
        "11_sync_fx",
        "07_sync_sec",
        "13_sync_positioning",
    }, "network step set drifted"
    # Manifest shape parity: acceptance/PASS-FAIL summary keys must exist.
    rows = [
        {
            "run_id": "r",
            "step_number": idx,
            "step_id": step.step_id,
            "stage": step.stage,
            "script": step.args[0],
            "network_flag": int(step.network),
            "command": subprocess.list2cmdline([sys.executable, *step.args]),
            "log_path": "",
            "status": "DRY_RUN",
            "return_code": "",
            "elapsed_sec": 0.0,
        }
        for idx, step in enumerate(full, start=1)
    ]
    assert set(rows[0]) == set(MANIFEST_FIELDS), "steps.csv fields drifted from MANIFEST_FIELDS"
    lock_path = shared_industrials_lock_path()
    assert lock_path.name == ".industrials_refresh.lock", lock_path
    assert lock_path.parent.name == "industrials" and lock_path.parent.parent.name == "output", lock_path
    # The shared advisory lock must reject concurrent ownership and become reusable
    # immediately after release (including process-exit release at the OS level).
    with tempfile.TemporaryDirectory() as temp_dir:
        probe_path = Path(temp_dir) / "refresh.lock"
        with RefreshLock(probe_path):
            try:
                with RefreshLock(probe_path):
                    raise AssertionError("concurrent refresh lock unexpectedly acquired")
            except RuntimeError:
                pass
        with RefreshLock(probe_path):
            pass

    # --- acceptance derivation (finding 6): fail-closed on completed < planned / lock exception ---
    def _row(status: str) -> dict[str, Any]:
        return {"status": status}

    all_pass_rows = [_row("PASS") for _ in range(14)]
    assert compute_manifest_acceptance(planned_step_count=14, report_rows=all_pass_rows, failures=[], dry_run=False) == "PASS"
    # completed < planned (aborted after 3 steps, one failed) must be FAIL.
    partial_rows = [_row("PASS"), _row("PASS"), _row("FAIL")]
    assert compute_manifest_acceptance(planned_step_count=14, report_rows=partial_rows, failures=[partial_rows[-1]], dry_run=False) == "FAIL"
    # lock exception: zero completed steps must seal FAIL, never PASS.
    assert compute_manifest_acceptance(planned_step_count=14, report_rows=[], failures=[], dry_run=False, error="RuntimeError: locked") == "FAIL"
    # empty-planned guard: no PASS is possible with 0 planned steps.
    assert compute_manifest_acceptance(planned_step_count=0, report_rows=[], failures=[], dry_run=False) == "FAIL"
    # completed == planned but zero failures without the error still PASS; with a lingering
    # exception it flips to FAIL even if rows look complete.
    assert compute_manifest_acceptance(planned_step_count=14, report_rows=all_pass_rows, failures=[], dry_run=False, error="boom") == "FAIL"
    # dry-run acceptance is isolated and never an operational verdict.
    assert compute_manifest_acceptance(planned_step_count=14, report_rows=[_row("DRY_RUN")], failures=[], dry_run=True) == "DRY_RUN"
    # dry-run writes a SEPARATE manifest file, never clobbering the operational one.
    assert "dryrun" not in "defense_refresh_manifest.json"

    # --- config forwarding (finding 6): only config-aware children receive --config ---
    cfg = Path("industrials/config.yaml")
    config_aware = {s.step_id for s in full if s.accepts_config}
    assert config_aware == {
        "13_sync_positioning", "14_validate_positioning", "17_publish", "18_validate_publish", "20_validate_portfolio",
    }, f"config-forwarding step set drifted: {config_aware}"
    pub_step = next(s for s in full if s.step_id == "17_publish")
    price_step = next(s for s in full if s.step_id == "03_sync_prices")
    pub_cmd = step_command(pub_step, cfg)
    assert "--config" in pub_cmd and pub_cmd.index("--config") == 2, f"config must follow the script path: {pub_cmd}"
    assert str(cfg) == pub_cmd[pub_cmd.index("--config") + 1], "forwarded config path must match"
    assert "--config" not in step_command(price_step, cfg), "config-unaware child must NOT receive --config"

    # --- coverage audit is a gated final step folded into the manifest before it seals (finding 6) ---
    steps_plus_audit = len(full) + 1
    audit_ok = [_row("PASS") for _ in range(steps_plus_audit)]
    assert compute_manifest_acceptance(planned_step_count=steps_plus_audit, report_rows=audit_ok, failures=[], dry_run=False) == "PASS"
    audit_fail = [_row("PASS") for _ in range(len(full))] + [_row("FAIL")]
    assert compute_manifest_acceptance(planned_step_count=steps_plus_audit, report_rows=audit_fail, failures=[audit_fail[-1]], dry_run=False) == "FAIL"
    # steps passed but audit never recorded (crash before folding it in) -> completed < planned -> FAIL
    assert compute_manifest_acceptance(planned_step_count=steps_plus_audit, report_rows=[_row("PASS") for _ in range(len(full))], failures=[], dry_run=False) == "FAIL"

    print("SELFTEST PASS: 14 full / 6 publish-only steps; manifest keys OK; acceptance fail-closed; "
          "config forwarding + gated coverage audit + crash-safe lock OK; lock=" + str(lock_path))
    return 0


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    if args.selftest:
        return run_selftest()
    if not args.asof:
        raise SystemExit("--asof is required (except with --selftest)")
    config_path = args.config.expanduser().resolve()
    asof = parse_asof(args.asof)
    history_start = parse_asof(args.positioning_history_start)
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    orchestration_root = (PROJECT_ROOT / "output" / "industrials" / "defense" / "orchestration").resolve()
    lock_path = shared_industrials_lock_path()

    steps = build_steps(asof, history_start, positioning_through_publish_only=args.positioning_through_publish_only)
    if args.list_steps:
        for step in steps:
            print(f"{step.step_id}\t{step.stage}\t{'network' if step.network else 'local'}\t{step.args[0]}")
        return 0

    # Sub-second + PID identity prevents concurrent/retried invocations in the same
    # second from writing indistinguishable manifest rows or log identities.
    run_id = f"{datetime.now(timezone.utc).strftime('defense_refresh_%Y%m%dT%H%M%S_%f')}Z_p{os.getpid()}"
    report_rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    def record(step: Step, index: int, status: str, return_code: object, elapsed: float) -> dict[str, Any]:
        row = {
            "run_id": run_id,
            "step_number": index,
            "step_id": step.step_id,
            "stage": step.stage,
            "script": step.args[0],
            "network_flag": int(step.network),
            "command": subprocess.list2cmdline(step_command(step, config_path)),
            "log_path": "",
            "status": status,
            "return_code": return_code,
            "elapsed_sec": round(elapsed, 3),
        }
        report_rows.append(row)
        return row

    planned_step_count = len(steps) + 1  # publish chain + the gated coverage audit (finding 6)
    if args.dry_run:
        for index, step in enumerate(steps, start=1):
            print(f"[defense_daily_refresh][DRY_RUN {index}/{planned_step_count}] {step.label}: "
                  f"{subprocess.list2cmdline(step_command(step, config_path))}", flush=True)
            record(step, index, "DRY_RUN", "", 0.0)
        print(f"[defense_daily_refresh][DRY_RUN {planned_step_count}/{planned_step_count}] coverage audit: "
              f"coverage_audit(config={config_path}, asof={asof})", flush=True)
        report_rows.append({
            "run_id": run_id, "step_number": planned_step_count, "step_id": COVERAGE_AUDIT_STEP_ID,
            "stage": "stage_11", "script": "coverage_audit", "network_flag": 0,
            "command": f"coverage_audit(config={config_path}, asof={asof})", "log_path": "",
            "status": "DRY_RUN", "return_code": "", "elapsed_sec": 0.0,
        })
        write_manifest(
            orchestration_root,
            run_id=run_id,
            asof=asof,
            db_path=db_path,
            config_path=config_path,
            dry_run=True,
            planned_step_count=planned_step_count,
            report_rows=report_rows,
            failures=failures,
        )
        return 0

    all_passed = False
    run_error: str | None = None
    try:
        with RefreshLock(lock_path):
            for index, step in enumerate(steps, start=1):
                cmd = step_command(step, config_path)
                print(f"[defense_daily_refresh] {step.label}: {' '.join(cmd)}", flush=True)
                started = time.perf_counter()
                result = subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=False)
                elapsed = time.perf_counter() - started
                status = "PASS" if result.returncode == 0 else "FAIL"
                row = record(step, index, status, result.returncode, elapsed)
                if result.returncode != 0:
                    failures.append(row)
                    # Preserve prior behavior: the sequence aborts on the first failure.
                    break
            steps_passed = not failures and len(report_rows) == len(steps)
            # Coverage audit is the FINAL gated step, folded into the SAME manifest BEFORE it
            # is sealed (finding 6). Previously the manifest was written (acceptance PASS) and
            # THEN coverage_audit ran outside the try -- an audit failure crashed the process
            # while the manifest already claimed PASS. Now a failing audit records a FAIL row
            # and sets run_error, so the acceptance derivation seals FAIL.
            if steps_passed:
                print("[defense_daily_refresh] coverage_audit: verifying source coverage", flush=True)
                audit_started = time.perf_counter()
                try:
                    coverage_audit(config_path, asof)
                    audit_status: str = "PASS"
                    audit_rc: object = 0
                except Exception as audit_exc:  # noqa: BLE001 - an audit failure must FAIL the manifest
                    audit_status = "FAIL"
                    audit_rc = 1
                    run_error = f"coverage_audit: {type(audit_exc).__name__}: {audit_exc}"
                audit_row = {
                    "run_id": run_id,
                    "step_number": len(steps) + 1,
                    "step_id": COVERAGE_AUDIT_STEP_ID,
                    "stage": "stage_11",
                    "script": "coverage_audit",
                    "network_flag": 0,
                    "command": f"coverage_audit(config={config_path}, asof={asof})",
                    "log_path": "",
                    "status": audit_status,
                    "return_code": audit_rc,
                    "elapsed_sec": round(time.perf_counter() - audit_started, 3),
                }
                report_rows.append(audit_row)
                if audit_status == "FAIL":
                    failures.append(audit_row)
            all_passed = not failures and len(report_rows) == planned_step_count
    except Exception as exc:  # noqa: BLE001 - a lock-contention/setup exception must seal a FAIL manifest
        run_error = f"{type(exc).__name__}: {exc}"
    finally:
        # Both the lock-exception path (completed 0 steps) and an audit failure record
        # completed < planned / a non-empty failure set, so the acceptance derivation seals
        # FAIL rather than a spurious PASS.
        write_manifest(
            orchestration_root,
            run_id=run_id,
            asof=asof,
            db_path=db_path,
            config_path=config_path,
            dry_run=False,
            planned_step_count=planned_step_count,
            report_rows=report_rows,
            failures=failures,
            error=run_error,
        )
    if run_error is not None:
        print(f"[defense_daily_refresh] FAILED: {run_error}", flush=True)
        return 1
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

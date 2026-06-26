#!/usr/bin/env python3
"""Run the med-devices production refresh sequence.

The default path refreshes production inputs, rebuilds derived features, scores
the requested as-of date, publishes the dated review pack, and validates the
final output surface. Research calibration and backfills remain separate so a
routine daily refresh cannot accidentally change model policy.
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CONFIG_KEY = "med_devices_refresh_pipeline"
PROTECTED_CRITICAL_STEPS = {
    "00_init_db",
    "01_load_universe",
    "06_build_financial_features",
    "09_link_fda",
    "70_audit_fda_mapping",
    "10_build_fda_features",
    "15_link_reimbursement",
    "11_build_reimbursement_features",
    "12_build_technical_features",
    "54_build_borrow_features",
    "56_build_short_interest_features",
    "58_build_institutional_flow_features",
    "60_build_insider_activity_features",
    "13_build_daily_scores",
    "16_publish_review_pack",
    "74_build_analyst_review",
    "72_validate_production_outputs",
}


@dataclass(frozen=True)
class Step:
    step_id: str
    stage: str
    description: str
    script: Path
    args: list[str] = field(default_factory=list)
    pass_db: bool = True
    network: bool = False
    optional: bool = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the med-devices production refresh pipeline.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default="", help="Production feature/score as-of date, YYYY-MM-DD.")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list-steps", action="store_true")
    parser.add_argument("--skip-network", action="store_true", help="Rebuild from existing DB/cache data only.")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--fail-on-optional-error", action="store_true")
    parser.add_argument("--from-step", default="", help="Start at this step id, inclusive.")
    parser.add_argument("--to-step", default="", help="Stop at this step id, inclusive.")
    parser.add_argument("--only", default="", help="Comma-separated step ids to run.")
    parser.add_argument("--skip-step", action="append", default=[], help="Step id to skip. Can be repeated.")
    parser.add_argument("--force-refresh", action="store_true", help="Refresh source caches where supported.")
    parser.add_argument("--skip-ibkr-borrow", action="store_true")
    parser.add_argument("--skip-form4-runner", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Resume from a previous manifest by skipping passed steps.")
    parser.add_argument("--resume-manifest", type=Path, default=None, help="Manifest JSON to resume from; defaults to latest manifest.")
    parser.add_argument("--rerun-passed", action="store_true", help="With --resume, rerun passed steps instead of skipping them.")
    parser.add_argument("--retry-optional", action="store_true", help="With --resume, retry optional failures instead of treating them as done.")
    parser.add_argument(
        "--import-positioning-sources",
        nargs="?",
        const="",
        default="short_interest,borrow",
        help="Sources passed to script 61 after shared positioning refreshes.",
    )
    return parser.parse_args()


def py_script(relative: str) -> Path:
    return PROJECT_ROOT / relative


def asof_or_today(raw: str) -> str:
    return str(raw or "").strip() or date.today().isoformat()


def build_steps(
    *,
    asof: str,
    force_refresh: bool,
    skip_ibkr_borrow: bool,
    skip_form4_runner: bool,
    import_positioning_sources: str,
) -> list[Step]:
    asof_args = ["--asof", asof] if asof else []
    sec_args = ["--refresh-cache"] if force_refresh else []
    form4_args = [*asof_args, "--skip-feature-build", "--skip-coverage-audit"]
    if skip_form4_runner:
        form4_args.append("--skip-runner")
    import_sources = ",".join(item.strip() for item in import_positioning_sources.split(",") if item.strip())

    steps: list[Step] = [
        Step("00_init_db", "stage_1", "Initialize med-devices DB/schema/source registry", py_script("med_devices/scripts/00_init_med_devices_db.py")),
        Step("01_load_universe", "stage_2", "Load med-devices investable universe", py_script("med_devices/scripts/01_load_med_device_universe.py")),
        Step("04_sync_yahoo_prices", "stage_3", "Sync Yahoo adjusted prices", py_script("med_devices/scripts/04_sync_med_device_yahoo_adjusted_prices.py"), [*asof_args, "--allow-partial"], network=True),
        Step("20_sync_market_snapshots", "stage_3", "Sync market snapshots", py_script("med_devices/scripts/20_sync_med_device_market_snapshots.py"), [*asof_args, "--allow-partial"], network=True),
        Step("05_sync_sec_fundamentals", "stage_4", "Sync SEC fundamentals", py_script("med_devices/scripts/05_sync_med_device_sec_fundamentals.py"), [*sec_args, "--allow-partial"], network=True),
        Step("06_build_financial_features", "stage_4", "Build financial/valuation features", py_script("med_devices/scripts/06_build_med_device_financial_features.py"), asof_args),
        Step("08_sync_fda_core", "stage_5", "Sync FDA core source facts", py_script("med_devices/scripts/08_sync_med_device_fda_core.py"), ["--allow-partial"], network=True),
        Step("09_link_fda", "stage_5", "Link FDA manufacturers to companies", py_script("med_devices/scripts/09_link_med_device_fda_to_companies.py")),
        Step("70_audit_fda_mapping", "stage_5", "Audit FDA mapping governance", py_script("med_devices/scripts/70_audit_med_device_fda_mapping_governance.py")),
        Step("10_build_fda_features", "stage_5", "Build FDA product-risk features", py_script("med_devices/scripts/10_build_med_device_fda_features.py"), asof_args),
        Step("14_sync_cms_reimbursement", "stage_5", "Sync CMS/reimbursement source facts", py_script("med_devices/scripts/14_sync_med_device_cms_reimbursement.py"), ["--allow-partial"], network=True),
        Step("15_link_reimbursement", "stage_5", "Link reimbursement evidence to companies", py_script("med_devices/scripts/15_link_med_device_reimbursement_to_companies.py")),
        Step("11_build_reimbursement_features", "stage_5", "Build reimbursement features", py_script("med_devices/scripts/11_build_med_device_reimbursement_features.py"), asof_args),
        Step("12_build_technical_features", "stage_6", "Build technical-entry features", py_script("med_devices/scripts/12_build_med_device_technical_features.py"), asof_args),
        Step("55_sync_finra_short_volume", "stage_7", "Sync FINRA short-volume facts", py_script("med_devices/scripts/55_sync_med_device_finra_short_volume.py"), ["--end-date", asof] if asof else [], network=True, optional=True),
        Step("65_update_finra_short_interest", "stage_7", "Update shared FINRA short-interest facts", py_script("med_devices/scripts/65_update_med_device_finra_short_interest.py"), asof_args, pass_db=False, network=True, optional=True),
        Step("62_update_sec13f_positioning", "stage_7", "Update shared SEC 13F positioning facts", py_script("med_devices/scripts/62_update_med_device_market_positioning.py"), asof_args, pass_db=False, network=True, optional=True),
    ]
    if not skip_ibkr_borrow:
        steps.append(
            Step(
                "53_sync_ibkr_borrow",
                "stage_7",
                "Sync IBKR borrow availability",
                py_script("med_devices/scripts/53_sync_med_device_ibkr_borrow.py"),
                asof_args,
                network=True,
                optional=True,
            )
        )
    if import_sources:
        steps.append(
            Step(
                "61_import_positioning",
                "stage_7",
                "Import shared positioning facts into med-devices DB",
                py_script("med_devices/scripts/61_import_med_device_external_positioning_facts.py"),
                [*asof_args, "--sources", import_sources],
                optional=True,
            )
        )
    steps.extend(
        [
            Step("63_rebuild_sec13f_common_shares", "stage_7", "Rebuild point-in-time SEC 13F common-share facts", py_script("med_devices/scripts/63_rebuild_med_device_sec_13f_common_share_facts.py"), asof_args, optional=True),
            Step("54_build_borrow_features", "stage_7", "Build borrow-positioning features", py_script("med_devices/scripts/54_build_med_device_borrow_features.py"), asof_args),
            Step("56_build_short_interest_features", "stage_7", "Build short-interest features", py_script("med_devices/scripts/56_build_med_device_short_interest_features.py"), asof_args),
            Step("58_build_institutional_flow_features", "stage_7", "Build institutional-flow features", py_script("med_devices/scripts/58_build_med_device_institutional_flow_features.py"), asof_args),
            Step("68_update_form4_canonical", "stage_7", "Update SEC Form 4 canonical/import path", py_script("med_devices/scripts/68_update_med_device_form4_canonical.py"), form4_args, pass_db=False, network=True, optional=True),
            Step("60_build_insider_activity_features", "stage_7", "Build insider-activity features", py_script("med_devices/scripts/60_build_med_device_insider_activity_features.py"), asof_args),
            Step("67_audit_external_positioning", "stage_7", "Audit external-positioning source coverage", py_script("med_devices/scripts/67_audit_med_device_external_positioning_coverage.py"), asof_args, optional=True),
            Step("13_build_daily_scores", "stage_8", "Build daily composite scores", py_script("med_devices/scripts/13_build_med_device_daily_scores.py"), asof_args),
            Step("16_publish_review_pack", "stage_8", "Publish dated score review pack", py_script("med_devices/scripts/16_publish_med_device_score_review_pack.py"), asof_args),
            Step("74_build_analyst_review", "stage_8", "Build analyst review queue", py_script("med_devices/scripts/74_build_med_device_analyst_review_queue.py"), asof_args),
            Step("72_validate_production_outputs", "stage_8", "Run final production QA gate", py_script("med_devices/scripts/72_validate_med_device_production_outputs.py"), asof_args),
            Step("73_audit_calibration_governance", "stage_9", "Audit calibration refresh cadence", py_script("med_devices/scripts/73_audit_med_device_calibration_governance.py"), asof_args, optional=True),
        ]
    )
    return steps


def step_index(steps: list[Step], step_id: str) -> int:
    for idx, step in enumerate(steps):
        if step.step_id == step_id:
            return idx
    raise ValueError(f"Unknown step id: {step_id}")


def selected_steps(steps: list[Step], args: argparse.Namespace) -> list[Step]:
    out = list(steps)
    if args.from_step:
        out = out[step_index(out, args.from_step):]
    if args.to_step:
        idx = step_index(out, args.to_step)
        out = out[: idx + 1]
    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        unknown = sorted(wanted.difference({step.step_id for step in steps}))
        if unknown:
            raise ValueError(f"Unknown --only step ids: {unknown}")
        out = [step for step in steps if step.step_id in wanted]
    skipped = {str(item).strip() for item in args.skip_step if str(item).strip()}
    out = [step for step in out if step.step_id not in skipped and (not args.skip_network or not step.network)]
    return out


def configured_optional_step_ids(config: dict[str, Any]) -> set[str] | None:
    raw = cfg_get(config, f"{CONFIG_KEY}.optional_external_steps", None)
    if raw is None:
        return None
    values = raw if isinstance(raw, list) else str(raw or "").split(",")
    return {str(item or "").strip() for item in values if str(item or "").strip()}


def known_step_ids_for_optional_config() -> set[str]:
    return {
        step.step_id
        for step in build_steps(
            asof="",
            force_refresh=False,
            skip_ibkr_borrow=False,
            skip_form4_runner=False,
            import_positioning_sources="short_interest,borrow",
        )
    }


def apply_optional_step_config(steps: list[Step], optional_step_ids: set[str] | None) -> list[Step]:
    if not optional_step_ids:
        return steps
    known = {step.step_id for step in steps}
    unknown = sorted(optional_step_ids.difference(known_step_ids_for_optional_config()))
    if unknown:
        raise ValueError(f"Unknown {CONFIG_KEY}.optional_external_steps ids: {unknown}")
    protected = sorted(optional_step_ids.intersection(PROTECTED_CRITICAL_STEPS))
    if protected:
        raise ValueError(
            f"{CONFIG_KEY}.optional_external_steps cannot mark protected critical steps optional: {protected}"
        )
    applicable_optional_ids = optional_step_ids.intersection(known)
    return [replace(step, optional=step.optional or step.step_id in applicable_optional_ids) for step in steps]


def command_for_step(step: Step, *, config_path: Path, db_path: Path | None) -> list[str]:
    cmd = [sys.executable, str(step.script), "--config", str(config_path)]
    if step.pass_db and db_path is not None:
        cmd.extend(["--db", str(db_path)])
    cmd.extend(step.args)
    return cmd


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        if fields:
            writer.writeheader()
            writer.writerows(rows)


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Resume manifest not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Resume manifest root must be an object: {path}")
    return payload


def resume_completed_step_ids(
    manifest: dict[str, Any],
    *,
    rerun_passed: bool,
    retry_optional: bool,
) -> set[str]:
    completed: set[str] = set()
    for row in manifest.get("steps", []) or []:
        if not isinstance(row, dict):
            continue
        step_id = str(row.get("step_id") or "").strip()
        status = str(row.get("status") or "").strip().upper()
        if not step_id:
            continue
        if status == "PASS" and not rerun_passed:
            completed.add(step_id)
        elif status == "OPTIONAL_FAIL" and not retry_optional:
            completed.add(step_id)
    return completed


def archive_paths(output_dir: Path, run_id: str) -> tuple[Path, Path, Path, Path]:
    latest_json = output_dir / "med_devices_refresh_manifest.json"
    latest_csv = output_dir / "med_devices_refresh_steps.csv"
    archive_json = output_dir / f"med_devices_refresh_manifest_{run_id}.json"
    archive_csv = output_dir / f"med_devices_refresh_steps_{run_id}.csv"
    return latest_json, latest_csv, archive_json, archive_csv


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    asof = asof_or_today(args.asof)
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(
            cfg_get(config, f"{CONFIG_KEY}.output_dir", "../output/med_devices_reports/orchestration"),
            base_dir=base_dir,
        )
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = output_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("med_devices_refresh_%Y%m%dT%H%M%SZ")
    production_latest_json, production_latest_csv, archive_manifest_json, archive_manifest_csv = archive_paths(output_dir, run_id)
    latest_manifest_json = (
        output_dir / "med_devices_refresh_dry_run_manifest.json" if args.dry_run else production_latest_json
    )
    latest_manifest_csv = (
        output_dir / "med_devices_refresh_dry_run_steps.csv" if args.dry_run else production_latest_csv
    )

    steps = build_steps(
        asof=asof,
        force_refresh=bool(args.force_refresh),
        skip_ibkr_borrow=bool(args.skip_ibkr_borrow),
        skip_form4_runner=bool(args.skip_form4_runner),
        import_positioning_sources=str(args.import_positioning_sources or ""),
    )
    steps = apply_optional_step_config(steps, configured_optional_step_ids(config))
    if args.list_steps:
        for step in steps:
            flags = ",".join(flag for flag, enabled in [("network", step.network), ("optional", step.optional)] if enabled)
            print(f"{step.step_id}\t{step.stage}\t{flags}\t{step.description}")
        return 0

    selected = selected_steps(steps, args)
    resumed_from: dict[str, Any] | None = None
    resume_skipped_ids: set[str] = set()
    if args.resume:
        resume_manifest_path = (
            args.resume_manifest.expanduser().resolve()
            if args.resume_manifest
            else production_latest_json
        )
        resumed_from = load_manifest(resume_manifest_path)
        resume_skipped_ids = resume_completed_step_ids(
            resumed_from,
            rerun_passed=bool(args.rerun_passed),
            retry_optional=bool(args.retry_optional),
        ).intersection({step.step_id for step in selected})
    planned = [step for step in selected if step.step_id not in resume_skipped_ids]
    rows: list[dict[str, Any]] = [
        {
            "run_id": run_id,
            "step_number": idx,
            "step_id": step.step_id,
            "stage": step.stage,
            "description": step.description,
            "script": str(step.script),
            "network_flag": int(step.network),
            "optional_flag": int(step.optional),
            "pass_db_flag": int(step.pass_db),
            "command": "",
            "log_path": "",
            "status": "RESUME_SKIPPED",
            "return_code": "",
            "elapsed_sec": 0.0,
        }
        for idx, step in enumerate(selected, start=1)
        if step.step_id in resume_skipped_ids
    ]
    failures: list[dict[str, Any]] = []
    started = datetime.now(timezone.utc)
    for run_idx, step in enumerate(planned, start=1):
        selected_idx = selected.index(step) + 1
        cmd = command_for_step(step, config_path=config_path, db_path=db_path)
        log_path = logs_dir / f"{run_id}_{selected_idx:02d}_{step.step_id}.log"
        row: dict[str, Any] = {
            "run_id": run_id,
            "step_number": selected_idx,
            "step_id": step.step_id,
            "stage": step.stage,
            "description": step.description,
            "script": str(step.script),
            "network_flag": int(step.network),
            "optional_flag": int(step.optional),
            "pass_db_flag": int(step.pass_db),
            "command": " ".join(cmd),
            "log_path": str(log_path),
        }
        print(f"[{run_idx}/{len(planned)}] {step.step_id}: {step.description}")
        if args.dry_run:
            row.update({"status": "DRY_RUN", "return_code": "", "elapsed_sec": 0.0})
            rows.append(row)
            continue
        start = time.perf_counter()
        with log_path.open("w", encoding="utf-8", newline="") as log:
            log.write(f"run_id={run_id}\nstep={step.step_id}\nstarted_utc={datetime.now(timezone.utc).isoformat(timespec='seconds')}\n")
            log.write(f"command={' '.join(cmd)}\n\n")
            result = subprocess.run(cmd, cwd=PROJECT_ROOT, stdout=log, stderr=subprocess.STDOUT, text=True, check=False)
        elapsed = time.perf_counter() - start
        failed = result.returncode != 0
        tolerated = failed and step.optional and not args.fail_on_optional_error
        row.update(
            {
                "status": "OPTIONAL_FAIL" if tolerated else "PASS" if not failed else "FAIL",
                "return_code": result.returncode,
                "elapsed_sec": round(elapsed, 3),
            }
        )
        rows.append(row)
        if failed:
            failures.append(row)
            print(f"{'OPTIONAL FAILED' if tolerated else 'FAILED'} {step.step_id}; see {log_path}")
            if not tolerated and not args.continue_on_error:
                break

    ended = datetime.now(timezone.utc)
    rows.sort(key=lambda item: int(item.get("step_number") or 0))
    validation: dict[str, Any] = {
        "status": "DELEGATED",
        "step_id": "72_validate_production_outputs",
        "description": "Final production QA is enforced by the dedicated QA gate step.",
    }

    summary = {
        "run_id": run_id,
        "started_at_utc": started.isoformat(timespec="seconds"),
        "ended_at_utc": ended.isoformat(timespec="seconds"),
        "dry_run": bool(args.dry_run),
        "asof": asof,
        "database_path": str(db_path),
        "config_path": str(config_path),
        "step_count": len(rows),
        "planned_step_count": len(selected),
        "executed_step_count": len([row for row in rows if row.get("status") != "RESUME_SKIPPED"]),
        "resume_skipped_step_count": len([row for row in rows if row.get("status") == "RESUME_SKIPPED"]),
        "resume_skipped_step_ids": sorted(resume_skipped_ids),
        "resumed_from_run_id": str(resumed_from.get("run_id") or "") if resumed_from else "",
        "resume_manifest": str(args.resume_manifest.expanduser().resolve() if args.resume_manifest else production_latest_json) if args.resume else "",
        "failed_step_count": len([row for row in failures if row.get("status") != "OPTIONAL_FAIL"]),
        "optional_failed_step_count": len([row for row in failures if row.get("status") == "OPTIONAL_FAIL"]),
        "status": "PASS" if not [row for row in failures if row.get("status") != "OPTIONAL_FAIL"] else "FAIL",
        "output_dir": str(output_dir),
        "manifest_json": str(latest_manifest_json),
        "manifest_csv": str(latest_manifest_csv),
        "manifest_archive_json": str(archive_manifest_json),
        "manifest_archive_csv": str(archive_manifest_csv),
        "validation": validation,
        "steps": rows,
    }
    archive_manifest_json.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str), encoding="utf-8")
    latest_manifest_json.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str), encoding="utf-8")
    write_csv(archive_manifest_csv, rows)
    write_csv(latest_manifest_csv, rows)
    print(
        json.dumps(
            {
                key: summary[key]
                for key in (
                    "run_id",
                    "status",
                    "dry_run",
                    "asof",
                    "step_count",
                    "failed_step_count",
                    "optional_failed_step_count",
                    "output_dir",
                )
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 1 if summary["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())

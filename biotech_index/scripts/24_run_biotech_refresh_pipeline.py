#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import signal
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from biotech_index.core.db import connect, quote_identifier  # noqa: E402
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402
from biotech_index.core.market_policy import scoring_market_sources  # noqa: E402
from biotech_index.core.pipeline_guards import format_ticker_sample, read_final_scoring_tickers, universe_coverage  # noqa: E402


LOGGER = logging.getLogger("run_biotech_refresh_pipeline")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"

BIOTECH_SCORE_REQUIRED_COLUMNS = [
    "tier1_selection_gate_score",
    "data_quality_confidence_multiplier",
    "clinical_risk_drag",
    "investment_risk_drag",
]

BIOTECH_SCORE_CSV_REQUIRED_COLUMNS = [
    *BIOTECH_SCORE_REQUIRED_COLUMNS,
    "tier1_primary_horizon_trading_days",
    "tier1_production_score_model",
    "tier1_selection_policy",
    "alpha_multibagger_role",
    "core_structural_veto_flag",
    "rank_demoted_by_core_veto",
    "effective_total_risk_drag",
]

BIOTECH_SCORE_CSV_PRESENT_COLUMNS = [
    "core_structural_veto_reasons",
]

MULTIBAGGER_SCORE_BASE_REQUIRED_COLUMNS = [
    "base_multibagger_score",
    "orthogonal_alpha_score",
    "distinctive_acceleration_score",
    "tier1_available",
    "tier1_interaction_reason",
]

MULTIBAGGER_SCORE_TIER1_REQUIRED_COLUMNS = [
    "tier1_opportunity_score",
    "tier1_risk_score",
    "tier1_bucket",
    "tier1_gate_score",
    "tier1_gate_multiplier",
]


@dataclass(frozen=True)
class Step:
    name: str
    script: str
    args: tuple[str, ...] = ()
    supports_asof: bool = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the biotech refresh pipeline with explicit delta/reconcile/backfill modes.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="As-of date in YYYY-MM-DD. Defaults to UTC today.")
    parser.add_argument("--mode", choices=["daily_delta", "weekly_reconcile", "full_backfill"], default="daily_delta")
    parser.add_argument("--steps", type=str, default="", help="Optional comma-separated step names to run.")
    parser.add_argument("--skip-ctgov", action="store_true", help="Skip CTGov sync/link/audit upstream steps.")
    parser.add_argument("--skip-ib", action="store_true", help="Skip the IB market-data step.")
    parser.add_argument("--skip-yahoo", action="store_true", help="Skip the Yahoo adjusted market-data step.")
    parser.add_argument("--skip-analyze", action="store_true", help="Skip SQLite ANALYZE at the end.")
    parser.add_argument("--skip-final-validation", action="store_true", help="Skip final as-of/coverage validation after a full pipeline run.")
    parser.add_argument("--skip-form4-preflight", action="store_true", help="Skip the staging Form 4 database freshness preflight.")
    parser.add_argument("--reuse-unchanged-historical", action="store_true", help="Reuse exact-signature governance rows for historical snapshot runs.")
    parser.add_argument(
        "--history-restatement",
        action="store_true",
        help=(
            "Restate derived historical feature/score layers over a date grid after raw source tables are current. "
            "This does not run raw upstream sync steps."
        ),
    )
    parser.add_argument("--history-start-asof", type=str, default="", help="Earliest YYYY-MM-DD date for --history-restatement.")
    parser.add_argument("--history-end-asof", type=str, default="", help="Latest YYYY-MM-DD date for --history-restatement. Defaults to --asof.")
    parser.add_argument(
        "--history-dates",
        type=str,
        default="",
        help="Optional comma-separated YYYY-MM-DD date grid for --history-restatement. Overrides source-table date loading.",
    )
    parser.add_argument(
        "--history-date-source",
        choices=["daily_scores", "daily_features"],
        default="daily_scores",
        help="Source table for --history-restatement date grid when --history-dates is omitted.",
    )
    parser.add_argument(
        "--history-fridays-only",
        action="store_true",
        help="Restrict --history-restatement source-table date grid to Fridays.",
    )
    return parser.parse_args()


def configure_logging() -> None:
    configure_utc_logging()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def compact_asof(asof: str) -> str:
    parsed = parse_date(asof)
    return parsed.strftime("%Y%m%d") if parsed is not None else str(asof).replace("-", "")


def dated_output_file(base_dir: Path, asof: str, filename: str) -> Path:
    dated_dir = base_dir if base_dir.name == compact_asof(asof) else base_dir / compact_asof(asof)
    return dated_dir / filename


def output_file_with_dated_fallback(base_dir: Path, asof: str, filename: str) -> Path:
    root_file = base_dir / filename
    return root_file if root_file.exists() else dated_output_file(base_dir, asof, filename)


def as_bool(raw: object, default: bool = False) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "t", "yes", "y", "enabled", "on"}:
        return True
    if text in {"0", "false", "f", "no", "n", "disabled", "off"}:
        return False
    return default


def parse_string_list(raw: object, default: list[str] | None = None) -> list[str]:
    if raw is None:
        return list(default or [])
    if isinstance(raw, str):
        parts = raw.replace(";", ",").replace("|", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        parts = [str(item) for item in raw]
    else:
        parts = [str(raw)]
    values = [part.strip() for part in parts if part.strip()]
    return values or list(default or [])


def validate_config(config: dict[str, Any]) -> None:
    optional_defaults = {
        "governance_events.form4_required": True,
        "governance_events.reuse_unchanged_historical": False,
        "biotech_refresh.step_timeout_sec": 14400.0,
        "calibration.exclude_tickers": [],
    }
    for key, default in optional_defaults.items():
        if cfg_get(config, key, None) is None:
            LOGGER.warning("Config key %s is missing; defaulting to %r", key, default)

    biotech_buckets = cfg_get(config, "biotech_scoring.buckets", {}) or {}
    if biotech_buckets:
        watch = float(biotech_buckets.get("watchlist_min", 60.0))
        speculative = float(biotech_buckets.get("speculative_min", 45.0))
        if watch <= speculative:
            raise RuntimeError(
                "Invalid biotech_scoring.buckets thresholds: watchlist_min must exceed speculative_min."
            )

    multibagger = cfg_get(config, "multibagger", {}) or {}
    if multibagger:
        weights = multibagger.get("weights", {}) if isinstance(multibagger.get("weights", {}), dict) else {}
        positive_weight_keys = [
            "commercial_acceleration",
            "upside_capacity",
            "cash_flow_acceleration",
            "survival_quality",
            "governance_event",
            "market_confirmation",
            "catalyst_quality",
        ]
        positive_total = sum(float(weights.get(key, 0.0)) for key in positive_weight_keys)
        if weights and abs(positive_total - 1.0) > 1e-6:
            LOGGER.warning(
                "multibagger positive component weights sum to %.6f; scoring normalizes them to 1.0 at runtime.",
                positive_total,
            )
        max_spec = float(multibagger.get("max_speculative_risk", 75.0))
        avoid = float(multibagger.get("avoid_risk_min", 80.0))
        if avoid <= max_spec:
            LOGGER.warning(
                "multibagger.avoid_risk_min (%s) is <= max_speculative_risk (%s); "
                "boundary-risk names may fall to avoid by default.",
                avoid,
                max_spec,
            )


def parse_clock_time(raw: object, default: str = "16:15") -> dt_time:
    text = str(raw or default).strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Invalid market close time: {raw}")


def previous_business_day(day: date) -> date:
    out = day - timedelta(days=1)
    while out.weekday() >= 5:
        out -= timedelta(days=1)
    return out


def business_day_age(start: date, end: date) -> int:
    """Return weekday-count age from start exclusive to end inclusive."""
    if end < start:
        return -business_day_age(end, start)
    age = 0
    current = start + timedelta(days=1)
    while current <= end:
        if current.weekday() < 5:
            age += 1
        current += timedelta(days=1)
    return age


def default_pipeline_asof(config: dict[str, Any]) -> date:
    market_timezone = str(cfg_get(config, "ib_market_data.market_timezone", "America/New_York"))
    market_close_time = parse_clock_time(cfg_get(config, "ib_market_data.market_close_time", "16:15"))
    guard_enabled = as_bool(cfg_get(config, "ib_market_data.market_close_guard", True))
    now_local = datetime.now(timezone.utc).astimezone(ZoneInfo(market_timezone))
    local_today = now_local.date()
    if guard_enabled and (local_today.weekday() >= 5 or now_local.time() < market_close_time):
        return previous_business_day(local_today)
    return local_today


def pipeline_steps(mode: str, *, skip_ctgov: bool, skip_ib: bool, skip_yahoo: bool, reuse_unchanged_historical: bool = False) -> list[Step]:
    sec_event_args: tuple[str, ...] = ("--full-rescan",) if mode in {"weekly_reconcile", "full_backfill"} else ()
    companyfacts_args: tuple[str, ...] = ("--full-refresh",) if mode == "full_backfill" else ()
    forward_args: tuple[str, ...] = ("--run-mode", mode)
    governance_reuse = reuse_unchanged_historical or mode == "weekly_reconcile"
    governance_args: tuple[str, ...] = ("--reuse-unchanged-historical",) if governance_reuse else ()
    ib_args: tuple[str, ...] = ("--allow-partial",) if mode in {"weekly_reconcile", "full_backfill"} else ()
    yahoo_args: tuple[str, ...] = ("--allow-partial",) if mode in {"weekly_reconcile", "full_backfill"} else ()
    commercial_args: tuple[str, ...] = ("--allow-missing-market",) if mode in {"weekly_reconcile", "full_backfill"} else ()
    multibagger_feature_args: tuple[str, ...] = ("--allow-missing-market",) if mode in {"weekly_reconcile", "full_backfill"} else ()
    # 08_scan_ctgov_reactivation_candidates.py is an audit/discovery utility, not a deterministic refresh step.
    steps = [
        Step("company_master", "02_build_company_master.py", supports_asof=False),
    ]
    if not skip_ctgov:
        steps.extend(
            [
                Step("ctgov_trials", "03_sync_ctgov_trials.py"),
                Step("trial_links", "04_link_trials_to_companies.py", supports_asof=False),
                Step("ctgov_audit", "05_audit_ctgov_trial_links.py"),
            ]
        )
    steps.extend(
        [
            Step("sec_filings", "06_sync_sec_filings.py"),
            Step("sec_events", "07_parse_sec_biotech_events.py", sec_event_args),
            Step("sec_companyfacts", "15_sync_sec_companyfacts_history.py", companyfacts_args),
            Step("financial_survival", "16_build_financial_survival_features.py"),
        ]
    )
    if not skip_ib:
        steps.append(Step("ib_market", "17_sync_market_data_ib.py", ib_args))
    if not skip_yahoo:
        steps.append(Step("yahoo_market_adjusted", "17_sync_market_data_yahoo_adjusted.py", yahoo_args))
    if not (skip_ib and skip_yahoo):
        steps.append(Step("market_policy_audit", "31_audit_market_data_policy.py"))
    steps.extend(
        [
            Step("commercial_value", "18_build_commercial_value_features.py", commercial_args),
            Step("forward_guidance", "19_parse_forward_guidance.py", forward_args),
            Step("governance_events", "20_build_governance_event_features.py", governance_args),
            Step("biotech_features", "10_build_biotech_features.py"),
            Step("biotech_scores", "11_score_biotech_index.py"),
            Step("biotech_reports", "12_publish_biotech_reports.py"),
            Step("multibagger_features", "21_build_multibagger_features.py", multibagger_feature_args),
            Step("multibagger_scores", "22_score_multibagger_candidates.py"),
            Step("multibagger_reports", "23_publish_multibagger_report.py"),
            Step("universe_coverage_audit", "32_audit_biotech_universe_coverage.py"),
        ]
    )
    return steps


def historical_restatement_steps(*, reuse_unchanged_historical: bool = True) -> list[Step]:
    """Derived layers that can be safely restated from already-synced source tables."""
    governance_args: tuple[str, ...] = ("--reuse-unchanged-historical",) if reuse_unchanged_historical else ()
    return [
        Step("financial_survival", "16_build_financial_survival_features.py"),
        Step("commercial_value", "18_build_commercial_value_features.py", ("--allow-missing-market", "--allow-stale-market")),
        Step("forward_guidance", "19_parse_forward_guidance.py", ("--run-mode", "weekly_reconcile")),
        Step("governance_events", "20_build_governance_event_features.py", governance_args),
        Step("biotech_features", "10_build_biotech_features.py"),
        Step("biotech_scores", "11_score_biotech_index.py"),
        Step("multibagger_features", "21_build_multibagger_features.py", ("--allow-missing-market", "--allow-stale-market")),
        Step("multibagger_scores", "22_score_multibagger_candidates.py"),
    ]


def parse_history_dates(raw: str) -> list[str]:
    dates: list[str] = []
    seen: set[str] = set()
    for part in str(raw or "").replace(";", ",").replace("|", ",").split(","):
        parsed = parse_date(part)
        if parsed is None:
            if str(part or "").strip():
                raise ValueError(f"Invalid historical restatement date: {part!r}")
            continue
        text = parsed.isoformat()
        if text not in seen:
            dates.append(text)
            seen.add(text)
    return dates


def load_history_date_grid(
    conn: sqlite3.Connection,
    *,
    source_table: str,
    explicit_dates: str,
    start_asof: str,
    end_asof: str,
    default_end_asof: str,
    fridays_only: bool,
) -> list[str]:
    dates = parse_history_dates(explicit_dates)
    start_date = parse_date(start_asof)
    if start_asof and start_date is None:
        raise ValueError(f"Invalid --history-start-asof date: {start_asof}")
    end_date = parse_date(end_asof) or parse_date(default_end_asof)
    if (end_asof or default_end_asof) and end_date is None:
        raise ValueError(f"Invalid historical restatement end date: {end_asof or default_end_asof}")
    if not dates:
        table_sql = quote_identifier(source_table)
        rows = conn.execute(f"SELECT DISTINCT asof_date FROM {table_sql} WHERE asof_date IS NOT NULL ORDER BY asof_date").fetchall()
        for row in rows:
            parsed = parse_date(row["asof_date"])
            if parsed is None:
                continue
            if start_date is not None and parsed < start_date:
                continue
            if end_date is not None and parsed > end_date:
                continue
            if fridays_only and parsed.weekday() != 4:
                continue
            dates.append(parsed.isoformat())
    else:
        filtered: list[str] = []
        for text in dates:
            parsed = parse_date(text)
            if parsed is None:
                continue
            if start_date is not None and parsed < start_date:
                continue
            if end_date is not None and parsed > end_date:
                continue
            if fridays_only and parsed.weekday() != 4:
                continue
            filtered.append(text)
        dates = filtered
    if not dates:
        raise RuntimeError("Historical restatement date grid is empty.")
    return dates


def write_timing_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["run_started_at", "mode", "step", "status", "elapsed_sec", "returncode", "command", "stdout_tail", "stderr_tail"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows([{field: row.get(field, "") for field in fieldnames} for row in rows])


def text_tail(raw: str, limit: int = 4000) -> str:
    text = str(raw or "").strip()
    return text[-limit:] if len(text) > limit else text


def terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        else:
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(pgid, signal.SIGKILL)
    except Exception as exc:
        LOGGER.warning("Failed to terminate process tree for pid=%s: %s", process.pid, exc)
    try:
        process.kill()
    except Exception:
        pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_direct_output_files(
    config: dict[str, Any],
    *,
    base_dir: Path,
    asof: str,
    run_started_at: str,
    mode: str,
    selected_steps: set[str],
) -> dict[str, Any]:
    start = time.monotonic()
    source_dir = resolve_path(
        cfg_get(config, "biotech_refresh.snapshot_outputs.source_dir", cfg_get(config, "biotech_reports.output_dir", "../output/biotech_index_reports")),
        base_dir=base_dir,
    )
    snapshot_root = resolve_path(
        cfg_get(config, "biotech_refresh.snapshot_outputs.root_dir", str(source_dir)),
        base_dir=base_dir,
    )
    include_extensions = {
        value.lower() if str(value).startswith(".") else f".{str(value).lower()}"
        for value in parse_string_list(
            cfg_get(config, "biotech_refresh.snapshot_outputs.include_extensions", [".csv", ".json"]),
            [".csv", ".json"],
        )
    }
    snapshot_dir = snapshot_root / asof.replace("-", "")
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    run_start = datetime.fromisoformat(run_started_at.replace("Z", "+00:00"))
    manifest_path = snapshot_dir / "snapshot_manifest.json"
    previous_snapshot_names: set[str] = set()
    if manifest_path.exists():
        try:
            previous_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous_manifest = {}
        if isinstance(previous_manifest, dict):
            for item in previous_manifest.get("files", []):
                if isinstance(item, dict) and str(item.get("name") or "").strip():
                    previous_snapshot_names.add(str(item["name"]))
    source_files = [
        source
        for source in sorted(list(source_dir.iterdir()), key=lambda item: item.name.lower())
        if source.is_file() and source.suffix.lower() in include_extensions
    ]
    source_names = {source.name for source in source_files}

    copied_files: list[dict[str, Any]] = []
    for stale in list(snapshot_dir.iterdir()):
        if stale.is_file() and stale.name == "snapshot_manifest.json":
            stale.unlink()
            continue
        if (
            stale.is_file()
            and stale.suffix.lower() in include_extensions
            and stale.name in previous_snapshot_names
            and stale.name not in source_names
            and datetime.fromtimestamp(stale.stat().st_mtime, timezone.utc) < run_start
        ):
            LOGGER.info("Removing stale dated snapshot file not refreshed in this run: %s", stale)
            stale.unlink()

    for source in source_files:
        target = snapshot_dir / source.name
        action = "copied"
        if target.exists() and target.stat().st_mtime >= source.stat().st_mtime:
            action = "kept_existing_newer_or_equal"
        else:
            shutil.copy2(source, target)
        try:
            stat = target.stat()
            digest = file_sha256(target)
        except FileNotFoundError:
            LOGGER.warning("Snapshot target disappeared before hashing: %s", target)
            continue
        copied_files.append(
            {
                "name": target.name,
                "size_bytes": stat.st_size,
                "sha256": digest,
                "last_write_time_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).replace(microsecond=0).isoformat(),
                "snapshot_action": action,
            }
        )

    manifest = {
        "asof_date": asof,
        "created_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "run_started_at_utc": run_started_at,
        "mode": mode,
        "selected_steps": sorted(selected_steps),
        "source_dir": str(source_dir),
        "snapshot_dir": str(snapshot_dir),
        "include_extensions": sorted(include_extensions),
        "file_count": len(copied_files),
        "files": copied_files,
        "notes": [
            "Snapshot folder name is derived from the data as-of date, not the wall-clock run date.",
            "Only direct files in source_dir are copied; subdirectories and calibration folders are not copied.",
            "Only files listed in the previous snapshot manifest are eligible for stale cleanup.",
        ],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")
    stat = manifest_path.stat()
    copied_files.append(
        {
            "name": manifest_path.name,
            "size_bytes": stat.st_size,
            "sha256": file_sha256(manifest_path),
            "last_write_time_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).replace(microsecond=0).isoformat(),
        }
    )
    max_history = int(cfg_get(config, "biotech_refresh.max_snapshot_history", 30) or 0)
    if max_history > 0:
        dated_dirs = sorted(
            [
                path
                for path in snapshot_root.iterdir()
                if path.is_dir() and path.name.isdigit() and len(path.name) == 8
            ],
            key=lambda path: path.name,
            reverse=True,
        )
        for old_dir in dated_dirs[max_history:]:
            LOGGER.info("Pruning old biotech snapshot directory: %s", old_dir)
            shutil.rmtree(old_dir)
    elapsed = round(time.monotonic() - start, 3)
    LOGGER.info(
        "Snapshot outputs written: asof=%s files=%d snapshot_dir=%s elapsed=%.3fs",
        asof,
        len(copied_files),
        snapshot_dir,
        elapsed,
    )
    return {
        "run_started_at": run_started_at,
        "mode": mode,
        "step": "snapshot_outputs",
        "status": "success",
        "elapsed_sec": elapsed,
        "returncode": 0,
        "command": f"snapshot outputs asof={asof} dir={snapshot_dir}",
    }


def build_step_command(
    step: Step,
    *,
    config_path: Path,
    db_path: Path,
    asof: str,
) -> list[str]:
    script_path = Path(__file__).resolve().with_name(step.script)
    cmd = [sys.executable, str(script_path), "--config", str(config_path), "--db", str(db_path), *step.args]
    if asof and step.supports_asof:
        cmd.extend(["--asof", asof])
    return cmd


def run_step(
    step: Step,
    *,
    command: list[str],
    mode: str,
    run_started_at: str,
    timeout_sec: float | None = None,
) -> dict[str, Any]:
    start = time.monotonic()
    LOGGER.info("Starting %s", step.name)
    LOGGER.info("Command for %s: %s", step.name, " ".join(command))
    stdout_text = ""
    stderr_text = ""
    try:
        process = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=sys.platform != "win32",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )
        try:
            stdout_text, stderr_text = process.communicate(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            terminate_process_tree(process)
            try:
                stdout_text, stderr_text = process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                terminate_process_tree(process)
                stdout_text = stdout_text or ""
                stderr_text = (stderr_text or "") + "\nTimed out waiting for process tree termination."
            raise
    except subprocess.TimeoutExpired:
        elapsed = round(time.monotonic() - start, 3)
        LOGGER.error("Step %s timed out after %.3fs", step.name, elapsed)
        return {
            "run_started_at": run_started_at,
            "mode": mode,
            "step": step.name,
            "status": "failed",
            "elapsed_sec": elapsed,
            "returncode": -1,
            "command": " ".join(command),
            "stdout_tail": text_tail(stdout_text),
            "stderr_tail": text_tail(stderr_text),
        }
    elapsed = round(time.monotonic() - start, 3)
    returncode = int(process.returncode if process.returncode is not None else -1)
    status = "success" if returncode == 0 else "failed"
    if stderr_text.strip():
        log_func = LOGGER.warning if status == "success" else LOGGER.error
        log_func("Step %s stderr tail: %s", step.name, text_tail(stderr_text, 1200))
    LOGGER.info("Finished %s status=%s elapsed=%.3fs", step.name, status, elapsed)
    return {
        "run_started_at": run_started_at,
        "mode": mode,
        "step": step.name,
        "status": status,
        "elapsed_sec": elapsed,
        "returncode": returncode,
        "command": " ".join(command),
        "stdout_tail": text_tail(stdout_text),
        "stderr_tail": text_tail(stderr_text),
    }


def connect_form4_readonly(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def read_form4_snapshot_date(conn: sqlite3.Connection, snapshot_table: str) -> tuple[str, str]:
    sources: list[tuple[str, str]] = []
    if snapshot_table:
        sources.append((snapshot_table, "last_index_date"))
    sources.extend(
        [
            ("form4_events_tier1", "filing_date"),
            ("form4_buy_events_v1", "filing_date"),
        ]
    )
    for table, field in sources:
        try:
            table_sql = quote_identifier(table)
            field_sql = quote_identifier(field)
            row = conn.execute(f"SELECT MAX({field_sql}) AS snapshot_date FROM {table_sql}").fetchone()
        except (sqlite3.Error, ValueError) as exc:
            LOGGER.debug("Form 4 snapshot probe skipped source=%s.%s error=%s", table, field, exc)
            continue
        if row and row["snapshot_date"]:
            return str(row["snapshot_date"]), f"{table}.{field}"
    return "", ""


def validate_form4_preflight(
    config: dict[str, Any],
    *,
    base_dir: Path,
    asof: str,
    run_started_at: str,
    mode: str,
) -> dict[str, Any]:
    start = time.monotonic()
    LOGGER.info("Starting form4_preflight")
    target_date = parse_date(asof)
    if target_date is None:
        raise ValueError(f"Invalid pipeline as-of date for Form 4 preflight: {asof}")

    form4_db_path = resolve_path(cfg_get(config, "governance_events.form4_db_path"), base_dir=base_dir)
    snapshot_table = str(cfg_get(config, "governance_events.form4_snapshot_table", "sec_form4_daily_state") or "")
    max_staleness_days = int(
        cfg_get(
            config,
            "biotech_refresh.form4_preflight.max_staleness_days",
            cfg_get(config, "biotech_refresh.max_upstream_staleness_days", 2),
        )
    )
    staleness_day_basis = str(cfg_get(config, "biotech_refresh.form4_preflight.staleness_day_basis", "calendar")).strip().lower()
    if staleness_day_basis not in {"calendar", "business"}:
        raise ValueError(
            "biotech_refresh.form4_preflight.staleness_day_basis must be 'calendar' or 'business', "
            f"got {staleness_day_basis!r}"
        )
    required = as_bool(
        cfg_get(
            config,
            "biotech_refresh.form4_preflight.required",
            cfg_get(config, "governance_events.form4_required", True),
        ),
        True,
    )
    warn_only = as_bool(cfg_get(config, "biotech_refresh.form4_preflight.warn_only", False), False)

    failures: list[str] = []
    warnings: list[str] = []
    snapshot_raw = ""
    snapshot_source = ""
    age_days: int | str = ""

    if max_staleness_days < 0:
        raise ValueError("biotech_refresh.form4_preflight.max_staleness_days must be >= 0")
    if not form4_db_path.exists():
        failures.append(f"Form 4 database not found: {form4_db_path}")
    else:
        try:
            with closing(connect_form4_readonly(form4_db_path)) as conn:
                snapshot_raw, snapshot_source = read_form4_snapshot_date(conn, snapshot_table)
        except sqlite3.Error as exc:
            failures.append(f"Form 4 database cannot be opened read-only: {form4_db_path} ({type(exc).__name__}: {exc})")

    if not failures:
        snapshot_date = parse_date(snapshot_raw)
        if snapshot_date is None:
            failures.append(f"Form 4 snapshot date is unavailable in {form4_db_path}")
        else:
            age_days = (
                business_day_age(snapshot_date, target_date)
                if staleness_day_basis == "business"
                else (target_date - snapshot_date).days
            )
            if isinstance(age_days, int) and age_days < 0:
                failures.append(
                    f"Form 4 snapshot is future-dated: snapshot_date={snapshot_date.isoformat()} "
                    f"asof={target_date.isoformat()} age_days={age_days} basis={staleness_day_basis}"
                )
            elif isinstance(age_days, int) and age_days > max_staleness_days:
                failures.append(
                    f"Form 4 snapshot is stale: snapshot_date={snapshot_date.isoformat()} "
                    f"asof={target_date.isoformat()} age_days={age_days} basis={staleness_day_basis} "
                    f"max_staleness_days={max_staleness_days}"
                )

    if failures and (required and not warn_only):
        raise RuntimeError("Form 4 preflight failed: " + " | ".join(failures))
    if failures:
        warnings.extend(failures)

    elapsed = round(time.monotonic() - start, 3)
    if warnings:
        LOGGER.warning("Finished form4_preflight status=warning elapsed=%.3fs warnings=%s", elapsed, " | ".join(warnings))
        status = "warning"
    else:
        LOGGER.info(
            "Finished form4_preflight status=success elapsed=%.3fs db=%s snapshot_date=%s source=%s age_days=%s",
            elapsed,
            form4_db_path,
            snapshot_raw,
            snapshot_source,
            age_days,
        )
        status = "success"
    return {
        "run_started_at": run_started_at,
        "mode": mode,
        "step": "form4_preflight",
        "status": status,
        "elapsed_sec": elapsed,
        "returncode": 0,
        "command": (
            f"validate Form 4 db={form4_db_path} asof={asof} snapshot_date={snapshot_raw or '<missing>'} "
            f"source={snapshot_source or '<missing>'} age_days={age_days} max_staleness_days={max_staleness_days}"
        ),
    }


def analyze_db(db_path: Path, *, run_started_at: str, mode: str) -> dict[str, Any]:
    start = time.monotonic()
    LOGGER.info("Starting sqlite_optimize")
    with connect(db_path) as conn:
        conn.execute("PRAGMA optimize")
    elapsed = round(time.monotonic() - start, 3)
    LOGGER.info("Finished sqlite_optimize status=success elapsed=%.3fs", elapsed)
    return {
        "run_started_at": run_started_at,
        "mode": mode,
        "step": "sqlite_optimize",
        "status": "success",
        "elapsed_sec": elapsed,
        "returncode": 0,
        "command": f"PRAGMA optimize {db_path}",
    }


def observed_table_tickers(conn: sqlite3.Connection, table: str, *, asof: str, source: str = "") -> list[str]:
    table_sql = quote_identifier(table)
    source_clause = " AND t.source = ?" if source else ""
    params: tuple[Any, ...] = (asof, source) if source else (asof,)
    rows = conn.execute(
        f"""
        SELECT c.ticker
        FROM {table_sql} t
        JOIN companies c ON c.company_id = t.company_id
        WHERE t.asof_date = ?{source_clause}
        """,
        params,
    ).fetchall()
    return [str(row["ticker"] or "") for row in rows]


def validate_table_coverage(
    conn: sqlite3.Connection,
    *,
    table: str,
    asof: str,
    expected_tickers: set[str],
    source: str = "",
    allow_extra: bool = False,
) -> None:
    observed = observed_table_tickers(conn, table, asof=asof, source=source)
    coverage = universe_coverage(expected_tickers, observed)
    failures: list[str] = []
    label = f"{table}{':' + source if source else ''}"
    if coverage.missing_tickers:
        failures.append(f"missing {len(coverage.missing_tickers)}: {format_ticker_sample(coverage.missing_tickers)}")
    if coverage.extra_tickers and not allow_extra:
        failures.append(f"extra {len(coverage.extra_tickers)}: {format_ticker_sample(coverage.extra_tickers)}")
    if failures:
        raise RuntimeError(f"{label} coverage failed for asof={asof}: " + " | ".join(failures))

def is_blank(raw: object) -> bool:
    return raw is None or str(raw).strip() == ""


def validate_table_required_columns(
    conn: sqlite3.Connection,
    *,
    table: str,
    asof: str,
    required_columns: list[str],
) -> None:
    table_sql = quote_identifier(table)
    columns = {
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({table_sql})").fetchall()
    }
    failures: list[str] = []
    missing_columns = [column for column in required_columns if column not in columns]
    if missing_columns:
        failures.append("missing required columns: " + ",".join(missing_columns))

    for column in required_columns:
        if column not in columns:
            continue
        column_sql = quote_identifier(column)
        blank_condition = f"t.{column_sql} IS NULL OR TRIM(CAST(t.{column_sql} AS TEXT)) = ''"
        count_row = conn.execute(
            f"""
            SELECT COUNT(*) AS blank_count
            FROM {table_sql} t
            WHERE t.asof_date = ? AND ({blank_condition})
            """,
            (asof,),
        ).fetchone()
        blank_count = int(count_row["blank_count"] or 0) if count_row else 0
        if blank_count:
            sample_rows = conn.execute(
                f"""
                SELECT c.ticker
                FROM {table_sql} t
                JOIN companies c ON c.company_id = t.company_id
                WHERE t.asof_date = ? AND ({blank_condition})
                ORDER BY c.ticker
                LIMIT 10
                """,
                (asof,),
            ).fetchall()
            sample = ",".join(str(row["ticker"] or "") for row in sample_rows)
            failures.append(f"{column} blank for {blank_count} row(s): {sample}")

    if failures:
        raise RuntimeError(f"{table} required column validation failed for asof={asof}: " + " | ".join(failures))


def validate_paired_score_dates(conn: sqlite3.Connection) -> None:
    daily_dates = {
        str(row["asof_date"] or "")
        for row in conn.execute("SELECT DISTINCT asof_date FROM daily_scores WHERE asof_date IS NOT NULL").fetchall()
    }
    multibagger_dates = {
        str(row["asof_date"] or "")
        for row in conn.execute("SELECT DISTINCT asof_date FROM multibagger_scores_daily WHERE asof_date IS NOT NULL").fetchall()
    }
    daily_only = sorted(daily_dates - multibagger_dates)
    multibagger_only = sorted(multibagger_dates - daily_dates)
    failures: list[str] = []
    if daily_only:
        sample = ",".join(daily_only[:10])
        failures.append(f"daily_scores only {len(daily_only)} date(s): {sample}")
    if multibagger_only:
        sample = ",".join(multibagger_only[:10])
        failures.append(f"multibagger_scores_daily only {len(multibagger_only)} date(s): {sample}")
    if failures:
        raise RuntimeError("Paired score date validation failed: " + " | ".join(failures))


def validate_score_csv(
    path: Path,
    *,
    asof: str,
    expected_tickers: set[str],
    required_columns: list[str],
    present_columns: list[str] | None = None,
) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Required pipeline output CSV not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = [str(field or "") for field in (reader.fieldnames or [])]
    if not rows:
        raise RuntimeError(f"{path} validation failed: score CSV has no data rows")
    observed_tickers = [str(row.get("ticker") or "") for row in rows]
    coverage = universe_coverage(expected_tickers, observed_tickers)
    asof_values = {str(row.get("asof_date") or "") for row in rows}
    failures: list[str] = []
    presence_columns = list(present_columns or [])
    missing_columns = [column for column in [*required_columns, *presence_columns] if column not in fieldnames]
    if missing_columns:
        failures.append("missing required columns: " + ",".join(missing_columns))
    if coverage.missing_tickers:
        failures.append(f"missing {len(coverage.missing_tickers)}: {format_ticker_sample(coverage.missing_tickers)}")
    if coverage.extra_tickers:
        failures.append(f"extra {len(coverage.extra_tickers)}: {format_ticker_sample(coverage.extra_tickers)}")
    if asof_values != {asof}:
        sample = ",".join(sorted(asof_values)[:5])
        failures.append(f"asof_date values are {sample or '<blank>'}, expected {asof}")
    for column in required_columns:
        if column not in fieldnames:
            continue
        blank_tickers = [str(row.get("ticker") or "") for row in rows if is_blank(row.get(column))]
        if blank_tickers:
            sample = ",".join(sorted(blank_tickers)[:10])
            failures.append(f"{column} blank for {len(blank_tickers)} row(s): {sample}")
    if failures:
        raise RuntimeError(f"{path} validation failed: " + " | ".join(failures))


def validate_required_csv_columns(
    path: Path,
    *,
    asof: str,
    required_columns: list[str],
    allow_empty_rows: bool = False,
) -> None:
    validate_required_csv_columns_with_presence(
        path,
        asof=asof,
        required_columns=required_columns,
        allow_empty_rows=allow_empty_rows,
    )


def validate_required_csv_columns_with_presence(
    path: Path,
    *,
    asof: str,
    required_columns: list[str],
    present_columns: list[str] | None = None,
    allow_empty_rows: bool = False,
) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Required pipeline output CSV not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = [str(field or "") for field in (reader.fieldnames or [])]

    failures: list[str] = []
    if not rows and not allow_empty_rows:
        failures.append("CSV has no data rows")
    presence_columns = list(present_columns or [])
    missing_columns = [column for column in [*required_columns, *presence_columns] if column not in fieldnames]
    if missing_columns:
        failures.append("missing required columns: " + ",".join(missing_columns))
    if rows:
        asof_values = {str(row.get("asof_date") or "") for row in rows}
        if asof_values != {asof}:
            sample = ",".join(sorted(asof_values)[:5])
            failures.append(f"asof_date values are {sample or '<blank>'}, expected {asof}")
        for column in required_columns:
            if column not in fieldnames:
                continue
            blank_tickers = [str(row.get("ticker") or "") for row in rows if is_blank(row.get(column))]
            if blank_tickers:
                sample = ",".join(sorted(blank_tickers)[:10])
                failures.append(f"{column} blank for {len(blank_tickers)} row(s): {sample}")
    if failures:
        raise RuntimeError(f"{path} required column validation failed: " + " | ".join(failures))


def validate_final_outputs(
    config: dict[str, Any],
    *,
    base_dir: Path,
    db_path: Path,
    asof: str,
    run_started_at: str,
    mode: str,
) -> dict[str, Any]:
    start = time.monotonic()
    LOGGER.info("Starting final_output_validation")
    universe_csv = resolve_path(
        cfg_get(config, "biotech_features.final_scoring_universe_csv", "../output/biotech_index_reports/ctgov_final_scoring_universe.csv"),
        base_dir=base_dir,
    )
    expected_tickers = read_final_scoring_tickers(universe_csv)
    biotech_output_dir = resolve_path(cfg_get(config, "biotech_scoring.output_dir", "../output/biotech_index_reports"), base_dir=base_dir)
    biotech_reports_output_dir = resolve_path(cfg_get(config, "biotech_reports.output_dir", "../output/biotech_index_reports"), base_dir=base_dir)
    multibagger_output_dir = resolve_path(cfg_get(config, "multibagger.output_dir", "../output/biotech_index_reports"), base_dir=base_dir)
    biotech_scores_csv = output_file_with_dated_fallback(
        biotech_output_dir,
        asof,
        str(cfg_get(config, "biotech_scoring.output_csv", "biotech_daily_scores.csv")),
    )
    biotech_top_candidates_csv = output_file_with_dated_fallback(
        biotech_reports_output_dir,
        asof,
        str(cfg_get(config, "biotech_reports.top_candidates_csv", "biotech_top_candidates.csv")),
    )
    multibagger_scores_csv = multibagger_output_dir / str(cfg_get(config, "multibagger.scores_csv", "biotech_multibagger_scores.csv"))
    multibagger_candidates_csv = multibagger_output_dir / str(cfg_get(config, "multibagger.candidates_csv", "biotech_multibagger_candidates.csv"))
    scoring_sources = scoring_market_sources(config)
    preferred_market_sources = {scoring_sources[0]} if scoring_sources else set()
    multibagger_required_columns = list(MULTIBAGGER_SCORE_BASE_REQUIRED_COLUMNS)
    if as_bool(cfg_get(config, "multibagger.tier1_interaction.enabled", False)):
        multibagger_required_columns.extend(MULTIBAGGER_SCORE_TIER1_REQUIRED_COLUMNS)
    with connect(db_path) as conn:
        for table in (
            "financial_survival_features",
            "commercial_value_features_daily",
            "forward_guidance_features_daily",
            "governance_event_features_daily",
            "daily_features",
            "daily_scores",
            "multibagger_features_daily",
            "multibagger_scores_daily",
        ):
            validate_table_coverage(conn, table=table, asof=asof, expected_tickers=expected_tickers)
        for source in sorted(source for source in preferred_market_sources if source):
            # Market features can include extra symbols from the vendor cache; downstream layers filter to the final universe.
            validate_table_coverage(
                conn,
                table="market_features_daily",
                asof=asof,
                expected_tickers=expected_tickers,
                source=source,
                allow_extra=True,
            )
        validate_table_required_columns(
            conn,
            table="daily_scores",
            asof=asof,
            required_columns=BIOTECH_SCORE_REQUIRED_COLUMNS,
        )
        validate_table_required_columns(
            conn,
            table="multibagger_scores_daily",
            asof=asof,
            required_columns=multibagger_required_columns,
        )
        validate_paired_score_dates(conn)
    validate_score_csv(
        biotech_scores_csv,
        asof=asof,
        expected_tickers=expected_tickers,
        required_columns=BIOTECH_SCORE_CSV_REQUIRED_COLUMNS,
        present_columns=BIOTECH_SCORE_CSV_PRESENT_COLUMNS,
    )
    validate_required_csv_columns_with_presence(
        biotech_top_candidates_csv,
        asof=asof,
        required_columns=BIOTECH_SCORE_CSV_REQUIRED_COLUMNS,
        present_columns=BIOTECH_SCORE_CSV_PRESENT_COLUMNS,
    )
    validate_score_csv(
        multibagger_scores_csv,
        asof=asof,
        expected_tickers=expected_tickers,
        required_columns=multibagger_required_columns,
    )
    validate_required_csv_columns(
        multibagger_candidates_csv,
        asof=asof,
        required_columns=multibagger_required_columns,
        allow_empty_rows=True,
    )
    elapsed = round(time.monotonic() - start, 3)
    LOGGER.info("Finished final_output_validation status=success elapsed=%.3fs expected_tickers=%d", elapsed, len(expected_tickers))
    return {
        "run_started_at": run_started_at,
        "mode": mode,
        "step": "final_output_validation",
        "status": "success",
        "elapsed_sec": elapsed,
        "returncode": 0,
        "command": f"validate outputs asof={asof}",
    }


def main() -> None:
    configure_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    validate_config(config)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    if args.asof:
        parsed_asof = parse_date(args.asof)
        if parsed_asof is None:
            raise ValueError(f"Invalid --asof date: {args.asof}")
        asof = parsed_asof.isoformat()
    else:
        asof = default_pipeline_asof(config).isoformat()
    LOGGER.info("Pipeline as-of date: %s", asof)
    timing_csv = resolve_path(
        cfg_get(config, "biotech_refresh.timing_csv", "../output/biotech_index_reports/biotech_refresh_timing.csv"),
        base_dir=base_dir,
    )
    raw_timeout_value = cfg_get(config, "biotech_refresh.step_timeout_sec", 14400.0)
    try:
        raw_timeout = float(raw_timeout_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid biotech_refresh.step_timeout_sec value: {raw_timeout_value!r}") from exc
    step_timeout_sec = raw_timeout if raw_timeout > 0 else None
    selected_steps = {step.strip() for step in args.steps.split(",") if step.strip()}
    all_steps = (
        historical_restatement_steps(reuse_unchanged_historical=True)
        if args.history_restatement
        else pipeline_steps(
            args.mode,
            skip_ctgov=args.skip_ctgov,
            skip_ib=args.skip_ib,
            skip_yahoo=args.skip_yahoo,
            reuse_unchanged_historical=args.reuse_unchanged_historical,
        )
    )
    if selected_steps:
        known = {step.name for step in all_steps}
        unknown = sorted(selected_steps - known)
        if unknown:
            raise ValueError(f"Unknown pipeline step(s): {', '.join(unknown)}")
    steps = [step for step in all_steps if not selected_steps or step.name in selected_steps]
    history_dates: list[str] = []
    if args.history_restatement:
        with connect(db_path) as conn:
            history_dates = load_history_date_grid(
                conn,
                source_table=args.history_date_source,
                explicit_dates=args.history_dates,
                start_asof=args.history_start_asof,
                end_asof=args.history_end_asof,
                default_end_asof=asof,
                fridays_only=args.history_fridays_only,
            )
        LOGGER.info(
            "Historical restatement date grid: dates=%d first=%s last=%s source=%s",
            len(history_dates),
            history_dates[0],
            history_dates[-1],
            "explicit" if args.history_dates else args.history_date_source,
        )
    final_validation_enabled = as_bool(cfg_get(config, "biotech_refresh.validate_final_outputs", True))
    snapshot_outputs_enabled = as_bool(cfg_get(config, "biotech_refresh.snapshot_outputs.enabled", True))
    form4_preflight_enabled = as_bool(cfg_get(config, "biotech_refresh.form4_preflight.enabled", True), True)
    form4_warning_is_fatal = as_bool(cfg_get(config, "biotech_refresh.form4_preflight.warning_is_fatal", True), True)
    form4_preflight_needed = any(step.name == "governance_events" for step in steps)

    run_started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    timing_rows: list[dict[str, Any]] = []
    try:
        if form4_preflight_needed and form4_preflight_enabled and not args.skip_form4_preflight:
            preflight_start = time.monotonic()
            timing_rows.append(
                {
                    "run_started_at": run_started_at,
                    "mode": args.mode,
                    "step": "form4_preflight",
                    "status": "running",
                    "elapsed_sec": "",
                    "returncode": "",
                    "command": f"validate Form 4 db freshness asof={asof}",
                }
            )
            write_timing_csv(timing_csv, timing_rows)
            try:
                timing_rows[-1] = validate_form4_preflight(
                    config,
                    base_dir=base_dir,
                    asof=asof,
                    run_started_at=run_started_at,
                    mode=args.mode,
                )
            except Exception as exc:
                timing_rows[-1] = {
                    "run_started_at": run_started_at,
                    "mode": args.mode,
                    "step": "form4_preflight",
                    "status": "failed",
                    "elapsed_sec": round(time.monotonic() - preflight_start, 3),
                    "returncode": 1,
                    "command": f"validate Form 4 db freshness asof={asof}: {type(exc).__name__}: {exc}",
                }
                write_timing_csv(timing_csv, timing_rows)
                raise
            if timing_rows[-1]["status"] == "warning" and form4_warning_is_fatal:
                timing_rows[-1] = {
                    **timing_rows[-1],
                    "status": "failed",
                    "returncode": 1,
                    "command": str(timing_rows[-1].get("command") or "") + " warning_is_fatal=true",
                }
                write_timing_csv(timing_csv, timing_rows)
                raise SystemExit(1)
            write_timing_csv(timing_csv, timing_rows)
        elif form4_preflight_needed and args.skip_form4_preflight:
            LOGGER.warning("Form 4 preflight skipped via --skip-form4-preflight.")
        elif form4_preflight_needed and not form4_preflight_enabled:
            LOGGER.warning("Form 4 preflight skipped because biotech_refresh.form4_preflight.enabled=false.")
        run_dates = history_dates if args.history_restatement else [asof]
        effective_mode = "history_restatement" if args.history_restatement else args.mode
        for run_asof in run_dates:
            for step in steps:
                command_step = step
                timing_step = step
                if args.history_restatement:
                    timing_step = Step(f"{step.name}@{run_asof}", step.script, step.args, step.supports_asof)
                command = build_step_command(command_step, config_path=config_path, db_path=db_path, asof=run_asof)
                row = {
                    "run_started_at": run_started_at,
                    "mode": effective_mode,
                    "step": timing_step.name,
                    "status": "running",
                    "elapsed_sec": "",
                    "returncode": "",
                    "command": " ".join(command),
                }
                timing_rows.append(row)
                write_timing_csv(timing_csv, timing_rows)
                timing_rows[-1] = run_step(
                    timing_step,
                    command=command,
                    mode=effective_mode,
                    run_started_at=run_started_at,
                    timeout_sec=step_timeout_sec,
                )
                write_timing_csv(timing_csv, timing_rows)
                if timing_rows[-1]["status"] != "success":
                    step_returncode = int(timing_rows[-1]["returncode"])
                    raise SystemExit(step_returncode if step_returncode > 0 else 1)
        if not args.skip_analyze:
            timing_rows.append(analyze_db(db_path, run_started_at=run_started_at, mode=effective_mode))
            write_timing_csv(timing_csv, timing_rows)
        if (
            not args.history_restatement
            and not selected_steps
            and not args.skip_final_validation
            and final_validation_enabled
        ):
            validation_start = time.monotonic()
            timing_rows.append(
                {
                    "run_started_at": run_started_at,
                    "mode": args.mode,
                    "step": "final_output_validation",
                    "status": "running",
                    "elapsed_sec": "",
                    "returncode": "",
                    "command": f"validate outputs asof={asof}",
                }
            )
            write_timing_csv(timing_csv, timing_rows)
            try:
                timing_rows[-1] = validate_final_outputs(
                    config,
                    base_dir=base_dir,
                    db_path=db_path,
                    asof=asof,
                    run_started_at=run_started_at,
                    mode=args.mode,
                )
            except Exception as exc:
                timing_rows[-1] = {
                    "run_started_at": run_started_at,
                    "mode": args.mode,
                    "step": "final_output_validation",
                    "status": "failed",
                    "elapsed_sec": round(time.monotonic() - validation_start, 3),
                    "returncode": 1,
                    "command": f"validate outputs asof={asof}: {type(exc).__name__}: {exc}",
                }
                write_timing_csv(timing_csv, timing_rows)
                raise
            write_timing_csv(timing_csv, timing_rows)
        elif selected_steps:
            LOGGER.warning("Final output validation skipped because --steps was used.")
        elif args.skip_final_validation:
            LOGGER.warning("Final output validation SKIPPED via --skip-final-validation; output may be unvalidated.")
        elif not final_validation_enabled:
            LOGGER.warning("Final output validation skipped because biotech_refresh.validate_final_outputs=false.")
        if snapshot_outputs_enabled and not args.history_restatement and not selected_steps:
            timing_rows.append(
                snapshot_direct_output_files(
                    config,
                    base_dir=base_dir,
                    asof=asof,
                    run_started_at=run_started_at,
                    mode=args.mode,
                    selected_steps=selected_steps,
                )
            )
            write_timing_csv(timing_csv, timing_rows)
        elif snapshot_outputs_enabled and args.history_restatement:
            LOGGER.warning("Snapshot output copy skipped during historical restatement.")
        elif snapshot_outputs_enabled and selected_steps:
            LOGGER.warning("Snapshot output copy skipped because --steps was used.")
    finally:
        write_timing_csv(timing_csv, timing_rows)


if __name__ == "__main__":
    main()

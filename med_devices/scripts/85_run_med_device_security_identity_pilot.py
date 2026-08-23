#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.security_identity import parse_iso_date  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_ASOFS = "2019-01-04,2020-10-02,2025-07-31,2026-07-21,2026-08-21"
PATH_KEY_SUFFIXES = ("_csv", "_dir", "_path", "_file", "_registry")
INPUT_PATH_KEYS = {
    "component_ic_csv",
    "current_scores_csv",
    "frozen_baseline_csv",
    "input_csv",
    "policy_csv",
    "source_csv",
    "source_mapping_csv",
    "technical_component_ic_csv",
    "tickers_csv",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run an isolated five-date med-device history pilot with security-identity assertions."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asofs", default=DEFAULT_ASOFS)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument(
        "--pilot-db-dir",
        type=Path,
        default=None,
        help="Local directory for the temporary SQLite backup. Defaults beside the source database.",
    )
    parser.add_argument("--keep-pilot-db", action="store_true")
    return parser.parse_args()


def parse_asofs(raw: str) -> list[str]:
    values = sorted({item.strip() for item in str(raw or "").split(",") if item.strip()})
    if not values or any(parse_iso_date(value) is None for value in values):
        raise ValueError("--asofs must contain valid comma-separated ISO dates")
    return values


def is_path_key(key: str) -> bool:
    normalized = str(key or "").strip().lower()
    return normalized in {"database_path", "seed_csv", "output_csv"} or normalized.endswith(PATH_KEY_SUFFIXES)


def isolated_config(
    node: Any,
    *,
    original_base: Path,
    production_reports_root: Path,
    pilot_root: Path,
    key: str = "",
) -> Any:
    if isinstance(node, dict):
        return {
            item_key: isolated_config(
                item_value,
                original_base=original_base,
                production_reports_root=production_reports_root,
                pilot_root=pilot_root,
                key=str(item_key),
            )
            for item_key, item_value in node.items()
        }
    if isinstance(node, list):
        return [
            isolated_config(
                item,
                original_base=original_base,
                production_reports_root=production_reports_root,
                pilot_root=pilot_root,
                key=key,
            )
            for item in node
        ]
    if not isinstance(node, str) or not is_path_key(key):
        return node
    raw = node.strip()
    if not raw or raw.lower() == "none" or "://" in raw:
        return node
    resolved = resolve_path(raw, base_dir=original_base)
    if key.strip().lower() in INPUT_PATH_KEYS or key.strip().lower().startswith("source_"):
        return str(resolved)
    try:
        relative_report_path = resolved.relative_to(production_reports_root)
    except ValueError:
        return str(resolved)
    return str(pilot_root / "reports" / relative_report_path)


def backup_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    destination_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(destination_conn)
    finally:
        destination_conn.close()
        source_conn.close()


def set_nested(config: dict[str, Any], section: str, key: str, value: Any) -> None:
    current = config.setdefault(section, {})
    if not isinstance(current, dict):
        raise ValueError(f"Config section must be a mapping: {section}")
    current[key] = value


def write_csv_rows(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0]) if rows else []
    with path.open("w", encoding="utf-8", newline="") as handle:
        if not fieldnames:
            return
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def run_snapshot_oos_validations(
    *,
    config_path: Path,
    review_pack_root: Path,
    output_root: Path,
    asofs: list[str],
) -> dict[str, Any]:
    validation_root = output_root / "oos_validation"
    by_asof_root = validation_root / "by_asof"
    strict_rows: list[dict[str, str]] = []
    diagnostic_rows: list[dict[str, str]] = []
    critical_failures = 0
    checks_by_asof: dict[str, int] = {}
    for asof in asofs:
        output_csv = by_asof_root / f"pilot_oos_validation_{asof}.csv"
        diagnostic_csv = by_asof_root / f"pilot_oos_validation_{asof}_diagnostic.csv"
        command = [
            sys.executable,
            str(PACKAGE_ROOT / "scripts" / "75_validate_med_device_historical_snapshot_oos.py"),
            "--config",
            str(config_path),
            "--start-asof",
            asof,
            "--end-asof",
            asof,
            "--reports-root",
            str(review_pack_root),
            "--output-csv",
            str(output_csv),
            "--diagnostic-output-csv",
            str(diagnostic_csv),
        ]
        subprocess.run(command, cwd=PROJECT_ROOT, check=True)
        with output_csv.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        checks_by_asof[asof] = len(rows)
        critical_failures += sum(
            1
            for row in rows
            if str(row.get("severity") or "").upper() == "CRITICAL"
            and str(row.get("status") or "").upper() != "PASS"
        )
        strict_rows.extend(rows)
        if diagnostic_csv.exists():
            with diagnostic_csv.open("r", encoding="utf-8-sig", newline="") as handle:
                diagnostic_rows.extend(csv.DictReader(handle))
    if critical_failures:
        raise RuntimeError(f"Per-snapshot OOS pilot validation found {critical_failures} critical failures")
    write_csv_rows(validation_root / "pilot_oos_validation.csv", strict_rows)
    write_csv_rows(validation_root / "pilot_oos_validation_diagnostic.csv", diagnostic_rows)
    return {
        "validated_asofs": asofs,
        "checks_by_asof": checks_by_asof,
        "critical_failures": critical_failures,
        "output_csv": str(validation_root / "pilot_oos_validation.csv"),
        "diagnostic_output_csv": str(validation_root / "pilot_oos_validation_diagnostic.csv"),
    }


def assert_pilot_outputs(
    *,
    config: dict[str, Any],
    review_pack_root: Path,
    asofs: list[str],
) -> dict[str, Any]:
    identity_specs = cfg_get(config, "universe_validation.security_identity_overrides", {})
    identity_specs = identity_specs if isinstance(identity_specs, dict) else {}
    checked_rows = 0
    violations: list[str] = []
    row_counts: dict[str, int] = {}
    for asof in asofs:
        path = review_pack_root / asof / "med_device_daily_composite_scores.csv"
        if not path.exists():
            violations.append(f"{asof}:missing_composite_csv")
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        row_counts[asof] = len(rows)
        for row in rows:
            ticker = str(row.get("ticker") or "").strip().upper()
            spec = identity_specs.get(ticker)
            if not isinstance(spec, dict):
                continue
            listing_start = str(spec.get("listing_start_date") or "")[:10]
            if not listing_start or asof >= listing_start:
                continue
            checked_rows += 1
            eligible = str(row.get("stage11_calibration_input_eligible_flag") or "0").strip()
            if eligible not in {"", "0", "0.0", "false", "False"}:
                violations.append(f"{asof}:{ticker}:prelisting_stage11_eligible={eligible}")
    if violations:
        raise RuntimeError("Pilot identity assertions failed: " + ";".join(violations[:20]))
    return {
        "asofs": asofs,
        "row_counts": row_counts,
        "prelisting_rows_checked": checked_rows,
        "identity_assertion_failures": 0,
    }


def main() -> None:
    args = parse_args()
    asofs = parse_asofs(args.asofs)
    config_path = args.config.expanduser().resolve()
    source_config = load_yaml(config_path)
    source_db = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(source_config, "paths.database_path"), base_dir=config_path.parent)
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_root = (
        args.output_root.expanduser().resolve()
        if args.output_root
        else PROJECT_ROOT / "output" / "med_devices_reports" / "security_identity" / "pilot" / timestamp
    )
    output_root.mkdir(parents=True, exist_ok=True)
    pilot_db_dir = (
        args.pilot_db_dir.expanduser().resolve()
        if args.pilot_db_dir
        else source_db.parent / "med_device_pilots"
    )
    pilot_db_dir.mkdir(parents=True, exist_ok=True)
    pilot_db = pilot_db_dir / f"med_devices_identity_pilot_{timestamp}.sqlite"
    production_reports_root = PROJECT_ROOT / "output" / "med_devices_reports"
    pilot_config = isolated_config(
        source_config,
        original_base=config_path.parent,
        production_reports_root=production_reports_root,
        pilot_root=output_root,
    )
    if not isinstance(pilot_config, dict):
        raise ValueError("Top-level config must be a mapping")
    set_nested(pilot_config, "paths", "database_path", str(pilot_db))
    review_pack_root = output_root / "reports" / "score_review_pack"
    set_nested(pilot_config, "scoring", "review_pack_dir", str(review_pack_root))
    set_nested(
        pilot_config,
        "historical_backfill",
        "manifest_csv",
        str(output_root / "manifest" / "pilot_manifest.csv"),
    )
    set_nested(
        pilot_config,
        "historical_backfill",
        "oos_validation_csv",
        str(output_root / "oos_validation" / "pilot_oos_validation.csv"),
    )
    set_nested(pilot_config, "historical_backfill", "run_setup", False)
    set_nested(pilot_config, "historical_backfill", "run_backtest", False)
    set_nested(pilot_config, "historical_backfill", "run_calibration", False)
    set_nested(pilot_config, "historical_backfill", "publish_review_packs", True)
    set_nested(pilot_config, "historical_backfill", "run_oos_validation", False)
    set_nested(pilot_config, "historical_backfill", "start_asof", min(asofs))
    set_nested(pilot_config, "historical_backfill", "end_asof", max(asofs))
    pilot_config_path = output_root / "pilot_config.yaml"
    pilot_config_path.write_text(
        yaml.safe_dump(pilot_config, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    backup_database(source_db, pilot_db)
    command = [
        sys.executable,
        str(PACKAGE_ROOT / "scripts" / "21_backfill_med_device_historical_scores.py"),
        "--config",
        str(pilot_config_path),
        "--db",
        str(pilot_db),
        "--asof-list",
        ",".join(asofs),
        "--force",
        "--no-run-setup",
        "--no-run-backtest",
        "--no-run-calibration",
        "--publish-review-packs",
        "--no-run-oos-validation",
    ]
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)
    oos_validation = run_snapshot_oos_validations(
        config_path=pilot_config_path,
        review_pack_root=review_pack_root,
        output_root=output_root,
        asofs=asofs,
    )
    summary = assert_pilot_outputs(
        config=pilot_config,
        review_pack_root=review_pack_root,
        asofs=asofs,
    )
    pilot_db_size_bytes = pilot_db.stat().st_size
    summary.update(
        {
            "source_database": str(source_db),
            "pilot_database": str(pilot_db),
            "pilot_database_size_bytes": pilot_db_size_bytes,
            "pilot_database_retained": bool(args.keep_pilot_db),
            "pilot_config": str(pilot_config_path),
            "review_pack_root": str(review_pack_root),
            "oos_validation": oos_validation,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    summary_path = output_root / "pilot_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    latest_pointer = output_root.parent / "latest.json"
    latest_pointer.write_text(
        json.dumps({"pilot_root": str(output_root), "summary": str(summary_path)}, indent=2),
        encoding="utf-8",
    )
    if not args.keep_pilot_db:
        pilot_db.unlink()
        for suffix in ("-journal", "-shm", "-wal"):
            pilot_db.with_name(f"{pilot_db.name}{suffix}").unlink(missing_ok=True)
    print(f"security_identity_pilot={summary_path} asofs={len(asofs)} failures=0")


if __name__ == "__main__":
    raise SystemExit(main())

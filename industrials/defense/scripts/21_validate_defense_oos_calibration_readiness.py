#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.rank_table_contracts import defense_final_rank_header  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.defense.research_artifacts import load_production_lock, lock_mode_for_asof  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
MODEL_FAMILY = "defense"
PANEL_SOURCE_CURRENT_UNIVERSE_REPLAY = "dashboard_rank_snapshot_current_universe_replay"
PANEL_SOURCE_SURVIVORSHIP_CORRECTED = "survivorship_corrected_pit_membership_score_recompute"
ALLOWED_STAGE11_PANEL_SOURCES = {
    PANEL_SOURCE_CURRENT_UNIVERSE_REPLAY,
    PANEL_SOURCE_SURVIVORSHIP_CORRECTED,
}
REPORT_FIELDS = [
    "asof_date",
    "snapshot_dir",
    "status",
    "rows",
    "sha256",
    "manifest_ok",
    "schema_ok",
    "score_units_ok",
    "shadow_gates_ok",
    "pit_dates_ok",
    "adapter_shadow_ok",
    "portfolio_candidate_rows",
    "oos_valid_rows",
    "research_eligible_rows",
    "max_source_date",
    "issues",
]
SHADOW_ZERO_FIELDS = [
    "portfolio_candidate_gate",
    "oos_score_valid_flag",
    "calibration_eligible_flag",
    "research_calibration_input_eligible_flag",
    "research_calibration_eligible_flag",
    "stage11_calibration_input_eligible_flag",
]
SHADOW_ONE_FIELDS = [
    "feature_point_in_time_flag",
    "future_return_excluded_flag",
    "non_point_in_time_sections_omitted_flag",
    "scoring_weights_frozen_flag",
]
PIT_DATE_FIELDS = [
    "source_snapshot_asof_date",
    "price_data_asof_date",
    "latest_price_date",
    "oos_score_asof_date",
    "market_feature_asof_date",
    "financial_feature_asof_date",
    "financial_data_asof_date",
    "positioning_feature_asof_date",
    "feature_data_asof_date",
    "latest_sec_filing_date",
    "short_interest_asof_date",
    "institutional_data_asof_date",
    "insider_data_asof_date",
    "borrow_data_asof_date",
    "forward_catalyst_asof_date",
]


@dataclass(frozen=True)
class SnapshotCheck:
    asof_date: str
    snapshot_dir: Path
    status: str
    rows: int
    sha256: str
    manifest_ok: bool
    schema_ok: bool
    score_units_ok: bool
    shadow_gates_ok: bool
    pit_dates_ok: bool
    adapter_shadow_ok: bool
    portfolio_candidate_rows: int
    oos_valid_rows: int
    research_eligible_rows: int
    max_source_date: str
    issues: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate defense shadow snapshots for Stage 8 OOS calibration readiness.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", default="", help="Validate a single snapshot date.")
    parser.add_argument("--start-date", default="", help="Optional inclusive snapshot start date.")
    parser.add_argument("--end-date", default="", help="Optional inclusive snapshot end date.")
    parser.add_argument("--snapshot-root", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument(
        "--promotion-check",
        action="store_true",
        help="Fail when the configured minimum snapshot count is not yet available.",
    )
    parser.add_argument("--skip-portfolio-adapter", action="store_true")
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Invalid date value: {raw!r}") from exc


def parse_snapshot_date(path: Path) -> date | None:
    try:
        return datetime.strptime(path.name, "%Y-%m-%d").date()
    except ValueError:
        return None


def expected_header() -> list[str]:
    return defense_final_rank_header(PROJECT_ROOT)


def as_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value == value and value not in (float("inf"), float("-inf")) else None


def truthy_count(rows: list[dict[str, str]], field: str) -> int:
    return sum(1 for row in rows if str(row.get(field) or "").strip() not in {"", "0", "0.0"})


def snapshot_dirs(root: Path, *, asof: date | None, start: date | None, end: date | None) -> list[Path]:
    if asof is not None:
        return [root / asof.isoformat()]
    dirs: list[Path] = []
    if not root.exists():
        return dirs
    for path in root.iterdir():
        if not path.is_dir():
            continue
        parsed = parse_snapshot_date(path)
        if parsed is None:
            continue
        if start is not None and parsed < start:
            continue
        if end is not None and parsed > end:
            continue
        dirs.append(path)
    return sorted(dirs, key=lambda item: item.name)


def validate_manifest(
    csv_path: Path,
    manifest_path: Path,
    *,
    asof: str,
    rows: int,
    expected_score_version: str,
    expected_shadow_only: bool,
) -> tuple[bool, str, list[str]]:
    issues: list[str] = []
    if not csv_path.exists():
        return False, "", [f"missing rank table: {csv_path}"]
    digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    if not manifest_path.exists():
        return False, digest, [f"missing manifest: {manifest_path}"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return False, digest, [f"manifest JSON invalid: {exc}"]
    if manifest.get("sha256") != digest:
        issues.append("manifest sha256 mismatch")
    if manifest.get("asof_date") != asof:
        issues.append("manifest asof_date mismatch")
    if manifest.get("model_family") != MODEL_FAMILY:
        issues.append("manifest model_family mismatch")
    if manifest.get("shadow_only") is not expected_shadow_only:
        issues.append(f"manifest shadow_only is not {expected_shadow_only}")
    if int(manifest.get("rows") or -1) != rows:
        issues.append("manifest row count mismatch")
    if expected_score_version and manifest.get("score_model_version") != expected_score_version:
        issues.append("manifest score_model_version mismatch")
    return not issues, digest, issues


def run_adapter_shadow_check(asof: str) -> tuple[bool, str]:
    script = PROJECT_ROOT / "industrials" / "defense" / "scripts" / "20_validate_defense_portfolio_adapter_shadow.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--asof", asof],
        cwd=str(PROJECT_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    message = (completed.stdout + completed.stderr).strip().replace("\n", " | ")
    return completed.returncode == 0, message


def validate_snapshot(
    path: Path,
    *,
    exp_header: list[str],
    expected_score_version: str,
    run_adapter: bool,
    mode: str,
) -> SnapshotCheck:
    asof_date = path.name
    csv_path = path / "defense_final_rank_table.csv"
    manifest_path = path / "defense_final_rank_table_manifest.json"
    issues: list[str] = []
    rows: list[dict[str, str]] = []
    header: list[str] = []
    if csv_path.exists():
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            header = list(reader.fieldnames or [])
            rows = [{str(k): str(v or "") for k, v in row.items()} for row in reader]
    else:
        issues.append(f"missing rank table: {csv_path}")
    schema_ok = header == exp_header
    if not schema_ok:
        issues.append("schema mismatch versus semiconductor contract with defense demand-pillar rename")
    manifest_ok, digest, manifest_issues = validate_manifest(
        csv_path,
        manifest_path,
        asof=asof_date,
        rows=len(rows),
        expected_score_version=expected_score_version,
        expected_shadow_only=mode != "production",
    )
    issues.extend(manifest_issues)

    bad_asof = [row.get("ticker", "") for row in rows if row.get("asof_date") != asof_date]
    if bad_asof:
        issues.append(f"row asof_date mismatch: {bad_asof[:10]}")
    bad_scores = [
        row.get("ticker", "")
        for row in rows
        if (score := as_float(row.get("final_score"))) is None or score < 0.0 or score > 100.0
    ]
    score_units_ok = not bad_scores
    if bad_scores:
        issues.append(f"final_score outside 0..100: {bad_scores[:10]}")

    if mode == "production":
        zero_fields: list[str] = []
        allowed_roles = {"strict_oos", "excluded"}
    elif mode == "pre_lock":
        zero_fields = ["portfolio_candidate_gate", "oos_score_valid_flag", "calibration_eligible_flag"]
        allowed_roles = {"pre_lock_research", "excluded"}
    else:
        zero_fields = list(SHADOW_ZERO_FIELDS)
        allowed_roles = {"excluded"}
    bad_shadow_zero = [
        row.get("ticker", "")
        for row in rows
        if any(str(row.get(field) or "").strip() != "0" for field in zero_fields)
    ]
    bad_shadow_one = [
        row.get("ticker", "")
        for row in rows
        if any(str(row.get(field) or "").strip() != "1" for field in SHADOW_ONE_FIELDS)
    ]
    bad_sample_role = [
        row.get("ticker", "")
        for row in rows
        if str(row.get("calibration_sample_role") or "") not in allowed_roles
    ]
    bad_production_gate = (
        [
            row.get("ticker", "")
            for row in rows
            if str(row.get("portfolio_candidate_gate") or "") == "1"
            and str(row.get("oos_score_valid_flag") or "") != "1"
        ]
        if mode == "production"
        else []
    )
    shadow_gates_ok = not bad_shadow_zero and not bad_shadow_one and not bad_sample_role and not bad_production_gate
    if bad_shadow_zero:
        issues.append(f"{mode} zero-gate fields not pinned: {bad_shadow_zero[:10]}")
    if bad_shadow_one:
        issues.append(f"PIT one-gate fields not pinned: {bad_shadow_one[:10]}")
    if bad_sample_role:
        issues.append(f"calibration_sample_role outside {sorted(allowed_roles)}: {bad_sample_role[:10]}")
    if bad_production_gate:
        issues.append(f"candidate gate open without oos validity: {bad_production_gate[:10]}")
    bad_research_alias = [
        row.get("ticker", "")
        for row in rows
        if str(row.get("research_calibration_eligible_flag") or "")
        != str(row.get("research_calibration_input_eligible_flag") or "")
    ]
    if bad_research_alias:
        issues.append(f"research_calibration_eligible_flag mismatch: {bad_research_alias[:10]}")
    bad_stage11_source = [
        row.get("ticker", "")
        for row in rows
        if str(row.get("stage11_calibration_panel_source") or "")
        not in ALLOWED_STAGE11_PANEL_SOURCES
    ]
    if bad_stage11_source:
        issues.append(f"stage11_calibration_panel_source not explicit: {bad_stage11_source[:10]}")
    blank_market_cap = [
        row.get("ticker", "")
        for row in rows
        if not str(row.get("market_cap") or "").strip()
        and "market_cap_unavailable" not in str(row.get("liquidity_capacity_reason") or "")
    ]
    if blank_market_cap:
        issues.append(f"market_cap blank without unavailable reason in published rank table: {blank_market_cap[:10]}")
    blank_adv60 = [row.get("ticker", "") for row in rows if not str(row.get("avg_dollar_volume_60d") or "").strip()]
    if blank_adv60:
        issues.append(f"avg_dollar_volume_60d blank in published rank table: {blank_adv60[:10]}")
    missing_capacity_reason = [
        row.get("ticker", "")
        for row in rows
        if (
            (not str(row.get("market_cap") or "").strip() and "market_cap_unavailable" not in str(row.get("liquidity_capacity_reason") or ""))
            or (
                not str(row.get("avg_dollar_volume_60d") or "").strip()
                and "avg_dollar_volume_60d_unavailable" not in str(row.get("liquidity_capacity_reason") or "")
            )
        )
    ]
    if missing_capacity_reason:
        issues.append(f"blank capacity fields missing liquidity_capacity_reason: {missing_capacity_reason[:10]}")

    asof = parse_date(asof_date)
    max_source_date = ""
    future_date_violations: list[str] = []
    for row in rows:
        ticker = row.get("ticker", "")
        for field in PIT_DATE_FIELDS:
            raw = str(row.get(field) or "").strip()
            if not raw:
                continue
            parsed = parse_date(raw)
            if parsed is None:
                future_date_violations.append(f"{ticker}:{field}=unparseable:{raw}")
                continue
            if max_source_date == "" or parsed.isoformat() > max_source_date:
                max_source_date = parsed.isoformat()
            if asof is not None and parsed > asof:
                future_date_violations.append(f"{ticker}:{field}={parsed.isoformat()}")
    pit_dates_ok = not future_date_violations
    if future_date_violations:
        issues.append(f"source date after snapshot asof: {future_date_violations[:10]}")

    adapter_ok = True
    if run_adapter:
        adapter_ok, adapter_message = run_adapter_shadow_check(asof_date)
        if not adapter_ok:
            issues.append(f"portfolio adapter shadow validation failed: {adapter_message[:500]}")

    status = "pass" if not issues else "fail"
    return SnapshotCheck(
        asof_date=asof_date,
        snapshot_dir=path,
        status=status,
        rows=len(rows),
        sha256=digest,
        manifest_ok=manifest_ok,
        schema_ok=schema_ok,
        score_units_ok=score_units_ok,
        shadow_gates_ok=shadow_gates_ok,
        pit_dates_ok=pit_dates_ok,
        adapter_shadow_ok=adapter_ok,
        portfolio_candidate_rows=truthy_count(rows, "portfolio_candidate_gate"),
        oos_valid_rows=truthy_count(rows, "oos_score_valid_flag"),
        research_eligible_rows=truthy_count(rows, "research_calibration_input_eligible_flag"),
        max_source_date=max_source_date,
        issues=issues,
    )


def to_report_row(check: SnapshotCheck) -> dict[str, Any]:
    return {
        "asof_date": check.asof_date,
        "snapshot_dir": str(check.snapshot_dir),
        "status": check.status,
        "rows": check.rows,
        "sha256": check.sha256,
        "manifest_ok": int(check.manifest_ok),
        "schema_ok": int(check.schema_ok),
        "score_units_ok": int(check.score_units_ok),
        "shadow_gates_ok": int(check.shadow_gates_ok),
        "pit_dates_ok": int(check.pit_dates_ok),
        "adapter_shadow_ok": int(check.adapter_shadow_ok),
        "portfolio_candidate_rows": check.portfolio_candidate_rows,
        "oos_valid_rows": check.oos_valid_rows,
        "research_eligible_rows": check.research_eligible_rows,
        "max_source_date": check.max_source_date,
        "issues": ";".join(check.issues),
    }


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    family_cfg = cfg_get(config, "oos_calibration_standards.families.defense", {}) or {}
    snapshot_root = (
        args.snapshot_root.expanduser().resolve()
        if args.snapshot_root
        else resolve_path(
            cfg_get(family_cfg, "snapshot_history_root", "../output/industrials/defense/dashboard"),
            base_dir=base_dir,
        )
    )
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else PROJECT_ROOT / "output" / "industrials" / "defense" / "stage8" / "oos_calibration_readiness_report.csv"
    )
    min_snapshots = int(cfg_get(family_cfg, "min_shadow_snapshots_for_promotion", 60) or 60)
    expected_score_version = str(cfg_get(family_cfg, "calibration_provenance_version", "") or "")
    primary_benchmark = str(cfg_get(family_cfg, "primary_benchmark_ticker", "") or "")
    robustness_benchmark = str(cfg_get(family_cfg, "robustness_benchmark_ticker", "") or "")
    if primary_benchmark != "XAR":
        raise ValueError(f"Defense primary OOS benchmark must be XAR, got {primary_benchmark!r}")
    if robustness_benchmark != "ITA":
        raise ValueError(f"Defense robustness benchmark must be ITA, got {robustness_benchmark!r}")

    asof = parse_date(args.asof)
    start = parse_date(args.start_date)
    end = parse_date(args.end_date)
    if asof and (start or end):
        raise ValueError("--asof cannot be combined with --start-date/--end-date")
    if start and end and start > end:
        raise ValueError("--start-date cannot be after --end-date")

    dirs = snapshot_dirs(snapshot_root, asof=asof, start=start, end=end)
    exp_header = expected_header()
    run_adapter = bool(cfg_get(family_cfg, "require_portfolio_adapter_shadow_validation", True)) and not args.skip_portfolio_adapter
    lock = load_production_lock(config, base_dir=base_dir)
    checks = [
        validate_snapshot(
            path,
            exp_header=exp_header,
            expected_score_version=expected_score_version,
            run_adapter=run_adapter,
            mode=lock_mode_for_asof(lock, path.name),
        )
        for path in dirs
    ]
    structural_failures = [check for check in checks if check.status != "pass"]
    valid_snapshots = [check for check in checks if check.status == "pass"]
    insufficient_history = len(valid_snapshots) < min_snapshots
    report_rows = [to_report_row(check) for check in checks]
    if insufficient_history:
        report_rows.append(
            {
                "asof_date": "__SUMMARY__",
                "snapshot_dir": str(snapshot_root),
                "status": "promotion_blocked" if args.promotion_check else "report_only_insufficient_history",
                "rows": "",
                "sha256": "",
                "manifest_ok": "",
                "schema_ok": "",
                "score_units_ok": "",
                "shadow_gates_ok": "",
                "pit_dates_ok": "",
                "adapter_shadow_ok": "",
                "portfolio_candidate_rows": "",
                "oos_valid_rows": "",
                "research_eligible_rows": "",
                "max_source_date": "",
                "issues": f"valid_snapshots={len(valid_snapshots)} below min_shadow_snapshots_for_promotion={min_snapshots}",
            }
        )
    write_csv_atomic(output_csv, REPORT_FIELDS, report_rows)
    print(
        f"Stage 8 OOS readiness report: valid_snapshots={len(valid_snapshots)} "
        f"checked={len(checks)} min_required={min_snapshots} promotion_ready={not structural_failures and not insufficient_history}"
    )
    print(f"Wrote {output_csv}")
    if structural_failures:
        for check in structural_failures[:10]:
            print(f"FAIL {check.asof_date}: {'; '.join(check.issues)}")
        return 1
    if args.promotion_check and insufficient_history:
        print(f"FAIL: valid snapshot history below promotion minimum ({len(valid_snapshots)}/{min_snapshots})")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

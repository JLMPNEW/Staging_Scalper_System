#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SCRIPT_35 = PACKAGE_ROOT / "scripts" / "35_calibrate_biotech_cohort_config_overrides.py"
TARGET_FIELDS = [
    "target_rank",
    "biotech_primary_cohort",
    "priority_score",
    "historical_observations",
    "current_ticker_count",
    "calibration_eligible_count",
    "sparse_or_label_blocked",
    "recommended_next_step",
    "output_dir",
]
BEST_FIELDS = [
    "target_rank",
    "target_cohort",
    "candidate_id",
    "candidate_description",
    "status",
    "config_action",
    "within_lcb_delta_pct",
    "within_profit_factor_delta",
    "improved_unique_ticker_rate_pct",
    "median_unique_ticker_return_delta_pct",
    "harmed_unique_ticker_rate_pct",
    "global_top10_lcb_delta_pct",
    "global_top20_lcb_delta_pct",
    "promotion_blockers",
    "recommendation",
    "source_output_dir",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run batch biotech cohort-config override calibration.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--target-count", type=int, default=None)
    parser.add_argument("--candidate-limit", type=int, default=None)
    parser.add_argument("--start-asof", default=None)
    parser.add_argument("--end-asof", default=None)
    parser.add_argument("--max-asof-dates", type=int, default=0)
    parser.add_argument("--horizons", default=None)
    parser.add_argument("--top-n", default=None)
    parser.add_argument("--candidate-pool-rank-max", type=int, default=None)
    parser.add_argument("--include-sparse-targets", action="store_true", default=None)
    parser.add_argument("--skip-component-audit", action="store_true")
    return parser.parse_args()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def to_float(raw: object, default: float = 0.0) -> float:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


def safe_name(raw: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in raw.strip().lower())
    return text.strip("_") or "unknown"


def add_common_args(command: list[str], args: argparse.Namespace) -> None:
    if args.db is not None:
        command.extend(["--db", str(args.db)])
    if args.start_asof:
        command.extend(["--start-asof", str(args.start_asof)])
    if args.end_asof:
        command.extend(["--end-asof", str(args.end_asof)])
    if args.max_asof_dates:
        command.extend(["--max-asof-dates", str(args.max_asof_dates)])
    if args.horizons:
        command.extend(["--horizons", str(args.horizons)])
    if args.top_n:
        command.extend(["--top-n", str(args.top_n)])
    if args.candidate_pool_rank_max is not None:
        command.extend(["--candidate-pool-rank-max", str(args.candidate_pool_rank_max)])


def run_script35(mode: str, output_dir: Path, args: argparse.Namespace, *, target_cohort: str = "") -> None:
    command = [
        sys.executable,
        str(SCRIPT_35),
        "--config",
        str(args.config),
        "--mode",
        mode,
        "--output-dir",
        str(output_dir),
    ]
    add_common_args(command, args)
    if target_cohort:
        command.extend(["--target-cohort", target_cohort])
    if args.candidate_limit is not None and mode == "calibrate-one":
        command.extend(["--candidate-limit", str(args.candidate_limit)])
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)


def selected_targets(priority_rows: list[dict[str, str]], *, target_count: int, include_sparse: bool) -> list[dict[str, str]]:
    ranked = sorted(priority_rows, key=lambda row: to_float(row.get("priority_score")), reverse=True)
    out: list[dict[str, str]] = []
    for row in ranked:
        cohort = str(row.get("biotech_primary_cohort") or "").strip()
        if not cohort:
            continue
        if not include_sparse and str(row.get("recommended_next_step") or "") != "calibrate_first_pass":
            continue
        out.append(row)
        if len(out) >= target_count:
            break
    return out


def best_promotion_row(rows: list[dict[str, str]]) -> dict[str, str] | None:
    if not rows:
        return None
    return max(
        rows,
        key=lambda row: (
            1.0 if str(row.get("status") or "").lower() == "promote" else 0.0,
            1.0 if str(row.get("status") or "").lower() == "shadow" else 0.0,
            to_float(row.get("within_lcb_delta_pct")),
            to_float(row.get("global_top20_lcb_delta_pct")),
            to_float(row.get("within_profit_factor_delta")),
        ),
    )


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    batch_base = "biotech_scoring.cohort_config_overrides.batch"
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(
            cfg_get(
                config,
                f"{batch_base}.output_dir",
                "../output/biotech_index_reports/cohort_config_override_batch",
            ),
            base_dir=base_dir,
        )
    )
    target_count = int(
        args.target_count
        if args.target_count is not None
        else cfg_get(config, f"{batch_base}.target_count", 5)
    )
    include_sparse = (
        bool(args.include_sparse_targets)
        if args.include_sparse_targets is not None
        else str(cfg_get(config, f"{batch_base}.include_sparse_targets", False)).strip().lower()
        in {"1", "true", "yes", "y", "on"}
    )
    if args.candidate_limit is None:
        raw_limit = cfg_get(config, f"{batch_base}.candidate_limit", None)
        if raw_limit is not None:
            args.candidate_limit = int(raw_limit)

    run_id = utc_stamp()
    run_dir = output_dir / run_id
    audit_dir = run_dir / "00_priority"
    component_dir = run_dir / "01_component_audit"
    run_script35("priority", audit_dir, args)
    if not args.skip_component_audit:
        run_script35("component-audit", component_dir, args)

    priority_rows = read_csv(audit_dir / "cohort_priority.csv")
    targets = selected_targets(priority_rows, target_count=max(1, target_count), include_sparse=include_sparse)
    target_rows: list[dict[str, Any]] = []
    promotion_rows: list[dict[str, Any]] = []
    best_rows: list[dict[str, Any]] = []
    for idx, target in enumerate(targets, start=1):
        cohort = str(target.get("biotech_primary_cohort") or "").strip()
        target_dir = run_dir / f"{idx:02d}_{safe_name(cohort)}"
        run_script35("calibrate-one", target_dir, args, target_cohort=cohort)
        target_row: dict[str, Any] = {field: target.get(field, "") for field in TARGET_FIELDS}
        target_row["target_rank"] = idx
        target_row["output_dir"] = str(target_dir)
        target_rows.append(target_row)
        rows = read_csv(target_dir / "promotion_recommendations.csv")
        for row in rows:
            promotion_row: dict[str, Any] = dict(row)
            promotion_row["target_rank"] = idx
            promotion_row["source_output_dir"] = str(target_dir)
            promotion_rows.append(promotion_row)
        best = best_promotion_row(rows)
        if best is not None:
            best_rows.append(
                {
                    "target_rank": idx,
                    "target_cohort": cohort,
                    "source_output_dir": str(target_dir),
                    **best,
                }
            )

    write_csv(run_dir / "batch_target_cohorts.csv", target_rows, TARGET_FIELDS)
    promotion_fields = ["target_rank", "source_output_dir"]
    if promotion_rows:
        for key in promotion_rows[0]:
            if key not in promotion_fields:
                promotion_fields.append(key)
    write_csv(run_dir / "batch_promotion_recommendations.csv", promotion_rows, promotion_fields)
    write_csv(run_dir / "batch_best_recommendations.csv", best_rows, BEST_FIELDS)
    manifest = {
        "run_id": run_id,
        "config_path": str(config_path),
        "output_dir": str(run_dir),
        "target_count_requested": target_count,
        "include_sparse_targets": include_sparse,
        "targets": [row["biotech_primary_cohort"] for row in target_rows],
        "promotion_recommendation_rows": len(promotion_rows),
        "best_recommendation_rows": len(best_rows),
        "component_audit_run": not args.skip_component_audit,
        "production_behavior_changed": False,
    }
    (run_dir / "batch_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(f"batch_output_dir={run_dir}")
    print(f"targets={','.join(manifest['targets'])}")
    print(f"promotion_rows={len(promotion_rows)} best_rows={len(best_rows)}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from types import ModuleType
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.defense.metric_promotion_audit import (  # noqa: E402
    FINDING_FIELDS,
    INVENTORY_FIELDS,
    OVERLAP_FIELDS,
    PIT_FIELDS,
    build_findings,
    build_inventory,
    candidate_snapshot_coverage,
    manifest_for_files,
    pairwise_overlap,
    read_csv_rows,
    summary_status,
)
from industrials.defense.research_artifacts import (  # noqa: E402
    PILLAR_SCORE_FIELDS,
    load_production_lock,
    sha256_file,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a read-only, exhaustive defense metric promotion-readiness "
            "audit across data, lineage, PIT snapshots, candidate evidence, "
            "production scoring, and daily orchestration."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Write all findings but return zero even when promotion sealing is blocked.",
    )
    return parser.parse_args()


def load_runner_module() -> ModuleType:
    path = PACKAGE_ROOT / "defense" / "scripts" / "16_run_defense_daily_refresh.py"
    spec = importlib.util.spec_from_file_location("defense_daily_refresh_audit", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load defense daily runner: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    asof = str(args.asof).strip()
    db_path = (
        args.db.expanduser().resolve()
        if args.db is not None
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=config_path.parent)
    )
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir is not None
        else PROJECT_ROOT
        / "output"
        / "industrials"
        / "defense"
        / "stage10"
        / "metric_promotion_audit"
        / asof
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    candidate_root = (
        PROJECT_ROOT
        / "output"
        / "industrials"
        / "defense"
        / "stage8"
        / "candidates"
        / "specialized_v1_matched"
    )
    candidate_snapshot_root = candidate_root / "weekly_rank_snapshots"
    candidate_manifest_path = (
        candidate_root
        / "baseline_vs_candidate"
        / "defense_baseline_vs_candidate_manifest.json"
    )
    production_rank_path = (
        PROJECT_ROOT
        / "output"
        / "industrials"
        / "defense"
        / "dashboard"
        / asof
        / "defense_final_rank_table.csv"
    )
    production_rank_manifest_path = production_rank_path.with_name(
        "defense_final_rank_table_manifest.json"
    )
    production_lock = load_production_lock(
        config,
        base_dir=config_path.parent,
        asof=asof,
    )
    if production_lock is None:
        raise ValueError(f"No effective defense production lock for asof={asof}")
    production_promotion_path = Path(
        str(production_lock["decision_manifest_path"])
    )
    parser_promotion_path = resolve_path(
        cfg_get(config, "dedicated_parser.production_manifest_json"),
        base_dir=config_path.parent,
    )
    review_policy_path = (
        PACKAGE_ROOT
        / "defense"
        / "review_policies"
        / "dedicated_parser_review_policy.csv"
    )
    golden_paths = [
        PROJECT_ROOT / "dedicated_parser" / "golden_corpus" / "defense_v1.json",
        PROJECT_ROOT
        / "dedicated_parser"
        / "golden_corpus"
        / "defense_policy_generated.json",
    ]

    runner = load_runner_module()
    steps = runner.build_steps(
        asof,
        "2018-01-01",
        positioning_through_publish_only=False,
        include_dedicated_parser_shadow=True,
    )
    orchestration_step_ids = [str(step.step_id) for step in steps]

    uri = f"{db_path.as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        inventory, schema_drift = build_inventory(
            conn,
            config=config,
            asof=asof,
        )
        pit_rows, pit_hash_issues = candidate_snapshot_coverage(
            candidate_snapshot_root
        )
        candidate_panel = (
            candidate_root
            / "oos_calibration_panel_weekly"
            / "defense_oos_calibration_panel.csv"
        )
        panel_rows = read_csv_rows(candidate_panel)
        evaluation_rows = [
            row
            for row in panel_rows
            if row.get("panel_row_eligible_flag") == "1"
            and row.get("split_name") in {"validation", "holdout"}
        ]
        overlap_rows = pairwise_overlap(
            evaluation_rows,
            fields=list(PILLAR_SCORE_FIELDS),
            dataset="candidate_validation_holdout_panel",
        )
        findings, context = build_findings(
            conn=conn,
            asof=asof,
            inventory=inventory,
            schema_drift=schema_drift,
            pit_rows=pit_rows,
            pit_hash_issues=pit_hash_issues,
            candidate_manifest_path=candidate_manifest_path,
            production_rank_path=production_rank_path,
            production_rank_manifest_path=production_rank_manifest_path,
            production_promotion_path=production_promotion_path,
            parser_promotion_path=parser_promotion_path,
            review_policy_path=review_policy_path,
            golden_paths=golden_paths,
            orchestration_step_ids=orchestration_step_ids,
            candidate_panel_rows=panel_rows,
        )

    status = summary_status(findings)
    inventory_path = output_dir / "defense_metric_inventory.csv"
    pit_path = output_dir / "defense_metric_pit_snapshot_coverage.csv"
    overlap_path = output_dir / "defense_metric_overlap.csv"
    findings_path = output_dir / "defense_metric_promotion_findings.csv"
    summary_path = output_dir / "defense_metric_promotion_audit_summary.json"
    manifest_path = output_dir / "defense_metric_promotion_audit_manifest.json"
    write_csv_atomic(inventory_path, INVENTORY_FIELDS, inventory)
    write_csv_atomic(pit_path, PIT_FIELDS, pit_rows)
    write_csv_atomic(overlap_path, OVERLAP_FIELDS, overlap_rows)
    write_csv_atomic(findings_path, FINDING_FIELDS, findings)
    blocking = [
        row
        for row in findings
        if row["status"] == "FAIL" and row["severity"] in {"critical", "high"}
    ]
    summary: dict[str, Any] = {
        "artifact_family": "defense_metric_promotion_readiness_audit",
        "asof_date": asof,
        "status": status,
        "database_path": str(db_path),
        "read_only_database_access": True,
        "active_ticker_count": context["active_ticker_count"],
        "historical_ticker_count": context["historical_ticker_count"],
        "inventory_column_count": len(inventory),
        "promotion_candidate_column_count": sum(
            int(row["promotion_candidate_flag"]) for row in inventory
        ),
        "candidate_snapshot_count": (
            int(pit_rows[0]["snapshot_count"]) if pit_rows else 0
        ),
        "candidate_panel_evaluation_rows": len(evaluation_rows),
        "finding_counts": {
            key: sum(
                row["severity"] == key and row["status"] == "FAIL"
                for row in findings
            )
            for key in ("critical", "high", "medium", "low")
        },
        "blocking_findings": [row["code"] for row in blocking],
        "production_rank_sha256": (
            sha256_file(production_rank_path)
            if production_rank_path.is_file()
            else ""
        ),
        "candidate_comparison_manifest": str(candidate_manifest_path),
        "candidate_promotable_evidence": bool(
            context["candidate_manifest"].get("promotable_evidence")
        ),
        "daily_orchestration_step_ids": orchestration_step_ids,
        "note": (
            "This audit is read-only and never activates a score model. "
            "READY is required before a separate production activation."
        ),
    }
    write_text_atomic(summary_path, json.dumps(summary, indent=2, sort_keys=True) + "\n")
    manifest = manifest_for_files(
        asof=asof,
        status=status,
        files=[inventory_path, pit_path, overlap_path, findings_path, summary_path],
        summary=summary,
    )
    write_text_atomic(manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if args.report_only or status != "BLOCKED" else 2


if __name__ == "__main__":
    raise SystemExit(main())

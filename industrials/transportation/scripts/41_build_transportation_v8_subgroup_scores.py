#!/usr/bin/env python3
"""Freeze v8 subgroup weights and regenerate scores once from accepted facts."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.contracts import file_sha256  # noqa: E402
from industrials.transportation.subgroup_scoring import (  # noqa: E402
    build_v8_score_rows,
    load_subgroup_score_policy,
)


ROOT = PROJECT_ROOT / "output" / "industrials" / "transportation"
DEFAULT_POLICY = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "data"
    / "transportation_subgroup_score_policy_v8.yaml"
)
DEFAULT_PANEL = (
    ROOT
    / "investable_v5"
    / "outcome_panel_v6"
    / "2026-08-16"
    / "transportation_v5_outcome_panel.csv"
)
DEFAULT_PANEL_MANIFEST = DEFAULT_PANEL.parent / "transportation_v5_outcome_panel_manifest.json"
DEFAULT_COMPLETION = (
    ROOT
    / "investable_v5"
    / "specialized_contemporaneous_coverage"
    / "2026-08-21"
    / "transportation_specialized_metric_completion.json"
)
DEFAULT_COVERAGE = DEFAULT_COMPLETION.parent / "transportation_specialized_contemporaneous_coverage.json"
DEFAULT_SURFACE_REPLAY = (
    ROOT
    / "investable_v3"
    / "surface_delta"
    / "2026-08-21"
    / "transportation_surface_semantic_replay_accepted.csv"
)
DEFAULT_TANKER_REPLAY = (
    ROOT
    / "investable_v3"
    / "tanker_delta"
    / "2026-08-21"
    / "transportation_tanker_semantic_replay_accepted.csv"
)
DEFAULT_REGISTRY = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "data"
    / "transportation_specialized_metric_discovery_registry.csv"
)
DEFAULT_OUTPUT_ROOT = ROOT / "investable_v5" / "subgroup_scores_v8"

SCORE_FIELDS = (
    "asof_date",
    "ticker",
    "calibration_cohort",
    "v8_cohort_id",
    "v8_group_id",
    "ranking_mode",
    "specialized_pack_active_flag",
    "specialized_activation_policy",
    "specialized_features_json",
    "specialized_source_keys_json",
    "component_scores_json",
    "component_weights_json",
    "v8_final_score",
    "v8_group_percentile_score",
    "source_rank_ready_flag",
    "source_calibration_eligible_flag",
    "group_cross_section_ready_flag",
    "group_specialized_ready_flag",
    "v8_calibration_eligible_flag",
    "source_score_sha256",
)
COVERAGE_FIELDS = (
    "policy_version",
    "cohort_id",
    "group_id",
    "score_date",
    "applicable_ticker_count",
    "specialized_observed_breadth",
    "minimum_specialized_breadth",
    "date_gate",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asof", default="2026-08-21")
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--panel", type=Path, default=DEFAULT_PANEL)
    parser.add_argument("--panel-manifest", type=Path, default=DEFAULT_PANEL_MANIFEST)
    parser.add_argument("--completion", type=Path, default=DEFAULT_COMPLETION)
    parser.add_argument("--coverage", type=Path, default=DEFAULT_COVERAGE)
    parser.add_argument("--surface-replay", type=Path, default=DEFAULT_SURFACE_REPLAY)
    parser.add_argument("--tanker-replay", type=Path, default=DEFAULT_TANKER_REPLAY)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def main() -> int:
    args = parse_args()
    paths = {
        "policy": args.policy.expanduser().resolve(),
        "panel": args.panel.expanduser().resolve(),
        "panel_manifest": args.panel_manifest.expanduser().resolve(),
        "completion": args.completion.expanduser().resolve(),
        "coverage": args.coverage.expanduser().resolve(),
        "surface_replay": args.surface_replay.expanduser().resolve(),
        "tanker_replay": args.tanker_replay.expanduser().resolve(),
        "registry": args.registry.expanduser().resolve(),
    }
    missing = [f"{name}={path}" for name, path in paths.items() if not path.is_file()]
    if missing:
        raise FileNotFoundError("missing v8 inputs=" + ",".join(missing))

    completion = read_json(paths["completion"])
    if completion.get("acceptance") != "PASS":
        raise ValueError("specialized parser completion is not PASS")
    if int(completion.get("document_reparse_after_semantic_review") or 0) != 0:
        raise ValueError("post-review parser rerun violates the efficient sequence")
    if int(completion.get("source_document_parse_batches") or 0) != 2:
        raise ValueError("expected exactly one surface and one tanker parser batch")

    coverage = read_json(paths["coverage"])
    if coverage.get("acceptance") != "PASS":
        raise ValueError("point-in-time coverage audit is not PASS")
    for lane in ("surface_replay", "tanker_replay"):
        if str(coverage["input_hashes"].get(lane) or "") != file_sha256(paths[lane]):
            raise ValueError(f"{lane}: replay hash does not match coverage audit")

    panel_manifest = read_json(paths["panel_manifest"])
    if panel_manifest.get("acceptance") != "PASS":
        raise ValueError("immutable source outcome panel is not PASS")
    if str(panel_manifest.get("panel_sha256") or "") != file_sha256(paths["panel"]):
        raise ValueError("immutable source panel hash mismatch")
    if panel_manifest.get("historical_results_can_authorize_production") is not False:
        raise ValueError("source panel governance unexpectedly allows production")

    policy = load_subgroup_score_policy(paths["policy"])
    registry_rows = read_csv(paths["registry"])
    staleness = {
        row["metric_id"]: int(row["max_staleness_days"])
        for row in registry_rows
        if row.get("metric_id") and row.get("max_staleness_days")
    }
    panel_rows = read_csv(paths["panel"])
    accepted_rows = read_csv(paths["surface_replay"]) + read_csv(paths["tanker_replay"])
    score_rows, coverage_rows, manifest = build_v8_score_rows(
        panel_rows=panel_rows,
        accepted_rows=accepted_rows,
        policy=policy,
        staleness_days=staleness,
    )

    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else DEFAULT_OUTPUT_ROOT / args.asof
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    score_path = output_dir / "transportation_v8_subgroup_score_history.csv"
    coverage_path = output_dir / "transportation_v8_specialized_pack_coverage.csv"
    result_path = output_dir / "transportation_v8_subgroup_score_history.json"
    write_csv_atomic(score_path, SCORE_FIELDS, score_rows)
    write_csv_atomic(coverage_path, COVERAGE_FIELDS, coverage_rows)

    cohort_authorization: dict[str, bool] = {}
    for cohort_id in policy["cohorts"]:
        group_rows = [
            row for row in manifest["group_summaries"] if row["cohort_id"] == cohort_id
        ]
        cohort_authorization[str(cohort_id)] = all(
            int(row["group_calibration_ready_flag"]) == 1 for row in group_rows
        )
    manifest.update(
        acceptance="PASS",
        contract_version="transportation_v8_subgroup_score_history_v1",
        asof_date=args.asof,
        lineage={
            name: {"path": str(path), "sha256": file_sha256(path)}
            for name, path in paths.items()
        },
        artifacts={
            "score_history": {"path": str(score_path), "sha256": file_sha256(score_path)},
            "specialized_pack_coverage": {
                "path": str(coverage_path),
                "sha256": file_sha256(coverage_path),
            },
        },
        cohort_diagnostic_calibration_authorized=cohort_authorization,
        historical_financial_reparse_count=0,
        post_semantic_parser_invocations=0,
        historical_score_regeneration_count=1,
        historical_results_can_authorize_production=False,
        production_activation_authorized=False,
        next_gate="RUN_V8_COHORT_AND_GROUP_DIAGNOSTIC_CALIBRATION_ON_AUTHORIZED_COHORTS_ONLY",
    )
    write_text_atomic(result_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


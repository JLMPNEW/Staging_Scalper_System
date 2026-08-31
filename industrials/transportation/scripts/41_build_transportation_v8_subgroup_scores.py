#!/usr/bin/env python3
"""Freeze v8 subgroup weights and regenerate scores once from accepted facts."""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence


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
    parser.add_argument(
        "--conflict-audit",
        type=Path,
        default=None,
        help=(
            "Optional accepted-fact conflict audit that provenance-binds "
            "versioned conflict-normalized replay derivatives to the original "
            "coverage inputs."
        ),
    )
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--allow-overwrite", action="store_true")
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _integer(value: object, *, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"conflict audit has invalid {label}") from exc
    if parsed < 0:
        raise ValueError(f"conflict audit has negative {label}")
    return parsed


def validate_conflict_audit_bridge(
    *,
    audit_path: Path,
    audit: Mapping[str, Any],
    coverage: Mapping[str, Any],
    replay_paths: Mapping[str, Path],
    accepted_rows: Sequence[Mapping[str, str]],
) -> dict[str, object]:
    """Validate an immutable normalized-replay derivative end to end."""
    if audit.get("acceptance") != "PASS":
        raise ValueError("accepted-fact conflict audit is not PASS")
    if audit.get("unresolved_conflicts_fail_closed") is not True:
        raise ValueError("accepted-fact conflict audit does not fail closed")
    if audit.get("source_conflicts_are_never_averaged") is not True:
        raise ValueError("accepted-fact conflict audit permits source averaging")
    if audit.get("historical_results_can_authorize_production") is not False:
        raise ValueError("conflict audit unexpectedly authorizes historical production")
    if audit.get("production_activation_authorized") is not False:
        raise ValueError("conflict audit unexpectedly authorizes production")

    before = _integer(
        audit.get("resolver_conflict_count_before"),
        label="resolver_conflict_count_before",
    )
    resolved = _integer(
        audit.get("deterministic_false_conflict_count"),
        label="deterministic_false_conflict_count",
    )
    residual = _integer(
        audit.get("resolver_conflict_count_after"),
        label="resolver_conflict_count_after",
    )
    unresolved = _integer(
        audit.get("unresolved_fail_closed_count"),
        label="unresolved_fail_closed_count",
    )
    if residual != unresolved or before != resolved + residual:
        raise ValueError("conflict audit counts do not reconcile")
    if sum(
        _integer(value, label="resolution_count_by_rule")
        for value in (audit.get("resolution_count_by_rule") or {}).values()
    ) != resolved:
        raise ValueError("conflict audit resolution-rule counts do not reconcile")
    if sum(
        _integer(value, label="residual_count_by_classification")
        for value in (audit.get("residual_count_by_classification") or {}).values()
    ) != residual:
        raise ValueError("conflict audit residual classifications do not reconcile")
    if sum(
        _integer(value, label="conflict_count_after_by_metric")
        for value in (audit.get("conflict_count_after_by_metric") or {}).values()
    ) != residual:
        raise ValueError("conflict audit residual metric counts do not reconcile")

    lineage = audit.get("lineage") or {}
    artifacts = audit.get("artifacts") or {}
    lane_map = {
        "surface_replay": (
            "surface_accepted_replay",
            "surface_normalized_replay",
        ),
        "tanker_replay": (
            "tanker_accepted_replay",
            "tanker_normalized_replay",
        ),
    }
    normalized_hashes: dict[str, str] = {}
    original_hashes: dict[str, str] = {}
    coverage_hashes = coverage.get("input_hashes") or {}
    for lane, (source_key, artifact_key) in lane_map.items():
        source = lineage.get(source_key) or {}
        artifact = artifacts.get(artifact_key) or {}
        coverage_hash = str(coverage_hashes.get(lane) or "")
        source_hash = str(source.get("sha256") or "")
        if not coverage_hash or source_hash != coverage_hash:
            raise ValueError(
                f"{lane}: conflict-audit original hash does not match coverage audit"
            )
        replay_path = replay_paths[lane].resolve()
        recorded_path = Path(str(artifact.get("path") or "")).expanduser().resolve()
        if recorded_path != replay_path:
            raise ValueError(
                f"{lane}: supplied normalized replay is not the audited artifact"
            )
        actual_hash = file_sha256(replay_path)
        recorded_hash = str(artifact.get("sha256") or "")
        if not recorded_hash or actual_hash != recorded_hash:
            raise ValueError(f"{lane}: normalized replay hash does not match conflict audit")
        original_hashes[lane] = source_hash
        normalized_hashes[lane] = actual_hash

    expected_rows = _integer(
        audit.get("normalized_row_count"),
        label="normalized_row_count",
    )
    if expected_rows != len(accepted_rows):
        raise ValueError("normalized replay row count does not match conflict audit")

    grouped: defaultdict[str, list[Mapping[str, str]]] = defaultdict(list)
    allowed_statuses = {
        "NOT_CONFLICTED",
        "RESOLVED_DETERMINISTIC",
        "FAIL_CLOSED_REVIEW_REQUIRED",
    }
    for row in accepted_rows:
        status = str(row.get("conflict_resolution_status") or "")
        group_id = str(row.get("conflict_group_id") or "")
        if status not in allowed_statuses:
            raise ValueError("normalized replay has an invalid conflict status")
        if status == "NOT_CONFLICTED":
            if group_id:
                raise ValueError("nonconflicted replay row unexpectedly has a conflict id")
            continue
        if not group_id:
            raise ValueError("conflict replay row is missing conflict_group_id")
        grouped[group_id].append(row)

    residual_group_count = 0
    for group_id, rows in grouped.items():
        statuses = {
            str(row.get("conflict_resolution_status") or "") for row in rows
        }
        if len(statuses) != 1:
            raise ValueError(f"{group_id}: mixed conflict dispositions")
        values: set[float] = set()
        for row in rows:
            try:
                value = float(str(row.get("value") or ""))
            except ValueError as exc:
                raise ValueError(f"{group_id}: invalid normalized value") from exc
            if not math.isfinite(value):
                raise ValueError(f"{group_id}: non-finite normalized value")
            values.add(value)
        status = next(iter(statuses))
        if status == "FAIL_CLOSED_REVIEW_REQUIRED":
            if len(values) < 2:
                raise ValueError(
                    f"{group_id}: fail-closed group no longer contains a conflict"
                )
            residual_group_count += 1
        elif len(values) != 1:
            raise ValueError(
                f"{group_id}: deterministic resolution retained multiple values"
            )
    if residual_group_count != residual:
        raise ValueError(
            "normalized replay residual group count does not match conflict audit"
        )

    return {
        "status": "VERIFIED",
        "audit_path": str(audit_path.resolve()),
        "audit_sha256": file_sha256(audit_path),
        "resolver_conflict_count_before": before,
        "deterministic_false_conflict_count": resolved,
        "unresolved_fail_closed_count": residual,
        "normalized_replay_group_count": len(grouped),
        "normalized_replay_row_count": len(accepted_rows),
        "original_replay_hashes": original_hashes,
        "normalized_replay_hashes": normalized_hashes,
        "unresolved_conflicts_excluded_by_score_resolver": True,
        "production_activation_authorized": False,
    }


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
    if args.conflict_audit is not None:
        paths["conflict_audit"] = args.conflict_audit.expanduser().resolve()
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
    direct_replay_binding = args.conflict_audit is None
    if direct_replay_binding:
        for lane in ("surface_replay", "tanker_replay"):
            if str(coverage["input_hashes"].get(lane) or "") != file_sha256(
                paths[lane]
            ):
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
    if direct_replay_binding:
        conflict_bridge: dict[str, object] = {
            "status": "NOT_REQUESTED",
            "replay_binding": "DIRECT_COVERAGE_INPUT_HASH",
        }
    else:
        conflict_audit = read_json(paths["conflict_audit"])
        conflict_bridge = validate_conflict_audit_bridge(
            audit_path=paths["conflict_audit"],
            audit=conflict_audit,
            coverage=coverage,
            replay_paths={
                "surface_replay": paths["surface_replay"],
                "tanker_replay": paths["tanker_replay"],
            },
            accepted_rows=accepted_rows,
        )
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
    if not args.allow_overwrite:
        existing = [
            str(path)
            for path in (score_path, coverage_path, result_path)
            if path.exists()
        ]
        if existing:
            raise FileExistsError(
                "v8 score-history artifacts are sealed; choose a new "
                f"--output-dir or use --allow-overwrite: {existing}"
            )
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
        contract_version="transportation_v8_subgroup_score_history_v2",
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
        conflict_resolution_bridge=conflict_bridge,
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

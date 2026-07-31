#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter, deque
from pathlib import Path
from typing import Any, Iterable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.selected_feature_history import (  # noqa: E402
    read_json,
    sha256,
)

MODEL_FAMILY = "transportation"
RELEASE_NAME = "code_aligned_zero_overlay_v2"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recursively verify transportation release artifacts, repaired "
            "parser lineage, zero-overlay serving parity, and committed code."
        )
    )
    parser.add_argument("--asof", required=True)
    parser.add_argument("--release-dir", type=Path, default=None)
    parser.add_argument("--completion-manifest", type=Path, default=None)
    parser.add_argument("--repair-manifest", type=Path, default=None)
    parser.add_argument("--recovered-residual-manifest", type=Path, default=None)
    parser.add_argument("--output-json", type=Path, default=None)
    return parser.parse_args()


def artifact_reference(path: Path) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "sha256": sha256(path),
        "size_bytes": path.stat().st_size,
    }


def iter_artifact_references(value: object) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        if (
            isinstance(value.get("path"), str)
            and isinstance(value.get("sha256"), str)
            and value.get("path")
            and value.get("sha256")
        ):
            yield value
        for child in value.values():
            yield from iter_artifact_references(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_artifact_references(child)


def verify_reference(
    reference: Mapping[str, Any],
    *,
    repair_aliases: Mapping[str, Path],
) -> dict[str, Any]:
    declared = Path(str(reference["path"])).expanduser().resolve()
    expected = str(reference["sha256"])
    actual = sha256(declared) if declared.is_file() else ""
    status = "PASS"
    resolved = declared
    if actual != expected:
        alias = repair_aliases.get(expected)
        alias_hash = sha256(alias) if alias is not None and alias.is_file() else ""
        if alias is not None and alias_hash == expected:
            status = "REPAIRED_BY_ATTESTED_ALIAS"
            resolved = alias.resolve()
            actual = alias_hash
        else:
            status = "FAIL"
    return {
        "status": status,
        "declared_path": str(declared),
        "resolved_path": str(resolved),
        "expected_sha256": expected,
        "actual_sha256": actual,
    }


def recursive_artifact_audit(
    roots: Iterable[Path],
    *,
    repair_aliases: Mapping[str, Path],
) -> tuple[list[dict[str, Any]], list[str]]:
    queue = deque(path.resolve() for path in roots)
    visited: set[Path] = set()
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    while queue:
        path = queue.popleft()
        if path in visited:
            continue
        visited.add(path)
        if not path.is_file():
            errors.append(f"missing root or referenced JSON={path}")
            continue
        try:
            payload = read_json(path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            errors.append(f"invalid JSON root={path}: {error}")
            continue
        if "acceptance" in payload and payload.get("acceptance") != "PASS":
            errors.append(f"non-passing JSON artifact={path}")
        for reference in iter_artifact_references(payload):
            result = verify_reference(reference, repair_aliases=repair_aliases)
            result["referencing_json"] = str(path)
            results.append(result)
            if result["status"] == "FAIL":
                errors.append(
                    "artifact hash mismatch="
                    f"{result['declared_path']} expected={result['expected_sha256']} "
                    f"actual={result['actual_sha256']}"
                )
                continue
            resolved = Path(result["resolved_path"])
            if resolved.suffix.lower() == ".json":
                queue.append(resolved)
    return results, errors


def git_release_state() -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tracked_output = subprocess.run(
        ["git", "ls-files", "industrials/transportation", "tests/industrials"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    tracked = {path.replace("\\", "/") for path in tracked_output}
    source_files = sorted(
        path
        for path in (PROJECT_ROOT / "industrials" / "transportation").rglob("*.py")
    )
    test_files = sorted(
        (PROJECT_ROOT / "tests" / "industrials").glob("test_transportation*.py")
    )
    required_paths = [
        "industrials/config.yaml",
        *[
            path.relative_to(PROJECT_ROOT).as_posix()
            for path in (*source_files, *test_files)
        ],
    ]
    untracked = sorted(path for path in required_paths if path not in tracked and path != "industrials/config.yaml")
    if "industrials/config.yaml" not in subprocess.run(
        ["git", "ls-files", "industrials/config.yaml"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines():
        untracked.append("industrials/config.yaml")
    if untracked:
        errors.append(f"untracked release source paths={untracked}")
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", *required_paths],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    if dirty:
        errors.append(f"uncommitted release source paths={dirty}")
    return {
        "git_commit_sha": head,
        "required_path_count": len(required_paths),
        "tracked_path_count": len(required_paths) - len(untracked),
        "untracked_paths": untracked,
        "dirty_entries": dirty,
        "release_sources_committed": not untracked and not dirty,
    }, errors


def main() -> int:
    args = parse_args()
    asof = str(args.asof)[:10]
    release_dir = (
        args.release_dir.expanduser().resolve()
        if args.release_dir
        else PROJECT_ROOT
        / "output"
        / "industrials"
        / MODEL_FAMILY
        / "releases"
        / asof
        / RELEASE_NAME
    )
    completion = (
        args.completion_manifest.expanduser().resolve()
        if args.completion_manifest
        else PROJECT_ROOT
        / "output"
        / "industrials"
        / MODEL_FAMILY
        / "implementation"
        / asof
        / "transportation_implementation_completion_manifest.json"
    )
    parser_dir = (
        PROJECT_ROOT
        / "output"
        / "industrials"
        / MODEL_FAMILY
        / "dedicated_parser"
        / "2026-07-22"
    )
    repair_manifest_path = (
        args.repair_manifest.expanduser().resolve()
        if args.repair_manifest
        else parser_dir / "transportation_parser_repair_manifest.json"
    )
    recovered_manifest_path = (
        args.recovered_residual_manifest.expanduser().resolve()
        if args.recovered_residual_manifest
        else parser_dir
        / "transportation_pre_repair_non_sec_residual_source_manifest.json"
    )
    calibration_dir = release_dir / "calibration"
    current_dir = release_dir / "current"
    dashboard_dir = (
        release_dir
        / "s"
        / "industrials"
        / MODEL_FAMILY
        / "dashboard"
        / asof
    )
    roots = {
        "prior_completion": completion,
        "repair_manifest": repair_manifest_path,
        "recovered_residual_manifest": recovered_manifest_path,
        "calibration_manifest": calibration_dir
        / "transportation_walk_forward_calibration_manifest.json",
        "calibration_validation": calibration_dir
        / "transportation_walk_forward_calibration_validation.json",
        "scoring_manifest": current_dir
        / "transportation_scoring_features.manifest.json",
        "scoring_validation": current_dir
        / "transportation_scoring_validation.json",
        "rank_manifest": dashboard_dir
        / "transportation_final_rank_table_manifest.json",
        "rank_validation": dashboard_dir
        / "transportation_final_rank_table_validation.json",
        "portfolio_validation": dashboard_dir
        / "transportation_portfolio_adapter_validation.json",
    }
    errors: list[str] = []
    repair_aliases: dict[str, Path] = {}
    repair_lineage: dict[str, Any] = {}
    if repair_manifest_path.is_file() and recovered_manifest_path.is_file():
        repair_manifest = read_json(repair_manifest_path)
        recovered_manifest = read_json(recovered_manifest_path)
        expected_reference = (
            (repair_manifest.get("inputs") or {}).get("residual_source_audit")
            or {}
        )
        recovered_reference = recovered_manifest.get("artifact") or {}
        expected_hash = str(expected_reference.get("sha256") or "")
        recovered_path = Path(str(recovered_reference.get("path") or "")).resolve()
        recovered_hash = sha256(recovered_path) if recovered_path.is_file() else ""
        lineage_pass = (
            expected_hash
            and expected_hash == str(recovered_reference.get("sha256") or "")
            and expected_hash == recovered_hash
        )
        if lineage_pass:
            repair_aliases[expected_hash] = recovered_path
        else:
            errors.append("reconstructed pre-repair residual lineage does not match")
        repair_lineage = {
            "status": "PASS" if lineage_pass else "FAIL",
            "declared_original_path": str(expected_reference.get("path") or ""),
            "expected_sha256": expected_hash,
            "recovered_path": str(recovered_path),
            "recovered_sha256": recovered_hash,
        }
    else:
        errors.append("repair lineage manifests are missing")

    reference_results, reference_errors = recursive_artifact_audit(
        roots.values(),
        repair_aliases=repair_aliases,
    )
    errors.extend(reference_errors)
    source_control, source_errors = git_release_state()
    errors.extend(source_errors)

    calibration = read_json(roots["calibration_manifest"]) if roots["calibration_manifest"].is_file() else {}
    calibration_validation = (
        read_json(roots["calibration_validation"])
        if roots["calibration_validation"].is_file()
        else {}
    )
    scoring = read_json(roots["scoring_manifest"]) if roots["scoring_manifest"].is_file() else {}
    calibration_weights = {
        metric: float((decision or {}).get("final_research_weight") or 0.0)
        for metric, decision in (calibration.get("candidate_decisions") or {}).items()
    }
    serving_weights = {
        metric: float(weight)
        for metric, weight in (scoring.get("specialized_overlay_weights") or {}).items()
    }
    if (
        calibration.get("acceptance") != "PASS"
        or calibration_validation.get("acceptance") != "PASS"
        or not calibration_weights
        or any(weight != 0.0 for weight in calibration_weights.values())
    ):
        errors.append("calibration is not a validated zero-overlay result")
    if (
        scoring.get("acceptance") != "PASS"
        or scoring.get("score_construction_mode")
        != "generic_baseline_with_bounded_overlays"
        or scoring.get("specialized_overlay_active") is not False
        or not serving_weights
        or any(weight != 0.0 for weight in serving_weights.values())
    ):
        errors.append("serving score is not the explicit generic zero-overlay model")
    calibration_commit = str(
        (calibration.get("source_control") or {}).get("git_commit_sha") or ""
    )
    calibration_commit_is_ancestor = bool(calibration_commit) and subprocess.run(
        [
            "git",
            "merge-base",
            "--is-ancestor",
            calibration_commit,
            source_control["git_commit_sha"],
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    ).returncode == 0
    if not calibration_commit_is_ancestor:
        errors.append("calibration commit is not an ancestor of release commit")

    direct_artifacts = {
        "scoring_csv": current_dir / "transportation_scoring_features.csv",
        "rank_csv": dashboard_dir / "transportation_final_rank_table.csv",
    }
    direct_references: dict[str, Any] = {}
    for label, path in direct_artifacts.items():
        if not path.is_file():
            errors.append(f"missing direct release artifact={path}")
        else:
            direct_references[label] = artifact_reference(path)

    status_counts = Counter(result["status"] for result in reference_results)
    acceptance = "PASS" if not errors else "FAIL"
    output_path = (
        args.output_json.expanduser().resolve()
        if args.output_json
        else release_dir / "transportation_release_integrity_audit.json"
    )
    payload = {
        "acceptance": acceptance,
        "gate": "TRANSPORTATION_CODE_ALIGNED_RELEASE_INTEGRITY",
        "model_family": MODEL_FAMILY,
        "asof_date": asof,
        "release_name": RELEASE_NAME,
        "release_dir": str(release_dir),
        "source_control": source_control,
        "repair_lineage": repair_lineage,
        "calibration_final_weights": calibration_weights,
        "serving_overlay_weights": serving_weights,
        "recursive_reference_count": len(reference_results),
        "recursive_reference_status_counts": dict(sorted(status_counts.items())),
        "recursive_reference_results": reference_results,
        "root_artifacts": {
            label: artifact_reference(path)
            for label, path in roots.items()
            if path.is_file()
        },
        "direct_artifacts": direct_references,
        "production_promotion_authorized": False,
        "production_model_promoted": False,
        "operations": {
            "network_requests": 0,
            "parser_invocations": 0,
            "feature_rebuilds": 0,
            "historical_materializations": 0,
            "database_writes": 0,
            "portfolio_writes": 0,
            "production_config_writes": 0,
        },
        "errors": errors,
        "next_gate": (
            "RELEASE_ACCEPTANCE_SUITE"
            if acceptance == "PASS"
            else "REPAIR_RELEASE_INTEGRITY_FAILURES"
        ),
    }
    write_text_atomic(output_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if acceptance == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
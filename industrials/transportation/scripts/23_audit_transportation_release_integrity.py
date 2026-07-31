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
from industrials.transportation.release_contract import (  # noqa: E402
    DEFAULT_RELEASE_NAME,
    git_source_state,
)
from industrials.transportation.selected_feature_history import (  # noqa: E402
    read_json,
    sha256,
)

MODEL_FAMILY = "transportation"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recursively verify transportation release artifacts, repaired "
            "parser lineage, zero-overlay serving parity, and committed code."
        )
    )
    parser.add_argument("--asof", required=True)
    parser.add_argument("--release-name", default=DEFAULT_RELEASE_NAME)
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
        acceptance = str(payload.get("acceptance") or "")
        if "acceptance" in payload and not acceptance.startswith("PASS"):
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
            if (
                resolved.suffix.lower() == ".json"
                and reference.get("recurse") is not False
            ):
                queue.append(resolved)
    return results, errors


def git_release_state(
    source_manifest: Mapping[str, Any],
) -> tuple[dict[str, object], list[str]]:
    dependencies = source_manifest.get("dependencies") or {}
    if not isinstance(dependencies, Mapping) or not dependencies:
        return {}, ["release source dependency manifest is empty"]
    required_paths = sorted(str(path) for path in dependencies)
    expected_commit = str(source_manifest.get("git_commit_sha") or "")
    if not expected_commit:
        return {}, ["release source dependency manifest has no git commit"]
    return git_source_state(
        PROJECT_ROOT,
        required_paths,
        expected_commit=expected_commit,
    )


def main() -> int:
    args = parse_args()
    asof = str(args.asof)[:10]
    release_name = str(args.release_name).strip()
    release_dir = (
        args.release_dir.expanduser().resolve()
        if args.release_dir
        else PROJECT_ROOT
        / "output"
        / "industrials"
        / MODEL_FAMILY
        / "releases"
        / asof
        / release_name
    )
    release_manifest_path = release_dir / "transportation_release_manifest.json"
    source_manifest_path = release_dir / "source_dependencies.json"
    source_manifest = (
        read_json(source_manifest_path) if source_manifest_path.is_file() else {}
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
    dashboard_dir = current_dir / "dashboard"
    generic_dir = release_dir / "generic_oos"
    score_history_dir = release_dir / "score_history"
    roots = {
        "release_manifest": release_manifest_path,
        "source_dependencies": source_manifest_path,
        "prior_completion": completion,
        "repair_manifest": repair_manifest_path,
        "recovered_residual_manifest": recovered_manifest_path,
        "recovered_semantic_manifest": parser_dir
        / "transportation_pre_financial_semantic_fixture_freeze_manifest.json",
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
        "daily_history_validation": score_history_dir
        / "transportation_daily_rank_history_validation.json",
        "generic_panel_validation": generic_dir
        / "transportation_generic_oos_panel_validation.json",
        "generic_calibration_manifest": generic_dir
        / "transportation_generic_oos_calibration_manifest.json",
        "generic_readiness_audit": generic_dir
        / "transportation_production_readiness_audit.json",
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

    prior_completion = (
        read_json(roots["prior_completion"])
        if roots["prior_completion"].is_file()
        else {}
    )
    if prior_completion.get("acceptance") != "PASS":
        errors.append("superseded predecessor completion is not passing")
    recursive_roots = [
        roots["release_manifest"],
        roots["repair_manifest"],
        roots["recovered_residual_manifest"],
        roots["recovered_semantic_manifest"],
    ]
    semantic_lineage: dict[str, Any] = {}
    recovered_semantic_path = roots["recovered_semantic_manifest"]
    if recovered_semantic_path.is_file():
        recovered_semantic = read_json(recovered_semantic_path)
        semantic_alias_paths = {
            "semantic_metric_contract": parser_dir
            / "transportation_pre_financial_semantic_metric_contract.csv",
            "semantic_fixture_pair_contract": parser_dir
            / "transportation_pre_financial_semantic_fixture_pair_contract.csv",
            "semantic_fixture_evidence": parser_dir
            / "transportation_pre_financial_semantic_fixture_evidence.csv",
        }
        semantic_hash = sha256(recovered_semantic_path)
        repair_aliases[semantic_hash] = recovered_semantic_path
        semantic_errors: list[str] = []
        semantic_artifacts = recovered_semantic.get("artifacts") or {}
        for label, alias_path in semantic_alias_paths.items():
            expected_hash = str(
                (semantic_artifacts.get(label) or {}).get("sha256") or ""
            )
            actual_hash = sha256(alias_path) if alias_path.is_file() else ""
            if not expected_hash or expected_hash != actual_hash:
                semantic_errors.append(
                    f"{label}: expected={expected_hash} actual={actual_hash}"
                )
            else:
                repair_aliases[expected_hash] = alias_path
        if semantic_errors:
            errors.append(
                "reconstructed pre-financial semantic lineage does not match="
                f"{semantic_errors}"
            )
        semantic_lineage = {
            "status": "FAIL" if semantic_errors else "PASS",
            "adjudication_prefix": recovered_semantic.get(
                "adjudication_prefix"
            ),
            "recovered_manifest_path": str(recovered_semantic_path),
            "recovered_manifest_sha256": semantic_hash,
            "recovered_artifact_count": len(semantic_alias_paths),
            "errors": semantic_errors,
        }
    else:
        errors.append("reconstructed pre-financial semantic manifest is missing")
    reference_results, reference_errors = recursive_artifact_audit(
        recursive_roots,
        repair_aliases=repair_aliases,
    )
    errors.extend(reference_errors)
    source_control, source_errors = git_release_state(source_manifest)
    errors.extend(source_errors)

    calibration = read_json(roots["calibration_manifest"]) if roots["calibration_manifest"].is_file() else {}
    calibration_validation = (
        read_json(roots["calibration_validation"])
        if roots["calibration_validation"].is_file()
        else {}
    )
    scoring = (
        read_json(roots["scoring_manifest"])
        if roots["scoring_manifest"].is_file()
        else {}
    )
    generic_panel = (
        read_json(roots["generic_panel_validation"])
        if roots["generic_panel_validation"].is_file()
        else {}
    )
    generic_calibration = (
        read_json(roots["generic_calibration_manifest"])
        if roots["generic_calibration_manifest"].is_file()
        else {}
    )
    generic_readiness = (
        read_json(roots["generic_readiness_audit"])
        if roots["generic_readiness_audit"].is_file()
        else {}
    )
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
    if generic_panel.get("acceptance") != "PASS":
        errors.append("generic OOS panel validation is not passing")
    if (
        generic_calibration.get("artifact_acceptance") != "PASS"
        or generic_calibration.get("promotion_eligible") is not False
    ):
        errors.append("generic OOS calibration is not a valid failed-promotion artifact")
    if (
        generic_readiness.get("audit_acceptance") != "PASS"
        or generic_readiness.get("promotion_readiness") != "FAIL"
    ):
        errors.append("generic production-readiness audit is not fail-closed")

    calibration_commit = str(
        (calibration.get("source_control") or {}).get("git_commit_sha") or ""
    )
    calibration_commit_is_ancestor = bool(calibration_commit) and subprocess.run(
        [
            "git",
            "merge-base",
            "--is-ancestor",
            calibration_commit,
            str(source_control["git_commit_sha"]),
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
        "release_name": release_name,
        "release_dir": str(release_dir),
        "source_control": source_control,
        "repair_lineage": repair_lineage,
        "semantic_fixture_lineage": semantic_lineage,
        "superseded_predecessor_completion": {
            "acceptance": prior_completion.get("acceptance"),
            "production_model_promoted": prior_completion.get(
                "production_model_promoted"
            ),
            "artifact": (
                artifact_reference(roots["prior_completion"])
                if roots["prior_completion"].is_file()
                else {}
            ),
        },
        "calibration_final_weights": calibration_weights,
        "serving_overlay_weights": serving_weights,
        "generic_oos_state": {
            "panel_acceptance": generic_panel.get("acceptance"),
            "artifact_acceptance": generic_calibration.get("artifact_acceptance"),
            "promotion_eligible": generic_calibration.get("promotion_eligible"),
            "readiness_audit_acceptance": generic_readiness.get("audit_acceptance"),
            "promotion_readiness": generic_readiness.get("promotion_readiness"),
        },
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
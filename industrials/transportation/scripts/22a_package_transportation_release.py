#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import load_yaml  # noqa: E402
from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.release_contract import (  # noqa: E402
    DEFAULT_RELEASE_NAME,
    MODEL_FAMILY,
    git_source_state,
    iter_existing_files,
    required_release_source_paths,
)
from industrials.transportation.selected_feature_history import (  # noqa: E402
    read_json,
    sha256,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Package a new immutable transportation shadow release from the "
            "already-built artifacts. This performs no parsing, data loading, "
            "feature building, calibration, promotion, or portfolio writes."
        )
    )
    parser.add_argument("--asof", required=True)
    parser.add_argument("--release-name", default=DEFAULT_RELEASE_NAME)
    parser.add_argument("--release-dir", type=Path, default=None)
    return parser.parse_args()


def _reference(
    declared_path: Path,
    content_path: Path,
    *,
    recurse: bool = True,
) -> dict[str, object]:
    return {
        "path": str(declared_path.resolve()),
        "sha256": sha256(content_path),
        "size_bytes": content_path.stat().st_size,
        "recurse": recurse,
    }


def _release_inventory(asof: str) -> list[tuple[Path, Path, bool]]:
    root = PROJECT_ROOT / "output" / "industrials" / MODEL_FAMILY
    inventory: list[tuple[Path, Path, bool]] = []

    def add_tree(source_root: Path, target_root: Path, *, recurse: bool) -> None:
        for source in iter_existing_files([source_root]):
            inventory.append(
                (source, target_root / source.relative_to(source_root), recurse)
            )

    add_tree(root / "generic_oos", Path("generic_oos"), recurse=True)
    add_tree(root / "score_history", Path("score_history"), recurse=True)
    add_tree(
        root / "required_metric_repair" / asof,
        Path("required_metric_repair") / asof,
        recurse=False,
    )

    legacy = root / "historical_features" / "v3_conflict_resolved"
    for source in sorted(legacy.glob("transportation_walk_forward_calibration*")):
        if source.is_file():
            inventory.append((source.resolve(), Path("calibration") / source.name, False))

    stage6 = root / "stage6"
    for name in (
        "transportation_scoring_features.csv",
        "transportation_scoring_features.manifest.json",
        "transportation_scoring_validation.json",
    ):
        inventory.append((stage6 / name, Path("current") / name, True))

    dashboard = root / "dashboard" / asof
    for name in (
        "transportation_final_rank_table.csv",
        "transportation_final_rank_table_manifest.json",
        "transportation_final_rank_table_validation.json",
        "transportation_portfolio_adapter_validation.json",
    ):
        inventory.append(
            (dashboard / name, Path("current") / "dashboard" / name, True)
        )

    readiness = root / "production_readiness"
    for source in iter_existing_files([readiness]):
        inventory.append(
            (source, Path("production_readiness") / source.name, True)
        )

    inventory.append(
        (
            PROJECT_ROOT
            / "industrials"
            / "transportation"
            / "system_csvs"
            / "transportation_production_model_locks.csv",
            Path("governance") / "transportation_production_model_locks.csv",
            False,
        )
    )
    return sorted(inventory, key=lambda item: item[1].as_posix())


def _validate_evidence(asof: str) -> dict[str, object]:
    root = PROJECT_ROOT / "output" / "industrials" / MODEL_FAMILY
    paths = {
        "history": root
        / "score_history"
        / "transportation_daily_rank_history_validation.json",
        "panel": root
        / "generic_oos"
        / "transportation_generic_oos_panel_validation.json",
        "calibration": root
        / "generic_oos"
        / "transportation_generic_oos_calibration_manifest.json",
        "readiness": root
        / "generic_oos"
        / "transportation_production_readiness_audit.json",
        "rank": root
        / "dashboard"
        / asof
        / "transportation_final_rank_table_manifest.json",
        "portfolio": root
        / "dashboard"
        / asof
        / "transportation_portfolio_adapter_validation.json",
    }
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"missing release evidence={missing}")
    data = {name: read_json(path) for name, path in paths.items()}
    failures: list[str] = []
    if data["history"].get("acceptance") != "PASS":
        failures.append("daily history validation is not passing")
    if data["panel"].get("acceptance") != "PASS":
        failures.append("generic OOS panel validation is not passing")
    if data["calibration"].get("artifact_acceptance") != "PASS":
        failures.append("generic OOS calibration artifacts are not passing")
    if data["calibration"].get("promotion_eligible") is not False:
        failures.append("v3 shadow release requires promotion_eligible=false")
    if data["readiness"].get("audit_acceptance") != "PASS":
        failures.append("production-readiness audit did not execute cleanly")
    if data["readiness"].get("promotion_readiness") != "FAIL":
        failures.append("v3 shadow release requires promotion_readiness=FAIL")
    if data["rank"].get("acceptance") != "PASS":
        failures.append("current rank manifest is not passing")
    if data["portfolio"].get("acceptance") != "PASS":
        failures.append("portfolio adapter validation is not passing")
    if failures:
        raise ValueError(f"release evidence failures={failures}")
    return {
        "history_acceptance": data["history"].get("acceptance"),
        "panel_acceptance": data["panel"].get("acceptance"),
        "calibration_artifact_acceptance": data["calibration"].get(
            "artifact_acceptance"
        ),
        "promotion_eligible": data["calibration"].get("promotion_eligible"),
        "readiness_audit_acceptance": data["readiness"].get(
            "audit_acceptance"
        ),
        "promotion_readiness": data["readiness"].get("promotion_readiness"),
        "portfolio_acceptance": data["portfolio"].get("acceptance"),
    }


def _portfolio_contract() -> dict[str, object]:
    config_path = PROJECT_ROOT / "portfolio_layer" / "config.yaml"
    config = load_yaml(config_path)
    sectors = (config.get("score_contract") or {}).get("sectors") or []
    sources = [
        source
        for source in sectors
        if str(source.get("model_family") or "") == MODEL_FAMILY
    ]
    if len(sources) != 1:
        raise ValueError(f"expected one transportation portfolio source, found={len(sources)}")
    source = dict(sources[0])
    if not (
        source.get("adapter") == "industrial_family"
        and source.get("enabled") is True
        and source.get("required") is False
        and source.get("require_oos_score_valid") is True
    ):
        raise ValueError(f"transportation portfolio source is not fail-closed={source}")
    return {
        "acceptance": "PASS",
        "artifact_family": "transportation_portfolio_contract_snapshot",
        "model_family": MODEL_FAMILY,
        "source": source,
        "production_allocation_authorized": False,
    }


def _lock_row_count(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def main() -> int:
    args = parse_args()
    asof = str(args.asof)[:10]
    release_name = str(args.release_name).strip()
    if not release_name or release_name == "code_aligned_zero_overlay_v2":
        raise ValueError("a new non-v2 release name is required")
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
    if release_dir.exists():
        raise FileExistsError(f"immutable release directory already exists={release_dir}")

    required_sources = required_release_source_paths(PROJECT_ROOT)
    source_state, source_errors = git_source_state(
        PROJECT_ROOT,
        required_sources,
    )
    if source_errors:
        raise ValueError(f"release sources are not committed and clean={source_errors}")
    evidence = _validate_evidence(asof)
    inventory = _release_inventory(asof)
    missing = [str(source) for source, _, _ in inventory if not source.is_file()]
    if missing:
        raise FileNotFoundError(f"missing release artifacts={missing}")

    lock_path = (
        PROJECT_ROOT
        / "industrials"
        / "transportation"
        / "system_csvs"
        / "transportation_production_model_locks.csv"
    )
    if _lock_row_count(lock_path) != 0:
        raise ValueError("v3 shadow release requires a header-only production lock registry")

    release_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(
        tempfile.mkdtemp(prefix=f".{release_name}.", dir=release_dir.parent)
    ).resolve()
    try:
        artifact_refs: dict[str, dict[str, object]] = {}
        for source, relative_target, recurse in inventory:
            staged_target = staging_dir / relative_target
            final_target = release_dir / relative_target
            staged_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, staged_target)
            artifact_refs[relative_target.as_posix()] = _reference(
                final_target,
                staged_target,
                recurse=recurse,
            )

        source_dependencies_path = staging_dir / "source_dependencies.json"
        source_dependencies_final = release_dir / "source_dependencies.json"
        source_dependencies = {
            "acceptance": "PASS",
            "artifact_family": "transportation_release_source_dependencies",
            "model_family": MODEL_FAMILY,
            "git_commit_sha": source_state["git_commit_sha"],
            "dependency_count": len(required_sources),
            "dependencies": {
                path: _reference(
                    PROJECT_ROOT / path,
                    PROJECT_ROOT / path,
                    recurse=False,
                )
                for path in required_sources
            },
        }
        write_text_atomic(
            source_dependencies_path,
            json.dumps(source_dependencies, indent=2, sort_keys=True) + "\n",
        )

        portfolio_path = staging_dir / "transportation_portfolio_contract.json"
        portfolio_final = release_dir / "transportation_portfolio_contract.json"
        write_text_atomic(
            portfolio_path,
            json.dumps(_portfolio_contract(), indent=2, sort_keys=True) + "\n",
        )

        manifest_path = staging_dir / "transportation_release_manifest.json"
        manifest = {
            "acceptance": "PASS",
            "artifact_family": "transportation_immutable_shadow_release",
            "model_family": MODEL_FAMILY,
            "asof_date": asof,
            "release_name": release_name,
            "git_commit_sha": source_state["git_commit_sha"],
            "release_state": "FAIL_CLOSED_SHADOW",
            "production_model_promoted": False,
            "production_allocation_authorized": False,
            "evidence": evidence,
            "source_dependencies": _reference(
                source_dependencies_final,
                source_dependencies_path,
            ),
            "portfolio_contract": _reference(portfolio_final, portfolio_path),
            "artifacts": artifact_refs,
            "artifact_count": len(artifact_refs),
            "operations": {
                "network_requests": 0,
                "parser_invocations": 0,
                "data_loads": 0,
                "feature_rebuilds": 0,
                "historical_materializations": 0,
                "calibration_invocations": 0,
                "portfolio_writes": 0,
                "production_config_writes": 0,
            },
        }
        write_text_atomic(
            manifest_path,
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        )
        staging_dir.replace(release_dir)
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    payload: dict[str, Any] = {
        **manifest,
        "release_dir": str(release_dir),
        "release_manifest": _reference(
            release_dir / "transportation_release_manifest.json",
            release_dir / "transportation_release_manifest.json",
        ),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

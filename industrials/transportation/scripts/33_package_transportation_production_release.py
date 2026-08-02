#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import load_yaml  # noqa: E402
from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.release_contract import (  # noqa: E402
    MODEL_FAMILY,
    git_source_state,
    iter_existing_files,
    required_release_source_paths,
)
from industrials.transportation.selected_feature_history import (  # noqa: E402
    read_json,
    sha256,
)


DEFAULT_RELEASE_NAME = "generic_oos_production_v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Package a new immutable transportation production release from "
            "already-built and activated artifacts. No retrieval, parsing, "
            "feature building, calibration, promotion, or portfolio write occurs."
        )
    )
    parser.add_argument("--asof", required=True)
    parser.add_argument("--release-name", default=DEFAULT_RELEASE_NAME)
    parser.add_argument("--release-dir", type=Path, default=None)
    return parser.parse_args()


def reference(
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


def lock_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {str(key): str(value or "").strip() for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def portfolio_contract() -> dict[str, object]:
    config_path = PROJECT_ROOT / "portfolio_layer" / "config.yaml"
    config = load_yaml(config_path)
    sectors = (config.get("score_contract") or {}).get("sectors") or []
    matches = [
        dict(source)
        for source in sectors
        if str(source.get("model_family") or "") == MODEL_FAMILY
    ]
    if len(matches) != 1:
        raise ValueError(
            "expected one transportation portfolio source, found={}".format(
                len(matches)
            )
        )
    source = matches[0]
    cap = float(
        ((config.get("optimizer") or {}).get("sector_weight_caps") or {}).get(
            MODEL_FAMILY, 0.0
        )
    )
    if not (
        source.get("adapter") == "industrial_family"
        and source.get("enabled") is True
        and source.get("required") is True
        and source.get("require_oos_score_valid") is True
        and abs(cap - 0.05) <= 1e-12
    ):
        raise ValueError(
            "transportation production portfolio contract is invalid: "
            "source={} cap={}".format(source, cap)
        )
    return {
        "acceptance": "PASS",
        "artifact_family": "transportation_portfolio_contract_snapshot",
        "model_family": MODEL_FAMILY,
        "source": source,
        "sector_weight_cap": cap,
        "production_allocation_authorized": True,
    }


def validate_evidence(asof: str) -> dict[str, object]:
    root = PROJECT_ROOT / "output" / "industrials" / MODEL_FAMILY
    promotion = root / "production_promotion" / asof
    dashboard = root / "dashboard" / asof
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
        "promotion": promotion
        / "transportation_production_promotion_manifest.json",
        "activation": promotion
        / "transportation_production_activation_manifest.json",
        "rank": dashboard / "transportation_final_rank_table_manifest.json",
        "portfolio": dashboard
        / "transportation_portfolio_adapter_production_validation.json",
    }
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError("missing production release evidence={}".format(missing))
    data = {name: read_json(path) for name, path in paths.items()}
    failures: list[str] = []
    if data["history"].get("acceptance") != "PASS":
        failures.append("daily history validation")
    if data["panel"].get("acceptance") != "PASS":
        failures.append("generic OOS panel validation")
    if data["calibration"].get("artifact_acceptance") != "PASS":
        failures.append("calibration artifact acceptance")
    if data["calibration"].get("promotion_eligible") is not True:
        failures.append("calibration promotion eligibility")
    if (
        data["readiness"].get("promotion_readiness") != "PASS"
        or data["readiness"].get("promotion_eligible") is not True
    ):
        failures.append("production readiness")
    if (
        data["promotion"].get("promoted") is not True
        or data["promotion"].get("asof_date") != asof
    ):
        failures.append("production promotion")
    if (
        data["activation"].get("acceptance") != "PASS"
        or data["activation"].get("activated") is not True
        or data["activation"].get("effective_date") != asof
    ):
        failures.append("production activation")
    if (
        data["rank"].get("acceptance") != "PASS"
        or int(data["rank"].get("portfolio_candidate_count") or 0) <= 0
        or int(data["rank"].get("oos_score_valid_count") or 0) <= 0
    ):
        failures.append("production rank")
    if (
        data["portfolio"].get("acceptance") != "PASS"
        or int(data["portfolio"].get("investable_rows") or 0) <= 0
    ):
        failures.append("production portfolio adapter")
    if failures:
        raise ValueError(
            "production release evidence failures={}".format(failures)
        )
    return {
        "artifacts": {
            name: reference(path, path, recurse=False)
            for name, path in paths.items()
        },
        "portfolio_candidate_rows": int(
            data["rank"].get("portfolio_candidate_count") or 0
        ),
        "oos_score_valid_rows": int(
            data["rank"].get("oos_score_valid_count") or 0
        ),
        "investable_rows": int(
            data["portfolio"].get("investable_rows") or 0
        ),
        "lock_id": str(data["activation"].get("lock_id") or ""),
    }


def inventory(asof: str) -> list[tuple[Path, Path, bool]]:
    root = PROJECT_ROOT / "output" / "industrials" / MODEL_FAMILY
    entries: list[tuple[Path, Path, bool]] = []

    def add_tree(source_root: Path, target_root: Path) -> None:
        for source in iter_existing_files([source_root]):
            entries.append(
                (source, target_root / source.relative_to(source_root), True)
            )

    add_tree(root / "generic_oos", Path("generic_oos"))
    add_tree(root / "score_history", Path("score_history"))
    add_tree(
        root / "production_promotion" / asof,
        Path("production_promotion"),
    )
    add_tree(root / "dashboard" / asof, Path("current") / "dashboard")
    lock_path = (
        PROJECT_ROOT
        / "industrials"
        / "transportation"
        / "system_csvs"
        / "transportation_production_model_locks.csv"
    )
    entries.append(
        (
            lock_path,
            Path("governance") / lock_path.name,
            False,
        )
    )
    portfolio_config = PROJECT_ROOT / "portfolio_layer" / "config.yaml"
    entries.append(
        (
            portfolio_config,
            Path("governance") / "portfolio_layer_config.yaml",
            False,
        )
    )
    return sorted(entries, key=lambda item: item[1].as_posix())


def main() -> int:
    args = parse_args()
    asof = str(args.asof)[:10]
    release_name = str(args.release_name).strip()
    if not release_name:
        raise ValueError("--release-name is required")
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
        raise FileExistsError(
            "immutable release directory already exists={}".format(release_dir)
        )

    required_sources = required_release_source_paths(PROJECT_ROOT)
    source_state, source_errors = git_source_state(
        PROJECT_ROOT,
        required_sources,
    )
    if source_errors:
        raise ValueError(
            "release sources are not committed and clean={}".format(
                source_errors
            )
        )
    evidence = validate_evidence(asof)
    contract = portfolio_contract()
    lock_path = (
        PROJECT_ROOT
        / "industrials"
        / "transportation"
        / "system_csvs"
        / "transportation_production_model_locks.csv"
    )
    active: list[dict[str, str]] = []
    for row in lock_rows(lock_path):
        effective_from = str(row.get("effective_from") or "")
        effective_to = str(row.get("effective_to") or "")
        if (
            row.get("enabled") == "1"
            and effective_from
            and effective_from <= asof
            and (not effective_to or asof <= effective_to)
        ):
            active.append(row)
    if len(active) != 1 or active[0].get("lock_id") != evidence["lock_id"]:
        raise ValueError("production lock registry does not match activation")

    items = inventory(asof)
    missing = [str(source) for source, _, _ in items if not source.is_file()]
    if missing:
        raise FileNotFoundError("missing release artifacts={}".format(missing))

    release_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix="." + release_name + ".",
            dir=release_dir.parent,
        )
    ).resolve()
    try:
        artifact_refs: dict[str, dict[str, object]] = {}
        for source, relative, recurse in items:
            staged = staging / relative
            final = release_dir / relative
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, staged)
            artifact_refs[relative.as_posix()] = reference(
                final,
                staged,
                recurse=recurse,
            )

        source_path = staging / "source_dependencies.json"
        source_final = release_dir / "source_dependencies.json"
        source_payload = {
            "acceptance": "PASS",
            "artifact_family": "transportation_release_source_dependencies",
            "model_family": MODEL_FAMILY,
            "git_commit_sha": source_state["git_commit_sha"],
            "dependency_count": len(required_sources),
            "dependencies": {
                path: reference(
                    PROJECT_ROOT / path,
                    PROJECT_ROOT / path,
                    recurse=False,
                )
                for path in required_sources
            },
        }
        write_text_atomic(
            source_path,
            json.dumps(source_payload, indent=2, sort_keys=True) + "\n",
        )
        contract_path = staging / "transportation_portfolio_contract.json"
        contract_final = release_dir / "transportation_portfolio_contract.json"
        write_text_atomic(
            contract_path,
            json.dumps(contract, indent=2, sort_keys=True) + "\n",
        )
        manifest = {
            "acceptance": "PASS",
            "artifact_family": "transportation_immutable_production_release",
            "model_family": MODEL_FAMILY,
            "asof_date": asof,
            "release_name": release_name,
            "git_commit_sha": source_state["git_commit_sha"],
            "release_state": "PRODUCTION_GENERIC_OOS_ACTIVE",
            "production_model_promoted": True,
            "production_allocation_authorized": True,
            "evidence": evidence,
            "source_dependencies": reference(source_final, source_path),
            "portfolio_contract": reference(contract_final, contract_path),
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
            staging / "transportation_release_manifest.json",
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        )
        staging.replace(release_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    print(
        json.dumps(
            {
                "acceptance": "PASS",
                "release_dir": str(release_dir),
                "release_name": release_name,
                "artifact_count": len(items),
                "production_allocation_authorized": True,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

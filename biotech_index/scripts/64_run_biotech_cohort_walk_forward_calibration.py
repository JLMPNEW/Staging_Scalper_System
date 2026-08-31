#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.cohort_calibration import (  # noqa: E402
    BIOTECH_CALIBRATION_COHORTS,
    cohort_output_directory_name,
    validate_calibration_cohorts,
    validate_cohort_budget_weights,
)
from biotech_index.core.cohort_portfolio import (  # noqa: E402
    CohortPromotionStatus,
    aligned_fold_manifest,
    cohort_promotion_status,
    combine_cohort_selection_rows,
)
from biotech_index.core.config import cfg_get, load_yaml, resolve_optional_path, resolve_path  # noqa: E402
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402


LOGGER = logging.getLogger("biotech_cohort_walk_forward")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
FRAMEWORK_VERSION = "biotech_cohort_walk_forward_v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Calibrate, validate, and promote each official biotech cohort independently, then apply a "
            "single net-of-cost portfolio replay after the five cohort sleeves are combined."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--observations-csv", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--candidate-limit", type=int, default=0)
    parser.add_argument("--max-workers", type=int, default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--run-optuna", action="store_true")
    parser.add_argument("--no-optuna", dest="run_optuna", action="store_false")
    parser.set_defaults(run_optuna=False)
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv(path: Path, rows: Iterable[Mapping[str, object]]) -> None:
    materialized = [dict(row) for row in rows]
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in materialized for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(materialized)


def read_json(path: Path) -> dict[str, object]:
    if not path.exists():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return payload


def write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_observations(config: dict[str, Any], config_path: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    configured = resolve_optional_path(
        cfg_get(config, "calibration.walk_forward.observations_csv", ""),
        base_dir=config_path.parent,
    )
    if configured is None:
        raise ValueError(
            "Set calibration.walk_forward.observations_csv or pass --observations-csv with the clean PIT panel"
        )
    return configured


def cohort_settings(config: dict[str, Any]) -> tuple[tuple[str, ...], dict[str, float]]:
    raw = cfg_get(config, "calibration.walk_forward.cohort_calibration", {}) or {}
    if not isinstance(raw, Mapping):
        raise ValueError("calibration.walk_forward.cohort_calibration must be a mapping")
    cohorts = validate_calibration_cohorts(raw.get("cohorts") or BIOTECH_CALIBRATION_COHORTS)
    if set(cohorts) != set(BIOTECH_CALIBRATION_COHORTS):
        raise ValueError("Production cohort calibration requires exactly the five official biotech cohorts")
    raw_budgets = raw.get("portfolio_budget_weights") or {}
    if not isinstance(raw_budgets, Mapping):
        raise ValueError("cohort_calibration.portfolio_budget_weights must be a mapping")
    return cohorts, validate_cohort_budget_weights(cohorts, raw_budgets)


def successful_run(path: Path, cohort: str) -> bool:
    required_paths = {
        "walk_forward": path / "walk_forward_run_manifest.json",
        "profitability": path / "portfolio_profitability_manifest.json",
        "verification": path / "portfolio_profitability_verification.json",
        "contract": path / "production_policy_contract_profitability_candidate.json",
    }
    if any(not artifact.exists() for artifact in required_paths.values()):
        return False
    try:
        manifest = read_json(required_paths["walk_forward"])
        profitability = read_json(required_paths["profitability"])
        verification = read_json(required_paths["verification"])
        contract = read_json(required_paths["contract"])
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    contract_verification = contract.get("profitability_replay_verification") or {}
    if not isinstance(contract_verification, Mapping):
        return False
    return (
        manifest.get("status") == "success"
        and manifest.get("calibration_scope") == "cohort"
        and manifest.get("calibration_cohort") == cohort
        and profitability.get("status") == "success"
        and verification.get("verification_status") == "pass"
        and verification.get("independent_normalized_input_replay") is True
        and contract_verification.get("verification_status") == "pass"
    )


def run_cohort(
    *,
    cohort: str,
    output_dir: Path,
    config_path: Path,
    observations_path: Path,
    candidate_limit: int,
    max_workers: int | None,
    run_optuna: bool,
    resume: bool,
) -> None:
    if resume and successful_run(output_dir, cohort):
        LOGGER.info("Reusing completed cohort calibration: %s", cohort)
        return
    command = [
        sys.executable,
        str(PACKAGE_ROOT / "scripts" / "60_run_biotech_walk_forward_calibration.py"),
        "--config",
        str(config_path),
        "--observations-csv",
        str(observations_path),
        "--output-dir",
        str(output_dir),
        "--cohort-filter",
        cohort,
        "--no-survivor-fallback",
        "production_incumbent",
        "--candidate-limit",
        str(max(0, candidate_limit)),
        "--run-optuna" if run_optuna else "--no-optuna",
    ]
    if max_workers is not None:
        command.extend(("--max-workers", str(max(1, max_workers))))
    if resume:
        command.append("--resume")
    LOGGER.info("Starting independent calibration for cohort=%s", cohort)
    subprocess.run(command, check=True, cwd=PROJECT_ROOT)


def _annotated_rows(path: Path, cohort: str) -> list[dict[str, object]]:
    return [{**row, "calibration_cohort": cohort} for row in read_csv(path)]


def load_cohort_outputs(
    cohort_dirs: Mapping[str, Path],
) -> tuple[
    dict[str, list[dict[str, object]]],
    dict[str, list[dict[str, object]]],
    dict[str, list[dict[str, object]]],
    dict[str, list[dict[str, object]]],
    dict[str, CohortPromotionStatus],
    dict[str, dict[str, object]],
]:
    manifests: dict[str, list[dict[str, object]]] = {}
    selected: dict[str, list[dict[str, object]]] = {}
    sleeves: dict[str, list[dict[str, object]]] = {}
    comparisons: dict[str, list[dict[str, object]]] = {}
    statuses: dict[str, CohortPromotionStatus] = {}
    contracts: dict[str, dict[str, object]] = {}
    for cohort in BIOTECH_CALIBRATION_COHORTS:
        directory = cohort_dirs[cohort]
        manifests[cohort] = _annotated_rows(directory / "walk_forward_fold_manifest.csv", cohort)
        selected[cohort] = _annotated_rows(directory / "walk_forward_selected_tickers.csv", cohort)
        sleeves[cohort] = _annotated_rows(directory / "adaptive_sleeve_allocation_replay.csv", cohort)
        comparisons[cohort] = _annotated_rows(
            directory / "walk_forward_outer_test_comparisons.csv",
            cohort,
        )
        contract = read_json(directory / "production_policy_contract_candidate.json")
        statistical = read_json(directory / "walk_forward_statistical_promotion_decision.json")
        profitability = read_json(directory / "profitability_promotion_decision.json")
        fold_contract = contract.get("latest_primary_fold_contract") or {}
        if not isinstance(fold_contract, Mapping):
            raise ValueError(f"Invalid latest fold contract for cohort={cohort}")
        statuses[cohort] = cohort_promotion_status(
            cohort,
            statistical_decision=statistical,
            profitability_decision=profitability,
            fold_contract=fold_contract,
        )
        contracts[cohort] = contract
    return manifests, selected, sleeves, comparisons, statuses, contracts


def aggregate_optional_csv(
    cohort_dirs: Mapping[str, Path],
    filename: str,
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for cohort in BIOTECH_CALIBRATION_COHORTS:
        path = cohort_dirs[cohort] / filename
        if path.exists():
            output.extend(_annotated_rows(path, cohort))
    return output


def main() -> int:
    args = parse_args()
    configure_utc_logging()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    observations_path = resolve_observations(config, config_path, args.observations_csv)
    cohorts, budgets = cohort_settings(config)
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir is not None
        else resolve_path(
            cfg_get(
                config,
                "calibration.walk_forward.cohort_calibration.output_dir",
                "../output/biotech_index_reports/calibration_walk_forward_by_cohort",
            ),
            base_dir=config_path.parent,
        )
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    cohort_dirs = {
        cohort: output_dir / "cohorts" / cohort_output_directory_name(cohort)
        for cohort in cohorts
    }
    write_json(
        output_dir / "cohort_directory_map.json",
        {cohort: str(directory.relative_to(output_dir)) for cohort, directory in cohort_dirs.items()},
    )
    for cohort in cohorts:
        run_cohort(
            cohort=cohort,
            output_dir=cohort_dirs[cohort],
            config_path=config_path,
            observations_path=observations_path,
            candidate_limit=int(args.candidate_limit),
            max_workers=args.max_workers,
            run_optuna=False,
            resume=bool(args.resume),
        )

    manifests, selected, sleeves, comparisons, statuses, contracts = load_cohort_outputs(cohort_dirs)
    if args.run_optuna:
        optuna_survivors = [
            cohort
            for cohort in cohorts
            if statuses[cohort].authorized
        ]
        if optuna_survivors:
            LOGGER.info("Optuna is restricted to deterministic cohort survivor(s): %s", optuna_survivors)
        else:
            LOGGER.info("No deterministic cohort survivor qualified for Optuna; skipping optimization")
        for cohort in optuna_survivors:
            optimized_dir = (
                output_dir / "cohorts_optuna" / cohort_output_directory_name(cohort)
            )
            run_cohort(
                cohort=cohort,
                output_dir=optimized_dir,
                config_path=config_path,
                observations_path=observations_path,
                candidate_limit=int(args.candidate_limit),
                max_workers=args.max_workers,
                run_optuna=True,
                resume=bool(args.resume),
            )
            cohort_dirs[cohort] = optimized_dir
        if optuna_survivors:
            manifests, selected, sleeves, comparisons, statuses, contracts = load_cohort_outputs(
                cohort_dirs
            )
    combined_manifest = aligned_fold_manifest(manifests)
    primary_horizon = int(cfg_get(config, "calibration.walk_forward.primary_horizon", 120))
    combined_selected, combined_sleeves = combine_cohort_selection_rows(
        selected_rows_by_cohort=selected,
        sleeve_rows_by_cohort=sleeves,
        comparison_rows_by_cohort=comparisons,
        promotion_status_by_cohort=statuses,
        cohort_budget_weights=budgets,
        primary_horizon=primary_horizon,
    )

    status_rows = [statuses[cohort].as_dict() for cohort in BIOTECH_CALIBRATION_COHORTS]
    comparison_rows = [row for cohort in BIOTECH_CALIBRATION_COHORTS for row in comparisons[cohort]]
    candidate_metric_rows = aggregate_optional_csv(cohort_dirs, "walk_forward_candidate_metrics.csv")
    optuna_rows = aggregate_optional_csv(cohort_dirs, "optuna_fold_trials.csv")
    write_csv(output_dir / "walk_forward_fold_manifest.csv", combined_manifest)
    write_csv(output_dir / "walk_forward_selected_tickers.csv", combined_selected)
    write_csv(output_dir / "adaptive_selection_replay.csv", combined_selected)
    write_csv(output_dir / "adaptive_sleeve_allocation_replay.csv", combined_sleeves)
    write_csv(output_dir / "cohort_walk_forward_outer_test_comparisons.csv", comparison_rows)
    write_csv(output_dir / "cohort_promotion_decisions.csv", status_rows)
    write_csv(output_dir / "walk_forward_candidate_metrics.csv", candidate_metric_rows)
    if optuna_rows:
        write_csv(output_dir / "optuna_fold_trials.csv", optuna_rows)
    write_json(
        output_dir / "cohort_promotion_decisions.json",
        {cohort: statuses[cohort].as_dict() for cohort in BIOTECH_CALIBRATION_COHORTS},
    )

    authorized_cohorts = [cohort for cohort in BIOTECH_CALIBRATION_COHORTS if statuses[cohort].authorized]
    cohort_contract = {
        "contract_version": "biotech_cohort_promotion_contract_v1",
        "framework_version": FRAMEWORK_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "primary_horizon": primary_horizon,
        "calibration_scope": "cohort_specific_combined_portfolio",
        "cohort_budget_weights": budgets,
        "cohort_promotion_status": {
            cohort: statuses[cohort].as_dict() for cohort in BIOTECH_CALIBRATION_COHORTS
        },
        "cohort_contracts": {
            cohort: {
                "latest_primary_fold_contract": contracts[cohort].get("latest_primary_fold_contract") or {},
                "monitoring_contract": contracts[cohort].get("monitoring_contract") or {},
                "source_provenance": contracts[cohort].get("source_provenance") or {},
            }
            for cohort in BIOTECH_CALIBRATION_COHORTS
        },
        "statistically_and_economically_authorized_cohorts": authorized_cohorts,
        "production_promotion_authorized": bool(authorized_cohorts),
        "live_deployment_ready": bool(authorized_cohorts),
        "activation_status": "pending_global_portfolio_risk_gate",
        "source_provenance": {
            "observation_csv": str(observations_path),
            "observation_csv_sha256": sha256_file(observations_path),
        },
        "monitoring_contract": cfg_get(config, "calibration.walk_forward.monitoring", {}) or {},
    }
    write_json(output_dir / "production_policy_contract_candidate.json", cohort_contract)
    root_manifest = {
        "status": "cohort_calibrations_complete_pending_global_replay",
        "framework_version": FRAMEWORK_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "primary_horizon": primary_horizon,
        "calibration_scope": "cohort_specific_combined_portfolio",
        "cohort_count": len(cohorts),
        "production_promotion_authorized": False,
        "source_provenance": cohort_contract["source_provenance"],
    }
    write_json(output_dir / "walk_forward_run_manifest.json", root_manifest)

    profitability_script = PACKAGE_ROOT / "scripts" / "62_compare_biotech_portfolio_profitability.py"
    subprocess.run(
        [
            sys.executable,
            str(profitability_script),
            "--config",
            str(config_path),
            "--calibration-output-dir",
            str(output_dir),
        ],
        check=True,
        cwd=PROJECT_ROOT,
    )
    global_profitability = read_json(output_dir / "profitability_promotion_decision.json")
    profitability_verification = read_json(output_dir / "portfolio_profitability_verification.json")
    global_risk_gate_passed = global_profitability.get("profitability_promotion_authorized") is True
    final_authorized = bool(authorized_cohorts) and global_risk_gate_passed
    cohort_contract["global_portfolio_profitability_decision"] = global_profitability
    cohort_contract["profitability_replay_verification"] = profitability_verification
    cohort_contract["global_portfolio_risk_gate_passed"] = global_risk_gate_passed
    cohort_contract["production_promotion_authorized"] = final_authorized
    cohort_contract["activation_status"] = (
        "candidate_requires_explicit_activation" if final_authorized else "not_authorized"
    )
    write_json(output_dir / "production_cohort_policy_contract_candidate.json", cohort_contract)
    root_manifest["status"] = "success"
    root_manifest["production_promotion_authorized"] = final_authorized
    root_manifest["authorized_cohorts"] = authorized_cohorts
    root_manifest["global_portfolio_risk_gate_passed"] = global_risk_gate_passed
    root_manifest["artifacts"] = {
        path.name: {"path": str(path), "sha256": sha256_file(path)}
        for path in sorted(output_dir.iterdir())
        if path.is_file() and path.name != "walk_forward_run_manifest.json"
    }
    write_json(output_dir / "walk_forward_run_manifest.json", root_manifest)
    LOGGER.info(
        "Cohort calibration complete: authorized_cohorts=%s global_risk_gate=%s output=%s",
        authorized_cohorts,
        global_risk_gate_passed,
        output_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

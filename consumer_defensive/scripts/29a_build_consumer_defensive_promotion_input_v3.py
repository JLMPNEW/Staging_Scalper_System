#!/usr/bin/env python3
"""Build sealed promotion-v3 design evidence from a script-29 artifact set.

This command is database-free and report-only.  It cannot label a retrospective
calibration as fresh chronological evidence and never writes Portfolio state.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from consumer_defensive.core.promotion_artifacts_v3 import publish_immutable_json  # noqa: E402
from consumer_defensive.core.promotion_bridge_v3 import (  # noqa: E402
    DESIGN_EVIDENCE_MAXIMUM_STATE,
    DESIGN_EVIDENCE_ROLE,
    build_bridge_artifacts,
    file_sha256,
    methodology_file_sha256s,
)
from consumer_defensive.core.promotion_engine_v3 import load_framework as load_framework_v3  # noqa: E402
from consumer_defensive.core.promotion_framework_v2 import load_framework as load_framework_v2  # noqa: E402


CALIBRATION_FILES = {
    "input_manifest": "consumer_defensive_calibration_input_manifest_v2.json",
    "fold_registry": "consumer_defensive_calibration_fold_registry_v2.json",
    "realized_path_attestation": "consumer_defensive_calibration_realized_path_attestation_v2.json",
    "matched_benchmark_attestation": "consumer_defensive_matched_benchmark_attestation_v3.json",
    "results": "consumer_defensive_calibration_results_v2.json",
    "decision": "consumer_defensive_calibration_decision_v2.json",
    "independent_validation": "consumer_defensive_calibration_independent_validation_v2.json",
}
PREREGISTRATION_FILES = {
    "candidate_registry": "consumer_defensive_calibration_candidate_registry_v2.json",
    "preregistration": "consumer_defensive_calibration_preregistration_v2.json",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration-root", type=Path, required=True,
                        help="Immutable output directory produced by script 29")
    parser.add_argument("--prereg-root", type=Path, required=True,
                        help="Immutable candidate-registry/preregistration directory")
    parser.add_argument("--calibration-framework", type=Path,
        default=ROOT / "consumer_defensive/data/consumer_defensive_promotion_framework_v2.yaml")
    parser.add_argument("--promotion-framework", type=Path,
        default=ROOT / "consumer_defensive/data/consumer_defensive_promotion_framework_v3.yaml")
    parser.add_argument(
        "--capital-context",
        type=Path,
        required=True,
        help="Immutable portfolio_capital_context_v1 JSON artifact",
    )
    parser.add_argument(
        "--trusted-capital-context-file-sha256",
        required=True,
        help="Trusted SHA-256 of the exact capital-context file bytes",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def _duplicate_safe_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON object contains a duplicate key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"JSON contains a non-finite constant: {value}")


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or resolved.is_symlink():
        raise ValueError(f"{label} is missing or unsafe: {resolved}")
    payload = json.loads(resolved.read_text(encoding="utf-8"),
        object_pairs_hook=_duplicate_safe_object, parse_constant=_reject_constant)
    if not isinstance(payload, Mapping):
        raise ValueError(f"{label} must contain a JSON object")
    return dict(payload)


def _source_file(root: Path, filename: str, *, label: str) -> Path:
    resolved_root = root.expanduser().resolve()
    if not resolved_root.is_dir() or resolved_root.is_symlink():
        raise ValueError(f"{label} root is missing or unsafe: {resolved_root}")
    resolved = (resolved_root / filename).resolve()
    if resolved.parent != resolved_root or not resolved.is_file() or resolved.is_symlink():
        raise ValueError(f"{label} is missing or unsafe: {resolved}")
    return resolved


def main() -> int:
    args = _parser().parse_args()
    paths: dict[str, Path] = {
        key: _source_file(args.calibration_root, filename, label=key)
        for key, filename in CALIBRATION_FILES.items()
    }
    paths.update({
        key: _source_file(args.prereg_root, filename, label=key)
        for key, filename in PREREGISTRATION_FILES.items()
    })
    calibration_framework_path = args.calibration_framework.expanduser().resolve()
    promotion_framework_path = args.promotion_framework.expanduser().resolve()
    artifacts = {key: _read_json(path, label=key) for key, path in paths.items()}
    calibration_framework = load_framework_v2(calibration_framework_path)
    promotion_framework = load_framework_v3(promotion_framework_path)
    capital_context_path = args.capital_context.expanduser().resolve()
    portfolio_capital_context = _read_json(
        capital_context_path,
        label="capital context",
    )
    capital_context_file_sha256 = file_sha256(capital_context_path)
    source_files = {key: file_sha256(path) for key, path in paths.items()}
    source_files["promotion_framework_v2"] = file_sha256(calibration_framework_path)
    source_files["promotion_framework_v3"] = file_sha256(promotion_framework_path)
    built = build_bridge_artifacts(artifacts=artifacts,
        calibration_framework=calibration_framework,
        promotion_framework=promotion_framework,
        source_file_sha256s=source_files,
        bridge_methodology_file_sha256s=methodology_file_sha256s(ROOT),
        portfolio_capital_context=portfolio_capital_context,
        capital_context_file_sha256=capital_context_file_sha256,
        trusted_capital_context_file_sha256=(
            args.trusted_capital_context_file_sha256
        ))

    output = args.output_dir.expanduser().resolve()
    publish_immutable_json(
        output / "portfolio_capital_context_v1.json",
        built["portfolio_capital_context"],
    )
    publish_immutable_json(
        output / "consumer_defensive_capital_allocation_context_v1.json",
        built["capital_allocation_context"],
    )
    for cohort, contract in sorted(built["production_model_contracts"].items()):
        publish_immutable_json(output / f"consumer_defensive_production_model_contract_{cohort}_v3.json", contract)
    publish_immutable_json(output / "consumer_defensive_matched_benchmark_attestation_v3.json",
                           built["benchmark_attestation"])
    publish_immutable_json(output / "consumer_defensive_promotion_input_v3.json",
                           built["promotion_input"])
    publish_immutable_json(output / "consumer_defensive_promotion_input_build_attestation_v3.json",
                           built["input_build_attestation"])
    summary = {
        "schema_version": "consumer_defensive_promotion_input_bridge_run_v3",
        "status": "PASS", "model_family": "consumer_defensive",
        "asof_date": built["promotion_input"]["asof_date"],
        "evidence_role": DESIGN_EVIDENCE_ROLE,
        "maximum_authorized_state": DESIGN_EVIDENCE_MAXIMUM_STATE,
        "capital_context_asof_date": built["portfolio_capital_context"]["asof_date"],
        "capital_context_source_path": str(capital_context_path),
        "capital_context_file_sha256": capital_context_file_sha256,
        "capital_context_payload_sha256": (
            built["portfolio_capital_context"]["payload_sha256"]
        ),
        "normalized_capital_context_payload_sha256": (
            built["capital_allocation_context"]["payload_sha256"]
        ),
        "capital_context_counts_as_fresh_predictive_evidence": False,
        "promotion_input_sha256": built["promotion_input"]["payload_sha256"],
        "benchmark_attestation_sha256": built["benchmark_attestation"]["payload_sha256"],
        "input_build_attestation_sha256": built["input_build_attestation"]["payload_sha256"],
        "production_model_contract_sha256s": {
            cohort: contract["payload_sha256"]
            for cohort, contract in sorted(built["production_model_contracts"].items())
        },
        "database_read_performed": False, "database_write_performed": False,
        "portfolio_write_performed": False, "production_activation_performed": False,
        "output_directory": str(output),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

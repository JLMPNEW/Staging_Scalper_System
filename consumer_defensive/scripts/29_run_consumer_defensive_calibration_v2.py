#!/usr/bin/env python3
"""Run the preregistered Consumer Defensive v2 calibration in report-only mode."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from consumer_defensive.core.calibration_execution_v2 import (  # noqa: E402
    load_preregistration_pair,
    run_sequence1_calibration,
)
from consumer_defensive.core.config import (  # noqa: E402
    ConfigBundle,
    cfg_get,
    load_config,
    resolve_path,
)
from consumer_defensive.core.promotion_framework_v2 import (  # noqa: E402
    framework_sha256,
    load_framework,
)
from consumer_defensive.core.shared_services import (  # noqa: E402
    load_shared_service_contract,
    shared_service_contract_sha256,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        required=True,
        type=Path,
        help="Explicit source SQLite database; it is opened in URI read-only mode",
    )
    parser.add_argument(
        "--factor-root",
        required=True,
        type=Path,
        help="Explicit immutable factor-validation evidence root",
    )
    parser.add_argument(
        "--prereg-root",
        required=True,
        type=Path,
        help="Directory containing the immutable candidate-registry/preregistration pair",
    )
    parser.add_argument("--config", type=Path, default=ROOT / "consumer_defensive/config.yaml")
    parser.add_argument("--framework", type=Path, help="Override the framework path from config")
    parser.add_argument(
        "--shared-service-contract",
        type=Path,
        help="Override the shared-service contract path from config",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=ROOT / "output/consumer_defensive/framework_v2/sequence1",
        help="Base directory; the preregistered as-of directory is appended",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Execute the complete calibration but do not publish report artifacts",
    )
    return parser


def _safe_file(path: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or resolved.is_symlink():
        raise FileNotFoundError(f"{label} is missing or unsafe: {resolved}")
    return resolved


def _safe_directory(path: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_dir() or resolved.is_symlink():
        raise FileNotFoundError(f"{label} is missing or unsafe: {resolved}")
    return resolved


def _configured_path(
    bundle: ConfigBundle,
    override: Path | None,
    *,
    config_key: str,
    label: str,
) -> Path:
    path = (
        override
        if override is not None
        else resolve_path(cfg_get(bundle.payload, config_key), base_dir=bundle.base_dir)
    )
    return _safe_file(path, label=label)


def _open_read_only(path: Path) -> sqlite3.Connection:
    resolved = _safe_file(path, label="--db")
    conn = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        conn.close()
        raise RuntimeError("failed to establish a query-only SQLite connection")
    return conn


def main() -> int:
    args = _parser().parse_args()
    bundle = load_config(_safe_file(args.config, label="--config"))
    framework_path = _configured_path(
        bundle,
        args.framework,
        config_key="promotion_framework_v2.framework_path",
        label="promotion framework",
    )
    shared_path = _configured_path(
        bundle,
        args.shared_service_contract,
        config_key="promotion_framework_v2.shared_service_contract_path",
        label="shared-service contract",
    )
    factor_root = _safe_directory(args.factor_root, label="--factor-root")
    prereg_root = _safe_directory(args.prereg_root, label="--prereg-root")

    # The immutable, label-blind pair must validate before this process opens
    # the database from which the forward labels are read.
    preregistration, candidate_registry = load_preregistration_pair(prereg_root)
    framework = load_framework(framework_path)
    shared_contract = load_shared_service_contract(shared_path)
    shared_hash = shared_service_contract_sha256(shared_contract)
    if shared_hash != framework["ownership"]["shared_service_contract_sha256"]:
        raise ValueError("framework is not bound to the supplied shared-service contract")
    if framework_sha256(framework) != preregistration["framework_sha256"]:
        raise ValueError("framework changed after calibration preregistration")
    if shared_hash != preregistration["shared_service_contract_sha256"]:
        raise ValueError("shared-service contract changed after calibration preregistration")

    asof_date = str(preregistration["asof_date"])
    output_dir = (
        args.output_root.expanduser().resolve()
        / asof_date
        / str(preregistration["payload_sha256"])[:16]
    )
    with _open_read_only(args.db) as conn:
        payload = run_sequence1_calibration(
            conn,
            bundle,
            repository_root=ROOT,
            framework=framework,
            preregistration=preregistration,
            candidate_registry=candidate_registry,
            factor_root=factor_root,
            output_dir=None if args.dry_run else output_dir,
        )

    decision = payload["decision"]
    results = payload["results"]
    print(
        json.dumps(
            {
                "schema_version": "consumer_defensive_calibration_execution_run_v2",
                "status": "PASS",
                "model_family": "consumer_defensive",
                "asof_date": asof_date,
                "decision_sequence": decision["decision_sequence"],
                "decision_payload_sha256": decision["payload_sha256"],
                "candidate_registry_sha256": candidate_registry["payload_sha256"],
                "preregistration_sha256": preregistration["payload_sha256"],
                "input_manifest_sha256": payload["input_manifest"]["payload_sha256"],
                "fold_registry_sha256": payload["fold_registry"]["payload_sha256"],
                "realized_path_attestation_sha256": payload["path_attestation"][
                    "payload_sha256"
                ],
                "matched_benchmark_attestation_sha256": payload[
                    "benchmark_attestation"
                ]["payload_sha256"],
                "results_sha256": results["payload_sha256"],
                "independent_validation_status": payload["independent_validation"]["status"],
                "accepted_specialized_factor_cell_count": results[
                    "accepted_specialized_factor_cell_count"
                ],
                "cohort_states": {
                    cohort: item["state"] for cohort, item in sorted(decision["cohorts"].items())
                },
                "cohort_failed_gate_counts": {
                    cohort: len(item["failed_gates"])
                    for cohort, item in sorted(decision["cohorts"].items())
                },
                "production_promotion_enabled": False,
                "database_write_performed": False,
                "portfolio_write_performed": False,
                "dry_run": args.dry_run,
                "output_directory": None if args.dry_run else str(output_dir),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())



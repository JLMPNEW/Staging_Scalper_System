#!/usr/bin/env python3
"""Preregister the label-blind Consumer Defensive v2 calibration contract."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from consumer_defensive.core.calibration_preregistration_v2 import (  # noqa: E402
    build_candidate_registry,
    build_preregistration,
    publish_immutable_json,
    read_stage6c_run_metadata,
    verify_factor_campaign,
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
    parser.add_argument("--asof", required=True, help="Canonical Stage 6C as-of date (YYYY-MM-DD)")
    parser.add_argument("--stage6c-run-id", required=True, type=int)
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
    parser.add_argument("--campaign-id", help="Defaults to stage7_scoring.factor_validation_campaign_id")
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
        default=ROOT / "output/consumer_defensive/framework_v2/preregistration",
        help="Base directory; the as-of directory is appended",
    )
    parser.add_argument("--dry-run", action="store_true", help="Validate and build without publishing")
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
    if args.stage6c_run_id <= 0:
        raise ValueError("--stage6c-run-id must be positive")
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
    campaign_id = args.campaign_id or str(
        cfg_get(bundle.payload, "stage7_scoring.factor_validation_campaign_id")
    )
    if not campaign_id.strip():
        raise ValueError("factor-validation campaign id must be nonblank")

    framework = load_framework(framework_path)
    shared_contract = load_shared_service_contract(shared_path)
    shared_hash = shared_service_contract_sha256(shared_contract)
    if shared_hash != framework["ownership"]["shared_service_contract_sha256"]:
        raise ValueError("framework is not bound to the supplied shared-service contract")
    campaign_summary, accepted_cells = verify_factor_campaign(
        factor_root,
        campaign_id=campaign_id,
    )
    with _open_read_only(args.db) as conn:
        stage6c_run = read_stage6c_run_metadata(conn, stage6c_run_id=args.stage6c_run_id)

    registry = build_candidate_registry(
        bundle,
        framework=framework,
        shared_contract=shared_contract,
        asof_date=args.asof,
        stage6c_run=stage6c_run,
        campaign_summary=campaign_summary,
        accepted_factor_cells=accepted_cells,
    )
    preregistration = build_preregistration(
        bundle,
        repository_root=ROOT,
        framework=framework,
        shared_contract=shared_contract,
        stage6c_run=stage6c_run,
        candidate_registry=registry,
    )
    output_dir = (
        args.output_root.expanduser().resolve()
        / args.asof
        / str(preregistration["payload_sha256"])[:16]
    )
    registry_path = output_dir / "consumer_defensive_calibration_candidate_registry_v2.json"
    preregistration_path = output_dir / "consumer_defensive_calibration_preregistration_v2.json"
    if not args.dry_run:
        publish_immutable_json(registry_path, registry)
        publish_immutable_json(preregistration_path, preregistration)

    print(
        json.dumps(
            {
                "schema_version": "consumer_defensive_calibration_preregistration_run_v2",
                "status": "PASS",
                "model_family": "consumer_defensive",
                "asof_date": args.asof,
                "stage6c_run_id": args.stage6c_run_id,
                "stage6c_panel_sha256": stage6c_run["panel_sha256"],
                "framework_sha256": framework_sha256(framework),
                "shared_service_contract_sha256": shared_hash,
                "factor_campaign_id": campaign_summary["campaign_id"],
                "accepted_specialized_factor_cell_count": len(accepted_cells),
                "candidate_count": registry["candidate_count"],
                "candidate_registry_sha256": registry["payload_sha256"],
                "preregistration_sha256": preregistration["payload_sha256"],
                "forward_label_accessed": False,
                "database_write_performed": False,
                "portfolio_write_performed": False,
                "dry_run": args.dry_run,
                "candidate_registry_output": None if args.dry_run else str(registry_path),
                "preregistration_output": None if args.dry_run else str(preregistration_path),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


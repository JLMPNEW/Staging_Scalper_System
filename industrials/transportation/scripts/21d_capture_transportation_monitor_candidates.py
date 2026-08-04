#!/usr/bin/env python3
"""Capture the pre-registered transportation monitor candidates for one date.

Outcome-blind and immutable: reads the day's published rank table plus
already-loaded features, writes one dated candidate snapshot, and refuses to
overwrite an existing capture.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.transportation.monitor_candidates import (  # noqa: E402
    CANDIDATES_CONTRACT_VERSION,
    SCORE_FIELDS,
    SLEEVE_FIELDS,
    asset_light_rows,
    read_rank_table,
    sleeve_rows,
)
from industrials.transportation.selected_feature_history import (  # noqa: E402
    read_only_connection,
    sha256,
    write_manifest,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Capture pre-registered shadow-monitor candidate snapshots "
            "(C1 sleeve membership, C3 asset-light fixed-weight score) for "
            "one completed session date."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--rank-table", type=Path, default=None)
    parser.add_argument("--output-root", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    asof = datetime.strptime(args.asof[:10], "%Y-%m-%d").date().isoformat()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    family = family_config(config, MODEL_FAMILY)
    contract_path = (
        base_dir
        / "transportation"
        / "data"
        / "transportation_monitor_candidates_v1.yaml"
    )
    contract = load_yaml(contract_path)
    if contract.get("contract_version") != CANDIDATES_CONTRACT_VERSION:
        raise ValueError("monitor-candidates contract version mismatch")
    rank_path = (
        args.rank_table.expanduser().resolve()
        if args.rank_table
        else resolve_path(
            family["scoring"]["dashboard_root"], base_dir=base_dir
        )
        / asof
        / "transportation_final_rank_table.csv"
    )
    if not rank_path.is_file():
        raise FileNotFoundError(
            f"{rank_path}: publish the rank table before candidate capture"
        )
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(
            cfg_get(config, "paths.database_path"), base_dir=base_dir
        )
    )
    output_root = (
        args.output_root.expanduser().resolve()
        if args.output_root
        else PROJECT_ROOT
        / "output"
        / "industrials"
        / MODEL_FAMILY
        / "monitoring_candidates"
    )
    output_dir = output_root / asof
    sleeve_path = output_dir / "transportation_monitor_candidate_sleeve.csv"
    score_path = (
        output_dir / "transportation_monitor_candidate_asset_light.csv"
    )
    manifest_path = (
        output_dir / "transportation_monitor_candidates_manifest.json"
    )
    if any(
        path.exists() for path in (sleeve_path, score_path, manifest_path)
    ):
        raise FileExistsError(
            f"{output_dir}: candidate capture already exists for {asof}; "
            "captures are immutable"
        )

    rank_rows = read_rank_table(rank_path)
    if {str(row.get("asof_date") or "") for row in rank_rows} != {asof}:
        raise ValueError("rank table asof does not match requested capture")
    sleeve = sleeve_rows(rank_rows, asof=asof)
    with read_only_connection(db_path) as connection:
        asset_light = asset_light_rows(
            connection, contract=contract, asof=asof
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv_atomic(sleeve_path, SLEEVE_FIELDS, sleeve)
    write_csv_atomic(score_path, SCORE_FIELDS, asset_light)
    scored = sum(1 for row in asset_light if row["status"] == "SCORED")
    payload: dict[str, Any] = {
        "acceptance": "PASS" if sleeve and asset_light else "FAIL",
        "gate": "MONITOR_CANDIDATE_CAPTURE",
        "contract_version": CANDIDATES_CONTRACT_VERSION,
        "model_family": MODEL_FAMILY,
        "asof_date": asof,
        "sleeve_member_count": len(sleeve),
        "asset_light_scored_count": scored,
        "asset_light_universe_count": len(asset_light),
        "outcomes_accessed": False,
        "production_promotion_authorized": False,
        "inputs": {
            "contract": {
                "path": str(contract_path),
                "sha256": sha256(contract_path),
            },
            "rank_table": {
                "path": str(rank_path),
                "sha256": sha256(rank_path),
            },
            "database_path": str(db_path),
        },
        "artifacts": {
            "sleeve": {"path": str(sleeve_path), "sha256": sha256(sleeve_path)},
            "asset_light": {
                "path": str(score_path),
                "sha256": sha256(score_path),
            },
        },
    }
    write_manifest(manifest_path, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["acceptance"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())

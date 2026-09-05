#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.machinery.stage12_contract_upgrade import (  # noqa: E402
    migrate_active_adapter_semantic_seal,
    migrate_active_optimizer_seal,
    upgrade_active_contract,
    upgrade_mapped_fact_idempotency_contract,
    upgrade_financial_lineage_contract,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=("Atomically align and reseal an already validated machinery production activation contract.")
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--governance-dir", type=Path, default=None)
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument(
        "--migrate-adapter-semantic-seal",
        action="store_true",
        help="Replace the legacy whole-adapter hash after exact candidate reproduction.",
    )
    operation.add_argument(
        "--upgrade-financial-lineage-contract",
        action="store_true",
        help=(
            "Reseal a lineage-only output contract after exact score/rank "
            "reproduction and strict effective-date lineage validation."
        ),
    )
    operation.add_argument(
        "--upgrade-mapped-fact-idempotency-contract",
        action="store_true",
        help=(
            "Reseal the exact mapped-fact conflict guard after proving the "
            "active machinery universe and sealed rank are unchanged."
        ),
    )
    operation.add_argument(
        "--migrate-optimizer-seal",
        action="store_true",
        help=("Reseal one explicitly reviewed optimizer source change after exact candidate reproduction."),
    )
    parser.add_argument(
        "--expected-optimizer-sha256",
        default="",
        help="Required reviewed optimizer_core.py SHA-256 for --migrate-optimizer-seal.",
    )
    parser.add_argument(
        "--expected-optimizer-runner-sha256",
        default="",
        help=("Required reviewed 09_run_portfolio_optimizer.py SHA-256 for --migrate-optimizer-seal."),
    )
    parser.add_argument(
        "--expected-optimizer-semantic-sha256",
        default="",
        help=("Required reviewed scoped optimizer semantic SHA-256 for --migrate-optimizer-seal."),
    )
    parser.add_argument(
        "--expected-adapter-semantic-sha256",
        default="",
        help=("Required reviewed industrial adapter semantic SHA-256 for --migrate-optimizer-seal."),
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Optional industrials database override for lineage validation.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    governance_root = (
        args.governance_dir.expanduser().resolve()
        if args.governance_dir
        else resolve_path(
            cfg_get(config, "machinery_stage12.output_root"),
            base_dir=config_path.parent,
        )
    )
    if args.upgrade_mapped_fact_idempotency_contract:
        result = upgrade_mapped_fact_idempotency_contract(
            config,
            config_path=config_path,
            governance_root=governance_root,
            asof=args.asof,
            db_path=args.db,
        )
    elif args.upgrade_financial_lineage_contract:
        result = upgrade_financial_lineage_contract(
            config,
            config_path=config_path,
            governance_root=governance_root,
            asof=args.asof,
            db_path=args.db,
        )
    elif args.migrate_optimizer_seal:
        if (
            not args.expected_optimizer_sha256
            or not args.expected_optimizer_runner_sha256
            or not args.expected_adapter_semantic_sha256
            or not args.expected_optimizer_semantic_sha256
        ):
            raise ValueError(
                "All reviewed portfolio dependency SHA-256 arguments are required with --migrate-optimizer-seal"
            )
        result = migrate_active_optimizer_seal(
            config,
            config_path=config_path,
            governance_root=governance_root,
            asof=args.asof,
            expected_optimizer_sha256=args.expected_optimizer_sha256,
            expected_runner_sha256=args.expected_optimizer_runner_sha256,
            expected_adapter_semantic_sha256=(args.expected_adapter_semantic_sha256),
            expected_optimizer_semantic_sha256=(args.expected_optimizer_semantic_sha256),
        )
    else:
        operation = (
            migrate_active_adapter_semantic_seal if args.migrate_adapter_semantic_seal else upgrade_active_contract
        )
        result = operation(
            config,
            config_path=config_path,
            governance_root=governance_root,
            asof=args.asof,
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
    upgrade_active_contract,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=("Atomically align and reseal an already validated machinery production activation contract.")
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--governance-dir", type=Path, default=None)
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
    result = upgrade_active_contract(
        config,
        config_path=config_path,
        governance_root=governance_root,
        asof=args.asof,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

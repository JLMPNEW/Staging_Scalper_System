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
from industrials.machinery.stage12_activation import (  # noqa: E402
    activate_candidate,
    prepare_activation_candidate,
    validate_activation_candidate,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare and optionally publish a fail-closed machinery "
            "production activation for a new dated dashboard."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--governance-dir", type=Path, default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--approval-token", default="")
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
    if args.publish:
        result = activate_candidate(
            config,
            config_path=config_path,
            governance_root=governance_root,
            asof=args.asof,
            approval_token=args.approval_token,
        )
    else:
        prepared = prepare_activation_candidate(
            config,
            config_path=config_path,
            governance_root=governance_root,
            asof=args.asof,
            force=bool(args.force),
        )
        validation = validate_activation_candidate(
            config,
            config_path=config_path,
            governance_root=governance_root,
            asof=args.asof,
        )
        result = {"candidate": prepared, "validation": validation}
    print(json.dumps(result, indent=2, sort_keys=True))
    acceptance = (
        result.get("acceptance")
        if args.publish
        else result["validation"].get("acceptance")
    )
    return 0 if acceptance == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

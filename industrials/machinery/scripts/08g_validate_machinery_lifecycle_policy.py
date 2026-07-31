#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.machinery.lifecycle_policy import (  # noqa: E402
    load_lifecycle_policy,
    validate_lifecycle_policy,
)
from industrials.machinery.scoring import parse_asof  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate machinery lifecycle ledgers and evidence hashes."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--output-json", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    asof = parse_asof(args.asof)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    output_root = resolve_path(
        cfg_get(
            config,
            "machinery_lifecycle.output_root",
            "../../output/industrials/machinery/lifecycle",
        ),
        base_dir=config_path.parent,
    )
    output_path = (
        args.output_json.expanduser().resolve()
        if args.output_json
        else output_root
        / asof
        / "machinery_lifecycle_policy_validation.json"
    )
    policy = load_lifecycle_policy(config, config_path=config_path)
    result = validate_lifecycle_policy(policy)
    result.update(
        {
            "artifact_family": "machinery_lifecycle_policy_validation",
            "asof_date": asof,
            "validated_at": datetime.now(timezone.utc).isoformat(),
            "production_policy_changed": False,
        }
    )
    write_text_atomic(
        output_path,
        json.dumps(result, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

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

from industrials.core.config import load_yaml  # noqa: E402
from industrials.machinery.operational_amendment_v141 import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_PROTOCOL_PATH,
    assess_operational_amendment,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Assess the versioned machinery v1.4.1 operational amendment."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL_PATH)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--approval-token", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    result = assess_operational_amendment(
        load_yaml(config_path),
        approval_token=args.approval_token,
        protocol_path=args.protocol.expanduser().resolve(),
        output_root=args.output_root.expanduser().resolve(),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("hard_gate_pass") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())

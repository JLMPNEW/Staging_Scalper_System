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
from industrials.machinery.conditional_promotion_v14 import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_PROTOCOL_PATH,
    open_conditional_lockbox,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open the machinery v1.4 conditional lockbox exactly once."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--approval-token", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    result = open_conditional_lockbox(
        load_yaml(config_path),
        config_path=config_path,
        approval_token=args.approval_token,
        protocol_path=args.protocol.expanduser().resolve(),
        output_root=args.output_dir.expanduser().resolve(),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

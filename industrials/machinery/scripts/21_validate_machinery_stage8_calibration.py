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

from industrials.core.config import (  # noqa: E402
    cfg_get,
    load_yaml,
    resolve_path,
)
from industrials.machinery.stage8_calibration import (  # noqa: E402
    validate_stage8,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate machinery Stage 8 research artifacts."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--require-stage9-ready", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    output_root = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(
            cfg_get(config, "machinery_stage8.output_root"),
            base_dir=config_path.parent,
        )
    )
    result = validate_stage8(
        config,
        output_root=output_root,
        require_stage9_ready=bool(args.require_stage9_ready),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

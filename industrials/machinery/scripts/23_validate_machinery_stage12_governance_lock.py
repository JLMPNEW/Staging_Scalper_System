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
from industrials.machinery.stage12_governance import (  # noqa: E402
    validate_stage12_lock,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate the machinery Stage 12 governance candidate."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    output_root = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(
            cfg_get(config, "machinery_stage12.output_root"),
            base_dir=config_path.parent,
        )
    )
    result = validate_stage12_lock(output_root=output_root)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

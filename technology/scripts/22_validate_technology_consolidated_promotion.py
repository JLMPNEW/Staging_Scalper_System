"""Validate sealed technology consolidated-promotion artifacts."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.consolidated_promotion import validate_consolidated_outputs  # noqa: E402


DEFAULT_CONFIG = PROJECT_ROOT / "technology" / "config.yaml"
DEFAULT_POLICY = PROJECT_ROOT / "technology" / "data" / "technology_consolidated_promotion_policy_v1.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the technology consolidated promotion run.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args()
    errors = validate_consolidated_outputs(
        policy_path=args.policy.expanduser().resolve(),
        technology_config_path=args.config.expanduser().resolve(),
        output_dir=args.output_dir.expanduser().resolve() if args.output_dir else None,
    )
    if errors:
        for error in errors:
            logging.error("FAIL: %s", error)
        return 1
    logging.info("PASS: consolidated technology promotion artifacts are current, sealed, and internally consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

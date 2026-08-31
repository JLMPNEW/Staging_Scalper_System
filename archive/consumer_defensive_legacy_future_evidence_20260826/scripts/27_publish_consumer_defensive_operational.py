"""Publish and independently validate the Stage 12 Portfolio Layer snapshot."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from consumer_defensive.core.stage12_operational import (  # noqa: E402
    load_activation_registry,
    publish_operational_snapshot,
    validate_operational_snapshot,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--stage10-output-dir", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=ROOT / "output" / "consumer_defensive" / "dashboard")
    parser.add_argument("--activation-registry", type=Path)
    parser.add_argument("--activation-registry-sha256", default="")
    parser.add_argument("--change-control-public-key", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    registry = None
    if args.activation_registry is not None or args.change_control_public_key is not None:
        if args.activation_registry is None or args.change_control_public_key is None:
            raise ValueError("activation registry and change-control public key must be supplied together")
        registry = load_activation_registry(
            args.activation_registry,
            expected_sha256=args.activation_registry_sha256,
            public_key_path=args.change_control_public_key,
        )
    manifest = publish_operational_snapshot(
        stage10_dir=args.stage10_output_dir,
        output_root=args.output_root,
        asof_date=args.as_of,
        activation_registry=registry,
        activation_registry_sha256=args.activation_registry_sha256,
    )
    validate_operational_snapshot(args.output_root / args.as_of)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

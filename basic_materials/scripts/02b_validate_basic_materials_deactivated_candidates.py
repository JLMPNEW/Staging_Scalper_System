"""Validate the fail-closed Basic Materials deactivated-company review queue."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from basic_materials.core.historical_candidates import (  # noqa: E402
    load_historical_candidate_policy,
    read_and_validate_historical_candidates,
    summarize_historical_candidates,
    validate_historical_candidate_manifest,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = PACKAGE_ROOT / "data" / "basic_materials_historical_candidate_policy.yaml"
DEFAULT_MANIFEST = PACKAGE_ROOT / "data" / "basic_materials_historical_candidate_manifest.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--csv", type=Path, help="Override the policy-owned candidate CSV")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        policy = load_historical_candidate_policy(args.policy)
        candidate_path = args.csv or PACKAGE_ROOT / policy.candidate_file
        manifest = validate_historical_candidate_manifest(args.manifest, candidate_path)
        candidates = read_and_validate_historical_candidates(candidate_path, policy)
        summary = summarize_historical_candidates(candidates, policy)
        print(json.dumps({**summary.as_dict(), "manifest": manifest}, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"passed": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

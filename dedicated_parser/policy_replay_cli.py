from __future__ import annotations

import argparse
import json
from contextlib import closing
from pathlib import Path

from dedicated_parser.adapters import load_registry
from dedicated_parser.review_replay import replay_review_policies
from dedicated_parser.storage import connect_database


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Apply a review policy to immutable evidence from an existing "
            "dedicated-parser run without opening source documents or invoking "
            "Arelle, EdgarTools, or OCR."
        )
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--policy-replay-run-id", type=int, required=True)
    parser.add_argument("--review-policy", type=Path, default=None)
    parser.add_argument("--output-json", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    registry = load_registry(args.adapter)
    policy_path = args.review_policy
    if policy_path is None:
        if not registry.review_policy_path:
            raise ValueError("Adapter registry has no review policy; pass --review-policy")
        policy_path = Path(registry.review_policy_path)
    with closing(connect_database(args.db)) as conn:
        summary = replay_review_policies(
            conn,
            base_run_id=args.policy_replay_run_id,
            adapter_path=args.adapter,
            policy_path=policy_path,
            expected_model_family=registry.model_family,
        )
    payload = {"mode": "policy_replay", **summary.as_dict()}
    if args.output_json is not None:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

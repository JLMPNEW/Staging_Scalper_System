"""Build a zero-cap manual activation candidate after independent review."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from .activation_candidate import build_activation_candidate
from .protocol import immutable_write_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--family",
        required=True,
        choices=("transportation",),
    )
    parser.add_argument("--scope-id", required=True)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--evaluation-sha256", required=True)
    parser.add_argument("--review-receipt", type=Path, required=True)
    parser.add_argument("--review-receipt-sha256", required=True)
    parser.add_argument("--review-public-key", type=Path, required=True)
    parser.add_argument(
        "--generated-at-utc",
        default=datetime.now(timezone.utc).isoformat(),
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = build_activation_candidate(
        family=args.family,
        scope_id=args.scope_id,
        evaluation_path=args.evaluation,
        expected_evaluation_sha256=args.evaluation_sha256,
        review_receipt_path=args.review_receipt,
        expected_review_receipt_sha256=args.review_receipt_sha256,
        review_public_key_path=args.review_public_key,
        generated_at_utc=args.generated_at_utc,
    )
    immutable_write_json(args.output, payload)
    print(
        json.dumps(
            {"output": str(args.output.resolve()), **payload},
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

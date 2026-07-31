from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.golden import validate_corpus  # noqa: E402
from dedicated_parser.benchmark import load_cohort_tickers  # noqa: E402
from dedicated_parser.storage import connect_database  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate one parser shadow run against a reviewed corpus."
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument(
        "--corpus",
        type=Path,
        action="append",
        required=True,
        help="Golden corpus path. Repeat to validate manual and generated corpora.",
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--run-id", type=int)
    selection.add_argument("--evaluation-id", type=int)
    parser.add_argument("--ticker-cohort", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    tickers = (
        set(load_cohort_tickers(args.ticker_cohort))
        if args.ticker_cohort is not None
        else None
    )
    with connect_database(args.db, readonly=True) as conn:
        errors = [
            error
            for corpus_path in args.corpus
            for error in validate_corpus(
                conn,
                corpus_path=corpus_path,
                table=(
                    "sec_parser_review_evidence"
                    if args.evaluation_id is not None
                    else "sec_parser_metric_evidence_shadow"
                ),
                run_id=args.run_id,
                evaluation_id=args.evaluation_id,
                tickers=tickers,
            )
        ]
    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    print(
        "PASS: "
        + (
            f"evaluation_id={args.evaluation_id} "
            if args.evaluation_id is not None
            else f"run_id={args.run_id} "
        )
        + f"corpora={','.join(str(path) for path in args.corpus)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

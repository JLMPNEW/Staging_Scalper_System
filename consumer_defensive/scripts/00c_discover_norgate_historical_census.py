#!/usr/bin/env python3
"""Build a read-only review queue from Norgate Current & Past watchlists.

This does not mutate the Consumer Defensive database and does not establish a
point-in-time, survivorship-complete universe.  Norgate's GICS endpoint has
no as-of-date parameter, so sector matching is only a way to prioritize
manual review of the historical index-constituent union.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from consumer_defensive.core.atomic_io import (  # noqa: E402
    atomic_text_writer,
    atomic_write_text,
)
from consumer_defensive.core.norgate_census import (  # noqa: E402
    discover_candidate_census,
)
from consumer_defensive.core.universe import load_policy  # noqa: E402


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = PACKAGE_ROOT / "data" / "consumer_defensive_universe_policy.yaml"
DEFAULT_OUTPUT = REPO_ROOT / "output" / "consumer_defensive" / "preflight" / "norgate_candidate_census"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--max-symbols",
        type=int,
        default=None,
        help="Inspect only the first N sorted symbols; marks the report incomplete.",
    )
    return parser.parse_args()


def write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    materialized = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not materialized:
        atomic_write_text(path, "", encoding="utf-8")
        return
    columns = list(materialized[0])
    with atomic_text_writer(path, encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(materialized)


def require_new_output(output_dir: Path, names: tuple[str, ...]) -> None:
    existing = [str(output_dir / name) for name in names if (output_dir / name).exists()]
    if existing:
        raise FileExistsError(
            "Historical census artifacts are immutable; choose a new "
            f"--output-dir: {existing}"
        )


def main() -> int:
    args = parse_args()
    require_new_output(
        args.output_dir,
        ("norgate_historical_candidate_census.csv", "summary.json"),
    )
    try:
        import norgatedata  # type: ignore
    except ImportError as exc:
        raise SystemExit("norgatedata is unavailable in this Python environment.") from exc
    policy = load_policy(args.policy)
    rows, summary = discover_candidate_census(
        norgatedata,
        policy,
        max_symbols=args.max_symbols,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.output_dir / "norgate_historical_candidate_census.csv", rows)
    atomic_write_text(
        args.output_dir / "summary.json",
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output_dir": str(args.output_dir), **summary}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

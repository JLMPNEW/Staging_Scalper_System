#!/usr/bin/env python3
"""Add PIT index-membership overlap to a historical candidate review queue.

This script is read-only with respect to all databases. Its output still uses
current/final GICS taxonomy and therefore cannot authorize calibration,
production, or a survivorship-correctness claim.
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
from consumer_defensive.core.norgate_pit_census import (  # noqa: E402
    enrich_candidate_pit_membership,
)
from consumer_defensive.core.universe import load_policy  # noqa: E402


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = PACKAGE_ROOT / "data" / "consumer_defensive_universe_policy.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-csv", type=Path, required=True)
    parser.add_argument("--membership-start", required=True)
    parser.add_argument("--membership-end", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


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
            "PIT membership artifacts are immutable; choose a new "
            f"--output-dir: {existing}"
        )


def main() -> int:
    args = parse_args()
    require_new_output(
        args.output_dir,
        ("norgate_candidate_pit_membership_review.csv", "summary.json"),
    )
    try:
        import norgatedata  # type: ignore
    except ImportError as exc:
        raise SystemExit(
            "norgatedata is unavailable in this Python environment."
        ) from exc
    rows = read_csv(args.candidate_csv)
    policy = load_policy(args.policy)
    enriched, summary = enrich_candidate_pit_membership(
        norgatedata,
        policy,
        rows,
        start_date=args.membership_start,
        end_date=args.membership_end,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(
        args.output_dir / "norgate_candidate_pit_membership_review.csv",
        enriched,
    )
    atomic_write_text(
        args.output_dir / "summary.json",
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"output_dir": str(args.output_dir), **summary},
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

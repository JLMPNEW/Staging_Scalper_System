#!/usr/bin/env python3
"""Freeze conflict-free semantic replay inputs before canonical materialization."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.contracts import file_sha256  # noqa: E402
from industrials.transportation.semantic_replay_contract import (  # noqa: E402
    resolve_semantic_replay_rows,
)


DEFAULT_SURFACE_MANIFEST = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "investable_v3"
    / "surface_delta"
    / "2026-07-30"
    / "transportation_surface_semantic_replay.json"
)
DEFAULT_TANKER_MANIFEST = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "investable_v3"
    / "tanker_delta"
    / "2026-08-13"
    / "transportation_tanker_semantic_replay.json"
)
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "investable_v5"
    / "semantic_materialization"
)
CONFLICT_EXTRA_FIELDS = (
    "conflict_reason",
    "observation_candidate_count",
    "observation_distinct_value_count",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit reviewed surface and tanker semantic replays for ambiguous "
            "same-filing observations without fetching or reparsing."
        )
    )
    parser.add_argument("--asof", required=True)
    parser.add_argument("--surface-manifest", type=Path, default=DEFAULT_SURFACE_MANIFEST)
    parser.add_argument("--tanker-manifest", type=Path, default=DEFAULT_TANKER_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def _csv(path: Path) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = tuple(reader.fieldnames or ())
        if not fields:
            raise ValueError(f"semantic replay has no header: {path}")
        return fields, [dict(row) for row in reader]


def _resolve_lane(
    *,
    lane: str,
    manifest_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("acceptance") != "PASS":
        raise ValueError(f"{lane} semantic replay manifest is not PASS")
    accepted_path = Path(str(manifest.get("accepted_csv") or "")).resolve()
    if not accepted_path.is_file():
        raise FileNotFoundError(accepted_path)
    if file_sha256(accepted_path) != str(manifest.get("accepted_csv_sha256") or ""):
        raise ValueError(f"{lane} accepted semantic replay hash mismatch")
    fields, rows = _csv(accepted_path)
    resolution = resolve_semantic_replay_rows(rows)
    clean_path = output_dir / f"transportation_{lane}_semantic_conflict_free.csv"
    conflict_path = output_dir / f"transportation_{lane}_semantic_conflicts.csv"
    write_csv_atomic(clean_path, fields, resolution.conflict_free_rows)
    write_csv_atomic(
        conflict_path,
        (*fields, *CONFLICT_EXTRA_FIELDS),
        resolution.conflict_rows,
    )
    return {
        "source_manifest_path": str(manifest_path),
        "source_manifest_sha256": file_sha256(manifest_path),
        "source_accepted_csv": str(accepted_path),
        "source_accepted_csv_sha256": file_sha256(accepted_path),
        "accepted_input_count": resolution.accepted_input_count,
        "observation_group_count": resolution.observation_group_count,
        "conflict_free_observation_count": len(resolution.conflict_free_rows),
        "conflict_group_count": resolution.conflict_group_count,
        "conflicted_candidate_count": len(resolution.conflict_rows),
        "conflict_free_csv": str(clean_path),
        "conflict_free_csv_sha256": file_sha256(clean_path),
        "conflict_csv": str(conflict_path),
        "conflict_csv_sha256": file_sha256(conflict_path),
    }


def main() -> int:
    args = parse_args()
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else DEFAULT_OUTPUT_ROOT / str(args.asof)[:10]
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    lanes = {
        "surface": _resolve_lane(
            lane="surface",
            manifest_path=args.surface_manifest.expanduser().resolve(),
            output_dir=output_dir,
        ),
        "tanker": _resolve_lane(
            lane="tanker",
            manifest_path=args.tanker_manifest.expanduser().resolve(),
            output_dir=output_dir,
        ),
    }
    result = {
        "acceptance": "PASS",
        "asof_date": str(args.asof)[:10],
        "contract_version": "transportation_semantic_materialization_v1",
        "lanes": lanes,
        "network_requests": 0,
        "parser_invocations": 0,
        "canonical_candidate_mutation": False,
        "historical_reconstruction_authorized": False,
        "calibration_authorized": False,
        "production_promotion_authorized": False,
        "next_gate": "RERUN_STRICT_SURFACE_AND_TANKER_COVERAGE",
    }
    manifest_path = output_dir / "transportation_semantic_materialization_audit.json"
    write_text_atomic(manifest_path, json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.machinery.lifecycle_policy import (  # noqa: E402
    ACCEPTED,
    HARD_EVENT_FIELDS,
    HARD_EVENT_HASH_FIELDS,
    POLICY_VERSION,
    REJECTED,
    REVENUE_HASH_FIELDS,
    REVENUE_POLICY_FIELDS,
    REVENUE_REVIEW_REQUIRED,
    TRANSITION_FIELDS,
    TRANSITION_HASH_FIELDS,
    VALIDATED_CUSTOMER_REVENUE,
    VALIDATED_NONCOMMERCIAL_REVENUE,
    file_sha256,
    load_lifecycle_policy,
    record_sha256,
    validate_lifecycle_policy,
)
from industrials.machinery.scoring import parse_asof  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
FINAL_STATUSES = frozenset({ACCEPTED, REJECTED})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate reviewed machinery lifecycle decisions and append them "
            "to the hash-guarded policy ledgers. The default is a dry run."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--transition-review", type=Path, default=None)
    parser.add_argument("--revenue-review", type=Path, default=None)
    parser.add_argument("--hard-event-review", type=Path, default=None)
    parser.add_argument("--output-json", type=Path, default=None)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Append validated decisions to the policy ledgers.",
    )
    return parser.parse_args()


def _read_rows(
    path: Path,
    *,
    fields: Sequence[str],
) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        actual = tuple(reader.fieldnames or ())
        if actual != tuple(fields):
            raise ValueError(
                f"{path}: expected fields={list(fields)} found={list(actual)}"
            )
        return [
            {field: str(row.get(field) or "").strip() for field in fields}
            for row in reader
            if any(str(row.get(field) or "").strip() for field in fields)
        ]


def _require_final_review(
    row: Mapping[str, str],
    *,
    row_id: str,
) -> None:
    status = str(row.get("decision_status") or "").strip().upper()
    if status not in FINAL_STATUSES:
        raise ValueError(
            f"{row_id}: decision_status must be ACCEPTED or REJECTED"
        )
    for field in (
        "ticker",
        "valid_from",
        "evidence_artifact",
        "evidence_sha256",
        "decision_reason",
        "reviewer",
        "reviewed_at",
    ):
        if not str(row.get(field) or "").strip():
            raise ValueError(f"{row_id}: missing {field}")
    if str(row.get("policy_version") or "").strip() != POLICY_VERSION:
        raise ValueError(f"{row_id}: invalid policy_version")
    evidence_path = Path(str(row["evidence_artifact"])).expanduser()
    if not evidence_path.is_file():
        raise ValueError(f"{row_id}: evidence artifact does not exist")
    if file_sha256(evidence_path) != str(row["evidence_sha256"]).strip():
        raise ValueError(f"{row_id}: evidence_sha256 mismatch")


def _finalize_rows(
    rows: Sequence[Mapping[str, str]],
    *,
    fields: Sequence[str],
    hash_fields: Sequence[str],
    key_fields: Sequence[str],
    existing: Sequence[Mapping[str, str]],
    kind: str,
) -> list[dict[str, str]]:
    existing_keys = {
        tuple(str(row.get(field) or "").strip() for field in key_fields)
        for row in existing
    }
    new_keys: set[tuple[str, ...]] = set()
    finalized: list[dict[str, str]] = []
    for index, source in enumerate(rows, start=1):
        row = {
            field: str(source.get(field) or "").strip() for field in fields
        }
        row["ticker"] = row["ticker"].upper()
        row["decision_status"] = row["decision_status"].upper()
        row_id = ":".join(row.get(field, "") for field in key_fields)
        row_id = row_id or f"{kind}:{index}"
        _require_final_review(row, row_id=row_id)
        key = tuple(row[field] for field in key_fields)
        if key in existing_keys:
            raise ValueError(f"{row_id}: decision already exists in ledger")
        if key in new_keys:
            raise ValueError(f"{row_id}: duplicate decision in review file")
        new_keys.add(key)
        row["record_sha256"] = record_sha256(row, fields=hash_fields)
        finalized.append(row)
    return finalized


def _review_paths(args: argparse.Namespace) -> list[Path]:
    return [
        path.expanduser().resolve()
        for path in (
            args.transition_review,
            args.revenue_review,
            args.hard_event_review,
        )
        if path is not None
    ]


def main() -> int:
    args = parse_args()
    asof = parse_asof(args.asof)
    supplied_paths = _review_paths(args)
    if not supplied_paths:
        raise ValueError("At least one reviewed CSV must be supplied")

    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    policy = load_lifecycle_policy(config, config_path=config_path)
    initial_validation = validate_lifecycle_policy(policy)
    if initial_validation["acceptance"] != "PASS":
        raise ValueError(
            "Existing lifecycle policy is invalid: "
            + ";".join(initial_validation["issues"])
        )

    transition_rows = (
        _read_rows(
            args.transition_review.expanduser().resolve(),
            fields=TRANSITION_FIELDS,
        )
        if args.transition_review
        else []
    )
    revenue_rows = (
        _read_rows(
            args.revenue_review.expanduser().resolve(),
            fields=REVENUE_POLICY_FIELDS,
        )
        if args.revenue_review
        else []
    )
    hard_event_rows = (
        _read_rows(
            args.hard_event_review.expanduser().resolve(),
            fields=HARD_EVENT_FIELDS,
        )
        if args.hard_event_review
        else []
    )
    for index, row in enumerate(revenue_rows, start=1):
        if row["decision_status"].strip().upper() != ACCEPTED:
            raise ValueError(
                f"revenue:{index}: revenue classifications must be ACCEPTED"
            )
        if row["revenue_classification"] not in {
            VALIDATED_CUSTOMER_REVENUE,
            VALIDATED_NONCOMMERCIAL_REVENUE,
        }:
            raise ValueError(
                f"revenue:{index}: replace {REVENUE_REVIEW_REQUIRED!r} with "
                "a final revenue classification"
            )

    new_transitions = _finalize_rows(
        transition_rows,
        fields=TRANSITION_FIELDS,
        hash_fields=TRANSITION_HASH_FIELDS,
        key_fields=("transition_id",),
        existing=policy.transitions,
        kind="transition",
    )
    new_revenue = _finalize_rows(
        revenue_rows,
        fields=REVENUE_POLICY_FIELDS,
        hash_fields=REVENUE_HASH_FIELDS,
        key_fields=("ticker", "valid_from"),
        existing=policy.revenue_decisions,
        kind="revenue",
    )
    new_events = _finalize_rows(
        hard_event_rows,
        fields=HARD_EVENT_FIELDS,
        hash_fields=HARD_EVENT_HASH_FIELDS,
        key_fields=("event_id",),
        existing=policy.hard_events,
        kind="hard_event",
    )
    proposed = replace(
        policy,
        transitions=(*policy.transitions, *new_transitions),
        revenue_decisions=(*policy.revenue_decisions, *new_revenue),
        hard_events=(*policy.hard_events, *new_events),
    )
    proposed_validation = validate_lifecycle_policy(proposed)
    if proposed_validation["acceptance"] != "PASS":
        raise ValueError(
            "Reviewed decisions would produce an invalid lifecycle policy: "
            + ";".join(proposed_validation["issues"])
        )

    if args.apply:
        if new_transitions:
            write_csv_atomic(
                policy.transitions_path,
                TRANSITION_FIELDS,
                proposed.transitions,
            )
        if new_revenue:
            write_csv_atomic(
                policy.revenue_policy_path,
                REVENUE_POLICY_FIELDS,
                proposed.revenue_decisions,
            )
        if new_events:
            write_csv_atomic(
                policy.hard_events_path,
                HARD_EVENT_FIELDS,
                proposed.hard_events,
            )
        reloaded = load_lifecycle_policy(config, config_path=config_path)
        applied_validation = validate_lifecycle_policy(reloaded)
        if applied_validation["acceptance"] != "PASS":
            raise RuntimeError(
                "Applied lifecycle policy failed post-write validation: "
                + ";".join(applied_validation["issues"])
            )
    else:
        applied_validation = proposed_validation

    output_root = resolve_path(
        cfg_get(
            config,
            "machinery_lifecycle.output_root",
            "../../output/industrials/machinery/lifecycle",
        ),
        base_dir=config_path.parent,
    )
    output_path = (
        args.output_json.expanduser().resolve()
        if args.output_json
        else output_root
        / asof
        / "machinery_lifecycle_ratification.json"
    )
    summary = {
        "acceptance": "PASS",
        "artifact_family": "machinery_lifecycle_ratification",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "asof_date": asof,
        "mode": "APPLY" if args.apply else "DRY_RUN",
        "input_review_files": [str(path) for path in supplied_paths],
        "transition_decisions_added": len(new_transitions),
        "revenue_decisions_added": len(new_revenue),
        "hard_event_decisions_added": len(new_events),
        "policy_validation": applied_validation,
        "production_policy_changed": False,
    }
    write_text_atomic(
        output_path,
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

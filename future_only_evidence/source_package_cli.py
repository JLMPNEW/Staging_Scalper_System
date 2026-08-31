"""Build unsigned, create-only inputs for the prospective evidence protocol."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .source_packages import (
    build_lifecycle_event_snapshot,
    build_transport_replay_inputs,
    build_transport_score_replay_baseline,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    lifecycle = subparsers.add_parser(
        "lifecycle-request",
        help=(
            "build an unsigned lifecycle snapshot and external market-authority "
            "signing request"
        ),
    )
    lifecycle.add_argument(
        "--family",
        required=True,
        choices=("transportation",),
    )
    lifecycle.add_argument("--policy-id", required=True)
    lifecycle.add_argument("--raw-lifecycle-csv", type=Path, required=True)
    lifecycle.add_argument("--expected-ticker", action="append", required=True)
    lifecycle.add_argument("--asof", required=True)
    lifecycle.add_argument("--signal-cutoff-at-utc", required=True)
    lifecycle.add_argument("--snapshot-generated-at-utc", required=True)
    lifecycle.add_argument("--query-sha256", required=True)
    lifecycle.add_argument("--output", type=Path, required=True)
    lifecycle.add_argument("--signing-request-output", type=Path, required=True)

    baseline = subparsers.add_parser(
        "transport-baseline",
        help=(
            "build a structurally validated unsigned Transportation baseline "
            "and availability signing request"
        ),
    )
    baseline.add_argument("--baseline-cutoff-at-utc", required=True)
    baseline.add_argument("--activation-registered-at-utc", required=True)
    baseline.add_argument("--raw-panel", type=Path, required=True)
    baseline.add_argument("--raw-accepted-facts", type=Path, required=True)
    baseline.add_argument("--staleness-policy", type=Path, required=True)
    baseline.add_argument("--v8-policy", type=Path, required=True)
    baseline.add_argument(
        "--raw-source-availability-csv", type=Path, required=True
    )
    baseline.add_argument("--snapshot-generated-at-utc", required=True)
    baseline.add_argument("--policy-id", required=True)
    baseline.add_argument("--query-sha256", required=True)
    baseline.add_argument("--output", type=Path, required=True)
    baseline.add_argument("--availability-output", type=Path, required=True)
    baseline.add_argument(
        "--availability-signing-request-output", type=Path, required=True
    )

    replay = subparsers.add_parser(
        "transport-replay-inputs",
        help=(
            "build structurally validated scheduled Transportation inputs and "
            "an unsigned availability signing request"
        ),
    )
    replay.add_argument("--asof", required=True)
    replay.add_argument("--signal-cutoff-at-utc", required=True)
    replay.add_argument("--scheduled-asof", action="append", required=True)
    replay.add_argument("--raw-panel", type=Path, required=True)
    replay.add_argument("--raw-accepted-facts", type=Path, required=True)
    replay.add_argument("--staleness-policy", type=Path, required=True)
    replay.add_argument("--canonical-score", type=Path, required=True)
    replay.add_argument("--score-replay-baseline", type=Path, required=True)
    replay.add_argument("--v8-policy", type=Path, required=True)
    replay.add_argument(
        "--raw-source-availability-csv", type=Path, required=True
    )
    replay.add_argument("--snapshot-generated-at-utc", required=True)
    replay.add_argument("--policy-id", required=True)
    replay.add_argument("--query-sha256", required=True)
    replay.add_argument("--predecessor-replay-audit", type=Path)
    replay.add_argument("--panel-output", type=Path, required=True)
    replay.add_argument("--accepted-facts-output", type=Path, required=True)
    replay.add_argument("--availability-output", type=Path, required=True)
    replay.add_argument(
        "--availability-signing-request-output", type=Path, required=True
    )

    args = parser.parse_args()
    if args.command == "lifecycle-request":
        audit = build_lifecycle_event_snapshot(
            family=args.family,
            policy_id=args.policy_id,
            raw_lifecycle_csv_path=args.raw_lifecycle_csv,
            expected_tickers=args.expected_ticker,
            asof_date=args.asof,
            signal_cutoff_at_utc=args.signal_cutoff_at_utc,
            snapshot_generated_at_utc=args.snapshot_generated_at_utc,
            query_sha256=args.query_sha256,
            output_path=args.output,
            signing_request_output_path=args.signing_request_output,
        )
    elif args.command == "transport-baseline":
        audit = build_transport_score_replay_baseline(
            baseline_cutoff_at_utc=args.baseline_cutoff_at_utc,
            activation_registered_at_utc=args.activation_registered_at_utc,
            raw_panel_path=args.raw_panel,
            raw_accepted_facts_path=args.raw_accepted_facts,
            staleness_path=args.staleness_policy,
            v8_policy_path=args.v8_policy,
            raw_source_availability_csv_path=(
                args.raw_source_availability_csv
            ),
            snapshot_generated_at_utc=args.snapshot_generated_at_utc,
            policy_id=args.policy_id,
            query_sha256=args.query_sha256,
            output_path=args.output,
            availability_output_path=args.availability_output,
            availability_signing_request_output_path=(
                args.availability_signing_request_output
            ),
        )
    elif args.command == "transport-replay-inputs":
        predecessor = None
        if args.predecessor_replay_audit is not None:
            predecessor = json.loads(
                args.predecessor_replay_audit.read_text(encoding="utf-8")
            )
            if not isinstance(predecessor, dict):
                raise ValueError("predecessor replay audit must be a JSON object")
        audit = build_transport_replay_inputs(
            asof_date=args.asof,
            raw_panel_path=args.raw_panel,
            raw_accepted_facts_path=args.raw_accepted_facts,
            staleness_path=args.staleness_policy,
            canonical_score_path=args.canonical_score,
            score_replay_baseline_path=args.score_replay_baseline,
            v8_policy_path=args.v8_policy,
            signal_cutoff_at_utc=args.signal_cutoff_at_utc,
            scheduled_append_asof_dates=args.scheduled_asof,
            raw_source_availability_csv_path=(
                args.raw_source_availability_csv
            ),
            snapshot_generated_at_utc=args.snapshot_generated_at_utc,
            policy_id=args.policy_id,
            query_sha256=args.query_sha256,
            panel_output_path=args.panel_output,
            accepted_facts_output_path=args.accepted_facts_output,
            availability_output_path=args.availability_output,
            availability_signing_request_output_path=(
                args.availability_signing_request_output
            ),
            predecessor_replay_audit=predecessor,
        )
    else:
        parser.error("unsupported source-package command")
    print(json.dumps(audit, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

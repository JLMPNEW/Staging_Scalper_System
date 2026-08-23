#!/usr/bin/env python3
# ruff: noqa: E402
"""Replay reviewed Stage 6B evidence and rebuild measurement-only coverage."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from datetime import date
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from consumer_defensive.core.config import cfg_get, load_config, resolve_path
from consumer_defensive.core.script_runtime import iso_date
from consumer_defensive.core.stage3_runtime import database_path
from dedicated_parser.adapters import load_registry
from dedicated_parser.atomic_io import atomic_write_text
from dedicated_parser.review_replay import (
    materialize_review_evaluation_run,
    replay_review_policies,
)
from dedicated_parser.storage import connect_database


DEFAULT_CONFIG = PACKAGE_ROOT / 'config.yaml'


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--db', type=Path, default=None)
    parser.add_argument('--as-of', type=iso_date, default=date.today().isoformat())
    parser.add_argument('--base-parser-run-id', type=int, required=True)
    parser.add_argument('--review-policy', type=Path, required=True)
    parser.add_argument('--source-manifest', type=Path, default=None)
    parser.add_argument('--cache-dir', type=Path, default=None)
    parser.add_argument('--output-dir', type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    bundle = load_config(args.config)
    db_path = database_path(bundle, args.db)
    adapter_path = str(cfg_get(bundle.payload, 'stage6b.adapter_path'))
    registry = load_registry(adapter_path)
    policy_path = args.review_policy.expanduser().resolve(strict=True)
    with connect_database(db_path) as conn:
        evaluation = replay_review_policies(
            conn,
            base_run_id=args.base_parser_run_id,
            adapter_path=adapter_path,
            policy_path=policy_path,
            expected_model_family='consumer_defensive',
        )
        reviewed_run_id = materialize_review_evaluation_run(
            conn, evaluation_id=evaluation.evaluation_id
        )
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(
            cfg_get(bundle.payload, 'paths.output_dir'),
            base_dir=bundle.base_dir,
        ) / 'stage6b' / args.as_of / 'reviewed'
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    replay_payload = {
        'mode': 'consumer_defensive_review_replay',
        **evaluation.as_dict(),
        'materialized_parser_run_id': reviewed_run_id,
        'adapter_version': registry.adapter_version,
        'review_policy': str(policy_path),
    }
    atomic_write_text(
        output_dir / 'consumer_defensive_review_replay.json',
        json.dumps(replay_payload, indent=2, sort_keys=True) + '\n',
    )
    runner_argv = [
        '--config', str(args.config),
        '--db', str(db_path),
        '--as-of', args.as_of,
        '--output-dir', str(output_dir),
        '--parser-run-id', str(reviewed_run_id),
    ]
    if args.source_manifest is not None:
        runner_argv.extend([
            '--source-manifest',
            str(args.source_manifest.expanduser().resolve(strict=True)),
        ])
    if args.cache_dir is not None:
        runner_argv.extend([
            '--cache-dir', str(args.cache_dir.expanduser().resolve()),
        ])
    runner_main = importlib.import_module(
        'consumer_defensive.scripts.'
        '13b_build_consumer_defensive_specialized_metrics'
    ).main
    return int(runner_main(runner_argv))


if __name__ == '__main__':
    raise SystemExit(main())

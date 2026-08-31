#!/usr/bin/env python3
"""Refresh Consumer inputs and publish authority-pinned production scores."""

from __future__ import annotations

# Direct execution bootstraps the repository root before package imports.
# ruff: noqa: E402

import argparse
import json
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from consumer_defensive.core.production_refresh_v3 import (
    DEFAULT_CONFIG,
    build_refresh_plan,
    run_refresh,
)
from consumer_defensive.core.script_runtime import iso_date


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--asof",
        required=True,
        type=iso_date,
        help="Allocation XNYS session (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--signal-asof-date",
        type=iso_date,
        help="Optional explicit prior XNYS signal session; independently verified.",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--output-root", type=Path)
    refresh_mode = parser.add_mutually_exclusive_group()
    refresh_mode.add_argument(
        "--cache-only",
        action="store_true",
        help="Forbid network access in Yahoo, SEC, and FX refresh stages.",
    )
    refresh_mode.add_argument(
        "--force-refresh",
        action="store_true",
        help="Explicitly bypass valid upstream caches for the network stages.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the exact argv plan; execute and write nothing.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume only from a source-sealed, log-verified PASS step prefix.",
    )
    parser.add_argument("--step-timeout-seconds", type=float, default=3600.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    plan = build_refresh_plan(
        allocation_asof_date=args.asof,
        signal_asof_date=args.signal_asof_date,
        config_path=args.config,
        database=args.db,
        output_root=args.output_root,
        cache_only=args.cache_only,
        force_refresh=args.force_refresh,
    )
    result = run_refresh(
        plan,
        dry_run=args.dry_run,
        resume=args.resume,
        timeout_seconds=args.step_timeout_seconds,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") in {"PASS", "PLAN_ONLY"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

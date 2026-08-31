#!/usr/bin/env python3
"""Fail-closed, idempotent Stage-2 readiness preflight for production refreshes."""

from __future__ import annotations

# Direct execution bootstraps the repository root before package imports.
# ruff: noqa: E402

import argparse
import json
import subprocess
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from consumer_defensive.core.config import cfg_get, load_config, resolve_path
from consumer_defensive.core.db import connect, init_db
from consumer_defensive.core.script_runtime import iso_date
from consumer_defensive.core.stage3_runtime import assert_stage2_ready


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--as-of", required=True, type=iso_date)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def _readiness(config: Path, database: Path, *, as_of: str) -> dict[str, object]:
    bundle = load_config(config)
    timeout = float(cfg_get(bundle.payload, "runtime.sqlite_timeout_sec", 30.0))
    with connect(database, timeout_sec=timeout) as conn:
        init_db(conn)
        return dict(assert_stage2_ready(conn, bundle, as_of=as_of))


def _run(script: str, *arguments: str) -> None:
    command = [sys.executable, str(PACKAGE_ROOT / "scripts" / script), *arguments]
    completed = subprocess.run(command, cwd=REPO_ROOT, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            f"Consumer Stage-2 prerequisite {script} failed with return code "
            f"{completed.returncode}"
        )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = args.config.expanduser().resolve()
    bundle = load_config(config)
    database = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(
            cfg_get(bundle.payload, "paths.database_path"),
            base_dir=bundle.base_dir,
        )
    )
    output_dir = args.output_dir.expanduser().resolve()
    try:
        readiness = _readiness(config, database, as_of=args.as_of)
    except RuntimeError as exc:
        initial_failure = str(exc)
    else:
        print(
            json.dumps(
                {
                    "status": "PASS_ALREADY_CURRENT",
                    "as_of": args.as_of,
                    "database": str(database),
                    "readiness": readiness,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    common = ("--config", str(config), "--db", str(database))
    _run("01_load_consumer_defensive_universe.py", *common)
    _run(
        "01b_load_consumer_defensive_historical_membership.py",
        *common,
        "--as-of",
        args.as_of,
        "--output-dir",
        str(output_dir),
    )
    validation_report = output_dir / "stage2_validation.json"
    _run(
        "02_validate_consumer_defensive_universe.py",
        *common,
        "--as-of",
        args.as_of,
        "--report",
        str(validation_report),
    )
    readiness = _readiness(config, database, as_of=args.as_of)
    print(
        json.dumps(
            {
                "status": "PASS_REPAIRED",
                "as_of": args.as_of,
                "database": str(database),
                "initial_failure": initial_failure,
                "readiness": readiness,
                "validation_report": str(validation_report),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

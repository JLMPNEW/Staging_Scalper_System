#!/usr/bin/env python3
"""Compatibility wrapper: semiconductor daily backfill via the generic technology supervisor.

This script used to carry its own stale copy of the chunked supervisor loop, which
drifted from 19_supervise_technology_daily_backfill.py and collided with it on
status/log filenames. It now delegates to the generic supervisor with
``--family semiconductors`` so all chunking, retry, status, and log behavior comes
from one implementation. The historical semiconductor default start date
(2019-11-07) is preserved when the caller does not supply --start-date, and
--require-oos-score-valid is accepted and forwarded.
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TECHNOLOGY_SUPERVISOR = PROJECT_ROOT / "technology" / "scripts" / "19_supervise_technology_daily_backfill.py"
DEFAULT_START_DATE = "2019-11-07"


def build_forward_argv(argv: list[str]) -> list[str]:
    forwarded = ["--family", "semiconductors", *argv]
    has_start_date = any(item == "--start-date" or item.startswith("--start-date=") for item in argv)
    if not has_start_date:
        forwarded.extend(["--start-date", DEFAULT_START_DATE])
    return forwarded


def main() -> int:
    sys.argv = [str(TECHNOLOGY_SUPERVISOR), *build_forward_argv(sys.argv[1:])]
    supervisor_globals = runpy.run_path(str(TECHNOLOGY_SUPERVISOR))
    return int(supervisor_globals["main"]())


if __name__ == "__main__":
    raise SystemExit(main())

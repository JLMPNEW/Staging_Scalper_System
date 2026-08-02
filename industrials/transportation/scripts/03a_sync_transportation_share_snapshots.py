#!/usr/bin/env python3
from __future__ import annotations

import runpy
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.transportation.scripts._shared import DEFAULT_CONFIG, MODEL_FAMILY  # noqa: E402


if __name__ == "__main__":
    shared = PROJECT_ROOT / "industrials" / "scripts" / "03a_sync_industrials_share_snapshots.py"
    user_args = list(sys.argv[1:])
    pinned = {"--model-family", "--output-csv"}
    overridden = sorted(
        {
            arg.split("=", 1)[0]
            for arg in user_args
            if arg.split("=", 1)[0] in pinned
        }
    )
    if overridden:
        raise ValueError(
            f"Transportation share wrapper arguments are pinned and cannot be overridden: {overridden}"
        )
    if "--config" not in user_args and not any(
        arg.startswith("--config=") for arg in user_args
    ):
        user_args = ["--config", str(DEFAULT_CONFIG), *user_args]
    historical = "--include-historical" in user_args
    output_folder = "historical_load" if historical else "stage3"
    output_name = (
        "transportation_historical_share_snapshot_coverage.csv"
        if historical
        else "transportation_share_snapshot_coverage.csv"
    )
    sys.argv = [
        str(shared),
        "--model-family",
        MODEL_FAMILY,
        "--output-csv",
        str(
            PROJECT_ROOT
            / "output"
            / "industrials"
            / MODEL_FAMILY
            / output_folder
            / output_name
        ),
        *user_args,
    ]
    runpy.run_path(str(shared), run_name="__main__")

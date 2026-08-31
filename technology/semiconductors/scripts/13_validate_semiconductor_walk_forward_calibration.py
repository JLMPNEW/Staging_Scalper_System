#!/usr/bin/env python3
from __future__ import annotations

import logging
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.logging_utils import configure_utc_logging  # noqa: E402
from technology.core.optuna_artifact_governance import validate_walk_forward_from_argv  # noqa: E402


if __name__ == "__main__":
    configure_utc_logging()
    status = validate_walk_forward_from_argv("semiconductors")
    if status == 0:
        logging.getLogger("semiconductor_walk_forward_validator").info(
            "Semiconductor sealed walk-forward outputs validated."
        )
    raise SystemExit(status)

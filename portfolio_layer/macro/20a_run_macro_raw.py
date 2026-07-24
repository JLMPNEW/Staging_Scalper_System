#!/usr/bin/env python3
"""Refresh the Staging-owned MacroLayer raw database for a portfolio as-of date."""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from datetime import date
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import ensure_not_prod_path  # noqa: E402


LOGGER = logging.getLogger("run_macro_raw")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh the vendored Staging MacroLayer raw database.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", type=iso_date_arg, required=True)
    parser.add_argument("--macro-config", type=Path, default=None)
    parser.add_argument("--python-executable", default=sys.executable)
    return parser.parse_args()


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    vendor_root = resolve_path(cfg_get(config, "macro.vendor_root", "MacroLayer"), base_dir=config_path.parent)
    vendor_root = ensure_not_prod_path(vendor_root, label="MacroLayer vendor root")
    script = vendor_root / "run_macro_raw_pipeline.py"
    if not script.is_file():
        LOGGER.error("Missing MacroLayer raw pipeline: %s", script)
        return 1
    macro_config = ensure_not_prod_path(
        args.macro_config or (vendor_root / "config_macro_raw.yaml"),
        label="MacroLayer config",
    )
    command = [
        str(args.python_executable),
        str(script),
        "--config",
        str(macro_config),
        "--mode",
        "daily",
        "--as-of-date",
        args.as_of,
    ]
    LOGGER.info("Running MacroLayer raw DAG: %s", subprocess.list2cmdline(command))
    completed = subprocess.run(command, cwd=str(vendor_root), check=False)
    if completed.returncode != 0:
        LOGGER.error("MacroLayer raw DAG failed with exit_code=%s", completed.returncode)
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())

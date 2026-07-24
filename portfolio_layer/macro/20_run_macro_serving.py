#!/usr/bin/env python3
"""Stage 6 helper - run the vendored MacroLayer serving DAG safely.

This is a convenience wrapper, not a dependency of the portfolio contract build. It delegates to
portfolio_layer/MacroLayer/run_macro_serving_pipeline.py with the final optimizer integration disabled
so MacroLayer cannot overwrite portfolio-layer Stage 1/3 artifacts.
"""
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
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402


LOGGER = logging.getLogger("run_macro_serving")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run the vendored MacroLayer serving DAG without legacy optimizer writes.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, required=True)
    p.add_argument("--macro-config", type=Path, default=None)
    p.add_argument("--python-executable", type=str, default=sys.executable)
    p.add_argument("--refresh-industry-stock-foreign", action="store_true",
                   help="Also rebuild MacroLayer industry, stock overlay, portfolio inputs, stock sleeves, and foreign budget.")
    p.add_argument("--rebuild-policies", action="store_true")
    return p.parse_args()


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    vendor_root = resolve_path(cfg_get(config, "macro.vendor_root", "MacroLayer"), base_dir=config_path.parent)
    vendor_root = ensure_not_prod_path(vendor_root, label="MacroLayer vendor root")
    script = vendor_root / "run_macro_serving_pipeline.py"
    if not script.exists():
        LOGGER.error("Missing MacroLayer serving wrapper: %s", script)
        return 1
    macro_config = args.macro_config or (vendor_root / "config_macro_raw.yaml")
    macro_config = ensure_not_prod_path(macro_config, label="MacroLayer config")
    serving_db = ensure_not_prod_path(paths.macro_serving_db_path, label="macro serving db")

    cmd = [
        str(args.python_executable),
        str(script),
        "--config",
        str(macro_config),
        "--serving-db-path",
        str(serving_db),
        "--end-date",
        args.as_of,
        "--skip-final-optimizer",
        "--allow-shadow-failures",
    ]
    if args.rebuild_policies:
        cmd.append("--rebuild-policies")
    if not args.refresh_industry_stock_foreign:
        cmd.extend([
            "--skip-industry-macro",
            "--skip-stock-macro-overlay",
            "--skip-portfolio-inputs",
            "--skip-stock-sleeve-targets",
            "--skip-foreign-sleeve-budget",
        ])
    LOGGER.info("Running MacroLayer serving DAG: %s", subprocess.list2cmdline(cmd))
    try:
        subprocess.run(cmd, cwd=str(vendor_root), check=True)
    except subprocess.CalledProcessError as exc:
        LOGGER.error("MacroLayer serving DAG failed with exit_code=%s", exc.returncode)
        return int(exc.returncode or 1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

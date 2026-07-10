#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, expand_env_vars, load_yaml, resolve_path  # noqa: E402
from industrials.machinery.scoring import parse_asof  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh the SEC ownership database used by machinery positioning.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--profile", choices=("daily", "weekly"), default="daily")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def nested_config(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key, payload)
    if not isinstance(value, dict):
        raise ValueError(f"{key} config section must be a mapping")
    return value


def normalized_path(path: Path) -> str:
    return os.path.normcase(str(path.expanduser().resolve()))


def validate_database_target(*, machinery_config: dict[str, Any], orchestrator_config: Path) -> Path:
    orchestrator_payload = nested_config(load_yaml(orchestrator_config), "sec_form4_orchestrator")
    runtime_config_raw = str(cfg_get(orchestrator_payload, "form4.config_path", "") or "").strip()
    if not runtime_config_raw:
        raise ValueError(f"Missing form4.config_path in {orchestrator_config}")
    runtime_config = Path(runtime_config_raw).expanduser()
    if not runtime_config.is_absolute():
        runtime_config = (PROJECT_ROOT / runtime_config).resolve()
    runtime_payload = nested_config(load_yaml(runtime_config), "sec_form4")
    actual_db_raw = str(cfg_get(runtime_payload, "db_path", "") or "").strip()
    expected_db_raw = str(cfg_get(machinery_config, "upstream_databases.form4.db_path", "") or "").strip()
    if not actual_db_raw or not expected_db_raw:
        raise ValueError("Both machinery and SEC Form 4 configs must define the ownership database path")
    actual_db = Path(expand_env_vars(actual_db_raw)).expanduser()
    if not actual_db.is_absolute():
        actual_db = (runtime_config.parent / actual_db).resolve()
    expected_db = Path(expand_env_vars(expected_db_raw)).expanduser()
    if not expected_db.is_absolute():
        expected_db = (PACKAGE_ROOT / expected_db).resolve()
    if normalized_path(actual_db) != normalized_path(expected_db):
        raise ValueError(
            "SEC Form 4 orchestrator database does not match machinery upstream database: "
            f"orchestrator={actual_db} machinery={expected_db}"
        )
    return actual_db


def main() -> int:
    args = parse_args()
    asof = parse_asof(args.asof)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    runner = resolve_path(cfg_get(config, "upstream_databases.form4.orchestrator_script"), base_dir=base_dir)
    orchestrator_config = resolve_path(
        cfg_get(config, "upstream_databases.form4.orchestrator_config"),
        base_dir=base_dir,
    )
    if not runner.is_file():
        raise FileNotFoundError(f"SEC Form 4 orchestrator script not found: {runner}")
    if not orchestrator_config.is_file():
        raise FileNotFoundError(f"SEC Form 4 orchestrator config not found: {orchestrator_config}")
    database_path = validate_database_target(machinery_config=config, orchestrator_config=orchestrator_config)
    command = [
        sys.executable,
        str(runner),
        "--config",
        str(orchestrator_config),
        "--target",
        "form4",
        "--profile",
        args.profile,
        "--as-of-date",
        asof,
    ]
    if args.dry_run:
        command.append("--dry-run")
    print(f"Refreshing machinery SEC ownership upstream: profile={args.profile} asof={asof} db={database_path}")
    result = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
    return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())

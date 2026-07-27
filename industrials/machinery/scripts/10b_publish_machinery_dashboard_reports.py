#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.machinery.scoring import (  # noqa: E402
    dated_path,
    file_sha256,
    parse_asof,
    publish_dashboard,
    read_rows,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish immutable machinery dashboard and calibration contracts.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--allow-overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    asof = parse_asof(args.asof)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    score_root = resolve_path(cfg_get(config, "machinery_scoring.score_output_root"), base_dir=base_dir)
    dashboard_root = resolve_path(cfg_get(config, "machinery_scoring.dashboard_root"), base_dir=base_dir)
    input_path = args.input_csv.expanduser().resolve() if args.input_csv else dated_path(
        score_root,
        asof,
        "machinery_calibrated_scores.csv",
    )
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else dashboard_root / asof
    score_manifest_path = input_path.with_suffix(".manifest.json")
    try:
        score_manifest = json.loads(
            score_manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"Invalid machinery score manifest {score_manifest_path}: {exc}"
        ) from exc
    if (
        score_manifest.get("acceptance") != "PASS"
        or score_manifest.get("asof_date") != asof
        or score_manifest.get("output_sha256") != file_sha256(input_path)
    ):
        raise ValueError(
            "Machinery score manifest acceptance, date, or hash mismatch"
        )
    production_active = (
        score_manifest.get("production_policy_active") is True
    )
    production_metadata = score_manifest.get("production_metadata")
    if not isinstance(production_metadata, dict):
        raise ValueError("Machinery score manifest production metadata is invalid")
    manifest = publish_dashboard(
        output_dir=output_dir,
        rows=read_rows(input_path),
        asof=asof,
        allow_overwrite=args.allow_overwrite,
        production_policy_active=production_active,
        activation_metadata=production_metadata,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

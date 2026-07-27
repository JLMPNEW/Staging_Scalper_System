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
from industrials.machinery.stage12_activation_transaction import (  # noqa: E402
    preflight_activation_transaction,
    run_activation_transaction,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the fail-closed machinery production activation transaction: "
            "incremental refresh, candidate preparation, portfolio config "
            "promotion, dashboard publish, and full portfolio smoke."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--governance-dir", type=Path, default=None)
    parser.add_argument("--approval-token", default="")
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Check gates without refreshing, publishing, or changing config.",
    )
    parser.add_argument(
        "--skip-refresh",
        action="store_true",
        help=(
            "Use an already completed shadow dashboard for this date. "
            "Candidate validation still verifies its manifest and hashes."
        ),
    )
    parser.add_argument(
        "--force-candidate",
        action="store_true",
        help="Replace only this date's activation-candidate artifacts.",
    )
    parser.add_argument(
        "--reuse-risk-price-data",
        action="store_true",
        help=(
            "Forward the portfolio runner's explicit sealed-panel/cache reuse "
            "flags. Intended only for a reviewed provider outage."
        ),
    )
    parser.add_argument(
        "--resume-portfolio-smoke",
        action="store_true",
        help=(
            "Hash-validate and reuse a previously passed portfolio prefix, "
            "then run only ledger through final/earnings."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    governance_root = (
        args.governance_dir.expanduser().resolve()
        if args.governance_dir
        else resolve_path(
            cfg_get(config, "machinery_stage12.output_root"),
            base_dir=config_path.parent,
        )
    )
    if args.preflight:
        result = preflight_activation_transaction(
            config,
            config_path=config_path,
            governance_root=governance_root,
            asof=args.asof,
        )
    else:
        result = run_activation_transaction(
            config,
            config_path=config_path,
            governance_root=governance_root,
            asof=args.asof,
            approval_token=args.approval_token,
            run_refresh=not args.skip_refresh,
            force_candidate=bool(args.force_candidate),
            reuse_risk_price_data=bool(args.reuse_risk_price_data),
            resume_portfolio_smoke=bool(args.resume_portfolio_smoke),
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("acceptance") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

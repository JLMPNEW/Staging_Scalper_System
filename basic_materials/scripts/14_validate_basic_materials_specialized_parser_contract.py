"""Validate the fixture-first F1 parser contract without source hydration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from basic_materials.core.config import load_config, resolve_cli_path  # noqa: E402
from basic_materials.core.specialized_contract import (  # noqa: E402
    load_specialized_metric_registry,
    load_specialized_source_policy,
)
from basic_materials.core.specialized_parser_contract import (  # noqa: E402
    load_specialized_parser_fixtures,
    load_specialized_parser_policy,
    validate_specialized_parser_contract,
    write_specialized_parser_contract_reports,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--report-dir", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    policy = load_specialized_parser_policy(config.paths.specialized_parser_policy)
    fixtures = load_specialized_parser_fixtures(config.paths.specialized_parser_fixtures)
    registry = load_specialized_metric_registry(config.paths.specialized_metric_registry)
    source_policy = load_specialized_source_policy(config.paths.specialized_source_policy)
    report_dir = resolve_cli_path(
        args.report_dir,
        config.paths.output_root / "f1_parser_contract" / policy.as_of_date,
    )
    try:
        report = validate_specialized_parser_contract(
            policy=policy,
            fixtures=fixtures,
            registry=registry,
            source_policy=source_policy,
        )
        artifacts = write_specialized_parser_contract_reports(report, report_dir=report_dir)
        print(json.dumps({**report.summary_dict(), "artifacts": artifacts}, indent=2, sort_keys=True))
        return 0 if report.fixture_gate_passed and not report.production_execution_allowed else 2
    except Exception as exc:
        print(
            json.dumps({"contract_valid": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

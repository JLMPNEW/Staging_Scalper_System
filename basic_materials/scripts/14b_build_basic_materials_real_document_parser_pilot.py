"""Compile or replay the F1A.1 real-document parser pilot without SQLite writes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from basic_materials.core.config import load_config, resolve_cli_path  # noqa: E402
from basic_materials.core.source_registry import load_source_registry  # noqa: E402
from basic_materials.core.specialized_contract import (  # noqa: E402
    load_specialized_metric_registry,
)
from basic_materials.core.specialized_parser_contract import (  # noqa: E402
    load_specialized_parser_policy,
)
from basic_materials.core.specialized_parser_pilot import (  # noqa: E402
    load_golden_expectations,
    load_pilot_document_manifest,
    load_specialized_parser_pilot_policy,
    run_real_document_parser_pilot,
    write_real_document_parser_pilot_reports,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument(
        "--cache-only",
        action="store_true",
        help="Replay immutable semantic objects without opening raw source documents.",
    )
    parser.add_argument(
        "--allow-open",
        action="store_true",
        help="Return zero when compilation passes but reviewed golden coverage remains open.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = load_config(args.config)
        policy = load_specialized_parser_pilot_policy(
            config.paths.specialized_parser_pilot_policy
        )
        documents = load_pilot_document_manifest(
            config.paths.specialized_parser_pilot_documents
        )
        golden = load_golden_expectations(
            config.paths.specialized_parser_golden_expectations
        )
        registry = load_specialized_metric_registry(
            config.paths.specialized_metric_registry
        )
        parser_policy = load_specialized_parser_policy(
            config.paths.specialized_parser_policy
        )
        sources = load_source_registry(config.paths.source_registry)
        report_dir = resolve_cli_path(
            args.report_dir,
            config.paths.output_root
            / "f1a1_real_document_parser_pilot"
            / policy.as_of_date
            / ("cache_replay" if args.cache_only else "compile"),
        )
        report = run_real_document_parser_pilot(
            policy=policy,
            documents=documents,
            golden=golden,
            registry=registry,
            parser_policy=parser_policy,
            cache_root=config.paths.cache_root,
            registered_source_ids={source.source_id for source in sources.sources if source.active},
            cache_only=args.cache_only,
        )
        artifacts = write_real_document_parser_pilot_reports(
            report,
            report_dir=report_dir,
        )
        summary = report.summary_dict()
        print(
            json.dumps(
                {
                    "pilot_gate_passed": summary["pilot_gate_passed"],
                    "ready_for_golden_review": summary["ready_for_golden_review"],
                    "document_gate_passed": summary["document_gate_passed"],
                    "compiler_gate_passed": summary["compiler_gate_passed"],
                    "golden_gate_passed": summary["golden_gate_passed"],
                    "production_execution_allowed": summary[
                        "production_execution_allowed"
                    ],
                    "database_mutated": summary["database_mutated"],
                    "issue_count": summary["issue_count"],
                    "counts": summary["counts"],
                    "artifacts": artifacts,
                },
                indent=2,
                sort_keys=True,
            )
        )
        if report.pilot_gate_passed:
            return 0
        if args.allow_open and report.ready_for_golden_review:
            return 0
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {"pilot_valid": False, "error": f"{type(exc).__name__}: {exc}"},
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

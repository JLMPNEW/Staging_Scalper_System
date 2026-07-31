#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from industrials.core.config import (  # noqa: E402
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.reports import (  # noqa: E402
    write_csv_atomic,
    write_text_atomic,
)
from industrials.transportation.dedicated_parser_adapter import (  # noqa: E402
    metric_search_aliases,
)
from industrials.transportation.fixture_review import (  # noqa: E402
    EVIDENCE_DECISION_FIELDS,
    FIXTURE_REVIEW_VERSION,
    PAIR_DECISION_FIELDS,
    build_fixture_review_decisions,
)
from industrials.transportation.parser_coverage import read_csv  # noqa: E402
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Review the frozen A/B/C transportation fixture batches with "
            "conservative metric-specific semantic rules. Parse-free."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--reviewed-at",
        default="2026-07-29T00:00:00-05:00",
    )
    return parser.parse_args(argv)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)[
        "dedicated_parser"
    ]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "Fixture review requires parser execution disabled"
        )
    output_dir = (
        resolve_path(
            parser_cfg["output_root"],
            base_dir=config_path.parent,
        )
        / str(parser_cfg["source_census_asof_date"])
    )
    pair_path = output_dir / "transportation_fixture_priority_pairs.csv"
    evidence_path = (
        output_dir / "transportation_fixture_priority_evidence.csv"
    )
    priority_manifest_path = (
        output_dir / "transportation_fixture_priority_manifest.json"
    )
    for path in (pair_path, evidence_path, priority_manifest_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    priority_manifest = _read_json(priority_manifest_path)
    artifacts = priority_manifest.get("artifacts") or {}
    if (
        priority_manifest.get("acceptance") != "PASS"
        or str(
            (artifacts.get("priority_pairs") or {}).get("sha256")
            or ""
        )
        != file_sha256(pair_path)
        or str(
            (artifacts.get("priority_evidence") or {}).get("sha256")
            or ""
        )
        != file_sha256(evidence_path)
    ):
        raise ValueError("Fixture priority batches are not sealed")

    pair_rows, evidence_rows, summary, errors = (
        build_fixture_review_decisions(
            pair_rows=read_csv(pair_path),
            evidence_rows=read_csv(evidence_path),
            aliases=metric_search_aliases(),
            reviewed_at=str(args.reviewed_at),
        )
    )
    pair_output_path = (
        output_dir / "transportation_fixture_review_pair_decisions.csv"
    )
    evidence_output_path = (
        output_dir
        / "transportation_fixture_review_evidence_decisions.csv"
    )
    manifest_path = (
        output_dir / "transportation_fixture_review_manifest.json"
    )
    write_csv_atomic(
        pair_output_path,
        PAIR_DECISION_FIELDS,
        pair_rows,
    )
    write_csv_atomic(
        evidence_output_path,
        EVIDENCE_DECISION_FIELDS,
        evidence_rows,
    )
    payload = {
        "acceptance": "PASS" if pair_rows and not errors else "FAIL",
        "gate": "DP6Z_PRIORITY_SEMANTIC_FIXTURE_REVIEW",
        "review_version": FIXTURE_REVIEW_VERSION,
        "model_family": MODEL_FAMILY,
        "reviewed_at": args.reviewed_at,
        **summary,
        "parser_invocations": 0,
        "source_document_open_count": 0,
        "network_requests": 0,
        "retrieval_invocations": 0,
        "feature_build_invocations": 0,
        "historical_materialization_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_invocations": 0,
        "policy_registry_mutated": False,
        "production_promotion_authorized": False,
        "errors": errors,
        "inputs": {
            "priority_pairs": {
                "path": str(pair_path.resolve()),
                "sha256": file_sha256(pair_path),
            },
            "priority_evidence": {
                "path": str(evidence_path.resolve()),
                "sha256": file_sha256(evidence_path),
            },
        },
        "artifacts": {
            "pair_decisions": {
                "path": str(pair_output_path.resolve()),
                "row_count": len(pair_rows),
                "sha256": file_sha256(pair_output_path),
            },
            "evidence_decisions": {
                "path": str(evidence_output_path.resolve()),
                "row_count": len(evidence_rows),
                "sha256": file_sha256(evidence_output_path),
            },
        },
        "next_gate": (
            "AUDIT_FIXTURE_DECISIONS_THEN_BUILD_EXACT_POLICY_CANDIDATE"
            if not errors
            else "REVIEW_FIXTURE_DECISION_ERRORS"
        ),
    }
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["acceptance"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())

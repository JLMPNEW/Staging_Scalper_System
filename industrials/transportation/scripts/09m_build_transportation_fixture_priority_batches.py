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
from industrials.transportation.fixture_priority import (  # noqa: E402
    EVIDENCE_PRIORITY_FIELDS,
    FIXTURE_PRIORITY_VERSION,
    PAIR_PRIORITY_FIELDS,
    build_fixture_priority_batches,
)
from industrials.transportation.parser_coverage import read_csv  # noqa: E402
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Freeze non-overlapping transportation semantic-fixture review "
            "batches from stored evidence. No parser or retrieval is allowed."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
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
            "Fixture priority freeze requires parser execution disabled"
        )
    output_dir = (
        resolve_path(
            parser_cfg["output_root"],
            base_dir=config_path.parent,
        )
        / str(parser_cfg["source_census_asof_date"])
    )
    adjudication_path = (
        output_dir
        / "transportation_all_source_union_evidence_adjudication.csv"
    )
    adjudication_manifest_path = (
        output_dir
        / "transportation_all_source_union_evidence_adjudication_manifest.json"
    )
    pair_contract_path = (
        output_dir / "transportation_semantic_fixture_pair_contract.csv"
    )
    evidence_path = (
        output_dir / "transportation_semantic_fixture_evidence.csv"
    )
    semantic_manifest_path = (
        output_dir
        / "transportation_semantic_fixture_freeze_manifest.json"
    )
    for path in (
        adjudication_path,
        adjudication_manifest_path,
        pair_contract_path,
        evidence_path,
        semantic_manifest_path,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)
    adjudication_manifest = _read_json(adjudication_manifest_path)
    semantic_manifest = _read_json(semantic_manifest_path)
    if (
        adjudication_manifest.get("acceptance") != "PASS"
        or str(
            (
                adjudication_manifest.get("artifacts") or {}
            ).get("pair_adjudication", {}).get("sha256")
            or ""
        )
        != file_sha256(adjudication_path)
    ):
        raise ValueError("All-source adjudication is not sealed")
    semantic_artifacts = semantic_manifest.get("artifacts") or {}
    if (
        semantic_manifest.get("acceptance") != "PASS"
        or str(
            (
                semantic_artifacts.get(
                    "semantic_fixture_pair_contract"
                )
                or {}
            ).get("sha256")
            or ""
        )
        != file_sha256(pair_contract_path)
        or str(
            (
                semantic_artifacts.get("semantic_fixture_evidence")
                or {}
            ).get("sha256")
            or ""
        )
        != file_sha256(evidence_path)
    ):
        raise ValueError("Semantic fixture corpus is not sealed")

    pair_rows, evidence_rows, summary, errors = (
        build_fixture_priority_batches(
            adjudication_rows=read_csv(adjudication_path),
            pair_contract_rows=read_csv(pair_contract_path),
            evidence_rows=read_csv(evidence_path),
        )
    )
    pair_output_path = (
        output_dir / "transportation_fixture_priority_pairs.csv"
    )
    evidence_output_path = (
        output_dir / "transportation_fixture_priority_evidence.csv"
    )
    manifest_path = (
        output_dir / "transportation_fixture_priority_manifest.json"
    )
    write_csv_atomic(
        pair_output_path,
        PAIR_PRIORITY_FIELDS,
        pair_rows,
    )
    write_csv_atomic(
        evidence_output_path,
        EVIDENCE_PRIORITY_FIELDS,
        evidence_rows,
    )
    payload = {
        "acceptance": "PASS" if pair_rows and not errors else "FAIL",
        "gate": "DP6Y_FIXTURE_PRIORITY_BATCH_FREEZE",
        "priority_version": FIXTURE_PRIORITY_VERSION,
        "model_family": MODEL_FAMILY,
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
        "errors": errors,
        "inputs": {
            "adjudication": {
                "path": str(adjudication_path.resolve()),
                "sha256": file_sha256(adjudication_path),
            },
            "fixture_pair_contract": {
                "path": str(pair_contract_path.resolve()),
                "sha256": file_sha256(pair_contract_path),
            },
            "fixture_evidence": {
                "path": str(evidence_path.resolve()),
                "sha256": file_sha256(evidence_path),
            },
        },
        "artifacts": {
            "priority_pairs": {
                "path": str(pair_output_path.resolve()),
                "row_count": len(pair_rows),
                "sha256": file_sha256(pair_output_path),
            },
            "priority_evidence": {
                "path": str(evidence_output_path.resolve()),
                "row_count": len(evidence_rows),
                "sha256": file_sha256(evidence_output_path),
            },
        },
        "next_gate": (
            "REVIEW_PHASES_A_B_C_FROM_STORED_FIXTURES"
            if not errors
            else "REVIEW_FIXTURE_PRIORITY_FREEZE_ERRORS"
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

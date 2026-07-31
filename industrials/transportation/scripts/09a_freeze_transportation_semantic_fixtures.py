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
from industrials.transportation.parser_coverage import (  # noqa: E402
    read_csv,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)
from industrials.transportation.semantic_fixture_freeze import (  # noqa: E402
    SEMANTIC_EVIDENCE_FIELDS,
    SEMANTIC_FIXTURE_FREEZE_VERSION,
    SEMANTIC_METRIC_CONTRACT_FIELDS,
    SEMANTIC_PAIR_CONTRACT_FIELDS,
    build_semantic_metric_contracts,
    build_semantic_pair_contracts,
    summarize_semantic_freeze,
)


DATA_DIR = PROJECT_ROOT / "industrials" / "transportation" / "data"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Freeze metric-level semantic rules and the complete deferred "
            "transportation evidence fixture corpus. No source retrieval, "
            "parser execution, or review-policy mutation is allowed."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--adjudication-prefix",
        default="transportation_union",
        help="Filename prefix of the sealed parse-free adjudication inputs.",
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
    adjudication_prefix = str(args.adjudication_prefix).strip()
    if (
        not adjudication_prefix
        or adjudication_prefix in {".", ".."}
        or "/" in adjudication_prefix
        or chr(92) in adjudication_prefix
    ):
        raise ValueError("--adjudication-prefix must be a filename prefix")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)["dedicated_parser"]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError("Semantic fixture freeze requires parser execution disabled")
    base_dir = config_path.parent
    output_dir = resolve_path(parser_cfg["output_root"], base_dir=base_dir) / str(parser_cfg["source_census_asof_date"])
    adjudication_path = output_dir / f"{adjudication_prefix}_evidence_adjudication.csv"
    fixture_queue_path = output_dir / f"{adjudication_prefix}_metric_fixture_queue.csv"
    adjudication_manifest_path = output_dir / f"{adjudication_prefix}_evidence_adjudication_manifest.json"
    final_registry_path = DATA_DIR / "transportation_specialized_metric_discovery_registry.csv"
    support_registry_path = DATA_DIR / "transportation_parser_supporting_metric_registry.csv"
    required = (
        adjudication_path,
        fixture_queue_path,
        adjudication_manifest_path,
        final_registry_path,
        support_registry_path,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Missing semantic-freeze inputs: {missing}")
    adjudication_manifest = _read_json(adjudication_manifest_path)
    artifacts = adjudication_manifest.get("artifacts") or {}
    if (
        adjudication_manifest.get("acceptance") != "PASS"
        or str(artifacts.get("pair_adjudication", {}).get("sha256") or "") != file_sha256(adjudication_path)
        or str(artifacts.get("metric_fixture_queue", {}).get("sha256") or "") != file_sha256(fixture_queue_path)
    ):
        raise ValueError("Post-policy union adjudication is not hash-sealed")
    adjudication_rows = read_csv(adjudication_path)
    fixture_rows = read_csv(fixture_queue_path)
    deferred_metric_ids = {row["metric_id"] for row in adjudication_rows}
    metric_rows, errors = build_semantic_metric_contracts(
        final_metric_rows=read_csv(final_registry_path),
        supporting_metric_rows=read_csv(support_registry_path),
        search_aliases=metric_search_aliases(),
        deferred_metric_ids=deferred_metric_ids,
    )
    pair_rows, evidence_rows, pair_errors = build_semantic_pair_contracts(
        adjudication_rows=adjudication_rows,
        fixture_evidence_rows=fixture_rows,
        metric_contract_rows=metric_rows,
    )
    errors.extend(pair_errors)
    expected_pairs = int(str((adjudication_manifest.get("decision_counts") or {}).get("DEFER", 0)))
    expected_evidence = int(adjudication_manifest.get("fixture_evidence_row_count") or 0)
    if len(metric_rows) != 84:
        errors.append(f"parser metric contracts={len(metric_rows)} expected=84")
    if len(pair_rows) != expected_pairs:
        errors.append(f"fixture pairs={len(pair_rows)} expected={expected_pairs}")
    if len(evidence_rows) != expected_evidence:
        errors.append(f"frozen fixture evidence rows={len(evidence_rows)} expected={expected_evidence}")
    metric_path = output_dir / "transportation_semantic_metric_contract.csv"
    pair_path = output_dir / "transportation_semantic_fixture_pair_contract.csv"
    evidence_path = output_dir / "transportation_semantic_fixture_evidence.csv"
    manifest_path = output_dir / "transportation_semantic_fixture_freeze_manifest.json"
    write_csv_atomic(
        metric_path,
        SEMANTIC_METRIC_CONTRACT_FIELDS,
        metric_rows,
    )
    write_csv_atomic(
        pair_path,
        SEMANTIC_PAIR_CONTRACT_FIELDS,
        pair_rows,
    )
    write_csv_atomic(
        evidence_path,
        SEMANTIC_EVIDENCE_FIELDS,
        evidence_rows,
    )
    payload = {
        "acceptance": ("PASS" if metric_rows and pair_rows and not errors else "FAIL"),
        "gate": "DP6L_SEMANTIC_FIXTURE_FREEZE",
        "freeze_version": SEMANTIC_FIXTURE_FREEZE_VERSION,
        "model_family": MODEL_FAMILY,
        "asof_date": str(parser_cfg["source_census_asof_date"]),
        "adjudication_prefix": adjudication_prefix,
        **summarize_semantic_freeze(
            metric_rows=metric_rows,
            pair_rows=pair_rows,
            evidence_rows=evidence_rows,
        ),
        "semantic_rules_frozen": not errors,
        "existing_evidence_decisions_mutated": False,
        "review_policy_mutated": False,
        "retrieval_authorized": False,
        "parser_execution_authorized": False,
        "network_requests": 0,
        "retrieval_invocations": 0,
        "parser_invocations": 0,
        "feature_build_invocations": 0,
        "historical_materialization_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_invocations": 0,
        "production_promotion_authorized": False,
        "errors": errors,
        "inputs": {
            "adjudication": {
                "path": str(adjudication_path.resolve()),
                "sha256": file_sha256(adjudication_path),
            },
            "fixture_queue": {
                "path": str(fixture_queue_path.resolve()),
                "sha256": file_sha256(fixture_queue_path),
            },
            "final_metric_registry": {
                "path": str(final_registry_path.resolve()),
                "sha256": file_sha256(final_registry_path),
            },
            "supporting_metric_registry": {
                "path": str(support_registry_path.resolve()),
                "sha256": file_sha256(support_registry_path),
            },
        },
        "artifacts": {
            "semantic_metric_contract": {
                "path": str(metric_path.resolve()),
                "row_count": len(metric_rows),
                "sha256": file_sha256(metric_path),
            },
            "semantic_fixture_pair_contract": {
                "path": str(pair_path.resolve()),
                "row_count": len(pair_rows),
                "sha256": file_sha256(pair_path),
            },
            "semantic_fixture_evidence": {
                "path": str(evidence_path.resolve()),
                "row_count": len(evidence_rows),
                "sha256": file_sha256(evidence_path),
            },
        },
        "next_gate": (
            "FREEZE_FINANCIAL_INPUT_REPAIR_CONTRACTS" if not errors else "REVIEW_SEMANTIC_FIXTURE_FREEZE_ERRORS"
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

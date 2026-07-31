#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from industrials.core.config import (  # noqa: E402
    cfg_get,
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.reports import (  # noqa: E402
    write_csv_atomic,
    write_text_atomic,
)
from industrials.transportation.coverage_lift import (  # noqa: E402
    METRIC_GATE_FIELDS,
    PAIR_QUEUE_FIELDS,
    SOURCE_CANDIDATE_FIELDS,
    SOURCE_FILING_FIELDS,
    build_metric_gate_rows,
    build_pair_review_queue,
    build_source_filing_rows,
    screen_cached_source_candidates,
)
from industrials.transportation.dedicated_parser_adapter import (  # noqa: E402
    metric_search_aliases,
)
from industrials.transportation.parser_coverage import (  # noqa: E402
    PARSER_DERIVATIONS,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)


PACKAGE_VERSION = "transportation_coverage_lift_v1"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build the bounded transportation coverage-lift review package "
            "without network access, parsing, feature builds, or calibration."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--near-gate-max-shortfall",
        type=int,
        default=0,
    )
    return parser.parse_args(argv)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {
                str(key): str(value or "").strip()
                for key, value in row.items()
            }
            for row in csv.DictReader(handle)
        ]


def _derived_dependencies(path: Path) -> dict[str, tuple[str, ...]]:
    output: dict[str, list[str]] = defaultdict(list)
    for metric_id, contract in PARSER_DERIVATIONS.items():
        output[metric_id].extend(
            str(value) for value in contract["dependencies"]
        )
    for row in _read_csv(path):
        support = row["support_metric_id"]
        for consumer in row["consumer_metric_ids"].split("|"):
            if consumer:
                output[consumer].append(support)
    return {
        metric_id: tuple(sorted(set(supports)))
        for metric_id, supports in output.items()
    }


def _artifact(
    path: Path,
    *,
    row_count: int,
) -> dict[str, object]:
    return {
        "path": str(path),
        "row_count": row_count,
        "sha256": file_sha256(path),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    parser_cfg = family["dedicated_parser"]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "Coverage-lift packaging requires parser_execution_authorized=false"
        )
    base_dir = config_path.parent
    asof_date = str(parser_cfg["source_census_asof_date"])
    output_dir = (
        resolve_path(parser_cfg["output_root"], base_dir=base_dir)
        / asof_date
    )
    near_gate_max = (
        args.near_gate_max_shortfall
        or int(
            parser_cfg.get(
                "coverage_lift_near_gate_max_issuer_shortfall",
                2,
            )
        )
    )
    if near_gate_max < 1:
        raise ValueError("near-gate issuer shortfall must be at least 1")

    coverage_path = (
        output_dir / "transportation_ticker_metric_coverage.csv"
    )
    coverage_manifest_path = (
        output_dir / "transportation_parser_coverage_manifest.json"
    )
    recovery_path = output_dir / "dedicated_parser_recovery_assessment.csv"
    decisions_path = resolve_path(
        parser_cfg["source_decisions_csv"],
        base_dir=base_dir,
    )
    support_registry_path = resolve_path(
        parser_cfg["supporting_registry_csv"],
        base_dir=base_dir,
    )
    cache_dir = resolve_path(
        cfg_get(config, "sec_fundamentals.cache_dir"),
        base_dir=base_dir,
    )
    required = (
        coverage_path,
        coverage_manifest_path,
        recovery_path,
        decisions_path,
        support_registry_path,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Missing coverage-lift inputs: {missing}")
    coverage_manifest = json.loads(
        coverage_manifest_path.read_text(encoding="utf-8")
    )
    if (
        coverage_manifest.get("acceptance") != "PASS"
        or int(coverage_manifest.get("final_scope_row_count") or 0)
        != 14_400
        or int(coverage_manifest.get("failed_work_count") or 0) != 0
    ):
        raise ValueError(
            "Coverage lift requires the completed zero-failure DP6 manifest"
        )

    coverage_rows = _read_csv(coverage_path)
    gate_rows = build_metric_gate_rows(
        coverage_rows,
        near_gate_max_shortfall=near_gate_max,
    )
    if len(gate_rows) != 90:
        raise ValueError(
            f"Expected 90 metric-gate rows; observed {len(gate_rows)}"
        )
    pair_rows = build_pair_review_queue(
        coverage_rows,
        gate_rows,
        _read_csv(recovery_path),
    )
    candidate_rows, screening = screen_cached_source_candidates(
        decisions=_read_csv(decisions_path),
        coverage_rows=coverage_rows,
        gate_rows=gate_rows,
        cache_dir=cache_dir,
        aliases=metric_search_aliases(),
        derived_dependencies=_derived_dependencies(
            support_registry_path
        ),
    )
    filing_rows = build_source_filing_rows(candidate_rows)
    duplicate_candidate_keys = [
        key
        for key, count in Counter(
            str(row["candidate_key"]) for row in candidate_rows
        ).items()
        if count > 1
    ]
    if duplicate_candidate_keys:
        raise ValueError(
            "Duplicate delta-source candidate keys: "
            f"{duplicate_candidate_keys[:10]}"
        )

    gate_path = (
        output_dir / "transportation_coverage_lift_metric_gate.csv"
    )
    pair_path = (
        output_dir / "transportation_coverage_lift_review_queue.csv"
    )
    candidate_path = (
        output_dir / "transportation_coverage_lift_source_candidates.csv"
    )
    filing_path = (
        output_dir
        / "transportation_coverage_lift_source_candidate_filings.csv"
    )
    manifest_path = (
        output_dir / "transportation_coverage_lift_manifest.json"
    )
    write_csv_atomic(gate_path, METRIC_GATE_FIELDS, gate_rows)
    write_csv_atomic(pair_path, PAIR_QUEUE_FIELDS, pair_rows)
    write_csv_atomic(
        candidate_path,
        SOURCE_CANDIDATE_FIELDS,
        candidate_rows,
    )
    write_csv_atomic(
        filing_path,
        SOURCE_FILING_FIELDS,
        filing_rows,
    )

    target_counts = Counter(
        str(row["coverage_target_class"]) for row in gate_rows
    )
    source_target_metrics = sorted(
        str(row["metric_id"])
        for row in gate_rows
        if int(str(row["source_search_target"])) == 1
    )
    pair_counts = Counter(
        str(row["coverage_status"]) for row in pair_rows
    )
    source_form_counts = Counter(
        str(row["form_type"]) for row in filing_rows
    )
    source_basis_counts = Counter(
        str(row["candidate_basis"]) for row in filing_rows
    )
    payload = {
        "acceptance": "PASS",
        "package_version": PACKAGE_VERSION,
        "model_family": MODEL_FAMILY,
        "asof_date": asof_date,
        "run_id": coverage_manifest["run_id"],
        "gate": "DP6A_BOUNDED_COVERAGE_LIFT_PACKAGE",
        "network_invocations": 0,
        "provider_invocations": 0,
        "parser_invocations": 0,
        "feature_build_invocations": 0,
        "calibration_invocations": 0,
        "hydration_authorized": False,
        "parser_authorized": False,
        "near_gate_max_issuer_shortfall": near_gate_max,
        "metric_count": len(gate_rows),
        "metric_target_class_counts": dict(
            sorted(target_counts.items())
        ),
        "source_search_target_metric_count": len(
            source_target_metrics
        ),
        "source_search_target_metrics": source_target_metrics,
        "review_pair_count": len(pair_rows),
        "review_pair_status_counts": dict(sorted(pair_counts.items())),
        "source_screening": {
            **screening,
            "candidate_metric_filing_rows": len(candidate_rows),
            "candidate_filing_rows": len(filing_rows),
            "candidate_form_counts": dict(
                sorted(source_form_counts.items())
            ),
            "candidate_basis_counts": dict(
                sorted(source_basis_counts.items())
            ),
        },
        "inputs": {
            "coverage": _artifact(
                coverage_path,
                row_count=len(coverage_rows),
            ),
            "coverage_manifest": {
                "path": str(coverage_manifest_path),
                "sha256": file_sha256(coverage_manifest_path),
            },
            "recovery_assessment": {
                "path": str(recovery_path),
                "sha256": file_sha256(recovery_path),
            },
            "source_decisions": {
                "path": str(decisions_path),
                "sha256": file_sha256(decisions_path),
            },
            "support_registry": {
                "path": str(support_registry_path),
                "sha256": file_sha256(support_registry_path),
            },
        },
        "artifacts": {
            "metric_gate": _artifact(
                gate_path,
                row_count=len(gate_rows),
            ),
            "review_queue": _artifact(
                pair_path,
                row_count=len(pair_rows),
            ),
            "source_candidates": _artifact(
                candidate_path,
                row_count=len(candidate_rows),
            ),
            "source_candidate_filings": _artifact(
                filing_path,
                row_count=len(filing_rows),
            ),
        },
        "next_gate": (
            "REVIEW_EXISTING_EVIDENCE_AND_DELTA_SOURCE_CANDIDATES"
        ),
    }
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

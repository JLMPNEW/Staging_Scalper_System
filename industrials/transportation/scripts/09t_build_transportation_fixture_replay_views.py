#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from dedicated_parser.policy import (  # noqa: E402
    POLICY_FIELDS,
    load_review_policies,
)
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
    get_registry,
)
from industrials.transportation.parser_coverage import (  # noqa: E402
    read_csv,
    read_only_connection,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)


RUN_IDS = (58, 59, 60, 65)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build run-scoped views of the active transportation review "
            "policy for zero-provider offline replay."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument(
        "--policy-manifest",
        type=Path,
        default=Path(
            "transportation_fixture_review_policy_manifest.json"
        ),
    )
    return parser.parse_args(argv)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def _json_object(value: object, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(str(value or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label}: invalid JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label}: expected JSON object")
    return payload


def _json_array(value: object, *, label: str) -> list[Any]:
    try:
        payload = json.loads(str(value or "[]"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label}: invalid JSON array") from exc
    if not isinstance(payload, list):
        raise ValueError(f"{label}: expected JSON array")
    return payload


def _run_scope(
    connection: Any,
    *,
    run_id: int,
) -> tuple[set[tuple[str, str, str, str]], int]:
    rows = list(
        connection.execute(
            """
            SELECT ledger.*
            FROM sec_parser_run_work AS relation
            JOIN sec_parser_work_ledger AS ledger
              ON ledger.work_key = relation.work_key
            WHERE relation.run_id=?
            ORDER BY ledger.work_key
            """,
            (run_id,),
        )
    )
    if not rows:
        raise ValueError(f"run_id={run_id} has no linked work")
    incomplete = [
        str(row["work_key"])
        for row in rows
        if str(row["status"]) != "COMPLETED"
    ]
    if incomplete:
        raise ValueError(
            f"run_id={run_id} incomplete work={incomplete[:10]}"
        )
    scope: set[tuple[str, str, str, str]] = set()
    for row in rows:
        documents = _json_object(
            row["input_hashes_json"],
            label=f"{row['work_key']}:input_hashes_json",
        )
        requests = _json_array(
            row["requested_metrics_json"],
            label=f"{row['work_key']}:requested_metrics_json",
        )
        metrics = {
            str(item.get("metric_name") or "")
            for item in requests
            if isinstance(item, dict)
            and str(item.get("metric_name") or "")
        }
        for document in documents:
            for metric in metrics:
                scope.add(
                    (
                        str(row["ticker"]).upper(),
                        str(row["accession_number"]),
                        str(document),
                        metric,
                    )
                )
    return scope, len(rows)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)[
        "dedicated_parser"
    ]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "Replay view generation requires parser execution disabled"
        )
    foundation = resolve_foundation(config_path, args.db)
    output_dir = (
        resolve_path(
            parser_cfg["output_root"],
            base_dir=config_path.parent,
        )
        / str(parser_cfg["source_census_asof_date"])
    )
    fixture_policy_manifest_path = (
        args.policy_manifest
        if args.policy_manifest.is_absolute()
        else output_dir / args.policy_manifest
    ).expanduser().resolve()
    fixture_policy_manifest = _read_json(
        fixture_policy_manifest_path
    )
    if (
        fixture_policy_manifest.get("acceptance") != "PASS"
        or not fixture_policy_manifest.get("applied")
    ):
        raise ValueError("Fixture review policy is not applied and passing")

    registry = get_registry()
    registry_path = Path(
        registry.review_policy_path
    ).expanduser().resolve()
    golden_path = Path(
        registry.review_policy_golden_path
    ).expanduser().resolve()
    golden_payload = _read_json(golden_path)
    golden_expectations = golden_payload.get("expectations") or []
    if not isinstance(golden_expectations, list):
        raise ValueError("Active golden corpus has invalid expectations")
    applied = (
        fixture_policy_manifest.get("artifacts") or {}
    ).get("applied_registry") or {}
    if str(applied.get("sha256") or "") != file_sha256(registry_path):
        raise ValueError(
            "Active review policy changed after fixture policy seal"
        )
    active_rows = read_csv(registry_path)
    artifacts: dict[str, dict[str, object]] = {}
    run_policy_counts: dict[str, int] = {}
    run_golden_counts: dict[str, int] = {}
    run_work_counts: dict[str, int] = {}
    decision_counts: Counter[str] = Counter()
    with read_only_connection(
        foundation.db_path,
        timeout_sec=foundation.timeout_sec,
    ) as connection:
        for run_id in RUN_IDS:
            scope, work_count = _run_scope(
                connection,
                run_id=run_id,
            )
            scoped = [
                row
                for row in active_rows
                if (
                    row["ticker"].upper(),
                    row["accession_number"],
                    row["source_document"],
                    row["metric_name"],
                )
                in scope
            ]
            view_path = (
                output_dir
                / f"transportation_fixture_review_policy_run{run_id}.csv"
            )
            write_csv_atomic(
                view_path,
                POLICY_FIELDS,
                scoped,
            )
            load_review_policies(view_path)
            artifacts[f"run_{run_id}_policy"] = {
                "path": str(view_path.resolve()),
                "row_count": len(scoped),
                "sha256": file_sha256(view_path),
            }
            scoped_golden = [
                expectation
                for expectation in golden_expectations
                if (
                    str(expectation.get("ticker") or "").upper(),
                    str(expectation.get("accession_number") or ""),
                    str(expectation.get("document_name") or ""),
                    str(expectation.get("metric_name") or ""),
                )
                in scope
            ]
            golden_view_path = (
                output_dir
                / (
                    "transportation_fixture_review_policy_golden_"
                    f"run{run_id}.json"
                )
            )
            golden_view_payload = {
                "corpus_id": (
                    "transportation_review_policy_generated_"
                    f"run_{run_id}"
                ),
                "description": (
                    "Run-scoped exact-match expectations for offline "
                    f"policy replay run {run_id}."
                ),
                "base_run_id": run_id,
                "expectations": scoped_golden,
            }
            write_text_atomic(
                golden_view_path,
                json.dumps(
                    golden_view_payload,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
            )
            artifacts[f"run_{run_id}_golden"] = {
                "path": str(golden_view_path.resolve()),
                "row_count": len(scoped_golden),
                "sha256": file_sha256(golden_view_path),
            }
            run_policy_counts[str(run_id)] = len(scoped)
            run_golden_counts[str(run_id)] = len(scoped_golden)
            run_work_counts[str(run_id)] = work_count
            decision_counts.update(
                row["decision"] for row in scoped
            )

    errors: list[str] = []
    if run_policy_counts["58"] == 0:
        errors.append("run 58 policy view is unexpectedly empty")
    if run_policy_counts["59"] == 0:
        errors.append("run 59 policy view is unexpectedly empty")
    if run_policy_counts["65"] == 0:
        errors.append("run 65 policy view is unexpectedly empty")
    for run_id in RUN_IDS:
        if (
            run_policy_counts[str(run_id)] > 0
            and run_golden_counts[str(run_id)] == 0
        ):
            errors.append(
                f"run {run_id} golden view is unexpectedly empty"
            )
    payload = {
        "acceptance": "PASS" if not errors else "FAIL",
        "gate": "DP6ZB_RUN_SCOPED_FIXTURE_POLICY_VIEWS",
        "model_family": MODEL_FAMILY,
        "run_ids": list(RUN_IDS),
        "active_policy_row_count": len(active_rows),
        "run_policy_counts": run_policy_counts,
        "run_golden_counts": run_golden_counts,
        "run_work_counts": run_work_counts,
        "scoped_policy_decision_counts": dict(
            sorted(decision_counts.items())
        ),
        "source_document_open_count": 0,
        "network_requests": 0,
        "retrieval_invocations": 0,
        "parser_invocations": 0,
        "feature_build_invocations": 0,
        "historical_materialization_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_invocations": 0,
        "policy_registry_mutated": False,
        "errors": errors,
        "inputs": {
            "active_policy": {
                "path": str(registry_path),
                "sha256": file_sha256(registry_path),
            },
            "active_golden": {
                "path": str(golden_path),
                "sha256": file_sha256(golden_path),
            },
        },
        "artifacts": artifacts,
        "next_gate": (
            "POLICY_ONLY_REPLAY_RUNS_58_59_60_65"
            if not errors
            else "REVIEW_RUN_SCOPED_POLICY_ERRORS"
        ),
    }
    manifest_path = (
        output_dir
        / "transportation_fixture_replay_policy_views_manifest.json"
    )
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["acceptance"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())

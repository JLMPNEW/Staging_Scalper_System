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
    export_policy_golden_corpus,
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
from industrials.transportation.adjudication import (  # noqa: E402
    policy_match_key,
    policy_row,
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


POLICY_VERSION = "transportation_union_exact_confirmation_v1"
REVIEWED_BY = "codex_transportation_systematic_union_review_v1"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build or apply review policies for only the hash-exact union "
            "confirmations. This command never opens or reparses a filing."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--base-evaluation-id", type=int, default=1)
    parser.add_argument(
        "--adjudication-prefix",
        default="transportation_union_pre_policy",
        help="Filename prefix of the sealed pre-policy adjudication.",
    )
    parser.add_argument(
        "--reviewed-at",
        default="2026-07-27T00:00:00Z",
    )
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args(argv)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def _dedupe(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    seen: set[tuple[str, ...]] = set()
    for row in sorted(
        rows,
        key=lambda item: (
            item["ticker"],
            item["metric_name"],
            item["period_end"],
            item["policy_id"],
        ),
    ):
        key = policy_match_key(row)
        if key in seen:
            continue
        seen.add(key)
        output.append(row)
    return output


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    adjudication_prefix = str(args.adjudication_prefix).strip()
    if (
        not adjudication_prefix
        or adjudication_prefix in {".", ".."}
        or "/" in adjudication_prefix
        or "\\" in adjudication_prefix
    ):
        raise ValueError(
            "--adjudication-prefix must be a filename prefix"
        )
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)[
        "dedicated_parser"
    ]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "Review policy generation requires parser execution disabled"
        )
    base_dir = config_path.parent
    foundation = resolve_foundation(config_path, args.db)
    output_dir = (
        resolve_path(parser_cfg["output_root"], base_dir=base_dir)
        / str(parser_cfg["source_census_asof_date"])
    )
    adjudication_path = (
        output_dir
        / f"{adjudication_prefix}_evidence_adjudication.csv"
    )
    adjudication_manifest_path = (
        output_dir
        / f"{adjudication_prefix}_evidence_adjudication_manifest.json"
    )
    for path in (adjudication_path, adjudication_manifest_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    adjudication_manifest = _read_json(adjudication_manifest_path)
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
        raise ValueError("Union adjudication is not sealed and passing")
    accepted_pairs = [
        row
        for row in read_csv(adjudication_path)
        if row["review_decision"] == "ACCEPT"
    ]
    confirmed_keys = {
        key
        for row in accepted_pairs
        for key in row["confirmed_evidence_keys"].split("|")
        if key
    }
    with read_only_connection(
        foundation.db_path,
        timeout_sec=foundation.timeout_sec,
    ) as connection:
        evaluation = connection.execute(
            """
            SELECT *
            FROM sec_parser_review_evaluation
            WHERE evaluation_id=?
            """,
            (args.base_evaluation_id,),
        ).fetchone()
        if (
            evaluation is None
            or str(evaluation["status"]) != "COMPLETED"
            or str(evaluation["model_family"]) != MODEL_FAMILY
        ):
            raise ValueError("Base review evaluation is not complete")
        base_run_id = int(evaluation["base_run_id"])
        evidence: dict[str, dict[str, object]] = {}
        if confirmed_keys:
            for row in connection.execute(
                """
                SELECT *
                FROM sec_parser_review_evidence
                WHERE evaluation_id=?
                  AND evaluated_evidence_key IN (
                """
                + ",".join("?" for _ in confirmed_keys)
                + ")",
                (args.base_evaluation_id, *sorted(confirmed_keys)),
            ):
                key = str(row["evaluated_evidence_key"])
                normalized = dict(row)
                normalized["evidence_key"] = key
                evidence[key] = normalized
            # 08x confirms evidence from every reviewed stage (base, delta,
            # repair, direct); keys outside the base evaluation resolve from
            # the shared shadow-evidence store instead of crashing the
            # policy build.
            shadow_keys = sorted(confirmed_keys - set(evidence))
            if shadow_keys:
                for row in connection.execute(
                    """
                    SELECT *
                    FROM sec_parser_metric_evidence_shadow
                    WHERE model_family=?
                      AND evidence_key IN (
                    """
                    + ",".join("?" for _ in shadow_keys)
                    + ")",
                    (MODEL_FAMILY, *shadow_keys),
                ):
                    key = str(row["evidence_key"])
                    evidence[key] = dict(row)
    missing = sorted(confirmed_keys - set(evidence))
    if missing:
        raise ValueError(
            "Confirmed evidence is in neither the base evaluation nor the "
            "shadow evidence store: " + ", ".join(missing[:10])
        )
    generated = _dedupe(
        [
            policy_row(
                evidence[key],
                decision="ACCEPTED",
                status_reason=(
                    "exact_prior_accepted_disclosure_confirmation"
                ),
                reviewed_at=args.reviewed_at,
                run_id=base_run_id,
                policy_version=POLICY_VERSION,
                reviewed_by=REVIEWED_BY,
            )
            for key in sorted(confirmed_keys)
        ]
    )
    adapter_registry = get_registry()
    registry_path = Path(
        adapter_registry.review_policy_path
    ).expanduser().resolve()
    golden_path = Path(
        adapter_registry.review_policy_golden_path
    ).expanduser().resolve()
    existing = [
        row
        for row in read_csv(registry_path)
        if row["policy_version"] != POLICY_VERSION
    ]
    merged = [*existing, *generated]
    candidate_registry_path = (
        output_dir
        / "transportation_union_review_policy_candidate.csv"
    )
    candidate_golden_path = (
        output_dir
        / "transportation_union_review_policy_golden_candidate.json"
    )
    manifest_path = (
        output_dir
        / "transportation_union_review_policy_manifest.json"
    )
    write_csv_atomic(
        candidate_registry_path,
        POLICY_FIELDS,
        merged,
    )
    export_policy_golden_corpus(
        load_review_policies(candidate_registry_path),
        output_path=candidate_golden_path,
        corpus_id="transportation_review_policy_generated",
    )
    if args.apply:
        write_csv_atomic(registry_path, POLICY_FIELDS, merged)
        export_policy_golden_corpus(
            load_review_policies(registry_path),
            output_path=golden_path,
            corpus_id="transportation_review_policy_generated",
        )
    errors: list[str] = []
    if len(generated) != len(confirmed_keys):
        errors.append("confirmed evidence did not map one-to-one to policy")
    if len(accepted_pairs) != int(
        str(
            (
                adjudication_manifest.get("decision_counts") or {}
            ).get("ACCEPT", 0)
        )
    ):
        errors.append("accepted pair count does not match adjudication")
    policy_counts = Counter(row["decision"] for row in generated)
    payload = {
        "acceptance": "PASS" if generated and not errors else "FAIL",
        "gate": "DP6I_HASH_EXACT_UNION_REVIEW_POLICY",
        "model_family": MODEL_FAMILY,
        "policy_version": POLICY_VERSION,
        "reviewed_by": REVIEWED_BY,
        "reviewed_at": args.reviewed_at,
        "base_evaluation_id": args.base_evaluation_id,
        "base_run_id": base_run_id,
        "adjudication_prefix": adjudication_prefix,
        "accepted_pair_count": len(accepted_pairs),
        "confirmed_evidence_key_count": len(confirmed_keys),
        "generated_policy_count": len(generated),
        "generated_policy_decision_counts": dict(
            sorted(policy_counts.items())
        ),
        "applied": bool(args.apply),
        "policy_registry_mutated": bool(args.apply),
        "source_document_open_count": 0,
        "network_requests": 0,
        "parser_invocations": 0,
        "feature_build_invocations": 0,
        "historical_materialization_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_invocations": 0,
        "production_promotion_authorized": False,
        "errors": errors,
        "input": {
            "path": str(adjudication_path.resolve()),
            "sha256": file_sha256(adjudication_path),
        },
        "artifacts": {
            "candidate_registry": {
                "path": str(candidate_registry_path.resolve()),
                "row_count": len(merged),
                "sha256": file_sha256(candidate_registry_path),
            },
            "candidate_golden": {
                "path": str(candidate_golden_path.resolve()),
                "sha256": file_sha256(candidate_golden_path),
            },
            "applied_registry": {
                "path": str(registry_path),
                "sha256": (
                    file_sha256(registry_path) if args.apply else ""
                ),
            },
            "applied_golden": {
                "path": str(golden_path),
                "sha256": (
                    file_sha256(golden_path) if args.apply else ""
                ),
            },
        },
        "next_gate": (
            f"POLICY_ONLY_REPLAY_RUN_{base_run_id}"
            if args.apply
            else "REVIEW_CANDIDATE_AND_RERUN_WITH_APPLY"
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

#!/usr/bin/env python3
"""Materialize only conflict-free specialized metrics that passed coverage gates."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.db import connect, init_db, utc_now  # noqa: E402
from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.contracts import file_sha256  # noqa: E402
from industrials.transportation.investable_universe import (  # noqa: E402
    load_investable_universe_policy,
    validate_investable_universe_policy,
)
from industrials.transportation.semantic_candidate_materialization import (  # noqa: E402
    CONTRACT_VERSION,
    EXTRACTION_METHOD,
    SOURCE_ID,
    build_materialization_candidates,
    persist_materialization_candidates,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    resolve_foundation,
)


DEFAULT_POLICY = PROJECT_ROOT / "industrials" / "transportation" / "data" / "transportation_investable_universe_v5.yaml"
DEFAULT_SEMANTIC = PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v5" / "semantic_materialization" / "2026-08-13" / "transportation_semantic_materialization_audit.json"
DEFAULT_SURFACE = PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v4" / "surface_reentry" / "2026-08-13" / "v5_domain_coverage_strict" / "transportation_surface_v5_domain_coverage.json"
DEFAULT_TANKER = PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v5" / "tanker_coverage_strict" / "2026-08-13" / "transportation_tanker_v5_coverage.json"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v5" / "semantic_candidates"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--semantic-manifest", type=Path, default=DEFAULT_SEMANTIC)
    parser.add_argument("--surface-coverage", type=Path, default=DEFAULT_SURFACE)
    parser.add_argument("--tanker-coverage", type=Path, default=DEFAULT_TANKER)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan-only", action="store_true")
    mode.add_argument("--execute", action="store_true")
    return parser.parse_args()


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _verified_artifact(path_value: object, hash_value: object, *, label: str) -> Path:
    path = Path(str(path_value or "")).resolve()
    if not path.is_file() or file_sha256(path) != str(hash_value or ""):
        raise ValueError(f"{label} artifact is missing or changed: {path}")
    return path


def _surface_pairs(manifest: dict[str, Any]) -> set[tuple[str, str]]:
    domain_path = _verified_artifact(
        manifest.get("domain_csv"), manifest.get("domain_csv_sha256"), label="surface domain coverage"
    )
    mapping_path = _verified_artifact(
        manifest.get("mapping_path"), manifest.get("mapping_sha256"), label="surface domain mapping"
    )
    qualifying = {
        (row["metric_id"], row["comparison_domain_id"])
        for row in _csv(domain_path)
        if row.get("disposition") == "QUALIFIES"
    }
    declared = {
        tuple(str(item).split("::", 1))
        for item in manifest.get("qualifying_metric_domains", [])
    }
    if qualifying != declared:
        raise ValueError("surface qualifying domains disagree with the sealed summary")
    pairs: set[tuple[str, str]] = set()
    for row in _csv(mapping_path):
        key = (row["metric_id"], row["comparison_domain_id"])
        if key not in qualifying:
            continue
        pairs.update(
            (ticker.strip().upper(), row["metric_id"])
            for ticker in row["applicable_tickers"].split("|")
            if ticker.strip()
        )
    return pairs


def main() -> int:
    args = parse_args()
    asof = str(args.asof)[:10]
    semantic_path = args.semantic_manifest.expanduser().resolve()
    surface_path = args.surface_coverage.expanduser().resolve()
    tanker_path = args.tanker_coverage.expanduser().resolve()
    policy_path = args.policy.expanduser().resolve()
    semantic = _json(semantic_path)
    surface = _json(surface_path)
    tanker = _json(tanker_path)
    policy = load_investable_universe_policy(policy_path)
    policy_errors, _ = validate_investable_universe_policy(policy)
    if policy_errors or policy.policy_version != "transportation_investable_universe_v5":
        raise ValueError(f"v5 policy is invalid: {policy_errors}")
    if semantic.get("acceptance") != "PASS" or semantic.get("contract_version") != "transportation_semantic_materialization_v1":
        raise ValueError("semantic conflict audit is not accepted")
    if surface.get("acceptance") != "PASS" or tanker.get("acceptance") != "PASS":
        raise ValueError("surface/tanker strict coverage gates must both pass")
    if (
        Path(str(tanker.get("policy_path") or "")).resolve() != policy_path
        or str(tanker.get("policy_sha256") or "") != file_sha256(policy_path)
    ):
        raise ValueError("tanker coverage does not pin the current v5 policy")
    semantic_hash = file_sha256(semantic_path)
    for label, manifest in (("surface", surface), ("tanker", tanker)):
        if (
            str(manifest.get("semantic_materialization_manifest_sha256") or "") != semantic_hash
            or Path(str(manifest.get("semantic_materialization_manifest_path") or "")).resolve() != semantic_path
        ):
            raise ValueError(f"{label} coverage does not pin this semantic audit")
    lanes = semantic.get("lanes") or {}
    surface_lane = lanes.get("surface") or {}
    tanker_lane = lanes.get("tanker") or {}
    surface_replay = _verified_artifact(
        surface_lane.get("conflict_free_csv"), surface_lane.get("conflict_free_csv_sha256"), label="surface conflict-free replay"
    )
    tanker_replay = _verified_artifact(
        tanker_lane.get("conflict_free_csv"), tanker_lane.get("conflict_free_csv_sha256"), label="tanker conflict-free replay"
    )
    surface_pairs = _surface_pairs(surface)
    tanker_group = next(group for group in policy.groups if group.group_id == "oil_tanker_operators")
    tanker_metrics = {str(item) for item in tanker.get("metrics_meeting_strict_gates", [])}
    if not tanker_metrics:
        raise ValueError("no tanker specialized metric passed the strict gate")
    tanker_pairs = {
        (ticker, metric) for ticker in tanker_group.tickers for metric in tanker_metrics
    }
    common_lineage = {
        "semantic_manifest": str(semantic_path),
        "semantic_manifest_sha256": semantic_hash,
        "surface_coverage_manifest": str(surface_path),
        "surface_coverage_manifest_sha256": file_sha256(surface_path),
        "tanker_coverage_manifest": str(tanker_path),
        "tanker_coverage_manifest_sha256": file_sha256(tanker_path),
        "investable_policy": str(policy_path),
        "investable_policy_sha256": file_sha256(policy_path),
    }
    candidates = build_materialization_candidates(
        _csv(surface_replay), lane="surface", allowed_pairs=surface_pairs, asof=asof, lineage=common_lineage
    ) + build_materialization_candidates(
        _csv(tanker_replay), lane="tanker", allowed_pairs=tanker_pairs, asof=asof, lineage=common_lineage
    )
    if not candidates:
        raise ValueError("qualifying semantic materialization set is empty")
    foundation = resolve_foundation(args.config.expanduser().resolve(), args.db)
    persistence: dict[str, int] = {}
    if args.execute:
        with connect(foundation.db_path, timeout_sec=foundation.timeout_sec) as connection:
            init_db(connection)
            with connection:
                persistence = persist_materialization_candidates(
                    connection, candidates, now=utc_now()
                )
    counts_by_lane = Counter(item.lane for item in candidates)
    counts_by_metric = Counter(item.metric_name for item in candidates)
    payload = {
        "acceptance": "PASS",
        "asof_date": asof,
        "mode": "execute" if args.execute else "plan_only",
        "contract_version": CONTRACT_VERSION,
        "source_id": SOURCE_ID,
        "extraction_method": EXTRACTION_METHOD,
        "candidate_count": len(candidates),
        "candidate_count_by_lane": dict(sorted(counts_by_lane.items())),
        "candidate_count_by_metric": dict(sorted(counts_by_metric.items())),
        "surface_qualifying_pair_count": len(surface_pairs),
        "tanker_qualifying_pair_count": len(tanker_pairs),
        "persistence": persistence,
        "lineage": common_lineage,
        "network_requests": 0,
        "parser_invocations": 0,
        "historical_reconstruction_performed": False,
        "calibration_performed": False,
        "production_activation_performed": False,
        "next_gate": "REBUILD_CURRENT_SPECIALIZED_AVAILABILITY_AND_AUDIT_V5_READINESS",
    }
    output_dir = args.output_root.expanduser().resolve() / asof
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "transportation_semantic_candidate_materialization.json"
    write_text_atomic(output_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

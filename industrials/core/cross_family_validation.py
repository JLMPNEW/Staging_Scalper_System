"""Read-only contract comparison for cross-family model replication."""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def csv_header(path: Path) -> tuple[str, ...]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return tuple(csv.DictReader(handle).fieldnames or ())


def component_compatibility(
    *,
    target_components: Sequence[str],
    source_components: Sequence[str],
    semantic_mapping: Mapping[str, str | None],
) -> list[dict[str, str]]:
    source = set(source_components)
    rows: list[dict[str, str]] = []
    used_source: set[str] = set()
    for target in target_components:
        mapped = semantic_mapping.get(target, target)
        available = bool(mapped and mapped in source)
        if available and mapped is not None:
            used_source.add(mapped)
        relation = (
            "exact"
            if available and mapped == target
            else "semantic_mapping"
            if available
            else "unmapped"
        )
        rows.append(
            {
                "target_component": target,
                "source_component": str(mapped or ""),
                "relation": relation,
                "available_flag": "1" if available else "0",
            }
        )
    for field in source_components:
        if field not in used_source:
            rows.append(
                {
                    "target_component": "",
                    "source_component": field,
                    "relation": "source_only",
                    "available_flag": "1",
                }
            )
    return rows


def compare_replication_contracts(
    *,
    target_components: Sequence[str],
    source_components: Sequence[str],
    semantic_mapping: Mapping[str, str | None],
    target_horizons: Sequence[int],
    source_horizons: Sequence[int],
    target_return_basis: str,
    source_return_basis: str,
    target_cost_bps: float,
    source_cost_bps: float | None,
    target_benchmark: str,
    source_benchmark: str,
) -> dict[str, Any]:
    component_rows = component_compatibility(
        target_components=target_components,
        source_components=source_components,
        semantic_mapping=semantic_mapping,
    )
    target_rows = [row for row in component_rows if row["target_component"]]
    unmapped = [
        row["target_component"]
        for row in target_rows
        if row["available_flag"] != "1"
    ]
    mapped = [
        row["target_component"]
        for row in target_rows
        if row["relation"] == "semantic_mapping"
    ]
    exact_components = not unmapped and not mapped and (
        set(target_components) == set(source_components)
    )
    exact_horizons = tuple(target_horizons) == tuple(source_horizons)
    exact_return_basis = target_return_basis == source_return_basis
    exact_costs = (
        source_cost_bps is not None
        and abs(target_cost_bps - source_cost_bps) <= 1e-12
    )
    direct_ready = all(
        (exact_components, exact_horizons, exact_return_basis, exact_costs)
    )
    blockers: list[str] = []
    if unmapped:
        blockers.append(f"unmapped_target_components:{','.join(unmapped)}")
    if mapped:
        blockers.append(
            f"semantic_component_adapter_required:{','.join(mapped)}"
        )
    if not exact_horizons:
        blockers.append("horizon_contract_mismatch")
    if not exact_return_basis:
        blockers.append("return_basis_mismatch")
    if not exact_costs:
        blockers.append("transaction_cost_contract_missing_or_mismatched")
    if target_benchmark == source_benchmark:
        benchmark_relation = "same"
    else:
        benchmark_relation = "family_specific"
    return {
        "component_rows": component_rows,
        "unmapped_target_components": unmapped,
        "semantic_mapped_components": mapped,
        "exact_component_contract": exact_components,
        "exact_horizon_contract": exact_horizons,
        "exact_return_basis": exact_return_basis,
        "exact_transaction_cost_contract": exact_costs,
        "benchmark_relation": benchmark_relation,
        "direct_replication_ready": direct_ready,
        "machinery_acceptance_eligible": False,
        "blockers": blockers,
    }

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from industrials.core.reports import write_csv_atomic
from industrials.transportation.financial_contract import MetricDefinition
from industrials.transportation.oos_outcomes import finite_float, fmt
from industrials.transportation.selected_feature_history import (
    iter_gzip_csv,
    sha256,
    stable_json_sha256,
    write_manifest,
)
from industrials.transportation.walk_forward_calibration import (
    generic_baseline_scores,
    percentile_scores,
)
from industrials.transportation.zero_overlay_monitoring import (
    SOURCE_FIELDS,
    load_monitoring_policy,
)


SOURCE_VERSION = "transportation_candidate_shadow_source_v1"


def source_paths(output_root: Path, asof: str) -> tuple[Path, Path]:
    directory = output_root / "sources" / asof
    return (
        directory / "transportation_candidate_shadow_source.csv",
        directory / "transportation_candidate_shadow_source_manifest.json",
    )


def build_source_rows(
    panel_rows: Sequence[Mapping[str, str]],
    *,
    asof: str,
    policy: Mapping[str, Any],
    definitions: Sequence[MetricDefinition],
    component_weights: Mapping[str, float],
) -> list[dict[str, object]]:
    generic_rows = [
        row
        for row in panel_rows
        if row.get("asof_date") == asof
        and row.get("metric_family") == "generic"
        and row.get("source_lane") == "V2_GENERIC"
    ]
    if not generic_rows:
        raise ValueError(f"{asof}: complete panel has no generic rows")
    baselines = generic_baseline_scores(
        generic_rows,
        definitions=definitions,
        component_weights=component_weights,
    )
    candidates = {
        str(metric): str(cohort)
        for metric, cohort in policy["candidate_cohorts"].items()
    }
    directions = {
        str(metric): int(value)
        for metric, value in policy["candidate_directions"].items()
    }
    adjusted: dict[str, dict[str, float]] = defaultdict(dict)
    candidate_rows: dict[tuple[str, str], Mapping[str, str]] = {}
    for row in panel_rows:
        if row.get("asof_date") != asof:
            continue
        metric = str(row.get("metric_id") or "")
        ticker = str(row.get("ticker") or "")
        cohort = str(row.get("calibration_cohort") or "")
        if candidates.get(metric) != cohort:
            continue
        value = finite_float(row.get("metric_value"))
        if value is None:
            continue
        adjusted[metric][ticker] = value * directions[metric]
        candidate_rows[(metric, ticker)] = row
    percentiles = {
        metric: percentile_scores(values)
        for metric, values in adjusted.items()
    }
    output: list[dict[str, object]] = []
    counts: dict[str, int] = defaultdict(int)
    for (metric, ticker), row in sorted(candidate_rows.items()):
        baseline = baselines.get((asof, ticker))
        specialized = percentiles.get(metric, {}).get(ticker)
        if baseline is None or specialized is None:
            continue
        counts[metric] += 1
        output.append(
            {
                "asof_date": asof,
                "ticker": ticker,
                "metric_id": metric,
                "calibration_cohort": str(row["calibration_cohort"]),
                "baseline_score": fmt(baseline["baseline_score"]),
                "specialized_percentile": fmt(specialized),
            }
        )
    minimum = int(policy["minimum_cross_section_per_candidate"])
    missing = [
        metric
        for metric in sorted(candidates)
        if counts.get(metric, 0) < minimum
    ]
    if missing:
        raise ValueError(
            f"{asof}: candidate source cross-section below {minimum}={missing}"
        )
    return output


def export_source_snapshot(
    *,
    asof: str,
    complete_panel: Path,
    policy_path: Path,
    registry_path: Path,
    definitions: Sequence[MetricDefinition],
    component_weights: Mapping[str, float],
    output_root: Path,
) -> dict[str, Any]:
    if not complete_panel.is_file():
        raise FileNotFoundError(complete_panel)
    policy = load_monitoring_policy(policy_path)
    rows = [
        row
        for row in iter_gzip_csv(complete_panel)
        if row.get("asof_date") == asof
    ]
    source_rows = build_source_rows(
        rows,
        asof=asof,
        policy=policy,
        definitions=definitions,
        component_weights=component_weights,
    )
    source_path, manifest_path = source_paths(output_root, asof)
    panel_hash = sha256(complete_panel)
    policy_hash = sha256(policy_path)
    registry_hash = sha256(registry_path)
    weights_hash = stable_json_sha256(
        {str(key): float(value) for key, value in component_weights.items()}
    )
    if source_path.exists() or manifest_path.exists():
        if not source_path.is_file() or not manifest_path.is_file():
            raise FileExistsError(
                f"incomplete monitoring source artifact={source_path.parent}"
            )
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            existing.get("acceptance") == "PASS"
            and existing.get("source_snapshot_sha256") == sha256(source_path)
            and existing.get("complete_panel_sha256") == panel_hash
            and existing.get("policy_sha256") == policy_hash
            and existing.get("metric_registry_sha256") == registry_hash
            and existing.get("component_weights_sha256") == weights_hash
        ):
            return existing
        raise FileExistsError(
            f"refusing to overwrite non-identical monitoring source={source_path}"
        )
    write_csv_atomic(source_path, SOURCE_FIELDS, source_rows)
    counts: dict[str, int] = defaultdict(int)
    for row in source_rows:
        counts[str(row["metric_id"])] += 1
    manifest = {
        "acceptance": "PASS",
        "artifact_family": "transportation_candidate_shadow_source",
        "source_version": SOURCE_VERSION,
        "asof_date": asof,
        "row_count": len(source_rows),
        "candidate_row_counts": dict(sorted(counts.items())),
        "source_snapshot_path": str(source_path.resolve()),
        "source_snapshot_sha256": sha256(source_path),
        "complete_panel_path": str(complete_panel.resolve()),
        "complete_panel_sha256": panel_hash,
        "policy_path": str(policy_path.resolve()),
        "policy_sha256": policy_hash,
        "metric_registry_path": str(registry_path.resolve()),
        "metric_registry_sha256": registry_hash,
        "component_weights_sha256": weights_hash,
        "outcomes_accessed": False,
        "outcome_fields_written": False,
        "parser_invocations": 0,
        "historical_rebuilds": 0,
        "calibration_invocations": 0,
        "portfolio_writes": 0,
        "database_writes": 0,
        "production_promotion_performed": False,
    }
    write_manifest(manifest_path, manifest)
    return manifest

#!/usr/bin/env python3
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CompositePolicy:
    composite_key: str
    metric_key: str
    feature_name: str
    ref_area: str
    regime_block: str
    base_weight: float
    required_flag: int
    min_feature_coverage_flag: int
    max_staleness_days_override: int | None
    source_quality_multiplier: float
    smoothing_window_days: int
    min_composite_coverage_ratio: float
    min_required_coverage_ratio: float
    notes: str


def _parse_int(value: str | None, *, default: int | None = None) -> int | None:
    text = str(value or "").strip()
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        return default


def _parse_float(value: str | None, *, default: float | None = None) -> float | None:
    text = str(value or "").strip()
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def load_composite_policy(csv_path: Path) -> list[CompositePolicy]:
    rows: list[CompositePolicy] = []
    seen: set[tuple[str, str, str]] = set()
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            composite_key = str(row.get("composite_key", "") or "").strip()
            metric_key = str(row.get("metric_key", "") or "").strip()
            feature_name = str(row.get("feature_name", "") or "").strip()
            if not composite_key or not metric_key or not feature_name:
                continue
            key = (composite_key, metric_key, feature_name)
            if key in seen:
                raise ValueError(
                    "Duplicate composite policy row for "
                    f"composite_key={composite_key} metric_key={metric_key} feature_name={feature_name}"
                )
            seen.add(key)
            base_weight = _parse_float(row.get("base_weight"), default=None)
            if base_weight is None or base_weight < 0.0:
                raise ValueError(
                    "Composite policy base_weight must be non-negative for "
                    f"composite_key={composite_key} metric_key={metric_key}"
                )
            min_feature_coverage_flag = _parse_int(row.get("min_feature_coverage_flag"), default=1)
            source_quality_multiplier = _parse_float(row.get("source_quality_multiplier"), default=1.0)
            smoothing_window_days = _parse_int(row.get("smoothing_window_days"), default=1)
            min_composite_coverage_ratio = _parse_float(row.get("min_composite_coverage_ratio"), default=0.0)
            min_required_coverage_ratio = _parse_float(row.get("min_required_coverage_ratio"), default=0.0)
            rows.append(
                CompositePolicy(
                    composite_key=composite_key,
                    metric_key=metric_key,
                    feature_name=feature_name,
                    ref_area=str(row.get("ref_area", "") or "").strip(),
                    regime_block=str(row.get("regime_block", "") or "").strip(),
                    base_weight=float(base_weight),
                    required_flag=int(_parse_int(row.get("required_flag"), default=0) or 0),
                    min_feature_coverage_flag=int(min_feature_coverage_flag if min_feature_coverage_flag is not None else 1),
                    max_staleness_days_override=_parse_int(row.get("max_staleness_days_override"), default=None),
                    source_quality_multiplier=float(source_quality_multiplier if source_quality_multiplier is not None else 1.0),
                    smoothing_window_days=int(smoothing_window_days if smoothing_window_days is not None else 1),
                    min_composite_coverage_ratio=float(min_composite_coverage_ratio if min_composite_coverage_ratio is not None else 0.0),
                    min_required_coverage_ratio=float(min_required_coverage_ratio if min_required_coverage_ratio is not None else 0.0),
                    notes=str(row.get("notes", "") or "").strip(),
                )
            )
    rows.sort(key=lambda item: (item.composite_key, item.metric_key, item.feature_name))
    return rows

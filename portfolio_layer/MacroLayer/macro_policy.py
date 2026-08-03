#!/usr/bin/env python3
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from macro_raw_config import parse_boolish


def _parse_int(raw: str | None, default: int | None = None) -> int | None:
    text = str(raw or "").strip()
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        return default


def _parse_float(raw: str | None, default: float | None = None) -> float | None:
    text = str(raw or "").strip()
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        return default


@dataclass(frozen=True)
class MetricPolicy:
    metric_key: str
    ref_area: str
    frequency: str
    regime_block: str
    max_staleness_days: int
    carry_forward_allowed: bool
    source_quality_weight: float
    country_class_applicability: str | None
    required_a_full: bool
    required_b_partial: bool
    required_c_fallback: bool
    qa_rule: str | None
    qa_min_value: float | None
    qa_max_value: float | None
    notes: str | None


def load_metric_policy(csv_path: Path) -> dict[str, MetricPolicy]:
    if not csv_path.exists():
        raise FileNotFoundError(f"Macro metric policy CSV not found: {csv_path}")
    policies: dict[str, MetricPolicy] = {}
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            metric_key = str(row.get("metric_key", "") or "").strip()
            if not metric_key:
                continue
            if metric_key in policies:
                raise ValueError(f"Duplicate metric_key in macro metric policy: {metric_key}")
            max_staleness_days = _parse_int(row.get("max_staleness_days"), default=None)
            source_quality_weight = _parse_float(row.get("source_quality_weight"), default=None)
            policies[metric_key] = MetricPolicy(
                metric_key=metric_key,
                ref_area=str(row.get("ref_area", "") or "").strip() or "USA",
                frequency=str(row.get("frequency", "") or "").strip() or "monthly",
                regime_block=str(row.get("regime_block", "") or "").strip(),
                max_staleness_days=max_staleness_days if max_staleness_days is not None else 45,
                carry_forward_allowed=parse_boolish(row.get("carry_forward_allowed"), default=True),
                source_quality_weight=source_quality_weight if source_quality_weight is not None else 1.0,
                country_class_applicability=str(row.get("country_class_applicability", "") or "").strip() or None,
                required_a_full=parse_boolish(row.get("required_a_full"), default=False),
                required_b_partial=parse_boolish(row.get("required_b_partial"), default=False),
                required_c_fallback=parse_boolish(row.get("required_c_fallback"), default=False),
                qa_rule=str(row.get("qa_rule", "") or "").strip() or None,
                qa_min_value=_parse_float(row.get("qa_min_value"), default=None),
                qa_max_value=_parse_float(row.get("qa_max_value"), default=None),
                notes=str(row.get("notes", "") or "").strip() or None,
            )
    return policies


def required_for_country_class(policy: MetricPolicy, country_class: str | None) -> bool:
    if country_class == "A_full":
        return policy.required_a_full
    if country_class == "B_partial":
        return policy.required_b_partial
    if country_class == "C_fallback":
        return policy.required_c_fallback
    return False

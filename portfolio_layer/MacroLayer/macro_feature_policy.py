#!/usr/bin/env python3
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path


logger = logging.getLogger(__name__)


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
class FeaturePolicy:
    metric_key: str
    feature_name: str
    ref_area: str
    frequency: str
    regime_block: str
    transform_code: str
    lookback_periods: int
    annualization_basis: int | None
    zscore_window: int
    percentile_window: int
    min_history_periods: int
    sign_multiplier: float
    standardized_clip_min: float | None
    standardized_clip_max: float | None
    notes: str | None


def load_feature_policy(csv_path: Path) -> dict[str, FeaturePolicy]:
    if not csv_path.exists():
        raise FileNotFoundError(f"Macro feature policy CSV not found: {csv_path}")
    policies: dict[str, FeaturePolicy] = {}
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            metric_key = str(row.get("metric_key", "") or "").strip()
            if not metric_key:
                continue
            feature_name = str(row.get("feature_name", "") or "").strip() or "level"
            lookback_periods = _parse_int(row.get("lookback_periods"), default=0)
            annualization_basis = _parse_int(row.get("annualization_basis"), default=None)
            zscore_window = _parse_int(row.get("zscore_window"), default=60)
            percentile_window = _parse_int(row.get("percentile_window"), default=60)
            min_history_periods = _parse_int(row.get("min_history_periods"), default=24)
            sign_multiplier = _parse_float(row.get("sign_multiplier"), default=1.0)
            if metric_key in policies:
                logger.warning("Duplicate metric_key=%s in feature policy CSV; last row wins.", metric_key)
            policies[metric_key] = FeaturePolicy(
                metric_key=metric_key,
                feature_name=feature_name,
                ref_area=str(row.get("ref_area", "") or "").strip() or "USA",
                frequency=str(row.get("frequency", "") or "").strip() or "monthly",
                regime_block=str(row.get("regime_block", "") or "").strip(),
                transform_code=str(row.get("transform_code", "") or "").strip() or "level",
                lookback_periods=max(0, lookback_periods if lookback_periods is not None else 0),
                annualization_basis=annualization_basis if annualization_basis is None else max(1, annualization_basis),
                zscore_window=max(1, zscore_window if zscore_window is not None else 60),
                percentile_window=max(1, percentile_window if percentile_window is not None else 60),
                min_history_periods=max(1, min_history_periods if min_history_periods is not None else 24),
                sign_multiplier=sign_multiplier if sign_multiplier is not None else 1.0,
                standardized_clip_min=_parse_float(row.get("standardized_clip_min"), default=None),
                standardized_clip_max=_parse_float(row.get("standardized_clip_max"), default=None),
                notes=str(row.get("notes", "") or "").strip() or None,
            )
    return policies

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
from pathlib import Path

from macro_raw_config import configure_pipeline_logging

logger = logging.getLogger(__name__)

DEFAULT_METRIC_POLICY = Path(__file__).resolve().with_name("macro_metric_policy.csv")
DEFAULT_COUNTRY_METADATA = Path(__file__).resolve().with_name("macro_country_metadata_seed.csv")
DEFAULT_OUTPUT = Path(__file__).resolve().with_name("macro_feature_policy.csv")

FIELDNAMES = [
    "metric_key",
    "feature_name",
    "ref_area",
    "frequency",
    "regime_block",
    "transform_code",
    "lookback_periods",
    "annualization_basis",
    "zscore_window",
    "percentile_window",
    "min_history_periods",
    "sign_multiplier",
    "standardized_clip_min",
    "standardized_clip_max",
    "notes",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the tier-1 macro feature policy file from the metric policy and country metadata.")
    parser.add_argument("--metric-policy", type=Path, default=DEFAULT_METRIC_POLICY)
    parser.add_argument("--country-metadata", type=Path, default=DEFAULT_COUNTRY_METADATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def _read_rows(path: Path) -> list[dict[str, str]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return [dict(row) for row in csv.DictReader(f)]


def _windows_by_frequency(frequency: str) -> tuple[int, int, int]:
    freq = str(frequency or "").strip().lower()
    mapping = {
        "daily": (252, 252, 63),
        "weekly": (104, 104, 26),
        "monthly": (60, 60, 24),
        "quarterly": (40, 40, 12),
    }
    return mapping.get(freq, (60, 60, 24))


def _metric_suffix(metric_key: str, ref_area: str) -> str:
    ref = str(ref_area or "").strip().lower()
    key = str(metric_key or "").strip()
    prefix = f"{ref}_"
    if ref and key.startswith(prefix):
        return key[len(prefix) :]
    # U.S. metrics are keyed as us_* while ref_area is stored as USA.
    if ref == "usa" and key.startswith("us_"):
        return key[3:]
    return key


def _country_metadata_by_ref_area(country_rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for row in country_rows:
        ref_area = str(row.get("oecd_ref_area") or row.get("ref_area") or "").strip().upper()
        if ref_area:
            out[ref_area] = row
    return out


def _feature_spec(metric_key: str, ref_area: str, frequency: str, regime_block: str) -> tuple[str, str, int, int | None]:
    suffix = _metric_suffix(metric_key, ref_area)
    freq = str(frequency or "").strip().lower()

    if metric_key == "us_real_gdp":
        return "qoq_ann_pct", "annualized_pct_change", 1, 4
    if suffix == "real_gdp_growth":
        return "level", "level", 0, None
    if metric_key in {"global_copper", "global_wheat"}:
        return "ann_3m_pct", "annualized_pct_change", 3, 12
    if suffix == "fx_usd":
        return "pct_21d", "pct_change", 21, None
    if suffix in {"neer", "reer"} or metric_key == "us_real_broad_dollar":
        return "pct_12m", "pct_change", 12, None
    if metric_key in {"us_nominal_broad_dollar", "us_wti_spot", "us_brent_spot", "us_henry_hub_natgas"}:
        return "pct_21d", "pct_change", 21, None
    if metric_key in {"us_initial_claims", "us_initial_claims_4w"}:
        return "yoy_pct", "pct_change", 52, None

    level_suffixes = {"cli", "bci", "cci", "unemployment_rate", "short_term_rate", "long_term_yield"}
    level_metrics = {
        "us_ads_index",
        "us_cfnai",
        "us_cfnai_ma3",
        "us_nfci",
        "us_anfci",
        "us_hy_oas",
        "us_yield_curve_10y2y",
        "us_yield_curve_10y3m",
        "us_effective_fed_funds",
        "us_sofr",
        "us_5y_breakeven",
        "us_5y5y_forward_inflation",
        "us_10y_real_yield",
        # V2.1 stress components (V2_1_CANDIDATE_SPEC.md): spreads/vol are levels —
        # a pct_change of a spread that can cross zero is undefined.
        "us_ig_spread_baa10y",
        "us_equity_vol",
    }
    if metric_key in level_metrics or suffix in level_suffixes:
        return "level", "level", 0, None

    if regime_block in {"inflation_now", "growth_now", "growth_lead"} and freq in {"monthly", "quarterly"}:
        lookback = 12 if freq == "monthly" else 4
        return "yoy_pct", "pct_change", lookback, None

    if regime_block == "inflation_lead":
        return "level", "level", 0, None

    logger.warning(
        "Unrecognized metric/regime combination for metric_key=%s ref_area=%s regime_block=%s frequency=%s; defaulting to level transform.",
        metric_key,
        ref_area,
        regime_block,
        frequency,
    )
    return "level", "level", 0, None


def _fx_sign_from_units(units: str | None) -> float:
    text = str(units or "").strip().lower()
    if not text:
        return 1.0
    if "per u.s. dollar" in text:
        return 1.0
    if "u.s. dollars per" in text:
        return -1.0
    return 1.0


def _sign_multiplier(
    metric_key: str,
    ref_area: str,
    regime_block: str,
    country_meta_map: dict[str, dict[str, str]],
) -> float:
    suffix = _metric_suffix(metric_key, ref_area)
    if suffix == "fx_usd":
        country_meta = country_meta_map.get(str(ref_area or "").upper(), {})
        return _fx_sign_from_units(country_meta.get("fred_fx_usd_units"))
    if suffix in {"neer", "reer"}:
        return -1.0
    if metric_key in {"us_nominal_broad_dollar", "us_real_broad_dollar", "us_wti_spot", "us_brent_spot", "us_henry_hub_natgas", "global_copper", "global_wheat"}:
        return 1.0
    if metric_key in {"us_initial_claims", "us_initial_claims_4w", "us_hy_oas", "us_nfci", "us_anfci", "us_effective_fed_funds", "us_sofr", "us_10y_real_yield", "us_ig_spread_baa10y", "us_equity_vol"}:
        return -1.0
    if suffix in {"unemployment_rate", "short_term_rate", "long_term_yield"}:
        return -1.0
    if metric_key in {"us_yield_curve_10y2y", "us_yield_curve_10y3m", "us_ads_index", "us_cfnai", "us_cfnai_ma3", "us_5y_breakeven", "us_5y5y_forward_inflation"}:
        return 1.0
    if suffix in {"cli", "bci", "cci", "real_gdp_growth"}:
        return 1.0
    if regime_block.startswith("inflation"):
        return 1.0
    if regime_block.startswith("growth"):
        return 1.0
    return 1.0


def build_rows(metric_rows: list[dict[str, str]], country_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    country_meta_map = _country_metadata_by_ref_area(country_rows)
    out: list[dict[str, str]] = []
    for row in metric_rows:
        metric_key = str(row.get("metric_key", "") or "").strip()
        if not metric_key:
            continue
        ref_area = str(row.get("ref_area", "") or "").strip() or "USA"
        frequency = str(row.get("frequency", "") or "").strip() or "monthly"
        regime_block = str(row.get("regime_block", "") or "").strip()
        feature_name, transform_code, lookback_periods, annualization_basis = _feature_spec(
            metric_key,
            ref_area,
            frequency,
            regime_block,
        )
        zscore_window, percentile_window, min_history_periods = _windows_by_frequency(frequency)
        sign_multiplier = _sign_multiplier(metric_key, ref_area, regime_block, country_meta_map)
        out.append(
            {
                "metric_key": metric_key,
                "feature_name": feature_name,
                "ref_area": ref_area,
                "frequency": frequency,
                "regime_block": regime_block,
                "transform_code": transform_code,
                "lookback_periods": str(lookback_periods),
                "annualization_basis": str(annualization_basis or ""),
                "zscore_window": str(zscore_window),
                "percentile_window": str(percentile_window),
                "min_history_periods": str(min_history_periods),
                "sign_multiplier": f"{sign_multiplier:.2f}",
                "standardized_clip_min": "-5.0",
                "standardized_clip_max": "5.0",
                "notes": "Generated tier-1 feature policy defaults from metric policy and country metadata.",
            }
        )
    out.sort(key=lambda item: (item["ref_area"], item["metric_key"]))
    return out


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    with open(tmp_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    tmp_path.replace(path)


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    rows = build_rows(_read_rows(args.metric_policy), _read_rows(args.country_metadata))
    write_rows(args.output, rows)
    logger.info("Wrote %d feature policy rows to: %s", len(rows), args.output)


if __name__ == "__main__":
    main()

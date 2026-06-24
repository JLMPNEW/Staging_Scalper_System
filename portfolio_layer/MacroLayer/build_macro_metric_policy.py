#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
from pathlib import Path

from macro_raw_config import configure_pipeline_logging, parse_boolish

logger = logging.getLogger(__name__)

DEFAULT_REGISTRY = Path(__file__).resolve().with_name("macro_metric_registry_full.csv")
DEFAULT_COUNTRY_METADATA = Path(__file__).resolve().with_name("macro_country_metadata_seed.csv")
DEFAULT_OUTPUT = Path(__file__).resolve().with_name("macro_metric_policy.csv")

FIELDNAMES = [
    "metric_key",
    "ref_area",
    "frequency",
    "regime_block",
    "max_staleness_days",
    "carry_forward_allowed",
    "source_quality_weight",
    "country_class_applicability",
    "required_a_full",
    "required_b_partial",
    "required_c_fallback",
    "qa_rule",
    "qa_min_value",
    "qa_max_value",
    "notes",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the tier-1 macro metric policy file from the enabled runtime registry.")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--country-metadata", type=Path, default=DEFAULT_COUNTRY_METADATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def _read_rows(path: Path) -> list[dict[str, str]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return [dict(row) for row in csv.DictReader(f)]

def _country_class_map(country_rows: list[dict[str, str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in country_rows:
        if not parse_boolish(row.get("enabled"), default=True):
            continue
        if not parse_boolish(row.get("country_pack_enabled"), default=True):
            continue
        ref_area = str(row.get("oecd_ref_area") or row.get("ref_area") or "").strip().upper()
        country_class = str(row.get("country_class") or "").strip()
        if ref_area and country_class:
            out[ref_area] = country_class
    return out


def _metric_suffix(metric_key: str, ref_area: str) -> str:
    ref = str(ref_area or "").strip().lower()
    key = str(metric_key or "").strip()
    prefix = f"{ref}_"
    if ref and key.startswith(prefix):
        return key[len(prefix) :]
    return key


def _max_staleness_days(metric_key: str, ref_area: str, frequency: str, source_name: str) -> int:
    suffix = _metric_suffix(metric_key, ref_area)
    source = str(source_name or "").strip()
    if metric_key == "us_nominal_broad_dollar":
        return 10
    if suffix == "fx_usd" and ref_area not in {"USA", "INT"}:
        return 10
    if suffix in {"neer", "reer"}:
        return 105
    if metric_key in {"global_copper", "global_wheat"}:
        return 105
    if source == "oecd_sdmx":
        if frequency == "monthly":
            return 105
        if frequency == "quarterly":
            return 180
    mapping = {
        "daily": 3,
        "weekly": 10,
        "monthly": 45,
        "quarterly": 120,
    }
    return mapping.get(str(frequency or "").strip().lower(), 45)


def _source_quality_weight(source_name: str) -> str:
    mapping = {
        "phillyfed_ads": "1.00",
        "fred_alfred": "1.00",
        "eia_seriesid": "1.00",
        "oecd_sdmx": "0.95",
    }
    return mapping.get(str(source_name or "").strip(), "0.90")


def _qa_rule(metric_key: str, ref_area: str) -> tuple[str, str, str]:
    suffix = _metric_suffix(metric_key, ref_area)
    if metric_key in {"us_wti_spot", "us_brent_spot"}:
        return "bounded", "-200.0", "500.0"
    if metric_key == "us_henry_hub_natgas":
        return "bounded", "-50.0", "100.0"
    positive_suffixes = {
        "fx_usd",
        "neer",
        "reer",
        "headline_cpi",
        "industrial_production",
    }
    positive_metrics = {
        "us_nonfarm_payrolls",
        "us_initial_claims",
        "us_initial_claims_4w",
        "us_industrial_production",
        "us_real_personal_income_less_transfers",
        "us_real_mfg_trade_sales",
        "us_real_gdp",
        "us_building_permits",
        "us_single_family_permits",
        "us_housing_starts",
        "us_single_family_starts",
        "us_advance_retail_sales",
        "us_real_retail_sales",
        "us_durable_goods_orders",
        "us_durable_goods_ex_transport",
        "us_headline_cpi",
        "us_core_cpi",
        "us_headline_pce",
        "us_core_pce",
        "us_avg_hourly_earnings",
        "us_eci_all",
        "us_ppi_final_demand",
        "us_ppi_core_goods_less_food_energy",
        "global_copper",
        "global_wheat",
    }
    bounded_metrics = {
        "us_yield_curve_10y2y",
        "us_yield_curve_10y3m",
        "us_hy_oas",
        "us_effective_fed_funds",
        "us_sofr",
        "us_5y_breakeven",
        "us_5y5y_forward_inflation",
        "us_10y_real_yield",
        "us_unemployment_rate",
    }
    bounded_suffixes = {
        "core_cpi_ex_food_energy",
        "unemployment_rate",
        "short_term_rate",
        "long_term_yield",
        "real_gdp_growth",
    }
    if metric_key in positive_metrics or suffix in positive_suffixes:
        return "positive", "0.0", ""
    if metric_key in bounded_metrics or suffix in bounded_suffixes:
        return "bounded", "-100.0", "100.0"
    return "finite", "", ""


def _required_flags(metric_key: str, ref_area: str, country_class: str | None) -> tuple[str, str, str]:
    if ref_area in {"USA", "INT"} or not country_class:
        return "0", "0", "0"
    suffix = _metric_suffix(metric_key, ref_area)
    if country_class == "A_full":
        return "1", "0", "0"
    if country_class == "B_partial":
        required = "1" if suffix in {"fx_usd", "neer", "reer"} else "0"
        return "0", required, "0"
    if country_class == "C_fallback":
        required = "1" if suffix in {"fx_usd", "neer", "reer", "headline_cpi", "cli"} else "0"
        return "0", "0", required
    return "0", "0", "0"


def build_rows(registry_rows: list[dict[str, str]], country_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    country_class_by_ref_area = _country_class_map(country_rows)
    out: list[dict[str, str]] = []
    for row in registry_rows:
        if not parse_boolish(row.get("enabled"), default=True):
            continue
        metric_key = str(row.get("metric_key", "") or "").strip()
        ref_area = str(row.get("ref_area", "") or "").strip() or "USA"
        frequency = str(row.get("frequency", "") or "").strip() or "monthly"
        regime_block = str(row.get("regime_block", "") or "").strip()
        country_class = country_class_by_ref_area.get(ref_area)
        required_a_full, required_b_partial, required_c_fallback = _required_flags(metric_key, ref_area, country_class)
        qa_rule, qa_min_value, qa_max_value = _qa_rule(metric_key, ref_area)
        if ref_area in {"USA", "INT"}:
            applicability = "GLOBAL"
        else:
            applicability = country_class or "UNSPECIFIED"
        out.append(
            {
                "metric_key": metric_key,
                "ref_area": ref_area,
                "frequency": frequency,
                "regime_block": regime_block,
                "max_staleness_days": str(_max_staleness_days(metric_key, ref_area, frequency, str(row.get("source_name", "") or "").strip())),
                "carry_forward_allowed": "1",
                "source_quality_weight": _source_quality_weight(str(row.get("source_name", "") or "").strip()),
                "country_class_applicability": applicability,
                "required_a_full": required_a_full,
                "required_b_partial": required_b_partial,
                "required_c_fallback": required_c_fallback,
                "qa_rule": qa_rule,
                "qa_min_value": qa_min_value,
                "qa_max_value": qa_max_value,
                "notes": "Generated tier-1 policy defaults for QA and PIT serving.",
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
    rows = build_rows(_read_rows(args.registry), _read_rows(args.country_metadata))
    write_rows(args.output, rows)
    logger.info("Wrote %d policy rows to: %s", len(rows), args.output)


if __name__ == "__main__":
    main()

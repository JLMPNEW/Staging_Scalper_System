from __future__ import annotations

import csv
import json
import math
import re
import sqlite3
from collections import defaultdict
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

from dedicated_parser.contracts import (
    AdapterRegistry,
    MetricEvidence,
    MetricRequest,
    MetricRequirement,
    NormalizedFact,
    WorkItem,
)
from dedicated_parser.semantic import SemanticBlock, parse_semantic_document
from industrials.machinery.disclosure_documents import extract_document_text
from industrials.transportation.content_text_cache import (
    ExtractionOptions,
    extract_document_once,
)
from industrials.transportation.tanker_metric_derivations import (
    derive_tanker_table_evidence,
)
from industrials.transportation.surface_metric_parser import (
    derive_surface_table_evidence,
    derive_surface_xbrl_evidence,
    surface_fact_rule,
)


ADAPTER_VERSION = "transportation_specialized_metrics_v3.discovery9"
_ROOT = Path(__file__).resolve().parent
_DATA = _ROOT / "data"
_FINAL_REGISTRY = _DATA / "transportation_specialized_metric_discovery_registry.csv"
_SUPPORT_REGISTRY = _DATA / "transportation_parser_supporting_metric_registry.csv"
_FINAL_SCOPE = _DATA / "transportation_dedicated_parser_scope.csv"
_SUPPORT_SCOPE = _DATA / "transportation_dedicated_parser_support_scope.csv"
_TANKER_SOURCE_MAP = _DATA / "transportation_tanker_metric_source_map_v1.csv"
_TANKER_XBRL_MAP = _DATA / "transportation_tanker_exact_xbrl_concepts_v1.csv"
_TANKER_FILING_PROFILES = _DATA / "transportation_tanker_filing_profiles_v1.csv"
_SURFACE_SOURCE_MAP = _DATA / "transportation_surface_metric_source_map_v2.csv"
_SURFACE_XBRL_OPERAND_MAP = _DATA / "transportation_surface_xbrl_operand_map_v1.csv"
_SURFACE_FILING_PROFILES = _DATA / "transportation_surface_filing_profiles_v2.csv"
_EXTRACTION_OVERRIDES = _DATA / "transportation_metric_extraction_overrides_v1.csv"
_DERIVATION_CONTRACTS = _DATA / "transportation_metric_derivation_contracts_v1.csv"
_ISSUER_ALIASES = _DATA / "transportation_issuer_aliases_v1.csv"
_INVESTABLE_V3_POLICY = _DATA / "transportation_investable_universe_v3.yaml"
_REVIEW_POLICY = _ROOT / "review_policies" / "dedicated_parser_review_policy.csv"
_REVIEW_POLICY_GOLDEN = _DATA / "transportation_dedicated_parser_review_policy_golden.json"

_SUPPORTED_FORMS = (
    "10-K",
    "10-K/A",
    "10-K405",
    "10-K405/A",
    "10-KT",
    "10-KT/A",
    "10-Q",
    "10-Q/A",
    "10-QT",
    "10-QT/A",
    "10-12B",
    "10-12B/A",
    "10-12G",
    "10-12G/A",
    "20-F",
    "20-F/A",
    "40-F",
    "40-F/A",
    "6-K",
    "6-K/A",
    "8-K",
    "8-K/A",
    "ARS",
    "DEF 14A",
    "DEF 14A/A",
    "DEF 14C",
    "DEF 14C/A",
    "DEFM14A",
    "DEFM14A/A",
    "PREM14A",
    "PREM14A/A",
    "FWP",
    "S-1",
    "S-1/A",
    "F-1",
    "F-1/A",
    "F-4",
    "F-4/A",
    "S-4",
    "S-4/A",
    "424B3",
    "424B4",
    "424B5",
)

# The registry-derived human label is always searched. These aliases widen
# discovery for common issuer terminology and acronyms without admitting an
# automatic acceptance rule. Metric-specific acceptance is added only after a
# positive/prohibited fixture pair has been reviewed.
_ALIASES: dict[str, tuple[str, ...]] = {
    "rail_carload_growth": ("carload growth", "carloads"),
    "rail_intermodal_volume_growth": (
        "intermodal volume growth",
        "intermodal units",
    ),
    "revenue_ton_miles_growth": (
        "revenue ton miles",
        "revenue tonne kilometres",
        "RTM",
    ),
    "shipment_or_load_growth": ("shipment growth", "load growth"),
    "pricing_or_yield_growth": (
        "pricing growth",
        "yield growth",
        "revenue per shipment growth",
    ),
    "operating_ratio": ("operating ratio",),
    "purchased_transportation_ratio": (
        "purchased transportation expense",
        "purchased transportation costs",
    ),
    "fuel_surcharge_revenue_ratio": ("fuel surcharge revenue",),
    "empty_mile_ratio": ("empty miles", "non-revenue miles"),
    "equipment_utilization": (
        "equipment utilization",
        "fleet utilization",
    ),
    "fleet_or_equipment_count": (
        "tractor count",
        "trailer count",
        "railcar count",
        "locomotive count",
    ),
    "service_reliability_rate": (
        "on-time service",
        "service reliability",
    ),
    "rail_network_velocity": ("train velocity", "network velocity"),
    "terminal_dwell_time": ("terminal dwell",),
    "freight_weight_per_shipment": ("weight per shipment",),
    "average_length_of_haul": ("length of haul",),
    "driver_turnover_rate": ("driver turnover",),
    "surface_lease_yield": ("lease yield", "rental yield"),
    "surface_asset_age": ("average equipment age", "average railcar age"),
    "rail_fuel_efficiency": (
        "fuel efficiency",
        "gallons per thousand gross ton miles",
    ),
    "insurance_claims_cost_ratio": (
        "insurance and claims expense",
        "claims cost ratio",
    ),
    "logistics_net_revenue_margin": (
        "net revenue margin",
        "gross profit margin",
    ),
    "traffic_growth": (
        "revenue passenger miles",
        "revenue passenger kilometres",
        "revenue ton miles",
        "RPM",
        "RPK",
        "RTM",
    ),
    "capacity_growth": (
        "available seat miles",
        "available seat kilometres",
        "available ton miles",
        "ASM",
        "ASK",
        "ATM",
    ),
    "passenger_load_factor": ("passenger load factor", "load factor"),
    "passenger_yield": ("passenger yield", "yield per passenger mile"),
    "passenger_revenue_per_capacity_unit": ("PRASM", "passenger RASK"),
    "total_revenue_per_capacity_unit": ("TRASM", "total RASK"),
    "unit_cost": ("CASM", "CASK"),
    "unit_cost_ex_fuel": (
        "CASM excluding fuel",
        "CASM ex fuel",
        "CASK excluding fuel",
    ),
    "fuel_price_per_gallon": (
        "average fuel price per gallon",
        "economic fuel price per gallon",
    ),
    "aircraft_utilization_hours": (
        "aircraft utilization",
        "block hours per aircraft day",
    ),
    "ancillary_revenue_per_passenger": ("ancillary revenue per passenger",),
    "passenger_throughput_growth": ("passenger traffic growth",),
    "cargo_throughput_growth": ("cargo tonnage growth",),
    "aircraft_movements_growth": (
        "aircraft movements",
        "takeoffs and landings",
    ),
    "lease_utilization": ("lease utilization", "fleet utilization"),
    "lease_rate_factor": ("lease rate factor",),
    "lease_collection_rate": ("lease collection rate", "cash collections"),
    "weighted_average_lease_term_remaining": ("weighted average remaining lease term",),
    "owned_or_managed_aircraft_count": (
        "owned aircraft",
        "managed aircraft",
    ),
    "aircraft_fleet_age": ("average aircraft age", "average fleet age"),
    "maintenance_service_event_growth": (
        "shop visit growth",
        "maintenance event growth",
        "engine event growth",
    ),
    "aviation_maintenance_intensity": (
        "maintenance expense ratio",
        "maintenance reserves",
    ),
    "contracted_aviation_backlog": (
        "aviation services backlog",
        "contracted backlog",
    ),
    "completion_factor": ("completion factor",),
    "on_time_performance": ("on-time performance",),
    "aircraft_orderbook_commitments": (
        "firm aircraft orders",
        "aircraft purchase commitments",
    ),
    "vessel_count": ("vessel count", "fleet consisted of"),
    "fleet_capacity": (
        "fleet capacity",
        "deadweight tonnage",
        "TEU capacity",
        "cubic meter capacity",
    ),
    "tce_day_rate": (
        "time charter equivalent rate",
        "TCE rate",
        "TCE per day",
    ),
    "spot_or_charter_day_rate": (
        "spot rate per day",
        "charter rate per day",
    ),
    "fleet_utilization": ("fleet utilization", "commercial utilization"),
    "charter_coverage_next_12m": (
        "charter coverage",
        "contracted coverage",
        "fixed for the next twelve months",
    ),
    "contracted_revenue_backlog": (
        "contracted revenue backlog",
        "minimum contracted revenue",
    ),
    "weighted_average_charter_term": (
        "weighted average charter term",
        "remaining charter duration",
    ),
    "fleet_age": ("average fleet age",),
    "newbuild_capacity_commitments": (
        "newbuild commitments",
        "newbuilding program",
    ),
    "capex_commitments": (
        "capital expenditure commitments",
        "remaining committed payments",
    ),
    "vessel_opex_per_day": (
        "vessel operating expenses per day",
        "opex per day",
    ),
    "cash_breakeven_per_day": ("cash breakeven per day",),
    "offhire_or_drydock_ratio": (
        "off-hire days",
        "drydock days",
        "offhire ratio",
    ),
    "revenue_days": ("revenue days", "operating days"),
    "spot_exposure_ratio": ("spot exposure", "spot market exposure"),
    "going_concern_flag": (
        "substantial doubt about our ability to continue as a going concern",
        "substantial doubt exists about the company's ability to continue",
    ),
    "commercialization_stage": ("commercialization stage",),
    "regulatory_certification_stage": (
        "type certification stage",
        "regulatory certification stage",
    ),
    "test_program_progress": (
        "flight test progress",
        "test program progress",
    ),
    "binding_order_units": ("firm orders", "binding orders"),
    "binding_order_value": ("firm order value", "binding order value"),
    "nonbinding_reservation_units": (
        "nonbinding reservations",
        "letters of intent",
    ),
    "customer_deposits": ("customer deposits",),
    "units_produced": ("units produced", "production units"),
    "units_delivered": ("units delivered", "customer deliveries"),
    "production_capacity": ("production capacity", "installed capacity"),
}

_NONISSUER_SCOPE = re.compile(
    r"\b(?:industry|market|peer|competitor|regional|region|global|worldwide|"
    r"transaction target|acquisition target|pro forma|customer fleet|"
    r"third[- ]party fleet)\b",
    re.IGNORECASE,
)
_ISSUER_SCOPE = re.compile(
    r"\b(?:our|we|the company(?:'s)?|consolidated|issuer)\b",
    re.IGNORECASE,
)
_DATE = re.compile(
    r"\b(?P<iso>20\d{2}-\d{2}-\d{2})\b|"
    r"\b(?P<month>January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+"
    r"(?P<day>\d{1,2}),\s+(?P<year>20\d{2})\b",
    re.IGNORECASE,
)
_NUMBER = re.compile(
    r"(?P<currency>US\$|USD|CA\$|CAD|AU\$|AUD|EUR|GBP|\$|€|£)?\s*"
    r"(?P<open>\()?(?P<value>-?\d[\d,]*(?:\.\d+)?)"
    r"(?P<close>\))?\s*"
    r"(?P<scale>billions?|millions?|thousands?|bn|mm|[bmk])?\s*"
    r"(?P<percent>%|percent(?:age)?(?:\s+points?)?)?",
    re.IGNORECASE,
)


def _read_csv(path: Path) -> tuple[dict[str, str], ...]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return tuple(
            {str(key): str(value or "").strip() for key, value in row.items()} for row in csv.DictReader(handle)
        )


def _pipe(value: object) -> tuple[str, ...]:
    return tuple(item.strip() for item in str(value or "").split("|") if item.strip())


def _merge_pipe(*values: object) -> str:
    return "|".join(
        dict.fromkeys(item for value in values for item in _pipe(value))
    )


@lru_cache(maxsize=1)
def _extraction_overrides() -> dict[tuple[str, str], dict[str, str]]:
    return {
        (row["cohort"], row["metric_id"]): row
        for row in _read_csv(_EXTRACTION_OVERRIDES)
    }


def _source_map_with_overrides(
    rows: tuple[dict[str, str], ...],
    *,
    cohort: str,
) -> dict[str, dict[str, str]]:
    output: dict[str, dict[str, str]] = {}
    for source in rows:
        row = dict(source)
        override = _extraction_overrides().get((cohort, row["metric_id"]), {})
        row["parser_aliases"] = _merge_pipe(
            row.get("parser_aliases", ""), override.get("extra_aliases", "")
        )
        if override.get("extra_event_sections"):
            row["source_sections_event"] = _merge_pipe(
                row.get("source_sections_event", ""),
                override.get("extra_event_sections", ""),
            )
        row["definition_basis"] = override.get("definition_basis", "")
        row["comparability_class"] = override.get("comparability_class", "")
        output[row["metric_id"]] = row
    return output


@lru_cache(maxsize=1)
def _final_metrics() -> tuple[dict[str, str], ...]:
    return _read_csv(_FINAL_REGISTRY)


@lru_cache(maxsize=1)
def _supporting_metrics() -> tuple[dict[str, str], ...]:
    return _read_csv(_SUPPORT_REGISTRY)


@lru_cache(maxsize=1)
def _tanker_source_map() -> dict[str, dict[str, str]]:
    return _source_map_with_overrides(
        _read_csv(_TANKER_SOURCE_MAP), cohort="tanker"
    )


@lru_cache(maxsize=1)
def _tanker_xbrl_map() -> dict[tuple[str, str, str], dict[str, str]]:
    return {
        (
            row["ticker"].upper(),
            row["metric_id"],
            row["concept_name"].casefold(),
        ): row
        for row in _read_csv(_TANKER_XBRL_MAP)
    }


@lru_cache(maxsize=1)
def _tanker_xbrl_concepts_by_metric() -> dict[str, tuple[str, ...]]:
    concepts: defaultdict[str, set[str]] = defaultdict(set)
    for row in _tanker_xbrl_map().values():
        concepts[row["metric_id"]].add(row["concept_name"])
    return {
        metric_id: tuple(sorted(values))
        for metric_id, values in concepts.items()
    }


@lru_cache(maxsize=1)
def _tanker_filing_profiles() -> dict[str, dict[str, str]]:
    return {row["ticker"].upper(): row for row in _read_csv(_TANKER_FILING_PROFILES)}


@lru_cache(maxsize=1)
def _surface_source_map() -> dict[str, dict[str, str]]:
    return _source_map_with_overrides(
        _read_csv(_SURFACE_SOURCE_MAP), cohort="surface"
    )


@lru_cache(maxsize=1)
def _metric_derivation_contracts() -> tuple[dict[str, str], ...]:
    return _read_csv(_DERIVATION_CONTRACTS)


@lru_cache(maxsize=1)
def _surface_derivation_contracts() -> tuple[dict[str, str], ...]:
    return tuple(
        row
        for row in _metric_derivation_contracts()
        if row.get("cohort") == "surface"
    )


@lru_cache(maxsize=1)
def _issuer_aliases() -> dict[str, dict[str, str]]:
    return {
        row["ticker"].upper(): row
        for row in _read_csv(_ISSUER_ALIASES)
    }


@lru_cache(maxsize=1)
def _surface_xbrl_rules() -> dict[str, tuple[dict[str, str], ...]]:
    output: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    for row in _read_csv(_SURFACE_XBRL_OPERAND_MAP):
        output[row["metric_id"]].append(row)
    return {
        metric_id: tuple(
            sorted(
                rows,
                key=lambda row: (
                    int(row.get("priority") or 99),
                    row.get("operand_role") or "",
                    row.get("concept_pattern") or "",
                ),
            )
        )
        for metric_id, rows in output.items()
    }


@lru_cache(maxsize=1)
def _surface_filing_profiles() -> dict[str, dict[str, str]]:
    return {row["ticker"].upper(): row for row in _read_csv(_SURFACE_FILING_PROFILES)}


@lru_cache(maxsize=1)
def _metric_contracts() -> dict[str, dict[str, str]]:
    output: dict[str, dict[str, str]] = {}
    tanker_source_map = _tanker_source_map()
    surface_source_map = _surface_source_map()
    for row in _final_metrics():
        if row["source_lane"] == "DP":
            metric_id = row["metric_id"]
            source_aliases = _pipe(
                tanker_source_map.get(metric_id, {}).get("parser_aliases", "")
            )
            surface_aliases = _pipe(
                surface_source_map.get(metric_id, {}).get("parser_aliases", "")
            )
            aliases = tuple(
                dict.fromkeys(
                    (metric_id.replace("_", " "),)
                    + _ALIASES.get(metric_id, ())
                    + source_aliases
                    + surface_aliases
                )
            )
            output[metric_id] = {
                **row,
                "source_lane": "DP",
                "search_aliases": "|".join(aliases),
            }
    for row in _supporting_metrics():
        output[row["support_metric_id"]] = {
            **row,
            "metric_id": row["support_metric_id"],
            "source_lane": "DP-S",
        }
    return output


@lru_cache(maxsize=1)
def _applicability() -> dict[tuple[str, str], dict[str, str]]:
    output: dict[tuple[str, str], dict[str, str]] = {}
    for path, metric_field in (
        (_FINAL_SCOPE, "metric_id"),
        (_SUPPORT_SCOPE, "support_metric_id"),
    ):
        for row in _read_csv(path):
            metric_id = row[metric_field]
            if metric_id in _metric_contracts():
                output[(row["ticker"].upper(), metric_id)] = row
    return output


@lru_cache(maxsize=1)
def _v3_tanker_applicability() -> dict[str, frozenset[str]]:
    from industrials.transportation.investable_universe import (
        load_investable_universe_policy,
    )

    policy = load_investable_universe_policy(_INVESTABLE_V3_POLICY)
    metrics = frozenset(policy.direct_tanker_metrics)
    return {ticker.upper(): metrics for ticker in policy.tanker_tickers}


@lru_cache(maxsize=1)
def _v3_surface_applicability() -> dict[str, frozenset[str]]:
    output: defaultdict[str, set[str]] = defaultdict(set)
    parser_metrics = set(_metric_contracts())
    for metric_id, row in _surface_source_map().items():
        # surface_volume_growth is a DP-D downstream derivation, not a
        # direct parser target, so it is absent from parser_metrics.
        if metric_id not in parser_metrics:
            continue
        for ticker in _pipe(row.get("applicable_tickers", "")):
            output[ticker.upper()].add(metric_id)
    return {
        ticker: frozenset(sorted(metrics))
        for ticker, metrics in output.items()
    }


def applicable_parser_metrics(ticker: str) -> frozenset[str]:
    symbol = ticker.strip().upper()
    baseline = {
        metric_id
        for (row_ticker, metric_id), row in _applicability().items()
        if row_ticker == symbol and row["applicability_status"] == "APPLICABLE"
    }
    if symbol in _surface_filing_profiles():
        # The v3 source map is authoritative for the redesigned 19-name
        # surface cohort. Legacy scope remains available for historical
        # issuers, but cannot leak obsolete archetype applicability into
        # current investable names.
        baseline -= set(_surface_source_map())
    return frozenset(
        baseline
        | set(_v3_tanker_applicability().get(symbol, ()))
        | set(_v3_surface_applicability().get(symbol, ()))
    )


def select_tickers(conn: sqlite3.Connection, asof_date: str) -> list[str]:
    return [
        str(row["ticker"])
        for row in conn.execute(
            """
            SELECT DISTINCT ticker
            FROM dim_universe_membership
            WHERE model_family = 'transportation'
              AND start_date <= ?
            ORDER BY ticker
            """,
            (asof_date,),
        )
    ]


def _alias_pattern(alias: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9]+", alias)
    if len(tokens) == 1 and len(tokens[0]) <= 4:
        return rf"\b{re.escape(tokens[0])}\b"
    return r"\b" + r"[\s/_-]*".join(map(re.escape, tokens)) + r"\b"


@lru_cache(maxsize=None)
def _text_patterns(metric_id: str) -> tuple[re.Pattern[str], ...]:
    aliases = _pipe(_metric_contracts()[metric_id]["search_aliases"])
    return tuple(re.compile(_alias_pattern(alias), re.IGNORECASE) for alias in dict.fromkeys(aliases))


def _concept_patterns(metric_id: str) -> tuple[str, ...]:
    aliases = _pipe(_metric_contracts()[metric_id]["search_aliases"])
    output: list[str] = []
    for alias in aliases:
        tokens = re.findall(r"[A-Za-z0-9]+", alias)
        if not tokens:
            continue
        output.append("(?i)" + ".*".join(re.escape(token) for token in tokens))
    for concept_name in _tanker_xbrl_concepts_by_metric().get(metric_id, ()):
        output.append(
            r"(?i)(?:^|[^A-Za-z0-9])"
            + re.escape(concept_name)
            + r"(?=$|[^A-Za-z0-9])"
        )
    for rule in _surface_xbrl_rules().get(metric_id, ()):
        pattern = str(rule.get("concept_pattern") or "").strip()
        if pattern:
            output.append(pattern)
            if pattern.startswith("(?i)^"):
                # Provider qnames may be returned as us-gaap:Concept or
                # {namespace}Concept even though persisted concept_name is
                # normalized to the local name. Request both forms.
                output.append("(?i)(?:^|[:}])" + pattern[len("(?i)^"):])
    for contract in _metric_derivation_contracts():
        if contract.get("metric_id") != metric_id:
            continue
        for field in (
            "numerator_aliases",
            "denominator_aliases",
            "alternate_numerator_aliases",
        ):
            for alias in _pipe(contract.get(field, "")):
                tokens = re.findall(r"[A-Za-z0-9]+", alias)
                if tokens:
                    output.append(
                        "(?i)" + ".*".join(re.escape(token) for token in tokens)
                    )
    return tuple(dict.fromkeys(output))


def metric_search_aliases() -> dict[str, tuple[str, ...]]:
    """Expose the frozen discovery aliases for read-only source screening."""
    return {metric_id: _pipe(contract["search_aliases"]) for metric_id, contract in sorted(_metric_contracts().items())}


def get_registry() -> AdapterRegistry:
    contracts = _metric_contracts()
    source = tuple(
        MetricRequest(metric_id, _concept_patterns(metric_id))
        for metric_id in sorted(metric_id for metric_id, row in contracts.items() if row["source_lane"] == "DP")
    )
    supporting = tuple(
        MetricRequest(metric_id, _concept_patterns(metric_id))
        for metric_id in sorted(metric_id for metric_id, row in contracts.items() if row["source_lane"] == "DP-S")
    )
    return AdapterRegistry(
        model_family="transportation",
        adapter_version=ADAPTER_VERSION,
        supported_forms=_SUPPORTED_FORMS,
        source_metrics=source,
        supporting_metrics=supporting,
        metric_dependencies={
            metric_id: metric_id for metric_id in contracts
        },
        metric_requirements={metric_id: MetricRequirement(metric_id) for metric_id in contracts},
        metric_freshness_days={metric_id: int(row["max_staleness_days"]) for metric_id, row in contracts.items()},
        production_mappings={},
        document_keywords=(
            "aircraft",
            "airport",
            "capacity",
            "carrying capacity",
            "carload",
            "car miles per day",
            "car velocity",
            "cash break-even",
            "cash breakeven",
            "contracted services",
            "certification",
            "charter",
            "commitments",
            "deadweight",
            "dry-docking",
            "driver turnover",
            "earning days",
            "empty miles",
            "fleet",
            "fuel",
            "going concern",
            "gross ton miles",
            "locomotive fuel consumed",
            "intermodal units",
            "lease",
            "load factor",
            "non-GAAP",
            "off-hire",
            "operating days",
            "operating statistics",
            "operating ratio",
            "orders",
            "passenger",
            "purchased transportation",
            "purchased transportation and warehousing",
            "pounds per day",
            "rail",
            "railroad performance",
            "revenue per",
            "revenue days",
            "shipment",
            "shipments per day",
            "terminal dwell",
            "TCE",
            "traffic",
            "train velocity",
            "percentage covered",
            "utilization",
            "vessel",
        ),
        review_policy_path=str(_REVIEW_POLICY),
        review_policy_golden_path=str(_REVIEW_POLICY_GOLDEN),
    )


def _scope_from_text(text: str) -> str:
    if _NONISSUER_SCOPE.search(text):
        return "nonissuer"
    if _ISSUER_SCOPE.search(text):
        return "consolidated"
    return "unknown"


def _date_value(text: str) -> str:
    match = _DATE.search(text)
    if match is None:
        return ""
    if match.group("iso"):
        return str(match.group("iso"))
    from datetime import datetime

    try:
        return (
            datetime.strptime(
                f"{match.group('month')} {match.group('day')}, {match.group('year')}",
                "%B %d, %Y",
            )
            .date()
            .isoformat()
        )
    except ValueError:
        return ""


def _numeric_match(
    text: str,
    *,
    label: re.Match[str],
) -> re.Match[str] | None:
    after = text[label.end() : label.end() + 220]
    match = _NUMBER.search(after)
    if match is not None:
        return match
    before_start = max(0, label.start() - 100)
    before = text[before_start : label.start()]
    matches = list(_NUMBER.finditer(before))
    return matches[-1] if matches else None


def _scale(raw: str) -> float:
    value = raw.lower()
    if value in {"billion", "billions", "bn", "b"}:
        return 1_000_000_000.0
    if value in {"million", "millions", "mm", "m"}:
        return 1_000_000.0
    if value in {"thousand", "thousands", "k"}:
        return 1_000.0
    return 1.0


def _normalized_number(
    *,
    metric_id: str,
    text: str,
    match: re.Match[str],
    company_currency: str,
) -> tuple[float | None, str, dict[str, Any]]:
    contract = _metric_contracts()[metric_id]
    unit_contract = contract["unit_contract"]
    try:
        value = float(match.group("value").replace(",", ""))
    except ValueError:
        return None, unit_contract, {}
    if match.group("open") and match.group("close"):
        value = -abs(value)
    value *= _scale(str(match.group("scale") or ""))
    percent = bool(match.group("percent"))
    context = text[max(0, match.start() - 100) : match.end() + 30].lower()
    if unit_contract == "boolean":
        value = 1.0 if value != 0 else 0.0
        unit = "boolean"
    elif unit_contract.startswith("ratio") or unit_contract == "ratio":
        if percent:
            value /= 100.0
        if re.search(
            r"\b(?:decreased|declined|fell|lower|contracted)\b",
            context,
        ):
            value = -abs(value)
        unit = "ratio"
    elif unit_contract == "ordinal_0_5":
        unit = "ordinal_0_5"
    elif "currency" in unit_contract:
        currency = str(match.group("currency") or company_currency or "USD")
        currency = {
            "$": company_currency or "USD",
            "US$": "USD",
            "€": "EUR",
            "£": "GBP",
        }.get(currency.upper() if currency not in {"€", "£"} else currency, currency)
        unit = currency if unit_contract == "currency" else f"{currency}_{unit_contract.replace('currency_', '')}"
    elif unit_contract in {
        "count",
        "count_and_currency",
        "count_and_segment_native_capacity",
    }:
        unit = "count"
    elif unit_contract == "fuel_volume":
        unit = "gallons" if "gallon" in context else "fuel_volume"
    elif unit_contract == "capacity_units":
        unit = (
            "ASM"
            if re.search(r"\bASM", context, re.IGNORECASE)
            else "ASK"
            if re.search(r"\bASK", context, re.IGNORECASE)
            else "capacity_units"
        )
    else:
        unit = unit_contract
    return (
        value,
        unit,
        {
            "raw_scale": str(match.group("scale") or ""),
            "raw_percent": str(match.group("percent") or ""),
            "unit_contract": unit_contract,
        },
    )


def _metric_source_context(
    item: WorkItem,
    metric_id: str,
    *,
    text: str = "",
) -> dict[str, Any]:
    surface_source = _surface_source_map().get(metric_id)
    is_surface = surface_source is not None
    source = surface_source or _tanker_source_map().get(metric_id, {})
    form_type = item.filing.form_type.upper()
    if form_type.startswith("10-K"):
        section_field = "source_sections_10k"
    elif form_type.startswith(("20-F", "40-F")):
        section_field = "source_sections_foreign" if is_surface else "source_sections_20f"
    elif is_surface:
        section_field = "source_sections_event"
    else:
        section_field = "source_sections_event"
    preferred_sections = _pipe(source.get(section_field, "")) if section_field else ()
    normalized_text = re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()
    section_match = any(
        re.sub(r"[^a-z0-9]+", " ", section.casefold()).strip()
        in normalized_text
        for section in preferred_sections
    )
    profiles = _surface_filing_profiles() if is_surface else _tanker_filing_profiles()
    profile = profiles.get(item.filing.ticker.upper(), {})
    expected_form = profile.get("annual_form", "")
    is_annual = form_type in {
        "10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"
    }
    return {
        "metric_source_profile": "surface_freight_v3" if is_surface else "tanker_v2",
        "accounting_framework": profile.get("accounting_framework", ""),
        "expected_annual_form": expected_form,
        "filing_form_type": item.filing.form_type,
        "form_profile_match": bool(
            is_annual and expected_form and form_type.startswith(expected_form)
        ),
        "preferred_sections": list(preferred_sections),
        "preferred_section_match": section_match,
        "expected_tables": list(_pipe(source.get("expected_tables", ""))),
        "source_posture": source.get("source_posture", ""),
        "definition_basis": source.get("definition_basis", ""),
        "comparability_class": source.get("comparability_class", ""),
        "issuer_aliases": list(
            _pipe(
                _issuer_aliases()
                .get(item.filing.ticker.upper(), {})
                .get("issuer_aliases", "")
            )
        ),
    }


def _block_evidence(
    item: WorkItem,
    block: SemanticBlock,
    *,
    metric_ids: Iterable[str],
    source_document: str,
    document_sha256: str,
    extraction_method: str,
    extraction_warning: str,
    extraction_cache_status: str,
) -> list[MetricEvidence]:
    text = block.search_text
    output: list[MetricEvidence] = []
    for metric_id in metric_ids:
        label = next(
            (match for pattern in _text_patterns(metric_id) if (match := pattern.search(text)) is not None),
            None,
        )
        if label is None:
            continue
        source_context = _metric_source_context(item, metric_id, text=text)
        target_date = _date_value(text) if metric_id == "milestone_target_date" else ""
        number = _numeric_match(text, label=label)
        if metric_id == "going_concern_flag":
            value, unit, numeric_provenance = 1.0, "boolean", {"unit_contract": "boolean"}
        elif target_date:
            value, unit, numeric_provenance = (
                None,
                "iso_date",
                {
                    "target_date": target_date,
                    "unit_contract": "iso_date",
                },
            )
        elif number is None:
            value, unit, numeric_provenance = (
                None,
                _metric_contracts()[metric_id]["unit_contract"],
                {},
            )
        else:
            value, unit, numeric_provenance = _normalized_number(
                metric_id=metric_id,
                text=text,
                match=number,
                company_currency=item.filing.company_currency,
            )
        scope = _scope_from_text(text)
        output.append(
            MetricEvidence(
                metric_name=metric_id,
                concept_name=("TransportationDiscovery" + "".join(part.title() for part in metric_id.split("_"))),
                value=value,
                unit=unit,
                period_start="",
                period_end=item.filing.report_date[:10],
                scope=scope,
                confidence=(
                    0.72
                    if source_context["preferred_section_match"]
                    and (value is not None or target_date)
                    else 0.65
                    if value is not None or target_date
                    else 0.45
                ),
                status=("REJECTED_POLICY" if scope == "nonissuer" else "REVIEW_REQUIRED"),
                reason=(
                    "nonissuer_or_proforma_scope"
                    if scope == "nonissuer"
                    else "broad_discovery_candidate_requires_metric_fixture_review"
                ),
                evidence_text=text[:2000],
                source_document=source_document,
                extraction_method=("dedicated_parser:transportation_semantic_discovery"),
                provenance={
                    "adapter_version": ADAPTER_VERSION,
                    "source_lane": _metric_contracts()[metric_id]["source_lane"],
                    "document_sha256": document_sha256,
                    "document_extraction_method": extraction_method,
                    "document_extraction_warning": extraction_warning,
                    "document_extraction_cache_status": (extraction_cache_status),
                    "semantic_block_index": block.index,
                    "semantic_block_kind": block.kind,
                    "semantic_table_id": block.table_id,
                    "semantic_row_index": block.row_index,
                    "semantic_section_path": list(block.section_path),
                    **source_context,
                    **numeric_provenance,
                },
            )
        )
    return output


def extract_metric_evidence(item: WorkItem) -> tuple[MetricEvidence, ...]:
    requested = {request.metric_name for request in item.requested_metrics}
    metric_ids = sorted(requested & applicable_parser_metrics(item.filing.ticker))
    if not metric_ids:
        return ()
    output: list[MetricEvidence] = []
    for document in item.documents:
        if document.is_full_submission:
            continue
        try:
            if document.source_kind == "transportation_non_sec_primary_document":
                extracted, extraction_cache_status = extract_document_once(
                    document,
                    options=ExtractionOptions(
                        enable_pdf_ocr=item.enable_pdf_ocr,
                        max_pdf_pages=item.max_pdf_pages,
                        max_pdf_bytes=item.max_pdf_bytes,
                        pdf_extraction_timeout_seconds=(item.pdf_extraction_timeout_seconds),
                    ),
                )
            else:
                extracted = extract_document_text(
                    Path(document.path).read_bytes(),
                    document_name=document.name,
                    enable_pdf_ocr=item.enable_pdf_ocr,
                    max_pdf_pages=item.max_pdf_pages,
                    max_pdf_bytes=item.max_pdf_bytes,
                    pdf_extraction_timeout_sec=(item.pdf_extraction_timeout_seconds),
                )
                extraction_cache_status = "NOT_APPLICABLE"
        except OSError as exc:
            output.extend(
                MetricEvidence(
                    metric_name=metric_id,
                    concept_name="DocumentReadFailure",
                    value=None,
                    unit="",
                    period_start="",
                    period_end=item.filing.report_date[:10],
                    scope="unknown",
                    confidence=0.0,
                    status="PARSER_FAILURE",
                    reason=f"document_read_failed:{type(exc).__name__}",
                    evidence_text=str(exc)[:500],
                    source_document=document.name,
                    extraction_method="dedicated_parser:document_read",
                    provenance={"adapter_version": ADAPTER_VERSION},
                )
                for metric_id in metric_ids
            )
            continue
        if not extracted.text.strip():
            if extracted.warning:
                output.extend(
                    MetricEvidence(
                        metric_name=metric_id,
                        concept_name="DocumentExtractionFailure",
                        value=None,
                        unit="",
                        period_start="",
                        period_end=item.filing.report_date[:10],
                        scope="unknown",
                        confidence=0.0,
                        status="PARSER_FAILURE",
                        reason=extracted.warning,
                        evidence_text=extracted.warning,
                        source_document=document.name,
                        extraction_method=(f"dedicated_parser:{extracted.extraction_method}"),
                        provenance={"adapter_version": ADAPTER_VERSION},
                    )
                    for metric_id in metric_ids
                )
            continue
        semantic = parse_semantic_document(
            extracted.text,
            source_document=document.name,
        )
        per_metric_count: defaultdict[str, int] = defaultdict(int)
        generic_metric_ids = set(metric_ids)
        for block in semantic.blocks:
            # Preserve broad prose discovery (the established DP contract)
            # while assigning semantic table rows exclusively to the
            # strict surface extractor to avoid duplicate/conflicting rows.
            block_metric_ids = (
                generic_metric_ids - set(_surface_source_map())
                if block.kind == "table_row"
                else generic_metric_ids
            )
            for evidence in _block_evidence(
                item,
                block,
                metric_ids=sorted(block_metric_ids),
                source_document=document.name,
                document_sha256=document.content_sha256,
                extraction_method=extracted.extraction_method,
                extraction_warning=extracted.warning,
                extraction_cache_status=extraction_cache_status,
            ):
                if per_metric_count[evidence.metric_name] >= 12:
                    continue
                per_metric_count[evidence.metric_name] += 1
                output.append(evidence)
        output.extend(
            derive_tanker_table_evidence(
                item,
                semantic.blocks,
                requested_metrics=set(metric_ids),
                source_document=document.name,
                document_sha256=document.content_sha256,
            )
        )
        output.extend(
            derive_surface_table_evidence(
                item,
                semantic.blocks,
                requested_metrics=set(metric_ids),
                source_document=document.name,
                document_sha256=document.content_sha256,
                source_kind=document.source_kind,
                source_contracts=_surface_source_map(),
                derivation_contracts=_surface_derivation_contracts(),
                filing_profiles=_surface_filing_profiles(),
                document_extraction_method=extracted.extraction_method,
                document_extraction_warning=extracted.warning,
                document_extraction_cache_status=extraction_cache_status,
            )
        )
    return postprocess_metric_evidence(item, tuple(output))


def _fact_search_text(fact: NormalizedFact) -> str:
    values = [fact.taxonomy, fact.concept_name]
    try:
        metadata = json.loads(fact.concept_metadata_json or "{}")
    except json.JSONDecodeError:
        metadata = {}
    if isinstance(metadata, Mapping):
        for value in metadata.values():
            if isinstance(value, str):
                values.append(value)
            elif isinstance(value, list):
                values.extend(str(item) for item in value)
    return " ".join(values)


def _currency_from_xbrl_unit(raw_unit: str, company_currency: str) -> str:
    match = re.search(r"\b([A-Z]{3})\b", raw_unit.upper())
    return match.group(1) if match is not None else company_currency.upper() or "USD"


def _normalized_xbrl_value_and_unit(
    item: WorkItem,
    metric_id: str,
    fact: NormalizedFact,
    exact_rule: Mapping[str, str] | None,
) -> tuple[float, str, dict[str, Any]]:
    value = float(fact.numeric_value)
    raw_unit = str(fact.unit or "").strip()
    unit_lower = raw_unit.casefold()
    contract = _metric_contracts()[metric_id]["unit_contract"]
    configured = str((exact_rule or {}).get("normalized_unit") or "")
    recognized = False
    unit = raw_unit

    if configured == "count" or (
        contract == "count"
        and unit_lower in {"item", "items", "number", "pure", "ship", "ships", "vessel", "vessels"}
    ):
        unit, recognized = "count", True
    elif configured == "segment_native_capacity" or (
        contract == "segment_native_capacity"
        and unit_lower in {"dwt", "t", "ton", "tons", "tonne", "tonnes", "mt", "teu", "cbm"}
    ):
        unit, recognized = "segment_native_capacity", True
    elif configured in {"years", "days", "ratio"}:
        unit, recognized = configured, True
    elif configured == "currency":
        unit, recognized = _currency_from_xbrl_unit(raw_unit, item.filing.company_currency), True
    elif contract == "years" and (
        unit_lower in {"year", "years", "yr", "yrs", "pure"}
        or "age" in fact.concept_name.casefold()
        or "term" in fact.concept_name.casefold()
    ):
        unit, recognized = "years", True
    elif contract == "days" and (
        unit_lower in {"day", "days", "item", "items", "pure"}
        or "day" in fact.concept_name.casefold()
    ):
        unit, recognized = "days", True
    elif contract == "ratio" and unit_lower in {"pure", "ratio", "%", "percent"}:
        if unit_lower in {"%", "percent"} and abs(value) > 1.0:
            value /= 100.0
        unit, recognized = "ratio", True
    elif contract == "currency" and re.search(r"[A-Z]{3}", raw_unit.upper()):
        unit, recognized = _currency_from_xbrl_unit(raw_unit, item.filing.company_currency), True
    elif contract == "currency_per_day" and (
        "day" in unit_lower
        or "daily" in fact.concept_name.casefold()
        or "perday" in fact.concept_name.casefold()
        or "rate" in fact.concept_name.casefold()
    ):
        currency = _currency_from_xbrl_unit(raw_unit, item.filing.company_currency)
        unit, recognized = f"{currency}_per_day", True
    elif contract == "count_and_segment_native_capacity":
        if unit_lower in {"item", "items", "number", "pure", "ship", "ships", "vessel", "vessels"}:
            unit, recognized = "count", True
        elif unit_lower in {"dwt", "t", "ton", "tons", "tonne", "tonnes", "mt", "teu", "cbm"}:
            unit, recognized = "segment_native_capacity", True

    provenance: dict[str, Any] = {
        "raw_xbrl_unit": raw_unit,
        "xbrl_unit_normalized": recognized,
    }
    if recognized:
        provenance["unit_contract"] = contract
    return value, unit, provenance


def map_normalized_facts(
    item: WorkItem,
    facts: tuple[NormalizedFact, ...],
) -> tuple[MetricEvidence, ...]:
    requested = {request.metric_name for request in item.requested_metrics}
    applicable = requested & applicable_parser_metrics(item.filing.ticker)
    registry = get_registry()
    patterns: dict[str, tuple[re.Pattern[str], ...]] = {}
    for metric_id in applicable:
        request = registry.request(metric_id)
        concept_patterns = request.concept_patterns if request is not None else ()
        patterns[metric_id] = tuple(re.compile(pattern) for pattern in concept_patterns)
    output: list[MetricEvidence] = []
    annual_form = item.filing.form_type.upper() in {
        "10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"
    }
    ticker = item.filing.ticker.upper()
    for fact in facts:
        if fact.numeric_value is None:
            continue
        search_text = _fact_search_text(fact)
        concept_key = fact.concept_name.casefold()
        for metric_id, metric_patterns in patterns.items():
            if not any(pattern.search(search_text) for pattern in metric_patterns):
                continue
            exact_rule = _tanker_xbrl_map().get((ticker, metric_id, concept_key))
            surface_rule = surface_fact_rule(
                metric_id, fact.concept_name, _surface_xbrl_rules()
            )
            if surface_rule and surface_rule.get("operand_role") != "direct_value":
                # Operands are inputs to a same-period derivation; emitting
                # them as the requested ratio would be semantically wrong.
                continue
            unit_rule = exact_rule or surface_rule
            value, unit, unit_provenance = _normalized_xbrl_value_and_unit(
                item,
                metric_id,
                fact,
                unit_rule,
            )
            exact_accept = bool(
                exact_rule
                and exact_rule["acceptance_posture"] == "ACCEPT_IF_CONSOLIDATED_ANNUAL"
                and fact.scope == "consolidated"
                and annual_form
            )
            if fact.scope != "consolidated":
                status = "REJECTED_POLICY"
                reason = "dimensional_or_segment_fact_not_consolidated"
                confidence = 0.99
            elif exact_accept:
                status = "ACCEPTED"
                reason = "audited_ixbrl_exact_tanker_concept"
                confidence = 0.98
            elif surface_rule:
                status = "REVIEW_REQUIRED"
                reason = "surface_direct_xbrl_value_requires_definition_review"
                confidence = 0.90
            else:
                status = "REVIEW_REQUIRED"
                reason = "normalized_fact_requires_transportation_semantic_review"
                confidence = 0.72
            source_context = _metric_source_context(item, metric_id)
            output.append(
                MetricEvidence(
                    metric_name=metric_id,
                    concept_name=fact.concept_name,
                    value=value,
                    unit=unit,
                    period_start=fact.period_start,
                    period_end=fact.period_end,
                    scope=fact.scope,
                    confidence=confidence,
                    status=status,
                    reason=reason,
                    evidence_text=(
                        f"{fact.taxonomy}:{fact.concept_name}={fact.numeric_value:g} "
                        f"{fact.unit}"
                    ),
                    source_document=fact.source_document,
                    extraction_method=f"dedicated_parser:{fact.provider}:normalized_fact",
                    provenance={
                        "adapter_version": ADAPTER_VERSION,
                        "source_lane": _metric_contracts()[metric_id]["source_lane"],
                        "context_id": fact.context_id,
                        "dimensions_json": fact.dimensions_json,
                        "exact_tanker_concept_rule": bool(exact_rule),
                        "surface_xbrl_rule": bool(surface_rule),
                        "surface_xbrl_operand_role": (
                            surface_rule.get("operand_role", "") if surface_rule else ""
                        ),
                        **source_context,
                        **unit_provenance,
                    },
                )
            )
    output.extend(
        derive_surface_xbrl_evidence(
            item,
            facts,
            requested_metrics=set(applicable),
            rules_by_metric=_surface_xbrl_rules(),
        )
    )
    return postprocess_metric_evidence(item, tuple(output))


def _bounds_error(metric_id: str, value: float | None) -> str:
    if value is None:
        return ""
    if not math.isfinite(value):
        return "nonfinite_value"
    policy = _metric_contracts()[metric_id]["bounds_policy"]
    if policy == "boolean" and value not in {0.0, 1.0}:
        return "boolean_value_out_of_bounds"
    if policy == "ordinal_0_5" and (value < 0.0 or value > 5.0 or not value.is_integer()):
        return "ordinal_value_out_of_bounds"
    if policy == "nonnegative" and value < 0.0:
        return "negative_value_prohibited"
    if policy == "nonnegative_integer" and (value < 0.0 or not value.is_integer()):
        return "nonnegative_integer_required"
    if policy == "growth_ratio" and not -10.0 <= value <= 10.0:
        return "growth_ratio_out_of_bounds"
    match = re.fullmatch(r"ratio_0_(\d+)", policy)
    if match is not None and not 0.0 <= value <= float(match.group(1)):
        return "ratio_value_out_of_bounds"
    if policy == "years_0_100" and not 0.0 <= value <= 100.0:
        return "years_value_out_of_bounds"
    return ""


def _unit_error(row: MetricEvidence) -> str:
    contract = _metric_contracts()[row.metric_name]
    expected = contract["unit_contract"].lower()
    actual = str(row.unit or "").strip().lower()
    if not actual:
        return "missing_unit"
    provenance_contract = str((row.provenance or {}).get("unit_contract") or "").lower()
    if provenance_contract == expected:
        return ""
    if expected == "ratio" and actual == "ratio":
        return ""
    if expected in {"boolean", "ordinal_0_5", "iso_date"} and actual == expected:
        return ""
    if expected == "currency" and re.fullmatch(r"[a-z]{3}", actual):
        return ""
    if expected.startswith("currency_") and re.fullmatch(
        rf"[a-z]{{3}}_{re.escape(expected.removeprefix('currency_'))}",
        actual,
    ):
        return ""
    if expected in {"count", "count_and_currency"} and (
        actual == "count" or re.fullmatch(r"[a-z]{3}(?:_.+)?", actual)
    ):
        return ""
    if expected == "segment_native_capacity" and actual == "segment_native_capacity":
        return ""
    if expected == "count_and_segment_native_capacity" and actual in {
        "count",
        "segment_native_capacity",
    }:
        return ""
    if expected == "ratio_or_count" and actual in {"ratio", "count"}:
        return ""
    if expected == "ratio_or_days" and actual in {"ratio", "days"}:
        return ""
    if expected == "currency_or_service_units" and (actual == "service_units" or re.fullmatch(r"[a-z]{3}", actual)):
        return ""
    if expected == "fuel_volume" and actual in {"fuel_volume", "gallons"}:
        return ""
    if expected == "capacity_units" and actual in {
        "capacity_units",
        "asm",
        "ask",
    }:
        return ""
    if actual == expected:
        return ""
    return "unit_contract_mismatch"


def _period_error(item: WorkItem, row: MetricEvidence) -> str:
    from datetime import date

    period_end = str(row.period_end or "")[:10]
    if not period_end:
        return "missing_period_end"
    try:
        end = date.fromisoformat(period_end)
    except ValueError:
        return "invalid_period_end"
    period_start = str(row.period_start or "")[:10]
    if period_start:
        try:
            start = date.fromisoformat(period_start)
        except ValueError:
            return "invalid_period_start"
        if start > end:
            return "period_start_after_period_end"
    period_type = _metric_contracts()[row.metric_name]["period_type"]
    if period_type not in {"forward_12m", "forward_or_fiscal_period", "milestone"}:
        availability = str(item.filing.accepted_at or item.filing.filing_date or "")[:10]
        try:
            if availability and end > date.fromisoformat(availability):
                return "period_end_after_filing_availability"
        except ValueError:
            return "invalid_filing_availability_date"
    return ""


def postprocess_metric_evidence(
    item: WorkItem,
    evidence: tuple[MetricEvidence, ...],
) -> tuple[MetricEvidence, ...]:
    requested = {request.metric_name for request in item.requested_metrics}
    applicable = applicable_parser_metrics(item.filing.ticker)
    normalized: list[MetricEvidence] = []
    for row in evidence:
        if row.metric_name not in requested:
            continue
        if row.metric_name not in _metric_contracts():
            continue
        if row.metric_name not in applicable:
            normalized.append(
                replace(
                    row,
                    status="REJECTED_POLICY",
                    confidence=0.99,
                    reason="ticker_metric_not_applicable_in_sealed_scope",
                )
            )
            continue
        if row.status == "PARSER_FAILURE":
            normalized.append(row)
            continue
        if row.scope == "nonissuer":
            normalized.append(
                replace(
                    row,
                    status="REJECTED_POLICY",
                    confidence=0.99,
                    reason="nonissuer_or_proforma_scope",
                )
            )
            continue
        if row.status == "ACCEPTED" and row.scope != "consolidated":
            normalized.append(
                replace(
                    row,
                    status="REVIEW_REQUIRED",
                    confidence=min(row.confidence, 0.75),
                    reason="explicit_issuer_scope_required_for_acceptance",
                )
            )
            continue
        unit_error = _unit_error(row)
        if unit_error:
            normalized.append(
                replace(
                    row,
                    status="REJECTED_POLICY",
                    confidence=0.99,
                    reason=unit_error,
                )
            )
            continue
        period_error = _period_error(item, row)
        if period_error:
            normalized.append(
                replace(
                    row,
                    status="REJECTED_POLICY",
                    confidence=0.99,
                    reason=period_error,
                )
            )
            continue
        bounds_error = _bounds_error(row.metric_name, row.value)
        if bounds_error:
            normalized.append(
                replace(
                    row,
                    status="REJECTED_POLICY",
                    confidence=0.99,
                    reason=bounds_error,
                )
            )
        else:
            normalized.append(row)

    conflict_groups: dict[
        tuple[str, str, str, str, str, str],
        list[MetricEvidence],
    ] = defaultdict(list)
    for row in normalized:
        conflict_groups[
            (
                row.metric_name,
                row.period_start,
                row.period_end,
                row.unit.upper(),
                row.scope,
                row.source_document,
            )
        ].append(row)
    conflict_keys = {
        key
        for key, rows in conflict_groups.items()
        if len(
            {
                f"{row.value:.12g}"
                for row in rows
                if row.value is not None and row.status in {"ACCEPTED", "REVIEW_REQUIRED"}
            }
        )
        > 1
    }
    if conflict_keys:
        normalized = [
            (
                replace(
                    row,
                    status="REVIEW_REQUIRED",
                    confidence=min(row.confidence, 0.75),
                    reason="conflicting_values_require_review",
                )
                if (
                    row.metric_name,
                    row.period_start,
                    row.period_end,
                    row.unit.upper(),
                    row.scope,
                    row.source_document,
                )
                in conflict_keys
                and row.status == "ACCEPTED"
                else row
            )
            for row in normalized
        ]

    status_rank = {
        "ACCEPTED": 4,
        "REJECTED_POLICY": 3,
        "REVIEW_REQUIRED": 2,
        "PARSER_FAILURE": 1,
    }
    grouped: dict[
        tuple[str, str, str, str, str, str, str],
        list[MetricEvidence],
    ] = defaultdict(list)
    for row in normalized:
        grouped[
            (
                row.metric_name,
                row.period_start,
                row.period_end,
                row.unit.upper(),
                row.scope,
                row.source_document,
                "" if row.value is None else f"{row.value:.12g}",
            )
        ].append(row)
    winners = [
        max(
            rows,
            key=lambda row: (
                status_rank.get(row.status, 0),
                row.confidence,
                row.concept_name,
                row.evidence_text,
            ),
        )
        for rows in grouped.values()
    ]
    return tuple(
        sorted(
            winners,
            key=lambda row: (
                row.metric_name,
                row.period_end,
                row.period_start,
                row.value if row.value is not None else float("-inf"),
                row.status,
                row.source_document,
            ),
        )
    )

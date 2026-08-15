from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from industrials.core.config import load_yaml


POLICY_VERSION = "transportation_investable_universe_v3"
LATEST_POLICY_VERSION = "transportation_investable_universe_v4"
SUPPORTED_POLICY_GROUP_IDS = {
    POLICY_VERSION: (
        "surface_freight_core",
        "passenger_airlines",
        "oil_tanker_operators",
    ),
    LATEST_POLICY_VERSION: (
        "surface_freight_core",
        "oil_tanker_operators",
    ),
}
SURFACE_DOMAIN_POLICY_VERSION = "transportation_surface_metric_domains_v1"
SURFACE_DOMAIN_IDS = (
    "rail_networks",
    "ltl_carriers",
    "truckload_intermodal",
    "asset_light_logistics",
    "integrated_parcel",
)
GROUP_IDS = SUPPORTED_POLICY_GROUP_IDS[POLICY_VERSION]


def _flag(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        return [
            {str(key): str(value or "").strip() for key, value in row.items()}
            for row in reader
        ]


def _resolve(path: Path, raw: object) -> Path:
    value = Path(str(raw or "").strip()).expanduser()
    return value.resolve() if value.is_absolute() else (path.parent / value).resolve()


@dataclass(frozen=True)
class InvestableGroup:
    group_id: str
    calibration_pool: str
    metric_pack: str
    economic_driver: str
    minimum_specialized_breadth: int
    tickers: tuple[str, ...]


@dataclass(frozen=True)
class SurfaceComparisonDomain:
    domain_id: str
    tickers: tuple[str, ...]


@dataclass(frozen=True)
class SurfaceMetricDomainRule:
    metric_id: str
    comparison_domain_id: str
    applicable_tickers: tuple[str, ...]
    minimum_accepted_fraction: float
    minimum_accepted_breadth: int
    calibration_eligibility: str
    normalization_scope: str
    notes: str

    @property
    def is_calibration_candidate(self) -> bool:
        return self.calibration_eligibility == "CANDIDATE"


@dataclass(frozen=True)
class InvestableUniversePolicy:
    path: Path
    policy_version: str
    effective_from: str
    catalog_path: Path
    implementation_catalog_path: Path
    exclusions_path: Path
    positioning_universe_path: Path
    expected_catalog_count: int
    expected_selected_count: int
    groups: tuple[InvestableGroup, ...]
    direct_tanker_metrics: tuple[str, ...]
    derived_tanker_metrics: tuple[str, ...]
    new_tanker_tickers: tuple[str, ...]
    existing_tanker_tickers: tuple[str, ...]
    minimum_median_periods: int
    minimum_median_history_years: float
    diagnostic_minimum_breadth: int
    surface_domain_policy_version: str
    surface_metric_domain_mapping_path: Path
    surface_metric_source_map_path: Path
    surface_minimum_accepted_fraction: float
    surface_minimum_absolute_breadth: int
    surface_minimum_calibratable_domain_size: int
    surface_normalization_scope: str
    surface_comparison_domains: tuple[SurfaceComparisonDomain, ...]
    surface_metric_domain_rules: tuple[SurfaceMetricDomainRule, ...]

    @property
    def selected_tickers(self) -> tuple[str, ...]:
        return tuple(ticker for group in self.groups for ticker in group.tickers)

    @property
    def group_by_ticker(self) -> dict[str, InvestableGroup]:
        return {
            ticker: group
            for group in self.groups
            for ticker in group.tickers
        }

    @property
    def tanker_tickers(self) -> tuple[str, ...]:
        return next(
            group.tickers
            for group in self.groups
            if group.group_id == "oil_tanker_operators"
        )

    @property
    def surface_comparison_domain_by_id(self) -> dict[str, SurfaceComparisonDomain]:
        return {domain.domain_id: domain for domain in self.surface_comparison_domains}

    def surface_metric_rules(self, metric_id: str) -> tuple[SurfaceMetricDomainRule, ...]:
        return tuple(
            rule for rule in self.surface_metric_domain_rules if rule.metric_id == metric_id
        )


def load_investable_universe_policy(path: Path) -> InvestableUniversePolicy:
    resolved = path.expanduser().resolve()
    payload = load_yaml(resolved)
    if payload.get("model_family") != "transportation":
        raise ValueError("investable-universe policy is not transportation-scoped")
    policy_version = str(payload.get("policy_version") or "")
    expected_group_ids = SUPPORTED_POLICY_GROUP_IDS.get(policy_version)
    if expected_group_ids is None:
        raise ValueError(
            f"unsupported policy_version={policy_version!r}"
        )
    catalog = payload.get("research_catalog")
    eligibility = payload.get("production_eligibility")
    gates = payload.get("specialized_metric_gates")
    tanker = payload.get("tanker_delta")
    surface_policy = payload.get("surface_metric_comparison_policy")
    if not all(
        isinstance(item, Mapping)
        for item in (catalog, eligibility, gates, tanker, surface_policy)
    ):
        raise ValueError("policy is missing a required mapping")
    raw_groups = eligibility.get("groups")
    if not isinstance(raw_groups, Mapping) or tuple(raw_groups) != expected_group_ids:
        raise ValueError(f"groups must be ordered exactly as {expected_group_ids}")
    groups: list[InvestableGroup] = []
    for group_id, raw in raw_groups.items():
        if not isinstance(raw, Mapping):
            raise ValueError(f"{group_id}: group payload must be a mapping")
        tickers = tuple(str(item).strip().upper() for item in raw.get("tickers", ()))
        if len(tickers) != int(raw.get("expected_count") or 0):
            raise ValueError(f"{group_id}: expected_count does not match ticker list")
        if not tickers or len(set(tickers)) != len(tickers):
            raise ValueError(f"{group_id}: blank or duplicate ticker")
        groups.append(
            InvestableGroup(
                group_id=str(group_id),
                calibration_pool=str(raw.get("calibration_pool") or ""),
                metric_pack=str(raw.get("metric_pack") or ""),
                economic_driver=str(raw.get("economic_driver") or ""),
                minimum_specialized_breadth=int(
                    raw.get("minimum_specialized_breadth") or 0
                ),
                tickers=tickers,
            )
        )
    if surface_policy.get("policy_version") != SURFACE_DOMAIN_POLICY_VERSION:
        raise ValueError("unsupported surface comparison-domain policy version")
    raw_domains = surface_policy.get("comparison_domains")
    if not isinstance(raw_domains, Mapping) or tuple(raw_domains) != SURFACE_DOMAIN_IDS:
        raise ValueError(f"surface comparison domains must be ordered exactly as {SURFACE_DOMAIN_IDS}")
    surface_group_tickers = set(
        next(group.tickers for group in groups if group.group_id == "surface_freight_core")
    )
    surface_domains: list[SurfaceComparisonDomain] = []
    for domain_id, raw in raw_domains.items():
        if not isinstance(raw, Mapping):
            raise ValueError(f"{domain_id}: comparison domain must be a mapping")
        tickers = tuple(str(item).strip().upper() for item in raw.get("tickers", ()))
        if not tickers or len(set(tickers)) != len(tickers):
            raise ValueError(f"{domain_id}: blank or duplicate comparison-domain ticker")
        if not set(tickers) <= surface_group_tickers:
            raise ValueError(f"{domain_id}: ticker lies outside surface_freight_core")
        surface_domains.append(SurfaceComparisonDomain(str(domain_id), tickers))
    if set().union(*(set(domain.tickers) for domain in surface_domains)) != surface_group_tickers:
        raise ValueError("surface comparison domains do not cover surface_freight_core")

    mapping_path = _resolve(resolved, surface_policy.get("mapping_csv"))
    source_map_path = _resolve(resolved, surface_policy.get("source_map_csv"))
    minimum_fraction = float(surface_policy.get("minimum_accepted_fraction") or 0.0)
    minimum_absolute = int(surface_policy.get("minimum_absolute_breadth") or 0)
    minimum_domain_size = int(surface_policy.get("minimum_calibratable_domain_size") or 0)
    normalization_scope = str(surface_policy.get("normalization_scope") or "")
    if not (0.0 < minimum_fraction <= 1.0) or minimum_absolute < 1:
        raise ValueError("invalid surface metric-domain breadth rule")
    domain_by_id = {domain.domain_id: domain for domain in surface_domains}
    surface_rules: list[SurfaceMetricDomainRule] = []
    seen_rule_keys: set[tuple[str, str]] = set()
    mapped_tickers_by_metric: dict[str, set[str]] = {}
    for row in _read_csv(mapping_path):
        if row.get("policy_version") != SURFACE_DOMAIN_POLICY_VERSION:
            raise ValueError("surface metric-domain row has the wrong policy version")
        metric_id = row.get("metric_id", "")
        domain_id = row.get("comparison_domain_id", "")
        key = (metric_id, domain_id)
        if not metric_id or domain_id not in domain_by_id or key in seen_rule_keys:
            raise ValueError(f"invalid or duplicate surface metric-domain key={key}")
        seen_rule_keys.add(key)
        tickers = tuple(
            item.strip().upper()
            for item in row.get("applicable_tickers", "").split("|")
            if item.strip()
        )
        if not tickers or len(set(tickers)) != len(tickers):
            raise ValueError(f"{key}: blank or duplicate applicable ticker")
        if not set(tickers) <= set(domain_by_id[domain_id].tickers):
            raise ValueError(f"{key}: applicable ticker is outside comparison domain")
        row_fraction = float(row.get("minimum_accepted_fraction") or 0.0)
        row_breadth = int(row.get("minimum_accepted_breadth") or 0)
        expected_breadth = max(minimum_absolute, math.ceil(minimum_fraction * len(tickers)))
        if not math.isclose(row_fraction, minimum_fraction) or row_breadth != expected_breadth:
            raise ValueError(f"{key}: breadth rule is not policy-derived")
        eligibility_value = row.get("calibration_eligibility", "")
        if eligibility_value not in {"CANDIDATE", "DIAGNOSTIC_ONLY"}:
            raise ValueError(f"{key}: invalid calibration eligibility")
        if len(tickers) < minimum_domain_size and eligibility_value != "DIAGNOSTIC_ONLY":
            raise ValueError(f"{key}: undersized comparison set must be diagnostic only")
        if row.get("normalization_scope") != normalization_scope:
            raise ValueError(f"{key}: normalization scope differs from policy")
        mapped_tickers_by_metric.setdefault(metric_id, set()).update(tickers)
        surface_rules.append(
            SurfaceMetricDomainRule(
                metric_id=metric_id,
                comparison_domain_id=domain_id,
                applicable_tickers=tickers,
                minimum_accepted_fraction=row_fraction,
                minimum_accepted_breadth=row_breadth,
                calibration_eligibility=eligibility_value,
                normalization_scope=row.get("normalization_scope", ""),
                notes=row.get("notes", ""),
            )
        )
    source_tickers_by_metric = {
        row["metric_id"]: {
            item.strip().upper()
            for item in row.get("applicable_tickers", "").split("|")
            if item.strip()
        }
        for row in _read_csv(source_map_path)
    }
    if set(mapped_tickers_by_metric) != set(source_tickers_by_metric):
        raise ValueError("surface metric-domain map and source map have different metrics")
    for metric_id, source_tickers in source_tickers_by_metric.items():
        if mapped_tickers_by_metric[metric_id] != source_tickers:
            raise ValueError(
                f"{metric_id}: comparison-domain applicability does not match source map"
            )

    policy = InvestableUniversePolicy(
        path=resolved,
        policy_version=policy_version,
        effective_from=str(payload.get("effective_from") or ""),
        catalog_path=_resolve(resolved, catalog.get("authoritative_csv")),
        implementation_catalog_path=_resolve(
            resolved, catalog.get("implementation_csv")
        ),
        exclusions_path=_resolve(resolved, eligibility.get("exclusions_csv")),
        positioning_universe_path=_resolve(
            resolved, eligibility.get("positioning_universe_csv")
        ),
        expected_catalog_count=int(catalog.get("expected_count") or 0),
        expected_selected_count=int(eligibility.get("expected_count") or 0),
        groups=tuple(groups),
        direct_tanker_metrics=tuple(
            str(item).strip() for item in tanker.get("direct_parser_metrics", ())
        ),
        derived_tanker_metrics=tuple(
            str(item).strip() for item in tanker.get("derived_metrics", ())
        ),
        new_tanker_tickers=tuple(
            str(item).strip().upper() for item in tanker.get("newly_added_tickers", ())
        ),
        existing_tanker_tickers=tuple(
            str(item).strip().upper()
            for item in tanker.get("reusable_existing_tickers", ())
        ),
        minimum_median_periods=int(gates.get("minimum_median_periods") or 0),
        minimum_median_history_years=float(
            gates.get("minimum_median_history_years") or 0.0
        ),
        diagnostic_minimum_breadth=int(
            gates.get("diagnostic_minimum_breadth") or 0
        ),
        surface_domain_policy_version=SURFACE_DOMAIN_POLICY_VERSION,
        surface_metric_domain_mapping_path=mapping_path,
        surface_metric_source_map_path=source_map_path,
        surface_minimum_accepted_fraction=minimum_fraction,
        surface_minimum_absolute_breadth=minimum_absolute,
        surface_minimum_calibratable_domain_size=minimum_domain_size,
        surface_normalization_scope=normalization_scope,
        surface_comparison_domains=tuple(surface_domains),
        surface_metric_domain_rules=tuple(surface_rules),
    )
    selected = policy.selected_tickers
    if len(selected) != policy.expected_selected_count:
        raise ValueError("selected ticker count does not match policy")
    if len(set(selected)) != len(selected):
        raise ValueError("selected tickers overlap across groups")
    if set(policy.new_tanker_tickers) | set(policy.existing_tanker_tickers) != set(
        policy.tanker_tickers
    ):
        raise ValueError("new and reusable tanker lists do not partition tanker group")
    if set(policy.new_tanker_tickers) & set(policy.existing_tanker_tickers):
        raise ValueError("new and reusable tanker lists overlap")
    return policy


def validate_investable_universe_policy(
    policy: InvestableUniversePolicy,
) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    catalog_rows = _read_csv(policy.catalog_path)
    implementation_rows = _read_csv(policy.implementation_catalog_path)
    exclusion_rows = _read_csv(policy.exclusions_path)
    positioning_rows = _read_csv(policy.positioning_universe_path)

    def ticker_map(
        rows: Iterable[Mapping[str, str]], *, label: str
    ) -> dict[str, Mapping[str, str]]:
        result: dict[str, Mapping[str, str]] = {}
        for row in rows:
            ticker = str(row.get("ticker") or "").strip().upper()
            if not ticker:
                errors.append(f"{label}: blank ticker")
            elif ticker in result:
                errors.append(f"{label}: duplicate ticker={ticker}")
            else:
                result[ticker] = row
        return result

    catalog = ticker_map(catalog_rows, label="authoritative_catalog")
    implementation = ticker_map(
        implementation_rows, label="implementation_catalog"
    )
    exclusions = ticker_map(exclusion_rows, label="exclusions")
    positioning = ticker_map(positioning_rows, label="positioning_universe")
    selected = set(policy.selected_tickers)

    if len(catalog) != policy.expected_catalog_count:
        errors.append(
            f"authoritative catalog count={len(catalog)} expected={policy.expected_catalog_count}"
        )
    if set(implementation) != set(catalog):
        errors.append(
            "implementation catalog differs from authoritative catalog: "
            f"missing={sorted(set(catalog) - set(implementation))} "
            f"extra={sorted(set(implementation) - set(catalog))}"
        )
    if set(positioning) != selected:
        errors.append(
            "positioning universe differs from selected policy: "
            f"missing={sorted(selected - set(positioning))} "
            f"extra={sorted(set(positioning) - selected)}"
        )
    expected_exclusions = set(catalog) - selected
    if set(exclusions) != expected_exclusions:
        errors.append(
            "exclusions are not the exact catalog complement: "
            f"missing={sorted(expected_exclusions - set(exclusions))} "
            f"extra={sorted(set(exclusions) - expected_exclusions)}"
        )
    for ticker, row in catalog.items():
        if str(row.get("investability_status") or "").lower() != "investable":
            errors.append(f"{ticker}: research catalog row is not investable")
        if str(row.get("listing_status") or "").lower() != "active":
            errors.append(f"{ticker}: research catalog row is not active")
        if not _flag(row.get("is_primary_listing")):
            errors.append(f"{ticker}: research catalog row is not primary listing")
    for ticker, row in exclusions.items():
        if row.get("effective_from") != policy.effective_from:
            errors.append(f"{ticker}: exclusion effective date is not policy date")
        if row.get("disposition") not in {"watchlist", "research_only"}:
            errors.append(f"{ticker}: invalid exclusion disposition")
        if not row.get("reason") or not row.get("reentry_rule"):
            errors.append(f"{ticker}: exclusion lacks reason or reentry rule")
    for ticker in selected:
        catalog_row = catalog.get(ticker, {})
        positioning_row = positioning.get(ticker, {})
        for field in ("company_name", "cik", "calibration_cohort"):
            if str(catalog_row.get(field) or "") != str(
                positioning_row.get(field) or ""
            ):
                errors.append(f"{ticker}: positioning row differs on {field}")

    summary = {
        "policy_version": policy.policy_version,
        "effective_from": policy.effective_from,
        "catalog_count": len(catalog),
        "selected_count": len(selected),
        "excluded_count": len(exclusions),
        "group_counts": {
            group.group_id: len(group.tickers) for group in policy.groups
        },
        "new_tanker_count": len(policy.new_tanker_tickers),
        "direct_tanker_metric_count": len(policy.direct_tanker_metrics),
        "derived_tanker_metric_count": len(policy.derived_tanker_metrics),
        "surface_comparison_domain_count": len(policy.surface_comparison_domains),
        "surface_metric_domain_rule_count": len(policy.surface_metric_domain_rules),
        "surface_candidate_domain_rule_count": sum(
            rule.is_calibration_candidate for rule in policy.surface_metric_domain_rules
        ),
        "errors": errors,
        "acceptance": "PASS" if not errors else "FAIL",
    }
    return errors, summary

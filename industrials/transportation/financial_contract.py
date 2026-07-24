from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from industrials.core.config import load_yaml


MODEL_FAMILY = "transportation"
VALID_COMPONENTS = frozenset(
    {
        "market_trend",
        "quality",
        "growth",
        "valuation",
        "operating_efficiency",
        "capital_risk",
        "development_stage_risk",
        "positioning",
    }
)
VALID_SOURCES = frozenset({"market", "financial", "derived", "disclosure_candidate"})
VALID_STATUSES = frozenset(
    {
        "REPORTED",
        "DERIVED",
        "PROXY",
        "NOT_APPLICABLE",
        "NOT_DISCLOSED",
        "DISCLOSED_UNPARSED",
        "PARSER_FAILURE",
    }
)


@dataclass(frozen=True)
class MetricDefinition:
    metric_id: str
    component: str
    source: str
    source_field: str
    formula: str
    candidate_metric: str
    direction: int
    cohorts: tuple[str, ...]
    industries: tuple[str, ...]
    required_for_rank: bool
    specialized: bool
    unit: str
    minimum_history_days: int
    winsor_lower: float
    winsor_upper: float
    birthdate: str
    production_status: str

    def applies_to(self, *, cohort: str, industry: str) -> bool:
        cohort_ok = "*" in self.cohorts or cohort in self.cohorts
        industry_ok = not self.industries or industry in self.industries
        return cohort_ok and industry_ok


def _as_bool(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def load_metric_registry(path: Path) -> tuple[str, list[MetricDefinition]]:
    payload = load_yaml(path)
    if str(payload.get("model_family") or "").strip() != MODEL_FAMILY:
        raise ValueError(f"{path}: model_family must be {MODEL_FAMILY}")
    version = str(payload.get("registry_version") or "").strip()
    if not version:
        raise ValueError(f"{path}: registry_version is required")
    configured_statuses = {str(item) for item in payload.get("availability_statuses", [])}
    if configured_statuses != set(VALID_STATUSES):
        raise ValueError(f"{path}: availability_statuses must equal {sorted(VALID_STATUSES)}")
    raw_defaults = payload.get("defaults")
    defaults: dict[str, Any] = raw_defaults if isinstance(raw_defaults, dict) else {}
    raw_metrics = payload.get("metrics")
    if not isinstance(raw_metrics, list) or not raw_metrics:
        raise ValueError(f"{path}: metrics must be a non-empty list")
    definitions: list[MetricDefinition] = []
    seen: set[str] = set()
    for raw in raw_metrics:
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: every metric entry must be a mapping")
        metric_id = str(raw.get("metric_id") or "").strip()
        component = str(raw.get("component") or "").strip()
        source = str(raw.get("source") or "").strip()
        if not metric_id or metric_id in seen:
            raise ValueError(f"{path}: blank or duplicate metric_id={metric_id!r}")
        if component not in VALID_COMPONENTS:
            raise ValueError(f"{path}: {metric_id} has invalid component={component!r}")
        if source not in VALID_SOURCES:
            raise ValueError(f"{path}: {metric_id} has invalid source={source!r}")
        direction = int(raw.get("direction") or 0)
        if direction not in {-1, 1}:
            raise ValueError(f"{path}: {metric_id} direction must be -1 or 1")
        cohorts = tuple(str(item).strip() for item in raw.get("cohorts", []) if str(item).strip())
        if not cohorts:
            raise ValueError(f"{path}: {metric_id} requires cohort applicability")
        source_field = str(raw.get("source_field") or "").strip()
        formula = str(raw.get("formula") or "").strip()
        candidate_metric = str(raw.get("candidate_metric") or "").strip()
        if source in {"market", "financial"} and not source_field:
            raise ValueError(f"{path}: {metric_id} requires source_field")
        if source == "derived" and not formula:
            raise ValueError(f"{path}: {metric_id} requires formula")
        if source == "disclosure_candidate" and not candidate_metric:
            raise ValueError(f"{path}: {metric_id} requires candidate_metric")
        lower = float(raw.get("winsor_lower", defaults.get("winsor_lower", 0.05)))
        upper = float(raw.get("winsor_upper", defaults.get("winsor_upper", 0.95)))
        if not 0.0 <= lower < upper <= 1.0:
            raise ValueError(f"{path}: {metric_id} has invalid winsor bounds")
        definitions.append(
            MetricDefinition(
                metric_id=metric_id,
                component=component,
                source=source,
                source_field=source_field,
                formula=formula,
                candidate_metric=candidate_metric,
                direction=direction,
                cohorts=cohorts,
                industries=tuple(
                    str(item).strip() for item in raw.get("industries", []) if str(item).strip()
                ),
                required_for_rank=_as_bool(raw.get("required_for_rank")),
                specialized=_as_bool(raw.get("specialized")),
                unit=str(raw.get("unit") or "").strip(),
                minimum_history_days=int(
                    raw.get("minimum_history_days", defaults.get("minimum_history_days", 0)) or 0
                ),
                winsor_lower=lower,
                winsor_upper=upper,
                birthdate=str(raw.get("birthdate", defaults.get("birthdate", ""))).strip(),
                production_status=str(
                    raw.get("production_status", defaults.get("production_status", "shadow"))
                ).strip(),
            )
        )
        seen.add(metric_id)
    return version, definitions


def registry_summary(definitions: list[MetricDefinition]) -> dict[str, Any]:
    return {
        "metric_count": len(definitions),
        "specialized_metric_count": sum(item.specialized for item in definitions),
        "required_metric_count": sum(item.required_for_rank for item in definitions),
        "components": sorted({item.component for item in definitions}),
    }

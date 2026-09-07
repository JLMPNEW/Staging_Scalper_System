"""Stage 5A specialized-metric, applicability, and all-source census contract."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

import yaml

from basic_materials import MODEL_FAMILY, SECTOR
from basic_materials.core.atomic_io import atomic_write_csv, atomic_write_json
from basic_materials.core.db import assert_database_identity, utc_now


COHORTS = frozenset(
    {
        "agricultural_inputs_crop_science",
        "building_materials",
        "commodity_chemicals",
        "industrial_metals_mining",
        "mining_royalty_streaming",
        "precious_metals_producers",
        "specialty_chemicals_materials",
        "steel_producers_processors",
    }
)
METRIC_ROLES = frozenset({"direct", "operand", "derived"})
APPLICABILITY_MODES = frozenset({"all_cohort_issuers", "issuer_selective_review"})
COVERAGE_TIERS = frozenset({"core", "supporting", "optional"})
PERIOD_TYPES = frozenset({"duration", "instant", "event"})
DIRECTION_HINTS = frozenset({"positive", "negative", "context_dependent", "diagnostic"})
SOURCE_FAMILIES = (
    "sec_filing",
    "issuer_ir",
    "local_exchange",
    "archived_issuer",
    "technical_report",
    "reserve_resource",
    "commodity_market",
    "positioning_market",
)


class SpecializedContractError(ValueError):
    """Raised when the Stage 5A contract is malformed or unsafe."""


@dataclass(frozen=True)
class SpecializedMetric:
    metric_id: str
    cohort_id: str
    metric_role: str
    applicability_mode: str
    coverage_tier: str
    unit_family: str
    period_type: str
    dimension_family: str
    direction_hint: str
    definition: str
    formula: str
    operands: tuple[Mapping[str, Any], ...]
    definition_variants: tuple[str, ...]
    source_families: tuple[str, ...]
    table_families: tuple[str, ...]
    plausibility: Mapping[str, Any]


@dataclass(frozen=True)
class SpecializedMetricRegistry:
    path: Path
    checksum: str
    version: str
    as_of_date: str
    coverage_gates: Mapping[str, float | int]
    metrics: tuple[SpecializedMetric, ...]


@dataclass(frozen=True)
class SpecializedSourcePolicy:
    path: Path
    checksum: str
    version: str
    as_of_date: str
    target_history_start_date: str
    source_census_start_date: str
    allowed_sec_forms: tuple[str, ...]
    source_family_priority: tuple[str, ...]
    source_families: Mapping[str, Mapping[str, str]]
    mining_cohorts: tuple[str, ...]
    reserve_resource_cohorts: tuple[str, ...]
    commodity_drivers: Mapping[str, tuple[str, ...]]
    positioning_drivers: tuple[str, ...]
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class SpecializedContractLoadStats:
    metrics: int
    cohort_links: int
    operand_links: int
    applicability_rows: int
    applicability_review_rows: int
    source_census_rows: int
    sec_filing_rows: int
    discovery_required_rows: int
    explicit_not_applicable_source_rows: int
    raw_contract_payloads: int
    metric_registry_sha256: str
    source_policy_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SpecializedContractValidationReport:
    contract_valid: bool
    stage5a_sealed: bool
    registry_version: str
    registry_sha256: str
    source_policy_version: str
    source_policy_sha256: str
    counts: Mapping[str, int]
    applicability_status_counts: Mapping[str, int]
    source_status_counts: Mapping[str, int]
    source_family_counts: Mapping[str, int]
    issues: tuple[Mapping[str, Any], ...]
    metrics: tuple[Mapping[str, Any], ...]
    applicability: tuple[Mapping[str, Any], ...]
    source_census: tuple[Mapping[str, Any], ...]

    def summary_dict(self) -> dict[str, Any]:
        return {
            "contract_valid": self.contract_valid,
            "stage5a_sealed": self.stage5a_sealed,
            "registry_version": self.registry_version,
            "registry_sha256": self.registry_sha256,
            "source_policy_version": self.source_policy_version,
            "source_policy_sha256": self.source_policy_sha256,
            "counts": dict(self.counts),
            "applicability_status_counts": dict(self.applicability_status_counts),
            "source_status_counts": dict(self.source_status_counts),
            "source_family_counts": dict(self.source_family_counts),
            "error_count": sum(item["severity"] == "error" for item in self.issues),
            "blocker_count": sum(item["severity"] == "blocker" for item in self.issues),
            "issues": [dict(item) for item in self.issues],
        }


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SpecializedContractError(f"{context} must be a mapping")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    actual = set(value)
    if actual != expected:
        raise SpecializedContractError(
            f"{context} keys differ; missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )


def _iso_date(value: Any, context: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise SpecializedContractError(f"{context} must be an ISO date") from exc


def _sequence(value: Any, context: str, *, allow_empty: bool = False) -> tuple[Any, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise SpecializedContractError(f"{context} must be a list")
    result = tuple(value)
    if not result and not allow_empty:
        raise SpecializedContractError(f"{context} must not be empty")
    return result


def _strings(value: Any, context: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    result = tuple(str(item).strip() for item in _sequence(value, context, allow_empty=allow_empty))
    if any(not item for item in result) or len(result) != len(set(result)):
        raise SpecializedContractError(f"{context} must contain unique non-empty strings")
    return result


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _row_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_specialized_metric_registry(path: str | Path) -> SpecializedMetricRegistry:
    registry_path = Path(path).resolve()
    payload = registry_path.read_bytes()
    root = _mapping(yaml.safe_load(payload.decode("utf-8")), "specialized metric registry")
    _exact_keys(
        root,
        {
            "registry_version",
            "contract_as_of_date",
            "model_family",
            "sector",
            "state",
            "production_weight",
            "expected_cohort_count",
            "expected_metric_count",
            "coverage_gates",
            "source_priority",
            "metric_sets",
        },
        "specialized metric registry",
    )
    if root["registry_version"] != "basic_materials_specialized_metrics_v1":
        raise SpecializedContractError("Unsupported specialized metric registry version")
    if root["model_family"] != MODEL_FAMILY or root["sector"] != SECTOR:
        raise SpecializedContractError("Specialized metric registry identity is invalid")
    if (
        root["state"] != "stage5a_candidate_contract_scoring_blocked"
        or float(root["production_weight"]) != 0.0
    ):
        raise SpecializedContractError("Specialized metrics must remain scoring-blocked at zero weight")
    gates = _mapping(root["coverage_gates"], "coverage_gates")
    _exact_keys(
        gates,
        {
            "current_core_applicable_pair_ratio",
            "historical_core_issuer_period_ratio",
            "chronological_block_floor",
            "calibration_min_comparable_issuers",
            "calibration_min_dates",
        },
        "coverage_gates",
    )
    expected_gate_values = {
        "current_core_applicable_pair_ratio": 0.80,
        "historical_core_issuer_period_ratio": 0.70,
        "chronological_block_floor": 0.60,
        "calibration_min_comparable_issuers": 8,
        "calibration_min_dates": 36,
    }
    if {key: float(value) for key, value in gates.items()} != {
        key: float(value) for key, value in expected_gate_values.items()
    }:
        raise SpecializedContractError("Specialized coverage gates differ from the approved F0 contract")
    source_priority = _strings(root["source_priority"], "source_priority")
    if source_priority != (
        "sec_filing",
        "issuer_ir",
        "local_exchange",
        "technical_report",
        "reserve_resource",
        "archived_issuer",
    ):
        raise SpecializedContractError("Specialized source priority is invalid")

    metric_sets = _sequence(root["metric_sets"], "metric_sets")
    metrics: list[SpecializedMetric] = []
    seen_cohorts: set[str] = set()
    seen_metrics: set[str] = set()
    metric_keys = {
        "metric_id",
        "metric_role",
        "applicability_mode",
        "coverage_tier",
        "unit_family",
        "period_type",
        "dimension_family",
        "direction_hint",
        "definition",
        "formula",
        "operands",
        "definition_variants",
        "source_families",
        "table_families",
        "plausibility",
    }
    for set_index, raw_set in enumerate(metric_sets):
        metric_set = _mapping(raw_set, f"metric_sets[{set_index}]")
        _exact_keys(metric_set, {"cohort_id", "metrics"}, f"metric_sets[{set_index}]")
        cohort = str(metric_set["cohort_id"]).strip()
        if cohort not in COHORTS or cohort in seen_cohorts:
            raise SpecializedContractError(f"Invalid or duplicate cohort metric set: {cohort}")
        seen_cohorts.add(cohort)
        for metric_index, raw_metric in enumerate(_sequence(metric_set["metrics"], f"{cohort}.metrics")):
            item = _mapping(raw_metric, f"{cohort}.metrics[{metric_index}]")
            _exact_keys(item, metric_keys, f"{cohort}.metrics[{metric_index}]")
            metric_id = str(item["metric_id"]).strip()
            role = str(item["metric_role"]).strip()
            mode = str(item["applicability_mode"]).strip()
            tier = str(item["coverage_tier"]).strip()
            period_type = str(item["period_type"]).strip()
            direction = str(item["direction_hint"]).strip()
            if not metric_id or metric_id in seen_metrics:
                raise SpecializedContractError(f"Invalid or duplicate metric_id: {metric_id!r}")
            if role not in METRIC_ROLES or mode not in APPLICABILITY_MODES or tier not in COVERAGE_TIERS:
                raise SpecializedContractError(f"Invalid role, applicability, or tier for {metric_id}")
            if period_type not in PERIOD_TYPES or direction not in DIRECTION_HINTS:
                raise SpecializedContractError(f"Invalid period type or direction for {metric_id}")
            formula = str(item["formula"]).strip()
            operands_raw = _sequence(item["operands"], f"{metric_id}.operands", allow_empty=True)
            operands: list[Mapping[str, Any]] = []
            for operand_index, raw_operand in enumerate(operands_raw):
                operand = _mapping(raw_operand, f"{metric_id}.operands[{operand_index}]")
                _exact_keys(operand, {"metric_id", "operand_role", "required"}, f"{metric_id}.operand")
                if not isinstance(operand["required"], bool):
                    raise SpecializedContractError(f"{metric_id} operand required flag must be boolean")
                operands.append(dict(operand))
            if role == "derived" and (not formula or not operands):
                raise SpecializedContractError(f"Derived metric {metric_id} requires formula and operands")
            if role != "derived" and (formula or operands):
                raise SpecializedContractError(f"Non-derived metric {metric_id} cannot define a formula")
            sources = _strings(item["source_families"], f"{metric_id}.source_families")
            if role == "derived" and sources != ("derived_from_accepted_observations",):
                raise SpecializedContractError(f"Derived metric {metric_id} has invalid source family")
            if role != "derived" and any(source not in SOURCE_FAMILIES for source in sources):
                raise SpecializedContractError(f"Metric {metric_id} has an unknown source family")
            plausibility = _mapping(item["plausibility"], f"{metric_id}.plausibility")
            _exact_keys(plausibility, {"minimum", "maximum", "allow_negative"}, f"{metric_id}.plausibility")
            if not isinstance(plausibility["allow_negative"], bool):
                raise SpecializedContractError(f"{metric_id}.plausibility.allow_negative must be boolean")
            minimum, maximum = plausibility["minimum"], plausibility["maximum"]
            if minimum is not None and maximum is not None and float(minimum) > float(maximum):
                raise SpecializedContractError(f"{metric_id} plausibility interval is reversed")
            seen_metrics.add(metric_id)
            metrics.append(
                SpecializedMetric(
                    metric_id=metric_id,
                    cohort_id=cohort,
                    metric_role=role,
                    applicability_mode=mode,
                    coverage_tier=tier,
                    unit_family=str(item["unit_family"]).strip(),
                    period_type=period_type,
                    dimension_family=str(item["dimension_family"]).strip(),
                    direction_hint=direction,
                    definition=str(item["definition"]).strip(),
                    formula=formula,
                    operands=tuple(operands),
                    definition_variants=_strings(item["definition_variants"], f"{metric_id}.variants"),
                    source_families=sources,
                    table_families=_strings(item["table_families"], f"{metric_id}.table_families"),
                    plausibility=dict(plausibility),
                )
            )
    if seen_cohorts != COHORTS or len(seen_cohorts) != int(root["expected_cohort_count"]):
        raise SpecializedContractError("Specialized metric cohort coverage is incomplete")
    if len(metrics) != int(root["expected_metric_count"]) or len(metrics) != 64:
        raise SpecializedContractError(f"Expected exactly 64 specialized metrics; found {len(metrics)}")
    metric_by_id = {metric.metric_id: metric for metric in metrics}
    for metric in metrics:
        for operand in metric.operands:
            operand_id = str(operand["metric_id"])
            linked = metric_by_id.get(operand_id)
            if linked is None or linked.cohort_id != metric.cohort_id or linked.metric_role == "derived":
                raise SpecializedContractError(
                    f"{metric.metric_id} has missing, cross-cohort, or derived operand {operand_id}"
                )
    return SpecializedMetricRegistry(
        path=registry_path,
        checksum=hashlib.sha256(payload).hexdigest(),
        version=str(root["registry_version"]),
        as_of_date=_iso_date(root["contract_as_of_date"], "contract_as_of_date"),
        coverage_gates=dict(gates),
        metrics=tuple(metrics),
    )


def load_specialized_source_policy(path: str | Path) -> SpecializedSourcePolicy:
    policy_path = Path(path).resolve()
    payload = policy_path.read_bytes()
    root = _mapping(yaml.safe_load(payload.decode("utf-8")), "specialized source policy")
    _exact_keys(
        root,
        {
            "policy_version",
            "contract_as_of_date",
            "model_family",
            "sector",
            "state",
            "target_history_start_date",
            "source_census_start_date",
            "allowed_sec_forms",
            "source_family_priority",
            "source_families",
            "mining_cohorts",
            "reserve_resource_cohorts",
            "commodity_drivers",
            "positioning_drivers",
            "census_controls",
            "hydration_controls",
            "required_flags",
        },
        "specialized source policy",
    )
    if root["policy_version"] != "basic_materials_specialized_source_policy_v1":
        raise SpecializedContractError("Unsupported specialized source policy version")
    if root["model_family"] != MODEL_FAMILY or root["sector"] != SECTOR:
        raise SpecializedContractError("Specialized source policy identity is invalid")
    if root["state"] != "stage5a_source_census_open_scoring_blocked":
        raise SpecializedContractError("Specialized source policy state is invalid")
    family_priority = _strings(root["source_family_priority"], "source_family_priority")
    if set(family_priority) != set(SOURCE_FAMILIES):
        raise SpecializedContractError("Source-family priority does not cover the complete census")
    families_raw = _mapping(root["source_families"], "source_families")
    if set(families_raw) != set(SOURCE_FAMILIES):
        raise SpecializedContractError("Source-family definitions are incomplete")
    families: dict[str, Mapping[str, str]] = {}
    for family, raw in families_raw.items():
        item = _mapping(raw, f"source_families.{family}")
        _exact_keys(
            item,
            {"source_id", "document_role", "availability_field", "applicability_rule"},
            f"source_families.{family}",
        )
        families[str(family)] = {key: str(value).strip() for key, value in item.items()}
    commodity_raw = _mapping(root["commodity_drivers"], "commodity_drivers")
    if set(commodity_raw) != COHORTS:
        raise SpecializedContractError("Commodity drivers must cover every cohort")
    commodity_drivers = {
        str(cohort): _strings(values, f"commodity_drivers.{cohort}")
        for cohort, values in commodity_raw.items()
    }
    census = _mapping(root["census_controls"], "census_controls")
    hydration = _mapping(root["hydration_controls"], "hydration_controls")
    flags = _mapping(root["required_flags"], "required_flags")
    if any(value is not True for value in census.values()):
        raise SpecializedContractError("Every Stage 5A census control must be enabled")
    if (
        hydration.get("content_address_algorithm") != "sha256"
        or hydration.get("compile_unique_content_once") is not True
        or hydration.get("parser_execution_during_stage5a") is not False
        or int(hydration.get("maximum_full_parse_attempts_per_content_hash", -1)) != 1
        or int(hydration.get("maximum_residual_parse_attempts_per_content_hash", -1)) != 1
        or hydration.get("policy_only_review_requires_zero_source_calls") is not True
    ):
        raise SpecializedContractError("One-pass hydration/parser controls are invalid")
    if any(value is not False for value in flags.values()):
        raise SpecializedContractError("Stage 5A cannot activate scoring, calibration, PIT, or portfolio gates")
    target_start = _iso_date(root["target_history_start_date"], "target_history_start_date")
    census_start = _iso_date(root["source_census_start_date"], "source_census_start_date")
    if census_start >= target_start:
        raise SpecializedContractError("Source census must begin before the target history window")
    mining = _strings(root["mining_cohorts"], "mining_cohorts")
    reserves = _strings(root["reserve_resource_cohorts"], "reserve_resource_cohorts")
    if not set(mining) <= COHORTS or not set(reserves) <= COHORTS:
        raise SpecializedContractError("Source policy contains an unknown cohort")
    return SpecializedSourcePolicy(
        path=policy_path,
        checksum=hashlib.sha256(payload).hexdigest(),
        version=str(root["policy_version"]),
        as_of_date=_iso_date(root["contract_as_of_date"], "contract_as_of_date"),
        target_history_start_date=target_start,
        source_census_start_date=census_start,
        allowed_sec_forms=_strings(root["allowed_sec_forms"], "allowed_sec_forms"),
        source_family_priority=family_priority,
        source_families=families,
        mining_cohorts=mining,
        reserve_resource_cohorts=reserves,
        commodity_drivers=commodity_drivers,
        positioning_drivers=_strings(root["positioning_drivers"], "positioning_drivers"),
        payload=dict(root),
    )


def _identity_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT t.security_id, t.ticker, t.cohort_id, m.membership_status,
                   m.membership_start_date, COALESCE(m.membership_end_date, '') AS membership_end_date,
                   COALESCE(p.filing_regime, 'unknown') AS filing_regime
            FROM dim_basic_materials_taxonomy t
            JOIN dim_universe_membership m ON m.security_id = t.security_id
            LEFT JOIN dim_issuer_reporting_profile p ON p.security_id = t.security_id
            ORDER BY t.ticker
            """
        ).fetchall()
    ]


def _metric_scope(
    registry: SpecializedMetricRegistry,
) -> dict[tuple[str, str], tuple[str, ...]]:
    scope: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    for metric in registry.metrics:
        if metric.metric_role == "derived":
            continue
        for family in metric.source_families:
            scope[(metric.cohort_id, family)].add(metric.metric_id)
    return {key: tuple(sorted(values)) for key, values in scope.items()}


def _source_row(
    *,
    identity: Mapping[str, Any],
    family: str,
    source_record_key: str,
    document_role: str,
    source_id: str,
    discovery_status: str,
    requested_metrics: Sequence[str],
    policy: SpecializedSourcePolicy,
    accession_number: str = "",
    form_type: str = "",
    period_end: str = "",
    availability_timestamp: str = "",
    source_url: str = "",
    source_metadata_sha256: str = "",
    content_sha256: str = "",
    terminal_reason: str = "",
) -> dict[str, Any]:
    base = {
        "security_id": int(identity["security_id"]),
        "ticker": str(identity["ticker"]),
        "cohort_id": str(identity["cohort_id"]),
        "source_family": family,
        "source_record_key": source_record_key,
        "document_role": document_role,
        "accession_number": accession_number,
        "form_type": form_type,
        "period_end": period_end,
        "availability_timestamp": availability_timestamp,
        "source_url": source_url,
        "source_id": source_id,
        "discovery_status": discovery_status,
        "source_metadata_sha256": source_metadata_sha256,
        "content_sha256": content_sha256,
        "terminal_reason": terminal_reason,
        "requested_metric_ids_json": _canonical_json(sorted(set(requested_metrics))),
        "census_version": policy.version,
        "production_eligible": 0,
        "calibration_eligible": 0,
    }
    key_payload = {
        "security_id": base["security_id"],
        "source_family": family,
        "source_record_key": source_record_key,
        "census_version": policy.version,
    }
    base["census_key"] = _row_hash(key_payload)
    base["census_sha256"] = _row_hash(base)
    return base


def _source_census_rows(
    conn: sqlite3.Connection,
    *,
    registry: SpecializedMetricRegistry,
    policy: SpecializedSourcePolicy,
    identities: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    metrics = _metric_scope(registry)
    identity_by_security = {int(row["security_id"]): row for row in identities}
    rows: list[dict[str, Any]] = []
    seen_sec: Counter[int] = Counter()
    placeholders = ",".join("?" for _ in policy.allowed_sec_forms)
    filings = conn.execute(
        f"""
        SELECT security_id, ticker, accession_number, form_type, report_date,
               accepted_at, source_url, payload_sha256
        FROM fact_sec_filing
        WHERE substr(accepted_at, 1, 10) >= ?
          AND form_type IN ({placeholders})
        ORDER BY security_id, accepted_at, accession_number
        """,
        (policy.source_census_start_date, *policy.allowed_sec_forms),
    ).fetchall()
    for filing in filings:
        identity = identity_by_security.get(int(filing["security_id"]))
        if identity is None:
            continue
        relevant = (
            identity["membership_status"] == "current"
            or not identity["membership_end_date"]
            or str(identity["membership_end_date"]) >= policy.target_history_start_date
        )
        if not relevant:
            continue
        seen_sec[int(identity["security_id"])] += 1
        family = "sec_filing"
        definition = policy.source_families[family]
        rows.append(
            _source_row(
                identity=identity,
                policy=policy,
                family=family,
                source_record_key=str(filing["accession_number"]),
                document_role=definition["document_role"],
                source_id=definition["source_id"],
                discovery_status="identified_unhydrated",
                requested_metrics=metrics.get((str(identity["cohort_id"]), family), ()),
                accession_number=str(filing["accession_number"]),
                form_type=str(filing["form_type"]),
                period_end=str(filing["report_date"]),
                availability_timestamp=str(filing["accepted_at"]),
                source_url=str(filing["source_url"]),
                source_metadata_sha256=str(filing["payload_sha256"]),
            )
        )

    mining = set(policy.mining_cohorts)
    reserve_cohorts = set(policy.reserve_resource_cohorts)
    positioning = set(policy.positioning_drivers)
    for identity in identities:
        security_id = int(identity["security_id"])
        cohort = str(identity["cohort_id"])
        role = str(identity["membership_status"])
        target_relevant = role == "current" or str(identity["membership_end_date"]) >= policy.target_history_start_date
        if not target_relevant:
            for family in SOURCE_FAMILIES:
                if family == "sec_filing" and seen_sec[security_id]:
                    continue
                definition = policy.source_families[family]
                rows.append(
                    _source_row(
                        identity=identity,
                        policy=policy,
                        family=family,
                        source_record_key="outside_target_window",
                        document_role=definition["document_role"],
                        source_id=definition["source_id"],
                        discovery_status="not_applicable",
                        requested_metrics=(),
                    )
                )
            continue
        if not seen_sec[security_id]:
            definition = policy.source_families["sec_filing"]
            rows.append(
                _source_row(
                    identity=identity,
                    policy=policy,
                    family="sec_filing",
                    source_record_key="discovery",
                    document_role=definition["document_role"],
                    source_id=definition["source_id"],
                    discovery_status="discovery_required",
                    requested_metrics=metrics.get((cohort, "sec_filing"), ()),
                )
            )
        family_rules = {
            "issuer_ir": True,
            "local_exchange": str(identity["filing_regime"]) in {"foreign_private_issuer", "canadian_mjds"},
            "archived_issuer": role == "historical",
            "technical_report": cohort in mining,
            "reserve_resource": cohort in reserve_cohorts,
        }
        for family, applicable in family_rules.items():
            definition = policy.source_families[family]
            rows.append(
                _source_row(
                    identity=identity,
                    policy=policy,
                    family=family,
                    source_record_key="discovery" if applicable else "not_applicable",
                    document_role=definition["document_role"],
                    source_id=definition["source_id"],
                    discovery_status="discovery_required" if applicable else "not_applicable",
                    requested_metrics=metrics.get((cohort, family), ()) if applicable else (),
                )
            )
        commodity_definition = policy.source_families["commodity_market"]
        cohort_drivers = policy.commodity_drivers[cohort]
        for driver in cohort_drivers:
            rows.append(
                _source_row(
                    identity=identity,
                    policy=policy,
                    family="commodity_market",
                    source_record_key=f"driver:{driver}",
                    document_role=f"{commodity_definition['document_role']}:{driver}",
                    source_id=commodity_definition["source_id"],
                    discovery_status="discovery_required",
                    requested_metrics=(),
                )
            )
        positioning_definition = policy.source_families["positioning_market"]
        positioned_drivers = tuple(driver for driver in cohort_drivers if driver in positioning)
        if positioned_drivers:
            for driver in positioned_drivers:
                rows.append(
                    _source_row(
                        identity=identity,
                        policy=policy,
                        family="positioning_market",
                        source_record_key=f"driver:{driver}",
                        document_role=f"{positioning_definition['document_role']}:{driver}",
                        source_id=positioning_definition["source_id"],
                        discovery_status="discovery_required",
                        requested_metrics=(),
                    )
                )
        else:
            rows.append(
                _source_row(
                    identity=identity,
                    policy=policy,
                    family="positioning_market",
                    source_record_key="not_applicable",
                    document_role=positioning_definition["document_role"],
                    source_id=positioning_definition["source_id"],
                    discovery_status="not_applicable",
                    requested_metrics=(),
                )
            )
    keys = [str(row["census_key"]) for row in rows]
    if len(keys) != len(set(keys)):
        raise SpecializedContractError("Source census contains duplicate keys")
    return rows


def _store_raw_contract(
    conn: sqlite3.Connection,
    *,
    path: Path,
    source_id: str,
    snapshot_date: str,
    checksum: str,
    row_count: int,
    manifest_version: str,
    now: str,
) -> None:
    payload = path.read_bytes()
    snapshot_id = _row_hash({"source_id": source_id, "sha256": checksum})
    conn.execute(
        """
        INSERT INTO raw_source_payloads (
            snapshot_id, source_id, source_snapshot_date, source_path, sha256,
            byte_size, row_count, media_type, payload, manifest_version, ingested_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'application/yaml', ?, ?, ?)
        ON CONFLICT(snapshot_id) DO UPDATE SET
            source_path = excluded.source_path,
            byte_size = excluded.byte_size,
            row_count = excluded.row_count,
            payload = excluded.payload,
            manifest_version = excluded.manifest_version,
            ingested_at_utc = excluded.ingested_at_utc
        """,
        (
            snapshot_id,
            source_id,
            snapshot_date,
            str(path),
            checksum,
            len(payload),
            row_count,
            payload,
            manifest_version,
            now,
        ),
    )


def load_specialized_contract(
    conn: sqlite3.Connection,
    *,
    registry: SpecializedMetricRegistry,
    source_policy: SpecializedSourcePolicy,
) -> SpecializedContractLoadStats:
    """Replace only the pre-parser Stage 5A contract inside one transaction."""

    assert_database_identity(conn)
    if registry.as_of_date != source_policy.as_of_date:
        raise SpecializedContractError("Metric and source policy as-of dates differ")
    parser_evidence = sum(
        int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "fact_specialized_parser_work",
            "fact_specialized_metric_candidate",
            "fact_specialized_metric_observation",
        )
    )
    if parser_evidence:
        raise SpecializedContractError(
            "Refusing to replace a specialized contract after parser evidence exists; use a new schema migration"
        )
    identities = _identity_rows(conn)
    if not identities:
        raise SpecializedContractError("Specialized contract requires a loaded universe")
    if len({int(row["security_id"]) for row in identities}) != len(identities):
        raise SpecializedContractError("Universe contains duplicate security identities")
    required_source_ids = {
        "basic_materials_specialized_metric_policy",
        "basic_materials_specialized_source_census",
        *(item["source_id"] for item in source_policy.source_families.values()),
    }
    active_sources = {
        str(row["source_id"])
        for row in conn.execute("SELECT source_id FROM source_registry WHERE active = 1").fetchall()
    }
    missing_sources = sorted(required_source_ids - active_sources)
    if missing_sources:
        raise SpecializedContractError(f"Source registry is missing active sources: {missing_sources}")

    applicability: list[dict[str, Any]] = []
    for identity in identities:
        outside_window = (
            identity["membership_status"] == "historical"
            and str(identity["membership_end_date"]) < source_policy.target_history_start_date
        )
        for metric in registry.metrics:
            if metric.cohort_id != identity["cohort_id"] or outside_window:
                status = "not_applicable"
                basis = "different_cohort" if metric.cohort_id != identity["cohort_id"] else "outside_target_history_window"
                reviewed = 1
            elif metric.applicability_mode == "all_cohort_issuers":
                status = "applicable"
                basis = "reviewed_cohort_default"
                reviewed = 1
            else:
                status = "review_required"
                basis = "issuer_selective_metric_requires_identity_review"
                reviewed = 0
            row = {
                "security_id": int(identity["security_id"]),
                "metric_id": metric.metric_id,
                "ticker": str(identity["ticker"]),
                "cohort_id": str(identity["cohort_id"]),
                "applicability_status": status,
                "applicability_basis": basis,
                "reviewed": reviewed,
                "review_source_id": "basic_materials_specialized_metric_policy",
                "policy_version": registry.version,
                "policy_sha256": registry.checksum,
            }
            row["row_sha256"] = _row_hash(row)
            applicability.append(row)
    source_rows = _source_census_rows(
        conn,
        registry=registry,
        policy=source_policy,
        identities=identities,
    )
    now = utc_now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM fact_specialized_source_census")
        conn.execute("DELETE FROM bridge_specialized_metric_applicability")
        conn.execute("DELETE FROM bridge_specialized_metric_operand")
        conn.execute("DELETE FROM bridge_specialized_metric_cohort")
        conn.execute("DELETE FROM dim_specialized_metric")
        for metric in registry.metrics:
            conn.execute(
                """
                INSERT INTO dim_specialized_metric (
                    metric_id, metric_role, unit_family, period_type, dimension_family,
                    direction_hint, definition, formula, definition_variants_json,
                    plausibility_json, production_weight, scoring_eligible,
                    calibration_eligible, source_id, policy_version, policy_sha256,
                    created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, ?, ?, ?)
                """,
                (
                    metric.metric_id,
                    metric.metric_role,
                    metric.unit_family,
                    metric.period_type,
                    metric.dimension_family,
                    metric.direction_hint,
                    metric.definition,
                    metric.formula,
                    _canonical_json(metric.definition_variants),
                    _canonical_json(metric.plausibility),
                    "basic_materials_specialized_metric_policy",
                    registry.version,
                    registry.checksum,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO bridge_specialized_metric_cohort (
                    metric_id, cohort_id, applicability_mode, coverage_tier,
                    minimum_current_coverage, minimum_historical_coverage,
                    source_families_json, table_families_json, policy_version,
                    policy_sha256, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    metric.metric_id,
                    metric.cohort_id,
                    metric.applicability_mode,
                    metric.coverage_tier,
                    float(registry.coverage_gates["current_core_applicable_pair_ratio"]),
                    float(registry.coverage_gates["historical_core_issuer_period_ratio"]),
                    _canonical_json(metric.source_families),
                    _canonical_json(metric.table_families),
                    registry.version,
                    registry.checksum,
                    now,
                    now,
                ),
            )
            for operand in metric.operands:
                conn.execute(
                    """
                    INSERT INTO bridge_specialized_metric_operand (
                        derived_metric_id, operand_metric_id, operand_role, required,
                        policy_version, policy_sha256, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        metric.metric_id,
                        operand["metric_id"],
                        operand["operand_role"],
                        int(operand["required"]),
                        registry.version,
                        registry.checksum,
                        now,
                    ),
                )
        conn.executemany(
            """
            INSERT INTO bridge_specialized_metric_applicability (
                security_id, metric_id, ticker, cohort_id, applicability_status,
                applicability_basis, reviewed, review_source_id, row_sha256,
                policy_version, policy_sha256, created_at_utc, updated_at_utc
            ) VALUES (
                :security_id, :metric_id, :ticker, :cohort_id, :applicability_status,
                :applicability_basis, :reviewed, :review_source_id, :row_sha256,
                :policy_version, :policy_sha256, :created_at_utc, :updated_at_utc
            )
            """,
            ({**row, "created_at_utc": now, "updated_at_utc": now} for row in applicability),
        )
        conn.executemany(
            """
            INSERT INTO fact_specialized_source_census (
                census_key, security_id, ticker, cohort_id, source_family,
                source_record_key, document_role, accession_number, form_type,
                period_end, availability_timestamp, source_url, source_id,
                discovery_status, source_metadata_sha256, content_sha256,
                terminal_reason, requested_metric_ids_json, census_version,
                census_sha256, production_eligible, calibration_eligible,
                created_at_utc, updated_at_utc
            ) VALUES (
                :census_key, :security_id, :ticker, :cohort_id, :source_family,
                :source_record_key, :document_role, :accession_number, :form_type,
                :period_end, :availability_timestamp, :source_url, :source_id,
                :discovery_status, :source_metadata_sha256, :content_sha256,
                :terminal_reason, :requested_metric_ids_json, :census_version,
                :census_sha256, :production_eligible, :calibration_eligible,
                :created_at_utc, :updated_at_utc
            )
            """,
            ({**row, "created_at_utc": now, "updated_at_utc": now} for row in source_rows),
        )
        _store_raw_contract(
            conn,
            path=registry.path,
            source_id="basic_materials_specialized_metric_policy",
            snapshot_date=registry.as_of_date,
            checksum=registry.checksum,
            row_count=len(registry.metrics),
            manifest_version=registry.version,
            now=now,
        )
        _store_raw_contract(
            conn,
            path=source_policy.path,
            source_id="basic_materials_specialized_source_census",
            snapshot_date=source_policy.as_of_date,
            checksum=source_policy.checksum,
            row_count=len(source_rows),
            manifest_version=source_policy.version,
            now=now,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return SpecializedContractLoadStats(
        metrics=len(registry.metrics),
        cohort_links=len(registry.metrics),
        operand_links=sum(len(metric.operands) for metric in registry.metrics),
        applicability_rows=len(applicability),
        applicability_review_rows=sum(row["applicability_status"] == "review_required" for row in applicability),
        source_census_rows=len(source_rows),
        sec_filing_rows=sum(
            bool(row["source_family"] == "sec_filing" and row["accession_number"])
            for row in source_rows
        ),
        discovery_required_rows=sum(row["discovery_status"] == "discovery_required" for row in source_rows),
        explicit_not_applicable_source_rows=sum(row["discovery_status"] == "not_applicable" for row in source_rows),
        raw_contract_payloads=2,
        metric_registry_sha256=registry.checksum,
        source_policy_sha256=source_policy.checksum,
    )


def validate_specialized_contract_database(
    conn: sqlite3.Connection,
    *,
    registry: SpecializedMetricRegistry,
    source_policy: SpecializedSourcePolicy,
) -> SpecializedContractValidationReport:
    assert_database_identity(conn)
    issues: list[dict[str, Any]] = []
    metric_rows = tuple(
        dict(row)
        for row in conn.execute(
            """
            SELECT m.metric_id, c.cohort_id, m.metric_role, c.applicability_mode,
                   c.coverage_tier, m.unit_family, m.period_type, m.dimension_family,
                   m.direction_hint, m.definition, m.formula,
                   m.definition_variants_json, c.source_families_json,
                   c.table_families_json, m.plausibility_json,
                   m.production_weight, m.scoring_eligible, m.calibration_eligible,
                   m.policy_version, m.policy_sha256
            FROM dim_specialized_metric m
            JOIN bridge_specialized_metric_cohort c ON c.metric_id = m.metric_id
            ORDER BY c.cohort_id, m.metric_id
            """
        ).fetchall()
    )
    applicability = tuple(
        dict(row)
        for row in conn.execute(
            """
            SELECT security_id, ticker, cohort_id, metric_id, applicability_status,
                   applicability_basis, reviewed, row_sha256, policy_version, policy_sha256
            FROM bridge_specialized_metric_applicability
            ORDER BY cohort_id, ticker, metric_id
            """
        ).fetchall()
    )
    source_census = tuple(
        dict(row)
        for row in conn.execute(
            """
            SELECT census_key, security_id, ticker, cohort_id, source_family,
                   source_record_key, document_role, accession_number, form_type,
                   period_end, availability_timestamp, source_url, source_id,
                   discovery_status, source_metadata_sha256, content_sha256,
                   terminal_reason, requested_metric_ids_json, census_version,
                   census_sha256, production_eligible, calibration_eligible
            FROM fact_specialized_source_census
            ORDER BY cohort_id, ticker, source_family, source_record_key
            """
        ).fetchall()
    )
    identity_count = int(conn.execute("SELECT COUNT(*) FROM dim_security").fetchone()[0])
    operand_count = int(conn.execute("SELECT COUNT(*) FROM bridge_specialized_metric_operand").fetchone()[0])
    expected_pairs = identity_count * len(registry.metrics)
    expected_operand_count = sum(len(metric.operands) for metric in registry.metrics)
    if len(metric_rows) != len(registry.metrics):
        issues.append({"severity": "error", "issue_code": "METRIC_COUNT_MISMATCH", "message": f"Expected {len(registry.metrics)} metric rows; found {len(metric_rows)}"})
    if operand_count != expected_operand_count:
        issues.append({"severity": "error", "issue_code": "OPERAND_COUNT_MISMATCH", "message": f"Expected {expected_operand_count} operand links; found {operand_count}"})
    if len(applicability) != expected_pairs:
        issues.append({"severity": "error", "issue_code": "APPLICABILITY_MATRIX_INCOMPLETE", "message": f"Expected {expected_pairs} identity-metric rows; found {len(applicability)}"})
    unsafe_metrics = sum(
        float(row["production_weight"]) != 0.0
        or int(row["scoring_eligible"]) != 0
        or int(row["calibration_eligible"]) != 0
        or row["policy_sha256"] != registry.checksum
        for row in metric_rows
    )
    unsafe_sources = sum(
        int(row["production_eligible"]) != 0
        or int(row["calibration_eligible"]) != 0
        or row["census_version"] != source_policy.version
        for row in source_census
    )
    if unsafe_metrics or unsafe_sources:
        issues.append({"severity": "error", "issue_code": "PROHIBITED_GATE_OR_HASH_STATE", "message": f"Unsafe metric rows={unsafe_metrics}; unsafe source rows={unsafe_sources}"})
    status_counts = dict(sorted(Counter(str(row["applicability_status"]) for row in applicability).items()))
    source_status_counts = dict(sorted(Counter(str(row["discovery_status"]) for row in source_census).items()))
    source_family_counts = dict(sorted(Counter(str(row["source_family"]) for row in source_census).items()))
    family_identity_counts = {
        str(row["source_family"]): int(row["n"])
        for row in conn.execute(
            """
            SELECT source_family, COUNT(DISTINCT security_id) AS n
            FROM fact_specialized_source_census GROUP BY source_family
            """
        ).fetchall()
    }
    incomplete_families = {
        family: family_identity_counts.get(family, 0)
        for family in SOURCE_FAMILIES
        if family_identity_counts.get(family, 0) != identity_count
    }
    if incomplete_families:
        issues.append({"severity": "error", "issue_code": "SOURCE_FAMILY_IDENTITY_ACCOUNTING_INCOMPLETE", "message": f"Every source family must account for every identity: {incomplete_families}"})
    review_count = status_counts.get("review_required", 0)
    if review_count:
        issues.append({"severity": "blocker", "issue_code": "ISSUER_METRIC_APPLICABILITY_REVIEW_OPEN", "message": f"{review_count} issuer-selective metric pairs require review before Stage 5A can seal"})
    open_sources = source_status_counts.get("identified_unhydrated", 0) + source_status_counts.get("discovery_required", 0)
    if open_sources:
        issues.append({"severity": "blocker", "issue_code": "SPECIALIZED_SOURCE_CENSUS_NOT_HYDRATED", "message": f"{open_sources} applicable source rows are not cached-and-hashed or terminally disposed"})
    contract_valid = not any(item["severity"] == "error" for item in issues)
    stage5a_sealed = contract_valid and not any(item["severity"] == "blocker" for item in issues)
    counts = {
        "identities": identity_count,
        "metrics": len(metric_rows),
        "operand_links": operand_count,
        "expected_applicability_rows": expected_pairs,
        "applicability_rows": len(applicability),
        "source_census_rows": len(source_census),
        "source_family_identity_pairs_expected": identity_count * len(SOURCE_FAMILIES),
        "source_family_identity_pairs_accounted": sum(family_identity_counts.values()),
        "open_applicability_reviews": review_count,
        "open_source_rows": open_sources,
    }
    return SpecializedContractValidationReport(
        contract_valid=contract_valid,
        stage5a_sealed=stage5a_sealed,
        registry_version=registry.version,
        registry_sha256=registry.checksum,
        source_policy_version=source_policy.version,
        source_policy_sha256=source_policy.checksum,
        counts=counts,
        applicability_status_counts=status_counts,
        source_status_counts=source_status_counts,
        source_family_counts=source_family_counts,
        issues=tuple(issues),
        metrics=metric_rows,
        applicability=applicability,
        source_census=source_census,
    )


def write_specialized_contract_reports(
    report: SpecializedContractValidationReport,
    *,
    report_dir: str | Path,
) -> dict[str, str]:
    output = Path(report_dir).resolve(strict=False)
    output.mkdir(parents=True, exist_ok=True)
    paths = {
        "summary": output / "specialized_contract_summary.json",
        "metrics": output / "specialized_metric_registry.csv",
        "applicability": output / "specialized_metric_applicability.csv",
        "applicability_review": output / "specialized_metric_applicability_review.csv",
        "source_census": output / "specialized_source_census.csv",
        "source_gaps": output / "specialized_source_gaps.csv",
        "artifact_manifest": output / "artifact_manifest.json",
    }
    atomic_write_json(paths["summary"], report.summary_dict())
    metric_fields = tuple(report.metrics[0]) if report.metrics else ("metric_id",)
    applicability_fields = tuple(report.applicability[0]) if report.applicability else ("ticker", "metric_id")
    source_fields = tuple(report.source_census[0]) if report.source_census else ("ticker", "source_family")
    atomic_write_csv(paths["metrics"], report.metrics, metric_fields)
    atomic_write_csv(paths["applicability"], report.applicability, applicability_fields)
    atomic_write_csv(
        paths["applicability_review"],
        (row for row in report.applicability if row["applicability_status"] == "review_required"),
        applicability_fields,
    )
    atomic_write_csv(paths["source_census"], report.source_census, source_fields)
    atomic_write_csv(
        paths["source_gaps"],
        (
            row
            for row in report.source_census
            if row["discovery_status"] in {"identified_unhydrated", "discovery_required"}
        ),
        source_fields,
    )
    manifest = {
        "registry_sha256": report.registry_sha256,
        "source_policy_sha256": report.source_policy_sha256,
        "stage5a_sealed": report.stage5a_sealed,
        "artifacts": [
            {
                "name": name,
                "path": str(path),
                "sha256": _file_hash(path),
                "byte_size": path.stat().st_size,
            }
            for name, path in paths.items()
            if name != "artifact_manifest"
        ],
    }
    atomic_write_json(paths["artifact_manifest"], manifest)
    return {name: str(path) for name, path in paths.items()}

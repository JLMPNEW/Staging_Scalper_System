"""Fixture-first F1 parser contract and evidence-driven retry scheduler."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from basic_materials import MODEL_FAMILY, SECTOR
from basic_materials.core.atomic_io import atomic_write_csv, atomic_write_json
from basic_materials.core.specialized_contract import (
    SpecializedMetric,
    SpecializedMetricRegistry,
    SpecializedSourcePolicy,
)


class SpecializedParserContractError(ValueError):
    """Raised when the fixture-only parser contract is malformed or unsafe."""


@dataclass(frozen=True)
class ParserAdapter:
    adapter_id: str
    order: int
    table_families: tuple[str, ...]


@dataclass(frozen=True)
class SpecializedParserPolicy:
    path: Path
    checksum: str
    version: str
    as_of_date: str
    metric_contract_version: str
    source_contract_version: str
    maximum_full_passes: int
    maximum_residual_passes: int
    adapters: tuple[ParserAdapter, ...]
    required_case_types: tuple[str, ...]
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class ParserFixture:
    case_id: str
    case_type: str
    security_cohort: str
    metric_id: str
    table_family: str
    source_family: str
    unit_family: str
    period_type: str
    definition_variant: str
    numeric_value: float
    period_end: str
    availability_timestamp: str
    score_date: str
    source_id: str
    source_location: str
    scope_status: str
    amendment_status: str
    expected_disposition: str
    expected_issue_code: str


@dataclass(frozen=True)
class ParserFixtureBundle:
    path: Path
    checksum: str
    version: str
    as_of_date: str
    parser_policy_version: str
    cases: tuple[ParserFixture, ...]


@dataclass(frozen=True)
class ParserContractReport:
    contract_valid: bool
    fixture_gate_passed: bool
    production_execution_allowed: bool
    policy_version: str
    policy_sha256: str
    fixture_version: str
    fixture_sha256: str
    counts: Mapping[str, Any]
    issues: tuple[Mapping[str, Any], ...]
    adapter_rows: tuple[Mapping[str, Any], ...]
    fixture_rows: tuple[Mapping[str, Any], ...]
    pass_strategy_rows: tuple[Mapping[str, Any], ...]

    def summary_dict(self) -> dict[str, Any]:
        return {
            "contract_valid": self.contract_valid,
            "fixture_gate_passed": self.fixture_gate_passed,
            "production_execution_allowed": self.production_execution_allowed,
            "policy_version": self.policy_version,
            "policy_sha256": self.policy_sha256,
            "fixture_version": self.fixture_version,
            "fixture_sha256": self.fixture_sha256,
            "counts": dict(self.counts),
            "issue_count": len(self.issues),
            "issues": [dict(row) for row in self.issues],
        }


ADAPTER_FIELDS = (
    "adapter_order",
    "adapter_id",
    "cohort_id",
    "metric_id",
    "metric_role",
    "coverage_tier",
    "table_family",
)

FIXTURE_FIELDS = (
    "case_id",
    "case_type",
    "metric_id",
    "actual_disposition",
    "actual_issue_code",
    "expected_disposition",
    "expected_issue_code",
    "passed",
)

PASS_STRATEGY_FIELDS = (
    "sequence",
    "pass_type",
    "physical_decode_allowed",
    "maximum_per_content_hash",
    "required_change",
    "scope",
)



def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SpecializedParserContractError(f"{context} must be a mapping")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    actual = set(value)
    if actual != expected:
        raise SpecializedParserContractError(
            f"{context} keys differ; missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )


def _strings(value: Any, context: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise SpecializedParserContractError(f"{context} must be a list")
    result = tuple(str(item).strip() for item in value)
    if not result or any(not item for item in result) or len(result) != len(set(result)):
        raise SpecializedParserContractError(f"{context} must contain unique non-empty strings")
    return result


def _iso_date(value: Any, context: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise SpecializedParserContractError(f"{context} must be an ISO date") from exc


def _iso_timestamp(value: Any, context: str) -> str:
    raw = str(value).strip()
    try:
        datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SpecializedParserContractError(f"{context} must be an ISO timestamp") from exc
    return raw


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_specialized_parser_policy(path: str | Path) -> SpecializedParserPolicy:
    policy_path = Path(path).resolve()
    payload = policy_path.read_bytes()
    root = _mapping(yaml.safe_load(payload.decode("utf-8")), "specialized parser policy")
    _exact_keys(
        root,
        {
            "policy_version",
            "contract_as_of_date",
            "model_family",
            "sector",
            "metric_contract_version",
            "source_contract_version",
            "state",
            "pass_budget",
            "content_controls",
            "adapter_sequence",
            "fixture_controls",
            "required_flags",
        },
        "specialized parser policy",
    )
    if root["policy_version"] != "basic_materials_specialized_parser_contract_v1":
        raise SpecializedParserContractError("Unsupported specialized parser policy version")
    if root["model_family"] != MODEL_FAMILY or root["sector"] != SECTOR:
        raise SpecializedParserContractError("Specialized parser policy identity is invalid")
    if root["state"] != "f1_fixture_only_production_disabled":
        raise SpecializedParserContractError("Specialized parser policy must remain fixture-only")
    budget = _mapping(root["pass_budget"], "pass_budget")
    _exact_keys(
        budget,
        {
            "maximum_full_passes_per_content_hash",
            "maximum_residual_passes_per_content_hash",
            "blind_reparse_allowed",
            "completed_content_reparse_allowed",
            "residual_requires_new_source_hash_or_rule_version",
            "later_improvement_requires_new_contract_version",
        },
        "pass_budget",
    )
    if (
        int(budget["maximum_full_passes_per_content_hash"]) != 1
        or int(budget["maximum_residual_passes_per_content_hash"]) != 1
        or budget["blind_reparse_allowed"] is not False
        or budget["completed_content_reparse_allowed"] is not False
        or budget["residual_requires_new_source_hash_or_rule_version"] is not True
        or budget["later_improvement_requires_new_contract_version"] is not True
    ):
        raise SpecializedParserContractError("Specialized parser pass budget is invalid")
    content = _mapping(root["content_controls"], "content_controls")
    _exact_keys(
        content,
        {
            "content_address_algorithm",
            "compile_unique_content_once",
            "semantic_cache_immutable",
            "cache_only_policy_replay",
            "source_calls_during_policy_replay",
        },
        "content_controls",
    )
    if (
        content["content_address_algorithm"] != "sha256"
        or content["compile_unique_content_once"] is not True
        or content["semantic_cache_immutable"] is not True
        or content["cache_only_policy_replay"] is not True
        or content["source_calls_during_policy_replay"] is not False
    ):
        raise SpecializedParserContractError("Specialized parser content controls are invalid")

    raw_adapters = root["adapter_sequence"]
    if not isinstance(raw_adapters, list) or not raw_adapters:
        raise SpecializedParserContractError("adapter_sequence must be a non-empty list")
    adapters: list[ParserAdapter] = []
    seen_ids: set[str] = set()
    seen_families: set[str] = set()
    for index, raw_adapter in enumerate(raw_adapters, start=1):
        adapter = _mapping(raw_adapter, f"adapter_sequence[{index - 1}]")
        _exact_keys(adapter, {"adapter_id", "order", "table_families"}, f"adapter {index}")
        adapter_id = str(adapter["adapter_id"]).strip()
        families = _strings(adapter["table_families"], f"{adapter_id}.table_families")
        if not adapter_id or adapter_id in seen_ids or int(adapter["order"]) != index:
            raise SpecializedParserContractError("Adapter IDs must be unique and orders contiguous")
        duplicate = seen_families & set(families)
        if duplicate:
            raise SpecializedParserContractError(f"Table families assigned to multiple adapters: {sorted(duplicate)}")
        seen_ids.add(adapter_id)
        seen_families.update(families)
        adapters.append(ParserAdapter(adapter_id=adapter_id, order=index, table_families=families))
    fixture_controls = _mapping(root["fixture_controls"], "fixture_controls")
    _exact_keys(
        fixture_controls,
        {
            "required_case_types",
            "every_case_must_match_expected_disposition",
            "production_document_execution_allowed",
        },
        "fixture_controls",
    )
    required_types = _strings(fixture_controls["required_case_types"], "required_case_types")
    if (
        fixture_controls["every_case_must_match_expected_disposition"] is not True
        or fixture_controls["production_document_execution_allowed"] is not False
    ):
        raise SpecializedParserContractError("Fixture controls are invalid")
    flags = _mapping(root["required_flags"], "required_flags")
    if not flags or any(value is not False for value in flags.values()):
        raise SpecializedParserContractError("F1 fixture-only flags must remain false")
    return SpecializedParserPolicy(
        path=policy_path,
        checksum=hashlib.sha256(payload).hexdigest(),
        version=str(root["policy_version"]),
        as_of_date=_iso_date(root["contract_as_of_date"], "contract_as_of_date"),
        metric_contract_version=str(root["metric_contract_version"]),
        source_contract_version=str(root["source_contract_version"]),
        maximum_full_passes=1,
        maximum_residual_passes=1,
        adapters=tuple(adapters),
        required_case_types=required_types,
        payload=dict(root),
    )



def load_specialized_parser_fixtures(path: str | Path) -> ParserFixtureBundle:
    fixture_path = Path(path).resolve()
    payload = fixture_path.read_bytes()
    root = _mapping(yaml.safe_load(payload.decode("utf-8")), "specialized parser fixtures")
    _exact_keys(
        root,
        {
            "fixture_version",
            "contract_as_of_date",
            "model_family",
            "sector",
            "parser_policy_version",
            "cases",
        },
        "specialized parser fixtures",
    )
    if root["fixture_version"] != "basic_materials_specialized_parser_fixtures_v1":
        raise SpecializedParserContractError("Unsupported specialized parser fixture version")
    if root["model_family"] != MODEL_FAMILY or root["sector"] != SECTOR:
        raise SpecializedParserContractError("Specialized parser fixture identity is invalid")
    raw_cases = root["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise SpecializedParserContractError("Specialized parser fixtures must contain cases")
    expected_keys = {
        "case_id",
        "case_type",
        "security_cohort",
        "metric_id",
        "table_family",
        "source_family",
        "unit_family",
        "period_type",
        "definition_variant",
        "numeric_value",
        "period_end",
        "availability_timestamp",
        "score_date",
        "source_id",
        "source_location",
        "scope_status",
        "amendment_status",
        "expected_disposition",
        "expected_issue_code",
    }
    cases: list[ParserFixture] = []
    seen_ids: set[str] = set()
    for index, raw_case in enumerate(raw_cases):
        item = _mapping(raw_case, f"cases[{index}]")
        _exact_keys(item, expected_keys, f"cases[{index}]")
        case_id = str(item["case_id"]).strip()
        if not case_id or case_id in seen_ids:
            raise SpecializedParserContractError(f"Invalid or duplicate fixture case_id: {case_id!r}")
        if isinstance(item["numeric_value"], bool) or not isinstance(item["numeric_value"], (int, float)):
            raise SpecializedParserContractError(f"Fixture {case_id} numeric_value must be numeric")
        disposition = str(item["expected_disposition"]).strip()
        if disposition not in {"extracted", "review_required", "rejected", "excluded_asof"}:
            raise SpecializedParserContractError(f"Fixture {case_id} has invalid expected disposition")
        seen_ids.add(case_id)
        cases.append(
            ParserFixture(
                case_id=case_id,
                case_type=str(item["case_type"]).strip(),
                security_cohort=str(item["security_cohort"]).strip(),
                metric_id=str(item["metric_id"]).strip(),
                table_family=str(item["table_family"]).strip(),
                source_family=str(item["source_family"]).strip(),
                unit_family=str(item["unit_family"]).strip(),
                period_type=str(item["period_type"]).strip(),
                definition_variant=str(item["definition_variant"]).strip(),
                numeric_value=float(item["numeric_value"]),
                period_end=_iso_date(item["period_end"], f"{case_id}.period_end"),
                availability_timestamp=_iso_timestamp(
                    item["availability_timestamp"], f"{case_id}.availability_timestamp"
                ),
                score_date=_iso_date(item["score_date"], f"{case_id}.score_date"),
                source_id=str(item["source_id"]).strip(),
                source_location=str(item["source_location"]).strip(),
                scope_status=str(item["scope_status"]).strip(),
                amendment_status=str(item["amendment_status"]).strip(),
                expected_disposition=disposition,
                expected_issue_code=str(item["expected_issue_code"]).strip(),
            )
        )
    return ParserFixtureBundle(
        path=fixture_path,
        checksum=hashlib.sha256(payload).hexdigest(),
        version=str(root["fixture_version"]),
        as_of_date=_iso_date(root["contract_as_of_date"], "contract_as_of_date"),
        parser_policy_version=str(root["parser_policy_version"]),
        cases=tuple(cases),
    )


def _fixture_disposition(
    fixture: ParserFixture,
    *,
    metric_by_id: Mapping[str, SpecializedMetric],
) -> tuple[str, str]:
    metric = metric_by_id.get(fixture.metric_id)
    if metric is None:
        return "rejected", "UNKNOWN_METRIC"
    if not fixture.source_id or not fixture.source_location:
        return "rejected", "MISSING_SOURCE_ID"
    if fixture.security_cohort != metric.cohort_id:
        return "rejected", "CROSS_COHORT_METRIC"
    if fixture.table_family not in metric.table_families:
        return "rejected", "TABLE_FAMILY_NOT_ALLOWED"
    if metric.metric_role != "derived" and fixture.source_family not in metric.source_families:
        return "rejected", "SOURCE_FAMILY_NOT_ALLOWED"
    if fixture.amendment_status == "superseded":
        return "rejected", "SUPERSEDED_SOURCE"
    if fixture.amendment_status not in {"current", "amended"}:
        return "review_required", "AMENDMENT_STATUS_REVIEW_REQUIRED"
    if fixture.unit_family != metric.unit_family:
        return "review_required", "UNIT_FAMILY_MISMATCH"
    if fixture.period_type != metric.period_type:
        return "review_required", "PERIOD_TYPE_MISMATCH"
    if fixture.definition_variant not in metric.definition_variants:
        return "review_required", "DEFINITION_VARIANT_REVIEW_REQUIRED"
    if fixture.scope_status != "exact":
        return "review_required", "SCOPE_REVIEW_REQUIRED"
    plausibility = metric.plausibility
    minimum = plausibility["minimum"]
    maximum = plausibility["maximum"]
    if (
        (minimum is not None and fixture.numeric_value < float(minimum))
        or (maximum is not None and fixture.numeric_value > float(maximum))
        or (not plausibility["allow_negative"] and fixture.numeric_value < 0)
    ):
        return "review_required", "PLAUSIBILITY_REVIEW_REQUIRED"
    if fixture.availability_timestamp[:10] < fixture.period_end:
        return "rejected", "AVAILABILITY_PRECEDES_PERIOD_END"
    if fixture.availability_timestamp[:10] > fixture.score_date:
        return "excluded_asof", "FUTURE_AVAILABILITY"
    return "extracted", ""


def next_parse_action(
    *,
    full_passes: int,
    residual_passes: int,
    unresolved_pairs: int,
    new_source_hashes: int = 0,
    parser_rule_version_changed: bool = False,
) -> str:
    """Return the only permitted next physical or policy action."""

    values = (full_passes, residual_passes, unresolved_pairs, new_source_hashes)
    if any(value < 0 for value in values):
        raise SpecializedParserContractError("Parser pass and gap counts cannot be negative")
    if full_passes > 1 or residual_passes > 1:
        raise SpecializedParserContractError("Parser attempt count exceeds the contract budget")
    if full_passes == 0:
        if residual_passes:
            raise SpecializedParserContractError("A residual pass cannot precede the full pass")
        return "full_pass"
    if unresolved_pairs == 0:
        return "complete"
    evidence_changed = new_source_hashes > 0 or parser_rule_version_changed
    if residual_passes == 0:
        return "residual_pass" if evidence_changed else "policy_review_only"
    return "new_contract_version_required" if evidence_changed else "coverage_disposition_required"



def validate_specialized_parser_contract(
    *,
    policy: SpecializedParserPolicy,
    fixtures: ParserFixtureBundle,
    registry: SpecializedMetricRegistry,
    source_policy: SpecializedSourcePolicy,
) -> ParserContractReport:
    issues: list[dict[str, Any]] = []
    if not (policy.as_of_date == fixtures.as_of_date == registry.as_of_date == source_policy.as_of_date):
        issues.append(
            {
                "severity": "error",
                "issue_code": "CONTRACT_ASOF_MISMATCH",
                "message": "Parser, fixture, metric, and source contracts do not share one as-of date",
            }
        )
    if policy.metric_contract_version != registry.version:
        issues.append(
            {
                "severity": "error",
                "issue_code": "METRIC_CONTRACT_VERSION_MISMATCH",
                "message": "Parser policy metric contract version is stale",
            }
        )
    if policy.source_contract_version != source_policy.version:
        issues.append(
            {
                "severity": "error",
                "issue_code": "SOURCE_CONTRACT_VERSION_MISMATCH",
                "message": "Parser policy source contract version is stale",
            }
        )
    if fixtures.parser_policy_version != policy.version:
        issues.append(
            {
                "severity": "error",
                "issue_code": "FIXTURE_POLICY_VERSION_MISMATCH",
                "message": "Fixture bundle does not target the active parser policy",
            }
        )
    hydration = _mapping(source_policy.payload["hydration_controls"], "source hydration controls")
    if (
        int(hydration["maximum_full_parse_attempts_per_content_hash"]) != policy.maximum_full_passes
        or int(hydration["maximum_residual_parse_attempts_per_content_hash"])
        != policy.maximum_residual_passes
    ):
        issues.append(
            {
                "severity": "error",
                "issue_code": "PASS_BUDGET_CONFLICT",
                "message": "Parser and source contracts have different pass budgets",
            }
        )

    adapter_by_family = {
        family: adapter
        for adapter in policy.adapters
        for family in adapter.table_families
    }
    expected_families = {
        family
        for metric in registry.metrics
        for family in metric.table_families
    }
    missing_families = sorted(expected_families - set(adapter_by_family))
    extra_families = sorted(set(adapter_by_family) - expected_families)
    if missing_families or extra_families:
        issues.append(
            {
                "severity": "error",
                "issue_code": "ADAPTER_TABLE_FAMILY_COVERAGE_INVALID",
                "message": f"missing={missing_families}; extra={extra_families}",
            }
        )
    adapter_rows: list[dict[str, Any]] = []
    metric_by_id = {metric.metric_id: metric for metric in registry.metrics}
    tier_by_metric = {
        metric.metric_id: metric.coverage_tier
        for metric in registry.metrics
    }
    for metric in registry.metrics:
        for family in metric.table_families:
            adapter = adapter_by_family.get(family)
            adapter_rows.append(
                {
                    "adapter_order": adapter.order if adapter else "",
                    "adapter_id": adapter.adapter_id if adapter else "",
                    "cohort_id": metric.cohort_id,
                    "metric_id": metric.metric_id,
                    "metric_role": metric.metric_role,
                    "coverage_tier": tier_by_metric[metric.metric_id],
                    "table_family": family,
                }
            )

    fixture_types = {case.case_type for case in fixtures.cases}
    missing_case_types = sorted(set(policy.required_case_types) - fixture_types)
    if missing_case_types:
        issues.append(
            {
                "severity": "error",
                "issue_code": "FIXTURE_CASE_COVERAGE_INCOMPLETE",
                "message": f"Missing fixture case types: {missing_case_types}",
            }
        )
    fixture_rows: list[dict[str, Any]] = []
    for case in fixtures.cases:
        disposition, issue_code = _fixture_disposition(case, metric_by_id=metric_by_id)
        passed = disposition == case.expected_disposition and issue_code == case.expected_issue_code
        fixture_rows.append(
            {
                "case_id": case.case_id,
                "case_type": case.case_type,
                "metric_id": case.metric_id,
                "actual_disposition": disposition,
                "actual_issue_code": issue_code,
                "expected_disposition": case.expected_disposition,
                "expected_issue_code": case.expected_issue_code,
                "passed": int(passed),
            }
        )
    failed_fixtures = [row for row in fixture_rows if not row["passed"]]
    if failed_fixtures:
        issues.append(
            {
                "severity": "error",
                "issue_code": "FIXTURE_DISPOSITION_MISMATCH",
                "message": f"{len(failed_fixtures)} fixture cases did not match expected dispositions",
            }
        )

    pass_strategy_rows = (
        {
            "sequence": 1,
            "pass_type": "full",
            "physical_decode_allowed": 1,
            "maximum_per_content_hash": 1,
            "required_change": "initial hash-sealed all-document plan",
            "scope": "all unresolved applicable metric-document pairs",
        },
        {
            "sequence": 2,
            "pass_type": "policy_replay",
            "physical_decode_allowed": 0,
            "maximum_per_content_hash": 0,
            "required_change": "none; uses immutable candidates and semantic cache",
            "scope": "all adjudication, conflict, and definition-policy changes",
        },
        {
            "sequence": 3,
            "pass_type": "residual",
            "physical_decode_allowed": 1,
            "maximum_per_content_hash": 1,
            "required_change": "new source hash or versioned parser rule",
            "scope": "only unresolved pairs; completed hashes excluded",
        },
        {
            "sequence": 4,
            "pass_type": "versioned_delta",
            "physical_decode_allowed": 0,
            "maximum_per_content_hash": 0,
            "required_change": "new parser contract version and new immutable work keys",
            "scope": "future release only; never mutates the original evidence run",
        },
    )
    contract_valid = not issues
    fixture_gate_passed = contract_valid and not failed_fixtures
    counts = {
        "metrics": len(registry.metrics),
        "core_metrics": sum(metric.coverage_tier == "core" for metric in registry.metrics),
        "adapters": len(policy.adapters),
        "expected_table_families": len(expected_families),
        "assigned_table_families": len(adapter_by_family),
        "metric_table_family_links": len(adapter_rows),
        "fixture_cases": len(fixtures.cases),
        "fixture_case_types": len(fixture_types),
        "fixture_failures": len(failed_fixtures),
        "maximum_physical_parse_passes_per_contract_version": (
            policy.maximum_full_passes + policy.maximum_residual_passes
        ),
    }
    return ParserContractReport(
        contract_valid=contract_valid,
        fixture_gate_passed=fixture_gate_passed,
        production_execution_allowed=False,
        policy_version=policy.version,
        policy_sha256=policy.checksum,
        fixture_version=fixtures.version,
        fixture_sha256=fixtures.checksum,
        counts=counts,
        issues=tuple(issues),
        adapter_rows=tuple(adapter_rows),
        fixture_rows=tuple(fixture_rows),
        pass_strategy_rows=pass_strategy_rows,
    )



def write_specialized_parser_contract_reports(
    report: ParserContractReport,
    *,
    report_dir: str | Path,
) -> dict[str, str]:
    output = Path(report_dir).resolve(strict=False)
    output.mkdir(parents=True, exist_ok=True)
    paths = {
        "summary": output / "specialized_parser_contract_summary.json",
        "adapter_matrix": output / "specialized_parser_adapter_matrix.csv",
        "fixture_results": output / "specialized_parser_fixture_results.csv",
        "pass_strategy": output / "specialized_parser_pass_strategy.csv",
        "artifact_manifest": output / "artifact_manifest.json",
    }
    atomic_write_json(paths["summary"], report.summary_dict())
    atomic_write_csv(paths["adapter_matrix"], report.adapter_rows, ADAPTER_FIELDS)
    atomic_write_csv(paths["fixture_results"], report.fixture_rows, FIXTURE_FIELDS)
    atomic_write_csv(paths["pass_strategy"], report.pass_strategy_rows, PASS_STRATEGY_FIELDS)
    manifest = {
        "policy_version": report.policy_version,
        "policy_sha256": report.policy_sha256,
        "fixture_version": report.fixture_version,
        "fixture_sha256": report.fixture_sha256,
        "fixture_gate_passed": report.fixture_gate_passed,
        "production_execution_allowed": report.production_execution_allowed,
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

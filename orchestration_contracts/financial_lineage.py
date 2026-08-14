from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml


POLICY_DISABLED = "disabled"
POLICY_CANDIDATE_ONLY = "candidate_only"
POLICY_STRICT_UNIVERSE = "strict_universe"
POLICY_MODES = frozenset(
    {POLICY_DISABLED, POLICY_CANDIDATE_ONLY, POLICY_STRICT_UNIVERSE}
)
SAFE_LINEAGE_STATUSES = frozenset({"INCORPORATED"})
DEFAULT_MIN_CORE_METRIC_COUNT = 2

LINEAGE_FIELDS = (
    "financial_lineage_checked_asof_date",
    "financial_lineage_status",
    "financial_lineage_gate",
    "financial_lineage_classification",
    "latest_material_financial_filing_date",
    "latest_material_financial_form",
    "latest_material_financial_accession",
    "latest_material_financial_report_date",
    "incorporated_financial_filing_date",
    "incorporated_financial_accession",
    "incorporated_financial_report_date",
    "incorporated_financial_core_metric_count",
    "financial_lineage_reason",
)

DEFAULT_POLICY_REGISTRY = (
    Path(__file__).resolve().parents[1]
    / "orchestration"
    / "financial_lineage_policy.yaml"
)


@dataclass(frozen=True)
class SectorLineagePolicy:
    model_family: str
    enabled: bool
    production_policy: str
    research_policy: str
    historical_policy: str
    min_core_metric_count: int
    policy_version: str

    def mode_for(self, context: str) -> str:
        normalized = context.strip().lower()
        if not self.enabled:
            return POLICY_DISABLED
        if normalized == "production":
            return self.production_policy
        if normalized == "research":
            return self.research_policy
        if normalized in {"history", "historical", "pit"}:
            return self.historical_policy
        raise ValueError(f"Unknown financial-lineage context: {context!r}")


@dataclass(frozen=True)
class LineageIssue:
    ticker: str
    code: str
    detail: str
    blocking: bool

    def render(self) -> str:
        suffix = f":{self.detail}" if self.detail else ""
        return f"{self.ticker}:{self.code}{suffix}"


@dataclass(frozen=True)
class LineageEvaluation:
    policy_mode: str
    row_count: int
    incorporated_count: int
    unresolved_count: int
    issues: tuple[LineageIssue, ...]

    @property
    def blocking_issues(self) -> tuple[LineageIssue, ...]:
        return tuple(issue for issue in self.issues if issue.blocking)

    @property
    def acceptance(self) -> str:
        return "PASS" if not self.blocking_issues else "FAIL"

    @property
    def errors(self) -> list[str]:
        return [issue.render() for issue in self.blocking_issues]

    @property
    def issue_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(issue.code for issue in self.issues).items()))


def _policy_mode(raw: object, *, field: str) -> str:
    value = str(raw or "").strip().lower()
    if value not in POLICY_MODES:
        raise ValueError(f"Invalid {field}={value!r}; expected one of {sorted(POLICY_MODES)}")
    return value


def _positive_int(raw: object, *, field: str) -> int:
    if not isinstance(raw, (str, int, float)):
        raise ValueError(f"Invalid {field}={raw!r}; expected a positive integer")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid {field}={raw!r}; expected a positive integer") from exc
    if value <= 0:
        raise ValueError(f"Invalid {field}={raw!r}; expected a positive integer")
    return value


def load_policy_registry(path: Path = DEFAULT_POLICY_REGISTRY) -> dict[str, SectorLineagePolicy]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ValueError(f"Financial-lineage policy registry unreadable: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Financial-lineage policy registry must be an object: {path}")
    policy_version = str(payload.get("policy_version") or "").strip()
    if not policy_version:
        raise ValueError(f"Financial-lineage policy registry lacks policy_version: {path}")
    defaults = payload.get("defaults") or {}
    sectors = payload.get("sectors") or {}
    if not isinstance(defaults, dict) or not isinstance(sectors, dict):
        raise ValueError(f"Financial-lineage defaults/sectors must be objects: {path}")

    output: dict[str, SectorLineagePolicy] = {}
    for raw_family, raw_config in sectors.items():
        family = str(raw_family).strip()
        if not family or not isinstance(raw_config, dict):
            raise ValueError(f"Invalid financial-lineage sector entry: {raw_family!r}")
        config = {**defaults, **raw_config}
        enabled = bool(config.get("enabled", False))
        output[family] = SectorLineagePolicy(
            model_family=family,
            enabled=enabled,
            production_policy=_policy_mode(
                config.get("production_policy", POLICY_DISABLED),
                field=f"{family}.production_policy",
            ),
            research_policy=_policy_mode(
                config.get("research_policy", POLICY_DISABLED),
                field=f"{family}.research_policy",
            ),
            historical_policy=_policy_mode(
                config.get("historical_policy", POLICY_DISABLED),
                field=f"{family}.historical_policy",
            ),
            min_core_metric_count=_positive_int(
                config.get("min_core_metric_count", DEFAULT_MIN_CORE_METRIC_COUNT),
                field=f"{family}.min_core_metric_count",
            ),
            policy_version=policy_version,
        )
    return output


def policy_for_model_family(
    model_family: str,
    *,
    registry_path: Path = DEFAULT_POLICY_REGISTRY,
) -> SectorLineagePolicy:
    family = str(model_family or "").strip()
    policies = load_policy_registry(registry_path)
    if family in policies:
        return policies[family]
    return SectorLineagePolicy(
        model_family=family,
        enabled=False,
        production_policy=POLICY_DISABLED,
        research_policy=POLICY_DISABLED,
        historical_policy=POLICY_DISABLED,
        min_core_metric_count=DEFAULT_MIN_CORE_METRIC_COUNT,
        policy_version=next(
            (policy.policy_version for policy in policies.values()),
            "financial_lineage_policy_unconfigured",
        ),
    )


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _candidate(row: Mapping[str, Any], fields: Sequence[str]) -> bool:
    return any(field in row and _truthy(row.get(field)) for field in fields)


def _valid_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def lineage_row_is_safe(
    row: Mapping[str, Any],
    *,
    expected_asof: str,
    min_core_metric_count: int = DEFAULT_MIN_CORE_METRIC_COUNT,
) -> bool:
    if any(field not in row for field in LINEAGE_FIELDS):
        return False
    if str(row.get("financial_lineage_checked_asof_date") or "").strip() != expected_asof:
        return False
    if str(row.get("financial_lineage_gate") or "").strip() != "1":
        return False
    if str(row.get("financial_lineage_status") or "").strip() not in SAFE_LINEAGE_STATUSES:
        return False
    if not str(row.get("latest_material_financial_accession") or "").strip():
        return False
    if not str(row.get("incorporated_financial_accession") or "").strip():
        return False
    try:
        count = int(float(str(row.get("incorporated_financial_core_metric_count") or "")))
    except ValueError:
        return False
    return count >= min_core_metric_count


def evaluate_financial_lineage_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    policy_mode: str,
    expected_asof: str | None = None,
    expected_asof_field: str = "",
    min_core_metric_count: int = DEFAULT_MIN_CORE_METRIC_COUNT,
    candidate_fields: Sequence[str] = (
        "portfolio_candidate_gate",
        "investable_eligible",
    ),
) -> LineageEvaluation:
    mode = _policy_mode(policy_mode, field="policy_mode")
    materialized = [dict(row) for row in rows]
    if mode == POLICY_DISABLED:
        return LineageEvaluation(mode, len(materialized), 0, 0, ())

    issues: list[LineageIssue] = []
    incorporated_count = 0
    unresolved_count = 0
    for row_number, row in enumerate(materialized, start=2):
        ticker = str(row.get("ticker") or f"row_{row_number}").strip()
        missing = [field for field in LINEAGE_FIELDS if field not in row]
        if missing:
            unresolved_count += 1
            issues.append(
                LineageIssue(
                    ticker,
                    "missing_financial_lineage_fields",
                    ",".join(missing),
                    True,
                )
            )
            continue

        row_expected_asof = (
            str(row.get(expected_asof_field) or "").strip()
            if expected_asof_field
            else str(expected_asof or "").strip()
        )
        checked_asof = str(row.get("financial_lineage_checked_asof_date") or "").strip()
        if not row_expected_asof or not _valid_iso_date(row_expected_asof):
            issues.append(
                LineageIssue(ticker, "invalid_expected_lineage_asof", row_expected_asof, True)
            )
        elif checked_asof != row_expected_asof:
            issues.append(
                LineageIssue(
                    ticker,
                    "lineage_checked_asof_mismatch",
                    f"checked={checked_asof!r},expected={row_expected_asof!r}",
                    True,
                )
            )

        gate = str(row.get("financial_lineage_gate") or "").strip()
        status = str(row.get("financial_lineage_status") or "").strip()
        if gate not in {"0", "1"}:
            unresolved_count += 1
            issues.append(LineageIssue(ticker, "invalid_financial_lineage_gate", gate, True))
            continue

        safe = lineage_row_is_safe(
            row,
            expected_asof=row_expected_asof,
            min_core_metric_count=min_core_metric_count,
        )
        if not safe:
            unresolved_count += 1
        else:
            incorporated_count += 1

        if gate == "0":
            is_candidate = _candidate(row, candidate_fields)
            if is_candidate:
                issues.append(
                    LineageIssue(
                        ticker,
                        "candidate_has_unresolved_financial_lineage",
                        status or "missing_status",
                        True,
                    )
                )
            if mode == POLICY_STRICT_UNIVERSE:
                issues.append(
                    LineageIssue(
                        ticker,
                        "material_financial_filing_unresolved",
                        status or "missing_status",
                        True,
                    )
                )
            elif not is_candidate:
                issues.append(
                    LineageIssue(
                        ticker,
                        "noncandidate_financial_lineage_unresolved",
                        status or "missing_status",
                        False,
                    )
                )
            continue

        if status not in SAFE_LINEAGE_STATUSES:
            issues.append(LineageIssue(ticker, "unsafe_open_lineage_status", status, True))

        for field in (
            "latest_material_financial_filing_date",
            "incorporated_financial_filing_date",
        ):
            value = str(row.get(field) or "").strip()
            if value and row_expected_asof and value > row_expected_asof:
                issues.append(
                    LineageIssue(
                        ticker,
                        "future_financial_lineage_date",
                        f"{field}={value},asof={row_expected_asof}",
                        True,
                    )
                )
        if not str(row.get("latest_material_financial_accession") or "").strip():
            issues.append(LineageIssue(ticker, "latest_material_accession_missing", "", True))
        if not str(row.get("incorporated_financial_accession") or "").strip():
            issues.append(LineageIssue(ticker, "incorporated_accession_missing", "", True))
        try:
            count = int(float(str(row.get("incorporated_financial_core_metric_count") or "")))
        except ValueError:
            count = 0
        if count < min_core_metric_count:
            issues.append(
                LineageIssue(
                    ticker,
                    "insufficient_incorporated_core_metrics",
                    f"count={count},minimum={min_core_metric_count}",
                    True,
                )
            )

    return LineageEvaluation(
        policy_mode=mode,
        row_count=len(materialized),
        incorporated_count=incorporated_count,
        unresolved_count=unresolved_count,
        issues=tuple(issues),
    )


def evaluation_manifest(
    evaluation: LineageEvaluation,
    *,
    policy: SectorLineagePolicy,
    context: str,
) -> dict[str, Any]:
    return {
        "acceptance": evaluation.acceptance,
        "policy_version": policy.policy_version,
        "policy_context": context,
        "policy_mode": evaluation.policy_mode,
        "row_count": evaluation.row_count,
        "incorporated_count": evaluation.incorporated_count,
        "unresolved_count": evaluation.unresolved_count,
        "blocking_issue_count": len(evaluation.blocking_issues),
        "issue_counts": evaluation.issue_counts,
        "blocking_issues": evaluation.errors[:25],
    }

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from industrials.core.config import load_yaml
from industrials.core.reports import write_csv_atomic, write_text_atomic
from industrials.transportation.contracts import file_sha256


MODEL_FAMILY = "transportation"
EXPECTED_ACTIVE_COUNT = 112
EXPECTED_DELISTED_COUNT = 48
EXPECTED_IDENTITY_COUNT = 160
EXPECTED_METRIC_COUNT = 90
EXPECTED_SCOPE_COUNT = EXPECTED_IDENTITY_COUNT * EXPECTED_METRIC_COUNT
EXPECTED_SUPPORTING_METRIC_COUNT = 7
EXPECTED_SUPPORTING_SCOPE_COUNT = EXPECTED_IDENTITY_COUNT * EXPECTED_SUPPORTING_METRIC_COUNT
EXPECTED_PACK_COUNTS = {
    "surface": 25,
    "air": 30,
    "marine": 17,
    "development": 18,
}
EXPECTED_LANE_COUNTS = {"DP": 77, "DP-D": 7, "FIN-D": 6}
DEVELOPMENT_COHORT = "development_stage_and_speculative_transport"

METRIC_FIELDS = (
    "metric_id",
    "metric_pack",
    "source_lane",
    "component",
    "applicability_tags",
    "unit_contract",
    "period_type",
    "max_staleness_days",
    "scoring_posture",
    "comparison_population",
    "bounds_policy",
    "formula",
    "discovery_status",
)
SUPPORTING_METRIC_FIELDS = (
    "support_metric_id",
    "consumer_metric_ids",
    "metric_pack",
    "applicability_tags",
    "unit_contract",
    "period_type",
    "max_staleness_days",
    "bounds_policy",
    "search_aliases",
    "discovery_status",
)
ARCHETYPE_FIELDS = (
    "ticker",
    "company_name",
    "universe_role",
    "calibration_cohort",
    "industry",
    "primary_archetype",
    "applicability_tags",
    "development_overlay",
    "review_status",
    "review_reason",
)
SCOPE_FIELDS = (
    "scope_version",
    "registry_version",
    "policy_version",
    "input_contract_hash",
    "ticker",
    "universe_role",
    "calibration_cohort",
    "industry",
    "primary_archetype",
    "applicability_tags",
    "development_overlay",
    "metric_id",
    "metric_pack",
    "source_lane",
    "applicability_status",
    "applicability_reason",
    "unit_contract",
    "period_type",
    "max_staleness_days",
    "scoring_posture",
    "comparison_population",
    "bounds_policy",
    "discovery_status",
)
SUPPORTING_SCOPE_FIELDS = (
    "scope_version",
    "registry_version",
    "policy_version",
    "input_contract_hash",
    "ticker",
    "universe_role",
    "calibration_cohort",
    "industry",
    "primary_archetype",
    "applicability_tags",
    "development_overlay",
    "support_metric_id",
    "consumer_metric_ids",
    "metric_pack",
    "source_lane",
    "applicability_status",
    "applicability_reason",
    "unit_contract",
    "period_type",
    "max_staleness_days",
    "bounds_policy",
    "discovery_status",
)
VALID_COMPONENTS = {
    "quality",
    "growth",
    "operating_efficiency",
    "capital_risk",
    "development_stage_risk",
}
VALID_PERIOD_TYPES = {
    "fiscal_period",
    "point_in_time",
    "forward_12m",
    "forward_or_fiscal_period",
    "milestone",
}
VALID_POSTURES = {"positive", "negative", "context"}
VALID_DISCOVERY_STATUSES = {"coverage_pending"}
METRIC_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [{str(key): str(value or "").strip() for key, value in row.items()} for row in reader]


def _field_errors(
    *,
    path: Path,
    rows: Sequence[Mapping[str, str]],
    expected_fields: Sequence[str],
) -> list[str]:
    if not rows:
        return [f"{path}: no data rows"]
    actual = tuple(rows[0])
    if actual != tuple(expected_fields):
        return [f"{path}: fields={actual!r} expected={tuple(expected_fields)!r}"]
    return []


def _pipe_values(value: object) -> tuple[str, ...]:
    return tuple(sorted({item.strip() for item in str(value or "").split("|") if item.strip()}))


def _canonical_rows_hash(rows: Iterable[Mapping[str, object]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(json.dumps(dict(row), ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def input_contract_hash(paths: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b":")
        digest.update(file_sha256(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def load_discovery_metrics(
    path: Path,
    *,
    allowed_tags: set[str],
) -> tuple[list[dict[str, str]], list[str]]:
    rows = _read_csv(path)
    errors = _field_errors(path=path, rows=rows, expected_fields=METRIC_FIELDS)
    if len(rows) != EXPECTED_METRIC_COUNT:
        errors.append(f"{path}: metric_count={len(rows)} expected={EXPECTED_METRIC_COUNT}")
    ids = [row.get("metric_id", "") for row in rows]
    duplicates = sorted(metric_id for metric_id, count in Counter(ids).items() if count > 1)
    if duplicates:
        errors.append(f"{path}: duplicate metric_id={duplicates}")
    pack_counts = Counter(row.get("metric_pack", "") for row in rows)
    if dict(pack_counts) != EXPECTED_PACK_COUNTS:
        errors.append(f"{path}: pack_counts={dict(pack_counts)} expected={EXPECTED_PACK_COUNTS}")
    lane_counts = Counter(row.get("source_lane", "") for row in rows)
    if dict(lane_counts) != EXPECTED_LANE_COUNTS:
        errors.append(f"{path}: lane_counts={dict(lane_counts)} expected={EXPECTED_LANE_COUNTS}")
    for index, row in enumerate(rows, start=2):
        metric_id = row.get("metric_id", "")
        if not METRIC_ID_RE.fullmatch(metric_id):
            errors.append(f"{path}:{index}: invalid metric_id={metric_id!r}")
        if row.get("component") not in VALID_COMPONENTS:
            errors.append(f"{path}:{index}: invalid component={row.get('component')!r}")
        if row.get("period_type") not in VALID_PERIOD_TYPES:
            errors.append(f"{path}:{index}: invalid period_type={row.get('period_type')!r}")
        if row.get("scoring_posture") not in VALID_POSTURES:
            errors.append(f"{path}:{index}: invalid scoring_posture={row.get('scoring_posture')!r}")
        if row.get("discovery_status") not in VALID_DISCOVERY_STATUSES:
            errors.append(f"{path}:{index}: invalid discovery_status={row.get('discovery_status')!r}")
        tags = set(_pipe_values(row.get("applicability_tags")))
        if not tags:
            errors.append(f"{path}:{index}: applicability_tags required")
        unknown_tags = sorted(tags - allowed_tags)
        if unknown_tags:
            errors.append(f"{path}:{index}: unknown applicability_tags={unknown_tags}")
        for field in (
            "unit_contract",
            "comparison_population",
            "bounds_policy",
            "max_staleness_days",
        ):
            if not row.get(field):
                errors.append(f"{path}:{index}: {field} required")
        try:
            if int(row.get("max_staleness_days") or 0) <= 0:
                raise ValueError
        except ValueError:
            errors.append(f"{path}:{index}: invalid max_staleness_days={row.get('max_staleness_days')!r}")
        formula = row.get("formula", "")
        if row.get("source_lane") in {"DP-D", "FIN-D"} and not formula:
            errors.append(f"{path}:{index}: derived source lane requires formula")
        if row.get("source_lane") == "DP" and formula:
            errors.append(f"{path}:{index}: direct parser metric must not define formula")
    return rows, errors


def load_supporting_metrics(
    path: Path,
    *,
    allowed_tags: set[str],
    discovery_metrics: Sequence[Mapping[str, str]],
) -> tuple[list[dict[str, str]], list[str]]:
    rows = _read_csv(path)
    errors = _field_errors(
        path=path,
        rows=rows,
        expected_fields=SUPPORTING_METRIC_FIELDS,
    )
    if len(rows) != EXPECTED_SUPPORTING_METRIC_COUNT:
        errors.append(f"{path}: supporting_metric_count={len(rows)} expected={EXPECTED_SUPPORTING_METRIC_COUNT}")
    ids = [row.get("support_metric_id", "") for row in rows]
    duplicates = sorted(metric_id for metric_id, count in Counter(ids).items() if count > 1)
    if duplicates:
        errors.append(f"{path}: duplicate support_metric_id={duplicates}")
    discovery_by_id = {row.get("metric_id", ""): row for row in discovery_metrics}
    derived_ids = {metric_id for metric_id, row in discovery_by_id.items() if row.get("source_lane") == "DP-D"}
    direct_ids = {metric_id for metric_id, row in discovery_by_id.items() if row.get("source_lane") == "DP"}
    collisions = sorted(set(ids) & set(discovery_by_id))
    if collisions:
        errors.append(f"{path}: supporting metrics collide with final metric ids={collisions}")
    covered_consumers: set[str] = set()
    for index, row in enumerate(rows, start=2):
        metric_id = row.get("support_metric_id", "")
        if not METRIC_ID_RE.fullmatch(metric_id):
            errors.append(f"{path}:{index}: invalid support_metric_id={metric_id!r}")
        if row.get("metric_pack") not in EXPECTED_PACK_COUNTS:
            errors.append(f"{path}:{index}: invalid metric_pack={row.get('metric_pack')!r}")
        if row.get("period_type") not in VALID_PERIOD_TYPES:
            errors.append(f"{path}:{index}: invalid period_type={row.get('period_type')!r}")
        if row.get("discovery_status") not in VALID_DISCOVERY_STATUSES:
            errors.append(f"{path}:{index}: invalid discovery_status={row.get('discovery_status')!r}")
        tags = set(_pipe_values(row.get("applicability_tags")))
        if not tags:
            errors.append(f"{path}:{index}: applicability_tags required")
        unknown_tags = sorted(tags - allowed_tags)
        if unknown_tags:
            errors.append(f"{path}:{index}: unknown applicability_tags={unknown_tags}")
        consumers = set(_pipe_values(row.get("consumer_metric_ids")))
        if not consumers:
            errors.append(f"{path}:{index}: consumer_metric_ids required")
        unknown_consumers = sorted(consumers - derived_ids)
        if unknown_consumers:
            errors.append(f"{path}:{index}: consumers must be DP-D metrics={unknown_consumers}")
        covered_consumers.update(consumers)
        for field in (
            "unit_contract",
            "bounds_policy",
            "max_staleness_days",
            "search_aliases",
        ):
            if not row.get(field):
                errors.append(f"{path}:{index}: {field} required")
        try:
            if int(row.get("max_staleness_days") or 0) <= 0:
                raise ValueError
        except ValueError:
            errors.append(f"{path}:{index}: invalid max_staleness_days={row.get('max_staleness_days')!r}")
    # These two derived outputs use histories of final direct metrics; every
    # other DP-D output must have one or more frozen supporting operands.
    history_derived = {
        "surface_volume_growth": {
            "rail_carload_growth",
            "rail_intermodal_volume_growth",
            "revenue_ton_miles_growth",
            "shipment_or_load_growth",
        },
        "fleet_capacity_growth": {"fleet_capacity"},
    }
    for consumer, operands in history_derived.items():
        if consumer not in derived_ids:
            errors.append(f"{path}: missing expected history-derived metric={consumer}")
        missing_direct = sorted(operands - direct_ids)
        if missing_direct:
            errors.append(f"{path}: history-derived operands are not direct metrics={missing_direct}")
    expected_supported = derived_ids - set(history_derived)
    if covered_consumers != expected_supported:
        errors.append(
            f"{path}: supporting consumer coverage={sorted(covered_consumers)} expected={sorted(expected_supported)}"
        )
    return rows, errors


def load_archetype_policy(path: Path) -> tuple[dict[str, Any], list[str]]:
    payload = load_yaml(path)
    errors: list[str] = []
    if str(payload.get("model_family") or "") != MODEL_FAMILY:
        errors.append(f"{path}: model_family must be {MODEL_FAMILY}")
    if int(payload.get("expected_identity_count") or 0) != EXPECTED_IDENTITY_COUNT:
        errors.append(f"{path}: expected_identity_count must be {EXPECTED_IDENTITY_COUNT}")
    allowed_primary = {str(value).strip() for value in payload.get("allowed_primary_archetypes", [])}
    allowed_tags = {str(value).strip() for value in payload.get("allowed_tags", [])}
    if not allowed_primary:
        errors.append(f"{path}: allowed_primary_archetypes required")
    if not allowed_tags:
        errors.append(f"{path}: allowed_tags required")
    if not allowed_primary.issubset(allowed_tags):
        errors.append(f"{path}: every primary archetype must also be an allowed tag")
    defaults = payload.get("cohort_industry_defaults")
    overrides = payload.get("ticker_overrides")
    if not isinstance(defaults, dict) or not defaults:
        errors.append(f"{path}: cohort_industry_defaults required")
    if not isinstance(overrides, dict):
        errors.append(f"{path}: ticker_overrides must be a mapping")
    return payload, errors


def load_universe(active_path: Path, delisted_path: Path) -> tuple[list[dict[str, str]], list[str]]:
    errors: list[str] = []
    active = _read_csv(active_path)
    delisted = _read_csv(delisted_path)
    if len(active) != EXPECTED_ACTIVE_COUNT:
        errors.append(f"{active_path}: active_count={len(active)} expected={EXPECTED_ACTIVE_COUNT}")
    if len(delisted) != EXPECTED_DELISTED_COUNT:
        errors.append(f"{delisted_path}: delisted_count={len(delisted)} expected={EXPECTED_DELISTED_COUNT}")
    rows = [
        {
            "ticker": row.get("ticker", "").upper(),
            "company_name": row.get("company_name", ""),
            "universe_role": "active",
            "calibration_cohort": row.get("calibration_cohort", ""),
            "industry": row.get("industry", ""),
        }
        for row in active
    ]
    rows.extend(
        {
            "ticker": row.get("ticker", "").upper(),
            "company_name": row.get("company", ""),
            "universe_role": "delisted_usable",
            "calibration_cohort": row.get("cohort", ""),
            "industry": row.get("industry", ""),
        }
        for row in delisted
    )
    tickers = [row["ticker"] for row in rows]
    duplicates = sorted(ticker for ticker, count in Counter(tickers).items() if count > 1)
    if duplicates:
        errors.append(f"active/delisted ticker overlap or duplicate={duplicates}")
    if len(rows) != EXPECTED_IDENTITY_COUNT:
        errors.append(f"identity_count={len(rows)} expected={EXPECTED_IDENTITY_COUNT}")
    for row in rows:
        for field in ("ticker", "company_name", "calibration_cohort", "industry"):
            if not row[field]:
                errors.append(f"{row.get('ticker') or '<blank>'}: missing {field}")
    return sorted(rows, key=lambda row: row["ticker"]), errors


def assign_archetypes(
    universe: Sequence[Mapping[str, str]],
    policy: Mapping[str, Any],
) -> tuple[list[dict[str, str]], list[str]]:
    errors: list[str] = []
    defaults = policy.get("cohort_industry_defaults") or {}
    overrides = policy.get("ticker_overrides") or {}
    allowed_primary = {str(value).strip() for value in policy.get("allowed_primary_archetypes", [])}
    allowed_tags = {str(value).strip() for value in policy.get("allowed_tags", [])}
    universe_tickers = {row["ticker"] for row in universe}
    unused_overrides = sorted(set(overrides) - universe_tickers)
    if unused_overrides:
        errors.append(f"archetype policy has unknown ticker overrides={unused_overrides}")
    assignments: list[dict[str, str]] = []
    for source in universe:
        ticker = source["ticker"]
        cohort = source["calibration_cohort"]
        industry = source["industry"]
        cohort_defaults = defaults.get(cohort)
        default = cohort_defaults.get(industry) if isinstance(cohort_defaults, dict) else None
        if not isinstance(default, dict):
            errors.append(f"{ticker}: no archetype default for cohort={cohort!r} industry={industry!r}")
            continue
        override = overrides.get(ticker)
        selected = override if isinstance(override, dict) else default
        primary = str(selected.get("primary_archetype") or "").strip()
        tags = {str(value).strip() for value in selected.get("tags", []) if str(value).strip()}
        if cohort == DEVELOPMENT_COHORT:
            tags.add("development_all")
        if primary:
            tags.add(primary)
        unknown_tags = sorted(tags - allowed_tags)
        if primary not in allowed_primary:
            errors.append(f"{ticker}: invalid primary_archetype={primary!r}")
        if unknown_tags:
            errors.append(f"{ticker}: unknown archetype tags={unknown_tags}")
        if not tags:
            errors.append(f"{ticker}: no applicability tags")
        assignments.append(
            {
                **dict(source),
                "primary_archetype": primary,
                "applicability_tags": "|".join(sorted(tags)),
                "development_overlay": "1" if cohort == DEVELOPMENT_COHORT else "0",
                "review_status": (
                    "ticker_exception_reviewed" if isinstance(override, dict) else "policy_rule_reviewed"
                ),
                "review_reason": (
                    str(override.get("reason") or "").strip()
                    if isinstance(override, dict)
                    else f"reviewed cohort/industry rule: {cohort} / {industry}"
                ),
            }
        )
    if len(assignments) != EXPECTED_IDENTITY_COUNT:
        errors.append(f"archetype_assignment_count={len(assignments)} expected={EXPECTED_IDENTITY_COUNT}")
    return sorted(assignments, key=lambda row: row["ticker"]), errors


def build_scope_rows(
    *,
    assignments: Sequence[Mapping[str, str]],
    metrics: Sequence[Mapping[str, str]],
    scope_version: str,
    registry_version: str,
    policy_version: str,
    contract_hash: str,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for assignment in assignments:
        identity_tags = set(_pipe_values(assignment["applicability_tags"]))
        for metric in metrics:
            metric_tags = set(_pipe_values(metric["applicability_tags"]))
            matched = sorted(identity_tags & metric_tags)
            applicable = bool(matched)
            rows.append(
                {
                    "scope_version": scope_version,
                    "registry_version": registry_version,
                    "policy_version": policy_version,
                    "input_contract_hash": contract_hash,
                    "ticker": assignment["ticker"],
                    "universe_role": assignment["universe_role"],
                    "calibration_cohort": assignment["calibration_cohort"],
                    "industry": assignment["industry"],
                    "primary_archetype": assignment["primary_archetype"],
                    "applicability_tags": assignment["applicability_tags"],
                    "development_overlay": assignment["development_overlay"],
                    "metric_id": metric["metric_id"],
                    "metric_pack": metric["metric_pack"],
                    "source_lane": metric["source_lane"],
                    "applicability_status": "APPLICABLE" if applicable else "NOT_APPLICABLE",
                    "applicability_reason": (
                        f"tag_match:{'|'.join(matched)}" if applicable else "no_applicable_archetype_tag"
                    ),
                    "unit_contract": metric["unit_contract"],
                    "period_type": metric["period_type"],
                    "max_staleness_days": metric["max_staleness_days"],
                    "scoring_posture": metric["scoring_posture"],
                    "comparison_population": metric["comparison_population"],
                    "bounds_policy": metric["bounds_policy"],
                    "discovery_status": (metric["discovery_status"] if applicable else "not_applicable"),
                }
            )
    return rows


def build_supporting_scope_rows(
    *,
    assignments: Sequence[Mapping[str, str]],
    metrics: Sequence[Mapping[str, str]],
    scope_version: str,
    registry_version: str,
    policy_version: str,
    contract_hash: str,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for assignment in assignments:
        identity_tags = set(_pipe_values(assignment["applicability_tags"]))
        for metric in metrics:
            metric_tags = set(_pipe_values(metric["applicability_tags"]))
            matched = sorted(identity_tags & metric_tags)
            applicable = bool(matched)
            rows.append(
                {
                    "scope_version": scope_version,
                    "registry_version": registry_version,
                    "policy_version": policy_version,
                    "input_contract_hash": contract_hash,
                    "ticker": assignment["ticker"],
                    "universe_role": assignment["universe_role"],
                    "calibration_cohort": assignment["calibration_cohort"],
                    "industry": assignment["industry"],
                    "primary_archetype": assignment["primary_archetype"],
                    "applicability_tags": assignment["applicability_tags"],
                    "development_overlay": assignment["development_overlay"],
                    "support_metric_id": metric["support_metric_id"],
                    "consumer_metric_ids": metric["consumer_metric_ids"],
                    "metric_pack": metric["metric_pack"],
                    "source_lane": "DP-S",
                    "applicability_status": ("APPLICABLE" if applicable else "NOT_APPLICABLE"),
                    "applicability_reason": (
                        f"tag_match:{'|'.join(matched)}" if applicable else "no_applicable_archetype_tag"
                    ),
                    "unit_contract": metric["unit_contract"],
                    "period_type": metric["period_type"],
                    "max_staleness_days": metric["max_staleness_days"],
                    "bounds_policy": metric["bounds_policy"],
                    "discovery_status": (metric["discovery_status"] if applicable else "not_applicable"),
                }
            )
    return rows


def validate_scope(
    *,
    rows: Sequence[Mapping[str, str]],
    assignments: Sequence[Mapping[str, str]],
    metrics: Sequence[Mapping[str, str]],
) -> list[str]:
    errors: list[str] = []
    if len(rows) != EXPECTED_SCOPE_COUNT:
        errors.append(f"scope_row_count={len(rows)} expected={EXPECTED_SCOPE_COUNT}")
    pair_counts = Counter((row.get("ticker", ""), row.get("metric_id", "")) for row in rows)
    duplicates = sorted(pair for pair, count in pair_counts.items() if count != 1)
    if duplicates:
        errors.append(f"scope duplicate/missing pair counts sample={duplicates[:10]}")
    per_ticker = Counter(row.get("ticker", "") for row in rows)
    bad_tickers = sorted((ticker, count) for ticker, count in per_ticker.items() if count != EXPECTED_METRIC_COUNT)
    if bad_tickers:
        errors.append(f"scope rows per ticker mismatch sample={bad_tickers[:10]}")
    per_metric = Counter(row.get("metric_id", "") for row in rows)
    bad_metrics = sorted(
        (metric_id, count) for metric_id, count in per_metric.items() if count != EXPECTED_IDENTITY_COUNT
    )
    if bad_metrics:
        errors.append(f"scope rows per metric mismatch sample={bad_metrics[:10]}")
    if set(per_ticker) != {row["ticker"] for row in assignments}:
        errors.append("scope ticker set does not match archetype assignments")
    if set(per_metric) != {row["metric_id"] for row in metrics}:
        errors.append("scope metric set does not match discovery registry")
    valid_statuses = {"APPLICABLE", "NOT_APPLICABLE"}
    for index, row in enumerate(rows, start=2):
        status = row.get("applicability_status", "")
        if status not in valid_statuses:
            errors.append(f"scope:{index}: invalid applicability_status={status!r}")
        if status == "APPLICABLE" and row.get("discovery_status") != "coverage_pending":
            errors.append(f"scope:{index}: applicable row must be coverage_pending")
        if status == "NOT_APPLICABLE" and row.get("discovery_status") != "not_applicable":
            errors.append(f"scope:{index}: nonapplicable row must be not_applicable")
    if not any(row.get("universe_role") == "delisted_usable" for row in rows):
        errors.append("scope excludes inactive/delisted identities")
    return errors


def validate_supporting_scope(
    *,
    rows: Sequence[Mapping[str, str]],
    assignments: Sequence[Mapping[str, str]],
    metrics: Sequence[Mapping[str, str]],
) -> list[str]:
    errors: list[str] = []
    if len(rows) != EXPECTED_SUPPORTING_SCOPE_COUNT:
        errors.append(f"supporting_scope_row_count={len(rows)} expected={EXPECTED_SUPPORTING_SCOPE_COUNT}")
    pair_counts = Counter((row.get("ticker", ""), row.get("support_metric_id", "")) for row in rows)
    duplicates = sorted(pair for pair, count in pair_counts.items() if count != 1)
    if duplicates:
        errors.append(f"supporting scope duplicate/missing pair counts sample={duplicates[:10]}")
    per_ticker = Counter(row.get("ticker", "") for row in rows)
    bad_tickers = sorted(
        (ticker, count) for ticker, count in per_ticker.items() if count != EXPECTED_SUPPORTING_METRIC_COUNT
    )
    if bad_tickers:
        errors.append(f"supporting scope rows per ticker mismatch sample={bad_tickers[:10]}")
    per_metric = Counter(row.get("support_metric_id", "") for row in rows)
    bad_metrics = sorted(
        (metric_id, count) for metric_id, count in per_metric.items() if count != EXPECTED_IDENTITY_COUNT
    )
    if bad_metrics:
        errors.append(f"supporting scope rows per metric mismatch sample={bad_metrics[:10]}")
    if set(per_ticker) != {row["ticker"] for row in assignments}:
        errors.append("supporting scope ticker set does not match archetype assignments")
    if set(per_metric) != {row["support_metric_id"] for row in metrics}:
        errors.append("supporting scope metric set does not match supporting registry")
    for index, row in enumerate(rows, start=2):
        status = row.get("applicability_status", "")
        if status not in {"APPLICABLE", "NOT_APPLICABLE"}:
            errors.append(f"supporting_scope:{index}: invalid applicability_status={status!r}")
        if row.get("source_lane") != "DP-S":
            errors.append(f"supporting_scope:{index}: source_lane must be DP-S")
        if status == "APPLICABLE" and row.get("discovery_status") != "coverage_pending":
            errors.append(f"supporting_scope:{index}: applicable row must be coverage_pending")
        if status == "NOT_APPLICABLE" and row.get("discovery_status") != "not_applicable":
            errors.append(f"supporting_scope:{index}: nonapplicable row must be not_applicable")
    if not any(row.get("universe_role") == "delisted_usable" for row in rows):
        errors.append("supporting scope excludes inactive/delisted identities")
    return errors


def baseline_artifact_hashes(
    *,
    project_root: Path,
    paths: Sequence[Path],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in paths:
        resolved = path.resolve()
        rows.append(
            {
                "path": resolved.relative_to(project_root.resolve()).as_posix(),
                "exists": resolved.exists(),
                "sha256": file_sha256(resolved) if resolved.is_file() else "",
            }
        )
    return rows


def build_and_write_contract(
    *,
    project_root: Path,
    active_path: Path,
    delisted_path: Path,
    metric_registry_path: Path,
    supporting_registry_path: Path,
    archetype_policy_path: Path,
    archetype_output_path: Path,
    scope_output_path: Path,
    supporting_scope_output_path: Path,
    manifest_output_path: Path,
    registry_version: str,
    scope_version: str,
    supporting_registry_version: str,
    supporting_scope_version: str,
    baseline_paths: Sequence[Path],
) -> dict[str, Any]:
    policy, policy_errors = load_archetype_policy(archetype_policy_path)
    allowed_tags = {str(value).strip() for value in policy.get("allowed_tags", []) if str(value).strip()}
    metrics, metric_errors = load_discovery_metrics(
        metric_registry_path,
        allowed_tags=allowed_tags,
    )
    supporting_metrics, supporting_metric_errors = load_supporting_metrics(
        supporting_registry_path,
        allowed_tags=allowed_tags,
        discovery_metrics=metrics,
    )
    universe, universe_errors = load_universe(active_path, delisted_path)
    assignments, assignment_errors = assign_archetypes(universe, policy)
    errors = [
        *policy_errors,
        *metric_errors,
        *supporting_metric_errors,
        *universe_errors,
        *assignment_errors,
    ]
    if errors:
        raise ValueError("DP0 contract input validation failed:\n- " + "\n- ".join(errors))
    contract_hash = input_contract_hash(
        [
            active_path,
            delisted_path,
            metric_registry_path,
            supporting_registry_path,
            archetype_policy_path,
        ]
    )
    scope = build_scope_rows(
        assignments=assignments,
        metrics=metrics,
        scope_version=scope_version,
        registry_version=registry_version,
        policy_version=str(policy["policy_version"]),
        contract_hash=contract_hash,
    )
    scope_errors = validate_scope(rows=scope, assignments=assignments, metrics=metrics)
    if scope_errors:
        raise ValueError("DP0 scope validation failed:\n- " + "\n- ".join(scope_errors))
    supporting_scope = build_supporting_scope_rows(
        assignments=assignments,
        metrics=supporting_metrics,
        scope_version=supporting_scope_version,
        registry_version=supporting_registry_version,
        policy_version=str(policy["policy_version"]),
        contract_hash=contract_hash,
    )
    supporting_scope_errors = validate_supporting_scope(
        rows=supporting_scope,
        assignments=assignments,
        metrics=supporting_metrics,
    )
    if supporting_scope_errors:
        raise ValueError("DP0 supporting scope validation failed:\n- " + "\n- ".join(supporting_scope_errors))

    write_csv_atomic(archetype_output_path, ARCHETYPE_FIELDS, assignments)
    write_csv_atomic(scope_output_path, SCOPE_FIELDS, scope)
    write_csv_atomic(
        supporting_scope_output_path,
        SUPPORTING_SCOPE_FIELDS,
        supporting_scope,
    )
    baseline = baseline_artifact_hashes(project_root=project_root, paths=baseline_paths)
    manifest: dict[str, Any] = {
        "acceptance": "PASS",
        "model_family": MODEL_FAMILY,
        "registry_version": registry_version,
        "scope_version": scope_version,
        "supporting_registry_version": supporting_registry_version,
        "supporting_scope_version": supporting_scope_version,
        "policy_version": str(policy["policy_version"]),
        "input_contract_hash": contract_hash,
        "counts": {
            "active_identities": EXPECTED_ACTIVE_COUNT,
            "delisted_identities": EXPECTED_DELISTED_COUNT,
            "total_identities": len(assignments),
            "metrics": len(metrics),
            "direct_parser_metrics": EXPECTED_LANE_COUNTS["DP"],
            "supporting_parser_metrics": len(supporting_metrics),
            "total_parser_search_metrics": (EXPECTED_LANE_COUNTS["DP"] + len(supporting_metrics)),
            "parser_derived_metrics": EXPECTED_LANE_COUNTS["DP-D"],
            "financial_derived_metrics": EXPECTED_LANE_COUNTS["FIN-D"],
            "scope_rows": len(scope),
            "applicable_rows": sum(row["applicability_status"] == "APPLICABLE" for row in scope),
            "not_applicable_rows": sum(row["applicability_status"] == "NOT_APPLICABLE" for row in scope),
            "supporting_scope_rows": len(supporting_scope),
            "supporting_applicable_rows": sum(row["applicability_status"] == "APPLICABLE" for row in supporting_scope),
            "supporting_not_applicable_rows": sum(
                row["applicability_status"] == "NOT_APPLICABLE" for row in supporting_scope
            ),
            "development_overlay_identities": sum(row["development_overlay"] == "1" for row in assignments),
        },
        "pack_counts": dict(Counter(row["metric_pack"] for row in metrics)),
        "source_lane_counts": dict(Counter(row["source_lane"] for row in metrics)),
        "primary_archetype_counts": dict(sorted(Counter(row["primary_archetype"] for row in assignments).items())),
        "hashes": {
            "active_seed_sha256": file_sha256(active_path),
            "delisted_seed_sha256": file_sha256(delisted_path),
            "metric_registry_sha256": file_sha256(metric_registry_path),
            "supporting_metric_registry_sha256": file_sha256(supporting_registry_path),
            "archetype_policy_sha256": file_sha256(archetype_policy_path),
            "archetype_map_sha256": file_sha256(archetype_output_path),
            "scope_sha256": file_sha256(scope_output_path),
            "supporting_scope_sha256": file_sha256(supporting_scope_output_path),
            "archetype_rows_canonical_sha256": _canonical_rows_hash(assignments),
            "scope_rows_canonical_sha256": _canonical_rows_hash(scope),
            "supporting_scope_rows_canonical_sha256": _canonical_rows_hash(supporting_scope),
        },
        "paths": {
            "metric_registry": metric_registry_path.resolve().relative_to(project_root.resolve()).as_posix(),
            "supporting_metric_registry": supporting_registry_path.resolve()
            .relative_to(project_root.resolve())
            .as_posix(),
            "archetype_policy": archetype_policy_path.resolve().relative_to(project_root.resolve()).as_posix(),
            "archetype_map": archetype_output_path.resolve().relative_to(project_root.resolve()).as_posix(),
            "scope": scope_output_path.resolve().relative_to(project_root.resolve()).as_posix(),
            "supporting_scope": supporting_scope_output_path.resolve().relative_to(project_root.resolve()).as_posix(),
        },
        "baseline_artifacts": baseline,
        "production_enabled": False,
        "parser_execution_authorized": False,
        "next_gate": "DP1_POLICY_ONLY_REPLAY_AND_DP2_ADAPTER_FIXTURES",
        "errors": [],
    }
    write_text_atomic(
        manifest_output_path,
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    return manifest


def validate_written_contract(
    *,
    project_root: Path,
    active_path: Path,
    delisted_path: Path,
    metric_registry_path: Path,
    supporting_registry_path: Path,
    archetype_policy_path: Path,
    archetype_output_path: Path,
    scope_output_path: Path,
    supporting_scope_output_path: Path,
    manifest_output_path: Path,
    registry_version: str,
    scope_version: str,
    supporting_registry_version: str,
    supporting_scope_version: str,
    validate_baseline: bool = True,
) -> dict[str, Any]:
    errors: list[str] = []
    policy, policy_errors = load_archetype_policy(archetype_policy_path)
    allowed_tags = {str(value).strip() for value in policy.get("allowed_tags", []) if str(value).strip()}
    metrics, metric_errors = load_discovery_metrics(
        metric_registry_path,
        allowed_tags=allowed_tags,
    )
    supporting_metrics, supporting_metric_errors = load_supporting_metrics(
        supporting_registry_path,
        allowed_tags=allowed_tags,
        discovery_metrics=metrics,
    )
    universe, universe_errors = load_universe(active_path, delisted_path)
    expected_assignments, assignment_errors = assign_archetypes(universe, policy)
    errors.extend(
        [
            *policy_errors,
            *metric_errors,
            *supporting_metric_errors,
            *universe_errors,
            *assignment_errors,
        ]
    )
    actual_assignments = _read_csv(archetype_output_path) if archetype_output_path.exists() else []
    errors.extend(
        _field_errors(
            path=archetype_output_path,
            rows=actual_assignments,
            expected_fields=ARCHETYPE_FIELDS,
        )
    )
    if actual_assignments != expected_assignments:
        errors.append("written archetype map does not match deterministic policy output")
    contract_hash = input_contract_hash(
        [
            active_path,
            delisted_path,
            metric_registry_path,
            supporting_registry_path,
            archetype_policy_path,
        ]
    )
    expected_scope = build_scope_rows(
        assignments=expected_assignments,
        metrics=metrics,
        scope_version=scope_version,
        registry_version=registry_version,
        policy_version=str(policy.get("policy_version") or ""),
        contract_hash=contract_hash,
    )
    actual_scope = _read_csv(scope_output_path) if scope_output_path.exists() else []
    errors.extend(_field_errors(path=scope_output_path, rows=actual_scope, expected_fields=SCOPE_FIELDS))
    if actual_scope != expected_scope:
        errors.append("written scope manifest does not match deterministic contract output")
    errors.extend(
        validate_scope(
            rows=actual_scope,
            assignments=actual_assignments,
            metrics=metrics,
        )
    )
    expected_supporting_scope = build_supporting_scope_rows(
        assignments=expected_assignments,
        metrics=supporting_metrics,
        scope_version=supporting_scope_version,
        registry_version=supporting_registry_version,
        policy_version=str(policy.get("policy_version") or ""),
        contract_hash=contract_hash,
    )
    actual_supporting_scope = _read_csv(supporting_scope_output_path) if supporting_scope_output_path.exists() else []
    errors.extend(
        _field_errors(
            path=supporting_scope_output_path,
            rows=actual_supporting_scope,
            expected_fields=SUPPORTING_SCOPE_FIELDS,
        )
    )
    if actual_supporting_scope != expected_supporting_scope:
        errors.append("written supporting scope manifest does not match deterministic contract output")
    errors.extend(
        validate_supporting_scope(
            rows=actual_supporting_scope,
            assignments=actual_assignments,
            metrics=supporting_metrics,
        )
    )
    manifest: dict[str, Any] = {}
    if not manifest_output_path.exists():
        errors.append(f"missing DP0 manifest={manifest_output_path}")
    else:
        manifest = json.loads(manifest_output_path.read_text(encoding="utf-8"))
        if manifest.get("acceptance") != "PASS":
            errors.append("DP0 manifest acceptance must be PASS")
        if manifest.get("input_contract_hash") != contract_hash:
            errors.append("DP0 manifest input_contract_hash mismatch")
        if manifest.get("registry_version") != registry_version:
            errors.append("DP0 manifest registry_version mismatch")
        if manifest.get("scope_version") != scope_version:
            errors.append("DP0 manifest scope_version mismatch")
        if manifest.get("supporting_registry_version") != supporting_registry_version:
            errors.append("DP0 manifest supporting_registry_version mismatch")
        if manifest.get("supporting_scope_version") != supporting_scope_version:
            errors.append("DP0 manifest supporting_scope_version mismatch")
        counts = manifest.get("counts") or {}
        if int(counts.get("scope_rows") or 0) != EXPECTED_SCOPE_COUNT:
            errors.append("DP0 manifest scope row count mismatch")
        if int(counts.get("supporting_scope_rows") or 0) != EXPECTED_SUPPORTING_SCOPE_COUNT:
            errors.append("DP0 manifest supporting scope row count mismatch")
        if (
            int(counts.get("total_parser_search_metrics") or 0)
            != EXPECTED_LANE_COUNTS["DP"] + EXPECTED_SUPPORTING_METRIC_COUNT
        ):
            errors.append("DP0 manifest parser search metric count mismatch")
        hashes = manifest.get("hashes") or {}
        expected_hashes = {
            "active_seed_sha256": file_sha256(active_path),
            "delisted_seed_sha256": file_sha256(delisted_path),
            "metric_registry_sha256": file_sha256(metric_registry_path),
            "supporting_metric_registry_sha256": file_sha256(supporting_registry_path),
            "archetype_policy_sha256": file_sha256(archetype_policy_path),
            "archetype_map_sha256": (file_sha256(archetype_output_path) if archetype_output_path.exists() else ""),
            "scope_sha256": file_sha256(scope_output_path) if scope_output_path.exists() else "",
            "supporting_scope_sha256": (
                file_sha256(supporting_scope_output_path) if supporting_scope_output_path.exists() else ""
            ),
            "archetype_rows_canonical_sha256": _canonical_rows_hash(actual_assignments),
            "scope_rows_canonical_sha256": _canonical_rows_hash(actual_scope),
            "supporting_scope_rows_canonical_sha256": _canonical_rows_hash(actual_supporting_scope),
        }
        for key, value in expected_hashes.items():
            if hashes.get(key) != value:
                errors.append(f"DP0 manifest hash mismatch: {key}")
        if manifest.get("production_enabled") is not False:
            errors.append("DP0 manifest must keep production_enabled=false")
        if manifest.get("parser_execution_authorized") is not False:
            errors.append("DP0 manifest must keep parser_execution_authorized=false")
        if validate_baseline:
            for baseline in manifest.get("baseline_artifacts") or []:
                baseline_path = project_root / str(baseline.get("path") or "")
                expected_exists = bool(baseline.get("exists"))
                if baseline_path.exists() != expected_exists:
                    errors.append(f"baseline artifact existence changed: {baseline_path}")
                elif baseline_path.is_file() and file_sha256(baseline_path) != baseline.get("sha256"):
                    errors.append(f"baseline artifact hash changed: {baseline_path}")
    return {
        "acceptance": "PASS" if not errors else "FAIL",
        "model_family": MODEL_FAMILY,
        "registry_version": registry_version,
        "scope_version": scope_version,
        "identity_count": len(actual_assignments),
        "metric_count": len(metrics),
        "supporting_metric_count": len(supporting_metrics),
        "scope_row_count": len(actual_scope),
        "applicable_row_count": sum(row.get("applicability_status") == "APPLICABLE" for row in actual_scope),
        "not_applicable_row_count": sum(row.get("applicability_status") == "NOT_APPLICABLE" for row in actual_scope),
        "supporting_scope_row_count": len(actual_supporting_scope),
        "supporting_applicable_row_count": sum(
            row.get("applicability_status") == "APPLICABLE" for row in actual_supporting_scope
        ),
        "supporting_not_applicable_row_count": sum(
            row.get("applicability_status") == "NOT_APPLICABLE" for row in actual_supporting_scope
        ),
        "manifest": str(manifest_output_path),
        "baseline_hash_check": "ENABLED" if validate_baseline else "SKIPPED",
        "errors": errors,
    }

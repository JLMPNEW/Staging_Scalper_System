"""Fail-closed replay of frozen Transportation v8 capture-date scores.

The future protocol may consume a published v8 score only when the score can be
reproduced from the exact, content-addressed point-in-time inputs available at
the signal cutoff.  This module deliberately does not inspect outcomes.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from future_only_evidence.canonical_trust import CanonicalTrustBundle
from future_only_evidence.canonical_values import exact_utc
from future_only_evidence.protocol import canonical_sha256
from future_only_evidence.transport_score_input_availability import (
    ACTIVATION_BASELINE_ROLE,
    transport_fact_identity,
    validate_transport_score_input_availability_snapshot,
)
from industrials.transportation.contemporaneous_metric_coverage import (
    availability_date,
)
from industrials.transportation.future_oos_activation_v6 import (
    GROUP_MINIMUM_CROSS_SECTIONS,
    GROUP_MODES,
    GROUP_TICKERS,
    GROUP_WEIGHTS,
)
from industrials.transportation.subgroup_scoring import (
    COMPONENTS,
    POLICY_VERSION,
    SUPPORTED_TRANSFORMS,
    build_v8_score_rows,
    ticker_location,
)


PANEL_SCHEMA = "transportation_future_v8_scoring_panel_v1"
ACCEPTED_FACTS_SCHEMA = "transportation_future_v8_accepted_facts_v1"
BASELINE_SCHEMA = "transportation_future_v8_score_replay_baseline_v1"
BASELINE_STRUCTURE_AUDIT_SCHEMA = (
    "transportation_future_v8_score_replay_baseline_structure_audit_v1"
)
REPLAY_INPUT_STRUCTURE_AUDIT_SCHEMA = (
    "transportation_future_v8_replay_input_structure_audit_v1"
)
AUDIT_SCHEMA = "transportation_future_v8_score_replay_audit_v1"
SCORE_FORMULA_ID = "transportation_v8_pit_subgroup_score_replay_v1"
EVIDENCE_ROLE = "prospective_future_only_capture"
BASELINE_EVIDENCE_ROLE = "prospective_future_only_activation_baseline"
GOVERNED_HORIZON_SESSIONS = 63

SOURCE_ROLES = frozenset(
    {
        "canonical_v8_score",
        "scoring_panel",
        "accepted_facts",
        "score_replay_baseline",
        "score_input_availability_baseline_snapshot",
        "score_input_availability_baseline_attestation",
        "score_input_availability_snapshot",
        "score_input_availability_attestation",
        "v8_policy",
    }
)
BASELINE_SOURCE_ROLES = frozenset(
    {
        "score_replay_baseline",
        "score_input_availability_baseline_snapshot",
        "score_input_availability_baseline_attestation",
        "v8_policy",
    }
)
STRUCTURAL_BASELINE_SOURCE_ROLES = frozenset(
    {
        "score_replay_baseline",
        "v8_policy",
    }
)
STRUCTURAL_REPLAY_INPUT_SOURCE_ROLES = frozenset(
    {
        "scoring_panel",
        "accepted_facts",
        "score_replay_baseline",
        "v8_policy",
    }
)

SCORE_FIELDS = (
    "asof_date",
    "ticker",
    "calibration_cohort",
    "v8_cohort_id",
    "v8_group_id",
    "ranking_mode",
    "specialized_pack_active_flag",
    "specialized_activation_policy",
    "specialized_features_json",
    "specialized_source_keys_json",
    "component_scores_json",
    "component_weights_json",
    "v8_final_score",
    "v8_group_percentile_score",
    "source_rank_ready_flag",
    "source_calibration_eligible_flag",
    "group_cross_section_ready_flag",
    "group_specialized_ready_flag",
    "v8_calibration_eligible_flag",
    "source_score_sha256",
)
_JSON_SCORE_FIELDS = frozenset(
    {
        "specialized_features_json",
        "specialized_source_keys_json",
        "component_scores_json",
        "component_weights_json",
    }
)
_FLOAT_SCORE_FIELDS = frozenset(
    {"v8_final_score", "v8_group_percentile_score"}
)
_FLAG_SCORE_FIELDS = frozenset(
    {
        "specialized_pack_active_flag",
        "source_rank_ready_flag",
        "source_calibration_eligible_flag",
        "group_cross_section_ready_flag",
        "group_specialized_ready_flag",
        "v8_calibration_eligible_flag",
    }
)

_PANEL_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "evidence_role",
        "asof_date",
        "governed_horizon_sessions",
        "rows",
        "rows_sha256",
        "date_ticker_census",
        "date_ticker_census_sha256",
    }
)
_PANEL_ROW_FIELDS = frozenset(
    {
        "asof_date",
        "ticker",
        "horizon_sessions",
        "calibration_cohort",
        "metric_values_json",
        "metric_status_json",
        "positioning_score",
        "rank_ready_flag",
        "calibration_eligible_flag",
        "source_score_sha256",
    }
)
_FACT_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "evidence_role",
        "asof_date",
        "rows",
        "rows_sha256",
        "staleness_days",
        "staleness_days_sha256",
    }
)
_BASELINE_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "evidence_role",
        "baseline_cutoff_at_utc",
        "panel_rows",
        "panel_rows_sha256",
        "date_ticker_census",
        "date_ticker_census_sha256",
        "source_score_file_sha256_by_date",
        "source_score_file_sha256_by_date_sha256",
        "accepted_fact_rows",
        "accepted_fact_rows_sha256",
        "accepted_fact_identity_census_sha256",
        "staleness_days",
        "staleness_days_sha256",
    }
)
_FACT_REQUIRED_FIELDS = frozenset(
    {
        "ticker",
        "metric_id",
        "value",
        "unit",
        "period_end",
        "filing_date",
        "accepted_at",
        "replay_status",
    }
)
_FORBIDDEN_INPUT_FIELD_FRAGMENTS = (
    "outcome",
    "forward_return",
    "excess_return",
    "benchmark_return",
    "security_return",
    "revealed_after",
    "exit_date",
    "exit_price",
)
_FACT_PIPELINE_TIMESTAMP_FIELDS = (
    "accepted_at",
    "reviewed_at",
    "adjudicated_at",
    "accepted_into_model_at",
)


def _read_snapshot(
    path: Path,
    *,
    role: str,
    expected_digest: str,
) -> tuple[bytes, str]:
    expected = _lower_sha256(expected_digest, label=f"{role} expected sha256")
    resolved = Path(path).expanduser().resolve()
    payload = resolved.read_bytes()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise ValueError(
            f"{role}: archived bytes differ from the frozen source identity"
        )
    return payload, actual


def _json_object(payload: bytes, *, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be one valid UTF-8 JSON object") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a JSON object")
    return parsed


def _yaml_object(payload: bytes, *, label: str) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(payload.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"{label} must be one valid UTF-8 YAML object") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a YAML object")
    return parsed


def _csv_rows(payload: bytes, *, label: str) -> list[dict[str, str]]:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8 CSV") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if tuple(reader.fieldnames or ()) != SCORE_FIELDS:
        raise ValueError(f"{label} changed the exact v8 score field census/order")
    rows: list[dict[str, str]] = []
    for raw in reader:
        if None in raw or set(raw) != set(SCORE_FIELDS):
            raise ValueError(f"{label} contains a malformed score row")
        rows.append({field: str(raw[field]) for field in SCORE_FIELDS})
    if not rows:
        raise ValueError(f"{label} contains no score rows")
    return rows


def _exact_date(value: Any, *, label: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be explicit YYYY-MM-DD")
    text = value
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be explicit YYYY-MM-DD") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"{label} must be explicit YYYY-MM-DD")
    return text


def _utc_timestamp(value: Any, *, label: str) -> datetime:
    """Parse one explicit RFC3339 UTC timestamp without date truncation."""

    return exact_utc(value, label=label)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_finite(value: Any, *, label: str) -> float:
    if type(value) not in {int, float}:
        raise ValueError(f"{label} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be finite")
    return parsed


def _csv_finite(value: Any, *, label: str) -> float:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{label} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be finite")
    return parsed


def _canonical_int(
    value: Any,
    *,
    label: str,
    minimum: int | None = None,
    allowed: set[int] | None = None,
) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be a canonical integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    if allowed is not None and value not in allowed:
        raise ValueError(f"{label} is outside the allowed integer census")
    return value


def _canonical_flag(value: Any, *, label: str) -> int:
    return _canonical_int(value, label=label, allowed={0, 1})


def _binary_flag(value: Any, *, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be the integer flag 0 or 1")
    if value not in (0, 1, "0", "1"):
        raise ValueError(f"{label} must be the integer flag 0 or 1")
    return int(value)


def _lower_sha256(value: Any, *, label: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be a lowercase SHA-256")
    text = value
    if (
        len(text) != 64
        or text != text.lower()
        or any(character not in "0123456789abcdef" for character in text)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return text


def _validate_policy(policy: Mapping[str, Any]) -> None:
    if policy.get("policy_version") != POLICY_VERSION:
        raise ValueError("Transportation v8 policy version changed")
    controls = policy.get("controls")
    if not isinstance(controls, dict):
        raise ValueError("Transportation v8 policy controls are absent")
    if controls.get("percentile_normalization") != "within_comparison_group":
        raise ValueError("Transportation normalization policy changed")
    if controls.get("group_weights_use_outcomes") is not False:
        raise ValueError("Transportation group weights are not outcome blind")
    if controls.get("component_weights_use_outcomes") is not False:
        raise ValueError("Transportation component weights are not outcome blind")
    winsor_lower = _canonical_finite(
        controls.get("winsor_lower"), label="Transportation winsor lower"
    )
    winsor_upper = _canonical_finite(
        controls.get("winsor_upper"), label="Transportation winsor upper"
    )
    neutral = _canonical_finite(
        controls.get("neutral_missing_score"),
        label="Transportation neutral missing score",
    )
    minimum_specialized_fraction = _canonical_finite(
        controls.get("minimum_specialized_date_fraction"),
        label="Transportation minimum specialized date fraction",
    )
    if (
        not 0.0 <= winsor_lower < winsor_upper <= 1.0
        or not 0.0 <= neutral <= 100.0
        or not 0.0 <= minimum_specialized_fraction <= 1.0
    ):
        raise ValueError("Transportation numeric score controls are outside policy bounds")

    recipes = policy.get("generic_metric_recipes")
    expected_generic = set(COMPONENTS) - {"positioning", "specialized"}
    if not isinstance(recipes, dict) or set(recipes) != expected_generic:
        raise ValueError("Transportation generic score recipe census changed")
    for component, recipe in recipes.items():
        if not isinstance(recipe, dict) or not recipe:
            raise ValueError(f"{component}: generic score recipe is empty")
        weights = [
            _canonical_finite(item.get("weight"), label=f"{component} weight")
            for item in recipe.values()
        ]
        if any(weight < 0.0 for weight in weights) or not math.isclose(
            sum(weights), 1.0, abs_tol=1e-12
        ):
            raise ValueError(f"{component}: generic score weights changed")
        for metric_id, item in recipe.items():
            if not metric_id or _canonical_int(
                item.get("direction"),
                label=f"{component}/{metric_id} direction",
                allowed={-1, 1},
            ) not in {-1, 1}:
                raise ValueError(f"{component}: generic score direction changed")

    expected_groups = set(GROUP_TICKERS)
    observed_groups: set[str] = set()
    cohorts = policy.get("cohorts")
    if not isinstance(cohorts, dict):
        raise ValueError("Transportation v8 cohort policy is absent")
    for cohort in cohorts.values():
        if not isinstance(cohort, dict):
            raise ValueError("Transportation v8 cohort definition is malformed")
        sleeve = str(cohort.get("calibration_cohort") or "")
        if sleeve not in GROUP_WEIGHTS:
            raise ValueError("Transportation v8 cohort maps outside frozen sleeves")
        weights = cohort.get("aggregate_group_weights")
        if not isinstance(weights, dict) or set(weights) != set(GROUP_WEIGHTS[sleeve]):
            raise ValueError(f"{sleeve}: aggregate group-weight census changed")
        for group_id, expected_weight in GROUP_WEIGHTS[sleeve].items():
            if not math.isclose(
                _canonical_finite(
                    weights[group_id], label=f"{group_id} aggregate weight"
                ),
                float(expected_weight),
                abs_tol=1e-12,
            ):
                raise ValueError(f"{group_id}: aggregate group weight changed")
        groups = cohort.get("groups")
        if not isinstance(groups, dict) or set(groups) != set(weights):
            raise ValueError(f"{sleeve}: comparison-group census changed")
        for group_id, group in groups.items():
            if group_id in observed_groups or group_id not in expected_groups:
                raise ValueError("Transportation comparison groups overlap or changed")
            observed_groups.add(str(group_id))
            if list(group.get("tickers") or []) != list(GROUP_TICKERS[group_id]):
                raise ValueError(f"{group_id}: frozen ticker census/order changed")
            if group.get("ranking_mode") != GROUP_MODES[group_id]:
                raise ValueError(f"{group_id}: frozen ranking mode changed")
            if _canonical_int(
                group.get("minimum_cross_section"),
                label=f"{group_id} minimum cross section",
                minimum=1,
            ) != GROUP_MINIMUM_CROSS_SECTIONS[group_id]:
                raise ValueError(f"{group_id}: frozen cross-section minimum changed")
            _canonical_int(
                group.get("minimum_specialized_breadth"),
                label=f"{group_id} minimum specialized breadth",
                minimum=1,
            )
            for field in ("component_weights_active", "component_weights_fallback"):
                component_weights = group.get(field)
                if not isinstance(component_weights, dict) or set(component_weights) != set(COMPONENTS):
                    raise ValueError(f"{group_id}: {field} component census changed")
                parsed = [
                    _canonical_finite(
                        value, label=f"{group_id}/{field}/{component}"
                    )
                    for component, value in component_weights.items()
                ]
                if any(value < 0.0 for value in parsed) or not math.isclose(
                    sum(parsed), 1.0, abs_tol=1e-12
                ):
                    raise ValueError(f"{group_id}: {field} weights changed")
            pack = group.get("specialized_pack")
            if not isinstance(pack, dict):
                raise ValueError(f"{group_id}: specialized pack is malformed")
            if pack:
                pack_weights = [
                    _canonical_finite(
                        item.get("weight"),
                        label=f"{group_id}/{feature_id} weight",
                    )
                    for feature_id, item in pack.items()
                ]
                if not math.isclose(sum(pack_weights), 1.0, abs_tol=1e-12):
                    raise ValueError(f"{group_id}: specialized weights changed")
                for feature_id, item in pack.items():
                    if item.get("transform") not in SUPPORTED_TRANSFORMS:
                        raise ValueError(f"{group_id}/{feature_id}: transform changed")
                    if _canonical_int(
                        item.get("direction"),
                        label=f"{group_id}/{feature_id} direction",
                        allowed={-1, 1},
                    ) not in {-1, 1}:
                        raise ValueError(f"{group_id}/{feature_id}: direction changed")
    if observed_groups != expected_groups:
        raise ValueError("Transportation frozen comparison-group census changed")
    historical = policy.get("historical_calibration_only")
    if not isinstance(historical, dict):
        raise ValueError("Transportation historical membership policy is absent")
    valid_locations = {
        (str(cohort_id), str(group_id))
        for cohort_id, cohort in cohorts.items()
        for group_id in cohort["groups"]
    }
    for ticker, item in historical.items():
        if (
            type(ticker) is not str
            or not ticker
            or ticker.strip() != ticker
            or ticker.upper() != ticker
            or ticker in _current_tickers()
        ):
            raise ValueError(
                "Transportation historical ticker identity is not canonical"
            )
        if not isinstance(item, dict) or set(item) != {
            "cohort",
            "group",
            "effective_from",
            "effective_to",
        }:
            raise ValueError(f"{ticker}: historical membership census changed")
        location = (item.get("cohort"), item.get("group"))
        if location not in valid_locations:
            raise ValueError(f"{ticker}: historical membership location changed")
        effective_from = _exact_date(
            item.get("effective_from"),
            label=f"{ticker} historical effective-from",
        )
        effective_to = _exact_date(
            item.get("effective_to"),
            label=f"{ticker} historical effective-to",
        )
        if effective_from > effective_to:
            raise ValueError(f"{ticker}: historical membership dates are reversed")
    governance = policy.get("governance")
    if not isinstance(governance, dict):
        raise ValueError("Transportation v8 governance is absent")
    for field in (
        "membership_selection_uses_outcomes",
        "metric_definition_selection_uses_outcomes",
        "production_activation_authorized",
    ):
        if governance.get(field) is not False:
            raise ValueError(f"Transportation v8 governance changed {field}")


def _source_metric_ids(policy: Mapping[str, Any]) -> set[str]:
    metrics: set[str] = set()
    for cohort in policy["cohorts"].values():
        for group in cohort["groups"].values():
            for feature in group.get("specialized_pack", {}).values():
                source_metrics = feature.get("source_metrics") or [
                    feature.get("source_metric")
                ]
                metrics.update(str(metric) for metric in source_metrics if metric)
    return metrics


def _current_tickers() -> set[str]:
    return {ticker for tickers in GROUP_TICKERS.values() for ticker in tickers}


def _known_tickers(policy: Mapping[str, Any]) -> set[str]:
    tickers = _current_tickers()
    tickers.update(policy.get("historical_calibration_only") or {})
    return tickers


def _parse_staleness(
    raw: Any,
    *,
    policy: Mapping[str, Any],
    label: str,
) -> dict[str, int]:
    required_metrics = _source_metric_ids(policy)
    if not isinstance(raw, dict) or set(raw) != required_metrics:
        raise ValueError(
            "Transportation staleness policy lacks the exact source-metric census"
        )
    parsed: dict[str, int] = {}
    for metric_id, value in raw.items():
        parsed[str(metric_id)] = _canonical_int(
            value,
            label=f"{label}/{metric_id}",
            minimum=1,
        )
    return dict(sorted(parsed.items()))


def _fact_identity(row: Mapping[str, Any]) -> str:
    source_identity = row.get("candidate_key") or row.get("evidence_key")
    if type(source_identity) is not str or not source_identity:
        raise ValueError("Transportation accepted fact lacks source identity")
    return canonical_sha256(
        {
            "ticker": row.get("ticker"),
            "metric_id": row.get("metric_id"),
            "period_end": row.get("period_end"),
            "candidate_key": row.get("candidate_key"),
            "evidence_key": row.get("evidence_key"),
            "source_content_sha256": row.get("source_content_sha256"),
        }
    )


def _fact_information_at(
    row: Mapping[str, Any],
    *,
    ticker: str,
    metric_id: str,
) -> datetime:
    accepted = _utc_timestamp(
        row.get("accepted_at"),
        label=f"{ticker}/{metric_id} accepted timestamp",
    )
    timestamps = [accepted]
    for field in _FACT_PIPELINE_TIMESTAMP_FIELDS[1:]:
        value = row.get(field)
        if value not in (None, ""):
            parsed = _utc_timestamp(
                value,
                label=f"{ticker}/{metric_id} {field}",
            )
            if parsed < accepted:
                raise ValueError(
                    f"{ticker}/{metric_id}: {field} predates source acceptance"
                )
            timestamps.append(parsed)
    return max(timestamps)


def _panel_identity_maps(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, list[str]], dict[str, str]]:
    census: defaultdict[str, list[str]] = defaultdict(list)
    source_hashes: dict[str, str] = {}
    for row in rows:
        score_date = _exact_date(row.get("asof_date"), label="baseline panel asof")
        ticker = row.get("ticker")
        if type(ticker) is not str or not ticker:
            raise ValueError("Transportation baseline panel ticker is malformed")
        source_hash = _lower_sha256(
            row.get("source_score_sha256"),
            label=f"{score_date}/{ticker} baseline source score sha256",
        )
        previous = source_hashes.setdefault(score_date, source_hash)
        if previous != source_hash:
            raise ValueError(
                "Transportation baseline mixes source-score identities within one date"
            )
        census[score_date].append(ticker)
    return (
        {
            score_date: sorted(tickers)
            for score_date, tickers in sorted(census.items())
        },
        dict(sorted(source_hashes.items())),
    )


def _validate_baseline(
    baseline: Mapping[str, Any],
    *,
    policy: Mapping[str, Any],
    signal_cutoff: datetime,
) -> dict[str, Any]:
    if set(baseline) != _BASELINE_TOP_LEVEL_FIELDS:
        raise ValueError("Transportation score-replay baseline census changed")
    if (
        baseline.get("schema_version") != BASELINE_SCHEMA
        or baseline.get("evidence_role") != BASELINE_EVIDENCE_ROLE
    ):
        raise ValueError("Transportation score-replay baseline identity changed")
    baseline_cutoff = _utc_timestamp(
        baseline.get("baseline_cutoff_at_utc"),
        label="Transportation replay baseline cutoff",
    )
    if baseline_cutoff >= signal_cutoff:
        raise ValueError("Transportation replay baseline is not strictly pre-signal")

    panel_rows = baseline.get("panel_rows")
    if not isinstance(panel_rows, list) or not panel_rows:
        raise ValueError("Transportation replay baseline panel is empty")
    if baseline.get("panel_rows_sha256") != canonical_sha256(panel_rows):
        raise ValueError("Transportation replay baseline panel hash is inconsistent")
    if any(
        not isinstance(row, dict) or set(row) != _PANEL_ROW_FIELDS
        for row in panel_rows
    ):
        raise ValueError("Transportation replay baseline panel row census changed")
    baseline_census, baseline_source_hashes = _panel_identity_maps(panel_rows)
    if baseline.get("date_ticker_census") != baseline_census:
        raise ValueError("Transportation replay baseline date/ticker census changed")
    if baseline.get("date_ticker_census_sha256") != canonical_sha256(
        baseline_census
    ):
        raise ValueError("Transportation replay baseline census hash is inconsistent")
    if baseline.get("source_score_file_sha256_by_date") != baseline_source_hashes:
        raise ValueError("Transportation replay baseline source-score map changed")
    if baseline.get(
        "source_score_file_sha256_by_date_sha256"
    ) != canonical_sha256(baseline_source_hashes):
        raise ValueError(
            "Transportation replay baseline source-score-map hash is inconsistent"
        )
    if max(baseline_census) > baseline_cutoff.date().isoformat():
        raise ValueError("Transportation replay baseline panel is post-baseline")

    fact_rows = baseline.get("accepted_fact_rows")
    if not isinstance(fact_rows, list):
        raise ValueError("Transportation replay baseline accepted facts are absent")
    if baseline.get("accepted_fact_rows_sha256") != canonical_sha256(fact_rows):
        raise ValueError("Transportation replay baseline fact hash is inconsistent")
    identities: list[str] = []
    for raw in fact_rows:
        if not isinstance(raw, dict):
            raise ValueError("Transportation replay baseline fact is malformed")
        ticker = raw.get("ticker")
        metric_id = raw.get("metric_id")
        if type(ticker) is not str or type(metric_id) is not str:
            raise ValueError("Transportation replay baseline fact identity is malformed")
        if _fact_information_at(
            raw,
            ticker=ticker,
            metric_id=metric_id,
        ) > baseline_cutoff:
            raise ValueError("Transportation replay baseline contains a late fact")
        identities.append(_fact_identity(raw))
    if len(identities) != len(set(identities)):
        raise ValueError("Transportation replay baseline contains duplicate facts")
    if baseline.get("accepted_fact_identity_census_sha256") != canonical_sha256(
        identities
    ):
        raise ValueError(
            "Transportation replay baseline fact-identity census is inconsistent"
        )

    staleness = _parse_staleness(
        baseline.get("staleness_days"),
        policy=policy,
        label="frozen Transportation staleness",
    )
    if baseline.get("staleness_days_sha256") != canonical_sha256(staleness):
        raise ValueError("Transportation replay baseline staleness hash is inconsistent")

    # A frozen activation package must be usable, not merely self-consistent.
    # Reuse the exact capture validators against synthetic wrappers around the
    # baseline rows so every ticker, cohort, metric, scalar type, timestamp,
    # source hash, and staleness entry is checked against the frozen policy.
    semantic_asof = baseline_cutoff.date().isoformat()
    semantic_panel = {
        "schema_version": PANEL_SCHEMA,
        "evidence_role": EVIDENCE_ROLE,
        "asof_date": semantic_asof,
        "governed_horizon_sessions": GOVERNED_HORIZON_SESSIONS,
        "rows": panel_rows,
        "rows_sha256": canonical_sha256(panel_rows),
        "date_ticker_census": baseline_census,
        "date_ticker_census_sha256": canonical_sha256(baseline_census),
    }
    (
        semantic_panel_rows,
        semantic_panel_census,
        semantic_source_hashes,
    ) = _validate_panel(
        semantic_panel,
        asof=semantic_asof,
        policy=policy,
        require_capture_census=False,
    )
    semantic_facts = {
        "schema_version": ACCEPTED_FACTS_SCHEMA,
        "evidence_role": EVIDENCE_ROLE,
        "asof_date": semantic_asof,
        "rows": fact_rows,
        "rows_sha256": canonical_sha256(fact_rows),
        "staleness_days": staleness,
        "staleness_days_sha256": canonical_sha256(staleness),
    }
    (
        semantic_fact_rows,
        semantic_staleness,
        semantic_information_times,
        semantic_identities,
    ) = _validate_facts(
        semantic_facts,
        asof=semantic_asof,
        policy=policy,
        signal_cutoff=baseline_cutoff,
    )
    if (
        semantic_panel_rows != panel_rows
        or semantic_panel_census != baseline_census
        or semantic_source_hashes != baseline_source_hashes
        or semantic_fact_rows != fact_rows
        or semantic_staleness != staleness
        or semantic_identities != identities
    ):
        raise ValueError(
            "Transportation replay baseline semantic normalization changed inputs"
        )
    return {
        "cutoff": baseline_cutoff,
        "panel_rows": panel_rows,
        "panel_census": baseline_census,
        "source_hashes": baseline_source_hashes,
        "fact_rows": fact_rows,
        "fact_identities": identities,
        "fact_information_times": semantic_information_times,
        "staleness": staleness,
    }


def _validate_scheduled_append_dates(
    values: Sequence[str],
    *,
    asof: str,
    baseline_dates: Sequence[str],
) -> list[str]:
    if isinstance(values, (str, bytes)) or not values:
        raise ValueError("Transportation scheduled append-date census is absent")
    scheduled = [
        _exact_date(value, label="Transportation scheduled append asof")
        for value in values
    ]
    if scheduled != sorted(set(scheduled)):
        raise ValueError(
            "Transportation scheduled append dates must be unique and increasing"
        )
    if scheduled[-1] != asof:
        raise ValueError("Transportation scheduled append dates do not end at capture")
    if baseline_dates and scheduled[0] <= max(baseline_dates):
        raise ValueError("Transportation scheduled append overlaps the frozen baseline")
    return scheduled


def _validate_panel(
    panel: Mapping[str, Any],
    *,
    asof: str,
    policy: Mapping[str, Any],
    require_capture_census: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, list[str]], dict[str, str]]:
    if set(panel) != _PANEL_TOP_LEVEL_FIELDS:
        raise ValueError("Transportation scoring-panel top-level census changed")
    if panel.get("schema_version") != PANEL_SCHEMA or panel.get("evidence_role") != EVIDENCE_ROLE:
        raise ValueError("Transportation scoring-panel identity changed")
    if _exact_date(panel.get("asof_date"), label="scoring-panel asof") != asof:
        raise ValueError("Transportation scoring-panel asof differs from capture")
    if _canonical_int(
        panel.get("governed_horizon_sessions"),
        label="Transportation governed source horizon",
        minimum=1,
    ) != GOVERNED_HORIZON_SESSIONS:
        raise ValueError("Transportation governed source horizon changed")
    raw_rows = panel.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ValueError("Transportation scoring panel contains no rows")
    if panel.get("rows_sha256") != canonical_sha256(raw_rows):
        raise ValueError("Transportation scoring-panel row hash is inconsistent")

    rows: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    source_hash_by_date: dict[str, str] = {}
    source_date_by_hash: dict[str, str] = {}
    census: defaultdict[str, list[str]] = defaultdict(list)
    for raw in raw_rows:
        if not isinstance(raw, dict) or set(raw) != _PANEL_ROW_FIELDS:
            raise ValueError("Transportation scoring-panel row census changed")
        row = dict(raw)
        row_asof = _exact_date(row.get("asof_date"), label="panel-row asof")
        if row_asof > asof:
            raise ValueError("Transportation scoring panel contains post-checkpoint rows")
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker or ticker != row.get("ticker"):
            raise ValueError("Transportation panel ticker must be canonical uppercase")
        if _canonical_int(
            row.get("horizon_sessions"),
            label=f"{row_asof}/{ticker} horizon sessions",
            minimum=1,
        ) != GOVERNED_HORIZON_SESSIONS:
            raise ValueError("Transportation panel contains a non-governed horizon")
        key = (row_asof, ticker)
        if key in seen_keys:
            raise ValueError(
                "Transportation panel has ambiguous duplicate 63-session rows"
            )
        seen_keys.add(key)
        location = ticker_location(ticker, row_asof, policy)
        if location is None:
            raise ValueError(f"{row_asof}/{ticker}: panel row is outside frozen policy")
        cohort = policy["cohorts"][location[0]]
        if row.get("calibration_cohort") != cohort.get("calibration_cohort"):
            raise ValueError(f"{row_asof}/{ticker}: calibration cohort changed")
        source_hash = _lower_sha256(
            row.get("source_score_sha256"),
            label=f"{row_asof}/{ticker} source score sha256",
        )
        prior_hash = source_hash_by_date.setdefault(row_asof, source_hash)
        if prior_hash != source_hash:
            raise ValueError(
                "Transportation panel mixes source-score identities within one date"
            )
        prior_date = source_date_by_hash.setdefault(source_hash, row_asof)
        if prior_date != row_asof:
            raise ValueError(
                "Transportation panel reuses one source-score identity across dates"
            )
        try:
            values = json.loads(str(row.get("metric_values_json")))
            statuses = json.loads(str(row.get("metric_status_json")))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{row_asof}/{ticker}: metric JSON is invalid") from exc
        if not isinstance(values, dict) or not isinstance(statuses, dict):
            raise ValueError(f"{row_asof}/{ticker}: metric JSON must contain objects")
        for metric_id, value in values.items():
            _canonical_finite(value, label=f"{row_asof}/{ticker}/{metric_id}")
        if any(not isinstance(status, str) for status in statuses.values()):
            raise ValueError(f"{row_asof}/{ticker}: metric statuses are malformed")
        if row.get("positioning_score") not in (None, ""):
            _canonical_finite(
                row.get("positioning_score"),
                label=f"{row_asof}/{ticker} positioning",
            )
        rank_ready = _canonical_flag(
            row.get("rank_ready_flag"), label=f"{row_asof}/{ticker} rank-ready"
        )
        calibration_ready = _canonical_flag(
            row.get("calibration_eligible_flag"),
            label=f"{row_asof}/{ticker} calibration-eligible",
        )
        if calibration_ready and not rank_ready:
            raise ValueError(
                f"{row_asof}/{ticker}: calibration eligibility contradicts rank readiness"
            )
        row["ticker"] = ticker
        rows.append(row)
        census[row_asof].append(ticker)

    normalized_census = {
        score_date: sorted(tickers) for score_date, tickers in sorted(census.items())
    }
    if panel.get("date_ticker_census") != normalized_census:
        raise ValueError("Transportation panel date/ticker census is not exact")
    if panel.get("date_ticker_census_sha256") != canonical_sha256(normalized_census):
        raise ValueError("Transportation panel census hash is inconsistent")
    if require_capture_census and set(
        normalized_census.get(asof, ())
    ) != _current_tickers():
        raise ValueError("Transportation capture date lacks the exact frozen 35-ticker census")
    return rows, normalized_census, dict(sorted(source_hash_by_date.items()))


def _validate_facts(
    facts: Mapping[str, Any],
    *,
    asof: str,
    policy: Mapping[str, Any],
    signal_cutoff: datetime,
) -> tuple[list[dict[str, Any]], dict[str, int], list[datetime], list[str]]:
    if set(facts) != _FACT_TOP_LEVEL_FIELDS:
        raise ValueError("Transportation accepted-facts top-level census changed")
    if facts.get("schema_version") != ACCEPTED_FACTS_SCHEMA or facts.get("evidence_role") != EVIDENCE_ROLE:
        raise ValueError("Transportation accepted-facts identity changed")
    if _exact_date(facts.get("asof_date"), label="accepted-facts asof") != asof:
        raise ValueError("Transportation accepted-facts asof differs from capture")
    raw_rows = facts.get("rows")
    if not isinstance(raw_rows, list):
        raise ValueError("Transportation accepted-facts rows are absent")
    if facts.get("rows_sha256") != canonical_sha256(raw_rows):
        raise ValueError("Transportation accepted-facts row hash is inconsistent")
    parsed_staleness = _parse_staleness(
        facts.get("staleness_days"),
        policy=policy,
        label="Transportation accepted-fact staleness",
    )
    if facts.get("staleness_days_sha256") != canonical_sha256(parsed_staleness):
        raise ValueError("Transportation staleness-policy hash is inconsistent")

    rows: list[dict[str, Any]] = []
    information_times: list[datetime] = []
    identities: list[str] = []
    required_metrics = _source_metric_ids(policy)
    known_tickers = _known_tickers(policy)
    for raw in raw_rows:
        if not isinstance(raw, dict) or not _FACT_REQUIRED_FIELDS <= set(raw):
            raise ValueError("Transportation accepted fact lacks required fields")
        lowered_fields = tuple(str(field).casefold() for field in raw)
        if any(
            fragment in field
            for field in lowered_fields
            for fragment in _FORBIDDEN_INPUT_FIELD_FRAGMENTS
        ):
            raise ValueError("Transportation accepted facts contain outcome fields")
        row = dict(raw)
        ticker = str(row.get("ticker") or "").strip().upper()
        metric_id = str(row.get("metric_id") or "")
        if ticker not in known_tickers or ticker != row.get("ticker"):
            raise ValueError("Transportation accepted fact has an unknown ticker")
        if metric_id not in required_metrics:
            raise ValueError(f"{ticker}: accepted fact metric is outside frozen recipes")
        if row.get("replay_status") != "ACCEPTED":
            raise ValueError(f"{ticker}/{metric_id}: fact is not accepted")
        _canonical_finite(
            row.get("value"), label=f"{ticker}/{metric_id} accepted value"
        )
        period_end = _exact_date(
            row.get("period_end"), label=f"{ticker}/{metric_id} period end"
        )
        filing_date = _exact_date(
            row.get("filing_date"), label=f"{ticker}/{metric_id} filing date"
        )
        information_at = _fact_information_at(
            row,
            ticker=ticker,
            metric_id=metric_id,
        )
        if max(period_end, filing_date) > asof:
            raise ValueError("Transportation accepted facts contain post-checkpoint data")
        if information_at > signal_cutoff:
            raise ValueError(
                "Transportation accepted facts contain post-signal information"
            )
        if row.get("asof_date") not in (None, ""):
            fact_asof = _exact_date(
                row.get("asof_date"), label=f"{ticker}/{metric_id} fact asof"
            )
            if fact_asof > asof:
                raise ValueError(
                    "Transportation accepted facts contain post-checkpoint asofs"
                )
        if row.get("period_start") not in (None, ""):
            period_start = _exact_date(
                row.get("period_start"), label=f"{ticker}/{metric_id} period start"
            )
            if period_start > period_end:
                raise ValueError(f"{ticker}/{metric_id}: period dates are reversed")
        available = availability_date(row)
        if available is None or available.isoformat() > asof:
            raise ValueError("Transportation fact was not available by capture")
        identity = _fact_identity(row)
        if identity in identities:
            raise ValueError("Transportation accepted facts contain duplicate identities")
        rows.append(row)
        information_times.append(information_at)
        identities.append(identity)
    return rows, parsed_staleness, information_times, identities


def _validate_append_only_contract(
    *,
    panel_rows: Sequence[Mapping[str, Any]],
    panel_census: Mapping[str, Sequence[str]],
    panel_source_hashes: Mapping[str, str],
    accepted_rows: Sequence[Mapping[str, Any]],
    accepted_information_times: Sequence[datetime],
    accepted_identities: Sequence[str],
    staleness: Mapping[str, int],
    baseline: Mapping[str, Any],
    scheduled_dates: Sequence[str],
    signal_cutoff: datetime,
    predecessor_replay_audit: Mapping[str, Any] | None,
) -> dict[str, Any]:
    baseline_panel_rows = baseline["panel_rows"]
    baseline_fact_rows = baseline["fact_rows"]
    if list(panel_rows[: len(baseline_panel_rows)]) != list(baseline_panel_rows):
        raise ValueError(
            "Transportation panel does not preserve the exact frozen baseline prefix"
        )
    if list(accepted_rows[: len(baseline_fact_rows)]) != list(baseline_fact_rows):
        raise ValueError(
            "Transportation facts do not preserve the exact frozen baseline prefix"
        )
    if list(accepted_identities[: len(baseline["fact_identities"])]) != list(
        baseline["fact_identities"]
    ):
        raise ValueError("Transportation accepted-fact baseline identities changed")
    if dict(staleness) != dict(baseline["staleness"]):
        raise ValueError("Transportation accepted-fact staleness changed after activation")

    expected_dates = list(baseline["panel_census"]) + list(scheduled_dates)
    if list(panel_census) != expected_dates:
        raise ValueError(
            "Transportation panel omitted or added a scheduled score date"
        )
    if dict(list(panel_source_hashes.items())[: len(baseline["source_hashes"])]) != dict(
        baseline["source_hashes"]
    ):
        raise ValueError("Transportation baseline source-score identity map changed")

    expected_tickers = sorted(_current_tickers())
    for score_date in scheduled_dates:
        if list(panel_census.get(score_date, ())) != expected_tickers:
            raise ValueError(
                f"{score_date}: Transportation scheduled panel ticker census changed"
            )
    appended_panel_rows = list(panel_rows[len(baseline_panel_rows) :])
    expected_append_keys = [
        (score_date, ticker)
        for score_date in scheduled_dates
        for ticker in expected_tickers
    ]
    observed_append_keys = [
        (row.get("asof_date"), row.get("ticker")) for row in appended_panel_rows
    ]
    if observed_append_keys != expected_append_keys:
        raise ValueError(
            "Transportation scheduled panel rows are missing, extra, or reordered"
        )

    baseline_cutoff = baseline["cutoff"]
    appended_fact_times = list(
        accepted_information_times[len(baseline_fact_rows) :]
    )
    if any(timestamp <= baseline_cutoff for timestamp in appended_fact_times):
        raise ValueError(
            "Transportation newly appended fact is backdated at/before baseline cutoff"
        )

    if len(scheduled_dates) == 1:
        if predecessor_replay_audit is not None:
            raise ValueError(
                "Transportation first scheduled append cannot declare a predecessor"
            )
        prior_cutoff = baseline_cutoff
    else:
        prior = predecessor_replay_audit
        if not isinstance(prior, Mapping):
            raise ValueError(
                "Transportation later scheduled append lacks predecessor replay state"
            )
        if (
            prior.get("schema_version") != AUDIT_SCHEMA
            or prior.get("score_formula_id") != SCORE_FORMULA_ID
            or _exact_date(
                prior.get("asof_date"), label="predecessor replay asof"
            )
            != scheduled_dates[-2]
        ):
            raise ValueError("Transportation predecessor replay identity changed")
        prior_dates = prior.get("scheduled_append_asof_dates")
        if prior_dates != list(scheduled_dates[:-1]):
            raise ValueError("Transportation predecessor schedule prefix changed")
        if prior.get("scheduled_append_asof_dates_sha256") != canonical_sha256(
            list(scheduled_dates[:-1])
        ):
            raise ValueError("Transportation predecessor schedule hash changed")
        prior_cutoff = _utc_timestamp(
            prior.get("signal_cutoff_at_utc"),
            label="Transportation predecessor signal cutoff",
        )
        if prior_cutoff >= signal_cutoff:
            raise ValueError("Transportation predecessor cutoff is not earlier")

        prior_panel_count = _canonical_int(
            prior.get("panel_row_count"),
            label="Transportation predecessor panel row count",
            minimum=1,
        )
        if prior_panel_count > len(panel_rows) or prior.get(
            "full_panel_rows_sha256"
        ) != canonical_sha256(list(panel_rows[:prior_panel_count])):
            raise ValueError("Transportation prior panel prefix was changed or deleted")
        prior_fact_count = _canonical_int(
            prior.get("accepted_fact_row_count"),
            label="Transportation predecessor fact row count",
            minimum=0,
        )
        if prior_fact_count > len(accepted_rows) or prior.get(
            "full_accepted_fact_rows_sha256"
        ) != canonical_sha256(list(accepted_rows[:prior_fact_count])):
            raise ValueError("Transportation prior fact prefix was changed or deleted")
        if any(
            timestamp <= prior_cutoff
            for timestamp in accepted_information_times[prior_fact_count:]
        ):
            raise ValueError(
                "Transportation newly appended fact is backdated at/before prior cutoff"
            )

    if any(timestamp > signal_cutoff for timestamp in accepted_information_times):
        raise ValueError("Transportation accepted facts contain post-signal information")
    return {
        "appended_panel_rows": appended_panel_rows,
        "appended_fact_rows": list(accepted_rows[len(baseline_fact_rows) :]),
        "prior_cutoff": prior_cutoff,
    }


def _validate_score_rows(
    rows: Sequence[Mapping[str, str]],
    *,
    asof: str,
    policy: Mapping[str, Any],
) -> dict[str, dict[str, str]]:
    capture_rows: dict[str, dict[str, str]] = {}
    for raw in rows:
        row = dict(raw)
        row_asof = _exact_date(row.get("asof_date"), label="v8 score asof")
        if row_asof > asof:
            raise ValueError("Transportation canonical scores contain post-checkpoint rows")
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker or ticker != row.get("ticker"):
            raise ValueError("Transportation score ticker must be canonical uppercase")
        if ticker_location(ticker, row_asof, policy) is None:
            raise ValueError(f"{row_asof}/{ticker}: score row is outside frozen policy")
        _lower_sha256(
            row.get("source_score_sha256"),
            label=f"{row_asof}/{ticker} score source sha256",
        )
        for field in _FLAG_SCORE_FIELDS:
            _binary_flag(row.get(field), label=f"{row_asof}/{ticker}/{field}")
        for field in _FLOAT_SCORE_FIELDS:
            _csv_finite(row.get(field), label=f"{row_asof}/{ticker}/{field}")
        for field in _JSON_SCORE_FIELDS:
            try:
                parsed = json.loads(str(row.get(field)))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{row_asof}/{ticker}/{field} is invalid JSON") from exc
            if not isinstance(parsed, dict):
                raise ValueError(f"{row_asof}/{ticker}/{field} must be an object")
        if row_asof == asof:
            if ticker in capture_rows:
                raise ValueError("Transportation capture score ticker is duplicated")
            capture_rows[ticker] = row
    if set(capture_rows) != _current_tickers():
        raise ValueError("Transportation canonical score lacks the frozen 35-ticker capture census")
    return capture_rows


def _compare_score_row(
    captured: Mapping[str, Any],
    replayed: Mapping[str, Any],
    *,
    ticker: str,
) -> None:
    if set(captured) != set(SCORE_FIELDS) or set(replayed) != set(SCORE_FIELDS):
        raise ValueError(f"{ticker}: v8 score field census changed")
    for field in SCORE_FIELDS:
        left = captured[field]
        right = replayed[field]
        if field in _FLOAT_SCORE_FIELDS:
            if not math.isclose(
                _csv_finite(left, label=f"{ticker}/{field} captured"),
                _csv_finite(right, label=f"{ticker}/{field} replayed"),
                rel_tol=0.0,
                abs_tol=1e-10,
            ):
                raise ValueError(f"{ticker}: replay changed {field}")
        elif field in _FLAG_SCORE_FIELDS:
            if _binary_flag(left, label=f"{ticker}/{field} captured") != _binary_flag(
                right, label=f"{ticker}/{field} replayed"
            ):
                raise ValueError(f"{ticker}: replay changed {field}")
        elif field in _JSON_SCORE_FIELDS:
            if canonical_sha256(json.loads(str(left))) != canonical_sha256(
                json.loads(str(right))
            ):
                raise ValueError(f"{ticker}: replay changed {field}")
        elif str(left) != str(right):
            raise ValueError(f"{ticker}: replay changed {field}")


def _eligibility_audit(row: Mapping[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    if _binary_flag(
        row["source_calibration_eligible_flag"],
        label="source calibration eligibility",
    ) == 0:
        reasons.append("source_calibration_ineligible")
    if _binary_flag(
        row["group_cross_section_ready_flag"], label="group cross-section readiness"
    ) == 0:
        reasons.append("group_cross_section_below_minimum")
    if _binary_flag(
        row["group_specialized_ready_flag"], label="group specialized readiness"
    ) == 0:
        reasons.append("required_specialized_pack_not_ready")
    reasons = sorted(reasons)
    eligible = int(not reasons)
    if eligible != _binary_flag(
        row["v8_calibration_eligible_flag"], label="v8 model-data eligibility"
    ):
        raise ValueError("Transportation scorer eligibility arithmetic is inconsistent")
    return {
        "model_data_eligible_flag": eligible,
        "model_data_exclusion_reason_codes": reasons,
    }


def validate_transport_score_replay_baseline_structure(
    *,
    baseline_path: Path,
    v8_policy_path: Path,
    expected_baseline_cutoff_at_utc: str,
    expected_sha256: Mapping[str, str],
    source_snapshot_bytes: Mapping[str, bytes] | None = None,
) -> dict[str, Any]:
    """Validate an unsigned baseline before requesting availability signing.

    This validates the exact policy, panel, accepted-fact, staleness, census,
    and cutoff semantics.  It deliberately does not authorize capture or
    activation because the independent availability attestation is absent.
    """

    expected_cutoff = _utc_timestamp(
        expected_baseline_cutoff_at_utc,
        label="Transportation expected baseline cutoff",
    )
    if set(expected_sha256) != STRUCTURAL_BASELINE_SOURCE_ROLES:
        raise ValueError(
            "Transportation structural baseline source-role census is not exact"
        )
    if (
        source_snapshot_bytes is not None
        and set(source_snapshot_bytes) != STRUCTURAL_BASELINE_SOURCE_ROLES
    ):
        raise ValueError(
            "Transportation structural baseline snapshot role census is not exact"
        )
    paths = {
        "score_replay_baseline": baseline_path,
        "v8_policy": v8_policy_path,
    }
    payloads: dict[str, bytes] = {}
    digests: dict[str, str] = {}
    for role in sorted(STRUCTURAL_BASELINE_SOURCE_ROLES):
        if source_snapshot_bytes is None:
            payload, digest = _read_snapshot(
                paths[role],
                role=role,
                expected_digest=expected_sha256[role],
            )
        else:
            payload = bytes(source_snapshot_bytes[role])
            digest = hashlib.sha256(payload).hexdigest()
            if digest != _lower_sha256(
                expected_sha256[role],
                label=f"{role} expected sha256",
            ):
                raise ValueError(
                    f"{role}: supplied snapshot differs from the frozen source identity"
                )
        payloads[role] = payload
        digests[role] = digest

    policy = _yaml_object(payloads["v8_policy"], label="Transportation v8 policy")
    _validate_policy(policy)
    baseline_payload = _json_object(
        payloads["score_replay_baseline"],
        label="Transportation score-replay baseline",
    )
    declared_cutoff = _utc_timestamp(
        baseline_payload.get("baseline_cutoff_at_utc"),
        label="Transportation replay baseline cutoff",
    )
    if declared_cutoff != expected_cutoff:
        raise ValueError("Transportation replay baseline cutoff differs from expected")
    baseline = _validate_baseline(
        baseline_payload,
        policy=policy,
        signal_cutoff=expected_cutoff + timedelta(microseconds=1),
    )

    dates = list(baseline["panel_census"])
    return {
        "schema_version": BASELINE_STRUCTURE_AUDIT_SCHEMA,
        "evidence_role": BASELINE_EVIDENCE_ROLE,
        "source_roles": sorted(STRUCTURAL_BASELINE_SOURCE_ROLES),
        "source_snapshot_sha256": dict(sorted(digests.items())),
        "baseline_cutoff_at_utc": _utc_text(baseline["cutoff"]),
        "baseline_panel_row_count": len(baseline["panel_rows"]),
        "baseline_panel_date_count": len(dates),
        "baseline_panel_date_min": dates[0],
        "baseline_panel_date_max": dates[-1],
        "baseline_panel_rows_sha256": canonical_sha256(baseline["panel_rows"]),
        "baseline_panel_date_ticker_census_sha256": canonical_sha256(
            baseline["panel_census"]
        ),
        "baseline_source_score_file_sha256_by_date_sha256": canonical_sha256(
            baseline["source_hashes"]
        ),
        "baseline_accepted_fact_row_count": len(baseline["fact_rows"]),
        "baseline_accepted_fact_rows_sha256": canonical_sha256(
            baseline["fact_rows"]
        ),
        "baseline_accepted_fact_identity_census_sha256": canonical_sha256(
            baseline["fact_identities"]
        ),
        "baseline_max_pipeline_fact_information_at_utc": (
            _utc_text(max(baseline["fact_information_times"]))
            if baseline["fact_information_times"]
            else None
        ),
        "frozen_staleness_days_sha256": canonical_sha256(baseline["staleness"]),
        "full_semantic_policy_validation_pass": True,
        "independent_source_availability_attestation_validated": False,
        "availability_signing_request_ready": True,
        "capture_ready": False,
        "production_activation_authorized": False,
    }


def validate_transport_replay_inputs_structure(
    *,
    asof_date: str,
    signal_cutoff_at_utc: str,
    scheduled_append_asof_dates: Sequence[str],
    scoring_panel_path: Path,
    accepted_facts_path: Path,
    score_replay_baseline_path: Path,
    v8_policy_path: Path,
    expected_baseline_cutoff_at_utc: str,
    expected_sha256: Mapping[str, str],
    predecessor_replay_audit: Mapping[str, Any] | None = None,
    source_snapshot_bytes: Mapping[str, bytes] | None = None,
) -> dict[str, Any]:
    """Validate unsigned cumulative replay inputs before availability signing.

    The result can authorize a signing request only.  Independent availability
    attestations and the canonical score replay remain mandatory for capture.
    """

    asof = _exact_date(asof_date, label="capture asof")
    signal_cutoff = _utc_timestamp(
        signal_cutoff_at_utc,
        label="Transportation signal cutoff",
    )
    if signal_cutoff.date().isoformat() != asof:
        raise ValueError("Transportation signal cutoff date differs from capture asof")
    if set(expected_sha256) != STRUCTURAL_REPLAY_INPUT_SOURCE_ROLES:
        raise ValueError(
            "Transportation structural replay-input source-role census is not exact"
        )
    if (
        source_snapshot_bytes is not None
        and set(source_snapshot_bytes) != STRUCTURAL_REPLAY_INPUT_SOURCE_ROLES
    ):
        raise ValueError(
            "Transportation structural replay-input snapshot census is not exact"
        )
    paths = {
        "scoring_panel": scoring_panel_path,
        "accepted_facts": accepted_facts_path,
        "score_replay_baseline": score_replay_baseline_path,
        "v8_policy": v8_policy_path,
    }
    payloads: dict[str, bytes] = {}
    digests: dict[str, str] = {}
    for role in sorted(STRUCTURAL_REPLAY_INPUT_SOURCE_ROLES):
        if source_snapshot_bytes is None:
            payload, digest = _read_snapshot(
                paths[role],
                role=role,
                expected_digest=expected_sha256[role],
            )
        else:
            payload = bytes(source_snapshot_bytes[role])
            digest = hashlib.sha256(payload).hexdigest()
            if digest != _lower_sha256(
                expected_sha256[role],
                label=f"{role} expected sha256",
            ):
                raise ValueError(
                    f"{role}: supplied snapshot differs from the frozen source identity"
                )
        payloads[role] = payload
        digests[role] = digest

    baseline_structure_audit = validate_transport_score_replay_baseline_structure(
        baseline_path=score_replay_baseline_path,
        v8_policy_path=v8_policy_path,
        expected_baseline_cutoff_at_utc=expected_baseline_cutoff_at_utc,
        expected_sha256={
            role: digests[role] for role in STRUCTURAL_BASELINE_SOURCE_ROLES
        },
        source_snapshot_bytes={
            role: payloads[role] for role in STRUCTURAL_BASELINE_SOURCE_ROLES
        },
    )
    policy = _yaml_object(payloads["v8_policy"], label="Transportation v8 policy")
    _validate_policy(policy)
    baseline_payload = _json_object(
        payloads["score_replay_baseline"],
        label="Transportation score-replay baseline",
    )
    baseline = _validate_baseline(
        baseline_payload,
        policy=policy,
        signal_cutoff=signal_cutoff,
    )
    scheduled_dates = _validate_scheduled_append_dates(
        scheduled_append_asof_dates,
        asof=asof,
        baseline_dates=list(baseline["panel_census"]),
    )
    panel_payload = _json_object(
        payloads["scoring_panel"], label="Transportation scoring panel"
    )
    panel_rows, panel_census, panel_source_hashes = _validate_panel(
        panel_payload,
        asof=asof,
        policy=policy,
    )
    facts_payload = _json_object(
        payloads["accepted_facts"], label="Transportation accepted facts"
    )
    (
        accepted_rows,
        staleness,
        accepted_information_times,
        accepted_identities,
    ) = _validate_facts(
        facts_payload,
        asof=asof,
        policy=policy,
        signal_cutoff=signal_cutoff,
    )
    append_audit = _validate_append_only_contract(
        panel_rows=panel_rows,
        panel_census=panel_census,
        panel_source_hashes=panel_source_hashes,
        accepted_rows=accepted_rows,
        accepted_information_times=accepted_information_times,
        accepted_identities=accepted_identities,
        staleness=staleness,
        baseline=baseline,
        scheduled_dates=scheduled_dates,
        signal_cutoff=signal_cutoff,
        predecessor_replay_audit=predecessor_replay_audit,
    )
    return {
        "schema_version": REPLAY_INPUT_STRUCTURE_AUDIT_SCHEMA,
        "evidence_role": EVIDENCE_ROLE,
        "asof_date": asof,
        "signal_cutoff_at_utc": _utc_text(signal_cutoff),
        "source_roles": sorted(STRUCTURAL_REPLAY_INPUT_SOURCE_ROLES),
        "source_snapshot_sha256": dict(sorted(digests.items())),
        "scheduled_append_asof_dates": scheduled_dates,
        "scheduled_append_asof_dates_sha256": canonical_sha256(scheduled_dates),
        "baseline_structure_audit": baseline_structure_audit,
        "panel_row_count": len(panel_rows),
        "panel_date_count": len(panel_census),
        "panel_date_ticker_census_sha256": canonical_sha256(panel_census),
        "panel_source_score_file_sha256_by_date_sha256": canonical_sha256(
            panel_source_hashes
        ),
        "full_panel_rows_sha256": canonical_sha256(panel_rows),
        "accepted_fact_row_count": len(accepted_rows),
        "accepted_fact_identity_census_sha256": canonical_sha256(
            accepted_identities
        ),
        "full_accepted_fact_rows_sha256": canonical_sha256(accepted_rows),
        "max_pipeline_fact_information_at_utc": (
            _utc_text(max(accepted_information_times))
            if accepted_information_times
            else None
        ),
        "frozen_staleness_days_sha256": canonical_sha256(staleness),
        "appended_panel_row_count": len(append_audit["appended_panel_rows"]),
        "appended_fact_row_count": len(append_audit["appended_fact_rows"]),
        "exact_baseline_prefix_pass": True,
        "exact_scheduled_append_pass": True,
        "append_only_accepted_facts_pass": True,
        "exact_frozen_staleness_pass": True,
        "pipeline_timestamp_cutoff_pass": True,
        "independent_source_availability_attestation_validated": False,
        "availability_signing_request_ready": True,
        "canonical_score_replay_validated": False,
        "capture_ready": False,
        "production_activation_authorized": False,
    }


def validate_transport_score_replay_baseline(
    *,
    baseline_path: Path,
    score_input_availability_baseline_snapshot_path: Path,
    score_input_availability_baseline_attestation_path: Path,
    v8_policy_path: Path,
    activation_registered_at_utc: str,
    policy_id: str,
    canonical_trust_bundle: CanonicalTrustBundle,
    expected_sha256: Mapping[str, str],
    source_snapshot_bytes: Mapping[str, bytes] | None = None,
) -> dict[str, Any]:
    """Validate the static score/fact baseline before prospective activation."""

    registered_at = _utc_timestamp(
        activation_registered_at_utc,
        label="Transportation baseline registration timestamp",
    )
    if set(expected_sha256) != BASELINE_SOURCE_ROLES:
        raise ValueError("Transportation baseline source-role census is not exact")
    if (
        source_snapshot_bytes is not None
        and set(source_snapshot_bytes) != BASELINE_SOURCE_ROLES
    ):
        raise ValueError("Transportation baseline snapshot role census is not exact")
    paths = {
        "score_replay_baseline": baseline_path,
        "score_input_availability_baseline_snapshot": (
            score_input_availability_baseline_snapshot_path
        ),
        "score_input_availability_baseline_attestation": (
            score_input_availability_baseline_attestation_path
        ),
        "v8_policy": v8_policy_path,
    }
    payloads: dict[str, bytes] = {}
    digests: dict[str, str] = {}
    for role in sorted(BASELINE_SOURCE_ROLES):
        if source_snapshot_bytes is None:
            payload, digest = _read_snapshot(
                paths[role],
                role=role,
                expected_digest=expected_sha256[role],
            )
        else:
            payload = bytes(source_snapshot_bytes[role])
            digest = hashlib.sha256(payload).hexdigest()
            if digest != _lower_sha256(
                expected_sha256[role],
                label=f"{role} expected sha256",
            ):
                raise ValueError(
                    f"{role}: supplied snapshot differs from the frozen source identity"
                )
        payloads[role] = payload
        digests[role] = digest

    policy = _yaml_object(payloads["v8_policy"], label="Transportation v8 policy")
    _validate_policy(policy)
    baseline_payload = _json_object(
        payloads["score_replay_baseline"],
        label="Transportation score-replay baseline",
    )
    baseline = _validate_baseline(
        baseline_payload,
        policy=policy,
        signal_cutoff=registered_at,
    )
    _, baseline_fact_availability, baseline_availability_audit = (
        validate_transport_score_input_availability_snapshot(
            score_input_availability_baseline_snapshot_path,
            asof_date=baseline["cutoff"].date().isoformat(),
            expected_panel_rows=baseline["panel_rows"],
            expected_accepted_fact_rows=baseline["fact_rows"],
            signal_cutoff_at_utc=_utc_text(baseline["cutoff"]),
            policy_id=policy_id,
            attestation_path=(
                score_input_availability_baseline_attestation_path
            ),
            expected_attestation_sha256=digests[
                "score_input_availability_baseline_attestation"
            ],
            bundle=canonical_trust_bundle,
            expected_evidence_role=ACTIVATION_BASELINE_ROLE,
            snapshot_bytes=payloads[
                "score_input_availability_baseline_snapshot"
            ],
            attestation_bytes=payloads[
                "score_input_availability_baseline_attestation"
            ],
        )
    )
    baseline_availability_exported = _utc_timestamp(
        baseline_availability_audit["source_attestation_exported_at_utc"],
        label="Transportation baseline availability export time",
    )
    if baseline_availability_exported > registered_at:
        raise ValueError(
            "Transportation baseline availability was attested after activation"
        )
    combined_baseline_fact_times = [
        max(
            pipeline_time,
            _utc_timestamp(
                baseline_fact_availability[transport_fact_identity(row)][
                    "source_available_at_utc"
                ],
                label="Transportation baseline fact source availability",
            ),
        )
        for row, pipeline_time in zip(
            baseline["fact_rows"],
            baseline["fact_information_times"],
        )
    ]
    dates = list(baseline["panel_census"])
    return {
        "schema_version": BASELINE_SCHEMA,
        "evidence_role": BASELINE_EVIDENCE_ROLE,
        "source_roles": sorted(BASELINE_SOURCE_ROLES),
        "source_snapshot_sha256": dict(sorted(digests.items())),
        "activation_registered_at_utc": _utc_text(registered_at),
        "baseline_cutoff_at_utc": _utc_text(baseline["cutoff"]),
        "baseline_panel_row_count": len(baseline["panel_rows"]),
        "baseline_panel_date_count": len(dates),
        "baseline_panel_date_min": dates[0],
        "baseline_panel_date_max": dates[-1],
        "baseline_panel_rows_sha256": canonical_sha256(baseline["panel_rows"]),
        "baseline_panel_date_ticker_census_sha256": canonical_sha256(
            baseline["panel_census"]
        ),
        "baseline_source_score_file_sha256_by_date_sha256": canonical_sha256(
            baseline["source_hashes"]
        ),
        "baseline_accepted_fact_row_count": len(baseline["fact_rows"]),
        "baseline_accepted_fact_rows_sha256": canonical_sha256(
            baseline["fact_rows"]
        ),
        "baseline_accepted_fact_identity_census_sha256": canonical_sha256(
            baseline["fact_identities"]
        ),
        "baseline_max_fact_information_at_utc": (
            _utc_text(max(combined_baseline_fact_times))
            if combined_baseline_fact_times
            else None
        ),
        "baseline_max_source_information_at_utc": baseline_availability_audit[
            "max_source_available_at_utc"
        ],
        "score_input_availability_audit": baseline_availability_audit,
        "frozen_staleness_days_sha256": canonical_sha256(baseline["staleness"]),
        "full_semantic_policy_validation_pass": True,
        "exact_activation_baseline_pass": True,
        "production_activation_authorized": False,
    }


def validate_and_replay_transport_scores(
    *,
    asof_date: str,
    signal_cutoff_at_utc: str,
    scheduled_append_asof_dates: Sequence[str],
    score_path: Path,
    scoring_panel_path: Path,
    accepted_facts_path: Path,
    score_replay_baseline_path: Path,
    score_input_availability_baseline_snapshot_path: Path,
    score_input_availability_baseline_attestation_path: Path,
    score_input_availability_snapshot_path: Path,
    score_input_availability_attestation_path: Path,
    v8_policy_path: Path,
    policy_id: str,
    canonical_trust_bundle: CanonicalTrustBundle,
    expected_sha256: Mapping[str, str],
    predecessor_replay_audit: Mapping[str, Any] | None = None,
    predecessor_score_input_availability_audit: Mapping[str, Any] | None = None,
    source_snapshot_bytes: Mapping[str, bytes] | None = None,
) -> dict[str, Any]:
    """Replay and attest the exact frozen v8 scores used at ``asof_date``.

    Every source is read once.  The bytes used for semantic parsing are the
    same bytes checked against the source identity, preventing split-read/ABA
    substitutions inside this validator.
    """

    asof = _exact_date(asof_date, label="capture asof")
    signal_cutoff = _utc_timestamp(
        signal_cutoff_at_utc,
        label="Transportation signal cutoff",
    )
    if signal_cutoff.date().isoformat() != asof:
        raise ValueError("Transportation signal cutoff date differs from capture asof")
    if set(expected_sha256) != SOURCE_ROLES:
        raise ValueError("Transportation replay source-role census is not exact")
    if (
        source_snapshot_bytes is not None
        and set(source_snapshot_bytes) != SOURCE_ROLES
    ):
        raise ValueError("Transportation replay snapshot source-role census is not exact")
    paths = {
        "canonical_v8_score": score_path,
        "scoring_panel": scoring_panel_path,
        "accepted_facts": accepted_facts_path,
        "score_replay_baseline": score_replay_baseline_path,
        "score_input_availability_baseline_snapshot": (
            score_input_availability_baseline_snapshot_path
        ),
        "score_input_availability_baseline_attestation": (
            score_input_availability_baseline_attestation_path
        ),
        "score_input_availability_snapshot": (
            score_input_availability_snapshot_path
        ),
        "score_input_availability_attestation": (
            score_input_availability_attestation_path
        ),
        "v8_policy": v8_policy_path,
    }
    source_bytes: dict[str, bytes] = {}
    source_digests: dict[str, str] = {}
    for role in sorted(SOURCE_ROLES):
        if source_snapshot_bytes is None:
            payload, digest = _read_snapshot(
                paths[role], role=role, expected_digest=expected_sha256[role]
            )
        else:
            payload = bytes(source_snapshot_bytes[role])
            digest = hashlib.sha256(payload).hexdigest()
            if digest != _lower_sha256(
                expected_sha256[role],
                label=f"{role} expected sha256",
            ):
                raise ValueError(
                    f"{role}: supplied snapshot differs from the frozen source identity"
                )
        source_bytes[role] = payload
        source_digests[role] = digest

    policy = _yaml_object(source_bytes["v8_policy"], label="Transportation v8 policy")
    _validate_policy(policy)
    baseline_payload = _json_object(
        source_bytes["score_replay_baseline"],
        label="Transportation score-replay baseline",
    )
    baseline = _validate_baseline(
        baseline_payload,
        policy=policy,
        signal_cutoff=signal_cutoff,
    )
    _, _, baseline_availability_audit = (
        validate_transport_score_input_availability_snapshot(
            score_input_availability_baseline_snapshot_path,
            asof_date=baseline["cutoff"].date().isoformat(),
            expected_panel_rows=baseline["panel_rows"],
            expected_accepted_fact_rows=baseline["fact_rows"],
            signal_cutoff_at_utc=_utc_text(baseline["cutoff"]),
            policy_id=policy_id,
            attestation_path=(
                score_input_availability_baseline_attestation_path
            ),
            expected_attestation_sha256=source_digests[
                "score_input_availability_baseline_attestation"
            ],
            bundle=canonical_trust_bundle,
            expected_evidence_role=ACTIVATION_BASELINE_ROLE,
            snapshot_bytes=source_bytes[
                "score_input_availability_baseline_snapshot"
            ],
            attestation_bytes=source_bytes[
                "score_input_availability_baseline_attestation"
            ],
        )
    )
    scheduled_dates = _validate_scheduled_append_dates(
        scheduled_append_asof_dates,
        asof=asof,
        baseline_dates=list(baseline["panel_census"]),
    )
    panel_payload = _json_object(
        source_bytes["scoring_panel"], label="Transportation scoring panel"
    )
    panel_rows, panel_census, panel_source_hashes = _validate_panel(
        panel_payload, asof=asof, policy=policy
    )
    facts_payload = _json_object(
        source_bytes["accepted_facts"], label="Transportation accepted facts"
    )
    (
        accepted_rows,
        staleness,
        accepted_information_times,
        accepted_identities,
    ) = _validate_facts(
        facts_payload,
        asof=asof,
        policy=policy,
        signal_cutoff=signal_cutoff,
    )
    if predecessor_score_input_availability_audit is None:
        raise ValueError(
            "Transportation predecessor score-input availability audit is required"
        )
    expected_predecessor_availability = (
        baseline_availability_audit
        if predecessor_replay_audit is None
        else predecessor_replay_audit.get("score_input_availability_audit")
    )
    if (
        not isinstance(expected_predecessor_availability, Mapping)
        or dict(predecessor_score_input_availability_audit)
        != dict(expected_predecessor_availability)
    ):
        raise ValueError(
            "Transportation predecessor availability is not bound to activation/prior capture"
        )
    _, fact_availability, score_input_availability_audit = (
        validate_transport_score_input_availability_snapshot(
            score_input_availability_snapshot_path,
            asof_date=asof,
            expected_panel_rows=panel_rows,
            expected_accepted_fact_rows=accepted_rows,
            signal_cutoff_at_utc=signal_cutoff_at_utc,
            policy_id=policy_id,
            attestation_path=score_input_availability_attestation_path,
            expected_attestation_sha256=source_digests[
                "score_input_availability_attestation"
            ],
            bundle=canonical_trust_bundle,
            predecessor_availability_audit=(
                predecessor_score_input_availability_audit
            ),
            snapshot_bytes=source_bytes[
                "score_input_availability_snapshot"
            ],
            attestation_bytes=source_bytes[
                "score_input_availability_attestation"
            ],
        )
    )
    accepted_information_times = [
        max(
            pipeline_time,
            _utc_timestamp(
                fact_availability[transport_fact_identity(row)][
                    "source_available_at_utc"
                ],
                label="Transportation accepted-fact source availability",
            ),
        )
        for row, pipeline_time in zip(
            accepted_rows,
            accepted_information_times,
        )
    ]
    append_audit = _validate_append_only_contract(
        panel_rows=panel_rows,
        panel_census=panel_census,
        panel_source_hashes=panel_source_hashes,
        accepted_rows=accepted_rows,
        accepted_information_times=accepted_information_times,
        accepted_identities=accepted_identities,
        staleness=staleness,
        baseline=baseline,
        scheduled_dates=scheduled_dates,
        signal_cutoff=signal_cutoff,
        predecessor_replay_audit=predecessor_replay_audit,
    )
    captured_score_rows = _validate_score_rows(
        _csv_rows(
            source_bytes["canonical_v8_score"],
            label="Transportation canonical v8 score",
        ),
        asof=asof,
        policy=policy,
    )

    replay_rows, coverage_rows, manifest = build_v8_score_rows(
        panel_rows=panel_rows,
        accepted_rows=accepted_rows,
        policy=policy,
        staleness_days=staleness,
    )
    replay_capture: dict[str, dict[str, Any]] = {}
    for raw in replay_rows:
        row = dict(raw)
        score_date = _exact_date(row.get("asof_date"), label="replayed score asof")
        if score_date > asof:
            raise ValueError("Transportation replay produced post-checkpoint scores")
        if score_date == asof:
            ticker = str(row.get("ticker") or "").upper()
            if ticker in replay_capture:
                raise ValueError("Transportation replay duplicated a capture ticker")
            replay_capture[ticker] = row
    if set(replay_capture) != _current_tickers():
        raise ValueError("Transportation replay lacks the exact capture ticker census")
    for ticker in sorted(replay_capture):
        _compare_score_row(
            captured_score_rows[ticker], replay_capture[ticker], ticker=ticker
        )

    score_dates = sorted(panel_census)
    if (
        manifest.get("policy_version") != POLICY_VERSION
        or manifest.get("score_date_count") != len(score_dates)
        or manifest.get("score_date_min") != score_dates[0]
        or manifest.get("score_date_max") != score_dates[-1]
        or manifest.get("score_row_count") != len(replay_rows)
        or manifest.get("network_requests") != 0
        or manifest.get("parser_invocations") != 0
        or manifest.get("production_activation_authorized") is not False
    ):
        raise ValueError("Transportation replay manifest invariants failed")
    for row in coverage_rows:
        if _exact_date(row.get("score_date"), label="coverage score date") > asof:
            raise ValueError("Transportation replay coverage uses post-checkpoint data")

    eligibility = {
        ticker: _eligibility_audit(replay_capture[ticker])
        for ticker in sorted(replay_capture)
    }
    normalized_capture_rows = [replay_capture[ticker] for ticker in sorted(replay_capture)]
    return {
        "schema_version": AUDIT_SCHEMA,
        "score_formula_id": SCORE_FORMULA_ID,
        "scoring_panel_schema": PANEL_SCHEMA,
        "accepted_facts_schema": ACCEPTED_FACTS_SCHEMA,
        "score_replay_baseline_schema": BASELINE_SCHEMA,
        "governed_source_horizon_sessions": GOVERNED_HORIZON_SESSIONS,
        "source_roles": sorted(SOURCE_ROLES),
        "no_reestimation_policy": "frozen_v8_no_outcome_reestimation_v1",
        "asof_date": asof,
        "signal_cutoff_at_utc": _utc_text(signal_cutoff),
        "source_snapshot_sha256": dict(sorted(source_digests.items())),
        "ticker_count": len(normalized_capture_rows),
        "panel_date_count": len(panel_census),
        "panel_date_min": score_dates[0],
        "panel_date_max": score_dates[-1],
        "full_panel_date_ticker_census_sha256": canonical_sha256(panel_census),
        "panel_row_count": len(panel_rows),
        "full_panel_rows_sha256": canonical_sha256(panel_rows),
        "source_score_file_date_count": len(panel_source_hashes),
        "source_score_file_sha256_by_date_sha256": canonical_sha256(
            panel_source_hashes
        ),
        "capture_date_score_rows_sha256": canonical_sha256(normalized_capture_rows),
        "accepted_fact_row_count": len(accepted_rows),
        "full_accepted_fact_rows_sha256": canonical_sha256(accepted_rows),
        "full_accepted_fact_identity_census_sha256": canonical_sha256(
            accepted_identities
        ),
        "max_fact_information_at_utc": (
            _utc_text(max(accepted_information_times))
            if accepted_information_times
            else None
        ),
        "max_source_information_at_utc": score_input_availability_audit[
            "max_source_available_at_utc"
        ],
        "score_input_availability_baseline_audit": (
            baseline_availability_audit
        ),
        "score_input_availability_audit": score_input_availability_audit,
        "baseline_cutoff_at_utc": _utc_text(baseline["cutoff"]),
        "baseline_panel_rows_sha256": canonical_sha256(baseline["panel_rows"]),
        "baseline_panel_date_ticker_census_sha256": canonical_sha256(
            baseline["panel_census"]
        ),
        "baseline_source_score_file_sha256_by_date_sha256": canonical_sha256(
            baseline["source_hashes"]
        ),
        "baseline_accepted_fact_rows_sha256": canonical_sha256(
            baseline["fact_rows"]
        ),
        "frozen_staleness_days_sha256": canonical_sha256(staleness),
        "scheduled_append_asof_dates": list(scheduled_dates),
        "scheduled_append_asof_dates_sha256": canonical_sha256(
            list(scheduled_dates)
        ),
        "appended_panel_rows_sha256": canonical_sha256(
            append_audit["appended_panel_rows"]
        ),
        "appended_accepted_fact_rows_sha256": canonical_sha256(
            append_audit["appended_fact_rows"]
        ),
        "replay_coverage_rows_sha256": canonical_sha256(coverage_rows),
        "replay_manifest_sha256": canonical_sha256(manifest),
        "model_data_eligibility_by_ticker": eligibility,
        "exact_panel_date_ticker_census_pass": True,
        "exact_baseline_prefix_pass": True,
        "exact_scheduled_append_pass": True,
        "append_only_accepted_facts_pass": True,
        "exact_frozen_staleness_pass": True,
        "exact_information_cutoff_pass": True,
        "market_authority_attested_full_score_inputs_pass": True,
        "no_post_checkpoint_inputs_pass": True,
        "exact_model_score_replay_pass": True,
        "no_reestimation_from_outcomes_pass": True,
        "production_activation_authorized": False,
    }


__all__ = [
    "ACCEPTED_FACTS_SCHEMA",
    "AUDIT_SCHEMA",
    "BASELINE_EVIDENCE_ROLE",
    "BASELINE_SCHEMA",
    "BASELINE_STRUCTURE_AUDIT_SCHEMA",
    "BASELINE_SOURCE_ROLES",
    "EVIDENCE_ROLE",
    "GOVERNED_HORIZON_SESSIONS",
    "PANEL_SCHEMA",
    "REPLAY_INPUT_STRUCTURE_AUDIT_SCHEMA",
    "SCORE_FIELDS",
    "SCORE_FORMULA_ID",
    "SOURCE_ROLES",
    "STRUCTURAL_BASELINE_SOURCE_ROLES",
    "STRUCTURAL_REPLAY_INPUT_SOURCE_ROLES",
    "validate_transport_replay_inputs_structure",
    "validate_transport_score_replay_baseline_structure",
    "validate_transport_score_replay_baseline",
    "validate_and_replay_transport_scores",
]

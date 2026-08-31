"""Replay prospective Consumer scores from atomic Stage 6 observations.

Timestamped bytes establish chronology.  This validator establishes a
separate invariant: the score/rank bytes must be the deterministic output of
the registered Stage 6/7 contracts and frozen cohort model.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from future_only_evidence.canonical_trust import CanonicalTrustBundle
from future_only_evidence.canonical_values import exact_utc
from future_only_evidence.protocol import canonical_sha256
from future_only_evidence.prospective_contracts import read_json_snapshot
from future_only_evidence.score_input_availability import (
    validate_score_input_availability_snapshot,
)

from .scoring_features import (
    COMPONENT_IDENTITY_FIELDS,
    CORE_COMPONENT_SPECS,
    INPUT_IDENTITY_FIELDS,
    component_observation_id,
    input_observation_id,
)
from .future_oos_capture_v4 import RANK_SNAPSHOT_SCHEMA
from .stage7_scoring import score_observation_id


BASELINE_SPEC_SCHEMA = "consumer_defensive_frozen_baseline_spec_v2"
FEATURE_SNAPSHOT_SCHEMA = "consumer_defensive_future_atomic_score_inputs_v2"
FEATURE_SNAPSHOT_FIELDS = frozenset(
    {
        "schema_version",
        "evidence_role",
        "asof_date",
        "input_rows",
        "input_rows_sha256",
        "component_rows",
        "component_rows_sha256",
    }
)
SCORE_FORMULA_ID = "atomic_stage6_stage7_then_frozen_candidate_v1"
QUALITY_GATE_ARITHMETIC = "sum_full_weight_only_when_binary_quality_is_one_v1"
SPECIALIZED_SCORING_POLICY = "zero_weight_until_factor_validation_v1"
CORE_NAMES = tuple(spec.name for spec in CORE_COMPONENT_SPECS)
CORE_SET = set(CORE_NAMES)


def _load_json(
    path: Path,
    label: str,
    *,
    snapshot_bytes: bytes | None = None,
) -> dict[str, Any]:
    if snapshot_bytes is None:
        payload, _, _, _ = read_json_snapshot(path, label=label)
        return payload
    try:
        payload = json.loads(bytes(snapshot_bytes).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be one valid UTF-8 JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _finite(value: Any, label: str) -> float:
    if type(value) not in {int, float}:
        raise ValueError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _optional_finite(value: Any, label: str) -> float | None:
    return None if value is None else _finite(value, label)


def _strict_int(value: Any, label: str, *, minimum: int | None = None) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be a canonical integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    return value


def _strict_flag(value: Any, label: str) -> int:
    flag = _strict_int(value, label)
    if flag not in (0, 1):
        raise ValueError(f"{label} must be strict 0/1")
    return flag


def _model_data_eligibility(
    *,
    stage7_eligible_flag: int,
    available_weight: float,
    missing_weight: float,
    minimum_quality: float,
    maximum_missing: float,
) -> tuple[int, list[str]]:
    reasons: list[str] = []
    if _strict_flag(stage7_eligible_flag, "Stage 7 model/data eligibility") != 1:
        reasons.append("stage7_model_data_not_rank_ready")
    if available_weight + 1e-12 < minimum_quality:
        reasons.append("candidate_low_data_quality")
    if missing_weight > maximum_missing + 1e-12:
        reasons.append("candidate_missing_weight_exceeded")
    canonical_reasons = sorted(set(reasons))
    return int(not canonical_reasons), canonical_reasons


def _exact_date(value: Any, expected: str, label: str) -> str:
    if type(value) is not str or type(expected) is not str:
        raise ValueError(f"{label} must be explicit YYYY-MM-DD")
    text = value
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be explicit YYYY-MM-DD") from exc
    if parsed.isoformat() != text or text != expected:
        raise ValueError(f"{label} differs from capture asof")
    return text


def _sha256(value: Any, label: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be a lowercase sha256")
    text = value
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{label} must be a lowercase sha256")
    return text


def _canonical_ticker(value: Any, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or value.upper() != value
    ):
        raise ValueError(f"{label} must be canonical uppercase")
    return value


def component_input_value_sha256(row: Mapping[str, Any]) -> str:
    """Bind the exact atomic component input semantics to provider evidence."""

    return canonical_sha256(
        {
            "ticker": row.get("ticker"),
            "component_name": row.get("component_name"),
            "availability_status": row.get("availability_status"),
            "source_table": row.get("source_table"),
            "source_id": row.get("source_id"),
            "source_field": row.get("source_field"),
            "source_asof_date": row.get("source_asof_date"),
            "raw_value": row.get("raw_value"),
        }
    )


def _weights(raw: Any, names: set[str], label: str) -> dict[str, float]:
    if not isinstance(raw, dict) or set(raw) != names:
        raise ValueError(f"{label} does not have the exact frozen key census")
    result = {
        str(name): _finite(value, f"{label}/{name}")
        for name, value in raw.items()
    }
    if any(value < 0.0 for value in result.values()):
        raise ValueError(f"{label} contains a negative weight")
    return dict(sorted(result.items()))


def validate_frozen_baseline_spec(
    path: Path,
    *,
    expected_cohorts: Sequence[str],
    baseline_snapshot_bytes: bytes | None = None,
) -> dict[str, Any]:
    snapshot_path = Path(path).expanduser().resolve()
    if baseline_snapshot_bytes is None:
        payload, snapshot_sha256, snapshot_path, snapshot_size = read_json_snapshot(
            snapshot_path,
            label="Consumer frozen baseline spec",
        )
    else:
        raw_bytes = bytes(baseline_snapshot_bytes)
        payload = _load_json(
            snapshot_path,
            "Consumer frozen baseline spec",
            snapshot_bytes=raw_bytes,
        )
        snapshot_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        snapshot_size = len(raw_bytes)
    fixed = {
        "schema_version": BASELINE_SPEC_SCHEMA,
        "score_formula_id": SCORE_FORMULA_ID,
        "quality_gate_arithmetic": QUALITY_GATE_ARITHMETIC,
        "specialized_scoring_policy": SPECIALIZED_SCORING_POLICY,
    }
    for field, expected in fixed.items():
        if payload.get(field) != expected:
            raise ValueError(f"Consumer frozen baseline changed {field}")
    neutral = _finite(payload.get("neutral_score"), "neutral_score")
    minimum_quality = _finite(
        payload.get("minimum_data_quality_confidence"),
        "minimum_data_quality_confidence",
    )
    maximum_missing = _finite(
        payload.get("maximum_missing_component_weight"),
        "maximum_missing_component_weight",
    )
    minimum_peers = _strict_int(
        payload.get("minimum_normalization_peer_count"),
        "minimum_normalization_peer_count",
        minimum=2,
    )
    if not 0.0 <= neutral <= 100.0:
        raise ValueError("Consumer neutral score is outside [0,100]")
    if not 0.0 <= minimum_quality <= 1.0:
        raise ValueError("Consumer minimum quality is outside [0,1]")
    if not 0.0 <= maximum_missing <= 1.0:
        raise ValueError("Consumer maximum missing weight is outside [0,1]")
    stage6_hash = _sha256(
        payload.get("stage6_contract_sha256"), "stage6_contract_sha256"
    )
    stage7_hash = _sha256(
        payload.get("stage7_model_contract_sha256"),
        "stage7_model_contract_sha256",
    )
    definition_version = str(payload.get("stage6_definition_version") or "")
    input_source_id = str(payload.get("stage6_input_source_id") or "")
    if not definition_version or not input_source_id:
        raise ValueError("Consumer frozen Stage 6 identity is incomplete")
    specialized = payload.get("specialized_component_names")
    if (
        not isinstance(specialized, list)
        or specialized != sorted(specialized)
        or len(specialized) != len(set(specialized))
        or any(
            not isinstance(name, str) or not name.startswith("specialized:")
            for name in specialized
        )
    ):
        raise ValueError("Consumer specialized component census is not exact")
    specialized_set = set(specialized)
    stage7_weights = _weights(
        payload.get("stage7_core_weights"), CORE_SET, "stage7_core_weights"
    )
    if not math.isclose(sum(stage7_weights.values()), 1.0, abs_tol=1e-12):
        raise ValueError("Consumer frozen Stage 7 weights must sum to one")
    stage7_contract = payload.get("stage7_output_contract")
    stage7_fields = {
        "source_id",
        "baseline_source_id",
        "model_version",
        "promotion_state",
        "portfolio_candidate_gate",
        "oos_score_valid_flag",
        "specialized_weight_policy",
        "factor_validation_campaign_id",
        "factor_validation_verdict",
    }
    if not isinstance(stage7_contract, dict) or set(stage7_contract) != stage7_fields:
        raise ValueError("Consumer frozen Stage 7 output contract is incomplete")
    string_fields = stage7_fields - {
        "portfolio_candidate_gate",
        "oos_score_valid_flag",
    }
    if any(not str(stage7_contract[field] or "") for field in string_fields):
        raise ValueError("Consumer frozen Stage 7 string identity is blank")
    for field in ("portfolio_candidate_gate", "oos_score_valid_flag"):
        _strict_flag(stage7_contract[field], f"Consumer frozen {field}")

    models = payload.get("cohort_models")
    if not isinstance(models, dict) or set(models) != set(expected_cohorts):
        raise ValueError("Consumer frozen cohort-model census changed")
    normalized_models: dict[str, dict[str, Any]] = {}
    for cohort, model in sorted(models.items()):
        if not isinstance(model, dict) or set(model) != {
            "candidate_id",
            "core_weights",
            "specialized_weights",
        }:
            raise ValueError(f"{cohort}: frozen model identity is incomplete")
        candidate_id = str(model.get("candidate_id") or "")
        if not candidate_id:
            raise ValueError(f"{cohort}: frozen candidate id is blank")
        core_weights = _weights(
            model.get("core_weights"), CORE_SET, f"{cohort}/core_weights"
        )
        specialized_weights = _weights(
            model.get("specialized_weights"),
            specialized_set,
            f"{cohort}/specialized_weights",
        )
        if any(value != 0.0 for value in specialized_weights.values()):
            raise ValueError(
                f"{cohort}: nonzero specialized weight lacks a validated replay policy"
            )
        if not math.isclose(
            sum(core_weights.values()) + sum(specialized_weights.values()),
            1.0,
            abs_tol=1e-12,
        ):
            raise ValueError(f"{cohort}: frozen model weights must sum to one")
        normalized_models[str(cohort)] = {
            "candidate_id": candidate_id,
            "core_weights": core_weights,
            "specialized_weights": specialized_weights,
        }
    identity = {
        **fixed,
        "neutral_score": neutral,
        "minimum_data_quality_confidence": minimum_quality,
        "maximum_missing_component_weight": maximum_missing,
        "minimum_normalization_peer_count": minimum_peers,
        "stage6_contract_sha256": stage6_hash,
        "stage6_definition_version": definition_version,
        "stage6_input_source_id": input_source_id,
        "stage7_model_contract_sha256": stage7_hash,
        "stage7_core_weights": stage7_weights,
        "stage7_output_contract": dict(stage7_contract),
        "specialized_component_names": list(specialized),
        "cohort_models": normalized_models,
    }
    return {
        **identity,
        "model_identity_sha256": canonical_sha256(identity),
        "source_snapshot": {
            "path": str(snapshot_path),
            "sha256": snapshot_sha256,
            "bytes": snapshot_size,
        },
    }


def _rank_readiness(rows: Sequence[Mapping[str, Any]]) -> tuple[int, list[str]]:
    usable = {
        str(row["component_name"])
        for row in rows
        if row["availability_status"] == "available"
        and row["normalized_value"] is not None
    }
    reasons = [
        f"missing_required:{spec.name}"
        for spec in CORE_COMPONENT_SPECS
        if spec.rank_requirement == "required" and spec.name not in usable
    ]
    for requirement in ("any_financial", "any_short"):
        if not any(
            spec.name in usable
            for spec in CORE_COMPONENT_SPECS
            if spec.rank_requirement == requirement
        ):
            reasons.append(f"missing_requirement:{requirement}")
    return (0 if reasons else 1), reasons


def _percentile(value: float, peers: Sequence[float], direction: str) -> float:
    lower = sum(peer < value for peer in peers)
    equal = sum(peer == value for peer in peers)
    result = 100.0 * (lower + (equal - 1) / 2.0) / (len(peers) - 1)
    return 100.0 - result if direction == "lower" else result


def _validate_normalization(
    rows: Sequence[Mapping[str, Any]],
    *,
    minimum_peers: int,
) -> None:
    for name in CORE_NAMES:
        named = [
            row
            for row in rows
            if row["component_name"] == name
            and row["raw_value"] is not None
            and row["availability_status"] == "available"
        ]
        global_peers = [float(row["raw_value"]) for row in named]
        for row in [item for item in rows if item["component_name"] == name]:
            observed_score = _optional_finite(
                row["component_score"], f"{row['ticker']}/{name} score"
            )
            observed_normalized = _optional_finite(
                row["normalized_value"], f"{row['ticker']}/{name} normalized"
            )
            try:
                lineage = json.loads(str(row["lineage_json"]))
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"{row['ticker']}/{name}: invalid component lineage"
                ) from exc
            if not isinstance(lineage, dict):
                raise ValueError(f"{row['ticker']}/{name}: lineage is not an object")
            if row not in named:
                if observed_score is not None or observed_normalized is not None:
                    raise ValueError(
                        f"{row['ticker']}/{name}: unavailable component has score"
                    )
                continue
            cohort_peers = [
                float(peer["raw_value"])
                for peer in named
                if peer["calibration_cohort_id"] == row["calibration_cohort_id"]
            ]
            peers = cohort_peers
            scope = "cohort"
            if len(peers) < minimum_peers or len(set(peers)) < 2:
                peers = global_peers
                scope = "universe_fallback"
            expected_score: float | None = None
            if len(peers) < minimum_peers or len(set(peers)) < 2:
                scope = "unavailable"
            else:
                expected_score = _percentile(
                    float(row["raw_value"]), peers, str(row["direction"])
                )
            if expected_score is None:
                if observed_score is not None or observed_normalized is not None:
                    raise ValueError(
                        f"{row['ticker']}/{name}: normalization should be unavailable"
                    )
            elif (
                observed_score is None
                or observed_normalized is None
                or not math.isclose(observed_score, expected_score, abs_tol=1e-10)
                or not math.isclose(
                    observed_normalized, expected_score, abs_tol=1e-10
                )
            ):
                raise ValueError(
                    f"{row['ticker']}/{name}: normalized score does not replay"
                )
            if lineage.get("normalization_scope") != scope:
                raise ValueError(
                    f"{row['ticker']}/{name}: normalization scope does not replay"
                )


def _assign_stage7_ranks(rows: list[dict[str, Any]]) -> None:
    rankable = sorted(
        [row for row in rows if int(row["rank_ready_flag"]) == 1],
        key=lambda row: (-float(row["final_score"]), str(row["ticker"])),
    )
    for rank, row in enumerate(rankable, 1):
        row["final_rank"] = rank
        row["final_percentile"] = (
            100.0 * (len(rankable) - rank + 0.5) / len(rankable)
        )
    cohorts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rankable:
        cohorts[str(row["calibration_cohort_id"])].append(row)
    for cohort_rows in cohorts.values():
        cohort_rows.sort(
            key=lambda row: (-float(row["final_score"]), str(row["ticker"]))
        )
        for rank, row in enumerate(cohort_rows, 1):
            row["cohort_rank"] = rank
            row["cohort_percentile"] = (
                100.0 * (len(cohort_rows) - rank + 0.5) / len(cohort_rows)
            )
    for row in rows:
        for field in (
            "final_rank",
            "final_percentile",
            "cohort_rank",
            "cohort_percentile",
        ):
            row.setdefault(field, None)


def _atomic_rows(
    snapshot: Mapping[str, Any],
    *,
    baseline: Mapping[str, Any],
    asof: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    inputs = snapshot.get("input_rows")
    components = snapshot.get("component_rows")
    if (
        not isinstance(inputs, list)
        or not inputs
        or snapshot.get("input_rows_sha256") != canonical_sha256(inputs)
        or not isinstance(components, list)
        or not components
        or snapshot.get("component_rows_sha256") != canonical_sha256(components)
    ):
        raise ValueError("Consumer atomic scoring rows are absent/hash-inconsistent")
    input_fields = {*INPUT_IDENTITY_FIELDS, "input_observation_id"}
    component_fields = {
        *COMPONENT_IDENTITY_FIELDS,
        "calibration_cohort_id",
        "component_observation_id",
    }
    input_index: dict[str, dict[str, Any]] = {}
    for raw in inputs:
        if not isinstance(raw, dict) or set(raw) != input_fields:
            raise ValueError("Consumer Stage 6 input row field census changed")
        row = dict(raw)
        ticker = _canonical_ticker(row.get("ticker"), "Consumer Stage 6 input ticker")
        cohort = str(row.get("calibration_cohort_id") or "")
        if (
            not ticker
            or ticker in input_index
            or cohort not in baseline["cohort_models"]
        ):
            raise ValueError("Consumer Stage 6 input ticker/cohort census is invalid")
        _exact_date(row.get("asof_date"), asof, f"{ticker} input asof")
        if (
            row["definition_version"] != baseline["stage6_definition_version"]
            or row["contract_sha256"] != baseline["stage6_contract_sha256"]
            or row["source_id"] != baseline["stage6_input_source_id"]
            or _strict_flag(
                row["calibration_eligible_flag"],
                f"{ticker} Stage 6 calibration eligibility",
            )
            not in (0, 1)
            or row["input_observation_id"] != input_observation_id(row)
        ):
            raise ValueError(f"{ticker}: Stage 6 input identity does not replay")
        input_index[ticker] = row

    expected_names = CORE_SET | set(baseline["specialized_component_names"])
    specs = {spec.name: spec for spec in CORE_COMPONENT_SPECS}
    by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for raw in components:
        if not isinstance(raw, dict) or set(raw) != component_fields:
            raise ValueError("Consumer Stage 6 component row field census changed")
        row = dict(raw)
        ticker = _canonical_ticker(
            row.get("ticker"),
            "Consumer Stage 6 component ticker",
        )
        name = str(row.get("component_name") or "")
        if ticker not in input_index or name not in expected_names or (ticker, name) in seen:
            raise ValueError("Consumer Stage 6 component matrix is invalid")
        seen.add((ticker, name))
        _exact_date(row.get("asof_date"), asof, f"{ticker}/{name} asof")
        source_asof = row.get("source_asof_date")
        if source_asof is not None:
            if type(source_asof) is not str:
                raise ValueError(f"{ticker}/{name}: source asof is invalid")
            source_text = source_asof
            try:
                parsed_source = date.fromisoformat(source_text)
            except ValueError as exc:
                raise ValueError(f"{ticker}/{name}: source asof is invalid") from exc
            if parsed_source.isoformat() != source_text or source_text > asof:
                raise ValueError(f"{ticker}/{name}: source is post-checkpoint")
        if (
            row["calibration_cohort_id"]
            != input_index[ticker]["calibration_cohort_id"]
            or row["definition_version"] != baseline["stage6_definition_version"]
            or row["contract_sha256"] != baseline["stage6_contract_sha256"]
            or _finite(row["component_weight"], f"{ticker}/{name} weight") != 0.0
            or row["component_observation_id"] != component_observation_id(row)
        ):
            raise ValueError(f"{ticker}/{name}: component identity does not replay")
        if name in specs:
            spec = specs[name]
            observed = (
                row["component_group"],
                row["source_table"],
                row["source_field"],
                row["direction"],
                row["rank_requirement"],
                row["unit"],
            )
            expected = (
                spec.group,
                spec.source_table,
                spec.source_field,
                spec.direction,
                spec.rank_requirement,
                spec.unit,
            )
            if observed != expected:
                raise ValueError(f"{ticker}/{name}: component contract changed")
        elif row["component_group"] != "specialized":
            raise ValueError(f"{ticker}/{name}: specialized group changed")
        by_ticker[ticker].append(row)
    if any(
        {row["component_name"] for row in by_ticker[ticker]} != expected_names
        for ticker in input_index
    ):
        raise ValueError("Consumer component matrix is not exact per ticker")
    _validate_normalization(
        components,
        minimum_peers=int(baseline["minimum_normalization_peer_count"]),
    )
    return input_index, by_ticker


def _validated_atomic_feature_snapshot(
    *,
    asof_date: str,
    feature_snapshot_path: Path,
    frozen_baseline_spec_path: Path,
    expected_cohorts: Sequence[str],
    feature_snapshot_bytes: bytes | None,
    frozen_baseline_spec_bytes: bytes | None,
) -> tuple[
    str,
    dict[str, Any],
    dict[str, dict[str, Any]],
    dict[str, list[dict[str, Any]]],
    dict[str, Any],
]:
    asof = _exact_date(asof_date, asof_date, "capture asof")
    baseline = validate_frozen_baseline_spec(
        frozen_baseline_spec_path,
        expected_cohorts=expected_cohorts,
        baseline_snapshot_bytes=frozen_baseline_spec_bytes,
    )
    snapshot_path = Path(feature_snapshot_path).expanduser().resolve()
    if feature_snapshot_bytes is None:
        snapshot, snapshot_sha256, snapshot_path, snapshot_size = (
            read_json_snapshot(
                snapshot_path,
                label="Consumer atomic score snapshot",
            )
        )
    else:
        raw_bytes = bytes(feature_snapshot_bytes)
        snapshot = _load_json(
            snapshot_path,
            "Consumer atomic score snapshot",
            snapshot_bytes=raw_bytes,
        )
        snapshot_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        snapshot_size = len(raw_bytes)
    if set(snapshot) != FEATURE_SNAPSHOT_FIELDS:
        raise ValueError("Consumer atomic score snapshot top-level census changed")
    if (
        snapshot.get("schema_version") != FEATURE_SNAPSHOT_SCHEMA
        or snapshot.get("evidence_role") != "prospective_future_only_capture"
    ):
        raise ValueError("Consumer atomic score snapshot changed identity")
    _exact_date(snapshot.get("asof_date"), asof, "atomic score snapshot asof")
    inputs, components = _atomic_rows(snapshot, baseline=baseline, asof=asof)
    input_ids = sorted(
        row["input_observation_id"] for row in inputs.values()
    )
    component_ids = sorted(
        row["component_observation_id"]
        for ticker_rows in components.values()
        for row in ticker_rows
    )
    audit = {
        "schema_version": "consumer_defensive_atomic_feature_snapshot_audit_v1",
        "asof_date": asof,
        "frozen_model_identity_sha256": baseline["model_identity_sha256"],
        "stage6_contract_sha256": baseline["stage6_contract_sha256"],
        "ticker_count": len(inputs),
        "ticker_census_sha256": canonical_sha256(sorted(inputs)),
        "input_row_count": len(input_ids),
        "input_rows_sha256": snapshot["input_rows_sha256"],
        "input_observation_ids_sha256": canonical_sha256(input_ids),
        "component_row_count": len(component_ids),
        "component_rows_sha256": snapshot["component_rows_sha256"],
        "component_observation_ids_sha256": canonical_sha256(component_ids),
        "source_snapshot": {
            "path": str(snapshot_path),
            "sha256": snapshot_sha256,
            "bytes": snapshot_size,
        },
        "exact_atomic_row_census_pass": True,
        "exact_stage6_identity_replay_pass": True,
        "no_post_asof_component_sources_pass": True,
        "production_activation_authorized": False,
    }
    return asof, baseline, inputs, components, audit


def validate_consumer_atomic_feature_snapshot(
    *,
    asof_date: str,
    feature_snapshot_path: Path,
    frozen_baseline_spec_path: Path,
    expected_cohorts: Sequence[str],
    feature_snapshot_bytes: bytes | None = None,
    frozen_baseline_spec_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Validate exact Stage 6 rows before requesting source attestations."""

    *_, audit = _validated_atomic_feature_snapshot(
        asof_date=asof_date,
        feature_snapshot_path=feature_snapshot_path,
        frozen_baseline_spec_path=frozen_baseline_spec_path,
        expected_cohorts=expected_cohorts,
        feature_snapshot_bytes=feature_snapshot_bytes,
        frozen_baseline_spec_bytes=frozen_baseline_spec_bytes,
    )
    return audit


def _validate_component_availability_crosswalk(
    components: Mapping[str, Sequence[Mapping[str, Any]]],
    availability: Mapping[str, Mapping[str, Any]],
) -> dict[str, str | None]:
    max_by_ticker: dict[str, str | None] = {}
    for ticker in sorted(components):
        available_times = []
        for component in components[ticker]:
            component_id = component["component_observation_id"]
            row = availability.get(component_id)
            if row is None:
                raise ValueError(
                    f"{ticker}: component lacks attested source availability"
                )
            expected_source_required = int(
                component.get("source_asof_date") is not None
                or component.get("source_id") is not None
            )
            expected = {
                "ticker": ticker,
                "component_name": component["component_name"],
                "availability_status": component["availability_status"],
                "source_required_flag": expected_source_required,
                "source_table": component["source_table"],
                "source_id": component["source_id"],
                "source_field": component["source_field"],
                "source_asof_date": component["source_asof_date"],
                "component_input_value_sha256": component_input_value_sha256(
                    component
                ),
            }
            if any(row.get(field) != value for field, value in expected.items()):
                raise ValueError(
                    f"{ticker}/{component['component_name']}: "
                    "attested source availability differs from atomic component"
                )
            if (
                component["availability_status"] == "available"
                and expected_source_required != 1
            ):
                raise ValueError(
                    f"{ticker}/{component['component_name']}: "
                    "available component lacks source provenance"
                )
            source_available = row.get("source_available_at_utc")
            if source_available is not None:
                available_times.append(
                    exact_utc(
                        source_available,
                        label=f"{ticker}/{component['component_name']} "
                        "source availability",
                    )
                )
        max_by_ticker[ticker] = (
            max(available_times).isoformat() if available_times else None
        )
    return max_by_ticker


def _replay_stage7(
    inputs: Mapping[str, dict[str, Any]],
    components: Mapping[str, list[dict[str, Any]]],
    *,
    baseline: Mapping[str, Any],
    asof: str,
) -> dict[str, dict[str, Any]]:
    output: list[dict[str, Any]] = []
    weights = baseline["stage7_core_weights"]
    contract = baseline["stage7_output_contract"]
    for ticker in sorted(inputs):
        input_row = inputs[ticker]
        rows = components[ticker]
        by_name = {row["component_name"]: row for row in rows}
        input_ready, input_reasons = _rank_readiness(rows)
        core_available = sum(
            by_name[name]["availability_status"] == "available"
            and by_name[name]["normalized_value"] is not None
            for name in CORE_NAMES
        )
        applicable_specialized = sum(
            row["component_group"] == "specialized"
            and row["availability_status"] != "not_applicable"
            for row in rows
        )
        available_specialized = sum(
            row["component_group"] == "specialized"
            and row["availability_status"] in {"available", "measurement_only"}
            and row["raw_value"] is not None
            for row in rows
        )
        denominator = len(CORE_NAMES) + applicable_specialized
        full_quality = (
            (core_available + available_specialized) / denominator
            if denominator
            else 0.0
        )
        expected_reason = None if input_ready else ";".join(sorted(input_reasons))
        try:
            input_lineage = json.loads(str(input_row["lineage_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{ticker}: invalid Stage 6 input lineage") from exc
        expected_ids = sorted(row["component_observation_id"] for row in rows)
        if (
            not isinstance(input_lineage, dict)
            or input_lineage.get("component_observation_ids") != expected_ids
            or _strict_flag(
                input_row["rank_ready_flag"], f"{ticker} Stage 6 rank readiness"
            )
            != input_ready
            or input_row["review_reason"] != expected_reason
            or input_row["feature_status"]
            != ("rank_ready" if input_ready else "review_required")
            or _strict_int(
                input_row["core_available_component_count"],
                f"{ticker} core available component count",
                minimum=0,
            )
            != core_available
            or _strict_int(
                input_row["core_missing_component_count"],
                f"{ticker} core missing component count",
                minimum=0,
            )
            != len(CORE_NAMES) - core_available
            or not math.isclose(
                _finite(input_row["core_data_quality_confidence"], "core quality"),
                core_available / len(CORE_NAMES),
                abs_tol=1e-12,
            )
            or not math.isclose(
                _finite(input_row["full_data_quality_confidence"], "full quality"),
                full_quality,
                abs_tol=1e-12,
            )
        ):
            raise ValueError(f"{ticker}: Stage 6 readiness/lineage does not replay")
        scores: dict[str, float] = {}
        quality: dict[str, float] = {}
        weighted = 0.0
        available_weight = 0.0
        missing_weight = 0.0
        missing: list[str] = []
        core_ids: list[str] = []
        for name in CORE_NAMES:
            component = by_name[name]
            core_ids.append(component["component_observation_id"])
            score = _optional_finite(component["component_score"], f"{ticker}/{name}")
            available = component["availability_status"] == "available" and score is not None
            effective = (
                min(100.0, max(0.0, score))
                if available
                else baseline["neutral_score"]
            )
            indicator = 1.0 if available else 0.0
            if not math.isfinite(indicator) or not 0.0 <= indicator <= 1.0:
                raise ValueError(f"{ticker}/{name}: quality is outside [0,1]")
            scores[name] = effective
            quality[name] = indicator
            weighted += weights[name] * effective
            available_weight += weights[name] * indicator
            missing_weight += weights[name] * (1.0 - indicator)
            if not available and weights[name] > 0.0:
                missing.append(name)
        reasons: list[str] = []
        if not input_ready:
            reasons.append(
                "baseline_not_rank_ready:"
                + str(input_row["review_reason"] or "unspecified")
            )
        if available_weight < baseline["minimum_data_quality_confidence"]:
            reasons.append(f"low_data_quality={available_weight:.6f}")
        if missing_weight > baseline["maximum_missing_component_weight"]:
            reasons.append(
                f"missing_component_weight={missing_weight:.6f}:"
                + ",".join(missing)
            )
        ready = int(not reasons)
        eligible = int(
            _strict_flag(
                input_row["calibration_eligible_flag"],
                f"{ticker} Stage 6 calibration eligibility",
            )
            == 1
            and ready == 1
        )
        lineage = {
            "baseline_source_id": contract["baseline_source_id"],
            "baseline_input_observation_id": input_row["input_observation_id"],
            "stage6_contract_sha256": input_row["contract_sha256"],
            "core_component_observation_ids": sorted(core_ids),
            "missing_components": missing,
            "missing_component_weight": missing_weight,
            "missing_value_policy": "neutral_score_contribution_no_weight_redistribution",
            "rank_tie_break_policy": "score_descending_then_ticker_ascending_ordinal",
            "normalization_policy": "stage6a_point_in_time_cohort_then_universe_fallback",
            "specialized_weight": 0.0,
            "specialized_weight_policy": contract["specialized_weight_policy"],
            "factor_validation_campaign_id": contract["factor_validation_campaign_id"],
            "factor_validation_verdict": contract["factor_validation_verdict"],
        }
        output.append(
            {
                "ticker": ticker,
                "asof_date": asof,
                "source_id": contract["source_id"],
                "model_family": "consumer_defensive",
                "model_version": contract["model_version"],
                "baseline_source_id": contract["baseline_source_id"],
                "baseline_input_observation_id": input_row["input_observation_id"],
                "calibration_cohort_id": input_row["calibration_cohort_id"],
                "core_score": weighted,
                "final_score": weighted,
                "component_weights_json": json.dumps(
                    weights, sort_keys=True, separators=(",", ":")
                ),
                "component_scores_json": json.dumps(
                    scores, sort_keys=True, separators=(",", ":")
                ),
                "component_quality_json": json.dumps(
                    quality, sort_keys=True, separators=(",", ":")
                ),
                "data_quality_confidence": available_weight,
                "full_data_quality_confidence": float(
                    input_row["full_data_quality_confidence"]
                ),
                "rank_ready_flag": ready,
                "calibration_eligible_flag": eligible,
                "model_status": "shadow_ready" if ready else "review_required",
                "review_reason": ";".join(reasons) if reasons else None,
                "promotion_state": contract["promotion_state"],
                "portfolio_candidate_gate": int(
                    contract["portfolio_candidate_gate"]
                ),
                "oos_score_valid_flag": int(contract["oos_score_valid_flag"]),
                "model_contract_sha256": baseline["stage7_model_contract_sha256"],
                "lineage_json": json.dumps(
                    lineage, sort_keys=True, separators=(",", ":")
                ),
            }
        )
    _assign_stage7_ranks(output)
    result: dict[str, dict[str, Any]] = {}
    for row in output:
        row["score_observation_id"] = score_observation_id(row)
        result[row["ticker"]] = row
    return result


def validate_and_replay_consumer_scores(
    *,
    asof_date: str,
    signal_cutoff_at_utc: str,
    rank_snapshot_path: Path,
    feature_snapshot_path: Path,
    frozen_baseline_spec_path: Path,
    score_input_availability_snapshot_path: Path,
    score_input_availability_attestation_path: Path,
    expected_score_input_availability_attestation_sha256: str,
    canonical_trust_bundle: CanonicalTrustBundle,
    policy_id: str,
    expected_cohorts: Sequence[str],
    rank_snapshot_bytes: bytes | None = None,
    feature_snapshot_bytes: bytes | None = None,
    frozen_baseline_spec_bytes: bytes | None = None,
    score_input_availability_snapshot_bytes: bytes | None = None,
    score_input_availability_attestation_bytes: bytes | None = None,
) -> dict[str, Any]:
    asof, baseline, inputs, components, atomic_snapshot_audit = (
        _validated_atomic_feature_snapshot(
            asof_date=asof_date,
            feature_snapshot_path=feature_snapshot_path,
            frozen_baseline_spec_path=frozen_baseline_spec_path,
            expected_cohorts=expected_cohorts,
            feature_snapshot_bytes=feature_snapshot_bytes,
            frozen_baseline_spec_bytes=frozen_baseline_spec_bytes,
        )
    )
    component_ids = sorted(
        row["component_observation_id"]
        for ticker_rows in components.values()
        for row in ticker_rows
    )
    availability_index, availability_audit = (
        validate_score_input_availability_snapshot(
            score_input_availability_snapshot_path,
            asof_date=asof,
            expected_component_observation_ids=component_ids,
            signal_cutoff_at_utc=signal_cutoff_at_utc,
            family="consumer_defensive",
            policy_id=policy_id,
            attestation_path=score_input_availability_attestation_path,
            expected_attestation_sha256=(
                expected_score_input_availability_attestation_sha256
            ),
            bundle=canonical_trust_bundle,
            snapshot_bytes=score_input_availability_snapshot_bytes,
            attestation_bytes=score_input_availability_attestation_bytes,
        )
    )
    max_source_available_by_ticker = _validate_component_availability_crosswalk(
        components,
        availability_index,
    )
    stage7 = _replay_stage7(
        inputs,
        components,
        baseline=baseline,
        asof=asof,
    )
    rank = _load_json(
        rank_snapshot_path,
        "Consumer future rank snapshot",
        snapshot_bytes=rank_snapshot_bytes,
    )
    if (
        rank.get("schema_version") != RANK_SNAPSHOT_SCHEMA
        or rank.get("evidence_role") != "prospective_future_only_capture"
    ):
        raise ValueError("Consumer future rank snapshot changed identity")
    _exact_date(rank.get("asof_date"), asof, "future rank snapshot asof")
    rank_rows = rank.get("rows")
    if (
        not isinstance(rank_rows, list)
        or rank.get("rows_sha256") != canonical_sha256(rank_rows)
    ):
        raise ValueError("Consumer rank rows are absent/hash-inconsistent")
    rank_index: dict[str, dict[str, Any]] = {}
    for raw in rank_rows:
        if not isinstance(raw, dict):
            raise ValueError("Consumer rank row must be a mapping")
        ticker = _canonical_ticker(raw.get("ticker"), "Consumer rank ticker")
        if not ticker or ticker in rank_index:
            raise ValueError("Consumer rank ticker census is invalid")
        row = dict(raw)
        _exact_date(row.get("asof_date"), asof, f"{ticker} rank row asof")
        _finite(row.get("score"), f"{ticker} rank score")
        _strict_int(row.get("rank"), f"{ticker} rank", minimum=1)
        for field in (
            "eligible_flag",
            "model_data_eligible_flag",
            "lifecycle_eligible_flag",
            "selected_top_flag",
            "selected_bottom_flag",
        ):
            _strict_flag(row.get(field), f"{ticker} rank {field}")
        if row.get("ranking_mode") != "ranked":
            raise ValueError(f"{ticker}: Consumer future ranking mode changed")
        rank_index[ticker] = row
    if set(rank_index) != set(inputs):
        raise ValueError("Consumer rank and atomic input ticker censuses differ")

    replay_rows: list[dict[str, Any]] = []
    eligibility: dict[str, dict[str, Any]] = {}
    for ticker in sorted(rank_index):
        rank_row = rank_index[ticker]
        stage7_row = stage7[ticker]
        cohort = str(inputs[ticker]["calibration_cohort_id"])
        model = baseline["cohort_models"][cohort]
        if (
            rank_row.get("sleeve_id") != cohort
            or rank_row.get("group_id") != cohort
            or rank_row.get("baseline_candidate_id") != model["candidate_id"]
            or rank_row.get("frozen_model_identity_sha256")
            != baseline["model_identity_sha256"]
            or rank_row.get("stage6_input_observation_id")
            != inputs[ticker]["input_observation_id"]
            or rank_row.get("stage7_score_observation_id")
            != stage7_row["score_observation_id"]
        ):
            raise ValueError(f"{ticker}: frozen score lineage identity changed")
        by_name = {row["component_name"]: row for row in components[ticker]}
        score = 0.0
        available_weight = 0.0
        missing_weight = 0.0
        for name, weight in model["core_weights"].items():
            component_score = _optional_finite(
                by_name[name]["component_score"], f"{ticker}/{name} candidate"
            )
            quality = float(
                by_name[name]["availability_status"] == "available"
                and component_score is not None
            )
            if not math.isfinite(quality) or not 0.0 <= quality <= 1.0:
                raise ValueError(f"{ticker}/{name}: quality is outside [0,1]")
            effective = (
                component_score
                if quality == 1.0
                else baseline["neutral_score"]
            )
            score += weight * min(100.0, max(0.0, float(effective)))
            available_weight += weight * quality
            missing_weight += weight * (1.0 - quality)
        for name, weight in model["specialized_weights"].items():
            component_score = _optional_finite(
                by_name[name]["component_score"], f"{ticker}/{name} candidate"
            )
            quality = float(component_score is not None)
            effective = (
                component_score
                if quality == 1.0
                else baseline["neutral_score"]
            )
            score += weight * min(100.0, max(0.0, float(effective)))
            available_weight += weight * quality
            missing_weight += weight * (1.0 - quality)
        model_eligible, reasons = _model_data_eligibility(
            stage7_eligible_flag=stage7_row["calibration_eligible_flag"],
            available_weight=available_weight,
            missing_weight=missing_weight,
            minimum_quality=baseline["minimum_data_quality_confidence"],
            maximum_missing=baseline["maximum_missing_component_weight"],
        )
        if not math.isclose(
            _finite(rank_row.get("score"), f"{ticker} rank score"),
            score,
            abs_tol=1e-10,
        ):
            raise ValueError(
                f"{ticker}: rank score differs from atomic frozen-model replay"
            )
        eligibility[ticker] = {
            "model_data_eligible_flag": model_eligible,
            "model_data_exclusion_reason_codes": reasons,
        }
        replay_rows.append(
            {
                "ticker": ticker,
                "cohort_id": cohort,
                "candidate_id": model["candidate_id"],
                "score": score,
                "available_weight": available_weight,
                "missing_weight": missing_weight,
                "model_data_eligible_flag": model_eligible,
                "model_data_exclusion_reason_codes": reasons,
                "stage6_input_observation_id": inputs[ticker][
                    "input_observation_id"
                ],
                "stage7_score_observation_id": stage7_row[
                    "score_observation_id"
                ],
            }
        )
    return {
        "schema_version": "consumer_defensive_future_score_replay_audit_v2",
        "score_formula_id": SCORE_FORMULA_ID,
        "quality_gate_arithmetic": QUALITY_GATE_ARITHMETIC,
        "frozen_model_identity_sha256": baseline["model_identity_sha256"],
        "ticker_count": len(replay_rows),
        "replay_rows_sha256": canonical_sha256(replay_rows),
        "model_data_eligibility_by_ticker": eligibility,
        "atomic_feature_snapshot_audit": atomic_snapshot_audit,
        "score_input_availability_audit": availability_audit,
        "signal_cutoff_at_utc": availability_audit["signal_cutoff_at_utc"],
        "max_source_available_at_utc": availability_audit[
            "max_source_available_at_utc"
        ],
        "max_source_available_at_utc_by_ticker": (
            max_source_available_by_ticker
        ),
        "source_availability_component_census_sha256": availability_audit[
            "component_observation_ids_sha256"
        ],
        "atomic_stage6_identity_replay_pass": True,
        "exact_source_availability_crosswalk_pass": True,
        "exact_information_cutoff_pass": True,
        "stage6_normalization_replay_pass": True,
        "stage7_score_identity_replay_pass": True,
        "exact_model_score_replay_pass": True,
        "no_reestimation_from_outcomes_pass": True,
    }


__all__ = [
    "BASELINE_SPEC_SCHEMA",
    "FEATURE_SNAPSHOT_SCHEMA",
    "FEATURE_SNAPSHOT_FIELDS",
    "QUALITY_GATE_ARITHMETIC",
    "SCORE_FORMULA_ID",
    "SPECIALIZED_SCORING_POLICY",
    "component_input_value_sha256",
    "validate_consumer_atomic_feature_snapshot",
    "validate_and_replay_consumer_scores",
    "validate_frozen_baseline_spec",
]
